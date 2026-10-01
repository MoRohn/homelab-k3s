"""KnowledgeSink: the adapter other LIF components use to record durable facts as typed knowledge.

    from lif.knowledge.sink import KnowledgeSink
    KnowledgeSink(actor="decision-engineering").record("decision-release", {"id": ..., "title": ...},
                                                       [("version-of", "batch-priority")])

* `record` never raises (errors are logged) and returns the object key, or None.
* A type the workspace defines is written as that type; anything else becomes an `event` whose
  `kind` is the given type (no silent schema invention).
* Edges become reference fields; edges whose targets do not exist are kept as text in
  `detail.unresolved_links` rather than written as broken links.
* Writes go to the workspace's `events_repo` (private operational history) unless `repo` is given;
  with neither, nothing is written. Per-request telemetry does not belong here.
* Off unless enabled: set LIF_KNOWLEDGE_SINK=1 (or pass enabled=True). Test suites and dev runs that
  construct services therefore never write into the owner's knowledge repos by accident.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lif.common import log
from lif.knowledge.writer import slug

LOG = log.get("lif.knowledge.sink")


class KnowledgeSink:
    def __init__(self, root: str | Path | None = None, repo: str | None = None, actor: str = "lif",
                 enabled: bool | None = None):
        self.root, self.repo_name, self.actor = root, repo, actor
        self.enabled = enabled if enabled is not None else os.environ.get("LIF_KNOWLEDGE_SINK") == "1"
        self._kn = None

    @property
    def kn(self):
        if self._kn is None:                          # lazy: no compile cost until the first record
            from lif.knowledge.ops import Knowledge
            self._kn = Knowledge.open(self.root)
            self.repo = self.repo_name or self._kn.ws.config.get("events_repo")
        return self._kn

    def record(self, type: str, obj: dict, edges: list[tuple[str, str]] | None = None) -> str | None:
        if not self.enabled:
            return None
        try:
            return self._record(type, dict(obj), edges or [])
        except Exception:                            # noqa: BLE001 — recording must never break the caller
            LOG.exception("knowledge sink record failed")
            return None

    def _record(self, type: str, obj: dict, edges: list[tuple[str, str]]) -> str | None:
        kn = self.kn
        repo = kn.ws.repo(self.repo) if self.repo else None
        if repo is None or not repo.writable:
            return None
        self.kn.refresh()
        known = self.kn.store.db.one("SELECT qname FROM types WHERE name=?", (type,)) is not None
        oid = slug(str(obj.pop("id", "") or obj.get("title") or type), 80)
        if self.kn.store.get(f"{repo.name}::{oid}"):
            return f"{repo.name}::{oid}"                 # idempotent
        unresolved = []
        fields: dict[str, Any] = {}
        for rel, target in edges:
            if self.kn.store.find_key(target, repo.name):
                fields.setdefault(rel, []).append(f"[[{target}]]")
            else:
                unresolved.append(f"{rel} → {target}")
        if known:
            fields.update(obj)
            otype = type
        else:
            scalars = {k: v for k, v in obj.items() if isinstance(v, (str, int, float, bool)) and k in ("title",)}
            fields = {**scalars, "kind": type, "actor": obj.get("actor", self.actor),
                      "detail": {k: v for k, v in obj.items() if k != "title"}, **{
                          k: v for k, v in fields.items()}}
            otype = "event"
        if unresolved:
            fields.setdefault("detail", {})["unresolved_links"] = unresolved
        res = self.kn.writer(self.actor).create(otype, fields, "", id=oid, repo=repo.name,
                                                folder="events" if otype == "event" else None, force=True)
        return res["key"]
