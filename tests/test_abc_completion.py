"""Completion boundaries: real Git evidence, stale-proof and bypass regressions."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_abc_worktrees import setup_repo, git, CLI, ROOT

SUMMARY='changed_files=app.txt; verification_command=git diff --check; verification_result=passed; critic=passed; runtime=fixture; model=fixture; billing_mode=unknown'


def fixture(tmp_path):
    repo,base=setup_repo(tmp_path)
    source=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(source.read_text())
    task.update(execution_mode='agent',shareable_delivery='none',acceptance=['app.txt has the delivered content'],verification=["python3 -c \"from pathlib import Path; assert Path('app.txt').read_text() == 'delivered'\""])
    task['claim']['base_commit']=base
    task['execution_contract']['workspace']['mode']='current'
    task['execution_contract']['release']['profile']='fixture-tags'
    source.write_text(json.dumps(task))
    bare=tmp_path/'origin.git';subprocess.run(['git','init','--bare','-q',str(bare)],check=True)
    git(repo,'remote','add','origin',str(bare))
    project=repo/'.go/project.json';data=json.loads(project.read_text());data['release_profiles']={'fixture-tags':{'provider':'git-tag','remote':'origin','branch':'main'}};project.write_text(json.dumps(data))
    (repo/'app.txt').write_text('delivered');git(repo,'add','.');git(repo,'commit','-qm','candidate')
    return repo,source,json.loads(source.read_text())


def call(repo,*args):
    return subprocess.run([sys.executable,str(CLI),*map(str,args)],cwd=repo,text=True,capture_output=True)


def test_manual_finish_refuses_prose_without_executed_check_and_release_proof(tmp_path):
    repo,source,task=fixture(tmp_path)
    result=call(repo,'finish',task['id'],'--repo',repo,'--agent','owner','--evidence',SUMMARY)
    assert result.returncode!=0,'Opted-in task finished from prose alone: '+result.stdout
    assert 'lifecycle' in result.stderr.lower()
    assert source.exists() and not (repo/'.go/tasks/done'/source.name).exists()


def test_internal_automatic_finish_uses_the_same_gate(tmp_path):
    from go_workflow.cli import finish_task_record,RepoLocalError
    repo,source,task=fixture(tmp_path)
    with pytest.raises(RepoLocalError,match='lifecycle'):
        finish_task_record(repo,repo/'.go',source,task,'owner',SUMMARY)
    assert source.exists()


@pytest.mark.parametrize('review_status',['review','approved'])
def test_approval_cannot_bypass_missing_publication_proof(tmp_path,review_status):
    repo,source,task=fixture(tmp_path)
    task.update(status='done',work_status='completed',review_status=review_status)
    done=repo/'.go/tasks/done'/source.name;done.parent.mkdir(exist_ok=True);done.write_text(json.dumps(task));source.unlink()
    result=call(repo,'task','review',repo,'--task-id',task['id'],'--status','approved','--agent','owner','--evidence','looks good')
    assert result.returncode!=0,'Approval bypassed missing release proof: '+result.stdout
    assert 'lifecycle' in result.stderr.lower()


def prepare_proofs(repo,task):
    from go_workflow.completion import capture_verification,record_critic,bind
    from go_workflow.shipping import capture_release
    artifact,verification=capture_verification(repo,task['id'],'owner')
    assert artifact['status']=='passed'
    review={'schema':'go-workflow.critic-review.v1',**bind(repo,task),'status':'passed','reviewer':'owner',
            'review_mode':'same_agent','summary':'Fixture reviewed delivered app content and mandatory verification.',
            'blocking_findings':[],'reviewed_paths':['app.txt','.go/project.json']}
    critic=record_critic(repo,task['id'],'owner',review)
    git(repo,'tag','-a','v1.2','-m','fixture release');git(repo,'push','origin','main','refs/tags/v1.2')
    release,reference=capture_release(repo,task['id'],'owner','v1.2')
    return artifact,verification,critic,release,reference


def test_executed_checks_review_and_remote_tag_allow_finish(tmp_path):
    from go_workflow.completion import completion_findings
    repo,source,task=fixture(tmp_path)
    prepare_proofs(repo,task)
    task=json.loads(source.read_text())
    assert completion_findings(repo,task)==[]
    result=call(repo,'finish',task['id'],'--repo',repo,'--agent','owner','--evidence',SUMMARY)
    assert result.returncode==0,result.stdout+result.stderr
    result=call(repo,'task','review',repo,'--task-id',task['id'],'--status','approved','--agent','owner','--evidence','Same-agent fixture critic and exact release proof passed')
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.parametrize('change',['code','raw_result','remote_tag'])
def test_drift_and_tampered_proof_cannot_finish_previously_green_work(tmp_path,change):
    repo,source,task=fixture(tmp_path)
    artifact,*_=prepare_proofs(repo,task)
    if change=='code':(repo/'app.txt').write_text('changed after verification')
    elif change=='raw_result':
        path=repo/artifact['checks'][0]['raw']['path'];data=json.loads(path.read_text());data['stdout']='rewritten';path.write_text(json.dumps(data))
    else:git(repo,'push','origin',':refs/tags/v1.2')
    result=call(repo,'finish',task['id'],'--repo',repo,'--agent','owner','--evidence',SUMMARY)
    assert result.returncode!=0 and 'lifecycle' in result.stderr
    assert source.exists()


def test_failed_verification_retains_actual_command_and_cannot_be_replaced_by_filled_fields(tmp_path):
    from go_workflow.completion import capture_verification,completion_findings
    repo,source,task=fixture(tmp_path)
    (repo/'app.txt').write_text('wrong')
    artifact,ref=capture_verification(repo,task['id'],'owner')
    assert artifact['status']=='failed'
    assert artifact['checks'][0]['verification']['exit_code']!=0
    assert artifact['checks'][0]['verification']['command']==task['verification'][0]
    assert 'AssertionError' in json.loads((repo/artifact['checks'][0]['raw']['path']).read_text())['stderr']
    assert completion_findings(repo,json.loads(source.read_text()))


@pytest.mark.parametrize('shipping_status',['push_failed','readback_failed'])
def test_opted_in_legacy_loop_restores_active_when_shipping_fails(tmp_path,monkeypatch,shipping_status):
    import go_workflow.cli as api
    repo,source,task=fixture(tmp_path)
    task['execution_mode']='mechanical';source.write_text(json.dumps(task))
    prepare_proofs(repo,task)
    task=json.loads(source.read_text());task.update(status='open',work_status='pending',claim={'agent':None,'claimed_at':None})
    opened=repo/'.go/tasks/open'/source.name;opened.parent.mkdir(exist_ok=True);opened.write_text(json.dumps(task));source.unlink()
    monkeypatch.setenv('GO_STACK_ALLOW_DEV','1')  # disposable candidate fixture only
    monkeypatch.setattr(api,'ship_changes',lambda *args,**kwargs:{'status':shipping_status,'readback_error':'fixture rejected push/readback'})
    args=api.build_parser().parse_args(['auto',str(repo),'--execute','--agent','owner','--max-tasks','1','--no-semantic-critic','--ship-policy','push','--allow-push','--json'])
    code,result=api.execute_loop_plan(repo,args,'go-auto')
    assert result['completed_tasks']==[],result
    assert result['status']=='blocked',result
    assert source.exists() and not (repo/'.go/tasks/done'/source.name).exists()


def test_cli_captures_checks_and_reports_missing_release_without_completing(tmp_path):
    repo,source,task=fixture(tmp_path)
    result=call(repo,'completion','verify',repo,'--task-id',task['id'],'--agent','owner','--json')
    assert result.returncode==0,result.stdout+result.stderr
    payload=json.loads(result.stdout)
    assert payload['status']=='passed' and payload['binding']['content_digest']
    status=call(repo,'completion','status',repo,'--task-id',task['id'],'--agent','owner')
    assert status.returncode!=0 and not json.loads(status.stdout)['eligible']
    assert source.exists()


def test_status_does_not_count_invalid_opted_in_done_as_verified_completion(tmp_path):
    repo,source,task=fixture(tmp_path)
    task.update(status='done',work_status='completed',review_status='approved')
    target=repo/'.go/tasks/done'/source.name;target.parent.mkdir(exist_ok=True);target.write_text(json.dumps(task));source.unlink()
    result=call(repo,'status',repo,'--json');assert result.returncode==0,result.stderr
    status=json.loads(result.stdout)
    assert status['tasks']['done']==0 and status['tasks']['recorded_done']==1
    assert task['id'] in status['lifecycle']['invalid_done']


def test_missing_publisher_profile_is_an_explicit_gate(tmp_path):
    from go_workflow.shipping import capture_release
    from go_workflow.completion import CompletionError
    repo,source,task=fixture(tmp_path)
    task['execution_contract']['release']['profile']='unimplemented-deployment';source.write_text(json.dumps(task))
    with pytest.raises(CompletionError,match='no supported'):
        capture_release(repo,task['id'],'owner','v1.2')
    assert source.exists()


def test_requirements_need_references_to_validated_phase_evidence(tmp_path):
    from go_workflow.completion import completion_findings
    repo,source,task=fixture(tmp_path)
    task.update(outcome_tracking_version=1,requested_outcomes=[{'id':'R1','text':'Delivered app','source':'fixture','status':'pending','evidence':[]}])
    source.write_text(json.dumps(task))
    _,verification,_,_,_=prepare_proofs(repo,task)
    task=json.loads(source.read_text());task['requested_outcomes'][0].update(status='verified',evidence=['test_evidence:none'])
    source.write_text(json.dumps(task));assert completion_findings(repo,task)
    task['requested_outcomes'][0]['evidence']=[verification['path']];source.write_text(json.dumps(task))
    assert completion_findings(repo,task)==[]


def test_existing_unconfigured_legacy_finish_is_not_silently_migrated(tmp_path):
    repo,source,task=fixture(tmp_path)
    task.pop('execution_contract');source.write_text(json.dumps(task))
    result=call(repo,'finish',task['id'],'--repo',repo,'--agent','owner','--evidence',SUMMARY)
    assert result.returncode==0,result.stdout+result.stderr


def test_killed_verification_collector_leaves_visible_orphan_and_blocks_finish(tmp_path):
    import os,signal,time
    from test_abc_resume import wait_file
    from go_workflow.run_state import group_alive
    repo,source,task=fixture(tmp_path)
    task['verification']=['python3 -c "import time; time.sleep(40)"'];source.write_text(json.dumps(task))
    process=subprocess.Popen([sys.executable,str(CLI),'completion','verify',str(repo),'--task-id',task['id'],'--agent','owner'],cwd=repo,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    group=None
    try:
        state=wait_file(repo/'.go/runs/task-schema-smoke/completion-state.json',lambda state:state.get('worker_group'))
        group=state['worker_group'];process.kill();process.wait(timeout=5)
        result=call(repo,'finish',task['id'],'--repo',repo,'--agent','owner','--evidence',SUMMARY)
        assert result.returncode!=0 and 'worker process group is still live' in result.stderr
        retry=call(repo,'completion','verify',repo,'--task-id',task['id'],'--agent','owner')
        assert retry.returncode!=0 and 'worker process group is still live' in retry.stderr
        assert source.exists()
    finally:
        if process.poll() is None:process.kill();process.wait()
        if group:
            try:os.killpg(group,signal.SIGKILL)
            except ProcessLookupError:pass
            for _ in range(100):
                if not group_alive(group):break
                time.sleep(.03)


def test_release_evidence_matches_schema_and_rejects_malformed_saved_readback(tmp_path):
    from jsonschema import Draft202012Validator
    from go_workflow.shipping import verify_release_evidence
    from go_workflow.completion import CompletionError
    repo,source,task=fixture(tmp_path)
    _,_,_,proof,_=prepare_proofs(repo,task)
    schema=json.loads((ROOT/'schemas/release-evidence.schema.json').read_text())
    assert not list(Draft202012Validator(schema).iter_errors(proof))
    proof['refs']={}
    with pytest.raises(CompletionError,match='raw reference'):
        verify_release_evidence(repo,task,proof,proof['content_digest'],remote=False)


def test_requirement_cannot_point_to_unvalidated_optional_release(tmp_path):
    from go_workflow.completion import capture_verification, record_critic, bind, completion_findings
    repo,source,task=fixture(tmp_path)
    task['execution_contract']['task_kind']='mechanical'
    task['execution_contract']['release']={'mode':'none','reason':'Fixture local-only check'}
    task['requested_outcomes']=[{'id':'R1','text':'Delivered app','source':'fixture','status':'pending','evidence':[]}]
    source.write_text(json.dumps(task))
    capture_verification(repo,task['id'],'owner')
    record_critic(repo,task['id'],'owner',{'schema':'go-workflow.critic-review.v1',**bind(repo,task),
        'status':'passed','reviewer':'owner','review_mode':'same_agent','summary':'Reviewed fixture',
        'blocking_findings':[],'reviewed_paths':['app.txt','.go/project.json']})
    task=json.loads(source.read_text())
    task['completion_evidence']['release']={'path':'.go/evidence/not-a-proof.json','sha256':'0'*64}
    task['requested_outcomes'][0].update(status='verified',evidence=['.go/evidence/not-a-proof.json'])
    source.write_text(json.dumps(task))
    assert any('Requirement lacks' in finding for finding in completion_findings(repo,task))


def test_check_proof_must_preserve_requirement_binding(tmp_path):
    from go_workflow.completion import capture_verification, completion_findings, save_artifact
    repo,source,task=fixture(tmp_path)
    artifact,_,_,_,_=prepare_proofs(repo,task)
    artifact['checks'][0]['verification']['requirement_ids']=['another-requirement']
    ref=save_artifact(repo/'.go',task['id'],'verification',artifact)
    task=json.loads(source.read_text());task['completion_evidence']['verification']=ref;source.write_text(json.dumps(task))
    assert any('Mandatory check' in finding for finding in completion_findings(repo,task))


def test_historical_completion_is_readable_in_an_ordinary_clone(tmp_path):
    from go_workflow.completion import lifecycle_report
    repo,source,task=fixture(tmp_path)
    prepare_proofs(repo,task)
    result=call(repo,'finish',task['id'],'--repo',repo,'--agent','owner','--evidence',SUMMARY)
    assert result.returncode==0,result.stderr
    git(repo,'add','.go');git(repo,'commit','-qm','persist completion history')
    clone=tmp_path/'ordinary clone'
    subprocess.run(['git','clone','--no-hardlinks','-q',str(repo),str(clone)],check=True)
    report=lifecycle_report(clone)
    assert report['evidence_valid'],report
    assert task['id'] in report['verified_done']
