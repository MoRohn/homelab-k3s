# Deployment

Everything below runs as user `tiny` without sudo. `kubectl` uses `~/.kube/config`.

## 1. One-time prerequisites (already done 2026-09-30 / 10-01)

| Step | Command / file | Notes |
|---|---|---|
| Namespaces, PriorityClasses, model store | `deploy/k8s/base/00–02` | Part of the kustomization |
| Local registry | `deploy/k8s/base/03-registry.yaml` | hostPort bound to `127.0.0.1:5000`, so it is not reachable from the LAN. K3s pulls `127.0.0.1:5000/...` over HTTP without any registries.yaml change |
| Seed the pinned Tier 0 weights | `kubectl apply -f deploy/k8s/bootstrap/download-tier0.yaml` | Resumable, sha256-verified, atomic rename; a mismatch is quarantined as `*.bad` |
| Secrets | `scripts/create-secrets.sh` | See below |
| gpusched token | `lif:` line appended to `~/.config/bnn/gpusched.token` | **Dormant** until gpusched next restarts (it reads tokens only at startup). LIF currently uses the read-only `metrics` token |

### Secrets

`scripts/create-secrets.sh` is idempotent and never prints values.

- It reads `TYPE_SAFE_JEV_API_KEY` from `labzilla/.env.local` and the `metrics` line from `~/.config/bnn/gpusched.token`.
- It generates gateway and admin keys once into `labzilla/secrets/lif-{probe,batch,decision,bnn,operator,admin}.key` (mode 0600, git-ignored).
- It applies `ai-system/lif-secrets`:
  - `TYPE_SAFE_JEV_API_KEY`
  - `LIF_GATEWAY_KEYS`
  - `LIF_ADMIN_KEYS`
  - `LIF_PROBE_KEY`
  - `LIF_BATCH_GATEWAY_KEY`
  - `LIF_DECISION_GATEWAY_KEY`
- It applies `ai-system/lif-gpusched` (`token`).

Pods mount these at `/var/run/lif/secrets/` and `/var/run/lif/gpusched/token`.

## 2. Build and deploy

```bash
cd ~/labzilla/lif
.venv/bin/python -m pytest tests -q                       # 54 tests
TAG=$(date +%Y%m%d-%H%M)
docker build -t 127.0.0.1:5000/lif/fabric:$TAG . && docker push 127.0.0.1:5000/lif/fabric:$TAG
kubectl kustomize . | sed "s/IMAGE_TAG/$TAG/g" | kubectl apply -f -
for d in controller gateway decision-fabric batch; do kubectl -n ai-system rollout status deploy/$d; done
```

- `kustomization.yaml` sits at the LIF root, because kustomize won't read `config/` from below `deploy/`.
- It generates the `lif-config` ConfigMap (hashed name, so pods roll on change) from `config/lif.yaml`, `config/models.yaml`, `config/decisions/core.yaml`, `config/workflows/candidate-model-analysis.yaml` and `evals/core.yaml`.
- The ConfigMap is mounted at `/etc/lif`, which overrides the copies bundled in the image.

### After every apply

1. **Check host headroom.** The kustomization sets `tier0-small` back to 1 replica. The memory guard re-sheds it within about 60 s if MemAvailable is below 9 GiB.
   ```bash
   cd ~/bnn/apps/blerbz-news-network && .venv/bin/python -m bnn.gpusched status | sed -n 2,3p
   ```
   `available` must stay at or above ~8 GiB, and there must be no `unmanaged` hold.
2. **Smoke test:** `local-ai doctor` (see OPERATIONS.md).

## 3. What needs the owner

| Action | Why |
|---|---|
| Push to GitHub | Argo CD syncs `main` of `MoRohn/labzilla`. A push is a production deploy |
| Argo CD Application for LIF | Not active. Template: `homelab/argocd/templates/local-intelligence-fabric.yaml.example` (path `lif`), plus the image tag substitution (kustomize `images:` / an `IMAGE_TAG` overlay) |
| Restart gpusched | Activates the `lif:` token (only needed for GPU leases) |
| Any sudo step | The agent has no sudo: firewall, containerd config, systemd system units |
| `/etc/hosts` / LAN DNS for `*.tiny-dgx.lan` | For ingress |

### Argo CD note: replicas versus the memory guard

The controller scales `ai-serving/tier0-small` (and, in the danger zone, `embedding`) to 0. A self-healing Argo app would undo that. Add:

```yaml
ignoreDifferences:
  - group: apps
    kind: Deployment
    namespace: ai-serving
    jsonPointers: [/spec/replicas]
```

Model server Deployments the controller creates (`lif-*`, `cand-*`) are not in git by design. Exclude them from pruning or keep them out of the app's resource scope.

## 4. Removing LIF

```bash
kubectl kustomize . | sed "s/IMAGE_TAG/x/g" | kubectl delete -f -
```

This deletes the PVCs (`registry.db`, `batch.db`, `decisions.db`, `model-store`), so back up first. The primary workload and gpusched are unaffected. Remove the `lif:` line from `gpusched.token` by hand.
