#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: scripts/apply-template.sh <target-repo> [--force]" >&2
  exit 2
fi

TARGET="$1"
FORCE="${2:-}"
STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_DIR="${GO_PROJECT_TEMPLATE:-$HOME/github/go-project-template}"

if [[ ! -d "$TARGET" ]]; then
  echo "target repo does not exist: $TARGET" >&2
  exit 1
fi
if [[ ! -d "$TEMPLATE_DIR/.go" ]]; then
  echo "template .go directory not found: $TEMPLATE_DIR/.go" >&2
  exit 1
fi
if [[ -e "$TARGET/.go" && "$FORCE" != "--force" ]]; then
  echo "refusing to overwrite existing $TARGET/.go; pass --force" >&2
  exit 1
fi

mkdir -p "$TARGET"
rm -rf "$TARGET/.go"
python3 "$STACK_DIR/cli/go.py" template-check "$TEMPLATE_DIR" >/dev/null
python3 "$STACK_DIR/cli/go.py" spike "$TARGET" \
  --brief "${GO_PROJECT_BRIEF:-Initialize $(basename "$TARGET") from the public go project template.}"
python3 "$STACK_DIR/cli/go.py" readback "$TARGET"

echo "applied and customized go-project-template contract in $TARGET"
