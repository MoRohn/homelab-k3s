"""Fake LIF upstreams for console development and e2e tests: controller, gateway, batch and Prometheus
on one port, so the console runs end-to-end without a cluster, a GPU or any inference.

    lif/.venv/bin/uvicorn --app-dir lif/apps/console/e2e fake_upstreams:app --port 8091

Shapes follow the real services field for field: the reference is the console planning notes, which
cite lif/lif/controller/app.py, gateway/app.py, batch/engine.py and decision/de_api.py. The fake
reproduces the real gaps instead of fixing them, so the console's honesty paths get exercised:
  - /v1/models/refresh returns no run id, and a running discovery row has funnel {} until it finishes
  - batch has no `waiting` state: a blocked job stays queued/running and only `reason` says "waiting: …"
  - batch cancel is DELETE /v1/batch/{id}; global pause goes through controller /v1/settings, and
    /v1/batch/control wants X-LIF-Internal (403 without it)
  - /v1/aliases omits the empty-chain aliases (local/vision, local/rerank) that /v1/routing lists
  - a review ticket can be answered twice (the real queue never checks status)
  - X-LIF-* headers only on streaming chat; non-streaming carries body.lif with blerbz + latency_ms
Error envelopes differ per service, as they do for real: controller {error: str}, gateway
{error: {message, type, lif?}}, batch {error: {message}}, Prometheus {status: "error", …}.

One in-memory World backs every view (overview embeds capabilities and batch stats, gateway health
repeats the GPU state), so the views never contradict each other. POST /__scenario {name} rebuilds it
deterministically, with timestamps relative to now:
  healthy      default: everything serving, one batch job running
  fallback     the 4B server crash-loops: local/default and local/fast served by the 1.7B fallback
  blerbz-busy  primary workload production live (IMMINENT): memory guard shed the optional servers,
               batch work waits with reason "waiting: primary workload IMMINENT …"
  offline      every upstream answers 503 (only /__scenario keeps working, so tests can switch back)

Prometheus is a heuristic: the query is matched leniently (selectors, label matchers, aggregations
with by/without, rate/increase/*_over_time, histogram_quantile, binary ops) over a synthetic metric
catalogue, and anything unrecognised returns an empty vector, never an error. Range queries evaluate
a time-varying model, so a 12 h timeline shows idle, inference, primary-workload video and batch hours.

All data is synthetic and generic: no production numbers, hosts or private content. Dev and test only;
nothing here ships in the image (.dockerignore excludes apps/console/e2e).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

ADMIN_KEY = os.environ.get("FAKE_ADMIN_KEY", "dev")        # what the console sends as LIF_CONSOLE_ADMIN_KEY
GATEWAY_KEY = os.environ.get("FAKE_GATEWAY_KEY", "dev")    # … and as LIF_CONSOLE_GATEWAY_KEY
INTERNAL_KEY = os.environ.get("FAKE_INTERNAL_KEY", "dev-internal")
TOKEN_DELAY = float(os.environ.get("FAKE_TOKEN_DELAY", "0.05"))   # ~20 tok/s, like the CPU tier
SCENARIOS = ("healthy", "fallback", "blerbz-busy", "offline")

MIB = 1024 * 1024
TOTAL_MIB = 128 * 1024                      # synthetic unified memory
HEADROOM_MIB = 8192                          # gpusched headroom the memory gates protect
CATEGORY_ALIAS = {"fast": "local/fast", "general": "local/default", "coding": "local/code",
                  "reasoning": "local/reasoning", "embedding": "local/embedding",
                  "reranking": "local/rerank", "vision": "local/vision"}
ALIAS_FLOOR = {"local/default": 4.0, "local/reasoning": 30.0, "local/code": 14.0}
COUNTED_STATES = ("PRODUCTION", "CANARY", "CANDIDATE", "STAGED", "APPROVED", "STANDBY", "REJECTED")
P4B, P17B, PEMB = "qwen3-4b-instruct-2507-q4km-cpu", "qwen3-1.7b-q8-cpu", "qwen3-embedding-0.6b-q8-cpu"
PAPPR, PCODE, PPHI = "qwen2-5-7b-instruct-q4km-cpu", "qwen2-5-coder-7b-instruct-q4km-cpu", "phi-4-mini-instruct-q4km-cpu"
PREJ, PGPU = "llama-3-2-3b-instruct-q4km-cpu", "qwen2-5-coder-14b-instruct-q4km-gpu"
# vision: the installed model (POST /__vision) and the candidate a vision discovery run shortlists
PVIS, PVISC = "qwen2-5-vl-3b-instruct-q4km-cpu", "gemma-3-4b-it-q4km-cpu"
# memory-guard deployments → the profile each one serves (the real guard reports deployment names)
DEPLOYMENT_PROFILE = {"tier0": P4B, "tier0-small": P17B, "embedding": PEMB}
CONN_FAIL = "All connection attempts failed"


def _sha(s: str, n: int = 40) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]


# ── the world ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class World:
    scenario: str
    t0: float                                   # build time; every seeded timestamp is relative to it
    models: dict[str, dict] = field(default_factory=dict)
    alias_versions: dict[str, list[dict]] = field(default_factory=dict)
    benchmarks: list[dict] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    running_run: int | None = None
    activity: list[dict] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    tasks: dict[str, str] = field(default_factory=dict)
    jobs: dict[str, dict] = field(default_factory=dict)
    batch_paused: bool = False
    batch_paused_reason: str = ""
    tickets: list[dict] = field(default_factory=list)
    traces: dict[str, dict] = field(default_factory=dict)
    seq: int = 0
    bg: set[asyncio.Task] = field(default_factory=set)

    @property
    def busy(self) -> bool:
        return self.scenario == "blerbz-busy"

    def log(self, kind: str, subject: str = "", actor: str = "controller", detail: dict | None = None,
            ts: float | None = None) -> None:
        self.seq += 1
        self.activity.append({"seq": self.seq, "ts": round(ts or time.time(), 3), "kind": kind,
                              "subject": subject, "actor": actor, "detail": detail or {}})


W: World


def _profile(mid: str, repo: str, category: str, params_b: float, *, device: str = "cpu", precision: str = "Q4_K_M",
             context: int = 32768, budget: int = 4096, endpoint: str | None = None, concurrency: int = 2,
             license_: str = "apache-2.0", size: int | None = None) -> dict:
    rev = _sha(repo)
    p = {"category": category, "runtime": "llama.cpp", "device": device, "hf_repo": repo, "revision": rev,
         "file": f"{repo.split('/')[-1].replace('-GGUF', '').lower()}-{precision.lower()}.gguf",
         "sha256_prefix": _sha(mid, 12), "precision": precision, "params_b": params_b, "context": context,
         "concurrency": concurrency, "memory_budget_mb": budget, "endpoint": endpoint, "license": license_,
         "sha256": hashlib.sha256(mid.encode()).hexdigest()}
    if category == "embedding":
        p["embedding_dim"] = 1024
    if size:
        p["size"] = size
    if category == "vision":            # a vision GGUF needs its image projector next to the weights
        p["mmproj"] = {"file": f"mmproj-{repo.split('/')[-1].replace('-GGUF', '')}-Q8_0.gguf", "size": 845_000_000,
                       "sha256": hashlib.sha256((mid + ":mmproj").encode()).hexdigest(), "quant": "Q8_0"}
    return p


def _meta(repo: str, category: str, params_b: float, *, downloads: int, likes: int, age_days: float,
          t0: float, family: str, quant: str, size: int, ctx: int = 32768) -> dict:
    return {"model_id": repo, "revision": _sha(repo), "category": category, "architecture": family + "ForCausalLM",
            "family": family.lower(), "params_b": params_b, "pipeline_tag": "text-generation",
            "library": "gguf", "license": "apache-2.0", "gated": False, "trust_remote_code": False,
            "safetensors": None, "gguf": True,
            "gguf_pick": {"file": repo.split("/")[-1].lower() + f"-{quant.lower()}.gguf", "size": size,
                          "sha256": hashlib.sha256(repo.encode()).hexdigest(), "quant": quant},
            "context_length": ctx, "downloads": downloads, "likes": likes,
            "last_modified": t0 - age_days * 86400, "created_at": t0 - (age_days + 30) * 86400,
            "tags": ["gguf", "text-generation", category], "base_model": repo.replace("-GGUF", ""),
            "num_layers": 28, "num_kv_heads": 4, "head_dim": 128, "source": "huggingface",
            "hardware_fit": "fits_cpu", "runtime_compatibility": ["llama.cpp"]}


def _fit(verdict: str, weights: int, kv: int, tps: float, reasons: list[str]) -> dict:
    return {"verdict": verdict, "weights_mib": weights, "kv_mib": kv, "overhead_mib": 512,
            "total_mib": weights + kv + 512, "anon_mib": weights + 512, "est_cpu_decode_tps": tps, "reasons": reasons}


def _judgement(decision: str, conf: float, labels: dict[str, float], action: str = "auto") -> dict:
    return {"decision": decision, "confidence": conf, "probabilities": labels, "provider": "jev",
            "decision_ref": "model-suitability/v2", "action": action, "score": conf, "cached": False,
            "latency_ms": 212.0, "cost_usd": 0.0002, "input_tokens": 640, "provider_version": "jev-synthetic",
            "escalated_from": [], "note": ""}


def _screening(action: str, why: str, p: float, comparison: dict | None = None) -> dict:
    s = {"recommendation": {"action": action, "why": why, "p_advance": p},
         "suitability": _judgement("benchmark", 0.74, {"benchmark": 0.74, "candidate": 0.14, "hold": 0.08, "reject": 0.04}),
         "operational_risk": _judgement("low", 0.81, {"low": 0.81, "medium": 0.15, "high": 0.04}),
         "improvement": _judgement("likely", 0.66, {"likely": 0.66, "unlikely": 0.34}, "validate"),
         "dag_ms": 431, "jev_calls": 2, "priority": 2}
    if comparison:
        s["comparison"] = comparison
    return s


def _core_summary(quality: float, ttft: float, tps: float, *, structured: float = 1.0, errors: int = 0) -> dict:
    cats = ("instruction", "summarization", "classification", "structured", "reasoning", "coding", "hallucination")
    return {"quality": quality, "by_category": {c: round(min(1.0, quality + (i % 3 - 1) * 0.05), 3) for i, c in enumerate(cats)},
            "json_valid": structured, "structured_ok": structured, "ttft_ms_p50": ttft, "ttft_ms_p95": round(ttft * 2.1, 1),
            "latency_ms_p50": round(ttft + 220 / tps * 1000, 1), "latency_ms_p95": round((ttft + 220 / tps * 1000) * 1.7, 1),
            "decode_tps_p50": tps, "concurrency": 2, "throughput_tps": round(tps * 1.6, 2), "errors": errors, "items": 15}


def _results(mid: str, quality: float) -> dict:
    out = {}
    for i in range(15):
        cat = ("instruction", "summarization", "classification", "structured", "reasoning", "coding")[i % 6]
        ok = (i * 7 + len(mid)) % 15 < round(quality * 15)
        out[f"{cat}-{i + 1:02d}"] = {"cat": cat, "pass": ok, "ttft": 0.3 + i * 0.01, "total": 2.0 + i * 0.1,
                                     "decode_tps": 15.0, "output": "(synthetic output)", **({} if ok else {"missing": ["expected term"]})}
    return out


def build(scenario: str) -> World:
    now = time.time()
    w = World(scenario=scenario, t0=now)
    ago = lambda h: now - h * 3600  # noqa: E731

    # registry: three seeded production servers, one approved candidate with a full comparison, two
    # fresh shortlisted candidates, one rejected and one GPU-only candidate that can't be benchmarked
    def row(mid: str, profile: dict, state: str, reason: str, created: float, *, meta=None, fit=None,
            screening=None, pinned=False) -> None:
        w.models[mid] = {"id": mid, "model_id": profile["hf_repo"], "revision": profile["revision"],
                         "category": profile["category"], "state": state,
                         "meta": meta or {"model_id": profile["hf_repo"], "params_b": profile["params_b"],
                                          "license": profile["license"]},
                         "profile": profile, "fit": fit or {}, "screening": screening or {}, "reason": reason,
                         "pinned": pinned, "blocked": False, "created": created, "updated": created}

    row(P4B, _profile(P4B, "Qwen/Qwen3-4B-Instruct-2507-GGUF", "general", 4.0, budget=3584,
                      endpoint="http://tier0.ai-serving.svc:8080"), "PRODUCTION", "seeded", ago(24 * 20), pinned=True)
    row(P17B, _profile(P17B, "Qwen/Qwen3-1.7B-GGUF", "fast", 1.7, precision="Q8_0", budget=2560, concurrency=4,
                       endpoint="http://tier0-small.ai-serving.svc:8080"), "PRODUCTION", "seeded", ago(24 * 20))
    row(PEMB, _profile(PEMB, "Qwen/Qwen3-Embedding-0.6B-GGUF", "embedding", 0.6, precision="Q8_0", budget=1536,
                       context=8192, concurrency=4, endpoint="http://embedding.ai-serving.svc:8080"),
        "PRODUCTION", "seeded", ago(24 * 20))
    comparison = {"thresholds": {"max_quality_regression": 0.01, "max_latency_regression_pct": 10,
                                 "max_memory_increase_pct": 20, "structured_output_min": 0.98, "crash_rate_max": 0},
                  "checks": {"quality": True, "latency": True, "memory": True, "structured": True, "stability": True},
                  "delta": {"quality": 0.134, "ttft_pct": 6.5, "decode_tps_pct": -14.2, "throughput_pct": -9.8,
                            "memory_pct": 17.5, "structured_ok": 1.0},
                  "recommendation": "CANARY", "failed_checks": [], "incumbent": P4B, "alias": "local/default",
                  "jev": {"improves": "yes", "confidence": 0.82, "provider": "jev", "p_improves": 0.82}}
    row(PAPPR, _profile(PAPPR, "Qwen/Qwen2.5-7B-Instruct-GGUF", "general", 7.6, budget=6144, size=4_680_000_000),
        "APPROVED", "comparison: CANARY (all checks passed)", ago(24 * 3),
        meta=_meta("Qwen/Qwen2.5-7B-Instruct-GGUF", "general", 7.6, downloads=184_000, likes=410, age_days=60,
                   t0=now, family="Qwen2", quant="Q4_K_M", size=4_680_000_000),
        fit=_fit("fits_cpu", 4460, 896, 12.8, ["CPU tier: 5.9 GiB fits the optional-server budget"]),
        screening=_screening("shortlist", "Official instruct release, larger than the current default model", 0.78,
                             comparison))
    row(PCODE, _profile(PCODE, "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF", "coding", 7.6, budget=6144),
        "CANDIDATE", "shortlisted by discovery run 3", ago(5),
        meta=_meta("Qwen/Qwen2.5-Coder-7B-Instruct-GGUF", "coding", 7.6, downloads=96_000, likes=230, age_days=90,
                   t0=now, family="Qwen2", quant="Q4_K_M", size=4_680_000_000),
        fit=_fit("fits_cpu", 4460, 896, 12.8, ["CPU tier"]),
        screening=_screening("shortlist", "Code-specialised release, closer to the local/code size floor", 0.71))
    row(PPHI, _profile(PPHI, "bartowski/microsoft_Phi-4-mini-instruct-GGUF", "general", 3.8, budget=3584,
                       license_="mit"),
        "CANDIDATE", "shortlisted by discovery run 3", ago(5),
        meta=_meta("bartowski/microsoft_Phi-4-mini-instruct-GGUF", "general", 3.8, downloads=52_000, likes=120,
                   age_days=40, t0=now, family="Phi3", quant="Q4_K_M", size=2_490_000_000),
        fit=_fit("fits_cpu", 2380, 640, 22.5, ["CPU tier"]),
        screening=_screening("review", "Similar size to the current model; newer release", 0.55))
    row(PREJ, _profile(PREJ, "bartowski/Llama-3.2-3B-Instruct-GGUF", "fast", 3.2, budget=3072, license_="llama3.2"),
        "REJECTED", "comparison: REJECT (quality, structured)", ago(24 * 9),
        meta=_meta("bartowski/Llama-3.2-3B-Instruct-GGUF", "fast", 3.2, downloads=300_000, likes=500, age_days=200,
                   t0=now, family="Llama", quant="Q4_K_M", size=2_020_000_000),
        fit=_fit("fits_cpu", 1930, 512, 26.0, ["CPU tier"]),
        screening=_screening("shortlist", "Popular small instruct model", 0.62,
                             {**comparison, "checks": {**comparison["checks"], "quality": False, "structured": False},
                              "delta": {**comparison["delta"], "quality": -0.133, "structured_ok": 0.93},
                              "recommendation": "REJECT", "failed_checks": ["quality", "structured"],
                              "incumbent": P4B, "alias": "local/fast",
                              "jev": {"improves": "no", "confidence": 0.77, "provider": "jev", "p_improves": 0.23}}))
    row(PGPU, _profile(PGPU, "Qwen/Qwen2.5-Coder-14B-Instruct-GGUF", "coding", 14.8, device="gpu", budget=12288),
        "CANDIDATE", "shortlisted by discovery run 1", ago(24 * 12),
        meta=_meta("Qwen/Qwen2.5-Coder-14B-Instruct-GGUF", "coding", 14.8, downloads=88_000, likes=300,
                   age_days=150, t0=now, family="Qwen2", quant="Q4_K_M", size=8_990_000_000),
        fit=_fit("fits_when_gramz_unloaded", 8580, 1536, 6.1, ["GPU only in a scheduled window"]),
        screening=_screening("shortlist", "Meets the local/code 14B floor", 0.74))

    # alias history: local/default was the 1.7B model until an operator promoted the 4B one
    def version(alias: str, v: int, chain: list[str], ts: float, actor: str, note: str) -> None:
        w.alias_versions.setdefault(alias, []).append(
            {"alias": alias, "version": v, "chain": chain, "canary": None, "ts": ts, "actor": actor, "note": note})

    for alias, chain in (("local/instant", [P17B, P4B]), ("local/fast", [P4B, P17B]),
                         ("local/reasoning", [P4B]), ("local/code", [P4B]), ("local/batch", [P4B]),
                         ("local/embedding", [PEMB])):
        version(alias, 1, chain, ago(24 * 20), "bootstrap", "seed")
    version("local/default", 1, [P17B], ago(24 * 20), "bootstrap", "seed")
    version("local/default", 2, [P4B, P17B], ago(24 * 6), "operator", f"promote {P4B}")

    # benchmarks (synthetic numbers; the 1.7B server has none, like the real registry gap)
    def bench(mid: str, ts: float, summary: dict, suite: str = "core") -> None:
        w.benchmarks.append({"id": len(w.benchmarks) + 1, "model": mid, "ts": ts, "suite": suite,
                             "results": _results(mid, summary.get("quality", 1.0)), "summary": summary})

    bench(P4B, ago(24 * 6), _core_summary(0.733, 412.0, 17.6))
    bench(PEMB, ago(24 * 6), {"sanity_pass": True, "latency_ms_p50": 38.0, "embeddings_per_sec": 41.5,
                              "errors": 0, "dim": 1024}, "embedding")
    bench(PREJ, ago(24 * 9), _core_summary(0.6, 290.0, 24.1, structured=0.93))
    bench(PAPPR, ago(24 * 2.5), _core_summary(0.867, 438.8, 15.1))

    # discovery: an old run, a failed run and the latest finished run that shortlisted two candidates
    w.runs = [
        {"id": 1, "ts": ago(24 * 12), "finished": ago(24 * 12) + 9.1, "status": "succeeded",
         "funnel": {"categories": {"general": _funnel(380, 3, ["Qwen/Qwen2.5-7B-Instruct-GGUF"]),
                                   "coding": _funnel(240, 2, ["Qwen/Qwen2.5-Coder-14B-Instruct-GGUF"])},
                    "total_ms": 9100}, "error": ""},
        {"id": 2, "ts": ago(24 * 5), "finished": ago(24 * 5) + 2.2, "status": "failed", "funnel": {},
         "error": "Hugging Face API rate limited (HTTP 429)"},
        {"id": 3, "ts": ago(5), "finished": ago(5) + 7.4, "status": "succeeded",
         "funnel": {"categories": {"general": _funnel(412, 2, ["bartowski/microsoft_Phi-4-mini-instruct-GGUF"]),
                                   "coding": _funnel(256, 2, ["Qwen/Qwen2.5-Coder-7B-Instruct-GGUF"]),
                                   "fast": _funnel(198, 0, [])}, "total_ms": 7400}, "error": ""},
    ]
    w.settings = {"discovery_disabled": False, "automatic_discovery": True, "automatic_download": False,
                  "automatic_promotion": False, "jev_disabled": False, "maintenance": False, "batch_paused": False,
                  "reserve_gpu_mib": 0}
    w.tasks = {"discovery": "done"}

    # activity, oldest first (served newest first)
    w.log("controller_started", actor="system", ts=ago(30))
    w.log("alias_changed", "local/default", "operator", {"version": 2, "chain": [P4B, P17B], "canary": None,
                                                         "note": f"promote {P4B}", "previous": [P17B]}, ago(24 * 6))
    w.log("download_started", PAPPR, "operator", {"job": "dl-" + _sha(PAPPR, 10), "size": 4_680_000_000}, ago(60))
    w.log("download_complete", PAPPR, "operator", {"job": "dl-" + _sha(PAPPR, 10)}, ago(59.8))
    w.log("state_changed", PAPPR, "operator", {"frm": "DOWNLOADING", "to": "STAGED", "reason": "checksum verified"}, ago(59.8))
    w.log("load_test_passed", PAPPR, "operator", {"load_sec": 41.0}, ago(59.5))
    w.log("benchmark_completed", PAPPR, "operator", {"suite": "core", "quality": 0.867, "json_valid": 1.0,
                                                    "ttft_ms_p50": 438.8, "decode_tps_p50": 15.1, "errors": 0}, ago(59.2))
    w.log("comparison_complete", PAPPR, "operator", {"recommendation": "CANARY", "incumbent": P4B,
                                                    "jev": comparison["jev"]}, ago(59.2))
    w.log("state_changed", PAPPR, "operator", {"frm": "BENCHMARKING", "to": "APPROVED",
                                               "reason": "comparison: CANARY (all checks passed)"}, ago(59.2))
    w.log("setting_changed", "automatic_discovery", "console", {"value": True}, ago(30))
    w.log("discovery_started", "run 3", "scheduler", {"categories": ["general", "coding", "fast"]}, ago(5))
    for mid in (PPHI, PCODE):
        w.log("model_registered", mid, "scheduler", {}, ago(5) + 6)
        w.log("state_changed", mid, "scheduler", {"frm": "DISCOVERED", "to": "CANDIDATE", "reason": "shortlisted"}, ago(5) + 6)
    w.log("discovery_finished", "run 3", "scheduler", {"shortlisted": 2, "total_ms": 7400}, ago(5) + 7.4)
    w.log("blerbz_state", "MODERATE", "gpu-resource-manager", {"frm": "LOW", "reason": "P(production within 1h) = 31%"}, ago(3.2))
    w.log("blerbz_state", "LOW", "gpu-resource-manager", {"frm": "MODERATE", "reason": "P(production within 1h) = 9%"}, ago(2.4))
    w.log("decision_human", "human/6", "console", {"answer": "yes"}, ago(1.5))
    if scenario == "fallback":
        w.log("model_unloaded", P4B, "controller", {"deployment": "tier0", "aliases_affected": ["local/default", "local/fast"],
                                                    "reason": "server stopped answering health checks"}, ago(0.3))
    if w.busy:
        w.log("blerbz_state", "IMMINENT", "gpu-resource-manager", {"frm": "LOW", "reason": "1 production lease(s) live"}, ago(0.4))
        w.log("blerbz_takeover", "IMMINENT", "gpu-resource-manager",
              {"actions": ["gateway: CPU concurrency -> 1, max_tokens <= 512", "batch: low-priority work paused",
                           "benchmarks aborting: []"]}, ago(0.4))
        for dep, th in (("embedding", 6144), ("tier0-small", 9216)):
            w.log("memory_guard_shed", dep, "gpu-resource-manager",
                  {"mem_available_mib": 4400, "threshold_mib": th, "reason": "protect gpusched headroom for the primary workload"},
                  ago(0.35))

    # batch jobs: one running, one paused by its owner, one queued, and finished ones for history
    def job(n: int, desc: str, state: str, counts: dict, *, priority: int = 5, created_h: float, reason: str = "",
            endpoint: str = "/v1/chat/completions", model: str = "local/batch", finished_h: float | None = None,
            deadline_h: float | None = None, source: str = "caller") -> None:
        jid = "bj_" + _sha(f"job{n}", 12)
        full = {k: counts.get(k, 0) for k in ("pending", "running", "succeeded", "failed", "cancelled", "expired")}
        w.jobs[jid] = {"id": jid, "owner": "operator", "data_class": "INTERNAL", "model": model, "endpoint": endpoint,
                       "priority": priority, "priority_source": source,
                       "deadline": now + deadline_h * 3600 if deadline_h else None, "description": desc,
                       "timeout_sec": 600, "max_attempts": 3, "state": state, "reason": reason,
                       "classification": {"batch-priority": {"decision": "normal", "confidence": 0.88,
                                                             "provider": "jev", "action": "auto"},
                                          "batch-model-size": {"decision": "small", "confidence": 0.79,
                                                               "provider": "jev", "action": "validate"}},
                       "model_hint": "local/fast", "created": ago(created_h), "updated": ago(min(created_h, 0.05)),
                       "finished": ago(finished_h) if finished_h is not None else None, "counts": full}

    job(1, "Summarize support tickets (nightly digest)", "running",
        {"pending": 1500, "running": 2, "succeeded": 496, "failed": 2}, created_h=1.2)
    job(2, "Embed knowledge notes", "paused", {"pending": 900, "succeeded": 300}, priority=6, created_h=3,
        reason="paused by owner", endpoint="/v1/embeddings", model="local/embedding")
    job(3, "Classify application log lines", "queued", {"pending": 500}, priority=6, created_h=0.5, deadline_h=10,
        source="decision:jev")
    job(4, "Translate release notes", "completed", {"succeeded": 48}, priority=4, created_h=7, finished_h=6.5)
    job(5, "Extract invoice fields (sample)", "failed", {"failed": 20}, created_h=26, finished_h=25.8)
    job(6, "Re-rank search results experiment", "cancelled", {"succeeded": 12, "cancelled": 88}, priority=8,
        created_h=30, finished_h=29.5, reason="cancelled by owner")

    # decision review queue: one pending ticket the console can answer, one already answered
    w.tickets = [_ticket(7, ago(0.8), "pending", "shadow_disagreement", PCODE, "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
                         "coding", jev_yes=0.61),
                 _ticket(6, ago(4.5), "answered", "shadow_sample", PPHI, "bartowski/microsoft_Phi-4-mini-instruct-GGUF",
                         "general", jev_yes=0.83, answer="yes", reviewer="console", resolved=ago(1.5))]
    w.traces = _traces(now)
    _apply_batch_gate(w)
    return w


def _funnel(listed: int, shortlisted_n: int, shortlisted: list[str]) -> dict:
    after_filter = listed // 4
    det = max(shortlisted_n + 2, after_filter // 6)
    return {"listed": listed, "after_listing_filter": after_filter, "detail_fetched": after_filter // 2,
            "after_deterministic": det,
            "rejected": {"too_large": after_filter // 3, "no_gguf": after_filter // 4, "license": 6,
                         "merge_or_finetune": 9, "stale": 5},
            "screen_ms": 1900 + listed, "jev_calls": det * 2, "screen_errors": 0, "after_screening": len(shortlisted),
            "providers": {"jev": det}, "shortlisted": shortlisted}


def _ticket(tid: int, ts: float, status: str, reason: str, mid: str, repo: str, category: str, *, jev_yes: float,
            answer: str | None = None, reviewer: str | None = None, resolved: float | None = None) -> dict:
    return {"id": tid, "ts": ts, "decision_ref": "model-advance/v1", "provenance_id": "prov_" + _sha(f"t{tid}", 16),
            "status": status, "answer": answer, "reviewer": reviewer, "resolved_ts": resolved,
            "package": {"decision": "model-advance/v1", "primitive": "noul",
                        "instructions": "Judge whether the model in `model.id` is worth downloading and benchmarking "
                                        "for the category in `category.name`, given its metadata and the production "
                                        "model in `current.model_id`.",
                        "labels": ["yes", "no"],
                        "criteria": {"yes": "the model is built for the category, is an official release or a direct "
                                            "quantization, and is newer or larger than the current model",
                                     "no": "otherwise"},
                        "state": {"model": {"id": repo, "pipeline_tag": "text-generation",
                                            "tags": ["gguf", "text-generation", category]},
                                  "category": {"name": category},
                                  "current": {"model_id": "Qwen/Qwen3-4B-Instruct-2507-GGUF"},
                                  "comparison": {"newer_than_current": False, "larger_than_current": True}},
                        "reason": reason, "data_class": "PUBLIC",
                        "jev": {"answer": "yes", "confidence": jev_yes,
                                "probabilities": {"yes": jev_yes, "no": round(1 - jev_yes, 2)}, "provider": "jev"},
                        "prior_tiers": [{"tier": "baseline", "executor": "model-discovery",
                                         "answer": "no" if reason == "shadow_disagreement" else "yes"}]},
            "_mid": mid}


def _traces(now: float) -> dict[str, dict]:
    disc = "obs-" + _sha("observe", 12)
    audit = "run-" + _sha("audit", 12)
    ops = []
    plan = [("tool", "read_file", "CODE", "code"), ("decision", "pick-next-file", "JEV_CANDIDATE", "jev"),
            ("generation", "draft-patch", "GENERATIVE", "local-fast"), ("tool", "run_tests", "CODE", "code"),
            ("decision", "result-keep", "JEV_CANDIDATE", "jev"), ("policy", "approve-write", "HUMAN_OR_POLICY", "human")]
    for i in range(12):
        kind, name, cls, ex = plan[i % len(plan)]
        ops.append({"seq": i + 1, "ts": now - 7200 + i * 40, "kind": kind, "name": name, "classification": cls,
                    "class_confidence": 0.9, "executor": ex, "input_tokens": 0 if ex == "code" else 800 + i * 10,
                    "output_tokens": 0 if ex == "code" else 120, "latency_ms": 15 if ex == "code" else 640,
                    "output_label": "ok" if cls == "CODE" else ("keep" if cls == "JEV_CANDIDATE" else ""),
                    "reasons": [f"{kind} step classified as {cls.lower()}"]})
    return {
        disc: {"summary": {"run_id": disc, "agent": "model-discovery", "workflow": "discovery", "t0": now - 5 * 3600,
                           "ops": 1, "jev": 1, "gen": 0, "code": 0, "human": 0},
               "operations": [{"seq": 1, "ts": now - 5 * 3600, "kind": "decision", "name": "model-advance",
                               "classification": "JEV_CANDIDATE", "class_confidence": 0.95, "executor": "model-discovery",
                               "input_tokens": 0, "output_tokens": 0, "latency_ms": 2, "output_label": "no",
                               "reasons": ["observed baseline decision"]}]},
        audit: {"summary": {"run_id": audit, "agent": "coding-agent", "workflow": "session", "t0": now - 7200,
                            "ops": len(ops), "jev": 4, "gen": 2, "code": 4, "human": 2},
                "operations": ops},
    }


# ── derived views ───────────────────────────────────────────────────────────────────────────────

def profile_health(w: World) -> dict[str, tuple[bool, str]]:
    """Endpoint health per routing profile. fallback: the 4B server crash-loops; busy: the memory guard
    shed the optional servers, which the gateway only sees as connection failures (as in reality)."""
    down = {P4B} if w.scenario == "fallback" else ({P17B, PEMB} if w.busy else set())
    return {pid: (pid not in down, CONN_FAIL if pid in down else "") for pid in routing_profiles(w)}


def routing_profiles(w: World) -> dict[str, dict]:
    return {m["id"]: m["profile"] for m in w.models.values()
            if m["state"] in ("PRODUCTION", "CANARY", "STANDBY", "APPROVED") and m["profile"].get("endpoint")}


def current_alias(w: World, alias: str) -> dict | None:
    v = w.alias_versions.get(alias)
    return v[-1] if v else None


def alias_chains(w: World) -> dict[str, list[str]]:
    out = {a: list(v[-1]["chain"]) for a, v in w.alias_versions.items()}
    out.setdefault("local/vision", [])
    out.setdefault("local/rerank", [])
    return out


def resolve(w: World, alias: str, *, sample_canary: bool = False) -> dict:
    """Mirror router.alias_status(): first healthy profile in the chain; fallback when it isn't the
    primary; degraded when it is smaller than the alias' quality floor. Reasons use the router's text."""
    chain = alias_chains(w).get(alias)
    if chain is None:
        return {"available": False, "reason": f"unknown model alias '{alias}'. Use GET /v1/models"}
    if not chain:
        return {"available": False, "reason": f"no local model is deployed for {alias} (capacity; see /v1/capabilities)"}
    health, profiles = profile_health(w), routing_profiles(w)
    cur = current_alias(w, alias)
    canary = (cur or {}).get("canary")
    if sample_canary and canary and health.get(canary["profile"], (False, ""))[0] \
            and random.random() * 100 < canary["percent"]:
        prof = canary["profile"]
        return {"available": True, "served_by": prof, "fallback": False, "degraded": False,
                "reason": f"canary ({canary['percent']}% of {alias})", "canary": True}
    errors = []
    for i, prof in enumerate(chain):
        ok, err = health.get(prof, (False, "not deployed"))
        if not ok:
            errors.append(f"{prof}: {err}")
            continue
        params = profiles[prof]["params_b"]
        floor = ALIAS_FLOOR.get(alias, 0.0)
        degraded = params < floor
        if i > 0:
            reason = f"primary unavailable: {health[chain[0]][1] or 'not deployed'}"
        elif degraded:
            reason = f"{alias} expects >= {floor:g}B; largest resident model is {params:g}B"
        else:
            reason = ""
        return {"available": True, "served_by": prof, "fallback": i > 0, "degraded": degraded, "reason": reason}
    return {"available": False, "reason": f"all models for {alias} are unavailable: " + "; ".join(errors)}


def capabilities(w: World) -> dict:
    aliases = {a: {k: v for k, v in resolve(w, a).items() if k != "canary"} for a in alias_chains(w)}
    aliases["local/auto"] = {"available": True, "served_by": "decision: request-route", "fallback": False,
                             "degraded": False, "reason": ""}
    health = profile_health(w)
    profiles = {pid: {"endpoint_healthy": health[pid][0], "params_b": p["params_b"], "device": p["device"],
                      "category": p["category"], "context": p["context"], "model": f"{p['hf_repo']}@{p['revision']}",
                      "last_error": health[pid][1]} for pid, p in routing_profiles(w).items()}
    return {"aliases": aliases, "profiles": profiles, "blerbz": gpu_snapshot(w),
            "yield": {"cpu_concurrency": 1 if w.busy else 4}, "decision_fabric": fabric_status(w, short=True)}


def gpu_snapshot(w: World) -> dict:
    now = time.time()
    m = mem_model(w, now)
    if w.busy:
        state, reason, p = "IMMINENT", "1 production lease(s) live", 1.0
    elif w.scenario == "fallback":
        state, reason, p = "LOW", "P(production within 1h) = 14%", 0.14
    else:
        state, reason, p = "LOW", "P(production within 1h) = 8%", 0.08
    return {"ts": now - 2.0, "reachable": True, "production_live": w.busy, "production_leases": 1 if w.busy else 0,
            "background_leases": 1 if m["lease_bg"] else 0, "p_next_hour": p, "forecast_authoritative": True,
            "admissible_mib": float(max(0, round(m["avail"] - HEADROOM_MIB))), "mem_available_mib": float(round(m["avail"])),
            "gpu_util_percent": round(m["util"], 1), "holds": 0,
            "residents_loaded": {"llm": True, "image": True, "embedding": True},
            "state": state, "reason": reason, "age_sec": 2.0}


def fabric_status(w: World, *, short: bool = False) -> dict:
    jev = not w.settings.get("jev_disabled")
    out = {"provider": "jev", "jev_enabled": jev, "jev_breaker_open": False, "jev_model": "jev-synthetic",
           "definitions": ["request-route/v1", "batch-priority/v1", "batch-model-size/v1", "model-advance/v1",
                           "model-suitability/v2", "candidate-vs-incumbent/v1"],
           "cache_entries": 37, "recent": []}
    if short:
        return out
    now = time.time()
    recent = [{"ts": now - 60 * i - 30, "state_digest": _sha(f"s{i}", 16),
               "decision": ("fast", "default", "code", "deferrable")[i % 4], "confidence": round(0.9 - i * 0.04, 3),
               "probabilities": {}, "provider": "jev" if i % 2 == 0 else "rules",
               "decision_ref": "request-route/v1" if i % 4 != 3 else "batch-priority/v1",
               "action": "auto" if i % 2 == 0 else "validate", "score": round(0.9 - i * 0.04, 3), "cached": i == 2,
               "latency_ms": 180.0 + i * 12, "cost_usd": 0.0002 if i % 2 == 0 else 0.0, "input_tokens": 420,
               "provider_version": "jev-synthetic", "escalated_from": [], "note": "",
               "threshold": {"auto": 0.85, "validate": 0.7, "escalate": 0.5}} for i in range(4)]
    return {**out, "recent": recent, "status": "healthy", "window": "24h", "decisions_total": 142,
            "by_provider": {"jev": 61, "rules": 74, "default": 7}, "by_action": {"auto": 58, "validate": 77, "escalate": 7},
            "cache_hit_rate": 0.21, "latency_ms_p50": 164.0, "high_confidence_rate": 0.41, "escalation_rate": 0.05,
            "jev_cost_usd": 0.0122, "uptime_sec": int(now - w.t0) + 86400}


def batch_gate(w: World, priority: int) -> str | None:
    """The `waiting: …` reason the batch engine writes for a job it can't dispatch, else None."""
    if w.batch_paused:
        return "waiting: batch paused by operator" + (f" ({w.batch_paused_reason})" if w.batch_paused_reason else "")
    if w.busy:
        if priority >= 5:
            return "waiting: primary workload IMMINENT (1 production lease(s) live): batch/eval paused"
        return "waiting: primary workload IMMINENT: async work yields"
    return None


