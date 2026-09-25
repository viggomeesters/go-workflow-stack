"""Bounded JSON command transport and an explicitly local recording adapter."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from .state_io import atomic_json, repository_lock

SCHEMA = 'go-workflow.progress-transport.v1'

class ProgressTransportError(ValueError):
    pass


def _safe_path(path):
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ProgressTransportError(f'symlink progress path: {component}')


def runtime_environment():
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[1])
    env['PYTHONPATH'] = root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    return env


def _request(config, payload):
    if not isinstance(config, dict) or config.get('schema') != SCHEMA:
        raise ProgressTransportError('unsupported progress transport schema')
    argv = config.get('command')
    timeout = config.get('timeout_seconds', 10)
    if (not isinstance(argv, list) or not argv or
        any(not isinstance(x, str) or not x or '\x00' in x for x in argv) or
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60):
        raise ProgressTransportError('invalid command or bounded timeout')
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True, env=runtime_environment())
    try:
        stdout, stderr = proc.communicate(json.dumps(payload), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise ProgressTransportError('progress transport command timed out') from exc
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    if proc.returncode:
        raise ProgressTransportError(f'progress transport exit {proc.returncode}: {stderr[-1000:]}')
    try:
        result = json.loads(stdout)
    except ValueError as exc:
        raise ProgressTransportError('progress transport returned invalid JSON') from exc
    if not isinstance(result, dict):
        raise ProgressTransportError('progress transport response must be an object')
    return result


def preflight(config):
    result = _request(config, {'operation': 'preflight', 'schema': SCHEMA})
    caps = result.get('capabilities', {})
    if not all(caps.get(k) is True for k in ('independent_messages', 'idempotent_event_ids')):
        raise ProgressTransportError('transport lacks independent_messages or idempotent_event_ids')
    return result


def flush(repo, campaign_id, config, *, max_events=None, lock_timeout=65):
    from .progress_events import ProgressOutbox
    repo = Path(repo)
    outbox = ProgressOutbox(repo, campaign_id)
    status_path = repo / '.go/runs/progress' / f'{campaign_id}.transport.json'
    _safe_path(status_path)
    delivered = 0
    with repository_lock(repo / '.go', f'progress-transport-{campaign_id}', timeout_seconds=lock_timeout):
        try:
            for event in outbox.pending()[:max_events]:
                response = _request(config, {'operation': 'deliver', 'schema': SCHEMA, 'event': event})
                if response.get('event_id') != event['id'] or not response.get('receipt'):
                    raise ProgressTransportError('transport did not acknowledge exact event identity')
                outbox.ack(event['id'], response)
                delivered += 1
            pending = len(outbox.pending())
            result = {'ok': pending == 0, 'delivered': delivered, 'pending': pending}
        except (OSError, ValueError) as exc:
            result = {'ok': False, 'delivered': delivered, 'pending': len(outbox.pending()), 'error': str(exc)}
        atomic_json(status_path, result)
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['record'])
    parser.add_argument('--path', type=Path, required=True)
    args = parser.parse_args()
    _safe_path(args.path)
    request = json.load(sys.stdin)
    if request['operation'] == 'preflight':
        response = {'capabilities': {'independent_messages': True, 'idempotent_event_ids': True,
                                     'user_chat': False}, 'transport': 'local-recording-reference'}
    elif request['operation'] == 'deliver':
        event = request['event']
        with repository_lock(args.path.parent, 'recording-' + args.path.name):
            data = json.loads(args.path.read_text()) if args.path.exists() else {'events': []}
            prior = next((e for e in data['events'] if e['id'] == event['id']), None)
            if prior is not None and prior != event:
                raise ProgressTransportError('event identity reused with changed content')
            if prior is None:
                data['events'].append(event)
                atomic_json(args.path, data)
        response = {'event_id': event['id'], 'receipt': event['id']}
    else:
        raise ProgressTransportError('unknown operation')
    print(json.dumps(response))

if __name__ == '__main__':
    main()
