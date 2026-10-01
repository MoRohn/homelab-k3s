"""Console CORE: sessions, CSRF, rate limits, setup, pairing lifecycle, devices, SSE hub, headers.

Runs over https://testserver so the real cookie flags (Secure, HttpOnly, SameSite=Strict) are
exercised — LIF_CONSOLE_INSECURE_COOKIES would hide them. No upstreams are called.
"""
from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from lif.console import auth, db, settings
from lif.console.contracts import User
from lif.console.events import QUEUE_MAX, Hub

ORIGIN = "https://testserver"
CODE = "setup-code-for-tests"
PASS = "correct horse battery"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LIF_CONSOLE_DB", str(tmp_path / "console.db"))
    monkeypatch.setenv("LIF_CONSOLE_SETUP_CODE", CODE)
    monkeypatch.delenv("LIF_CONSOLE_INSECURE_COOKIES", raising=False)
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>Labzilla</title>")
    (ui / "assets" / "app-abc123.js").write_text("console.log(1)")
    (ui / "sw.js").write_text("// sw")
    monkeypatch.setenv("LIF_CONSOLE_UI_DIR", str(ui))
    yield tmp_path
    db.close()


@pytest.fixture()
def app(env):
    from lif.console.app import create_app
    return create_app()


def client(app) -> TestClient:
    c = TestClient(app, base_url=ORIGIN)
    c.get("/healthz")           # any GET issues lz_csrf
    return c


def post(c: TestClient, path: str, json: dict | None = None, **kw):
    headers = {"X-Labzilla-CSRF": c.cookies.get("lz_csrf") or "", "Origin": ORIGIN, **kw.pop("headers", {})}
    return c.post(path, json=json or {}, headers=headers, **kw)


def delete(c: TestClient, path: str):
    return c.delete(path, headers={"X-Labzilla-CSRF": c.cookies.get("lz_csrf") or "", "Origin": ORIGIN})


def setup_admin(c: TestClient):
    r = post(c, "/api/setup", {"setup_code": CODE, "name": "owner", "passphrase": PASS, "default_mode": "auto"})
    assert r.status_code == 200, r.text
    return r


def cookie_line(r, name: str) -> str:
    return next(v for v in r.headers.get_list("set-cookie") if v.startswith(f"{name}="))


# ── passphrases and limiters ──────────────────────────────────────────────────────────────────

def test_scrypt_roundtrip():
    h = auth.hash_passphrase(PASS)
    assert h.startswith("scrypt$16384$8$1$") and PASS not in h
    assert auth.verify_passphrase(PASS, h)
    assert not auth.verify_passphrase(PASS + "x", h)
    assert not auth.verify_passphrase(PASS, "garbage")
    assert auth.hash_passphrase(PASS) != h          # per-user salt


def test_rate_limiter_token_bucket():
    lim = auth.RateLimiter(3, 60)
    assert [lim.hit("a") for _ in range(4)] == [True, True, True, False]
    assert lim.hit("b")                              # keys are independent
    assert lim.retry_after("a") >= 1


def _req(peer: str, headers: dict[str, str], scheme: str = "http") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "scheme": scheme, "server": ("x", 80),
                    "client": (peer, 1234), "query_string": b"",
                    "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})


def test_client_ip_trusts_forwarded_headers_only_from_proxy():
    xff = {"x-forwarded-for": "6.6.6.6, 192.168.1.20", "x-forwarded-proto": "https"}
    assert auth.client_ip(_req("10.42.0.17", xff)) == "192.168.1.20"     # right-most untrusted hop
    assert auth.is_secure(_req("10.42.0.17", xff))
    assert auth.client_ip(_req("192.168.1.50", xff)) == "192.168.1.50"   # spoofed header ignored
    assert not auth.is_secure(_req("192.168.1.50", xff))
    assert auth.client_ip(_req("testclient", xff)) == "testclient"       # non-IP peer doesn't raise


