"""Controlled prose renderers (spec §27–§28): STE-style text and structured markdown.

Both are template compilers over the IR. They choose, order, split and label spec text; they do not
paraphrase it. Modes:

  plain       novice wording: the first mention of a term gets its plain gloss from `terms`
  technical   spec text as written
  ste         Simplified-Technical-English-inspired: canonical terms, one statement per sentence
  strict      ste + split long sentences at clause boundaries (≤ 25 words where the text allows it)
"""
from __future__ import annotations

import re

from lif.understanding.render.base import RendererCapabilities, RenderRequest, RenderResult, Segment
from lif.understanding.render.common import canonical_terms, claims_in_order, gloss, headline, pct, uncertainty_segments
from lif.understanding.spec import Claim, ExplanationSpec

MAX_WORDS = 25
_SPLIT = (re.compile(r";\s+"), re.compile(r",\s+(?=so\s)"), re.compile(r",\s+(?=and\s)"),
          re.compile(r",\s+(?=which\s)"))


def _mode(req: RenderRequest) -> str:
    m = req.options.get("prose_mode")
    if m:
        return m
    who = req.who
    if who.expertise == "novice" or who.role in ("executive", "general", "student"):
        return "plain"
    return "ste"


def _sentences(text: str, mode: str) -> list[str]:
    parts = [text.strip()]
    if mode in ("ste", "strict"):
        out: list[str] = []
        for p in parts:
            out.extend(s for s in _SPLIT[0].split(p) if s)
        parts = out
    if mode == "strict":
        for rx in _SPLIT[1:]:
            nxt: list[str] = []
            for p in parts:
                nxt.extend(rx.split(p) if len(p.split()) > MAX_WORDS else [p])
            parts = nxt
    fixed = []
    for p in parts:
        p = p.strip().rstrip(",;")
        if p.lower().startswith("so "):
            p = "As a result, " + p[3:]
        if p.lower().startswith("and "):
            p = p[4:]
        if p.lower().startswith("which "):
            p = "This " + p[6:]
        p = p[:1].upper() + p[1:]
        fixed.append(p if p.endswith((".", "?", "!", ")")) else p + ".")
    return fixed


def claim_text(spec: ExplanationSpec, c: Claim, mode: str, glossed: set[str] | None = None) -> str:
    t = c.text if mode == "technical" else canonical_terms(spec, c.text)
    text = " ".join(_sentences(t, mode))
    if mode == "plain" and glossed is not None:
        for cid in c.concepts:
            con = spec.concept(cid)
            g = gloss(spec, cid)
            if con is None or not g or cid in glossed:
                continue
            label = re.escape(con.term or con.label)
            new, n = re.subn(rf"\b({label})\b", rf"\1 ({g})", text, count=1, flags=re.I)
            if n:
                text = new
                glossed.add(cid)
    return text


def _tag(c: Claim) -> str:
    return {"observed": "Observed", "inferred": "Inferred", "assumption": "Assumption"}.get(c.kind, "")


class SteProse:
    """The minimum fallback (§81): always available, LIGHT, offline."""

    def capabilities(self) -> RendererCapabilities:
        return RendererCapabilities(
            name="ste-prose", version="1.0.0", target="STE_PROSE", best_for=("definition", "narrative"),
            outputs=("text/plain",), latency_class="instant", resource_class="LIGHT", clarity=0.7,
            consume_seconds=20, verification=("contract",))

    def accepts(self, spec: ExplanationSpec) -> str | None:
        return None

    def render(self, req: RenderRequest) -> RenderResult:
        spec, mode = req.spec, _mode(req)
        segs: list[Segment] = []
        glossed: set[str] = set()
        head = headline(req)
        shown = [head] + [c for c in claims_in_order(req) if c.id != head.id]
        if req.level == 0:
            shown = [head]
        for c in shown:
            tag = _tag(c) if req.level >= 1 else ""
            txt = claim_text(spec, c, mode, glossed)
            segs.append(Segment(f"{tag}: {txt}" if tag else txt, [c.id] + (c.evidence if tag else [])))
        segs += uncertainty_segments(spec, [c.id for c in shown], short=req.level == 0)
        if req.level >= 2:
            for ex in spec.examples:
                if req.visible(ex):
                    segs.append(Segment(("Counterexample: " if ex.counter else "Example: ") + ex.text, [ex.id]))
        caps = self.capabilities()
        return RenderResult(caps.name, caps.version, caps.target, "\n".join(s.text for s in segs),
                            "text/plain", segs, emphasis=[head.id], verification={"mode": mode})


