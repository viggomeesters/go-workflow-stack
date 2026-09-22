"""Full serial autonomy campaign through real processes, Git and public CLI."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "autonomy-campaign" / "campaign.py"


def load_campaign():
    spec = importlib.util.spec_from_file_location("autonomy_campaign", FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_one_controller_process_explores_repairs_releases_two_tasks_and_audits_goal(tmp_path):
    campaign = load_campaign()
    proof = tmp_path / "proof"
    prepared = campaign.setup(proof, runtime=ROOT)
    binary = tmp_path / "codex"
    binary.write_text("#!" + sys.executable + "\n" + campaign.worker_source())
    binary.chmod(0o755)
    capture = proof / "worker-calls.jsonl"
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "AUTONOMY_CAMPAIGN_CAPTURE": str(capture),
        "GO_STACK_ALLOW_DEV": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    result = campaign.run(prepared, env=env)

    assert result["returncode"] == 0, result["stdout"] + result["stderr"]
    payload = result["result"]
    assert payload["status"] == "goal_verified"
    assert payload["goal_verified"] is True
    assert payload["completed_tasks"] == ["deliver-alpha", "deliver-beta"]
    assert payload["completion_audit"]["status"] == "achieved"
    assert len(list((proof / "invocations").glob("*.json"))) == 2  # intake + one controller

    repo = prepared["repo"]
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    alpha = [item for item in calls if item["task_id"] == "deliver-alpha"]
    assert [item["phase"] for item in alpha] == ["build", "critic", "repair", "critic"]
    assert "forced behavioral finding" in json.dumps(alpha[2]["feedback"])
    assert [item["phase"] for item in calls if item["task_id"] == "deliver-beta"] == ["build", "critic"]
    assert len(list((repo / ".go/intake").glob("*.json"))) == 1
    handoff = repo / payload["completion_audit"]["handoff"]["path"]
    assert handoff.is_file()
    assert "Ship a bounded two-step local campaign." in handoff.read_text()
    for task_id, tag in (("deliver-alpha", "v1.2.0"), ("deliver-beta", "v1.3.0")):
        done = json.loads((repo / f".go/tasks/done/{task_id}.json").read_text())
        assert done["review_status"] == "approved"
        assert done["release_receipt"]["tag"] == tag
        assert not (proof / "workspaces" / task_id).exists()
        commit = campaign.git(repo, "rev-parse", f"{tag}^{{commit}}")
        assert campaign.git(repo, "ls-remote", "origin", f"refs/tags/{tag}^{{}}").startswith(commit)


def test_unresolved_task_does_not_prevent_independent_release(tmp_path):
    campaign = load_campaign()
    proof = tmp_path / "proof"
    prepared = campaign.setup(proof, runtime=ROOT, unresolved_task="deliver-alpha")
    binary = tmp_path / "codex"
    binary.write_text("#!" + sys.executable + "\n" + campaign.worker_source())
    binary.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "AUTONOMY_CAMPAIGN_CAPTURE": str(proof / "worker-calls.jsonl"),
        "GO_STACK_ALLOW_DEV": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    result = campaign.run(prepared, env=env)

    assert result["returncode"] == 1
    payload = result["result"]
    assert payload["status"] == "no_eligible_tasks"
    assert payload["goal_verified"] is False
    assert payload["completed_tasks"] == ["deliver-beta"]
    assert (prepared["repo"] / ".go/tasks/open/deliver-alpha.json").is_file()
    done = json.loads((prepared["repo"] / ".go/tasks/done/deliver-beta.json").read_text())
    assert done["release_receipt"]["tag"] == "v1.2.0"
    assert campaign.git(prepared["repo"], "ls-remote", "origin", "refs/tags/v1.2.0")


def test_omitted_rough_requirement_is_rejected_before_task_or_worker_write(tmp_path):
    campaign = load_campaign()
    proof = tmp_path / "proof"

    result = campaign.setup(proof, runtime=ROOT, omit_requirement=True, expect_failure=True)

    assert result["returncode"] != 0
    assert "does not preserve requested outcome" in result["stderr"]
    repo = result["repo"]
    assert sorted(path.name for path in (repo / ".go/tasks/open").glob("*.json")) == [
        "deliver-alpha.json",
        "deliver-beta.json",
    ]
    assert not (repo / ".go/intake").exists()
    assert not (proof / "worker-calls.jsonl").exists()


def test_failure_matrix_references_executable_behavior_and_the_gate_runs_every_suite():
    import ast

    campaign = load_campaign()
    matrix = json.loads((ROOT / "fixtures/autonomy-campaign/coverage.json").read_text())
    gate = (ROOT / "scripts/check-autonomy.sh").read_text()
    assert matrix["schema"] == "go-workflow.autonomy-campaign-coverage.v1"
    for scenario in matrix["scenarios"].values():
        assert scenario["injected_effect"]
        assert scenario["expected_observation"]
        for reference in scenario["tests"]:
            filename, test_name = reference.split("::")
            tree = ast.parse((ROOT / "tests" / filename).read_text())
            assert test_name in {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
            assert "tests/" + filename in gate
    assert "GitHub Actions" in matrix["limits"]


def test_dirty_control_repo_blocks_publication_and_preserves_dirt(tmp_path):
    campaign = load_campaign()
    proof = tmp_path / "proof"
    prepared = campaign.setup(proof, runtime=ROOT)
    (prepared["repo"] / "VERSION").write_text("dirty-user-version\n")
    binary = tmp_path / "codex"
    binary.write_text("#!" + sys.executable + "\n" + campaign.worker_source())
    binary.chmod(0o755)
    capture = proof / "worker-calls.jsonl"
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "AUTONOMY_CAMPAIGN_CAPTURE": str(capture),
        "GO_STACK_ALLOW_DEV": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    result = campaign.run(prepared, env=env)

    assert result["returncode"] != 0
    assert result["result"]["status"] == "unknown_external_effect"
    assert capture.exists(), "worktree execution may proceed; publication must fail closed"
    assert campaign.git(prepared["repo"], "ls-remote", "origin", "refs/tags/v1.2.0") == ""
    assert (prepared["repo"] / "VERSION").read_text() == "dirty-user-version\n"
