import json
import shutil
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from go_workflow.architecture import (
    architecture_claim_findings,
    architecture_finish_findings,
    effective_architecture_impact,
    validate_architecture_brief,
    validate_architecture_event,
    validate_task_architecture,
)
from go_workflow.cli import architecture_readback_payload, build_execution_context, validate_repo, validate_task


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


def test_execution_context_resolves_exact_old_decision_and_architecture_status(tmp_path: Path):
    repo = tmp_path / "demo"
    shutil.copytree(ROOT / "fixtures" / "minimal", repo)
    root = repo / ".go"
    project_id = json.loads((root / "project.json").read_text(encoding="utf-8"))["id"]
    architecture = root / "architecture"
    (architecture / "briefs").mkdir(parents=True)
    brief = valid_brief()
    brief["project"] = project_id
    (architecture / "briefs" / "project-memory.json").write_text(json.dumps(brief) + "\n", encoding="utf-8")

    decision_events = []
    for index in range(12):
        decision_id = "canonical-project-state-v1" if index == 0 else f"noise-{index}"
        decision_events.append({
            "schema": "go-workflow.repo-local.event.v1",
            "kind": "event",
            "event": "decision.recorded",
            "created_at": f"2026-08-26T12:{index:02d}:00+02:00",
            "task_id": "architecture-test",
            "agent": "hermes",
            "data": {
                "decision_id": decision_id,
                "title": decision_id,
                "status": "accepted",
                "context": "test",
                "decision": "test",
                "consequences": [],
            },
        })
    (root / "decisions").mkdir(parents=True, exist_ok=True)
    (root / "decisions" / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in decision_events), encoding="utf-8"
    )

    deviation = valid_event("architecture.deviation.recorded")
    deviation["data"] = {"deviation_id": "dev-1", "status": "open", "reason": "Pending adapter isolation"}
    waiver = valid_event("architecture.waiver.granted")
    waiver["data"] = {
        "waiver_id": "waiver-1",
        "status": "active",
        "reason": "Temporary adapter",
        "accepted_risk": "One adapter remains coupled",
        "expires_at": "2026-12-31T23:59:59+00:00",
    }
    (architecture / "events.jsonl").write_text(json.dumps(deviation) + "\n" + json.dumps(waiver) + "\n", encoding="utf-8")

    task = valid_task()
    task["project"] = project_id
    task["architecture"] = {
        "impact": "material",
        "scope_refs": ["project-memory"],
        "concerns": ["data"],
        "decision_ids": ["canonical-project-state-v1"],
        "conformance_required": True,
        "human_gate": "decision",
    }
    context = build_execution_context(repo, task)
    recent_ids = [(event.get("data") or {}).get("decision_id") for event in context["recent_decisions"]]
    applicable_ids = [(event.get("data") or {}).get("decision_id") for event in context["applicable_architecture"]["decisions"]]
    assert "canonical-project-state-v1" not in recent_ids
    assert applicable_ids == ["canonical-project-state-v1"]
    assert context["applicable_architecture"]["quality_attributes"][0]["id"] == "project-isolation"

    readback = architecture_readback_payload(repo, "")
    assert readback["status"]["briefs"] == {"total": 1, "accepted": 1, "draft": 0}
    assert readback["status"]["open_deviations"] == 1
    assert readback["status"]["active_waivers"] == 1


