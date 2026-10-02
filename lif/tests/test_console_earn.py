"""Earn page BFF: honest states, authority boundaries and key handling, through the real console app.

The earning service is one more host ("earn") in test_console's fake world. Assertions cover: unconfigured
and unreachable states, the status mapping (estimates kept apart from credited rewards, exact decimals),
the role matrix (phones may pause/stop/kill; resume and close-only are admin-only with the typed confirmation),
CSRF on the new mutation, scopes that can't reach the earn API, and keys that never leave the BFF.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
import test_console as tc                   # the integration harness (pytest puts tests/ on sys.path)

READ_KEY = "earn-read-key-0123456789abcdef"
ADMIN_KEY = "earn-admin-key-fedcba9876543210"
NOW = time.time()


def earn_status() -> dict[str, Any]:
    """The shape earn/earn/ops/runtime.py `status()` returns (paper book, not safe to trade)."""
    return {
        "ts": NOW, "iso": "2026-10-02T14:00:00Z", "book": "paper", "instance": "earn-0",
        "mode": {"liquidity": "paper", "arbitrage": "paper", "synth": "offline"}, "policy_sha256": "ab" * 32,
        "live": {"liquidity": {"kalshi": {"allowed": False, "reasons": ["live_limits.daily_loss_usd is unset"]}}},
        "boot": {"steps": [{"step": "verify_rules", "ok": False, "detail": "kalshi.fee_changes: never checked", "ts": NOW}],
                 "booted_at": NOW - 600, "warm": True},
        "trading_safe": {"safe": False, "reasons": ["boot step verify_rules failed: kalshi.fee_changes: never checked",
                                                    "venue:polymarket_us: rule_change:pmus.fees"]},
        "states": [{"scope": "venue:polymarket_us", "state": "paused", "reason": "rule change", "actor": "risk", "updated_at": NOW}],
        "trips": [{"id": 3, "scope": "venue:polymarket_us", "trigger": "rule_change:pmus.fees", "effect": "paused",
                   "detail": "docs changed; review", "ts": NOW - 120, "cleared_at": None}],
        "venues": {"kalshi": {"markets": 1, "feed_ok_at": NOW - 4, "api_ok_at": NOW - 4, "clock_skew_s": 0.14,
                              "last_error": None, "programs": 8000, "committed": "0", "paper_cash": "500",
                              "unresolved_orders": [], "last_reconcile": {"clean": True},
                              "tracked": [{"market": "KXCPIYOY-26DEC-T3.8", "title": "CPI above 3.8%?", "status": "open",
                                           "book_age_s": 4.1, "valid": True, "best_bid": "0.22", "best_ask": "0.49",
                                           "program": {"reward_usd": "100", "target": "1000", "df": "0.5", "end": NOW + 3600},
                                           "decision": {"quoting": False, "eligible": False,
                                                        "return_per_collateral_hour": "-0.0051",
                                                        "reward_per_hour": "0", "target": [],
                                                        "reasons": ["wide spread: thin market"]}}]}},
        "pnl": {"realized_by_engine": {"all": {"realized_trading": "0.16", "rewards_recognized": "0", "rebates": "0.31",
                                               "execution_and_transfer_fees": "0.18", "incremental_operating_costs": "0",
                                               "allocated_infrastructure": "0", "net_incremental": "0.29",
                                               "net_fully_loaded": "0.29"}},
                "marked_unrealized": "-0.04", "positions": []},
        "rewards": {"estimated": [{"venue": "kalshi", "market": "KXCPIYOY-26DEC-T3.8", "estimate": "1.20", "low": "0",
                                   "high": "2.10", "method": "sampled mean"}],
                    "credited_by_account": {}, "note": "estimates are not cash; paper never receives credits"},
        "costs": {"electricity_usd": None, "process_cpu_seconds": 12.5, "process_max_rss_mib": 96.0},
        "reservations": {"committed_total": "0", "by_event_group": []},
        "arbitrage": {"candidates": {"by_classification": {"not_arbitrage": 4, "unverifiable": 2}, "total": 6, "last_ts": NOW},
                      "recent": [{"id": 1, "ts": NOW, "kind": "same_venue_exclusive_event:long_all", "group_key": "kalshi:EV",
                                  "classification": "not_arbitrage", "size": "1", "cost": "1.02", "net_edge": "-0.02",
                                  "reasons": json.dumps(["state no listed outcome wins pays 0"])}], "baskets": {}},
        "markouts": {}, "learning": {"by_status": {"proposed": 1}, "champions": [{"component": "liquidity.quoter", "ref": "v1"}],
                                     "recent": []},
        "synth": {"mode": "offline", "champion_model": "reference_rw", "registration_state": "not_registered",
                  "miner_enabled": False, "reasons": ["registration budget is zero until the operator approves a spend"],
                  "last_benchmark": None},
        "availability": {"control": {"ratio": 0.999, "ok_minutes": 999, "observed_minutes": 1000},
                         "feed:kalshi": {"ratio": 0.95, "ok_minutes": 950, "observed_minutes": 1000}},
        "loops": {"last_ok": {}, "errors": {}}, "sources": [], "audit": [],
    }


class EarnWorld(tc.World):
    def __init__(self) -> None:
        super().__init__()
        self.earn_reply: Any = None

    def route(self, host: str, method: str, path: str, params: Any, body: Any) -> Any:
        if host == "earn":
            if self.earn_reply is not None:
                return self.earn_reply
            if path == "/api/status" and method == "GET":
                return earn_status()
            if path == "/api/control" and method == "POST":
                if body["action"] == "resume" and body["scope"] == "venue:polymarket_us":
                    return httpx.Response(409, json={"detail": "open safety trips on venue:polymarket_us must be cleared first"})
                return {"ok": True, "state": body["action"], "cancel_requests": 2 if body["action"] == "kill" else None}
            return httpx.Response(404, json={"detail": "not found"})
        return super().route(host, method, path, params, body)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """test_console's environment (same env vars, UI dir, fake world), with the earn host and its keys added."""
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>Labzilla</title>")
    env = {"LIF_CONSOLE_DB": str(tmp_path / "console.db"), "LIF_CONSOLE_UI_DIR": str(ui),
           "LIF_CONSOLE_SETUP_CODE": tc.CODE, "LIF_CONSOLE_ADMIN_KEY": "test-admin-key",
           "LIF_CONSOLE_GATEWAY_KEY": "test-gateway-key", "LIF_SECRETS_DIR": str(tmp_path / "secrets"),
           "LIF_KNOWLEDGE_ROOT": str(tc.LIF / "knowledge"),
           "LIF_CONTROLLER_URL": "http://controller.invalid:8080", "LIF_GATEWAY_URL": "http://gateway.invalid:8080",
           "LIF_BATCH_URL": "http://batch.invalid:8080",
           "LIF_PROMETHEUS_URL": "http://monitoring-kube-prometheus-prometheus.invalid:9090",
           "LIF_EARN_URL": "http://earn.invalid:8080", "LIF_EARN_READ_KEY": READ_KEY, "LIF_EARN_ADMIN_KEY": ADMIN_KEY}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("LIF_KNOWLEDGE_URL", "LIF_CONSOLE_INSECURE_COOKIES", "LIF_CONSOLE_LAN_URL"):
        monkeypatch.delenv(k, raising=False)
    w = EarnWorld()
    tc.upstream.set_transport(httpx.MockTransport(w))
    tc.ai._transport = httpx.MockTransport(w.gateway_chat)
    tc.poller.reset()
    tc.models_routes._disc.update(requested_at=0.0, known_max=0, categories=[])
    asyncio.run(tc.poller.refresh())
    yield w
    tc.upstream.set_transport(None)
    tc.ai._transport = None
    tc.poller.reset()
    tc.db.close()


