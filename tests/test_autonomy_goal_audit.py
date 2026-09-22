"""Outcome-bound campaign completion and auditable handoff behavior."""
from __future__ import annotations

from argparse import Namespace
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

import go_workflow.campaign_audit as campaign_audit
from go_workflow.campaign import execute_campaign
from test_autonomy_campaign import campaign_fixture


ROOT = Path(__file__).resolve().parents[1]


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def proof(repo: Path, task_id: str, kind: str) -> dict[str, str]:
    path = repo / ".go/runs" / task_id / "completion" / f"{kind}.json"
    write(path, {"schema": f"fixture.{kind}.v1", "task_id": task_id, "status": "passed"})
    return {
        "path": str(path.relative_to(repo)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def complete_campaign_task(repo: Path, task: dict, *, tracked: bool = True) -> dict:
    source = repo / ".go/tasks/open" / f"{task['id']}.json"
    task = json.loads(source.read_text(encoding="utf-8"))
    verification = proof(repo, task["id"], "verification")
    critic = proof(repo, task["id"], "critic")
    task.update(status="done", work_status="completed", review_status="approved")
    if tracked:
        task["outcome_tracking_version"] = 1
    task["completion_evidence"] = {
        "schema": "go-workflow.completion-evidence.v1",
        "verification": verification,
        "critic": critic,
    }
    task["requested_outcomes"][0].update(
        status="verified",
        evidence=[{"summary": critic["path"]}],
    )
    write(repo / ".go/tasks/done" / source.name, task)
    source.unlink()
    return task


def test_empty_queue_and_declared_metrics_are_not_goal_proof(tmp_path: Path, monkeypatch):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)
    complete_campaign_task(repo, task, tracked=False)
    monkeypatch.setattr(campaign_audit, "completion_findings", lambda *a, **k: [])

    audit = campaign_audit.audit_campaign_goal(repo, contract_path, persist=False)

    assert audit["status"] == "blocked"
    assert audit["goal_verified"] is False
    assert audit["outcomes"][0]["status"] == "blocked"
    assert "outcome-tracked" in " ".join(audit["outcomes"][0]["findings"])
    assert task["id"] not in audit["queue"]["open"]


def test_current_outcome_proof_produces_schema_valid_auditable_handoff(tmp_path: Path, monkeypatch):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)
    done = complete_campaign_task(repo, task)
    done["completion_evidence"]["release"] = proof(repo, task["id"], "release")
    done["release_receipt"] = {
        "schema": "go-workflow.release-receipt.v1",
        "status": "verified",
        "task_id": task["id"],
        "project": task["project"],
        "commit": "a" * 40,
        "tag": "v1.2.3",
        "evidence": [],
    }
    write(repo / ".go/tasks/done" / f"{task['id']}.json", done)
    monkeypatch.setattr(campaign_audit, "completion_findings", lambda *a, **k: [])

    audit = campaign_audit.audit_campaign_goal(repo, contract_path, persist=True)

    schema = json.loads((ROOT / "schemas/campaign-goal-audit.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(audit)
    assert audit["status"] == "achieved"
    assert audit["goal_verified"] is True
    assert audit["original_request"] == contract["intent"]
    assert audit["outcomes"][0]["status"] == "achieved"
    assert {item["evidence_class"] for item in audit["provenance"]} >= {
        "locally_verified", "release",
    }
    report = repo / audit["handoff"]["path"]
    text = report.read_text(encoding="utf-8")
    assert contract["intent"]["text"] in text
    assert "Locally verified" in text
    assert "Pending / blocked" in text
    assert "User decisions" in text


def test_missing_work_is_deduplicated_and_bounded_by_campaign_allowance(tmp_path: Path, monkeypatch):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)
    complete_campaign_task(repo, task)
    monkeypatch.setattr(campaign_audit, "completion_findings", lambda *a, **k: ["current proof failed"])
    contract["authority"]["expansion"]["repair"] = {
        "max_tasks": 2,
        "modify": ["src/**", "tests/**"],
        "outcome_ids": ["O1"],
    }
    write(contract_path, contract)

    audit = campaign_audit.audit_campaign_goal(repo, contract_path, persist=False)

    assert audit["status"] == "blocked"
    assert audit["follow_ups"] == [{
        "key": "repair:O1",
        "kind": "repair",
        "outcome_id": "O1",
        "modify": ["src/**", "tests/**"],
        "reason": "Campaign outcome O1 lacks current completion proof.",
        "source_ref": contract["intent"]["source_ref"],
    }]
    assert all("project-orchestration" not in json.dumps(item) for item in audit["follow_ups"])


def test_campaign_completion_uses_shared_audit_and_can_reach_goal_verified(tmp_path: Path, monkeypatch):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)
    complete_campaign_task(repo, task)
    monkeypatch.setattr(campaign_audit, "completion_findings", lambda *a, **k: [])
    args = Namespace(
        campaign=str(contract_path), previous_campaign="",
        campaign_workspace_root=str(tmp_path / "workspaces"), agent="fixture",
        max_commands=10, max_minutes=5, max_attempts=5,
    )

    code, result = execute_campaign(
        repo, args, "go-auto", __import__("go_workflow.cli", fromlist=["cli"]),
        lambda *a: (_ for _ in ()).throw(AssertionError("no task should be dispatched")),
    )

    assert code == 0
    assert result["status"] == "goal_verified"
    assert result["goal_verified"] is True
    assert result["completion_audit"]["status"] == "achieved"
    state = json.loads((repo / result["campaign_state"]).read_text(encoding="utf-8"))
    assert state["status"] == "goal_verified"
    assert state["stop"]["condition"] == "goal_verified"


def test_invalid_current_proof_blocks_instead_of_reinterpreting_done_history(tmp_path: Path, monkeypatch):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)
    complete_campaign_task(repo, task)
    monkeypatch.setattr(
        campaign_audit,
        "completion_findings",
        lambda *a, **k: ["lifecycle: current candidate does not match proof"],
    )

    audit = campaign_audit.audit_campaign_goal(repo, contract_path, persist=False)

    assert audit["status"] == "blocked"
    assert audit["outcomes"][0]["status"] == "blocked"
    assert "current candidate" in " ".join(audit["outcomes"][0]["findings"])


def test_invalid_campaign_identity_fails_before_writing_report(tmp_path: Path):
    repo, contract_path, contract, task = campaign_fixture(tmp_path)
    contract["id"] = "../../escape"
    write(contract_path, contract)

    with pytest.raises(ValueError, match="invalid campaign contract"):
        campaign_audit.audit_campaign_goal(repo, contract_path, persist=True)

    assert not (repo / ".go/escape").exists()
