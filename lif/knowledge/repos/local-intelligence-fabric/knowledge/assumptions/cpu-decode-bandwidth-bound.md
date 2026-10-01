---
type: assumption
title: CPU inference competes with the primary workload for memory bandwidth
statement: CPU and GPU share one LPDDR5X bus, so saturated CPU inference measurably slows production LLM decode.
status: active
confidence: medium
tags: [constraint, gpu]
grounds:
  - id: g1
    source: "[[src-gpu-scheduling-doc]]"
    passage: "[[src-gpu-scheduling-doc^p6]]"
    stance: supports
    basis: measured
  - id: g2
    source: "[[src-architecture-doc]]"
    passage: "[[src-architecture-doc^p4]]"
    stance: supports
    basis: reported
---
Confidence is medium: the interference measurement is n = 1 per arm.
