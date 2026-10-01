"""Model registry: lifecycle state machine, pinned revisions, alias versions, activity.

Invariants enforced here (not by callers):
  * every model row has an immutable 40-char commit SHA; `main` is never stored
  * state changes follow ALLOWED transitions; every change is an activity event with a reason
  * alias changes are versioned (alias_versions); rollback restores a previous version
  * a PRODUCTION model referenced by an alias can't be deleted
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from lif.common import config, db, metrics

STATES = ["DISCOVERED", "CANDIDATE", "DOWNLOADING", "STAGED", "VALIDATING", "BENCHMARKING", "APPROVED",
          "CANARY", "PRODUCTION", "STANDBY", "DEPRECATED", "QUARANTINED", "REJECTED", "FAILED"]

ALLOWED: dict[str, set[str]] = {
    "DISCOVERED": {"CANDIDATE", "REJECTED", "QUARANTINED"},
    "CANDIDATE": {"DOWNLOADING", "REJECTED", "QUARANTINED", "DISCOVERED"},
    "DOWNLOADING": {"STAGED", "FAILED", "QUARANTINED"},
    "STAGED": {"VALIDATING", "BENCHMARKING", "REJECTED", "DEPRECATED"},
    "VALIDATING": {"BENCHMARKING", "FAILED", "QUARANTINED"},
    "BENCHMARKING": {"APPROVED", "REJECTED", "FAILED", "STAGED"},
    "APPROVED": {"CANARY", "PRODUCTION", "STANDBY", "DEPRECATED"},
    "CANARY": {"PRODUCTION", "APPROVED", "REJECTED", "STANDBY"},
    "PRODUCTION": {"STANDBY", "DEPRECATED"},
    "STANDBY": {"PRODUCTION", "CANARY", "DEPRECATED"},
    "DEPRECATED": {"STANDBY", "STAGED"},
    "QUARANTINED": {"DISCOVERED", "REJECTED"},
    "REJECTED": {"DISCOVERED"},
    "FAILED": {"DISCOVERED", "REJECTED"},
}

SHA = re.compile(r"^[0-9a-f]{40}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
  id TEXT PRIMARY KEY,               -- profile name, e.g. qwen3-4b-instruct-2507-q4km-cpu
  model_id TEXT NOT NULL,            -- HF repo
  revision TEXT NOT NULL,            -- 40-char commit sha
  category TEXT NOT NULL,
  state TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',   -- normalized HF metadata
  profile TEXT NOT NULL DEFAULT '{}',-- deployment profile (runtime, file, sha256, context, endpoint…)
  fit TEXT NOT NULL DEFAULT '{}',
  screening TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT '',
  pinned INTEGER NOT NULL DEFAULT 0,
  blocked INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  UNIQUE(model_id, revision, category, id)
);
CREATE INDEX IF NOT EXISTS models_state ON models(state);
CREATE TABLE IF NOT EXISTS activity (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT 'controller', detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS alias_versions (
  alias TEXT NOT NULL, version INTEGER NOT NULL, chain TEXT NOT NULL,
  canary TEXT, ts REAL NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(alias, version)
);
CREATE TABLE IF NOT EXISTS benchmarks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, ts REAL NOT NULL,
  suite TEXT NOT NULL, results TEXT NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, finished REAL, status TEXT NOT NULL,
  funnel TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS availability (
  ts INTEGER NOT NULL, capability TEXT NOT NULL, ok INTEGER NOT NULL, latency_ms REAL,
  PRIMARY KEY(ts, capability)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class RegistryError(Exception):
    pass


class Registry:
    def __init__(self, path: str):
        self.db = db.DB(path, SCHEMA)

    # ── activity ─────────────────────────────────────────────────────────────

    def event(self, kind: str, subject: str = "", actor: str = "controller", **detail) -> None:
        self.db.x("INSERT INTO activity(ts,kind,subject,actor,detail) VALUES(?,?,?,?,?)",
                  (time.time(), kind, subject, actor, json.dumps(detail, default=str)))

    def activity(self, limit: int = 200, since: int = 0) -> list[dict]:
        rows = self.db.q("SELECT * FROM activity WHERE seq > ? ORDER BY seq DESC LIMIT ?", (since, limit))
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return rows

    # ── settings (operator controls) ─────────────────────────────────────────

    def setting(self, key: str, default: Any = None) -> Any:
        r = self.db.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value: Any, actor: str = "operator") -> None:
        self.db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))
        self.event("setting_changed", key, actor, value=value)

    # ── models ───────────────────────────────────────────────────────────────

    def upsert(self, mid: str, model_id: str, revision: str, category: str, state: str, *, meta: dict | None = None,
               profile: dict | None = None, fit: dict | None = None, screening: dict | None = None,
               reason: str = "", actor: str = "controller") -> dict:
        if not SHA.match(revision or ""):
            raise RegistryError(f"{model_id}: revision must be a pinned 40-char commit sha, got {revision!r}")
        if state not in STATES:
            raise RegistryError(f"unknown state {state}")
        now = time.time()
        cur = self.get(mid)
        with self.db.tx() as c:
            if cur is None:
                c.execute("INSERT INTO models(id,model_id,revision,category,state,meta,profile,fit,screening,reason,"
                          "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (mid, model_id, revision, category, state, json.dumps(meta or {}),
                           json.dumps(profile or {}), json.dumps(fit or {}), json.dumps(screening or {}), reason,
                           now, now))
            else:
                if cur["revision"] != revision and cur["state"] in ("PRODUCTION", "CANARY"):
                    raise RegistryError(f"{mid} is {cur['state']}; a new revision must be registered as a new "
                                        "profile, not overwrite a live one")
                c.execute("UPDATE models SET model_id=?,revision=?,category=?,meta=?,profile=?,fit=?,screening=?,"
                          "updated=? WHERE id=?",
                          (model_id, revision, category,
                           json.dumps(meta if meta is not None else cur["meta"]),
                           json.dumps(profile if profile is not None else cur["profile"]),
                           json.dumps(fit if fit is not None else cur["fit"]),
                           json.dumps(screening if screening is not None else cur["screening"]), now, mid))
        if cur is None:
            self.event("model_registered", mid, actor, model_id=model_id, revision=revision, state=state,
                       reason=reason)
        elif cur["state"] != state:
            self.transition(mid, state, reason or "upsert", actor=actor)
        return self.get(mid)

    def get(self, mid: str) -> dict | None:
        r = self.db.one("SELECT * FROM models WHERE id=?", (mid,))
        return _decode(r) if r else None

    def list(self, states: list[str] | None = None, category: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM models WHERE 1=1", []
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args += states
        if category:
            sql += " AND category=?"
            args.append(category)
        return [_decode(r) for r in self.db.q(sql + " ORDER BY updated DESC", tuple(args))]

    def transition(self, mid: str, to: str, reason: str, actor: str = "controller", force: bool = False) -> dict:
        cur = self.get(mid)
        if cur is None:
            raise RegistryError(f"unknown model {mid}")
        if cur["blocked"] and to in ("CANDIDATE", "DOWNLOADING", "CANARY", "PRODUCTION"):
            raise RegistryError(f"{mid} is blocked by the operator")
        if to != cur["state"] and to not in ALLOWED[cur["state"]] and not force:
            raise RegistryError(f"{mid}: {cur['state']} → {to} is not an allowed transition")
        if cur["state"] == "PRODUCTION" and to != "PRODUCTION" and self.aliases_using(mid) and not force:
            raise RegistryError(f"{mid} still serves {self.aliases_using(mid)}; repoint the alias first")
        self.db.x("UPDATE models SET state=?, reason=?, updated=? WHERE id=?", (to, reason, time.time(), mid))
        self.event("state_changed", mid, actor, frm=cur["state"], to=to, reason=reason)
        for s in STATES:
            metrics.model_state.labels(mid, s).set(1 if s == to else 0)
        return self.get(mid)

    def set_flag(self, mid: str, flag: str, value: bool, actor: str = "operator") -> None:
        if flag not in ("pinned", "blocked"):
            raise RegistryError("flag must be pinned|blocked")
        self.db.x(f"UPDATE models SET {flag}=?, updated=? WHERE id=?", (int(value), time.time(), mid))
        self.event(f"model_{flag}" if value else f"model_un{flag}", mid, actor)

    def delete(self, mid: str, actor: str = "operator") -> None:
        cur = self.get(mid)
        if cur is None:
            return
        if cur["state"] in ("PRODUCTION", "CANARY") or self.aliases_using(mid) or cur["pinned"]:
            raise RegistryError(f"{mid} is {cur['state']}/pinned or referenced by an alias; refusing to delete")
        if mid in self._rollback_critical():
            raise RegistryError(f"{mid} is a rollback target; refusing to delete")
        self.db.x("DELETE FROM models WHERE id=?", (mid,))
        self.event("model_deleted", mid, actor)

    # ── aliases (versioned) ─────────────────────────────────────────────────

    def alias(self, alias: str) -> dict | None:
        r = self.db.one("SELECT * FROM alias_versions WHERE alias=? ORDER BY version DESC LIMIT 1", (alias,))
        if not r:
            return None
        r["chain"] = json.loads(r["chain"])
        r["canary"] = json.loads(r["canary"]) if r["canary"] else None
        return r

    def aliases(self) -> dict[str, dict]:
        names = [r["alias"] for r in self.db.q("SELECT DISTINCT alias FROM alias_versions")]
        return {a: self.alias(a) for a in names}

    def set_alias(self, alias: str, chain: list[str], actor: str, note: str = "",
                  canary: dict | None = None) -> dict:
        for mid in chain + ([canary["profile"]] if canary else []):
            m = self.get(mid)
            if m is None:
                raise RegistryError(f"alias {alias}: unknown model {mid}")
            if m["blocked"]:
                raise RegistryError(f"alias {alias}: {mid} is blocked")
            if m["state"] not in ("PRODUCTION", "CANARY", "APPROVED", "STANDBY"):
                raise RegistryError(f"alias {alias}: {mid} is {m['state']}; only tested models may serve")
        cur = self.alias(alias)
        if cur and cur["chain"] == chain and cur["canary"] == canary:
            return cur
        v = (cur["version"] + 1) if cur else 1
        self.db.x("INSERT INTO alias_versions(alias,version,chain,canary,ts,actor,note) VALUES(?,?,?,?,?,?,?)",
                  (alias, v, json.dumps(chain), json.dumps(canary) if canary else None, time.time(), actor, note))
        self.event("alias_changed", alias, actor, version=v, chain=chain, canary=canary, note=note,
                   previous=cur["chain"] if cur else None)
        return self.alias(alias)

    def rollback_alias(self, alias: str, actor: str, to_version: int | None = None) -> dict:
        rows = self.db.q("SELECT * FROM alias_versions WHERE alias=? ORDER BY version DESC", (alias,))
        if len(rows) < 2 and to_version is None:
            raise RegistryError(f"{alias} has no previous version to roll back to")
        cur = rows[0]
        # Default target: the most recent earlier version that was a settled, known-good state —
        # never a canary version (transitional) and never identical to the current chain.
        target = next((r for r in rows if r["version"] == to_version), None) if to_version else next(
            (r for r in rows[1:] if not r["canary"] and (r["chain"] != cur["chain"] or cur["canary"])), None)
        if target is None:
            raise RegistryError(f"{alias}: rollback target not found")
        chain = json.loads(target["chain"])
        for mid in chain:
            m = self.get(mid)
            if m is None or m["blocked"]:
                raise RegistryError(f"rollback target {mid} is missing or blocked")
            if m["state"] not in ("PRODUCTION", "STANDBY", "APPROVED", "CANARY"):
                self.transition(mid, "STANDBY" if m["state"] == "DEPRECATED" else m["state"],
                                "restored by rollback", actor)
        out = self.set_alias(alias, chain, actor, note=f"rollback to v{target['version']}",
                             canary=json.loads(target["canary"]) if target["canary"] else None)
        self.event("alias_rollback", alias, actor, to_version=target["version"], chain=chain)
        return out

    def aliases_using(self, mid: str) -> list[str]:
        return [a for a, v in self.aliases().items()
                if v and (mid in v["chain"] or (v["canary"] or {}).get("profile") == mid)]

    def _rollback_critical(self) -> set[str]:
        keep = int(config.get("models.rollback_versions", 2))
        out: set[str] = set()
        for a in self.aliases():
            rows = self.db.q("SELECT chain FROM alias_versions WHERE alias=? ORDER BY version DESC LIMIT ?",
                             (a, keep + 1))
            for r in rows:
                out.update(json.loads(r["chain"]))
        return out

    # ── benchmarks / discovery / availability ───────────────────────────────

    def add_benchmark(self, mid: str, suite: str, results: dict, summary: dict) -> int:
        self.db.x("INSERT INTO benchmarks(model,ts,suite,results,summary) VALUES(?,?,?,?,?)",
                  (mid, time.time(), suite, json.dumps(results), json.dumps(summary)))
        self.event("benchmark_completed", mid, suite=suite, **{k: summary.get(k) for k in
                                                                 ("quality", "json_valid", "ttft_ms_p50",
                                                                  "decode_tps_p50", "errors")})
        return self.db.one("SELECT max(id) AS i FROM benchmarks")["i"]

    def benchmarks(self, mid: str | None = None, limit: int = 50) -> list[dict]:
        if mid:
            rows = self.db.q("SELECT * FROM benchmarks WHERE model=? ORDER BY id DESC LIMIT ?", (mid, limit))
        else:
            rows = self.db.q("SELECT * FROM benchmarks ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["results"], r["summary"] = json.loads(r["results"]), json.loads(r["summary"])
        return rows

    def latest_benchmark(self, mid: str, suite: str | None = None) -> dict | None:
        for b in self.benchmarks(mid, 20):
            if suite is None or b["suite"] == suite:
                return b
        return None

    def start_discovery(self) -> int:
        self.db.x("INSERT INTO discovery_runs(ts,status) VALUES(?, 'running')", (time.time(),))
        return self.db.one("SELECT max(id) AS i FROM discovery_runs")["i"]

    def finish_discovery(self, run: int, status: str, funnel: dict, error: str = "") -> None:
        self.db.x("UPDATE discovery_runs SET finished=?, status=?, funnel=?, error=? WHERE id=?",
                  (time.time(), status, json.dumps(funnel), error[:2000], run))

    def discovery_runs(self, limit: int = 20) -> list[dict]:
        rows = self.db.q("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["funnel"] = json.loads(r["funnel"])
        return rows

    def record_availability(self, capability: str, ok: bool, latency_ms: float | None, ts: int | None = None):
        self.db.x("INSERT OR REPLACE INTO availability(ts,capability,ok,latency_ms) VALUES(?,?,?,?)",
                  (ts or int(time.time()), capability, int(ok), latency_ms))

    def availability(self, since_sec: int = 86400) -> dict[str, dict]:
        rows = self.db.q("SELECT capability, count(*) AS n, sum(ok) AS ok, avg(latency_ms) AS lat "
                         "FROM availability WHERE ts >= ? GROUP BY capability", (int(time.time()) - since_sec,))
        return {r["capability"]: {"samples": r["n"], "availability": (r["ok"] / r["n"]) if r["n"] else None,
                                  "mean_latency_ms": r["lat"]} for r in rows}

    def prune_availability(self, keep_days: int = 45) -> None:
        self.db.x("DELETE FROM availability WHERE ts < ?", (int(time.time()) - keep_days * 86400,))


def _decode(r: dict) -> dict:
    for k in ("meta", "profile", "fit", "screening"):
        r[k] = json.loads(r[k]) if isinstance(r[k], str) else r[k]
    r["pinned"], r["blocked"] = bool(r["pinned"]), bool(r["blocked"])
    return r
