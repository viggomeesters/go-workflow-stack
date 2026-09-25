"""Effectless prepared-release reconciliation is separate from ordinary workspace merge."""
import json
from pathlib import Path

import pytest

from test_abc_release import fixture, prepare
from test_abc_worktrees import git


def state(repo):
    return json.loads((repo / '.go/runs/task-schema-smoke/release-state.json').read_text())


def advance(repo, path='unrelated.txt'):
    (repo / path).write_text('new base\n')
    git(repo, 'add', path)
    git(repo, 'commit', '-qm', 'advance base')
    git(repo, 'push', 'origin', 'main')
    return git(repo, 'rev-parse', 'HEAD')


def snapshot(repo, worker):
    from go_workflow.worktrees import git_text
    return (git_text(worker, 'rev-parse', 'HEAD'),
            (worker / 'app.txt').read_bytes(), (worker / 'VERSION').read_bytes(),
            (worker / 'CHANGELOG.md').read_bytes(),
            (repo / '.go/workspaces/task-schema-smoke.json').read_bytes(),
            (repo / '.go/runs/task-schema-smoke/release-state.json').read_bytes())


def test_prepared_release_reconciles_linear_base_preserving_candidate_and_invalidates_proof(tmp_path):
    from go_workflow.release import reconcile_prepared_release
    repo, worker, source = fixture(tmp_path)
    prepared = prepare(repo)
    old = snapshot(repo, worker)
    new_base = advance(repo)
    result = reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    assert result['base_commit'] == new_base
    assert git(worker, 'rev-parse', 'HEAD') == new_base
    assert (worker / 'app.txt').read_bytes() == old[1]
    assert (worker / 'VERSION').read_bytes() == old[2]
    assert (worker / 'CHANGELOG.md').read_bytes() == old[3]
    updated = state(repo)
    registry = json.loads((repo / '.go/workspaces/task-schema-smoke.json').read_text())
    assert updated['phase'] == 'prepared' and updated['effects'] == {}
    assert updated['base_commit'] == updated['remote_base'] == new_base
    assert updated['workspace'] == registry
    assert updated['preparation'] == prepared['preparation']
    assert updated['tag'] == prepared['tag']
    assert 'content_digest' not in updated
    assert registry['reconciliations'][-1]['evidence_invalidated'] is True
    assert reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')['base_commit'] == new_base


def test_reconcile_rejects_any_effect_and_leaves_candidate_unchanged(tmp_path):
    from go_workflow.release import reconcile_prepared_release, PublicationError
    repo, worker, _ = fixture(tmp_path)
    prepare(repo)
    advance(repo)
    path = repo / '.go/runs/task-schema-smoke/release-state.json'
    value = state(repo)
    value['effects']['commit'] = {'expected': {'parent': git(worker, 'rev-parse', 'HEAD')}, 'status': 'pending'}
    path.write_text(json.dumps(value))
    old = snapshot(repo, worker)
    with pytest.raises(PublicationError, match='effect'):
        reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    assert snapshot(repo, worker) == old


def test_reconcile_rejects_overlapping_candidate_change_before_mutation(tmp_path):
    from go_workflow.release import reconcile_prepared_release, PublicationError
    repo, worker, _ = fixture(tmp_path)
    prepare(repo)
    (repo / 'VERSION').write_text('9.9.9\n')
    git(repo, 'add', 'VERSION'); git(repo, 'commit', '-qm', 'conflicting version')
    git(repo, 'push', 'origin', 'main')
    old = snapshot(repo, worker)
    with pytest.raises(PublicationError, match='overlap|conflict|preparation'):
        reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    assert snapshot(repo, worker) == old


def test_reconcile_rejects_unowned_workspace_without_mutation(tmp_path):
    from go_workflow.release import reconcile_prepared_release
    repo, worker, _ = fixture(tmp_path)
    prepare(repo)
    advance(repo)
    old = snapshot(repo, worker)
    with pytest.raises(ValueError):
        reconcile_prepared_release(repo, 'task-schema-smoke', 'someone-else', 'run-1')
    assert snapshot(repo, worker) == old

def test_rebind_only_corrected_verification_then_reconcile(tmp_path, monkeypatch):
    import go_workflow.release as publisher
    from go_workflow.release import rebind_prepared_verification, reconcile_prepared_release
    from go_workflow.completion import contract_digest
    monkeypatch.setattr(publisher, '_safe_verification_correction',
                        lambda control, task_id, before, after: before != after and after == ["python3 -c 'print(1)'", "git diff --check"])
    repo, worker, source = fixture(tmp_path)
    prepared = prepare(repo)
    candidate = snapshot(repo, worker)
    task = json.loads(source.read_text())
    task['verification'] = ["python3 -c 'print(1)'", "git diff --check"]
    source.write_text(json.dumps(task))
    git(repo, 'add', str(source.relative_to(repo)))
    git(repo, 'commit', '-qm', 'correct verification commands')
    git(repo, 'push', 'origin', 'main')
    updated = rebind_prepared_verification(repo, 'task-schema-smoke', 'owner', 'run-1')
    assert updated['contract_digest'] == contract_digest(task)
    assert updated['effects'] == {} and updated['phase'] == 'prepared'
    assert updated['version'] == prepared['version'] and updated['preparation'] == prepared['preparation']
    assert snapshot(repo, worker)[:3] == candidate[:3]
    assert rebind_prepared_verification(repo, 'task-schema-smoke', 'owner', 'run-1')['contract_digest'] == contract_digest(task)
    assert reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')['base_commit'] == git(repo, 'rev-parse', 'HEAD')

