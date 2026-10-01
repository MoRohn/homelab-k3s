#!/usr/bin/env bash
# Off-site copy of Longhorn backups: MinIO bucket -> another machine over SFTP.
#
# - Streams straight from MinIO with rclone (no local staging copy).
# - Never deletes off-site data outright: files that change or disappear here are moved
#   into versions/<timestamp>/ on the target and kept for $KEEP_DAYS days. A wiped or
#   corrupted bucket on this machine can't propagate into the only other copy.
# - Runs hourly from a systemd user timer but only syncs when the last success is older
#   than $MIN_INTERVAL_H, so a laptop target that sleeps at night catches up when it wakes.
# - Reports to Prometheus via node-exporter's textfile collector.
#
# Config: secrets/offsite.env (git-ignored)
#   OFFSITE_HOST=192.168.68.61   OFFSITE_USER=<mac user>   OFFSITE_PATH=Backups/tiny-dgx/longhorn
#   [OFFSITE_KEY=~/.ssh/id_ed25519_backup] [KEEP_DAYS=30] [MIN_INTERVAL_H=20] [BWLIMIT=25M]
set -euo pipefail
cd "$(dirname "$0")/.."
source secrets/offsite.env
source secrets/offsite-reader.env
OFFSITE_KEY=${OFFSITE_KEY:-$HOME/.ssh/id_ed25519_backup}
KEEP_DAYS=${KEEP_DAYS:-30}; MIN_INTERVAL_H=${MIN_INTERVAL_H:-20}; BWLIMIT=${BWLIMIT:-25M}
STATE=$HOME/.local/state/homelab-offsite; mkdir -p "$STATE"
METRICS_DIR=${METRICS_DIR:-/var/tmp/node_exporter_textfile}
mkdir -p "$METRICS_DIR" && chmod 755 "$METRICS_DIR"
RCLONE=$HOME/.local/bin/rclone

metrics() {  # write atomically so node-exporter never reads a half-written file
  [[ -d $METRICS_DIR && -w $METRICS_DIR ]] || return 0
  local tmp; tmp=$(mktemp "$METRICS_DIR/.offsite.XXXX")
  {
    echo "# HELP homelab_offsite_last_success_timestamp_seconds Last successful off-site sync."
    echo "# TYPE homelab_offsite_last_success_timestamp_seconds gauge"
    echo "homelab_offsite_last_success_timestamp_seconds $(cat "$STATE/last_success" 2>/dev/null || echo 0)"
    echo "# HELP homelab_offsite_last_attempt_timestamp_seconds Last off-site sync attempt."
    echo "# TYPE homelab_offsite_last_attempt_timestamp_seconds gauge"
    echo "homelab_offsite_last_attempt_timestamp_seconds $(date +%s)"
    echo "# HELP homelab_offsite_target_reachable Whether the off-site target answered on the last check."
    echo "# TYPE homelab_offsite_target_reachable gauge"
    echo "homelab_offsite_target_reachable $1"
    echo "# HELP homelab_offsite_bytes Size of the current off-site copy."
    echo "# TYPE homelab_offsite_bytes gauge"
    echo "homelab_offsite_bytes $(cat "$STATE/bytes" 2>/dev/null || echo 0)"
    echo "# HELP homelab_offsite_last_run_failed 1 if the most recent sync attempt failed."
    echo "# TYPE homelab_offsite_last_run_failed gauge"
    echo "homelab_offsite_last_run_failed ${2:-0}"
  } > "$tmp"; chmod 644 "$tmp"; mv "$tmp" "$METRICS_DIR/offsite.prom"
}

last=$(cat "$STATE/last_success" 2>/dev/null || echo 0)
if [[ ${1:-} != --force ]] && (( $(date +%s) - last < MIN_INTERVAL_H * 3600 )); then
  metrics 1; exit 0
fi

SSH_OPTS=(-i "$OFFSITE_KEY" -o BatchMode=yes -o ConnectTimeout=8 -o UserKnownHostsFile="$HOME/.ssh/known_hosts")
if ! ssh "${SSH_OPTS[@]}" "$OFFSITE_USER@$OFFSITE_HOST" true 2>/dev/null; then
  echo "Off-site target $OFFSITE_HOST unreachable (asleep or offline); will retry next hour."
  metrics 0; exit 0
fi

# rclone remotes from env vars: no config file holding credentials
export RCLONE_CONFIG_SRC_TYPE=s3 RCLONE_CONFIG_SRC_PROVIDER=Minio \
       RCLONE_CONFIG_SRC_ENDPOINT=http://127.0.0.1:9000 \
       RCLONE_CONFIG_SRC_ACCESS_KEY_ID="$OFFSITE_MINIO_USER" \
       RCLONE_CONFIG_SRC_SECRET_ACCESS_KEY="$OFFSITE_MINIO_PASSWORD" \
       RCLONE_CONFIG_SRC_NO_CHECK_BUCKET=true
export RCLONE_CONFIG_DST_TYPE=sftp RCLONE_CONFIG_DST_HOST="$OFFSITE_HOST" \
       RCLONE_CONFIG_DST_USER="$OFFSITE_USER" RCLONE_CONFIG_DST_KEY_FILE="$OFFSITE_KEY" \
       RCLONE_CONFIG_DST_KNOWN_HOSTS_FILE="$HOME/.ssh/known_hosts" \
       RCLONE_CONFIG_DST_HOST_KEY_ALGORITHMS=ssh-ed25519 \
       RCLONE_CONFIG_DST_SHELL_TYPE=unix RCLONE_CONFIG_DST_MD5SUM_COMMAND=none RCLONE_CONFIG_DST_SHA1SUM_COMMAND=none

trap 'metrics 1 1; echo "Off-site sync FAILED" >&2' ERR

count() {  # a missing path (first run) counts as 0 files, not as a failure
  { "$RCLONE" size "$1" --json 2>/dev/null || true; } | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("count",0))
except Exception: print(0)'; }
# Shrink guard: if the source suddenly holds far fewer files than the off-site copy (bucket
# wiped, wrong credentials, MinIO restored empty), stop instead of mirroring the loss.
src_n=$(count src:longhorn-backups); dst_n=$(count "dst:$OFFSITE_PATH/current")
if (( dst_n > 20 && src_n * 2 < dst_n )) && [[ ${ALLOW_SHRINK:-0} != 1 ]]; then
  echo "REFUSING to sync: source has $src_n files, off-site copy has $dst_n. If this is" >&2
  echo "intentional, rerun with ALLOW_SHRINK=1." >&2
  metrics 1 1; exit 1
fi

stamp=$(date +%Y-%m-%dT%H%M)
echo "Syncing longhorn-backups -> $OFFSITE_USER@$OFFSITE_HOST:$OFFSITE_PATH/current"
"$RCLONE" sync src:longhorn-backups "dst:$OFFSITE_PATH/current" \
  --backup-dir "dst:$OFFSITE_PATH/versions/$stamp" \
  --fast-list --transfers 4 --checkers 8 --bwlimit "$BWLIMIT" \
  --retries 3 --low-level-retries 10 --stats-one-line --stats 0 -v 2>&1 | tail -5

# Prune old versions (only ever touches versions/, never current/)
"$RCLONE" delete "dst:$OFFSITE_PATH/versions" --min-age "${KEEP_DAYS}d" --rmdirs 2>/dev/null || true

{ "$RCLONE" size "dst:$OFFSITE_PATH/current" --json 2>/dev/null || true; } | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("bytes",0))
except Exception: print(0)' > "$STATE/bytes"
date +%s > "$STATE/last_success"
metrics 1
echo "Off-site sync complete."
