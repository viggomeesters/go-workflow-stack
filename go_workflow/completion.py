"""Executed, content-bound evidence and the shared opted-in completion gate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager

from .execution_context import json_hash
from .execution_contracts import validate_verification_evidence
from .state_io import atomic_json, repository_lock
from .worktrees import (active_task, git, git_text, read_object, registered_workspace,
                        workflow_root, require_visible_index, execution_lease)


class CompletionError(ValueError):
    pass


@contextmanager
def proof_lease(repo, task_id, owner):
    from .worktrees import registry_path, owned_record, require_run_idle
    root = workflow_root(repo)
    record = registered_workspace(repo)
    if record:
        with execution_lease(repo, task_id, owner, record['run_id']): yield
    else:
        with repository_lock(root, 'workspace-execution-' + task_id):
            require_run_idle({'control_repo': str(root.parent), 'task_id': task_id})
            path = registry_path(root.parent, task_id)
            if path.exists():
                record = read_object(path)
                owned_record(root.parent, task_id, owner, record['run_id'])
                require_run_idle(record)
                if record['state'] == 'ready':
                    raise CompletionError('Verify the owned workspace before integration; do not substitute the control checkout')
            yield


def operational(path):
    return (path.startswith(('.go/tasks/', '.go/runs/', '.go/evidence/', '.go/reflections/',
                             '.go/locks/', '.go/workspaces/', '.go/deliveries/', '.go/plans/'))
            or path in {'.go/hierarchy.json', '.go/architecture/events.jsonl'})


def contract_digest(task):
    fields = ('id', 'project', 'acceptance', 'verification', 'scope', 'execution_contract', 'architecture',
              'execution_mode', 'shareable_delivery', 'summary', 'description', 'dependencies', 'context_files', 'skill_files')
    contract = {key: task.get(key) for key in fields}
    contract['requirements'] = [{key: item.get(key) for key in ('id', 'text', 'source')}
                                for item in task.get('requested_outcomes', [])]
    return json_hash(contract)


def content_snapshot(repo, task, revision=None):
    """Git blob identity, including actual tracked bytes that index flags hide."""
    import fnmatch
    entries = {}
    if revision:
        for line in git(repo, 'ls-tree', '-rz', revision).stdout.split('\0'):
            if not line: continue
            meta, path = line.split('\t', 1)
            mode, kind, oid = meta.split()
            if operational(path): continue
            if kind != 'blob': raise CompletionError('Completion content does not support nested Git repositories')
            entries[path] = {'mode': mode, 'oid': oid}
    else:
        require_visible_index({'path': str(repo)})
        paths = set(git(repo, 'ls-files', '-z').stdout.split('\0')) - {''}
        for name in git(repo, 'ls-files', '--others', '--exclude-standard', '-z').stdout.split('\0'):
            if name and any(fnmatch.fnmatchcase(name, pattern) for pattern in task['scope']['modify']): paths.add(name)
        for name in sorted(paths):
            if operational(name): continue
            path = repo / name
            if not path.exists() and not path.is_symlink(): continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                data = os.readlink(path).encode(); size = len(data); mode = '120000'
                hasher = hashlib.sha1(b'blob ' + str(size).encode() + b'\0' + data)
            elif stat.S_ISREG(info.st_mode):
                mode = '100755' if info.st_mode & stat.S_IXUSR else '100644'
                hasher = hashlib.sha1(b'blob ' + str(info.st_size).encode() + b'\0')
                with path.open('rb') as handle:
                    while chunk := handle.read(1024 * 1024): hasher.update(chunk)
            else: raise CompletionError('Unsupported completion content: ' + name)
            entries[name] = {'mode': mode, 'oid': hasher.hexdigest()}
    return {'digest': json_hash(entries), 'files': entries}


def save_artifact(root, task_id, kind, value):
    path = root / 'runs' / task_id / 'completion' / (kind + '-' + uuid.uuid4().hex + '.json')
    atomic_json(path, value)
    return {'path': str(path.relative_to(root.parent)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def read_artifact(root, ref):
    if not isinstance(ref, dict) or set(ref) != {'path', 'sha256'} or not all(isinstance(v, str) for v in ref.values()):
        raise CompletionError('Invalid lifecycle evidence reference')
    path = (root.parent / ref['path']).resolve()
    if not path.is_relative_to((root / 'runs').resolve()) and not path.is_relative_to((root / 'evidence').resolve()):
        raise CompletionError('Lifecycle evidence must remain in canonical runs/evidence')
    try: raw = path.read_bytes()
    except OSError as exc: raise CompletionError('Lifecycle evidence is unavailable') from exc
    if hashlib.sha256(raw).hexdigest() != ref['sha256']: raise CompletionError('Lifecycle evidence hash mismatch')
    value = json.loads(raw)
    if not isinstance(value, dict): raise CompletionError('Lifecycle evidence must be an object')
    return value


def bind(repo, task):
    return {'task_id': task['id'], 'project': task['project'], 'contract_digest': contract_digest(task),
            'revision': git_text(repo, 'rev-parse', 'HEAD'), 'content_digest': content_snapshot(repo, task)['digest']}


def changed_content(repo, task, snapshot):
    base = (task.get('claim') or {}).get('base_commit')
    if not base: raise CompletionError('Lifecycle proof requires exact claim base provenance')
    previous = content_snapshot(repo, task, base)['files']
    current = snapshot['files']
    return {name for name in previous.keys() | current.keys() if previous.get(name) != current.get(name)}


def attach(root, task_id, owner, phase, ref, *, task_lock_held=False):
    import contextlib
    with contextlib.nullcontext() if task_lock_held else repository_lock(root, 'task-' + task_id):
        task = active_task(root.parent, task_id, owner)
        manifest = task.setdefault('completion_evidence', {'schema': 'go-workflow.completion-evidence.v1'})
        manifest[phase] = ref
        atomic_json(root / 'tasks/active' / (task_id + '.json'), task)


def verification_session(repo, task):
    import socket
    from .run_state import RunSession, SCHEMA, state_path, read_state, require_stopped
    root = workflow_root(repo)
    task_id = task['id']
    path = state_path(root.parent, task_id, 'completion')
    with repository_lock(root, 'completion-state-' + task_id):
        history = []
        if path.exists():
            old = read_state(root.parent, task_id, 'completion'); require_stopped(old)
            history = old['history'] + [{'event': 'new_verification_capture', 'previous_inflight': old['inflight']}]
        record = registered_workspace(repo)
        state = {'schema': SCHEMA, 'task_id': task_id, 'project': task['project'], 'control_repo': str(root.parent),
                 'owner': task['claim']['agent'], 'run_id': record['run_id'] if record else 'verify-' + uuid.uuid4().hex,
                 'workspace': record or {'path': str(repo), 'control_repo': str(root.parent)},
                 'phase': 'verify', 'attempt': 1, 'check_index': 0, 'checks': [], 'phase_evidence': [],
                 'effects': {}, 'budgets': [], 'history': history, 'requirements': task.get('requested_outcomes', []),
                 'models': {}, 'task_hash': contract_digest(task), 'code': {}, 'execution_cwd': str(repo),
                 'controller': {'host': socket.gethostname(), 'pid': os.getpid()}, 'worker_group': None, 'inflight': None}
        atomic_json(path, state)
    return RunSession(root.parent, task_id, 'completion')


def execute_check(repo, task, command, session, *, timeout_seconds=900, binding=None):
    """Execute one declared command; retain full raw output before checkpointing."""
    from .cli import run_shell_with_timeout
    if command not in task['verification']: raise CompletionError('Undeclared verification command')
    root = workflow_root(repo)
    binding = binding or bind(repo, task)
    env = os.environ.copy()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='go-completion-cache-') as cache:
        env['PYTHONPYCACHEPREFIX'] = cache
        env['PYTEST_ADDOPTS'] = env.get('PYTEST_ADDOPTS', '') + ' -p no:cacheprovider'
        with session.operation():
            output = run_shell_with_timeout(repo, command, env, timeout_seconds)
        session.update(inflight=None)
    raw = save_artifact(root, task['id'], 'command', {**binding, 'command': command, 'cwd': str(repo),
                         'elapsed_seconds': time.monotonic() - started, **output})
    proof = {'schema': 'go-workflow.verification-evidence.v1', 'task_id': task['id'], 'phase_id': 'verify',
        'requirement_ids': [item['id'] for item in task.get('requested_outcomes', [])] or ['acceptance'],
        'status': 'passed' if output['returncode'] == 0 else 'failed', 'command': command, 'cwd': str(repo),
        'revision': binding['revision'], 'worktree_digest': binding['content_digest'],
        'exit_code': output['returncode'], 'evidence': [raw['path']]}
    return {'verification': proof, 'raw': raw}, output


def finalize_checks(repo, task, owner, checks, *, binding=None, task_lock_held=False):
    root = workflow_root(repo)
    binding = binding or bind(repo, task)
    unchanged = content_snapshot(repo, task)['digest'] == binding['content_digest']
    matched = len(checks) == len(task['verification']) and all(
        item['verification']['command'] == command and item['verification']['exit_code'] == 0
        and all(read_artifact(root, item['raw']).get(key) == value for key, value in binding.items())
        for command, item in zip(task['verification'], checks))
    artifact = {'schema': 'go-workflow.executed-verification.v1', **binding, 'checks': checks,
        'content_unchanged': unchanged, 'status': 'passed' if unchanged and matched else 'failed'}
    ref = save_artifact(root, task['id'], 'verification', artifact)
    attach(root, task['id'], owner, 'verification', ref, task_lock_held=task_lock_held)
    return artifact, ref


def capture_verification(repo, task_id, owner, *, timeout_seconds=900):
    root = workflow_root(repo)
    with repository_lock(root, 'completion-' + task_id):
        task = active_task(root.parent, task_id, owner)
        if not task.get('verification'): raise CompletionError('No declared verification commands')
        with proof_lease(repo, task_id, owner), repository_lock(root, 'task-' + task_id):
            task = active_task(root.parent, task_id, owner)
            binding = bind(repo, task)
            session = verification_session(repo, task)
            checks = [execute_check(repo, task, command, session, timeout_seconds=timeout_seconds, binding=binding)[0]
                      for command in task['verification']]
            artifact, ref = finalize_checks(repo, task, owner, checks, binding=binding, task_lock_held=True)
            session.update(phase='complete', checks=checks)
            return artifact, ref


def record_critic(repo, task_id, owner, review):
    root = workflow_root(repo)
    with repository_lock(root, 'completion-' + task_id), proof_lease(repo, task_id, owner):
        task = active_task(root.parent, task_id, owner)
        binding = bind(repo, task)
        if (review.get('schema') != 'go-workflow.critic-review.v1' or review.get('status') != 'passed'
                or any(review.get(key) != value for key, value in binding.items())
                or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()
                or review.get('review_mode') not in {'same_agent', 'independent'}
                or not isinstance(review.get('summary'), str) or not review['summary'].strip()
                or review.get('blocking_findings') != []):
            raise CompletionError('Critic review must explicitly pass for the current task/contract/content')
        required = changed_content(repo, task, content_snapshot(repo, task))
        reviewed = review.get('reviewed_paths')
        if (not isinstance(reviewed, list) or not reviewed or not all(isinstance(p, str) and p for p in reviewed)
                or not required.issubset(set(reviewed))):
            raise CompletionError('Critic review does not cover changed product paths')
        ref = save_artifact(root, task_id, 'critic', review)
        attach(root, task_id, owner, 'critic', ref)
        return ref


def completion_findings(repo, task, *, current=True, remote=True, phase_only=False):
    if 'execution_contract' not in task: return []
    root = workflow_root(repo)
    # Historical proof is portable Git/evidence data. Old process identities
    # govern current mutation, never whether a completed release can be read.
    if current:
        try:
            from .worktrees import require_run_idle
            require_run_idle({'control_repo': str(root.parent), 'task_id': task['id']})
        except ValueError as exc:
            return ['lifecycle: ' + str(exc)]
    manifest = task.get('completion_evidence')
    if not isinstance(manifest, dict) or manifest.get('schema') != 'go-workflow.completion-evidence.v1':
        return ['lifecycle evidence is missing; run declared verification, record review and verify required shipping']
    try:
        if set(manifest) - {'schema', 'verification', 'critic', 'release'}:
            raise CompletionError('Unknown lifecycle evidence phase')
        verify = read_artifact(root, manifest.get('verification'))
        critic = read_artifact(root, manifest.get('critic'))
        for artifact in (verify, critic):
            if (artifact.get('task_id') != task['id'] or artifact.get('project') != task['project']
                    or artifact.get('contract_digest') != contract_digest(task) or artifact.get('status') != 'passed'):
                raise CompletionError('Lifecycle evidence belongs to another task/contract or did not pass')
        digest = verify.get('content_digest')
        if not digest or critic.get('content_digest') != digest: raise CompletionError('Verification and critic content differ')
        snapshot = content_snapshot(repo, task) if current else content_snapshot(repo, task, verify['revision'])
        if current and snapshot['digest'] != digest:
            raise CompletionError('Product content changed after verification/review')
        if verify.get('schema') != 'go-workflow.executed-verification.v1' or verify.get('content_unchanged') is not True:
            raise CompletionError('Verification did not execute on unchanged content')
        checks = verify.get('checks')
        if not isinstance(checks, list) or not checks or len(checks) != len(task.get('verification', [])):
            raise CompletionError('Mandatory verification commands are missing')
        requirements = [item['id'] for item in task.get('requested_outcomes', [])] or ['acceptance']
        for expected, item in zip(task['verification'], checks):
            if not isinstance(item, dict): raise CompletionError('Invalid mandatory check record')
            proof = item.get('verification', {})
            raw = read_artifact(root, item.get('raw'))
            if (validate_verification_evidence(proof) or proof.get('status') != 'passed' or proof.get('command') != expected
                    or proof.get('task_id') != task['id'] or proof.get('phase_id') != 'verify'
                    or proof.get('requirement_ids') != requirements
                    or proof.get('worktree_digest') != digest or raw.get('content_digest') != digest
                    or raw.get('command') != expected or raw.get('cwd') != proof.get('cwd')
                    or raw.get('revision') != proof.get('revision') or proof.get('revision') != verify.get('revision')
                    or any(raw.get(key) != verify.get(key) for key in ('task_id', 'project', 'contract_digest'))
                    or type(raw.get('returncode')) is not int or raw.get('returncode') != 0 or raw.get('timed_out') is not False
                    or proof.get('evidence') != [item['raw']['path']]):
                raise CompletionError('Mandatory check lacks matching executed command/raw proof')
        if (critic.get('schema') != 'go-workflow.critic-review.v1' or critic.get('blocking_findings') != []
                or not critic.get('reviewer') or not critic.get('reviewed_paths') or not critic.get('summary')
                or critic.get('review_mode') not in {'same_agent', 'independent'}):
            raise CompletionError('Critic evidence is incomplete or blocked')
        if current and not changed_content(repo, task, snapshot).issubset(set(critic['reviewed_paths'])):
            raise CompletionError('Critic proof does not cover current changed product paths')
        # Internal publisher precondition, before a release can exist. Public
        # finish/approval always use the default complete lifecycle gate.
        if phase_only: return []
        refs = {manifest[name]['path'] for name in ('verification', 'critic')}
        release = task['execution_contract']['release']
        if release['mode'] == 'required':
            from .shipping import verify_release_evidence
            proof = read_artifact(root, manifest.get('release'))
            verify_release_evidence(repo, task, proof, digest, remote=remote)
            refs.add(manifest['release']['path'])
        elif release.get('mode') != 'none' or not release.get('reason'):
            raise CompletionError('Explicit release policy is required')
        for outcome in task.get('requested_outcomes', []):
            evidence = outcome.get('evidence') or []
            if not isinstance(evidence, list): raise CompletionError('Requirement evidence must be a list')
            # The public outcome command stores attribution objects. Their
            # exact summary is the proof pointer; older direct references remain
            # readable, but arbitrary prose or nested objects are never paths.
            pointers = [item.get('summary') if isinstance(item, dict) else item for item in evidence]
            pointers = {item for item in pointers if isinstance(item, str)}
            if outcome.get('status') != 'verified' or not refs.intersection(pointers):
                raise CompletionError('Requirement lacks verified lifecycle proof: ' + str(outcome.get('id')))
        return []
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return ['lifecycle: ' + str(exc)]


def require_completion(repo, task):
    if 'execution_contract' in task:
        current = active_task(workflow_root(repo).parent, task['id'])
        if (contract_digest(current) != contract_digest(task)
                or current.get('completion_evidence') != task.get('completion_evidence')
                or current.get('requested_outcomes') != task.get('requested_outcomes')):
            from .cli import RepoLocalError
            raise RepoLocalError('lifecycle task snapshot is stale; reload canonical proof and outcomes')
    findings = completion_findings(repo, task)
    if findings:
        from .cli import RepoLocalError
        raise RepoLocalError('lifecycle completion blocked:\n- ' + '\n- '.join(findings))


def lifecycle_report(repo):
    root = workflow_root(repo)
    report = {'verified_done': [], 'invalid_done': {}, 'historical_unmigrated': []}
    for path in sorted((root / 'tasks/done').glob('*.json')):
        task = read_object(path)
        # History is not silently migrated. Explicit profile/proof adoption opts
        # current records into audit; missing proof on those records is visible.
        profile = ((task.get('execution_contract') or {}).get('release') or {}).get('profile')
        if 'execution_contract' not in task or (not task.get('completion_evidence') and not profile):
            report['historical_unmigrated'].append(task['id']); continue
        findings = completion_findings(repo, task, current=False, remote=False)
        if findings: report['invalid_done'][task['id']] = findings
        else: report['verified_done'].append(task['id'])
    report['evidence_valid'] = not report['invalid_done']
    report['remote_policy'] = 'Historical readback evidence; finish/approval performs fresh remote verification.'
    return report
