"""Progress gates preserve scope/authority and never invent task completion."""
from copy import deepcopy
import json
import sys

import pytest
from go_workflow.campaign_progress import ProgressSession, ProgressError, validate_progress
from go_workflow.campaign_intake import materialize_until_scope
from go_workflow.campaign_changes import configure_progress
from go_workflow.campaign_contracts import contract_digest
from test_campaign_changes import fixture, write
from test_campaign_intake import setup


def transport(tmp_path):
    return {'schema':'go-workflow.progress-transport.v1','command':[sys.executable,'-m','go_workflow.progress_transport','record','--path',str(tmp_path/'chat.json')],'timeout_seconds':10}


def test_missing_transport_blocks_before_task_claim(tmp_path):
    repo,task=setup(tmp_path)
    contract=materialize_until_scope(repo,intent='Go tot alle taken klaar',source_ref='user:test',campaign_id='all')
    before=(repo/'.go/tasks/open'/f"{task['id']}.json").read_bytes()
    with pytest.raises(ProgressError,match='No independent progress transport'):
        with ProgressSession(repo,contract,'owner'):pass
    assert (repo/'.go/tasks/open'/f"{task['id']}.json").read_bytes()==before


def test_explicit_progress_revision_preserves_frozen_authority_and_checkpoint(tmp_path):
    repo,path,_,_,evidence,state=fixture(tmp_path)
    old=json.loads(path.read_text())
    old['execution']={'schema':'go-workflow.taskwise-execution.v1','mode':'until_scope'}
    write(path,old)
    from go_workflow.campaign import _new_state
    from argparse import Namespace
    updated=_new_state(repo,old,path,tmp_path/'workers',Namespace(agent='owner',max_commands=20,max_attempts=5))
    updated['resources']=state['resources'];updated['failures']=state['failures']
    statepath=repo/'.go/runs/campaigns/bounded/state.json';write(statepath,updated)
    args=dict(change_id='enable-progress',transport=transport(tmp_path),owner='owner',reason='Explicit new transport selection',evidence=evidence)
    result=configure_progress(repo,path,**args)
    new=json.loads(path.read_text());after=json.loads(statepath.read_text())
    assert new['authority']==old['authority']
    assert after['resources']==updated['resources'] and after['failures']==updated['failures']
    assert after['contract']['sha256']==contract_digest(new)
    assert configure_progress(repo,path,**args)==result


def test_legacy_contract_never_silently_gains_progress_or_shipping(tmp_path):
    repo,path,*_=fixture(tmp_path)
    contract=json.loads(path.read_text());before=deepcopy(contract)
    with ProgressSession(repo,contract,'owner') as session:
        assert not session.enabled
    assert contract==before
    assert not (repo/'.go/runs/progress').exists()


def test_dead_independent_watcher_prevents_next_task(tmp_path):
    repo,task=setup(tmp_path)
    contract=materialize_until_scope(repo,intent='Go tot alle taken klaar',source_ref='user:test',campaign_id='all')
    contract['execution']['progress']['transport']=transport(tmp_path)
    with ProgressSession(repo,contract,'owner') as session:
        class Dead:
            process=type('Process',(),{'poll':lambda self:1})()
            def stop(self):pass
        session.watcher=Dead()
        with pytest.raises(ProgressError,match='watcher stopped'):session.drain()


@pytest.mark.parametrize('value',[{},None,{'schema':'go-workflow.campaign-progress.v1','heartbeat_seconds':5,'transport':None}])
def test_invalid_contract_rejected(value):
    assert validate_progress(value)


def test_control_requests_do_not_depend_on_chat_transport(tmp_path):
    repo,task=setup(tmp_path)
    contract=materialize_until_scope(repo,intent='Go tot alle taken klaar',source_ref='user:test',campaign_id='all')
    with ProgressSession(repo,contract,'owner',control_only=True) as session:
        assert not session.enabled
