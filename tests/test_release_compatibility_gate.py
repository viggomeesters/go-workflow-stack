"""Run the release compatibility phase against real source and a regression."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COMPATIBILITY_GATE_TIMEOUT_SECONDS = 600


def run_gate(tmp_path, *, regress=False):
    repo = tmp_path / 'source'
    repo.mkdir()
    for name in ('go_workflow', 'cli', 'schemas', 'fixtures', 'tests', 'scripts'):
        shutil.copytree(ROOT / name, repo / name)
    for name in ('AGENTS.md', 'release-pairings.json', 'pyproject.toml'):
        shutil.copy2(ROOT / name, repo / name)
    contract = ROOT / '.go'
    fixture_contract = repo / '.go'
    fixture_contract.mkdir()
    for name in ('project.json', 'vision.json', 'hierarchy.json',
                 'architecture-principles.json', 'repository-map.json'):
        shutil.copy2(contract / name, fixture_contract / name)
    for name in ('architecture', 'decisions', 'tasks'):
        shutil.copytree(contract / name, fixture_contract / name)
    for name in ('evidence', 'runs'):
        (fixture_contract / name).mkdir()
        shutil.copy2(contract / name / 'events.jsonl', fixture_contract / name / 'events.jsonl')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null',
                    '-c', 'user.name=go-workflow', '-c', 'user.email=go-workflow@local.invalid',
                    'commit', '-qm', 'Compatibility fixture'], check=True)
    if regress:
        path = repo / 'go_workflow/cli.py'
        source = path.read_text()
        strict = 'if "execution_contract" in data and "dependencies" in data:'
        assert strict in source
        path.write_text(source.replace(strict, 'if "dependencies" in data:', 1))
    return subprocess.run(['bash', str(repo / 'scripts/check-compatibility.sh')],
                          cwd=tmp_path, text=True, capture_output=True,
                          env={**os.environ, 'PYTHON': sys.executable,
                               'PYTHONDONTWRITEBYTECODE': '1'},
                          timeout=COMPATIBILITY_GATE_TIMEOUT_SECONDS)


def test_compatibility_gate_has_finite_full_suite_budget(tmp_path, monkeypatch):
    original = subprocess.run
    observed = []

    def capture(command, *args, **kwargs):
        if command[:2] == ['bash', str(tmp_path / 'source' / 'scripts/check-compatibility.sh')]:
            observed.append(kwargs['timeout'])
            return subprocess.CompletedProcess(command, 0, 'compatibility release gate: passed\n', '')
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', capture)
    assert run_gate(tmp_path).returncode == 0
    assert observed == [COMPATIBILITY_GATE_TIMEOUT_SECONDS]
    assert 300 < COMPATIBILITY_GATE_TIMEOUT_SECONDS <= 600


def test_compatibility_gate_accepts_supported_history(tmp_path):
    result = run_gate(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'compatibility release gate: passed' in result.stdout


def test_reintroduced_legacy_bug_blocks_compatibility_gate(tmp_path):
    result = run_gate(tmp_path, regress=True)
    assert result.returncode != 0
    assert 'dependency must be an object' in result.stdout
    assert 'compatibility release gate: passed' not in result.stdout
