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
import stat
import subprocess
import time
import tomllib
from contextvars import ContextVar

from .completion import (completion_findings, content_snapshot, contract_digest,
                         read_artifact, CompletionError)
from .execution_context import json_hash
from .state_io import atomic_json, atomic_write_text, repository_lock
from .worktrees import (active_task, checked_scope, git, git_text, owned_record,
                        read_object, registry_path, require_clean, require_run_idle,
                        stage_workspace, verify_workspace, validate_record)

SCHEMA = 'go-workflow.publication-state.v1'
BUDGET = ContextVar('publication_budget', default=None)
ACTIVE_SESSION = ContextVar('publication_session', default=None)
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
    if (not isinstance(version, dict) or not isinstance(version.get('format'), str) or version.get('format') not in {'text', 'json', 'toml'}
            or set(version) != ({'path', 'format', 'key'} if version.get('format') in {'json', 'toml'} else {'path', 'format'})
            or not relative_path(version.get('path'))):
        errors.append('publication version source must be an explicit text/JSON/TOML path')
    elif version['format'] in {'json', 'toml'} and (not isinstance(version['key'], str) or not version['key']
                                        or any(not key for key in version['key'].split('.'))):
        errors.append('publication structured version key is invalid')
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
    result = {**identity, 'publication': deepcopy(value)}
    from .deployment import profile as deployment_profile
    deployment = deployment_profile(control, task)
    if deployment is not None: result['deployment'] = deployment
    return result


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
            or set(value) - required - {'commit', 'tag_object', 'content_digest', 'release_evidence', 'deployment', 'candidate'}
            or value.get('schema') != SCHEMA or not isinstance(value.get('phase'), str) or value.get('phase') not in {'preparing', 'prepared', 'publishing', 'deploying', 'finishing', 'published'}
            or not isinstance(value.get('preparation'), list) or not isinstance(value.get('effects'), dict)
            or not isinstance(value.get('observations'), list)):
        raise PublicationError('Invalid publication checkpoint')
    for name in ('task_id', 'project', 'owner', 'run_id', 'control_repo', 'remote_url', 'version', 'tag', 'reservation', 'notes'):
        if not isinstance(value[name], str) or not value[name]: raise PublicationError('Invalid publication ' + name)
    for name in ('base_commit', 'remote_base', 'commit', 'tag_object'):
        if name in value and not re.fullmatch('[0-9a-f]{40}', str(value[name])): raise PublicationError('Invalid publication Git identity')
    expected_authority = {'ship_policy': 'push', 'allow_push': True, 'actor': value['owner']}
    deployment = value['profile'].get('deployment') if isinstance(value['profile'], dict) else None
    if isinstance(deployment, dict) and deployment.get('mode') == 'required':
        expected_authority['deployment'] = {'allowed': True, 'target': deployment.get('target')}
    if value['authority'] != expected_authority:
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
    if 'candidate' in value:
        candidate=value['candidate']
        if (not isinstance(candidate,dict) or set(candidate)!={'schema','revision','digest','history'}
                or candidate.get('schema')!='go-workflow.release-candidate.v1'
                or type(candidate.get('revision')) is not int or candidate['revision']<1
                or not re.fullmatch('[0-9a-f]{64}',str(candidate.get('digest')))
                or not isinstance(candidate.get('history'),list) or len(candidate['history'])!=candidate['revision']
                or any(not isinstance(item,dict) or set(item)!={'reason','digest'}
                       or not isinstance(item.get('reason'),str) or not item['reason'].strip()
                       or not re.fullmatch('[0-9a-f]{64}',str(item.get('digest'))) for item in candidate['history'])
                or candidate['history'][-1]['digest']!=candidate['digest']):
            raise PublicationError('Invalid explicit release candidate revision')
    if 'deployment' in value:
        from .deployment import validate_state as validate_deployment
        validate_deployment(value['deployment'])
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
        token = ACTIVE_SESSION.set(session)
        try: yield record, session
        finally:
            ACTIVE_SESSION.reset(token)
            if not group_alive(session.load().get('worker_group')):
                session.update(phase='complete', worker_group=None, inflight=None)


