"""decision-miner: find the bounded decisions hidden inside agent executions (spec §3–§8, §51).

    runs → segment → classify → cluster by signature → output-domain analysis
         → knowledge check (existing code methods / knowledge objects) → inventory + opportunity

Opportunity (§8) is a product of factors in [0, ∞) so that any missing ingredient sinks it:

    opportunity = log1p(calls/day) × log1p(avoidable_tokens/100) × latency_penalty
                  × boundedness × suitability × reversibility × consistency

`avoidable_tokens` counts only separable calls: a decision fused with generation in the
same LLM call saves nothing by itself; it is listed as `embedded` instead (the fix there
is to split the call, which the migration plan says).
"""
from __future__ import annotations

import math
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from lif.common import config, log, metrics
from lif.decision import pricing
from lif.decision.mining import classify as C
from lif.decision.mining.traces import Run

LOG = log.get("lif.decision.miner")

# Deterministic methods that already exist in this codebase, keyed by the words an agent
# would use when it re-derives them with an LLM. A match moves the operation to CODE (§51).
KNOWN_CODE_METHODS: list[tuple[frozenset[str], str]] = [
    (frozenset({"fit", "hardware"}), "lif.models.hardware_fit.fit"),
    (frozenset({"fit", "dgx"}), "lif.models.hardware_fit.fit"),
    (frozenset({"fit", "memory"}), "lif.models.hardware_fit.fit"),
    (frozenset({"license"}), "lif.models.discovery.license_ok"),
    (frozenset({"gpu", "admission"}), "lif.gpu.state.can_run"),
    (frozenset({"gpu", "can", "run"}), "lif.gpu.state.can_run"),
    (frozenset({"privacy", "class"}), "lif.policy.engine.classify"),
    (frozenset({"schema", "valid"}), "jsonschema / pydantic validation"),
    (frozenset({"test", "pass"}), "process exit code"),
]


@dataclass
class InventoryItem:
    signature: str
    agent: str
    kind: str
    name: str
    description: str
    classification: str
    class_confidence: float
    n: int
    runs: int
    calls_per_day: float
    executor: str
    separable_share: float
    mean_input_tokens: float
    mean_output_tokens: float
    mean_latency_ms: float | None
    p95_latency_ms: float | None
    cost_per_call_usd: float | None
    distinct_outputs: int
    top_outputs: list[tuple[str, int]]
    boundedness: float
    consistency: float
    reversible: bool
    risk: str
    jev_suitability: float
    weaknesses: list[str]
    detections: list[str]
    reasons: list[str]
    existing_methods: list[str] = field(default_factory=list)
    related_knowledge: list[str] = field(default_factory=list)
    existing_decision: str = ""
    embedded: bool = False
    avoidable_tokens_per_day: float = 0.0
    savings_per_day_usd: float | None = None
    latency_reduction_ms: float | None = None
    opportunity: float = 0.0
    recommended_primitive: str = ""

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


@dataclass
class MiningResult:
    operations: list[C.Operation]
    inventory: list[InventoryItem]
    steps_by_bucket: dict[str, int]
    runs: int
    span_days: float
    generated_at: float = field(default_factory=time.time)

    def top(self, n: int = 10, bucket: str | None = C.JEV) -> list[InventoryItem]:
        xs = [i for i in self.inventory if bucket is None or i.classification == bucket]
        return sorted(xs, key=lambda i: -i.opportunity)[:n]

    def to_dict(self, ops: bool = False) -> dict:
        out = {"generated_at": self.generated_at, "runs": self.runs, "span_days": round(self.span_days, 3),
               "steps_by_bucket": self.steps_by_bucket, "operations_total": len(self.operations),
               "inventory": [i.to_dict() for i in sorted(self.inventory, key=lambda i: -i.opportunity)]}
        if ops:
            out["operations"] = [o.to_dict() for o in self.operations]
        return out


def known_methods(text: str) -> list[str]:
    """Deterministic code that already answers this question. Only these move an operation to CODE."""
    words = set(text.lower().replace("-", " ").replace("_", " ").replace(":", " ").split())
    return sorted({m for keys, m in KNOWN_CODE_METHODS if keys <= words})


