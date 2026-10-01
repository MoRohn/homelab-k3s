---
type: task
title: Deploy the knowledge service to the cluster
status: completed
implements: '[[knowledge-canonical-typed-markdown]]'
updated: '2026-10-01'
updated_by: agent:claude-code
---

Manifest: `lif/deploy/k8s/knowledge/knowledge.yaml` (not in the default kustomization). Needs the
owner's go-ahead; see docs/KNOWLEDGE.md → Deployment.

Completed 2026-10-01: applied lif/deploy/k8s/knowledge/knowledge.yaml (admin keys from lif-secrets; /tmp emptyDir added because SQLite's FTS5 temp file failed on the read-only root). Event sink stays off (no lif-events-key); see enable-event-sink. The Labzilla Console reads through it.
