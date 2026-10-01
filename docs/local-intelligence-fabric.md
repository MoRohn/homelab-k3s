# Local Intelligence Fabric (optional add-on)

The **Local Intelligence Fabric (LIF)** is a private, OpenAI-compatible AI platform that runs on
this cluster (`ai-system` and `ai-serving` namespaces). It's a separate project, kept out of this
repository, which is public:

| What | Where |
|---|---|
| Source, deploy manifests, docs | `~/local-intelligence-fabric` (its own folder) |
| Compatibility path | `~/homelab-k3s/local-intelligence-fabric` → symlink, git-ignored |
| API keys | `~/homelab-k3s/secrets/lif-*.key` (git-ignored, referenced by LIF's docs) |
| Gateway | `https://ai.tiny-dgx.lan` → Traefik on `192.168.68.72` |

The symlink exists because LIF's virtualenv and editable install hard-code the old path. Once
LIF recreates its `.venv` under `~/local-intelligence-fabric`, the symlink can be deleted.

## How it fits with the rest of the homelab

- **Memory:** LIF models run on the CPU and share unified memory with the BNN model servers.
  The Homelab Overview dashboard shows `ai-serving` / `ai-system` under *Memory by namespace*.
- **GPU:** any LIF GPU tier must go through bnn gpusched (`~/bnn/deploy/k8s/gpusched`).
- **Ingress:** LIF relies on Traefik keeping `192.168.68.72`; the MetalLB config pins it there.
- **Monitoring:** LIF ships its own ServiceMonitors (`deploy/k8s/base/14-monitoring.yaml`); the
  Prometheus here picks up every ServiceMonitor in the cluster automatically.

## Optional: manage LIF with Argo CD

`argocd/templates/local-intelligence-fabric.yaml.example` is a ready-made Argo CD Application,
inactive until copied into `argocd/apps/`. It needs LIF to live in its own git repository first:

```bash
cd ~/local-intelligence-fabric
git init -b main && git add -A && git commit -m "Initial import"   # check .gitignore covers .venv first
# create an empty repo on GitHub, then:
git remote add origin git@github.com:MoRohn/local-intelligence-fabric.git && git push -u origin main
```

Then follow the four steps at the top of the template.
