"""Configured task publication with durable intent and exact effect readback."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
from contextvars import ContextVar

from .completion import (completion_findings, content_snapshot, contract_digest,
                         read_artifact, CompletionError)
from .execution_context import json_hash
from .state_io import atomic_json, atomic_write_text, repository_lock
from .worktrees import (active_task, checked_scope, git, git_text, owned_record,
                        read_object, registry_path, require_clean, require_run_idle,
                        stage_workspace, verify_workspace)

SCHEMA = 'go-workflow.publication-state.v1'
BUDGET = ContextVar('publication_budget', default=None)
VERSION = re.compile(r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')


class PublicationError(ValueError):
    pass


class PublicationBudget(PublicationError):
    pass


@contextmanager
def publication_budget(max_commands, deadline):
    value = {'maximum': max_commands, 'used': 0, 'deadline': deadline}
    token = BUDGET.set(value)
    try: yield value
    finally: BUDGET.reset(token)


def remaining_timeout(default):
    budget = BUDGET.get()
    remaining = budget['deadline'] - time.monotonic() if budget else default
    if remaining <= 0: raise PublicationBudget('Publication time budget exhausted; resume exact pending intent')
    return min(default, remaining)


def relative_path(value):
    return (isinstance(value, str) and bool(value) and bool(Path(value).parts) and not Path(value).is_absolute()
            and '..' not in Path(value).parts and Path(value).parts[0] not in {'.go', '.git'}
            and not any(c in value for c in '\n\r\x00'))


def validate_publication(value):
    if not isinstance(value, dict) or set(value) != {'version', 'bump', 'tag_prefix', 'changelog'}:
        return ['publication requires version, bump, tag_prefix and changelog']
    errors = []
    version = value['version']
    if (not isinstance(version, dict) or not isinstance(version.get('format'), str) or version.get('format') not in {'text', 'json'}
            or set(version) != ({'path', 'format', 'key'} if version.get('format') == 'json' else {'path', 'format'})
            or not relative_path(version.get('path'))):
        errors.append('publication version source must be an explicit text/JSON path')
    elif version['format'] == 'json' and (not isinstance(version['key'], str) or not version['key']
                                        or any(not key for key in version['key'].split('.'))):
        errors.append('publication JSON version key is invalid')
    if not isinstance(value['bump'], str) or value['bump'] not in {'major', 'minor', 'patch'}: errors.append('publication bump is invalid')
    if not isinstance(value['tag_prefix'], str) or not re.fullmatch(r'[A-Za-z0-9._-]*', value['tag_prefix']):
        errors.append('publication tag prefix is invalid')
    if not relative_path(value['changelog']): errors.append('publication changelog path is invalid')
    if isinstance(version, dict) and value['changelog'] == version.get('path'):
        errors.append('version and changelog must be separate files')
    return errors


def publication_profile(control, task):
    from .shipping import release_profile
    identity = release_profile(control, task)
    value = read_object(control / '.go/project.json')['release_profiles'][identity['name']].get('publication')
    errors = validate_publication(value)
    if errors: raise PublicationError('Explicit publication profile required: ' + '; '.join(errors))
    return {**identity, 'publication': deepcopy(value)}


def configured(control, task):
    name = ((task.get('execution_contract') or {}).get('release') or {}).get('profile')
    value = read_object(control / '.go/project.json').get('release_profiles', {}).get(name, {})
    return isinstance(value, dict) and 'publication' in value


def state_path(control, task_id):
    registry_path(control, task_id)
    return control / '.go/runs' / task_id / 'release-state.json'


def validate_state(value):
    required = {'schema', 'task_id', 'project', 'owner', 'run_id', 'control_repo', 'workspace', 'profile',
                'contract_digest', 'authority', 'base_commit', 'remote_base', 'remote_url', 'version', 'tag',
                'phase', 'preparation', 'effects', 'observations', 'reservation', 'notes'}
    if (not isinstance(value, dict) or not required.issubset(value)
            or set(value) - required - {'commit', 'tag_object', 'content_digest', 'release_evidence'}
            or value.get('schema') != SCHEMA or not isinstance(value.get('phase'), str) or value.get('phase') not in {'preparing', 'prepared', 'publishing', 'finishing', 'published'}
            or not isinstance(value.get('preparation'), list) or not isinstance(value.get('effects'), dict)
            or not isinstance(value.get('observations'), list)):
        raise PublicationError('Invalid publication checkpoint')
    for name in ('task_id', 'project', 'owner', 'run_id', 'control_repo', 'remote_url', 'version', 'tag', 'reservation', 'notes'):
        if not isinstance(value[name], str) or not value[name]: raise PublicationError('Invalid publication ' + name)
    for name in ('base_commit', 'remote_base', 'commit', 'tag_object'):
        if name in value and not re.fullmatch('[0-9a-f]{40}', str(value[name])): raise PublicationError('Invalid publication Git identity')
    if value['authority'] != {'ship_policy': 'push', 'allow_push': True, 'actor': value['owner']}:
        raise PublicationError('Publication checkpoint lacks explicit authorization')
    if not isinstance(value['profile'], dict) or validate_publication(value['profile'].get('publication')) or not VERSION.fullmatch(value['version']):
        raise PublicationError('Invalid frozen publication profile/version')
    from .worktrees import validate_record
    from .shipping import validate_release_profiles
    if not isinstance(value['workspace'], dict): raise PublicationError('Invalid publication workspace')
    try: validate_record(value['workspace'])
    except ValueError as exc: raise PublicationError('Invalid publication workspace: ' + str(exc)) from exc
    profile = value['profile']
    if (not isinstance(profile.get('name'), str) or not profile['name']
            or validate_release_profiles({profile['name']: {key: item for key, item in profile.items() if key != 'name'}})):
        raise PublicationError('Invalid frozen publication profile')
    for key in ('contract_digest', 'content_digest'):
        if key in value and (not isinstance(value[key], str) or not re.fullmatch('[0-9a-f]{64}', value[key])):
            raise PublicationError('Invalid publication content/contract digest')
    if len(value['preparation']) != 2: raise PublicationError('Invalid publication preparation list')
    expected_paths = [profile['publication']['version']['path'], profile['publication']['changelog']]
    for change, path in zip(value['preparation'], expected_paths):
        if (not isinstance(change, dict) or set(change) != {'path', 'before', 'after'} or change['path'] != path
                or not isinstance(change['before'], str) or not re.fullmatch('[0-9a-f]{64}', change['before'])
                or not isinstance(change['after'], str)):
            raise PublicationError('Invalid publication preparation item')
    allowed_effects = {'commit', 'integration', 'tag', 'push', 'publication'}
    for key, effect in value['effects'].items():
        if (key not in allowed_effects or not isinstance(effect, dict) or set(effect) != {'expected', 'status'}
                or not isinstance(effect['status'], str) or effect['status'] not in {'pending', 'confirmed'}):
            raise PublicationError('Invalid publication effect')
    for item in value['observations']:
        if (not isinstance(item, dict) or set(item) != {'effect', 'observed'}
                or not isinstance(item['effect'], str) or item['effect'] not in allowed_effects):
            raise PublicationError('Invalid publication readback observation')
    if value['tag'] != profile['publication']['tag_prefix'] + value['version']:
        raise PublicationError('Publication version/tag mismatch')
    if 'release_evidence' in value:
        ref = value['release_evidence']
        if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'} or not isinstance(ref['path'], str)
                or not isinstance(ref['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', ref['sha256'])):
            raise PublicationError('Invalid publication evidence reference')
    return value


def _load(control, task, owner, run_id):
    value = validate_state(read_object(state_path(control, task['id'])))
    if (any(value[key] != expected for key, expected in {'control_repo': str(control), 'task_id': task['id'],
            'project': task['project'], 'owner': owner, 'run_id': run_id, 'contract_digest': contract_digest(task),
            'profile': publication_profile(control, task)}.items())):
        raise PublicationError('Frozen publication task/run/owner/profile changed; reconcile explicitly')
    record = owned_record(control, task['id'], owner, run_id, active=False)
    binding = ('control_repo', 'path', 'owner', 'run_id', 'branch', 'base_branch', 'base_commit',
               'common_dir', 'repository_id', 'generation')
    if any(record[key] != value['workspace'].get(key) for key in binding):
        raise PublicationError('Frozen publication workspace identity changed')
    reservation = (control / value['reservation']).resolve()
    if reservation.parent != (control / '.go/runs/publication-reservations').resolve():
        raise PublicationError('Publication reservation escaped canonical state')
    if value['phase'] != 'published':
        expected = {'task_id': task['id'], 'run_id': run_id, 'owner': owner, 'version': value['version'], 'status': 'reserved'}
        if reservation.exists() and read_object(reservation) != expected:
            raise PublicationError('Publication reservation ownership changed; explicit reconciliation required')
        if not reservation.exists() and value['phase'] != 'preparing':
            raise PublicationError('Publication reservation is missing; reconcile before further effects')
    return value


def _save(control, state):
    validate_state(state)
    atomic_json(state_path(control, state['task_id']), state)


@contextmanager
def publication_slot(control, task_id, owner, run_id):
    from .run_state import (RunSession, SCHEMA as PROCESS_SCHEMA, state_path as process_path,
                            read_state, require_stopped, group_alive)
    with repository_lock(control / '.go', 'workspace-integration', timeout_seconds=.1):
        record = owned_record(control, task_id, owner, run_id, active=False)
        require_run_idle(record)
        target = process_path(control, task_id, 'publication')
        with repository_lock(control / '.go', 'publication-state-' + task_id):
            history = []
            if target.exists():
                old = read_state(control, task_id, 'publication'); require_stopped(old)
                history = old['history'] + [{'event': 'publication_controller_resumed', 'previous_inflight': old['inflight']}]
            task = active_task(control, task_id)
            atomic_json(target, {'schema': PROCESS_SCHEMA, 'task_id': task_id, 'project': task['project'],
                'control_repo': str(control), 'owner': owner, 'run_id': run_id, 'workspace': record,
                'phase': 'release', 'attempt': 1, 'check_index': 0, 'checks': [], 'phase_evidence': [],
                'effects': {}, 'budgets': [], 'history': history, 'requirements': task.get('requested_outcomes', []),
                'models': {}, 'task_hash': contract_digest(task), 'code': {}, 'execution_cwd': str(control),
                'controller': {'host': socket.gethostname(), 'pid': os.getpid()}, 'worker_group': None, 'inflight': None})
        session = RunSession(control, task_id, 'publication')
        try: yield record, session
        finally:
            if not group_alive(session.load().get('worker_group')):
                session.update(phase='complete', worker_group=None, inflight=None)


def _write_command(session, cwd, argv):
    from .cli import run_shell_with_timeout
    budget = BUDGET.get()
    if budget and budget['used'] >= budget['maximum']:
        raise PublicationBudget('Publication command budget exhausted; resume exact pending intent')
    timeout = remaining_timeout(120)
    if budget: budget['used'] += 1
    session.update(execution_cwd=str(cwd))
    with session.operation():
        result = run_shell_with_timeout(cwd, shlex.join(argv), _environment(), timeout)
    session.update(inflight=None)
    if result['returncode']:
        raise PublicationError('Publication command failed; inspect/reconcile before retry: ' + result['stderr'][-1500:])
    return result


def _environment():
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE'):
        env.pop(key, None)
    return env


def _remote(control, profile):
    from .shipping import release_profile
    url = git_text(control, 'remote', 'get-url', profile['remote'])
    if profile['provider'] == 'github-release':
        expected = 'https://github.com/' + profile['repository']
        if url not in {expected, expected + '.git'}: raise PublicationError('GitHub profile/remote identity mismatch')
    elif not (url.startswith(('https://', 'ssh://', 'git@')) or Path(url).is_absolute()):
        raise PublicationError('Publication refuses implicit executable remote helpers')
    try:
        result = subprocess.run(['git', '-C', str(control), '-c', 'protocol.ext.allow=never', 'ls-remote', '--', profile['remote']],
                                env=_environment(), text=True, capture_output=True, timeout=remaining_timeout(30))
    except subprocess.TimeoutExpired as exc: raise PublicationError('Remote readback unavailable; not absence') from exc
    if result.returncode: raise PublicationError('Remote readback unavailable; not absence: ' + result.stderr[-1000:])
    refs = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2: raise PublicationError('Malformed remote readback')
        oid, ref = parts
        if not re.fullmatch('[0-9a-f]{40}', oid) or ref in refs: raise PublicationError('Malformed remote readback')
        refs[ref] = oid
    return url, refs


def _file(worker, task, path):
    if not relative_path(path) or not any(fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern.rstrip('/') + '/')
                                         for pattern in task['scope']['modify']):
        raise PublicationError('Release preparation path is outside task scope: ' + str(path))
    target = worker / path
    if target.is_symlink() or not target.resolve().is_relative_to(worker.resolve()):
        raise PublicationError('Release preparation refuses foreign/symlink files')
    return target


def _version_change(text, spec, replacement=None):
    if spec['format'] == 'text': return text.strip() if replacement is None else replacement + '\n'
    try:
        value = json.loads(text); parent = value
        keys = spec['key'].split('.')
        for key in keys[:-1]: parent = parent[key]
        if replacement is None: return parent[keys[-1]]
        parent[keys[-1]] = replacement
        return json.dumps(value, indent=2, ensure_ascii=False) + '\n'
    except (KeyError, TypeError, ValueError) as exc: raise PublicationError('Version JSON source/key is invalid') from exc


def _hash(text): return hashlib.sha256(text.encode()).hexdigest()


def prepare_release(control, task_id, owner, run_id, *, ship_policy='none', allow_push=False):
    control = Path(control).resolve()
    with publication_slot(control, task_id, owner, run_id) as (record, session):
        task = active_task(control, task_id, owner); profile = publication_profile(control, task)
        worker = Path(record['path']); verify_workspace(record); checked_scope(record)
        if record['state'] != 'ready' or profile['branch'] != record['base_branch']:
            raise PublicationError('Publication preparation requires the owned ready base branch')
        path = state_path(control, task_id)
        resuming = path.exists()
        if resuming: state = _load(control, task, owner, run_id)
        else:
            if ship_policy != 'push' or allow_push is not True: raise PublicationError('Explicit push authorization is required')
            url, refs = _remote(control, profile)
            remote_base = refs.get('refs/heads/' + profile['branch'])
            if remote_base != record['base_commit'] or git_text(control, 'rev-parse', 'HEAD') != record['base_commit']:
                raise PublicationError('Base/remote advanced; preserve workspace and reconcile before reserving a version')
            spec = profile['publication']; version_path = _file(worker, task, spec['version']['path'])
            before = version_path.read_text(); current = _version_change(before, spec['version'])
            if not isinstance(current, str) or not VERSION.fullmatch(current): raise PublicationError('Version source must contain semantic X.Y.Z')
            prefix = 'refs/tags/' + spec['tag_prefix']
            versions = [ref[len(prefix):] for ref in refs if ref.startswith(prefix) and VERSION.fullmatch(ref[len(prefix):])]
            latest = max(versions, key=lambda value: tuple(map(int, value.split('.')))) if versions else current
            if latest != current: raise PublicationError('Version source differs from latest remote release; reconcile the base')
            parts = list(map(int, current.split('.'))); index = {'major': 0, 'minor': 1, 'patch': 2}[spec['bump']]
            parts[index] += 1
            for following in range(index + 1, 3): parts[following] = 0
            version = '.'.join(map(str, parts)); tag = spec['tag_prefix'] + version
            channel = hashlib.sha256((url + '\0' + spec['tag_prefix']).encode()).hexdigest()
            reservation = control / '.go/runs/publication-reservations' / (channel + '.json')
            if reservation.exists() and read_object(reservation).get('status') != 'released':
                existing = read_object(reservation)
                if existing.get('task_id') != task_id or existing.get('run_id') != run_id:
                    raise PublicationError('Another task owns the publication channel reservation')
            changelog = _file(worker, task, spec['changelog'])
            old_log = changelog.read_text() if changelog.exists() else ''
            notes = '## ' + version + '\n\n- ' + task['summary'] + '\n'
            state = {'schema': SCHEMA, 'task_id': task_id, 'project': task['project'], 'owner': owner, 'run_id': run_id,
                'control_repo': str(control), 'workspace': record, 'profile': profile, 'contract_digest': contract_digest(task),
                'authority': {'ship_policy': 'push', 'allow_push': True, 'actor': owner}, 'base_commit': record['base_commit'],
                'remote_base': remote_base, 'remote_url': url, 'version': version, 'tag': tag, 'phase': 'preparing',
                'effects': {}, 'observations': [], 'reservation': str(reservation.relative_to(control)), 'notes': notes,
                'preparation': [{'path': spec['version']['path'], 'before': _hash(before),
                                 'after': _version_change(before, spec['version'], version)},
                                {'path': spec['changelog'], 'before': _hash(old_log), 'after': notes + '\n' + old_log}]}
            _save(control, state)  # intent precedes both the reservation and file writes
        reservation = control / state['reservation']
        if reservation.exists():
            existing = read_object(reservation)
            expected = {'task_id': task_id, 'run_id': run_id, 'owner': owner, 'version': state['version'], 'status': 'reserved'}
            if existing != expected and (resuming or existing.get('status') != 'released'):
                raise PublicationError('Publication reservation ownership changed; explicit reconciliation required')
        atomic_json(reservation, {'task_id': task_id, 'run_id': run_id, 'owner': owner, 'version': state['version'], 'status': 'reserved'})
        if state['phase'] == 'preparing':
            for change in state['preparation']:
                target = _file(worker, task, change['path']); current = target.read_text() if target.exists() else ''
                if current == change['after']: continue
                if _hash(current) != change['before']: raise PublicationError('Preparation file changed; preserve it and reconcile')
                atomic_write_text(target, change['after'])
            checked_scope(record); state['phase'] = 'prepared'; _save(control, state)
        return deepcopy(state)


def _intent(control, state, key, expected):
    effect = state['effects'].get(key)
    if effect and effect['expected'] != expected: raise PublicationError('Publication effect identity changed: ' + key)
    if not effect:
        state['effects'][key] = {'expected': expected, 'status': 'pending'}; _save(control, state)


def _confirmed(control, state, key, observed):
    if state['effects'][key]['expected'] != observed: raise PublicationError('Publication readback differs from intent: ' + key)
    state['effects'][key]['status'] = 'confirmed'
    state['observations'].append({'effect': key, 'observed': observed}); _save(control, state)


def _commit(control, state, record, session, task):
    worker = Path(record['path'])
    message = 'go release: ' + task['id'] + ' ' + state['run_id'] + ' ' + state['tag']
    if 'commit' not in state['effects']:
        stage_workspace(control, task['id'], state['owner'], state['run_id'],
            command_runner=lambda cwd, argv: _write_command(session, cwd, argv))
        tree = git_text(worker, 'write-tree')
        parent = git_text(worker, 'rev-parse', 'HEAD')
        _intent(control, state, 'commit', {'parent': parent, 'tree': tree, 'message': message})
    expected = state['effects']['commit']['expected']; head = git_text(worker, 'rev-parse', 'HEAD')
    if head == expected['parent']:
        _write_command(session, worker, ['git', 'commit', '-m', message])
        head = git_text(worker, 'rev-parse', 'HEAD')
    observed = {'parent': git_text(worker, 'rev-parse', head + '^'), 'tree': git_text(worker, 'rev-parse', head + '^{tree}'),
                'message': git_text(worker, 'show', '-s', '--format=%B', head)}
    _confirmed(control, state, 'commit', observed)
    state['commit'] = head; _save(control, state); require_clean(record)
    if content_snapshot(worker, task, head)['digest'] != state['content_digest']:
        raise PublicationError('Committed content differs from verified candidate')


def _integrate(control, state, record, session, task):
    branch = state['profile']['branch']; commit = state['commit']
    _intent(control, state, 'integration', commit)
    head = git_text(control, 'rev-parse', 'HEAD')
    if git_text(control, 'symbolic-ref', '--short', 'HEAD') != branch: raise PublicationError('Control checkout changed branch')
    if head == state['base_commit']:
        if content_snapshot(control, task)['digest'] != content_snapshot(control, task, head)['digest']:
            raise PublicationError('Control product content is dirty; preserve unrelated work')
        _write_command(session, control, ['git', 'merge', '--ff-only', commit])
        head = git_text(control, 'rev-parse', 'HEAD')
    _confirmed(control, state, 'integration', head)
    # Caller holds the existing global integration slot. The writer guard keeps
    # other task processes out; preserve the manager's exact integration shape.
    with repository_lock(control / '.go', 'workspace-execution-' + task['id']):
        record.update(state='integrated', integration={'workspace_head': commit, 'integrated_commit': commit})
        atomic_json(registry_path(control, task['id']), record)


def _tag(control, state, session):
    message = 'go release ' + state['task_id'] + ' ' + state['run_id'] + ' ' + state['commit']
    _intent(control, state, 'tag', {'commit': state['commit'], 'message': message})
    tagref = 'refs/tags/' + state['tag']
    if git(control, 'show-ref', '--verify', '--quiet', tagref, check=False).returncode:
        _write_command(session, control, ['git', 'tag', '-a', state['tag'], state['commit'], '-m', message])
    oid = git_text(control, 'rev-parse', tagref)
    if git_text(control, 'cat-file', '-t', oid) != 'tag': raise PublicationError('Conflicting non-annotated tag')
    raw = git_text(control, 'cat-file', 'tag', oid)
    observed = {'commit': git_text(control, 'rev-parse', oid + '^{commit}'), 'message': raw.split('\n\n', 1)[1]}
    _confirmed(control, state, 'tag', observed); state['tag_object'] = oid; _save(control, state)


def _push(control, state, session):
    profile = state['profile']; branch = 'refs/heads/' + profile['branch']; tag = 'refs/tags/' + state['tag']
    expected = {branch: state['commit'], tag: state['tag_object'], tag + '^{}': state['commit']}
    _intent(control, state, 'push', expected)
    url, refs = _remote(control, profile)
    if url != state['remote_url']: raise PublicationError('Publication remote changed')
    observed = {key: refs.get(key) for key in expected}
    if observed != expected:
        if refs.get(branch) != state['remote_base'] or tag in refs:
            raise PublicationError('Remote branch/tag conflicts with publication intent; preserve pending state')
        _write_command(session, control, ['git', 'push', '--atomic', profile['remote'],
                                         state['commit'] + ':' + branch, state['tag_object'] + ':' + tag])
        _, refs = _remote(control, profile); observed = {key: refs.get(key) for key in expected}
    _confirmed(control, state, 'push', observed)


def _github_observation(profile, tag):
    try: return _read_github(profile, tag)
    except (subprocess.TimeoutExpired, OSError, ValueError, TypeError, AttributeError) as exc:
        if isinstance(exc, PublicationError): raise
        raise PublicationError('GitHub publication readback unavailable; not absence') from exc


def _read_github(profile, tag):
    # HTTP 404 is explicit absence; auth, transport and parse failures are unknown.
    command = ['gh', 'api', 'repos/' + profile['repository'] + '/releases/tags/' + tag]
    result = subprocess.run(command, text=True, capture_output=True, timeout=remaining_timeout(30))
    if result.returncode:
        if '(HTTP 404)' in result.stderr:
            access = subprocess.run(['gh', 'api', 'repos/' + profile['repository'] + '/releases?per_page=1'],
                                    text=True, capture_output=True, timeout=remaining_timeout(30))
            if not access.returncode and isinstance(json.loads(access.stdout), list): return None
        raise PublicationError('GitHub publication readback unavailable; not absence')
    value = json.loads(result.stdout)
    if value.get('tag_name') != tag or value.get('draft') is not False: raise PublicationError('Conflicting GitHub publication')
    return value


def _publish_github(control, state, session):
    if state['profile']['provider'] != 'github-release': return
    expected = {'tag': state['tag'], 'body': state['notes']}
    _intent(control, state, 'publication', expected)
    observed = _github_observation(state['profile'], state['tag'])
    if observed is None:
        notes = control / '.go/runs' / state['task_id'] / 'publication-notes.md'; atomic_write_text(notes, state['notes'])
        _write_command(session, control, ['gh', 'release', 'create', state['tag'], '--repo', state['profile']['repository'],
                                         '--verify-tag', '--title', state['tag'], '--notes-file', str(notes)])
        observed = _github_observation(state['profile'], state['tag'])
    if observed is None: raise PublicationError('GitHub publication not confirmed')
    _confirmed(control, state, 'publication', {'tag': observed['tag_name'], 'body': observed.get('body')})


def _release_reservation(control, state):
    path = control / state['reservation']
    expected = {'task_id': state['task_id'], 'run_id': state['run_id'], 'owner': state['owner'],
                'version': state['version'], 'status': 'reserved'}
    # Historical readback may run after another task acquired this channel.
    if path.exists() and read_object(path) == expected:
        atomic_json(path, {**expected, 'status': 'released'})


def publish_release(control, task_id, owner, run_id):
    from . import cli as api
    from .shipping import capture_release, verify_release_evidence
    from argparse import Namespace
    control = Path(control).resolve()
    with publication_slot(control, task_id, owner, run_id) as (record, session):
        task = active_task(control, task_id); state = _load(control, task, owner, run_id)
        if state['phase'] == 'published':
            proof = read_artifact(control / '.go', state['release_evidence'])
            verify_release_evidence(control, task, proof, state['content_digest'])
            _release_reservation(control, state)
            return {'status': 'published', 'task_id': task_id, 'commit': state['commit'], 'tag': state['tag'], 'cleanup': 'separate'}
        remaining_timeout(120)
        if state['phase'] == 'preparing': raise PublicationError('Release preparation is incomplete')
        worker = Path(record['path']); verify_workspace(record); checked_scope(record)
        if any(outcome['status'] in {'blocked', 'rejected'} for outcome in task.get('requested_outcomes', [])):
            raise PublicationError('Explicitly blocked/rejected requirement needs resolution before publication')
        if state['phase'] != 'finishing':
            findings = completion_findings(worker, task, phase_only=True)
            if findings: raise PublicationError('Final candidate proof blocked: ' + '; '.join(findings))
            if api.architecture_finish_findings(control / '.go', task):
                raise PublicationError('Architecture proof is incomplete before publication')
            url, refs = _remote(control, state['profile'])
            branch = 'refs/heads/' + state['profile']['branch']
            if url != state['remote_url'] or refs.get(branch) not in {state['remote_base'], state.get('commit')}:
                raise PublicationError('Remote base advanced; final candidate must be reconciled and verified again')
            state.update(phase='publishing', content_digest=content_snapshot(worker, task)['digest']); _save(control, state)
            _commit(control, state, record, session, task)
            _integrate(control, state, record, session, task)
            if content_snapshot(control, task)['digest'] != state['content_digest']:
                raise PublicationError('Integrated product content differs from final proof')
            _tag(control, state, session); _push(control, state, session); _publish_github(control, state, session)
            _, ref = capture_release(control, task_id, owner, state['tag'])
            state.update(phase='finishing', release_evidence=ref); _save(control, state)
        remaining_timeout(120)
        task = active_task(control, task_id)
        if task['status'] == 'active':
            for outcome in task.get('requested_outcomes', []):
                if outcome['status'] in {'blocked', 'rejected'}: raise PublicationError('Explicitly blocked/rejected requirement needs resolution')
                with redirect_stdout(io.StringIO()):
                    api.cmd_task_outcome(Namespace(repo=str(control), task_id=task_id, outcome=outcome['id'], status='verified',
                        evidence=state['release_evidence']['path'], agent=owner))
            summary = ('changed_files=' + ','.join(checked_scope(record)) + '; verification_command=declared task commands; '
                'verification_result=passed; critic=passed recorded review; runtime=configured controller publisher; '
                'model=requested ' + task['execution_contract']['model']['id'] + ' (effective identity unconfirmed); billing_mode=unknown; '
                'release=' + state['tag'] + ' ' + state['commit'])
            with redirect_stdout(io.StringIO()): api.cmd_finish(Namespace(repo=str(control), task_id=task_id, agent=owner, evidence=summary))
        with redirect_stdout(io.StringIO()):
            api.cmd_task_review(api.build_parser().parse_args(['task', 'review', str(control), '--task-id', task_id,
                '--status', 'approved', '--agent', owner, '--evidence',
                'Recorded critic and exact release readback passed; no independent identity attestation.']))
        state['phase'] = 'published'; _save(control, state)
        _release_reservation(control, state)
        return {'status': 'published', 'task_id': task_id, 'commit': state['commit'], 'tag': state['tag'], 'cleanup': 'separate'}
