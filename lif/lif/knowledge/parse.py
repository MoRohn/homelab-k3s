"""Typed Markdown parser: YAML front matter + Markdown body → raw objects.

    ---
    type: decision
    status: accepted
    evidence: ["[[dgx-resource-baseline]]"]
    grounds:
      - id: g1
        source: "[[gpu-profile-run-12]]"
        stance: supports
    ---
    Prose may cite [[other-object]], [[pkg::object]], an embedded record [[^g1]],
    or a passage of another object [[some-source^p3]].

    A paragraph ending in a block anchor becomes a citable passage. ^p1

* A file with no front matter is a `document` (incremental adoption: raw → indexed → typed).
* The object id is `id:` from the front matter, else the file name without `.md`.
* Parsing never raises: problems come back as diagnostics so one bad file can't stop a compile.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

WIKILINK = re.compile(r"\[\[([^\[\]|]+?)(?:\|[^\]]*)?\]\]")
ANCHOR = re.compile(r"\s\^([A-Za-z0-9][A-Za-z0-9_-]*)\s*$")
ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class Link:
    raw: str          # as written, without brackets
    repo: str | None  # explicit namespace (pkg::id)
    id: str           # '' for a local embedded ref [[^g1]]
    sub: str | None   # embedded/passage id after ^

    @classmethod
    def parse(cls, text: str) -> "Link":
        t = text.strip()
        if t.startswith("[[") and t.endswith("]]"):
            t = t[2:-2].split("|", 1)[0].strip()
        repo = None
        if "::" in t:
            repo, t = t.split("::", 1)
        sub = None
        if "^" in t:
            t, sub = t.split("^", 1)
        return cls(raw=text.strip().strip("[]"), repo=repo, id=t.strip(), sub=(sub or "").strip() or None)


@dataclass
class RawObject:
    repo: str
    id: str
    type: str
    fields: dict[str, Any]
    body: str
    path: str
    line: int = 1
    parent: str | None = None      # id of the containing object (embedded records, passages)
    sub: str | None = None
    sha: str = ""

    @property
    def local_key(self) -> str:
        return f"{self.id}^{self.sub}" if self.sub else self.id

    @property
    def key(self) -> str:
        return f"{self.repo}::{self.local_key}"


@dataclass
class ParseResult:
    objects: list[RawObject] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)


def is_link(v: Any) -> bool:
    return isinstance(v, str) and v.strip().startswith("[[") and v.strip().endswith("]]")


def split_front_matter(text: str) -> tuple[str | None, str, int]:
    """→ (yaml text or None, body, body start line)."""
    if not text.startswith("---"):
        return None, text, 1
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return None, text, 1
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]), i + 2
    return None, text, 1


def _title(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def passages(body: str, start_line: int) -> list[tuple[str, str, int]]:
    """Paragraphs ending in ` ^id` → (id, text without the anchor, line)."""
    out = []
    para: list[str] = []
    para_line = start_line
    for i, line in enumerate(body.split("\n") + [""]):
        if line.strip():
            if not para:
                para_line = start_line + i
            para.append(line)
            m = ANCHOR.search(line)
            if m:
                text = "\n".join(para[:-1] + [ANCHOR.sub("", line)]).strip()
                out.append((m.group(1), text, para_line))
                para = []
        else:
            para = []
    return out


def parse_file(text: str, repo: str, path: str, stem: str) -> ParseResult:
    res = ParseResult()
    sha = hashlib.sha256(text.encode()).hexdigest()
    fm_text, body, body_line = split_front_matter(text)
    fields: dict[str, Any] = {}
    if fm_text is not None:
        try:
            loaded = yaml.safe_load(fm_text)
        except yaml.YAMLError as e:
            res.problems.append({"code": "K022", "path": path, "line": 1,
                                 "message": f"front matter is not valid YAML: {str(e).splitlines()[0]}"})
            loaded = {}
        if loaded is not None and not isinstance(loaded, dict):
            res.problems.append({"code": "K022", "path": path, "line": 1, "message": "front matter must be a mapping"})
            loaded = {}
        fields = loaded or {}
    oid = str(fields.pop("id", stem))
    if not ID_OK.match(oid):
        res.problems.append({"code": "K022", "path": path, "line": 1,
                             "message": f"id '{oid}' must be letters, digits, '.', '_' or '-'"})
    otype = str(fields.pop("type", "document"))
    if "title" not in fields:
        t = _title(body)
        if t:
            fields["title"] = t
    obj = RawObject(repo=repo, id=oid, type=otype, fields=fields, body=body, path=path, sha=sha)
    res.objects.append(obj)

    # embedded records: any list of mappings that carry an `id`
    for fname, val in list(fields.items()):
        if isinstance(val, list) and val and all(isinstance(x, dict) for x in val) and any("id" in x for x in val):
            for item in val:
                if "id" not in item:
                    res.problems.append({"code": "K022", "path": path, "line": 1,
                                         "message": f"embedded record in '{fname}' has no id"})
                    continue
                rec = dict(item)
                sub = str(rec.pop("id"))
                rtype = str(rec.pop("type", ""))       # filled from the field definition when blank
                res.objects.append(RawObject(repo=repo, id=oid, sub=sub, type=rtype, fields=rec, body="",
                                             path=path, parent=oid, sha=sha))
    # citable passages
    for pid, ptext, pline in passages(body, body_line):
        res.objects.append(RawObject(repo=repo, id=oid, sub=pid, type="passage",
                                     fields={"text": ptext, "line": pline}, body=ptext, path=path, line=pline,
                                     parent=oid, sha=sha))
    return res


def body_links(body: str) -> list[Link]:
    return [Link.parse(m.group(1)) for m in WIKILINK.finditer(body)]


def dump(fields: dict, body: str, oid: str | None = None, otype: str | None = None) -> str:
    """Serialize an object back to typed Markdown (front matter keys keep insertion order)."""
    head: dict[str, Any] = {}
    if otype:
        head["type"] = otype
    if oid:
        head["id"] = oid
    head.update({k: v for k, v in fields.items() if k not in ("type", "id")})
    fm = yaml.safe_dump(head, sort_keys=False, allow_unicode=True, width=100).rstrip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n" if body.strip() else f"---\n{fm}\n---\n"
