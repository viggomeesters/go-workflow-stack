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
constants = (root / "go_workflow" / "constants.py").read_text(encoding="utf-8")
pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
project = json.loads((root / ".go" / "project.json").read_text(encoding="utf-8"))

values = {
    "runtime STACK_VERSION": re.search(r'^STACK_VERSION = "([^"]+)"', constants, re.M).group(1),
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
  tag_type="$(git -C "$ROOT" cat-file -t "refs/tags/v$VERSION")"
  if [ "$tag_type" != "tag" ]; then
    echo "tag v$VERSION must be annotated, found $tag_type" >&2
    exit 1
  fi
  tag_commit="$(git -C "$ROOT" rev-list -n 1 "v$VERSION")"
  head_commit="$(git -C "$ROOT" rev-parse HEAD)"
  if [ "$tag_commit" != "$head_commit" ]; then
    echo "tag v$VERSION does not point to HEAD" >&2
    exit 1
  fi
  echo "tag: annotated v$VERSION points to HEAD"
else
  echo "tag: v$VERSION is ready to create after review"
fi

if [ "${GO_RELEASE_SKIP_TESTS:-0}" != "1" ]; then
  bash "$ROOT/scripts/check-linux.sh"
fi

echo "publish: not performed"
