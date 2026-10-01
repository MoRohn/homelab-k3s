"""LIF knowledge service: the versioned HTTP API over the knowledge graph (/v1/knowledge/*).

Same auth model as the controller: operators use an admin key (Secret lif-admin-keys) and act as
humans; agents use a key from Secret lif-knowledge-keys (`agent-name:key` lines) and get the scopes
listed for them in workspace.yaml `permissions:`. Set LIF_KNOWLEDGE_OPEN=1 only for local dev
(`knowledge serve` on 127.0.0.1) to skip auth.

The service also serves the Control Center at `/`, so its Knowledge pages and the infrastructure
pages share one UI. In the cluster, the ingress routes lif.<host>/v1/knowledge to this service and
everything else to the controller.

Background work: incremental recompile every 5 s; optional tail of the controller activity stream
(LIF_CONTROLLER_URL + LIF_EVENTS_ADMIN_KEY) into `events_repo`.
"""
from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from lif.common import config, log
from lif.knowledge import packages as PK
from lif.knowledge import skills as SK
from lif.knowledge import views as VW
from lif.knowledge.ops import Knowledge, PermissionDenied
from lif.knowledge.packages import ApprovalRequired
from lif.knowledge.repo import RepoError
from lif.knowledge.writer import WriteError

LOG = log.get("lif.knowledge.app")
UI = Path(__file__).resolve().parents[2] / "apps" / "control-center" / "index.html"
API = "/v1/knowledge"


def _keys(secret: str) -> dict[str, str]:
    out = {}
    for line in (config.secret(secret) or "").splitlines():
        if ":" in line and not line.strip().startswith("#"):
            n, k = line.strip().split(":", 1)
            out[k.strip()] = n.strip()
    return out


class State:
    kn: Knowledge
    admin: dict[str, str]
    agents: dict[str, str]
    tasks: list[asyncio.Task]


S = State()


async def _refresh_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(S.kn.refresh)
        except Exception:                 # noqa: BLE001
            LOG.exception("knowledge refresh failed")
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.setup()
    S.kn = Knowledge.open()
    S.admin, S.agents = _keys("LIF_ADMIN_KEYS"), _keys("LIF_KNOWLEDGE_KEYS")
    S.tasks = [asyncio.create_task(_refresh_loop())]
    ctl, key = os.environ.get("LIF_CONTROLLER_URL"), config.secret("LIF_EVENTS_ADMIN_KEY")
    if ctl and key and S.kn.ws.config.get("events_repo"):
        from lif.knowledge.events import EventSink
        try:
            S.tasks.append(asyncio.create_task(EventSink(S.kn).tail(ctl, key)))
        except WriteError as e:
            LOG.warning(f"event sink disabled: {e}")
    yield
    for t in S.tasks:
        t.cancel()


app = FastAPI(title="LIF knowledge", version="1", lifespan=lifespan)
OPEN = {"/healthz", "/metrics", "/"}


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path in OPEN or request.url.path.startswith("/ui") or os.environ.get("LIF_KNOWLEDGE_OPEN") == "1":
        request.state.actor = "human"
        return await call_next(request)
    h = request.headers.get("authorization", "")
    tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
    actor = None
    for table, prefix in ((S.admin, None), (S.agents, "agent:")):
        for k, name in table.items():
            if tok and hmac.compare_digest(k, tok):
                actor = "human" if prefix is None else prefix + name
    if actor is None:
        return JSONResponse({"error": "key required"}, status_code=401)
    request.state.actor = actor
    return await call_next(request)


