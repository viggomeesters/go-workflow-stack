"""Bounded, model-assessed intake without pretending structure is semantics."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .execution_contracts import validate_dependencies


REQUEST_SCHEMA = "go-workflow.intake-request.v1"
ASSESSMENT_SCHEMA = "go-workflow.intake-assessment.v1"
RECORD_SCHEMA = "go-workflow.intake-record.v1"
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LIST_ITEM = re.compile(r"^\s*(?:\d+(?:[.)])?|[-*+])\s+(.+?)\s*$")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _objects(path: Path) -> list[dict[str, Any]]:
    values = []
    if not path.is_dir():
        return values
    for item in sorted(path.glob("*.json")):
        try:
            value = json.loads(item.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _latest_decisions(root: Path) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    path = root / "decisions/events.jsonl"
    if not path.is_file():
        return []
    for line in path.read_text().splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        data = value.get("data") if isinstance(value, dict) else None
        if value.get("event") == "decision.recorded" and isinstance(data, dict) and isinstance(data.get("decision_id"), str):
            latest[data["decision_id"]] = {key: data.get(key) for key in ("decision_id", "title", "status")}
    return [latest[key] for key in sorted(latest)]


def _outcomes(text: str, source_ref: str) -> list[dict[str, str]]:
    items: list[str] = []
    current: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = LIST_ITEM.match(line)
        if match:
            if current:
                items.append(" ".join(current))
            current = [match.group(1).strip()]
        elif current is not None:
            current.append(line)
    if current:
        items.append(" ".join(current))
    values = items or [text]
    return [{"id": f"O{index}", "text": value, "source_ref": source_ref}
            for index, value in enumerate(values, 1)]


def prepare_request(repo: Path, intent: str, source_ref: str, authority: str) -> dict[str, Any]:
    repo = repo.resolve()
    root = repo / ".go"
    text = intent.strip()
    if not text:
        raise ValueError("intake intent must be non-empty")
    source_ref = source_ref.strip()
    if not source_ref:
        raise ValueError("intake source_ref must be non-empty")
    if authority not in {"advice", "planning", "execute"}:
        raise ValueError("intake authority must be advice, planning, or execute")
    tasks = []
    for state in ("open", "active", "blocked", "done"):
        for task in _objects(root / "tasks" / state):
            tasks.append({key: task.get(key) for key in
                          ("id", "status", "summary", "intent_source", "requested_outcomes", "review_status")})
    basis = {
        "project": json.loads((root / "project.json").read_text()),
        "vision_sha256": hashlib.sha256((root / "vision.json").read_bytes()).hexdigest(),
        "principles_sha256": hashlib.sha256((root / "architecture-principles.json").read_bytes()).hexdigest(),
        "tasks": sorted(tasks, key=lambda item: str(item.get("id"))),
        "decisions": _latest_decisions(root),
    }
    core = {
        "schema": REQUEST_SCHEMA,
        "project": basis["project"]["id"],
        "intent": {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(), "source_ref": source_ref},
        "authority": {"mode": authority, "source_ref": source_ref,
                      "implementation_authorized": authority == "execute"},
        "outcomes": _outcomes(text, source_ref),
        "repository": basis,
    }
    identity = canonical_digest({key: core[key] for key in ("schema", "project", "intent", "authority", "outcomes")})
    return {**core, "id": identity, "sha256": canonical_digest(core)}


def validate_assessment(value: Any, request: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["assessment must be an object"]
    required = {"schema", "request_sha256", "summary", "disposition", "actions", "questions", "relevant_decisions"}
    if set(value) != required:
        errors.append("assessment has missing or unknown fields")
    if value.get("schema") != ASSESSMENT_SCHEMA:
        errors.append("assessment schema mismatch")
    if value.get("request_sha256") != request.get("sha256"):
        errors.append("assessment request binding mismatch")
    if not isinstance(value.get("summary"), str) or not value.get("summary", "").strip():
        errors.append("assessment summary required")
    if value.get("disposition") not in {"ready", "questions", "research_first"}:
        errors.append("assessment disposition invalid")
    actions = value.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("assessment actions must be non-empty")
        actions = []
    outcome_ids = {item["id"] for item in request.get("outcomes", [])}
    seen = set()
    for action in actions:
        if not isinstance(action, dict):
            errors.append("assessment action must be an object")
            continue
        required_action = {"action", "task_id", "summary", "work_type", "root_cause", "substantial",
                           "outcome_ids", "scope", "behavior", "non_goals", "dependencies",
                           "delegated_choices", "question_ids", "acceptance", "verification"}
        if set(action) != required_action:
            errors.append("assessment action has missing or unknown fields")
        task_id = action.get("task_id")
        if not isinstance(task_id, str) or not ID.fullmatch(task_id):
            errors.append("assessment task_id invalid")
        elif task_id in seen:
            errors.append("assessment task_id duplicated: " + task_id)
        seen.add(task_id)
        if action.get("action") not in {"create", "reuse", "update"}:
            errors.append("assessment action kind invalid")
        if action.get("work_type") not in {"implementation", "bug_fix", "research"}:
            errors.append("assessment work_type invalid")
        if action.get("root_cause") not in {"known", "unknown", "not_applicable"}:
            errors.append("assessment root_cause invalid")
        if action.get("root_cause") == "unknown" and action.get("work_type") != "research":
            errors.append("unknown bug root cause must materialize research, not a fix")
        linked = action.get("outcome_ids")
        if not isinstance(linked, list) or not linked or not set(linked).issubset(outcome_ids):
            errors.append("assessment action outcome_ids invalid")
        for key in ("non_goals", "dependencies", "delegated_choices", "question_ids", "acceptance", "verification"):
            if not isinstance(action.get(key), list):
                errors.append(f"assessment action {key} must be a list")
        errors.extend(validate_dependencies(action.get("dependencies")))
        scope = action.get("scope")
        if not isinstance(scope, dict) or set(scope) != {"read", "modify"} or not all(isinstance(scope.get(k), list) for k in ("read", "modify")):
            errors.append("assessment action scope invalid")
        behavior = action.get("behavior")
        if action.get("substantial") is True:
            if (not isinstance(behavior, dict) or set(behavior) != {"before", "after", "states", "edges"}
                    or any(not isinstance(behavior.get(k), list) or not behavior[k] for k in behavior)):
                errors.append("substantial action requires observable behavior, states and edges")
            if not action.get("non_goals") or not action.get("acceptance") or not action.get("verification"):
                errors.append("substantial action requires non-goals, acceptance and verification")
    covered_outcomes = {
        outcome_id
        for action in actions if isinstance(action, dict)
        for outcome_id in action.get("outcome_ids", []) if isinstance(action.get("outcome_ids"), list)
    }
    for outcome_id in sorted(outcome_ids - covered_outcomes):
        errors.append("assessment does not preserve requested outcome: " + outcome_id)
    if not isinstance(value.get("questions"), list) or not isinstance(value.get("relevant_decisions"), list):
        errors.append("assessment questions and relevant_decisions must be lists")
    else:
        question_ids = set()
        for question in value["questions"]:
            if (not isinstance(question, dict) or set(question) != {"id", "text", "owner", "blocks"}
                    or not isinstance(question.get("id"), str) or not ID.fullmatch(question["id"])
                    or not isinstance(question.get("text"), str) or not question["text"].strip()
                    or not isinstance(question.get("owner"), str) or not question["owner"].strip()
                    or not isinstance(question.get("blocks"), list)):
                errors.append("intake question invalid")
                continue
            if question["id"] in question_ids:
                errors.append("intake question duplicated: " + question["id"])
            question_ids.add(question["id"])
        for action in actions:
            if isinstance(action, dict):
                for question_id in action.get("question_ids", []) if isinstance(action.get("question_ids"), list) else []:
                    if question_id not in question_ids:
                        errors.append(f"assessment action references unknown question: {question_id}")
        action_ids = {action.get("task_id") for action in actions if isinstance(action, dict)}
        question_map = {question.get("id"): question for question in value["questions"] if isinstance(question, dict)}
        for question_id, question in question_map.items():
            blocks = question.get("blocks", [])
            for task_id in blocks if isinstance(blocks, list) else []:
                if task_id not in action_ids:
                    errors.append(f"intake question {question_id} blocks unknown action: {task_id}")
        for action in actions:
            if not isinstance(action, dict):
                continue
            for question_id in action.get("question_ids", []) if isinstance(action.get("question_ids"), list) else []:
                if action.get("task_id") not in question_map.get(question_id, {}).get("blocks", []):
                    errors.append(f"intake question {question_id} does not block action {action.get('task_id')}")
        current_decisions = {item.get("decision_id"): item.get("status")
                             for item in request.get("repository", {}).get("decisions", [])}
        for decision in value["relevant_decisions"]:
            if not isinstance(decision, dict) or set(decision) != {"id", "status"}:
                errors.append("relevant decision reference invalid")
                continue
            current = current_decisions.get(decision["id"])
            if current is None:
                errors.append("relevant decision is missing: " + str(decision["id"]))
            elif current != decision["status"]:
                errors.append(f"relevant decision {decision['id']} is {current}, not {decision['status']}")
    return errors


def record_path(repo: Path, request: dict[str, Any]) -> Path:
    return repo / ".go/intake" / (request["id"] + ".json")


def validate_record(value: Any, project_id: str, task_ids: set[str]) -> list[str]:
    if not isinstance(value, dict):
        return ["intake record must be an object"]
    errors: list[str] = []
    required = {"schema", "id", "status", "request", "assessment", "task_ids", "authority", "adapter"}
    if set(value) != required:
        errors.append("intake record has missing or unknown fields")
    if value.get("schema") != RECORD_SCHEMA or value.get("status") != "applied":
        errors.append("intake record schema/status invalid")
    request = value.get("request")
    if not isinstance(request, dict):
        return errors + ["intake request must be an object"]
    core = {key: item for key, item in request.items() if key not in {"id", "sha256"}}
    identity = canonical_digest({key: core.get(key) for key in ("schema", "project", "intent", "authority", "outcomes")})
    if request.get("id") != identity or value.get("id") != identity:
        errors.append("intake identity digest mismatch")
    if request.get("sha256") != canonical_digest(core):
        errors.append("intake request digest mismatch")
    if request.get("project") != project_id:
        errors.append("intake project mismatch")
    errors.extend(validate_assessment(value.get("assessment"), request))
    linked = value.get("task_ids")
    if not isinstance(linked, list) or not linked or any(task_id not in task_ids for task_id in linked):
        errors.append("intake task links are missing or invalid")
    if value.get("authority") != request.get("authority"):
        errors.append("intake authority drift")
    if not isinstance(value.get("adapter"), dict):
        errors.append("intake adapter evidence must be an object")
    return errors
