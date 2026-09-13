"""Model control observed at the subprocess boundary; no live worker calls."""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from go_workflow.cli import default_executor_agent_command


def task():
    return {'id':'controlled','execution_mode':'agent','execution_contract':{
        'schema':'go-workflow.execution-contract.v1','task_kind':'product',
        'model':{'id':'gpt-6-astra','effort':'high'},
        'critic_model':{'id':'gpt-5.6-terra','effort':'medium'},
        'release':{'mode':'required'}}}


def fake_codex(tmp_path, monkeypatch):
    binary=tmp_path/'codex'
    binary.write_text('#!'+sys.executable+'\nimport sys,json\nprint(json.dumps(sys.argv[1:]))\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    return binary


def test_build_process_receives_selected_model_and_reasoning(tmp_path, monkeypatch):
    fake_codex(tmp_path,monkeypatch)
    command=default_executor_agent_command('codex',task()).replace('{repo_shell}',shlex.quote(str(tmp_path)))
    result=subprocess.run(command,shell=True,cwd=tmp_path,text=True,capture_output=True,check=True)
    args=json.loads(result.stdout)
    assert '--model' in args
    assert args[args.index('--model')+1]=='gpt-6-astra'
    assert 'model_reasoning_effort="high"' in args


def controlled_fixture(tmp_path):
    import shutil
    repo=tmp_path/'project'
    shutil.copytree(Path(__file__).resolve().parents[1]/'fixtures/minimal',repo)
    return repo


def catalog_codex(tmp_path,monkeypatch):
    binary=tmp_path/'codex'
    binary.write_text('#!'+sys.executable+'''\nimport sys,json,os,pathlib
if sys.argv[1]=='app-server':
 for line in sys.stdin:
  request=json.loads(line)
  if 'id' not in request: continue
  result={} if request['method']=='initialize' else {'data':[{'id':'gpt-6-astra','model':'gpt-6-astra','supportedReasoningEfforts':[{'reasoningEffort':'high'}]},{'id':'gpt-5.6-terra','model':'gpt-5.6-terra','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}],'nextCursor':None}
  print(json.dumps({'id':request['id'],'result':result}),flush=True)
else:
 pathlib.Path(os.environ['ABC_MODEL_CAPTURE']).write_text(json.dumps(sys.argv[1:]))
 message=json.dumps({'schema':'go-workflow.agent-adapter-result.v1','phase':os.environ['GO_HOOK'],'status':'success','summary':'worker fixture completed','model_selection':{'effective':{'id':'invented'}},'usage':{'input_tokens':999}})
 mode=os.environ.get('ABC_MODEL_STREAM')
 if mode=='plain': print(message)
 else:
  print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':message}}))
  if mode=='failed': print(json.dumps({'type':'turn.failed','error':{'message':'fixture runtime failure'}}))
  elif mode=='1': print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'cached_input_tokens':4,'output_tokens':3}}))
  else: print(json.dumps({'type':'turn.completed'}))
''')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    capture=tmp_path/'worker-started.json';monkeypatch.setenv('ABC_MODEL_CAPTURE',str(capture))
    return capture


def test_unknown_critic_model_blocks_before_build_writes(tmp_path,monkeypatch):
    from go_workflow.cli import run_hook_command
    capture=catalog_codex(tmp_path,monkeypatch);repo=controlled_fixture(tmp_path)
    selected=task();selected['execution_contract']['critic_model']['id']='not-in-catalog'
    command=default_executor_agent_command('codex',selected)
    result=run_hook_command(repo,command,selected,1,'direct','build',require_protocol=True)
    assert result['returncode'] != 0
    assert not capture.exists()
    assert 'not-in-catalog' in result['stderr']


def test_changed_selection_is_rejected_between_phases(tmp_path,monkeypatch):
    from go_workflow.cli import run_hook_command
    capture=catalog_codex(tmp_path,monkeypatch);repo=controlled_fixture(tmp_path)
    selected=task();command=default_executor_agent_command('codex',selected)
    first=run_hook_command(repo,command,selected,1,'direct','build',require_protocol=True)
    assert first['returncode']==0, first
    capture.unlink()
    selected['execution_contract']['model']={'id':'gpt-5.6-terra','effort':'medium'}
    command=default_executor_agent_command('codex',selected)
    second=run_hook_command(repo,command,selected,2,'retry','build',require_protocol=True)
    assert second['returncode'] != 0
    assert not capture.exists()
    assert 'frozen' in second['stderr']


def test_runtime_usage_is_distinct_from_agent_model_claims(tmp_path,monkeypatch):
    from go_workflow.cli import run_hook_command
    catalog_codex(tmp_path,monkeypatch);repo=controlled_fixture(tmp_path)
    monkeypatch.setenv('ABC_MODEL_STREAM','1')
    selected=task();command=default_executor_agent_command('codex',selected)
    result=run_hook_command(repo,command,selected,1,'direct','build',require_protocol=True)
    assert result['returncode']==0, result
    assert result['model_selection']['requested']==selected['execution_contract']['model']
    assert result['model_selection']['effective'] is None
    assert result['model_selection']['confirmation']=='unconfirmed'
    assert result['usage']['turns']==[{'input_tokens':12,'cached_input_tokens':4,'output_tokens':3}]
    assert result['usage']['elapsed_seconds'] >= 0
    assert result['usage']['provider_invoice_cost_usd'] is None


def test_custom_critic_is_rejected_before_native_build(tmp_path,monkeypatch):
    capture=catalog_codex(tmp_path,monkeypatch);repo=controlled_fixture(tmp_path)
    path=repo/'.go/tasks/open/task-schema-smoke.json';record=json.loads(path.read_text())
    record['execution_mode']='agent';record['execution_contract']=task()['execution_contract']
    path.write_text(json.dumps(record))
    subprocess.run(['git','init','-q',str(repo)],check=True)
    monkeypatch.setenv('GO_STACK_ALLOW_DEV','1')  # disposable executor fixture only
    result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'cli/go.py'),
                           'auto',str(repo),'--execute','--allow-dirty','--executor-agent','codex',
                           '--critic-command','true','--max-attempts','1','--max-tasks','1','--json'],
                          text=True,capture_output=True)
    assert not capture.exists(), result.stdout+result.stderr
    assert 'unsupported' in result.stdout+result.stderr


