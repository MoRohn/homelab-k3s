"""Decision Engineering HTTP API (mounted on the decision-fabric service under /de).

Read routes are open inside ai-system (NetworkPolicy). Routes that change what serves
(transition, rollback, human answers, inventory upload) need X-LIF-Internal.

  POST /de/decide           {decision, state, data_class?, agent?, workflow?, choices?, explain?}
  POST /de/decide_many      {requests: [{name, state, depends_on?, data_class?}]}
  POST /de/outcome          {provenance_id | state_hash+decision, outcome, source?}
  GET  /de/registry         · GET /de/registry/{name}
  POST /de/lint             {spec}                       → findings
  POST /de/test             {decision, cases?}           → test report (stored as an experiment)
  POST /de/benchmark        {decision, against?}         → old vs new regression comparison
  POST /de/calibrate        {decision, use_agreement?}   → calibration report (saved)
  POST /de/simulate         {decision, volume_per_day, thresholds?, escalate_to?}
  POST /de/transition       {ref, stage, actor, reason, policy?, thresholds?, rollout_pct?}   [internal]
  POST /de/rollback         {name, actor, reason}                                             [internal]
  GET  /de/shadow/{ref}     · GET /de/autotune/{ref} · GET /de/experiments
  GET  /de/human            · POST /de/human/{ticket} {answer, reviewer}                     [internal]
  GET  /de/overview         · GET /de/cascade · GET /de/inventory · GET /de/traces · GET /de/traces/{run_id}
  POST /de/inventory        {operations: [...], inventory: [...]}   (from `local-ai agent audit --upload`) [internal]
"""
from __future__ import annotations

import dataclasses
import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from lif.common import config
from lif.decision import autotune, calibration, experiments, fanout, pricing, shadow
from lif.decision.lint import lint, summarize
from lif.decision.registry import RegistryError
from lif.decision.types import DecisionDef

router = APIRouter(prefix="/de")
RT: Any = None                     # engineering.Runtime, set by the app at startup
LATEST_INVENTORY: dict = {}


