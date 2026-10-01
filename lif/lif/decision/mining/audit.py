"""Agent audit and workflow optimization plans (spec §83, §84). Read-only: never alters production.

Numbers are measured from the traces given. A heavy call counts as avoidable only when
every operation fused into it is CODE or a separable bounded decision.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from lif.common import config
from lif.decision.mining import classify as C
from lif.decision.mining.compiler import compile_item
from lif.decision.mining.miner import MiningResult


def _heavy(executor: str) -> bool:
    heavy = config.get("decision_engineering.heavy_executors") or []
    return any(h in executor for h in heavy)


def audit(res: MiningResult, agent: str | None = None, workflow: str | None = None, top: int = 5) -> dict[str, Any]:
    ops = [o for o in res.operations if (agent is None or o.agent.split("/")[0] == agent)
           and (workflow is None or o.workflow == workflow)]
    by_call: dict[tuple, list[C.Operation]] = defaultdict(list)
    for o in ops:
        if o.kind not in ("execute_tool", "judge_result") and _heavy(o.executor):
            by_call[(o.run_id, o.seq if o.kind not in ("select_tool", "compose_tool_input", "delegate",
                                                       "authorize_action") else _llm_seq(o, ops))].append(o)
    heavy_calls = len(by_call)
    avoidable = sum(1 for g in by_call.values()
                    if all(o.classification in (C.CODE, C.JEV) and (o.separable or o.classification == C.CODE)
                           for o in g if o.kind != "interpret_task") and not any(o.kind == "interpret_task" for o in g))
    splittable = sum(1 for g in by_call.values()
                     if any(o.classification == C.JEV for o in g) and any(o.classification == C.GEN for o in g))
    buckets = {b: sum(1 for o in ops if o.classification == b) for b in C.BUCKETS}
    sigs = {o.signature for o in ops}
    inv = [i for i in res.inventory if i.signature in sigs]
    jev = sorted([i for i in inv if i.classification == C.JEV], key=lambda i: -i.opportunity)
    tokens_in = sum(o.input_tokens for o in ops)
    tokens_out = sum(o.output_tokens for o in ops)
    avoid_tok = sum(i.avoidable_tokens_per_day for i in jev) * res.span_days
    savings = [i.savings_per_day_usd for i in jev if i.savings_per_day_usd is not None]
    return {
        "agent": agent or "all", "workflow": workflow or "all", "runs": len({o.run_id for o in ops}),
        "span_days": round(res.span_days, 3), "steps_analyzed": len(ops), "by_bucket": buckets,
        "heavy_calls": heavy_calls,
        "avoidable_heavy_calls": avoidable,
        "avoidable_heavy_share": round(avoidable / heavy_calls, 4) if heavy_calls else None,
        "calls_with_fused_decisions": splittable,
        "tokens": {"input": tokens_in, "output": tokens_out, "avoidable_estimate": round(avoid_tok)},
        "estimated_savings_per_day_usd": round(sum(savings), 4) if savings else None,
        "savings_note": None if savings else "current executor price unknown (providers.yaml executors); "
                                             "token counts are exact",
        "top_opportunities": [_brief(i) for i in jev[:top]],
        "embedded_decisions": [_brief(i) for i in jev if i.embedded][:top],
        "code_candidates": [_brief(i) for i in inv if i.classification == C.CODE and i.existing_methods][:top],
        "excessive_state": [_brief(i) for i in inv if "excessive_state" in i.detections][:top],
    }


def _llm_seq(o: C.Operation, ops: list[C.Operation]) -> int:
    """Tool-derived operations belong to the LLM call just before them in the same run."""
    prev = [x.seq for x in ops if x.run_id == o.run_id and x.kind == "decide_continue" and x.seq < o.seq]
    return max(prev) if prev else o.seq


def _brief(i) -> dict:
    return {"signature": i.signature, "description": i.description, "calls_per_day": i.calls_per_day, "n": i.n,
            "executor": i.executor, "separable": i.separable_share, "opportunity": i.opportunity,
            "boundedness": i.boundedness, "consistency": i.consistency, "risk": i.risk,
            "primitive": i.recommended_primitive, "top_outputs": i.top_outputs[:5],
            "mean_latency_ms": i.mean_latency_ms, "savings_per_day_usd": i.savings_per_day_usd,
            "existing_methods": i.existing_methods, "weaknesses": i.weaknesses}


def optimize(res: MiningResult, workflow: str | None = None, agent: str | None = None, top: int = 5) -> dict:
    """Migration plan (§84). Recommendations only."""
    a = audit(res, agent=agent, workflow=workflow, top=top)
    plan: list[dict] = []
    sigs = {x["signature"] for x in a["top_opportunities"] + a["embedded_decisions"]}
    for i in [x for x in res.inventory if x.signature in sigs]:
        cand = compile_item(i)
        steps = []
        if i.embedded:
            steps.append("split the loop: decide() the bounded part before generate() (agent_loop.AgentLoop)")
        steps += [f"review draft {cand.ref} (lint {cand.lint})", "write ≥ 20 labelled cases (tests.jsonl)",
                  "local-ai decision test → shadow (baseline keeps serving)",
                  "collect outcomes until calibration is adequate", "local-ai decision calibrate → owner promotes"]
        if cand.reuse:
            steps.insert(0, f"reuse agent-core/{cand.reuse} with agent-specific state instead of a new schema")
        plan.append({"signature": i.signature, "candidate": cand.ref, "primitive": cand.spec["primitive"],
                     "reuse": cand.reuse, "lint": cand.lint, "notes": cand.notes, "steps": steps,
                     "expected": {"calls_per_day": i.calls_per_day, "separable": i.separable_share,
                                  "savings_per_day_usd": i.savings_per_day_usd,
                                  "latency_reduction_ms": i.latency_reduction_ms}})
    for c in a["code_candidates"]:
        plan.append({"signature": c["signature"], "candidate": None, "steps": [
            f"replace the LLM judgment with {c['existing_methods'][0]}"], "expected": {}})
    for x in a["excessive_state"]:
        plan.append({"signature": x["signature"], "candidate": None, "steps": [
            "route context through the state compiler (minimum sufficient state)"], "expected": {}})
    return {"audit": a, "plan": plan, "applies_changes": False}
