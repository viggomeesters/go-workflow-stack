"""Observable contract intake and readiness behavior (no live model/publisher)."""
import json
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(repo, *args):
    return subprocess.run([sys.executable, str(ROOT / 'cli/go.py'), *map(str, args)],
                          cwd=repo, text=True, capture_output=True)


def fixture(tmp_path, name='fixture'):
    repo = tmp_path / name
    shutil.copytree(ROOT / 'fixtures/minimal', repo)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    return repo


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n')


def test_create_freezes_explicit_contract_with_project_defaults(tmp_path):
    repo = fixture(tmp_path)
    project_path = repo / '.go/project.json'
    project = json.loads(project_path.read_text())
    project['execution_defaults'] = {
        'schema': 'go-workflow.execution-contract.v1', 'task_kind': 'product',
        'model': {'id': 'gpt-6-astra', 'effort': 'high'},
        'release': {'mode': 'required'},
    }
    write(project_path, project)
    profile = tmp_path / 'profile.json'
    write(profile, {'schema': 'go-workflow.execution-contract.v1',
                    'model': {'id': 'gpt-5.6-terra'}})
    result = run(repo, 'task', 'create', repo, '--id', 'explicit', '--summary',
                 'Explicit contract', '--execution-mode', 'agent',
                 '--execution-contract', profile, '--epic', 'workflow-contract')
    assert result.returncode == 0, result.stdout + result.stderr

    task = json.loads((repo / '.go/tasks/open/explicit.json').read_text())
    assert task['execution_contract']['model'] == {'id': 'gpt-5.6-terra', 'effort': 'high'}
    assert task['execution_contract']['release'] == {'mode': 'required'}
    assert task['execution_contract']['task_kind'] == 'product'
    # Defaults later changing do not rewrite a task's explicit selection.
    project['execution_defaults']['model']['effort'] = 'low'
    write(project_path, project)
    assert json.loads((repo / '.go/tasks/open/explicit.json').read_text()) == task


def test_execution_brief_preserves_profiles_and_rejects_bad_batch_atomically(tmp_path):
    from go_workflow.cli import create_tasks_from_execution_brief, RepoLocalError
    repo = fixture(tmp_path)
    unit = {'id': 'first', 'summary': 'First', 'scope': {'read': ['.go/**'], 'modify': ['src/**']},
            'execution_mode': 'agent', 'acceptance': ['Observable'], 'verification': ['true'],
            'execution_contract': profile()}
    second = {**unit, 'id': 'second', 'dependencies': [
        {'project': 'repo-local-spike-fixture', 'task_id': 'first', 'requires': 'done'}]}
    brief = {'schema': 'go-workflow.execution-brief.v1', 'destination': 'Result', 'problem': 'Problem',
             'chosen_approach': 'Explicit units', 'non_goals': [],
             'source': {'recommendation': 'Explicit units', 'sha256': hashlib.sha256(b'Explicit units').hexdigest()},
             'work_units': [unit, second]}
    second['execution_contract'] = {**profile(), 'model': {'id': 'gpt-6-astra', 'effort': 'typo'}}
    before = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob('*') if p.is_file()}
    with pytest.raises(RepoLocalError, match='effort'):
        create_tasks_from_execution_brief(repo, brief)
    assert {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob('*') if p.is_file()} == before
    second['execution_contract'] = profile()
    create_tasks_from_execution_brief(repo, brief)
    actual = json.loads((repo / '.go/tasks/open/second.json').read_text())
    assert actual['execution_contract'] == profile()
    assert actual['dependencies'] == second['dependencies']


@pytest.mark.parametrize('mutation', [
    {'model': {'id': 'x', 'effort': 'typo'}},
    {'model': {'id': 'x', 'effort': []}},
    {'task_kind': {}},
    {'release': {'mode': 'none'}},
    {'unexpected': True},
    {'workspace': {'mode': 'task_worktree', 'control_state': 'chat'}},
])
def test_invalid_contract_cli_rejects_before_task_or_hierarchy_write(tmp_path, mutation):
    repo = fixture(tmp_path)
    path = tmp_path / 'profile.json'
    write(path, {**profile(), **mutation})
    before = {str(p): p.read_bytes() for p in (repo / '.go').rglob('*') if p.is_file()}
    result = run(repo, 'task', 'create', repo, '--id', 'bad', '--summary', 'Bad profile',
                 '--execution-contract', path, '--epic', 'workflow-contract')
    assert result.returncode != 0
    assert 'Traceback' not in result.stderr
    assert {str(p): p.read_bytes() for p in (repo / '.go').rglob('*') if p.is_file()} == before


def profile():
    return {'schema': 'go-workflow.execution-contract.v1', 'task_kind': 'product',
            'model': {'id': 'gpt-6-astra', 'effort': 'high'}, 'release': {'mode': 'required'}}


