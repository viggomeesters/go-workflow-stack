"""Explicit task workspaces; canonical .go owns state, Git markers bind workers."""
from __future__ import annotations

import json
from contextlib import contextmanager
import os
import re
import subprocess
import uuid
from pathlib import Path

from .state_io import atomic_json, atomic_write_text, repository_lock

SCHEMA = 'go-workflow.workspace-state.v1'
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
SHA = re.compile(r'^[0-9a-f]{40}$')


class WorkspaceError(ValueError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE'):
        env.pop(key, None)
    result = subprocess.run(['git', '-C', str(repo), *args], env=env, text=True, capture_output=True)
    if check and result.returncode:
        raise WorkspaceError(result.stderr.strip() or 'Git operation failed')
    return result


def git_text(repo, *args):
    return git(repo, *args).stdout.rstrip('\n')


def is_git_checkout(repo: Path) -> bool:
    if not repo.is_dir(): return False
    result = git(repo, 'rev-parse', '--show-toplevel', check=False)
    return result.returncode == 0 and Path(result.stdout.rstrip('\n')).resolve() == repo.resolve()


def common_dir(repo):
    return Path(git_text(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()


def marker_path(repo):
    return Path(git_text(repo, 'rev-parse', '--absolute-git-dir')) / 'go-workspace.json'


def read_object(path):
    try: value = json.loads(path.read_text())
    except (OSError, ValueError) as exc: raise WorkspaceError(f'Workspace state unavailable: {path}') from exc
    if not isinstance(value, dict): raise WorkspaceError(f'Workspace state must be an object: {path}')
    return value


def active_task(control, task_id, owner=None):
    matches = [control / '.go/tasks' / state / (task_id + '.json') for state in ('open', 'active', 'blocked', 'done')
               if (control / '.go/tasks' / state / (task_id + '.json')).is_file()]
    if len(matches) != 1: raise WorkspaceError('Workspace task is missing or duplicated')
    task = read_object(matches[0])
    project = read_object(control / '.go/project.json')
    if task.get('id') != task_id or task.get('project') != project.get('id') or task.get('status') != matches[0].parent.name:
        raise WorkspaceError('Workspace task identity/state mismatch')
    if owner is not None and (task.get('status') != 'active' or (task.get('claim') or {}).get('agent') != owner):
        raise WorkspaceError('Workspace requires the matching active task owner')
    return task


def registry_path(control, task_id):
    if not isinstance(task_id, str) or not ID.fullmatch(task_id): raise WorkspaceError('Invalid workspace task id')
    return control / '.go/workspaces' / (task_id + '.json')


def validate_record(record):
    required = {'schema', 'task_id', 'owner', 'run_id', 'control_repo', 'path', 'branch',
                'base_branch', 'base_commit', 'common_dir', 'repository_id', 'generation', 'state'}
    if not isinstance(record, dict) or not required.issubset(record): raise WorkspaceError('Incomplete workspace registry')
    if record['schema'] != SCHEMA: raise WorkspaceError('Workspace schema mismatch')
    for name in required - {'schema'}:
        if not isinstance(record[name], str) or not record[name]: raise WorkspaceError('Invalid workspace ' + name)
    if not ID.fullmatch(record['task_id']) or not SHA.fullmatch(record['base_commit']): raise WorkspaceError('Invalid workspace task/base identity')
    if record['state'] not in {'creating', 'ready', 'integrated', 'cleanup_failed', 'cleaned'}:
        raise WorkspaceError('Invalid workspace state')
    optional = {'integration', 'ownership_history', 'cleanup_error', 'recovery_action'}
    if set(record) - required - optional: raise WorkspaceError('Unknown workspace registry fields')
    for name in ('owner', 'run_id'):
        if not ID.fullmatch(record[name]): raise WorkspaceError('Invalid workspace ' + name)
    for name in ('repository_id', 'generation'):
        if not re.fullmatch('[0-9a-f]{32}', record[name]): raise WorkspaceError('Invalid workspace ' + name)
    for name in ('control_repo', 'path', 'common_dir'):
        if not Path(record[name]).is_absolute(): raise WorkspaceError('Workspace paths must be absolute')
    if 'integration' in record:
        proof = record['integration']
        if (not isinstance(proof, dict) or set(proof) != {'workspace_head', 'integrated_commit'}
                or not all(isinstance(value, str) and SHA.fullmatch(value) for value in proof.values())):
            raise WorkspaceError('Invalid workspace integration proof')
    if record['state'] in {'integrated', 'cleanup_failed', 'cleaned'} and 'integration' not in record:
        raise WorkspaceError('Workspace state requires integration proof')
    for name in ('cleanup_error', 'recovery_action'):
        if name in record and (not isinstance(record[name], str) or not record[name]): raise WorkspaceError('Invalid workspace ' + name)
    if 'ownership_history' in record:
        history = record['ownership_history']
        if (not isinstance(history, list) or any(not isinstance(item, dict) or set(item) != {'owner', 'run_id'}
                or not all(isinstance(value, str) and ID.fullmatch(value) for value in item.values()) for item in history)):
            raise WorkspaceError('Invalid workspace ownership history')
    return record


def marker_for(record):
    return {name: record[name] for name in ('schema', 'task_id', 'control_repo', 'path', 'repository_id', 'generation')}


def verify_workspace(record, *, pending=False):
    validate_record(record)
    control, workspace = Path(record['control_repo']), Path(record['path'])
    if not is_git_checkout(control) or not is_git_checkout(workspace): raise WorkspaceError('Workspace/control checkout is missing')
    common = common_dir(control)
    if str(common) != record['common_dir'] or common_dir(workspace) != common:
        raise WorkspaceError('Workspace repository identity mismatch')
    identity = common / 'go-workflow-repository-id'
    if not identity.is_file() or identity.read_text().strip() != record['repository_id']:
        raise WorkspaceError('Workspace repository id mismatch')
    if git_text(workspace, 'symbolic-ref', '--quiet', '--short', 'HEAD') != record['branch']:
        raise WorkspaceError('Workspace branch mismatch')
    if git(workspace, 'merge-base', '--is-ancestor', record['base_commit'], 'HEAD', check=False).returncode:
        raise WorkspaceError('Workspace no longer descends from its registered base')
    marker = marker_path(workspace)
    if marker.exists():
        if read_object(marker) != marker_for(record): raise WorkspaceError('Foreign workspace marker')
    elif not pending:
        raise WorkspaceError('Workspace marker is missing; recover its recorded creation first')
    if pending and (git_text(workspace, 'rev-parse', 'HEAD') != record['base_commit']
                    or git_text(workspace, 'status', '--porcelain', '--untracked-files=all')):
        raise WorkspaceError('Interrupted creation contains unexpected work; preserve and inspect it')
    return record


def create_workspace(control, task_id, owner, run_id, path, branch, base_branch, base_commit):
    if Path(path).is_symlink(): raise WorkspaceError('Foreign existing workspace symlink; preserve and inspect it')
    control, path = Path(control).resolve(), Path(path).resolve()
    if not is_git_checkout(control) or not (control / '.git').is_dir():
        raise WorkspaceError('Workspace creation requires the explicit primary control checkout')
    for label, value in (('owner', owner), ('run_id', run_id)):
        if not isinstance(value, str) or not ID.fullmatch(value): raise WorkspaceError('Invalid workspace ' + label)
    if not SHA.fullmatch(base_commit): raise WorkspaceError('An exact base commit is required')
    for name in (branch, base_branch): git(control, 'check-ref-format', 'refs/heads/' + name)
    if path.is_relative_to(control) or control.is_relative_to(path): raise WorkspaceError('Workspace must be separate from the control checkout')
    if any(c in str(path) for c in '\n\r\x00'): raise WorkspaceError('Workspace path contains unsupported control characters')
    target = registry_path(control, task_id)
    with repository_lock(control / '.go', 'workspace-create'), repository_lock(control / '.go', 'workspace-execution-' + task_id):
        task = active_task(control, task_id, owner)
        policy = (task.get('execution_contract') or {}).get('workspace') or {}
        if policy.get('mode') != 'task_worktree' or policy.get('control_state') != 'repo_local_single_writer':
            raise WorkspaceError('Task must explicitly opt into a single-writer task worktree')
        if policy.get('base_branch') and policy['base_branch'] != base_branch:
            raise WorkspaceError('Explicit base branch differs from the task contract')
        expected = {'task_id': task_id, 'owner': owner, 'run_id': run_id, 'control_repo': str(control),
                    'path': str(path), 'branch': branch, 'base_branch': base_branch, 'base_commit': base_commit}
        if target.exists():
            record = validate_record(read_object(target))
            if any(record[key] != value for key, value in expected.items()): raise WorkspaceError('Workspace already belongs to another owner/run/path/base')
            if record['state'] == 'cleaned': raise WorkspaceError('Workspace is already cleaned; do not recreate the delivered run')
            if record['state'] != 'creating': return verify_workspace(record)
        else:
            if path.exists() or git(control, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch, check=False).returncode == 0:
                raise WorkspaceError('Foreign existing workspace path or branch collision')
            if git_text(control, 'rev-parse', '--verify', 'refs/heads/' + base_branch) != base_commit:
                raise WorkspaceError('Base branch revision changed before workspace creation')
            common = common_dir(control)
            identity = common / 'go-workflow-repository-id'
            if not identity.exists(): atomic_write_text(identity, uuid.uuid4().hex + '\n')
            record = {**expected, 'schema': SCHEMA, 'common_dir': str(common),
                      'repository_id': identity.read_text().strip(), 'generation': uuid.uuid4().hex, 'state': 'creating'}
            atomic_json(target, record)
        if not path.exists():
            if git(control, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch, check=False).returncode == 0:
                raise WorkspaceError('Interrupted creation left an orphan branch; preserve and inspect it')
            git(control, '-c', 'core.hooksPath=/dev/null', 'worktree', 'add', '-b', branch, str(path), base_commit)
        verify_workspace(record, pending=True)
        atomic_json(marker_path(path), marker_for(record))
        record['state'] = 'ready'
        atomic_json(target, record)
        return record


def registered_workspace(repo: Path):
    repo = repo.resolve()
    dotgit = repo / '.git'
    if not dotgit.exists(): return None
    if dotgit.is_dir() and not (dotgit / 'go-workspace.json').exists(): return None
    marker = marker_path(repo)
    if not marker.exists():
        # Creation only accepts a primary checkout with a .git directory, so
        # common-dir's parent is the explicit control checkout for our registry.
        control = common_dir(repo).parent
        for candidate in (control / '.go/workspaces').glob('*.json'):
            record = read_object(candidate)
            if record.get('path') == str(repo):
                raise WorkspaceError('Registered workspace marker missing; recover the recorded creation')
        return None
    binding = read_object(marker)
    if binding.get('schema') != SCHEMA or not isinstance(binding.get('control_repo'), str):
        raise WorkspaceError('Invalid workspace control binding')
    control = Path(binding['control_repo']).resolve()
    record = validate_record(read_object(registry_path(control, binding.get('task_id'))))
    if binding != marker_for(record) or Path(record['path']).resolve() != repo:
        raise WorkspaceError('Workspace control binding mismatch')
    if record['state'] in {'creating', 'cleaned'}: raise WorkspaceError('Workspace is not ready; recover its recorded state')
    return verify_workspace(record)


def workflow_root(repo: Path) -> Path:
    record = registered_workspace(repo)
    return Path(record['control_repo']) / '.go' if record else repo / '.go'


def guard_workspace_command(args):
    """Workers can read the control state; its controller owns queue mutations."""
    repo = Path(getattr(args, 'repo', Path.cwd())).resolve()
    record = registered_workspace(repo) or registered_workspace(Path.cwd())
    if record is None: return
    read_only = {'cmd_version', 'cmd_status', 'cmd_readback', 'cmd_route', 'cmd_router',
                 'cmd_validate', 'cmd_next', 'cmd_doctor', 'cmd_agent_check', 'cmd_dirty_check',
                 'cmd_architecture_validate', 'cmd_architecture_readback', 'cmd_architecture_status',
                 'cmd_recommendation_status', 'cmd_context_verify'}
    name = args.func.__name__
    if name in read_only or (name == 'cmd_workspace_operation' and args.workspace_operation == 'status'): return
    if name == 'cmd_managed_worker_enter' and getattr(args, 'task_id', None) == record['task_id']:
        if args.owner == record['owner'] and args.run_id == record['run_id']: return
    if name == 'cmd_task_outcome' and getattr(args, 'task_id', None) == record['task_id']:
        if getattr(args, 'agent', None) == record['owner'] and workflow_root(repo) == Path(record['control_repo']) / '.go':
            active_task(Path(record['control_repo']), record['task_id'], record['owner'])
            return
    raise WorkspaceError('Use the control checkout for workflow mutations; a worker may only record its owned task outcomes')


def owned_record(control, task_id, owner, run_id, *, active=True):
    control = Path(control).resolve()
    record = validate_record(read_object(registry_path(control, task_id)))
    if record['control_repo'] != str(control) or record['owner'] != owner or record['run_id'] != run_id:
        raise WorkspaceError('Workspace owner/run/control mismatch')
    if active: active_task(control, task_id, owner)
    return record


def changed_paths(record):
    workspace = Path(record['path'])
    # Separate index and worktree comparisons retain staged changes later undone
    # in the working file. --no-renames checks both sides of moves against scope.
    paths = set()
    for args in [('diff', '--name-only', '-z', '--no-renames', record['base_commit'], '--'),
                 ('diff', '--cached', '--name-only', '-z', '--no-renames', record['base_commit'], '--'),
                 ('ls-files', '--others', '--exclude-standard', '-z')]:
        paths.update(path for path in git(workspace, *args).stdout.split('\0') if path)
    return sorted(paths)


def require_visible_index(record):
    workspace = Path(record['path'])
    for option in ('-v', '-f'):
        entries = git(workspace, 'ls-files', option, '-z').stdout.split('\0')
        if any(entry and (entry[0].islower() or entry[0] == 'S') for entry in entries):
            raise WorkspaceError('Workspace index flags can hide changes; preserve and inspect before staging/integration/cleanup')


def checked_scope(record):
    require_visible_index(record)
    import fnmatch
    task = active_task(Path(record['control_repo']), record['task_id'])
    allowed = (task.get('scope') or {}).get('modify') or []
    paths = changed_paths(record)
    runtime = ('.go/tasks', '.go/runs', '.go/evidence', '.go/reflections', '.go/locks', '.go/workspaces')
    violations = [path for path in paths if any(path == prefix or path.startswith(prefix + '/') for prefix in runtime)
                  or not any(fnmatch.fnmatchcase(path, pattern) or path == pattern.rstrip('/')
                             or path.startswith(pattern.rstrip('/') + '/') for pattern in allowed)]
    if violations: raise WorkspaceError('Workspace scope violations: ' + ', '.join(violations))
    return paths


def require_run_idle(record):
    from .run_state import state_path, read_state, require_stopped, RunStateError
    control = Path(record['control_repo'])
    if state_path(control, record['task_id']).exists():
        try: require_stopped(read_state(control, record['task_id']))
        except RunStateError as exc: raise WorkspaceError(str(exc)) from exc


def stage_workspace(control, task_id, owner, run_id):
    with repository_lock(Path(control) / '.go', 'workspace-execution-' + task_id):
        record = verify_workspace(owned_record(control, task_id, owner, run_id))
        require_run_idle(record)
        if record['state'] != 'ready': raise WorkspaceError('Workspace must be ready for staging')
        paths = checked_scope(record)
        if paths: git(Path(record['path']), '--literal-pathspecs', 'add', '--all', '--', *paths)
        return {'task_id': task_id, 'staged_paths': paths}


@contextmanager
def execution_lease(workspace, task_id, owner, run_id, *, timeout_seconds=10.0):
    record = registered_workspace(Path(workspace))
    if record is None or record['task_id'] != task_id: raise WorkspaceError('A registered task workspace is required')
    control = Path(record['control_repo'])
    with repository_lock(control / '.go', 'workspace-execution-' + task_id, timeout_seconds):
        current = verify_workspace(owned_record(control, task_id, owner, run_id))
        require_run_idle(current)
        if current['state'] != 'ready': raise WorkspaceError('Workspace must be ready for execution')
        yield current
        # An owner/branch change while the worker was running is never accepted.
        verify_workspace(owned_record(control, task_id, owner, run_id))
        checked_scope(current)


def require_clean(record):
    require_visible_index(record)
    if git_text(Path(record['path']), 'status', '--porcelain', '--untracked-files=all', '--ignored'):
        raise WorkspaceError('Workspace is dirty (including ignored files); preserve and inspect it')


@contextmanager
def integration_slot(control, task_id, owner, run_id, *, timeout_seconds=10.0):
    control = Path(control).resolve()
    # Global integration first, then task execution: every integrating/cleanup
    # operation uses this order. Hold both across the caller's actual operation.
    with repository_lock(control / '.go', 'workspace-integration', timeout_seconds):
        with repository_lock(control / '.go', 'workspace-execution-' + task_id, timeout_seconds):
            record = verify_workspace(owned_record(control, task_id, owner, run_id))
            require_run_idle(record)
            if record['state'] != 'ready': raise WorkspaceError('Workspace is not ready for integration')
            require_clean(record)
            checked_scope(record)
            if git_text(control, 'rev-parse', 'refs/heads/' + record['base_branch']) != record['base_commit']:
                raise WorkspaceError('Base branch advanced; retain workspace and explicitly reconcile before retry')
            yield {'task_id': task_id, 'base_branch': record['base_branch'],
                   'base_commit': record['base_commit'], 'workspace_head': git_text(Path(record['path']), 'rev-parse', 'HEAD'),
                   'path': record['path'], 'generation': record['generation']}


def record_integration(control, task_id, owner, run_id, integrated_commit):
    control = Path(control).resolve()
    with repository_lock(control / '.go', 'workspace-integration'):
        with repository_lock(control / '.go', 'workspace-execution-' + task_id):
            record = verify_workspace(owned_record(control, task_id, owner, run_id))
            require_run_idle(record)
            require_clean(record)
            checked_scope(record)
            head = git_text(Path(record['path']), 'rev-parse', 'HEAD')
            if not SHA.fullmatch(integrated_commit): raise WorkspaceError('Exact integrated commit required')
            target = git_text(control, 'rev-parse', 'refs/heads/' + record['base_branch'])
            for ancestor, descendant in ((head, integrated_commit), (integrated_commit, target)):
                if git(control, 'merge-base', '--is-ancestor', ancestor, descendant, check=False).returncode:
                    raise WorkspaceError('Integration proof does not preserve the complete workspace on the base branch')
            record['integration'] = {'workspace_head': head, 'integrated_commit': integrated_commit}
            record['state'] = 'integrated'
            atomic_json(registry_path(control, task_id), record)
            return record


def cleanup_proof(control, record):
    from .execution_contracts import valid_release_receipt
    proof = record.get('integration') or {}
    if not all(isinstance(proof.get(key), str) and SHA.fullmatch(proof[key]) for key in ('workspace_head', 'integrated_commit')):
        raise WorkspaceError('Verified integration is required before cleanup')
    task = active_task(control, record['task_id'])
    if task.get('status') != 'done' or task.get('work_status') != 'completed' or task.get('review_status') != 'approved':
        raise WorkspaceError('Approved task completion is required before cleanup')
    branch = 'refs/heads/' + record['base_branch']
    pairs = [(proof['workspace_head'], proof['integrated_commit']), (proof['integrated_commit'], branch)]
    release = (task.get('execution_contract') or {}).get('release') or {}
    if release.get('mode') == 'required':
        receipt = task.get('release_receipt')
        if not valid_release_receipt(receipt, task): raise WorkspaceError('Verified release receipt is required before cleanup')
        pairs.extend([(proof['workspace_head'], receipt['commit']), (receipt['commit'], branch)])
    elif release.get('mode') != 'none' or not release.get('reason'):
        raise WorkspaceError('Explicit release policy is required before cleanup')
    for ancestor, descendant in pairs:
        if git(control, 'merge-base', '--is-ancestor', ancestor, descendant, check=False).returncode:
            raise WorkspaceError('Integration/release proof does not preserve the workspace on the configured base branch')
    return proof


def cleanup_workspace(control, task_id, owner, run_id):
    control = Path(control).resolve()
    with repository_lock(control / '.go', 'workspace-integration'):
        with repository_lock(control / '.go', 'workspace-execution-' + task_id):
            record = owned_record(control, task_id, owner, run_id, active=False)
            require_run_idle(record)
            if record['state'] == 'cleaned': return record
            proof = cleanup_proof(control, record)
            workspace = Path(record['path'])
            if workspace.exists():
                verify_workspace(record)
                require_clean(record)
                if git_text(workspace, 'rev-parse', 'HEAD') != proof['workspace_head']:
                    raise WorkspaceError('Workspace changed after integration; preserve and reconcile it')
                try:
                    git(control, 'worktree', 'remove', str(workspace))
                except WorkspaceError as exc:
                    record.update(state='cleanup_failed', cleanup_error=str(exc),
                                  recovery_action='Inspect the retained workspace and Git locks, then retry workspace cleanup; do not republish')
                    atomic_json(registry_path(control, task_id), record)
                    raise
            else:
                listed = git(control, 'worktree', 'list', '--porcelain', '-z').stdout.split('\0')
                if 'worktree ' + str(workspace) in listed:
                    raise WorkspaceError('Workspace path is absent but still registered in Git; preserve and inspect it')
            record.update(state='cleaned', recovery_action='No action; delivered branch is retained')
            record.pop('cleanup_error', None)
            atomic_json(registry_path(control, task_id), record)
            return record


def rebind_workspace(control, task_id, owner, run_id, new_owner, new_run_id):
    """Follow an already transferred canonical claim; never transfer the claim here."""
    control = Path(control).resolve()
    if not all(isinstance(value, str) and ID.fullmatch(value) for value in (new_owner, new_run_id)):
        raise WorkspaceError('Invalid new workspace owner/run')
    with repository_lock(control / '.go', 'workspace-execution-' + task_id):
        with repository_lock(control / '.go', 'task-' + task_id):
            record = verify_workspace(owned_record(control, task_id, owner, run_id, active=False))
            require_run_idle(record)
            if record['state'] != 'ready': raise WorkspaceError('Only a ready workspace can follow a claim transfer')
            active_task(control, task_id, new_owner)
            record.setdefault('ownership_history', []).append({'owner': owner, 'run_id': run_id})
            record.update(owner=new_owner, run_id=new_run_id)
            atomic_json(registry_path(control, task_id), record)
            return record
