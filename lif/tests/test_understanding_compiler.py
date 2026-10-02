"""The compile loop end to end: progressive events, overrides from the same spec, fallbacks, offline
operation and the live-state builder (Acceptance B, F, G, H, I; spec §47–§49, §72, §80, §92–§94, §130)."""
from __future__ import annotations

import json

import pytest

from lif.understanding import compiler as C
from lif.understanding import knowledge_model as K
from lif.understanding.registry import Registry
from lif.understanding.render.base import RendererCapabilities, RenderResult
from lif.understanding.router import ResourceState
from lif.understanding.store import Store

GPU = "Why are these Kubernetes pods not reaching available GPUs?"

# Synthetic host state (no production numbers): one pod waiting for a GPU, gpusched reachable and calm.
STATE = {"collected_at": 1.0, "gpu": {"util_percent": 3.0, "mem_available_mib": 40960.0,
                                      "util_source": "gpusched_gpu_util_percent"},
         "blerbz": {"state": "LOW", "reason": "P(production within 1h) = 5%", "production_leases": 0,
                    "background_leases": 0},
         "pods": [{"ns": "ai-batch", "name": "job-1", "phase": "Pending", "gpu": True, "reason": "Unschedulable",
                   "message": "0/1 nodes are available: 1 Insufficient nvidia.com/gpu."}],
         "residents": {"chat": True}, "errors": {}}


async def fake_gpu():
    return json.loads(json.dumps(STATE))


def make(tmp_path, **kw):
    kw.setdefault("collectors", {"gpu": fake_gpu})
    return C.Compiler(Store(str(tmp_path / "u.db")), **kw)


async def run(it):
    return await C.collect_all(it)


def names(events):
    return [e["event"] for e in events]


async def test_explain_streams_progressively(tmp_path):
    comp = make(tmp_path)
    evs = await run(comp.explain(C.ExplanationRequest(question=GPU, audience="engineer")))
    n = names(evs)
    assert n[:4] == ["analysis.started", "knowledge_model.ready", "explanation_ir.ready", "summary.ready"]
    assert n.index("summary.ready") < n.index("route.ready") < n.index("mermaid.ready")
    assert n[-2:] == ["evaluation.ready", "done"]
    route = next(e for e in evs if e["event"] == "route.ready")["data"]
    assert route["primary"] == "mermaid" and "ste-prose" in route["supporting"]
    assert set(route["judgments"]) == set(C.DECISIONS)       # offline rules answered all three
    evaluation = next(e for e in evs if e["event"] == "evaluation.ready")["data"]
    assert evaluation["ok"] and evaluation["semantic_ok"]
    assert comp.builder_calls == 1


async def test_acceptance_f_and_g_overrides_never_rebuild(tmp_path):
    comp = make(tmp_path)
    evs = await run(comp.explain(C.ExplanationRequest(question=GPU, profile="deep")))
    sid = evs[0]["data"]["session"]
    spec_id = evs[-1]["data"]["explanation_id"]
    h = comp.store.get_spec(spec_id).semantic_hash()
    simpler = await run(comp.simplify(sid))
    exec_ = await run(comp.rerender(sid, audience={"role": "executive", "expertise": "novice"}))
    text = await run(comp.rerender(sid, format="text"))
    assert comp.builder_calls == 1
    assert comp.store.get_spec(spec_id).semantic_hash() == h
    for evs2 in (simpler, exec_, text):
        assert evs2[-1]["event"] == "done" and evs2[-1]["data"]["explanation_id"] == spec_id
    assert comp.store.get_session(sid)["presentation"]["depth"] == "learn"     # simplify stepped deep → learn; later overrides keep it
    assert next(e for e in text if e["event"] == "route.ready")["data"]["primary"] == "ste-prose"
    fb = comp.evaluate(sid)["feedback"]
    assert [f["kind"] for f in fb] == ["simplify", "override"]


async def test_deepen_uses_existing_levels_first(tmp_path):
    comp = make(tmp_path)
    evs = await run(comp.explain(C.ExplanationRequest(question=GPU, profile="quick")))
    sid = evs[0]["data"]["session"]
    deeper = await run(comp.deepen(sid))
    assert deeper[-1]["event"] == "done" and comp.builder_calls == 1
    assert comp.store.get_session(sid)["presentation"]["depth"] == "learn"


async def test_acceptance_h_offline_works_from_packages_and_state(tmp_path):
    comp = make(tmp_path)            # no generate, no judge service, no resource probe: fully local
    for q in ("What is Kubernetes?", GPU, "How does changing GPU reservation affect throughput?",
              "Why is GPU utilization on the DGX Spark low right now?"):
        evs = await run(comp.explain(C.ExplanationRequest(question=q)))
        assert evs[-1]["event"] == "done", (q, evs[-1])
    unknown = await run(comp.explain(C.ExplanationRequest(question="Teach me how transformer attention works")))
    assert unknown[-1]["event"] == "error" and "no local model" in unknown[-1]["data"]["detail"]


async def test_live_state_builder_dogfood(tmp_path):
    comp = make(tmp_path)
    evs = await run(comp.explain(C.ExplanationRequest(question="Explain why GPU utilization on the DGX Spark is low "
                                                               "right now.")))
    assert evs[1]["data"]["builder"] == "state:gpu"
    spec = comp.store.get_spec(evs[-1]["data"]["explanation_id"])
    assert spec.summary.headline == "c-cause-sched"
    assert spec.claim("c-cause-sched").kind == "inferred" and spec.uncertainty_for("c-cause-sched")
    assert spec.claim("c-pending").kind == "observed" and spec.claim("c-pending").evidence == ["e-pending"]
    summary = next(e for e in evs if e["event"] == "summary.ready")["data"]["artifact"]
    assert "cannot be scheduled" in summary and "Confidence" in summary