@pytest.mark.parametrize('mutation', ['summary', 'scope', 'effect', 'foreign_owner'])
def test_rebind_rejects_other_changes_without_checkpoint_mutation(tmp_path, mutation, monkeypatch):
    import go_workflow.release as publisher
    from go_workflow.release import rebind_prepared_verification, PublicationError
    monkeypatch.setattr(publisher, '_safe_verification_correction',
                        lambda control, task_id, before, after: before != after and after == ["python3 -c 'print(1)'"])
    repo, worker, source = fixture(tmp_path)
    prepare(repo)
    task = json.loads(source.read_text())
    task['verification'] = ["python3 -c 'print(1)'"]
    if mutation == 'summary':
        task['summary'] = 'not the original task'
    if mutation == 'scope':
        task['scope']['modify'].append('personal.txt')
    source.write_text(json.dumps(task))
    if mutation == 'effect':
        path = repo / '.go/runs/task-schema-smoke/release-state.json'
        value = state(repo)
        value['effects']['commit'] = {'status': 'pending', 'expected': {}}
        path.write_text(json.dumps(value))
    checkpoint = snapshot(repo, worker)[3:]
    with pytest.raises((PublicationError, ValueError)):
        rebind_prepared_verification(repo, 'task-schema-smoke',
                                     'other' if mutation == 'foreign_owner' else 'owner', 'run-1')
    assert snapshot(repo, worker)[3:] == checkpoint

def test_rebind_allowlist_preserves_every_release_gate_and_rejects_true(tmp_path):
    from go_workflow.release import _safe_verification_correction, _safe_second_verification_correction
    control = tmp_path / 'go-workflow-stack'
    before = [
        'PYTHONPATH=. uv run --no-project --with "pytest>=8,<9" --with "jsonschema>=4.23" pytest -q',
        'make check', 'python3 cli/go.py validate .',
        'python3 cli/go.py architecture validate . --json',
        'python3 cli/go.py doctor . --platform wsl --agent hermes --json',
        'bash scripts/release-check.sh --allow-candidate', 'git diff --check',
    ]
    after = before.copy()
    after[0] = 'env -u PYTHONPATH uv run --no-project --with "pytest>=8,<9" --with "jsonschema>=4.23" python -m pytest -q'
    after[5] = 'GO_PROJECT_TEMPLATE=' + str(control.parent / 'go-project-template') + ' ./scripts/release-check.sh --allow-candidate'
    task = 'release-safe-active-task-handoff-v0347'
    assert _safe_verification_correction(control, task, before, after)
    assert not _safe_verification_correction(control, task, before, ['true'])
    assert not _safe_verification_correction(control, task, before, after[:-1])
    assert not _safe_verification_correction(control, task, before, [*after[:1], 'true', *after[2:]])
    assert not _safe_verification_correction(control, task, before, [*after[:5], before[5], *after[6:]])
    assert not _safe_verification_correction(control, 'foreign-task', before, after)
    final = after.copy()
    final[4] = "python3 -c 'from go_workflow.release import candidate_doctor_gate; candidate_doctor_gate()'"
    final[5] += ' --allow-local-origin'
    assert _safe_second_verification_correction(control, task, after, final)
    assert not _safe_verification_correction(control, task, before, final)
    assert not _safe_second_verification_correction(control, task, after, [*final[:5], 'true', final[6]])
    assert not _safe_second_verification_correction(control, task, after, [*final[:4], 'true', *final[5:]])
    assert not _safe_second_verification_correction(control, task, before, final)


