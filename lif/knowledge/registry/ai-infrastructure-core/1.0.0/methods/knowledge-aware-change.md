---
type: method
status: active
version: 1
title: Knowledge-aware production change
steps:
  - Retrieve related decisions (knowledge context "<change>" or MCP assemble_context)
  - Retrieve known incidents and lessons touching the same services or models
  - Retrieve the methods that govern this kind of change
  - Inspect the assumptions the affected decisions rest on — are any challenged or invalidated?
  - Plan, and state which decisions the plan follows or departs from
  - Execute (pushing to main is the owner's call in labzilla)
  - Validate against the method's checks
  - Record the result — change object, any decision, any lesson — and checkpoint the session
rationale: >
  Most repeated infrastructure mistakes are a lost "why". Pulling the decisions, incidents and
  assumptions before acting is cheap; rediscovering them after an outage is not.
---
# Knowledge-aware production change

The method behind the `review-change` skill. It turns "change request → done" into
"change request → context → plan → execute → validate → record", so the next agent inherits the why.
