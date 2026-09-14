"""Guided facts/settings feed the existing intake and real release controller."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_abc_worktrees import ROOT, CLI, git


def call(repo, *args, env=None):
    result = subprocess.run([sys.executable, str(CLI), *map(str,args)], cwd=repo,
                            env=env, text=True, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def fixture(tmp_path):
    repo = tmp_path / 'project'; repo.mkdir()
    subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
    git(repo,'config','user.name','Fixture'); git(repo,'config','user.email','fixture@example.com')
    (repo/'VERSION').write_text('1.1.0\n')
    (repo/'Makefile').write_text('check:\n\tgit diff --check\n')
    return repo


def answers():
    return {'model':{'id':'gpt-6-astra','effort':'medium'},'base_branch':'main',
        'verification':["python3 -c \"from pathlib import Path; assert Path('app.txt').read_text() == 'two\\n'; assert Path('VERSION').read_text().strip() == '1.2.0'\""],
        'publisher':{'provider':'git-tag','remote':'origin'},
        'version':{'path':'VERSION','format':'text'},'bump':'minor','tag_prefix':'v','changelog':'CHANGELOG.md',
        'deployment':{'mode':'none','reason':'This fixture publishes only a local Git release'},
        'task':{'id':'first-release','summary':'Write app.txt with two followed by a newline',
                'scope':{'read':['app.txt'],'modify':['app.txt']},'acceptance':['app.txt contains two and the required release is verified']}}


def files(repo):
    return {str(p.relative_to(repo)):p.read_bytes() for p in repo.rglob('*') if p.is_file() and '.git' not in p.parts}


def test_detect_facts_without_selecting_policy_or_writing(tmp_path):
    from go_workflow.onboarding import plan_onboarding
    repo=fixture(tmp_path); before=files(repo)
    plan=plan_onboarding(repo)
    assert plan['status']=='needs_configuration' and plan['settings'] is None and plan['execution_brief'] is None
    assert plan['facts']['base_branch']=='main'
    assert {'path':'VERSION','format':'text','current':'1.1.0'} in plan['facts']['version_sources']
    assert 'make check' in plan['facts']['verification_candidates']
    assert {'model','publisher','verification','deployment'} <= {q['field'] for q in plan['questions']}
    assert files(repo)==before and not (repo/'.go').exists()


def test_resolved_settings_and_task_scope_are_reviewable(tmp_path):
    from go_workflow.onboarding import plan_onboarding
    repo=fixture(tmp_path); before=files(repo); plan=plan_onboarding(repo, answers())
    assert plan['status']=='ready' and not plan['questions']
    unit=plan['execution_brief']['work_units'][0]
    assert unit['scope']['modify']==['app.txt','VERSION','CHANGELOG.md']
    assert unit['verification']==answers()['verification']
    assert plan['settings']['execution_defaults']['model']==answers()['model']
    assert 'allow_push' not in json.dumps(plan['settings'])
    assert files(repo)==before
    import jsonschema
    jsonschema.validate(plan,json.loads((ROOT/'schemas/onboarding-plan.schema.json').read_text()))
    jsonschema.validate(plan['settings'],json.loads((ROOT/'schemas/lifecycle-settings.schema.json').read_text()))


@pytest.mark.parametrize('field,value', [('allow_push',True),('model',{}),('verification',[]),
    ('version',{'path':'../foreign','format':'text'}),('publisher',{'provider':'github-release','remote':'origin'}),
    ('deployment',{'mode':'none'}),('task',{'id':'bad','summary':'Missing scope'})])
def test_invalid_or_authority_answers_fail_without_writing(tmp_path, field, value):
    from go_workflow.onboarding import plan_onboarding, OnboardingError
    repo=fixture(tmp_path); before=files(repo); config=answers(); config[field]=value
    with pytest.raises(OnboardingError):plan_onboarding(repo,config)
    assert files(repo)==before and not (repo/'.go').exists()


def test_cli_json_and_toml_detection(tmp_path):
    repo=fixture(tmp_path); (repo/'pyproject.toml').write_text('[project]\nversion="2.0.0"\n')
    result=call(repo,'onboarding','plan',repo,'--json'); plan=json.loads(result.stdout)
    assert {'path':'pyproject.toml','format':'toml','key':'project.version','current':'2.0.0'} in plan['facts']['version_sources']
    assert not (repo/'.go').exists()


def test_guided_first_task_completes_through_existing_native_controller(tmp_path, monkeypatch):
    from go_workflow.onboarding import plan_onboarding
    from go_workflow.cli import create_tasks_from_execution_brief
    from go_workflow.completion import lifecycle_report
    monkeypatch.chdir(tmp_path)
    repo=fixture(tmp_path); remote=tmp_path/'remote.git'
    subprocess.run(['git','init','--bare','-q',str(remote)],check=True); git(repo,'remote','add','origin',str(remote))
    plan=plan_onboarding(repo,answers()); config=tmp_path/'settings.json';config.write_text(json.dumps(plan['settings']))
    call(repo,'adopt',repo,'--project-id','guided-fixture','--name','Guided fixture','--lifecycle-settings',config)
    created=create_tasks_from_execution_brief(repo,plan['execution_brief'],'fixture')
    task=json.loads((repo/created[0]['path']).read_text())
    assert task['status']=='open' and not task['claim']['agent']
    assert all(o['status']=='pending' and not o['evidence'] for o in task['requested_outcomes'])
    git(repo,'add','.');git(repo,'commit','-qm','Explicit guided project')
    git(repo,'tag','-a','v1.1.0','-m','Baseline');git(repo,'push','origin','main','refs/tags/v1.1.0')
    binary_dir=tmp_path/'bin';binary_dir.mkdir();binary=binary_dir/'codex'
    binary.write_text('#!'+sys.executable+'\n'+(ROOT/'fixtures/abc-campaign/worker.py').read_text());binary.chmod(0o755)
    capture=tmp_path/'workers.jsonl'
    env={**os.environ,'PATH':str(binary_dir)+os.pathsep+os.environ['PATH'],
         'ABC_CAMPAIGN_CAPTURE':str(capture),'GO_STACK_ALLOW_DEV':'1','PYTHONDONTWRITEBYTECODE':'1'}
    result=call(repo,'auto',repo,'--execute','--task-id','first-release','--agent','fixture','--executor-agent','codex',
        '--max-commands','30','--max-minutes','4','--workspace-path',tmp_path/'worker','--workspace-branch','task/first',
        '--base-branch','main','--base-commit',git(repo,'rev-parse','HEAD'),'--run-id','first-run',
        '--ship-policy','push','--allow-push','--json',env=env)
    result=json.loads(result.stdout)
    assert result['status'] in {'done','task_complete'},result
    done=json.loads((repo/'.go/tasks/done/first-release.json').read_text())
    assert done['review_status']=='approved' and done['release_receipt']['tag']=='v1.2.0'
    assert lifecycle_report(repo)['evidence_valid']
    assert [json.loads(line)['phase'] for line in capture.read_text().splitlines()]==['build','critic']
    assert not (tmp_path/'worker').exists()


def test_history_and_linked_version_inputs_remain_untouched(tmp_path):
    from go_workflow.onboarding import plan_onboarding, OnboardingError
    repo=fixture(tmp_path); (repo/'.go').mkdir(); (repo/'.go/history.json').write_text('{"keep":"history"}')
    before=files(repo); plan_onboarding(repo,answers()); assert files(repo)==before
    (repo/'VERSION').unlink(); outside=tmp_path/'private-version';outside.write_text('1.1.0\n')
    (repo/'VERSION').symlink_to(outside)
    with pytest.raises(OnboardingError,match='inside the project'):
        plan_onboarding(repo,answers())
    assert outside.read_text()=='1.1.0\n' and (repo/'VERSION').is_symlink()


def test_detection_does_not_execute_candidate_checks(tmp_path):
    from go_workflow.onboarding import plan_onboarding
    repo=fixture(tmp_path); (repo/'Makefile').write_text('check:\n\ttouch should-not-run\n')
    plan=plan_onboarding(repo)
    assert 'make check' in plan['facts']['verification_candidates']
    assert not (repo/'should-not-run').exists()


def test_owned_worker_can_inspect_read_only_onboarding(tmp_path, monkeypatch):
    from test_abc_worktrees import setup_repo, create
    monkeypatch.chdir(tmp_path)
    repo, base=setup_repo(tmp_path); worker=tmp_path/'worker'
    created=create(repo,base,worker); assert created.returncode==0,created.stderr
    before=files(repo), files(worker)
    result=call(worker,'onboarding','plan',worker,'--json')
    assert json.loads(result.stdout)['status']=='needs_configuration'
    assert (files(repo),files(worker))==before
