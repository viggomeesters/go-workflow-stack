"""Joint delivery freezes real canonical tasks without claiming member completion."""
from copy import deepcopy
import json
import pytest
from test_campaign_intake import setup, write
from go_workflow.delivery_blocks import prepare_block, membership_findings, pending_block_findings, shared_proof_findings


def fixture(tmp_path):
    repo, task = setup(tmp_path)
    task['execution_contract']['workspace'] = {'mode': 'task_worktree', 'base_branch': 'main', 'control_state': 'repo_local_single_writer'}
    write(repo/'.go/tasks/open'/f'{task["id"]}.json', task)
    member = deepcopy(task);member['id'] = 'second';member['summary'] = 'Second task'
    member['scope']['modify'] = ['src/second.py']
    member['verification'] = ['python -m pytest tests/test_second.py']
    member['requested_outcomes'][0]['text'] = 'Second result'
    write(repo/'.go/tasks/open/second.json', member)
    return repo, task['id']


def prepare(repo, coordinator):
    return prepare_block(repo, coordinator, ['second'], 'Must deploy together', 'owner')


def test_canonical_group_preserves_ids_originals_and_does_not_complete_members(tmp_path):
    repo, coordinator = fixture(tmp_path)
    before=json.loads((repo/f'.go/tasks/open/{coordinator}.json').read_text())
    meta=prepare(repo, coordinator)
    combined=json.loads((repo/f'.go/tasks/open/{coordinator}.json').read_text())
    member=json.loads((repo/'.go/tasks/blocked/second.json').read_text())
    assert meta['originals'][coordinator] == before
    assert combined['requested_outcomes'][0]['id'] == before['requested_outcomes'][0]['id']
    assert len(combined['requested_outcomes']) == 2
    assert 'src/second.py' in combined['scope']['modify']
    assert member['status']=='blocked' and not (repo/'.go/tasks/done/second.json').exists()
    assert membership_findings(repo, coordinator)==[]
    assert pending_block_findings(repo)==[]
    assert shared_proof_findings(repo, coordinator)
    assert prepare(repo, coordinator)==meta


@pytest.mark.parametrize('fault', ['done','active','profile','model','cycle'])
def test_reject_unsafe_groups_without_mutation(tmp_path, fault):
    repo, coordinator=fixture(tmp_path)
    path=repo/'.go/tasks/open/second.json';task=json.loads(path.read_text())
    if fault in {'done','active'}:
        task['status']=fault;path.unlink();path=repo/f'.go/tasks/{fault}/second.json'
    elif fault=='profile':task['execution_contract']['release']={'mode':'required','profile':'other'}
    elif fault=='model':task['execution_contract']['model']['id']='other'
    else:
        task['dependencies']=[{'project':task['project'],'task_id':coordinator,'requires':'done'}]
        first=repo/f'.go/tasks/open/{coordinator}.json';data=json.loads(first.read_text())
        data['dependencies']=[{'project':task['project'],'task_id':'second','requires':'done'}];write(first,data)
    write(path,task)
    before={str(path):path.read_bytes() for path in (repo/'.go/tasks').rglob('*.json')}
    with pytest.raises(ValueError):prepare(repo,coordinator)
    assert before=={str(path):path.read_bytes() for path in (repo/'.go/tasks').rglob('*.json')}
    assert not (repo/'.go/runs'/coordinator/'delivery-block-intent.json').exists()


def test_crash_after_target_write_is_recoverable_and_selection_blocked(tmp_path,monkeypatch):
    import go_workflow.delivery_blocks as blocks
    repo,coordinator=fixture(tmp_path);original=blocks.atomic_json;failed=False
    def crash(path,value):
        nonlocal failed
        original(path,value)
        if path.name=='second.json' and not failed:
            failed=True;raise OSError('lost write ack')
    monkeypatch.setattr(blocks,'atomic_json',crash)
    with pytest.raises(OSError):prepare(repo,coordinator)
    assert pending_block_findings(repo)
    assert (repo/'.go/tasks/blocked'/f'{coordinator}.json').exists()
    assert (repo/'.go/tasks/open/second.json').exists()
    assert (repo/'.go/tasks/blocked/second.json').exists()
    monkeypatch.setattr(blocks,'atomic_json',original)
    prepare(repo,coordinator)
    assert not pending_block_findings(repo)
    assert membership_findings(repo,coordinator)==[]


