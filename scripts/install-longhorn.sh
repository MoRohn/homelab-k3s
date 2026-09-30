#!/usr/bin/env bash
# Install or upgrade Longhorn from longhorn/values.yaml.
set -euo pipefail
cd "$(dirname "$0")/.."
LONGHORN_VERSION=1.13.0

scripts/create-minio-secret.sh
helm repo add longhorn https://charts.longhorn.io >/dev/null 2>&1 || true
helm repo update longhorn >/dev/null
helm upgrade --install longhorn longhorn/longhorn --version "$LONGHORN_VERSION" \
  -n longhorn-system --create-namespace -f longhorn/values.yaml --wait --timeout 10m
kubectl -n longhorn-system get pods
