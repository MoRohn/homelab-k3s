"""Agent loop controller (spec §39–§44, §70–§71).

    GOAL → STATE → which tool?  (code / Jev over the CURRENTLY valid tools)
         → is this invocation permitted?  (ToolPolicy: deterministic, never a model)
         → EXECUTE → STATE UPDATE → goal satisfied? (Jev) AND acceptance checks (code)
         → loop | VERIFY → COMPLETE

"What should run?" may be probabilistic. "May it run?" is not: ToolPolicy enforces
command allowlists, filesystem scope, network and privilege rules, and irreversible
tools need an explicit human approval callback. A probabilistic done-check never closes
a workflow alone: every acceptance check must pass too (§43).

Every step is written to the trace (lif.decision.instrument format) so the decision
miner can measure this loop like any other agent.
"""
from __future__ import annotations

import asyncio
import fnmatch
import inspect
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from lif.common import log
from lif.decision.instrument import TraceWriter
from lif.decision.sdk import Decision, Intelligence

LOG = log.get("lif.decision.agent_loop")


@dataclass
class Tool:
    name: str
    description: str
    run: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any] | Any]   # (args, state) → result
    args: Callable[[dict[str, Any]], Awaitable[dict] | dict] | None = None    # build args (code or generate())
    available: Callable[[dict[str, Any]], bool] = lambda state: True
    permission: str = "read"           # read | write | network | exec | irreversible
    command: Callable[[dict], str] | None = None      # for exec tools: the command line the policy checks
    paths: Callable[[dict], list[str]] | None = None  # filesystem paths touched


@dataclass
class ToolPolicy:
    """Deterministic authorization (§41, §70). Default-deny for anything not listed."""
    allowed_permissions: set[str] = field(default_factory=lambda: {"read"})
    command_allowlist: list[str] = field(default_factory=list)     # glob on the program + args
    command_denylist: list[str] = field(default_factory=lambda: [
        r"\brm\s+-[a-z]*[rf]", r"\bsudo\b", r"\bgit\s+push\b", r"\bkubectl\s+(delete|drain|apply)\b",
        r"\bdocker\s+(rm|restart|stop)\b", r"\bsystemctl\b", r"curl\s+[^|]*\|\s*(ba)?sh", r"\bchmod\s+-R\b",
        r"[;&|]\s*(rm|sudo|curl|wget)\b"])
    fs_roots: list[str] = field(default_factory=list)              # writes/reads must stay under these
    fs_deny: list[str] = field(default_factory=lambda: ["*/secrets/*", "*/private/*", "*/personal/*",
                                                        "*.env*", "*/.ssh/*", "*/.kube/*"])
    network: bool = False
    approve: Callable[[Tool, dict], Awaitable[bool] | bool] | None = None   # human gate for irreversible

    async def check(self, tool: Tool, args: dict) -> tuple[bool, str]:
        if tool.permission == "irreversible":
            if self.approve is None:
                return False, "irreversible tool: no human approval channel"
            ok = self.approve(tool, args)
            ok = await ok if inspect.isawaitable(ok) else ok
            return (True, "approved by human") if ok else (False, "human declined")
        if tool.permission not in self.allowed_permissions:
            return False, f"permission {tool.permission!r} not granted"
        if tool.permission == "network" and not self.network:
            return False, "network access not granted"
        if tool.command is not None:
            cmd = tool.command(args)
            for pat in self.command_denylist:
                if re.search(pat, cmd):
                    return False, f"command matches deny rule {pat!r}"
            try:
                argv = shlex.split(cmd)
            except ValueError:
                return False, "unparseable command"
            if not argv or not any(fnmatch.fnmatch(" ".join(argv), g) for g in self.command_allowlist):
                return False, "command not on the allowlist"
        if tool.paths is not None:
            for p in tool.paths(args):
                rp = str(Path(p).expanduser().resolve())
                if any(fnmatch.fnmatch(rp, g) for g in self.fs_deny):
                    return False, f"path {p} is denied"
                if self.fs_roots and not any(rp == r or rp.startswith(r.rstrip("/") + "/")
                                             for r in (str(Path(x).expanduser().resolve()) for x in self.fs_roots)):
                    return False, f"path {p} is outside the allowed roots"
        return True, "policy ok"


Check = Callable[[dict[str, Any]], Awaitable[tuple[bool, str]] | tuple[bool, str]]


@dataclass
class LoopResult:
    status: str                        # complete | blocked | budget_exhausted | needs_human | failed
    steps: int
    state: dict[str, Any]
    decisions: list[dict]
    checks: list[dict]
    elapsed_ms: float


