"""Regressions for the code review of the Understanding Compiler branch (one test per finding)."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lif.understanding import api, collect, packages
from lif.understanding import compiler as C
from lif.understanding import knowledge_model as K
from lif.understanding.registry import Registry
from lif.understanding.render import base
from lif.understanding.store import Store, default_path

GPU_Q = "Why can Kubernetes pods fail to reach available GPUs?"
BASE = {"collected_at": 1.0, "gpu": {"util_percent": 3.0, "mem_available_mib": 40960.0},
        "blerbz": {"state": "LOW", "reason": "calm", "production_leases": 0, "background_leases": 0},
        "pods": [], "residents": {}, "errors": {}}


def state(**kw):
    s = json.loads(json.dumps(BASE))
    for k, v in kw.items():
        s[k] = {**s[k], **v} if isinstance(v, dict) else v
    return s


def test_forced_diagram_family_falls_back_when_the_spec_lacks_it():
    for e in packages.entries(packages.SHIPPED):
        for fam in ("timeline", "causal", "process", "dependency"):
            res = base.run(Registry().get("mermaid"), base.RenderRequest(e.spec, depth="deep",
                                                                         options={"diagram_family": fam}))
            assert res.status in ("ok", "needs_semantic_update"), (e.ref, fam, res.detail)


def test_fallback_cause_never_contradicts_lease_evidence():
    busy_leases = K.plan(K.from_gpu_state("Why is the GPU idle?", state(blerbz={"production_leases": 2,
                                                                                "background_leases": 1})))
    texts = " ".join(c.text for c in busy_leases.claims)
    assert "no leases are active" not in texts
    assert busy_leases.summary.headline == "c-cause-light"
    hot = K.plan(K.from_gpu_state("Why is the GPU idle?", state(gpu={"util_percent": 85.0},
                                                               blerbz={"production_leases": 2})))
    assert hot.summary.headline == "c-not-low" and hot.claim("c-cause-demand") is None


def test_pending_while_pulling_is_not_a_scheduling_failure():
    pulling = {"ns": "a", "name": "p", "phase": "Pending", "gpu": True, "unschedulable": False, "reason": ""}
    spec = K.plan(K.from_gpu_state("Why is the GPU idle?", state(pods=[pulling])))
    assert spec.claim("c-cause-sched") is None
    doc = {"items": [{"metadata": {"namespace": "a", "name": "p"},
                      "spec": {"containers": [{"resources": {"limits": {"nvidia.com/gpu": 1}}}]},
                      "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "True"}]}}]}
    assert collect.pods_from_json(doc)[0]["unschedulable"] is False


def make(tmp_path):
    return C.Compiler(Store(str(tmp_path / "u.db")), collectors={})


async def test_simplify_at_glance_stays_at_glance(tmp_path):
    comp = make(tmp_path)
    evs = await C.collect_all(comp.explain(C.ExplanationRequest(question=GPU_Q, depth="glance")))
    sid = evs[0]["data"]["session"]
    await C.collect_all(comp.simplify(sid))
    assert comp.store.get_session(sid)["presentation"]["depth"] == "glance"


def test_override_audience_words_and_bad_input_over_http(tmp_path):
    comp = make(tmp_path)
    app = FastAPI()
    app.include_router(api.make_router(lambda: comp))
    c = TestClient(app)
    body = c.post("/v1/explain", json={"question": GPU_Q}).json()
    xid = body["explanation_id"]
    ok = c.post(f"/v1/explanations/{xid}/render", json={"audience": {"role": "engineer"}})
    assert ok.status_code == 200 and ok.json()["status"] == "done"
    assert c.post(f"/v1/explanations/{xid}/render", json={"audience": {"colour": "red"}}).status_code == 422
    assert c.post(f"/v1/explanations/{xid}/render", json={"depth": "bottomless"}).status_code == 422
    # Unknown or mismatched sessions are 404, never 500.
    assert c.post(f"/v1/explanations/{xid}/render", json={"session": "s-nope"}).status_code == 404
    sim = c.post("/v1/explain", json={"question": "How does changing GPU reservation affect throughput?"}).json()
    art = c.get(f"/v1/sessions/{sim['session']}/artifacts/simulation")
    assert art.status_code == 200 and art.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in art.headers["content-security-policy"]


async def test_sessions_follow_a_spec_through_deepen(tmp_path):
    comp = make(tmp_path)
    evs = await C.collect_all(comp.explain(C.ExplanationRequest(question=GPU_Q)))
    sid, xid = evs[0]["data"]["session"], evs[-1]["data"]["explanation_id"]
    parent = comp.store.get_spec(xid)
    child = parent.model_copy(deep=True)
    child.id, child.metadata = "x-child", parent.metadata.model_copy(update={"parent": xid})
    comp.store.put_spec(child)
    s = comp.store.get_session(sid)
    comp.store.put_session(sid, child.id, s["request"], s["presentation"], s["plan"], s["outputs"])
    assert comp.store.latest_session(xid) == sid
    assert comp.store.session_belongs(sid, xid) and comp.store.session_belongs(sid, "x-child")
    assert not comp.store.session_belongs(sid, "x-other")


def test_store_defaults_next_to_the_console_database(monkeypatch, tmp_path):
    monkeypatch.delenv("LIF_UNDERSTANDING_DB", raising=False)
    monkeypatch.setenv("LIF_CONSOLE_DB", str(tmp_path / "data" / "console.db"))
    assert default_path() == str(tmp_path / "data" / "understanding.db")


async def test_fabric_judge_reuses_one_client(monkeypatch):
    made = []

    class Fake:
        def __init__(self, url=None):
            made.append(url)

        async def decide(self, name, state, **kw):
            raise RuntimeError("unreachable")

    import lif.decision.sdk as sdk
    monkeypatch.setattr(sdk, "RemoteIntelligence", Fake)
    judge = C.fabric_judge("http://x")
    for name in C.DECISIONS:
        await judge(name, {"features": {"question_kind": "definition", "structure": {"definition": 1.0}}})
    assert len(made) == 1


async def test_gpusched_snapshot_is_shared_and_closes_its_client(monkeypatch):
    from lif.gpu import state as gs
    closed, reads = [], []

    class W:
        def __init__(self):
            class Cl:
                async def aclose(self_inner):
                    closed.append(1)
            self.client = Cl()

        async def refresh(self):
            reads.append(1)
            return gs.Snapshot(reachable=True)

    monkeypatch.setattr(gs, "GpuStateWatcher", W)
    monkeypatch.setattr(collect, "_SNAP", (0.0, None))
    await collect.gpusched_snapshot()
    await C.gpusched_probe()
    assert reads == [1] and closed == [1]


def test_gpusched_token_file_env(monkeypatch, tmp_path):
    from lif.gpu.state import GpuStateWatcher
    tok = tmp_path / "t"
    tok.write_text("abc\n")
    monkeypatch.setenv("LIF_GPUSCHED_TOKEN_FILE", str(tok))
    assert GpuStateWatcher().token == "abc"
