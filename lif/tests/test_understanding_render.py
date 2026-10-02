"""Analysis, registry, router, renderers, contract and critic (Acceptance A, C, E, G, I, J)."""
from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from lif.understanding import analysis, critic, packages, router
from lif.understanding.registry import Registry
from lif.understanding.render import base
from lif.understanding.render.base import RendererCapabilities, RenderRequest, RenderResult, Segment
from lif.understanding.spec import Audience

K8S = "What is Kubernetes?"
GPU = "Why are these Kubernetes pods not reaching available GPUs?"
RES = "How does changing GPU reservation affect throughput?"
JEV = "How does Jev reduce heavy model calls in Labzilla?"


def spec(q):
    return packages.find(q).spec


def plan(q, depth="learn", reg=None, **ctx):
    s = spec(q)
    return router.route(s, analysis.analyze(s, depth, q), reg or Registry(),
                        router.RouteContext(depth=depth, **ctx))


class FakeVideo:
    """An available GPU video renderer, to prove the router does not pick video just because it exists."""

    def capabilities(self):
        return RendererCapabilities(name="fake-video", version="1", target="NARRATED_VIDEO",
                                    best_for=("temporal", "process", "causal", "narrative", "definition"),
                                    outputs=("video/mp4",), latency_class="minutes", resource_class="LONG_RUNNING",
                                    requires_gpu=True, clarity=0.95, consume_seconds=300)

    def accepts(self, s):
        return None

    def render(self, req):
        return RenderResult("fake-video", "1", "NARRATED_VIDEO", "", segments=[])


def test_question_kinds():
    k = analysis.question_kind
    assert k("What is Kubernetes?") == "definition"
    assert k("Why is GPU utilization low right now?") == "debug"
    assert k("How does changing GPU reservation affect throughput?") == "parameter"
    assert k("Teach me how transformer attention works") == "teach"
    assert k("Explain what changed after the last Labzilla incident") == "temporal"
    assert k("Compare vLLM versus llama.cpp") == "comparative"
    assert k("Why does the scheduler score nodes?") == "causal"


def test_features_respect_depth():
    s = spec(GPU)
    glance, deep = analysis.analyze(s, "glance"), analysis.analyze(s, "deep")
    assert glance.process_steps == 0 and deep.process_steps == 4
    assert glance.structure["causal"] == 1.0
    pub = deep.public_state()
    assert GPU not in json.dumps(pub) and "affinity" not in json.dumps(pub).lower()


def test_registry_lists_unavailable_renderers_with_reasons():
    caps = {c.name: c for c in Registry().capabilities()}
    assert caps["ste-prose"].available and caps["mermaid"].resource_class == "CPU"
    assert not caps["narrated-video"].available and "phase 8" in caps["narrated-video"].unavailable_reason
    assert caps["narrated-video"].resource_class == "LONG_RUNNING"


def test_acceptance_a_definition_gets_prose_not_video():
    reg = Registry()
    reg.register(FakeVideo())
    for depth in ("summary", "learn"):
        p = plan(K8S, depth, reg, resources=router.ResourceState("LOW"))
        assert p.primary == "ste-prose" and p.supporting == []
        assert "fake-video" not in p.selected
        assert not any(d["renderer"] == "fake-video" for d in p.deferred)
        assert any("Video not generated" in w for w in p.why) or depth == "summary"


def test_acceptance_b_causal_question_gets_diagram_plus_summary():
    p = plan(GPU, "learn")
    assert p.primary == "mermaid" and p.supporting[0] == "ste-prose"
    assert "causal links" in p.why[0]


def test_acceptance_c_parameter_question_gets_simulation():
    p = plan(RES, "learn")
    assert p.primary == "simulation"
    assert "mermaid" in p.supporting               # the causal chain is still worth a diagram


def test_marginal_value_limits_artifacts_by_time_budget():
    assert len(plan(GPU, "learn", time_budget_seconds=30).selected) <= 2
    assert len(plan(JEV, "deep", time_budget_seconds=600).selected) <= 4


def test_acceptance_i_gpu_renderers_defer_while_primary_workload_busy():
    reg = Registry()
    reg.register(FakeVideo())
    busy = plan(GPU, "deep", reg, format="video", resources=router.ResourceState("IMMINENT"))
    assert busy.primary != "fake-video"
    assert any(d["renderer"] == "fake-video" and "GPU priority" in d["reason"] for d in busy.deferred)
    assert busy.primary == "mermaid"                 # summary and diagram stay available
    free = plan(GPU, "deep", reg, format="video", resources=router.ResourceState("LOW"))
    assert free.primary == "fake-video"
    assert plan(GPU, "deep", reg, format="video", resources=router.ResourceState("HIGH", override=True)).primary \
        == "fake-video"


