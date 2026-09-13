"""Read-only release proof; publishing effects belong to a separate driver."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone

from .completion import CompletionError, attach, content_snapshot, contract_digest, save_artifact, proof_lease
from .state_io import repository_lock
from .worktrees import active_task, git, git_text, read_object, workflow_root


def validate_release_profiles(value):
    if not isinstance(value, dict): return ['release_profiles must be an object']
    errors = []
    for name, profile in value.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(profile, dict):
            errors.append('Invalid release profile name/object'); continue
        required = {'provider', 'remote', 'branch'} | ({'repository'} if profile.get('provider') == 'github-release' else set())
        if (not isinstance(profile.get('provider'), str) or profile.get('provider') not in {'git-tag', 'github-release'} or set(profile) - {'publication'} != required
                or not all(isinstance(item, str) and item.strip() for key, item in profile.items() if key != 'publication')):
            errors.append('Invalid/unsupported release profile: ' + name)
        if 'publication' in profile:
            from .release import validate_publication
            errors.extend(validate_publication(profile['publication']))
    return errors


def release_profile(repo, task):
    name = task['execution_contract']['release'].get('profile')
    profiles = read_object(workflow_root(repo) / 'project.json').get('release_profiles', {})
    profile = profiles.get(name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict) or profile.get('provider') not in {'git-tag', 'github-release'}:
        raise CompletionError('Required release profile has no supported verifier/publisher configuration')
    required = {'provider', 'remote', 'branch'} | ({'repository'} if profile['provider'] == 'github-release' else set())
    profile = {key: value for key, value in profile.items() if key != 'publication'}
    if set(profile) != required or not all(isinstance(value, str) and value for value in profile.values()):
        raise CompletionError('Release profile has missing or unknown fields')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', profile['remote']):
        raise CompletionError('Release remote must be an explicit configured remote name')
    git(repo, 'check-ref-format', 'refs/heads/' + profile['branch'])
    if profile['provider'] == 'github-release' and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', profile['repository']):
        raise CompletionError('Explicit GitHub owner/repository required')
    return {'name': name, **profile}


def observe_release(repo, task, tag):
    from .release import remaining_timeout
    profile = release_profile(repo, task)
    git(repo, 'check-ref-format', 'refs/tags/' + tag)
    url = git_text(repo, 'remote', 'get-url', profile['remote'])
    if profile['provider'] == 'github-release':
        expected = 'https://github.com/' + profile['repository']
        if url not in {expected, expected + '.git'}: raise CompletionError('GitHub profile and configured origin differ')
    elif not (url.startswith(('https://', 'ssh://', 'git@')) or Path(url).is_absolute()):
        raise CompletionError('Release readback refuses implicit/remote-helper URLs')
    # Read only advertised exact refs; prohibit executable Git remote helpers.
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE'): env.pop(key, None)
    try:
        result = subprocess.run(['git', '-C', str(repo), '-c', 'protocol.ext.allow=never', 'ls-remote', '--', profile['remote'],
                                 'refs/heads/' + profile['branch'], 'refs/tags/' + tag, 'refs/tags/' + tag + '^{}'],
                                env=env, text=True, capture_output=True, timeout=remaining_timeout(30))
    except subprocess.TimeoutExpired as exc: raise CompletionError('Remote release readback timed out') from exc
    if result.returncode: raise CompletionError('Remote release readback failed: ' + result.stderr[-1000:])
    refs = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch('[0-9a-f]{40}', parts[0]): raise CompletionError('Malformed remote release readback')
        if parts[1] in refs: raise CompletionError('Ambiguous remote release reference')
        refs[parts[1]] = parts[0]
    branch = refs.get('refs/heads/' + profile['branch'])
    tag_object = refs.get('refs/tags/' + tag)
    commit = refs.get('refs/tags/' + tag + '^{}')
    if not branch or not tag_object or not commit: raise CompletionError('Required remote branch/annotated tag is missing')
    if git_text(repo, 'cat-file', '-t', tag_object) != 'tag': raise CompletionError('Remote annotated tag object is unavailable locally')
    if git_text(repo, 'rev-parse', tag_object + '^{commit}') != commit:
        raise CompletionError('Remote annotated tag does not peel to its advertised commit')
    if branch != commit and git(repo, 'merge-base', '--is-ancestor', commit, branch, check=False).returncode:
        raise CompletionError('Remote branch does not verifiably preserve the released commit')
    publication = None
    if profile['provider'] == 'github-release':
        command = ['gh', 'release', 'view', tag, '--repo', profile['repository'], '--json',
                   'tagName,url,isDraft,isPrerelease,publishedAt']
        try: completed = subprocess.run(command, text=True, capture_output=True, timeout=remaining_timeout(30))
        except subprocess.TimeoutExpired as exc: raise CompletionError('GitHub release readback timed out') from exc
        if completed.returncode: raise CompletionError('GitHub release readback failed: ' + completed.stderr[-1000:])
        publication = json.loads(completed.stdout)
        if (publication.get('tagName') != tag or publication.get('isDraft') is not False
                or not publication.get('publishedAt') or not publication.get('url')):
            raise CompletionError('GitHub release publication is not confirmed')
    return {'profile': profile, 'remote_url': url, 'tag': tag, 'commit': commit, 'tag_object': tag_object,
            'branch_commit': branch, 'refs': refs, 'publication': publication,
            'observed_at': datetime.now(timezone.utc).isoformat()}


def capture_release(repo, task_id, owner, tag):
    root = workflow_root(repo)
    with repository_lock(root, 'completion-' + task_id), proof_lease(repo, task_id, owner):
        task = active_task(root.parent, task_id, owner)
        observed = observe_release(repo, task, tag)
        digest = content_snapshot(repo, task, observed['commit'])['digest']
        if digest != content_snapshot(repo, task)['digest']:
            raise CompletionError('Published commit differs from current product content')
        proof = {'schema': 'go-workflow.release-evidence.v1', 'status': 'verified', 'task_id': task_id,
                 'project': task['project'], 'contract_digest': contract_digest(task), 'content_digest': digest, **observed}
        ref = save_artifact(root, task_id, 'release', proof)
        attach(root, task_id, owner, 'release', ref)
        with repository_lock(root, 'task-' + task_id):
            task = active_task(root.parent, task_id, owner)
            task['release_receipt'] = {'schema': 'go-workflow.release-receipt.v1', 'status': 'verified', 'task_id': task_id,
                                       'project': task['project'], 'commit': observed['commit'], 'tag': tag, 'evidence': [ref['path']]}
            from .state_io import atomic_json
            atomic_json(root / 'tasks/active' / (task_id + '.json'), task)
        return proof, ref


def verify_release_evidence(repo, task, proof, digest, *, remote=True):
    if (proof.get('schema') != 'go-workflow.release-evidence.v1' or proof.get('status') != 'verified'
            or proof.get('task_id') != task['id'] or proof.get('project') != task['project']
            or proof.get('contract_digest') != contract_digest(task) or proof.get('content_digest') != digest
            or proof.get('profile') != release_profile(repo, task)):
        raise CompletionError('Required release evidence does not match task/profile/verified content')
    if not all(isinstance(proof.get(key), str) and re.fullmatch('[0-9a-f]{40}', proof[key])
               for key in ('commit', 'tag_object', 'branch_commit')):
        raise CompletionError('Release evidence requires exact Git object identities')
    tag = proof.get('tag')
    if not isinstance(tag, str) or not tag: raise CompletionError('Release tag is missing')
    expected_refs = {'refs/heads/' + proof['profile']['branch']: proof['branch_commit'],
                     'refs/tags/' + tag: proof['tag_object'], 'refs/tags/' + tag + '^{}': proof['commit']}
    if proof.get('refs') != expected_refs or not proof.get('observed_at'):
        raise CompletionError('Release evidence lacks consistent raw reference readback')
    if proof['profile']['provider'] == 'github-release':
        publication = proof.get('publication') or {}
        if (publication.get('tagName') != tag or publication.get('isDraft') is not False
                or not publication.get('publishedAt') or not publication.get('url')):
            raise CompletionError('Published GitHub release proof is missing or draft')
    if git_text(repo, 'cat-file', '-t', proof['tag_object']) != 'tag' or git_text(repo, 'rev-parse', proof['tag_object'] + '^{commit}') != proof['commit']:
        raise CompletionError('Recorded release tag object does not bind the tested commit')
    if content_snapshot(repo, task, proof['commit'])['digest'] != digest:
        raise CompletionError('Released Git commit differs from tested product content')
    if remote:
        observed = observe_release(repo, task, proof['tag'])
        if any(observed[key] != proof.get(key) for key in ('tag_object', 'commit', 'profile', 'remote_url')):
            raise CompletionError('Remote release changed after recorded readback')
