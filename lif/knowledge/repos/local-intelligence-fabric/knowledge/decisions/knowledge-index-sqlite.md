---
type: decision
title: The knowledge graph index is SQLite (recursive CTEs + FTS5), not a graph database
question: Which graph implementation should index the knowledge?
status: accepted
selected: SQLite with WAL, recursive CTEs for traversal, FTS5/BM25 for text, optional embeddings cache
alternatives:
  - PostgreSQL with recursive queries (a server to run, back up and upgrade for no query we need)
  - Apache AGE (PostgreSQL extension; adds the server and an extension upgrade path)
  - Neo4j (JVM memory footprint on a node whose memory belongs to the primary workload; licensing)
  - Kuzu (embedded and fast, but a new native dependency on arm64 for traversals CTEs already do)
  - DuckDB extensions (analytics-oriented; no FTS5-grade incremental text index)
  - custom in-memory graph (fast, but no persistence or FTS)
date: 2026-10-01
evidence: ["[[src-sqlite-helper]]", "[[src-architecture-doc]]"]
assumptions: ["[[knowledge-fits-in-text]]", "[[single-writer-per-database]]", "[[primary-workload-owns-gpu]]"]
review_when: ["[[knowledge-scale-question]]"]
rationale: >
  Single-node simplicity, zero new services, the same SQLite pattern every LIF service already
  uses, trivially rebuildable, backups are file copies, arm64 stdlib support. Traversals at
  homelab scale (≤ 10⁵ edges) are milliseconds with recursive CTEs. The graph is a data model, not a reason to run a graph database.
---
