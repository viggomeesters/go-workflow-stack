"""Two release campaign and legacy completeness properties, through actual Go APIs."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_abc_worktrees import ROOT

spec=importlib.util.spec_from_file_location('abc_campaign',ROOT/'fixtures/abc-campaign/campaign.py')
campaign=importlib.util.module_from_spec(spec);spec.loader.exec_module(campaign)


@pytest.fixture(autouse=True)
def neutral_controller(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_two_models_repair_current_feedback_and_release_in_dependency_order(tmp_path,monkeypatch):
    from go_workflow.completion import lifecycle_report,read_artifact
    from go_workflow.cli import validate_repo
    repo=campaign.setup(tmp_path/'campaign')
    binary=tmp_path/'codex';binary.write_text('#!'+sys.executable+'\n'+(ROOT/'fixtures/abc-campaign/worker.py').read_text());binary.chmod(0o755)
    capture=tmp_path/'workers.jsonl';env={**os.environ,'PATH':str(tmp_path)+os.pathsep+os.environ['PATH'],
        'ABC_CAMPAIGN_CAPTURE':str(capture),'GO_STACK_ALLOW_DEV':'1','PYTHONDONTWRITEBYTECODE':'1'}
    blocked=campaign.run_task(repo,'campaign-2',env=env,expected_blocked=True)
    assert not blocked['completed_tasks'] and not capture.exists()
    assert (repo/'.go/tasks/open/campaign-2.json').exists()
    first=campaign.run_task(repo,'campaign-1',env=env)
    assert first['completed_tasks']==['campaign-1'],first
    assert (repo/'.go/tasks/open/campaign-2.json').exists()
    assert 'v1.3.0' not in campaign.git(repo,'tag','--list')
    second=campaign.run_task(repo,'campaign-2',env=env)
    assert second['completed_tasks']==['campaign-2'],second
    assert lifecycle_report(repo)['evidence_valid']
    assert not validate_repo(repo)
    calls=[json.loads(line) for line in capture.read_text().splitlines()]
    first_calls=[c for c in calls if c['task_id']=='campaign-1']
    assert [c['phase'] for c in first_calls]==['build','critic','repair','critic']
    assert 'forced critic finding C1' in json.dumps(first_calls[2]['feedback'])
    assert len({c['cwd'] for c in first_calls})==1
    for index,tid in enumerate(['campaign-1','campaign-2'],1):
        done=json.loads((repo/f'.go/tasks/done/{tid}.json').read_text())
        assert done['release_receipt']['tag']==f'v1.{index+1}.0'
        assert done['review_status']=='approved'
        ref=done['completion_evidence']['verification'];verification=read_artifact(repo/'.go',ref)
        assert verification['status']=='passed' and verification['content_unchanged']
        for c in [c for c in calls if c['task_id']==tid]:
            args=c['argv'];profile=campaign.PROFILES[index-1]
            assert args[args.index('--model')+1]==profile['id']
            assert 'model_reasoning_effort='+json.dumps(profile['effort']) in args
        assert not (repo.parent/(tid+'-worker')).exists()
    tag1=campaign.git(repo,'rev-parse','v1.2.0^{commit}');tag2=campaign.git(repo,'rev-parse','v1.3.0^{commit}')
    subprocess.run(['git','merge-base','--is-ancestor',tag1,tag2],cwd=repo,check=True)
    assert campaign.git(repo,'ls-remote','origin','refs/tags/v1.2.0^{}').startswith(tag1)
    assert campaign.git(repo,'ls-remote','origin','refs/tags/v1.3.0^{}').startswith(tag2)
    # State serialization cannot turn missing proof into valid historical completion.
    for tid in ['campaign-1','campaign-2']:
        p=repo/f'.go/tasks/done/{tid}.json';raw=p.read_text();task=json.loads(raw)
        p.write_text(json.dumps(json.loads(json.dumps(task)),indent=1));assert lifecycle_report(repo)['evidence_valid']
        for missing in ['verification','critic','release']:
            mutated=copy.deepcopy(task);mutated['completion_evidence'].pop(missing);p.write_text(json.dumps(mutated))
            assert tid in lifecycle_report(repo)['invalid_done']
        p.write_text(raw)


def test_conflicting_base_after_green_preserves_both_sides_without_release(tmp_path):
    from test_abc_release import fixture,prepare,proofs
    from go_workflow.release import publish_release,PublicationError
    repo,worker,source=fixture(tmp_path);prepare(repo);proofs(worker,source)
    # The tested worker has app.txt='delivered'; a competing base changes that path.
    (repo/'app.txt').write_text('concurrent base change')
    campaign.git(repo,'add','app.txt');campaign.git(repo,'commit','-qm','competing base content')
    with pytest.raises(PublicationError,match='base|advance|commit|branch|readback.*integration'):
        publish_release(repo,'task-schema-smoke','owner','run-1')
    assert (repo/'app.txt').read_text()=='concurrent base change'
    assert (worker/'app.txt').read_text()=='delivered'
    assert not campaign.git(repo,'ls-remote','origin','refs/tags/v1.2.0')
    assert source.exists() and not (repo/'.go/tasks/done/task-schema-smoke.json').exists()


def test_campaign_refuses_reusing_existing_destination_or_overwriting_proof(tmp_path):
    destination=tmp_path/'evidence';destination.mkdir();(destination/'keep').write_text('preserved')
    with pytest.raises(ValueError,match='new'):
        campaign.setup(destination)
    assert (destination/'keep').read_text()=='preserved'


def test_failure_matrix_points_to_collected_real_regressions():
    import ast
    matrix=json.loads((ROOT/'fixtures/abc-campaign/coverage.json').read_text())
    script=(ROOT/'scripts/check-abc.sh').read_text()
    for refs in matrix['scenarios'].values():
        for ref in refs:
            filename,name=ref.split('::')
            tree=ast.parse((ROOT/'tests'/filename).read_text())
            assert name in {node.name for node in tree.body if isinstance(node,ast.FunctionDef)}
            assert 'tests/'+filename in script


def test_manual_critic_does_not_infer_pending_controller_publication(tmp_path,monkeypatch):
    from test_abc_models import catalog_codex,controlled_fixture,task
    from go_workflow.cli import run_default_critic_agent
    capture=catalog_codex(tmp_path,monkeypatch);repo=controlled_fixture(tmp_path)
    result=run_default_critic_agent(repo,'codex',task(),1,'direct',20)
    assert result['returncode']==0
    assert 'This critic runs before controller-owned publication.' not in json.loads(capture.read_text())[-1]
