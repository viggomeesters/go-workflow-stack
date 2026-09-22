"""Durable serial orchestration for an explicit bounded campaign contract.

Task, managed-run, workspace and release records remain authoritative.  This
module only projects eligible work and owns the campaign controller checkpoint.
"""
from __future__ import annotations

from copy import copy
import json
from pathlib import Path
import subprocess
import time
from typing import Any

from .campaign_contracts import (
    campaign_findings,
    campaign_task_findings,
    contract_digest,
)
from .task_state import open_task_records
from .state_io import atomic_json, repository_lock


class CampaignError(ValueError):
    """Fail-closed campaign contract or controller error."""


RUN_SCHEMA = "go-workflow.campaign-run.v1"
RUN_STATUSES = {
    "running", "task_in_progress", "budget_exhausted", "no_eligible_tasks",
    "authority_required", "unsafe_repository", "unknown_external_effect",
}
RUN_FIELDS = {
    "schema", "campaign_id", "project", "contract", "workspace_root", "status",
    "started_at", "updated_at", "current_task", "completed_tasks", "consumption",
    "history", "stop",
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
        "history": [],
        "stop": None,
    }


def _load_or_create_state(
    repo: Path,
    contract: dict[str, Any],
    contract_path: Path,
    workspace_root: Path,
) -> tuple[Path, dict[str, Any]]:
    path = _run_directory(repo, contract["id"]) / "state.json"
    digest = contract_digest(contract)
    if not path.exists():
        state = _new_state(repo, contract, contract_path, workspace_root)
        atomic_json(path, state)
        return path, state
    state = _load_object(path, "campaign run state")
    if state.get("schema") != RUN_SCHEMA:
        raise CampaignError("campaign run state schema mismatch")
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
    return path, state


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
        or min(consumption["attempts_started"], consumption["tasks_completed"], consumption["active_wall_seconds"]) < 0
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
    state["completed_tasks"].append(task_id)
    state["consumption"]["tasks_completed"] += 1
    state["current_task"] = None
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
        "max_minutes": max(1, (contract["authority"]["budget"]["wall_seconds"] + 59) // 60),
        "max_attempts": contract["authority"]["budget"]["max_attempts"],
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
        contract["authority"]["budget"]["max_attempts"],
    )
    bound.executor_agent = "codex"
    bound.semantic_critic = True
    bound.build_command = bound.critic_command = bound.repair_command = ""
    bound.repair_agent = ""
    bound.task_id = task["id"]
    release_authority = contract["authority"]["release"]
    bound.allow_push = bool(release_authority["allow_push"])
    bound.ship_policy = "push" if bound.allow_push else "none"
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
    invocation_started = time.monotonic()
    with repository_lock(repo / ".go", "campaign-controller", timeout_seconds=0.1):
        state_path, state = _load_or_create_state(
            repo, contract, contract_path, workspace_root,
        )
        _reconcile_completed_task(repo, state_path, state)
        state["status"] = "running"
        state["stop"] = None
        _save_state(state_path, state)
        while True:
            elapsed = state["consumption"]["active_wall_seconds"] + (time.monotonic() - invocation_started)
            if (
                elapsed >= budget["wall_seconds"]
                or state["consumption"]["attempts_started"] >= budget["max_attempts"]
                or state["consumption"]["tasks_completed"] >= budget["max_tasks"]
            ):
                state["consumption"]["active_wall_seconds"] = elapsed
                _stop(state_path, state, "budget_exhausted", "campaign-wide budget exhausted")
                result.update(status="budget_exhausted", budget_exhausted=True)
                break

            projection, selected = plan_campaign(
                repo, contract_path, api, previous_path=previous,
            )
            result["campaign"] = projection
            if not selected:
                state["consumption"]["active_wall_seconds"] = elapsed
                _stop(
                    state_path,
                    state,
                    "no_eligible_tasks",
                    "No permitted eligible task remains; goal verification is a separate audit.",
                )
                result.update(
                    status="no_eligible_tasks",
                    summary="No permitted eligible task remains; campaign goal is not verified.",
                )
                break

            task = selected[0]
            state["current_task"] = task["id"]
            state["status"] = "task_in_progress"
            state["consumption"]["attempts_started"] += 1
            state["history"].append({
                "event": "campaign.task_selected",
                "created_at": _now_iso(),
                "task_id": task["id"],
                "attempt": state["consumption"]["attempts_started"],
            })
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
            except CampaignError as exc:
                state["consumption"]["active_wall_seconds"] += time.monotonic() - invocation_started
                _stop(state_path, state, "unsafe_repository", str(exc), task_id=task["id"])
                result.update(
                    status="unsafe_repository",
                    summary=str(exc),
                    blocked_task=task["id"],
                    campaign=projection,
                    completed_tasks=list(state["completed_tasks"]),
                )
                break
            code, task_result = execute_task(repo, task_args, mode, task, api)
            result["commands_run"] += int(task_result.get("commands_run") or 0)
            result["checks"].extend(task_result.get("checks") or [])
            completed = task_result.get("completed_tasks") or []
            successful = code == 0 and task_result.get("status") in {"task_complete", "done"} and task["id"] in completed
            if not successful:
                state["consumption"]["active_wall_seconds"] += time.monotonic() - invocation_started
                invocation_started = time.monotonic()
                condition = (
                    "budget_exhausted"
                    if task_result.get("status") == "budget_exhausted"
                    else "unknown_external_effect"
                    if "external effect" in str(task_result.get("summary", "")).lower()
                    else "authority_required"
                    if "authority" in str(task_result.get("summary", "")).lower()
                    or "requires explicit" in str(task_result.get("summary", "")).lower()
                    else "unsafe_repository"
                    if task_result.get("status") in {"safety_gate", "resume_gate"}
                    else "authority_required"
                )
                _stop(
                    state_path,
                    state,
                    condition,
                    str(task_result.get("summary") or task_result.get("status") or "task did not complete"),
                    task_id=task["id"],
                )
                result.update(task_result)
                result["status"] = condition
                result["campaign"] = projection
                result["completed_tasks"] = list(state["completed_tasks"])
                result["blocked_task"] = task["id"]
                break

            if task["id"] not in state["completed_tasks"]:
                state["completed_tasks"].append(task["id"])
                state["consumption"]["tasks_completed"] += 1
            state["consumption"]["active_wall_seconds"] += time.monotonic() - invocation_started
            invocation_started = time.monotonic()
            state["current_task"] = None
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
    return (0 if result["status"] == "budget_exhausted" else 1), result