@pytest.fixture
def app(world):
    from lif.console.app import create_app
    return create_app()


def earn_calls(w: EarnWorld) -> list[tuple[str, str, Any, dict[str, str]]]:
    return [(m, p, b, h) for host, m, p, b, h in w.calls if host == "earn"]


def test_unconfigured_and_unreachable_states_are_honest(app, world, monkeypatch):
    owner = tc.admin(app)
    monkeypatch.delenv("LIF_EARN_URL")
    o = owner.get("/api/earn").json()
    assert o["configured"] is False and o["available"] is False and o["lead"] == "The earning service isn't configured"
    assert earn_calls(world) == []
    monkeypatch.setenv("LIF_EARN_URL", "http://earn.invalid:8080")
    world.down.add("earn")
    o = owner.get("/api/earn").json()
    assert o["configured"] and not o["available"] and o["lead"] == "The earning service isn't answering"
    assert "isn't answering" in o["reason"] and o["markets"] == [] and o["pnl"] == []
    world.down.clear()
    world.earn_reply = httpx.Response(401, json={"detail": "read key required"})
    o = owner.get("/api/earn").json()
    assert not o["available"] and "read key" in o["reason"]


def test_status_maps_to_the_page_without_inventing_anything(app, world):
    owner = tc.admin(app)
    o = owner.get("/api/earn").json()
    assert o["available"] and o["book"] == "paper" and o["trading_safe"] is False
    assert o["lead"].startswith("Paper — not safe to trade: Boot check “Verify rule documents” failed")
    assert "Safety stop on venue:polymarket_us: rule change:pmus.fees" in o["not_trading"]
    m = o["markets"][0]
    assert m["quoting"] is False and m["reasons"] == ["wide spread: thin market"] and m["program"].startswith("$100 pool")
    assert o["rewards_estimated_usd"] == "1.20" and o["rewards_estimated_high_usd"] == "2.10"
    assert o["rewards_credited_usd"] == "0" and "not cash" in o["rewards_note"]
    pnl = o["pnl"][0]
    assert pnl["net_incremental_usd"] == "0.29" and pnl["rebates_usd"] == "0.31"     # exact strings, never floats
    assert o["marked_unrealized_usd"] == "-0.04" and o["arbitrage_total"] == 6
    assert o["arbitrage_recent"][0]["reason"] == "state no listed outcome wins pays 0"
    assert o["synth"]["registration_state"] == "not_registered" and o["synth"]["available"]
    kinds = {a["component"]: a["kind"] for a in o["availability"]}
    assert kinds == {"control": "control", "feed:kalshi": "feed"}
    assert o["costs"]["electricity"].startswith("Unverified")
    assert o["trips"][0]["scope"] == "venue:polymarket_us" and o["live"][0]["allowed"] is False
    (method, path, _, headers), = earn_calls(world)
    assert (method, path) == ("GET", "/api/status") and headers.get("x-earn-key") == READ_KEY
    assert "x-earn-admin" not in headers


