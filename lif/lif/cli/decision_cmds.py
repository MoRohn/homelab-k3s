"""local-ai decision … / agent audit / workflow optimize (spec §38, §83–§85).

Offline (no service needed): decision lint, decision generate, agent audit, workflow optimize,
and decision test/calibrate with --local (in-process fabric; Jev only when
TYPE_SAFE_JEV_API_KEY is in the environment and the test states may leave the box).
Everything else talks to the decision-fabric service (/de). Lifecycle changes send
X-LIF-Internal ($LIF_INTERNAL_KEY) and a named --actor; the service checks the gates.

Mining reads real traces, so its outputs default to ../private/lif/decision-engineering/
(git-ignored), never to the public tree.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PRIVATE = Path(os.environ.get("LIF_PRIVATE_DIR", Path(__file__).resolve().parents[3] / "private" / "lif"))
DE_ACTIONS = ["status", "workflows", "metrics", "lint", "test", "benchmark", "calibrate", "shadow", "promote",
              "rollback", "generate", "simulate", "registry", "autotune", "inventory", "outcome", "human"]


def add_parsers(sub, d) -> None:
    d.add_argument("target", nargs="?", help="PATH (lint), NAME or NAME/VERSION, or TRACE_PATTERN (generate)")
    d.add_argument("--local", action="store_true", help="run in-process instead of via the service")
    d.add_argument("--cases", help="tests.jsonl to use instead of the package's")
    d.add_argument("--to", dest="stage", help="target stage for promote / shadow")
    d.add_argument("--actor", default=os.environ.get("USER", ""), help="who is making the change")
    d.add_argument("--reason", default="")
    d.add_argument("--threshold", type=float, help="HIGH zone threshold (must be ≥ the calibrated one)")
    d.add_argument("--low", type=float, help="LOW zone threshold")
    d.add_argument("--rollout", type=float, help="rollout percent for automation stages")
    d.add_argument("--volume", type=float, default=1000, help="simulate: decisions per day")
    d.add_argument("--source", action="append", help="trace dir/file (repeatable); default: Claude Code projects")
    d.add_argument("--out", help="where to write generated drafts")
    d.add_argument("--use-agreement", action="store_true", dest="use_agreement")
    d.add_argument("--data-class", dest="data_class",
                   help="declare the test states' class (synthetic suites: PUBLIC); default: the spec's")
    d.add_argument("--outcome", help="outcome: the correct label for a provenance id (target)")
    d.add_argument("--answer", help="human: answer for ticket (target)")
    a = sub.add_parser("agent")
    a.add_argument("action", choices=["audit"])
    a.add_argument("target", nargs="?", help="AGENT (default: all)")
    a.add_argument("--source", action="append")
    a.add_argument("--top", type=int, default=5)
    a.add_argument("--upload", action="store_true", help="send the redacted inventory to the service (UI)")
    a.add_argument("--save", action="store_true", help="write the report under private/lif/decision-engineering/")
    w = sub.add_parser("workflow")
    w.add_argument("action", choices=["optimize"])
    w.add_argument("target", nargs="?", help="WORKFLOW (default: all)")
    w.add_argument("--source", action="append")
    w.add_argument("--agent")
    w.add_argument("--top", type=int, default=5)


# ── helpers ──────────────────────────────────────────────────────────────────

def _sources(args) -> list[str]:
    from lif.common import config
    return args.source or list((config.get("decision_engineering.traces") or {}).get("claude_code_dirs")
                               or ["~/.claude/projects"])


def _runs(srcs: list[str]):
    from lif.decision.mining import traces
    out = []
    for s in srcs:
        p = Path(s).expanduser()
        if p.suffix == ".db":
            out += list(traces.decisions_db_runs(p))
        elif p.is_dir() and any(p.rglob("*.jsonl")) and "claude" in str(p):
            out += list(traces.claude_code_runs([p]))
        else:
            out += list(traces.jsonl_runs([p]))
    return out


def _mine(args):
    from lif.decision.mining.miner import mine
    from lif.decision.types import load_definitions
    runs = _runs(_sources(args))
    if not runs:
        raise SystemExit(f"no traces found in {_sources(args)}")
    return mine(runs, existing_decisions=load_definitions())


def _pct(x):
    return "-" if x is None else f"{x * 100:.1f}%"


def _internal(api, method: str, path: str, body: Any) -> Any:
    import httpx
    from lif.cli.main import CliError
    key = os.environ.get("LIF_INTERNAL_KEY", "")
    if not key:
        raise CliError("LIF_INTERNAL_KEY is not set (lifecycle changes are internal)")
    r = api.http.request(method, api.decision + path, json=body, headers={"X-LIF-Internal": key})
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise CliError(f"HTTP {r.status_code}: {data.get('error') if isinstance(data, dict) else data}")
    return data


def _local_runtime():
    from lif.decision.engineering import Runtime
    from lif.decision.store import Store
    db = PRIVATE / "decision-engineering" / "local.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LIF_CALIBRATION_DIR", str(PRIVATE / "decision-engineering" / "calibration"))
    gw = os.environ.get("LIF_GATEWAY_URL")
    if not gw:
        print("note: LIF_GATEWAY_URL is not set; local escalation tiers will be unavailable", file=sys.stderr)
    return Runtime.build(store=Store(str(db)), gateway_url=gw or "http://127.0.0.1:18080",
                         gateway_key=os.environ.get("LIF_API_KEY"))


# ── decision ─────────────────────────────────────────────────────────────────

def cmd(api, args) -> int:
    from lif.cli.main import CliError, out, table
    a = args.action
    if a == "lint":
        from lif.decision.lint import lint_path, summarize
        res = lint_path(args.target or "decision-packages")
        errors = sum(summarize(fs)["error"] for fs in res.values())
        out(args, {k: [f.to_dict() for f in v] for k, v in res.items()}, lambda d: "\n".join(
            [f"{ref:<40} {summarize(fs)}" + "".join(f"\n   {f.severity:<7} {f.code:<22} {f.message}" for f in fs
                                                   if f.severity != "info") for ref, fs in res.items()]))
        return 1 if errors else 0
    if a == "generate":
        return _generate(args)
    if a in ("test", "calibrate") and args.local:
        return _local(args, a)
    if a == "test":
        body: dict[str, Any] = {"decision": _need(args), "data_class": args.data_class}
        if args.cases:
            from lif.decision.experiments import load_cases
            body["cases"] = load_cases(args.cases)
        rep = api.dec("POST", "/de/test", body)
        out(args, rep, _render_test)
        return 0 if (rep.get("accuracy") or 0) >= 0.9 else 1
    if a == "benchmark":
        rep = api.dec("POST", "/de/benchmark", {"decision": _need(args)})
        out(args, rep, lambda d: f"regression_ok={d['regression_ok']}\n" + "\n".join(
            f"  {k:<18} {v}" for k, v in d["delta"].items()) + "".join(f"\n  ✗ {r}" for r in d["reasons"]))
        return 0 if rep.get("regression_ok") else 1
    if a == "calibrate":
        rep = api.dec("POST", "/de/calibrate", {"decision": _need(args), "use_agreement": args.use_agreement})
        out(args, rep, _render_cal)
        return 0
    if a == "simulate":
        rep = api.dec("POST", "/de/simulate", {"decision": _need(args), "volume_per_day": args.volume})
        out(args, rep, lambda d: table([[r["threshold"], _pct(r["coverage"]), r["escalated_per_day"],
                                         _pct(r["error_rate"]), r["expected_failures_per_day"],
                                         f"${r['cost_per_day_usd']:.2f}", r["mean_latency_ms"]] for r in d["rows"]],
                                       ["threshold", "jev coverage", "escalations/day", "error rate",
                                        "failures/day", "cost/day", "mean ms"]))
        return 0
    if a in ("shadow", "promote"):
        stage = "shadow" if a == "shadow" else (args.stage or "low_risk_automation")
        pol = {k: v for k, v in (("high", args.threshold), ("low", args.low)) if v is not None}
        rep = _internal(api, "POST", "/de/transition", {"ref": _need(args), "stage": stage, "actor": args.actor,
                                                         "reason": args.reason, "policy": pol,
                                                         "rollout_pct": args.rollout})
        out(args, rep, lambda d: f"{d['release']['name']}/{d['release']['version']} → {d['release']['stage']} "
                                 f"(rollout {d['release']['rollout_pct']}%)\nevidence: {d['evidence']}")
        return 0
    if a == "rollback":
        rep = _internal(api, "POST", "/de/rollback", {"name": _need(args), "actor": args.actor,
                                                       "reason": args.reason})
        out(args, rep, lambda d: f"now serving: {d['serving']}")
        return 0
    if a == "registry":
        path = f"/de/registry/{args.target}" if args.target else "/de/registry"
        rep = api.dec("GET", path)
        out(args, rep, lambda d: table([[x["name"], x["serving"], x["stage"], x["rollout_pct"],
                                         " ".join(f"{v['ref'].split('/')[1]}:{v['stage']}" for v in x["versions"])]
                                        for x in d["decisions"]], ["decision", "serving", "stage", "rollout%",
                                                                   "versions"])
            if "decisions" in d else json.dumps(d, indent=2, default=str))
        return 0
    if a == "autotune":
        rep = api.dec("GET", f"/de/autotune/{_need(args)}")
        out(args, rep, lambda d: "\n".join(f"{r['severity']:<8} {r['action']:<22} {r['why']}"
                                           for r in d["recommendations"]) or "no recommendations")
        return 0
    if a == "inventory":
        rep = api.dec("GET", "/de/inventory")
        out(args, rep, lambda d: _render_inventory(d.get("inventory") or []))
        return 0
    if a == "outcome":
        rep = api.dec("POST", "/de/outcome", {"provenance_id": _need(args), "outcome": args.outcome,
                                               "source": "human"})
        out(args, rep, lambda d: json.dumps(d))
        return 0
    if a == "human":
        if args.target and args.answer:
            rep = _internal(api, "POST", f"/de/human/{args.target}", {"answer": args.answer, "reviewer": args.actor})
        else:
            rep = api.dec("GET", "/de/human")
        out(args, rep, lambda d: json.dumps(d, indent=2, default=str))
        return 0
    raise CliError(f"unknown decision action {a}")


def _need(args) -> str:
    from lif.cli.main import CliError
    if not args.target:
        raise CliError(f"decision {args.action} needs NAME or NAME/VERSION")
    return args.target


def _local(args, action: str) -> int:
    from lif.cli.main import out
    from lif.decision import experiments
    rt = _local_runtime()
    ref = _need(args)
    d = rt.registry.get(ref) if "/" in ref else rt.registry.versions(ref)[-1]
    if action == "test":
        pd = rt.package_dir(d)
        cases = experiments.load_cases(args.cases or (pd / "tests.jsonl" if pd else ""))
        rep = asyncio.run(experiments.run_tests(rt.fabric, d.ref, cases, data_class=args.data_class))
        if args.data_class == "PUBLIC" and set(rep["by_provider"]) - {"jev"}:
            # a Jev test that fell back to rules/defaults measures the fallback, not Jev: refuse it
            print(f"invalid run: providers {rep['by_provider']} (Jev unavailable?)", file=sys.stderr)
            return 3
        experiments.save(rt.store, "decision-test", "labels", d.ref, rep)
        out(args, {k: v for k, v in rep.items() if k != "samples"}, _render_test)
        return 0 if (rep.get("accuracy") or 0) >= 0.9 else 1
    rep = rt.calibrate(d.ref, use_agreement=args.use_agreement)
    out(args, rep, _render_cal)
    return 0


def _generate(args) -> int:
    from lif.cli.main import out
    from lif.decision.mining.compiler import compile_item, write_package
    res = _mine(args)
    pat = args.target or ""
    items = [i for i in res.inventory if pat in i.signature and i.classification in ("JEV_CANDIDATE",)]
    if not items:
        print(f"no bounded-decision pattern matches {pat!r}; try `local-ai agent audit`", file=sys.stderr)
        return 1
    root = Path(args.out).expanduser() if args.out else PRIVATE / "decisions"
    written = []
    for it in items[:5]:
        cand = compile_item(it)
        try:
            p = write_package(cand, root, evidence=it.to_dict())
            written.append({"ref": cand.ref, "path": str(p), "lint": cand.lint, "notes": cand.notes})
        except FileExistsError as e:
            written.append({"ref": cand.ref, "skipped": str(e)})
    out(args, written, lambda ws: "\n".join(
        f"{w['ref']:<40} {w.get('path') or w.get('skipped')}\n    lint {w.get('lint')}" +
        "".join(f"\n    - {n}" for n in w.get("notes", [])) for w in ws) +
        "\n\nDrafts need review and labelled tests before `decision shadow` (spec §85).")
    return 0


# ── agent audit / workflow optimize ──────────────────────────────────────────

def cmd_agent(api, args) -> int:
    from lif.cli.main import out
    from lif.decision.mining.audit import audit
    res = _mine(args)
    rep = audit(res, agent=args.target, top=args.top)
    if args.save:
        f = PRIVATE / "decision-engineering" / f"audit-{args.target or 'all'}-{time.strftime('%Y%m%d-%H%M')}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"audit": rep, **res.to_dict()}, indent=1, default=str))
        rep["saved_to"] = str(f)
    if args.upload:
        body = {**res.to_dict(ops=True), "audit": rep}
        rep["uploaded"] = _internal(api, "POST", "/de/inventory", body)
    out(args, rep, _render_audit)
    return 0


def cmd_workflow(api, args) -> int:
    from lif.cli.main import out
    from lif.decision.mining.audit import optimize
    res = _mine(args)
    rep = optimize(res, workflow=args.target, agent=args.agent, top=args.top)
    out(args, rep, lambda d: _render_audit(d["audit"]) + "\n\nMIGRATION PLAN (recommendations; nothing is changed)\n"
        + "\n".join(f"\n{i + 1}. {p['signature']}  →  {p['candidate'] or 'no Jev candidate'}" +
                    "".join(f"\n     - {s}" for s in p["steps"]) for i, p in enumerate(d["plan"])))
    return 0


# ── rendering ─────────────────────────────────────────────────────────────────

def _render_audit(a: dict) -> str:
    b = a["by_bucket"]
    lines = ["AGENT DECISION AUDIT", "", f"Agent / workflow            {a['agent']} / {a['workflow']}",
             f"Runs · span                 {a['runs']} · {a['span_days']} days", ""]
    if a["span_days"] < 1:
        lines += [f"NOTE: per-day rates are extrapolated from {a['span_days'] * 24:.1f} h of traces", ""]
    lines += [
f"Steps analyzed              {a['steps_analyzed']:>8,}", "",
             f"Deterministic (CODE)        {b.get('CODE', 0):>8,}", f"Jev candidates              {b.get('JEV_CANDIDATE', 0):>8,}",
             f"Generative                  {b.get('GENERATIVE', 0):>8,}",
             f"Human / policy              {b.get('HUMAN_OR_POLICY', 0):>8,}", f"Unknown                     {b.get('UNKNOWN', 0):>8,}",
             "", f"Heavy-model calls           {a['heavy_calls']:>8,}",
             f"  avoidable as-is           {a['avoidable_heavy_calls']:>8,}  ({_pct(a['avoidable_heavy_share'])})",
             f"  with a fused decision     {a['calls_with_fused_decisions']:>8,}  (split decide()/generate() first)",
             f"Tokens in / out             {a['tokens']['input']:,} / {a['tokens']['output']:,}",
             f"Est. savings / day          " + (f"${a['estimated_savings_per_day_usd']:.2f}"
                                               if a["estimated_savings_per_day_usd"] is not None
                                               else f"unknown ({a['savings_note']})"), "", "Top opportunities:"]
    for i, t in enumerate(a["top_opportunities"], 1):
        lines.append(f"  {i}. {t['description']:<55} {t['calls_per_day']:>8}/day  separable {_pct(t['separable'])}"
                     f"  score {t['opportunity']}")
    if a.get("code_candidates"):
        lines += ["", "Move to code:"] + [f"  - {c['signature']} → {c['existing_methods'][0]}"
                                          for c in a["code_candidates"]]
    if a.get("excessive_state"):
        lines += ["", "Excessive state (state-compiler targets):"] + [
            f"  - {c['signature']}" for c in a["excessive_state"]]
    if a.get("saved_to"):
        lines += ["", f"saved: {a['saved_to']}"]
    return "\n".join(lines)


def _render_inventory(inv: list[dict]) -> str:
    from lif.cli.main import table
    return table([[i["agent"], i["description"][:48], i["calls_per_day"], i["classification"], i["executor"],
                   i["mean_latency_ms"], i["separable_share"], i["opportunity"]] for i in inv[:40]],
                 ["agent", "decision", "calls/day", "bucket", "executor", "latency ms", "separable", "score"]) \
        if inv else "no inventory yet"


def _render_test(r: dict) -> str:
    lines = [f"{r['decision']}  n={r['n']}  accuracy={_pct(r['accuracy'])}  mean confidence={r['mean_confidence']}",
             f"providers {r['by_provider']}  cost ${r['cost_usd']}  latency p50/p95 {r['latency_ms']['p50']}/"
             f"{r['latency_ms']['p95']} ms", f"by slice  {r.get('by_slice')}"]
    if r.get("at_threshold"):
        t = r["at_threshold"]
        lines.append(f"at threshold {t['threshold']}: coverage {_pct(t['coverage'])}, error {_pct(t['error_rate'])}")
    for f in r.get("failures", [])[:10]:
        lines.append(f"  ✗ expected {f['expected']:<12} got {f['answer']:<12} conf {f['confidence']}")
    return "\n".join(lines)


def _render_cal(r: dict) -> str:
    from lif.cli.main import table
    rows = [[f"{b['lo']:.2f}–{b['hi']:.2f}", b["n"], _pct(b["accuracy"]), b["mean_confidence"]]
            for b in r["reliability"] if b["n"]]
    rec = r["recommended"]
    ad = r["adequacy"]
    return "\n".join([f"{r['decision']}  labelled {r['n_labelled']}/{r['n_samples']} ({r['label_source']})  "
                      f"ECE {r['ece']}", table(rows, ["confidence", "n", "observed", "mean conf"]) if rows else
                      "no labelled samples yet", "",
                      f"recommended HIGH {rec['high']}  LOW {rec['low']}  (max error {rec['max_error']}, risk "
                      f"{r['risk']})", f"adequate: {ad['adequate']}" + "".join(f"\n  - {x}" for x in ad["reasons"])])
