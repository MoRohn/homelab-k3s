"""Decision Fabric service (internal, ai-system only).

POST /decision/evaluate   {decision, state, data_class?, thresholds?}           → one decision
POST /decision/batch      {decision, states: [...], data_class?, concurrency?}   → many states, in parallel
POST /decision/score      {decision, state, ...}   (score-type decisions)
POST /decision/route      {state, data_class?}     (request-route)
POST /decision/validate   {state, data_class?}     (output-acceptable)
POST /decision/workflow   {workflow, input, data_class?}                         → DAG run
GET  /decision/status | /decision/workflows | /decision/definitions | /decision/recent
POST /decision/control    {jev_enabled: bool}      operator kill-switch

Outputs are typed recommendations. `action` is the policy gate; callers act on it, never
on the raw provider answer. Hidden reasoning is never produced or exposed.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from lif.common import config, db, log
from lif.decision.dag import DagRuntime
from lif.decision.fabric import DecisionFabric, DecisionLog
from lif.decision.providers import JevProvider, LocalLLMProvider
from lif.decision.rules import rules
from lif.decision.types import load_definitions
from lif.models import discovery

LOG = log.get("lif.decision.app")

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, decision_ref TEXT NOT NULL,
  provider TEXT NOT NULL, decision TEXT NOT NULL, confidence REAL NOT NULL, action TEXT NOT NULL,
  cached INTEGER NOT NULL, latency_ms REAL, cost_usd REAL, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions(ts);
"""


class State:
    def __init__(self):
        self.db = db.DB(os.environ.get("LIF_DECISIONS_DB", "/data/decisions.db"), SCHEMA)
        gw = os.environ.get("LIF_GATEWAY_URL", "http://gateway.ai-system.svc:8080")
        self.fabric = DecisionFabric(
            load_definitions(), rules,
            jev=JevProvider(config.secret("TYPE_SAFE_JEV_API_KEY")),
            local_llm=LocalLLMProvider(gw, "local/instant", api_key=config.secret("LIF_DECISION_GATEWAY_KEY")),
            decision_log=DecisionLog(2000, sink=self._persist))
        self.dag = DagRuntime(self.fabric)
        self.dag.load()
        discovery.build_dag(self.dag, {})      # registers the model-analysis node functions
        self.started = time.time()

    def _persist(self, rec: dict) -> None:
        self.db.x("INSERT INTO decisions(ts,decision_ref,provider,decision,confidence,action,cached,latency_ms,"
                  "cost_usd,record) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (rec["ts"], rec["decision_ref"], rec["provider"], str(rec["decision"]), rec["confidence"],
                   rec["action"], int(rec["cached"]), rec["latency_ms"], rec["cost_usd"], json.dumps(rec, default=str)))

    def prune(self, days: int = 30) -> None:
        self.db.x("DELETE FROM decisions WHERE ts < ?", (time.time() - days * 86400,))


S: State


@asynccontextmanager
async def lifespan(app: FastAPI):
    global S
    log.setup()
    S = State()

    async def daily():
        while True:
            S.prune()
            await asyncio.sleep(3600)
    t = asyncio.create_task(daily())
    yield
    t.cancel()


app = FastAPI(title="LIF decision fabric", lifespan=lifespan)


