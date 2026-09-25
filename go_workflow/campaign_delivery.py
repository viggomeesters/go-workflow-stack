"""Read-only delivery truth for campaign task transitions.

The managed runner, publisher, deployment verifier and workspace registry remain
the authorities.  This module combines their durable readbacks without
performing or repeating an external effect.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .completion import content_snapshot, read_artifact
from .execution_contracts import valid_release_receipt
from .worktrees import git, git_text, read_object, registry_path, validate_record


SCHEMA = "go-workflow.task-delivery-report.v1"


def _task(repo: Path, task_id: str) -> dict[str, Any]:
    matches = [
        repo / ".go" / "tasks" / state / f"{task_id}.json"
        for state in ("open", "active", "blocked", "done")
        if (repo / ".go" / "tasks" / state / f"{task_id}.json").is_file()
    ]
    if len(matches) != 1:
        raise ValueError("delivery task is missing or duplicated")
    value = read_object(matches[0])
    if value.get("id") != task_id or value.get("status") != matches[0].parent.name:
        raise ValueError("delivery task identity/state mismatch")
    return value


def _read_optional(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return read_object(path)


def _block(blockers: list[dict[str, str]], code: str, message: str, condition: str) -> None:
    blockers.append({"code": code, "message": message, "condition": condition})


def _head_relation(repo: Path, task: dict[str, Any], released: str) -> str:
    head = git_text(repo, "rev-parse", "HEAD")
    if head == released:
        return "released_head"
    if git(repo, "merge-base", "--is-ancestor", released, head, check=False).returncode:
        return "released_commit_not_preserved"
    released_digest = content_snapshot(repo, task, released)["digest"]
    head_digest = content_snapshot(repo, task, head)["digest"]
    return "checkpoint_only_advance" if released_digest == head_digest else "product_advance"


def delivery_report(repo: Path, task_id: str) -> dict[str, Any]:
    """Project exact remaining blockers without mutating Git or external state."""
    repo = Path(repo).resolve()
    task = _task(repo, task_id)
    if (task.get("delivery_block") or {}).get("role") == "member":
        from .delivery_blocks import member_completion_findings
        errors = member_completion_findings(repo,task)
        coordinator_id = task["delivery_block"]["coordinator_id"]
        if coordinator_id == task_id:raise ValueError("Self-referential joint member")
        report = delivery_report(repo,coordinator_id)
        report["task_id"] = task_id
        report["blockers"] += [{"code":"joint_delivery_pending","message":error,"condition":"authority_required"} for error in errors]
        report["delivered"] = report["delivered"] and not errors
        if errors:report["stop_condition"]="authority_required"
        return report
    release_policy = ((task.get("execution_contract") or {}).get("release") or {})
    release_mode = release_policy.get("mode")
    closure_path = repo / ".go/runs" / task_id / "delivery-closure.json"
    if closure_path.exists() or closure_path.is_symlink():
        from .delivery_closure import inspect_closure
        closure = inspect_closure(repo, task_id)
        receipt = task.get("release_receipt") or {}
        if closure["delivered"] and receipt.get("commit") and _head_relation(repo, task, receipt["commit"]) == "released_commit_not_preserved":
            closure["delivered"] = False
            closure["blockers"].append("Current branch does not preserve the released product commit")
        publication = ({"status": "verified", "commit": receipt.get("commit"), "tag": receipt.get("tag")}
                       if closure["delivered"] and release_mode == "required"
                       else {"status": "not_applicable", "commit": None, "tag": None})
        project = read_object(repo / ".go/project.json")
        profile = (project.get("release_profiles") or {}).get(release_policy.get("profile"), {})
        live = (profile.get("deployment") or {}).get("mode") == "required"
        return {"schema": SCHEMA, "task_id": task_id, "release_policy": release_mode,
                "delivered": closure["delivered"], "reported_live": closure["delivered"] and live,
                "head_relation": _head_relation(repo, task, receipt["commit"]) if publication["commit"] else "not_applicable",
                "publication": publication, "workspace_state": "cleaned" if closure["delivered"] else "unconfirmed",
                "blockers": [{"code": "closure_pending", "message": error, "condition": "unknown_external_effect"}
                             for error in closure["blockers"]],
                "stop_condition": None if closure["delivered"] else "unknown_external_effect"}
    blockers: list[dict[str, str]] = []
    run_path = repo / ".go" / "runs" / task_id / "run-state.json"
    release_path = repo / ".go" / "runs" / task_id / "release-state.json"
    run = None
    if run_path.is_file():
        try:
            from .run_state import read_state

            run = read_state(repo, task_id)
        except ValueError as exc:
            _block(blockers, "managed_checkpoint_invalid", str(exc), "unsafe_repository")
    release_state = None
    if release_path.is_file():
        try:
            raw_release = _read_optional(release_path)
            from .release import _load as load_release

            release_state = load_release(repo, task, raw_release["owner"], raw_release["run_id"])
        except (KeyError, TypeError, ValueError) as exc:
            _block(blockers, "release_checkpoint_invalid", str(exc), "unsafe_repository")

    lifecycle_done = (
        task.get("status") == "done"
        and task.get("work_status") == "completed"
        and task.get("review_status") == "approved"
    )
    if not lifecycle_done:
        _block(blockers, "task_not_approved_done", "Task is not completed and approved.", "authority_required")

    # Standalone publication has no managed checkpoint.  Its published state,
    # task lifecycle and cleanup proof are sufficient authorities.
    standalone_published = release_state is not None and release_state.get("phase") == "published"
    if not standalone_published and (run is None or run.get("phase") != "complete"):
        if not run_path.is_file():
            _block(blockers, "managed_checkpoint_missing", "Managed task checkpoint is missing, not complete.", "authority_required")
        elif run is not None:
            _block(
                blockers,
                "managed_checkpoint_incomplete",
                f"Managed task checkpoint is {run.get('phase')!s}, not complete.",
                "authority_required",
            )

    workspace = None
    workspace_path = registry_path(repo, task_id)
    if not workspace_path.is_file():
        _block(blockers, "workspace_registry_missing", "Workspace integration/cleanup proof is missing.", "authority_required")
    else:
        try:
            workspace = validate_record(read_object(workspace_path))
        except ValueError as exc:
            _block(blockers, "workspace_registry_invalid", str(exc), "unsafe_repository")
        else:
            if workspace["state"] != "cleaned":
                base_ref = "refs/heads/" + workspace["base_branch"]
                try:
                    advanced = git_text(repo, "rev-parse", base_ref) != workspace["base_commit"]
                except ValueError:
                    advanced = False
                if workspace["state"] == "ready" and advanced:
                    _block(
                        blockers,
                        "base_reconciliation_required",
                        "Base branch advanced; reconcile the owned workspace and rerun final evidence.",
                        "unsafe_repository",
                    )
                else:
                    _block(
                        blockers,
                        "workspace_cleanup_pending",
                        f"Workspace state is {workspace['state']}; verified cleanup is still required.",
                        "authority_required",
                    )

    publication: dict[str, Any] = {"status": "not_applicable", "commit": None, "tag": None}
    head_relation = "not_applicable"
    reported_live = False
    if release_mode == "required":
        receipt = task.get("release_receipt")
        if not valid_release_receipt(receipt, task):
            _block(blockers, "release_receipt_missing", "Verified release receipt is missing or invalid.", "authority_required")
        if not release_path.is_file():
            _block(blockers, "release_checkpoint_missing", "Durable publisher checkpoint is missing.", "authority_required")
        elif release_state is not None:
            pending = sorted(
                name for name, effect in (release_state.get("effects") or {}).items()
                if not isinstance(effect, dict) or effect.get("status") != "confirmed"
            )
            deployment = release_state.get("deployment") or {}
            if isinstance(deployment, dict) and (deployment.get("effect") or {}).get("status") == "pending":
                pending.append("deployment")
            if pending:
                _block(
                    blockers,
                    "publication_effect_unconfirmed",
                    "Publisher must observe pending effect(s) before retry: " + ", ".join(sorted(set(pending))),
                    "unknown_external_effect",
                )
            if release_state.get("phase") != "published":
                _block(
                    blockers,
                    "publication_readback_pending",
                    f"Publisher phase is {release_state.get('phase')!s}; exact publication/deployment readback is incomplete.",
                    "authority_required",
                )

        manifest = task.get("completion_evidence") or {}
        reference = manifest.get("release") if isinstance(manifest, dict) else None
        if valid_release_receipt(receipt, task) and reference is not None:
            try:
                proof = read_artifact(repo / ".go", reference)
                from .shipping import verify_release_evidence

                verify_release_evidence(repo, task, proof, proof.get("content_digest"), remote=False)
                head_relation = _head_relation(repo, task, proof["commit"])
                if head_relation == "released_commit_not_preserved":
                    _block(
                        blockers,
                        "released_commit_not_preserved",
                        "Current base branch does not preserve the released product commit.",
                        "unsafe_repository",
                    )
                publication = {"status": "verified", "commit": proof["commit"], "tag": proof["tag"]}
                profile_name = release_policy.get("profile")
                project = read_object(repo / ".go/project.json")
                profile = (project.get("release_profiles") or {}).get(profile_name) or {}
                deployment_policy = profile.get("deployment") or {}
                reported_live = deployment_policy.get("mode") == "required" and "deployment" in proof
            except (OSError, ValueError, KeyError, TypeError) as exc:
                _block(blockers, "release_evidence_invalid", str(exc), "unsafe_repository")
        elif valid_release_receipt(receipt, task):
            _block(blockers, "release_evidence_missing", "Content-bound release evidence reference is missing.", "authority_required")
    elif release_mode != "none" or not isinstance(release_policy.get("reason"), str) or not release_policy["reason"].strip():
        _block(blockers, "release_policy_invalid", "Explicit required or reasoned no-release policy is required.", "unsafe_repository")

    priority = ("unknown_external_effect", "unsafe_repository", "authority_required")
    stop_condition = next((condition for condition in priority if any(item["condition"] == condition for item in blockers)), None)
    return {
        "schema": SCHEMA,
        "task_id": task_id,
        "release_policy": release_mode,
        "delivered": not blockers,
        "reported_live": reported_live and not blockers,
        "head_relation": head_relation,
        "publication": publication,
        "workspace_state": workspace.get("state") if workspace else "missing",
        "blockers": blockers,
        "stop_condition": stop_condition,
    }
