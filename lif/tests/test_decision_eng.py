"""Decision Engineering: registry/lifecycle, lint, calibration, cascade + escalation, shadow.

Readiness items (spec §99) are tagged in test names: test_ready_<letter>_….
No test calls a live provider; Jev and Kimi are httpx.MockTransport handlers.
"""
from __future__ import annotations

import json

import httpx
import pytest

from lif.decision import calibration, pricing, shadow
from lif.decision.cascade import Cascade, ZonePolicy
from lif.decision.escalation import (EscalationPackage, HumanReviewProvider, KimiK3Provider, LocalReasoningProvider,
                                     RateLimiter)
from lif.decision.fabric import DecisionFabric
from lif.decision.lint import lint, summarize
from lif.decision.providers import JevProvider, ProviderUnavailable, RulesProvider
from lif.decision.registry import Registry, RegistryError, promotion_blockers
from lif.decision.store import Store
from lif.decision.types import DecisionDef, index_definitions, load_definitions
from lif.policy import engine as policy

REL = {
    "name": "source-relevance", "version": "v1", "primitive": "noul", "stage": "production", "risk": "low",
    "data_class": "PUBLIC", "owner": "research",
    "instructions": "Judge whether `source.text` provides evidence that helps answer `task.question`.",
    "criteria": "`source.text` states a fact, figure or claim that supports or contradicts an answer to "
                "`task.question`.",
    "state_schema": {"task.question": "str", "source.text": "str"}, "default": "no",
    "policy": {"mode": "three_zone", "high": 0.9, "low": 0.3, "middle_route": ["local_reasoning", "kimi"],
               "low_route": ["safe_default"]},
}
TOOL = {
    "name": "tool-selection", "version": "v1", "primitive": "choice", "stage": "production", "risk": "low",
    "data_class": "PUBLIC",
    "instructions": "Choose the tool that should run next to make progress on `goal` given `last_result`.",
    "criteria": {"search": "look up information on the web", "code": "run or edit code in the workspace",
                 "none": "no tool is needed; the next step is to answer"},
    "state_schema": {"goal": "str", "last_result": "str"}, "default": "none",
    "policy": {"mode": "three_zone", "high": 0.85, "low": 0.2, "min_margin": 0.15,
               "middle_route": ["kimi"], "low_route": ["human", "safe_default"]},
}


def defs(*extra: dict) -> dict:
    return index_definitions([DecisionDef.from_raw(r) for r in (REL, TOOL, *extra)])


