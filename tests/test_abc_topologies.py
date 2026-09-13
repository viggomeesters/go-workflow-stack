"""Topology capability evidence and real Go boundaries; no live model calls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft7Validator
from test_abc_context import workspace_fixture
from test_abc_worktrees import ROOT, git

FIXTURES = ROOT / 'fixtures/abc-topologies'


@pytest.fixture(autouse=True)
def neutral_controller(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def read(name):
    return json.loads((FIXTURES / name).read_text())


def test_inspected_protocol_and_ephemeral_help_are_versioned_and_hash_bound():
    evidence = read('provenance.json')
    assert evidence['binary_version'] == 'codex-cli 0.153.4'
    for name, digest in evidence['files_sha256'].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest
    helptext = (FIXTURES / 'exec-resume-help.txt').read_text()
    assert '--ephemeral' in helptext and 'without persisting session files' in helptext
    assert not evidence['effective_model_attestation']


@pytest.mark.parametrize('name', ['ThreadResumeParams', 'ThreadForkParams', 'TurnStartParams'])
def test_persistent_alternatives_require_exact_session_identity(name):
    schema = read(name + '.json')
    validator = Draft7Validator(schema)
    request = {'threadId': 'explicit-owned-session'}
    if name == 'TurnStartParams':
        request['input'] = [{'type': 'text', 'text': 'read canonical context', 'text_elements': []}]
    assert validator.is_valid(request)
    request.pop('threadId')
    assert not validator.is_valid(request)
    # Schemas require presence/type, not ownership. A future adapter must bind IDs.
    request['threadId'] = 'foreign-session'
    assert validator.is_valid(request)


def test_per_turn_overrides_are_persistent_protocol_fields_not_hot_switch_proof():
    schema = read('TurnStartParams.json')
    for field in ['model', 'effort']:
        assert 'subsequent turns' in schema['properties'][field]['description']
    build = {'threadId': 'owned', 'input': [], 'model': 'gpt-6-astra', 'effort': 'high'}
    critic = {**build, 'model': 'gpt-5.6-terra', 'effort': 'medium'}
    repair = {**build}
    validator = Draft7Validator(schema)
    assert all(validator.is_valid(value) for value in [build, critic, repair])
    # This protocol accepts arbitrary strings: catalog validation is a separate gate.
    assert validator.is_valid({**build, 'effort': 'invented'})
    assert not validator.is_valid({**build, 'effort': 42})
    assert 'ephemeral' in read('ThreadStartParams.json')['properties']
    assert 'ephemeral' in read('ThreadForkParams.json')['properties']
    # No turn mutation method is implemented by Go: this is protocol capability only.
    assert read('provenance.json')['proof_levels']['persistent_turn'].endswith('adapter deferred')


def run_phase(control, workspace, phase, env, output):
    script = '''import json,sys
from pathlib import Path
from go_workflow.cli import run_hook_command
from go_workflow.adapters import native_agent_command
from go_workflow.model_profiles import phase_model
control,workspace,phase,output=map(str,sys.argv[1:])
task=json.loads((Path(control)/'.go/tasks/active/task-schema-smoke.json').read_text())
result=run_hook_command(Path(workspace),native_agent_command('codex',phase,model_profile=phase_model(task,phase)),task,1,'direct',phase,require_protocol=True,feedback={'current':'critic feedback'} if phase=='repair' else None)
Path(output).write_text(json.dumps(result))
sys.exit(0 if result['returncode']==0 else 1)
'''
    return subprocess.run([sys.executable, '-c', script, str(control), str(workspace), phase, str(output)],
                          cwd=control.parent, env=env, capture_output=True, text=True)


def test_fresh_parent_processes_switch_profiles_with_one_workspace_and_durable_context(tmp_path, monkeypatch):
    control, workspace, task = workspace_fixture(tmp_path)
    task['execution_contract']['critic_model'] = {'id': 'gpt-5.6-terra', 'effort': 'medium'}
    canonical = control / '.go/tasks/active/task-schema-smoke.json'
    canonical.write_text(json.dumps(task))
    binary = tmp_path / 'codex'
    binary.write_text('#!' + sys.executable + '\n' + (FIXTURES / 'worker.py').read_text())
    binary.chmod(0o755)
    capture = tmp_path / 'captures.jsonl'
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
           'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1',
           'ABC_TOPOLOGY_CAPTURE': str(capture)}
    generation = json.loads((control / '.go/workspaces/task-schema-smoke.json').read_text())['generation']
    refs = []
    for phase, profile in [('build', task['execution_contract']['model']),
                           ('critic', task['execution_contract']['critic_model']),
                           ('repair', task['execution_contract']['model'])]:
        output = tmp_path / (phase + '.json')
        before = (workspace / 'app.txt').read_bytes() if phase == 'critic' else None
        result = run_phase(control, workspace, phase, env, output)
        assert result.returncode == 0, result.stdout + result.stderr + output.read_text()
        response = json.loads(output.read_text())
        assert response['model_selection']['requested'] == profile
        assert response['model_selection']['effective'] is None
        observed = json.loads(capture.read_text().splitlines()[-1])
        assert observed['cwd'] == str(workspace)
        args = observed['argv']
        assert args[:1] == ['exec'] and '--ephemeral' in args and 'resume' not in args
        assert args[args.index('--model') + 1] == profile['id']
        assert args[args.index('--sandbox') + 1] == ('read-only' if phase == 'critic' else 'workspace-write')
        ref = observed['context_ref']; refs.append(ref['path'])
        assert Path(ref['path']).is_relative_to(control / '.go')
        assert observed['snapshot']['workspace']['generation'] == generation
        if phase == 'critic':
            assert (workspace / 'app.txt').read_bytes() == before
        if phase == 'repair':
            assert observed['snapshot']['feedback'] == {'current': 'critic feedback'}
    assert len(set(refs)) == 3 and all(Path(p).exists() for p in refs)
    assert git(control, 'worktree', 'list', '--porcelain').count('worktree ') == 2
    # Fresh parent processes have exited, but canonical run selection/context survive.
    frozen = json.loads((control / '.go/runs/task-schema-smoke/model-selection.json').read_text())
    assert frozen['profiles']['critic'] != frozen['profiles']['repair']
    assert json.loads((control / '.go/workspaces/task-schema-smoke.json').read_text())['generation'] == generation
    # A changed task profile cannot silently migrate an already frozen run.
    task['execution_contract']['model'] = copy.deepcopy(task['execution_contract']['critic_model'])
    canonical.write_text(json.dumps(task)); prior = capture.read_bytes()
    output = tmp_path / 'changed.json'
    result = run_phase(control, workspace, 'repair', env, output)
    assert result.returncode != 0 and 'frozen' in output.read_text()
    assert capture.read_bytes() == prior


def test_competing_phase_cannot_acquire_owned_workspace_writer(tmp_path):
    from go_workflow.worktrees import execution_lease
    control, workspace, task = workspace_fixture(tmp_path)
    code = '''from pathlib import Path
import sys
from go_workflow.worktrees import execution_lease
with execution_lease(Path(sys.argv[1]),'task-schema-smoke','owner','run-1',timeout_seconds=.1):
 Path(sys.argv[1],'app.txt').write_text('competing worker started')
'''
    with execution_lease(workspace, task['id'], 'owner', 'run-1'):
        result = subprocess.run([sys.executable, '-c', code, str(workspace)], cwd=tmp_path,
                                env={**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1'},
                                capture_output=True, text=True)
        assert result.returncode != 0 and 'lock' in result.stderr.lower()
        assert not (workspace / 'app.txt').exists()
