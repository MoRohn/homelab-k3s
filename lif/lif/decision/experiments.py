"""experiment-runner: decision tests, version regression and baseline-vs-candidate (spec §34, §69, §100).

    run_tests(fabric, ref, cases)          accuracy, confusion, coverage at the zone threshold,
                                           escalation rate, confidence, cost, latency p50/p95
    compare(old_report, new_report)        regression verdict under decision_engineering.promotion
    benchmark(baseline_fn, candidate_fn)   equivalent tasks through two executors

Test cases: tests.jsonl lines {"state": {...}, "expected": "<label>", "slice"?: "edge"}.
Results are stored in decision-eng.db (experiments) and, when durable, the knowledge layer.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from lif.common import config
from lif.decision import calibration, provenance
from lif.decision.cascade import ZonePolicy
from lif.decision.fabric import DecisionFabric
from lif.decision.store import Store


def load_cases(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for i, line in enumerate(p.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        c = json.loads(line)
        if "state" not in c or "expected" not in c:
            raise ValueError(f"{p}:{i}: a case needs `state` and `expected`")
        out.append(c)
    return out


def _pct(xs: list[float], q: float) -> float | None:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


async def run_tests(fabric: DecisionFabric, ref: str, cases: list[dict], *, data_class: str | None = None,
                    concurrency: int = 8, use_cache: bool = False) -> dict:
    d = fabric.definition(ref)
    for c in cases:
        if c["expected"] not in d.labels:
            raise ValueError(f"{ref}: expected {c['expected']!r} is not a label {d.labels}")
    cache, fabric.cache = fabric.cache, (fabric.cache if use_cache else None)
    sem = asyncio.Semaphore(concurrency)

    async def one(c):
        async with sem:
            t0 = time.perf_counter()
            r = await fabric.evaluate(ref, c["state"], c.get("data_class", data_class))
            return c, r, (time.perf_counter() - t0) * 1000
    try:
        rows = await asyncio.gather(*(one(c) for c in cases))
    finally:
        fabric.cache = cache
    zp = ZonePolicy.for_decision(d)
    samples = [calibration.Sample(confidence=r.confidence, correct=r.decision == c["expected"],
                                  slice=c.get("slice", "common"), answer=r.decision, expected=c["expected"],
                                  exit=r.decision in set(d.exits)) for c, r, _ in rows]
    n = len(rows)
    right = sum(1 for s in samples if s.correct)
    confusion: dict[str, dict[str, int]] = {}
    for s in samples:
        confusion.setdefault(s.expected, {}).setdefault(s.answer, 0)
        confusion[s.expected][s.answer] += 1
    at = calibration.at_threshold(samples, zp.high) if zp.high is not None else None
    lat = [ms for _, _, ms in rows]
    providers = {}
    for _, r, _ in rows:
        providers[r.provider] = providers.get(r.provider, 0) + 1
    failures = [{"expected": c["expected"], "answer": r.decision, "confidence": round(r.confidence, 4),
                 "state_hash": provenance.state_hash(c["state"])} for c, r, _ in rows if r.decision != c["expected"]]
    return {"decision": ref, "n": n, "accuracy": round(right / n, 4) if n else None,
            "by_provider": providers, "mean_confidence": round(statistics.mean(s.confidence for s in samples), 4)
            if n else None,
            "threshold": zp.high, "at_threshold": at,
            "escalation_rate": at["escalation_rate"] if at else None,
            "coverage": at["coverage"] if at else None,
            "cost_usd": round(sum(r.cost_usd or 0 for _, r, _ in rows), 6),
            "latency_ms": {"p50": round(statistics.median(lat), 1) if lat else None, "p95": _pct(lat, 0.95),
                           "p99": _pct(lat, 0.99)},
            "confusion": confusion, "failures": failures[:50],
            "by_slice": {sl: round(sum(1 for s in samples if s.slice == sl and s.correct) /
                                   max(1, sum(1 for s in samples if s.slice == sl)), 4)
                         for sl in sorted({s.slice for s in samples})},
            "samples": [s.__dict__ for s in samples]}


def compare(old: dict, new: dict) -> dict:
    """Reject regressions beyond policy (§34)."""
    pol = config.get("decision_engineering.promotion") or {}
    max_reg = float(pol.get("max_regression", 0.01))
    max_cov = float(pol.get("max_coverage_drop", 0.05))
    reasons = []
    da = (new.get("accuracy") or 0) - (old.get("accuracy") or 0)
    if da < -max_reg:
        reasons.append(f"accuracy {old.get('accuracy')} → {new.get('accuracy')} (drop > {max_reg})")
    if old.get("coverage") is not None and new.get("coverage") is not None and \
            new["coverage"] < old["coverage"] - max_cov:
        reasons.append(f"coverage {old['coverage']} → {new['coverage']} (drop > {max_cov})")
    for sl, acc in (old.get("by_slice") or {}).items():
        nv = (new.get("by_slice") or {}).get(sl)
        if nv is not None and nv < acc - max_reg * 3:
            reasons.append(f"slice {sl}: {acc} → {nv}")
    delta = {k: _delta(old.get(k), new.get(k)) for k in ("accuracy", "coverage", "escalation_rate",
                                                         "mean_confidence", "cost_usd")}
    delta["latency_p95_ms"] = _delta((old.get("latency_ms") or {}).get("p95"), (new.get("latency_ms") or {}).get("p95"))
    return {"regression_ok": not reasons, "reasons": reasons, "delta": delta,
            "old": old.get("decision"), "new": new.get("decision")}


def _delta(a, b):
    return None if a is None or b is None else round(b - a, 6)


async def benchmark(cases: list[dict], baseline: Callable[[dict], Awaitable[dict]],
                    candidate: Callable[[dict], Awaitable[dict]], *, concurrency: int = 4) -> dict:
    """Equivalent tasks through two executors. Each fn returns
    {answer, latency_ms?, input_tokens?, output_tokens?, cost_usd?, calls: {tier: n}, human?: bool}."""
    async def side(fn):
        sem = asyncio.Semaphore(concurrency)

        async def one(c):
            async with sem:
                t0 = time.perf_counter()
                out = await fn(c)
                out.setdefault("latency_ms", (time.perf_counter() - t0) * 1000)
                return c, out
        t0 = time.perf_counter()
        rows = await asyncio.gather(*(one(c) for c in cases))
        return rows, time.perf_counter() - t0

    out = {}
    for label, fn in (("baseline", baseline), ("candidate", candidate)):
        rows, wall = await side(fn)
        lat = [o["latency_ms"] for _, o in rows]
        calls: dict[str, int] = {}
        for _, o in rows:
            for k, v in (o.get("calls") or {}).items():
                calls[k] = calls.get(k, 0) + v
        costs = [o.get("cost_usd") for _, o in rows]
        out[label] = {"n": len(rows), "accuracy": round(sum(1 for c, o in rows if o.get("answer") == c["expected"])
                                                         / len(rows), 4) if rows else None,
                      "wall_s": round(wall, 2), "latency_ms": {"p50": round(statistics.median(lat), 1) if lat else None,
                                                                "p95": _pct(lat, 0.95)},
                      "input_tokens": sum(int(o.get("input_tokens", 0)) for _, o in rows),
                      "output_tokens": sum(int(o.get("output_tokens", 0)) for _, o in rows),
                      "cost_usd": None if any(c is None for c in costs) else round(sum(costs), 6),
                      "calls": calls, "human_interventions": sum(1 for _, o in rows if o.get("human")),
                      "errors": sum(1 for _, o in rows if o.get("error"))}
    b, c = out["baseline"], out["candidate"]
    out["verdict"] = {
        "quality_delta": _delta(b["accuracy"], c["accuracy"]),
        "p50_speedup": round(b["latency_ms"]["p50"] / c["latency_ms"]["p50"], 2)
        if b["latency_ms"]["p50"] and c["latency_ms"]["p50"] else None,
        "token_reduction": round(1 - (c["input_tokens"] + c["output_tokens"]) /
                                 max(1, b["input_tokens"] + b["output_tokens"]), 4),
        "quality_ok": (c["accuracy"] or 0) >= (b["accuracy"] or 0) - float(
            (config.get("decision_engineering.promotion") or {}).get("max_regression", 0.01))}
    return out


def save(store: Store | None, name: str, baseline: str, candidate: str, report: dict) -> int | None:
    if store is None:
        return None
    verdict = "pass" if report.get("regression_ok", report.get("verdict", {}).get("quality_ok")) else "fail"
    slim = {k: v for k, v in report.items() if k != "samples"}
    keep = report if len(report.get("samples") or []) <= 5000 else slim      # samples feed the simulator
    rid = store.insert("experiments", {"ts": time.time(), "name": name, "baseline": baseline, "candidate": candidate,
                                       "verdict": verdict, "report": keep})
    provenance.knowledge("experiment", {"id": f"experiment-{rid}", "title": f"{name}: {candidate} vs {baseline}",
                                        "verdict": verdict, "summary": json.dumps(slim.get("verdict") or
                                                                                  slim.get("delta") or {})[:500]},
                         [("validated-by", candidate)])
    return rid