def test_csrf_origin_normalisation():
    base = {"cookie": "lz_csrf=t", "x-labzilla-csrf": "t"}
    ok = _req("1.2.3.4", {**base, "host": "labzilla.local", "origin": "https://labzilla.local"})
    assert auth.csrf_problem(ok) is None
    dev = _req("1.2.3.4", {**base, "host": "localhost:5173", "origin": "http://localhost:5173"})
    assert auth.csrf_problem(dev) is None
    evil = _req("1.2.3.4", {**base, "host": "labzilla.local", "origin": "https://evil.example"})
    assert auth.csrf_problem(evil) == "cross_origin"
    ref = _req("1.2.3.4", {**base, "host": "labzilla.local", "referer": "https://labzilla.local/pair"})
    assert auth.csrf_problem(ref) is None
    assert auth.csrf_problem(_req("1.2.3.4", {**base, "host": "labzilla.local"})) == "no_origin"


# ── setup, login, sessions, CSRF over HTTP ────────────────────────────────────────────────────

def test_setup_gate_and_cookie_flags(app, monkeypatch):
    c = client(app)
    st = c.get("/api/setup").json()
    assert st["needs_setup"] and st["setup_code_configured"]
    assert [d["label"] for d in st["detected"]] == ["DGX Spark", "K3s", "GPU", "Network"]

    r = post(c, "/api/setup", {"setup_code": "wrong", "name": "owner", "passphrase": PASS})
    assert r.status_code == 403 and r.json()["error"]["title"] == "That setup code isn't right"
    r = post(c, "/api/setup", {"setup_code": CODE, "name": "owner", "passphrase": "short"})
    assert r.status_code == 422 and "longer passphrase" in r.json()["error"]["title"]

    r = setup_admin(c)
    assert r.json()["role"] == "admin" and "devices.manage" in r.json()["perms"]
    line = cookie_line(r, "lz_session").lower()
    assert "httponly" in line and "secure" in line and "samesite=strict" in line and "path=/" in line
    assert c.get("/api/auth/me").json()["name"] == "owner"
    assert post(c, "/api/setup", {"setup_code": CODE, "name": "x", "passphrase": PASS}).status_code == 409
    assert not c.get("/api/setup").json()["needs_setup"]


def test_setup_refused_without_configured_code(app, monkeypatch):
    monkeypatch.delenv("LIF_CONSOLE_SETUP_CODE")
    monkeypatch.setenv("LIF_SECRETS_DIR", "/nonexistent")
    c = client(app)
    assert not c.get("/api/setup").json()["setup_code_configured"]
    r = post(c, "/api/setup", {"setup_code": "", "name": "owner", "passphrase": PASS})
    assert r.status_code == 503 and r.json()["error"]["title"] == "Setup code isn't configured"