def test_state_builder_never_reports_unknown_leases():
    st = json.loads(json.dumps(STATE))
    st["blerbz"], st["pods"] = {}, []
    st["errors"] = {"gpusched": "unreachable"}
    spec = K.plan(K.from_gpu_state("Why is the GPU idle?", st))
    assert spec.claim("c-leases") is None and spec.claim("c-state") is None
    head = spec.claim(spec.summary.headline)
    assert head.id == "c-cause-demand" and head.confidence < 0.6
    assert "gpusched metrics (read failed)" in spec.uncertainty_for(head.id)[0].evidence_needed
    busy = json.loads(json.dumps(STATE))
    busy["pods"], busy["blerbz"]["state"] = [], "HIGH"
    assert K.plan(K.from_gpu_state("Why is the GPU idle?", busy)).summary.headline == "c-cause-hold"
    hot = json.loads(json.dumps(STATE))
    hot["gpu"]["util_percent"] = 91.0
    assert K.plan(K.from_gpu_state("Why is the GPU idle?", hot)).summary.headline == "c-not-low"


class Crashy:
    def capabilities(self):
        return RendererCapabilities(name="mermaid", version="9", target="MERMAID",
                                    best_for=("causal", "process", "dependency"), clarity=0.95, resource_class="CPU")

    def accepts(self, s):
        return None

    def render(self, req):
        raise RuntimeError("renderer crashed")


async def test_failed_renderer_falls_back_to_structured_prose(tmp_path):
    reg = Registry()
    reg.register(Crashy())
    comp = make(tmp_path, registry=reg)
    evs = await run(comp.explain(C.ExplanationRequest(question=GPU)))
    n = names(evs)
    assert "mermaid.failed" in n and "structured_prose.ready" in n and n[-1] == "done"


class SlowVideo:
    def capabilities(self):
        return RendererCapabilities(name="video", version="1", target="NARRATED_VIDEO",
                                    best_for=("causal", "process", "temporal"), clarity=0.99,
                                    resource_class="LONG_RUNNING", requires_gpu=True, latency_class="seconds",
                                    consume_seconds=60)

    def accepts(self, s):
        return None

    def render(self, req):
        return RenderResult("video", "1", "NARRATED_VIDEO", "", segments=[])


async def test_acceptance_i_video_yields_to_primary_workload(tmp_path):
    reg = Registry()
    reg.register(SlowVideo())

    async def busy():
        return ResourceState("IMMINENT")
    comp = make(tmp_path, registry=reg, resources=busy)
    evs = await run(comp.explain(C.ExplanationRequest(question=GPU, format="video")))
    route = next(e for e in evs if e["event"] == "route.ready")["data"]
    assert route["primary"] == "mermaid"
    assert route["deferred"][0]["renderer"] == "video" and "GPU priority" in route["deferred"][0]["reason"]
    assert "summary.ready" in names(evs) and "mermaid.ready" in names(evs)


async def test_artifact_cache_hits_on_repeat(tmp_path):
    comp = make(tmp_path)
    evs = await run(comp.explain(C.ExplanationRequest(question=GPU)))
    sid = evs[0]["data"]["session"]
    again = await run(comp.rerender(sid))
    ready = [e for e in again if e["event"] == "mermaid.ready"][0]
    assert ready["data"]["render_metrics"].get("cache") == "hit"


async def test_llm_builder_repairs_once_and_validates():
    good = {"headline": "c1", "concepts": [{"id": "attn", "label": "Attention", "kind": "idea"},
                                           {"id": "tok", "label": "Token", "kind": "entity"}],
            "claims": [{"id": "c1", "text": "Attention lets each token weigh every other token.", "kind": "general",
                        "concepts": ["attn", "tok"], "importance": "primary"}],
            "relationships": [{"id": "r1", "from": "attn", "to": "tok", "type": "uses", "label": "weighs"}]}
    bad = dict(good, claims=[dict(good["claims"][0], concepts=["ghost"])])
    calls = []

    async def gen(msgs):
        calls.append(msgs)
        return "```json\n" + json.dumps(bad if len(calls) == 1 else good) + "\n```"
    km = await K.from_llm("Teach me how attention works", gen, model="test")
    assert km.headline == "c1" and len(calls) == 2
    assert "invalid" in calls[1][-1]["content"]


def test_semantic_diff_reports_claim_and_uncertainty_changes():
    from lif.understanding import packages
    a = packages.find(GPU).spec
    d = a.to_json()
    d["claims"][0]["confidence"] = 0.6
    d["uncertainties"][0]["confidence"] = 0.6
    d["claims"].append({"id": "c-new", "text": "A new point.", "concepts": ["pod"]})
    from lif.understanding.spec import load
    changes = {c["change"] for c in C.semantic_diff(a, load(d))}
    assert {"new claim", "changed confidence", "updated uncertainty"} <= changes


@pytest.mark.parametrize("text,expected", [
    ("Explain why GPU utilization is low", True), ("Why is the batch queue stuck?", True),
    ("How does Jev reduce heavy calls?", True), ("Help me understand attention", True),
    ("Walk me through a deploy", True), ("What causes OOM kills?", True), ("Write a haiku", False),
    ("open gpu", False)])
def test_understanding_intent(text, expected):
    assert C.is_understanding_intent(text) is expected


def test_request_profiles_and_audience():
    d, b, a = C.ExplanationRequest(question="x", profile="quick", audience="executive").resolved()
    assert (d, b, a.role, a.expertise) == ("summary", 30, "executive", "novice")
    d, b, a = C.ExplanationRequest(question="x", audience="engineer", time_budget_seconds=90).resolved()
    assert (d, b, a.role) == ("learn", 90, "software_engineer")
