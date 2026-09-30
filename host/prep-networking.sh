#!/usr/bin/env bash
# One-time host prep for MetalLB + monitoring. Run with sudo. Restarts k3s (~1 min).
set -euo pipefail

echo "== Firewall (ufw) before changes =="
ufw status verbose

# Pods (10.42.0.0/16) scraping node-exporter on the host. Without this, scrapes of
# :9100 time out intermittently while the kubelet (:10250) is unaffected.
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
