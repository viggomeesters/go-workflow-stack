#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  VERSION="$(sed -n 's/^version = "\([0-9][0-9.]*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
fi

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "release version must be X.Y.Z" >&2
  exit 2
fi

python3 - "$ROOT" "$VERSION" <<'PY'
import json
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
cli = (root / "cli" / "go.py").read_text(encoding="utf-8")
pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
project = json.loads((root / ".go" / "project.json").read_text(encoding="utf-8"))

values = {
    "cli STACK_VERSION": re.search(r'^STACK_VERSION = "([^"]+)"', cli, re.M).group(1),
    "pyproject version": re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1),
    ".go required_stack_version": project.get("required_stack_version"),
}
for label, value in values.items():
    if value != expected:
        raise SystemExit(f"{label} is {value!r}, expected {expected!r}")
if project.get("stack_ref") != f"v{expected}":
    raise SystemExit(f".go stack_ref is {project.get('stack_ref')!r}, expected 'v{expected}'")
PY

echo "release preflight: v$VERSION"
if git -C "$ROOT" rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then
  echo "tag: v$VERSION already exists locally"
else
  echo "tag: v$VERSION is ready to create after review"
fi

if [ "${GO_RELEASE_SKIP_TESTS:-0}" != "1" ]; then
  bash "$ROOT/scripts/check-linux.sh"
fi

echo "publish: not performed"
