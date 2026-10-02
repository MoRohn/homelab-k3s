"""GET /api/home: the Home cockpit's data — Ask stats, recent threads, availability, value, trends.

No network: the controller and Prometheus are test_console_sys's Fake. Each part must degrade on its own
with a plain reason, never as a made-up 0, and the shared upstream parts must be cached."""
from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lif.console import auth, db, errors, threads
from lif.console.routes import home as home_routes
from test_console_sys import ADMIN, Fake, fake  # noqa: F401  (fixture; pytest puts tests/ on sys.path)

SAVINGS = {"window": "24h",
           "measured": {"requests": 412, "local_tokens": {"input": 90000, "output": 31000}},
           "kpi": {"llm_avoidance": 0.62, "heavy_model_avoidance": 1.0},
           "estimates_usd": {"api_equivalent_value": 0.0321, "net_savings": 0.0123, "basis": "ESTIMATE: tokens × …"}}


@pytest.fixture
def client(fake: Fake, tmp_path: Any, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    db.close()
    db.init(tmp_path / "console.db")
    home_routes._cache.clear()
    orig = fake.route

    def route(host: str, method: str, path: str, params: Any, body: Any) -> Any:
        if host == "controller" and path == "/v1/savings":
            return SAVINGS
        if host == "monitoring-kube-prometheus-prometheus" and path.endswith("query_range"):
            q = params.get("query", "")
            start, end, step = float(params["start"]), float(params["end"]), float(params["step"])
            ts = [start + i * step for i in range(int((end - start) // step) + 1)]
            if "llamacpp" in q:            # decode: no traffic in the first half (NaN), then 21 tok/s
                vals = ["NaN" if i < len(ts) // 2 else "21.0" for i in range(len(ts))]
            elif "gpusched_mem_available_mib" in q:
                vals = ["6.5"] * len(ts)
            else:
                return {"status": "success", "data": {"resultType": "matrix", "result": []}}
            return {"status": "success", "data": {"resultType": "matrix",
                                                  "result": [{"metric": {}, "values": list(zip(ts, vals))}]}}
        return orig(host, method, path, params, body)

    fake.route = route  # type: ignore[method-assign]
    app = FastAPI()
    errors.install(app)
    app.include_router(home_routes.router)
    app.dependency_overrides[auth.current_user] = lambda: ADMIN
    yield TestClient(app, raise_server_exceptions=False)
    db.close()


def _answer(thread_id: str, *, latency: float, prompt: int, completion: int, status: str = "done",
            ago: float = 60) -> None:
    rec = {"latency_ms": latency, "tokens": {"prompt": prompt, "completion": completion}}
    with db.tx() as c:
        c.execute("INSERT INTO messages(id, thread_id, role, content, created_at, status, attachments_json, receipt_json) "
                  "VALUES(?,?,?,?,?,?,?,?)", (threads.new_id("msg"), thread_id, "assistant", "x", time.time() - ago,
                                              status, "[]", json.dumps(rec)))


def test_home_reports_ask_stats_threads_value_and_trends(client: TestClient, fake: Fake) -> None:  # noqa: F811
    t = threads.create_thread(ADMIN.id, "auto", "local_only", title="Explain unified memory")
    for lat in (900, 1200, 1500, 2100):
        _answer(t.id, latency=lat, prompt=100, completion=50)
    _answer(t.id, latency=0, prompt=0, completion=0, status="error")
    _answer(t.id, latency=5000, prompt=999, completion=999, ago=2 * 86400)       # outside the 24 h window
    other = threads.create_thread("someone-else", "auto", "local_only", title="Not mine")
    _answer(other.id, latency=1, prompt=1, completion=1)

    h = client.get("/api/home").json()
    assert h["ask"] == {"window_hours": 24, "answers": 4, "failed": 1, "prompt_tokens": 400, "completion_tokens": 200,
                        "median_latency_ms": 1350.0, "p90_latency_ms": 2100.0}
    assert [x["title"] for x in h["threads"]] == ["Explain unified memory"]           # this user's only
    assert h["value"]["requests"] == 412 and h["value"]["local_tokens"] == 121000 and h["value_note"] is None
    trends = {x["key"]: x for x in h["trends"]}
    assert set(trends) == {"decode", "ttft", "requests", "mem_free", "gpu"} and h["trends_note"] is None
    d = trends["decode"]
    assert len(d["values"]) == 72 and d["values"][0] is None and d["latest"] == 21.0    # no traffic = gap, not 0
    assert trends["mem_free"]["threshold"] == 8.0 and trends["mem_free"]["latest"] == 6.5
    assert trends["ttft"]["values"] == [None] * 72 and trends["ttft"]["latest"] is None  # no data: honest


def test_home_parts_degrade_on_their_own(client: TestClient, fake: Fake) -> None:  # noqa: F811
    fake.down |= {"controller", "monitoring-kube-prometheus-prometheus"}
    h = client.get("/api/home").json()
    assert h["value"] is None and "can't be shown" in h["value_note"]
    assert h["trends"] == [] and "can't be shown" in h["trends_note"]
    assert h["ask"]["answers"] == 0 and h["ask"]["median_latency_ms"] is None           # still answers


def test_home_shares_cached_upstream_calls(client: TestClient, fake: Fake) -> None:  # noqa: F811
    client.get("/api/home")
    n = len(fake.calls)
    client.get("/api/home")
    client.get("/api/home")
    assert len(fake.calls) == n                      # value (120 s) and trends (60 s) came from the cache
    assert sum(1 for c in fake.calls if c[2].endswith("query_range")) == len(home_routes.TRENDS)


def test_availability_reads_the_useful_local_ai_capability(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Controller shape: {capability: {samples, availability, mean_latency_ms}} per window (registry.availability)."""
    from lif.console import poller
    snap = poller.snapshot()
    win = lambda a: {"gateway": {"samples": 10, "availability": 1.0},  # noqa: E731
                     "useful_local_ai": {"samples": 2880, "availability": a, "mean_latency_ms": 610.0}}
    monkeypatch.setattr(snap, "raw", {**snap.raw, "overview": {"availability_24h": win(0.993), "availability_7d": win(0.981)}})
    monkeypatch.setattr(poller, "snapshot", lambda: snap)
    av = client.get("/api/home").json()["availability"]
    assert av["last_24h"] == 0.993 and av["last_7d"] == 0.981 and av["target"] == 0.95
    monkeypatch.setattr(snap, "raw", {**snap.raw, "overview": {"availability_24h": {}}})     # no probes yet
    assert client.get("/api/home").json()["availability"]["last_24h"] is None
