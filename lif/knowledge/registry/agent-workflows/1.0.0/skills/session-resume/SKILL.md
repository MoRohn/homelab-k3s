---
type: skill
name: session-resume
version: 1
title: Resume a project in a fresh session
description: Rebuild working context from the graph and the last checkpoint — not from chat history.
when: At the start of every agent session on a project.
inputs: [project]
status: active
---
Call `resume_session` (or `knowledge session resume PROJECT`). Read pending reviews and changed
assumptions first: they mean prior conclusions may no longer hold. At checkpoint time, set
`recovered: true|false` — whether the resume carried the context you needed (context recovery rate).