def test_csrf_rejections_and_headers(app):
    c = TestClient(app, base_url=ORIGIN)
    r = c.get("/api/setup")
    assert r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    line = cookie_line(r, "lz_csrf").lower()
    assert "httponly" not in line and "samesite=strict" in line
    assert "lz_csrf" not in (c.get("/api/setup").headers.get("set-cookie") or "")    # issued once, not per GET

    fresh = TestClient(app, base_url=ORIGIN)       # POST before any GET: no cookie yet
    r = fresh.post("/api/auth/login", json={"name": "a", "passphrase": PASS}, headers={"Origin": ORIGIN})
    assert r.status_code == 403 and r.json()["error"]["actions"][0]["action"] == "reload"
    r = c.post("/api/auth/login", json={}, headers={"Origin": ORIGIN, "X-Labzilla-CSRF": "nope"})
    assert r.status_code == 403
    r = post(c, "/api/auth/login", {"name": "a", "passphrase": PASS}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["title"] == "Request blocked for safety"


def test_login_rate_limit_backoff_and_logout(app):
    c = client(app)
    setup_admin(c)
    post(c, "/api/auth/logout")
    assert c.get("/api/auth/me").status_code == 401

    bad = post(c, "/api/auth/login", {"name": "owner", "passphrase": "wrong passphrase"})
    assert bad.status_code == 401 and "don't match" in bad.json()["error"]["title"]
    ok = post(c, "/api/auth/login", {"name": "OWNER", "passphrase": PASS})       # names are case-insensitive
    assert ok.status_code == 200 and c.get("/api/auth/me").status_code == 200
    codes = [post(c, "/api/auth/login", {"name": "ghost", "passphrase": PASS}).status_code for _ in range(4)]
    assert codes[-1] == 429                       # 5/min per IP across all names
    assert "retry-after" in post(c, "/api/auth/login", {"name": "x", "passphrase": PASS}).headers

    out = post(c, "/api/auth/logout")
    assert out.status_code == 200 and c.get("/api/auth/me").status_code == 401


def test_per_name_backoff():
    b = auth.LoginBackoff()
    for _ in range(5):
        assert b.wait("owner") == 0
        b.failed("Owner")
    b.failed("owner")
    assert b.wait("OWNER") > 0
    b.succeeded("owner")
    assert b.wait("owner") == 0


# ── pairing lifecycle (§15–§17) ───────────────────────────────────────────────────────────────

def test_pairing_lifecycle_and_revocation(app):
    desk = client(app)
    setup_admin(desk)
    start = post(desk, "/api/pair/start")
    assert start.status_code == 200
    p = start.json()
    token = p["token"]
    assert p["status"] == "waiting" and p["url"].endswith(f"/pair#{token}")

    phone = TestClient(app, base_url=ORIGIN, headers={"User-Agent": "Mozilla/5.0 (iPhone) Safari/605.1"})
    assert phone.get("/pair").status_code == 200          # the SPA page issues lz_csrf to the phone
    assert phone.cookies.get("lz_csrf")
    claim = post(phone, "/api/pair/claim", {"token": token, "device_name": "Kitchen\x00 phone"})
    assert claim.status_code == 200, claim.text
    code = claim.json()["code"]
    assert len(code) == 6 and code.isdigit() and claim.json()["device_name"] == "Kitchen phone"
    assert "token" not in claim.json() or claim.json()["token"] is None
    assert post(phone, "/api/pair/claim", {"token": token}).json()["code"] == code     # idempotent double tap

    other = client(app)
    assert post(other, "/api/pair/claim", {"token": token}).status_code == 410        # single use

    seen = desk.get(f"/api/pair/{p['id']}").json()
    assert seen["status"] == "claimed" and seen["code"] == code                          # same code both sides
    assert phone.get("/api/pair/status").json()["status"] == "claimed"

    from lif.console.routes import auth as auth_routes
    pending = auth_routes.pending_approvals()
    assert [a.id for a in pending] == [f"pairing:{p['id']}"] and pending[0].kind == "device_pairing"

    assert post(desk, f"/api/pair/{p['id']}/approve").json()["status"] == "approved"
    first = phone.get("/api/pair/status")
    assert first.json()["status"] == "approved" and "lz_session" in first.headers.get("set-cookie", "")
    again = phone.get("/api/pair/status")
    assert "lz_session" not in (again.headers.get("set-cookie") or "")                   # delivered once

    me = phone.get("/api/auth/me").json()
    assert me["role"] == "device" and me["device_name"] == "Kitchen phone"
    assert "models.release" not in me["perms"] and "jobs.control" in me["perms"]
    assert post(phone, "/api/pair/start").status_code == 403                             # no devices.manage
    assert phone.get("/api/devices").status_code == 403

    devs = desk.get("/api/devices").json()
    assert len(devs) == 1 and devs[0]["user_agent_summary"] == "Safari on iPhone" and not devs[0]["current"]
    assert delete(desk, f"/api/devices/{devs[0]['id']}").status_code == 200
    assert phone.get("/api/auth/me").status_code == 401                                  # sessions gone at once
    assert desk.get("/api/devices").json() == []


def test_pairing_reject_and_expiry(app, monkeypatch):
    desk = client(app)
    setup_admin(desk)
    p = post(desk, "/api/pair/start").json()
    phone = client(app)
    post(phone, "/api/pair/claim", {"token": p["token"], "device_name": "tablet"})
    assert post(desk, f"/api/pair/{p['id']}/reject").json()["status"] == "rejected"
    st = phone.get("/api/pair/status")
    assert st.json()["status"] == "rejected" and "lz_session" not in (st.headers.get("set-cookie") or "")
    assert post(desk, f"/api/pair/{p['id']}/approve").status_code == 409

    monkeypatch.setattr(settings, "pairing_ttl_sec", lambda: -1)
    late = post(desk, "/api/pair/start").json()
    r = post(client(app), "/api/pair/claim", {"token": late["token"]})
    assert r.status_code == 410 and "expired" in r.json()["error"]["title"]
    assert post(client(app), "/api/pair/claim", {"token": "not-a-token"}).status_code == 404


def test_pair_claim_rate_limited(app):
    phone = client(app)
    codes = [post(phone, "/api/pair/claim", {"token": f"t{i}"}).status_code for i in range(11)]
    assert codes[:10] == [404] * 10 and codes[10] == 429


# ── app plumbing ──────────────────────────────────────────────────────────────────────────────

def test_probes_metrics_spa_and_unknown_api(app):
    c = client(app)
    assert c.get("/readyz").status_code == 200
    m = c.get("/metrics")
    assert m.status_code == 200 and "lif_console_requests_total" in m.text
    assert c.get("/metrics", headers={"X-Forwarded-For": "192.168.1.9"}).status_code == 404
    r = c.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"]["title"] == "Not found"
    assert c.get("/models/roles/fast").headers["cache-control"] == "no-cache"
    assert c.get("/sw.js").headers["cache-control"] == "no-cache"
    assert "immutable" in c.get("/assets/app-abc123.js").headers["cache-control"]
    assert c.get("/api/events").status_code == 401
    acc = c.get("/api/access").json()
    assert acc["secure"] and acc["mdns"] == settings.mdns() and acc["public_url"].startswith("https://")


def test_audit_rows_written(app):
    c = client(app)
    setup_admin(c)
    post(c, "/api/pair/start")
    actions = [r["action"] for r in db.q("SELECT action FROM audit ORDER BY id")]
    assert actions == ["setup.admin_created", "pairing.started"]


# ── SSE hub ───────────────────────────────────────────────────────────────────────────────────

async def _drain(gen) -> list[str]:
    """Event types until the stream ends (fails the test if it doesn't end within 1 s)."""
    out = []
    while True:
        try:
            frame = await asyncio.wait_for(gen.__anext__(), 1)
        except StopAsyncIteration:
            return out
        out.append(frame.split("\n")[0].removeprefix("event: "))


ADMIN = User(id="u1", name="owner", role="admin", perms=["read"])
PHONE = User(id="u1", name="owner", role="device", device_name="phone", perms=["read"])
OTHER = User(id="u2", name="guest", role="device", perms=["read"])


async def _frames(gen, n: int) -> list[str]:
    return [await asyncio.wait_for(gen.__anext__(), 1) for _ in range(n)]


def test_hub_audience_drop_oldest_threads_and_disconnect():
    async def run() -> None:
        hub = Hub()
        gens = {u.role + u.id: hub.subscribe(u, session=f"s-{u.role}{u.id}", device_id="d1" if u is PHONE else None)
                for u in (ADMIN, PHONE, OTHER)}
        for g in gens.values():
            assert (await _frames(g, 1)) == ["retry: 3000\n\n"]
        await asyncio.sleep(0)
        assert len(hub) == 3

        hub.publish("pairing", {"id": "p1"}, audience="admin")
        hub.publish("thread", {"thread_id": "t"}, audience="user:u1")
        hub.publish("status", {"health": "healthy"})
        assert (await _frames(gens["adminu1"], 3))[0].startswith("event: pairing\ndata: {\"id\":\"p1\"}")
        phone = await _frames(gens["deviceu1"], 2)
        assert [f.split("\n")[0] for f in phone] == ["event: thread", "event: status"]
        assert (await _frames(gens["deviceu2"], 1))[0].startswith("event: status")

        t = threading.Thread(target=hub.publish, args=("jobs", {"n": 1}))     # from the threadpool
        t.start()
        t.join()
        assert (await _frames(gens["deviceu2"], 1))[0].startswith("event: jobs")

        for i in range(QUEUE_MAX + 5):
            hub.publish("activity", {"i": i}, audience="user:u2")
        first = (await _frames(gens["deviceu2"], 1))[0]
        assert '"i":5}' in first                                    # the 5 oldest were dropped

        assert hub.disconnect(device_id="d1") == 1
        assert await _drain(gens["deviceu1"]) == ["jobs"]             # queued frames, then the stream ends
        assert len(hub) == 2
        late = hub.subscribe(OTHER)
        assert (await _frames(late, 2))[1].startswith("event: status")   # late joiner gets last status
        hub.close()
        assert await _drain(gens["adminu1"]) == ["jobs"]
        assert await _drain(late) == []

    asyncio.run(run())
