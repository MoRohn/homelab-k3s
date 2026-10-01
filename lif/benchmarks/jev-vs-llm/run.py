"""Jev vs LLM-first on a real public workload (spec §50, acceptance Q).

Workload: AG News topic classification (fancyzhx/ag_news, test split, first 200 rows) —
the same shape as BNN story triage. Public data, so the privacy policy allows Jev.

  A  LLM-first:   every story → local/fast (Qwen3-4B, CPU) via the LIF gateway
  B  Fabric:      every story → Jev `story-topic` decision (parallel); only stories whose
                  policy gate is not AUTO/VALIDATE escalate to the same local LLM

Both arms use the same prompt, gateway, model, and LLM concurrency (2).
Run:  python benchmarks/jev-vs-llm/run.py  (env: LIF_GATEWAY_URL, LIF_API_KEY, TYPE_SAFE_JEV_API_KEY)
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lif.decision.fabric import DecisionFabric          # noqa: E402
from lif.decision.providers import JevProvider          # noqa: E402
from lif.decision.rules import rules                    # noqa: E402
from lif.decision.types import load_definitions         # noqa: E402
from lif.policy import engine as policy                 # noqa: E402

HERE = Path(__file__).parent
LABELS = ["world", "sports", "business", "scitech"]
PROMPT = ("Classify the news story into exactly one topic: world, sports, business, scitech.\n"
          "Answer with the single label only.\n\nStory: {text}")
GW = os.environ.get("LIF_GATEWAY_URL", "http://127.0.0.1:18080")
KEY = os.environ["LIF_API_KEY"]
LLM_CONCURRENCY = 2


async def llm_label(client: httpx.AsyncClient, sem: asyncio.Semaphore, text: str) -> dict:
    async with sem:
        t0 = time.perf_counter()
        r = await client.post(f"{GW}/v1/chat/completions", headers={
            "Authorization": f"Bearer {KEY}", "X-LIF-Data-Class": "PUBLIC", "X-LIF-Workload": "experiment:jev-vs-llm"},
            json={"model": "local/fast", "temperature": 0.0, "max_tokens": 4,
                  "messages": [{"role": "user", "content": PROMPT.format(text=text)}]})
        dt = time.perf_counter() - t0
    r.raise_for_status()
    d = r.json()
    raw = d["choices"][0]["message"]["content"].strip().lower()
    label = next((l for l in LABELS if raw.startswith(l) or l in raw), raw[:12])
    if label == "sci" or raw.startswith("sci"):
        label = "scitech"
    u = d.get("usage") or {}
    return {"label": label, "sec": dt, "prompt_tokens": u.get("prompt_tokens", 0),
            "completion_tokens": u.get("completion_tokens", 0), "served_by": d.get("lif", {}).get("served_by")}


def summarize(name: str, items: list[dict], wall: float, extra: dict) -> dict:
    lat = [i["sec"] for i in items]
    acc = sum(i["pred"] == i["label"] for i in items) / len(items)
    return {"arm": name, "n": len(items), "accuracy": round(acc, 4), "wall_sec": round(wall, 1),
            "throughput_items_per_sec": round(len(items) / wall, 2),
            "latency_p50_ms": round(statistics.median(lat) * 1000), "latency_p95_ms":
                round(sorted(lat)[int(0.95 * (len(lat) - 1))] * 1000),
            "llm_calls": sum(i.get("llm", 0) for i in items),
            "llm_prompt_tokens": sum(i.get("prompt_tokens", 0) for i in items),
            "llm_generated_tokens": sum(i.get("completion_tokens", 0) for i in items),
            "external_llm_calls": 0, **extra}


async def main() -> None:
    data = json.loads((HERE / "ag_news_test_200.json").read_text())
    async with httpx.AsyncClient(timeout=120) as client:
        # ── arm A: LLM-first ──
        sem = asyncio.Semaphore(LLM_CONCURRENCY)
        t0 = time.perf_counter()
        outs = await asyncio.gather(*(llm_label(client, sem, d["text"]) for d in data))
        wall_a = time.perf_counter() - t0
        a = [{**d, "pred": o["label"], "sec": o["sec"], "llm": 1, "prompt_tokens": o["prompt_tokens"],
              "completion_tokens": o["completion_tokens"]} for d, o in zip(data, outs)]
        res_a = summarize("A: LLM-first", a, wall_a, {"jev_calls": 0, "jev_cost_usd": 0.0})

        # ── arm B: Jev decision fabric + selective local LLM ──
        jev = JevProvider(os.environ["TYPE_SAFE_JEV_API_KEY"])
        fab = DecisionFabric(load_definitions(), rules, jev=jev, cache=None)
        t0 = time.perf_counter()
        decs = await fab.evaluate_many("story-topic", [{"story": d["text"]} for d in data], data_class="PUBLIC",
                                       concurrency=16)
        t_dec = time.perf_counter() - t0
        hard = [i for i, r in enumerate(decs) if r.action not in (policy.Gate.AUTO, policy.Gate.VALIDATE)
                or r.provider != "jev"]
        esc = await asyncio.gather(*(llm_label(client, sem, data[i]["text"]) for i in hard))
        wall_b = time.perf_counter() - t0
        esc_by = dict(zip(hard, esc))
        b = []
        for i, (d, r) in enumerate(zip(data, decs)):
            if i in esc_by:
                o = esc_by[i]
                b.append({**d, "pred": o["label"], "sec": r.latency_ms / 1000 + o["sec"], "llm": 1,
                          "prompt_tokens": o["prompt_tokens"], "completion_tokens": o["completion_tokens"],
                          "jev": r.decision, "jev_conf": r.confidence})
            else:
                b.append({**d, "pred": r.decision, "sec": r.latency_ms / 1000, "llm": 0,
                          "jev": r.decision, "jev_conf": r.confidence})
        jev_only = [x for x in b if not x["llm"]]
        res_b = summarize("B: code+Jev+selective LLM", b, wall_b, {
            "jev_calls": len(decs), "jev_resolved": len(jev_only), "escalated_to_llm": len(hard),
            "jev_resolved_accuracy": round(sum(x["pred"] == x["label"] for x in jev_only) / max(1, len(jev_only)), 4),
            "escalated_accuracy": round(sum(x["pred"] == x["label"] for x in b if x["llm"]) / max(1, len(hard)), 4),
            "jev_only_accuracy_all_items": round(sum(x["jev"] == x["label"] for x in b) / len(b), 4),
            "decision_phase_sec": round(t_dec, 2),
            "jev_input_tokens": sum(r.input_tokens for r in decs),
            "jev_cost_usd": round(sum(r.cost_usd for r in decs), 6),
            "providers": {p: sum(1 for r in decs if r.provider == p) for p in {r.provider for r in decs}}})
    out = {"workload": "ag_news test[0:200] topic classification", "ts": time.time(),
           "model": "local/fast → qwen3-4b-instruct-2507-q4km-cpu", "llm_concurrency": LLM_CONCURRENCY,
           "results": [res_a, res_b],
           "llm_avoidance_rate": round(1 - res_b["llm_calls"] / len(data), 4),
           "generated_tokens_avoided": res_a["llm_generated_tokens"] - res_b["llm_generated_tokens"],
           "prompt_tokens_avoided": res_a["llm_prompt_tokens"] - res_b["llm_prompt_tokens"]}
    (HERE / f"results-{time.strftime('%Y%m%d-%H%M')}.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
