"""Explanation critic (spec §58–§62, §68, §105): semantic checks first, presentation second.

Semantic (blocking):
  contract          every artifact passed the renderer contract (no unknown IDs, no new words or
                    numbers, no dropped uncertainty)
  emphasis          every artifact that leads with a claim leads with the spec's headline (§68: the
                    summary and the diagram cannot disagree about the main cause)
  edges             every connection an artifact draws exists in the spec (chain step, process step,
                    relationship or hierarchy edge)
Coverage (advisory):
  objectives        each visible learning objective's claims appear in at least one artifact
  checkpoints       each checkpoint's answer claims appear, so the question is answerable from the result
Presentation (advisory unless a renderer marked it failed):
  each renderer's own verification block (syntax, CSP, overlap, bindings)
"""
from __future__ import annotations

from itertools import pairwise

from lif.understanding.render.base import OK, RenderResult
from lif.understanding.spec import DEPTH_LEVEL, ExplanationSpec


def spec_edges(spec: ExplanationSpec) -> set[tuple[str, str]]:
    e: set[tuple[str, str]] = set()
    for ch in spec.causal_chains:
        e.update(pairwise(ch.steps))
    for p in spec.processes:
        e.update(pairwise(p.steps))
    for r in spec.relationships:
        e.add((r.source, r.target))
    for h in spec.hierarchies:
        e.update((x.parent, x.child) for x in h.edges)
    return e


def evaluate(spec: ExplanationSpec, results: list[RenderResult], depth: str = "learn") -> dict:
    claim_ids = {c.id for c in spec.claims}
    allowed_edges = spec_edges(spec)
    contradictions: list[dict] = []
    per: dict[str, dict] = {}
    cited: set[str] = set()
    for r in results:
        if r.status != OK:
            per[r.renderer] = {"status": r.status, "detail": r.detail}
            continue
        ids = set(r.referenced_ids)
        cited |= ids
        problems = list((r.verification.get("contract") or {}).get("problems") or [])
        lead = next((e for e in r.emphasis if e in claim_ids), None)
        if lead and lead != spec.summary.headline:
            contradictions.append({"renderer": r.renderer, "kind": "emphasis",
                                   "detail": f"leads with {lead}, the spec's headline is {spec.summary.headline}"})
        for a, b in r.edges:
            if (a, b) not in allowed_edges:
                contradictions.append({"renderer": r.renderer, "kind": "edge",
                                       "detail": f"draws {a} → {b}, which the spec does not relate"})
        visual = {k: v for k, v in r.verification.items() if isinstance(v, dict) and "ok" in v and k != "contract"}
        per[r.renderer] = {"status": r.status, "claims": sorted(ids & claim_ids),
                           "uncertainties": sorted(i for i in ids if spec.index().get(i, ("",))[0] == "uncertainties"),
                           "contract_problems": problems,
                           "presentation": {k: v["ok"] for k, v in visual.items()}}
    lv = DEPTH_LEVEL.get(depth, 2)
    objectives = {o.id: all(c in cited for c in o.claims) for o in spec.learning_objectives if o.level <= lv}
    checkpoints = {c.id: all(a in cited for a in c.answer_claims) for c in spec.checkpoints if c.level <= lv}
    primary = [c.id for c in spec.claims if c.importance == "primary" and c.level <= lv]
    missing_primary = [c for c in primary if c not in cited]
    presentation_ok = all(all(v["presentation"].values()) for v in per.values() if "presentation" in v)
    ok = not contradictions and all(v["status"] == OK for v in per.values())
    return {"ok": ok, "semantic_ok": not contradictions, "presentation_ok": presentation_ok,
            "contradictions": contradictions, "objectives_covered": objectives, "checkpoints_answerable": checkpoints,
            "missing_primary_claims": missing_primary, "renderers": per,
            "summary": {"artifacts": len(results), "ok_artifacts": sum(1 for r in results if r.status == OK),
                        "claims_cited": len(cited & claim_ids), "claims_total": len(claim_ids)}}


def consistency(spec: ExplanationSpec, results: list[RenderResult]) -> dict:
    """Acceptance E: across artifacts of one spec, do claims, relationships, numbers and uncertainty agree?"""
    rep = evaluate(spec, results, "deep")
    by = {r.renderer: r for r in results if r.status == OK}
    unc = {name: set(v["uncertainties"]) for name, v in rep["renderers"].items() if "uncertainties" in v}
    head_unc = {u.id for u in spec.uncertainty_for(spec.summary.headline)}
    drops = [n for n, us in unc.items() if spec.summary.headline in by[n].referenced_ids and not head_unc <= us]
    return {"consistent": rep["semantic_ok"] and not drops and all(not v.get("contract_problems")
                                                                    for v in rep["renderers"].values()),
            "uncertainty_dropped_by": drops, **rep}
