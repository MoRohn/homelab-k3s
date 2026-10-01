---
type: service
title: batch
status: active
runs_on: "[[dgx-spark]]"
code: lif/lif/batch/app.py
depends_on: ["[[gateway]]"]
---
Batch engine — priority queue with checkpointing; pauses background work while the primary workload runs.
