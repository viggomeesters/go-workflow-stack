"""Real TTY progress is a rendered outbox projection, not a second queue."""
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time

from go_workflow.progress_events import ProgressOutbox
from go_workflow.progress_transport import runtime_environment

CLI = Path(__file__).resolve().parents[1] / 'cli/go.py'


def fixture(tmp_path):
    (tmp_path / '.go').mkdir()
    (tmp_path / '.go/project.json').write_text(json.dumps({'id': 'terminal-test'}))
    return ProgressOutbox(tmp_path, 'pilot')


def test_watch_replays_existing_messages_to_real_tty(tmp_path):
    box = fixture(tmp_path)
    event = box.emit('start', 'T01', {'title': 'Controle uitvoeren'}, key='start')
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, str(CLI), 'progress', 'watch', str(tmp_path),
                                '--campaign', 'pilot'], stdout=slave, stderr=slave,
                               env=runtime_environment(), start_new_session=True)
    os.close(slave)
    output = b''
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try: output += os.read(master, 65536)
                except OSError: break
            if b'T01 in progress' in output: break
        assert 'T01 in progress' in output.decode(), output.decode()
        assert box.pending() == [event], 'Viewer must not acknowledge canonical delivery by itself'
    finally:
        process.terminate(); process.wait(timeout=5); os.close(master)


def config(repo):
    return {'schema': 'go-workflow.progress-transport.v1', 'timeout_seconds': 5,
            'command': [sys.executable, '-m', 'go_workflow.progress_terminal', 'transport',
                        '--repo', str(repo), '--campaign', 'pilot', '--no-auto-open']}


def test_render_receipt_and_retry_survive_viewer_restart(tmp_path):
    from go_workflow.progress_transport import preflight, flush
    box = fixture(tmp_path)
    first = box.emit('start', 'T01', {'title': 'Render once'}, key='first')
    def viewer():
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, str(CLI), 'progress', 'watch', str(tmp_path),
                                    '--campaign', 'pilot'], stdout=slave, stderr=slave,
                                   env=runtime_environment(), start_new_session=True)
        os.close(slave)
        return master, process
    master, process = viewer()
    try:
        caps = preflight(config(tmp_path))['capabilities']
        assert caps['user_terminal'] is True and caps['user_chat'] is False
        assert flush(tmp_path, 'pilot', config(tmp_path))['ok']
        receipt = box.snapshot()['events'][0]['receipt']
        assert receipt['event_id'] == first['id']
        assert receipt['surface'] == 'terminal' and receipt['displayed'] is True
    finally:
        process.terminate(); process.wait(timeout=5); os.close(master)
    second = box.emit('phase', 'T01', {'phase': 'verify'}, key='second')
    assert not flush(tmp_path, 'pilot', config(tmp_path))['ok']
    assert box.pending() == [second]
    master, process = viewer()
    try:
        assert preflight(config(tmp_path))['capabilities']['user_terminal']
        assert flush(tmp_path, 'pilot', config(tmp_path))['ok']
        assert box.snapshot()['events'][0]['receipt'] == receipt
        assert box.pending() == []
    finally:
        process.terminate(); process.wait(timeout=5); os.close(master)


def test_new_campaign_resolves_terminal_choice_without_changing_existing_config(tmp_path):
    from test_campaign_intake import setup
    from go_workflow.campaign_intake import materialize_until_scope
    repo, task = setup(tmp_path)
    project = repo / '.go/project.json'
    data = json.loads(project.read_text())
    data['progress_transport'] = {'schema': 'go-workflow.terminal-progress.v1', 'auto_open': True}
    project.write_text(json.dumps(data))
    before = project.read_bytes()
    contract = materialize_until_scope(repo, intent='Go tot alle taken klaar', source_ref='user:terminal', campaign_id='screen')
    transport = contract['execution']['progress']['transport']
    assert transport['schema'] == 'go-workflow.progress-transport.v1'
    assert 'go_workflow.progress_terminal' in transport['command']
    assert str(repo) in transport['command'] and 'screen' in transport['command']
    assert project.read_bytes() == before


def test_preflight_opens_once_and_requires_real_viewer(tmp_path, monkeypatch):
    import go_workflow.progress_terminal as terminal
    box = fixture(tmp_path)
    launched = []
    def launch(box):
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, '-m', 'go_workflow.progress_terminal', 'watch',
                                    '--repo', str(box.repo), '--campaign', box.campaign_id],
                                   stdout=slave, stderr=slave, env=runtime_environment())
        os.close(slave)
        launched.append((master, process))
        return lambda: None
    monkeypatch.setattr(terminal, '_launch_native', launch)
    try:
        for _ in range(2):
            assert terminal.request(tmp_path, 'pilot', {'operation': 'preflight'})['capabilities']['user_terminal']
        assert len(launched) == 1
    finally:
        for master, process in launched:
            process.terminate(); process.wait(timeout=5); os.close(master)


def test_non_tty_is_not_delivery_and_control_sequences_are_inert(tmp_path):
    from go_workflow.progress_terminal import terminal_text
    fixture(tmp_path)
    result = subprocess.run([sys.executable, str(CLI), 'progress', 'watch', str(tmp_path), '--campaign', 'pilot'],
                            capture_output=True, text=True)
    assert result.returncode and 'attached terminal' in result.stderr
    assert not (tmp_path / '.go/runs/progress/pilot.terminal.json').exists()
    text = terminal_text('T01\x1b]52;c;clipboard\x07\x9b2J\u202evil\nnext')
    assert '\x1b' not in text and '\x07' not in text and '\x9b' not in text and '\u202e' not in text
    assert text.endswith('\nnext')


