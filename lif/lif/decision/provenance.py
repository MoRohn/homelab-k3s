"""Decision provenance (spec §54) and the optional bridge into the Knowledge Work layer (§53, §88).

Every cascade decision gets a provenance record: agent, workflow, state hash, question
version, provider, pinned model, full distribution, confidence, threshold, route, fallback
chain, eventual outcome, timestamp. Records hold the state *hash*, never the state.

The knowledge layer receives only durable facts (spec versions, promotions, rollbacks,
audits, experiments, notable incidents), never per-request traces. Those are telemetry.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Protocol

from lif.common import log

LOG = log.get("lif.decision.provenance")


class KnowledgeSink(Protocol):
    def record(self, type: str, obj: dict, edges: list[tuple[str, str]]) -> str | None: ...


class NullSink:
    def record(self, type: str, obj: dict, edges: list[tuple[str, str]]) -> str | None:
        return None


_sink: KnowledgeSink = NullSink()


def set_sink(sink: KnowledgeSink | None) -> None:
    global _sink
    _sink = sink or NullSink()


def default_sink() -> KnowledgeSink:
    """The knowledge layer's adapter when it is installed (lif.knowledge.sink), else a no-op."""
    try:
        from lif.knowledge.sink import KnowledgeSink as Real   # optional; owned by the knowledge layer
        return Real(actor="decision-engineering")
    except Exception:                                         # absent or failing: decisions never depend on it
        return NullSink()


def knowledge(type: str, obj: dict, edges: list[tuple[str, str]] | None = None) -> str | None:
    try:
        return _sink.record(type, obj, edges or [])
    except Exception:
        LOG.exception("knowledge sink failed")
        return None


def state_hash(state: Any) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()[:24]


def new_id() -> str:
    return uuid.uuid4().hex[:20]


def record(*, decision_ref: str, state: Any, answer: str, confidence: float, route: str, executor: str,
           agent: str = "", workflow: str = "", probabilities: dict | None = None, thresholds: dict | None = None,
           pins: dict | None = None, chain: list[dict] | None = None, model: str = "", stage: str = "",
           extra: dict | None = None) -> dict:
    return {"id": new_id(), "ts": time.time(), "type": "agent-decision", "decision_ref": decision_ref,
            "agent": agent, "workflow": workflow, "state_hash": state_hash(state), "answer": answer,
            "confidence": round(float(confidence), 6), "probabilities": probabilities or {},
            "thresholds": thresholds or {}, "route": route, "executor": executor, "model": model,
            "pins": pins or {}, "stage": stage, "chain": chain or [], "outcome": None, **(extra or {})}
