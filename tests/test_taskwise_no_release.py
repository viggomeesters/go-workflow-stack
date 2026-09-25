"""Managed no-release task delivery with real Git and durable proof."""
import json
import subprocess

import pytest

from test_abc_resume import runner_fixture
from test_abc_worktrees import git
from go_workflow import cli as api
from go_workflow.run_state import execute_managed


def setup(tmp_path, monkeypatch, policy='push'):
    repo, _, workspace, capture = runner_fixture(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    path = repo / '.go/tasks/open/task-schema-smoke.json'
    task = json.loads(path.read_text())
    task['execution_contract']['task_kind'] = 'mechanical'
    task['execution_contract']['release'] = {'mode': 'none', 'reason': 'Internal task needs no versioned release'}
    task['intent_source'] = {'text': 'app.txt contains built', 'sha256': __import__('hashlib').sha256(b'app.txt contains built').hexdigest(), 'source_ref': 'fixture'}
    task['outcome_tracking_version'] = 1
    task['requested_outcomes'] = [{'id': 'R1', 'text': 'app.txt contains built', 'status': 'pending',
                                   'source': 'intake_acceptance', 'evidence': []}]
    path.write_text(json.dumps(task))
    git(repo, 'add', '.go'); git(repo, 'commit', '-qm', 'no release task')
    base = git(repo, 'rev-parse', 'HEAD')
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    git(repo, 'remote', 'add', 'origin', str(remote)); git(repo, 'push', 'origin', 'main')
    args = api.build_parser().parse_args(['auto', str(repo), '--execute', '--task-id', task['id'],
        '--agent', 'owner', '--executor-agent', 'codex', '--max-commands', '30', '--max-minutes', '5',
        '--workspace-path', str(workspace), '--workspace-branch', 'task/resume', '--base-branch', 'main',
        '--base-commit', base, '--run-id', 'resume-run', '--json'])
    args.taskwise_delivery = True
    args.ship_policy = policy
    args.allow_push = policy == 'push'
    return repo, workspace, capture, args, task


def run(repo, args, task):
    return execute_managed(repo, args, 'go-auto', task, api)


def test_default_profileless_push_finishes_with_real_proof_and_no_tag(tmp_path, monkeypatch):
    repo, workspace, capture, args, task = setup(tmp_path, monkeypatch)
    code, result = run(repo, args, task)
    assert code == 0 and task['id'] in result['completed_tasks'], result.get('summary', result)
    done = json.loads((repo / '.go/tasks/done' / f'{task["id"]}.json').read_text())
    assert done['review_status'] == 'approved'
    assert set(done['completion_evidence']) == {'schema', 'verification', 'critic'}
    assert not git(repo, 'tag', '--list')
    head = git(repo, 'rev-parse', 'HEAD')
    assert head in git(repo, 'ls-remote', 'origin', 'refs/heads/main')
    assert not workspace.exists()
    assert capture.read_text().splitlines() == ['build', 'critic']
    assert run(repo, args, task)[0] == 0
    assert git(repo, 'rev-parse', 'HEAD') == head


@pytest.mark.parametrize('policy', ['commit', 'local-commit'])
def test_local_commit_does_not_push(tmp_path, monkeypatch, policy):
    repo, _, _, args, task = setup(tmp_path, monkeypatch, policy)
    before = git(repo, 'ls-remote', 'origin', 'refs/heads/main')
    code, result = run(repo, args, task)
    assert code == 0 and task['id'] in result['completed_tasks'], result.get('summary', result)
    assert git(repo, 'ls-remote', 'origin', 'refs/heads/main') == before
    assert git(repo, 'rev-parse', 'HEAD') not in before


@pytest.mark.parametrize('operation', ['commit', 'merge', 'push'])
def test_lost_acknowledgement_does_not_repeat_effect(tmp_path, monkeypatch, operation):
    from go_workflow import release
    repo, _, capture, args, task = setup(tmp_path, monkeypatch)
    original = release._write_command
    calls = []
    def interrupted(session, cwd, argv, **kwargs):
        result = original(session, cwd, argv, **kwargs)
        if argv[:2] == ['git', operation]:
            calls.append(operation)
            if len(calls) == 1:
                raise release.PublicationError('lost acknowledgement')
        return result
    monkeypatch.setattr(release, '_write_command', interrupted)
    code, result = run(repo, args, task)
    assert result['status'] == 'resume_gate', result
    code, result = run(repo, args, task)
    assert code == 0 and task['id'] in result['completed_tasks'], result.get('summary', result)
    assert calls == [operation]
    assert capture.read_text().splitlines() == ['build', 'critic']


def test_none_is_a_concrete_blocker_and_legacy_run_is_not_elevated(tmp_path, monkeypatch):
    repo, _, _, args, task = setup(tmp_path, monkeypatch, 'none')
    code, result = run(repo, args, task)
    assert result['status'] == 'resume_gate' and 'forbids' in result['summary'], result
    args.ship_policy = 'push'; args.allow_push = True
    code, result = run(repo, args, task)
    assert result['status'] == 'resume_gate' and 'forbids' in result['summary'], result


def test_old_run_cannot_gain_delivery_authority_on_resume(tmp_path, monkeypatch):
    repo, _, _, args, task = setup(tmp_path, monkeypatch)
    args.taskwise_delivery = False
    args.max_commands = 1
    assert run(repo, args, task)[1]['status'] == 'budget_exhausted'
    args.taskwise_delivery = True
    args.max_commands = 30
    result = run(repo, args, task)[1]
    assert result['status'] == 'release_pending'
    state = json.loads((repo / '.go/runs' / task['id'] / 'run-state.json').read_text())
    assert 'taskwise_delivery' not in state['publication']


def test_missing_verification_artifact_refuses_commit(tmp_path, monkeypatch):
    repo, _, _, args, task = setup(tmp_path, monkeypatch)
    args.max_commands = 3
    result = run(repo, args, task)[1]
    assert result['status'] == 'budget_exhausted' and result['phase'] == 'release'
    path = repo / '.go/tasks/active' / f'{task["id"]}.json'
    active = json.loads(path.read_text())
    (repo / active['completion_evidence']['verification']['path']).unlink()
    before = git(repo, 'rev-parse', 'HEAD')
    args.max_commands = 30
    result = run(repo, args, task)[1]
    assert result['status'] == 'resume_gate' and 'proof' in result['summary']
    assert git(repo, 'rev-parse', 'HEAD') == before
