"""Knowledge index: SQLite (WAL) + FTS5 + recursive CTEs. Derived data only.

Everything here can be rebuilt from the canonical agent repos (`knowledge rebuild`), except
three runtime tables that are deliberately not knowledge: `query_log` (usage counts that drive
tool/view suggestions), `audit` (who called what) and `embeddings` (a cache). Losing them loses
no knowledge.

Edge convention: (src, rel, dst) means **src depends on dst** — a decision depends on its
evidence and assumptions. Fields marked `inverse` (e.g. an assumption's `used_by`) are stored
flipped so that "what depends on X" is always the reverse walk.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from lif.common.db import DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, repo TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL,
  sha TEXT NOT NULL, parsed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS objects (
  key TEXT PRIMARY KEY, repo TEXT NOT NULL, id TEXT NOT NULL, sub TEXT, parent TEXT,
  type TEXT NOT NULL, type_name TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL, line INTEGER NOT NULL DEFAULT 1, body TEXT NOT NULL DEFAULT '',
  fields TEXT NOT NULL DEFAULT '{}', sha TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '', repo_version TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'workspace', mtime REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS objects_type ON objects(type_name);
CREATE INDEX IF NOT EXISTS objects_repo ON objects(repo);
CREATE TABLE IF NOT EXISTS edges (
  src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT, raw TEXT NOT NULL, field TEXT NOT NULL,
  kind TEXT NOT NULL, propagates INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS diagnostics (
  code TEXT NOT NULL, severity TEXT NOT NULL, key TEXT, path TEXT NOT NULL DEFAULT '', line INTEGER,
  message TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS reconsiderations (
  key TEXT NOT NULL, trigger TEXT NOT NULL, reason TEXT NOT NULL, chain TEXT NOT NULL DEFAULT '[]',
  since TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL DEFAULT 'normal', resolved_by TEXT,
  PRIMARY KEY(key, trigger)
);
CREATE TABLE IF NOT EXISTS types (
  qname TEXT PRIMARY KEY, namespace TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL,
  extends TEXT, definition TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
  name TEXT PRIMARY KEY, path TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL,
  source TEXT NOT NULL, manifest TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(key UNINDEXED, title, body, fields, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS query_log (sig TEXT NOT NULL, ts REAL NOT NULL, actor TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS audit (ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '', allowed INTEGER NOT NULL, detail TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, sha TEXT NOT NULL, vec TEXT NOT NULL);
"""

DERIVED = ("objects", "edges", "diagnostics", "reconsiderations", "types", "repos", "fts")


SCHEMA_VERSION = 2


