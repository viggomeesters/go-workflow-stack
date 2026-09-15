"""Readiness gate behavior through the real CLI; semantic review is separate."""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'cli/go.py'


def call(repo, *args, env=None):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=repo,
                          env=env, text=True, capture_output=True, timeout=60)


def seed(tmp_path, *, decision_status='proposed', impact='local'):
    repo = tmp_path / 'project'
    shutil.copytree(ROOT / 'fixtures/minimal', repo)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    path = repo / '.go/tasks/open/task-schema-smoke.json'
    task = json.loads(path.read_text())
    task.update(summary='Retarget a moving marker', description='Apply the selected retargeting rule.',
                execution_mode='agent', shareable_delivery='none',
                scope={'read': ['**'], 'modify': ['app.txt']},
                acceptance=['A second target during travel follows the accepted rule.'],
                verification=['git diff --check'],
                architecture={'impact': impact, 'decision_ids': ['retarget-rule']})
    path.write_text(json.dumps(task, indent=2) + '\n')
    decisions = repo / '.go/decisions/events.jsonl'
    decisions.parent.mkdir(exist_ok=True)
    if decision_status is not None:
        record = {'schema': 'go-workflow.repo-local.event.v1', 'kind': 'event',
                  'event': 'decision.recorded', 'created_at': '2026-09-15T10:00:00+02:00',
                  'task_id': task['id'], 'agent': 'fixture',
                  'data': {'decision_id': 'retarget-rule', 'title': 'Retargeting behavior',
                           'status': decision_status, 'context': 'A second click during travel.',
                           'decision': 'Retarget immediately from the current position.',
                           'consequences': ['Do not jump back to the original position.']}}
        decisions.write_text(json.dumps(record) + '\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qm', 'Seed task'], check=True)
    return repo, task


def test_local_task_cannot_claim_an_unresolved_explicit_rule(tmp_path):
    repo, task = seed(tmp_path)
    path = repo / '.go/tasks/open/task-schema-smoke.json'
    before = path.read_bytes()
    assert call(repo, 'validate', repo).returncode == 0
    result = call(repo, 'claim', task['id'], '--repo', repo, '--agent', 'fixture')
    assert result.returncode != 0, 'A structurally valid task claimed an unresolved governing rule'
    assert 'retarget-rule' in result.stderr and 'accepted' in result.stderr
    assert path.read_bytes() == before
    assert not (repo / '.go/tasks/active/task-schema-smoke.json').exists()


@pytest.mark.parametrize('impact', ['none', 'local'])
@pytest.mark.parametrize('status', [None, 'rejected', 'superseded', 'accepted'])
def test_explicit_rule_state_controls_claim_without_requiring_a_new_brief(tmp_path, impact, status):
    repo, task = seed(tmp_path, decision_status=status, impact=impact)
    result = call(repo, 'claim', task['id'], '--repo', repo, '--agent', 'fixture')
    assert (result.returncode == 0) is (status == 'accepted'), result.stderr
    if status != 'accepted':
        assert 'retarget-rule' in result.stderr
    else:
        assert not (repo / '.go/architecture/briefs').exists()


def test_finish_rechecks_a_rule_invalidated_after_claim(tmp_path):
    repo, task = seed(tmp_path, decision_status='accepted')
    assert call(repo, 'claim', task['id'], '--repo', repo, '--agent', 'fixture').returncode == 0
    path = repo / '.go/decisions/events.jsonl'
    record = json.loads(path.read_text().splitlines()[-1])
    record['data']['status'] = 'superseded'
    with path.open('a') as stream:
        stream.write(json.dumps(record) + '\n')
    result = call(repo, 'finish', task['id'], '--repo', repo, '--agent', 'fixture',
                  '--evidence', 'All checks passed')
    assert result.returncode != 0 and 'retarget-rule' in result.stderr
    assert (repo / '.go/tasks/active/task-schema-smoke.json').exists()
    assert not (repo / '.go/tasks/done/task-schema-smoke.json').exists()


