"""Decision Engineering benchmark (spec §100): LLM-first vs the intelligence cascade on the
labelled agent-decision suites in decision-packages/ (synthetic, public; declared PUBLIC).

  BASELINE   every case → local/fast (Qwen3-4B, CPU) picks a label (what LIF agents do today)
  OPTIMIZED  state compiler → Jev; cases below T (or exit answers) → local/reasoning with the
             Jev distribution (EscalationPackage); escalation answers below 0.7 → safe default

Thresholds are NOT calibrated (n ≈ 27 per decision cannot prove an error bound), so the
optimized arm is reported as an exploratory sweep over T. Each case costs at most one Jev
call and one escalation call; every sweep row is computed from those cached answers.
Kimi K3: not configured (no key; privacy blocks external LLMs). GPU: not used.

Run (host, concurrency 1, pauses while the primary workload is HIGH/IMMINENT):
  set -a; . ../.env.local; set +a
  LIF_GATEWAY_URL=http://<gateway clusterIP>:8080 LIF_API_KEY=$(cat ../secrets/lif-operator.key) \\
  LIF_CONTROLLER_URL=http://<controller clusterIP>:8080 LIF_ADMIN_KEY=$(cat ../secrets/lif-admin.key) \\
  .venv/bin/python benchmarks/decision-engineering/run.py
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lif.decision import pricing                                   # noqa: E402
from lif.decision.calibration import wilson                         # noqa: E402
from lif.decision.escalation import EscalationPackage, LocalReasoningProvider   # noqa: E402
from lif.decision.experiments import load_cases                    # noqa: E402
from lif.decision.providers import JevProvider, LocalLLMProvider   # noqa: E402
from lif.decision.state_compiler import compile_state, tokens      # noqa: E402
from lif.decision.types import load_definitions                    # noqa: E402

HERE = Path(__file__).parent
ARMS = set((os.environ.get("ARMS") or "baseline,jev,escalation").split(","))   # ARMS=jev → no local inference
SWEEP = [0.7, 0.8, 0.9, 0.95, 0.98]
ACCEPT = 0.7
GW, KEY = os.environ.get("LIF_GATEWAY_URL", ""), os.environ.get("LIF_API_KEY", "")
CTL, ADMIN = os.environ.get("LIF_CONTROLLER_URL"), os.environ.get("LIF_ADMIN_KEY")


MIN_HEADROOM_MIB = 8192          # gpusched's MemAvailable headroom: below it, local CPU inference thrashes


async def workload_state(http: httpx.AsyncClient) -> tuple[str, float]:
    if not (CTL and ADMIN):
        return "UNKNOWN", 0.0
    try:
        r = await http.get(f"{CTL}/v1/gpu", headers={"Authorization": f"Bearer {ADMIN}"}, timeout=5)
        d = r.json()
        return d.get("state", "UNKNOWN"), float(d.get("mem_available_mib") or 0)
    except Exception:
        return "UNKNOWN", 0.0


async def wait_for_capacity(http, local: bool) -> None:
    """Yield to the primary workload: its state AND host memory headroom (measured 2026-10-01: driving
    the CPU tier at 6 GiB MemAvailable caused ~1.1 GB/s page-ins and 25% iowait until stopped)."""
    while True:
        st, mem = await workload_state(http)
        if st in ("HIGH", "IMMINENT") or st == "UNKNOWN" and local or (local and mem < MIN_HEADROOM_MIB):
            print(f"  waiting: primary workload {st}, MemAvailable {mem:.0f} MiB", flush=True)
            await asyncio.sleep(60)
            continue
        return


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


async def main() -> None:
    defs = load_definitions()
    suites = sorted(HERE.parents[1].glob("decision-packages/*/*/tests.jsonl"))
    jev = JevProvider(os.environ["TYPE_SAFE_JEV_API_KEY"])
    jev.enabled = True
    llm = LocalLLMProvider(GW, "local/fast", api_key=KEY, timeout=180)
    esc = LocalReasoningProvider(GW, "local-reasoning", api_key=KEY)
    esc.limiter.max_wait = 600
    http = httpx.AsyncClient()
    out = {"date": time.strftime("%Y-%m-%d"), "started": time.time(), "jev_model": jev.model,
           "baseline_model": "local/fast", "escalation_model": "local/reasoning", "sweep": SWEEP,
           "accept": ACCEPT, "kimi": "not configured", "arms": sorted(ARMS), "decisions": {}}
    partial = HERE / f"results-{time.strftime('%Y%m%d-%H%M')}.partial.json"
    for suite in suites:
        name = suite.parent.name
        d = defs[f"{name}/v1"]
        cases = load_cases(suite)
        exits = set(d.exits)
        rows = []
        print(f"{d.ref}: {len(cases)} cases", flush=True)
        for i, c in enumerate(cases):
            local = bool(ARMS & {"baseline", "escalation"})
            if local or i % 10 == 0:
                await wait_for_capacity(http, local)
            st = compile_state(d, c["state"]).state
            row = {"expected": c["expected"], "slice": c.get("slice", "common"), "state_tokens": tokens(st)}
            # BASELINE: LLM-first on the raw state (no compiler; that is today's agent behaviour)
            if "baseline" in ARMS:
                try:
                    b = await llm.evaluate(d, c["state"])
                    row.update(base=b.decision, base_ms=b.latency_ms, base_tokens=b.input_tokens)
                except Exception as e:
                    row.update(base=None, base_ms=None, base_err=str(e)[:120])
            # OPTIMIZED: Jev once
            t0 = time.perf_counter()
            j = (await jev.evaluate_group([d], st))[d.name]
            row.update(jev=j.decision, jev_conf=j.confidence, jev_ms=(time.perf_counter() - t0) * 1000,
                       jev_tokens=j.input_tokens, jev_cost=j.cost_usd, jev_probs=j.probabilities)
            # escalation once, if any sweep threshold would send this case up
            if "escalation" in ARMS and (j.confidence < max(SWEEP) or j.decision in exits):
                pkg = EscalationPackage.build(d, st, "exit_answer" if j.decision in exits else "below_threshold",
                                              "PUBLIC", {"answer": j.decision, "confidence": round(j.confidence, 4),
                                                         "probabilities": j.probabilities})
                try:
                    e = await esc.resolve(pkg)
                    row.update(esc=e.answer, esc_conf=e.confidence, esc_ms=e.latency_ms,
                               esc_tokens=e.input_tokens + e.output_tokens)
                except Exception as ex:
                    row.update(esc=None, esc_conf=None, esc_ms=None, esc_err=str(ex)[:120])
            rows.append(row)
        out["decisions"][d.ref] = summarize(d, rows)
        out["decisions"][d.ref]["rows"] = rows
        partial.write_text(json.dumps(out, indent=1, default=str))          # survive interruption
        s = out["decisions"][d.ref]
        print(f"   baseline acc {s['baseline']['accuracy']}  jev-only acc {s['jev_only']['accuracy']}  "
              f"T=0.9 auto {s['sweep']['0.9']['auto_n']}/{s['n']} auto-acc {s['sweep']['0.9']['auto_accuracy']}",
              flush=True)
    out["pooled"] = pooled(out["decisions"])
    out["finished"] = time.time()
    f = HERE / partial.name.replace(".partial", "")
    f.write_text(json.dumps(out, indent=1, default=str))
    partial.unlink(missing_ok=True)
    print(json.dumps(out["pooled"], indent=1))
    print(f"wrote {f}")


def optimized(d, r, t):
    """Answer, latency, local-call flag, safe-default flag for one case at threshold t."""
    exits = set(d.exits)
    if r["jev_conf"] >= t and r["jev"] not in exits:
        return r["jev"], r["jev_ms"], False, False
    e, ec = r.get("esc"), r.get("esc_conf")
    ms = r["jev_ms"] + (r.get("esc_ms") or 0)
    if e is not None and e not in exits and (ec or 0) >= ACCEPT:
        return e, ms, True, False
    return d.default, ms, r.get("esc_ms") is not None, True


def summarize(d, rows):
    n = len(rows)
    base_ok = [r for r in rows if r.get("base") is not None]
    out = {"n": n, "labels": sorted({r["expected"] for r in rows}),
           "baseline": {"accuracy": round(sum(r["base"] == r["expected"] for r in base_ok) / n, 4) if base_ok else None,
                        "errors": n - len(base_ok),
                        "p50_ms": pct([r["base_ms"] for r in base_ok], 0.5),
                        "p95_ms": pct([r["base_ms"] for r in base_ok], 0.95),
                        "local_llm_calls": len(base_ok),
                        "input_tokens": sum(r.get("base_tokens") or 0 for r in base_ok)},
           "jev_only": {"accuracy": round(sum(r["jev"] == r["expected"] for r in rows) / n, 4),
                        "p50_ms": pct([r["jev_ms"] for r in rows], 0.5), "p95_ms": pct([r["jev_ms"] for r in rows], 0.95),
                        "mean_confidence": round(statistics.mean(r["jev_conf"] for r in rows), 4),
                        "jev_input_tokens": sum(r["jev_tokens"] for r in rows),
                        "jev_cost_usd": round(sum(r["jev_cost"] or 0 for r in rows), 6),
                        "accuracy_by_slice": {sl: round(sum(r["jev"] == r["expected"] for r in rows if r["slice"] == sl)
                                                        / max(1, sum(1 for r in rows if r["slice"] == sl)), 4)
                                              for sl in ("common", "edge")}},
           "sweep": {}}
    for t in SWEEP:
        res = [optimized(d, r, t) for r in rows]
        acc = sum(a == r["expected"] for (a, _, _, _), r in zip(res, rows)) / n
        auto = [(a, r) for (a, _, esc, _), r in zip(res, rows)
                if not esc and r["jev_conf"] >= t and r["jev"] not in set(d.exits)]
        out["sweep"][str(t)] = {
            "accuracy": round(acc, 4), "escalation_rate": round(sum(1 for _, _, e, _ in res if e) / n, 4),
            "safe_default_rate": round(sum(1 for *_, s in res if s) / n, 4),
            "auto_accuracy": round(sum(a == r["expected"] for a, r in auto) / len(auto), 4) if auto else None,
            "auto_n": len(auto), "local_llm_calls": sum(1 for _, _, e, _ in res if e),
            "p50_ms": pct([m for _, m, _, _ in res], 0.5), "p95_ms": pct([m for _, m, _, _ in res], 0.95)}
    return out


def pooled(decs):
    allrows = [(ref, r) for ref, s in decs.items() for r in s["rows"]]
    n = len(allrows)
    defs = load_definitions()
    out = {"n": n, "decisions": len(decs),
           "baseline_accuracy": round(sum(r.get("base") == r["expected"] for _, r in allrows) / n, 4)
           if any(r.get("base") for _, r in allrows) else None,
           "baseline_p50_ms": pct([r["base_ms"] for _, r in allrows if r.get("base_ms")], 0.5),
           "baseline_p95_ms": pct([r["base_ms"] for _, r in allrows if r.get("base_ms")], 0.95),
           "baseline_local_calls": sum(1 for _, r in allrows if r.get("base") is not None),
           "baseline_input_tokens": sum(r.get("base_tokens") or 0 for _, r in allrows),
           "jev_only_accuracy": round(sum(r["jev"] == r["expected"] for _, r in allrows) / n, 4),
           "jev_calls": n, "jev_input_tokens": sum(r["jev_tokens"] for _, r in allrows),
           "jev_cost_usd": round(sum(r["jev_cost"] or 0 for _, r in allrows), 6),
           "jev_p50_ms": pct([r["jev_ms"] for _, r in allrows], 0.5),
           "price_source": pricing.provider("typesafe-jev").get("pricing", {}).get("source"), "sweep": {}}
    for t in SWEEP:
        res = [(optimized(defs[ref], r, t), r) for ref, r in allrows]
        auto = [r for ref, r in allrows if r["jev_conf"] >= t and r["jev"] not in set(defs[ref].exits)]
        wrong = sum(1 for r in auto if r["jev"] != r["expected"])
        out["sweep"][str(t)] = {
            "auto_n": len(auto), "auto_share": round(len(auto) / n, 4),
            "auto_accuracy": round(1 - wrong / len(auto), 4) if auto else None,
            "auto_error_ci95_upper": round(wilson(wrong, len(auto))[1], 4) if auto else None,
            "accuracy": round(sum(a == r["expected"] for (a, *_), r in res) / n, 4),
            "local_llm_calls": sum(1 for (_, _, e, _), _ in res if e),
            "safe_defaults": sum(1 for (*_, s), _ in res if s),
            "p50_ms": pct([m for (_, m, _, _), _ in res], 0.5), "p95_ms": pct([m for (_, m, _, _), _ in res], 0.95)}
    return out


def recompute(path: str) -> None:
    """Re-derive summaries from the saved per-case rows (no new calls)."""
    out = json.loads(Path(path).read_text())
    defs = load_definitions()
    for ref, s in out["decisions"].items():
        rows = s["rows"]
        out["decisions"][ref] = {**summarize(defs[ref], rows), "rows": rows}
    out["pooled"] = pooled(out["decisions"])
    Path(path).write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "recompute":
        recompute(sys.argv[2])
    else:
        asyncio.run(main())
