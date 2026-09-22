"""Dry-run-first, transactional project stack pin updates."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state_io import atomic_json, repository_lock
from .agents_gateway import AgentsGatewayError, apply_agents_gateway, plan_agents_gateway, restore_agents_gateway

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


def workflow_inventory(repo: Path) -> dict[str, str]:
    """Fingerprint durable workflow bytes; transient lock files are not state."""
    root = repo / '.go'
    if root.is_symlink():
        raise StackUpdateError('Cannot isolate a linked .go root')
    files = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if relative.parts[0] == 'locks':
            continue
        if path.is_symlink():
            raise StackUpdateError('Cannot isolate linked workflow path: .go/' + str(relative))
        if path.is_file():
            files[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def workflow_graph(repo: Path) -> dict[Path, dict[str, str]]:
    """Only follow explicitly configured local participants; never guess siblings."""
    graph, pending = {}, [repo.resolve()]
    while pending:
        current = pending.pop(0)
        if current in graph:
            continue
        if len(graph) >= 64:
            raise StackUpdateError('Upgrade preview exceeds 64 explicit participants')
        graph[current] = workflow_inventory(current)
        path = current / '.go/project.json'
        if not path.is_file():
            continue
        project = json.loads(path.read_text())
        mapping = project.get('dependency_projects', {})
        if isinstance(mapping, dict):
            pending.extend((current / value).resolve() for value in mapping.values()
                           if isinstance(value, str) and value.strip())
    return graph


def _preview_compatibility(repo: Path, stack_repo: Path, commit: str,
                          project: dict[str, Any]) -> dict[str, Any]:
    before = workflow_graph(repo)
    digest = hashlib.sha256(json.dumps({str(key): value for key, value in before.items()},
                                      sort_keys=True).encode()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='go-upgrade-preview-') as directory:
        base = Path(directory).resolve(); runtime = base / 'runtime'; snapshot = base / 'project'
        runtime.mkdir()
        locations = {original: snapshot if original == repo else base / f'participant-{index}'
                     for index, original in enumerate(before)}
        for original, files in before.items():
            destination = locations[original]; destination.mkdir()
            for name in files:
                source = original / '.go' / name; target = destination / '.go' / name
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
            project_path = destination / '.go/project.json'
            if not project_path.is_file():
                continue
            candidate = dict(project) if original == repo else json.loads(project_path.read_text())
            mapping = candidate.get('dependency_projects')
            if isinstance(mapping, dict):
                candidate['dependency_projects'] = {
                    key: str(locations[(original / value).resolve()])
                    if isinstance(value, str) and value.strip() else value for key, value in mapping.items()}
            atomic_json(project_path, candidate)
        archive_path = base / 'runtime.tar'
        with archive_path.open('wb') as output:
            result = subprocess.run(['git', '-C', str(stack_repo), 'archive', '--format=tar', commit],
                                    stdout=output, stderr=subprocess.PIPE, timeout=30)
        if result.returncode:
            raise StackUpdateError('Cannot materialize exact target runtime: ' + result.stderr.decode(errors='replace'))
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                name = Path(member.name)
                if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir()):
                    raise StackUpdateError('Unsafe or linked target runtime archive entry: ' + member.name)
            # Members above are restricted to relative regular files/directories.
            archive.extractall(runtime, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
        entry = runtime / 'cli/go.py'
        if not entry.is_file():
            raise StackUpdateError('Target runtime has no cli/go.py validator')
        environment = {key: value for key, value in os.environ.items()
                       if key not in {'GO_STACK', 'GO_STACK_ALLOW_DEV', 'PYTHONPATH'} and not key.startswith('GIT_')}
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        # A target runtime that introduces or upgrades the root gateway must be
        # able to prove the resulting contract before the real repository is
        # touched. Copy existing instructions, then let that exact runtime
        # perform the same bounded repair it will require after update.
        target_has_gateway = (runtime / 'go_workflow/agents_gateway.py').is_file()
        for original, destination in locations.items():
            variants = [entry for entry in original.iterdir() if entry.name.casefold() == 'agents.md']
            if len(variants) == 1 and variants[0].is_file() and not variants[0].is_symlink():
                shutil.copy2(variants[0], destination / variants[0].name)
            if not target_has_gateway:
                continue
            synchronized = subprocess.run(
                [sys.executable, '-I', str(entry), 'agents', 'sync', str(destination), '--apply', '--json'],
                cwd=destination, env=environment, capture_output=True, text=True, timeout=60,
            )
            if synchronized.returncode:
                raise StackUpdateError(
                    'Target runtime cannot synchronize root AGENTS.md: '
                    + (synchronized.stderr.strip() or synchronized.stdout.strip())
                )
        snapshot_before = {path: workflow_inventory(path) for path in locations.values()}
        result = subprocess.run([sys.executable, '-I', str(entry), 'validate', str(snapshot)],
                                cwd=snapshot, env=environment, capture_output=True, text=True, timeout=60)
        if any(workflow_inventory(path) != value for path, value in snapshot_before.items()):
            raise StackUpdateError('Target validator mutated its workflow snapshot; upgrade refused')
        stdout, stderr = result.stdout, result.stderr
        for original, destination in locations.items():
            stdout = stdout.replace(str(destination), str(original))
            stderr = stderr.replace(str(destination), str(original))
    if workflow_graph(repo) != before:
        raise StackUpdateError('Workflow state changed during upgrade preview; retry against current state')
    errors = stderr.strip().splitlines() if result.returncode else []
    if result.returncode and not errors:
        errors = stdout.strip().splitlines() or [f'Target validator exited {result.returncode}']
    return {'status': 'passed' if not result.returncode else 'failed', 'resolved_commit': commit,
            'source_digest': digest, 'exit_code': result.returncode, 'errors': errors,
            'stdout': stdout, 'stderr': stderr, 'validation': 'exact-target-workflow-snapshot'}


def preview_compatibility(repo: Path, stack_repo: Path, commit: str,
                          project: dict[str, Any]) -> dict[str, Any]:
    try:
        return _preview_compatibility(repo, stack_repo, commit, project)
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired, tarfile.TarError) as exc:
        raise StackUpdateError(f'Cannot complete isolated target validation: {exc}') from exc


def plan_stack_update(repo: Path, stack_repo: Path, to_ref: str) -> dict[str, Any]:
    repo, stack_repo = repo.resolve(), stack_repo.resolve()
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
    if _git(stack_repo, 'cat-file', '-t', f'refs/tags/{to_ref}').stdout.strip() != 'tag':
        raise StackUpdateError('Target stack ref must be an annotated immutable tag')
    constants = _git(stack_repo, "show", f"{resolved.stdout.strip()}:go_workflow/constants.py")
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
    try:
        gateway = plan_agents_gateway(repo)
    except AgentsGatewayError as exc:
        raise StackUpdateError(str(exc)) from exc
    gateway_summary = {key: value for key, value in gateway.items() if key not in {'before', 'after'}}
    project_up_to_date = project.get("required_stack_version") == version and project.get("stack_ref") == to_ref
    up_to_date = project_up_to_date and gateway['action'] == 'none'
    after = dict(project)
    after.update({"required_stack_version": version, "stack_ref": to_ref})
    changes = [] if project_up_to_date else [".go/project.json:required_stack_version", ".go/project.json:stack_ref"]
    if gateway['action'] != 'none':
        changes.append('AGENTS.md:bounded-go-gateway')
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
        "changes": changes,
        "agents_gateway": gateway_summary,
        "before_project": project,
        "after_project": after,
        "compatibility": preview_compatibility(repo, stack_repo, resolved.stdout.strip(), after),
    }


def apply_stack_update(repo: Path, plan: dict[str, Any]) -> dict[str, Any]:
    from .migrations import pending_lifecycle_findings
    repo = repo.resolve()
    if (not isinstance(plan, dict) or not isinstance(plan.get('compatibility'), dict)
            or not isinstance(plan.get('stack_repo'), str) or not isinstance(plan.get('to_ref'), str)):
        raise StackUpdateError('An exact-target compatibility preview is required before applying')
    with repository_lock(repo / '.go', 'lifecycle-migration'):
        findings = pending_lifecycle_findings(repo)
        if findings: raise StackUpdateError('; '.join(findings))
        current = json.loads((repo / '.go/project.json').read_text())
        if current != plan.get('before_project'):
            raise StackUpdateError('Project changed after stack update planning; replan without overwriting lifecycle policy')
        if plan.get('compatibility', {}).get('status') != 'passed':
            raise StackUpdateError('Target runtime compatibility failed: ' + '; '.join(plan['compatibility'].get('errors', [])))
        # Recompute rather than trusting a persisted or caller-edited receipt.
        fresh = plan_stack_update(repo, Path(plan['stack_repo']), plan['to_ref'])
        fields = ('repo', 'to_ref', 'to_version', 'resolved_commit', 'before_project', 'after_project')
        if (any(plan.get(key) != fresh[key] for key in fields)
                or plan.get('agents_gateway') != fresh.get('agents_gateway')
                or plan['compatibility'].get('source_digest') != fresh['compatibility']['source_digest']):
            raise StackUpdateError('Workflow, target or preview changed; replan before applying')
        if fresh['compatibility']['status'] != 'passed':
            raise StackUpdateError('Target runtime compatibility changed; inspect a fresh preview')
        return _apply_stack_update(repo, fresh)


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
    gateway = plan_agents_gateway(repo)
    rollback['before_agents'] = gateway['before']
    rollback['before_agents_path'] = gateway['source_path']
    rollback['after_agents_sha256'] = gateway['after_sha256']
    atomic_json(rollback_path, rollback)
    try:
        atomic_json(repo / ".go" / "project.json", plan["after_project"])
        apply_agents_gateway(repo, gateway)
        rollback["status"] = "applied"
        atomic_json(rollback_path, rollback)
    except BaseException:
        atomic_json(repo / ".go" / "project.json", plan["before_project"])
        restore_agents_gateway(repo, gateway)
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
        if 'before_agents' in data:
            restore_agents_gateway(repo, {
                'before': data.get('before_agents'),
                'source_path': data.get('before_agents_path'),
            })
        data['status'] = 'rolled_back'; atomic_json(path, data)
