"""Root AGENTS.md is the mandatory, safely managed gateway into `.go`."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli" / "go.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_go(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *(str(arg) for arg in args)],
        text=True,
        capture_output=True,
        cwd=ROOT.parent,
        check=False,
    )


def adopt(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = run_go("adopt", repo, "--project-id", "gateway", "--name", "Gateway")
    assert result.returncode == 0, result.stderr + result.stdout


def test_adopt_installs_exact_root_gateway_and_validation_requires_it(tmp_path: Path):
    from go_workflow.agents_gateway import GATEWAY_BLOCK

    repo = tmp_path / "repo"
    adopt(repo)

    agents = repo / "AGENTS.md"
    assert agents.read_text(encoding="utf-8") == GATEWAY_BLOCK
    agents.unlink()

    missing = run_go("validate", repo)
    assert missing.returncode == 1
    assert "root AGENTS.md is required whenever .go exists" in missing.stderr
    assert "agents sync" in missing.stderr

    (repo / "agents.md").write_text("wrong case\n", encoding="utf-8")
    wrong_case = run_go("validate", repo)
    assert wrong_case.returncode == 1
    assert "exact casing" in wrong_case.stderr


def test_gateway_sync_preserves_custom_instructions_and_is_idempotent(tmp_path: Path):
    from go_workflow.agents_gateway import GATEWAY_BLOCK

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".go").mkdir()
    custom = "# Local instructions\n\nNever publish customer data.\n"
    (repo / "AGENTS.md").write_text(custom, encoding="utf-8")

    dry_run = run_go("agents", "sync", repo, "--json")
    assert dry_run.returncode == 0, dry_run.stderr
    plan = json.loads(dry_run.stdout)
    assert plan["mode"] == "dry_run"
    assert plan["action"] == "append"
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == custom

    applied = run_go("agents", "sync", repo, "--apply", "--json")
    assert applied.returncode == 0, applied.stderr
    expected = custom + "\n" + GATEWAY_BLOCK
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == expected

    repeated = run_go("agents", "sync", repo, "--apply", "--json")
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["action"] == "none"
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == expected


def test_gateway_sync_replaces_only_its_bounded_block(tmp_path: Path):
    from go_workflow.agents_gateway import GATEWAY_BLOCK, GATEWAY_END, GATEWAY_START

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".go").mkdir()
    custom_before = "# Before\nKeep before.\n\n"
    custom_after = "\n# After\nKeep after.\n"
    stale = GATEWAY_START + "\nstale managed text\n" + GATEWAY_END
    (repo / "AGENTS.md").write_text(custom_before + stale + custom_after, encoding="utf-8")

    result = run_go("agents", "sync", repo, "--apply", "--json")

    assert result.returncode == 0, result.stderr
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == custom_before + GATEWAY_BLOCK.rstrip("\n") + custom_after


def test_gateway_sync_preserves_crlf_custom_bytes_and_file_mode(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".go").mkdir()
    agents = repo / "AGENTS.md"
    before = b"# Windows-local rules\r\n\r\nKeep these bytes.\r\n"
    agents.write_bytes(before)
    agents.chmod(0o640)

    result = run_go("agents", "sync", repo, "--apply", "--json")

    assert result.returncode == 0, result.stderr
    assert agents.read_bytes().startswith(before)
    assert agents.stat().st_mode & 0o777 == 0o640


def test_gateway_sync_refuses_ambiguous_markers_without_writing(tmp_path: Path):
    from go_workflow.agents_gateway import GATEWAY_START

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".go").mkdir()
    agents = repo / "AGENTS.md"
    before = "custom\n" + GATEWAY_START + "\nbroken\n"
    agents.write_text(before, encoding="utf-8")

    result = run_go("agents", "sync", repo, "--apply", "--json")

    assert result.returncode == 1
    assert "ambiguous managed gateway markers" in result.stderr
    assert agents.read_text(encoding="utf-8") == before


def test_migrate_repairs_gateway_without_touching_custom_text(tmp_path: Path):
    repo = tmp_path / "repo"
    adopt(repo)
    agents = repo / "AGENTS.md"
    custom = "# Project-specific\n\nKeep this obligation.\n"
    agents.write_text(custom, encoding="utf-8")

    planned = run_go("migrate", repo, "--json")
    assert planned.returncode == 0, planned.stderr
    assert any(change["path"] == "AGENTS.md" for change in json.loads(planned.stdout)["changes"])
    assert agents.read_text(encoding="utf-8") == custom

    applied = run_go("migrate", repo, "--apply", "--json")
    assert applied.returncode == 0, applied.stderr
    assert agents.read_text(encoding="utf-8").startswith(custom)
    assert run_go("validate", repo).returncode == 0


def test_stack_update_repairs_gateway_and_rollback_restores_original(tmp_path: Path):
    from go_workflow.stack_update import _apply_stack_update, rollback_stack_update

    repo = tmp_path / "repo"
    adopt(repo)
    agents = repo / "AGENTS.md"
    custom = "# Existing project rules\n"
    agents.write_text(custom, encoding="utf-8")
    project = json.loads((repo / ".go" / "project.json").read_text(encoding="utf-8"))
    plan = {
        "up_to_date": False,
        "repo": str(repo),
        "stack_repo": str(ROOT),
        "from_version": project["required_stack_version"],
        "from_ref": project["stack_ref"],
        "to_version": project["required_stack_version"],
        "to_ref": project["stack_ref"],
        "resolved_commit": "a" * 40,
        "before_project": project,
        "after_project": project,
        "compatibility": {"status": "passed", "errors": []},
        "changes": ["AGENTS.md:bounded-go-gateway"],
    }

    applied = _apply_stack_update(repo, plan)

    assert applied["mode"] == "applied"
    assert agents.read_text(encoding="utf-8").startswith(custom)
    rollback_stack_update(repo, applied["rollback_record"])
    assert agents.read_text(encoding="utf-8") == custom


def test_fixture_and_gateway_document_the_full_execution_handoff():
    from go_workflow.agents_gateway import GATEWAY_BLOCK

    required = (
        ".go/vision.json",
        ".go/architecture-principles.json",
        ".go/hierarchy.json",
        "selected task",
        "validate",
        "status",
        "router",
        "scope.modify",
        "evidence",
        "critic",
        "repair",
        "finish",
    )
    assert all(value in GATEWAY_BLOCK for value in required)
    assert (ROOT / "fixtures" / "minimal" / "AGENTS.md").read_text(encoding="utf-8") == GATEWAY_BLOCK


def test_wrong_case_is_repaired_without_losing_content_on_case_sensitive_filesystems(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".go").mkdir()
    wrong = repo / "agents.md"
    wrong.write_text("# Custom\n", encoding="utf-8")

    result = run_go("agents", "sync", repo, "--apply", "--json")

    assert result.returncode == 0, result.stderr
    assert "agents.md" not in {entry.name for entry in repo.iterdir()}
    assert "AGENTS.md" in {entry.name for entry in repo.iterdir()}
    assert (repo / "AGENTS.md").read_text(encoding="utf-8").startswith("# Custom\n")


@pytest.mark.parametrize("command", ["init", "adopt", "spike"])
def test_all_bootstrap_paths_install_gateway(tmp_path: Path, command: str):
    repo = tmp_path / command
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    args: list[object] = [command, repo]
    if command in {"adopt", "spike"}:
        args += ["--project-id", command, "--name", command.title()]
    if command == "spike":
        args += ["--skip-repo-complete"]

    result = run_go(*args)

    assert result.returncode == 0, result.stderr + result.stdout
    assert (repo / "AGENTS.md").is_file()
    assert run_go("validate", repo).returncode == 0


def test_released_gateway_is_readable_only_at_older_matching_pin(tmp_path):
    from go_workflow.agents_gateway import LEGACY_GATEWAY_BLOCK, validate_agents_gateway, plan_agents_gateway
    import json
    (tmp_path/'.go').mkdir()
    project=tmp_path/'.go/project.json'
    project.write_text(json.dumps({'required_stack_version':'0.3.47','stack_ref':'v0.3.47'}))
    (tmp_path/'AGENTS.md').write_text(LEGACY_GATEWAY_BLOCK)
    assert validate_agents_gateway(tmp_path)==[]
    assert plan_agents_gateway(tmp_path)['action']!='none'
    project.write_text(json.dumps({'required_stack_version':'0.3.48','stack_ref':'v0.3.48'}))
    assert validate_agents_gateway(tmp_path)
    project.write_text(json.dumps({'required_stack_version':'0.3.47','stack_ref':'v0.3.47'}))
    (tmp_path/'AGENTS.md').write_text(LEGACY_GATEWAY_BLOCK.replace('one task at a time','all tasks simultaneously'))
    assert validate_agents_gateway(tmp_path)
