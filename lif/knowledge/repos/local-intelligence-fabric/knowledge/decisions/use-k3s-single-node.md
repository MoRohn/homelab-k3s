---
type: decision
title: Use K3s as the orchestration layer on the single DGX node
question: Which orchestration layer should run the platform on one DGX Spark?
status: accepted
selected: k3s
alternatives: [full upstream Kubernetes (kubeadm), Docker Compose only, no orchestration]
date: 2026-09-01
evidence: ["[[src-homelab-readme]]"]
assumptions: ["[[single-node-primary]]"]
affects: ["[[gateway]]", "[[controller]]", "[[decision-fabric]]", "[[batch]]"]
rationale: >
  K3s is a conformant Kubernetes in one process: low overhead on a node whose memory is mostly
  owned by the primary workload, GitOps via Argo CD, and manifests that stay portable to standard
  Kubernetes if a second node ever arrives.
---
Use K3s initially while keeping workloads portable to standard Kubernetes. The primary workload
itself stays in Docker and is not migrated.
