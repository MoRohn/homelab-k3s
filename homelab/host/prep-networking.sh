#!/usr/bin/env bash
# One-time host prep for MetalLB. Run with sudo. Restarts k3s (~1 min).
# (Ran 2026-10-01. ufw is inactive on this host, so no firewall rules are needed.)
set -euo pipefail
# MetalLB replaces k3s's built-in ServiceLB (klipper-lb); the two can't run together.
mkdir -p /etc/rancher/k3s/config.yaml.d
cat > /etc/rancher/k3s/config.yaml.d/10-disable-servicelb.yaml <<'CONF'
# Managed by labzilla/homelab/host/prep-networking.sh — MetalLB provides LoadBalancer IPs
disable:
  - servicelb
CONF

echo "== Restarting k3s (workloads keep running; the API is briefly unavailable) =="
systemctl restart k3s
until k3s kubectl get --raw /readyz >/dev/null 2>&1; do sleep 2; done
echo "k3s ready"
k3s kubectl -n kube-system get pods -l svccontroller.k3s.cattle.io/svcname --no-headers 2>/dev/null \
  | wc -l | xargs -I{} echo "ServiceLB pods remaining: {} (they are removed on restart)"
