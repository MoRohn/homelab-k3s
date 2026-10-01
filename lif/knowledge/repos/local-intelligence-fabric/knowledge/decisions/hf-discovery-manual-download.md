---
type: decision
title: Discover models on Hugging Face automatically, but download and promote manually
question: How much of model discovery and adoption should be automatic?
status: accepted
selected: discovery + screening may run; automatic download and promotion are off
alternatives: [fully automatic refresh, fully manual research]
date: 2026-09-25
evidence: ["[[src-platform-config]]", "[[src-model-lifecycle-doc]]"]
assumptions: ["[[primary-workload-owns-gpu]]"]
affects: ["[[manual-first-model-promotion]]"]
---
Grounding: [[src-platform-config^p2]].
