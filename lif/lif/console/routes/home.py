"""Home cockpit: GET /api/home (spec §6) — everything Home shows beyond the status block, in one call.

| Part | Source | Cost |
|---|---|---|
| ask, threads | the console's SQLite (this user's Ask history, last 24 h) | local, per request |
| availability | poller snapshot (controller /v1/overview, already cached) | none |
| value | controller /v1/savings?window=24h (counts measured, $ ESTIMATES) | cached 120 s |
| trends | Prometheus range queries, last 6 h at 5-min steps | cached 60 s |

Shared upstream parts are cached and concurrent viewers share one in-flight fetch, so a room full of open
dashboards costs Prometheus one set of queries a minute. A part whose source is down comes back empty with a
plain reason (`value_note`, `trends_note`); it never blanks the page or invents a 0.
"""
from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends

from lif.common import config
from lif.console import auth, db, poller, threads, upstream
from lif.console.contracts import AskStats, Availability, HomeOverview, Trend, User, ValueStats
from lif.console.upstream import UpstreamError

router = APIRouter(prefix="/api", tags=["home"])

WINDOW_SEC = 24 * 3600
TREND_HOURS = 6
TREND_STEP = 300
RECENT_THREADS = 6

# (key, label, unit, PromQL, threshold, threshold label, drill-down)
TRENDS: list[tuple[str, str, str, str, float | None, str | None, str]] = [
    # One ratio over all model servers, not per pod (pods restart; per-pod series break). 0/0 = no traffic = gap.
    ("decode", "Answer speed", "tok/s",
     "sum(rate(llamacpp:tokens_predicted_total[10m])) / sum(rate(llamacpp:tokens_predicted_seconds_total[10m]))",
     None, None, "/models"),
    ("ttft", "First word (p90)", "s",
     "histogram_quantile(0.9, sum by (le) (rate(lif_ttft_seconds_bucket[15m])))", None, None, "/models"),
    ("requests", "AI requests", "req/min", "sum(rate(lif_requests_total[5m])) * 60", None, None, "/system/logs"),
    ("mem_free", "Free memory", "GB", "max(gpusched_mem_available_mib) / 1024", 8.0, "8 GB safety margin",
     "/system/compute"),
    ("gpu", "DGX GPU load", "%", "max(gpusched_gpu_util_percent)", None, None, "/system/compute"),
]

_cache: dict[str, tuple[float, Any]] = {}
_inflight: dict[str, asyncio.Future[Any]] = {}


