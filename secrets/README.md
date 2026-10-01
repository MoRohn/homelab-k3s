# secrets/ — local credentials (git-ignored)

Everything in this folder except this README is git-ignored and blocked by the pre-commit guard.
Files are mode `0600`. Back them up out of band (password manager or an encrypted drive); a repo
clone alone cannot rebuild them.

| File | Holds | Created by |
|---|---|---|
| `longhorn-minio.env` | `LONGHORN_MINIO_USER`, `LONGHORN_MINIO_PASSWORD`, `LONGHORN_MINIO_ENDPOINT` | `homelab/scripts/setup-minio-bucket.sh` |
| `minio-prometheus.env`, `minio-prometheus.token` | Metrics-only MinIO user and its bearer token | `homelab/scripts/setup-minio-metrics.sh` |
| `grafana.env` | Grafana admin user and password | `homelab/scripts/install-monitoring.sh` |
| `offsite.env`, `offsite-reader.env` | Off-site SFTP target and read-only MinIO user | `homelab/scripts/setup-offsite.sh` |
| `tailscale.env` | Tailscale OAuth client (`TS_OAUTH_CLIENT_ID`, `TS_OAUTH_CLIENT_SECRET`) | by hand, see `homelab/docs/tailscale.md` |
| `lif-*.key` | LIF gateway, admin and internal keys | `lif/scripts/create-secrets.sh` (generated once) |

`../.env.local` (also git-ignored) holds `TYPE_SAFE_JEV_API_KEY` for the LIF Decision Fabric.
Source it; don't parse it.
