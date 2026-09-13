"""Real owned-workspace publication and recovery boundaries."""
import json
import subprocess
from pathlib import Path

import pytest

from test_abc_worktrees import setup_repo, git, create


@pytest.fixture(autouse=True)
def neutral_controller_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def fixture(tmp_path):
    repo,_=setup_repo(tmp_path)
    bare=tmp_path/'remote.git';subprocess.run(['git','init','--bare','-q',str(bare)],check=True)
    git(repo,'remote','add','origin',str(bare))
    project=repo/'.go/project.json';value=json.loads(project.read_text())
    value['release_profiles']={'fixture':{'provider':'git-tag','remote':'origin','branch':'main',
        'publication':{'version':{'path':'VERSION','format':'text'},'bump':'minor','tag_prefix':'v','changelog':'CHANGELOG.md'}}}
    project.write_text(json.dumps(value))
    (repo/'VERSION').write_text('1.1.0\n');(repo/'CHANGELOG.md').write_text('# Changes\n')
    source=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(source.read_text())
    task['execution_contract']['release']['profile']='fixture'
    task['scope']['modify']=['app.txt','VERSION','CHANGELOG.md']
    task.update(execution_mode='agent',shareable_delivery='none',outcome_tracking_version=1,
        acceptance=['Versioned app delivered'],requested_outcomes=[{'id':'R1','text':'Versioned app delivered','source':'fixture','status':'pending','evidence':[]}],
        verification=["python3 -c \"from pathlib import Path; assert Path('VERSION').read_text().strip() == '1.2.0'; assert Path('app.txt').read_text() == 'delivered'\""])
    source.write_text(json.dumps(task));git(repo,'add','.');git(repo,'commit','-qm','release profile')
    base=git(repo,'rev-parse','HEAD');task['claim']['base_commit']=base;source.write_text(json.dumps(task))
    git(repo,'add','.go');git(repo,'commit','-qm','claim provenance');base=git(repo,'rev-parse','HEAD')
    git(repo,'tag','-a','v1.1.0','-m','baseline');git(repo,'push','origin','main','refs/tags/v1.1.0')
    worker=tmp_path/'worker';result=create(repo,base,worker);assert result.returncode==0,result.stderr
    (worker/'app.txt').write_text('delivered')
    (repo/'personal.txt').write_text('preserve me')
    return repo,worker,source


def prepare(repo):
    from go_workflow.release import prepare_release
    return prepare_release(repo,'task-schema-smoke','owner','run-1',ship_policy='push',allow_push=True)


def proofs(worker,source):
    from go_workflow.completion import capture_verification,record_critic,bind,changed_content,content_snapshot
    task=json.loads(source.read_text())
    artifact,_=capture_verification(worker,task['id'],'owner');assert artifact['status']=='passed'
    record_critic(worker,task['id'],'owner',{'schema':'go-workflow.critic-review.v1',**bind(worker,task),
        'status':'passed','reviewer':'owner','review_mode':'same_agent','summary':'Reviewed versioned app and release preparation',
        'blocking_findings':[],'reviewed_paths':sorted(changed_content(worker,task,content_snapshot(worker,task)))})


def test_preparation_requires_explicit_authority_and_never_infers_it_from_model(tmp_path):
    from go_workflow.release import prepare_release,PublicationError
    repo,worker,_=fixture(tmp_path)
    with pytest.raises(PublicationError,match='authoriz'):
        prepare_release(repo,'task-schema-smoke','owner','run-1',ship_policy='none',allow_push=False)
    assert (worker/'VERSION').read_text().strip()=='1.1.0'
    assert 'v1.2.0' not in git(repo,'tag','--list')


def test_preparation_reserves_one_version_and_reuses_it(tmp_path):
    repo,worker,_=fixture(tmp_path)
    first=prepare(repo);second=prepare(repo)
    assert first['version']==second['version']=='1.2.0'
    assert (worker/'VERSION').read_text()=='1.2.0\n'
    assert (worker/'CHANGELOG.md').read_text().count('1.2.0')==1
    assert (repo/'personal.txt').read_text()=='preserve me'


def test_publication_requires_proof_after_version_preparation(tmp_path):
    from go_workflow.release import publish_release,PublicationError
    repo,worker,_=fixture(tmp_path);prepare(repo)
    with pytest.raises(PublicationError,match='proof|evidence'):
        publish_release(repo,'task-schema-smoke','owner','run-1')
    assert not git(repo,'ls-remote','origin','refs/tags/v1.2.0')


