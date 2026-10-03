"""Labzilla Console BFF: one stable endpoint for the PWA (spec §75–§77).

Serves the built Preact app from settings.ui_dir() and the /api surface (routes/*), owns sessions and
CSRF (auth.py), and streams live state from one poller over /api/events (events.py). Upstream keys
stay server-side; the browser only ever talks to this process.

Run: uvicorn lif.console.app:app --port 8090 (dev) — the image runs it on :8080.

Request path: `Guard` (pure ASGI, so SSE and Ask streams pass through untouched) checks CSRF and the
mutation rate limit on /api, then stamps security headers on every response, `no-store` on /api,
and issues the `lz_csrf` cookie on GET responses that arrive without one (auth.py explains why that
removes the need for CSRF exemptions). Unknown non-/api GETs fall through to the SPA via the 404
handler, so routes added later are never shadowed; unknown /api paths answer with a human error.
"""
from __future__ import annotations

import asyncio
import inspect
import mimetypes
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lif.common import log
from lif.console import auth, db, errors, events, poller, settings, upstream
from lif.console.routes import agents, ai, home, jobs, knowledge, models, system
from lif.console.routes import auth as auth_routes

LOG = log.get("lif.console")

ROUTERS = (auth_routes.router, events.router, system.router, models.router, ai.router, jobs.router,
           agents.router, knowledge.router, home.router)

# Python's table doesn't know the PWA manifest; served as text/plain, some browsers ignore it (no install).
mimetypes.add_type("application/manifest+json", ".webmanifest")

# Long-lived caching only for content-hashed build output; the shell files must revalidate so a
# deploy is picked up on the next load (the service worker also fetches index.html network-first).
_NO_CACHE = {"index.html", "sw.js", "manifest.webmanifest"}

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("Permissions-Policy", "microphone=(self), camera=()"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)

# Bounded label set for lif_console_requests_total (never raw paths: ids would explode cardinality).
_API_AREAS = frozenset({"v1", "auth", "setup", "access", "pair", "devices", "events", "system", "models", "ai",
                        "command", "jobs", "agents", "approvals", "knowledge", "home", "trust"})

requests_total = Counter("lif_console_requests_total", "Console HTTP requests by route class and status class",
                         ["route", "status"])

