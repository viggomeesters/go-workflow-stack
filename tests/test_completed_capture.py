"""Completed captures release process ownership even if the local hostname changes."""
from copy import deepcopy
import os
import pytest


def completed():
    return {'controller':{'host':'previous-network-name','pid':os.getpid()},'phase':'complete',
            'execution_cwd':'/same/local/project','inflight':None,'worker_group':None}


def test_fully_closed_capture_needs_no_host_or_pid_liveness(monkeypatch):
    import go_workflow.run_state as state
    value=completed();before=deepcopy(value)
    monkeypatch.setattr(state.socket,'gethostname',lambda:'current-network-name')
    monkeypatch.setattr(state,'_pid_alive',lambda *_:pytest.fail('Closed capture queried controller liveness'))
    state.require_stopped(value)
    assert value==before


@pytest.mark.parametrize('change',[{'phase':'verify'},{'inflight':{'nonce':'pending'}},
    {'worker_group':12345},{'execution_cwd':None}])
def test_unknown_foreign_process_remains_blocked(monkeypatch,change):
    import go_workflow.run_state as state
    value=completed();value.update(change)
    if change.get('execution_cwd','exists') is None:value.pop('execution_cwd')
    monkeypatch.setattr(state.socket,'gethostname',lambda:'current-network-name')
    with pytest.raises(state.RunStateError,match='Cross-host'):state.require_stopped(value)


def test_same_host_live_controller_still_cannot_be_stolen(monkeypatch):
    import go_workflow.run_state as state
    value=completed();value.update(phase='verify');value['controller']['pid']=os.getpid()+1
    monkeypatch.setattr(state.socket,'gethostname',lambda:value['controller']['host'])
    monkeypatch.setattr(state,'_pid_alive',lambda *_:True)
    with pytest.raises(state.RunStateError,match='still live'):state.require_stopped(value)


def test_real_completed_verification_can_record_critic_after_hostname_change(tmp_path, monkeypatch):
    import json
    from test_abc_worktrees import setup_repo
    import go_workflow.run_state as state
    from go_workflow.completion import capture_verification,record_critic,bind,changed_content,content_snapshot
    monkeypatch.chdir(tmp_path)
    repo,base=setup_repo(tmp_path);path=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(path.read_text())
    task['claim']['base_commit']=base;task['verification']=['python3 -c "print(42)"'];path.write_text(json.dumps(task))
    (repo/'app.txt').write_text('verified change')
    proof,ref=capture_verification(repo,task['id'],'owner');assert proof['status']=='passed'
    checkpoint=repo/'.go/runs/task-schema-smoke/completion-state.json';before=checkpoint.read_bytes()
    monkeypatch.setattr(state.socket,'gethostname',lambda:'current-network-name')
    critic=record_critic(repo,task['id'],'owner',{'schema':'go-workflow.critic-review.v1',**bind(repo,task),
        'status':'passed','reviewer':'owner','review_mode':'same_agent','summary':'Reviewed exact candidate after completed process capture',
        'blocking_findings':[],'reviewed_paths':sorted(changed_content(repo,task,content_snapshot(repo,task)))})
    assert critic['sha256'] and checkpoint.read_bytes()==before
    assert json.loads(path.read_text())['completion_evidence']['verification']==ref
