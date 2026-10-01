---
type: decision
title: Manual-first model promotion with a recorded benchmark
question: Who promotes a model to production, and on what evidence?
status: accepted
selected: an operator promotes; the registry refuses models without a local benchmark
alternatives: [automatic promotion on benchmark win]
date: 2026-09-25
evidence: ["[[src-model-lifecycle-doc]]", "[[src-platform-config]]"]
assumptions: ["[[single-node-primary]]"]
related: ["[[model-lifecycle::manual-first-promotion]]", "[[model-evaluation::model-promotion]]"]
---
Applies the package method [[model-lifecycle::manual-first-promotion]]. Promotions recorded by the
event sink become decision objects whose evidence is the benchmark.