_NOT_BUILT = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Labzilla</title><body style="font-family:system-ui;background:#07110f;color:#e7f0ed;padding:2rem">
<h1>Labzilla console UI is not built</h1><p>The API is running, but no UI was found in the configured
directory. Build it with <code>npm run build</code> in <code>lif/apps/console</code>, or set LIF_CONSOLE_UI_DIR.</p>
</body></html>"""


_FILE_DIRS = frozenset({"assets", "icons", "logo"})   # top-level dirs of real files in dist/, never client routes


def _static(path: str) -> Response:
    """Serve a built file, or index.html for client-side routes (/models/roles/fast…)."""
    root = settings.ui_dir().resolve()
    index = root / "index.html"
    if not index.is_file():
        return HTMLResponse(_NOT_BUILT, status_code=503, headers={"Cache-Control": "no-store"})
    target = (root / path.lstrip("/")).resolve() if path.strip("/") else index
    if not target.is_relative_to(root) or not target.is_file():
        # A missing asset must be a 404, never index.html: after a redeploy an old tab asks for a chunk hash that is
        # gone, and HTML served as JavaScript fails with a MIME error instead of the client noticing and reloading.
        parts = [p for p in path.split("/") if p]
        if parts and (parts[0] in _FILE_DIRS or (len(parts) == 1 and "." in parts[0])):
            return Response("Not found", status_code=404, media_type="text/plain", headers={"Cache-Control": "no-store"})
        target = index
    if target.name in _NO_CACHE or target == index:
        cache = "no-cache"
    elif "assets" in target.relative_to(root).parts:
        cache = "public, max-age=31536000, immutable"
    else:
        cache = "public, max-age=3600"
    headers = {"Cache-Control": cache}
    return FileResponse(target, headers=headers)


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def route_class(path: str) -> str:
    if _is_api(path):
        area = path.split("/")[2] if path.count("/") >= 2 else ""
        return f"api_{area}" if area in _API_AREAS else "api_other"
    if path in ("/healthz", "/readyz", "/metrics"):
        return "probe"
    return "assets" if path.startswith("/assets/") else "spa"


class Guard:
    """CSRF + mutation rate limit for /api, response headers and the CSRF cookie for everything."""

    def __init__(self, app: ASGIApp):
        self.app = app
        self._intake: asyncio.Semaphore | None = None   # big Ask bodies being read and parsed (see ASK_INTAKE_SLOTS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path, method = scope["path"], scope["method"]
        api = _is_api(path)
        ip_token = auth.client_ip_var.set(auth.client_ip(request))
        status = 500
        issue_csrf = method in ("GET", "HEAD") and auth.CSRF_COOKIE not in request.cookies
        intake: asyncio.Semaphore | None = None

        def release_intake() -> None:
            nonlocal intake
            if intake is not None:
                intake.release()
                intake = None

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                release_intake()            # the body has been read and parsed by now
                status = message["status"]
                headers = MutableHeaders(scope=message)
                for k, v in SECURITY_HEADERS:
                    if k not in headers:
                        headers[k] = v
                if api:
                    headers["Cache-Control"] = "no-store"
                if issue_csrf and not any(v.startswith(f"{auth.CSRF_COOKIE}=")
                                          for v in headers.getlist("set-cookie")):
                    headers.append("set-cookie", auth.csrf_cookie_header())
            await send(message)

        try:
            if api and method not in auth.SAFE_METHODS:
                problem = auth.csrf_problem(request)
                if problem is not None:
                    auth.csrf_rejected.labels(problem).inc()
                    await _csrf_refusal(problem)(scope, receive, send_with_headers)
                    return
                try:
                    limit, signed_in = body_limit(path), True
                    if path not in OPEN_AUTH:     # those have their own limits; logout is a no-op without a session
                        key, signed_in = await _rate_key(request)
                        auth.check_rate(auth.limiters(request).mutations, key, "mutations")
                        if signed_in and is_ask(path):
                            # Before the (up to 4 MB) body is read and parsed: a refused Ask costs no memory.
                            auth.check_rate(auth.limiters(request).ask, key, "ask")
                            request.state.ask_rate_checked = True
                    if not signed_in:             # the route answers 401: don't read a big body for it,
                        limit = AUTH_BODY_MAX     # and say "sign in", not "send less"
                    refuse = partial(_too_large, limit) if signed_in else auth.sign_in_required
                    declared = _declared_length(request)
                    if declared > limit:
                        raise refuse()
                    chunked = request.headers.get("content-length") is None
                    if signed_in and is_ask(path) and (chunked or declared > ASK_INTAKE_FROM):
                        intake = await self._take_intake()
                    receive = await _capped(receive, request, limit, refuse)
                except errors.HumanHTTPError as exc:
                    release_intake()
                    await errors.response(exc)(scope, receive, send_with_headers)
                    return
            await self.app(scope, receive, send_with_headers)
        finally:
            release_intake()
            requests_total.labels(route_class(path), f"{status // 100}xx").inc()
            auth.client_ip_var.reset(ip_token)

    async def _take_intake(self) -> asyncio.Semaphore:
        """One of ASK_INTAKE_SLOTS, held from before the body is read until the response starts, so
        concurrent big Ask bodies (several sessions, each within its Ask limit) can't add up past the
        pod's memory limit while they are buffered and parsed."""
        if self._intake is None:
            self._intake = asyncio.Semaphore(ASK_INTAKE_SLOTS)
        try:
            await asyncio.wait_for(self._intake.acquire(), ASK_INTAKE_WAIT_SEC)
        except TimeoutError:
            raise errors.human(503, "Labzilla is busy taking other messages", "Nothing was sent.",
                               "Try again in a moment.", [("Retry", "retry")], {"limit": "ask_intake"}) from None
        return self._intake


# Open (no session needed) mutating endpoints: each has its own per-IP and global limit (routes/auth.py).
OPEN_AUTH = frozenset({"/api/auth/login", "/api/auth/logout", "/api/setup", "/api/pair/claim"})

# Request body caps, checked before anything parses the body (FastAPI reads the whole JSON first).
# Ask: 512 KiB prompt + 1 MiB of attachment text (routes/ai.py) with room for JSON escaping; the
# gateway refuses bodies over 4 MiB anyway (gateway.max_body_bytes).
AUTH_BODY_MAX = 4 * 1024
ASK_BODY_MAX = 4 * 1024 * 1024
TEXT_BODY_MAX = 1024 * 1024             # Save to Knowledge (a whole answer)
BODY_MAX = 64 * 1024
# Ask bodies over ASK_INTAKE_FROM (or chunked) are read and parsed at most ASK_INTAKE_SLOTS at a time;
# a request that can't get a slot within ASK_INTAKE_WAIT_SEC gets a human 503.
ASK_INTAKE_FROM = 256 * 1024
ASK_INTAKE_SLOTS = 3
ASK_INTAKE_WAIT_SEC = 30.0


def is_ask(path: str) -> bool:
    """POST /api/ai/threads/{id}/messages (not …/messages/{id}/cancel)."""
    return path.startswith("/api/ai/threads/") and path.endswith("/messages")


def body_limit(path: str) -> int:
    if path in OPEN_AUTH:
        return AUTH_BODY_MAX
    if is_ask(path):
        return ASK_BODY_MAX
    if path == "/api/knowledge/notes":
        return TEXT_BODY_MAX
    return BODY_MAX


def _declared_length(request: Request) -> int:
    try:
        return int(request.headers.get("content-length") or 0)
    except ValueError:
        return 0


def _too_large(limit: int) -> errors.HumanHTTPError:
    kib = limit // 1024
    size = f"{kib // 1024} MB" if kib >= 1024 else f"{kib} KB"
    return errors.human(413, "That's too much to send at once", "Nothing was sent or changed.",
                        f"Keep it under {size}: shorten the text or attach fewer files.", tech={"limit_bytes": limit})


