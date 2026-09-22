"""Deterministic bounded-intake double for the autonomy campaign fixture."""
import json
import os
from pathlib import Path


request = json.loads(os.environ["GO_ADAPTER_REQUEST_JSON"])
capture = Path(os.environ["AUTONOMY_INTAKE_CAPTURE"])
with capture.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(request) + "\n")

outcomes = request["task"]["intake_request"]["outcomes"]
actions = []
for index, outcome in enumerate(outcomes, 1):
    task_id = "deliver-alpha" if index == 1 else "deliver-beta"
    artifact = "alpha.txt" if index == 1 else "beta.txt"
    actions.append({
        "action": "update",
        "task_id": task_id,
        "summary": f"Deliver campaign artifact {index}",
        "work_type": "implementation",
        "root_cause": "not_applicable",
        "substantial": True,
        "outcome_ids": [outcome["id"]],
        "scope": {"read": [".go/**", artifact, "VERSION", "CHANGELOG.md"],
                  "modify": [artifact, "VERSION", "CHANGELOG.md"]},
        "behavior": {
            "before": [f"{artifact} does not contain the requested value"],
            "after": [f"{artifact} contains the requested value and has release proof"],
            "states": ["open", "built", "reviewed", "released"],
            "edges": ["open -> built -> reviewed -> released"],
        },
        "non_goals": ["Hosted publication or a real deployment"],
        "dependencies": [],
        "delegated_choices": ["Use the existing local text fixture format"],
        "question_ids": [],
        "acceptance": [f"{artifact} has the requested content and immutable local release readback"],
        "verification": [f"test -f {artifact}"],
    })

if os.environ.get("AUTONOMY_OMIT_REQUIREMENT") == "1":
    actions = actions[:1]

assessment = {
    "schema": "go-workflow.intake-assessment.v1",
    "request_sha256": request["task"]["intake_request"]["sha256"],
    "summary": "Two independently releasable tasks preserve both rough-request outcomes.",
    "disposition": "ready",
    "actions": actions,
    "questions": [],
    "relevant_decisions": [{"id": "autonomy-fixture-policy", "status": "accepted"}],
}
print(json.dumps({
    "schema": "go-workflow.agent-adapter-result.v1",
    "phase": "critic",
    "status": "success",
    "summary": "Grounded two-task intake assessment complete.",
    "intake_assessment": assessment,
}))
