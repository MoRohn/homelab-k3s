#!/usr/bin/env bash
# One-time host prep for MetalLB and node-exporter. Run with sudo. Restarts k3s (~1 min).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
DIAG="$REPO/personal/firewall-diagnostics.txt"

# Diagnostics (git-ignored): from pods, :9100 drops ~20% of connections while :10250
# on the same address never does, so something in netfilter treats the ports differently.
{
  echo "== ufw status verbose =="; ufw status verbose
  echo; echo "== nftables rules mentioning 9100 / 10250 / 10.42 =="
  nft list ruleset 2>/dev/null | grep -nE '9100|10250|10\.42\.' || true
  echo; echo "== INPUT chain (iptables-legacy view) =="; iptables -S INPUT 2>/dev/null | head -40 || true
  echo; echo "== conntrack stats =="; cat /proc/sys/net/netfilter/nf_conntrack_count /proc/sys/net/netfilter/nf_conntrack_max
} > "$DIAG" 2>&1
chown "${SUDO_USER:-root}": "$DIAG"
echo "Diagnostics written to $DIAG"

# Explicitly allow pods (10.42.0.0/16) to reach node-exporter, as port 10250 evidently is
ufw allow from 10.42.0.0/16 to any port 9100 proto tcp comment 'k3s pods -> node-exporter'

# MetalLB replaces k3s's built-in ServiceLB (klipper-lb); the two can't run together.
mkdir -p /etc/rancher/k3s/config.yaml.d
cat > /etc/rancher/k3s/config.yaml.d/10-disable-servicelb.yaml <<'CONF'
# Managed by homelab-k3s/host/prep-networking.sh — MetalLB provides LoadBalancer IPs
disable:
  - servicelb
CONF

echo "== Restarting k3s (workloads keep running; the API is briefly unavailable) =="
systemctl restart k3s
until k3s kubectl get --raw /readyz >/dev/null 2>&1; do sleep 2; done
echo "k3s ready"
k3s kubectl -n kube-system get pods -l svccontroller.k3s.cattle.io/svcname --no-headers 2>/dev/null \
  | wc -l | xargs -I{} echo "ServiceLB pods remaining: {} (they are removed on restart)"
echo "== Firewall after =="
ufw status numbered
