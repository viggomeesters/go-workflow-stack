"""Worker chunk budgets preserve serial execution without manufacturing progress."""
import json

from go_workflow import cli
from go_workflow.campaign import execute_campaign, _chunk_checkpoint, _failure_record
from go_workflow.campaign_intake import materialize_until_scope
from go_workflow.run_state import execute_managed
from test_taskwise_no_release import setup, run
from test_abc_worktrees import git


def test_checkpoint_does_not_count_log_or_status_churn(tmp_path,monkeypatch):
    repo,workspace,_,args,task=setup(tmp_path,monkeypatch)
    args.max_commands=1
    assert run(repo,args,task)[1]['status']=='budget_exhausted'
    first=_chunk_checkpoint(repo,task['id'])
    assert first['verified_checks']==0
    state_path=repo/'.go/runs'/task['id']/'run-state.json'
    state=json.loads(state_path.read_text())
    state['history'].append({'event':'busy','summary':'lots of work'})
    state['phase_evidence'][-1]['result']['summary']='claimed progress'
    state_path.write_text(json.dumps(state))
    assert _chunk_checkpoint(repo,task['id'])==first
    assert run(repo,args,task)[1]['status']=='budget_exhausted'
    second=_chunk_checkpoint(repo,task['id'])
    assert second['verified_checks']==1 and second['cursor']!=first['cursor']


def test_until_scope_resumes_internal_budget_chunks_to_actual_delivery(tmp_path,monkeypatch):
    repo,workspace,capture,args,task=setup(tmp_path,monkeypatch)
    decision={'schema':'go-workflow.repo-local.event.v1','kind':'event','event':'decision.recorded',
        'created_at':'2026-09-25T00:00:00Z','task_id':task['id'],'agent':'owner',
        'data':{'decision_id':'bounded-policy','status':'accepted','decision':'Deliver original outcomes serially'}}
    ledger=repo/'.go/decisions/events.jsonl';ledger.parent.mkdir(parents=True,exist_ok=True)
    ledger.write_text(json.dumps(decision)+'\n')
    git(repo,'add','.go');git(repo,'commit','-qm','campaign policy')
    contract=materialize_until_scope(repo,intent='Go tot alle taken klaar',source_ref='user:fixture',campaign_id='chunks')
    path=tmp_path/'campaign.json';path.write_text(json.dumps(contract))
    args.campaign=str(path);args.previous_campaign='';args.campaign_workspace_root=str(tmp_path/'workers')
    args.max_commands=1
    code,result=execute_campaign(repo,args,'go-auto',cli,execute_managed)
    assert code==0 and result['status']=='goal_verified',result
    state=json.loads((repo/'.go/runs/campaigns/chunks/state.json').read_text())
    assert state['failures']==[]
    assert state['completed_tasks']==[task['id']]
    assert capture.read_text().splitlines()==['build','critic']
    assert git(repo,'rev-parse','HEAD') in git(repo,'ls-remote','origin','refs/heads/main')


def test_failure_comparison_uses_last_failure_of_same_task():
    first=_failure_record({'id':'a'},{'status':'blocked','summary':'assert expected 2 got 1'},[])
    other=_failure_record({'id':'b'},{'status':'blocked','summary':'other'},[first])
    repeated=_failure_record({'id':'a'},{'status':'blocked','summary':'assert expected 2 got 1'},[first,other])
    assert repeated['repeat_count']==2


def test_final_critic_is_not_replaced_by_recovery_diagnosis(tmp_path,monkeypatch):
    captured=[]
    monkeypatch.setattr(cli,'registered_workspace',lambda repo:None)
    monkeypatch.setattr(cli,'repair_agent_available',lambda agent:{'available':True,'compatible':True,'prompt_flag':'-z'})
    monkeypatch.setattr(cli,'phase_model',lambda task,phase:{'id':'fixture','effort':'high'})
    monkeypatch.setattr(cli,'native_agent_command',lambda agent,phase,prompt,**kwargs: captured.append(prompt) or 'fixture')
    monkeypatch.setattr(cli,'run_hook_command',lambda *args,**kwargs:{'stdout':'ok'})
    for strategy in ('direct_fix','minimal_reproduction','recovery_diagnosis'):
        cli.run_default_critic_agent(tmp_path,'codex',{'id':'t'},1,strategy,5,
            feedback={'recovery':{'kind':'recovery_diagnosis'}},publication_pending=True)
    assert all('blocking critic' in prompt and 'current scoped candidate' in prompt for prompt in captured[:2])
    assert 'recovery_plan' in captured[2] and 'independent read-only recovery critic' in captured[2]


def _campaign_for(repo,args,task,tmp_path):
    ledger=repo/'.go/decisions/events.jsonl'
    ledger.parent.mkdir(parents=True,exist_ok=True)
    ledger.write_text(json.dumps({'schema':'go-workflow.repo-local.event.v1','kind':'event','event':'decision.recorded',
        'created_at':'2026-09-25T00:00:00Z','task_id':task['id'],'agent':'owner',
        'data':{'decision_id':'bounded-policy','status':'accepted','decision':'Deliver requested outcomes'}})+'\n')
    git(repo,'add','.go');git(repo,'commit','-qm','campaign recovery policy')
    contract=materialize_until_scope(repo,intent='Go tot alle taken klaar',source_ref='user:fixture',campaign_id='recovery')
    path=repo/'.go/recovery-campaign.json';path.write_text(json.dumps(contract))
    args.campaign=str(path);args.previous_campaign='';args.campaign_workspace_root=str(tmp_path/'workers')
    return path


def test_recovery_diagnosis_survives_single_command_campaign_chunks(tmp_path,monkeypatch):
    from test_taskwise_recovery import managed_case
    repo,_,args,task,seen,_=managed_case(tmp_path,monkeypatch)
    _campaign_for(repo,args,task,tmp_path)
    args.max_commands=1
    code,result=execute_campaign(repo,args,'go-auto',cli,execute_managed)
    assert code==0 and result['status']=='goal_verified',result
    assert seen==['direct_fix','recovery_diagnosis','minimal_reproduction']


def test_blocking_first_invocation_preserves_actual_owner_and_proof(tmp_path,monkeypatch):
    from test_taskwise_recovery import managed_case
    repo,_,args,task,_,_=managed_case(tmp_path,monkeypatch,valid_diagnosis=False)
    _campaign_for(repo,args,task,tmp_path)
    result=execute_campaign(repo,args,'go-auto',cli,execute_managed)[1]
    assert not result['goal_verified']
    blocked=json.loads((repo/'.go/tasks/blocked'/f"{task['id']}.json").read_text())
    assert blocked['claim']['agent']=='owner'
    state=json.loads((repo/'.go/runs'/task['id']/'run-state.json').read_text())
    assert state['phase_evidence'] and state['owner']==blocked['claim']['agent']
