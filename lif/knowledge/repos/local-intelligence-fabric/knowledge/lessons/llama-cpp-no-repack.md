---
type: lesson
title: llama.cpp must run with --no-repack on unified memory
observation: 'By default llama.cpp repacked quantized weights into anonymous memory (3.1 GB for the 4B
  model). Three models dropped MemAvailable from ~10 to 3.9 GiB, swap grew and gpusched admissible went
  to 0.

  '
lesson: 'On a unified-memory host shared with the primary workload, model weights must stay file-backed
  (reclaimable). Run llama.cpp with --no-repack; it costs about 5% decode speed.

  '
evidence:
- '[[src-gpu-scheduling-doc^p4]]'
- '[[src-gpu-scheduling-doc^p5]]'
changed:
- '[[cpu-tiers-for-local-inference]]'
updated: '2026-10-01'
updated_by: agent:claude-code
---

Measured 2026-09-30 (see the GPU scheduling doc).

**Superseded for chat servers (2026-10-01).** Repacking costs anonymous memory but gave 1.6× prompt speed on the A725 cores (4.2× with prompts on X925; see `cpu-tiers-for-local-inference`). It was harmful when three models repacked on a host near its memory limit with undersized cgroups; the cgroup limits now hold the repacked weights (`hardware_fit.memory_limit_mib`) and the memory guard sheds tier0-small first. Embedding servers keep `--no-repack`.
