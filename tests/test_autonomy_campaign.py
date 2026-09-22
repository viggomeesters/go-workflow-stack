"""Durable bounded campaign controller behavior through public and library APIs."""
from __future__ import annotations

from copy import deepcopy
from argparse import Namespace
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from test_autonomy_contracts import fixture as contract_fixture, sample as sample_contract
from test_abc_resume import runner_fixture, run_managed
from test_abc_worktrees import git
from go_workflow.campaign import execute_campaign
from go_workflow.worktrees import record_integration


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli" / "go.py"


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def call(repo: Path, *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *map(str, args)],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=60,
    )


def campaign_fixture(tmp_path: Path):
    repo, contract, task = contract_fixture(tmp_path)
    other = deepcopy(task)
    other.update(id="unrelated", summary="Unrelated task outside this campaign")
    write(repo / ".go/tasks/open/unrelated.json", other)
    hierarchy_path = repo / ".go/hierarchy.json"
    hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8"))
    hierarchy["epics"][0]["features"][0]["tasks"].append("unrelated")
    write(hierarchy_path, hierarchy)
    contract_path = tmp_path / "campaign.json"
    write(contract_path, contract)
    return repo, contract_path, contract, task


def serial_campaign_fixture(tmp_path: Path):
    repo, contract_path, contract, first = campaign_fixture(tmp_path)
    second_path = repo / ".go/tasks/open/unrelated.json"
    second = json.loads(second_path.read_text(encoding="utf-8"))
    second_text = "Second requested behavior works"
    second["requested_outcomes"] = [
        {
            "id": "R1",
            "text": second_text,
            "status": "pending",
            "source": "intake_acceptance",
            "evidence": [],
        }
    ]
    write(second_path, second)
    contract["authority"]["permitted_tasks"] = [first["id"], second["id"]]
    contract["goal"]["outcomes"][0]["task_outcomes"].append(
        {
            "task_id": second["id"],
            "requirement_id": "R1",
            "text_sha256": hashlib.sha256(second_text.encode()).hexdigest(),
        }
    )
    write(contract_path, contract)
    return repo, contract_path, contract, first, second


