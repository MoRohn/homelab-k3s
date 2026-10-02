"""The Understanding Compiler: request → knowledge model → ExplanationSpec → route → render → critic (spec §2, §47–§49, §76, §80, §92–§94, §97, §130).

`Compiler.explain()` is an async generator of events, so callers (SSE, CLI, MCP) show results as they
arrive:

    analysis.started → knowledge_model.ready → explanation_ir.ready → summary.ready → route.ready
    → <renderer>.rendering → <renderer>.ready | <renderer>.failed (+ fallback) → evaluation.ready → done

Overrides (`render`, `simplify`, `deepen`, audience change) recompile from the SAME stored spec; they
never call a builder unless the spec lacks what was asked for (deepen past the deepest level).

Model use, cheapest first (§76): code builds features, routes and renders; Jev answers three bounded
routing questions over derived features; a local model drafts a knowledge model only when no package
or state builder applies. Nothing in this module calls an external model.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lif.common import log
from lif.understanding import analysis, critic, knowledge_model as km_mod, router
from lif.understanding.registry import Registry
from lif.understanding.render import base
from lif.understanding.render.base import OK, RenderRequest, RenderResult
from lif.understanding.spec import DEPTH_LEVEL, DEPTH_SECONDS, Audience, ExplanationSpec
from lif.understanding.store import Store, artifact_key
from lif.understanding.validate import Resolver, syntax_resolver, validate

LOG = log.get("lif.understanding")

Judge = Callable[[str, dict], Awaitable[tuple[str, bool]]]           # (answer, actionable)
Generate = km_mod.Generate
ResourceProbe = Callable[[], Awaitable[router.ResourceState]]
Collector = Callable[[], Awaitable[dict]]

PROFILES: dict[str, tuple[str, int]] = {"quick": ("summary", 30), "standard": ("learn", 180),
                                        "deep": ("deep", 600), "teach": ("deep", 900)}
DEPTHS = ("glance", "summary", "learn", "deep")
DECISIONS = ("understanding-prose-sufficient", "understanding-interaction-worthwhile", "understanding-diagram-family")
# §80: if a renderer fails, fall back to a cheaper target. Structured prose never fails the explanation.
FALLBACK = {"SIMULATION": "STATIC_VISUAL_EXPLAINER", "INTERACTIVE_HTML": "STATIC_VISUAL_EXPLAINER",
            "MINI_APPLICATION": "STATIC_VISUAL_EXPLAINER", "INTERACTIVE_CANVAS": "STATIC_VISUAL_EXPLAINER",
            "MERMAID": "STRUCTURED_PROSE", "EXCALIDRAW": "STRUCTURED_PROSE", "TABLE": "STRUCTURED_PROSE",
            "STATIC_VISUAL_EXPLAINER": "STRUCTURED_PROSE", "ANIMATION": "MERMAID", "NARRATED_VIDEO": "MERMAID"}
# Operational questions answered from live state rather than a package (§55, §100).
GPU_NOW = re.compile(r"\bgpu\b.*\b(utili[sz]ation|usage|busy|idle|under-?used|under-?utili[sz]ed)\b.*"
                     r"\b(now|currently|right now|today|at the moment|on (the )?(dgx|spark|host|this))\b"
                     r"|\bwhy is (the )?gpu (idle|so low|under-?used|not busy)\b", re.I)
UNDERSTAND = re.compile(r"^\s*(explain|why\b|how (does|do|did|is|are|can)\b|help me understand|walk me through|"
                        r"what causes|what caused|teach me|what changed|make me understand)", re.I)


def is_understanding_intent(text: str) -> bool:
    """§50: explanatory phrasing, detected by rules (no model on this path)."""
    return bool(UNDERSTAND.search(text or ""))


class ExplanationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    audience: Audience | str | None = None          # "engineer", "executive", or a full Audience
    expertise: Literal["novice", "intermediate", "expert"] | None = None
    objective: str = ""
    profile: Literal["quick", "standard", "deep", "teach"] | None = None
    depth: Literal["glance", "summary", "learn", "deep"] | None = None
    time_budget_seconds: int | None = Field(None, ge=5, le=7200)
    mode: Literal["auto", "understand", "fast", "deep"] = "auto"
    format: str = "auto"
    viewport: Literal["desktop", "mobile"] = "desktop"
    interaction_allowed: bool = True
    local_only: bool = True
    comprehension: bool = False                       # §63: only when asked
    context: dict[str, Any] = {}                      # "Explain this" state, e.g. {"kind": "gpu"}

    def resolved(self) -> tuple[str, int, Audience]:
        depth, budget = PROFILES.get(self.profile or "", (None, None))
        if self.mode == "fast" and not self.profile:
            depth, budget = "summary", 30
        if self.mode == "deep" and not self.profile:
            depth, budget = "deep", 600
        depth = self.depth or depth or "learn"
        budget = self.time_budget_seconds or budget or DEPTH_SECONDS[depth]
        if isinstance(self.audience, Audience):
            aud = self.audience
        else:
            role = {"engineer": "software_engineer", "dev": "developer", "exec": "executive", "ops": "operator",
                    "user": "general", None: "general"}.get(self.audience, self.audience)
            aud = Audience(role=role if role in Audience.model_fields["role"].annotation.__args__ else "general")
        if self.expertise:
            aud = aud.model_copy(update={"expertise": self.expertise})
        elif aud.role == "executive" and "expertise" not in aud.model_fields_set:
            aud = aud.model_copy(update={"expertise": "novice"})
        return depth, budget, aud


def ev(event: str, **data: Any) -> dict:
    return {"event": event, "data": data, "ts": round(time.time(), 3)}


# ── judges and probes ─────────────────────────────────────────────────────────────────────────

async def rules_judge(name: str, state: dict) -> tuple[str, bool]:
    """Offline judge: the deterministic rules registered for the understanding decisions."""
    from lif.decision.rules import rules
    fn = rules._rules.get(name)
    out = fn(state) if fn else None
    if out is None:
        return "", False
    return out[0], out[1] >= 0.8


def fabric_judge(url: str | None = None, timeout: float = 3.0) -> Judge:
    """The Decision Fabric (cascade: rules → Jev → local), with a short timeout. Unreachable → no judgment."""
    from lif.decision.sdk import RemoteIntelligence

    async def judge(name: str, state: dict) -> tuple[str, bool]:
        try:
            d = await asyncio.wait_for(RemoteIntelligence(url).decide(name, state, data_class="PUBLIC"), timeout)
            return d.answer, bool(d.actionable)
        except Exception:                           # noqa: BLE001 - routing must not depend on Jev being up
            return await rules_judge(name, state)
    return judge


async def gpusched_probe() -> router.ResourceState:
    try:
        from lif.gpu.state import GpuStateWatcher
        snap = await asyncio.wait_for(GpuStateWatcher().refresh(), 3.0)
        return router.ResourceState(snap.state.name if snap.reachable else "UNKNOWN")
    except Exception:                               # noqa: BLE001
        return router.ResourceState("UNKNOWN")


def gateway_generate(alias: str = "local/default", max_tokens: int = 2500) -> Generate:
    """Local generation through the LIF gateway. CONFIDENTIAL, never external."""
    import os

    import httpx

    url = os.environ.get("LIF_GATEWAY_URL", "http://127.0.0.1:18080").rstrip("/")
    key = os.environ.get("LIF_API_KEY", "")

    async def gen(messages: list[dict]) -> str:
        headers = {"X-LIF-Data-Class": "CONFIDENTIAL", "X-LIF-Workload": "understanding"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{url}/v1/chat/completions", headers=headers,
                             json={"model": alias, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1})
            r.raise_for_status()
            return r.json()["choices"][0]["message"].get("content") or ""
    return gen


# ── semantic diff (§72) ───────────────────────────────────────────────────────────────────────

def semantic_diff(a: ExplanationSpec, b: ExplanationSpec) -> list[dict]:
    ia, ib = a.index(), b.index()
    out: list[dict] = []
    for i in sorted(set(ib) - set(ia)):
        out.append({"change": f"new {ib[i][0].rstrip('s')}", "id": i})
    for i in sorted(set(ia) - set(ib)):
        out.append({"change": f"removed {ia[i][0].rstrip('s')}", "id": i})
    for i in sorted(set(ia) & set(ib)):
        x, y = ia[i][1], ib[i][1]
        dx, dy = x.model_dump(by_alias=True), y.model_dump(by_alias=True)
        dx.pop("level", None), dy.pop("level", None)
        if dx == dy:
            continue
        kind = ia[i][0]
        if kind == "uncertainties":
            label = "updated uncertainty"
        elif kind == "claims" and dx.get("evidence") != dy.get("evidence"):
            label = "changed evidence"
        elif kind == "claims" and dx.get("confidence") != dy.get("confidence"):
            label = "changed confidence"
        else:
            label = f"changed {kind.rstrip('s')}"
        out.append({"change": label, "id": i, "before": {k: v for k, v in dx.items() if dy.get(k) != v},
                    "after": {k: v for k, v in dy.items() if dx.get(k) != v}})
    if a.summary.headline != b.summary.headline:
        out.append({"change": "new headline", "before": a.summary.headline, "after": b.summary.headline})
    return out


# ── compiler ─────────────────────────────────────────────────────────────────────────────────

class Compiler:
    def __init__(self, store: Store | None = None, registry: Registry | None = None, *, judge: Judge | None = None,
                 generate: Generate | None = None, resources: ResourceProbe | None = None,
                 collectors: dict[str, Collector] | None = None, resolver: Resolver | None = syntax_resolver,
                 use_cache: bool = True):
        self.store = store or Store()
        self.registry = registry or Registry()
        self.judge = judge or rules_judge
        self.generate = generate
        self.resources = resources
        self.collectors = collectors if collectors is not None else {}
        self.resolver = resolver
        self.use_cache = use_cache
        self.builder_calls = 0           # how many times a knowledge model was built (Acceptance F/G: zero on overrides)

    # build ────────────────────────────────────────────────────────────────────────────────────

    async def build(self, req: ExplanationRequest, audience: Audience) -> tuple[ExplanationSpec, str]:
        q = req.question
        kind = str(req.context.get("kind") or "")
        if (kind == "gpu" or GPU_NOW.search(q)) and "gpu" in self.collectors:
            self.builder_calls += 1
            state = req.context.get("state") or await self.collectors["gpu"]()
            return km_mod.plan(km_mod.from_gpu_state(q, state), audience), "state:gpu"
        spec = km_mod.from_package(q)
        if spec is not None:
            self.builder_calls += 1
            spec.audience = audience
            return spec, f"package:{spec.metadata.package}"
        if self.generate is not None:
            self.builder_calls += 1
            ctx = "\n".join(f"- {k}: {v}" for k, v in req.context.items() if k != "kind")[:4000]
            km = await km_mod.from_llm(q, self.generate, ctx, model="local")
            return km_mod.plan(km, audience), km.builder
        raise LookupError("no explanation package matches this question and no local model is configured; "
                          "start the gateway or add an explanation package")

    # route ────────────────────────────────────────────────────────────────────────────────────

    async def judgments(self, f: analysis.Features) -> dict[str, str]:
        state = {"features": f.public_state()}
        got = await asyncio.gather(*(self.judge(n, state) for n in DECISIONS), return_exceptions=True)
        out = {}
        for name, g in zip(DECISIONS, got):
            if isinstance(g, tuple) and g[1] and g[0]:
                out[name] = g[0]
        return out

    async def route(self, spec: ExplanationSpec, pres: dict) -> tuple[analysis.Features, router.RoutePlan]:
        f = analysis.analyze(spec, pres["depth"], spec.question)
        res = await self.resources() if self.resources else router.ResourceState("UNKNOWN")
        ctx = router.RouteContext(depth=pres["depth"], time_budget_seconds=pres["budget"],
                                  audience=Audience(**pres["audience"]), viewport=pres["viewport"],
                                  interaction_allowed=pres["interaction"], local_only=pres["local_only"],
                                  format=pres["format"], resources=res, judgments=await self.judgments(f))
        return f, router.route(spec, f, self.registry, ctx)

    # render ───────────────────────────────────────────────────────────────────────────────────

    def render_one(self, spec: ExplanationSpec, name: str, pres: dict, options: dict | None = None) -> RenderResult:
        r = self.registry.get(name)
        req = RenderRequest(spec, depth=pres["depth"], audience=Audience(**pres["audience"]), viewport=pres["viewport"],
                            selected=pres.get("selected") or [], interactivity=pres["interaction"],
                            options={**(options or {}), **({"comprehension": True} if pres.get("comprehension") else {})})
        caps = r.capabilities()
        key = artifact_key(spec, caps.name, caps.version, req)
        if self.use_cache and (hit := self.store.get_artifact(key)) is not None:
            hit.render_metrics["cache"] = "hit"
            return hit
        res = base.run(r, req)
        if res.status == OK:
            self.store.put_artifact(key, spec.id, res)
        return res

    async def compile(self, spec: ExplanationSpec, pres: dict, sid: str) -> AsyncIterator[dict]:
        """Route and render an existing spec. Shared by explain() and every override."""
        summary = self.render_one(spec, "ste-prose", {**pres, "depth": "glance" if DEPTH_LEVEL[pres["depth"]] < 2
                                                      else "summary"})
        yield ev("summary.ready", session=sid, artifact=summary.artifact, referenced_ids=summary.referenced_ids)
        f, plan = await self.route(spec, pres)
        yield ev("route.ready", session=sid, primary=plan.primary, supporting=plan.supporting, deferred=plan.deferred,
                 why=plan.why, judgments=plan.judgments, features=f.public_state())
        outputs: dict[str, dict] = {}
        results: list[RenderResult] = []
        order = sorted(plan.selected, key=lambda n: (n != "ste-prose",
                                                     self.registry.get(n).capabilities().consume_seconds))
        for name in order:
            caps = self.registry.get(name).capabilities()
            yield ev(f"{caps.target.lower()}.rendering", session=sid, renderer=name)
            res = await asyncio.to_thread(self.render_one, spec, name, pres, plan.options)
            tried = {name}
            while res.status != OK:
                yield ev(f"{caps.target.lower()}.failed", session=sid, renderer=name, status=res.status,
                         detail=res.detail)
                nxt = self.registry.by_target(FALLBACK.get(caps.target, "STRUCTURED_PROSE"))
                if nxt is None or nxt.capabilities().name in tried:
                    nxt = self.registry.get("structured-prose")
                    if "structured-prose" in tried:
                        break
                name, caps = nxt.capabilities().name, nxt.capabilities()
                tried.add(name)
                res = await asyncio.to_thread(self.render_one, spec, name, pres, plan.options)
            if res.status == OK:
                results.append(res)
                outputs[name] = {"target": res.target, "media_type": res.media_type,
                                 "cache": res.render_metrics.get("cache", "miss")}
                yield ev(f"{res.target.lower()}.ready", session=sid, renderer=name, media_type=res.media_type,
                         artifact=res.artifact, referenced_ids=res.referenced_ids, verification=res.verification,
                         render_metrics=res.render_metrics)
        report = critic.evaluate(spec, results, pres["depth"])
        self.store.put_session(sid, spec.id, pres.get("request") or {}, pres, plan.to_dict(), outputs, report)
        yield ev("evaluation.ready", session=sid, **{k: report[k] for k in
                                                     ("ok", "semantic_ok", "presentation_ok", "contradictions",
                                                      "objectives_covered", "missing_primary_claims", "summary")})
        yield ev("done", session=sid, explanation_id=spec.id, primary=plan.primary, outputs=list(outputs))

    async def explain(self, req: ExplanationRequest) -> AsyncIterator[dict]:
        sid = f"s-{uuid.uuid4().hex[:12]}"
        depth, budget, aud = req.resolved()
        pres = {"depth": depth, "budget": budget, "audience": aud.model_dump(), "viewport": req.viewport,
                "interaction": req.interaction_allowed, "local_only": req.local_only, "format": req.format,
                "comprehension": req.comprehension or req.profile == "teach", "request": req.model_dump(mode="json")}
        yield ev("analysis.started", session=sid, question=req.question, depth=depth, time_budget_seconds=budget)
        try:
            spec, builder = await self.build(req, aud)
        except Exception as e:                       # noqa: BLE001
            yield ev("error", session=sid, stage="knowledge_model", detail=str(e)[:500])
            return
        yield ev("knowledge_model.ready", session=sid, builder=builder, concepts=len(spec.concepts),
                 claims=len(spec.claims))
        result = validate(spec, self.resolver)
        if not result.ok:
            yield ev("error", session=sid, stage="explanation_ir", issues=[i.to_dict() for i in result.errors])
            return
        self.store.put_spec(spec)
        yield ev("explanation_ir.ready", session=sid, explanation_id=spec.id, semantic_hash=spec.semantic_hash(),
                 warnings=[i.to_dict() for i in result.warnings])
        async for e in self.compile(spec, pres, sid):
            yield e

    # overrides (§92–§94) ──────────────────────────────────────────────────────────────────────

    def _session(self, sid: str) -> tuple[dict, ExplanationSpec]:
        s = self.store.get_session(sid)
        if s is None:
            raise KeyError(f"no explanation session {sid}")
        spec = self.store.get_spec(s["spec_id"])
        assert spec is not None
        return s, spec

    async def rerender(self, sid: str, **changes: Any) -> AsyncIterator[dict]:
        """Recompile the session's spec with presentation changes (format, audience, depth, viewport…)."""
        s, spec = self._session(sid)
        pres = {**s["presentation"], **{k: v for k, v in changes.items() if v is not None}}
        if "format" in changes and changes["format"] not in (None, "auto"):
            self.store.feedback(sid, "override", {"format": changes["format"], "was": s["plan"].get("primary")})
        async for e in self.compile(spec, pres, sid):
            yield e

    async def simplify(self, sid: str) -> AsyncIterator[dict]:
        """Same spec, shallower and plainer: audience novice, depth one step down, plain wording (§93)."""
        s, _ = self._session(sid)
        pres = s["presentation"]
        aud = {**pres["audience"], "expertise": "novice"}
        depth = DEPTHS[max(1, DEPTHS.index(pres["depth"]) - 1)]
        self.store.feedback(sid, "simplify", {"from_depth": pres["depth"]})
        async for e in self.rerender(sid, audience=aud, depth=depth, budget=min(pres["budget"], DEPTH_SECONDS[depth]),
                                     format="auto"):
            yield e

    async def deepen(self, sid: str, focus: list[str] | None = None) -> AsyncIterator[dict]:
        """One level deeper from the same spec while it has deeper content; past that, extend with a builder."""
        s, spec = self._session(sid)
        pres = s["presentation"]
        lv = DEPTH_LEVEL[pres["depth"]]
        deeper = any(getattr(el, "level", 0) > lv for _, el in spec.elements())
        self.store.feedback(sid, "deepen", {"from_depth": pres["depth"], "focus": focus or []})
        if deeper and lv < 3:
            depth = DEPTHS[lv + 1]
            async for e in self.rerender(sid, depth=depth, budget=max(pres["budget"], DEPTH_SECONDS[depth]),
                                         selected=focus or pres.get("selected") or [], format="auto"):
                yield e
            return
        if self.generate is None:
            yield ev("error", session=sid, stage="deepen",
                     detail="this explanation is already at its deepest level and no local model is configured to "
                            "extend it")
            return
        self.builder_calls += 1
        focus_txt = "; ".join(getattr(spec.get(i), "text", "") or getattr(spec.get(i), "label", "") for i in focus or [])
        km = await km_mod.from_llm(spec.question + (f" (go deeper on: {focus_txt})" if focus_txt else ""),
                                   self.generate, context=spec.summary.headline, model="local")
        new = km_mod.plan(km, Audience(**pres["audience"]), parent=spec.id)
        new.version = spec.version + 1
        res = validate(new, self.resolver)
        if not res.ok:
            yield ev("error", session=sid, stage="deepen", issues=[i.to_dict() for i in res.errors])
            return
        self.store.put_spec(new)
        yield ev("explanation_ir.ready", session=sid, explanation_id=new.id, parent=spec.id,
                 diff=semantic_diff(spec, new))
        async for e in self.compile(new, {**pres, "depth": "deep"}, sid):
            yield e

    def evaluate(self, sid: str) -> dict:
        s, spec = self._session(sid)
        return {"session": sid, "explanation_id": spec.id, "evaluation": s["evaluation"],
                "feedback": self.store.feedback_for(sid), "router": self.store.router_stats(),
                "checkpoints": [{"id": c.id, "question": c.question} for c in spec.checkpoints]}

    def history(self, spec_id: str) -> list[dict]:
        line = self.store.lineage(spec_id)
        return [{"from": a.id, "to": b.id, "changes": semantic_diff(a, b)} for a, b in zip(line, line[1:])]


async def collect_all(it: AsyncIterator[dict]) -> list[dict]:
    return [e async for e in it]


def default_compiler(store: Store | None = None, *, online: bool = True) -> Compiler:
    """Wiring for the CLI, API and MCP: live GPU state, gpusched-aware deferral, the Decision Fabric for
    judgments (rules when it is unreachable) and the local gateway for drafting new knowledge models."""
    import os

    from lif.understanding import collect
    if not online:
        return Compiler(store, collectors={"gpu": collect.gpu_state})
    return Compiler(store, judge=fabric_judge(os.environ.get("LIF_DECISION_URL")), generate=gateway_generate(),
                    resources=gpusched_probe, collectors={"gpu": collect.gpu_state})


__all__ = ["Compiler", "ExplanationRequest", "default_compiler", "is_understanding_intent", "semantic_diff",
           "collect_all"]
