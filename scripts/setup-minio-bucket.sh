#!/usr/bin/env bash
# Create the longhorn-backups bucket, a scoped policy, and the `longhorn` MinIO user.
# Writes the generated password to secrets/longhorn-minio.env (git-ignored).
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=secrets/longhorn-minio.env
MINIO=bnn-minio

if [[ ! -f $ENV ]]; then
  umask 077
  printf 'LONGHORN_MINIO_USER=longhorn\nLONGHORN_MINIO_PASSWORD=%s\nLONGHORN_MINIO_ENDPOINT=http://192.168.68.72:9000\n' \
    "$(openssl rand -hex 24)" > "$ENV"
fi
source "$ENV"

docker cp minio/longhorn-backups-policy.json $MINIO:/tmp/longhorn-policy.json
docker exec -i -e LH_USER="$LONGHORN_MINIO_USER" -e LH_PW="$LONGHORN_MINIO_PASSWORD" $MINIO sh -s <<'IN'
set -e
mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing local/longhorn-backups
mc admin policy create local longhorn-backups-rw /tmp/longhorn-policy.json
mc admin user add local "$LH_USER" "$LH_PW" >/dev/null
mc admin policy attach local longhorn-backups-rw --user "$LH_USER" || true
rm -f /tmp/longhorn-policy.json
mc alias remove local >/dev/null
IN
