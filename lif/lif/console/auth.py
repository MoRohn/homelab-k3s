"""Authentication, sessions, roles, CSRF, rate limits and audit for the console (brief §3, spec §16, §77).

Server-enforced: the UI hiding a button is never the control. Every mutating route declares
`Depends(require("<perm>"))`; CSRF is checked by middleware for non-GET/HEAD/OPTIONS; every mutation
is recorded with `audit()`.

Sessions: an opaque 32-byte token in `lz_session` (HttpOnly, SameSite=Strict, Secure unless
LIF_CONSOLE_INSECURE_COOKIES). Only its sha256 is stored, so a copy of the database can't be replayed.
A paired device's session acts *as the admin who paired it* with role "device" (User.id = that
admin's id, User.device_name set): threads and `user:<id>` events follow the person across their
phone and desktop (§68–§70), while the device role caps what the phone may do (§99). The device id
and session live on `request.state.session`.

CSRF (no exemptions): every GET response that arrives without `lz_csrf` gets one (random,
JS-readable), including the SPA's index.html — so a phone that opens /pair#<token> holds the cookie
before it can POST /api/pair/claim, and /login and /setup likewise. A mutation must echo the cookie
in X-Labzilla-CSRF *and* carry an Origin (or Referer) whose host equals Host. The session cookie is
SameSite=Strict on top. A mutation without the cookie gets a human 403 with a "reload" action.

Client address: X-Forwarded-For / X-Forwarded-Proto are believed only when the TCP peer is a trusted
proxy (console.trusted_proxies, default the pod network Traefik runs in); the client is then the
right-most untrusted hop. Otherwise the peer address is the client.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, Request, Response
from prometheus_client import Counter

from lif.common import log
from lif.console import db, settings
from lif.console.contracts import Perm, User, UserRole
from lif.console.errors import HumanHTTPError, human

LOG = log.get("lif.console.audit")

SESSION_COOKIE = "lz_session"     # HttpOnly, Secure (unless LIF_CONSOLE_INSECURE_COOKIES), SameSite=Strict
CSRF_COOKIE = "lz_csrf"           # readable by JS; echoed in CSRF_HEADER on mutations
CSRF_HEADER = "X-Labzilla-CSRF"
PAIR_COOKIE = "lz_pair"           # HttpOnly claim cookie held by a phone while pairing

ALL_PERMS: tuple[Perm, ...] = ("read", "ask", "jobs.control", "approvals.answer", "models.discover",
                               "models.operate", "models.release", "system.safe", "system.settings",
                               "devices.manage")
ROLE_PERMS: dict[UserRole, frozenset[Perm]] = {
    "admin": frozenset(ALL_PERMS),
    # Phones/tablets: safe operations only (spec §99) — no release, settings or device management.
    "device": frozenset({"read", "ask", "jobs.control", "approvals.answer", "models.discover", "system.safe"}),
}

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LAST_SEEN_EVERY_SEC = 60.0          # session/device last_seen is written at most this often
MIN_PASSPHRASE = 10

# ── metrics (lif_console_*; scraped by the lif-services ServiceMonitor) ──────────────────────────
logins = Counter("lif_console_logins_total", "Console sign-in attempts", ["outcome"])     # ok|bad|limited
pairings = Counter("lif_console_pairings_total", "Device pairing steps", ["step"])
rate_limited = Counter("lif_console_rate_limited_total", "Requests refused by a console rate limit", ["limiter"])
csrf_rejected = Counter("lif_console_csrf_rejected_total", "Mutations refused by the CSRF/origin check",
                        ["reason"])

# Set by the middleware for each request so audit() can record where an action came from.
client_ip_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("lz_client_ip", default=None)


# ── passphrases and tokens ───────────────────────────────────────────────────────────────────

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_passphrase(passphrase: str) -> str:
    """scrypt (n=2**14, r=8, p=1) with a per-user salt, encoded for storage."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(passphrase.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${_b64(salt)}${_b64(dk)}"


def verify_passphrase(passphrase: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(passphrase.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                            dklen=len(_unb64(want)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, _unb64(want))


# Unknown names still pay for one scrypt, so response time doesn't reveal which names exist.
DUMMY_HASH = hash_passphrase(secrets.token_urlsafe(16))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def valid_passphrase(passphrase: str) -> bool:
    return MIN_PASSPHRASE <= len(passphrase) <= 1024


_NAME = re.compile(r"^[\w][\w .@-]{0,39}$")


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name.strip()))


