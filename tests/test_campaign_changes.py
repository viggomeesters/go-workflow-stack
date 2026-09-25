"""Canonical, bounded campaign changes retain intent and cumulative resources."""
from argparse import Namespace
from copy import deepcopy
import hashlib
import json
import pytest

from test_campaign_intake import setup, write
from test_autonomy_contracts import sample
from go_workflow.campaign import _new_state
from go_workflow.campaign_contracts import contract_digest, campaign_findings
from go_workflow.campaign_changes import admit_repair, amend_future_task, pending_change_findings


def fixture(tmp_path):
    repo, parent = setup(tmp_path)
    contract=sample()
    for field,name in [('vision_sha256','vision.json'),('principles_sha256','architecture-principles.json')]:
        contract['basis'][field]=hashlib.sha256((repo/'.go'/name).read_bytes()).hexdigest()
    contract['authority']['expansion']['repair']={'max_tasks':2,'modify':['.go/**'],'outcome_ids':['O1']}
    path=repo/'.go/campaigns/bounded.json';write(path,contract)
    state=_new_state(repo,contract,path,tmp_path/'workers',Namespace(agent='owner',max_commands=20,max_attempts=5))
    state['consumption']['attempts_started']=2;state['resources']['commands_used']=7
    state['failures']=[{'task_id':parent['id'],'fingerprint':'f'*64}]
    write(repo/'.go/runs/campaigns/bounded/state.json',state)
    proof=repo/'.go/evidence/failed.json';write(proof,{'task_id':parent['id'],'failure':'reproducible defect'})
    evidence=[{'path':str(proof.relative_to(repo)),'sha256':hashlib.sha256(proof.read_bytes()).hexdigest()}]
    repair=deepcopy(parent);repair.update(id='repair-one',summary='Repair prerequisite')
    repair['requested_outcomes'][0]['text']='Necessary repair works'
    return repo,path,parent,repair,evidence,state


def admit(values, **kwargs):
    repo,path,parent,repair,evidence,_=values
    args=dict(change_id='fix-one',parent_id=parent['id'],repair_task=repair,outcome_ids=['O1'],
              failure_fingerprint='f'*64,owner='owner',reason='Fix prerequisite preventing original task',evidence=evidence)
    args.update(kwargs)
    return admit_repair(repo,path,**args)


def test_admission_preserves_originals_authority_and_consumption(tmp_path):
    values=fixture(tmp_path);repo,path,parent,repair,evidence,state=values
    old=json.loads(path.read_text());result=admit(values);new=json.loads(path.read_text())
    assert result['revision']==2 and new['goal']['outcomes'][0]==old['goal']['outcomes'][0]
    assert new['authority']['permitted_tasks']==['repair-one',parent['id']]
    for key in old['authority']:
        if key!='permitted_tasks':assert new['authority'][key]==old['authority'][key]
    migrated=json.loads((repo/'.go/runs/campaigns/bounded/state.json').read_text())
    assert migrated['resources']==state['resources'] and migrated['consumption']==state['consumption']
    assert migrated['failures']==state['failures']
    assert migrated['contract']['sha256']==contract_digest(new)
    assert campaign_findings(repo,new,previous=old)==[]
    assert json.loads((repo/f'.go/tasks/open/{parent["id"]}.json').read_text())==parent
    assert admit(values)==result
    assert not pending_change_findings(repo)


@pytest.mark.parametrize('fault',['unrelated','scope','recursive','fingerprint','done','budget','model'])
def test_admission_rejects_unbounded_or_unrelated_work_without_writes(tmp_path,fault):
    values=fixture(tmp_path);repo,path,parent,repair,evidence,state=values
    args={}
    if fault=='unrelated':args['outcome_ids']=['O99']
    elif fault=='scope':repair['scope']['modify']=['/tmp/outside']
    elif fault=='recursive':parent['campaign_repair']={'parent_id':'old'};write(repo/f'.go/tasks/open/{parent["id"]}.json',parent)
    elif fault=='fingerprint':args['failure_fingerprint']='a'*64
    elif fault=='done':
        (repo/f'.go/tasks/open/{parent["id"]}.json').unlink();parent['status']='done';write(repo/f'.go/tasks/done/{parent["id"]}.json',parent)
    elif fault=='budget':
        data=json.loads(path.read_text());data['authority']['expansion']['repair']={'max_tasks':0,'modify':[],'outcome_ids':[]};write(path,data)
    elif fault=='model':repair['execution_contract']['model']['id']='unauthorized'
    before=path.read_bytes()
    with pytest.raises((ValueError,FileNotFoundError)):admit(values,**args)
    assert path.read_bytes()==before
    assert not (repo/'.go/tasks/open/repair-one.json').exists()


