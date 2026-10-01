---
type: source
title: GPU scheduling and resource protection
kind: documentation
location: ../../../../../docs/GPU_SCHEDULING.md
quality: primary
captured: 2026-10-01
---
Quoted passages from `lif/docs/GPU_SCHEDULING.md`.

gpusched is the only GPU admission authority on this host. LIF is a read-only client. ^p1

Kubernetes PriorityClasses order CPU and RAM inside K3s only. The kubelet can't see unified GPU memory. ^p2

Today gpusched admits about 0.6–1.2 GiB, so no LLM fits on the GPU. ^p3

By default, llama.cpp repacks quantized weights into anonymous memory. The 4B model held 3.1 GB anonymous. Three models dropped MemAvailable from ~10 to 3.9 GiB, PSI reached 4.2, swap grew, and gpusched admissible went to 0. ^p4

With --no-repack the weights stay file-backed (reclaimable). 4B anon dropped to ~670 MiB, at a cost of only −5 % decode (22.3 → 21.1 tok/s). Measured 2026-09-30. ^p5

GB10's CPU and GPU share one memory bus. With the CPU tiers saturated (8 concurrent generations), production LLM decode went from 2.98 / 3.09 tok/s idle to 2.74 tok/s, about −10 % (n = 1 per arm, indicative only). ^p6
