#!/usr/bin/env bash
# Create/refresh LIF Kubernetes Secrets. Idempotent; never prints secret values.
#   * Jev key:        TYPE_SAFE_JEV_API_KEY from labzilla/.env.local (git-ignored)
#   * gpusched token: the read-only `metrics` line of ~/.config/bnn/gpusched.token
#   * gateway/admin keys: generated once into labzilla/secrets/lif-*.key (git-ignored, 0600)
set -euo pipefail
# secrets/ and .env.local live, git-ignored, at the labzilla repo root (two levels up).
cd "${LABZILLA_DIR:-$(dirname "$(readlink -f "$0")")/../..}"
SECRETS=secrets; mkdir -p "$SECRETS"; chmod 700 "$SECRETS"
gen() { local f="$SECRETS/lif-$1.key"; [ -s "$f" ] || { umask 077; python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > "$f"; }; cat "$f"; }

# Let the shell parse .env.local (handles quoting) — never grep/cut a dotenv file.
JEV=$(set -a; . ./.env.local >/dev/null 2>&1; printf '%s' "${TYPE_SAFE_JEV_API_KEY:-}")
[ -n "$JEV" ] || { echo "TYPE_SAFE_JEV_API_KEY missing from .env.local" >&2; exit 1; }
MTOK=$(awk -F: '$1=="metrics"{print $2}' ~/.config/bnn/gpusched.token)
[ -n "$MTOK" ] || { echo "no metrics token in gpusched.token" >&2; exit 1; }

K_PROBE=$(gen probe); K_BATCH=$(gen batch); K_DEC=$(gen decision); K_BNN=$(gen bnn); K_OPS=$(gen operator)
K_ADMIN=$(gen admin); K_INTERNAL=$(gen internal); K_ENGINE=$(gen engine)   # engine = BNN vLLM LIF door

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT; umask 077
printf '%s' "$JEV" > "$tmp/TYPE_SAFE_JEV_API_KEY"
printf 'controller-probe:%s\nbatch:%s\ndecision-fabric:%s\nbnn:%s\noperator:%s\n' \
  "$K_PROBE" "$K_BATCH" "$K_DEC" "$K_BNN" "$K_OPS" > "$tmp/LIF_GATEWAY_KEYS"
printf 'operator:%s\n' "$K_ADMIN" > "$tmp/LIF_ADMIN_KEYS"
printf '%s' "$K_PROBE" > "$tmp/LIF_PROBE_KEY"
printf '%s' "$K_BATCH" > "$tmp/LIF_BATCH_GATEWAY_KEY"
printf '%s' "$K_DEC" > "$tmp/LIF_DECISION_GATEWAY_KEY"
printf '%s' "$K_INTERNAL" > "$tmp/LIF_INTERNAL_KEY"
printf '%s' "$K_ENGINE" > "$tmp/LIF_ENGINE_KEY"
printf '%s' "$MTOK" > "$tmp/token"

kubectl -n ai-system create secret generic lif-secrets --from-file="$tmp/TYPE_SAFE_JEV_API_KEY" \
  --from-file="$tmp/LIF_GATEWAY_KEYS" --from-file="$tmp/LIF_ADMIN_KEYS" --from-file="$tmp/LIF_PROBE_KEY" \
  --from-file="$tmp/LIF_BATCH_GATEWAY_KEY" --from-file="$tmp/LIF_DECISION_GATEWAY_KEY" \
  --from-file="$tmp/LIF_INTERNAL_KEY" --from-file="$tmp/LIF_ENGINE_KEY" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n ai-system create secret generic lif-gpusched --from-file="$tmp/token" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "secrets applied: ai-system/lif-secrets, ai-system/lif-gpusched"
echo "keys on disk: $SECRETS/lif-{probe,batch,decision,bnn,operator,admin}.key (BNN uses lif-bnn.key; the UI/CLI use lif-admin.key)"
