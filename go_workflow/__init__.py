"""Importable core for the repo-local Go workflow stack."""

from .constants import CURRENT_CONTRACT_VERSION, STACK_REF, STACK_VERSION
from .routing import normalize_router_command, recommend_route
from .task_state import open_task_records, task_path, unfinished_task_ids
from .hermes_proof import LIVE_HERMES_PROOF_SCHEMA, validate_live_hermes_proof, verify_live_hermes_evidence

__all__ = [
    "CURRENT_CONTRACT_VERSION",
    "STACK_REF",
    "STACK_VERSION",
    "normalize_router_command",
    "recommend_route",
    "open_task_records",
    "task_path",
    "unfinished_task_ids",
    "LIVE_HERMES_PROOF_SCHEMA",
    "validate_live_hermes_proof",
    "verify_live_hermes_evidence",
]
