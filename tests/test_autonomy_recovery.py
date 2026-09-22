"""Failure-injected recovery and control semantics for serial campaigns."""
from __future__ import annotations

from argparse import Namespace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_autonomy_campaign import serial_campaign_fixture, write
from go_workflow.campaign import CampaignError, execute_campaign
from go_workflow.cli import build_parser


def args_for(contract: Path, workspace_root: Path, **overrides) -> Namespace:
    values = {
        "campaign": str(contract),
        "previous_campaign": "",
        "campaign_workspace_root": str(workspace_root),
        "campaign_action": "run",
        "agent": "fixture",
        "max_commands": 3,
        "max_attempts": 4,
    }
    values.update(overrides)
    return Namespace(**values)


def test_public_cli_exposes_explicit_campaign_control_actions():
    parser = build_parser()
    for action in ("run", "resume", "pause", "drain", "cancel"):
        parsed = parser.parse_args(["auto", ".", "--campaign-action", action])
        assert parsed.campaign_action == action


def complete(control, task):
    source = control / ".go/tasks/open" / f"{task['id']}.json"
    record = json.loads(source.read_text(encoding="utf-8"))
    record.update(status="done", work_status="completed", review_status="approved")
    write(control / ".go/tasks/done" / source.name, record)
    source.unlink()


