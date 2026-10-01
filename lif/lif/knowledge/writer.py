"""Writes to canonical knowledge: typed Markdown files in workspace repos.

Every write is validated by compiling it. A write that would introduce an error diagnostic on the
written object is rolled back (the file is restored) unless `force=True`, so agents cannot
silently corrupt the graph. Registry packages are read-only: change a package by publishing a new
version.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import yaml

from lif.knowledge import parse as P
from lif.knowledge.compiler import Compiler
from lif.knowledge.repo import Repo, Workspace
from lif.knowledge.store import Store

SLUG = re.compile(r"[^a-z0-9]+")


class WriteError(Exception):
    def __init__(self, message: str, diagnostics: list[dict] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or []


def slug(text: str, max_len: int = 60) -> str:
    s = SLUG.sub("-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "item"


def plural(t: str) -> str:
    return t + ("es" if t.endswith(("s", "x", "ch")) else "s")


def as_link(v: Any) -> Any:
    if isinstance(v, list):
        return [as_link(x) for x in v]
    if isinstance(v, str) and not v.startswith("[["):
        return f"[[{v}]]"
    return v


class Writer:
    def __init__(self, ws: Workspace, store: Store, actor: str = "agent", session: str | None = None,
                 allowed: set[str] | None = None):
        self.ws, self.store, self.actor, self.session = ws, store, actor, session
        self.allowed = allowed                     # repos this actor may write (None = all)

    def _repo(self, name: str | None) -> Repo:
        if name is None and self.allowed:
            name = next((r.name for r in self.ws.repos if r.name in self.allowed), None)
        repo = self.ws.repo(name) if name else (self.ws.repos[0] if self.ws.repos else None)
        if repo is None:
            raise WriteError(f"unknown repo '{name}'")
        if self.allowed is not None and repo.name not in self.allowed:
            raise WriteError(f"{self.actor} may not write to repo {repo.name} (write_repos: {sorted(self.allowed)})")
        if not repo.writable:
            raise WriteError(f"{repo.name} is a published package (read-only); publish a new version instead")
        return repo

    def _path_of(self, key: str) -> tuple[Repo, Path, dict]:
        o = self.store.get(key)
        if o is None:
            raise WriteError(f"no object {key}")
        if o["sub"]:
            raise WriteError(f"{key} is embedded; update its parent {o['parent']}")
        repo = self._repo(o["repo"])
        return repo, Path(o["path"]), o

    def _commit(self, path: Path, text: str, key: str, force: bool) -> dict:
        before = path.read_text() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        stats = Compiler(self.ws, self.store).compile()
        errors = [d for d in self.store.diagnostics("error") if d["key"] and d["key"].split("^")[0] == key]
        if errors and not force:
            if before is None:
                path.unlink()
            else:
                path.write_text(before)
            Compiler(self.ws, self.store).compile()
            raise WriteError(f"write rejected: {len(errors)} error(s) on {key}: " +
                             "; ".join(d["message"] for d in errors[:5]), errors)
        warnings = [d for d in self.store.diagnostics() if d["key"] and d["key"].split("^")[0] == key]
        return {"key": key, "path": str(path), "diagnostics": warnings, "compile": stats}

    def _stamp(self, fields: dict) -> dict:
        fields["updated"] = dt.date.today().isoformat()
        fields["updated_by"] = self.actor
        return fields

    def create(self, type: str, fields: dict, body: str = "", id: str | None = None, repo: str | None = None,
               folder: str | None = None, force: bool = False) -> dict:
        r = self._repo(repo)
        oid = id or slug(str(fields.get("title") or fields.get("statement") or fields.get("question") or type))
        key = f"{r.name}::{oid}"
        if self.store.get(key):
            raise WriteError(f"{key} already exists (use update)")
        fields = {k: v for k, v in fields.items() if v is not None and v != []}
        fields.setdefault("created", dt.date.today().isoformat())
        self._stamp(fields)
        path = r.path / "knowledge" / (folder or plural(type.split("::")[-1])) / f"{oid}.md"
        return self._commit(path, P.dump(fields, body, otype=type), key, force)

    def update(self, ref: str, set: dict | None = None, append: dict | None = None, unset: list[str] | None = None,
               body: str | None = None, append_body: str | None = None, force: bool = False) -> dict:
        key = self.store.find_key(ref) or ref
        repo, path, o = self._path_of(key)
        text = path.read_text()
        fm, old_body, _ = P.split_front_matter(text)
        head = yaml.safe_load(fm) if fm else {}
        head = head or {}
        for k, v in (set or {}).items():
            head[k] = v
        for k, v in (append or {}).items():
            cur = head.get(k) or []
            cur = cur if isinstance(cur, list) else [cur]
            for item in (v if isinstance(v, list) else [v]):
                if item not in cur:
                    cur.append(item)
            head[k] = cur
        for k in unset or []:
            head.pop(k, None)
        self._stamp(head)
        new_body = body if body is not None else old_body
        if append_body:
            new_body = new_body.rstrip() + "\n\n" + append_body.strip() + "\n"
        otype = head.pop("type", o["type_name"])
        oid = head.pop("id", None)
        return self._commit(path, P.dump(head, new_body, oid=oid, otype=otype), key, force)

    def link(self, src: str, field: str, target: str, force: bool = False) -> dict:
        key = self.store.find_key(src) or src
        o = self.store.get(key)
        if o is None:
            raise WriteError(f"no object {src}")
        tkey = self.store.find_key(target, o["repo"])
        t = self.store.get(tkey) if tkey else None
        ref = target if target.startswith("[[") else (
            f"[[{t['id'] if t['repo'] == o['repo'] else t['repo'] + '::' + t['id']}{'^' + t['sub'] if t and t['sub'] else ''}]]"
            if t else f"[[{target}]]")
        return self.update(key, append={field: [ref]}, force=force)

    def delete(self, ref: str, approved: bool = False) -> dict:
        """Destructive: requires approval (a human at the CLI)."""
        if not approved:
            raise WriteError("approval required: deleting knowledge is destructive (run `knowledge delete KEY --yes`)")
        key = self.store.find_key(ref) or ref
        _, path, _ = self._path_of(key)
        dependents = [e["src"] for e in self.store.edges_to(key) if e["kind"] != "contains"]
        path.unlink()
        Compiler(self.ws, self.store).compile()
        return {"deleted": key, "now_broken_links_from": dependents}
