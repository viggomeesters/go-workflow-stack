"""Repaired candidates cannot quietly inherit an earlier release proof."""
import pytest
from test_abc_release import fixture, proofs


def test_candidate_revision_is_explicit_and_publication_freezes_it(tmp_path,monkeypatch):
    from go_workflow.release import prepare_release, revise_prepared_candidate, publish_release
    monkeypatch.chdir(tmp_path)
    repo,worker,source=fixture(tmp_path)
    initial=prepare_release(repo,'task-schema-smoke','owner','run-1',ship_policy='push',allow_push=True,freeze_candidate=True)
    proofs(worker,source)
    (worker/'CHANGELOG.md').write_text((worker/'CHANGELOG.md').read_text()+'\nRepaired release note.\n')
    with pytest.raises(ValueError,match='explicitly revise'):
        publish_release(repo,'task-schema-smoke','owner','run-1')
    revised=revise_prepared_candidate(repo,'task-schema-smoke','owner','run-1',reason='Correct release note')
    assert revised['revision']==2 and revised['digest']!=initial['candidate']['digest']
    with pytest.raises(ValueError,match='proof'):
        publish_release(repo,'task-schema-smoke','owner','run-1')
    proofs(worker,source)
    assert publish_release(repo,'task-schema-smoke','owner','run-1')['status']=='published'
    with pytest.raises(ValueError,match='effectless|matching active task owner'):
        revise_prepared_candidate(repo,'task-schema-smoke','owner','run-1',reason='Too late')


@pytest.mark.parametrize('initial_opt_in',[False,True])
def test_candidate_opt_in_is_frozen_across_normal_resume(tmp_path,monkeypatch,initial_opt_in):
    import json
    from test_abc_release import managed_fixture
    from go_workflow import cli
    from go_workflow.run_state import execute_managed
    repo,base,worker,_=managed_fixture(tmp_path,monkeypatch)
    monkeypatch.chdir(tmp_path)
    task=json.loads((repo/'.go/tasks/open/task-schema-smoke.json').read_text())
    parser=cli.build_parser()
    args=parser.parse_args(['auto',str(repo),'--execute','--agent','owner','--executor-agent','codex',
        '--max-commands','1','--max-minutes','5','--workspace-path',str(worker),'--workspace-branch','task/candidate',
        '--base-branch','main','--base-commit',base,'--run-id','resume-run','--ship-policy','push','--allow-push'])
    args.taskwise_delivery=initial_opt_in
    _,result=execute_managed(repo,args,'go-auto',task,cli)
    assert result['phase']=='release_prepare',result
    resumed=parser.parse_args(result['resume']['args'])
    resumed.max_commands=1
    resumed.taskwise_delivery=not initial_opt_in
    _,result=execute_managed(repo,resumed,'go-auto',task,cli)
    assert result['phase']=='verify',result
    state=json.loads((repo/'.go/runs/task-schema-smoke/release-state.json').read_text())
    assert ('candidate' in state)==initial_opt_in
