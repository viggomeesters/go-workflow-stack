#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STACK="${GO_STACK:-}"
if [ -z "$STACK" ] || [ ! -f "$STACK/cli/go.py" ]; then
  for candidate in "$REPO_ROOT/../go-workflow-stack" "$HOME/github/go-workflow-stack" "$HOME/Dev/go-workflow-stack"; do
    if [ -f "$candidate/cli/go.py" ]; then STACK="$candidate"; break; fi
  done
fi
if [ -z "$STACK" ] || [ ! -f "$STACK/cli/go.py" ]; then
  echo "go-workflow-stack not found; set GO_STACK or clone it beside this repository" >&2
  exit 2
fi
cd "$REPO_ROOT"
exec python3 "$STACK/cli/go.py" go-auto . --execute --max-tasks 1 --summary-chars 900 --max-minutes 90 --max-commands 30 --command-timeout-seconds 3600 --max-attempts 3 --checkpoint-every-tasks 1 --agent codex-astra-high --executor-agent codex --ship-policy local-commit --semantic-critic --json
