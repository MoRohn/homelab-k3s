# private/ — sensitive project material (git-ignored)

Real project documents and data that must not be public, kept next to the code they describe.
The tree mirrors the repo: `private/lif/docs/X.md` is the private sibling of `lif/docs/`.

Put a file here instead of in a tracked folder when it contains any of:

- host, network or service inventories, risk registers, audit findings
- security posture details: which services lack auth, incidents, key-rotation notes
- primary-workload integration internals or production observations
- benchmarks captured from production traffic

Tracked docs may link here. Mark such links *(private, local only)* so public readers
know why they 404.

Current contents:

| Path | What |
|---|---|
| `lif/docs/audit/` | Phase 0 host, GPU and network baselines; risk register |
| `lif/docs/SECURITY.md` | Keys, auth surface, network policy, open actions |
| `lif/docs/PRIMARY_WORKLOAD_INTEGRATION.md` | How the primary workload uses LIF |
| `lif/docs/PRODUCTION_READINESS.md` | Measured results, acceptance status, risks |
| `lif/benchmarks/` | Production-interference and takeover observations, lifecycle validation log |