def test_manual_override_and_text_only():
    assert plan(GPU, format="table").primary == "mermaid"      # no comparison in this spec → fallback, explained
    assert "not available" in plan(GPU, format="table").why[0]
    t = plan(GPU, format="text")
    assert t.primary == "ste-prose" and t.supporting == []
    assert plan(JEV, format="table").primary == "table"


def test_mobile_prefers_step_through_over_wide_sketch():
    p = plan(GPU, "learn", viewport="mobile")
    scores = {s.renderer: s.utility for s in p.scores}
    assert scores["excalidraw"] < scores["step-through"]


def test_jev_judgments_adjust_scores():
    base_scores = {s.renderer: s.utility for s in plan(GPU).scores}
    p = plan(GPU, judgments={"understanding-prose-sufficient": "yes", "understanding-diagram-family": "process"})
    adj = {s.renderer: s.utility for s in p.scores}
    assert adj["mermaid"] < base_scores["mermaid"] and adj["ste-prose"] == base_scores["ste-prose"]
    assert p.options == {"diagram_family": "process"}


# ── renderers ──────────────────────────────────────────────────────────────────────────────────

ALL = ["ste-prose", "structured-prose", "mermaid", "excalidraw", "table", "static-explainer", "step-through",
       "simulation"]


@pytest.mark.parametrize("q", [K8S, GPU, RES, JEV])
@pytest.mark.parametrize("depth", ["glance", "summary", "learn", "deep"])
def test_every_renderer_honours_the_contract(q, depth):
    reg, s = Registry(), spec(q)
    for name in ALL:
        r = reg.get(name)
        if r.accepts(s):
            continue
        res = base.run(r, RenderRequest(s, depth=depth))
        assert res.status in ("ok", "needs_semantic_update"), (name, res.detail)
        if res.status == "ok":
            assert res.verification["contract"]["ok"]


def test_acceptance_j_invented_claim_is_rejected():
    s = spec(GPU)

    class Rogue:
        def capabilities(self):
            return RendererCapabilities(name="rogue", version="1", target="STE_PROSE", best_for=("narrative",))

        def accepts(self, s):
            return None

        def render(self, req):
            segs = [Segment("Memory fragmentation is the main cause.", ["c-root", "u-root"])]
            return RenderResult("rogue", "1", "STE_PROSE", segs[0].text, segments=segs, emphasis=["c-root"])

    res = base.run(Rogue(), RenderRequest(s))
    assert res.status == "failed" and "fragmentation" in res.detail
    # Uncited content, unknown ids, invented numbers and dropped uncertainty are each rejected.
    cases = [([Segment("Affinity.", [])], "uncited"), ([Segment("Affinity.", ["c-nope"])], "unknown ids"),
             ([Segment("GPU utilization on node B was 0% for 9 minutes.", ["c-idle"])], "numbers"),
             ([Segment("Node affinity keeps the pending pods off node B.", ["c-root"])], "without its uncertainty")]
    for segs, needle in cases:
        problems = base.check_contract(s, RenderResult("x", "1", "STE_PROSE", "", segments=segs))
        assert any(needle in p for p in problems), (needle, problems)


def test_renderer_crash_and_gap_are_contained():
    class Boom:
        def capabilities(self):
            return RendererCapabilities(name="boom", version="1", target="MERMAID", best_for=())

        def accepts(self, s):
            return None

        def render(self, req):
            raise ZeroDivisionError("x")

    assert base.run(Boom(), RenderRequest(spec(GPU))).status == "failed"
    gap = base.run(Registry().get("excalidraw"), RenderRequest(spec(K8S), depth="summary"))
    assert gap.status == "needs_semantic_update"


def test_mermaid_output_is_valid_and_marks_inference_and_uncertainty():
    res = base.run(Registry().get("mermaid"), RenderRequest(spec(GPU), depth="learn"))
    src = res.artifact
    assert src.startswith("flowchart LR") and "accTitle:" in src and "accDescr:" in src
    assert "-.->" in src                                 # c-root is inferred: its chain is dashed
    assert ":::uncertain" in src and "Confidence 78%" in src
    assert res.verification["syntax"] == {"ok": True, "errors": [], "method": "static", "rasterised": False}
    tl = base.run(Registry().get("mermaid"), RenderRequest(spec(GPU), depth="deep",
                                                          options={"diagram_family": "timeline"}))
    assert tl.artifact.startswith("timeline") and "5s : Scheduling failed" in tl.artifact