def test_campaign_plan_is_allowlist_ordered_and_read_only(tmp_path: Path):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)
    before = {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob("*") if path.is_file()}

    result = call(
        repo,
        "auto",
        repo,
        "--campaign",
        contract_path,
        "--campaign-workspace-root",
        tmp_path / "workspaces",
        "--json",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["campaign"]["id"] == contract["id"]
    assert payload["campaign"]["revision"] == contract["revision"]
    assert payload["campaign"]["next_tasks"] == [task["id"]]
    assert payload["next_tasks"] == [task["id"]]
    assert not (repo / ".go/runs/campaigns").exists()
    after = {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    assert after == before


def test_one_foreground_run_advances_serially_and_persists_truthful_stop(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    calls: list[str] = []

    def completed_executor(control, args, mode, task, api):
        calls.append(task["id"])
        source = control / ".go/tasks/open" / f"{task['id']}.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        record.update(status="done", work_status="completed", review_status="approved")
        target = control / ".go/tasks/done" / source.name
        write(target, record)
        source.unlink()
        return 0, {
            "schema": "go-workflow.auto-run-result.v1",
            "mode": mode,
            "repo": str(control),
            "status": "task_complete",
            "completed_tasks": [task["id"]],
            "blocked_task": None,
            "checks": [],
            "commands_run": 1,
        }

    args = Namespace(
        campaign=str(contract_path),
        previous_campaign="",
        campaign_workspace_root=str(tmp_path / "workspaces"),
        agent="fixture",
        max_commands=10,
        max_minutes=5,
        max_attempts=5,
    )
    code, result = execute_campaign(repo, args, "go-auto", __import__("go_workflow.cli", fromlist=["cli"]), completed_executor)

    assert code == 1
    assert calls == [first["id"], second["id"]]
    assert result["status"] == "no_eligible_tasks"
    assert result["goal_verified"] is False
    assert result["completed_tasks"] == calls
    state_path = repo / ".go/runs/campaigns" / contract["id"] / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["contract"]["sha256"] == result["campaign"]["contract_sha256"]
    assert state["consumption"] == {
        "active_wall_seconds": state["consumption"]["active_wall_seconds"],
        "attempts_started": 2,
        "tasks_completed": 2,
    }
    assert state["completed_tasks"] == calls
    assert state["stop"]["condition"] == "no_eligible_tasks"
    latest = json.loads((repo / ".go/runs/latest.json").read_text(encoding="utf-8"))
    assert latest["resume_args"][0:3] == ["go-auto", ".", "--execute"]
    assert latest["resume_args"][latest["resume_args"].index("--campaign") + 1] == str(contract_path)
    assert latest["resume_args"][latest["resume_args"].index("--campaign-workspace-root") + 1] == str(tmp_path / "workspaces")
    assert (repo / ".go/runs/resume.sh").stat().st_mode & 0o111


def test_resume_keeps_cumulative_consumption_and_rejects_mandate_drift(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    calls: list[str] = []

    def interrupted_executor(control, args, mode, task, api):
        calls.append(task["id"])
        if task["id"] == second["id"]:
            return 0, {
                "status": "budget_exhausted",
                "completed_tasks": [],
                "checks": [],
                "commands_run": 1,
                "summary": "phase checkpoint saved",
            }
        source = control / ".go/tasks/open" / f"{task['id']}.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        record.update(status="done", work_status="completed", review_status="approved")
        write(control / ".go/tasks/done" / source.name, record)
        source.unlink()
        return 0, {"status": "task_complete", "completed_tasks": [task["id"]], "checks": [], "commands_run": 1}

    args = Namespace(campaign=str(contract_path), previous_campaign="",
                     campaign_workspace_root=str(tmp_path / "workspaces"), agent="fixture")
    api = __import__("go_workflow.cli", fromlist=["cli"])
    code, first_result = execute_campaign(repo, args, "go-auto", api, interrupted_executor)
    assert code == 0 and first_result["status"] == "budget_exhausted"
    assert calls == [first["id"], second["id"]]

    def resumed_executor(control, args, mode, task, api):
        calls.append(task["id"])
        source = control / ".go/tasks/open" / f"{task['id']}.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        record.update(status="done", work_status="completed", review_status="approved")
        write(control / ".go/tasks/done" / source.name, record)
        source.unlink()
        return 0, {"status": "task_complete", "completed_tasks": [task["id"]], "checks": [], "commands_run": 1}

    code, resumed = execute_campaign(repo, args, "go-auto", api, resumed_executor)
    assert code == 1 and resumed["status"] == "no_eligible_tasks"
    assert calls == [first["id"], second["id"], second["id"]]
    state_path = repo / ".go/runs/campaigns" / contract["id"] / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["consumption"]["attempts_started"] == 3
    assert state["consumption"]["tasks_completed"] == 2

    changed = deepcopy(contract)
    changed["goal"]["text"] = "Silently changed mandate"
    changed_path = tmp_path / "changed-campaign.json"
    write(changed_path, changed)
    changed_args = Namespace(campaign=str(changed_path), previous_campaign="",
                             campaign_workspace_root=str(tmp_path / "workspaces"), agent="fixture")
    try:
        execute_campaign(repo, changed_args, "go-auto", api, resumed_executor)
    except ValueError as exc:
        assert "mandate changed" in str(exc)
    else:
        raise AssertionError("resume accepted a different contract digest")
    assert calls == [first["id"], second["id"], second["id"]]


def test_resume_reconciles_completion_after_controller_checkpoint_loss(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    calls: list[str] = []

    def crash_after_task_completion(control, args, mode, task, api):
        calls.append(task["id"])
        source = control / ".go/tasks/open" / f"{task['id']}.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        record.update(status="done", work_status="completed", review_status="approved")
        write(control / ".go/tasks/done" / source.name, record)
        source.unlink()
        write(control / ".go/runs" / task["id"] / "run-state.json", {"phase": "complete"})
        raise RuntimeError("simulated process loss after authoritative task completion")

    args = Namespace(campaign=str(contract_path), previous_campaign="",
                     campaign_workspace_root=str(tmp_path / "workspaces"), agent="fixture")
    api = __import__("go_workflow.cli", fromlist=["cli"])
    with pytest.raises(RuntimeError, match="simulated process loss"):
        execute_campaign(repo, args, "go-auto", api, crash_after_task_completion)

    def complete_remaining(control, args, mode, task, api):
        calls.append(task["id"])
        source = control / ".go/tasks/open" / f"{task['id']}.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        record.update(status="done", work_status="completed", review_status="approved")
        write(control / ".go/tasks/done" / source.name, record)
        source.unlink()
        return 0, {"status": "task_complete", "completed_tasks": [task["id"]], "checks": [], "commands_run": 1}

    code, result = execute_campaign(repo, args, "go-auto", api, complete_remaining)

    assert code == 1 and result["status"] == "no_eligible_tasks"
    assert calls == [first["id"], second["id"]]
    state = json.loads((repo / ".go/runs/campaigns" / contract["id"] / "state.json").read_text())
    assert state["completed_tasks"] == [first["id"], second["id"]]
    assert any(item["event"] == "campaign.task_reconciled" for item in state["history"])


def test_managed_binding_failure_is_a_durable_unsafe_stop(tmp_path: Path):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)

    def execute_managed(control, args, mode, selected, api):
        raise AssertionError("binding must fail before worker dispatch")

    args = Namespace(campaign=str(contract_path), previous_campaign="",
                     campaign_workspace_root=str(tmp_path / "workspaces"), agent="fixture")
    code, result = execute_campaign(
        repo, args, "go-auto", __import__("go_workflow.cli", fromlist=["cli"]), execute_managed,
    )

    assert code == 1
    assert result["status"] == "unsafe_repository"
    assert result["blocked_task"] == task["id"]
    state = json.loads((repo / ".go/runs/campaigns" / contract["id"] / "state.json").read_text())
    assert state["stop"]["condition"] == "unsafe_repository"
    assert "task_worktree" in state["stop"]["reason"]


def test_task_local_decision_blocker_skips_to_independent_permitted_task(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    contract["decisions"].append(
        {
            "id": "local-choice",
            "status": "unresolved",
            "source_ref": "decision:local-choice",
            "owner": "user",
            "resolution_gate": "choose first-task behavior",
            "task_ids": [first["id"]],
            "bounds": "Only the first task is affected",
        }
    )
    write(contract_path, contract)

    result = call(repo, "auto", repo, "--campaign", contract_path,
                  "--campaign-workspace-root", tmp_path / "workspaces", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["campaign"]["next_tasks"] == [second["id"]]
    assert payload["campaign"]["skipped_tasks"][0]["task_id"] == first["id"]
    assert "unresolved decision local-choice" in " ".join(payload["campaign"]["skipped_tasks"][0]["findings"])


def test_active_managed_task_precedes_new_open_task(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    source = repo / ".go/tasks/open" / f"{first['id']}.json"
    active = json.loads(source.read_text(encoding="utf-8"))
    active.update(
        status="active",
        work_status="in_progress",
        claim={"agent": "fixture", "claimed_at": "2026-09-22T00:00:00Z", "base_commit": "a" * 40},
    )
    write(repo / ".go/tasks/active" / source.name, active)
    source.unlink()
    write(
        repo / ".go/runs" / first["id"] / "run-state.json",
        {
            "schema": "go-workflow.managed-run.v1",
            "task_id": first["id"],
            "project": contract["project"],
            "control_repo": str(repo),
            "owner": "fixture",
            "run_id": "fixture-run",
            "task_hash": "fixture-hash",
            "phase": "verify",
            "attempt": 1,
            "check_index": 0,
            "worker_group": None,
            "inflight": None,
            "controller": {"host": socket.gethostname(), "pid": os.getpid()},
            "workspace": {},
            "models": {},
            "effects": {},
            "code": {},
            "checks": [],
            "phase_evidence": [],
            "budgets": [],
            "history": [],
            "requirements": [],
        },
    )

    result = call(repo, "auto", repo, "--campaign", contract_path,
                  "--campaign-workspace-root", tmp_path / "workspaces", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["campaign"]["next_tasks"] == [first["id"]]
    assert payload["campaign"]["selection"] == "resume_active"


def test_new_managed_tasks_receive_deterministic_workspace_binding(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    for task in (first, second):
        path = repo / ".go/tasks/open" / f"{task['id']}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["execution_contract"]["workspace"] = {
            "mode": "task_worktree",
            "base_branch": "main",
            "control_state": "repo_local_single_writer",
        }
        write(path, record)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "fixture"],
        cwd=repo,
        check=True,
    )
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
    observed: list[dict[str, str]] = []

    def bound_executor(control, args, mode, task, api):
        observed.append({name: getattr(args, name) for name in (
            "workspace_path", "workspace_branch", "base_branch", "base_commit", "run_id"
        )})
        source = control / ".go/tasks/open" / f"{task['id']}.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        record.update(status="done", work_status="completed", review_status="approved")
        write(control / ".go/tasks/done" / source.name, record)
        source.unlink()
        return 0, {"status": "task_complete", "completed_tasks": [task["id"]], "checks": [], "commands_run": 1}

    workspace_root = tmp_path / "campaign workspaces"
    args = Namespace(campaign=str(contract_path), previous_campaign="",
                     campaign_workspace_root=str(workspace_root), agent="fixture")
    execute_campaign(repo, args, "go-auto", __import__("go_workflow.cli", fromlist=["cli"]), bound_executor)

    assert [Path(item["workspace_path"]) for item in observed] == [workspace_root / first["id"], workspace_root / second["id"]]
    assert [item["workspace_branch"] for item in observed] == [
        f"codex/{contract['id']}-{first['id']}", f"codex/{contract['id']}-{second['id']}"
    ]
    assert all(item["base_branch"] == "main" and item["base_commit"] == base for item in observed)
    assert len({item["run_id"] for item in observed}) == 2


def test_campaign_never_grants_a_different_deployment_target(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    project_path = repo / ".go/project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["release_profiles"] = {
        "production": {"provider": "git-tag", "remote": "origin", "branch": "main",
                       "deployment": {"mode": "required", "target": "production"}},
        "staging": {"provider": "git-tag", "remote": "origin", "branch": "main",
                    "deployment": {"mode": "required", "target": "staging"}},
    }
    write(project_path, project)
    for task in (first, second):
        path = repo / ".go/tasks/open" / f"{task['id']}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["execution_contract"].update(
            task_kind="product", release={"mode": "required", "profile": "production"},
        )
        write(path, record)
    contract["authority"]["release"] = {
        "profiles": ["production"], "allow_push": True, "source_ref": "user:release",
    }
    contract["authority"]["deployment"] = {
        "targets": ["staging"], "source_ref": "user:staging-only",
    }
    contract["goal"]["outcomes"][0]["required_evidence"].extend(["release", "live"])
    write(contract_path, contract)
    observed: list[bool] = []

    def completed_executor(control, args, mode, task, api):
        observed.append(args.allow_deploy)
        source = control / ".go/tasks/open" / f"{task['id']}.json"
        record = json.loads(source.read_text(encoding="utf-8"))
        record.update(status="done", work_status="completed", review_status="approved")
        write(control / ".go/tasks/done" / source.name, record)
        source.unlink()
        return 0, {"status": "task_complete", "completed_tasks": [task["id"]], "checks": [], "commands_run": 1}

    args = Namespace(campaign=str(contract_path), previous_campaign="",
                     campaign_workspace_root=str(tmp_path / "workspaces"), agent="fixture", allow_deploy=True)
    execute_campaign(repo, args, "go-auto", __import__("go_workflow.cli", fromlist=["cli"]), completed_executor)

    assert observed == [False, False]


def test_task_release_profile_must_be_inside_campaign_authority(tmp_path: Path):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)
    project_path = repo / ".go/project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["release_profiles"] = {
        "permitted": {"provider": "git-tag", "remote": "origin", "branch": "main"},
        "unpermitted": {"provider": "git-tag", "remote": "other", "branch": "main"},
    }
    write(project_path, project)
    task_path = repo / ".go/tasks/open" / f"{task['id']}.json"
    task["execution_contract"].update(
        task_kind="product", release={"mode": "required", "profile": "unpermitted"},
    )
    write(task_path, task)
    contract["authority"]["release"].update(
        profiles=["permitted"], allow_push=True, source_ref="user:publish",
    )
    contract["goal"]["outcomes"][0]["required_evidence"].append("release")
    write(contract_path, contract)

    result = call(repo, "auto", repo, "--campaign", contract_path,
                  "--campaign-workspace-root", tmp_path / "workspaces", "--json")

    assert result.returncode == 1
    assert "release profile outside campaign authority" in result.stderr


def test_missing_runtime_authority_is_not_misreported_as_repository_corruption(tmp_path: Path):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)

    def authority_gate(control, args, mode, selected, api):
        return 1, {"status": "resume_gate", "completed_tasks": [], "checks": [], "commands_run": 0,
                   "summary": "Deployment requires explicit authority"}

    args = Namespace(campaign=str(contract_path), previous_campaign="",
                     campaign_workspace_root=str(tmp_path / "workspaces"), agent="fixture")
    code, result = execute_campaign(
        repo, args, "go-auto", __import__("go_workflow.cli", fromlist=["cli"]), authority_gate,
    )

    assert code == 1
    assert result["status"] == "authority_required"
    assert result["blocked_task"] == task["id"]


def test_execute_loop_plan_routes_campaign_before_single_task_selection(tmp_path: Path, monkeypatch):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)
    import go_workflow.cli as cli

    observed = {}

    def fake_campaign(control, args, mode, api, executor):
        observed.update(control=control, mode=mode, executor=executor.__name__)
        return 0, {"status": "campaign-sentinel", "completed_tasks": []}

    monkeypatch.setattr(cli, "execute_campaign", fake_campaign)
    args = Namespace(campaign=str(contract_path), campaign_workspace_root=str(tmp_path / "workspaces"))

    code, result = cli.execute_loop_plan(repo, args, "go-auto")

    assert code == 0 and result["status"] == "campaign-sentinel"
    assert observed == {"control": repo, "mode": "go-auto", "executor": "execute_managed"}


def test_campaign_state_schema_and_corrupt_consumption_fail_closed(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)

    def stop_after_selection(control, args, mode, task, api):
        return 0, {"status": "budget_exhausted", "completed_tasks": [], "checks": [], "commands_run": 1,
                   "summary": "fixture interruption"}

    args = Namespace(campaign=str(contract_path), previous_campaign="",
                     campaign_workspace_root=str(tmp_path / "workspaces"), agent="fixture")
    api = __import__("go_workflow.cli", fromlist=["cli"])
    execute_campaign(repo, args, "go-auto", api, stop_after_selection)
    path = repo / ".go/runs/campaigns" / contract["id"] / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "schemas/campaign-run.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(state)

    state["consumption"]["attempts_started"] = True
    write(path, state)
    with pytest.raises(ValueError, match="consumption checkpoint invalid"):
        execute_campaign(repo, args, "go-auto", api, stop_after_selection)

    state["consumption"]["attempts_started"] = 1
    state["unexpected"] = "must not be ignored"
    write(path, state)
    with pytest.raises(ValueError, match="run state invalid"):
        execute_campaign(repo, args, "go-auto", api, stop_after_selection)

    state.pop("unexpected")
    write(path, state)
    snapshot = repo / state["contract"]["snapshot_path"]
    frozen = json.loads(snapshot.read_text(encoding="utf-8"))
    frozen["goal"]["text"] = "tampered after the run began"
    write(snapshot, frozen)
    with pytest.raises(ValueError, match="snapshot integrity"):
        execute_campaign(repo, args, "go-auto", api, stop_after_selection)


def test_real_managed_cleanup_advances_to_next_task_in_same_cli_run(tmp_path: Path, monkeypatch):
    repo, base, workspace, capture = runner_fixture(tmp_path, monkeypatch)
    first_open_path = repo / ".go/tasks/open/task-schema-smoke.json"
    first_open = json.loads(first_open_path.read_text(encoding="utf-8"))
    first_open["execution_contract"]["task_kind"] = "smoke"
    first_open["execution_contract"]["release"] = {
        "mode": "none",
        "reason": "Disposable integration fixture has no publication boundary.",
    }
    write(first_open_path, first_open)
    initial = run_managed(repo, base, workspace, 10, initial=True)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    assert json.loads(initial.stdout)["status"] == "release_pending"
    git(workspace, "add", "app.txt")
    git(workspace, "commit", "-qm", "first product")
    head = git(workspace, "rev-parse", "HEAD")
    git(repo, "merge", "--ff-only", "task/resume")
    record_integration(repo, "task-schema-smoke", "owner", "resume-run", head)

    active_path = repo / ".go/tasks/active/task-schema-smoke.json"
    first = json.loads(active_path.read_text(encoding="utf-8"))
    first_text = "First managed task is delivered"
    first["requested_outcomes"] = [{"id": "R1", "text": first_text, "status": "verified",
                                     "source": "intake_acceptance", "evidence": [{"summary": "fixture"}]}]
    first.update(status="done", work_status="completed", review_status="approved")
    write(repo / ".go/tasks/done/task-schema-smoke.json", first)
    active_path.unlink()
    run_path = repo / ".go/runs/task-schema-smoke/run-state.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["phase"] = "cleanup"
    write(run_path, run)

    second = deepcopy(first)
    second_text = "Second managed task reaches its release boundary"
    second.update(id="second-managed", status="open", summary="Second managed task", work_status="pending",
                  review_status="none", claim={"agent": None, "claimed_at": None})
    second["requested_outcomes"] = [{"id": "R1", "text": second_text, "status": "pending",
                                      "source": "intake_acceptance", "evidence": []}]
    second.pop("release_receipt", None)
    write(repo / ".go/tasks/open/second-managed.json", second)
    hierarchy_path = repo / ".go/hierarchy.json"
    hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8"))
    hierarchy["epics"][0]["features"][0]["tasks"].append("second-managed")
    write(hierarchy_path, hierarchy)

    contract = sample_contract()
    contract.update(id="managed-serial", project=first["project"])
    contract["authority"]["permitted_tasks"] = [first["id"], second["id"]]
    contract["authority"]["models"] = [first["execution_contract"]["model"]]
    contract["goal"]["outcomes"][0]["task_outcomes"] = [
        {"task_id": first["id"], "requirement_id": "R1", "text_sha256": hashlib.sha256(first_text.encode()).hexdigest()},
        {"task_id": second["id"], "requirement_id": "R1", "text_sha256": hashlib.sha256(second_text.encode()).hexdigest()},
    ]
    contract["basis"]["vision_sha256"] = hashlib.sha256((repo / ".go/vision.json").read_bytes()).hexdigest()
    contract["basis"]["principles_sha256"] = hashlib.sha256((repo / ".go/architecture-principles.json").read_bytes()).hexdigest()
    decision = {
        "schema": "go-workflow.repo-local.event.v1", "kind": "event", "event": "decision.recorded",
        "created_at": "2026-09-22T00:00:00Z", "task_id": first["id"], "agent": "fixture",
        "data": {"decision_id": "bounded-policy", "status": "accepted"},
    }
    decision_path = repo / ".go/decisions/events.jsonl"
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(json.dumps(decision) + "\n", encoding="utf-8")
    contract_path = tmp_path / "managed-campaign.json"
    write(contract_path, contract)

    result = call(repo, "auto", repo, "--campaign", contract_path,
                  "--campaign-workspace-root", tmp_path / "campaign-workers", "--execute",
                  "--agent", "owner", "--executor-agent", "codex", "--max-commands", "10",
                  "--max-minutes", "5", "--json")

    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "authority_required", payload
    assert payload["completed_tasks"] == [first["id"]]
    assert payload["blocked_task"] == second["id"]
    assert not workspace.exists()
    assert (repo / ".go/tasks/active/second-managed.json").is_file()
    assert capture.read_text(encoding="utf-8").splitlines() == ["build", "critic", "build", "critic"]
