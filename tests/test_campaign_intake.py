"""Real repository bindings for read-only until-scope intake."""
from copy import deepcopy
import hashlib
import json

import pytest

from go_workflow.campaign_contracts import campaign_findings
from go_workflow.campaign_intake import materialize_until_scope
from test_autonomy_contracts import fixture, write


def setup(tmp_path):
    repo, _, task = fixture(tmp_path)
    ledger = repo / '.go/decisions/events.jsonl'
    event = json.loads(ledger.read_text())
    event['data']['decision'] = 'Keep campaign work bound to existing requested outcomes.'
    write(ledger, event)
    return repo, task


def intake(repo, **kwargs):
    return materialize_until_scope(repo, intent='Go tot alle taken klaar',
                                   source_ref='user:explicit-current-request', campaign_id='all-work', **kwargs)


def test_default_snapshot_includes_blocked_but_freezes_new_scope_read_only(tmp_path):
    repo, task = setup(tmp_path)
    blocked = deepcopy(task)
    blocked.update(id='blocked-work', status='blocked')
    write(repo / '.go/tasks/blocked/blocked-work.json', blocked)
    before = {str(path): path.read_bytes() for path in repo.rglob('*') if path.is_file()}
    contract = intake(repo)
    assert set(contract['authority']['permitted_tasks']) == {task['id'], 'blocked-work'}
    assert contract['authority']['budget'] == dict(wall_seconds=None, max_tasks=None, max_attempts=None)
    assert campaign_findings(repo, contract) == []
    assert before == {str(path): path.read_bytes() for path in repo.rglob('*') if path.is_file()}
    task.update(id='later-work')
    write(repo / '.go/tasks/open/later-work.json', task)
    assert 'later-work' not in contract['authority']['permitted_tasks']
    assert campaign_findings(repo, contract) == []


def test_original_text_is_bound_and_drift_rejected(tmp_path):
    repo, task = setup(tmp_path)
    contract = intake(repo)
    link = contract['goal']['outcomes'][0]['task_outcomes'][0]
    assert link['text_sha256'] == hashlib.sha256(task['requested_outcomes'][0]['text'].encode()).hexdigest()
    task['requested_outcomes'][0]['text'] = 'Changed request'
    write(repo / '.go/tasks/open' / f'{task["id"]}.json', task)
    assert any('text drift' in item for item in campaign_findings(repo, contract))


@pytest.mark.parametrize('missing', ['execution_contract', 'requested_outcomes'])
def test_legacy_prerequisites_are_not_invented(tmp_path, missing):
    repo, task = setup(tmp_path)
    del task[missing]
    write(repo / '.go/tasks/open' / f'{task["id"]}.json', task)
    with pytest.raises(ValueError, match=missing):
        intake(repo)


def test_no_decision_or_invented_authority(tmp_path):
    repo, _ = setup(tmp_path)
    (repo / '.go/decisions/events.jsonl').write_text('')
    with pytest.raises(ValueError, match='No accepted repository decision'):
        intake(repo)
    with pytest.raises(ValueError, match='authorization'):
        materialize_until_scope(repo, intent='Go', source_ref='', campaign_id='x')


def test_push_and_deployment_follow_exact_task_profiles_and_restrictions(tmp_path):
    repo, task = setup(tmp_path)
    project_path = repo / '.go/project.json'
    project = json.loads(project_path.read_text())
    project['release_profiles'] = {'production': {'deployment': {'mode': 'required', 'target': 'configured-live'}},
                                   'unrelated': {'deployment': {'mode': 'required', 'target': 'not-authorized'}}}
    write(project_path, project)
    task['execution_contract']['release'] = {'mode': 'required', 'profile': 'production'}
    write(repo / '.go/tasks/open' / f'{task["id"]}.json', task)
    first = intake(repo)
    assert first['authority']['release']['allow_push'] is True
    assert first['authority']['release']['profiles'] == ['production']
    assert first['authority']['deployment']['targets'] == ['configured-live']
    project['taskwise_policy'] = {'allow_push': False, 'allow_deployment': False}
    write(project_path, project)
    restricted = intake(repo)
    assert restricted['authority']['release']['allow_push'] is False
    assert restricted['authority']['deployment'] == {'targets': [], 'source_ref': None}
    assert 'live' in restricted['goal']['outcomes'][0]['required_evidence']
    # Existing objects retain original authority: intake does not migrate runs.
    assert first['authority']['release']['allow_push'] is True


