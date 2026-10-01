---
type: decision
title: Target 95% useful local AI availability, measured by real inference probes
question: What availability should the platform promise, and how is it measured?
status: accepted
selected: 0.95 "useful local AI" (fast or default model answers), probed every 30 s with real inference
alternatives: [process liveness only, 99.9% target]
date: 2026-09-20
evidence: ["[[src-platform-config]]"]
assumptions: ["[[single-node-primary]]", "[[primary-workload-owns-gpu]]"]
rationale: >
  A single node shared with a higher-priority workload cannot honestly promise more; probes that
  run inference measure what callers experience, not whether a process is up.
---
Grounding: [[src-platform-config^p1]].
