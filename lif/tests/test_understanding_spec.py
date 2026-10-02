"""ExplanationSpec/v1 schema, migration and semantic validation (spec §13–§28, §23, §115)."""
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from lif.understanding import packages, simulate, validate as V
from lif.understanding.spec import ExplanationSpec, load, migrate

GPU_Q = "Why are these Kubernetes pods not reaching available GPUs?"


def base() -> dict:
    return copy.deepcopy(packages.find(GPU_Q).spec.to_json())


def codes(doc: dict, resolver=V.syntax_resolver) -> set[str]:
    return {i.code for i in V.validate(load(doc), resolver).errors}


def test_packages_validate_clean():
    es = packages.entries()
    assert {e.name for e in es} >= {"what-is-kubernetes", "pods-not-reaching-gpus", "reservation-throughput",
                                    "jev-cascade"}
    for e in es:
        r = V.validate(e.spec)
        assert r.ok, (e.ref, [i.message for i in r.errors])


def test_schema_rejects_unknown_fields_and_bad_ids():
    d = base()
    d["claims"][0]["colour"] = "red"
    with pytest.raises(ValidationError):
        load(d)
    d = base()
    d["concepts"][0]["id"] = "Bad ID"
    with pytest.raises(ValidationError):
        load(d)


def test_migrate_refuses_unknown_versions():
    assert migrate({"schema_version": "ExplanationSpec/v1"})["schema_version"] == "ExplanationSpec/v1"
    with pytest.raises(ValueError):
        migrate({"schema_version": "ExplanationSpec/v9"})


def test_semantic_hash_ignores_metadata_and_hints():
    a = load(base())
    d = base()
    d["metadata"]["created_at"] = 1.0
    d["render_hints"] = [{"kind": "prefer", "value": "diagram"}]
    assert load(d).semantic_hash() == a.semantic_hash()
    d["claims"][0]["text"] = "Something else."
    assert load(d).semantic_hash() != a.semantic_hash()


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d["concepts"].append(dict(d["concepts"][0])), "duplicate-id"),
    (lambda d: d["claims"][0]["concepts"].append("ghost"), "unknown-ref"),
    (lambda d: d["relationships"][0].update({"to": "c-root"}), "wrong-kind"),
    (lambda d: d["claims"][1].update({"evidence": []}), "observed-without-evidence"),
    (lambda d: d["claims"][0].update({"concepts": []}), "orphan-claim"),
    (lambda d: d["causal_chains"][0]["steps"].append("nope"), "unknown-ref"),
    (lambda d: d["causal_chains"][0].update({"steps": ["affinity"]}), "short-chain"),
    (lambda d: d["processes"][0]["steps"].append("missing-step"), "unknown-ref"),
    (lambda d: d["timelines"][0]["events"].reverse(), "unordered-timeline"),
    (lambda d: d["uncertainties"].clear(), "hidden-uncertainty"),
    (lambda d: d["uncertainties"][0].update({"confidence": 0.5}), "confidence-mismatch"),
    (lambda d: d["uncertainties"][0].update({"level": 3}), "uncertainty-too-deep"),
    (lambda d: d["summary"].update({"headline": "affinity"}), "wrong-kind"),
    (lambda d: d["evidence"][0].update({"source": "src-nowhere"}), "unknown-ref"),
    (lambda d: d["terms"].append({"canonical": "Other", "synonyms": ["scheduler"]}), "term-collision"),
    (lambda d: d["concepts"][1].update({"term": "Not a term"}), "unknown-term"),
    (lambda d: d["render_hints"].append({"kind": "emphasize", "targets": ["ghost"]}), "unknown-ref"),
    (lambda d: d["misconceptions"][0].update({"correction": "c-ghost"}), "unknown-ref"),
])
def test_semantic_errors(mutate, code):
    d = base()
    mutate(d)
    assert code in codes(d)


def test_source_refs_resolve_through_pluggable_resolver():
    d = base()
    d["source_refs"].append({"id": "src-kn", "kind": "knowledge", "ref": "not a key"})
    assert "unresolved-source" in codes(d)
    d["source_refs"][-1]["ref"] = "local-intelligence-fabric::jev-decision-fabric"
    assert "unresolved-source" not in codes(d)
    known = {"local-intelligence-fabric::jev-decision-fabric"}
    assert "unresolved-source" not in codes(d, V.knowledge_resolver(known.__contains__))
    d["source_refs"][-1]["ref"] = "local-intelligence-fabric::does-not-exist"
    assert "unresolved-source" in codes(d, V.knowledge_resolver(known.__contains__))


def test_equations_resolve_variables():
    sim = packages.find("How does changing GPU reservation affect throughput?").spec.to_json()
    sim["equations"][0]["expr"] = "slots = floor((M - R - X) / m)"
    assert "unresolved-variable" in codes(sim)


def test_simulation_bindings_checked():
    sim = packages.find("How does changing GPU reservation affect throughput?").spec.to_json()
    bad = copy.deepcopy(sim)
    bad["simulation"]["primitive"] = "nope/v1"
    assert "unknown-primitive" in codes(bad)
    bad = copy.deepcopy(sim)
    bad["simulation"]["outputs"].append({"key": "made_up", "label": "x"})
    assert "bad-output" in codes(bad)
    bad = copy.deepcopy(sim)
    del bad["simulation"]["bindings"]["v-job"]
    assert {"unbound-input", "unbound-control"} <= codes(bad)


def test_unplaced_claim_is_a_warning():
    d = base()
    d["claims"].append({"id": "c-extra", "text": "An extra point.", "concepts": ["pod"]})
    r = V.validate(load(d))
    assert r.ok and any(i.code == "unplaced-claim" for i in r.warnings)


def test_simulation_primitives_are_deterministic():
    spec = packages.find("How does changing GPU reservation affect throughput?").spec
    assert simulate.run(spec) == simulate.run(spec) == {"slots": 5, "jobs_per_hour": 15.0, "idle_gb": 0.0}
    assert simulate.run(spec, {"v-reserved": 96})["slots"] == 2
    g = simulate.grid(spec)
    assert g["controls"] == ["v-reserved", "v-job", "v-minutes"]
    assert len(g["results"]) == 13 * 8 * 12
    p = simulate.PRIMITIVES["gpu-placement/v1"].fn
    strict = p({"gpus": 2, "gpu_mem_gb": 40, "pods": 3, "pod_mem_gb": 20, "affinity": "strict", "eligible_gpus": 1})
    assert strict == {"placed": 2, "pending": 1, "utilization_pct": 50.0, "idle_gpus": 1, "pending_reason": "affinity"}
    relaxed = p({"gpus": 2, "gpu_mem_gb": 40, "pods": 3, "pod_mem_gb": 20, "affinity": "preferred",
                 "eligible_gpus": 1})
    assert relaxed["pending"] == 0 and relaxed["idle_gpus"] == 0


def test_spec_lookups():
    s: ExplanationSpec = packages.find(GPU_Q).spec
    assert s.get("c-root").confidence == 0.78
    assert [u.id for u in s.uncertainty_for("c-root")] == ["u-root"]
    assert s.term_for("kube-scheduler").canonical == "Kubernetes scheduler"
    assert s.index()["t-failed"][0] == "timeline_events"
