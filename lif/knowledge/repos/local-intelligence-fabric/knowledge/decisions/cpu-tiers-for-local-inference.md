---
type: decision
title: Serve LIF models from the CPU (llama.cpp on the A725 cores) for now
question: Where should LIF's local models run while the GPU is owned by the primary workload?
status: accepted
selected: 'llama.cpp on the CPU: writing a single answer on the A725 cores 0-4 and 10-14; prompt processing
  (and batched decode) on the X925 cores 5-9 and 15-19 (--cpu-mask-batch); chat servers repack weights,
  embedding keeps --no-repack. Amended 2026-10-01 by the owner.'
alternatives:
- GPU tiers through gpusched leases
- vLLM on GPU
- no local inference
date: 2026-09-30
evidence:
- '[[src-architecture-doc]]'
- '[[src-gpu-scheduling-doc]]'
assumptions:
- '[[gpu-admission-too-small-for-llms]]'
- '[[cpu-decode-bandwidth-bound]]'
review_when:
- '[[gpu-tiers-question]]'
affects:
- '[[fast-fallback-model]]'
rationale: No LLM fits in the 0.6–1.2 GiB gpusched admits. CPU tiers create no CUDA context, so a GPU
  takeover cannot evict them; the cost is bandwidth contention, bounded by the yield policy.
updated: '2026-10-01'
updated_by: agent:claude-code
---

## Amendment (2026-10-01, owner decision)

The CPU-affinity benchmark (`lif/benchmarks/cpu-affinity/run.py`, Qwen3-4B Q4_K_M, n=3 per arm, production idle; raw results private) measured prompt processing (pp512) and writing (tg128):

| Setup | pp512 tok/s | tg128 tok/s |
|---|---|---|
| A725 ×10, --no-repack (before) | 51.8 | 26.9 |
| A725 ×10, repack | 83.4 | 29.3 |
| X925 ×10 | 139.4 | 36.9 |
| X925 ×10, repack | 218.6 | 39.4 |

The owner chose the split: prompt bursts on X925 (where the primary workload was ~4 % busy when sampled), single-answer decode stays on A725, chat servers repack. Bandwidth contention is still bounded by the yield policy; the cgroup limits hold the repacked weights.
