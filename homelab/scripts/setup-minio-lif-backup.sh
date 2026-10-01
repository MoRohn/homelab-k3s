#!/usr/bin/env bash
# Create the `lif-backup` MinIO user, scoped to longhorn-backups/lif-sqlite/ only (it cannot
# read or delete Longhorn's own backups), and the ai-system/lif-backup-minio Secret that the
# lif-sqlite-backup CronJob uses. Idempotent. Never prints the password.
# Writes ../secrets/lif-backup-minio.env (git-ignored).
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=../secrets/lif-backup-minio.env
MINIO=bnn-minio

if [[ ! -f $ENV ]]; then
  umask 077
  printf 'LIF_BACKUP_MINIO_USER=lif-backup\nLIF_BACKUP_MINIO_PASSWORD=%s\nLIF_BACKUP_MINIO_ENDPOINT=http://192.168.68.72:9000\n' \
    "$(openssl rand -hex 24)" > "$ENV"
fi
source "$ENV"

docker cp minio/lif-sqlite-backup-policy.json $MINIO:/tmp/lif-sqlite-policy.json
docker exec -i -e U="$LIF_BACKUP_MINIO_USER" -e P="$LIF_BACKUP_MINIO_PASSWORD" $MINIO sh -s <<'IN'
set -e
mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing local/longhorn-backups >/dev/null
mc admin policy create local lif-sqlite-backup-rw /tmp/lif-sqlite-policy.json >/dev/null
mc admin user add local "$U" "$P" >/dev/null
mc admin policy attach local lif-sqlite-backup-rw --user "$U" >/dev/null 2>&1 || true
rm -f /tmp/lif-sqlite-policy.json
mc alias remove local >/dev/null
IN

kubectl -n ai-system create secret generic lif-backup-minio \
  --from-literal=S3_ENDPOINT="$LIF_BACKUP_MINIO_ENDPOINT" \
  --from-literal=S3_ACCESS_KEY="$LIF_BACKUP_MINIO_USER" \
  --from-literal=S3_SECRET_KEY="$LIF_BACKUP_MINIO_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "MinIO user $LIF_BACKUP_MINIO_USER (lif-sqlite/ only) and Secret ai-system/lif-backup-minio are in place."
