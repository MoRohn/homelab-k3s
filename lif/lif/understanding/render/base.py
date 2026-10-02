"""Renderer SDK and contract (spec §41, §77–§79, §114).

A renderer declares what it is good for (`capabilities`), says whether a spec has what it needs
(`validate`) and compiles a RenderRequest into a RenderResult. It never reasons about sources: every
piece of meaning in the artifact is emitted as a `Segment` tagged with the spec IDs it comes from.

`check_contract` enforces "renderers do not invent facts" (Acceptance J). A result fails when:
  - a segment cites an ID the spec does not have;
  - a semantic segment cites no ID at all;
  - a segment uses a number, or a content word, that its cited elements (and their terminology) do
    not contain. Words a renderer adds for structure ("because", "observed", "confidence") come from
    the fixed CONNECTIVES vocabulary below; anything else must come from the spec;
  - a cited claim has an uncertainty the artifact dropped (§19, uncertainty must survive every target).

A renderer that finds the IR insufficient returns status NEEDS_SEMANTIC_UPDATE instead of filling the
gap itself (§77).
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from lif.understanding.spec import DEPTH_LEVEL, Audience, ExplanationSpec

Target = Literal["STE_PROSE", "STRUCTURED_PROSE", "MERMAID", "EXCALIDRAW", "STATIC_VISUAL_EXPLAINER",
                 "INTERACTIVE_HTML", "INTERACTIVE_CANVAS", "MINI_APPLICATION", "TABLE", "TIMELINE", "SIMULATION",
                 "ANIMATION", "NARRATED_VIDEO"]
ResourceClass = Literal["LIGHT", "CPU", "BROWSER", "GPU", "LONG_RUNNING"]
LatencyClass = Literal["instant", "fast", "seconds", "minutes"]
LATENCY_SECONDS = {"instant": 0.05, "fast": 1.0, "seconds": 10.0, "minutes": 180.0}
Structure = Literal["definition", "causal", "process", "temporal", "hierarchy", "comparison", "dependency",
                    "parameter", "quantitative", "spatial", "narrative"]

OK, NEEDS_SEMANTIC_UPDATE, FAILED = "ok", "needs_semantic_update", "failed"


@dataclass(frozen=True)
class RendererCapabilities:
    name: str
    version: str
    target: Target
    best_for: tuple[str, ...]                 # Structure kinds this renderer communicates well
    unsupported: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ("ExplanationSpec/v1",)
    outputs: tuple[str, ...] = ("text/plain",)
    latency_class: LatencyClass = "fast"
    resource_class: ResourceClass = "LIGHT"
    interactive: bool = False
    local: bool = True
    requires_gpu: bool = False
    permissions: tuple[str, ...] = ()         # filesystem | browser | gpu | audio | network (§84)
    verification: tuple[str, ...] = ("contract",)
    clarity: float = 0.6                      # how directly it communicates what it is best for
    consume_seconds: float = 30.0             # typical time for a reader to take it in
    mobile: bool = True
    available: bool = True
    unavailable_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Segment:
    text: str
    refs: list[str]
    role: Literal["semantic", "label", "structural"] = "semantic"


@dataclass
class RenderRequest:
    spec: ExplanationSpec
    depth: str = "summary"                    # glance | summary | learn | deep
    audience: Audience | None = None          # overrides spec.audience (§92 "make simpler", Acceptance G)
    selected: list[str] = field(default_factory=list)    # restrict to these element ids (highlight-to-ask)
    theme: str = "labzilla-dark"
    viewport: Literal["desktop", "mobile"] = "desktop"
    interactivity: bool = True
    output_format: str = ""
    options: dict[str, Any] = field(default_factory=dict)   # e.g. prose_mode, diagram_family

    @property
    def level(self) -> int:
        return DEPTH_LEVEL.get(self.depth, 1)

    @property
    def who(self) -> Audience:
        return self.audience or self.spec.audience

    def visible(self, el: Any) -> bool:
        if self.selected and getattr(el, "id", None) not in self._selected_closure():
            return False
        return getattr(el, "level", 0) <= self.level

    def _selected_closure(self) -> set[str]:
        """Selected ids plus what they directly reference, so a selected claim keeps its concepts."""
        out = set(self.selected)
        for i in list(out):
            el = self.spec.get(i)
            for attr in ("concepts", "evidence", "steps", "claims"):
                out.update(getattr(el, attr, []) or [])
            for u in self.spec.uncertainty_for(i):
                out.add(u.id)
        return out


@dataclass
class RenderResult:
    renderer: str
    version: str
    target: str
    artifact: str
    media_type: str = "text/plain"
    segments: list[Segment] = field(default_factory=list)
    emphasis: list[str] = field(default_factory=list)       # what the artifact leads with, in order
    edges: list[tuple[str, str]] = field(default_factory=list)   # concept pairs the artifact draws as connected
    warnings: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    render_metrics: dict[str, Any] = field(default_factory=dict)
    status: str = OK
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)      # e.g. sidecar sources (excalidraw scene json)

    @property
    def referenced_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for s in self.segments:
            for r in s.refs:
                seen.setdefault(r, None)
        return list(seen)

    def to_dict(self, with_artifact: bool = True) -> dict:
        d = {"renderer": self.renderer, "version": self.version, "target": self.target,
             "media_type": self.media_type, "referenced_ids": self.referenced_ids, "emphasis": self.emphasis,
             "warnings": self.warnings, "verification": self.verification, "render_metrics": self.render_metrics,
             "status": self.status, "detail": self.detail}
        if with_artifact:
            d["artifact"] = self.artifact
        return d


class Renderer(Protocol):
    def capabilities(self) -> RendererCapabilities: ...

    def accepts(self, spec: ExplanationSpec) -> str | None:
        """None if this renderer can render the spec, else why not (no comparisons for a table, …)."""
        ...

    def render(self, req: RenderRequest) -> RenderResult: ...


class SemanticGap(Exception):
    """Raised inside a renderer when the IR lacks something it needs; becomes NEEDS_SEMANTIC_UPDATE."""


def run(renderer: Renderer, req: RenderRequest) -> RenderResult:
    """Render, time it, and apply the contract. Never raises for a renderer bug: returns FAILED."""
    caps = renderer.capabilities()
    t0 = time.perf_counter()
    try:
        res = renderer.render(req)
    except SemanticGap as e:
        return RenderResult(caps.name, caps.version, caps.target, "", status=NEEDS_SEMANTIC_UPDATE, detail=str(e))
    except Exception as e:                  # noqa: BLE001 - a renderer must never take the explanation down
        return RenderResult(caps.name, caps.version, caps.target, "", status=FAILED,
                            detail=f"{type(e).__name__}: {e}")
    res.render_metrics.setdefault("ms", round((time.perf_counter() - t0) * 1000, 2))
    res.render_metrics.setdefault("bytes", len(res.artifact.encode()))
    problems = check_contract(req.spec, res)
    res.verification["contract"] = {"ok": not problems, "problems": problems}
    if problems:
        res.status = FAILED
        res.detail = "contract: " + "; ".join(problems[:5])
    return res


# ── contract ─────────────────────────────────────────────────────────────────────────────────

WORD = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
LIST_MARKER = re.compile(r"^\s*(?:[-*]|\d+\.)\s+", re.M)
NUMBER = re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?")

# Structural words any renderer may add. Kept deliberately small: no nouns that could carry a new fact, and no
# negations, quantifiers or modals (not, no, all, only, must, will…): those change a claim's meaning, so they
# must come from the cited text.
CONNECTIVES = frozenset("""
the and but for with from into onto that this these those then than there their they them its
because so therefore leads lead causes cause caused which who what why how when where while also
are was were has have had does did can been being
observed inferred assumption general definition evidence sources source uncertain uncertainty confidence
needed need unresolved resolved short version summary diagram explore details detail more deeper simpler
step steps next previous first last example examples counterexample misconception correction check
understanding question answer answers objective objectives learn learning goal goals key point points
factor factors contributing involved shows show see view open table timeline process chain relationship
relationships concept concepts term terms means meaning control controls output outputs scenario scenarios
result results value values scale set reset play pause slider drag change changes changed try explanation
known unknown about over after before during each one use uses used via
limits blocks depends contains routes competes precedes follows consumes produces supports contradicts
maps scheduled part per out off down
""".split())


def _stem(w: str) -> str:
    w = w.lower().strip("'-")
    for suf in ("'s", "ing", "ies", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def _texts(obj: Any) -> list[str]:
    """Every string (and number) inside an element, recursively."""
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, bool) or obj is None:
        pass
    elif isinstance(obj, (int, float)):
        out.append(f"{obj:g}")
        if 0 < obj <= 1 and isinstance(obj, float):
            out.append(f"{obj * 100:g}")              # confidences render as percentages
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_texts(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_texts(v))
    elif hasattr(obj, "model_dump"):
        out.extend(_texts(obj.model_dump(by_alias=True)))
    return out


def vocabulary(spec: ExplanationSpec, ids: list[str]) -> tuple[set[str], set[str]]:
    """Content-word stems and numbers that the cited elements license, plus their terminology."""
    words: set[str] = set()
    nums: set[str] = set()
    texts: list[str] = []
    for i in ids:
        el = spec.get(i)
        if el is None:
            continue
        texts.extend(_texts(el))
        for cid in [i, *getattr(el, "concepts", []), *getattr(el, "steps", []), *getattr(el, "items", [])]:
            c = spec.concept(cid)
            if c is not None:
                texts.extend([c.label, c.definition])
                if c.term and (t := spec.term_for(c.term)):
                    texts.extend(_texts(t))
        for u in spec.uncertainty_for(i):
            texts.extend(_texts(u))
        if spec.simulation is not None:            # control and output labels bound to a cited element
            for o in spec.simulation.outputs:
                if o.concept == i or o.key == i:
                    texts.extend([o.label, o.unit])
            texts.extend(sc.label for sc in spec.simulation.scenarios if i in sc.values)
    texts.extend(t for term in spec.terms for t in _texts(term))   # terminology is definitional, never a fact
    for t in texts:
        words.update(_stem(w) for w in WORD.findall(t))
        nums.update(_num(n) for n in NUMBER.findall(t))
    return words, nums


def _num(s: str) -> str:
    f = float(s)
    return f"{f:g}"


def check_contract(spec: ExplanationSpec, res: RenderResult) -> list[str]:
    problems: list[str] = []
    idx = spec.index()
    cited: set[str] = set()
    for s in res.segments:
        unknown = [r for r in s.refs if r not in idx]
        if unknown:
            problems.append(f"unknown ids {unknown} in {s.text[:40]!r}")
            continue
        cited.update(s.refs)
        if s.role == "structural":
            continue
        if not s.refs:
            problems.append(f"uncited content {s.text[:60]!r}")
            continue
        words, nums = vocabulary(spec, s.refs)
        text = LIST_MARKER.sub("", s.text)
        novel = sorted({w for w in WORD.findall(text) if _stem(w) not in words
                        and w.lower() not in CONNECTIVES and _stem(w) not in CONNECTIVES})
        if novel:
            problems.append(f"words not in cited elements {novel[:6]} in {s.text[:60]!r}")
        bad_nums = sorted({n for n in NUMBER.findall(text) if _num(n) not in nums})
        if bad_nums:
            problems.append(f"numbers not in cited elements {bad_nums} in {s.text[:60]!r}")
    for e in res.emphasis:
        if e not in idx:
            problems.append(f"emphasis on unknown id {e!r}")
    for cid in cited:
        for u in spec.uncertainty_for(cid):
            if u.id not in cited:
                problems.append(f"claim {cid} is shown without its uncertainty {u.id}")
    return problems
