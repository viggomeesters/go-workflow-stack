#!/usr/bin/env bash
# Deterministic source/packaged campaign gates. Live model calls are separate.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
python3 -m pytest tests/test_abc_contracts.py tests/test_abc_phase_contracts.py \
  tests/test_abc_models.py tests/test_abc_worktrees.py tests/test_abc_context.py \
  tests/test_abc_resume.py tests/test_abc_completion.py tests/test_abc_release.py \
  tests/test_abc_deployment.py tests/test_abc_migration.py \
  tests/test_abc_topologies.py tests/test_abc_end_to_end.py -q
python3 cli/go.py validate .
git diff --check
