#!/usr/bin/env bash
# One-time setup for a fresh labzilla clone. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

# 1. Pre-commit guard (core.hooksPath is per-clone, so every clone runs this once)
git config core.hooksPath .githooks
chmod +x .githooks/* tools/*.py tools/*.sh
echo "✓ pre-commit guard enabled (.githooks/pre-commit → tools/check-private.py)"

# 2. Git-ignored local roots, each with its tracked README
mkdir -p secrets personal private && chmod 700 secrets
echo "✓ secrets/ personal/ private/ present (git-ignored)"

# 3. LIF dev environment (optional: skip with --no-venv)
if [[ "${1:-}" != "--no-venv" ]]; then
  python3 -m venv lif/.venv
  lif/.venv/bin/pip install -q -e "lif[test]"
  echo "✓ lif/.venv ready (local-ai CLI: lif/.venv/bin/local-ai)"
fi

# 4. The `labzilla` command (quick start + status + trust) on PATH
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/tools/labzilla" "$HOME/.local/bin/labzilla"
echo "✓ labzilla command installed (~/.local/bin/labzilla → tools/labzilla); run: labzilla"

python3 tools/check-private.py --all