def test_full_owned_worktree_release_and_repeat_readback(tmp_path):
    from go_workflow.release import publish_release
    from go_workflow.completion import lifecycle_report
    repo,worker,source=fixture(tmp_path);prepare(repo);proofs(worker,source)
    result=publish_release(repo,'task-schema-smoke','owner','run-1')
    assert result['status']=='published',result
    done=json.loads((repo/'.go/tasks/done/task-schema-smoke.json').read_text())
    assert done['review_status']=='approved' and done['release_receipt']['tag']=='v1.2.0'
    assert result['commit']==git(repo,'rev-parse','v1.2.0^{commit}')
    assert result['commit'] in git(repo,'ls-remote','origin','refs/heads/main')
    again=publish_release(repo,'task-schema-smoke','owner','run-1')
    assert again['commit']==result['commit'] and again['status']=='published'
    assert lifecycle_report(repo)['evidence_valid']
    assert (repo/'personal.txt').read_text()=='preserve me'


def test_changed_content_and_conflicting_tag_never_publish(tmp_path):
    from go_workflow.release import publish_release,PublicationError
    repo,worker,source=fixture(tmp_path);prepare(repo);proofs(worker,source)
    (worker/'app.txt').write_text('untested')
    with pytest.raises(PublicationError,match='content|proof'):
        publish_release(repo,'task-schema-smoke','owner','run-1')
    assert not git(repo,'ls-remote','origin','refs/tags/v1.2.0')


@pytest.mark.parametrize('operation',['commit','merge','tag','push'])
def test_lost_command_acknowledgement_reconciles_without_duplicate_effect(tmp_path,monkeypatch,operation):
    import go_workflow.release as publisher
    repo,worker,source=fixture(tmp_path);prepare(repo);proofs(worker,source)
    original=publisher._write_command;calls=[];lost=False
    def interrupted(session,cwd,argv):
        nonlocal lost
        calls.append(argv[:2]);value=original(session,cwd,argv)
        if argv[:2]==['git',operation] and not lost:
            lost=True
            raise publisher.PublicationError('fixture lost acknowledgement after '+operation)
        return value
    monkeypatch.setattr(publisher,'_write_command',interrupted)
    with pytest.raises(publisher.PublicationError,match='lost acknowledgement'):
        publisher.publish_release(repo,'task-schema-smoke','owner','run-1')
    assert source.exists()
    result=publisher.publish_release(repo,'task-schema-smoke','owner','run-1')
    assert result['status']=='published'
    assert calls.count(['git',operation])==1,calls


def test_conflicting_published_tag_is_preserved(tmp_path):
    from go_workflow.release import publish_release,PublicationError
    repo,worker,source=fixture(tmp_path);prepare(repo);proofs(worker,source)
    git(repo,'tag','-a','v1.2.0','-m','foreign release');foreign=git(repo,'rev-parse','v1.2.0')
    git(repo,'push','origin','refs/tags/v1.2.0')
    with pytest.raises(PublicationError,match='intent|conflict'):
        publish_release(repo,'task-schema-smoke','owner','run-1')
    assert foreign in git(repo,'ls-remote','origin','refs/tags/v1.2.0')
    assert source.exists()


def test_remote_advance_blocks_old_candidate_before_publication(tmp_path):
    from go_workflow.release import publish_release,PublicationError
    repo,worker,source=fixture(tmp_path);prepare(repo);proofs(worker,source)
    other=tmp_path/'other';subprocess.run(['git','clone','-q',str(tmp_path/'remote.git'),str(other)],check=True)
    git(other,'config','user.name','other');git(other,'config','user.email','other@example.test')
    git(other,'checkout','main');(other/'foreign.txt').write_text('another task')
    git(other,'add','foreign.txt');git(other,'commit','-qm','advance');git(other,'push','origin','main')
    with pytest.raises(PublicationError,match='Remote base advanced'):
        publish_release(repo,'task-schema-smoke','owner','run-1')
    assert source.exists() and not git(repo,'ls-remote','origin','refs/tags/v1.2.0')


