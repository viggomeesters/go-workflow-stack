"""Agent capacity policy for repo-local go runs.

The policy is intentionally conservative: more agents are not more throughput
when their modify scopes overlap. Default to solo/lead+worker execution, allow
parallel builders only for provably disjoint scopes, and prefer a reviewer lane
for broad or risky work.
"""

from __future__ import annotations

import fnmatch
from typing import Any


BROAD_SCOPE_PATTERNS = {"**", "*", ".", "./", ".go/**", "docs/**"}


def normalize_scope(patterns: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for pattern in patterns or []:
        value = str(pattern).strip().replace("\\", "/")
        if value:
            normalized.append(value)
    return normalized


def _scope_prefix(pattern: str) -> str:
    value = pattern.strip().strip("/")
    for marker in ("/**", "/*", "*"):
        if marker in value:
            value = value.split(marker, 1)[0]
    return value.rstrip("/")


def scopes_overlap(left: list[str] | None, right: list[str] | None) -> bool:
    """Return whether two modify scopes can touch the same path.

    This is a safe approximation: broad glob patterns are treated as overlap,
    exact equal paths overlap, and parent/child prefixes overlap. False means
    the policy can treat the scopes as independent for capacity planning.
    """

    left_patterns = normalize_scope(left)
    right_patterns = normalize_scope(right)
    if not left_patterns or not right_patterns:
        return True
    for left_pattern in left_patterns:
        for right_pattern in right_patterns:
            if left_pattern in BROAD_SCOPE_PATTERNS or right_pattern in BROAD_SCOPE_PATTERNS:
                return True
            if left_pattern == right_pattern:
                return True
            left_prefix = _scope_prefix(left_pattern)
            right_prefix = _scope_prefix(right_pattern)
            if not left_prefix or not right_prefix:
                return True
            if left_prefix == right_prefix:
                return True
            if left_prefix.startswith(right_prefix + "/") or right_prefix.startswith(left_prefix + "/"):
                return True
            if fnmatch.fnmatch(left_prefix, right_pattern) or fnmatch.fnmatch(right_prefix, left_pattern):
                return True
    return False


def task_modify_scope(task: dict[str, Any]) -> list[str]:
    scope = task.get("scope") if isinstance(task, dict) else None
    if not isinstance(scope, dict):
        return []
    modify = scope.get("modify")
    if not isinstance(modify, list):
        return []
    return normalize_scope([str(item) for item in modify])


def plan_capacity(tasks: list[dict[str, Any]], requested_parallel_builders: int | None = None) -> dict[str, Any]:
    """Build the default capacity decision for a selected task batch."""

    if not tasks:
        return {
            "mode": "idle",
            "builder_slots": 0,
            "reviewer_lane": False,
            "parallel_builders_allowed": False,
            "reasons": ["no selected tasks"],
            "overlaps": [],
        }

    overlaps: list[dict[str, Any]] = []
    broad_or_empty_scope = False
    for index, task in enumerate(tasks):
        scope = task_modify_scope(task)
        if not scope or any(pattern in BROAD_SCOPE_PATTERNS for pattern in scope):
            broad_or_empty_scope = True
        for other in tasks[index + 1 :]:
            if scopes_overlap(scope, task_modify_scope(other)):
                overlaps.append({"left": task.get("id"), "right": other.get("id")})

    independent = not overlaps and not broad_or_empty_scope
    requested = max(int(requested_parallel_builders or 1), 1)
    if len(tasks) == 1:
        return {
            "mode": "solo",
            "builder_slots": 1,
            "reviewer_lane": True,
            "parallel_builders_allowed": False,
            "reasons": ["single selected task defaults to solo plus critic/reviewer lane"],
            "overlaps": overlaps,
        }
    if independent and requested > 1:
        return {
            "mode": "parallel-disjoint-builders",
            "builder_slots": min(requested, len(tasks)),
            "reviewer_lane": True,
            "parallel_builders_allowed": True,
            "reasons": ["selected task modify scopes are disjoint"],
            "overlaps": overlaps,
        }
    return {
        "mode": "serial-with-reviewer-lane",
        "builder_slots": 1,
        "reviewer_lane": True,
        "parallel_builders_allowed": False,
        "reasons": [
            "ambiguous, broad, or overlapping modify scopes stay serial",
            "use reviewer/critic lane before adding more builders",
        ],
        "overlaps": overlaps,
    }
