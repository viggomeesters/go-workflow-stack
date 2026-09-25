"""Durable serial orchestration for an explicit bounded campaign contract.

Task, managed-run, workspace and release records remain authoritative.  This
module only projects eligible work and owns the campaign controller checkpoint.
"""
from __future__ import annotations

from copy import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Any
import uuid

from .campaign_contracts import (
    campaign_findings,
    campaign_task_findings,
    contract_digest,
    limit_reached,
)
from .task_state import open_task_records
from .state_io import atomic_json, repository_lock, _pid_alive


class CampaignError(ValueError):
    """Fail-closed campaign contract or controller error."""


RUN_SCHEMA = "go-workflow.campaign-run.v1"
RUN_STATUSES = {
    "running", "task_in_progress", "budget_exhausted", "no_eligible_tasks",
    "authority_required", "unsafe_repository", "unknown_external_effect",
    "provider_backoff", "paused", "drained", "cancelled", "goal_verified",
}
RUN_FIELDS = {
    "schema", "campaign_id", "project", "contract", "workspace_root", "status",
    "started_at", "updated_at", "current_task", "completed_tasks", "consumption",
    "history", "stop", "controller", "dispatch", "control", "limits", "resources", "clock",
    "provider", "failures",
}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _run_directory(repo: Path, campaign_id: str) -> Path:
    return repo / ".go" / "runs" / "campaigns" / campaign_id


def _new_state(
    repo: Path,
    contract: dict[str, Any],
    contract_path: Path,
    workspace_root: Path,
    args: Any,
) -> dict[str, Any]:
    digest = contract_digest(contract)
    directory = _run_directory(repo, contract["id"])
    snapshot = directory / f"contract-r{contract['revision']}-{digest[:12]}.json"
    if snapshot.exists():
        if contract_digest(_load_object(snapshot, "campaign contract snapshot")) != digest:
            raise CampaignError("campaign contract snapshot collision")
    else:
        atomic_json(snapshot, contract)
    created_at = _now_iso()
    return {
        "schema": RUN_SCHEMA,
        "campaign_id": contract["id"],
        "project": contract["project"],
        "contract": {
            "revision": contract["revision"],
            "sha256": digest,
            "source_path": str(contract_path.resolve()),
            "snapshot_path": str(snapshot.relative_to(repo)),
        },
        "workspace_root": str(workspace_root.resolve()),
        "status": "running",
        "started_at": created_at,
        "updated_at": created_at,
        "current_task": None,
        "completed_tasks": [],
        "consumption": {
            "active_wall_seconds": 0.0,
            "attempts_started": 0,
            "tasks_completed": 0,
        },
        "limits": {
            "max_commands": max(int(getattr(args, "max_commands", 36) or 36), 1),
            "max_repairs": max(int(getattr(args, "max_attempts", 5) or 5), 1),
        },
        "resources": {"commands_used": 0, "repair_attempts": 0},
        "clock": {"active_since_epoch": None},
        "provider": {
            "task_id": None,
            "model": None,
            "failure_count": 0,
            "not_before_epoch": None,
        },
        "controller": {"host": socket.gethostname(), "pid": os.getpid(), "nonce": uuid.uuid4().hex},
        "dispatch": None,
        "control": {"action": "run", "requested_at": created_at, "actor": str(getattr(args, "agent", "agent"))},
        "failures": [],
        "history": [],
        "stop": None,
    }


def _load_or_create_state(
    repo: Path,
    contract: dict[str, Any],
    contract_path: Path,
    workspace_root: Path,
    args: Any,
) -> tuple[Path, dict[str, Any]]:
    path = _run_directory(repo, contract["id"]) / "state.json"
    digest = contract_digest(contract)
    if not path.exists():
        state = _new_state(repo, contract, contract_path, workspace_root, args)
        atomic_json(path, state)
        return path, state
    state = _load_object(path, "campaign run state")
    if state.get("schema") != RUN_SCHEMA:
        raise CampaignError("campaign run state schema mismatch")
    _upgrade_state(state, args)
    _validate_state(state)
    if state.get("campaign_id") != contract["id"] or state.get("project") != contract["project"]:
        raise CampaignError("campaign run identity changed")
    binding = state.get("contract") or {}
    if binding.get("revision") != contract["revision"] or binding.get("sha256") != digest:
        raise CampaignError("campaign mandate changed; start or migrate an explicit revision")
    if state.get("workspace_root") != str(workspace_root.resolve()):
        raise CampaignError("campaign workspace root changed")
    permitted = set(contract["authority"]["permitted_tasks"])
    if state["current_task"] not in permitted | {None} or not set(state["completed_tasks"]) <= permitted:
        raise CampaignError("campaign run state invalid: task outside frozen mandate")
    snapshot_rel = Path(binding["snapshot_path"])
    expected_directory = _run_directory(repo, contract["id"]).resolve()
    snapshot = (repo / snapshot_rel).resolve()
    if snapshot_rel.is_absolute() or not snapshot.is_relative_to(expected_directory):
        raise CampaignError("campaign contract snapshot integrity failure: unsafe path")
    frozen = _load_object(snapshot, "campaign contract snapshot")
    if contract_digest(frozen) != binding["sha256"]:
        raise CampaignError("campaign contract snapshot integrity failure: digest mismatch")
    previous = state["controller"]
    if state["dispatch"] is not None and previous["pid"] != os.getpid():
        if previous["host"] != socket.gethostname():
            raise CampaignError("campaign controller identity is unknown across hosts")
        if _pid_alive(previous["pid"]):
            raise CampaignError("previous campaign controller is still live; takeover refused")
    if state["dispatch"] is not None:
        state["history"].append({
            "event": "campaign.dispatch_recovered",
            "created_at": _now_iso(),
            "task_id": state["dispatch"]["task_id"],
            "stage": state["dispatch"]["stage"],
        })
    state["controller"] = {"host": socket.gethostname(), "pid": os.getpid(), "nonce": uuid.uuid4().hex}
    return path, state