def test_explicit_cross_project_dependency_and_cycle_validation(tmp_path):
    from go_workflow.cli import create_tasks_from_execution_brief, RepoLocalError
    from go_workflow.execution_contracts import dependency_findings
    repo = fixture(tmp_path, 'one')
    other = fixture(tmp_path, 'two')
    p = other / '.go/project.json'
    project = json.loads(p.read_text()); project['id'] = 'other'; write(p, project)
    p = other / '.go/tasks/open/task-schema-smoke.json'
    parent = json.loads(p.read_text()); parent['project'] = 'other'; write(p, parent)
    child = {'id': 'child', 'project': 'repo-local-spike-fixture', 'status': 'open',
             'execution_contract': profile(), 'dependencies': [
                 {'project': 'other', 'task_id': 'task-schema-smoke', 'requires': 'done'}]}
    assert 'not configured' in '; '.join(dependency_findings(repo, child))
    p = repo / '.go/project.json'; project = json.loads(p.read_text())
    project['dependency_projects'] = {'other': str(other)}; write(p, project)
    assert not dependency_findings(repo, child)
    assert 'not done' in '; '.join(dependency_findings(repo, child, readiness=True))
    project['dependency_projects']['other'] = str(repo); write(p, project)
    assert 'identity mismatch' in '; '.join(dependency_findings(repo, child))
    child['dependencies'] = [{'project': child['project'], 'task_id': 'child', 'requires': 'done'}]
    assert 'cycle' in '; '.join(dependency_findings(repo, child))


def test_intent_and_followup_inherit_contract_without_rewriting_legacy(tmp_path):
    from go_workflow.cli import create_task_from_intent, create_followup_task
    repo = fixture(tmp_path)
    root = repo / '.go'
    smoke = (root / 'tasks/open/task-schema-smoke.json').read_bytes()
    p = root / 'project.json'; project = json.loads(p.read_text())
    project['execution_defaults'] = profile(); write(p, project)
    created = create_task_from_intent(repo, 'Add a bounded command')
    parent = json.loads((repo / created['path']).read_text())
    assert parent['execution_contract'] == profile()
    parent['execution_contract']['model']['id'] = 'gpt-5.6-sol'
    follow = create_followup_task(repo, parent, ['Repair one failure'], 'pytest')
    assert follow['execution_contract'] == parent['execution_contract']
    assert (root / 'tasks/open/task-schema-smoke.json').read_bytes() == smoke


def test_contract_schema_and_runtime_agree_on_compatibility_profiles():
    from jsonschema import Draft202012Validator
    from go_workflow.execution_contracts import validate_execution_contract
    schema = json.loads((ROOT / 'schemas/task.schema.json').read_text())['$defs']['executionContract']
    for kind, mode, reason, valid in [('product', 'required', None, True),
                                     ('product', 'none', 'No changes', False),
                                     ('mechanical', 'none', 'Local bookkeeping', True),
                                     ('no_change', 'none', None, False),
                                     ('smoke', 'none', 'Reusable source fixture', True)]:
        value = profile(); value['task_kind'] = kind; value['release'] = {'mode': mode}
        if reason: value['release']['reason'] = reason
        assert (not validate_execution_contract(value)) == valid
        assert Draft202012Validator(schema).is_valid(value) == valid


def test_claim_and_next_wait_for_dependency_receipt(tmp_path):
    repo = fixture(tmp_path)
    root = repo / '.go'
    child = json.loads((root / 'tasks/open/task-schema-smoke.json').read_text())
    child.update(id='child', summary='Waiting child', order=0, execution_contract=profile(),
                 dependencies=[{'project': child['project'], 'task_id': 'task-schema-smoke',
                                'requires': 'done_with_required_release_evidence'}])
    write(root / 'tasks/open/child.json', child)
    hierarchy = json.loads((root / 'hierarchy.json').read_text())
    hierarchy['epics'][0]['tasks'].append('child')
    write(root / 'hierarchy.json', hierarchy)
    result = run(repo, 'next', repo)
    assert 'task-schema-smoke' in result.stdout, result.stdout + result.stderr
    result = run(repo, 'claim', 'child', '--repo', repo, '--allow-dirty')
    assert result.returncode != 0 and 'dependency' in result.stderr, result.stdout + result.stderr
    parent_path = root / 'tasks/open/task-schema-smoke.json'
    parent = json.loads(parent_path.read_text())
    parent['status'] = 'done'
    done_path = root / 'tasks/done/task-schema-smoke.json'
    write(done_path, parent)
    parent_path.unlink()
    result = run(repo, 'claim', 'child', '--repo', repo, '--allow-dirty')
    assert result.returncode != 0 and 'receipt' in result.stderr
    parent['release_receipt'] = {'schema': 'go-workflow.release-receipt.v1', 'task_id': parent['id'],
                                'project': parent['project'], 'status': 'verified',
                                'commit': 'a' * 40, 'evidence': ['test:remote-readback']}
    write(done_path, parent)
    result = run(repo, 'claim', 'child', '--repo', repo, '--allow-dirty')
    assert result.returncode == 0, result.stdout + result.stderr