def _write_command(session, cwd, argv, *, check=True):
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
    if check and result['returncode']:
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


def _toml_version_change(text, spec, replacement):
    try:
        value = tomllib.loads(text)
        keys = spec['key'].split('.')
        if keys == ['project', 'version'] and 'version' in value.get('project', {}).get('dynamic', []):
            raise PublicationError('Dynamic project.version cannot be published as a literal')
        parent = value
        for key in keys[:-1]: parent = parent[key]
        current = parent[keys[-1]]
        if not isinstance(current, str) or not VERSION.fullmatch(current):
            raise PublicationError('TOML version must be a literal semantic X.Y.Z')
        if replacement is None: return current
        expected = deepcopy(value); target = expected
        for key in keys[:-1]: target = target[key]
        target[keys[-1]] = replacement
        candidates = []
        # Select by parsed meaning, not the first matching name/value. This also
        # handles dotted keys, inline tables, comments and repeated other values.
        pattern = r"(['\"])" + re.escape(current) + r"\1"
        for match in re.finditer(pattern, text):
            candidate = text[:match.start()+1] + replacement + text[match.end()-1:]
            try:
                if tomllib.loads(candidate) == expected: candidates.append(candidate)
            except tomllib.TOMLDecodeError: continue
        if len(candidates) != 1:
            raise PublicationError('TOML version must have one unambiguous unescaped literal')
        return candidates[0]
    except (KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError) as exc:
        raise PublicationError('Version TOML source/key is invalid') from exc


def _version_change(text, spec, replacement=None):
    if spec['format'] == 'toml': return _toml_version_change(text, spec, replacement)
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

def candidate_doctor_gate():
    """Pretag-only doctor proof: accept exactly the missing tag, never a broken prerequisite."""
    command = ['python3', 'cli/go.py', 'doctor', '.', '--platform', 'wsl', '--agent', 'hermes', '--json']
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    try:
        report = json.loads(result.stdout)
        stack = report['stack']
        valid = (result.returncode == 1 and report['ready'] is False
                 and report['contract'] == {'valid': True, 'errors': []}
                 and report['agent']['compatible'] is True
                 and len(report['prerequisites']) == 5
                 and {item['name'] for item in report['prerequisites']} == {'python', 'git', 'bash', 'make', 'uv'}
                 and all(item['available'] is True for item in report['prerequisites'])
                 and stack['version'] == stack['required_version'] == '0.3.47'
                 and stack['ref'] == stack['required_ref'] == stack['provenance_requested_ref'] == 'v0.3.47'
                 and stack['identity_source'] == 'git-checkout' and stack['git_head']
                 and stack['pinned_commit'] is None and stack['exact_ref'] is False
                 and stack['compatible'] is False and stack['development_override'] is False
                 and report['actions'] == ['checkout the pinned go-workflow-stack ref v0.3.47'])
    except (KeyError, TypeError, ValueError):
        valid = False
    print(result.stdout, end='')
    if not valid:
        raise PublicationError('Candidate doctor failed beyond expected missing pretag v0.3.47: '
                               + result.stderr[-2000:])
    print('Candidate doctor: only the absent immutable pretag blocks ready; post-tag exact_ref=true is still required')


def _safe_verification_correction(control, task_id, before, after):
    """Explicit v0.3.47 gate-preserving invocation mappings; no arbitrary commands."""
    if task_id != 'release-safe-active-task-handoff-v0347' or not isinstance(before, list) or len(before) != 7:
        return False
    expected = before.copy()
    if (before[0] != 'PYTHONPATH=. uv run --no-project --with "pytest>=8,<9" --with "jsonschema>=4.23" pytest -q'
            or before[5] != 'bash scripts/release-check.sh --allow-candidate'):
        return False
    expected[0] = 'env -u PYTHONPATH uv run --no-project --with "pytest>=8,<9" --with "jsonschema>=4.23" python -m pytest -q'
    expected[5] = ('GO_PROJECT_TEMPLATE=' + str(control.parent / 'go-project-template')
                   + ' ./scripts/release-check.sh --allow-candidate')
    return after == expected


