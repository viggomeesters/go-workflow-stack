"""Dry-run-first, transactional project stack pin updates."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state_io import atomic_json, repository_lock

STACK_UPDATE_SCHEMA = "go-workflow.stack-update-plan.v1"
ROLLBACK_SCHEMA = "go-workflow.stack-update-rollback.v1"
VERSION_REF_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")


class StackUpdateError(ValueError):
    pass


def _git(stack_repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(stack_repo), *args], text=True, capture_output=True)


def latest_stack_ref(stack_repo: Path) -> str:
    listed = _git(stack_repo, "tag", "--list", "v[0-9]*")
    if listed.returncode != 0:
        raise StackUpdateError(f"cannot list immutable stack tags in {stack_repo}: {listed.stderr.strip()}")
    releases: list[tuple[tuple[int, int, int], str]] = []
    for tag in listed.stdout.splitlines():
        match = VERSION_REF_RE.fullmatch(tag.strip())
        if not match:
            continue
        kind = _git(stack_repo, "cat-file", "-t", f"refs/tags/{tag}")
        if kind.returncode != 0 or kind.stdout.strip() != "tag":
            continue
        releases.append((tuple(int(part) for part in match.group(1).split(".")), tag))
    if not releases:
        raise StackUpdateError(f"no annotated immutable vX.Y.Z stack tags found in {stack_repo}")
    return max(releases)[1]


def plan_stack_update(repo: Path, stack_repo: Path, to_ref: str) -> dict[str, Any]:
    match = VERSION_REF_RE.fullmatch(to_ref)
    if not match:
        raise StackUpdateError("target stack ref must be an immutable vX.Y.Z tag")
    version = match.group(1)
    project_path = repo / ".go" / "project.json"
    try:
        project = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StackUpdateError(f"cannot read project contract: {exc}") from exc
    resolved = _git(stack_repo, "rev-parse", "-q", "--verify", f"refs/tags/{to_ref}^{{commit}}")
    if resolved.returncode != 0 or not resolved.stdout.strip():
        raise StackUpdateError(f"stack ref {to_ref} does not exist in {stack_repo}")
    constants = _git(stack_repo, "show", f"{to_ref}:go_workflow/constants.py")
    if constants.returncode != 0:
        raise StackUpdateError(f"stack ref {to_ref} does not contain go_workflow/constants.py")
    declared = re.search(r'^STACK_VERSION = "([^"]+)"', constants.stdout, re.M)
    declared_version = declared.group(1) if declared else ""
    if declared_version != version:
        raise StackUpdateError(f"stack ref {to_ref} declares version {declared_version or '<missing>'}, expected {version}")
    contract = re.search(r"^CURRENT_CONTRACT_VERSION = (\d+)", constants.stdout, re.M)
    runtime_contract = int(contract.group(1)) if contract else 0
    project_contract = int(project.get("contract_version") or 1)
    if runtime_contract < project_contract:
        raise StackUpdateError(
            f"stack ref {to_ref} supports contract {runtime_contract}, project requires {project_contract}"
        )
    up_to_date = project.get("required_stack_version") == version and project.get("stack_ref") == to_ref
    after = dict(project)
    after.update({"required_stack_version": version, "stack_ref": to_ref})
    return {
        "schema": STACK_UPDATE_SCHEMA,
        "mode": "dry_run",
        "repo": str(repo),
        "stack_repo": str(stack_repo),
        "from_version": project.get("required_stack_version"),
        "from_ref": project.get("stack_ref"),
        "to_version": version,
        "to_ref": to_ref,
        "resolved_commit": resolved.stdout.strip(),
        "runtime_contract_version": runtime_contract,
        "project_contract_version": project_contract,
        "lifecycle_policy_migrated": False,
        "up_to_date": up_to_date,
        "changes": [] if up_to_date else [".go/project.json:required_stack_version", ".go/project.json:stack_ref"],
        "before_project": project,
        "after_project": after,
    }


def apply_stack_update(repo: Path, plan: dict[str, Any]) -> dict[str, Any]:
    from .migrations import pending_lifecycle_findings
    with repository_lock(repo / '.go', 'lifecycle-migration'):
        findings = pending_lifecycle_findings(repo)
        if findings: raise StackUpdateError('; '.join(findings))
        current = json.loads((repo / '.go/project.json').read_text())
        if current != plan.get('before_project'):
            raise StackUpdateError('Project changed after stack update planning; replan without overwriting lifecycle policy')
        return _apply_stack_update(repo, plan)


def _apply_stack_update(repo: Path, plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("up_to_date"):
        result = {key: value for key, value in plan.items() if key not in {"before_project", "after_project"}}
        result.update({"mode": "noop", "rollback_record": None})
        return result
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    slug = f"{stamp}-{plan['to_ref']}-{plan['resolved_commit'][:12]}"
    rollback_path = repo / ".go" / "updates" / f"{slug}.json"
    rollback = {
        "schema": ROLLBACK_SCHEMA,
        "status": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": ".go/project.json",
        "from_ref": plan.get("from_ref"),
        "to_ref": plan["to_ref"],
        "resolved_commit": plan["resolved_commit"],
        "before_project": plan["before_project"],
        "after_project": plan["after_project"],
    }
    atomic_json(rollback_path, rollback)
    try:
        atomic_json(repo / ".go" / "project.json", plan["after_project"])
        rollback["status"] = "applied"
        atomic_json(rollback_path, rollback)
    except BaseException:
        atomic_json(repo / ".go" / "project.json", plan["before_project"])
        rollback["status"] = "rolled_back"
        atomic_json(rollback_path, rollback)
        raise
    result = {key: value for key, value in plan.items() if key not in {"before_project", "after_project"}}
    result.update({"mode": "applied", "rollback_record": str(rollback_path.relative_to(repo))})
    return result


def rollback_stack_update(repo: Path, rollback_record: str) -> None:
    from .migrations import pending_lifecycle_findings
    repo = repo.resolve()
    path = repo / rollback_record
    if path.parent != repo / '.go/updates' or path.resolve() != path.absolute() or path.is_symlink():
        raise StackUpdateError('Rollback must use a repository-owned stack update record')
    with repository_lock(repo / '.go', 'lifecycle-migration'):
        findings = pending_lifecycle_findings(repo)
        if findings: raise StackUpdateError('; '.join(findings))
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema') != ROLLBACK_SCHEMA or data.get('status') not in {'applied', 'rolled_back'}:
            raise StackUpdateError('Invalid stack update rollback record')
        before, after = data.get('before_project'), data.get('after_project')
        current = json.loads((repo / '.go/project.json').read_text())
        if not isinstance(before, dict) or not isinstance(after, dict) or current not in (before, after):
            raise StackUpdateError('Project changed after stack update; preserve adopted lifecycle policy and replan')
        if data['status'] == 'rolled_back' and current != before:
            raise StackUpdateError('Project advanced after recorded rollback')
        if current != before: atomic_json(repo / '.go/project.json', before)
        data['status'] = 'rolled_back'; atomic_json(path, data)
