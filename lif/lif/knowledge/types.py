"""Type system: declarative `*.type.yaml` definitions with namespaces, inheritance and typed references.

A type file:

    name: decision
    version: 2
    extends: object
    fields:
      status:   {type: enum, values: [proposed, accepted, rejected, superseded], required: true}
      evidence: {type: "evidence[]", min: 1}
      supersedes: {type: "decision?", propagates: false, acyclic: true}
      used_by:  {type: "object[]", inverse: true}      # edge points the other way (target depends on me)

Field type grammar: a scalar (string, text, int, float, bool, date, url, enum, any, map) or a
reference to a type name (`evidence`, `pkg::evidence`, `object` = anything), with an optional
`[]` (list) or `?` (optional) suffix. Every type lives in a namespace (the package or repo that
defines it); unqualified names resolve against the referencing repo first, then its dependencies.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

SCALARS = {"string", "text", "int", "float", "bool", "date", "url", "enum", "any", "map"}
BUILTIN_NS = "builtin"
REF_RE = re.compile(r"^\[\[([^\]]+)\]\]$")


@dataclass
class FieldDef:
    name: str
    type: str                     # scalar name or a type reference (unqualified or ns::name)
    many: bool = False
    required: bool = False
    values: list[str] = field(default_factory=list)
    min: int | None = None
    max: int | None = None
    inverse: bool = False         # edge stored target → self
    propagates: bool = True       # participates in change-impact traversal
    acyclic: bool = False
    embedded: bool = False        # list items are inline records with an `id`
    description: str = ""

    @property
    def is_ref(self) -> bool:
        return self.type not in SCALARS

    @classmethod
    def parse(cls, name: str, raw: Any) -> "FieldDef":
        if isinstance(raw, str):
            raw = {"type": raw}
        raw = dict(raw or {})
        t = str(raw.pop("type", "string")).strip()
        many = optional = False
        if t.endswith("[]"):
            many, t = True, t[:-2]
        if t.endswith("?"):
            optional, t = True, t[:-1]
        mn = raw.get("min")
        required = bool(raw.get("required", False)) or (mn is not None and int(mn) >= 1 and not optional)
        return cls(name=name, type=t, many=many, required=required, values=[str(v) for v in raw.get("values", [])],
                   min=mn, max=raw.get("max"), inverse=bool(raw.get("inverse", False)),
                   propagates=bool(raw.get("propagates", True)), acyclic=bool(raw.get("acyclic", False)),
                   embedded=bool(raw.get("embedded", False)), description=str(raw.get("description", "")))

    def signature(self) -> dict:
        """What matters for compatibility between two versions of a type."""
        return {"type": self.type, "many": self.many, "required": self.required, "values": sorted(self.values),
                "min": self.min, "max": self.max}


@dataclass
class TypeDef:
    name: str
    namespace: str
    version: int = 1
    extends: str | None = "object"
    fields: dict[str, FieldDef] = field(default_factory=dict)
    description: str = ""
    strict: bool = False                     # unknown fields → warning
    review_after_days: int | None = None     # default staleness horizon
    title_field: str | None = None
    path: str = ""

    @property
    def qname(self) -> str:
        return f"{self.namespace}::{self.name}"

    @classmethod
    def from_yaml(cls, raw: dict, namespace: str, path: str = "") -> "TypeDef":
        return cls(name=str(raw["name"]), namespace=namespace, version=int(raw.get("version", 1)),
                   extends=raw.get("extends", "object") if raw.get("name") != "object" else None,
                   fields={k: FieldDef.parse(k, v) for k, v in (raw.get("fields") or {}).items()},
                   description=str(raw.get("description", "")), strict=bool(raw.get("strict", False)),
                   review_after_days=raw.get("review_after_days"), title_field=raw.get("title_field"), path=path)


# Built-in base types every repo can use without a dependency. Kept tiny on purpose:
# domain types belong in packages (knowledge-governance, ai-infrastructure-core, …).
_COMMON = {
    "title": {"type": "string"},
    "status": {"type": "string"},
    "tags": {"type": "string[]"},
    "owner": {"type": "string"},
    "project": {"type": "project?", "propagates": False},
    "created": {"type": "date"},
    "updated": {"type": "date"},
    "updated_by": {"type": "string"},
    "review_after": {"type": "date"},
    "expires_after": {"type": "date"},
    "refresh_when": {"type": "string[]"},
    "pinned": {"type": "bool"},
    "related": {"type": "object[]", "propagates": False},
    "data_class": {"type": "enum", "values": ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]},
}
BUILTIN_TYPES = [
    TypeDef.from_yaml({"name": "object", "fields": _COMMON, "description": "Root of every type"}, BUILTIN_NS),
    TypeDef.from_yaml({"name": "document", "description": "Untyped or lightly typed note (incremental adoption)"},
                      BUILTIN_NS),
    TypeDef.from_yaml({"name": "project", "fields": {"summary": "text", "repo": "string"}}, BUILTIN_NS),
    TypeDef.from_yaml({"name": "passage", "fields": {"text": "text", "line": "int"},
                       "description": "A block of a document marked with ^id; the unit of source evidence"},
                      BUILTIN_NS),
]


class TypeSystem:
    """All types visible in a workspace, indexed by qualified name, with per-repo name resolution."""

    def __init__(self):
        self.types: dict[str, TypeDef] = {}
        self.scopes: dict[str, list[str]] = {}       # repo → namespaces it can see, in priority order
        for t in BUILTIN_TYPES:
            self.add(t)

    def add(self, t: TypeDef) -> None:
        self.types[t.qname] = t

    def set_scope(self, repo: str, namespaces: list[str]) -> None:
        self.scopes[repo] = [repo] + [n for n in namespaces if n != repo] + [BUILTIN_NS]

    def resolve(self, name: str, repo: str) -> tuple[TypeDef | None, str]:
        """→ (type, error). Qualified names must exist; unqualified must be unambiguous in scope."""
        if "::" in name:
            t = self.types.get(name)
            return (t, "") if t else (None, f"unknown type '{name}'")
        scope = self.scopes.get(repo, [repo, BUILTIN_NS])
        hits = [self.types[f"{ns}::{name}"] for ns in scope if f"{ns}::{name}" in self.types]
        if not hits:
            return None, f"unknown type '{name}'"
        if hits[0].namespace == repo or len(hits) == 1:
            return hits[0], ""
        if len({h.qname for h in hits if h.namespace != BUILTIN_NS}) > 1:
            return None, f"ambiguous type '{name}': " + ", ".join(h.qname for h in hits) + " (qualify it)"
        return hits[0], ""

    def parent(self, t: TypeDef) -> TypeDef | None:
        if not t.extends:
            return None
        p, _ = self.resolve(t.extends, t.namespace)
        return p if p is not t else None

    def lineage(self, t: TypeDef) -> list[TypeDef]:
        out, seen = [], set()
        cur: TypeDef | None = t
        while cur is not None and cur.qname not in seen:
            out.append(cur)
            seen.add(cur.qname)
            cur = self.parent(cur)
        return out

    def all_fields(self, t: TypeDef) -> dict[str, FieldDef]:
        merged: dict[str, FieldDef] = {}
        for anc in reversed(self.lineage(t)):          # child overrides parent
            merged.update(anc.fields)
        return merged

    def is_a(self, t: TypeDef, expected: str, repo: str) -> bool:
        if expected in ("object", f"{BUILTIN_NS}::object"):
            return True
        want, _ = self.resolve(expected, repo)
        if want is None:
            return False
        return any(a.qname == want.qname for a in self.lineage(t))


# ── scalar validation ────────────────────────────────────────────────────────

def as_date(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        try:
            return dt.date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def check_scalar(f: FieldDef, v: Any) -> str | None:
    """→ error message or None."""
    t = f.type
    if v is None:
        return None
    if t == "enum":
        return None if str(v) in f.values else f"'{v}' is not one of {f.values}"
    if t == "date":
        return None if as_date(v) else f"'{v}' is not a date (YYYY-MM-DD)"
    if t == "int":
        return None if isinstance(v, int) and not isinstance(v, bool) else f"'{v}' is not an integer"
    if t == "float":
        return None if isinstance(v, (int, float)) and not isinstance(v, bool) else f"'{v}' is not a number"
    if t == "bool":
        return None if isinstance(v, bool) else f"'{v}' is not true/false"
    if t == "map":
        return None if isinstance(v, dict) else "expected a mapping"
    if t in ("string", "text", "url"):
        return None if isinstance(v, (str, int, float)) else f"expected text, got {type(v).__name__}"
    return None


def type_changes(old: TypeDef, new: TypeDef) -> list[dict]:
    """Compatibility diff between two versions of one type. `breaking` changes can invalidate
    existing objects; the rest cannot."""
    out = []
    for name, f in old.fields.items():
        g = new.fields.get(name)
        if g is None:
            out.append({"field": name, "change": "removed", "breaking": True})
            continue
        if g.type != f.type or g.many != f.many:
            out.append({"field": name, "change": f"type {f.type}{'[]' if f.many else ''} → "
                                                  f"{g.type}{'[]' if g.many else ''}", "breaking": True})
        if g.required and not f.required:
            out.append({"field": name, "change": "now required", "breaking": True})
        removed = set(f.values) - set(g.values)
        if removed:
            out.append({"field": name, "change": f"enum values removed: {sorted(removed)}", "breaking": True})
        added = set(g.values) - set(f.values)
        if added:
            out.append({"field": name, "change": f"enum values added: {sorted(added)}", "breaking": False})
        if (g.min or 0) > (f.min or 0):
            out.append({"field": name, "change": f"min {f.min} → {g.min}", "breaking": True})
    for name, g in new.fields.items():
        if name not in old.fields:
            out.append({"field": name, "change": "added" + (" (required)" if g.required else ""),
                        "breaking": g.required})
    if (old.extends or "") != (new.extends or ""):
        out.append({"field": "", "change": f"extends {old.extends} → {new.extends}", "breaking": True})
    return out