def test_user_changes_during_recovery_are_preserved(tmp_path,monkeypatch):
    import go_workflow.delivery_blocks as blocks
    repo,coordinator=fixture(tmp_path);original=blocks._apply_task
    def crash(*args):raise OSError('stop')
    monkeypatch.setattr(blocks,'_apply_task',crash)
    with pytest.raises(OSError):prepare(repo,coordinator)
    path=repo/'.go/tasks/open/second.json';task=json.loads(path.read_text());task['summary']='User edit';write(path,task)
    monkeypatch.setattr(blocks,'_apply_task',original)
    with pytest.raises(ValueError,match='changed'):prepare(repo,coordinator)
    assert json.loads(path.read_text())['summary']=='User edit'


def test_source_drift_and_changed_recovery_parameters_fail(tmp_path):
    repo,coordinator=fixture(tmp_path);prepare(repo,coordinator)
    with pytest.raises(ValueError,match='intent changed'):
        prepare_block(repo,coordinator,['second'],'Different reason','owner')
    path=repo/'.go/tasks/blocked/second.json';task=json.loads(path.read_text())
    task['requested_outcomes'][0]['text']='Changed source';write(path,task)
    assert membership_findings(repo,coordinator)


def test_grouping_rejects_new_cycle_through_external_dependency(tmp_path):
    repo,coordinator=fixture(tmp_path)
    first=repo/f'.go/tasks/open/{coordinator}.json';task=json.loads(first.read_text())
    outside=deepcopy(task);outside['id']='outside';outside['dependencies']=[{'project':task['project'],'task_id':'second','requires':'done'}]
    write(repo/'.go/tasks/open/outside.json',outside)
    task['dependencies']=[{'project':task['project'],'task_id':'outside','requires':'done'}];write(first,task)
    with pytest.raises(ValueError,match='cycle'):prepare(repo,coordinator)


def test_missing_managed_workspace_is_rejected(tmp_path):
    repo,coordinator=fixture(tmp_path)
    path=repo/'.go/tasks/open/second.json';task=json.loads(path.read_text())
    del task['execution_contract']['workspace'];write(path,task)
    with pytest.raises(ValueError,match='managed task workspace'):prepare(repo,coordinator)


def test_frozen_original_contract_tampering_is_detected(tmp_path):
    repo,coordinator=fixture(tmp_path);prepare(repo,coordinator)
    path=repo/f'.go/tasks/open/{coordinator}.json';task=json.loads(path.read_text())
    task['delivery_block']['originals']['second']['requested_outcomes'][0]['text']='rewritten original'
    write(path,task)
    assert any('Frozen original contract changed' in item for item in membership_findings(repo,coordinator))


def test_parent_symlink_is_refused(tmp_path):
    repo,coordinator=fixture(tmp_path)
    other=tmp_path/'other';other.mkdir()
    (repo/'.go/runs').symlink_to(other,target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):prepare(repo,coordinator)
    assert not list(other.iterdir())


