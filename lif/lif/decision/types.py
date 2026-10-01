"""The standard decision primitive shared by every provider (Jev, rules, local LLM)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lif.common import config
from lif.policy.engine import DataClass


@dataclass(frozen=True)
class DecisionDef:
    name: str
    version: str
    type: str                                  # choice | score | noul
    instructions: str
    choices: dict[str, str] = field(default_factory=dict)   # choice: label → description
    levels: list[str] = field(default_factory=list)         # score: low → high
    thresholds: dict[str, float] = field(default_factory=dict)
    data_class: str = "CONFIDENTIAL"           # class of the state this decision sees
    fallback: list[str] = field(default_factory=lambda: ["rules", "local_llm"])
    default: str | None = None                 # answer when every provider fails (safe choice)
    owner: str = "platform"
    cacheable: bool = True
    # ── decision-engineering fields (all optional; legacy entries are production) ──
    stage: str = "production"                  # see registry.STAGES
    risk: str = "low"                          # low | medium | high | critical
    reversible: bool = True
    package: str = ""                          # decision-packages/<package>/
    description: str = ""
    noul_criteria: str = ""                    # noul: the observable conditions for "yes"
    level_descriptions: dict[str, str] = field(default_factory=dict)   # score: level → standalone meaning
    exits: list[str] = field(default_factory=list)          # choice labels meaning "can't decide"
    state_schema: dict[str, str] = field(default_factory=dict)         # dotted path → type
    policy: dict[str, Any] = field(default_factory=dict)    # confidence routing (cascade.ZonePolicy)
    pins: dict[str, str] = field(default_factory=dict)      # jev_model, state_compiler
    derived_from: str = ""                     # mined pattern / trace cluster id

    @classmethod
    def from_raw(cls, raw: dict) -> "DecisionDef":
        """Accept both the legacy list entry and the decision-package spec format
        (`primitive` for `type`; `criteria` as choices, level descriptions or the noul condition)."""
        raw = dict(raw)
        if "primitive" in raw:
            raw["type"] = raw.pop("primitive")
        crit = raw.pop("criteria", None)
        t = raw.get("type")
        if crit is not None:
            if t == "choice":
                raw["choices"] = dict(crit)
            elif t == "score":
                if isinstance(crit, dict):
                    raw["levels"] = [str(k) for k in crit]
                    raw["level_descriptions"] = {str(k): str(v) for k, v in crit.items()}
                else:
                    raw["levels"] = [str(x) for x in crit]
            else:
                raw["noul_criteria"] = crit if isinstance(crit, str) else "\n".join(f"- {c}" for c in crit)
        raw["version"] = str(raw.get("version", "v1"))
        for k in ("tests", "calibration", "readme", "lint_ack"):
            raw.pop(k, None)                    # file pointers, resolved by the registry
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"{raw.get('name')}/{raw.get('version')}: unknown fields {unknown}")
        return cls(**raw)

    @property
    def ref(self) -> str:
        return f"{self.name}/{self.version}"

    @property
    def labels(self) -> list[str]:
        if self.type == "choice":
            return list(self.choices)
        if self.type == "score":
            return list(self.levels)
        return ["no", "yes"]

    def jev_question(self) -> dict:
        q: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.type == "choice":
            q["criteria"] = dict(self.choices)
        elif self.type == "score":
            q["criteria"] = ([f"{lv}: {self.level_descriptions[lv]}" for lv in self.levels]
                             if self.level_descriptions else list(self.levels))
        elif self.noul_criteria:
            q["instructions"] = f"{self.instructions.rstrip()}\n\nAnswer yes when:\n{self.noul_criteria}"
        return q

    def data_class_enum(self) -> DataClass:
        return DataClass.parse(self.data_class, DataClass.CONFIDENTIAL)


@dataclass
class DecisionResult:
    decision: str
    confidence: float
    probabilities: dict[str, float]
    provider: str                               # jev | rules | local_llm | default
    decision_ref: str
    action: str = ""                            # policy gate outcome (auto/validate/local_llm/escalate)
    score: float | None = None
    cached: bool = False
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    provider_version: str = ""
    escalated_from: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_state(state: Any) -> str:
    """Canonical JSON so equal inputs share a cache key regardless of key order."""
    return json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)


def cache_key(d: DecisionDef, state: Any, provider_version: str) -> str:
    policy_version = str(config.get("decision_fabric.jev.model", ""))
    # labels: a dynamic choice set (spec §39) changes the question under the same ref
    raw = "|".join([d.ref, normalize_state(state), policy_version, provider_version,
                    d.pins.get("state_compiler", ""), ",".join(d.labels)])
    return hashlib.sha256(raw.encode()).hexdigest()


def load_definitions(directory: str | Path | None = None,
                     packages: str | Path | None | bool = None) -> dict[str, DecisionDef]:
    """Load decision definitions. Keyed by 'name/version' (every version) and by bare 'name'.

    A bare name resolves ONLY to the newest version whose stage is `production`: adding
    `x/v2` in shadow never silently moves callers of `x` (spec §32). Promotion and
    rollback go through lif.decision.registry, which re-pins the bare name.

    Sources: config/decisions/*.yaml (lists) and decision-packages/<pkg>/<name>/v*.yaml
    (one spec per file). `packages=False` skips the package tree.
    """
    d = Path(directory) if directory else (
        Path("/etc/lif/decisions") if Path("/etc/lif/decisions").exists() else config.REPO_CONFIG / "decisions")
    raws: list[dict] = []
    for f in sorted(d.glob("*.yaml")):
        raws.extend(yaml.safe_load(f.read_text()) or [])
    if packages is not False:
        for f in package_files(None if packages in (None, True) else packages):
            raw = yaml.safe_load(f.read_text()) or {}
            raw.setdefault("package", f.parent.parent.name)
            raws.append(raw)
    return index_definitions([DecisionDef.from_raw(r) for r in raws])


def index_definitions(defs: list[DecisionDef]) -> dict[str, DecisionDef]:
    """{'name/version': def} for every version, plus bare 'name' → newest production version."""
    out: dict[str, DecisionDef] = {}
    for dd in defs:
        if dd.ref in out:
            raise ValueError(f"duplicate decision definition {dd.ref}")
        out[dd.ref] = dd
    for dd in defs:
        if dd.stage != "production":
            continue
        prev = out.get(dd.name)
        if prev is None or _vkey(dd.version) > _vkey(prev.version):
            out[dd.name] = dd
    return out


def packages_root() -> Path:
    import os
    if os.environ.get("LIF_DECISION_PACKAGES"):
        return Path(os.environ["LIF_DECISION_PACKAGES"])
    mounted = Path("/etc/lif/decision-packages")
    return mounted if mounted.exists() else config.REPO_CONFIG.parent / "decision-packages"


def package_files(root: str | Path | None = None) -> list[Path]:
    r = Path(root) if root else packages_root()
    return sorted(r.glob("*/*/v*.yaml")) if r.exists() else []


def _vkey(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in v.lstrip("v").split("."))
