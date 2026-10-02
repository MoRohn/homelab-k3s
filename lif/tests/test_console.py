"""Console integration: the whole app (Guard middleware, every router, real sessions) against fake upstreams.

The per-owner suites (test_console_core / _sys / _ai) test their modules in isolation, often with the user
dependency overridden. This file checks the seams brief §8 cares about, through the real request path:
setup-code gate and cookie flags, CSRF and sign-in on every mutating route, the role permission matrix with
a *paired* device session (not a stub user), the pairing lifecycle (single use, expiry, revoke), Ask
streaming and its failure path, threads following the person across devices, knowledge never leaking the
private operations repo, the intent table over HTTP, translation tables, and TS contract drift.

No network: controller/gateway/Prometheus/batch are one httpx.MockTransport (the SYS fake world plus a batch
service), the Ask stream has its own MockTransport, and every upstream URL points at `.invalid` hosts as a
backstop. Knowledge reads the public workspace in lif/knowledge; assertions about it report counts only,
so a regression can't print private text into the test log.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import textwrap
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import test_console_sys as sysfake          # the SYS fake world (pytest puts tests/ on sys.path)
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from lif.console import auth, db, events, gen_ts, poller, settings, upstream
from lif.console import humanize as hz
from lif.console.contracts import User
from lif.console.routes import ai
from lif.console.routes import knowledge as kn_routes
from lif.console.routes import models as models_routes

LIF = Path(__file__).resolve().parents[1]
ORIGIN = "https://testserver"
CODE = "ABCD-EFGH-JKMN-PQRS"
PASS = "correct horse battery"
P4B, CAND = sysfake.P4B, sysfake.CAND
BATCH_ID = "bj_0a1b2c3d"


# ── fake upstreams ───────────────────────────────────────────────────────────────────────────

def _stream(*parts: str, meta: dict[str, Any] | None = None) -> bytes:
    out = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': p}, 'finish_reason': None}]})}\n\n"
                  for p in parts)
    out += f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
    out += f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 9, 'completion_tokens': 2}, 'lif': meta or {}})}\n\n"
    return (out + "data: [DONE]\n\n").encode()


class World(sysfake.Fake):
    """SYS's controller/gateway/Prometheus world plus the batch service and the gateway's chat endpoint."""

    def __init__(self) -> None:
        super().__init__()
        self.job = {"id": BATCH_ID, "state": "running", "reason": "", "total": 4, "progress": 0.5,
                    "counts": {"succeeded": 2, "failed": 0}, "created": time.time() - 60, "description": "Summaries"}
        self.chat: list[httpx.Request] = []
        self.chat_reply: httpx.Response | Exception | None = None

    def route(self, host: str, method: str, path: str, params: Any, body: Any) -> Any:
        if host == "batch":
            if path == "/v1/batch":
                return {"data": [self.job]}
            if path == "/v1/batch/stats":
                return {"paused": bool(self.s["settings"].get("batch_paused"))}
            if path == f"/v1/batch/{BATCH_ID}":
                return self.job
            if path in (f"/v1/batch/{BATCH_ID}/pause", f"/v1/batch/{BATCH_ID}/resume"):
                self.job["state"] = "paused" if path.endswith("pause") else "running"
                return self.job
            return httpx.Response(404, json={"error": "not found"})
        return super().route(host, method, path, params, body)

    def gateway_chat(self, req: httpx.Request) -> httpx.Response:
        self.chat.append(req)
        if isinstance(self.chat_reply, Exception):
            raise self.chat_reply
        if self.chat_reply is not None:
            return self.chat_reply
        meta = {"requested": "local/default", "alias": "local/default", "served_by": P4B, "model": f"Org/{P4B}@abc123",
                "fallback": False, "degraded": False}
        return httpx.Response(200, headers={"X-LIF-Served-By": P4B, "X-LIF-Requested": "local/default",
                                            "content-type": "text/event-stream"}, content=_stream("Hello", " there", meta=meta))

    def mutations(self) -> list[tuple[str, str, str]]:
        return [(h, m, p) for h, m, p, *_ in self.calls if m in ("POST", "DELETE", "PUT", "PATCH")]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>Labzilla</title>")
    (ui / "manifest.webmanifest").write_text('{"name": "Labzilla"}')
    env = {"LIF_CONSOLE_DB": str(tmp_path / "console.db"), "LIF_CONSOLE_UI_DIR": str(ui),
           "LIF_CONSOLE_SETUP_CODE": CODE, "LIF_CONSOLE_ADMIN_KEY": "test-admin-key",
           "LIF_CONSOLE_GATEWAY_KEY": "test-gateway-key", "LIF_SECRETS_DIR": str(tmp_path / "secrets"),
           "LIF_KNOWLEDGE_ROOT": str(LIF / "knowledge"),
           # backstop: nothing here can reach the cluster even if a fake transport were missing
           "LIF_CONTROLLER_URL": "http://controller.invalid:8080", "LIF_GATEWAY_URL": "http://gateway.invalid:8080",
           "LIF_BATCH_URL": "http://batch.invalid:8080",
           "LIF_PROMETHEUS_URL": "http://monitoring-kube-prometheus-prometheus.invalid:9090"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("LIF_KNOWLEDGE_URL", "LIF_CONSOLE_INSECURE_COOKIES", "LIF_CONSOLE_LAN_URL"):
        monkeypatch.delenv(k, raising=False)
    w = World()
    upstream.set_transport(httpx.MockTransport(w))
    ai._transport = httpx.MockTransport(w.gateway_chat)
    poller.reset()
    models_routes._disc.update(requested_at=0.0, known_max=0, categories=[])
    asyncio.run(poller.refresh())           # what the running loop would have cached by now
    yield w
    upstream.set_transport(None)
    ai._transport = None
    poller.reset()
    db.close()


@pytest.fixture
def app(world: World):
    from lif.console.app import create_app
    return create_app()                     # fresh limiters per test (every client is peer "testclient")


# ── browser helpers ──────────────────────────────────────────────────────────────────────────

def browser(app: Any) -> TestClient:
    c = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
    c.get("/healthz")                       # any GET issues lz_csrf, as the SPA's index.html does
    return c


def send(c: TestClient, method: str, path: str, body: Any = None, **headers: str) -> httpx.Response:
    h = {"X-Labzilla-CSRF": c.cookies.get("lz_csrf") or "", "Origin": ORIGIN, **headers}
    return c.request(method, path, json=body if body is not None else ({} if method != "DELETE" else None), headers=h)


def post(c: TestClient, path: str, body: Any = None, **headers: str) -> httpx.Response:
    return send(c, "POST", path, body, **headers)


def admin(app: Any) -> TestClient:
    c = browser(app)
    r = post(c, "/api/setup", {"setup_code": CODE, "name": "owner", "passphrase": PASS, "default_mode": "auto"})
    assert r.status_code == 200, r.text
    return c


def pair(owner: TestClient, app: Any, name: str = "Kitchen phone") -> tuple[TestClient, dict[str, Any]]:
    """Start → claim → approve → status, exactly as Connect and /pair do it. Returns the phone and its pairing."""
    start = post(owner, "/api/pair/start").json()
    phone = browser(app)
    claim = post(phone, "/api/pair/claim", {"token": start["url"].split("#", 1)[1], "device_name": name})
    assert claim.status_code == 200, claim.text
    assert post(owner, f"/api/pair/{start['id']}/approve").json()["status"] == "approved"
    st = phone.get("/api/pair/status")
    assert st.json()["status"] == "approved" and phone.cookies.get("lz_session")
    return phone, start


def cookie(r: httpx.Response, name: str) -> str:
    return next(v for v in r.headers.get_list("set-cookie") if v.startswith(f"{name}=")).lower()


def human_error(r: httpx.Response) -> dict[str, Any]:
    body = r.json()
    assert set(body) == {"error"} and body["error"]["title"], body
    assert not re.search(r"\bHTTP\b|\b[45]\d\d\b", body["error"]["title"]), body["error"]["title"]
    return body["error"]


def sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for frame in text.strip().split("\n\n"):
        ev = next((ln[7:] for ln in frame.splitlines() if ln.startswith("event: ")), None)
        data = "\n".join(ln[6:] for ln in frame.splitlines() if ln.startswith("data: "))
        if ev and data:
            out.append((ev, json.loads(data)))
    return out


# ── setup gate, cookies ──────────────────────────────────────────────────────────────────────

def test_setup_code_gate_and_cookie_flags(app, monkeypatch):
    c = browser(app)
    monkeypatch.delenv("LIF_CONSOLE_SETUP_CODE")
    body = {"setup_code": CODE, "name": "owner", "passphrase": PASS, "default_mode": "fast"}
    assert human_error(post(c, "/api/setup", body))["title"] == "Setup code isn't configured"
    monkeypatch.setenv("LIF_CONSOLE_SETUP_CODE", CODE)
    assert post(c, "/api/setup", {**body, "setup_code": "ABCD-EFGH-JKMN-PQRT"}).status_code == 403
    assert c.get("/api/auth/me").status_code == 401

    # read off a terminal: case, spaces and dashes don't matter (the generated code is upper-case)
    r = post(c, "/api/setup", {**body, "setup_code": " abcd efgh jkmn-pqrs "})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    line = cookie(r, "lz_session")
    assert all(f in line for f in ("httponly", "secure", "samesite=strict", "path=/"))
    csrf = cookie(c.get("/api/setup", headers={"cookie": ""}), "lz_csrf")
    assert "httponly" not in csrf and "samesite=strict" in csrf and "secure" in csrf
    assert db.kv_get("default_mode") == "fast"

    assert human_error(post(c, "/api/setup", body))["title"] == "Labzilla is already set up"
    anon = browser(app).get("/api/setup").json()
    assert not anon["needs_setup"] and anon["detected"] == [] and anon["lan_url"] is None

    monkeypatch.setenv("LIF_CONSOLE_INSECURE_COOKIES", "1")     # dev over plain http only
    r = post(c, "/api/auth/login", {"name": "owner", "passphrase": PASS})
    assert r.status_code == 200 and "secure" not in cookie(r, "lz_session")


def _mutating_routes(app: Any) -> list[tuple[str, str]]:
    out = []
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api"):
            for m in sorted(r.methods - {"GET", "HEAD", "OPTIONS"}):
                out.append((m, re.sub(r"\{[^}]+\}", "x1", r.path)))
    return out


# Mutations a signed-out browser must be able to make: signing in, first-run setup, claiming a pairing,
# and signing out (harmless without a session).
OPEN_MUTATIONS = {"/api/auth/login", "/api/auth/logout", "/api/setup", "/api/pair/claim"}


def test_every_mutation_needs_csrf_and_a_session(app, world):
    routes = _mutating_routes(app)
    assert len(routes) >= 20 and ("POST", "/api/models/deployments/x1/x1") in routes
    c = browser(app)
    for method, path in routes:
        no_header = c.request(method, path, json={}, headers={"Origin": ORIGIN})
        assert no_header.status_code == 403, (method, path)
        evil = send(c, method, path, Origin="https://evil.example")
        assert evil.status_code == 403 and human_error(evil)["title"] == "Request blocked for safety", (method, path)
        signed_out = send(c, method, path)
        if path in OPEN_MUTATIONS:
            assert signed_out.status_code < 500, (method, path)
        else:
            assert signed_out.status_code == 401, (method, path, signed_out.status_code)
            assert human_error(signed_out)["actions"][0]["action"] == "login"
    assert world.mutations() == []      # nothing above reached an upstream


def test_permission_literals_are_real_permissions():
    src = "\n".join(p.read_text() for p in (LIF / "lif" / "console").rglob("*.py"))
    used = set(re.findall(r'require\("([a-z.]+)"\)', src)) | set(re.findall(r'perm="([a-z.]+)"', src))
    assert used and used <= set(auth.ALL_PERMS), used - set(auth.ALL_PERMS)
    device = auth.ROLE_PERMS["device"]
    assert {"models.operate", "models.release", "system.settings", "devices.manage"}.isdisjoint(device)
    assert {"read", "ask", "jobs.control", "approvals.answer", "models.discover", "system.safe"} <= device


# ── role permission matrix with a real paired device ─────────────────────────────────────────

def test_paired_device_is_refused_dangerous_operations(app, world):
    owner = admin(app)
    phone, _ = pair(owner, app)
    me = phone.get("/api/auth/me").json()
    assert me["role"] == "device" and me["device_name"] == "Kitchen phone"
    other = post(owner, "/api/pair/start").json()                     # a second phone waiting for approval
    assert post(browser(app), "/api/pair/claim", {"token": other["url"].split("#")[1], "device_name": "x"}).is_success
    device_id = owner.get("/api/devices").json()[0]["id"]
    before = len(world.mutations())

    refused = [
        ("POST", f"/api/models/deployments/{CAND}/promote", {"alias": "local/code"}),
        ("POST", f"/api/models/deployments/{CAND}/canary", {"alias": "local/code", "percent": 10}),
        ("POST", f"/api/models/deployments/{CAND}/delete", {"confirm": CAND}),
        ("POST", f"/api/models/deployments/{P4B}/unload", {}),
        ("POST", f"/api/models/deployments/{CAND}/load", {}),
        ("POST", f"/api/models/deployments/{CAND}/benchmark", {}),
        ("POST", f"/api/models/deployments/{CAND}/block", {}),
        ("POST", "/api/models/roles/balanced/rollback", {"confirm": "local/default"}),
        ("POST", "/api/system/settings", {"key": "maintenance", "value": True}),
        ("POST", "/api/system/settings", {"key": "automatic_promotion", "value": True}),
        ("POST", "/api/system/settings", {"key": "reserve_gpu_mib", "value": 0}),
        ("POST", "/api/pair/start", {}),
        ("POST", f"/api/pair/{other['id']}/approve", {}),
        ("POST", f"/api/pair/{other['id']}/reject", {}),
        ("POST", f"/api/approvals/pairing:{other['id']}", {"answer": "approve"}),   # has approvals.answer, not devices.manage
        ("DELETE", f"/api/devices/{device_id}", None),
        ("POST", "/api/knowledge/notes", {"title": "x", "body": "y"}),
        ("POST", "/api/agents/run", {"agent": "evaluator", "params": {"candidate": CAND}}),
    ]
    for method, path, body in refused:
        r = send(phone, method, path, body)
        assert r.status_code == 403, (method, path, r.status_code)
        assert human_error(r)["title"] == "Not allowed from this session"
    assert phone.get("/api/devices").status_code == 403
    assert phone.get(f"/api/pair/{other['id']}").status_code == 403
    assert world.mutations()[before:] == []                           # refused before any upstream call
    assert owner.get(f"/api/pair/{other['id']}").json()["status"] == "claimed"   # still undecided

    # safe operations (§99) work from the phone
    assert post(phone, "/api/jobs/batch/pause-all", {"paused": True}).status_code == 200
    assert ("controller", "POST", "/v1/settings") in world.mutations()
    assert post(phone, f"/api/jobs/batch:{BATCH_ID}/pause").json()["status"] == "paused"
    assert post(phone, "/api/system/settings", {"key": "batch_paused", "value": False}).status_code == 200
    assert post(phone, "/api/models/discovery", {}).status_code == 200      # any body, as the Models page sends
    assert post(phone, "/api/system/services/gateway/retry").json()["key"] == "gateway"
    assert post(phone, "/api/approvals/review:1", {"answer": "yes"}).status_code == 409    # not pending: honest


def test_admin_operations_use_the_previewed_confirmation(app, world):
    owner = admin(app)
    # Role rollback exactly as RoleDetail does it: preview through the serving deployment, then POST the role.
    pv = owner.get(f"/api/models/deployments/{P4B}/preview", params={"action": "rollback", "alias": "local/default"})
    assert pv.status_code == 200 and pv.json()["confirm"] == "typed"
    assert pv.json() == owner.get("/api/models/roles/balanced/rollback/preview").json()
    assert post(owner, "/api/models/roles/balanced/rollback", {"confirm": "local/fast"}).status_code == 422
    r = post(owner, "/api/models/roles/balanced/rollback", {"confirm": pv.json()["confirm_text"]})
    assert r.status_code == 200 and ("controller", "POST", "/v1/aliases/rollback") in world.mutations()

    # Promote: simple confirmation; the preview says what changes and whether rollback exists (§98).
    pp = owner.get(f"/api/models/deployments/{CAND}/preview", params={"action": "promote", "alias": "local/code"}).json()
    assert pp["confirm"] == "simple" and pp["changes"] and pp["rollback"]
    assert post(owner, f"/api/models/deployments/{CAND}/promote", {"alias": "local/code"}).status_code == 200
    assert ("controller", "POST", f"/v1/models/{CAND}/promote") in world.mutations()

    # Delete: typed, and the text the preview asks for is exactly what the server checks.
    world.s["models"][CAND]["state"] = "APPROVED"                     # the fake doesn't move state on promote
    pd = owner.get(f"/api/models/deployments/{CAND}/preview", params={"action": "delete"}).json()
    assert pd["confirm"] == "typed" and pd["confirm_text"] == CAND and "can't be undone" in pd["rollback"]
    assert post(owner, f"/api/models/deployments/{CAND}/delete", {"confirm": "yes"}).status_code == 422
    assert ("controller", "DELETE", f"/v1/models/{CAND}") not in world.mutations()
    assert post(owner, f"/api/models/deployments/{CAND}/delete", {"confirm": CAND}).status_code == 200
    assert ("controller", "DELETE", f"/v1/models/{CAND}") in world.mutations()


def test_model_ids_cannot_steer_upstream_paths(app, world):
    owner = admin(app)
    for bad in ("x%3Fstate=PRODUCTION", "%2E%2E", "a%2F..%2Fsettings", "a%23b", "a%20b"):
        r = owner.get(f"/api/models/deployments/{bad}")
        assert r.status_code == 404, bad
    assert not any("?" in p or ".." in p or p.endswith("/settings") for _, _, p, *_ in world.calls if "models" in p)

    async def direct() -> None:
        with pytest.raises(upstream.UpstreamError) as e:
            await upstream.post("controller", "/v1/models/../settings", {"maintenance": True})
        assert e.value.status == 404
    n = len(world.calls)
    asyncio.run(direct())
    assert len(world.calls) == n                                       # refused before the transport


# ── pairing lifecycle ────────────────────────────────────────────────────────────────────────

def test_pairing_lifecycle_single_use_and_revoke(app, world):
    owner = admin(app)
    start = post(owner, "/api/pair/start").json()
    assert start["status"] == "waiting" and start["url"].startswith(settings.public_url() + "/pair#")
    token = start["url"].split("#", 1)[1]
    assert len(token) >= 40 and token == start["token"]

    phone = browser(app)
    r = post(phone, "/api/pair/claim", {"token": token, "device_name": "Pixel\x00 in the hall"})
    claim = r.json()
    assert claim["status"] == "claimed" and re.fullmatch(r"\d{6}", claim["code"])
    assert claim["device_name"] == "Pixel in the hall" and not claim.get("token")
    pc = cookie(r, "lz_pair")
    assert "httponly" in pc and "path=/api/pair" in pc
    seen = owner.get(f"/api/pair/{start['id']}").json()
    assert seen["code"] == claim["code"] and not seen.get("token") and not seen.get("url")
    cards = owner.get("/api/approvals").json()
    assert any(a["id"] == f"pairing:{start['id']}" and a["blocking"] for a in cards)

    # single use: the same link from another browser is refused, and the phone can't sign in early
    thief = browser(app)
    assert post(thief, "/api/pair/claim", {"token": token, "device_name": "x"}).status_code in (404, 410)
    assert phone.get("/api/pair/status").json()["status"] == "claimed"
    assert phone.get("/api/auth/me").status_code == 401

    assert post(owner, f"/api/approvals/pairing:{start['id']}", {"answer": "approve"}).status_code == 200
    first = phone.get("/api/pair/status")
    assert first.json()["status"] == "approved" and "httponly" in cookie(first, "lz_session")
    again = phone.get("/api/pair/status")                              # the session is delivered once
    assert not any(v.startswith("lz_session=") for v in again.headers.get_list("set-cookie"))
    me = phone.get("/api/auth/me").json()
    assert me["role"] == "device" and me["id"] == owner.get("/api/auth/me").json()["id"]
    assert post(owner, f"/api/pair/{start['id']}/approve").status_code == 409

    devices = owner.get("/api/devices").json()
    assert [d["name"] for d in devices] == ["Pixel in the hall"]
    assert send(owner, "DELETE", f"/api/devices/{devices[0]['id']}").status_code == 200
    assert phone.get("/api/auth/me").status_code == 401                # revocation ends the session at once
    assert owner.get("/api/devices").json() == []

    # a device that signs out is unpaired (it has no passphrase to come back with)
    phone2, _ = pair(owner, app, "Tablet")
    assert post(phone2, "/api/auth/logout").status_code == 200
    assert owner.get("/api/devices").json() == [] and phone2.get("/api/auth/me").status_code == 401


def test_pairing_expiry(app, world, monkeypatch):
    owner = admin(app)
    monkeypatch.setattr(settings, "pairing_ttl_sec", lambda: -1)      # every window has already closed
    start = post(owner, "/api/pair/start").json()
    r = post(browser(app), "/api/pair/claim", {"token": start["token"], "device_name": "late"})
    assert r.status_code == 410 and human_error(r)["title"] == "This pairing link expired"
    assert owner.get(f"/api/pair/{start['id']}").json()["status"] == "expired"

    # approved, but the phone never came back for its session: expired, and its device revoked
    monkeypatch.setattr(settings, "pairing_ttl_sec", lambda: 120)
    start = post(owner, "/api/pair/start").json()
    phone = browser(app)
    assert post(phone, "/api/pair/claim", {"token": start["token"], "device_name": "slow"}).status_code == 200
    monkeypatch.setattr(settings, "pairing_ttl_sec", lambda: -1)
    assert post(owner, f"/api/pair/{start['id']}/approve").json()["status"] == "approved"
    assert phone.get("/api/pair/status").json()["status"] == "expired"
    assert not phone.cookies.get("lz_session") and owner.get("/api/devices").json() == []


# ── Ask: streaming, failures, threads across devices ─────────────────────────────────────────

def test_ask_streams_and_the_thread_follows_the_person(app, world):
    owner = admin(app)
    t = post(owner, "/api/ai/threads", {"mode": "balanced"}).json()
    r = post(owner, f"/api/ai/threads/{t['id']}/messages", {"content": "What is unified memory?", "mode": "balanced"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = sse(r.text)
    kinds = [e for e, _ in evs]
    assert kinds[0] == "route" and kinds[-1] == "done" and "receipt" in kinds and "error" not in kinds
    mid = evs[-1][1]["message_id"]
    assert evs[0][1]["message_id"] == mid                              # Stop can cancel from the first event
    assert "".join(d["text"] for e, d in evs if e == "delta") == "Hello there"
    rec = next(d for e, d in evs if e == "receipt")
    assert rec["alias"] == "local/default" and rec["privacy"] == "local_only" and not rec["fallback"]
    assert {"label": "device", "value": "CPU"} in rec["tech"]          # from the registry, never guessed

    sent = world.chat[-1]
    assert sent.headers["authorization"] == "Bearer test-gateway-key"
    assert sent.headers["x-lif-data-class"] == "CONFIDENTIAL" and sent.headers["x-lif-workload"] == "console"
    assert "test-gateway-key" not in r.text

    # The phone sees the same conversation (§68–§70) and continues it with the earlier turn as context.
    phone, _ = pair(owner, app)
    assert t["id"] in [s["id"] for s in phone.get("/api/ai/threads").json()]
    on_phone = phone.get(f"/api/ai/threads/{t['id']}").json()
    assert [(m["role"], m["status"]) for m in on_phone["messages"]] == [("user", "done"), ("assistant", "done")]
    assert on_phone["messages"][1]["content"] == "Hello there" and on_phone["messages"][1]["receipt"]
    r2 = post(phone, f"/api/ai/threads/{t['id']}/messages", {"content": "Shorter please", "mode": "fast"})
    assert sse(r2.text)[-1][0] == "done"
    history = json.loads(world.chat[-1].content)["messages"]
    assert [m["content"] for m in history] == ["What is unified memory?", "Hello there", "Shorter please"]
    assert len(owner.get(f"/api/ai/threads/{t['id']}").json()["messages"]) == 4


@pytest.mark.parametrize("reply,title", [
    (httpx.Response(503, json={"error": {"message": "all models for local/default are unavailable: x: down",
                                         "type": "capacity"}}), "Balanced models are offline"),
    (httpx.ConnectError("refused"), "Local AI is unreachable"),
])
def test_ask_upstream_failure_is_a_human_error(app, world, reply, title):
    owner = admin(app)
    world.chat_reply = reply
    t = post(owner, "/api/ai/threads", {"mode": "balanced"}).json()
    evs = sse(post(owner, f"/api/ai/threads/{t['id']}/messages", {"content": "hi", "mode": "balanced"}).text)
    err = next(d for e, d in evs if e == "error")
    assert err["title"] == title and err["impact"] and err["next_step"] and evs[-1][0] == "done"
    saved = owner.get(f"/api/ai/threads/{t['id']}").json()["messages"][-1]
    assert saved["status"] == "error" and saved["error"]["title"] == title


# ── knowledge never leaks the private repo ───────────────────────────────────────────────────

def _keys(obj: Any) -> list[str]:
    """Every knowledge key anywhere in a response (hits, links, evidence, assumptions)."""
    if isinstance(obj, dict):
        return [v for k, v in obj.items() if k == "key" and isinstance(v, str)] + [x for v in obj.values()
                                                                                  for x in _keys(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _keys(v)]
    return []


def test_knowledge_never_returns_the_operations_repo(app, world):
    owner = admin(app)
    phone, _ = pair(owner, app)
    allowed = {r.name for r in kn_routes.public_workspace(LIF / "knowledge").all_repos()}
    assert "lif-operations" not in allowed and allowed

    responses = [phone.get("/api/knowledge").json()]
    for q in ("incident", "oom", "outage", "gpu", "memory", "event", "deploy", "operations", "production", "k3s",
              "backup", "model", "decision", "why", "token", "secret", "host"):
        hits = phone.get("/api/knowledge/search", params={"q": q})
        assert hits.status_code == 200
        responses.append(hits.json())
        for h in hits.json()[:3]:
            responses.append(phone.get(f"/api/knowledge/objects/{h['key']}").json())
    keys = [k for r in responses for k in _keys(r)]
    outside = [k for k in keys if k.split("::", 1)[0] not in allowed]
    assert keys and not outside, f"{len(outside)} keys from repos outside the public allowlist"
    text = json.dumps(responses)
    # (a public task's title names the repo — "Enable the controller event sink into lif-operations" — so the
    # check is on keys and repo fields, not on the words)
    assert '"repo": "lif-operations"' not in text and "/home/" not in text and "/private/" not in text

    assert phone.get("/api/knowledge/objects/lif-operations::incident").status_code == 404
    cmd = post(phone, "/api/command", {"text": "What happened in the last OOM incident?"}).json()
    assert cmd["kind"] == "knowledge_query" and "lif-operations::" not in json.dumps(cmd)


def test_knowledge_excludes_a_repo_named_lif_operations_even_if_marked_public(app, world, tmp_path, monkeypatch):
    root = tmp_path / "ws"
    shutil.copytree(LIF / "knowledge" / "registry", root / "registry")

    def write(p: Path, text: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip())

    write(root / "workspace.yaml", "name: t\nrepos:\n  - path: repos/pub\n  - path: repos/ops\n")
    for repo, name in (("pub", "pub"), ("ops", "lif-operations")):
        write(root / f"repos/{repo}/repo.yaml", f"""
            name: {name}
            version: 0.1.0
            kind: project
            data_class: PUBLIC
            dependencies:
              - {{name: knowledge-governance, version: ^1.0}}
        """)
        write(root / f"repos/{repo}/knowledge/{repo}-quokka.md", f"""
            ---
            type: assumption
            statement: Quokka note in {repo}.
            status: active
            ---
        """)
    monkeypatch.setenv("LIF_KNOWLEDGE_ROOT", str(root))
    owner = admin(app)
    hits = owner.get("/api/knowledge/search", params={"q": "quokka"}).json()
    assert [h["key"].split("::")[0] for h in hits] == ["pub"]
    assert owner.get("/api/knowledge/objects/lif-operations::ops-quokka").status_code == 404
    assert "lif-operations" not in json.dumps(owner.get("/api/knowledge").json())


# ── command bar over HTTP (§7, §8) ───────────────────────────────────────────────────────────

SPEC_COMMANDS = [
    ("Ask the local model to explain unified memory", "ai_prompt"),            # §7
    ("Run the code agent on repo X", "agent_request"),
    ("Check for better coding models", "model_request"),
    ("Why is the GPU busy?", "system_query"),
    ("Pause batch jobs", "operational_command"),
    ("What changed today?", "system_query"),
    ("Summarize current incidents", "system_query"),
    ("Why did local/default fall back to local/fast?", "system_query"),        # §8
    ("Write a Python parser for this JSON", "ai_prompt"),
    ("Check whether any better coding models were released", "model_request"),
    ("Why are we using K3s?", "knowledge_query"),                              # §33
    ("Open GPU", "navigation"),                                                # §50
]


def test_command_bar_routes_every_spec_example_and_executes_nothing(app, world):
    owner = admin(app)
    phone, _ = pair(owner, app)
    before = len(world.mutations())
    for text, kind in SPEC_COMMANDS:
        r = post(phone, "/api/command", {"text": text})
        assert r.status_code == 200 and r.json()["kind"] == kind, (text, r.json().get("kind"))
    pause = post(phone, "/api/command", {"text": "Pause batch jobs"}).json()["proposed_action"]
    assert pause["path"] == "/api/jobs/batch/pause-all" and pause["perm"] == "jobs.control"
    assert world.mutations()[before:] == []                            # proposals only, never executed


# ── translation tables and contracts ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,human", [
    ("CrashLoopBackOff", "Service repeatedly failed to start"),
    ("Pending", "Waiting for resources"),
])
def test_k8s_terms_read_as_plain_language(raw, human):
    health, label = hz.k8s_state(raw)
    assert label == human and raw not in label and health != "healthy"


@pytest.mark.parametrize("reason,fallback,cause", [
    ("primary unavailable: All connection attempts failed", True, "primary_unavailable"),
    ("local/code expects >= 14B; largest resident model is 4B (GPU reserved for the primary workload)", False,
     "below_quality_floor"),
    ("no local model is deployed for local/vision (capacity; see /v1/capabilities)", False, "not_deployed"),
])
def test_fallback_reasons_become_causes(reason, fallback, cause):
    got, label = hz.role_cause(reason, fallback=fallback, degraded=not fallback)
    assert got == cause and label and "/v1/" not in label


def test_contracts_ts_has_not_drifted():
    assert gen_ts.OUT.read_text() == gen_ts.render(), "run: lif/.venv/bin/python -m lif.console.gen_ts"


# ── live updates and lifecycle ───────────────────────────────────────────────────────────────

def test_sse_rechecks_the_session_while_frames_keep_flowing(monkeypatch):
    """The poller publishes most cycles, so an expired session must be noticed between frames too."""
    hub = events.Hub()
    monkeypatch.setattr(events.Hub, "HEARTBEAT_SEC", 0.05)
    user = User(id="u", name="owner", role="admin", perms=list(auth.ALL_PERMS))
    state = {"alive": True}

    async def run() -> int:
        frames = 0
        stream = hub.subscribe(user, session="s", alive=lambda: state["alive"])

        async def chatter() -> None:
            for i in range(200):
                hub.publish("activity", {"i": i})
                if i == 20:
                    state["alive"] = False
                await asyncio.sleep(0.005)
        task = asyncio.create_task(chatter())
        async for _ in stream:
            frames += 1
        task.cancel()
        return frames

    frames = asyncio.run(asyncio.wait_for(run(), 5))
    assert 0 < frames < 150 and len(hub) == 0


def test_app_lifespan_runs_the_poller_and_shuts_down_cleanly(world):
    from lif.console.app import create_app
    poller.reset()
    with TestClient(create_app(), base_url=ORIGIN) as c:
        assert poller.running()
        deadline = time.monotonic() + 5
        while not poller.snapshot().updated_at and time.monotonic() < deadline:
            time.sleep(0.02)
        assert poller.snapshot().updated_at
        assert c.get("/readyz").status_code == 200
        assert c.get("/manifest.webmanifest").headers["content-type"].startswith("application/manifest+json")
        assert c.get("/models/roles/fast").text.startswith("<!doctype html>")        # SPA fallback
        assert human_error(c.get("/api/nope"))
    assert not poller.running()


# ── abuse resistance: shared addresses, body size, lockouts ──────────────────────────────────

TRAEFIK = ("10.42.0.7", 40000)          # the ingress pod: a trusted proxy
SNAT = {"X-Forwarded-For": "10.42.0.1"}  # kube-proxy SNATs every LAN client to cni0 (externalTrafficPolicy: Cluster)


def lan(app: Any, port: int) -> TestClient:
    """A browser on the LAN, seen through Traefik behind SNAT: every one of them has the same address."""
    c = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False, client=(TRAEFIK[0], port), headers=SNAT)
    c.get("/healthz")
    return c


def test_shared_address_cant_throttle_signed_in_people(app):
    owner = lan(app, 40001)
    assert post(owner, "/api/setup", {"setup_code": CODE, "name": "owner", "passphrase": PASS,
                                      "default_mode": "auto"}).status_code == 200
    script = lan(app, 40002)                # no account: a curl loop echoing lz_csrf
    codes = {post(script, "/api/auth/logout").status_code for _ in range(130)}
    assert codes == {200}                   # anonymous sign-out is a no-op and spends nothing
    codes = [post(script, "/api/jobs/batch/pause-all", {"paused": True}).status_code for _ in range(125)]
    assert 429 in codes                     # anonymous mutations are still limited, per address...
    r = post(owner, "/api/ai/threads", {"mode": "auto", "privacy": "local_only"})
    assert r.status_code == 200, r.text     # ...but a signed-in session has its own bucket


def test_pair_status_without_a_claim_spends_nothing(app):
    owner = admin(app)
    start = post(owner, "/api/pair/start").json()
    phone = lan(app, 40005)
    assert post(phone, "/api/pair/claim", {"token": start["url"].split("#", 1)[1]}).status_code == 200
    script = lan(app, 40002)
    assert {script.get("/api/pair/status").status_code for _ in range(70)} == {404}
    assert phone.get("/api/pair/status").json()["status"] == "claimed"


def test_global_login_ceiling_holds_against_spoofed_addresses(app):
    codes = []
    for i in range(35):                     # a pod that could forge X-Forwarded-For: a fresh address each time
        c = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False, client=("10.42.3.9", 50000 + i),
                       headers={"X-Forwarded-For": f"192.0.2.{i}"})
        c.get("/healthz")
        codes.append(post(c, "/api/auth/login", {"name": f"u{i}", "passphrase": "wrong wrong wrong"}).status_code)
    assert codes[0] == 401 and codes[-1] == 429
    assert human_error(post(c, "/api/auth/login", {"name": "x", "passphrase": "y"}))["title"] == "Too many attempts"


def test_bodies_are_capped_before_parsing(app):
    c = browser(app)
    big = json.dumps({"name": "a" * 64 * 1024, "passphrase": "x" * 12})
    h = {"content-type": "application/json", "X-Labzilla-CSRF": c.cookies.get("lz_csrf") or "", "Origin": ORIGIN}
    r = c.post("/api/auth/login", content=big, headers=h)
    assert r.status_code == 413 and "too much" in human_error(r)["title"]

    def chunks():                           # no Content-Length: counted while it streams
        for _ in range(20):
            yield b" " * 4096
    r = c.post("/api/auth/login", content=chunks(), headers=h)
    assert r.status_code == 413
    # without a session nothing but sign-in is worth reading: a big Ask body is refused unread, with the
    # 401 the route would give (the client sends people to sign in on a 401, not on a 413)
    r = c.post("/api/ai/threads/th_x/messages", content=json.dumps({"content": "x" * 64 * 1024}), headers=h)
    assert r.status_code == 401 and human_error(r)["actions"] == [{"label": "Sign in", "action": "login"}]
    r = c.post("/api/ai/threads/th_x/messages", content=chunks(), headers=h)
    assert r.status_code == 401 and human_error(r)["title"] == "Sign in to continue"
    # an invalid (but small) name is refused without entering the per-name backoff table
    r = post(c, "/api/auth/login", {"name": "a" * 300, "passphrase": "x" * 12})
    assert r.status_code == 401 and not app.state.limiters.login_names._fails


def test_ended_session_with_a_big_body_is_told_to_sign_in(app, world, monkeypatch):
    import sqlite3

    from lif.console import errors
    c = admin(app)
    tid = post(c, "/api/ai/threads", {"mode": "fast"}).json()["id"]
    stale = c.cookies.get("lz_session") or ""
    assert post(c, "/api/auth/logout").status_code == 200
    c.cookies.set("lz_session", stale)      # a tab still holding the cookie of an ended session
    for path, body in ((f"/api/ai/threads/{tid}/messages", {"content": "x" * 5000, "mode": "fast"}),
                       ("/api/knowledge/notes", {"title": "t", "body": "x" * 5000})):
        r = post(c, path, body)
        assert r.status_code == 401, (path, r.text)
        assert human_error(r)["actions"] == [{"label": "Sign in", "action": "login"}]
    assert post(c, f"/api/ai/threads/{tid}/messages", {"content": "hi", "mode": "fast"}).status_code == 401

    # signed in, over the real cap: still "send less"
    owner = browser(app)
    r = post(owner, "/api/auth/login", {"name": "owner", "passphrase": PASS})
    assert r.status_code == 200, r.text
    r = post(owner, "/api/ai/threads", {"mode": "fast", "title": "x" * 70 * 1024})
    assert r.status_code == 413 and "64 KB" in human_error(r)["next_step"]

    # the session lookup itself failed: that is the storage error, not "too much" or "sign in"
    def down(_request):
        raise errors.storage(sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(auth, "_load", down)
    r = post(owner, f"/api/ai/threads/{tid}/messages", {"content": "x" * 5000, "mode": "fast"})
    assert r.status_code == 503 and human_error(r)["title"] == "Labzilla can't reach its own storage"


def test_ask_limit_is_spent_before_the_body_is_read(app, world):
    owner = admin(app)
    tid = post(owner, "/api/ai/threads", {"mode": "fast"}).json()["id"]
    key = f"s:{auth.token_hash(owner.cookies.get('lz_session') or '')}"
    for _ in range(20):
        assert app.state.limiters.ask.hit(key)
    reads: list[int] = []

    async def counting(scope, receive, send):
        async def counted():
            msg = await receive()
            reads.append(len(msg.get("body", b"")))
            return msg
        await app(scope, counted, send)
    c = TestClient(counting, base_url=ORIGIN, raise_server_exceptions=False, cookies=dict(owner.cookies))
    r = c.post(f"/api/ai/threads/{tid}/messages", content=json.dumps({"content": "x" * 1024 * 1024}),
               headers={"content-type": "application/json", "X-Labzilla-CSRF": owner.cookies.get("lz_csrf") or "",
                        "Origin": ORIGIN})
    assert r.status_code == 429 and {"label": "limit", "value": "ask"} in human_error(r)["tech"]
    assert reads == []                      # refused without reading (or parsing) the megabyte


def test_big_ask_bodies_wait_for_an_intake_slot(app, world, monkeypatch):
    from lif.console import app as app_module
    monkeypatch.setattr(app_module, "ASK_INTAKE_SLOTS", 0)       # every slot busy
    monkeypatch.setattr(app_module, "ASK_INTAKE_WAIT_SEC", 0.05)
    owner = admin(app)
    tid = post(owner, "/api/ai/threads", {"mode": "fast"}).json()["id"]
    r = post(owner, f"/api/ai/threads/{tid}/messages", {"content": "x" * 300 * 1024, "mode": "fast"})
    assert r.status_code == 503 and human_error(r)["title"] == "Labzilla is busy taking other messages"
    assert not world.chat
    r = post(owner, f"/api/ai/threads/{tid}/messages", {"content": "small", "mode": "fast"})
    assert r.status_code == 200 and [e for e, _ in sse(r.text)][-1] == "done"   # small ones don't queue


def test_one_conversation_cannot_outgrow_its_cap(app, world, monkeypatch):
    from lif.console import threads
    assert threads.THREAD_QUOTA_BYTES * 2 <= threads.USER_QUOTA_BYTES   # one thread can't push out all others
    owner = admin(app)
    other = post(owner, "/api/ai/threads", {"mode": "fast"}).json()["id"]
    threads.add_message(other, "user", "older conversation")
    with db.tx() as c:
        c.execute("UPDATE threads SET updated_at=0 WHERE id=?", (other,))
    tid = post(owner, "/api/ai/threads", {"mode": "fast"}).json()["id"]
    monkeypatch.setattr(threads, "THREAD_QUOTA_BYTES", 12000)
    monkeypatch.setattr(threads, "USER_QUOTA_BYTES", 40000)
    codes = []
    for _ in range(4):
        threads._quota_checked.clear()
        before = (len(world.chat), db.one("SELECT COUNT(*) AS n FROM messages WHERE thread_id=?", (tid,))["n"])
        r = post(owner, f"/api/ai/threads/{tid}/messages", {"content": "x" * 5000, "mode": "fast"})
        codes.append(r.status_code)
    assert codes == [200, 200, 413, 413]
    err = human_error(r)
    assert err["title"] == "This conversation is too long"
    assert {"label": "New conversation", "action": "/ask"} in err["actions"]
    after = (len(world.chat), db.one("SELECT COUNT(*) AS n FROM messages WHERE thread_id=?", (tid,))["n"])
    assert after == before                  # refused before anything was stored or sent
    assert threads.thread_bytes(tid) <= 12000
    assert db.one("SELECT 1 FROM threads WHERE id=?", (other,)) is not None     # nothing else was pruned


def test_quota_failure_does_not_strand_the_turn(app, world, monkeypatch):
    import sqlite3

    from lif.console import threads
    owner = admin(app)
    tid = post(owner, "/api/ai/threads", {"mode": "fast"}).json()["id"]

    def full(*_a, **_k):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(threads, "enforce_quota", full)
    r = post(owner, f"/api/ai/threads/{tid}/messages", {"content": "hello", "mode": "fast"})
    assert r.status_code == 200 and [e for e, _ in sse(r.text)][-1] == "done"
    msgs = owner.get(f"/api/ai/threads/{tid}").json()["messages"]
    assert [(m["role"], m["status"]) for m in msgs] == [("user", "done"), ("assistant", "done")]


def test_largest_legitimate_ask_message_fits(app, world):
    c = admin(app)
    tid = post(c, "/api/ai/threads", {"mode": "fast", "privacy": "local_only"}).json()["id"]
    code = ('print("x")\n' * 60000)[:256 * 1024]
    body = {"content": 'Review "this"\n' * 37000, "mode": "fast", "privacy": "local_only",
            "attachments": [{"name": f"f{i}.py", "kind": "code", "size": len(code), "text": code} for i in range(4)]}
    assert len(json.dumps(body)) > 1.8 * 1024 * 1024       # escaped quotes and newlines
    r = post(c, f"/api/ai/threads/{tid}/messages", body)
    assert r.status_code == 200 and [e for e, _ in sse(r.text)][-1] == "done"


def test_name_lock_is_short():
    b = auth.LoginBackoff()
    for _ in range(30):
        b.failed("owner")
    assert 0 < b.wait("owner") <= 60


def test_files_never_go_to_jev(app, world):
    owner = admin(app)
    t = post(owner, "/api/ai/threads", {"mode": "auto", "privacy": "allow_jev"}).json()
    post(owner, f"/api/ai/threads/{t['id']}/messages", {"content": "short question", "mode": "auto",
                                                        "privacy": "allow_jev"})
    assert world.chat[-1].headers["x-lif-data-class"] == "PUBLIC"          # the toggle works for typed text
    r = post(owner, f"/api/ai/threads/{t['id']}/messages", {
        "content": "what's wrong here?", "mode": "auto", "privacy": "allow_jev",
        "attachments": [{"name": "app.log", "kind": "log", "size": 20, "text": "ERROR db password rejected"}]})
    assert world.chat[-1].headers["x-lif-data-class"] == "CONFIDENTIAL"    # ...but files keep it local
    rec = next(d for e, d in sse(r.text) if e == "receipt")
    assert rec["privacy"] == "local_only"
    note = owner.get(f"/api/ai/threads/{t['id']}").json()["messages"][-2]["attachments"][0]["note"]
    assert "never go to Jev" in note


def test_ask_is_limited_per_session_and_storage_is_bounded(app, world, monkeypatch):
    owner = admin(app)
    tid = post(owner, "/api/ai/threads", {"mode": "fast"}).json()["id"]
    codes = [post(owner, f"/api/ai/threads/{tid}/messages", {"content": "hi", "mode": "fast"}).status_code
             for _ in range(22)]
    assert codes[:20] == [200] * 20 and codes[-1] == 429

    from lif.console import threads
    big = "x" * 40000                       # bigger than the replayable context: the file text isn't kept
    threads.start_turn(tid, "q", [{"name": "f.txt", "kind": "text", "included": True, "text": big}],
                              mode="fast", privacy="local_only", title=None)
    stored = json.loads(db.one("SELECT attachments_json FROM messages WHERE thread_id=? AND role='user' "
                               "ORDER BY created_at DESC, rowid DESC LIMIT 1", (tid,))["attachments_json"])
    assert stored[0].get("text_omitted") and "text" not in stored[0]

    user_id = db.one("SELECT user_id FROM threads WHERE id=?", (tid,))["user_id"]
    old = threads.create_thread(user_id, "fast", "local_only", "old")
    threads.add_message(old.id, "user", "y" * 5000)
    monkeypatch.setattr(threads, "USER_QUOTA_BYTES", 3000)
    with db.tx() as c:                      # the old thread is older than the one being written to
        c.execute("UPDATE threads SET updated_at=0 WHERE id=?", (old.id,))
    assert threads.enforce_quota(user_id, keep=tid, force=True) == [old.id]
    assert db.one("SELECT 1 FROM threads WHERE id=?", (tid,)) is not None


def test_thread_storage_errors_are_human_and_leave_no_orphan(app, world, monkeypatch):
    import sqlite3
    from lif.console import threads
    owner = admin(app)
    tid = post(owner, "/api/ai/threads", {"mode": "fast"}).json()["id"]
    real = db.tx

    def locked():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(threads.db, "tx", locked)
    r = post(owner, f"/api/ai/threads/{tid}/messages", {"content": "done", "mode": "fast"})
    assert r.status_code == 503 and "storage" in human_error(r)["title"]
    monkeypatch.setattr(threads.db, "tx", real)
    assert owner.get(f"/api/ai/threads/{tid}").json()["messages"] == []    # no question without an answer
    monkeypatch.setattr(threads.db, "q", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
    assert owner.get("/api/ai/threads").status_code == 503


# ── Understanding Compiler mount (off by default) ────────────────────────────────────────────

def test_understanding_api_is_off_unless_enabled(app: Any) -> None:
    assert browser(app).get("/api/v1/renderers").status_code == 404


def test_understanding_api_mounts_behind_console_auth(world: World, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from lif.console.app import create_app
    from lif.understanding import api as uapi
    from lif.understanding.compiler import Compiler
    from lif.understanding.store import Store
    monkeypatch.setenv("LIF_CONSOLE_UNDERSTANDING", "1")
    monkeypatch.setattr(uapi, "_compiler", Compiler(Store(str(tmp_path / "u.db")), collectors={}))
    a = create_app()
    anon = browser(a)
    assert anon.get("/api/v1/renderers").status_code in (401, 403)
    owner = admin(a)
    assert owner.get("/api/v1/renderers").status_code == 200
    r = post(owner, "/api/v1/explain", {"question": "What is Kubernetes?"})
    assert r.status_code == 200 and r.json()["primary"] == "ste-prose"
    sim = post(owner, "/api/v1/explain", {"question": "How does changing GPU reservation affect throughput?"}).json()
    art = owner.get(f"/api/v1/sessions/{sim['session']}/artifacts/simulation")
    assert art.status_code == 200
    csp = art.headers.get_list("content-security-policy")
    assert csp and all(c.startswith("sandbox allow-scripts") for c in csp), csp   # the console's own CSP must not stack
