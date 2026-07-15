"""Importable core for the repo-local Go workflow stack."""

from .constants import CURRENT_CONTRACT_VERSION, STACK_REF, STACK_VERSION
from .routing import normalize_router_command, recommend_route
from .task_state import open_task_records, task_path, unfinished_task_ids

__all__ = [
    "CURRENT_CONTRACT_VERSION",
    "STACK_REF",
    "STACK_VERSION",
    "normalize_router_command",
    "recommend_route",
    "open_task_records",
    "task_path",
    "unfinished_task_ids",
]
