"""Trace segmentation and the four-bucket classifier (spec §4–§6, §36).

segment(run) breaks a run into atomic Operations. One LLM call often fuses several
operations (decide whether to continue, choose a tool, write its arguments, write prose);
each becomes its own Operation, and the call's tokens are attributed honestly:

  * generative operations in the call carry its cost
  * a bounded decision carries the cost only when nothing generative shares the call
    (`separable`); otherwise moving it to Jev would not remove the heavy call

classify(op) assigns CODE / JEV_CANDIDATE / GENERATIVE / HUMAN_OR_POLICY / UNKNOWN with a
confidence and the reasons, plus §36 weakness flags that veto Jev. Cluster-level evidence
(the output domain across many runs) is added later by the miner.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from lif.decision.mining.traces import Run, Step

CODE, JEV, GEN, HUMAN, UNKNOWN = "CODE", "JEV_CANDIDATE", "GENERATIVE", "HUMAN_OR_POLICY", "UNKNOWN"
BUCKETS = [CODE, JEV, GEN, HUMAN, UNKNOWN]

# tools whose *choice* is inherently a human/policy step
HUMAN_TOOLS = {"askuserquestion", "exitplanmode", "enterplanmode"}
# tools with no or trivially selectable arguments (selection IS the whole call)
NO_ARG_TOOLS = {"listagents", "taskstop", "enterworktree", "exitworktree", "cronlist"}


@dataclass
class Operation:
    id: str
    run_id: str
    seq: int
    ts: float
    agent: str
    workflow: str
    source: str
    kind: str                      # see KINDS
    name: str
    executor: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float | None = None
    output_label: str = ""
    reversible: bool = True
    bounded: bool | None = None
    separable: bool = False
    classification: str = UNKNOWN
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    detections: list[str] = field(default_factory=list)       # §6 hidden-decision signals
    candidate: dict[str, Any] = field(default_factory=dict)    # {primitive, decision}
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        """Cluster key: the same kind of operation in the same agent."""
        return f"{self.agent}:{self.kind}:{self.name}" if self.kind not in ("select_tool", "decide_continue",
                                                                          "judge_result") \
            else f"{self.agent}:{self.kind}"

    def to_dict(self) -> dict:
        return {**asdict(self), "signature": self.signature}


KINDS = {
    "interpret_task": "read the user's request and form a plan",
    "decide_continue": "decide whether the task is done or another step is needed",
    "select_tool": "choose which tool runs next",
    "compose_tool_input": "write the arguments for the chosen tool",
    "authorize_action": "decide whether a consequential action may run",
    "execute_tool": "run a tool",
    "judge_result": "judge whether a tool result is usable or needs a retry",
    "write_response": "write prose for the user",
    "delegate": "choose a sub-agent",
    "ask_human": "ask the human",
    "decision": "an existing typed decision",
    "llm_call": "an LLM call of unknown purpose",
}


def _oid(*parts: Any) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def segment(run: Run) -> list[Operation]:
    if run.source == "claude_code":
        return _segment_claude_code(run)
    return _segment_generic(run)


def _op(s: Step, kind: str, name: str, executor: str, **kw) -> Operation:
    return Operation(id=_oid(s.run_id, s.seq, kind, name, kw.get("output_label", "")), run_id=s.run_id, seq=s.seq,
                     ts=s.ts, agent=s.agent + ("/subagent" if s.features.get("sidechain") else ""),
                     workflow=s.workflow, source=s.source, kind=kind, name=name, executor=executor, **kw)


def _segment_claude_code(run: Run) -> list[Operation]:
    ops: list[Operation] = []
    steps = run.steps
    after_prompt = False
    tool_by_seq = {s.seq: s for s in steps if s.kind == "tool"}
    for i, s in enumerate(steps):
        if s.kind == "user":
            after_prompt = True
            continue
        if s.kind == "llm":
            tools = [tool_by_seq[t.seq] for t in steps[i + 1:i + 1 + len(s.features.get("tools", []))]
                     if t.seq in tool_by_seq]
            text = int(s.features.get("text_chars", 0))
            heavy_args = [t for t in tools if t.name.lower() not in NO_ARG_TOOLS and t.features.get("arg_chars", 0) > 2]
            generative = bool(heavy_args) or text > 200 or after_prompt
            tin, tout = s.input_tokens, s.output_tokens
            # cost goes to the generative parts; decisions get it only when separable
            if after_prompt:
                ops.append(_op(s, "interpret_task", "task", s.model, input_tokens=tin, output_tokens=tout,
                               latency_ms=s.latency_ms, features={"thinking": s.features.get("thinking")}))
                after_prompt = False
            sep = not generative
            label = "continue" if tools else "done"
            ops.append(_op(s, "decide_continue", "goal", s.model, output_label=label, bounded=True, separable=sep,
                           input_tokens=tin if sep else 0, output_tokens=tout if sep else 0,
                           latency_ms=s.latency_ms if sep else None,
                           features={"fused_with": "tool_args" if heavy_args else ("prose" if text else "")}))
            for t in tools:
                name = t.name
                sep_t = sep or (name.lower() in NO_ARG_TOOLS and len(tools) == 1 and text <= 200)
                if name.lower() in HUMAN_TOOLS:
                    ops.append(_op(t, "ask_human", name, s.model, output_label="ask", features=t.features))
                    continue
                if name == "Agent":
                    ops.append(_op(t, "delegate", t.features.get("subagent", "default"), s.model,
                                   output_label=t.features.get("subagent", "default"), bounded=True,
                                   features=t.features))
                ops.append(_op(t, "select_tool", name, s.model, output_label=name.lower(), bounded=True,
                               separable=sep_t, input_tokens=tin if sep_t else 0,
                               output_tokens=tout if sep_t else 0, latency_ms=s.latency_ms if sep_t else None,
                               features=t.features))
                if t.features.get("destructive"):
                    ops.append(_op(t, "authorize_action", t.features.get("program", name), "permission-mode",
                                   reversible=False, features=t.features))
                if t in heavy_args:
                    share = max(1, len(heavy_args) + (1 if text > 200 else 0))
                    ops.append(_op(t, "compose_tool_input", name if name != "Bash" else
                                   f"bash:{t.features.get('program', '')}", s.model,
                                   input_tokens=tin // share, output_tokens=tout // share,
                                   latency_ms=(s.latency_ms or 0) / share if s.latency_ms else None,
                                   features=t.features))
            if text > 200 and not tools:
                ops.append(_op(s, "write_response", "answer", s.model, input_tokens=tin if not heavy_args else 0,
                               output_tokens=tout, latency_ms=s.latency_ms, features={"text_chars": text}))
        elif s.kind == "tool_result":
            prog = s.features.get("program", "")
            ops.append(_op(s, "execute_tool", s.name, s.name, latency_ms=s.latency_ms, output_label="error" if
                           s.is_error else "ok", features=s.features))
            nxt = next((x for x in steps[i + 1:] if x.kind == "tool"), None)
            nxt_llm = next((x for x in steps[i + 1:] if x.kind == "llm"), None)
            if nxt_llm is None:
                continue
            retry = bool(nxt and s.is_error and nxt.name == s.name and
                         nxt.features.get("program", "") == prog)
            ops.append(_op(s, "judge_result", s.name, nxt_llm.model, output_label="retry" if retry else "proceed",
                           bounded=True, features={**s.features, "runs_tests": tool_by_seq.get(
                               s.seq - 1, s).features.get("runs_tests", False)}))
    return ops


def _segment_generic(run: Run) -> list[Operation]:
    ops = []
    for s in run.steps:
        if s.kind == "decision":
            ops.append(_op(s, "decision", s.name, s.model or "jev", output_label=s.output_label, bounded=True,
                           latency_ms=s.latency_ms, features=s.features))
        elif s.kind in ("tool", "mcp"):
            ops.append(_op(s, "execute_tool", s.name, s.name, latency_ms=s.latency_ms, features=s.features,
                           output_label="error" if s.is_error else "ok"))
        elif s.kind == "human":
            ops.append(_op(s, "ask_human", s.name, "human", features=s.features))
        elif s.kind == "policy":
            ops.append(_op(s, "authorize_action", s.name, "policy-engine", output_label=s.output_label,
                           features=s.features))
        elif s.kind == "llm":
            purpose = str(s.features.get("purpose") or s.name or "llm")
            ops.append(_op(s, "llm_call", purpose, s.model or s.name, input_tokens=s.input_tokens,
                           output_tokens=s.output_tokens, latency_ms=s.latency_ms, output_label=s.output_label,
                           separable=True, features=s.features))
    return ops


# ── classifier ───────────────────────────────────────────────────────────────

WEAKNESS_TAGS = {
    "arithmetic": ("calc", "sum", "total", "percent", "arithmetic", "math"),
    "date_reasoning": ("date", "deadline", "schedule", "calendar"),
    "counting": ("count", "how many", "tally"),
    "multi_hop": ("plan", "multi-hop", "research", "investigate"),
    "image": ("image", "vision", "screenshot", "photo"),
    "ocr": ("ocr", "scan", "pdf-scan"),
    "explanation": ("explain", "summary", "summarize", "report", "draft", "write"),
}


def classify(op: Operation) -> Operation:
    r: list[str] = []
    det: list[str] = []
    weak: list[str] = []
    f = op.features
    purpose = f"{op.name} {f.get('purpose', '')}".lower()
    for tag, keys in WEAKNESS_TAGS.items():
        if any(k in purpose for k in keys):
            weak.append(tag)
    if f.get("has_image"):
        weak.append("image")
    if f.get("untrusted_input"):
        weak.append("untrusted_instructions")
    if not op.reversible:
        weak.append("irreversible")
    if op.input_tokens > 8000 and op.output_tokens and op.output_tokens < 16:
        det.append("excessive_state")          # huge context for a tiny answer: state-compiler target

    k = op.kind
    if k in ("authorize_action", "ask_human") or not op.reversible:
        b, c = HUMAN, 0.95
        r.append("irreversible or consequential: human approval / hard policy (§70)")
    elif k == "execute_tool":
        b, c = CODE, 0.99
        r.append("tool execution is deterministic software")
    elif k == "judge_result" and f.get("runs_tests"):
        b, c = CODE, 0.9
        r.append("test outcome is the exit code; check it in code (§43)")
    elif k == "judge_result":
        b, c = JEV, 0.7
        det.append("accepts/rejects results")
        r.append("bounded verdict (proceed / retry) on a tool result")
    elif k == "decide_continue":
        b, c = JEV, 0.8
        det.append("evaluates completion")
        r.append("bounded: done / continue / blocked; pair with deterministic acceptance checks (§43)")
    elif k == "select_tool":
        b, c = JEV, 0.85
        det.append("chooses among tools")
        r.append("finite tool set known before the call; selection beats generation (rule 8)")
    elif k == "delegate":
        b, c = JEV, 0.8
        det.append("selects among routes")
        r.append("finite set of sub-agent types")
    elif k in ("compose_tool_input", "write_response", "interpret_task"):
        b, c = GEN, 0.9
        r.append("creates new text/code/plan")
        if k == "compose_tool_input" and op.name.startswith("bash:") and op.name[5:] in ("git status", "ls",
                                                                                         "pwd", "date"):
            b, c = CODE, 0.6
            r.append("fixed read-only command; could be a deterministic call")
    elif k == "decision":
        b, c = JEV, 0.99
        r.append("already a typed decision")
    elif k == "llm_call":
        b, c = _classify_llm_call(op, r, det)
    else:
        b, c = UNKNOWN, 0.0
    if b == JEV and (set(weak) & {"arithmetic", "counting", "date_reasoning", "image", "ocr", "explanation",
                                  "irreversible"}):
        r.append(f"Jev weakness: {sorted(set(weak))} → route elsewhere (§36)")
        b, c = (HUMAN, 0.9) if "irreversible" in weak else ((CODE, 0.6) if set(weak) & {
            "arithmetic", "counting", "date_reasoning"} else (GEN, 0.7))
    if k == "llm_call" and b in (GEN, UNKNOWN) and set(weak) & {"arithmetic", "counting", "date_reasoning"}:
        b, c = CODE, 0.6
        r.append("counting/arithmetic/date work: compute it in code and pass the value in state (rule 6)")
    op.classification, op.confidence, op.reasons = b, round(c, 3), r
    op.detections, op.weaknesses = sorted(set(det)), sorted(set(weak))
    if b == JEV and op.bounded is None:
        op.bounded = True
    return op


def _classify_llm_call(op: Operation, r: list[str], det: list[str]) -> tuple[str, float]:
    f = op.features
    ot = str(f.get("output_type", ""))
    if ot in ("label", "bool", "enum", "choice", "score") or (op.output_label and op.output_tokens <= 8):
        det.append("output can be enumerated beforehand")
        if op.output_label in ("yes", "no", "true", "false", "done", "not_done"):
            det.append("answer is effectively boolean")
        r.append("short, label-like output from an LLM call")
        op.bounded = True
        return JEV, 0.75
    if f.get("candidates_n"):
        det.append("ranks/selects known candidates")
        r.append(f"selects among {f['candidates_n']} known candidates")
        op.bounded = True
        return JEV, 0.75
    if ot in ("text", "code", "markdown") or op.output_tokens > 120:
        r.append("long free-form output")
        return GEN, 0.8
    r.append("not enough evidence; decided at cluster level")
    return UNKNOWN, 0.3
