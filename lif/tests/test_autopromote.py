"""Automatic promotion (lif/controller/autopromote.py): every canary gate, and the driver's actions."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from lif.controller import autopromote as ap
from lif.gpu.state import BlerbzState

P = ap.Policy(min_hours=24, max_hours=72, min_requests=100, max_error_rate=0.01, max_latency_regression=0.10)


def meas(**kw: Any) -> ap.Measurements:
    base = dict(requests=500, errors=0, ttft_p90=1.0, incumbent_ttft_p90=1.0, decode_p50=20.0,
                incumbent_decode_p50=20.0, restarts=0)
    return ap.Measurements(**{**base, **kw})


@pytest.mark.parametrize("m,age,action,why", [
    (meas(), 30, "promote", "500 requests"),
    (meas(), 3, "wait", None),                                        # too young
    (meas(requests=40), 30, "wait", None),                            # too little traffic
    (meas(requests=40), 80, "end", "inconclusive"),                   # …for too long
    (meas(restarts=1), 1, "end", "restarted"),                        # a crash never waits
    (meas(errors=10), 30, "end", "errors 2.0%"),
    (meas(ttft_p90=1.2), 30, "end", "first word p90 1.20 s > 1.10 s"),
    (meas(ttft_p90=1.09), 30, "promote", None),                       # inside the 10 % allowance
    (meas(decode_p50=17.0), 30, "end", "speed 17.0 tok/s < 18.0 tok/s"),
    (meas(incumbent_ttft_p90=None, incumbent_decode_p50=None), 30, "promote", None),  # nothing to compare
])
def test_judge_gates(m: ap.Measurements, age: float, action: str, why: str | None) -> None:
    v = ap.judge(m, age, P)
    assert v.action == action, v
    if why:
        assert any(why in r for r in v.reasons), v.reasons


@dataclass
class FakeReg:
    models: dict[str, dict] = field(default_factory=dict)
    alias_map: dict[str, dict] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=lambda: {"automatic_promotion": True})
    events: list[tuple] = field(default_factory=list)

    def setting(self, k: str, d: Any = None) -> Any:
        return self.settings.get(k, d)

    def aliases(self) -> dict[str, dict]:
        return dict(self.alias_map)

    def alias(self, a: str) -> dict | None:
        return self.alias_map.get(a)

    def list(self) -> list[dict]:
        return list(self.models.values())

    def get(self, mid: str) -> dict | None:
        return self.models.get(mid)

    def set_alias(self, alias: str, chain: list[str], actor: str, note: str = "", canary: dict | None = None) -> dict:
        self.alias_map[alias] = {"chain": chain, "canary": canary}
        return self.alias_map[alias]

    def transition(self, mid: str, to: str, reason: str, actor: str = "", force: bool = False) -> None:
        self.models[mid]["state"] = to

    def event(self, kind: str, subject: str = "", actor: str = "", **d: Any) -> None:
        self.events.append((kind, subject, actor, d))


class FakeLife:
    def __init__(self, reg: FakeReg, state: BlerbzState = BlerbzState.LOW):
        self.reg = reg
        self.gpu = type("G", (), {"current": lambda _s: type("S", (), {"state": state})()})()
        self.calls: list[tuple] = []

    async def canary(self, mid: str, actor: str, alias: str) -> None:
        self.calls.append(("canary", mid, alias))
        cur = self.reg.alias_map[alias]
        self.reg.set_alias(alias, cur["chain"], actor, canary={"profile": mid, "percent": 10, "since": time.time()})

    async def promote(self, mid: str, actor: str, alias: str) -> None:
        self.calls.append(("promote", mid, alias))
        self.reg.set_alias(alias, [mid], actor)


def model(mid: str, state: str = "APPROVED", rec: str = "CANARY", category: str = "general") -> dict:
    return {"id": mid, "state": state, "category": category, "blocked": False, "profile": {"endpoint": ""},
            "screening": {"comparison": {"recommendation": rec}}}


async def _prom(answers: dict[str, float]):
    async def prom(q: str) -> dict[str, float]:
        for k, v in answers.items():
            if k in q:
                return {"value": v}
        return {}
    return prom


async def test_approved_model_starts_a_canary_and_empty_alias_promotes_directly() -> None:
    reg = FakeReg(models={"new": model("new"), "hold": model("hold", rec="HOLD"), "vis": model("vis", category="vision")},
                  alias_map={"local/default": {"chain": ["old"], "canary": None}})
    life = FakeLife(reg)
    await ap.AutoPromoter(life, await _prom({})).tick()
    assert ("canary", "new", "local/default") in life.calls                # beat the incumbent offline → canary
    assert ("promote", "vis", "local/vision") in life.calls                 # nothing to compare against
    assert not any(c[1] == "hold" for c in life.calls)                      # HOLD is not "better"
    assert {e[0] for e in reg.events} >= {"auto_canary_started", "auto_promoted"}


async def test_canary_promotes_or_ends_on_live_measurements() -> None:
    since = time.time() - 30 * 3600
    reg = FakeReg(models={"new": model("new", state="CANARY")},
                  alias_map={"local/default": {"chain": ["old"], "canary": {"profile": "new", "since": since}}})
    life = FakeLife(reg)
    good = {"lif_ttft_seconds_count": 400, "lif_fallbacks_total": 0, "restarts": 0}
    await ap.AutoPromoter(life, await _prom(good)).tick()
    assert ("promote", "new", "local/default") in life.calls

    reg2 = FakeReg(models={"new": model("new", state="CANARY")},
                   alias_map={"local/default": {"chain": ["old"], "canary": {"profile": "new", "since": since}}})
    await ap.AutoPromoter(FakeLife(reg2), await _prom({**good, "lif_fallbacks_total": 40})).tick()
    assert reg2.models["new"]["state"] == "STANDBY" and reg2.alias_map["local/default"]["canary"] is None
    ended = next(e for e in reg2.events if e[0] == "auto_canary_ended")
    assert any("errors" in r for r in ended[3]["reasons"])


@pytest.mark.parametrize("settings,state", [({"automatic_promotion": False}, BlerbzState.LOW),
                                            ({"automatic_promotion": True, "maintenance": True}, BlerbzState.LOW),
                                            ({"automatic_promotion": True}, BlerbzState.IMMINENT)])
async def test_never_acts_when_off_in_maintenance_or_while_production_runs(settings: dict, state: BlerbzState) -> None:
    reg = FakeReg(models={"new": model("new")}, alias_map={"local/default": {"chain": ["old"], "canary": None}},
                  settings=settings)
    life = FakeLife(reg, state)
    await ap.AutoPromoter(life, await _prom({})).tick()
    assert life.calls == [] and reg.events == []
