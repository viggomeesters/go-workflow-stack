"""Exact target validation runs on disposable workflow snapshots, not live state."""
import json
from pathlib import Path
import subprocess

import pytest

from test_abc_contracts import fixture, run, write


def tree(repo):
    return {str(p.relative_to(repo)): p.read_bytes()
            for p in (repo / '.go').rglob('*') if p.is_file()}


def target_runtime(tmp_path, *, reject=False, version='9.0.0'):
    stack = tmp_path / 'target-runtime'
    (stack / 'go_workflow').mkdir(parents=True)
    (stack / 'cli').mkdir()
    (stack / 'go_workflow/constants.py').write_text(
        f'STACK_VERSION = "{version}"\nCURRENT_CONTRACT_VERSION = 2\n')
    (stack / 'cli/go.py').write_text(f'''import json,sys
from pathlib import Path
assert sys.argv[1]=='validate'
repo=Path(sys.argv[2]);project=json.loads((repo/'.go/project.json').read_text())
assert project['stack_ref']=='v{version}'
assert Path.cwd()==repo
for relative in project.get('dependency_projects',{{}}).values():
 participant=(repo/relative).resolve()
 assert participant.is_relative_to(repo.parent)
 assert (participant/'.go/project.json').is_file()
print('exact target runtime v9.0.0')
if {reject!r}:
 print('error: .go/tasks/open/task-schema-smoke.json: target-only rejection',file=sys.stderr)
 raise SystemExit(1)
''')
    def git(*args):
        subprocess.run(['git', '-C', str(stack), *args], check=True, capture_output=True)
    git('init', '-q'); git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com', 'commit', '-qm', 'Target runtime')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com', 'tag', '-a', f'v{version}', '-m', 'Target')
    return stack


def test_preview_reports_target_only_failure_without_mutation(tmp_path):
    repo = fixture(tmp_path); stack = target_runtime(tmp_path, reject=True)
    before = tree(repo)
    result = run(repo, 'stack', 'update', repo, '--to', 'v9.0.0', '--stack-repo', stack, '--json')
    assert result.returncode == 1, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan['compatibility']['status'] == 'failed'
    assert 'task-schema-smoke.json' in '; '.join(plan['compatibility']['errors'])
    assert 'exact target runtime v9.0.0' in plan['compatibility']['stdout']
    assert tree(repo) == before


def test_apply_rejects_target_failure_before_any_workflow_write(tmp_path):
    repo = fixture(tmp_path); stack = target_runtime(tmp_path, reject=True)
    before = tree(repo)
    result = run(repo, 'stack', 'update', repo, '--to', 'v9.0.0', '--stack-repo', stack,
                 '--apply', '--json')
    assert result.returncode != 0
    assert tree(repo) == before


def test_apply_uses_target_verdict_instead_of_older_calling_validator(tmp_path):
    repo = fixture(tmp_path); stack = target_runtime(tmp_path)
    result = run(repo, 'stack', 'update', repo, '--to', 'v9.0.0', '--stack-repo', stack,
                 '--apply', '--json')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['compatibility']['status'] == 'passed'
    assert json.loads((repo / '.go/project.json').read_text())['stack_ref'] == 'v9.0.0'


def test_current_pin_noop_uses_valid_current_contract_not_older_tag_validator(tmp_path):
    from go_workflow.stack_update import plan_stack_update, apply_stack_update, StackUpdateError
    repo = fixture(tmp_path); stack = target_runtime(tmp_path, reject=True, version='0.3.46')
    project_path = repo / '.go/project.json'
    project = json.loads(project_path.read_text())
    project.update(required_stack_version='0.3.46', stack_ref='v0.3.46')
    write(project_path, project)
    before = tree(repo)
    plan = plan_stack_update(repo, stack, 'v0.3.46')
    assert plan['up_to_date'] is True
    assert apply_stack_update(repo, plan)['mode'] == 'noop'
    assert tree(repo) == before
    task_path = repo / '.go/tasks/open/task-schema-smoke.json'
    task_path.write_text('{corrupt')
    corrupt = tree(repo)
    with pytest.raises(StackUpdateError, match='compatibility failed'):
        apply_stack_update(repo, plan_stack_update(repo, stack, 'v0.3.46'))
    assert tree(repo) == corrupt


