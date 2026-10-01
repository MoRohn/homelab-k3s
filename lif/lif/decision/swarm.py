"""Swarm coordination behind an abstraction (spec §47–§49).

Kimi's API has no swarm endpoint (checked 2026-10-01: platform.kimi.ai chat API), so the
swarm is client-side and provider-neutral:

    PLAN (generate)  → WORKERS (parallel callables) → RESULT POOL
      → CODE filter (validators) → JEV triage per result (keep / drop / review), one state each
      → rank survivors by P(keep) → SYNTHESIS (generate, on the small set) → next round?

Jev is the control plane for bounded swarm questions (keep this result? goal met?); a
heavy model is never used to decide which worker runs when a bounded decision suffices.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from lif.decision.sdk import Intelligence

Worker = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class SwarmRound:
    produced: int
    after_code: int
    kept: int
    review: int
    dropped: int
    synthesis: str | None = None
    jev_requests: int = 0


@dataclass
class SwarmResult:
    rounds: list[SwarmRound] = field(default_factory=list)
    final: str | None = None
    kept: list[Any] = field(default_factory=list)
    status: str = "incomplete"


class Swarm:
    def __init__(self, intel: Intelligence, *, triage_decision: str = "result-keep",
                 done_decision: str = "goal-satisfied", max_keep: int = 12, concurrency: int = 32):
        self.intel, self.triage, self.done = intel, triage_decision, done_decision
        self.max_keep, self.sem = max_keep, asyncio.Semaphore(concurrency)

    async def run(self, goal: str, tasks: list[dict], worker: Worker, *,
                  code_filter: Callable[[Any], bool] = lambda r: r is not None,
                  summarize: Callable[[Any], str] = lambda r: str(r)[:1500],
                  synthesize: Callable[[str, list[Any]], Awaitable[str]] | None = None,
                  next_tasks: Callable[[list[Any], SwarmRound], list[dict]] | None = None,
                  max_rounds: int = 3, data_class: str = "CONFIDENTIAL") -> SwarmResult:
        out = SwarmResult()
        pool: list[Any] = []
        for _ in range(max_rounds):
            async def w(t):
                async with self.sem:
                    try:
                        return await worker(t)
                    except Exception:
                        return None
            produced = await asyncio.gather(*(w(t) for t in tasks))
            passed = [r for r in produced if code_filter(r)]                         # CODE first
            decisions = await asyncio.gather(*(self.intel.decide(
                self.triage, {"goal": {"text": goal}, "result": {"summary": summarize(r)}},
                data_class=data_class, agent="swarm") for r in passed))
            scored = sorted(zip(passed, decisions), key=lambda x: -x[1].probabilities.get("keep", x[1].confidence
                                                                                           if x[1].answer == "keep"
                                                                                           else 0))
            keep = [r for r, d in scored if d.answer == "keep" and d.actionable][: self.max_keep]
            review = [r for r, d in scored if not d.actionable]
            rnd = SwarmRound(produced=len(produced), after_code=len(passed), kept=len(keep), review=len(review),
                             dropped=len(passed) - len(keep) - len(review), jev_requests=len(passed))
            pool = (pool + keep)[: self.max_keep]
            if synthesize is not None and pool:
                rnd.synthesis = await synthesize(goal, pool)                         # generation on a SMALL set
            out.rounds.append(rnd)
            done = await self.intel.decide(self.done, {"goal": {"text": goal}, "progress": {
                "artifacts": [summarize(r)[:300] for r in pool], "checks": [], "remaining_requirements": [],
                "last_result_summary": (rnd.synthesis or "")[:1000]}}, data_class=data_class, agent="swarm")
            if done.answer == "done" and done.actionable:
                out.status = "complete"
                break
            if next_tasks is None:
                break
            tasks = next_tasks(pool, rnd)
            if not tasks:
                break
        out.kept, out.final = pool, (out.rounds[-1].synthesis if out.rounds else None)
        return out