def test_future_amendment_preserves_requested_outcomes_and_is_visible(tmp_path):
    values=fixture(tmp_path);repo,path,parent,_,evidence,state=values
    old=json.loads(path.read_text())
    result=amend_future_task(repo,path,change_id='amend-one',task_id=parent['id'],
        patch={'description':'Implementation adjusted after verified discovery'},owner='owner',reason='New evidence',evidence=evidence)
    updated=json.loads((repo/f'.go/tasks/open/{parent["id"]}.json').read_text())
    assert updated['requested_outcomes']==parent['requested_outcomes']
    assert updated['campaign_amendments'][0]['reason']=='New evidence'
    assert campaign_findings(repo,json.loads(path.read_text()),previous=old)==[]
    assert result['revision']==2


@pytest.mark.parametrize('patch',[{'acceptance':[]},{'verification':[]},{'requested_outcomes':[]},
                                  {'scope':{'read':[],'modify':['elsewhere/**']}}])
def test_future_amendment_cannot_weaken_or_expand_authority(tmp_path,patch):
    repo,path,parent,_,evidence,_=fixture(tmp_path)
    with pytest.raises(ValueError):amend_future_task(repo,path,change_id='amend',task_id=parent['id'],patch=patch,
        owner='owner',reason='Changed',evidence=evidence)


def test_amendment_detects_dependency_cycle(tmp_path):
    repo,path,parent,_,evidence,_=fixture(tmp_path)
    with pytest.raises(ValueError,match='cycle'):
        amend_future_task(repo,path,change_id='amend',task_id=parent['id'],patch={'dependencies':[
            {'project':parent['project'],'task_id':parent['id'],'requires':'done'}]},
            owner='owner',reason='Changed',evidence=evidence)


def test_crash_after_task_write_recovers_without_duplicate_admission(tmp_path,monkeypatch):
    import go_workflow.campaign_changes as changes
    values=fixture(tmp_path);repo,path,parent,repair,evidence,state=values
    original=changes.atomic_json;failed=False
    def interrupted(target,value):
        nonlocal failed
        original(target,value)
        if target.name=='repair-one.json' and not failed:
            failed=True;raise OSError('lost ack')
    monkeypatch.setattr(changes,'atomic_json',interrupted)
    with pytest.raises(OSError):admit(values)
    assert pending_change_findings(repo)
    monkeypatch.setattr(changes,'atomic_json',original)
    admit(values)
    assert not pending_change_findings(repo)
    assert json.loads(path.read_text())['authority']['permitted_tasks'].count('repair-one')==1


