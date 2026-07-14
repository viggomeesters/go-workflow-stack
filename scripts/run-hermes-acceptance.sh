#!/usr/bin/env bash
set -euo pipefail

STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "${GO_RUN_REAL_HERMES_E2E:-}" != "1" ]; then
  echo "NOT PROVEN: set GO_RUN_REAL_HERMES_E2E=1 to authorize a live Hermes campaign" >&2
  exit 2
fi
if ! command -v hermes >/dev/null 2>&1; then
  echo "NOT PROVEN: hermes is not available on PATH" >&2
  exit 2
fi

WORK_ROOT="${GO_HERMES_E2E_ROOT:-$(mktemp -d)}"
REPO="$WORK_ROOT/hermes-go-campaign"
mkdir -p "$REPO"
git init -q -b main "$REPO"

python3 "$STACK_ROOT/cli/go.py" adopt "$REPO" \
  --project-id hermes-go-campaign \
  --name "Hermes Go Campaign" \
  --north-star "A live Hermes adapter completes and resumes a bounded two-task campaign" \
  --verification "test -f phase-one.txt && test -f phase-two.txt"
python3 "$STACK_ROOT/cli/go.py" task create "$REPO" \
  --id phase-one --summary "Create phase-one.txt containing phase one complete" --epic workflow \
  --execution-mode agent --modify phase-one.txt \
  --acceptance "phase-one.txt exists and contains phase one complete" \
  --verification "grep -q 'phase one complete' phase-one.txt"
python3 "$STACK_ROOT/cli/go.py" task create "$REPO" \
  --id phase-two --summary "Create phase-two.txt containing phase two complete" --epic workflow \
  --execution-mode agent --modify phase-two.txt \
  --acceptance "phase-two.txt exists and contains phase two complete" \
  --verification "grep -q 'phase two complete' phase-two.txt"

git -C "$REPO" add .
git -C "$REPO" -c user.name="Hermes Acceptance" -c user.email="hermes-acceptance@example.com" commit -m "seed live Hermes campaign" -q

export GO_STACK="$STACK_ROOT"
export GO_EXECUTOR_AGENT=hermes
python3 "$STACK_ROOT/cli/go.py" doctor "$REPO" --platform auto --agent hermes --json >"$WORK_ROOT/doctor.json"
python3 "$STACK_ROOT/cli/go.py" go-loop "$REPO" --max-tasks 1 --max-commands 24 --execute --agent hermes --ship-policy local-commit --json >"$WORK_ROOT/first.json"
python3 - "$WORK_ROOT/first.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
assert result["status"] == "budget_exhausted", result
assert result["completed_tasks"] == ["phase-one"], result
PY

(cd "$REPO" && bash .go/runs/resume.sh >"$WORK_ROOT/resumed.json")
python3 - "$WORK_ROOT/resumed.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
assert result["status"] == "done", result
assert result["completed_tasks"] == ["phase-two"], result
assert result["completion_audit"]["project_verification_passed"] is True, result
PY

echo "PROVEN: live Hermes build/critic/repair/resume campaign completed at $REPO"
