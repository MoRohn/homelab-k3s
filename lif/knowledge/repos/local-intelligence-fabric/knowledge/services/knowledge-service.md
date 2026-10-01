---
type: service
title: knowledge-service
status: planned
runs_on: "[[dgx-spark]]"
code: lif/lif/knowledge/app.py
depends_on: ["[[controller]]"]
---
Knowledge API (/v1/knowledge), incremental compile, event sink.