class AgentLoop:
    def __init__(self, intelligence: Intelligence, tools: list[Tool], policy: ToolPolicy, *, agent: str,
                 workflow: str = "", acceptance: list[Check] | None = None, max_steps: int = 20,
                 trace: TraceWriter | None = None, data_class: str = "CONFIDENTIAL",
                 select_decision: str = "tool-selection", done_decision: str = "goal-satisfied"):
        self.intel, self.tools, self.policy = intelligence, {t.name: t for t in tools}, policy
        self.agent, self.workflow, self.acceptance = agent, workflow, acceptance or []
        self.max_steps, self.trace, self.data_class = max_steps, trace, data_class
        self.select_decision, self.done_decision = select_decision, done_decision

    def valid_tools(self, state: dict) -> dict[str, str]:
        """Rebuilt every iteration (§40): only tools that exist, are permitted by kind, and
        are usable in the current state are offered to the decision."""
        out = {}
        for t in self.tools.values():
            if t.permission not in self.policy.allowed_permissions and t.permission != "irreversible":
                continue
            try:
                if t.available(state):
                    out[t.name] = t.description
            except Exception:
                continue
        return out

    async def run(self, goal: str, state: dict[str, Any] | None = None) -> LoopResult:
        t0 = time.perf_counter()
        st: dict[str, Any] = {"goal": {"text": goal}, "progress": {"last_step": "", "last_result_summary": "",
                                                                 "artifacts": [], "checks": [],
                                                                 "remaining_requirements": []}, **(state or {})}
        decisions: list[dict] = []
        checks: list[dict] = []
        status = "budget_exhausted"
        for step in range(1, self.max_steps + 1):
            # DONE? — probabilistic judgment, then deterministic verification
            done = await self._decide(self.done_decision, st, step)
            decisions.append(_d(self.done_decision, done))
            if done.answer == "done" and done.actionable:
                ok, checks = await self._verify(st)
                if ok:
                    status = "complete"
                    break
                st["progress"]["checks"] = checks
                st["progress"]["last_result_summary"] = "acceptance checks failed: " + "; ".join(
                    c["detail"] for c in checks if not c["ok"])
            elif done.answer == "blocked" and done.actionable:
                status = "blocked"
                break
            elif not done.actionable and done.route in ("human", "advisory"):
                status = "needs_human"
                break
            # WHICH TOOL? over the live option set
            valid = self.valid_tools(st)
            if not valid:
                status = "blocked"
                break
            choice = await self._decide(self.select_decision, st, step, choices={**valid})
            decisions.append(_d(self.select_decision, choice))
            if not choice.actionable or choice.answer not in self.tools:
                status = "needs_human" if not choice.actionable else "blocked"
                if choice.answer == "none" and choice.actionable:
                    continue                        # nothing to run: re-check completion
                break
            tool = self.tools[choice.answer]
            args = await _call(tool.args, st) if tool.args else {}
            allowed, why = await self.policy.check(tool, args)
            self._trace(step, "policy", f"policy:{tool.name}", output="allow" if allowed else "deny")
            if not allowed:
                st["progress"].update(last_step=tool.name, last_result_summary=f"denied by policy: {why}")
                decisions.append({"decision": "tool-policy", "answer": "deny", "reason": why})
                continue
            t1 = time.perf_counter()
            try:
                result = await _call(tool.run, args, st)
                err = False
            except Exception as e:                  # tool failure is state, not a crash
                result, err = f"error: {type(e).__name__}: {str(e)[:200]}", True
            self._trace(step, "tool", tool.name, latency_ms=(time.perf_counter() - t1) * 1000, is_error=err)
            st["progress"].update(last_step=tool.name, last_result_summary=str(result)[:1000])
            if isinstance(result, dict) and result.get("artifact"):
                st["progress"]["artifacts"].append(result["artifact"])
        return LoopResult(status=status, steps=step, state=st, decisions=decisions, checks=checks,
                          elapsed_ms=(time.perf_counter() - t0) * 1000)

    async def _decide(self, name: str, st: dict, step: int, **kw) -> Decision:
        d = await self.intel.decide(name, st, agent=self.agent, workflow=self.workflow, data_class=self.data_class,
                                    **kw)
        self._trace(step, "decision", d.decision_version, output=d.answer, latency_ms=d.latency_ms,
                    features={"executor": d.executor, "route": d.route, "confidence": round(d.confidence, 4)})
        return d

    async def _verify(self, st: dict) -> tuple[bool, list[dict]]:
        out = []
        for chk in self.acceptance:
            try:
                ok, detail = await _call(chk, st)
            except Exception as e:
                ok, detail = False, f"check crashed: {type(e).__name__}"
            out.append({"check": getattr(chk, "__name__", "check"), "ok": bool(ok), "detail": detail})
        if not self.acceptance:
            out.append({"check": "acceptance", "ok": False,
                        "detail": "no deterministic acceptance checks configured; a done-check alone cannot close"})
        return all(c["ok"] for c in out), out

    def _trace(self, step: int, kind: str, name: str, **kw) -> None:
        if self.trace is not None:
            self.trace.step(run_id=self.trace.run_id, agent=self.agent, workflow=self.workflow, kind=kind,
                            name=name, **kw)


async def _call(fn, *a):
    out = fn(*a)
    return await out if inspect.isawaitable(out) else out


def _d(name: str, d: Decision) -> dict:
    return {"decision": name, "answer": d.answer, "confidence": round(d.confidence, 4), "executor": d.executor,
            "route": d.route, "version": d.decision_version}


async def goal_satisfied(intel: Intelligence, state: dict, acceptance: list[Check], **kw) -> dict:
    """Reusable completion capability (§42–§43): Jev's bounded status AND deterministic checks."""
    d = await intel.decide("goal-satisfied", state, **kw)
    results = []
    for chk in acceptance:
        ok, detail = await _call(chk, state)
        results.append({"ok": bool(ok), "detail": detail})
    complete = d.answer == "done" and d.actionable and bool(acceptance) and all(r["ok"] for r in results)
    return {"complete": complete, "status": d.answer, "confidence": d.confidence, "executor": d.executor,
            "route": d.route, "checks": results,
            "reason": "complete" if complete else ("no acceptance checks" if not acceptance else
                                                   "done-check or acceptance checks not satisfied")}


__all__ = ["AgentLoop", "Tool", "ToolPolicy", "LoopResult", "goal_satisfied", "asyncio"]
