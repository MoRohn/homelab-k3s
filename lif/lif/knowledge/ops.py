"""One facade over the workspace for every interface (CLI, MCP, HTTP API, other LIF services).

    kn = Knowledge.open()            # opens the workspace and brings the index up to date (incremental)
    kn.graph.why("use-k3s")
    kn.context.assemble("change the gateway rate limit")
    kn.writer("agent:claude-code").create("decision", {...})

Permissions are enforced here (scopes per actor from workspace `permissions:`), and every call made
through `call()` is written to the audit table.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from lif.knowledge.compiler import Compiler
from lif.knowledge.graph import Graph
from lif.knowledge.repo import Workspace
from lif.knowledge.search import Embedder, Search
from lif.knowledge.store import Store
from lif.knowledge.writer import Writer

SCOPES = ["read:knowledge", "write:knowledge", "execute:skills", "execute:tools", "modify:types",
          "install:packages", "deploy:apps", "admin"]
# Operations a non-human actor can never complete on its own: they return "approval required".
APPROVAL_GATED = {"migrate.apply", "package.update", "package.install", "delete", "publish"}


class PermissionDenied(Exception):
    pass


class Knowledge:
    def __init__(self, ws: Workspace, store: Store, embedder: Embedder | None = None):
        self.ws, self.store = ws, store
        self.graph = Graph(store)
        self.search = Search(store, embedder)
        self._checked = 0.0
        self.refresh_interval = 1.0          # seconds between stat walks for non-forced refreshes

    @classmethod
    def open(cls, root: str | Path | None = None, index: str | Path | None = None, compile: bool = True,
             embedder: Embedder | None = None, registry: str | Path | None = None) -> "Knowledge":
        ws = Workspace.open(root, registry=registry)
        store = Store(index if index is not None else (os.environ.get("LIF_KNOWLEDGE_INDEX") or ws.index_path))
        kn = cls(ws, store, embedder)
        if compile:
            kn.refresh(force=True)
        return kn

    def refresh(self, force: bool = False) -> dict | None:
        """Incremental compile when any repo file changed since the last compile (cheap stat walk)."""
        if not force and time.time() - self._checked < self.refresh_interval:
            return None
        self._checked = time.time()
        last = self.store.meta("compiled_at", 0) or 0
        if not force and not self._dirty(last):
            return None
        self.ws = Workspace.open(self.ws.root, registry=self.ws.registry.root)   # manifests / locks may change
        return Compiler(self.ws, self.store).compile()

    def _dirty(self, since: float) -> bool:
        for r in self.ws.all_repos():
            for p in r.path.rglob("*"):
                if p.is_file() and p.stat().st_mtime > since and ".knowledge" not in p.parts:
                    return True
        return False

    def rebuild(self) -> dict:
        self.ws = Workspace.open(self.ws.root, registry=self.ws.registry.root)
        return Compiler(self.ws, self.store).compile(full=True)

    # ── permissions ──────────────────────────────────────────────────────────
    def scopes(self, actor: str) -> set[str]:
        perms = self.ws.config.get("permissions") or {}
        if actor in ("human", "operator"):
            return set(SCOPES)
        name = actor.split(":", 1)[-1]
        raw = perms.get(actor) or perms.get(name) or perms.get("default") or ["read:knowledge"]
        out = set(raw if isinstance(raw, list) else raw.get("scopes", []))
        return set(SCOPES) if "admin" in out else out

    def repos_for(self, actor: str, write: bool = False) -> set[str] | None:
        """Repos an actor may read (`repos`) or write (`write_repos`, else `repos`). None = all.
        Humans are unrestricted."""
        if actor in ("human", "operator"):
            return None
        perms = self.ws.config.get("permissions") or {}
        raw = perms.get(actor) or perms.get(actor.split(":", 1)[-1]) or perms.get("default")
        if not isinstance(raw, dict):
            return None
        allowed = (raw.get("write_repos") or raw.get("repos")) if write else raw.get("repos")
        return set(allowed) if allowed else None

    def require(self, actor: str, scope: str, action: str, target: str = "") -> None:
        ok = scope in self.scopes(actor)
        self.store.audit(actor, action, target, ok, scope=scope)
        if not ok:
            raise PermissionDenied(f"{actor} lacks scope {scope} for {action}")

    def writer(self, actor: str = "agent", session: str | None = None) -> Writer:
        return Writer(self.ws, self.store, actor=actor, session=session, allowed=self.repos_for(actor, write=True))

    @property
    def context(self):
        from lif.knowledge.context import ContextEngine
        return ContextEngine(self)

    # ── saved queries and suggestions (repeated operations → tools/views) ────
    def saved_queries(self) -> list[dict]:
        import yaml
        out = []
        for r in self.ws.all_repos():
            for p in r.glob("queries", "*.query.yaml"):
                raw = yaml.safe_load(p.read_text()) or {}
                out.append({"name": raw.get("name", p.name.split(".")[0]), "repo": r.name,
                            "description": raw.get("description", ""), "params": raw.get("params") or [],
                            "find": raw.get("find") or {}, "path": str(p)})
        return out

    def run_query(self, name: str, params: dict | None = None) -> list[dict]:
        q = next((q for q in self.saved_queries() if q["name"] == name), None)
        if q is None:
            raise KeyError(f"no saved query '{name}'")
        args = {}
        for k, v in q["find"].items():
            if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                v = (params or {}).get(v[1:-1])
                if v is None:
                    raise ValueError(f"query {name} needs parameter {k}")
            args[k] = v
        self.store.log_query(f"saved:{name}")
        return self.graph.find(**args)

    def suggest(self, min_uses: int = 3, days: int = 30) -> list[dict]:
        """Operations repeated often enough to deserve a saved query/tool or a view. Proposals only."""
        rows = self.store.db.q("SELECT sig, count(*) n FROM query_log WHERE ts > ? AND sig NOT LIKE 'saved:%' "
                               "GROUP BY sig HAVING n >= ? ORDER BY n DESC", (time.time() - days * 86400, min_uses))
        have = {q["name"] for q in self.saved_queries()}
        out = []
        for r in rows:
            name = "q-" + "-".join(r["sig"].split(":", 1)[-1].replace("=", "-").split())[:50]
            if name in have:
                continue
            out.append({"operation": r["sig"], "uses": r["n"], "proposal": "save as a query (becomes an MCP tool)",
                        "file": f"queries/{name}.query.yaml"})
        return out
