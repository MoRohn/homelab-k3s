---
type: method
status: active
version: 1
title: Every workload declares requests and limits
steps:
  - Set CPU and memory requests and limits on every container
  - Pin image tags and chart versions
rationale: On a single node with memory shared with the GPU, one unbounded pod can starve everything.
---
