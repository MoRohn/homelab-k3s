# MinIO (external dependency)

MinIO is **not** in this cluster. It is the `bnn-minio` container from the bnn stack:
`~/bnn/docker-compose.foundation.yml`, data in `~/bnn/data/object_store`.

## Port binding for k3s

The S3 API is bound to the LAN IP in addition to localhost so Longhorn pods can reach it:

```yaml
    ports:
      - "127.0.0.1:9000:9000"       # S3 API (bnn scripts use this)
      - "192.168.68.72:9000:9000"   # S3 API for k3s (Longhorn backups)
      - "127.0.0.1:9001:9001"       # web console, local only
```

## Restarting MinIO — always pass the env file

```bash
cd ~/bnn && docker compose -f docker-compose.foundation.yml --env-file secrets/.env up -d minio
```

Without `--env-file`, `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` interpolate as empty and MinIO
falls back to the default `minioadmin:minioadmin` login.

## Longhorn's access

- Bucket: `longhorn-backups`
- User: `longhorn`, policy `longhorn-backups-rw` (that bucket only) — `minio/longhorn-backups-policy.json`
- Password: `secrets/longhorn-minio.env` (repo root)
