"""Previously free task metadata must not silently acquire lifecycle semantics."""
import json
import subprocess

import pytest
from jsonschema import Draft202012Validator

from test_abc_contracts import ROOT, fixture, profile, run, write


@pytest.mark.parametrize('status', ['open', 'active', 'blocked', 'done'])
@pytest.mark.parametrize('metadata', [
    {'dependencies': ['legacy-predecessor', 'legacy-predecessor']},
    {'dependencies': {'old-format': 'legacy-predecessor'}},
    {'dependencies': None},
    {'verification_evidence': ['historical check output']},
    {'verification_evidence': {'old-check': 'passed'}},
    {'verification_evidence': None},
])
def test_legacy_metadata_remains_valid_and_unchanged(tmp_path, status, metadata):
    from go_workflow.cli import validate_task
    repo = fixture(tmp_path)
    source = repo / '.go/tasks/open/task-schema-smoke.json'
    task = json.loads(source.read_text())
    task.update(status=status, **metadata)
    path = repo / f'.go/tasks/{status}/task-schema-smoke.json'
    source.unlink()
    write(path, task)
    before = path.read_bytes()
    assert not validate_task(task, str(path), expected_status=status)
    schema = json.loads((ROOT / 'schemas/task.schema.json').read_text())
    assert Draft202012Validator(schema).is_valid(task)
    result = run(repo, 'validate', repo)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == before


@pytest.mark.parametrize('metadata', [
    {'dependencies': ['legacy-predecessor']},
    {'verification_evidence': ['historical check output']},
])
def test_explicit_lifecycle_metadata_stays_strict(tmp_path, metadata):
    from go_workflow.cli import validate_task
    repo = fixture(tmp_path)
    task = json.loads((repo / '.go/tasks/open/task-schema-smoke.json').read_text())
    task.update(execution_contract=profile(), **metadata)
    assert validate_task(task, task['id'])
    schema = json.loads((ROOT / 'schemas/task.schema.json').read_text())
    assert not Draft202012Validator(schema).is_valid(task)


def test_new_dependency_can_reference_legacy_done_without_reinterpreting_history(tmp_path):
    from go_workflow.execution_contracts import dependency_findings
    repo = fixture(tmp_path)
    source = repo / '.go/tasks/open/task-schema-smoke.json'
    old = json.loads(source.read_text())
    old.update(status='done', dependencies=['historical-predecessor'])
    source.unlink()
    write(repo / '.go/tasks/done/task-schema-smoke.json', old)
    new = {**old, 'id': 'new', 'status': 'open', 'execution_contract': profile(),
           'dependencies': [{'project': old['project'], 'task_id': old['id'], 'requires': 'done'}]}
    assert not dependency_findings(repo, new, readiness=True)
    new['dependencies'][0]['requires'] = 'done_with_required_release_evidence'
    assert 'receipt required' in '; '.join(dependency_findings(repo, new, readiness=True))


@pytest.mark.parametrize('metadata', [
    {'dependencies': ['legacy-predecessor']},
    {'verification_evidence': ['historical check output']},
])
def test_adoption_requires_explicit_metadata_resolution_without_mutation(tmp_path, monkeypatch, metadata):
    from go_workflow.migrations import adopt_lifecycle, MigrationError
    from test_abc_migration import settings, tree
    monkeypatch.chdir(tmp_path)
    repo = fixture(tmp_path)
    path = repo / '.go/tasks/open/task-schema-smoke.json'
    task = json.loads(path.read_text()); task.update(metadata); write(path, task)
    before = tree(repo)
    with pytest.raises(MigrationError, match=r'task-schema-smoke.*explicit.*metadata'):
        adopt_lifecycle(repo, settings(), apply=True)
    assert tree(repo) == before


