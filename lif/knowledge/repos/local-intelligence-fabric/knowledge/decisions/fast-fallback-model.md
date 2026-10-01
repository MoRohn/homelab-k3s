---
type: decision
title: Keep a small hot fallback model that the memory guard never sheds
question: What keeps "useful local AI" up when larger tiers are shed or fail?
status: accepted
selected: tier0 (4B) is the hot fallback, never shed by the memory guard; aliases fall back to it visibly
alternatives: [no fallback (fail closed), fall back to an external API]
date: 2026-09-28
evidence: ["[[src-architecture-doc]]", "[[src-model-routing-doc]]"]
assumptions: ["[[private-data-stays-local]]", "[[cpu-decode-bandwidth-bound]]"]
affects: ["[[availability-target-95]]"]
---
Grounding: [[src-model-routing-doc^p3]] — there is no external fallback by design.