def jev(handler) -> JevProvider:
    p = JevProvider("jv_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    p.enabled = True
    return p


def jev_answers(table: dict):
    """table: question key → answer dict. Records calls."""
    calls = []

    def h(req):
        body = json.loads(req.content)
        calls.append(body)
        return httpx.Response(200, json={"model": "jev-1.13.0", "usage": {"input_tokens": 50},
                                         "answers": {k: table[k] for k in body["questions"]}})
    return h, calls


def kimi(handler, key="mk_test") -> KimiK3Provider:
    p = KimiK3Provider(api_key=key, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return p


def kimi_says(answer, conf=0.9, calls=None):
    def h(req):
        if calls is not None:
            calls.append(json.loads(req.content))
        return httpx.Response(200, json={"model": "kimi-k3", "choices": [{"message": {
            "content": json.dumps({"answer": answer, "confidence": conf, "evidence": "quoted"})}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50}})
    return h


@pytest.fixture()
def allow_external(monkeypatch):
    """Simulate the owner listing PUBLIC under privacy.external_llm_allowed."""
    from lif.common import config
    real = config.get

    def get(key, default=None):
        if key == "privacy.external_llm_allowed":
            return ["PUBLIC"]
        return real(key, default)
    monkeypatch.setattr(config, "get", get)


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "de.db"))


# ── registry / versioning ─────────────────────────────────────────────────────

def test_ready_L_new_version_does_not_move_bare_name():
    v2 = {**REL, "version": "v2", "stage": "shadow", "instructions": REL["instructions"] + " Ignore ads."}
    d = defs(v2)
    assert d["source-relevance"].version == "v1"               # shadow v2 never serves silently
    assert d["source-relevance/v2"].stage == "shadow"


def test_legacy_definitions_still_resolve():
    d = load_definitions()
    assert d["request-route"].ref == "request-route/v1" and d["request-route"].stage == "production"


def test_promotion_gates_are_deterministic():
    d = DecisionDef.from_raw({**REL, "stage": "calibrated"})
    ev = {"lint_errors": 0, "tests_total": 40, "tests_accuracy": 0.95, "calibration_adequate": True,
          "thresholds_from_calibration": True}
    assert promotion_blockers(d, "low_risk_automation", ev, "owner") == []
    assert any("human actor" in b for b in promotion_blockers(d, "low_risk_automation", ev, "autotuner"))
    assert any("calibration" in b for b in promotion_blockers(d, "low_risk_automation",
                                                              {**ev, "thresholds_from_calibration": False}, "owner"))
    crit = DecisionDef.from_raw({**REL, "stage": "calibrated", "risk": "critical"})
    assert any("never be automated" in b for b in promotion_blockers(crit, "production", ev, "owner"))
    early = DecisionDef.from_raw({**REL, "stage": "designed"})
    assert any("skip" in b for b in promotion_blockers(early, "production", ev, "owner"))


def test_ready_M_promote_then_rollback_restores_previous(store):
    v2 = {**REL, "version": "v2", "stage": "calibrated"}
    reg = Registry(defs(v2), store)
    ev = {"lint_errors": 0, "tests_total": 40, "tests_accuracy": 0.95, "calibration_adequate": True,
          "thresholds_from_calibration": True}
    reg.transition("source-relevance/v2", "low_risk_automation", actor="owner", reason="calibrated", evidence=ev,
                   thresholds={}, policy={"high": 0.93})
    assert reg.resolve("source-relevance").version == "v2"
    assert reg.resolve("source-relevance").policy["high"] == 0.93
    assert reg.active_release("source-relevance").rollout_pct == 10.0
    reg.rollback("source-relevance", actor="owner", reason="incident")
    assert reg.resolve("source-relevance").version == "v1"      # YAML production version serves again
    assert reg.stage_of("source-relevance/v2") == "shadow"        # still observed
    with pytest.raises(RegistryError):
        reg.transition("source-relevance/v2", "production", actor="autotuner", reason="x", evidence=ev)


def test_threshold_rollback_restores_previous_threshold(store):
    reg = Registry(defs({**REL, "version": "v2", "stage": "calibrated"}), store)
    ev = {"lint_errors": 0, "tests_total": 40, "tests_accuracy": 0.95, "calibration_adequate": True,
          "thresholds_from_calibration": True}
    reg.transition("source-relevance/v2", "production", actor="owner", reason="a", evidence=ev, policy={"high": 0.9})
    reg.transition("source-relevance/v2", "production", actor="owner", reason="bad threshold", evidence=ev,
                   policy={"high": 0.5})
    assert reg.resolve("source-relevance").policy["high"] == 0.5
    reg.rollback("source-relevance", actor="owner", reason="bad threshold deployed")
    assert reg.resolve("source-relevance").policy["high"] == 0.9


# ── lint ──────────────────────────────────────────────────────────────────────

def _codes(raw):
    return {f.code for f in lint(DecisionDef.from_raw(raw))}


def test_ready_D_lint_catches_known_antipatterns():
    assert not summarize(lint(DecisionDef.from_raw(REL)))["error"]
    bad = {**REL, "instructions": "Is this source trustworthy and relevant to the question?"}
    assert {"compound-judgment", "undefined-pronoun"} <= _codes(bad)
    assert "vague-adjective" in _codes({**REL, "instructions": REL["instructions"] + " The customer seems serious."})
    assert "asks-calculation" in _codes({**REL, "instructions": "Count the number of errors in `log.text` and "
                                                                "decide if it is over the limit."})
    assert "inverted-noul" in _codes({**REL, "instructions": "Judge whether `cmd.text` is unsafe to run on the host.",
                                      "criteria": ""})
    assert "no-exit" in _codes({**TOOL, "criteria": {"search": "web search", "code": "run code in the repo"}})
    sc = {**REL, "primitive": "score", "default": None,
          "criteria": {"low": "No relationship to `task.question` at all.", "mid": "More than level low.",
                       "high": "Directly answers a major part of `task.question`."}}
    assert "relative-level" in _codes(sc)
    assert "missing-state-path" in _codes({**REL, "instructions": REL["instructions"] + " Also read `source.url`."})
    assert "generative-ask" in _codes({**REL, "instructions": "Summarize `source.text` and explain why it matters."})
    assert "multiple-questions" in _codes({**REL, "instructions": "Is `a.b` relevant? Is `a.b` recent?"})


# ── calibration ──────────────────────────────────────────────────────────────

def _synthetic(n=4000):
    """Deterministic samples whose accuracy rises steeply with confidence."""
    out = []
    for i in range(n):
        c = 0.5 + 0.5 * (i / n)
        correct = (i * 7919 % 1000) / 1000 < 1 - (1 - c) * 0.4      # pseudo-random but reproducible
        out.append(calibration.Sample(confidence=c, correct=correct, slice="edge" if c < 0.8 else "common"))
    return out


def test_ready_F_calibration_metrics_and_threshold_by_consequence():
    s = _synthetic()
    rel = calibration.reliability(s)
    assert sum(r["n"] for r in rel) == len(s)
    rep = calibration.report("x/v1", s, risk="low")
    hi_low = rep["recommended"]["high"]
    hi_med = calibration.choose_threshold(s, calibration.max_error_for("medium"))["threshold"]
    assert hi_low is not None and hi_med is not None and hi_med > hi_low     # costlier error → higher T
    at = calibration.at_threshold(s, hi_low)
    assert at["error_ci95_upper"] <= 0.05 and 0 < at["coverage"] < 1
    assert calibration.choose_threshold(s, None)["threshold"] is None         # critical: never
    assert rep["ece"] is not None and rep["adequacy"]["n"] == len(s)


def test_adequacy_needs_edge_cases_and_failures():
    only_easy = [calibration.Sample(confidence=0.99, correct=True, slice="common") for _ in range(500)]
    a = calibration.adequacy(only_easy)
    assert not a.adequate and any("edge" in r for r in a.reasons) and any("failures" in r for r in a.reasons)


def test_simulator_rows():
    rows = calibration.simulate(_synthetic(), 10000, {"jev": 0.0001, "kimi": 0.02}, {"jev": 300, "kimi": 4000},
                                thresholds=[0.8, 0.9, 0.95, 0.98])
    assert [r["threshold"] for r in rows] == [0.8, 0.9, 0.95, 0.98]
    assert rows[0]["coverage"] > rows[-1]["coverage"] and rows[0]["cost_per_day_usd"] < rows[-1]["cost_per_day_usd"]


# ── pricing ──────────────────────────────────────────────────────────────────

def test_ready_K_pricing_change_needs_no_code_change(tmp_path, monkeypatch):
    base = pricing.call_cost("kimi-k3", 1_000_000, 0)
    f = tmp_path / "providers.yaml"
    f.write_text("providers:\n  kimi-k3:\n    pricing: {input_per_mtok: 9.0, output_per_mtok: 1.0}\n")
    monkeypatch.setenv("LIF_PROVIDERS", str(f))
    pricing.reload()
    try:
        assert pricing.call_cost("kimi-k3", 1_000_000, 0) == pytest.approx(9.0) != base
        assert pricing.call_cost("human-review") is None                  # unknown price is not $0
    finally:
        monkeypatch.delenv("LIF_PROVIDERS")
        pricing.reload()


# ── cascade ──────────────────────────────────────────────────────────────────

def cascade(jev_handler=None, kimi_handler=None, store=None, local=None, extra_defs=(), registry=False):
    d = defs(*extra_defs)
    fab = DecisionFabric(d, RulesProvider(), jev=jev(jev_handler) if jev_handler else None)
    providers = {}
    if kimi_handler is not None:
        providers["kimi"] = kimi(kimi_handler)
    if local is not None:
        providers["local_reasoning"] = local
    if store is not None:
        providers["human"] = HumanReviewProvider(store)
    reg = Registry(d, store) if registry else None
    return Cascade(fab, reg, providers, store)


STATE = {"task": {"question": "When was the Spark released?"}, "source": {"text": "DGX Spark shipped in Oct 2025."}}


async def test_ready_G_high_confidence_routes_automatically(allow_external):
    h, calls = jev_answers({"source_relevance": {"noul": 0.97}})
    kcalls = []
    c = cascade(h, kimi_says("no", calls=kcalls))
    r = await c.decide("source-relevance", STATE, data_class="PUBLIC")
    assert r.route == "auto" and r.answer == "yes" and r.executor == "jev" and r.actionable
    assert not kcalls and len(calls) == 1


async def test_ready_H_middle_zone_escalates_to_kimi_with_package(allow_external):
    h, _ = jev_answers({"source_relevance": {"noul": 0.7}})
    kcalls = []
    c = cascade(h, kimi_says("yes", 0.9, kcalls))
    r = await c.decide("source-relevance", STATE, data_class="PUBLIC")
    assert r.route == "escalated" and r.executor == "kimi-k3" and r.escalated and r.answer == "yes"
    sent = json.loads(kcalls[0]["messages"][1]["content"])
    assert sent["first_pass"]["probabilities"] == {"yes": 0.7, "no": pytest.approx(0.3)}
    assert sent["escalation_reason"] == "below_threshold" and sent["decision"] == "source-relevance/v1"
    assert "temperature" not in kcalls[0]                       # K3 fixes sampling params
    assert r.cost_usd == pytest.approx(pricing.call_cost("kimi-k3", 1000, 50) + 50 * 0.42 / 1e6, rel=1e-6)


async def test_low_zone_goes_to_safe_default_not_kimi(allow_external):
    h, _ = jev_answers({"source_relevance": {"noul": 0.75}})      # confidence 0.75 → middle
    h2, _ = jev_answers({"source_relevance": {"noul": 0.5}})      # confidence 0.5 → middle (low=0.3 never hit)
    kcalls = []
    c = cascade(h2, kimi_says("yes", 0.9, kcalls), extra_defs=())
    r = await c.decide("source-relevance", STATE, data_class="PUBLIC")
    assert r.zone == "middle"
    low = {**REL, "name": "rel-strict", "policy": {**REL["policy"], "low": 0.8}}
    h3, _ = jev_answers({"rel_strict": {"noul": 0.6}})
    kcalls.clear()
    c = cascade(h3, kimi_says("yes", 0.9, kcalls), extra_defs=(low,))
    r = await c.decide("rel-strict", STATE, data_class="PUBLIC")
    assert r.zone == "low" and r.route == "safe_default" and r.answer == "no" and not kcalls and not r.actionable


async def test_flat_distribution_escalates_not_argmax(allow_external, store):
    h, _ = jev_answers({"tool_selection": {"choice": "search", "confidence": 0.39,
                                           "probabilities": {"search": 0.39, "code": 0.34, "none": 0.27}}})
    kcalls = []
    c = cascade(h, kimi_says("code", 0.88, kcalls), store=store)
    r = await c.decide("tool-selection", {"goal": "fix test", "last_result": "1 failed"}, data_class="PUBLIC")
    assert r.answer == "code" and r.executor == "kimi-k3"           # not Jev's top value 'search'
    sent = json.loads(kcalls[0]["messages"][1]["content"])
    assert sent["first_pass"]["probabilities"]["search"] == 0.39


async def test_ready_I_kimi_outage_falls_back_safely(allow_external, store):
    h, _ = jev_answers({"tool_selection": {"choice": "search", "confidence": 0.5,
                                           "probabilities": {"search": 0.5, "code": 0.3, "none": 0.2}}})
    c = cascade(h, lambda req: httpx.Response(503), store=store)
    for _ in range(4):
        r = await c.decide("tool-selection", {"goal": "g", "last_result": "r"}, data_class="PUBLIC")
        assert r.route == "human" and r.answer == "none" and r.pending_ticket and not r.actionable
    assert c.providers["kimi"].breaker.open                          # stops hammering after 3 failures
    hp = HumanReviewProvider(store)
    assert hp.pending_count() == 4
    row = hp.answer(r.pending_ticket, "code", "owner")                # answer → outcome on the provenance record
    assert shadow.record_outcome(store, outcome="code", provenance_id=row["provenance_id"])["provenance"] == 1


async def test_kimi_429_honours_reset_header(allow_external):
    h, _ = jev_answers({"source_relevance": {"noul": 0.7}})
    c = cascade(h, lambda req: httpx.Response(429, headers={"x-ratelimit-reset": "30"}))
    r = await c.decide("source-relevance", STATE, data_class="PUBLIC")
    assert r.route == "safe_default"
    assert c.providers["kimi"].limiter.blocked_until > 0
    with pytest.raises(ProviderUnavailable):
        await c.providers["kimi"].limiter.acquire()                  # refuses instead of hammering


async def test_kimi_invalid_answer_is_rejected(allow_external):
    h, _ = jev_answers({"source_relevance": {"noul": 0.7}})
    c = cascade(h, kimi_says("maybe"))
    r = await c.decide("source-relevance", STATE, data_class="PUBLIC")
    assert r.route == "safe_default" and "not one of" in r.chain[-1]["error"]


async def test_confidential_state_never_reaches_kimi():
    """No allow_external fixture: lif.yaml external_llm_allowed is []."""
    h, _ = jev_answers({"source_relevance": {"noul": 0.7}})
    kcalls = []
    c = cascade(h, kimi_says("yes", 0.9, kcalls))
    r = await c.decide("source-relevance", STATE, data_class="PUBLIC")
    assert not kcalls and r.route == "safe_default"
    assert {"tier": "kimi", "skipped": "unavailable"} in r.chain


async def test_kimi_context_too_large(allow_external):
    p = kimi(kimi_says("yes"))
    d = DecisionDef.from_raw(REL)
    pkg = EscalationPackage.build(d, {"blob": "x" * 4_000_000}, "below_threshold", "PUBLIC")
    with pytest.raises(ProviderUnavailable, match="context too large"):
        await p.resolve(pkg)


async def test_ready_J_jev_outage_uses_local_first_chain(store):
    local_calls = []

    def gw(req):
        local_calls.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"answer": "yes", "confidence": 0.8, "evidence": "Oct 2025"})}}], "lif": {"degraded": True,
                                                                                          "served_by": "qwen3-4b"}})
    local = LocalReasoningProvider("http://gw", client=httpx.AsyncClient(transport=httpx.MockTransport(gw)))
    c = cascade(lambda req: httpx.Response(502), store=store, local=local)
    r = await c.decide("source-relevance", STATE, data_class="PUBLIC")
    assert r.executor == "local-reasoning" and r.route == "escalated" and r.degraded
    assert local_calls[0]["model"] == "local/reasoning"
    # local down too → safe default, never a silent guess
    local.breaker.opened_at = 1e18
    r = await c.decide("source-relevance", {**STATE, "n": 2}, data_class="PUBLIC")
    assert r.route == "safe_default" and r.answer == "no"


