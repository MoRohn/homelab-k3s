---
type: assumption
title: Admissible GPU memory is too small for any LLM
statement: gpusched admits only about 0.6–1.2 GiB of GPU memory to secondary workloads, so no LLM fits on the GPU.
status: active
confidence: high
tags: [constraint, gpu]
grounds:
  - id: g1
    source: "[[src-gpu-scheduling-doc]]"
    passage: "[[src-gpu-scheduling-doc^p3]]"
    stance: supports
    basis: measured
  - id: g2
    source: "[[src-architecture-doc]]"
    passage: "[[src-architecture-doc^p3]]"
    stance: supports
    basis: reported
review_after: 2027-01-01
check: gpusched admissible memory stays above ~6 GiB for a week (enough for a 4B model on the GPU).
---
Both sources ultimately report the same gpusched measurement, so they are not independent.
