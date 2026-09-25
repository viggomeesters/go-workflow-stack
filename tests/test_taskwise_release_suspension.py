"""Blocked effectless candidates free their channel without reviving publication."""
import hashlib
import json
import subprocess
import sys

import pytest

from go_workflow import release
from test_abc_release import fixture, prepare, proofs
from test_abc_worktrees import git


def prepared(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo,worker,source=fixture(tmp_path)
    state=prepare(repo)
    return repo,worker,source,state


def candidate_bytes(worker):
    return {str(path.relative_to(worker)):path.read_bytes() for path in worker.rglob('*') if path.is_file()}


def test_suspension_preserves_candidate_proof_and_freezes_old_publish_authority(tmp_path,monkeypatch):
    repo,worker,source,state=prepared(tmp_path,monkeypatch)
    proofs(worker,source)
    before=candidate_bytes(worker)
    path=release.state_path(repo,'task-schema-smoke'); original=path.read_bytes()
    task_before=source.read_bytes()
    result=release.suspend_prepared_release(repo,'task-schema-smoke','owner',reason='Repeated failure blocks this candidate')
    assert result['phase']=='suspended'
    assert result['suspension']['checkpoint_sha256']==hashlib.sha256(original).hexdigest()
    assert candidate_bytes(worker)==before and source.read_bytes()==task_before
    assert json.loads((repo/state['reservation']).read_text())['status']=='released'
    assert release._load(repo,json.loads(source.read_text()),'owner','run-1')['phase']=='suspended'
    for operation in (lambda:prepare(repo), lambda:release.publish_release(repo,'task-schema-smoke','owner','run-1')):
        with pytest.raises(release.PublicationError,match='Suspended release'):operation()
    assert not git(repo,'tag','--list','v1.2.0')


@pytest.mark.parametrize('boundary',['checkpoint','reservation'])
def test_lost_acknowledgement_rolls_forward_without_changing_candidate(tmp_path,monkeypatch,boundary):
    repo,worker,source,state=prepared(tmp_path,monkeypatch)
    before=candidate_bytes(worker)
    real=release.atomic_json;lost=False
    def interrupted(path,value):
        nonlocal lost
        real(path,value)
        matches=(boundary=='checkpoint' and value.get('phase')=='suspended') or (boundary=='reservation' and value.get('status')=='released')
        if matches and not lost:
            lost=True
            raise OSError('lost suspension acknowledgement')
    monkeypatch.setattr(release,'atomic_json',interrupted)
    with pytest.raises(OSError,match='lost suspension'):release.suspend_prepared_release(repo,'task-schema-smoke','owner')
    assert json.loads(release.state_path(repo,'task-schema-smoke').read_text())['phase']=='suspended'
    result=release.suspend_prepared_release(repo,'task-schema-smoke','owner')
    assert result['phase']=='suspended' and candidate_bytes(worker)==before
    assert json.loads((repo/state['reservation']).read_text())['status']=='released'


def test_retry_never_releases_a_later_tasks_reservation(tmp_path,monkeypatch):
    repo,worker,_,state=prepared(tmp_path,monkeypatch)
    release.suspend_prepared_release(repo,'task-schema-smoke','owner')
    path=repo/state['reservation']
    later={'task_id':'another-task','run_id':'another-run','owner':'another-owner','version':'1.2.0','status':'reserved'}
    path.write_text(json.dumps(later))
    assert release.suspend_prepared_release(repo,'task-schema-smoke','owner')['phase']=='suspended'
    assert json.loads(path.read_text())==later


@pytest.mark.parametrize('problem',['pending_effect','observation','foreign_owner','live_writer'])
def test_uncertain_effect_or_writer_never_frees_channel(tmp_path,monkeypatch,problem):
    repo,worker,_,state=prepared(tmp_path,monkeypatch)
    path=release.state_path(repo,'task-schema-smoke')
    child=None
    if problem=='pending_effect':
        state['effects']['commit']={'status':'pending','expected':{'commit':'a'*40}}
        release._save(repo,state)
    elif problem=='observation':
        state['observations'].append({'effect':'tag','observed':{'tag':state['tag']}})
        release._save(repo,state)
    elif problem=='live_writer':
        from go_workflow.run_state import state_path
        managed=state_path(repo,'task-schema-smoke','publication')
        data=json.loads(managed.read_text())
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        data['phase']='release';data['controller']['pid']=child.pid
        managed.write_text(json.dumps(data))
    original=path.read_bytes();reservation=(repo/state['reservation']).read_bytes();before=candidate_bytes(worker)
    try:
        with pytest.raises(ValueError):release.suspend_prepared_release(repo,'task-schema-smoke','foreign' if problem=='foreign_owner' else 'owner')
        assert path.read_bytes()==original and (repo/state['reservation']).read_bytes()==reservation
        assert candidate_bytes(worker)==before
    finally:
        if child:child.terminate();child.wait(timeout=5)


def test_already_suspended_blocked_task_can_finish_only_its_reservation_cleanup(tmp_path,monkeypatch):
    from go_workflow import cli as api
    repo,worker,source,state=prepared(tmp_path,monkeypatch)
    actual=release._release_reservation
    def interrupted(*args):raise OSError('before reservation release')
    monkeypatch.setattr(release,'_release_reservation',interrupted)
    with pytest.raises(OSError):release.suspend_prepared_release(repo,'task-schema-smoke','owner')
    task=json.loads(source.read_text())
    api.block_task_record(repo,repo/'.go',source,task,'owner','failure isolated',[])
    monkeypatch.setattr(release,'_release_reservation',actual)
    before=candidate_bytes(worker)
    result=release.suspend_prepared_release(repo,'task-schema-smoke','owner')
    assert result['phase']=='suspended' and candidate_bytes(worker)==before
    assert json.loads((repo/state['reservation']).read_text())['status']=='released'
    assert (repo/'.go/tasks/blocked/task-schema-smoke.json').exists()


def test_first_suspension_cannot_mutate_a_blocked_task(tmp_path,monkeypatch):
    from go_workflow import cli as api
    repo,worker,source,state=prepared(tmp_path,monkeypatch)
    task=json.loads(source.read_text())
    api.block_task_record(repo,repo/'.go',source,task,'owner','failure isolated',[])
    with pytest.raises(ValueError,match='matching active task owner'):
        release.suspend_prepared_release(repo,'task-schema-smoke','owner')
    assert json.loads((repo/state['reservation']).read_text())['status']=='reserved'
