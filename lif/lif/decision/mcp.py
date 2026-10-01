"""MCP server for Decision Engineering (stdio, JSON-RPC 2.0, no SDK dependency).

    python -m lif.decision.mcp            (registered as `lif-decisions` in the repo's .mcp.json)

Tools let a coding agent choose the right primitive consciously (spec §80, §103) and use
the platform's decisions:

  classify_operation   CODE / JEV / GENERATIVE / HUMAN for an operation you are about to do
  decide               a typed decision through the decision-fabric service (cascade, provenance)
  decision_lint        lint a draft spec (JSON object) or a path in the repo
  decision_registry    versions, stages and what serves each decision name
  agent_audit          mine local agent traces (primary-workload sessions excluded) → audit

`decide` only talks to the decision-fabric service ($LIF_DECISION_URL); this process never
calls Jev or Kimi itself, so privacy policy is enforced in one place.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from typing import Any

PROTOCOL = "2025-06-18"
INSTRUCTIONS = ("Decision Engineering for this platform. Before calling an LLM for a judgment, call "
                "classify_operation: exact results belong in code, bounded judgments in decide(), creation in "
                "generation, irreversible actions with a human. decide() returns `actionable`; never act on a "
                "result whose actionable is false.")
S, O, A, B = {"type": "string"}, {"type": "object"}, {"type": "array", "items": {"type": "string"}}, {"type": "boolean"}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def classify_operation(a: dict) -> dict:
    from lif.decision.mining import classify as C
    labels = a.get("labels") or []
    op = C.Operation(id="mcp", run_id="mcp", seq=0, ts=0, agent="mcp", workflow="", source="mcp",
                     kind="llm_call", name=a.get("description", ""), executor=a.get("executor", "llm"),
                     output_label=labels[0] if len(labels) == 1 else "", reversible=not a.get("irreversible", False),
                     output_tokens=1 if labels else int(a.get("expected_output_tokens", 200)),
                     features={"purpose": a.get("description", ""), "output_type": "label" if labels else
                               a.get("output_type", "text"), "candidates_n": len(labels) or None,
                               "has_image": bool(a.get("needs_image")), "untrusted_input": bool(a.get("untrusted"))})
    C.classify(op)
    from lif.decision.mining.miner import known_methods
    methods = known_methods(a.get("description", ""))
    bucket = C.CODE if methods and op.classification == C.JEV else op.classification
    advice = {C.CODE: "compute it in code" + (f" ({methods[0]})" if methods else ""),
              C.JEV: "use decide() with a versioned decision; reuse agent-core packages first",
              C.GEN: "use generate() (local first)", C.HUMAN: "ask a human / enforce hard policy; never automate",
              C.UNKNOWN: "describe the output: is it one of a known set of labels?"}[bucket]
    return {"bucket": bucket, "advice": advice, "reasons": op.reasons, "weaknesses": op.weaknesses,
            "existing_methods": methods}


async def _decide(a: dict) -> dict:
    from lif.decision.sdk import RemoteIntelligence
    url = os.environ.get("LIF_DECISION_URL")
    if not url:
        return {"error": "LIF_DECISION_URL is not set; decide() runs through the decision-fabric service"}
    d = await RemoteIntelligence(url).decide(a["decision"], a.get("state") or {},
                                             **{k: a[k] for k in ("data_class", "agent", "workflow", "choices")
                                                if a.get(k) is not None})
    return d.__dict__


def decision_lint(a: dict) -> dict:
    from lif.decision.lint import lint, lint_path, summarize
    from lif.decision.types import DecisionDef
    if a.get("path"):
        res = lint_path(a["path"])
        return {ref: {"summary": summarize(fs), "findings": [f.to_dict() for f in fs]} for ref, fs in res.items()}
    spec = dict(a.get("spec") or {})
    acks = spec.pop("lint_ack", []) or []
    try:
        d = DecisionDef.from_raw(spec)
    except (TypeError, ValueError) as e:
        return {"error": f"invalid spec: {e}"}
    fs = lint(d, acks=acks)
    return {"ref": d.ref, "summary": summarize(fs), "findings": [f.to_dict() for f in fs]}


def decision_registry(a: dict) -> dict:
    from lif.decision.registry import Registry
    from lif.decision.types import load_definitions
    reg = Registry(load_definitions())
    out = reg.summary()
    if a.get("name"):
        out = [x for x in out if x["name"] == a["name"]]
    return {"decisions": out, "note": "YAML view; live releases are in the decision-fabric service (/de/registry)"}


def agent_audit(a: dict) -> dict:
    from lif.decision.mining.audit import audit
    from lif.decision.mining.miner import mine
    from lif.decision.mining.traces import claude_code_runs, jsonl_runs
    from lif.common import config
    srcs = a.get("sources") or (config.get("decision_engineering.traces") or {}).get("claude_code_dirs") or []
    runs = []
    for s in srcs:
        runs += list(claude_code_runs([s])) if "claude" in s else list(jsonl_runs([s]))
    if not runs:
        return {"error": f"no traces in {srcs}"}
    return audit(mine(runs), agent=a.get("agent"), top=int(a.get("top", 5)))


TOOLS: list[dict] = [
    {"name": "classify_operation", "fn": classify_operation,
     "description": "Which primitive should do this operation: CODE, JEV (bounded decision), GENERATIVE or "
                    "HUMAN_OR_POLICY? Give the operation and, if the answer is one of a known set, the labels.",
     "inputSchema": _schema({"description": S, "labels": A, "irreversible": B, "needs_image": B, "untrusted": B,
                             "output_type": S}, ["description"])},
    {"name": "decide", "fn": _decide, "async": True,
     "description": "Evaluate a versioned decision through the cascade (code → Jev → local → Kimi → human). "
                    "Returns answer, confidence, executor, decision_version, escalated, actionable.",
     "inputSchema": _schema({"decision": S, "state": O, "data_class": S, "agent": S, "workflow": S,
                             "choices": A}, ["decision", "state"])},
    {"name": "decision_lint", "fn": decision_lint,
     "description": "Lint a decision spec (JSON object in `spec`) or a path (file, decision dir, package tree).",
     "inputSchema": _schema({"spec": O, "path": S})},
    {"name": "decision_registry", "fn": decision_registry,
     "description": "Decision names, versions, stages and the serving version (repository view).",
     "inputSchema": _schema({"name": S})},
    {"name": "agent_audit", "fn": agent_audit,
     "description": "Mine agent traces and report bounded decisions hidden in LLM calls, avoidable heavy calls "
                    "and fused decisions. Primary-workload sessions are excluded by default.",
     "inputSchema": _schema({"agent": S, "sources": A, "top": {"type": "integer"}})},
]


def handle(msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None                                      # notification
    try:
        if method == "initialize":
            res: Any = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                        "serverInfo": {"name": "lif-decisions", "version": "1"}, "instructions": INSTRUCTIONS}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]}
        elif method == "tools/call":
            t = next((t for t in TOOLS if t["name"] == params.get("name")), None)
            if t is None:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
            args = params.get("arguments") or {}
            out = asyncio.run(t["fn"](args)) if t.get("async") else t["fn"](args)
            res = {"content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}],
                   "isError": isinstance(out, dict) and "error" in out}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {e}"}],
                                                       "isError": True}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