def _safe_second_verification_correction(control, task_id, before, after):
    """After reconciliation, only missing-tag semantics and isolated local origin may change."""
    if task_id != 'release-safe-active-task-handoff-v0347' or not isinstance(before, list) or len(before) != 7:
        return False
    if (before[0] != 'env -u PYTHONPATH uv run --no-project --with "pytest>=8,<9" --with "jsonschema>=4.23" python -m pytest -q'
            or before[4] != 'python3 cli/go.py doctor . --platform wsl --agent hermes --json'
            or before[5] != 'GO_PROJECT_TEMPLATE=' + str(control.parent / 'go-project-template')
                               + ' ./scripts/release-check.sh --allow-candidate'):
        return False
    final = before.copy()
    final[4] = "python3 -c 'from go_workflow.release import candidate_doctor_gate; candidate_doctor_gate()'"
    final[5] += ' --allow-local-origin'
    return after == final

def rebind_prepared_verification(control, task_id, owner, run_id):
    """Rebind a reserved, effectless candidate to a verification-only task correction.

    The prior task is read from the frozen base, never reconstructed from a
    caller-supplied digest.  Publication cannot resume until fresh proof for
    the corrected contract has been captured.
    """
    control = Path(control).resolve()
    with repository_lock(control / '.go', 'workspace-integration'):
        with repository_lock(control / '.go', 'workspace-execution-' + task_id):
            task = active_task(control, task_id, owner)
            record = owned_record(control, task_id, owner, run_id, active=False)
            require_run_idle(record)
            verify_workspace(record)
            checked_scope(record)
            state = validate_state(read_object(state_path(control, task_id)))
            if (state['phase'] != 'prepared' or state['effects'] or state['observations']
                    or 'content_digest' in state or 'release_evidence' in state):
                raise PublicationError('Only effectless, unverified prepared releases can rebind verification')
            if (state['task_id'] != task_id or state['owner'] != owner or state['run_id'] != run_id
                    or state['control_repo'] != str(control) or state['project'] != task['project']
                    or state['profile'] != publication_profile(control, task)):
                raise PublicationError('Frozen publication identity differs from the current task')
            if any(record[key] != state['workspace'].get(key) for key in (
                    'control_repo', 'path', 'owner', 'run_id', 'branch', 'base_branch',
                    'base_commit', 'common_dir', 'repository_id', 'generation')):
                raise PublicationError('Frozen publication workspace identity changed')
            reservation = (control / state['reservation']).resolve()
            if reservation.parent != (control / '.go/runs/publication-reservations').resolve() or not reservation.exists():
                raise PublicationError('Publication reservation missing or outside canonical state')
            if read_object(reservation) != {'task_id': task_id, 'run_id': run_id, 'owner': owner,
                                            'version': state['version'], 'status': 'reserved'}:
                raise PublicationError('Publication reservation changed')
            if task.get('completion_evidence'):
                raise PublicationError('Attached verification/critic proof must be explicitly invalidated')
            old_path = '.go/tasks/active/' + task_id + '.json'
            try:
                previous = json.loads(git_text(control, 'show', state['base_commit'] + ':' + old_path))
            except (ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
                raise PublicationError('Frozen task contract unavailable at release base') from exc
            corrected = deepcopy(previous)
            corrected['verification'] = task.get('verification')
            if (corrected != task or not isinstance(task.get('verification'), list)
                    or not task['verification']):
                raise PublicationError('Only verification commands may change on the frozen task')
            if not (_safe_verification_correction(control, task_id, previous.get('verification'), task['verification'])
                    or _safe_second_verification_correction(control, task_id, previous.get('verification'), task['verification'])):
                raise PublicationError('Verification correction is not an approved gate-preserving mapping')
            if state['contract_digest'] == contract_digest(task):
                if task['verification'] == previous.get('verification'):
                    raise PublicationError('No verification correction is recorded')
                return deepcopy(state)
            if (contract_digest(previous) != state['contract_digest']
                    or task['verification'] == previous.get('verification')):
                raise PublicationError('Frozen base task does not match the reserved contract')
            state['contract_digest'] = contract_digest(task)
            _save(control, state)
            return deepcopy(state)


def prepare_release(control, task_id, owner, run_id, *, ship_policy='none', allow_push=False, allow_deploy=False, freeze_candidate=False):
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
            from .deployment import authorize
            authorize(profile.get('deployment'), allow_deploy)
            if ship_policy != 'push' or allow_push is not True: raise PublicationError('Explicit push authorization is required')
            url, refs = _remote(control, profile)
            remote_base = refs.get('refs/heads/' + profile['branch'])
            if remote_base != record['base_commit'] or git_text(control, 'rev-parse', 'HEAD') != record['base_commit']:
                raise PublicationError('Base/remote advanced; preserve workspace and reconcile before reserving a version')
            spec = profile['publication']; version_path = _file(worker, task, spec['version']['path'])
            before = version_path.read_bytes().decode('utf-8'); current = _version_change(before, spec['version'])
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
            if (profile.get('deployment') or {}).get('mode') == 'required':
                state['authority']['deployment'] = {'allowed': True, 'target': profile['deployment']['target']}
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
                target = _file(worker, task, change['path']); current = target.read_bytes().decode('utf-8') if target.exists() else ''
                if current == change['after']: continue
                if _hash(current) != change['before']: raise PublicationError('Preparation file changed; preserve it and reconcile')
                atomic_write_text(target, change['after'])
            checked_scope(record); state['phase'] = 'prepared'; _save(control, state)
        if freeze_candidate and 'candidate' not in state and not state['effects']:
            digest=content_snapshot(worker,task)['digest']
            state['candidate']={'schema':'go-workflow.release-candidate.v1','revision':1,'digest':digest,
                                'history':[{'reason':'Initial prepared candidate','digest':digest}]}
            _save(control,state)
        return deepcopy(state)



def revise_prepared_candidate(control,task_id,owner,run_id,*,reason):
    """Explicitly admit repaired content before re-verification, never after effects."""
    if not isinstance(reason,str) or not reason.strip():
        raise PublicationError('Candidate revision requires a concrete repair reason')
    control=Path(control).resolve()
    with publication_slot(control,task_id,owner,run_id) as (record,_):
        task=active_task(control,task_id,owner)
        state=_load(control,task,owner,run_id)
        if state['phase']!='prepared' or state['effects'] or 'candidate' not in state:
            raise PublicationError('Only an effectless frozen candidate can be revised')
        worker=Path(record['path']);verify_workspace(record);checked_scope(record)
        digest=content_snapshot(worker,task)['digest']
        candidate=state['candidate']
        if digest!=candidate['digest']:
            candidate['revision']+=1
            candidate['digest']=digest
            candidate['history'].append({'reason':reason,'digest':digest})
            _save(control,state)
        return deepcopy(candidate)

def reconcile_prepared_release(control, task_id, owner, run_id):
    """Advance an effectless dirty candidate to a linear base; invalidate its proof.

    This is deliberately narrower than workspace reconciliation: only a
    fast-forward with no overlapping paths and no publication effect is safe.
    The integration lock excludes other workflow writers across the transition.
    An interrupted state write can be rolled forward on the next invocation.
    """
    from datetime import datetime, timezone

    def candidate_content(worker, path):
        target = worker / path
        if target.is_symlink():
            payload, kind = os.readlink(os.fsencode(target)), 'symlink'
        elif target.is_file():
            payload, kind = target.read_bytes(), 'file'
        elif not target.exists():
            payload, kind = b'', 'deleted'
        else:
            raise PublicationError('Candidate has unsupported file type: ' + path)
        return (kind, stat.S_IMODE(target.lstat().st_mode) if kind != 'deleted' else None, payload)

    def candidate_fingerprint(worker, scope_record):
        paths = checked_scope(scope_record)
        entries = []
        for path in paths:
            kind, mode, payload = candidate_content(worker, path)
            entries.append({'path': path, 'kind': kind, 'mode': mode,
                            'sha256': hashlib.sha256(payload).hexdigest()})
        payload = {'entries': entries, 'diff_sha256': hashlib.sha256(
            git(worker, 'diff', '--binary', 'HEAD', '--').stdout.encode()).hexdigest()}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    control = Path(control).resolve()
    with repository_lock(control / '.go', 'workspace-integration'):
        with repository_lock(control / '.go', 'workspace-execution-' + task_id):
            record = verify_workspace(owned_record(control, task_id, owner, run_id))
            require_run_idle(record)
            task = active_task(control, task_id, owner)
            state = validate_state(read_object(state_path(control, task_id)))
            if (state['task_id'] != task_id or state['control_repo'] != str(control)
                    or state['owner'] != owner or state['run_id'] != run_id
                    or state['contract_digest'] != contract_digest(task)
                    or state['profile'] != publication_profile(control, task)
                    or state['phase'] != 'prepared' or state['effects'] or state['observations']):
                raise PublicationError('Only the owned effectless prepared release may reconcile')
            reservation = (control / state['reservation']).resolve()
            if (reservation.parent != (control / '.go/runs/publication-reservations').resolve()
                    or not reservation.is_file()
                    or read_object(reservation) != {'task_id': task_id, 'owner': owner, 'run_id': run_id,
                                                      'version': state['version'], 'status': 'reserved'}):
                raise PublicationError('Publication reservation is missing or changed')
            worker = Path(record['path'])
            if record['state'] != 'ready' or state['tag'] != state['profile']['publication']['tag_prefix'] + state['version']:
                raise PublicationError('Release workspace/version is not ready')
            if git_text(control, 'symbolic-ref', '--short', 'HEAD') != record['base_branch']:
                raise PublicationError('Control branch changed')
            new_base = git_text(control, 'rev-parse', 'HEAD')
            url, refs = _remote(control, state['profile'])
            if (url != state['remote_url'] or refs.get('refs/heads/' + record['base_branch']) != new_base
                    or 'refs/tags/' + state['tag'] in refs):
                raise PublicationError('Remote base/tag changed')
            old_base = state['base_commit']
            journal_path = control / '.go/runs' / task_id / 'reconciliation-intent.json'
            journal = read_object(journal_path) if journal_path.exists() else None
            if journal and set(journal) != {'old_base', 'new_base', 'candidate_sha256'}:
                raise PublicationError('Foreign release reconciliation intent')
            if journal and journal['new_base'] != new_base:
                history = record.get('reconciliations') or []
                if (journal['new_base'] == old_base and state['workspace'] == record
                        and state['base_commit'] == old_base and history
                        and history[-1]['old_base_commit'] == journal['old_base']
                        and history[-1]['new_base_commit'] == old_base
                        and git_text(worker, 'rev-parse', 'HEAD') == old_base):
                    journal = None  # The preceding reconciliation completed.
                else:
                    raise PublicationError('Foreign or stale release reconciliation intent')
            if state['workspace'] != record:
                # Crash between registry and release-state writes: only roll
                # forward the exact recorded reconciliation, never guess.
                history = record.get('reconciliations') or []
                old_record = deepcopy(record)
                old_record['base_commit'] = old_base
                old_record['reconciliations'] = history[:-1]
                if not old_record['reconciliations']:
                    old_record.pop('reconciliations')
                if (not history or history[-1]['old_base_commit'] != old_base
                        or history[-1]['new_base_commit'] != new_base
                        or history[-1]['workspace_head_before'] != old_base
                        or history[-1]['workspace_head_after'] != new_base
                        or old_record != state['workspace']
                        or git_text(worker, 'rev-parse', 'HEAD') != new_base):
                    raise PublicationError('Frozen workspace differs from registry')
                advancing = set(git(control, 'diff', '--name-only', '-z', old_base, new_base).stdout.split('\0'))
                if advancing.intersection(checked_scope(record)):
                    raise PublicationError('Base/candidate overlap during recovery')
                for change in state['preparation']:
                    target = _file(worker, task, change['path'])
                    if not target.exists() or target.read_text() != change['after']:
                        raise PublicationError('Frozen preparation bytes changed during recovery')
                if (not journal or journal['old_base'] != old_base
                        or candidate_fingerprint(worker, record) != journal['candidate_sha256']):
                    raise PublicationError('Candidate fingerprint changed during recovery')
                state.update(workspace=record, base_commit=new_base, remote_base=new_base)
                state.pop('content_digest', None)
                state.pop('release_evidence', None)
                _save(control, state)
                return deepcopy(state)
            if old_base != record['base_commit'] or state['remote_base'] != old_base:
                raise PublicationError('Frozen base differs from workspace')
            if new_base == old_base:
                if journal and journal['new_base'] == new_base:
                    history = record.get('reconciliations') or []
                    if (not history or history[-1]['old_base_commit'] != journal['old_base']
                            or history[-1]['new_base_commit'] != new_base
                            or candidate_fingerprint(worker, record) != journal['candidate_sha256']):
                        raise PublicationError('Candidate fingerprint changed after completed reconciliation')
                for change in state['preparation']:
                    target = _file(worker, task, change['path'])
                    if not target.exists() or target.read_text() != change['after']:
                        raise PublicationError('Frozen preparation bytes changed')
                return deepcopy(state)
            worker_head = git_text(worker, 'rev-parse', 'HEAD')
            if (git(control, 'merge-base', '--is-ancestor', old_base, new_base, check=False).returncode
                    or worker_head not in {old_base, new_base}):
                raise PublicationError('Non-linear base or candidate commits require explicit review')
            for change in state['preparation']:
                target = _file(worker, task, change['path'])
                if not target.exists() or target.read_text() != change['after']:
                    raise PublicationError('Frozen preparation bytes changed')
                source = git(control, 'show', old_base + ':' + change['path'], check=False)
                if source.returncode or _hash(source.stdout) != change['before']:
                    raise PublicationError('Frozen preparation source changed')
            if git(worker, 'diff', '--cached', '--quiet', check=False).returncode:
                raise PublicationError('Staged candidate changes require explicit review')
            scope_record = {**record, 'base_commit': new_base} if worker_head == new_base else record
            paths = checked_scope(scope_record)
            advancing = set(git(control, 'diff', '--name-only', '-z', old_base, new_base).stdout.split('\0'))
            if advancing.intersection(paths):
                raise PublicationError('Base/candidate overlap: ' + ', '.join(sorted(advancing.intersection(paths))))
            fingerprint = candidate_fingerprint(worker, scope_record)
            if journal:
                if journal != {'old_base': old_base, 'new_base': new_base, 'candidate_sha256': fingerprint}:
                    raise PublicationError('Candidate fingerprint changed after reconciliation intent')
            elif worker_head == new_base:
                raise PublicationError('Worker advanced without durable candidate intent')
            else:
                atomic_json(journal_path, {'old_base': old_base, 'new_base': new_base,
                                           'candidate_sha256': fingerprint})
            original = {path: candidate_content(worker, path) for path in paths}
            original_diff = git(worker, 'diff', '--binary', 'HEAD', '--').stdout
            if worker_head == old_base:
                result = git(worker, '-c', 'core.hooksPath=/dev/null', 'merge', '--ff-only', new_base, check=False)
                if result.returncode:
                    raise PublicationError('Candidate fast-forward conflict; preserve worker: ' + result.stderr.strip())
            if (git_text(worker, 'rev-parse', 'HEAD') != new_base
                    or any(candidate_content(worker, path) != content for path, content in original.items())
                    or git(worker, 'diff', '--binary', 'HEAD', '--').stdout != original_diff
                    or candidate_fingerprint(worker, {**record, 'base_commit': new_base}) != fingerprint):
                raise PublicationError('Candidate bytes changed during reconciliation; preserve worker for review')
            proposed = deepcopy(record)
            proposed['base_commit'] = new_base
            proposed.setdefault('reconciliations', []).append({
                'old_base_commit': old_base, 'new_base_commit': new_base,
                'workspace_head_before': old_base, 'workspace_head_after': new_base,
                'reconciled_at': datetime.now(timezone.utc).isoformat(),
                'evidence_invalidated': True,
            })
            validate_record(proposed)
            checked_scope(proposed)
            # Registry first: a crash leaves enough history to finish the
            # frozen-state write through the guarded roll-forward above.
            atomic_json(registry_path(control, task_id), proposed)
            state.update(workspace=proposed, base_commit=new_base, remote_base=new_base)
            state.pop('content_digest', None)
            state.pop('release_evidence', None)
            _save(control, state)
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
    from .shipping import capture_release, verify_release_evidence, observe_release
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
        if state['phase'] not in {'deploying', 'finishing'}:
            if 'candidate' in state and content_snapshot(worker,task)['digest']!=state['candidate']['digest']:
                raise PublicationError('Candidate content changed; explicitly revise before final verification')
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
            state['phase'] = 'deploying'; _save(control, state)
        if state['phase'] == 'deploying':
            observed = observe_release(control, task, state['tag'])
            if (observed['commit'] != state['commit'] or observed['tag_object'] != state['tag_object']
                    or observed['remote_url'] != state['remote_url']):
                raise PublicationError('Published release identity changed before deployment/readback')
            from .deployment import run_deployment
            run_deployment(control, task, state, session)
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


def taskwise_no_release_policy(control, task, args):
    """Freeze explicit taskwise delivery authority without inventing a release."""
    if not getattr(args, 'taskwise_delivery', False):
        return None
    if ((task.get('execution_contract') or {}).get('release') or {}).get('mode') != 'none':
        return None
    policy = getattr(args, 'ship_policy', 'none')
    if policy == 'local-commit':
        policy = 'commit'
    if policy not in {'none', 'commit', 'push'}:
        raise PublicationError('Invalid taskwise shipping policy')
    if policy == 'push' and not getattr(args, 'allow_push', False):
        raise PublicationError('Taskwise push requires explicit frozen push authorization')
    binding = {'schema': 'go-workflow.taskwise-delivery.v1', 'ship_policy': 'local-commit' if policy == 'commit' else policy,
               'branch': args.base_branch, 'remote': None, 'remote_url': None, 'remote_base': None}
    if policy == 'push':
        url, refs = _remote(control, {'provider': 'git-tag', 'remote': 'origin'})
        binding.update(remote='origin', remote_url=url, remote_base=refs.get('refs/heads/' + args.base_branch))
        if binding['remote_base'] is None:
            raise PublicationError('Configured origin lacks taskwise base branch; configure it before execution')
    return binding


def complete_taskwise_no_release(control, task_id, owner, run_id, session):
    """Commit/integrate/finish a verified release:none task; closure sync follows."""
    from . import cli as api
    from argparse import Namespace
    state = session.load()
    policy = state['publication']['taskwise_delivery']
    if policy['ship_policy'] == 'none':
        raise PublicationError('Taskwise delivery needs a product commit; explicit ship-policy none forbids it')
    with publication_slot(control, task_id, owner, run_id) as (_, command_session):
        record = owned_record(control, task_id, owner, run_id, active=False)
        task = active_task(control, task_id)
        if task['execution_contract']['release']['mode'] != 'none':
            raise PublicationError('Profileless completion is only valid for explicit release:none')
        if any(outcome.get('status') in {'blocked', 'rejected'} for outcome in task.get('requested_outcomes', [])):
            raise PublicationError('Explicitly blocked/rejected outcome requires resolution')
        worker = Path(record['path'])
        verify_workspace(record)
        if completion_findings(worker, task, phase_only=True):
            raise PublicationError('Taskwise completion requires current verification and critic proof: '
                                   + '; '.join(completion_findings(worker, task, phase_only=True)))
        if api.architecture_finish_findings(control / '.go', task):
            raise PublicationError('Architecture proof is incomplete before taskwise completion')

        def effect(name, expected=None, observed=None):
            effects = deepcopy(session.load()['effects'])
            if name not in effects:
                if expected is None:
                    raise PublicationError('Missing taskwise effect intent')
                effects[name] = {'status': 'pending', 'expected': expected}
            elif expected is not None and effects[name]['expected'] != expected:
                raise PublicationError('Taskwise effect intent changed: ' + name)
            if observed is not None:
                if effects[name]['expected'] != observed:
                    raise PublicationError('Taskwise effect readback differs: ' + name)
                effects[name]['status'] = 'confirmed'
            session.update(effects=effects)
            return effects[name]['expected']

        effects = session.load()['effects']
        if 'taskwise_commit' not in effects:
            stage_workspace(control, task_id, owner, run_id,
                            command_runner=lambda cwd, argv: _write_command(command_session, cwd, argv))
            effect('taskwise_commit', {'parent': git_text(worker, 'rev-parse', 'HEAD'),
                                      'tree': git_text(worker, 'write-tree'),
                                      'message': f'go deliver: {task_id} {run_id}'})
        expected = effect('taskwise_commit')
        head = git_text(worker, 'rev-parse', 'HEAD')
        if head == expected['parent']:
            _write_command(command_session, worker, ['git', 'commit', '--allow-empty', '-m', expected['message']])
            head = git_text(worker, 'rev-parse', 'HEAD')
        effect('taskwise_commit', observed={'parent': git_text(worker, 'rev-parse', head + '^'),
                                           'tree': git_text(worker, 'rev-parse', head + '^{tree}'),
                                           'message': git_text(worker, 'show', '-s', '--format=%B', head)})
        require_clean(record)
        if git_text(control, 'symbolic-ref', '--short', 'HEAD') != policy['branch']:
            raise PublicationError('Taskwise control branch changed')
        effect('taskwise_integration', head)
        base = git_text(control, 'rev-parse', 'HEAD')
        if base != head:
            if base != record['base_commit']:
                raise PublicationError('Taskwise base advanced; preserve and reconcile candidate')
            if content_snapshot(control, task)['digest'] != content_snapshot(control, task, base)['digest']:
                raise PublicationError('Control product content is dirty; preserve unrelated work')
            _write_command(command_session, control, ['git', 'merge', '--ff-only', head])
        effect('taskwise_integration', observed=git_text(control, 'rev-parse', 'HEAD'))
        with repository_lock(control / '.go', 'workspace-execution-' + task_id):
            record.update(state='integrated', integration={'workspace_head': head, 'integrated_commit': head})
            atomic_json(registry_path(control, task_id), record)
        if policy['ship_policy'] == 'push':
            remote_profile = {'provider': 'git-tag', 'remote': policy['remote']}
            url, refs = _remote(control, remote_profile)
            if url != policy['remote_url']:
                raise PublicationError('Taskwise publication remote changed')
            branch = 'refs/heads/' + policy['branch']
            effect('taskwise_push', {branch: head})
            if refs.get(branch) != head:
                if refs.get(branch) != policy['remote_base']:
                    raise PublicationError('Remote taskwise branch advanced; preserve pending delivery')
                _write_command(command_session, control, ['git', 'push', policy['remote'], head + ':' + branch])
                url, refs = _remote(control, remote_profile)
                if url != policy['remote_url']:
                    raise PublicationError('Taskwise publication remote changed during push')
            effect('taskwise_push', observed={branch: refs.get(branch)})
        task = active_task(control, task_id)
        if task['status'] == 'active':
            ref = task['completion_evidence']['verification']['path']
            for outcome in task.get('requested_outcomes', []):
                if outcome['status'] in {'blocked', 'rejected'}:
                    raise PublicationError('Explicitly blocked/rejected outcome requires resolution')
                with redirect_stdout(io.StringIO()):
                    api.cmd_task_outcome(Namespace(repo=str(control), task_id=task_id, outcome=outcome['id'],
                        status='verified', evidence=ref, agent=owner))
            with redirect_stdout(io.StringIO()):
                api.cmd_finish(Namespace(repo=str(control), task_id=task_id, agent=owner,
                    evidence='changed_files=' + ','.join(checked_scope(record)) +
                    '; verification_command=declared task commands; verification_result=passed; critic=passed recorded review; '
                    'runtime=managed taskwise; model=requested ' + task['execution_contract']['model']['id'] +
                    ' (effective identity unconfirmed); billing_mode=unknown; release=not applicable; commit=' + head))
        with redirect_stdout(io.StringIO()):
            api.cmd_task_review(api.build_parser().parse_args(['task', 'review', str(control), '--task-id', task_id,
                '--status', 'approved', '--agent', owner, '--evidence',
                'Current content-bound verification and critic passed; configured no-release policy retained.']))
        return {'status': 'delivered', 'commit': head, 'tag': None, 'deployment': 'not_applicable'}