def test_builder_commits_are_preserved_by_release_commit(tmp_path):
    from go_workflow.release import publish_release
    repo,worker,source=fixture(tmp_path)
    git(worker,'add','app.txt');git(worker,'commit','-qm','builder implementation');build=git(worker,'rev-parse','HEAD')
    prepare(repo);proofs(worker,source)
    result=publish_release(repo,'task-schema-smoke','owner','run-1')
    assert git(repo,'rev-parse',result['commit']+'^')==build


def test_finished_task_can_resume_approval_after_interruption(tmp_path,monkeypatch):
    from go_workflow.release import publish_release
    import go_workflow.cli as api
    repo,worker,source=fixture(tmp_path);prepare(repo);proofs(worker,source)
    original=api.cmd_task_review
    monkeypatch.setattr(api,'cmd_task_review',lambda *_: (_ for _ in ()).throw(RuntimeError('review interrupted')))
    with pytest.raises(RuntimeError,match='review interrupted'):
        publish_release(repo,'task-schema-smoke','owner','run-1')
    assert (repo/'.go/tasks/done/task-schema-smoke.json').exists()
    before=git(repo,'rev-parse','v1.2.0')
    monkeypatch.setattr(api,'cmd_task_review',original)
    assert publish_release(repo,'task-schema-smoke','owner','run-1')['status']=='published'
    assert git(repo,'rev-parse','v1.2.0')==before


def managed_fixture(tmp_path,monkeypatch):
    from test_abc_resume import runner_fixture
    repo,base,worker,capture=runner_fixture(tmp_path,monkeypatch)
    bare=tmp_path/'remote.git';subprocess.run(['git','init','--bare','-q',str(bare)],check=True)
    git(repo,'remote','add','origin',str(bare))
    project=repo/'.go/project.json';value=json.loads(project.read_text())
    value['release_profiles']={'fixture':{'provider':'git-tag','remote':'origin','branch':'main',
        'publication':{'version':{'path':'VERSION','format':'text'},'bump':'minor','tag_prefix':'v','changelog':'CHANGELOG.md'}}}
    project.write_text(json.dumps(value))
    (repo/'VERSION').write_text('1.1.0\n');(repo/'CHANGELOG.md').write_text('# Changes\n')
    source=repo/'.go/tasks/open/task-schema-smoke.json';task=json.loads(source.read_text())
    task['execution_contract']['release']['profile']='fixture'
    task['scope']['modify']=['app.txt','VERSION','CHANGELOG.md']
    task['intent_source']={'text':'Versioned app delivered','sha256':__import__('hashlib').sha256(b'Versioned app delivered').hexdigest(),'source_ref':'fixture'}
    task.update(outcome_tracking_version=1,requested_outcomes=[{'id':'R1','text':'Versioned app delivered','source':'fixture','status':'pending','evidence':[]}])
    task['verification'].append("python3 -c \"from pathlib import Path; assert Path('VERSION').read_text().strip() == '1.2.0'; print('full-output-'*2000)\"")
    source.write_text(json.dumps(task));git(repo,'add','.');git(repo,'commit','-qm','managed publication profile')
    git(repo,'tag','-a','v1.1.0','-m','baseline');git(repo,'push','origin','main','refs/tags/v1.1.0')
    return repo,git(repo,'rev-parse','HEAD'),worker,capture


def managed_publish(repo,base,worker,commands,initial=False):
    import sys
    from test_abc_worktrees import CLI
    args=[sys.executable,str(CLI),'auto',str(repo),'--execute','--task-id','task-schema-smoke',
        '--agent','owner','--executor-agent','codex','--max-commands',str(commands),'--max-minutes','5','--json']
    if initial: args+=['--workspace-path',str(worker),'--workspace-branch','task/release','--base-branch','main',
        '--base-commit',base,'--run-id','resume-run','--ship-policy','push','--allow-push']
    result=subprocess.run(args,cwd=repo,text=True,capture_output=True,timeout=120)
    assert result.returncode==0,result.stdout+result.stderr
    return json.loads(result.stdout)


