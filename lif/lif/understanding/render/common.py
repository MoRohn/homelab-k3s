"""Helpers shared by renderers: element selection at a depth, terminology, uncertainty wording."""
from __future__ import annotations

import html
import re

from lif.understanding.render.base import RenderRequest, Segment
from lif.understanding.spec import Claim, ExplanationSpec


def pct(x: float) -> str:
    return f"{x * 100:g}%"


def claims_in_order(req: RenderRequest) -> list[Claim]:
    """Visible claims: the summary's order first, then primary, supporting, detail."""
    spec = req.spec
    order = [spec.summary.headline, *spec.summary.claims]
    rank = {"primary": 0, "supporting": 1, "detail": 2}
    vis = [c for c in spec.claims if req.visible(c)]
    return sorted(vis, key=lambda c: (order.index(c.id) if c.id in order else len(order), rank[c.importance]))


def headline(req: RenderRequest) -> Claim:
    c = req.spec.claim(req.spec.summary.headline)
    assert c is not None
    return c


def uncertainty_segments(spec: ExplanationSpec, claim_ids: list[str], *, short: bool = False) -> list[Segment]:
    """One segment per uncertainty attached to the claims shown, so uncertainty survives every target."""
    out: list[Segment] = []
    for cid in dict.fromkeys(claim_ids):
        for u in spec.uncertainty_for(cid):
            text = f"Confidence {pct(u.confidence)}. {u.reason}"
            if u.evidence_needed and not short:
                text += " Evidence needed: " + "; ".join(u.evidence_needed) + "."
            out.append(Segment(text, [u.id, cid]))
    return out


def canonical_terms(spec: ExplanationSpec, text: str) -> str:
    """Replace synonyms with the canonical term (§28). Abbreviations stay: they are deliberate."""
    for t in spec.terms:
        for syn in t.synonyms:
            text = re.sub(rf"(?<![\w-]){re.escape(syn)}(?![\w-])", t.canonical, text, flags=re.I)
    return text


def gloss(spec: ExplanationSpec, concept_id: str) -> str:
    """Plain wording for a concept's term, for novice readers."""
    c = spec.concept(concept_id)
    if c is None or not c.term:
        return ""
    t = spec.term_for(c.term)
    return t.plain if t is not None else ""


def esc(s: str) -> str:
    return html.escape(s, quote=True)