def test_keys_never_reach_the_browser(app, world):
    owner = tc.admin(app)
    texts = [owner.get("/api/earn").text, tc.post(owner, "/api/earn/control", {"action": "pause"}).text,
             owner.get("/api/earn/control/preview", params={"action": "resume"}).text]
    world.earn_reply = httpx.Response(500, json={"detail": "boom"})
    texts.append(tc.post(owner, "/api/earn/control", {"action": "stop"}).text)
    assert not any(k in t for t in texts for k in (READ_KEY, ADMIN_KEY))


def test_phone_may_reduce_risk_but_not_resume(app, world):
    owner = tc.admin(app)
    phone, _ = tc.pair(owner, app)
    for action in ("pause", "stop", "kill"):
        r = tc.post(phone, "/api/earn/control", {"action": action, "scope": "venue:kalshi"})
        assert r.status_code == 200, (action, r.text)
    assert "2 cancel requests sent" in tc.post(phone, "/api/earn/control", {"action": "kill"}).json()["message"]
    before = len(earn_calls(world))
    for action in ("resume", "close_only"):
        r = tc.post(phone, "/api/earn/control", {"action": action, "scope": "global", "confirm": "global"})
        assert r.status_code == 403 and tc.human_error(r)["title"] == "Not allowed from this session"
    assert len(earn_calls(world)) == before                     # refused before any upstream call
    sent = [b for m, p, b, h in earn_calls(world) if m == "POST"]
    assert all(b["actor"].startswith("console:owner (Kitchen phone)") for b in sent)
    assert all(h.get("x-earn-admin") == ADMIN_KEY for m, p, b, h in earn_calls(world) if m == "POST")


def test_admin_resume_needs_the_previewed_typed_confirmation(app, world):
    owner = tc.admin(app)
    pv = owner.get("/api/earn/control/preview", params={"action": "resume", "scope": "engine:liquidity"}).json()
    assert pv["confirm"] == "typed" and pv["confirm_text"] == "engine:liquidity"
    assert owner.get("/api/earn/control/preview", params={"action": "kill"}).json()["confirm"] == "none"
    r = tc.post(owner, "/api/earn/control", {"action": "resume", "scope": "engine:liquidity", "confirm": "yes"})
    assert r.status_code == 422 and tc.human_error(r)["title"] == "Confirmation didn't match"
    assert not [c for c in earn_calls(world) if c[0] == "POST"]
    r = tc.post(owner, "/api/earn/control", {"action": "resume", "scope": "engine:liquidity", "confirm": "engine:liquidity"})
    assert r.status_code == 200 and r.json()["message"].startswith("Resumed")
    # the earn service refuses while safety trips are open; its explanation reaches the person
    r = tc.post(owner, "/api/earn/control", {"action": "resume", "scope": "venue:polymarket_us",
                                             "confirm": "venue:polymarket_us"})
    err = tc.human_error(r)
    assert r.status_code == 409 and "open safety trips" in err["impact"]
    with tc.db.tx() as c:
        acts = [r[0] for r in c.execute("SELECT action FROM audit WHERE action LIKE 'earn.%'").fetchall()]
    assert acts == ["earn.resume"]                               # the refused resume left no audit row


def test_csrf_and_scope_guard(app, world):
    owner = tc.admin(app)
    r = owner.post("/api/earn/control", json={"action": "pause"}, headers={"Origin": tc.ORIGIN})
    assert r.status_code == 403                                  # no CSRF header
    for bad in ("venue:kalshi/../x", "global; drop", "market:kalshi:a b", "admin", "venue:Kalshi"):
        r = tc.post(owner, "/api/earn/control", {"action": "pause", "scope": bad})
        assert r.status_code == 422 and tc.human_error(r)["title"] == "That scope isn't valid", bad
    r = tc.post(owner, "/api/earn/control", {"action": "withdraw", "scope": "global"})
    assert r.status_code == 422                                  # not an action the contract knows
    assert [c for c in earn_calls(world) if c[0] == "POST"] == []
