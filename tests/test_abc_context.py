"""Durable context and fresh-process handoff fixtures; no model API calls."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_abc_worktrees import setup_repo, create, git, invoke, ROOT


def workspace_fixture(tmp_path):
    control,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    result=create(control,base,workspace);assert result.returncode==0,result.stderr
    task=json.loads((control/'.go/tasks/active/task-schema-smoke.json').read_text())
    return control,workspace,task


def test_snapshot_is_unique_and_refuses_changed_code_or_canonical_state(tmp_path):
    from go_workflow.execution_context import create_snapshot, verify_snapshot, ContextError
    control,workspace,task=workspace_fixture(tmp_path)
    (workspace/'app.txt').write_text('new file')
    first=create_snapshot(workspace,task,'build',1,'direct',{'task':task})
    second=create_snapshot(workspace,task,'build',1,'direct',{'task':task})
    assert first['path']!=second['path']
    snapshot=verify_snapshot(workspace,task['id'],first['path'],first['sha256'])
    assert snapshot['phase']=='build' and snapshot['workspace']['run_id']=='run-1'
    assert snapshot['git']['files']['app.txt']['sha256']
    (workspace/'app.txt').write_text('changed after snapshot')
    with pytest.raises(ContextError,match='Git|workspace'): verify_snapshot(workspace,task['id'],first['path'],first['sha256'])
    (workspace/'app.txt').write_text('new file')
    path=control/'.go/tasks/active/task-schema-smoke.json';task['summary']='changed scope contract';path.write_text(json.dumps(task))
    with pytest.raises(ContextError,match='state|task'): verify_snapshot(workspace,task['id'],first['path'],first['sha256'])


def test_repair_snapshot_preserves_raw_feedback_and_detects_tampering(tmp_path):
    from go_workflow.execution_context import create_snapshot, verify_snapshot, ContextError
    control,workspace,task=workspace_fixture(tmp_path)
    feedback={'checks':[{'command':'test','returncode':1,'stdout':'expected 2 got 1'}],
              'critic':{'blocking_findings':['new file fails requirement R1']},'remaining_steps':['repair','verify','critic','ship']}
    ref=create_snapshot(workspace,task,'repair',2,'retry',{'task':task},feedback=feedback)
    snapshot=verify_snapshot(workspace,task['id'],ref['path'],ref['sha256'])
    assert snapshot['feedback']==feedback
    assert Path(ref['path']).is_relative_to(control/'.go')
    Path(ref['path']).write_text('{}')
    with pytest.raises(ContextError,match='hash'): verify_snapshot(workspace,task['id'],ref['path'],ref['sha256'])


def test_managed_native_phase_uses_bounded_references_and_bootstrap_verification(tmp_path, monkeypatch):
    from go_workflow.cli import run_hook_command
    control,workspace,task=workspace_fixture(tmp_path)
    monkeypatch.setattr('go_workflow.cli.controlled_preflight',lambda *args: None)
    vision=control/'.go/vision.json';data=json.loads(vision.read_text());data['north_star']='large context '*200000;vision.write_text(json.dumps(data))
    worker=tmp_path/'fresh_worker.py'
    worker.write_text('''import json,os,pathlib
request=json.loads(os.environ['GO_ADAPTER_REQUEST_JSON'])
ref=request['context_ref']
snapshot=json.loads(pathlib.Path(ref['path']).read_text())
pathlib.Path('app.txt').write_text(json.dumps({'context_size':len(snapshot['context']['vision']['north_star']),'env_size':sum(len(os.environ[k]) for k in ['GO_CONTEXT_JSON','GO_TASK_JSON','GO_ADAPTER_REQUEST_JSON']),'feedback':snapshot['feedback']}))
print(json.dumps({'schema':'go-workflow.agent-adapter-result.v1','phase':'repair','status':'success','summary':'fresh worker read snapshot'}))
''')
    import shlex
    feedback={'checks':[{'returncode':1,'stdout':'current failed test'}],'critic':{'blocking_findings':['R1 missing']}}
    result=run_hook_command(workspace,shlex.join([sys.executable,str(worker)]),task,1,'retry','repair',require_protocol=True,feedback=feedback)
    assert result['returncode']==0,result
    observed=json.loads((workspace/'app.txt').read_text())
    assert observed['context_size']>2_000_000 and observed['env_size']<8000
    assert observed['feedback']==feedback
    assert result['context_ref']['sha256']


def test_child_bootstrap_rejects_drift_between_snapshot_and_worker_start(tmp_path,monkeypatch):
    import go_workflow.cli as cli
    control,workspace,task=workspace_fixture(tmp_path)
    monkeypatch.setattr(cli,'controlled_preflight',lambda *args: None)
    original=cli.run_shell_with_timeout
    def drift(repo,command,env,timeout):
        (workspace/'app.txt').write_text('concurrent change before worker')
        return original(repo,command,env,timeout)
    monkeypatch.setattr(cli,'run_shell_with_timeout',drift)
    result=cli.run_hook_command(workspace,'touch WORKER_STARTED',task,1,'direct','build',require_protocol=True)
    assert result['returncode']!=0 and not (workspace/'WORKER_STARTED').exists()
    assert 'state changed' in result['stderr']


def test_selected_phase_skills_and_raw_new_file_evidence_are_verified(tmp_path):
    from go_workflow.execution_context import create_snapshot, verify_snapshot, ContextError
    control,workspace,task=workspace_fixture(tmp_path)
    (workspace/'build-skill.md').write_text('Build instructions')
    (workspace/'repair-skill.md').write_text('Repair instructions')
    (workspace/'notes.md').write_text('Supplementary explanation')
    task.update(skill_files={'build':['build-skill.md'],'repair':['repair-skill.md']},notepad_path='notes.md')
    (control/'.go/tasks/active/task-schema-smoke.json').write_text(json.dumps(task))
    (workspace/'app.txt').write_bytes(b'new binary\x00content')
    ref=create_snapshot(workspace,task,'repair',2,'retry',{})
    snapshot=verify_snapshot(workspace,task['id'],ref['path'],ref['sha256'])
    names=[Path(item['path']).name for item in snapshot['selected_files']]
    assert 'repair-skill.md' in names and 'build-skill.md' not in names
    assert snapshot['notepad_policy'].startswith('Supplementary')
    raw=Path(snapshot['raw_evidence']['files']['app.txt']['path'])
    assert raw.read_bytes()==b'new binary\x00content'
    raw.write_bytes(b'corrupted')
    with pytest.raises(ContextError,match='evidence hash'): verify_snapshot(workspace,task['id'],ref['path'],ref['sha256'])


def test_context_detects_index_only_changes_and_rejects_foreign_references(tmp_path):
    from go_workflow.execution_context import create_snapshot, verify_snapshot, ContextError
    control,workspace,task=workspace_fixture(tmp_path)
    (workspace/'app.txt').write_text('staged content');git(workspace,'add','app.txt')
    (workspace/'app.txt').write_text('working content')
    ref=create_snapshot(workspace,task,'build',1,'direct',{})
    git(workspace,'add','app.txt')
    with pytest.raises(ContextError,match='Git'): verify_snapshot(workspace,task['id'],ref['path'],ref['sha256'])
    task['skill_files']=['../outside/SKILL.md']
    (control/'.go/tasks/active/task-schema-smoke.json').write_text(json.dumps(task))
    with pytest.raises(ContextError,match='inside'): create_snapshot(workspace,task,'build',1,'direct',{})


def test_native_model_control_and_context_bootstrap_work_together(tmp_path,monkeypatch):
    from test_abc_models import catalog_codex
    from go_workflow.cli import run_hook_command, default_executor_agent_command
    control,workspace,task=workspace_fixture(tmp_path)
    capture=catalog_codex(tmp_path,monkeypatch)
    result=run_hook_command(workspace,default_executor_agent_command('codex',task),task,1,'direct','build',require_protocol=True)
    assert result['returncode']==0,result
    assert capture.exists() and result['context_ref']['sha256']
    assert result['model_selection']['requested']=={'id':'gpt-6-astra','effort':'high'}
    assert result['model_selection']['effective'] is None


def test_attempt_artifacts_do_not_overwrite_prior_managed_attempts(tmp_path):
    from go_workflow.cli import record_attempt
    control,workspace,task=workspace_fixture(tmp_path)
    attempt={'task_id':task['id'],'attempt':1,'strategy':'direct','verify':{'status':'failed'},'critic':{'blocking_findings':['first finding']}}
    record_attempt(workspace,control/'.go',task,'owner',attempt,[])
    first=dict(attempt['artifacts'])
    first_file=Path(first['critic'])
    assert first_file.is_absolute() and 'first finding' in first_file.read_text()
    attempt['critic']['blocking_findings']=['second finding']
    record_attempt(workspace,control/'.go',task,'owner',attempt,[])
    assert attempt['artifacts']['critic']!=first['critic']
    assert 'first finding' in first_file.read_text()


def test_context_and_request_schemas_cover_snapshot_reference_and_reject_invalid_identity(tmp_path):
    from jsonschema import Draft202012Validator
    from go_workflow.execution_context import create_snapshot
    from go_workflow.adapter_protocol import build_adapter_request
    control,workspace,task=workspace_fixture(tmp_path)
    ref=create_snapshot(workspace,task,'build',1,'direct',{})
    snapshot=json.loads(Path(ref['path']).read_text())
    validator=Draft202012Validator(json.loads((ROOT/'schemas/execution-context.schema.json').read_text()))
    assert not list(validator.iter_errors(snapshot))
    snapshot['attempt']=0
    assert list(validator.iter_errors(snapshot))
    request=build_adapter_request(workspace,task,{},'build',1,'direct',context_ref=ref)
    request_validator=Draft202012Validator(json.loads((ROOT/'schemas/agent-adapter-request.schema.json').read_text()))
    assert not list(request_validator.iter_errors(request))
    request['context_ref']['sha256']='wrong'
    assert list(request_validator.iter_errors(request))


def test_feedback_file_changes_are_rejected_before_repair(tmp_path):
    import hashlib
    from go_workflow.execution_context import create_snapshot, verify_snapshot, ContextError
    control,workspace,task=workspace_fixture(tmp_path)
    proof=control/'.go/evidence/failed-check.log';proof.write_text('actual failing output')
    feedback={'evidence_refs':[{'path':str(proof),'sha256':hashlib.sha256(proof.read_bytes()).hexdigest()}]}
    ref=create_snapshot(workspace,task,'repair',1,'retry',{},feedback=feedback)
    proof.write_text('substituted output')
    with pytest.raises(ContextError,match='feedback'): verify_snapshot(workspace,task['id'],ref['path'],ref['sha256'])


def test_context_checks_tracked_bytes_even_when_git_index_hides_changes(tmp_path):
    from go_workflow.execution_context import create_snapshot, verify_snapshot, ContextError
    control,base=setup_repo(tmp_path);(control/'app.txt').write_text('baseline')
    git(control,'add','app.txt');git(control,'commit','-qm','baseline file');base=git(control,'rev-parse','HEAD')
    workspace=tmp_path/'worker';assert create(control,base,workspace).returncode==0
    task=json.loads((control/'.go/tasks/active/task-schema-smoke.json').read_text())
    git(workspace,'update-index','--assume-unchanged','app.txt')
    ref=create_snapshot(workspace,task,'build',1,'direct',{})
    (workspace/'app.txt').write_text('hidden modification')
    with pytest.raises(ContextError,match='Git'): verify_snapshot(workspace,task['id'],ref['path'],ref['sha256'])


@pytest.mark.parametrize('flag',['--assume-unchanged','--skip-worktree'])
def test_cleanup_preserves_tracked_changes_hidden_by_index_flags(tmp_path,flag):
    from go_workflow.worktrees import record_integration, cleanup_workspace, WorkspaceError
    control,base=setup_repo(tmp_path);(control/'app.txt').write_text('baseline')
    git(control,'add','app.txt');git(control,'commit','-qm','baseline file');base=git(control,'rev-parse','HEAD')
    workspace=tmp_path/'worker';assert create(control,base,workspace).returncode==0
    record_integration(control,'task-schema-smoke','owner','run-1',base)
    path=control/'.go/tasks/active/task-schema-smoke.json';task=json.loads(path.read_text())
    task.update(status='done',work_status='completed',review_status='approved')
    task['execution_contract']['release']={'mode':'none','reason':'fixture no-op'}
    done=control/'.go/tasks/done/task-schema-smoke.json';done.parent.mkdir();done.write_text(json.dumps(task));path.unlink()
    git(workspace,'update-index',flag,'app.txt');(workspace/'app.txt').write_text('hidden user work')
    with pytest.raises(WorkspaceError,match='index'): cleanup_workspace(control,task['id'],'owner','run-1')
    assert (workspace/'app.txt').read_text()=='hidden user work'


def test_legacy_adapter_keeps_inline_context_without_inheriting_foreign_snapshot(tmp_path,monkeypatch):
    import shlex
    from test_abc_models import controlled_fixture
    from go_workflow.cli import run_hook_command
    repo=controlled_fixture(tmp_path);task={'id':'legacy','execution_mode':'mechanical'}
    monkeypatch.setenv('GO_CONTEXT_PATH','/foreign/context.json')
    monkeypatch.setenv('GO_CONTEXT_SHA256','not-this-task')
    worker=tmp_path/'legacy_worker.py'
    worker.write_text("import os,json,pathlib\npathlib.Path('capture.json').write_text(json.dumps({'path':os.getenv('GO_CONTEXT_PATH'),'context':json.loads(os.environ['GO_CONTEXT_JSON']),'task':json.loads(os.environ['GO_TASK_JSON'])}))\n")
    result=run_hook_command(repo,shlex.join([sys.executable,str(worker)]),task,1,'direct','build')
    assert result['returncode']==0,result
    captured=json.loads((repo/'capture.json').read_text())
    assert captured['path'] is None
    assert captured['context']['task']==task and captured['task']==task
