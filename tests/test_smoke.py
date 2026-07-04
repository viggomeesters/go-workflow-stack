import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def template_repo() -> Path:
    sibling = ROOT.parent / "go-project-template"
    if (sibling / ".go").is_dir():
        return sibling
    return ROOT / "fixtures" / "minimal"


def test_template_validates():
    result = subprocess.run([sys.executable, str(ROOT / "cli" / "go.py"), "validate", str(template_repo())], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr + result.stdout


def test_bundle_export_import_smoke(tmp_path: Path):
    bundle = tmp_path / "bundle.json"
    source = template_repo()
    export_result = subprocess.run([
        sys.executable,
        str(ROOT / "cli" / "go.py"),
        "bundle",
        "export",
        str(source),
        "--output",
        str(bundle),
    ], text=True, capture_output=True)
    assert export_result.returncode == 0, export_result.stderr + export_result.stdout
    second_bundle = tmp_path / "bundle-second.json"
    second_export = subprocess.run([
        sys.executable,
        str(ROOT / "cli" / "go.py"),
        "bundle",
        "export",
        str(source),
        "--output",
        str(second_bundle),
    ], text=True, capture_output=True)
    assert second_export.returncode == 0, second_export.stderr + second_export.stdout
    import json
    first_id = json.loads(bundle.read_text())["bundle_id"]
    second_id = json.loads(second_bundle.read_text())["bundle_id"]
    assert first_id != second_id
    target = tmp_path / "target"
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    adopt_result = subprocess.run([
        sys.executable,
        str(ROOT / "cli" / "go.py"),
        "adopt",
        str(target),
        "--project-id",
        "target",
        "--name",
        "Target",
        "--feature-group",
        "workflow|Workflow",
        "--feature",
        "workflow|repo-local|Repo-local",
        "--verification",
        "git diff --check",
    ], text=True, capture_output=True)
    assert adopt_result.returncode == 0, adopt_result.stderr + adopt_result.stdout
    dry_run = subprocess.run([
        sys.executable,
        str(ROOT / "cli" / "go.py"),
        "bundle",
        "import",
        str(target),
        str(bundle),
    ], text=True, capture_output=True)
    assert dry_run.returncode == 0, dry_run.stderr + dry_run.stdout
    assert '"mode": "dry_run"' in dry_run.stdout
    write_result = subprocess.run([
        sys.executable,
        str(ROOT / "cli" / "go.py"),
        "bundle",
        "import",
        str(target),
        str(bundle),
        "--write",
        "--agent",
        "pytest",
        "--task-id",
        "import-smoke",
    ], text=True, capture_output=True)
    assert write_result.returncode == 0, write_result.stderr + write_result.stdout
    assert list((target / ".go" / "imports").glob("*.json"))
    validate_result = subprocess.run([sys.executable, str(ROOT / "cli" / "go.py"), "validate", str(target)], text=True, capture_output=True)
    assert validate_result.returncode == 0, validate_result.stderr + validate_result.stdout
