"""Real Git fixtures for task workspace ownership and lifecycle safety."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CLI=ROOT/'cli/go.py'


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()


def invoke(repo,*args):
    return subprocess.run([sys.executable,str(CLI),*map(str,args)],cwd=repo,text=True,capture_output=True)


def setup_repo(tmp_path):
    repo=tmp_path/'control repo';shutil.copytree(ROOT/'fixtures/minimal',repo)
    subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
    git(repo,'config','user.name','Fixture');git(repo,'config','user.email','fixture@example.com')
    path=repo/'.go/tasks/open/task-schema-smoke.json';t=json.loads(path.read_text())
    t.update(status='active',claim={'agent':'owner','claimed_at':'2026-09-13T00:00:00Z'},scope={'read':['.go/**'],'modify':['app.txt']})
    t['execution_contract']={'schema':'go-workflow.execution-contract.v1','task_kind':'product','model':{'id':'gpt-6-astra','effort':'high'},'release':{'mode':'required'},'workspace':{'mode':'task_worktree','base_branch':'main','control_state':'repo_local_single_writer'}}
    target=repo/'.go/tasks/active/task-schema-smoke.json';target.parent.mkdir(parents=True);target.write_text(json.dumps(t));path.unlink()
    git(repo,'add','.');git(repo,'commit','-qm','fixture')
    return repo,git(repo,'rev-parse','HEAD')


def create(repo,base,path,owner='owner'):
    return invoke(repo,'workspace','create',repo,'--task-id','task-schema-smoke','--owner',owner,'--run-id','run-1','--path',path,'--branch','task/smoke','--base-branch','main','--base-commit',base,'--json')


def test_create_and_reuse_one_owned_worktree_without_copying_user_dirt(tmp_path):
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'task workspace'
    (repo/'personal.txt').write_text('unrelated user work')
    first=create(repo,base,workspace)
    assert first.returncode==0, first.stdout+first.stderr
    assert (workspace/'.git').is_file()
    assert not (workspace/'personal.txt').exists()
    second=create(repo,base,workspace)
    assert second.returncode==0, second.stdout+second.stderr
    assert json.loads(first.stdout)['generation']==json.loads(second.stdout)['generation']
    assert git(repo,'worktree','list','--porcelain').count('worktree ')==2
    assert (repo/'personal.txt').read_text()=='unrelated user work'


def test_registered_worktree_reads_canonical_task_state(tmp_path):
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    result=create(repo,base,workspace);assert result.returncode==0,result.stderr
    active=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(active.read_text())
    task.update(status='done',work_status='completed',review_status='approved')
    done=repo/'.go/tasks/done/task-schema-smoke.json';done.parent.mkdir();done.write_text(json.dumps(task));active.unlink()
    result=invoke(workspace,'status',workspace,'--json')
    assert result.returncode==0,result.stdout+result.stderr
    status=json.loads(result.stdout)
    assert status['tasks']['active']==0
    assert status['tasks']['done']==1
    assert (workspace/'.go/tasks/active/task-schema-smoke.json').exists()  # copied data is inert


def test_worker_cannot_create_an_independent_queue_or_mutate_other_tasks(tmp_path):
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    assert create(repo,base,workspace).returncode==0
    result=invoke(workspace,'task','create',workspace,'--summary','Stray queue')
    assert result.returncode!=0
    assert 'control checkout' in result.stderr
    assert not list((repo/'.go/tasks/open').glob('*stray*'))


def test_git_common_directory_serializes_state_writers(tmp_path):
    from go_workflow.state_io import repository_lock, StateLockError
    import pytest
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    assert create(repo,base,workspace).returncode==0
    with repository_lock(repo/'.go','shared-test'):
        with pytest.raises(StateLockError):
            with repository_lock(workspace/'.go','shared-test',timeout_seconds=0.05): pass


def test_stage_checks_committed_and_untracked_scope_before_touching_index(tmp_path):
    from go_workflow.worktrees import stage_workspace, WorkspaceError
    import pytest
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    record=json.loads(create(repo,base,workspace).stdout)
    (workspace/'foreign.txt').write_text('committed violation')
    git(workspace,'add','foreign.txt');git(workspace,'commit','-qm','outside scope')
    (workspace/'app.txt').write_text('allowed')
    with pytest.raises(WorkspaceError,match='scope'): stage_workspace(repo,record['task_id'],'owner','run-1')
    assert git(workspace,'diff','--cached','--name-only')==''
    assert (workspace/'app.txt').exists()


def test_execution_and_integration_leases_exclude_competing_writers(tmp_path):
    from go_workflow.worktrees import execution_lease, integration_slot, WorkspaceError
    from go_workflow.state_io import StateLockError
    import pytest
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    assert create(repo,base,workspace).returncode==0
    with execution_lease(workspace,'task-schema-smoke','owner','run-1'):
        with pytest.raises(StateLockError):
            with execution_lease(workspace,'task-schema-smoke','owner','run-1',timeout_seconds=0.05): pass
    with integration_slot(repo,'task-schema-smoke','owner','run-1') as candidate:
        assert candidate['base_commit']==base
        with pytest.raises(StateLockError):
            with integration_slot(repo,'task-schema-smoke','owner','run-1',timeout_seconds=0.05): pass
    git(repo,'commit','--allow-empty','-qm','concurrent base advance')
    with pytest.raises(WorkspaceError,match='Base branch'):
        with integration_slot(repo,'task-schema-smoke','owner','run-1'): pass
    assert workspace.exists()


def test_cleanup_preserves_work_until_integration_completion_and_release(tmp_path):
    from go_workflow.worktrees import cleanup_workspace, record_integration, WorkspaceError
    import pytest
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    assert create(repo,base,workspace).returncode==0
    (workspace/'app.txt').write_text('delivered')
    git(workspace,'add','app.txt');git(workspace,'commit','-qm','product')
    head=git(workspace,'rev-parse','HEAD')
    with pytest.raises(WorkspaceError): cleanup_workspace(repo,'task-schema-smoke','owner','run-1')
    git(repo,'merge','--ff-only','task/smoke')
    record_integration(repo,'task-schema-smoke','owner','run-1',head)
    with pytest.raises(WorkspaceError,match='completion'): cleanup_workspace(repo,'task-schema-smoke','owner','run-1')
    active=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(active.read_text())
    task.update(status='done',work_status='completed',review_status='approved')
    done=repo/'.go/tasks/done/task-schema-smoke.json';done.parent.mkdir();done.write_text(json.dumps(task));active.unlink()
    with pytest.raises(WorkspaceError,match='release'): cleanup_workspace(repo,'task-schema-smoke','owner','run-1')
    task['release_receipt']={'schema':'go-workflow.release-receipt.v1','project':task['project'],'task_id':task['id'],'status':'verified','commit':head,'evidence':['fixture local release evidence']}
    done.write_text(json.dumps(task))
    (workspace/'untracked.txt').write_text('preserve')
    with pytest.raises(WorkspaceError,match='dirty'): cleanup_workspace(repo,'task-schema-smoke','owner','run-1')
    assert (workspace/'untracked.txt').read_text()=='preserve'
    (workspace/'untracked.txt').unlink()
    record=cleanup_workspace(repo,'task-schema-smoke','owner','run-1')
    assert record['state']=='cleaned' and not workspace.exists()
    assert cleanup_workspace(repo,'task-schema-smoke','owner','run-1')['state']=='cleaned'
    assert (repo/'app.txt').read_text()=='delivered'


def test_worker_phase_requires_registered_workspace_and_uses_canonical_context(tmp_path, monkeypatch):
    from go_workflow.cli import run_hook_command
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    task=json.loads((repo/'.go/tasks/active/task-schema-smoke.json').read_text())
    # Model enforcement has separate subprocess fixtures; this isolates workspace dispatch.
    monkeypatch.setattr('go_workflow.cli.controlled_preflight', lambda *args: None)
    task['summary']='fresh canonical context'
    (repo/'.go/tasks/active/task-schema-smoke.json').write_text(json.dumps(task))
    result=run_hook_command(repo,'touch SHOULD_NOT_EXIST',task,1,'direct','build')
    assert result['status']=='blocked' and not (repo/'SHOULD_NOT_EXIST').exists()
    assert create(repo,base,workspace).returncode==0
    command='printf "%s" "$GO_CONTEXT_JSON" > app.txt'
    result=run_hook_command(workspace,command,task,1,'direct','build')
    assert result['returncode']==0,result
    context=json.loads((workspace/'app.txt').read_text())
    assert context['task']['summary']=='fresh canonical context'


def test_creation_recovers_known_interruption_but_rejects_foreign_paths_and_owners(tmp_path):
    from go_workflow.worktrees import marker_path
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    workspace.mkdir();(workspace/'foreign').write_text('keep')
    assert create(repo,base,workspace).returncode!=0
    assert (workspace/'foreign').read_text()=='keep'
    (workspace/'foreign').unlink();workspace.rmdir()
    first=json.loads(create(repo,base,workspace).stdout)
    registry=repo/'.go/workspaces/task-schema-smoke.json'
    interrupted=dict(first,state='creating');registry.write_text(json.dumps(interrupted));marker_path(workspace).unlink()
    recovered=create(repo,base,workspace)
    assert recovered.returncode==0,recovered.stderr
    assert json.loads(recovered.stdout)['generation']==first['generation']
    assert create(repo,base,workspace,owner='other').returncode!=0
    registry.write_text(json.dumps(interrupted));marker_path(workspace).unlink()
    (workspace/'app.txt').write_text('unexpected work')
    assert create(repo,base,workspace).returncode!=0
    assert (workspace/'app.txt').read_text()=='unexpected work'


def test_concurrent_creation_and_claim_rebinding_preserve_one_workspace(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from go_workflow.worktrees import rebind_workspace, execution_lease, WorkspaceError
    import pytest
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _: create(repo,base,workspace),range(2)))
    assert all(r.returncode==0 for r in results),[r.stderr for r in results]
    assert len({json.loads(r.stdout)['generation'] for r in results})==1
    with pytest.raises(WorkspaceError): rebind_workspace(repo,'task-schema-smoke','owner','run-1','next-owner','run-2')
    path=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(path.read_text())
    task['claim']['agent']='next-owner';path.write_text(json.dumps(task))
    record=rebind_workspace(repo,'task-schema-smoke','owner','run-1','next-owner','run-2')
    assert record['path']==str(workspace.resolve())
    with pytest.raises(WorkspaceError):
        with execution_lease(workspace,'task-schema-smoke','owner','run-1'): pass
    with execution_lease(workspace,'task-schema-smoke','next-owner','run-2'): pass


def test_scope_stages_literal_names_but_refuses_copied_runtime_state(tmp_path):
    from go_workflow.worktrees import stage_workspace, WorkspaceError
    import pytest
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    path=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(path.read_text())
    task['scope']['modify']=[':(glob)*','.go/**'];path.write_text(json.dumps(task))
    assert create(repo,base,workspace).returncode==0
    (workspace/':(glob)*').write_text('literal magic-looking name')
    stage_workspace(repo,'task-schema-smoke','owner','run-1')
    assert git(workspace,'diff','--cached','--name-only')==':(glob)*'
    (workspace/'.go/tasks/active/task-schema-smoke.json').write_text(json.dumps(task))
    with pytest.raises(WorkspaceError,match='scope'): stage_workspace(repo,'task-schema-smoke','owner','run-1')


def test_missing_marker_does_not_reactivate_the_copied_queue(tmp_path):
    from go_workflow.worktrees import marker_path
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    assert create(repo,base,workspace).returncode==0
    marker_path(workspace).unlink()
    result=invoke(workspace,'status',workspace,'--json')
    assert result.returncode!=0
    assert 'recover' in result.stderr.lower()


def test_workspace_schema_and_repo_validation_reject_malformed_registry(tmp_path):
    from jsonschema import Draft202012Validator
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    result=create(repo,base,workspace);assert result.returncode==0,result.stderr
    record=json.loads(result.stdout)
    schema=json.loads((ROOT/'schemas/workspace-state.schema.json').read_text())
    validator=Draft202012Validator(schema)
    assert not list(validator.iter_errors(record))
    record['integration']={'workspace_head':'bad','integrated_commit':base}
    assert list(validator.iter_errors(record))
    (repo/'.go/workspaces/task-schema-smoke.json').write_text(json.dumps(record))
    result=invoke(repo,'validate',repo)
    assert result.returncode!=0 and 'workspace' in (result.stderr+result.stdout).lower()


def test_cleanup_failure_has_recovery_without_republishing_and_preserves_ignored_files(tmp_path):
    from go_workflow.worktrees import cleanup_workspace, record_integration, WorkspaceError
    import pytest
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    assert create(repo,base,workspace).returncode==0
    record_integration(repo,'task-schema-smoke','owner','run-1',base)
    active=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(active.read_text())
    task.update(status='done',work_status='completed',review_status='approved')
    task['execution_contract']['release']={'mode':'none','reason':'fixture no-op with explicit policy'}
    done=repo/'.go/tasks/done/task-schema-smoke.json';done.parent.mkdir();done.write_text(json.dumps(task));active.unlink()
    git(repo,'worktree','lock',str(workspace))
    with pytest.raises(WorkspaceError): cleanup_workspace(repo,'task-schema-smoke','owner','run-1')
    record=json.loads((repo/'.go/workspaces/task-schema-smoke.json').read_text())
    assert record['state']=='cleanup_failed' and 'do not republish' in record['recovery_action']
    assert workspace.exists() and json.loads(done.read_text())==task
    git(repo,'worktree','unlock',str(workspace))
    # Ignore rules in the shared Git directory are not part of the task diff.
    (repo/'.git/info/exclude').write_text('ignored.secret\n')
    (workspace/'ignored.secret').write_text('retain private work')
    with pytest.raises(WorkspaceError,match='dirty'): cleanup_workspace(repo,'task-schema-smoke','owner','run-1')
    (workspace/'ignored.secret').unlink()
    assert cleanup_workspace(repo,'task-schema-smoke','owner','run-1')['state']=='cleaned'


def test_worker_outcome_targets_only_its_active_owned_canonical_task(tmp_path):
    repo,base=setup_repo(tmp_path);workspace=tmp_path/'worker'
    path=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(path.read_text())
    task.update(outcome_tracking_version=1,requested_outcomes=[{'id':'R1','status':'pending','text':'fixture','source':'test','evidence':[]}])
    path.write_text(json.dumps(task))
    assert create(repo,base,workspace).returncode==0
    args=('task','outcome',workspace,'--task-id',task['id'],'--outcome','R1','--status','verified','--evidence','fixture observed')
    assert invoke(workspace,*args,'--agent','other').returncode!=0
    result=invoke(workspace,*args,'--agent','owner')
    assert result.returncode==0,result.stderr
    assert json.loads(path.read_text())['requested_outcomes'][0]['status']=='verified'
    assert 'requested_outcomes' not in json.loads((workspace/'.go/tasks/active/task-schema-smoke.json').read_text())


def test_creation_refuses_a_dangling_symlink_without_creating_its_target(tmp_path):
    repo,base=setup_repo(tmp_path)
    target=tmp_path/'foreign target';workspace=tmp_path/'existing link'
    workspace.symlink_to(target,target_is_directory=True)
    result=create(repo,base,workspace)
    assert result.returncode!=0
    assert workspace.is_symlink() and not target.exists()
