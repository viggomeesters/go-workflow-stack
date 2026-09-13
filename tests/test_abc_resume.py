"""Managed resume through real Git and bounded native-process fixtures."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_abc_worktrees import setup_repo, git, ROOT, CLI


def runner_fixture(tmp_path,monkeypatch):
    repo,base=setup_repo(tmp_path)
    source=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(source.read_text())
    task.update(status='open',work_status='pending',execution_mode='agent',shareable_delivery='none',
                claim={'agent':None,'claimed_at':None},acceptance=['app.txt contains built after one builder invocation'],
                verification=["python3 -c \"from pathlib import Path; assert Path('app.txt').read_text() == 'built'\""])
    target=repo/'.go/tasks/open/task-schema-smoke.json';target.write_text(json.dumps(task));source.unlink()
    git(repo,'add','.go');git(repo,'commit','-qm','open managed task');base=git(repo,'rev-parse','HEAD')
    capture=tmp_path/'phase-calls.log';monkeypatch.setenv('RESUME_PHASE_CAPTURE',str(capture))
    binary=tmp_path/'codex'
    binary.write_text('#!'+sys.executable+'''\nimport sys,json,os,pathlib
if sys.argv[1]=='app-server':
 for line in sys.stdin:
  request=json.loads(line)
  if 'id' not in request: continue
  result={} if request['method']=='initialize' else {'data':[{'id':'gpt-6-astra','model':'gpt-6-astra','supportedReasoningEfforts':[{'reasoningEffort':'high'}]}],'nextCursor':None}
  print(json.dumps({'id':request['id'],'result':result}),flush=True)
else:
 phase=os.environ['GO_HOOK']
 with pathlib.Path(os.environ['RESUME_PHASE_CAPTURE']).open('a') as output: output.write(phase+'\\n')
 if phase=='build': pathlib.Path('app.txt').write_text('built')
 message=json.dumps({'schema':'go-workflow.agent-adapter-result.v1','phase':phase,'status':'success','summary':'fixture phase passed'})
 print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':message}}))
 print(json.dumps({'type':'turn.completed'}))
''');binary.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    monkeypatch.setenv('GO_STACK_ALLOW_DEV','1')  # disposable candidate runtime fixture only
    return repo,base,tmp_path/'worker',capture


def run_managed(repo,base,workspace,commands,*,initial=False):
    args=[sys.executable,str(CLI),'auto',str(repo),'--execute','--task-id','task-schema-smoke',
          '--agent','owner','--executor-agent','codex','--max-commands',str(commands),'--max-minutes','5','--json']
    if initial:
        args+=['--workspace-path',str(workspace),'--workspace-branch','task/resume','--base-branch','main','--base-commit',base,'--run-id','resume-run']
    return subprocess.run(args,cwd=repo,text=True,capture_output=True,timeout=60)


def test_budget_after_build_resumes_same_active_task_at_verification(tmp_path,monkeypatch):
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    first=run_managed(repo,base,workspace,1,initial=True)
    assert first.returncode==0,first.stdout+first.stderr
    result=json.loads(first.stdout)
    assert result['status']=='budget_exhausted'
    state_path=repo/'.go/runs/task-schema-smoke/run-state.json'
    state=json.loads(state_path.read_text())
    assert state['phase']=='verify' and state['run_id']=='resume-run'
    assert (repo/'.go/tasks/active/task-schema-smoke.json').exists()
    second=run_managed(repo,base,workspace,10)
    assert second.returncode==0,second.stdout+second.stderr
    result=json.loads(second.stdout)
    assert result['status']=='release_pending'  # publisher is a later task; never claim done early
    assert capture.read_text().splitlines()==['build','critic']
    resumed=json.loads(state_path.read_text())
    assert resumed['run_id']==state['run_id'] and resumed['phase']=='release'
    assert resumed['checks'][0]['returncode']==0
    assert (workspace/'app.txt').read_text()=='built'


def test_resume_invalidates_changed_code_without_rebuilding_confirmed_build(tmp_path,monkeypatch):
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    (repo/'personal.txt').write_text('untouched')
    assert run_managed(repo,base,workspace,1,initial=True).returncode==0
    (workspace/'app.txt').write_text('changed after checkpoint')
    result=run_managed(repo,base,workspace,1)
    assert result.returncode==0,result.stderr
    state=json.loads((repo/'.go/runs/task-schema-smoke/run-state.json').read_text())
    assert state['phase']=='repair' and state['checks'][0]['returncode']!=0
    assert state['history'][-1]['event']=='reconciled_interruption_or_drift'
    assert capture.read_text().splitlines()==['build']
    assert (repo/'personal.txt').read_text()=='untouched'


def test_model_change_and_foreign_owner_refuse_before_worker_dispatch(tmp_path,monkeypatch):
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    assert run_managed(repo,base,workspace,1,initial=True).returncode==0
    source=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(source.read_text())
    task['execution_contract']['model']['effort']='medium';source.write_text(json.dumps(task))
    resumed=run_managed(repo,base,workspace,10)
    assert resumed.returncode!=0 and 'contract changed' in resumed.stdout
    assert capture.read_text().splitlines()==['build']


def wait_file(path,predicate=lambda value: bool(value),timeout=15):
    import time
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if path.exists():
            try:
                value=json.loads(path.read_text())
                if predicate(value):return value
            except ValueError:pass
        time.sleep(.03)
    raise AssertionError('fixture state did not become ready: '+str(path))


def test_dead_controller_does_not_authorize_takeover_of_a_live_worker(tmp_path,monkeypatch):
    import signal
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    binary=tmp_path/'codex';binary.write_text(binary.read_text().replace("if phase=='build': pathlib.Path('app.txt').write_text('built')", "if phase=='build':\n  pathlib.Path('app.txt').write_text('built')\n  if os.environ.get('FIXTURE_SLEEP'): __import__('time').sleep(40)"))
    env=os.environ.copy();env['FIXTURE_SLEEP']='1'
    argv=[sys.executable,str(CLI),'auto',str(repo),'--execute','--task-id','task-schema-smoke',
          '--agent','owner','--executor-agent','codex','--max-commands','1','--json',
          '--workspace-path',str(workspace),'--workspace-branch','task/resume','--base-branch','main',
          '--base-commit',base,'--run-id','resume-run']
    parent=subprocess.Popen(argv,cwd=repo,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    group=None
    try:
        state=wait_file(repo/'.go/runs/task-schema-smoke/run-state.json',lambda s:s.get('worker_group'))
        group=state['worker_group']
        live=run_managed(repo,base,workspace,10)
        assert live.returncode!=0 and 'live state lock' in live.stdout
        parent.kill();parent.wait(timeout=5)
        orphan=run_managed(repo,base,workspace,10)
        assert orphan.returncode!=0 and 'worker process group is still live' in orphan.stdout
        staging=subprocess.run([sys.executable,str(CLI),'workspace','stage',str(repo),'--task-id','task-schema-smoke','--owner','owner','--run-id','resume-run'],cwd=repo,text=True,capture_output=True)
        assert staging.returncode!=0 and 'worker process group is still live' in staging.stderr
        os.killpg(group,signal.SIGKILL)
        from go_workflow.run_state import group_alive
        import time
        for _ in range(100):
            if not group_alive(group):break
            time.sleep(.03)
        assert not group_alive(group)
        restored=run_managed(repo,base,workspace,10)
        assert restored.returncode==0,restored.stdout+restored.stderr
        assert json.loads(restored.stdout)['status']=='release_pending'
    finally:
        if parent.poll() is None:parent.kill();parent.wait()
        if group:
            try:os.killpg(group,signal.SIGKILL)
            except ProcessLookupError:pass


def test_effect_intent_crash_requires_exact_remote_readback_and_preserves_requirements(tmp_path,monkeypatch):
    from go_workflow.run_state import RunSession,RunStateError
    from go_workflow.state_io import atomic_json
    import socket
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    assert run_managed(repo,base,workspace,1,initial=True).returncode==0
    session=RunSession(repo,'task-schema-smoke');state=session.load()
    state['controller']={'host':socket.gethostname(),'pid':os.getpid()}
    state['requirements']=[{'id':'R1','status':'verified','evidence':['durable-proof']}]
    atomic_json(repo/'.go/runs/task-schema-smoke/run-state.json',state)
    bare=tmp_path/'remote.git';subprocess.run(['git','init','--bare','-q',str(bare)],check=True)
    effect,fresh=session.effect_intent('publish-base','push',str(bare)+'#refs/heads/main',base)
    assert fresh and effect['status']=='pending'
    git(repo,'push',str(bare),base+':refs/heads/main')  # effect succeeds, process loses its acknowledgement
    effect,fresh=session.effect_intent('publish-base','push',str(bare)+'#refs/heads/main',base)
    assert not fresh and effect['status']=='pending'  # never reissue blindly
    effect=session.reconcile_effect('publish-base',lambda intent:{'status':'confirmed','observed':git(repo,'ls-remote',str(bare),'refs/heads/main').split()[0]})
    assert effect['status']=='confirmed'
    assert session.load()['requirements']==state['requirements']
    with pytest.raises(RunStateError,match='identity changed'):
        session.effect_intent('publish-base','push',str(bare),'different')
    with pytest.raises(RunStateError,match='does not match'):
        session.reconcile_effect('publish-base',lambda intent:{'status':'confirmed','observed':'wrong'})


def test_explicit_checkout_relocation_preserves_run_and_user_files(tmp_path,monkeypatch):
    from go_workflow.run_state import relocate_run
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    assert run_managed(repo,base,workspace,1,initial=True).returncode==0
    (repo/'personal.txt').write_text('retain')
    moved=tmp_path/'moved';moved.mkdir()
    new_control=moved/'control';new_workspace=moved/'worker'
    repo.rename(new_control);workspace.rename(new_workspace)
    relocated=relocate_run(new_control,'task-schema-smoke','owner','resume-run',repo,workspace,new_workspace)
    assert relocated['control_repo']==str(new_control)
    assert relocated['run_id']=='resume-run' and relocated['phase']=='verify'
    resumed=run_managed(new_control,base,new_workspace,10)
    assert resumed.returncode==0,resumed.stdout+resumed.stderr
    assert capture.read_text().splitlines()==['build','critic']
    assert (new_control/'personal.txt').read_text()=='retain'
    assert (new_workspace/'app.txt').read_text()=='built'
    assert relocated['history'][-1]['event']=='relocated'


def test_initial_workspace_failure_has_a_retryable_setup_checkpoint(tmp_path,monkeypatch):
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    workspace.mkdir();(workspace/'foreign.txt').write_text('do not delete')
    first=run_managed(repo,base,workspace,1,initial=True)
    assert first.returncode!=0
    state=json.loads((repo/'.go/runs/task-schema-smoke/run-state.json').read_text())
    assert state['phase']=='setup'
    assert (workspace/'foreign.txt').read_text()=='do not delete'
    workspace.rename(tmp_path/'preserved foreign')
    resumed=run_managed(repo,base,workspace,1)
    assert resumed.returncode==0,resumed.stdout+resumed.stderr
    assert json.loads((repo/'.go/runs/task-schema-smoke/run-state.json').read_text())['phase']=='verify'


def test_cleanup_retry_only_cleans_the_already_delivered_task(tmp_path,monkeypatch):
    from go_workflow.worktrees import record_integration
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    assert run_managed(repo,base,workspace,10,initial=True).returncode==0
    git(workspace,'add','app.txt');git(workspace,'commit','-qm','fixture product')
    head=git(workspace,'rev-parse','HEAD');git(repo,'merge','--ff-only','task/resume')
    record_integration(repo,'task-schema-smoke','owner','resume-run',head)
    active=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(active.read_text())
    task.update(status='done',work_status='completed',review_status='approved')
    task['release_receipt']={'schema':'go-workflow.release-receipt.v1','project':task['project'],'task_id':task['id'],'status':'verified','commit':head,'evidence':['fixture local release evidence']}
    done=repo/'.go/tasks/done/task-schema-smoke.json';done.parent.mkdir(exist_ok=True);done.write_text(json.dumps(task));active.unlink()
    path=repo/'.go/runs/task-schema-smoke/run-state.json';state=json.loads(path.read_text());state['phase']='cleanup';path.write_text(json.dumps(state))
    (workspace/'untracked.txt').write_text('keep')
    blocked=run_managed(repo,base,workspace,10)
    assert blocked.returncode!=0 and workspace.exists()
    (workspace/'untracked.txt').unlink()
    resumed=run_managed(repo,base,workspace,10)
    assert resumed.returncode==0,resumed.stdout+resumed.stderr
    assert json.loads(resumed.stdout)['status']=='done' and not workspace.exists()
    assert capture.read_text().splitlines()==['build','critic']
    assert json.loads(path.read_text())['phase']=='complete'
    assert json.loads(done.read_text())['release_receipt']==task['release_receipt']


def test_state_schema_roundtrip_and_malformed_owner_fail_closed(tmp_path,monkeypatch):
    from jsonschema import Draft202012Validator
    from go_workflow.run_state import read_state,RunStateError
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    assert run_managed(repo,base,workspace,1,initial=True).returncode==0
    state=read_state(repo,'task-schema-smoke')
    schema=json.loads((ROOT/'schemas/run-state.schema.json').read_text())
    assert not list(Draft202012Validator(schema).iter_errors(state))
    path=repo/'.go/runs/task-schema-smoke/run-state.json'
    state['controller']['pid']=0;path.write_text(json.dumps(state))
    with pytest.raises(RunStateError,match='controller'):
        read_state(repo,'task-schema-smoke')


def test_relocation_recovers_after_git_repair_before_state_confirmation(tmp_path,monkeypatch):
    import go_workflow.run_state as runs
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    assert run_managed(repo,base,workspace,1,initial=True).returncode==0
    target=tmp_path/'new';target.mkdir();control=target/'control';worker=target/'worker'
    repo.rename(control);workspace.rename(worker)
    real_write=runs.atomic_json
    def fail_confirmation(path,value):
        if path.name=='run-state.json':raise OSError('fixture crash after Git repair')
        real_write(path,value)
    monkeypatch.setattr(runs,'atomic_json',fail_confirmation)
    with pytest.raises(OSError,match='fixture crash'):
        runs.relocate_run(control,'task-schema-smoke','owner','resume-run',repo,workspace,worker)
    monkeypatch.setattr(runs,'atomic_json',real_write)
    state=runs.relocate_run(control,'task-schema-smoke','owner','resume-run',repo,workspace,worker)
    assert state['phase']=='verify'
    assert runs.relocate_run(control,'task-schema-smoke','owner','resume-run',repo,workspace,worker)==state
    assert run_managed(control,base,worker,10).returncode==0


def test_cross_host_owner_is_not_assumed_dead_and_resume_keeps_exact_task(tmp_path,monkeypatch):
    repo,base,workspace,capture=runner_fixture(tmp_path,monkeypatch)
    assert run_managed(repo,base,workspace,1,initial=True).returncode==0
    resume=json.loads((repo/'.go/runs/task-schema-smoke/resume.json').read_text())
    assert resume['args'][resume['args'].index('--task-id')+1]=='task-schema-smoke'
    assert resume['runtime_required_ref']==json.loads((repo/'.go/project.json').read_text())['stack_ref']
    path=repo/'.go/runs/task-schema-smoke/run-state.json';state=json.loads(path.read_text())
    state['controller']['host']='different-host.fixture';path.write_text(json.dumps(state))
    result=run_managed(repo,base,workspace,10)
    assert result.returncode!=0 and 'Cross-host' in result.stdout
    assert capture.read_text().splitlines()==['build']