def test_excalidraw_scene_has_no_overlaps_and_bound_arrows():
    res = base.run(Registry().get("excalidraw"), RenderRequest(spec(GPU), depth="learn"))
    scene = json.loads(res.artifact)
    assert scene["type"] == "excalidraw" and res.verification["no-overlap"]["ok"]
    assert res.verification["bindings"]["ok"]
    assert {e["customData"]["spec_id"] for e in scene["elements"] if e["type"] == "rectangle"} >= {"affinity", "u-root"}


def test_html_is_sandboxed_and_accessible():
    reg = Registry()
    for name, q in (("static-explainer", GPU), ("step-through", GPU), ("simulation", RES)):
        res = base.run(reg.get(name), RenderRequest(spec(q), depth="learn"))
        assert res.status == "ok", res.detail
        doc = res.artifact
        assert res.verification["csp"]["ok"], res.verification
        assert "default-src 'none'" in doc and not re.search(r"https?://", doc.split("<main>")[1])
        assert "prefers-reduced-motion" in doc and 'lang="en"' in doc
    sim = base.run(reg.get("simulation"), RenderRequest(spec(RES), depth="learn"))
    grid = json.loads(re.search(r'id="grid">(.*?)</script>', sim.artifact, re.S).group(1))
    assert grid["results"]["6|3|3"] == {"slots": 5, "jobs_per_hour": 15.0, "idle_gb": 0.0}
    assert 'type="range"' in sim.artifact and 'aria-live="polite"' in sim.artifact


def test_table_renders_comparison():
    res = base.run(Registry().get("table"), RenderRequest(spec(JEV), depth="learn"))
    assert "| Jev | fast | low | fixed-choice judgments |" in res.artifact


def test_acceptance_e_four_formats_one_spec_are_consistent():
    s, reg = spec(GPU), Registry()
    results = [base.run(reg.get(n), RenderRequest(s, depth="deep"))
               for n in ("structured-prose", "mermaid", "static-explainer", "step-through")]
    rep = critic.consistency(s, results)
    assert rep["consistent"], rep
    assert rep["objectives_covered"] and all(rep["objectives_covered"].values())
    # A diagram that leads with a different cause, or draws an edge the spec lacks, is flagged.
    bad = base.run(reg.get("mermaid"), RenderRequest(s, depth="deep"))
    bad.emphasis = ["c-memory"]
    bad.edges.append(("memory-request", "low-util"))
    rep = critic.consistency(s, results + [bad])
    kinds = {c["kind"] for c in rep["contradictions"]}
    assert not rep["consistent"] and kinds == {"emphasis", "edge"}


def test_acceptance_g_audience_change_reuses_semantics():
    s = spec(GPU)
    h = s.semantic_hash()
    eng = base.run(Registry().get("structured-prose"), RenderRequest(s, depth="learn"))
    ex = base.run(Registry().get("structured-prose"),
                  RenderRequest(s, depth="learn", audience=Audience(role="executive", expertise="novice")))
    assert s.semantic_hash() == h
    assert ex.status == eng.status == "ok"
    assert "GPU pod scheduling" in eng.artifact and "GPU pod scheduling" not in ex.artifact   # less mechanism
    assert {i for i in eng.referenced_ids if i.startswith("c-")} >= {i for i in ex.referenced_ids if i.startswith("c-")}
    assert "c-root" in ex.referenced_ids and "u-root" in ex.referenced_ids
    exec_plan = plan(GPU, audience=Audience(role="executive"))
    assert all(router.score(c, analysis.analyze(s, "learn"), router.RouteContext(audience=Audience(role="executive")))
               [1]["audience"] <= 0 for c in Registry().capabilities() if c.interactive)
    assert exec_plan.primary in ("mermaid", "ste-prose", "structured-prose")


def test_strict_mode_splits_long_sentences_and_uses_canonical_terms():
    s = spec(GPU)
    res = base.run(Registry().get("ste-prose"), RenderRequest(s, depth="learn", options={"prose_mode": "strict"}))
    assert res.status == "ok"
    assert "nodeAffinity" not in res.artifact and "node affinity" in res.artifact
    plain = base.run(Registry().get("ste-prose"), RenderRequest(spec(K8S), depth="summary"))
    assert "(a system that runs containers on a group of machines)" in plain.artifact


def test_selected_elements_narrow_the_render():
    s = spec(GPU)
    res = base.run(Registry().get("structured-prose"), RenderRequest(s, depth="deep", selected=["c-memory"]))
    assert res.status == "ok"
    claims = {i for i in res.referenced_ids if i.startswith("c-")}
    assert "c-memory" in claims and "c-affinity" not in claims
    assert replace(RenderRequest(s), selected=[]).visible(s.claim("c-affinity"))