# ── client address and transport ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=8)
def _networks(cidrs: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    out = []
    for c in cidrs:
        try:
            out.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            log.event(LOG, "bad_trusted_proxy", cidr=c)
    return tuple(out)


def _trusted(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:          # "testclient", unix sockets, garbage in a header
        return False
    return any(ip in n for n in _networks(tuple(settings.trusted_proxies())))


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if not _trusted(peer):
        return peer or "unknown"
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not _trusted(hop):
            return hop
    return hops[0] if hops else peer


def is_secure(request: Request) -> bool:
    """HTTPS as the browser sees it (TLS ends at Traefik, which says so in X-Forwarded-Proto)."""
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else ""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return proto == "https" and _trusted(peer)


def _cookie_secure() -> bool:
    return not settings.insecure_cookies()


def set_cookie(response: Response, name: str, value: str, max_age: int, *, http_only: bool = True,
               path: str = "/") -> None:
    response.set_cookie(name, value, max_age=max_age, path=path, secure=_cookie_secure(), httponly=http_only,
                        samesite="strict")


def clear_cookie(response: Response, name: str, *, path: str = "/") -> None:
    response.delete_cookie(name, path=path, secure=_cookie_secure(), httponly=name != CSRF_COOKIE,
                           samesite="strict")


def csrf_cookie_header() -> str:
    """A Set-Cookie value issuing a fresh lz_csrf (used by the middleware on GET responses)."""
    r = Response()
    set_cookie(r, CSRF_COOKIE, secrets.token_urlsafe(24), settings.device_session_ttl_days() * 86400,
               http_only=False)
    return r.headers["set-cookie"]


# ── sessions ─────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Session:
    token_hash: str
    user_id: str
    role: UserRole
    device_id: str | None
    expires_at: float


def make_user(user_id: str, name: str, role: UserRole, device_name: str | None = None) -> User:
    perms = ROLE_PERMS.get(role, frozenset())
    return User(id=user_id, name=name, role=role, device_name=device_name,
                perms=[p for p in ALL_PERMS if p in perms])


def create_session(conn: sqlite3.Connection, user_id: str, role: UserRole, *, device_id: str | None = None,
                   user_agent: str = "", now: float | None = None) -> tuple[str, int]:
    """Insert a session inside the caller's tx(); returns (cookie token, max_age seconds)."""
    now = time.time() if now is None else now
    days = settings.device_session_ttl_days() if role == "device" else settings.session_ttl_days()
    ttl = int(days * 86400)
    token = new_token()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO sessions(token_hash, user_id, device_id, role, created_at, expires_at, last_seen,"
                 " user_agent) VALUES(?,?,?,?,?,?,?,?)",
                 (token_hash(token), user_id, device_id, role, now, now + ttl, now, user_agent[:300]))
    return token, ttl


def set_session(response: Response, token: str, max_age: int) -> None:
    set_cookie(response, SESSION_COOKIE, token, max_age)


def _db_down(exc: Exception) -> Exception:
    log.event(LOG, "db_unavailable", error=type(exc).__name__)
    return human(503, "Labzilla can't read its sign-in records", "Signing in and saved conversations are "
                 "unavailable until the console's storage is back.", "Retry in a moment.", [("Retry", "retry")],
                 {"error": type(exc).__name__})


def _load(request: Request) -> tuple[User, Session] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 200:
        return None
    th = token_hash(token)
    try:
        row = db.one("SELECT s.*, u.name AS user_name, d.name AS device_name, d.revoked_at "
                     "FROM sessions s JOIN users u ON u.id = s.user_id "
                     "LEFT JOIN devices d ON d.id = s.device_id WHERE s.token_hash = ?", (th,))
        if row is None:
            return None
        now = time.time()
        if row["expires_at"] < now or (row["device_id"] and (row["device_name"] is None or row["revoked_at"])):
            with db.tx() as c:
                c.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
            return None
        if now - row["last_seen"] > LAST_SEEN_EVERY_SEC:
            with db.tx() as c:
                c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, th))
                if row["device_id"]:
                    c.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, row["device_id"]))
    except sqlite3.Error as e:
        raise _db_down(e) from e
    role: UserRole = "admin" if row["role"] == "admin" else "device"
    return (make_user(row["user_id"], row["user_name"], role, row["device_name"]),
            Session(th, row["user_id"], role, row["device_id"], row["expires_at"]))


async def optional_user(request: Request) -> User | None:
    """The signed-in user for this request, or None. Cached on request.state for the request."""
    if not hasattr(request.state, "lz_user"):
        loaded = _load(request)
        request.state.lz_user = loaded[0] if loaded else None
        request.state.session = loaded[1] if loaded else None
    return request.state.lz_user


def session_of(request: Request) -> Session | None:
    """The current session (call after optional_user/current_user ran)."""
    return getattr(request.state, "session", None)


async def current_user(request: Request) -> User:
    """Dependency: the signed-in user, or 401 with a human error."""
    user = await optional_user(request)
    if user is None:
        raise sign_in_required()
    return user


