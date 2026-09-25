"""Task completion is portable only after its closure commit reaches the remote."""
import json
import subprocess

import pytest

from test_abc_release import fixture, prepare, proofs
from test_abc_worktrees import git


def published(tmp_path, monkeypatch):
    from go_workflow.release import publish_release
    from go_workflow.worktrees import cleanup_workspace
    monkeypatch.chdir(tmp_path)
    repo, worker, source = fixture(tmp_path)
    prepare(repo); proofs(worker, source)
    release=publish_release(repo,'task-schema-smoke','owner','run-1')
    cleanup_workspace(repo,'task-schema-smoke','owner','run-1')
    return repo, release


def test_closure_is_portable_and_preserves_unrelated_staged_work(tmp_path, monkeypatch):
    from go_workflow.delivery_closure import synchronize_closure, inspect_closure
    repo, release = published(tmp_path, monkeypatch)
    git(repo,'add','personal.txt')
    first=synchronize_closure(repo,'task-schema-smoke',policy='push')
    assert first['delivered'] is True
    assert git(repo,'rev-parse','v1.2.0^{commit}')==release['commit']
    assert 'personal.txt' in git(repo,'diff','--cached','--name-only')
    assert subprocess.run(['git','show','HEAD:personal.txt'],cwd=repo,capture_output=True).returncode != 0
    again=synchronize_closure(repo,'task-schema-smoke',policy='push')
    assert first['commit']==again['commit']
    clone=tmp_path/'fresh'
    subprocess.run(['git','clone','-q','--branch','main',str(tmp_path/'remote.git'),str(clone)],check=True)
    report=inspect_closure(clone,'task-schema-smoke')
    assert report['delivered'] is True
    from go_workflow.campaign_delivery import delivery_report
    assert delivery_report(clone,'task-schema-smoke')['delivered'] is True
    assert json.loads((clone/'.go/tasks/done/task-schema-smoke.json').read_text())['review_status']=='approved'
    assert not (clone/'.go/tasks/active/task-schema-smoke.json').exists()


@pytest.mark.parametrize('boundary',['commit','push'])
def test_lost_closure_ack_does_not_duplicate_effect(tmp_path,monkeypatch,boundary):
    import go_workflow.delivery_closure as closure
    repo,_=published(tmp_path,monkeypatch)
    original=closure._effect
    lost=False
    def interrupted(repo, operation, *args, **kwargs):
        nonlocal lost
        result=original(repo,operation,*args,**kwargs)
        if operation==boundary and not lost:
            lost=True
            raise OSError('lost acknowledgement')
        return result
    monkeypatch.setattr(closure,'_effect',interrupted)
    with pytest.raises(OSError,match='lost acknowledgement'):
        closure.synchronize_closure(repo,'task-schema-smoke',policy='push')
    result=closure.synchronize_closure(repo,'task-schema-smoke',policy='push')
    assert result['delivered']
    assert git(repo,'log','--format=%s').count('Synchronize delivery task-schema-smoke')==1


def test_unpushed_and_changed_closure_never_claim_delivery(tmp_path,monkeypatch):
    from go_workflow.delivery_closure import synchronize_closure, inspect_closure
    repo,_=published(tmp_path,monkeypatch)
    local=synchronize_closure(repo,'task-schema-smoke',policy='local-commit')
    assert local['delivered'] is True and local['push']=='not_authorized'
    path=repo/'.go/tasks/done/task-schema-smoke.json'
    path.write_text('{}')
    assert inspect_closure(repo,'task-schema-smoke')['delivered'] is False


def test_fake_committed_manifest_is_not_completion(tmp_path):
    from go_workflow.delivery_closure import inspect_closure
    repo=tmp_path/'repo';repo.mkdir()
    subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
    git(repo,'config','user.name','Fixture');git(repo,'config','user.email','fixture@example.com')
    path=repo/'.go/runs/fake/delivery-closure.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'schema':'go-workflow.delivery-closure.v1','task_id':'fake','files':{},'policy':'local-commit'}))
    git(repo,'add','.');git(repo,'commit','-qm','fake')
    assert inspect_closure(repo,'fake')['delivered'] is False