def test_user_conflict_during_recovery_is_preserved(tmp_path,monkeypatch):
    import go_workflow.campaign_changes as changes
    values=fixture(tmp_path);repo,path,parent,repair,evidence,state=values
    original=changes._apply
    monkeypatch.setattr(changes,'_apply',lambda *args: (_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(OSError):admit(values)
    data=json.loads((repo/'.go/hierarchy.json').read_text());data['user_note']='preserve';write(repo/'.go/hierarchy.json',data)
    monkeypatch.setattr(changes,'_apply',original)
    with pytest.raises(ValueError,match='User changes'):admit(values)
    assert json.loads((repo/'.go/hierarchy.json').read_text())['user_note']=='preserve'


def test_changed_source_evidence_is_rejected(tmp_path):
    values=fixture(tmp_path);repo,_,_,_,evidence,_=values
    (repo/evidence[0]['path']).write_text('changed')
    with pytest.raises(ValueError,match='evidence changed'):admit(values)


@pytest.mark.parametrize('interrupt', [False, True])
def test_parent_resumes_only_after_real_published_repair(tmp_path,monkeypatch,interrupt):
    from test_taskwise_closure import published
    from go_workflow.completion import contract_digest as task_digest
    from go_workflow.delivery_closure import synchronize_closure
    from go_workflow.campaign_changes import resume_repaired_parent
    repo,_=published(tmp_path,monkeypatch)
    repair_path=repo/'.go/tasks/done/task-schema-smoke.json';repair=json.loads(repair_path.read_text())
    parent=deepcopy(repair);parent.update(id='original-parent',status='blocked',claim={'agent':None,'claimed_at':None})
    for key in ('completion_evidence','release_receipt','review_history','work_status','review_status'):
        parent.pop(key,None)
    parent['blocked']={'reason':'waiting for repair'}
    write(repo/'.go/tasks/blocked/original-parent.json',parent)
    repair['campaign_repair']={'parent_id':parent['id'],'parent_contract_sha256':task_digest(parent),'campaign_id':'campaign'}
    write(repair_path,repair)
    synchronize_closure(repo,repair['id'],policy='push')
    if interrupt:
        import go_workflow.campaign_changes as changes
        original=changes.atomic_json
        def interrupted(target,value):
            original(target,value)
            if target.name=='original-parent.json':raise OSError('lost parent resume ack')
        monkeypatch.setattr(changes,'atomic_json',interrupted)
        with pytest.raises(OSError):resume_repaired_parent(repo,repair['id'],'owner')
        assert pending_change_findings(repo)
        monkeypatch.setattr(changes,'atomic_json',original)
    report=resume_repaired_parent(repo,repair['id'],'owner')
    assert report=={'resumed':True,'task_id':parent['id'],'status':'open'}
    assert not (repo/'.go/tasks/blocked/original-parent.json').exists()
    resumed=json.loads((repo/'.go/tasks/open/original-parent.json').read_text())
    assert task_digest(resumed)==task_digest(parent)
    assert resume_repaired_parent(repo,repair['id'],'owner')['resumed'] is False


def test_parent_does_not_resume_from_unproven_status(tmp_path):
    from go_workflow.campaign_changes import resume_repaired_parent
    values=fixture(tmp_path);repo,path,parent,repair,_,_=values;admit(values)
    with pytest.raises(ValueError,match='verified repair delivery'):
        resume_repaired_parent(repo,repair['id'],'owner')


def test_active_future_task_criteria_cannot_be_amended(tmp_path):
    repo,path,parent,_,evidence,_=fixture(tmp_path)
    (repo/f'.go/tasks/open/{parent["id"]}.json').unlink();parent['status']='active'
    write(repo/f'.go/tasks/active/{parent["id"]}.json',parent)
    with pytest.raises(ValueError,match='open'):
        amend_future_task(repo,path,change_id='amend',task_id=parent['id'],patch={'description':'change'},
                          owner='owner',reason='new info',evidence=evidence)


def test_live_managed_controller_prevents_future_amendment(tmp_path):
    from go_workflow.state_io import repository_lock,StateLockError
    repo,path,parent,_,evidence,_=fixture(tmp_path)
    before=(repo/f'.go/tasks/open/{parent["id"]}.json').read_bytes()
    with repository_lock(repo/'.go','managed-run-'+parent['id']):
        with pytest.raises(StateLockError,match='live state lock'):
            amend_future_task(repo,path,change_id='amend',task_id=parent['id'],patch={'description':'change'},
                              owner='owner',reason='new info',evidence=evidence)
    assert (repo/f'.go/tasks/open/{parent["id"]}.json').read_bytes()==before


def test_parent_waits_for_actual_repair_delivery_and_revision_resolves_automatically(tmp_path):
    from go_workflow.campaign import plan_campaign
    from go_workflow import cli
    values=fixture(tmp_path);repo,path,parent,repair,_,_=values;admit(values)
    projection,selected=plan_campaign(repo,path,cli)
    assert [task['id'] for task in selected]==[repair['id']]
    assert any(item['task_id']==parent['id'] and 'repair' in str(item['findings']) for item in projection['skipped_tasks'])
    # Merely setting done does not unblock the original task.
    source=repo/'.go/tasks/open/repair-one.json';value=json.loads(source.read_text());value['status']='done'
    write(repo/'.go/tasks/done/repair-one.json',value);source.unlink()
    assert plan_campaign(repo,path,cli)[1]==[]


def test_parent_resumption_waits_for_all_admitted_repairs(tmp_path,monkeypatch):
    from go_workflow.campaign import _resume_delivered_repairs
    import go_workflow.campaign_changes as changes
    import go_workflow.campaign_delivery as delivery
    repo=tmp_path/'repo';calls=[]
    for identity,status in [('first','done'),('second','open')]:
        write(repo/'.go/tasks'/status/(identity+'.json'),{'id':identity,'status':status,
            'campaign_repair':{'parent_id':'parent','campaign_id':'both'}})
    monkeypatch.setattr(delivery,'delivery_report',lambda repo,identity:{'delivered':True})
    monkeypatch.setattr(changes,'resume_repaired_parent',lambda repo,identity,actor,**kw:calls.append(identity) or {'task_id':'parent'})
    state={'campaign_id':'both','completed_tasks':['first'],'history':[],'failures':[]}
    _resume_delivered_repairs(repo,state,'owner')
    assert calls==[]
    (repo/'.go/tasks/open/second.json').unlink()
    write(repo/'.go/tasks/done/second.json',{'id':'second','status':'done','campaign_repair':{'parent_id':'parent','campaign_id':'both'}})
    state['completed_tasks'].append('second')
    _resume_delivered_repairs(repo,state,'owner')
    assert calls and state['history'][0]['task_id']=='parent'


def test_real_managed_parent_keeps_dirty_workspace_after_separate_delivered_repair(tmp_path,monkeypatch):
    from test_taskwise_no_release import setup, run
    from test_abc_worktrees import git
    from go_workflow import cli
    from go_workflow.completion import contract_digest as task_digest
    from go_workflow.delivery_closure import synchronize_closure
    from go_workflow.campaign_changes import resume_repaired_parent
    repo,workspace,capture,args,parent=setup(tmp_path,monkeypatch)
    args.max_commands=1
    assert run(repo,args,parent)[1]['status']=='budget_exhausted'
    active_path=repo/'.go/tasks/active'/f'{parent["id"]}.json'
    active=json.loads(active_path.read_text())
    cli.block_task_record(repo,repo/'.go',active_path,active,'owner','Repair prerequisite',[])
    (workspace/'app.txt').write_text('preserve my unfinished parent changes')
    repair=deepcopy(parent);repair['id']='separate-repair'
    repair['campaign_repair']={'parent_id':parent['id'],'parent_contract_sha256':task_digest(active),'campaign_id':'repair-campaign'}
    write(repo/'.go/tasks/open/separate-repair.json',repair)
    hierarchy_path=repo/'.go/hierarchy.json';hierarchy=json.loads(hierarchy_path.read_text())
    hierarchy['epics'][0]['tasks'].append(repair['id']);write(hierarchy_path,hierarchy)
    repair_args=deepcopy(args)
    repair_args.max_commands=30;repair_args.task_id=repair['id'];repair_args.workspace_path=str(tmp_path/'repair-worker')
    repair_args.workspace_branch='task/separate-repair';repair_args.run_id='repair-run'
    repair_args.base_commit=git(repo,'rev-parse','HEAD')
    code,result=run(repo,repair_args,repair)
    assert code==0 and repair['id'] in result['completed_tasks'],result
    synchronize_closure(repo,repair['id'],policy='push')
    blocked=repo/'.go/tasks/blocked'/f'{parent["id"]}.json'
    checkpoint=repo/'.go/runs'/parent['id']/'run-state.json'
    registry=repo/'.go/workspaces'/f'{parent["id"]}.json'
    before={path:path.read_bytes() for path in (blocked,checkpoint,registry,workspace/'app.txt')}
    old_base=json.loads(registry.read_text())['base_commit']
    current_base=git(repo,'rev-parse','HEAD')
    assert old_base!=current_base
    report=resume_repaired_parent(repo,repair['id'],'owner')
    assert report['resumed'] is False and report['reason']=='workspace_reconciliation_required'
    assert report['old_base']==old_base and report['current_base']==current_base and report['next_action']
    assert before=={path:path.read_bytes() for path in before}
    assert not (repo/'.go/tasks/active'/f'{parent["id"]}.json').exists()
    assert not (repo/'.go/runs/campaigns/repair-campaign/changes/resume-separate-repair.json').exists()
