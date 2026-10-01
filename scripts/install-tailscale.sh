#!/usr/bin/env bash
# Bring up Tailscale access end to end, verifying each step.
# Prereq: secrets/tailscale.env (git-ignored) with an OAuth client from the admin console:
#   TS_OAUTH_CLIENT_ID=...
#   TS_OAUTH_CLIENT_SECRET=tskey-client-...
set -euo pipefail
cd "$(dirname "$0")/.."
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f secrets/tailscale.env ]] || fail "secrets/tailscale.env not found (see docs/tailscale.md)"
source secrets/tailscale.env
[[ -n ${TS_OAUTH_CLIENT_ID:-} && ${TS_OAUTH_CLIENT_SECRET:-} == tskey-client-* ]] \
  || fail "secrets/tailscale.env needs TS_OAUTH_CLIENT_ID and TS_OAUTH_CLIENT_SECRET=tskey-client-..."

echo "1. Checking the OAuth client with Tailscale"
resp=$(curl -fsS https://api.tailscale.com/api/v2/oauth/token \
  -d "client_id=$TS_OAUTH_CLIENT_ID" -d "client_secret=$TS_OAUTH_CLIENT_SECRET" 2>&1) \
  || fail "Tailscale rejected the client ID/secret: $resp"
scopes=$(python3 -c 'import sys,json; print(json.load(sys.stdin).get("scope",""))' <<<"$resp")
for need in devices:core auth_keys; do
  [[ " $scopes " == *" $need"* || " $scopes " == *"$need "* || $scopes == *"$need"* ]] \
    || fail "OAuth client is missing the '$need' write scope (has: $scopes)"
done
ok "credentials valid; scopes: $scopes"

echo "2. Storing the credentials in the cluster"
kubectl create namespace tailscale --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n tailscale create secret generic operator-oauth \
  --from-literal=client_id="$TS_OAUTH_CLIENT_ID" \
  --from-literal=client_secret="$TS_OAUTH_CLIENT_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
ok "secret tailscale/operator-oauth"

echo "3. Handing the operator and ingresses to Argo CD"
kubectl apply -f networking/tailscale/apps/ >/dev/null
ok "Argo CD apps tailscale-operator, tailscale-ingresses"

echo "4. Waiting for the operator to join your tailnet (up to 5 min)"
for i in $(seq 1 60); do
  kubectl -n tailscale rollout status deploy/operator --timeout=5s >/dev/null 2>&1 && break; sleep 5
done
kubectl -n tailscale rollout status deploy/operator --timeout=5s >/dev/null 2>&1 \
  || { kubectl -n tailscale logs deploy/operator --tail=15 >&2 || true; fail "operator not ready (logs above)"; }
ok "operator running"

echo "5. Waiting for HTTPS hostnames (needs MagicDNS + HTTPS Certificates enabled)"
for ing in grafana prometheus; do
  host=""
  for i in $(seq 1 60); do
    host=$(kubectl -n monitoring get ingress "$ing" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
    [[ -n $host ]] && break; sleep 5
  done
  [[ -n $host ]] || fail "$ing has no tailnet hostname yet: check MagicDNS and HTTPS Certificates in the admin console DNS page"
  ok "https://$host"
done
echo
echo "Done. Open those URLs from any device signed in to your tailnet."
echo "The first HTTPS request can take ~30 s while Tailscale issues the certificate."
