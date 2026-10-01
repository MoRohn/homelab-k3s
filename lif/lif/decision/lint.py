"""decision lint — static checks for Jev question definitions (spec §12, §36, §37).

Deterministic and offline. Severities:
  error    blocks TESTED and later stages (registry.promotion_blockers reads lint_errors)
  warning  should be fixed or justified in the decision's README
  info     advice

Each finding names a code so a definition can acknowledge one deliberately:
    lint_ack: [vague-adjective]      # in the spec, with a reason in README.md
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from lif.decision.types import DecisionDef

EXIT_LABELS = {"other", "none", "unknown", "insufficient_information", "not_applicable", "uncertain", "abstain",
               "neither", "no_tool", "human"}
VAGUE = {"serious", "good", "bad", "appropriate", "reasonable", "significant", "important", "interesting",
         "nice", "proper", "decent", "meaningful", "substantial", "adequate", "excessive", "seems", "seem",
         "probably", "kind of", "sort of", "high-quality", "low-quality", "trustworthy", "legit", "fishy"}
JUDGMENT_ADJ = {"relevant", "trustworthy", "accurate", "complete", "safe", "correct", "useful", "valid",
                "reliable", "allowed", "permitted", "urgent", "important", "appropriate", "recent", "credible",
                "harmful", "sufficient", "consistent", "done", "finished", "successful"}
NEGATIVE = re.compile(r"(?i)\b(unsafe|unsuitable|invalid|irrelevant|incorrect|incomplete|fail(?:s|ed|ure)?|"
                      r"harmful|dangerous|malicious|not\s+(?:relevant|done|safe|valid|complete|allowed)|"
                      r"should\s+(?:reject|block|deny)|reject(?:ed)?|bad)\b")
CALC = re.compile(r"(?i)\b(count (?:the|how|all|each|every|of)|how many|sum of|total of|add up|percentage|percent of|average|mean of|"
                  r"multiply|divide|calculate|compute|days? (?:between|since|until)|older than \d|"
                  r"before or after|later than|earlier than|greater than \d|less than \d)\b")
GENERATIVE = re.compile(r"(?i)\b(write|draft|generate|compose|explain|summari[sz]e|rewrite|translate|"
                        r"extract (?:the|all|a)|list (?:the|all)|describe|produce a)\b")
RELATIVE_LEVEL = re.compile(r"(?i)(more than (?:level|the previous|the last|\d)|less than (?:level|the next)|"
                            r"better than|worse than|same as (?:level|above|below)|previous level|next level|"
                            r"higher than|lower than|between (?:level|the)|as above|see above|"
                            r"like (?:level|the previous))")
PRONOUN_START = re.compile(r"(?i)^\s*(?:is|does|did|was|are|can|should|will)\s+(?:it|this|that|they|these|those)\b")
STATE_PATH = re.compile(r"`([A-Za-z_][\w]*(?:\.[\w\[\]]+)*)`")
MULTI_Q = re.compile(r"\?.*\?", re.S)


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    where: str = "instructions"

    def to_dict(self) -> dict:
        return asdict(self)


def lint(d: DecisionDef, package_dir: str | Path | None = None, acks: list[str] | None = None) -> list[Finding]:
    f: list[Finding] = []
    add = lambda code, sev, msg, where="instructions": f.append(Finding(code, sev, msg, where))  # noqa: E731
    text = (d.instructions or "").strip()
    criteria_text = " ".join([d.noul_criteria, *d.choices.values(), *d.level_descriptions.values()])
    everything = f"{text} {criteria_text}"

    # ── structure ──
    if d.type not in ("choice", "score", "noul"):
        add("unknown-primitive", "error", f"primitive must be choice, score or noul, not {d.type!r}", "type")
    if len(text) < 40:
        add("thin-instructions", "error", "instructions are under 40 characters; the decision name is metadata, "
            "all meaning must be in instructions/criteria (rule 1)")
    if d.name.replace("-", " ").lower() == text.lower().rstrip("?. "):
        add("name-as-meaning", "error", "instructions only restate the decision id (rule 1)")
    if d.type == "choice":
        if len(d.choices) < 2:
            add("too-few-choices", "error", "a choice needs at least two options", "criteria")
        if len(d.choices) > 20:
            add("too-many-choices", "warning", f"{len(d.choices)} options; consider a two-stage choice", "criteria")
        for lab, desc in d.choices.items():
            if not str(desc).strip() or str(desc).strip().lower() == lab.replace("_", " ").lower():
                add("undescribed-choice", "error", f"choice {lab!r} has no standalone description", "criteria")
        if not (set(d.choices) & (EXIT_LABELS | set(d.exits))):
            add("no-exit", "warning", "choice list has no exit option (other / none / insufficient_information); "
                "list it under `exits:` if it is named differently (rule 5)", "criteria")
        for e in d.exits:
            if e not in d.choices:
                add("bad-exit", "error", f"exit {e!r} is not one of the choices", "exits")
    if d.type == "score":
        if len(d.levels) < 2:
            add("too-few-levels", "error", "a score needs at least two levels", "criteria")
        if not d.level_descriptions:
            add("unlabelled-levels", "warning", "score levels have names only; give each level an independent "
                "observable description (criteria: {level: description})", "criteria")
        for lv, desc in d.level_descriptions.items():
            if RELATIVE_LEVEL.search(desc):
                add("relative-level", "error", f"level {lv!r} is defined relative to another level; each level "
                    "must stand on its own", "criteria")
            if len(desc.strip()) < 12:
                add("thin-level", "warning", f"level {lv!r} description is too short to be observable", "criteria")
    if d.default is not None and d.default not in d.labels:
        add("bad-default", "error", f"default {d.default!r} is not a label ({d.labels})", "default")
    if d.default is None:
        add("no-default", "warning", "no safe default for when every provider fails", "default")

    # ── question design ──
    if d.type == "noul" and NEGATIVE.search(re.split(r"(?<=[.?!])\s", text)[0]):   # the question, not its criteria
        add("inverted-noul", "warning", "noul is phrased around a negative condition; high probability must mean "
            "the desirable/affirmative 'yes' where avoidable (e.g. ask 'is it safe' not 'is it unsafe')")
    for a, b in re.findall(r"(?i)\b(\w+)\s+(?:and|or|and/or)\s+(\w+)\b", text):
        if a.lower() in JUDGMENT_ADJ and b.lower() in JUDGMENT_ADJ and a.lower() != b.lower():
            add("compound-judgment", "error", f"'{a} and/or {b}' asks two judgments in one question; split them "
                "and combine in code (rule 2)")
    if MULTI_Q.search(text):
        add("multiple-questions", "error", "more than one question mark; ask one judgment per decision (rule 2)")
    vague = sorted({w for w in VAGUE if re.search(rf"(?i)\b{re.escape(w)}\b", everything)})
    if vague:
        add("vague-adjective", "warning", f"vague terms {vague}; describe observable conditions instead (rule 4)")
    if CALC.search(everything):
        add("asks-calculation", "error", "asks Jev to count/compare/compute; calculate in code and pass the value "
            "in state (rule 6)")
    if GENERATIVE.search(text):
        add("generative-ask", "error", "asks for generated text or extraction; that is generation, not a bounded "
            "decision (rule 8 / §36)")
    if PRONOUN_START.search(text) and not STATE_PATH.search(text):
        add("undefined-pronoun", "warning", "starts with an undefined pronoun ('is it…'); point at the state, "
            "e.g. 'Judge whether `source.text` …' (rule 3)")
    paths = set(STATE_PATH.findall(everything)) - set(d.labels)     # `label` mentions are not state
    if not paths:
        add("no-state-path", "warning", "instructions do not point at any state field in `backticks` (rule 3)")
    if d.state_schema:
        unknown = sorted(p for p in paths if p.split("[")[0] not in d.state_schema
                         and not any(p.startswith(k + ".") or k.startswith(p + ".") for k in d.state_schema))
        if unknown:
            add("missing-state-path", "error", f"references {unknown} which are not in state_schema", "state_schema")
        unused = sorted(k for k in d.state_schema if not any(p == k or p.startswith(k) or k.startswith(p)
                                                             for p in paths))
        if unused:
            add("unused-state", "info", f"state_schema fields {unused} are never referenced; the state compiler "
                "will still send them (rule 7)", "state_schema")
    else:
        add("no-state-schema", "warning", "no state_schema: the state compiler cannot reduce state (rule 7)",
            "state_schema")

    # ── governance ──
    if not d.owner or d.owner == "platform" and d.package:
        add("no-owner", "info", "owner is the generic 'platform'", "owner")
    if d.risk not in ("low", "medium", "high", "critical"):
        add("bad-risk", "error", f"risk must be low|medium|high|critical, not {d.risk!r}", "risk")
    if (d.risk == "critical" or not d.reversible) and d.stage in ("low_risk_automation", "expanded_automation",
                                                                  "production"):
        add("automated-irreversible", "error", "critical or irreversible decisions may not be automated; they "
            "belong to human review or hard policy (§70)", "stage")
    if package_dir is not None:
        pd = Path(package_dir)
        tests = pd / "tests.jsonl"
        if not tests.exists():
            add("no-tests", "warning", "no tests.jsonl next to the spec", "tests")
        if not (pd / "README.md").exists():
            add("no-readme", "info", "no README.md explaining why this decision exists", "readme")

    acked = set(acks or [])
    return [x for x in f if x.code not in acked]


def summarize(findings: list[Finding]) -> dict:
    return {s: sum(1 for x in findings if x.severity == s) for s in ("error", "warning", "info")}


def lint_path(path: str | Path) -> dict[str, list[Finding]]:
    """Lint a spec file, a decision directory, a package tree, or a legacy list file."""
    import yaml
    p = Path(path)
    files = sorted(p.rglob("v*.yaml")) + sorted(x for x in p.glob("*.yaml") if not x.name.startswith("v")) \
        if p.is_dir() else [p]
    out: dict[str, list[Finding]] = {}
    for f in files:
        raw = yaml.safe_load(f.read_text()) or []
        for item in (raw if isinstance(raw, list) else [raw]):
            acks = list(item.pop("lint_ack", []) or [])
            try:
                d = DecisionDef.from_raw(item)
            except (TypeError, ValueError) as e:
                out[f"{f}:{item.get('name', '?')}"] = [Finding("invalid-spec", "error", str(e), "file")]
                continue
            out[d.ref] = lint(d, f.parent if not isinstance(raw, list) else None, acks)
    return out