async def test_ready_R_irreversible_decisions_stay_human(store):
    destroy = {**REL, "name": "delete-volume-ok", "risk": "critical", "reversible": False,
               "instructions": "Judge whether `request.text` asks to delete the volume named in `volume.name`.",
               "state_schema": {"request.text": "str", "volume.name": "str"}}
    h, _ = jev_answers({"delete_volume_ok": {"noul": 0.999}})
    c = cascade(h, store=store, extra_defs=(destroy,))
    r = await c.decide("delete-volume-ok", {"request": {"text": "rm it"}, "volume": {"name": "v"}},
                       data_class="PUBLIC")
    assert r.route == "advisory" and not r.actionable and r.pending_ticket
    assert ZonePolicy.for_decision(DecisionDef.from_raw(destroy)).mode == "advisory"


async def test_ready_E_shadow_candidates_never_change_the_answer(store):
    v2 = {**REL, "version": "v2", "stage": "shadow"}
    h, calls = jev_answers({"source_relevance": {"noul": 0.97}})
    c = cascade(h, store=store, extra_defs=(v2,), registry=True)
    r = await c.decide("source-relevance", STATE, data_class="PUBLIC")
    await c.drain()
    assert r.decision_version == "source-relevance/v1"
    rows = store.q("SELECT * FROM shadow")
    assert len(rows) == 1 and rows[0]["decision_ref"] == "source-relevance/v2"
    # outcome feedback reaches both provenance and shadow rows
    upd = shadow.record_outcome(store, outcome="yes", provenance_id=r.provenance_id, source="human")
    assert upd == {"provenance": 1, "shadow": 1}
    s = shadow.samples(store, "source-relevance/v2")
    assert len(s) == 1 and s[0].correct is True


