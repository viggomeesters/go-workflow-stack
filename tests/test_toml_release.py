"""TOML preparation preserves bytes and uses the existing real publication path."""
import json
import subprocess
import tomllib

import pytest
from test_abc_worktrees import setup_repo, git, create, ROOT
from test_abc_release import prepare, proofs


@pytest.fixture(autouse=True)
def neutral_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def fixture(tmp_path, text, key='project.version'):
    repo, _ = setup_repo(tmp_path)
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    git(repo, 'remote', 'add', 'origin', str(remote))
    source = repo / '.go/tasks/active/task-schema-smoke.json'
    task = json.loads(source.read_text())
    task['execution_contract']['release']['profile'] = 'fixture'
    task['scope']['modify'] = ['app.txt', 'pyproject.toml', 'CHANGELOG.md']
    task.update(execution_mode='agent', shareable_delivery='none', outcome_tracking_version=1,
                acceptance=['TOML version delivered'], requested_outcomes=[{'id':'R1','text':'TOML version delivered','source':'fixture','status':'pending','evidence':[]}],
                verification=["python3 -c \"import tomllib; from pathlib import Path; assert tomllib.loads(Path('pyproject.toml').read_text())['project']['version'] == '1.2.0'; assert Path('app.txt').read_text() == 'delivered'\""])
    source.write_text(json.dumps(task))
    p = repo / '.go/project.json'; project = json.loads(p.read_text())
    project['release_profiles'] = {'fixture': {'provider':'git-tag','remote':'origin','branch':'main',
        'publication':{'version':{'path':'pyproject.toml','format':'toml','key':key},'bump':'minor','tag_prefix':'v','changelog':'CHANGELOG.md'}}}
    p.write_text(json.dumps(project)); (repo / 'pyproject.toml').write_bytes(text.encode())
    (repo / 'CHANGELOG.md').write_text('# Changes\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'TOML release fixture')
    task['claim']['base_commit'] = git(repo, 'rev-parse', 'HEAD'); source.write_text(json.dumps(task))
    git(repo, 'add', '.go'); git(repo, 'commit', '-qm', 'Claim provenance')
    git(repo, 'tag', '-a', 'v1.1.0', '-m', 'baseline'); git(repo, 'push', 'origin', 'main', 'refs/tags/v1.1.0')
    worker = tmp_path / 'worker'; result = create(repo, git(repo, 'rev-parse', 'HEAD'), worker)
    assert result.returncode == 0, result.stderr
    (worker / 'app.txt').write_text('delivered')
    return repo, worker, source, project


@pytest.mark.parametrize('text', [
    '# café 1.1.0\n[project]\nname = "fixture"\nversion = "1.1.0" # keep\n[tool.example]\nversion = "1.1.0"\n',
    "[project]\r\nversion = '1.1.0' # keep CRLF\r\nname = 'fixture'\r\n",
    'project = {name="fixture", version="1.1.0"}\n',
    'project.version = "1.1.0"\n',
])
def test_prepare_preserves_all_other_bytes_and_resumes(tmp_path, text):
    repo, worker, _, project = fixture(tmp_path, text)
    result = prepare(repo)
    assert result['version'] == '1.2.0'
    after = (worker / 'pyproject.toml').read_bytes()
    # The selected literal changes exactly once, including when another key has the same version.
    expected = text.replace('version = "1.1.0"', 'version = "1.2.0"', 1) if 'version = "1.1.0"' in text else text.replace('1.1.0', '1.2.0', 1)
    assert after == expected.encode()
    assert prepare(repo)['version'] == '1.2.0'
    assert (worker / 'pyproject.toml').read_bytes() == after
    import jsonschema
    jsonschema.validate(project, json.loads((ROOT / 'schemas/project.schema.json').read_text()))
    settings = {'schema':'go-workflow.lifecycle-settings.v1','execution_defaults':json.loads((repo/'.go/tasks/active/task-schema-smoke.json').read_text())['execution_contract'], 'release_profiles':project['release_profiles'], 'default_verification':['git diff --check']}
    jsonschema.validate(settings, json.loads((ROOT/'schemas/lifecycle-settings.schema.json').read_text()))


@pytest.mark.parametrize('text', [
    '[project]\nname="fixture"\n',
    '[project]\ndynamic=["version"]\n',
    '[project]\ndynamic=["version"]\nversion="1.1.0"\n',
    '[project]\nversion=12\n',
    '[project]\nversion="1.1.0.dev1"\n',
    '[project]\nversion="1.1.0"\nversion="1.1.0"\n',
    '[project]\nversion="\\u0031.1.0"\n',
])
def test_invalid_toml_never_reserves_or_changes_files(tmp_path, text):
    from go_workflow.release import PublicationError
    repo, worker, _, _ = fixture(tmp_path, text)
    before = (worker/'pyproject.toml').read_bytes(), (worker/'CHANGELOG.md').read_bytes()
    with pytest.raises(PublicationError):
        prepare(repo)
    assert ((worker/'pyproject.toml').read_bytes(), (worker/'CHANGELOG.md').read_bytes()) == before
    assert not (repo/'.go/runs/task-schema-smoke/release-state.json').exists()
    assert not list((repo/'.go/runs/publication-reservations').glob('*.json'))


def test_toml_publication_recovers_lost_push_without_repeating_effect(tmp_path, monkeypatch):
    import go_workflow.release as publisher
    repo, worker, source, _ = fixture(tmp_path, '[project]\nversion="1.1.0"\n')
    prepare(repo); proofs(worker, source)
    original = publisher._write_command; pushes = []
    def lost(session, cwd, argv):
        value = original(session, cwd, argv)
        if argv[:2] == ['git', 'push']:
            pushes.append(argv)
            if len(pushes) == 1:
                raise publisher.PublicationError('lost push acknowledgement')
        return value
    monkeypatch.setattr(publisher, '_write_command', lost)
    with pytest.raises(publisher.PublicationError, match='lost push'):
        publisher.publish_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    result = publisher.publish_release(repo, 'task-schema-smoke', 'owner', 'run-1')
    assert result['status'] == 'published' and len(pushes) == 1
    assert tomllib.loads(git(repo, 'show', 'v1.2.0:pyproject.toml'))['project']['version'] == '1.2.0'
    assert json.loads((repo/'.go/tasks/done/task-schema-smoke.json').read_text())['review_status'] == 'approved'


def test_poetry_version_key_uses_explicit_path(tmp_path):
    text = '[tool.poetry]\nname="fixture"\nversion="1.1.0" # preserve\n'
    repo, worker, _, _ = fixture(tmp_path, text, 'tool.poetry.version')
    assert prepare(repo)['version'] == '1.2.0'
    assert (worker/'pyproject.toml').read_bytes() == text.replace('1.1.0','1.2.0').encode()


def test_json_and_text_version_sources_remain_supported():
    from go_workflow.release import _version_change
    assert _version_change('1.1.0\n', {'format':'text'}) == '1.1.0'
    assert _version_change('1.1.0\n', {'format':'text'}, '1.2.0') == '1.2.0\n'
    spec = {'format':'json','key':'package.version'}
    text = '{"package":{"version":"1.1.0"}, "keep":true}'
    assert _version_change(text, spec) == '1.1.0'
    assert json.loads(_version_change(text,spec,'1.2.0')) == {'package':{'version':'1.2.0'},'keep':True}