def test_candidate_doctor_requires_only_missing_pretag(monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    import go_workflow.release as publisher
    good = {'ready': False, 'contract': {'valid': True, 'errors': []},
            'agent': {'compatible': True},
            'prerequisites': [{'name': name, 'available': True} for name in ('python', 'git', 'bash', 'make', 'uv')],
            'actions': ['checkout the pinned go-workflow-stack ref v0.3.47'],
            'stack': {'version': '0.3.47', 'required_version': '0.3.47',
                      'ref': 'v0.3.47', 'required_ref': 'v0.3.47',
                      'provenance_requested_ref': 'v0.3.47', 'identity_source': 'git-checkout',
                      'git_head': 'a'*40, 'pinned_commit': None, 'exact_ref': False,
                      'compatible': False, 'development_override': False}}
    report = good.copy()
    monkeypatch.setattr(publisher.subprocess, 'run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=json.dumps(report), stderr=''))
    publisher.candidate_doctor_gate()
    assert 'only the absent immutable pretag' in capsys.readouterr().out
    for changed in ({'ready': True}, {'prerequisites': []},
                    {'prerequisites': good['prerequisites'][:-1]},
                    {'prerequisites': [{**good['prerequisites'][0], 'available': False}, *good['prerequisites'][1:]]},
                    {'actions': ['unknown failure']},
                    {'stack': {**good['stack'], 'development_override': True}},
                    {'stack': {**good['stack'], 'pinned_commit': 'a'*40}}):
        report = {**good, **changed}
        with pytest.raises(publisher.PublicationError, match='beyond expected'):
            publisher.candidate_doctor_gate()


def test_reconcile_recovers_after_worker_fast_forward_before_checkpoint(tmp_path, monkeypatch):
    import go_workflow.release as publisher
    repo, worker, _ = fixture(tmp_path)
    prepare(repo)
    new_base = advance(repo)
    original = publisher.atomic_json
    registry = repo / '.go/workspaces/task-schema-smoke.json'
    def interrupt(path, value):
        if path == registry:
            raise RuntimeError('lost acknowledgement after fast-forward')
        return original(path, value)
    monkeypatch.setattr(publisher, 'atomic_json', interrupt)
    with pytest.raises(RuntimeError, match='lost acknowledgement'):
        publisher.reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    assert git(worker, 'rev-parse', 'HEAD') == new_base
    monkeypatch.setattr(publisher, 'atomic_json', original)
    assert publisher.reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')['base_commit'] == new_base


def test_reconcile_rejects_candidate_tamper_after_fast_forward_crash(tmp_path, monkeypatch):
    import go_workflow.release as publisher
    repo, worker, _ = fixture(tmp_path)
    prepare(repo)
    advance(repo)
    original = publisher.atomic_json
    registry = repo / '.go/workspaces/task-schema-smoke.json'
    def interrupt(path, value):
        if path == registry:
            raise RuntimeError('lost acknowledgement after fast-forward')
        return original(path, value)
    monkeypatch.setattr(publisher, 'atomic_json', interrupt)
    with pytest.raises(RuntimeError, match='lost acknowledgement'):
        publisher.reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    monkeypatch.setattr(publisher, 'atomic_json', original)
    (worker / 'app.txt').write_text('TAMPERED AFTER CRASH\n')
    with pytest.raises(publisher.PublicationError, match='fingerprint'):
        publisher.reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')


def test_reconcile_preserves_broken_symlink_candidate_without_dereference(tmp_path):
    from go_workflow.release import reconcile_prepared_release
    repo, worker, _ = fixture(tmp_path)
    prepare(repo)
    (worker / 'app.txt').unlink()
    (worker / 'app.txt').symlink_to('missing-target')
    new_base = advance(repo)
    state = reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    assert state['base_commit'] == new_base
    assert (worker / 'app.txt').is_symlink()
    assert (worker / 'app.txt').readlink().as_posix() == 'missing-target'


def test_reconcile_preserves_non_utf8_symlink_target(tmp_path):
    import os
    from go_workflow.release import reconcile_prepared_release
    repo, worker, _ = fixture(tmp_path)
    prepare(repo)
    (worker / 'app.txt').unlink()
    os.symlink(b'\xff', os.fsencode(worker / 'app.txt'))
    new_base = advance(repo)
    assert reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')['base_commit'] == new_base
    assert os.readlink(os.fsencode(worker / 'app.txt')) == b'\xff'


def test_reconcile_rejects_tampered_idempotent_retry(tmp_path):
    from go_workflow.release import reconcile_prepared_release, PublicationError
    repo, worker, _ = fixture(tmp_path)
    prepare(repo)
    advance(repo)
    reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    (worker / 'app.txt').unlink()
    with pytest.raises(PublicationError, match='fingerprint'):
        reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')


def test_reconcile_rolls_forward_registry_write_and_rejects_preparation_tamper(tmp_path, monkeypatch):
    import go_workflow.release as publisher
    repo, worker, _ = fixture(tmp_path)
    prepare(repo)
    new_base = advance(repo)
    original = publisher.atomic_json
    release_state = repo / '.go/runs/task-schema-smoke/release-state.json'
    def interrupt(path, value):
        if path == release_state:
            raise RuntimeError('lost acknowledgement after registry')
        return original(path, value)
    monkeypatch.setattr(publisher, 'atomic_json', interrupt)
    with pytest.raises(RuntimeError, match='lost acknowledgement'):
        publisher.reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    monkeypatch.setattr(publisher, 'atomic_json', original)
    assert publisher.reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')['base_commit'] == new_base
    (worker / 'VERSION').write_text('tampered\n')
    advance(repo, 'later.txt')
    with pytest.raises(publisher.PublicationError, match='preparation'):
        publisher.reconcile_prepared_release(repo, 'task-schema-smoke', 'owner', 'run-1')
