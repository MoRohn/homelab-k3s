---
type: source
title: LIF architecture document
kind: documentation
location: ../../../../../docs/ARCHITECTURE.md
quality: primary
captured: 2026-10-01
---
Quoted passages from `lif/docs/ARCHITECTURE.md`.

One GB10 GPU with unified memory. NVML reports memory as N/A, and CUDA allocations are not charged to cgroups. ^p1

The primary workload's residents in Docker pin about 107 GB. Host MemAvailable is about 8–10 GiB. ^p2

gpusched keeps 8 GiB of headroom, so admissible GPU memory is about 0.6–1.2 GiB. gpusched (a host systemd user unit) is the single GPU admission authority. LIF only reads it. ^p3

CPU and GPU share one LPDDR5X bus. Production LLM decode (~3 tok/s on 67 GB of weights) is bandwidth-bound. ^p4

As a result, every LIF model runs on the CPU today: llama.cpp on the 10 Cortex-A725 cores, with weights mmap'd. ^p5

If the controller fails, the gateway keeps its last-known-good routing table; discovery, lifecycle and probes pause. The router falls back along the alias chain, visibly. ^p6
