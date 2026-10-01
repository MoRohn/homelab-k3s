---
type: task
title: Enable the controller event sink into lif-operations
status: open
depends_on: ["[[deploy-knowledge-service]]"]
implements: "[[manual-first-model-promotion]]"
---
Give the knowledge service an admin key for `/v1/activity` (Secret `LIF_EVENTS_ADMIN_KEY`) so model
promotions, rollbacks and benchmarks become persistent knowledge automatically.
