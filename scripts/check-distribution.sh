#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ROOT="$(cd "$ROOT" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export UV_TOOL_DIR="$WORK/tools"
export UV_TOOL_BIN_DIR="$WORK/bin"

uv tool install --reinstall --from "$ROOT" go-workflow-stack >/dev/null
cd "$WORK"
"$UV_TOOL_BIN_DIR/go-workflow" version --json >version.json
"$UV_TOOL_BIN_DIR/go-workflow" init fixture
"$UV_TOOL_BIN_DIR/go-workflow" validate fixture
python3 - "$WORK/version.json" "$ROOT/pyproject.toml" <<'PY'
import json, sys, tomllib
result = json.load(open(sys.argv[1], encoding="utf-8"))
project = tomllib.load(open(sys.argv[2], "rb"))
assert result["schema"] == "go-workflow.runtime-version.v1"
assert result["stack_version"] == project["project"]["version"]
PY
echo "standalone uv tool distribution: ok"
