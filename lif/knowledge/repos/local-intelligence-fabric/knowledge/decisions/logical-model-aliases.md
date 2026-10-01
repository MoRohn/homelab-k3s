---
type: decision
title: Applications call logical model aliases, never physical models
question: How do applications name the model they want?
status: accepted
selected: local/* aliases resolved by the gateway along an ordered chain, with visible fallback
alternatives: [physical Hugging Face IDs in callers, one model for everything]
date: 2026-09-20
evidence: ["[[src-lif-readme]]", "[[src-model-routing-doc]]"]
assumptions: ["[[openai-compatible-api-needed]]"]
affects: ["[[gateway]]", "[[fast-fallback-model]]"]
---
Grounding: [[src-lif-readme^p1]], [[src-model-routing-doc^p2]]. Models can be canaried, promoted and
rolled back without any caller changing.
