"""`local-ai explain`: the Understanding Compiler from the terminal (spec §98).

    local-ai explain "Why are these pods not reaching the GPU?"
    local-ai explain "QUESTION" --format mermaid | --profile quick | --audience executive
    local-ai explain ID --interactive | --video | --simplify | --deepen | --show-ir | --evaluate | --history
    local-ai explain --renderers | --lint-packages

ID is an explanation id (x-…) or a session id (s-…). Runs in-process against the local store
($LIF_UNDERSTANDING_DB). Text artifacts print inline; HTML and Excalidraw files are written under
--out (default ~/.local/share/lif/understanding/artifacts/<session>/). --offline uses no model, no
Decision Fabric and no gpusched probe: packages, live state and rules only.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

INLINE = ("text/plain", "text/markdown", "text/vnd.mermaid")
EXT = {"text/html": "html", "application/vnd.excalidraw+json": "excalidraw", "text/vnd.mermaid": "mmd",
       "text/markdown": "md", "text/plain": "txt"}


def add_parser(sub) -> None:
    e = sub.add_parser("explain", help="explain a question with the best representation")
    e.add_argument("target", nargs="?", help="a question, or an explanation/session id")
    e.add_argument("--format", default="auto", help="auto | text | markdown | mermaid | excalidraw | table | html | "
                                                    "interactive | simulation | video")
    e.add_argument("--audience", help="engineer | developer | operator | executive | researcher | student | general")
    e.add_argument("--expertise", choices=["novice", "intermediate", "expert"])
    e.add_argument("--profile", choices=["quick", "standard", "deep", "teach"])
    e.add_argument("--depth", choices=["glance", "summary", "learn", "deep"])
    e.add_argument("--time", type=int, dest="time_budget", help="seconds the reader has")
    e.add_argument("--mobile", action="store_true")
    e.add_argument("--check", action="store_true", help="add 'check my understanding' questions")
    for flag in ("interactive", "video", "simplify", "deepen", "show-ir", "evaluate", "history", "renderers",
                 "lint-packages", "offline"):
        e.add_argument(f"--{flag}", action="store_true")
    e.add_argument("--focus", nargs="+", help="element ids to go deeper on (with --deepen)")
    e.add_argument("--out", help="directory for HTML/Excalidraw artifacts")
    e.add_argument("--db", help="SQLite path (default $LIF_UNDERSTANDING_DB)")


def _out_dir(args, sid: str) -> Path:
    base = Path(args.out) if args.out else Path(os.path.expanduser("~/.local/share/lif/understanding/artifacts"))
    p = base / sid if not args.out else base
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _print(events, args) -> int:
    rc, sid = 0, ""
    collected = []
    async for e in events:
        name, d = e["event"], e["data"]
        sid = d.get("session", sid)
        if args.json:
            collected.append(e)
            continue
        if name == "knowledge_model.ready":
            print(f"· knowledge model: {d['builder']} ({d['concepts']} concepts, {d['claims']} claims)",
                  file=sys.stderr)
        elif name == "summary.ready":
            print("\n" + d["artifact"] + "\n")
        elif name == "route.ready":
            print("Why this format:", file=sys.stderr)
            for w in d["why"]:
                print(f"  - {w}", file=sys.stderr)
            for x in d["deferred"]:
                print(f"  - deferred {x['renderer']}: {x['reason']}", file=sys.stderr)
        elif name.endswith(".ready") and "renderer" in d and d["renderer"] != "ste-prose":
            mt = d["media_type"]
            if mt in INLINE:
                fence = "mermaid" if mt == "text/vnd.mermaid" else ""
                print(f"── {d['renderer']} ──")
                print(f"```{fence}\n{d['artifact'].rstrip()}\n```\n" if fence else d["artifact"])
            else:
                path = _out_dir(args, sid) / f"{d['renderer']}.{EXT.get(mt, 'out')}"
                path.write_text(d["artifact"])
                print(f"── {d['renderer']} ── written to {path}")
        elif name.endswith(".failed"):
            print(f"! {d['renderer']} {d['status']}: {d['detail']} (falling back)", file=sys.stderr)
        elif name == "evaluation.ready":
            obj = d["objectives_covered"]
            print(f"· critic: semantic {'ok' if d['semantic_ok'] else 'FAILED'}, objectives covered "
                  f"{sum(obj.values())}/{len(obj)}, claims {d['summary']['claims_cited']}/{d['summary']['claims_total']}",
                  file=sys.stderr)
            for c in d["contradictions"]:
                print(f"  ! {c['renderer']}: {c['detail']}", file=sys.stderr)
        elif name == "done":
            print(f"· explanation {d['explanation_id']}  session {d['session']}", file=sys.stderr)
        elif name == "error":
            print(f"error ({d.get('stage')}): {d.get('detail') or d.get('issues')}", file=sys.stderr)
            rc = 2
    if args.json:
        print(json.dumps(collected, indent=1, default=str))
    return rc


def cmd_explain(api, args) -> int:
    from lif.understanding import packages, validate as V
    from lif.understanding.compiler import ExplanationRequest, default_compiler
    from lif.understanding.store import Store

    if args.renderers:
        from lif.understanding.registry import Registry
        rows = [c.to_dict() for c in Registry().capabilities()]
        if args.json:
            print(json.dumps(rows, indent=1))
        else:
            for c in rows:
                state = "ok" if c["available"] else f"unavailable: {c['unavailable_reason']}"
                print(f"{c['name']:18} {c['target']:24} {c['resource_class']:13} {state}")
        return 0
    if args.lint_packages:
        bad = 0
        for e in packages.entries():
            r = V.validate(e.spec)
            bad += len(r.errors)
            print(f"{e.ref:48} {'ok' if r.ok else 'ERRORS'}  " + "; ".join(i.message for i in r.issues))
        return 1 if bad else 0
    if not args.target:
        print("error: give a question or an explanation id", file=sys.stderr)
        return 2
    comp = default_compiler(Store(args.db) if args.db else None, online=not args.offline)
    t = args.target.strip()
    fmt = "simulation" if args.interactive else "video" if args.video else args.format
    if t.startswith(("x-", "s-")) and " " not in t:
        if t.startswith("x-"):
            row = comp.store.db.one("SELECT id FROM sessions WHERE spec_id=? ORDER BY updated_at DESC LIMIT 1", (t,))
            spec_id, sid = t, row["id"] if row else ""
        else:
            s = comp.store.get_session(t)
            spec_id, sid = (s["spec_id"] if s else ""), t
        spec = comp.store.get_spec(spec_id)
        if spec is None or not sid:
            print(f"error: no explanation {t}", file=sys.stderr)
            return 2
        if args.show_ir:
            print(json.dumps(spec.to_json(), indent=1))
            return 0
        if args.evaluate:
            print(json.dumps(comp.evaluate(sid), indent=1, default=str))
            return 0
        if args.history:
            print(json.dumps(comp.history(spec.id), indent=1, default=str))
            return 0
        if args.simplify:
            return asyncio.run(_print(comp.simplify(sid), args))
        if args.deepen:
            return asyncio.run(_print(comp.deepen(sid, args.focus), args))
        aud = {"role": {"engineer": "software_engineer"}.get(args.audience, args.audience)} if args.audience else None
        if aud and args.expertise:
            aud["expertise"] = args.expertise
        return asyncio.run(_print(comp.rerender(sid, format=fmt, depth=args.depth, audience=aud,
                                                viewport="mobile" if args.mobile else None), args))
    req = ExplanationRequest(question=t, audience=args.audience, expertise=args.expertise, profile=args.profile,
                             depth=args.depth, time_budget_seconds=args.time_budget, format=fmt,
                             viewport="mobile" if args.mobile else "desktop", comprehension=args.check)
    rc = asyncio.run(_print(comp.explain(req), args))
    if args.show_ir and rc == 0:
        row = comp.store.db.one("SELECT id FROM specs ORDER BY created_at DESC LIMIT 1")
        print(json.dumps(comp.store.get_spec(row["id"]).to_json(), indent=1))
    return rc
