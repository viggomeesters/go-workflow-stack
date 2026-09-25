"""Real managed parent/repair workspaces; publication never discards parent work."""
from copy import deepcopy
import json
import pytest

from test_taskwise_no_release import setup, run
from test_abc_worktrees import git
from go_workflow import cli
from go_workflow.completion import contract_digest
from go_workflow.delivery_closure import synchronize_closure
from go_workflow.repair_reconciliation import reconcile_repair_parent


def fixture(tmp_path,monkeypatch,conflict=False,parent_policy='push'):

    repo,worker,capture,args,parent=setup(tmp_path,monkeypatch,policy=parent_policy)
    parent['scope']['modify'].append('parent.txt')
    parent['verification'].append("python3 -c \"from pathlib import Path; assert Path('parent.txt').read_text() == 'draft'\"")
    (repo/'.go/tasks/open'/f'{parent["id"]}.json').write_text(json.dumps(parent))
    args.max_commands=1
    assert run(repo,args,parent)[1]['status']=='budget_exhausted'
    (worker/'parent.txt').write_text('draft')
    if conflict:(worker/'app.txt').write_text('conflicting parent bytes')
    path=repo/'.go/tasks/active'/f'{parent["id"]}.json';active=json.loads(path.read_text())
    cli.block_task_record(repo,repo/'.go',path,active,'owner','Repair prerequisite',[])
    repair=deepcopy(parent);repair['id']='repair'
    repair['scope']['modify']=['app.txt'];repair['verification']=parent['verification'][:1]
    repair['campaign_repair']={'parent_id':parent['id'],'parent_contract_sha256':contract_digest(active),'campaign_id':'campaign'}
    path=repo/'.go/tasks/open/repair.json';path.write_text(json.dumps(repair))
    path=repo/'.go/hierarchy.json';hierarchy=json.loads(path.read_text());hierarchy['epics'][0]['tasks'].append('repair');path.write_text(json.dumps(hierarchy))
    repair_args=deepcopy(args);repair_args.max_commands=30;repair_args.task_id='repair'
    repair_args.ship_policy='push';repair_args.allow_push=True
    repair_args.workspace_path=str(tmp_path/'repair-worker');repair_args.workspace_branch='task/repair';repair_args.run_id='repair-run'
    repair_args.base_commit=git(repo,'rev-parse','HEAD')
    code,result=run(repo,repair_args,repair)
    assert code==0 and 'repair' in result['completed_tasks'],result
    synchronize_closure(repo,'repair',policy='push')
    return repo,worker,capture,args,parent


def test_managed_parent_reconciles_actual_repair_then_completes(tmp_path,monkeypatch):
    from go_workflow.campaign_changes import resume_repaired_parent
    repo,worker,capture,args,parent=fixture(tmp_path,monkeypatch)
    before=json.loads((repo/'.go/runs'/parent['id']/'run-state.json').read_text())
    result=resume_repaired_parent(repo,'repair','owner',reconcile=True)
    assert result['resumed'],result
    assert (worker/'parent.txt').read_text()=='draft' and (worker/'app.txt').read_text()=='built'
    state=json.loads((repo/'.go/runs'/parent['id']/'run-state.json').read_text())
    assert state['workspace']['base_commit']==git(repo,'rev-parse','HEAD')
    assert state['publication']['taskwise_delivery']['remote_base']==state['workspace']['base_commit']
    assert state['publication']['allow_push']==before['publication']['allow_push']
    assert state['phase']=='verify' and state['checks']==[] and state['phase_evidence']==[]
    assert (repo/'.go/tasks/active'/f'{parent["id"]}.json').exists()
    args.max_commands=30
    for field in ('base_commit','workspace_path','workspace_branch','base_branch','run_id'):setattr(args,field,'')
    code,final=run(repo,args,parent)
    assert code==0 and parent['id'] in final['completed_tasks'],final
    assert synchronize_closure(repo,parent['id'],policy='push')['delivered']
    assert (repo/'parent.txt').read_text()=='draft' and (repo/'app.txt').read_text()=='built'
    assert capture.read_text().splitlines().count('build')==2