def sign_in_required() -> HumanHTTPError:
    """The 401 every route without a session answers (also used by the body cap in app.Guard)."""
    return human(401, "Sign in to continue", "Your session has ended or this device isn't paired.",
                 "Sign in again.", [("Sign in", "login")])


def require(perm: Perm) -> Callable[..., Awaitable[User]]:
    """Dependency factory: `user: User = Depends(require("models.release"))` → 403 human error if missing."""

    async def dep(user: User = Depends(current_user)) -> User:
        if perm not in user.perms:
            raise human(403, "Not allowed from this session",
                        "This action needs more access than this session has. Nothing was changed.",
                        "Use an admin session on a desktop for this action.", tech={"permission": perm})
        return user

    return dep


def end_sessions(conn: sqlite3.Connection, *, token_hash_: str | None = None, device_id: str | None = None) -> int:
    """Delete sessions inside the caller's tx(); the caller also tells the hub to drop their streams."""
    if token_hash_:
        return conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash_,)).rowcount
    if device_id:
        return conn.execute("DELETE FROM sessions WHERE device_id=?", (device_id,)).rowcount
    return 0


def session_alive(th: str) -> bool:
    try:
        row = db.one("SELECT expires_at FROM sessions WHERE token_hash=?", (th,))
    except sqlite3.Error:
        return True         # storage hiccup: don't drop live viewers over it
    return row is not None and row["expires_at"] >= time.time()


# ── CSRF ─────────────────────────────────────────────────────────────────────────────────────

def _norm_host(netloc: str, scheme: str | None = None) -> str:
    netloc = netloc.strip().lower().rsplit("@", 1)[-1]
    for default, schemes in ((":443", ("https", None)), (":80", ("http", None))):
        if netloc.endswith(default) and scheme in schemes:
            return netloc[: -len(default)]
    return netloc


def csrf_problem(request: Request) -> str | None:
    """Why this mutation fails the CSRF check (None = it passes). Reasons are metric labels."""
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    if not cookie:
        return "no_cookie"
    if not header or not hmac.compare_digest(cookie.encode(), header.encode()):
        return "token_mismatch"
    source = request.headers.get("origin")
    if not source or source == "null":
        source = request.headers.get("referer")
    if not source:
        return "no_origin"
    parts = urlsplit(source)
    host = request.headers.get("host", "")
    if not parts.netloc or not host or _norm_host(parts.netloc, parts.scheme) != _norm_host(host):
        return "cross_origin"
    return None


def csrf_ok(request: Request) -> bool:
    """Header X-Labzilla-CSRF == cookie lz_csrf AND Origin/Referer host == Host."""
    return csrf_problem(request) is None


