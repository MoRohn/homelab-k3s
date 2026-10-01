---
type: decision
title: Structured decisions go through the Decision Fabric (Jev → rules → local LLM → human)
question: How should fast structured decisions be made across LIF?
status: accepted
selected: Jev for calibrated typed decisions on PUBLIC state; deterministic rules always available; policy code acts
alternatives: [prompt a local LLM for every decision, hard-coded rules only]
date: 2026-09-22
evidence: ["[[src-jev-doc]]", "[[src-platform-config]]"]
assumptions: ["[[private-data-stays-local]]"]
---
Grounding: [[src-jev-doc^p1]], [[src-jev-doc^p3]], [[src-platform-config^p4]]. Knowledge-layer
decisions (context profile, passage classification, review priority, duplicates) use the same
fabric after deterministic graph rules (config/decisions/knowledge.yaml).