def test_conflict_keeps_original_dirty_bytes_and_checkpoints(tmp_path,monkeypatch):
    repo,worker,_,_,parent=fixture(tmp_path,monkeypatch,conflict=True)
    paths=[worker/'app.txt',worker/'parent.txt',repo/'.go/tasks/blocked'/f'{parent["id"]}.json',
           repo/'.go/runs'/parent['id']/'run-state.json',repo/'.go/workspaces'/f'{parent["id"]}.json']
    before={path:path.read_bytes() for path in paths};head=git(worker,'rev-parse','HEAD')
    result=reconcile_repair_parent(repo,parent['id'],'repair','owner')
    assert not result['reconciled'] and result['reason']=='repair_merge_conflict'
    assert before=={path:path.read_bytes() for path in paths}
    assert git(worker,'rev-parse','HEAD')==head


@pytest.mark.parametrize('operation',['commit','merge','registry','checkpoint'])
def test_lost_effect_ack_recovers_exact_candidates(tmp_path,monkeypatch,operation):
    import go_workflow.repair_reconciliation as reconciliation
    repo,worker,_,_,parent=fixture(tmp_path,monkeypatch)
    original=reconciliation._effect;lost=False
    def interrupt(name,action,*args,**kwargs):
        nonlocal lost
        result=original(name,action,*args,**kwargs)
        if name==operation and not lost:
            lost=True;raise OSError('lost effect ack')
        return result
    monkeypatch.setattr(reconciliation,'_effect',interrupt)
    with pytest.raises(OSError):reconcile_repair_parent(repo,parent['id'],'repair','owner')
    from go_workflow.migrations import pending_lifecycle_findings
    assert any('Pending parent workspace reconciliation' in item for item in pending_lifecycle_findings(repo))
    result=reconcile_repair_parent(repo,parent['id'],'repair','owner')
    assert result['reconciled'] and (worker/'parent.txt').read_text()=='draft'
    assert not pending_lifecycle_findings(repo)
    assert git(worker,'log','--format=%s').count('Checkpoint blocked parent '+parent['id'])==1
    assert git(worker,'log','--format=%s').count('Reconcile delivered repair repair into '+parent['id'])==1


def test_staged_changes_refused_without_touching_index(tmp_path,monkeypatch):
    repo,worker,_,_,parent=fixture(tmp_path,monkeypatch)
    git(worker,'add','parent.txt');index=git(worker,'write-tree')
    with pytest.raises(ValueError,match='staged changes'):reconcile_repair_parent(repo,parent['id'],'repair','owner')
    assert git(worker,'write-tree')==index and (worker/'parent.txt').read_text()=='draft'


def test_frozen_no_commit_authority_cannot_be_enlarged(tmp_path,monkeypatch):
    repo,worker,_,_,parent=fixture(tmp_path,monkeypatch,parent_policy='none')
    head=git(worker,'rev-parse','HEAD');dirty=(worker/'parent.txt').read_bytes()
    with pytest.raises(ValueError,match='Frozen parent authority'):
        reconcile_repair_parent(repo,parent['id'],'repair','owner')
    assert git(worker,'rev-parse','HEAD')==head and (worker/'parent.txt').read_bytes()==dirty


def test_external_base_advance_is_not_silently_folded_in(tmp_path,monkeypatch):
    repo,worker,_,_,parent=fixture(tmp_path,monkeypatch)
    git(repo,'commit','--allow-empty','-m','Unrelated external advance')
    git(repo,'push','origin','main')
    head=git(worker,'rev-parse','HEAD');dirty=(worker/'parent.txt').read_bytes()
    with pytest.raises(ValueError,match='exact delivered repair closure'):
        reconcile_repair_parent(repo,parent['id'],'repair','owner')
    assert git(worker,'rev-parse','HEAD')==head and (worker/'parent.txt').read_bytes()==dirty
