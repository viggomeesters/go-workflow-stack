#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$ROOT/scripts/run-pilots.py" >/tmp/go-workflow-pilot-metrics.json
bash "$ROOT/scripts/check-distribution.sh"
echo "diverse local pilots: ok"
