"""Earn area: the earning system (namespace earn) on the console — status, safety controls, honest states.

Two allowlisted upstream calls, each with its own narrowly scoped key held only here:
- GET  {LIF_EARN_URL}/api/status   (LIF_EARN_READ_KEY  → X-Earn-Key)
- POST {LIF_EARN_URL}/api/control  (LIF_EARN_ADMIN_KEY → X-Earn-Admin)
The earn API has no endpoint that enables live trading, raises a limit, moves money or registers anything,
so nothing on this page can either. Live activation stays an operator CLI action bound to the policy file.

Permissions: pause, stop and kill only reduce risk, so anyone with `system.safe` (paired phones included)
runs them directly, without a confirmation (spec §41). Resume and close-only change what the system may do,
so they are admin-only (`system.settings`) and need the typed confirmation from the preview (the scope).

States: LIF_EARN_URL unset → "The earning service isn't configured"; unreachable or refused → available=False
with the reason. Money stays the earn ledger's exact decimal strings. Owner: SYS.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Query

from lif.common import log
from lif.console import auth, settings, upstream
from lif.console.contracts import (ActionPreview, EarnArbCandidate, EarnAvailability, EarnBootStep, EarnControlRequest,
                                   EarnControlState, EarnCosts, EarnExposure, EarnLearning, EarnLiveStatus, EarnMarket,
                                   EarnOverview, EarnPnl, EarnRewardEstimate, EarnSynth, EarnTrip, EarnVenue, Fact,
                                   OkResponse, TechDetail, User)
from lif.console.errors import human
from lif.console.upstream import UpstreamError

LOG = log.get("lif.console.earn")

router = APIRouter(prefix="/api/earn", tags=["earn"])

# Same grammar the earn API enforces; checked here first so a bad scope never reaches it.
SCOPE = re.compile(r"^(global|engine:[a-z_]+|venue:[a-z_]+|market:[a-z_]+:[A-Za-z0-9._-]+)$")
SAFE_ACTIONS = {"pause", "stop", "kill"}
VENUE_LABEL = {"kalshi": "Kalshi", "polymarket_us": "Polymarket US", "bittensor_sn50": "Bittensor SN50"}
BOOT_LABEL = {"load_policy": "Load policy", "acquire_leadership": "Take the execution lease",
              "verify_clock": "Check clocks against the venues", "verify_rules": "Verify rule documents",
              "reconcile": "Reconcile orders, fills, positions and balances",
              "restore_reservations": "Restore capital reservations", "feeds": "Start market data",
              "enable_modes": "Enable the allowed modes"}
DONE = {"pause": "Paused. Resting orders in that scope are being cancelled.",
        "stop": "Stopped. Resting orders in that scope are being cancelled; nothing new will be placed.",
        "kill": "Kill switch on: every resting order is being cancelled and trading is stopped.",
        "close_only": "Close-only: only orders that reduce existing positions are allowed.",
        "resume": "Resumed. Trading in that scope may continue within its limits."}


def _s(v: Any) -> str | None:
    return None if v is None or v == "" else str(v)


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _d(v: Any) -> Decimal | None:
    try:
        return None if v in (None, "") else Decimal(str(v))
    except InvalidOperation:
        return None


def _sum(xs: list[Any]) -> str | None:
    vals = [d for d in (_d(x) for x in xs) if d is not None]
    return None if not vals else format(sum(vals, Decimal(0)), "f")


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _reason_text(r: str) -> str:
    """Raw earn reasons are already sentences; tidy the common machine shapes."""
    m = re.match(r"boot step (\w+) failed: (.*)", r)
    if m:
        return f"Boot check “{BOOT_LABEL.get(m.group(1), m.group(1))}” failed: {m.group(2)}"
    m = re.match(r"^(global|engine:\S+|venue:\S+|market:\S+|instance:\S+): (\S+)$", r)
    if m:
        return f"Safety stop on {m.group(1)}: {m.group(2).replace('_', ' ')}"
    return r[:1].upper() + r[1:] if r else r


def _quote(t: dict[str, Any]) -> str:
    verb = "Buy" if t.get("side") == "buy" else "Sell"
    return f"{verb} {t.get('size')} at {t.get('price')}"


def overview(s: dict[str, Any]) -> EarnOverview:
    """earn /api/status → EarnOverview. Every field is optional upstream; missing data stays missing."""
    now = _f(s.get("ts"))
    safe = bool(_dict(s.get("trading_safe")).get("safe"))
    reasons = [_reason_text(str(r)) for r in _list(_dict(s.get("trading_safe")).get("reasons"))]
    modes = _dict(s.get("mode"))
    book = str(s.get("book") or "")
    venues, markets = [], []
    for name, v in _dict(s.get("venues")).items():
        v = _dict(v)
        age = (now - v["feed_ok_at"]) if now is not None and v.get("feed_ok_at") else None
        unresolved = len(_list(v.get("unresolved_orders")))
        health = "offline" if age is None or age > 120 else "attention" if unresolved or v.get("last_error") else "healthy"
        venues.append(EarnVenue(venue=name, label=VENUE_LABEL.get(name, name), health=health,  # type: ignore[arg-type]
                                markets=int(v.get("markets") or 0), feed_age_s=age, clock_skew_s=_f(v.get("clock_skew_s")),
                                committed_usd=_s(v.get("committed")), paper_cash_usd=_s(v.get("paper_cash")),
                                unresolved_orders=unresolved, last_error=_s(v.get("last_error"))))
        for t in _list(v.get("tracked")):
            t = _dict(t)
            d = _dict(t.get("decision"))
            p = _dict(t.get("program"))
            prog = (f"${p.get('reward_usd')} pool · target {p.get('target')} · discount {p.get('df')}" if p else None)
            markets.append(EarnMarket(venue=name, market=str(t.get("market") or ""), title=str(t.get("title") or ""),
                                      best_bid=_s(t.get("best_bid")), best_ask=_s(t.get("best_ask")),
                                      book_age_s=_f(t.get("book_age_s")), quoting=bool(d.get("quoting")),
                                      eligible=d.get("eligible") if isinstance(d.get("eligible"), bool) else None,
                                      return_per_collateral_hour=_s(d.get("return_per_collateral_hour")),
                                      reward_per_hour_usd=_s(d.get("reward_per_hour")), program=prog,
                                      quotes=[_quote(_dict(q)) for q in _list(d.get("target"))],
                                      reasons=[str(r) for r in _list(d.get("reasons"))] +
                                      [f"Refused: {r}" for r in _list(d.get("rejections"))]))
    pnl_raw = _dict(_dict(s.get("pnl")).get("realized_by_engine"))
    pnl = [EarnPnl(engine=e, realized_trading_usd=_s(_dict(p).get("realized_trading")),
                   rewards_recognized_usd=_s(_dict(p).get("rewards_recognized")), rebates_usd=_s(_dict(p).get("rebates")),
                   fees_usd=_s(_dict(p).get("execution_and_transfer_fees")),
                   net_incremental_usd=_s(_dict(p).get("net_incremental")),
                   net_fully_loaded_usd=_s(_dict(p).get("net_fully_loaded"))) for e, p in pnl_raw.items()]
    rw = _dict(s.get("rewards"))
    est = [EarnRewardEstimate(venue=str(_dict(r).get("venue") or ""), market=str(_dict(r).get("market") or ""),
                              estimate_usd=_s(_dict(r).get("estimate")), low_usd=_s(_dict(r).get("low")),
                              high_usd=_s(_dict(r).get("high")), method=str(_dict(r).get("method") or ""))
           for r in _list(rw.get("estimated"))]
    credited = _dict(rw.get("credited_by_account"))
    costs = _dict(s.get("costs"))
    elec = costs.get("electricity_usd")
    trips = [EarnTrip(scope=str(_dict(t).get("scope") or ""), trigger=str(_dict(t).get("trigger") or ""),
                      effect=str(_dict(t).get("effect") or ""), detail=str(_dict(t).get("detail") or ""),
                      since=_f(_dict(t).get("ts"))) for t in _list(s.get("trips"))]
    controls = [EarnControlState(scope=str(_dict(c).get("scope") or ""), state=str(_dict(c).get("state") or ""),
                                 reason=str(_dict(c).get("reason") or ""), actor=str(_dict(c).get("actor") or ""),
                                 updated_at=_f(_dict(c).get("updated_at"))) for c in _list(s.get("states"))]
    arb = _dict(s.get("arbitrage"))
    cand = _dict(arb.get("candidates"))
    recent = []
    for r in _list(arb.get("recent"))[:10]:
        r = _dict(r)
        rs = r.get("reasons")
        if isinstance(rs, str):
            try:
                import json
                rs = json.loads(rs)
            except ValueError:
                rs = [rs]
        recent.append(EarnArbCandidate(ts=_f(r.get("ts")), kind=str(r.get("kind") or ""), group=str(r.get("group_key") or ""),
                                       classification=str(r.get("classification") or ""), size=_s(r.get("size")),
                                       net_edge_usd=_s(r.get("net_edge")), reason=str((_list(rs) or [""])[0])[:240]))
    syn = s.get("synth")
    if not isinstance(syn, dict):
        synth = EarnSynth(available=False, reason="The forecasting worker hasn't reported yet.")
    elif syn.get("error"):
        synth = EarnSynth(available=False, reason=f"The forecasting worker isn't answering ({syn['error']}).")
    else:
        lb = _dict(syn.get("last_benchmark"))
        synth = EarnSynth(available=True, mode=str(syn.get("mode") or ""), champion_model=_s(syn.get("champion_model")),
                          registration_state=str(syn.get("registration_state") or "unknown"),
                          miner_enabled=bool(syn.get("miner_enabled")),
                          notes=[str(x) for x in _list(syn.get("reasons"))] + [str(x) for x in _list(syn.get("miner_refusals"))],
                          last_benchmark=[Fact(label=str(k), value=str(v)[:120]) for k, v in list(lb.items())[:8]])
    lr = _dict(s.get("learning"))
    learning = EarnLearning(by_status=[Fact(label=str(k), value=str(v)) for k, v in _dict(lr.get("by_status")).items()],
                            champions=[Fact(label=str(_dict(c).get("component") or ""), value=str(_dict(c).get("ref") or ""))
                                       for c in _list(lr.get("champions"))],
                            recent=[f"{_dict(x).get('component')}: {_dict(x).get('status')}"
                                    + (f" — {_dict(x).get('status_reason')}" if _dict(x).get("status_reason") else "")
                                    for x in _list(lr.get("recent"))])
    avail = []
    for comp, a in sorted(_dict(s.get("availability")).items()):
        kind, _, venue = comp.partition(":")
        label = {"control": "This service (control plane)", "model_gateway": "Local AI (research only)"}.get(
            comp, f"{VENUE_LABEL.get(venue, venue)} {'market data' if kind == 'feed' else 'API'}")
        avail.append(EarnAvailability(component=comp, label=label,
                                      kind={"model_gateway": "model"}.get(comp, kind if kind in ("feed", "venue_api") else "control"),
                                      ratio=_f(_dict(a).get("ratio")), observed_minutes=int(_dict(a).get("observed_minutes") or 0)))
    boot = _dict(s.get("boot"))
    steps = [EarnBootStep(step=str(_dict(b).get("step") or ""), label=BOOT_LABEL.get(str(_dict(b).get("step")), str(_dict(b).get("step"))),
                          ok=bool(_dict(b).get("ok")), detail=str(_dict(b).get("detail") or "")) for b in _list(boot.get("steps"))]
    live = [EarnLiveStatus(engine=e, venue=v, allowed=bool(_dict(x).get("allowed")), reasons=[str(r) for r in _list(_dict(x).get("reasons"))])
            for e, per in _dict(s.get("live")).items() for v, x in _dict(per).items()]
    quoting = sum(m.quoting for m in markets)
    globally = next((c for c in controls if c.scope == "global"), None)
    word = "Live" if book == "live" else "Paper" if book == "paper" else "Earning system"
    if globally and globally.state in ("stopped", "paused"):
        lead, health = f"{word} — {globally.state} by {globally.actor or 'an operator'}: {globally.reason}", "paused"
    elif not safe:
        lead, health = f"{word} — not safe to trade: {reasons[0] if reasons else 'checks did not pass'}", "attention"
    elif quoting:
        lead, health = f"{word} trading: quoting {quoting} market{'s' * (quoting != 1)}", "healthy"
    elif not boot.get("warm", True):
        lead, health = f"{word} — warming up after a restart; observing only", "busy"
    else:
        lead, health = f"{word} — observing; no market meets the return threshold", "healthy"
    loops = _dict(_dict(s.get("loops")).get("errors"))
    est_vals = [e.estimate_usd for e in est]
    return EarnOverview(
        configured=True, available=True, lead=lead, health=health, book=book,  # type: ignore[arg-type]
        modes=[Fact(label=k, value=str(v)) for k, v in modes.items()], trading_safe=safe, not_trading=reasons,
        warm=bool(boot.get("warm")), boot=steps, live=live, venues=venues, markets=markets, quoting_markets=quoting,
        pnl=pnl, marked_unrealized_usd=_s(_dict(s.get("pnl")).get("marked_unrealized")), rewards_estimated=est,
        rewards_estimated_usd=_sum(est_vals), rewards_estimated_low_usd=_sum([e.low_usd for e in est]),
        rewards_estimated_high_usd=_sum([e.high_usd for e in est]),
        rewards_credited_usd=_sum(list(credited.values())) or ("0" if book else None),
        rewards_note=str(rw.get("note") or "Estimates are not cash; only credited rewards are income."),
        costs=EarnCosts(cpu_seconds=_f(costs.get("process_cpu_seconds")), max_rss_mib=_f(costs.get("process_max_rss_mib")),
                        electricity=(f"${elec}" if isinstance(elec, (int, float)) else
                                     "Unverified: set the tariff and measured watts in the earn config")),
        committed_usd=_s(_dict(s.get("reservations")).get("committed_total")),
        exposures=[EarnExposure(event_group=str(_dict(g).get("event_group") or "ungrouped"), amount_usd=_s(_dict(g).get("amt")))
                   for g in _list(_dict(s.get("reservations")).get("by_event_group"))],
        trips=trips, controls=controls, arbitrage_total=int(cand.get("total") or 0),
        arbitrage_counts=[Fact(label=str(k), value=str(v)) for k, v in _dict(cand.get("by_classification")).items()],
        arbitrage_recent=recent, synth=synth, learning=learning, availability=avail,
        loop_errors=[f"{k}: {v}" for k, v in loops.items()], updated_at=now,
        tech=[TechDetail(label=k, value=str(v)) for k, v in (("instance", s.get("instance")), ("policy sha256", s.get("policy_sha256")),
                                                              ("book", book), ("status time", s.get("iso")))
              if v not in (None, "")])


def unavailable(reason: str, *, configured: bool = True, tech: list[TechDetail] | None = None) -> EarnOverview:
    lead = "The earning service isn't configured" if not configured else "The earning service isn't answering"
    return EarnOverview(configured=configured, available=False, reason=reason, lead=lead,
                        health="unknown" if not configured else "offline", tech=tech or [])


@router.get("", response_model=EarnOverview)
async def get_overview(user: User = Depends(auth.require("read"))) -> EarnOverview:
    if not settings.earn_url():
        return unavailable("LIF_EARN_URL is not set for the console, so it can't show the earning system. "
                           "The owner adds it when the earn service is deployed.", configured=False)
    try:
        s = await upstream.earn_status()
    except UpstreamError as e:
        if e.unauthorized:
            why = "The console's earn read key is missing or was rejected (LIF_EARN_READ_KEY)."
        else:
            why = upstream.reason(e) + "."
        return unavailable(why, tech=[TechDetail(label="status", value=str(e.status or "no answer")),
                                      TechDetail(label="detail", value=e.detail[:200])])
    if not isinstance(s, dict):
        return unavailable("The earning service sent an unexpected response.")
    try:
        return overview(s)
    except (TypeError, ValueError, AttributeError) as e:          # a shape we don't understand: say so, don't guess
        log.event(LOG, "earn_status_shape", error=type(e).__name__)
        return unavailable("The earning service's status didn't have the expected shape.",
                           tech=[TechDetail(label="error", value=type(e).__name__)])


def _plan(action: str, scope: str) -> ActionPreview:
    if action in SAFE_ACTIONS:
        return ActionPreview(title=f"{action.capitalize()} {scope}", changes=[DONE[action]], confirm="none",
                             rollback="Resume it later from this page (admin, with confirmation).")
    if action == "close_only":
        return ActionPreview(title=f"Allow only closing orders on {scope}",
                             changes=["New exposure is refused; orders that reduce existing positions may still be placed."],
                             rollback="Resume or pause it again at any time.", confirm="typed", confirm_text=scope)
    return ActionPreview(title=f"Resume trading on {scope}",
                         changes=["The engines in this scope may place orders again, within the policy's limits.",
                                  "Open safety stops still block it; the earning service refuses while any is open."],
                         interrupts=[], rollback="Pause or kill it again at any time.", confirm="typed", confirm_text=scope)


def _check(action: str, scope: str) -> None:
    if not SCOPE.match(scope):
        raise human(422, "That scope isn't valid", "Nothing was changed.",
                    "Use global, engine:<name>, venue:<name> or market:<venue>:<id>.", tech={"scope": scope[:80]})
    if not settings.earn_url():
        raise human(503, "The earning service isn't configured", "Nothing was changed.",
                    "The owner sets LIF_EARN_URL for the console when the earn service is deployed.")


@router.get("/control/preview", response_model=ActionPreview)
async def preview(action: str = Query(...), scope: str = Query("global"),
                  user: User = Depends(auth.require("read"))) -> ActionPreview:
    if action not in DONE:
        raise human(404, "That action isn't available", "Nothing was changed.", "Use the buttons on the Earn page.")
    _check(action, scope)
    return _plan(action, scope)


@router.post("/control", response_model=OkResponse)
async def control(req: EarnControlRequest, user: User = Depends(auth.current_user)) -> OkResponse:
    # Risk-reducing actions: anyone allowed safe operations (phones included). Everything else: admin.
    await auth.require("system.safe" if req.action in SAFE_ACTIONS else "system.settings")(user)
    _check(req.action, req.scope)
    plan = _plan(req.action, req.scope)
    if plan.confirm == "typed" and (req.confirm or "").strip() != plan.confirm_text:
        raise human(422, "Confirmation didn't match", "Nothing was changed.", f"Type {plan.confirm_text} exactly to confirm.")
    who = user.name + (f" ({user.device_name})" if user.device_name else "")
    reason = (req.reason or "").strip() or f"{req.action.replace('_', ' ')} from the Labzilla Console"
    body = {"action": req.action, "scope": req.scope, "reason": reason[:300].ljust(3, "."), "actor": f"console:{who}"[:80]}
    try:
        res = await upstream.earn_control(body, actor=user.name)
    except UpstreamError as e:
        raise upstream.to_human(e, doing=f"{req.action.replace('_', ' ')} {req.scope}") from None
    auth.audit(user, f"earn.{req.action}", req.scope, {"reason": reason[:200]})
    extra = ""
    if isinstance(res, dict) and res.get("cancel_requests") is not None:
        extra = f" {res['cancel_requests']} cancel request{'s' * (res['cancel_requests'] != 1)} sent."
    return OkResponse(message=DONE[req.action] + extra)
