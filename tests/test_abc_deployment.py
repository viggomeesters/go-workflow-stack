"""Explicit deployment policy, authoritative target readback and crash recovery."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_abc_worktrees import setup_repo,git,create,ROOT,CLI
from test_abc_release import proofs


@pytest.fixture(autouse=True)
def neutral_controller_directory(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)


def adapter_profile(tmp_path,*,package=True,mode='required'):
    target=tmp_path/'target.json';flags=tmp_path/'flags.json';flags.write_text('{}')
    adapter=tmp_path/'adapter.py'
    adapter.write_text('''import sys,json,pathlib,hashlib
operation,target_path,flags_path,*args=sys.argv[1:]
target=pathlib.Path(target_path);flags=json.loads(pathlib.Path(flags_path).read_text())
if operation=='package':
 output,commit,version=args
 pathlib.Path(output).write_bytes(('package:'+commit+':'+version).encode())
 print(json.dumps({'packaged':True}));sys.exit(0)
key,environment,commit,version,artifact_sha,*payload=args
expected={'schema':'go-workflow.deployment-observation.v1','target':environment,'commit':commit,'version':version,'artifact_sha256':artifact_sha or None,'idempotency_key':key}
if operation=='deploy':
 old=json.loads(target.read_text()) if target.exists() else {}
 if old.get('idempotency_key')!=key:
  if payload and payload[0]:
   data=pathlib.Path(payload[0]).read_bytes();actual=hashlib.sha256(data).hexdigest()
   assert actual==artifact_sha
   target.with_suffix('.pkg').write_bytes(data)
   expected['artifact_sha256']=actual
  expected.update(status='accepted' if flags.get('delay') else 'live',authorized=True,available=not flags.get('delay'),effects=old.get('effects',0)+1)
  target.write_text(json.dumps(expected))
 if flags.get('lost_ack'): print('lost acknowledgement',file=sys.stderr);sys.exit(1)
 print(json.dumps({'accepted':True}));sys.exit(0)
if flags.get('unknown'): print('network unavailable',file=sys.stderr);sys.exit(2)
if not target.exists():
 print(json.dumps({**expected,'status':'absent','authorized':True,'available':False}));sys.exit(0)
observed=json.loads(target.read_text())
if observed.get('artifact_sha256'):
 observed['artifact_sha256']=hashlib.sha256(target.with_suffix('.pkg').read_bytes()).hexdigest()
if flags.get('wrong_version'):observed['version']='0.0.0'
if flags.get('wrong_target'):observed['target']='foreign-target'
if flags.get('wrong_hash'):observed['artifact_sha256']='0'*64
if not flags.get('delay'):observed.update(status='live',available=True)
print(json.dumps(observed))
''')
    common=[sys.executable,str(adapter),None,str(target),str(flags),'{idempotency_key}','{target}','{commit}','{version}','{artifact_sha256}']
    deploy=[*common,'{artifact}'];deploy[2]='deploy';observe=[*common];observe[2]='observe'
    deployment={'mode':'required','target':'fixture-staging','recovery_policy':'resume_only','required_env':[],
        'deploy':{'argv':deploy,'idempotency':'required'},'observe':{'argv':observe,'read_only':True}}
    if package:deployment['package']={'argv':[sys.executable,str(adapter),'package',str(target),str(flags),'{output}','{commit}','{version}'],'filename':'app.pkg'}
    if mode=='none':deployment={'mode':'none','reason':'This project publishes its source release without a deployed service.'}
    return deployment,target,flags


def fixture(tmp_path,*,package=True,mode='required'):
    repo,_=setup_repo(tmp_path)
    bare=tmp_path/'remote.git';subprocess.run(['git','init','--bare','-q',str(bare)],check=True)
    git(repo,'remote','add','origin',str(bare))
    deployment,target,flags=adapter_profile(tmp_path,package=package,mode=mode)
    project=repo/'.go/project.json';value=json.loads(project.read_text())
    value['release_profiles']={'fixture':{'provider':'git-tag','remote':'origin','branch':'main',
        'publication':{'version':{'path':'VERSION','format':'text'},'bump':'minor','tag_prefix':'v','changelog':'CHANGELOG.md'},'deployment':deployment}}
    project.write_text(json.dumps(value));(repo/'VERSION').write_text('1.1.0\n');(repo/'CHANGELOG.md').write_text('# Changes\n')
    source=repo/'.go/tasks/active/task-schema-smoke.json';task=json.loads(source.read_text())
    task['execution_contract']['release']['profile']='fixture';task['scope']['modify']=['app.txt','VERSION','CHANGELOG.md']
    task['intent_source']={'text':'Versioned app delivered','sha256':__import__('hashlib').sha256(b'Versioned app delivered').hexdigest(),'source_ref':'fixture'}
    task.update(execution_mode='agent',shareable_delivery='none',acceptance=['Versioned app delivered'],outcome_tracking_version=1,
        requested_outcomes=[{'id':'R1','text':'Versioned app delivered','source':'fixture','status':'pending','evidence':[]}],
        verification=["python3 -c \"from pathlib import Path; assert Path('app.txt').read_text() == 'delivered'\""])
    source.write_text(json.dumps(task));git(repo,'add','.');git(repo,'commit','-qm','explicit deployment fixture')
    base=git(repo,'rev-parse','HEAD');task['claim']['base_commit']=base;source.write_text(json.dumps(task));git(repo,'add','.go');git(repo,'commit','-qm','claim provenance')
    base=git(repo,'rev-parse','HEAD');git(repo,'tag','-a','v1.1.0','-m','baseline');git(repo,'push','origin','main','refs/tags/v1.1.0')
    worker=tmp_path/'worker';result=create(repo,base,worker);assert result.returncode==0,result.stderr
    (worker/'app.txt').write_text('delivered')
    return repo,worker,source,target,flags


def prepare(repo,*,allow_deploy=True):
    from go_workflow.release import prepare_release
    return prepare_release(repo,'task-schema-smoke','owner','run-1',ship_policy='push',allow_push=True,allow_deploy=allow_deploy)


def publish(repo):
    from go_workflow.release import publish_release
    return publish_release(repo,'task-schema-smoke','owner','run-1')


def test_deployment_needs_its_own_authority_before_release_writes(tmp_path):
    from go_workflow.deployment import DeploymentError
    repo,worker,_,target,_=fixture(tmp_path)
    with pytest.raises(DeploymentError,match='authorization'):
        prepare(repo,allow_deploy=False)
    assert not target.exists() and (worker/'VERSION').read_text()=='1.1.0\n'
    assert not git(repo,'tag','--list','v1.2.0')


@pytest.mark.parametrize('package',[True,False])
def test_full_release_requires_matching_live_target_and_optional_artifact(tmp_path,package):
    from go_workflow.completion import read_artifact,lifecycle_report
    repo,worker,source,target,_=fixture(tmp_path,package=package)
    prepare(repo);proofs(worker,source);result=publish(repo)
    assert result['status']=='published'
    deployed=json.loads(target.read_text());assert deployed['effects']==1 and deployed['available']
    assert deployed['commit']==result['commit'] and deployed['version']=='1.2.0'
    assert bool(deployed['artifact_sha256'])==package
    done=json.loads((repo/'.go/tasks/done/task-schema-smoke.json').read_text())
    release=read_artifact(repo/'.go',done['completion_evidence']['release'])
    assert release['deployment'] and lifecycle_report(repo)['evidence_valid']
    assert publish(repo)['status']=='published' and json.loads(target.read_text())['effects']==1


def test_no_deployment_policy_does_not_invent_live_proof_or_require_permission(tmp_path):
    repo,worker,source,target,_=fixture(tmp_path,mode='none')
    prepare(repo,allow_deploy=False);proofs(worker,source)
    assert publish(repo)['status']=='published' and not target.exists()


@pytest.mark.parametrize('problem',['wrong_version','wrong_target','wrong_hash','unknown','delay','lost_ack'])
def test_wrong_or_uncertain_target_stays_pending_and_recovers_without_second_effect(tmp_path,problem):
    from go_workflow.deployment import DeploymentError
    repo,worker,source,target,flags=fixture(tmp_path)
    prepare(repo);proofs(worker,source);flags.write_text(json.dumps({problem:True}))
    with pytest.raises((DeploymentError,ValueError)):
        publish(repo)
    assert source.exists()
    if problem!='unknown':assert json.loads(target.read_text())['effects']==1
    flags.write_text('{}');assert publish(repo)['status']=='published'
    assert json.loads(target.read_text())['effects']==1


def test_schema_roundtrip_includes_package_and_live_proof(tmp_path):
    import jsonschema
    from go_workflow.completion import read_artifact
    repo,worker,source,_,_=fixture(tmp_path)
    prepare(repo);proofs(worker,source);publish(repo)
    task=json.loads((repo/'.go/tasks/done/task-schema-smoke.json').read_text())
    release=read_artifact(repo/'.go',task['completion_evidence']['release'])
    deployment=read_artifact(repo/'.go',release['deployment'])
    examples={'project.schema.json':json.loads((repo/'.go/project.json').read_text()),
        'release-profile.schema.json':json.loads((repo/'.go/project.json').read_text())['release_profiles']['fixture'],
        'publication-state.schema.json':json.loads((repo/'.go/runs/task-schema-smoke/release-state.json').read_text()),
        'release-evidence.schema.json':release,'deployment-evidence.schema.json':deployment}
    for name,value in examples.items():jsonschema.validate(value,json.loads((ROOT/'schemas'/name).read_text()))


def test_missing_credentials_and_unsafe_recovery_policy_block_before_preparation(tmp_path,monkeypatch):
    from go_workflow.deployment import DeploymentError,validate_profile
    repo,worker,_,target,_=fixture(tmp_path)
    path=repo/'.go/project.json';project=json.loads(path.read_text());spec=project['release_profiles']['fixture']['deployment']
    assert validate_profile({**spec,'recovery_policy':'automatic_rollback'})
    assert validate_profile({**spec,'target':''})
    assert validate_profile({**spec,'deploy':{**spec['deploy'],'idempotency':'none'}})
    assert validate_profile({'mode':'none','reason':''})
    spec['required_env']=['GO_FIXTURE_DEPLOY_TOKEN'];path.write_text(json.dumps(project));monkeypatch.delenv('GO_FIXTURE_DEPLOY_TOKEN',raising=False)
    with pytest.raises(DeploymentError,match='credentials'):
        prepare(repo)
    assert not target.exists() and (worker/'VERSION').read_text()=='1.1.0\n'


def test_invalid_checkpoint_fields_are_rejected_before_resume(tmp_path):
    from copy import deepcopy
    from go_workflow.deployment import validate_state,DeploymentError,STATE
    spec,_,_=adapter_profile(tmp_path)
    value={'schema':STATE,'profile':spec,'key':'a'*64,'phase':'pending','commands':[],
           'observations':[],'artifact':None,'package_attempts':[]}
    validate_state(value)
    for key,bad in [('effect',True),('effect',{'expected':{},'status':'pending'}),
                    ('commands',[{}]),('evidence',[]),('package_attempts',[{'output':'','status':'pending'}]),
                    ('observations',[{'observation':None,'raw':{'path':'x','sha256':'a'*64}}])]:
        changed=deepcopy(value);changed[key]=bad
        with pytest.raises(DeploymentError):validate_state(changed)


def test_interrupted_packaging_preserves_partial_file_and_rebuilds_fresh(tmp_path,monkeypatch):
    import go_workflow.deployment as deployment
    repo,worker,source,target,_=fixture(tmp_path)
    prepare(repo);proofs(worker,source)
    original=deployment._command;partial=[]
    def interrupt(control,task,session,argv,digest,phase):
        if phase=='package' and not partial:
            output=Path(argv[-3]);output.write_bytes(b'partial output');partial.append(output)
            raise RuntimeError('package controller interrupted')
        return original(control,task,session,argv,digest,phase)
    monkeypatch.setattr(deployment,'_command',interrupt)
    with pytest.raises(RuntimeError,match='package controller'):publish(repo)
    assert not target.exists() and source.exists()
    assert publish(repo)['status']=='published'
    state=json.loads((repo/'.go/runs/task-schema-smoke/release-state.json').read_text())['deployment']
    assert partial[0].read_bytes()==b'partial output'
    assert len(state['package_attempts'])==2 and state['artifact']['path']!=str(partial[0])
    assert json.loads(target.read_text())['effects']==1


def test_live_version_is_rechecked_after_interrupted_finish(tmp_path,monkeypatch):
    import go_workflow.cli as api
    repo,worker,source,target,flags=fixture(tmp_path)
    prepare(repo);proofs(worker,source);finish=api.cmd_finish
    monkeypatch.setattr(api,'cmd_finish',lambda *_: (_ for _ in ()).throw(RuntimeError('finish interrupted')))
    with pytest.raises(RuntimeError,match='finish interrupted'):publish(repo)
    assert source.exists() and json.loads(target.read_text())['effects']==1
    monkeypatch.setattr(api,'cmd_finish',finish);flags.write_text('{"wrong_version":true}')
    with pytest.raises(api.RepoLocalError,match='deployment|Deployment'):publish(repo)
    assert source.exists()
    flags.write_text('{}');assert publish(repo)['status']=='published'
    assert json.loads(target.read_text())['effects']==1


def test_historical_clone_does_not_require_old_service_version_to_remain_live(tmp_path):
    from go_workflow.completion import lifecycle_report
    repo,worker,source,target,flags=fixture(tmp_path)
    prepare(repo);proofs(worker,source);publish(repo)
    git(repo,'add','.go');git(repo,'commit','-qm','durable workflow proof')
    clone=tmp_path/'clone';subprocess.run(['git','clone','--no-local','-q',str(repo),str(clone)],check=True)
    flags.write_text('{"unknown":true}')
    report=lifecycle_report(clone)
    assert report['evidence_valid'] and report['verified_done']==['task-schema-smoke'],report


def test_changed_remote_artifact_is_detected_without_redeploy(tmp_path,monkeypatch):
    import go_workflow.cli as api
    repo,worker,source,target,_=fixture(tmp_path)
    prepare(repo);proofs(worker,source);finish=api.cmd_finish
    monkeypatch.setattr(api,'cmd_finish',lambda *_: (_ for _ in ()).throw(RuntimeError('finish interrupted')))
    with pytest.raises(RuntimeError):publish(repo)
    payload=target.with_suffix('.pkg');original=payload.read_bytes();payload.write_bytes(b'wrong uploaded artifact')
    monkeypatch.setattr(api,'cmd_finish',finish)
    with pytest.raises(api.RepoLocalError,match='artifact'):publish(repo)
    assert source.exists() and json.loads(target.read_text())['effects']==1
    payload.write_bytes(original);assert publish(repo)['status']=='published'


def test_native_runner_resumes_deployment_with_frozen_separate_authority(tmp_path,monkeypatch):
    from test_abc_release import managed_fixture
    repo,_,worker,capture=managed_fixture(tmp_path,monkeypatch)
    spec,target,flags=adapter_profile(tmp_path)
    path=repo/'.go/project.json';project=json.loads(path.read_text());project['release_profiles']['fixture']['deployment']=spec
    path.write_text(json.dumps(project));git(repo,'add','.go/project.json');git(repo,'commit','-qm','explicit native deployment')
    git(repo,'push','origin','main');base=git(repo,'rev-parse','HEAD')
    def run(commands,initial=False):
        argv=[sys.executable,str(CLI),'auto',str(repo),'--execute','--task-id','task-schema-smoke','--agent','owner',
            '--executor-agent','codex','--max-commands',str(commands),'--max-minutes','5','--json']
        if initial:argv+=['--workspace-path',str(worker),'--workspace-branch','task/deployment','--base-branch','main',
            '--base-commit',base,'--run-id','resume-run','--ship-policy','push','--allow-push','--allow-deploy']
        result=subprocess.run(argv,cwd=repo,text=True,capture_output=True,timeout=120)
        assert result.returncode==0,result.stdout+result.stderr
        return json.loads(result.stdout)
    first=run(1,initial=True);assert first['status']=='budget_exhausted' and '--allow-deploy' in first['resume']['args']
    flags.write_text('{"delay":true}');second=run(20)
    assert second['status']=='resume_gate' and second['phase']=='release'
    assert json.loads(target.read_text())['effects']==1 and worker.exists()
    flags.write_text('{}');third=run(20)
    assert third['completed_tasks']==['task-schema-smoke'] and not worker.exists()
    assert capture.read_text().splitlines()==['build','critic']
    assert json.loads(target.read_text())['effects']==1
