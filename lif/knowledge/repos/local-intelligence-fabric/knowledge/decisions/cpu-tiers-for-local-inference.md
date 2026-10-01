---
type: decision
title: Serve LIF models from the CPU (llama.cpp on the A725 cores) for now
question: Where should LIF's local models run while the GPU is owned by the primary workload?
status: accepted
selected: llama.cpp on CPU cores 0–4 and 10–14, weights mmap'd with --no-repack
alternatives: [GPU tiers through gpusched leases, vLLM on GPU, no local inference]
date: 2026-09-30
evidence: ["[[src-architecture-doc]]", "[[src-gpu-scheduling-doc]]"]
assumptions: ["[[gpu-admission-too-small-for-llms]]", "[[cpu-decode-bandwidth-bound]]"]
review_when: ["[[gpu-tiers-question]]"]
affects: ["[[fast-fallback-model]]"]
rationale: >
  No LLM fits in the 0.6–1.2 GiB gpusched admits. CPU tiers create no CUDA context, so a GPU
  takeover cannot evict them; the cost is bandwidth contention, bounded by the yield policy.
---