# ── rate limits ──────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    """Token bucket per key: up to `limit` hits in a burst, refilled at limit/window_sec per second.
    `hit(key)` → False when the bucket is empty. In-memory: one replica, and a restart forgiving
    everyone is acceptable for a LAN console."""

    MAX_KEYS = 10_000

    def __init__(self, limit: int, window_sec: float):
        self.limit, self.window_sec = limit, window_sec
        self._rate = limit / window_sec
        self._buckets: dict[str, tuple[float, float]] = {}     # key → (tokens, last refill)
        self._lock = threading.Lock()

    def _level(self, key: str, now: float) -> float:
        tokens, last = self._buckets.get(key, (float(self.limit), now))
        return min(float(self.limit), tokens + (now - last) * self._rate)

    def hit(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens = self._level(key, now)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            if len(self._buckets) > self.MAX_KEYS:
                self._prune(now)
            return True

    def retry_after(self, key: str) -> int:
        with self._lock:
            tokens = self._level(key, time.monotonic())
        return max(1, int((1.0 - tokens) / self._rate) + 1) if tokens < 1.0 else 0

    def _prune(self, now: float) -> None:
        full = [k for k in self._buckets if self._level(k, now) >= self.limit]
        for k in full:
            del self._buckets[k]
        while len(self._buckets) > self.MAX_KEYS:       # still flooded: forget the oldest
            del self._buckets[next(iter(self._buckets))]


class LoginBackoff:
    """Per-username lockout after repeated failures (independent of IP, so rotating addresses
    doesn't help a guesser): 5 free failures, then 30 s doubling up to 60 s. Success resets.

    The cap is short on purpose: the lock also refuses the right passphrase, and anyone who
    knows the admin's name can renew it with one wrong guess per window. A 60 s cap bounds that
    lockout to a minute's wait; scrypt plus the per-IP and global login limits bound guessing."""

    FREE, BASE, CAP = 5, 30.0, 60.0

    def __init__(self) -> None:
        self._fails: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def wait(self, name: str) -> int:
        with self._lock:
            _, until = self._fails.get(name.lower(), (0, 0.0))
        return max(0, int(until - time.monotonic() + 0.999))

    def failed(self, name: str) -> None:
        with self._lock:
            n, _ = self._fails.get(name.lower(), (0, 0.0))
            n += 1
            until = time.monotonic() + min(self.CAP, self.BASE * 2 ** (n - self.FREE - 1)) if n > self.FREE else 0.0
            self._fails[name.lower()] = (n, until)
            if len(self._fails) > RateLimiter.MAX_KEYS:
                del self._fails[next(iter(self._fails))]

    def succeeded(self, name: str) -> None:
        with self._lock:
            self._fails.pop(name.lower(), None)


class Limiters:
    """The console's limiters. One set per app (app.state.limiters), so tests get fresh buckets."""

    def __init__(self) -> None:
        self.login = RateLimiter(5, 60)            # per IP (brief §3)
        self.login_names = LoginBackoff()
        self.setup = RateLimiter(5, 60)
        self.pair_claim = RateLimiter(10, 60)      # per IP (brief §3)
        # Global ceilings (key GLOBAL), independent of the client address: a peer that can fake
        # X-Forwarded-For (any trusted proxy) gets a fresh per-IP bucket per request, not more attempts.
        self.login_all = RateLimiter(30, 60)
        self.setup_all = RateLimiter(10, 60)
        self.pair_claim_all = RateLimiter(30, 60)
        self.pair_start = RateLimiter(10, 60)      # per admin
        self.pair_status = RateLimiter(60, 60)     # per claim cookie: the phone polls every ~2 s while waiting
        self.mutations = RateLimiter(120, 60)      # every other non-GET /api request, per session (else per IP)
        self.ask = RateLimiter(20, 60)             # Ask messages per session: each one is stored and runs a model


GLOBAL = "*"
_fallback_limiters = Limiters()


def limiters(request: Request) -> Limiters:
    return getattr(request.app.state, "limiters", None) or _fallback_limiters


def check_rate(limiter: RateLimiter, key: str, name: str) -> None:
    """Raise a human 429 (with Retry-After) when `key` is over `limiter`."""
    if limiter.hit(key):
        return
    rate_limited.labels(name).inc()
    wait = limiter.retry_after(key)
    err = human(429, "Too many attempts", "Labzilla is slowing requests from this device to keep it safe.",
                f"Wait about {max(wait, 1)} seconds and try again.", [("Retry", "retry")], {"limit": name})
    err.headers = {"Retry-After": str(max(wait, 1))}
    raise err


# ── audit ────────────────────────────────────────────────────────────────────────────────────

def audit(user: User | None, action: str, target: str, detail: dict[str, Any] | None = None) -> None:
    """Record who did what to what: the audit table plus a structured log line. Never raises —
    a failed audit write must not turn a completed action into an error."""
    ip = client_ip_var.get()
    try:
        log.event(LOG, "audit", user=user.name if user else None, role=user.role if user else None,
                  device=user.device_name if user else None, ip=ip, action=action, target=target, detail=detail)
        with db.tx() as c:
            c.execute("INSERT INTO audit(ts, user_id, user_name, role, device_name, ip, action, target, detail_json)"
                      " VALUES(?,?,?,?,?,?,?,?,?)",
                      (time.time(), user.id if user else None, user.name if user else None,
                       user.role if user else None, user.device_name if user else None, ip, action, target,
                       json.dumps(detail, default=str) if detail else None))
    except Exception as e:      # storage down: the log line above is the record
        log.event(LOG, "audit_write_failed", error=type(e).__name__)


# ── user agents (for the devices list) ───────────────────────────────────────────────────────

def ua_summary(ua: str) -> str:
    """'Safari on iPhone' — a label for a person, not a fingerprint."""
    ua = ua or ""
    os_ = next((label for pat, label in (
        (r"iPhone", "iPhone"), (r"iPad", "iPad"), (r"Android", "Android"), (r"Windows", "Windows"),
        (r"Mac OS X|Macintosh", "macOS"), (r"CrOS", "ChromeOS"), (r"Linux", "Linux")) if re.search(pat, ua)), "")
    browser = next((label for pat, label in (
        (r"EdgA?/", "Edge"), (r"Firefox/|FxiOS/", "Firefox"), (r"OPR/", "Opera"),
        (r"SamsungBrowser/", "Samsung Internet"),
        (r"Chrome/|CriOS/", "Chrome"), (r"Safari/", "Safari")) if re.search(pat, ua)), "")
    if browser and os_:
        return f"{browser} on {os_}"
    return browser or os_ or "Unknown browser"
