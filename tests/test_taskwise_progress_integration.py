"""Real serial delivery: native protocol workers, Git remote, deployments and chat."""
from copy import deepcopy
import hashlib
import json
import subprocess
import sys

import pytest

from go_workflow import cli as api
from go_workflow.campaign import execute_campaign
from go_workflow.campaign_intake import materialize_until_scope
from go_workflow.delivery_closure import inspect_closure
from go_workflow.run_state import execute_managed
from test_abc_resume import runner_fixture
from test_abc_worktrees import CLI, git


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=None if path.suffix=='.jsonl' else 2) + '\n')


def integration_fixture(tmp_path, monkeypatch, count=10):
    repo, _, _, phases = runner_fixture(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    worker = tmp_path / 'codex'
    script = worker.read_text()
    script = script.replace("phase=os.environ['GO_HOOK']", "phase=os.environ['GO_HOOK']; task=json.loads(os.environ['GO_TASK_JSON']); identity=task['id']")
    script = script.replace("output.write(phase+'\\n')", "output.write(identity+':'+phase+'\\n')")
    script = script.replace("if phase=='build': pathlib.Path('app.txt').write_text('built')", "if phase=='build':\n  pathlib.Path('products').mkdir(exist_ok=True)\n  pathlib.Path('products',identity+'.txt').write_text(identity+' delivered')")
    worker.write_text(script)
    tasks = [f'task-{number:02d}' for number in range(1, count + 1)]
    source = repo / '.go/tasks/open/task-schema-smoke.json'
    template = json.loads(source.read_text()); source.unlink()
    for identity in tasks:
        task = deepcopy(template)
        text = f'{identity} delivers its own verified product'
        task.update(id=identity, summary=text, description=text, acceptance=[text],
                    scope={'read':['.go/**','products/**','VERSION'], 'modify':[f'products/{identity}.txt','VERSION','CHANGELOG.md']},
                    outcome_tracking_version=1,
                    intent_source={'text':text,'sha256':hashlib.sha256(text.encode()).hexdigest(),'source_ref':'user:integration'},
                    requested_outcomes=[{'id':'R1','text':text,'status':'pending','source':'intake_acceptance','evidence':[]}],
                    verification=[f"python3 -c \"from pathlib import Path; assert Path('products/{identity}.txt').read_text() == '{identity} delivered'\""])
        task['execution_contract']['task_kind']='mechanical'
        task['execution_contract']['release']={'mode':'required','profile':'fixture'}
        write(repo/'.go/tasks/open'/f'{identity}.json',task)
    hierarchy_path=repo/'.go/hierarchy.json';hierarchy=json.loads(hierarchy_path.read_text())
    hierarchy['epics'][0]['features'][0]['tasks']=tasks
    write(hierarchy_path,hierarchy)
    write(repo/'.go/decisions/events.jsonl',{'schema':'go-workflow.repo-local.event.v1','kind':'event',
          'event':'decision.recorded','created_at':'2026-09-25T00:00:00Z','task_id':tasks[0],'agent':'fixture',
          'data':{'decision_id':'serial-integration','status':'accepted','decision':'Deliver each adopted task separately before starting another.'}})
    deployments=tmp_path/'deployments';deployments.mkdir()
    adapter=tmp_path/'deployment_adapter.py'
    adapter.write_text('''import sys,json,pathlib
operation,directory,key,target,commit,version=sys.argv[1:]
path=pathlib.Path(directory)/(key+'.json')
expected={'schema':'go-workflow.deployment-observation.v1','idempotency_key':key,'target':target,'commit':commit,'version':version,'artifact_sha256':None}
if operation=='deploy':
 if not path.exists():path.write_text(json.dumps(dict(expected,status='live',authorized=True,available=True,effects=1)))
 print(json.dumps({'accepted':True}))
else:
 print(path.read_text() if path.exists() else json.dumps(dict(expected,status='absent',authorized=True,available=False)))
''')
    base=[sys.executable,str(adapter),None,str(deployments),'{idempotency_key}','{target}','{commit}','{version}']
    deploy=base.copy();deploy[2]='deploy';observe=base.copy();observe[2]='observe'
    project_path=repo/'.go/project.json';project=json.loads(project_path.read_text())
    project['release_profiles']={'fixture':{'provider':'git-tag','remote':'origin','branch':'main',
       'publication':{'version':{'path':'VERSION','format':'text'},'bump':'patch','tag_prefix':'v','changelog':'CHANGELOG.md'},
       'deployment':{'mode':'required','target':'local-integration','recovery_policy':'resume_only','required_env':[],
                     'deploy':{'argv':deploy,'idempotency':'required'},'observe':{'argv':observe,'read_only':True}}}}
    write(project_path,project)
    (repo/'VERSION').write_text('1.0.0\n');(repo/'CHANGELOG.md').write_text('# Changes\n')
    remote=tmp_path/'remote.git';subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
    git(repo,'remote','add','origin',str(remote));git(repo,'add','.');git(repo,'commit','-qm','Ten independently deliverable tasks')
    git(repo,'tag','-a','v1.0.0','-m','baseline');git(repo,'push','origin','main','refs/tags/v1.0.0')
    return repo,tasks,phases,deployments


def recording_transport(tmp_path, fail_task='task-05'):
    """Lost acknowledgement: delivery happened, but first run cannot confirm it."""
    records=tmp_path/'chat.json';blocked=tmp_path/'chat-blocked';blocked.touch()
    adapter=tmp_path/'chat_adapter.py'
    adapter.write_text('''import sys,json,pathlib
# This disposable test adapter persists one receipt identity per event.
request=json.loads(sys.stdin.read()); records=pathlib.Path(sys.argv[1]); blocked=pathlib.Path(sys.argv[2]); fail_task=sys.argv[3]
if request['operation']=='preflight':
 print(json.dumps({'capabilities':{'independent_messages':True,'idempotent_event_ids':True,'user_chat':False}}));sys.exit(0)
event=request['event']; value=json.loads(records.read_text()) if records.exists() else {'events':[]}
if not any(item['id']==event['id'] for item in value['events']):
 value['events'].append(event); temp=records.with_suffix('.tmp');temp.write_text(json.dumps(value));temp.replace(records)
if blocked.exists() and event.get('kind')=='done' and event.get('task_id')==fail_task:
 print('delivered, acknowledgement unavailable',file=sys.stderr);sys.exit(3)
print(json.dumps({'event_id':event['id'],'receipt':event['id']}))
''')
    return {'schema':'go-workflow.progress-transport.v1','command':[sys.executable,str(adapter),str(records),str(blocked),fail_task],'timeout_seconds':10},records,blocked


def campaign_args(repo,path,tmp_path):
    return api.build_parser().parse_args(['auto',str(repo),'--execute','--campaign',str(path),
        '--campaign-workspace-root',str(tmp_path/'workspaces'),'--agent','owner','--executor-agent','codex',
        '--max-commands','50','--max-minutes','10','--json'])


def assert_real_deliveries(repo, tasks, deployments):
    observations=[json.loads(path.read_text()) for path in deployments.glob('*.json')]
    assert len(observations)==len(tasks)
    assert all(item['effects']==1 and item['status']=='live' for item in observations)
    for identity in tasks:
        closure=inspect_closure(repo,identity)
        assert closure['delivered'],closure
        task=json.loads((repo/'.go/tasks/done'/f'{identity}.json').read_text())
        receipt=task['release_receipt']
        assert git(repo,'rev-parse',receipt['tag']+'^{commit}')==receipt['commit']
        assert any(item['commit']==receipt['commit'] for item in observations)
        assert git(repo,'show',receipt['commit']+':products/'+identity+'.txt')==identity+' delivered'


def test_ten_real_deliveries_pause_at_fifth_chat_ack_then_resume_new_process(tmp_path,monkeypatch):
    repo,tasks,phases,deployments=integration_fixture(tmp_path,monkeypatch)
    transport,records,blocked=recording_transport(tmp_path)
    contract=materialize_until_scope(repo,intent='Go tot alle taken klaar',source_ref='user:integration',campaign_id='ten-deliveries')
    contract['execution']['progress']={'schema':'go-workflow.campaign-progress.v1','heartbeat_seconds':300,'transport':transport}
    path=tmp_path/'campaign.json';write(path,contract)
    args=campaign_args(repo,path,tmp_path)
    _,first=execute_campaign(repo,args,'go-auto',api,execute_managed)
    assert first['status']=='authority_required',first
    assert first['progress_delivery']['ok'] is False
    assert phases.read_text().splitlines()==[f'{identity}:{phase}' for identity in tasks[:5] for phase in ('build','critic')]
    assert_real_deliveries(repo,tasks[:5],deployments)
    clone=tmp_path/'independent-readback'
    subprocess.run(['git','clone','-q','--branch','main',str(tmp_path/'remote.git'),str(clone)],check=True)
    assert_real_deliveries(clone,tasks[:5],deployments)
    delivered_before=json.loads(records.read_text())['events']
    assert len([event for event in delivered_before if event['kind']=='done'])==5
    assert not any(event['kind']=='start' and event.get('task_id')==tasks[5] for event in delivered_before)
    blocked.unlink()
    resumed=subprocess.run([sys.executable,str(CLI),'auto',str(repo),'--execute','--campaign',str(path),
        '--campaign-workspace-root',str(tmp_path/'workspaces'),'--agent','owner','--executor-agent','codex',
        '--max-commands','50','--max-minutes','10','--json'],cwd=tmp_path,text=True,capture_output=True,timeout=360)
    assert resumed.returncode==0,resumed.stdout+resumed.stderr
    result=json.loads(resumed.stdout)
    assert result['goal_verified'] is True,result
    assert_real_deliveries(repo,tasks,deployments)
    assert phases.read_text().splitlines()==[f'{identity}:{phase}' for identity in tasks for phase in ('build','critic')]
    events=json.loads(records.read_text())['events']
    assert len({event['id'] for event in events})==len(events)
    done=[event for event in events if event['kind']=='done']
    assert [event['task_id'] for event in done]==tasks
    for previous,following in zip(tasks,tasks[1:]):
        assert next(i for i,e in enumerate(events) if e['kind']=='done' and e.get('task_id')==previous)<next(i for i,e in enumerate(events) if e['kind']=='start' and e.get('task_id')==following)


def test_repeated_real_failure_blocks_dependents_but_delivers_independent_task(tmp_path,monkeypatch):
    repo,tasks,phases,deployments=integration_fixture(tmp_path,monkeypatch,count=3)
    first_path=repo/'.go/tasks/open'/f'{tasks[0]}.json';first=json.loads(first_path.read_text())
    first['verification']=["python3 -c \"raise AssertionError('irreparable fixture prerequisite')\""]
    write(first_path,first)
    second_path=repo/'.go/tasks/open'/f'{tasks[1]}.json';second=json.loads(second_path.read_text())
    second['dependencies']=[{'project':second['project'],'task_id':tasks[0],'requires':'done_with_required_release_evidence'}]
    write(second_path,second)
    git(repo,'add','.go');git(repo,'commit','-qm','Real failure with one dependent and one independent task');git(repo,'push','origin','main')
    transport,records,blocked=recording_transport(tmp_path,fail_task='no-task');blocked.unlink()
    contract=materialize_until_scope(repo,intent='Go tot alle taken klaar',source_ref='user:integration',campaign_id='failed-prerequisite')
    contract['execution']['progress']={'schema':'go-workflow.campaign-progress.v1','heartbeat_seconds':300,'transport':transport}
    path=tmp_path/'campaign.json';write(path,contract)
    args=campaign_args(repo,path,tmp_path)
    _,result=execute_campaign(repo,args,'go-auto',api,execute_managed)
    assert result['goal_verified'] is False
    assert (repo/'.go/tasks/blocked'/f'{tasks[0]}.json').is_file(),result
    assert (repo/'.go/tasks/open'/f'{tasks[1]}.json').is_file()
    assert_real_deliveries(repo,[tasks[2]],deployments)
    calls=phases.read_text().splitlines()
    assert not any(line.startswith(tasks[1]+':') for line in calls)
    assert calls.count(tasks[0]+':build')==1
    assert calls.count(tasks[0]+':repair')<=2
    state=json.loads((repo/'.go/runs/campaigns/failed-prerequisite/state.json').read_text())
    assert any(event['event']=='campaign.no_progress_isolated' for event in state['history'])
    events=json.loads(records.read_text())['events']
    assert any(event['kind']=='block' and event.get('task_id')==tasks[0] for event in events)
    assert [event['task_id'] for event in events if event['kind']=='done']==[tasks[2]]


@pytest.mark.parametrize('boundary',['suspension_checkpoint','task_block_record'])
def test_campaign_recovers_suspension_ack_without_replaying_blocked_worker(tmp_path,monkeypatch,boundary):
    from go_workflow import release
    repo,tasks,phases,deployments=integration_fixture(tmp_path,monkeypatch,count=2)
    first_path=repo/'.go/tasks/open'/f'{tasks[0]}.json';first=json.loads(first_path.read_text())
    first['verification']=["python3 -c \"raise AssertionError('fixed failure fingerprint')\""]
    write(first_path,first)
    git(repo,'add','.go');git(repo,'commit','-qm','Failing candidate must not monopolize release channel');git(repo,'push','origin','main')
    transport,records,blocked=recording_transport(tmp_path,fail_task='no-task');blocked.unlink()
    contract=materialize_until_scope(repo,intent='Go tot alle taken klaar',source_ref='user:integration',campaign_id='suspension-crash')
    contract['execution']['progress']={'schema':'go-workflow.campaign-progress.v1','heartbeat_seconds':300,'transport':transport}
    path=tmp_path/'campaign.json';write(path,contract)
    args=campaign_args(repo,path,tmp_path)
    lost=False
    if boundary=='suspension_checkpoint':
        real=release._release_reservation
        def interrupt(control,state):
            nonlocal lost
            if state['phase']=='suspended' and not lost:
                lost=True
                raise OSError('lost acknowledgement after suspension checkpoint')
            return real(control,state)
        monkeypatch.setattr(release,'_release_reservation',interrupt)
        _,stopped=execute_campaign(repo,args,'go-auto',api,execute_managed)
        assert stopped['goal_verified'] is False
        assert (repo/'.go/tasks/active'/f'{tasks[0]}.json').exists()
    else:
        real=api.block_task_record
        def interrupt(*values,**keywords):
            nonlocal lost
            result=real(*values,**keywords)
            if not lost:
                lost=True
                raise OSError('lost acknowledgement after task block')
            return result
        monkeypatch.setattr(api,'block_task_record',interrupt)
        with pytest.raises(OSError,match='after task block'):
            execute_campaign(repo,args,'go-auto',api,execute_managed)
        assert (repo/'.go/tasks/blocked'/f'{tasks[0]}.json').exists()
    assert lost
    before=phases.read_text().splitlines()
    suspension=json.loads((repo/'.go/runs'/tasks[0]/'release-state.json').read_text())
    assert suspension['phase']=='suspended'
    _,resumed=execute_campaign(repo,args,'go-auto',api,execute_managed)
    assert resumed['goal_verified'] is False
    assert_real_deliveries(repo,[tasks[1]],deployments)
    after=phases.read_text().splitlines()
    assert [line for line in after if line.startswith(tasks[0]+':')]==before
    assert after[len(before):]==[tasks[1]+':build',tasks[1]+':critic']
    state=json.loads((repo/'.go/runs/campaigns/suspension-crash/state.json').read_text())
    assert len([item for item in state['history'] if item['event']=='campaign.no_progress_isolated' and item.get('task_id')==tasks[0]])==1
