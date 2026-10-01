#!/usr/bin/env bash
# Local CA for LAN HTTPS (Traefik default certificate). Idempotent; never prints key material.
#   * CA: name-constrained (critical) to tiny-dgx.lan and labzilla.local, so even if its key leaked it
#     could not vouch for any other site on devices that trust it. 5-year validity.
#   * Server cert: every LAN ingress host, ≤ 397 days (Apple's limit), reissued when < 30 days remain
#     or when HOSTS changes.
#   * Installs the cert as kube-system/lan-default-tls, used by Traefik's TLSStore "default"
#     (tlsstore-default.yaml next to this script). Traefik reloads it without a restart.
# Files (git-ignored, 0600): secrets/labzilla-ca.{key,crt}, secrets/lan-tls.{key,crt}.
# Devices trust secrets/labzilla-ca.crt (public, safe to copy to phones): see lif/docs/CONSOLE.md → TLS.
set -euo pipefail
cd "${LABZILLA_DIR:-$(dirname "$(readlink -f "$0")")/../../..}"
S=secrets; mkdir -p "$S"; chmod 700 "$S"; umask 077
HOSTS=(ai.tiny-dgx.lan lif.tiny-dgx.lan labzilla.tiny-dgx.lan labzilla.local)
here=$(dirname "$(readlink -f "$0")")

if [ ! -s "$S/labzilla-ca.key" ]; then
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$S/labzilla-ca.key" 2>/dev/null
  openssl req -x509 -new -key "$S/labzilla-ca.key" -sha256 -days 1825 -subj "/CN=Labzilla Local CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "nameConstraints=critical,permitted;DNS:tiny-dgx.lan,permitted;DNS:labzilla.local" \
    -out "$S/labzilla-ca.crt"
  echo "created CA: $S/labzilla-ca.crt"
fi

want=$(printf 'DNS:%s, ' "${HOSTS[@]}"); want=${want%, }
have=$( [ -s "$S/lan-tls.crt" ] && openssl x509 -in "$S/lan-tls.crt" -noout -ext subjectAltName 2>/dev/null | tail -1 | xargs || true)
if [ ! -s "$S/lan-tls.crt" ] || [ "$have" != "$want" ] || ! openssl x509 -in "$S/lan-tls.crt" -noout -checkend $((30*86400)) >/dev/null; then
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$S/lan-tls.key" 2>/dev/null
  openssl req -new -key "$S/lan-tls.key" -subj "/CN=${HOSTS[0]}" -out "$tmp/csr"
  printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "$want" > "$tmp/ext"
  openssl x509 -req -in "$tmp/csr" -CA "$S/labzilla-ca.crt" -CAkey "$S/labzilla-ca.key" -CAcreateserial \
    -CAserial "$tmp/serial" -days 397 -sha256 -extfile "$tmp/ext" -out "$tmp/leaf" 2>/dev/null
  cat "$tmp/leaf" "$S/labzilla-ca.crt" > "$S/lan-tls.crt"     # chain: leaf + CA
  echo "issued server cert for: ${HOSTS[*]} (expires $(openssl x509 -in "$tmp/leaf" -noout -enddate | cut -d= -f2))"
fi

kubectl -n kube-system create secret tls lan-default-tls --cert="$S/lan-tls.crt" --key="$S/lan-tls.key" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -f "$here/tlsstore-default.yaml" >/dev/null
echo "applied kube-system/lan-default-tls and TLSStore default"
