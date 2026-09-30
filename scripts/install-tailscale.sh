#!/usr/bin/env bash
# Create the Tailscale operator's OAuth secret from secrets/tailscale.env (git-ignored).
#   TS_OAUTH_CLIENT_ID=...
#   TS_OAUTH_CLIENT_SECRET=tskey-client-...
set -euo pipefail
cd "$(dirname "$0")/.."
source secrets/tailscale.env
kubectl create namespace tailscale --dry-run=client -o yaml | kubectl apply -f -
kubectl -n tailscale create secret generic operator-oauth \
  --from-literal=client_id="$TS_OAUTH_CLIENT_ID" \
  --from-literal=client_secret="$TS_OAUTH_CLIENT_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -
