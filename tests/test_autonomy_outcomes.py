"""Behavioral outcome review stays grounded in fresh, requirement-bound proof."""

import hashlib
import json
import shlex
import sys
from pathlib import Path

import pytest

from test_abc_completion import fixture


ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def outcome_task(task):
    task.update(
        outcome_tracking_version=1,
        behavior_review_version=1,
        requested_outcomes=[
            {
                "id": "R1",
                "text": "The delivered app shows the corrected behavior",
                "source": "user",
                "status": "pending",
                "evidence": [],
            }
        ],
    )
    return task


def grounded_review(repo, task, contract, *, status="passed", evidence=None, repairs=None):
    from go_workflow.completion import bind

    return {
        "schema": "go-workflow.critic-review.v1",
        **bind(repo, task),
        "status": status,
        "reviewer": "independent-fixture",
        "review_mode": "independent",
        "summary": "Inspected the delivered candidate against the original outcome.",
        "blocking_findings": [] if status == "passed" else ["R1 behavior remains unproven"],
        "reviewed_paths": ["app.txt", ".go/project.json"],
        "behavior_review": {
            "schema": "go-workflow.behavior-review.v1",
            "task_id": task["id"],
            "context_digest": contract["context_digest"],
            "candidate_digest": contract["candidate_digest"],
            "acceptance_digest": contract["acceptance_digest"],
            "status": status,
            "outcomes": [
                {
                    "requirement_id": "R1",
                    "status": status,
                    "rationale": "The current app.txt bytes and executed result directly establish R1.",
                    "evidence": evidence or [],
                }
            ],
            "repairs": repairs or [],
            "publication_pending": True,
        },
    }


def test_fresh_critic_context_carries_exact_goals_architecture_and_original_outcomes(tmp_path):
    from go_workflow.cli import build_execution_context

    repo, source, task = fixture(tmp_path)
    task = outcome_task(task)
    task["architecture"] = {"impact": "none", "scope_refs": [], "decision_ids": []}
    source.write_text(json.dumps(task))

    context = build_execution_context(repo, task, phase="critic")
    review = context["behavior_review"]
    assert review["original_outcomes"] == [
        {"id": "R1", "text": task["requested_outcomes"][0]["text"], "source": "user"}
    ]
    assert review["product_goals"]
    assert all(item["text"] and item["source"] and item["applicability"] for item in review["product_goals"])
    assert review["architecture_refs"] == [], "tiny unrelated changes must not inherit architecture ceremony"
    assert review["evidence_policy"]["media"] == "only_when_required_by_the_behavior"
    assert review["read_only"] is True and review["fresh"] is True


def test_material_context_explains_each_carried_architecture_reference(tmp_path):
    from go_workflow.cli import build_execution_context

    repo, source, task = fixture(tmp_path)
    task = outcome_task(task)
    task["architecture"] = {"impact": "material", "scope_refs": [], "decision_ids": [], "conformance_required": True}
    source.write_text(json.dumps(task))
    review = build_execution_context(repo, task, phase="critic")["behavior_review"]
    assert review["architecture_refs"]
    assert all(item["id"] and item["source"] and item["applicability"] for item in review["architecture_refs"])


@pytest.mark.parametrize("case_id", ["preparation-only", "stale-capture", "unrelated-green", "unreproduced-claim"])
def test_unsupported_behavioral_completion_examples_are_rejected(tmp_path, case_id):
    from go_workflow.cli import build_execution_context
    from go_workflow.task_design import validate_behavior_review

    repo, source, task = fixture(tmp_path)
    task = outcome_task(task)
    source.write_text(json.dumps(task))
    contract = build_execution_context(repo, task, phase="critic")["behavior_review"]
    cases = {item["id"]: item for item in json.loads((ROOT / "fixtures/autonomy-outcomes/reviews.json").read_text())["cases"]}
    review = grounded_review(repo, task, contract, evidence=cases[case_id]["evidence"])["behavior_review"]
    assert validate_behavior_review(repo, task, contract, review), case_id


