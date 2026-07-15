#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${GO_PROJECT_TEMPLATE:-$ROOT/../go-project-template}"

if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  PYTHON=(python3)
  PYTEST=(python3 -m pytest)
elif command -v uv >/dev/null 2>&1; then
  PYTHON=(uv run --python '>=3.11' --no-project python)
  PYTEST=(uv run --python '>=3.11' --no-project --with pytest pytest)
else
  echo "Python 3.11+ required; install Python 3.11+ or uv." >&2
  exit 2
fi

VERSION="$("${PYTHON[@]}" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
echo "local Linux/WSL verification with Python $VERSION"

cd "$ROOT"
"${PYTHON[@]}" -m py_compile cli/go.py
"${PYTEST[@]}" tests/test_smoke.py -q
"${PYTHON[@]}" cli/go.py validate .

if [ -d "$TEMPLATE/.go" ]; then
  "${PYTHON[@]}" cli/go.py template-check "$TEMPLATE" --json >/tmp/go-template-check-local.json
  echo "template pairing: ok"
else
  echo "template pairing: skipped (set GO_PROJECT_TEMPLATE to its checkout)"
fi

echo "local Linux/WSL verification: ok"