async def _cached(key: str, ttl: float, fn: Callable[[], Awaitable[Any]]) -> Any:
    """TTL cache with one shared in-flight fetch per key. `fn` must not raise (it returns its own note)."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    if key in _inflight:
        return await asyncio.shield(_inflight[key])
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        val = await fn()
    except asyncio.CancelledError:
        fut.cancel()                        # release the callers waiting on this fetch, never leave them hanging
        raise
    except Exception as e:
        fut.set_exception(e)
        fut.exception()                     # retrieved here, so no "never retrieved" warning without waiters
        raise
    else:
        _cache[key] = (time.time(), val)
        fut.set_result(val)
        return val
    finally:
        if _inflight.get(key) is fut:
            _inflight.pop(key, None)


# ── parts ─────────────────────────────────────────────────────────────────────────────────────

def _count(v: Any) -> int:
    """A stored token count, or 0 when an old or damaged receipt holds something else."""
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(q * len(s)))], 1)


def ask_stats(user_id: str, now: float) -> AskStats:
    rows = db.q("SELECT m.status, m.receipt_json FROM messages m JOIN threads t ON t.id = m.thread_id "
                "WHERE t.user_id=? AND m.role='assistant' AND m.created_at>=?", (user_id, now - WINDOW_SEC))
    st = AskStats(window_hours=WINDOW_SEC // 3600)
    lat: list[float] = []
    for r in rows:
        if r["status"] == "error":
            st.failed += 1
        if r["status"] != "done":
            continue
        st.answers += 1
        try:
            rec = json.loads(r["receipt_json"] or "{}")
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if isinstance(rec.get("latency_ms"), (int, float)):
            lat.append(float(rec["latency_ms"]))
        tok = rec.get("tokens") if isinstance(rec.get("tokens"), dict) else {}
        st.prompt_tokens += _count(tok.get("prompt"))
        st.completion_tokens += _count(tok.get("completion"))
    st.median_latency_ms = round(statistics.median(lat), 1) if lat else None
    st.p90_latency_ms = _pct(lat, 0.9)
    return st


def availability() -> Availability:
    """The controller reports availability per probed capability ({capability: {availability, samples, …}});
    the 95 % target is about `useful_local_ai` (the fast or default model answered)."""
    ov = poller.snapshot().raw.get("overview")
    ov = ov if isinstance(ov, dict) else {}

    def useful(window: Any) -> float | None:
        cap = window.get("useful_local_ai") if isinstance(window, dict) else None
        v = cap.get("availability") if isinstance(cap, dict) else None
        return float(v) if isinstance(v, (int, float)) else None

    t = config.get("availability_target")
    return Availability(last_24h=useful(ov.get("availability_24h")), last_7d=useful(ov.get("availability_7d")),
                        target=float(t) if isinstance(t, (int, float)) else None)


async def _value() -> tuple[ValueStats | None, str | None]:
    try:
        s = await upstream.get("controller", "/v1/savings", params={"window": "24h"}, timeout=6.0)
    except UpstreamError as e:
        return None, f"{upstream.reason(e)}, so today's usage and value can't be shown."
    m, k, est = s.get("measured") or {}, s.get("kpi") or {}, s.get("estimates_usd") or {}
    toks = m.get("local_tokens") or {}
    return ValueStats(requests=m.get("requests"),
                      local_tokens=int(sum(v for v in toks.values() if isinstance(v, (int, float)))) if toks else None,
                      llm_avoidance=k.get("llm_avoidance"), api_equivalent_usd=est.get("api_equivalent_value"),
                      net_savings_usd=est.get("net_savings"), basis=str(est.get("basis") or "")), None


def _grid(result: list[dict[str, Any]], start: float, n: int) -> list[float | None]:
    vals: list[float | None] = [None] * n
    for series in result[:1]:
        for ts, raw in series.get("values") or []:
            i = int(round((float(ts) - start) / TREND_STEP))
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= i < n and math.isfinite(v):
                vals[i] = round(v, 3)
    return vals


async def _trends() -> tuple[list[Trend], str | None]:
    n = TREND_HOURS * 3600 // TREND_STEP
    end = math.floor(time.time() / TREND_STEP) * TREND_STEP
    start = end - (n - 1) * TREND_STEP
    try:
        first = await upstream.prom_range(TRENDS[0][3], start, end, TREND_STEP, strict=True)
    except UpstreamError as e:
        return [], f"{upstream.reason(e)}, so the trends can't be shown."
    rest = await asyncio.gather(*(upstream.prom_range(t[3], start, end, TREND_STEP) for t in TRENDS[1:]))
    out = []
    for (key, label, unit, _, thr, thr_label, href), res in zip(TRENDS, [first, *rest]):
        vals = _grid(res, start, n)
        latest = next((v for v in reversed(vals) if v is not None), None)
        out.append(Trend(key=key, label=label, unit=unit, start=start, step=TREND_STEP, values=vals, latest=latest,
                         threshold=thr, threshold_label=thr_label, href=href))
    return out, None


@router.get("/home", response_model=HomeOverview)
async def home(user: User = Depends(auth.require("read"))) -> HomeOverview:
    from lif.console.routes.ai import _active      # the answers being written right now (in-process)
    now = time.time()
    (value, value_note), (trends, trends_note) = await asyncio.gather(
        _cached("value", 120, _value), _cached("trends", 60, _trends))
    return HomeOverview(generated_at=now, ask=ask_stats(user.id, now),
                        threads=threads.list_threads(user.id, limit=RECENT_THREADS, active=_active()),
                        availability=availability(), value=value, value_note=value_note,
                        trends=trends, trends_hours=TREND_HOURS, trends_note=trends_note)
