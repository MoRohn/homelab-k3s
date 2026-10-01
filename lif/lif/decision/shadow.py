"""Shadow mode and outcome feedback (spec §29–§30, §52).

    CURRENT EXECUTOR → production action
          └──────→ candidate version (Jev) → shadow table   (never affects the result)

Outcomes arrive later (task success, a human label, a test result) and are joined to
both the provenance record and the shadow rows by state hash. Calibration reads them:
ground truth when an outcome exists, agreement-with-baseline otherwise (reported as such).
"""
from __future__ import annotations

import json
import statistics
import time
from typing import Any

from lif.common import log, metrics
from lif.decision import calibration, provenance
from lif.decision.fabric import DecisionFabric
from lif.decision.store import Store
from lif.decision.types import DecisionDef

LOG = log.get("lif.decision.shadow")


async def record_shadow(fabric: DecisionFabric, store: Store, candidates: list[DecisionDef], state: Any,
                        data_class: str | None, served) -> None:
    sh = provenance.state_hash(state)
    for c in candidates:
        try:
            r = await fabric.evaluate(c.ref, state, data_class)
        except Exception as e:                   # shadow must never disturb production
            LOG.warning("shadow evaluation failed", extra={"fields": {"ref": c.ref, "err": str(e)[:160]}})
            continue
        agree = r.decision == served.answer
        store.insert("shadow", {
            "ts": time.time(), "decision_ref": c.ref, "state_hash": sh, "baseline_answer": served.answer,
            "baseline_executor": served.executor, "baseline_latency_ms": served.latency_ms,
            "baseline_cost_usd": served.cost_usd, "baseline_tokens": None, "candidate_answer": r.decision,
            "candidate_confidence": r.confidence, "candidate_probs": r.probabilities,
            "candidate_provider": r.provider, "candidate_latency_ms": r.latency_ms, "candidate_cost_usd": r.cost_usd,
            "slice": "common" if served.zone in ("high", "n/a") else "edge"})
        metrics.shadow.labels(c.name, "agree" if agree else "disagree").inc()


def record_outcome(store: Store, *, outcome: str, provenance_id: str | None = None, state_hash: str | None = None,
                   decision: str | None = None, source: str = "observed") -> dict:
    """Attach the correct answer (`outcome` = the label that SHOULD have been chosen) to a
    decision. source: observed | human | test. Returns counts of rows updated."""
    ts = time.time()
    n_prov = n_shadow = 0
    if provenance_id:
        rows = store.q("SELECT state_hash, decision_ref, route, answer FROM provenance WHERE id=?", (provenance_id,))
        if not rows:
            raise KeyError(f"no provenance record {provenance_id}")
        state_hash, decision = rows[0]["state_hash"], rows[0]["decision_ref"]
        n_prov = store.x("UPDATE provenance SET outcome=?, outcome_ts=? WHERE id=?", (outcome, ts, provenance_id))
        metrics.outcomes.labels(decision.split("/")[0], rows[0]["route"],
                                "correct" if rows[0]["answer"] == outcome else "incorrect").inc()
    if not state_hash:
        raise ValueError("provenance_id or state_hash is required")
    name = (decision or "").split("/")[0]
    n_shadow = store.x("UPDATE shadow SET outcome=?, outcome_source=?, outcome_ts=? WHERE state_hash=? "
                       "AND decision_ref LIKE ?", (outcome, source, ts, state_hash, f"{name}/%" if name else "%"))
    if not provenance_id:
        n_prov = store.x("UPDATE provenance SET outcome=?, outcome_ts=? WHERE state_hash=? AND decision_ref LIKE ?",
                         (outcome, ts, state_hash, f"{name}/%" if name else "%"))
    return {"provenance": n_prov, "shadow": n_shadow}


def samples(store: Store, ref: str, *, live: bool = True, since: float = 0.0) -> list[calibration.Sample]:
    """Calibration samples for one decision version: shadow rows plus (live=True) its own
    production Jev decisions with outcomes."""
    out = []
    for r in store.q("SELECT * FROM shadow WHERE decision_ref=? AND ts>=?", (ref, since)):
        correct = None if r["outcome"] is None else r["candidate_answer"] == r["outcome"]
        agree = None if r["baseline_answer"] is None else r["candidate_answer"] == r["baseline_answer"]
        sl = r["slice"] or "common"
        if correct is False:
            sl = "failure" if sl == "common" else sl
        out.append(calibration.Sample(confidence=r["candidate_confidence"], correct=correct, agree=agree, slice=sl,
                                      answer=r["candidate_answer"], expected=r["outcome"] or ""))
    if live:
        for r in store.q("SELECT record, outcome FROM provenance WHERE decision_ref=? AND ts>=? AND executor='jev'",
                         (ref, since)):
            rec = json.loads(r["record"])
            if r["outcome"] is None:
                continue
            out.append(calibration.Sample(confidence=rec["confidence"], correct=rec["answer"] == r["outcome"],
                                          slice="common" if rec["route"] == "auto" else "edge",
                                          answer=rec["answer"], expected=r["outcome"]))
    return out


def summary(store: Store, ref: str) -> dict:
    rows = store.q("SELECT * FROM shadow WHERE decision_ref=?", (ref,))
    if not rows:
        return {"decision": ref, "n": 0}
    agree = [r["candidate_answer"] == r["baseline_answer"] for r in rows if r["baseline_answer"] is not None]
    lab = [r for r in rows if r["outcome"] is not None]
    lat_c = [r["candidate_latency_ms"] for r in rows if r["candidate_latency_ms"]]
    lat_b = [r["baseline_latency_ms"] for r in rows if r["baseline_latency_ms"]]
    cost_c = [r["candidate_cost_usd"] for r in rows if r["candidate_cost_usd"] is not None]
    cost_b = [r["baseline_cost_usd"] for r in rows if r["baseline_cost_usd"] is not None]

    def pct(xs, q):
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None
    return {"decision": ref, "n": len(rows), "labelled": len(lab),
            "agreement_rate": round(sum(agree) / len(agree), 4) if agree else None,
            "candidate_accuracy": round(sum(r["candidate_answer"] == r["outcome"] for r in lab) / len(lab), 4)
            if lab else None,
            "baseline_accuracy": round(sum(r["baseline_answer"] == r["outcome"] for r in lab) / len(lab), 4)
            if lab else None,
            "candidate_latency_ms": {"p50": round(statistics.median(lat_c), 1) if lat_c else None,
                                     "p95": pct(lat_c, 0.95)},
            "baseline_latency_ms": {"p50": round(statistics.median(lat_b), 1) if lat_b else None,
                                    "p95": pct(lat_b, 0.95)},
            "candidate_cost_usd_mean": round(statistics.mean(cost_c), 8) if cost_c else None,
            "baseline_cost_usd_mean": round(statistics.mean(cost_b), 8) if cost_b else None,
            "by_slice": _count(r["slice"] for r in rows)}


def _count(xs) -> dict:
    out: dict = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out
