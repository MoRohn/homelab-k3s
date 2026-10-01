---
type: skill
name: incident-review
version: 1
title: Review an incident and propagate its lessons
description: Turn an incident into a lesson, a method revision and a skill check, each linked back to the incident.
when: After any sev1–sev3 incident, or a near miss that exposed a false assumption.
inputs: [incident]
outputs: [lesson, method revision, skill check, reviews for raised reconsiderations]
method: "[[incident-learning]]"
status: active
---
Use `knowledge` (or MCP) to: get the incident; `change_impact` on each failed assumption;
create the lesson (evidence = the incident, `invalidates`, `changed`); bump the method's version
with a revision citing the lesson; add a check to the enforcing skill with `because:` the lesson;
record reviews for each reconsideration. `lif.knowledge.learn.learn()` does the writes in one call.
