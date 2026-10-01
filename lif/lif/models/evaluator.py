"""Evaluation harness: quality + performance against any OpenAI-compatible endpoint.

Quality   pass rate over an eval suite (evals/*.yaml), per category, JSON validity.
Latency   TTFT (streaming), decode tok/s (server timings), end-to-end p50/p95.
Load      throughput at `concurrency` parallel requests; errors and timeouts.

Every number in a summary is measured on this host; nothing is copied from model cards.
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from lif.common import config, log

LOG = log.get("lif.eval")


def load_suite(name: str = "core") -> dict:
    for d in (Path("/etc/lif/evals"), config.REPO_CONFIG.parent / "evals"):
        p = d / f"{name}.yaml"
        if p.exists():
            return yaml.safe_load(p.read_text())
    raise FileNotFoundError(f"eval suite {name}")


def _strip(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text or "").strip()
    m = re.search(r"```(?:json|python)?\s*([\s\S]*?)```", text)
    return m.group(1).strip() if m and not text.startswith("def ") else text


def check(item: dict, output: str) -> tuple[bool, dict]:
    """Deterministic checkers. Returns (passed, details)."""
    out = _strip(output)
    c = item["checker"]
    if c == "contains":
        missing = [e for e in item["expect"] if e.lower() not in out.lower()]
        return not missing, {"missing": missing}
    if c == "regex":
        return bool(re.search(item["expect"], out.strip())), {}
    if c == "choice":
        word = re.sub(r"[^a-z_ -]", "", out.lower()).strip().split()
        got = word[0] if word else ""
        return got == item["expect"], {"got": got}
    if c == "number":
        nums = re.findall(r"-?\d+(?:\.\d+)?", out)
        return bool(nums) and abs(float(nums[-1]) - float(item["expect"])) <= float(item.get("tol", 0)), \
            {"got": nums[-1] if nums else None}
    if c == "json":
        try:
            obj = json.loads(out[out.find("{"): out.rfind("}") + 1])
        except Exception:
            return False, {"json_valid": False}
        ok = all(k in obj for k in item.get("required", []))
        for k, t in (item.get("types") or {}).items():
            ok &= isinstance(obj.get(k), {"int": int, "list": list, "str": str}[t])
        for k, allowed in (item.get("enums") or {}).items():
            ok &= obj.get(k) in allowed
        return bool(ok), {"json_valid": True}
    raise ValueError(f"unknown checker {c}")


async def _one(client: httpx.AsyncClient, url: str, model: str, prompt: str, max_tokens: int,
               extra: dict) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
            "temperature": 0, "stream": True, "stream_options": {"include_usage": True}, **extra}
    t0 = time.perf_counter()
    ttft, text, timings, usage = None, [], {}, {}
    async with client.stream("POST", f"{url}/v1/chat/completions", json=body) as r:
        if r.status_code != 200:
            raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
        async for line in r.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            ch = json.loads(line[6:])
            for c in ch.get("choices") or []:
                piece = (c.get("delta") or {}).get("content")
                if piece:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    text.append(piece)
            timings = ch.get("timings") or timings
            usage = ch.get("usage") or usage
    return {"text": "".join(text), "ttft": ttft, "total": time.perf_counter() - t0,
            "decode_tps": timings.get("predicted_per_second"), "usage": usage}


async def run_suite(url: str, *, model: str = "eval", suite: str = "core", max_tokens: int = 256,
                    concurrency: int = 4, extra: dict | None = None, headers: dict | None = None,
                    timeout: float = 180, should_stop=lambda: False) -> tuple[dict, dict]:
    """Returns (per-item results, summary). `should_stop` lets the caller abort when BLERBZ needs capacity."""
    s = load_suite(suite)
    items = s["items"]
    extra = extra or {}
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=timeout, headers=headers or {}) as client:
        # 1. quality pass, sequential (clean latency numbers)
        for it in items:
            if should_stop():
                return results, {"aborted": True, "reason": "stopped (capacity reclaimed)"}
            try:
                o = await _one(client, url, model, it["prompt"], max_tokens, extra)
                ok, det = check(it, o["text"])
                results[it["id"]] = {"cat": it["cat"], "pass": ok, **det, "ttft": o["ttft"], "total": o["total"],
                                     "decode_tps": o["decode_tps"], "output": o["text"][:400]}
            except Exception as exc:
                results[it["id"]] = {"cat": it["cat"], "pass": False, "error": str(exc)[:200]}
        # 2. load pass: `concurrency` parallel copies of the summarization prompt
        load_prompt = next((i["prompt"] for i in items if i["cat"] == "summarization"), items[0]["prompt"])
        t0 = time.perf_counter()
        outs = await asyncio.gather(*(_one(client, url, model, load_prompt, 128, extra) for _ in range(concurrency)),
                                    return_exceptions=True)
        wall = time.perf_counter() - t0
    ok_outs = [o for o in outs if not isinstance(o, Exception)]
    gen_tokens = sum((o["usage"] or {}).get("completion_tokens") or 0 for o in ok_outs)
    return results, summarize(results, concurrency, gen_tokens, wall, len(outs) - len(ok_outs))


def _pct(xs: list[float], p: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 3)


def summarize(results: dict, concurrency: int, gen_tokens: int, wall: float, load_errors: int) -> dict:
    vals = list(results.values())
    by_cat: dict[str, list[bool]] = {}
    for v in vals:
        by_cat.setdefault(v["cat"], []).append(v["pass"])
    jsons = [v for v in vals if v["cat"] == "structured"]
    errors = sum(1 for v in vals if "error" in v) + load_errors
    return {
        "quality": round(sum(v["pass"] for v in vals) / max(1, len(vals)), 4),
        "by_category": {k: round(sum(x) / len(x), 3) for k, x in by_cat.items()},
        "json_valid": round(sum(1 for v in jsons if v.get("json_valid")) / max(1, len(jsons)), 3),
        "structured_ok": round(sum(v["pass"] for v in jsons) / max(1, len(jsons)), 3),
        "ttft_ms_p50": _ms(_pct([v.get("ttft") for v in vals], 0.5)),
        "ttft_ms_p95": _ms(_pct([v.get("ttft") for v in vals], 0.95)),
        "latency_ms_p50": _ms(_pct([v.get("total") for v in vals], 0.5)),
        "latency_ms_p95": _ms(_pct([v.get("total") for v in vals], 0.95)),
        "decode_tps_p50": _pct([v.get("decode_tps") for v in vals], 0.5),
        "concurrency": concurrency,
        "throughput_tps": round(gen_tokens / wall, 2) if wall else None,
        "errors": errors,
        "items": len(vals),
    }


def _ms(x: float | None) -> float | None:
    return round(x * 1000, 1) if x is not None else None


# ── comparison & promotion recommendation ────────────────────────────────────

def compare(candidate: dict, current: dict | None, cand_mem_mib: float, cur_mem_mib: float | None) -> dict:
    """Evidence-driven comparison against config models.promotion thresholds."""
    th = config.get("models.promotion") or {}
    rep: dict[str, Any] = {"thresholds": th, "checks": {}}
    if not current:
        rep["checks"]["no_incumbent"] = True
        ok = candidate["errors"] <= int(th.get("crash_rate_max", 0)) and \
            candidate["structured_ok"] >= float(th.get("structured_output_min", 0.98)) * 0.9
        rep["recommendation"] = "CANARY" if ok else "REJECT"
        return rep

    def pct(a, b):
        return None if a is None or not b else round((a - b) / b * 100, 1)
    rep["delta"] = {
        "quality": round(candidate["quality"] - current["quality"], 4),
        "ttft_pct": pct(candidate["ttft_ms_p50"], current["ttft_ms_p50"]),
        "decode_tps_pct": pct(candidate["decode_tps_p50"], current["decode_tps_p50"]),
        "throughput_pct": pct(candidate["throughput_tps"], current["throughput_tps"]),
        "memory_pct": pct(cand_mem_mib, cur_mem_mib),
        "structured_ok": candidate["structured_ok"],
    }
    d, c = rep["delta"], rep["checks"]
    c["quality"] = d["quality"] >= -float(th.get("max_quality_regression", 0.01))
    c["latency"] = d["ttft_pct"] is None or d["ttft_pct"] <= float(th.get("max_latency_regression_pct", 10))
    c["memory"] = d["memory_pct"] is None or d["memory_pct"] <= float(th.get("max_memory_increase_pct", 20))
    c["structured"] = candidate["structured_ok"] >= min(float(th.get("structured_output_min", 0.98)),
                                                        current["structured_ok"])
    c["stability"] = candidate["errors"] <= int(th.get("crash_rate_max", 0))
    better = d["quality"] > 0 or (d["quality"] >= 0 and (d["decode_tps_pct"] or 0) > 10)
    if all(c.values()) and better:
        rep["recommendation"] = "CANARY"
    elif all(c.values()):
        rep["recommendation"] = "HOLD"          # safe but not better: keep as STANDBY option
    else:
        rep["recommendation"] = "REJECT"
    rep["failed_checks"] = [k for k, v in c.items() if not v]
    return rep
