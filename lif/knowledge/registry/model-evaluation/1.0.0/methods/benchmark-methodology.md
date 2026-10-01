---
type: method
status: active
version: 1
title: Local benchmark methodology
steps:
  - Pin the model revision and runtime version
  - Run the same evaluation suite as the incumbent, on this host
  - Record quality, structured-output validity, TTFT p50, decode tokens/s p50 and peak memory
  - Record n and the date with every number
rationale: Numbers from other hardware do not transfer to a shared, bandwidth-bound unified-memory host.
---