def test_managed_release_resumes_budgeted_effects_and_cleans_separately(tmp_path,monkeypatch):
    from go_workflow.completion import read_artifact,lifecycle_report
    repo,base,worker,capture=managed_fixture(tmp_path,monkeypatch)
    first=managed_publish(repo,base,worker,1,initial=True)
    assert first['status']=='budget_exhausted' and first['phase']=='release_prepare'
    assert '--allow-push' in first['resume']['args']
    second=managed_publish(repo,base,worker,6)
    assert second['status']=='budget_exhausted' and second['phase']=='release'
    checkpoint=json.loads((repo/'.go/runs/task-schema-smoke/release-state.json').read_text())
    assert checkpoint['effects']['commit']['status']=='confirmed'
    assert not git(repo,'ls-remote','origin','refs/tags/v1.2.0')
    third=managed_publish(repo,base,worker,8)
    assert third['status'] in {'task_complete','done'} and third['completed_tasks']==['task-schema-smoke']
    assert not worker.exists()
    assert capture.read_text().splitlines()==['build','critic']
    done=json.loads((repo/'.go/tasks/done/task-schema-smoke.json').read_text())
    proof=read_artifact(repo/'.go',done['completion_evidence']['verification'])
    raw=read_artifact(repo/'.go',proof['checks'][1]['raw'])
    assert len(raw['stdout'])>20000 and proof['content_unchanged']
    assert lifecycle_report(repo)['evidence_valid']
    assert managed_publish(repo,base,worker,8)['completed_tasks']==['task-schema-smoke']


