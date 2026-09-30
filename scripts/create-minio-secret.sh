#!/usr/bin/env bash
# Create/update the minio-credentials secret Longhorn uses for backups.
set -euo pipefail
cd "$(dirname "$0")/.."
source secrets/longhorn-minio.env

kubectl create namespace longhorn-system --dry-run=client -o yaml | kubectl apply -f -
kubectl -n longhorn-system create secret generic minio-credentials \
  --from-literal=AWS_ACCESS_KEY_ID="$LONGHORN_MINIO_USER" \
  --from-literal=AWS_SECRET_ACCESS_KEY="$LONGHORN_MINIO_PASSWORD" \
  --from-literal=AWS_ENDPOINTS="$LONGHORN_MINIO_ENDPOINT" \
  --dry-run=client -o yaml | kubectl apply -f -