async def _capped(receive: Receive, request: Request, limit: int,
                  refuse: Callable[[], errors.HumanHTTPError]) -> Receive:
    """Without a Content-Length (chunked), read the body up to `limit` first, then replay it, so an
    oversized body is refused (`refuse()`) before it is parsed. With one, the server already enforces it."""
    if request.headers.get("content-length") is not None:
        return receive
    body = bytearray()
    more = True
    while more:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        body += msg.get("body", b"")
        if len(body) > limit:
            raise refuse()
        more = bool(msg.get("more_body"))
    replay: list[Message] = [{"type": "http.request", "body": bytes(body), "more_body": False}]

    async def replayed() -> Message:
        return replay.pop() if replay else await receive()
    return replayed


async def _rate_key(request: Request) -> tuple[str, bool]:
    """(limiter key, signed in). Signed-in requests are limited per session: behind SNAT every LAN
    client reaches Labzilla from one address, so a per-IP bucket would let anyone throttle everyone
    (CONSOLE.md, Security model)."""
    await auth.optional_user(request)       # cached on request.state, so the route reuses it. Storage down:
    sess = auth.session_of(request)         # its 503 propagates (every route here needs a session anyway)
    if sess is not None:
        return f"s:{sess.token_hash}", True
    return auth.client_ip_var.get() or "", False


def _csrf_refusal(problem: str) -> JSONResponse:
    if problem == "no_cookie":
        err = errors.human(403, "This page needs a refresh", "Nothing was changed: Labzilla couldn't confirm "
                           "the request came from this page.", "Reload the page and try again.",
                           [("Reload", "reload")], {"csrf": problem})
    else:
        err = errors.human(403, "Request blocked for safety", "Nothing was changed: the request didn't come "
                           "from a Labzilla page on this address.", "Reload Labzilla and try again from there.",
                           [("Reload", "reload")], {"csrf": problem})
    return errors.response(err)


async def _maybe(fn: Callable[..., Any], *args: Any, what: str, wait: float = 5.0) -> asyncio.Task[Any] | None:
    """Call a lifecycle hook owned by another module without letting it break or hang the app.
    A stub (NotImplementedError) is fine; a coroutine still running after `wait` keeps running as a
    task, which is returned so the caller holds a strong reference and can cancel it at shutdown."""
    try:
        res = fn(*args)
        if inspect.isawaitable(res):
            task = asyncio.ensure_future(res)
            done, _ = await asyncio.wait({task}, timeout=wait)
            if task in done:
                task.result()
            else:
                log.event(LOG, "lifecycle_background", hook=what)
                return task
    except NotImplementedError:
        log.event(LOG, "lifecycle_not_implemented", hook=what)
    except Exception as e:
        log.event(LOG, "lifecycle_failed", hook=what, error=type(e).__name__, detail=str(e)[:200])
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        db.init()
    except Exception as e:      # unwritable /data etc.: keep serving; /readyz says so, routes answer 503
        log.event(LOG, "db_init_failed", path=str(settings.db_path()), error=type(e).__name__)
    takes_app = bool(inspect.signature(poller.start).parameters)
    app.state.poller_task = await _maybe(poller.start, *((app,) if takes_app else ()), what="poller.start")
    try:
        yield
    finally:
        events.hub.close()
        await _maybe(poller.stop, what="poller.stop")
        task = app.state.poller_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await _maybe(upstream.close, what="upstream.close")


def settings_flag(name: str) -> bool:
    import os
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def create_app() -> FastAPI:
    app = FastAPI(title="Labzilla Console", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.limiters = auth.Limiters()
    app.add_middleware(Guard)
    errors.install(app)
    for r in ROUTERS:
        app.include_router(r)
    if settings_flag("LIF_CONSOLE_UNDERSTANDING"):
        # Explanation API (lif/understanding/api.py) behind the console session; off unless enabled.
        from lif.understanding import api as understanding_api
        app.include_router(understanding_api.make_router(understanding_api.compiler,
                                                         [Depends(auth.require("ask"))], prefix="/api"))

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Response:
        ok = await asyncio.to_thread(db.ok)
        return JSONResponse({"status": "ok" if ok else "db unavailable"}, status_code=200 if ok else 503)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        # Prometheus scrapes the pod directly. Anything that came through Traefik carries
        # X-Forwarded-For: LAN browsers have no business with the process's counters.
        if "x-forwarded-for" in request.headers:
            return Response("Not found", status_code=404, media_type="text/plain")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        # The SPA lives in the 404 path rather than a catch-all route, so routes added later (by any
        # owner, in any order) are never shadowed. /api/* always answers with a human error body.
        if exc.status_code == 404 and request.method in ("GET", "HEAD") and not _is_api(request.url.path):
            return _static(request.url.path)
        human = errors.generic(exc.status_code, exc.detail)
        return JSONResponse(errors.body(human.error), status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    return app


app = create_app()
