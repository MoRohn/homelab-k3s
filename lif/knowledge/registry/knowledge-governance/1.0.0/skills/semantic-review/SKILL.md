---
type: skill
name: semantic-review
version: 1
title: Semantic review of a claim or decision
description: Check that evidence supports what it is cited for, independently and proportionately.
when: Before accepting a decision, and when a claim's groundings change.
inputs: [subject]
method: "[[semantic-review-method]]"
status: active
---
1. `get_evidence` on each claim/assumption the subject rests on; read each passage.
2. Flag shared datasets (`independence_warning`).
3. If a source does not support the claim, change the grounding's stance (never delete it).
4. Record a `review` with outcome reaffirmed / revised / needs-work.
