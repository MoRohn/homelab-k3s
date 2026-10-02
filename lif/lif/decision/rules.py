"""Deterministic rule fallbacks for every core decision.

Rules are what the fabric uses when Jev is disabled, down, or not allowed to see the
data. They are deliberately conservative: when a rule is unsure it returns a modest
confidence so the policy gate escalates rather than acting automatically.
"""
from __future__ import annotations

import re
from typing import Any

from lif.decision.providers import RulesProvider

rules = RulesProvider()

_REPUTABLE = {"qwen", "meta-llama", "mistralai", "google", "microsoft", "nvidia", "ibm-granite",
              "deepseek-ai", "allenai", "huggingfacetb", "baai", "intfloat", "nomic-ai", "unsloth",
              "bartowski", "ggml-org", "openai", "tiiuae", "cohereforai", "internlm", "thudm", "zai-org"}
_TOY = re.compile(r"(?i)(tiny-random|test|dummy|debug|toy|-merge|frankenmerge|abliterated|uncensored)")


def _org(model_id: str) -> str:
    return model_id.split("/", 1)[0].lower() if "/" in model_id else ""


@rules.register("model-suitability")
def model_suitability(s: dict) -> tuple[str, float] | None:
    mid = s.get("model_id", "")
    if _TOY.search(mid):
        return "reject", 0.95
    if s.get("hardware_fit") == "no_fit":
        return "reject", 0.99
    downloads = int(s.get("downloads") or 0)
    reputable = _org(mid) in _REPUTABLE
    if reputable and downloads >= 50_000:
        return "benchmark", 0.80
    if reputable and downloads >= 5_000:
        return "review", 0.75
    if downloads < 1_000:
        return "reject", 0.80
    return "review", 0.65


@rules.register("improvement-probability")
def improvement_probability(s: dict) -> tuple[str, float] | None:
    cand, cur = s.get("candidate") or {}, s.get("current") or {}
    if not cur:
        return "likely", 0.60          # empty category: anything that fits is an improvement
    cp, up = float(cand.get("params_b") or 0), float(cur.get("params_b") or 0)
    newer = str(cand.get("last_modified", "")) > str(cur.get("last_modified", ""))
    if cp > up * 1.5 and newer:
        return "likely", 0.60
    if newer and cp >= up * 0.8:
        return "possible", 0.55
    return "unlikely", 0.55


@rules.register("operational-risk")
def operational_risk(s: dict) -> tuple[str, float] | None:
    if s.get("trust_remote_code"):
        return "high", 0.90
    if not (s.get("safetensors") or s.get("gguf")):
        return "high", 0.85
    if s.get("runtime_compatibility") in (None, "unknown"):
        return "moderate", 0.70
    return "low", 0.70


_CODE = re.compile(r"(?i)(```|\bdef |\bclass |function\s*\(|#include|\bSELECT\b.+\bFROM\b|traceback|stack ?trace|"
                   r"\bcompile|\brefactor|\bbug\b|\bregex\b|\bpython\b|\bjavascript\b|\btypescript\b|\brust\b|\bgolang\b)")
_REASON = re.compile(r"(?i)(prove|derive|step[- ]by[- ]step|\bplan\b|trade-?offs?|analy[sz]e|compare .+ and |"
                     r"why does|calculate|optimi[sz]e|\bmath\b|theorem|puzzle)")


@rules.register("request-route")
def request_route(s: dict) -> tuple[str, float] | None:
    text = s.get("last_user", "") or ""
    tokens = int(s.get("prompt_tokens") or len(text) // 4)
    if _CODE.search(text):
        return "code", 0.75
    if _REASON.search(text):
        return "reasoning", 0.65
    if tokens < 40 and not s.get("structured_output"):
        return "instant", 0.70
    if tokens < 600:
        return "fast", 0.70
    return "default", 0.70


@rules.register("output-acceptable")
def output_acceptable(s: dict) -> tuple[str, float] | None:
    out = (s.get("output") or "").strip()
    if not out:
        return "no", 0.99
    if s.get("finish_reason") == "length":
        return "no", 0.80
    if s.get("schema_valid") is False:
        return "no", 0.99
    return None                         # can't judge content deterministically → abstain


@rules.register("batch-priority")
def batch_priority(s: dict) -> tuple[str, float] | None:
    hours = s.get("deadline_hours")
    if hours is not None and float(hours) <= 2:
        return "urgent", 0.90
    if hours is not None and float(hours) >= 24:
        return "deferrable", 0.85
    return "normal", 0.70


@rules.register("batch-model-size")
def batch_model_size(s: dict) -> tuple[str, float] | None:
    task = (s.get("task") or "").lower()
    if any(k in task for k in ("classif", "tag", "label", "extract", "detect", "dedup", "triage")):
        return "fast", 0.85
    if any(k in task for k in ("reason", "long-form", "essay", "report", "plan")):
        return "large", 0.70
    return "default", 0.65


@rules.register("gpu-admission")
def gpu_admission(s: dict) -> tuple[str, float] | None:
    # Hard safety first: no headroom or primary workload IMMINENT → the fast model, always.
    if s.get("blerbz_state") in ("HIGH", "IMMINENT") or float(s.get("admissible_mib", 0)) < float(
            s.get("required_mib", 1e12)):
        return "use_fast", 0.99
    latency_ms, load_s = float(s.get("latency_target_ms", 2000)), float(s.get("load_time_s", 60))
    if load_s * 1000 > latency_ms and s.get("fast_available", True):
        return "use_fast", 0.90
    if s.get("can_wait"):
        return "queue", 0.75
    return "load_default", 0.70


# ── understanding router (decision-packages/understanding) ──
# State is {"features": analysis.Features.public_state()}: derived numbers only.
_VISUAL = ("causal", "process", "temporal", "comparison", "dependency", "parameter")


def _features(s: dict) -> tuple[dict, str]:
    f = s.get("features") or s
    return f.get("structure") or {}, str(f.get("question_kind") or "other")


@rules.register("understanding-prose-sufficient")
def understanding_prose_sufficient(s: dict) -> tuple[str, float] | None:
    st, qk = _features(s)
    strongest = max((float(st.get(k, 0)) for k in _VISUAL + ("hierarchy", "spatial")), default=0.0)
    if strongest >= 0.5:
        return "no", 0.85
    if qk in ("definition", "other") and strongest < 0.25:
        return "yes", 0.85
    return None


@rules.register("understanding-interaction-worthwhile")
def understanding_interaction_worthwhile(s: dict) -> tuple[str, float] | None:
    f = s.get("features") or s
    st, qk = _features(s)
    if not f.get("has_simulation"):
        return "no", 0.95
    if qk in ("parameter", "teach") or float(st.get("parameter", 0)) >= 0.7:
        return "yes", 0.85
    return None


@rules.register("understanding-diagram-family")
def understanding_diagram_family(s: dict) -> tuple[str, float] | None:
    st, qk = _features(s)
    cand = {"causal": float(st.get("causal", 0)), "process": float(st.get("process", 0)),
            "dependency": max(float(st.get("dependency", 0)), float(st.get("spatial", 0))),
            "timeline": float(st.get("temporal", 0))}
    if max(cand.values()) < 0.25:
        return "none", 0.85
    if qk == "temporal" and cand["timeline"] >= 0.25:
        return "timeline", 0.8
    if qk in ("causal", "debug") and cand["causal"] >= 0.5:
        return "causal", 0.85
    best = max(cand, key=lambda k: (cand[k], k == "causal"))
    ties = sum(1 for v in cand.values() if v == cand[best])
    return best, 0.8 if ties == 1 else 0.6
