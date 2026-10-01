"""Decision-engineering store: one SQLite file shared by the registry, shadow runner,
human-review queue, trace store and experiment log (decision-fabric service, one writer).

Path: $LIF_DE_DB (default /data/decision-eng.db, next to decisions.db on the same PVC).
Rows hold redacted state digests and typed answers, never raw prompts or secrets.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from lif.common import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, version TEXT NOT NULL, stage TEXT NOT NULL,
  rollout_pct REAL NOT NULL DEFAULT 100, pins TEXT NOT NULL, thresholds TEXT NOT NULL, policy TEXT NOT NULL,
  actor TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL, ts REAL NOT NULL, active INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS releases_name ON releases(name, active);

CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, decision_ref TEXT NOT NULL, state_hash TEXT NOT NULL,
  baseline_answer TEXT, baseline_executor TEXT, baseline_latency_ms REAL, baseline_cost_usd REAL,
  baseline_tokens INTEGER, candidate_answer TEXT NOT NULL, candidate_confidence REAL NOT NULL,
  candidate_probs TEXT NOT NULL, candidate_provider TEXT NOT NULL, candidate_latency_ms REAL,
  candidate_cost_usd REAL, outcome TEXT, outcome_source TEXT, outcome_ts REAL, slice TEXT
);
CREATE INDEX IF NOT EXISTS shadow_ref ON shadow(decision_ref, ts);
CREATE INDEX IF NOT EXISTS shadow_hash ON shadow(state_hash);

CREATE TABLE IF NOT EXISTS provenance (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, decision_ref TEXT NOT NULL, agent TEXT, workflow TEXT,
  state_hash TEXT NOT NULL, answer TEXT NOT NULL, confidence REAL NOT NULL, route TEXT NOT NULL,
  executor TEXT NOT NULL, record TEXT NOT NULL, outcome TEXT, outcome_ts REAL
);
CREATE INDEX IF NOT EXISTS provenance_ref ON provenance(decision_ref, ts);

CREATE TABLE IF NOT EXISTS human_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, decision_ref TEXT NOT NULL, provenance_id TEXT,
  package TEXT NOT NULL, status TEXT NOT NULL, answer TEXT, reviewer TEXT, resolved_ts REAL
);
CREATE INDEX IF NOT EXISTS human_status ON human_queue(status, ts);

CREATE TABLE IF NOT EXISTS operations (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL, agent TEXT NOT NULL,
  workflow TEXT, source TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, signature TEXT NOT NULL,
  classification TEXT NOT NULL, class_confidence REAL NOT NULL, reasons TEXT NOT NULL,
  executor TEXT, input_tokens INTEGER, output_tokens INTEGER, latency_ms REAL, cost_usd REAL,
  output_label TEXT, reversible INTEGER, bounded INTEGER, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS operations_sig ON operations(signature);
CREATE INDEX IF NOT EXISTS operations_agent ON operations(agent, ts);

CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, name TEXT NOT NULL, baseline TEXT NOT NULL,
  candidate TEXT NOT NULL, verdict TEXT NOT NULL, report TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | None = None):
        self.db = db.DB(path or os.environ.get("LIF_DE_DB", "/data/decision-eng.db"), SCHEMA)

    # thin helpers so callers never hand-roll JSON columns
    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = list(row)
        vals = [json.dumps(v, default=str, sort_keys=True) if isinstance(v, (dict, list)) else v for v in row.values()]
        with self.db.tx() as c:
            cur = c.execute(f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                            vals)
            return int(cur.lastrowid or 0)

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        return self.db.q(sql, args)

    def x(self, sql: str, args: tuple = ()) -> int:
        return self.db.x(sql, args)


def now() -> float:
    return time.time()
