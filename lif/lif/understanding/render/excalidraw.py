"""Excalidraw renderer (spec §30): a semantic sketch whose geometry the renderer owns.

Nodes are the visible concepts of the primary chain plus related concepts; layers come from the
longest path over the drawn edges (cause on the left). Each concept is a rectangle with bound text,
each edge an arrow bound to both ends, and each uncertainty a dashed note. Layout is a grid, so boxes
never overlap. Verification checks exactly that, plus that every arrow binds two drawn shapes.
"""
from __future__ import annotations

import hashlib
import json

from lif.understanding.render.base import RendererCapabilities, RenderRequest, RenderResult, Segment
from lif.understanding.render.common import headline, pct
from lif.understanding.render.mermaid import plan
from lif.understanding.spec import ExplanationSpec

CHAR_W, BOX_H, GAP_X, GAP_Y, PAD = 9, 64, 90, 40, 24
GREEN, INK, AMBER = "#00a35d", "#1e1e1e", "#d49a12"


def _seed(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest()[:8], 16)


def _base(id_: str, kind: str, x: float, y: float, w: float, h: float, **kw) -> dict:
    return {"id": id_, "type": kind, "x": x, "y": y, "width": w, "height": h, "angle": 0, "strokeColor": INK,
            "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 2, "strokeStyle": "solid",
            "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None, "roundness": {"type": 3},
            "seed": _seed(id_), "version": 1, "versionNonce": _seed(id_ + "v"), "isDeleted": False,
            "boundElements": [], "updated": 1, "link": None, "locked": False, **kw}


def _text(id_: str, text: str, container: str | None, x: float, y: float, w: float, size: int = 18) -> dict:
    return _base(id_, "text", x, y, w, size * 1.25, text=text, originalText=text, fontSize=size, fontFamily=1,
                 textAlign="center", verticalAlign="middle", containerId=container, lineHeight=1.25,
                 baseline=size, autoResize=True, roundness=None)


