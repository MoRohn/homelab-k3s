"""Explanation analysis: exact features of the understanding problem (spec §7).

Pure code over the ExplanationSpec at a given depth (only elements with level ≤ depth count) and the
question's wording. The router scores representations from these features; Jev only ever sees them,
never the question text or the cluster state behind the spec.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from lif.understanding.spec import DEPTH_LEVEL, ExplanationSpec

CAUSAL_RELS = {"causes", "blocks", "limits", "prevents", "contradicts", "supports"}
DEPENDENCY_RELS = {"depends_on", "routes_to", "consumes", "produces", "uses", "maps_to", "competes_with"}
SPATIAL_RELS = {"contains", "scheduled_on", "part_of", "routes_to"}
TEMPORAL_RELS = {"precedes", "follows"}
WPM = 200

# Question shape (§7, §50). Ordered: first match wins.
QUESTION_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = tuple((k, re.compile(p, re.I)) for k, p in (
    ("parameter", r"\b(how does|what happens (if|when)|what if)\b.*\b(chang\w*|increas\w*|decreas\w*|more|less|"
                  r"fewer|vary\w*|adjust\w*|affect\w*)\b|\bsensitiv\w*\b|\btrade-?off\b"),
    ("teach", r"^\s*(teach me|help me understand|walk me through|explain .* from scratch)\b"),
    ("comparative", r"\b(compare|comparison|versus|vs\.?|difference between|which is better)\b"),
    ("temporal", r"\b(what changed|timeline|history|sequence of events|when did|after the last)\b"),
    ("debug", r"\b(why (is|are|does|do|did)n?'?t?\b.*\b(fail\w*|slow|low|high|idle|stuck|pending|broken|error\w*)|"
              r"not (working|reaching|starting|running))\b"),
    ("causal", r"^\s*(why|what causes|what caused|how come)\b"),
    ("procedural", r"^\s*(how (do|does|to|can)|walk me through|what are the steps)\b"),
    ("definition", r"^\s*(what('s| is| are)|define|who is)\b"),
))


def question_kind(q: str) -> str:
    return next((k for k, p in QUESTION_KINDS if p.search(q)), "other")


@dataclass
class Features:
    depth: str
    question_kind: str
    concept_count: int = 0
    claim_count: int = 0
    evidence_count: int = 0
    observed_claims: int = 0
    inferred_claims: int = 0
    relationship_count: int = 0
    relationship_density: float = 0.0       # relationships per concept
    causal_edges: int = 0
    process_steps: int = 0
    timeline_events: int = 0
    hierarchy_edges: int = 0
    comparison_cells: int = 0
    equation_count: int = 0
    variable_count: int = 0
    has_simulation: bool = False
    uncertainty_count: int = 0
    words: int = 0
    reading_seconds: float = 0.0
    complexity: float = 0.0                 # 0..1
    structure: dict[str, float] = field(default_factory=dict)   # Structure kind → strength 0..1

    def to_dict(self) -> dict:
        return asdict(self)

    def public_state(self) -> dict:
        """What a Jev decision may see: derived numbers and categories, nothing from the source."""
        return {"question_kind": self.question_kind, "depth": self.depth,
                "structure": {k: round(v, 2) for k, v in sorted(self.structure.items())},
                "counts": {"concepts": self.concept_count, "claims": self.claim_count,
                           "relationships": self.relationship_count, "causal_edges": self.causal_edges,
                           "process_steps": self.process_steps, "timeline_events": self.timeline_events,
                           "comparison_cells": self.comparison_cells, "variables": self.variable_count,
                           "uncertainties": self.uncertainty_count},
                "has_simulation": self.has_simulation, "reading_seconds": round(self.reading_seconds),
                "complexity": round(self.complexity, 2)}


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def analyze(spec: ExplanationSpec, depth: str = "learn", question: str | None = None) -> Features:
    lv = DEPTH_LEVEL.get(depth, 2)
    vis = lambda xs: [x for x in xs if x.level <= lv]          # noqa: E731
    concepts, claims, rels = vis(spec.concepts), vis(spec.claims), vis(spec.relationships)
    chains, procs, tls = vis(spec.causal_chains), vis(spec.processes), vis(spec.timelines)
    hier, cmps, eqs, vars_ = vis(spec.hierarchies), vis(spec.comparisons), vis(spec.equations), vis(spec.variables)
    qk = question_kind(question if question is not None else spec.question)

    f = Features(depth=depth, question_kind=qk)
    f.concept_count, f.claim_count = len(concepts), len(claims)
    f.evidence_count = len(vis(spec.evidence))
    f.observed_claims = sum(1 for c in claims if c.kind == "observed")
    f.inferred_claims = sum(1 for c in claims if c.kind == "inferred")
    f.relationship_count = len(rels)
    f.relationship_density = round(len(rels) / max(len(concepts), 1), 3)
    f.causal_edges = sum(len(c.steps) - 1 for c in chains) + sum(1 for r in rels if r.type in CAUSAL_RELS)
    f.process_steps = sum(len(p.steps) for p in procs)
    f.timeline_events = sum(len([e for e in t.events if e.level <= lv]) for t in tls)
    f.hierarchy_edges = sum(len(h.edges) for h in hier)
    f.comparison_cells = sum(len(c.cells) for c in cmps)
    f.equation_count, f.variable_count = len(eqs), len(vars_)
    f.has_simulation = spec.simulation is not None and bool(vars_)
    f.uncertainty_count = len(vis(spec.uncertainties))
    text = " ".join([c.text for c in claims] + [c.definition for c in concepts])
    f.words = len(text.split())
    f.reading_seconds = round(f.words / WPM * 60, 1)

    dep = sum(1 for r in rels if r.type in DEPENDENCY_RELS)
    spatial = sum(1 for r in rels if r.type in SPATIAL_RELS) + f.hierarchy_edges
    temporal_q = 0.3 if qk == "temporal" else 0.0
    s = {
        "definition": 1.0 if qk == "definition" and f.relationship_count <= 3 and not chains else
                      (0.5 if f.concept_count <= 3 and f.relationship_count <= 1 else 0.1),
        "causal": _clip(f.causal_edges / 4),
        "process": _clip(f.process_steps / 5),
        "temporal": _clip(f.timeline_events / 4 + temporal_q + sum(1 for r in rels if r.type in TEMPORAL_RELS) / 4),
        "hierarchy": _clip(f.hierarchy_edges / 5),
        "comparison": _clip(f.comparison_cells / 8),
        "dependency": _clip(dep / 4),
        "spatial": _clip(spatial / 4),
        "quantitative": _clip((f.equation_count + f.variable_count) / 4),
        "parameter": (1.0 if qk == "parameter" else 0.7) if f.has_simulation else 0.0,
        "narrative": round(0.6 * _clip(f.claim_count / 12), 3),
    }
    f.structure = {k: round(v, 3) for k, v in s.items()}
    f.complexity = round(_clip((f.concept_count / 12 + f.relationship_density / 2 + f.causal_edges / 8
                                + f.process_steps / 10 + f.uncertainty_count / 4) / 2.5), 3)
    return f
