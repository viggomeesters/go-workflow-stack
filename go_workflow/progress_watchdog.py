"""Independent observational heartbeat process; never repairs or completes work."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import signal
import re
import socket
import subprocess
import sys
import time
from .state_io import atomic_json, repository_lock
from .progress_transport import flush, _safe_path, runtime_environment

class Clock:
    def now(self):
        return time.time()


def process_identity(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True)
    token = result.stdout.strip()
    return {'host': socket.gethostname(), 'pid': pid, 'start_token': token} if token else None


def supervisor_alive(identity):
    return bool(identity and identity.get('host') == socket.gethostname()
                and process_identity(identity.get('pid', 0)) == identity)


def _read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _diagnose(managed):
    controller = managed.get('controller') or {}
    group = managed.get('worker_group')
    if controller.get('host') != socket.gethostname() or not isinstance(group, int) or group <= 0:
        return 'worker liveness unknown: no locally attributable process group; inspect source records'
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return 'recorded worker process group is absent; controller diagnosis required'
    except PermissionError:
        return 'worker liveness unknown: process inspection denied'
    return 'recorded worker process group exists; duration alone is not a stall'


class Watchdog:
    def __init__(self, repo, campaign_id, state_path, config, *, heartbeat_seconds=300, clock=None):
        from .progress_events import ProgressOutbox
        self.repo = Path(repo)
        self.campaign_id = campaign_id
        self.outbox = ProgressOutbox(self.repo, campaign_id)
        canonical = self.repo / '.go/runs/campaigns' / campaign_id / 'state.json'
        if canonical.is_symlink() or Path(state_path).absolute() != canonical.absolute():
            raise ValueError('watchdog requires canonical campaign state path')
        if not isinstance(heartbeat_seconds, (int, float)) or not math.isfinite(heartbeat_seconds) or heartbeat_seconds <= 0:
            raise ValueError('heartbeat interval must be positive')
        _safe_path(canonical)
        self.state_path = canonical
        self.config = config
        self.interval = heartbeat_seconds
        self.clock = clock or Clock()
        self.path = self.repo / '.go/runs/progress' / f'{campaign_id}.watchdog.json'

    def tick(self, *, deliver=True):
        now = self.clock.now()
        _safe_path(self.path)
        _safe_path(self.state_path)
        with repository_lock(self.repo / '.go', f'progress-watchdog-{self.campaign_id}'):
            saved = _read(self.path)
            if saved and (saved.get('schema') != 'go-workflow.progress-watchdog.v1' or
                          any(not isinstance(saved.get(k), (int, float)) or not math.isfinite(saved[k])
                              for k in ('started_epoch', 'next_epoch', 'sequence'))):
                raise ValueError('invalid watchdog checkpoint')
            if not saved:
                saved = {'schema': 'go-workflow.progress-watchdog.v1', 'started_epoch': now,
                         'next_epoch': now + self.interval, 'sequence': 0}
            if now < saved.get('last_epoch', now):
                saved['next_epoch'] = now
            saved['last_epoch'] = now
            if now >= saved['next_epoch']:
                state = _read(self.state_path)
                task_id = state.get('current_task')
                if task_id is not None and (not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', task_id)):
                    task_id = None
                managed_path = self.repo / '.go/runs' / str(task_id) / 'run-state.json'
                _safe_path(managed_path)
                managed = _read(managed_path) if task_id else {}
                phase = managed.get('phase', 'unknown')
                inflight = managed.get('inflight') or {}
                # State readback confirms a recorded operation, never its continued liveness or outcome.
                activity = 'unknown'
                if inflight.get('phase') and inflight.get('started_at'):
                    activity = f"recorded {inflight['phase']} start at {inflight['started_at']}; current activity unknown"
                sequence = saved['sequence'] + 1
                key = f'watchdog:heartbeat:{sequence}'
                if self.outbox.get(key) is None:
                    self.outbox.emit('heartbeat', task_id, {'phase': phase,
                        'elapsed_seconds': max(0, now - saved['started_epoch']),
                        'activity': activity, 'confirmed_progress': 'unknown',
                        'diagnostic': _diagnose(managed)}, key=key)
                saved.update(sequence=sequence, next_epoch=now + self.interval)
            atomic_json(self.path, saved)
        if not deliver:
            return {'ok': True, 'delivered': 0, 'pending': len(self.outbox.pending())}
        # Never begin a potentially slow adapter call across the next observation deadline.
        remaining = saved['next_epoch'] - self.clock.now()
        if self.interval >= 300 and remaining <= self.config.get('timeout_seconds', 10) + 2:
            return {'ok': True, 'delivered': 0, 'pending': len(self.outbox.pending()), 'deferred': True}
        return flush(self.repo, self.campaign_id, self.config, max_events=1, lock_timeout=1)


class WatchdogHandle:
    def __init__(self, process):
        self.process = process
    def stop(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.stop()


def start(repo, campaign_id, state_path, config, *, heartbeat_seconds=300):
    repo = Path(repo).resolve()
    state_path = Path(state_path).resolve()
    Watchdog(repo, campaign_id, state_path, config, heartbeat_seconds=heartbeat_seconds)
    identity = process_identity(os.getpid())
    if identity is None:
        raise ValueError('cannot prove supervisor process identity')
    path = repo / '.go/runs/progress' / f'{campaign_id}.watchdog-config.json'
    _safe_path(path)
    atomic_json(path, {'transport': config, 'supervisor': identity, 'heartbeat_seconds': heartbeat_seconds})
    process = subprocess.Popen([sys.executable, '-m', 'go_workflow.progress_watchdog',
        '--repo', str(repo), '--campaign-id', campaign_id, '--state-path', str(state_path),
        '--config', str(path)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=runtime_environment())
    deadline = time.monotonic() + 5
    ready = path.with_suffix('.ready.json')
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('heartbeat watcher exited during startup')
        if _read(ready).get('pid') == process.pid:
            return WatchdogHandle(process)
        time.sleep(0.02)
    WatchdogHandle(process).stop()
    raise RuntimeError('heartbeat watcher did not acknowledge startup')


def main():
    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--campaign-id', required=True)
    parser.add_argument('--state-path', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = _read(args.config)
    watcher = Watchdog(args.repo, args.campaign_id, args.state_path, config['transport'],
                       heartbeat_seconds=config.get('heartbeat_seconds', 300))
    _safe_path(args.config.with_suffix('.ready.json'))
    _safe_path(args.config.with_suffix('.error.json'))
    watcher.tick(deliver=False)
    atomic_json(args.config.with_suffix('.ready.json'), {'pid': os.getpid(), 'ready': True})
    while supervisor_alive(config.get('supervisor')):
        try:
            watcher.tick()
        except (OSError, ValueError, RuntimeError) as exc:
            atomic_json(args.config.with_suffix('.error.json'), {'pid': os.getpid(), 'error': str(exc)})
        time.sleep(min(1, watcher.interval))

if __name__ == '__main__':
    main()
