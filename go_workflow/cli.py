#!/usr/bin/env python3
"""Repo-local Go Workflow Stack vNext spike CLI.

Operates project-local `.go/` JSON/JSONL state. The CLI is intentionally
clone-local: execution commands read/write the target repo's `.go/` directory,
not the Life OS vault's Agent Workflow Lite task queue.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import html
import io
import json
import os
import re
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from copy import deepcopy

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_ROOT = SCRIPT_DIR.parent
if str(STACK_ROOT) not in sys.path:
    sys.path.insert(0, str(STACK_ROOT))

from go_workflow.execution_contracts import (dependency_findings, resolve_execution_contract, validate_dependencies, validate_execution_contract, validate_phase_profiles, validate_verification_evidence)
from go_workflow.constants import CURRENT_CONTRACT_VERSION, STACK_REF, STACK_VERSION
from go_workflow.task_design import behavior_review_context, behavior_review_required, review_contract, validate_behavior_review
from go_workflow.intake import (RECORD_SCHEMA as INTAKE_RECORD_SCHEMA, prepare_request, record_path,
                                validate_assessment, validate_record as validate_intake_record)
from go_workflow.migrations import plan_contract_migration
from go_workflow.adapter_protocol import build_adapter_request, normalize_adapter_result, validate_adapter_result, codex_stream_payload
from go_workflow.adapters import detect_hermes_prompt_flag, native_agent_command
from go_workflow.execution_context import ContextError, create_snapshot, verify_snapshot
from go_workflow.model_profiles import phase_model, controlled_preflight, adapter_capabilities
from go_workflow.release import PublicationError
from go_workflow.run_state import RunStateError, select_managed_task, execute_managed, process_command, worker_enter, relocate_run, state_path, read_state
from go_workflow.completion import CompletionError, require_completion, lifecycle_report
from go_workflow.worktrees import (guard_workspace_command, registered_workspace, execution_lease, active_task,
    validate_record, stage_workspace, integration_slot, record_integration, cleanup_workspace, reconcile_workspace, rebind_workspace, owned_record, verify_workspace,
    registry_path, read_object, require_run_idle, require_visible_index, workspace_content_fingerprint,
    WorkspaceError, create_workspace, workflow_root, is_git_checkout)
from go_workflow.capacity_policy import plan_capacity
from go_workflow.campaign import CampaignError, execute_campaign, plan_campaign
from go_workflow.routing import detected_platform, normalize_router_command, recommend_route
from go_workflow.task_state import open_task_records, pending_review_task_ids as task_state_pending_review_task_ids, task_path
from go_workflow.task_state import unfinished_task_ids as task_state_unfinished_task_ids
from go_workflow.stack_update import StackUpdateError, apply_stack_update, latest_stack_ref, plan_stack_update, rollback_stack_update
from go_workflow.state_io import StateLockError, append_jsonl_locked, atomic_json, atomic_move_json, atomic_write_text, remove_jsonl_events_locked, repository_lock
from go_workflow.agents_gateway import (
    AgentsGatewayError,
    apply_agents_gateway,
    plan_agents_gateway,
    restore_agents_gateway,
    validate_agents_gateway,
)
from go_workflow.hermes_proof import validate_live_hermes_proof, verify_live_hermes_evidence
from go_workflow.runtime_identity import resolve_runtime_identity
from go_workflow.repository_index import (
    GRAPH_RELATIVE_PATH,
    RepositoryIndexError,
    build_graph,
    index_status,
    query_graph,
    repository_blast,
    select_repository_context,
    validate_repository_map,
    validate_repository_context,
    validate_task_repository_context,
)
from go_workflow.architecture import (
    architecture_briefs,
    architecture_claim_findings,
    architecture_finish_findings,
    architecture_status as summarize_architecture,
    deterministic_minimum_impact,
    effective_architecture_impact,
    resolve_applicable_architecture,
    validate_architecture_state,
    validate_task_architecture,
)

CONTRACT_ROOT = STACK_ROOT
SCHEMA_ROOT = CONTRACT_ROOT / "schemas"
if not SCHEMA_ROOT.is_dir():
    SCHEMA_ROOT = Path(sys.prefix) / "schemas"
FIXTURE_ROOT = CONTRACT_ROOT / "fixtures" / "minimal" / ".go"
if not FIXTURE_ROOT.is_dir():
    FIXTURE_ROOT = Path(sys.prefix) / "fixtures" / "minimal" / ".go"

PROJECT_SCHEMA = "go-workflow.repo-local.project.v1"
ARCH_SCHEMA = "go-workflow.repo-local.architecture-principles.v1"
VISION_SCHEMA = "go-workflow.repo-local.vision.v1"
HIERARCHY_SCHEMA = "go-workflow.repo-local.hierarchy.v1"
TASK_SCHEMA = "go-workflow.repo-local.task.v1"
EVENT_SCHEMA = "go-workflow.repo-local.event.v1"
EXPORT_BUNDLE_SCHEMA = "go-workflow.repo-local.export-bundle.v1"
EXECUTION_BRIEF_SCHEMA = "go-workflow.execution-brief.v1"
RECOMMENDATION_SCHEMA = "go-workflow.recommendation.v1"
DELIVERY_SCHEMA = "go-workflow.delivery.v1"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BLOCK_SECRET_RE = re.compile(r"(secret|token|credential|password|\.env|id_rsa|private[-_]key)", re.I)
DELIVERY_SENSITIVE_PATTERNS = (
    ("local home path", re.compile(r"(?:/home/[^/\s]+|/Users/[^/\s]+|[A-Za-z]:\\\\Users\\\\[^\\\s]+)")),
    ("credential assignment", re.compile(r"(?:token|password|secret|credential)\s*[:=]\s*\S+", re.I)),
    ("credential file", re.compile(r"(?:^|[/\\])(?:\.env(?:\.[^/\\\s]+)?|id_rsa|private[-_]key)(?:$|[/\\\s])", re.I)),
    ("private key material", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
)
OUTCOME_TERMINAL_STATUSES = {"verified", "blocked", "rejected"}


class RepoLocalError(Exception):
    """Expected repo-local workflow failure."""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return slug or "project"


def parse_pipe_fields(value: str, expected: int, label: str) -> list[str]:
    parts = [part.strip() for part in value.split("|")]
    if len(parts) != expected or any(not part for part in parts):
        raise RepoLocalError(f"{label} must have {expected} pipe-separated non-empty fields")
    return parts


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepoLocalError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RepoLocalError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RepoLocalError(f"JSON root must be an object: {path}")
    return data


def dump_json(path: Path, data: dict[str, Any]) -> None:
    atomic_json(path, data)


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    append_jsonl_locked(path, event)


def require(condition: bool, errors: list[str], message: str) -> None:
    if not condition:
        errors.append(message)


def go_root(repo: Path) -> Path:
    return workflow_root(repo)


def relative(repo: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def validate_project(data: dict[str, Any], rel: str) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == PROJECT_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "project", errors, f"{rel}: kind must be project")
    require(bool(data.get("id")), errors, f"{rel}: id required")
    require(bool(data.get("name")), errors, f"{rel}: name required")
    require(data.get("source_of_truth") == "repo-local", errors, f"{rel}: source_of_truth must be repo-local")
    contract_version = data.get("contract_version", 1)
    require(isinstance(contract_version, int) and 1 <= contract_version <= CURRENT_CONTRACT_VERSION, errors, f"{rel}: contract_version must be between 1 and {CURRENT_CONTRACT_VERSION}")
    require(data.get("project_mode", "project") in {"project", "template"}, errors, f"{rel}: project_mode must be project or template")
    require(isinstance(data.get("default_verification"), list) and bool(data.get("default_verification")), errors, f"{rel}: default_verification must be a non-empty list")
    required_stack_version = data.get("required_stack_version")
    require(bool(re.fullmatch(r"\d+\.\d+\.\d+", str(required_stack_version or ""))), errors, f"{rel}: required_stack_version must be semantic version X.Y.Z")
    stack_ref = data.get("stack_ref")
    immutable_ref = bool(re.fullmatch(r"v\d+\.\d+\.\d+|[0-9a-f]{40}", str(stack_ref or "")))
    require(immutable_ref, errors, f"{rel}: stack_ref must be an immutable version tag (vX.Y.Z) or full commit SHA")
    if "release_profiles" in data:
        from go_workflow.shipping import validate_release_profiles
        errors.extend(validate_release_profiles(data["release_profiles"]))
    if "phase_profiles" in data:
        errors.extend(validate_phase_profiles(data["phase_profiles"]))
    if "dependency_projects" in data:
        registry = data["dependency_projects"]
        if not isinstance(registry, dict) or not all(isinstance(k, str) and TASK_ID_RE.fullmatch(k) and isinstance(v, str) and v.strip() for k, v in registry.items()):
            errors.append("dependency_projects must map project IDs to explicit paths")
    if "execution_defaults" in data:
        errors.extend(validate_execution_contract(data["execution_defaults"]))
    return errors


def validate_architecture_principles(data: dict[str, Any], rel: str) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == ARCH_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "architecture_principles", errors, f"{rel}: kind must be architecture_principles")
    require(bool(data.get("project")), errors, f"{rel}: project required")
    principles = data.get("principles")
    require(isinstance(principles, list) and bool(principles), errors, f"{rel}: principles must be a non-empty list")
    if isinstance(principles, list):
        for index, principle in enumerate(principles, start=1):
            require(isinstance(principle, dict), errors, f"{rel}: principle {index} must be an object")
            if isinstance(principle, dict):
                for key in ("id", "statement", "rationale", "enforcement"):
                    require(bool(principle.get(key)), errors, f"{rel}: principle {index} missing {key}")
    return errors


def validate_vision(data: dict[str, Any], rel: str) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == VISION_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "vision", errors, f"{rel}: kind must be vision")
    require(data.get("status") in {"draft", "active", "superseded", "archived"}, errors, f"{rel}: invalid status")
    for key in ("project", "north_star", "wedge", "target_user", "core_promise"):
        require(bool(data.get(key)), errors, f"{rel}: {key} required")
    for key in ("product_principles", "non_goals", "success_metrics"):
        require(isinstance(data.get(key), list), errors, f"{rel}: {key} must be a list")
    return errors


def hierarchy_epics(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return canonical epics, accepting legacy feature_groups during migration."""
    epics = data.get("epics")
    if isinstance(epics, list):
        return [epic for epic in epics if isinstance(epic, dict)]
    groups = data.get("feature_groups")
    if isinstance(groups, list):
        return [group for group in groups if isinstance(group, dict)]
    return []


def epic_task_ids(epic: dict[str, Any]) -> list[str]:
    task_ids = [str(task_id) for task_id in (epic.get("tasks") or [])]
    for feature in epic.get("features") or []:
        if isinstance(feature, dict):
            task_ids.extend(str(task_id) for task_id in (feature.get("tasks") or []))
    return list(dict.fromkeys(task_ids))


def set_hierarchy_epics(data: dict[str, Any], epics: list[dict[str, Any]]) -> None:
    data["epics"] = epics
    data.pop("feature_groups", None)


def validate_hierarchy(data: dict[str, Any], rel: str) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == HIERARCHY_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "hierarchy", errors, f"{rel}: kind must be hierarchy")
    require(bool(data.get("project")), errors, f"{rel}: project required")
    has_epics = isinstance(data.get("epics"), list)
    has_legacy_groups = isinstance(data.get("feature_groups"), list)
    require(has_epics or has_legacy_groups, errors, f"{rel}: epics must be a list")
    for epic in hierarchy_epics(data):
        require(bool(epic.get("id")) and bool(epic.get("title")), errors, f"{rel}: each epic needs id and title")
        require(isinstance(epic.get("tasks", []), list), errors, f"{rel}: epic {epic.get('id', '<missing>')} tasks must be a list")
        require(isinstance(epic.get("features", []), list), errors, f"{rel}: epic {epic.get('id', '<missing>')} features must be a list")
        for feature in epic.get("features", []):
            require(isinstance(feature, dict) and bool(feature.get("id")) and bool(feature.get("title")), errors, f"{rel}: each feature needs id and title")
            if isinstance(feature, dict):
                require(isinstance(feature.get("tasks", []), list), errors, f"{rel}: feature {feature.get('id', '<missing>')} tasks must be a list")
    return errors


def outcome_completion_findings(task: dict[str, Any]) -> list[str]:
    if task.get("outcome_tracking_version") != 1:
        return []
    findings: list[str] = []
    outcomes = task.get("requested_outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        return ["outcome tracking requires requested_outcomes"]
    for index, outcome in enumerate(outcomes, start=1):
        if not isinstance(outcome, dict):
            findings.append(f"outcome {index} must be an object")
            continue
        outcome_id = str(outcome.get("id") or f"#{index}")
        status = outcome.get("status")
        evidence = outcome.get("evidence")
        if status not in OUTCOME_TERMINAL_STATUSES:
            findings.append(f"{outcome_id} has no terminal disposition")
        if not isinstance(evidence, list) or not evidence or not all(
            isinstance(item, dict) and bool(str(item.get("summary") or "").strip()) for item in evidence
        ):
            findings.append(f"{outcome_id} has no evidence")
    return findings


def verify_pending_outcomes_from_checks(repo: Path, path: Path, task: dict[str, Any], agent: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Close pending acceptance outcomes only after verification and critic have passed."""
    if task.get("outcome_tracking_version") != 1:
        return task
    outcomes = task.get("requested_outcomes") or []
    if not outcomes or any(
        not isinstance(outcome, dict) or outcome.get("source") != "execution_brief_acceptance"
        for outcome in outcomes
    ):
        return task
    passed_commands = [str(check.get("command") or "verification") for check in checks if check.get("returncode") == 0]
    evidence_summary = "verification and critic passed: " + "; ".join(passed_commands or ["no verification command"])
    changed: list[str] = []
    for outcome in outcomes:
        if not isinstance(outcome, dict) or outcome.get("status") != "pending":
            continue
        outcome["status"] = "verified"
        outcome.setdefault("evidence", []).append({
            "created_at": now_iso(),
            "agent": agent,
            "summary": evidence_summary,
        })
        changed.append(str(outcome.get("id") or "unknown"))
    if changed:
        dump_json(path, task)
        for outcome_id in changed:
            append_jsonl(go_root(repo) / "evidence" / "events.jsonl", event(task["id"], "evidence.appended", agent, {
                "action": "outcome.disposition_recorded",
                "outcome_id": outcome_id,
                "status": "verified",
                "evidence": evidence_summary,
                "source": "autonomous_verification_and_critic",
            }))
    return task


def validate_task(data: dict[str, Any], rel: str, expected_status: str | None = None) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == TASK_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "task", errors, f"{rel}: kind must be task")
    task_id = str(data.get("id") or "")
    require(bool(TASK_ID_RE.fullmatch(task_id)), errors, f"{rel}: invalid id")
    require(data.get("status") in {"open", "active", "blocked", "done"}, errors, f"{rel}: invalid status")
    if expected_status:
        require(data.get("status") == expected_status, errors, f"{rel}: status must match directory {expected_status}")
    for key in ("project", "summary"):
        require(bool(data.get(key)), errors, f"{rel}: {key} required")
    for key in ("acceptance", "verification"):
        require(isinstance(data.get(key), list) and bool(data.get(key)), errors, f"{rel}: {key} must be a non-empty list")
    scope = data.get("scope")
    require(isinstance(scope, dict), errors, f"{rel}: scope must be an object")
    if isinstance(scope, dict):
        require(isinstance(scope.get("read"), list), errors, f"{rel}: scope.read must be a list")
        require(isinstance(scope.get("modify"), list), errors, f"{rel}: scope.modify must be a list")
    require(isinstance(data.get("claim"), dict), errors, f"{rel}: claim must be an object")
    require(data.get("execution_mode", "mechanical") in {"mechanical", "agent"}, errors, f"{rel}: execution_mode must be mechanical or agent")
    require(data.get("shareable_delivery", "auto") in {"auto", "required", "none"}, errors, f"{rel}: shareable_delivery must be auto, required, or none")
    # These were free-form metadata before lifecycle opt-in. Never reinterpret
    # historical records merely because a newer runtime reads them.
    if "execution_contract" in data and "dependencies" in data:
        errors.extend(f"{rel}: {error}" for error in validate_dependencies(data["dependencies"]))
    if "execution_contract" in data:
        errors.extend(validate_execution_contract(data["execution_contract"]))
    if "execution_contract" in data and "verification_evidence" in data:
        if not isinstance(data["verification_evidence"], list):
            errors.append("verification_evidence must be a list")
        else:
            for proof in data["verification_evidence"]:
                errors.extend(validate_verification_evidence(proof))
                if isinstance(proof, dict) and proof.get("task_id") != task_id:
                    errors.append("verification evidence task_id mismatch")
    if "repository_context" in data:
        errors.extend(validate_repository_context(data.get("repository_context"), f"{rel}: repository_context"))
    if "architecture" in data:
        errors.extend(validate_task_architecture(data.get("architecture"), rel))
    if "work_status" in data:
        require(data.get("work_status") in {"pending", "in_progress", "completed"}, errors, f"{rel}: invalid work_status")
    if "review_status" in data:
        require(data.get("review_status") in {"none", "review", "needs_fix", "approved"}, errors, f"{rel}: invalid review_status")
    if "review_history" in data:
        require(isinstance(data.get("review_history"), list), errors, f"{rel}: review_history must be a list")
    if data.get("outcome_tracking_version") == 1:
        source = data.get("intent_source")
        require(isinstance(source, dict), errors, f"{rel}: intent_source must be an object")
        if isinstance(source, dict):
            text = str(source.get("text") or "")
            require(bool(text), errors, f"{rel}: intent_source.text required")
            require(source.get("sha256") == hashlib.sha256(text.encode("utf-8")).hexdigest(), errors, f"{rel}: intent_source.sha256 mismatch")
        outcomes = data.get("requested_outcomes")
        require(isinstance(outcomes, list) and bool(outcomes), errors, f"{rel}: requested_outcomes must be a non-empty list")
        if isinstance(outcomes, list):
            for index, outcome in enumerate(outcomes, start=1):
                require(isinstance(outcome, dict), errors, f"{rel}: outcome {index} must be an object")
                if isinstance(outcome, dict):
                    require(outcome.get("status") in ({"pending"} | OUTCOME_TERMINAL_STATUSES), errors, f"{rel}: outcome {index} has invalid status")
                    require(isinstance(outcome.get("evidence"), list), errors, f"{rel}: outcome {index} evidence must be a list")
        if expected_status == "done":
            errors.extend(f"{rel}: {finding}" for finding in outcome_completion_findings(data))
    if 'behavior_review_version' in data:
        require(data.get('behavior_review_version') == 1, errors, f'{rel}: behavior_review_version must be 1')
        require(data.get('outcome_tracking_version') == 1, errors,
                f'{rel}: behavior_review_version requires outcome_tracking_version 1')
    return errors


def validate_delivery_manifest(data: dict[str, Any]) -> list[str]:
    if not isinstance(data, dict):
        return ["delivery must be an object"]
    errors: list[str] = []

    def object_value(name: str, required_keys: set[str]) -> dict[str, Any] | None:
        value = data.get(name)
        if not isinstance(value, dict):
            errors.append(f"delivery.{name} must be an object")
            return None
        unknown = sorted(set(value) - required_keys)
        if unknown:
            errors.append(f"delivery.{name} contains unknown properties: {', '.join(unknown)}")
        for key in sorted(required_keys - set(value)):
            errors.append(f"delivery.{name}.{key} is required")
        return value

    root_keys = {"schema", "delivery_id", "project", "assignment", "release", "disclosure", "sections", "provenance"}
    unknown_root = sorted(set(data) - root_keys)
    if unknown_root:
        errors.append(f"delivery contains unknown properties: {', '.join(unknown_root)}")
    for key in sorted(root_keys - set(data)):
        errors.append(f"delivery.{key} is required")

    require(data.get("schema") == DELIVERY_SCHEMA, errors, "delivery.schema must be go-workflow.delivery.v1")
    delivery_id = data.get("delivery_id")
    require(isinstance(delivery_id, str) and bool(TASK_ID_RE.fullmatch(delivery_id)), errors, "delivery.delivery_id must be a valid identifier")

    project = object_value("project", {"id", "name"}) or {}
    for key in ("id", "name"):
        value = project.get(key)
        require(isinstance(value, str) and len(value) >= 1, errors, f"delivery.project.{key} must be a non-empty string")

    assignment = object_value("assignment", {"kind", "id", "title"}) or {}
    require(assignment.get("kind") in {"epic", "task-set"}, errors, "delivery.assignment.kind must be epic or task-set")
    for key in ("id", "title"):
        value = assignment.get(key)
        require(isinstance(value, str) and len(value) >= 1, errors, f"delivery.assignment.{key} must be a non-empty string")

    release = object_value("release", {"version", "status", "created_at", "supersedes"}) or {}
    version = release.get("version")
    require(type(version) is int and version >= 1, errors, "delivery.release.version must be an integer >= 1")
    require(release.get("status") in {"draft", "released", "superseded"}, errors, "delivery.release.status must be draft, released, or superseded")
    created_at = release.get("created_at")
    require(isinstance(created_at, str) and len(created_at) >= 1, errors, "delivery.release.created_at must be a non-empty string")
    supersedes = release.get("supersedes")
    require(supersedes is None or isinstance(supersedes, str), errors, "delivery.release.supersedes must be a string or null")

    disclosure = object_value("disclosure", {"class", "scan_status"}) or {}
    require(disclosure.get("class") in {"public", "link-private", "restricted"}, errors, "delivery.disclosure.class must be public, link-private, or restricted")
    require(disclosure.get("scan_status") in {"passed", "blocked"}, errors, "delivery.disclosure.scan_status must be passed or blocked")

    sections = object_value("sections", {"summary", "delivered_scope", "excluded_scope", "evidence", "limitations", "next_steps"}) or {}
    summary = sections.get("summary")
    if "summary" in sections:
        require(isinstance(summary, str) and len(summary) >= 1, errors, "delivery.sections.summary must be a non-empty string")
    for key in ("delivered_scope", "excluded_scope", "limitations", "next_steps"):
        if key not in sections:
            continue
        value = sections.get(key)
        if not isinstance(value, list) or not value:
            errors.append(f"delivery.sections.{key} must be a non-empty list")
        else:
            for index, item in enumerate(value, start=1):
                require(isinstance(item, str) and len(item) >= 1, errors, f"delivery.sections.{key} item {index} must be a non-empty string")

    evidence = sections.get("evidence")
    if "evidence" not in sections:
        pass
    elif not isinstance(evidence, list) or not evidence:
        errors.append("delivery.sections.evidence must be a non-empty list")
    else:
        for index, item in enumerate(evidence, start=1):
            if not isinstance(item, dict):
                errors.append(f"delivery.sections.evidence item {index} must be an object")
                continue
            unknown = sorted(set(item) - {"label", "summary"})
            if unknown:
                errors.append(f"delivery.sections.evidence item {index} contains unknown properties: {', '.join(unknown)}")
            for key in ("label", "summary"):
                value = item.get(key)
                require(isinstance(value, str) and len(value) >= 1, errors, f"delivery.sections.evidence item {index}.{key} must be a non-empty string")

    provenance = object_value("provenance", {"source", "source_sha256", "html_sha256"}) or {}
    require(provenance.get("source") == ".go", errors, "delivery.provenance.source must be .go")
    source_sha = provenance.get("source_sha256")
    require(isinstance(source_sha, str) and bool(re.fullmatch(r"[0-9a-f]{64}", source_sha)), errors, "delivery.provenance.source_sha256 must be sha256")
    html_sha = provenance.get("html_sha256")
    require(html_sha is None or (isinstance(html_sha, str) and bool(re.fullmatch(r"[0-9a-f]{64}", html_sha))), errors, "delivery.provenance.html_sha256 must be null or sha256")
    return errors


def validate_recommendation(data: dict[str, Any], rel: str, project_id: str = "") -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == RECOMMENDATION_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "recommendation", errors, f"{rel}: kind must be recommendation")
    require(bool(TASK_ID_RE.fullmatch(str(data.get("id") or ""))), errors, f"{rel}: invalid id")
    require(data.get("status") in {"pending", "applied", "superseded"}, errors, f"{rel}: invalid status")
    require(bool(data.get("created_at")), errors, f"{rel}: created_at required")
    require(bool(data.get("project")), errors, f"{rel}: project required")
    if project_id:
        require(data.get("project") == project_id, errors, f"{rel}: project does not match project.json id {project_id!r}")
    authority = data.get("authority")
    require(isinstance(authority, dict), errors, f"{rel}: authority must be an object")
    if isinstance(authority, dict):
        mode = authority.get("mode")
        require(mode in {"advice", "execute"}, errors, f"{rel}: authority.mode must be advice or execute")
        require(bool(str(authority.get("source") or "").strip()), errors, f"{rel}: authority.source required")
        require(authority.get("implementation_authorized") is (mode == "execute"), errors, f"{rel}: implementation_authorized disagrees with mode")
        require(authority.get("planning_state_authorized") is True, errors, f"{rel}: durable recommendation requires planning-state authority")
    brief = data.get("brief")
    require(isinstance(brief, dict), errors, f"{rel}: brief must be an object")
    if isinstance(brief, dict):
        errors.extend(f"{rel}: {item}" for item in validate_execution_brief(brief))
        expected_id = f"recommendation-{str(brief.get('source', {}).get('sha256') or '')[:16]}"
        require(data.get("id") == expected_id, errors, f"{rel}: id must derive from recommendation sha256")
    return errors


def validate_event(data: dict[str, Any], rel: str, line_number: int) -> list[str]:
    prefix = f"{rel}:{line_number}"
    errors: list[str] = []
    require(data.get("schema") == EVENT_SCHEMA, errors, f"{prefix}: schema mismatch")
    require(data.get("kind") == "event", errors, f"{prefix}: kind must be event")
    require(data.get("event") in {"task.created", "task.claimed", "task.finished", "task.blocked", "task.reviewed", "delivery.built", "evidence.appended", "decision.recorded", "run.checked", "auto.safety_gate", "auto.reflected", "auto.attempt"}, errors, f"{prefix}: invalid event")
    require(bool(data.get("created_at")), errors, f"{prefix}: created_at required")
    require(bool(data.get("task_id")), errors, f"{prefix}: task_id required")
    if data.get("event") == "delivery.built":
        payload = data.get("data")
        require(isinstance(payload, dict), errors, f"{prefix}: delivery.built data must be an object")
        if isinstance(payload, dict):
            for field in ("transaction_id", "delivery_id", "manifest", "html", "html_sha256"):
                require(isinstance(payload.get(field), str) and bool(payload.get(field, "").strip()), errors, f"{prefix}: delivery.built data.{field} required")
    return errors


def validate_repo(repo: Path, *, skip_lifecycle_migration: bool = False) -> list[str]:
    repo = repo.resolve()
    root = go_root(repo)
    errors: list[str] = []
    if not root.is_dir():
        return [f"missing .go directory: {root}"]
    errors.extend(validate_agents_gateway(repo))
    if not skip_lifecycle_migration:
        from go_workflow.migrations import pending_lifecycle_findings
        errors.extend(pending_lifecycle_findings(repo))
    validators = {
        "project.json": validate_project,
        "architecture-principles.json": validate_architecture_principles,
        "vision.json": validate_vision,
        "hierarchy.json": validate_hierarchy,
    }
    documents: dict[str, dict[str, Any]] = {}
    for filename, validator in validators.items():
        path = root / filename
        try:
            documents[filename] = load_json(path)
            errors.extend(validator(documents[filename], relative(repo, path)))
        except RepoLocalError as exc:
            errors.append(str(exc))
    project_id = str(documents.get("project.json", {}).get("id") or "")
    repository_map = root / "repository-map.json"
    if repository_map.exists():
        try:
            errors.extend(
                validate_repository_map(
                    load_json(repository_map),
                    relative(repo, repository_map),
                    project=project_id,
                )
            )
        except RepoLocalError as exc:
            errors.append(str(exc))
    required_stack_version = str(documents.get("project.json", {}).get("required_stack_version") or "0.0.0")
    if semantic_version_tuple(STACK_VERSION) < semantic_version_tuple(required_stack_version):
        errors.append(f".go/project.json: requires go-workflow-stack >= {required_stack_version}, current runtime is {STACK_VERSION}")
    for filename in ("architecture-principles.json", "vision.json", "hierarchy.json"):
        document_project = documents.get(filename, {}).get("project")
        if project_id and document_project != project_id:
            errors.append(f".go/{filename}: project {document_project!r} does not match project.json id {project_id!r}")
    linked_task_ids: set[str] = set()
    hierarchy = documents.get("hierarchy.json", {})
    for epic in hierarchy_epics(hierarchy):
        linked_task_ids.update(str(task_id) for task_id in epic.get("tasks", []) if task_id)
        for feature in epic.get("features", []):
            if isinstance(feature, dict):
                linked_task_ids.update(str(task_id) for task_id in feature.get("tasks", []) if task_id)
    task_ids: set[str] = set()
    for status in ("open", "active", "blocked", "done"):
        for path in sorted((root / "tasks" / status).glob("*.json")):
            try:
                data = load_json(path)
                errors.extend(validate_task(data, relative(repo, path), expected_status=status))
                errors.extend(validate_task_repository_context(repo, data, relative(repo, path)))
                errors.extend(dependency_findings(repo, data))
                contract = data.get("execution_contract")
                if isinstance(contract, dict) and isinstance(contract.get("phase_profile"), str):
                    if contract["phase_profile"] not in documents.get("project.json", {}).get("phase_profiles", {}):
                        errors.append(f"{relative(repo, path)}: unknown phase_profile")
                task_id = str(data.get("id") or "")
                if task_id in task_ids:
                    errors.append(f"{relative(repo, path)}: duplicate task id {task_id}")
                task_ids.add(task_id)
                if project_id and data.get("project") != project_id:
                    errors.append(f"{relative(repo, path)}: task project {data.get('project')!r} does not match project.json id {project_id!r}")
                if task_id and task_id not in linked_task_ids:
                    errors.append(f"{relative(repo, path)}: task {task_id!r} is not linked from hierarchy")
            except RepoLocalError as exc:
                errors.append(str(exc))
    for task_id in sorted(linked_task_ids - task_ids):
        errors.append(f".go/hierarchy.json: linked task {task_id!r} does not exist in any task state")
    for path in sorted((root / "intake").glob("*.json")):
        try:
            for finding in validate_intake_record(load_json(path), project_id, task_ids):
                errors.append(f"{relative(repo, path)}: {finding}")
        except RepoLocalError as exc:
            errors.append(str(exc))
    for path in sorted((root / 'workspaces').glob('*.json')):
        try:
            record = validate_record(load_json(path))
            if record['task_id'] != path.stem or record['task_id'] not in task_ids:
                errors.append(f'{path}: workspace task identity missing/mismatched')
        except (WorkspaceError, RepoLocalError) as exc:
            errors.append(f'{path}: {exc}')
    from go_workflow.run_state import validate_state
    for path in sorted([* (root / 'runs').glob('*/run-state.json'), * (root / 'runs').glob('*/completion-state.json'),
                        * (root / 'runs').glob('*/publication-process.json')]):
        try:
            run = load_json(path)
            validate_state(run)
            if run['task_id'] != path.parent.name or run['task_id'] not in task_ids or run['project'] != project_id:
                errors.append(f'{path}: managed run task/project identity mismatch')
        except (RunStateError, RepoLocalError) as exc:
            errors.append(f'{path}: {exc}')
    from go_workflow.release import validate_state as validate_publication_state
    for path in sorted((root / 'runs').glob('*/release-state.json')):
        try:
            publication = validate_publication_state(load_json(path))
            if publication['task_id'] != path.parent.name or publication['task_id'] not in task_ids or publication['project'] != project_id:
                errors.append(f'{path}: publication task/project identity mismatch')
        except (PublicationError, RepoLocalError) as exc:
            errors.append(f'{path}: {exc}')
    errors.extend(validate_architecture_state(root, project_id))
    recommendation_paths = list((root / "recommendations").glob("*.json"))
    recommendation_paths.extend((root / "recommendations" / "applied").glob("*.json"))
    recommendation_paths.extend((root / "recommendations" / "superseded").glob("*.json"))
    for path in sorted(recommendation_paths):
        try:
            errors.extend(validate_recommendation(load_json(path), relative(repo, path), project_id))
        except RepoLocalError as exc:
            errors.append(str(exc))
    for folder in ("runs", "evidence", "decisions"):
        for path in sorted((root / folder).glob("*.jsonl")):
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{relative(repo, path)}:{index}: invalid JSONL: {exc}")
                    continue
                if not isinstance(event, dict):
                    errors.append(f"{relative(repo, path)}:{index}: event must be an object")
                    continue
                errors.extend(validate_event(event, relative(repo, path), index))
    return errors


def copy_fixture_init(repo: Path, force: bool = False) -> None:
    root = go_root(repo)
    if root.exists() and any(root.iterdir()) and not force:
        raise RepoLocalError(f"refusing to overwrite existing non-empty {root}; pass --force for spike fixtures")
    root.mkdir(parents=True, exist_ok=True)
    for source in FIXTURE_ROOT.rglob("*"):
        if source.is_dir():
            continue
        target = root / source.relative_to(FIXTURE_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def ensure_go_dirs(root: Path) -> None:
    for rel in [
        "tasks/open",
        "tasks/active",
        "tasks/blocked",
        "tasks/done",
        "runs",
        "evidence",
        "decisions",
        "imports",
        "locks",
    ]:
        (root / rel).mkdir(parents=True, exist_ok=True)


def parse_principles(values: list[str]) -> list[dict[str, str]]:
    if not values:
        values = [
            "repo-local-state|Project workflow state lives in .go/ next to code.|A fresh clone should explain current direction and next work.|go-workflow-stack validate/readback",
            "json-first|Current state is JSON and append-only history is JSONL.|Agents need deterministic, diffable contracts.|schema validation and CLI checks",
        ]
    principles = []
    for value in values:
        pid, statement, rationale, enforcement = parse_pipe_fields(value, 4, "--principle")
        principles.append({"id": slugify(pid), "statement": statement, "rationale": rationale, "enforcement": enforcement})
    return principles


def parse_hierarchy(feature_groups: list[str], features: list[str], project_id: str) -> dict[str, Any]:
    epics: dict[str, dict[str, Any]] = {}
    for value in feature_groups or ["workflow|Workflow"]:
        gid, title = parse_pipe_fields(value, 2, "--feature-group")
        epics[slugify(gid)] = {"id": slugify(gid), "title": title, "features": [], "tasks": []}
    for value in features or ["workflow|repo-local-workflow|Repo-local workflow"]:
        gid, fid, title = parse_pipe_fields(value, 3, "--feature")
        gid = slugify(gid)
        if gid not in epics:
            epics[gid] = {"id": gid, "title": gid.replace("-", " ").title(), "features": [], "tasks": []}
        epics[gid]["features"].append({"id": slugify(fid), "title": title, "tasks": []})
    return {"schema": HIERARCHY_SCHEMA, "kind": "hierarchy", "project": project_id, "epics": list(epics.values())}


def append_task_to_epic(root: Path, epic_id: str, task_id: str) -> None:
    if not epic_id:
        return
    path = root / "hierarchy.json"
    hierarchy = load_json(path)
    epics = hierarchy_epics(hierarchy)
    for epic in epics:
        if epic.get("id") != slugify(epic_id):
            continue
        tasks = epic.setdefault("tasks", [])
        if task_id not in tasks:
            tasks.append(task_id)
        set_hierarchy_epics(hierarchy, epics)
        dump_json(path, hierarchy)
        return
    raise RepoLocalError(f"epic not found in hierarchy: {epic_id}")


def append_task_to_hierarchy(root: Path, feature_ref: str, task_id: str) -> None:
    if not feature_ref:
        return
    if "." not in feature_ref:
        raise RepoLocalError("--feature must be formatted as epic_id.feature_id")
    group_id, feature_id = [slugify(part) for part in feature_ref.split(".", 1)]
    path = root / "hierarchy.json"
    hierarchy = load_json(path)
    epics = hierarchy_epics(hierarchy)
    for epic in epics:
        if epic.get("id") != group_id:
            continue
        for feature in epic.get("features", []):
            if feature.get("id") != feature_id:
                continue
            tasks = feature.setdefault("tasks", [])
            if task_id not in tasks:
                tasks.append(task_id)
            set_hierarchy_epics(hierarchy, epics)
            dump_json(path, hierarchy)
            return
    raise RepoLocalError(f"feature not found in hierarchy: {feature_ref}")


def find_task(root: Path, task_id: str) -> tuple[Path, dict[str, Any]]:
    matches: list[Path] = []
    for status in ("open", "active", "blocked", "done"):
        path = task_path(root, status, task_id)
        if path.exists():
            matches.append(path)
    if not matches:
        raise RepoLocalError(f"task not found: {task_id}")
    if len(matches) > 1:
        raise RepoLocalError(f"task exists in multiple status dirs: {task_id}")
    return matches[0], load_json(matches[0])


def open_tasks(repo: Path) -> list[tuple[Path, dict[str, Any]]]:
    return [(path, task) for path, task in open_task_records(go_root(repo))
            if not dependency_findings(repo, task, readiness=True)]


def path_matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) or path == pattern for pattern in patterns)


def git_status(repo: Path) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        code = line[:2]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append((code, path))
    return entries


def parse_finish_evidence_fields(summary: str) -> dict[str, str]:
    """Parse compact `key=value; key=value` finish evidence without making prose illegal."""
    fields: dict[str, str] = {}
    for part in re.split(r"[;\n]", summary):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = re.sub(r"[^a-z0-9_]+", "_", key.strip().lower()).strip("_")
        value = value.strip()
        if key and value:
            fields[key] = value
    return fields


def split_evidence_list(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,|]", value) if item.strip()]


EMPTY_GIT_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def git_head_commit(repo: Path) -> str | None:
    result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo, text=True, capture_output=True, check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


def claim_base_commit(repo: Path) -> str:
    return git_head_commit(repo) or EMPTY_GIT_TREE_SHA


def lifecycle_only_path(path: str) -> bool:
    lifecycle_prefixes = (
        ".go/tasks/",
        ".go/runs/",
        ".go/evidence/",
        ".go/reflections/",
    )
    return path == ".go/hierarchy.json" or path.startswith(lifecycle_prefixes)


def claim_event_base_commit(repo: Path, task: dict[str, Any]) -> str:
    task_id = str(task.get("id", "")).strip()
    claim = task.get("claim") or {}
    claimed_at = str(claim.get("claimed_at", "")).strip()
    events_path = repo / ".go" / "runs" / "events.jsonl"
    if not events_path.exists():
        raise RepoLocalError("no_diff=true requires task.claimed event provenance")

    candidates: list[dict[str, Any]] = []
    for line_number, line in enumerate(events_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RepoLocalError(
                f"no_diff=true cannot trust malformed task.claimed ledger at line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(event, dict):
            raise RepoLocalError(
                f"no_diff=true cannot trust non-object task.claimed ledger entry at line {line_number}"
            )
        if event.get("event") == "task.claimed" and event.get("task_id") == task_id:
            candidates.append(event)

    identified = [
        event
        for event in candidates
        if str((event.get("data") or {}).get("claimed_at", "")).strip()
    ]
    if identified:
        exact = [
            event
            for event in identified
            if str((event.get("data") or {}).get("claimed_at", "")).strip() == claimed_at
        ]
        if len(exact) != 1:
            raise RepoLocalError("no_diff=true claim identity does not match exactly one task.claimed event")
        event = exact[0]
    else:
        event = candidates[-1] if candidates else None
    event_base = str(((event or {}).get("data") or {}).get("base_commit", "")).strip()
    if not event_base:
        raise RepoLocalError("no_diff=true requires task.claimed event base_commit provenance")
    return event_base


def committed_paths_since_claim(repo: Path, task: dict[str, Any]) -> list[str]:
    base = str((task.get("claim") or {}).get("base_commit") or "").strip()
    if not base:
        raise RepoLocalError("no_diff=true requires claim.base_commit provenance")
    event_base = claim_event_base_commit(repo, task)
    if event_base != base:
        raise RepoLocalError("no_diff=true claim.base_commit does not match task.claimed event")
    head = git_head_commit(repo)
    if head is None:
        if base == EMPTY_GIT_TREE_SHA:
            return []
        raise RepoLocalError("no_diff=true cannot compare a claim base without a current HEAD")
    if head == base:
        return []
    if base != EMPTY_GIT_TREE_SHA:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{base}^{{commit}}"], cwd=repo, text=True, capture_output=True, check=False,
        )
        if exists.returncode != 0:
            raise RepoLocalError(f"claim.base_commit is not available: {base}")
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, head],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if ancestor.returncode == 1:
            raise RepoLocalError(f"no_diff=true claim base commit is not an ancestor of HEAD: {base}")
        if ancestor.returncode != 0:
            raise RepoLocalError(
                "no_diff=true could not verify claim base ancestry: " + (ancestor.stderr.strip() or base)
            )
    result = subprocess.run(
        ["git", "diff", "--name-only", base, head, "--"], cwd=repo, text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise RepoLocalError(result.stderr.strip() or "cannot compare claim base with current HEAD")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def finish_changed_files(repo: Path, task: dict[str, Any], fields: dict[str, str]) -> tuple[list[str], bool]:
    explicit = fields.get("changed_files") or fields.get("changed") or fields.get("files")
    if explicit:
        return split_evidence_list(explicit), False
    no_diff_value = (fields.get("no_diff") or fields.get("no_changes") or "").lower()
    status_paths = [path for _code, path in git_status(repo)]
    if no_diff_value in {"1", "true", "yes", "ok"}:
        product_paths = [path for path in status_paths if not lifecycle_only_path(path)]
        if product_paths:
            raise RepoLocalError("no_diff=true contradicts dirty worktree paths: " + ", ".join(product_paths))
        committed_product_paths = [path for path in committed_paths_since_claim(repo, task) if not lifecycle_only_path(path)]
        if committed_product_paths:
            raise RepoLocalError(
                "no_diff=true contradicts committed changes since claim: " + ", ".join(committed_product_paths)
            )
        return [], True
    return status_paths, not status_paths


def task_requires_review_evidence(task: dict[str, Any]) -> bool:
    if task.get("execution_mode", "mechanical") == "agent":
        return True
    modify_scope = [str(item) for item in task.get("scope", {}).get("modify", []) or []]
    return any(not item.startswith(".go/") for item in modify_scope)


def build_finish_evidence(repo: Path, task: dict[str, Any], agent: str, summary: str) -> dict[str, Any]:
    fields = parse_finish_evidence_fields(summary)
    changed_files, no_diff = finish_changed_files(repo, task, fields)
    verification_text = fields.get("verification") or fields.get("validation") or fields.get("verify") or fields.get("check")
    result_text = fields.get("verification_result") or fields.get("result") or fields.get("status")
    critic_text = fields.get("critic") or fields.get("critic_outcome") or fields.get("review") or fields.get("reviewer") or fields.get("reviewer_outcome")
    skip_reason = fields.get("review_skip_reason") or fields.get("critic_skip_reason") or fields.get("skip_reason")
    billing_mode = (fields.get("billing_mode") or "unknown").lower()
    if billing_mode not in {"api", "subscription", "free", "unknown"}:
        billing_mode = "unknown"
    api_equivalent_cost = fields.get("api_equivalent_cost_usd") or fields.get("api_equivalent_cost")
    provider_invoice_cost = fields.get("provider_invoice_cost_usd") or fields.get("provider_invoice_cost")
    usage = {
        "schema": "go-workflow.usage-attribution.v1",
        "runtime_kind": fields.get("runtime_kind") or fields.get("runtime") or "unknown",
        "billing_mode": billing_mode,
        "owner": fields.get("owner") or fields.get("run_owner") or fields.get("task_owner") or agent,
        "model": fields.get("model"),
        "token_estimate": fields.get("token_estimate") or fields.get("tokens") or fields.get("context_tokens"),
        "api_equivalent_cost_usd": api_equivalent_cost,
        "provider_invoice_cost_usd": provider_invoice_cost if billing_mode == "api" else None,
        "cost_label": "provider_invoice_cost_usd" if billing_mode == "api" else "api_equivalent_not_invoice_cost",
    }
    return {
        "schema": "go-workflow.finish-evidence.v1",
        "created_at": now_iso(),
        "task_id": task.get("id"),
        "agent": agent,
        "summary": summary,
        "changed_files": changed_files,
        "no_diff": no_diff,
        "git": {
            "claim_base_commit": (task.get("claim") or {}).get("base_commit"),
            "observed_head_commit": git_head_commit(repo),
        },
        "verification": {
            "command": fields.get("verification_command") or fields.get("command") or verification_text,
            "result": result_text or verification_text,
        },
        "runtime": {
            "agent": agent,
            "runtime": fields.get("runtime"),
            "model": fields.get("model"),
            "billing_mode": billing_mode,
        },
        "usage": usage,
        "review": {
            "outcome": critic_text or ("skipped" if skip_reason else None),
            "skip_reason": skip_reason,
        },
    }


def finish_evidence_findings(task: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    if evidence.get("task_id") != task.get("id"):
        findings.append("finish evidence must record matching task id")
    if not evidence.get("changed_files") and not evidence.get("no_diff"):
        findings.append("finish evidence must record changed_files or no_diff=true")
    verification = evidence.get("verification") or {}
    if not verification.get("command") or not verification.get("result"):
        findings.append("finish evidence must record verification command and result")
    runtime = evidence.get("runtime") or {}
    if not runtime.get("agent"):
        findings.append("finish evidence must record runtime/agent attribution")
    review = evidence.get("review") or {}
    if task_requires_review_evidence(task) and not (review.get("outcome") or review.get("skip_reason")):
        findings.append("non-trivial finish evidence must record reviewer/critic outcome or explicit skip reason")
    return findings


def managed_task_transition(repo: Path, path: str) -> bool:
    if not path.startswith(".go/tasks/"):
        return False
    root = go_root(repo)
    return any((root / "tasks" / state / Path(path).name).is_file() for state in ("open", "active", "blocked", "done"))


def classify_dirty(repo: Path, owned_patterns: list[str]) -> dict[str, list[str]]:
    result = {"blocking": [], "report_only": []}
    for code, path in git_status(repo):
        reason = ""
        if "U" in code or code in {"AA", "DD"}:
            reason = "merge conflict"
        elif BLOCK_SECRET_RE.search(path):
            reason = "secret-looking path"
        elif (code.strip().startswith("D") or code.endswith("D")) and not managed_task_transition(repo, path):
            reason = "delete requires explicit review"
        elif path.startswith(".go/locks/"):
            reason = "workflow lock state"
        elif path_matches(path, owned_patterns):
            reason = "owned-path dirty state"
        if reason:
            result["blocking"].append(f"{code} {path} — {reason}")
        else:
            result["report_only"].append(f"{code} {path} — unrelated dirty state")
    return result


def event(task_id: str, event_name: str, agent: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schema": EVENT_SCHEMA, "kind": "event", "event": event_name, "created_at": now_iso(), "task_id": task_id, "agent": agent, "data": data or {}}


def write_missing_text(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def ensure_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        result = subprocess.run(["git", "init", "-q"], cwd=repo, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise RepoLocalError(result.stderr.strip() or "git init failed")


def write_repo_complete_starter(repo: Path, name: str) -> list[str]:
    created: list[str] = []
    files = {
        "README.md": f"# {name}\n\nRepo-local agent workflow project.\n\n## Development\n\n```bash\nmake check\n```\n",
        ".gitignore": ".env\n.env.*\n.DS_Store\n__pycache__/\n.pytest_cache/\nnode_modules/\ndist/\nbuild/\n",
        "LICENSE": "MIT License\n\nCopyright (c) 2026 Viggo Meesters\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\nof this software and associated documentation files (the \"Software\"), to deal\nin the Software without restriction, including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.\n",
        "SECURITY.md": "# Security\n\nDo not commit credentials, private data, tokens, cookies, or production secrets.\n",
        "CONTRIBUTING.md": "# Contributing\n\nUse repo-local `.go/` tasks, verify before finishing, and keep changes scoped.\n",
        "CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n- Initial repo-local spike scaffold.\n",
        "Makefile": "GO_STACK ?= ../go-workflow-stack\n\n.PHONY: check\ncheck:\n\tpython3 $(GO_STACK)/cli/go.py validate .\n\tpython3 $(GO_STACK)/cli/go.py readback .\n",
        "scripts/check.sh": "#!/usr/bin/env bash\nset -euo pipefail\nmake check\n",
        "go": "#!/usr/bin/env bash\nset -euo pipefail\nREPO_ROOT=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\nSTACK=\"${GO_STACK:-}\"\nif [ -z \"$STACK\" ] || [ ! -f \"$STACK/cli/go.py\" ]; then\n  for candidate in \"$REPO_ROOT/../go-workflow-stack\" \"$HOME/github/go-workflow-stack\" \"$HOME/Dev/go-workflow-stack\"; do\n    if [ -f \"$candidate/cli/go.py\" ]; then STACK=\"$candidate\"; break; fi\n  done\nfi\nif [ -z \"$STACK\" ] || [ ! -f \"$STACK/cli/go.py\" ]; then\n  echo \"go-workflow-stack not found; set GO_STACK or clone it beside this repository\" >&2\n  exit 2\nfi\nexport GO_STACK=\"$STACK\"\nexec python3 \"$STACK/cli/go.py\" \"$@\"\n",
    }
    for rel, content in files.items():
        if write_missing_text(repo / rel, content):
            created.append(rel)
    check = repo / "scripts" / "check.sh"
    if check.exists():
        check.chmod(check.stat().st_mode | 0o111)
    launcher = repo / "go"
    if launcher.exists():
        launcher.chmod(launcher.stat().st_mode | 0o111)
    return created


def parse_spike_task(value: str) -> tuple[str, str]:
    task_id, summary = parse_pipe_fields(value, 2, "--task")
    return slugify(task_id), summary


def default_spike_tasks() -> list[tuple[str, str]]:
    return [
        ("write-vision", "Write or refine the repo-local vision"),
        ("write-architecture-principles", "Write architecture principles and constraints"),
        ("repo-complete", "Bootstrap repo-complete hygiene and local checks"),
        ("implementation-slice", "Build the first working implementation slice"),
        ("verification", "Run focused and repository verification"),
        ("hardening-devil-review", "Run recheck, devil review, and hardening pass"),
        ("self-reflect", "Self-reflect on architecture and workflow improvements"),
        ("handoff-summary", "Write concise user handoff and next steps"),
    ]


def scaffold_lifecycle_settings(repo, args):
    path = getattr(args, 'lifecycle_settings', '')
    if not path: return None
    from go_workflow.migrations import configured_project, MigrationError
    settings = load_json(Path(path))
    existing = go_root(repo) / 'project.json'
    if existing.exists() and load_json(existing).get('id') != 'go-project-template':
        raise RepoLocalError('Use migrate --lifecycle --config for an existing project; scaffold settings are for a new project')
    try: configured_project(load_json(FIXTURE_ROOT / 'project.json'), settings)
    except MigrationError as exc: raise RepoLocalError(str(exc)) from exc
    return settings


def scaffold_project(project, settings):
    if settings is None: return project
    from go_workflow.migrations import configured_project
    return configured_project(project, settings)


def cmd_adopt(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    lifecycle = scaffold_lifecycle_settings(repo, args)
    repo.mkdir(parents=True, exist_ok=True)
    root = go_root(repo)
    if root.exists() and any(root.iterdir()) and not args.force:
        raise RepoLocalError(f"{root} already exists; use status/task create, or pass --force to replace")
    if args.force and root.exists():
        shutil.rmtree(root)
    ensure_go_dirs(root)
    project_id = slugify(args.project_id or repo.name)
    name = args.name or repo.name
    default_verification = args.verification or ["git diff --check"]
    dump_json(root / "project.json", scaffold_project({
        "schema": PROJECT_SCHEMA,
        "kind": "project",
        "id": project_id,
        "name": name,
        "source_of_truth": "repo-local",
        "contract_version": CURRENT_CONTRACT_VERSION,
        "project_mode": "project",
        "required_stack_version": STACK_VERSION,
        "stack_ref": STACK_REF,
        "default_verification": default_verification,
        "links": {"repo": args.repo_url or ""},
    }, lifecycle))
    dump_json(root / "architecture-principles.json", {
        "schema": ARCH_SCHEMA,
        "kind": "architecture_principles",
        "project": project_id,
        "principles": parse_principles(args.principle or []),
    })
    dump_json(root / "vision.json", {
        "schema": VISION_SCHEMA,
        "kind": "vision",
        "project": project_id,
        "status": "active",
        "north_star": args.north_star or f"{name} is understandable and operable from its repo-local .go contract.",
        "wedge": args.wedge or "Repo-local project state with clone-readable agent continuity.",
        "target_user": args.target_user or "Project maintainers and future agents.",
        "core_promise": args.core_promise or "A fresh clone can explain current direction, constraints, hierarchy, and next work.",
        "product_principles": args.product_principle or ["repo-local", "json-first", "proof-first"],
        "non_goals": args.non_goal or ["no central execution database", "no broad migration by default"],
        "success_metrics": args.success_metric or ["go-workflow-stack validate passes", "go-workflow-stack readback is useful"],
    })
    dump_json(root / "hierarchy.json", parse_hierarchy(args.feature_group or [], args.feature or [], project_id))
    for jsonl in [root / "runs" / "events.jsonl", root / "evidence" / "events.jsonl", root / "decisions" / "events.jsonl"]:
        jsonl.touch(exist_ok=True)
    apply_agents_gateway(repo)
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"adopted: {root}")
    return 0


def spike_task_scope(scope_name: str) -> dict[str, list[str]]:
    if scope_name == "docs":
        return {"read": [".go/**", "README.md", "docs/**"], "modify": [".go/**", "README.md", "docs/**", "Makefile"]}
    return {
        "read": [".go/**", "README.md", "docs/**", "cli/go.py", "tests/**", "Makefile"],
        "modify": [".go/**", "README.md", "docs/**", "cli/go.py", "tests/**", "Makefile"],
    }


def cmd_spike(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    lifecycle = scaffold_lifecycle_settings(repo, args)
    ensure_git_repo(repo)
    name = args.name or repo.name.replace("-", " ").title()
    project_id = slugify(args.project_id or repo.name)
    root = go_root(repo)
    created_repo_files: list[str] = [] if args.skip_repo_complete else write_repo_complete_starter(repo, name)
    if (root / "project.json").is_file():
        existing_project = load_json(root / "project.json")
        if existing_project.get("id") == "go-project-template" and project_id != "go-project-template":
            shutil.rmtree(root)
    if not (root / "project.json").exists():
        ensure_go_dirs(root)
        dump_json(root / "project.json", scaffold_project({
            "schema": PROJECT_SCHEMA,
            "kind": "project",
            "id": project_id,
            "name": name,
            "source_of_truth": "repo-local",
            "contract_version": CURRENT_CONTRACT_VERSION,
            "project_mode": "project",
            "required_stack_version": STACK_VERSION,
            "stack_ref": STACK_REF,
            "default_verification": args.verification or ["make check"],
            "links": {"repo": args.repo_url or ""},
        }, lifecycle))
        dump_json(root / "architecture-principles.json", {
            "schema": ARCH_SCHEMA,
            "kind": "architecture_principles",
            "project": project_id,
            "principles": parse_principles(args.principle or []),
        })
        brief = args.brief or f"{name} repo-local spike."
        dump_json(root / "vision.json", {
            "schema": VISION_SCHEMA,
            "kind": "vision",
            "project": project_id,
            "status": "active",
            "north_star": args.north_star or f"{name} can be designed, built, verified, and continued from repo-local .go state.",
            "wedge": args.wedge or brief,
            "target_user": args.target_user or "Viggo and future autonomous agents continuing this project.",
            "core_promise": args.core_promise or "A single go spike/go auto loop can turn rough intent into repo, vision, principles, tasks, verified work, reflection, and concise handoff.",
            "product_principles": args.product_principle or ["repo-local", "proof-first", "autonomous-but-scoped", "feedback-turns-into-tasks"],
            "non_goals": args.non_goal or ["no hidden central execution state", "no unbounded public/destructive actions", "no technical fluff handoff"],
            "success_metrics": args.success_metric or ["repo validates", "next tasks are claimable", "go auto plan is explicit"],
        })
        epics = args.epic or ["delivery|Delivery"]
        dump_json(root / "hierarchy.json", parse_hierarchy(epics, [], project_id))
        for jsonl in [root / "runs" / "events.jsonl", root / "evidence" / "events.jsonl", root / "decisions" / "events.jsonl"]:
            jsonl.touch(exist_ok=True)
        apply_agents_gateway(repo)
    else:
        apply_agents_gateway(repo)
        errors = validate_repo(repo)
        if errors:
            raise RepoLocalError("existing .go state is invalid:\n- " + "\n- ".join(errors))
        project_id = str(load_json(root / "project.json").get("id") or project_id)
        for epic_value in args.epic or []:
            epic_id, title = parse_pipe_fields(epic_value, 2, "--epic")
            hierarchy = load_json(root / "hierarchy.json")
            epics = hierarchy_epics(hierarchy)
            if not any(epic.get("id") == slugify(epic_id) for epic in epics):
                epics.append({"id": slugify(epic_id), "title": title, "features": [], "tasks": []})
                set_hierarchy_epics(hierarchy, epics)
                dump_json(root / "hierarchy.json", hierarchy)
    hierarchy = load_json(root / "hierarchy.json")
    epics = hierarchy_epics(hierarchy)
    if not epics:
        epics = [{"id": "delivery", "title": "Delivery", "features": [], "tasks": []}]
        set_hierarchy_epics(hierarchy, epics)
        dump_json(root / "hierarchy.json", hierarchy)
    target_epic = slugify(args.target_epic or str(epics[0]["id"]))
    task_values = [parse_spike_task(value) for value in args.task] if args.task else default_spike_tasks()
    default_task_scope = spike_task_scope(args.task_scope)
    created_tasks: list[str] = []
    project = load_json(root / "project.json")
    for order, (task_id, summary) in enumerate(task_values, start=1):
        if any(task_path(root, state, task_id).exists() for state in ("open", "active", "blocked", "done")):
            continue
        task = {
            "schema": TASK_SCHEMA,
            "kind": "task",
            "execution_mode": args.execution_mode,
            "shareable_delivery": "auto",
            "id": task_id,
            "project": project_id,
            "status": "open",
            "summary": summary,
            "description": summary,
            "order": order,
            "scope": default_task_scope,
            "acceptance": [
                f"Outcome is observable: {summary}.",
                "All task verification commands pass and evidence is recorded.",
            ],
            "verification": project.get("default_verification") or ["make check"],
            "claim": {"agent": None, "claimed_at": None},
            "evidence": [],
        }
        apply_intake_contract(repo, task)
        dump_json(task_path(root, "open", task_id), task)
        append_task_to_epic(root, target_epic, task_id)
        created_tasks.append(task_id)
    append_jsonl(root / "decisions" / "events.jsonl", event(
        "go-spike",
        "decision.recorded",
        args.agent,
        {
            "decision_id": "go-spike-created",
            "title": "Initialize go spike contract",
            "status": "accepted",
            "context": args.brief or "go spike command",
            "decision": "Use repo-local .go vision, principles, epics, tasks, evidence, and go auto loop for this project.",
            "consequences": ["Feedback becomes tasks", "go auto can continue from repo state"],
        },
    ))
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("spike produced invalid .go state:\n- " + "\n- ".join(errors))
    result = {
        "repo": str(repo),
        "project_id": project_id,
        "created_repo_files": created_repo_files,
        "created_tasks": created_tasks,
        "next": None if not open_tasks(repo) else open_tasks(repo)[0][1]["id"],
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"spike: {repo}")
        print(f"project: {project_id}")
        print("created_tasks: " + (", ".join(created_tasks) if created_tasks else "none"))
        print("next: " + str(result["next"] or "none"))
    return 0


def arg_int(args: argparse.Namespace, name: str, default: int) -> int:
    return int(getattr(args, name, default) or default)


def build_loop_plan(repo: Path, args: argparse.Namespace, mode: str = "go-auto") -> dict[str, Any]:
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError(f"cannot run {mode} on invalid .go state:\n- " + "\n- ".join(errors))
    root = go_root(repo)
    project = load_json(root / "project.json")
    max_tasks = max(arg_int(args, "max_tasks", 3), 1)
    max_minutes = max(arg_int(args, "max_minutes", 45), 1)
    max_commands = max(arg_int(args, "max_commands", max_tasks * 12), 1)
    command_timeout_seconds = max(arg_int(args, "command_timeout_seconds", 900), 1)
    checkpoint_every_tasks = max(arg_int(args, "checkpoint_every_tasks", 1), 1)
    campaign = None
    if getattr(args, "campaign", ""):
        previous = Path(args.previous_campaign) if getattr(args, "previous_campaign", "") else None
        campaign, campaign_tasks = plan_campaign(
            repo,
            Path(args.campaign),
            sys.modules[__name__],
            previous_path=previous,
        )
        tasks = campaign_tasks[:max_tasks]
    else:
        tasks = [task[1] for task in open_tasks(repo)[:max_tasks]]
    is_loop = mode == "go-loop"
    stop_conditions = [
        "no_open_tasks_and_no_self_reflect_follow_up",
        "repository_safety_gate",
        "external_authority_required",
        "outcome_ambiguity",
        "scope_tradeoff_requires_direction",
    ]
    execution_policy = {
        "ask_policy": "do-not-ask-when-safe-default-exists",
        "authority": "high-autonomy-bounded-by-repo-scope-and-human-gates",
        "capacity": plan_capacity(tasks, requested_parallel_builders=max_tasks),
        "may_create_follow_up_tasks": True,
        "may_continue_after_self_reflect": True,
        "may_escalate_to_go_loop": not is_loop,
        "allowed_autonomous_actions": [
            "claim_and_execute_open_tasks",
            "edit_files_within_task_scope",
            "run_tests_checks_and_smokes",
            "fix_verification_failures_within_scope",
            "run_recheck_devil_hardening",
            "append_evidence_and_decisions",
            "create_same_scope_follow_up_tasks",
            "summarize_compactly_without_waiting_for_prompt",
        ],
        "human_gates": stop_conditions[1:],
    }
    preflight = build_auto_preflight(repo, tasks, max_tasks)
    run_envelope = {
        "schema": "go-workflow.auto-run-envelope.v1",
        "result_schema": "go-workflow.auto-run-result.v1",
        "run_until": "done_or_blocker_or_budget_or_safety_gate",
        "budget": {"max_tasks": max_tasks, "max_minutes": max_minutes, "max_commands": max_commands, "command_timeout_seconds": command_timeout_seconds, "checkpoint_every_tasks": checkpoint_every_tasks, "summary_chars": args.summary_chars},
        "preflight": preflight,
        "checkpoint_after": ["checkpoint_every_tasks", "blocker", "budget_exhausted", "safety_gate", "done"],
        "telegram_policy": {"default": "silent_until_done_blocker_or_checkpoint", "checkpoint_every_tasks": checkpoint_every_tasks, "summary_chars": args.summary_chars},
        "final_result_fields": ["status", "completed_tasks", "blocked_task", "evidence", "checks", "completion_audit", "summary", "next_action"],
    }
    result = {
        "mode": mode,
        "repo": str(repo),
        "project_id": project.get("id"),
        "next_tasks": [task["id"] for task in tasks],
        "control_handoff": True,
        "autonomy": "control-handed-off-until-blocker" if is_loop else "high-autonomy-bounded-batch-with-loop-escalation",
        "can_escalate_to": [] if is_loop else ["go-loop"],
        "continues_beyond_initial_tasks": is_loop,
        "execution_policy": execution_policy,
        "run_envelope": run_envelope,
        "loop": ["route", "status", "contract-repair-if-needed", "next-or-create-task", "claim", "execute", "verify", "recheck", "devil", "repair", "verify", "commit-or-ship", "finish", "self-reflect", "continue-or-block"],
        "agent_contract": {
            "execute": "The invoking coding agent does not hand commands back to Viggo. It starts tool calls now: repair or confirm .go contract, create/claim one task, execute inside scope, verify, critic/recheck, repair if needed, finish with evidence, then continue until done, a repository gate, or budget.",
            "contract_preflight": "Before implementation, ensure vision/end goal, architecture principles, hierarchy, executable task, acceptance, and verification are present or create/repair them.",
            "task_design_review": review_contract(),
            "control": "Viggo has handed off control with go/go-auto: do not stop after one phase and wait for another go; keep executing until done, blocker, budget, or safety gate.",
            "loop_escalation": "go-auto may invoke go-loop when self-reflect creates follow-up work, verification/review fails, first green is not trustworthy, or the project needs continued autonomous repair beyond the initial batch.",
            "feedback": "New Viggo input is converted into .go tasks/decisions before another go auto/go loop pass.",
            "summary_max_chars": args.summary_chars,
            "telegram_policy": "quiet until done/blocker/checkpoint; do not stream command transcripts",
            "stop_conditions": stop_conditions,
        },
    }
    if campaign is not None:
        result["campaign"] = campaign
    return result


def task_contract_findings(task: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    acceptance = task.get("acceptance") or []
    generic_acceptance = {
        "task result is implemented and verified.",
        "task result is implemented and verified with evidence.",
    }
    if not acceptance:
        findings.append("acceptance criteria are missing")
    elif any(str(item).strip().lower() in generic_acceptance for item in acceptance):
        findings.append("acceptance criteria are generic and cannot prove the requested outcome")
    if not task.get("verification"):
        findings.append("verification commands are missing")
    scope = task.get("scope") or {}
    if not isinstance(scope, dict) or not isinstance(scope.get("modify"), list):
        findings.append("task modify scope is missing or invalid")
    findings.extend(intake_claim_findings(task))
    return findings


def intake_claim_findings(task: dict[str, Any]) -> list[str]:
    """Return only intake authority blockers, preserving legacy manual claims."""
    findings: list[str] = []
    intake = task.get("intake")
    if isinstance(intake, dict):
        authority = intake.get("authority") or {}
        if authority.get("mode") != "execute" or authority.get("implementation_authorized") is not True:
            findings.append(f"{authority.get('mode', 'unknown')} intake is not execution authority")
        questions = intake.get("unresolved_question_ids") or []
        if questions:
            findings.append("intake has unresolved user questions: " + ", ".join(map(str, questions)))
    return findings


def unfinished_task_ids(repo: Path) -> dict[str, list[str]]:
    return task_state_unfinished_task_ids(go_root(repo))


def pending_review_task_ids(repo: Path) -> list[str]:
    return task_state_pending_review_task_ids(go_root(repo))


def build_auto_preflight(repo: Path, selected_tasks: list[dict[str, Any]], max_tasks: int) -> dict[str, Any]:
    root = go_root(repo)
    dirty_entries: list[str] = []
    blockers: list[str] = []
    for code, path in git_status(repo):
        entry = f"{code} {path}"
        dirty_entries.append(entry)
        conflict = "U" in code or code in {"AA", "DD"}
        secret_like = bool(BLOCK_SECRET_RE.search(path))
        destructive = code.strip().startswith("D") or code.endswith("D")
        task_transition = destructive and managed_task_transition(repo, path)
        lock_state = path.startswith(".go/locks/")
        if conflict:
            blockers.append(f"{entry} — merge conflict")
        elif secret_like:
            blockers.append(f"{entry} — secret-looking path")
        elif destructive and not task_transition:
            blockers.append(f"{entry} — delete requires explicit review")
        elif lock_state:
            blockers.append(f"{entry} — workflow lock state")
    lock_files = [] if not (root / "locks").is_dir() else [relative(repo, path) for path in sorted((root / "locks").glob("*")) if path.is_file()]
    for lock in lock_files:
        blockers.append(f"{lock} — active workflow lock")
    contract_findings = [
        {"task_id": task.get("id"), "findings": findings}
        for task in selected_tasks
        if (findings := task_contract_findings(task) + architecture_claim_findings(root, task))
    ]
    unfinished = unfinished_task_ids(repo)
    return {
        "valid_go_state": True,
        "open_task_count": len(open_tasks(repo)),
        "selected_task_count": len(selected_tasks),
        "max_tasks": max_tasks,
        "dirty_entries": dirty_entries,
        "lock_files": lock_files,
        "human_gate_required": bool(blockers),
        "human_gate_blockers": blockers,
        "contract_gate_required": bool(contract_findings),
        "contract_findings": contract_findings,
        "unfinished_tasks": unfinished,
    }


def run_shell_with_timeout(repo: Path, command: str, env: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
    command = process_command(command, Path(__file__))
    process = subprocess.Popen(
        command,
        cwd=repo,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr, "timed_out": False}
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        return {
            "returncode": 124,
            "stdout": stdout,
            "stderr": (stderr + f"\ncommand timed out after {timeout_seconds}s").strip(),
            "timed_out": True,
        }


def run_verification_commands(repo: Path, task: dict[str, Any], command_budget: int | None = None, timeout_seconds: int = 900) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    commands_run = 0
    for command in task.get("verification", []) or []:
        if command_budget is not None and commands_run >= command_budget:
            checks.append({
                "task_id": task.get("id"),
                "command": command,
                "returncode": 124,
                "stdout": "",
                "stderr": "command budget exhausted before verification command",
                "budget_exhausted": True,
            })
            break
        env = os.environ.copy()
        with tempfile.TemporaryDirectory(prefix="go-verify-pycache-") as pycache_dir:
            env["PYTHONPYCACHEPREFIX"] = pycache_dir
            completed = run_shell_with_timeout(repo, command, env, timeout_seconds)
        commands_run += 1
        checks.append({
            "task_id": task.get("id"),
            "command": command,
            "returncode": completed["returncode"],
            "stdout": completed["stdout"][-2000:],
            "stderr": completed["stderr"][-2000:],
            "timed_out": completed["timed_out"],
        })
        if completed["returncode"] != 0:
            break
    if not checks:
        checks.append({"task_id": task.get("id"), "command": None, "returncode": 0, "stdout": "", "stderr": "no verification commands configured"})
    return checks


def finish_task_record(
    repo: Path,
    root: Path,
    active_path: Path,
    task: dict[str, Any],
    agent: str,
    evidence_summary: str,
    transaction_id: str | None = None,
) -> Path:
    with repository_lock(root, f"task-{task['id']}"):
        if not active_path.is_file():
            raise StateLockError(f"active task disappeared before finish: {task['id']}")
        require_completion(repo, task)
        finish_evidence = build_finish_evidence(repo, task, agent, evidence_summary)
        findings = finish_evidence_findings(task, finish_evidence)
        if findings:
            raise RepoLocalError("finish blocked by incomplete evidence attribution:\n- " + "\n- ".join(findings))
        task["status"] = "done"
        task["work_status"] = "completed"
        task["review_status"] = "review" if task_requires_review_evidence(task) else "approved"
        task.setdefault("evidence", []).append(finish_evidence)
        target = task_path(root, "done", task["id"])
        atomic_move_json(active_path, target, task)
        event_data: dict[str, Any] = {"evidence": finish_evidence}
        if transaction_id:
            event_data["transaction_id"] = transaction_id
        append_jsonl(root / "evidence" / "events.jsonl", event(task["id"], "task.finished", agent, event_data))
    return target


def task_requires_shareable_delivery(task: dict[str, Any]) -> bool:
    policy = str(task.get("shareable_delivery") or "auto")
    if policy == "required":
        return True
    if policy == "none":
        return False
    modify_paths = [str(path) for path in (task.get("scope") or {}).get("modify", [])]
    substantive_paths = [path for path in modify_paths if path != ".go" and not path.startswith(".go/")]
    return task.get("execution_mode") == "agent" and len(task.get("acceptance") or []) >= 2 and bool(substantive_paths)


def automatic_delivery_plan(repo: Path, task: dict[str, Any]) -> dict[str, Any] | None:
    if not task_requires_shareable_delivery(task):
        return None
    root = go_root(repo)
    hierarchy = load_json(root / "hierarchy.json")
    owning_epics = [epic for epic in hierarchy_epics(hierarchy) if task["id"] in epic_task_ids(epic)]
    if len(owning_epics) != 1:
        raise RepoLocalError(
            f"shareable delivery requires exactly one owning epic for task {task['id']}; found {len(owning_epics)}"
        )
    epic = owning_epics[0]
    releases: list[tuple[int, str]] = []
    for manifest_path in sorted((root / "deliveries").glob("*/manifest.json")):
        try:
            manifest = load_json(manifest_path)
            if manifest.get("assignment", {}).get("id") != epic.get("id"):
                continue
            releases.append((int(manifest.get("release", {}).get("version") or 0), str(manifest.get("delivery_id") or manifest_path.parent.name)))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    latest_version, latest_delivery = max(releases, default=(0, ""))
    version = latest_version + 1
    delivery_id = f"{slugify(str(epic['id']))}-v{version}"
    return {
        "epic": str(epic["id"]),
        "version": version,
        "delivery_id": delivery_id,
        "supersedes": latest_delivery or "",
        "target": root / "deliveries" / delivery_id,
    }


def build_automatic_delivery(repo: Path, plan: dict[str, Any]) -> dict[str, Any]:
    args = argparse.Namespace(
        repo=str(repo), epic=plan["epic"], version=plan["version"], delivery_id=plan["delivery_id"],
        summary="", status="released", disclosure="restricted", supersedes=plan["supersedes"],
        delivered=[], excluded=[], limitation=[], next_step=[], delivery_lock_held=True,
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = cmd_delivery_build(args)
    if result != 0:
        raise RepoLocalError(f"automatic shareable delivery build failed with exit code {result}")
    try:
        return json.loads(output.getvalue())
    except json.JSONDecodeError as exc:
        raise RepoLocalError("automatic shareable delivery build returned invalid JSON") from exc


def approve_completed_task(
    repo: Path,
    root: Path,
    done_path: Path,
    task: dict[str, Any],
    record: dict[str, Any],
    delivery_lock_held: bool = False,
) -> dict[str, Any] | None:
    require_completion(repo, task)
    if task.get("status") != "done" or task.get("work_status") != "completed":
        raise RepoLocalError("approval requires completed work in done state")
    if task.get("review_status") == "approved":
        latest = (task.get("review_history") or [{}])[-1]
        return latest.get("delivery") if isinstance(latest, dict) else None
    plan = automatic_delivery_plan(repo, task)
    if plan:
        if delivery_lock_held:
            return approve_completed_task_with_plan(repo, root, done_path, task, record, plan)
        try:
            with repository_lock(root, f"delivery-{plan['epic']}"):
                return approve_completed_task_with_plan(repo, root, done_path, task, record, automatic_delivery_plan(repo, task))
        except StateLockError as exc:
            raise RepoLocalError(str(exc)) from exc
    return approve_completed_task_with_plan(repo, root, done_path, task, record, None)


def approve_completed_task_with_plan(
    repo: Path,
    root: Path,
    done_path: Path,
    task: dict[str, Any],
    record: dict[str, Any],
    plan: dict[str, Any] | None,
) -> dict[str, Any] | None:
    require_completion(repo, task)
    original = json.loads(json.dumps(task))
    transaction_id = str(record.get("transaction_id") or uuid.uuid4().hex)
    record["transaction_id"] = transaction_id
    task["review_status"] = "approved"
    task.setdefault("review_history", []).append(record)
    dump_json(done_path, task)
    delivery: dict[str, Any] | None = None
    try:
        if plan:
            delivery = build_automatic_delivery(repo, plan)
            record["delivery"] = delivery
            dump_json(done_path, task)
        append_jsonl(root / "evidence" / "events.jsonl", event(task["id"], "task.reviewed", record["agent"], record))
        if delivery:
            append_jsonl(root / "runs" / "events.jsonl", event(task["id"], "delivery.built", record["agent"], {
                "transaction_id": transaction_id,
                **delivery,
            }))
    except Exception:
        dump_json(done_path, original)
        remove_jsonl_events_locked(
            root / "evidence" / "events.jsonl",
            lambda payload: (payload.get("data") or {}).get("transaction_id") == transaction_id,
        )
        remove_jsonl_events_locked(
            root / "runs" / "events.jsonl",
            lambda payload: (payload.get("data") or {}).get("transaction_id") == transaction_id,
        )
        if plan:
            shutil.rmtree(plan["target"], ignore_errors=True)
        raise
    return delivery


def approve_task_after_passed_critic(
    root: Path,
    done_path: Path,
    task_id: str,
    agent: str,
    transaction_id: str | None = None,
    delivery_lock_held: bool = False,
) -> dict[str, Any] | None:
    if not delivery_lock_held:
        task_snapshot = load_json(done_path)
        plan = automatic_delivery_plan(root.parent, task_snapshot)
        if plan:
            try:
                with repository_lock(root, f"delivery-{plan['epic']}"):
                    return approve_task_after_passed_critic(
                        root,
                        done_path,
                        task_id,
                        agent,
                        transaction_id=transaction_id,
                        delivery_lock_held=True,
                    )
            except StateLockError as exc:
                raise RepoLocalError(str(exc)) from exc
    with repository_lock(root, f"task-{task_id}"):
        task = load_json(done_path)
        if task.get("status") != "done" or task.get("work_status") != "completed":
            raise RepoLocalError("critic approval requires completed work in done state")
        if task.get("review_status") != "review":
            latest = (task.get("review_history") or [{}])[-1]
            return latest.get("delivery") if isinstance(latest, dict) else None
        record = {
            "created_at": now_iso(),
            "agent": agent,
            "status": "approved",
            "evidence": "go-loop critic phase passed with no blocking findings",
        }
        if transaction_id:
            record["transaction_id"] = transaction_id
        return approve_completed_task(
            root.parent,
            root,
            done_path,
            task,
            record,
            delivery_lock_held=delivery_lock_held,
        )


def restore_active_after_failed_ship(
    root: Path,
    active_path: Path,
    done_path: Path,
    active_task: dict[str, Any],
    evidence_path: Path,
    transaction_id: str,
) -> None:
    with repository_lock(root, f"task-{active_task['id']}"):
        if done_path.is_file():
            current = load_json(done_path)
            baseline_history = active_task.get("review_history", []) or []
            transaction_deliveries = [
                record.get("delivery") for record in (current.get("review_history", []) or [])[len(baseline_history):]
                if record.get("transaction_id") == transaction_id and isinstance(record.get("delivery"), dict)
            ]
            concurrent_history = [
                record for record in (current.get("review_history", []) or [])[len(baseline_history):]
                if record.get("transaction_id") != transaction_id
            ]
            restored = json.loads(json.dumps(active_task))
            if concurrent_history:
                restored.setdefault("review_history", []).extend(concurrent_history)
                restored["review_status"] = current.get("review_status", restored.get("review_status", "none"))
            atomic_move_json(done_path, active_path, restored)
            for delivery in transaction_deliveries:
                delivery_id = str(delivery.get("delivery_id") or "")
                if delivery_id:
                    shutil.rmtree(root / "deliveries" / delivery_id, ignore_errors=True)
        elif active_path.is_file():
            current = load_json(active_path)
            if current.get("status") != "active" or current.get("work_status") != "in_progress":
                atomic_json(active_path, active_task)
        else:
            atomic_json(active_path, active_task)
        remove_jsonl_events_locked(
            evidence_path,
            lambda payload: payload.get("task_id") == active_task.get("id")
            and (payload.get("data") or {}).get("transaction_id") == transaction_id,
        )
        remove_jsonl_events_locked(
            root / "runs" / "events.jsonl",
            lambda payload: payload.get("task_id") == active_task.get("id")
            and (payload.get("data") or {}).get("transaction_id") == transaction_id,
        )


def block_task_record(repo: Path, root: Path, active_path: Path, task: dict[str, Any], agent: str, reason: str, checks: list[dict[str, Any]]) -> Path:
    with repository_lock(root, f"task-{task['id']}"):
        if not active_path.is_file():
            raise StateLockError(f"active task disappeared before block: {task['id']}")
        current=load_json(active_path)
        if current.get('id')!=task['id'] or current.get('status')!='active':
            raise StateLockError('Active task identity changed before block')
        task=current
        task["status"] = "blocked"
        task["blocked"] = {"created_at": now_iso(), "agent": agent, "reason": reason}
        target = task_path(root, "blocked", task["id"])
        atomic_move_json(active_path, target, task)
        append_jsonl(root / "runs" / "events.jsonl", event(task["id"], "task.blocked", agent, {"reason": reason, "checks": checks}))
    return target


def format_hook_command(command: str, repo: Path, task: dict[str, Any], attempt: int, strategy: str) -> str:
    replacements = {
        "{repo}": str(repo),
        "{repo_shell}": shell_quote(str(repo)),
        "{task_id}": str(task.get("id", "unknown")),
        "{attempt}": str(attempt),
        "{strategy}": strategy,
    }
    rendered = command
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def build_execution_context(repo: Path, task: dict[str, Any], *, phase: str = 'build') -> dict[str, Any]:
    root = go_root(repo)
    architecture = resolve_applicable_architecture(root, task)
    vision = load_json(root / "vision.json")
    from go_workflow.completion import content_snapshot
    context = {
        "schema": "go-workflow.execution-context.v1",
        "project": load_json(root / "project.json"),
        "vision": vision,
        "architecture_principles": load_json(root / "architecture-principles.json"),
        "hierarchy": load_json(root / "hierarchy.json"),
        "task": task,
        "recent_evidence": load_jsonl_events(root / "evidence" / "events.jsonl", limit=10),
        "recent_decisions": load_jsonl_events(root / "decisions" / "events.jsonl", limit=10),
        "applicable_architecture": architecture,
        "task_design_review": review_contract(),
    }
    if task.get('outcome_tracking_version') == 1:
        context['behavior_review'] = behavior_review_context(
            task, vision, architecture, content_snapshot(repo, task)['digest'], phase=phase,
        )
    if "repository_context" in task:
        context["repository_context"] = select_repository_context(repo, task["repository_context"])
    return context


def run_hook_command(repo: Path, command: str, task: dict[str, Any], attempt: int, strategy: str, hook: str, timeout_seconds: int = 900, require_protocol: bool = False, feedback: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        record = registered_workspace(repo)
        required = ((task.get("execution_contract") or {}).get("workspace") or {}).get("mode") == "task_worktree"
        if required and record is None:
            raise WorkspaceError("Create the explicitly configured task workspace from the control checkout before dispatch")
        if record is not None:
            if task.get("id") != record["task_id"] or (task.get("claim") or {}).get("agent") != record["owner"]:
                raise WorkspaceError("Worker task/owner differs from the registered workspace")
            with execution_lease(repo, record["task_id"], record["owner"], record["run_id"]):
                current = active_task(Path(record["control_repo"]), record["task_id"], record["owner"])
                mutable_evidence = {'requested_outcomes', 'evidence', 'review_history'}
                if ({key: value for key, value in current.items() if key not in mutable_evidence}
                        != {key: value for key, value in task.items() if key not in mutable_evidence}):
                    raise WorkspaceError("Worker task snapshot is stale; reload canonical task state")
                return _run_hook_command(repo, command, current, attempt, strategy, hook, timeout_seconds, require_protocol, feedback)
        return _run_hook_command(repo, command, task, attempt, strategy, hook, timeout_seconds, require_protocol, feedback)
    except (WorkspaceError, StateLockError, ContextError) as exc:
        return {"schema": "go-workflow.agent-adapter-result.v1", "phase": hook, "status": "blocked",
                "summary": str(exc), "hook": hook, "command": command, "returncode": 78,
                "stdout": "", "stderr": str(exc), "timed_out": False}


def _run_hook_command(repo: Path, command: str, task: dict[str, Any], attempt: int, strategy: str, hook: str, timeout_seconds: int = 900, require_protocol: bool = False, feedback: dict[str, Any] | None = None) -> dict[str, Any]:
    rendered = format_hook_command(command, repo, task, attempt, strategy)
    try:
        model_selection = controlled_preflight(repo, task, hook, rendered, require_protocol)
    except (ValueError, OSError, StateLockError, subprocess.SubprocessError) as exc:
        return {"schema": "go-workflow.agent-adapter-result.v1", "phase": hook,
                "status": "blocked", "summary": str(exc), "hook": hook,
                "command": rendered, "returncode": 78, "stdout": "", "stderr": str(exc),
                "timed_out": False}
    context = build_execution_context(repo, task, phase=hook)
    context_ref = (create_snapshot(repo, task, hook, attempt, strategy, context, feedback=feedback)
                   if registered_workspace(repo) is not None and require_protocol else None)
    request = build_adapter_request(repo, task, context, hook, attempt, strategy, context_ref=context_ref)
    if model_selection is not None:
        request["model_selection"] = model_selection
        append_jsonl(go_root(repo) / "runs" / "events.jsonl", event(task["id"], "run.checked", "model-runtime", {
            "action": "model.phase_started", "phase": hook, "attempt": attempt, "model_selection": model_selection,
        }))
    env = os.environ.copy()
    for name in ('GO_CONTEXT_PATH', 'GO_CONTEXT_SHA256', 'GO_CONTEXT_VERIFY_COMMAND'):
        env.pop(name, None)
    env.update({
        "GO_REPO": str(repo),
        "GO_TASK_ID": str(task.get("id", "unknown")),
        "GO_TASK_JSON": json.dumps(task, ensure_ascii=False),
        "GO_CONTEXT_JSON": json.dumps(context, ensure_ascii=False),
        "GO_ADAPTER_REQUEST_JSON": json.dumps(request, ensure_ascii=False),
        "GO_ATTEMPT": str(attempt),
        "GO_STRATEGY": strategy,
        "GO_HOOK": hook,
    })
    child_command = rendered
    if context_ref is not None:
        verify_command = shlex.join([sys.executable, str(Path(__file__).resolve()), 'context', 'verify', str(repo),
                                    '--task-id', task['id'], '--snapshot', context_ref['path'], '--sha256', context_ref['sha256']])
        env.update(GO_CONTEXT_JSON=json.dumps({'snapshot': context_ref}),
                   GO_TASK_JSON=json.dumps({'id': task['id'], 'snapshot': context_ref}),
                   GO_CONTEXT_PATH=context_ref['path'], GO_CONTEXT_SHA256=context_ref['sha256'],
                   GO_CONTEXT_VERIFY_COMMAND=verify_command, PYTHONDONTWRITEBYTECODE='1')
        # The child verifies the immutable file and current Git/control state
        # before executing any native worker command, under the parent's lease.
        child_command = verify_command + ' >/dev/null && ' + rendered
        verify_snapshot(repo, task['id'], context_ref['path'], context_ref['sha256'])
    started = time.monotonic()
    completed = run_shell_with_timeout(repo, child_command, env, timeout_seconds)
    elapsed = time.monotonic() - started
    if context_ref is not None:
        result_path = Path(context_ref['path']).parent / 'process-result.json'
        atomic_json(result_path, completed)
    turns = None
    if model_selection is not None:
        completed, turns = codex_stream_payload(completed)
    result = normalize_adapter_result(hook, rendered, completed, require_protocol=require_protocol)
    if hook == 'critic' and task.get('outcome_tracking_version') == 1 and (
            behavior_review_required(task) or result.get('behavior_review') is not None):
        review_errors = validate_behavior_review(repo, task, context['behavior_review'], result.get('behavior_review'))
        if review_errors:
            result.update(status='failure', returncode=result.get('returncode') or 65,
                          summary='invalid behavioral critic result: ' + '; '.join(review_errors))
    if context_ref is not None:
        result['context_ref'] = context_ref
        result['process_result_ref'] = {'path': str(result_path), 'sha256': hashlib.sha256(result_path.read_bytes()).hexdigest()}
    if model_selection is not None:
        if result["returncode"] and result["status"] == "success":
            result["status"] = "failure"
        result["model_selection"] = model_selection
        result["usage"] = {"source": "codex_json_stream" if turns else "unavailable",
                           "turns": turns, "elapsed_seconds": elapsed,
                           "provider_invoice_cost_usd": None}
        append_jsonl(go_root(repo) / "runs" / "events.jsonl", event(task["id"], "run.checked", "model-runtime", {
            "action": "model.phase_completed", "phase": hook, "attempt": attempt,
            "model_selection": model_selection, "usage": result["usage"], "returncode": result["returncode"],
        }))
    return result


def arg_str(args: argparse.Namespace, name: str, default: str = "") -> str:
    value = getattr(args, name, default)
    return value or default


def git_status_paths(repo: Path) -> list[str]:
    completed = subprocess.run(["git", "status", "--porcelain"], cwd=repo, text=True, capture_output=True)
    if completed.returncode != 0:
        return []
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        raw = line[3:]
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        paths.append(raw.strip().strip('"'))
    return paths


def worktree_path_fingerprint(repo: Path, path: str) -> str:
    target = repo / path
    digest = hashlib.sha256()
    if target.is_symlink():
        return "symlink:" + os.readlink(target)
    if not target.exists():
        return "missing"
    if target.is_file():
        try:
            digest.update(target.read_bytes())
            return "file:" + digest.hexdigest()
        except OSError as exc:
            return f"unreadable:{type(exc).__name__}:{exc}"
    for child in sorted(item for item in target.rglob("*") if not item.is_dir()):
        digest.update(str(child.relative_to(target)).encode("utf-8", errors="surrogateescape"))
        if child.is_symlink():
            digest.update(os.readlink(child).encode("utf-8", errors="surrogateescape"))
            continue
        try:
            digest.update(child.read_bytes())
        except OSError as exc:
            digest.update(f"unreadable:{type(exc).__name__}:{exc}".encode())
    return "dir:" + digest.hexdigest()


def git_dirty_snapshot(repo: Path) -> dict[str, str]:
    return {path: worktree_path_fingerprint(repo, path) for path in git_status_paths(repo)}


def allowed_runtime_path(path: str) -> bool:
    return path.startswith((
        ".go/tasks/",
        ".go/runs/",
        ".go/evidence/",
        ".go/reflections/",
        ".go/locks/",
    ))


def task_allowed_paths(task: dict[str, Any]) -> set[str]:
    scope = task.get("scope", {}) or {}
    return {path.strip("/") for path in (scope.get("modify", []) or []) if path}


def path_allowed_by_task(path: str, task: dict[str, Any], allow_runtime: bool = True) -> bool:
    clean = path.strip("/")
    if allow_runtime and allowed_runtime_path(clean):
        return True
    for allowed in task_allowed_paths(task):
        if fnmatch.fnmatch(clean, allowed) or clean == allowed or clean.startswith(allowed.rstrip("/") + "/"):
            return True
    return False


def ignorable_generated_path(path: str) -> bool:
    clean = path.strip("/")
    return clean == "__pycache__" or clean.startswith("__pycache__/") or "/__pycache__/" in clean or clean.startswith(".pytest_cache/")


def scope_violations(repo: Path, task: dict[str, Any]) -> list[str]:
    return [path for path in git_status_paths(repo) if not ignorable_generated_path(path) and not path_allowed_by_task(path, task)]


def scope_violations_after(repo: Path, task: dict[str, Any], before: dict[str, str]) -> list[str]:
    after = git_dirty_snapshot(repo)
    return [
        path for path, fingerprint in after.items()
        if before.get(path) != fingerprint
        and not ignorable_generated_path(path)
        and not path_allowed_by_task(path, task)
    ]


def ensure_budget(result: dict[str, Any], max_commands: int, started_at: float, max_minutes: int, stage: str) -> bool:
    elapsed_minutes = (time.monotonic() - started_at) / 60
    if result["commands_run"] >= max_commands or elapsed_minutes >= max_minutes:
        result.update({
            "status": "budget_exhausted",
            "budget_exhausted": True,
            "summary": f"Budget exhausted before {stage}.",
            "next_action": "resume with go-loop using a larger budget or narrower task scope",
        })
        return False
    return True


def shell_quote(value: str) -> str:
    return shlex.quote(str(value))


def shell_join(*parts: object) -> str:
    return " ".join(shell_quote(str(part)) for part in parts)


def attempt_markdown(task: dict[str, Any], attempt: dict[str, Any], context: dict[str, Any]) -> str:
    vision = context.get("vision") or {}
    principles = (context.get("architecture_principles") or {}).get("principles") or []
    hierarchy = context.get("hierarchy") or {}
    epic_ids = [str(epic.get("id")) for epic in hierarchy.get("epics", []) if epic.get("id")]
    principle_lines = [
        f"- `{principle.get('id', 'unnamed')}`: {principle.get('statement', '')}"
        for principle in principles
    ] or ["- none declared"]
    return "\n".join([
        f"# Attempt {attempt.get('attempt')} — {task.get('id')}",
        "",
        f"Strategy: `{attempt.get('strategy')}`",
        "",
        "## Project contract",
        "",
        f"North star: {vision.get('north_star', '')}",
        "",
        "Success metrics:",
        *[f"- {metric}" for metric in vision.get("success_metrics", [])],
        "",
        "Architecture principles:",
        *principle_lines,
        "",
        f"Hierarchy epics: {', '.join(epic_ids) if epic_ids else 'none declared'}",
        "",
        "## Task",
        "",
        f"Summary: {task.get('summary', '')}",
        "",
        str(task.get('description', '')),
        "",
        "## Acceptance",
        "",
        *[f"- {item}" for item in task.get("acceptance", [])],
        "",
        "## Verification",
        "",
        *[f"- `{item}`" for item in task.get("verification", [])],
        "",
    ])


def checks_log(checks: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for check in checks:
        chunks.extend([
            f"$ {check.get('command')}",
            f"returncode: {check.get('returncode')}",
            "stdout:",
            str(check.get("stdout", "")),
            "stderr:",
            str(check.get("stderr", "")),
            "",
        ])
    return "\n".join(chunks)


def critic_markdown(attempt: dict[str, Any]) -> str:
    critic = attempt.get("critic", {})
    findings = critic.get("blocking_findings") or []
    lines = [f"# Critic — {attempt.get('task_id')} attempt {attempt.get('attempt')}", "", f"Status: `{critic.get('status')}`", "", "## Blocking findings"]
    lines.extend(f"- {finding}" for finding in findings)
    if not findings:
        lines.append("- none")
    if critic.get("repair_hint"):
        lines.extend(["", "## Repair hint", str(critic.get("repair_hint"))])
    if critic.get("result"):
        lines.extend(["", "## Adapter result", "```json", json.dumps(critic.get("result"), indent=2), "```"])
    return "\n".join(lines) + "\n"


def git_diff_text(repo: Path) -> str:
    completed = subprocess.run(["git", "diff", "--no-ext-diff", "--"], cwd=repo, text=True, capture_output=True)
    return completed.stdout if completed.returncode == 0 else completed.stderr


def record_attempt(repo: Path, root: Path, task: dict[str, Any], agent: str, attempt: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    attempt_no = int(attempt.get("attempt") or 0)
    suffix = '-' + uuid.uuid4().hex if registered_workspace(repo) is not None else ''
    attempt_dir = root / "runs" / str(task.get("id", "unknown")) / f"attempt-{attempt_no:02d}{suffix}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    context = build_execution_context(repo, task)
    (attempt_dir / "prompt.md").write_text(attempt_markdown(task, attempt, context), encoding="utf-8")
    (attempt_dir / "verify.log").write_text(checks_log(checks), encoding="utf-8")
    (attempt_dir / "critic.md").write_text(critic_markdown(attempt), encoding="utf-8")
    (attempt_dir / "diff.patch").write_text(git_diff_text(repo), encoding="utf-8")
    verdict = {
        "schema": "go-workflow.attempt-verdict.v1",
        "task_id": task.get("id"),
        "attempt": attempt_no,
        "strategy": attempt.get("strategy"),
        "build_status": (attempt.get("build") or {}).get("status"),
        "verify_status": (attempt.get("verify") or {}).get("status"),
        "critic_status": (attempt.get("critic") or {}).get("status"),
        "repair_status": (attempt.get("repair") or {}).get("status"),
        "judge_status": (attempt.get("judge") or {}).get("status"),
        "created_at": now_iso(),
    }
    dump_json(attempt_dir / "verdict.json", verdict)
    artifact_root = attempt_dir if suffix else attempt_dir.relative_to(root.parent)
    attempt["artifacts"] = {
        "prompt": str(artifact_root / "prompt.md"),
        "verify_log": str(artifact_root / "verify.log"),
        "critic": str(artifact_root / "critic.md"),
        "diff": str(artifact_root / "diff.patch"),
        "verdict": str(artifact_root / "verdict.json"),
    }
    append_jsonl(root / "runs" / "events.jsonl", event(str(task.get("id", "unknown")), "auto.attempt", agent, attempt))


def create_followup_task(repo: Path, task: dict[str, Any], findings: list[str], agent: str) -> dict[str, Any]:
    root = go_root(repo)
    followup_id = slugify(f"followup-{task.get('id', 'task')}-{len(findings)}").lower()
    base = followup_id
    index = 2
    while any(task_path(root, state, followup_id).exists() for state in ("open", "active", "blocked", "done")):
        followup_id = f"{base}-{index}"
        index += 1
    project = load_json(root / "project.json")
    followup = {
        "schema": TASK_SCHEMA,
        "kind": "task",
        "execution_mode": task.get("execution_mode", "agent"),
        "shareable_delivery": "none",
        "id": followup_id,
        "project": project["id"],
        "status": "open",
        "summary": f"Resolve critic findings for {task.get('id')}",
        "description": "\n".join(findings),
        "scope": task.get("scope", {"read": [], "modify": []}),
        "acceptance": ["All listed critic findings are resolved or explicitly reclassified as non-blocking."],
        "verification": task.get("verification", []),
        "claim": {},
        "evidence": [],
        "created_from": {"task_id": task.get("id"), "agent": agent, "created_at": now_iso(), "findings": findings},
    }
    apply_intake_contract(repo, followup, task)
    findings_before_write = validate_task(followup, followup_id, expected_status="open") + dependency_findings(repo, followup)
    if findings_before_write:
        raise RepoLocalError("invalid followup: " + "; ".join(findings_before_write))
    hierarchy = load_json(root / "hierarchy.json")
    owners = [epic for epic in hierarchy_epics(hierarchy) if task['id'] in epic_task_ids(epic)]
    if len(owners) != 1:
        raise RepoLocalError("followup requires one source-task owning epic")
    dump_json(task_path(root, "open", followup_id), followup)
    append_task_to_epic(root, owners[0]['id'], followup_id)
    append_jsonl(root / "runs" / "events.jsonl", event(followup_id, "run.checked", agent, {"action": "critic.followup_created", "source_task": task.get("id"), "findings": findings}))
    return followup


def builtin_semantic_findings(repo: Path, task: dict[str, Any], checks: list[dict[str, Any]]) -> list[str]:
    findings: list[str] = []
    acceptance = task.get("acceptance") or []
    if not acceptance:
        findings.append("task has no acceptance criteria; first-green cannot prove done")
    if acceptance == ["Task result is implemented and verified."]:
        findings.append("task uses generic default acceptance criteria; first-green cannot prove done")
    if not task.get("verification"):
        findings.append("task has no verification commands; loop cannot prove behavior")
    failed = [check for check in checks if check.get("returncode") != 0]
    if failed:
        findings.append(f"verification still failing: {failed[0].get('command')}")
    findings.extend(architecture_finish_findings(go_root(repo), task))
    return list(dict.fromkeys(findings))


def repair_agent_available(agent: str) -> dict[str, Any]:
    binary = "codex" if agent == "codex" else "hermes" if agent == "hermes" else agent
    path = shutil.which(binary)
    prompt_flag = detect_hermes_prompt_flag(path) if agent == "hermes" and path else None
    compatible = bool(path) and (agent != "hermes" or prompt_flag is not None)
    return {
        "agent": agent,
        "binary": binary,
        "available": bool(path),
        "compatible": compatible,
        "path": path,
        "prompt_flag": prompt_flag,
        "model_selection_capabilities": adapter_capabilities(agent),
    }


def worker_outcome_instructions(task: dict[str, Any]) -> str:
    workspace = ((task.get("execution_contract") or {}).get("workspace") or {})
    if workspace.get("mode") == "task_worktree":
        reporting = "Report implemented R# evidence in your phase result; the controller owns canonical outcome updates. Do not run task outcome commands or write canonical .go state from this worker. Reading and verifying the canonical context is permitted."
    else:
        reporting = "Record implemented R# evidence with `go-workflow task outcome`."
    return reporting + " Leave deferred shipping requirements pending for the controller publisher. Report blockers explicitly. Do not publish or change a configured release version/changelog; the controller prepares these before final verification."


def default_repair_agent_command(agent: str, task: dict[str, Any]) -> str:
    availability = repair_agent_available(agent)
    if not availability["available"]:
        raise RepoLocalError(f"repair agent '{agent}' is not available on PATH")
    if not availability["compatible"]:
        raise RepoLocalError(f"repair agent '{agent}' has no supported prompt interface (-z or -p)")
    instructions = " ".join([
        "You are the repair adapter for go-workflow-stack.",
        "In the repository named by GO_REPO, fix the task named by GO_TASK_ID using the GO_ATTEMPT and GO_STRATEGY context.",
        "Read GO_TASK_JSON from the environment; if it contains a snapshot reference, use GO_CONTEXT_PATH.",
        "If GO_CONTEXT_PATH exists, run GO_CONTEXT_VERIFY_COMMAND before writing and read the complete context and raw feedback from that file. Otherwise read GO_CONTEXT_JSON. Obey its vision, architecture principles, hierarchy, acceptance, verification, and task scope.",
        "Edit only paths allowed by the task scope.",
        "Apply task_design_review.author from the verified context before changing product behavior; report its grounded disposition in your phase result.",
        "Run the task verification commands before exiting.",
        worker_outcome_instructions(task),
        "Exit non-zero if you cannot safely repair within scope.",
    ])
    return native_agent_command(
        agent,
        "repair",
        instructions,
        hermes_prompt_flag=availability["prompt_flag"] or "-z",
        model_profile=phase_model(task, "repair"),
    )


def select_executor_agent(requested: str) -> str:
    if requested == "none":
        return ""
    if requested in {"codex", "hermes"}:
        availability = repair_agent_available(requested)
        if not availability["available"]:
            raise RepoLocalError(f"executor agent '{requested}' is not available on PATH")
        if not availability["compatible"]:
            raise RepoLocalError(f"executor agent '{requested}' has no supported prompt interface (-z or -p)")
        return requested
    for candidate in ("codex", "hermes"):
        if repair_agent_available(candidate)["compatible"]:
            return candidate
    raise RepoLocalError("no Codex or Hermes executor agent is available on PATH")


def executor_agent_default() -> str:
    requested = os.environ.get("GO_EXECUTOR_AGENT", "auto").strip().lower()
    return requested if requested in {"auto", "codex", "hermes", "none"} else "auto"


def default_executor_agent_command(agent: str, task: dict[str, Any]) -> str:
    selected = select_executor_agent(agent)
    availability = repair_agent_available(selected)
    instructions = " ".join([
        "You are the build executor for a repo-local .go task.",
        "Work in the repository named by GO_REPO on the task named by GO_TASK_ID using the GO_ATTEMPT and GO_STRATEGY context.",
        "If GO_CONTEXT_PATH exists, run GO_CONTEXT_VERIFY_COMMAND before writing and read the complete context and raw feedback from that file. Otherwise read GO_CONTEXT_JSON. Obey its vision, architecture principles, hierarchy, acceptance, verification, and modify scope.",
        "Implement the task, run focused verification, and leave only scoped changes.",
        "Apply task_design_review.author from the verified context before product edits; report its grounded disposition in your phase result.",
        worker_outcome_instructions(task),
        "Do not merely describe commands; perform the work and exit non-zero when the task cannot be completed safely.",
    ])
    return native_agent_command(
        selected,
        "build",
        instructions,
        hermes_prompt_flag=availability["prompt_flag"] or "-z",
        model_profile=phase_model(task, "build"),
    )


def run_default_critic_agent(
    repo: Path,
    agent: str,
    task: dict[str, Any],
    attempt: int,
    strategy: str,
    timeout_seconds: int,
    feedback: dict[str, Any] | None = None,
    *, publication_pending: bool = False,
) -> dict[str, Any]:
    output = go_root(repo) / "runs" / str(task.get("id", "unknown")) / f"attempt-{attempt:02d}" / "deep-critic.txt"
    if registered_workspace(repo) is not None:
        output = output.with_name('deep-critic-' + uuid.uuid4().hex + '.txt')
    output.parent.mkdir(parents=True, exist_ok=True)
    instructions = " ".join([
        "You are the blocking critic for a repo-local .go task.",
        "Review the current repository result for task {task_id} against GO_CONTEXT_JSON, including vision, architecture principles, acceptance, verification, scope, and diff. If GO_CONTEXT_PATH exists, verify with GO_CONTEXT_VERIFY_COMMAND and read that snapshot and its raw evidence.",
        "Do not edit files.",
        "Apply task_design_review.critic from the verified context and cite actual inspected evidence for your verdict.",
        "When behavior_review is present in the verified context, emit a matching go-workflow.behavior-review.v1 object in the adapter result. Cover each original R# exactly once; bind inspected evidence to the task, requirement, candidate and context digests. A passed outcome needs current behavior_proof bytes. A blocked outcome needs an actionable repair limited to the original scope and declared checks. Use pending_downstream only for controller-owned publication still pending.",
        "Return status success only when there are no blocking findings; otherwise return status blocked and summarize the findings.",
    ])
    if (strategy in {'recovery_diagnosis','recovery_reassessment'}
            and feedback and feedback.get('recovery',{}).get('kind')==strategy):
        instructions = ' '.join([
            'You are an independent read-only recovery critic. Do not edit files or publish.',
            'Verify GO_CONTEXT_VERIFY_COMMAND and inspect the current source and raw failure evidence in GO_CONTEXT_PATH.',
            'Diagnose why the previous approach failed; choose a substantively different safe method, such as a smaller reproduction or targeted instrumentation.',
            'This is diagnosis, not final product approval. Return a versioned adapter result containing recovery_plan:',
            'schema go-workflow.recovery-plan.v1, failure_fingerprint copied exactly from feedback.recovery.failure_fingerprint,',
            'method (new nonempty approach, different from prior_methods), diagnosis (concrete reasoning), safe_to_continue (boolean),',
            'findings (nonempty array of path, sha256 of actual current file bytes, finding describing the relevant inspected evidence).',
            'Paths must be repository-relative existing files. Do not invent evidence or relabel an unchanged approach.',
            'After two strategies explicitly reassess the original route. If no evidence-backed safe route remains, use safe_to_continue false.',
            'A safe recovery proposal uses adapter status success; it does not approve the task or waive verification and final critic.',
        ])
    if publication_pending:
        instructions += " " + " ".join([
            "This critic runs before controller-owned publication.",
            "Judge whether the current scoped candidate and executed checks are ready for configured commit/push. For release:required, also review the prepared version/changelog and release configuration; release:none does not require a tag or version change.",
            "Task-level push, tag, release-readback and live-deployment requirements remain pending downstream controller work. Do not block solely because their post-publication receipts do not exist yet.",
            "Still block missing or failed current verification, unsafe release readiness, scope violations and implementation defects. Never invent release evidence or waive a required downstream check.",
            "Do not publish or mark shipping outcomes verified. Your success approves only this critic phase; the controller must prove publication and any required deployment before task completion.",
        ])
    availability = repair_agent_available(agent)
    if not availability["available"]:
        raise RepoLocalError(f"critic agent '{agent}' is not available on PATH")
    if not availability["compatible"]:
        raise RepoLocalError(f"critic agent '{agent}' has no supported prompt interface (-z or -p)")
    command = native_agent_command(
        agent,
        "critic",
        instructions,
        hermes_prompt_flag=availability["prompt_flag"] or "-z",
        model_profile=phase_model(task, "critic"),
    )
    result = run_hook_command(repo, command, task, attempt, strategy, "critic", timeout_seconds, require_protocol=True, feedback=feedback)
    output.write_text(result.get("stdout") or "", encoding="utf-8")
    verdict_text = output.read_text(encoding="utf-8") if output.is_file() else ""
    result["verdict_text"] = verdict_text
    return result


def git_push_target(repo: Path) -> tuple[str, str, str] | None:
    branch_result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo, text=True, capture_output=True, check=False,
    )
    branch = branch_result.stdout.strip()
    if branch_result.returncode != 0 or not branch:
        return None
    remote = ""
    for key in (f"branch.{branch}.pushRemote", "remote.pushDefault", f"branch.{branch}.remote"):
        configured = subprocess.run(
            ["git", "config", "--get", key],
            cwd=repo, text=True, capture_output=True, check=False,
        )
        if configured.returncode == 0 and configured.stdout.strip():
            remote = configured.stdout.strip()
            break
    if not remote or remote == ".":
        return None
    return remote, branch, f"{remote}/{branch}"


def verify_remote_commit(
    repo: Path,
    commit_sha: str,
    target: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    target = target or git_push_target(repo)
    if target is None:
        return {
            "push_target": None,
            "remote_sha": None,
            "readback_verified": False,
            "readback_error": "cannot resolve configured push target",
        }
    remote, branch, label = target
    readback = subprocess.run(
        ["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
        cwd=repo, text=True, capture_output=True, check=False,
    )
    remote_sha = None
    if readback.returncode == 0 and readback.stdout.strip():
        candidate = readback.stdout.split()[0]
        if re.fullmatch(r"[0-9a-f]{40}", candidate):
            remote_sha = candidate
    verified = readback.returncode == 0 and remote_sha == commit_sha
    return {
        "push_target": label,
        "remote_sha": remote_sha,
        "readback_verified": verified,
        "readback_error": None if verified else (readback.stderr.strip() or "remote readback does not match committed SHA"),
    }


def push_exact_commit(repo: Path, commit_sha: str, target: tuple[str, str, str]) -> subprocess.CompletedProcess[str]:
    remote, branch, _label = target
    return subprocess.run(
        ["git", "push", remote, f"{commit_sha}:refs/heads/{branch}"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def attach_ship_provenance(root: Path, done_path: Path, task_id: str, agent: str, ship: dict[str, Any]) -> None:
    task = load_json(done_path)
    evidence = task.get("evidence") or []
    if not evidence:
        raise RepoLocalError("cannot attach ship provenance without finish evidence")
    provenance = {
        "policy": ship.get("policy"),
        "commit_sha": ship.get("commit_sha"),
        "push_target": ship.get("push_target"),
        "remote_sha": ship.get("remote_sha"),
        "readback_verified": bool(ship.get("readback_verified")),
    }
    evidence[-1]["ship"] = provenance
    dump_json(done_path, task)
    append_jsonl(root / "evidence" / "events.jsonl", event(
        task_id,
        "evidence.appended",
        agent,
        {"kind": "ship_provenance", "ship": provenance},
    ))


def ship_changes(repo: Path, policy: str, allow_push: bool, message: str, task: dict[str, Any] | None = None) -> dict[str, Any]:
    if policy == "none":
        return {"policy": policy, "status": "skipped"}
    if policy == "push" and not allow_push:
        return {"policy": policy, "status": "blocked", "reason": "push requires --allow-push"}
    status = subprocess.run(["git", "status", "--short"], cwd=repo, text=True, capture_output=True)
    if status.returncode != 0:
        return {"policy": policy, "status": "failed", "stderr": status.stderr}
    changed = git_status_paths(repo)
    if not changed:
        result: dict[str, Any] = {"policy": policy, "status": "clean", "commit_sha": git_head_commit(repo)}
        if policy == "push" and result["commit_sha"]:
            target = git_push_target(repo)
            if target is None:
                result.update({"status": "push_failed", "push_target": None, "reason": "cannot resolve configured push target"})
                return result
            result["push_target"] = target[2]
            push = push_exact_commit(repo, result["commit_sha"], target)
            result["push"] = {"returncode": push.returncode, "stdout": push.stdout[-2000:], "stderr": push.stderr[-2000:]}
            if push.returncode != 0:
                result["status"] = "push_failed"
                return result
            result.update(verify_remote_commit(repo, result["commit_sha"], target))
            result["status"] = "pushed" if result["readback_verified"] else "readback_failed"
        return result
    allowed_paths = [path for path in changed if allowed_runtime_path(path) or (task is not None and path_allowed_by_task(path, task, allow_runtime=False))]
    unrelated = [path for path in changed if path not in allowed_paths]
    if not allowed_paths:
        return {"policy": policy, "status": "blocked", "reason": "no scoped paths to ship", "unrelated_dirty": unrelated}
    add = subprocess.run(["git", "add", "--", *allowed_paths], cwd=repo, text=True, capture_output=True)
    if add.returncode != 0:
        return {"policy": policy, "status": "failed", "stderr": add.stderr, "scoped_paths": allowed_paths, "unrelated_dirty": unrelated}
    commit = subprocess.run(["git", "-c", "user.name=Go Workflow", "-c", "user.email=go-workflow@example.com", "commit", "-m", message], cwd=repo, text=True, capture_output=True)
    result = {"policy": policy, "status": "committed" if commit.returncode == 0 else "failed", "stdout": commit.stdout[-2000:], "stderr": commit.stderr[-2000:], "scoped_paths": allowed_paths, "unrelated_dirty": unrelated}
    if commit.returncode != 0:
        return result
    result["commit_sha"] = git_head_commit(repo)
    if policy == "push":
        target = git_push_target(repo)
        if target is None:
            result.update({"status": "push_failed", "push_target": None, "reason": "cannot resolve configured push target"})
            return result
        result["push_target"] = target[2]
        push = push_exact_commit(repo, result["commit_sha"], target)
        result["push"] = {"returncode": push.returncode, "stdout": push.stdout[-2000:], "stderr": push.stderr[-2000:]}
        if push.returncode != 0:
            result["status"] = "push_failed"
            return result
        if not result["commit_sha"]:
            result["status"] = "readback_failed"
            result["readback_verified"] = False
            result["readback_error"] = "cannot resolve committed SHA after push"
            return result
        result.update(verify_remote_commit(repo, result["commit_sha"], target))
        result["status"] = "pushed" if result["readback_verified"] else "readback_failed"
    return result


def ship_policy_blocker(policy: str, allow_push: bool) -> str | None:
    if policy == "push" and not allow_push:
        return "push requires --allow-push"
    return None


def build_resume_args(mode: str, args: argparse.Namespace) -> list[str]:
    parts = [mode, ".", "--execute"]
    valued = [
        ("--max-tasks", arg_int(args, "max_tasks", 1)),
        ("--summary-chars", arg_int(args, "summary_chars", 1200)),
        ("--max-minutes", arg_int(args, "max_minutes", 45)),
        ("--max-commands", arg_int(args, "max_commands", 12)),
        ("--command-timeout-seconds", arg_int(args, "command_timeout_seconds", 900)),
        ("--max-attempts", arg_int(args, "max_attempts", 5)),
        ("--checkpoint-every-tasks", arg_int(args, "checkpoint_every_tasks", 1)),
        ("--agent", getattr(args, "agent", "agent")),
    ]
    for flag, value in valued:
        parts.extend([flag, str(value)])
    for flag, name in [
        ("--build-command", "build_command"),
        ("--critic-command", "critic_command"),
        ("--repair-command", "repair_command"),
        ("--repair-agent", "repair_agent"),
        ("--executor-agent", "executor_agent"),
        ("--ship-policy", "ship_policy"),
        ("--campaign", "campaign"),
        ("--previous-campaign", "previous_campaign"),
        ("--campaign-workspace-root", "campaign_workspace_root"),
    ]:
        value = arg_str(args, name)
        if value:
            parts.extend([flag, value])
    parts.append("--semantic-critic" if bool(getattr(args, "semantic_critic", True)) else "--no-semantic-critic")
    for flag, name in [
        ("--followup-on-block", "followup_on_block"),
        ("--allow-dirty", "allow_dirty"),
        ("--allow-push", "allow_push"),
        ("--allow-deploy", "allow_deploy"),
        ("--json", "json"),
    ]:
        if bool(getattr(args, name, False)):
            parts.append(flag)
    return parts


def write_resume_script(root: Path, mode: str, args: argparse.Namespace) -> Path:
    resume_args = " ".join(shell_quote(part) for part in build_resume_args(mode, args))
    script = "\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"',
        'STACK="${GO_STACK:-}"',
        'if [ -z "$STACK" ] || [ ! -f "$STACK/cli/go.py" ]; then',
        '  for candidate in "$REPO_ROOT/../go-workflow-stack" "$HOME/github/go-workflow-stack" "$HOME/Dev/go-workflow-stack"; do',
        '    if [ -f "$candidate/cli/go.py" ]; then STACK="$candidate"; break; fi',
        "  done",
        "fi",
        'if [ -z "$STACK" ] || [ ! -f "$STACK/cli/go.py" ]; then',
        '  echo "go-workflow-stack not found; set GO_STACK or clone it beside this repository" >&2',
        "  exit 2",
        "fi",
        'cd "$REPO_ROOT"',
        f'exec python3 "$STACK/cli/go.py" {resume_args}',
        "",
    ])
    path = root / "runs" / "resume.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def build_resume_command(mode: str, args: argparse.Namespace) -> str:
    return "bash .go/runs/resume.sh"


def write_latest_run_state(repo: Path, root: Path, result: dict[str, Any], args: argparse.Namespace, mode: str) -> None:
    write_resume_script(root, mode, args)
    latest = {
        "schema": "go-workflow.latest-run.v1",
        "updated_at": now_iso(),
        "mode": mode,
        "status": result.get("status"),
        "completed_tasks": result.get("completed_tasks", []),
        "blocked_task": result.get("blocked_task"),
        "budget_exhausted": result.get("budget_exhausted", False),
        "resume_command": build_resume_command(mode, args),
        "resume_args": build_resume_args(mode, args),
        "runtime_resolution": ["GO_STACK", "sibling checkout", "~/github/go-workflow-stack", "~/Dev/go-workflow-stack"],
        "effective_flags": {
            "max_tasks": arg_int(args, "max_tasks", 1),
            "summary_chars": arg_int(args, "summary_chars", 1200),
            "max_minutes": arg_int(args, "max_minutes", 45),
            "max_commands": arg_int(args, "max_commands", 12),
            "command_timeout_seconds": arg_int(args, "command_timeout_seconds", 900),
            "max_attempts": arg_int(args, "max_attempts", 5),
            "build_command": arg_str(args, "build_command"),
            "critic_command": arg_str(args, "critic_command"),
            "repair_command": arg_str(args, "repair_command"),
            "repair_agent": arg_str(args, "repair_agent"),
            "executor_agent": arg_str(args, "executor_agent", "auto"),
            "semantic_critic": bool(getattr(args, "semantic_critic", False)),
            "followup_on_block": bool(getattr(args, "followup_on_block", False)),
            "ship_policy": arg_str(args, "ship_policy", "none"),
            "allow_dirty": bool(getattr(args, "allow_dirty", False)),
            "allow_push": bool(getattr(args, "allow_push", False)),
            "allow_deploy": bool(getattr(args, "allow_deploy", False)),
        },
        "next_action": result.get("next_action"),
    }
    dump_json(root / "runs" / "latest.json", latest)


def execute_loop_plan(repo: Path, args: argparse.Namespace, mode: str) -> tuple[int, dict[str, Any]]:
    if getattr(args, "campaign", ""):
        return execute_campaign(repo, args, mode, sys.modules[__name__], execute_managed)
    managed = select_managed_task(repo, args, sys.modules[__name__])
    if managed is not None:
        return execute_managed(repo, args, mode, managed, sys.modules[__name__])
    plan = build_loop_plan(repo, args, mode=mode)
    root = go_root(repo)
    preflight = plan["run_envelope"]["preflight"]
    result: dict[str, Any] = {
        "schema": "go-workflow.auto-run-result.v1",
        "mode": mode,
        "repo": str(repo),
        "run_envelope": plan["run_envelope"],
        "status": "running",
        "completed_tasks": [],
        "blocked_task": None,
        "evidence": [],
        "checks": [],
        "summary": "",
        "next_action": None,
        "checkpoints": [],
        "commands_run": 0,
        "budget_exhausted": False,
        "attempts": [],
        "ship": [],
        "completion_audit": None,
    }
    started_at = time.monotonic()
    budget = plan["run_envelope"]["budget"]
    max_commands = int(budget.get("max_commands") or 1)
    max_minutes = int(budget.get("max_minutes") or 1)
    command_timeout_seconds = int(budget.get("command_timeout_seconds") or 900)
    checkpoint_every_tasks = int(budget.get("checkpoint_every_tasks") or 1)
    max_attempts = max(int(getattr(args, "max_attempts", 5) or 5), 1)
    strategies = ["direct_fix", "re_approach", "simplify", "last_stand", "block_with_evidence"]
    build_command = arg_str(args, "build_command")
    critic_command = arg_str(args, "critic_command")
    repair_command = arg_str(args, "repair_command")
    executor_agent = arg_str(args, "executor_agent", "auto")
    semantic_critic = bool(getattr(args, "semantic_critic", False))
    followup_on_block = bool(getattr(args, "followup_on_block", False))
    ship_policy = arg_str(args, "ship_policy", "none")
    allow_push = bool(getattr(args, "allow_push", False))

    if preflight.get("contract_gate_required"):
        result.update({
            "status": "contract_gate",
            "summary": "Task contract is not executable enough for autonomous completion.",
            "next_action": "resolve preflight.contract_findings (task scope/proof or governing decisions), then rerun",
        })
        return 1, result

    initial_unfinished = preflight.get("unfinished_tasks", {})
    initial_pending_reviews = pending_review_task_ids(repo)
    if not plan.get("next_tasks") and initial_pending_reviews and not (
        initial_unfinished.get("active") or initial_unfinished.get("blocked")
    ):
        result.update({
            "status": "review_pending",
            "blocked_task": initial_pending_reviews[0],
            "summary": "Work is complete, but explicit review approval is still pending.",
            "next_action": "review completed work and record `go-workflow task review --status approved` or `needs_fix`",
        })
        write_latest_run_state(repo, root, result, args, mode)
        return 1, result
    if not plan.get("next_tasks") and open_task_records(root):
        blockers = [finding for _, candidate in open_task_records(root)
                    for finding in dependency_findings(repo, candidate, readiness=True)]
        if blockers:
            result.update({"status": "dependency_blocked", "summary": "; ".join(blockers),
                           "next_action": "complete dependency release evidence before retrying"})
            write_latest_run_state(repo, root, result, args, mode)
            return 1, result
    if not plan.get("next_tasks") and not (initial_unfinished.get("active") or initial_unfinished.get("blocked")):
        result.update({
            "status": "task_required",
            "summary": "GO execution requires a repo-local task; no open task is available.",
            "next_action": "run go <repo> --intent '<instruction>' --loop --write --execute, or create a task first",
        })
        write_latest_run_state(repo, root, result, args, mode)
        return 1, result

    if not plan.get("next_tasks") and (initial_unfinished.get("active") or initial_unfinished.get("blocked")):
        blocked_id = (initial_unfinished.get("blocked") or initial_unfinished.get("active") or [None])[0]
        result.update({
            "status": "blocked",
            "blocked_task": blocked_id,
            "summary": "No open task is claimable while active or blocked task state remains.",
            "next_action": "repair, requeue, or explicitly resolve the unfinished task before claiming goal completion",
        })
        return 1, result

    if preflight["human_gate_required"] and not args.allow_dirty:
        result.update({"status": "safety_gate", "summary": "Preflight blocked by dirty/lock/conflict/secret state.", "next_action": "resolve human_gate_blockers or rerun with explicit override"})
        append_jsonl(root / "runs" / "events.jsonl", event("go-auto", "auto.safety_gate", args.agent, {"blockers": preflight["human_gate_blockers"]}))
        return 1, result

    for _ in range(max(arg_int(args, "max_tasks", 3), 1)):
        elapsed_minutes = (time.monotonic() - started_at) / 60
        if result["commands_run"] >= max_commands or elapsed_minutes >= max_minutes:
            result.update({"status": "budget_exhausted", "budget_exhausted": True, "summary": "Budget exhausted before selecting another task.", "next_action": "continue with go-loop using a larger budget"})
            break
        tasks = open_tasks(repo)
        if not tasks:
            result["status"] = "done"
            result["summary"] = "No open tasks remain."
            break
        open_path, task = tasks[0]
        task_build_command = build_command
        default_executor_selected = False
        selected_executor_agent = ""
        if task.get("execution_mode", "mechanical") == "agent" and not task_build_command:
            try:
                selected_executor_agent = select_executor_agent(executor_agent)
                task_build_command = default_executor_agent_command(selected_executor_agent, task)
                default_executor_selected = True
            except RepoLocalError as exc:
                result.update({
                    "status": "adapter_gate",
                    "blocked_task": task.get("id"),
                    "summary": str(exc),
                    "next_action": "install/select an executor agent, provide --build-command, or explicitly mark the task mechanical",
                })
                break
        if "execution_contract" in task and task.get("execution_mode") == "agent":
            requested_repair = arg_str(args, "repair_agent", "")
            if (build_command or critic_command or repair_command or selected_executor_agent != "codex"
                    or requested_repair not in {"", "codex"}):
                result.update({"status": "adapter_gate", "blocked_task": task["id"],
                               "summary": "Controlled model selection is unsupported for Hermes/custom phase adapters.",
                               "next_action": "configure native Codex phases; unsupported adapters require explicit model-control implementation"})
                break
        task_owned_patterns = [
            pattern for pattern in task.get("scope", {}).get("modify", [])
            if not str(pattern).startswith(".go/")
        ]
        dirty = classify_dirty(repo, task_owned_patterns)
        # In an executed batch, finishing earlier tasks intentionally mutates .go evidence/runs/task files.
        # Do not let our own lifecycle writes block the next task; pre-existing dirty state is already gated by preflight.
        if result["completed_tasks"]:
            dirty = {"blocking": [], "report_only": dirty.get("blocking", []) + dirty.get("report_only", [])}
        if dirty["blocking"] and not args.allow_dirty:
            result.update({"status": "safety_gate", "blocked_task": task.get("id"), "summary": "Task scope dirty gate blocked execution.", "next_action": "resolve dirty state or rerun with explicit override"})
            result["run_envelope"]["preflight"]["human_gate_required"] = True
            result["run_envelope"]["preflight"].setdefault("human_gate_blockers", []).extend(dirty["blocking"])
            append_jsonl(root / "runs" / "events.jsonl", event(task.get("id", "unknown"), "auto.safety_gate", args.agent, {"blockers": dirty["blocking"]}))
            return 1, result
        try:
            with repository_lock(root, f"task-{task['id']}"):
                if not open_path.is_file():
                    continue
                task = load_json(open_path)
                if task.get("status") != "open" or task.get("claim", {}).get("agent"):
                    continue
                from go_workflow.migrations import pending_lifecycle_findings
                pending = pending_lifecycle_findings(repo)
                if pending: raise RepoLocalError('; '.join(pending))
                dependency_errors = dependency_findings(repo, task, readiness=True)
                if dependency_errors:
                    result.update({"status": "blocked", "blocked_task": task["id"], "summary": "; ".join(dependency_errors)})
                    break
                architecture_findings = architecture_claim_findings(root, task)
                if architecture_findings:
                    result.update({"status": "contract_gate", "blocked_task": task["id"],
                                   "summary": "; ".join(architecture_findings)})
                    break
                task["status"] = "active"
                task["work_status"] = "in_progress"
                task.setdefault("review_status", "none")
                task.setdefault("review_history", [])
                base_commit = claim_base_commit(repo)
                claimed_at = now_iso()
                task["claim"] = {
                    "agent": args.agent,
                    "claimed_at": claimed_at,
                    "base_commit": base_commit,
                }
                active_path = task_path(root, "active", task["id"])
                atomic_move_json(open_path, active_path, task)
                append_jsonl(root / "runs" / "events.jsonl", event(
                    task["id"],
                    "task.claimed",
                    args.agent,
                    {
                        "report_only_dirty": dirty["report_only"],
                        "executor": mode,
                        "base_commit": base_commit,
                        "claimed_at": claimed_at,
                    },
                ))
        except StateLockError as exc:
            result.update({"status": "safety_gate", "blocked_task": task.get("id"), "summary": str(exc), "next_action": "wait for the live task owner or recover the stale lock"})
            break
        task_passed = False
        final_checks: list[dict[str, Any]] = []
        last_failed_command = "verification"
        task_repair_command = repair_command
        repair_agent = arg_str(args, "repair_agent", "")
        if repair_agent and not task_repair_command:
            try:
                task_repair_command = default_repair_agent_command(repair_agent, task)
            except RepoLocalError as exc:
                block_task_record(repo, root, active_path, task, args.agent, str(exc), final_checks)
                result.update({"status": "blocked", "blocked_task": task["id"], "summary": str(exc), "next_action": "install/configure the repair agent or use --repair-command"})
                break
        elif default_executor_selected and not task_repair_command:
            task_repair_command = default_repair_agent_command(selected_executor_agent, task)
        if result.get("status") == "blocked":
            break
        for attempt_number in range(1, max_attempts + 1):
            strategy = strategies[min(attempt_number - 1, len(strategies) - 1)]
            attempt = {
                "task_id": task["id"],
                "attempt": attempt_number,
                "strategy": strategy,
                "stages": ["build", "verify", "critic", "repair", "judge"],
                "build": {"status": "skipped", "note": "No build adapter command configured; assuming task artifacts are already produced by the invoking agent."},
                "verify": {"status": "running", "checks": []},
                "critic": {"status": "pending", "blocking_findings": []},
                "repair": {"status": "not_needed"},
                "judge": {"status": "pending"},
            }
            if task_build_command:
                if not ensure_budget(result, max_commands, started_at, max_minutes, "build adapter"):
                    break
                before_paths = git_dirty_snapshot(repo)
                build = run_hook_command(repo, task_build_command, task, attempt_number, strategy, "build", command_timeout_seconds, require_protocol=default_executor_selected)
                result["commands_run"] += 1
                violations = scope_violations_after(repo, task, before_paths)
                if violations:
                    build["returncode"] = build["returncode"] or 126
                    build["stderr"] = (build.get("stderr") or "") + "\nscope violations after build adapter: " + ", ".join(violations)
                attempt["build"] = {"status": "passed" if build["returncode"] == 0 else "failed", "result": build}
                if build["returncode"] != 0:
                    attempt["critic"] = {"status": "blocking_findings", "blocking_findings": ["build adapter failed"], "repair_hint": build["stderr"] or build["stdout"]}
                    attempt["judge"] = {"status": "retry_or_block", "reason": "build adapter failed"}
                    last_failed_command = build["command"]
                    result["attempts"].append(attempt)
                    record_attempt(repo, root, task, args.agent, attempt, final_checks)
                    if task_repair_command and attempt_number < max_attempts:
                        if not ensure_budget(result, max_commands, started_at, max_minutes, "repair adapter"):
                            break
                        before_paths = git_dirty_snapshot(repo)
                        repair = run_hook_command(repo, task_repair_command, task, attempt_number, strategy, "repair", command_timeout_seconds, require_protocol=bool(repair_agent) or default_executor_selected, feedback={"checks": final_checks, "critic": attempt["critic"], "build": attempt["build"], "remaining_steps": ["repair", "verify", "critic", "ship"]})
                        result["commands_run"] += 1
                        violations = scope_violations_after(repo, task, before_paths)
                        if violations:
                            repair["returncode"] = repair["returncode"] or 126
                            repair["stderr"] = (repair.get("stderr") or "") + "\nscope violations after repair adapter: " + ", ".join(violations)
                        attempt["repair"] = {"status": "passed" if repair["returncode"] == 0 else "failed", "result": repair}
                        continue
                    break
            if not ensure_budget(result, max_commands, started_at, max_minutes, "verification"):
                break
            checks = run_verification_commands(repo, task, command_budget=max_commands - result["commands_run"], timeout_seconds=command_timeout_seconds)
            final_checks = checks
            attempt["verify"] = {"status": "passed" if all(check["returncode"] == 0 for check in checks) else "failed", "checks": checks}
            result["commands_run"] += len([check for check in checks if check.get("command") and not check.get("budget_exhausted")])
            result["checks"].extend(checks)
            if any(check.get("budget_exhausted") for check in checks):
                result.update({"status": "budget_exhausted", "budget_exhausted": True, "summary": "Budget exhausted during verification.", "next_action": "resume with go-loop using a larger budget or narrower task scope"})
            failed = next((check for check in checks if check["returncode"] != 0), None)
            if failed:
                last_failed_command = failed["command"] or "verification"
                attempt["critic"] = {
                    "status": "blocking_findings",
                    "blocking_findings": [f"verification failed: {last_failed_command}"],
                    "repair_hint": "Fix the failing command output inside task scope, then rerun verification.",
                }
            else:
                builtin_findings = builtin_semantic_findings(repo, task, checks) if semantic_critic else []
                if builtin_findings:
                    attempt["critic"] = {
                        "status": "blocking_findings",
                        "blocking_findings": builtin_findings,
                        "repair_hint": "Resolve the critic findings or create scoped follow-up work before finishing.",
                    }
                    last_failed_command = "semantic critic"
                elif critic_command:
                    if not ensure_budget(result, max_commands, started_at, max_minutes, "critic adapter"):
                        break
                    before_paths = git_dirty_snapshot(repo)
                    critic = run_hook_command(repo, critic_command, task, attempt_number, strategy, "critic", command_timeout_seconds, feedback={"checks": checks, "remaining_steps": ["critic", "ship"]})
                    result["commands_run"] += 1
                    violations = scope_violations_after(repo, task, before_paths)
                    if violations:
                        critic["returncode"] = critic["returncode"] or 126
                        critic["stderr"] = (critic.get("stderr") or "") + "\nscope violations after critic adapter: " + ", ".join(violations)
                    attempt["critic"] = {
                        "status": "passed" if critic["returncode"] == 0 else "blocking_findings",
                        "blocking_findings": [] if critic["returncode"] == 0 else [critic["stderr"] or critic["stdout"] or "critic adapter failed"],
                        "result": critic,
                    }
                    if critic["returncode"] != 0:
                        last_failed_command = critic["command"]
                elif default_executor_selected and selected_executor_agent:
                    if not ensure_budget(result, max_commands, started_at, max_minutes, "deep critic agent"):
                        break
                    before_paths = git_dirty_snapshot(repo)
                    critic = run_default_critic_agent(
                        repo,
                        selected_executor_agent,
                        task,
                        attempt_number,
                        strategy,
                        command_timeout_seconds,
                        feedback={"checks": checks, "remaining_steps": ["critic", "ship"]},
                    )
                    result["commands_run"] += 1
                    violations = scope_violations_after(repo, task, before_paths)
                    if violations:
                        critic["returncode"] = critic["returncode"] or 126
                        critic["stderr"] = (critic.get("stderr") or "") + "\nscope violations after deep critic: " + ", ".join(violations)
                    attempt["critic"] = {
                        "status": "passed" if critic["returncode"] == 0 else "blocking_findings",
                        "blocking_findings": [] if critic["returncode"] == 0 else [critic.get("verdict_text") or critic.get("stderr") or "deep critic blocked"],
                        "result": critic,
                    }
                    if critic["returncode"] != 0:
                        last_failed_command = "deep critic agent"
                else:
                    attempt["critic"] = {"status": "passed", "blocking_findings": []}
            if attempt["verify"]["status"] == "passed" and attempt["critic"]["status"] == "passed":
                attempt["judge"] = {"status": "passed", "reason": "verification and critic passed"}
                result["attempts"].append(attempt)
                record_attempt(repo, root, task, args.agent, attempt, final_checks)
                task_passed = True
                break
            if task_repair_command and attempt_number < max_attempts:
                if not ensure_budget(result, max_commands, started_at, max_minutes, "repair adapter"):
                    break
                before_paths = git_dirty_snapshot(repo)
                repair = run_hook_command(repo, task_repair_command, task, attempt_number, strategy, "repair", command_timeout_seconds, require_protocol=bool(repair_agent) or default_executor_selected, feedback={"checks": final_checks, "critic": attempt["critic"], "build": attempt["build"], "remaining_steps": ["repair", "verify", "critic", "ship"]})
                result["commands_run"] += 1
                violations = scope_violations_after(repo, task, before_paths)
                if violations:
                    repair["returncode"] = repair["returncode"] or 126
                    repair["stderr"] = (repair.get("stderr") or "") + "\nscope violations after repair adapter: " + ", ".join(violations)
                attempt["repair"] = {"status": "passed" if repair["returncode"] == 0 else "failed", "result": repair}
                attempt["judge"] = {"status": "retry", "reason": "repair attempted; rerun loop strategy"}
                result["attempts"].append(attempt)
                record_attempt(repo, root, task, args.agent, attempt, final_checks)
                if repair["returncode"] == 0:
                    continue
                break
            attempt["repair"] = {"status": "requires_agent_repair", "reason": "no repair adapter available or max attempts reached"}
            attempt["judge"] = {"status": "blocked", "reason": "failed after bounded attempt"}
            result["attempts"].append(attempt)
            record_attempt(repo, root, task, args.agent, attempt, final_checks)
            break
        if result.get("budget_exhausted"):
            result.update({"blocked_task": task["id"]})
            break
        if not task_passed:
            block_reason = f"critic blocked after bounded executor attempts: {last_failed_command}"
            if followup_on_block and result["attempts"]:
                last_attempt = result["attempts"][-1]
                findings = (last_attempt.get("critic") or {}).get("blocking_findings") or [block_reason]
                followup = create_followup_task(repo, task, findings, args.agent)
                result.setdefault("created_followups", []).append(followup["id"])
            block_task_record(repo, root, active_path, task, args.agent, block_reason, final_checks)
            result.update({"status": "blocked", "blocked_task": task["id"], "summary": "Bounded executor attempts failed; critic/repair evidence recorded and task moved to blocked.", "next_action": "repair failing gate or configure build/critic/repair adapter, then rerun go-loop"})
            break
        task = load_json(active_path)
        task = verify_pending_outcomes_from_checks(repo, active_path, task, args.agent, final_checks)
        outcome_findings = outcome_completion_findings(task)
        if outcome_findings:
            result.update({
                "status": "blocked",
                "blocked_task": task["id"],
                "outcome_findings": outcome_findings,
                "summary": "Verification passed, but requested-outcome closure is incomplete; task remains active.",
                "next_action": "record every R# with `go-workflow task outcome` and evidence, then rerun go-loop",
            })
            break
        verification_summary = " | ".join(
            f"{check['command'] or 'no verification configured'} rc={check.get('returncode')}"
            for check in final_checks
        )
        critic_status = str((result["attempts"][-1].get("critic") or {}).get("status") or "passed") if result["attempts"] else "passed"
        evidence_summary = f"verification={verification_summary}; critic={critic_status}; runtime={mode}"
        ship_blocker = ship_policy_blocker(ship_policy, allow_push)
        if ship_blocker:
            result.update({
                "status": "blocked",
                "blocked_task": task["id"],
                "summary": "Ship policy blocked completion after verification; task remains active.",
                "next_action": ship_blocker,
            })
            result["ship"].append({"task_id": task["id"], "policy": ship_policy, "status": "blocked", "reason": ship_blocker})
            break
        active_task_before_finish = json.loads(json.dumps(task))
        evidence_path = root / "evidence" / "events.jsonl"
        finish_transaction_id = uuid.uuid4().hex
        done_path = task_path(root, "done", task["id"])
        try:
            delivery_plan = automatic_delivery_plan(repo, task)
        except Exception as exc:
            result.update({
                "status": "blocked",
                "blocked_task": task["id"],
                "summary": "Automatic delivery preflight failed; task remains active.",
                "next_action": str(exc),
            })
            break
        delivery_context = (
            repository_lock(root, f"delivery-{delivery_plan['epic']}")
            if delivery_plan else contextlib.nullcontext()
        )
        try:
            with delivery_context:
                try:
                    done_path = finish_task_record(
                        repo, root, active_path, task, args.agent, evidence_summary, transaction_id=finish_transaction_id,
                    )
                    if critic_status == "passed":
                        delivery = approve_task_after_passed_critic(
                            root,
                            done_path,
                            task["id"],
                            args.agent,
                            transaction_id=finish_transaction_id,
                            delivery_lock_held=bool(delivery_plan),
                        )
                        if delivery:
                            result.setdefault("deliveries", []).append({"task_id": task["id"], **delivery})
                except Exception as exc:
                    restore_active_after_failed_ship(
                        root, active_path, done_path, active_task_before_finish, evidence_path, finish_transaction_id,
                    )
                    result.update({
                        "status": "blocked",
                        "blocked_task": task["id"],
                        "summary": "Finish or review transition failed; task and evidence were restored to active.",
                        "next_action": str(exc),
                    })
                    break
                ship = ship_changes(repo, ship_policy, allow_push, f"go-loop: finish {task['id']}", task)
                result["ship"].append({"task_id": task["id"], **ship})
                if (ship.get("status") in {"blocked", "failed"}
                        or ('execution_contract' in task and ship.get('status') in {'push_failed', 'readback_failed'})):
                    restore_active_after_failed_ship(
                        root, active_path, done_path, active_task_before_finish, evidence_path, finish_transaction_id,
                    )
                    result["deliveries"] = [item for item in result.get("deliveries", []) if item.get("task_id") != task["id"]]
                    result.update({"status": "blocked", "blocked_task": task["id"], "summary": "Ship failed; verified task was restored to active.", "next_action": ship.get("reason") or ship.get("stderr")})
                    break
                if ship.get("status") in {"committed", "pushed"}:
                    attach_ship_provenance(root, done_path, task["id"], args.agent, ship)
                result["completed_tasks"].append(task["id"])
                result["evidence"].append({"task_id": task["id"], "summary": evidence_summary})
                if ship.get("status") in {"push_failed", "readback_failed"}:
                    reason = ship.get("readback_error") or (ship.get("push") or {}).get("stderr") or "push/readback failed"
                    result.update({"status": "ship_pending", "summary": "Task is complete in a local commit, but push/readback proof failed.", "next_action": reason})
                    break
        except StateLockError as exc:
            result.update({
                "status": "blocked",
                "blocked_task": task["id"],
                "summary": "Automatic delivery is busy for this epic; task remains active.",
                "next_action": str(exc),
            })
            break
        result["status"] = "done"
        result["summary"] = f"Completed {len(result['completed_tasks'])} task(s)."
        if len(result["completed_tasks"]) % checkpoint_every_tasks == 0:
            result["checkpoints"].append({"created_at": now_iso(), "completed_tasks": list(result["completed_tasks"]), "status": result["status"], "telegram_policy": plan["run_envelope"].get("telegram_policy", {})})
        if mode != "go-loop" and len(result["completed_tasks"]) >= max(arg_int(args, "max_tasks", 3), 1):
            result["next_action"] = "budget_exhausted_or_batch_complete"
            break
    remaining_tasks = open_tasks(repo)
    if result.get("status") == "done" and remaining_tasks:
        result.update({
            "status": "budget_exhausted",
            "budget_exhausted": True,
            "summary": f"Completed {len(result['completed_tasks'])} task(s); {len(remaining_tasks)} open task(s) remain.",
            "next_action": "resume with the persisted go-loop command",
        })
    elif result.get("status") == "done":
        unfinished = unfinished_task_ids(repo)
        pending_reviews = pending_review_task_ids(repo)
        if unfinished.get("active") or unfinished.get("blocked") or pending_reviews:
            blocked_id = (unfinished.get("blocked") or unfinished.get("active") or pending_reviews or [None])[0]
            if pending_reviews and not (unfinished.get("active") or unfinished.get("blocked")):
                result.update({
                    "status": "review_pending",
                    "blocked_task": blocked_id,
                    "summary": "Work is complete, but explicit review approval is still pending.",
                    "next_action": "review completed work and record `go-workflow task review --status approved` or `needs_fix`",
                })
            else:
                result.update({
                    "status": "blocked",
                    "blocked_task": blocked_id,
                    "summary": "Open work is exhausted, but active or blocked task state prevents goal completion.",
                    "next_action": "repair, requeue, or explicitly resolve the unfinished task",
                })
        else:
            project = load_json(root / "project.json")
            if project.get("project_mode") == "template":
                contract_errors = validate_repo(repo)
                result["completion_audit"] = {
                    "schema": "go-workflow.template-smoke-completion.v1",
                    "setup_required": True,
                    "contract_valid": not contract_errors,
                    "contract_errors": contract_errors,
                    "completed_example_tasks": list(result["completed_tasks"]),
                }
                if contract_errors:
                    result.update({
                        "status": "goal_incomplete",
                        "summary": "The template smoke ran, but the starter contract is invalid.",
                        "next_action": "repair the template contract before project customization",
                    })
            else:
                vision = load_json(root / "vision.json")
                done_tasks = [load_json(path) for path in sorted((root / "tasks" / "done").glob("*.json"))]
                task_evidence_complete = bool(done_tasks) and all(bool(task.get("evidence")) for task in done_tasks)
                lifecycle = lifecycle_report(repo)
                task_evidence_complete = task_evidence_complete and lifecycle['evidence_valid']
                contract_errors = validate_repo(repo)
                goal_checks: list[dict[str, Any]] = []
                if ensure_budget(result, max_commands, started_at, max_minutes, "goal completion verification"):
                    goal_checks = run_verification_commands(
                        repo,
                        {"id": "goal-completion", "verification": project.get("default_verification", [])},
                        command_budget=max_commands - result["commands_run"],
                        timeout_seconds=command_timeout_seconds,
                    )
                    result["commands_run"] += len([check for check in goal_checks if check.get("command") and not check.get("budget_exhausted")])
                    result["checks"].extend(goal_checks)
                project_verification_passed = bool(goal_checks) and all(check.get("command") and check.get("returncode") == 0 for check in goal_checks)
                completion_audit = {
                    "schema": "go-workflow.goal-completion-audit.v1",
                    "vision_status": vision.get("status"),
                    "success_metrics": vision.get("success_metrics", []),
                    "success_metrics_declared": bool(vision.get("success_metrics")),
                    "contract_valid": not contract_errors,
                    "contract_errors": contract_errors,
                    "task_evidence_complete": task_evidence_complete,
                    "lifecycle": lifecycle,
                    "project_verification": goal_checks,
                    "project_verification_passed": project_verification_passed,
                    "open_tasks": 0,
                    "active_tasks": unfinished.get("active", []),
                    "blocked_tasks": unfinished.get("blocked", []),
                }
                result["completion_audit"] = completion_audit
                completion_proven = (
                    completion_audit["vision_status"] == "active"
                    and completion_audit["success_metrics_declared"]
                    and completion_audit["contract_valid"]
                    and completion_audit["task_evidence_complete"]
                    and completion_audit["project_verification_passed"]
                )
                if not completion_proven and not result.get("budget_exhausted"):
                    result.update({
                        "status": "goal_incomplete",
                        "summary": "Tasks are exhausted, but the vision-level completion audit is not proven.",
                        "next_action": "create a follow-up task for the failing completion-audit evidence and resume go-loop",
                    })
    append_jsonl(root / "reflections" / "events.jsonl", event("go-auto", "auto.reflected", args.agent, {"mode": mode, "status": result["status"], "completed_tasks": result["completed_tasks"], "blocked_task": result["blocked_task"], "next_action": result["next_action"]}))
    write_latest_run_state(repo, root, result, args, mode)
    if ship_policy != "none" and result.get("completed_tasks"):
        final_ship = ship_changes(repo, ship_policy, allow_push, "go-loop: finalize run state")
        result["ship"].append({"task_id": "run-state", **final_ship})
    return (0 if result["status"] in {"done", "budget_exhausted"} else 1), result


def build_agent_handoff(repo: Path, args: argparse.Namespace, mode: str) -> dict[str, Any]:
    plan = build_loop_plan(repo, args, mode=mode)
    selected: list[dict[str, Any]] = []
    verification_commands: list[str] = []
    for _, task in open_tasks(repo)[: max(args.max_tasks, 1)]:
        selected.append({
            "id": task.get("id"),
            "execution_mode": task.get("execution_mode", "mechanical"),
            "summary": task.get("summary"),
            "description": task.get("description", ""),
            "scope": task.get("scope", {}),
            "acceptance": task.get("acceptance", []),
            "verification": task.get("verification", []),
        })
        verification_commands.extend(task.get("verification", []) or [])
    return {
        "schema": "go-workflow.agent-handoff.v1",
        "mode": mode,
        "target_runtime": "codex-or-hermes-agent",
        "repo": str(repo),
        "project_id": plan["project_id"],
        "command": f"{mode} --execute",
        "control_handoff": True,
        "tasks": selected,
        "run_envelope": plan["run_envelope"],
        "execution_policy": plan["execution_policy"],
        "gates": plan["execution_policy"]["human_gates"],
        "expected_evidence": {
            "verification_commands": verification_commands,
            "task_state": ".go/tasks/done/<task-id>.json or .go/tasks/blocked/<task-id>.json",
            "events": [".go/evidence/events.jsonl", ".go/runs/events.jsonl", ".go/reflections/events.jsonl"],
            "final_result_schema": "go-workflow.auto-run-result.v1",
        },
        "agent_instructions": [
            "Do not ask when a safe default exists.",
            "Edit only within task scope.",
            "Run verification before finish.",
            "Finish with evidence or block with check output.",
            "Escalate to go-loop when self-reflect or review finds same-scope repair work.",
        ],
    }



INTENT_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+(?:[.)])?|[-*+])\s+(.+?)\s*$")


def intent_task_contract(intent: str) -> dict[str, Any]:
    """Turn free-form GO input into traceable outcomes without syntactic task spam."""
    normalized = intent.strip() or "Continue toward project goal"
    preface: list[str] = []
    items: list[str] = []
    current_item: list[str] | None = None
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = INTENT_LIST_ITEM_RE.match(line)
        if match:
            if current_item:
                items.append(" ".join(current_item))
            current_item = [match.group(1).strip()]
        elif current_item is not None:
            current_item.append(line)
        else:
            preface.append(line)
    if current_item:
        items.append(" ".join(current_item))

    if items:
        summary = " ".join(preface) if preface else f"Implement {len(items)} requested outcomes"
        outcomes = items
        source = "numbered_or_bulleted_item"
    else:
        summary = " ".join(normalized.split())
        outcomes = [summary]
        source = "intent"
    requested_outcomes = [
        {"id": f"R{index}", "text": text, "source": source, "status": "pending", "evidence": []}
        for index, text in enumerate(outcomes, start=1)
    ]
    return {
        "summary": summary,
        "requested_outcomes": requested_outcomes,
        "acceptance": [f"{item['id']}: {item['text']}" for item in requested_outcomes]
        + ["All requested outcomes are verified or explicitly blocked with evidence."],
        "decomposition_policy": {
            "mode": "semantic",
            "default": "bundle_when_coherent",
            "split_when": [
                "outcomes_can_ship_independently",
                "outcomes_require_different_verification",
                "outcomes_touch_distinct_components_or_repositories",
                "combined_scope_is_too_large_for_one_safe_claim",
            ],
            "rule": "List numbering is acceptance coverage, not an automatic task boundary.",
        },
    }


def intent_task_scope(repo: Path) -> dict[str, list[str]]:
    paths = [".go/**", "README.md", "docs/**", "tests/**"]
    for directory in ("src", "go_workflow", "scripts", "cli", "schemas"):
        if (repo / directory).is_dir():
            paths.append(f"{directory}/**")
    for filename in ("CHANGELOG.md", "pyproject.toml", "Makefile"):
        if (repo / filename).is_file():
            paths.append(filename)
    return {"read": paths.copy(), "modify": paths.copy()}


def create_task_from_intent(repo: Path, intent: str, agent: str = "agent", source_ref: str = "") -> dict[str, Any]:
    root = go_root(repo)
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot create intent task in invalid .go state:\n- " + "\n- ".join(errors))
    project = load_json(root / "project.json")
    contract = intent_task_contract(intent)
    summary = contract["summary"]
    task_id = slugify(summary).lower()[:48].strip("-") or "continue-project-goal"
    base_id = task_id
    index = 2
    while any(task_path(root, state, task_id).exists() for state in ("open", "active", "blocked", "done")):
        suffix = f"-{index}"
        task_id = (base_id[: 48 - len(suffix)] + suffix).strip("-")
        index += 1
    verification = project.get("default_verification") or ["git diff --check"]
    source_text = intent.strip() or summary
    intent_source = {
        "text": source_text,
        "sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "source_ref": source_ref.strip() or None,
    }
    task = {
        "schema": TASK_SCHEMA,
        "kind": "task",
        "execution_mode": "agent",
        "shareable_delivery": "auto",
        "id": task_id,
        "project": project["id"],
        "status": "open",
        "summary": summary,
        "description": f"Created from bare go intent:\n\n{source_text}",
        "scope": intent_task_scope(repo),
        "outcome_tracking_version": 1,
        "intent_source": intent_source,
        "requested_outcomes": contract["requested_outcomes"],
        "decomposition_policy": contract["decomposition_policy"],
        "acceptance": contract["acceptance"],
        "verification": verification,
        "claim": {"agent": None, "claimed_at": None},
        "evidence": [],
    }
    apply_intake_contract(repo, task)
    target = task_path(root, "open", task_id)
    dump_json(target, task)
    hierarchy = load_json(root / "hierarchy.json")
    epics = hierarchy_epics(hierarchy)
    if epics:
        epics[0].setdefault("tasks", [])
        if task_id not in epics[0]["tasks"]:
            epics[0]["tasks"].append(task_id)
        set_hierarchy_epics(hierarchy, epics)
        dump_json(root / "hierarchy.json", hierarchy)
    append_jsonl(root / "runs" / "events.jsonl", event(task_id, "task.created", agent, {
        "action": "task.created_from_go_intent",
        "intent": source_text,
        "intent_sha256": intent_source["sha256"],
        "intent_source_ref": intent_source["source_ref"],
        "path": relative(repo, target),
        "requested_outcome_count": len(contract["requested_outcomes"]),
        "decomposition_mode": contract["decomposition_policy"]["mode"],
    }))
    errors = validate_repo(repo)
    if errors:
        target.unlink(missing_ok=True)
        raise RepoLocalError("created intent task invalidated .go state:\n- " + "\n- ".join(errors))
    return {
        "id": task_id,
        "summary": summary,
        "path": relative(repo, target),
        "requested_outcome_count": len(contract["requested_outcomes"]),
        "decomposition_mode": contract["decomposition_policy"]["mode"],
        "intent_sha256": intent_source["sha256"],
        "intent_source_ref": intent_source["source_ref"],
    }


def cmd_intake_explore(args: argparse.Namespace) -> int:
    """Run one bounded read-only semantic assessment, then optionally materialize its plan."""
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot explore intake in invalid .go state:\n- " + "\n- ".join(errors))
    try:
        request = prepare_request(repo, args.intent, args.source_ref, args.authority)
    except ValueError as exc:
        raise RepoLocalError(str(exc)) from exc
    target = record_path(repo, request)
    if target.is_file():
        existing = load_json(target)
        if existing.get("schema") == INTAKE_RECORD_SCHEMA and existing.get("status") == "applied":
            payload = {"schema": "go-workflow.intake-explore-result.v1", "status": "already_applied",
                       "intake_id": existing["id"], "task_ids": existing["task_ids"],
                       "questions": existing["assessment"]["questions"]}
            print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else f"intake: {payload['status']}")
            return 0
    if not args.write:
        payload = {"schema": "go-workflow.intake-explore-result.v1", "status": "prepared", "request": request,
                   "write_required": True}
        print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else "intake: prepared")
        return 0
    synthetic = {
        "schema": TASK_SCHEMA, "kind": "task", "id": "intake-" + request["sha256"][:16],
        "project": request["project"], "status": "open", "summary": "Assess bounded intake",
        "scope": {"read": ["**"], "modify": []}, "acceptance": ["Return a grounded intake assessment"],
        "verification": ["adapter protocol validation"], "claim": {"agent": None, "claimed_at": None},
        "intake_request": request,
    }
    assessment_command = args.assessment_command
    if not assessment_command:
        if args.executor_agent != "codex" or not args.model:
            raise RepoLocalError("--write requires --assessment-command or explicit --executor-agent codex --model --effort")
        profile = {"id": args.model, "effort": args.effort}
        synthetic["execution_contract"] = {
            "schema": "go-workflow.execution-contract.v1", "task_kind": "no_change",
            "model": profile, "critic_model": profile,
            "release": {"mode": "none", "reason": "Read-only bounded intake assessment"},
        }
        assessment_command = native_agent_command(
            "codex", "critic",
            "Assess only the supplied task.intake_request against the repository snapshot. Do not edit files. "
            "Include exactly one extra intake_assessment object in the final adapter result. Use this exact shape "
            "(replace values, retain every key and no others): "
            "{\"schema\":\"go-workflow.intake-assessment.v1\",\"request_sha256\":\"<task.intake_request.sha256>\","
            "\"summary\":\"<grounded summary>\",\"disposition\":\"ready|questions|research_first\",\"actions\":[{"
            "\"action\":\"create|reuse|update\",\"task_id\":\"<id>\",\"summary\":\"<summary>\","
            "\"work_type\":\"implementation|bug_fix|research\",\"root_cause\":\"known|unknown|not_applicable\","
            "\"substantial\":true,\"outcome_ids\":[\"O1\"],\"scope\":{\"read\":[\"path/**\"],"
            "\"modify\":[\"path/**\"]},\"behavior\":{\"before\":[\"...\"],\"after\":[\"...\"],"
            "\"states\":[\"...\"],\"edges\":[\"...\"]},\"non_goals\":[\"...\"],\"dependencies\":[],"
            "\"delegated_choices\":[\"...\"],\"question_ids\":[],\"acceptance\":[\"...\"],"
            "\"verification\":[\"...\"]}],\"questions\":[],\"relevant_decisions\":[]}. "
            "Map every request outcome exactly once or more. Existing task IDs require reuse/update; new IDs require create. "
            "Unknown bug causes must use work_type research. Questions use {id,text,owner,blocks}; decisions use {id,status}.",
            model_profile=profile,
        )
    with tempfile.TemporaryDirectory(prefix="go-intake-assessment-") as directory:
        assessment_repo = Path(directory) / "repo"
        shutil.copytree(repo, assessment_repo, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".mypy_cache"))
        subprocess.run(["git", "init", "-q"], cwd=assessment_repo, check=True)
        result = run_hook_command(assessment_repo, assessment_command, synthetic, 1,
                                  "bounded-intake-exploration", "critic",
                                  timeout_seconds=args.timeout_seconds, require_protocol=True)
    if result.get("status") != "success":
        detail = str(result.get("summary") or "unknown failure")
        process_detail = str(result.get("stderr") or result.get("stdout") or "").strip()
        if process_detail:
            detail += ": " + process_detail[-1000:]
        raise RepoLocalError("intake assessment adapter did not succeed: " + detail)
    assessment = result.get("intake_assessment")
    findings = validate_assessment(assessment, request)
    if findings:
        rendered = json.dumps(assessment, ensure_ascii=False, sort_keys=True)[:2000]
        raise RepoLocalError("invalid intake assessment:\n- " + "\n- ".join(findings) + "\npayload: " + rendered)
    root = go_root(repo)
    project = load_json(root / "project.json")
    task_ids: list[str] = []
    created_ids: list[str] = []
    questions = {item.get("id"): item for item in assessment["questions"] if isinstance(item, dict)}
    outcomes = {item["id"]: item for item in request["outcomes"]}
    prepared: list[tuple[dict[str, Any], list[Path], dict[str, Any]]] = []
    for action in assessment["actions"]:
        task_id = action["task_id"]
        matches = [task_path(root, state, task_id) for state in ("open", "active", "blocked", "done")
                   if task_path(root, state, task_id).is_file()]
        if action["action"] in {"reuse", "update"}:
            if len(matches) != 1:
                raise RepoLocalError("assessment references missing or duplicated existing task: " + task_id)
            task = load_json(matches[0])
            if action["action"] == "update" and task["status"] != "open":
                raise RepoLocalError("only an open task may receive intake feedback: " + task_id)
        else:
            if matches:
                raise RepoLocalError("assessment create collides with existing task: " + task_id)
            task = {
                "schema": TASK_SCHEMA, "kind": "task", "execution_mode": "agent", "shareable_delivery": "auto",
                "id": task_id, "project": project["id"], "status": "open", "summary": action["summary"],
                "description": assessment["summary"], "scope": action["scope"],
                "acceptance": action["acceptance"], "verification": action["verification"],
                "claim": {"agent": None, "claimed_at": None}, "evidence": [],
            }
        current = {item["text"]: item for item in task.get("requested_outcomes", [])}
        for outcome_id in action["outcome_ids"]:
            outcome = outcomes[outcome_id]
            current.setdefault(outcome["text"], {"id": "R" + str(len(current) + 1), "text": outcome["text"],
                                                   "source": outcome["source_ref"] + "#" + outcome_id,
                                                   "status": "pending", "evidence": []})
        history = list(task.get("intake_history", []))
        if request["intent"]["sha256"] not in {item.get("sha256") for item in history if isinstance(item, dict)}:
            history.append(request["intent"])
        if action["action"] == "update":
            task["acceptance"] = list(dict.fromkeys([*task.get("acceptance", []), *action["acceptance"]]))
            task["verification"] = list(dict.fromkeys([*task.get("verification", []), *action["verification"]]))
            task["scope"] = {
                key: list(dict.fromkeys([*(task.get("scope") or {}).get(key, []), *action["scope"][key]]))
                for key in ("read", "modify")
            }
        task.update(outcome_tracking_version=1, requested_outcomes=list(current.values()),
                    dependencies=action["dependencies"], intake_history=history,
                    task_design={key: action[key] for key in ("work_type", "root_cause", "substantial", "behavior", "non_goals", "delegated_choices")},
                    intake={"id": request["id"], "request_sha256": request["sha256"],
                            "authority": request["authority"], "action": action["action"],
                            "unresolved_question_ids": action["question_ids"]})
        task.setdefault("intent_source", request["intent"])
        task_errors = validate_task(task, f"intake:{task_id}")
        if task_errors:
            raise RepoLocalError("intake produced invalid task:\n- " + "\n- ".join(task_errors))
        prepared.append((action, matches, task))
    for action, matches, task in prepared:
        task_id = action["task_id"]
        if action["action"] == "create":
            dump_json(task_path(root, "open", task_id), task)
            created_ids.append(task_id)
        elif action["action"] == "update":
            dump_json(matches[0], task)
        task_ids.append(task_id)
    if created_ids:
        hierarchy = load_json(root / "hierarchy.json")
        epics = hierarchy_epics(hierarchy)
        if epics:
            epics[0].setdefault("tasks", [])
            epics[0]["tasks"].extend(task_id for task_id in created_ids if task_id not in epics[0]["tasks"])
            set_hierarchy_epics(hierarchy, epics)
            dump_json(root / "hierarchy.json", hierarchy)
    record = {"schema": INTAKE_RECORD_SCHEMA, "id": request["id"], "status": "applied",
              "request": request, "assessment": assessment, "task_ids": task_ids,
              "authority": request["authority"],
              "adapter": {key: result.get(key) for key in
                          ("summary", "command", "returncode", "timed_out", "model_selection", "usage")
                          if key in result}}
    target.parent.mkdir(parents=True, exist_ok=True)
    dump_json(target, record)
    append_jsonl(root / "runs/events.jsonl", event("intake", "run.checked", args.agent, {
        "action": "intake.assessed", "intake_id": request["id"], "task_ids": task_ids,
        "authority": request["authority"], "question_ids": sorted(questions), "path": relative(repo, target),
    }))
    payload = {"schema": "go-workflow.intake-explore-result.v1", "status": "applied",
               "intake_id": request["id"], "task_ids": task_ids, "questions": assessment["questions"]}
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else f"intake: applied {', '.join(task_ids)}")
    return 0


def validate_execution_brief(data: dict[str, Any]) -> list[str]:
    """Validate the compact, durable handoff between recommendation and execution."""
    errors: list[str] = []
    require(data.get("schema") == EXECUTION_BRIEF_SCHEMA, errors, "execution brief: schema mismatch")
    for field in ("destination", "problem", "chosen_approach"):
        value = data.get(field)
        require(isinstance(value, str) and bool(value.strip()), errors, f"execution brief: {field} must be a non-empty string")
    chosen_approach = data.get("chosen_approach")
    if isinstance(chosen_approach, str):
        require(len(chosen_approach) <= 2000, errors, "execution brief: chosen_approach exceeds compact 2000 character boundary")
    non_goals = data.get("non_goals")
    require(isinstance(non_goals, list) and all(isinstance(item, str) and item.strip() for item in non_goals or []), errors, "execution brief: non_goals must be a list of non-empty strings")

    source = data.get("source")
    require(isinstance(source, dict), errors, "execution brief: source must be an object")
    if isinstance(source, dict):
        recommendation = source.get("recommendation")
        digest = source.get("sha256")
        require(isinstance(recommendation, str) and bool(recommendation.strip()), errors, "execution brief: source.recommendation must be a non-empty string")
        require(recommendation == chosen_approach, errors, "execution brief: source.recommendation must equal chosen_approach")
        if isinstance(recommendation, str):
            expected = hashlib.sha256(recommendation.encode("utf-8")).hexdigest()
            require(digest == expected, errors, "execution brief: recommendation sha256 does not match")
        source_ref = source.get("source_ref")
        require(source_ref is None or isinstance(source_ref, str), errors, "execution brief: source.source_ref must be a string or null")

    work_units = data.get("work_units")
    require(isinstance(work_units, list) and bool(work_units), errors, "execution brief: work_units must be a non-empty list")
    seen_ids: set[str] = set()
    if isinstance(work_units, list):
        for position, unit in enumerate(work_units, start=1):
            prefix = f"execution brief: work_units[{position}]"
            require(isinstance(unit, dict), errors, f"{prefix} must be an object")
            if not isinstance(unit, dict):
                continue
            if "execution_contract" in unit:
                errors.extend(validate_execution_contract(unit["execution_contract"], partial=True))
            if "dependencies" in unit:
                errors.extend(validate_dependencies(unit["dependencies"]))
            unit_id = unit.get("id")
            require(isinstance(unit_id, str) and bool(TASK_ID_RE.fullmatch(unit_id)), errors, f"{prefix}.id is invalid")
            if isinstance(unit_id, str):
                require(unit_id not in seen_ids, errors, f"{prefix}.id is duplicated: {unit_id}")
                seen_ids.add(unit_id)
            require(isinstance(unit.get("summary"), str) and bool(unit.get("summary", "").strip()), errors, f"{prefix}.summary must be a non-empty string")
            require(unit.get("execution_mode", "agent") in {"mechanical", "agent"}, errors, f"{prefix}.execution_mode must be mechanical or agent")
            require(unit.get("shareable_delivery", "auto") in {"auto", "required", "none"}, errors, f"{prefix}.shareable_delivery must be auto, required, or none")
            scope = unit.get("scope")
            require(isinstance(scope, dict), errors, f"{prefix}.scope must be an object")
            if isinstance(scope, dict):
                for field in ("read", "modify"):
                    values = scope.get(field)
                    require(isinstance(values, list) and all(isinstance(value, str) and value.strip() for value in values or []), errors, f"{prefix}.scope.{field} must be a list of non-empty strings")
            for field in ("acceptance", "verification"):
                values = unit.get(field)
                require(isinstance(values, list) and bool(values) and all(isinstance(value, str) and value.strip() for value in values or []), errors, f"{prefix}.{field} must be a non-empty list of non-empty strings")
    return errors


def load_execution_brief(path: Path) -> dict[str, Any]:
    brief = load_json(path.resolve())
    errors = validate_execution_brief(brief)
    if errors:
        raise RepoLocalError("invalid execution brief:\n- " + "\n- ".join(errors))
    return brief


def pending_recommendation_path(repo: Path) -> Path:
    return go_root(repo) / "recommendations" / "pending.json"


def cmd_recommendation_create(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot create recommendation in invalid .go state:\n- " + "\n- ".join(errors))
    brief = load_execution_brief(Path(args.brief))
    if args.read_only:
        payload = {
            "schema": "go-workflow.recommendation-create-result.v1",
            "status": "read_only",
            "repo": str(repo),
            "path": None,
            "planning_state_authorized": False,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else "recommendation: read_only (not written)")
        return 0

    root = go_root(repo)
    project = load_json(root / "project.json")
    target = pending_recommendation_path(repo)
    if target.exists() and not args.replace:
        raise RepoLocalError("pending recommendation already exists; consume it or pass --replace")
    digest = brief["source"]["sha256"]
    record = {
        "schema": RECOMMENDATION_SCHEMA,
        "kind": "recommendation",
        "id": f"recommendation-{digest[:16]}",
        "project": project["id"],
        "status": "pending",
        "created_at": now_iso(),
        "authority": {
            "mode": args.authority,
            "source": args.authority_source,
            "implementation_authorized": args.authority == "execute",
            "planning_state_authorized": True,
        },
        "brief": brief,
    }
    record_errors = validate_recommendation(record, relative(repo, target), project["id"])
    if record_errors:
        raise RepoLocalError("invalid recommendation record:\n- " + "\n- ".join(record_errors))
    target.parent.mkdir(parents=True, exist_ok=True)
    dump_json(target, record)
    append_jsonl(root / "runs" / "events.jsonl", event("recommendation", "run.checked", args.agent, {
        "action": "recommendation.created",
        "recommendation_id": record["id"],
        "authority": record["authority"],
        "recommendation_sha256": digest,
        "source_ref": brief["source"].get("source_ref"),
        "path": relative(repo, target),
    }))
    errors = validate_repo(repo)
    if errors:
        target.unlink(missing_ok=True)
        raise RepoLocalError("recommendation invalidated .go state:\n- " + "\n- ".join(errors))
    payload = {
        "schema": "go-workflow.recommendation-create-result.v1",
        "status": "pending",
        "repo": str(repo),
        "id": record["id"],
        "path": relative(repo, target),
        "authority": record["authority"],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else f"recommendation: {payload['path']}")
    return 0


def cmd_recommendation_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot read recommendation status from invalid .go state:\n- " + "\n- ".join(errors))
    target = pending_recommendation_path(repo)
    record = load_json(target) if target.is_file() else None
    payload = {
        "schema": "go-workflow.recommendation-status.v1",
        "repo": str(repo),
        "status": "pending" if record else "empty",
        "path": relative(repo, target) if record else None,
        "recommendation": record,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else f"recommendation: {payload['status']}")
    return 0


def archive_applied_recommendation(repo: Path, record: dict[str, Any], task_ids: list[str], authority_source: str) -> dict[str, Any]:
    source = pending_recommendation_path(repo)
    applied = dict(record)
    applied["status"] = "applied"
    applied["resolved_at"] = now_iso()
    applied["applied_tasks"] = task_ids
    applied["promotion"] = {
        "authority": "execute",
        "authority_source": authority_source,
    }
    target = go_root(repo) / "recommendations" / "applied" / f"{record['id']}.json"
    record_errors = validate_recommendation(applied, relative(repo, target), str(record.get("project") or ""))
    if record_errors:
        raise RepoLocalError("cannot archive invalid applied recommendation:\n- " + "\n- ".join(record_errors))
    atomic_move_json(source, target, applied)
    append_jsonl(go_root(repo) / "runs" / "events.jsonl", event("recommendation", "run.checked", "go", {
        "action": "recommendation.applied",
        "recommendation_id": record["id"],
        "task_ids": task_ids,
        "authority_source": authority_source,
        "path": relative(repo, target),
    }))
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("applied recommendation invalidated .go state:\n- " + "\n- ".join(errors))
    return {
        "id": record["id"],
        "status": "applied",
        "path": relative(repo, target),
        "task_ids": task_ids,
        "authority_source": authority_source,
    }


def create_tasks_from_execution_brief(repo: Path, brief: dict[str, Any], agent: str = "agent") -> list[dict[str, Any]]:
    """Compile explicit semantic work units to repo-local tasks without re-expanding chat prose."""
    root = go_root(repo)
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot create execution brief tasks in invalid .go state:\n- " + "\n- ".join(errors))
    brief_errors = validate_execution_brief(brief)
    if brief_errors:
        raise RepoLocalError("invalid execution brief:\n- " + "\n- ".join(brief_errors))
    project = load_json(root / "project.json")
    work_units = brief["work_units"]
    for unit in work_units:
        if any(task_path(root, state, unit["id"]).exists() for state in ("open", "active", "blocked", "done")):
            raise RepoLocalError(f"execution brief task already exists: {unit['id']}")

    candidates = []
    for unit in work_units:
        candidate = {"id": unit["id"], "project": project["id"], "status": "open"}
        apply_intake_contract(repo, candidate, unit)
        candidates.append(candidate)
    for candidate in candidates:
        dependency_errors = dependency_findings(repo, candidate, candidates=candidates)
        if dependency_errors:
            raise RepoLocalError("invalid dependencies: " + "; ".join(dependency_errors))

    compact_brief = {
        "schema": brief["schema"],
        "destination": brief["destination"],
        "problem": brief["problem"],
        "chosen_approach": brief["chosen_approach"],
        "non_goals": brief["non_goals"],
        "source": brief["source"],
    }
    intent_source = {
        "text": brief["problem"],
        "sha256": hashlib.sha256(brief["problem"].encode("utf-8")).hexdigest(),
        "source_ref": brief["source"].get("source_ref"),
    }
    hierarchy = load_json(root / "hierarchy.json")
    epics = hierarchy_epics(hierarchy)
    created: list[dict[str, Any]] = []
    total = len(work_units)
    for position, unit in enumerate(work_units, start=1):
        task = {
            "schema": TASK_SCHEMA,
            "kind": "task",
            "execution_mode": unit.get("execution_mode", "agent"),
            "shareable_delivery": unit.get("shareable_delivery", "auto"),
            "id": unit["id"],
            "project": project["id"],
            "status": "open",
            "summary": unit["summary"],
            "description": f"Execution brief work unit {position}/{total}. Destination: {brief['destination']}",
            "scope": unit["scope"],
            "intent_source": intent_source,
            "execution_brief": compact_brief,
            "work_unit": {"id": unit["id"], "position": position, "total": total},
            "outcome_tracking_version": 1,
            "requested_outcomes": [
                {
                    "id": f"R{index}",
                    "text": acceptance,
                    "source": "execution_brief_acceptance",
                    "status": "pending",
                    "evidence": [],
                }
                for index, acceptance in enumerate(unit["acceptance"], start=1)
            ],
            "acceptance": unit["acceptance"],
            "verification": unit["verification"],
            "claim": {"agent": None, "claimed_at": None},
            "evidence": [],
            "order": position,
        }
        task.update({key: value for key, value in candidates[position - 1].items()
                     if key in {"execution_contract", "dependencies"}})
        target = task_path(root, "open", unit["id"])
        dump_json(target, task)
        if epics:
            epics[0].setdefault("tasks", [])
            if unit["id"] not in epics[0]["tasks"]:
                epics[0]["tasks"].append(unit["id"])
        append_jsonl(root / "runs" / "events.jsonl", event(unit["id"], "task.created", agent, {
            "action": "task.created_from_execution_brief",
            "path": relative(repo, target),
            "work_unit_position": position,
            "work_unit_total": total,
            "recommendation_sha256": brief["source"]["sha256"],
            "source_ref": brief["source"].get("source_ref"),
        }))
        created.append({"id": unit["id"], "summary": unit["summary"], "path": relative(repo, target)})
    if epics:
        set_hierarchy_epics(hierarchy, epics)
        dump_json(root / "hierarchy.json", hierarchy)
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("execution brief tasks invalidated .go state:\n- " + "\n- ".join(errors))
    return created


def _public_campaign(repo, **kwargs):
    from .campaign_intake import materialize_until_scope
    try:
        return materialize_until_scope(repo, **kwargs)
    except ValueError as exc:
        raise RepoLocalError('Taskwise execution configuration required: ' + str(exc)) from exc


def cmd_go(args: argparse.Namespace) -> int:
    """Bare go universal router: route loose vs repo-local work and optionally execute."""
    repo = Path(args.repo).resolve()
    intent = (args.intent or "").strip()
    if re.fullmatch(r"go\s*:?\s*", intent, flags=re.I):
        intent = ""
    continuation_promotion = bool(re.fullmatch(
        r"(?:aan de slag|doe het|voer (?:het )?uit|ga verder|werk verder|continue|finish|sent as goal)",
        intent,
        flags=re.I,
    ))
    from .routing import until_scope_intent
    if getattr(args, 'campaign', ''):
        if intent:
            raise RepoLocalError('Existing campaign scope is frozen; record a campaign amendment before changing its intent')
        return cmd_auto(args)  # Existing frozen contract retains its original authority.
    named = re.fullmatch(r'(?:go\s+)?([A-Za-z0-9][A-Za-z0-9._-]*)', intent, flags=re.I)
    if named and any((repo / '.go/tasks' / state / (named.group(1) + '.json')).is_file() for state in ('open', 'active', 'blocked', 'done')):
        args.task_id = named.group(1)
        intent = ''
    if until_scope_intent(intent):
        from .state_io import atomic_json
        errors = validate_repo(repo)
        if errors:
            raise RepoLocalError("invalid repository: " + "; ".join(errors))
        campaign_id = "taskwise-" + datetime.now().strftime("%Y%m%dT%H%M%S%f")
        contract = _public_campaign(repo, intent=intent,
            source_ref=args.intent_source_ref or "user:go-until-scope",
            campaign_id=campaign_id, budget=getattr(args, "explicit_campaign_budget", None),
            ship_policy=getattr(args, "explicit_ship_policy", None))
        if not (args.write or args.execute):
            print(json.dumps({"mode": "dry_run", "campaign": contract}, indent=2, ensure_ascii=False))
            return 0
        path = go_root(repo) / "campaigns" / (campaign_id + ".json")
        atomic_json(path, contract)
        args.campaign = str(path)
        args.previous_campaign = ""
        args.campaign_workspace_root = str(repo.parent / (repo.name + "-task-workspaces"))
        args.emit_handoff = False
        return cmd_auto(args)
    root = go_root(repo)
    state = {
        "repo_exists": repo.exists(),
        "has_go": root.is_dir(),
        "has_project": (root / "project.json").is_file(),
        "has_vision": (root / "vision.json").is_file(),
        "has_principles": (root / "architecture-principles.json").is_file(),
        "has_hierarchy": (root / "hierarchy.json").is_file(),
    }
    result: dict[str, Any] = {
        "schema": "go-workflow.bare-go.v1",
        "repo": str(repo),
        "intent": intent,
        "state": state,
        "created_task": None,
        "created_tasks": [],
        "recommendation_promotion": None,
        "action": None,
        "plan": None,
    }
    if not state["repo_exists"]:
        result.update({"action": "spike", "reason": "repo missing; create repo-local .go contract first", "next_command": shell_join("python3", Path(__file__).resolve(), "spike", repo, "--brief", intent or "<intent>")})
    elif not state["has_project"]:
        result.update({"action": "spike", "reason": "repo has no .go/project.json; repair/adopt contract first", "next_command": shell_join("python3", Path(__file__).resolve(), "spike", repo, "--brief", intent or "<intent>", "--skip-repo-complete")})
    else:
        errors = validate_repo(repo)
        if errors:
            result.update({"action": "contract_repair_required", "reason": "repo-local .go contract is invalid", "errors": errors})
        else:
            pending_record = None
            if not args.execution_brief and (not intent or continuation_promotion) and pending_recommendation_path(repo).is_file():
                pending_record = load_json(pending_recommendation_path(repo))
                pending_errors = validate_recommendation(pending_record, relative(repo, pending_recommendation_path(repo)), str(load_json(root / "project.json").get("id") or ""))
                if pending_errors:
                    raise RepoLocalError("invalid pending recommendation:\n- " + "\n- ".join(pending_errors))
            brief = load_execution_brief(Path(args.execution_brief)) if args.execution_brief else pending_record["brief"] if pending_record else None
            if pending_record and continuation_promotion:
                intent = ""
            if brief and intent:
                raise RepoLocalError("use either --intent or --execution-brief, not both")
            routing_text = intent or (brief["problem"] if brief else "")
            mode = "go-loop" if args.loop or any(word in routing_text.lower() for word in ["loop", "ralph", "groen", "controle afgeven", "tot bare go echt werkt"]) else "go-auto"
            may_write_intent_task = bool(args.write or args.execute)
            if brief and may_write_intent_task:
                result["created_tasks"] = create_tasks_from_execution_brief(repo, brief, agent=args.agent)
                result["created_task"] = result["created_tasks"][0]
                if pending_record:
                    result["recommendation_promotion"] = archive_applied_recommendation(
                        repo,
                        pending_record,
                        [task["id"] for task in result["created_tasks"]],
                        args.authority_source,
                    )
            elif brief:
                result["proposed_tasks"] = [
                    {"id": unit["id"], "summary": unit["summary"], "write_required": True}
                    for unit in brief["work_units"]
                ]
                result["write_boundary"] = "dry_run: no .go state was changed; rerun with --write or --execute to materialize the execution brief"
            elif intent and may_write_intent_task:
                result["created_task"] = create_task_from_intent(repo, intent, agent=args.agent, source_ref=args.intent_source_ref)
                result["created_tasks"] = [result["created_task"]]
            elif intent:
                contract = intent_task_contract(intent)
                proposed_id = slugify(contract["summary"]).lower()[:48].strip("-") or "continue-project-goal"
                result["proposed_task"] = {
                    "id": proposed_id,
                    "summary": contract["summary"],
                    "requested_outcomes": contract["requested_outcomes"],
                    "decomposition_policy": contract["decomposition_policy"],
                    "acceptance": contract["acceptance"],
                    "write_required": True,
                    "write_flag": "--write",
                }
                result["write_boundary"] = "dry_run: no .go state was changed; rerun with --write or --execute to materialize the intent task"
            result["action"] = mode
            plan = build_loop_plan(repo, args, mode=mode)
            if result.get("proposed_task") and not plan.get("next_tasks"):
                plan["next_tasks"] = [result["proposed_task"]["id"]]
                plan["run_envelope"]["preflight"]["selected_task_count"] = 0
                plan["run_envelope"]["preflight"]["dry_run_proposed_task"] = result["proposed_task"]
            result["plan"] = plan
            if args.execute:
                # New public Go work uses the same serial delivery boundary. Explicit
                # saved managed runs remain resumptions, never new publication grants.
                saved_identity = getattr(args, 'task_id', '')
                saved_run = bool(saved_identity and (root / 'runs' / saved_identity / 'run-state.json').is_file())
                if not saved_run:
                    selected = ([args.task_id] if getattr(args, 'task_id', '') else
                                [task['id'] for task in result.get('created_tasks') or []])
                    if not selected:
                        explicit = getattr(args, 'explicit_campaign_budget', None) or {}
                        count = explicit.get('max_tasks') or 1
                        selected = plan.get('next_tasks', [])[:count]
                    if selected:
                        from .state_io import atomic_json
                        identity = 'taskwise-' + datetime.now().strftime('%Y%m%dT%H%M%S%f')
                        campaign = _public_campaign(repo, intent=intent or ('Go ' + ', '.join(selected)),
                            source_ref=args.intent_source_ref or 'user:public-go', campaign_id=identity,
                            task_ids=selected, budget=getattr(args, 'explicit_campaign_budget', None),
                            ship_policy=getattr(args, 'explicit_ship_policy', None))
                        path = root / 'campaigns' / (identity + '.json')
                        atomic_json(path, campaign)
                        args.campaign = str(path)
                        args.previous_campaign = ''
                        args.campaign_workspace_root = str(repo.parent / (repo.name + '-task-workspaces'))
                exit_code, executed = execute_loop_plan(repo, args, mode=mode)
                result["execution"] = executed
                if args.json:
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                else:
                    print(f"go: {executed['status']}")
                return exit_code
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"go action: {result.get('action')}")
        if result.get("created_task"):
            print(f"created_task: {result['created_task']['id']}")
        if result.get("plan"):
            print("next_tasks: " + (", ".join(result["plan"].get("next_tasks", [])) or "none"))
        elif result.get("next_command"):
            print(f"next: {result['next_command']}")
    return 0

def cmd_auto(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if args.emit_handoff:
        handoff = build_agent_handoff(repo, args, mode="go-auto")
        print(json.dumps(handoff, indent=2, ensure_ascii=False) if args.json else handoff["command"])
        return 0
    if args.execute:
        exit_code, result = execute_loop_plan(repo, args, mode="go-auto")
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"go-auto execute: {result['status']}")
            print("completed_tasks: " + (", ".join(result["completed_tasks"]) if result["completed_tasks"] else "none"))
            if result.get("blocked_task"):
                print(f"blocked_task: {result['blocked_task']}")
        return exit_code
    plan = build_loop_plan(repo, args, mode="go-auto")
    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        print(f"go-auto: {plan['project_id']}")
        print("control: handed off until blocker")
        print("next_tasks: " + (", ".join(plan["next_tasks"]) if plan["next_tasks"] else "none"))
        print("can_escalate_to: " + (", ".join(plan["can_escalate_to"]) if plan["can_escalate_to"] else "none"))
        print("loop: " + " → ".join(plan["loop"]))
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if args.emit_handoff:
        handoff = build_agent_handoff(repo, args, mode="go-loop")
        print(json.dumps(handoff, indent=2, ensure_ascii=False) if args.json else handoff["command"])
        return 0
    if args.execute:
        exit_code, result = execute_loop_plan(repo, args, mode="go-loop")
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"go-loop execute: {result['status']}")
            print("completed_tasks: " + (", ".join(result["completed_tasks"]) if result["completed_tasks"] else "none"))
            if result.get("blocked_task"):
                print(f"blocked_task: {result['blocked_task']}")
        return exit_code
    plan = build_loop_plan(repo, args, mode="go-loop")
    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        print(f"go-loop: {plan['project_id']}")
        print("control: handed off until blocker")
        print("next_tasks: " + (", ".join(plan["next_tasks"]) if plan["next_tasks"] else "none"))
        print("loop: " + " → ".join(plan["loop"]))
    return 0


def cmd_router(args: argparse.Namespace) -> int:
    raw_command = args.command or "go"
    normalized = normalize_router_command(raw_command)
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    state = {
        "repo_exists": repo.exists(),
        "is_git_repo": is_git_checkout(repo),
        "has_go": root.is_dir(),
        "has_project": (root / "project.json").is_file(),
        "has_vision": (root / "vision.json").is_file(),
        "has_principles": (root / "architecture-principles.json").is_file(),
        "has_hierarchy": (root / "hierarchy.json").is_file(),
        "open_task_count": 0,
        "active_task_count": 0,
        "blocked_task_count": 0,
        "done_task_count": 0,
        "valid": False,
        "errors": [],
    }
    if state["has_go"]:
        for status in ("open", "active", "blocked", "done"):
            state[f"{status}_task_count"] = len(list((root / "tasks" / status).glob("*.json")))
    if state["has_project"]:
        errors = validate_repo(repo)
        state["valid"] = not errors
        state["errors"] = errors
    intent = (args.intent or "").strip().lower()
    recommended: dict[str, Any] = recommend_route(normalized, intent, state)
    if recommended["command"] == "spike":
        repair = recommended.get("mode") != "create_repo"
        brief = args.intent or ("<repair intent>" if repair else "<intent>")
        parts: list[object] = ["python3", Path(__file__).resolve(), "spike", repo, "--brief", brief]
        if recommended.get("mode") == "repair_existing_repo":
            parts.append("--skip-repo-complete")
        recommended["example"] = shell_join(*parts)
    elif recommended["command"] in {"auto", "go-loop"}:
        recommended["example"] = shell_join("python3", Path(__file__).resolve(), recommended["command"], repo, "--max-tasks", args.max_tasks)
    elif recommended["command"] == "task create":
        recommended["example"] = shell_join("python3", Path(__file__).resolve(), "task", "create", repo, "--summary", "<next task>")
    result = {
        "schema": "go-workflow.router.v1",
        "public_command": recommended.get("public_command", "go"),
        "selected_route": recommended.get("selected_route", ""),
        "stop_before_implementation": bool(recommended.get("stop_before_implementation", False)),
        "normalized_command": normalized,
        "raw_command": raw_command,
        "intent": args.intent,
        "repo": str(repo),
        "state": state,
        "recommended": recommended,
        "router_policy": "Expose one public Go command; classify explicit plan/loop/task-id/Wayfinder intent, then choose internal repo-local primitives without requiring another user command.",
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"command: {raw_command} -> {normalized}")
        print(f"repo_exists: {state['repo_exists']}")
        print(f"has_go: {state['has_go']}")
        print(f"has_vision: {state['has_vision']}")
        print(f"has_principles: {state['has_principles']}")
        print(f"open_tasks: {state['open_task_count']}")
        print(f"recommended: {recommended['command']} — {recommended['reason']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    route = route_repo(repo)
    status: dict[str, Any] = {"repo": str(repo), "route": route}
    if route["mode"] == "repo-local" and route["valid"]:
        root = go_root(repo)
        project = load_json(root / "project.json")
        project_mode = str(project.get("project_mode") or "project")
        status["project"] = {"id": project.get("id"), "name": project.get("name"), "mode": project_mode}
        counts = {}
        for state in ("open", "active", "blocked", "done"):
            counts[state] = len(list((root / "tasks" / state).glob("*.json")))
        status["tasks"] = counts
        status['lifecycle'] = lifecycle_report(repo)
        if status['lifecycle']['invalid_done']:
            counts['recorded_done'] = counts['done']
            counts['done'] -= len(status['lifecycle']['invalid_done'])
            counts['completion_invalid'] = len(status['lifecycle']['invalid_done'])
        status["architecture"] = summarize_architecture(root)
        tasks = open_tasks(repo)
        status["setup_required"] = project_mode == "template"
        if status["setup_required"]:
            status["setup_command"] = './go spike . --brief "<project intent>"'
            status["next"] = None
        else:
            status["next"] = None if not tasks else {"id": tasks[0][1].get("id"), "summary": tasks[0][1].get("summary")}
        status["dirty"] = classify_dirty(repo, [".go/**"])
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    else:
        print(f"mode: {route['mode']}")
        print(f"valid: {route['valid']}")
        if status.get("project"):
            print(f"project: {status['project']['name']} ({status['project']['id']})")
            print("tasks: " + ", ".join(f"{k}={v}" for k, v in status["tasks"].items()))
            nxt = status.get("next")
            print("next: none" if not nxt else f"next: {nxt['id']} — {nxt['summary']}")
            blocking = status.get("dirty", {}).get("blocking", [])
            print(f"dirty_blocking: {len(blocking)}")
        if route.get("errors"):
            print("errors:")
            for error in route["errors"]:
                print(f"- {error}")
    return 0 if route["valid"] else 1


def cmd_index_build(args: argparse.Namespace) -> int:
    graph = build_graph(Path(args.repo))
    if args.json:
        print(json.dumps(graph, indent=2, ensure_ascii=False))
    else:
        print(f"graph: {GRAPH_RELATIVE_PATH}")
        print(f"files: {graph['coverage']['files']}")
        print(f"nodes: {graph['coverage']['nodes']}")
        print(f"edges: {graph['coverage']['edges']}")
    return 0


def cmd_index_status(args: argparse.Namespace) -> int:
    status = index_status(Path(args.repo))
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    else:
        print(f"present: {str(status['present']).lower()}")
        print(f"fresh: {str(status['fresh']).lower()}")
        print(f"reason: {status['reason']}")
    return 0 if status["fresh"] else 1


def cmd_index_query(args: argparse.Namespace) -> int:
    result = query_graph(Path(args.repo), args.query, limit=args.limit)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for node in result["nodes"]:
            print(f"{node['id']}\t{node.get('kind', '')}\t{node.get('path', '')}")
    return 0


def cmd_index_blast(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    task = None
    if args.task_id:
        _path, task = find_task(go_root(repo), args.task_id)
    result = repository_blast(
        repo,
        base=args.base,
        head=args.head,
        max_nodes=args.max_nodes,
        task=task,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"changed_paths: {len(result['changed_paths'])}")
        print(f"impacted_nodes: {len(result['impacted_nodes'])}")
        print(f"recommended_tests: {len(result['recommended_tests'])}")
        if result["conformance"]:
            print(f"conformance: {result['conformance']['status']}")
    conformance = result.get("conformance")
    return 1 if conformance and conformance["policy"] == "strict" and conformance["status"] == "findings" else 0


def template_check_environment(source: dict[str, str], stack_root: Path) -> dict[str, str]:
    env = source.copy()
    if is_git_checkout(stack_root):
        env["GO_STACK"] = str(stack_root)
        env["GO_STACK_ALLOW_DEV"] = "1"
    else:
        env.pop("GO_STACK", None)
        env.pop("GO_STACK_ALLOW_DEV", None)
    return env


def cmd_template_check(args: argparse.Namespace) -> int:
    """Validate that a project template still works with this stack checkout."""
    template = Path(args.template_repo).resolve()
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    record("template_exists", template.is_dir(), str(template))
    record("template_has_go", go_root(template).is_dir(), str(go_root(template)))
    errors = validate_repo(template) if go_root(template).is_dir() else [f"missing .go directory: {go_root(template)}"]
    record("validate", not errors, "; ".join(errors))
    if not errors:
        route = route_repo(template)
        tasks = open_tasks(template)
        record("route_repo_local", route.get("mode") == "repo-local" and route.get("valid") is True, json.dumps(route, sort_keys=True))
        record("has_claimable_example_task", bool(tasks), tasks[0][1].get("id", "") if tasks else "")
    makefile = template / "Makefile"
    check_script = template / "scripts" / "check.sh"
    record("makefile_present", makefile.is_file(), str(makefile))
    record("check_script_present", check_script.is_file(), str(check_script))
    if makefile.is_file():
        make_text = makefile.read_text(encoding="utf-8")
        record("makefile_uses_stack_cli", "cli/go.py validate" in make_text and "cli/go.py readback" in make_text, "Makefile should validate and read back via stack CLI")
    if check_script.is_file():
        script_text = check_script.read_text(encoding="utf-8")
        record("check_script_bootstraps_stack", "bootstrap-stack.sh" in script_text, "check.sh should make a fresh template clone self-checkable")
    if not errors and open_tasks(template):
        with tempfile.TemporaryDirectory(prefix="go-template-check-") as temp_dir:
            clone = Path(temp_dir) / "template"
            shutil.copytree(template, clone, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
            subprocess.run(["git", "init", "-q", str(clone)], text=True, capture_output=True, check=False)
            subprocess.run(["git", "add", "."], cwd=clone, text=True, capture_output=True, check=False)
            seeded = subprocess.run(
                ["git", "-c", "user.name=Template Check", "-c", "user.email=template-check@example.com", "commit", "-m", "seed template check", "-q"],
                cwd=clone,
                text=True,
                capture_output=True,
                check=False,
            )
            env = template_check_environment(os.environ, STACK_ROOT)
            executed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "auto", str(clone), "--max-tasks", "1", "--max-attempts", "1", "--execute", "--agent", "template-check", "--json"],
                cwd=clone,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            try:
                auto_result = json.loads(executed.stdout)
            except json.JSONDecodeError:
                auto_result = {}
            auto_ok = seeded.returncode == 0 and executed.returncode == 0 and auto_result.get("status") == "done" and bool(auto_result.get("completed_tasks"))
            detail = json.dumps({
                "seed_returncode": seeded.returncode,
                "auto_returncode": executed.returncode,
                "status": auto_result.get("status"),
                "completed_tasks": auto_result.get("completed_tasks", []),
                "stderr": (executed.stderr or "")[-500:],
            }, ensure_ascii=False)
            record("first_auto_execute", auto_ok, detail)
    else:
        record("first_auto_execute", False, "template must validate and contain an open task")

    ok = all(item["ok"] for item in checks)
    result = {
        "schema": "go-workflow.template-check.v1",
        "stack": str(STACK_ROOT),
        "template": str(template),
        "ok": ok,
        "checks": checks,
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"template: {template}")
        for item in checks:
            marker = "ok" if item["ok"] else "FAIL"
            suffix = f" — {item['detail']}" if item["detail"] else ""
            print(f"{marker}: {item['name']}{suffix}")
    return 0 if ok else 1


def ensure_intake_outcomes(task):
    """New adopted intake carries pending acceptance coverage, never old proof."""
    if 'execution_contract' not in task or not task.get('acceptance') or task.get('outcome_tracking_version'):
        return
    text = str(task.get('description') or task.get('summary') or task['id'])
    task['outcome_tracking_version'] = 1
    task.setdefault('intent_source', {'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                             'source_ref': 'repo-local-intake:' + task['id']})
    task['requested_outcomes'] = [{'id': 'R' + str(index), 'text': value, 'source': 'intake_acceptance',
                                    'status': 'pending', 'evidence': []}
                                  for index, value in enumerate(task['acceptance'], 1)]


def apply_intake_contract(repo: Path, task: dict[str, Any], source: dict[str, Any] | None = None) -> None:
    source = source or {}
    if "execution_contract" in source:
        errors = validate_execution_contract(source["execution_contract"], partial=True)
        if errors:
            raise RepoLocalError("invalid execution override: " + "; ".join(errors))
    try:
        profile = resolve_execution_contract(load_json(go_root(repo) / "project.json"), source.get("execution_contract"))
    except (ValueError, TypeError) as exc:
        raise RepoLocalError(str(exc)) from exc
    if profile is not None:
        errors = validate_execution_contract(profile)
        if errors:
            raise RepoLocalError("invalid execution contract: " + "; ".join(errors))
        if profile.get("phase_profile") and profile["phase_profile"] not in load_json(go_root(repo) / "project.json").get("phase_profiles", {}):
            raise RepoLocalError("unknown phase_profile: " + profile["phase_profile"])
        task["execution_contract"] = profile
    if "dependencies" in source:
        if profile is None:
            raise RepoLocalError("explicit dependencies require an execution_contract or execution_defaults")
        errors = validate_dependencies(source["dependencies"])
        if errors:
            raise RepoLocalError("invalid dependencies: " + "; ".join(errors))
        task["dependencies"] = source["dependencies"]
    if "repository_context" in source:
        errors = validate_repository_context(source["repository_context"])
        if errors:
            raise RepoLocalError("invalid repository context: " + "; ".join(errors))
        task["repository_context"] = source["repository_context"]
        errors = validate_task_repository_context(repo, task, str(task.get("id") or "task"))
        if errors:
            raise RepoLocalError("invalid repository context: " + "; ".join(errors))

    ensure_intake_outcomes(task)


def cmd_task_create(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    project = load_json(root / "project.json")
    task_id = slugify(args.id or args.summary)
    if not TASK_ID_RE.fullmatch(task_id):
        raise RepoLocalError(f"invalid task id: {task_id}")
    for status in ("open", "active", "blocked", "done"):
        if task_path(root, status, task_id).exists():
            raise RepoLocalError(f"task already exists: {task_id}")
    acceptance = args.acceptance or ["Task result is implemented and verified."]
    verification = args.verification or project.get("default_verification") or ["git diff --check"]
    task = {
        "schema": TASK_SCHEMA,
        "kind": "task",
        "execution_mode": args.execution_mode,
        "shareable_delivery": args.shareable_delivery,
        "id": task_id,
        "project": project["id"],
        "status": "open",
        "summary": args.summary,
        "description": args.description or args.summary,
        "scope": {"read": args.read or [".go/**"], "modify": args.modify or [".go/**"]},
        "acceptance": acceptance,
        "verification": verification,
        "claim": {"agent": None, "claimed_at": None},
        "work_status": "pending",
        "review_status": "none",
        "review_history": [],
        "evidence": [],
    }
    source = {}
    if getattr(args, "execution_contract", ""):
        source["execution_contract"] = load_json(Path(args.execution_contract))
    if getattr(args, "dependencies", ""):
        try:
            source["dependencies"] = json.loads(Path(args.dependencies).read_text())
        except (OSError, ValueError) as exc:
            raise RepoLocalError(f"invalid dependency file: {exc}") from exc
    if getattr(args, "repository_context", ""):
        source["repository_context"] = load_json(Path(args.repository_context))
    apply_intake_contract(repo, task, source)
    dependency_errors = dependency_findings(repo, task, candidates=[task])
    if dependency_errors:
        raise RepoLocalError("invalid dependencies: " + "; ".join(dependency_errors))
    if args.feature and args.epic:
        raise RepoLocalError("use either --feature or --epic, not both")
    target = task_path(root, "open", task_id)
    dump_json(target, task)
    try:
        if args.epic:
            append_task_to_epic(root, args.epic, task_id)
        else:
            append_task_to_hierarchy(root, args.feature, task_id)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    errors = validate_repo(repo)
    if errors:
        target.unlink(missing_ok=True)
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(relative(repo, target))
    return 0


def cmd_task_outcome(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    try:
        with repository_lock(root, f"task-{args.task_id}"):
            path, task = find_task(root, args.task_id)
            # Recheck ownership under the task lock, after the CLI routing guard.
            record = registered_workspace(repo) or registered_workspace(Path.cwd())
            if record is not None and (task.get('id') != record['task_id'] or task.get('status') != 'active'
                    or (task.get('claim') or {}).get('agent') != args.agent or args.agent != record['owner']):
                raise WorkspaceError('Owned outcome requires the matching current active claim')
            if task.get("status") == "done":
                raise RepoLocalError("cannot change outcome disposition on a done task")
            if task.get("outcome_tracking_version") != 1:
                raise RepoLocalError("task does not use requested-outcome tracking")
            outcomes = task.get("requested_outcomes") or []
            outcome = next((item for item in outcomes if isinstance(item, dict) and item.get("id") == args.outcome), None)
            if outcome is None:
                raise RepoLocalError(f"unknown requested outcome: {args.outcome}")
            evidence = args.evidence.strip()
            if not evidence:
                raise RepoLocalError("outcome disposition requires evidence")
            outcome["status"] = args.status
            outcome.setdefault("evidence", []).append({
                "created_at": now_iso(),
                "agent": args.agent,
                "summary": evidence,
            })
            dump_json(path, task)
            append_jsonl(root / "evidence" / "events.jsonl", event(task["id"], "evidence.appended", args.agent, {
                "action": "outcome.disposition_recorded",
                "outcome_id": args.outcome,
                "status": args.status,
                "evidence": evidence,
            }))
    except StateLockError as exc:
        raise RepoLocalError(str(exc)) from exc
    print(f"{args.task_id}:{args.outcome}={args.status}")
    return 0


def _handoff_json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _handoff_event_exists(root: Path, task_id: str, handoff_id: str) -> bool:
    path = root / "runs" / "events.jsonl"
    if not path.is_file():
        return False
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RepoLocalError(f"cannot hand off through malformed runs/events.jsonl line {line_number}") from exc
        data = item.get("data") if isinstance(item, dict) else None
        if (isinstance(item, dict) and item.get("event") == "task.claimed"
                and item.get("task_id") == task_id and isinstance(data, dict)
                and data.get("handoff_id") == handoff_id):
            return True
    return False


def _handoff_workspace_after(record: dict[str, Any], journal: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(record)
    history = list(updated.get("ownership_history") or [])
    history.append({"owner": journal["expected_owner"], "run_id": journal["old_run_id"]})
    updated["ownership_history"] = history
    updated.update(owner=journal["new_owner"], run_id=journal["new_run_id"])
    return validate_record(updated)


def _block_handoff_after_apply_failure(
    root: Path, journal_path: Path, journal: dict[str, Any], active_path: Path, registry: Path, reason: str
) -> str:
    """Block a failed apply and restore only snapshots that still exactly match its after-state."""
    rollback: dict[str, str] = {}
    snapshots = (
        ("task", active_path, journal.get("task_before"), journal.get("task_before_sha256"), journal.get("task_after_sha256")),
        ("workspace", registry, journal.get("workspace_before"), journal.get("workspace_before_sha256"), journal.get("workspace_after_sha256")),
    )
    for name, path, before, before_hash, after_hash in snapshots:
        if name == "workspace" and journal.get("legacy_unmanaged"):
            continue
        if not isinstance(before, dict) or _handoff_json_hash(before) != before_hash:
            rollback[name] = "not_restored_missing_or_invalid_before_snapshot"
            continue
        try:
            current = load_json(path) if name == "task" else validate_record(read_object(path))
        except (OSError, RepoLocalError, WorkspaceError):
            rollback[name] = "not_restored_unreadable"
            continue
        if _handoff_json_hash(current) != after_hash:
            rollback[name] = "not_restored_after_state_drifted"
            continue
        try:
            dump_json(path, deepcopy(before))
            restored = load_json(path) if name == "task" else validate_record(read_object(path))
        except (OSError, RepoLocalError, WorkspaceError):
            rollback[name] = "restore_failed"
            continue
        rollback[name] = "restored" if _handoff_json_hash(restored) == before_hash else "restore_readback_mismatch"

    journal["status"] = "blocked"
    journal["blocked_at"] = now_iso()
    journal["blocked_reason"] = reason
    journal["rollback"] = rollback
    dump_json(journal_path, journal)
    return "; handoff journal is blocked; inspect its rollback record before using a new handoff id"


def _apply_task_handoff_journal(repo: Path, root: Path, journal_path: Path, journal: dict[str, Any]) -> dict[str, Any]:
    task_id = journal["task_id"]
    active_path = root / "tasks" / "active" / (task_id + ".json")
    registry = registry_path(repo, task_id)
    task = load_json(active_path)
    task_hash = _handoff_json_hash(task)
    before_task_hash = journal["task_before_sha256"]
    after_task_hash = journal["task_after_sha256"]
    if task.get("status") != "active" or task_hash not in {before_task_hash, after_task_hash}:
        raise RepoLocalError("task state drifted during prepared handoff; preserve and inspect")
    expected_task_owner = journal["expected_owner"] if task_hash == before_task_hash else journal["new_owner"]
    if (task.get("claim") or {}).get("agent") != expected_task_owner:
        raise RepoLocalError("task claim owner drifted during prepared handoff")

    record = None
    record_hash = None
    content_fingerprint = None
    if journal["legacy_unmanaged"]:
        if registry.exists():
            raise RepoLocalError("legacy handoff found a workspace registry; preserve and inspect")
        workspace_contract = ((task.get("execution_contract") or {}).get("workspace") or {})
        if workspace_contract.get("mode") == "task_worktree":
            raise RepoLocalError("task requires a registered workspace; legacy handoff is refused")
        if not journal.get("confirm_same_host"):
            raise RepoLocalError("legacy handoff lacks same-host confirmation")
        for channel in ("managed", "completion", "publication"):
            if state_path(repo, task_id, channel).exists():
                raise RepoLocalError("legacy handoff found managed run state; preserve and inspect")
    else:
        if not registry.is_file():
            raise RepoLocalError("registered workspace disappeared during prepared handoff")
        record = validate_record(read_object(registry))
        record_hash = _handoff_json_hash(record)
        before_record_hash = journal["workspace_before_sha256"]
        after_record_hash = journal["workspace_after_sha256"]
        if record_hash not in {before_record_hash, after_record_hash}:
            raise RepoLocalError("workspace registry drifted during prepared handoff; preserve and inspect")
        expected_record_owner = journal["expected_owner"] if record_hash == before_record_hash else journal["new_owner"]
        expected_record_run = journal["old_run_id"] if record_hash == before_record_hash else journal["new_run_id"]
        if record.get("owner") != expected_record_owner or record.get("run_id") != expected_record_run:
            raise RepoLocalError("workspace owner/run identity drifted during prepared handoff")
        if record.get("state") != "ready":
            raise WorkspaceError("only a ready workspace can be handed off")
        if state_path(repo, task_id, "managed").exists():
            raise WorkspaceError("managed run checkpoint exists; resume it before handoff")
        if state_path(repo, task_id, "publication").exists():
            raise WorkspaceError("publication checkpoint exists; finish/read it back before handoff")
        verify_workspace(record)
        require_visible_index(record)
        current_host_evidence = _handoff_host_evidence(
            repo, task_id, record, journal["expected_owner"], journal["old_run_id"], bool(journal.get("confirm_same_host"))
        )
        if current_host_evidence != journal.get("host_evidence"):
            raise WorkspaceError("host evidence changed during prepared handoff; preserve and inspect")
        require_run_idle(record)
        content_fingerprint = workspace_content_fingerprint(record)
        if content_fingerprint != journal.get("workspace_content_sha256"):
            raise RepoLocalError("workspace content changed during prepared handoff; preserve and inspect")

    # Construct and validate all intended writes only after the complete preflight above.
    updated_task = None
    if task_hash == before_task_hash:
        updated_task = deepcopy(journal.get("task_after") or {})
        if _handoff_json_hash(updated_task) != after_task_hash:
            raise RepoLocalError("handoff journal task snapshot is invalid")

    updated_record = None
    if not journal["legacy_unmanaged"] and record_hash == journal["workspace_before_sha256"]:
        updated_record = deepcopy(journal.get("workspace_after") or {})
        if _handoff_json_hash(updated_record) != journal["workspace_after_sha256"]:
            raise RepoLocalError("handoff journal workspace snapshot is invalid")

    events_path = root / "runs" / "events.jsonl"
    event_already_recorded = _handoff_event_exists(root, task_id, journal["handoff_id"])

    if updated_task is not None:
        dump_json(active_path, updated_task)
    if updated_record is not None:
        dump_json(registry, updated_record)

    try:
        final_task = load_json(active_path)
        if _handoff_json_hash(final_task) != after_task_hash:
            raise RepoLocalError("task claim handoff readback mismatch")
        if not journal["legacy_unmanaged"]:
            final_record = validate_record(read_object(registry))
            verify_workspace(final_record)
            if _handoff_json_hash(final_record) != journal["workspace_after_sha256"]:
                raise RepoLocalError("workspace handoff readback mismatch")
            if workspace_content_fingerprint(final_record) != journal.get("workspace_content_sha256"):
                raise RepoLocalError("workspace content changed while handoff was applied; preserve and inspect")
        elif registry.exists():
            raise RepoLocalError("legacy handoff created a workspace registry unexpectedly")
    except (RepoLocalError, WorkspaceError) as exc:
        guidance = _block_handoff_after_apply_failure(root, journal_path, journal, active_path, registry, str(exc))
        raise RepoLocalError(str(exc) + guidance) from exc

    if not event_already_recorded:
        append_jsonl(events_path, event(
            task_id, "task.claimed", journal["new_owner"], journal["event_data"]
        ))
    journal["status"] = "completed"
    journal["completed_at"] = now_iso()
    journal["event_recorded"] = True
    dump_json(journal_path, journal)
    return {
        "schema": "go-workflow.task-handoff-result.v1",
        "status": "transferred",
        "task_id": task_id,
        "handoff_id": journal["handoff_id"],
        "old_owner": journal["expected_owner"],
        "new_owner": journal["new_owner"],
        "old_run_id": journal["old_run_id"],
        "new_run_id": journal["new_run_id"],
        "legacy_unmanaged": journal["legacy_unmanaged"],
        "host_evidence": journal.get("host_evidence"),
        "workspace_fingerprint_sha256": journal.get("workspace_content_sha256"),
    }


def _handoff_host_evidence(repo: Path, task_id: str, record: dict[str, Any], expected_owner: str, old_run_id: str, confirm_same_host: bool) -> str:
    current_host = socket.gethostname()
    evidence: list[str] = []
    record_host = record.get("control_host")
    if record_host:
        if record_host != current_host:
            raise WorkspaceError("handoff workspace control_host mismatch; cross-host handoff is unsupported")
        evidence.append("workspace-control-host")
    completion_path = state_path(repo, task_id, "completion")
    if completion_path.is_file():
        completion = read_state(repo, task_id, "completion")
        controller = completion.get("controller") or {}
        checkpoint_host = controller.get("host")
        if checkpoint_host:
            if checkpoint_host != current_host:
                raise WorkspaceError("handoff completion checkpoint host mismatch; cross-host handoff is unsupported")
            if completion.get("owner") and completion.get("owner") != expected_owner:
                raise WorkspaceError("handoff completion checkpoint owner mismatch")
            if completion.get("run_id") and completion.get("run_id") != old_run_id:
                raise WorkspaceError("handoff completion checkpoint run-id mismatch")
            evidence.append("completion-checkpoint-host")
    if evidence:
        return "+".join(evidence)
    if not confirm_same_host:
        raise WorkspaceError("host identity is unavailable; explicit --confirm-same-host is required")
    return "operator-confirmation"


def perform_task_handoff(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(repo).resolve()
    root = go_root(repo)
    task_id = args.task_id
    old_owner = str(args.expected_owner or "").strip()
    new_owner = str(args.new_owner or "").strip()
    reason = str(args.reason or "").strip()
    old_run_id = str(args.old_run_id or "").strip()
    new_run_id = str(args.new_run_id or "").strip()
    legacy_unmanaged = bool(args.legacy_unmanaged)
    confirm_same_host = bool(args.confirm_same_host)
    handoff_id = str(args.handoff_id or uuid.uuid4().hex).strip()
    owner_pattern = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    if not re.fullmatch(owner_pattern, old_owner) or not re.fullmatch(owner_pattern, new_owner):
        raise RepoLocalError("handoff requires valid expected/new owner ids")
    if old_owner == new_owner:
        raise RepoLocalError("handoff old and new owners must differ")
    if not reason or len(reason) > 500:
        raise RepoLocalError("handoff requires a 1-500 character reason")
    if not bool(args.confirm_owner_stopped):
        raise RepoLocalError("handoff requires --confirm-owner-stopped")
    if not re.fullmatch(r"[0-9a-f]{32}", handoff_id):
        raise RepoLocalError("handoff id must be 32 lowercase hexadecimal characters")

    journal_path = root / "runs" / task_id / "handoffs" / (handoff_id + ".json")
    journal_created = journal_path.exists()
    try:
        with repository_lock(root, "workspace-execution-" + task_id):
            with repository_lock(root, "task-" + task_id):
                if journal_path.exists():
                    journal = load_json(journal_path)
                    identity = (journal.get("schema") == "go-workflow.task-handoff.v1"
                        and journal.get("handoff_id") == handoff_id
                        and journal.get("task_id") == task_id
                        and journal.get("expected_owner") == old_owner
                        and journal.get("new_owner") == new_owner
                        and journal.get("old_run_id") == (old_run_id or None)
                        and journal.get("new_run_id") == (new_run_id or None)
                        and journal.get("legacy_unmanaged") is legacy_unmanaged
                        and journal.get("confirm_same_host") is confirm_same_host
                        and journal.get("reason") == reason)
                    if not identity:
                        raise RepoLocalError("handoff id belongs to a different transfer")
                    if journal.get("status") == "completed":
                        return {
                            "schema": "go-workflow.task-handoff-result.v1",
                            "status": "already_transferred",
                            "task_id": task_id,
                            "handoff_id": handoff_id,
                            "old_owner": old_owner,
                            "new_owner": new_owner,
                            "old_run_id": old_run_id or None,
                            "new_run_id": new_run_id or None,
                            "legacy_unmanaged": legacy_unmanaged,
                            "confirm_same_host": confirm_same_host,
                            "host_evidence": journal.get("host_evidence"),
                            "workspace_fingerprint_sha256": journal.get("workspace_content_sha256"),
                        }
                    if journal.get("status") == "blocked":
                        raise RepoLocalError(
                            "handoff journal is blocked; inspect its rollback record and create a new handoff after resolving drift"
                        )
                    if journal.get("status") != "prepared":
                        raise RepoLocalError("handoff journal has an unsupported status")
                    return _apply_task_handoff_journal(repo, root, journal_path, journal)

                task_path_value, task = find_task(root, task_id)
                if task_path_value.parent.name != "active" or task.get("status") != "active":
                    raise RepoLocalError("handoff requires an active task")
                claim = task.get("claim") or {}
                if claim.get("agent") != old_owner:
                    raise RepoLocalError("handoff expected owner does not match the current task claim")

                registry = registry_path(repo, task_id)
                if registry.exists() and not registry.is_file():
                    raise WorkspaceError("workspace registry path is not a regular file; preserve and inspect")
                workspace_mode = ((task.get("execution_contract") or {}).get("workspace") or {}).get("mode")
                record = None
                content_fingerprint = None
                host_evidence = ""
                base_commit = claim.get("base_commit")
                if registry.is_file():
                    if legacy_unmanaged:
                        raise RepoLocalError("--legacy-unmanaged cannot be used when a workspace is registered")
                    if not re.fullmatch(owner_pattern, old_run_id) or not re.fullmatch(owner_pattern, new_run_id):
                        raise RepoLocalError("registered workspace handoff requires valid old/new run ids")
                    if old_run_id == new_run_id:
                        raise RepoLocalError("registered workspace handoff requires a new run id")
                    record = validate_record(read_object(registry))
                    if record.get("control_repo") != str(repo) or record.get("owner") != old_owner or record.get("run_id") != old_run_id:
                        raise WorkspaceError("handoff workspace owner/run/control identity mismatch")
                    if record.get("state") != "ready":
                        raise WorkspaceError("only a ready workspace can be handed off")
                    if state_path(repo, task_id, "managed").exists():
                        raise WorkspaceError("managed run checkpoint exists; recover it through the managed-run lane before handoff")
                    if state_path(repo, task_id, "publication").exists():
                        raise WorkspaceError("publication checkpoint exists; finish/read it back before handoff")
                    verify_workspace(record)
                    require_visible_index(record)
                    host_evidence = _handoff_host_evidence(repo, task_id, record, old_owner, old_run_id, confirm_same_host)
                    require_run_idle(record)
                    content_fingerprint = workspace_content_fingerprint(record)
                    base_commit = record.get("base_commit") or base_commit
                else:
                    if not legacy_unmanaged:
                        raise RepoLocalError("unregistered claim requires explicit --legacy-unmanaged")
                    if workspace_mode == "task_worktree":
                        raise WorkspaceError("task requires a registered workspace; legacy handoff is refused")
                    if old_run_id or new_run_id:
                        raise RepoLocalError("legacy unmanaged handoff must not invent run ids")
                    for channel in ("managed", "completion", "publication"):
                        if state_path(repo, task_id, channel).exists():
                            raise WorkspaceError("legacy handoff found managed run state; preserve and inspect")
                    if not confirm_same_host:
                        raise RepoLocalError("legacy unmanaged handoff requires --confirm-same-host")
                    host_evidence = "operator-confirmation"

                claim_after = deepcopy(claim)
                claim_after.update(agent=new_owner, claimed_at=now_iso())
                task_after = deepcopy(task)
                task_after["claim"] = claim_after
                record_after = _handoff_workspace_after(record, {
                    "expected_owner": old_owner,
                    "old_run_id": old_run_id,
                    "new_owner": new_owner,
                    "new_run_id": new_run_id,
                }) if record else None
                event_data = {
                    "action": "owner_handoff",
                    "handoff_id": handoff_id,
                    "previous_owner": old_owner,
                    "previous_run_id": old_run_id or None,
                    "new_owner": new_owner,
                    "new_run_id": new_run_id or None,
                    "reason": reason,
                    "legacy_unmanaged": legacy_unmanaged,
                    "confirm_same_host": confirm_same_host,
                    "host_evidence": host_evidence,
                }
                if base_commit:
                    event_data["base_commit"] = base_commit
                if content_fingerprint:
                    event_data["workspace_content_sha256"] = content_fingerprint
                journal = {
                    "schema": "go-workflow.task-handoff.v1",
                    "handoff_id": handoff_id,
                    "status": "prepared",
                    "created_at": now_iso(),
                    "task_id": task_id,
                    "expected_owner": old_owner,
                    "new_owner": new_owner,
                    "old_run_id": old_run_id or None,
                    "new_run_id": new_run_id or None,
                    "reason": reason,
                    "legacy_unmanaged": legacy_unmanaged,
                    "confirm_same_host": confirm_same_host,
                    "host_evidence": host_evidence,
                    "claim_after": claim_after,
                    "task_before": task,
                    "task_after": task_after,
                    "task_before_sha256": _handoff_json_hash(task),
                    "task_after_sha256": _handoff_json_hash(task_after),
                    "workspace_before": record,
                    "workspace_after": record_after,
                    "workspace_before_sha256": _handoff_json_hash(record) if record else None,
                    "workspace_after_sha256": _handoff_json_hash(record_after) if record_after else None,
                    "workspace_content_sha256": content_fingerprint,
                    "event_data": event_data,
                }
                dump_json(journal_path, journal)
                journal_created = True
                return _apply_task_handoff_journal(repo, root, journal_path, journal)
    except StateLockError as exc:
        raise RepoLocalError(str(exc)) from exc
    except (WorkspaceError, RunStateError) as exc:
        raise RepoLocalError(str(exc)) from exc
    except Exception as exc:
        if journal_path.is_file():
            try:
                pending = load_json(journal_path).get("status") == "prepared"
            except RepoLocalError:
                pending = False
            if pending:
                raise RepoLocalError(f"handoff {handoff_id} is pending; preserve the workspace and retry with --handoff-id {handoff_id}: {exc}") from exc
        raise


def cmd_task_handoff(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    result = perform_task_handoff(repo, args)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"{args.task_id}: {result['status']} {result['old_owner']} -> {result['new_owner']} ({result['handoff_id']})")
    return 0


def cmd_task_review(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    if not args.evidence.strip():
        raise RepoLocalError("review transition requires evidence")
    delivery: dict[str, Any] | None = None
    try:
        path, task_snapshot = find_task(root, args.task_id)
        delivery_plan = automatic_delivery_plan(repo, task_snapshot) if args.status == "approved" else None
        delivery_context = (
            repository_lock(root, f"delivery-{delivery_plan['epic']}")
            if delivery_plan else contextlib.nullcontext()
        )
        with delivery_context:
            with repository_lock(root, f"task-{args.task_id}"):
                path, task = find_task(root, args.task_id)
                record: dict[str, Any] = {
                    "created_at": now_iso(),
                    "agent": args.agent,
                    "status": args.status,
                    "evidence": args.evidence,
                }
                if args.owner:
                    record["owner"] = args.owner
                if args.status == "approved":
                    delivery = approve_completed_task(
                        repo,
                        root,
                        path,
                        task,
                        record,
                        delivery_lock_held=bool(delivery_plan),
                    )
                    target = path
                else:
                    task["review_status"] = "needs_fix"
                    task["work_status"] = "in_progress"
                    task["status"] = "active"
                    task.setdefault("claim", {})["agent"] = task.get("claim", {}).get("agent") or args.owner or args.agent
                    task.setdefault("claim", {})["claimed_at"] = task.get("claim", {}).get("claimed_at") or now_iso()
                    task.setdefault("review_history", []).append(record)
                    target = task_path(root, "active", task["id"])
                    if target != path:
                        atomic_move_json(path, target, task)
                    else:
                        dump_json(path, task)
                    append_jsonl(root / "evidence" / "events.jsonl", event(task["id"], "task.reviewed", args.agent, record))
    except StateLockError as exc:
        raise RepoLocalError(str(exc)) from exc
    suffix = f" delivery={delivery['html']}" if delivery else ""
    print(f"{relative(repo, target)}{suffix}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    lifecycle = scaffold_lifecycle_settings(repo, args)
    repo.mkdir(parents=True, exist_ok=True)
    copy_fixture_init(repo, force=args.force)
    apply_agents_gateway(repo)
    if lifecycle is not None:
        root = go_root(repo)
        dump_json(root / 'project.json', scaffold_project(load_json(root / 'project.json'), lifecycle))
        for path in (root / 'tasks/open').glob('*.json'):
            task = load_json(path); apply_intake_contract(repo, task); dump_json(path, task)
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"initialized: {repo / '.go'}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    from go_workflow.migrations import adopt_lifecycle, resume_lifecycle, rollback_lifecycle, MigrationError
    lifecycle = getattr(args, 'lifecycle', False)
    config = getattr(args, 'config', '')
    resume = getattr(args, 'resume', '')
    rollback = getattr(args, 'rollback', '')
    if lifecycle or config or resume or rollback:
        try:
            if sum(bool(value) for value in (lifecycle or config, resume, rollback)) != 1:
                raise MigrationError('Choose lifecycle adoption, resume, or rollback')
            if resume: result = resume_lifecycle(repo, resume, apply=args.apply)
            elif rollback: result = rollback_lifecycle(repo, rollback, apply=args.apply)
            else: result = adopt_lifecycle(repo, load_json(Path(config)) if config else None, apply=args.apply)
        except (ValueError, OSError) as exc: raise RepoLocalError(str(exc)) from exc
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    root = go_root(repo)
    try:
        plan, documents = plan_contract_migration(
            load_json(root / "project.json"),
            load_json(root / "hierarchy.json"),
            sorted(path.stem for path in (root / "tasks").glob("*/*.json")),
        )
    except ValueError as exc:
        raise RepoLocalError(str(exc)) from exc
    try:
        gateway_plan = plan_agents_gateway(repo)
    except AgentsGatewayError as exc:
        raise RepoLocalError(str(exc)) from exc
    if gateway_plan["action"] != "none":
        plan["changes"].append({
            "path": "AGENTS.md",
            "operations": [f"{gateway_plan['action']} bounded repository-local Go gateway"],
        })
        plan["write_required"] = True
    if args.apply and plan["changes"]:
        project_path = root / "project.json"
        hierarchy_path = root / "hierarchy.json"
        before_project = project_path.read_text(encoding="utf-8")
        before_hierarchy = hierarchy_path.read_text(encoding="utf-8")
        gateway_applied = False
        try:
            dump_json(project_path, documents["project.json"])
            dump_json(hierarchy_path, documents["hierarchy.json"])
            apply_agents_gateway(repo, gateway_plan)
            gateway_applied = True
            errors = validate_repo(repo)
            if errors:
                raise RepoLocalError("migration produced an invalid contract:\n- " + "\n- ".join(errors))
        except BaseException:
            atomic_write_text(project_path, before_project)
            atomic_write_text(hierarchy_path, before_hierarchy)
            if gateway_applied:
                restore_agents_gateway(repo, gateway_plan)
            raise
        plan["applied"] = True
        append_jsonl(
            root / "runs" / "events.jsonl",
            event("contract-migration", "run.checked", args.agent, {
                "action": "contract.migrated",
                "from_version": plan["from_version"],
                "to_version": plan["to_version"],
                "changes": plan["changes"],
            }),
        )
    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        mode = "applied" if plan["applied"] else "dry-run"
        print(f"migration {mode}: v{plan['from_version']} -> v{plan['to_version']}")
        for change in plan["changes"]:
            print(f"- {change['path']}: " + "; ".join(change["operations"]))
        if not plan["changes"]:
            print("- no changes")
    return 0


def cmd_agents_sync(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    try:
        plan = plan_agents_gateway(repo)
        result = apply_agents_gateway(repo, plan) if args.apply else {
            key: value for key, value in plan.items() if key not in {"before", "after"}
        }
    except AgentsGatewayError as exc:
        raise RepoLocalError(str(exc)) from exc
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"AGENTS.md gateway {result['mode']}: {result['action']}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    campaign_path = getattr(args, 'campaign', None)
    previous_path = getattr(args, 'previous_campaign', None)
    if previous_path and not campaign_path:
        errors.append('--previous-campaign requires --campaign')
    if campaign_path:
        from go_workflow.campaign_contracts import campaign_findings
        try:
            contract = load_json(Path(campaign_path))
            previous = load_json(Path(previous_path)) if previous_path else None
            errors.extend(campaign_findings(repo, contract, previous=previous))
        except RepoLocalError as exc:
            errors.append(str(exc))
    if getattr(args, 'json', False):
        print(json.dumps({'valid': not errors, 'errors': errors}, indent=2))
        return int(bool(errors))
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"ok: {repo / '.go'}")
    return 0


def cmd_campaign_block(args):
    from .delivery_blocks import prepare_block
    value=prepare_block(Path(args.repo).resolve(),args.coordinator,args.member,args.reason,args.agent)
    print(json.dumps(value,indent=2,ensure_ascii=False))
    return 0


def cmd_campaign_change(args):
    from .campaign_changes import admit_repair, amend_future_task, configure_progress
    repo=Path(args.repo).resolve()
    request=load_json(Path(args.request))
    operation={'repair':admit_repair,'amend':amend_future_task,'progress':configure_progress}[args.change_kind]
    value=operation(repo,args.campaign,owner=args.agent,**request)
    print(json.dumps(value,indent=2,ensure_ascii=False))
    return 0


def cmd_resume_context(args):
    from .resume_context import compose_resume_context
    result=compose_resume_context(Path(args.repo),args.task_id,actor=args.agent)
    print(json.dumps(result,indent=2,ensure_ascii=False) if args.json else result['summary'])
    return 0


def cmd_campaign_audit(args: argparse.Namespace) -> int:
    """Run the same outcome audit used by campaign completion."""
    from .campaign_audit import audit_campaign_goal

    repo = Path(args.repo).resolve()
    previous = Path(args.previous_campaign).resolve() if args.previous_campaign else None
    try:
        audit = audit_campaign_goal(
            repo,
            Path(args.contract).resolve(),
            previous_path=previous,
            persist=not args.read_only,
        )
    except (ValueError, OSError) as exc:
        raise RepoLocalError(str(exc)) from exc
    if args.json:
        print(json.dumps(audit, indent=2, ensure_ascii=False))
    else:
        print(f"campaign {audit['campaign_id']}: {audit['status']}")
        print(f"handoff: {audit['handoff']['path']}")
        for outcome in audit["outcomes"]:
            print(f"- {outcome['id']}: {outcome['status']}")
    return 0 if audit["goal_verified"] else 1


def cmd_next(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    tasks = open_tasks(repo)
    if not tasks:
        waiting = [(t["id"], dependency_findings(repo, t, readiness=True))
                   for _, t in open_task_records(go_root(repo))]
        if waiting:
            print("no eligible tasks; dependency blockers: " + "; ".join(
                f"{tid}: {', '.join(reasons)}" for tid, reasons in waiting))
        else:
            print("no open tasks")
        return 0
    _, data = tasks[0]
    print(f"{data['id']} — {data['summary']}")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    try:
        with repository_lock(root, f"task-{args.task_id}"):
            from go_workflow.migrations import pending_lifecycle_findings
            pending = pending_lifecycle_findings(repo)
            if pending: raise RepoLocalError('; '.join(pending))
            path, data = find_task(root, args.task_id)
            if data.get("status") != "open":
                raise RepoLocalError(f"task is not open: {data.get('status')}")
            if data.get("claim", {}).get("agent"):
                raise RepoLocalError(f"task already claimed by {data['claim']['agent']}")
            contract_errors = intake_claim_findings(data)
            if contract_errors:
                raise RepoLocalError("task contract blocked claim: " + "; ".join(contract_errors))
            dependency_errors = dependency_findings(repo, data, readiness=True)
            if dependency_errors:
                raise RepoLocalError("dependency preflight blocked claim: " + "; ".join(dependency_errors))
            architecture_findings = architecture_claim_findings(root, data)
            if architecture_findings:
                raise RepoLocalError("architecture preflight blocked claim:\n- " + "\n- ".join(architecture_findings))
            dirty = classify_dirty(repo, data.get("scope", {}).get("modify", []))
            if dirty["blocking"] and not args.allow_dirty:
                raise RepoLocalError("blocking dirty state before claim:\n- " + "\n- ".join(dirty["blocking"]))
            data["status"] = "active"
            data["work_status"] = "in_progress"
            data.setdefault("review_status", "none")
            data.setdefault("review_history", [])
            base_commit = claim_base_commit(repo)
            claimed_at = now_iso()
            data["claim"] = {
                "agent": args.agent,
                "claimed_at": claimed_at,
                "base_commit": base_commit,
            }
            target = task_path(root, "active", data["id"])
            atomic_move_json(path, target, data)
            append_jsonl(root / "runs" / "events.jsonl", event(
                data["id"],
                "task.claimed",
                args.agent,
                {"report_only_dirty": dirty["report_only"], "base_commit": base_commit, "claimed_at": claimed_at},
            ))
    except StateLockError as exc:
        raise RepoLocalError(str(exc)) from exc
    print(relative(repo, target))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    try:
        with repository_lock(root, f"task-{args.task_id}"):
            path, data = find_task(root, args.task_id)
            if data.get("status") != "active":
                raise RepoLocalError(f"task is not active: {data.get('status')}")
            if args.agent and data.get("claim", {}).get("agent") not in {args.agent, None, ""}:
                raise RepoLocalError(f"task claimed by {data.get('claim', {}).get('agent')}, not {args.agent}")
            if not args.evidence.strip():
                raise RepoLocalError("finish requires evidence")
            require_completion(repo, data)
            outcome_findings = outcome_completion_findings(data)
            if outcome_findings:
                raise RepoLocalError("finish blocked by incomplete requested outcomes:\n- " + "\n- ".join(outcome_findings))
            architecture_findings = architecture_finish_findings(root, data)
            if architecture_findings:
                raise RepoLocalError("architecture conformance blocked finish:\n- " + "\n- ".join(architecture_findings))
            finish_evidence = build_finish_evidence(repo, data, args.agent, args.evidence)
            evidence_findings = finish_evidence_findings(data, finish_evidence)
            if evidence_findings:
                raise RepoLocalError("finish blocked by incomplete evidence attribution:\n- " + "\n- ".join(evidence_findings))
            data["status"] = "done"
            data["work_status"] = "completed"
            data["review_status"] = "review" if task_requires_review_evidence(data) else "approved"
            data.setdefault("evidence", []).append(finish_evidence)
            target = task_path(root, "done", data["id"])
            atomic_move_json(path, target, data)
            append_jsonl(root / "evidence" / "events.jsonl", event(data["id"], "task.finished", args.agent, {"evidence": finish_evidence}))
    except StateLockError as exc:
        raise RepoLocalError(str(exc)) from exc
    print(relative(repo, target))
    return 0


def cmd_dirty_check(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    owned = args.owned or [".go/**"]
    dirty = classify_dirty(repo, owned)
    print(json.dumps(dirty, indent=2, ensure_ascii=False))
    return 1 if dirty["blocking"] and args.fail_on_blocking else 0



def load_jsonl_events(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            events.append(data)
    if limit <= 0:
        return []
    return events[-limit:]


def collect_tasks(root: Path, include_done: bool = False) -> dict[str, Any]:
    states = ("open", "active", "blocked", "done")
    result: dict[str, Any] = {"counts": {}, "records": {}}
    for state in states:
        records = []
        for path in sorted((root / "tasks" / state).glob("*.json")):
            data = load_json(path)
            if state != "done" or include_done:
                records.append({
                    "id": data.get("id"),
                    "status": data.get("status"),
                    "summary": data.get("summary"),
                    "scope": data.get("scope"),
                    "acceptance": data.get("acceptance", []),
                    "verification": data.get("verification", []),
                    "evidence": data.get("evidence", []),
                })
        result["counts"][state] = len(list((root / "tasks" / state).glob("*.json")))
        result["records"][state] = records
    result['lifecycle'] = lifecycle_report(root.parent)
    invalid = result['lifecycle']['invalid_done']
    if invalid:
        result['counts']['recorded_done'] = result['counts']['done']
        result['counts']['done'] -= len(invalid)
        result['counts']['completion_invalid'] = len(invalid)
        for record in result['records'].get('done', []):
            if record['id'] in invalid: record['completion_findings'] = invalid[record['id']]
    return result


def build_export_bundle(repo: Path, include_done: bool = False, max_events: int = 20) -> dict[str, Any]:
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot export invalid .go state:\n- " + "\n- ".join(errors))
    root = go_root(repo)
    project = load_json(root / "project.json")
    vision = load_json(root / "vision.json")
    principles = load_json(root / "architecture-principles.json")
    hierarchy = load_json(root / "hierarchy.json")
    tasks = collect_tasks(root, include_done=include_done)
    next_tasks = tasks["records"].get("open", [])
    lifecycle_contracts = {'schema': 'go-workflow.lifecycle-review-bundle.v1', 'project': project,
        'tasks': [load_json(path) for state in ('open', 'active', 'blocked', 'done') if state != 'done' or include_done
                  for path in sorted((root / 'tasks' / state).glob('*.json'))],
        'purpose': 'review_only', 'runtime_transfer': False,
        'evidence_policy': 'Full source records and references preserved; external evidence objects, Git history and active runtime contents are not embedded or attested.'}
    lifecycle_contracts['sha256'] = hashlib.sha256(json.dumps(lifecycle_contracts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {
        "schema": EXPORT_BUNDLE_SCHEMA,
        "kind": "export_bundle",
        "bundle_id": f"{slugify(str(project.get('id') or repo.name))}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}",
        "created_at": now_iso(),
        "source": {
            "repo_name": repo.name,
            "project_id": project.get("id"),
            "project_name": project.get("name"),
        },
        "readback": {
            "north_star": vision.get("north_star"),
            "wedge": vision.get("wedge"),
            "principles": [p.get("id") for p in principles.get("principles", []) if isinstance(p, dict)],
            "epics": [g.get("id") for g in hierarchy_epics(hierarchy)],
            "next_task": None if not next_tasks else {"id": next_tasks[0].get("id"), "summary": next_tasks[0].get("summary")},
        },
        "tasks": tasks,
        "lifecycle_contracts": lifecycle_contracts,
        "history": {
            "runs": load_jsonl_events(root / "runs" / "events.jsonl", max_events),
            "evidence": load_jsonl_events(root / "evidence" / "events.jsonl", max_events),
            "decisions": load_jsonl_events(root / "decisions" / "events.jsonl", max_events),
        },
    }


def validate_export_bundle(data: dict[str, Any]) -> None:
    if data.get("schema") != EXPORT_BUNDLE_SCHEMA:
        raise RepoLocalError("bundle schema mismatch")
    if data.get("kind") != "export_bundle":
        raise RepoLocalError("bundle kind must be export_bundle")
    source = data.get("source")
    if not isinstance(source, dict) or not source.get("project_id"):
        raise RepoLocalError("bundle source.project_id required")
    if not data.get("bundle_id"):
        raise RepoLocalError("bundle_id required")
    if 'lifecycle_contracts' in data:
        value = data['lifecycle_contracts']
        fields = {'schema', 'project', 'tasks', 'purpose', 'runtime_transfer', 'evidence_policy', 'sha256'}
        if (not isinstance(value, dict) or set(value) != fields
                or value.get('schema') != 'go-workflow.lifecycle-review-bundle.v1'
                or value.get('purpose') != 'review_only' or value.get('runtime_transfer') is not False
                or not isinstance(value.get('evidence_policy'), str) or not value['evidence_policy'].strip()
                or not isinstance(value.get('tasks'), list) or not isinstance(value.get('project'), dict)):
            raise RepoLocalError('Invalid lifecycle review bundle')
        payload = {key: item for key, item in value.items() if key != 'sha256'}
        if value['sha256'] != hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest():
            raise RepoLocalError('Lifecycle review bundle digest changed')
        errors = validate_project(value['project'], 'lifecycle_contracts.project')
        seen = set()
        for task in value['tasks']:
            if (not isinstance(task, dict) or not isinstance(task.get('id'), str)
                    or task.get('status') not in ('open', 'active', 'blocked', 'done')):
                raise RepoLocalError('Invalid lifecycle task record')
            errors.extend(validate_task(task, str(task.get('id', '')), expected_status=task.get('status')))
            if task.get('project') != source['project_id'] or task.get('id') in seen:
                errors.append('Lifecycle task project/identity mismatch')
            seen.add(task.get('id'))
        if value['project'].get('id') != source['project_id']: errors.append('Lifecycle project identity mismatch')
        if errors: raise RepoLocalError('; '.join(errors))


def cmd_bundle_export(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    bundle = build_export_bundle(repo, include_done=args.include_done, max_events=args.max_events)
    text = json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(output)
    else:
        print(text, end="")
    return 0


def cmd_bundle_import(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot import into invalid .go state:\n- " + "\n- ".join(errors))
    bundle = load_json(Path(args.bundle).resolve())
    validate_export_bundle(bundle)
    root = go_root(repo)
    project = load_json(root / "project.json")
    source = bundle["source"]
    import_name = slugify(str(bundle["bundle_id"])) + ".json"
    target = root / "imports" / import_name
    plan = {
        "schema": "go-workflow.repo-local.import-plan.v1",
        "kind": "import_plan",
        "mode": "write" if args.write else "dry_run",
        "target_project": {"id": project.get("id"), "name": project.get("name")},
        "source_project": {"id": source.get("project_id"), "name": source.get("project_name")},
        "bundle_id": bundle.get("bundle_id"),
        "target_path": relative(repo, target),
        "source_task_counts": bundle.get("tasks", {}).get("counts", {}),
        "actions": [
            "validate target .go state",
            "validate export bundle schema",
            "write immutable import artifact under .go/imports/" if args.write else "dry-run only; no files written",
            "append decision.recorded event" if args.write else "skip event append until --write",
        ],
    }
    if not args.write:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    if target.exists() and not args.force:
        raise RepoLocalError(f"import artifact already exists: {relative(repo, target)}; pass --force to replace")
    target.parent.mkdir(parents=True, exist_ok=True)
    dump_json(target, {"plan": plan, "bundle": bundle})
    append_jsonl(root / "decisions" / "events.jsonl", event(
        args.task_id,
        "decision.recorded",
        args.agent,
        {
            "decision": "imported repo-local export bundle as review/reconcile artifact",
            "bundle_id": bundle.get("bundle_id"),
            "source_project": source.get("project_id"),
            "target_path": relative(repo, target),
        },
    ))
    errors = validate_repo(repo)
    if errors:
        target.unlink(missing_ok=True)
        raise RepoLocalError("import wrote invalid .go state:\n- " + "\n- ".join(errors))
    print(relative(repo, target))
    return 0


def delivery_task_records(root: Path, task_ids: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for task_id in task_ids:
        for status in ("done", "active", "blocked", "open"):
            path = task_path(root, status, task_id)
            if path.is_file():
                records.append(load_json(path))
                break
    return records


def delivery_sensitive_findings(payload: dict[str, Any]) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return [label for label, pattern in DELIVERY_SENSITIVE_PATTERNS if pattern.search(text)]


def delivery_html(manifest: dict[str, Any]) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    sections = manifest["sections"]
    release = manifest["release"]
    delivered = sections["delivered_scope"]
    evidence = sections["evidence"]
    status_label = {"draft": "Concept", "released": "Gereed", "superseded": "Vervangen"}[release["status"]]

    outcome_items = "".join(
        f'<li><span>{index:02d}</span><strong>{esc(value)}</strong></li>'
        for index, value in enumerate(delivered[:3], start=1)
    )
    remaining_outcomes = ""
    if len(delivered) > 3:
        remaining_outcomes = (
            f'<details class="more-items"><summary>Nog {len(delivered) - 3} onderdelen</summary><ul>'
            + "".join(f"<li>{esc(value)}</li>" for value in delivered[3:])
            + "</ul></details>"
        )
    excluded = sections["excluded_scope"]
    excluded_more = ""
    if len(excluded) > 1:
        excluded_more = (
            f'<details class="more-items"><summary>Nog {len(excluded) - 1} grenzen</summary><ul>'
            + "".join(f"<li>{esc(value)}</li>" for value in excluded[1:])
            + "</ul></details>"
        )
    evidence_items = "".join(
        f'<li><span aria-hidden="true">✓</span>{esc(item["label"])}</li>'
        for item in evidence
    )
    supersedes = release.get("supersedes")
    supersedes_row = f'<div><dt>Vervangt</dt><dd>{esc(supersedes)}</dd></div>' if supersedes else ""
    created_date = str(release["created_at"])[:10]
    return f'''<!doctype html>
<html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delivery — {esc(manifest["assignment"]["title"])}</title><style>
:root{{--canvas:#eef1f6;--surface:#ffffff;--ink:#111827;--muted:#596273;--line:#dfe4ec;--accent:#2457ff;--accent-soft:#edf2ff;--success:#08775d;--dark:#111d35}}*{{box-sizing:border-box}}html{{background:var(--canvas);color:var(--ink);font-family:Atkinson Hyperlegible,"Segoe UI",Verdana,sans-serif;line-height:1.6;letter-spacing:.012em}}body{{margin:0}}.page{{width:min(1040px,calc(100% - 32px));margin:28px auto 64px;background:var(--surface);box-shadow:0 24px 70px rgba(24,35,58,.12)}}.accent{{height:10px;background:var(--accent)}}header{{padding:34px 48px 0}}.topline{{display:flex;align-items:center;justify-content:space-between;gap:20px;color:var(--muted);font-size:.78rem;font-weight:750;text-transform:uppercase;letter-spacing:.1em}}.release-chip{{color:var(--success);background:#e7f5f0;padding:6px 9px;border-radius:4px}}h1{{font-size:clamp(2.45rem,6.7vw,4.9rem);line-height:1.03;margin:42px 0 18px;max-width:820px;font-weight:760;text-wrap:balance}}.lede{{max-width:760px;margin:0 0 36px;color:var(--muted);font-size:clamp(1.05rem,2vw,1.22rem);line-height:1.65;text-wrap:pretty}}.signal-strip{{display:grid;grid-template-columns:repeat(3,1fr);background:var(--dark);color:white;margin:0 -48px}}.signal{{padding:22px 48px;border-right:1px solid rgba(255,255,255,.14)}}.signal:last-child{{border-right:0}}.signal strong{{display:block;font-size:1.35rem;font-variant-numeric:tabular-nums}}.signal span{{display:block;margin-top:2px;color:#b8c2d8;font-size:.78rem}}main{{padding:52px 48px 44px}}section{{margin-bottom:42px}}h2{{font-size:1.55rem;line-height:1.2;margin:0 0 22px;font-weight:760}}.outcome-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:0;list-style:none;padding:0;margin:0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}}.outcome-grid>li{{padding:24px 24px 26px 0;min-width:0}}.outcome-grid>li+li{{padding-left:24px;border-left:1px solid var(--line)}}.outcome-grid span{{display:block;color:var(--accent);font-size:.75rem;font-weight:800;letter-spacing:.1em;margin-bottom:10px}}.outcome-grid strong{{font-size:1.02rem;line-height:1.45}}.boundary{{display:grid;grid-template-columns:150px 1fr;gap:24px;margin-top:24px;padding:18px 0;border-bottom:1px solid var(--line)}}.boundary>strong{{font-size:.78rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}.boundary p{{margin:0}}.decision-grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.decision{{background:#f7f8fb;padding:22px 24px;border-left:4px solid var(--line)}}.decision.next{{border-left-color:var(--accent);background:var(--accent-soft)}}.decision h2{{font-size:.82rem;text-transform:uppercase;letter-spacing:.09em;margin-bottom:9px;color:var(--muted)}}.decision p{{margin:0;font-weight:650}}details{{border-top:1px solid var(--line)}}details:last-of-type{{border-bottom:1px solid var(--line)}}summary{{cursor:pointer;min-height:52px;display:flex;align-items:center;justify-content:space-between;gap:16px;font-weight:720;list-style:none}}summary::-webkit-details-marker{{display:none}}summary::after{{content:"+";color:var(--accent);font-size:1.35rem;font-weight:500}}details[open]>summary::after{{content:"−"}}summary:focus-visible{{outline:3px solid var(--accent);outline-offset:3px}}.detail-count{{color:var(--muted);font-size:.78rem;font-weight:500;margin-left:auto}}.proof-list{{list-style:none;margin:0 0 22px;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:8px 24px}}.proof-list li{{display:grid;grid-template-columns:20px 1fr;gap:8px;color:var(--muted);font-size:.9rem}}.proof-list span{{color:var(--success);font-weight:800}}.release-data{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px 24px;margin:0 0 22px}}.release-data div{{min-width:0}}dt{{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.08em}}dd{{margin:3px 0 0;font-weight:650;overflow-wrap:anywhere}}code{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.78rem}}.more-items{{border:0!important;margin-top:14px}}.more-items summary{{display:inline-flex;min-height:36px;color:var(--accent);font-size:.86rem}}.more-items ul{{margin-top:4px}}footer{{padding:18px 48px 24px;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:.75rem}}@media(max-width:720px){{.page{{width:100%;margin:0;box-shadow:none}}header{{padding:26px 22px 0}}.topline{{align-items:flex-start}}h1{{margin-top:34px;font-size:2.25rem;line-height:1.08}}.signal-strip{{margin:0 -22px;grid-template-columns:1fr 1fr 1fr}}.signal{{padding:16px 12px;text-align:center}}.signal strong{{font-size:1.05rem}}.signal span{{font-size:.67rem;line-height:1.35}}main{{padding:38px 22px 30px}}.outcome-grid{{grid-template-columns:1fr}}.outcome-grid>li,.outcome-grid>li+li{{padding:18px 0;border-left:0;border-top:1px solid var(--line)}}.outcome-grid>li:first-child{{border-top:0}}.boundary{{grid-template-columns:1fr;gap:6px}}.decision-grid,.proof-list,.release-data{{grid-template-columns:1fr}}footer{{padding:16px 22px 22px;display:block}}footer span{{display:block;margin-top:4px}}}}@media print{{html{{background:white}}.page{{width:auto;margin:0;box-shadow:none}}details{{break-inside:avoid}}}}
</style></head><body><div class="page"><div class="accent"></div><header><div class="topline"><span>{esc(manifest["project"]["name"])}</span><span class="release-chip">{esc(status_label)} · v{release["version"]}</span></div><h1>{esc(manifest["assignment"]["title"])}</h1><p class="lede">{esc(sections["summary"])}</p><div class="signal-strip"><div class="signal"><strong>{esc(status_label)}</strong><span>opleverstatus</span></div><div class="signal"><strong>{len(delivered)}</strong><span>opgeleverde onderdelen</span></div><div class="signal"><strong>{len(evidence)}</strong><span>controles vastgelegd</span></div></div></header><main>
<section><h2>Opgeleverd</h2><ol class="outcome-grid">{outcome_items}</ol>{remaining_outcomes}<div class="boundary"><strong>Niet inbegrepen</strong><div><p>{esc(excluded[0])}</p>{excluded_more}</div></div></section>
<section class="decision-grid"><div class="decision"><h2>Aandachtspunt</h2><p>{esc(sections["limitations"][0])}</p></div><div class="decision next"><h2>Volgende stap</h2><p>{esc(sections["next_steps"][0])}</p></div></section>
<details class="evidence-details"><summary>Technisch bewijs <span class="detail-count">{len(evidence)} items</span></summary><ul class="proof-list">{evidence_items}</ul></details>
<details class="release-details"><summary>Release en provenance</summary><dl class="release-data"><div><dt>Delivery</dt><dd>{esc(manifest["delivery_id"])}</dd></div><div><dt>Status</dt><dd>{esc(release["status"])}</dd></div><div><dt>Disclosure</dt><dd>{esc(manifest["disclosure"]["class"])}</dd></div><div><dt>Datum</dt><dd>{esc(created_date)}</dd></div>{supersedes_row}<div><dt>Bronhash</dt><dd><code>{esc(manifest["provenance"]["source_sha256"])}</code></dd></div><div><dt>HTML-hash</dt><dd>Zie <code>manifest.json</code></dd></div></dl></details>
</main><footer><span>{esc(manifest["delivery_id"])}</span><span>Zelfstandig HTML-bestand · geen externe toegang nodig</span></footer></div></body></html>
'''


def cmd_delivery_build(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    if not getattr(args, "delivery_lock_held", False):
        try:
            with repository_lock(root, f"delivery-{args.epic}"):
                args.delivery_lock_held = True
                return cmd_delivery_build(args)
        except StateLockError as exc:
            raise RepoLocalError(str(exc)) from exc
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("invalid repo-local contract:\n- " + "\n- ".join(errors))
    project = load_json(root / "project.json")
    hierarchy = load_json(root / "hierarchy.json")
    epic = next((item for item in hierarchy_epics(hierarchy) if item.get("id") == args.epic), None)
    if not epic:
        raise RepoLocalError(f"unknown epic: {args.epic}")
    tasks = delivery_task_records(root, epic_task_ids(epic))
    done = [task for task in tasks if task.get("status") == "done"]
    delivered = args.delivered or [str(task.get("summary")) for task in done] or [f"Delivery voor {epic['title']}"]
    excluded = args.excluded or ["Geen aanvullende uitgesloten scope vastgelegd."]
    limitations = args.limitation or ["Alleen bewijs dat in .go is vastgelegd wordt in deze levering vertegenwoordigd."]
    next_steps = args.next_step or [str(task.get("summary")) for task in tasks if task.get("status") != "done"] or ["Review deze levering met de opdrachtgever."]
    evidence_items: list[dict[str, str]] = []
    for task in done:
        latest = (task.get("evidence") or [{}])[-1]
        summary = latest.get("summary") if isinstance(latest, dict) else str(latest)
        evidence_items.append({"label": str(task.get("summary")), "summary": str(summary or "Task afgerond met repo-lokale evidence.")})
    if not evidence_items:
        evidence_items = [{"label": "Workflow", "summary": f"{len(done)}/{len(tasks)} gekoppelde tasks afgerond."}]
    delivery_id = args.delivery_id or f"{slugify(args.epic)}-v{args.version}"
    target = root / "deliveries" / delivery_id
    existing_manifest = target / "manifest.json"
    existing: dict[str, Any] | None = None
    created_at = now_iso()
    if existing_manifest.is_file():
        existing = load_json(existing_manifest)
        created_at = str(existing.get("release", {}).get("created_at") or created_at)
    if args.supersedes:
        superseded_path = root / "deliveries" / args.supersedes / "manifest.json"
        if not superseded_path.is_file():
            raise RepoLocalError(f"superseded delivery does not exist: {args.supersedes}")
        superseded = load_json(superseded_path)
        if args.version <= int(superseded.get("release", {}).get("version") or 0):
            raise RepoLocalError("replacement delivery version must be greater than the superseded version")
    source_payload = {"project": project, "epic": epic, "tasks": tasks, "summary": args.summary or epic.get("description") or f"Delivery voor {epic['title']}.", "delivered": delivered, "excluded": excluded, "limitations": limitations, "next_steps": next_steps, "version": args.version, "status": args.status, "disclosure": args.disclosure, "supersedes": args.supersedes or None}
    source_sha = hashlib.sha256(json.dumps(source_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    sensitive_findings = delivery_sensitive_findings(source_payload)
    if sensitive_findings and args.disclosure != "restricted":
        raise RepoLocalError("delivery is unsafe for public disclosure: " + ", ".join(sensitive_findings))
    manifest = {"schema": DELIVERY_SCHEMA, "delivery_id": delivery_id, "project": {"id": project["id"], "name": project["name"]}, "assignment": {"kind": "epic", "id": epic["id"], "title": epic["title"]}, "release": {"version": args.version, "status": args.status, "created_at": created_at, "supersedes": args.supersedes or None}, "disclosure": {"class": args.disclosure, "scan_status": "blocked" if sensitive_findings else "passed"}, "sections": {"summary": source_payload["summary"], "delivered_scope": delivered, "excluded_scope": excluded, "evidence": evidence_items, "limitations": limitations, "next_steps": next_steps}, "provenance": {"source": ".go", "source_sha256": source_sha, "html_sha256": None}}
    findings = validate_delivery_manifest(manifest)
    if findings:
        raise RepoLocalError("invalid delivery manifest:\n- " + "\n- ".join(findings))
    html_text = delivery_html(manifest)
    html_sha = hashlib.sha256(html_text.encode("utf-8")).hexdigest()
    manifest["provenance"]["html_sha256"] = html_sha
    if existing and existing.get("release", {}).get("status") in {"released", "superseded"}:
        if existing.get("provenance", {}).get("source_sha256") == source_sha and existing.get("provenance", {}).get("html_sha256") == html_sha:
            print(json.dumps({"delivery_id": delivery_id, "html": relative(repo, target / "index.html"), "manifest": relative(repo, existing_manifest), "source_sha256": source_sha, "html_sha256": html_sha}, indent=2, ensure_ascii=False))
            return 0
        raise RepoLocalError("released delivery is immutable; create a higher version and use --supersedes")
    target.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target / "index.html", html_text)
    atomic_write_text(target / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"delivery_id": delivery_id, "html": relative(repo, target / "index.html"), "manifest": relative(repo, target / "manifest.json"), "source_sha256": source_sha, "html_sha256": html_sha}, indent=2, ensure_ascii=False))
    return 0


def cmd_delivery_publish(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("invalid repo-local contract:\n- " + "\n- ".join(errors))
    manifest = go_root(repo) / "deliveries" / args.delivery_id / "manifest.json"
    if not manifest.is_file():
        raise RepoLocalError(f"delivery manifest not found: {args.delivery_id}")
    raise RepoLocalError("publisher adapter is not configured; delivery build is local-only and publication requires an explicit adapter")


def cmd_decision_create(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot record decision in invalid .go state:\n- " + "\n- ".join(errors))
    decision_id = slugify(args.id or args.title)
    if not TASK_ID_RE.fullmatch(decision_id):
        raise RepoLocalError(f"invalid decision id: {decision_id}")
    append_jsonl(root / "decisions" / "events.jsonl", event(
        args.task_id or "project-decision",
        "decision.recorded",
        args.agent,
        {
            "decision_id": decision_id,
            "title": args.title,
            "status": args.status,
            "context": args.context,
            "decision": args.decision,
            "consequences": args.consequence or [],
        },
    ))
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("decision wrote invalid .go state:\n- " + "\n- ".join(errors))
    print(f"{decision_id} — {args.title}")
    return 0


def cmd_epic_create(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot create epic in invalid .go state:\n- " + "\n- ".join(errors))
    epic_id = slugify(args.id or args.title)
    path = root / "hierarchy.json"
    hierarchy = load_json(path)
    epics = hierarchy_epics(hierarchy)
    if any(epic.get("id") == epic_id for epic in epics):
        raise RepoLocalError(f"epic already exists: {epic_id}")
    epics.append({
        "id": epic_id,
        "title": args.title,
        "description": args.description,
        "features": [],
        "tasks": [],
    })
    set_hierarchy_epics(hierarchy, epics)
    dump_json(path, hierarchy)
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("epic wrote invalid .go state:\n- " + "\n- ".join(errors))
    print(f"{epic_id} — {args.title}")
    return 0


def cmd_readback(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    root = go_root(repo)
    project = load_json(root / "project.json")
    vision = load_json(root / "vision.json")
    principles = load_json(root / "architecture-principles.json")
    hierarchy = load_json(root / "hierarchy.json")
    tasks = open_tasks(repo)
    print(f"Project: {project['name']} ({project['id']})")
    print(f"North star: {vision['north_star']}")
    print(f"Wedge: {vision['wedge']}")
    print("Principles: " + "; ".join(p["id"] for p in principles.get("principles", [])))
    print("Epics: " + "; ".join(g["id"] for g in hierarchy_epics(hierarchy)))
    architecture = summarize_architecture(root)
    print(
        "Architecture: "
        f"briefs={architecture['briefs']['total']}; "
        f"open_deviations={architecture['open_deviations']}; "
        f"active_waivers={architecture['active_waivers']}"
    )
    if tasks:
        print(f"Next task: {tasks[0][1]['id']} — {tasks[0][1]['summary']}")
    else:
        print("Next task: none")
    return 0


def architecture_task(root: Path, task_id: str) -> dict[str, Any] | None:
    if not task_id:
        return None
    _path, task = find_task(root, task_id)
    return task


def cmd_architecture_validate(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    project = load_json(root / "project.json")
    errors = validate_architecture_state(root, str(project.get("id") or ""))
    payload = {"schema": "go-workflow.architecture-validation.v1", "valid": not errors, "errors": errors}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
    else:
        print(f"ok: {root / 'architecture'}")
    return 0 if not errors else 1


def architecture_readback_payload(repo: Path, task_id: str = "") -> dict[str, Any]:
    root = go_root(repo)
    task = architecture_task(root, task_id)
    briefs = architecture_briefs(root)
    return {
        "schema": "go-workflow.architecture-readback.v1",
        "project": load_json(root / "project.json").get("id"),
        "task_id": task_id or None,
        "status": summarize_architecture(root),
        "briefs": [briefs[brief_id] for brief_id in sorted(briefs)],
        "applicable_architecture": resolve_applicable_architecture(root, task),
    }


def cmd_architecture_readback(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot read back invalid .go state:\n- " + "\n- ".join(errors))
    payload = architecture_readback_payload(repo, args.task_id)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"project: {payload['project']}")
        print(f"briefs: {payload['status']['briefs']['total']}")
        print(f"open_deviations: {payload['status']['open_deviations']}")
        print(f"active_waivers: {payload['status']['active_waivers']}")
        if args.task_id:
            applicable = payload["applicable_architecture"]
            print(f"task: {args.task_id}")
            print(f"impact: {applicable['classification']['impact']}")
            print(f"decisions: {len(applicable['decisions'])}")
    return 0


def cmd_architecture_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot summarize invalid .go state:\n- " + "\n- ".join(errors))
    payload = {"schema": "go-workflow.architecture-status.v1", **summarize_architecture(go_root(repo))}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("briefs: " + str(payload["briefs"]["total"]))
        print("open_deviations: " + str(payload["open_deviations"]))
        print("active_waivers: " + str(payload["active_waivers"]))
    return 0


def architecture_lane_event(event_name: str, task_id: str, scope_id: str, actor: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "go-workflow.architecture-event.v1",
        "kind": "architecture_event",
        "event": event_name,
        "created_at": now_iso(),
        "task_id": task_id,
        "scope_id": scope_id or "project",
        "actor": actor,
        "data": data,
    }


def cmd_architecture_classify(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    path, task = find_task(root, args.task_id)
    if task.get("status") == "done" and args.write:
        raise RepoLocalError("cannot reclassify a done task")
    existing = task.get("architecture") if isinstance(task.get("architecture"), dict) else {}
    requested = args.impact or str(existing.get("impact") or "none")
    minimum, signals = deterministic_minimum_impact(task)
    candidate = dict(existing)
    candidate.update({
        "impact": max((requested, minimum), key=lambda item: {"none": 0, "local": 1, "material": 2, "foundational": 3}[item]),
        "scope_refs": args.scope_ref or existing.get("scope_refs", []),
        "concerns": args.concern or existing.get("concerns", []),
        "decision_ids": args.decision_id or existing.get("decision_ids", []),
        "conformance_required": args.conformance_required or bool(existing.get("conformance_required", False)),
        "human_gate": args.human_gate or str(existing.get("human_gate") or "none"),
    })
    if candidate["impact"] in {"material", "foundational"}:
        candidate["conformance_required"] = True
    if candidate["impact"] == "foundational" and candidate["human_gate"] == "none":
        candidate["human_gate"] = "risk_acceptance"
    payload = {
        "schema": "go-workflow.architecture-classification.v1",
        "task_id": task.get("id"),
        "requested_impact": requested,
        "deterministic_minimum": minimum,
        "signals": signals,
        "classification": candidate,
        "written": bool(args.write),
    }
    if args.write:
        task["architecture"] = candidate
        errors = validate_task(task, relative(repo, path))
        if errors:
            raise RepoLocalError("classification produced an invalid task:\n- " + "\n- ".join(errors))
        dump_json(path, task)
        append_jsonl(root / "architecture" / "events.jsonl", architecture_lane_event(
            "architecture.classified", str(task.get("id")), candidate["scope_refs"][0] if candidate["scope_refs"] else "project", args.actor,
            {"status": "recorded", "requested_impact": requested, "minimum_impact": minimum, "effective_impact": candidate["impact"], "signals": signals},
        ))
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else f"{task.get('id')}: {candidate['impact']} (minimum={minimum})")
    return 0


def parse_architecture_checks(values: list[str]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for value in values:
        parts = value.split("=", 2)
        checks.append({"id": parts[0], "status": parts[1] if len(parts) > 1 else "passed", "evidence": parts[2] if len(parts) > 2 else ""})
    return checks


def cmd_architecture_conformance(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    _path, task = find_task(root, args.task_id)
    if args.status in {"passed", "passed_with_waiver"} and not args.evidence_ref:
        raise RepoLocalError("passing conformance requires at least one --evidence-ref")
    if args.status == "passed_with_waiver" and not args.waiver_id:
        raise RepoLocalError("passed_with_waiver requires --waiver-id")
    data = {
        "status": args.status,
        "principle_checks": parse_architecture_checks(args.principle_check),
        "decision_checks": parse_architecture_checks(args.decision_check),
        "quality_attribute_checks": parse_architecture_checks(args.quality_attribute_check),
        "evidence_refs": args.evidence_ref,
        "waiver_ids": args.waiver_id,
    }
    event_name = "architecture.deviation.recorded" if args.status == "deviation" else "architecture.conformance.recorded"
    if args.status == "deviation":
        data["status"] = "open"
        data["conformance_status"] = "deviation"
        data["deviation_id"] = f"{task.get('id')}:{args.scope_id}"
    append_jsonl(root / "architecture" / "events.jsonl", architecture_lane_event(
        event_name, str(task.get("id")), args.scope_id, args.actor, data,
    ))
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("conformance event produced invalid state:\n- " + "\n- ".join(errors))
    print(f"{task.get('id')}: conformance={args.status}")
    return 0


def cmd_architecture_review(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    _path, task = find_task(root, args.task_id)
    if args.human and args.actor.strip().lower() in {"agent", "hermes", "codex", "architecture-critic", "template-check", "make-check"}:
        raise RepoLocalError("--human approval requires a named human actor, not an automation identity")
    data = {"status": args.status, "human": bool(args.human), "evidence_ref": args.evidence_ref}
    append_jsonl(root / "architecture" / "events.jsonl", architecture_lane_event(
        "architecture.reviewed", str(task.get("id")), args.scope_id, args.actor, data,
    ))
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("review event produced invalid state:\n- " + "\n- ".join(errors))
    print(f"{task.get('id')}: review={args.status} human={bool(args.human)}")
    return 0


def cmd_architecture_waiver(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    _path, task = find_task(root, args.task_id)
    try:
        expiry = datetime.fromisoformat(args.expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RepoLocalError("--expires-at must be an ISO datetime") from exc
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise RepoLocalError("--expires-at must be a future timezone-aware datetime")
    data = {
        "waiver_id": args.id,
        "status": "active",
        "reason": args.reason,
        "accepted_risk": args.accepted_risk,
        "expires_at": args.expires_at,
        "deviation_ids": args.deviation_id,
    }
    append_jsonl(root / "architecture" / "events.jsonl", architecture_lane_event(
        "architecture.waiver.granted", str(task.get("id")), args.scope_id, args.actor, data,
    ))
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("waiver event produced invalid state:\n- " + "\n- ".join(errors))
    print(f"{args.id}: active until {args.expires_at}")
    return 0


def cmd_architecture_waiver_close(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    _path, task = find_task(root, args.task_id)
    event_name = f"architecture.waiver.{args.status}"
    data = {"waiver_id": args.id, "status": args.status, "reason": args.reason}
    append_jsonl(root / "architecture" / "events.jsonl", architecture_lane_event(
        event_name, str(task.get("id")), args.scope_id, args.actor, data,
    ))
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("waiver closure produced invalid state:\n- " + "\n- ".join(errors))
    print(f"{args.id}: {args.status}")
    return 0


def route_repo(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    root = go_root(repo)
    project_file = root / "project.json"
    if project_file.exists():
        errors = validate_repo(repo)
        project_id = ""
        project_name = ""
        try:
            project = load_json(project_file)
            project_id = str(project.get("id") or "")
            project_name = str(project.get("name") or "")
        except RepoLocalError:
            pass
        return {
            "repo": str(repo),
            "mode": "repo-local",
            "state_root": ".go",
            "project_id": project_id,
            "project_name": project_name,
            "valid": not errors,
            "reason": ".go/project.json exists",
            "fallback": None,
            "errors": errors,
        }
    return {
        "repo": str(repo),
        "mode": "missing-local-contract",
        "state_root": None,
        "project_id": "",
        "project_name": "",
        "valid": False,
        "reason": "no .go/project.json in target repo; adopt or spike the repository before durable workflow work",
        "fallback": None,
        "next_command": shell_join("python3", Path(__file__).resolve(), "adopt", repo),
        "errors": ["repo-local .go/project.json is required"],
    }


def cmd_route(args: argparse.Namespace) -> int:
    route = route_repo(Path(args.repo))
    if args.json:
        print(json.dumps(route, indent=2, ensure_ascii=False))
    else:
        print(f"mode: {route['mode']}")
        print(f"repo: {route['repo']}")
        print(f"state_root: {route['state_root']}")
        if route.get("project_id"):
            print(f"project: {route['project_name']} ({route['project_id']})")
        print(f"reason: {route['reason']}")
        if route.get("errors"):
            print("errors:")
            for error in route["errors"]:
                print(f"- {error}")
    return 0 if route["valid"] else 1


class ExplicitShipPolicy(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.explicit_ship_policy = values


class ExplicitCampaignLimit(argparse.Action):
    """Retain explicit user limits without promoting parser defaults to authority."""
    def __call__(self, parser, namespace, values, option_string=None):
        if values < 1:
            raise argparse.ArgumentError(self, "limit must be positive")
        setattr(namespace, self.dest, values)
        budget = dict(getattr(namespace, "explicit_campaign_budget", {}))
        key = "wall_seconds" if self.dest == "max_minutes" else self.dest
        budget[key] = values * 60 if self.dest == "max_minutes" else values
        namespace.explicit_campaign_budget = budget


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    version = sub.add_parser("version", help="Report the stack and contract runtime versions")
    version.add_argument("--json", action="store_true")
    version.set_defaults(func=cmd_version)
    adopt = sub.add_parser("adopt", help="Adopt a repo by creating real repo-local .go state")
    adopt.add_argument("repo", nargs="?", default=".")
    adopt.add_argument("--project-id")
    adopt.add_argument("--name")
    adopt.add_argument("--repo-url", default="")
    adopt.add_argument("--verification", action="append", default=[])
    adopt.add_argument("--north-star", default="")
    adopt.add_argument("--wedge", default="")
    adopt.add_argument("--target-user", default="")
    adopt.add_argument("--core-promise", default="")
    adopt.add_argument("--product-principle", action="append", default=[])
    adopt.add_argument("--non-goal", action="append", default=[])
    adopt.add_argument("--success-metric", action="append", default=[])
    adopt.add_argument("--principle", action="append", default=[], help="id|statement|rationale|enforcement")
    adopt.add_argument("--feature-group", action="append", default=[], help="legacy alias for epic: id|title")
    adopt.add_argument("--feature", action="append", default=[], help="epic_id|feature_id|title")
    adopt.add_argument("--force", action="store_true")
    adopt.add_argument('--lifecycle-settings', default='', help='Explicit lifecycle settings for a new project; no execution authority')
    adopt.set_defaults(func=cmd_adopt)
    spike = sub.add_parser("spike", help="Bootstrap a repo and .go contract from rough intent")
    spike.add_argument("repo")
    spike.add_argument("--project-id")
    spike.add_argument("--name")
    spike.add_argument("--repo-url", default="")
    spike.add_argument("--brief", default="")
    spike.add_argument("--north-star", default="")
    spike.add_argument("--wedge", default="")
    spike.add_argument("--target-user", default="")
    spike.add_argument("--core-promise", default="")
    spike.add_argument("--product-principle", action="append", default=[])
    spike.add_argument("--non-goal", action="append", default=[])
    spike.add_argument("--success-metric", action="append", default=[])
    spike.add_argument("--principle", action="append", default=[], help="id|statement|rationale|enforcement")
    spike.add_argument("--epic", action="append", default=[], help="epic_id|title")
    spike.add_argument("--target-epic", default="", help="epic id to attach generated tasks to")
    spike.add_argument("--task", action="append", default=[], help="task_id|summary")
    spike.add_argument("--task-scope", choices=["code", "docs"], default="code", help="default scope preset for generated tasks")
    spike.add_argument("--execution-mode", choices=["mechanical", "agent"], default="agent", help="execution mode for generated tasks")
    spike.add_argument("--verification", action="append", default=[])
    spike.add_argument("--skip-repo-complete", action="store_true")
    spike.add_argument("--agent", default="agent")
    spike.add_argument("--json", action="store_true")
    spike.add_argument('--lifecycle-settings', default='', help='Explicit lifecycle settings for a new project; no execution authority')
    spike.set_defaults(func=cmd_spike)
    intake = sub.add_parser("intake", help="Explore rough intent through a bounded semantic assessment")
    intake_sub = intake.add_subparsers(dest="intake_command", required=True)
    intake_explore = intake_sub.add_parser("explore", help="Prepare or apply an adapter-assessed intake")
    intake_explore.add_argument("repo", nargs="?", default=".")
    intake_explore.add_argument("--intent", required=True)
    intake_explore.add_argument("--source-ref", required=True)
    intake_explore.add_argument("--authority", choices=["advice", "planning", "execute"], default="planning")
    intake_explore.add_argument("--assessment-command", default="")
    intake_explore.add_argument("--executor-agent", choices=["codex", "none"], default="none")
    intake_explore.add_argument("--model", default="")
    intake_explore.add_argument("--effort", choices=["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"], default="high")
    intake_explore.add_argument("--timeout-seconds", type=int, default=900)
    intake_explore.add_argument("--write", action="store_true")
    intake_explore.add_argument("--agent", default="agent")
    intake_explore.add_argument("--json", action="store_true")
    intake_explore.set_defaults(func=cmd_intake_explore)
    go = sub.add_parser("go", help="Bare go universal router: loose command vs repo-local .go autonomous loop")
    go.add_argument("repo", nargs="?", default=".")
    go.add_argument("--intent", default="")
    go.add_argument("--intent-source-ref", default="", help="optional durable source reference, for example telegram:<chat>:<message>")
    go.add_argument("--execution-brief", default="", help="schema-validated compact recommendation handoff to materialize as semantic work units")
    go.add_argument("--authority-source", choices=["go", "imperative", "sent_as_goal"], default="go", help="execution authority that promoted a pending recommendation")
    go.add_argument("--loop", action="store_true", help="force go-loop rather than go-auto")
    go.add_argument("--write", action="store_true", help="materialize intent-created tasks; default --json/plan mode is non-mutating")
    go.add_argument("--execute", action="store_true", help="execute selected auto/go-loop lifecycle")
    go.add_argument("--max-tasks", type=int, default=10, action=ExplicitCampaignLimit)
    go.add_argument("--summary-chars", type=int, default=900)
    go.add_argument("--max-minutes", type=int, default=90, action=ExplicitCampaignLimit)
    go.add_argument("--max-commands", type=int, default=120, action=ExplicitCampaignLimit)
    go.add_argument("--command-timeout-seconds", type=int, default=900)
    go.add_argument("--max-attempts", type=int, default=5, action=ExplicitCampaignLimit)
    go.add_argument("--build-command", default="", help="optional adapter command run before verification; supports safe {repo_shell}, raw {repo}, {task_id}, {attempt}, {strategy}")
    go.add_argument("--critic-command", default="", help="optional adapter command run after passing verification; non-zero blocks/repairs")
    go.add_argument("--repair-command", default="", help="optional adapter command run after failed verify/critic before next attempt")
    go.add_argument("--repair-agent", choices=["codex", "hermes"], default="", help="use a built-in repair adapter command template")
    go.add_argument("--executor-agent", choices=["auto", "codex", "hermes", "none"], default=executor_agent_default(), help="executor for agent tasks (default: GO_EXECUTOR_AGENT or auto)")
    go.add_argument("--semantic-critic", action=argparse.BooleanOptionalAction, default=True, help="run built-in semantic critic before finish (default: enabled)")
    go.add_argument("--followup-on-block", action="store_true", help="create a scoped follow-up task when critic blocks")
    go.add_argument("--checkpoint-every-tasks", type=int, default=1)
    go.add_argument("--ship-policy", choices=["none", "local-commit", "push"], default="none", action=ExplicitShipPolicy)
    go.add_argument("--allow-push", action="store_true")
    go.add_argument("--agent", default="agent")
    go.add_argument("--allow-dirty", action="store_true")
    go.add_argument("--json", action="store_true")
    go.set_defaults(func=cmd_go)
    auto = sub.add_parser("auto", help="Emit the go-auto control-handoff contract; the invoking coding agent executes until done/gate/budget")
    auto.add_argument("repo", nargs="?", default=".")
    auto.add_argument("--max-tasks", type=int, default=3)
    auto.add_argument("--summary-chars", type=int, default=900)
    auto.add_argument("--max-minutes", type=int, default=45)
    auto.add_argument("--max-commands", type=int, default=36)
    auto.add_argument("--command-timeout-seconds", type=int, default=900)
    auto.add_argument("--max-attempts", type=int, default=5)
    auto.add_argument("--build-command", default="", help="optional adapter command run before verification; supports safe {repo_shell}, raw {repo}, {task_id}, {attempt}, {strategy}")
    auto.add_argument("--critic-command", default="", help="optional adapter command run after passing verification; non-zero blocks/repairs")
    auto.add_argument("--repair-command", default="", help="optional adapter command run after failed verify/critic before next attempt")
    auto.add_argument("--repair-agent", choices=["codex", "hermes"], default="", help="use a built-in repair adapter command template")
    auto.add_argument("--executor-agent", choices=["auto", "codex", "hermes", "none"], default=executor_agent_default(), help="executor for agent tasks (default: GO_EXECUTOR_AGENT or auto)")
    auto.add_argument("--semantic-critic", action=argparse.BooleanOptionalAction, default=True, help="run built-in semantic critic before finish (default: enabled)")
    auto.add_argument("--followup-on-block", action="store_true", help="create a scoped follow-up task when critic blocks")
    auto.add_argument("--checkpoint-every-tasks", type=int, default=1)
    auto.add_argument("--ship-policy", choices=["none", "local-commit", "push"], default="none")
    auto.add_argument("--allow-push", action="store_true")
    auto.add_argument("--execute", action="store_true", help="execute the lifecycle: preflight, claim, run verification, finish/block, reflect")
    auto.add_argument("--emit-handoff", action="store_true", help="emit a Codex/Hermes-compatible agent handoff JSON")
    auto.add_argument("--agent", default="agent")
    auto.add_argument("--allow-dirty", action="store_true", help="explicitly override dirty/lock preflight gates")
    auto.add_argument("--json", action="store_true")
    auto.set_defaults(func=cmd_auto)
    adapter = sub.add_parser("adapter", help="Inspect and validate the versioned agent-adapter protocol")
    adapter_sub = adapter.add_subparsers(dest="adapter_command", required=True)
    adapter_validate = adapter_sub.add_parser("validate-result", help="Validate one adapter result JSON document")
    adapter_validate.add_argument("result")
    adapter_validate.add_argument("--phase", choices=["build", "critic", "repair"])
    adapter_validate.add_argument("--json", action="store_true")
    adapter_validate.set_defaults(func=cmd_adapter_validate_result)
    proof = sub.add_parser("proof", help="Validate and explicitly copy live runtime proof artifacts")
    proof_sub = proof.add_subparsers(dest="proof_command", required=True)
    proof_validate = proof_sub.add_parser("validate", help="Fail-closed validation for live Hermes proof")
    proof_validate.add_argument("proof")
    proof_validate.add_argument("--evidence-root", default="", help="recompute doctor/first/resumed hashes from this directory")
    proof_validate.add_argument("--copy-to", default="", help="copy only after successful validation with --evidence-root")
    proof_validate.add_argument("--json", action="store_true")
    proof_validate.set_defaults(func=cmd_proof_validate)
    stack = sub.add_parser("stack", help="Plan or apply immutable project stack pin updates")
    stack_sub = stack.add_subparsers(dest="stack_command", required=True)
    stack_update = stack_sub.add_parser("update", help="Validate and update required_stack_version and stack_ref")
    stack_update.add_argument("repo", nargs="?", default=".")
    stack_update_target = stack_update.add_mutually_exclusive_group(required=True)
    stack_update_target.add_argument("--to", help="immutable target tag vX.Y.Z")
    stack_update_target.add_argument("--latest", action="store_true", help="use the highest annotated immutable vX.Y.Z tag in the stack checkout")
    stack_update.add_argument("--stack-repo", help="stack git checkout used to resolve and inspect the tag; defaults to GO_STACK or the source checkout")
    stack_update.add_argument("--apply", action="store_true", help="apply transaction; default is dry-run")
    stack_update.add_argument("--agent", default="agent")
    stack_update.add_argument("--json", action="store_true")
    stack_update.set_defaults(func=cmd_stack_update)

    onboarding = sub.add_parser('onboarding', help='Internal read-only guided configuration')
    onboarding_sub = onboarding.add_subparsers(dest='onboarding_command', required=True)
    onboarding_plan = onboarding_sub.add_parser('plan')
    onboarding_plan.add_argument('repo', nargs='?', default='.')
    onboarding_plan.add_argument('--answers', default='')
    onboarding_plan.add_argument('--json', action='store_true')
    onboarding_plan.set_defaults(func=cmd_onboarding_plan)

    pairing = sub.add_parser('pairing', help='Internal immutable release pairing checks')
    pairing_sub = pairing.add_subparsers(dest='pairing_operation', required=True)
    for operation in ('inspect', 'baseline', 'template'):
        item = pairing_sub.add_parser(operation)
        item.add_argument('--json', action='store_true')
        if operation in ('inspect', 'baseline'): item.add_argument('--stack-ref', default=STACK_REF)
        if operation in ('baseline', 'template'): item.add_argument('--template', required=True)
        if operation == 'template':
            item.add_argument('--stack-repo', required=True)
            item.add_argument('--render', action='store_true')
        item.set_defaults(func=cmd_pairing)
    agent_check = sub.add_parser("agent-check", help="Report repair-agent adapter availability")
    agent_check.add_argument("--agent", action="append", choices=["codex", "hermes"], default=[])
    agent_check.add_argument("--json", action="store_true")
    agent_check.set_defaults(func=cmd_agent_check)
    doctor = sub.add_parser("doctor", help="Check Linux/WSL prerequisites, agent availability, and stack compatibility")
    doctor.add_argument("repo", nargs="?", default=".")
    doctor.add_argument("--platform", choices=["auto", "linux", "wsl"], default="auto")
    doctor.add_argument("--agent", choices=["codex", "hermes"], default="hermes")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)
    for loop_command, loop_help in (
        ("loop", "Run the stronger go-loop control-handoff contract until blocker"),
        ("go-loop", "Alias for loop; explicit go-loop control-handoff contract"),
    ):
        loop = sub.add_parser(loop_command, help=loop_help)
        loop.add_argument("repo", nargs="?", default=".")
        loop.add_argument("--max-tasks", type=int, default=10)
        loop.add_argument("--summary-chars", type=int, default=900)
        loop.add_argument("--max-minutes", type=int, default=90)
        loop.add_argument("--max-commands", type=int, default=120)
        loop.add_argument("--command-timeout-seconds", type=int, default=900)
        loop.add_argument("--max-attempts", type=int, default=5)
        loop.add_argument("--build-command", default="", help="optional adapter command run before verification; supports safe {repo_shell}, raw {repo}, {task_id}, {attempt}, {strategy}")
        loop.add_argument("--critic-command", default="", help="optional adapter command run after passing verification; non-zero blocks/repairs")
        loop.add_argument("--repair-command", default="", help="optional adapter command run after failed verify/critic before next attempt")
        loop.add_argument("--repair-agent", choices=["codex", "hermes"], default="", help="use a built-in repair adapter command template")
        loop.add_argument("--executor-agent", choices=["auto", "codex", "hermes", "none"], default=executor_agent_default(), help="executor for agent tasks (default: GO_EXECUTOR_AGENT or auto)")
        loop.add_argument("--semantic-critic", action=argparse.BooleanOptionalAction, default=True, help="run built-in semantic critic before finish (default: enabled)")
        loop.add_argument("--followup-on-block", action="store_true", help="create a scoped follow-up task when critic blocks")
        loop.add_argument("--checkpoint-every-tasks", type=int, default=1)
        loop.add_argument("--ship-policy", choices=["none", "local-commit", "push"], default="none")
        loop.add_argument("--allow-push", action="store_true")
        loop.add_argument("--execute", action="store_true", help="execute the lifecycle until done/blocker/budget/safety gate")
        loop.add_argument("--emit-handoff", action="store_true", help="emit a Codex/Hermes-compatible agent handoff JSON")
        loop.add_argument("--agent", default="agent")
        loop.add_argument("--allow-dirty", action="store_true", help="explicitly override dirty/lock preflight gates")
        loop.add_argument("--json", action="store_true")
        loop.set_defaults(func=cmd_loop)
    router = sub.add_parser("router", help="Route one public Go command to internal repo-local discovery/planning/execution primitives")
    router.add_argument("repo", nargs="?", default=".")
    router.add_argument("--command", default="go")
    router.add_argument("--intent", default="")
    router.add_argument("--max-tasks", type=int, default=3)
    router.add_argument("--json", action="store_true")
    router.set_defaults(func=cmd_router)
    status = sub.add_parser("status", help="Summarize route, project, task counts, next work, and dirty state")
    status.add_argument("repo", nargs="?", default=".")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)
    index = sub.add_parser("index", help="Build and query the local derived repository graph")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    index_build = index_sub.add_parser("build", help="Build the deterministic local repository graph")
    index_build.add_argument("repo", nargs="?", default=".")
    index_build.add_argument("--json", action="store_true")
    index_build.set_defaults(func=cmd_index_build)
    index_status_parser = index_sub.add_parser("status", help="Check whether the local repository graph is fresh")
    index_status_parser.add_argument("repo", nargs="?", default=".")
    index_status_parser.add_argument("--json", action="store_true")
    index_status_parser.set_defaults(func=cmd_index_status)
    index_query = index_sub.add_parser("query", help="Select a bounded lexical subgraph")
    index_query.add_argument("repo", nargs="?", default=".")
    index_query.add_argument("query")
    index_query.add_argument("--limit", type=int, default=20)
    index_query.add_argument("--json", action="store_true")
    index_query.set_defaults(func=cmd_index_query)
    index_blast = index_sub.add_parser("blast", help="Trace changed files through reverse dependencies and task scope")
    index_blast.add_argument("repo", nargs="?", default=".")
    index_blast.add_argument("--base", required=True)
    index_blast.add_argument("--head", default="WORKTREE")
    index_blast.add_argument("--task-id")
    index_blast.add_argument("--max-nodes", type=int, default=200)
    index_blast.add_argument("--json", action="store_true")
    index_blast.set_defaults(func=cmd_index_blast)
    template_check = sub.add_parser("template-check", help="Validate a go-project-template checkout against this stack")
    template_check.add_argument("template_repo", nargs="?", default="../go-project-template")
    template_check.add_argument("--json", action="store_true")
    template_check.set_defaults(func=cmd_template_check)
    recommendation = sub.add_parser("recommendation", help="Persist or inspect a compact repo-local recommendation handoff")
    recommendation_sub = recommendation.add_subparsers(dest="recommendation_command", required=True)
    recommendation_create = recommendation_sub.add_parser("create", help="Validate and persist the latest execution brief as a pending recommendation")
    recommendation_create.add_argument("repo")
    recommendation_create.add_argument("--brief", required=True)
    recommendation_create.add_argument("--authority", choices=["advice", "execute"], default="advice")
    recommendation_create.add_argument("--authority-source", default="question")
    recommendation_create.add_argument("--read-only", action="store_true", help="validate the brief but do not mutate .go state")
    recommendation_create.add_argument("--replace", action="store_true", help="replace an existing pending recommendation")
    recommendation_create.add_argument("--agent", default="agent")
    recommendation_create.add_argument("--json", action="store_true")
    recommendation_create.set_defaults(func=cmd_recommendation_create)
    recommendation_status = recommendation_sub.add_parser("status", help="Show the current pending recommendation")
    recommendation_status.add_argument("repo", nargs="?", default=".")
    recommendation_status.add_argument("--json", action="store_true")
    recommendation_status.set_defaults(func=cmd_recommendation_status)
    delivery = sub.add_parser("delivery", help="Build shareable stakeholder delivery artifacts from repo-local .go state")
    delivery_sub = delivery.add_subparsers(dest="delivery_command", required=True)
    delivery_build = delivery_sub.add_parser("build", help="Generate standalone HTML plus a delivery manifest for one epic")
    delivery_build.add_argument("repo", nargs="?", default=".")
    delivery_build.add_argument("--epic", required=True)
    delivery_build.add_argument("--delivery-id", default="")
    delivery_build.add_argument("--version", type=int, default=1)
    delivery_build.add_argument("--status", choices=["draft", "released", "superseded"], default="draft")
    delivery_build.add_argument("--disclosure", choices=["public", "link-private", "restricted"], default="restricted")
    delivery_build.add_argument("--supersedes", default="")
    delivery_build.add_argument("--summary", default="")
    delivery_build.add_argument("--delivered", action="append", default=[])
    delivery_build.add_argument("--excluded", action="append", default=[])
    delivery_build.add_argument("--limitation", action="append", default=[])
    delivery_build.add_argument("--next-step", action="append", default=[])
    delivery_build.set_defaults(func=cmd_delivery_build)
    delivery_publish = delivery_sub.add_parser("publish", help="Publish a built delivery through an explicit adapter")
    delivery_publish.add_argument("repo", nargs="?", default=".")
    delivery_publish.add_argument("--delivery-id", required=True)
    delivery_publish.set_defaults(func=cmd_delivery_publish)
    task = sub.add_parser("task", help="Author repo-local tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_create = task_sub.add_parser("create", help="Create an open repo-local task")
    task_create.add_argument("repo")
    task_create.add_argument("--id")
    task_create.add_argument("--summary", required=True)
    task_create.add_argument("--description", default="")
    task_create.add_argument("--feature", default="", help="epic_id.feature_id")
    task_create.add_argument("--epic", default="", help="epic_id; attaches the task directly to an epic")
    task_create.add_argument("--read", action="append", default=[])
    task_create.add_argument("--modify", action="append", default=[])
    task_create.add_argument("--execution-mode", choices=["mechanical", "agent"], default="mechanical")
    task_create.add_argument("--shareable-delivery", choices=["auto", "required", "none"], default="auto")
    task_create.add_argument("--acceptance", action="append", default=[])
    task_create.add_argument("--verification", action="append", default=[])
    task_create.add_argument("--execution-contract", default="", help="JSON file: opt-in execution contract overrides")
    task_create.add_argument("--dependencies", default="", help="JSON array file of explicit dependency references")
    task_create.add_argument("--repository-context", default="", help="JSON file: stable repository nodes, discovery queries and context budget")
    task_create.set_defaults(func=cmd_task_create)
    task_outcome = task_sub.add_parser("outcome", help="Record one requested-outcome disposition with evidence")
    task_outcome.add_argument("repo")
    task_outcome.add_argument("--task-id", required=True)
    task_outcome.add_argument("--outcome", required=True, help="requested outcome id such as R1")
    task_outcome.add_argument("--status", choices=sorted(OUTCOME_TERMINAL_STATUSES), required=True)
    task_outcome.add_argument("--evidence", required=True)
    task_outcome.add_argument("--agent", default="agent")
    task_outcome.set_defaults(func=cmd_task_outcome)
    task_review = task_sub.add_parser("review", help="Approve completed work or send it back as needs_fix")
    task_review.add_argument("repo")
    task_review.add_argument("--task-id", required=True)
    task_review.add_argument("--status", choices=["approved", "needs_fix"], required=True)
    task_review.add_argument("--evidence", required=True)
    task_review.add_argument("--agent", default="agent")
    task_review.add_argument("--owner", default="", help="owner/assignee for needs_fix return path")
    task_review.set_defaults(func=cmd_task_review)
    task_handoff = task_sub.add_parser("handoff", help="Transfer one idle active task claim without modifying worker files")
    task_handoff.add_argument("repo")
    task_handoff.add_argument("--task-id", required=True)
    task_handoff.add_argument("--expected-owner", required=True)
    task_handoff.add_argument("--new-owner", required=True)
    task_handoff.add_argument("--old-run-id", default="")
    task_handoff.add_argument("--new-run-id", default="")
    task_handoff.add_argument("--reason", required=True)
    task_handoff.add_argument("--confirm-owner-stopped", action="store_true")
    task_handoff.add_argument("--confirm-same-host", action="store_true")
    task_handoff.add_argument("--legacy-unmanaged", action="store_true")
    task_handoff.add_argument("--handoff-id", default="")
    task_handoff.add_argument("--json", action="store_true")
    task_handoff.set_defaults(func=cmd_task_handoff)
    epic = sub.add_parser("epic", help="Author repo-local epics")
    epic_sub = epic.add_subparsers(dest="epic_command", required=True)
    epic_create = epic_sub.add_parser("create", help="Create an epic in hierarchy.json")
    epic_create.add_argument("repo")
    epic_create.add_argument("--id")
    epic_create.add_argument("--title", required=True)
    epic_create.add_argument("--description", default="")
    epic_create.set_defaults(func=cmd_epic_create)
    decision = sub.add_parser("decision", help="Record ADR-lite repo-local decisions")
    decision_sub = decision.add_subparsers(dest="decision_command", required=True)
    decision_create = decision_sub.add_parser("create", help="Append a decision.recorded event")
    decision_create.add_argument("repo")
    decision_create.add_argument("--id")
    decision_create.add_argument("--title", required=True)
    decision_create.add_argument("--status", default="accepted", choices=["proposed", "accepted", "superseded", "rejected"])
    decision_create.add_argument("--context", required=True)
    decision_create.add_argument("--decision", required=True)
    decision_create.add_argument("--consequence", action="append", default=[])
    decision_create.add_argument("--agent", default="agent")
    decision_create.add_argument("--task-id", default="project-decision")
    decision_create.set_defaults(func=cmd_decision_create)
    architecture = sub.add_parser("architecture", help="Inspect and operate the optional conditional architecture lane")
    architecture_sub = architecture.add_subparsers(dest="architecture_command", required=True)
    architecture_validate = architecture_sub.add_parser("validate", help="Validate architecture briefs and append-only events")
    architecture_validate.add_argument("repo", nargs="?", default=".")
    architecture_validate.add_argument("--json", action="store_true")
    architecture_validate.set_defaults(func=cmd_architecture_validate)
    architecture_readback = architecture_sub.add_parser("readback", help="Resolve applicable architecture for a project or task")
    architecture_readback.add_argument("repo", nargs="?", default=".")
    architecture_readback.add_argument("--task-id", default="")
    architecture_readback.add_argument("--json", action="store_true")
    architecture_readback.set_defaults(func=cmd_architecture_readback)
    architecture_status = architecture_sub.add_parser("status", help="Summarize briefs, deviations, waivers, and scopes")
    architecture_status.add_argument("repo", nargs="?", default=".")
    architecture_status.add_argument("--json", action="store_true")
    architecture_status.set_defaults(func=cmd_architecture_status)
    architecture_classify = architecture_sub.add_parser("classify", help="Calculate deterministic minimum impact and optionally write task metadata")
    architecture_classify.add_argument("repo", nargs="?", default=".")
    architecture_classify.add_argument("--task-id", required=True)
    architecture_classify.add_argument("--impact", choices=["none", "local", "material", "foundational"])
    architecture_classify.add_argument("--scope-ref", action="append", default=[])
    architecture_classify.add_argument("--concern", action="append", default=[])
    architecture_classify.add_argument("--decision-id", action="append", default=[])
    architecture_classify.add_argument("--conformance-required", action="store_true")
    architecture_classify.add_argument("--human-gate", choices=["none", "decision", "risk_acceptance"])
    architecture_classify.add_argument("--actor", default="agent")
    architecture_classify.add_argument("--write", action="store_true")
    architecture_classify.add_argument("--json", action="store_true")
    architecture_classify.set_defaults(func=cmd_architecture_classify)
    architecture_conformance = architecture_sub.add_parser("conformance", help="Record task architecture conformance evidence")
    architecture_conformance.add_argument("repo", nargs="?", default=".")
    architecture_conformance.add_argument("--task-id", required=True)
    architecture_conformance.add_argument("--scope-id", required=True)
    architecture_conformance.add_argument("--status", choices=["passed", "passed_with_waiver", "deviation", "blocked"], required=True)
    architecture_conformance.add_argument("--principle-check", action="append", default=[])
    architecture_conformance.add_argument("--decision-check", action="append", default=[])
    architecture_conformance.add_argument("--quality-attribute-check", action="append", default=[])
    architecture_conformance.add_argument("--evidence-ref", action="append", default=[])
    architecture_conformance.add_argument("--waiver-id", action="append", default=[])
    architecture_conformance.add_argument("--actor", default="architecture-critic")
    architecture_conformance.set_defaults(func=cmd_architecture_conformance)
    architecture_review = architecture_sub.add_parser("review", help="Record an architecture review or explicit human approval")
    architecture_review.add_argument("repo", nargs="?", default=".")
    architecture_review.add_argument("--task-id", required=True)
    architecture_review.add_argument("--scope-id", required=True)
    architecture_review.add_argument("--status", choices=["approved", "rejected"], required=True)
    architecture_review.add_argument("--human", action="store_true")
    architecture_review.add_argument("--evidence-ref", required=True)
    architecture_review.add_argument("--actor", required=True)
    architecture_review.set_defaults(func=cmd_architecture_review)
    architecture_waiver = architecture_sub.add_parser("waiver", help="Grant a reasoned, time-bounded architecture waiver")
    architecture_waiver.add_argument("repo", nargs="?", default=".")
    architecture_waiver.add_argument("--id", required=True)
    architecture_waiver.add_argument("--task-id", required=True)
    architecture_waiver.add_argument("--scope-id", required=True)
    architecture_waiver.add_argument("--reason", required=True)
    architecture_waiver.add_argument("--accepted-risk", required=True)
    architecture_waiver.add_argument("--expires-at", required=True)
    architecture_waiver.add_argument("--deviation-id", action="append", default=[])
    architecture_waiver.add_argument("--actor", required=True)
    architecture_waiver.set_defaults(func=cmd_architecture_waiver)
    architecture_waiver_close = architecture_sub.add_parser("waiver-close", help="Expire or revoke an architecture waiver without rewriting its grant")
    architecture_waiver_close.add_argument("repo", nargs="?", default=".")
    architecture_waiver_close.add_argument("--id", required=True)
    architecture_waiver_close.add_argument("--task-id", required=True)
    architecture_waiver_close.add_argument("--scope-id", required=True)
    architecture_waiver_close.add_argument("--status", choices=["expired", "revoked"], required=True)
    architecture_waiver_close.add_argument("--reason", required=True)
    architecture_waiver_close.add_argument("--actor", required=True)
    architecture_waiver_close.set_defaults(func=cmd_architecture_waiver_close)
    agents = sub.add_parser("agents", help="Inspect or safely synchronize the root AGENTS.md .go gateway")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    agents_sync = agents_sub.add_parser("sync", help="Plan or apply the bounded root AGENTS.md gateway")
    agents_sync.add_argument("repo", nargs="?", default=".")
    agents_sync.add_argument("--apply", action="store_true", help="write the bounded gateway; default is dry-run")
    agents_sync.add_argument("--json", action="store_true")
    agents_sync.set_defaults(func=cmd_agents_sync)
    migrate = sub.add_parser("migrate", help="Plan or explicitly apply versioned .go contract migrations")
    migrate.add_argument("repo", nargs="?", default=".")
    migrate.add_argument("--apply", action="store_true", help="write the proposed migration; default is dry-run")
    migrate.add_argument("--agent", default="agent")
    migrate.add_argument("--json", action="store_true")
    migrate.add_argument('--lifecycle', action='store_true', help='Preview explicit lifecycle policy adoption, separate from schema/pin updates')
    migrate.add_argument('--config', default='', help='Explicit lifecycle settings JSON; never grants execution authority')
    migrate.add_argument('--resume', default='', help='Resume a preserved lifecycle journal; --apply writes')
    migrate.add_argument('--rollback', default='', help='Restore a preserved lifecycle journal; --apply writes')
    migrate.set_defaults(func=cmd_migrate)
    init = sub.add_parser("init", help="Initialize .go fixture state in a repo")
    init.add_argument("repo", nargs="?", default=".")
    init.add_argument("--force", action="store_true")
    init.add_argument('--lifecycle-settings', default='', help='Explicit lifecycle settings for a new project; no execution authority')
    init.set_defaults(func=cmd_init)
    validate = sub.add_parser("validate", help="Validate repo-local .go state")
    validate.add_argument("repo", nargs="?", default=".")
    validate.add_argument('--campaign', help='Also validate an explicit bounded campaign contract (read-only)')
    validate.add_argument('--previous-campaign', help='Previous immutable revision required for campaign revision > 1')
    validate.add_argument('--json', action='store_true')
    validate.set_defaults(func=cmd_validate)
    campaign_parser = sub.add_parser("campaign", help="Inspect bounded campaign completion evidence")
    campaign_sub = campaign_parser.add_subparsers(dest="campaign_command", required=True)
    resume_context=campaign_sub.add_parser('context',help='Compose current authoritative task resume context')
    resume_context.add_argument('repo',nargs='?',default='.')
    resume_context.add_argument('--task-id',required=True)
    resume_context.add_argument('--agent',default=None)
    resume_context.add_argument('--json',action='store_true')
    resume_context.set_defaults(func=cmd_resume_context)
    campaign_block = campaign_sub.add_parser("block", help="Prepare one joint delivery using existing task IDs")
    campaign_block.add_argument("repo", nargs="?", default=".")
    campaign_block.add_argument("--coordinator", required=True)
    campaign_block.add_argument("--member", action="append", required=True)
    campaign_block.add_argument("--reason", required=True)
    campaign_block.add_argument("--agent", default="agent")
    campaign_block.set_defaults(func=cmd_campaign_block)
    for change_kind in ('repair','amend','progress'):
        change=campaign_sub.add_parser(change_kind,help='Record a bounded evidence-backed campaign change')
        change.add_argument('repo',nargs='?',default='.')
        change.add_argument('--campaign',required=True)
        change.add_argument('--request',required=True,help='JSON proposal with reason, source evidence and existing identities')
        change.add_argument('--agent',default='agent')
        change.set_defaults(func=cmd_campaign_change,change_kind=change_kind)
    campaign_audit = campaign_sub.add_parser("audit", help="Audit adopted outcomes and write a compact handoff")
    campaign_audit.add_argument("repo", nargs="?", default=".")
    campaign_audit.add_argument("--contract", required=True)
    campaign_audit.add_argument("--previous-campaign", default="")
    campaign_audit.add_argument("--read-only", action="store_true", help="compute without writing audit/handoff artifacts")
    campaign_audit.add_argument("--json", action="store_true")
    campaign_audit.set_defaults(func=cmd_campaign_audit)
    nxt = sub.add_parser("next", help="Print the first claimable open task")
    nxt.add_argument("repo", nargs="?", default=".")
    nxt.set_defaults(func=cmd_next)
    claim = sub.add_parser("claim", help="Claim one repo-local task")
    claim.add_argument("task_id")
    claim.add_argument("--repo", default=".")
    claim.add_argument("--agent", default="agent")
    claim.add_argument("--allow-dirty", action="store_true")
    claim.set_defaults(func=cmd_claim)
    finish = sub.add_parser("finish", help="Finish one repo-local active task")
    finish.add_argument("task_id")
    finish.add_argument("--repo", default=".")
    finish.add_argument("--agent", default="agent")
    finish.add_argument("--evidence", required=True)
    finish.set_defaults(func=cmd_finish)
    dirty = sub.add_parser("dirty-check", help="Classify dirty git state against owned paths")
    dirty.add_argument("repo", nargs="?", default=".")
    dirty.add_argument("--owned", action="append", default=[])
    dirty.add_argument("--fail-on-blocking", action="store_true")
    dirty.set_defaults(func=cmd_dirty_check)
    readback = sub.add_parser("readback", help="Summarize a repo from .go state only")
    readback.add_argument("repo", nargs="?", default=".")
    readback.set_defaults(func=cmd_readback)
    route = sub.add_parser("route", help="Classify a target repo as repo-local .go or fail closed on a missing local contract")
    route.add_argument("repo", nargs="?", default=".")
    route.add_argument("--json", action="store_true")
    route.set_defaults(func=cmd_route)
    bundle = sub.add_parser("bundle", help="Export/import compact repo-local .go bundles")
    bundle_sub = bundle.add_subparsers(dest="bundle_command", required=True)
    bundle_export = bundle_sub.add_parser("export", help="Export a compact .go readback/task/history bundle")
    bundle_export.add_argument("repo", nargs="?", default=".")
    bundle_export.add_argument("--output", default="")
    bundle_export.add_argument("--include-done", action="store_true", help="Include done task summaries/evidence instead of counts only")
    bundle_export.add_argument("--max-events", type=int, default=20, help="Maximum recent events per JSONL stream")
    bundle_export.set_defaults(func=cmd_bundle_export)
    bundle_import = bundle_sub.add_parser("import", help="Validate and optionally write an import/reconcile artifact")
    bundle_import.add_argument("repo")
    bundle_import.add_argument("bundle")
    bundle_import.add_argument("--write", action="store_true", help="Write .go/imports/<bundle_id>.json and append a decision event")
    bundle_import.add_argument("--force", action="store_true", help="Replace an existing import artifact with the same bundle id")
    bundle_import.add_argument("--agent", default="agent")
    bundle_import.add_argument("--task-id", default="bundle-import")
    bundle_import.set_defaults(func=cmd_bundle_import)
    workspace = sub.add_parser("workspace", help="Manage explicit task workspaces")
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_create = workspace_sub.add_parser("create")
    workspace_create.add_argument("repo")
    for field in ("task-id", "owner", "run-id", "path", "branch", "base-branch", "base-commit"):
        workspace_create.add_argument("--" + field, required=True)
    workspace_create.add_argument("--json", action="store_true")
    workspace_create.set_defaults(func=cmd_workspace_create)
    for operation in ('status', 'stage', 'integration-check', 'record-integration', 'cleanup', 'reconcile', 'rebind'):
        workspace_op = workspace_sub.add_parser(operation)
        workspace_op.add_argument('repo')
        for field in ('task-id', 'owner', 'run-id'):
            workspace_op.add_argument('--' + field, required=True)
        if operation == 'record-integration': workspace_op.add_argument('--integrated-commit', required=True)
        if operation == 'rebind':
            workspace_op.add_argument('--new-owner', required=True)
            workspace_op.add_argument('--new-run-id', required=True)
        workspace_op.add_argument('--json', action='store_true')
        workspace_op.set_defaults(func=cmd_workspace_operation, workspace_operation=operation)
    context_parser = sub.add_parser('context', help='Verify durable worker context')
    context_sub = context_parser.add_subparsers(dest='context_command', required=True)
    context_verify = context_sub.add_parser('verify')
    context_verify.add_argument('repo')
    for field in ('task-id', 'snapshot', 'sha256'): context_verify.add_argument('--' + field, required=True)
    context_verify.set_defaults(func=cmd_context_verify)
    for command_parser in (go, auto, *[sub.choices[name] for name in ('loop', 'go-loop')]):
        command_parser.add_argument('--allow-deploy', action='store_true')
        command_parser.add_argument('--campaign', default='', help='explicit bounded campaign contract')
        command_parser.add_argument('--previous-campaign', default='', help='previous immutable campaign revision')
        command_parser.add_argument('--campaign-workspace-root', default='', help='outside-repository root for campaign task worktrees')
        command_parser.add_argument(
            '--campaign-action',
            choices=['run', 'resume', 'pause', 'drain', 'cancel'],
            default='run',
            help='durable campaign controller action; control actions stop at a checkpoint boundary',
        )
        for field in ('task-id', 'workspace-path', 'workspace-branch', 'base-branch', 'base-commit', 'run-id'):
            command_parser.add_argument('--' + field, default='')
    managed_parser = sub.add_parser('managed', help='Internal managed-run process boundary')
    managed_sub = managed_parser.add_subparsers(dest='managed_operation', required=True)
    enter = managed_sub.add_parser('worker-enter')
    enter.add_argument('repo')
    for field in ('task-id', 'run-id', 'nonce', 'owner'): enter.add_argument('--' + field, required=True)
    enter.add_argument('--channel', choices=['managed', 'completion', 'publication'], default='managed')
    enter.set_defaults(func=cmd_managed_worker_enter)
    relocate = managed_sub.add_parser('relocate')
    relocate.add_argument('repo')
    for field in ('task-id', 'run-id', 'owner', 'old-control', 'old-workspace', 'workspace'):
        relocate.add_argument('--' + field, required=True)
    relocate.set_defaults(func=cmd_managed_relocate)
    release_parser = sub.add_parser('release', help='Explicit configured task publication')
    release_sub = release_parser.add_subparsers(dest='release_operation', required=True)
    for operation in ('prepare', 'publish', 'status', 'reconcile', 'rebind-verification'):
        item = release_sub.add_parser(operation)
        item.add_argument('repo')
        for field in ('task-id', 'owner', 'run-id'): item.add_argument('--' + field, required=True)
        item.add_argument('--json', action='store_true')
        if operation == 'prepare':
            item.add_argument('--ship-policy', choices=['none', 'push'], default='none')
            item.add_argument('--allow-push', action='store_true')
            item.add_argument('--allow-deploy', action='store_true')
        item.set_defaults(func=cmd_release)
    completion_parser = sub.add_parser('completion', help='Capture and inspect content-bound lifecycle proof')
    completion_sub = completion_parser.add_subparsers(dest='completion_operation', required=True)
    for operation in ('verify', 'critic', 'readback', 'status'):
        item = completion_sub.add_parser(operation)
        item.add_argument('repo')
        item.add_argument('--task-id', required=True)
        item.add_argument('--agent', default='agent')
        item.add_argument('--workspace', default='')
        item.add_argument('--json', action='store_true')
        if operation == 'verify': item.add_argument('--timeout-seconds', type=int, default=900)
        if operation == 'critic': item.add_argument('--review-file', required=True)
        if operation == 'readback': item.add_argument('--tag', required=True)
        item.set_defaults(func=cmd_completion_status if operation == 'status' else cmd_completion)
    return parser


def cmd_agent_check(args: argparse.Namespace) -> int:
    requested_agents = args.agent
    agents = requested_agents or ["codex", "hermes"]
    payload = {"schema": "go-workflow.agent-check.v1", "agents": [repair_agent_available(agent) for agent in agents]}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for item in payload["agents"]:
            if not item["available"]:
                status = "missing"
            elif not item["compatible"]:
                status = "incompatible"
            else:
                status = "available"
            detail = item.get("prompt_flag") or item.get("path") or ""
            print(f"{item['agent']}: {status} {detail}".rstrip())
    if not requested_agents:
        return 0
    return 0 if all(item["compatible"] for item in payload["agents"]) else 1


def cmd_version(args: argparse.Namespace) -> int:
    payload = {
        "schema": "go-workflow.runtime-version.v1",
        "stack_version": STACK_VERSION,
        "stack_ref": STACK_REF,
        "contract_version": CURRENT_CONTRACT_VERSION,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(STACK_VERSION)
    return 0


def cmd_pairing(args):
    from .release_pairings import load_manifest, pairing_for, verify_baseline, current_template, render_metadata, PairingError
    try:
        data = load_manifest(expected_ref=STACK_REF)
        if args.pairing_operation == 'inspect': result = pairing_for(data, args.stack_ref)
        elif args.pairing_operation == 'baseline': result = verify_baseline(data, args.stack_ref, Path(args.template))
        else:
            result = current_template(data, Path(args.template), Path(args.stack_repo), check_docs=not args.render)
            if args.render:
                print(render_metadata(result))
                return 0
    except (PairingError, OSError) as exc:
        raise RepoLocalError(str(exc)) from exc
    print(json.dumps(result, indent=2))
    return 0


def cmd_onboarding_plan(args):
    from .onboarding import plan_onboarding, OnboardingError
    try:
        plan = plan_onboarding(Path(args.repo), load_json(Path(args.answers)) if args.answers else None)
    except (OnboardingError, OSError) as exc:
        raise RepoLocalError(str(exc)) from exc
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0


def cmd_stack_update(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    configured_stack = args.stack_repo or os.environ.get("GO_STACK")
    if configured_stack:
        stack_repo = Path(configured_stack).expanduser().resolve()
    elif is_git_checkout(STACK_ROOT):
        stack_repo = STACK_ROOT
    else:
        raise RepoLocalError(
            "stack update requires a go-workflow-stack Git checkout; "
            "set GO_STACK or pass --stack-repo"
        )
    try:
        target_ref = latest_stack_ref(stack_repo) if args.latest else args.to
        plan = plan_stack_update(repo, stack_repo, target_ref)
    except StackUpdateError as exc:
        raise RepoLocalError(str(exc)) from exc
    result = plan
    if args.apply:
        result = apply_stack_update(repo, plan)
        if result["mode"] != "noop":
            append_jsonl(
                go_root(repo) / "runs" / "events.jsonl",
                event("stack-update", "run.checked", args.agent, {
                    "action": "stack.updated",
                    "from_ref": result.get("from_ref"),
                    "to_ref": result["to_ref"],
                    "resolved_commit": result["resolved_commit"],
                    "rollback_record": result["rollback_record"],
                }),
            )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"stack update {result['mode']}: {result.get('from_ref')} -> {result['to_ref']}")
        print(f"commit: {result['resolved_commit']}")
        for finding in result["compatibility"]["errors"]:
            print(f"compatibility: {finding}", file=sys.stderr)
        if result.get("rollback_record"):
            print(f"rollback: {result['rollback_record']}")
    return 0 if result['compatibility']['status'] == 'passed' else 1


def cmd_adapter_validate_result(args: argparse.Namespace) -> int:
    data = load_json(Path(args.result))
    errors = validate_adapter_result(data, expected_phase=args.phase)
    payload = {
        "schema": "go-workflow.agent-adapter-validation.v1",
        "valid": not errors,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("valid adapter result" if not errors else "invalid adapter result: " + "; ".join(errors))
    return 0 if not errors else 1


def cmd_proof_validate(args: argparse.Namespace) -> int:
    source = Path(args.proof).resolve()
    data = load_json(source)
    errors = validate_live_hermes_proof(data)
    if args.copy_to and not args.evidence_root:
        errors.append("--copy-to requires --evidence-root so raw result hashes and semantics are verified")
    if args.evidence_root and not errors:
        errors.extend(verify_live_hermes_evidence(data, Path(args.evidence_root).resolve()))
    copied_to = None
    if not errors and args.copy_to:
        target = Path(args.copy_to).resolve()
        atomic_json(target, data)
        copied_to = str(target)
    payload = {
        "schema": "go-workflow.live-proof-validation.v1",
        "proof": str(source),
        "valid": not errors,
        "errors": errors,
        "evidence_root": str(Path(args.evidence_root).resolve()) if args.evidence_root else None,
        "copied_to": copied_to,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("valid live Hermes proof" if not errors else "invalid live Hermes proof: " + "; ".join(errors))
        if copied_to:
            print(f"copied: {copied_to}")
    return 0 if not errors else 1


def semantic_version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def cmd_doctor(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    # A source checkout used directly as the CLI runtime must match the pinned
    # immutable release. Tests may set STACK_ROOT to an isolated fixture; this
    # also verifies the stack source selected by the runtime, not the caller's
    # cwd or an unrelated installed package.
    stack_source = STACK_ROOT
    project_path = go_root(repo) / "project.json"
    project = load_json(project_path) if project_path.is_file() else {}
    required_version = str(project.get("required_stack_version") or "0.0.0")
    required_ref = str(project.get("stack_ref") or "")
    compatible = semantic_version_tuple(STACK_VERSION) >= semantic_version_tuple(required_version)
    identity = resolve_runtime_identity(stack_source, required_ref, expected_version=STACK_VERSION)
    git_head = identity["git_head"]
    pinned_commit = identity["pinned_commit"]
    exact_ref = identity["exact_ref"]
    development_override = os.environ.get("GO_STACK_ALLOW_DEV") == "1" and not exact_ref
    ref_compatible = exact_ref or development_override
    prerequisites: list[dict[str, Any]] = [
        {"name": "python", "available": sys.version_info >= (3, 11), "version": ".".join(str(part) for part in sys.version_info[:3]), "path": sys.executable},
    ]
    for name in ("git", "bash", "make", "uv"):
        path = shutil.which(name)
        prerequisites.append({"name": name, "available": bool(path), "path": path})
    agent_availability = repair_agent_available(args.agent)
    agent = {
        "name": args.agent,
        "available": agent_availability["available"],
        "compatible": agent_availability["compatible"],
        "path": agent_availability["path"],
        "prompt_flag": agent_availability["prompt_flag"],
    }
    contract_errors = validate_repo(repo) if project_path.is_file() else [".go/project.json is missing"]
    actions: list[str] = []
    for item in prerequisites:
        if not item["available"]:
            actions.append(f"install {item['name']}")
    if not agent["available"]:
        actions.append(f"install {args.agent} and make it available on PATH")
    elif not agent["compatible"]:
        actions.append(f"install a compatible {args.agent} CLI with a supported prompt interface (-z or -p)")
    if not compatible:
        actions.append(f"update go-workflow-stack to at least {required_version}")
    if not ref_compatible:
        actions.append(f"checkout the pinned go-workflow-stack ref {required_ref}")
    if contract_errors:
        actions.append("repair the .go project contract")
    ready = not actions
    payload = {
        "schema": "go-workflow.doctor.v1",
        "repo": str(repo),
        "platform": detected_platform(args.platform),
        "prerequisites": prerequisites,
        "agent": agent,
        "stack": {
            "version": STACK_VERSION,
            "ref": STACK_REF,
            "git_head": git_head or None,
            "pinned_commit": pinned_commit or None,
            "identity_source": identity["source"],
            "provenance_requested_ref": identity["requested_ref"],
            "required_version": required_version,
            "required_ref": required_ref or None,
            "exact_ref": exact_ref,
            "development_override": development_override,
            "compatible": compatible and ref_compatible,
        },
        "contract": {"valid": not contract_errors, "errors": contract_errors},
        "ready": ready,
        "actions": actions,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"platform: {payload['platform']['kind']}")
        print(f"stack: {STACK_VERSION} (required {required_version})")
        if not agent["available"]:
            agent_status = "missing"
        elif not agent["compatible"]:
            agent_status = "incompatible"
        else:
            agent_status = f"available, prompt {agent['prompt_flag']}" if agent["prompt_flag"] else "available"
        print(f"agent: {args.agent} ({agent_status})")
        print("ready" if ready else "not ready: " + "; ".join(actions))
    return 0 if ready else 1


def cmd_workspace_create(args: argparse.Namespace) -> int:
    record = create_workspace(Path(args.repo), args.task_id, args.owner, args.run_id,
                              Path(args.path), args.branch, args.base_branch, args.base_commit)
    print(json.dumps(record, indent=2) if args.json else record["path"])
    return 0


def cmd_workspace_operation(args: argparse.Namespace) -> int:
    params = (Path(args.repo), args.task_id, args.owner, args.run_id)
    operation = args.workspace_operation
    if operation == 'status':
        result = owned_record(*params, active=False)
        if result['state'] != 'cleaned': verify_workspace(result)
    elif operation == 'stage': result = stage_workspace(*params)
    elif operation == 'cleanup': result = cleanup_workspace(*params)
    elif operation == 'record-integration': result = record_integration(*params, args.integrated_commit)
    elif operation == 'reconcile': result = reconcile_workspace(*params)
    elif operation == 'rebind': result = rebind_workspace(*params, args.new_owner, args.new_run_id)
    else:
        with integration_slot(*params) as result:
            result = {**result, 'preview_only': True, 'lock_held_after_return': False}
    print(json.dumps(result, indent=2))
    return 0


def cmd_context_verify(args: argparse.Namespace) -> int:
    snapshot = verify_snapshot(Path(args.repo), args.task_id, args.snapshot, args.sha256)
    print(json.dumps({'verified': True, 'task_id': snapshot['task_id'], 'snapshot_id': snapshot['snapshot_id']}))
    return 0


def cmd_managed_worker_enter(args: argparse.Namespace) -> int:
    worker_enter(Path(args.repo).resolve(), args.task_id, args.run_id, args.nonce, args.owner, args.channel)
    return 0


def cmd_managed_relocate(args: argparse.Namespace) -> int:
    state = relocate_run(Path(args.repo), args.task_id, args.owner, args.run_id,
                         Path(args.old_control), Path(args.old_workspace), Path(args.workspace))
    print(json.dumps(state, indent=2))
    return 0


def cmd_release(args):
    from go_workflow.release import (prepare_release, publish_release, reconcile_prepared_release,
                                     rebind_prepared_verification, _load)
    repo = Path(args.repo).resolve()
    if args.release_operation == 'prepare':
        result = prepare_release(repo, args.task_id, args.owner, args.run_id,
            ship_policy=args.ship_policy, allow_push=args.allow_push, allow_deploy=args.allow_deploy)
    elif args.release_operation == 'publish':
        result = publish_release(repo, args.task_id, args.owner, args.run_id)
    elif args.release_operation == 'reconcile':
        result = reconcile_prepared_release(repo, args.task_id, args.owner, args.run_id)
    elif args.release_operation == 'rebind-verification':
        result = rebind_prepared_verification(repo, args.task_id, args.owner, args.run_id)
    else:
        result = _load(repo, active_task(repo, args.task_id), args.owner, args.run_id)
    print(json.dumps(result, indent=2))
    return 0


def completion_target(args):
    control = Path(args.repo).resolve()
    if args.workspace:
        workspace = Path(args.workspace).resolve()
        record = registered_workspace(workspace)
        if (not record or record['control_repo'] != str(control) or record['task_id'] != args.task_id
                or record['owner'] != args.agent):
            raise CompletionError('Completion workspace does not match the owned control/task binding')
        return workspace
    return control


def cmd_completion(args: argparse.Namespace) -> int:
    from go_workflow.completion import capture_verification, record_critic
    from go_workflow.shipping import capture_release
    repo = completion_target(args)
    if args.completion_operation == 'verify':
        artifact, ref = capture_verification(repo, args.task_id, args.agent, timeout_seconds=args.timeout_seconds)
        result = {'status': artifact['status'], 'evidence': ref, 'binding': {key: artifact[key] for key in ('task_id', 'project', 'contract_digest', 'revision', 'content_digest')}}
    elif args.completion_operation == 'critic':
        ref = record_critic(repo, args.task_id, args.agent, load_json(Path(args.review_file)))
        result = {'status': 'passed', 'evidence': ref}
    else:
        proof, ref = capture_release(repo, args.task_id, args.agent, args.tag)
        result = {'status': proof['status'], 'evidence': ref, 'commit': proof['commit'], 'tag': proof['tag']}
    print(json.dumps(result, indent=2))
    return 0 if result['status'] in {'passed', 'verified'} else 1


def cmd_completion_status(args: argparse.Namespace) -> int:
    from go_workflow.completion import completion_findings
    repo = completion_target(args)
    task = active_task(go_root(repo).parent, args.task_id)
    findings = completion_findings(repo, task, current=task['status'] != 'done')
    print(json.dumps({'task_id': task['id'], 'eligible': not findings, 'findings': findings}, indent=2))
    return 1 if findings else 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        guard_workspace_command(args)
        return int(args.func(args))
    except (RepoLocalError, RepositoryIndexError, StateLockError, WorkspaceError, ContextError, RunStateError, CompletionError, PublicationError, CampaignError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