def _apply_batch_gate(w: World) -> None:
    for j in w.jobs.values():
        if j["state"] in ("queued", "running"):
            j["reason"] = batch_gate(w, j["priority"]) or ("" if not j["reason"].startswith("waiting:") else j["reason"])


def job_view(j: dict) -> dict:
    c = j["counts"]
    total = sum(c.values())
    terminal = total - c["pending"] - c["running"]
    return {**j, "total": total, "progress": round(terminal / total, 4) if total else 1.0, "status": j["state"],
            "completed": c["succeeded"], "failed": c["failed"]}


def batch_stats(w: World) -> dict:
    active = [j for j in w.jobs.values() if j["state"] in ("queued", "running", "paused")]
    depth: dict[str, int] = {}
    for j in active:
        if j["counts"]["pending"]:
            depth[f"P{j['priority']}"] = depth.get(f"P{j['priority']}", 0) + j["counts"]["pending"]
    items: dict[str, int] = {}
    jobs: dict[str, int] = {}
    for j in w.jobs.values():
        jobs[j["state"]] = jobs.get(j["state"], 0) + 1
        for k, n in j["counts"].items():
            if n:
                items[k] = items.get(k, 0) + n
    snap = gpu_snapshot(w)
    pending = sum(j["counts"]["pending"] for j in active)
    recent = [job_view(j) for j in sorted(w.jobs.values(), key=lambda j: -j["created"])[:10]]
    return {"queue_depth": depth, "pending": pending, "running": sum(j["counts"]["running"] for j in active),
            "paused": w.batch_paused, "paused_reason": w.batch_paused_reason,
            "blerbz": {"state": snap["state"], "reason": snap["reason"]}, "items": items, "jobs": jobs,
            "queued": pending, "queue_by_priority": depth, "pause_reason": w.batch_paused_reason, "totals": items,
            "recent": recent}


