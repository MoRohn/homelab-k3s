---
type: decision
title: The primary workload has GPU priority; gpusched is the only GPU admission authority
question: Who decides what may use the GPU?
status: accepted
selected: gpusched (host) decides; LIF is a read-only client and leases through it
alternatives: [Kubernetes device plugin + PriorityClasses, LIF-side GPU scheduler]
date: 2026-09-15
evidence: ["[[src-gpu-scheduling-doc]]", "[[src-architecture-doc]]"]
assumptions: ["[[primary-workload-owns-gpu]]"]
affects: ["[[cpu-tiers-for-local-inference]]", "[[gateway]]", "[[batch]]"]
rationale: >
  PriorityClasses order only CPU and RAM inside K3s; the kubelet cannot see unified GPU memory.
  One host-level authority that already protects the primary workload avoids two schedulers
  disagreeing about the same memory.
---
Grounding: [[src-gpu-scheduling-doc^p1]], [[src-gpu-scheduling-doc^p2]].
