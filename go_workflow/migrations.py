"""Pure, reviewable migrations for repo-local `.go` contracts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .constants import CURRENT_CONTRACT_VERSION, STACK_REF, STACK_VERSION

MIGRATION_PLAN_SCHEMA = "go-workflow.migration-plan.v1"


def plan_contract_migration(
    project: dict[str, Any], hierarchy: dict[str, Any], task_ids: list[str] | None = None
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
        "required_stack_version": STACK_VERSION,
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

    linked_task_ids: set[str] = set()
    for epic in migrated_hierarchy.get("epics", []):
        linked_task_ids.update(epic.get("tasks", []))
        for feature in epic.get("features", []):
            linked_task_ids.update(feature.get("tasks", []))
    unlinked_task_ids = sorted(set(task_ids or []) - linked_task_ids)
    if unlinked_task_ids:
        epics = migrated_hierarchy.setdefault("epics", [])
        target = next((epic for epic in epics if epic.get("id") == "workflow"), None)
        if target is None:
            target = epics[0] if epics else {"id": "workflow", "title": "Workflow", "features": [], "tasks": []}
            if not epics:
                epics.append(target)
        target.setdefault("tasks", []).extend(unlinked_task_ids)
        changes.append({
            "path": ".go/hierarchy.json",
            "operations": [f"link historical tasks: {', '.join(unlinked_task_ids)}"],
        })

    plan = {
        "schema": MIGRATION_PLAN_SCHEMA,
        "from_version": from_version,
        "to_version": CURRENT_CONTRACT_VERSION,
        "changes": changes,
        "write_required": bool(changes),
        "applied": False,
    }
    return plan, {"project.json": migrated_project, "hierarchy.json": migrated_hierarchy}

# Lifecycle adoption is deliberately separate from runtime pins and v1→v2 shape.
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from .state_io import atomic_json, repository_lock

SETTINGS = 'go-workflow.lifecycle-settings.v1'
ADOPTION = 'go-workflow.lifecycle-adoption.v1'
JOURNAL = 'go-workflow.lifecycle-migration-journal.v1'
SETTING_FIELDS = {'execution_defaults', 'release_profiles', 'phase_profiles', 'default_verification'}


class MigrationError(ValueError):
    pass


def _json(value): return json.dumps(value, indent=2, ensure_ascii=False) + '\n'
def _hash(text): return hashlib.sha256(text.encode()).hexdigest()
def _read(path): return json.loads(path.read_text())


def atomic_write_text(path, text):
    """Preserve snapshot permissions before replacement, including crash recovery."""
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text); stream.flush(); os.fchmod(stream.fileno(), mode); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configured_project(project, settings):
    from . import cli as api
    from .execution_contracts import validate_execution_contract
    if (not isinstance(settings, dict) or settings.get('schema') != SETTINGS
            or not {'schema', 'execution_defaults'}.issubset(settings)
            or set(settings) - SETTING_FIELDS - {'schema'}):
        raise MigrationError('Explicit lifecycle settings required; unknown settings or authority fields are not allowed')
    candidate = {**deepcopy(project), **{key: deepcopy(value) for key, value in settings.items() if key != 'schema'}}
    for key in ('release_profiles', 'phase_profiles'):
        values = settings.get(key)
        existing = project.get(key, {})
        if isinstance(values, dict) and isinstance(existing, dict):
            for name, value in values.items():
                if name in existing and value != existing[name]:
                    raise MigrationError('Existing ' + key + ' entry cannot be replaced by adoption; choose a new profile name: ' + name)
            candidate[key] = {**deepcopy(existing), **deepcopy(values)}
    errors = api.validate_project(candidate, '.go/project.json') + validate_execution_contract(settings['execution_defaults'])
    if errors: raise MigrationError('; '.join(errors))
    contract = settings['execution_defaults']; release = contract.get('release', {})
    if release.get('mode') == 'required':
        selected = candidate.get('release_profiles', {}).get(release.get('profile'))
        if not isinstance(selected, dict) or 'publication' not in selected:
            errors.append('Lifecycle adoption requires an explicit configured publisher for required releases')
    if contract.get('phase_profile') and contract['phase_profile'] not in candidate.get('phase_profiles', {}):
        errors.append('Selected phase profile is not configured')
    if not candidate.get('default_verification'):
        errors.append('Lifecycle adoption requires explicit default verification commands')
    if errors: raise MigrationError('; '.join(errors))
    return candidate


def pending_lifecycle_findings(repo):
    findings = []
    for path in sorted((Path(repo) / '.go/migrations').glob('*.json')):
        try:
            record = _read(path)
            if record.get('schema') != JOURNAL: raise MigrationError('Invalid lifecycle journal schema')
            if record.get('status') not in {'applied', 'rolled_back'}:
                findings.append('Unfinished lifecycle migration: ' + str(path.relative_to(repo)) + '; explicitly resume or rollback')
        except (OSError, ValueError, AttributeError, TypeError):
            findings.append('Unreadable lifecycle migration journal: ' + str(path))
    return findings


def plan_lifecycle_adoption(repo, settings=None):
    from . import cli as api
    repo = Path(repo).resolve(); root = repo / '.go'
    project = _read(root / 'project.json')
    candidate = configured_project(project, settings) if settings is not None else project
    documents, tasks = {}, []
    if candidate != project: documents['.go/project.json'] = _json(candidate)
    for path in sorted((root / 'tasks').glob('*/*.json')):
        task = _read(path); status = task['status']; disposition = 'preserved_' + status
        if status == 'open':
            if 'execution_contract' in task: disposition = 'existing_contract'
            elif settings is None or task.get('claim', {}).get('agent'):
                disposition = 'needs_configuration'
            else:
                adopted = deepcopy(task)
                adopted['execution_contract'] = deepcopy(candidate['execution_defaults'])
                api.ensure_intake_outcomes(adopted)
                errors = api.validate_task(adopted, task['id'], expected_status='open')
                if errors:
                    raise MigrationError(
                        f"{path.relative_to(repo)}: lifecycle adoption requires explicit resolution "
                        "of task metadata before opt-in; no dependency or evidence semantics "
                        "were inferred: " + '; '.join(errors))
                documents[str(path.relative_to(repo))] = _json(adopted); disposition = 'adopted'
        tasks.append({'id': task['id'], 'status': status, 'disposition': disposition})
    changes = [{'path': name, 'before_sha256': _hash((repo / name).read_text()), 'after_sha256': _hash(text)}
               for name, text in sorted(documents.items())]
    return {'schema': ADOPTION, 'project': project['id'], 'status': 'dry_run', 'write_required': bool(changes),
            'settings_configured': settings is not None, 'changes': changes, 'tasks': tasks,
            'authority': 'Configuration only; no claim, execution, push or deployment authority granted'}, documents


def _safe_path(repo, name):
    if not isinstance(name, str) or not (name == '.go/project.json' or re.fullmatch(r'\.go/tasks/open/[A-Za-z0-9][A-Za-z0-9._-]*\.json', name)):
        raise MigrationError('Invalid migration target path')
    path = repo / name
    if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file():
        raise MigrationError('Migration target missing, moved or linked: ' + name)
    return path


def _journal_path(repo, name):
    if not isinstance(name, str) or not re.fullmatch(r'\.go/migrations/[0-9a-f]{32}\.json', name):
        raise MigrationError('Invalid lifecycle journal path')
    path = repo / name
    if path.is_symlink() or path.resolve() != path.absolute(): raise MigrationError('Lifecycle journal path is linked')
    return path


@contextmanager
def adoption_lock(repo):
    from .worktrees import require_run_idle
    root = repo / '.go'
    with repository_lock(root, 'lifecycle-migration'), repository_lock(root, 'workspace-integration'):
        with ExitStack() as locks:
            ids = sorted({p.stem for p in (root / 'tasks').glob('*/*.json')})
            for task_id in ids:
                locks.enter_context(repository_lock(root, 'managed-run-' + task_id, .1))
                locks.enter_context(repository_lock(root, 'task-' + task_id))
            if list((root / 'tasks/active').glob('*.json')):
                raise MigrationError('Lifecycle migration requires a quiescent repository; active tasks exist')
            for path in (root / 'workspaces').glob('*.json'): require_run_idle(_read(path))
            yield


def _load_journal(repo, name):
    path = _journal_path(repo, name)
    record = _read(path)
    fields = {'schema', 'status', 'repo', 'project', 'entries', 'entries_sha256', 'inventory', 'plan'}
    if (not isinstance(record, dict) or set(record) != fields or record['schema'] != JOURNAL
            or record['repo'] != str(repo) or record['status'] not in ('pending', 'applied', 'rolling_back', 'rolled_back')
            or not isinstance(record['entries'], dict) or not record['entries']
            or not isinstance(record['inventory'], dict)
            or not isinstance(record['plan'], dict) or record['plan'].get('schema') != ADOPTION
            or record['plan'].get('project') != record['project']
            or record['entries_sha256'] != _hash(_json(record['entries']))):
        raise MigrationError('Invalid lifecycle migration journal')
    for name, entry in record['entries'].items():
        _safe_path(repo, name)
        if (not isinstance(entry, dict) or set(entry) != {'before', 'after', 'mode'}
                or not all(isinstance(entry[key], str) for key in ('before', 'after'))
                or type(entry['mode']) is not int or not 0 <= entry['mode'] <= 0o777):
            raise MigrationError('Invalid migration snapshot')
        for side in ('before', 'after'):
            value = json.loads(entry[side])
            if name == '.go/project.json':
                if value.get('id') != record['project']: raise MigrationError('Migration project identity changed')
            elif value.get('status') != 'open' or value.get('id') != Path(name).stem:
                raise MigrationError('Migration task identity changed')
    return path, record


def _check_entries(repo, record, allowed):
    inventory = {str(path.relative_to(repo)): _hash(path.read_text())
                 for path in (repo / '.go/tasks').glob('*/*.json')}
    if set(inventory) != set(record['inventory']) or any(digest != record['inventory'][name]
            for name, digest in inventory.items() if name not in record['entries']):
        raise MigrationError('Task inventory changed; preserve advanced state and reconcile migration')
    for name, entry in record['entries'].items():
        path = _safe_path(repo, name)
        if path.read_text() not in [entry[key] for key in allowed] or stat.S_IMODE(path.stat().st_mode) != entry['mode']:
            raise MigrationError('Migration target changed/drifted; preserve and reconcile: ' + name)


def _recover(repo, name, *, rollback, apply):
    from . import cli as api
    path, record = _load_journal(repo, name)
    status = record['status']
    if not rollback and status in ('rolling_back', 'rolled_back'):
        raise MigrationError('Rollback journal cannot resume forward adoption')
    allowed = ('before', 'after') if status in ('pending', 'rolling_back') else ('after',) if status == 'applied' else ('before',)
    _check_entries(repo, record, allowed)
    target_status = 'rolled_back' if rollback else 'applied'
    if not apply: return {**record['plan'], 'status': 'rollback_dry_run' if rollback else 'resume_dry_run', 'journal': name}
    if status == target_status: return {**record['plan'], 'status': status, 'journal': name}
    if rollback:
        record['status'] = 'rolling_back'; atomic_json(path, record)
    for name_, entry in record['entries'].items():
        target = _safe_path(repo, name_); desired = entry['before' if rollback else 'after']
        _check_entries(repo, record, ('before', 'after'))
        if target.read_text() != desired:
            atomic_write_text(target, desired)
    errors = api.validate_repo(repo, skip_lifecycle_migration=True)
    if errors: raise MigrationError('Migration validation failed; explicitly resume or rollback: ' + '; '.join(errors))
    record['status'] = target_status; atomic_json(path, record)
    return {**record['plan'], 'status': target_status, 'journal': name}


def adopt_lifecycle(repo, settings=None, *, apply=False):
    from . import cli as api
    repo = Path(repo).resolve()
    if not apply:
        plan, _ = plan_lifecycle_adoption(repo, settings); return plan
    with adoption_lock(repo):
        errors = api.validate_repo(repo)
        if errors: raise MigrationError('; '.join(errors))
        if settings is None: raise MigrationError('Explicit lifecycle settings are required for adoption')
        plan, documents = plan_lifecycle_adoption(repo, settings)
        if not documents: return {**plan, 'status': 'noop', 'journal': None}
        entries = {name: {'before': _safe_path(repo, name).read_text(), 'after': text,
                          'mode': stat.S_IMODE((repo / name).stat().st_mode)} for name, text in sorted(documents.items())}
        name = '.go/migrations/' + uuid.uuid4().hex + '.json'; path = _journal_path(repo, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, {'schema': JOURNAL, 'status': 'pending', 'repo': str(repo), 'project': plan['project'],
                          'entries': entries, 'entries_sha256': _hash(_json(entries)),
                          'inventory': {str(path.relative_to(repo)): _hash(path.read_text())
                                        for path in (repo / '.go/tasks').glob('*/*.json')}, 'plan': plan})
        return _recover(repo, name, rollback=False, apply=True)


def resume_lifecycle(repo, journal, *, apply=False):
    repo = Path(repo).resolve()
    if not apply: return _recover(repo, journal, rollback=False, apply=False)
    with adoption_lock(repo): return _recover(repo, journal, rollback=False, apply=True)


def rollback_lifecycle(repo, journal, *, apply=False):
    repo = Path(repo).resolve()
    if not apply: return _recover(repo, journal, rollback=True, apply=False)
    with adoption_lock(repo): return _recover(repo, journal, rollback=True, apply=True)
