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
