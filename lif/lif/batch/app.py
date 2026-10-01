"""LIF batch service (FastAPI, :8080). Applications reach it through the gateway's /v1/batch.

Routes: POST/GET /v1/batch, GET/DELETE /v1/batch/{id}, GET /v1/batch/{id}/results,
POST /v1/batch/{id}/pause|resume, GET /v1/batch/stats, POST /v1/batch/control,
GET /healthz, GET /metrics.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from lif.batch.engine import BatchEngine, BatchError
from lif.common import config, log
from lif.decision.fabric import DecisionFabric
from lif.decision.providers import JevProvider
from lif.decision.rules import rules
from lif.decision.types import load_definitions
from lif.gpu.state import GpuStateWatcher

LOG = log.get("lif.batch.app")

E: BatchEngine
_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global E
    log.setup()
    gpu = GpuStateWatcher()
    await gpu.refresh()
    fabric = DecisionFabric(load_definitions(), rules, jev=JevProvider(config.secret("TYPE_SAFE_JEV_API_KEY")))
    E = BatchEngine(os.environ.get("LIF_BATCH_DB", "/data/batch.db"), fabric, gpu,
                    httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5)),
                    os.environ.get("LIF_GATEWAY_URL", "http://gateway.ai-system.svc:8080"),
                    config.secret("LIF_BATCH_GATEWAY_KEY") or "",
                    default_timeout_sec=float(config.get("batch.default_timeout_sec", 600)),
                    default_max_attempts=int(config.get("batch.max_attempts", 3)))
    if not config.get("batch.enabled", True):
        E.control(True, "batch.enabled=false in config")
    async def kill_switch():
        url = os.environ.get("LIF_CONTROLLER_URL", "http://controller.ai-system.svc:8080")
        async with httpx.AsyncClient(timeout=5) as c:
            while True:
                try:
                    t = (await c.get(f"{url}/v1/routing")).json()
                    if "jev_enabled" in t:
                        fabric.jev_enabled_override = bool(t["jev_enabled"])
                except Exception:
                    pass                     # keep the last setting; Jev failures fall back anyway
                await asyncio.sleep(15)
    _tasks[:] = [asyncio.create_task(gpu.run()), asyncio.create_task(E.run()),
                 asyncio.create_task(kill_switch())]
    yield
    for t in _tasks:
        t.cancel()


app = FastAPI(title="LIF batch", lifespan=lifespan)


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": {"message": msg}}, status_code=status)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/metrics")
async def prom():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/batch")
async def create(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _err(400, "body must be JSON")
    if not isinstance(body, dict):
        return _err(400, "body must be a JSON object")
    try:
        job, created = await E.submit(body, owner=request.headers.get("x-lif-client") or "unknown",
                                      data_class=request.headers.get("x-lif-data-class") or None)
    except BatchError as e:
        return _err(400, str(e))
    return JSONResponse(job, status_code=201 if created else 200)


@app.get("/v1/batch")
async def list_jobs(limit: int = 100, state: str | None = None):
    return {"data": E.jobs(min(limit, 1000), state)}


@app.get("/v1/batch/stats")
async def stats():
    return E.stats()


@app.post("/v1/batch/control")
async def control(request: Request):
    if not config.internal_ok(request):
        return _err(403, "internal key required")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "body must be JSON")
    return E.control(bool(body.get("paused")), str(body.get("reason") or ""))


@app.get("/v1/batch/{jid}")
async def get(jid: str):
    j = E.job(jid)
    return j if j else _err(404, f"no batch {jid}")


@app.get("/v1/batch/{jid}/results")
async def results(jid: str):
    if not E.job(jid):
        return _err(404, f"no batch {jid}")
    return {"id": jid, "data": E.results(jid)}


@app.delete("/v1/batch/{jid}")
async def cancel(jid: str):
    try:
        return E.cancel(jid)
    except KeyError:
        return _err(404, f"no batch {jid}")


@app.post("/v1/batch/{jid}/pause")
async def pause(jid: str):
    try:
        return E.pause(jid)
    except KeyError:
        return _err(404, f"no batch {jid}")


@app.post("/v1/batch/{jid}/resume")
async def resume(jid: str):
    try:
        return E.resume(jid)
    except KeyError:
        return _err(404, f"no batch {jid}")
