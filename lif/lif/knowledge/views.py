"""Version-controlled views and apps over the shared graph (no per-app databases).

views/<name>.view.yaml:
    name: open-decisions
    kind: table            # table | kanban | timeline | lineage | evidence | impact | decision-review | health
    title: Open decisions
    query: {type: decision, status: "proposed,accepted"}     # graph.find() arguments
    columns: [title, status, date, evidence, assumptions]
    group_by: status       # kanban
    date_field: date       # timeline
    params: [object]       # lineage / evidence / impact / decision-review take an object

apps/<name>/app.yaml:
    name: gpu-operations-console
    title: GPU operations console
    views: [gpu-capacity, resource-incidents, open-reconsiderations]

A view's rows carry the graph object keys, so the UI, the agent and every other view read the
same objects.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from lif.knowledge.ops import Knowledge

OBJECT_KINDS = {"lineage", "evidence", "impact", "decision-review"}


def list_views(kn: "Knowledge") -> list[dict]:
    out = []
    for r in kn.ws.all_repos():
        for p in r.glob("views", "*.view.yaml"):
            raw = yaml.safe_load(p.read_text()) or {}
            out.append({"name": raw.get("name", p.name.split(".")[0]), "repo": r.name, "version": r.version,
                        "kind": raw.get("kind", "table"), "title": raw.get("title", ""), "params": raw.get("params", []),
                        "path": str(p), "definition": raw})
    return out


def list_apps(kn: "Knowledge") -> list[dict]:
    out = []
    for r in kn.ws.all_repos():
        for p in r.glob("apps", "*/app.yaml"):
            raw = yaml.safe_load(p.read_text()) or {}
            out.append({"name": raw.get("name", p.parent.name), "repo": r.name, "title": raw.get("title", ""),
                        "description": raw.get("description", ""), "views": raw.get("views", []), "path": str(p)})
    return out


def _view(kn: "Knowledge", name: str) -> dict:
    vs = [v for v in list_views(kn) if v["name"] == name or f"{v['repo']}::{v['name']}" == name]
    if not vs:
        raise KeyError(f"no view '{name}'")
    ws_first = sorted(vs, key=lambda v: kn.ws.repo(v["repo"]).source != "workspace")
    return ws_first[0]


def _cell(kn: "Knowledge", key: str, col: str) -> Any:
    o = kn.store.get(key)
    if col in ("title", "status", "updated", "repo", "key", "updated_by", "project"):
        return o[col]
    if col == "type":
        return o["type_name"]
    links = [e["dst"] or e["raw"] for e in kn.store.edges_from(key) if e["field"] == col and e["kind"] == "field"]
    if links:
        return links
    inbound = col.startswith("<")             # "<implements": objects that point here via that field
    if inbound:
        return [e["src"] for e in kn.store.edges_to(key) if e["field"] == col[1:]]
    return o["fields"].get(col)


def render(kn: "Knowledge", name: str, params: dict | None = None) -> dict:
    kn.refresh()
    v = _view(kn, name)
    d = v["definition"]
    kind = d.get("kind", "table")
    params = params or {}
    out: dict[str, Any] = {"view": v["name"], "repo": v["repo"], "version": v["version"], "kind": kind,
                           "title": d.get("title", v["name"]), "description": d.get("description", "")}
    g = kn.graph
    if kind in OBJECT_KINDS:
        ref = params.get("object") or params.get("key")
        if not ref:
            raise ValueError(f"view {name} needs an `object` parameter")
        if kind == "lineage":
            out["graph"] = g.lineage(ref, int(params.get("depth", 4)))
        elif kind == "evidence":
            out["evidence"] = g.trace_claim(ref)
        elif kind == "impact":
            out["impact"] = g.impact(ref)
        else:
            out["decision"] = g.why(ref)
        return out
    if kind == "health":
        out["health"] = g.health()
        out["reconsiderations"] = kn.store.reconsiderations()[:50]
        return out
    q = dict(d.get("query") or {})
    for k, val in list(q.items()):
        if isinstance(val, str) and val.startswith("{") and val.endswith("}"):
            q[k] = params.get(val[1:-1])
    if d.get("source") == "reconsiderations":
        rows = [{"key": r["key"], "trigger": r["trigger"], "reason": r["reason"], "priority": r["priority"],
                 "since": r["since"], "title": (kn.store.get(r["key"]) or {}).get("title")}
                for r in kn.store.reconsiderations()]
        out.update(columns=["title", "priority", "reason", "trigger", "since"], rows=rows)
        return out
    objs = g.find(**q)
    cols = d.get("columns") or ["title", "type", "status", "updated"]
    rows = [{"key": o["key"], **{c: _cell(kn, o["key"], c) for c in cols}} for o in objs]
    out.update(columns=cols, rows=rows)
    if kind == "kanban":
        gb = d.get("group_by", "status")
        lanes = d.get("lanes") or sorted({str(r.get(gb) or "") for r in rows})
        out["groups"] = {lane: [r["key"] for r in rows if str(r.get(gb) or "") == lane] for lane in lanes}
    if kind == "timeline":
        df = d.get("date_field", "updated")
        for r in rows:
            r["_date"] = str(_cell(kn, r["key"], df) or "")
        rows.sort(key=lambda r: r["_date"], reverse=True)
    if kind == "comparison":
        out["metrics"] = d.get("metrics") or []
    return out