def _upgrade_state(state: dict[str, Any], args: Any) -> None:
    """Add v0.3.37 supervision fields to a valid v0.3.36 checkpoint."""
    consumption = state.get("consumption")
    migrated_commands = 0
    migrated_repairs = 0
    if isinstance(consumption, dict):
        migrated_commands = consumption.pop("commands_used", 0)
        migrated_repairs = consumption.pop("repair_attempts", 0)
    state.setdefault("limits", {
        "max_commands": max(int(getattr(args, "max_commands", 36) or 36), 1),
        "max_repairs": max(int(getattr(args, "max_attempts", 5) or 5), 1),
    })
    state.setdefault("resources", {
        "commands_used": migrated_commands,
        "repair_attempts": migrated_repairs,
    })
    state.setdefault("clock", {"active_since_epoch": None})
    state.setdefault("provider", {
        "task_id": None,
        "model": None,
        "failure_count": 0,
        "not_before_epoch": None,
    })
    state.setdefault("controller", {"host": socket.gethostname(), "pid": os.getpid(), "nonce": uuid.uuid4().hex})
    state.setdefault("dispatch", None)
    state.setdefault("control", {"action": "run", "requested_at": _now_iso(), "actor": str(getattr(args, "agent", "agent"))})
    state.setdefault("failures", [])


