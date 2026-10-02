"""Router evaluation over synthetic explanation shapes (spec §103–§104).

`synthetic_spec(question, shape)` builds a valid ExplanationSpec with the requested structure and
neutral content; `run(path)` routes every case in evals/understanding/router_cases.yaml and reports
which expectations hold. The same file grows with real cases (minus private content) as feedback
arrives; override rates from the session store show where the router disagrees with readers.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from lif.understanding import analysis, router
from lif.understanding.registry import Registry
from lif.understanding.spec import ExplanationSpec, load

DEFAULT = Path(__file__).resolve().parents[2] / "evals" / "understanding" / "router_cases.yaml"


def synthetic_spec(question: str, shape: dict) -> ExplanationSpec:
    n = max(int(shape.get("concepts", 3)), 2)
    cs = [{"id": f"k{i}", "label": f"Part {i}", "level": 0, "definition": f"Part {i} of the system."} for i in range(n)]
    for i in range(int(shape.get("process", 0))):
        cs.append({"id": f"p{i}", "label": f"Step {i}", "level": 1, "kind": "event"})
    for i in range(int(shape.get("timeline", 0))):
        cs.append({"id": f"t{i}", "label": f"Event {i}", "level": 1, "kind": "event"})
    claims = [{"id": "c0", "text": "Part 0 explains the answer.", "concepts": ["k0"], "importance": "primary",
               "level": 0, "kind": "general"}]
    evidence, srcs, uncs = [], [], []
    for i in range(int(shape.get("observed", 0))):
        srcs.append({"id": f"s{i}", "kind": "metric", "ref": f"metric_{i}"})
        evidence.append({"id": f"e{i}", "source": f"s{i}", "text": f"Part {i % n} was measured."})
        claims.append({"id": f"o{i}", "text": f"Part {i % n} was measured.", "concepts": [f"k{i % n}"],
                       "evidence": [f"e{i}"], "kind": "observed", "importance": "supporting", "level": 1})
    for i in range(int(shape.get("uncertain", 0))):
        claims.append({"id": f"u-c{i}", "text": f"Part {i % n} may contribute.", "concepts": [f"k{i % n}"],
                       "kind": "inferred", "confidence": 0.6, "importance": "supporting", "level": 1})
        uncs.append({"id": f"u{i}", "about": f"u-c{i}", "confidence": 0.6, "reason": "Few observations.", "level": 1})
    doc: dict = {"id": "synthetic", "question": question, "concepts": cs, "claims": claims, "evidence": evidence,
                 "source_refs": srcs, "uncertainties": uncs, "summary": {"headline": "c0", "claims": ["c0"]},
                 "learning_objectives": [{"id": "obj", "text": "Answer the question.", "claims": ["c0"], "level": 0}]}
    k = int(shape.get("chain", 0))
    if k:
        doc["causal_chains"] = [{"id": "ch", "label": "Chain", "steps": [f"k{i % n}" for i in range(k)],
                                 "claim": "c0", "level": 0}]
        if len(set(doc["causal_chains"][0]["steps"])) < 2:
            doc["causal_chains"][0]["steps"] = ["k0", "k1"]
    r = int(shape.get("relationships", 0))
    doc["relationships"] = [{"id": f"r{i}", "from": f"k{i % n}", "to": f"k{(i + 1) % n}",
                             "type": "routes_to" if i % 2 else "depends_on", "level": 1} for i in range(r)]
    if shape.get("process"):
        doc["processes"] = [{"id": "proc", "name": "Process", "steps": [f"p{i}" for i in range(int(shape["process"]))],
                             "level": 1}]
    if shape.get("timeline"):
        doc["timelines"] = [{"id": "tl", "label": "Timeline", "level": 1,
                             "events": [{"id": f"te{i}", "t": i, "label": f"Event {i}", "concept": f"t{i}"}
                                        for i in range(int(shape["timeline"]))]}]
    cells = int(shape.get("comparison", 0))
    if cells:
        dims = max(cells // n, 1)
        doc["comparisons"] = [{"id": "cmp", "label": "Comparison", "level": 1, "items": [c["id"] for c in cs[:n]],
                               "dimensions": [{"id": f"d{j}", "label": f"Dimension {j}"} for j in range(dims)],
                               "cells": [{"item": f"k{i}", "dimension": f"d{j}", "value": "x"}
                                         for i in range(n) for j in range(dims)][:cells]}]
    v = int(shape.get("variables", 0))
    if v:
        doc["variables"] = [{"id": f"v{i}", "symbol": f"x{i}", "label": f"Input {i}", "min": 0, "max": 10, "step": 1,
                             "default": 5, "level": 1} for i in range(v)]
    e = int(shape.get("equations", 0))
    if e:
        doc["equations"] = [{"id": f"eq{i}", "expr": f"y{i} = x0 + x{min(i, v - 1)}", "level": 2} for i in range(e)]
    if shape.get("simulation"):
        doc["variables"] = doc.get("variables") or []
        doc["variables"][:4] = [
            {"id": "v0", "symbol": "M", "label": "Total", "default": 128, "level": 1},
            {"id": "v1", "symbol": "R", "label": "Held back", "min": 0, "max": 64, "step": 16, "default": 32, "level": 1},
            {"id": "v2", "symbol": "m", "label": "Per job", "min": 8, "max": 32, "step": 8, "default": 16, "level": 1}]
        doc["simulation"] = {"primitive": "gpu-reservation-throughput/v1", "variables": ["v0", "v1", "v2"],
                             "bindings": {"v0": "total_mem_gb", "v1": "reserved_gb", "v2": "job_mem_gb"},
                             "controls": [{"variable": "v1"}, {"variable": "v2"}],
                             "outputs": [{"key": "slots", "label": "Slots"}], "claims": ["c0"]}
        doc["simulation"]["bindings"]["v0"] = "total_mem_gb"
        doc["variables"].append({"id": "v-min", "symbol": "t", "label": "Minutes", "default": 20, "level": 1})
        doc["simulation"]["variables"].append("v-min")
        doc["simulation"]["bindings"]["v-min"] = "job_minutes"
    return load(doc)


def check(plan: router.RoutePlan, expect: dict) -> list[str]:
    sel = plan.selected
    fails = []
    if "primary_in" in expect and plan.primary not in expect["primary_in"]:
        fails.append(f"primary {plan.primary} not in {expect['primary_in']}")
    for r in expect.get("must_include", []):
        if r not in sel:
            fails.append(f"{r} not selected")
    for r in expect.get("must_not_include", []):
        if r in sel:
            fails.append(f"{r} selected")
    if "max_artifacts" in expect and len(sel) > expect["max_artifacts"]:
        fails.append(f"{len(sel)} artifacts > {expect['max_artifacts']}")
    return fails


def run(path: str | Path = DEFAULT, registry: Registry | None = None) -> dict:
    cases = yaml.safe_load(Path(path).read_text())["cases"]
    reg = registry or Registry()
    out = []
    for c in cases:
        spec = synthetic_spec(c["question"], c["shape"])
        depth = c.get("depth", "learn")
        plan = router.route(spec, analysis.analyze(spec, depth, c["question"]), reg,
                            router.RouteContext(depth=depth, time_budget_seconds=c.get("budget"),
                                                viewport=c.get("viewport", "desktop")))
        fails = check(plan, c["expect"])
        out.append({"id": c["id"], "ok": not fails, "primary": plan.primary, "selected": plan.selected,
                    "failures": fails})
    return {"cases": out, "passed": sum(1 for x in out if x["ok"]), "total": len(out)}
