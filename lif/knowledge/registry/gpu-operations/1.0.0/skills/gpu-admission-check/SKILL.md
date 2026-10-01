---
type: skill
name: gpu-admission-check
version: 1
title: Check GPU admission before a heavy job
description: Verify a job may start without threatening the primary workload.
when: Before benchmarks, model loads or batch jobs that need GPU or large unified memory.
inputs: [job]
method: "[[gpu-admission]]"
status: active
---
Read the primary-workload state and admissible memory (`local-ai gpu`), then run this skill's checks.
