"""Knowledge compiler: agent repos → typed, linked, diagnosed graph in the index.

    repos → (incremental) parse → type check → reference resolution → semantic rules → index

Incremental: a file whose (mtime, size) is unchanged reuses its cached parse; a file whose content
hash is unchanged reuses its index rows. Resolution and diagnostics always run over the whole
graph, because one deleted file can break references anywhere (cheap at homelab scale: thousands
of objects compile in well under a second).

Diagnostic codes (docs/KNOWLEDGE.md has the full table):
  K001 unknown type          K002 missing required field  K003 invalid enum value
  K004 bad scalar value      K005 broken reference        K006 wrong reference type
  K007 duplicate id          K008 cardinality (min/max)   K009 unknown field (strict types)
  K010 prohibited cycle      K011 orphaned object         K012 stale (review_after / expires_after)
  K013 reconsideration required                           K014 decision without evidence
  K015 unverified claim      K016 unresolved contradiction K017 source changed since capture
  K018 dependency/package    K019 ambiguous type          K020 optional repo missing
  K021 evidence without traceable origin                  K022 parse error
  K023 cross-repo link to a repo that is not a dependency
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from lif.knowledge import parse as P
from lif.knowledge.repo import Repo, Workspace
from lif.knowledge.store import Store
from lif.knowledge.types import BUILTIN_NS, FieldDef, TypeDef, TypeSystem, as_date, check_scalar

SEVERITY = {"K001": "error", "K002": "error", "K003": "error", "K004": "error", "K005": "error", "K006": "error",
            "K007": "error", "K008": "error", "K009": "warning", "K010": "error", "K011": "info", "K012": "warning",
            "K013": "warning", "K014": "warning", "K015": "info", "K016": "warning", "K017": "warning",
            "K018": "error", "K019": "error", "K020": "info", "K021": "warning", "K022": "error", "K023": "error"}
INVALIDATING = {"invalidated", "superseded", "deprecated", "retracted", "refuted", "challenged"}
CLOSED = {"completed", "done", "cancelled", "rejected", "superseded", "resolved", "closed", "answered"}
# what can be asked to reconsider: work and conclusions, not raw evidence
RECONSIDERABLE = {"decision", "assumption", "claim", "commitment", "requirement", "task", "workflow", "method",
                  "skill", "plan", "feature", "deployment", "risk", "lesson"}


def today() -> dt.date:
    return dt.date.today()


class Compiler:
    def __init__(self, ws: Workspace, store: Store):
        self.ws, self.store = ws, store
        self.ts = TypeSystem()
        self.diags: list[dict] = []
        self.stats: dict[str, Any] = {}

    # ── entry point ──────────────────────────────────────────────────────────
    def compile(self, full: bool = False) -> dict:
        t0 = time.perf_counter()
        if full:
            self.store.clear(include_cache=True)
        self.diags = [dict(p, severity=p.get("severity") or SEVERITY[p["code"]]) for p in self.ws.problems]
        self._load_types()
        raw = self._parse_all()
        objs, edges = self._check(raw)
        recons = self._semantics(objs, edges)
        res = self.store.replace_graph(
            list(objs.values()), edges, self.diags, recons,
            [{"qname": t.qname, "namespace": t.namespace, "name": t.name, "version": t.version, "extends": t.extends,
              "definition": {"fields": {k: asdict(f) for k, f in t.fields.items()}, "description": t.description,
                             "path": t.path}} for t in self.ts.types.values()],
            [{"name": r.name, "path": str(r.path), "version": r.version, "kind": r.kind, "source": r.source,
              "manifest": r.manifest} for r in self.ws.all_repos()])
        self.stats.update(res, seconds=round(time.perf_counter() - t0, 4), objects=len(objs),
                          diagnostics={s: sum(1 for d in self.diags if d["severity"] == s)
                                       for s in ("error", "warning", "info")})
        self.store.set_meta("compiled_at", time.time())
        self.store.set_meta("last_compile", self.stats)
        _observe(self.stats)
        return self.stats

    def _diag(self, code: str, message: str, key: str | None = None, path: str = "", line: int | None = None,
              **detail) -> None:
        self.diags.append({"code": code, "severity": SEVERITY[code], "key": key, "path": path, "line": line,
                           "message": message, "detail": detail})

    # ── types ────────────────────────────────────────────────────────────────
    def _load_types(self) -> None:
        for repo in self.ws.all_repos():
            for f in repo.type_files():
                try:
                    raw = yaml.safe_load(f.read_text()) or {}
                    self.ts.add(TypeDef.from_yaml(raw, repo.name, str(f)))
                except Exception as e:          # noqa: BLE001 — a bad type file is a diagnostic
                    self._diag("K022", f"type definition: {e}", path=str(f))
        for repo in self.ws.all_repos():
            self.ts.set_scope(repo.name, self.ws.visible(repo))
        for t in self.ts.types.values():
            if t.extends and t.namespace != BUILTIN_NS:
                p, err = self.ts.resolve(t.extends, t.namespace)
                if p is None:
                    self._diag("K001", f"type {t.qname} extends {err}", path=t.path)

    # ── parse (incremental) ──────────────────────────────────────────────────
    def _parse_all(self) -> list[tuple[Repo, P.RawObject]]:
        out: list[tuple[Repo, P.RawObject]] = []
        seen: set[str] = set()
        parsed = cached = 0
        for repo in self.ws.all_repos():
            for f in repo.markdown_files():
                path = str(f)
                seen.add(path)
                st = f.stat()
                c = self.store.cached_file(path)
                if c and c["mtime"] == st.st_mtime and c["size"] == st.st_size:
                    items = json.loads(c["parsed"])
                    cached += 1
                else:
                    text = f.read_text(errors="replace")
                    stem = f.stem if f.name != "SKILL.md" else f.parent.name
                    res = P.parse_file(text, repo.name, path, stem)
                    items = [{"obj": asdict(o)} for o in res.objects] + [{"problem": p} for p in res.problems]
                    for it in items:
                        if "obj" in it:
                            it["obj"]["mtime"] = st.st_mtime
                    self.store.put_file(path, repo.name, st.st_mtime, st.st_size,
                                        hashlib.sha256(text.encode()).hexdigest(), items)
                    parsed += 1
                for it in items:
                    if "problem" in it:
                        p = it["problem"]
                        self._diag(p["code"], p["message"], path=p["path"], line=p.get("line"))
                    else:
                        o = dict(it["obj"])
                        mtime = o.pop("mtime", 0)
                        ro = P.RawObject(**o)
                        ro.fields.setdefault("__mtime", mtime)
                        out.append((repo, ro))
        self.store.drop_files_except(seen)
        self.stats.update(files_parsed=parsed, files_cached=cached)
        return out

    # ── type check + resolution ──────────────────────────────────────────────
    def _check(self, raw: list[tuple[Repo, P.RawObject]]) -> tuple[dict[str, dict], list[dict]]:
        objs: dict[str, dict] = {}
        tdefs: dict[str, TypeDef] = {}
        rawmap: dict[str, tuple[Repo, P.RawObject]] = {}
        projects = {r.name: (r.manifest.get("project") or (r.name if r.kind == "project" else "")) for r in
                    self.ws.all_repos()}
        # 1. identities and types (parents first so embedded records can take their field's type)
        for repo, ro in sorted(raw, key=lambda x: x[1].sub is not None):
            key = ro.key
            if key in rawmap:
                other = rawmap[key][1]
                self._diag("K007", f"duplicate id '{ro.local_key}' (also in {other.path})", key=key, path=ro.path,
                           line=ro.line)
                continue
            mtime = ro.fields.pop("__mtime", 0)
            tname = ro.type
            if ro.sub and not tname:
                parent = rawmap.get(f"{ro.repo}::{ro.parent}")
                fdef = self._field_of_parent(parent, ro.sub) if parent else None
                tname = fdef.type if fdef is not None and fdef.is_ref else "object"
            t, err = self.ts.resolve(tname, repo.name)
            if t is None:
                self._diag("K019" if err.startswith("ambiguous") else "K001", err, key=key, path=ro.path,
                           line=ro.line, received=tname)
                t, _ = self.ts.resolve("document", repo.name)
            rawmap[key] = (repo, ro)
            tdefs[key] = t
            updated = as_date(ro.fields.get("updated")) or as_date(ro.fields.get("date")) or \
                dt.date.fromtimestamp(mtime or time.time())
            objs[key] = {
                "key": key, "repo": repo.name, "id": ro.id, "sub": ro.sub,
                "parent": f"{ro.repo}::{ro.parent}" if ro.parent else None, "type": t.qname, "type_name": t.name,
                "title": str(ro.fields.get("title") or ro.fields.get(t.title_field or "", "") or
                             ro.fields.get("statement") or ro.fields.get("question") or
                             (ro.fields.get("text", "")[:80] if ro.type == "passage" else "") or ro.local_key),
                "status": str(ro.fields.get("status", "")), "path": ro.path, "line": ro.line, "body": ro.body,
                "fields": ro.fields, "sha": ro.sha, "updated": updated.isoformat(),
                "updated_by": str(ro.fields.get("updated_by", ro.fields.get("created_by", ""))),
                "project": str(ro.fields.get("project", "")).strip("[]") or projects.get(repo.name, ""),
                "repo_version": repo.version, "source": repo.source, "mtime": float(mtime or 0)}
        # 2. fields
        edges: list[dict] = []
        for key, (repo, ro) in rawmap.items():
            t = tdefs[key]
            fdefs = self.ts.all_fields(t)
            for fname, fd in fdefs.items():
                v = ro.fields.get(fname)
                if fd.required and (v is None or v == "" or v == []):
                    self._diag("K002", f"missing required field '{fname}' ({t.name})", key=key, path=ro.path,
                               line=ro.line, field=fname, type=t.qname)
            for fname, v in ro.fields.items():
                fd = fdefs.get(fname)
                if fd is None:
                    if t.strict:
                        self._diag("K009", f"unknown field '{fname}' for type {t.name}", key=key, path=ro.path,
                                   line=ro.line)
                    # untyped fields may still carry links: index them as untyped, non-propagating relations
                    for item in (v if isinstance(v, list) else [v]):
                        if P.is_link(item):
                            edges.append(self._edge(key, fname, item, fname, "field", False, False))
                    continue
                items = v if isinstance(v, list) else [v]
                if not fd.many and isinstance(v, list):
                    self._diag("K004", f"field '{fname}' takes one value, got a list", key=key, path=ro.path,
                               line=ro.line)
                if fd.many and (fd.min is not None and len(items) < int(fd.min) and v not in (None, [])):
                    self._diag("K008", f"field '{fname}' needs at least {fd.min} item(s), has {len(items)}",
                               key=key, path=ro.path, line=ro.line)
                if fd.many and fd.max is not None and len(items) > int(fd.max):
                    self._diag("K008", f"field '{fname}' allows at most {fd.max} item(s), has {len(items)}",
                               key=key, path=ro.path, line=ro.line)
                for item in items:
                    if item is None:
                        continue
                    if fd.is_ref:
                        if isinstance(item, dict):        # embedded record: its own object (see `contains`)
                            continue
                        edges.append(self._edge(key, fname, item, fname, "field", fd.propagates, fd.inverse))
                    else:
                        err = check_scalar(fd, item)
                        if err:
                            self._diag("K003" if fd.type == "enum" else "K004", f"field '{fname}': {err}", key=key,
                                       path=ro.path, line=ro.line, field=fname, expected=fd.values or fd.type,
                                       received=item)
            if ro.parent:
                edges.append({"src": f"{ro.repo}::{ro.parent}", "rel": "contains", "dst": key, "raw": ro.local_key,
                              "field": "", "kind": "contains", "propagates": False})
            for link in P.body_links(ro.body if not ro.sub else ""):
                edges.append(self._edge(key, "mentions", f"[[{link.raw}]]", "", "body", False, False))
        # 3. resolve every reference
        for e in edges:
            if e["kind"] == "contains":
                continue
            owner = e.pop("_owner")
            repo, ro = rawmap[owner]
            link = P.Link.parse(e["raw"])
            target_key, why = self._resolve(link, repo, ro)
            fd = self.ts.all_fields(tdefs[owner]).get(e["field"]) if e["field"] else None
            if target_key is None or target_key not in objs:
                hint = why or f"no object '{link.raw}'"
                code = "K023" if why.startswith("not a dependency") else "K005"
                sev_code = code if e["kind"] == "field" else "K005"
                d = {"code": sev_code, "severity": SEVERITY[sev_code] if e["kind"] == "field" else "warning",
                     "key": owner, "path": ro.path, "line": ro.line,
                     "message": f"broken link [[{link.raw}]] in '{e['field'] or 'body'}': {hint}",
                     "detail": {"field": e["field"], "target": link.raw}}
                self.diags.append(d)
                e["dst"] = None
            else:
                e["dst"] = target_key
                if fd is not None and fd.is_ref and fd.type != "object":
                    tt = tdefs[target_key]
                    if tt.name == "passage" and objs[target_key]["parent"] in tdefs:
                        tt = tdefs[objs[target_key]["parent"]]      # a passage of a source counts as that source
                        if fd.type == "passage":
                            tt = tdefs[target_key]
                    if not self.ts.is_a(tt, fd.type, tdefs[owner].namespace) and \
                            not self.ts.is_a(tt, fd.type, repo.name):
                        self._diag("K006", f"INVALID REFERENCE in '{e['field']}': expected {fd.type}, "
                                           f"received {tt.name} ([[{link.raw}]])", key=owner, path=ro.path,
                                   line=ro.line, field=e["field"], expected=fd.type, received=tt.name,
                                   target=target_key)
            if e.pop("_inverse", False) and e["dst"]:
                e["src"], e["dst"] = e["dst"], e["src"]
                e["kind"] = "inverse"           # declared on the target; stored as dependent → dependency
                e["rel"] = e["field"] + "⁻¹"
            e.pop("_owner", None)
        for e in edges:
            e.pop("_inverse", None)
            e.pop("_owner", None)
        self._cycles(edges, tdefs, rawmap)
        return objs, edges

    def _field_of_parent(self, parent: tuple[Repo, P.RawObject], sub: str) -> FieldDef | None:
        repo, pro = parent
        pt, _ = self.ts.resolve(pro.type, repo.name)
        if pt is None:
            return None
        fdefs = self.ts.all_fields(pt)
        for fname, v in pro.fields.items():
            if isinstance(v, list) and any(isinstance(x, dict) and str(x.get("id")) == sub for x in v):
                return fdefs.get(fname)
        return None

    @staticmethod
    def _edge(owner: str, rel: str, raw: Any, fname: str, kind: str, propagates: bool, inverse: bool) -> dict:
        text = str(raw).strip()
        if not (text.startswith("[[") and text.endswith("]]")):
            text = f"[[{text}]]"         # bare ids are accepted in reference-typed fields
        return {"src": owner, "rel": rel, "dst": None, "raw": text[2:-2], "field": fname, "kind": kind,
                "propagates": propagates, "_inverse": inverse, "_owner": owner}

    def _resolve(self, link: P.Link, repo: Repo, ro: P.RawObject) -> tuple[str | None, str]:
        if not link.id:                                  # [[^g1]] → this object's embedded record
            return f"{repo.name}::{ro.id}^{link.sub}", ""
        local = f"{link.id}^{link.sub}" if link.sub else link.id
        if link.repo and link.repo != repo.name:
            visible = self.ws.visible(repo)
            if link.repo not in visible:
                return None, f"not a dependency of {repo.name}: add '{link.repo}' to repo.yaml"
            return f"{link.repo}::{local}", ""
        return f"{repo.name}::{local}", ""

    def _cycles(self, edges: list[dict], tdefs: dict[str, TypeDef], rawmap) -> None:
        acyclic: dict[str, list[tuple[str, str]]] = {}
        for e in edges:
            if not e["dst"] or not e["field"] or e["src"] not in tdefs:
                continue
            fd = self.ts.all_fields(tdefs[e["src"]]).get(e["field"])
            if fd is not None and fd.acyclic:
                acyclic.setdefault(e["field"], []).append((e["src"], e["dst"]))
        for fname, pairs in acyclic.items():
            graph: dict[str, list[str]] = {}
            for a, b in pairs:
                graph.setdefault(a, []).append(b)
            state: dict[str, int] = {}

            def visit(n: str, stack: list[str]) -> None:
                state[n] = 1
                for m in graph.get(n, []):
                    if state.get(m) == 1:
                        cyc = stack[stack.index(m):] + [m] if m in stack else [n, m]
                        _, ro = rawmap[n]
                        self._diag("K010", f"cycle through '{fname}': " + " → ".join(cyc), key=n, path=ro.path,
                                   line=ro.line, cycle=cyc)
                    elif state.get(m) is None:
                        visit(m, stack + [m])
                state[n] = 2

            for n in list(graph):
                if state.get(n) is None:
                    visit(n, [n])

    # ── semantic rules (deterministic) ──────────────────────────────────────
    def _semantics(self, objs: dict[str, dict], edges: list[dict]) -> list[dict]:
        out_e: dict[str, list[dict]] = {}
        in_e: dict[str, list[dict]] = {}
        for e in edges:
            out_e.setdefault(e["src"], []).append(e)
            if e["dst"]:
                in_e.setdefault(e["dst"], []).append(e)
        isa = lambda k, name: self._isa(objs[k], name)
        now = today()

        for k, o in objs.items():
            f = o["fields"]
            # K011 orphans (top-level typed objects only)
            if not o["sub"] and o["type_name"] not in ("document", "project", "session", "skill") and o["source"] == "workspace":
                links = [e for e in out_e.get(k, []) + in_e.get(k, []) if e["kind"] != "contains"]
                if not links:
                    self._diag("K011", f"orphaned {o['type_name']}: nothing links to or from it", key=k, path=o["path"])
            # K012 staleness
            for fld in ("review_after", "expires_after"):
                d = as_date(f.get(fld))
                if d and d < now and o["status"] not in CLOSED:
                    self._diag("K012", f"{fld} {d} has passed", key=k, path=o["path"], field=fld)
            t = self.ts.types.get(o["type"])
            if t and t.review_after_days and not f.get("review_after") and o["status"] not in CLOSED | INVALIDATING:
                age = (now - dt.date.fromisoformat(o["updated"])).days
                if age > t.review_after_days:
                    self._diag("K012", f"not reviewed for {age} days (type horizon {t.review_after_days})", key=k,
                               path=o["path"])
            # K014 accepted decision without evidence
            if isa(k, "decision") and o["status"] in ("accepted", "proposed") and not any(
                    e["field"] in ("evidence", "grounds") and e["dst"] for e in out_e.get(k, [])) and \
                    not any(e["kind"] == "contains" for e in out_e.get(k, [])):
                self._diag("K014", "decision cites no evidence", key=k, path=o["path"])
            # K021 evidence without origin
            if isa(k, "evidence") and not o["sub"] and not any(f.get(x) for x in
                                                               ("source", "location", "url", "produced_by",
                                                                "artifact", "sources", "run")):
                self._diag("K021", "evidence has no traceable origin (source, location, url or produced_by)", key=k,
                           path=o["path"])
            # K017 captured source changed
            if isa(k, "source") and f.get("sha256") and f.get("location"):
                p = Path(o["path"]).parent / str(f["location"])
                if p.is_file():
                    cur = hashlib.sha256(p.read_bytes()).hexdigest()
                    if cur != str(f["sha256"]).removeprefix("sha256:"):
                        self._diag("K017", f"source file {f['location']} changed since capture", key=k,
                                   path=o["path"], current=cur)

        # claims / assumptions: support and contradiction from groundings
        stances = self.stances(objs, edges)
        for k, s in stances.items():
            o = objs[k]
            if isa(k, "claim") and not s["supports"] and o["status"] not in INVALIDATING:
                self._diag("K015", "unverified claim: no grounding supports it", key=k, path=o["path"])
            if s["contradicts"] and s["supports"] and not o["fields"].get("resolution"):
                self._diag("K016", f"contradicted by {', '.join(s['contradicts'])} while supported by "
                                   f"{', '.join(s['supports'])}; record a resolution", key=k, path=o["path"],
                           supports=s["supports"], contradicts=s["contradicts"])

        return self._reconsiderations(objs, edges, in_e, out_e, stances)

    def _isa(self, o: dict, name: str) -> bool:
        t = self.ts.types.get(o["type"])
        return bool(t) and any(a.name == name for a in self.ts.lineage(t))

    def stances(self, objs: dict[str, dict], edges: list[dict]) -> dict[str, dict]:
        """For every claim/assumption: which groundings support / contradict it.
        A grounding is any object with a `stance` and a target (`claim`, or the embedding parent)."""
        out: dict[str, dict] = {}
        by_src: dict[str, list[dict]] = {}
        for e in edges:
            by_src.setdefault(e["src"], []).append(e)
        for k, o in objs.items():
            stance = o["fields"].get("stance")
            if not stance:
                continue
            targets = [e["dst"] for e in by_src.get(k, []) if e["field"] in ("claim", "assumption", "target")
                       and e["dst"]]
            if not targets and o["parent"] and (self._isa(objs[o["parent"]], "claim") or
                                                self._isa(objs[o["parent"]], "assumption")):
                targets = [o["parent"]]
            for t in targets:
                if t not in objs:
                    continue
                s = out.setdefault(t, {"supports": [], "contradicts": [], "other": [], "latest_contra": ""})
                if stance == "supports":
                    s["supports"].append(k)
                elif stance == "contradicts":
                    s["contradicts"].append(k)
                    s["latest_contra"] = max(s["latest_contra"], o["updated"])
                else:
                    s["other"].append(k)
        for k, o in objs.items():
            if self._isa(o, "claim") or self._isa(o, "assumption"):
                out.setdefault(k, {"supports": [], "contradicts": [], "other": [], "latest_contra": ""})
        # plain evidence lists count too: supported_by / contradicted_by
        for e in edges:
            if e["src"] in out and e["dst"] in objs and e["field"] in ("supported_by", "contradicted_by"):
                s = out[e["src"]]
                if e["field"] == "supported_by":
                    s["supports"].append(e["dst"])
                else:
                    s["contradicts"].append(e["dst"])
                    s["latest_contra"] = max(s["latest_contra"], objs[e["dst"]]["updated"])
        return out

    def _reconsiderations(self, objs, edges, in_e, out_e, stances) -> list[dict]:
        """Triggers → dependents (transitive over propagating edges) → review queue entries.
        Never edits anything: a `review` object that names the item and the trigger closes it."""
        triggers: list[tuple[str, str, str]] = []           # (trigger key, reason, since)
        for k, o in objs.items():
            if o["status"] in INVALIDATING and not o["sub"]:
                triggers.append((k, f"{o['type_name']} is {o['status']}", o["updated"]))
        for k, s in stances.items():
            if s["contradicts"] and objs[k]["status"] not in INVALIDATING:
                if s["latest_contra"] >= objs[k]["updated"] or not s["supports"]:
                    triggers.append((k, f"new contradicting evidence ({', '.join(s['contradicts'])})",
                                     s["latest_contra"]))
        # review_when: a named future condition now exists and is newer than the object
        for e in edges:
            if e["field"] == "review_when" and e["dst"] and e["dst"] in objs:
                t = objs[e["dst"]]
                met = t["status"] in ("answered", "resolved", "completed", "accepted") if t["type_name"] in (
                    "question", "task", "decision") else True
                if met and t["updated"] >= objs[e["src"]]["updated"]:
                    triggers.append((e["dst"], f"review condition met: {objs[e['dst']]['title']}",
                                     objs[e["dst"]]["updated"]))
        # refresh_when: an `event` of that kind happened after the object last changed
        events = [o for o in objs.values() if o["type_name"] == "event"]
        for k, o in objs.items():
            for kind in o["fields"].get("refresh_when") or []:
                for ev in events:
                    if ev["fields"].get("kind") == kind and ev["updated"] >= o["updated"]:
                        triggers.append((ev["key"], f"event {kind}", ev["updated"]))
                        in_e.setdefault(ev["key"], []).append({"src": k, "dst": ev["key"], "rel": "refresh_when",
                                                               "propagates": True, "kind": "field"})
        # who reviewed what (closing entries)
        reviews: dict[tuple[str, str], str] = {}
        for k, o in objs.items():
            if o["type_name"] != "review":
                continue
            subj = [e["dst"] for e in out_e.get(k, []) if e["field"] == "reviews" and e["dst"]]
            trig = [e["dst"] for e in out_e.get(k, []) if e["field"] == "trigger" and e["dst"]]
            for s in subj:
                for t in trig or ["*"]:
                    reviews[(s, t)] = k

        out: dict[tuple[str, str], dict] = {}
        for trig, reason, since in triggers:
            frontier, seen = [(trig, [trig])], {trig}
            while frontier:
                node, chain = frontier.pop(0)
                if len(chain) > 13:
                    continue
                for e in in_e.get(node, []):
                    src = e["src"]
                    if not e.get("propagates") or e.get("kind") == "contains" or src in seen or src not in objs:
                        continue
                    seen.add(src)
                    nchain = chain + [e["rel"], src]
                    o = objs[src]
                    base = o["parent"] if o["sub"] and o["parent"] in objs else src
                    if o["status"] in CLOSED - {"completed", "done"} and o["type_name"] != "task":
                        continue
                    if any(self._isa(objs[base], t) for t in RECONSIDERABLE) and objs[base]["type_name"] != "review":
                        rid = (base, trig)
                        if rid not in out:
                            dependents = len(in_e.get(base, []))
                            prio = "high" if self._isa(objs[base], "decision") or dependents >= 3 else "normal"
                            if self._isa(objs[base], "deployment") or self._isa(objs[base], "commitment"):
                                prio = "high"
                            out[rid] = {"key": base, "trigger": trig, "reason": reason, "chain": nchain,
                                        "since": since, "priority": prio,
                                        "resolved_by": reviews.get((base, trig)) or reviews.get((base, "*"))}
                    frontier.append((src, nchain))
        for r in out.values():
            if not r["resolved_by"]:
                o = objs[r["key"]]
                self._diag("K013", f"RECONSIDERATION REQUIRED: {r['reason']} (via {' → '.join(r['chain'][::2][:4])})",
                           key=r["key"], path=o["path"], trigger=r["trigger"])
        return list(out.values())


# ── observability (no-op if prometheus_client is unavailable) ─────────────────
try:
    from prometheus_client import Gauge, Histogram
    _COMPILE = Histogram("lif_knowledge_compile_seconds", "Knowledge graph compile duration",
                         buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30))
    _DIAG = Gauge("lif_knowledge_diagnostics", "Knowledge diagnostics by severity", ["severity"])
    _OBJ = Gauge("lif_knowledge_objects", "Objects in the compiled knowledge graph")
except Exception:                       # pragma: no cover
    _COMPILE = _DIAG = _OBJ = None


def _observe(stats: dict) -> None:
    if _COMPILE is None:
        return
    _COMPILE.observe(stats.get("seconds", 0))
    _OBJ.set(stats.get("objects", 0))
    for s, n in (stats.get("diagnostics") or {}).items():
        _DIAG.labels(s).set(n)


def build(root: str | Path | None = None, index: str | Path | None = None, full: bool = False) -> tuple[Workspace, Store, dict]:
    """Open the workspace, compile into its index, return everything."""
    ws = Workspace.open(root)
    store = Store(index if index is not None else ws.index_path)
    stats = Compiler(ws, store).compile(full=full)
    return ws, store, stats