@pytest.mark.parametrize('legacy_ref', ['v0.3.7', 'v0.3.14', 'v0.3.26'])
def test_upgrade_from_legacy_pin_preserves_tasks_and_rollback(tmp_path, legacy_ref):
    from go_workflow.constants import STACK_VERSION
    from go_workflow.stack_update import rollback_stack_update
    repo = fixture(tmp_path)
    p = repo / '.go/project.json'; project = json.loads(p.read_text())
    project.update(required_stack_version=legacy_ref[1:], stack_ref=legacy_ref); write(p, project)
    old_project = json.loads(p.read_text())
    task_path = repo / '.go/tasks/open/task-schema-smoke.json'
    task = json.loads(task_path.read_text())
    task.update(dependencies=['legacy-predecessor'], verification_evidence=['historical output'])
    write(task_path, task); before = task_path.read_bytes()
    hierarchy_path = repo / '.go/hierarchy.json'
    hierarchy = json.loads(hierarchy_path.read_text())
    for state in ('active', 'blocked', 'done'):
        record = {**task, 'id': f'legacy-{state}', 'status': state}
        write(repo / f'.go/tasks/{state}/{record["id"]}.json', record)
        hierarchy['epics'][0]['tasks'].append(record['id'])
    write(hierarchy_path, hierarchy)
    task_bytes = {path: path.read_bytes() for path in (repo / '.go/tasks').glob('*/*.json')}
    import shutil
    stack = tmp_path / 'stack'
    for folder in ('go_workflow', 'cli', 'schemas', 'fixtures'):
        shutil.copytree(ROOT / folder, stack / folder, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    def git(*args):
        subprocess.run(['git', '-C', str(stack), *args], check=True, capture_output=True)
    git('init', '-q'); git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com', 'commit', '-qm', 'Runtime fixture')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com', 'tag', '-a', f'v{STACK_VERSION}', '-m', 'Test runtime')
    result = run(repo, 'stack', 'update', repo, '--latest', '--stack-repo', stack, '--apply', '--json')
    assert result.returncode == 0, result.stderr
    updated = json.loads(result.stdout)
    assert updated['mode'] == 'applied' and updated['from_ref'] == legacy_ref
    assert json.loads(p.read_text())['stack_ref'] == f'v{STACK_VERSION}'
    assert task_path.read_bytes() == before
    assert all(path.read_bytes() == value for path, value in task_bytes.items())
    assert run(repo, 'validate', repo).returncode == 0
    rollback_stack_update(repo, updated['rollback_record'])
    assert json.loads(p.read_text()) == old_project
    assert task_path.read_bytes() == before
    assert all(path.read_bytes() == value for path, value in task_bytes.items())


def test_adoption_preserves_legacy_done_metadata(tmp_path, monkeypatch):
    from go_workflow.migrations import adopt_lifecycle
    from test_abc_migration import settings
    monkeypatch.chdir(tmp_path)
    repo = fixture(tmp_path)
    task = json.loads((repo / '.go/tasks/open/task-schema-smoke.json').read_text())
    task.update(id='history', status='done', dependencies=['old-predecessor'],
                verification_evidence=['old output'])
    historical = repo / '.go/tasks/done/history.json'; write(historical, task)
    h = repo / '.go/hierarchy.json'; hierarchy = json.loads(h.read_text())
    hierarchy['epics'][0]['tasks'].append('history'); write(h, hierarchy)
    before = historical.read_bytes()
    assert adopt_lifecycle(repo, settings(), apply=True)['status'] == 'applied'
    assert historical.read_bytes() == before


@pytest.mark.parametrize('defaults', [False, True])
def test_explicit_intake_rejects_legacy_dependency_shape_atomically(tmp_path, defaults):
    repo = fixture(tmp_path)
    if defaults:
        p = repo / '.go/project.json'; data = json.loads(p.read_text())
        data['execution_defaults'] = profile(); write(p, data)
    deps = tmp_path / 'dependencies.json'; write(deps, ['old-predecessor'])
    before = {p: p.read_bytes() for p in (repo / '.go').rglob('*') if p.is_file()}
    result = run(repo, 'task', 'create', repo, '--id', 'new', '--summary', 'New task',
                 '--epic', 'workflow-contract', '--dependencies', deps)
    assert result.returncode != 0 and 'Traceback' not in result.stderr
    assert {p: p.read_bytes() for p in (repo / '.go').rglob('*') if p.is_file()} == before
