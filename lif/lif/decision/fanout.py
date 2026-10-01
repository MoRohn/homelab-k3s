"""Fan-out planner (spec §15–§17, §50): one Jev request per group of independent decisions
that can share one state; sequential calls where a decision needs another's answer.

    requests: [DecisionRequest(name, state, depends_on)]
      → waves in dependency order (never fabricated parallelism)
      → within a wave, group requests whose merged state stays small:
           tokens(merge) ≤ (1 + slack) × max(tokens(each))   (sharing must not bloat any
           single decision's context, rule 7) and the same data class
      → each group = ONE fabric.evaluate_group call

`saved_tokens` is the state that would have been re-sent without fan-out.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from lif.common import metrics
from lif.decision.state_compiler import tokens


@dataclass
class DecisionRequest:
    name: str
    state: Any
    depends_on: list[str] = field(default_factory=list)
    data_class: str | None = None
    # builds the state from earlier answers when depends_on is set: f(answers) -> state
    state_fn: Callable[[dict[str, Any]], Any] | None = None


@dataclass
class Group:
    names: list[str]
    state: Any
    data_class: str | None
    saved_tokens: int = 0


def _merge(a: Any, b: Any) -> Any | None:
    """Deep-merge two dict states; None when they conflict (same path, different value)."""
    if a == b:
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            if k in out:
                m = _merge(out[k], v)
                if m is None:
                    return None
                out[k] = m
            else:
                out[k] = v
        return out
    return None


def waves(reqs: list[DecisionRequest]) -> list[list[DecisionRequest]]:
    names = {r.name for r in reqs}
    for r in reqs:
        for dep in r.depends_on:
            if dep not in names:
                raise ValueError(f"{r.name} depends on {dep}, which is not in this plan")
    done: set[str] = set()
    out: list[list[DecisionRequest]] = []
    left = list(reqs)
    while left:
        ready = [r for r in left if all(d in done for d in r.depends_on)]
        if not ready:
            raise ValueError("dependency cycle among decisions: " + ", ".join(r.name for r in left))
        out.append(ready)
        done |= {r.name for r in ready}
        left = [r for r in left if r.name not in done]
    return out


def group_wave(wave: list[DecisionRequest], states: dict[str, Any], slack: float = 0.25) -> list[Group]:
    groups: list[Group] = []
    for r in wave:
        st = states[r.name]
        placed = False
        for g in groups:
            if g.data_class != r.data_class:
                continue
            m = _merge(g.state, st)
            if m is None:
                continue
            biggest = max(tokens(g.state), tokens(st))
            if tokens(m) <= (1 + slack) * biggest:
                g.saved_tokens += tokens(st) + tokens(g.state) - tokens(m)
                g.state, placed = m, True
                g.names.append(r.name)
                break
        if not placed:
            groups.append(Group(names=[r.name], state=st, data_class=r.data_class))
    return groups


async def run(fabric_eval: Callable, reqs: list[DecisionRequest], slack: float = 0.25) -> tuple[dict, dict]:
    """fabric_eval(names, state, data_class) -> {name: result}. Returns (results, stats)."""
    answers: dict[str, Any] = {}
    stats = {"waves": 0, "requests": 0, "decisions": len(reqs), "saved_tokens": 0, "groups": []}
    for wave in waves(reqs):
        stats["waves"] += 1
        states = {r.name: (r.state_fn(answers) if r.state_fn else r.state) for r in wave}
        for g in group_wave(wave, states, slack):
            got = await fabric_eval(g.names, g.state, g.data_class)
            answers.update(got)
            stats["requests"] += 1
            stats["saved_tokens"] += g.saved_tokens
            stats["groups"].append(g.names)
            metrics.fanout_size.observe(len(g.names))
            if g.saved_tokens:
                metrics.fanout_saved_tokens.inc(g.saved_tokens)
    return answers, stats


def state_digest(state: Any) -> str:
    return json.dumps(state, sort_keys=True, default=str)[:64]