def model_row(w: World, m: dict) -> dict:
    last = next((b for b in reversed(w.benchmarks) if b["model"] == m["id"]), None)
    aliases = sorted(a for a, chain in alias_chains(w).items() if m["id"] in chain)
    return {**m, "last_benchmark": {"ts": last["ts"], "summary": last["summary"]} if last else None, "aliases": aliases}


def availability(w: World, window: str) -> dict:
    base = {"gateway": (0.999, 4.0), "fast_model": (0.996, 610.0), "default_model": (0.993, 790.0),
            "embedding": (0.99, 45.0), "decision_fabric": (0.998, 170.0), "useful_local_ai": (0.997, 610.0)}
    if w.scenario == "fallback":
        base["default_model"] = (0.71 if window == "24h" else 0.93, 930.0)
    if w.busy:
        base["embedding"] = (0.42 if window == "24h" else 0.84, 51.0)
    samples = 2880 if window == "24h" else 20160
    return {k: {"samples": samples, "availability": a, "mean_latency_ms": lat} for k, (a, lat) in base.items()}


def memory_guard(w: World) -> dict:
    return {"shed": ["tier0-small", "embedding"] if w.busy else [], "pending": {}}


# ── time-varying resource model (Prometheus and /v1/gpu share it) ─────────────────────────────

_PATTERN = ("idle", "inference", "inference", "video", "video", "inference+batch", "idle", "inference")


