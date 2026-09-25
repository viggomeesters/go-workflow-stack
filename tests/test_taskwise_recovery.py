"""Evidence-backed recovery decisions and real managed verification integration."""
import hashlib
import json

import pytest

from go_workflow.recovery_policy import failure_fingerprint, recovery_decision, validate_recovery_plan


def failure(summary='assert expected content'):
    return {'phase': 'verify', 'strategy': 'direct_fix', 'result': {'returncode': 1, 'summary': summary}}


def plan(workspace, expected, method='minimal_reproduction'):
    path = workspace / 'app.txt'
    return {'schema': 'go-workflow.recovery-plan.v1', 'failure_fingerprint': expected['failure_fingerprint'],
            'method': method, 'diagnosis': 'The earlier repair left app.txt unchanged; reproduce the exact content assertion.',
            'safe_to_continue': True, 'findings': [{'path': 'app.txt', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                                   'finding': 'Current bytes do not satisfy the declared assertion.'}]}


def test_failure_fingerprint_excludes_administrative_activity():
    a = {'status': 'failed', 'checks': [{'returncode': 1, 'stderr': 'assert mismatch', 'cwd': '/tmp/a', 'elapsed': 1}], 'commands_run': 2}
    b = {'status': 'failed', 'checks': [{'returncode': 1, 'stderr': 'assert mismatch', 'cwd': '/tmp/b', 'elapsed': 2}], 'commands_run': 9}
    assert failure_fingerprint('T1', a) == failure_fingerprint('T1', b)
    assert failure_fingerprint('T1', a) != failure_fingerprint('T2', a)
    b['checks'][0]['stderr'] = 'network unavailable'
    assert failure_fingerprint('T1', a) != failure_fingerprint('T1', b)


def test_successful_repair_and_partial_checks_do_not_erase_same_failure():
    evidence = [failure(), {'phase': 'repair', 'result': {'returncode': 0, 'status': 'success'}},
                {'phase': 'verify', 'result': {'returncode': 0}}, failure()]
    assert recovery_decision('T1', evidence)['required']
    evidence.append({'phase': 'critic', 'result': {'returncode': 0}})
    assert not recovery_decision('T1', evidence)['required']


def test_source_bound_changed_strategy_then_reassessment(tmp_path):
    (tmp_path / 'app.txt').write_text('broken')
    evidence = [failure(), failure()]
    expected = recovery_decision('T1', evidence)
    first = plan(tmp_path, expected)
    assert validate_recovery_plan(tmp_path, first, expected) == first
    evidence.append({'phase': expected['kind'], 'recovery_plan': first})
    evidence += [failure(), failure()]
    expected = recovery_decision('T1', evidence)
    assert expected['kind'] == 'recovery_reassessment'
    with pytest.raises(ValueError, match='same source evidence'):
        validate_recovery_plan(tmp_path, plan(tmp_path, expected, 'renamed_approach'), expected)
    (tmp_path / 'app.txt').write_text('distinct candidate still fails')
    assert validate_recovery_plan(tmp_path, plan(tmp_path, expected, 'isolated_causal_test'), expected)


@pytest.mark.parametrize('fault', ['old_hash', 'old_failure', 'same_method', 'unsafe', 'no_findings', 'escape'])
def test_invalid_plan_cannot_reset_retries(tmp_path, fault):
    (tmp_path / 'app.txt').write_text('broken')
    expected = recovery_decision('T1', [failure(), failure()])
    value = plan(tmp_path, expected)
    if fault == 'old_hash': value['findings'][0]['sha256'] = 'a' * 64
    if fault == 'old_failure': value['failure_fingerprint'] = 'b' * 64
    if fault == 'same_method': value['method'] = 'direct_fix'
    if fault == 'unsafe': value['safe_to_continue'] = False
    if fault == 'no_findings': value['findings'] = []
    if fault == 'escape': value['findings'][0]['path'] = '../app.txt'
    with pytest.raises(ValueError): validate_recovery_plan(tmp_path, value, expected)


def managed_case(tmp_path, monkeypatch, valid_diagnosis=True):
    from test_taskwise_no_release import setup, run
    from test_abc_worktrees import git
    from go_workflow import cli as api
    repo, workspace, capture, args, task = setup(tmp_path, monkeypatch)
    task['verification'] = ["python3 -c \"from pathlib import Path; assert Path('app.txt').read_text() == 'fixed'\""]
    task['acceptance'] = ['app.txt contains fixed']
    task['requested_outcomes'][0]['text'] = 'app.txt contains fixed'
    path = repo / '.go/tasks/open' / f'{task["id"]}.json'
    path.write_text(json.dumps(task))
    git(repo, 'add', '.go'); git(repo, 'commit', '-qm', 'recovery acceptance'); git(repo, 'push', 'origin', 'main')
    args.base_commit = git(repo, 'rev-parse', 'HEAD')
    args.max_commands = 40
    original_hook = api.run_hook_command
    original_critic = api.run_default_critic_agent
    seen = []
    def hook(repo, command, task, attempt, strategy, phase, *rest, **kwargs):
        if phase != 'repair': return original_hook(repo, command, task, attempt, strategy, phase, *rest, **kwargs)
        seen.append(strategy)
        if strategy != 'direct_fix': (repo / 'app.txt').write_text('fixed')
        return {'returncode': 0, 'status': 'success', 'summary': 'Repair phase completed'}
    def critic(repo, agent, task, attempt, strategy, timeout, **kwargs):
        if strategy not in {'recovery_diagnosis', 'recovery_reassessment'}:
            return original_critic(repo, agent, task, attempt, strategy, timeout, **kwargs)
        seen.append(strategy)
        expected = kwargs['feedback']['recovery']
        value = plan(repo, expected)
        if not valid_diagnosis: value['findings'][0]['sha256'] = 'a' * 64
        return {'returncode': 0, 'status': 'success', 'summary': 'Independent causal diagnosis', 'recovery_plan': value}
    monkeypatch.setattr(api, 'run_hook_command', hook)
    monkeypatch.setattr(api, 'run_default_critic_agent', critic)
    return repo, workspace, args, task, seen, run


