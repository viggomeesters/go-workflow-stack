"""Pure, reviewable migrations for repo-local `.go` contracts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .constants import CURRENT_CONTRACT_VERSION, STACK_REF

MIGRATION_PLAN_SCHEMA = "go-workflow.migration-plan.v1"


def plan_contract_migration(
    project: dict[str, Any], hierarchy: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return a non-mutating v1→current plan and the proposed documents."""
    migrated_project = deepcopy(project)
    migrated_hierarchy = deepcopy(hierarchy)
    from_version = int(project.get("contract_version") or 1)
    if from_version > CURRENT_CONTRACT_VERSION:
        raise ValueError(
            f"contract version {from_version} is newer than supported version {CURRENT_CONTRACT_VERSION}"
        )

    changes: list[dict[str, Any]] = []
    project_operations: list[str] = []
    defaults = {
        "contract_version": CURRENT_CONTRACT_VERSION,
        "project_mode": "project",
        "stack_ref": STACK_REF,
    }
    for key, value in defaults.items():
        if migrated_project.get(key) != value and key not in migrated_project:
            migrated_project[key] = value
            project_operations.append(f"add {key}={value!r}")
    if migrated_project.get("contract_version") != CURRENT_CONTRACT_VERSION:
        migrated_project["contract_version"] = CURRENT_CONTRACT_VERSION
        project_operations.append(f"set contract_version={CURRENT_CONTRACT_VERSION}")
    if project_operations:
        changes.append({"path": ".go/project.json", "operations": project_operations})

    if "epics" not in migrated_hierarchy and isinstance(migrated_hierarchy.get("feature_groups"), list):
        migrated_hierarchy["epics"] = migrated_hierarchy.pop("feature_groups")
        changes.append({"path": ".go/hierarchy.json", "operations": ["rename feature_groups to epics"]})

    plan = {
        "schema": MIGRATION_PLAN_SCHEMA,
        "from_version": from_version,
        "to_version": CURRENT_CONTRACT_VERSION,
        "changes": changes,
        "write_required": bool(changes),
        "applied": False,
    }
    return plan, {"project.json": migrated_project, "hierarchy.json": migrated_hierarchy}
