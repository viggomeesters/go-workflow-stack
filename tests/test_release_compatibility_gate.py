"""Run the release compatibility phase against real source and a regression."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run_gate(tmp_path, *, regress=False):
    repo = tmp_path / 'source'
    repo.mkdir()
    for name in ('go_workflow', 'cli', 'schemas', 'fixtures', 'tests', 'scripts'):
        shutil.copytree(ROOT / name, repo / name)
    if regress:
        path = repo / 'go_workflow/cli.py'
        source = path.read_text()
        strict = 'if "execution_contract" in data and "dependencies" in data:'
        assert strict in source
        path.write_text(source.replace(strict, 'if "dependencies" in data:', 1))
    return subprocess.run(['bash', str(repo / 'scripts/check-compatibility.sh')],
                          cwd=tmp_path, text=True, capture_output=True,
                          env={**os.environ, 'PYTHON': sys.executable,
                               'PYTHONDONTWRITEBYTECODE': '1'}, timeout=120)


def test_compatibility_gate_accepts_supported_history(tmp_path):
    result = run_gate(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'compatibility release gate: passed' in result.stdout


def test_reintroduced_legacy_bug_blocks_compatibility_gate(tmp_path):
    result = run_gate(tmp_path, regress=True)
    assert result.returncode != 0
    assert 'dependency must be an object' in result.stdout
    assert 'compatibility release gate: passed' not in result.stdout