def _validate_state(state: dict[str, Any]) -> None:
    """Validate untrusted durable state without adding a runtime schema dependency."""
    if set(state) != RUN_FIELDS:
        raise CampaignError("campaign run state invalid: missing or unknown fields")
    binding = state.get("contract")
    if not isinstance(binding, dict) or set(binding) != {"revision", "sha256", "source_path", "snapshot_path"}:
        raise CampaignError("campaign run state invalid: contract binding")
    if (
        type(binding.get("revision")) is not int
        or binding["revision"] < 1
        or not isinstance(binding.get("sha256"), str)
        or len(binding["sha256"]) != 64
        or any(not isinstance(binding.get(key), str) or not binding[key] for key in ("source_path", "snapshot_path"))
    ):
        raise CampaignError("campaign run state invalid: contract binding")
    consumption = state.get("consumption")
    if not isinstance(consumption, dict) or set(consumption) != {
        "active_wall_seconds", "attempts_started", "tasks_completed",
    }:
        raise CampaignError("campaign consumption checkpoint invalid")
    if (
        type(consumption.get("attempts_started")) is not int
        or type(consumption.get("tasks_completed")) is not int
        or type(consumption.get("active_wall_seconds")) not in (int, float)
        or min(consumption.values()) < 0
    ):
        raise CampaignError("campaign consumption checkpoint invalid")
    completed = state.get("completed_tasks")
    if (
        not isinstance(completed, list)
        or any(not isinstance(item, str) or not item for item in completed)
        or len(completed) != len(set(completed))
        or consumption["tasks_completed"] != len(completed)
        or consumption["attempts_started"] < consumption["tasks_completed"]
    ):
        raise CampaignError("campaign run state invalid: completed task accounting")
    if state.get("status") not in RUN_STATUSES:
        raise CampaignError("campaign run state invalid: status")
    current = state.get("current_task")
    if current is not None and (not isinstance(current, str) or not current):
        raise CampaignError("campaign run state invalid: current task")
    if not isinstance(state.get("history"), list) or any(not isinstance(item, dict) for item in state["history"]):
        raise CampaignError("campaign run state invalid: history")
    limits = state.get("limits")
    if (not isinstance(limits, dict) or set(limits) != {"max_commands", "max_repairs"}
            or any(type(limits.get(key)) is not int or limits[key] < 1 for key in limits)):
        raise CampaignError("campaign run state invalid: frozen limits")
    resources = state.get("resources")
    if (not isinstance(resources, dict) or set(resources) != {"commands_used", "repair_attempts"}
            or any(type(resources.get(key)) is not int or resources[key] < 0 for key in resources)):
        raise CampaignError("campaign run state invalid: resource accounting")
    clock = state.get("clock")
    if (not isinstance(clock, dict) or set(clock) != {"active_since_epoch"}
            or (clock["active_since_epoch"] is not None
                and type(clock["active_since_epoch"]) not in (int, float))):
        raise CampaignError("campaign run state invalid: durable wall clock")
    provider = state.get("provider")
    if (not isinstance(provider, dict)
            or set(provider) != {"task_id", "model", "failure_count", "not_before_epoch"}
            or (provider["task_id"] is not None and not isinstance(provider["task_id"], str))
            or (provider["model"] is not None and (
                not isinstance(provider["model"], dict)
                or set(provider["model"]) != {"id", "effort"}
                or any(not isinstance(value, str) or not value for value in provider["model"].values())
            ))
            or type(provider["failure_count"]) is not int or provider["failure_count"] < 0
            or (provider["not_before_epoch"] is not None
                and type(provider["not_before_epoch"]) not in (int, float))):
        raise CampaignError("campaign run state invalid: provider backoff")
    controller = state.get("controller")
    if (not isinstance(controller, dict) or set(controller) != {"host", "pid", "nonce"}
            or not isinstance(controller["host"], str) or type(controller["pid"]) is not int
            or controller["pid"] < 1 or not isinstance(controller["nonce"], str) or not controller["nonce"]):
        raise CampaignError("campaign run state invalid: controller identity")
    dispatch = state.get("dispatch")
    if dispatch is not None and (not isinstance(dispatch, dict)
            or set(dispatch) != {"task_id", "stage", "nonce", "attempt", "started_at"}
            or dispatch.get("stage") not in {"selected", "bound", "dispatched", "returned"}):
        raise CampaignError("campaign run state invalid: dispatch checkpoint")
    control = state.get("control")
    if (not isinstance(control, dict) or set(control) != {"action", "requested_at", "actor"}
            or control.get("action") not in {"run", "pause", "drain", "cancel"}):
        raise CampaignError("campaign run state invalid: control request")
    if not isinstance(state.get("failures"), list) or any(not isinstance(item, dict) for item in state["failures"]):
        raise CampaignError("campaign run state invalid: failures")
    if any(not isinstance(state.get(key), str) or not state[key] for key in (
        "campaign_id", "project", "workspace_root", "started_at", "updated_at",
    )):
        raise CampaignError("campaign run state invalid: identity or timestamps")
    stop = state.get("stop")
    if stop is not None:
        if (
            not isinstance(stop, dict)
            or set(stop) != {"condition", "reason", "task_id"}
            or stop.get("condition") not in RUN_STATUSES - {"running", "task_in_progress"}
            or not isinstance(stop.get("reason"), str)
            or not stop["reason"]
            or (stop.get("task_id") is not None and not isinstance(stop["task_id"], str))
        ):
            raise CampaignError("campaign run state invalid: stop record")


def _save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now_iso()
    atomic_json(path, state)


def _checkpoint_wall(state: dict[str, Any]) -> None:
    started = state["clock"]["active_since_epoch"]
    if started is not None:
        state["consumption"]["active_wall_seconds"] += max(time.time() - started, 0.0)
        state["clock"]["active_since_epoch"] = None


def _start_wall(state: dict[str, Any]) -> None:
    if state["clock"]["active_since_epoch"] is None:
        state["clock"]["active_since_epoch"] = time.time()


def _reconcile_completed_task(
    repo: Path,
    state_path: Path,
    state: dict[str, Any],
) -> None:
    """Recover the narrow crash window after managed completion returned."""
    task_id = state.get("current_task")
    if not task_id or task_id in state["completed_tasks"]:
        return
    task_path = repo / ".go" / "tasks" / "done" / f"{task_id}.json"
    run_path = repo / ".go" / "runs" / task_id / "run-state.json"
    if not task_path.is_file() or not run_path.is_file():
        return
    run = _load_object(run_path, "managed completion checkpoint")
    if run.get("phase") != "complete":
        return
    workspace_path = repo / ".go" / "workspaces" / f"{task_id}.json"
    if workspace_path.is_file():
        from .campaign_delivery import delivery_report

        delivery = delivery_report(repo, task_id)
        if not delivery["delivered"]:
            exact = "; ".join(item["message"] for item in delivery["blockers"])
            raise CampaignError(f"completed task lacks delivery proof: {task_id}: {exact}")
    state["completed_tasks"].append(task_id)
    state["consumption"]["tasks_completed"] += 1
    state["current_task"] = None
    state["dispatch"] = None
    state["provider"] = {
        "task_id": None,
        "model": None,
        "failure_count": 0,
        "not_before_epoch": None,
    }
    state["history"].append({
        "event": "campaign.task_reconciled",
        "created_at": _now_iso(),
        "task_id": task_id,
        "source": "authoritative managed phase=complete and done task",
    })
    _save_state(state_path, state)


