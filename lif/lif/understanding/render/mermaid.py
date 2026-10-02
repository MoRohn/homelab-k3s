"""Mermaid renderer (spec §29): ExplanationSpec → DiagramPlan → Mermaid source.

The plan picks one diagram family from the structure the spec has at this depth (or the
`diagram_family` option, e.g. from the Jev `understanding-diagram-family` decision):

  causal      flowchart LR over the first visible causal chain plus causal relationships
  process     flowchart LR over the first visible process
  dependency  flowchart TD over the visible relationships
  timeline    mermaid `timeline` over the first visible timeline

Every node is a spec concept (label from the spec), every edge is a chain step, a process step or a
relationship. Inferred edges are dashed; uncertainty becomes a note node linked to what it qualifies.
accTitle/accDescr carry the text alternative (§88). Verification is a static syntax check: this host
has no mermaid-cli, so the diagram is not rasterised here and the result says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from lif.understanding.analysis import analyze
from lif.understanding.render.base import RendererCapabilities, RenderRequest, RenderResult, Segment, SemanticGap
from lif.understanding.render.common import headline, pct
from lif.understanding.spec import ExplanationSpec

FAMILIES = ("causal", "process", "dependency", "timeline")
_ID = re.compile(r"[^A-Za-z0-9_]")


def mid(i: str) -> str:
    return "n_" + _ID.sub("_", i)


def q(s: str) -> str:
    return '"' + s.replace('"', "#quot;").replace("\n", " ") + '"'


@dataclass
class DiagramPlan:
    family: str
    direction: str = "LR"
    nodes: list[str] = field(default_factory=list)                     # concept ids
    edges: list[tuple[str, str, str, str, bool]] = field(default_factory=list)   # (from, to, label, ref, dashed)
    notes: list[tuple[str, str, str]] = field(default_factory=list)    # (note id, text, attached concept)
    source: str = ""                                                   # chain/process/timeline id


def choose_family(req: RenderRequest) -> str:
    fam = req.options.get("diagram_family")
    if fam in FAMILIES:
        return fam
    s = analyze(req.spec, req.depth).structure
    vis = req.visible
    ranked = sorted(
        [("causal", s["causal"] if any(vis(c) for c in req.spec.causal_chains) else 0.0),
         ("process", s["process"] if any(vis(p) for p in req.spec.processes) else 0.0),
         ("timeline", s["temporal"] if any(vis(t) for t in req.spec.timelines) else 0.0),
         ("dependency", max(s["dependency"], s["spatial"], 0.01) if any(vis(r) for r in req.spec.relationships)
          else 0.0)], key=lambda kv: -kv[1])
    if ranked[0][1] <= 0:
        raise SemanticGap("no causal chain, process, timeline or relationship is visible at this depth")
    return ranked[0][0]


def plan(req: RenderRequest) -> DiagramPlan:
    spec, fam = req.spec, choose_family(req)
    p = DiagramPlan(fam, direction="TD" if fam == "dependency" else "LR")
    add = lambda n: p.nodes.append(n) if n not in p.nodes else None          # noqa: E731
    dashed_claim = lambda cid: bool(cid) and (c := spec.claim(cid)) is not None and c.kind == "inferred"  # noqa: E731
    if fam == "causal":
        ch = next(c for c in spec.causal_chains if req.visible(c))
        p.source = ch.id
        for a, b in zip(ch.steps, ch.steps[1:]):
            add(a), add(b)
            p.edges.append((a, b, "", ch.id, dashed_claim(ch.claim)))
        rels = [r for r in spec.relationships if req.visible(r) and (r.source in p.nodes or r.target in p.nodes)]
    elif fam == "process":
        pr = next(x for x in spec.processes if req.visible(x))
        p.source = pr.id
        for a, b in zip(pr.steps, pr.steps[1:]):
            add(a), add(b)
            p.edges.append((a, b, "", pr.id, False))
        if len(pr.steps) == 1:
            add(pr.steps[0])
        rels = [r for r in spec.relationships if req.visible(r) and r.source in p.nodes and r.target in p.nodes]
    elif fam == "dependency":
        rels = [r for r in spec.relationships if req.visible(r)]
    else:
        tl = next(t for t in spec.timelines if req.visible(t))
        p.source = tl.id
        return p
    for r in rels:
        if any(e[3] == r.id for e in p.edges):
            continue
        add(r.source), add(r.target)
        p.edges.append((r.source, r.target, r.label or r.type.replace("_", " "), r.id, dashed_claim(r.claim)))
    claims = {spec.summary.headline} | {e[3] for e in p.edges}
    for ref in list(claims):
        el = spec.get(ref)
        if el is not None and getattr(el, "claim", ""):
            claims.add(el.claim)
    for cid in claims:
        for u in spec.uncertainty_for(cid):
            if req.visible(u):
                target = next((s for s in reversed(p.nodes) if s in (spec.claim(cid).concepts if spec.claim(cid)
                                                                      else [])), p.nodes[-1] if p.nodes else "")
                if target:
                    p.notes.append((u.id, f"Confidence {pct(u.confidence)}: {u.reason}", target))
    return p


def check_syntax(src: str) -> list[str]:
    """A static check for what this renderer emits (no mermaid-cli on this host)."""
    errs = []
    lines = [ln for ln in src.splitlines() if ln.strip() and not ln.strip().startswith("%%")]
    if not lines or not re.match(r"^(flowchart (LR|TD|TB|RL)|timeline)\s*$", lines[0]):
        errs.append(f"unknown diagram header {lines[0] if lines else ''!r}")
    for ln in lines[1:]:
        s = ln.strip()
        if s.count('"') % 2:
            errs.append(f"unbalanced quotes: {s[:60]}")
        for o, c in ("[]", "()", "{}"):
            if s.count(o) != s.count(c) and not s.startswith(("accDescr", "accTitle")):
                errs.append(f"unbalanced {o}{c}: {s[:60]}")
    return errs


class MermaidRenderer:
    def capabilities(self) -> RendererCapabilities:
        return RendererCapabilities(
            name="mermaid", version="1.0.0", target="MERMAID",
            best_for=("causal", "process", "dependency", "temporal", "spatial"),
            unsupported=("comparison",),
            outputs=("text/vnd.mermaid",), latency_class="fast", resource_class="CPU", clarity=0.85,
            consume_seconds=40, verification=("contract", "syntax"))

    def accepts(self, spec: ExplanationSpec) -> str | None:
        if not (spec.causal_chains or spec.processes or spec.relationships or spec.timelines):
            return "the spec has no chain, process, timeline or relationship to draw"
        return None

    def render(self, req: RenderRequest) -> RenderResult:
        spec = req.spec
        p = plan(req)
        segs: list[Segment] = []
        head = headline(req)
        out: list[str] = []
        if p.family == "timeline":
            tl = spec.get(p.source)
            out += ["timeline", f"    title {tl.label}"]
            segs.append(Segment(tl.label, [tl.id], "label"))
            for e in tl.events:
                if req.visible(e):
                    out.append(f"    {e.t:g}{tl.unit} : {e.label}")
                    segs.append(Segment(f"{e.t:g} {e.label}", [e.id]))
        else:
            out.append(f"flowchart {p.direction}")
            out.append(f"    accTitle: {head.text}")
            segs.append(Segment(head.text, [head.id]))
            steps = " then ".join(spec.concept(n).label for n in p.nodes)
            out.append(f"    accDescr: {steps}")
            segs.append(Segment(steps, p.nodes))
            for n in p.nodes:
                c = spec.concept(n)
                shape = f"([{q(c.label)}])" if c.kind in ("state", "event") else f"[{q(c.label)}]"
                out.append(f"    {mid(n)}{shape}")
                segs.append(Segment(c.label, [n], "label"))
            for a, b, label, ref, dashed in p.edges:
                arrow = "-.->" if dashed else "-->"
                out.append(f"    {mid(a)} {arrow}|{q(label)}| {mid(b)}" if label else f"    {mid(a)} {arrow} {mid(b)}")
                if label:
                    segs.append(Segment(label, [ref], "label"))
                else:
                    segs.append(Segment("", [ref], "structural"))
            for nid, text, target in p.notes:
                out.append(f"    {mid(nid)}[/{q(text)}/]:::uncertain")
                out.append(f"    {mid(nid)} -.- {mid(target)}")
                segs.append(Segment(text, [nid, spec.get(nid).about]))
            out.append("    classDef uncertain stroke-dasharray: 4 3,stroke:#d6a446,color:#d6a446")
        src = "\n".join(out) + "\n"
        caps = self.capabilities()
        errs = check_syntax(src)
        res = RenderResult(caps.name, caps.version, caps.target, src, "text/vnd.mermaid", segs,
                           emphasis=[head.id], edges=[(a, b) for a, b, *_ in p.edges],
                           verification={"syntax": {"ok": not errs, "errors": errs, "method": "static",
                                                    "rasterised": False},
                                         "family": p.family})
        if errs:
            res.status = "failed"
            res.detail = "syntax: " + "; ".join(errs)
        return res