async def test_rollout_slice_serves_baseline_outside_slice(store):
    v2 = {**REL, "version": "v2", "stage": "calibrated"}
    h, _ = jev_answers({"source_relevance": {"noul": 0.97}})
    c = cascade(h, store=store, extra_defs=(v2,), registry=True)
    ev = {"lint_errors": 0, "tests_total": 40, "tests_accuracy": 0.95, "calibration_adequate": True,
          "thresholds_from_calibration": True}
    c.registry.transition("source-relevance/v2", "low_risk_automation", actor="owner", reason="t", evidence=ev,
                          rollout_pct=50)
    routes = set()
    for i in range(40):
        r = await c.decide("source-relevance", {**STATE, "i": i}, data_class="PUBLIC", baseline=lambda s: "no")
        routes.add(r.route)
    await c.drain()
    assert routes == {"auto", "baseline"}


async def test_pinned_jev_model_mismatch_skips_jev():
    pinned = {**REL, "name": "pinned", "pins": {"jev_model": "jev-0.9.0"}}
    h, calls = jev_answers({"pinned": {"noul": 0.99}})
    c = cascade(h, extra_defs=(pinned,))
    r = await c.decide("pinned", STATE, data_class="PUBLIC")
    assert not calls and r.route == "safe_default"


async def test_rate_limiter_backpressure():
    rl = RateLimiter("t", rps=1, burst=1, concurrency=1, max_wait=0.01)
    await rl.acquire()
    with pytest.raises(ProviderUnavailable):
        await rl.acquire()


