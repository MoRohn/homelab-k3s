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
            q["criteria"] = list(self.levels)
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
    raw = "|".join([d.ref, normalize_state(state), policy_version, provider_version])
    return hashlib.sha256(raw.encode()).hexdigest()


def load_definitions(directory: str | Path | None = None) -> dict[str, DecisionDef]:
    """Load decision definitions (config/decisions/*.yaml). Keyed by 'name' (latest) and 'name/version'."""
    d = Path(directory) if directory else (
        Path("/etc/lif/decisions") if Path("/etc/lif/decisions").exists() else config.REPO_CONFIG / "decisions")
    out: dict[str, DecisionDef] = {}
    for f in sorted(d.glob("*.yaml")):
        for raw in yaml.safe_load(f.read_text()) or []:
            dd = DecisionDef(**raw)
            out[dd.ref] = dd
            prev = out.get(dd.name)
            if prev is None or _vkey(dd.version) > _vkey(prev.version):
                out[dd.name] = dd
    return out


def _vkey(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in v.lstrip("v").split("."))