def test_managed_loop_changes_approach_only_after_verified_diagnosis(tmp_path, monkeypatch):
    repo, _, args, task, seen, run = managed_case(tmp_path, monkeypatch)
    code, result = run(repo, args, task)
    assert code == 0 and result['completed_tasks'] == [task['id']], result.get('summary', result)
    assert seen == ['direct_fix', 'recovery_diagnosis', 'minimal_reproduction']
    state = json.loads((repo / '.go/runs' / task['id'] / 'run-state.json').read_text())
    assert state['publication']['taskwise_recovery'] is True
    assert sum('recovery_plan' in entry for entry in state['phase_evidence']) == 1


def test_invalid_managed_diagnosis_blocks_without_further_repairs(tmp_path, monkeypatch):
    repo, _, args, task, seen, run = managed_case(tmp_path, monkeypatch, valid_diagnosis=False)
    result = run(repo, args, task)[1]
    assert result['status'] == 'recovery_blocked', result.get('summary', result)
    assert seen == ['direct_fix', 'recovery_diagnosis']
    assert (repo / '.go/tasks/active' / f'{task["id"]}.json').exists()


def test_budget_chunk_resumes_same_failure_evidence_and_relative_attempt_budget(tmp_path, monkeypatch):
    repo, _, args, task, seen, run = managed_case(tmp_path, monkeypatch)
    args.max_attempts = 1
    for _ in range(5):
        result = run(repo, args, task)[1]
        if result['completed_tasks']: break
        assert result['status'] == 'budget_exhausted', result
    assert result['completed_tasks'] == [task['id']], result.get('summary', result)
    assert seen == ['direct_fix', 'recovery_diagnosis', 'minimal_reproduction']


@pytest.mark.parametrize('name', ['.go/runs/status.json', '.git/config'])
def test_administrative_churn_cannot_authorize_another_strategy(tmp_path, name):
    path = tmp_path / name; path.parent.mkdir(parents=True); path.write_text('new status')
    expected = recovery_decision('T1', [failure(), failure()])
    value = {'schema': 'go-workflow.recovery-plan.v1', 'failure_fingerprint': expected['failure_fingerprint'],
             'method': 'new_label', 'diagnosis': 'administration changed', 'safe_to_continue': True,
             'findings': [{'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'finding': 'different status'}]}
    with pytest.raises(ValueError, match='administrative'):
        validate_recovery_plan(tmp_path, value, expected)


def test_recovery_source_respects_task_scope(tmp_path):
    (tmp_path / 'app.txt').write_text('broken')
    expected = recovery_decision('T1', [failure(), failure()]); expected['scope'] = {'read': ['docs/**'], 'modify': []}
    with pytest.raises(ValueError, match='scope'):
        validate_recovery_plan(tmp_path, plan(tmp_path, expected), expected)


def test_failure_identity_ignores_logging_clocks_but_keeps_asserted_values():
    first = {'returncode': 1, 'stderr': '2026-09-25T12:00:00Z assert 10 == 11\nElapsed time: 0.14 seconds\n1 failed in 0.14s'}
    second = {'returncode': 1, 'stderr': '2026-09-25T12:01:00Z assert 10 == 11\nElapsed time: 3.25 seconds\n1 failed in 3.25s'}
    assert failure_fingerprint('T1', first) == failure_fingerprint('T1', second)
    second['stderr'] = second['stderr'].replace('10 == 11', '12 == 11')
    assert failure_fingerprint('T1', first) != failure_fingerprint('T1', second)


@pytest.mark.parametrize('fault', ['cosmetic_method', 'path_alias'])
def test_cosmetic_alias_cannot_count_as_new_method_or_source(tmp_path, fault):
    (tmp_path / 'app.txt').write_text('broken')
    expected = recovery_decision('T1', [failure(), failure()])
    value = plan(tmp_path, expected)
    if fault == 'cosmetic_method': value['method'] = 'Direct-Fix '
    else: value['findings'][0]['path'] = './app.txt'
    with pytest.raises(ValueError): validate_recovery_plan(tmp_path, value, expected)