@pytest.mark.parametrize('summary,description,scope', [
    ('Correct a typo', 'Change teh to the in one sentence.', ['README.md']),
    ('Compare retargeting alternatives', 'Research only. Produce observations and a recommendation; do not change the app.', ['docs/research.md']),
    ('Tune the duration', 'Retarget behavior is fixed. Choose and document a duration between 120 and 180 ms.', ['app.txt']),
])
def test_small_research_and_delegated_work_do_not_inherit_unrelated_open_decisions(tmp_path, summary, description, scope):
    repo, task = seed(tmp_path)
    path = repo / '.go/tasks/open/task-schema-smoke.json'
    task.update(summary=summary, description=description,
                scope={'read': ['**'], 'modify': scope}, architecture={'impact': 'local'})
    path.write_text(json.dumps(task))
    result = call(repo, 'claim', task['id'], '--repo', repo, '--agent', 'fixture')
    assert result.returncode == 0, result.stderr


def test_legacy_task_keeps_its_unmigrated_dependency_metadata(tmp_path):
    repo, task = seed(tmp_path)
    task.pop('architecture')
    task['dependencies'] = ['historical-note-not-a-runtime-edge']
    path = repo / '.go/tasks/open/task-schema-smoke.json'
    path.write_text(json.dumps(task))
    assert call(repo, 'validate', repo).returncode == 0
    result = call(repo, 'claim', task['id'], '--repo', repo, '--agent', 'fixture')
    assert result.returncode == 0, result.stderr


def test_pending_bug_outcome_cannot_finish_on_passing_process_prose(tmp_path):
    repo, task = seed(tmp_path, decision_status='accepted')
    assert call(repo, 'claim', task['id'], '--repo', repo, '--agent', 'fixture').returncode == 0
    path = repo / '.go/tasks/active/task-schema-smoke.json'
    active = json.loads(path.read_text())
    active.update(outcome_tracking_version=1, requested_outcomes=[
        {'id': 'R1', 'text': 'Previously observed snap is reproduced and prevented',
         'source': 'user', 'status': 'pending', 'evidence': []}])
    path.write_text(json.dumps(active))
    result = call(repo, 'finish', task['id'], '--repo', repo, '--agent', 'fixture',
                  '--evidence', 'A hypothesis document and all structural checks are complete')
    assert result.returncode != 0 and 'R1' in result.stderr
    assert json.loads(path.read_text())['requested_outcomes'][0]['status'] == 'pending'


def test_declared_research_dependency_blocks_dependent_implementation(tmp_path):
    repo, task = seed(tmp_path, decision_status='accepted')
    task['execution_contract'] = {
        'schema': 'go-workflow.execution-contract.v1', 'task_kind': 'no_change',
        'model': {'id': 'gpt-6-astra', 'effort': 'high'},
        'release': {'mode': 'none', 'reason': 'This is a disposable dependency preflight test'}}
    task['dependencies'] = [{'project': task['project'], 'task_id': 'research-rule', 'requires': 'done'}]
    path = repo / '.go/tasks/open/task-schema-smoke.json'
    path.write_text(json.dumps(task))
    research = {**task, 'id': 'research-rule', 'dependencies': [],
                'summary': 'Research the retargeting choice', 'architecture': {'impact': 'local'}}
    (path.parent / 'research-rule.json').write_text(json.dumps(research))
    result = call(repo, 'claim', task['id'], '--repo', repo, '--agent', 'fixture')
    assert result.returncode != 0 and 'dependency not done/approved: research-rule' in result.stderr
    assert json.loads(path.read_text())['status'] == 'open'


def test_valid_auto_plan_requests_content_review_without_claiming_semantic_readiness(tmp_path):
    repo, task = seed(tmp_path, decision_status='accepted')
    result = call(repo, 'auto', repo, '--json')
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    review = plan['agent_contract'].get('task_design_review')
    assert review is not None, 'An execution handoff needs the author/reviewer assessment contract'
    assert review['kind'] == 'review_instructions_not_verdict'
    assert set(review['dispositions']) == {'ready', 'design_research_first', 'blocked_on_material_decision'}
    assert review['evidence_required'] and 'status' not in review
    assert json.loads((repo / '.go/tasks/open/task-schema-smoke.json').read_text())['status'] == 'open'


