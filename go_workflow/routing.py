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


def classify_public_go_intent(intent: str, state: dict[str, Any]) -> dict[str, Any]:
    """Classify one public Go prompt without exposing CLI primitives."""
    text = (intent or "").strip().lower()
    stop_before_implementation = bool(
        re.search(r"\b(alleen plan|plan only|niet uitvoeren|do not execute)\b", text)
        or re.match(r"^(?:go\s+)?plan\b", text)
    )
    if "wayfinder" in text:
        route = "wayfinder"
    elif stop_before_implementation:
        route = "plan"
    elif any(word in text for word in ("loop", "ralph", "groen", "avondrun", "controle afgeven")):
        route = "loop"
    elif re.match(r"^(?:go\s+)?(?:task|backlog|leg vast)\b", text):
        route = "task"
    elif any(word in text for word in ("vision", "visie", "north star", "non-goal")):
        route = "vision"
    elif re.fullmatch(r"(?:go\s+)?[a-z]+\d+", text, flags=re.I):
        route = "goal"
    elif state.get("open_task_count", 0) > 0 or state.get("active_task_count", 0) > 0:
        route = "goal"
    elif text:
        route = "now"
    else:
        route = "task"
    return {
        "public_command": "go",
        "route": route,
        "stop_before_implementation": stop_before_implementation,
    }


def recommend_route(normalized: str, intent: str, state: dict[str, Any]) -> dict[str, Any]:
    intent = (intent or "").strip().lower()
    public = classify_public_go_intent(intent, state)
    if normalized == "go-loop":
        public.update({"route": "loop", "stop_before_implementation": False})

    def recommendation(command: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "command": command,
            "reason": reason,
            "public_command": "go",
            "selected_route": public["route"],
            "stop_before_implementation": public["stop_before_implementation"],
            **extra,
        }

    if normalized not in {"go", "go-loop"}:
        return recommendation("unknown", "command token is not a go/go-loop variant")
    if not state.get("repo_exists"):
        return recommendation("spike", "repo directory is missing", mode="create_repo", selected_route="spike")
    if not state.get("has_go"):
        return recommendation("spike", "repo exists but .go contract is missing", mode="repair_existing_repo", selected_route="spike")
    if not state.get("valid") or not all(state.get(key) for key in ("has_vision", "has_principles", "has_hierarchy")):
        return recommendation("spike", "repo-local contract is incomplete or invalid", selected_route="spike")
    if public["route"] in {"wayfinder", "vision", "plan"}:
        return recommendation(public["route"], f"public Go selected the non-executing {public['route']} route")
    if public["route"] == "task":
        return recommendation("task create", "public Go requested durable intake without execution")
    if state.get("open_task_count", 0) > 0 and normalized == "go-loop":
        return recommendation("go-loop", "explicit loop route and repo has open tasks")
    if state.get("open_task_count", 0) > 0 and public["route"] == "loop":
        return recommendation("go-loop", "repo is valid, has open tasks, and public Go selected a bounded loop")
    if state.get("open_task_count", 0) > 0:
        return recommendation("auto", "repo is valid and has open tasks")
    return recommendation("task create", "repo is valid but has no open tasks; materialize the public Go intent first")


def detected_platform(requested: str) -> dict[str, Any]:
    if requested != "auto":
        return {"kind": requested, "detected": False}
    proc_version = Path("/proc/version")
    kernel = proc_version.read_text(encoding="utf-8", errors="ignore").lower() if proc_version.is_file() else ""
    is_wsl = bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in kernel
    kind = "wsl" if is_wsl else "linux" if sys.platform.startswith("linux") else sys.platform
    return {"kind": kind, "detected": True}
