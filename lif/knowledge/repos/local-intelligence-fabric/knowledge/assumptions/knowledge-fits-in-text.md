---
type: assumption
title: Project knowledge is small enough for text + an embedded index
statement: The knowledge of this homelab is thousands to tens of thousands of objects — small enough to keep canonical in Git as typed Markdown and to recompile into an embedded SQLite index in seconds.
status: active
confidence: medium
check: Compile time exceeds 10 s or the object count passes 200,000.
used_by: ["[[knowledge-index-sqlite]]", "[[knowledge-canonical-typed-markdown]]"]
---
