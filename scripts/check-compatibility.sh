#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if "${PYTHON:-python3}" -c 'import sys, pytest, jsonschema; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
  RUNNER=("${PYTHON:-python3}" -m pytest)
elif command -v uv >/dev/null 2>&1; then
  RUNNER=(uv run --no-project --python '>=3.11' --with 'pytest>=8,<9' --with 'jsonschema>=4.23' python -m pytest)
else
  echo "compatibility release gate requires Python 3.11+, pytest and jsonschema, or uv" >&2
  exit 2
fi

# Use a neutral cwd: tests exercise disposable controllers, never an enclosing
# managed source worktree. Keep this bounded phase out of its own outer tests.
cd "${TMPDIR:-/tmp}"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT" "${RUNNER[@]}" \
  "$ROOT/tests/test_legacy_lifecycle_compatibility.py" \
  "$ROOT/tests/test_abc_contracts.py" \
  "$ROOT/tests/test_abc_migration.py" \
  "$ROOT/tests/test_upgrade_preview.py" \
  "$ROOT/tests/test_toml_release.py" \
  "$ROOT/tests/test_guided_onboarding.py" \
  "$ROOT/tests/test_release_pairings.py" \
  "$ROOT/tests/test_completed_capture.py" \
  "$ROOT/tests/test_task_design_readiness.py" \
  "$ROOT/tests/test_architecture.py" -q
echo "compatibility release gate: passed"
