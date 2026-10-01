---
type: method
status: active
version: 1
title: Semantic review of knowledge
steps:
  - Does each cited source actually support the claim? (read the passage, not the title)
  - Are the supporting sources independent, or do they share a dataset?
  - Is the conclusion stronger than the evidence allows?
  - Does the decision follow from its listed assumptions?
  - Do any two sources contradict each other? Record it as a grounding with stance contradicts
rationale: >
  Structural checks prove links exist; they cannot prove links mean anything. Use the decision
  hierarchy: deterministic checks first, Jev for fast structured judgments, a local model for
  reading passages, a human for anything that changes an accepted decision.
---