def related_knowledge(text: str, knowledge_root: str | None = None) -> list[str]:
    """Methods, rules and incidents in the Knowledge Work layer that a decision designer should read
    first (§51). Advisory: a runbook is not code, so these never reclassify an operation."""
    try:
        from lif.knowledge.context import assemble
        pkg = assemble(text, root=knowledge_root, budget_tokens=400)
    except Exception:
        return []
    words = {w for w in text.lower().replace("-", " ").replace("_", " ").split() if len(w) > 3}
    out = []
    for it in pkg.get("items", []):
        if it.get("type") not in ("method", "rule", "runbook", "incident", "lesson"):
            continue
        blob = f"{it.get('title', '')} {it.get('summary', '')}".lower()
        if len([w for w in words if w in blob]) >= 2:           # real overlap, not a loose FTS hit
            out.append(f"{it.get('type')}:{it.get('key')}")
    return out[:5]


def _entropy_consistency(counts: Counter) -> float:
    n = sum(counts.values())
    if n == 0 or len(counts) <= 1:
        return 1.0 if n else 0.0
    h = -sum(c / n * math.log(c / n) for c in counts.values())
    return round(1 - h / math.log(len(counts)), 4) if len(counts) > 1 else 1.0


def _pct(xs: list[float], q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def mine(runs: Iterable[Run], *, existing_decisions: dict | None = None, check_knowledge: bool = True,
         jev_state_tokens: int | None = None) -> MiningResult:
    runs = list(runs)
    ops: list[C.Operation] = []
    for r in runs:
        for op in C.segment(r):
            ops.append(C.classify(op))
    ts = [o.ts for o in ops if o.ts]
    span = max((max(ts) - min(ts)) / 86400, 1 / 24) if ts else 1.0
    clusters: dict[str, list[C.Operation]] = defaultdict(list)
    for o in ops:
        clusters[o.signature].append(o)
    jev_tokens = jev_state_tokens or int(config.get("decision_engineering.jev_state_tokens_estimate", 400))
    jev_cost = pricing.call_cost("typesafe-jev", jev_tokens, 0) or 0.0
    jev_lat = float(config.get("decision_engineering.jev_latency_ms_estimate", 300))
    inv: list[InventoryItem] = []
    for sig, group in clusters.items():
        inv.append(_item(sig, group, span, existing_decisions or {}, check_knowledge, jev_cost, jev_lat))
    # cluster-level reclassification flows back to operations (UNKNOWN → bounded or generative)
    by_sig = {i.signature: i for i in inv}
    for o in ops:
        it = by_sig[o.signature]
        if o.classification == C.UNKNOWN and it.classification != C.UNKNOWN:
            o.classification = it.classification
            o.reasons = o.reasons + ["cluster-level: " + it.reasons[-1]]
        if it.existing_methods and o.classification == C.JEV:
            o.classification = C.CODE
            o.reasons = o.reasons + [f"existing deterministic method: {it.existing_methods[0]}"]
    buckets = Counter(o.classification for o in ops)
    for o in ops:
        metrics.agent_steps.labels(o.agent, {"CODE": "code", "JEV_CANDIDATE": "jev_candidate",
                                             "GENERATIVE": "generative", "HUMAN_OR_POLICY": "human"}.get(
            o.classification, "unknown")).inc()
    return MiningResult(operations=ops, inventory=inv, steps_by_bucket={b: buckets.get(b, 0) for b in C.BUCKETS},
                        runs=len(runs), span_days=span)


def _item(sig: str, g: list[C.Operation], span: float, existing: dict, check_knowledge: bool, jev_cost: float,
          jev_lat: float) -> InventoryItem:
    first = g[0]
    n = len(g)
    labels = Counter(o.output_label for o in g if o.output_label)
    cls = Counter(o.classification for o in g).most_common(1)[0][0]
    conf = statistics.mean(o.confidence for o in g)
    reasons = list(dict.fromkeys(r for o in g[:50] for r in o.reasons))
    weak = sorted({w for o in g for w in o.weaknesses})
    det = sorted({d for o in g for d in o.detections})
    labelled = sum(labels.values())
    # output domain (§6): the agent keeps generating the same small set of answers
    if cls in (C.UNKNOWN, C.JEV) and first.kind == "llm_call" and n >= 5:
        top_cover = sum(c for _, c in labels.most_common(12)) / n
        if labelled / n >= 0.9 and top_cover >= 0.9:
            cls, conf = C.JEV, max(conf, 0.8)
            det.append("repeatedly generates the same small category of answer")
            reasons.append(f"{len(labels)} distinct outputs cover {top_cover:.0%} of {n} calls")
        elif labelled / n < 0.3:
            cls, conf = C.GEN, max(conf, 0.7)
            reasons.append("outputs are free-form text")
    boundedness = 1.0 if first.bounded else (labelled / n if n else 0.0)
    if cls == C.GEN:
        boundedness = min(boundedness, 0.1)
    consistency = _entropy_consistency(labels) if labels else 0.0
    if first.kind in ("select_tool", "decide_continue", "judge_result", "delegate"):
        consistency = max(consistency, 0.5)     # choice among live options: low entropy is not required
    reversible = all(o.reversible for o in g)
    risk = "critical" if not reversible else ("medium" if first.kind in ("judge_result", "decide_continue")
                                              else "low")
    sep = sum(1 for o in g if o.separable) / n
    tin = statistics.mean(o.input_tokens for o in g)
    tout = statistics.mean(o.output_tokens for o in g)
    lats = [o.latency_ms for o in g if o.latency_ms]
    lat = statistics.mean(lats) if lats else None
    executor = Counter(o.executor for o in g).most_common(1)[0][0]
    cpc = _executor_cost(executor, tin, tout)
    text = f"{first.kind} {first.name} {first.features.get('purpose', '')}"
    methods = known_methods(text)
    related = related_knowledge(text) if check_knowledge and cls == C.JEV else []
    exist = next((k for k, d in existing.items() if "/" not in k and (k in sig or k == first.name)), "")
    suit = 0.0
    if cls == C.JEV:
        suit = conf * (0.5 if weak else 1.0) * (0.5 if methods else 1.0) * (0.9 if related else 1.0)
    calls_day = n / span
    avoid_tokens = calls_day * sep * (tin + tout)
    savings = None if cpc is None else round(calls_day * sep * (cpc - jev_cost), 6)
    lat_red = round(sep * (lat - jev_lat), 1) if (lat and sep) else None
    lat_pen = 1 + min(2.0, (lat or 0) / 2000)
    opp = (math.log1p(calls_day) * math.log1p(sep * (tin + tout) / 100) * lat_pen * boundedness * suit
           * (1.0 if reversible else 0.0) * max(consistency, 0.1)) if cls == C.JEV else 0.0
    prim = ""
    if cls == C.JEV:
        if first.kind == "decide_continue":
            prim = "choice"            # done / not_done / blocked / uncertain (§42)
        elif set(labels) <= {"yes", "no", "true", "false", "proceed", "retry"} and len(labels) <= 2:
            prim = "noul"
        else:
            prim = "choice"
    if methods and cls == C.JEV:
        cls = C.CODE
        reasons.append(f"an existing deterministic method covers this: {methods[0]}")
    return InventoryItem(
        signature=sig, agent=first.agent, kind=first.kind, name=first.name,
        description=C.KINDS.get(first.kind, first.kind) + ("" if first.kind in ("select_tool", "decide_continue",
                                                                              "judge_result")
                                                           else f" ({first.name})"),
        classification=cls, class_confidence=round(conf, 3), n=n, runs=len({o.run_id for o in g}),
        calls_per_day=round(calls_day, 2), executor=executor, separable_share=round(sep, 3),
        mean_input_tokens=round(tin, 1), mean_output_tokens=round(tout, 1),
        mean_latency_ms=round(lat, 1) if lat else None, p95_latency_ms=_pct(lats, 0.95),
        cost_per_call_usd=cpc, distinct_outputs=len(labels), top_outputs=labels.most_common(8),
        boundedness=round(boundedness, 3), consistency=round(consistency, 3), reversible=reversible, risk=risk,
        jev_suitability=round(suit, 3), weaknesses=weak, detections=sorted(set(det)), reasons=reasons[-6:],
        existing_methods=methods, related_knowledge=related, existing_decision=exist, embedded=(cls == C.JEV and sep < 0.5),
        avoidable_tokens_per_day=round(avoid_tokens, 1), savings_per_day_usd=savings, latency_reduction_ms=lat_red,
        opportunity=round(opp, 4), recommended_primitive=prim)


def _executor_cost(executor: str, tin: float, tout: float) -> float | None:
    """Per-call cost of the CURRENT executor from providers.yaml `executors:` (model id → provider)."""
    key = pricing.executor_provider(executor)
    return pricing.call_cost(key, int(tin), int(tout)) if key else None
