"""Policy engine — the only component allowed to turn a recommendation into an action.

    JEV / MODEL RECOMMENDATION → policy.decide_action(...) → ACTION

Hard rules live here as plain code and are never delegated to Jev or an LLM:
privacy (what may leave the box), confidence gating, request budgets, and
resource safety.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum

from lif.common import config, metrics


class DataClass(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3

    @classmethod
    def parse(cls, value: str | None, default: "DataClass") -> "DataClass":
        if not value:
            return default
        try:
            return cls[value.strip().upper()]
        except KeyError:
            return default


# Detectors may only RAISE the declared class — a caller cannot label a secret PUBLIC.
_DETECTORS: list[tuple[re.Pattern, DataClass, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), DataClass.RESTRICTED, "private_key"),
    (re.compile(r"\b(?:sk|pk|rk|jv|hf|ghp|gho|xox[abp])_[A-Za-z0-9_\-]{16,}"), DataClass.RESTRICTED, "api_key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), DataClass.RESTRICTED, "aws_key"),
    (re.compile(r"(?i)\b(?:password|passwd|secret|token)\s*[:=]\s*\S{6,}"), DataClass.RESTRICTED, "credential"),
    (re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:@]+:[^\s@]+@"), DataClass.RESTRICTED, "dsn"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), DataClass.RESTRICTED, "ssn"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), DataClass.CONFIDENTIAL, "card_like_number"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), DataClass.CONFIDENTIAL, "email"),
    (re.compile(r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b"), DataClass.CONFIDENTIAL, "phone"),
]


@dataclass
class Classification:
    data_class: DataClass
    declared: DataClass
    findings: list[str] = field(default_factory=list)


def classify(text: str, declared: str | None = None) -> Classification:
    default = DataClass.parse(config.get("privacy.default_class"), DataClass.CONFIDENTIAL)
    decl = DataClass.parse(declared, default)
    level, found = decl, []
    for pat, cls, name in _DETECTORS:
        if pat.search(text):
            found.append(name)
            level = max(level, cls)
    return Classification(level, decl, found)


def may_send(data_class: DataClass, destination: str) -> bool:
    """destination: 'jev' | 'external_llm'. Restricted data never leaves; confidential
    only if explicitly allowed (it is not, by default)."""
    if data_class >= DataClass.RESTRICTED:
        allowed = False
    elif data_class == DataClass.CONFIDENTIAL and not config.get("privacy.confidential_external_allowed", False):
        allowed = False
    else:
        key = {"jev": "privacy.jev_allowed", "external_llm": "privacy.external_llm_allowed"}[destination]
        allowed = data_class.name in (config.get(key) or [])
    if not allowed:
        metrics.privacy_blocks.labels(data_class.name, destination).inc()
    return allowed


# ── confidence gating ────────────────────────────────────────────────────────

class Gate:
    AUTO = "auto"                  # act on the recommendation
    VALIDATE = "validate"          # act only if deterministic validation also passes
    LOCAL_LLM = "local_llm"        # confirm with the local fast model
    ESCALATE = "escalate"          # larger model or human review


def gate(confidence: float, thresholds: dict | None = None) -> str:
    t = {**(config.get("decision_fabric.confidence") or {}), **(thresholds or {})}
    if confidence >= float(t.get("auto", 0.97)):
        return Gate.AUTO
    if confidence >= float(t.get("validate", 0.85)):
        return Gate.VALIDATE
    if confidence >= float(t.get("escalate", 0.70)):
        return Gate.LOCAL_LLM
    return Gate.ESCALATE


# ── request budgets ──────────────────────────────────────────────────────────

@dataclass
class Budget:
    max_latency_ms: int | None = None
    max_external_cost: float = 0.0
    minimum_confidence: float | None = None
    external_allowed: bool = False

    @classmethod
    def from_request(cls, raw: dict | None) -> "Budget":
        raw = raw or {}
        return cls(max_latency_ms=raw.get("max_latency_ms"),
                   max_external_cost=float(raw.get("max_external_cost", 0.0)),
                   minimum_confidence=raw.get("minimum_confidence"),
                   external_allowed=bool(raw.get("external_allowed", False)))
