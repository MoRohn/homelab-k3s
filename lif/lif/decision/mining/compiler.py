"""decision-compiler: inventory item → versioned candidate Jev spec (spec §9–§12, §85).

    TRACE PATTERN → primitive selection → instructions/criteria from templates or the
    observed output domain → state requirements → test skeleton → lint → DRAFT package

Output is a draft for review (stage `discovered`, or `designed` when lint finds no errors).
It never enters shadow without a human (registry gates). Mined drafts are written under
private/lif/decisions/ by default because they derive from real traces.

When the agent-core package already has a decision for the pattern, the compiler
proposes reusing it (with a domain extension) instead of a new schema (§87).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lif.decision.lint import lint, summarize
from lif.decision.mining.miner import InventoryItem
from lif.decision.types import DecisionDef

# Mined pattern kind → shared decision in decision-packages/agent-core (reuse, §87)
SHARED = {
    "select_tool": "tool-selection",
    "decide_continue": "goal-satisfied",
    "judge_result": "result-usable",
    "delegate": "delegate-route",
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "select_tool": {
        "primitive": "choice",
        "instructions": "Choose the tool that should run next to make progress on `goal.text`, given "
                        "`progress.last_step` and `progress.last_result_summary`. Only the tools listed in the "
                        "options are available; pick `none` when the next step needs no tool.",
        "state_schema": {"goal.text": "str", "progress.last_step": "str", "progress.last_result_summary": "str"},
        "dynamic_choices": True,
    },
    "decide_continue": {
        "primitive": "choice",
        "instructions": "Judge the status of `goal.text` given `progress.artifacts`, `progress.checks` and "
                        "`progress.remaining_requirements`.",
        "criteria": {
            "done": "every item in `progress.remaining_requirements` is resolved and `progress.checks` shows no "
                    "failing check",
            "not_done": "at least one requirement in `progress.remaining_requirements` is still open and the "
                        "agent can act on it",
            "blocked": "an open requirement needs something the agent cannot obtain: a missing permission, a "
                       "human answer, or an unavailable service named in `progress.last_result_summary`",
            "uncertain": "the state does not show whether the open requirements are resolved",
        },
        "state_schema": {"goal.text": "str", "progress.artifacts": "list", "progress.checks": "list",
                         "progress.remaining_requirements": "list", "progress.last_result_summary": "str"},
        "exits": ["uncertain"],
    },
    "judge_result": {
        "primitive": "noul",
        "instructions": "Judge whether `result.summary` gives the agent what `step.intent` needed in order to "
                        "continue.",
        "criteria": "`result.summary` contains the information or confirmation named in `step.intent`, and "
                    "`result.status` is not an error.",
        "state_schema": {"step.intent": "str", "result.summary": "str", "result.status": "str"},
    },
    "delegate": {
        "primitive": "choice",
        "instructions": "Choose which kind of sub-agent should take `task.text`. Only the agent types listed "
                        "in the options exist.",
        "state_schema": {"task.text": "str"},
        "dynamic_choices": True,
    },
}


@dataclass
class Candidate:
    spec: dict[str, Any]
    lint: dict[str, int]
    findings: list[dict]
    reuse: str = ""                       # shared decision to reuse instead, if any
    notes: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.spec['name']}/{self.spec['version']}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "decision"


def compile_item(item: InventoryItem, *, package: str | None = None, owner: str = "") -> Candidate:
    tpl = TEMPLATES.get(item.kind)
    notes: list[str] = []
    reuse = SHARED.get(item.kind, "")
    agent = _slug(item.agent.split("/")[0])
    if tpl is not None:
        name = f"{agent}-{_slug(item.kind.replace('_', '-'))}" if not reuse else f"{reuse}-{agent}"
        spec: dict[str, Any] = {"name": name, "version": "v1", "primitive": tpl["primitive"],
                                "instructions": tpl["instructions"], "state_schema": dict(tpl["state_schema"])}
        if tpl.get("criteria"):
            spec["criteria"] = tpl["criteria"]
        if tpl.get("exits"):
            spec["exits"] = list(tpl["exits"])
        if tpl["primitive"] == "choice" and "criteria" not in spec:
            spec["criteria"] = _observed_choices(item)
            notes.append("choices come from the observed output domain; at run time the caller passes the live "
                         "option set (dynamic choices, §39) and the spec's list is the superset")
        if reuse:
            notes.append(f"agent-core/{reuse} already defines this decision; prefer reusing it with "
                         f"agent-specific state over a new schema (§87)")
    else:
        name = f"{agent}-{_slug(item.name)}"
        labels = [lab for lab, _ in item.top_outputs]
        if item.recommended_primitive == "noul":
            spec = {"name": name, "version": "v1", "primitive": "noul",
                    "instructions": f"Judge whether `input` satisfies the condition the agent currently checks "
                                    f"with an LLM in step '{item.name}'.",
                    "criteria": "REVIEW: describe the observable condition for 'yes'"}
        else:
            spec = {"name": name, "version": "v1", "primitive": "choice",
                    "instructions": f"Choose the category for `input` that the agent currently produces with an "
                                    f"LLM in step '{item.name}'.",
                    "criteria": {lab: f"REVIEW: observable condition for '{lab}'" for lab in labels} | {
                        "other": "none of the categories above applies"}}
        spec["state_schema"] = {"input": "str"}
        notes.append("generic template: instructions and criteria need a human pass before tests are written")
    spec.update({
        "stage": "discovered", "risk": item.risk if item.risk != "critical" else "critical",
        "reversible": item.reversible, "owner": owner or item.agent.split("/")[0],
        "data_class": "CONFIDENTIAL", "default": _safe_default(spec),
        "derived_from": item.signature,
        "description": f"Mined from {item.n} operations across {item.runs} runs ({item.calls_per_day}/day). "
                       f"Current executor: {item.executor}.",
        "fallback": ["rules", "local_llm"],
    })
    if package:
        spec["package"] = package
    d = DecisionDef.from_raw({k: v for k, v in spec.items() if k != "dynamic_choices"})
    fs = lint(d)
    s = summarize(fs)
    if not s["error"]:
        spec["stage"] = "designed"
    if item.embedded:
        notes.append(f"only {item.separable_share:.0%} of these calls are separable: the decision is fused with "
                     f"generation in one LLM call. Savings need the agent loop split into decide() then generate() "
                     f"(lif.decision.agent_loop), not just a Jev swap")
    if item.existing_methods:
        notes.append(f"an existing deterministic method may make this CODE instead: {item.existing_methods}")
    if getattr(item, "related_knowledge", None):
        notes.append(f"read before writing criteria (knowledge layer): {item.related_knowledge}")
    return Candidate(spec=spec, lint=s, findings=[f.to_dict() for f in fs], reuse=reuse, notes=notes)


def _observed_choices(item: InventoryItem) -> dict[str, str]:
    out = {lab: f"REVIEW: when the next step needs '{lab}'" for lab, _ in item.top_outputs}
    out.setdefault("none", "no option applies; the next step needs no tool or agent")
    return out


def _safe_default(spec: dict) -> str:
    if spec["primitive"] == "noul":
        return "no"
    crit = spec.get("criteria") or {}
    for e in ("uncertain", "none", "other", "insufficient_information"):
        if e in crit:
            return e
    return next(iter(crit), "other")


def write_package(c: Candidate, root: str | Path, evidence: dict | None = None) -> Path:
    """decisions/<name>/{v1.yaml, tests.jsonl, README.md}. Never overwrites an existing version."""
    d = Path(root).expanduser() / c.spec["name"]
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{c.spec['version']}.yaml"
    if f.exists():
        raise FileExistsError(f"{f} exists; a change is a new version")
    spec = {k: v for k, v in c.spec.items() if k != "dynamic_choices"}
    f.write_text(yaml.safe_dump(spec, sort_keys=False, width=110, allow_unicode=True))
    tests = d / "tests.jsonl"
    if not tests.exists():
        tests.write_text("")
    readme = d / "README.md"
    if not readme.exists():
        lines = [f"# {c.spec['name']}", "", f"Draft generated by the decision compiler from `{c.spec['derived_from']}`.",
                 "", "| | |", "|---|---|", f"| Primitive | {c.spec['primitive']} |", f"| Stage | {c.spec['stage']} |",
                 f"| Lint | {c.lint} |", f"| Reuse instead | {c.reuse or '-'} |", "", "## Review notes", ""]
        lines += [f"- {n}" for n in c.notes] or ["- none"]
        lines += ["", "## Before shadow", "", "- [ ] criteria reviewed and observable",
                  "- [ ] ≥ 20 labelled cases in tests.jsonl (`{\"state\": {...}, \"expected\": \"label\"}`)",
                  "- [ ] `local-ai decision test` passes", ""]
        if evidence:
            lines += ["## Mining evidence", "", "```json", json.dumps(evidence, indent=2, default=str)[:4000],
                      "```", ""]
        readme.write_text("\n".join(lines))
    return d