def phase(w: World, t: float) -> str:
    """What the box was doing in the hour containing t. The current hour follows the scenario."""
    if int(t // 3600) == int(time.time() // 3600):
        return "video" if w.busy else "inference+batch"
    return _PATTERN[int(t // 3600) % len(_PATTERN)]


def mem_model(w: World, t: float) -> dict:
    ph = phase(w, t)
    now_hour = int(t // 3600) == int(time.time() // 3600)
    wobble = math.sin(t / 600.0)
    residents = {"llm": 36864.0, "image": 20480.0, "embedding": 1229.0}
    lease_prod = (54000.0 if (now_hour and w.busy) else 30000.0) if ph == "video" else 0.0
    lease_bg = 2048.0 if ph.endswith("batch") else 0.0
    shed = set(memory_guard(w)["shed"]) if now_hour else set()
    crash = w.scenario == "fallback" and now_hour
    serving = {"tier0": 220.0 if crash else 3100.0, "tier0-small": 0.0 if "tier0-small" in shed else 2100.0,
               "embedding": 0.0 if "embedding" in shed else 900.0}
    system = {"gateway": 360.0, "controller": 220.0, "batch": 150.0, "decision-fabric": 260.0, "console": 110.0}
    other = 9800.0 + 400.0 * wobble
    used = sum(residents.values()) + lease_prod + lease_bg + sum(serving.values()) + sum(system.values()) + other
    util = {"idle": 3.0, "inference": 22.0, "video": 88.0, "inference+batch": 35.0}[ph] + 3.0 * wobble
    return {"phase": ph, "residents": residents, "lease_prod": lease_prod, "lease_bg": lease_bg, "serving": serving,
            "system": system, "other": other, "avail": TOTAL_MIB - used, "util": max(0.0, util),
            "swap": 1800.0 if (now_hour and w.busy) else 64.0, "psi": 4.2 if (now_hour and w.busy) else 0.1}


# ── Prometheus: a synthetic metric catalogue + a lenient query matcher ─────────────────────────

Series = tuple[dict[str, str], float, float | None]     # labels (incl. __name__), value, per-second rate
LAT_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120, 300)
TPS_BUCKETS = (1, 2, 4, 8, 12, 16, 24, 32, 48, 64, 96, 128)
COUNTER_EPOCH_SEC = 7 * 86400                     # counters "started" a week before t


def _lognorm_cdf(x: float, median: float, sigma: float = 0.6) -> float:
    if x <= 0:
        return 0.0
    return 0.5 * (1 + math.erf(math.log(x / median) / (sigma * math.sqrt(2))))


def _store(w: World, t: float) -> dict[str, list[Series]]:
    m = mem_model(w, t)
    ph = m["phase"]
    now_hour = int(t // 3600) == int(time.time() // 3600)
    out: dict[str, list[Series]] = {}

    def g(name: str, value: float, **labels: str) -> None:
        out.setdefault(name, []).append(({"__name__": name, **labels}, float(value), None))

    def c(name: str, rate: float, **labels: str) -> None:
        out.setdefault(name, []).append(({"__name__": name, **labels}, rate * COUNTER_EPOCH_SEC, rate))

    def h(name: str, median: float, rate: float, buckets: tuple, **labels: str) -> None:
        for le in (*buckets, math.inf):
            frac = 1.0 if le == math.inf else _lognorm_cdf(le, median)
            c(name + "_bucket", rate * frac, le="+Inf" if le == math.inf else f"{le:g}", **labels)
        c(name + "_sum", rate * median, **labels)
        c(name + "_count", rate, **labels)

    node = {"instance": "node-exporter:9100", "job": "node-exporter"}
    g("node_memory_MemTotal_bytes", TOTAL_MIB * MIB, **node)
    g("node_memory_MemAvailable_bytes", m["avail"] * MIB, **node)
    g("node_memory_SwapTotal_bytes", 16 * 1024 * MIB, **node)
    g("node_memory_SwapFree_bytes", (16 * 1024 - m["swap"]) * MIB, **node)
    g("node_filesystem_size_bytes", 3.7e12, mountpoint="/", fstype="ext4", device="/dev/nvme0n1p2", **node)
    g("node_filesystem_avail_bytes", 2.2e12, mountpoint="/", fstype="ext4", device="/dev/nvme0n1p2", **node)
    temp = 44.0 + m["util"] * 0.2
    g("node_hwmon_temp_celsius", 39.0, chip="nvme_nvme0", sensor="temp1", **node)
    g("node_hwmon_temp_celsius", temp, chip="thermal_thermal_zone0", sensor="temp1", **node)
    for z in range(3):
        g("node_thermal_zone_temp", temp - z, type="acpitz", zone=str(z), **node)

    gs = {"job": "gpusched"}
    g("gpusched_up", 1, **gs)
    g("gpusched_gpu_util_percent", m["util"], **gs)
    g("gpusched_mem_available_mib", m["avail"], **gs)
    g("gpusched_capacity_admissible_mib", max(0.0, m["avail"] - HEADROOM_MIB), **gs)
    g("gpusched_capacity_residents_mib", sum(m["residents"].values()), **gs)
    g("gpusched_capacity_measured_free_mib", m["avail"] - HEADROOM_MIB, **gs)
    g("gpusched_capacity_unmanaged_mib", m["other"], **gs)
    g("gpusched_swap_used_mib", m["swap"], **gs)
    g("gpusched_psi_memory_some_avg10", m["psi"], **gs)
    g("gpusched_holds", 0, **gs)
    g("gpusched_queue_depth", 0, **gs)
    g("gpusched_forecast_authoritative", 1, **gs)
    g("gpusched_forecast_p_arrival_next_hour", 1.0 if ph == "video" else 0.08, **gs)
    g("gpusched_telemetry_age_seconds", 2, **gs)
    for klass, mib in (("production", m["lease_prod"]), ("background", m["lease_bg"])):
        g("gpusched_leases", 1 if mib else 0, klass=klass, **gs)
        g("gpusched_capacity_leases_mib", mib, klass=klass, **gs)
    for r, mib in m["residents"].items():
        g("gpusched_resident_loaded", 1, resident=r, **gs)
        g("gpusched_resident_measured_mib", mib, resident=r, **gs)
        g("gpusched_resident_counted_mib", mib, resident=r, **gs)

    state = 3 if ph == "video" else (1 if int(t // 3600) % 8 == 6 else 0)
    for job in ("controller", "gateway", "batch", "decision-fabric"):
        g("lif_blerbz_state", state, job=job)
        g("lif_gpu_admissible_mib", max(0.0, m["avail"] - HEADROOM_MIB), job=job)
    avail = availability(w, "24h")
    down = {"default_model"} if (w.scenario == "fallback" and now_hour) else (
        {"embedding"} if (w.busy and now_hour) else set())
    for cap, a in avail.items():
        g("lif_available", 0 if cap in down else 1, capability=cap, job="controller")
        g("lif_probe_seconds", a["mean_latency_ms"] / 1000, capability=cap, job="controller")
    g("lif_jev_circuit_open", 0, job="decision-fabric")

    load = {"idle": 0.05, "inference": 1.0, "video": 0.3, "inference+batch": 1.0}[ph]
    batching = ph.endswith("batch") and not w.batch_paused
    served = {"local/fast": P17B if (w.scenario == "fallback" and now_hour) else P4B,
              "local/default": P17B if (w.scenario == "fallback" and now_hour) else P4B,
              "local/instant": P4B if (w.busy and now_hour) else P17B, "local/auto": P4B}
    for alias, base in (("local/fast", 0.05), ("local/default", 0.03), ("local/instant", 0.01), ("local/auto", 0.02)):
        c("lif_requests_total", base * load, endpoint="/v1/chat/completions", alias=alias, status="200", job="gateway")
        prof = served[alias]
        h("lif_ttft_seconds", 0.16 if prof == P17B else 0.41, base * load, LAT_BUCKETS, alias=alias, profile=prof, job="gateway")
        h("lif_request_seconds", 2.4 if prof == P17B else 6.8, base * load, LAT_BUCKETS, endpoint="/v1/chat/completions",
          alias=alias, job="gateway")
        if prof != alias_chains(w).get(alias, [prof])[0] and alias != "local/auto":
            c("lif_fallbacks_total", base * load, alias=alias, served_by=prof, reason="primary unavailable", job="gateway")
    if w.scenario == "fallback" and now_hour:
        c("lif_requests_total", 0.002, endpoint="/v1/chat/completions", alias="local/default", status="503", job="gateway")
    c("lif_requests_total", 0.02 * load, endpoint="/v1/embeddings", alias="local/embedding", status="200", job="gateway")
    for prof, tps in ((P4B, 17.0), (P17B, 31.0)):
        h("lif_decode_tokens_per_second", tps, 0.04 * load, TPS_BUCKETS, profile=prof, job="gateway")
        c("lif_tokens_total", 30 * load, profile=prof, direction="input", job="gateway")
        c("lif_tokens_total", 9 * load, profile=prof, direction="output", job="gateway")
        g("lif_inflight_requests", 1 if load >= 1 else 0, profile=prof, job="gateway")
    for tier, r in (("fast_local", 0.06), ("large_local", 0.0), ("deterministic", 0.01), ("jev", 0.008)):
        c("lif_tasks_total", r * load, workload="console", tier=tier, job="gateway")
        c("lif_external_cost_avoided_usd_total", r * load * 0.0004, tier=tier, job="gateway")
    if w.busy and now_hour:
        c("lif_throttled_total", 0.01, reason="blerbz_imminent", job="gateway")
    c("lif_batch_items_total", 0.8 if batching else 0.0, outcome="succeeded", job="batch")
    c("lif_batch_items_total", 0.01 if batching else 0.0, outcome="failed", job="batch")
    for p in range(4, 9):
        g("lif_batch_queue_depth", sum(j["counts"]["pending"] for j in w.jobs.values()
                                       if j["priority"] == p and j["state"] in ("queued", "running", "paused")),
          priority=f"P{p}", job="batch")
    for dec, prov, act, r in (("request-route/v1", "jev", "auto", 0.004), ("request-route/v1", "rules", "validate", 0.006),
                              ("batch-priority/v1", "jev", "auto", 0.0005)):
        c("lif_decisions_total", r * load, decision=dec, provider=prov, action=act, job="decision-fabric")
        h("lif_decision_seconds", 0.17, r * load, LAT_BUCKETS, decision=dec, provider=prov, job="decision-fabric")
    c("lif_jev_cost_usd_total", 0.0000012, job="decision-fabric")
    for mid in (PAPPR, PCODE, PPHI):
        if mid in w.models:
            g("lif_model_state", 1, model=mid, state=w.models[mid]["state"], job="controller")

    # kube-state-metrics: the same deployments the real cluster runs, in generic form
    crash = w.scenario == "fallback" and now_hour
    shed = set(memory_guard(w)["shed"]) if now_hour else set()
    deps = [("ai-serving", "tier0", 1, 0 if crash else 1), ("ai-serving", "tier0-small", 0 if "tier0-small" in shed else 1,
                                                            0 if "tier0-small" in shed else 1),
            ("ai-serving", "embedding", 0 if "embedding" in shed else 1, 0 if "embedding" in shed else 1),
            ("ai-system", "gateway", 2, 2), ("ai-system", "controller", 1, 1), ("ai-system", "batch", 1, 1),
            ("ai-system", "decision-fabric", 1, 1), ("ai-system", "console", 1, 1)]
    for ns, dep, spec, ready in deps:
        g("kube_deployment_spec_replicas", spec, namespace=ns, deployment=dep, job="kube-state-metrics")
        g("kube_deployment_status_replicas_available", ready, namespace=ns, deployment=dep, job="kube-state-metrics")
        for i in range(spec):
            pod = f"{dep}-{_sha(dep, 9)}-{_sha(dep + str(i), 5)}"
            failing = crash and dep == "tier0"
            for phase_name in ("Pending", "Running", "Succeeded", "Failed", "Unknown"):
                g("kube_pod_status_phase", 1 if phase_name == "Running" else 0, namespace=ns, pod=pod, phase=phase_name,
                  job="kube-state-metrics")
            container = "llama" if ns == "ai-serving" else dep
            g("kube_pod_container_status_restarts_total", 7 if failing else 0, namespace=ns, pod=pod,
              container=container, job="kube-state-metrics")
            g("kube_pod_container_status_ready", 0 if failing else 1, namespace=ns, pod=pod, container=container,
              job="kube-state-metrics")
            if failing:
                g("kube_pod_container_status_waiting_reason", 1, namespace=ns, pod=pod, container=container,
                  reason="CrashLoopBackOff", job="kube-state-metrics")
            mib = (m["serving"] if ns == "ai-serving" else m["system"]).get(dep, 100.0) / max(spec, 1)
            g("container_memory_working_set_bytes", mib * MIB, namespace=ns, pod=pod, container=container,
              job="kubelet")
    for job in ("gateway", "controller", "batch", "decision-fabric", "gpusched", "node-exporter", "kube-state-metrics"):
        g("up", 1, job=job)

    alerts = [("Watchdog", "none")]
    if crash:
        alerts += [("LIFDefaultDegraded", "warning"), ("KubePodCrashLooping", "warning")]
    if w.busy and now_hour:
        alerts += [("LIFHostHeadroomLow", "warning")]
    for name, sev in alerts:
        g("ALERTS", 1, alertname=name, alertstate="firing", severity=sev)
    return out


_STORE_CACHE: dict[tuple[int, int], dict[str, list[Series]]] = {}


def store(t: float) -> dict[str, list[Series]]:
    key = (id(W), int(t))
    if key not in _STORE_CACHE:
        if len(_STORE_CACHE) > 32:             # each entry is ~600 series; keep memory flat
            _STORE_CACHE.clear()
        _STORE_CACHE[key] = _store(W, t)
    return _STORE_CACHE[key]


_DUR = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
AGGS = {"sum", "max", "min", "avg", "count", "topk", "bottomk", "group", "stddev", "stdvar", "quantile"}
Vec = list[tuple[dict[str, str], float]]
Val = Vec | float | str


def _dur(s: str) -> float:
    return sum(float(n) * _DUR[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)", s))


def _close(q: str, i: int) -> int:
    """Index of the bracket closing q[i], honouring quotes."""
    depth, quote = 0, None
    while i < len(q):
        ch = q[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced brackets")


def _top_split(q: str, ops: tuple[str, ...]) -> tuple[str, str, str] | None:
    """Split at the LAST top-level occurrence of any op (left-associative)."""
    depth, quote, found, i = 0, None, None, 0
    while i < len(q):
        ch = q[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            for op in ops:
                if not q.startswith(op, i):
                    continue
                j = i + len(op)
                if op.isalpha():
                    if (i and (q[i - 1].isalnum() or q[i - 1] == "_")) or (j < len(q) and (q[j].isalnum() or q[j] == "_")):
                        continue
                elif op in "<>" and j < len(q) and q[j] == "=":
                    continue
                elif op in "+-":
                    prev = q[:i].rstrip()
                    if not prev or prev[-1] in "+-*/%^(<>=,!" or re.search(r"\d[eE]$", prev):
                        continue
                found = (i, op)
                i = j - 1
                break
        i += 1
    if not found:
        return None
    i, op = found
    return q[:i], op, q[i + len(op):]


def _args(s: str) -> list[str]:
    out, depth, quote, cur = [], 0, None, ""
    for ch in s:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    if cur.strip():
        out.append(cur)
    return [a.strip() for a in out]


def _sig(labels: dict[str, str], on: list[str] | None = None, ignoring: list[str] | None = None) -> tuple:
    keys = on if on is not None else [k for k in labels if k != "__name__" and k not in (ignoring or [])]
    return tuple(sorted((k, labels.get(k, "")) for k in keys))


def _drop_name(v: Vec) -> Vec:
    return [({k: x for k, x in lab.items() if k != "__name__"}, val) for lab, val in v]


_ARITH: dict[str, Callable[[float, float], float]] = {
    "+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b,
    "/": lambda a, b: a / b if b else (math.nan if a == 0 else math.copysign(math.inf, a)),
    "%": lambda a, b: math.fmod(a, b) if b else math.nan, "^": lambda a, b: a ** b,
    "==": lambda a, b: float(a == b), "!=": lambda a, b: float(a != b), ">": lambda a, b: float(a > b),
    "<": lambda a, b: float(a < b), ">=": lambda a, b: float(a >= b), "<=": lambda a, b: float(a <= b)}
_CMP = {"==", "!=", ">", "<", ">=", "<="}


def _binop(op: str, left: Val, right_expr: str, t: float) -> Val:
    mod = re.match(r"\s*(bool\b)?\s*(?:(on|ignoring)\s*\(([^)]*)\))?\s*(?:(group_left|group_right)\s*(?:\([^)]*\))?)?",
                   right_expr)
    is_bool = bool(mod.group(1))
    match_labels = [x.strip() for x in mod.group(3).split(",") if x.strip()] if mod.group(3) is not None else None
    on, ignoring = (match_labels, None) if mod.group(2) == "on" else (None, match_labels)
    right = evaluate(right_expr[mod.end():], t)
    if op in ("or", "and", "unless"):
        lv, rv = (left if isinstance(left, list) else []), (right if isinstance(right, list) else [])
        rs = {_sig(lab, on, ignoring) for lab, _ in rv}
        if op == "or":
            ls = {_sig(lab, on, ignoring) for lab, _ in lv}
            return lv + [s for s in rv if _sig(s[0], on, ignoring) not in ls]
        keep = (lambda s: s in rs) if op == "and" else (lambda s: s not in rs)
        return [s for s in lv if keep(_sig(s[0], on, ignoring))]
    fn = _ARITH[op]
    if not isinstance(left, list) and not isinstance(right, list):
        return fn(float(left), float(right))

    def emit(lab: dict, a: float, b: float, keep_val: float) -> tuple[dict, float] | None:
        r = fn(a, b)
        if op in _CMP and not is_bool:
            return (lab, keep_val) if r else None
        return lab, r

    out: Vec = []
    if not isinstance(right, list):
        out = [e for lab, v in _drop_name(left) if (e := emit(lab, v, float(right), v))]
    elif not isinstance(left, list):
        out = [e for lab, v in _drop_name(right) if (e := emit(lab, float(left), v, v))]
    else:
        lv, rv = _drop_name(left), _drop_name(right)
        index = {_sig(lab, on, ignoring): v for lab, v in rv}
        for lab, v in lv:
            b = index.get(_sig(lab, on, ignoring))
            if b is None and len(rv) == 1:
                b = rv[0][1]
            if b is not None and (e := emit(lab, v, b, v)):
                out.append(e)
        if not out and len(lv) == 1 and len(rv) > 1:        # one-to-many broadcast
            out = [e for lab, v in rv if (e := emit(lab, lv[0][1], v, lv[0][1]))]
    return out


def _select(expr: str, t: float) -> tuple[list[Series], float]:
    """Selector `name{matchers}[range] offset d` → series at t, and the range in seconds (0 for none)."""
    m = re.match(r"^([a-zA-Z_:][\w:]*)?\s*(\{.*?\})?\s*(\[[^\]]*\])?\s*(?:offset\s+(-?[\dsmhdwy.]+))?\s*(?:@\s*[\d.]+)?$",
                 expr.strip(), re.S)
    if not m:
        return [], 0.0
    name, matchers, rng, offset = m.groups()
    if offset:
        t -= _dur(offset)
    data = store(t)
    conds = re.findall(r"([a-zA-Z_][\w]*)\s*(=~|!~|!=|=)\s*\"((?:[^\"\\]|\\.)*)\"", matchers or "")
    if not name:
        name_conds = [c for c in conds if c[0] == "__name__"]
        names = [n for n in data if all(_match(n, o, v) for _, o, v in name_conds)] if name_conds else []
    else:
        names = [name]
    series = [s for n in names for s in data.get(n, [])
              if all(_match(s[0].get(k, ""), o, v) for k, o, v in conds if k != "__name__")]
    return series, (_dur(rng.strip("[]").split(":")[0]) if rng else 0.0)


def _match(value: str, op: str, pattern: str) -> bool:
    pattern = pattern.replace('\\"', '"')
    if op == "=":
        return value == pattern
    if op == "!=":
        return value != pattern
    ok = re.fullmatch(pattern, value) is not None
    return ok if op == "=~" else not ok


def _hq(q: float, v: Vec) -> Vec:
    groups: dict[tuple, list[tuple[float, float]]] = {}
    labels: dict[tuple, dict] = {}
    for lab, val in v:
        if "le" not in lab:
            continue
        rest = {k: x for k, x in lab.items() if k not in ("le", "__name__")}
        key = _sig(rest)
        labels[key] = rest
        groups.setdefault(key, []).append((math.inf if lab["le"] == "+Inf" else float(lab["le"]), val))
    out: Vec = []
    for key, buckets in groups.items():
        buckets.sort()
        total = buckets[-1][1]
        if total <= 0:
            out.append((labels[key], math.nan))
            continue
        rank, prev_le, prev_c = q * total, 0.0, 0.0
        for le, cnt in buckets:
            if cnt >= rank:
                if le == math.inf:
                    out.append((labels[key], prev_le))
                else:
                    span = cnt - prev_c
                    out.append((labels[key], prev_le + (le - prev_le) * ((rank - prev_c) / span if span else 1)))
                break
            prev_le, prev_c = le, cnt
    return out


def _aggregate(op: str, param: Val | None, v: Vec, by: list[str] | None, without: list[str] | None) -> Vec:
    if op in ("topk", "bottomk"):
        k = int(float(param or 1))
        return sorted(v, key=lambda s: s[1], reverse=op == "topk")[:k]
    groups: dict[tuple, list[float]] = {}
    labels: dict[tuple, dict] = {}
    for lab, val in v:
        if by is not None:
            keep = {k: lab[k] for k in by if k in lab}
        elif without is not None:
            keep = {k: x for k, x in lab.items() if k not in without and k != "__name__"}
        else:
            keep = {}
        key = _sig(keep)
        labels[key] = keep
        groups.setdefault(key, []).append(val)
    fns: dict[str, Callable[[list[float]], float]] = {
        "sum": sum, "max": max, "min": min, "avg": lambda xs: sum(xs) / len(xs), "count": len,
        "group": lambda xs: 1.0,
        "stddev": lambda xs: math.sqrt(sum((x - sum(xs) / len(xs)) ** 2 for x in xs) / len(xs)),
        "stdvar": lambda xs: sum((x - sum(xs) / len(xs)) ** 2 for x in xs) / len(xs),
        "quantile": lambda xs: sorted(xs)[min(len(xs) - 1, int(float(param or 0.5) * len(xs)))]}
    return [(labels[k], float(fns[op](xs))) for k, xs in groups.items()]


def evaluate(expr: str, t: float) -> Val:
    """Lenient PromQL over the synthetic store. Unknown things evaluate to an empty vector."""
    q = expr.strip()
    if not q:
        return []
    while q.startswith("(") and _close(q, 0) == len(q) - 1:
        q = q[1:-1].strip()
    for ops in (("or",), ("and", "unless"), ("==", "!=", ">=", "<=", ">", "<"), ("+", "-"), ("*", "/", "%"), ("^",)):
        parts = _top_split(q, ops)
        if parts:
            left, op, right = parts
            return _binop(op, evaluate(left, t), right, t)
    if re.fullmatch(r"[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?|[-+]?Inf|NaN", q):
        return float(q)
    if q[0] in "\"'":
        return q[1:-1]
    if q.startswith("-"):
        inner = evaluate(q[1:], t)
        return -inner if isinstance(inner, float) else [(lab, -v) for lab, v in _drop_name(inner)]
    m = re.match(r"^([a-zA-Z_:][\w:]*)\s*", q)
    if not m:                                   # `{__name__=~"…"}` and other name-less selectors
        return [(s[0], s[1]) for s in _select(q, t)[0]]
    name, rest = m.group(1), q[m.end():]
    if name in AGGS and (rest.startswith("(") or re.match(r"(by|without)\s*\(", rest)):
        by = without = None
        pre = re.match(r"(by|without)\s*\(([^)]*)\)\s*", rest)
        if pre:
            labs = [x.strip() for x in pre.group(2).split(",") if x.strip()]
            by, without = (labs, None) if pre.group(1) == "by" else (None, labs)
            rest = rest[pre.end():]
        end = _close(rest, 0)
        args, tail = _args(rest[1:end]), rest[end + 1:].strip()
        post = re.match(r"(by|without)\s*\(([^)]*)\)", tail)
        if post:
            labs = [x.strip() for x in post.group(2).split(",") if x.strip()]
            by, without = (labs, None) if post.group(1) == "by" else (None, labs)
        param = evaluate(args[0], t) if len(args) > 1 else None
        inner = evaluate(args[-1], t) if args else []
        return _aggregate(name, param, inner if isinstance(inner, list) else [], by, without)
    if rest.startswith("("):
        end = _close(rest, 0)
        return _function(name, _args(rest[1:end]), t)
    return [(s[0], s[1]) for s in _select(q, t)[0]]


def _function(name: str, args: list[str], t: float) -> Val:
    if name == "time":
        return t
    if name == "vector":
        v = evaluate(args[0], t)
        return [({}, float(v))] if not isinstance(v, list) else v
    if name == "scalar":
        v = evaluate(args[0], t)
        return v if not isinstance(v, list) else (v[0][1] if len(v) == 1 else math.nan)
    if name == "histogram_quantile":
        q, v = evaluate(args[0], t), evaluate(args[1], t)
        return _hq(float(q), v if isinstance(v, list) else [])
    if name in ("rate", "irate", "increase", "delta", "idelta", "deriv"):
        series, rng = _select(args[0], t)
        if not series:          # e.g. rate(x[5m] offset …) forms the selector doesn't parse: be honest
            return []
        mult = rng if name == "increase" else 1.0
        return [({k: v for k, v in lab.items() if k != "__name__"}, (r or 0.0) * mult if name in ("rate", "irate", "increase")
                 else 0.0) for lab, _, r in series]
    if name.endswith("_over_time"):
        series, _ = _select(args[-1], t)
        factor = {"max_over_time": 1.08, "min_over_time": 0.92}.get(name, 1.0)
        if name == "count_over_time":
            return [({k: v for k, v in lab.items() if k != "__name__"}, 60.0) for lab, _, _ in series]
        return [({k: v for k, v in lab.items() if k != "__name__"}, val * factor) for lab, val, _ in series]
    if name == "absent":
        v = evaluate(args[0], t)
        return [] if (isinstance(v, list) and v) else [({}, 1.0)]
    unary: dict[str, Callable[[float], float]] = {"abs": abs, "ceil": math.ceil, "floor": math.floor, "round": round,
                                                  "exp": math.exp, "sqrt": math.sqrt,
                                                  "ln": lambda x: math.log(x) if x > 0 else math.nan}
    first = evaluate(args[0], t) if args else []
    if not isinstance(first, list):
        return first
    if name in unary:
        return [(lab, float(unary[name](v))) for lab, v in _drop_name(first)]
    if name in ("clamp_min", "clamp_max", "clamp") and len(args) > 1:
        lo = float(evaluate(args[1], t)) if name != "clamp_max" else -math.inf
        hi = float(evaluate(args[-1], t)) if name != "clamp_min" else math.inf
        return [(lab, min(max(v, lo), hi)) for lab, v in _drop_name(first)]
    if name in ("sort", "sort_desc"):
        return sorted(first, key=lambda s: s[1], reverse=name == "sort_desc")
    return first                # label_replace, last_over_time-likes, anything else: pass the vector through


def _fmt(v: float) -> str:
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return repr(round(v, 6)) if v != int(v) or abs(v) >= 1e15 else str(int(v))


def prom_instant(query: str, t: float) -> dict:
    try:
        v = evaluate(query, t)
    except (ValueError, KeyError, IndexError, TypeError, ZeroDivisionError, OverflowError):
        v = []
    if not isinstance(v, list):
        return {"resultType": "scalar", "result": [t, _fmt(float(v)) if not isinstance(v, str) else v]}
    return {"resultType": "vector", "result": [{"metric": lab, "value": [t, _fmt(val)]} for lab, val in v]}


def prom_range(query: str, start: float, end: float, step: float) -> dict:
    step = max(step, 1.0)
    if (end - start) / step > 11000:
        raise ValueError("exceeded maximum resolution of 11,000 points per timeseries")
    series: dict[tuple, dict] = {}
    t = start
    while t <= end + 1e-9:
        try:
            v = evaluate(query, t)
        except (ValueError, KeyError, IndexError, TypeError, ZeroDivisionError, OverflowError):
            v = []
        if not isinstance(v, list):
            v = [({}, float(v))] if not isinstance(v, str) else []
        for lab, val in v:
            s = series.setdefault(_sig(lab) + (("__name__", lab.get("__name__", "")),), {"metric": lab, "values": []})
            s["values"].append([t, _fmt(val)])
        t += step
    return {"resultType": "matrix", "result": list(series.values())}


# ── background work: discovery, benchmarks, downloads and batch progress ─────────────────────

def spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    W.bg.add(task)
    task.add_done_callback(W.bg.discard)


async def _discovery(w: World, run: dict) -> None:
    await asyncio.sleep(6)
    if w is not W:
        return
    cats = run.pop("_categories")
    funnel = {c: _funnel(150 + 40 * i, 0, []) for i, c in enumerate(cats)}
    if "vision" in cats and PVISC not in w.models:
        repo = "ggml-org/gemma-3-4b-it-GGUF"
        prof = _profile(PVISC, repo, "vision", 3.9, budget=4096)
        meta = _meta(repo, "vision", 3.9, downloads=61_000, likes=180, age_days=120, t0=w.t0, family="Gemma3",
                     quant="Q4_K_M", size=2_490_000_000)
        meta.update(pipeline_tag="image-text-to-text", tags=["gguf", "image-text-to-text", "vision"],
                    architecture="Gemma3ForConditionalGeneration", mmproj_listed=True)
        meta["gguf_pick"]["mmproj"] = dict(prof["mmproj"])
        w.models[PVISC] = {"id": PVISC, "model_id": repo, "revision": prof["revision"], "category": "vision",
                           "state": "CANDIDATE", "meta": meta, "profile": prof,
                           "fit": _fit("fits_cpu", 2380, 640, 14.0, ["CPU tier: weights + image projector fit"]),
                           "screening": _screening("shortlist", "Small vision model with an image projector; "
                                                   "fits the CPU tier", 0.68),
                           "reason": f"shortlisted by discovery run {run['id']}", "pinned": False, "blocked": False,
                           "created": time.time(), "updated": time.time()}
        funnel["vision"] = _funnel(120, 1, [repo])
    run.update(status="succeeded", finished=time.time(), funnel={"categories": funnel, "total_ms": 6000})
    w.running_run = None
    w.tasks["discovery"] = "done"
    w.log("discovery_finished", f"run {run['id']}", "console", {"shortlisted": 0, "total_ms": 6000})


async def _download(w: World, mid: str) -> None:
    await asyncio.sleep(4)
    if w is not W or mid not in w.models:
        return
    _transition(w, mid, "STAGED", "checksum verified")
    w.log("download_complete", mid, "operator", {"job": "dl-" + _sha(mid, 10)})
    w.tasks[f"download:{mid}"] = "done"


async def _benchmark(w: World, mid: str) -> None:
    m = w.models[mid]
    live = m["state"] in ("PRODUCTION", "CANARY", "STANDBY")
    if not live:
        _transition(w, mid, "VALIDATING", "starting test server")
        await asyncio.sleep(3)
        if w is not W:
            return
        w.log("load_test_passed", mid, "operator", {"load_sec": 3.0})
        _transition(w, mid, "BENCHMARKING", "running core suite")
    await asyncio.sleep(5)
    if w is not W or mid not in w.models:
        return
    summary = _core_summary(0.733, 395.0, 17.9) if mid == P4B else _core_summary(0.667, 360.0, 19.5)
    w.benchmarks.append({"id": len(w.benchmarks) + 1, "model": mid, "ts": time.time(), "suite": "core",
                         "results": _results(mid, summary["quality"]), "summary": summary})
    w.log("benchmark_completed", mid, "operator", {"suite": "core", "quality": summary["quality"], "json_valid": 1.0,
                                                  "ttft_ms_p50": summary["ttft_ms_p50"],
                                                  "decode_tps_p50": summary["decode_tps_p50"], "errors": 0})
    if not live:
        rec = "HOLD"
        m["screening"]["comparison"] = {"thresholds": {"max_quality_regression": 0.01}, "checks": {"quality": True},
                                        "delta": {"quality": -0.066, "ttft_pct": -12.6, "decode_tps_pct": 10.8,
                                                  "throughput_pct": 8.0, "memory_pct": 0.0, "structured_ok": 1.0},
                                        "recommendation": rec, "failed_checks": [], "incumbent": P4B,
                                        "alias": CATEGORY_ALIAS.get(m["category"], "local/default"),
                                        "jev": {"improves": "no", "confidence": 0.58, "provider": "jev", "p_improves": 0.42}}
        w.log("comparison_complete", mid, "operator", {"recommendation": rec, "incumbent": P4B})
        _transition(w, mid, "APPROVED", f"comparison: {rec}")
    w.tasks[f"benchmark:{mid}"] = "done"


async def _batch_ticker() -> None:
    """One scheduling pass per second, shaped like BatchEngine.tick(): ONE pool of 2 in-flight items
    across all jobs, filled in (priority, deadline, created) order, so lower-priority jobs stay queued
    behind a busy one. Each pass finishes one in-flight item. Blocked jobs keep only a `waiting: …`
    reason; their state does not change (the engine has no waiting state)."""
    while True:
        await asyncio.sleep(1.0)
        w = W
        if w.scenario == "offline":
            continue
        now = time.time()
        active = sorted((j for j in w.jobs.values() if j["state"] in ("queued", "running")),
                        key=lambda j: (j["priority"], j["deadline"] or math.inf, j["created"]))
        done = next((j for j in active if j["counts"]["running"]), None)
        if done:
            c = done["counts"]
            c["running"] -= 1
            n = sum(c.values()) - c["pending"] - c["running"]
            c["failed" if n % 37 == 36 else "succeeded"] += 1
            done["updated"] = now
        free = 2 - sum(j["counts"]["running"] for j in active)
        for j in active:
            gate = batch_gate(w, j["priority"])
            if gate:
                j["reason"] = gate
                continue
            c = j["counts"]
            take = min(free, c["pending"])
            if take:
                c["pending"] -= take
                c["running"] += take
                free -= take
                j.update(state="running", reason="", updated=now)
            if not c["pending"] and not c["running"]:
                j["state"] = "failed" if c["failed"] and not c["succeeded"] else "completed"
                j.update(finished=now, updated=now)


def _transition(w: World, mid: str, to: str, reason: str, actor: str = "operator") -> None:
    m = w.models[mid]
    w.log("state_changed", mid, actor, {"frm": m["state"], "to": to, "reason": reason})
    m.update(state=to, reason=reason, updated=time.time())


def _new_alias_version(w: World, alias: str, chain: list[str], canary: dict | None, actor: str, note: str) -> dict:
    prev = current_alias(w, alias)
    row = {"alias": alias, "version": (prev["version"] + 1) if prev else 1, "chain": chain, "canary": canary,
           "ts": time.time(), "actor": actor, "note": note}
    w.alias_versions.setdefault(alias, []).append(row)
    w.log("alias_changed", alias, actor, {"version": row["version"], "chain": chain, "canary": canary, "note": note,
                                          "previous": prev["chain"] if prev else []})
    return row


# ── app ─────────────────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    ticker = asyncio.get_running_loop().create_task(_batch_ticker())
    yield
    ticker.cancel()


W = build(os.environ.get("FAKE_SCENARIO", "healthy"))
app = FastAPI(title="LIF fake upstreams (dev/e2e only)", lifespan=lifespan, docs_url=None, redoc_url=None)

CONTROLLER_OPEN = {"/v1/routing", "/healthz", "/readyz", "/metrics"}
GATEWAY_PATHS = {"/v1/capabilities", "/v1/chat/completions"}
GATEWAY_OPEN = {"/v1/health"}


def c_err(status: int, msg: str) -> JSONResponse:          # controller envelope
    return JSONResponse({"error": msg}, status_code=status)


def g_err(status: int, msg: str, type_: str, **extra: Any) -> JSONResponse:   # gateway envelope
    return JSONResponse({"error": {"message": msg, "type": type_, **extra}}, status_code=status)


def b_err(status: int, msg: str) -> JSONResponse:          # batch envelope
    return JSONResponse({"error": {"message": msg}}, status_code=status)


@app.middleware("http")
async def scenario_and_auth(request: Request, call_next):
    path = request.url.path
    if path.startswith("/__"):
        return await call_next(request)
    if W.scenario == "offline":
        if path.startswith("/api/v1/"):
            return JSONResponse({"status": "error", "errorType": "unavailable", "error": "service unavailable"}, 503)
        return JSONResponse({"error": "service unavailable (fake offline scenario)"}, 503)
    auth = request.headers.get("authorization", "")
    if path in GATEWAY_PATHS and auth != f"Bearer {GATEWAY_KEY}":
        return g_err(401, "missing or invalid API key", "auth")
    if (path.startswith("/v1/") and path not in GATEWAY_PATHS | GATEWAY_OPEN | CONTROLLER_OPEN
            and not path.startswith("/v1/batch") and auth != f"Bearer {ADMIN_KEY}"):
        return c_err(401, "unauthorized")
    return await call_next(request)


@app.get("/__scenario")
async def get_scenario() -> dict:
    return {"name": W.scenario, "scenarios": list(SCENARIOS), "since": W.t0}


@app.post("/__scenario")
async def set_scenario(request: Request) -> JSONResponse:
    """Switch (or reset, when the name is unchanged) the world. Background work of the old world is
    cancelled so nothing from it leaks into the new one."""
    global W
    body = await _json(request)
    name = body.get("name", "")
    if name not in SCENARIOS:
        return JSONResponse({"error": f"unknown scenario {name!r}", "scenarios": list(SCENARIOS)}, 400)
    for task in list(W.bg):
        task.cancel()
    W = build(name)
    _STORE_CACHE.clear()
    return JSONResponse({"name": name})


def install_vision(w: World, on: bool) -> None:
    """Dev toggle: a deployed local/vision model (or none, as in the real world today)."""
    if on and PVIS not in w.models:
        repo = "Qwen/Qwen2.5-VL-3B-Instruct-GGUF"
        prof = _profile(PVIS, repo, "vision", 3.8, budget=4608, endpoint="http://vision.ai-serving.svc:8080")
        w.models[PVIS] = {"id": PVIS, "model_id": repo, "revision": prof["revision"], "category": "vision",
                          "state": "PRODUCTION", "meta": {"model_id": repo, "params_b": 3.8, "license": "apache-2.0"},
                          "profile": prof, "fit": {}, "screening": {}, "reason": "installed (dev toggle)",
                          "pinned": False, "blocked": False, "created": time.time(), "updated": time.time()}
        w.alias_versions["local/vision"] = [{"alias": "local/vision", "version": 1, "chain": [PVIS], "canary": None,
                                             "ts": time.time(), "actor": "operator", "note": f"promote {PVIS}"}]
        w.log("state_changed", PVIS, "operator", {"from": "APPROVED", "to": "PRODUCTION"})
    elif not on and PVIS in w.models:
        del w.models[PVIS]
        w.alias_versions.pop("local/vision", None)


@app.get("/__vision")
async def get_vision() -> dict:
    return {"installed": PVIS in W.models}


@app.post("/__vision")
async def set_vision(request: Request) -> dict:
    """POST {"installed": true|false}. A scenario reset uninstalls it again."""
    body = await _json(request)
    install_vision(W, bool(body.get("installed")))
    return {"installed": PVIS in W.models}


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# ── controller ──

@app.get("/v1/overview")
async def overview() -> dict:
    counts = {s: 0 for s in COUNTED_STATES}
    for m in W.models.values():
        if m["state"] in counts:
            counts[m["state"]] += 1
    return {"blerbz": gpu_snapshot(W), "capabilities": capabilities(W), "decision_fabric": fabric_status(W),
            "batch": batch_stats(W), "availability_24h": availability(W, "24h"),
            "availability_7d": availability(W, "7d"), "settings": dict(W.settings), "tasks": dict(W.tasks),
            "memory_guard": memory_guard(W), "models": counts, "controller_uptime_sec": int(time.time() - W.t0) + 30 * 3600}


@app.get("/v1/gpu")
async def gpu() -> dict:
    return gpu_snapshot(W)


@app.get("/v1/routing")
async def routing() -> dict:
    canaries = {a: v[-1]["canary"] for a, v in W.alias_versions.items() if v[-1]["canary"]}
    return {"profiles": routing_profiles(W), "aliases": alias_chains(W), "canaries": canaries,
            "jev_enabled": not W.settings["jev_disabled"], "alias_min_params_b": dict(ALIAS_FLOOR),
            "generated": time.time() - 4}


@app.get("/v1/aliases")
async def aliases() -> dict:
    return {a: dict(v[-1]) for a, v in W.alias_versions.items()}       # empty chains are absent, as live


@app.post("/v1/aliases/rollback")
async def alias_rollback(request: Request) -> JSONResponse:
    body = await _json(request)
    alias = body.get("alias")
    versions = W.alias_versions.get(alias or "")
    if not versions:
        return c_err(404, f"not found: '{alias}'")
    cur = versions[-1]
    if body.get("to_version") is not None:
        target = next((v for v in versions if v["version"] == int(body["to_version"])), None)
    else:
        target = next((v for v in reversed(versions[:-1]) if not v["canary"] and v["chain"] != cur["chain"]), None)
    if not target:
        return c_err(409, f"no earlier version of {alias} to roll back to")
    row = {"alias": alias, "version": cur["version"] + 1, "chain": list(target["chain"]), "canary": None,
           "ts": time.time(), "actor": "operator", "note": f"rollback to v{target['version']}"}
    versions.append(row)
    W.log("alias_rollback", alias, "operator", {"to_version": target["version"], "chain": row["chain"]})
    return JSONResponse(row)


@app.get("/v1/models")
async def models(state: str = "", category: str = "") -> dict:
    states = {s.strip().upper() for s in state.split(",") if s.strip()}
    rows = [model_row(W, m) for m in W.models.values()
            if (not states or m["state"] in states) and (not category or m["category"] == category)]
    return {"models": sorted(rows, key=lambda r: -r["updated"])}


@app.post("/v1/models/refresh")
async def refresh(request: Request) -> dict:
    body = await _json(request)
    if W.running_run is not None:
        return {"status": "already_running", "task": "discovery"}
    W.tasks["discovery"] = "done"
    if W.settings["discovery_disabled"] or W.settings["maintenance"]:
        return {"status": "started", "task": "discovery"}       # the real 'disabled' outcome is swallowed
    cats = body.get("categories") or ["fast", "general", "coding", "reasoning"]
    run = {"id": max(r["id"] for r in W.runs) + 1, "ts": time.time(), "finished": None, "status": "running",
           "funnel": {}, "error": "", "_categories": cats}
    W.runs.append(run)
    W.running_run = run["id"]
    W.tasks["discovery"] = "running"
    W.log("discovery_started", f"run {run['id']}", "console", {"categories": cats})
    spawn(_discovery(W, run))
    return {"status": "started", "task": "discovery"}


@app.get("/v1/models/{mid}")
async def model(mid: str) -> JSONResponse:
    m = W.models.get(mid)
    if not m:
        return c_err(404, "not found")
    bench = [b for b in reversed(W.benchmarks) if b["model"] == mid][:10]
    act = [a for a in reversed(W.activity[-500:]) if a["subject"] == mid][:50]
    return JSONResponse({**model_row(W, m), "benchmarks": bench, "activity": act})


@app.delete("/v1/models/{mid}")
async def delete_model(mid: str) -> JSONResponse:
    m = W.models.get(mid)
    if not m:
        return JSONResponse({"ok": True})                      # real: no existence check
    if m["state"] in ("PRODUCTION", "CANARY"):
        return c_err(409, f"refusing to delete {mid}: state {m['state']}")
    if m["pinned"]:
        return c_err(409, f"refusing to delete {mid}: pinned")
    if any(mid in chain for chain in alias_chains(W).values()):
        return c_err(409, f"refusing to delete {mid}: referenced by an alias")
    del W.models[mid]
    W.log("model_deleted", mid, "operator")
    return JSONResponse({"ok": True})


@app.post("/v1/models/{mid}/{action}")
async def model_action(mid: str, action: str, request: Request) -> JSONResponse:
    body = await _json(request)
    if action in ("pin", "unpin", "block", "unblock"):
        flag = "pinned" if action.endswith("pin") else "blocked"
        if mid in W.models:
            W.models[mid][flag] = not action.startswith("un")
            W.log(f"model_{action}ned" if action.endswith("pin") else f"model_{action}ed", mid, "operator")
        return JSONResponse({"ok": True})
    if action not in ("download", "benchmark", "load", "unload", "canary", "promote"):
        return c_err(404, "unknown action")
    m = W.models.get(mid)
    if not m:
        return c_err(409, f"unknown model {mid}")
    snap = gpu_snapshot(W)
    if action in ("download", "benchmark"):
        key = f"{action}:{mid}"
        if W.tasks.get(key) == "running":
            return JSONResponse({"status": "already_running", "task": key})
        if W.settings["maintenance"]:
            return c_err(409, "maintenance mode")
        if snap["state"] in ("HIGH", "IMMINENT"):
            return c_err(409, f"Primary workload {snap['state']} ({snap['reason']}); {action}s deferred")
        if action == "download":
            if m["state"] != "CANDIDATE":
                return c_err(409, f"cannot download a model in state {m['state']}")
            _transition(W, mid, "DOWNLOADING", "download started")
            W.log("download_started", mid, "operator", {"job": "dl-" + _sha(mid, 10), "size": m["profile"].get("size")})
            W.tasks[key] = "running"
            spawn(_download(W, mid))
            return JSONResponse({"status": "started", "task": key})
        if m["profile"]["device"] != "cpu":
            return c_err(409, "GPU-tier benchmarks need a gpusched command-job window")
        if m["state"] not in ("STAGED", "APPROVED", "PRODUCTION", "CANARY", "STANDBY", "REJECTED"):
            return c_err(409, f"cannot benchmark a model in state {m['state']}")
        if snap["mem_available_mib"] < HEADROOM_MIB + 1024:
            return c_err(409, f"not enough host headroom: MemAvailable {snap['mem_available_mib']:.0f} MiB < 9216 MiB")
        W.tasks[key] = "running"
        spawn(_benchmark(W, mid))
        return JSONResponse({"status": "started", "task": key})
    if action == "load":
        if m["profile"]["device"] != "cpu":
            return c_err(409, "GPU-tier models load only in a gpusched command-job window")
        dep = {P4B: "tier0", P17B: "tier0-small", PEMB: "embedding"}.get(mid, "lif-" + _sha(mid, 10))
        if not m["profile"].get("endpoint"):
            m["profile"]["endpoint"] = f"http://{dep}.ai-serving.svc:8080"
        W.log("model_loaded", mid, "operator", {"deployment": dep})
        return JSONResponse({"deployment": dep})
    if action == "unload":
        used = sorted(a for a, chain in alias_chains(W).items() if mid in chain)
        if used and not body.get("force"):
            return c_err(409, f"{mid} is used by {', '.join(used)}; pass force to unload anyway")
        dep = {P4B: "tier0", P17B: "tier0-small", PEMB: "embedding"}.get(mid, "lif-" + _sha(mid, 10))
        W.log("model_unloaded", mid, "operator", {"deployment": dep, "aliases_affected": used})
        return JSONResponse({"deployment": dep, "aliases_affected": used})
    alias = body.get("alias") or CATEGORY_ALIAS.get(m["category"], "local/default")
    cur = current_alias(W, alias)
    chain = list(cur["chain"]) if cur else []
    if action == "canary":
        if m["state"] not in ("APPROVED", "STANDBY"):
            return c_err(409, f"cannot canary a model in state {m['state']}")
        if not m["profile"].get("endpoint"):
            m["profile"]["endpoint"] = f"http://lif-{_sha(mid, 10)}.ai-serving.svc:8080"
        canary = {"profile": mid, "percent": body.get("percent", 10), "since": time.time()}
        _transition(W, mid, "CANARY", f"canary on {alias}")
        return JSONResponse(_new_alias_version(W, alias, chain, canary, "operator", f"canary {mid}"))
    if m["state"] not in ("CANARY", "APPROVED", "STANDBY"):
        return c_err(409, f"cannot promote a model in state {m['state']}")
    if not m["profile"].get("endpoint"):
        m["profile"]["endpoint"] = f"http://lif-{_sha(mid, 10)}.ai-serving.svc:8080"
    _transition(W, mid, "PRODUCTION", f"promoted on {alias}")
    keep_canary = cur["canary"] if cur and cur["canary"] and cur["canary"]["profile"] != mid else None
    return JSONResponse(_new_alias_version(W, alias, [mid] + [p for p in chain if p != mid], keep_canary, "operator",
                                           f"promote {mid}"))


@app.get("/v1/benchmarks")
async def benchmarks(model: str = "") -> dict:
    rows = [b for b in reversed(W.benchmarks) if not model or b["model"] == model][:100]
    return {"benchmarks": rows}


@app.get("/v1/discovery/runs")
async def discovery_runs() -> dict:
    runs = [{k: v for k, v in r.items() if not k.startswith("_")} for r in reversed(W.runs)][:20]
    return {"runs": runs, "running": W.running_run}


@app.get("/v1/activity")
async def activity(limit: int = 200, since: int = 0) -> dict:
    rows = [a for a in reversed(W.activity) if a["seq"] > since]
    return {"activity": rows[:max(1, min(limit, 2000))]}


@app.get("/v1/settings")
async def get_settings() -> dict:
    return dict(W.settings)


@app.post("/v1/settings")
async def post_settings(request: Request) -> JSONResponse:
    body = await _json(request)
    unknown = sorted(k for k in body if k not in W.settings)
    if unknown:
        return c_err(400, f"unknown settings {unknown}")
    for k, v in body.items():
        W.settings[k] = v
        W.log("setting_changed", k, "console", {"value": v})
    if "batch_paused" in body or "maintenance" in body:
        # mirrors controller/app.py: the pause sent to batch reads the REQUEST body only, so posting
        # {batch_paused: false} while maintenance is on still un-pauses batch (a real, known bug)
        W.batch_paused = bool(body.get("batch_paused") or body.get("maintenance"))
        W.batch_paused_reason = "operator" if W.batch_paused else ""
        _apply_batch_gate(W)
    return JSONResponse(dict(W.settings))


@app.get("/v1/availability")
async def get_availability(window_sec: int = 86400) -> dict:
    return availability(W, "24h" if window_sec <= 86400 else "7d")


@app.get("/v1/storage")
async def storage() -> dict:
    groups: dict[str, dict] = {}
    klass = {"PRODUCTION": "production", "CANARY": "production", "STANDBY": "rollback", "APPROVED": "candidate",
             "STAGED": "candidate", "REJECTED": "failed", "FAILED": "failed", "CANDIDATE": "metadata_only",
             "DISCOVERED": "metadata_only"}
    for m in W.models.values():
        k = klass.get(m["state"])
        if not k:
            continue
        g = groups.setdefault(k, {"count": 0, "bytes": 0, "models": []})
        g["count"] += 1
        g["bytes"] += 0 if k == "metadata_only" else int(m["profile"].get("size") or 0)   # seeded: no size (real gap)
        g["models"].append(m["id"])
    return groups


@app.get("/v1/savings")
async def savings(window: str = "24h") -> dict:
    scale = {"1h": 1 / 24, "24h": 1.0, "7d": 7.0, "30d": 30.0}.get(window, 1.0)
    req = int(1400 * scale)
    return {"window": window,
            "measured": {"requests": req, "tasks_by_tier": {"fast_local": int(req * 0.78), "deterministic": int(req * 0.12),
                                                            "jev": int(req * 0.1), "large_local": 0},
                         "decisions_by_provider": {"jev": int(61 * scale), "rules": int(74 * scale)},
                         "local_tokens": {"input": int(2_600_000 * scale), "output": int(780_000 * scale)},
                         "external_calls": 0},
            "kpi": {"heavy_model_avoidance": 1.0, "llm_avoidance": 0.22},
            "estimates_usd": {"api_equivalent_value": round(0.86 * scale, 2), "jev_spend": round(0.012 * scale, 3),
                              "local_power_cost": round(0.31 * scale, 2), "net_savings": round(0.54 * scale, 2),
                              "basis": "ESTIMATE: API-equivalent token prices minus Jev spend and local power"},
            "availability_target": 0.95}


@app.get("/v1/de/human")
async def de_human(status: str = "") -> dict:
    rows = [{k: v for k, v in t.items() if not k.startswith("_")} for t in W.tickets if not status or t["status"] == status]
    return {"queue": sorted(rows, key=lambda t: -t["ts"])[:200]}


@app.post("/v1/de/human/{tid}")
async def de_answer(tid: int, request: Request) -> JSONResponse:
    body = await _json(request)
    t = next((x for x in W.tickets if x["id"] == tid), None)
    if not t:
        return c_err(400, f"unknown ticket {tid}")
    if body.get("answer") not in t["package"]["labels"]:
        return c_err(400, f"answer must be one of {t['package']['labels']}")
    # no status check: the real queue lets an answered ticket be answered again (the console must guard)
    t.update(status="answered", answer=body["answer"], reviewer="console", resolved_ts=time.time())
    W.log("decision_human", f"human/{tid}", "console", {"answer": body["answer"]})
    return JSONResponse({"ticket": tid, "status": "answered"})


@app.get("/v1/de/traces")
async def de_traces(limit: int = 50) -> dict:
    runs = sorted((t["summary"] for t in W.traces.values()), key=lambda r: -r["t0"])
    return {"runs": runs[:limit]}


@app.get("/v1/de/traces/{run_id}")
async def de_trace(run_id: str) -> JSONResponse:
    t = W.traces.get(run_id)
    if not t:
        return c_err(404, "not found")
    return JSONResponse({"run_id": run_id, "operations": t["operations"]})


@app.get("/v1/de/cycle")
async def de_cycle() -> dict:
    started = W.t0 - 3 * 3600
    return {"started": started, "mined": {"runs": 2, "operations": 13},
            "calibrated": [{"ref": "model-advance/v1", "high": None, "adequate": False}],
            "recommendations": [{"decision": "model-advance/v1", "action": "collect_labels",
                                 "why": "not enough human-labelled samples to calibrate a threshold yet",
                                 "evidence": {"n_labelled": 1, "needed": 30}, "command": "", "severity": "info",
                                 "applies_automatically": False}],
            "elapsed_s": 1.8}


def _providers_runtime() -> dict:
    return {"local_reasoning": {"name": "local-reasoning", "kind": "reasoning", "tier": "local_reasoning",
                                "privacy": "local", "enabled": True, "alias": "local/reasoning", "breaker_open": False},
            "local_fast": {"name": "local-fast", "kind": "classifier", "tier": "local_fast", "privacy": "local",
                           "enabled": True, "alias": "local/instant", "breaker_open": False},
            "kimi": {"name": "kimi-k3", "kind": "reasoning", "tier": "kimi", "privacy": "external_llm", "enabled": False,
                     "model": "kimi-k3", "breaker_open": False, "reason": "no MOONSHOT_API_KEY secret"},
            "frontier": {"name": "frontier", "kind": "reasoning", "tier": "frontier", "privacy": "external_llm",
                         "enabled": False, "reason": "disabled in providers.yaml"},
            "human": {"name": "human-review", "kind": "human", "tier": "human", "privacy": "local", "enabled": True}}


@app.get("/v1/de/providers")
async def de_providers() -> dict:
    return {"providers": {"jev": {"kind": "decision", "model": "jev-synthetic", "pricing": {"per_question_usd": 0.0001},
                                  "rate_limit": {"rpm": 600}, "privacy": "jev"},
                          "kimi-k3": {"kind": "reasoning", "model": "kimi-k3",
                                      "pricing": {"input_per_mtok_usd": 0.6, "output_per_mtok_usd": 2.5},
                                      "rate_limit": {"rpm": 60}, "privacy": "external_llm"},
                          "local-reasoning": {"kind": "reasoning", "model": "local/reasoning", "pricing": {},
                                              "rate_limit": {}, "privacy": "local"}},
            "runtime": _providers_runtime(),
            "jev": {"model": "jev-synthetic", "enabled": not W.settings["jev_disabled"], "breaker_open": False}}


@app.get("/v1/de/overview")
async def de_overview(hours: int = 24) -> dict:
    pending = sum(1 for t in W.tickets if t["status"] == "pending")
    return {"window_hours": hours, "decisions": 142, "share": {"code": 0.12, "jev": 0.43, "rules": 0.4, "human": 0.05},
            "by_route": {"auto": 58, "validated": 41, "baseline": 36, "human": 7}, "jev_auto_rate": 0.41,
            "decision_offload_rate": 0.88, "frontier_generation_avoidance_rate": 1.0, "heavy_escalations": 0,
            "jev_resolved": 99, "median_decision_latency_ms": 164.0, "median_escalation_latency_ms": None,
            "agent_steps": 13, "human_pending": pending, "note": "synthetic data from the e2e fake upstreams",
            "cost_unknown_tiers": ["local_reasoning"], "cost_usd": 0.0122, "providers": _providers_runtime(),
            "registry": [{"name": "model-advance", "serving": None, "stage": "designed", "rollout_pct": 0},
                         {"name": "request-route", "serving": "request-route/v1", "stage": "production", "rollout_pct": 100}],
            "inventory_generated_at": W.t0 - 7200}


# ── gateway ──

@app.get("/v1/health")
async def gw_health() -> dict:
    caps = capabilities(W)["aliases"]
    useful = caps["local/fast"].get("available") or caps["local/default"].get("available")
    snap = gpu_snapshot(W)
    return {"status": "ok" if useful else "degraded", "gateway": "ready", "useful_local_ai": bool(useful),
            "blerbz": {"state": snap["state"], "reason": snap["reason"]},
            "decision_fabric": {"jev_enabled": not W.settings["jev_disabled"], "jev_breaker_open": False},
            "routing_table": "controller", "uptime_sec": int(time.time() - W.t0) + 26 * 3600}


@app.get("/v1/capabilities")
async def gw_capabilities() -> dict:
    return capabilities(W)


def _route_auto(text: str, data_class: str) -> tuple[str, dict]:
    """A deterministic stand-in for the request-route decision (labels and actions as the real rules)."""
    low = text.lower()
    if re.search(r"\b(code|python|function|bug|regex|json|parser|script|typescript)\b", low):
        label, conf = "code", 0.75
    elif re.search(r"\b(why|prove|derive|plan|analy[sz]e|compare)\b", low):
        label, conf = "reasoning", 0.65
    elif len(low) < 60:
        label, conf = "instant", 0.7
    else:
        label, conf = "default", 0.7
    if data_class == "PUBLIC" and not W.settings["jev_disabled"]:
        decision = {"decision": label, "confidence": round(min(0.97, conf + 0.22), 3), "provider": "jev", "action": "auto"}
    else:
        decision = {"decision": label, "confidence": conf, "provider": "rules",
                    "action": "local_llm" if label == "reasoning" else "validate"}
    alias = f"local/{label}" if decision["action"] in ("auto", "validate") else "local/default"
    return alias, decision


def _answer(prompt: str, alias: str, images: int = 0) -> str:
    topic = " ".join(prompt.split()[:8]) or "your request"
    if images:
        return (f"**What I see:** {'an image' if images == 1 else f'{images} images'} — a synthetic description "
                f"from the e2e fake vision model about “{topic}”.\n\n"
                "- The picture arrived as an `image_url` content part, already downscaled by the browser.\n"
                "- Nothing here was generated by a model, and the image is not kept.")
    if alias == "local/code" or "```" in prompt or re.search(r"\b(code|python|parser|function)\b", prompt.lower()):
        return ("Here is a small, self-contained starting point.\n\n"
                "```python\nimport json\n\n\ndef parse(text: str) -> dict:\n"
                "    \"\"\"Parse a JSON object and fail with a clear message.\"\"\"\n"
                "    data = json.loads(text)\n    if not isinstance(data, dict):\n"
                "        raise ValueError(\"expected a JSON object\")\n    return data\n```\n\n"
                "It raises `ValueError` for anything that is not an object, so callers can show a clear error. "
                "(Synthetic answer from the e2e fake gateway.)")
    return (f"**Short answer:** this is a synthetic reply about “{topic}”.\n\n"
            "The e2e fake gateway streams it a token at a time, at about the speed of the local CPU tier, so the "
            "console's streaming caret, receipt and routing trail can be checked without real inference.\n\n"
            "- It names the model that served it in the final usage chunk.\n"
            "- It carries the same `X-LIF-*` headers as the real gateway.\n"
            "- Nothing here was generated by a model.")


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Response:
    body = await _json(request)
    requested = body.get("model") or "local/default"
    messages = body.get("messages") or []
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    content = last_user.get("content", "")
    text = content if isinstance(content, str) else " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    n_images = 0 if isinstance(content, str) else sum(
        1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
    data_class = request.headers.get("x-lif-data-class", "CONFIDENTIAL").upper()
    snap = gpu_snapshot(W)

    route_decision = None
    alias = requested
    if n_images and requested == "local/auto":           # the gateway sends images to the vision alias
        alias, route_decision = "local/vision", {"decision": "vision", "confidence": 1.0, "provider": "rules",
                                                 "action": "auto"}
    elif n_images and requested != "local/vision":
        return g_err(422, f"{requested} does not accept images; use local/vision or local/auto", "not_supported",
                     code="images_not_supported", lif={"requested": requested, "use": "local/vision"})
    elif requested == "local/auto":
        alias, route_decision = _route_auto(text, data_class)
    if n_images and not alias_chains(W).get("local/vision"):
        return g_err(503, "no local model is deployed for local/vision; no vision model is installed yet", "capacity",
                     lif={"requested": requested, "blerbz": snap["state"]})
    st = resolve(W, alias, sample_canary=True)
    if not st.get("available"):
        reason = st["reason"]
        if reason.startswith("unknown model alias"):
            return g_err(400, reason, "invalid_request")
        return g_err(503, reason, "capacity", lif={"requested": requested, "blerbz": snap["state"]})
    prof = routing_profiles(W)[st["served_by"]]
    meta: dict[str, Any] = {"requested": requested, "alias": alias, "served_by": st["served_by"],
                            "model": f"{prof['hf_repo']}@{prof['revision'][:12]}",
                            "fallback": st["fallback"] or requested != alias, "degraded": st["degraded"]}
    if st["reason"]:
        meta["reason"] = st["reason"]
    if route_decision:
        meta["route_decision"] = route_decision
    if st.get("canary"):
        meta["canary"] = True
    answer = _answer(text, alias, n_images)
    if "[long]" in text:            # a slow answer (~12 s) so another device can watch it grow
        answer = "\n\n".join([answer] * 4)
    pieces = re.findall(r"\s*\S+", answer)
    prompt_tokens = max(1, len(text) // 4) + 12
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": len(pieces), "total_tokens": prompt_tokens + len(pieces)}
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())

    if not body.get("stream"):
        await asyncio.sleep(min(2.0, len(pieces) * TOKEN_DELAY / 4))
        return JSONResponse({"id": cid, "object": "chat.completion", "created": created, "model": requested,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                                          "finish_reason": "stop"}],
                             "usage": usage, "lif": {**meta, "blerbz": snap["state"], "latency_ms": 900 + 40 * len(pieces)}})

    def chunk(delta: dict, finish: str | None = None) -> str:
        return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                      "model": requested, "choices": [{"index": 0, "delta": delta,
                                                                       "finish_reason": finish}]}) + "\n\n"

    async def events() -> AsyncIterator[str]:
        await asyncio.sleep(0.35)                                   # time to first token
        yield chunk({"role": "assistant", "content": ""})
        for i, piece in enumerate(pieces):
            if "[fail-midstream]" in text and i == len(pieces) // 2:
                yield 'data: {"error":{"message":"upstream stream failed","type":"upstream"}}\n\n'
                return                                              # no [DONE], as the real gateway
            await asyncio.sleep(TOKEN_DELAY)
            yield chunk({"content": piece})
        yield chunk({}, "stop")
        yield "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                     "model": requested, "choices": [], "usage": usage, "lif": meta}) + "\n\n"
        yield "data: [DONE]\n\n"

    headers = {"X-LIF-Served-By": st["served_by"], "X-LIF-Fallback": str(meta["fallback"]).lower(),
               "X-LIF-Degraded": str(st["degraded"]).lower(), "X-LIF-Requested": requested,
               "Cache-Control": "no-cache"}
    if st["reason"]:
        headers["X-LIF-Reason"] = st["reason"][:200]
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


# ── batch ── (stats/control are declared before /{bid} so they aren't captured as job ids)

@app.get("/v1/batch/stats")
async def b_stats() -> dict:
    return batch_stats(W)


@app.post("/v1/batch/control")
async def b_control(request: Request) -> JSONResponse:
    if request.headers.get("x-lif-internal") != INTERNAL_KEY:
        return b_err(403, "internal key required")
    body = await _json(request)
    W.batch_paused = bool(body.get("paused"))
    W.batch_paused_reason = str(body.get("reason") or "") if W.batch_paused else ""
    _apply_batch_gate(W)
    return JSONResponse({"paused": W.batch_paused, "reason": W.batch_paused_reason})


@app.post("/v1/batch")
async def b_submit(request: Request) -> JSONResponse:
    body = await _json(request)
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return b_err(400, "items must be a non-empty list")
    idem = body.get("idempotency_key")
    if idem:
        for j in W.jobs.values():
            if j.get("_idem") == idem:
                return JSONResponse(job_view({k: v for k, v in j.items() if k != "_idem"}), 200)
    prio = body.get("priority")
    now = time.time()
    jid = "bj_" + uuid.uuid4().hex[:12]
    W.jobs[jid] = {"id": jid, "owner": request.headers.get("x-lif-client", "console"),
                   "data_class": request.headers.get("x-lif-data-class", "CONFIDENTIAL").upper(),
                   "model": body.get("model") or "local/batch", "endpoint": body.get("endpoint") or "/v1/chat/completions",
                   "priority": int(prio) if prio else 5, "priority_source": "caller" if prio else "default",
                   "deadline": body.get("deadline"), "description": body.get("description") or "",
                   "timeout_sec": body.get("timeout_sec") or 600, "max_attempts": body.get("max_attempts") or 3,
                   "state": "queued", "reason": "", "classification": {}, "model_hint": None, "created": now,
                   "updated": now, "finished": None,
                   "counts": {"pending": len(items), "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0,
                              "expired": 0}, "_idem": idem}
    _apply_batch_gate(W)
    return JSONResponse(job_view({k: v for k, v in W.jobs[jid].items() if k != "_idem"}), 201)


def _job(bid: str) -> dict | None:
    j = W.jobs.get(bid)
    return job_view({k: v for k, v in j.items() if k != "_idem"}) if j else None


@app.get("/v1/batch")
async def b_list(limit: int = 100, state: str = "") -> dict:
    rows = [_job(j["id"]) for j in sorted(W.jobs.values(), key=lambda j: -j["created"])
            if not state or j["state"] == state]
    return {"data": rows[:max(1, min(limit, 1000))]}


@app.get("/v1/batch/{bid}")
async def b_get(bid: str) -> JSONResponse:
    j = _job(bid)
    return JSONResponse(j) if j else b_err(404, "not found")


@app.get("/v1/batch/{bid}/results")
async def b_results(bid: str) -> JSONResponse:
    j = W.jobs.get(bid)
    if not j:
        return b_err(404, "not found")
    out, idx = [], 0
    for status in ("succeeded", "failed", "running", "pending", "cancelled", "expired"):
        for _ in range(j["counts"][status]):
            idx += 1
            item: dict[str, Any] = {"custom_id": f"item-{idx}", "status": status,
                                    "attempts": 3 if status == "failed" else (1 if status in ("succeeded", "running") else 0)}
            if status == "succeeded":
                item["response"] = {"object": "chat.completion", "model": j["model"],
                                    "choices": [{"index": 0, "message": {"role": "assistant",
                                                                         "content": f"(synthetic result {idx})"},
                                                 "finish_reason": "stop"}],
                                    "lif": {"requested": j["model"], "alias": j["model"], "served_by": P4B,
                                            "fallback": False, "degraded": False}}
            elif status == "failed":
                item["error"] = "HTTP 503 from gateway: all models for local/batch are unavailable"
            out.append(item)
    return JSONResponse({"id": bid, "data": out})


@app.delete("/v1/batch/{bid}")
async def b_cancel(bid: str) -> JSONResponse:
    j = W.jobs.get(bid)
    if not j:
        return b_err(404, "not found")
    if j["state"] not in ("completed", "failed", "expired", "cancelled"):
        j["counts"]["cancelled"] += j["counts"]["pending"]
        j["counts"]["pending"] = 0
        j.update(state="cancelled", reason="cancelled by owner", updated=time.time(), finished=time.time())
    return JSONResponse(_job(bid))


@app.post("/v1/batch/{bid}/pause")
async def b_pause(bid: str) -> JSONResponse:
    j = W.jobs.get(bid)
    if not j:
        return b_err(404, "not found")
    if j["state"] in ("queued", "running"):
        j.update(state="paused", reason="paused by owner", updated=time.time())
    return JSONResponse(_job(bid))


@app.post("/v1/batch/{bid}/resume")
async def b_resume(bid: str) -> JSONResponse:
    j = W.jobs.get(bid)
    if not j:
        return b_err(404, "not found")
    if j["state"] == "paused":
        c = j["counts"]
        j.update(state="running" if (c["succeeded"] or c["failed"]) else "queued", reason="", updated=time.time())
        _apply_batch_gate(W)
    return JSONResponse(_job(bid))


# ── prometheus ──

async def _prom_params(request: Request) -> dict[str, str]:
    params = dict(request.query_params)
    if request.method == "POST":
        raw = (await request.body()).decode("utf-8", "replace")
        params.update({k: v[-1] for k, v in parse_qs(raw).items()})
    return params


def _prom_time(v: str | None, default: float) -> float:
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        from datetime import datetime
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


@app.api_route("/api/v1/query", methods=["GET", "POST"])
async def prom_query(request: Request) -> JSONResponse:
    p = await _prom_params(request)
    if not p.get("query"):
        return JSONResponse({"status": "error", "errorType": "bad_data", "error": "query is required"}, 400)
    return JSONResponse({"status": "success", "data": prom_instant(p["query"], _prom_time(p.get("time"), time.time()))})


@app.api_route("/api/v1/query_range", methods=["GET", "POST"])
async def prom_query_range(request: Request) -> JSONResponse:
    p = await _prom_params(request)
    try:
        start, end = _prom_time(p.get("start"), 0), _prom_time(p.get("end"), 0)
        step = _dur(p["step"]) if re.search(r"[a-z]$", p.get("step", "")) else float(p.get("step") or 0)
        if not p.get("query") or not start or not end or step <= 0 or end < start:
            raise ValueError("query, start, end and a positive step are required")
        data = prom_range(p["query"], start, end, step)
    except (ValueError, KeyError) as e:
        return JSONResponse({"status": "error", "errorType": "bad_data", "error": str(e)}, 400)
    return JSONResponse({"status": "success", "data": data})
