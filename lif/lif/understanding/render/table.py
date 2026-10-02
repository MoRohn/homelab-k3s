"""Table renderer: comparisons as markdown tables (spec §8 TABLE)."""
from __future__ import annotations

from lif.understanding.render.base import RendererCapabilities, RenderRequest, RenderResult, Segment, SemanticGap
from lif.understanding.spec import ExplanationSpec


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


class TableRenderer:
    def capabilities(self) -> RendererCapabilities:
        return RendererCapabilities(
            name="table", version="1.0.0", target="TABLE", best_for=("comparison",),
            unsupported=("causal", "process", "parameter"), outputs=("text/markdown",), latency_class="instant",
            resource_class="LIGHT", clarity=0.8, consume_seconds=30)

    def accepts(self, spec: ExplanationSpec) -> str | None:
        return None if spec.comparisons else "the spec has no comparison"

    def render(self, req: RenderRequest) -> RenderResult:
        spec = req.spec
        cmps = [c for c in spec.comparisons if req.visible(c)]
        if not cmps:
            raise SemanticGap("no comparison is visible at this depth")
        segs: list[Segment] = []
        for cmp in cmps:
            segs.append(Segment(f"\n**{cmp.label}**\n", [cmp.id]))
            head = "| | " + " | ".join(_cell(d.label) for d in cmp.dimensions) + " |"
            segs.append(Segment(head, [cmp.id]))
            segs.append(Segment("|---" * (len(cmp.dimensions) + 1) + "|", [], "structural"))
            cells = {(c.item, c.dimension): c for c in cmp.cells}
            for item in cmp.items:
                con = spec.concept(item)
                vals = [cells.get((item, d.id)) for d in cmp.dimensions]
                row = f"| {_cell(con.label)} | " + " | ".join(_cell(v.value) if v else "" for v in vals) + " |"
                refs = [item, cmp.id] + [v.claim for v in vals if v and v.claim]
                segs.append(Segment(row, refs))
        caps = self.capabilities()
        return RenderResult(caps.name, caps.version, caps.target, "\n".join(s.text for s in segs).strip() + "\n",
                            "text/markdown", segs, emphasis=[cmps[0].id])
