"""Safe task-claim handoff preserves managed workspace contents and identity."""
from __future__ import annotations

import json
from pathlib import Path
import pytest
import socket
import subprocess

from test_abc_worktrees import create, git, invoke, setup_repo


def test_task_handoff_rebinds_idle_registered_workspace_without_touching_dirty_work(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    created = create(repo, base, workspace, owner="owner")
    assert created.returncode == 0, created.stdout + created.stderr

    app = workspace / "app.txt"
    app.write_text("valuable dirty work\n", encoding="utf-8")
    before_content = app.read_bytes()
    before_head = git(workspace, "rev-parse", "HEAD")
    before_status = git(workspace, "status", "--porcelain", "--untracked-files=all")
    before_task = json.loads((repo / ".go/tasks/active/task-schema-smoke.json").read_text(encoding="utf-8"))

    transferred = invoke(
        repo,
        "task", "handoff", repo,
        "--task-id", "task-schema-smoke",
        "--expected-owner", "owner",
        "--new-owner", "hermes",
        "--old-run-id", "run-1",
        "--new-run-id", "run-2",
        "--reason", "operator-authorized idle recovery",
        "--confirm-owner-stopped",
        "--json",
    )
    assert transferred.returncode == 0, transferred.stdout + transferred.stderr
    result = json.loads(transferred.stdout)
    assert result["status"] == "transferred"
    assert result["task_id"] == "task-schema-smoke"
    assert result["old_owner"] == "owner" and result["new_owner"] == "hermes"
    assert result["old_run_id"] == "run-1" and result["new_run_id"] == "run-2"

    task = json.loads((repo / ".go/tasks/active/task-schema-smoke.json").read_text(encoding="utf-8"))
    assert task["status"] == "active"
    assert task.get("review_status") == before_task.get("review_status")
    assert task["claim"]["agent"] == "hermes"

    record = json.loads((repo / ".go/workspaces/task-schema-smoke.json").read_text(encoding="utf-8"))
    assert record["control_host"] == socket.gethostname()
    from go_workflow.worktrees import marker_path
    marker = json.loads(marker_path(workspace).read_text(encoding="utf-8"))
    assert marker["control_host"] == socket.gethostname()
    assert record["owner"] == "hermes" and record["run_id"] == "run-2"
    assert record["ownership_history"][-1] == {"owner": "owner", "run_id": "run-1"}
    assert (workspace / "app.txt").read_bytes() == before_content
    assert git(workspace, "rev-parse", "HEAD") == before_head
    assert git(workspace, "status", "--porcelain", "--untracked-files=all") == before_status

    readback = invoke(
        repo,
        "workspace", "status", repo,
        "--task-id", "task-schema-smoke", "--owner", "hermes", "--run-id", "run-2", "--json",
    )
    assert readback.returncode == 0, readback.stdout + readback.stderr
    assert json.loads(readback.stdout)["owner"] == "hermes"

    old_binding = invoke(
        repo,
        "workspace", "status", repo,
        "--task-id", "task-schema-smoke", "--owner", "owner", "--run-id", "run-1", "--json",
    )
    assert old_binding.returncode != 0
    validation = invoke(repo, "validate", repo)
    assert validation.returncode == 0, validation.stdout + validation.stderr


def test_task_handoff_refuses_registered_workspace_from_foreign_host(tmp_path: Path) -> None:
    from go_workflow.worktrees import marker_path

    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    registry = repo / ".go/workspaces/task-schema-smoke.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    foreign = "not-" + socket.gethostname()
    record["control_host"] = foreign
    registry.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    marker = marker_path(workspace)
    marker_data = json.loads(marker.read_text(encoding="utf-8"))
    marker_data["control_host"] = foreign
    marker.write_text(json.dumps(marker_data, indent=2) + "\n", encoding="utf-8")
    before_task = (repo / ".go/tasks/active/task-schema-smoke.json").read_bytes()
    before_record = registry.read_bytes()

    refused = invoke(repo, *_handoff_args(repo, confirm_same_host=True))
    assert refused.returncode != 0
    assert "control_host" in refused.stderr.lower()
    assert (repo / ".go/tasks/active/task-schema-smoke.json").read_bytes() == before_task
    assert registry.read_bytes() == before_record


def test_task_handoff_requires_same_host_ack_for_hostless_workspace(tmp_path: Path) -> None:
    from go_workflow.worktrees import marker_path

    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    registry = repo / ".go/workspaces/task-schema-smoke.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record.pop("control_host", None)
    registry.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    marker = marker_path(workspace)
    marker_data = json.loads(marker.read_text(encoding="utf-8"))
    marker_data.pop("control_host", None)
    marker.write_text(json.dumps(marker_data, indent=2) + "\n", encoding="utf-8")

    unconfirmed = invoke(repo, *_handoff_args(repo))
    assert unconfirmed.returncode != 0
    assert "same-host" in unconfirmed.stderr.lower()
    confirmed = invoke(repo, *_handoff_args(repo, confirm_same_host=True))
    assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr


def _handoff_args(repo: Path, *, acknowledge: bool = True, expected_owner: str = "owner", legacy: bool = False, handoff_id: str = "", confirm_same_host: bool = False):
    args = [
        "task", "handoff", repo,
        "--task-id", "task-schema-smoke",
        "--expected-owner", expected_owner,
        "--new-owner", "hermes",
        "--reason", "operator-authorized idle recovery",
    ]
    if acknowledge:
        args.append("--confirm-owner-stopped")
    if legacy:
        args.append("--legacy-unmanaged")
    else:
        args.extend(["--old-run-id", "run-1", "--new-run-id", "run-2"])
    if confirm_same_host:
        args.append("--confirm-same-host")
    if handoff_id:
        args.extend(["--handoff-id", handoff_id])
    return args + ["--json"]


def test_task_handoff_requires_stopped_ack_and_exact_owner(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    (workspace / "app.txt").write_text("dirty and preserved\n", encoding="utf-8")
    before_task = (repo / ".go/tasks/active/task-schema-smoke.json").read_bytes()
    before_workspace = (repo / ".go/workspaces/task-schema-smoke.json").read_bytes()
    before_content = (workspace / "app.txt").read_bytes()

    missing_ack = invoke(repo, *_handoff_args(repo, acknowledge=False))
    assert missing_ack.returncode != 0
    wrong_owner = invoke(repo, *_handoff_args(repo, expected_owner="someone-else"))
    assert wrong_owner.returncode != 0

    assert (repo / ".go/tasks/active/task-schema-smoke.json").read_bytes() == before_task
    assert (repo / ".go/workspaces/task-schema-smoke.json").read_bytes() == before_workspace
    assert (workspace / "app.txt").read_bytes() == before_content


def test_task_handoff_refuses_invisible_index_flags(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    (workspace / "app.txt").write_text("hidden tracked work\n", encoding="utf-8")
    git(workspace, "add", "app.txt")
    git(workspace, "update-index", "--skip-worktree", "app.txt")
    before_task = (repo / ".go/tasks/active/task-schema-smoke.json").read_bytes()
    before_record = (repo / ".go/workspaces/task-schema-smoke.json").read_bytes()

    refused = invoke(repo, *_handoff_args(repo))
    assert refused.returncode != 0
    assert "index flags can hide changes" in refused.stderr
    assert (repo / ".go/tasks/active/task-schema-smoke.json").read_bytes() == before_task
    assert (repo / ".go/workspaces/task-schema-smoke.json").read_bytes() == before_record


def test_task_handoff_accepts_matching_stopped_completion_host_for_legacy_workspace(tmp_path: Path) -> None:
    import socket
    from go_workflow.run_state import read_state, state_path
    from go_workflow.worktrees import marker_path

    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    registry = repo / ".go/workspaces/task-schema-smoke.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record.pop("control_host", None)
    registry.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    marker = marker_path(workspace)
    marker_data = json.loads(marker.read_text(encoding="utf-8"))
    marker_data.pop("control_host", None)
    marker.write_text(json.dumps(marker_data, indent=2) + "\n", encoding="utf-8")
    state = {
        "schema": "go-workflow.managed-run.v1",
        "task_id": "task-schema-smoke",
        "project": json.loads((repo / ".go/project.json").read_text(encoding="utf-8"))["id"],
        "control_repo": str(repo), "owner": "owner", "run_id": "run-1",
        "task_hash": "fixture-task-hash",
        "workspace": {key: record[key] for key in ("path", "branch", "base_branch", "base_commit")},
        "models": {}, "effects": {}, "code": {},
        "controller": {"host": socket.gethostname(), "pid": 1},
        "phase": "complete", "attempt": 1, "check_index": 0,
        "checks": [], "phase_evidence": [], "budgets": [], "history": [], "requirements": [],
        "worker_group": None, "inflight": None, "execution_cwd": str(workspace),
    }
    completion = state_path(repo, "task-schema-smoke", "completion")
    completion.parent.mkdir(parents=True, exist_ok=True)
    completion.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    assert read_state(repo, "task-schema-smoke", "completion")["controller"]["host"] == socket.gethostname()

    transferred = invoke(repo, *_handoff_args(repo))
    assert transferred.returncode == 0, transferred.stdout + transferred.stderr
    result = json.loads(transferred.stdout)
    assert "completion-checkpoint-host" in result["host_evidence"]
    assert json.loads((repo / ".go/tasks/active/task-schema-smoke.json").read_text(encoding="utf-8"))["claim"]["agent"] == "hermes"


def test_task_handoff_supports_explicit_legacy_unmanaged_claims(tmp_path: Path) -> None:
    repo, _ = setup_repo(tmp_path)
    task_path = repo / ".go/tasks/active/task-schema-smoke.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    workspace_contract_refused = invoke(repo, *_handoff_args(repo, legacy=True))
    assert workspace_contract_refused.returncode != 0
    task.pop("execution_contract", None)
    task["review_status"] = "needs_fix"
    task["review_history"] = [{"agent": "codex-reviewer", "status": "needs_fix", "evidence": "preserve this"}]
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")

    refused = invoke(repo, *_handoff_args(repo, legacy=False))
    assert refused.returncode != 0
    unconfirmed_host = invoke(repo, *_handoff_args(repo, legacy=True))
    assert unconfirmed_host.returncode != 0
    assert not (repo / ".go/workspaces/task-schema-smoke.json").exists()

    transferred = invoke(repo, *_handoff_args(repo, legacy=True, confirm_same_host=True))
    assert transferred.returncode == 0, transferred.stdout + transferred.stderr
    updated = json.loads(task_path.read_text(encoding="utf-8"))
    assert updated["status"] == "active"
    assert updated["review_status"] == "needs_fix"
    assert updated["review_history"] == task["review_history"]
    assert updated["claim"]["agent"] == "hermes"
    assert not (repo / ".go/workspaces/task-schema-smoke.json").exists()


def test_task_handoff_refuses_publication_checkpoint_before_mutation(tmp_path: Path) -> None:
    from go_workflow.run_state import state_path

    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    publication = state_path(repo, "task-schema-smoke", "publication")
    publication.parent.mkdir(parents=True, exist_ok=True)
    publication.write_text(json.dumps({"schema": "fixture"}), encoding="utf-8")
    before_task = (repo / ".go/tasks/active/task-schema-smoke.json").read_bytes()
    before_record = (repo / ".go/workspaces/task-schema-smoke.json").read_bytes()

    refused = invoke(repo, *_handoff_args(repo))
    assert refused.returncode != 0
    assert "publication checkpoint" in refused.stderr.lower()
    assert (repo / ".go/tasks/active/task-schema-smoke.json").read_bytes() == before_task
    assert (repo / ".go/workspaces/task-schema-smoke.json").read_bytes() == before_record


def test_task_handoff_refuses_live_managed_controller_before_mutation(tmp_path: Path) -> None:
    import socket
    from go_workflow.run_state import state_path

    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    record = json.loads((repo / ".go/workspaces/task-schema-smoke.json").read_text(encoding="utf-8"))
    state = {
        "schema": "go-workflow.managed-run.v1",
        "task_id": "task-schema-smoke",
        "project": json.loads((repo / ".go/project.json").read_text(encoding="utf-8"))["id"],
        "control_repo": str(repo),
        "owner": "owner",
        "run_id": "run-1",
        "task_hash": "fixture-task-hash",
        "workspace": {key: record[key] for key in ("path", "branch", "base_branch", "base_commit")},
        "models": {}, "effects": {}, "code": {}, "controller": {"host": socket.gethostname(), "pid": __import__("os").getpid()},
        "phase": "build", "attempt": 1, "check_index": 0, "checks": [], "phase_evidence": [],
        "budgets": [], "history": [], "requirements": [], "worker_group": None,
        "inflight": {"phase": "build", "nonce": "live", "attempt": 1, "started_at": "fixture"},
    }
    state_path(repo, "task-schema-smoke").parent.mkdir(parents=True, exist_ok=True)
    state_path(repo, "task-schema-smoke").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    before_task = (repo / ".go/tasks/active/task-schema-smoke.json").read_bytes()
    before_record = (repo / ".go/workspaces/task-schema-smoke.json").read_bytes()

    refused = invoke(repo, *_handoff_args(repo))
    assert refused.returncode != 0
    assert (repo / ".go/tasks/active/task-schema-smoke.json").read_bytes() == before_task
    assert (repo / ".go/workspaces/task-schema-smoke.json").read_bytes() == before_record


def test_task_handoff_retry_is_idempotent(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    args = _handoff_args(repo, handoff_id="a" * 32)
    first = invoke(repo, *args)
    assert first.returncode == 0, first.stdout + first.stderr
    record = json.loads((repo / ".go/workspaces/task-schema-smoke.json").read_text(encoding="utf-8"))
    history = list(record["ownership_history"])

    retry = invoke(repo, *args)
    assert retry.returncode == 0, retry.stdout + retry.stderr
    again = json.loads((repo / ".go/workspaces/task-schema-smoke.json").read_text(encoding="utf-8"))
    assert again["ownership_history"] == history


def test_task_handoff_resumes_after_interrupted_workspace_registry_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse
    from go_workflow import cli, worktrees
    from go_workflow.cli import RepoLocalError

    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    (workspace / "app.txt").write_text("preserve across recovery\n", encoding="utf-8")
    original_atomic_json = cli.atomic_json
    registry = repo / ".go/workspaces/task-schema-smoke.json"
    failed = False

    def fail_once(path: Path, data: dict) -> None:
        nonlocal failed
        if Path(path) == registry and data.get("owner") == "hermes" and not failed:
            failed = True
            raise OSError("simulated registry write interruption")
        original_atomic_json(path, data)

    args = argparse.Namespace(
        task_id="task-schema-smoke", expected_owner="owner", new_owner="hermes",
        old_run_id="run-1", new_run_id="run-2", reason="resume interrupted handoff",
        confirm_owner_stopped=True, legacy_unmanaged=False, confirm_same_host=False,
        handoff_id="b" * 32, json=True,
    )
    monkeypatch.setattr(cli, "atomic_json", fail_once)
    with pytest.raises(RepoLocalError, match="handoff .* is pending"):
        cli.perform_task_handoff(repo, args)
    assert json.loads((repo / ".go/tasks/active/task-schema-smoke.json").read_text(encoding="utf-8"))["claim"]["agent"] == "hermes"
    assert json.loads(registry.read_text(encoding="utf-8"))["owner"] == "owner"
    assert (workspace / "app.txt").read_text(encoding="utf-8") == "preserve across recovery\n"

    monkeypatch.setattr(cli, "atomic_json", original_atomic_json)
    resumed = cli.perform_task_handoff(repo, args)
    assert resumed["status"] == "transferred"
    assert json.loads(registry.read_text(encoding="utf-8"))["owner"] == "hermes"
    assert (workspace / "app.txt").read_text(encoding="utf-8") == "preserve across recovery\n"
    events = [json.loads(line) for line in (repo / ".go/runs/events.jsonl").read_text(encoding="utf-8").splitlines()]
    handoffs = [item for item in events if (item.get("data") or {}).get("handoff_id") == "b" * 32]
    assert len(handoffs) == 1


def _prepare_handoff_interrupted_before_claim_write(
    repo: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    handoff_id: str,
):
    task_path = repo / ".go/tasks/active/task-schema-smoke.json"
    registry = repo / ".go/workspaces/task-schema-smoke.json"
    from argparse import Namespace
    from go_workflow import cli
    from go_workflow.cli import RepoLocalError

    args = Namespace(
        task_id="task-schema-smoke",
        expected_owner="owner",
        new_owner="hermes",
        old_run_id="run-1",
        new_run_id="run-2",
        reason="recover prepared handoff",
        confirm_owner_stopped=True,
        confirm_same_host=False,
        legacy_unmanaged=False,
        handoff_id=handoff_id,
        json=True,
    )
    original_dump_json = cli.dump_json

    def stop_before_claim_write(path: Path, data: dict) -> None:
        if Path(path) == task_path and (data.get("claim") or {}).get("agent") == "hermes":
            raise OSError("simulated interruption before claim write")
        original_dump_json(path, data)

    monkeypatch.setattr(cli, "dump_json", stop_before_claim_write)
    with pytest.raises(RepoLocalError):
        cli.perform_task_handoff(repo, args)
    monkeypatch.setattr(cli, "dump_json", original_dump_json)
    handoff_journal = repo / ".go/runs/task-schema-smoke/handoffs" / f"{handoff_id}.json"
    journal = json.loads(handoff_journal.read_text(encoding="utf-8"))
    assert journal["status"] == "prepared"
    assert json.loads(task_path.read_text(encoding="utf-8"))["claim"]["agent"] == "owner"
    assert json.loads(registry.read_text(encoding="utf-8"))["owner"] == "owner"
    return args, task_path, registry, handoff_journal


@pytest.mark.parametrize("mutation", ["live-managed", "dirty-content", "foreign-host", "hidden-index"])
def test_prepared_handoff_revalidates_safety_before_any_state_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    import socket
    from go_workflow import cli
    from go_workflow.run_state import state_path
    from go_workflow.worktrees import marker_path
    from go_workflow.cli import RepoLocalError

    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    dirty = workspace / "app.txt"
    dirty.write_text("pre-handoff contents\n", encoding="utf-8")
    if mutation == "hidden-index":
        git(workspace, "add", "app.txt")
    handoff_id = "c" * 32
    args, task_path, registry, journal_path = _prepare_handoff_interrupted_before_claim_write(
        repo, workspace, monkeypatch, handoff_id
    )

    if mutation == "live-managed":
        active = state_path(repo, "task-schema-smoke")
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text("{}\n", encoding="utf-8")
    elif mutation == "dirty-content":
        dirty.write_text("changed after handoff intent\n", encoding="utf-8")
    elif mutation == "foreign-host":
        record = json.loads(registry.read_text(encoding="utf-8"))
        record["control_host"] = "FOREIGN-HOST"
        registry.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        marker = marker_path(workspace)
        marker_value = json.loads(marker.read_text(encoding="utf-8"))
        marker_value["control_host"] = "FOREIGN-HOST"
        marker.write_text(json.dumps(marker_value, indent=2) + "\n", encoding="utf-8")
    elif mutation == "hidden-index":
        git(workspace, "update-index", "--skip-worktree", "app.txt")

    task_before_retry = task_path.read_bytes()
    registry_before_retry = registry.read_bytes()
    workspace_before_retry = dirty.read_bytes()
    with pytest.raises(RepoLocalError):
        cli.perform_task_handoff(repo, args)
    assert task_path.read_bytes() == task_before_retry
    assert registry.read_bytes() == registry_before_retry
    assert dirty.read_bytes() == workspace_before_retry
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == "prepared"


def test_prepared_handoff_refuses_staged_index_blob_drift_before_any_state_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from go_workflow import cli
    from go_workflow.cli import RepoLocalError
    from go_workflow.worktrees import marker_path, workspace_content_fingerprint

    def replace_staged_blob(contents: str) -> None:
        blob = subprocess.run(
            ["git", "-C", str(workspace), "hash-object", "-w", "--stdin"],
            input=contents,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        git(workspace, "update-index", "--add", "--cacheinfo", f"100644,{blob},app.txt")

    repo, _ = setup_repo(tmp_path)
    (repo / "app.txt").write_text("committed baseline\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-m", "add tracked handoff fixture")
    base = git(repo, "rev-parse", "HEAD")
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    dirty = workspace / "app.txt"
    dirty.write_text("pre-handoff worktree contents\n", encoding="utf-8")
    replace_staged_blob("staged contents before interrupted handoff\n")
    assert git(workspace, "status", "--porcelain", "--untracked-files=all").startswith("MM ")
    args, task_path, registry, journal_path = _prepare_handoff_interrupted_before_claim_write(
        repo, workspace, monkeypatch, "d" * 32
    )

    fingerprint_before_status_refresh = workspace_content_fingerprint(json.loads(registry.read_text(encoding="utf-8")))
    git(workspace, "status", "--porcelain", "--untracked-files=all")
    assert workspace_content_fingerprint(json.loads(registry.read_text(encoding="utf-8"))) == fingerprint_before_status_refresh
    status_before_retry = git(workspace, "status", "--porcelain", "--untracked-files=all")
    replace_staged_blob("staged contents changed after handoff intent\n")
    assert git(workspace, "status", "--porcelain", "--untracked-files=all") == status_before_retry
    task_before_retry = task_path.read_bytes()
    registry_before_retry = registry.read_bytes()
    marker_before_retry = marker_path(workspace).read_bytes()
    worktree_before_retry = dirty.read_bytes()
    index_before_retry = git(workspace, "ls-files", "--stage", "--", "app.txt")

    with pytest.raises(RepoLocalError):
        cli.perform_task_handoff(repo, args)

    assert task_path.read_bytes() == task_before_retry
    assert registry.read_bytes() == registry_before_retry
    assert marker_path(workspace).read_bytes() == marker_before_retry
    assert dirty.read_bytes() == worktree_before_retry
    assert git(workspace, "ls-files", "--stage", "--", "app.txt") == index_before_retry
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == "prepared"


@pytest.mark.parametrize("workspace_path_kind", ["untracked", "tracked"])
def test_prepared_handoff_rolls_back_exact_owner_snapshots_when_workspace_drifts_during_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_path_kind: str
) -> None:
    """An apply-window workspace edit must not leave a failed transfer half-applied."""
    from go_workflow import cli
    from go_workflow.cli import RepoLocalError

    repo, base = setup_repo(tmp_path)
    if workspace_path_kind == "tracked":
        (repo / "app.txt").write_text("committed baseline\n", encoding="utf-8")
        git(repo, "add", "app.txt")
        git(repo, "commit", "-m", "add tracked handoff fixture")
        base = git(repo, "rev-parse", "HEAD")
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    dirty = workspace / "app.txt"
    dirty.write_text("before apply-window drift\n", encoding="utf-8")
    args, task_path, registry, journal_path = _prepare_handoff_interrupted_before_claim_write(
        repo, workspace, monkeypatch, "e" * 32
    )
    task_before_retry = task_path.read_bytes()
    registry_before_retry = registry.read_bytes()
    events_path = repo / ".go/runs/events.jsonl"
    events_before_retry = events_path.read_bytes() if events_path.exists() else b""
    original_dump_json = cli.dump_json
    mutated = False

    def drift_workspace_before_task_claim_write(path: Path, data: dict) -> None:
        nonlocal mutated
        if (Path(path) == task_path and (data.get("claim") or {}).get("agent") == "hermes"
                and not mutated):
            mutated = True
            dirty.write_text("changed during apply-window\n", encoding="utf-8")
        original_dump_json(path, data)

    monkeypatch.setattr(cli, "dump_json", drift_workspace_before_task_claim_write)
    with pytest.raises(RepoLocalError, match="workspace content changed while handoff was applied"):
        cli.perform_task_handoff(repo, args)

    assert mutated
    assert dirty.read_bytes() == b"changed during apply-window\n"
    assert json.loads(task_path.read_text(encoding="utf-8")) == json.loads(task_before_retry)
    assert json.loads(registry.read_text(encoding="utf-8")) == json.loads(registry_before_retry)
    assert (events_path.read_bytes() if events_path.exists() else b"") == events_before_retry
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["status"] == "blocked"
    assert "workspace content changed while handoff was applied" in journal["blocked_reason"]

    with pytest.raises(RepoLocalError, match="blocked"):
        cli.perform_task_handoff(repo, args)
    assert json.loads(task_path.read_text(encoding="utf-8")) == json.loads(task_before_retry)
    assert json.loads(registry.read_text(encoding="utf-8")) == json.loads(registry_before_retry)
    assert (events_path.read_bytes() if events_path.exists() else b"") == events_before_retry


def test_apply_window_rollback_never_overwrites_concurrent_owner_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback restores only records that still equal this handoff's after snapshot."""
    from go_workflow import cli
    from go_workflow.cli import RepoLocalError

    repo, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(repo, base, workspace, owner="owner").returncode == 0
    dirty = workspace / "app.txt"
    dirty.write_text("before apply-window drift\n", encoding="utf-8")
    args, task_path, registry, journal_path = _prepare_handoff_interrupted_before_claim_write(
        repo, workspace, monkeypatch, "f" * 32
    )
    original_fingerprint = cli.workspace_content_fingerprint
    calls = 0

    def drift_task_after_final_task_readback(record: dict) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            concurrent_task = json.loads(task_path.read_text(encoding="utf-8"))
            concurrent_task["claim"]["agent"] = "other-agent"
            task_path.write_text(json.dumps(concurrent_task), encoding="utf-8")
            dirty.write_text("changed during apply-window\n", encoding="utf-8")
        return original_fingerprint(record)

    monkeypatch.setattr(cli, "workspace_content_fingerprint", drift_task_after_final_task_readback)
    with pytest.raises(RepoLocalError, match="blocked"):
        cli.perform_task_handoff(repo, args)

    assert json.loads(task_path.read_text(encoding="utf-8"))["claim"]["agent"] == "other-agent"
    assert json.loads(registry.read_text(encoding="utf-8"))["owner"] == "owner"
    assert dirty.read_bytes() == b"changed during apply-window\n"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["status"] == "blocked"
    assert journal["rollback"]["task"] == "not_restored_after_state_drifted"
    assert journal["rollback"]["workspace"] == "restored"
