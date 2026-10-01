"""Trace instrumentation (spec §81–§82): wrap model and tool calls so the decision miner
can observe input size, output type, tokens, latency, model, tool and outcome.

Writes the LIF trace format (lif.decision.mining.traces.jsonl_runs) to
$LIF_TRACE_DIR/<agent>/<date>.jsonl. Payloads are NOT logged: an LLM output survives only
as `output_label` when it is a short, non-sensitive label (traces.safe_label).

    tw = TraceWriter(agent="model-discovery")
    with tw.llm("judge-suitability", model="local/fast") as call:
        out = await client.chat(...)
        call.done(output=out.text, input_tokens=..., output_tokens=...)

    @traced_tool(tw, "hf-search")
    async def search(q): ...
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from lif.decision.mining.traces import safe_label


class TraceWriter:
    def __init__(self, agent: str, workflow: str = "", directory: str | Path | None = None, run_id: str | None = None,
                 enabled: bool | None = None):
        self.agent, self.workflow = agent, workflow
        self.dir = Path(directory or os.environ.get("LIF_TRACE_DIR", "/data/traces")) / agent
        self.run_id = run_id or uuid.uuid4().hex[:16]
        self.seq = 0
        self.enabled = enabled if enabled is not None else os.environ.get("LIF_TRACE", "1") != "0"
        self._lock = threading.Lock()

    def step(self, *, kind: str, name: str, output: Any = None, **kw: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.seq += 1
            rec = {"run_id": kw.pop("run_id", self.run_id), "seq": self.seq, "ts": time.time(),
                   "agent": kw.pop("agent", self.agent), "workflow": kw.pop("workflow", self.workflow),
                   "kind": kind, "name": name}
            if output is not None:
                rec["output_label"] = safe_label(output)
                rec["output_type"] = kw.pop("output_type", "label" if rec["output_label"] else "text")
            for k in ("input_tokens", "output_tokens", "latency_ms", "model", "is_error", "outcome", "purpose",
                      "candidates_n", "features"):
                if k in kw and kw[k] is not None:
                    rec[k] = kw[k]
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / f"{time.strftime('%Y-%m-%d')}.jsonl", "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")

    @contextmanager
    def llm(self, purpose: str, model: str = "", **kw) -> Iterator["_Call"]:
        call = _Call()
        t0 = time.perf_counter()
        try:
            yield call
        finally:
            self.step(kind="llm", name=purpose, purpose=purpose, model=model,
                      latency_ms=round((time.perf_counter() - t0) * 1000, 2), **{**kw, **call.data})


class _Call:
    def __init__(self):
        self.data: dict[str, Any] = {}

    def done(self, **kw) -> None:
        self.data.update(kw)


def traced_tool(tw: TraceWriter, name: str):
    def wrap(fn):
        @functools.wraps(fn)
        async def run(*a, **k):
            t0 = time.perf_counter()
            err = False
            try:
                out = fn(*a, **k)
                return await out if inspect.isawaitable(out) else out
            except Exception:
                err = True
                raise
            finally:
                tw.step(kind="tool", name=name, latency_ms=round((time.perf_counter() - t0) * 1000, 2), is_error=err)
        return run
    return wrap
