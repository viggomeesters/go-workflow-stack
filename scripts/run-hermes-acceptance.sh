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
HERMES_BIN="$(python3 -c 'import os,shutil; print(os.path.realpath(shutil.which("hermes")))')"
if [ ! -f "$HERMES_BIN" ] || [ ! -x "$HERMES_BIN" ]; then
  echo "NOT PROVEN: resolved Hermes binary is not an executable file: $HERMES_BIN" >&2
  exit 2
fi

WORK_ROOT="${GO_HERMES_E2E_ROOT:-$(mktemp -d)}"
REPO="$WORK_ROOT/hermes-go-campaign"
mkdir -p "$REPO"
if ! hermes --version >"$WORK_ROOT/hermes-version.txt" 2>&1 || [ ! -s "$WORK_ROOT/hermes-version.txt" ]; then
  echo "NOT PROVEN: Hermes did not provide executable version evidence" >&2
  exit 2
fi
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
export GO_STACK_ALLOW_DEV=1
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

python3 - "$WORK_ROOT" "$REPO" "$HERMES_BIN" <<'PY'
import hashlib
import json
import pathlib
import sys
from datetime import datetime, timezone

work = pathlib.Path(sys.argv[1])
repo = pathlib.Path(sys.argv[2])
binary = pathlib.Path(sys.argv[3])
protocol = []
for name in ("first.json", "resumed.json"):
    path = work / name
    result = json.loads(path.read_text(encoding="utf-8"))
    for attempt in result.get("attempts", []):
        for phase in ("build", "critic", "repair"):
            phase_result = attempt.get(phase, {}).get("result")
            if phase_result is None:
                continue
            assert phase_result.get("schema") == "go-workflow.agent-adapter-result.v1", (name, phase, phase_result)
            assert phase_result.get("phase") == phase, (name, phase, phase_result)
            protocol.append({
                "result_file": name,
                "task_id": attempt.get("task_id"),
                "attempt": attempt.get("attempt"),
                "phase": phase,
                "status": phase_result.get("status"),
                "summary": phase_result.get("summary"),
            })
assert protocol, "no native Hermes protocol results were recorded"
proof = {
    "schema": "go-workflow.live-hermes-proof.v1",
    "status": "proven",
    "created_at": datetime.now(timezone.utc).isoformat(),
    "binary": str(binary),
    "binary_version": (work / "hermes-version.txt").read_text(encoding="utf-8", errors="replace").strip()[:500],
    "repo": str(repo),
    "completed_tasks": ["phase-one", "phase-two"],
    "protocol_results": protocol,
    "result_sha256": {
        name: hashlib.sha256((work / name).read_bytes()).hexdigest()
        for name in ("doctor.json", "first.json", "resumed.json")
    },
}
(work / "proof.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
PY

test -s "$WORK_ROOT/proof.json"
python3 "$STACK_ROOT/cli/go.py" proof validate "$WORK_ROOT/proof.json" --evidence-root "$WORK_ROOT" --json >"$WORK_ROOT/proof-validation.json"
echo "PROVEN: live Hermes build/critic/repair/resume campaign completed; evidence: $WORK_ROOT/proof.json"
