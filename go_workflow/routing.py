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
    explicit_read_only = bool(re.search(
        r"\b(alleen advies|read[- ]only|niet wegschrijven|geen wijzigingen|no writes)\b",
        text,
    ))
    explicit_stop = bool(
        re.search(r"\b(alleen plan|plan only|niet uitvoeren|do not execute)\b", text)
        or re.match(r"^(?:go\s+)?plan\b", text)
    )
    sent_as_goal = bool(re.match(r"^sent as goal\s*:", text))
    explicit_go = bool(re.match(r"^go(?:\s|:|$)", text))
    explicit_loop = bool(re.match(r"^(?:go\s+)?(?:loop|ralph)\b", text))
    named_task = bool(re.fullmatch(r"(?:go\s+)?[a-z]+\d+", text, flags=re.I))
    continuation = bool(re.match(r"^(?:ga verder|werk verder|continue|finish|next)\b", text))
    imperative = bool(re.match(
        r"^(?:fix|maak|verbeter|bouw|implementeer|voer|werk|ga|stel|schrijf|update|verwijder|"
        r"add|create|implement|build|execute|continue|finish)\b",
        text,
    ))
    question = bool(
        text.endswith("?")
        or re.match(r"^(?:hoe|wat|waarom|welke|kan|kunnen|zou|moeten|how|what|why|which|can|could|should|would)\b", text)
    )

    if explicit_read_only:
        authority = "read_only"
        authority_source = "explicit_read_only"
    elif sent_as_goal:
        authority = "execute"
        authority_source = "sent_as_goal"
    elif explicit_stop:
        authority = "advice"
        authority_source = "explicit_stop"
    elif explicit_go or explicit_loop or named_task or continuation or not text:
        authority = "execute"
        authority_source = "go" if explicit_go or not text else "task_id" if named_task else "continuation"
    elif imperative:
        authority = "execute"
        authority_source = "imperative"
    elif question:
        authority = "advice"
        authority_source = "question"
    else:
        authority = "advice"
        authority_source = "unconfirmed"

    stop_before_implementation = authority != "execute" or explicit_stop
    if "wayfinder" in text:
        route = "wayfinder"
        stop_before_implementation = True
    elif explicit_stop:
        route = "plan"
    elif any(word in text for word in ("loop", "ralph", "groen", "avondrun", "controle afgeven")):
        route = "loop"
    elif re.match(r"^(?:go\s+)?(?:task|backlog|leg vast)\b", text):
        route = "task"
        stop_before_implementation = True
    elif any(word in text for word in ("vision", "visie", "north star", "non-goal")):
        route = "vision"
        stop_before_implementation = True
    elif named_task:
        route = "goal"
    elif authority in {"advice", "read_only"}:
        route = "advice"
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
        "authority": authority,
        "authority_source": authority_source,
        "implementation_authorized": authority == "execute" and not stop_before_implementation,
        "planning_state_authorized": authority != "read_only",
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
            "authority": public["authority"],
            "authority_source": public["authority_source"],
            "implementation_authorized": public["implementation_authorized"],
            "planning_state_authorized": public["planning_state_authorized"],
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
    if public["route"] == "advice":
        return recommendation("recommendation", "public Go selected advice without implementation authority")
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
