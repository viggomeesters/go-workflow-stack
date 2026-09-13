"""Explicit command adapters and content-bound live deployment evidence.

Adapters are trusted infrastructure: observations must come from the target,
not echo the supplied expectation. External effects require stable idempotency.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .completion import content_snapshot, contract_digest, read_artifact, save_artifact, verification_session
from .execution_context import json_hash
from .release import PublicationError
from .worktrees import active_task, git_text, read_object, workflow_root

SCHEMA = 'go-workflow.deployment-evidence.v1'
STATE = 'go-workflow.deployment-state.v1'
OBSERVATION = 'go-workflow.deployment-observation.v1'


class DeploymentError(PublicationError):
    pass


def _argv(value):
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item and '\x00' not in item for item in value)


def validate_profile(value):
    if not isinstance(value, dict): return ['deployment must be an explicit object']
    if value.get('mode') == 'none':
        return [] if set(value) == {'mode', 'reason'} and isinstance(value['reason'], str) and value['reason'].strip() else ['no-deployment policy requires a reason']
    required = {'mode', 'target', 'recovery_policy', 'required_env', 'deploy', 'observe'}
    if value.get('mode') != 'required' or not required.issubset(value) or set(value) - required - {'package'}:
        return ['required deployment profile has missing or unknown fields']
    errors = []
    if not isinstance(value['target'], str) or not value['target'].strip(): errors.append('deployment target is required')
    if value['recovery_policy'] != 'resume_only': errors.append('deployment recovery must be explicit resume_only; no inferred rollback')
    env = value['required_env']
    if not isinstance(env, list) or not all(isinstance(name, str) and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name) for name in env):
        errors.append('required_env must name environment variables, not their values')
    deploy, observe = value['deploy'], value['observe']
    if (not isinstance(deploy, dict) or set(deploy) != {'argv', 'idempotency'} or not _argv(deploy.get('argv'))
            or deploy.get('idempotency') != 'required' or not any('{idempotency_key}' in arg for arg in deploy['argv'])):
        errors.append('deploy requires argv with an idempotency key and an explicit idempotency contract')
    if (not isinstance(observe, dict) or set(observe) != {'argv', 'read_only'} or not _argv(observe.get('argv'))
            or observe.get('read_only') is not True or not any('{idempotency_key}' in arg for arg in observe['argv'])):
        errors.append('observe requires read-only argv querying the operation identity')
    package = value.get('package')
    if 'package' in value and (not isinstance(package, dict) or set(package) != {'argv', 'filename'}
            or not _argv(package.get('argv')) or not isinstance(package.get('filename'), str)
            or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]*', package['filename'])
            or not any('{output}' in arg for arg in package['argv'])):
        errors.append('package requires argv with an output path and a plain artifact filename')
    return errors


def require_profile(value):
    errors = validate_profile(value)
    if errors: raise DeploymentError('; '.join(errors))
    return value


def profile(repo, task):
    project = read_object(workflow_root(repo) / 'project.json')
    name = task['execution_contract']['release'].get('profile')
    spec = project.get('release_profiles', {}).get(name, {}).get('deployment')
    return deepcopy(require_profile(spec)) if spec is not None else None


def required(spec): return isinstance(spec, dict) and spec.get('mode') == 'required'


def authorize(spec, allowed):
    if required(spec) and allowed is not True:
        raise DeploymentError('Explicit deployment authorization is required separately from Git push permission')
    if required(spec): credentials(spec)


def credentials(spec):
    missing = [name for name in spec['required_env'] if not os.environ.get(name)]
    if missing: raise DeploymentError('Required deployment credentials/environment are unavailable: ' + ', '.join(missing))


def _reference(value):
    return (isinstance(value, dict) and set(value) == {'path', 'sha256'}
            and isinstance(value['path'], str) and bool(value['path'])
            and isinstance(value['sha256'], str) and bool(re.fullmatch('[0-9a-f]{64}', value['sha256'])))


def _identity(value):
    from .release import VERSION
    return (isinstance(value, dict) and set(value) == {'target', 'commit', 'version', 'artifact_sha256', 'idempotency_key'}
            and isinstance(value['target'], str) and bool(value['target'].strip())
            and isinstance(value['commit'], str) and bool(re.fullmatch('[0-9a-f]{40}', value['commit']))
            and isinstance(value['version'], str) and bool(VERSION.fullmatch(value['version']))
            and (value['artifact_sha256'] is None or isinstance(value['artifact_sha256'], str)
                 and bool(re.fullmatch('[0-9a-f]{64}', value['artifact_sha256'])))
            and isinstance(value['idempotency_key'], str) and bool(re.fullmatch('[0-9a-f]{64}', value['idempotency_key'])))


def validate_state(value):
    needed = {'schema', 'profile', 'key', 'phase', 'commands', 'observations', 'artifact', 'package_attempts'}
    if (not isinstance(value, dict) or not needed.issubset(value) or set(value) - needed - {'effect', 'evidence'}
            or value.get('schema') != STATE or not isinstance(value.get('phase'), str)
            or value['phase'] not in {'pending', 'accepted', 'verified'} or not isinstance(value.get('key'), str)
            or not re.fullmatch('[0-9a-f]{64}', value['key'])
            or any(not isinstance(value.get(name), list) for name in ('commands', 'observations', 'package_attempts'))):
        raise DeploymentError('Invalid deployment checkpoint')
    require_profile(value['profile'])
    if not required(value['profile']): raise DeploymentError('Operational deployment checkpoint requires a target')
    if any(not _reference(item) for item in value['commands']) or ('evidence' in value and not _reference(value['evidence'])):
        raise DeploymentError('Invalid deployment evidence reference')
    for item in value['package_attempts']:
        if (not isinstance(item, dict) or set(item) != {'output', 'status'} or not isinstance(item['output'], str)
                or not item['output'] or item['status'] not in ('pending', 'confirmed')):
            raise DeploymentError('Invalid package attempt checkpoint')
    if 'effect' in value:
        effect = value['effect']
        if (not isinstance(effect, dict) or set(effect) != {'expected', 'status'}
                or effect['status'] not in ('pending', 'confirmed') or not _identity(effect['expected'])):
            raise DeploymentError('Invalid deployment effect checkpoint')
    for item in value['observations']:
        if not isinstance(item, dict) or set(item) != {'observation', 'raw'} or not _reference(item['raw']):
            raise DeploymentError('Invalid deployment observation checkpoint')
        observed = item['observation']
        expected = {key: observed.get(key) for key in ('target', 'commit', 'version', 'artifact_sha256', 'idempotency_key')} if isinstance(observed, dict) else {}
        if not _identity(expected): raise DeploymentError('Invalid deployment observation identity')
        observation({'returncode': 0, 'timed_out': False, 'stdout': json.dumps(observed)}, expected)
    artifact = value['artifact']
    if artifact is not None and (not isinstance(artifact, dict) or set(artifact) != {'path', 'sha256', 'size'}
            or not isinstance(artifact['path'], str) or not artifact['path'] or not isinstance(artifact['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', artifact['sha256']) or type(artifact['size']) is not int or artifact['size'] < 0):
        raise DeploymentError('Invalid package artifact identity')
    return value


def _key(state):
    return json_hash({key: state[key] for key in ('project', 'task_id', 'run_id', 'commit', 'version', 'contract_digest')}
                     | {'target': state['profile']['deployment']['target']})


def expectation(state):
    deployment = state['deployment']; artifact = deployment['artifact']
    return {'target': deployment['profile']['target'], 'commit': state['commit'], 'version': state['version'],
            'artifact_sha256': artifact['sha256'] if artifact else None, 'idempotency_key': deployment['key']}


def render(argv, expected, *, output='', artifact=''):
    values = {**expected, 'output': output, 'artifact': artifact}
    result = []
    for arg in argv:
        for key, value in values.items(): arg = arg.replace('{' + key + '}', str(value) if value is not None else '')
        result.append(arg)
    return result


def _unchanged(control, task, digest):
    if content_snapshot(control, task)['digest'] != digest:
        raise DeploymentError('Adapter changed verified product content; preserve state and reconcile')


def _command(control, task, session, argv, digest, phase):
    from .release import _write_command
    _unchanged(control, task, digest)
    previous = session.load()['phase']
    if phase == 'observe': session.update(phase='verify')
    try: result = _write_command(session, control, argv, check=False)
    finally:
        from .run_state import group_alive
        if phase == 'observe' and not group_alive(session.load().get('worker_group')):
            session.update(phase=previous)
    raw = save_artifact(control / '.go', task['id'], 'deployment-command', {
        'schema': 'go-workflow.deployment-command.v1', 'task_id': task['id'], 'project': task['project'],
        'contract_digest': contract_digest(task), 'content_digest': digest, 'phase': phase,
        'argv': argv, 'cwd': str(control), **result})
    _unchanged(control, task, digest)
    return result, raw


def observation(result, expected):
    if type(result.get('returncode')) is not int or result.get('returncode') != 0 or result.get('timed_out') is not False:
        raise DeploymentError('Deployment observation unavailable; not absence or live proof')
    try: value = json.loads(result['stdout'])
    except (TypeError, ValueError, KeyError) as exc: raise DeploymentError('Malformed deployment observation') from exc
    if (not isinstance(value, dict) or value.get('schema') != OBSERVATION
            or not isinstance(value.get('status'), str) or value['status'] not in {'absent', 'accepted', 'live', 'unknown'}
            or value.get('authorized') is not True or type(value.get('available')) is not bool
            or not set(expected).issubset(value) or any(value.get(key) != item for key, item in expected.items())):
        raise DeploymentError('Deployment observation does not match authorized target/operation/commit/version/artifact')
    if value['status'] == 'unknown': raise DeploymentError('Deployment observation is unknown; preserve the pending effect')
    if value['status'] in {'absent', 'accepted'} and value['available'] is not False:
        raise DeploymentError('Contradictory deployment availability observation')
    if value['status'] == 'live' and value.get('available') is not True:
        raise DeploymentError('Deployment version is not available at the target')
    return value


def _artifact(path, control, task_id):
    base = (control / git_text(control, 'rev-parse', '--git-common-dir') / 'go-workflow-artifacts' / task_id).resolve()
    if not path.resolve().is_relative_to(base) or path.is_symlink() or not path.is_file():
        raise DeploymentError('Package output is missing or outside controller-owned artifact storage')
    data = path.read_bytes()
    return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}


def run_deployment(control, task, state, session):
    from .release import _save
    spec = state['profile'].get('deployment')
    if spec is None or spec['mode'] == 'none': return
    authorize(spec, (state['authority'].get('deployment') or {}).get('allowed'))
    if 'deployment' not in state:
        state['deployment'] = {'schema': STATE, 'profile': deepcopy(spec), 'key': _key(state), 'phase': 'pending',
                               'commands': [], 'observations': [], 'artifact': None, 'package_attempts': []}
        _save(control, state)
    deployment = validate_state(state['deployment'])
    if deployment['profile'] != spec or deployment['key'] != _key(state): raise DeploymentError('Frozen deployment identity changed')
    if spec.get('package'):
        if deployment['artifact'] is None:
            # Packaging is local and may be replayed into a fresh file after an
            # interrupted result. Never adopt an unacknowledged partial artifact.
            base = control / git_text(control, 'rev-parse', '--git-common-dir') / 'go-workflow-artifacts' / task['id']
            output = base / hashlib.sha256(state['run_id'].encode()).hexdigest() / uuid.uuid4().hex / spec['package']['filename']
            output.parent.mkdir(parents=True, exist_ok=False)
            deployment['package_attempts'].append({'output': str(output), 'status': 'pending'}); _save(control, state)
            result, ref = _command(control, task, session, render(spec['package']['argv'], expectation(state), output=str(output)), state['content_digest'], 'package')
            deployment['commands'].append(ref); _save(control, state)
            if result['returncode'] != 0: raise DeploymentError('Packaging did not complete; retain its output and retry locally')
            deployment['artifact'] = _artifact(output, control, task['id'])
            deployment['package_attempts'][-1]['status'] = 'confirmed'; _save(control, state)
        if _artifact(Path(deployment['artifact']['path']), control, task['id']) != deployment['artifact']:
            raise DeploymentError('Confirmed package bytes changed; never substitute a different artifact')
    expected = expectation(state)
    def observe():
        result, ref = _command(control, task, session, render(spec['observe']['argv'], expected), state['content_digest'], 'observe')
        deployment['commands'].append(ref); _save(control, state)
        value = observation(result, expected)
        deployment['observations'].append({'observation': value, 'raw': ref}); _save(control, state)
        return value, ref
    value, ref = observe()
    if value['status'] == 'absent':
        old = deployment.get('effect')
        if old and old['expected'] != expected: raise DeploymentError('Deployment effect identity changed')
        deployment['effect'] = {'status': 'pending', 'expected': expected}; _save(control, state)
        argv = render(spec['deploy']['argv'], expected, artifact=deployment['artifact']['path'] if deployment['artifact'] else '')
        result, command_ref = _command(control, task, session, argv, state['content_digest'], 'deploy')
        deployment['commands'].append(command_ref); _save(control, state)
        if result['returncode'] != 0:
            raise DeploymentError('Deployment response is uncertain; resume authoritative readback with the same identity')
        value, ref = observe()
    if value['status'] != 'live':
        deployment['phase'] = 'accepted' if value['status'] == 'accepted' else 'pending'; _save(control, state)
        raise DeploymentError('Deployment accepted or pending, but the expected live version is not yet available')
    deployment['effect'] = {'status': 'confirmed', 'expected': expected}
    evidence = {'schema': SCHEMA, 'status': 'verified', 'task_id': task['id'], 'project': task['project'],
        'contract_digest': contract_digest(task), 'content_digest': state['content_digest'], 'run_id': state['run_id'],
        'profile': deepcopy(spec), **expected, 'observation': value, 'raw': ref,
        'artifact': deepcopy(deployment['artifact']), 'commands': list(deployment['commands'])}
    deployment.update(phase='verified', evidence=save_artifact(control / '.go', task['id'], 'deployment', evidence)); _save(control, state)


def release_reference(repo, task, commit, digest):
    spec = profile(repo, task)
    if not required(spec): return None
    from .release import state_path
    state = read_object(state_path(workflow_root(repo).parent, task['id']))
    ref = (state.get('deployment') or {}).get('evidence')
    if not ref: raise DeploymentError('Required deployment proof is missing; use the configured publisher')
    verify_completion(repo, task, {'commit': commit, 'content_digest': digest, 'deployment': ref}, current=True)
    return ref


def verify_completion(repo, task, release, *, current):
    root = workflow_root(repo); control = root.parent; ref = release.get('deployment')
    spec = profile(repo, task) if current else None
    if not ref:
        if required(spec): raise DeploymentError('Required deployment proof is missing')
        return
    evidence = read_artifact(root, ref)
    if (evidence.get('schema') != SCHEMA or evidence.get('status') != 'verified'
            or any(evidence.get(key) != value for key, value in {'task_id': task['id'], 'project': task['project'],
                'contract_digest': contract_digest(task), 'commit': release['commit'], 'content_digest': release['content_digest']}.items())):
        raise DeploymentError('Deployment proof belongs to another task/contract/released content')
    frozen = require_profile(evidence.get('profile'))
    if not required(frozen) or (current and frozen != spec): raise DeploymentError('Required deployment profile changed')
    from .release import _version_change, validate_publication, VERSION
    try:
        project = json.loads(git_text(repo, 'show', release['commit'] + ':.go/project.json'))
        frozen_release = project['release_profiles'][task['execution_contract']['release']['profile']]
        publication = frozen_release['publication']
        if validate_publication(publication): raise DeploymentError('Invalid released publication contract')
        version = _version_change(git_text(repo, 'show', release['commit'] + ':' + publication['version']['path']), publication['version'])
    except (KeyError, TypeError, ValueError) as exc:
        raise DeploymentError('Released deployment/version contract is unavailable or invalid') from exc
    if (frozen_release.get('deployment') != frozen or not isinstance(evidence.get('version'), str)
            or not VERSION.fullmatch(evidence['version']) or evidence['version'] != version
            or not isinstance(evidence.get('run_id'), str) or not evidence['run_id']
            or evidence.get('target') != frozen['target']):
        raise DeploymentError('Deployment identity differs from the released profile/version')
    expected_key = json_hash({key: evidence[key] for key in ('project', 'task_id', 'run_id', 'commit', 'version', 'contract_digest')}
                             | {'target': frozen['target']})
    if evidence.get('idempotency_key') != expected_key: raise DeploymentError('Deployment operation identity is invalid')
    artifact = evidence.get('artifact')
    if frozen.get('package'):
        if (not isinstance(artifact, dict) or set(artifact) != {'path', 'sha256', 'size'}
                or not isinstance(artifact['path'], str) or not isinstance(artifact['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', artifact['sha256']) or type(artifact['size']) is not int
                or artifact['size'] < 0 or evidence.get('artifact_sha256') != artifact['sha256']):
            raise DeploymentError('Deployment package identity is missing or inconsistent')
    elif artifact is not None or evidence.get('artifact_sha256') is not None:
        raise DeploymentError('Unconfigured package proof is not valid')
    expected = {key: evidence.get(key) for key in ('target', 'commit', 'version', 'artifact_sha256', 'idempotency_key')}
    commands = evidence.get('commands')
    if not isinstance(commands, list) or not commands: raise DeploymentError('Executed deployment command references are missing')
    executed = [read_artifact(root, item) for item in commands]
    if any(item.get('schema') != 'go-workflow.deployment-command.v1'
           or any(item.get(key) != evidence.get(key) for key in ('task_id', 'project', 'contract_digest', 'content_digest'))
           for item in executed): raise DeploymentError('Deployment command proof binding changed')
    if frozen.get('package') and not any(item.get('phase') == 'package'
            and item.get('argv') == render(frozen['package']['argv'], {**expected, 'artifact_sha256': None}, output=artifact['path'])
            and type(item.get('returncode')) is int and item['returncode'] == 0 and item.get('timed_out') is False
            for item in executed): raise DeploymentError('Package lacks matching executed producer proof')

    if evidence.get('raw') not in commands: raise DeploymentError('Live readback is not among the executed deployment commands')
    raw = read_artifact(root, evidence.get('raw'))
    if (raw.get('phase') != 'observe' or raw.get('argv') != render(frozen['observe']['argv'], expected)
            or any(raw.get(key) != evidence.get(key) for key in ('task_id', 'project', 'contract_digest', 'content_digest'))
            or observation(raw, expected) != evidence.get('observation') or evidence['observation']['status'] != 'live'):
        raise DeploymentError('Deployment proof lacks matching executed live readback')
    if current:
        credentials(frozen)
        from .release import ACTIVE_SESSION
        from .run_state import group_alive
        session = ACTIVE_SESSION.get(); separate = session is None
        if separate: session = verification_session(control, task)
        try:
            result, _ = _command(control, task, session, render(frozen['observe']['argv'], expected), evidence['content_digest'], 'observe')
            if observation(result, expected)['status'] != 'live': raise DeploymentError('Expected deployment is not currently live')
        finally:
            if separate and not group_alive(session.load().get('worker_group')):
                session.update(phase='complete', worker_group=None, inflight=None)
