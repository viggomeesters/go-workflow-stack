"""Executable Codex double: protocol events and observed inputs, never a model call."""
import json
import os
from pathlib import Path
import sys

if sys.argv[1] == 'app-server':
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        result = {} if request['method'] == 'initialize' else {'data': [
            {'model': 'gpt-6-astra', 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]},
            {'model': 'gpt-5.6-terra', 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]},
        ], 'nextCursor': None}
        print(json.dumps({'id': request['id'], 'result': result}), flush=True)
else:
    request = json.loads(os.environ['GO_ADAPTER_REQUEST_JSON'])
    snapshot = json.loads(Path(request['context_ref']['path']).read_text())
    phase = os.environ['GO_HOOK']
    capture = {'pid': os.getpid(), 'cwd': str(Path.cwd()), 'argv': sys.argv[1:],
               'context_ref': request['context_ref'], 'snapshot': snapshot}
    with Path(os.environ['ABC_TOPOLOGY_CAPTURE']).open('a') as stream:
        stream.write(json.dumps(capture) + '\n')
    if phase != 'critic':
        Path('app.txt').write_text(phase + ' completed')
    message = {'schema': 'go-workflow.agent-adapter-result.v1', 'phase': phase,
               'status': 'success', 'summary': 'topology fixture completed'}
    print(json.dumps({'type': 'thread.started', 'thread_id': 'ephemeral-fixture-' + str(os.getpid())}))
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(message)}}))
    print(json.dumps({'type': 'turn.completed'}))
