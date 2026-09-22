#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if "${PYTHON:-python3}" -c 'import sys, pytest, jsonschema; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
  RUNNER=("${PYTHON:-python3}" -m pytest)
elif command -v uv >/dev/null 2>&1; then
  RUNNER=(uv run --no-project --python '>=3.11' --with 'pytest>=8,<9' --with 'jsonschema>=4.23' python -m pytest)
else
  echo "autonomy gate requires Python 3.11+, pytest and jsonschema, or uv" >&2
  exit 2
fi

# Run outside an enclosing managed worktree. Every selected test corresponds to
# an injected behavior in fixtures/autonomy-campaign/coverage.json.
cd "${TMPDIR:-/tmp}"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT" "${RUNNER[@]}" \
  "$ROOT/tests/test_autonomy_end_to_end.py" \
  "$ROOT/tests/test_autonomy_recovery.py::test_repeated_identical_failure_records_no_progress" \
  "$ROOT/tests/test_autonomy_recovery.py::test_command_and_repair_budgets_are_cumulative_across_resume" \
  "$ROOT/tests/test_autonomy_recovery.py::test_dispatch_checkpoint_recovers_same_task_after_controller_death" \
  "$ROOT/tests/test_autonomy_recovery.py::test_live_foreign_controller_identity_refuses_takeover" \
  "$ROOT/tests/test_autonomy_outcomes.py::test_native_critic_cannot_report_success_without_grounded_review" \
  "$ROOT/tests/test_autonomy_delivery.py::test_history_preserving_reconciliation_updates_only_owned_workspace_and_invalidates_checks" \
  "$ROOT/tests/test_autonomy_delivery.py::test_malformed_effect_checkpoints_are_unsafe_not_success_or_absence" \
  "$ROOT/tests/test_abc_end_to_end.py::test_conflicting_base_after_green_preserves_both_sides_without_release" \
  "$ROOT/tests/test_abc_release.py::test_lost_command_acknowledgement_reconciles_without_duplicate_effect" \
  "$ROOT/tests/test_abc_release.py::test_github_create_lost_response_uses_readback_without_republication" \
  "$ROOT/tests/test_abc_deployment.py::test_wrong_or_uncertain_target_stays_pending_and_recovers_without_second_effect" \
  "$ROOT/tests/test_abc_resume.py::test_cleanup_retry_only_cleans_the_already_delivered_task" \
  "$ROOT/tests/test_abc_worktrees.py::test_cleanup_failure_has_recovery_without_republishing_and_preserves_ignored_files" -q

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT" "${PYTHON:-python3}" "$ROOT/cli/go.py" validate "$ROOT"
git -C "$ROOT" diff --check
echo "autonomy campaign gate: passed"
