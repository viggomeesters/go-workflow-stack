"""Deterministic native CLI double; same protocol path as live campaign."""
import json
import os
from pathlib import Path
import sys

if sys.argv[1]=='app-server':
    for line in sys.stdin:
        request=json.loads(line)
        if 'id' not in request:continue
        result={} if request['method']=='initialize' else {'data':[
            {'model':'gpt-5.6-terra','supportedReasoningEfforts':[{'reasoningEffort':'high'}]},
            {'model':'gpt-6-astra','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}], 'nextCursor':None}
        print(json.dumps({'id':request['id'],'result':result}),flush=True)
else:
    request=json.loads(os.environ['GO_ADAPTER_REQUEST_JSON']);snapshot=json.loads(Path(request['context_ref']['path']).read_text())
    task=snapshot['context']['task'];phase=os.environ['GO_HOOK'];capture=Path(os.environ['ABC_CAMPAIGN_CAPTURE'])
    previous=[json.loads(line) for line in capture.read_text().splitlines()] if capture.exists() else []
    missing_boundary=phase=='critic' and 'This critic runs before controller-owned publication.' not in sys.argv[-1]
    reject=missing_boundary or (phase=='critic' and task['id']=='campaign-1' and not any(x['phase']=='critic' for x in previous))
    if phase in {'build','repair'}:Path('app.txt').write_text('one\n' if task['id']=='campaign-1' else 'two\n')
    with capture.open('a') as out:out.write(json.dumps({'task_id':task['id'],'phase':phase,'cwd':str(Path.cwd()),'argv':sys.argv[1:],'feedback':snapshot.get('feedback'),'context_ref':request['context_ref']})+'\n')
    message={'schema':'go-workflow.agent-adapter-result.v1','phase':phase,'status':'blocked' if reject else 'success','summary':'Please recheck current app content: forced critic finding C1' if reject else 'campaign step checked'}
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(message)}}))
    print(json.dumps({'type':'turn.completed'}))
