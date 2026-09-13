"""Durable, single-owner phase execution for explicitly managed native tasks.

Publication is a separate driver: reaching release never means the task is done.
Every subprocess registers its process group before executing its command, so a
dead controller cannot leave an invisible writer for a successor to race with.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time
import uuid

from .execution_context import git_state, json_hash, canonical_sources, file_state
from .model_profiles import freeze_selection
from .state_io import atomic_json, atomic_move_json, repository_lock, _pid_alive
from .worktrees import (WorkspaceError, active_task, checked_scope, cleanup_workspace,
                        create_workspace, owned_record, read_object, registry_path,
                        verify_workspace, execution_lease)

SCHEMA = 'go-workflow.managed-run.v1'
PROCESS = ContextVar('managed_phase_process', default=None)


class RunStateError(ValueError):
    pass


def validate_state(value):
    strings = {'task_id', 'project', 'control_repo', 'owner', 'run_id', 'task_hash'}
    objects = {'workspace', 'models', 'effects', 'code', 'controller'}
    arrays = {'checks', 'phase_evidence', 'budgets', 'history', 'requirements'}
    required = strings | objects | arrays | {'schema', 'phase', 'attempt', 'check_index', 'worker_group', 'inflight'}
    if (not isinstance(value, dict) or not required.issubset(value)
            or set(value) - required - {'cleanup', 'setup_task'}
            or value['schema'] != SCHEMA
            or value['phase'] not in {'setup', 'build', 'verify', 'critic', 'repair', 'release', 'cleanup', 'complete'}
            or any(not isinstance(value[name], str) or not value[name] for name in strings)
            or any(not isinstance(value[name], dict) for name in objects)
            or any(not isinstance(value[name], list) for name in arrays)):
        raise RunStateError('Invalid managed run state shape')
    for field, minimum in [('attempt', 1), ('check_index', 0)]:
        if type(value[field]) is not int or value[field] < minimum: raise RunStateError('Invalid run counter: ' + field)
    owner = value['controller']
    if (set(owner) != {'host', 'pid'} or not isinstance(owner['host'], str) or not owner['host']
            or type(owner['pid']) is not int or owner['pid'] < 1): raise RunStateError('Invalid controller identity')
    if value['worker_group'] is not None and (type(value['worker_group']) is not int or value['worker_group'] < 1):
        raise RunStateError('Invalid worker group')
    flight = value['inflight']
    if flight is not None and (not isinstance(flight, dict) or not {'phase', 'nonce', 'attempt', 'started_at'}.issubset(flight)):
        raise RunStateError('Invalid phase intent')
    if value['phase'] == 'setup' and not isinstance(value.get('setup_task'), dict): raise RunStateError('Missing setup intake')


def state_path(control, task_id):
    registry_path(control, task_id)  # validates the identifier before path use
    return control / '.go/runs' / task_id / 'run-state.json'


def read_state(control, task_id):
    value = read_object(state_path(control, task_id))
    validate_state(value)
    if value['task_id'] != task_id or value['control_repo'] != str(control):
        raise RunStateError('Managed run control/task identity mismatch')
    return value


def group_alive(group):
    if not group: return False
    # Zombies cannot write. A reused group id or an unreadable process table is
    # deliberately treated as live; elapsed time is never proof of termination.
    result = subprocess.run(['ps', '-axo', 'pgid=,stat='], text=True, capture_output=True)
    if result.returncode: raise RunStateError('Cannot establish worker process termination')
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == str(group) and not parts[1].startswith('Z'):
            return True
    return False


def require_stopped(state):
    owner = state['controller']
    if owner['host'] != socket.gethostname():
        raise RunStateError('Cross-host owner liveness is unknown; explicit recovery is required')
    if owner['pid'] != os.getpid() and _pid_alive(owner['pid']):
        raise RunStateError('Previous controller is still live; ownership cannot be stolen')
    if group_alive(state.get('worker_group')):
        raise RunStateError('Previous worker process group is still live; preserve its workspace')


def run_code(workspace, record, task):
    from .worktrees import git_text
    code = git_state(workspace, record)
    control = Path(record['control_repo'])
    sources = canonical_sources(control / '.go', task)
    # Outcomes/evidence are mutable task data, while these instructions and
    # architecture decisions determine whether old phase proof still applies.
    sources = [path for path in sources if path.parent.name != 'active']
    code['contract_context_sha256'] = json_hash({str(path.relative_to(control)): file_state(path) for path in sources})
    code['control_base_head'] = git_text(control, 'rev-parse', 'refs/heads/' + record['base_branch'])
    return code


def protected_task(task):
    return {key: value for key, value in task.items()
            if key not in {'requested_outcomes', 'evidence', 'review_history', 'release_receipt'}}


def worker_enter(control, task_id, run_id, nonce, owner):
    """Called inside the new session, before the shell may launch its payload."""
    with repository_lock(control / '.go', 'managed-state-' + task_id):
        state = read_state(control, task_id)
        if (state['run_id'] != run_id or state['owner'] != owner
                or state.get('inflight', {}).get('nonce') != nonce
                or state['controller']['host'] != socket.gethostname()
                or not _pid_alive(state['controller']['pid'])):
            raise RunStateError('Stale worker bootstrap; controller/run/phase no longer owns execution')
        if state.get('worker_group') is not None:
            raise RunStateError('This phase already registered a worker')
        record = owned_record(control, task_id, owner, run_id)
        verify_workspace(record)
        if Path.cwd().resolve() != Path(record['path']):
            raise RunStateError('Worker bootstrap must run in the registered workspace')
        state['worker_group'] = os.getpgrp()
        atomic_json(state_path(control, task_id), state)
        if not _pid_alive(state['controller']['pid']):
            raise RunStateError('Controller died during worker registration')


def process_command(command, cli_path):
    binding = PROCESS.get()
    if binding is None: return command
    import sys
    control, task_id, run_id, nonce, owner = binding
    bootstrap = [sys.executable, str(cli_path), 'managed', 'worker-enter', str(control),
                 '--task-id', task_id, '--run-id', run_id, '--nonce', nonce, '--owner', owner]
    return shlex.join(bootstrap) + ' >/dev/null && ' + command


def relocate_run(control, task_id, owner, run_id, old_control, old_workspace, workspace):
    """Repair one explicitly moved, stopped local checkout pair, never a clone.

    The relocation intent survives a crash between Git repair and registry/state
    updates. Historical snapshots retain their original hashes and path map.
    """
    from .worktrees import common_dir, git, marker_for
    control, workspace = Path(control).resolve(), Path(workspace).resolve()
    old_control, old_workspace = Path(old_control).absolute(), Path(old_workspace).absolute()
    if old_control.exists() or old_workspace.exists():
        raise RunStateError('Old checkout paths still exist; this is not a verified move')
    if not (control / '.git').is_dir() or workspace.is_relative_to(control) or control.is_relative_to(workspace):
        raise RunStateError('Relocation needs a primary control checkout and separate workspace')
    root = control / '.go'
    with repository_lock(root, 'managed-run-' + task_id, timeout_seconds=.1):
        with repository_lock(root, 'workspace-integration'), repository_lock(root, 'workspace-execution-' + task_id):
            with repository_lock(root, 'managed-state-' + task_id):
                state = read_object(state_path(control, task_id)); validate_state(state)
                require_stopped(state)
                if (state['task_id'] != task_id or state['owner'] != owner or state['run_id'] != run_id
                        or state['control_repo'] not in {str(old_control), str(control)}):
                    raise RunStateError('Relocation run/owner/control identity mismatch')
                active_task(control, task_id, owner)
                if state['phase'] not in {'build', 'verify', 'critic', 'repair', 'release'} or state['effects']:
                    raise RunStateError('Reconcile setup/publication/cleanup before relocation')
                record = read_object(registry_path(control, task_id))
                old = state['workspace']
                if old['control_repo'] == str(control):
                    intent = read_object(root / 'runs' / task_id / 'relocation.json')['binding']
                    if (intent['old_control'] != str(old_control) or intent['old_workspace'] != str(old_workspace)
                            or old['path'] != str(workspace)):
                        raise RunStateError('Completed relocation retry has different paths')
                    verify_workspace(record)
                    return state
                if (old['control_repo'] != str(old_control) or old['path'] != str(old_workspace)
                        or old['owner'] != owner or old['run_id'] != run_id or old['state'] != 'ready'):
                    raise RunStateError('Relocation workspace binding mismatch')
                common = common_dir(control)
                if (common / 'go-workflow-repository-id').read_text().strip() != old['repository_id']:
                    raise RunStateError('Relocation repository identity mismatch; preserve the foreign clone')
                others = [p for p in (root / 'workspaces').glob('*.json')
                          if p.stem != task_id and read_object(p).get('state') != 'cleaned']
                if others: raise RunStateError('Other registered workspaces require an explicit coordinated relocation')
                matches = [p for p in (common / 'worktrees').glob('*/go-workspace.json')
                           if read_object(p).get('generation') == old['generation']]
                if len(matches) != 1: raise RunStateError('Relocation Git marker is missing or ambiguous')
                marker = matches[0]
                new = {**old, 'control_repo': str(control), 'path': str(workspace), 'common_dir': str(common)}
                if record not in (old, new) or read_object(marker) not in (marker_for(old), marker_for(new)):
                    raise RunStateError('Relocation marker or registry is foreign')
                pointer = (workspace / '.git').read_text().strip()
                allowed = {'gitdir: ' + str(Path(old['common_dir']) / 'worktrees' / marker.parent.name),
                           'gitdir: ' + str(marker.parent)}
                if pointer not in allowed: raise RunStateError('Moved workspace points at a foreign Git directory')
                intent_path = root / 'runs' / task_id / 'relocation.json'
                intent = {'old_control': str(old_control), 'old_workspace': str(old_workspace),
                          'control': str(control), 'workspace': str(workspace), 'generation': old['generation']}
                if intent_path.exists() and read_object(intent_path)['binding'] != intent:
                    raise RunStateError('Another relocation intent needs reconciliation')
                atomic_json(intent_path, {'binding': intent, 'status': 'pending'})
                git(control, 'worktree', 'repair', str(workspace))
                atomic_json(marker, marker_for(new))
                atomic_json(registry_path(control, task_id), new)
                verify_workspace(new); checked_scope(new)
                state['history'].append({'event': 'relocated', 'path_map': intent,
                                         'invalidated_checks': state['checks'], 'phase_evidence': state['phase_evidence']})
                state.update(control_repo=str(control), workspace=new, checks=[], check_index=0,
                             phase_evidence=[], inflight=None, worker_group=None,
                             phase=state['phase'] if state['phase'] in {'build', 'repair'} else 'verify',
                             code=run_code(workspace, new, active_task(control, task_id, owner)))
                atomic_json(state_path(control, task_id), state)
                atomic_json(intent_path, {'binding': intent, 'status': 'confirmed'})
                return state


class RunSession:
    def __init__(self, control, task_id):
        self.control, self.task_id = control, task_id

    def load(self): return read_state(self.control, self.task_id)

    def update(self, **changes):
        with repository_lock(self.control / '.go', 'managed-state-' + self.task_id):
            state = self.load()
            if state['controller'] != {'host': socket.gethostname(), 'pid': os.getpid()}:
                raise RunStateError('Controller ownership changed')
            state.update(deepcopy(changes))
            atomic_json(state_path(self.control, self.task_id), state)
        return state

    @contextmanager
    def operation(self):
        state = self.load()
        require_stopped(state)
        nonce = uuid.uuid4().hex
        self.update(inflight={'phase': state['phase'], 'nonce': nonce,
                              'attempt': state['attempt'], 'started_at': time.time()}, worker_group=None)
        token = PROCESS.set((str(self.control), self.task_id, state['run_id'], nonce, state['owner']))
        try:
            yield
            latest = self.load()
            if group_alive(latest.get('worker_group')):
                raise RunStateError('Phase left a live worker; cannot advance or switch models')
            self.update(worker_group=None)
        finally:
            PROCESS.reset(token)

    def effect_intent(self, key, kind, target, expected):
        """Persist before an effect; callers must reconcile an existing intent."""
        state = self.load()
        require_stopped(state)
        if not all(isinstance(value, str) and value for value in (key, kind, target)):
            raise RunStateError('Side-effect key, kind and target must be explicit')
        effects = state['effects']
        intent = {'key': key, 'kind': kind, 'target': target, 'expected': expected}
        if key in effects:
            if any(effects[key][name] != value for name, value in intent.items()):
                raise RunStateError('Side-effect identity changed')
            return deepcopy(effects[key]), False
        effects[key] = {**intent, 'status': 'pending', 'observations': []}
        self.update(effects=effects)
        return deepcopy(effects[key]), True

    def reconcile_effect(self, key, observer):
        state = self.load()
        require_stopped(state)
        effect = state['effects'][key]
        observation = observer(deepcopy(effect))
        if not isinstance(observation, dict) or observation.get('status') not in {'confirmed', 'absent', 'unknown'}:
            raise RunStateError('Effect observer must report confirmed/absent/unknown')
        if observation['status'] == 'confirmed' and observation.get('observed') != effect['expected']:
            raise RunStateError('External readback does not match the recorded effect intent')
        effect['observations'].append(observation)
        effect['status'] = 'confirmed' if observation['status'] == 'confirmed' else 'pending'
        self.update(effects=state['effects'])
        return deepcopy(effect)


def select_managed_task(control, args, api):
    explicit = getattr(args, 'task_id', '')
    if explicit:
        task = active_task(control, explicit)
        if ((task.get('execution_contract') or {}).get('workspace') or {}).get('mode') == 'task_worktree': return task
        raise RunStateError('--task-id execution currently requires an explicit managed workspace contract')
    candidates = [read_object(path) for path in (control / '.go/tasks/active').glob('*.json')
                  if state_path(control, path.stem).exists()]
    if len(candidates) > 1: raise RunStateError('Multiple managed active tasks; select --task-id explicitly')
    if candidates: return candidates[0]
    cleanup = [read_object(path) for path in (control / '.go/tasks/done').glob('*.json')
               if state_path(control, path.stem).exists() and read_state(control, path.stem)['phase'] == 'cleanup']
    if len(cleanup) > 1: raise RunStateError('Multiple pending cleanups; select --task-id explicitly')
    if cleanup: return cleanup[0]
    tasks = api.open_tasks(control)
    if tasks:
        task = tasks[0][1]
        if ((task.get('execution_contract') or {}).get('workspace') or {}).get('mode') == 'task_worktree': return task
    return None


def execute_managed(control, args, mode, task, api):
    task_id = task['id']
    result = {'schema': 'go-workflow.auto-run-result.v1', 'mode': mode, 'repo': str(control),
              'completed_tasks': [], 'blocked_task': None, 'checks': [], 'commands_run': 0}
    try:
        with repository_lock(control / '.go', 'managed-run-' + task_id, timeout_seconds=0.1):
            return _execute_managed(control, args, task_id, api, result)
    except (ValueError, api.StateLockError, api.RepoLocalError) as exc:
        result.update(status='resume_gate', blocked_task=task_id, summary=str(exc))
        return 1, result


def _execute_managed(control, args, task_id, api, result):
    root = control / '.go'
    session = RunSession(control, task_id)
    task = active_task(control, task_id)
    if any(getattr(args, key, '') for key in ('build_command', 'critic_command', 'repair_command')):
        raise RunStateError('Managed phases require the controlled native Codex adapter')
    if getattr(args, 'executor_agent', 'auto') not in {'auto', 'codex'} or getattr(args, 'repair_agent', '') not in {'', 'codex'}:
        raise RunStateError('Managed phases require Codex model control')
    if not getattr(args, 'semantic_critic', True): raise RunStateError('Managed execution requires its critic phase')
    if state_path(control, task_id).exists():
        state = session.load()
        require_stopped(state)
        if state['owner'] != args.agent: raise RunStateError('Run owner differs; explicit claim/workspace transfer is required')
        for arg, key in [('run_id', 'run_id'), ('workspace_path', 'path'), ('workspace_branch', 'branch'),
                         ('base_branch', 'base_branch'), ('base_commit', 'base_commit')]:
            supplied = getattr(args, arg, '')
            expected = state['run_id'] if key == 'run_id' else state['workspace'][key]
            if supplied and supplied != expected: raise RunStateError('Resume binding changed: ' + arg)
        if state['phase'] not in {'setup', 'cleanup', 'complete'} and json_hash(protected_task(task)) != state['task_hash']:
            raise RunStateError('Task/model contract changed; explicit checkpoint migration is required')
        with repository_lock(root, 'managed-state-' + task_id):
            state = session.load()
            require_stopped(state)
            state['controller'] = {'host': socket.gethostname(), 'pid': os.getpid()}
            if state['inflight']: state['inflight']['nonce'] = 'retired-' + uuid.uuid4().hex
            atomic_json(state_path(control, task_id), state)
    else:
        fields = ('workspace_path', 'workspace_branch', 'base_branch', 'base_commit', 'run_id')
        if not all(getattr(args, field, '') for field in fields):
            raise RunStateError('Initial managed execution requires explicit workspace path/branch, base branch/commit and run id')
        if task['status'] != 'open': raise RunStateError('Active task has no checkpoint; do not invent its previous phase')
        if list((root / 'tasks/active').glob('*.json')):
            raise RunStateError('Another active task must be resolved before starting this managed run')
        errors = (api.validate_repo(control) + api.dependency_findings(control, task, readiness=True)
                  + api.architecture_claim_findings(root, task))
        preflight = api.build_auto_preflight(control, [task], 1)
        if errors or preflight.get('contract_gate_required'):
            raise RunStateError('Managed task contract/dependency preflight failed: ' + '; '.join(errors or [str(preflight.get('contract_findings'))]))
        # A separate workspace excludes unrelated controller dirt. Still refuse
        # unresolved Git conflicts and contract gates; never run in that dirt.
        if api.subprocess.run(['git', 'ls-files', '-u'], cwd=control, capture_output=True).stdout:
            raise RunStateError('Control checkout has unresolved Git conflicts')
        record = {'path': str(Path(args.workspace_path).resolve()), 'branch': args.workspace_branch,
                  'base_branch': args.base_branch, 'base_commit': args.base_commit}
        models = freeze_selection(control, task)
        state = {'schema': SCHEMA, 'task_id': task_id, 'project': task['project'], 'control_repo': str(control),
                 'owner': args.agent, 'run_id': args.run_id, 'workspace': record, 'phase': 'setup',
                 'attempt': 1, 'check_index': 0, 'checks': [], 'phase_evidence': [], 'effects': {},
                 'budgets': [], 'history': [], 'requirements': task.get('requested_outcomes', []),
                 'models': models, 'task_hash': json_hash(protected_task(task)),
                 'code': {},
                 'controller': {'host': socket.gethostname(), 'pid': os.getpid()},
                 'worker_group': None, 'inflight': None, 'setup_task': task}
        atomic_json(state_path(control, task_id), state)
    state = session.load()
    if state['phase'] == 'setup':
        expected = state['setup_task']
        with repository_lock(root, 'task-' + task_id):
            task = active_task(control, task_id)
            if task['status'] == 'open':
                if task != expected: raise RunStateError('Task changed during setup; inspect the saved intake')
                task.update(status='active', work_status='in_progress',
                            claim={'agent': args.agent, 'claimed_at': api.now_iso(), 'base_commit': state['workspace']['base_commit']})
                atomic_move_json(root / 'tasks/open' / (task_id + '.json'), root / 'tasks/active' / (task_id + '.json'), task)
            else:
                active_task(control, task_id, args.agent)
                ignored = {'status', 'work_status', 'claim'}
                if ({k:v for k,v in task.items() if k not in ignored}
                        != {k:v for k,v in expected.items() if k not in ignored}):
                    raise RunStateError('Claimed task changed during setup')
        binding = state['workspace']
        record = create_workspace(control, task_id, args.agent, state['run_id'], Path(binding['path']),
                                  binding['branch'], binding['base_branch'], binding['base_commit'])
        state = session.update(phase='build', workspace=record, task_hash=json_hash(protected_task(task)),
                               code=run_code(Path(record['path']), record, task))
    if state['phase'] == 'complete':
        from .worktrees import cleanup_proof
        record = owned_record(control, task_id, args.agent, state['run_id'], active=False)
        if record['state'] != 'cleaned': raise RunStateError('Completed checkpoint lacks confirmed workspace cleanup')
        cleanup_proof(control, record)
        result.update(status='task_complete' if api.open_tasks(control) else 'done', completed_tasks=[task_id]); return 0, result
    record = owned_record(control, task_id, args.agent, state['run_id'], active=state['phase'] != 'cleanup')
    binding = ('control_repo', 'path', 'owner', 'run_id', 'branch', 'base_branch', 'base_commit',
               'common_dir', 'repository_id', 'generation')
    if any(record[key] != state['workspace'].get(key) for key in binding):
        raise RunStateError('Workspace binding/generation changed')
    if state['phase'] == 'cleanup':
        if task['status'] != 'done': raise RunStateError('Cleanup recovery requires a completed task')
        cleanup = cleanup_workspace(control, task_id, args.agent, state['run_id'])
        session.update(phase='complete', cleanup=cleanup, inflight=None)
        result.update(status='task_complete' if api.open_tasks(control) else 'done', completed_tasks=[task_id]); return 0, result
    verify_workspace(record)
    checked_scope(record)
    workspace = Path(record['path'])
    if freeze_selection(control, task) != state['models']: raise RunStateError('Frozen model selection changed')
    current_code = run_code(workspace, record, task)
    if state['inflight'] or current_code != state['code']:
        history = state['history'] + [{'event': 'reconciled_interruption_or_drift', 'previous_phase': state['phase'],
                                     'inflight': state['inflight'], 'code_before': state['code'], 'code_now': current_code,
                                     'invalidated_checks': state['checks']}]
        if state['effects']: raise RunStateError('External effects need readback before code drift/recovery can advance')
        # A confirmed build is kept. All affected verification and critic proof
        # is invalidated, while an unconfirmed build/repair repeats its own phase.
        phase = state['phase'] if state['phase'] in {'build', 'repair'} else 'verify'
        session.update(phase=phase, check_index=0, checks=[], history=history, code=current_code,
                       inflight=None, worker_group=None)
    maximum = max(int(args.max_commands), 1)
    minutes = max(int(args.max_minutes), 1)
    started = time.monotonic()
    state = session.load()
    budget = {'max_commands': maximum, 'max_minutes': minutes, 'commands_used': 0, 'started_at': time.time()}
    session.update(budgets=state['budgets'] + [budget])
    while True:
        state = session.load()
        phase = state['phase']
        if phase == 'release':
            result.update(status='release_pending', summary='Build, verification and critic confirmed; lifecycle publisher required.')
            break
        if result['commands_run'] >= maximum or time.monotonic() - started >= minutes * 60:
            result.update(status='budget_exhausted', budget_exhausted=True, summary='Phase checkpoint saved; resume this task with a new budget.')
            break
        if state['attempt'] > max(int(args.max_attempts), 1):
            result.update(status='attempts_exhausted', blocked_task=task_id); break
        task = active_task(control, task_id, args.agent)
        if json_hash(protected_task(task)) != state['task_hash']: raise RunStateError('Task contract changed during execution')
        verify_workspace(record); checked_scope(record)
        timeout = min(max(int(args.command_timeout_seconds), 1), max(1, int(minutes * 60 - (time.monotonic() - started))))
        if phase == 'verify' and state['check_index'] >= len(task['verification']):
            session.update(phase='critic'); continue
        before = run_code(workspace, record, task)
        with session.operation():
            if phase == 'verify':
                command = task['verification'][state['check_index']]
                with execution_lease(workspace, task_id, args.agent, state['run_id']):
                    output = api.run_verification_commands(workspace, {**task, 'verification': [command]}, timeout_seconds=timeout)[0]
            else:
                feedback = {'checks': state['checks'], 'result': state['phase_evidence'][-1]['result'] if state['phase_evidence'] else {}}
                if phase == 'critic':
                    output = api.run_default_critic_agent(workspace, 'codex', task, state['attempt'], 'direct_fix', timeout, feedback=feedback)
                else:
                    command = (api.default_executor_agent_command if phase == 'build' else api.default_repair_agent_command)('codex', task)
                    output = api.run_hook_command(workspace, command, task, state['attempt'], 'direct_fix', phase, timeout,
                                                  require_protocol=True, feedback=feedback)
        after = run_code(workspace, record, task)
        if phase in {'critic', 'verify'} and before != after:
            output = {**output, 'returncode': 78, 'status': 'blocked', 'summary': 'Read-only proof phase changed workspace; proof invalidated'}
        state = session.load()
        success = output.get('returncode') == 0 and (phase == 'verify' or output.get('status') == 'success')
        evidence = state['phase_evidence'] + [{'phase': phase, 'attempt': state['attempt'], 'result': output,
                                               'code_before': before, 'code_after': after}]
        changes = {'phase_evidence': evidence, 'inflight': None, 'worker_group': None, 'code': after,
                   'requirements': active_task(control, task_id, args.agent).get('requested_outcomes', [])}
        if phase == 'verify':
            changes.update(checks=state['checks'] + [output], check_index=state['check_index'] + 1)
            if not success: changes.update(phase='repair', attempt=state['attempt'] + 1)
        elif success:
            changes.update(phase='release' if phase == 'critic' else 'verify')
            if phase != 'critic': changes.update(check_index=0, checks=[])
        else:
            changes.update(phase='repair', attempt=state['attempt'] + 1)
        result['commands_run'] += 1
        state['budgets'][-1]['commands_used'] = result['commands_run']
        changes['budgets'] = state['budgets']
        session.update(**changes)
    state = session.load()
    result.update(task_id=task_id, run_id=state['run_id'], phase=state['phase'], checks=state['checks'],
                  state_path=str(state_path(control, task_id)))
    # A structured handoff deliberately names the immutable runtime contract;
    # it does not fall back to GO_STACK or an arbitrary mutable sibling checkout.
    resume_args = ['loop' if result['mode'] == 'go-loop' else 'auto', str(control), '--execute',
                   '--task-id', task_id, '--agent', args.agent, '--executor-agent', 'codex',
                   '--max-commands', str(maximum), '--max-minutes', str(minutes),
                   '--max-attempts', str(args.max_attempts), '--command-timeout-seconds', str(args.command_timeout_seconds), '--json']
    resume = {'schema': 'go-workflow.managed-resume.v1', 'task_id': task_id, 'run_id': state['run_id'],
              'runtime_required_ref': read_object(root / 'project.json')['stack_ref'], 'args': resume_args,
              'resolution': 'Resolve and preflight the immutable project runtime before invoking these CLI arguments.'}
    atomic_json(root / 'runs' / task_id / 'resume.json', resume)
    result['resume'] = resume
    return 0, result
