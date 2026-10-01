---
type: source
title: LIF platform policy (config/lif.yaml)
kind: repository
location: ../../../../../config/lif.yaml
quality: primary
captured: 2026-10-01
---
Quoted settings from `lif/config/lif.yaml`.

availability_target: 0.95 ^p1

models: automatic_discovery: false, automatic_download: false, automatic_promotion: false, rollback_versions: 2. ^p2

privacy: default_class CONFIDENTIAL — unlabelled application data never leaves the box; jev_allowed: [PUBLIC]. ^p3

decision_fabric: provider typesafe_jev, model jev-1.13.0 (pinned; jev-latest is never used in production). ^p4
