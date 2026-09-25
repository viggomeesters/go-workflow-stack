"""Ordered durable progress outbox; transport acknowledgements are separate effects."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .state_io import atomic_json, repository_lock

EVENT_SCHEMA = 'go-workflow.progress-event.v1'
STATE_SCHEMA = 'go-workflow.progress-state.v1'
KINDS = ('run_start', 'start', 'phase', 'heartbeat', 'done', 'block', 'repair', 'amend', 'resume', 'run_end')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('Invalid progress identity')
    return value


def _object(value):
    if not isinstance(value, dict):
        raise ValueError('Progress payload/receipt must be a JSON object')
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError('Progress payload/receipt must contain finite JSON values') from exc


def render_message(kind, task_id, payload):
    """Human text is derived from durable facts, never an independent progress claim."""
    if kind not in KINDS:
        raise ValueError('Unknown progress event kind')
    task = task_id or 'Run'
    detail = payload.get('summary') or payload.get('result') or payload.get('title') or 'onbekend'
    if kind == 'run_end':
        lines = [f'Run afgesloten — {detail}.']
        originals = payload.get('original_task_ids', [])
        added = payload.get('added_task_ids', [])
        lines.append('Oorspronkelijke taken: ' + (', '.join(originals) or 'onbekend') + '.')
        if added:
            lines.append('Toegevoegd herstelwerk: ' + ', '.join(added) + '.')
        for result in payload.get('results', []):
            publication = result.get('publication') or {}
            shipping = '; '.join(f'{name}: {publication.get(name, "onbekend")}'
                                for name in ('commit', 'push', 'release', 'deployment')) if isinstance(publication, dict) else str(publication)
            lines.append(f"{result.get('task_id', 'onbekend')} done — {result.get('summary') or 'onbekend'}. "
                         f"Controles: {result.get('checks') or 'onbekend'}; {shipping}.")
        for item in payload.get('remaining_tasks', []):
            lines.append(f"{item.get('task_id', 'onbekend')} — {item.get('status') or 'onbekend'}; nog niet afgerond.")
        for change in payload.get('changes', []):
            identity = change.get('task_id') or change.get('repair_task_id') or change.get('parent_id') or 'onbekend'
            reason = change.get('reason') or change.get('summary') or 'reden onbekend'
            lines.append(f"Wijziging {identity} ({change.get('event', 'onbekend')}) — {reason}.")
        return '\n'.join(lines)
    if kind == 'heartbeat':
        return (f"{task} heartbeat — fase: {payload.get('phase') or 'onbekend'}; "
                f"verstreken: {payload.get('elapsed_seconds', 'onbekend')} seconden; "
                f"activiteit: {payload.get('activity') or 'onbekend'}; "
                f"laatste bevestigde voortgang: {payload.get('confirmed_progress') or 'onbekend'}.")
    if kind == 'done':
        return (f"{task} done — {detail}. Controles: {payload.get('checks') or 'onbekend'}; "
                f"commit/push/deployment: {payload.get('publication') or 'onbekend'}.")
    labels = {'start': 'in progress', 'phase': 'fase ' + str(payload.get('phase') or 'onbekend'),
              'block': 'geblokkeerd', 'repair': 'herstel', 'amend': 'bijgewerkt',
              'resume': 'hervat', 'run_start': 'gestart', 'run_end': 'afgesloten'}
    return f'{task} {labels[kind]} — {detail}.'


def _safe(path, root):
    if not path.is_relative_to(root):
        raise ValueError('Progress path escapes repository')
    for node in (path, *path.parents):
        if node == root:
            break
        if node.is_symlink():
            raise ValueError('Progress paths cannot traverse symlinks')
    return path


class ProgressOutbox:
    def __init__(self, repo, campaign_id):
        self.repo = Path(repo).resolve()
        self.campaign_id = _identifier(campaign_id)
        self.root = _safe(self.repo / '.go', self.repo)
        project = _safe(self.root / 'project.json', self.repo)
        self.project_id = _identifier(json.loads(project.read_text())['id'])
        self.path = _safe(self.root / 'runs/progress' / (campaign_id + '.json'), self.repo)

    def _lock(self):
        _safe(self.path, self.repo)
        lock = repository_lock(self.root, 'progress-' + self.campaign_id)
        # repository_lock may use the Git common directory in another checkout.
        for node in (lock.path, *lock.path.parents):
            if node.is_symlink():
                raise ValueError('Progress lock cannot traverse symlinks')
        return lock

    def _read(self):
        _safe(self.path, self.repo)
        if not self.path.exists():
            return {'schema': STATE_SCHEMA, 'project_id': self.project_id,
                    'campaign_id': self.campaign_id, 'next_sequence': 1, 'events': []}
        value = json.loads(self.path.read_text())
        expected = {'schema', 'project_id', 'campaign_id', 'next_sequence', 'events'}
        if (not isinstance(value, dict) or set(value) != expected or value['schema'] != STATE_SCHEMA
                or value['project_id'] != self.project_id or value['campaign_id'] != self.campaign_id
                or not isinstance(value['events'], list) or type(value['next_sequence']) is not int
                or value['next_sequence'] != len(value['events']) + 1):
            raise ValueError('Invalid progress outbox state')
        keys = set(); pending_seen = False
        for seq, event in enumerate(value['events'], 1):
            fields = {'schema', 'id', 'sequence', 'project_id', 'campaign_id', 'kind', 'task_id',
                      'payload', 'key', 'message', 'created_at', 'status', 'receipt'}
            if (not isinstance(event, dict) or set(event) != fields or event['schema'] != EVENT_SCHEMA
                    or type(event['sequence']) is not int or event['sequence'] != seq
                    or event['id'] != self._id(seq) or event['project_id'] != self.project_id
                    or event['campaign_id'] != self.campaign_id or event['kind'] not in KINDS
                    or not isinstance(event['key'], str) or not event['key'].strip()
                    or event['key'] in keys or event['status'] not in {'pending', 'acknowledged'}):
                raise ValueError('Invalid progress event identity/order')
            if event['task_id'] is not None:
                _identifier(event['task_id'])
            _object(event['payload'])
            if event['message'] != render_message(event['kind'], event['task_id'], event['payload']):
                raise ValueError('Progress message does not match durable payload')
            try:
                if datetime.fromisoformat(event['created_at']).tzinfo is None:
                    raise ValueError('Missing progress timezone')
            except (TypeError, ValueError) as exc:
                raise ValueError('Invalid progress timestamp') from exc
            if event['status'] == 'pending':
                pending_seen = True
                if event['receipt'] is not None:
                    raise ValueError('Unacknowledged event has a receipt')
            elif pending_seen or not _object(event['receipt']):
                raise ValueError('Out-of-order or empty progress acknowledgement')
            keys.add(event['key'])
        return value

    def _id(self, sequence):
        return f'{self.project_id}:{self.campaign_id}:{sequence}'

    def snapshot(self):
        with self._lock():
            return deepcopy(self._read())

    def get(self, key):
        return next((event for event in self.snapshot()['events'] if event['key'] == key), None)

    def pending(self):
        return [event for event in self.snapshot()['events'] if event['status'] == 'pending']

    def emit(self, kind, task_id, payload, *, key):
        if kind not in KINDS:
            raise ValueError('Unknown progress event kind')
        if task_id is not None:
            _identifier(task_id)
        if not isinstance(key, str) or not key.strip():
            raise ValueError('Progress event needs a nonempty semantic key')
        payload = _object(payload)
        with self._lock():
            state = self._read()
            for event in state['events']:
                if event['key'] == key:
                    if (event['kind'], event['task_id'], event['payload']) != (kind, task_id, payload):
                        raise ValueError('Progress semantic key conflicts with existing event')
                    return deepcopy(event)
            seq = state['next_sequence']
            event = {'schema': EVENT_SCHEMA, 'id': self._id(seq), 'sequence': seq,
                     'project_id': self.project_id, 'campaign_id': self.campaign_id,
                     'kind': kind, 'task_id': task_id, 'payload': payload, 'key': key,
                     'message': render_message(kind, task_id, payload),
                     'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'pending', 'receipt': None}
            state['events'].append(event); state['next_sequence'] += 1
            atomic_json(self.path, state)
            return deepcopy(event)

    def ack(self, event_id, receipt):
        receipt = _object(receipt)
        if not receipt:
            raise ValueError('Progress acknowledgement requires a concrete receipt')
        with self._lock():
            state = self._read()
            matching = [event for event in state['events'] if event['id'] == event_id]
            if not matching:
                raise ValueError('Unknown progress acknowledgement identity')
            event = matching[0]
            if event['status'] == 'acknowledged':
                if event['receipt'] != receipt:
                    raise ValueError('Progress acknowledgement conflicts with durable receipt')
                return deepcopy(event)
            if next(item['id'] for item in state['events'] if item['status'] == 'pending') != event_id:
                raise ValueError('Progress acknowledgements must follow event order')
            event.update(status='acknowledged', receipt=receipt)
            atomic_json(self.path, state)
            return deepcopy(event)
