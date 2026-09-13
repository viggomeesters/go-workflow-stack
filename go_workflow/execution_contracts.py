"""Opt-in execution data contracts; no model dispatch or publication here."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path
from typing import Any

EXECUTION_SCHEMA = 'go-workflow.execution-contract.v1'
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def string_list(value: Any, *, nonempty: bool = True) -> bool:
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(isinstance(v, str) and bool(v.strip()) for v in value))


def validate_phase_contract(data: Any) -> list[str]:
    if not isinstance(data, dict): return ['phase contract must be an object']
    required = {'schema', 'id', 'inputs', 'outputs', 'required_evidence', 'stop_conditions',
                'handoff', 'scope', 'required_outcomes'}
    errors = []
    if set(data) != required: errors.append('phase contract has missing or unknown fields')
    if data.get('schema') != 'go-workflow.phase-contract.v1': errors.append('phase schema mismatch')
    if not isinstance(data.get('id'), str) or not ID_RE.fullmatch(data['id']): errors.append('phase id invalid')
    for field in required - {'schema', 'id', 'scope'}:
        if not string_list(data.get(field)): errors.append(f'phase {field} must be a non-empty string list')
    scope = data.get('scope')
    if not isinstance(scope, dict) or set(scope) != {'read', 'modify'}:
        errors.append('phase scope requires read and modify')
    elif not all(string_list(scope[f], nonempty=False) for f in ('read', 'modify')):
        errors.append('phase scope paths invalid')
    return errors


def validate_phase_profiles(data: Any) -> list[str]:
    if not isinstance(data, dict): return ['phase_profiles must be an object']
    errors = []
    for name, phases in data.items():
        if not ID_RE.fullmatch(name): errors.append('phase profile name invalid')
        if not isinstance(phases, list) or not phases:
            errors.append('phase profile requires phases'); continue
        ids = set()
        for phase in phases:
            errors.extend(validate_phase_contract(phase))
            if isinstance(phase, dict) and isinstance(phase.get('id'), str):
                if phase['id'] in ids: errors.append('duplicate phase id')
                ids.add(phase['id'])
    return errors


def validate_verification_evidence(data: Any) -> list[str]:
    if not isinstance(data, dict): return ['verification evidence must be an object']
    errors = []
    allowed = {'schema', 'status', 'reason', 'command', 'cwd', 'revision', 'worktree_digest',
               'exit_code', 'evidence', 'task_id', 'phase_id', 'requirement_ids'}
    if set(data) - allowed: errors.append('verification evidence unknown fields')
    if data.get('schema') != 'go-workflow.verification-evidence.v1': errors.append('verification evidence schema mismatch')
    status = data.get('status')
    if not isinstance(status, str) or status not in {'missing', 'failed', 'not_applicable', 'passed'}:
        errors.append('verification evidence status invalid')
    for field in ('task_id', 'phase_id'):
        if not isinstance(data.get(field), str) or not ID_RE.fullmatch(data[field]): errors.append(f'verification {field} invalid')
    if not string_list(data.get('requirement_ids')): errors.append('verification requirement_ids required')
    if status == 'not_applicable' and (not isinstance(data.get('reason'), str) or not data['reason'].strip()):
        errors.append('verification not_applicable requires policy reason')
    for field in ('reason', 'command', 'cwd', 'worktree_digest'):
        if field in data and (not isinstance(data[field], str) or not data[field].strip()): errors.append(f'verification {field} invalid')
    if 'revision' in data and (not isinstance(data['revision'], str) or not re.fullmatch('[0-9a-f]{40}', data['revision'])):
        errors.append('verification revision invalid')
    if 'exit_code' in data and type(data['exit_code']) is not int: errors.append('verification exit_code invalid')
    if 'evidence' in data and not string_list(data['evidence']): errors.append('verification evidence references invalid')
    if status in ('passed', 'failed'):
        for field in ('command', 'cwd', 'revision', 'worktree_digest', 'exit_code', 'evidence'):
            if field not in data: errors.append(f'verification {field} required')
        if status == 'passed' and data.get('exit_code') != 0: errors.append('passed verification requires exit_code 0')
        if status == 'failed' and data.get('exit_code') == 0: errors.append('failed verification cannot have exit_code 0')
    return errors


def validate_dependencies(value: Any) -> list[str]:
    if not isinstance(value, list): return ['dependencies must be a list']
    errors, seen = [], set()
    for dep in value:
        if not isinstance(dep, dict):
            errors.append('dependency must be an object'); continue
        if set(dep) != {'project', 'task_id', 'requires'}:
            errors.append('dependency requires only project, task_id and requires fields')
        for field in ('project', 'task_id'):
            if not isinstance(dep.get(field), str) or not ID_RE.fullmatch(dep[field]):
                errors.append(f'dependency {field} invalid')
        if dep.get('requires') not in ('done', 'done_with_required_release_evidence'):
            errors.append('dependency requires invalid')
        key = (str(dep.get('project')), str(dep.get('task_id')))
        if key in seen: errors.append('duplicate dependency')
        seen.add(key)
    return errors


def dependency_findings(repo: Path, task: dict[str, Any], *, readiness: bool = False,
                        candidates: list[dict[str, Any]] | None = None) -> list[str]:
    """Read explicit participant paths; never infer a global/sibling queue.

    Dependency enforcement is opt-in with execution_contract. Old planning
    metadata remains valid without silently changing historical semantics.
    """
    if 'execution_contract' not in task: return []
    errors = validate_execution_contract(task['execution_contract']) + validate_dependencies(task.get('dependencies', []))
    if errors: return errors
    from .worktrees import workflow_root
    repo = workflow_root(repo.resolve()).parent
    cache: dict[tuple[Path, str], dict[str, Any]] = {}
    for item in candidates or []: cache[(repo, item['id'])] = item
    cache[(repo, task['id'])] = task
    seen, visiting = set(), set()

    def read(path):
        data = json.loads(path.read_text())
        if not isinstance(data, dict): raise ValueError(f'object required: {path}')
        return data

    def visit(root, current):
        key = (root, current['id'])
        if key in visiting:
            errors.append(f'dependency cycle: {current["id"]}'); return
        if key in seen: return
        visiting.add(key)
        malformed = validate_dependencies(current.get('dependencies', []))
        errors.extend(malformed)
        if not malformed:
            project = read(root / '.go/project.json')
            for dep in current.get('dependencies', []):
                participant = root
                if dep['project'] != project['id']:
                    mapping = project.get('dependency_projects', {})
                    explicit = mapping.get(dep['project']) if isinstance(mapping, dict) else None
                    if not isinstance(explicit, str) or not explicit.strip():
                        errors.append(f'dependency participant not configured: {dep["project"]}'); continue
                    participant = workflow_root((root / explicit).resolve()).parent
                participant_project = read(participant / '.go/project.json')
                if (participant_project.get('schema') != 'go-workflow.repo-local.project.v1'
                        or participant_project.get('kind') != 'project'
                        or participant_project.get('source_of_truth') != 'repo-local'):
                    errors.append(f'dependency participant contract invalid: {dep["project"]}'); continue
                if participant_project.get('id') != dep['project']:
                    errors.append(f'dependency participant identity mismatch: {dep["project"]}'); continue
                depkey = (participant, dep['task_id'])
                if depkey not in cache:
                    paths = [participant / '.go/tasks' / state / f'{dep["task_id"]}.json'
                             for state in ('open', 'active', 'blocked', 'done')
                             if (participant / '.go/tasks' / state / f'{dep["task_id"]}.json').is_file()]
                    if len(paths) != 1:
                        errors.append(f'dependency missing or duplicated: {dep["project"]}:{dep["task_id"]}'); continue
                    value = read(paths[0])
                    if value.get('id') != dep['task_id'] or value.get('project') != dep['project'] or value.get('status') != paths[0].parent.name:
                        errors.append(f'dependency task identity/state mismatch: {dep["task_id"]}'); continue
                    cache[depkey] = value
                target = cache[depkey]
                if readiness and key == (repo, task['id']):
                    if (target.get('status') != 'done' or target.get('review_status', 'approved') != 'approved'
                            or target.get('work_status', 'completed') != 'completed'):
                        errors.append(f'dependency not done/approved: {dep["task_id"]}')
                    elif dep['requires'] == 'done_with_required_release_evidence':
                        receipt = target.get('release_receipt')
                        if not valid_release_receipt(receipt, target):
                            errors.append(f'dependency verified release receipt required: {dep["task_id"]}')
                visit(participant, target)
        visiting.remove(key)
        seen.add(key)
    try:
        visit(repo, task)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        errors.append(f'dependency state unavailable or invalid: {exc}')
    return list(dict.fromkeys(errors))


def valid_release_receipt(receipt: Any, task: dict[str, Any]) -> bool:
    return (isinstance(receipt, dict)
            and receipt.get('schema') == 'go-workflow.release-receipt.v1'
            and receipt.get('status') == 'verified'
            and receipt.get('task_id') == task.get('id') and receipt.get('project') == task.get('project')
            and isinstance(receipt.get('commit'), str) and bool(re.fullmatch('[0-9a-f]{40}', receipt['commit']))
            and isinstance(receipt.get('evidence'), list) and bool(receipt['evidence'])
            and all(isinstance(e, str) and e.strip() for e in receipt['evidence']))


def resolve_execution_contract(project: dict[str, Any], override: Any = None) -> dict[str, Any] | None:
    """Freeze project defaults plus section-level task overrides at intake."""
    defaults = project.get('execution_defaults')
    if defaults is None and override is None:
        return None
    if defaults is not None and not isinstance(defaults, dict):
        raise ValueError('execution_defaults must be an object')
    if override is not None and not isinstance(override, dict):
        raise ValueError('execution_contract must be an object')
    result = deepcopy(defaults or {})
    for key, value in (override or {}).items():
        if key in {'model', 'critic_model', 'release', 'workspace'} and isinstance(value, dict):
            result[key] = {**result.get(key, {}), **deepcopy(value)}
        else:
            result[key] = deepcopy(value)
    return result


def validate_execution_contract(data: Any, *, partial: bool = False) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ['execution_contract must be an object']
    allowed = {'schema', 'task_kind', 'model', 'critic_model', 'release', 'workspace', 'phase_profile'}
    if data.keys() - allowed:
        errors.append('execution_contract unknown fields: ' + ', '.join(sorted(data.keys() - allowed)))
    if (not partial or 'schema' in data) and data.get('schema') != EXECUTION_SCHEMA:
        errors.append('execution_contract schema mismatch')
    if (not partial or 'task_kind' in data) and data.get('task_kind') not in ('product', 'mechanical', 'no_change', 'smoke'):
        errors.append('execution_contract task_kind must be product, mechanical, no_change or smoke')
    for key in ('model', 'critic_model'):
        if key not in data:
            if not partial and key == 'model': errors.append('execution_contract model required')
            continue
        model = data[key]
        if not isinstance(model, dict):
            errors.append(f'execution_contract {key} must be an object'); continue
        if model.keys() - {'id', 'effort'}: errors.append(f'execution_contract {key} unknown fields')
        if (not partial or 'id' in model) and (not isinstance(model.get('id'), str) or not model['id'].strip()):
            errors.append(f'execution_contract {key}.id required')
        if (not partial or 'effort' in model) and model.get('effort') not in EFFORTS:
            errors.append(f'execution_contract {key}.effort invalid')
    release = data.get('release')
    if release is None and not partial: errors.append('execution_contract release required')
    if 'release' in data:
        if not isinstance(release, dict):
            errors.append('execution_contract release must be an object')
        else:
            if release.keys() - {'mode', 'profile', 'reason'}: errors.append('execution_contract release unknown fields')
            if (not partial or 'mode' in release) and release.get('mode') not in ('required', 'none'):
                errors.append('execution_contract release.mode invalid')
            if release.get('mode') == 'none' and not partial:
                if not isinstance(release.get('reason'), str) or not release['reason'].strip():
                    errors.append('execution_contract non-release requires a reason')
                if data.get('task_kind') == 'product': errors.append('execution_contract product requires release')
            for key in ('profile', 'reason'):
                if key in release and (not isinstance(release[key], str) or not release[key].strip()):
                    errors.append(f'execution_contract release.{key} must be non-empty')
    workspace = data.get('workspace')
    if 'workspace' in data:
        if not isinstance(workspace, dict):
            errors.append('execution_contract workspace must be an object')
        else:
            if workspace.keys() - {'mode', 'base_branch', 'control_state'}: errors.append('execution_contract workspace unknown fields')
            if (not partial or 'mode' in workspace) and workspace.get('mode') not in ('task_worktree', 'current'): errors.append('execution_contract workspace.mode invalid')
            if (not partial or 'control_state' in workspace) and workspace.get('control_state') != 'repo_local_single_writer': errors.append('execution_contract workspace.control_state invalid')
            if 'base_branch' in workspace and (not isinstance(workspace['base_branch'], str) or not workspace['base_branch'].strip()):
                errors.append('execution_contract workspace.base_branch must be non-empty')
    if 'phase_profile' in data and (not isinstance(data['phase_profile'], str) or not data['phase_profile'].strip()):
        errors.append('execution_contract phase_profile must be non-empty')
    return errors
