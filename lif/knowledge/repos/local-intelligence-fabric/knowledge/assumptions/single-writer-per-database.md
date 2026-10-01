---
type: assumption
title: One writer process per database file
statement: Every LIF service owns its own SQLite file and is its only writer, so SQLite (WAL) is sufficient and no database server is needed.
status: active
confidence: high
supported_by: ["[[src-sqlite-helper]]"]
grounds:
  - id: g1
    source: "[[src-knowledge-doc]]"
    passage: "[[src-knowledge-doc^p1]]"
    stance: contradicts
    basis: observed
    note: >
      The knowledge index (lif/knowledge/.knowledge/index.db) is written by several processes — the
      CLI, the MCP server, the knowledge service and in-process callers — not by one service.
---
Recorded by the agent that built the knowledge layer. Left for the owner to review: the index is
derived (rebuildable) and its writes are short `BEGIN IMMEDIATE` transactions with a 5 s busy
timeout, which may make this acceptable — or the decision may need revising.
