"""Decision Fabric runtime.

evaluate(name, state)          one decision, full chain: cache → privacy → provider chain → policy gate
evaluate_group(names, state)   several decisions over one state; ONE Jev request carries all questions
evaluate_many(name, states)    map: many states concurrently (bounded), each through evaluate()
map_reduce(...)                partition → parallel decisions → keep only the hard cases for an LLM

Provider chain per decision: [jev] + definition.fallback (default rules → local_llm).
Jev is skipped when disabled, when its breaker is open, or when the state's data class may
not leave the box. Every result carries its provider and the policy gate outcome; the
caller acts only on `action`, never on the raw provider answer.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Iterable

from lif.common import config, log, metrics
from lif.decision.providers import (JevProvider, LocalLLMProvider, ProviderUnavailable, RulesProvider)
from lif.decision.types import DecisionDef, DecisionResult, cache_key
from lif.policy import engine as policy

LOG = log.get("lif.decision.fabric")


class DecisionCache:
    """In-process LRU with TTL. Keys include decision version + provider version, so a
    new definition or model version never reuses stale answers."""

    def __init__(self, max_entries: int = 50000, ttl_sec: float = 86400):
        self.max, self.ttl = max_entries, ttl_sec
        self._d: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def get(self, key: str) -> dict | None:
        hit = self._d.get(key)
        if hit is None or time.time() - hit[0] > self.ttl:
            self._d.pop(key, None)
            metrics.jev_cache.labels("miss").inc()
            return None
        self._d.move_to_end(key)
        metrics.jev_cache.labels("hit").inc()
        return hit[1]

    def put(self, key: str, value: dict) -> None:
        self._d[key] = (time.time(), value)
        self._d.move_to_end(key)
        while len(self._d) > self.max:
            self._d.popitem(last=False)

    def invalidate(self, prefix_ref: str | None = None) -> int:
        if prefix_ref is None:
            n = len(self._d); self._d.clear(); return n
        drop = [k for k, (_, v) in self._d.items() if v.get("decision_ref", "").startswith(prefix_ref)]
        for k in drop:
            self._d.pop(k, None)
        return len(drop)


class DecisionLog:
    """Bounded in-memory ring of recent decisions for the Decision Inspector, plus an
    optional sink (the decision-fabric service persists to SQLite)."""

    def __init__(self, size: int = 2000, sink: Callable[[dict], None] | None = None):
        self.size, self.sink = size, sink
        self.items: list[dict] = []

    def add(self, rec: dict) -> None:
        self.items.append(rec)
        if len(self.items) > self.size:
            del self.items[: len(self.items) - self.size]
        if self.sink:
            try:
                self.sink(rec)
            except Exception:                          # logging must never fail a decision
                LOG.exception("decision sink failed")


class DecisionFabric:
    def __init__(self, definitions: dict[str, DecisionDef], rules: RulesProvider,
                 jev: JevProvider | None = None, local_llm: LocalLLMProvider | None = None,
                 cache: DecisionCache | None = None, decision_log: DecisionLog | None = None,
                 max_parallel: int = 64):
        self.defs, self.rules, self.jev, self.llm = definitions, rules, jev, local_llm
        c = config.get("decision_fabric.cache") or {}
        self.cache = cache if cache is not None else (
            DecisionCache(c.get("max_entries", 50000), c.get("ttl_sec", 86400)) if c.get("enabled", True) else None)
        self.log = decision_log or DecisionLog()
        self.sem = asyncio.Semaphore(max_parallel)
        self.jev_enabled_override: bool | None = None       # operator kill-switch (None = config)

    # ── public API ───────────────────────────────────────────────────────────

    def definition(self, name: str) -> DecisionDef:
        if name not in self.defs:
            raise KeyError(f"unknown decision '{name}'")
        return self.defs[name]

    async def evaluate(self, name: str, state: Any, data_class: str | None = None,
                       thresholds: dict | None = None) -> DecisionResult:
        return (await self.evaluate_group([name], state, data_class, thresholds))[self.definition(name).name]

    async def evaluate_group(self, names: Iterable[str], state: Any, data_class: str | None = None,
                             thresholds: dict | None = None) -> dict[str, DecisionResult]:
        defs = [self.definition(n) for n in names]
        t0 = time.perf_counter()
        results: dict[str, DecisionResult] = {}
        pending: list[DecisionDef] = []

        # 1. cache
        for d in defs:
            hit = self._cache_get(d, state)
            if hit is not None:
                results[d.name] = hit
            else:
                pending.append(d)

        # 2. Jev — one request for every pending decision whose data may leave the box
        jev_ok = [d for d in pending if self._jev_allowed(d, state, data_class)]
        if jev_ok:
            try:
                async with self.sem:
                    got = await self.jev.evaluate_group(jev_ok, state)
                for d in jev_ok:
                    results[d.name] = got[d.name]
                    self._cache_put(d, state, got[d.name])
            except (ProviderUnavailable, ValueError) as exc:
                LOG.warning("jev group failed; falling back", extra={"fields": {"err": str(exc)[:200]}})
                for d in jev_ok:
                    self._note_escalation(d, "jev", "fallback")

        # 3. fallback chain for everything still unanswered (concurrently)
        rest = [d for d in pending if d.name not in results]
        if rest:
            fb = await asyncio.gather(*(self._fallback(d, state) for d in rest))
            for d, r in zip(rest, fb):
                results[d.name] = r
                if r.provider != "default":
                    self._cache_put(d, state, r)

        # 4. policy gate + accounting
        ms = (time.perf_counter() - t0) * 1000
        for d in defs:
            r = results[d.name]
            r.action = policy.gate(r.confidence, {**d.thresholds, **(thresholds or {})})
            if r.provider == "default":
                r.action = policy.Gate.ESCALATE
            if not r.latency_ms:
                r.latency_ms = ms
            metrics.decisions.labels(d.name, r.provider, r.action).inc()
            metrics.tasks.labels(f"decision:{d.name}", _TIER.get(r.provider, "rejected")).inc()
            metrics.decision_latency.labels(d.name, r.provider).observe(r.latency_ms / 1000)
            metrics.decision_confidence.labels(d.name, r.provider).observe(r.confidence)
            if r.action != policy.Gate.AUTO:
                metrics.jev_escalations.labels(d.name, r.action).inc()
            self.log.add({"ts": time.time(), "state_digest": _digest(state), **r.to_dict(),
                          "threshold": {**(config.get("decision_fabric.confidence") or {}), **d.thresholds}})
        return results

    async def evaluate_many(self, name: str, states: list[Any], data_class: str | None = None,
                            concurrency: int = 32) -> list[DecisionResult]:
        sem = asyncio.Semaphore(concurrency)

        async def one(s):
            async with sem:
                return await self.evaluate(name, s, data_class)
        return list(await asyncio.gather(*(one(s) for s in states)))

    async def map_reduce(self, name: str, items: list[Any], *, key: Callable[[Any], Any] = lambda x: x,
                         keep: Callable[[DecisionResult], bool] | None = None,
                         data_class: str | None = None, concurrency: int = 32
                         ) -> tuple[list[tuple[Any, DecisionResult]], list[tuple[Any, DecisionResult]]]:
        """Returns (resolved, hard_cases). A case is hard when its gate is not AUTO/VALIDATE
        — only those should be sent to a generative model or a human."""
        res = await self.evaluate_many(name, [key(i) for i in items], data_class, concurrency)
        pairs = list(zip(items, res))
        if keep is not None:
            pairs = [(i, r) for i, r in pairs if keep(r)]
        hard = [(i, r) for i, r in pairs if r.action in (policy.Gate.LOCAL_LLM, policy.Gate.ESCALATE)]
        easy = [(i, r) for i, r in pairs if r.action in (policy.Gate.AUTO, policy.Gate.VALIDATE)]
        return easy, hard

    def status(self) -> dict:
        return {
            "provider": config.get("decision_fabric.provider"),
            "jev_enabled": self._jev_enabled(),
            "jev_breaker_open": bool(self.jev and self.jev.breaker.open),
            "jev_model": self.jev.model if self.jev else None,
            "definitions": sorted({d.ref for d in self.defs.values()}),
            "cache_entries": len(self.cache._d) if self.cache else 0,
            "recent": len(self.log.items),
        }

    # ── internals ────────────────────────────────────────────────────────────

    def _jev_enabled(self) -> bool:
        if self.jev_enabled_override is not None:
            return self.jev_enabled_override and bool(self.jev and self.jev.enabled)
        return bool(self.jev and self.jev.enabled)

    def _jev_allowed(self, d: DecisionDef, state: Any, declared: str | None) -> bool:
        if not self._jev_enabled() or not self.jev.available():
            return False
        # The caller's declaration wins over the definition's default class; content
        # detectors can only raise it (a declared-PUBLIC state containing a key is RESTRICTED).
        base = policy.DataClass.parse(declared, d.data_class_enum())
        text = state if isinstance(state, str) else json.dumps(state, default=str)
        cls = policy.classify(text, base.name).data_class
        return policy.may_send(cls, "jev")

    async def _fallback(self, d: DecisionDef, state: Any) -> DecisionResult:
        tried: list[str] = []
        for p in d.fallback:
            try:
                if p == "rules" and self.rules.has(d.name):
                    r = await self.rules.evaluate(d, state)
                elif p == "local_llm" and self.llm is not None:
                    async with self.sem:
                        r = await self.llm.evaluate(d, state)
                else:
                    continue
                r.escalated_from = tried
                return r
            except (ProviderUnavailable, ValueError) as exc:
                tried.append(f"{p}: {str(exc)[:120]}")
                self._note_escalation(d, p, "fallback")
        # Every provider failed: return the definition's safe default (if any). Policy
        # forces ESCALATE on defaults, so no automatic action is ever taken from one.
        label = d.default or d.labels[0]
        return DecisionResult(decision=label, confidence=0.0, probabilities={}, provider="default",
                              decision_ref=d.ref, escalated_from=tried,
                              note="no provider could answer; safe default returned")

    def _note_escalation(self, d: DecisionDef, provider: str, to: str) -> None:
        metrics.jev_escalations.labels(d.name, f"{provider}->{to}").inc()

    def _cache_get(self, d: DecisionDef, state: Any) -> DecisionResult | None:
        if not (self.cache and d.cacheable):
            return None
        for pv in self._provider_versions():
            hit = self.cache.get(cache_key(d, state, pv))
            if hit is not None:
                r = DecisionResult(**hit)
                r.cached, r.latency_ms, r.cost_usd = True, 0.0, 0.0
                return r
        return None

    def _cache_put(self, d: DecisionDef, state: Any, r: DecisionResult) -> None:
        if self.cache and d.cacheable and r.provider in ("jev", "rules"):
            pv = self.jev.version if r.provider == "jev" and self.jev else self.rules.version
            self.cache.put(cache_key(d, state, pv), r.to_dict())

    def _provider_versions(self) -> list[str]:
        # Only the provider that would answer first: a rules answer cached during a Jev
        # outage must not be served once Jev is back.
        if self.jev and self._jev_enabled() and self.jev.available():
            return [self.jev.version]
        return [self.rules.version]


def _digest(state: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()[:16]


Node = Callable[[dict[str, Any]], Awaitable[Any] | Any]

# provider → resolution tier for the Heavy-Model-Avoidance KPI (lif_tasks_total)
_TIER = {"jev": "jev", "rules": "deterministic", "local_llm": "fast_local", "default": "rejected"}
