#!/usr/bin/env bash
# Create a MinIO `prometheus` user that can only read metrics, and save a scrape
# token to ../secrets/minio-prometheus.token (git-ignored).
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
MINIO=bnn-minio

[[ -f ../secrets/minio-prometheus.env ]] || \
  printf 'MINIO_PROM_USER=prometheus\nMINIO_PROM_PASSWORD=%s\n' "$(openssl rand -hex 24)" > ../secrets/minio-prometheus.env
source ../secrets/minio-prometheus.env

docker cp minio/prometheus-policy.json $MINIO:/tmp/prometheus-policy.json
docker exec -i -e U="$MINIO_PROM_USER" -e P="$MINIO_PROM_PASSWORD" $MINIO sh -s <<'IN' \
  | awk '/bearer_token:/ {print $2}' > ../secrets/minio-prometheus.token
set -e
mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc admin policy create local prometheus-metrics /tmp/prometheus-policy.json >/dev/null
mc admin user add local "$U" "$P" >/dev/null
mc admin policy attach local prometheus-metrics --user "$U" >/dev/null 2>&1 || true
rm -f /tmp/prometheus-policy.json
mc alias set prom http://127.0.0.1:9000 "$U" "$P" >/dev/null
mc admin prometheus generate prom
mc alias remove prom >/dev/null; mc alias remove local >/dev/null
IN
[[ -s ../secrets/minio-prometheus.token ]] || { echo "Token generation failed" >&2; exit 1; }
echo "Token saved to ../secrets/minio-prometheus.token"
