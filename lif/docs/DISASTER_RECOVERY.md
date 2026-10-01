# Disaster recovery

## State and where it lives

| Data | Location | Backed up | Loss impact |
|---|---|---|---|
| Model registry, aliases, activity, availability (`registry.db`) | PVC `ai-system/lif-registry` (Longhorn) | **not yet**: no Longhorn recurring backup job exists (checked 2026-10-01). Add one for `lif-registry` (MinIO target) | High: alias versions and evidence. The seed rebuilds a minimal registry from `config/models.yaml` |
| Decision log (`decisions.db`) | `lif-decisions` (Longhorn) | not yet | Low (30-day history) |
| Batch queue (`batch.db`) | `lif-batch` (Longhorn) | not yet | Medium: pending jobs |
| Model weights | `ai-serving/model-store` (local-path, a host dir) | **no**, re-downloadable | Re-download by sha-pinned Job |
| Images | `ai-system/registry` (local-path) | no | Rebuild with `docker build` |
| Config | git (`config/`, manifests) | git | none |
| Secrets | `labzilla/.env.local`, `secrets/lif-*.key` (host only) | **no**: back these up out of band | Regenerate keys with `create-secrets.sh` (clients must update) |

## Reboot (designed order)

1. K3s starts.
2. Model servers start: tier0, tier0-small and embedding, with startup probes up to 300 s.
3. Gateway: ready only when at least one model is healthy (`/readyz`).
4. Controller: its probes run every 30 s. gpusched is unreachable until its user unit starts, and LIF treats that as **IMMINENT (fail-safe)** until it answers.
5. Decision fabric and batch: batch items that were `running` return to `pending`.
6. gpusched's user unit may start before or after K3s. Until LIF can read it, the state stays IMMINENT, so CPU tiers run at concurrency 1 (cluster-wide) and batch is paused.

"Container started" is not "usable": readiness and the availability probes check real inference.

**Status: a host reboot has not been exercised.** It is pending owner approval, because it reboots BNN production too.

## Failure scenarios

| Failure | Detect | Contain / recover | Validated |
|---|---|---|---|
| Model server crash | `/health` probe (10 s) + passive breaker | Router falls back along the alias chain (`fallback: true`); kubelet restarts the pod | unit test (gateway fallback) |
| Gateway pod crash | readiness | Second replica + PDB | – |
| Controller down | – | Gateway keeps its last-known-good routing table; probes and guard pause | design |
| Jev outage / no internet | breaker, `LIFJevCircuitOpen` | Rules answer. Installed models unaffected. Discovery and downloads fail cleanly | unit test (502 → rules) |
| HF outage | discovery run `failed` with error | Nothing else affected | – |
| gpusched down | metrics unreachable | State = IMMINENT: minimum CPU concurrency, batch paused | unit test |
| Host memory pressure | guard (MemAvailable via gpusched) | Shed `tier0-small`, then `embedding` | **live 2026-10-01 07:21** |
| Corrupt download | sha256 mismatch | `*.bad`, state QUARANTINED | code path (not exercised live) |
| Bad promotion | quality drop / fallbacks | `local-ai models rollback <alias>` | unit test (registry rollback) |
| Disk pressure | – | Retention GC (7 days); 3.3 TB free today | – |

## Restore procedures

**Re-seed the model store:**

```bash
kubectl apply -f deploy/k8s/bootstrap/download-tier0.yaml
```

Cached files are re-verified rather than re-downloaded.

**Restore `registry.db`** (once a recurring backup exists):

1. Restore the Longhorn backup of `lif-registry` from MinIO (Longhorn UI → Backup → restore to a new volume).
2. Scale the controller to 0 and swap the PVC.
3. Scale the controller back to 1.

**Lost secrets:**

1. Delete `secrets/lif-*.key` and run `scripts/create-secrets.sh`.
2. `kubectl -n ai-system rollout restart deploy`.
3. Hand out the new keys (BNN: `lif-bnn.key`).
