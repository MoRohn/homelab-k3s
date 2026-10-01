---
type: method
status: active
version: 1
title: Model promotion requirements
steps:
  - Hardware validation (fits the memory budget with headroom)
  - Local benchmark against the incumbent
  - Regression check against promotion thresholds
  - Canary on a share of traffic
  - Rollback readiness (previous chain kept)
rationale: Manual-first promotion — each step catches a different failure, and rollback must never depend on the new model.
---
