"""Contract-only campaign proof: no workers, publication or canonical mutations."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from go_workflow.campaign_contracts import (
    campaign_findings, campaign_task_findings, contract_digest, validate_campaign_contract,
)

ROOT = Path(__file__).resolve().parents[1]


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + '\n')


def sample():
    return {
        'schema': 'go-workflow.campaign-contract.v1', 'id': 'bounded',
        'project': 'repo-local-spike-fixture', 'revision': 1, 'previous_sha256': None,
        'intent': {'text': 'Deliver the requested behavior',
                   'sha256': digest('Deliver the requested behavior'), 'source_ref': 'user:request'},
        'goal': {'text': 'Prove the requested behavior', 'non_goals': ['Unrelated features'],
                 'outcomes': [{'id': 'O1', 'text': 'Requested behavior works',
                               'task_outcomes': [{'task_id': 'task-schema-smoke', 'requirement_id': 'R1',
                                                   'text_sha256': digest('Requested behavior works')}],
                               'required_evidence': ['verification', 'critic']}]},
        'basis': {'vision_sha256': 'a' * 64, 'principles_sha256': 'b' * 64,
                  'decision_ids': ['bounded-policy']},
        'authority': {
            'mode': 'execute', 'source_ref': 'user:bounded-go',
            'permitted_tasks': ['task-schema-smoke'],
            'expansion': {kind: {'max_tasks': 0, 'modify': [], 'outcome_ids': []}
                          for kind in ('research', 'repair')},
            'models': [{'id': 'selected-model', 'effort': 'high'}],
            'budget': {'wall_seconds': 3600, 'max_tasks': 3, 'max_attempts': 6},
            'release': {'profiles': [], 'allow_push': False, 'source_ref': None},
            'deployment': {'targets': [], 'source_ref': None},
            'stop_conditions': ['goal_verified', 'budget_exhausted', 'no_eligible_tasks',
                                'authority_required', 'unsafe_repository', 'unknown_external_effect'],
        },
        'decisions': [
            {'id': 'bounded-policy', 'status': 'accepted', 'source_ref': 'decision:bounded-policy',
             'owner': 'maintainer', 'resolution_gate': 'existing architecture gates',
             'task_ids': ['task-schema-smoke'], 'bounds': 'Existing task and outcome authority'},
        ],
    }


def fixture(tmp_path):
    repo = tmp_path / 'repo'
    shutil.copytree(ROOT / 'fixtures/minimal', repo)
    data = sample()
    for field, name in [('vision_sha256', 'vision.json'), ('principles_sha256', 'architecture-principles.json')]:
        data['basis'][field] = hashlib.sha256((repo / '.go' / name).read_bytes()).hexdigest()
    task_path = repo / '.go/tasks/open/task-schema-smoke.json'
    task = json.loads(task_path.read_text())
    task['requested_outcomes'] = [{'id': 'R1', 'text': 'Requested behavior works',
                                   'status': 'pending', 'source': 'intake_acceptance', 'evidence': []}]
    task['execution_contract'] = {
        'schema': 'go-workflow.execution-contract.v1', 'task_kind': 'mechanical',
        'model': deepcopy(data['authority']['models'][0]),
        'release': {'mode': 'none', 'reason': 'Fixture only'},
    }
    write(task_path, task)
    write(repo / '.go/decisions/events.jsonl', {
        'schema': 'go-workflow.repo-local.event.v1', 'kind': 'event', 'event': 'decision.recorded',
        'created_at': '2026-09-19T00:00:00Z', 'task_id': task['id'], 'agent': 'fixture',
        'data': {'decision_id': 'bounded-policy', 'status': 'accepted'},
    })
    return repo, data, task


def schema_validator():
    schema = json.loads((ROOT / 'schemas/campaign-contract.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_versioned_contract_and_readonly_repository_binding(tmp_path):
    repo, data, task = fixture(tmp_path)
    schema_validator().validate(data)
    assert validate_campaign_contract(data) == []
    before = {str(p): p.read_bytes() for p in repo.rglob('*') if p.is_file()}
    assert campaign_findings(repo, data) == []
    assert campaign_task_findings(data, task) == []
    assert {str(p): p.read_bytes() for p in repo.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize(('path', 'value'), [
    (('schema',), 'unknown'), (('revision',), True), (('revision',), 0),
    (('authority', 'parallel_builders'), 2),
    (('authority', 'mode'), 'unbounded'), (('authority', 'permitted_tasks'), ['*']),
    (('authority', 'models', 0, 'effort'), 'automatic'),
    (('authority', 'models', 0, 'id'), '  '),
    (('authority', 'budget', 'wall_seconds'), 0),
    (('authority', 'budget', 'max_attempts'), True),
    (('authority', 'budget', 'max_tasks'), -1),
    (('authority', 'release', 'allow_push'), 'true'),
    (('authority', 'stop_conditions'), []),
    (('authority', 'expansion', 'research', 'modify'), ['/tmp/**']),
    (('authority', 'expansion', 'repair', 'modify'), ['../outside']),
    (('goal', 'outcomes', 0, 'task_outcomes', 0, 'requirement_id'), 'anything'),
    (('goal', 'outcomes', 0, 'required_evidence'), ['green']),
    (('intent', 'source_ref'), ''), (('basis', 'vision_sha256'), 'head'),
    (('decisions', 0, 'status'), 'approved-by-agent'),
    (('decisions', 0, 'owner'), ''), (('decisions', 0, 'resolution_gate'), ''),
])
def test_bad_shapes_rejected_by_schema_and_runtime(path, value):
    data = sample()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert list(schema_validator().iter_errors(data)), path
    assert validate_campaign_contract(data), path


@pytest.mark.parametrize('mutation, expected', [
    (lambda x: x['intent'].update(text='rewritten'), 'intent hash'),
    (lambda x: x['goal']['outcomes'].append(deepcopy(x['goal']['outcomes'][0])), 'duplicate outcome'),
    (lambda x: x['authority'].update(permitted_tasks=[]), 'permitted task'),
    (lambda x: x['goal']['outcomes'][0]['task_outcomes'][0].update(task_id='outside'), 'permitted task'),
    (lambda x: x['goal']['outcomes'][0].update(task_outcomes=[]), 'unmapped'),
    (lambda x: x['authority']['release'].update(allow_push=True), 'release authority'),
    (lambda x: x['authority']['deployment'].update(targets=['production']), 'deployment authority'),
    (lambda x: x['authority']['expansion']['research'].update(max_tasks=2), 'expansion'),
    (lambda x: x['basis'].update(decision_ids=['invented']), 'accepted decision'),
])
def test_semantic_errors_fail_closed(mutation, expected):
    data = sample()
    mutation(data)
    assert expected in '\n'.join(validate_campaign_contract(data))


def test_revisions_bind_original_intent_and_preserve_outcomes(tmp_path):
    repo, old, _ = fixture(tmp_path)
    new = deepcopy(old)
    new.update(revision=2, previous_sha256=contract_digest(old))
    new['goal']['text'] = 'Refined bounded goal'
    assert not campaign_findings(repo, new, previous=old)
    assert 'previous' in '\n'.join(campaign_findings(repo, new))
    for key, value, expected in [('revision', 3, 'revision'), ('previous_sha256', 'f' * 64, 'previous')]:
        invalid = deepcopy(new)
        invalid[key] = value
        assert expected in '\n'.join(campaign_findings(repo, invalid, previous=old))
    new['intent']['text'] = 'Replacement request'
    new['intent']['sha256'] = digest(new['intent']['text'])
    assert 'original intent' in '\n'.join(campaign_findings(repo, new, previous=old))
    new['intent'] = old['intent']
    new['goal']['outcomes'][0]['text'] = 'Different promise'
    assert 'outcome' in '\n'.join(campaign_findings(repo, new, previous=old))


def test_planning_unresolved_and_delegated_authority_are_distinct(tmp_path):
    repo, data, task = fixture(tmp_path)
    data['decisions'].append({
        'id': 'tuning', 'status': 'delegated', 'source_ref': data['authority']['source_ref'],
        'owner': 'builder', 'resolution_gate': 'task verification',
        'task_ids': [task['id']], 'bounds': 'Reversible tuning within stated modify scope',
    })
    assert not campaign_findings(repo, data)
    assert not campaign_task_findings(data, task)
    for state in ('proposed', 'unresolved'):
        data['decisions'][-1]['status'] = state
        assert not campaign_findings(repo, data)  # recording uncertainty is valid
        assert state in '\n'.join(campaign_task_findings(data, task))
    data['authority']['mode'] = 'planning'
    assert 'planning' in '\n'.join(campaign_task_findings(data, task))
    data['authority']['release'].update(profiles=['github'], allow_push=True, source_ref='user:push')
    assert 'planning' in '\n'.join(validate_campaign_contract(data))


def test_empty_planning_queue_is_valid_but_never_execution_authority():
    data = sample()
    data['authority'].update(mode='planning', permitted_tasks=[])
    data['goal']['outcomes'][0]['task_outcomes'] = []
    data['decisions'][0]['task_ids'] = []
    assert not validate_campaign_contract(data)
    assert campaign_task_findings(data, {'id': 'invented'})
    data['authority']['mode'] = 'execute'
    assert validate_campaign_contract(data)


@pytest.mark.parametrize('change, expected', [
    ('task', 'missing or duplicated task'), ('outcome', 'unknown task outcome'),
    ('decision', 'accepted governing decision'), ('vision', 'vision'), ('model', 'model'),
    ('release', 'release evidence'), ('profile', 'release profile'),
])
def test_repository_reference_drift(tmp_path, change, expected):
    repo, data, task = fixture(tmp_path)
    path = repo / '.go/tasks/open/task-schema-smoke.json'
    if change == 'task':
        path.unlink()
    elif change == 'outcome':
        task['requested_outcomes'] = []
        write(path, task)
    elif change == 'decision':
        (repo / '.go/decisions/events.jsonl').write_text('')
    elif change == 'vision':
        (repo / '.go/vision.json').write_text('{}')
    elif change == 'model':
        task['execution_contract']['model']['id'] = 'unselected'
        write(path, task)
    elif change == 'release':
        task['execution_contract'].update(task_kind='product', release={'mode': 'required'})
        write(path, task)
    else:
        data['authority']['release'].update(profiles=['absent'], source_ref='user:release')
    assert expected in '\n'.join(campaign_findings(repo, data))


def test_cli_validation_and_legacy_repo_are_readonly(tmp_path):
    from go_workflow.cli import validate_repo
    repo = tmp_path / 'legacy'
    shutil.copytree(ROOT / 'fixtures/minimal', repo)
    before = {str(p): p.read_bytes() for p in repo.rglob('*') if p.is_file()}
    assert not validate_repo(repo)
    assert {str(p): p.read_bytes() for p in repo.rglob('*') if p.is_file()} == before
    repo, data, _ = fixture(tmp_path)
    path = tmp_path / 'campaign.json'
    write(path, data)
    argv = [sys.executable, str(ROOT / 'cli/go.py'), 'validate', str(repo),
            '--campaign', str(path), '--json']
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)['valid'] is True
    data['authority']['budget']['max_tasks'] = 0
    write(path, data)
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)['valid'] is False


def test_expansion_is_bounded_delegation_not_an_executable_queue(tmp_path):
    repo, data, task = fixture(tmp_path)
    data['authority']['expansion']['repair'] = {
        'max_tasks': 2, 'modify': ['src/**', 'tests/**'], 'outcome_ids': ['O1'],
    }
    assert not campaign_findings(repo, data)
    assert 'outside permitted tasks' in '\n'.join(campaign_task_findings(data, {**task, 'id': 'invented'}))
    data['authority']['expansion']['repair']['outcome_ids'] = ['unadopted']
    assert 'unknown outcomes' in '\n'.join(validate_campaign_contract(data))


def test_task_outcome_text_drift_and_corrupt_ledger_fail_closed(tmp_path):
    repo, data, task = fixture(tmp_path)
    task['requested_outcomes'][0]['text'] = 'A different deliverable'
    write(repo / '.go/tasks/open/task-schema-smoke.json', task)
    assert 'text drift' in '\n'.join(campaign_findings(repo, data))
    with (repo / '.go/decisions/events.jsonl').open('a') as f:
        f.write('{broken-json\n')
    assert 'invalid' in '\n'.join(campaign_findings(repo, data))


def test_ambiguous_task_outcome_ids_are_rejected(tmp_path):
    repo, data, task = fixture(tmp_path)
    task['requested_outcomes'].append(deepcopy(task['requested_outcomes'][0]))
    write(repo / '.go/tasks/open/task-schema-smoke.json', task)
    assert 'duplicate task outcome IDs' in '\n'.join(campaign_findings(repo, data))


def test_independent_task_unaffected_by_another_tasks_unresolved_decision():
    data = sample()
    task = {'id': 'task-schema-smoke', 'execution_contract': {
        'schema': 'go-workflow.execution-contract.v1', 'task_kind': 'smoke',
        'model': data['authority']['models'][0], 'release': {'mode': 'none', 'reason': 'test'},
    }}
    data['authority']['permitted_tasks'].append('dependent')
    data['goal']['outcomes'][0]['task_outcomes'].append({
        'task_id': 'dependent', 'requirement_id': 'R1', 'text_sha256': 'a' * 64,
    })
    data['decisions'].append({**data['decisions'][0], 'id': 'choice', 'status': 'unresolved',
                              'task_ids': ['dependent'], 'owner': 'user',
                              'resolution_gate': 'Resolve product destination under existing authority'})
    assert not campaign_task_findings(data, task)
    assert 'unresolved' in '\n'.join(campaign_task_findings(data, {**task, 'id': 'dependent'}))


def test_release_and_deployment_authority_remain_separate(tmp_path):
    repo, data, task = fixture(tmp_path)
    project_path = repo / '.go/project.json'
    project = json.loads(project_path.read_text())
    project['release_profiles'] = {
        'configured': {'provider': 'git-tag', 'remote': 'origin', 'branch': 'main',
                       'deployment': {'mode': 'required', 'target': 'fixture-staging'}},
    }
    write(project_path, project)
    task['execution_contract'].update(task_kind='product', release={'mode': 'required', 'profile': 'configured'})
    write(repo / '.go/tasks/open/task-schema-smoke.json', task)
    data['authority']['release'].update(profiles=['configured'], allow_push=True, source_ref='user:publish')
    data['goal']['outcomes'][0]['required_evidence'].append('release')
    assert 'live evidence' in '\n'.join(campaign_findings(repo, data))
    data['goal']['outcomes'][0]['required_evidence'].append('live')
    assert not campaign_findings(repo, data)
    assert data['authority']['deployment'] == {'targets': [], 'source_ref': None}
    data['authority']['deployment'].update(targets=['fixture-staging'], source_ref='user:deploy')
    assert not campaign_findings(repo, data)
    data['authority']['deployment']['targets'] = ['unconfigured-production']
    assert 'unconfigured target' in '\n'.join(campaign_findings(repo, data))


def test_mutations_at_every_field_fail_without_crashing():
    """Malformed external JSON must yield findings, never an unhashable-type crash."""
    data = sample()
    paths = []

    def walk(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                paths.append(path + (key,))
                walk(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + (index,))
    walk(data, ())
    for path in paths:
        for replacement in (None, [], {}, True, 3.5):
            changed = deepcopy(data)
            parent = changed
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = replacement
            errors = validate_campaign_contract(changed)
            assert isinstance(errors, list), (path, replacement)
            if list(schema_validator().iter_errors(changed)):
                assert errors, (path, replacement)


def test_revision_cannot_drop_outcomes_or_weaken_delivery(tmp_path):
    repo, old, _ = fixture(tmp_path)
    old['goal']['outcomes'][0]['required_evidence'].append('release')
    new = deepcopy(old)
    new.update(revision=2, previous_sha256=contract_digest(old))
    new['goal']['outcomes'][0]['required_evidence'].remove('release')
    assert 'weakened' in '\n'.join(campaign_findings(repo, new, previous=old))
    new['goal']['outcomes'][0]['id'] = 'replacement'
    assert 'removed' in '\n'.join(campaign_findings(repo, new, previous=old))


def test_cli_rejects_invalid_json_and_previous_without_campaign(tmp_path):
    repo = tmp_path / 'legacy'
    shutil.copytree(ROOT / 'fixtures/minimal', repo)
    path = tmp_path / 'bad.json'
    path.write_text('{bad')
    argv = [sys.executable, str(ROOT / 'cli/go.py'), 'validate', str(repo), '--json']
    for options in (['--campaign', str(path)], ['--previous-campaign', str(path)]):
        result = subprocess.run(argv + options, cwd=repo, capture_output=True, text=True)
        assert result.returncode == 1, result.stdout + result.stderr
        assert json.loads(result.stdout)['valid'] is False
        assert 'Traceback' not in result.stderr