def _stop(
    path: Path,
    state: dict[str, Any],
    condition: str,
    reason: str,
    *,
    task_id: str | None = None,
) -> None:
    _checkpoint_wall(state)
    state["status"] = condition
    state["stop"] = {"condition": condition, "reason": reason, "task_id": task_id}
    state["history"].append({
        "event": "campaign.stopped",
        "created_at": _now_iso(),
        "condition": condition,
        "reason": reason,
        "task_id": task_id,
    })
    _save_state(path, state)


def _failure_record(task: dict[str, Any], task_result: dict[str, Any], previous: list[dict[str, Any]]) -> dict[str, Any]:
    material = {
        "task_id": task["id"],
        "status": task_result.get("status"),
        "summary": task_result.get("summary"),
        "checks": task_result.get("checks") or [],
    }
    fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()
    repeat = previous[-1].get("repeat_count", 0) + 1 if previous and previous[-1].get("fingerprint") == fingerprint else 1
    return {
        "task_id": task["id"],
        "created_at": _now_iso(),
        "fingerprint": fingerprint,
        "repeat_count": repeat,
        "strategy": "block_or_isolate_research" if repeat >= 2 else "re_approach",
        "summary": str(task_result.get("summary") or task_result.get("status") or "task did not complete"),
        "evidence": {
            "status": task_result.get("status"),
            "checks": task_result.get("checks") or [],
            "commands_run": max(int(task_result.get("commands_run") or 0), 0),
            "repair_attempts": max(int(task_result.get("repair_attempts") or 0), 0),
        },
    }


def _temporary_provider_failure(task_result: dict[str, Any]) -> bool:
    status = str(task_result.get("status") or "").lower()
    summary = str(task_result.get("summary") or "").lower()
    if status in {"provider_unavailable", "provider_temporary", "rate_limited"}:
        return True
    return "temporary provider" in summary or "provider rate limit" in summary


def _task_model(task: dict[str, Any]) -> dict[str, str]:
    model = ((task.get("execution_contract") or {}).get("model") or {})
    if set(model) != {"id", "effort"} or any(not isinstance(value, str) or not value for value in model.values()):
        raise CampaignError(f"campaign task lacks an exact model binding: {task['id']}")
    return {"id": model["id"], "effort": model["effort"]}


def _control_stop(state_path: Path, state: dict[str, Any], action: str, actor: str) -> tuple[str, str]:
    status = {"pause": "paused", "drain": "drained", "cancel": "cancelled"}[action]
    state["control"] = {"action": action, "requested_at": _now_iso(), "actor": actor}
    reason = {
        "pause": "Campaign paused at a durable boundary; no worker was dispatched.",
        "drain": "Campaign drained at a durable boundary; no new task was dispatched.",
        "cancel": "Campaign cancelled without terminating an unverified process; task/workspace state is preserved.",
    }[action]
    _stop(state_path, state, status, reason, task_id=state.get("current_task"))
    return status, reason