def test_grounded_current_behavior_proof_is_accepted_and_binds_every_identity(tmp_path):
    from go_workflow.cli import build_execution_context
    from go_workflow.task_design import validate_behavior_review

    repo, source, task = fixture(tmp_path)
    task = outcome_task(task)
    source.write_text(json.dumps(task))
    contract = build_execution_context(repo, task, phase="critic")["behavior_review"]
    evidence = [{
        "kind": "source",
        "role": "behavior_proof",
        "path": "app.txt",
        "sha256": sha(repo / "app.txt"),
        "task_id": task["id"],
        "requirement_id": "R1",
        "candidate_digest": contract["candidate_digest"],
        "context_digest": contract["context_digest"],
        "observation": "The inspected bytes are the delivered app behavior fixture.",
    }]
    review = grounded_review(repo, task, contract, evidence=evidence)["behavior_review"]
    assert validate_behavior_review(repo, task, contract, review) == []
    (repo / "app.txt").write_text("changed after review")
    assert any("stale" in item or "candidate" in item for item in validate_behavior_review(repo, task, contract, review))


def test_blocked_review_requires_bounded_in_scope_repair_without_acceptance_rewrite(tmp_path):
    from go_workflow.cli import build_execution_context
    from go_workflow.task_design import validate_behavior_review

    repo, source, task = fixture(tmp_path)
    task = outcome_task(task)
    task["scope"] = {"read": ["**"], "modify": ["app.txt"]}
    source.write_text(json.dumps(task))
    contract = build_execution_context(repo, task, phase="critic")["behavior_review"]
    repair = {
        "requirement_id": "R1",
        "action": "Reproduce the reported transition, correct app.txt, and rerun the declared behavior check.",
        "paths": ["app.txt"],
        "verification": [task["verification"][0]],
        "acceptance_digest": contract["acceptance_digest"],
    }
    review = grounded_review(repo, task, contract, status="blocked", repairs=[repair])["behavior_review"]
    assert validate_behavior_review(repo, task, contract, review) == []
    review["repairs"][0]["paths"] = ["outside.txt"]
    assert any("scope" in item for item in validate_behavior_review(repo, task, contract, review))
    review["repairs"][0]["paths"] = ["app.txt"]
    review["repairs"][0]["acceptance_digest"] = "0" * 64
    assert any("acceptance" in item for item in validate_behavior_review(repo, task, contract, review))


def test_record_critic_rejects_ungrounded_outcome_review(tmp_path):
    from go_workflow.cli import build_execution_context
    from go_workflow.completion import CompletionError, record_critic

    repo, source, task = fixture(tmp_path)
    task = outcome_task(task)
    source.write_text(json.dumps(task))
    contract = build_execution_context(repo, task, phase="critic")["behavior_review"]
    review = grounded_review(repo, task, contract, evidence=[])
    with pytest.raises(CompletionError, match="behavior"):
        record_critic(repo, task["id"], "owner", review)


def test_grounded_review_matches_published_schema(tmp_path):
    from jsonschema import Draft202012Validator
    from go_workflow.cli import build_execution_context

    repo, source, task = fixture(tmp_path)
    task = outcome_task(task)
    source.write_text(json.dumps(task))
    contract = build_execution_context(repo, task, phase="critic")["behavior_review"]
    evidence = [{
        "kind": "runtime_observation", "role": "behavior_proof", "path": "app.txt",
        "sha256": sha(repo / "app.txt"), "task_id": task["id"], "requirement_id": "R1",
        "candidate_digest": contract["candidate_digest"], "context_digest": contract["context_digest"],
        "observation": "Observed the current fixture candidate.",
    }]
    review = grounded_review(repo, task, contract, evidence=evidence)["behavior_review"]
    schema = json.loads((ROOT / "schemas/behavior-review.schema.json").read_text())
    assert list(Draft202012Validator(schema).iter_errors(review)) == []


def test_native_critic_cannot_report_success_without_grounded_review(tmp_path):
    from go_workflow.cli import run_hook_command

    repo, source, task = fixture(tmp_path)
    task = outcome_task(task)
    task.pop("execution_contract")
    source.write_text(json.dumps(task))
    payload = json.dumps({
        "schema": "go-workflow.agent-adapter-result.v1", "phase": "critic",
        "status": "success", "summary": "Everything looks fine",
    })
    command = shlex.join([sys.executable, "-c", f"print({payload!r})"])
    result = run_hook_command(repo, command, task, 1, "direct_fix", "critic", require_protocol=False)
    assert result["status"] == "failure", result.get("summary")
    assert "behavior_review" in result["summary"]
