#!/usr/bin/env bash
# Run the Labzilla console against fake upstreams: no cluster, GPU, secrets or inference needed.
#   fake upstreams  http://127.0.0.1:8091  (controller + gateway + batch + Prometheus, see fake_upstreams.py)
#   console         http://127.0.0.1:8090  (lif.console.app, fresh SQLite DB in a temp dir)
# Usage: lif/apps/console/e2e/run_dev.sh [healthy|fallback|blerbz-busy|offline]
# Switch scenario while running: curl -XPOST localhost:8091/__scenario -H 'content-type: application/json' -d '{"name":"fallback"}'
# Dev only: plain http, insecure cookies, a known setup code. Never point this at real services.
set -euo pipefail

E2E="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIF="$(cd "$E2E/../../.." && pwd)"                 # …/labzilla/lif
PY="$LIF/.venv/bin"
FAKE_PORT="${FAKE_PORT:-8091}"
CONSOLE_PORT="${CONSOLE_PORT:-8090}"
SCENARIO="${1:-${FAKE_SCENARIO:-healthy}}"
FAKE="http://127.0.0.1:$FAKE_PORT"

[ -x "$PY/uvicorn" ] || { echo "no venv at $LIF/.venv: run tools/setup.sh first" >&2; exit 1; }
case "$SCENARIO" in healthy|fallback|blerbz-busy|offline) ;; *) echo "unknown scenario: $SCENARIO" >&2; exit 2;; esac
for port in "$FAKE_PORT" "$CONSOLE_PORT"; do
  # never kill whatever already listens there: it may be someone else's process
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port is already in use; stop that process or set FAKE_PORT/CONSOLE_PORT" >&2; exit 1
  fi
done

TMP="$(mktemp -d -t labzilla-console-dev.XXXXXX)"
mkdir -p "$TMP/secrets"                             # empty: secrets come from the env below
PIDS=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

wait_for() {   # url, name, pid
  for _ in $(seq 1 100); do
    curl -sf -o /dev/null "$1" && return 0
    kill -0 "$3" 2>/dev/null || { echo "$2 exited during startup (log above)" >&2; return 1; }
    sleep 0.2
  done
  echo "$2 did not answer at $1" >&2; return 1
}

FAKE_SCENARIO="$SCENARIO" "$PY/uvicorn" --app-dir "$E2E" fake_upstreams:app \
  --host 127.0.0.1 --port "$FAKE_PORT" --log-level warning &
PIDS+=("$!")
wait_for "$FAKE/__scenario" "fake upstreams" "${PIDS[-1]}"

(
  cd "$LIF"
  unset LIF_KNOWLEDGE_ROOT LIF_KNOWLEDGE_URL       # bundled read-only knowledge (default root)
  export LIF_CONTROLLER_URL="$FAKE" LIF_GATEWAY_URL="$FAKE" LIF_BATCH_URL="$FAKE" LIF_PROMETHEUS_URL="$FAKE"
  export LIF_CONSOLE_DB="$TMP/console.db" LIF_CONSOLE_INSECURE_COOKIES=1 LIF_SECRETS_DIR="$TMP/secrets"
  export LIF_CONSOLE_SETUP_CODE=dev-setup-code LIF_CONSOLE_ADMIN_KEY=dev LIF_CONSOLE_GATEWAY_KEY=dev
  exec "$PY/uvicorn" lif.console.app:app --host 127.0.0.1 --port "$CONSOLE_PORT" --log-level info
) &
PIDS+=("$!")
wait_for "http://127.0.0.1:$CONSOLE_PORT/healthz" "console" "${PIDS[-1]}"

cat <<EOF

  Labzilla console   http://127.0.0.1:$CONSOLE_PORT   (first run: /setup, setup code: dev-setup-code)
  fake upstreams     $FAKE   scenario: $SCENARIO
  UI hot reload      cd lif/apps/console && npm run dev   (vite proxies /api to :$CONSOLE_PORT)
  switch scenario    curl -XPOST $FAKE/__scenario -H 'content-type: application/json' -d '{"name":"fallback"}'
  temp data          $TMP (deleted on exit)

  Ctrl+C stops both.
EOF
wait -n "${PIDS[@]}"
