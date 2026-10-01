"""Provider pricing and per-call cost, read from config/providers.yaml (spec §27, §77).

No price lives in code. A provider whose price is null reports cost as None ("unknown"),
which reports render as such rather than as $0.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from lif.common import config


@lru_cache(maxsize=None)
def _catalogue() -> dict[str, Any]:
    if os.environ.get("LIF_PROVIDERS"):
        p = Path(os.environ["LIF_PROVIDERS"])
    else:
        mounted = Path("/etc/lif/providers.yaml")
        p = mounted if mounted.exists() else config.REPO_CONFIG / "providers.yaml"
    return yaml.safe_load(p.read_text()) or {}


def providers() -> dict[str, Any]:
    return _catalogue().get("providers", {})


def executor_provider(model: str) -> str | None:
    """Model id seen in a trace → provider key (longest matching prefix in `executors:`)."""
    table = _catalogue().get("executors") or {}
    hits = [k for k in table if model.startswith(k)]
    return table[max(hits, key=len)] if hits else None


def provider(name: str) -> dict[str, Any]:
    return providers().get(name) or {}


def reload() -> None:
    _catalogue.cache_clear()


def call_cost(name: str, input_tokens: int = 0, output_tokens: int = 0, cached_input_tokens: int = 0,
              latency_s: float = 0.0) -> float | None:
    """USD for one call. Local providers: energy ESTIMATE (lif.yaml cost.*). Unknown price → None."""
    pr = provider(name).get("pricing") or {}
    if pr.get("energy_estimate"):
        watts = float(config.get("cost.cpu_tier_incremental_watts", 0) or 0)
        kwh = float(config.get("cost.electricity_usd_per_kwh", 0) or 0)
        return latency_s * watts / 3_600_000 * kwh
    if "per_review" in pr:
        return None if pr["per_review"] is None else float(pr["per_review"])
    pin, pout = pr.get("input_per_mtok"), pr.get("output_per_mtok")
    if pin is None or pout is None:
        return None
    hit = pr.get("input_cache_hit_per_mtok", pin)
    fresh = max(0, input_tokens - cached_input_tokens)
    return (fresh * float(pin) + cached_input_tokens * float(hit) + output_tokens * float(pout)) / 1e6


def workflow_cost(counts: dict[str, float], per_call: dict[str, float | None], code_cost: float = 0.0) -> dict:
    """C_total = C_code + Σ N_tier × C_tier (§27). Tiers with unknown price are listed, not zeroed."""
    total, unknown = code_cost, []
    for tier, n in counts.items():
        c = per_call.get(tier)
        if c is None:
            if n:
                unknown.append(tier)
            continue
        total += n * c
    return {"total_usd": round(total, 6), "unknown_price_tiers": unknown}


def expected_latency(l_code_ms: float, l_jev_ms: float, p_escalate: float, l_escalation_ms: float) -> float:
    """L ≈ L_code + L_jev + P_escalate × L_escalation (§28)."""
    return l_code_ms + l_jev_ms + p_escalate * l_escalation_ms