def test_architecture_gates_raise_hard_minimum_and_require_conformance_and_human_review(tmp_path: Path):
    repo = tmp_path / "demo"
    shutil.copytree(ROOT / "fixtures" / "minimal", repo)
    root = repo / ".go"
    project_id = json.loads((root / "project.json").read_text(encoding="utf-8"))["id"]

    legacy_task = valid_task()
    legacy_task["project"] = project_id
    legacy_task["summary"] = "Legacy API migration task"
    assert architecture_claim_findings(root, legacy_task) == []
    assert architecture_finish_findings(root, legacy_task) == []

    task = valid_task()
    task["project"] = project_id
    task["summary"] = "Add external integration and API contract"
    task["architecture"] = {
        "impact": "local",
        "scope_refs": [],
        "concerns": [],
        "decision_ids": [],
        "conformance_required": False,
        "human_gate": "none",
    }
    effective, minimum, signals = effective_architecture_impact(task)
    assert (effective, minimum) == ("material", "material")
    assert "integration" in signals
    assert any("must be raised" in finding for finding in architecture_claim_findings(root, task))

    architecture = root / "architecture"
    (architecture / "briefs").mkdir(parents=True)
    brief = valid_brief()
    brief["project"] = project_id
    (architecture / "briefs" / "project-memory.json").write_text(json.dumps(brief) + "\n", encoding="utf-8")
    (root / "decisions").mkdir(parents=True, exist_ok=True)
    decision = {
        "schema": "go-workflow.repo-local.event.v1",
        "kind": "event",
        "event": "decision.recorded",
        "created_at": "2026-08-26T12:00:00+02:00",
        "task_id": "architecture-test",
        "agent": "human-architect",
        "data": {
            "decision_id": "canonical-project-state-v1",
            "title": "Canonical state",
            "status": "accepted",
            "context": "test",
            "decision": "Use canonical project state",
            "consequences": [],
        },
    }
    (root / "decisions" / "events.jsonl").write_text(json.dumps(decision) + "\n", encoding="utf-8")
    task["architecture"] = {
        "impact": "material",
        "scope_refs": ["project-memory"],
        "concerns": ["integration", "data"],
        "decision_ids": ["canonical-project-state-v1"],
        "conformance_required": True,
        "human_gate": "decision",
    }
    assert architecture_claim_findings(root, task) == []
    initial_finish = architecture_finish_findings(root, task)
    assert any("conformance event required" in finding for finding in initial_finish)
    assert any("human architecture approval" in finding for finding in initial_finish)

    conformance = valid_event("architecture.conformance.recorded")
    conformance["data"] = {
        "status": "passed",
        "principle_checks": [{"id": "json-first", "status": "passed"}],
        "decision_checks": [{"id": "canonical-project-state-v1", "status": "passed"}],
        "quality_attribute_checks": [{"id": "project-isolation", "status": "passed", "evidence": "pytest"}],
        "evidence_refs": [".go/evidence/events.jsonl#proof"],
    }
    review = valid_event("architecture.reviewed")
    review["actor"] = "viggo"
    review["data"] = {"status": "approved", "human": True, "evidence_ref": "review:architecture-test"}
    (architecture / "events.jsonl").write_text(json.dumps(conformance) + "\n" + json.dumps(review) + "\n", encoding="utf-8")
    assert architecture_finish_findings(root, task) == []


def test_material_tasks_require_an_accepted_governing_decision_but_may_reuse_one_from_a_brief(tmp_path: Path):
    repo = tmp_path / "demo"
    shutil.copytree(ROOT / "fixtures" / "minimal", repo)
    root = repo / ".go"
    project_id = json.loads((root / "project.json").read_text(encoding="utf-8"))["id"]
    architecture = root / "architecture"
    (architecture / "briefs").mkdir(parents=True)

    brief = valid_brief()
    brief["project"] = project_id
    brief["decision_ids"] = []
    brief_path = architecture / "briefs" / "project-memory.json"
    brief_path.write_text(json.dumps(brief) + "\n", encoding="utf-8")

    task = valid_task()
    task["project"] = project_id
    task["architecture"] = {
        "impact": "material",
        "scope_refs": ["project-memory"],
        "concerns": ["data"],
        "decision_ids": [],
        "conformance_required": True,
        "human_gate": "none",
    }

    findings = architecture_claim_findings(root, task)
    assert any("requires at least one accepted governing architecture decision" in finding for finding in findings)

    brief["decision_ids"] = ["canonical-project-state-v1"]
    brief_path.write_text(json.dumps(brief) + "\n", encoding="utf-8")
    findings = architecture_claim_findings(root, task)
    assert any("missing architecture decisions: canonical-project-state-v1" in finding for finding in findings)

    decision = {
        "schema": "go-workflow.repo-local.event.v1",
        "kind": "event",
        "event": "decision.recorded",
        "created_at": "2026-08-27T09:00:00+02:00",
        "task_id": "architecture-test",
        "agent": "human-architect",
        "data": {
            "decision_id": "canonical-project-state-v1",
            "title": "Canonical project state",
            "status": "proposed",
            "context": "test",
            "decision": "Use canonical project state",
            "consequences": [],
        },
    }
    (root / "decisions").mkdir(parents=True, exist_ok=True)
    decision_path = root / "decisions" / "events.jsonl"
    decision_path.write_text(json.dumps(decision) + "\n", encoding="utf-8")
    findings = architecture_claim_findings(root, task)
    assert any("architecture decisions must be accepted: canonical-project-state-v1" in finding for finding in findings)

    decision["data"]["status"] = "accepted"
    decision_path.write_text(json.dumps(decision) + "\n", encoding="utf-8")
    assert architecture_claim_findings(root, task) == []

    conformance = valid_event("architecture.conformance.recorded")
    conformance["data"] = {
        "status": "passed",
        "principle_checks": [],
        "decision_checks": [],
        "quality_attribute_checks": [{"id": "project-isolation", "status": "passed", "evidence": "pytest"}],
        "evidence_refs": [".go/evidence/events.jsonl#proof"],
    }
    (architecture / "events.jsonl").write_text(json.dumps(conformance) + "\n", encoding="utf-8")
    findings = architecture_finish_findings(root, task)
    assert any("conformance missing passing decision checks for scope project-memory: canonical-project-state-v1" in finding for finding in findings)

    conformance["data"]["decision_checks"] = [{"id": "canonical-project-state-v1", "status": "passed"}]
    (architecture / "events.jsonl").write_text(json.dumps(conformance) + "\n", encoding="utf-8")
    assert architecture_finish_findings(root, task) == []

    task["architecture"]["impact"] = "local"
    task["architecture"]["scope_refs"] = []
    task["architecture"]["decision_ids"] = []
    assert architecture_claim_findings(root, task) == []


