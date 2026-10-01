"""knowledge — CLI for the persistent knowledge layer. Works locally against the workspace (no service).

    knowledge status                      health + compile summary
    knowledge validate                    compile; exit 1 on error diagnostics (CI / pre-commit)
    knowledge diagnostics [--severity S]  all diagnostics
    knowledge search QUERY [--type T]     hybrid search
    knowledge get KEY | why KEY | graph KEY | impact KEY | evidence KEY
    knowledge query --type decision --depends-on X [--where f=v] …
    knowledge context "TASK" [--project P] [--profile engineering] [--budget 2000]
    knowledge decisions | assumptions | reconsiderations
    knowledge review KEY --outcome reaffirmed [--trigger T] [--notes …]
    knowledge session resume [PROJECT] | session checkpoint PROJECT --summary … [--next …]
    knowledge package list|search|inspect|install|update|lock|publish
    knowledge skill list|run|test|fork
    knowledge view list | view render NAME [--param k=v]
    knowledge migrate FILE [--apply --yes]
    knowledge ingest FILE --repo R
    knowledge diff [REV]                  semantic diff of the working tree against a git revision
    knowledge delete KEY --yes            destructive; reports the links it breaks
    knowledge rebuild | compile | watch
    knowledge eval [SUITE] | suggest | events consume --registry-db PATH
    knowledge mcp [--agent NAME] | serve [--port 8090]

Global: --root (default $LIF_KNOWLEDGE_ROOT or lif/knowledge), --json, --as ACTOR (default human).
The CLI trusts its caller: `--yes` approvals are enforced at the MCP and HTTP interfaces, not here.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from lif.knowledge.ops import Knowledge


def table(rows: list[list[Any]], headers: list[str]) -> str:
    if not rows:
        return "(none)"
    cells = [headers] + [["" if c is None else str(c) for c in r] for r in rows]
    w = [min(60, max(len(r[i]) for r in cells)) for i in range(len(headers))]
    lines = ["  ".join(c[:60].ljust(x) for c, x in zip(r, w)).rstrip() for r in cells]
    lines.insert(1, "  ".join("-" * x for x in w))
    return "\n".join(lines)


def emit(args, data: Any, render=None) -> None:
    if args.json or render is None:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(render(data))


def kv(pairs: list[str] | None) -> dict:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k.strip()] = v.strip()
    return out


# ── renderers ────────────────────────────────────────────────────────────────

def r_health(h: dict) -> str:
    lc = h.get("last_compile") or {}
    rows = [("Objects", h["objects"]), ("Embedded records/passages", h["embedded"]), ("References", h["references"]),
            ("Valid references", f"{h['valid_reference_rate'] * 100:.1f}%"), ("Broken references", h["broken_references"]),
            ("Missing required fields", h["missing_fields"]), ("Errors / warnings", f"{h['errors']} / {h['warnings']}"),
            ("Stale objects", h["stale"]), ("Stale or challenged assumptions", h["stale_assumptions"]),
            ("Decisions needing review", h["decisions_needing_review"]),
            ("Decision traceability", "-" if h["decision_traceability_rate"] is None else f"{h['decision_traceability_rate'] * 100:.0f}%"),
            ("Evidence coverage", "-" if h["evidence_coverage"] is None else f"{h['evidence_coverage'] * 100:.0f}%"),
            ("Conflicting claims", h["conflicting_claims"]), ("Orphans", h["orphans"]),
            ("Open questions", h["open_questions"]), ("Open tasks", h["open_tasks"]),
            ("Reconsideration queue", h["reconsideration_queue"]),
            ("Context recovery rate", "-" if h["context_recovery_rate"] is None else f"{h['context_recovery_rate'] * 100:.0f}%"),
            ("Last compile", f"{lc.get('seconds')} s · parsed {lc.get('files_parsed')} · cached {lc.get('files_cached')}")]
    return "KNOWLEDGE HEALTH\n\n" + "\n".join(f"{k:<34}{v:>12}" if not isinstance(v, str) or len(v) < 12 else f"{k:<34}{v}"
                                             for k, v in rows)


def r_diags(ds: list[dict]) -> str:
    if not ds:
        return "no diagnostics"
    out = []
    for d in ds:
        loc = f"{d['path']}" + (f":{d['line']}" if d.get("line") else "")
        out.append(f"{d['severity']:<7} {d['code']} {d.get('key') or ''}\n        {d['message']}\n        {loc}")
    return "\n".join(out)


def r_objs(rows: list[dict]) -> str:
    return table([[r["key"], r["type"], r["status"], r["title"][:60], r["updated"]] for r in rows],
                 ["key", "type", "status", "title", "updated"])


def r_recon(rows: list[dict]) -> str:
    if not rows:
        return "reconsideration queue is empty"
    return "\n".join(f"[{r['priority']}] RECONSIDERATION REQUIRED  {r['key']}\n    trigger: {r['trigger']} — {r['reason']}"
                     f"\n    via: {' → '.join(r['chain'])}" for r in rows)


def r_why(w: dict) -> str:
    o = w["object"]
    L = [f"{o['type'].upper()} {o['key']} — {o['status']}", f"  {o['title']}"]
    for k in ("question", "selected", "date"):
        if w.get(k):
            L.append(f"  {k}: {w[k]}")
    if w["alternatives"]:
        L.append("  alternatives: " + ", ".join(map(str, w["alternatives"])))
    L.append("  evidence:")
    L += [f"    - {e['key']}  {e.get('title', '')}" + ("  (MISSING)" if e.get("missing") else "") for e in w["evidence"]] or ["    (none)"]
    L.append("  assumptions:")
    for a in w["assumptions"]:
        s = a.get("support") or {}
        L.append(f"    - {a['key']} [{a.get('status')}]  supports={len(s.get('supports', []))} "
                 f"contradicts={len(s.get('contradicts', []))}")
    if w["affects"]:
        L.append("  affects: " + ", ".join(x["key"] for x in w["affects"] if x))
    if w["implemented_by"]:
        L.append("  implemented by: " + ", ".join(x["key"] for x in w["implemented_by"] if x))
    if w["supersedes"]:
        L.append("  supersedes: " + " → ".join(x["key"] for x in w["supersedes"]))
    if w["dependents"]:
        L.append("  downstream: " + ", ".join(f"{len(v)} {t}" for t, v in w["dependents"].items()))
    for r in w["reconsiderations"]:
        L.append(f"  RECONSIDERATION {'(resolved by ' + r['resolved_by'] + ')' if r['resolved_by'] else 'REQUIRED'}: "
                 f"{r['reason']}")
    if w.get("rationale"):
        L += ["", "  " + w["rationale"].strip().replace("\n", "\n  ")[:1500]]
    p = w["provenance"]
    L.append(f"\n  source: {p['path']}:{p['line']} · {p['repo']}@{p['repo_version']} · updated {p['updated']} "
             f"{('by ' + p['updated_by']) if p['updated_by'] else ''}")
    return "\n".join(L)


def r_impact(i: dict) -> str:
    c = i["changed"]
    L = [f"CHANGED OBJECT  {c['key']} ({c['type']})", f"  {i['total']} dependent object(s)"]
    for t, items in sorted(i["by_type"].items()):
        L.append(f"  ├── {t} ({len(items)})")
        L += [f"  │     {x['key']}  [{x['status']}]  depth {x['depth']}" for x in items]
    return "\n".join(L)


def r_trace(t: dict) -> str:
    c = t["claim"]
    L = [f"CLAIM {c['key']} [{c['status']}]", f"  {t.get('statement') or c['title']}"]
    for g in t["groundings"]:
        ev = g["evidence"] or {}
        L.append(f"  {g['stance'].upper():<11} {ev.get('key', '?')} (basis {g['basis']}, quality "
                 f"{g['evidence_quality'] or '?'})")
        if g["passage"]:
            L.append(f"      “{(g['passage']['text'] or '')[:300]}”  — {Path(g['passage']['path']).name}:{g['passage']['line']}")
        elif g["location"]:
            L.append(f"      at {g['location']}")
    if t["extracted_from"]:
        L.append(f"  extracted from {t['extracted_from']['key']}: “{(t['extracted_from']['text'] or '')[:200]}”")
    if t["independence_warning"]:
        L.append("  WARNING: several groundings share one dataset (not independent)")
    L.append(f"  note: {t['verdict']}")
    return "\n".join(L)


# ── commands ─────────────────────────────────────────────────────────────────

def c_status(kn, a):
    emit(a, kn.graph.health(), r_health)


def c_validate(kn, a):
    errs = kn.store.diagnostics("error")
    emit(a, errs, lambda d: r_diags(d) + f"\n\n{len(d)} error(s)")
    return 1 if errs else 0


def c_diagnostics(kn, a):
    emit(a, kn.store.diagnostics(a.severity, a.key), r_diags)


def c_search(kn, a):
    res = kn.search.search(" ".join(a.query), a.type, a.repo, a.status, a.project, a.limit)
    emit(a, res, lambda r: f"retrieval: {' + '.join(r['retrieval'])}\n" + table(
        [[x["key"], x["type"], x["status"], x["title"][:50], x["score"], (x["snippet"] or "").replace("\n", " ")[:60]]
         for x in r["results"]], ["key", "type", "status", "title", "score", "snippet"]))


def c_get(kn, a):
    emit(a, kn.graph.get(a.key), lambda o: json.dumps({k: o[k] for k in ("key", "type", "title", "status", "fields",
                                                                         "provenance", "links_out", "links_in",
                                                                         "diagnostics")}, indent=2, default=str))


def c_why(kn, a):
    emit(a, kn.graph.why(a.key), r_why)


def c_graph(kn, a):
    def r(g):
        L = [f"LINEAGE {g['root']}", "  upstream (rests on):"]
        L += [f"    {e['from']} —{e['rel']}→ {e['to']}" for e in g["edges"] if e["from"] in [g["root"]] + g["upstream"]
              and e["to"] in g["upstream"]] or ["    (none)"]
        L.append("  downstream (rests on it):")
        L += [f"    {e['from']} —{e['rel']}→ {e['to']}" for e in g["edges"] if e["from"] in g["downstream"]] or ["    (none)"]
        return "\n".join(L)
    emit(a, kn.graph.lineage(a.key, a.depth), r)


def c_impact(kn, a):
    emit(a, kn.graph.impact(a.key), r_impact)


def c_evidence(kn, a):
    emit(a, kn.graph.trace_claim(a.key), r_trace)


def c_query(kn, a):
    args = {k: getattr(a, k) for k in ("type", "status", "repo", "project", "depends_on", "dependent_of", "links_to",
                                       "linked_from", "older_than_days", "text") if getattr(a, k) is not None}
    if a.where:
        args["where"] = a.where
    kn.store.log_query("find:" + json.dumps(args, sort_keys=True), "human")
    emit(a, kn.graph.find(**args), r_objs)


def c_context(kn, a):
    pkg = kn.context.assemble(" ".join(a.task), a.project, a.profile, a.budget, a.pin)
    emit(a, pkg, lambda p: p["markdown"] + f"\n({p['used_tokens']} tokens, {len(p['items'])} items of "
                                           f"{p['candidates']} candidates, {p['elapsed_ms']} ms)")


def c_list_type(t: str, status: str | None = None):
    def run(kn, a):
        emit(a, kn.graph.find(type=t, status=a.status or status, project=a.project), r_objs)
    return run


def c_recon(kn, a):
    emit(a, kn.store.reconsiderations(open_only=not a.all), r_recon)


def c_review(kn, a):
    from lif.knowledge.mcp import _review
    emit(a, _review(kn, a.actor, {"object": a.key, "trigger": a.trigger, "outcome": a.outcome, "notes": a.notes or "",
                                  "repo": a.repo}), lambda r: f"recorded {r['key']}")


def c_session(kn, a):
    if a.action == "resume":
        emit(a, kn.context.resume(a.project), lambda r: r["markdown"])
    else:
        if not a.project or not a.summary:
            raise SystemExit("session checkpoint needs PROJECT and --summary")
        res = kn.context.checkpoint(a.project, a.agent or a.actor, a.summary, a.completed, a.decisions, a.questions,
                                    a.artifacts, a.changed, a.next, recovered=a.recovered)
        emit(a, res, lambda r: f"checkpoint {r['key']} written to {r['path']}")


def c_package(kn, a):
    from lif.knowledge import packages as P
    act = a.action
    if act in ("list", "search"):
        emit(a, kn.ws.registry.search(a.name or ""), lambda rows: table(
            [[r["name"], ", ".join(r["versions"]), r["description"][:70],
              ",".join(x for x in ([p.name for p in kn.ws.repos if any(d.name == r["name"] for d in p.dependencies)]))]
             for r in rows], ["package", "versions", "description", "used by"]))
    elif act == "inspect":
        emit(a, P.inspect(kn, a.name, a.version))
    elif act == "install":
        emit(a, P.install(kn, a.repo or kn.ws.repos[0].name, a.name, approved=a.yes))
    elif act == "update":
        rep = P.update(kn, a.repo or kn.ws.repos[0].name, a.name, a.version, migrate=a.migrate, approved=a.yes)

        def r(x):
            L = [f"{x['package']} {x['from']} → {x['to']}  ({'MAJOR ' if x['major'] else ''}"
                 f"{'BREAKING' if x['breaking'] else 'compatible schema'})"]
            for t, cs in x["type_changes"].items():
                L += [f"  {t}: {c['field']} {c['change']}{'  [breaking]' if c['breaking'] else ''}" for c in cs]
            if x["would_break"]:
                L.append(f"  would break {len(x['would_break'])} object(s):")
                L += [f"    {d['code']} {d['key']}: {d['message']}" for d in x["would_break"][:20]]
            L += [f"  migration available: {Path(m['file']).name}" for m in x["migrations"]]
            L.append("  " + x["advice"])
            if not x.get("applied"):
                L.append("  " + x.get("approval", ""))
            return "\n".join(L)
        emit(a, rep, r)
    elif act == "lock":
        from lif.knowledge.repo import Workspace
        repo = kn.ws.repo(a.repo or kn.ws.repos[0].name)
        emit(a, P.lock(kn.ws, repo), lambda d: f"locked {len(d['packages'])} package(s) in {repo.lock_path()}")
    elif act == "publish":
        emit(a, P.publish(kn, a.name, approved=a.yes))
    elif act == "test":
        res = P.test_types(kn, a.name, a.version)
        emit(a, res, lambda x: "\n".join(f"[{'ok' if c['ok'] else 'FAIL'}] {c['case']}" +
                                         (f"  expected {c['expected']} got {c['got']}" if not c["ok"] and "expected" in c
                                          else f"  {c.get('errors')}" if not c["ok"] else "") for c in x["cases"])
             or "no type tests")
        return 0 if res["passed"] else 1


def c_skill(kn, a):
    from lif.knowledge import learn, skills
    if a.action == "list":
        emit(a, skills.list_skills(kn), lambda rows: table(
            [[s["name"], s["version"], f"{s['repo']}@{s['repo_version']}", s["description"][:70]] for s in rows],
            ["skill", "v", "from", "description"]))
    elif a.action == "run":
        res = skills.run_skill(kn, a.name, kv(a.input))

        def r(x):
            L = [f"SKILL {x['skill']} v{x['version']} ({x['repo']}@{x['repo_version']}) — "
                 f"{'PASS' if x['passed'] else 'FAIL'}"]
            L += [f"  input error: {e}" for e in x.get("input_errors") or []]
            for c in x["checks"]:
                L.append(f"  [{'ok' if c['passed'] else 'FAIL'}] {c['id']}: {c['description']}"
                         + (f"  (because {c['because']})" if c.get("because") else ""))
            return "\n".join(L) + "\n\n" + x["instructions"]
        emit(a, res, r)
        return 0 if res["passed"] else 1
    elif a.action == "test":
        res = skills.test_skill(kn, a.name)
        emit(a, res, lambda x: "\n".join(f"[{'ok' if c['ok'] else 'FAIL'}] {c['case']} {c['got']}" for c in x["cases"])
             or "no tests")
        return 0 if res["passed"] else 1
    elif a.action == "fork":
        emit(a, learn.fork_skill(kn, a.name, a.repo or kn.ws.repos[0].name))


def c_view(kn, a):
    from lif.knowledge import views
    if a.action == "list":
        emit(a, {"views": views.list_views(kn), "apps": views.list_apps(kn)}, lambda d: table(
            [[v["name"], v["kind"], v["repo"], v["title"]] for v in d["views"]], ["view", "kind", "repo", "title"])
             + "\n\napps: " + ", ".join(x["name"] for x in d["apps"]))
    else:
        emit(a, views.render(kn, a.name, kv(a.param)))


def c_migrate(kn, a):
    import yaml
    from lif.knowledge import packages as P
    m = yaml.safe_load(Path(a.file).read_text())
    m["file"] = a.file
    res = P.apply_migration(kn, m, approved=a.yes and a.apply, dry_run=not a.apply)
    emit(a, res)


def c_ingest(kn, a):
    from lif.knowledge.learn import ingest
    emit(a, ingest(kn, a.file, a.repo or kn.ws.repos[0].name, kind=a.kind, author=a.author or "",
                   extract=not a.no_extract, actor=a.actor))


def c_delete(kn, a):
    emit(a, kn.writer(a.actor).delete(a.key, approved=a.yes),
         lambda r: f"deleted {r['deleted']}" + (f"; now broken links from: {', '.join(r['now_broken_links_from'])}"
                                                 if r["now_broken_links_from"] else ""))


def c_diff(kn, a):
    from lif.knowledge.graph import diff, render_diff
    from lif.knowledge.repo import Workspace
    from lif.knowledge.compiler import Compiler
    from lif.knowledge.store import Store
    root = kn.ws.root
    top = Path(subprocess.check_output(["git", "-C", str(root), "rev-parse", "--show-toplevel"], text=True).strip())
    with tempfile.TemporaryDirectory() as tmp:
        p = subprocess.run(["git", "-C", str(top), "archive", a.rev, str(root.relative_to(top))], capture_output=True)
        if p.returncode != 0:
            raise ValueError(f"{root.relative_to(top)} is not in git revision {a.rev}: {p.stderr.decode().strip()[:200]}")
        arch = p.stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=arch, check=True)
        old_ws = Workspace.open(Path(tmp) / root.relative_to(top), registry=kn.ws.registry.root)
        old = Store(None)
        Compiler(old_ws, old).compile()
        emit(a, diff(old, kn.store, kn.graph), render_diff)


def c_rebuild(kn, a):
    t0 = time.time()
    st = kn.rebuild()
    emit(a, st, lambda s: f"rebuilt index from canonical repos: {s['objects']} objects in {time.time() - t0:.2f} s, "
                          f"{s['diagnostics']}")


def c_compile(kn, a):
    emit(a, kn.store.meta("last_compile"))


def c_watch(kn, a):
    print("watching for changes (ctrl-c to stop)", file=sys.stderr)
    while True:
        st = kn.refresh()
        if st:
            print(f"{time.strftime('%H:%M:%S')} compiled: parsed {st['files_parsed']}, changed {st['changed']}, "
                  f"diagnostics {st['diagnostics']}", flush=True)
        time.sleep(a.interval)


def c_eval(kn, a):
    from lif.knowledge.evals import run
    res = run(kn, a.suite)
    emit(a, res, lambda r: table([[c["case"], "ok" if c["ok"] else "FAIL", c["critical_recall"], c["precision"],
                                   c["tokens"], ",".join(c["missing"])] for c in r["cases"]],
                                 ["case", "result", "recall", "precision", "tokens", "missing"]) +
         f"\n\ncontext recovery {r['context_recovery']}, mean precision {r['mean_precision']}, "
         f"mean tokens {r['mean_tokens']} ({r['passed']}/{r['total']} passed)")
    return 0 if res["passed"] == res["total"] else 1


def c_suggest(kn, a):
    emit(a, kn.suggest(), lambda rows: "\n".join(f"{r['uses']}× {r['operation']} → {r['proposal']} ({r['file']})"
                                                 for r in rows) or "nothing repeated often enough yet")


def c_events(kn, a):
    from lif.knowledge.events import EventSink
    from lif.models.registry import Registry
    sink = EventSink(kn, a.repo)
    reg = Registry(a.registry_db)
    rows = reg.activity(5000, sink.cursor)
    emit(a, {"consumed": len(rows), "written": sink.consume(rows) if rows else []})


def c_mcp(kn, a):
    from lif.knowledge.mcp import serve
    serve(kn, f"agent:{a.agent}")


def c_serve(kn, a):
    import uvicorn
    uvicorn.run("lif.knowledge.app:app", host=a.host, port=a.port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="knowledge", description="Persistent knowledge layer for LIF agent repos")
    p.add_argument("--root")
    p.add_argument("--json", action="store_true")
    p.add_argument("--as", dest="actor", default="human")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(fn=fn)
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return sp

    add("status", c_status)
    add("health", c_status)
    add("validate", c_validate)
    d = add("diagnostics", c_diagnostics)
    d.add_argument("--severity")
    d.add_argument("--key")
    s = add("search", c_search)
    s.add_argument("query", nargs="+")
    for k in ("type", "repo", "status", "project"):
        s.add_argument(f"--{k}")
    s.add_argument("--limit", type=int, default=20)
    for name, fn in (("get", c_get), ("why", c_why), ("impact", c_impact), ("evidence", c_evidence)):
        add(name, fn).add_argument("key")
    g = add("graph", c_graph)
    g.add_argument("key")
    g.add_argument("--depth", type=int, default=4)
    q = add("query", c_query)
    for k in ("type", "status", "repo", "project", "depends-on", "dependent-of", "links-to", "linked-from", "text"):
        q.add_argument(f"--{k}", dest=k.replace("-", "_"))
    q.add_argument("--older-than-days", type=int, dest="older_than_days")
    q.add_argument("--where", action="append")
    c = add("context", c_context)
    c.add_argument("task", nargs="+")
    c.add_argument("--project")
    c.add_argument("--profile")
    c.add_argument("--budget", type=int, default=2000)
    c.add_argument("--pin", action="append")
    for name, t in (("decisions", "decision"), ("assumptions", "assumption"), ("tasks", "task"),
                    ("questions", "question"), ("incidents", "incident"), ("methods", "method")):
        sp = add(name, c_list_type(t))
        sp.add_argument("--status")
        sp.add_argument("--project")
    r = add("reconsiderations", c_recon)
    r.add_argument("--all", action="store_true")
    rv = add("review", c_review)
    rv.add_argument("key")
    rv.add_argument("--outcome", required=True, choices=["reaffirmed", "revised", "superseded", "needs-work"])
    rv.add_argument("--trigger")
    rv.add_argument("--notes")
    rv.add_argument("--repo")
    se = add("session", c_session)
    se.add_argument("action", choices=["resume", "checkpoint"])
    se.add_argument("project", nargs="?")
    se.add_argument("--summary")
    se.add_argument("--agent")
    for k in ("completed", "decisions", "questions", "artifacts", "changed", "next"):
        se.add_argument(f"--{k}", action="append")
    se.add_argument("--recovered", type=lambda v: v.lower() in ("1", "yes", "true"), default=None)
    pk = add("package", c_package)
    pk.add_argument("action", choices=["list", "search", "inspect", "install", "update", "lock", "publish", "test"])
    pk.add_argument("name", nargs="?")
    pk.add_argument("--version")
    pk.add_argument("--repo")
    pk.add_argument("--migrate", action="store_true")
    pk.add_argument("--yes", action="store_true", help="approve (human only)")
    sk = add("skill", c_skill)
    sk.add_argument("action", choices=["list", "run", "test", "fork"])
    sk.add_argument("name", nargs="?")
    sk.add_argument("--input", action="append", help="k=v")
    sk.add_argument("--repo")
    v = add("view", c_view)
    v.add_argument("action", choices=["list", "render"])
    v.add_argument("name", nargs="?")
    v.add_argument("--param", action="append", help="k=v")
    m = add("migrate", c_migrate)
    m.add_argument("file")
    m.add_argument("--apply", action="store_true")
    m.add_argument("--yes", action="store_true")
    i = add("ingest", c_ingest)
    i.add_argument("file")
    i.add_argument("--repo")
    i.add_argument("--kind", default="documentation")
    i.add_argument("--author")
    i.add_argument("--no-extract", action="store_true")
    dl = add("delete", c_delete)
    dl.add_argument("key")
    dl.add_argument("--yes", action="store_true", help="approve (human only)")
    df = add("diff", c_diff)
    df.add_argument("rev", nargs="?", default="HEAD")
    add("rebuild", c_rebuild)
    add("compile", c_compile)
    w = add("watch", c_watch)
    w.add_argument("--interval", type=float, default=1.0)
    e = add("eval", c_eval)
    e.add_argument("suite", nargs="?")
    add("suggest", c_suggest)
    ev = add("events", c_events)
    ev.add_argument("action", choices=["consume"])
    ev.add_argument("--registry-db", required=True)
    ev.add_argument("--repo")
    mc = add("mcp", c_mcp)
    mc.add_argument("--agent", default="agent")
    sv = add("serve", c_serve)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8090)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if not hasattr(a, "json"):
        a.json = False
    from lif.knowledge.packages import ApprovalRequired
    from lif.knowledge.repo import RepoError
    from lif.knowledge.writer import WriteError
    try:
        kn = Knowledge.open(a.root)
        rc = a.fn(kn, a)
        return int(rc or 0)
    except (KeyError, ValueError, WriteError, RepoError, ApprovalRequired) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