def test_current_pin_refuses_mutating_validator_without_touching_live_workflow(tmp_path, monkeypatch):
    from go_workflow.stack_update import plan_stack_update, apply_stack_update, StackUpdateError
    import go_workflow.cli as cli
    repo = fixture(tmp_path); stack = target_runtime(tmp_path, version='0.3.46')
    project_path = repo / '.go/project.json'
    project = json.loads(project_path.read_text())
    project.update(required_stack_version='0.3.46', stack_ref='v0.3.46')
    write(project_path, project)
    before = tree(repo)
    def mutating_validator(target):
        (target / '.go/invented.json').write_text('{}')
        return []
    monkeypatch.setattr(cli, 'validate_repo', mutating_validator)
    with pytest.raises(StackUpdateError, match='mutated its workflow snapshot'):
        apply_stack_update(repo, plan_stack_update(repo, stack, 'v0.3.46'))
    assert tree(repo) == before


def test_apply_rejects_task_changes_after_preview(tmp_path, monkeypatch):
    from go_workflow.stack_update import plan_stack_update, apply_stack_update, StackUpdateError
    monkeypatch.chdir(tmp_path)
    repo = fixture(tmp_path); stack = target_runtime(tmp_path)
    plan = plan_stack_update(repo, stack, 'v9.0.0')
    path = repo / '.go/tasks/open/task-schema-smoke.json'
    task = json.loads(path.read_text()); task['summary'] = 'User changed the task'; write(path, task)
    before = tree(repo)
    with pytest.raises(StackUpdateError, match='changed|stale'):
        apply_stack_update(repo, plan)
    assert tree(repo) == before


def test_preview_isolates_explicit_dependency_participants(tmp_path):
    repo = fixture(tmp_path); other = fixture(tmp_path, 'other'); stack = target_runtime(tmp_path)
    path = repo / '.go/project.json'; project = json.loads(path.read_text())
    project['dependency_projects'] = {'other': '../other'}; write(path, project)
    before = tree(repo), tree(other)
    result = run(repo, 'stack', 'update', repo, '--to', 'v9.0.0', '--stack-repo', stack, '--json')
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tree(repo), tree(other)) == before

@pytest.mark.parametrize('change', ['forged', 'tag', 'peer'])
def test_apply_rejects_changed_inputs(tmp_path, monkeypatch, change):
    from go_workflow.stack_update import plan_stack_update, apply_stack_update, StackUpdateError
    monkeypatch.chdir(tmp_path)
    repo = fixture(tmp_path); other = fixture(tmp_path, 'other'); stack = target_runtime(tmp_path)
    p = repo / '.go/project.json'; project = json.loads(p.read_text())
    project['dependency_projects'] = {'other': '../other'}; write(p, project)
    plan = plan_stack_update(repo, stack, 'v9.0.0')
    if change == 'forged':
        plan['after_project']['execution_defaults'] = {'unauthorized': True}
    elif change == 'peer':
        (other / '.go/new-state.json').write_text('{}')
    else:
        subprocess.run(['git', '-C', str(stack), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@example.com', 'commit', '--allow-empty', '-qm', 'Changed target'], check=True)
        subprocess.run(['git', '-C', str(stack), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@example.com', 'tag', '-fa', 'v9.0.0', '-m', 'Moved'], check=True)
    before = tree(repo), tree(other)
    with pytest.raises(StackUpdateError, match='changed|stale'):
        apply_stack_update(repo, plan)
    assert (tree(repo), tree(other)) == before


def test_validator_cannot_mutate_snapshot_and_report_success(tmp_path):
    repo = fixture(tmp_path); stack = target_runtime(tmp_path)
    entry = stack / 'cli/go.py'
    entry.write_text(entry.read_text() + "\n(repo / '.go/invented.json').write_text('{}')\n")
    for args in [('add', '.'), ('commit', '-qm', 'Mutating validator'), ('tag', '-fa', 'v9.0.0', '-m', 'Target')]:
        subprocess.run(['git', '-C', str(stack), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@example.com', *args], check=True, capture_output=True)
    before = tree(repo)
    result = run(repo, 'stack', 'update', repo, '--to', 'v9.0.0', '--stack-repo', stack, '--apply', '--json')
    assert result.returncode != 0
    assert 'mutated' in result.stderr
    assert tree(repo) == before
