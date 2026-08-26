import json
import shutil
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from go_workflow.architecture import (
    validate_architecture_brief,
    validate_architecture_event,
    validate_task_architecture,
)
from go_workflow.cli import validate_repo, validate_task


def valid_task() -> dict:
    return {
        "schema": "go-workflow.repo-local.task.v1",
        "kind": "task",
        "id": "architecture-test",
        "project": "demo",
        "status": "open",
        "summary": "Exercise architecture metadata",
        "scope": {"read": ["**"], "modify": ["src/**"]},
        "acceptance": ["Architecture metadata validates"],
        "verification": ["pytest -q"],
        "claim": {"agent": None, "claimed_at": None},
    }


def valid_brief() -> dict:
    return {
        "schema": "go-workflow.architecture-brief.v1",
        "kind": "architecture_brief",
        "id": "project-memory",
        "project": "demo",
        "title": "Project memory boundary",
        "status": "accepted",
        "owner": "solution-architect",
        "outcomes": ["Traceable project state"],
        "scope": {
            "in": ["canonical project records"],
            "out": ["customer production data"],
            "systems": ["project-ledger"],
            "boundaries": ["project tenant"],
        },
        "stakeholders": [
            {
                "role": "data-owner",
                "concerns": ["retention"],
                "decision_rights": ["approve-real-data-processing"],
            }
        ],
        "constraints": ["Generated views are not canonical"],
        "quality_attributes": [
            {
                "id": "project-isolation",
                "scenario": "A user queries another project",
                "measure": "forbidden results",
                "threshold": 0,
            }
        ],
        "risks": ["Cross-project retrieval"],
        "open_questions": [],
        "decision_ids": ["canonical-project-state-v1"],
    }


def valid_event(event_name: str = "architecture.conformance.recorded") -> dict:
    return {
        "schema": "go-workflow.architecture-event.v1",
        "kind": "architecture_event",
        "event": event_name,
        "created_at": "2026-08-26T12:00:00+02:00",
        "task_id": "architecture-test",
        "scope_id": "project-memory",
        "actor": "architecture-critic",
        "data": {
            "status": "passed",
            "principle_checks": [],
            "decision_checks": [],
            "quality_attribute_checks": [],
            "evidence_refs": [".go/evidence/events.jsonl#proof"],
        },
    }


def test_task_architecture_metadata_is_optional_and_strict():
    legacy = valid_task()
    assert validate_task(legacy, "task.json") == []

    material = valid_task()
    material["architecture"] = {
        "impact": "material",
        "scope_refs": ["project-memory"],
        "concerns": ["data", "security"],
        "decision_ids": ["canonical-project-state-v1"],
        "conformance_required": True,
        "human_gate": "decision",
    }
    assert validate_task(material, "task.json") == []

    invalid = valid_task()
    invalid["architecture"] = {"impact": "huge"}
    errors = validate_task_architecture(invalid["architecture"], "task.json")
    assert any("impact" in error for error in errors)
    assert validate_task(invalid, "task.json")


def test_architecture_contract_schemas_and_runtime_validators_agree():
    brief = valid_brief()
    event = valid_event()
    brief_schema = json.loads((ROOT / "schemas" / "architecture-brief.schema.json").read_text())
    event_schema = json.loads((ROOT / "schemas" / "architecture-event.schema.json").read_text())

    Draft202012Validator.check_schema(brief_schema)
    Draft202012Validator.check_schema(event_schema)
    Draft202012Validator(brief_schema).validate(brief)
    Draft202012Validator(event_schema).validate(event)
    assert validate_architecture_brief(brief, "brief.json") == []
    assert validate_architecture_event(event, "events.jsonl", 1) == []


def test_validate_repo_accepts_optional_architecture_state_and_rejects_expired_waiver(tmp_path: Path):
    repo = tmp_path / "demo"
    shutil.copytree(ROOT / "fixtures" / "minimal", repo)
    architecture = repo / ".go" / "architecture"
    (architecture / "briefs").mkdir(parents=True)
    brief = valid_brief()
    brief["project"] = json.loads((repo / ".go" / "project.json").read_text(encoding="utf-8"))["id"]
    (architecture / "briefs" / "project-memory.json").write_text(
        json.dumps(brief, indent=2) + "\n", encoding="utf-8"
    )
    event_path = architecture / "events.jsonl"
    event_path.write_text(json.dumps(valid_event()) + "\n", encoding="utf-8")
    assert validate_repo(repo) == []

    waiver = valid_event("architecture.waiver.granted")
    waiver["data"] = {
        "waiver_id": "legacy-adapter-waiver",
        "status": "active",
        "reason": "Temporary migration boundary",
        "accepted_risk": "One legacy adapter is not isolated",
        "expires_at": "2020-01-01T00:00:00+00:00",
    }
    event_path.write_text(json.dumps(waiver) + "\n", encoding="utf-8")
    errors = validate_repo(repo)
    assert any("waiver has expired" in error for error in errors)

    closed = valid_event("architecture.waiver.expired")
    closed["data"] = {"waiver_id": "legacy-adapter-waiver", "status": "expired"}
    event_path.write_text(json.dumps(waiver) + "\n" + json.dumps(closed) + "\n", encoding="utf-8")
    assert validate_repo(repo) == []
