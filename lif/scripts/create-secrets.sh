#!/usr/bin/env bash
# Create/refresh LIF Kubernetes Secrets. Idempotent; never prints secret values.
#   * Jev key:        TYPE_SAFE_JEV_API_KEY from labzilla/.env.local (git-ignored)
#   * gpusched token: the read-only `metrics` line of ~/.config/bnn/gpusched.token
#   * gateway/admin keys: generated once into labzilla/secrets/lif-*.key (git-ignored, 0600)
#   * console setup code: generated once into labzilla/secrets/lif-console-setup.code; the owner
#     reads it on the host to create the first console admin (docs/CONSOLE.md → Deploy & access)
set -euo pipefail
# secrets/ and .env.local live, git-ignored, at the labzilla repo root (two levels up).
cd "${LABZILLA_DIR:-$(dirname "$(readlink -f "$0")")/../..}"
SECRETS=secrets; mkdir -p "$SECRETS"; chmod 700 "$SECRETS"
gen() { local f="$SECRETS/lif-$1.key"; [ -s "$f" ] || { umask 077; python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > "$f"; }; cat "$f"; }
# Typed by a person once, so: 4×4 characters without look-alikes (no 0/O, 1/I/L), ~80 bits.
gen_code() { local f="$SECRETS/lif-$1.code"; [ -s "$f" ] || { umask 077; python3 -c '
import secrets; a = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
print("-".join("".join(secrets.choice(a) for _ in range(4)) for _ in range(4)))' > "$f"; }; cat "$f"; }

# Let the shell parse .env.local (handles quoting) — never grep/cut a dotenv file.
JEV=$(set -a; . ./.env.local >/dev/null 2>&1; printf '%s' "${TYPE_SAFE_JEV_API_KEY:-}")
[ -n "$JEV" ] || { echo "TYPE_SAFE_JEV_API_KEY missing from .env.local" >&2; exit 1; }
MTOK=$(awk -F: '$1=="metrics"{print $2}' ~/.config/bnn/gpusched.token)
[ -n "$MTOK" ] || { echo "no metrics token in gpusched.token" >&2; exit 1; }

K_PROBE=$(gen probe); K_BATCH=$(gen batch); K_DEC=$(gen decision); K_BNN=$(gen bnn); K_OPS=$(gen operator)
K_ADMIN=$(gen admin); K_INTERNAL=$(gen internal); K_ENGINE=$(gen engine)   # engine = primary-workload vLLM LIF door
# Labzilla Console: its own controller admin key and gateway client key (audited as `console`).
K_CON_ADMIN=$(gen console-admin); K_CON_GW=$(gen console-gateway); C_SETUP=$(gen_code console-setup)

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT; umask 077
printf '%s' "$JEV" > "$tmp/TYPE_SAFE_JEV_API_KEY"
# Optional heavy escalation (Kimi K3). Absent → the provider stays disabled; privacy policy
# (lif.yaml privacy.external_llm_allowed) still decides what may be sent even with a key.
MOONSHOT=$(set -a; . ./.env.local >/dev/null 2>&1; printf '%s' "${MOONSHOT_API_KEY:-}")
EXTRA=()
if [ -n "$MOONSHOT" ]; then printf '%s' "$MOONSHOT" > "$tmp/MOONSHOT_API_KEY"; EXTRA+=(--from-file="$tmp/MOONSHOT_API_KEY"); fi
printf 'controller-probe:%s\nbatch:%s\ndecision-fabric:%s\nbnn:%s\noperator:%s\nconsole:%s\n' \
  "$K_PROBE" "$K_BATCH" "$K_DEC" "$K_BNN" "$K_OPS" "$K_CON_GW" > "$tmp/LIF_GATEWAY_KEYS"
printf 'operator:%s\nconsole:%s\n' "$K_ADMIN" "$K_CON_ADMIN" > "$tmp/LIF_ADMIN_KEYS"
printf '%s' "$K_PROBE" > "$tmp/LIF_PROBE_KEY"
printf '%s' "$K_BATCH" > "$tmp/LIF_BATCH_GATEWAY_KEY"
printf '%s' "$K_DEC" > "$tmp/LIF_DECISION_GATEWAY_KEY"
printf '%s' "$K_INTERNAL" > "$tmp/LIF_INTERNAL_KEY"
printf '%s' "$K_ENGINE" > "$tmp/LIF_ENGINE_KEY"
printf '%s' "$K_CON_ADMIN" > "$tmp/LIF_CONSOLE_ADMIN_KEY"
printf '%s' "$K_CON_GW" > "$tmp/LIF_CONSOLE_GATEWAY_KEY"
printf '%s' "$C_SETUP" > "$tmp/LIF_CONSOLE_SETUP_CODE"
printf '%s' "$MTOK" > "$tmp/token"

kubectl -n ai-system create secret generic lif-secrets --from-file="$tmp/TYPE_SAFE_JEV_API_KEY" \
  --from-file="$tmp/LIF_GATEWAY_KEYS" --from-file="$tmp/LIF_ADMIN_KEYS" --from-file="$tmp/LIF_PROBE_KEY" \
  --from-file="$tmp/LIF_BATCH_GATEWAY_KEY" --from-file="$tmp/LIF_DECISION_GATEWAY_KEY" \
  --from-file="$tmp/LIF_INTERNAL_KEY" --from-file="$tmp/LIF_ENGINE_KEY" \
  --from-file="$tmp/LIF_CONSOLE_ADMIN_KEY" --from-file="$tmp/LIF_CONSOLE_GATEWAY_KEY" \
  --from-file="$tmp/LIF_CONSOLE_SETUP_CODE" "${EXTRA[@]}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n ai-system create secret generic lif-gpusched --from-file="$tmp/token" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "secrets applied: ai-system/lif-secrets, ai-system/lif-gpusched"
echo "keys on disk: $SECRETS/lif-{probe,batch,decision,bnn,operator,admin,console-admin,console-gateway}.key (the primary workload uses lif-bnn.key; the UI/CLI use lif-admin.key)"
echo "console setup code: $SECRETS/lif-console-setup.code (read it on the host when creating the first console admin)"
echo "gateway and controller read their key lists only at startup: roll them out so the console keys work:"
echo "  kubectl -n ai-system rollout restart deploy/gateway deploy/controller"