def _err(code: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


def _internal(request: Request) -> bool:
    return config.internal_ok(request)


@router.post("/decide")
async def decide(request: Request):
    b = await request.json()
    try:
        d = await RT.intel.decide(b["decision"], b.get("state") or {}, agent=b.get("agent", ""),
                                  workflow=b.get("workflow", ""), data_class=b.get("data_class"),
                                  choices=b.get("choices"), explain=bool(b.get("explain")),
                                  untrusted_paths=b.get("untrusted_paths"))
    except KeyError as e:
        return _err(404, str(e))
    except ValueError as e:
        return _err(400, str(e))
    return dataclasses.asdict(d)


@router.post("/decide_many")
async def decide_many(request: Request):
    b = await request.json()
    reqs = [fanout.DecisionRequest(name=r["name"], state=r.get("state") or {}, depends_on=r.get("depends_on") or [],
                                   data_class=r.get("data_class")) for r in b.get("requests") or []]
    if any(r.depends_on for r in reqs):
        return _err(400, "dependent decisions need state built from earlier answers; use the SDK (state_fn)")
    try:
        res, stats = await RT.intel.decide_many(reqs, agent=b.get("agent", ""), workflow=b.get("workflow", ""))
    except (KeyError, ValueError) as e:
        return _err(400, str(e))
    return {"results": {k: dataclasses.asdict(v) for k, v in res.items()}, "fanout": stats}


@router.post("/outcome")
async def outcome(request: Request):
    b = await request.json()
    try:
        return shadow.record_outcome(RT.store, outcome=b["outcome"], provenance_id=b.get("provenance_id"),
                                     state_hash=b.get("state_hash"), decision=b.get("decision"),
                                     source=b.get("source", "observed"))
    except (KeyError, ValueError) as e:
        return _err(400, str(e))


@router.get("/registry")
async def registry():
    return {"decisions": RT.registry.summary()}


@router.get("/registry/{name}")
async def registry_one(name: str):
    vs = RT.registry.versions(name)
    if not vs:
        return _err(404, f"unknown decision {name}")
    out = []
    for d in vs:
        fs = lint(d, RT.package_dir(d))
        out.append({**dataclasses.asdict(d), "ref": d.ref, "stage_now": RT.registry.stage_of(d.ref),
                    "lint": summarize(fs), "findings": [f.to_dict() for f in fs],
                    "latest_test": _slim(RT.latest_test(d.ref)), "calibration": RT.latest_calibration(d.ref),
                    "shadow": shadow.summary(RT.store, d.ref)})
    serving = RT.registry.resolve(name)
    return {"name": name, "serving": serving.ref if serving else None, "versions": out,
            "history": [r.to_dict() for r in RT.registry.history(name)]}


@router.post("/lint")
async def lint_spec(request: Request):
    b = await request.json()
    raw = b.get("spec") or {}
    acks = list(raw.pop("lint_ack", []) or [])
    try:
        d = DecisionDef.from_raw(raw)
    except (TypeError, ValueError) as e:
        return {"findings": [{"code": "invalid-spec", "severity": "error", "message": str(e), "where": "file"}],
                "summary": {"error": 1, "warning": 0, "info": 0}}
    fs = lint(d, acks=acks)
    return {"ref": d.ref, "findings": [f.to_dict() for f in fs], "summary": summarize(fs)}


@router.post("/test")
async def test(request: Request):
    b = await request.json()
    ref = b["decision"]
    try:
        d = RT.registry.get(ref) if "/" in ref else RT.fabric.definition(ref)
    except (RegistryError, KeyError) as e:
        return _err(404, str(e))
    cases = b.get("cases")
    if cases is None:
        pd = RT.package_dir(d)
        cases = experiments.load_cases(pd / "tests.jsonl") if pd else []
    if not cases:
        return _err(400, f"{d.ref}: no test cases (tests.jsonl)")
    rep = await experiments.run_tests(RT.fabric, d.ref, cases, data_class=b.get("data_class"))
    rid = experiments.save(RT.store, "decision-test", "labels", d.ref, rep)
    return {**_slim(rep), "experiment_id": rid}


@router.post("/benchmark")
async def benchmark(request: Request):
    b = await request.json()
    ref = b["decision"]
    d = RT.registry.get(ref)
    against = b.get("against") or next((v.ref for v in reversed(RT.registry.versions(d.name))
                                        if v.version != d.version), None)
    new, old = RT.latest_test(ref), RT.latest_test(against) if against else None
    if not new or not old:
        return _err(400, f"run `decision test` for both {ref} and {against} first")
    cmp = experiments.compare(old, new)
    experiments.save(RT.store, "decision-regression", against, ref, cmp)
    return cmp


@router.post("/calibrate")
async def calibrate(request: Request):
    b = await request.json()
    try:
        return RT.calibrate(b["decision"], use_agreement=bool(b.get("use_agreement")))
    except RegistryError as e:
        return _err(404, str(e))


@router.post("/simulate")
async def simulate(request: Request):
    b = await request.json()
    ref = b["decision"]
    samples = shadow.samples(RT.store, ref)
    if not samples:
        test = RT.latest_test(ref)
        samples = [calibration.Sample(**s) for s in (test or {}).get("samples", [])]
    if not samples:
        return _err(400, f"{ref}: no shadow or test samples to simulate on")
    esc = b.get("escalate_to", "kimi-k3")
    sh = shadow.summary(RT.store, ref)
    jev_lat = (sh.get("candidate_latency_ms") or {}).get("p50") or float(
        config.get("decision_engineering.jev_latency_ms_estimate", 300))
    tok = int(b.get("state_tokens", config.get("decision_engineering.jev_state_tokens_estimate", 400)))
    costs = {"jev": pricing.call_cost("typesafe-jev", tok, 0) or 0.0,
             "esc": pricing.call_cost(esc, tok * 2, 300) or 0.0}
    lat = {"jev": jev_lat, "esc": float(b.get("escalation_latency_ms", 8000))}
    rows = calibration.simulate(samples, float(b.get("volume_per_day", 1000)), costs, lat, b.get("thresholds"),
                                escalate_to="esc", use_agreement=bool(b.get("use_agreement")))
    return {"decision": ref, "samples": len(samples), "escalate_to": esc, "assumptions": {
        "state_tokens": tok, "jev_latency_ms": jev_lat, "escalation_latency_ms": lat["esc"],
        "prices": "providers.yaml"}, "rows": rows}


@router.post("/transition")
async def transition(request: Request):
    if not _internal(request):
        return _err(403, "internal key required")
    b = await request.json()
    pol = b.get("policy") or {}
    ev = RT.evidence(b["ref"], pol)
    try:
        rel = RT.registry.transition(b["ref"], b["stage"], actor=b.get("actor", ""), reason=b.get("reason", ""),
                                     evidence=ev, thresholds=b.get("thresholds"), policy=pol,
                                     rollout_pct=b.get("rollout_pct"))
    except RegistryError as e:
        return _err(409, str(e))
    RT.refresh(rel.name)
    from lif.decision import provenance
    provenance.knowledge("decision-release", {"id": f"{rel.name}-{rel.version}-{rel.id}", "title":
                                              f"{rel.ref} → {rel.stage}", "actor": rel.actor, "reason": rel.reason,
                                              "evidence": ev}, [("version-of", rel.name)])
    return {"release": rel.to_dict(), "evidence": ev}


@router.post("/rollback")
async def rollback(request: Request):
    if not _internal(request):
        return _err(403, "internal key required")
    b = await request.json()
    try:
        rel = RT.registry.rollback(b["name"], actor=b.get("actor", ""), reason=b.get("reason", ""))
    except RegistryError as e:
        return _err(409, str(e))
    RT.refresh(b["name"])
    serving = RT.registry.resolve(b["name"])
    return {"serving": serving.ref if serving else None, "release": rel.to_dict() if rel else None}


@router.get("/shadow/{ref:path}")
async def shadow_summary(ref: str):
    return shadow.summary(RT.store, ref)


@router.get("/autotune/{ref:path}")
async def tune(ref: str):
    d = RT.registry.get(ref)
    rel = RT.registry.active_release(d.name)
    return {"decision": ref, "recommendations": autotune.recommend(
        d, RT.latest_calibration(ref), shadow.summary(RT.store, ref), RT.latest_test(ref),
        pinned_high=(rel.policy or {}).get("high") if rel else d.policy.get("high"))}


@router.get("/experiments")
async def exps(limit: int = 50):
    rows = RT.store.q("SELECT id, ts, name, baseline, candidate, verdict, report FROM experiments "
                      "ORDER BY id DESC LIMIT ?", (min(limit, 500),))
    return {"experiments": [{**{k: v for k, v in r.items() if k != "report"},
                             "summary": _slim(json.loads(r["report"]))} for r in rows]}


@router.get("/human")
async def human(status: str = "pending"):
    rows = RT.store.q("SELECT * FROM human_queue WHERE status=? ORDER BY id DESC LIMIT 200", (status,))
    return {"queue": [{**r, "package": json.loads(r["package"])} for r in rows]}


@router.post("/human/{ticket}")
async def human_answer(ticket: int, request: Request):
    if not _internal(request):
        return _err(403, "internal key required")
    b = await request.json()
    hp = RT.cascade.human
    try:
        row = hp.answer(ticket, b["answer"], b.get("reviewer", "operator"))
    except (KeyError, ValueError) as e:
        return _err(400, str(e))
    if row.get("provenance_id"):
        shadow.record_outcome(RT.store, outcome=b["answer"], provenance_id=row["provenance_id"], source="human")
    return {"ticket": ticket, "status": "answered"}


@router.get("/overview")
async def overview(hours: float = 24):
    return {**RT.overview(hours * 3600), "providers": RT.cascade.status()["providers"],
            "registry": RT.registry.summary(), "inventory_generated_at": LATEST_INVENTORY.get("generated_at")}


@router.get("/cascade")
async def cascade_edges(hours: float = 168):
    return RT.cascade_edges(hours * 3600)


@router.get("/inventory")
async def inventory():
    if LATEST_INVENTORY:
        return LATEST_INVENTORY
    return {"inventory": [], "note": "no mining run yet: run `local-ai agent audit <agent> --upload`"}


@router.post("/inventory")
async def upload_inventory(request: Request):
    if not _internal(request):
        return _err(403, "internal key required")
    b = await request.json()
    LATEST_INVENTORY.clear()
    LATEST_INVENTORY.update({k: b.get(k) for k in ("inventory", "audit", "steps_by_bucket", "runs", "span_days")})
    LATEST_INVENTORY["generated_at"] = b.get("generated_at", time.time())
    ops = b.get("operations") or []
    if ops:
        from lif.decision.mining.classify import Operation
        fields = Operation.__dataclass_fields__
        RT.save_operations([Operation(**{k: v for k, v in o.items() if k in fields}) for o in ops])
    return {"stored_operations": len(ops), "inventory_items": len(b.get("inventory") or [])}


@router.get("/traces")
async def traces(limit: int = 50):
    rows = RT.store.q("SELECT run_id, agent, workflow, MIN(ts) AS t0, COUNT(*) AS ops, "
                      "SUM(classification='JEV_CANDIDATE') AS jev, SUM(classification='GENERATIVE') AS gen, "
                      "SUM(classification='CODE') AS code, SUM(classification='HUMAN_OR_POLICY') AS human "
                      "FROM operations GROUP BY run_id ORDER BY t0 DESC LIMIT ?", (min(limit, 500),))
    return {"runs": rows}


@router.get("/traces/{run_id:path}")
async def trace(run_id: str):
    rows = RT.store.q("SELECT seq, ts, kind, name, classification, class_confidence, executor, input_tokens, "
                      "output_tokens, latency_ms, output_label, reasons FROM operations WHERE run_id=? "
                      "ORDER BY seq, kind", (run_id,))
    return {"run_id": run_id, "operations": [{**r, "reasons": json.loads(r["reasons"])} for r in rows]}


def _slim(rep: dict | None) -> dict | None:
    return None if rep is None else {k: v for k, v in rep.items() if k != "samples"}


@router.post("/compile")
async def compile_candidate(request: Request):
    """Draft a candidate spec from an inventory row (preview only; nothing is written)."""
    from lif.decision.mining.compiler import compile_item
    from lif.decision.mining.miner import InventoryItem
    b = await request.json()
    row = next((i for i in LATEST_INVENTORY.get("inventory") or [] if i.get("signature") == b.get("signature")), None)
    if row is None:
        return _err(404, "signature not in the latest inventory")
    fields = InventoryItem.__dataclass_fields__
    item = InventoryItem(**{k: (tuple(map(tuple, v)) if k == "top_outputs" else v) for k, v in row.items()
                            if k in fields})
    c = compile_item(item)
    return {"ref": c.ref, "spec": c.spec, "lint": c.lint, "findings": c.findings, "reuse": c.reuse, "notes": c.notes,
            "next": f"local-ai decision generate {item.signature}  (writes the draft under private/lif/decisions/)"}


@router.get("/providers")
async def providers():
    cfg = pricing.providers()
    status = RT.cascade.status()["providers"]
    return {"providers": {k: {"kind": v.get("kind"), "model": v.get("model") or v.get("alias"),
                              "pricing": v.get("pricing"), "rate_limit": v.get("rate_limit"),
                              "privacy": v.get("privacy_destination")} for k, v in cfg.items()},
            "runtime": status, "jev": {"model": RT.fabric.jev.model if RT.fabric.jev else None,
                                        "enabled": bool(RT.fabric.jev and RT.fabric.jev.enabled),
                                        "breaker_open": bool(RT.fabric.jev and RT.fabric.jev.breaker.open)}}


@router.get("/cycle")
async def last_cycle():
    from lif.decision import jobs
    return jobs.LAST or {"note": "no improvement cycle has run yet"}


@router.post("/observe")
async def observe(request: Request):
    """Shadow a real agent (spec §29, §92). The agent reports the decision it made with its
    own logic; that answer is recorded as served (route=baseline), every shadow-stage version
    of `decision` evaluates the same state, and disagreements, plus a sample of agreements,
    go to the human review queue, whose answers become calibration outcomes.

    {decision, state, data_class?, agent, workflow?, baseline: {answer, executor?, confidence?,
     latency_ms?, cost_usd?}}
    """
    import hashlib
    from lif.decision.escalation import EscalationPackage
    from lif.decision.instrument import TraceWriter
    from lif.decision.state_compiler import MissingState, compile_state
    b = await request.json()
    name, base = b.get("decision"), b.get("baseline") or {}
    versions = RT.registry.versions(name or "")
    if not versions:
        return _err(404, f"unknown decision {name!r}")
    cands = RT.registry.shadow_candidates(name)
    d = cands[-1] if cands else versions[-1]
    if base.get("answer") not in d.labels:
        return _err(400, f"baseline.answer must be one of {d.labels}")
    try:
        state = compile_state(d, b.get("state") or {}).state
    except MissingState as e:
        return _err(400, str(e))
    agent, workflow = b.get("agent", "unknown"), b.get("workflow", "")
    served = await RT.cascade._baseline(name, state, lambda s: (base["answer"], {
        "executor": base.get("executor", agent), "confidence": base.get("confidence", 1.0),
        "cost_usd": base.get("cost_usd")}))
    if base.get("latency_ms") is not None:
        served.latency_ms = float(base["latency_ms"])
    RT.cascade._provenance(name, state, served, agent, workflow)
    shadows = []
    if cands:
        await shadow.record_shadow(RT.fabric, RT.store, cands, state, b.get("data_class"), served)
        sh = provenance_hash(state)
        for c in cands:
            row = RT.store.q("SELECT * FROM shadow WHERE decision_ref=? AND state_hash=? ORDER BY id DESC LIMIT 1",
                             (c.ref, sh))
            if row:
                shadows.append({"ref": c.ref, "answer": row[0]["candidate_answer"],
                                "confidence": row[0]["candidate_confidence"], "provider": row[0]["candidate_provider"],
                                "probabilities": json.loads(row[0]["candidate_probs"])})
    # human evidence: every disagreement, plus a stable sample of agreements (unbiased labels)
    pct = float((config.get("decision_engineering.observe") or {}).get("review_agreement_pct", 20))
    disagree = any(s["answer"] != served.answer for s in shadows)
    sampled = int(hashlib.sha256(provenance_hash(state).encode()).hexdigest()[:4], 16) / 0xFFFF * 100 < pct
    ticket = None
    if shadows and (disagree or sampled) and RT.cascade.human is not None:
        top = shadows[-1]
        pkg = EscalationPackage.build(d, state, "shadow_disagreement" if disagree else "shadow_sample",
                                      b.get("data_class") or d.data_class,
                                      {"answer": top["answer"], "confidence": round(top["confidence"], 4),
                                       "probabilities": top["probabilities"], "version": top["ref"]},
                                      [{"tier": "agent", "executor": served.executor, "answer": served.answer}])
        hr = await RT.cascade.human.resolve(pkg, provenance_id=served.provenance_id)
        ticket = hr.ticket
    TraceWriter(agent, workflow).step(kind="llm", name=name, purpose=name, output=served.answer,
                                      model=served.executor, latency_ms=served.latency_ms,
                                      features={"observed": True, "shadow_agree": not disagree if shadows else None})
    return {"provenance_id": served.provenance_id, "served": served.answer, "shadow": shadows,
            "disagreement": disagree, "review_ticket": ticket}


def provenance_hash(state) -> str:
    from lif.decision.provenance import state_hash
    return state_hash(state)
