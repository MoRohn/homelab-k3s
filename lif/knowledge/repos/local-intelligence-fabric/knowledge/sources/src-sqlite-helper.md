---
type: source
title: LIF SQLite helper (lif/common/db.py)
kind: repository
location: ../../../../../lif/common/db.py
quality: primary
captured: 2026-10-01
---
Quoted from the module docstring of `lif/lif/common/db.py`.

Each service owns its own database file on its own PVC (registry.db, batch.db, decisions.db), so there is never more than one writer process per file. ^p1
