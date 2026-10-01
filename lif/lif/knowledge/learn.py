"""Learning loops: incidents become lessons, lessons change methods, methods change skills/checks;
sources become typed, evidence-linked candidate objects.

    INCIDENT → evidence → root cause → failed assumption → LESSON → METHOD CHANGE → SKILL/CHECK UPDATE

Every step is a typed object or a versioned file edit that links back to what caused it, so a later
agent reading the check (or the method step) can follow `because` back to the incident.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from lif.knowledge import parse as P
from lif.knowledge.writer import WriteError, slug

if TYPE_CHECKING:
    from lif.knowledge.ops import Knowledge


def _local_link(kn: "Knowledge", key: str, from_repo: str) -> str:
    o = kn.store.get(key)
    if o is None:
        raise WriteError(f"no object {key}")
    lid = o["id"] + (f"^{o['sub']}" if o["sub"] else "")
    return f"[[{lid}]]" if o["repo"] == from_repo else f"[[{o['repo']}::{lid}]]"


def learn(kn: "Knowledge", repo: str, incident: str, observation: str, lesson: str, *, lesson_id: str | None = None,
          invalidates: list[str] | None = None, method: str | None = None, method_change: str | None = None,
          new_step: str | None = None, skill: str | None = None, check: dict | None = None,
          actor: str = "agent") -> dict:
    """Record a lesson from an incident and propagate it into a method and a skill check."""
    g, w = kn.graph, kn.writer(actor)
    inc_key = g.key(incident)
    today = dt.date.today().isoformat()
    lid = lesson_id or slug("lesson " + lesson, 70)
    changed: list[str] = []
    result: dict[str, Any] = {"incident": inc_key}

    # 1. assumptions the incident disproved (their dependents enter the reconsideration queue)
    for a in invalidates or []:
        ak = g.key(a)
        w.update(ak, set={"status": "invalidated"},
                 append={"contradicted_by": [_local_link(kn, inc_key, kn.store.get(ak)["repo"])]})
    result["invalidated"] = [g.key(a) for a in invalidates or []]

    # 2. the method change (method objects must live in a writable repo; packages change by publishing)
    if method:
        mk = g.key(method)
        mo = kn.store.get(mk)
        ver = int(mo["fields"].get("version") or 1) + 1
        sets: dict[str, Any] = {"version": ver}
        if new_step:
            steps = list(mo["fields"].get("steps") or [])
            if new_step not in steps:
                sets["steps"] = steps + [new_step]
        result["method"] = {"key": mk, "version": ver}
        changed.append(mk)
    # 3. lesson (written before the method edit so the method's links resolve)
    w.create("lesson", {"title": lesson[:90], "observation": observation, "lesson": lesson,
                        "evidence": [_local_link(kn, inc_key, repo)],
                        "invalidates": [_local_link(kn, g.key(a), repo) for a in invalidates or []],
                        "changed": [_local_link(kn, c, repo) for c in changed]},
             f"Learned from {_local_link(kn, inc_key, repo)}.", id=lid, repo=repo, folder="lessons")
    result["lesson"] = f"{repo}::{lid}"
    if method:
        because = _local_link(kn, result["lesson"], mo["repo"])
        rev = {"version": ver, "date": today, "change": method_change or lesson, "because": because}
        w.update(mk, set=sets, append={"revisions": [rev], "derived_from": [because]})
        w.update(inc_key, append={"lessons": [_local_link(kn, result["lesson"], kn.store.get(inc_key)["repo"])]},
                 force=True)

    # 4. the skill check
    if skill and check:
        from lif.knowledge.skills import find_skill
        r, d = find_skill(kn, skill)
        if not r.writable:
            raise WriteError(f"skill {skill} lives in package {r.name}@{r.version}; fork it into {repo} "
                             f"(`knowledge skill fork {skill}`) and publish a new package version")
        cf = d / "checks.yaml"
        spec = yaml.safe_load(cf.read_text()) if cf.exists() else {}
        spec = spec or {}
        c = dict(check)
        c.setdefault("because", f"[[{repo}::{lid}]]" if r.name != repo else f"[[{lid}]]")
        spec["checks"] = [x for x in spec.get("checks") or [] if x.get("id") != c["id"]] + [c]
        cf.write_text(yaml.safe_dump(spec, sort_keys=False))
        skill_md = d / "SKILL.md"
        fm, body, _ = P.split_front_matter(skill_md.read_text())
        head = yaml.safe_load(fm) or {}
        head["version"] = int(head.get("version", 1)) + 1
        head["updated"] = today
        head.setdefault("derived_from", [])
        head["derived_from"] = list(head["derived_from"]) + [c["because"]]
        skill_md.write_text(P.dump({k: v for k, v in head.items() if k not in ("type", "id")}, body,
                                   oid=head.get("id"), otype=head.get("type", "skill")))
        kn.refresh(force=True)
        sk = kn.store.find_key(skill, r.name)
        if sk:
            w.update(result["lesson"], append={"changed": [_local_link(kn, sk, repo)]})
        result["skill"] = {"name": skill, "repo": r.name, "version": head["version"], "check": c["id"]}
    kn.refresh(force=True)
    result["reconsiderations"] = [r for r in kn.store.reconsiderations()
                                  if r["trigger"] in result["invalidated"]]
    return result


def fork_skill(kn: "Knowledge", skill: str, repo: str) -> dict:
    """Copy a package's skill into a project repo (the project's copy then overrides the package's)."""
    import shutil
    from lif.knowledge.skills import find_skill
    r, d = find_skill(kn, skill)
    dst_repo = kn.ws.repo(repo)
    if dst_repo is None or not dst_repo.writable:
        raise WriteError(f"no writable repo {repo}")
    dst = dst_repo.path / "skills" / d.name
    if dst.exists():
        raise WriteError(f"{dst} exists")
    shutil.copytree(d, dst)
    kn.refresh(force=True)
    return {"forked": skill, "from": f"{r.name}@{r.version}", "to": str(dst)}


