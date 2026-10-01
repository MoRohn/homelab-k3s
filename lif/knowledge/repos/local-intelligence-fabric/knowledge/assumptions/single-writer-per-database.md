---
type: assumption
title: SQLite writers are serialised (WAL + BEGIN IMMEDIATE + busy timeout)
statement: 'Each LIF service owns its SQLite file; where several processes do write one file (on the host,
  every Claude Code session runs its own knowledge MCP server against lif/knowledge/.knowledge/index.db),
  lif.common.db serialises them with WAL, short BEGIN IMMEDIATE transactions and a 5 s busy timeout, so
  SQLite is sufficient and no database server is needed. The in-cluster knowledge service is the only
  writer of its own index (replicas: 1).'
status: active
confidence: high
supported_by:
- '[[src-sqlite-helper]]'
grounds:
- id: g1
  basis: observed
  stance: supports
  source: '[[src-knowledge-doc]]'
  passage: '[[src-knowledge-doc^p1]]'
  note: The knowledge index (lif/knowledge/.knowledge/index.db) is written by several processes — the
    CLI, the MCP server, the knowledge service and in-process callers — not by one service. (Contradicted
    the original 'only writer' wording; the corrected statement covers it.)
- id: g2
  basis: measured
  stance: supports
  source: '[[src-sqlite-helper]]'
  note: '2026-10-01, lif/tests/test_db_concurrency.py: 6 processes x 150 concurrent BEGIN IMMEDIATE transactions
    on one file through lif.common.db.DB: 900/900 rows, 0 ''database is locked'' failures, read-modify-write
    counter exact (n=1 run; the test runs in CI on every change).'
updated: '2026-10-01'
updated_by: agent:claude-code
---

Recorded by the agent that built the knowledge layer. Left for the owner to review: the index is
derived (rebuildable) and its writes are short `BEGIN IMMEDIATE` transactions with a 5 s busy
timeout, which may make this acceptable — or the decision may need revising.

Resolved 2026-10-01 by measurement: g1 is true (the host index has several writer processes), and g2 shows they are serialised safely. The statement now says what actually holds; the decision `knowledge-index-sqlite` stands.