def _bound_task_args(
    args: Any,
    repo: Path,
    task: dict[str, Any],
    contract: dict[str, Any],
    workspace_root: Path,
    *,
    require_managed: bool,
) -> Any:
    """Bind a fresh managed task deterministically; resumes use saved state."""
    bound = copy(args)
    defaults = {
        "workspace_path": "",
        "workspace_branch": "",
        "base_branch": "",
        "base_commit": "",
        "run_id": "",
        "task_id": "",
        "max_commands": 36,
        "max_minutes": max(1, ((contract["authority"]["budget"]["wall_seconds"] or 3600) + 59) // 60),
        "max_attempts": contract["authority"]["budget"]["max_attempts"] or 2,
        "command_timeout_seconds": 900,
        "executor_agent": "codex",
        "repair_agent": "",
        "build_command": "",
        "critic_command": "",
        "repair_command": "",
        "semantic_critic": True,
        "allow_deploy": False,
    }
    for name, value in defaults.items():
        if not hasattr(bound, name):
            setattr(bound, name, value)
    bound.max_attempts = min(
        max(int(bound.max_attempts), 1),
        contract["authority"]["budget"]["max_attempts"] or max(int(bound.max_attempts), 1),
    )
    bound.executor_agent = "codex"
    bound.semantic_critic = True
    bound.build_command = bound.critic_command = bound.repair_command = ""
    bound.repair_agent = ""
    bound.task_id = task["id"]
    release_authority = contract["authority"]["release"]
    bound.allow_push = bool(release_authority["allow_push"])
    bound.ship_policy = "push" if bound.allow_push else ("local-commit" if getattr(args, "explicit_ship_policy", None) == "local-commit" else "none")
    release = ((task.get("execution_contract") or {}).get("release") or {})
    project = _load_object(repo / ".go" / "project.json", "campaign project")
    profile = (project.get("release_profiles") or {}).get(release.get("profile"), {})
    deployment = profile.get("deployment") if isinstance(profile, dict) else None
    target = deployment.get("target") if isinstance(deployment, dict) else None
    bound.allow_deploy = (
        bool(getattr(bound, "allow_deploy", False))
        and isinstance(target, str)
        and target in contract["authority"]["deployment"]["targets"]
    )

    if task.get("status") != "open":
        return bound
    workspace = ((task.get("execution_contract") or {}).get("workspace") or {})
    if workspace.get("mode") != "task_worktree" or workspace.get("control_state") != "repo_local_single_writer":
        if require_managed:
            raise CampaignError(f"campaign task requires managed task_worktree execution: {task['id']}")
        return bound
    base_branch = str(workspace.get("base_branch") or "")
    if not base_branch:
        raise CampaignError(f"campaign task lacks an explicit base branch: {task['id']}")
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"refs/heads/{base_branch}"],
        text=True,
        capture_output=True,
    )
    if resolved.returncode:
        raise CampaignError(f"campaign base branch is unavailable: {base_branch}")
    campaign_id = contract["id"]
    bound.workspace_path = str((workspace_root / task["id"]).resolve())
    bound.workspace_branch = f"codex/{campaign_id}-{task['id']}"
    bound.base_branch = base_branch
    bound.base_commit = resolved.stdout.strip()
    bound.run_id = f"{campaign_id}-{task['id']}-r{contract['revision']}"
    return bound


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CampaignError(f"{label} unavailable or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise CampaignError(f"{label} must be a JSON object: {path}")
    return value


def load_contract(
    repo: Path,
    path: Path,
    *,
    previous_path: Path | None = None,
) -> dict[str, Any]:
    """Load and bind one explicit contract revision to the canonical repo."""
    contract = _load_object(path, "campaign contract")
    previous = _load_object(previous_path, "previous campaign contract") if previous_path else None
    findings = campaign_findings(repo, contract, previous=previous)
    if findings:
        raise CampaignError("campaign contract rejected: " + "; ".join(findings))
    return contract


def plan_campaign(
    repo: Path,
    contract_path: Path,
    api: Any,
    *,
    previous_path: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return an allowlist-ordered read-only projection of currently eligible work."""
    contract = load_contract(repo, contract_path, previous_path=previous_path)
    root = repo / ".go"
    permitted = contract["authority"]["permitted_tasks"]
    active_paths = sorted((root / "tasks" / "active").glob("*.json"))
    if len(active_paths) > 1:
        raise CampaignError("multiple active tasks make serial campaign ownership unsafe")
    selection = "next_open"
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def eligible(task: dict[str, Any]) -> list[str]:
        findings = campaign_task_findings(contract, task)
        findings.extend(api.dependency_findings(repo, task, readiness=True))
        findings.extend(api.architecture_claim_findings(root, task))
        return findings

    if active_paths:
        task = _load_object(active_paths[0], "active campaign task")
        task_id = str(task.get("id") or active_paths[0].stem)
        if task_id not in permitted:
            raise CampaignError(f"foreign active task prevents serial campaign execution: {task_id}")
        if not (root / "runs" / task_id / "run-state.json").is_file():
            raise CampaignError(f"active campaign task lacks a durable managed checkpoint: {task_id}")
        findings = eligible(task)
        if findings:
            raise CampaignError(f"active campaign task is no longer eligible: {task_id}: " + "; ".join(findings))
        selected = [task]
        selection = "resume_active"

    cleanup: list[dict[str, Any]] = []
    if not selected:
        for task_id in permitted:
            task_path = root / "tasks" / "done" / f"{task_id}.json"
            run_path = root / "runs" / task_id / "run-state.json"
            if not task_path.is_file() or not run_path.is_file():
                continue
            run = _load_object(run_path, "managed cleanup checkpoint")
            if run.get("phase") in {"release", "cleanup"}:
                cleanup.append(_load_object(task_path, "completed campaign task"))
        if len(cleanup) > 1:
            raise CampaignError("multiple pending managed cleanups require explicit repair")
        if cleanup:
            findings = eligible(cleanup[0])
            if findings:
                raise CampaignError("pending cleanup task is no longer campaign-eligible: " + "; ".join(findings))
            selected = cleanup
            selection = "resume_cleanup"

    open_by_id = {
        str(task.get("id") or path.stem): task
        for path, task in open_task_records(root)
    }
    if not selected:
        for task_id in permitted:
            task = open_by_id.get(task_id)
            if task is None:
                continue
            findings = eligible(task)
            if findings:
                skipped.append({"task_id": task_id, "findings": findings})
            else:
                selected.append(task)
    projection = {
        "schema": "go-workflow.campaign-plan.v1",
        "id": contract["id"],
        "project": contract["project"],
        "revision": contract["revision"],
        "contract_sha256": contract_digest(contract),
        "contract_path": str(contract_path.resolve()),
        "next_tasks": [task["id"] for task in selected],
        "selection": selection if selected else "none",
        "skipped_tasks": skipped,
        "stop_semantics": "no_eligible_tasks_is_not_goal_verified",
    }
    if contract.get("execution", {}).get("mode") == "until_scope":
        from .campaign_contracts import proven_progress
        projection["progress"] = proven_progress(repo, contract)
    return projection, selected


def execute_campaign(
    repo: Path,
    args: Any,
    mode: str,
    api: Any,
    execute_task: Any,
) -> tuple[int, dict[str, Any]]:
    """Run permitted tasks serially until a declared stop boundary is reached."""
    repo = repo.resolve()
    contract_path = Path(args.campaign).resolve()
    previous = Path(args.previous_campaign).resolve() if getattr(args, "previous_campaign", "") else None
    workspace_value = str(getattr(args, "campaign_workspace_root", "") or "").strip()
    if not workspace_value:
        raise CampaignError("--campaign-workspace-root is required for durable campaign execution")
    workspace_root = Path(workspace_value)
    if not workspace_root.is_absolute():
        workspace_root = (Path.cwd() / workspace_root).resolve()
    if workspace_root == repo or workspace_root.is_relative_to(repo) or repo.is_relative_to(workspace_root):
        raise CampaignError("campaign workspace root must be outside the control repository")
    contract = load_contract(repo, contract_path, previous_path=previous)
    budget = contract["authority"]["budget"]
    taskwise = contract.get("execution", {}).get("mode") == "until_scope"
    result: dict[str, Any] = {
        "schema": "go-workflow.auto-run-result.v1",
        "mode": mode,
        "repo": str(repo),
        "status": "running",
        "completed_tasks": [],
        "blocked_task": None,
        "checks": [],
        "commands_run": 0,
        "goal_verified": False,
    }
    with repository_lock(repo / ".go", "campaign-controller", timeout_seconds=0.1):
        state_path, state = _load_or_create_state(
            repo, contract, contract_path, workspace_root, args,
        )
        _reconcile_completed_task(repo, state_path, state)
        action = str(getattr(args, "campaign_action", "run") or "run")
        if action in {"pause", "drain", "cancel"}:
            status, reason = _control_stop(state_path, state, action, str(getattr(args, "agent", "agent")))
            result.update(status=status, summary=reason, blocked_task=state.get("current_task"))
            result["campaign_state"] = str(state_path.relative_to(repo))
            api.write_latest_run_state(repo, repo / ".go", result, args, mode)
            return 1, result
        if action not in {"run", "resume"}:
            raise CampaignError("unsupported campaign action")
        not_before = state["provider"]["not_before_epoch"]
        if not_before is not None and time.time() < not_before:
            reason = "Temporary provider failure is in bounded backoff; exact model binding is preserved."
            state["status"] = "provider_backoff"
            state["stop"] = {
                "condition": "provider_backoff",
                "reason": reason,
                "task_id": state["provider"]["task_id"],
            }
            _save_state(state_path, state)
            result.update(
                status="provider_backoff",
                summary=reason,
                blocked_task=state["provider"]["task_id"],
                retry_not_before_epoch=not_before,
                campaign_state=str(state_path.relative_to(repo)),
            )
            api.write_latest_run_state(repo, repo / ".go", result, args, mode)
            return 1, result
        if not_before is not None:
            state["provider"]["not_before_epoch"] = None
        _checkpoint_wall(state)
        _start_wall(state)
        state["control"] = {"action": "run", "requested_at": _now_iso(), "actor": str(getattr(args, "agent", "agent"))}
        state["status"] = "running"
        state["stop"] = None
        _save_state(state_path, state)
        while True:
            active_since = state["clock"]["active_since_epoch"]
            elapsed = state["consumption"]["active_wall_seconds"] + (
                max(time.time() - active_since, 0.0) if active_since is not None else 0.0
            )
            if (
                limit_reached(elapsed, budget["wall_seconds"])
                or limit_reached(state["consumption"]["attempts_started"], budget["max_attempts"])
                or limit_reached(state["consumption"]["tasks_completed"], budget["max_tasks"])
                or limit_reached(state["resources"]["commands_used"], budget.get("max_commands") if taskwise else state["limits"]["max_commands"])
                or (not taskwise and state["resources"]["repair_attempts"] >= state["limits"]["max_repairs"])
            ):
                _stop(state_path, state, "budget_exhausted", "campaign-wide budget exhausted")
                result.update(status="budget_exhausted", budget_exhausted=True)
                break

            projection, selected = plan_campaign(
                repo, contract_path, api, previous_path=previous,
            )
            no_progress_ids = {
                item["task_id"] for item in state["failures"]
                if item.get("repeat_count", 0) >= 2
                and item.get("strategy") == "block_or_isolate_research"
            }
            if no_progress_ids and projection["selection"] == "next_open":
                independent = [task for task in selected if task["id"] not in no_progress_ids]
                for task in selected:
                    if task["id"] in no_progress_ids:
                        projection["skipped_tasks"].append({
                            "task_id": task["id"],
                            "findings": ["repeated evidence-identical failure requires a different strategy or bounded research"],
                        })
                selected = independent
                projection["next_tasks"] = [task["id"] for task in selected]
                if selected:
                    projection["selection"] = "next_independent_after_no_progress"
            result["campaign"] = projection
            if not selected:
                if no_progress_ids:
                    blocked = sorted(no_progress_ids)[0]
                    reason = (
                        "Repeated evidence-identical failure requires a different strategy or bounded research; "
                        "no eligible independent task remains."
                    )
                    _stop(state_path, state, "authority_required", reason, task_id=blocked)
                    result.update(status="authority_required", summary=reason, blocked_task=blocked)
                    break
                from .campaign_audit import audit_campaign_goal
                audit = audit_campaign_goal(
                    repo,
                    contract_path,
                    previous_path=previous,
                    persist=True,
                )
                result["completion_audit"] = audit
                result["goal_verified"] = audit["goal_verified"]
                if audit["goal_verified"]:
                    reason = "Every adopted campaign outcome has current required proof."
                    _stop(state_path, state, "goal_verified", reason)
                    result.update(status="goal_verified", summary=reason)
                else:
                    reason = (
                        "No permitted eligible task remains, but the shared goal audit is "
                        f"{audit['status']}; empty queue is not completion proof."
                    )
                    _stop(state_path, state, "no_eligible_tasks", reason)
                    result.update(status="no_eligible_tasks", summary=reason)
                break

            task = selected[0]
            model = _task_model(task)
            provider = state["provider"]
            if provider["task_id"] == task["id"] and provider["model"] is not None and provider["model"] != model:
                raise CampaignError("campaign task model changed during provider recovery; fallback refused")
            if provider["task_id"] != task["id"]:
                state["provider"] = {
                    "task_id": task["id"],
                    "model": model,
                    "failure_count": 0,
                    "not_before_epoch": None,
                }
            state["current_task"] = task["id"]
            state["status"] = "task_in_progress"
            state["consumption"]["attempts_started"] += 1
            state["history"].append({
                "event": "campaign.task_selected",
                "created_at": _now_iso(),
                "task_id": task["id"],
                "attempt": state["consumption"]["attempts_started"],
            })
            state["dispatch"] = {
                "task_id": task["id"],
                "stage": "selected",
                "nonce": uuid.uuid4().hex,
                "attempt": state["consumption"]["attempts_started"],
                "started_at": _now_iso(),
            }
            _save_state(state_path, state)
            try:
                task_args = _bound_task_args(
                    args,
                    repo,
                    task,
                    contract,
                    workspace_root,
                    require_managed=getattr(execute_task, "__name__", "") == "execute_managed",
                )
                task_args.campaign_failed_proof = [
                    item for item in state["failures"] if item.get("task_id") == task["id"]
                ]
                task_args.max_commands = min(
                    max(int(task_args.max_commands), 1),
                    (budget.get("max_commands", state["limits"]["max_commands"]) -
                     (0 if taskwise and "max_commands" not in budget else state["resources"]["commands_used"])),
                )
                task_args.max_attempts = min(
                    max(int(task_args.max_attempts), 1),
                    state["limits"]["max_repairs"] - (0 if taskwise else state["resources"]["repair_attempts"]),
                )
                state["dispatch"]["stage"] = "bound"
                _save_state(state_path, state)
            except CampaignError as exc:
                _stop(state_path, state, "unsafe_repository", str(exc), task_id=task["id"])
                result.update(
                    status="unsafe_repository",
                    summary=str(exc),
                    blocked_task=task["id"],
                    campaign=projection,
                    completed_tasks=list(state["completed_tasks"]),
                )
                break
            state["dispatch"]["stage"] = "dispatched"
            _save_state(state_path, state)
            code, task_result = execute_task(repo, task_args, mode, task, api)
            used_commands = max(int(task_result.get("commands_run") or 0), 0)
            used_repairs = max(int(task_result.get("repair_attempts") or 0), 0)
            result["commands_run"] += used_commands
            state["resources"]["commands_used"] += used_commands
            state["resources"]["repair_attempts"] += used_repairs
            state["dispatch"]["stage"] = "returned"
            result["checks"].extend(task_result.get("checks") or [])
            completed = task_result.get("completed_tasks") or []
            managed_delivery = getattr(execute_task, "__name__", "") == "execute_managed"
            delivery = None
            if managed_delivery:
                from .campaign_delivery import delivery_report

                delivery = delivery_report(repo, task["id"])
                task_result["delivery"] = delivery
            successful = (
                code == 0
                and task_result.get("status") in {"task_complete", "done"}
                and task["id"] in completed
                and (delivery is None or delivery["delivered"])
            )
            if not successful:
                if delivery is not None and code == 0 and task["id"] in completed and not delivery["delivered"]:
                    task_result["summary"] = "Delivery proof incomplete: " + "; ".join(
                        item["message"] for item in delivery["blockers"]
                    )
                failure = _failure_record(task, task_result, state["failures"])
                state["failures"].append(failure)
                if _temporary_provider_failure(task_result):
                    state["provider"]["failure_count"] += 1
                    delay = min(2 ** (state["provider"]["failure_count"] - 1), 60)
                    state["provider"]["not_before_epoch"] = time.time() + delay
                    reason = f"Temporary provider failure; retry is bounded for {delay}s with the same model binding."
                    _stop(state_path, state, "provider_backoff", reason, task_id=task["id"])
                    result.update(
                        task_result,
                        status="provider_backoff",
                        summary=reason,
                        campaign=projection,
                        completed_tasks=list(state["completed_tasks"]),
                        blocked_task=task["id"],
                        retry_not_before_epoch=state["provider"]["not_before_epoch"],
                    )
                    break
                no_progress = failure["repeat_count"] >= 2
                delivery_condition = delivery.get("stop_condition") if delivery is not None else None
                condition = (
                    "budget_exhausted"
                    if task_result.get("status") == "budget_exhausted"
                    or limit_reached(state["resources"]["commands_used"], budget.get("max_commands") if taskwise else state["limits"]["max_commands"])
                    or (not taskwise and state["resources"]["repair_attempts"] >= state["limits"]["max_repairs"])
                    else delivery_condition
                    if delivery_condition is not None
                    else "unknown_external_effect"
                    if "external effect" in str(task_result.get("summary", "")).lower()
                    else "authority_required"
                    if "authority" in str(task_result.get("summary", "")).lower()
                    or "requires explicit" in str(task_result.get("summary", "")).lower()
                    else "unsafe_repository"
                    if task_result.get("status") in {"safety_gate", "resume_gate"}
                    else "authority_required"
                )
                open_path = repo / ".go" / "tasks" / "open" / f"{task['id']}.json"
                active_path = repo / ".go" / "tasks" / "active" / f"{task['id']}.json"
                if no_progress and (open_path.is_file() or active_path.is_file()):
                    if active_path.is_file():
                        api.block_task_record(
                            repo,
                            repo / ".go",
                            active_path,
                            task,
                            str(getattr(args, "agent", "agent")),
                            "Repeated evidence-identical failure requires a different strategy or bounded research.",
                            failure["evidence"]["checks"],
                        )
                    state["current_task"] = None
                    state["dispatch"] = None
                    state["status"] = "running"
                    state["stop"] = None
                    state["history"].append({
                        "event": "campaign.no_progress_isolated",
                        "created_at": _now_iso(),
                        "task_id": task["id"],
                        "fingerprint": failure["fingerprint"],
                        "strategy": failure["strategy"],
                    })
                    _save_state(state_path, state)
                    result["blocked_task"] = task["id"]
                    continue
                _stop(
                    state_path,
                    state,
                    condition,
                    ("No progress after repeated evidence-identical failure; block or isolate bounded research: "
                     + failure["summary"]) if no_progress else failure["summary"],
                    task_id=task["id"],
                )
                result.update(task_result)
                result["status"] = condition
                result["campaign"] = projection
                result["completed_tasks"] = list(state["completed_tasks"])
                result["blocked_task"] = task["id"]
                if no_progress:
                    result["summary"] = state["stop"]["reason"]
                break

            if task["id"] not in state["completed_tasks"]:
                state["completed_tasks"].append(task["id"])
                state["consumption"]["tasks_completed"] += 1
            _checkpoint_wall(state)
            _start_wall(state)
            state["current_task"] = None
            state["dispatch"] = None
            state["provider"] = {
                "task_id": None,
                "model": None,
                "failure_count": 0,
                "not_before_epoch": None,
            }
            state["status"] = "running"
            state["history"].append({
                "event": "campaign.task_completed",
                "created_at": _now_iso(),
                "task_id": task["id"],
            })
            _save_state(state_path, state)
            result["completed_tasks"] = list(state["completed_tasks"])
        result["campaign_state"] = str(state_path.relative_to(repo))
        api.write_latest_run_state(repo, repo / ".go", result, args, mode)
    return (0 if result["status"] in {"budget_exhausted", "goal_verified"} else 1), result