# ── ingestion ────────────────────────────────────────────────────────────────

def ingest(kn: "Knowledge", path: str | Path, repo: str, *, kind: str = "documentation", author: str = "",
           extract: bool = True, actor: str = "agent", copy: bool = True) -> dict:
    """SOURCE → parse → deterministic metadata → classify passages (rules/Jev) → typed candidate objects
    (status open/proposed, each linked to its exact passage) → validation → review queue.

    The original is copied into the repo (sources/) with its hash, so evidence keeps a traceable origin."""
    from lif.knowledge.decisions import decide
    src = Path(path)
    r = kn.ws.repo(repo)
    if r is None or not r.writable:
        raise WriteError(f"no writable repo {repo}")
    raw = src.read_bytes()
    text = _to_text(src, raw)
    sha = hashlib.sha256(raw).hexdigest()
    sid = slug("src " + src.stem, 70)
    if kn.store.get(f"{repo}::{sid}"):
        old = kn.store.get(f"{repo}::{sid}")
        if old["fields"].get("sha256") == sha:
            return {"source": f"{repo}::{sid}", "unchanged": True, "created": []}
        sid = f"{sid}-{sha[:8]}"
    loc = str(src)
    if copy:
        dst = r.path / "sources" / "originals" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
        loc = str(Path("../../sources/originals") / src.name)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    body_parts, cands = [], []
    for i, p in enumerate(paras, 1):
        one = " ".join(p.split())
        body_parts.append(f"{p} ^p{i}" if not p.startswith(("#", "```", "|")) else p)
        if extract and not p.startswith(("#", "```", "|")):
            d = decide("knowledge-object-type", {"text": one[:1200]})
            if d["decision"] and d["decision"] != "none":
                cands.append((i, one, d))
    w = kn.writer(actor)
    src_fields = {"title": src.stem.replace("-", " ").replace("_", " "), "kind": kind, "location": loc,
                  "sha256": sha, "captured": dt.date.today().isoformat(), "author": author or None,
                  "quality": "unknown"}
    w.create("source", src_fields, "\n\n".join(body_parts), id=sid, repo=repo, folder="sources", force=True)
    created = []
    for i, one, d in cands:
        t = d["decision"]
        cid = slug(f"{t} {one}", 60)
        if kn.store.get(f"{repo}::{cid}"):
            continue
        pas = f"[[{sid}^p{i}]]"
        base = {"title": one[:90], "extracted_from": pas, "extraction": {"by": d["provider"],
                                                                         "confidence": d["confidence"],
                                                                         "needs_review": True}}
        if t in ("claim", "requirement", "commitment"):
            fields = {**base, "statement": one, "status": "open",
                      "grounds": [{"id": "g1", "source": f"[[{sid}]]", "passage": pas, "stance": "reports",
                                   "basis": "reported"}]}
            otype = "claim" if t == "claim" or not _has_type(kn, repo, t) else t
        elif t == "question":
            fields, otype = {**base, "status": "open"}, "question"
        else:   # decision found in a document: record it as proposed, evidence = the source itself
            fields, otype = {**base, "status": "proposed", "evidence": [f"[[{sid}]]"],
                             "date": dt.date.today().isoformat()}, "decision"
        try:
            res = w.create(otype, fields, f"Extracted from {pas}: “{one[:400]}”", id=cid, repo=repo,
                           folder="extracted")
            created.append({"key": res["key"], "type": otype, "by": d["provider"], "confidence": d["confidence"]})
        except WriteError as e:
            created.append({"error": str(e)[:200], "passage": i})
    return {"source": f"{repo}::{sid}", "sha256": sha, "passages": len(paras), "created": created,
            "review": "extracted objects are status open/proposed with extraction.needs_review: true"}


def _has_type(kn: "Knowledge", repo: str, t: str) -> bool:
    return kn.store.db.one("SELECT 1 AS x FROM types WHERE name=?", (t,)) is not None


def _to_text(src: Path, raw: bytes) -> str:
    suf = src.suffix.lower()
    if suf == ".pdf":
        import shutil
        import subprocess
        if shutil.which("pdftotext"):
            return subprocess.run(["pdftotext", "-layout", str(src), "-"], capture_output=True, text=True,
                                  check=True).stdout
        raise WriteError("PDF ingestion needs `pdftotext` (poppler-utils) on the host")
    text = raw.decode("utf-8", errors="replace")
    if suf == ".json":
        import json
        data = json.loads(text)
        return "\n\n".join(f"{k}: {json.dumps(v)}" for k, v in (data.items() if isinstance(data, dict) else
                                                                 enumerate(data)))
    if suf == ".csv":
        import csv
        import io
        rows = list(csv.reader(io.StringIO(text)))
        if rows:
            head = rows[0]
            return "\n\n".join("; ".join(f"{h}: {v}" for h, v in zip(head, r)) for r in rows[1:])
    if suf in (".md", ".markdown"):
        _, body, _ = P.split_front_matter(text)
        return body
    return text
