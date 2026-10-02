"""Renderer registry (spec §41–§45, §84).

The router asks the registry what exists; it never names a renderer. A renderer that is declared
but not implemented on this host (Manim, narrated video, the Glimpse canvas, mini-apps) is listed
with `available=False` and a reason, so "why not a video?" has an answer and adding one later is a
registration, not a router change.

Each capability record also carries what a K3s worker for it would need (`resource_class`,
`requires_gpu`, `permissions`). Today all available renderers run in-process: they are LIGHT/CPU
template compilers. docs/UNDERSTANDING.md lists the worker split for when GPU renderers arrive.
"""
from __future__ import annotations

from lif.understanding.render.base import RendererCapabilities, RenderRequest, RenderResult
from lif.understanding.render.excalidraw import ExcalidrawRenderer
from lif.understanding.render.html import SimulationApp, StaticExplainer, StepThrough
from lif.understanding.render.mermaid import MermaidRenderer
from lif.understanding.render.prose import SteProse, StructuredProse
from lif.understanding.render.table import TableRenderer
from lif.understanding.spec import ExplanationSpec


class Declared:
    """A renderer this build knows about but cannot run. render() is never called by the router."""

    def __init__(self, caps: RendererCapabilities):
        self.caps = caps

    def capabilities(self) -> RendererCapabilities:
        return self.caps

    def accepts(self, spec: ExplanationSpec) -> str | None:
        return self.caps.unavailable_reason

    def render(self, req: RenderRequest) -> RenderResult:
        raise RuntimeError(self.caps.unavailable_reason)


DECLARED = (
    RendererCapabilities(name="manim", version="0", target="ANIMATION",
                         best_for=("process", "quantitative", "temporal"),
                         outputs=("video/mp4",), latency_class="minutes", resource_class="GPU", requires_gpu=False,
                         permissions=("filesystem",), clarity=0.85, consume_seconds=120, available=False,
                         unavailable_reason="no Manim SceneIR compiler in this build (phase 8)"),
    RendererCapabilities(name="narrated-video", version="0", target="NARRATED_VIDEO",
                         best_for=("temporal", "process", "narrative"), outputs=("video/mp4", "text/vtt"),
                         latency_class="minutes", resource_class="LONG_RUNNING", requires_gpu=True,
                         permissions=("filesystem", "gpu", "audio"), clarity=0.8, consume_seconds=300, mobile=False,
                         available=False, unavailable_reason="no storyboard/TTS pipeline in this build (phase 8)"),
    RendererCapabilities(name="glimpse-canvas", version="0", target="INTERACTIVE_CANVAS",
                         best_for=("dependency", "spatial", "causal"), outputs=("text/html",), interactive=True,
                         resource_class="BROWSER", permissions=("browser",), clarity=0.75, consume_seconds=180,
                         available=False, unavailable_reason="no canvas workspace in the console yet (phase 6/10)"),
    RendererCapabilities(name="mini-app", version="0", target="MINI_APPLICATION", best_for=("parameter",),
                         outputs=("text/html",), interactive=True, latency_class="minutes", resource_class="BROWSER",
                         permissions=("browser",), clarity=0.8, consume_seconds=300, available=False,
                         unavailable_reason="generated mini-apps are not enabled; deterministic simulations cover "
                                            "parameter questions"),
)


class Registry:
    def __init__(self, renderers: list | None = None):
        self._r: dict[str, object] = {}
        for r in renderers if renderers is not None else default_renderers():
            self.register(r)

    def register(self, renderer) -> None:
        self._r[renderer.capabilities().name] = renderer

    def get(self, name: str):
        return self._r.get(name)

    def all(self) -> list:
        return list(self._r.values())

    def capabilities(self) -> list[RendererCapabilities]:
        return [r.capabilities() for r in self._r.values()]

    def by_target(self, target: str):
        return next((r for r in self._r.values() if r.capabilities().target == target
                     and r.capabilities().available), None)


def default_renderers() -> list:
    return [SteProse(), StructuredProse(), MermaidRenderer(), ExcalidrawRenderer(), TableRenderer(),
            StaticExplainer(), StepThrough(), SimulationApp(), *(Declared(c) for c in DECLARED)]
