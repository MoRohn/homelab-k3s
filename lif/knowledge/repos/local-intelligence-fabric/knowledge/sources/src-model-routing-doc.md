---
type: source
title: Model routing
kind: documentation
location: ../../../../../docs/MODEL_ROUTING.md
quality: primary
captured: 2026-10-01
---
Quoted passages from `lif/docs/MODEL_ROUTING.md`.

On any failure, the gateway keeps the last-known-good routing table. ^p1

Otherwise the first healthy profile in the chain is used. Using a non-first profile sets fallback: true with a reason. An empty or entirely unhealthy chain returns 503 with the reason. There is never a silent substitution outside the chain. ^p2

No external provider is configured, and private data is never sent out because local capacity is busy. ^p3
