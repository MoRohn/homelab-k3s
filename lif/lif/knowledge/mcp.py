"""MCP server (stdio, JSON-RPC 2.0) exposing the knowledge graph to coding agents.

    knowledge --root lif/knowledge mcp --agent claude-code   (see .mcp.json at the repo root)

No SDK dependency: the protocol surface used here is small (initialize, tools/list, tools/call,
resources/list, resources/read, ping). Each tool declares the permission scope it needs; the
actor's scopes come from workspace.yaml `permissions:`. Approval-gated operations (installs,
upgrades, migrations, deletes, publishing) never complete through MCP: they return the exact command
a human must run.

Saved queries in any repo (`queries/*.query.yaml`) are exposed as extra tools named `query_<name>`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from typing import Any

from lif.knowledge.ops import Knowledge, PermissionDenied

PROTOCOL = "2025-06-18"
INSTRUCTIONS = """Persistent project knowledge for this workspace (typed objects: decisions, assumptions,
evidence, tasks, incidents, methods, skills…). Before recommending or changing infrastructure, call
assemble_context with the task (it returns related decisions, incidents, constraints and methods with
provenance). Start a session with resume_session; end significant work with checkpoint_session and
record what changed, why, and any decision, lesson or open question. Never treat a supported claim as
proven: use get_evidence to inspect groundings."""

S = {"type": "string"}
I = {"type": "integer"}
O = {"type": "object"}
A = {"type": "array", "items": {"type": "string"}}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def _find_args(a: dict) -> dict:
    keys = ("type", "status", "repo", "project", "where", "depends_on", "dependent_of", "links_to", "linked_from",
            "older_than_days", "text", "limit")
    return {k: a[k] for k in keys if a.get(k) is not None}


def _review(kn: Knowledge, actor: str, a: dict) -> dict:
    import datetime as dt
    w = kn.writer(actor)
    fields = {"title": a.get("title") or f"Review of {a['object']}", "reviews": [f"[[{a['object']}]]"],
              "outcome": a["outcome"], "reviewer": actor, "date": dt.date.today().isoformat()}
    if a.get("trigger"):
        fields["trigger"] = f"[[{a['trigger']}]]"
    return w.create("review", fields, a.get("notes", ""), repo=a.get("repo"), folder="reviews")


TOOLS: list[dict] = [
    {"name": "search_objects", "scope": "read:knowledge",
     "description": "Hybrid search (full text + graph importance + recency [+ embeddings]) over all objects.",
     "inputSchema": _schema({"query": S, "type": S, "repo": S, "status": S, "project": S, "limit": I}, ["query"]),
     "fn": lambda kn, actor, a: kn.search.search(a["query"], a.get("type"), a.get("repo"), a.get("status"),
                                                 a.get("project"), int(a.get("limit", 15)))},
    {"name": "get_object", "scope": "read:knowledge",
     "description": "One object with fields, body, typed links in/out, embedded records, diagnostics and provenance.",
     "inputSchema": _schema({"key": S}, ["key"]), "fn": lambda kn, actor, a: kn.graph.get(a["key"])},
    {"name": "create_object", "scope": "write:knowledge",
     "description": "Create a typed object (validated; rejected if it would add an error diagnostic). "
                    "References are written as \"[[id]]\" or \"[[package::id]]\".",
     "inputSchema": _schema({"type": S, "fields": O, "body": S, "id": S, "repo": S}, ["type", "fields"]),
     "fn": lambda kn, actor, a: kn.writer(actor).create(a["type"], a["fields"], a.get("body", ""), a.get("id"),
                                                        a.get("repo"))},
    {"name": "update_object", "scope": "write:knowledge",
     "description": "Update fields (set / append to lists / unset) or the body of an object (validated).",
     "inputSchema": _schema({"key": S, "set": O, "append": O, "unset": A, "append_body": S}, ["key"]),
     "fn": lambda kn, actor, a: kn.writer(actor).update(a["key"], a.get("set"), a.get("append"), a.get("unset"),
                                                        append_body=a.get("append_body"))},
    {"name": "link_objects", "scope": "write:knowledge",
     "description": "Add a typed reference: source.field += [[target]] (target type is checked).",
     "inputSchema": _schema({"source": S, "field": S, "target": S}, ["source", "field", "target"]),
     "fn": lambda kn, actor, a: kn.writer(actor).link(a["source"], a["field"], a["target"])},
    {"name": "trace_relationship", "scope": "read:knowledge",
     "description": "Shortest relationship path between two objects (or the full lineage of one if `to` is omitted).",
     "inputSchema": _schema({"from": S, "to": S, "depth": I}, ["from"]),
     "fn": lambda kn, actor, a: ({"path": __import__("lif.knowledge.graph", fromlist=["x"]).path_between(
         kn.store, kn.graph.key(a["from"]), kn.graph.key(a["to"]), int(a.get("depth", 6)))}
         if a.get("to") else kn.graph.lineage(a["from"], int(a.get("depth", 4))))},
    {"name": "get_dependencies", "scope": "read:knowledge",
     "description": "What an object rests on (evidence, assumptions, …), to the given depth.",
     "inputSchema": _schema({"key": S, "depth": I}, ["key"]),
     "fn": lambda kn, actor, a: kn.graph.dependencies(a["key"], int(a.get("depth", 1)))},
    {"name": "get_dependents", "scope": "read:knowledge",
     "description": "What depends on an object. Use change_impact for the transitive blast radius.",
     "inputSchema": _schema({"key": S, "depth": I}, ["key"]),
     "fn": lambda kn, actor, a: kn.graph.dependents(a["key"], int(a.get("depth", 1)))},
    {"name": "change_impact", "scope": "read:knowledge",
     "description": "Blast radius if this object changes: dependent decisions, tasks, deployments… by type.",
     "inputSchema": _schema({"key": S}, ["key"]), "fn": lambda kn, actor, a: kn.graph.impact(a["key"])},
    {"name": "get_evidence", "scope": "read:knowledge",
     "description": "Trace a claim/assumption to its groundings, evidence objects and exact source passages.",
     "inputSchema": _schema({"key": S}, ["key"]), "fn": lambda kn, actor, a: kn.graph.trace_claim(a["key"])},
    {"name": "why_decision", "scope": "read:knowledge",
     "description": "Why a decision was made: question, alternatives, evidence, assumptions (with current status), "
                    "what it affects, implementing work, reconsiderations.",
     "inputSchema": _schema({"key": S}, ["key"]), "fn": lambda kn, actor, a: kn.graph.why(a["key"])},
    {"name": "get_decision_history", "scope": "read:knowledge",
     "description": "Supersession chain, reviews and change records of a decision.",
     "inputSchema": _schema({"key": S}, ["key"]), "fn": lambda kn, actor, a: kn.graph.decision_history(a["key"])},
    {"name": "query_graph", "scope": "read:knowledge",
     "description": "Structured query. e.g. {type: decision, depends_on: single-node-primary} | {type: incident, "
                    "status: open, links_to: gateway} | {type: model, where: [\"state=production\"], older_than_days: 90}",
     "inputSchema": _schema({"type": S, "status": S, "repo": S, "project": S, "where": A, "depends_on": S,
                             "dependent_of": S, "links_to": S, "linked_from": S, "older_than_days": I, "text": S,
                             "limit": I}),
     "fn": lambda kn, actor, a: (kn.store.log_query("find:" + json.dumps(_find_args(a), sort_keys=True), actor),
                                 kn.graph.find(**_find_args(a)))[1]},
    {"name": "run_diagnostics", "scope": "read:knowledge",
     "description": "Knowledge diagnostics (like compiler errors): missing fields, wrong reference types, broken "
                    "links, stale/contradicted assumptions, reconsiderations. Optional filter by severity or object.",
     "inputSchema": _schema({"severity": S, "key": S}),
     "fn": lambda kn, actor, a: kn.store.diagnostics(a.get("severity"), a.get("key"))},
    {"name": "list_reconsiderations", "scope": "read:knowledge",
     "description": "Review queue: objects whose inputs changed (assumption invalidated, contradicting evidence, "
                    "review_when met, refresh event). Nothing is auto-reversed.",
     "inputSchema": _schema({}), "fn": lambda kn, actor, a: kn.store.reconsiderations()},
    {"name": "record_review", "scope": "write:knowledge",
     "description": "Close a reconsideration: record the outcome (reaffirmed | revised | superseded | needs-work).",
     "inputSchema": _schema({"object": S, "trigger": S, "outcome": S, "notes": S, "repo": S}, ["object", "outcome"]),
     "fn": _review},
    {"name": "assemble_context", "scope": "read:knowledge",
     "description": "Budgeted, provenance-carrying context package for a task (decisions, assumptions, incidents, "
                    "methods, constraints, open reviews). Call this before planning infrastructure changes.",
     "inputSchema": _schema({"task": S, "project": S, "profile": S, "budget_tokens": I, "pins": A}, ["task"]),
     "fn": lambda kn, actor, a: kn.context.assemble(a["task"], a.get("project"), a.get("profile"),
                                                    int(a.get("budget_tokens", 2000)), a.get("pins"))},
    {"name": "resume_session", "scope": "read:knowledge",
     "description": "Project status for a fresh session: last checkpoint, changes since, decisions, open tasks and "
                    "questions, changed assumptions, pending reviews, diagnostics.",
     "inputSchema": _schema({"project": S}), "fn": lambda kn, actor, a: kn.context.resume(a.get("project"))},
    {"name": "checkpoint_session", "scope": "write:knowledge",
     "description": "End-of-work capture: summary + links to completed work, decisions, questions, artifacts, "
                    "changed objects and next steps. The next session resumes from this.",
     "inputSchema": _schema({"project": S, "summary": S, "completed": A, "decisions": A, "questions": A,
                             "artifacts": A, "changed": A, "next": A, "recovered": {"type": "boolean"}},
                            ["project", "summary"]),
     "fn": lambda kn, actor, a: kn.context.checkpoint(a["project"], actor, a["summary"], a.get("completed"),
                                                      a.get("decisions"), a.get("questions"), a.get("artifacts"),
                                                      a.get("changed"), a.get("next"), recovered=a.get("recovered"))},
    {"name": "list_skills", "scope": "read:knowledge", "description": "Skills available (project + installed packages).",
     "inputSchema": _schema({}), "fn": lambda kn, actor, a: __import__("lif.knowledge.skills", fromlist=["x"]).list_skills(kn)},
    {"name": "execute_skill", "scope": "execute:skills",
     "description": "Run a skill: validates inputs, runs its deterministic graph checks, returns its instructions.",
     "inputSchema": _schema({"name": S, "inputs": O}, ["name"]),
     "fn": lambda kn, actor, a: __import__("lif.knowledge.skills", fromlist=["x"]).run_skill(kn, a["name"],
                                                                                            a.get("inputs") or {})},
    {"name": "list_views", "scope": "read:knowledge", "description": "Version-controlled views and apps.",
     "inputSchema": _schema({}), "fn": lambda kn, actor, a: {
         "views": [{k: v[k] for k in ("name", "repo", "kind", "title", "params")} for v in
                   __import__("lif.knowledge.views", fromlist=["x"]).list_views(kn)],
         "apps": __import__("lif.knowledge.views", fromlist=["x"]).list_apps(kn)}},
    {"name": "render_view", "scope": "read:knowledge", "description": "Render a view (rows reference graph objects).",
     "inputSchema": _schema({"name": S, "params": O}, ["name"]),
     "fn": lambda kn, actor, a: __import__("lif.knowledge.views", fromlist=["x"]).render(kn, a["name"],
                                                                                        a.get("params") or {})},
    {"name": "knowledge_health", "scope": "read:knowledge", "description": "Knowledge health metrics.",
     "inputSchema": _schema({}), "fn": lambda kn, actor, a: kn.graph.health()},
    {"name": "suggest_improvements", "scope": "read:knowledge",
     "description": "Operations repeated often enough to become saved queries/tools or views (proposals only).",
     "inputSchema": _schema({}), "fn": lambda kn, actor, a: kn.suggest()},
]


def tool_list(kn: Knowledge) -> list[dict]:
    out = [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]
    for q in kn.saved_queries():
        out.append({"name": f"query_{q['name'].replace('-', '_')}", "description": f"Saved query: {q['description']}",
                    "inputSchema": _schema({p: S for p in q["params"]}, list(q["params"]))})
    return out


def call(kn: Knowledge, actor: str, name: str, args: dict) -> Any:
    kn.refresh()
    if name.startswith("query_"):
        q = next((q for q in kn.saved_queries() if f"query_{q['name'].replace('-', '_')}" == name), None)
        if q is None:
            raise KeyError(f"unknown tool {name}")
        kn.require(actor, "read:knowledge", name)
        return kn.run_query(q["name"], args)
    t = next((t for t in TOOLS if t["name"] == name), None)
    if t is None:
        raise KeyError(f"unknown tool {name}")
    kn.require(actor, t["scope"], name, str(args.get("key") or args.get("name") or args.get("type") or ""))
    t0 = time.perf_counter()
    try:
        return t["fn"](kn, actor, args)
    finally:
        _observe(name, time.perf_counter() - t0)


def handle(kn: Knowledge, actor: str, msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:                                  # notification
        return None

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                   "capabilities": {"tools": {"listChanged": False}, "resources": {}},
                   "serverInfo": {"name": "lif-knowledge", "version": "0.1.0"}, "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list(kn)})
    if method == "tools/call":
        try:
            res = call(kn, actor, params.get("name", ""), params.get("arguments") or {})
            text = json.dumps(res, default=str, indent=1)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except PermissionDenied as e:
            return ok({"content": [{"type": "text", "text": f"permission denied: {e}"}], "isError": True})
        except Exception as e:                       # tool errors are results, not protocol errors
            return ok({"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})
    if method == "resources/list":
        return ok({"resources": [{"uri": "knowledge://bootstrap", "name": "Repository bootstrap",
                                  "mimeType": "application/json",
                                  "description": "Project summary, dependencies, types, skills, tools, critical "
                                                 "diagnostics, recent decisions, open work"}]})
    if method == "resources/read":
        if params.get("uri") == "knowledge://bootstrap":
            return ok({"contents": [{"uri": "knowledge://bootstrap", "mimeType": "application/json",
                                     "text": json.dumps(kn.context.bootstrap(), default=str)}]})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown resource"}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(kn: Knowledge, actor: str, stdin=sys.stdin, stdout=sys.stdout) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        try:
            resp = handle(kn, actor, msg)
        except Exception as e:                       # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            resp = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            stdout.write(json.dumps(resp, default=str) + "\n")
            stdout.flush()


try:
    from prometheus_client import Histogram
    _CALLS = Histogram("lif_knowledge_mcp_call_seconds", "MCP tool call latency", ["tool"],
                       buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
except Exception:                       # pragma: no cover
    _CALLS = None


def _observe(tool: str, sec: float) -> None:
    if _CALLS is not None:
        _CALLS.labels(tool).observe(sec)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lif-knowledge-mcp")
    p.add_argument("--root", help="workspace root (default $LIF_KNOWLEDGE_ROOT or lif/knowledge)")
    p.add_argument("--agent", default="agent", help="actor name; scopes come from workspace.yaml permissions")
    a = p.parse_args(argv)
    kn = Knowledge.open(a.root)
    serve(kn, f"agent:{a.agent}" if ":" not in a.agent else a.agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
