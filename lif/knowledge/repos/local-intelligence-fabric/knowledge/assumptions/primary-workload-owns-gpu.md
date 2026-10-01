---
type: assumption
title: The primary workload owns the GPU and most unified memory
statement: The primary workload's resident models pin about 107 GB of the 128 GB unified memory and have absolute priority on the GPU.
status: active
confidence: high
tags: [constraint, gpu]
grounds:
  - id: g1
    source: "[[src-architecture-doc]]"
    passage: "[[src-architecture-doc^p2]]"
    stance: supports
    basis: observed
check: The primary workload's residents change size or move off this host.
---
