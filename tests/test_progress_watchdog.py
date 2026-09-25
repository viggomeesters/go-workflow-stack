import json
import os
import time
import pytest
from test_progress_transport import repo, recording
from go_workflow.progress_events import ProgressOutbox
from go_workflow.progress_watchdog import Watchdog, start, supervisor_alive, process_identity

class FakeClock:
    value=1000
    def now(self):return self.value

def setup(repo):
    path=repo/'.go/runs/campaigns/campaign/state.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'current_task':'T1'}))
    return path

def test_fake_clock_restart_unknown_and_outage(repo):
    path=setup(repo);clock=FakeClock();config=recording(repo/'record.json')
    watcher=Watchdog(repo,'campaign',path,config,clock=clock)
    watcher.tick();clock.value+=299;watcher.tick()
    assert ProgressOutbox(repo,'campaign').snapshot()['events']==[]
    clock.value+=1;watcher.tick()
    first=ProgressOutbox(repo,'campaign').snapshot()['events'][0]
    assert first['payload']['activity']=='unknown' and first['payload']['phase']=='unknown'
    watcher=Watchdog(repo,'campaign',path,config,clock=clock);watcher.tick()
    assert len(ProgressOutbox(repo,'campaign').snapshot()['events'])==1
    clock.value+=300;watcher.config={**config,'command':['/nonexistent']}
    assert not watcher.tick()['ok']
    assert len(ProgressOutbox(repo,'campaign').pending())==1
    watcher.config=config;assert watcher.tick()['ok']
    assert len(json.loads((repo/'record.json').read_text())['events'])==2

def test_independent_process_during_blocked_supervisor(repo):
    path=setup(repo)
    with start(repo,'campaign',path,recording(repo/'record.json'),heartbeat_seconds=0.2) as handle:
        time.sleep(0.75)
        assert handle.process.poll() is None
    assert len(json.loads((repo/'record.json').read_text())['events'])>=2
    assert handle.process.poll() is not None

def test_supervisor_identity_is_bound_to_host_and_start_token():
    identity=process_identity(os.getpid());assert supervisor_alive(identity)
    assert not supervisor_alive({**identity,'start_token':'wrong'})
    assert not supervisor_alive({**identity,'host':'other'})

@pytest.mark.skipif(os.environ.get('GO_REAL_HEARTBEAT_TEST')!='1',reason='explicit 600 second duration test')
def test_real_two_heartbeat_intervals(repo):
    import hashlib
    from pathlib import Path
    import go_workflow.progress_watchdog as watchdog_module
    import go_workflow.progress_transport as transport_module
    import go_workflow.progress_events as events_module
    sources=[Path(module.__file__) for module in (watchdog_module,transport_module,events_module)]
    hashes={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    (repo/'source-hashes.json').write_text(json.dumps(hashes,sort_keys=True))
    print('REAL_HEARTBEAT_SOURCE_HASHES '+json.dumps(hashes,sort_keys=True),flush=True)
    path=setup(repo)
    with start(repo,'campaign',path,recording(repo/'record.json')):
        deadline=time.monotonic()+615
        while time.monotonic()<deadline:
            time.sleep(1)
            record=repo/'record.json'
            if record.exists() and len(json.loads(record.read_text())['events'])>=2:break
        else:pytest.fail('two independently delivered 300-second heartbeats missing')
    events=json.loads((repo/'record.json').read_text())['events']
    assert len(events)>=2
    assert events[0]['payload']['elapsed_seconds']>=300
    assert events[1]['payload']['elapsed_seconds']>=600
    assert hashes=={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}

def test_consumer_cwd_imports_exact_runtime(repo,monkeypatch):
    path=setup(repo)
    monkeypatch.chdir(repo)
    monkeypatch.delenv('PYTHONPATH',raising=False)
    with start(repo,'campaign',path,recording(repo/'record.json'),heartbeat_seconds=0.1):
        time.sleep(0.35)
    assert json.loads((repo/'record.json').read_text())['events']

def test_clock_rollback_and_invalid_task_source(repo):
    path=setup(repo);path.write_text(json.dumps({'current_task':'../../outside'}))
    clock=FakeClock();watcher=Watchdog(repo,'campaign',path,recording(repo/'record.json'),clock=clock)
    watcher.tick();clock.value-=20;watcher.tick()
    event=ProgressOutbox(repo,'campaign').snapshot()['events'][0]
    assert event['task_id'] is None and event['payload']['activity']=='unknown'

def test_rejects_symlink_state(repo):
    path=setup(repo);path.unlink();other=repo/'other.json';other.write_text('{}');path.symlink_to(other)
    with pytest.raises(ValueError):Watchdog(repo,'campaign',path,recording(repo/'record.json'))

def test_subprocess_exits_when_supervisor_identity_is_invalid(repo):
    import subprocess,sys
    from go_workflow.progress_transport import runtime_environment
    path=setup(repo);config_path=repo/'config.json'
    identity=process_identity(os.getpid());identity['start_token']='not-this-process'
    config_path.write_text(json.dumps({'transport':recording(repo/'record.json'),'supervisor':identity}))
    proc=subprocess.Popen([sys.executable,'-m','go_workflow.progress_watchdog','--repo',str(repo),
        '--campaign-id','campaign','--state-path',str(path),'--config',str(config_path)],env=runtime_environment())
    assert proc.wait(timeout=3)==0
    assert not (repo/'record.json').exists()

def test_slow_delivery_is_not_started_across_observation_deadline(repo,monkeypatch):
    import go_workflow.progress_watchdog as module
    path=setup(repo);clock=FakeClock();config=recording(repo/'record.json');config['timeout_seconds']=60
    watcher=Watchdog(repo,'campaign',path,config,clock=clock);watcher.tick()
    calls=[]
    monkeypatch.setattr(module,'flush',lambda *a,**k:calls.append(True) or {'ok':True})
    clock.value+=250
    assert watcher.tick()['deferred'] and calls==[]
    clock.value+=50;watcher.tick()
    assert len(calls)==1 and len(ProgressOutbox(repo,'campaign').pending())==1

def test_diagnosis_does_not_call_live_long_test_stalled(repo):
    import socket
    path=setup(repo);managed=repo/'.go/runs/T1/run-state.json';managed.parent.mkdir(parents=True)
    managed.write_text(json.dumps({'phase':'verify','controller':{'host':socket.gethostname()},
        'worker_group':os.getpgrp(),'inflight':{'phase':'verify','started_at':1}}))
    clock=FakeClock();watcher=Watchdog(repo,'campaign',path,recording(repo/'record.json'),clock=clock)
    watcher.tick();clock.value+=300;watcher.tick()
    event=ProgressOutbox(repo,'campaign').snapshot()['events'][0]
    assert 'duration alone is not a stall' in event['payload']['diagnostic']
    assert 'current activity unknown' in event['payload']['activity']
