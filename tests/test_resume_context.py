"""Current context wins over stale messages; portable delivery prevents replay."""
import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from go_workflow.resume_context import compose_resume_context
from test_taskwise_no_release import setup,run
from test_abc_worktrees import git

ROOT=Path(__file__).resolve().parents[1]


def test_context_reflects_current_phase_source_owner_and_unknown_proof(tmp_path,monkeypatch):
    repo,workspace,_,args,task=setup(tmp_path,monkeypatch)
    args.max_commands=1
    run(repo,args,task)
    before=compose_resume_context(repo,task['id'],actor='owner')
    assert before['phase']=='verify' and before['next_action']=='verify'
    assert before['owner']=='owner' and not before['completed']
    assert before['source_revision']==git(workspace,'rev-parse','HEAD')
    (workspace/'app.txt').write_text('new actual bytes')
    after=compose_resume_context(repo,task['id'],actor='owner')
    assert after['source_digest']!=before['source_digest']
    assert after['next_action']=='verify_current_source'
    Draft202012Validator(json.loads((ROOT/'schemas/resume-context.schema.json').read_text())).validate(after)


def test_new_snapshot_overrides_old_chat_resume_state(tmp_path,monkeypatch):
    from go_workflow.execution_context import create_snapshot
    repo,workspace,_,args,task=setup(tmp_path,monkeypatch)
    args.max_commands=1;run(repo,args,task)
    current=json.loads((repo/'.go/tasks/active'/f'{task["id"]}.json').read_text())
    ref=create_snapshot(workspace,current,'repair',2,'direct_fix',
        {'resume_state':{'phase':'complete','completed':True},'old_chat':'everything was done'})
    snapshot=json.loads(Path(ref['path']).read_text())
    assert snapshot['context']['resume_state']['phase']=='verify'
    assert snapshot['context']['resume_state']['completed'] is False


def test_live_foreign_process_is_a_resume_blocker_even_after_timeout(tmp_path,monkeypatch):
    repo,_,_,args,task=setup(tmp_path,monkeypatch)
    args.max_commands=1;run(repo,args,task)
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    try:
        path=repo/'.go/runs'/task['id']/'run-state.json';state=json.loads(path.read_text())
        state['controller']['pid']=child.pid
        path.write_text(json.dumps(state))
        context=compose_resume_context(repo,task['id'],actor='owner')
        assert context['next_action']=='resolve_blocker'
        assert any('still live' in item for item in context['blockers'])
    finally:
        child.terminate();child.wait(timeout=5)


def test_completed_clone_uses_portable_closure_not_old_checkout(tmp_path,monkeypatch):
    from test_taskwise_closure import published
    from go_workflow.delivery_closure import synchronize_closure
    repo,_=published(tmp_path,monkeypatch)
    task=json.loads((repo/'.go/tasks/done/task-schema-smoke.json').read_text())
    assert synchronize_closure(repo,task['id'],policy='push')['delivered']
    clone=tmp_path/'new-session'
    subprocess.run(['git','clone','-q',str(tmp_path/'remote.git'),str(clone)],check=True)
    # Bare remote HEAD can still default to master; choose the published branch.
    git(clone,'checkout','main')
    context=compose_resume_context(clone,task['id'],actor='new-session-agent')
    assert context['completed'] and context['next_action']=='none'
    assert context['blockers']==[]
    Draft202012Validator(json.loads((ROOT/'schemas/resume-context.schema.json').read_text())).validate(context)


def test_confirmed_verification_survives_pending_critic(tmp_path,monkeypatch):
    from go_workflow.completion import finalize_checks
    repo,workspace,_,args,task=setup(tmp_path,monkeypatch)
    args.max_commands=2;run(repo,args,task)
    current=json.loads((repo/'.go/tasks/active'/f'{task["id"]}.json').read_text())
    state=json.loads((repo/'.go/runs'/task['id']/'run-state.json').read_text())
    finalize_checks(workspace,current,'owner',[entry['completion_check'] for entry in state['checks']])
    context=compose_resume_context(repo,task['id'],actor='owner')
    assert context['evidence']['verification']['valid'] is True
    assert not context['completed']


def test_owner_is_enforced_even_when_checkpoint_missing(tmp_path,monkeypatch):
    repo,_,_,args,task=setup(tmp_path,monkeypatch)
    args.max_commands=1;run(repo,args,task)
    (repo/'.go/runs'/task['id']/'run-state.json').unlink()
    context=compose_resume_context(repo,task['id'],actor='another-owner')
    assert context['next_action']=='resolve_blocker'
    assert any('owner differs' in item for item in context['blockers'])


def test_done_without_closure_requests_actual_delivery_recovery(tmp_path,monkeypatch):
    repo,_,_,args,task=setup(tmp_path,monkeypatch)
    run(repo,args,task)
    context=compose_resume_context(repo,task['id'],actor='owner')
    assert not context['completed'] and context['next_action']=='recover_delivery'
    assert context['blockers']


def test_inflight_foreign_writer_does_not_authorize_reconciliation(tmp_path,monkeypatch):
    repo,_,_,args,task=setup(tmp_path,monkeypatch)
    args.max_commands=1;run(repo,args,task)
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    try:
        path=repo/'.go/runs'/task['id']/'run-state.json';state=json.loads(path.read_text())
        state['controller']['pid']=child.pid
        state['inflight']={'phase':'verify','nonce':'old','attempt':1,'started_at':0}
        path.write_text(json.dumps(state))
        assert compose_resume_context(repo,task['id'],actor='owner')['next_action']=='resolve_blocker'
    finally:
        child.terminate();child.wait(timeout=5)


def test_current_index_changes_keep_confirmed_next_phase(tmp_path,monkeypatch):
    repo,workspace,_,args,task=setup(tmp_path,monkeypatch)
    args.max_commands=2;run(repo,args,task)
    git(workspace,'add','app.txt')
    context=compose_resume_context(repo,task['id'],actor='owner')
    assert context['phase']=='verify' and context['next_action']=='verify'


def test_external_base_advance_blocks_execution_without_erasing_checks(tmp_path,monkeypatch):
    repo,workspace,capture,args,task=setup(tmp_path,monkeypatch)
    args.max_commands=2;run(repo,args,task)
    path=repo/'.go/runs'/task['id']/'run-state.json';before=json.loads(path.read_text())
    git(repo,'commit','--allow-empty','-m','External advance')
    result=run(repo,args,task)[1]
    after=json.loads(path.read_text())
    assert result['status']=='resume_gate' and 'base advanced' in result['summary']
    assert after['checks']==before['checks']
    assert capture.read_text().splitlines()==['build']


def test_unknown_liveness_gates_done_delivery_recovery(tmp_path,monkeypatch):
    repo,_,_,args,task=setup(tmp_path,monkeypatch);run(repo,args,task)
    path=repo/'.go/runs'/task['id']/'run-state.json';state=json.loads(path.read_text())
    state['phase']='release';state['controller']['host']='unknown-other-host'
    path.write_text(json.dumps(state))
    context=compose_resume_context(repo,task['id'],actor='owner')
    assert context['next_action']=='resolve_blocker'
    assert any('Cross-host' in item for item in context['blockers'])
