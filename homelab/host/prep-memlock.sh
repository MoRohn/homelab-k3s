#!/usr/bin/env bash
# One-time host prep so pods can mlock. Run with sudo. Restarts k3s (~1 min).
# k3s ships LimitMEMLOCK=8M, which every container inherits, so tier0's mlock locks nothing. Pods run
# as non-root with all capabilities dropped (no CAP_IPC_LOCK), so this rlimit is the only lever.
# Each pod's locked memory is still charged to, and bounded by, its own cgroup memory limit.
set -euo pipefail
mkdir -p /etc/systemd/system/k3s.service.d
cat > /etc/systemd/system/k3s.service.d/20-memlock.conf <<'CONF'
# Managed by labzilla/homelab/host/prep-memlock.sh: lets LIF's tier0 lock its model weights (mlock)
[Service]
LimitMEMLOCK=infinity
CONF
systemctl daemon-reload

echo "== Restarting k3s (workloads keep running; the API is briefly unavailable) =="
systemctl restart k3s
until k3s kubectl get --raw /readyz >/dev/null 2>&1; do sleep 2; done
echo "k3s ready. Existing pods keep the old limit. Apply tier0 (--load-mode mmap+mlock) to lock its weights:"
echo "  kubectl apply -f ~/labzilla/lif/deploy/k8s/serving/tier0.yaml"
echo "  check: grep -i locked /proc/\$(pgrep -f 'llama-server.*Qwen3-4B')/limits  → unlimited"
