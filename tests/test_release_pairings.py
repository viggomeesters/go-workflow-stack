"""Immutable historical baselines and current starter metadata must agree."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest
from test_abc_worktrees import ROOT, CLI, git


def manifest(commit='a'*40):
    return {'schema':'go-workflow.release-pairings.v1','current_stack_ref':'v1.0.0',
        'template_repository':'https://github.com/viggomeesters/go-project-template.git',
        'pairings':[{'stack_ref':'v1.0.0','template_commit':commit,'template_ref':'v2.0.0','note':'Verified fixture baseline'}]}


def init(repo):
    subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
    git(repo,'config','user.name','Fixture');git(repo,'config','user.email','fixture@example.com')


def fixture(tmp_path):
    template=tmp_path/'template';init(template)
    (template/'.go').mkdir();(template/'.go/project.json').write_text(json.dumps({'stack_ref':'v1.0.0','required_stack_version':'1.0.0'}))
    (template/'README.md').write_text('# Fixture\n')
    git(template,'add','.');git(template,'commit','-qm','Template fixture');git(template,'tag','-a','v2.0.0','-m','Template')
    data=manifest(git(template,'rev-parse','HEAD'))
    stack=tmp_path/'stack';init(stack);(stack/'go_workflow').mkdir()
    (stack/'go_workflow/constants.py').write_text('STACK_VERSION = "1.0.0"\n')
    git(stack,'add','.');git(stack,'commit','-qm','Stack fixture');git(stack,'tag','-a','v1.0.0','-m','Stack')
    return template,stack,data


@pytest.mark.parametrize('mutation', ['missing-current','duplicate','bad-sha','bad-ref','bad-repo','unknown','unpaired-current'])
def test_invalid_manifest_fails_closed(tmp_path, mutation):
    from go_workflow.release_pairings import load_manifest, PairingError
    data=manifest()
    if mutation=='missing-current':data['pairings']=[]
    elif mutation=='duplicate':data['pairings'].append(deepcopy(data['pairings'][0]))
    elif mutation=='bad-sha':data['pairings'][0]['template_commit']='main'
    elif mutation=='bad-ref':data['pairings'][0]['stack_ref']='main'
    elif mutation=='bad-repo':data['template_repository']='https://unrelated.example/repo.git'
    elif mutation=='unknown':data['allow_push']=True
    else:data['pairings'][0].update(template_commit=None,template_ref=None)
    path=tmp_path/'manifest.json';path.write_text(json.dumps(data))
    with pytest.raises(PairingError):load_manifest(path)


def test_missing_pair_and_contradictory_current_ref_are_rejected(tmp_path):
    from go_workflow.release_pairings import load_manifest, pairing_for, PairingError
    path=tmp_path/'manifest.json';path.write_text(json.dumps(manifest()))
    with pytest.raises(PairingError,match='current'):load_manifest(path,expected_ref='v9.0.0')
    with pytest.raises(PairingError,match='missing'):pairing_for(load_manifest(path),'v9.0.0')


def test_baseline_checks_commit_tag_and_clean_checkout(tmp_path):
    from go_workflow.release_pairings import verify_baseline, PairingError
    template,_,data=fixture(tmp_path)
    assert verify_baseline(data,'v1.0.0',template)['verified']
    (template/'README.md').write_text('Changed')
    with pytest.raises(PairingError,match='clean'):verify_baseline(data,'v1.0.0',template)
    git(template,'add','.');git(template,'commit','-qm','Changed baseline')
    with pytest.raises(PairingError,match='commit'):verify_baseline(data,'v1.0.0',template)


def test_current_template_metadata_detects_pin_and_document_drift(tmp_path):
    from go_workflow.release_pairings import current_template, render_metadata, PairingError
    template,stack,data=fixture(tmp_path)
    metadata=current_template(data,template,stack,check_docs=False)
    block=render_metadata(metadata);(template/'README.md').write_text('# Fixture\n\n'+block+'\n')
    assert current_template(data,template,stack)['stack_commit']==git(stack,'rev-parse','HEAD')
    assert render_metadata(metadata)==block
    (template/'README.md').write_text(block.replace(metadata['stack_commit'],'b'*40))
    with pytest.raises(PairingError,match='README'):current_template(data,template,stack)
    path=template/'.go/project.json';project=json.loads(path.read_text());project['required_stack_version']='1.0.1';path.write_text(json.dumps(project))
    with pytest.raises(PairingError,match='pin'):current_template(data,template,stack,check_docs=False)


def test_historical_pairings_preserve_the_previous_immutable_commits():
    from go_workflow.release_pairings import load_manifest, pairing_for
    from go_workflow.constants import STACK_REF
    data=load_manifest(expected_ref=STACK_REF)
    expected={'v0.3.8':'3956fc92f9e99520756d10f08373635182f22d67',
        'v0.3.10':'d4a09d972451472180d45ef2a48c920ad91c496e',
        'v0.3.11':'6fb460a3ae15e6ed04f5abc0461c1bfade364522',
        'v0.3.12':'158b602ca5d4630895bacd7061b3fa8aca42398f',
        'v0.3.13':'ab89ae489fd88e464c052c076e4c6f48a06d5b2b'}
    expected.update({f'v0.3.{n}':'490dd50671dd740e5902ad17ed475258bb7c939b' for n in range(14,27)})
    expected.update({f'v0.3.{n}':'06678be4fdc95dc671d6adbc1a50b3f073d66b5c' for n in range(27,32)})
    for ref,commit in expected.items():assert pairing_for(data,ref)['template_commit']==commit
    import jsonschema
    jsonschema.validate(data,json.loads((ROOT/'schemas/release-pairings.schema.json').read_text()))
    inner=(ROOT/'scripts/release-check-inner.sh').read_text()
    assert 'release_pairings' in inner and '0.3.8:https://*)' not in inner


def test_cli_inspects_pairing_without_writes(tmp_path):
    from go_workflow.constants import STACK_REF
    result=subprocess.run([sys.executable,str(CLI),'pairing','inspect','--stack-ref',STACK_REF,'--json'],cwd=tmp_path,text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['stack_ref']==STACK_REF
    assert not list(tmp_path.iterdir())


def test_duplicate_json_keys_cannot_hide_a_conflicting_pair(tmp_path):
    from go_workflow.release_pairings import load_manifest, PairingError
    path=tmp_path/'pairs.json';path.write_text('{"current_stack_ref":"v1.0.0","current_stack_ref":"v2.0.0"}')
    with pytest.raises(PairingError,match='Duplicate'):load_manifest(path)


@pytest.mark.skipif(sys.platform!='linux',reason='Release launcher requires Linux memfd')
def test_release_gate_rejects_dirty_pairing_code_before_import(tmp_path):
    import shutil
    from go_workflow.constants import STACK_VERSION, STACK_REF
    repo=tmp_path/'release';shutil.copytree(ROOT,repo,ignore=shutil.ignore_patterns('.git','__pycache__','.pytest_cache'))
    init(repo);git(repo,'add','.');git(repo,'commit','-qm','Release fixture');git(repo,'tag','-a',STACK_REF,'-m','Release')
    marker=tmp_path/'injected-code-ran'
    (repo/'go_workflow/release_pairings.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').touch()\nraise RuntimeError("injected")\n')
    result=subprocess.run([str(repo/'scripts/release-check.sh'),STACK_VERSION],cwd=repo,text=True,capture_output=True)
    assert result.returncode!=0 and 'clean' in result.stderr,result.stdout+result.stderr
    assert not marker.exists()
