"""Batch engine tests: deterministic (injected clock, explicit tick()), no Jev, fake gateway."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lif.batch.engine import BatchEngine, BatchError
from lif.decision.fabric import DecisionFabric
from lif.decision.rules import rules
from lif.decision.types import load_definitions
from lif.gpu.state import BlerbzState, Snapshot


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeGpu:
    def __init__(self, clock: Clock, state: BlerbzState = BlerbzState.LOW):
        self.clock, self.state = clock, state

    def current(self) -> Snapshot:
        s = Snapshot(ts=self.clock(), reachable=True, residents_loaded={"llm": True})
        s.state, s.reason = self.state, f"test {self.state.name}"
        return s


class Gateway:
    """Fake gateway: per-call scripted status codes, records every request."""

    def __init__(self):
        self.calls: list[dict] = []
        self.script: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append({"path": request.url.path, "body": body, "headers": dict(request.headers)})
        code = self.script.pop(0) if self.script else 200
        if code == 200:
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                             "lif": {"served_by": "tier0", "fallback": False}})
        return httpx.Response(code, json={"error": {"message": f"scripted {code}"}})


def make(tmp_path, state=BlerbzState.LOW, name="b.db"):
    clock, gw = Clock(), Gateway()
    fabric = DecisionFabric(load_definitions(), rules, jev=None)
    eng = BatchEngine(str(tmp_path / name), fabric, FakeGpu(clock, state),
                      httpx.AsyncClient(transport=httpx.MockTransport(gw.handler)),
                      "http://gateway.test", "k", clock=clock)
    return eng, gw, clock


def items(n, prefix="i"):
    return [{"custom_id": f"{prefix}{k}", "messages": [{"role": "user", "content": f"hello {k}"}]} for k in range(n)]


def run(coro):
    return asyncio.run(coro)


def drain(eng, max_ticks=50):
    for _ in range(max_ticks):
        if run(eng.tick()) == 0:
            return


def test_submit_and_idempotency(tmp_path):
    eng, gw, _ = make(tmp_path)
    j1, created1 = run(eng.submit({"items": items(3), "idempotency_key": "abc", "description": "tag stories"},
                                  owner="bnn", data_class="INTERNAL"))
    j2, created2 = run(eng.submit({"items": items(5), "idempotency_key": "abc"}, owner="bnn"))
    assert created1 and not created2 and j1["id"] == j2["id"] and j2["total"] == 3
    assert j1["owner"] == "bnn" and j1["data_class"] == "INTERNAL" and j1["state"] == "queued"
    # classification ran through rules (no Jev), and recorded a model hint for a tagging task
    assert j1["classification"]["batch-model-size"]["provider"] == "rules"
    assert j1["model_hint"] == "local/fast" and j1["model"] == "local/batch"
    with pytest.raises(BatchError):
        run(eng.submit({"items": []}))
    with pytest.raises(BatchError):
        run(eng.submit({"items": items(1), "priority": 2}))


def test_runs_to_completion_with_headers(tmp_path):
    eng, gw, _ = make(tmp_path)
    j, _ = run(eng.submit({"items": items(3), "priority": 5}, data_class="PUBLIC"))
    drain(eng)
    job = eng.job(j["id"])
    assert job["state"] == "completed" and job["counts"]["succeeded"] == 3 and job["progress"] == 1.0
    h = gw.calls[0]["headers"]
    assert h["x-lif-workload"] == f"batch:{j['id']}" and h["x-lif-priority"] == "5"
    assert h["x-lif-data-class"] == "PUBLIC" and h["authorization"] == "Bearer k"
    res = eng.results(j["id"])
    assert [r["custom_id"] for r in res] == ["i0", "i1", "i2"]
    assert res[0]["response"]["lif"]["served_by"] == "tier0"


def test_priority_and_deadline_ordering(tmp_path):
    eng, gw, clock = make(tmp_path)
    low, _ = run(eng.submit({"items": items(1, "low"), "priority": 7}))
    high, _ = run(eng.submit({"items": items(1, "high"), "priority": 4}))
    soon, _ = run(eng.submit({"items": items(1, "soon"), "priority": 7, "deadline": clock() + 600}))
    eng.max_concurrency = 1
    drain(eng)
    order = [c["body"]["messages"][0]["content"] for c in gw.calls]
    ids = [eng.results(x["id"])[0]["custom_id"] for x in (high, soon, low)]
    assert ids == ["high0", "soon0", "low0"]
    assert len(order) == 3
    # order of dispatch: P4 first, then the P7 job with the earlier deadline, then the other P7
    sent = [c["headers"]["x-lif-workload"] for c in gw.calls]
    assert sent == [f"batch:{high['id']}", f"batch:{soon['id']}", f"batch:{low['id']}"]


def test_concurrency_is_two_when_run(tmp_path):
    eng, gw, _ = make(tmp_path)
    run(eng.submit({"items": items(5), "priority": 5}))
    assert run(eng.tick()) == 2


def test_blerbz_imminent_pauses_background(tmp_path):
    eng, gw, _ = make(tmp_path, BlerbzState.IMMINENT)
    p5, _ = run(eng.submit({"items": items(2), "priority": 5}))
    p4, _ = run(eng.submit({"items": items(2), "priority": 4}))
    assert run(eng.tick()) == 0 and gw.calls == []
    assert eng.job(p5["id"])["reason"].startswith("waiting:") and "IMMINENT" in eng.job(p5["id"])["reason"]
    assert eng.job(p4["id"])["reason"].startswith("waiting:")
    # production finishes → work resumes automatically
    eng.gpu.state = BlerbzState.LOW
    drain(eng)
    assert eng.job(p5["id"])["state"] == "completed" and eng.job(p4["id"])["state"] == "completed"


def test_operator_pause(tmp_path):
    eng, gw, _ = make(tmp_path)
    j, _ = run(eng.submit({"items": items(2), "priority": 5}))
    eng.control(True, "maintenance")
    assert run(eng.tick()) == 0 and eng.stats()["paused"] is True
    assert "paused by operator" in eng.job(j["id"])["reason"]
    eng.control(False)
    drain(eng)
    assert eng.job(j["id"])["state"] == "completed"


def test_retry_on_503_then_success(tmp_path):
    eng, gw, clock = make(tmp_path)
    j, _ = run(eng.submit({"items": items(1), "priority": 5, "max_attempts": 3}))
    gw.script = [503]
    assert run(eng.tick()) == 1
    r = eng.results(j["id"])[0]
    assert r["status"] == "pending" and r["attempts"] == 1 and "503" in r["error"]
    assert run(eng.tick()) == 0              # still in backoff
    clock.t += 5
    assert run(eng.tick()) == 1
    r = eng.results(j["id"])[0]
    assert r["status"] == "succeeded" and r["attempts"] == 2
    assert eng.job(j["id"])["state"] == "completed"


def test_retries_exhausted(tmp_path):
    eng, gw, clock = make(tmp_path)
    j, _ = run(eng.submit({"items": items(1), "priority": 5, "max_attempts": 2}))
    gw.script = [503, 503]
    run(eng.tick())
    clock.t += 10
    run(eng.tick())
    assert eng.results(j["id"])[0]["status"] == "failed"
    assert eng.job(j["id"])["state"] == "failed"


def test_permanent_400_failure(tmp_path):
    eng, gw, _ = make(tmp_path)
    j, _ = run(eng.submit({"items": items(2), "priority": 5, "max_attempts": 3}))
    gw.script = [400]
    drain(eng)
    res = {r["custom_id"]: r for r in eng.results(j["id"])}
    statuses = sorted(r["status"] for r in res.values())
    assert statuses == ["failed", "succeeded"]
    failed = next(r for r in res.values() if r["status"] == "failed")
    assert failed["attempts"] == 1 and "400" in failed["error"]
    assert eng.job(j["id"])["state"] == "completed"


def test_cancel(tmp_path):
    eng, gw, _ = make(tmp_path)
    j, _ = run(eng.submit({"items": items(4), "priority": 5}))
    run(eng.tick())                         # 2 done
    out = eng.cancel(j["id"])
    assert out["state"] == "cancelled" and out["counts"]["cancelled"] == 2 and out["counts"]["succeeded"] == 2
    assert run(eng.tick()) == 0


def test_job_pause_resume(tmp_path):
    eng, gw, _ = make(tmp_path)
    j, _ = run(eng.submit({"items": items(3), "priority": 5}))
    eng.pause(j["id"])
    assert run(eng.tick()) == 0 and eng.job(j["id"])["state"] == "paused"
    eng.resume(j["id"])
    drain(eng)
    assert eng.job(j["id"])["state"] == "completed"


def test_restart_recovery(tmp_path):
    eng, gw, clock = make(tmp_path)
    j, _ = run(eng.submit({"items": items(2), "priority": 5}))
    # simulate a crash mid-dispatch: an item is left `running`
    eng.db.x("UPDATE items SET state='running', attempts=1 WHERE job_id=? AND idx=0", (j["id"],))
    eng2 = BatchEngine(eng.db.path, eng.fabric, eng.gpu, eng.client, "http://gateway.test", "k", clock=clock)
    assert eng2.results(j["id"])[0]["status"] == "pending"
    drain(eng2)
    assert eng2.job(j["id"])["state"] == "completed" and eng2.job(j["id"])["counts"]["succeeded"] == 2


def test_deadline_expiry(tmp_path):
    eng, gw, clock = make(tmp_path)
    j, _ = run(eng.submit({"items": items(4), "priority": 5, "deadline": clock() + 60}))
    run(eng.tick())                         # 2 succeed
    clock.t += 120
    run(eng.tick())
    job = eng.job(j["id"])
    assert job["state"] == "expired" and job["counts"]["expired"] == 2 and job["counts"]["succeeded"] == 2


def test_urgent_deadline_sets_priority_via_rules(tmp_path):
    eng, gw, clock = make(tmp_path)
    j, _ = run(eng.submit({"items": items(1), "deadline": clock() + 3600, "description": "summarize"}))
    assert j["priority"] == 4 and j["priority_source"] == "decision:rules"
    k, _ = run(eng.submit({"items": items(1), "description": "summarize"}))
    assert k["priority"] == 5 and k["priority_source"] == "default"


def test_stats(tmp_path):
    eng, gw, _ = make(tmp_path)
    run(eng.submit({"items": items(3), "priority": 6}))
    s = eng.stats()
    assert s["queue_depth"] == {"P6": 3} and s["pending"] == 3 and s["blerbz"]["state"] == "LOW"
