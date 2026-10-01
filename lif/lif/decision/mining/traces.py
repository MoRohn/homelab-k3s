"""Trace ingestion (spec §3, §82): normalize agent executions into redacted Steps.

Sources
  claude_code   Claude Code session logs (~/.claude/projects/*/*.jsonl)
  jsonl         the LIF trace format, written by lif.decision.instrument (native agents,
                MCP, LangChain/Pydantic-AI adapters, batch workflows)
  decisions_db  the decision-fabric's own decision log (decisions.db)

Redaction is structural: a Step keeps *features* (tool name, program name, sizes, error
flags, token counts, short enumerable outputs), never prompts, file contents, command
arguments or tool output text. Short outputs survive only when they look like a label
(≤ 48 chars, one line) and the privacy detectors find nothing. Mining needs telemetry,
not uncontrolled full-data logging.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from lif.policy import engine as policy


@dataclass
class Step:
    run_id: str
    seq: int
    ts: float
    agent: str
    source: str
    kind: str                     # llm | tool | tool_result | user | decision | human | system
    name: str                     # model id, tool name, decision ref
    workflow: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: float | None = None
    model: str = ""
    output_label: str = ""        # short enumerable output, if any (redacted)
    is_error: bool | None = None
    outcome: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Run:
    id: str
    agent: str
    source: str
    steps: list[Step] = field(default_factory=list)
    workflow: str = ""


# ── redaction helpers ─────────────────────────────────────────────────────────

_LABEL = re.compile(r"^[\w .:/+\-]{1,48}$")
_PROG_SKIP = {"sudo", "env", "time", "nice", "timeout", "cd", "bash", "sh", "-c", "exec"}
DESTRUCTIVE = re.compile(r"(?i)(\brm\s+-[a-z]*[rf]|\bkubectl\s+(delete|drain|cordon|replace\s+--force)|"
                         r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-z]*f|branch\s+-D|rebase)|\bdocker\s+(rm|rmi|"
                         r"system\s+prune|stop|restart)|\bsystemctl\s+(stop|restart|disable)|\bDROP\s+(TABLE|DATABASE)|"
                         r"\bmkfs|\bdd\s+if=|\bchmod\s+-R|\bchown\s+-R|\bhelm\s+(uninstall|delete)|--force\b|"
                         r"\breboot\b|\bshutdown\b)")
TESTS = re.compile(r"(?i)\b(pytest|npm\s+test|go\s+test|cargo\s+test|make\s+test|unittest|jest|vitest)\b")


def safe_label(text: Any) -> str:
    """Return `text` if it is a short, single-line, non-sensitive label; else ''."""
    if not isinstance(text, str):
        return ""
    t = text.strip()
    if not _LABEL.match(t):
        return ""
    if policy.classify(t, "PUBLIC").data_class != policy.DataClass.PUBLIC:
        return ""
    return t.lower()


def program_of(cmd: str) -> str:
    """'cd x && .venv/bin/pytest -q' → 'pytest'; 'kubectl -n a get pods' → 'kubectl get'."""
    first = re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd.strip())
    best = ""
    for seg in first:
        if seg.split()[:1] and seg.split()[0] in ("cd", "export", "source", ".", "set", "pushd", "popd"):
            continue
        toks = [t for t in seg.split() if t not in _PROG_SKIP and "=" not in t.split("/")[-1][:1]]
        toks = [t for t in toks if not re.match(r"^[A-Z_]+=", t)]
        if not toks:
            continue
        prog = toks[0].split("/")[-1]
        if prog in ("cd", "export", "source", "."):
            continue
        sub, skip = "", False
        for t in toks[1:]:
            if skip:
                skip = False
                continue
            if t in ("-n", "-C", "-c", "-f", "--namespace", "--context", "-o", "-l", "--kubeconfig"):
                skip = True                  # the next token is this option's value, not a subcommand
                continue
            if not t.startswith("-") and re.match(r"^[a-z][\w-]*$", t):
                sub = t
                break
        best = f"{prog} {sub}".strip() if prog in ("git", "kubectl", "docker", "helm", "npm", "pip", "uv",
                                                     "systemctl", "gh", "cargo", "go", "make") else prog
        break
    return best[:40]


def _ts(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# ── Claude Code ───────────────────────────────────────────────────────────────

def claude_code_runs(paths: Iterable[str | Path], agent: str = "claude-code",
                     exclude: list[str] | None = None) -> Iterator[Run]:
    """One Run per session file. Each API request (requestId) is one `llm` Step; each tool
    use becomes a `tool` Step carrying the chosen tool; each tool result a `tool_result`.
    `exclude`: fnmatch patterns on the file path (default: decision_engineering.traces.exclude)."""
    import fnmatch
    from lif.common import config
    pats = exclude if exclude is not None else list((config.get("decision_engineering.traces") or {})
                                                     .get("exclude") or [])
    for p in paths:
        p = Path(p).expanduser()
        files = sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
        for f in files:
            if any(fnmatch.fnmatch(str(f), pat) for pat in pats):
                continue
            run = _claude_code_file(f, agent)
            if run is not None and run.steps:
                yield run


def _claude_code_file(f: Path, agent: str) -> Run | None:
    project = f.parent.name
    run = Run(id=f"cc:{f.stem}", agent=agent, source="claude_code", workflow=project)
    reqs: dict[str, Step] = {}
    seq = 0
    last_ts = 0.0
    pending_tools: dict[str, Step] = {}
    try:
        lines = f.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        typ, ts = o.get("type"), _ts(o.get("timestamp"))
        m = o.get("message") or {}
        if typ == "user":
            content = m.get("content")
            if isinstance(content, str) or (isinstance(content, list) and any(
                    b.get("type") == "text" for b in content if isinstance(b, dict))):
                if not o.get("isMeta") and not o.get("toolUseResult"):
                    seq += 1
                    run.steps.append(Step(run.id, seq, ts, agent, "claude_code", "user", "prompt",
                                          workflow=project, features={"chars": len(json.dumps(content))}))
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tu = pending_tools.pop(b.get("tool_use_id", ""), None)
                        tr = o.get("toolUseResult") if isinstance(o.get("toolUseResult"), dict) else {}
                        seq += 1
                        feats = {"tool": tu.name if tu else "", "stdout_chars": len(str(tr.get("stdout", "")))
                                 if tr else len(json.dumps(b.get("content", ""))),
                                 "stderr_chars": len(str(tr.get("stderr", ""))) if tr else 0,
                                 "interrupted": bool(tr.get("interrupted")) if tr else False}
                        if tu is not None:
                            feats["program"] = tu.features.get("program", "")
                        run.steps.append(Step(run.id, seq, ts, agent, "claude_code", "tool_result",
                                              tu.name if tu else "unknown", workflow=project, features=feats,
                                              is_error=bool(b.get("is_error")),
                                              latency_ms=(ts - tu.ts) * 1000 if tu and ts and tu.ts else None))
            last_ts = ts or last_ts
            continue
        if typ != "assistant":
            if typ == "system" and ts:
                last_ts = ts
            continue
        rid = o.get("requestId") or o.get("uuid") or f"r{seq}"
        u = m.get("usage") or {}
        st = reqs.get(rid)
        if st is None:
            seq += 1
            st = Step(run.id, seq, ts, agent, "claude_code", "llm", m.get("model", ""), workflow=project,
                      model=m.get("model", ""), latency_ms=(ts - last_ts) * 1000 if ts and last_ts else None,
                      features={"blocks": [], "tools": [], "text_chars": 0, "thinking": False,
                                "sidechain": bool(o.get("isSidechain"))})
            reqs[rid] = st
            run.steps.append(st)
        st.input_tokens = max(st.input_tokens, int(u.get("input_tokens", 0)) + int(
            u.get("cache_creation_input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)))
        st.cached_tokens = max(st.cached_tokens, int(u.get("cache_read_input_tokens", 0)))
        st.output_tokens = max(st.output_tokens, int(u.get("output_tokens", 0)))
        for b in m.get("content") or []:
            bt = b.get("type")
            st.features["blocks"].append(bt)
            if bt == "thinking":
                st.features["thinking"] = True
            elif bt == "text":
                st.features["text_chars"] += len(b.get("text", ""))
            elif bt in ("tool_use", "server_tool_use"):
                inp = b.get("input") or {}
                feats = {"arg_keys": sorted(inp)[:12], "arg_chars": len(json.dumps(inp))}
                if b.get("name") == "Bash":
                    cmd = str(inp.get("command", ""))
                    feats.update(program=program_of(cmd), destructive=bool(DESTRUCTIVE.search(cmd)),
                                 runs_tests=bool(TESTS.search(cmd)), background=bool(inp.get("run_in_background")))
                elif b.get("name") in ("Read", "Write", "Edit"):
                    fp = str(inp.get("file_path", ""))
                    feats.update(ext=Path(fp).suffix.lower()[:8],
                                 private=any(x in fp for x in ("/secrets/", "/private/", "/personal/", ".env")))
                elif b.get("name") == "Agent":
                    feats.update(subagent=safe_label(inp.get("subagent_type", "")) or "default")
                elif b.get("name") == "AskUserQuestion":
                    feats.update(n_questions=len(inp.get("questions") or []))
                seq += 1
                tstep = Step(run.id, seq, ts, agent, "claude_code", "tool", b.get("name", ""), workflow=project,
                             features=feats, output_label=b.get("name", "").lower(), model=st.model)
                st.features["tools"].append(b.get("name", ""))
                run.steps.append(tstep)
                pending_tools[b.get("id", "")] = tstep
        last_ts = ts or last_ts
    return run


# ── LIF trace format ─────────────────────────────────────────────────────────

def jsonl_runs(paths: Iterable[str | Path]) -> Iterator[Run]:
    """LIF format: one JSON object per line with Step fields (run_id, seq, ts, agent, kind,
    name, …). Free-text `output` is reduced to `output_label` via safe_label()."""
    runs: dict[str, Run] = {}
    for p in paths:
        p = Path(p).expanduser()
        for f in (sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]):
            for line in f.read_text(errors="replace").splitlines():
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                rid = str(o.get("run_id", f.stem))
                run = runs.setdefault(rid, Run(id=rid, agent=o.get("agent", "unknown"), source="jsonl",
                                               workflow=o.get("workflow", "")))
                feats = dict(o.get("features") or {})
                for k in ("purpose", "output_type", "tool", "candidates_n", "prompt_template"):
                    if k in o:
                        feats[k] = o[k]
                label = o.get("output_label") or safe_label(o.get("output", ""))
                run.steps.append(Step(rid, int(o.get("seq", len(run.steps) + 1)), _ts(o.get("ts", 0)),
                                      run.agent, "jsonl", o.get("kind", "llm"), str(o.get("name", "")),
                                      workflow=run.workflow, features=feats,
                                      input_tokens=int(o.get("input_tokens", 0)),
                                      output_tokens=int(o.get("output_tokens", 0)),
                                      latency_ms=o.get("latency_ms"), model=o.get("model", ""),
                                      output_label=label, is_error=o.get("is_error"), outcome=o.get("outcome", "")))
    for r in runs.values():
        r.steps.sort(key=lambda s: s.seq)
        yield r


# ── decision-fabric log ───────────────────────────────────────────────────────

def decisions_db_runs(path: str | Path, limit: int = 100000) -> Iterator[Run]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    run = Run(id=f"decisions:{Path(path).stem}", agent="decision-fabric", source="decisions_db")
    for i, r in enumerate(reversed(rows)):
        run.steps.append(Step(run.id, i + 1, r["ts"], "decision-fabric", "decisions_db", "decision",
                              r["decision_ref"], output_label=str(r["decision"])[:48],
                              latency_ms=r["latency_ms"], model=r["provider"],
                              features={"confidence": r["confidence"], "action": r["action"],
                                        "cached": bool(r["cached"])}))
    if run.steps:
        yield run
