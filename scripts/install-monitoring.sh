#!/usr/bin/env bash
# Bootstrap monitoring: create the secrets Argo CD can't (they aren't in git),
# then hand everything else to Argo CD via the root app.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

# Grafana admin login
if [[ ! -f secrets/grafana.env ]]; then
  printf 'GRAFANA_ADMIN_USER=admin\nGRAFANA_ADMIN_PASSWORD=%s\n' "$(openssl rand -hex 16)" > secrets/grafana.env
fi
source secrets/grafana.env

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl -n monitoring create secret generic grafana-admin \
  --from-literal=admin-user="$GRAFANA_ADMIN_USER" \
  --from-literal=admin-password="$GRAFANA_ADMIN_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

# MinIO scrape token (see scripts/setup-minio-metrics.sh)
if [[ -f secrets/minio-prometheus.token ]]; then
  kubectl -n monitoring create secret generic minio-prometheus \
    --from-literal=token="$(tr -d '[:space:]' < secrets/minio-prometheus.token)" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

kubectl apply -f argocd/root.yaml
echo "Argo CD is syncing. Watch: kubectl -n argocd get applications"