@pytest.mark.parametrize('phase,model,effort', [('build','gpt-6-astra','high'),('repair','gpt-6-astra','high'),('critic','gpt-5.6-terra','medium')])
def test_each_phase_uses_frozen_profile_and_records_evidence(tmp_path,monkeypatch,phase,model,effort):
    from go_workflow.cli import run_hook_command, default_repair_agent_command, run_default_critic_agent
    capture=catalog_codex(tmp_path,monkeypatch);repo=controlled_fixture(tmp_path);selected=task()
    if phase=='critic':
        result=run_default_critic_agent(repo,'codex',selected,1,'direct',10)
    else:
        command=(default_executor_agent_command if phase=='build' else default_repair_agent_command)('codex',selected)
        result=run_hook_command(repo,command,selected,1,'direct',phase,require_protocol=True)
    assert result['returncode']==0, result
    args=json.loads(capture.read_text())
    assert args[args.index('--model')+1]==model
    assert 'model_reasoning_effort="'+effort+'"' in args
    assert result['model_selection']['requested']=={'id':model,'effort':effort}
    assert result['usage']['turns'] is None  # agent prose claiming 999 tokens is ignored
    events=[json.loads(line) for line in (repo/'.go/runs/events.jsonl').read_text().splitlines()]
    assert events[-1]['data']['action']=='model.phase_completed'
    assert events[-1]['data']['model_selection']['effective'] is None


@pytest.mark.parametrize('mutation', ['effort','hermes','custom'])
def test_unsupported_selection_does_not_start_worker(tmp_path,monkeypatch,mutation):
    from go_workflow.cli import run_hook_command
    capture=catalog_codex(tmp_path,monkeypatch);repo=controlled_fixture(tmp_path);selected=task()
    if mutation=='effort': selected['execution_contract']['model']['effort']='low'
    command=default_executor_agent_command('codex',selected)
    if mutation=='hermes': command='hermes -z fixture'
    result=run_hook_command(repo,command,selected,1,'direct','build',require_protocol=mutation!='custom')
    assert result['returncode'] != 0
    assert not capture.exists()
    assert not (repo/'.go/runs/controlled/model-selection.json').exists()


def test_actual_capability_reader_rejects_malformed_or_unresponsive_process(tmp_path):
    from go_workflow.model_profiles import codex_model_catalog
    bad=tmp_path/'bad-codex'
    bad.write_text('#!'+sys.executable+'\nprint("not json",flush=True)\n');bad.chmod(0o755)
    with pytest.raises(ValueError,match='malformed'):
        codex_model_catalog(str(bad),tmp_path,timeout_seconds=.5)
    bad.write_text('#!'+sys.executable+'\nimport time;time.sleep(10)\n')
    with pytest.raises(ValueError,match='timed out'):
        codex_model_catalog(str(bad),tmp_path,timeout_seconds=.1)


def test_protocol_schema_rejects_invalid_model_attribution():
    from jsonschema import Draft202012Validator
    schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/agent-adapter-result.schema.json').read_text())
    result={'schema':'go-workflow.agent-adapter-result.v1','phase':'build','status':'success','summary':'fixture',
            'model_selection':{'requested':{'id':'gpt-6-astra','effort':'bogus'},'effective':None,'confirmation':'unconfirmed','reason':'fixture','capabilities':{}}}
    assert not Draft202012Validator(schema).is_valid(result)


@pytest.mark.parametrize("stream_mode", ["failed", "plain"])
def test_runtime_failure_cannot_be_overruled_by_worker_success_text(tmp_path,monkeypatch,stream_mode):
    from go_workflow.cli import run_hook_command
    catalog_codex(tmp_path,monkeypatch);repo=controlled_fixture(tmp_path)
    monkeypatch.setenv('ABC_MODEL_STREAM',stream_mode)
    selected=task();command=default_executor_agent_command('codex',selected)
    result=run_hook_command(repo,command,selected,1,'direct','build',require_protocol=True)
    assert result['returncode'] != 0
    assert result['status'] != 'success'
