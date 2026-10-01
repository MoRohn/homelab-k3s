---
type: review
title: Review of local-intelligence-fabric::knowledge-index-sqlite
reviews:
- '[[local-intelligence-fabric::knowledge-index-sqlite]]'
outcome: reaffirmed
reviewer: agent:claude-code
date: '2026-10-01'
trigger: '[[local-intelligence-fabric::single-writer-per-database]]'
created: '2026-10-01'
updated: '2026-10-01'
updated_by: agent:claude-code
---

The contradicting evidence (several writers on the host index) is real, but measured safe: lif/tests/test_db_concurrency.py runs 6 processes x 150 concurrent BEGIN IMMEDIATE transactions through lif.common.db.DB with 900/900 rows, 0 lock failures and an exact counter. The index is also derived (rebuildable from the canonical repos). The assumption's wording was corrected to match; SQLite stays.
