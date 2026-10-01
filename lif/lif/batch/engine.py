"""Batch engine: durable, priority-ordered batch inference on spare capacity.

Jobs and items live in SQLite (one writer: this service). Every item is its own
checkpoint: an item that was `running` when the process died goes back to `pending`
on start, so a restart re-runs at most the items that were in flight.

Scheduling is deterministic (no AI): per job, `can_run(Job(priority, device="cpu"))`
against the BLERBZ snapshot from gpusched decides whether it may dispatch now.
  RUN               global concurrency 2 (CPU tiers are bandwidth-bound; 4-way measured slower)
  RUN_DEGRADED      concurrency 1
  QUEUE / PAUSE_*   not dispatched; the reason is recorded on the job
Order: priority asc, deadline asc, created asc.

Jev classifies each job ONCE at submission (batch-priority + batch-model-size, one
grouped request) from the job's description/metadata only — never item contents.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Callable

import httpx

from lif.common import db, log, metrics
from lif.decision.fabric import DecisionFabric
from lif.gpu.state import Job as GpuJob
from lif.gpu.state import Verdict, can_run
from lif.policy import engine as policy

LOG = log.get("lif.batch")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  data_class TEXT NOT NULL,
  model TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  priority INTEGER NOT NULL,
  priority_source TEXT NOT NULL,
  deadline REAL,
  description TEXT NOT NULL DEFAULT '',
  idem_key TEXT UNIQUE,
  timeout_sec REAL NOT NULL,
  max_attempts INTEGER NOT NULL,
  state TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  classification TEXT NOT NULL DEFAULT '{}',
  model_hint TEXT,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  finished REAL
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS items (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  idx INTEGER NOT NULL,
  custom_id TEXT NOT NULL,
  body TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_at REAL NOT NULL DEFAULT 0,
  response TEXT,
  error TEXT,
  updated REAL NOT NULL,
  PRIMARY KEY (job_id, idx)
);
CREATE INDEX IF NOT EXISTS items_state ON items(job_id, state);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

ENDPOINTS = ("/v1/chat/completions", "/v1/embeddings")
ITEM_TERMINAL = ("succeeded", "failed", "cancelled", "expired")
JOB_ACTIVE = ("queued", "running")
PRIORITY_FROM_DECISION = {"deferrable": 6, "normal": 5, "urgent": 4}


class BatchError(ValueError):
    pass


class BatchEngine:
    def __init__(self, path: str, fabric: DecisionFabric | None, gpu, client: httpx.AsyncClient,
                 gateway_url: str, gateway_key: str = "", clock: Callable[[], float] = time.time,
                 max_concurrency: int = 2, default_timeout_sec: float = 600, default_max_attempts: int = 3):
        self.db = db.DB(path, SCHEMA)
        self.fabric, self.gpu, self.client = fabric, gpu, client
        self.gateway_url, self.gateway_key = gateway_url.rstrip("/"), gateway_key
        self.clock = clock
        self.max_concurrency = max_concurrency
        self.default_timeout, self.default_attempts = default_timeout_sec, default_max_attempts
        self.recover()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def recover(self) -> int:
        """Checkpoint recovery: items in flight when the process died re-run."""
        n = self.db.x("UPDATE items SET state='pending', updated=? WHERE state='running'", (self.clock(),))
        if n:
            LOG.info("recovered in-flight items", extra={"fields": {"items": n}})
        return n

    # ── settings ─────────────────────────────────────────────────────────────

    def setting(self, key: str, default: Any = None) -> Any:
        r = self.db.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))

    def control(self, paused: bool, reason: str = "") -> dict:
        self.set_setting("paused", bool(paused))
        self.set_setting("paused_reason", reason if paused else "")
        LOG.info("batch control", extra={"fields": {"paused": paused, "reason": reason}})
        return {"paused": bool(paused), "reason": reason if paused else ""}

    # ── submit ───────────────────────────────────────────────────────────────

    async def submit(self, body: dict, owner: str = "unknown", data_class: str | None = None) -> tuple[dict, bool]:
        """Returns (job view, created)."""
        idem = body.get("idempotency_key")
        if idem:
            cur = self.db.one("SELECT id FROM jobs WHERE idem_key=?", (str(idem),))
            if cur:
                return self.job(cur["id"]), False
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise BatchError("items must be a non-empty list")
        endpoint = body.get("endpoint", "/v1/chat/completions")
        if endpoint not in ENDPOINTS:
            raise BatchError(f"endpoint must be one of {ENDPOINTS}")
        key = "messages" if endpoint == "/v1/chat/completions" else "input"
        seen = set()
        for i, it in enumerate(items):
            if not isinstance(it, dict) or key not in it:
                raise BatchError(f"item {i} needs '{key}'")
            cid = str(it.get("custom_id", i))
            if cid in seen:
                raise BatchError(f"duplicate custom_id {cid}")
            seen.add(cid)
        priority = body.get("priority")
        if priority is not None and not (4 <= int(priority) <= 8):
            raise BatchError("priority must be 4..8 (P0–P3 are not batch classes)")
        deadline = float(body["deadline"]) if body.get("deadline") is not None else None
        dclass = policy.DataClass.parse(data_class, policy.DataClass.CONFIDENTIAL).name
        now = self.clock()
        description = str(body.get("description") or "")[:2000]

        classification, model_hint = await self._classify(description, deadline, len(items), endpoint, dclass, now)
        if priority is not None:
            prio, src = int(priority), "caller"
        else:
            bp = classification.get("batch-priority") or {}
            if bp.get("action") in (policy.Gate.AUTO, policy.Gate.VALIDATE):
                prio, src = PRIORITY_FROM_DECISION.get(bp.get("decision"), 5), f"decision:{bp.get('provider')}"
            else:
                prio, src = 5, "default"

        jid = "bj_" + uuid.uuid4().hex[:12]
        try:
            with self.db.tx() as c:
                c.execute("INSERT INTO jobs(id,owner,data_class,model,endpoint,priority,priority_source,deadline,"
                          "description,idem_key,timeout_sec,max_attempts,state,classification,model_hint,created,"
                          "updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (jid, owner, dclass, body.get("model") or "local/batch", endpoint, prio, src, deadline,
                           description, str(idem) if idem else None,
                           float(body.get("timeout_sec") or self.default_timeout),
                           int(body.get("max_attempts") or self.default_attempts), "queued",
                           json.dumps(classification), model_hint, now, now))
                c.executemany("INSERT INTO items(job_id,idx,custom_id,body,state,updated) VALUES(?,?,?,?,?,?)",
                              [(jid, i, str(it.get("custom_id", i)),
                                json.dumps({k: v for k, v in it.items() if k != "custom_id"}), "pending", now)
                               for i, it in enumerate(items)])
        except Exception as exc:
            if idem and "UNIQUE" in str(exc):          # concurrent duplicate submit
                cur = self.db.one("SELECT id FROM jobs WHERE idem_key=?", (str(idem),))
                return self.job(cur["id"]), False
            raise
        LOG.info("batch submitted", extra={"fields": {"job": jid, "items": len(items), "priority": prio,
                                                      "priority_source": src, "owner": owner}})
        self._update_queue_gauge()
        return self.job(jid), True

    async def _classify(self, description: str, deadline: float | None, n: int, endpoint: str, dclass: str,
                        now: float) -> tuple[dict, str | None]:
        if self.fabric is None:
            return {}, None
        state = {"description": description, "task": description, "items": n, "endpoint": endpoint,
                 "deadline_hours": round((deadline - now) / 3600, 2) if deadline else None}
        try:
            res = await self.fabric.evaluate_group(["batch-priority", "batch-model-size"], state, data_class=dclass)
        except Exception as exc:          # classification is advisory; never block a submission on it
            LOG.warning("batch classification failed", extra={"fields": {"err": str(exc)[:160]}})
            return {"error": str(exc)[:200]}, None
        out = {k: {"decision": r.decision, "confidence": round(r.confidence, 3), "provider": r.provider,
                   "action": r.action} for k, r in res.items()}
        ms = out.get("batch-model-size") or {}
        hint = f"local/{ms['decision']}" if ms.get("action") in (policy.Gate.AUTO, policy.Gate.VALIDATE) else None
        return out, hint

    # ── read ─────────────────────────────────────────────────────────────────

    def job(self, jid: str) -> dict | None:
        j = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not j:
            return None
        j["classification"] = json.loads(j["classification"])
        counts = {r["state"]: r["n"] for r in self.db.q(
            "SELECT state, count(*) AS n FROM items WHERE job_id=? GROUP BY state", (jid,))}
        total = sum(counts.values())
        done = sum(counts.get(s, 0) for s in ITEM_TERMINAL)
        j["counts"] = {s: counts.get(s, 0) for s in ("pending", "running", *ITEM_TERMINAL)}
        j["total"] = total
        j["progress"] = round(done / total, 4) if total else 1.0
        # names the CLI / Control Center read (same data)
        j["status"], j["completed"], j["failed"] = j["state"], j["counts"]["succeeded"], j["counts"]["failed"]
        j.pop("idem_key", None)
        return j

    def jobs(self, limit: int = 100, state: str | None = None) -> list[dict]:
        sql, args = "SELECT id FROM jobs", ()
        if state:
            sql, args = sql + " WHERE state=?", (state,)
        rows = self.db.q(sql + " ORDER BY created DESC LIMIT ?", (*args, limit))
        return [self.job(r["id"]) for r in rows]

    def results(self, jid: str) -> list[dict]:
        out = []
        for r in self.db.q("SELECT custom_id, state, response, error, attempts FROM items WHERE job_id=? "
                           "ORDER BY idx", (jid,)):
            rec: dict[str, Any] = {"custom_id": r["custom_id"], "status": r["state"], "attempts": r["attempts"]}
            if r["response"]:
                rec["response"] = json.loads(r["response"])
            if r["error"]:
                rec["error"] = r["error"]
            out.append(rec)
        return out

    def stats(self) -> dict:
        snap = self.gpu.current()
        q = self.db.q("SELECT j.priority AS p, count(*) AS n FROM items i JOIN jobs j ON j.id=i.job_id "
                      "WHERE i.state='pending' AND j.state IN ('queued','running','paused') GROUP BY j.priority")
        totals = {r["state"]: r["n"] for r in self.db.q("SELECT state, count(*) AS n FROM items GROUP BY state")}
        jobs = {r["state"]: r["n"] for r in self.db.q("SELECT state, count(*) AS n FROM jobs GROUP BY state")}
        return {"queue_depth": {f"P{r['p']}": r["n"] for r in q},
                "pending": sum(r["n"] for r in q),
                "running": totals.get("running", 0),
                "paused": bool(self.setting("paused", False)),
                "paused_reason": self.setting("paused_reason", ""),
                "blerbz": {"state": snap.state.name, "reason": snap.reason},
                "items": totals, "jobs": jobs,
                # names the Control Center reads (same data)
                "queued": sum(r["n"] for r in q), "queue_by_priority": {f"P{r['p']}": r["n"] for r in q},
                "pause_reason": self.setting("paused_reason", ""), "totals": totals,
                "recent": self.jobs(10)}

    # ── control ──────────────────────────────────────────────────────────────

    def cancel(self, jid: str) -> dict:
        j = self.job(jid)
        if not j:
            raise KeyError(jid)
        if j["state"] in ("completed", "cancelled", "expired", "failed"):
            return j
        now = self.clock()
        with self.db.tx() as c:
            c.execute("UPDATE items SET state='cancelled', updated=? WHERE job_id=? AND state='pending'", (now, jid))
            c.execute("UPDATE jobs SET state='cancelled', reason='cancelled by owner', updated=?, finished=? "
                      "WHERE id=?", (now, now, jid))
        # running items finish (their result is kept) but nothing new is dispatched
        self._update_queue_gauge()
        return self.job(jid)

    def pause(self, jid: str) -> dict:
        j = self.job(jid)
        if not j:
            raise KeyError(jid)
        if j["state"] in JOB_ACTIVE:
            self.db.x("UPDATE jobs SET state='paused', reason='paused by owner', updated=? WHERE id=?",
                      (self.clock(), jid))
        return self.job(jid)

    def resume(self, jid: str) -> dict:
        j = self.job(jid)
        if not j:
            raise KeyError(jid)
        if j["state"] == "paused":
            done = j["counts"]["succeeded"] + j["counts"]["failed"]
            self.db.x("UPDATE jobs SET state=?, reason='', updated=? WHERE id=?",
                      ("running" if done else "queued", self.clock(), jid))
        return self.job(jid)

    # ── scheduling ───────────────────────────────────────────────────────────

    async def tick(self) -> int:
        """One scheduling pass. Dispatches up to the allowed concurrency, waits for those
        items, then settles job states. Returns the number of items dispatched."""
        now = self.clock()
        self._expire(now)
        if self.setting("paused", False):
            self._note_waiting("waiting: batch paused by operator" +
                               (f" ({self.setting('paused_reason')})" if self.setting("paused_reason") else ""))
            self._update_queue_gauge()
            return 0
        snap = self.gpu.current()
        jobs = self.db.q("SELECT * FROM jobs WHERE state IN ('queued','running') "
                         "ORDER BY priority ASC, COALESCE(deadline, 9e18) ASC, created ASC")
        limit: int | None = None
        picks: list[tuple[dict, dict]] = []
        for j in jobs:
            verdict, why = can_run(GpuJob(priority=int(j["priority"]), device="cpu"), snap)
            if verdict in (Verdict.QUEUE, Verdict.PAUSE_BACKGROUND, Verdict.REJECT):
                self._set_reason(j["id"], f"waiting: {why}")
                continue
            job_limit = 1 if verdict == Verdict.RUN_DEGRADED else self.max_concurrency
            limit = job_limit if limit is None else min(limit, job_limit)
            if len(picks) >= limit:
                break
            need = limit - len(picks)
            items = self.db.q("SELECT * FROM items WHERE job_id=? AND state='pending' AND next_at<=? "
                              "ORDER BY idx LIMIT ?", (j["id"], now, need))
            if items:
                self._set_reason(j["id"], "" if verdict == Verdict.RUN else f"running degraded: {why}")
            for it in items:
                picks.append((j, it))
        if not picks:
            self._settle()
            self._update_queue_gauge()
            return 0
        with self.db.tx() as c:
            for j, it in picks:
                c.execute("UPDATE items SET state='running', attempts=attempts+1, updated=? WHERE job_id=? AND idx=?",
                          (now, j["id"], it["idx"]))
                if j["state"] == "queued":
                    c.execute("UPDATE jobs SET state='running', updated=? WHERE id=?", (now, j["id"]))
        await asyncio.gather(*(self._dispatch(j, it) for j, it in picks))
        self._settle()
        self._update_queue_gauge()
        return len(picks)

    async def _dispatch(self, job: dict, item: dict) -> None:
        body = json.loads(item["body"])
        body.setdefault("model", job["model"])
        headers = {"X-LIF-Workload": f"batch:{job['id']}", "X-LIF-Priority": str(job["priority"]),
                   "X-LIF-Data-Class": job["data_class"]}
        if self.gateway_key:
            headers["Authorization"] = f"Bearer {self.gateway_key}"
        attempts = int(item["attempts"]) + 1          # the increment made at dispatch
        retryable, err = False, ""
        try:
            r = await self.client.post(f"{self.gateway_url}{job['endpoint']}", json=body, headers=headers,
                                       timeout=float(job["timeout_sec"]))
            if r.status_code == 200:
                self._finish_item(job["id"], item["idx"], "succeeded", response=r.text)
                return
            err = f"gateway {r.status_code}: {r.text[:300]}"
            retryable = r.status_code >= 500 or r.status_code == 429
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            err, retryable = f"{type(exc).__name__}: {str(exc)[:200]}", True
        except Exception as exc:
            err, retryable = f"{type(exc).__name__}: {str(exc)[:200]}", False
        if retryable and attempts < int(job["max_attempts"]):
            backoff = min(60.0, 2.0 ** attempts)
            self.db.x("UPDATE items SET state='pending', next_at=?, error=?, updated=? WHERE job_id=? AND idx=?",
                      (self.clock() + backoff, err, self.clock(), job["id"], item["idx"]))
            metrics.batch_items.labels("retried").inc()
        else:
            self._finish_item(job["id"], item["idx"], "failed", error=err)

    def _finish_item(self, jid: str, idx: int, state: str, response: str | None = None, error: str | None = None):
        self.db.x("UPDATE items SET state=?, response=?, error=?, updated=? WHERE job_id=? AND idx=?",
                  (state, response, error, self.clock(), jid, idx))
        metrics.batch_items.labels(state).inc()

    def _expire(self, now: float) -> None:
        for j in self.db.q("SELECT id FROM jobs WHERE state IN ('queued','running','paused') "
                           "AND deadline IS NOT NULL AND deadline < ?", (now,)):
            n = self.db.x("UPDATE items SET state='expired', error='deadline passed', updated=? "
                          "WHERE job_id=? AND state='pending'", (now, j["id"]))
            if n:
                metrics.batch_items.labels("expired").inc(n)
            self._set_reason(j["id"], "deadline passed")
        self._settle()

    def _settle(self) -> None:
        """Move jobs whose items are all terminal to their final state."""
        now = self.clock()
        for j in self.db.q("SELECT id, state FROM jobs WHERE state IN ('queued','running','paused')"):
            c = {r["state"]: r["n"] for r in self.db.q(
                "SELECT state, count(*) AS n FROM items WHERE job_id=? GROUP BY state", (j["id"],))}
            if c.get("pending", 0) or c.get("running", 0):
                continue
            total = sum(c.values())
            if c.get("expired", 0):
                final = "expired"
            elif total and c.get("failed", 0) == total:
                final = "failed"
            else:
                final = "completed"
            self.db.x("UPDATE jobs SET state=?, updated=?, finished=? WHERE id=?", (final, now, now, j["id"]))
            LOG.info("batch finished", extra={"fields": {"job": j["id"], "state": final, **c}})

    def _set_reason(self, jid: str, reason: str) -> None:
        self.db.x("UPDATE jobs SET reason=? WHERE id=? AND reason<>?", (reason, jid, reason))

    def _note_waiting(self, reason: str) -> None:
        self.db.x("UPDATE jobs SET reason=? WHERE state IN ('queued','running') AND reason<>?", (reason, reason))

    def _update_queue_gauge(self) -> None:
        rows = self.db.q("SELECT j.priority AS p, count(*) AS n FROM items i JOIN jobs j ON j.id=i.job_id "
                         "WHERE i.state='pending' AND j.state IN ('queued','running','paused') GROUP BY j.priority")
        seen = {int(r["p"]): r["n"] for r in rows}
        for p in range(4, 9):
            metrics.batch_queue.labels(f"P{p}").set(seen.get(p, 0))

    async def run(self, idle_sec: float = 2.0) -> None:
        while True:
            try:
                n = await self.tick()
            except Exception:
                LOG.exception("batch tick failed")
                n = 0
            # Items in a retry backoff or jobs waiting on BLERBZ: poll slowly.
            await asyncio.sleep(0.05 if n else idle_sec)
