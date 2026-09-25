import json
import sys
from concurrent.futures import ThreadPoolExecutor
import pytest
from go_workflow.progress_events import ProgressOutbox
from go_workflow.progress_transport import preflight, flush, ProgressTransportError

@pytest.fixture
def repo(tmp_path):
    (tmp_path/'.go').mkdir()
    (tmp_path/'.go/project.json').write_text(json.dumps({'id':'project'}))
    return tmp_path

def recording(path):
    return {'schema':'go-workflow.progress-transport.v1','command':[sys.executable,'-m','go_workflow.progress_transport','record','--path',str(path)],'timeout_seconds':2}

def test_reference_capabilities_order_and_concurrent_retry(repo):
    config=recording(repo/'record.json')
    assert preflight(config)['capabilities']['user_chat'] is False
    box=ProgressOutbox(repo,'campaign')
    for i in range(4): box.emit('start',f'T{i}',{},key=str(i))
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:flush(repo,'campaign',config),range(2)))
    assert all(x['ok'] for x in results)
    assert [x['sequence'] for x in json.loads((repo/'record.json').read_text())['events']]==[1,2,3,4]
    assert box.pending()==[]

def test_missing_capabilities_and_timeout(repo):
    config=recording(repo/'record.json')
    config['command']=[sys.executable,'-c','print("{}")']
    with pytest.raises(ProgressTransportError,match='lacks'):preflight(config)
    config['command']=[sys.executable,'-c','import time; time.sleep(30)'];config['timeout_seconds']=0.1
    with pytest.raises(ProgressTransportError,match='timed out'):preflight(config)
    box=ProgressOutbox(repo,'campaign');box.emit('done','T1',{},key='done')
    assert not flush(repo,'campaign',config)['ok']
    assert len(box.pending())==1
    assert 'timed out' in (repo/'.go/runs/progress/campaign.transport.json').read_text()
    assert flush(repo,'campaign',recording(repo/'record.json'))['ok']

def test_lost_ack_retries_same_remote_event(repo,monkeypatch):
    box=ProgressOutbox(repo,'campaign');box.emit('done','T1',{},key='done')
    config=recording(repo/'record.json')
    original=ProgressOutbox.ack
    monkeypatch.setattr(ProgressOutbox,'ack',lambda *a: (_ for _ in ()).throw(OSError('lost ack')))
    assert not flush(repo,'campaign',config)['ok']
    monkeypatch.setattr(ProgressOutbox,'ack',original)
    assert flush(repo,'campaign',config)['ok']
    assert len(json.loads((repo/'record.json').read_text())['events'])==1

def test_concurrent_emission_is_reported_pending(repo,monkeypatch):
    import go_workflow.progress_transport as transport
    box=ProgressOutbox(repo,'campaign');box.emit('start','T1',{},key='first')
    original=transport._request
    def response(config,payload):
        result=original(config,payload)
        box.emit('phase','T1',{'phase':'verify'},key='later')
        return result
    monkeypatch.setattr(transport,'_request',response)
    result=flush(repo,'campaign',recording(repo/'record.json'))
    assert result['pending']==1 and not result['ok']

def test_interrupted_transport_cleans_process_group(repo,monkeypatch):
    import go_workflow.progress_transport as module
    class Interrupted:
        pid=123456789
        calls=0
        def communicate(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1:raise KeyboardInterrupt
            return ('','')
    process=Interrupted();killed=[]
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k:process)
    monkeypatch.setattr(module.os,'killpg',lambda pid,sig:killed.append(pid))
    with pytest.raises(KeyboardInterrupt):preflight(recording(repo/'record.json'))
    assert killed==[process.pid] and process.calls==2
