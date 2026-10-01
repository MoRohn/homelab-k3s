"""LIF gateway — the only application-facing AI endpoint.

OpenAI-compatible: /v1/chat/completions, /v1/completions, /v1/embeddings, /v1/models.
Platform: /v1/health, /v1/capabilities, /v1/batch (proxied to the batch engine), /metrics.

Every response says which physical model served it and whether that was a fallback:
  non-streaming → a top-level `lif` object in the JSON body
  streaming     → X-LIF-* response headers, plus `lif` in the final usage chunk
Fallback is never hidden.

Request headers (all optional):
  X-LIF-Data-Class   PUBLIC | INTERNAL | CONFIDENTIAL | RESTRICTED (default CONFIDENTIAL)
  X-LIF-Workload     free-form tag for accounting (e.g. "bnn-stories")
  X-LIF-Priority     2..8 (spec priority class; default 2 interactive)
Body extension (optional): "lif": {"budget": {...}} — see policy.Budget.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict, defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from lif.common import config, log, metrics
from lif.decision.fabric import DecisionFabric
from lif.decision.providers import JevProvider
from lif.decision.rules import rules
from lif.decision.types import load_definitions
from lif.gpu.state import BlerbzState, GpuStateWatcher
from lif.policy import engine as policy
from lif.routing.router import AUTO_ALIAS, ROUTE_TO_ALIAS, NoRoute, Route, Router

LOG = log.get("lif.gateway")


# ── auth & rate limiting ──────────────────────────────────────────────────────

def load_keys() -> dict[str, str]:
    """key → client name, from `name:key` lines (Secret lif-gateway-keys)."""
    raw = config.secret("LIF_GATEWAY_KEYS") or ""
    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            name, key = line.split(":", 1)
            out[key.strip()] = name.strip()
    return out


class RateLimiter:
    def __init__(self, rpm: int):
        self.rate, self.cap = rpm / 60.0, float(rpm)
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, who: str) -> bool:
        now = time.monotonic()
        tokens, last = self.buckets.get(who, (self.cap, now))
        tokens = min(self.cap, tokens + (now - last) * self.rate)
        if tokens < 1:
            self.buckets[who] = (tokens, now)
            return False
        self.buckets[who] = (tokens - 1, now)
        return True


# ── Primary-workload bandwidth yield ──────────────────────────────────────────

class YieldLimiter:
    """Per-profile concurrency that shrinks while primary-workload production runs.
    GB10 CPU and GPU share one LPDDR5X bus; production LLM decode is bandwidth bound."""

    def __init__(self):
        self.active: dict[str, int] = defaultdict(int)
        self.cond = asyncio.Condition()

    def limit(self, profile_concurrency: int, state: BlerbzState) -> int:
        y = config.get("yield") or {}
        if state == BlerbzState.IMMINENT:
            return max(1, int(y.get("cpu_concurrency_blerbz", 1)))
        return max(1, min(profile_concurrency, int(y.get("cpu_concurrency_normal", 4))))

    @asynccontextmanager
    async def slot(self, profile: str, limit_fn, timeout: float, upstream_busy=None):
        """`upstream_busy` (async → int) is consulted only while the primary workload is IMMINENT: the
        model server's own in-flight count is shared truth across gateway replicas, so the
        production-time cap holds cluster-wide, not per replica."""
        deadline = time.monotonic() + timeout
        if upstream_busy is not None:
            while (busy := await upstream_busy()) >= limit_fn():
                if time.monotonic() >= deadline:
                    raise TimeoutError("queue timeout waiting for a model slot (primary-workload yield)")
                metrics.throttled.labels("cluster_yield_wait").inc()
                await asyncio.sleep(0.25)
        async with self.cond:
            while self.active[profile] >= limit_fn():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("queue timeout waiting for a model slot")
                metrics.throttled.labels("queued").inc()
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    pass
            self.active[profile] += 1
        metrics.inflight.labels(profile).inc()
        try:
            yield
        finally:
            metrics.inflight.labels(profile).dec()
            async with self.cond:
                self.active[profile] -= 1
                self.cond.notify_all()


async def _upstream_busy(endpoint: str) -> int:
    """llamacpp:requests_processing from the model server (0 if unreadable → fail-open is
    acceptable here: the per-replica cap still applies)."""
    try:
        r = await S.http.get(f"{endpoint}/metrics", timeout=2)
        for line in r.text.splitlines():
            if line.startswith("llamacpp:requests_processing"):
                return int(float(line.split()[-1]))
    except Exception:
        pass
    return 0


# ── deterministic tier: response cache for temperature-0 requests ─────────────

class ResponseCache:
    def __init__(self, size: int = 2000, ttl: float = 3600):
        self.size, self.ttl = size, ttl
        self.d: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    @staticmethod
    def key(profile: str, revision: str, body: dict) -> str | None:
        # Only explicitly deterministic requests: non-streaming with temperature == 0.
        if body.get("stream") or body.get("temperature") is None or float(body["temperature"]) != 0.0:
            return None
        clean = {k: v for k, v in body.items() if k not in ("lif", "user", "stream_options")}
        return hashlib.sha256(json.dumps([profile, revision, clean], sort_keys=True).encode()).hexdigest()

    def get(self, k: str | None) -> dict | None:
        if not k or k not in self.d:
            return None
        ts, v = self.d[k]
        if time.time() - ts > self.ttl:
            self.d.pop(k, None)
            return None
        self.d.move_to_end(k)
        return v

    def put(self, k: str | None, v: dict) -> None:
        if not k:
            return
        self.d[k] = (time.time(), v)
        while len(self.d) > self.size:
            self.d.popitem(last=False)


# ── app ──────────────────────────────────────────────────────────────────────

class State:
    def __init__(self):
        self.router = Router(controller_url=os.environ.get("LIF_CONTROLLER_URL"))
        self.gpu = GpuStateWatcher()
        self.keys = load_keys()
        self.ratelimit = RateLimiter(int(config.get("gateway.rate_limit_rpm", 600)))
        self.yielder = YieldLimiter()
        self.cache = ResponseCache()
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(float(config.get("gateway.request_timeout_sec", 300)),
                                                            connect=3))
        self.fabric = DecisionFabric(load_definitions(), rules, jev=JevProvider(config.secret("TYPE_SAFE_JEV_API_KEY")))
        self.batch_url = os.environ.get("LIF_BATCH_URL", "http://batch.ai-system.svc:8080")
        self.started = time.time()
        self.tasks: list[asyncio.Task] = []


S: State


async def _loop(fn, every: float):
    while True:
        try:
            await fn()
        except Exception:
            LOG.exception("background loop failed")
        await asyncio.sleep(every)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global S
    log.setup()
    S = State()
    await S.router.probe_all()
    await S.gpu.refresh()
    async def refresh():
        await S.router.refresh_table()
        if S.router.jev_enabled is not None:
            S.fabric.jev_enabled_override = S.router.jev_enabled
    S.tasks = [asyncio.create_task(_loop(S.router.probe_all, 10)),
               asyncio.create_task(_loop(refresh, 15)),
               asyncio.create_task(S.gpu.run())]
    yield
    for t in S.tasks:
        t.cancel()
    await S.http.aclose()


app = FastAPI(title="LIF gateway", lifespan=lifespan)
PUBLIC_PATHS = {"/v1/health", "/metrics", "/healthz", "/readyz"}


@app.middleware("http")
async def auth_mw(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    client = S.keys.get(key)
    if client is None:
        return JSONResponse({"error": {"message": "missing or invalid API key", "type": "auth"}}, status_code=401)
    if not S.ratelimit.allow(client):
        return JSONResponse({"error": {"message": "rate limit exceeded", "type": "rate_limit"}}, status_code=429)
    cl = int(request.headers.get("content-length") or 0)
    if cl > int(config.get("gateway.max_body_bytes", 4 << 20)):
        return JSONResponse({"error": {"message": "request body too large", "type": "invalid_request"}},
                            status_code=413)
    request.state.client = client
    return await call_next(request)


def _err(status: int, msg: str, typ: str = "invalid_request", **extra) -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": typ, **extra}}, status_code=status)


# ── health / capabilities / models ───────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    # Ready = can serve at least one text alias with a verified-healthy model.
    ok = any(S.router.healthy(p) for p in S.router.profiles)
    return JSONResponse({"ready": ok}, status_code=200 if ok else 503)


@app.get("/v1/health")
async def health():
    snap = S.gpu.current()
    aliases = S.router.alias_status(_imminent())
    useful = aliases.get("local/fast", {}).get("available") or aliases.get("local/default", {}).get("available")
    return {"status": "ok" if useful else "degraded", "gateway": "ready", "useful_local_ai": bool(useful),
            "blerbz": {"state": snap.state.name, "reason": snap.reason},
            "decision_fabric": {"jev_enabled": S.fabric._jev_enabled(),
                                "jev_breaker_open": S.fabric.jev.breaker.open},
            "routing_table": S.router.table_source, "uptime_sec": round(time.time() - S.started)}


@app.get("/v1/capabilities")
async def capabilities():
    snap = S.gpu.current()
    return {"aliases": S.router.alias_status(_imminent()),
            "profiles": {n: {"endpoint_healthy": S.router.healthy(n), "params_b": p.params_b, "device": p.device,
                             "category": p.category, "context": p.context,
                             "model": f"{p.hf_repo}@{p.revision}", "last_error": S.router.health[n].last_error}
                         for n, p in S.router.profiles.items()},
            "blerbz": snap.to_dict(),
            "yield": {"cpu_concurrency": S.yielder.limit(4, snap.state)},
            "decision_fabric": S.fabric.status()}


@app.get("/v1/models")
async def models():
    st = S.router.alias_status(_imminent())
    data = [{"id": a, "object": "model", "owned_by": "lif", "created": int(S.started),
             "lif": st.get(a, {})} for a in sorted(set(S.router.aliases) | {AUTO_ALIAS})]
    return {"object": "list", "data": data}


@app.get("/metrics")
async def prom():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── inference ────────────────────────────────────────────────────────────────

def _imminent() -> bool:
    return S.gpu.current().state == BlerbzState.IMMINENT


def _auth(route: Route) -> dict:
    if route.profile.auth_secret:
        return {"Authorization": f"Bearer {config.secret(route.profile.auth_secret) or ''}"}
    return {}


def _last_user(body: dict) -> str:
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return body.get("prompt", "") if isinstance(body.get("prompt"), str) else ""


async def _route(body: dict, request: Request) -> Route:
    requested = body.get("model") or "local/default"
    if requested != AUTO_ALIAS:
        return S.router.resolve(requested, blerbz_imminent=_imminent())
    text = _last_user(body)
    d = await S.fabric.evaluate("request-route",
                                {"last_user": text[:6000], "prompt_tokens": len(text) // 4,
                                 "structured_output": bool(body.get("response_format"))},
                                data_class=request.headers.get("x-lif-data-class"))
    choice = d.decision if d.action in (policy.Gate.AUTO, policy.Gate.VALIDATE) else "default"
    return S.router.resolve(ROUTE_TO_ALIAS[choice], requested=AUTO_ALIAS, blerbz_imminent=_imminent(),
                            route_decision={"decision": d.decision, "confidence": round(d.confidence, 3),
                                            "provider": d.provider, "action": d.action})


def _prepare(body: dict, route: Route, state: BlerbzState) -> dict:
    out = {k: v for k, v in body.items() if k != "lif"}
    out["model"] = route.profile.name
    if route.profile.chat_template_kwargs and "chat_template_kwargs" not in out:
        out["chat_template_kwargs"] = route.profile.chat_template_kwargs
    if state == BlerbzState.IMMINENT:
        cap = int((config.get("yield") or {}).get("max_tokens_blerbz", 512))
        mt = out.get("max_tokens") or out.get("max_completion_tokens")
        if mt is None or int(mt) > cap:
            out["max_tokens"] = cap
            out.pop("max_completion_tokens", None)
            metrics.throttled.labels("max_tokens_clamped").inc()
    if out.get("stream"):
        out["stream_options"] = {**(out.get("stream_options") or {}), "include_usage": True}
    return out


def _tier(route: Route) -> str:
    return "large_local" if route.profile.params_b >= metrics.HEAVY_PARAMS_B else "fast_local"


def _account(route: Route, usage: dict, workload: str, seconds: float) -> None:
    pin, pout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    metrics.tokens.labels(route.profile.name, "input").inc(pin)
    metrics.tokens.labels(route.profile.name, "output").inc(pout)
    metrics.tasks.labels(workload, _tier(route)).inc()
    c = config.get("cost.external_equivalent_usd_per_mtok") or {}
    metrics.cost_avoided.labels(_tier(route)).inc(pin * float(c.get("input", 0)) / 1e6 +
                                                  pout * float(c.get("output", 0)) / 1e6)
    if route.fallback:
        metrics.fallbacks.labels(route.requested, route.profile.name,
                                 (route.reason or "fallback").split(":")[0][:40]).inc()


async def _proxy(request: Request, path: str, endpoint_name: str):
    try:
        body = await request.json()
    except Exception:
        return _err(400, "body must be JSON")
    workload = request.headers.get("x-lif-workload", getattr(request.state, "client", "unknown"))
    requested = body.get("model") or "local/default"
    t0 = time.perf_counter()
    snap = S.gpu.current()
    try:
        route = await _route(body, request)
    except NoRoute as e:
        metrics.requests.labels(endpoint_name, requested, "503").inc()
        metrics.tasks.labels(workload, "rejected").inc()
        return _err(503, e.reason, "capacity", lif={"requested": requested, "blerbz": snap.state.name})

    budget = policy.Budget.from_request((body.get("lif") or {}).get("budget"))
    if budget.max_latency_ms and snap.state == BlerbzState.IMMINENT and route.profile.params_b >= 4:
        # production is running and the caller is latency-sensitive: prefer the smaller model
        try:
            alt = S.router.resolve("local/instant", requested=route.requested, blerbz_imminent=True)
            alt.reason = "latency budget during primary-workload production"
            route = alt
        except NoRoute:
            pass

    exclude: set[str] = set()
    while True:
        upstream = _prepare(body, route, snap.state)
        ck = S.cache.key(route.profile.name, route.profile.revision, upstream) if path != "/v1/embeddings" else None
        hit = S.cache.get(ck)
        if hit is not None:
            metrics.tasks.labels(workload, "deterministic").inc()
            metrics.requests.labels(endpoint_name, requested, "200").inc()
            return JSONResponse({**hit, "lif": {**route.meta(), "cache": "hit"}})
        limit = lambda: S.yielder.limit(route.profile.concurrency, S.gpu.current().state)
        ep = route.profile.endpoint
        busy = (lambda: _upstream_busy(ep)) if (S.gpu.current().state == BlerbzState.IMMINENT
                                                and route.profile.device == "cpu") else None
        try:
            if upstream.get("stream"):
                return await _stream(route, path, upstream, limit, workload, endpoint_name, requested, t0, busy)
            async with S.yielder.slot(route.profile.name, limit, timeout=120, upstream_busy=busy):
                r = await S.http.post(f"{route.profile.endpoint}{path}", json=upstream, headers=_auth(route))
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"upstream {r.status_code}", request=r.request, response=r)
        except TimeoutError as e:
            metrics.requests.labels(endpoint_name, requested, "429").inc()
            return _err(429, str(e), "capacity", lif=route.meta())
        except (httpx.HTTPError, httpx.HTTPStatusError) as e:
            S.router.mark_failure(route.profile.name, str(e))
            exclude.add(route.profile.name)
            try:
                nxt = S.router.resolve(route.alias, requested=route.requested, exclude=exclude,
                                       blerbz_imminent=_imminent())
                nxt.fallback, nxt.reason = True, f"upstream_error on {route.profile.name}: {str(e)[:120]}"
                route = nxt
                continue
            except NoRoute as nr:
                metrics.requests.labels(endpoint_name, requested, "503").inc()
                return _err(503, nr.reason, "capacity", lif={"requested": requested})
        break

    S.router.mark_success(route.profile.name)
    if r.status_code != 200:
        metrics.requests.labels(endpoint_name, requested, str(r.status_code)).inc()
        return Response(r.content, status_code=r.status_code, media_type="application/json")
    data = r.json()
    dt = time.perf_counter() - t0
    usage = data.get("usage") or {}
    _account(route, usage, workload, dt)
    tm = data.pop("timings", None) or {}
    if tm.get("predicted_per_second"):
        metrics.decode_tps.labels(route.profile.name).observe(tm["predicted_per_second"])
    data["model"] = route.requested
    S.cache.put(ck, data)
    data["lif"] = {**route.meta(), "blerbz": snap.state.name, "latency_ms": round(dt * 1000, 1)}
    metrics.requests.labels(endpoint_name, requested, "200").inc()
    metrics.request_latency.labels(endpoint_name, requested).observe(dt)
    return JSONResponse(data)


async def _stream(route: Route, path: str, upstream: dict, limit, workload: str, endpoint_name: str,
                  requested: str, t0: float, busy=None) -> StreamingResponse:
    # Acquire the slot and open the upstream BEFORE returning, so connection errors can
    # still fall back; once bytes flow to the client the route is committed.
    cm = S.yielder.slot(route.profile.name, limit, timeout=120, upstream_busy=busy)
    await cm.__aenter__()
    try:
        req = S.http.build_request("POST", f"{route.profile.endpoint}{path}", json=upstream, headers=_auth(route))
        resp = await S.http.send(req, stream=True)
        if resp.status_code >= 500:
            await resp.aclose()
            raise httpx.HTTPStatusError(f"upstream {resp.status_code}", request=req, response=resp)
    except BaseException:
        await cm.__aexit__(None, None, None)
        raise
    meta = route.meta()

    async def gen() -> AsyncIterator[bytes]:
        first = True
        usage: dict = {}
        try:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        chunk = json.loads(line[6:])
                    except json.JSONDecodeError:
                        yield (line + "\n\n").encode()
                        continue
                    if first and any((c.get("delta") or {}).get("content") or c.get("text")
                                     for c in chunk.get("choices") or []):
                        metrics.ttft.labels(route.requested, route.profile.name).observe(time.perf_counter() - t0)
                        first = False
                    chunk["model"] = route.requested
                    chunk.pop("timings", None)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                        chunk["lif"] = meta
                    yield f"data: {json.dumps(chunk)}\n\n".encode()
                else:
                    yield (line + "\n\n").encode()
            S.router.mark_success(route.profile.name)
            metrics.requests.labels(endpoint_name, requested, "200").inc()
        except httpx.HTTPError as e:
            S.router.mark_failure(route.profile.name, str(e))
            metrics.requests.labels(endpoint_name, requested, "502").inc()
            yield f"data: {json.dumps({'error': {'message': 'upstream stream failed', 'type': 'upstream'}})}\n\n".encode()
        finally:
            await resp.aclose()
            await cm.__aexit__(None, None, None)
            dt = time.perf_counter() - t0
            _account(route, usage, workload, dt)
            metrics.request_latency.labels(endpoint_name, requested).observe(dt)

    headers = {"X-LIF-Served-By": route.profile.name, "X-LIF-Fallback": str(route.fallback).lower(),
               "X-LIF-Degraded": str(route.degraded).lower(), "X-LIF-Requested": route.requested}
    if route.reason:
        headers["X-LIF-Reason"] = route.reason[:200]
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@app.post("/v1/chat/completions")
async def chat(request: Request):
    return await _proxy(request, "/v1/chat/completions", "chat")


@app.post("/v1/completions")
async def completions(request: Request):
    return await _proxy(request, "/v1/completions", "completions")


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    return await _proxy(request, "/v1/embeddings", "embeddings")


# ── batch (thin proxy; the batch engine owns durability) ─────────────────────

async def _batch_proxy(request: Request, method: str, path: str):
    try:
        content = await request.body()
        r = await S.http.request(method, f"{S.batch_url}{path}", content=content,
                                 headers={"content-type": "application/json",
                                          "x-lif-client": getattr(request.state, "client", "unknown"),
                                          "x-lif-data-class": request.headers.get("x-lif-data-class", "")},
                                 timeout=15)
        return Response(r.content, status_code=r.status_code, media_type="application/json")
    except httpx.HTTPError as e:
        return _err(503, f"batch engine unavailable: {e}", "capacity")


@app.post("/v1/batch")
async def batch_create(request: Request):
    return await _batch_proxy(request, "POST", "/v1/batch")


@app.get("/v1/batch")
async def batch_list(request: Request):
    return await _batch_proxy(request, "GET", "/v1/batch")


@app.get("/v1/batch/{bid}")
async def batch_get(bid: str, request: Request):
    return await _batch_proxy(request, "GET", f"/v1/batch/{bid}")


@app.get("/v1/batch/{bid}/results")
async def batch_results(bid: str, request: Request):
    return await _batch_proxy(request, "GET", f"/v1/batch/{bid}/results")


@app.delete("/v1/batch/{bid}")
async def batch_cancel(bid: str, request: Request):
    return await _batch_proxy(request, "DELETE", f"/v1/batch/{bid}")
