#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export UV_TOOL_DIR="$WORK/tools"
export UV_TOOL_BIN_DIR="$WORK/bin"

uv tool install --from "$ROOT" go-workflow-stack >/dev/null
cd "$WORK"
"$UV_TOOL_BIN_DIR/go-workflow" version --json >version.json
"$UV_TOOL_BIN_DIR/go-workflow" init fixture
"$UV_TOOL_BIN_DIR/go-workflow" validate fixture
python3 - "$WORK/version.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
assert result["schema"] == "go-workflow.runtime-version.v1"
assert result["stack_version"] == "0.3.4"
PY
echo "standalone uv tool distribution: ok"
