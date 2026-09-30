# homelab-k3s

Single-node k3s cluster on `tiny-dgx` (192.168.68.72), with Longhorn block storage
backed up to MinIO.

## Layout

| Path | What |
|---|---|
| `host/` | One-time host prep (run with sudo) |
| `longhorn/values.yaml` | Longhorn Helm values (chart 1.13.0) |
| `minio/` | MinIO policy for the Longhorn backup user |
| `scripts/` | Repeatable setup steps |
| `secrets/` | Local credentials — git-ignored |
| `docs/` | Reference docs |
| `personal/` | Your own notes and drafts — git-ignored |

## Setup walkthrough

1. **kubectl access**: copy `/etc/rancher/k3s/k3s.yaml` to `~/.kube/config` (owned by you) and
   `export KUBECONFIG=~/.kube/config` in `~/.bashrc`.
2. **Host prep**: `sudo host/prep-longhorn.sh` (iscsid, iscsi_tcp, multipath blacklist).
3. **Expose MinIO to the cluster**: see [docs/minio.md](docs/minio.md).
4. **MinIO bucket and user**: `scripts/setup-minio-bucket.sh`.
5. **Install Longhorn** (also creates the `minio-credentials` secret and sets the backup target):
   `scripts/install-longhorn.sh`.
6. **Verify**:
   ```bash
   kubectl -n longhorn-system get backuptargets.longhorn.io   # AVAILABLE should be true
   kubectl get sc                                               # local-path (default) + longhorn
   ```
7. **Test backup and restore**: `scripts/test-backup.sh` writes 50 MiB to a Longhorn volume,
   backs it up to MinIO, restores it to a new volume, compares checksums, then removes everything.

## Using Longhorn

`local-path` stays the default StorageClass. Request Longhorn explicitly:

```yaml
spec:
  storageClassName: longhorn
```

UI (no authentication, so not exposed on the LAN):

```bash
kubectl -n longhorn-system port-forward svc/longhorn-frontend 8080:80   # http://localhost:8080
```

## Caveats

- MinIO runs on this same machine, so backups protect against mistakes, not disk or host loss.
- 192.168.68.72 is DHCP on Wi-Fi. Reserve it in the router or the node and backup endpoint break.
