"""The improvement cycle (spec §52, §102), run in-process by the decision-fabric service.

    every `decision_engineering.cycle.interval_sec` (default 6 h):
      1. mine in-cluster traces ($LIF_TRACE_DIR, written by lif.decision.instrument) and the
         decision log → operations + inventory (Opportunities / Inventory / Agent Traces)
      2. recalibrate every version in shadow / calibrated / automation stages from its
         shadow rows and outcomes
      3. run the autotuner → recommendations (stored; nothing is applied)

CPU only, small, and it yields: it does nothing while the primary workload is IMMINENT
(the controller's gpusched state is not needed: this never touches the GPU, but it also
does not compete for CPU bandwidth during production runs; the service checks
`decision_engineering.cycle.enabled`).
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from lif.common import config, log
from lif.decision import autotune, de_api, shadow
from lif.decision.registry import LIVE, OBSERVED

LOG = log.get("lif.decision.jobs")
LAST: dict = {}


def cycle(rt) -> dict:
    t0 = time.time()
    out: dict = {"started": t0, "mined": None, "calibrated": [], "recommendations": []}
    trace_dir = Path(os.environ.get("LIF_TRACE_DIR", "/data/traces"))
    sources = []
    if trace_dir.exists() and any(trace_dir.rglob("*.jsonl")):
        sources.append(trace_dir)
    if sources:
        from lif.decision.mining.miner import mine
        from lif.decision.mining.traces import jsonl_runs
        res = mine(jsonl_runs(sources), existing_decisions=rt.fabric.defs)
        rt.save_operations(res.operations)
        de_api.LATEST_INVENTORY.clear()
        de_api.LATEST_INVENTORY.update({**res.to_dict(), "source": "in-cluster traces"})
        out["mined"] = {"runs": res.runs, "operations": len(res.operations)}
    for name in rt.registry.names():
        for d in rt.registry.versions(name):
            if rt.registry.stage_of(d.ref) not in (LIVE | OBSERVED):
                continue
            if not shadow.samples(rt.store, d.ref):
                continue
            cal = rt.calibrate(d.ref)
            out["calibrated"].append({"ref": d.ref, "high": cal["recommended"]["high"],
                                      "adequate": cal["adequacy"]["adequate"]})
            rel = rt.registry.active_release(d.name)
            recs = autotune.recommend(d, cal, shadow.summary(rt.store, d.ref), rt.latest_test(d.ref),
                                      pinned_high=(rel.policy or {}).get("high") if rel else d.policy.get("high"))
            out["recommendations"] += recs
    out["elapsed_s"] = round(time.time() - t0, 2)
    LAST.clear()
    LAST.update(out)
    return out


async def loop(rt) -> None:
    c = config.get("decision_engineering.cycle") or {}
    if not c.get("enabled", True):
        return
    await asyncio.sleep(float(c.get("initial_delay_sec", 300)))
    while True:
        try:
            r = await asyncio.to_thread(cycle, rt)
            LOG.info("improvement cycle", extra={"fields": {"mined": r["mined"], "calibrated": len(r["calibrated"]),
                                                            "recommendations": len(r["recommendations"])}})
        except Exception:
            LOG.exception("improvement cycle failed")
        await asyncio.sleep(float(c.get("interval_sec", 21600)))
