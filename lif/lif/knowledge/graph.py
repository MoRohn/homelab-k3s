"""Graph queries over the compiled index. Agents use these (via MCP/API/CLI); none of them need SQL.

    get(key)                     object + outgoing and incoming typed edges + diagnostics
    why(key)                     decision lineage: evidence, assumptions (and their state), alternatives,
                                 supersession chain, dependents, open reconsiderations
    impact(key)                  change impact: dependents grouped by type
    trace_claim(key)             claim → groundings → evidence/source → passage text
    find(**filters)              structured query (type, status, field ops, depends_on, dependent_of, …)
    health()                     knowledge health metrics
    diff(old, new)               semantic diff between two compiled graphs
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from lif.knowledge.store import Store


def _brief(o: dict | None) -> dict | None:
    if o is None:
        return None
    return {"key": o["key"], "type": o["type_name"], "title": o["title"], "status": o["status"],
            "updated": o["updated"], "repo": o["repo"]}


class Graph:
    def __init__(self, store: Store):
        self.s = store

    def key(self, ref: str, repo: str | None = None) -> str:
        ref = ref.strip()
        if ref.startswith("[[") and ref.endswith("]]"):
            ref = ref[2:-2]
        k = self.s.find_key(ref, repo)
        if k is None:
            raise KeyError(f"no object '{ref}'")
        return k

    # ── single objects ───────────────────────────────────────────────────────
    def get(self, ref: str, repo: str | None = None) -> dict:
        k = self.key(ref, repo)
        o = self.s.get(k)
        out = dict(o)
        out["provenance"] = self.provenance(o)
        out["links_out"] = [{"rel": e["rel"], "field": e["field"], "target": e["dst"] or e["raw"],
                             "resolved": bool(e["dst"]), "declared_by_target": e["kind"] == "inverse",
                             **({"brief": _brief(self.s.get(e["dst"]))} if e["dst"] else {})}
                            for e in self.s.edges_from(k) if e["kind"] != "contains"]
        out["links_in"] = [{"rel": e["rel"], "field": e["field"], "source": e["src"],
                            "brief": _brief(self.s.get(e["src"]))} for e in self.s.edges_to(k)
                           if e["kind"] != "contains"]
        out["embedded"] = [_brief(self.s.get(e["dst"])) | {"fields": self.s.get(e["dst"])["fields"]}
                           for e in self.s.edges_from(k) if e["kind"] == "contains" and e["dst"] and self.s.get(e["dst"])]
        out["diagnostics"] = self.s.diagnostics(key=k)
        return out

    def provenance(self, o: dict) -> dict:
        sources = [e["dst"] for e in self.s.edges_from(o["key"])
                   if e["dst"] and e["field"] in ("evidence", "source", "supported_by", "passage", "extracted_from",
                                                  "sources", "grounds")]
        return {"key": o["key"], "repo": o["repo"], "repo_version": o["repo_version"], "path": o["path"],
                "line": o["line"], "updated": o["updated"], "updated_by": o["updated_by"], "project": o["project"],
                "sources": sources}

    # ── lineage / why ────────────────────────────────────────────────────────
    def why(self, ref: str) -> dict:
        k = self.key(ref)
        o = self.s.get(k)
        f = o["fields"]
        out_e = self.s.edges_from(k)

        def refs(field: str) -> list[dict]:
            res = []
            for e in out_e:
                if e["field"] == field:
                    t = self.s.get(e["dst"]) if e["dst"] else None
                    item = _brief(t) or {"key": e["raw"], "missing": True}
                    if t and t["type_name"] in ("assumption", "claim"):
                        item["support"] = self.stance_summary(t["key"])
                    if t and t["type_name"] == "passage":
                        item["text"] = t["fields"].get("text")
                    res.append(item)
            return res

        chain, cur, seen = [], o, set()
        while cur and cur["key"] not in seen:          # supersession chain (older decisions)
            seen.add(cur["key"])
            sup = [e["dst"] for e in self.s.edges_from(cur["key"]) if e["field"] == "supersedes" and e["dst"]]
            cur = self.s.get(sup[0]) if sup else None
            if cur:
                chain.append(_brief(cur))
        superseded_by = [_brief(self.s.get(e["src"])) for e in self.s.edges_to(k) if e["field"] == "supersedes"]
        grounds = [self.s.get(e["dst"]) for e in out_e if e["kind"] == "contains" and e["dst"]]
        tasks = [_brief(self.s.get(e["src"])) for e in self.s.edges_to(k) if e["field"] in ("implements", "because")]
        recon = [r for r in self.s.reconsiderations(open_only=False) if r["key"] == k]
        return {"object": _brief(o), "question": f.get("question"), "selected": f.get("selected"),
                "rationale": f.get("rationale") or o["body"].strip()[:1200],
                "alternatives": f.get("alternatives") or [], "date": f.get("date"), "deciders": f.get("deciders"),
                "evidence": refs("evidence") + refs("supported_by"),
                "grounds": [{"id": g["sub"], **{x: g["fields"].get(x) for x in ("stance", "basis", "source")}}
                            for g in grounds if g],
                "assumptions": refs("assumptions"), "affects": [_brief(self.s.get(e["src"])) for e in
                                                                self.s.edges_to(k) if e["field"] == "affects"],
                "implemented_by": tasks, "supersedes": chain, "superseded_by": superseded_by,
                "dependents": self.impact(k)["by_type"], "reconsiderations": recon,
                "provenance": self.provenance(o)}

    def lineage(self, ref: str, depth: int = 4) -> dict:
        """Upstream (what this rests on) and downstream (what rests on this) as a tree-friendly edge list."""
        k = self.key(ref)
        up = self.s.walk(k, "dependencies", depth, propagating=False)
        down = self.s.walk(k, "dependents", depth, propagating=True)
        nodes = {k: _brief(self.s.get(k))}
        for r in up + down:
            nodes[r["node"]] = _brief(self.s.get(r["node"]))
        edges = set()
        for r in up + down:
            p = r["path"]
            for i in range(0, len(p) - 2, 2):
                edges.add((p[i], p[i + 1], p[i + 2]))
        return {"root": k, "nodes": list(nodes.values()),
                "edges": [{"from": a, "rel": r, "to": b} for a, r, b in sorted(edges)],
                "upstream": [r["node"] for r in up], "downstream": [r["node"] for r in down]}

    def impact(self, ref: str, depth: int = 8) -> dict:
        k = self.key(ref)
        deps = self.s.walk(k, "dependents", depth, propagating=True)
        by_type: dict[str, list[dict]] = {}
        for d in deps:
            o = self.s.get(d["node"])
            if o is None:
                continue
            by_type.setdefault(o["type_name"], []).append(_brief(o) | {"depth": d["depth"], "via": d["path"]})
        return {"changed": _brief(self.s.get(k)), "total": len(deps), "by_type": by_type}

    def dependencies(self, ref: str, depth: int = 1) -> list[dict]:
        k = self.key(ref)
        return [_brief(self.s.get(r["node"])) | {"depth": r["depth"], "via": r["path"]}
                for r in self.s.walk(k, "dependencies", depth, propagating=False)]

    def dependents(self, ref: str, depth: int = 1) -> list[dict]:
        k = self.key(ref)
        return [_brief(self.s.get(r["node"])) | {"depth": r["depth"], "via": r["path"]}
                for r in self.s.walk(k, "dependents", depth, propagating=False)]

    # ── evidence ─────────────────────────────────────────────────────────────
    def stance_summary(self, key: str) -> dict:
        sup, con, oth = [], [], []
        for e in self.s.edges_to(key):
            src = self.s.get(e["src"])
            if src and e["field"] in ("claim", "assumption", "target") and src["fields"].get("stance"):
                {"supports": sup, "contradicts": con}.get(src["fields"]["stance"], oth).append(src["key"])
        for e in self.s.edges_from(key):
            if e["kind"] == "contains" and e["dst"]:
                g = self.s.get(e["dst"])
                if g and g["fields"].get("stance"):
                    {"supports": sup, "contradicts": con}.get(g["fields"]["stance"], oth).append(g["key"])
            if e["field"] == "supported_by" and e["dst"]:
                sup.append(e["dst"])
            if e["field"] == "contradicted_by" and e["dst"]:
                con.append(e["dst"])
        return {"supports": sup, "contradicts": con, "other": oth}

    def trace_claim(self, ref: str) -> dict:
        """Claim → every grounding → the evidence object → the exact passage. Inspectable, not a verdict."""
        k = self.key(ref)
        o = self.s.get(k)
        st = self.stance_summary(k)
        rows = []
        for stance, keys in (("supports", st["supports"]), ("contradicts", st["contradicts"]), ("other", st["other"])):
            for gk in keys:
                g = self.s.get(gk)
                if g is None:
                    continue
                if g["fields"].get("stance"):           # a grounding: follow its source/passage
                    src_key = next((e["dst"] for e in self.s.edges_from(gk) if e["field"] == "source"), None)
                    pas_key = next((e["dst"] for e in self.s.edges_from(gk) if e["field"] == "passage"), None)
                    if src_key and "^" in src_key and not pas_key:
                        pas_key = src_key
                    stance_v, basis = g["fields"].get("stance"), g["fields"].get("basis")
                else:                                     # evidence cited directly
                    src_key, pas_key, stance_v, basis = gk, None, stance, g["fields"].get("basis")
                src = self.s.get(src_key) if src_key else None
                if src and src["sub"] and src["type_name"] == "passage":
                    pas_key, src = src["key"], self.s.get(src["parent"])
                pas = self.s.get(pas_key) if pas_key else None
                rows.append({"grounding": gk, "stance": stance_v, "basis": basis, "evidence": _brief(src),
                             "evidence_quality": (src or {}).get("fields", {}).get("quality"),
                             "dataset": (src or {}).get("fields", {}).get("dataset"),
                             "location": (src or {}).get("fields", {}).get("location") or
                                         (src or {}).get("fields", {}).get("url"),
                             "passage": {"key": pas["key"], "text": pas["fields"].get("text"), "path": pas["path"],
                                         "line": pas["line"]} if pas else None})
        extracted = next((e["dst"] for e in self.s.edges_from(k) if e["field"] == "extracted_from" and e["dst"]), None)
        datasets = [r["dataset"] for r in rows if r["dataset"]]
        return {"claim": _brief(o), "statement": o["fields"].get("statement"), "groundings": rows,
                "extracted_from": (lambda p: {"key": p["key"], "text": p["fields"].get("text")} if p else None)(
                    self.s.get(extracted) if extracted else None),
                "independence_warning": len(datasets) != len(set(datasets)),
                "verdict": "the graph records evidence; it does not decide truth",
                "open_questions": [_brief(self.s.get(e["src"])) for e in self.s.edges_to(k)
                                   if (self.s.get(e["src"]) or {}).get("type_name") == "question"]}

    def decision_history(self, ref: str) -> dict:
        """A decision with its supersession chain, reviews, and change records over time."""
        k = self.key(ref)
        w = self.why(k)
        reviews = [_brief(self.s.get(e["src"])) | {"outcome": self.s.get(e["src"])["fields"].get("outcome")}
                   for e in self.s.edges_to(k) if e["field"] == "reviews"]
        changes = [_brief(self.s.get(e["src"])) for e in self.s.edges_to(k) if e["field"] in ("subject", "because")]
        return {"decision": w["object"], "supersedes": w["supersedes"], "superseded_by": w["superseded_by"],
                "reviews": reviews, "changes": changes, "reconsiderations": w["reconsiderations"]}

    # ── structured query ─────────────────────────────────────────────────────
    OPS = re.compile(r"^(?P<field>[\w.-]+)\s*(?P<op>>=|<=|!=|=|>|<|~)\s*(?P<val>.+)$")

    def find(self, type: str | None = None, status: str | None = None, repo: str | None = None,
             project: str | None = None, where: list[str] | None = None, depends_on: str | None = None,
             dependent_of: str | None = None, links_to: str | None = None, linked_from: str | None = None,
             older_than_days: int | None = None, text: str | None = None, include_embedded: bool = False,
             limit: int = 200) -> list[dict]:
        """Examples:
            find(type="decision", depends_on="single-node-primary")       decisions resting on an assumption
            find(type="incident", status="open", links_to="gateway")      unresolved incidents for a service
            find(type="claim", depends_on="k3s-docs")                     claims supported by a source
            find(type="model", where=["state=production"], older_than_days=90)"""
        sql, args = ["1=1"], []
        if not include_embedded:
            sql.append("sub IS NULL")
        if type:
            types = [t.strip() for t in type.split(",")]
            names = self._subtypes(types)          # a query for `evidence` also returns benchmarks, sources…
            sql.append("type_name IN (%s)" % ",".join("?" * len(names)))
            args += sorted(names)
        if status:
            st = [x.strip() for x in status.split(",")]
            neg = [x[1:] for x in st if x.startswith("!")]
            pos = [x for x in st if not x.startswith("!")]
            if pos:
                sql.append("status IN (%s)" % ",".join("?" * len(pos)))
                args += pos
            if neg:
                sql.append("status NOT IN (%s)" % ",".join("?" * len(neg)))
                args += neg
        if repo:
            sql.append("repo=?")
            args.append(repo)
        if project:
            sql.append("project=?")
            args.append(project)
        if older_than_days is not None:
            sql.append("updated < ?")
            args.append((dt.date.today() - dt.timedelta(days=int(older_than_days))).isoformat())
        rows = self.s.objects(" AND ".join(sql), tuple(args), limit=100000)
        keep: set[str] | None = None

        def restrict(keys: set[str]) -> None:
            nonlocal keep
            keep = keys if keep is None else keep & keys

        if depends_on:
            restrict({r["node"] for r in self.s.walk(self.key(depends_on), "dependents", 8, propagating=False)})
        if dependent_of:
            restrict({r["node"] for r in self.s.walk(self.key(dependent_of), "dependencies", 8, propagating=False)})
        if links_to:
            k = self.key(links_to)
            restrict({self._top(e["src"]) for e in self.s.edges_to(k)})
        if linked_from:
            k = self.key(linked_from)
            restrict({e["dst"] for e in self.s.edges_from(k) if e["dst"]})
        if text:
            from lif.knowledge.search import fts_query
            restrict({self._top(r["key"]) for r in self.s.fts(fts_query(text), 500)})
        out = []
        for r in rows:
            if keep is not None and r["key"] not in keep:
                continue
            if where and not all(self._match(r, w) for w in where):
                continue
            out.append(_brief(r) | {"fields": r["fields"]})
            if len(out) >= limit:
                break
        return out

    def _top(self, key: str) -> str:
        return key.split("^", 1)[0]

    def _subtypes(self, names: list[str]) -> set[str]:
        rows = self.s.db.q("SELECT qname, name, extends FROM types")
        out, changed = set(names), True
        while changed:
            changed = False
            for r in rows:
                ext = (r["extends"] or "").split("::")[-1]
                if ext in out and r["name"] not in out:
                    out.add(r["name"])
                    changed = True
        return out

    def _match(self, o: dict, cond: str) -> bool:
        m = self.OPS.match(cond.strip())
        if not m:
            raise ValueError(f"bad condition '{cond}' (use field=value, field>value, field~text)")
        f, op, val = m.group("field"), m.group("op"), m.group("val").strip()
        cur: Any = o["fields"]
        for part in f.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if f in ("status", "type", "title", "updated", "repo"):
            cur = o.get({"type": "type_name"}.get(f, f))
        if op == "~":
            return val.lower() in json.dumps(cur, default=str).lower()
        if cur is None:
            return op == "!="
        a, b = (str(cur), val)
        try:
            a, b = float(cur), float(val)            # numeric if both are numbers
        except (TypeError, ValueError):
            pass
        return {"=": a == b, "!=": a != b, ">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]

    # ── health ───────────────────────────────────────────────────────────────
    def health(self) -> dict:
        c = self.s.counts()
        q = lambda sql, a=(): self.s.db.one(sql, a)["n"]
        refs = c["references"] or 1
        decisions = q("SELECT count(*) n FROM objects WHERE type_name='decision' AND sub IS NULL")
        traced = q("SELECT count(DISTINCT e.src) n FROM edges e JOIN objects o ON o.key=e.src "
                   "WHERE o.type_name='decision' AND e.field='evidence' AND e.dst IS NOT NULL")
        claims = q("SELECT count(*) n FROM objects WHERE type_name IN ('claim','assumption') AND sub IS NULL")
        uncovered = q("SELECT count(*) n FROM diagnostics WHERE code='K015'")
        sessions = self.s.objects("type_name='session' AND sub IS NULL")
        resumed = [s for s in sessions if s["fields"].get("resumed_from")]
        recovered = [s for s in resumed if s["fields"].get("recovered") is True]
        return {
            "objects": c["objects"], "embedded": c["embedded"], "references": c["references"],
            "valid_reference_rate": round(1 - c["broken_references"] / refs, 4),
            "broken_references": c["broken_references"],
            "missing_fields": q("SELECT count(*) n FROM diagnostics WHERE code='K002'"),
            "errors": c["errors"], "warnings": c["warnings"],
            "stale": q("SELECT count(*) n FROM diagnostics WHERE code='K012'"),
            "stale_assumptions": q("SELECT count(*) n FROM diagnostics d JOIN objects o ON o.key=d.key "
                                   "WHERE d.code='K012' AND o.type_name='assumption'") +
                                 q("SELECT count(*) n FROM objects WHERE type_name='assumption' "
                                   "AND status IN ('challenged','invalidated')"),
            "decisions": decisions,
            "decisions_needing_review": q("SELECT count(DISTINCT r.key) n FROM reconsiderations r JOIN objects o "
                                          "ON o.key=r.key WHERE o.type_name='decision' AND r.resolved_by IS NULL"),
            "decision_traceability_rate": round(traced / decisions, 4) if decisions else None,
            "evidence_coverage": round(1 - uncovered / claims, 4) if claims else None,
            "orphans": q("SELECT count(*) n FROM diagnostics WHERE code='K011'"),
            "conflicting_claims": q("SELECT count(*) n FROM diagnostics WHERE code='K016'"),
            "open_questions": q("SELECT count(*) n FROM objects WHERE type_name='question' AND status='open'"),
            "open_tasks": q("SELECT count(*) n FROM objects WHERE type_name='task' AND status IN "
                            "('open','in-progress','blocked')"),
            "reconsideration_queue": c["reconsiderations"],
            "sessions": len(sessions),
            "context_recovery_rate": round(len(recovered) / len(resumed), 4) if resumed else None,
            "compiled_at": self.s.meta("compiled_at"), "last_compile": self.s.meta("last_compile"),
        }


def diff(old: Store, new: Store, graph_for_impact: Graph | None = None) -> list[dict]:
    """Semantic knowledge diff: per object, field changes, link changes and downstream impact."""
    o_objs = {o["key"]: o for o in old.objects("sub IS NULL", limit=10 ** 6)}
    n_objs = {o["key"]: o for o in new.objects("sub IS NULL", limit=10 ** 6)}
    g = graph_for_impact or Graph(new)
    out = []

    def links(s: Store, k: str) -> set[tuple[str, str]]:
        return {(e["field"], e["dst"] or e["raw"]) for e in s.edges_from(k) if e["kind"] == "field"}

    for k in sorted(set(o_objs) | set(n_objs)):
        a, b = o_objs.get(k), n_objs.get(k)
        if a and b and a["sha"] == b["sha"] and a["fields"] == b["fields"]:
            continue
        entry: dict[str, Any] = {"key": k, "type": (b or a)["type_name"], "title": (b or a)["title"]}
        if a is None:
            entry["change"] = "added"
        elif b is None:
            entry["change"] = "removed"
        else:
            entry["change"] = "changed"
            fa, fb = a["fields"], b["fields"]
            entry["fields"] = {f: {"from": fa.get(f), "to": fb.get(f)} for f in sorted(set(fa) | set(fb))
                               if fa.get(f) != fb.get(f) and not _is_links(fa.get(f)) and not _is_links(fb.get(f))}
            entry["body_changed"] = a["body"] != b["body"]
        la = links(old, k) if a else set()
        lb = links(new, k) if b else set()
        entry["links_added"] = [{"field": f, "target": t} for f, t in sorted(lb - la)]
        entry["links_removed"] = [{"field": f, "target": t} for f, t in sorted(la - lb)]
        if b:
            imp = g.impact(k)
            entry["impact"] = {t: len(v) for t, v in imp["by_type"].items()}
        out.append(entry)
    return out


def _is_links(v: Any) -> bool:
    items = v if isinstance(v, list) else [v]
    return bool(items) and all(isinstance(x, str) and x.startswith("[[") for x in items)


def render_diff(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        lines.append(f"{e['type'].upper()} {e['change'].upper()}  {e['key']}  {e['title']}")
        for f, ch in (e.get("fields") or {}).items():
            lines.append(f"  {f}: {ch['from']} → {ch['to']}")
        for l in e.get("links_added") or []:
            lines.append(f"  + {l['field']}: {l['target']}")
        for l in e.get("links_removed") or []:
            lines.append(f"  - {l['field']}: {l['target']}")
        if e.get("impact"):
            lines.append("  downstream impact: " + ", ".join(f"{n} {t}" for t, n in e["impact"].items()))
        lines.append("")
    return "\n".join(lines) or "no semantic changes"


def path_between(store: Store, a: str, b: str, depth: int = 6) -> list[str] | None:
    """Shortest undirected path a … b as [node, rel, node, …] (both keys resolved)."""
    if a == b:
        return [a]
    prev: dict[str, tuple[str, str]] = {a: ("", "")}
    frontier = [a]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for e in store.edges_from(n):
                if e["dst"] and e["dst"] not in prev:
                    prev[e["dst"]] = (n, e["rel"])
                    nxt.append(e["dst"])
            for e in store.edges_to(n):
                if e["src"] not in prev:
                    prev[e["src"]] = (n, "←" + e["rel"])
                    nxt.append(e["src"])
        if b in prev:
            out = [b]
            cur = b
            while cur != a:
                p, rel = prev[cur]
                out = [p, rel] + out
                cur = p
            return out
        frontier = nxt
    return None
