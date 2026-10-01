#!/usr/bin/env bash
# Vendor pinned community dashboards from grafana.com into monitoring/dashboards/<folder>/.
# Datasource inputs are rewritten to the stack's Prometheus datasource (uid "prometheus").
set -euo pipefail
cd "$(dirname "$0")/.."

# folder  id     revision  file
DASHBOARDS=(
  "node        1860  45  node-exporter-full"
  "kubernetes  15757 43  k8s-views-global"
  "kubernetes  15758 46  k8s-views-namespaces"
  "kubernetes  15759 40  k8s-views-nodes"
  "kubernetes  15760 41  k8s-views-pods"
  "storage     16888 14  longhorn"
)

for row in "${DASHBOARDS[@]}"; do
  read -r folder id rev file <<<"$row"
  mkdir -p "monitoring/dashboards/$folder"
  curl -fsSL "https://grafana.com/api/dashboards/$id/revisions/$rev/download" |
    python3 -c '
import json, re, sys
d = json.load(sys.stdin)
d.pop("__inputs", None); d.pop("__requires", None); d.pop("__elements", None)
d["id"] = None
d["tags"] = sorted(set(d.get("tags", [])) | {"homelab"})
s = re.sub(r"\$\{DS_[A-Z0-9_]+\}", "prometheus", json.dumps(d, indent=2))
print(s)' > "monitoring/dashboards/$folder/$file.json"
  echo "  $folder/$file.json  (grafana.com $id rev $rev)"
done
