import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from go_workflow.progress_events import ProgressOutbox, render_message


@pytest.fixture
def repo(tmp_path):
    (tmp_path / '.go').mkdir()
    (tmp_path / '.go/project.json').write_text(json.dumps({'id': 'project'}))
    return tmp_path


def test_durable_retry_and_ordered_receipts(repo):
    box = ProgressOutbox(repo, 'campaign')
    first = box.emit('start', 'T01', {'title':'build'}, key='start:T01')
    second = box.emit('done', 'T01', {'result':'built'}, key='done:T01')
    restarted = ProgressOutbox(repo, 'campaign')
    assert restarted.emit('start', 'T01', {'title':'build'}, key='start:T01') == first
    assert restarted.get('start:T01') == first
    assert [event['sequence'] for event in restarted.pending()] == [1, 2]
    with pytest.raises(ValueError, match='order'):
        restarted.ack(second['id'], {'transport':'receipt2'})
    receipt = {'transport':'receipt1'}
    delivered = restarted.ack(first['id'], receipt)
    assert ProgressOutbox(repo, 'campaign').ack(first['id'], receipt) == delivered
    with pytest.raises(ValueError, match='conflicts'):
        restarted.ack(first['id'], {'transport':'another'})
    assert restarted.pending() == [second]
    with pytest.raises(ValueError, match='conflicts'):
        restarted.emit('start', 'T01', {'title':'changed'}, key='start:T01')


def test_concurrent_writers_have_one_sequence_and_no_duplicates(repo):
    def emit(index):
        return ProgressOutbox(repo, 'campaign').emit('phase', 'T01', {'phase':str(index % 10)}, key=str(index % 10))
    with ThreadPoolExecutor(max_workers=8) as workers:
        events = list(workers.map(emit, range(40)))
    assert len({event['id'] for event in events}) == 10
    assert [event['sequence'] for event in ProgressOutbox(repo, 'campaign').pending()] == list(range(1, 11))


@pytest.mark.parametrize('operation', ['emit','ack'])
def test_lost_write_acknowledgement_recovers_without_duplicates(repo, monkeypatch, operation):
    from go_workflow import progress_events
    box = ProgressOutbox(repo, 'campaign')
    if operation == 'ack':
        event = box.emit('done', 'T01', {}, key='done')
    original = progress_events.atomic_json
    def lost(path, state):
        original(path, state)
        raise OSError('lost write acknowledgement')
    monkeypatch.setattr(progress_events, 'atomic_json', lost)
    with pytest.raises(OSError):
        if operation == 'emit':
            box.emit('done', 'T01', {}, key='done')
        else:
            box.ack(event['id'], {'id':'remote-receipt'})
    monkeypatch.setattr(progress_events, 'atomic_json', original)
    box = ProgressOutbox(repo, 'campaign')
    event = box.emit('done', 'T01', {}, key='done')
    if operation == 'ack':
        assert box.ack(event['id'], {'id':'remote-receipt'})['status'] == 'acknowledged'
    assert len(box.snapshot()['events']) == 1


def test_unknown_heartbeat_does_not_invent_activity(repo):
    event = ProgressOutbox(repo, 'campaign').emit('heartbeat', 'T01', {}, key='tick:1')
    assert 'activiteit: onbekend' in event['message']
    assert 'voortgang: onbekend' in event['message']
    assert 'Controles: onbekend' in render_message('done','T01',{})


@pytest.mark.parametrize('identity', ['../bad', '', '/absolute', 'with space'])
def test_bad_campaign_ids_rejected(repo, identity):
    with pytest.raises(ValueError):
        ProgressOutbox(repo, identity)


def test_symlink_and_corrupt_state_rejected(repo, tmp_path):
    box = ProgressOutbox(repo, 'campaign')
    event = box.emit('start', 'T01', {}, key='start')
    state = box.snapshot(); state['events'][0]['sequence'] = 7
    box.path.write_text(json.dumps(state))
    with pytest.raises(ValueError):
        box.pending()
    box.path.unlink()
    outside = tmp_path / 'outside.json'; outside.write_text('{}')
    box.path.symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        box.emit('start', 'T01', {}, key='start')


def test_state_and_event_schemas(repo):
    from pathlib import Path
    import jsonschema
    from referencing import Registry, Resource
    root = Path(__file__).resolve().parents[1] / 'schemas'
    event_schema = json.loads((root/'progress-event.schema.json').read_text())
    state_schema = json.loads((root/'progress-state.schema.json').read_text())
    box = ProgressOutbox(repo, 'campaign')
    event = box.emit('start', 'T01', {}, key='start')
    jsonschema.validate(event,event_schema)
    registry = Registry().with_resource('progress-event.schema.json',Resource.from_contents(event_schema))
    jsonschema.Draft202012Validator(state_schema,registry=registry).validate(box.snapshot())


def test_run_end_renders_scope_per_task_shipping_repairs_and_remaining():
    message = render_message('run_end', None, {
        'summary':'2 taken afgerond; status blocked',
        'original_task_ids':['T01','T02'], 'added_task_ids':['T01b'],
        'results':[{'task_id':'T01','summary':'Fout hersteld','checks':'geslaagd',
                    'publication':{'commit':'geverifieerd','push':'geverifieerd','release':'geen release nodig','deployment':'niet van toepassing'}},
                   {'task_id':'T01b','summary':'Afhankelijkheid hersteld','checks':'geslaagd','publication':{}}],
        'remaining_tasks':[{'task_id':'T02','status':'blocked'}],
        'changes':[{'event':'campaign.repair_admitted','task_id':'T01b','reason':'Noodzakelijk voor T01'},
                   {'event':'campaign.task_amended','task_id':'T02','reason':'Nieuw broncontract'}]})
    for expected in ['Oorspronkelijke taken: T01, T02', 'Toegevoegd herstelwerk: T01b',
                     'T01 done — Fout hersteld', 'push: geverifieerd', 'deployment: niet van toepassing',
                     'T01b done — Afhankelijkheid hersteld', 'T02 — blocked; nog niet afgerond',
                     'Noodzakelijk voor T01', 'Nieuw broncontract']:
        assert expected in message
