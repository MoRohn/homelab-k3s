---
type: lesson
title: llama.cpp must run with --no-repack on unified memory
observation: >
  By default llama.cpp repacked quantized weights into anonymous memory (3.1 GB for the 4B model).
  Three models dropped MemAvailable from ~10 to 3.9 GiB, swap grew and gpusched admissible went to 0.
lesson: >
  On a unified-memory host shared with the primary workload, model weights must stay file-backed
  (reclaimable). Run llama.cpp with --no-repack; it costs about 5% decode speed.
evidence: ["[[src-gpu-scheduling-doc^p4]]", "[[src-gpu-scheduling-doc^p5]]"]
changed: ["[[cpu-tiers-for-local-inference]]"]
---
Measured 2026-09-30 (see the GPU scheduling doc).
