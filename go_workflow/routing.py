"""Pure command routing decisions used by the CLI facade."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any


def normalize_router_command(raw_command: str) -> str:
    token = (raw_command or "go").strip().lower()
    if re.fullmatch(r"go+", token, flags=re.I):
        return "go"
    if token in {"go-loop", "goloop", "loop"}:
        return "go-loop"
    return token


def recommend_route(normalized: str, intent: str, state: dict[str, Any]) -> dict[str, str]:
    intent = (intent or "").strip().lower()
    if normalized not in {"go", "go-loop"}:
        return {"command": "unknown", "reason": "command token is not a go/go-loop variant"}
    if not state.get("repo_exists"):
        return {"command": "spike", "mode": "create_repo", "reason": "repo directory is missing"}
    if not state.get("has_go"):
        return {"command": "spike", "mode": "repair_existing_repo", "reason": "repo exists but .go contract is missing"}
    if not state.get("valid") or not all(state.get(key) for key in ("has_vision", "has_principles", "has_hierarchy")):
        return {"command": "spike", "reason": "repo-local contract is incomplete or invalid"}
    if state.get("open_task_count", 0) > 0 and normalized == "go-loop":
        return {"command": "go-loop", "reason": "explicit go-loop command and repo has open tasks"}
    if state.get("open_task_count", 0) > 0 and any(word in intent for word in ("loop", "ralph", "groen", "avondrun", "controle afgeven")):
        return {"command": "go-loop", "reason": "repo is valid, has open tasks, and intent asks for full control handoff/loop"}
    if state.get("open_task_count", 0) > 0:
        return {"command": "auto", "reason": "repo is valid and has open tasks"}
    return {"command": "task create", "reason": "repo is valid but has no open tasks; convert feedback into tasks"}


def detected_platform(requested: str) -> dict[str, Any]:
    if requested != "auto":
        return {"kind": requested, "detected": False}
    proc_version = Path("/proc/version")
    kernel = proc_version.read_text(encoding="utf-8", errors="ignore").lower() if proc_version.is_file() else ""
    is_wsl = bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in kernel
    kind = "wsl" if is_wsl else "linux" if sys.platform.startswith("linux") else sys.platform
    return {"kind": kind, "detected": True}
