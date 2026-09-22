"""Task-by-task delivery truth and controlled base reconciliation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket

import pytest
from jsonschema import Draft202012Validator

from test_abc_release import fixture as release_fixture, prepare, proofs
from test_abc_worktrees import create, git, setup_repo


def assert_report_schema(report):
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas/task-delivery-report.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(report)


def test_required_release_is_not_live_from_local_completion_alone(tmp_path):
    from go_workflow.campaign_delivery import delivery_report

    repo, _ = setup_repo(tmp_path)
    task_path = repo / ".go/tasks/active/task-schema-smoke.json"
    task = json.loads(task_path.read_text())
    task.update(status="done", work_status="completed", review_status="approved")
    done = repo / ".go/tasks/done/task-schema-smoke.json"
    done.parent.mkdir(exist_ok=True)
    done.write_text(json.dumps(task))
    task_path.unlink()

    report = delivery_report(repo, task["id"])
    assert_report_schema(report)

    assert report["delivered"] is False
    assert report["reported_live"] is False
    assert report["stop_condition"] == "authority_required"
    assert [item["code"] for item in report["blockers"]] == [
        "managed_checkpoint_missing",
        "workspace_registry_missing",
        "release_receipt_missing",
        "release_checkpoint_missing",
    ]


def test_malformed_effect_checkpoints_are_unsafe_not_success_or_absence(tmp_path):
    from go_workflow.campaign_delivery import delivery_report

    repo, _ = setup_repo(tmp_path)
    active = repo / ".go/tasks/active/task-schema-smoke.json"
    task = json.loads(active.read_text())
    task.update(status="done", work_status="completed", review_status="approved")
    done = repo / ".go/tasks/done/task-schema-smoke.json"
    done.parent.mkdir(exist_ok=True)
    done.write_text(json.dumps(task))
    active.unlink()
    run_dir = repo / ".go/runs/task-schema-smoke"
    run_dir.mkdir(parents=True)
    (run_dir / "run-state.json").write_text('{"phase":"complete"}\n')
    (run_dir / "release-state.json").write_text('{"phase":"published"}\n')

    report = delivery_report(repo, task["id"])
    assert_report_schema(report)

    assert report["delivered"] is False and report["reported_live"] is False
    assert report["stop_condition"] == "unsafe_repository"
    codes = [item["code"] for item in report["blockers"]]
    assert "managed_checkpoint_invalid" in codes
    assert "release_checkpoint_invalid" in codes
    assert "publication_readback_pending" not in codes


def test_published_task_survives_checkpoint_only_branch_advance_without_redeploy(tmp_path, monkeypatch):
    from go_workflow.campaign_delivery import delivery_report
    from go_workflow.release import publish_release
    from go_workflow.worktrees import cleanup_workspace

    monkeypatch.chdir(tmp_path)
    repo, worker, source = release_fixture(tmp_path)
    prepare(repo)
    proofs(worker, source)
    published = publish_release(repo, "task-schema-smoke", "owner", "run-1")
    cleanup_workspace(repo, "task-schema-smoke", "owner", "run-1")
    release_state = repo / ".go/runs/task-schema-smoke/release-state.json"
    before = release_state.read_bytes()
    marker = repo / ".go/runs/task-schema-smoke/post-release-checkpoint.json"
    marker.write_text('{"status":"closed"}\n')
    git(repo, "add", "--", str(marker.relative_to(repo)))
    git(repo, "commit", "-qm", "checkpoint-only closure")

    report = delivery_report(repo, "task-schema-smoke")
    assert_report_schema(report)

    assert report["delivered"] is True
    assert report["publication"]["status"] == "verified"
    assert report["publication"]["commit"] == published["commit"]
    assert report["head_relation"] == "checkpoint_only_advance"
    assert report["reported_live"] is False
    assert release_state.read_bytes() == before


def test_reasoned_no_release_task_completes_without_invented_publication(tmp_path):
    from go_workflow.campaign_delivery import delivery_report
    from go_workflow.worktrees import cleanup_workspace, record_integration

    repo, base = setup_repo(tmp_path)
    active = repo / ".go/tasks/active/task-schema-smoke.json"
    task = json.loads(active.read_text())
    task["execution_contract"]["release"] = {"mode": "none", "reason": "Local mechanical fixture only"}
    active.write_text(json.dumps(task))
    worker = tmp_path / "worker"
    record = json.loads(create(repo, base, worker).stdout)
    record_integration(repo, task["id"], "owner", "run-1", base)
    task = json.loads(active.read_text())
    task.update(status="done", work_status="completed", review_status="approved")
    done = repo / ".go/tasks/done/task-schema-smoke.json"
    done.parent.mkdir(exist_ok=True)
    done.write_text(json.dumps(task))
    active.unlink()
    cleaned = cleanup_workspace(repo, task["id"], "owner", "run-1")
    run_dir = repo / ".go/runs/task-schema-smoke"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run-state.json").write_text(json.dumps({
        "schema": "go-workflow.managed-run.v1",
        "task_id": task["id"],
        "project": task["project"],
        "control_repo": str(repo.resolve()),
        "owner": "owner",
        "run_id": "run-1",
        "task_hash": "fixture",
        "phase": "complete",
        "workspace": cleaned,
        "models": {},
        "effects": {},
        "code": {},
        "check_index": 0,
        "attempt": 1,
        "worker_group": None,
        "inflight": None,
        "controller": {"host": socket.gethostname(), "pid": os.getpid()},
        "checks": [],
        "phase_evidence": [],
        "budgets": [],
        "history": [],
        "requirements": [],
        "execution_cwd": str(repo),
    }))

    report = delivery_report(repo, task["id"])
    assert_report_schema(report)

    assert report["delivered"] is True
    assert report["release_policy"] == "none"
    assert report["publication"]["status"] == "not_applicable"
    assert report["reported_live"] is False
    assert not (run_dir / "release-state.json").exists()


def test_completion_checks_use_an_unregistered_evidence_identical_checkout(tmp_path, monkeypatch):
    from go_workflow.completion import capture_verification, read_artifact

    monkeypatch.chdir(tmp_path)
    repo, base = setup_repo(tmp_path)
    worker = tmp_path / "worker"
    assert create(repo, base, worker).returncode == 0
    cli = Path(__file__).resolve().parents[1] / "cli/go.py"
    command = (
        "python3 -c \"import pathlib,subprocess,tempfile,sys; "
        "target=pathlib.Path(tempfile.mkdtemp())/'child'; "
        f"result=subprocess.run([sys.executable,{str(cli)!r},'adopt',str(target),"
        "'--project-id','child','--name','Child'],capture_output=True,text=True); "
        "assert result.returncode == 0, result.stderr\""
    )
    active = repo / ".go/tasks/active/task-schema-smoke.json"
    task = json.loads(active.read_text())
    task["verification"] = [command]
    active.write_text(json.dumps(task))

    artifact, _ = capture_verification(worker, task["id"], "owner")
    raw = read_artifact(repo / ".go", artifact["checks"][0]["raw"])

    assert artifact["status"] == "passed"
    assert raw["verification_source"]["isolation"] == "unregistered_disposable_checkout"
    assert raw["verification_source"]["changed_files"] == []
    assert not (worker / ".go/tasks/done/task-schema-smoke.json").exists()


def test_history_preserving_reconciliation_updates_only_owned_workspace_and_invalidates_checks(tmp_path):
    from go_workflow.worktrees import reconcile_workspace

    repo, base = setup_repo(tmp_path)
    worker = tmp_path / "worker"
    record = json.loads(create(repo, base, worker).stdout)
    (worker / "app.txt").write_text("owned change")
    git(worker, "add", "--", "app.txt")
    git(worker, "commit", "-qm", "owned product")
    worker_before = git(worker, "rev-parse", "HEAD")
    (repo / "base.txt").write_text("new shared base")
    git(repo, "add", "--", "base.txt")
    git(repo, "commit", "-qm", "advance base")
    new_base = git(repo, "rev-parse", "HEAD")
    (repo / "personal.txt").write_text("preserve local save")
    run_path = repo / ".go/runs/task-schema-smoke/run-state.json"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(json.dumps({
        "schema": "go-workflow.managed-run.v1",
        "task_id": "task-schema-smoke",
        "project": "fixture",
        "control_repo": str(repo.resolve()),
        "owner": "owner",
        "run_id": "run-1",
        "task_hash": "fixture",
        "phase": "critic",
        "workspace": record,
        "models": {},
        "effects": {},
        "code": {},
        "check_index": 1,
        "attempt": 1,
        "worker_group": None,
        "inflight": None,
        "controller": {"host": socket.gethostname(), "pid": os.getpid()},
        "checks": [{"status": "passed"}],
        "phase_evidence": [{"status": "passed"}],
        "budgets": [],
        "history": [],
        "requirements": [],
    }))

    reconciled = reconcile_workspace(repo, "task-schema-smoke", "owner", "run-1")

    assert reconciled["base_commit"] == new_base
    assert reconciled["state"] == "ready"
    assert git(worker, "merge-base", "--is-ancestor", worker_before, "HEAD") == ""
    assert git(worker, "merge-base", "--is-ancestor", new_base, "HEAD") == ""
    assert (repo / "personal.txt").read_text() == "preserve local save"
    run = json.loads(run_path.read_text())
    assert run["workspace"]["base_commit"] == new_base
    assert run["phase"] == "verify"
    assert run["checks"] == [] and run["phase_evidence"] == [] and run["check_index"] == 0
    assert run["history"][-1]["event"] == "workspace.base_reconciled"


def test_reconciliation_aborts_conflicts_and_retains_both_histories(tmp_path):
    from go_workflow.worktrees import WorkspaceError, reconcile_workspace

    repo, base = setup_repo(tmp_path)
    worker = tmp_path / "worker"
    assert create(repo, base, worker).returncode == 0
    (worker / "app.txt").write_text("task version")
    git(worker, "add", "--", "app.txt")
    git(worker, "commit", "-qm", "task edit")
    worker_before = git(worker, "rev-parse", "HEAD")
    (repo / "app.txt").write_text("base version")
    git(repo, "add", "--", "app.txt")
    git(repo, "commit", "-qm", "base edit")
    new_base = git(repo, "rev-parse", "HEAD")

    with pytest.raises(WorkspaceError, match=r"conflicts: app\.txt"):
        reconcile_workspace(repo, "task-schema-smoke", "owner", "run-1")

    registry = json.loads((repo / ".go/workspaces/task-schema-smoke.json").read_text())
    assert registry["base_commit"] == base
    assert git(worker, "rev-parse", "HEAD") == worker_before
    assert git(repo, "rev-parse", "HEAD") == new_base
    assert git(worker, "status", "--porcelain") == ""
