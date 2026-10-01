---
type: source
title: Model lifecycle
kind: documentation
location: ../../../../../docs/MODEL_LIFECYCLE.md
quality: primary
captured: 2026-10-01
---
Quoted passages from `lif/docs/MODEL_LIFECYCLE.md`.

The registry owns model state. Every change is an activity event with an actor and a reason. ^p1

Only PRODUCTION, CANARY, APPROVED and STANDBY models may appear in an alias. A model referenced by an alias can't leave PRODUCTION, and can't be deleted. ^p2

The revision must be a 40-char commit SHA. main is rejected. ^p3
