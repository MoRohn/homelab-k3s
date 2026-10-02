"""The intelligence cascade (spec §2, §21–§23, §45, §78): CODE → JEV → LOCAL → KIMI → HUMAN / SAFE FAIL.

    decide(name, state)
      1. code      a caller precheck, or a registered rule that is certain (conf ≥ code_min)
      2. jev       DecisionFabric (Jev, or its local rules/local_llm fallbacks when Jev is
                   down/forbidden; that is the local-first path of §78)
      3. zones     per-decision ZonePolicy, pinned by the release (never a global threshold):
                     HIGH   (conf ≥ high, not an exit, margin ok) → act
                     MIDDLE                                        → middle_route providers in order
                     LOW    (conf < low)                           → low_route (safe default / human)
      4. escalate  EscalationPackage(state, schema, Jev answer + distribution, reason) to each
                   provider that is available for the state's data class; first valid,
                   non-exit answer at ≥ accept confidence wins
      5. fail safe nothing resolved → human queue (if configured) + the definition's default,
                   with route=safe_default; callers must not treat that as an approval

Release stages (registry.py): a LIVE release at rollout < 100% serves the cascade answer for a
stable hash-slice of states and the caller's baseline for the rest; shadow versions run beside
it and are only logged.

Critical or irreversible decisions are never automated: their route is always human/advisory.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from lif.common import config, log, metrics
from lif.decision import provenance
from lif.decision.escalation import EscalationPackage, EscalationProvider, HumanReviewProvider
from lif.decision.fabric import DecisionFabric
from lif.decision.providers import ProviderUnavailable
from lif.decision.registry import Registry
from lif.decision.store import Store
from lif.decision.types import DecisionDef, DecisionResult, normalize_state
from lif.policy import engine as policy

LOG = log.get("lif.decision.cascade")

Baseline = Callable[[Any], Awaitable[Any] | Any]          # the old executor: returns label or (label, meta)
Validator = Callable[[str, Any], bool]


@dataclass
class ZonePolicy:
    mode: str = "legacy"                  # legacy (4-value gate) | three_zone | two_zone | advisory
    high: float | None = None
    low: float | None = None
    validate: float | None = None         # [validate, high) acts only if the caller's validator passes
    min_margin: float = 0.0               # top-1 minus top-2 probability below this → MIDDLE (§45)
    middle_route: list[str] = field(default_factory=list)
    low_route: list[str] = field(default_factory=lambda: ["safe_default"])
    exit_route: list[str] | None = None
    accept: float = 0.7                   # escalation self-confidence needed to act on its answer
    code_min: float = 0.99                # a rule this certain resolves at the CODE tier
    calibrated: bool = False              # thresholds came from a calibration report

    @classmethod
    def for_decision(cls, d: DecisionDef) -> "ZonePolicy":
        p = dict(d.policy or {})
        dflt = config.get("decision_engineering.cascade") or {}
        zp = cls(**{k: v for k, v in {**dflt.get("defaults", {}), **p}.items() if k in cls.__dataclass_fields__})
        if not zp.middle_route:
            zp.middle_route = list(dflt.get("middle_route", ["local_reasoning", "kimi"]))
        if d.risk == "critical" or not d.reversible:
            zp.mode, zp.low_route = "advisory", ["human", "safe_default"]
        if zp.mode == "legacy":
            t = {**(config.get("decision_fabric.confidence") or {}), **d.thresholds}
            zp.high = float(t.get("auto", 0.97)) if zp.high is None else zp.high
            zp.validate = float(t.get("validate", 0.85)) if zp.validate is None else zp.validate
            zp.low = float(t.get("escalate", 0.70)) if zp.low is None else zp.low
        if zp.mode == "two_zone":
            zp.low = None
        return zp


@dataclass
class CascadeResult:
    answer: str
    confidence: float
    executor: str                         # code | jev | rules | local_llm | local_reasoning | kimi | … | human | baseline | default
    route: str                            # auto | validated | escalated | human | safe_default | baseline | advisory
    decision_version: str
    escalated: bool = False
    latency_ms: float = 0.0
    cost_usd: float | None = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    zone: str = ""                        # high | middle | low | exit | n/a
    action: str = ""                      # legacy gate value for existing callers
    degraded: bool = False
    pending_ticket: int | None = None
    provenance_id: str = ""
    chain: list[dict] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """True when the answer may drive an automatic action. Safe defaults, pending human
        reviews and advisory decisions are not actionable."""
        return self.route in ("auto", "validated", "escalated", "baseline")

    def to_dict(self) -> dict:
        return {**asdict(self), "actionable": self.actionable}


def _bucket(state: Any, salt: str) -> float:
    h = hashlib.sha256((salt + normalize_state(state)).encode()).digest()
    return int.from_bytes(h[:4], "big") / 2**32 * 100


def _margin(probs: dict[str, float]) -> float:
    v = sorted(probs.values(), reverse=True)
    return (v[0] - v[1]) if len(v) > 1 else 1.0


class Cascade:
    def __init__(self, fabric: DecisionFabric, registry: Registry | None = None,
                 escalation: dict[str, EscalationProvider] | None = None, store: Store | None = None,
                 shadow_sample: float | None = None):
        self.fabric, self.registry, self.store = fabric, registry, store
        self.providers: dict[str, EscalationProvider] = dict(escalation or {})
        self.human = next((p for p in self.providers.values() if isinstance(p, HumanReviewProvider)), None)
        self.shadow_sample = float(shadow_sample if shadow_sample is not None else
                                   config.get("decision_engineering.shadow.sample_pct", 100))
        self._bg: set[asyncio.Task] = set()

    # ── public ───────────────────────────────────────────────────────────────

    async def decide(self, name: str, state: Any, *, data_class: str | None = None, agent: str = "",
                     workflow: str = "", baseline: Baseline | None = None, code: Baseline | None = None,
                     validator: Validator | None = None,
                     choices: list[str] | dict[str, str] | None = None) -> CascadeResult:
        return (await self.decide_group([name], state, data_class=data_class, agent=agent, workflow=workflow,
                                        baselines={name: baseline} if baseline else None,
                                        code={name: code} if code else None,
                                        validators={name: validator} if validator else None,
                                        choices={name: choices} if choices is not None else None))[name]

    async def decide_group(self, names: list[str], state: Any, *, data_class: str | None = None, agent: str = "",
                           workflow: str = "", baselines: dict[str, Baseline] | None = None,
                           code: dict[str, Baseline] | None = None,
                           validators: dict[str, Validator] | None = None,
                           choices: dict[str, list[str] | dict[str, str]] | None = None
                           ) -> dict[str, CascadeResult]:
        """Independent decisions over ONE state: one Jev request carries all of them (§15, §50).
        `choices` narrows a choice decision to the options valid right now (§39)."""
        t0 = time.perf_counter()
        baselines, code, validators, choices = baselines or {}, code or {}, validators or {}, choices or {}
        out: dict[str, CascadeResult] = {}
        serving: dict[str, DecisionDef] = {}
        for n in names:
            d = self._serving(n)
            if d is not None and n in choices:
                d = self._with_choices(d, choices[n])
            if d is None:
                # No version serves yet: the old executor answers; candidates only shadow.
                if n not in baselines:
                    raise KeyError(f"decision {n!r} has no serving version and no baseline was given")
                out[n] = await self._baseline(n, state, baselines[n])
            else:
                serving[n] = d
        # 1. CODE tier
        for n, d in list(serving.items()):
            r = await self._code(d, state, code.get(n))
            if r is not None:
                out[n] = r
                del serving[n]
        # rollout slice: states outside the slice keep the baseline
        for n, d in list(serving.items()):
            rel = self.registry.active_release(d.name) if self.registry else None
            if rel is not None and rel.rollout_pct < 100 and n in baselines and \
                    _bucket(state, d.ref) >= rel.rollout_pct:
                out[n] = await self._baseline(n, state, baselines[n])
                del serving[n]
        # 2. JEV tier: one grouped request for everything left
        if serving:
            got = await self.fabric.evaluate_group([self._key(d) for d in serving.values()], state, data_class)
            cls = self._data_class(state, data_class, next(iter(serving.values())))
            routed = await asyncio.gather(*(self._route(d, state, got[d.name], cls, validators.get(n))
                                            for n, d in serving.items()))
            for n, r in zip(serving, routed):
                out[n] = r
        # 3. shadow candidates + provenance
        for n in names:
            r = out[n]
            if not r.latency_ms:
                r.latency_ms = (time.perf_counter() - t0) * 1000
            self._provenance(n, state, r, agent, workflow)
            if r.pending_ticket and self.store is not None:     # the human's answer becomes this decision's outcome
                self.store.x("UPDATE human_queue SET provenance_id=? WHERE id=?", (r.provenance_id, r.pending_ticket))
            self._spawn_shadow(n, state, data_class, r)
            metrics.cascade.labels(n, _tier(r)).inc()
        return out

    def status(self) -> dict:
        return {"providers": {k: p.status() for k, p in self.providers.items()},
                "shadow_sample_pct": self.shadow_sample, "background_tasks": len(self._bg)}

    async def drain(self) -> None:
        """Wait for shadow work (tests, shutdown)."""
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # ── dynamic choices ──────────────────────────────────────────────────────

    def _with_choices(self, d: DecisionDef, allowed: list[str] | dict[str, str]) -> DecisionDef:
        if d.type != "choice":
            raise ValueError(f"{d.ref}: dynamic choices need a choice decision")
        desc = allowed if isinstance(allowed, dict) else {a: d.choices.get(a, "") for a in allowed}
        exits = set(d.exits) | ({"other", "none", "unknown", "insufficient_information"} & set(d.choices))
        opts = {k: (v or d.choices.get(k) or k.replace("_", " ")) for k, v in desc.items()}
        for e in exits:                                   # the exit always stays available (rule 5)
            opts.setdefault(e, d.choices[e])
        if len(opts) - len(exits & set(opts)) < 1:
            raise ValueError(f"{d.ref}: no valid options left; decide in code (nothing to choose)")
        h = hashlib.sha256(",".join(sorted(opts)).encode()).hexdigest()[:8]
        nd = dataclasses.replace(d, choices=opts, pins={**d.pins, "choice_set": h},
                                 default=d.default if d.default in opts else next(iter(exits & set(opts)), None))
        self.fabric.defs[self._key(nd)] = nd
        return nd

    @staticmethod
    def _key(d: DecisionDef) -> str:
        cs = d.pins.get("choice_set")
        return f"{d.ref}#{cs}" if cs else d.ref

    # ── tiers ────────────────────────────────────────────────────────────────

    def _serving(self, name: str) -> DecisionDef | None:
        if self.registry is not None:
            return self.registry.resolve(name)
        return self.fabric.defs.get(name)

    async def _code(self, d: DecisionDef, state: Any, fn: Baseline | None) -> CascadeResult | None:
        zp = ZonePolicy.for_decision(d)
        t0 = time.perf_counter()
        if fn is not None:
            v = fn(state)
            v = await v if asyncio.iscoroutine(v) else v
            if v is not None:
                if v not in d.labels:
                    raise ValueError(f"code precheck for {d.name} returned unknown label {v!r}")
                return CascadeResult(answer=v, confidence=1.0, executor="code", route="auto", decision_version=d.ref,
                                     zone="n/a", action=policy.Gate.AUTO, probabilities={v: 1.0},
                                     latency_ms=(time.perf_counter() - t0) * 1000)
        if self.fabric.rules.has(d.name):
            try:
                rr = await self.fabric.rules.evaluate(d, state)
            except (ProviderUnavailable, ValueError, TypeError, KeyError, AttributeError):
                return None
            if rr.confidence >= zp.code_min and zp.mode != "advisory":
                return CascadeResult(answer=rr.decision, confidence=rr.confidence, executor="code", route="auto",
                                     decision_version=d.ref, zone="n/a", action=policy.Gate.AUTO,
                                     probabilities=rr.probabilities, latency_ms=rr.latency_ms)
        return None

    async def _route(self, d: DecisionDef, state: Any, r: DecisionResult, data_class: str,
                     validator: Validator | None) -> CascadeResult:
        zp = ZonePolicy.for_decision(d)
        exits = set(d.exits) | ({"other", "none", "unknown", "insufficient_information"} & set(d.labels))
        base = dict(answer=r.decision, confidence=r.confidence, executor=r.provider, decision_version=d.ref,
                    probabilities=r.probabilities, cost_usd=r.cost_usd, action=r.action)
        if r.provider == "default":
            zone, reason = "low", "jev_unavailable"
            route_list = zp.middle_route + zp.low_route          # local-first failure chain (§78)
        elif r.decision in exits:
            zone, reason = "exit", "exit_answer"
            route_list = (zp.exit_route if zp.exit_route is not None else zp.middle_route) + zp.low_route
        elif zp.high is not None and r.confidence >= zp.high and _margin(r.probabilities) >= zp.min_margin:
            zone, reason, route_list = "high", "", []
        elif zp.low is not None and r.confidence < zp.low:
            zone, reason, route_list = "low", "below_low", list(zp.low_route)
        else:
            zone = "middle"
            reason = "flat_distribution" if _margin(r.probabilities) < zp.min_margin else "below_threshold"
            if zp.validate is not None and r.confidence >= zp.validate and validator is not None:
                try:
                    ok = bool(validator(r.decision, state))
                except Exception:
                    ok = False
                if ok and zp.mode != "advisory":
                    return CascadeResult(**base, route="validated", zone=zone)
            route_list = zp.middle_route + zp.low_route
        if zp.mode == "advisory":
            # never automated: record Jev's view, queue for a human, act on nothing
            return await self._fail_safe(d, state, data_class, base, zone, "policy_review", r,
                                         ["human", "safe_default"], [])
        if zone == "high":
            return CascadeResult(**base, route="auto", zone=zone)
        return await self._escalate(d, state, data_class, base, zone, reason, r, route_list, zp)

    async def _escalate(self, d: DecisionDef, state: Any, data_class: str, base: dict, zone: str, reason: str,
                        r: DecisionResult, route_list: list[str], zp: ZonePolicy) -> CascadeResult:
        chain: list[dict] = []
        jev_view = {"answer": r.decision, "confidence": round(r.confidence, 4), "probabilities": r.probabilities,
                    "provider": r.provider, "model": r.provider_version}
        cost = r.cost_usd or 0.0
        unknown_cost = False
        t0 = time.perf_counter()
        terminal = {"human", "safe_default"}
        for step in route_list:
            if step in terminal:
                break
            prov = self.providers.get(step)
            if prov is None or not prov.available(data_class):
                chain.append({"tier": step, "skipped": "unavailable" if prov else "not configured"})
                continue
            pkg = EscalationPackage.build(d, state, reason, data_class, jev_view,
                                          [c for c in chain if "answer" in c])
            metrics.escalations.labels(d.name, prov.name, reason).inc()
            try:
                er = await prov.resolve(pkg)
            except (ProviderUnavailable, ValueError) as e:
                chain.append({"tier": step, "error": str(e)[:160]})
                continue
            if er.cost_usd is None:
                unknown_cost = True
            else:
                cost += er.cost_usd
            chain.append({"tier": step, "provider": er.provider, "answer": er.answer, "confidence": er.confidence,
                          "degraded": er.degraded, "latency_ms": round(er.latency_ms, 1)})
            exits = set(d.exits) | ({"other", "none", "unknown", "insufficient_information"} & set(d.labels))
            if er.answer is not None and er.answer not in exits and (er.confidence or 0) >= zp.accept:
                return CascadeResult(answer=er.answer, confidence=float(er.confidence or 0), executor=er.provider,
                                     route="escalated", decision_version=d.ref, escalated=True,
                                     cost_usd=None if unknown_cost else cost, probabilities=r.probabilities,
                                     zone=zone, action=r.action, degraded=er.degraded, chain=chain,
                                     latency_ms=(time.perf_counter() - t0) * 1000 + r.latency_ms)
        return await self._fail_safe(d, state, data_class, {**base, "cost_usd": None if unknown_cost else cost},
                                     zone, reason, r, route_list, chain)

    async def _fail_safe(self, d: DecisionDef, state: Any, data_class: str, base: dict, zone: str, reason: str,
                         r: DecisionResult, route_list: list[str], chain: list[dict]) -> CascadeResult:
        ticket = None
        if "human" in route_list and self.human is not None and self.human.available():
            pkg = EscalationPackage.build(d, state, reason, data_class,
                                          {"answer": r.decision, "confidence": r.confidence,
                                           "probabilities": r.probabilities})
            hr = await self.human.resolve(pkg)
            ticket = hr.ticket
            chain.append({"tier": "human", "ticket": ticket})
        safe = d.default if d.default is not None else d.labels[0]
        advisory = d.risk == "critical" or not d.reversible
        out = CascadeResult(**{**base, "answer": safe, "confidence": 0.0, "executor": "default"},
                            route="advisory" if advisory else ("human" if ticket else "safe_default"),
                            escalated=bool(chain), zone=zone, pending_ticket=ticket, chain=chain)
        out.action = policy.Gate.ESCALATE
        out.probabilities = r.probabilities
        if advisory:
            out.chain.append({"tier": "jev_view", "answer": r.decision, "confidence": r.confidence})
        return out

    async def _baseline(self, name: str, state: Any, fn: Baseline) -> CascadeResult:
        t0 = time.perf_counter()
        v = fn(state)
        v = await v if asyncio.iscoroutine(v) else v
        label, meta = (v if isinstance(v, tuple) else (v, {}))
        ref = self.registry.versions(name)[-1].ref if self.registry and self.registry.versions(name) else name
        return CascadeResult(answer=str(label), confidence=float(meta.get("confidence", 1.0)),
                             executor=str(meta.get("executor", "baseline")), route="baseline",
                             decision_version=ref, latency_ms=(time.perf_counter() - t0) * 1000,
                             cost_usd=meta.get("cost_usd"), zone="n/a")

    # ── shadow + provenance ──────────────────────────────────────────────────

    def _spawn_shadow(self, name: str, state: Any, data_class: str | None, served: CascadeResult) -> None:
        if self.registry is None or self.store is None:
            return
        cands = [c for c in self.registry.shadow_candidates(name) if c.ref != served.decision_version]
        if not cands or _bucket(state, "shadow:" + name) >= self.shadow_sample:
            return
        from lif.decision.shadow import record_shadow
        task = asyncio.create_task(record_shadow(self.fabric, self.store, cands, state, data_class, served))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _provenance(self, name: str, state: Any, r: CascadeResult, agent: str, workflow: str) -> None:
        d = self.fabric.defs.get(r.decision_version) or self.fabric.defs.get(name)
        rec = provenance.record(decision_ref=r.decision_version, state=state, answer=r.answer,
                                confidence=r.confidence, route=r.route, executor=r.executor, agent=agent,
                                workflow=workflow, probabilities=r.probabilities,
                                thresholds=asdict(ZonePolicy.for_decision(d)) if d else {},
                                pins=d.pins if d else {}, chain=r.chain, stage=d.stage if d else "",
                                model=self.fabric.jev.model if (self.fabric.jev and r.executor == "jev") else "")
        r.provenance_id = rec["id"]
        if self.store is None:
            return
        try:
            self.store.insert("provenance", {"id": rec["id"], "ts": rec["ts"], "decision_ref": rec["decision_ref"],
                                             "agent": agent, "workflow": workflow, "state_hash": rec["state_hash"],
                                             "answer": r.answer, "confidence": r.confidence, "route": r.route,
                                             "executor": r.executor, "record": rec})
        except Exception:                                 # provenance must never fail a decision
            LOG.exception("provenance write failed")

    def _data_class(self, state: Any, declared: str | None, d: DecisionDef) -> str:
        base = policy.DataClass.parse(declared, d.data_class_enum())
        from lif.decision.state_compiler import embedded_class
        marked = embedded_class(state)
        if marked is not None:
            base = max(base, policy.DataClass[marked])
        text = state if isinstance(state, str) else normalize_state(state)
        return policy.classify(text, base.name).data_class.name


def _tier(r: CascadeResult) -> str:
    if r.route in ("safe_default", "advisory"):
        return "safe_default"
    if r.route == "human":
        return "human"
    if r.route == "baseline":
        return "baseline"
    return {"code": "code", "jev": "jev", "rules": "code", "local_llm": "local_fast", "kimi-k3": "kimi",
            "local-reasoning": "local_reasoning", "local-fast": "local_fast", "frontier": "frontier"}.get(
        r.executor, r.executor)
