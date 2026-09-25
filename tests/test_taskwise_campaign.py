"""Taskwise campaigns preserve authority while removing implicit total ceilings."""
from copy import deepcopy
from pathlib import Path
import json
from jsonschema import Draft202012Validator
from test_autonomy_contracts import sample
from go_workflow.campaign_contracts import validate_campaign_contract

ROOT = Path(__file__).resolve().parents[1]

def until_scope():
    contract = sample()
    contract['execution'] = {'schema': 'go-workflow.taskwise-execution.v1', 'mode': 'until_scope'}
    contract['authority']['budget'] = dict.fromkeys(['wall_seconds', 'max_tasks', 'max_attempts'])
    return contract

def test_until_scope_accepts_explicit_absence_of_total_limits_without_changing_legacy():
    contract = until_scope()
    assert validate_campaign_contract(contract) == []
    Draft202012Validator(json.loads((ROOT/'schemas/campaign-contract.schema.json').read_text())).validate(contract)
    old = deepcopy(contract)
    del old['execution']
    assert validate_campaign_contract(old)
    assert validate_campaign_contract(sample()) == []

def test_snapshot_includes_blocked_tasks_and_excludes_future_work(tmp_path):
    from test_autonomy_contracts import fixture, write
    from go_workflow.campaign_contracts import snapshot_task_scope
    repo, contract, task = fixture(tmp_path)
    blocked = deepcopy(task); blocked.update(id='blocked-example', status='blocked')
    write(repo/'.go/tasks/blocked/blocked-example.json', blocked)
    frozen = snapshot_task_scope(repo)
    assert set(frozen) == {task['id'], 'blocked-example'}
    later = deepcopy(task); later['id']='later'
    write(repo/'.go/tasks/open/later.json', later)
    assert 'later' not in frozen


import pytest

@pytest.mark.parametrize("explicit", [False, True])
def test_until_scope_does_not_inherit_legacy_total_command_ceiling(tmp_path, explicit):
    from argparse import Namespace
    from test_autonomy_campaign import serial_campaign_fixture, write
    from go_workflow.campaign import execute_campaign
    from go_workflow import cli
    repo, path, contract, first, second = serial_campaign_fixture(tmp_path)
    contract['execution'] = until_scope()['execution']
    contract['authority']['budget'] = until_scope()['authority']['budget']
    if explicit:
        contract['authority']['budget']['max_commands'] = 1
    write(path, contract)
    calls=[]
    def executor(control, args, mode, task, api):
        calls.append(task['id'])
        source=control/'.go/tasks/open'/f"{task['id']}.json"
        record=json.loads(source.read_text());record.update(status='done',work_status='completed',review_status='approved')
        write(control/'.go/tasks/done'/source.name, record);source.unlink()
        return 0, {'status':'done','completed_tasks':[task['id']], 'checks':[], 'commands_run':1}
    args=Namespace(campaign=str(path),previous_campaign='',campaign_workspace_root=str(tmp_path/'workspaces'),agent='fixture',max_commands=1,max_attempts=1,max_minutes=5)
    _,result=execute_campaign(repo,args,'go-auto',cli,executor)
    assert calls == ([first['id']] if explicit else [first['id'],second['id']])
    assert (result['status']=='budget_exhausted') == explicit

def test_administrative_done_without_lifecycle_proof_is_not_progress(tmp_path):
    from test_autonomy_contracts import fixture, write
    from go_workflow.campaign_contracts import proven_progress
    repo, contract, task=fixture(tmp_path)
    task.update(status='done',work_status='completed',review_status='approved')
    (repo/'.go/tasks/open'/f"{task['id']}.json").unlink()
    write(repo/'.go/tasks/done'/f"{task['id']}.json",task)
    report=proven_progress(repo,contract)
    assert report['completed']==[]
    assert report['remaining']==[task['id']]

def test_router_selects_until_scope_only_for_execution():
    from go_workflow.routing import recommend_route
    state=dict(repo_exists=True,has_go=True,has_vision=True,has_principles=True,has_hierarchy=True,valid=True,open_task_count=2)
    assert recommend_route('go','Go tot alle taken klaar',state)['execution_mode']=='until_scope'
    assert recommend_route('go','Go plan tot alle taken klaar',state).get('execution_mode')!='until_scope'

def test_explicit_go_limits_are_distinguished_from_legacy_defaults():
    from go_workflow.cli import build_parser
    parser=build_parser()
    plain=parser.parse_args(['go','.','--intent','Go tot alle taken klaar'])
    assert getattr(plain,'explicit_campaign_budget',{})=={}
    limited=parser.parse_args(['go','.','--intent','Go tot alle taken klaar','--max-minutes','12','--max-tasks','4'])
    assert limited.explicit_campaign_budget=={'wall_seconds':720,'max_tasks':4}


def test_explicit_go_shipping_and_command_limit_do_not_become_defaults():
    from go_workflow.cli import build_parser
    parser=build_parser()
    plain=parser.parse_args(['go','.'])
    assert getattr(plain,'explicit_ship_policy',None) is None
    limited=parser.parse_args(['go','.','--ship-policy','none','--max-commands','4'])
    assert limited.explicit_ship_policy=='none'
    assert limited.explicit_campaign_budget=={'max_commands':4}


def test_real_release_proof_counts_progress_but_tampering_does_not(tmp_path, monkeypatch):
    from test_abc_release import fixture, prepare, proofs
    from go_workflow.release import publish_release
    from go_workflow.worktrees import cleanup_workspace
    from go_workflow.campaign_contracts import proven_progress
    monkeypatch.chdir(tmp_path)
    repo, worker, source = fixture(tmp_path)
    prepare(repo)
    proofs(worker, source)
    publish_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    cleanup_workspace(repo, 'task-schema-smoke', 'owner', 'run-1')
    contract={'authority':{'permitted_tasks':['task-schema-smoke']}}
    assert proven_progress(repo,contract)['completed']==['task-schema-smoke']
    task=json.loads((repo/'.go/tasks/done/task-schema-smoke.json').read_text())
    proof=repo/task['completion_evidence']['verification']['path']
    proof.write_text('{}')
    assert proven_progress(repo,contract)['completed']==[]