def test_dispatch_checkpoint_recovers_same_task_after_controller_death(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    api = __import__("go_workflow.cli", fromlist=["cli"])
    calls = []

    def dies_after_dispatch(control, args, mode, task, api):
        calls.append(task["id"])
        raise KeyboardInterrupt("injected controller death")

    with pytest.raises(KeyboardInterrupt):
        execute_campaign(repo, args_for(contract_path, tmp_path / "work"), "go-auto", api, dies_after_dispatch)
    state_path = repo / ".go/runs/campaigns" / contract["id"] / "state.json"
    interrupted = json.loads(state_path.read_text())
    assert interrupted["dispatch"]["task_id"] == first["id"]
    assert interrupted["dispatch"]["stage"] == "dispatched"
    assert interrupted["controller"]["pid"] == os.getpid()
    assert interrupted["clock"]["active_since_epoch"] is not None
    interrupted["clock"]["active_since_epoch"] -= 7
    write(state_path, interrupted)

    def resumed(control, args, mode, task, api):
        calls.append(task["id"])
        complete(control, task)
        return 0, {"status": "task_complete", "completed_tasks": [task["id"]],
                   "checks": [], "commands_run": 1, "repair_attempts": 0}

    execute_campaign(repo, args_for(contract_path, tmp_path / "work"), "go-auto", api, resumed)
    recovered = json.loads(state_path.read_text())
    assert calls[:2] == [first["id"], first["id"]]
    assert any(item["event"] == "campaign.dispatch_recovered" for item in recovered["history"])
    assert recovered["consumption"]["active_wall_seconds"] >= 7


def test_command_and_repair_budgets_are_cumulative_across_resume(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    api = __import__("go_workflow.cli", fromlist=["cli"])
    observed = []

    def interrupted(control, args, mode, task, api):
        observed.append((task["id"], args.max_commands, args.max_attempts))
        return 0, {"status": "budget_exhausted", "completed_tasks": [], "checks": [],
                   "commands_run": 2, "repair_attempts": 1, "summary": "checkpoint"}

    execute_campaign(repo, args_for(contract_path, tmp_path / "work"), "go-auto", api, interrupted)

    def finishes(control, args, mode, task, api):
        observed.append((task["id"], args.max_commands, args.max_attempts))
        complete(control, task)
        return 0, {"status": "task_complete", "completed_tasks": [task["id"]],
                   "checks": [], "commands_run": 1, "repair_attempts": 1}

    code, result = execute_campaign(
        repo, args_for(contract_path, tmp_path / "work", max_commands=99, max_attempts=99),
        "go-auto", api, finishes,
    )
    state = json.loads((repo / ".go/runs/campaigns" / contract["id"] / "state.json").read_text())
    assert observed == [(first["id"], 3, 4), (first["id"], 1, 3)]
    assert state["limits"] == {"max_commands": 3, "max_repairs": 4}
    assert state["resources"]["commands_used"] == 3
    assert state["resources"]["repair_attempts"] == 2
    assert code == 0 and result["status"] == "budget_exhausted"
    assert (repo / ".go/tasks/open" / f"{second['id']}.json").is_file()


def test_repeated_identical_failure_records_no_progress(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    api = __import__("go_workflow.cli", fromlist=["cli"])

    calls = []

    def unchanged_failure(control, args, mode, task, api):
        calls.append(task["id"])
        return 1, {"status": "blocked", "completed_tasks": [], "checks": [{"exit_code": 1}],
                   "commands_run": 1, "repair_attempts": 1, "summary": "same failing proof"}

    execute_campaign(
        repo, args_for(contract_path, tmp_path / "work", max_commands=10),
        "go-auto", api, unchanged_failure,
    )

    def recover_independent(control, args, mode, task, api):
        calls.append(task["id"])
        if task["id"] == first["id"]:
            assert len(args.campaign_failed_proof) == 1
            return unchanged_failure(control, args, mode, task, api)
        complete(control, task)
        return 0, {"status": "task_complete", "completed_tasks": [task["id"]],
                   "checks": [], "commands_run": 1, "repair_attempts": 0}

    _, result = execute_campaign(
        repo, args_for(contract_path, tmp_path / "work", max_commands=10),
        "go-auto", api, recover_independent,
    )
    state = json.loads((repo / ".go/runs/campaigns" / contract["id"] / "state.json").read_text())
    assert result["status"] == "authority_required"
    assert "different strategy" in result["summary"].lower()
    assert state["failures"][-1]["repeat_count"] == 2
    assert state["failures"][-1]["strategy"] == "block_or_isolate_research"
    assert state["failures"][-1]["evidence"]["checks"] == [{"exit_code": 1}]
    assert (repo / ".go/tasks/done" / f"{second['id']}.json").is_file()
    assert calls[-1] == second["id"]


def test_active_no_progress_task_is_preserved_as_blocked_before_independent_work(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    api = __import__("go_workflow.cli", fromlist=["cli"])
    source = repo / ".go/tasks/open" / f"{first['id']}.json"
    active = json.loads(source.read_text())
    active.update(
        status="active",
        work_status="in_progress",
        claim={"agent": "fixture", "claimed_at": "2026-09-22T00:00:00Z", "base_commit": "a" * 40},
    )
    write(repo / ".go/tasks/active" / source.name, active)
    source.unlink()
    write(repo / ".go/runs" / first["id"] / "run-state.json", {"phase": "repair"})
    calls = []

    def first_failure(control, args, mode, task, api):
        calls.append(task["id"])
        return 1, {"status": "blocked", "completed_tasks": [], "checks": [{"exit_code": 1}],
                   "commands_run": 1, "repair_attempts": 1, "summary": "same active failure"}

    execute_campaign(
        repo, args_for(contract_path, tmp_path / "work", max_commands=10),
        "go-auto", api, first_failure,
    )

    def retry_then_finish(control, args, mode, task, api):
        if task["id"] == first["id"]:
            return first_failure(control, args, mode, task, api)
        calls.append(task["id"])
        complete(control, task)
        return 0, {"status": "task_complete", "completed_tasks": [task["id"]],
                   "checks": [], "commands_run": 1, "repair_attempts": 0}

    _, result = execute_campaign(
        repo, args_for(contract_path, tmp_path / "work", max_commands=10),
        "go-auto", api, retry_then_finish,
    )
    assert result["status"] == "authority_required"
    assert (repo / ".go/tasks/blocked" / f"{first['id']}.json").is_file()
    assert (repo / ".go/tasks/done" / f"{second['id']}.json").is_file()
    assert calls == [first["id"], first["id"], second["id"]]


def test_temporary_provider_failure_backs_off_and_keeps_model_binding(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    api = __import__("go_workflow.cli", fromlist=["cli"])
    calls = []

    def unavailable(control, args, mode, task, api):
        calls.append(task["id"])
        return 1, {
            "status": "provider_unavailable",
            "completed_tasks": [],
            "checks": [],
            "commands_run": 0,
            "repair_attempts": 0,
            "summary": "temporary provider outage",
        }

    _, first_result = execute_campaign(
        repo, args_for(contract_path, tmp_path / "work"), "go-auto", api, unavailable,
    )
    state_path = repo / ".go/runs/campaigns" / contract["id"] / "state.json"
    state = json.loads(state_path.read_text())
    assert first_result["status"] == "provider_backoff"
    assert state["provider"]["model"] == first["execution_contract"]["model"]

    def forbidden(*_args):
        raise AssertionError("backoff dispatched a provider call")

    _, waiting = execute_campaign(
        repo, args_for(contract_path, tmp_path / "work"), "go-auto", api, forbidden,
    )
    assert waiting["status"] == "provider_backoff"
    assert calls == [first["id"]]

    state["provider"]["not_before_epoch"] = 0
    write(state_path, state)
    state["provider"]["model"] = {"id": "fallback", "effort": "low"}
    write(state_path, state)
    with pytest.raises(CampaignError, match="fallback refused"):
        execute_campaign(
            repo, args_for(contract_path, tmp_path / "work"), "go-auto", api, forbidden,
        )


@pytest.mark.parametrize("action, expected", [
    ("pause", "paused"), ("drain", "drained"), ("cancel", "cancelled"),
])
def test_explicit_control_actions_do_not_dispatch_new_work(tmp_path: Path, action: str, expected: str):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    api = __import__("go_workflow.cli", fromlist=["cli"])

    def forbidden(*_args):
        raise AssertionError("control action dispatched work")

    code, result = execute_campaign(
        repo, args_for(contract_path, tmp_path / "work", campaign_action=action),
        "go-auto", api, forbidden,
    )
    state = json.loads((repo / ".go/runs/campaigns" / contract["id"] / "state.json").read_text())
    assert code == 1 and result["status"] == expected
    assert state["control"]["action"] == action
    assert state["current_task"] is None


def test_live_foreign_controller_identity_refuses_takeover(tmp_path: Path):
    repo, contract_path, contract, first, second = serial_campaign_fixture(tmp_path)
    api = __import__("go_workflow.cli", fromlist=["cli"])

    def stopped(control, args, mode, task, api):
        return 0, {"status": "budget_exhausted", "completed_tasks": [], "checks": [],
                   "commands_run": 0, "repair_attempts": 0, "summary": "fixture stop"}

    execute_campaign(repo, args_for(contract_path, tmp_path / "work"), "go-auto", api, stopped)
    state_path = repo / ".go/runs/campaigns" / contract["id"] / "state.json"
    state = json.loads(state_path.read_text())
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        state["controller"]["pid"] = sleeper.pid
        state["dispatch"] = {"task_id": first["id"], "stage": "dispatched", "nonce": "foreign",
                             "attempt": 1, "started_at": state["updated_at"]}
        write(state_path, state)
        with pytest.raises(CampaignError, match="controller is still live"):
            execute_campaign(repo, args_for(contract_path, tmp_path / "work"), "go-auto", api, stopped)
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)