def test_explicit_budget_selection_and_no_release_exception(tmp_path):
    repo, task = setup(tmp_path)
    contract = intake(repo, task_ids=[task['id']], budget={'wall_seconds': 600})
    assert contract['authority']['budget']['wall_seconds'] == 600
    assert contract['authority']['release'] == {
        'profiles': [], 'allow_push': True, 'source_ref': 'user:explicit-current-request'}
    assert contract['execution']['shipping']['policy'] == 'push'
    with pytest.raises(ValueError, match='must exist'):
        intake(repo, task_ids=['absent'])
    with pytest.raises(ValueError, match='duplicate'):
        intake(repo, task_ids=[task['id'], task['id']])
    with pytest.raises(ValueError, match='budget'):
        intake(repo, budget={'max_tasks': False})

@pytest.mark.parametrize('ship_policy', ['none', 'local-commit'])
def test_explicit_shipping_restrictions_survive_intake(tmp_path, ship_policy):
    repo, task = setup(tmp_path)
    project_path = repo / '.go/project.json'
    project = json.loads(project_path.read_text())
    project['release_profiles'] = {'production': {}}
    write(project_path, project)
    task['execution_contract']['release'] = {'mode':'required','profile':'production'}
    write(repo/'.go/tasks/open'/f'{task["id"]}.json', task)
    contract = intake(repo, ship_policy=ship_policy)
    assert contract['authority']['release']['allow_push'] is False
    assert contract['execution']['shipping'] == {
        'schema': 'go-workflow.taskwise-shipping.v1', 'policy': ship_policy,
        'source_ref': 'user:explicit-current-request'}
    assert contract['authority']['release']['profiles'] == ['production']
    assert 'release' in contract['goal']['outcomes'][0]['required_evidence']


@pytest.mark.parametrize('requested,expected', [(None, 'local-commit'), ('push', 'local-commit'),
                                               ('local-commit', 'local-commit'), ('none', 'none')])
def test_repository_push_restriction_caps_current_request(tmp_path, requested, expected):
    repo, _ = setup(tmp_path)
    path = repo / '.go/project.json'
    project = json.loads(path.read_text())
    project['taskwise_policy'] = {'allow_push': False}
    write(path, project)
    contract = intake(repo, ship_policy=requested)
    assert contract['execution']['shipping']['policy'] == expected
    assert contract['authority']['release']['allow_push'] is False


@pytest.mark.parametrize('shipping', ['none', 'local-commit'])
def test_cli_restriction_also_withholds_deployment_without_weakening_evidence(tmp_path, shipping):
    repo, task = setup(tmp_path)
    path = repo / '.go/project.json'
    project = json.loads(path.read_text())
    project['release_profiles'] = {'production': {'deployment': {'mode': 'required', 'target': 'live'}}}
    write(path, project)
    task['execution_contract']['release'] = {'mode': 'required', 'profile': 'production'}
    write(repo / '.go/tasks/open' / f'{task["id"]}.json', task)
    contract = intake(repo, ship_policy=shipping)
    assert contract['authority']['deployment'] == {'targets': [], 'source_ref': None}
    assert 'live' in contract['goal']['outcomes'][0]['required_evidence']


@pytest.mark.parametrize('policy', ['force', '', False, {'mode': 'push'}])
def test_invalid_shipping_is_not_inferred(tmp_path, policy):
    repo, _ = setup(tmp_path)
    with pytest.raises(ValueError, match='ship_policy'):
        intake(repo, ship_policy=policy)