class Store:
    def __init__(self, path: str | Path | None):
        self.path = str(path) if path else ":memory:"
        self.db = DB(self.path, SCHEMA)
        if self.meta("schema_version") != SCHEMA_VERSION:
            # derived data only: an index from an older layout is dropped and rebuilt, never migrated
            with self.db.tx() as c:
                for t in DERIVED + ("files", "embeddings", "meta"):
                    c.execute(f"DROP TABLE IF EXISTS {t}")
            self.db._conn.executescript(SCHEMA)
            self.set_meta("schema_version", SCHEMA_VERSION)

    # ── meta ─────────────────────────────────────────────────────────────────
    def meta(self, k: str, default: Any = None) -> Any:
        r = self.db.one("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def set_meta(self, k: str, v: Any) -> None:
        self.db.x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def clear(self, include_cache: bool = False) -> None:
        with self.db.tx() as c:
            for t in DERIVED + (("files", "embeddings") if include_cache else ()):
                c.execute(f"DELETE FROM {t}")

    # ── reads ────────────────────────────────────────────────────────────────
    @staticmethod
    def _obj(r: dict | None) -> dict | None:
        if r is None:
            return None
        r = dict(r)
        r["fields"] = json.loads(r["fields"])
        return r

    def get(self, key: str) -> dict | None:
        return self._obj(self.db.one("SELECT * FROM objects WHERE key=?", (key,)))

    def objects(self, where: str = "1=1", args: tuple = (), order: str = "updated DESC, key",
                limit: int = 10000) -> list[dict]:
        return [self._obj(r) for r in self.db.q(f"SELECT * FROM objects WHERE {where} ORDER BY {order} LIMIT ?",
                                                (*args, limit))]

    def find_key(self, ref: str, repo: str | None = None) -> str | None:
        """Accept a full key (repo::id), or a bare id (preferring `repo`, else unique match)."""
        if self.get(ref):
            return ref
        if repo and self.get(f"{repo}::{ref}"):
            return f"{repo}::{ref}"
        rows = self.db.q("SELECT key FROM objects WHERE (CASE WHEN sub IS NULL THEN id ELSE id || '^' || sub END)=?",
                         (ref,))
        return rows[0]["key"] if len(rows) == 1 else None

    def edges_from(self, key: str) -> list[dict]:
        return self.db.q("SELECT * FROM edges WHERE src=? ORDER BY field, rel", (key,))

    def edges_to(self, key: str) -> list[dict]:
        return self.db.q("SELECT * FROM edges WHERE dst=? ORDER BY field, rel", (key,))

    def walk(self, key: str, direction: str = "dependents", depth: int = 6, propagating: bool = True,
             rels: Iterable[str] | None = None) -> list[dict]:
        """Transitive closure with the shortest path to each node.
        dependents  : who depends on key (reverse edges)       — change impact, blast radius
        dependencies: what key depends on (forward edges)      — lineage, why"""
        a, b = ("dst", "src") if direction == "dependents" else ("src", "dst")
        cond = " AND e.propagates=1" if propagating else ""
        rel_list = list(rels or [])
        if rel_list:
            cond += " AND e.rel IN (%s)" % ",".join("?" * len(rel_list))
        sql = f"""
        WITH RECURSIVE walk(node, depth, path) AS (
          SELECT ?, 0, json_array(?)
          UNION
          SELECT e.{b}, w.depth + 1, json_insert(json_insert(w.path, '$[#]', e.rel), '$[#]', e.{b})
          FROM walk w JOIN edges e ON e.{a} = w.node
          WHERE w.depth < ? AND e.{b} IS NOT NULL AND e.kind != 'contains'{cond}
            AND instr(w.path, json_quote(e.{b})) = 0
        )
        SELECT node, min(depth) AS depth, path FROM walk WHERE node != ? GROUP BY node ORDER BY depth, node"""
        rows = self.db.q(sql, (key, key, depth, *rel_list, key))
        for r in rows:
            r["path"] = json.loads(r["path"])
        return rows

    def diagnostics(self, severity: str | None = None, key: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM diagnostics WHERE 1=1", []
        if severity:
            sql += " AND severity=?"
            args.append(severity)
        if key:
            sql += " AND (key=? OR key LIKE ?)"
            args += [key, key + "^%"]
        rows = self.db.q(sql + " ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, path",
                         tuple(args))
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return rows

    def reconsiderations(self, open_only: bool = True) -> list[dict]:
        rows = self.db.q("SELECT * FROM reconsiderations" + (" WHERE resolved_by IS NULL" if open_only else "") +
                         " ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2"
                         " ELSE 3 END, since DESC, key")
        for r in rows:
            r["chain"] = json.loads(r["chain"])
        return rows

    def fts(self, query: str, limit: int = 50) -> list[dict]:
        return self.db.q("SELECT key, bm25(fts, 0.0, 4.0, 1.0, 2.0) AS rank, "
                         "snippet(fts, 2, '[', ']', '…', 12) AS snippet FROM fts WHERE fts MATCH ? "
                         "ORDER BY rank LIMIT ?", (query, limit))

    def counts(self) -> dict:
        one = lambda sql, a=(): self.db.one(sql, a)["n"]
        return {"objects": one("SELECT count(*) n FROM objects WHERE sub IS NULL"),
                "embedded": one("SELECT count(*) n FROM objects WHERE sub IS NOT NULL"),
                "edges": one("SELECT count(*) n FROM edges WHERE kind != 'contains'"),
                "references": one("SELECT count(*) n FROM edges WHERE kind IN ('field','body','inverse')"),
                "broken_references": one("SELECT count(*) n FROM edges WHERE dst IS NULL"),
                "errors": one("SELECT count(*) n FROM diagnostics WHERE severity='error'"),
                "warnings": one("SELECT count(*) n FROM diagnostics WHERE severity='warning'"),
                "reconsiderations": one("SELECT count(*) n FROM reconsiderations WHERE resolved_by IS NULL")}

    # ── writes (compiler only) ───────────────────────────────────────────────
    def cached_file(self, path: str) -> dict | None:
        return self.db.one("SELECT * FROM files WHERE path=?", (path,))

    def put_file(self, path: str, repo: str, mtime: float, size: int, sha: str, parsed: list[dict]) -> None:
        self.db.x("INSERT INTO files(path,repo,mtime,size,sha,parsed) VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE "
                  "SET repo=excluded.repo, mtime=excluded.mtime, size=excluded.size, sha=excluded.sha, "
                  "parsed=excluded.parsed", (path, repo, mtime, size, sha, json.dumps(parsed, default=str)))

    def drop_files_except(self, keep: set[str]) -> None:
        for r in self.db.q("SELECT path FROM files"):
            if r["path"] not in keep:
                self.db.x("DELETE FROM files WHERE path=?", (r["path"],))

    def replace_graph(self, objects: list[dict], edges: list[dict], diags: list[dict], recons: list[dict],
                      types: list[dict], repos: list[dict]) -> dict:
        """Write a compiled graph. Objects whose content hash is unchanged keep their rows (and FTS)."""
        old = {r["key"]: r["sha"] + r["fields"] + r["updated"] + str(r["mtime"]) for r in
               self.db.q("SELECT key, sha, fields, updated, mtime FROM objects")}
        new_keys = {o["key"] for o in objects}
        changed = [o for o in objects if old.get(o["key"]) != o["sha"] + json.dumps(
            o["fields"], default=str, sort_keys=True) + o["updated"] + str(o.get("mtime", 0))]
        removed = [k for k in old if k not in new_keys]
        with self.db.tx() as c:
            for k in removed + [o["key"] for o in changed]:
                c.execute("DELETE FROM objects WHERE key=?", (k,))
                c.execute("DELETE FROM fts WHERE key=?", (k,))
            for o in changed:
                fj = json.dumps(o["fields"], default=str, sort_keys=True)
                c.execute("INSERT INTO objects(key,repo,id,sub,parent,type,type_name,title,status,path,line,body,fields,"
                          "sha,updated,updated_by,project,repo_version,source,mtime) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (o["key"], o["repo"], o["id"], o["sub"], o["parent"], o["type"], o["type_name"], o["title"],
                           o["status"], o["path"], o["line"], o["body"], fj, o["sha"], o["updated"], o["updated_by"],
                           o["project"], o["repo_version"], o["source"], o.get("mtime", 0)))
                c.execute("INSERT INTO fts(key,title,body,fields) VALUES(?,?,?,?)",
                          (o["key"], o["title"] + " " + o["id"].replace("-", " "), o["body"],
                           " ".join(str(v) for v in o["fields"].values())))
            for t in ("edges", "diagnostics", "reconsiderations", "types", "repos"):
                c.execute(f"DELETE FROM {t}")
            c.executemany("INSERT INTO edges(src,rel,dst,raw,field,kind,propagates) VALUES(?,?,?,?,?,?,?)",
                          [(e["src"], e["rel"], e["dst"], e["raw"], e["field"], e["kind"], int(e["propagates"]))
                           for e in edges])
            c.executemany("INSERT INTO diagnostics(code,severity,key,path,line,message,detail) VALUES(?,?,?,?,?,?,?)",
                          [(d["code"], d["severity"], d.get("key"), d.get("path", ""), d.get("line"), d["message"],
                            json.dumps(d.get("detail") or {}, default=str)) for d in diags])
            c.executemany("INSERT OR REPLACE INTO reconsiderations(key,trigger,reason,chain,since,priority,resolved_by) "
                          "VALUES(?,?,?,?,?,?,?)",
                          [(r["key"], r["trigger"], r["reason"], json.dumps(r["chain"]), r["since"], r["priority"],
                            r.get("resolved_by")) for r in recons])
            c.executemany("INSERT INTO types(qname,namespace,name,version,extends,definition) VALUES(?,?,?,?,?,?)",
                          [(t["qname"], t["namespace"], t["name"], t["version"], t["extends"],
                            json.dumps(t["definition"])) for t in types])
            c.executemany("INSERT INTO repos(name,path,version,kind,source,manifest) VALUES(?,?,?,?,?,?)",
                          [(r["name"], r["path"], r["version"], r["kind"], r["source"], json.dumps(r["manifest"]))
                           for r in repos])
        return {"changed": len(changed), "removed": len(removed), "unchanged": len(objects) - len(changed)}

    # ── runtime logs ─────────────────────────────────────────────────────────
    def log_query(self, sig: str, actor: str = "") -> None:
        self.db.x("INSERT INTO query_log(sig,ts,actor) VALUES(?,?,?)", (sig, time.time(), actor))

    def audit(self, actor: str, action: str, target: str, allowed: bool, **detail) -> None:
        self.db.x("INSERT INTO audit(ts,actor,action,target,allowed,detail) VALUES(?,?,?,?,?,?)",
                  (time.time(), actor, action, target, int(allowed), json.dumps(detail, default=str)))