async def test_human_review_answer_round_trip(store):
    hp = HumanReviewProvider(store)
    pkg = EscalationPackage.build(DecisionDef.from_raw(REL), {"api": "sk_live_ABCDEFGHIJKLMNOPQRSTU"}, "x", "PUBLIC")
    er = await hp.resolve(pkg)
    row = store.q("SELECT package FROM human_queue WHERE id=?", (er.ticket,))[0]
    assert "sk_live" not in row["package"]                           # secrets are redacted from the queue
    with pytest.raises(ValueError):
        hp.answer(er.ticket, "maybe", "owner")
    assert hp.answer(er.ticket, "yes", "owner")["status"] == "answered"


def test_policy_gate_contract_unchanged():
    assert policy.gate(0.99) == policy.Gate.AUTO and policy.gate(0.5) == policy.Gate.ESCALATE


async def test_private_knowledge_in_state_never_reaches_jev():
    """A knowledge item from a private repo (data_class CONFIDENTIAL) raises a declared-PUBLIC state."""
    h, calls = jev_answers({"source_relevance": {"noul": 0.97}})
    c = cascade(h)
    st = {**STATE, "knowledge": [{"id": "ops-incident-1", "summary": "x", "data_class": "CONFIDENTIAL"}]}
    r = await c.decide("source-relevance", st, data_class="PUBLIC")
    assert not calls and r.executor != "jev"
    r = await c.decide("source-relevance", {**STATE, "knowledge": [{"id": "k", "data_class": "PUBLIC"}]},
                       data_class="PUBLIC")
    assert len(calls) == 1 and r.executor == "jev"
