"""Decision DAG runtime.

A workflow is a set of nodes with `depends_on`. Every node starts the moment its
dependencies finish; independent nodes run concurrently. Jev nodes that become ready in
the same wave and read the same state are folded into ONE Jev request.

    workflow:
      name: candidate-model-analysis
      version: v1
      nodes:
        license:          {type: deterministic, fn: license_ok}
        architecture_fit: {type: deterministic, fn: architecture_fit}
        suitability:      {type: jev, decision: model-suitability, state: input}
        risk:             {type: jev, decision: operational-risk, state: input}
        recommendation:   {type: policy, fn: candidate_recommendation,
                           depends_on: [license, architecture_fit, suitability, risk]}

Node functions receive a context dict: {"input": <workflow input>, "<node>": <result>, ...}.
`deterministic` and `policy` nodes are registered Python callables (sync or async).
A `jev` node's result is a DecisionResult; its `state` names the context key to evaluate
(default: "input"). Policy nodes are the only ones whose output is an action.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from lif.common import config, log
from lif.decision.fabric import DecisionFabric
from lif.decision.types import DecisionResult

LOG = log.get("lif.decision.dag")


@dataclass
class NodeSpec:
    name: str
    type: str                               # deterministic | jev | policy
    fn: str | None = None
    decision: str | None = None
    state: str = "input"
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Workflow:
    name: str
    version: str
    nodes: dict[str, NodeSpec]
    data_class: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Workflow":
        w = raw.get("workflow", raw)
        nodes = {n: NodeSpec(name=n, **(spec or {})) for n, spec in w["nodes"].items()}
        wf = cls(name=w["name"], version=str(w.get("version", "v1")), nodes=nodes, data_class=w.get("data_class"))
        wf.validate()
        return wf

    def validate(self) -> None:
        for n in self.nodes.values():
            for dep in n.depends_on:
                if dep not in self.nodes:
                    raise ValueError(f"{self.name}: node {n.name} depends on unknown node {dep}")
            if n.type not in ("deterministic", "jev", "policy"):
                raise ValueError(f"{self.name}: node {n.name} has unknown type {n.type}")
            if n.type == "jev" and not n.decision:
                raise ValueError(f"{self.name}: jev node {n.name} needs `decision`")
            if n.type != "jev" and not n.fn:
                raise ValueError(f"{self.name}: node {n.name} needs `fn`")
        # cycle check (Kahn)
        indeg = {n: len(s.depends_on) for n, s in self.nodes.items()}
        ready = [n for n, d in indeg.items() if d == 0]
        seen = 0
        while ready:
            cur = ready.pop()
            seen += 1
            for n, s in self.nodes.items():
                if cur in s.depends_on:
                    indeg[n] -= 1
                    if indeg[n] == 0:
                        ready.append(n)
        if seen != len(self.nodes):
            raise ValueError(f"{self.name}: cycle detected")

    @property
    def ref(self) -> str:
        return f"{self.name}/{self.version}"


@dataclass
class WorkflowRun:
    workflow: str
    results: dict[str, Any]
    timings_ms: dict[str, float]
    waves: list[list[str]]
    total_ms: float
    jev_calls: int

    def to_dict(self) -> dict:
        def ser(v):
            return v.to_dict() if isinstance(v, DecisionResult) else v
        return {"workflow": self.workflow, "results": {k: ser(v) for k, v in self.results.items()},
                "timings_ms": self.timings_ms, "waves": self.waves, "total_ms": self.total_ms,
                "jev_calls": self.jev_calls}


class DagRuntime:
    def __init__(self, fabric: DecisionFabric, functions: dict[str, Callable] | None = None):
        self.fabric = fabric
        self.fns: dict[str, Callable] = dict(functions or {})
        self.workflows: dict[str, Workflow] = {}

    def register(self, name: str):
        def wrap(fn):
            self.fns[name] = fn
            return fn
        return wrap

    def load(self, directory: str | Path | None = None) -> None:
        d = Path(directory) if directory else (
            Path("/etc/lif/workflows") if Path("/etc/lif/workflows").exists() else config.REPO_CONFIG / "workflows")
        for f in sorted(d.glob("*.yaml")):
            wf = Workflow.from_dict(yaml.safe_load(f.read_text()))
            self.workflows[wf.name] = wf

    async def run(self, workflow: str | Workflow, inp: Any, data_class: str | None = None) -> WorkflowRun:
        wf = workflow if isinstance(workflow, Workflow) else self.workflows[workflow]
        ctx: dict[str, Any] = {"input": inp}
        done: set[str] = set()
        timings: dict[str, float] = {}
        waves: list[list[str]] = []
        jev_calls = 0
        t0 = time.perf_counter()
        running: dict[str, asyncio.Task] = {}

        def ready_nodes() -> list[str]:
            return [n for n, s in wf.nodes.items()
                    if n not in done and n not in running and all(d in done for d in s.depends_on)]

        while len(done) < len(wf.nodes):
            ready = ready_nodes()
            if ready:
                waves.append(ready)
                # fold ready jev nodes that share a state source into one group call
                groups: dict[str, list[NodeSpec]] = {}
                for n in ready:
                    s = wf.nodes[n]
                    if s.type == "jev":
                        groups.setdefault(s.state, []).append(s)
                    else:
                        running[n] = asyncio.create_task(self._timed(n, self._run_fn(s, ctx), timings))
                for state_key, specs in groups.items():
                    task = asyncio.create_task(self._timed(
                        "+".join(s.name for s in specs),
                        self.fabric.evaluate_group([s.decision for s in specs], ctx[state_key],
                                                   data_class or wf.data_class), timings))
                    for s in specs:
                        running[s.name] = task
                    jev_calls += 1
            if not running:
                raise RuntimeError(f"{wf.name}: no runnable nodes (unsatisfied dependencies)")
            finished, _ = await asyncio.wait(set(running.values()), return_when=asyncio.FIRST_COMPLETED)
            for n, task in list(running.items()):
                if task in finished:
                    out = task.result()
                    spec = wf.nodes[n]
                    if spec.type == "jev":
                        out = out[self.fabric.definition(spec.decision).name]
                    ctx[n] = out
                    done.add(n)
                    del running[n]
        return WorkflowRun(workflow=wf.ref, results={k: v for k, v in ctx.items() if k != "input"},
                           timings_ms=timings, waves=waves, total_ms=(time.perf_counter() - t0) * 1000,
                           jev_calls=jev_calls)

    async def _run_fn(self, spec: NodeSpec, ctx: dict) -> Any:
        fn = self.fns.get(spec.fn)
        if fn is None:
            raise KeyError(f"node {spec.name}: function '{spec.fn}' is not registered")
        out = fn(ctx)
        return await out if inspect.isawaitable(out) else out

    @staticmethod
    async def _timed(label: str, coro, timings: dict) -> Any:
        t = time.perf_counter()
        try:
            return await coro
        finally:
            timings[label] = round((time.perf_counter() - t) * 1000, 2)