@pytest.mark.parametrize("crash",[False,True])
def test_real_joint_delivery_closes_members_only_after_shared_proof(tmp_path,monkeypatch,crash):
    from test_taskwise_no_release import setup,run
    from test_abc_worktrees import git
    from go_workflow.delivery_blocks import finalize_block,member_completion_findings
    from go_workflow.delivery_closure import synchronize_closure
    from go_workflow.campaign_delivery import delivery_report
    import subprocess
    repo,workspace,capture,args,task=setup(tmp_path,monkeypatch)
    member=deepcopy(task);member['id']='member-two';member['summary']='Check delivered length'
    member['requested_outcomes'][0]['text']='The delivered app content has five characters'
    member['verification']=["python3 -c \"from pathlib import Path; assert len(Path('app.txt').read_text())==5\""]
    write(repo/'.go/tasks/open/member-two.json',member)
    hierarchy=json.loads((repo/'.go/hierarchy.json').read_text())
    hierarchy['epics'][0]['features'][0]['tasks'].append('member-two')
    write(repo/'.go/hierarchy.json',hierarchy)
    prepare_block(repo,task['id'],['member-two'],'One shared app delivery','owner')
    with pytest.raises(ValueError,match='proof incomplete'):
        finalize_block(repo,task['id'])
    git(repo,'add','.go');git(repo,'commit','-qm','joint delivery intake');git(repo,'push','origin','main')
    args.base_commit=git(repo,'rev-parse','HEAD')
    task=json.loads((repo/'.go/tasks/open'/f'{task["id"]}.json').read_text())
    code,result=run(repo,args,task)
    assert code==0 and task['id'] in result['completed_tasks'],result.get('summary',result)
    assert (repo/'.go/tasks/blocked/member-two.json').exists()
    if crash:
        import go_workflow.delivery_blocks as blocks
        original=blocks.atomic_json
        def interrupted(path,value):
            original(path,value)
            if path.name=='member-two.json' and path.parent.name=='done':
                raise OSError('interrupted after member target write')
        monkeypatch.setattr(blocks,'atomic_json',interrupted)
        with pytest.raises(OSError):finalize_block(repo,task['id'])
        assert blocks.pending_block_findings(repo)
        monkeypatch.setattr(blocks,'atomic_json',original)
    assert finalize_block(repo,task['id'])==['member-two']
    assert synchronize_closure(repo,task['id'],policy='push')['delivered']
    clone=tmp_path/'fresh-block'
    subprocess.run(['git','clone','-q','--branch','main',str(tmp_path/'remote.git'),str(clone)],check=True)
    delivered=json.loads((clone/'.go/tasks/done/member-two.json').read_text())
    assert member_completion_findings(clone,delivered)==[]
    assert delivery_report(clone,'member-two')['delivered']
    from go_workflow import cli
    assert cli.validate_repo(clone)==[]
    assert capture.read_text().splitlines()==['build','critic']


def test_phase_context_and_skill_maps_survive_grouping(tmp_path):
    repo,coordinator=fixture(tmp_path)
    for identity,fields in [(coordinator,{'context_files':{'build':['first.md']},'skill_files':['common.md']}),
                            ('second',{'context_files':{'critic':['review.md']},'skill_files':{'build':['build.md']}})]:
        path=repo/f'.go/tasks/open/{identity}.json';task=json.loads(path.read_text());task.update(fields);write(path,task)
    prepare(repo,coordinator)
    task=json.loads((repo/f'.go/tasks/open/{coordinator}.json').read_text())
    assert task['context_files']['build']==['first.md']
    assert task['context_files']['critic']==['review.md']
    assert task['skill_files']['build']==['common.md','build.md']
    assert task['skill_files']['critic']==['common.md']


def test_different_phase_profiles_cannot_be_silently_discarded(tmp_path):
    repo,coordinator=fixture(tmp_path)
    path=repo/'.go/tasks/open/second.json';task=json.loads(path.read_text())
    task['execution_contract']['phase_profile']='different';write(path,task)
    with pytest.raises(ValueError,match='phase_profile'):prepare(repo,coordinator)


def test_existing_managed_setup_cannot_be_regrouped(tmp_path):
    repo,coordinator=fixture(tmp_path)
    write(repo/'.go/runs/second/run-state.json',{'phase':'setup'})
    with pytest.raises(ValueError,match='existing managed run'):prepare(repo,coordinator)
    assert (repo/'.go/tasks/open/second.json').exists()