def layers(nodes: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest-path layering; edges that would close a cycle are ignored for layering only."""
    layer = {n: 0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for a, b in edges:
            if a in layer and b in layer and layer[b] < layer[a] + 1 <= len(nodes):
                layer[b] = layer[a] + 1
                changed = True
        if not changed:
            break
    return layer


class ExcalidrawRenderer:
    def capabilities(self) -> RendererCapabilities:
        return RendererCapabilities(
            name="excalidraw", version="1.0.0", target="EXCALIDRAW", best_for=("causal", "spatial", "dependency"),
            unsupported=("comparison", "temporal", "parameter"), outputs=("application/vnd.excalidraw+json",),
            latency_class="fast", resource_class="CPU", clarity=0.75, consume_seconds=45, mobile=False,
            verification=("contract", "no-overlap", "bindings"))

    def accepts(self, spec: ExplanationSpec) -> str | None:
        return None if spec.causal_chains or spec.relationships else "nothing to sketch: no chain or relationship"

    def render(self, req: RenderRequest) -> RenderResult:
        spec = req.spec
        p = plan(RenderRequest(**{**req.__dict__, "options": {**req.options, "diagram_family":
                                  req.options.get("diagram_family") if req.options.get("diagram_family") in
                                  ("causal", "dependency", "process") else None}}))
        segs: list[Segment] = []
        edges = [(a, b) for a, b, *_ in p.edges]
        lay = layers(p.nodes, edges)
        cols: dict[int, list[str]] = {}
        for n in p.nodes:
            cols.setdefault(lay[n], []).append(n)
        width = {n: max(140, CHAR_W * len(spec.concept(n).label) + 2 * PAD) for n in p.nodes}
        col_w = {c: max(width[n] for n in ns) for c, ns in cols.items()}
        x_of, x = {}, 0.0
        for c in sorted(cols):
            x_of[c] = x
            x += col_w[c] + GAP_X
        els: list[dict] = []
        box: dict[str, dict] = {}
        for c, ns in cols.items():
            for r, n in enumerate(ns):
                bx, by, w = x_of[c] + (col_w[c] - width[n]) / 2, 120 + r * (BOX_H + GAP_Y), width[n]
                rect = _base(f"box-{n}", "rectangle", bx, by, w, BOX_H, customData={"spec_id": n},
                             backgroundColor="#e8fff3" if spec.concept(n).kind in ("state", "event") else "transparent")
                label = spec.concept(n).label
                t = _text(f"txt-{n}", label, rect["id"], bx + PAD / 2, by + BOX_H / 2 - 11, w - PAD)
                rect["boundElements"].append({"id": t["id"], "type": "text"})
                els += [rect, t]
                box[n] = rect
                segs.append(Segment(label, [n], "label"))
        for k, (a, b, label, ref, dashed) in enumerate(p.edges):
            A, B = box[a], box[b]
            sx, sy = A["x"] + A["width"], A["y"] + BOX_H / 2
            ex, ey = B["x"], B["y"] + BOX_H / 2
            if B["x"] <= A["x"]:                       # back edge: leave from the bottom
                sx, sy, ex, ey = A["x"] + A["width"] / 2, A["y"] + BOX_H, B["x"] + B["width"] / 2, B["y"] + BOX_H
            arrow = _base(f"arr-{k}", "arrow", sx, sy, ex - sx, ey - sy, points=[[0, 0], [ex - sx, ey - sy]],
                          strokeColor=GREEN, strokeStyle="dashed" if dashed else "solid", roundness={"type": 2},
                          startBinding={"elementId": A["id"], "focus": 0, "gap": 4},
                          endBinding={"elementId": B["id"], "focus": 0, "gap": 4}, startArrowhead=None,
                          endArrowhead="arrow", customData={"spec_id": ref}, lastCommittedPoint=None)
            A["boundElements"].append({"id": arrow["id"], "type": "arrow"})
            B["boundElements"].append({"id": arrow["id"], "type": "arrow"})
            els.append(arrow)
            if label:
                t = _text(f"lbl-{k}", label, arrow["id"], sx + (ex - sx) / 2 - 50, sy + (ey - sy) / 2 - 20, 100, 14)
                arrow["boundElements"].append({"id": t["id"], "type": "text"})
                els.append(t)
                segs.append(Segment(label, [ref], "label"))
            else:
                segs.append(Segment("", [ref], "structural"))
        bottom = max((e["y"] + e["height"] for e in els if e["type"] == "rectangle"), default=120) + 60
        head = headline(req)
        title = _text("title", head.text, None, 0, 40, max(x - GAP_X, 400), 20)
        title["textAlign"] = "left"
        els.append(title)
        segs.append(Segment(head.text, [head.id]))
        for k, (nid, text, target) in enumerate(p.notes):
            u = spec.get(nid)
            w = max(260, CHAR_W * min(len(text), 48) + PAD)
            note = _base(f"note-{nid}", "rectangle", box[target]["x"], bottom + k * (BOX_H + GAP_Y), w, BOX_H + 20,
                         strokeColor=AMBER, strokeStyle="dashed", customData={"spec_id": nid})
            t = _text(f"notetxt-{nid}", f"Confidence {pct(u.confidence)}\n{u.reason}", note["id"],
                      note["x"] + PAD / 2, note["y"] + 14, w - PAD, 15)
            note["boundElements"].append({"id": t["id"], "type": "text"})
            els += [note, t]
            segs.append(Segment(f"Confidence {pct(u.confidence)} {u.reason}", [nid, u.about]))
        scene = {"type": "excalidraw", "version": 2, "source": "labzilla-understanding",
                 "elements": els, "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None}, "files": {}}
        caps = self.capabilities()
        return RenderResult(caps.name, caps.version, caps.target, json.dumps(scene, indent=1),
                            "application/vnd.excalidraw+json", segs, emphasis=[head.id], edges=edges,
                            verification=verify_scene(els))


def verify_scene(els: list[dict]) -> dict:
    rects = [e for e in els if e["type"] == "rectangle"]
    overlaps = []
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            if (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
                    and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]):
                overlaps.append((a["id"], b["id"]))
    ids = {e["id"] for e in els}
    unbound = [e["id"] for e in els if e["type"] == "arrow"
               and not (e["startBinding"]["elementId"] in ids and e["endBinding"]["elementId"] in ids)]
    return {"no-overlap": {"ok": not overlaps, "overlaps": overlaps[:5]},
            "bindings": {"ok": not unbound, "unbound": unbound}}
