"""Real managed checkpoints preserve only content-bound confirmed check prefixes."""
import json

from test_taskwise_no_release import setup, run
from test_abc_worktrees import git
from go_workflow.run_state import read_state, state_path


def checkpoint(tmp_path, monkeypatch):
    repo, workspace, capture, args, task = setup(tmp_path, monkeypatch)
    path = repo / '.go/tasks/open' / (task['id'] + '.json')
    task['verification'] = ['test -f app.txt', 'test -s app.txt']
    path.write_text(json.dumps(task))
    git(repo, 'add', '.go'); git(repo, 'commit', '-qm', 'two checks')
    args.base_commit = git(repo, 'rev-parse', 'HEAD')
    args.max_commands = 2
    code, result = run(repo, args, task)
    assert result['status'] == 'budget_exhausted', result
    assert read_state(repo, task['id'])['check_index'] == 1
    args.max_commands = 1
    return repo, workspace, capture, args, task


def test_staging_preserves_confirmed_check(tmp_path, monkeypatch):
    repo, workspace, _, args, task = checkpoint(tmp_path, monkeypatch)
    first = read_state(repo, task['id'])['checks'][0]
    git(workspace, 'add', 'app.txt')
    _, result = run(repo, args, task)
    state = read_state(repo, task['id'])
    assert state['check_index'] == 2, result.get('summary', result)
    assert state['checks'][0] == first
    assert state['history'][-1]['preserved_checks'] == 1


def test_interrupted_second_check_keeps_confirmed_first(tmp_path, monkeypatch):
    repo, _, _, args, task = checkpoint(tmp_path, monkeypatch)
    state = read_state(repo, task['id'])
    first = state['checks'][0]
    state['inflight'] = {'phase': 'verify', 'nonce': 'interrupted', 'attempt': 1, 'started_at': 1}
    state_path(repo, task['id']).write_text(json.dumps(state))
    _, result = run(repo, args, task)
    state = read_state(repo, task['id'])
    assert state['check_index'] == 2, result.get('summary', result)
    assert state['checks'][0] == first


def test_source_or_head_change_reexecutes_checks(tmp_path, monkeypatch):
    repo, workspace, _, args, task = checkpoint(tmp_path, monkeypatch)
    git(workspace, 'commit', '--allow-empty', '-qm', 'new candidate HEAD')
    _, result = run(repo, args, task)
    assert read_state(repo, task['id'])['check_index'] == 1, result
    assert read_state(repo, task['id'])['history'][-1]['invalidated_checks']


def test_tampered_raw_proof_cannot_be_preserved(tmp_path, monkeypatch):
    repo, workspace, _, args, task = checkpoint(tmp_path, monkeypatch)
    state = read_state(repo, task['id'])
    raw = state['checks'][0]['completion_check']['raw']
    target = repo / raw['path']
    if not target.exists():
        target = __import__('pathlib').Path(raw['path'])
    target.write_text('{}')
    git(workspace, 'add', 'app.txt')
    _, result = run(repo, args, task)
    assert read_state(repo, task['id'])['check_index'] == 1, result


def test_hierarchy_refresh_preserves_verified_prefix(tmp_path, monkeypatch):
    repo, _, _, args, task = checkpoint(tmp_path, monkeypatch)
    path = repo / '.go/hierarchy.json'
    path.write_text(path.read_text() + '\n')
    _, result = run(repo, args, task)
    assert read_state(repo, task['id'])['check_index'] == 2, result.get('summary', result)


def test_source_change_invalidates_verified_prefix(tmp_path, monkeypatch):
    repo, workspace, _, args, task = checkpoint(tmp_path, monkeypatch)
    (workspace / 'app.txt').write_text('changed product\n')
    _, result = run(repo, args, task)
    state = read_state(repo, task['id'])
    assert state['check_index'] == 1, result
    assert state['history'][-1]['invalidated_checks']


def test_interrupted_critic_reuses_all_checks(tmp_path, monkeypatch):
    repo, _, capture, args, task = checkpoint(tmp_path, monkeypatch)
    run(repo, args, task)
    state = read_state(repo, task['id'])
    state['phase'] = 'critic'
    state['inflight'] = {'phase': 'critic', 'nonce': 'interrupted', 'attempt': 1, 'started_at': 1}
    before = state['checks']
    state_path(repo, task['id']).write_text(json.dumps(state))
    _, result = run(repo, args, task)
    state = read_state(repo, task['id'])
    assert state['phase'] == 'release', result.get('summary', result)
    assert state['checks'] == before
    assert capture.read_text().splitlines() == ['build', 'critic']


def test_prefix_metadata_must_match_executed_receipt(tmp_path, monkeypatch):
    from copy import deepcopy
    from go_workflow.proof_dependencies import valid_checks
    repo, workspace, _, _, task = checkpoint(tmp_path, monkeypatch)
    checks = read_state(repo, task['id'])['checks']
    assert valid_checks(workspace, task, checks)
    for field, value in [('task_id', 'another-task'), ('phase_id', 'build'),
                         ('requirement_ids', ['invented']), ('cwd', '/wrong'),
                         ('status', 'failed'), ('evidence', ['wrong'])]:
        invalid = deepcopy(checks)
        invalid[0]['completion_check']['verification'][field] = value
        assert not valid_checks(workspace, task, invalid), field
