"""MCP server for the Understanding Compiler (stdio, JSON-RPC 2.0, no SDK dependency; spec §43, §99).

    python -m lif.understanding.mcp          (registered as `lif-understanding` in the repo's .mcp.json)

Tools: explanation_create, explanation_get, explanation_render, explanation_simplify, explanation_deepen,
explanation_evaluate, explanation_renderers. Text artifacts (prose, markdown, Mermaid) come back
inline; HTML and Excalidraw files are written under ~/.local/share/lif/understanding/artifacts/<session>/
and returned as paths. Everything runs locally: packages, live state, rules, the local gateway.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from lif.decision.mcp import PROTOCOL, _schema

INSTRUCTIONS = ("Adaptive Understanding Compiler. explanation_create turns a question into one canonical "
                "ExplanationSpec and the representations that explain it best (summary, diagram, table, interactive "
                "page). Overrides (render/simplify/deepen) reuse the same spec. Renderers never add facts.")
S, A, B = {"type": "string"}, {"type": "array", "items": {"type": "string"}}, {"type": "boolean"}
INT = {"type": "integer"}
INLINE = ("text/plain", "text/markdown", "text/vnd.mermaid")

_comp = None


def comp():
    global _comp
    if _comp is None:
        from lif.understanding.compiler import default_compiler
        _comp = default_compiler(online=os.environ.get("LIF_UNDERSTANDING_OFFLINE") != "1")
    return _comp


async def _finish(events) -> dict:
    from lif.understanding.api import finish
    out = await finish(events)
    sid = out.get("session") or "unknown"
    for name, a in list(out.get("artifacts", {}).items()):
        if a["media_type"] not in INLINE:
            d = Path(os.path.expanduser("~/.local/share/lif/understanding/artifacts")) / sid
            d.mkdir(parents=True, exist_ok=True)
            ext = "html" if a["media_type"] == "text/html" else "excalidraw"
            p = d / f"{name}.{ext}"
            p.write_text(a.pop("artifact"))
            a["path"] = str(p)
    out.pop("events", None)
    return out


def _sid(a: dict) -> str:
    if a.get("session"):
        return a["session"]
    sid = comp().store.latest_session(a.get("explanation_id", ""))
    if sid is None:
        raise KeyError("pass `session`, or an `explanation_id` that has a session")
    return sid


async def create(a: dict) -> dict:
    from lif.understanding.compiler import ExplanationRequest
    keys = set(ExplanationRequest.model_fields)
    return await _finish(comp().explain(ExplanationRequest(**{k: v for k, v in a.items() if k in keys})))


async def get(a: dict) -> dict:
    spec = comp().store.get_spec(a["explanation_id"])
    if spec is None:
        return {"error": "no such explanation"}
    return {"spec": spec.to_json(), "semantic_hash": spec.semantic_hash()}


async def render(a: dict) -> dict:
    return await _finish(comp().rerender(_sid(a), format=a.get("format"), depth=a.get("depth"),
                                         audience=a.get("audience"), viewport=a.get("viewport"),
                                         selected=a.get("selected")))


async def simplify(a: dict) -> dict:
    return await _finish(comp().simplify(_sid(a)))


async def deepen(a: dict) -> dict:
    return await _finish(comp().deepen(_sid(a), a.get("focus")))


async def evaluate(a: dict) -> dict:
    return comp().evaluate(_sid(a))


async def renderers(a: dict) -> dict:
    return {"renderers": [c.to_dict() for c in comp().registry.capabilities()]}


REQ = {"question": S, "audience": S, "expertise": S, "profile": S, "depth": S, "time_budget_seconds": INT,
       "format": S, "viewport": S, "comprehension": B, "context": {"type": "object"}}
REF = {"explanation_id": S, "session": S}
TOOLS: list[dict[str, Any]] = [
    {"name": "explanation_create", "fn": create, "inputSchema": _schema(REQ, ["question"]),
     "description": "Explain a question: build one ExplanationSpec, choose representations, render and check them."},
    {"name": "explanation_get", "fn": get, "inputSchema": _schema({"explanation_id": S}, ["explanation_id"]),
     "description": "The canonical ExplanationSpec (IR) for an explanation id."},
    {"name": "explanation_render", "fn": render,
     "inputSchema": _schema({**REF, "format": S, "depth": S, "viewport": S, "audience": {"type": "object"},
                             "selected": A}),
     "description": "Re-render an explanation from the same spec: another format, depth, audience or selection."},
    {"name": "explanation_simplify", "fn": simplify, "inputSchema": _schema(REF),
     "description": "Simpler and shorter, from the same spec (no new research)."},
    {"name": "explanation_deepen", "fn": deepen, "inputSchema": _schema({**REF, "focus": A}),
     "description": "One level deeper; extends the spec with the local model only when it has no deeper content."},
    {"name": "explanation_evaluate", "fn": evaluate, "inputSchema": _schema(REF),
     "description": "Critic report (semantic consistency, objective coverage), feedback and router override rates."},
    {"name": "explanation_renderers", "fn": renderers, "inputSchema": _schema({}),
     "description": "Renderer registry: capabilities, resource classes, and which renderers are unavailable and why."},
]


def handle(msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    try:
        if method == "initialize":
            res: Any = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                        "serverInfo": {"name": "lif-understanding", "version": "1"}, "instructions": INSTRUCTIONS}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]}
        elif method == "tools/call":
            t = next((t for t in TOOLS if t["name"] == params.get("name")), None)
            if t is None:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
            out = asyncio.run(t["fn"](params.get("arguments") or {}))
            res = {"content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}],
                   "isError": isinstance(out, dict) and ("error" in out or out.get("status") == "error")}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    except Exception as e:                              # noqa: BLE001
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
