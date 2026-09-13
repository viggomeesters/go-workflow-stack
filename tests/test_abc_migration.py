"""Lifecycle adoption preserves historical state and never grants execution authority."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from test_abc_contracts import fixture,write,profile,ROOT,run


@pytest.fixture(autouse=True)
def neutral_directory(tmp_path,monkeypatch):monkeypatch.chdir(tmp_path)


def settings():
    contract=profile();contract['task_kind']='smoke';contract['release']={'mode':'none','reason':'Local fixture only'}
    contract['workspace']={'mode':'task_worktree','control_state':'repo_local_single_writer','base_branch':'main'}
    return {'schema':'go-workflow.lifecycle-settings.v1','execution_defaults':contract,'default_verification':['git diff --check']}


def tree(repo):return {str(p.relative_to(repo)):p.read_bytes() for p in (repo/'.go').rglob('*') if p.is_file()}


def test_preview_is_nonmutating_and_reports_unconfigured_records(tmp_path):
    from go_workflow.migrations import plan_lifecycle_adoption
    repo=fixture(tmp_path);before=tree(repo)
    plan,docs=plan_lifecycle_adoption(repo,None)
    assert plan['tasks'][0]['disposition']=='needs_configuration' and not docs
    assert tree(repo)==before
    plan,docs=plan_lifecycle_adoption(repo,settings())
    assert plan['write_required'] and '.go/project.json' in docs and tree(repo)==before


def test_apply_rollback_idempotent_preserves_exact_historical_bytes(tmp_path):
    from go_workflow.migrations import adopt_lifecycle,rollback_lifecycle
    repo=fixture(tmp_path);source=repo/'.go/tasks/open/task-schema-smoke.json'
    history=json.loads(source.read_text());history.update(id='history',status='done',evidence=['historical evidence'])
    done=repo/'.go/tasks/done/history.json';write(done,history)
    hierarchy=repo/'.go/hierarchy.json';h=json.loads(hierarchy.read_text());h['epics'][0]['tasks'].append('history');write(hierarchy,h)
    before=tree(repo);result=adopt_lifecycle(repo,settings(),apply=True)
    assert result['status']=='applied' and done.read_bytes()==before['.go/tasks/done/history.json']
    task=json.loads(source.read_text());assert task['execution_contract']==settings()['execution_defaults']
    assert task['claim'].get('agent') is None and task['status']=='open' and 'completion_evidence' not in task
    assert adopt_lifecycle(repo,settings(),apply=True)['status']=='noop'
    rollback_lifecycle(repo,result['journal'],apply=True)
    assert all((repo/path).read_bytes()==data for path,data in before.items())
    assert rollback_lifecycle(repo,result['journal'],apply=True)['status']=='rolled_back'


def test_existing_override_survives_adoption_and_no_authority_keys_are_accepted(tmp_path):
    from go_workflow.migrations import adopt_lifecycle,MigrationError
    repo=fixture(tmp_path);p=repo/'.go/tasks/open/task-schema-smoke.json';d=json.loads(p.read_text())
    d['execution_contract']=settings()['execution_defaults'];d['execution_contract']['model']['effort']='medium';write(p,d);before=p.read_bytes()
    adopt_lifecycle(repo,settings(),apply=True);assert p.read_bytes()==before
    with pytest.raises(MigrationError):adopt_lifecycle(repo,{**settings(),'allow_push':True},apply=True)


def test_interrupted_apply_blocks_claim_and_resumes_exact_journal(tmp_path,monkeypatch):
    import go_workflow.migrations as migration
    import go_workflow.cli as api
    repo=fixture(tmp_path);original=migration.atomic_write_text;calls=[]
    def interrupted(path,text):
        if path.name=='project.json' and not calls:
            calls.append(path);original(path,text);raise KeyboardInterrupt('controller death')
        return original(path,text)
    monkeypatch.setattr(migration,'atomic_write_text',interrupted)
    with pytest.raises(KeyboardInterrupt):migration.adopt_lifecycle(repo,settings(),apply=True)
    assert any('migration' in error.lower() for error in api.validate_repo(repo))
    result=run(repo,'claim','task-schema-smoke','--repo',repo,'--agent','fixture','--allow-dirty')
    assert result.returncode!=0 and 'migration' in result.stderr.lower()
    journal=next((repo/'.go/migrations').glob('*.json'))
    monkeypatch.setattr(migration,'atomic_write_text',original)
    assert migration.resume_lifecycle(repo,str(journal.relative_to(repo)),apply=True)['status']=='applied'
    assert not api.validate_repo(repo)


def test_rollback_refuses_advanced_task_without_overwriting_it(tmp_path):
    from go_workflow.migrations import adopt_lifecycle,rollback_lifecycle,MigrationError
    repo=fixture(tmp_path);result=adopt_lifecycle(repo,settings(),apply=True)
    p=repo/'.go/tasks/open/task-schema-smoke.json';d=json.loads(p.read_text());d['description']='User updated the task';write(p,d);before=tree(repo)
    with pytest.raises(MigrationError,match='changed|drift'):rollback_lifecycle(repo,result['journal'],apply=True)
    assert tree(repo)==before


def test_export_import_preserves_full_contract_and_intake_provenance(tmp_path):
    from go_workflow.migrations import adopt_lifecycle
    import go_workflow.cli as api
    repo=fixture(tmp_path);adopt_lifecycle(repo,settings(),apply=True)
    task=api.create_task_from_intent(repo,'Deliver an observable result',source_ref='user:direction')
    source=json.loads((repo/task['path']).read_text())
    bundle=api.build_export_bundle(repo,include_done=True)
    assert bundle['lifecycle_contracts']['project']['execution_defaults']==settings()['execution_defaults']
    assert source in bundle['lifecycle_contracts']['tasks']
    exported=tmp_path/'bundle.json';write(exported,bundle)
    target=fixture(tmp_path,'target');result=run(target,'bundle','import',target,exported,'--write','--agent','fixture','--task-id','import-proof')
    assert result.returncode==0,result.stderr
    imported=json.loads(next((target/'.go/imports').glob('*.json')).read_text())
    assert imported['bundle']['lifecycle_contracts']==bundle['lifecycle_contracts']
    assert not (target/'.go/tasks/open'/f"{source['id']}.json").exists()


@pytest.mark.parametrize('command',['init','adopt','spike'])
def test_new_scaffold_uses_explicit_settings_without_claim(tmp_path,command):
    repo=tmp_path/'new';config=tmp_path/'settings.json';write(config,settings())
    args=[command,repo,'--lifecycle-settings',config]
    if command=='spike':args+=['--task','first|First task','--skip-repo-complete']
    result=run(tmp_path,*args);assert result.returncode==0,result.stderr
    assert json.loads((repo/'.go/project.json').read_text())['execution_defaults']==settings()['execution_defaults']
    for path in (repo/'.go/tasks/open').glob('*.json'):
        task=json.loads(path.read_text());assert task['execution_contract']==settings()['execution_defaults']
        assert task['requested_outcomes'] and not task['claim'].get('agent')
    assert not list((repo/'.go/tasks/active').glob('*.json'))


def test_all_intake_routes_keep_profiles_pending_requirements_and_phase_scope(tmp_path):
    import go_workflow.cli as api
    from go_workflow.migrations import adopt_lifecycle
    repo=fixture(tmp_path);config=settings()
    phase={'schema':'go-workflow.phase-contract.v1','id':'verify','inputs':['candidate'], 'outputs':['verdict'],
           'required_evidence':['executed command output'],'stop_conditions':['failed check'], 'handoff':['raw findings'],
           'scope':{'read':['app.py'],'modify':[]},'required_outcomes':['all requirements verified']}
    config['phase_profiles']={'local':[phase]};config['execution_defaults']['phase_profile']='local'
    adopt_lifecycle(repo,config,apply=True)
    result=run(repo,'task','create',repo,'--id','direct','--summary','Direct outcome','--acceptance','Observable result','--verification','git diff --check','--epic','workflow-contract')
    assert result.returncode==0,result.stderr
    direct=json.loads((repo/'.go/tasks/open/direct.json').read_text())
    follow=api.create_followup_task(repo,direct,['A bounded finding'],'fixture')
    unit={'id':'recommended','summary':'Recommended outcome','scope':{'read':['app.py'],'modify':['app.py']},'acceptance':['Observable recommended result'],'verification':['git diff --check']}
    brief={'schema':'go-workflow.execution-brief.v1','destination':'Result','problem':'Problem','chosen_approach':'Chosen direction','non_goals':[],
           'source':{'recommendation':'Chosen direction','sha256':hashlib.sha256(b'Chosen direction').hexdigest(),'source_ref':'user:direction'},'work_units':[unit]}
    source=tmp_path/'brief.json';write(source,brief)
    assert run(repo,'recommendation','create',repo,'--brief',source,'--authority','execute','--authority-source','fixture').returncode==0
    result=run(repo,'go',repo,'--write','--json');assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['recommendation_promotion']['status']=='applied'
    for name in ['direct',follow['id'],'recommended']:
        task=json.loads((repo/'.go/tasks/open'/f'{name}.json').read_text())
        assert task['execution_contract']==config['execution_defaults'] and task['verification']==['git diff --check']
        assert all(value['status']=='pending' and not value['evidence'] for value in task['requested_outcomes'])
        assert 'completion_evidence' not in task and not task['claim'].get('agent')
    bundle=api.build_export_bundle(repo);assert bundle['lifecycle_contracts']['project']['phase_profiles']=={'local':[phase]}


def test_rollback_after_new_task_refuses_to_remove_its_project_defaults(tmp_path):
    from go_workflow.migrations import adopt_lifecycle,rollback_lifecycle,MigrationError
    from go_workflow.cli import create_task_from_intent
    repo=fixture(tmp_path);result=adopt_lifecycle(repo,settings(),apply=True)
    create_task_from_intent(repo,'New task after adoption');before=tree(repo)
    with pytest.raises(MigrationError,match='inventory'):rollback_lifecycle(repo,result['journal'],apply=True)
    assert tree(repo)==before


def test_invalid_settings_and_active_task_fail_without_policy_writes(tmp_path):
    from go_workflow.migrations import adopt_lifecycle,MigrationError
    repo=fixture(tmp_path);before=tree(repo)
    for invalid in [{**settings(),'allow_deploy':True},{**settings(),'execution_defaults':[]},{**settings(),'execution_defaults':profile()}]:
        with pytest.raises(MigrationError):adopt_lifecycle(repo,invalid,apply=True)
        assert tree(repo)==before
    p=repo/'.go/tasks/open/task-schema-smoke.json';d=json.loads(p.read_text());d['status']='active';write(repo/'.go/tasks/active'/p.name,d);p.unlink();before=tree(repo)
    with pytest.raises(MigrationError,match='quiescent'):adopt_lifecycle(repo,settings(),apply=True)
    assert tree(repo)==before


def test_pin_update_is_not_policy_adoption_and_stale_rollback_preserves_settings(tmp_path):
    from test_smoke import make_tagged_stack_repo
    from go_workflow.stack_update import plan_stack_update,apply_stack_update,rollback_stack_update,latest_stack_ref,StackUpdateError
    from go_workflow.migrations import adopt_lifecycle
    repo=fixture(tmp_path);stack=make_tagged_stack_repo(tmp_path/'stack');target=latest_stack_ref(stack)
    plan=plan_stack_update(repo,stack,target);before=tree(repo)
    assert not plan['lifecycle_policy_migrated'] and tree(repo)==before
    result=apply_stack_update(repo,plan)
    assert 'execution_defaults' not in json.loads((repo/'.go/project.json').read_text())
    adopt_lifecycle(repo,settings(),apply=True);before=tree(repo)
    with pytest.raises(StackUpdateError,match='changed'):rollback_stack_update(repo,result['rollback_record'])
    assert tree(repo)==before
    with pytest.raises(StackUpdateError,match='changed'):apply_stack_update(repo,plan)
    assert tree(repo)==before


def test_rollback_interruption_is_recoverable_without_reapplying_policy(tmp_path,monkeypatch):
    import go_workflow.migrations as migration
    import go_workflow.cli as api
    repo=fixture(tmp_path);before=tree(repo);result=migration.adopt_lifecycle(repo,settings(),apply=True)
    original=migration.atomic_write_text;calls=[]
    def interrupted(path,text):
        original(path,text)
        if not calls:calls.append(path);raise KeyboardInterrupt('rollback interrupted')
    monkeypatch.setattr(migration,'atomic_write_text',interrupted)
    with pytest.raises(KeyboardInterrupt):migration.rollback_lifecycle(repo,result['journal'],apply=True)
    assert api.validate_repo(repo)
    with pytest.raises(migration.MigrationError):migration.resume_lifecycle(repo,result['journal'],apply=True)
    monkeypatch.setattr(migration,'atomic_write_text',original)
    migration.rollback_lifecycle(repo,result['journal'],apply=True)
    assert all((repo/path).read_bytes()==data for path,data in before.items()) and not api.validate_repo(repo)


def test_lifecycle_schemas_and_review_bundle_tamper_detection(tmp_path):
    import jsonschema
    import go_workflow.cli as api
    from go_workflow.migrations import adopt_lifecycle
    repo=fixture(tmp_path);result=adopt_lifecycle(repo,settings(),apply=True)
    bundle=api.build_export_bundle(repo,include_done=True)
    examples={'lifecycle-settings.schema.json':settings(),'lifecycle-adoption.schema.json':result,
        'lifecycle-migration-journal.schema.json':json.loads((repo/result['journal']).read_text()),
        'lifecycle-review-bundle.schema.json':bundle['lifecycle_contracts']}
    for name,value in examples.items():jsonschema.validate(value,json.loads((ROOT/'schemas'/name).read_text()))
    api.validate_export_bundle(bundle)
    altered=deepcopy(bundle);altered['lifecycle_contracts']['tasks'][0]['execution_contract']['model']['effort']='low'
    with pytest.raises(api.RepoLocalError,match='digest'):api.validate_export_bundle(altered)
    altered=deepcopy(bundle);altered['lifecycle_contracts']['tasks'][0]['id']=[]
    value=altered['lifecycle_contracts'];value['sha256']=hashlib.sha256(json.dumps({k:v for k,v in value.items() if k!='sha256'},sort_keys=True,separators=(',', ':')).encode()).hexdigest()
    with pytest.raises(api.RepoLocalError,match='record'):api.validate_export_bundle(altered)


def test_malformed_journal_or_escaped_target_blocks_recovery_and_claim(tmp_path):
    import go_workflow.cli as api
    from go_workflow.migrations import adopt_lifecycle,rollback_lifecycle,MigrationError
    repo=fixture(tmp_path);result=adopt_lifecycle(repo,settings(),apply=True)
    path=repo/result['journal'];journal=json.loads(path.read_text());journal['schema']='typo';write(path,journal)
    assert api.validate_repo(repo)
    journal['schema']='go-workflow.lifecycle-migration-journal.v1';journal['status']={};write(path,journal)
    assert api.validate_repo(repo)
    with pytest.raises(MigrationError):rollback_lifecycle(repo,result['journal'],apply=True)
    with pytest.raises(MigrationError):rollback_lifecycle(repo,'../foreign.json',apply=True)


def test_adoption_preserves_named_profiles_and_rejects_redefining_history(tmp_path):
    from go_workflow.migrations import adopt_lifecycle,MigrationError
    repo=fixture(tmp_path);path=repo/'.go/project.json';project=json.loads(path.read_text())
    old={'provider':'git-tag','remote':'origin','branch':'main'};project['release_profiles']={'old':old};write(path,project)
    config={**settings(),'release_profiles':{'new':{**old,'branch':'release'}}}
    adopt_lifecycle(repo,config,apply=True)
    assert json.loads(path.read_text())['release_profiles']=={'old':old,'new':{**old,'branch':'release'}}
    before=tree(repo)
    with pytest.raises(MigrationError,match='new profile name'):adopt_lifecycle(repo,{**settings(),'release_profiles':{'old':{**old,'branch':'other'}}},apply=True)
    assert tree(repo)==before


def test_direct_cli_script_retains_readonly_validation(tmp_path):
    repo=fixture(tmp_path)
    result=subprocess.run([sys.executable,str(ROOT/'go_workflow/cli.py'),'validate',str(repo)],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_product_defaults_keep_full_publication_deployment_and_model_contract(tmp_path):
    import jsonschema
    from go_workflow.migrations import adopt_lifecycle
    repo=fixture(tmp_path);config=settings();config['execution_defaults']['task_kind']='product'
    config['execution_defaults']['release']={'mode':'required','profile':'app'}
    config['execution_defaults']['critic_model']={'id':'gpt-5.6-terra','effort':'high'}
    config['release_profiles']={'app':{'provider':'git-tag','remote':'origin','branch':'main',
        'publication':{'version':{'path':'VERSION','format':'text'},'bump':'minor','tag_prefix':'v','changelog':'CHANGELOG.md'},
        'deployment':{'mode':'required','target':'fixture-only','recovery_policy':'resume_only','required_env':['FIXTURE_DEPLOY_TOKEN'],
            'deploy':{'argv':['fixture-deploy','{idempotency_key}'],'idempotency':'required'},
            'observe':{'argv':['fixture-observe','{idempotency_key}'],'read_only':True}}}}
    jsonschema.validate(config,json.loads((ROOT/'schemas/lifecycle-settings.schema.json').read_text()))
    adopt_lifecycle(repo,config,apply=True)
    task=json.loads((repo/'.go/tasks/open/task-schema-smoke.json').read_text());project=json.loads((repo/'.go/project.json').read_text())
    assert task['execution_contract']==config['execution_defaults'] and project['release_profiles']==config['release_profiles']
    assert not task['claim'].get('agent') and 'authority' not in task and 'completion_evidence' not in task
    assert not list((repo/'.go/runs').glob('*/release-state.json'))
