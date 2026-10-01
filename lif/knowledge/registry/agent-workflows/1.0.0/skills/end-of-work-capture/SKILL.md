---
type: skill
name: end-of-work-capture
version: 1
title: End-of-work knowledge capture
description: Record what changed, why, evidence created, decisions made, assumptions changed, lessons and follow-ups.
when: At the end of every significant task or session.
inputs: [project]
status: active
---
Answer, recording only what is meaningful:
1. What changed? → `change` objects (subject, reason, because).
2. Why? → link the decision or task.
3. What evidence was created? → `artifact`/`result`/`benchmark` with locations.
4. Did we make a decision? → `decision` with evidence, assumptions, alternatives.
5. Did any assumption change? → update its status; let the reconsideration queue do the rest.
6. Did we learn something reusable? → `lesson`; does a method need updating? does a check?
7. What work follows? → `task`s and `question`s.
Then `checkpoint_session` with the summary, links and next steps.
