"""Decision autotuner (spec §67–§68): conservative RECOMMENDATIONS, never silent changes.

Works in both directions. Jev use is not itself a success metric: when production
evidence shows poor calibration, failing edge cases, excessive escalation or no cost
benefit, it recommends returning the operation to code or to the generative model.

Each recommendation carries its evidence and the exact command an owner would run; the
registry refuses automation releases from machine actors anyway (registry.MACHINE_ACTORS).
"""
from __future__ import annotations

from typing import Any

from lif.decision.lint import lint
from lif.decision.types import DecisionDef


def recommend(d: DecisionDef, cal: dict | None, shadow: dict | None = None, test: dict | None = None,
              cost: dict | None = None, pinned_high: float | None = None, mean_state_tokens: float | None = None,
              co_evaluated: list[str] | None = None) -> list[dict[str, Any]]:
    out: list[dict] = []

    def rec(action: str, why: str, evidence: Any, command: str = "", severity: str = "info"):
        out.append({"decision": d.ref, "action": action, "why": why, "evidence": evidence, "command": command,
                    "severity": severity, "applies_automatically": False})

    if cal:
        hi = (cal.get("recommended") or {}).get("high")
        ad = cal.get("adequacy") or {}
        if not ad.get("adequate"):
            rec("keep_in_shadow", "calibration sample is not adequate yet", ad.get("reasons"))
        elif hi is None:
            rec("return_to_generative", "no threshold meets the consequence bound: Jev cannot carry this decision "
                "safely at any coverage", cal["recommended"].get("detail"), severity="warning")
        elif pinned_high is not None and abs(hi - pinned_high) > 0.02:
            act = "raise_threshold" if hi > pinned_high else "lower_threshold"
            rec(act, f"calibration supports {hi} (pinned {pinned_high})",
                {"curve_at_recommended": next((r for r in cal.get("curve", []) if r["threshold"] >= hi), None)},
                f"local-ai decision promote {d.ref} --threshold {hi}",
                severity="warning" if act == "raise_threshold" else "info")
        ece = cal.get("ece")
        if ece is not None and ece > 0.1:
            rec("recalibrate_or_split", f"ECE {ece}: confidence is not tracking accuracy", ece, severity="warning")
        cov = next((r["coverage"] for r in cal.get("curve", []) if hi and r["threshold"] >= hi), None)
        if cov is not None and cov < 0.3:
            rec("return_to_generative", f"only {cov:.0%} of cases clear the safe threshold; escalation dominates",
                {"coverage": cov}, severity="warning")
    if test:
        weak = {sl: acc for sl, acc in (test.get("by_slice") or {}).items() if acc < (test.get("accuracy") or 0) - 0.1}
        if weak:
            rec("change_question", "edge-case slices fail far more than the average: a criterion is missing",
                weak, f"write {d.name}/v{_next(d.version)} with the missing criterion; shadow it")
        conf = test.get("confusion") or {}
        pairs = [(e, a, n) for e, row in conf.items() for a, n in row.items() if e != a and n >= 3]
        if pairs:
            rec("split_question", "systematic confusion between specific labels", pairs[:5])
    if shadow and shadow.get("n"):
        if shadow.get("agreement_rate") is not None and shadow["agreement_rate"] > 0.995 and shadow["n"] >= 500 \
                and d.type != "score":
            rec("consider_code", "candidate agrees with the baseline > 99.5%; check whether a deterministic rule "
                "would do", {"agreement": shadow["agreement_rate"], "n": shadow["n"]})
    if cost:
        if cost.get("savings_per_day_usd") is not None and cost["savings_per_day_usd"] <= 0:
            rec("return_to_generative", "no cost benefit at current volume and escalation rate", cost)
        if cost.get("escalation_rate") is not None and cost["escalation_rate"] > 0.5:
            rec("return_to_generative", "over half of the cases escalate", cost, severity="warning")
    if mean_state_tokens and mean_state_tokens > 2000:
        rec("reduce_state", f"mean state {mean_state_tokens:.0f} tokens; tighten state_schema / ranking budget",
            mean_state_tokens)
    if co_evaluated:
        rec("merge_fanout", "decisions over the same state hashes are requested separately", co_evaluated)
    errs = [f for f in lint(d) if f.severity == "error"]
    if errs:
        rec("change_question", "lint errors", [f.code for f in errs], severity="warning")
    return out


def _next(v: str) -> int:
    try:
        return int(v.lstrip("v").split(".")[0]) + 1
    except ValueError:
        return 2
