---
type: decision
title: Canonical knowledge is typed Markdown in Git; every index is derived
question: Where does the authoritative copy of project knowledge live?
status: accepted
selected: typed Markdown + YAML type definitions in agent repos (Git); SQLite index rebuilt from them
alternatives: [database as source of truth with exports, wiki, chat transcripts]
date: 2026-10-01
evidence: ["[[src-sqlite-helper]]"]
assumptions: ["[[knowledge-fits-in-text]]"]
rationale: >
  Text in Git is human-readable, reviewable, diffable and recoverable from any clone; an index
  that can always be rebuilt (`knowledge rebuild`) can never be the only copy of anything.
  High-volume telemetry stays out of Git (metrics, activity tables); only significant events become objects.
---
