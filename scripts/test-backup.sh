#!/usr/bin/env bash
# End-to-end Longhorn backup test: write data -> snapshot -> backup to MinIO ->
# restore to a new volume -> compare checksums -> clean up everything.
set -euo pipefail
cd "$(dirname "$0")/.."
NS=longhorn-test
LH=longhorn-system

wait_for() {  # wait_for <description> <timeout-seconds> <command...>
  local desc=$1 t=$2; shift 2
  for ((i = 0; i < t; i += 3)); do "$@" >/dev/null 2>&1 && return 0; timeout 3 tail -f /dev/null || true; done
  echo "Timed out waiting for: $desc" >&2; return 1
}

cleanup() {
  echo "==> Cleaning up"
  kubectl delete namespace $NS --ignore-not-found --wait=true >/dev/null
  kubectl delete storageclass longhorn-restore-test --ignore-not-found >/dev/null
  if [[ -n ${VOL:-} ]]; then
    kubectl -n $LH delete backups.longhorn.io -l backup-volume="$VOL" --ignore-not-found >/dev/null
    kubectl -n $LH delete backupvolumes.longhorn.io -l backup-volume="$VOL" --ignore-not-found >/dev/null
  fi
}
trap cleanup EXIT

echo "==> 1. Writing test data to a new Longhorn volume"
kubectl create namespace $NS --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: source, namespace: $NS}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn
  resources: {requests: {storage: 1Gi}}
---
apiVersion: v1
kind: Pod
metadata: {name: writer, namespace: $NS}
spec:
  containers:
  - name: writer
    image: busybox:1.36
    command: [sh, -c, "dd if=/dev/urandom of=/data/blob bs=1M count=50 2>/dev/null && sha256sum /data/blob | cut -d' ' -f1 > /data/blob.sha && sync && sleep 3600"]
    volumeMounts: [{name: data, mountPath: /data}]
  volumes: [{name: data, persistentVolumeClaim: {claimName: source}}]
EOF
kubectl -n $NS wait --for=condition=Ready pod/writer --timeout=180s >/dev/null
wait_for "test data written" 60 kubectl -n $NS exec writer -- test -s /data/blob.sha
SRC_SHA=$(kubectl -n $NS exec writer -- cat /data/blob.sha)
VOL=$(kubectl -n $NS get pvc source -o jsonpath='{.spec.volumeName}')
echo "    volume $VOL, sha256 $SRC_SHA"

echo "==> 2. Snapshot + backup to MinIO"
kubectl apply -f - >/dev/null <<EOF
apiVersion: longhorn.io/v1beta2
kind: Snapshot
metadata: {name: test-snap, namespace: $LH}
spec: {volume: $VOL, createSnapshot: true}
EOF
wait_for "snapshot ready" 120 bash -c "[ \"\$(kubectl -n $LH get snapshots.longhorn.io test-snap -o jsonpath='{.status.readyToUse}')\" = true ]"
kubectl apply -f - >/dev/null <<EOF
apiVersion: longhorn.io/v1beta2
kind: Backup
metadata:
  name: test-backup
  namespace: $LH
  labels: {backup-volume: $VOL, backup-target: default}
spec: {snapshotName: test-snap}
EOF
wait_for "backup completed" 300 bash -c "[ \"\$(kubectl -n $LH get backups.longhorn.io test-backup -o jsonpath='{.status.state}')\" = Completed ]"
URL=$(kubectl -n $LH get backups.longhorn.io test-backup -o jsonpath='{.status.url}')
echo "    backup $URL"

source secrets/longhorn-minio.env
OBJECTS=$(docker exec -e LH_USER="$LONGHORN_MINIO_USER" -e LH_PW="$LONGHORN_MINIO_PASSWORD" bnn-minio sh -c \
  'mc alias set t http://127.0.0.1:9000 "$LH_USER" "$LH_PW" >/dev/null && mc ls -r t/longhorn-backups | wc -l; mc alias remove t >/dev/null')
echo "    MinIO bucket longhorn-backups holds $OBJECTS objects"

echo "==> 3. Restoring backup to a new volume"
kubectl delete pod -n $NS writer --wait=true >/dev/null
kubectl apply -f - >/dev/null <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: longhorn-restore-test}
provisioner: driver.longhorn.io
reclaimPolicy: Delete
parameters: {numberOfReplicas: "1", fromBackup: "$URL"}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: restored, namespace: $NS}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn-restore-test
  resources: {requests: {storage: 1Gi}}
---
apiVersion: v1
kind: Pod
metadata: {name: reader, namespace: $NS}
spec:
  containers:
  - name: reader
    image: busybox:1.36
    command: [sleep, "3600"]
    volumeMounts: [{name: data, mountPath: /data}]
  volumes: [{name: data, persistentVolumeClaim: {claimName: restored}}]
EOF
kubectl -n $NS wait --for=condition=Ready pod/reader --timeout=600s >/dev/null
RESTORED_SHA=$(kubectl -n $NS exec reader -- sh -c "sha256sum /data/blob | cut -d' ' -f1")
echo "    restored sha256 $RESTORED_SHA"

if [[ $SRC_SHA == "$RESTORED_SHA" ]]; then
  echo "==> PASS: restored data matches the original"
else
  echo "==> FAIL: checksum mismatch" >&2; exit 1
fi
