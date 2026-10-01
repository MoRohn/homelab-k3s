"""Automatic promotion: benchmark-approved models move through a measured canary into production.

Promotion used to wait for an operator. With `models.automatic_promotion` on, every minute:

  1. start  an APPROVED model whose benchmark comparison said CANARY (it already passed the offline policy:
            quality regression, latency, memory, structured output, crashes — evaluator.compare) gets
            `canary_percent` of its alias. An alias with no model at all gets it directly.
  2. judge  a running canary, once it has run `canary_min_hours` AND served `canary_min_requests`, is held to
            live gates against the incumbent on the same alias (Prometheus, same window):
              errors      failed upstream calls / requests ≤ canary_max_error_rate
              first word  p90 TTFT ≤ incumbent × (1 + max_latency_regression_pct)
              speed       median decode tok/s ≥ incumbent × (1 − max_latency_regression_pct)
              crashes     no container restarts of the canary's server
  3. act    all pass → promote (the old chain stays behind it, and rollback keeps the last versions);
            any fail → the canary ends and the model goes to STANDBY, with the failed gates in the event.
            No verdict after `canary_max_hours` (too little traffic) → the canary ends as inconclusive.

Nothing runs during maintenance or while the primary workload is HIGH/IMMINENT, and only one canary per alias.
Every decision is an activity event with its measurements, so Home and the console show what happened.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from lif.common import config, log
from lif.gpu.state import BlerbzState

LOG = log.get("lif.autopromote")
ACTOR = "auto-promotion"

Prom = Callable[[str], Awaitable[dict[str, float]]]


@dataclass
class Policy:
    min_hours: float = 24.0
    max_hours: float = 72.0
    min_requests: int = 100
    max_error_rate: float = 0.01
    max_latency_regression: float = 0.10

    @classmethod
    def load(cls) -> "Policy":
        p = config.get("models.promotion") or {}
        return cls(min_hours=float(p.get("canary_min_hours", 24)), max_hours=float(p.get("canary_max_hours", 72)),
                   min_requests=int(p.get("canary_min_requests", 100)),
                   max_error_rate=float(p.get("canary_max_error_rate", 0.01)),
                   max_latency_regression=float(p.get("max_latency_regression_pct", 10)) / 100)


@dataclass
class Measurements:
    requests: float
    errors: float
    ttft_p90: float | None
    incumbent_ttft_p90: float | None
    decode_p50: float | None
    incumbent_decode_p50: float | None
    restarts: float


@dataclass
class Verdict:
    action: str                          # wait | promote | end
    reasons: list[str] = field(default_factory=list)


def judge(m: Measurements, age_hours: float, p: Policy) -> Verdict:
    """The canary decision from live measurements. Pure: no I/O, so every gate is unit-tested."""
    if m.restarts > 0:                                   # a crashing server never waits for more evidence
        return Verdict("end", [f"the canary's server restarted {m.restarts:g} time(s)"])
    if age_hours < p.min_hours or m.requests < p.min_requests:
        if age_hours >= p.max_hours:
            return Verdict("end", [f"inconclusive: {m.requests:g} requests in {age_hours:.0f} h "
                                   f"(needs {p.min_requests} within {p.max_hours:g} h)"])
        return Verdict("wait")
    failed: list[str] = []
    rate = m.errors / m.requests if m.requests else 0.0
    if rate > p.max_error_rate:
        failed.append(f"errors {rate:.1%} > {p.max_error_rate:.1%}")
    if m.ttft_p90 is not None and m.incumbent_ttft_p90:
        limit = m.incumbent_ttft_p90 * (1 + p.max_latency_regression)
        if m.ttft_p90 > limit:
            failed.append(f"first word p90 {m.ttft_p90:.2f} s > {limit:.2f} s")
    if m.decode_p50 is not None and m.incumbent_decode_p50:
        floor = m.incumbent_decode_p50 * (1 - p.max_latency_regression)
        if m.decode_p50 < floor:
            failed.append(f"speed {m.decode_p50:.1f} tok/s < {floor:.1f} tok/s")
    return Verdict("end", failed) if failed else Verdict("promote", [
        f"{m.requests:g} requests, errors {rate:.1%}"
        + (f", first word p90 {m.ttft_p90:.2f} s" if m.ttft_p90 is not None else "")
        + (f", {m.decode_p50:.1f} tok/s" if m.decode_p50 is not None else "")])


def _one(d: dict[str, float]) -> float | None:
    return next(iter(d.values()), None) if d else None


async def measure(prom: Prom, canary: str, incumbent: str | None, window_h: float, deployment: str) -> Measurements:
    w = f"{max(1, int(window_h * 3600))}s"
    q = lambda e: prom(e)  # noqa: E731
    req = _one(await q(f'sum(increase(lif_ttft_seconds_count{{profile="{canary}"}}[{w}]))')) or 0.0
    err = _one(await q(f'sum(increase(lif_fallbacks_total{{reason=~"upstream_error on {canary}.*"}}[{w}]))')) or 0.0

    def p_ttft(prof: str) -> str:
        return f'histogram_quantile(0.9, sum by (le) (increase(lif_ttft_seconds_bucket{{profile="{prof}"}}[{w}])))'

    def p_tps(prof: str) -> str:
        return (f'histogram_quantile(0.5, sum by (le) '
                f'(increase(lif_decode_tokens_per_second_bucket{{profile="{prof}"}}[{w}])))')
    restarts = _one(await q(f'sum(increase(kube_pod_container_status_restarts_total'
                            f'{{namespace="ai-serving",pod=~"{deployment}-.*"}}[{w}]))')) or 0.0
    return Measurements(requests=req, errors=err, ttft_p90=_one(await q(p_ttft(canary))),
                        incumbent_ttft_p90=_one(await q(p_ttft(incumbent))) if incumbent else None,
                        decode_p50=_one(await q(p_tps(canary))),
                        incumbent_decode_p50=_one(await q(p_tps(incumbent))) if incumbent else None,
                        restarts=restarts)


class AutoPromoter:
    def __init__(self, life: Any, prom: Prom):
        self.life, self.prom = life, prom

    def enabled(self) -> bool:
        reg = self.life.reg
        if not reg.setting("automatic_promotion", config.get("models.automatic_promotion")):
            return False
        if reg.setting("maintenance", False):
            return False
        return self.life.gpu.current().state not in (BlerbzState.HIGH, BlerbzState.IMMINENT)

    async def tick(self) -> None:
        if not self.enabled():
            return
        await self._judge_canaries()
        await self._start_canaries()

    async def _judge_canaries(self) -> None:
        from lif.controller.lifecycle import deployment_name
        reg, p = self.life.reg, Policy.load()
        for alias, a in reg.aliases().items():
            can = (a or {}).get("canary")
            if not can:
                continue
            mid = can["profile"]
            m = reg.get(mid)
            if m is None:
                continue
            age_h = (time.time() - float(can.get("since") or time.time())) / 3600
            incumbent = (a.get("chain") or [None])[0]
            meas = await measure(self.prom, mid, incumbent, max(age_h, 1 / 60), deployment_name(mid, m["profile"]))
            v = judge(meas, age_h, p)
            if v.action == "wait":
                continue
            detail = {"alias": alias, "requests": meas.requests, "errors": meas.errors, "ttft_p90": meas.ttft_p90,
                      "incumbent_ttft_p90": meas.incumbent_ttft_p90, "decode_p50": meas.decode_p50,
                      "incumbent_decode_p50": meas.incumbent_decode_p50, "restarts": meas.restarts,
                      "hours": round(age_h, 1), "reasons": v.reasons}
            if v.action == "promote":
                await self.life.promote(mid, ACTOR, alias)
                reg.event("auto_promoted", mid, ACTOR, **detail)
            else:
                reg.set_alias(alias, a["chain"], ACTOR, note=f"canary ended: {'; '.join(v.reasons)}", canary=None)
                reg.transition(mid, "STANDBY", f"canary ended: {'; '.join(v.reasons)}", ACTOR)
                reg.event("auto_canary_ended", mid, ACTOR, **detail)
            log.event(LOG, "canary_" + v.action, model=mid, alias=alias, reasons=v.reasons)

    async def _start_canaries(self) -> None:
        from lif.controller.lifecycle import CATEGORY_ALIAS
        reg = self.life.reg
        busy = {a for a, v in reg.aliases().items() if (v or {}).get("canary")}
        for m in reg.list():
            if m["state"] != "APPROVED" or m.get("blocked"):
                continue
            if ((m.get("screening") or {}).get("comparison") or {}).get("recommendation") != "CANARY":
                continue
            alias = CATEGORY_ALIAS.get(m["category"], "local/default")
            if alias in busy:
                continue                                     # one canary per alias at a time
            cur = reg.alias(alias)
            try:
                if cur and cur["chain"]:
                    await self.life.canary(m["id"], ACTOR, alias)
                    reg.event("auto_canary_started", m["id"], ACTOR, alias=alias)
                else:                                        # an empty alias has nothing to compare against
                    await self.life.promote(m["id"], ACTOR, alias)
                    reg.event("auto_promoted", m["id"], ACTOR, alias=alias, reasons=["the alias had no model"])
                busy.add(alias)
            except Exception as e:                           # headroom, maintenance, …: retried next tick
                log.event(LOG, "auto_canary_deferred", model=m["id"], reason=str(e)[:200])
