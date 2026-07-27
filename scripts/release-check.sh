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
SOURCE_ROOT="$ROOT"
ARCHIVE_WORK=""
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
  if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
    echo "release worktree must be clean when validating tag v$VERSION" >&2
    exit 1
  fi
  ARCHIVE_WORK="$(mktemp -d)"
  trap 'rm -rf "$ARCHIVE_WORK"' EXIT
  git -C "$ROOT" archive "v$VERSION" | tar -x -C "$ARCHIVE_WORK"
  SOURCE_ROOT="$ARCHIVE_WORK"
  echo "tag: annotated v$VERSION points to HEAD; tests use its archived payload"
else
  if [ "${GO_RELEASE_ALLOW_CANDIDATE:-0}" != "1" ]; then
    echo "annotated tag v$VERSION is required; use GO_RELEASE_ALLOW_CANDIDATE=1 only for the pre-tag candidate gate" >&2
    exit 1
  fi
  echo "tag: v$VERSION candidate mode; final gate still requires the annotated tag"
fi

if [ "${GO_RELEASE_SKIP_TESTS:-0}" != "1" ]; then
  GO_PROJECT_TEMPLATE="${GO_PROJECT_TEMPLATE:-$ROOT/../go-project-template}" bash "$SOURCE_ROOT/scripts/check-linux.sh"
  bash "$SOURCE_ROOT/scripts/check-distribution.sh" "$SOURCE_ROOT"
fi

echo "publish: not performed"