def test_branch_switch_after_lost_ack_prevents_all_further_effects(tmp_path,monkeypatch):
    import go_workflow.delivery_closure as closure
    repo,_=published(tmp_path,monkeypatch)
    original=closure._effect
    def interrupted(repo, operation, *args, **kwargs):
        result=original(repo,operation,*args,**kwargs)
        if operation=='commit':raise OSError('lost acknowledgement')
        return result
    monkeypatch.setattr(closure,'_effect',interrupted)
    with pytest.raises(OSError):closure.synchronize_closure(repo,'task-schema-smoke',policy='push')
    git(repo,'checkout','-qb','user-feature')
    before=git(repo,'rev-parse','main')
    monkeypatch.setattr(closure,'_effect',original)
    with pytest.raises(ValueError,match='branch changed'):
        closure.synchronize_closure(repo,'task-schema-smoke',policy='push')
    assert git(repo,'rev-parse','main')==before


def test_recovery_preserves_new_user_staged_content(tmp_path,monkeypatch):
    import go_workflow.delivery_closure as closure
    repo,_=published(tmp_path,monkeypatch)
    original=closure._effect
    def interrupted(repo,operation,*args,**kwargs):
        result=original(repo,operation,*args,**kwargs)
        if operation=='commit':raise OSError('lost acknowledgement')
        return result
    monkeypatch.setattr(closure,'_effect',interrupted)
    with pytest.raises(OSError):closure.synchronize_closure(repo,'task-schema-smoke',policy='push')
    path=repo/'.go/tasks/done/task-schema-smoke.json'
    content=path.read_bytes()
    path.write_text('user staged alternate');git(repo,'add',str(path));path.write_bytes(content)
    monkeypatch.setattr(closure,'_effect',original)
    with pytest.raises(ValueError,match='User staged'):
        closure.synchronize_closure(repo,'task-schema-smoke',policy='push')
    assert git(repo,'show',':.go/tasks/done/task-schema-smoke.json')=='user staged alternate'


def test_closure_binds_actual_product_not_later_head(tmp_path,monkeypatch):
    from go_workflow.delivery_closure import synchronize_closure,inspect_closure
    repo,release=published(tmp_path,monkeypatch)
    marker=repo/'.go/extra.json';marker.write_text('{}')
    git(repo,'add',str(marker));git(repo,'commit','-qm','later operational work')
    assert git(repo,'rev-parse','HEAD')!=release['commit']
    assert synchronize_closure(repo,'task-schema-smoke',policy='push')['delivered']
    path=repo/'.go/runs/task-schema-smoke/delivery-closure.json'
    manifest=json.loads(path.read_text())
    assert manifest['product_commit']==release['commit']
    manifest['product_commit']=git(repo,'rev-parse','HEAD')
    path.write_text(json.dumps(manifest))
    git(repo,'add',str(path));git(repo,'commit','-qm','tampered product identity')
    assert not inspect_closure(repo,'task-schema-smoke')['delivered']


def test_profileless_closure_checks_integrated_bytes_before_publication(tmp_path,monkeypatch):
    from test_taskwise_no_release import setup,run
    from go_workflow.delivery_closure import synchronize_closure
    repo,_,_,args,task=setup(tmp_path,monkeypatch)
    assert task['id'] in run(repo,args,task)[1]['completed_tasks']
    registry=repo/'.go/workspaces'/f'{task["id"]}.json'
    record=json.loads(registry.read_text())
    record['integration']['integrated_commit']=record['base_commit']
    registry.write_text(json.dumps(record))
    head=git(repo,'rev-parse','HEAD')
    with pytest.raises(ValueError,match='product bytes differ'):
        synchronize_closure(repo,task['id'],policy='push')
    assert git(repo,'rev-parse','HEAD')==head
    assert head in git(repo,'ls-remote','origin','refs/heads/main')
