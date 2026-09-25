"""TTY projection of the canonical progress outbox; never a task controller."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import tempfile
import sys
import time
import unicodedata
import uuid

from .progress_events import ProgressOutbox
from .progress_transport import _safe_path
from .progress_watchdog import process_identity, supervisor_alive
from .state_io import atomic_json, repository_lock

VIEW_SCHEMA = 'go-workflow.terminal-view.v1'


def terminal_text(text):
    # Task text is data: suppress OSC/ANSI, C1 controls and directional overrides.
    return ''.join(c if c in '\n\t' or not unicodedata.category(c).startswith('C')
                   else f'\\u{ord(c):04x}' for c in text)


def event_digest(event):
    body = {k: v for k, v in event.items() if k not in {'status', 'receipt'}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _view_path(box):
    path = box.path.with_suffix('.terminal.json')
    _safe_path(path)
    return path


def _read_view(box):
    path = _view_path(box)
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get('schema') != VIEW_SCHEMA
            or value.get('project_id') != box.project_id or value.get('campaign_id') != box.campaign_id
            or value.get('repo') != str(box.repo) or not isinstance(value.get('displayed'), dict)
            or not isinstance(value.get('viewer'), dict) or not isinstance(value.get('session'), str)
            or not isinstance(value.get('updated_at'), (int, float))):
        raise ValueError('Invalid terminal display checkpoint')
    return value


def _live(view):
    return bool(view and view.get('ready') is True and view.get('tty')
                and 0 <= time.time() - view['updated_at'] < 5
                and supervisor_alive(view['viewer']))


def watch(repo, campaign_id):
    if not sys.stdout.isatty():
        raise ValueError('Progress display requires an attached terminal (TTY)')
    box = ProgressOutbox(repo, campaign_id)
    identity = process_identity(os.getpid())
    if identity is None:
        raise ValueError('Cannot establish terminal viewer process identity')
    path = _view_path(box)
    # One receipt writer per campaign; closing a terminal releases this OS lock.
    with repository_lock(box.root, 'progress-terminal-view-' + campaign_id, timeout_seconds=.2):
        prior = _read_view(box)
        state = {'schema': VIEW_SCHEMA, 'project_id': box.project_id, 'campaign_id': campaign_id,
                 'repo': str(box.repo), 'viewer': identity, 'session': uuid.uuid4().hex,
                 'tty': os.ttyname(sys.stdout.fileno()), 'updated_at': time.time(),
                 'ready': False, 'displayed': prior.get('displayed', {})}
        atomic_json(path, state)
        print(terminal_text(f'Go — {box.project_id} / {campaign_id}\nHeropenen toont opgeslagen meldingen opnieuw; dit is geen nieuwe uitvoering.\nSluiten stopt alleen dit venster. Niet-afgeleverde meldingen houden de volgende taak tegen.'), flush=True)
        cursor = 0
        last_write = 0
        while True:
            events = box.snapshot()['events']
            changed = False
            for event in events[cursor:]:
                digest = event_digest(event)
                old = state['displayed'].get(event['id'])
                if old and old != digest:
                    raise ValueError('Displayed event identity changed content')
                print(terminal_text(f"[{event['created_at']}] {event['message']}"), flush=True)
                # A real TTY write and flush precedes every rendering receipt.
                state['displayed'][event['id']] = digest
                cursor = event['sequence']
                changed = True
            now = time.monotonic()
            if changed or now - last_write >= 1:
                state.update(ready=True, updated_at=time.time())
                _safe_path(path)
                atomic_json(path, state)
                last_write = now
            time.sleep(.1)


def request(repo, campaign_id, payload, *, auto_open=True):
    box = ProgressOutbox(repo, campaign_id)
    operation = payload.get('operation')
    if operation == 'preflight':
        _ensure_viewer(box, auto_open)
        return {'capabilities': {'independent_messages': True, 'idempotent_event_ids': True,
                'user_chat': False, 'user_terminal': True}, 'transport': 'live-terminal'}
    if operation != 'deliver':
        raise ValueError('Unknown terminal transport operation')
    event = payload.get('event')
    if not isinstance(event, dict):
        raise ValueError('Terminal delivery requires an existing outbox event')
    original = box.get(event.get('key'))
    if original is None or event_digest(original) != event_digest(event):
        raise ValueError('Terminal delivery must match the canonical outbox')
    digest = event_digest(event)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        view = _read_view(box)
        if not _live(view):
            raise ValueError('Terminal viewer closed or unresponsive; progress remains pending')
        displayed = view['displayed'].get(event['id'])
        if displayed:
            if displayed != digest:
                raise ValueError('Terminal rendering receipt differs from event')
            return {'event_id': event['id'], 'receipt': 'terminal:' + event['id'] + ':' + digest,
                    'surface': 'terminal', 'displayed': True}
        time.sleep(.05)
    raise ValueError('Terminal did not acknowledge rendering; progress remains pending')


def transport_config(repo, campaign_id, *, auto_open=True):
    box = ProgressOutbox(repo, campaign_id)
    argv = [sys.executable, '-m', 'go_workflow.progress_terminal', 'transport',
            '--repo', str(box.repo), '--campaign', campaign_id]
    if not auto_open:
        argv.append('--no-auto-open')
    return {'schema': 'go-workflow.progress-transport.v1', 'command': argv, 'timeout_seconds': 15}


def resolve_transport(repo, campaign_id, configured):
    if isinstance(configured, dict) and configured.get('schema') == 'go-workflow.terminal-progress.v1':
        if set(configured) != {'schema', 'auto_open'} or type(configured.get('auto_open')) is not bool:
            raise ValueError('Terminal preference requires boolean auto_open')
        return transport_config(repo, campaign_id, auto_open=configured['auto_open'])
    return configured


def enable_terminal(repo):
    path = Path(repo).resolve() / '.go/project.json'
    _safe_path(path)
    with repository_lock(path.parent, 'progress-configuration'):
        project = json.loads(path.read_text())
        previous = project.get('progress_transport')
        choice = {'schema': 'go-workflow.terminal-progress.v1', 'auto_open': True}
        if previous is not None and previous != choice:
            raise ValueError('Existing progress transport preserved; explicitly reconcile it before selecting terminal')
        ignore = path.parent.parent / '.gitignore'
        _safe_path(ignore)
        text = ignore.read_text() if ignore.exists() else ''
        pattern = '.go/runs/progress/*.terminal.json'
        if pattern not in text.splitlines():
            from .state_io import atomic_write_text
            atomic_write_text(ignore, (text.rstrip('\n') + '\n' + pattern + '\n'))
        project['progress_transport'] = choice
        atomic_json(path, project)
    return {'configured': choice, 'existing_campaigns_changed': False,
            'behavior': 'New Go campaigns open a live terminal during preflight'}


def _launch_native(box):
    """Launch only a fixed viewer command; no event text is executable input."""
    argv = [sys.executable, '-m', 'go_workflow.progress_terminal', 'watch',
            '--repo', str(box.repo), '--campaign', box.campaign_id]
    from .progress_transport import runtime_environment
    if sys.platform == 'darwin':
        directory = tempfile.TemporaryDirectory(prefix='go-progress-terminal-')
        script = Path(directory.name) / 'Go progress.command'
        script.write_text('#!/bin/sh\nexport PYTHONPATH=' + shlex.quote(str(Path(__file__).resolve().parents[1]))
                          + '\nexec ' + shlex.join(argv) + '\n')
        script.chmod(0o700)
        try:
            subprocess.run(['/usr/bin/open', '-a', '/System/Applications/Utilities/Terminal.app', str(script)],
                           check=True, capture_output=True, timeout=5)
        except BaseException:
            directory.cleanup()
            raise
        return directory.cleanup
    executable = shutil.which('x-terminal-emulator')
    if executable and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        subprocess.Popen([executable, '-e', *argv], env=runtime_environment(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return lambda: None
    raise ValueError('No desktop terminal launcher available; open progress watch in an attached terminal')


def _ensure_viewer(box, auto_open):
    with repository_lock(box.root, 'progress-terminal-open-' + box.campaign_id):
        view = _read_view(box)
        if _live(view):
            return
        if view and view['viewer'].get('host') != socket.gethostname():
            raise ValueError('Terminal viewer belongs to another host; reconcile ownership explicitly')
        cleanup = lambda: None
        try:
            # A live but stuck/starting viewer must never acquire a duplicate writer.
            if auto_open and not supervisor_alive(view.get('viewer')):
                cleanup = _launch_native(box)
            deadline = time.monotonic() + (5 if auto_open else 2)
            while time.monotonic() < deadline:
                if _live(_read_view(box)):
                    return
                time.sleep(.05)
            raise ValueError('No live terminal viewer; open progress watch for this campaign before resuming')
        finally:
            cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['transport', 'watch'])
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--campaign', required=True)
    parser.add_argument('--no-auto-open', action='store_true')
    args = parser.parse_args()
    try:
        if args.operation == 'watch':
            watch(args.repo, args.campaign)
        else:
            print(json.dumps(request(args.repo, args.campaign, json.load(sys.stdin), auto_open=not args.no_auto_open)))
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