def test_independent_watchdog_renders_while_controller_is_idle(tmp_path):
    from go_workflow.progress_transport import preflight
    from go_workflow.progress_watchdog import start
    box = fixture(tmp_path)
    state = tmp_path / '.go/runs/campaigns/pilot/state.json'
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'current_task': 'T01'}))
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, str(CLI), 'progress', 'watch', str(tmp_path), '--campaign', 'pilot'],
                               stdout=slave, stderr=slave, env=runtime_environment())
    os.close(slave)
    try:
        preflight(config(tmp_path))
        with start(tmp_path, 'pilot', state, config(tmp_path), heartbeat_seconds=.25):
            time.sleep(1.5)  # Main controller is deliberately idle; independent child must deliver.
            assert len([e for e in box.snapshot()['events'] if e['kind'] == 'heartbeat' and e['status'] == 'acknowledged']) >= 2
        output = b''
        while select.select([master], [], [], .1)[0]:
            output += os.read(master, 65536)
        assert output.count(b'T01 heartbeat') >= 2
    finally:
        process.terminate(); process.wait(timeout=5); os.close(master)


def test_native_launcher_quotes_paths_and_never_embeds_task_text(tmp_path, monkeypatch):
    import shlex
    import go_workflow.progress_terminal as terminal
    repo = tmp_path / "space 'quote' $(touch SHOULD_NOT_EXIST)"
    repo.mkdir(); box = fixture(repo)
    captured = []
    def launch(argv, **kwargs):
        script = Path(argv[-1]); captured.append(script.read_text())
        assert script.stat().st_mode & 0o777 == 0o700
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(terminal.sys, 'platform', 'darwin')
    monkeypatch.setattr(terminal.subprocess, 'run', launch)
    cleanup = terminal._launch_native(box)
    try:
        command = captured[0].splitlines()[-1]
        assert shlex.split(command)[-3:] == [str(repo), '--campaign', 'pilot']
        assert not (tmp_path / 'SHOULD_NOT_EXIST').exists()
    finally:
        cleanup()


def test_enable_is_explicit_and_existing_transport_is_preserved(tmp_path):
    from go_workflow.progress_terminal import enable_terminal
    fixture(tmp_path)
    project = tmp_path / '.go/project.json'
    project.write_text(json.dumps({'id': 'terminal-test', 'progress_transport': {'command': ['existing']}}))
    before = project.read_bytes()
    import pytest
    with pytest.raises(ValueError, match='preserved'):
        enable_terminal(tmp_path)
    assert project.read_bytes() == before


def test_lost_ack_does_not_render_again_in_same_window(tmp_path, monkeypatch):
    from go_workflow.progress_transport import preflight, flush
    box = fixture(tmp_path)
    box.emit('start', 'T01', {'title': 'Stable rendering'}, key='first')
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, str(CLI), 'progress', 'watch', str(tmp_path), '--campaign', 'pilot'],
                               stdout=slave, stderr=slave, env=runtime_environment())
    os.close(slave)
    try:
        preflight(config(tmp_path))
        original = ProgressOutbox.ack
        monkeypatch.setattr(ProgressOutbox, 'ack', lambda *a: (_ for _ in ()).throw(OSError('lost ack')))
        assert not flush(tmp_path, 'pilot', config(tmp_path))['ok']
        monkeypatch.setattr(ProgressOutbox, 'ack', original)
        assert flush(tmp_path, 'pilot', config(tmp_path))['ok']
        output = b''
        while select.select([master], [], [], .1)[0]: output += os.read(master, 65536)
        assert output.count(b'T01 in progress') == 1
    finally:
        process.terminate(); process.wait(timeout=5); os.close(master)


def test_launch_failure_cannot_claim_terminal_delivery(tmp_path, monkeypatch):
    import pytest
    import go_workflow.progress_terminal as terminal
    box = fixture(tmp_path)
    def fail(box): raise OSError('terminal launch refused')
    monkeypatch.setattr(terminal, '_launch_native', fail)
    with pytest.raises(OSError, match='refused'):
        terminal.request(tmp_path, 'pilot', {'operation': 'preflight'})
    assert box.snapshot()['events'] == []


def test_foreign_viewer_and_symlink_checkpoint_fail_closed(tmp_path, monkeypatch):
    import pytest
    import go_workflow.progress_terminal as terminal
    box = fixture(tmp_path)
    path = box.path.with_suffix('.terminal.json'); path.parent.mkdir(parents=True)
    foreign = {'schema': terminal.VIEW_SCHEMA, 'repo': str(box.repo), 'project_id': box.project_id,
               'campaign_id': 'pilot', 'displayed': {}, 'viewer': {'host': 'other-host', 'pid': 10},
               'session': 'old', 'updated_at': time.time(), 'ready': True, 'tty': '/dev/tty1'}
    path.write_text(json.dumps(foreign))
    with pytest.raises(ValueError, match='another host'):
        terminal.request(tmp_path, 'pilot', {'operation': 'preflight'})
    path.unlink(); target = tmp_path / 'user-data'; target.write_text('preserve')
    path.symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        terminal.request(tmp_path, 'pilot', {'operation': 'preflight'})
    assert target.read_text() == 'preserve'


def test_enable_persists_portable_preference_and_ignores_only_local_viewer_state(tmp_path):
    from go_workflow.progress_terminal import enable_terminal
    fixture(tmp_path)
    (tmp_path / '.gitignore').write_text('user-pattern\n')
    first = enable_terminal(tmp_path)
    assert first['existing_campaigns_changed'] is False
    assert enable_terminal(tmp_path) == first
    assert (tmp_path / '.gitignore').read_text() == 'user-pattern\n.go/runs/progress/*.terminal.json\n'
    preference = json.loads((tmp_path / '.go/project.json').read_text())['progress_transport']
    assert preference == {'schema': 'go-workflow.terminal-progress.v1', 'auto_open': True}