def _err(code: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


async def _one(body: dict, name: str | None = None) -> JSONResponse:
    name = name or body.get("decision")
    if not name:
        return _err(400, "`decision` is required")
    try:
        r = await S.fabric.evaluate(name, body.get("state"), body.get("data_class"), body.get("thresholds"))
    except KeyError as e:
        return _err(404, str(e))
    return JSONResponse(r.to_dict())


@app.post("/decision/evaluate")
async def evaluate(request: Request):
    return await _one(await request.json())


@app.post("/decision/score")
async def score(request: Request):
    body = await request.json()
    d = S.fabric.defs.get(body.get("decision", ""))
    if d is None or d.type != "score":
        return _err(400, "`decision` must name a score-type decision")
    return await _one(body)


@app.post("/decision/route")
async def route(request: Request):
    return await _one(await request.json(), "request-route")


@app.post("/decision/validate")
async def validate(request: Request):
    return await _one(await request.json(), "output-acceptable")


@app.post("/decision/batch")
async def batch(request: Request):
    body = await request.json()
    states = body.get("states") or []
    if not isinstance(states, list) or len(states) > 10000:
        return _err(400, "`states` must be a list of at most 10000 items")
    t0 = time.perf_counter()
    try:
        rs = await S.fabric.evaluate_many(body["decision"], states, body.get("data_class"),
                                          int(body.get("concurrency", 32)))
    except KeyError as e:
        return _err(404, str(e))
    wall = time.perf_counter() - t0
    return {"results": [r.to_dict() for r in rs], "wall_ms": round(wall * 1000, 1),
            "decisions_per_sec": round(len(rs) / wall, 1) if wall else None,
            "by_action": _count(r.action for r in rs), "by_provider": _count(r.provider for r in rs)}


@app.post("/decision/workflow")
async def workflow(request: Request):
    body = await request.json()
    if body.get("workflow") not in S.dag.workflows:
        return _err(404, f"unknown workflow {body.get('workflow')}")
    run = await S.dag.run(body["workflow"], body.get("input"), body.get("data_class"))
    return run.to_dict()


@app.post("/decision/control")
async def control(request: Request):
    if not config.internal_ok(request):
        return _err(403, "internal key required")
    body = await request.json()
    if "jev_enabled" in body:
        S.fabric.jev_enabled_override = bool(body["jev_enabled"])
        LOG.info("jev kill-switch", extra={"fields": {"jev_enabled": body["jev_enabled"]}})
    if body.get("invalidate_cache") and S.fabric.cache:
        n = S.fabric.cache.invalidate(body.get("decision_ref"))
        return {"invalidated": n, **S.fabric.status()}
    return S.fabric.status()


@app.get("/decision/workflows")
async def workflows():
    return {n: {"version": w.version, "nodes": {k: {**asdict(v)} for k, v in w.nodes.items()},
                "data_class": w.data_class} for n, w in S.dag.workflows.items()}


@app.get("/decision/definitions")
async def definitions():
    return {d.ref: asdict(d) for d in {v.ref: v for v in S.fabric.defs.values()}.values()}


@app.get("/decision/recent")
async def recent(limit: int = 100, decision: str | None = None):
    sql, args = "SELECT record FROM decisions", []
    if decision:
        sql += " WHERE decision_ref LIKE ?"
        args.append(f"{decision}%")
    rows = S.db.q(sql + " ORDER BY id DESC LIMIT ?", tuple(args + [min(limit, 1000)]))
    return {"recent": [json.loads(r["record"]) for r in rows]}


@app.get("/decision/status")
async def status():
    since = time.time() - 86400
    rows = S.db.q("SELECT provider, action, cached, latency_ms, confidence, cost_usd FROM decisions WHERE ts >= ?",
                  (since,))
    lat = [r["latency_ms"] for r in rows if r["latency_ms"] is not None and not r["cached"]]
    out = {**S.fabric.status(), "status": "healthy",
           "window": "24h", "decisions_total": len(rows),
           "by_provider": _count(r["provider"] for r in rows), "by_action": _count(r["action"] for r in rows),
           "cache_hit_rate": round(sum(r["cached"] for r in rows) / len(rows), 3) if rows else None,
           "latency_ms_p50": round(statistics.median(lat), 1) if lat else None,
           "high_confidence_rate": round(sum(1 for r in rows if r["action"] == "auto") / len(rows), 3) if rows else None,
           "escalation_rate": round(sum(1 for r in rows if r["action"] in ("local_llm", "escalate")) / len(rows), 3)
           if rows else None,
           "jev_cost_usd": round(sum(r["cost_usd"] or 0 for r in rows if r["provider"] == "jev"), 6),
           "uptime_sec": round(time.time() - S.started)}
    out["recent"] = (await recent(25))["recent"]
    return out


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/metrics")
async def prom():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _count(xs) -> dict:
    out: dict = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out
