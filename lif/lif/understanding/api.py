"""Explanation API (spec §95–§97).

    POST /v1/explain                                  ExplanationRequest → result, or SSE with ?stream=1
    GET  /v1/explanations/{id}                        the ExplanationSpec, its validation and sessions
    GET  /v1/explanations/{id}/history                semantic diffs along the spec's lineage
    POST /v1/explanations/{id}/render                 {session?, format?, depth?, audience?, viewport?, selected?}
    POST /v1/explanations/{id}/simplify               {session?}
    POST /v1/explanations/{id}/deepen                 {session?, focus?: [ids]}
    POST /v1/explanations/{id}/evaluate               {session?} → critic report, feedback, router stats
    POST /v1/sessions/{sid}/feedback                  {kind, detail}
    GET  /v1/sessions/{sid}/artifacts/{renderer}      the rendered artifact (HTML is served sandboxed)
    GET  /v1/renderers                                renderer registry, including unavailable renderers

Events on the stream: analysis.started, knowledge_model.ready, explanation_ir.ready, summary.ready,
route.ready, <target>.rendering, <target>.ready, <target>.failed, evaluation.ready, done, error.

Standalone: `uvicorn lif.understanding.api:app --host 127.0.0.1 --port 18084`. The console mounts the
same router under /api when LIF_CONSOLE_UNDERSTANDING=1, behind its session auth.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from lif.understanding.compiler import Compiler, ExplanationRequest, default_compiler
from lif.understanding.validate import validate

# Served artifacts get their own sandbox: an opaque origin with no network, whatever page embeds them (§83).
SANDBOX_CSP = ("sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
               "img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'")


class OverrideBody(BaseModel):
    session: str | None = None
    format: str | None = None
    depth: str | None = None
    audience: dict | None = None
    viewport: str | None = None
    selected: list[str] | None = None
    focus: list[str] | None = None


class FeedbackBody(BaseModel):
    kind: str                          # helpful | not_helpful | abandoned | override | quiz_correct | quiz_wrong
    detail: dict[str, Any] = {}


def sse(events: AsyncIterator[dict]) -> StreamingResponse:
    async def gen():
        async for e in events:
            yield f"event: {e['event']}\ndata: {json.dumps(e['data'], default=str)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


async def finish(events: AsyncIterator[dict]) -> dict:
    """Run to completion and return a compact result: every artifact, the route, the evaluation."""
    out: dict[str, Any] = {"status": "running", "artifacts": {}, "events": []}
    async for e in events:
        d, name = e["data"], e["event"]
        out["events"].append(name)
        out.setdefault("session", d.get("session"))
        if name == "explanation_ir.ready":
            out["explanation_id"] = d.get("explanation_id")
        elif name == "summary.ready":
            out["summary"] = d["artifact"]
        elif name == "route.ready":
            out.update(primary=d["primary"], supporting=d["supporting"], deferred=d["deferred"], why=d["why"])
        elif name.endswith(".ready") and "renderer" in d:
            out["artifacts"][d["renderer"]] = {k: d[k] for k in ("media_type", "artifact", "verification")}
        elif name == "evaluation.ready":
            out["evaluation"] = d
        elif name == "done":
            out["status"] = "done"
            out["explanation_id"] = d["explanation_id"]
        elif name == "error":
            out["status"] = "error"
            out["error"] = d
    return out


def make_router(get_compiler: Callable[[], Compiler], dependencies: list | None = None,
                prefix: str = "") -> APIRouter:
    r = APIRouter(prefix=prefix, dependencies=dependencies or [], tags=["understanding"])

    def session_for(comp: Compiler, spec_id: str, sid: str | None) -> str:
        if sid:
            s = comp.store.get_session(sid)
            if s is None or s["spec_id"] != spec_id and comp.store.get_spec(s["spec_id"]).metadata.parent != spec_id:
                raise HTTPException(404, "no such session for this explanation")
            return sid
        row = comp.store.db.one("SELECT id FROM sessions WHERE spec_id=? ORDER BY updated_at DESC LIMIT 1", (spec_id,))
        if row is None:
            raise HTTPException(404, "no session for this explanation; POST /v1/explain first")
        return row["id"]

    @r.post("/v1/explain")
    async def explain(req: ExplanationRequest, stream: bool = Query(False)):
        comp = get_compiler()
        if stream:
            return sse(comp.explain(req))
        return await finish(comp.explain(req))

    @r.get("/v1/explanations/{spec_id}")
    async def get_explanation(spec_id: str):
        comp = get_compiler()
        spec = comp.store.get_spec(spec_id)
        if spec is None:
            raise HTTPException(404, "no such explanation")
        sessions = comp.store.db.q("SELECT id, updated_at FROM sessions WHERE spec_id=? ORDER BY updated_at DESC",
                                   (spec_id,))
        return {"spec": spec.to_json(), "semantic_hash": spec.semantic_hash(),
                "validation": validate(spec, comp.resolver).to_dict(), "sessions": sessions}

    @r.get("/v1/explanations/{spec_id}/history")
    async def history(spec_id: str):
        return {"lineage": get_compiler().history(spec_id)}

    @r.post("/v1/explanations/{spec_id}/render")
    async def render(spec_id: str, body: OverrideBody, stream: bool = Query(False)):
        comp = get_compiler()
        sid = session_for(comp, spec_id, body.session)
        ev = comp.rerender(sid, format=body.format, depth=body.depth, audience=body.audience, viewport=body.viewport,
                           selected=body.selected)
        return sse(ev) if stream else await finish(ev)

    @r.post("/v1/explanations/{spec_id}/simplify")
    async def simplify(spec_id: str, body: OverrideBody, stream: bool = Query(False)):
        comp = get_compiler()
        ev = comp.simplify(session_for(comp, spec_id, body.session))
        return sse(ev) if stream else await finish(ev)

    @r.post("/v1/explanations/{spec_id}/deepen")
    async def deepen(spec_id: str, body: OverrideBody, stream: bool = Query(False)):
        comp = get_compiler()
        ev = comp.deepen(session_for(comp, spec_id, body.session), body.focus)
        return sse(ev) if stream else await finish(ev)

    @r.post("/v1/explanations/{spec_id}/evaluate")
    async def evaluate(spec_id: str, body: OverrideBody):
        comp = get_compiler()
        return comp.evaluate(session_for(comp, spec_id, body.session))

    @r.post("/v1/sessions/{sid}/feedback")
    async def feedback(sid: str, body: FeedbackBody):
        comp = get_compiler()
        if comp.store.get_session(sid) is None:
            raise HTTPException(404, "no such session")
        comp.store.feedback(sid, body.kind[:40], body.detail)
        return {"ok": True}

    @r.get("/v1/sessions/{sid}/artifacts/{renderer}")
    async def artifact(sid: str, renderer: str):
        comp = get_compiler()
        s = comp.store.get_session(sid)
        if s is None or renderer not in s["outputs"]:
            raise HTTPException(404, "no such artifact")
        spec = comp.store.get_spec(s["spec_id"])
        res = comp.render_one(spec, renderer, s["presentation"], (s["plan"] or {}).get("options"))
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if res.media_type == "text/html":
            headers["Content-Security-Policy"] = SANDBOX_CSP
        return Response(res.artifact, media_type=res.media_type, headers=headers)

    @r.get("/v1/renderers")
    async def renderers():
        return {"renderers": [c.to_dict() for c in get_compiler().registry.capabilities()]}

    return r


_compiler: Compiler | None = None


def compiler() -> Compiler:
    global _compiler
    if _compiler is None:
        _compiler = default_compiler()
    return _compiler


def create_app() -> FastAPI:
    app = FastAPI(title="Labzilla Understanding", docs_url=None, redoc_url=None)
    app.include_router(make_router(compiler))

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return JSONResponse({"status": "ok"})
    return app


app = create_app()
