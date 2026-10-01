---
type: method
status: active
version: 1
title: Incident learning loop
steps:
  - Record the incident with a timeline and affected objects
  - Link the evidence (logs, metrics snapshots, run records) with traceable locations
  - State the root cause in one paragraph
  - Name the assumptions that turned out false and mark them invalidated (never delete them)
  - Write a lesson only if it would change future work
  - Change the method that should have prevented it, with a revision that cites the lesson
  - Add or update the skill check that enforces the changed method
  - Close the reconsiderations the invalidated assumptions raised, with review objects
rationale: >
  A fix without a method change gets lost with the session. A method change without a check gets
  forgotten under pressure. A check without provenance gets deleted as "unexplained".
---
INCIDENT → TIMELINE → ROOT CAUSE → ASSUMPTION FAILURE → LESSON → METHOD CHANGE → SKILL / CHECK UPDATE
