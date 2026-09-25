"""Delivery projection of canonical campaign/run records, never another task queue."""
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path

ACTIVE = ContextVar('campaign_progress', default=None)
SCHEMA = 'go-workflow.campaign-progress.v1'


class ProgressError(ValueError):
    pass


def validate_progress(value):
    if (not isinstance(value, dict) or set(value) != {'schema', 'heartbeat_seconds', 'transport'}
            or value.get('schema') != SCHEMA or type(value.get('heartbeat_seconds')) is not int
            or value['heartbeat_seconds'] != 300):
        return ['progress requires versioned contract and 300-second heartbeat']
    config = value['transport']
    if config is not None:
        if (not isinstance(config, dict) or set(config) != {'schema', 'command', 'timeout_seconds'}
                or config.get('schema') != 'go-workflow.progress-transport.v1'
                or not isinstance(config.get('command'), list) or not config['command']
                or any(not isinstance(arg, str) or not arg for arg in config['command'])
                or type(config.get('timeout_seconds')) not in (float, int)
                or not 0 < config['timeout_seconds'] <= 60):
            return ['progress transport requires explicit argv and timeout in (0,60] seconds']
    return []


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class ProgressSession:
    def __init__(self, repo, contract, actor, *, control_only=False):
        self.repo, self.contract, self.actor = Path(repo), contract, actor
        self.binding = (contract.get('execution') or {}).get('progress')
        self.enabled = self.binding is not None and not control_only
        self.watcher = None
        self.token = None
        self.outbox = None

    def __enter__(self):
        if not self.enabled:
            return self
        from .progress_transport import preflight
        from .progress_events import ProgressOutbox
        errors = validate_progress(self.binding)
        if errors:
            raise ProgressError('; '.join(errors))
        self.config = self.binding['transport']
        if self.config is None:
            raise ProgressError('No independent progress transport configured; unattended run cannot promise chat heartbeats. Configure project.progress_transport and explicitly revise the campaign.')
        preflight(self.config)
        self.outbox = ProgressOutbox(self.repo, self.contract['id'])
        self.drain()
        self.token = ACTIVE.set(self)
        return self

    def __exit__(self, *args):
        if self.watcher:
            self.watcher.stop()
        if self.token is not None:
            ACTIVE.reset(self.token)

    def emit(self, kind, task_id, payload, key):
        if self.enabled and self.outbox.get(key) is None:
            self.outbox.emit(kind, task_id, payload, key=key)

    def drain(self):
        if self.enabled:
            if self.watcher and self.watcher.process.poll() is not None:
                raise ProgressError('Independent heartbeat watcher stopped; resume to restore monitoring before another task')
            from .progress_transport import flush
            receipt = flush(self.repo, self.contract['id'], self.config)
            if not receipt['ok']:
                raise ProgressError('Progress delivery pending: ' + str(receipt.get('error')))

    def begin(self, state):
        if not self.enabled:
            return
        ids = self.contract['authority']['permitted_tasks']
        tasks = {key: self._task(key) for key in ids}
        ordered, remaining = [], list(ids)
        while remaining:
            ready = [key for key in remaining if all(dep.get('task_id') not in remaining
                     for dep in tasks[key].get('dependencies', []) if dep.get('project', self.contract['project']) == self.contract['project'])]
            if not ready:
                raise ProgressError('Cannot present cyclic task dependency order')
            ordered.extend(ready)
            remaining = [key for key in remaining if key not in ready]
        blocked = [key for key in ids if tasks[key]['status'] == 'blocked']
        self.emit('run_start', None, {'summary': 'Oplevervolgorde: ' + ', '.join(ordered) +
                  '; geblokkeerd: ' + (', '.join(blocked) or 'geen'), 'task_ids': ids,
                  'delivery_order': ordered, 'blocked_task_ids': blocked}, 'run:start')
        self.sync(state)
        if state.get('history'):
            from .resume_context import compose_resume_context
            identity = state.get('current_task')
            payload = {'summary': 'Afgerond: ' + (', '.join(state['completed_tasks']) or 'geen') + '; vervolg: ' + (identity or 'volgende uitvoerbare taak'), 'completed_tasks': state['completed_tasks']}
            if identity:
                payload['context'] = compose_resume_context(self.repo, identity, actor=self.actor)
                payload['summary'] += '; ' + payload['context']['summary']
            self.emit('resume', identity, payload, 'resume:' + state['controller']['nonce'])
        self.drain()
        from .progress_watchdog import start
        try:
            self.watcher = start(self.repo, self.contract['id'], self.repo / '.go/runs/campaigns' / self.contract['id'] / 'state.json', self.config, heartbeat_seconds=300)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ProgressError('Independent heartbeat startup failed: ' + str(exc)) from exc

    def sync(self, state):
        if not self.enabled:
            return
        for index, item in enumerate(state['history']):
            key = 'campaign:' + str(index) + ':' + _digest(item)
            if self.outbox.get(key):
                continue
            name, task_id = item.get('event'), item.get('task_id')
            payload = {'summary': item.get('reason') or item.get('summary') or item.get('source') or 'vastgelegd in het campaign-checkpoint', 'source': item}
            kind = None
            if name == 'campaign.task_selected':
                task = self._task(task_id)
                if task['status'] == 'active':
                    self.task_started(task_id)
                continue
            elif name in {'campaign.task_completed', 'campaign.task_reconciled', 'campaign.block_member_completed'}:
                kind = 'done'
                payload.update(self._delivered(task_id))
            elif name in {'campaign.no_progress_isolated', 'campaign.parent_resume_blocked'}:
                kind = 'block'
                failures = [failure for failure in state.get('failures', []) if failure.get('task_id') == task_id]
                if failures:
                    payload['summary'] = failures[-1].get('summary') or payload['summary']
            elif name == 'campaign.repair_admitted':
                kind = 'repair'; task_id = item.get('repair_id') or task_id
            elif name == 'campaign.task_amended':
                kind = 'amend'
            elif name == 'campaign.parent_resumed':
                kind = 'resume'
            if kind:
                self.emit(kind, task_id, payload, key)
        self.drain()

    def task_started(self, task_id):
        task = self._task(task_id)
        if task['status'] != 'active' or task.get('claim', {}).get('agent') != self.actor:
            raise ProgressError('Task start requires the actual owned canonical claim')
        self.emit('start', task_id, {'title': task['summary'], 'summary': task['summary'],
                  'intended_result': task.get('acceptance', [])}, 'task:start:' + task_id)
        self.drain()

    def _task(self, task_id):
        from .campaign_delivery import _task
        return _task(self.repo, task_id)

    def _delivered(self, task_id):
        from .campaign_delivery import delivery_report
        report = delivery_report(self.repo, task_id)
        if not report['delivered']:
            raise ProgressError('Cannot announce done without verified delivery: ' + task_id)
        task = self._task(task_id)
        shipping = (self.contract.get('execution') or {}).get('shipping') or {}
        from .delivery_closure import inspect_closure
        closure = inspect_closure(self.repo, task_id) if shipping else {}
        if shipping and not closure.get('delivered') and (task.get('delivery_block') or {}).get('role') != 'member':
            raise ProgressError('Cannot announce done before closure synchronization: ' + task_id)
        released = report['publication'].get('status') == 'verified' and bool(report['publication'].get('commit'))
        committed = released or (closure.get('delivered') and shipping.get('policy') in {'local-commit', 'push'})
        pushed = released or (closure.get('delivered') and shipping.get('policy') == 'push')
        return {'summary': task['summary'], 'checks': 'geslaagd en beoordeeld',
                'publication': {'commit': 'geverifieerd' if committed else 'niet aangetoond',
                                'push': 'geverifieerd' if pushed else 'niet aangetoond',
                                'release': report['publication'],
                                'deployment': 'geverifieerd' if report['reported_live'] else 'niet van toepassing'},
                'delivery': report, 'closure': closure}

    def finish(self, state, result):
        if not self.enabled:
            return
        self.sync(state)
        from .campaign import resolve_previous_path
        from .campaign_contracts import contract_digest
        original = self.contract
        while original['revision'] > 1:
            prior = resolve_previous_path(self.repo, original, None)
            if prior is None:
                raise ProgressError('Original scope revision chain is unavailable')
            previous = json.loads(prior.read_text())
            if contract_digest(previous) != original['previous_sha256']:
                raise ProgressError('Original scope revision chain changed')
            original = previous
        originals = original['authority']['permitted_tasks']
        payload = {'summary': f"{len(state['completed_tasks'])} taken afgerond; status {result['status']}",
                   'original_task_ids': originals,
                   'added_task_ids': [key for key in self.contract['authority']['permitted_tasks'] if key not in originals],
                   'results': [{'task_id': key, **self._delivered(key)} for key in state['completed_tasks']],
                   'completed_tasks': state['completed_tasks'], 'blocked_task': result.get('blocked_task'),
                   'remaining_tasks': [{'task_id': key, 'status': self._task(key)['status']} for key in self.contract['authority']['permitted_tasks'] if key not in state['completed_tasks']],
                   'stop': state.get('stop'), 'changes': [{**item, 'task_id': item.get('repair_id') or item.get('task_id')} for item in state['history'] if item.get('event') in {'campaign.repair_admitted', 'campaign.task_amended'}]}
        self.emit('run_end', None, payload, 'run:end:' + _digest(payload))
        self.drain()


def phase_changed(control, task_id, before, after):
    session = ACTIVE.get()
    if session is None or not session.enabled or Path(control).resolve() != session.repo.resolve():
        return
    if before.get('phase') == 'setup' and after.get('phase') != 'setup':
        session.task_started(task_id)
    if before.get('phase') == after.get('phase'):
        return
    phase = after.get('phase')
    payload = {'phase': phase, 'summary': 'controller gaat naar fase ' + str(phase), 'run_id': after.get('run_id')}
    key = 'phase:' + _digest({'task': task_id, 'run': after.get('run_id'), 'phase': phase, 'history': after.get('history'), 'attempt': after.get('attempt')})
    session.emit('phase', task_id, payload, key)