def test_cli_profile_and_checkpoint_schemas_and_malformed_state(tmp_path):
    from go_workflow.release import validate_state,PublicationError
    from test_abc_worktrees import ROOT,CLI
    import sys,jsonschema
    repo,worker,source=fixture(tmp_path);state=prepare(repo)
    jsonschema.validate(state,json.loads((ROOT/'schemas/publication-state.schema.json').read_text()))
    project=json.loads((repo/'.go/project.json').read_text())
    jsonschema.validate(project,json.loads((ROOT/'schemas/project.schema.json').read_text()))
    result=subprocess.run([sys.executable,str(CLI),'release','status',str(repo),'--task-id','task-schema-smoke',
        '--owner','owner','--run-id','run-1','--json'],cwd=repo,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['version']=='1.2.0'
    for field,value in [('profile',[]),('effects',{'push':None}),('preparation',[None]),('workspace',[]),('phase',{})]:
        broken={**state,field:value}
        with pytest.raises(PublicationError): validate_state(broken)


def test_resumed_preparation_cannot_steal_another_reservation(tmp_path):
    from go_workflow.release import PublicationError
    repo,worker,_=fixture(tmp_path);state=prepare(repo)
    reservation=repo/state['reservation'];foreign=json.loads(reservation.read_text());foreign['owner']='foreign'
    reservation.write_text(json.dumps(foreign))
    with pytest.raises(PublicationError,match='reservation ownership'):
        prepare(repo)
    assert json.loads(reservation.read_text())==foreign


def test_blocked_requirement_prevents_any_publication_effect(tmp_path):
    from go_workflow.release import publish_release,PublicationError
    repo,worker,source=fixture(tmp_path);prepare(repo);proofs(worker,source)
    task=json.loads(source.read_text());task['requested_outcomes'][0]['status']='blocked';source.write_text(json.dumps(task))
    before=git(worker,'rev-parse','HEAD')
    with pytest.raises(PublicationError,match='blocked/rejected'):
        publish_release(repo,'task-schema-smoke','owner','run-1')
    assert git(worker,'rev-parse','HEAD')==before and not git(repo,'tag','--list','v1.2.0')


def test_partial_preparation_retries_only_the_unwritten_file(tmp_path,monkeypatch):
    import go_workflow.release as publisher
    repo,worker,_=fixture(tmp_path);original=publisher.atomic_write_text;lost=False
    def interrupted(path,text):
        nonlocal lost
        original(path,text)
        if path.name=='VERSION' and not lost:
            lost=True;raise publisher.PublicationError('lost preparation acknowledgement')
    monkeypatch.setattr(publisher,'atomic_write_text',interrupted)
    with pytest.raises(publisher.PublicationError,match='lost preparation'):
        prepare(repo)
    assert prepare(repo)['phase']=='prepared'
    assert (worker/'VERSION').read_text()=='1.2.0\n'
    assert (worker/'CHANGELOG.md').read_text().count('1.2.0')==1


def test_global_integration_slot_blocks_a_competing_publisher(tmp_path):
    import sys
    from test_abc_worktrees import ROOT,CLI
    from go_workflow.release import publication_slot
    repo,worker,_=fixture(tmp_path)
    with publication_slot(repo,'task-schema-smoke','owner','run-1'):
        result=subprocess.run([sys.executable,str(CLI),'release','prepare',str(repo),'--task-id','task-schema-smoke',
            '--owner','owner','--run-id','run-1','--ship-policy','push','--allow-push'],cwd=repo,capture_output=True,text=True,timeout=10)
        assert result.returncode!=0 and 'lock' in result.stderr.lower(),result.stderr
    assert (worker/'VERSION').read_text()=='1.1.0\n'


@pytest.mark.parametrize('failure',['authorization','timeout','malformed'])
def test_unknown_github_response_is_never_treated_as_absence(tmp_path,monkeypatch,failure):
    import go_workflow.release as publisher
    def read(*args,**kwargs):
        if failure=='timeout': raise subprocess.TimeoutExpired(args[0],30)
        if failure=='malformed': return subprocess.CompletedProcess(args[0],0,'[invalid','')
        return subprocess.CompletedProcess(args[0],1,'','HTTP 403 forbidden')
    monkeypatch.setattr(publisher.subprocess,'run',read)
    with pytest.raises(publisher.PublicationError,match='not absence'):
        publisher._github_observation({'repository':'fixture/repo'},'v1.2.0')


def test_github_create_lost_response_uses_readback_without_republication(tmp_path,monkeypatch):
    import go_workflow.release as publisher
    import sys,os
    repo,worker,_=fixture(tmp_path);state=prepare(repo)
    state['profile'].update(provider='github-release',repository='fixture/repo')
    data=tmp_path/'github.json';calls=tmp_path/'github-calls.txt'
    binary=tmp_path/'gh';binary.write_text('#!'+sys.executable+'''\nimport sys,json,pathlib,os
args=sys.argv[1:];state=pathlib.Path(os.environ['GH_FIXTURE_DATA'])
if args[:2]==['release','create']:
 body=pathlib.Path(args[args.index('--notes-file')+1]).read_text()
 state.write_text(json.dumps({'tag_name':args[2],'draft':False,'body':body}))
 with pathlib.Path(os.environ['GH_FIXTURE_CALLS']).open('a') as f: f.write('create\\n')
 sys.exit(1) # server accepted, caller lost the response
if state.exists(): print(state.read_text())
elif 'per_page' in args[-1]: print('[]')
else: print('gh: Not Found (HTTP 404)',file=sys.stderr);sys.exit(1)
''');binary.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    monkeypatch.setenv('GH_FIXTURE_DATA',str(data));monkeypatch.setenv('GH_FIXTURE_CALLS',str(calls))
    with publisher.publication_slot(repo,'task-schema-smoke','owner','run-1') as (_,session):
        with pytest.raises(publisher.PublicationError,match='command failed'):
            publisher._publish_github(repo,state,session)
        publisher._publish_github(repo,state,session)
    assert calls.read_text().splitlines()==['create']
    assert state['effects']['publication']['status']=='confirmed'


def test_dead_publisher_with_live_child_blocks_successor_and_workspace_mutations(tmp_path):
    import sys,signal,os
    from test_abc_resume import wait_file
    from test_abc_worktrees import ROOT
    from go_workflow.release import PublicationError,publish_release
    from go_workflow.worktrees import stage_workspace,WorkspaceError
    repo,worker,_=fixture(tmp_path);prepare(repo)
    program="""import sys
from pathlib import Path
from go_workflow.release import publication_slot,_write_command
repo=Path(sys.argv[1])
with publication_slot(repo,'task-schema-smoke','owner','run-1') as (_,session):
 _write_command(session,repo,[sys.executable,'-c','import time; time.sleep(40)'])
"""
    env=os.environ.copy();env['PYTHONPATH']=str(ROOT)
    parent=subprocess.Popen([sys.executable,'-c',program,str(repo)],cwd=repo,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    group=None
    try:
        state=wait_file(repo/'.go/runs/task-schema-smoke/publication-process.json',lambda value:bool(value.get('worker_group')))
        group=state['worker_group'];parent.kill();parent.wait(timeout=5)
        with pytest.raises(WorkspaceError,match='worker process group is still live'):
            publish_release(repo,'task-schema-smoke','owner','run-1')
        with pytest.raises(WorkspaceError,match='worker process group is still live'):
            stage_workspace(repo,'task-schema-smoke','owner','run-1')
        assert worker.exists() and not git(repo,'tag','--list','v1.2.0')
    finally:
        if parent.poll() is None: parent.kill();parent.wait(timeout=5)
        if group:
            try: os.killpg(group,signal.SIGKILL)
            except ProcessLookupError: pass
        parent.communicate(timeout=5)


def test_published_checkpoint_recovers_lost_reservation_release(tmp_path,monkeypatch):
    import go_workflow.release as publisher
    repo,worker,source=fixture(tmp_path);state=prepare(repo);proofs(worker,source)
    original=publisher._release_reservation
    monkeypatch.setattr(publisher,'_release_reservation',lambda *_: (_ for _ in ()).throw(RuntimeError('interrupted reservation release')))
    with pytest.raises(RuntimeError,match='interrupted reservation'):
        publisher.publish_release(repo,'task-schema-smoke','owner','run-1')
    assert json.loads((repo/state['reservation']).read_text())['status']=='reserved'
    monkeypatch.setattr(publisher,'_release_reservation',original)
    assert publisher.publish_release(repo,'task-schema-smoke','owner','run-1')['status']=='published'
    assert json.loads((repo/state['reservation']).read_text())['status']=='released'


def test_unapproved_managed_publication_leaves_intake_open(tmp_path,monkeypatch):
    import sys
    from test_abc_worktrees import CLI
    repo,base,worker,_=managed_fixture(tmp_path,monkeypatch)
    args=[sys.executable,str(CLI),'auto',str(repo),'--execute','--task-id','task-schema-smoke','--agent','owner',
        '--executor-agent','codex','--workspace-path',str(worker),'--workspace-branch','task/release','--base-branch','main',
        '--base-commit',base,'--run-id','resume-run','--max-commands','1','--json']
    result=subprocess.run(args,cwd=repo,text=True,capture_output=True,timeout=30)
    assert result.returncode!=0 and 'explicit --ship-policy push --allow-push' in result.stdout
    assert (repo/'.go/tasks/open/task-schema-smoke.json').exists() and not worker.exists()
    assert not (repo/'.go/runs/task-schema-smoke/run-state.json').exists()


def test_managed_conflicting_remote_preserves_explicit_resume_handoff(tmp_path,monkeypatch):
    repo,base,worker,capture=managed_fixture(tmp_path,monkeypatch)
    managed_publish(repo,base,worker,1,initial=True)
    assert managed_publish(repo,base,worker,6)['phase']=='release'
    other=tmp_path/'other';subprocess.run(['git','clone','-q',str(tmp_path/'remote.git'),str(other)],check=True)
    git(other,'config','user.name','other');git(other,'config','user.email','other@example.test')
    git(other,'checkout','main');(other/'foreign.txt').write_text('concurrent change')
    git(other,'add','foreign.txt');git(other,'commit','-qm','advance');git(other,'push','origin','main')
    result=managed_publish(repo,base,worker,8)
    assert result['status']=='resume_gate' and result['phase']=='release'
    assert result['blocked_task']=='task-schema-smoke' and result['resume']['run_id']=='resume-run'
    assert '--allow-push' in result['resume']['args']
    assert capture.read_text().splitlines()==['build','critic'] and worker.exists()


def test_task_completion_does_not_report_queue_done_with_blocked_work(tmp_path,monkeypatch):
    repo,base,worker,capture=managed_fixture(tmp_path,monkeypatch)
    managed_publish(repo,base,worker,1,initial=True)
    task=json.loads((repo/'.go/tasks/active/task-schema-smoke.json').read_text())
    blocked={**task,'id':'unresolved','status':'blocked','work_status':'blocked',
             'claim':{'agent':None,'claimed_at':None},'review_status':'none'}
    blocked.pop('completion_evidence',None)
    (repo/'.go/tasks/blocked').mkdir(exist_ok=True)
    (repo/'.go/tasks/blocked/unresolved.json').write_text(json.dumps(blocked))
    hierarchy=repo/'.go/hierarchy.json';value=json.loads(hierarchy.read_text())
    value['epics'][0]['features'][0]['tasks'].append('unresolved');hierarchy.write_text(json.dumps(value))
    result=managed_publish(repo,base,worker,15)
    assert result['completed_tasks']==['task-schema-smoke'] and result['status']=='task_complete'
    assert (repo/'.go/tasks/blocked/unresolved.json').exists()
    assert managed_publish(repo,base,worker,15)['status']=='task_complete'
