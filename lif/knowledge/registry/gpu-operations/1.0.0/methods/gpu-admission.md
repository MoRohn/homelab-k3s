---
type: method
status: active
version: 1
title: GPU admission for secondary workloads
steps:
  - Ask the host GPU scheduler for a lease; never allocate GPU memory outside it
  - Check the primary-workload state; do not start large jobs when it is HIGH or IMMINENT
  - Leave the scheduler's memory headroom intact (unified memory is shared with the CPU)
rationale: >
  The primary workload owns the GPU. Unified memory means CPU-side allocations also consume GPU
  headroom, so admission has to consider host memory, not only the GPU.
---
# GPU admission

Applies to every secondary workload (LIF tiers, batch, benchmarks).