def test_finish_conformance_checks_each_briefs_decisions_and_quality_attributes_in_its_own_scope(tmp_path: Path):
    repo = tmp_path / "demo"
    shutil.copytree(ROOT / "fixtures" / "minimal", repo)
    root = repo / ".go"
    project_id = json.loads((root / "project.json").read_text(encoding="utf-8"))["id"]
    architecture = root / "architecture"
    (architecture / "briefs").mkdir(parents=True)

    memory_brief = valid_brief()
    memory_brief["project"] = project_id
    integration_brief = valid_brief()
    integration_brief["project"] = project_id
    integration_brief["id"] = "integration-boundary"
    integration_brief["title"] = "Integration boundary"
    integration_brief["decision_ids"] = ["integration-contract-v1"]
    integration_brief["quality_attributes"] = [
        {
            "id": "integration-latency",
            "scenario": "A dependency responds normally",
            "measure": "p95 milliseconds",
            "threshold": 500,
        }
    ]
    (architecture / "briefs" / "project-memory.json").write_text(json.dumps(memory_brief) + "\n", encoding="utf-8")
    (architecture / "briefs" / "integration-boundary.json").write_text(json.dumps(integration_brief) + "\n", encoding="utf-8")

    decisions = []
    for decision_id in ("canonical-project-state-v1", "integration-contract-v1"):
        decisions.append({
            "schema": "go-workflow.repo-local.event.v1",
            "kind": "event",
            "event": "decision.recorded",
            "created_at": "2026-08-27T09:00:00+02:00",
            "task_id": "architecture-test",
            "agent": "human-architect",
            "data": {
                "decision_id": decision_id,
                "title": decision_id,
                "status": "accepted",
                "context": "test",
                "decision": "test",
                "consequences": [],
            },
        })
    (root / "decisions").mkdir(parents=True, exist_ok=True)
    (root / "decisions" / "events.jsonl").write_text(
        "".join(json.dumps(decision) + "\n" for decision in decisions), encoding="utf-8"
    )

    task = valid_task()
    task["project"] = project_id
    task["architecture"] = {
        "impact": "material",
        "scope_refs": ["project-memory", "integration-boundary"],
        "concerns": ["data", "integration"],
        "decision_ids": [],
        "conformance_required": True,
        "human_gate": "none",
    }

    memory_conformance = valid_event("architecture.conformance.recorded")
    memory_conformance["data"] = {
        "status": "passed",
        "principle_checks": [],
        "decision_checks": [{"id": "canonical-project-state-v1", "status": "passed"}],
        "quality_attribute_checks": [{"id": "project-isolation", "status": "passed", "evidence": "pytest"}],
        "evidence_refs": ["evidence:memory"],
    }
    integration_conformance = valid_event("architecture.conformance.recorded")
    integration_conformance["scope_id"] = "integration-boundary"
    integration_conformance["data"] = {
        "status": "passed",
        "principle_checks": [],
        "decision_checks": [{"id": "integration-contract-v1", "status": "passed"}],
        "quality_attribute_checks": [{"id": "integration-latency", "status": "passed", "evidence": "pytest"}],
        "evidence_refs": ["evidence:integration"],
    }
    (architecture / "events.jsonl").write_text(
        json.dumps(memory_conformance) + "\n" + json.dumps(integration_conformance) + "\n",
        encoding="utf-8",
    )

    assert architecture_claim_findings(root, task) == []
    assert architecture_finish_findings(root, task) == []