class StructuredProse:
    """Markdown with the observed/inferred split, factors, evidence, uncertainty and sources (§52, §56–§57)."""

    def capabilities(self) -> RendererCapabilities:
        return RendererCapabilities(
            name="structured-prose", version="1.0.0", target="STRUCTURED_PROSE",
            best_for=("narrative", "definition", "causal"), outputs=("text/markdown",), latency_class="instant",
            resource_class="LIGHT", clarity=0.65, consume_seconds=60)

    def accepts(self, spec: ExplanationSpec) -> str | None:
        return None

    def render(self, req: RenderRequest) -> RenderResult:
        spec, mode = req.spec, _mode(req)
        segs: list[Segment] = []
        H = lambda t: segs.append(Segment(f"\n## {t}", [], "structural"))           # noqa: E731
        glossed: set[str] = set()
        head = headline(req)
        claims = claims_in_order(req)
        H("The short version")
        segs.append(Segment(claim_text(spec, head, mode, glossed), [head.id]))
        segs += uncertainty_segments(spec, [head.id], short=True)
        rest = [c for c in claims if c.id != head.id]
        observed = [c for c in rest if c.kind == "observed"]
        inferred = [c for c in rest if c.kind == "inferred"]
        other = [c for c in rest if c.kind not in ("observed", "inferred")]
        if observed:
            H("Observed")
            for c in observed:
                segs.append(Segment("- " + claim_text(spec, c, mode, glossed), [c.id, *c.evidence]))
        if inferred:
            H("Inferred")
            for c in inferred:
                segs.append(Segment(f"- {claim_text(spec, c, mode, glossed)} (confidence {pct(c.confidence)})",
                                    [c.id, *c.evidence]))
        if other:
            H("Details" if req.level < 2 else "What to know")
            for c in other:
                segs.append(Segment("- " + claim_text(spec, c, mode, glossed), [c.id]))
        if req.level >= 2 and req.who.role != "executive":
            for p in spec.processes:
                if req.visible(p):
                    H(p.name)
                    for i, sid in enumerate(p.steps, 1):
                        con = spec.concept(sid)
                        segs.append(Segment(f"{i}. {con.label}" + (f": {con.definition}" if con.definition else ""),
                                            [sid, p.id]))
        if req.level >= 2:
            exs = [e for e in spec.examples if req.visible(e)]
            if exs:
                H("Examples")
                for e in exs:
                    segs.append(Segment(("- Counterexample: " if e.counter else "- Example: ") + e.text, [e.id]))
            for m in spec.misconceptions:
                if req.visible(m):
                    H("Common misconception")
                    segs.append(Segment(f"- {m.text}", [m.id]))
                    corr = spec.claim(m.correction)
                    segs.append(Segment(f"- Correction: {claim_text(spec, corr, mode)}", [m.correction]))
        unc = uncertainty_segments(spec, [c.id for c in claims if c.id != head.id])
        if unc or spec.uncertainty_for(head.id):
            H("Uncertainty")
            segs += uncertainty_segments(spec, [head.id]) + unc
        if req.options.get("comprehension") and spec.checkpoints:
            H("Check your understanding")
            for cp in spec.checkpoints[:2]:
                segs.append(Segment(f"- {cp.question}", [cp.id]))
        srcs = [s for s in spec.source_refs]
        if srcs and req.level >= 1:
            H("Sources")
            for s in srcs:
                segs.append(Segment(f"- {s.title or s.kind}: `{s.ref}`", [s.id]))
        caps = self.capabilities()
        md = "\n".join(s.text for s in segs).strip() + "\n"
        return RenderResult(caps.name, caps.version, caps.target, md, "text/markdown", segs,
                            emphasis=[head.id], verification={"mode": mode})