def test_auto_cannot_build_before_an_explicit_design_dependency_is_resolved(tmp_path):
    repo, task = seed(tmp_path)
    command = shlex.join([sys.executable, '-c', "from pathlib import Path; Path('app.txt').write_text('built')"])
    result = call(repo, 'auto', repo, '--execute', '--agent', 'fixture', '--max-attempts', '1',
                  '--build-command', command, '--critic-command', 'true', '--executor-agent', 'none', '--json')
    assert not (repo / 'app.txt').exists(), 'Autonomous execution built before checking its governing decision'
    assert result.returncode != 0 and 'retarget-rule' in result.stdout
    assert (repo / '.go/tasks/open/task-schema-smoke.json').exists()


@pytest.mark.parametrize('brief_state', ['missing', 'draft', 'accepted'])
def test_explicit_local_brief_remains_a_dependency(tmp_path, brief_state):
    from test_architecture import valid_brief
    repo, task = seed(tmp_path, decision_status='accepted')
    task['architecture'] = {'impact': 'local', 'scope_refs': ['motion']}
    (repo / '.go/tasks/open/task-schema-smoke.json').write_text(json.dumps(task))
    if brief_state != 'missing':
        brief = valid_brief()
        brief.update(id='motion', project=task['project'], status=brief_state, decision_ids=['retarget-rule'])
        path = repo / '.go/architecture/briefs/motion.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(brief))
    result = call(repo, 'claim', task['id'], '--repo', repo, '--agent', 'fixture')
    assert (result.returncode == 0) is (brief_state == 'accepted'), result.stderr
    if brief_state != 'accepted':
        assert 'motion' in result.stderr


def test_native_review_receives_guidance_and_can_refuse_unproven_bug_closure(tmp_path):
    repo, task = seed(tmp_path, decision_status='accepted')
    binary_dir = tmp_path / 'bin'
    binary_dir.mkdir()
    capture = tmp_path / 'calls.jsonl'
    binary = binary_dir / 'codex'
    # Protocol transport fixture. It does not simulate or claim semantic intelligence.
    binary.write_text('#!' + sys.executable + '\n' + '''
import json,os,sys
from pathlib import Path
phase=os.environ['GO_HOOK'];context=json.loads(os.environ['GO_CONTEXT_JSON'])
with Path(os.environ['READINESS_CAPTURE']).open('a') as stream:
    stream.write(json.dumps({'phase':phase,'argv':sys.argv,'context':context})+'\\n')
if phase=='build': Path('app.txt').write_text('Investigation notes only; reported snap remains unreproduced.\\n')
print(json.dumps({'schema':'go-workflow.agent-adapter-result.v1','phase':phase,
    'status':'blocked' if phase=='critic' else 'success',
    'summary':'R1 remains unproven: app.txt contains hypotheses; no reproduction or correction evidence.' if phase=='critic' else 'Investigation artifact written.'}))
''')
    binary.chmod(0o755)
    env = {**os.environ, 'PATH': str(binary_dir) + os.pathsep + os.environ['PATH'],
           'READINESS_CAPTURE': str(capture)}
    result = call(repo, 'auto', repo, '--execute', '--agent', 'fixture', '--executor-agent', 'codex',
                  '--max-attempts', '1', '--json', env=env)
    assert result.returncode != 0, result.stdout
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [item['phase'] for item in calls] == ['build', 'critic']
    for item in calls:
        review = item['context']['task_design_review']
        assert review['kind'] == 'review_instructions_not_verdict' and 'status' not in review
        role = 'author' if item['phase'] == 'build' else 'critic'
        assert 'task_design_review.' + role in ' '.join(item['argv'])
    assert not (repo / '.go/tasks/done/task-schema-smoke.json').exists()
    assert 'R1 remains unproven' in result.stdout


def test_paired_examples_are_structurally_valid_without_becoming_semantic_verdicts():
    from jsonschema import Draft202012Validator
    from go_workflow.cli import task_contract_findings, validate_task
    examples = json.loads((ROOT / 'tests/fixtures/task-design-readiness.json').read_text())
    schema = json.loads((ROOT / 'schemas/task.schema.json').read_text())
    cases = {case['id']: case for case in examples['cases']}
    for case in cases.values():
        Draft202012Validator(schema).validate(case['task'])
        assert validate_task(case['task'], case['id']) == []
    # Existing structural heuristics deliberately cannot distinguish this pair.
    # The supplied reviews must be read as content assessments, never test output.
    for name in ('vague-motion', 'actionable-motion'):
        assert task_contract_findings(cases[name]['task']) == []
