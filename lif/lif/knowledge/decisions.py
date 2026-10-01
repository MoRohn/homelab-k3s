"""Knowledge decisions through the Decision Fabric (Jev → rules), used only after the type system and
deterministic graph rules have had their say (hierarchy: types → graph rules → Jev → local LLM → human).

Definitions live in config/decisions/knowledge.yaml. Rules registered here keep every decision
answerable offline; the fabric's privacy gate decides whether Jev may see the state.
"""
from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

from lif.common import config, log
from lif.decision.rules import rules

LOG = log.get("lif.knowledge.decisions")
DEFS_FILE = "knowledge.yaml"


def _profiles_rule(s: dict) -> tuple[str, float] | None:
    scores = s.get("keyword_scores") or {}
    if not scores:
        return "engineering", 0.55
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    margin = ranked[0] - (ranked[1] if len(ranked) > 1 else 0)
    return best, 0.9 if margin >= 2 else 0.75 if margin >= 1 else 0.6


_CLAIM = re.compile(r"(?i)\b(is|are|was|were|has|have|uses?|runs?|requires?|supports?|measured|reaches|takes)\b")
_DECISION = re.compile(r"(?i)\b(we (decided|chose|will use|use)|decision:|selected|adopt(ed)?|in favou?r of)\b")
_REQ = re.compile(r"(?i)\b(must|shall|required|needs? to|never|always)\b")
_COMMIT = re.compile(r"(?i)\b(will deliver|by (mon|tues|wednes|thurs|fri|satur|sun)day|by \d{4}-\d\d-\d\d|promised|commit(ted)? to)\b")


def _object_type_rule(s: dict) -> tuple[str, float] | None:
    t = (s.get("text") or "").strip()
    if len(t) < 25 or t.startswith(("#", "|", "```")):
        return "none", 0.85
    if t.endswith("?"):
        return "question", 0.85
    if _COMMIT.search(t):
        return "commitment", 0.75
    if _DECISION.search(t):
        return "decision", 0.75
    if _REQ.search(t):
        return "requirement", 0.70
    if _CLAIM.search(t):
        return "claim", 0.60
    return "none", 0.55


def _review_priority_rule(s: dict) -> tuple[str, float] | None:
    dep, t = int(s.get("dependents") or 0), s.get("type", "")
    if t in ("deployment", "commitment") or (t == "decision" and dep >= 5):
        return "critical" if s.get("production") else "high", 0.8
    if t == "decision" or dep >= 3:
        return "high", 0.75
    if dep == 0 and t in ("task", "claim"):
        return "low", 0.7
    return "normal", 0.7


def _duplicate_rule(s: dict) -> tuple[str, float] | None:
    a, b = (s.get("a") or {}), (s.get("b") or {})
    if a.get("type") != b.get("type"):
        return "no", 0.9
    r = SequenceMatcher(None, str(a.get("title", "")).lower(), str(b.get("title", "")).lower()).ratio()
    if r >= 0.92:
        return "yes", 0.85
    if r < 0.6:
        return "no", 0.85
    return None                                  # unsure → abstain → escalate


for _name, _fn in (("knowledge-context-profile", _profiles_rule), ("knowledge-object-type", _object_type_rule),
                   ("knowledge-review-priority", _review_priority_rule), ("knowledge-duplicate", _duplicate_rule)):
    if not rules.has(_name):
        rules.register(_name)(_fn)

_FABRIC = None


def fabric():
    """In-process fabric over the knowledge definitions only (Jev if a key is configured)."""
    global _FABRIC
    if _FABRIC is None:
        from lif.decision.fabric import DecisionFabric
        from lif.decision.providers import JevProvider
        from lif.decision.types import DecisionDef
        path = Path("/etc/lif/decisions") / DEFS_FILE
        if not path.exists():
            path = config.REPO_CONFIG / "decisions" / DEFS_FILE
        defs: dict[str, Any] = {}
        for raw in yaml.safe_load(path.read_text()) or []:
            d = DecisionDef(**{k: v for k, v in raw.items() if k in DecisionDef.__dataclass_fields__})
            defs[d.name] = defs[d.ref] = d
        key = config.secret("TYPE_SAFE_JEV_API_KEY")
        _FABRIC = DecisionFabric(defs, rules, jev=JevProvider(key) if key else None)
    return _FABRIC


def decide(name: str, state: dict, data_class: str | None = None) -> dict:
    """Synchronous wrapper. Never raises: on any failure the local rule answers directly."""
    try:
        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
        if running:                     # called from async code: don't nest loops — rules only
            raise RuntimeError("in event loop")
        r = asyncio.run(fabric().evaluate(name, state, data_class))
        return {"decision": r.decision, "confidence": r.confidence, "provider": r.provider, "action": r.action}
    except Exception as e:              # noqa: BLE001
        fn = {"knowledge-context-profile": _profiles_rule, "knowledge-object-type": _object_type_rule,
              "knowledge-review-priority": _review_priority_rule, "knowledge-duplicate": _duplicate_rule}[name]
        out = fn(state) or (None, 0.0)
        return {"decision": out[0], "confidence": out[1], "provider": "rules-direct", "action": "validate",
                "note": str(e)[:120]}
