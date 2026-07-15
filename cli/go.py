#!/usr/bin/env python3
"""Backward-compatible source-checkout launcher for the packaged CLI."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from go_workflow.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
