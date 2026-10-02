"""Bad input and failure paths across the services: they answer 400s instead of 500s or misleading 404s,
and they never treat a blank key as a credential."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lif.common import config, db


# ── keys: a blank `name:` line must not make an empty bearer token valid ──────────────────────────

def test_keys_skip_blank_names_and_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIF_TEST_KEYS", "ops:\n:orphan\n# c:x\n  ci : k1  \nbad-line\n")
    assert config.keys("LIF_TEST_KEYS") == {"k1": "ci"}


def test_controller_rejects_empty_bearer_with_blank_admin_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from lif.controller import app as ctl
    monkeypatch.setenv("LIF_ADMIN_KEYS", "operator:   \nops:good-key")
    monkeypatch.setattr(ctl, "S", SimpleNamespace(admin=config.keys("LIF_ADMIN_KEYS")), raising=False)
    c = TestClient(ctl.app)                  # no lifespan: only the auth middleware and body parsing run
    assert c.post("/v1/aliases/rollback", headers={"Authorization": "Bearer "}).status_code == 401


def test_controller_body_errors_are_400(monkeypatch: pytest.MonkeyPatch) -> None:
    from lif.controller import app as ctl
    monkeypatch.setattr(ctl, "S", SimpleNamespace(admin={"good-key": "ops"}), raising=False)
    c = TestClient(ctl.app)
    h = {"Authorization": "Bearer good-key", "content-type": "application/json"}
    assert c.post("/v1/aliases/rollback", json={}, headers=h).status_code == 400            # no alias
    assert c.post("/v1/aliases/rollback", content=b"{not json", headers=h).status_code == 400
    assert c.post("/v1/settings", content=b"[1, 2]", headers=h).status_code == 400          # not an object


def test_daily_jobs_retry_after_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from lif.controller import app as ctl
    settings: dict[str, Any] = {"automatic_discovery": True}

    async def boom(**_: Any) -> None:
        raise RuntimeError("cluster unreachable")

    reg = SimpleNamespace(prune_availability=lambda: None, setting=lambda k, d=None: settings.get(k, d),
                          set_setting=lambda k, v, actor="": settings.__setitem__(k, v))
    monkeypatch.setattr(ctl, "S", SimpleNamespace(reg=reg, k8s=SimpleNamespace(enabled=True),
                                                  life=SimpleNamespace(gc=boom), disc=SimpleNamespace(run=boom)),
                        raising=False)
    with pytest.raises(RuntimeError):
        asyncio.run(ctl._daily())
    assert "last_gc" not in settings          # the failed run is retried next hour, not in 24 h


# ── decision service ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def decision(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any]:
    from lif.decision import app as dapp
    monkeypatch.setenv("LIF_INTERNAL_KEY", "ik")
    d = db.DB(str(tmp_path / "d.db"), dapp.SCHEMA)
    for i in range(3):
        d.x("INSERT INTO decisions(ts,decision_ref,provider,decision,confidence,action,cached,record) "
            "VALUES(?,?,?,?,?,?,?,?)", (time.time(), "x/v1", "rules", "yes", 1.0, "auto", 0, json.dumps({"i": i})))
    monkeypatch.setattr(dapp, "S", SimpleNamespace(db=d, fabric=SimpleNamespace(jev_enabled_override=None)),
                        raising=False)
    return TestClient(dapp.app), dapp


def test_decision_recent_clamps_negative_limit(decision: tuple[TestClient, Any]) -> None:
    c, _ = decision
    assert len(c.get("/decision/recent?limit=-1").json()["recent"]) == 0          # SQLite: LIMIT -1 = no limit
    assert len(c.get("/decision/recent?limit=2").json()["recent"]) == 2


def test_decision_control_needs_a_real_boolean(decision: tuple[TestClient, Any]) -> None:
    c, dapp = decision
    r = c.post("/decision/control", json={"jev_enabled": "false"}, headers={"X-LIF-Internal": "ik"})
    assert r.status_code == 400 and dapp.S.fabric.jev_enabled_override is None


@pytest.mark.parametrize("body", [{"states": []}, {"decision": "x", "concurrency": "many"},
                                  {"decision": "x", "concurrency": 0}, {"decision": "x", "concurrency": None}, []])
def test_decision_batch_rejects_bad_input(decision: tuple[TestClient, Any], body: Any) -> None:
    c, _ = decision
    assert c.post("/decision/batch", json=body).status_code == 400


@pytest.fixture
def de(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any]:
    from lif.decision import de_api
    monkeypatch.setenv("LIF_INTERNAL_KEY", "ik")
    saved: list[Any] = []
    monkeypatch.setattr(de_api, "RT", SimpleNamespace(save_operations=saved.extend))
    monkeypatch.setattr(de_api, "LATEST_INVENTORY", {"generated_at": 1.0})
    app = FastAPI()
    app.include_router(de_api.router)
    return TestClient(app), de_api


@pytest.mark.parametrize("path,body", [
    ("/de/decide", {}), ("/de/test", {}), ("/de/benchmark", {}), ("/de/calibrate", {}), ("/de/simulate", {}),
    ("/de/outcome", {}), ("/de/decide_many", {"requests": [{"state": {}}]}), ("/de/decide_many", {"requests": "x"}),
    ("/de/transition", {"ref": "a/v1"}), ("/de/rollback", {}), ("/de/decide", [1]),
])
def test_de_missing_fields_are_400(de: tuple[TestClient, Any], path: str, body: Any) -> None:
    c, _ = de
    assert c.post(path, json=body, headers={"X-LIF-Internal": "ik"}).status_code == 400


def test_inventory_upload_is_all_or_nothing(de: tuple[TestClient, Any]) -> None:
    c, de_api = de
    r = c.post("/de/inventory", json={"inventory": [{"a": 1}], "operations": ["not an object"]},
               headers={"X-LIF-Internal": "ik"})
    assert r.status_code == 400 and de_api.LATEST_INVENTORY == {"generated_at": 1.0}


# ── batch, gateway ────────────────────────────────────────────────────────────────────────────

def test_batch_control_rejects_non_object(monkeypatch: pytest.MonkeyPatch) -> None:
    from lif.batch import app as bapp
    monkeypatch.setenv("LIF_INTERNAL_KEY", "ik")
    r = TestClient(bapp.app).post("/v1/batch/control", json=["paused"], headers={"X-LIF-Internal": "ik"})
    assert r.status_code == 400


@pytest.mark.parametrize("temp,cacheable", [(0, True), ("0", True), ("warm", False), ([0], False), (0.7, False)])
def test_gateway_cache_key_tolerates_odd_temperatures(temp: Any, cacheable: bool) -> None:
    from lif.gateway.app import ResponseCache
    k = ResponseCache.key("p", "r", {"model": "m", "temperature": temp, "messages": []})
    assert (k is not None) is cacheable


# ── console ───────────────────────────────────────────────────────────────────────────────────

def test_home_cache_releases_waiters_when_the_fetch_fails() -> None:
    from lif.console.routes import home

    async def run() -> list[Any]:
        gate = asyncio.Event()

        async def fetch() -> Any:
            await gate.wait()
            raise RuntimeError("upstream bug")

        first = asyncio.create_task(home._cached("t-fail", 60, fetch))
        await asyncio.sleep(0)
        second = asyncio.create_task(home._cached("t-fail", 60, fetch))   # joins the in-flight fetch
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 2)

    out = asyncio.run(run())
    assert all(isinstance(e, RuntimeError) for e in out) and "t-fail" not in home._inflight


def test_home_token_counts_ignore_damaged_receipts() -> None:
    from lif.console.routes.home import _count
    assert [_count(v) for v in (12, 3.0, "7", None, [], True, float("nan"))] == [12, 3, 0, 0, 0, 0, 0]


def test_damaged_ca_file_is_no_ca_not_a_500(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from lif.console.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, "CA_FILE", tmp_path / "ca.crt")
    auth_routes.CA_FILE.write_text("-----BEGIN CERTIFICATE-----\nAAA=A\n-----END CERTIFICATE-----\n")
    assert auth_routes.local_ca() is None
    good = base64.encodebytes(b"\x30\x82" + bytes(64)).decode()
    auth_routes.CA_FILE.write_text(f"-----BEGIN CERTIFICATE-----\n{good}-----END CERTIFICATE-----\n")
    assert auth_routes.local_ca() is not None


def test_home_is_its_own_metrics_area() -> None:
    from lif.console.app import route_class
    assert route_class("/api/home") == "api_home"
