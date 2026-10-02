"""Explanation and artifact cache, sessions and feedback (spec §49, §69–§72, §104).

ExplanationSpecs are stored separately from rendered artifacts, so the same IR can be rendered again
for a new audience, depth or renderer without repeating research. Artifacts are keyed by
(semantic hash, renderer, renderer version, theme, audience, depth, viewport, options).

Default path: $LIF_UNDERSTANDING_DB, else ~/.local/share/lif/understanding.db. Specs built from live
state are CONFIDENTIAL and stay in this local file.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from lif.common.db import DB
from lif.understanding.render.base import RenderRequest, RenderResult, Segment
from lif.understanding.spec import ExplanationSpec, load

SCHEMA = """
CREATE TABLE IF NOT EXISTS specs (
  id TEXT PRIMARY KEY, semantic_hash TEXT NOT NULL, version INTEGER NOT NULL, parent TEXT NOT NULL DEFAULT '',
  question TEXT NOT NULL, data_class TEXT NOT NULL, builder TEXT NOT NULL, created_at REAL NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS specs_parent ON specs(parent);
CREATE TABLE IF NOT EXISTS artifacts (
  key TEXT PRIMARY KEY, spec_id TEXT NOT NULL, renderer TEXT NOT NULL, renderer_version TEXT NOT NULL,
  created_at REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_spec ON artifacts(spec_id);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, spec_id TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  request TEXT NOT NULL, presentation TEXT NOT NULL, plan TEXT NOT NULL, outputs TEXT NOT NULL,
  evaluation TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_session ON feedback(session_id);
"""


def default_path() -> str:
    if os.environ.get("LIF_UNDERSTANDING_DB"):
        return os.environ["LIF_UNDERSTANDING_DB"]
    if os.environ.get("LIF_CONSOLE_DB"):              # inside the console: its writable data volume
        return str(Path(os.environ["LIF_CONSOLE_DB"]).parent / "understanding.db")
    return str(Path.home() / ".local/share/lif/understanding.db")


def artifact_key(spec: ExplanationSpec, renderer: str, version: str, req: RenderRequest) -> str:
    who = (req.audience or spec.audience).model_dump_json()
    blob = json.dumps([spec.semantic_hash(), renderer, version, req.theme, who, req.depth, req.viewport,
                       sorted(req.selected), req.options], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def result_to_json(r: RenderResult) -> dict:
    d = r.to_dict()
    d["segments"] = [s.__dict__ for s in r.segments]
    d["edges"] = [list(e) for e in r.edges]
    d["extra"] = r.extra
    return d


def result_from_json(d: dict) -> RenderResult:
    r = RenderResult(d["renderer"], d["version"], d["target"], d.get("artifact", ""), d.get("media_type", ""),
                     [Segment(**s) for s in d.get("segments", [])], d.get("emphasis", []),
                     [tuple(e) for e in d.get("edges", [])], d.get("warnings", []), d.get("verification", {}),
                     d.get("render_metrics", {}), d.get("status", "ok"), d.get("detail", ""), d.get("extra", {}))
    return r


class Store:
    def __init__(self, path: str | None = None):
        self.db = DB(path or default_path(), SCHEMA)

    # specs
    def put_spec(self, spec: ExplanationSpec) -> None:
        self.db.x("INSERT OR REPLACE INTO specs VALUES (?,?,?,?,?,?,?,?,?)",
                  (spec.id, spec.semantic_hash(), spec.version, spec.metadata.parent, spec.question,
                   spec.metadata.data_class, spec.metadata.builder, spec.metadata.created_at,
                   json.dumps(spec.to_json())))

    def get_spec(self, spec_id: str) -> ExplanationSpec | None:
        row = self.db.one("SELECT doc FROM specs WHERE id=?", (spec_id,))
        return load(json.loads(row["doc"])) if row else None

    def lineage(self, spec_id: str) -> list[ExplanationSpec]:
        """The spec and its ancestors (via metadata.parent), oldest first."""
        out, cur = [], self.get_spec(spec_id)
        while cur is not None and len(out) < 50:
            out.append(cur)
            cur = self.get_spec(cur.metadata.parent) if cur.metadata.parent else None
        return list(reversed(out))

    # artifacts
    def get_artifact(self, key: str) -> RenderResult | None:
        row = self.db.one("SELECT result FROM artifacts WHERE key=?", (key,))
        if row is None:
            return None
        self.db.x("UPDATE artifacts SET hits=hits+1 WHERE key=?", (key,))
        return result_from_json(json.loads(row["result"]))

    def put_artifact(self, key: str, spec_id: str, r: RenderResult) -> None:
        self.db.x("INSERT OR REPLACE INTO artifacts (key,spec_id,renderer,renderer_version,created_at,result) "
                  "VALUES (?,?,?,?,?,?)", (key, spec_id, r.renderer, r.version, time.time(),
                                           json.dumps(result_to_json(r))))

    # sessions
    def put_session(self, sid: str, spec_id: str, request: dict, presentation: dict, plan: dict, outputs: dict,
                    evaluation: dict | None = None) -> None:
        now = time.time()
        prev = self.db.one("SELECT created_at FROM sessions WHERE id=?", (sid,))
        self.db.x("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                  (sid, spec_id, prev["created_at"] if prev else now, now, json.dumps(request),
                   json.dumps(presentation), json.dumps(plan, default=str), json.dumps(outputs),
                   json.dumps(evaluation or {})))

    def get_session(self, sid: str) -> dict | None:
        row = self.db.one("SELECT * FROM sessions WHERE id=?", (sid,))
        if row is None:
            return None
        for k in ("request", "presentation", "plan", "outputs", "evaluation"):
            row[k] = json.loads(row[k])
        return row

    def latest_session(self, spec_id: str) -> str | None:
        """The newest session showing this spec or a spec derived from it (deepen moves a session to the child)."""
        row = self.db.one("WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT s.id FROM specs s JOIN d ON s.parent = d.id) "
                          "SELECT id FROM sessions WHERE spec_id IN (SELECT id FROM d) "
                          "ORDER BY updated_at DESC LIMIT 1", (spec_id,))
        return row["id"] if row else None

    def session_belongs(self, sid: str, spec_id: str) -> bool:
        s = self.get_session(sid)
        if s is None:
            return False
        line = {x.id for x in self.lineage(s["spec_id"])}
        return spec_id in line

    # feedback (§104: override rate, abandonment, comprehension)
    def feedback(self, sid: str, kind: str, detail: dict[str, Any]) -> None:
        self.db.x("INSERT INTO feedback (session_id, ts, kind, detail) VALUES (?,?,?,?)",
                  (sid, time.time(), kind, json.dumps(detail)))

    def feedback_for(self, sid: str) -> list[dict]:
        return [{**r, "detail": json.loads(r["detail"])} for r in
                self.db.q("SELECT ts, kind, detail FROM feedback WHERE session_id=? ORDER BY ts", (sid,))]

    def router_stats(self) -> dict:
        """Override rate per primary renderer: how often people asked for something else (§104)."""
        rows = self.db.q("SELECT s.plan, (SELECT COUNT(*) FROM feedback f WHERE f.session_id=s.id AND "
                         "f.kind='override') AS overrides FROM sessions s")
        stats: dict[str, dict] = {}
        for r in rows:
            primary = json.loads(r["plan"]).get("primary", "?")
            st = stats.setdefault(primary, {"sessions": 0, "overridden": 0})
            st["sessions"] += 1
            st["overridden"] += 1 if r["overrides"] else 0
        for st in stats.values():
            st["override_rate"] = round(st["overridden"] / st["sessions"], 3) if st["sessions"] else None
        return stats
