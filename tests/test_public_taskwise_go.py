"""Public Go uses taskwise delivery without upgrading explicitly saved authority."""
import json
from copy import deepcopy

from go_workflow import cli as api
from go_workflow.campaign_progress import ProgressSession
from go_workflow.progress_events import ProgressOutbox
from test_abc_worktrees import git
from test_taskwise_progress_integration import (
    integration_fixture, recording_transport, assert_real_deliveries, write,
)


def test_public_go_delivers_one_actual_task_and_acknowledges_messages(tmp_path, monkeypatch, capsys):
    repo, tasks, phases, deployments = integration_fixture(tmp_path, monkeypatch, count=1)
    transport, records, blocked = recording_transport(tmp_path, fail_task='never')
    blocked.unlink()
    path = repo / '.go/project.json'
    project = json.loads(path.read_text()); project['progress_transport'] = transport
    write(path, project)
    git(repo, 'add', '.go/project.json'); git(repo, 'commit', '-qm', 'Configure independent progress')
    git(repo, 'push', 'origin', 'main')
    args = api.build_parser().parse_args(['go', str(repo), '--execute', '--agent', 'owner',
        '--executor-agent', 'codex', '--max-commands', '50', '--max-minutes', '10', '--json'])
    code = api.cmd_go(args)
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    assert result['execution']['goal_verified'] is True, result
    assert_real_deliveries(repo, tasks, deployments)
    assert phases.read_text().splitlines() == ['task-01:build', 'task-01:critic']
    contract = json.loads(__import__('pathlib').Path(args.campaign).read_text())
    assert contract['authority']['permitted_tasks'] == tasks
    events = json.loads(records.read_text())['events']
    relevant = [event['kind'] for event in events if event.get('task_id') == tasks[0] and event['kind'] in {'start','done'}]
    assert relevant == ['start', 'done']
    assert not ProgressOutbox(repo, contract['id']).pending()


def test_public_go_saved_campaign_is_forwarded_without_new_authority(tmp_path, monkeypatch):
    args = api.build_parser().parse_args(['go', str(tmp_path), '--execute', '--campaign', str(tmp_path/'saved.json'), '--json'])
    before = deepcopy(vars(args))
    observed = []
    def existing(incoming):
        observed.append(deepcopy(vars(incoming)))
        return 7
    monkeypatch.setattr(api, 'cmd_auto', existing)
    assert api.cmd_go(args) == 7
    assert observed == [before]
    assert not (tmp_path/'.go/campaigns').exists()


def test_missing_shipping_policy_never_claims_verified_commit(tmp_path, monkeypatch):
    from go_workflow import campaign_delivery
    report = {'delivered':True, 'publication':{'status':'not_required'}, 'reported_live':False}
    monkeypatch.setattr(campaign_delivery, 'delivery_report', lambda *args: report)
    session = ProgressSession(tmp_path, {'execution':{'schema':'go-workflow.taskwise-execution.v1','mode':'until_scope'}}, 'owner')
    monkeypatch.setattr(session, '_task', lambda identity: {'summary':'Local result'})
    payload = session._delivered('T01')
    assert payload['publication']['commit'] == 'niet aangetoond'
    assert payload['publication']['push'] == 'niet aangetoond'


def test_named_existing_task_routes_without_creating_duplicate(tmp_path, monkeypatch, capsys):
    repo, tasks, _, _ = integration_fixture(tmp_path, monkeypatch, count=1)
    before = sorted(path.name for path in (repo/'.go/tasks/open').glob('*.json'))
    args = api.build_parser().parse_args(['go', str(repo), '--intent', 'Go ' + tasks[0], '--write', '--json'])
    assert api.cmd_go(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert args.task_id == tasks[0]
    assert not result.get('created_tasks')
    assert sorted(path.name for path in (repo/'.go/tasks/open').glob('*.json')) == before