def run(request: Request, scope: str, action: str, fn, *a, **kw):
    actor = request.state.actor
    try:
        S.kn.require(actor, scope, action)
        S.kn.refresh()
        return JSONResponse(_jsonable(fn(*a, **kw)))
    except PermissionDenied as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ApprovalRequired as e:
        return JSONResponse({"error": str(e), "approval_required": True}, status_code=409)
    except WriteError as e:
        return JSONResponse({"error": str(e), "diagnostics": e.diagnostics}, status_code=422)
    except (RepoError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except KeyError as e:
        return JSONResponse({"error": f"not found: {e}"}, status_code=404)


def _jsonable(x):
    import json
    return json.loads(json.dumps(x, default=str))


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
async def ui():
    return FileResponse(UI)


R, W = "read:knowledge", "write:knowledge"


@app.get(API + "/health")
async def health(request: Request):
    return run(request, R, "health", S.kn.graph.health)


@app.get(API + "/bootstrap")
async def bootstrap(request: Request, repo: str | None = None):
    return run(request, R, "bootstrap", S.kn.context.bootstrap, repo)


@app.get(API + "/objects")
async def objects(request: Request, type: str | None = None, status: str | None = None, repo: str | None = None,
                  project: str | None = None, depends_on: str | None = None, dependent_of: str | None = None,
                  links_to: str | None = None, linked_from: str | None = None, older_than_days: int | None = None,
                  text: str | None = None, limit: int = 200):
    where = request.query_params.getlist("where") or None
    return run(request, R, "find", S.kn.graph.find, type, status, repo, project, where, depends_on, dependent_of,
               links_to, linked_from, older_than_days, text, False, limit)


@app.get(API + "/objects/{key:path}")
async def get_object(request: Request, key: str):
    return run(request, R, "get", S.kn.graph.get, key)


@app.post(API + "/objects")
async def create(request: Request):
    b = await _body(request)
    return run(request, W, "create", lambda: S.kn.writer(request.state.actor).create(
        b["type"], b.get("fields") or {}, b.get("body", ""), b.get("id"), b.get("repo")))


@app.patch(API + "/objects/{key:path}")
async def update(request: Request, key: str):
    b = await _body(request)
    return run(request, W, "update", lambda: S.kn.writer(request.state.actor).update(
        key, b.get("set"), b.get("append"), b.get("unset"), b.get("body"), b.get("append_body")))


@app.post(API + "/links")
async def link(request: Request):
    b = await _body(request)
    return run(request, W, "link", lambda: S.kn.writer(request.state.actor).link(b["source"], b["field"], b["target"]))


@app.get(API + "/search")
async def search(request: Request, q: str, type: str | None = None, repo: str | None = None, limit: int = 20):
    return run(request, R, "search", S.kn.search.search, q, type, repo, None, None, limit)


for _name, _fn in (("why", lambda k: S.kn.graph.why(k)), ("lineage", lambda k: S.kn.graph.lineage(k)),
                   ("impact", lambda k: S.kn.graph.impact(k)), ("evidence", lambda k: S.kn.graph.trace_claim(k)),
                   ("history", lambda k: S.kn.graph.decision_history(k))):
    def _mk(name=_name, fn=_fn):
        async def ep(request: Request, key: str):
            return run(request, R, name, fn, key)
        ep.__name__ = f"ep_{name}"
        return ep
    app.get(API + f"/{_name}/{{key:path}}")(_mk())


@app.get(API + "/diagnostics")
async def diagnostics(request: Request, severity: str | None = None, key: str | None = None):
    return run(request, R, "diagnostics", S.kn.store.diagnostics, severity, key)


@app.get(API + "/reconsiderations")
async def reconsiderations(request: Request, all: bool = False):
    return run(request, R, "reconsiderations", S.kn.store.reconsiderations, not all)


@app.post(API + "/reviews")
async def review(request: Request):
    from lif.knowledge.mcp import _review
    b = await _body(request)
    return run(request, W, "review", _review, S.kn, request.state.actor, b)


@app.post(API + "/context")
async def context(request: Request):
    b = await _body(request)
    return run(request, R, "context", S.kn.context.assemble, b["task"], b.get("project"), b.get("profile"),
               int(b.get("budget_tokens", 2000)), b.get("pins"))


@app.get(API + "/session/resume")
async def resume(request: Request, project: str | None = None):
    return run(request, R, "resume", S.kn.context.resume, project)


@app.post(API + "/session/checkpoint")
async def checkpoint(request: Request):
    b = await _body(request)
    return run(request, W, "checkpoint", lambda: S.kn.context.checkpoint(
        b["project"], b.get("agent") or request.state.actor, b["summary"], b.get("completed"), b.get("decisions"),
        b.get("questions"), b.get("artifacts"), b.get("changed"), b.get("next"), recovered=b.get("recovered")))


@app.get(API + "/types")
async def types(request: Request):
    return run(request, R, "types", lambda: S.kn.store.db.q("SELECT qname, namespace, name, version, extends, "
                                                            "definition FROM types ORDER BY qname"))


@app.get(API + "/repos")
async def repos(request: Request):
    return run(request, R, "repos", lambda: S.kn.store.db.q("SELECT name, path, version, kind, source FROM repos"))


@app.get(API + "/packages")
async def packages(request: Request, q: str = ""):
    return run(request, R, "packages", S.kn.ws.registry.search, q)


@app.get(API + "/packages/{name}")
async def package(request: Request, name: str, version: str | None = None):
    return run(request, R, "package", PK.inspect, S.kn, name, version)


@app.post(API + "/packages/{name}/check-update")
async def check_update(request: Request, name: str):
    b = await _body(request)
    return run(request, R, "check-update", PK.check_update, S.kn, b.get("repo") or S.kn.ws.repos[0].name, name,
               b.get("version"))


@app.get(API + "/skills")
async def skills(request: Request):
    return run(request, R, "skills", SK.list_skills, S.kn)


@app.post(API + "/skills/{name}/run")
async def run_skill(request: Request, name: str):
    b = await _body(request)
    return run(request, "execute:skills", "skill", SK.run_skill, S.kn, name, b.get("inputs") or {})


@app.get(API + "/views")
async def views(request: Request):
    return run(request, R, "views", lambda: {"views": VW.list_views(S.kn), "apps": VW.list_apps(S.kn)})


@app.get(API + "/views/{name}")
async def view(request: Request, name: str):
    params = dict(request.query_params)
    return run(request, R, "view", VW.render, S.kn, name, params)


@app.get(API + "/queries")
async def queries(request: Request):
    return run(request, R, "queries", S.kn.saved_queries)


@app.post(API + "/queries/{name}")
async def query(request: Request, name: str):
    b = await _body(request)
    return run(request, R, "query", S.kn.run_query, name, b)


@app.get(API + "/model-context/{model:path}")
async def model_context(request: Request, model: str):
    """Knowledge-aware model refresh: what history says about a candidate model or its family."""
    return run(request, R, "model-context", model_history, S.kn, model)


def model_history(kn: Knowledge, model: str) -> dict:
    fam = model.split("/")[-1].split("-")[0].lower()
    hits = kn.search.search(f"{model} {fam}", limit=30, log=False)["results"]
    pick = lambda *ts: [h for h in hits if h["type"] in ts]
    return {"model": model, "family": fam,
            "benchmarks": pick("benchmark"), "incidents": pick("incident"), "lessons": pick("lesson"),
            "decisions": pick("decision"), "models": pick("model"),
            "constraints": [o for o in kn.graph.find(type="assumption", status="active")
                            if "constraint" in (o["fields"].get("tags") or [])],
            "methods": kn.graph.find(type="method", status="active", text="promotion benchmark")}
