"""Architecture contracts for the optional repo-local Go architecture lane."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ARCHITECTURE_BRIEF_SCHEMA = "go-workflow.architecture-brief.v1"
ARCHITECTURE_EVENT_SCHEMA = "go-workflow.architecture-event.v1"
ARCHITECTURE_IMPACTS = {"none", "local", "material", "foundational"}
HUMAN_GATES = {"none", "decision", "risk_acceptance"}
ARCHITECTURE_EVENTS = {
    "architecture.classified",
    "architecture.reviewed",
    "architecture.conformance.recorded",
    "architecture.deviation.recorded",
    "architecture.waiver.granted",
    "architecture.waiver.expired",
    "architecture.waiver.revoked",
}
CONFORMANCE_STATUSES = {"passed", "passed_with_waiver", "deviation", "blocked"}


def _require(condition: bool, errors: list[str], message: str) -> None:
    if not condition:
        errors.append(message)


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and bool(item.strip()) for item in value)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def validate_task_architecture(value: Any, rel: str) -> list[str]:
    """Validate optional task-local activation metadata without requiring it on legacy tasks."""
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{rel}: architecture must be an object"]
    allowed = {
        "impact",
        "scope_refs",
        "concerns",
        "decision_ids",
        "conformance_required",
        "human_gate",
        "classification",
    }
    unknown = sorted(set(value) - allowed)
    _require(not unknown, errors, f"{rel}: architecture contains unknown properties: {', '.join(unknown)}")
    _require(value.get("impact") in ARCHITECTURE_IMPACTS, errors, f"{rel}: architecture.impact must be one of {', '.join(sorted(ARCHITECTURE_IMPACTS))}")
    for key in ("scope_refs", "concerns", "decision_ids"):
        _require(_string_list(value.get(key, [])), errors, f"{rel}: architecture.{key} must be a list of non-empty strings")
    _require(isinstance(value.get("conformance_required", False), bool), errors, f"{rel}: architecture.conformance_required must be boolean")
    _require(value.get("human_gate", "none") in HUMAN_GATES, errors, f"{rel}: architecture.human_gate must be one of {', '.join(sorted(HUMAN_GATES))}")
    classification = value.get("classification")
    if classification is not None:
        _require(isinstance(classification, dict), errors, f"{rel}: architecture.classification must be an object")
        if isinstance(classification, dict):
            _require(bool(str(classification.get("reason") or "").strip()), errors, f"{rel}: architecture.classification.reason required")
            _require(bool(str(classification.get("actor") or "").strip()), errors, f"{rel}: architecture.classification.actor required")
    return errors


def validate_architecture_brief(data: Any, rel: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"{rel}: architecture brief must be an object"]
    _require(data.get("schema") == ARCHITECTURE_BRIEF_SCHEMA, errors, f"{rel}: schema mismatch")
    _require(data.get("kind") == "architecture_brief", errors, f"{rel}: kind must be architecture_brief")
    for key in ("id", "project", "title", "owner"):
        _require(bool(str(data.get(key) or "").strip()), errors, f"{rel}: {key} required")
    _require(data.get("status") in {"draft", "accepted", "superseded", "archived"}, errors, f"{rel}: invalid status")
    for key in ("outcomes", "constraints", "risks", "open_questions", "decision_ids"):
        _require(_string_list(data.get(key)), errors, f"{rel}: {key} must be a list of non-empty strings")
    scope = data.get("scope")
    _require(isinstance(scope, dict), errors, f"{rel}: scope must be an object")
    if isinstance(scope, dict):
        for key in ("in", "out", "systems", "boundaries"):
            _require(_string_list(scope.get(key)), errors, f"{rel}: scope.{key} must be a list of non-empty strings")
    stakeholders = data.get("stakeholders")
    _require(isinstance(stakeholders, list), errors, f"{rel}: stakeholders must be a list")
    if isinstance(stakeholders, list):
        for index, stakeholder in enumerate(stakeholders, start=1):
            prefix = f"{rel}: stakeholder {index}"
            _require(isinstance(stakeholder, dict), errors, f"{prefix} must be an object")
            if isinstance(stakeholder, dict):
                _require(bool(str(stakeholder.get("role") or "").strip()), errors, f"{prefix} role required")
                for key in ("concerns", "decision_rights"):
                    _require(_string_list(stakeholder.get(key)), errors, f"{prefix} {key} must be a list of non-empty strings")
    quality_attributes = data.get("quality_attributes")
    _require(isinstance(quality_attributes, list), errors, f"{rel}: quality_attributes must be a list")
    if isinstance(quality_attributes, list):
        for index, attribute in enumerate(quality_attributes, start=1):
            prefix = f"{rel}: quality attribute {index}"
            _require(isinstance(attribute, dict), errors, f"{prefix} must be an object")
            if isinstance(attribute, dict):
                for key in ("id", "scenario", "measure"):
                    _require(bool(str(attribute.get(key) or "").strip()), errors, f"{prefix} {key} required")
                _require("threshold" in attribute, errors, f"{prefix} threshold required")
    return errors


def validate_architecture_event(data: Any, rel: str, line_number: int, *, now: datetime | None = None) -> list[str]:
    prefix = f"{rel}:{line_number}"
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"{prefix}: architecture event must be an object"]
    _require(data.get("schema") == ARCHITECTURE_EVENT_SCHEMA, errors, f"{prefix}: schema mismatch")
    _require(data.get("kind") == "architecture_event", errors, f"{prefix}: kind must be architecture_event")
    event_name = data.get("event")
    _require(event_name in ARCHITECTURE_EVENTS, errors, f"{prefix}: invalid event")
    for key in ("created_at", "task_id", "scope_id", "actor"):
        _require(bool(str(data.get(key) or "").strip()), errors, f"{prefix}: {key} required")
    _require(_parse_datetime(data.get("created_at")) is not None, errors, f"{prefix}: created_at must be an ISO datetime with timezone")
    payload = data.get("data")
    _require(isinstance(payload, dict), errors, f"{prefix}: data must be an object")
    if not isinstance(payload, dict):
        return errors
    if event_name == "architecture.conformance.recorded":
        _require(payload.get("status") in CONFORMANCE_STATUSES, errors, f"{prefix}: conformance status invalid")
        for key in ("principle_checks", "decision_checks", "quality_attribute_checks", "evidence_refs"):
            _require(isinstance(payload.get(key), list), errors, f"{prefix}: data.{key} must be a list")
    if event_name == "architecture.waiver.granted":
        for key in ("waiver_id", "reason", "accepted_risk", "expires_at"):
            _require(bool(str(payload.get(key) or "").strip()), errors, f"{prefix}: waiver data.{key} required")
        _require(payload.get("status") == "active", errors, f"{prefix}: granted waiver status must be active")
        expires_at = _parse_datetime(payload.get("expires_at"))
        _require(expires_at is not None, errors, f"{prefix}: waiver expires_at must be an ISO datetime with timezone")
    if event_name in {"architecture.waiver.expired", "architecture.waiver.revoked"}:
        _require(bool(str(payload.get("waiver_id") or "").strip()), errors, f"{prefix}: waiver data.waiver_id required")
    return errors


def validate_architecture_state(root: Path, project_id: str) -> list[str]:
    """Validate architecture state when present; absence is backward-compatible."""
    architecture_root = root / "architecture"
    if not architecture_root.exists():
        return []
    errors: list[str] = []
    brief_ids: set[str] = set()
    for path in sorted((architecture_root / "briefs").glob("*.json")):
        rel = str(path.relative_to(root.parent))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{rel}: invalid JSON: {exc}")
            continue
        errors.extend(validate_architecture_brief(data, rel))
        if isinstance(data, dict):
            brief_id = str(data.get("id") or "")
            if brief_id in brief_ids:
                errors.append(f"{rel}: duplicate architecture brief id {brief_id}")
            brief_ids.add(brief_id)
            if project_id and data.get("project") != project_id:
                errors.append(f"{rel}: project {data.get('project')!r} does not match project.json id {project_id!r}")
    events_path = architecture_root / "events.jsonl"
    waiver_state: dict[str, tuple[int, dict[str, Any]]] = {}
    if events_path.is_file():
        for index, line in enumerate(events_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{events_path.relative_to(root.parent)}:{index}: invalid JSONL: {exc}")
                continue
            errors.extend(validate_architecture_event(event, str(events_path.relative_to(root.parent)), index))
            if isinstance(event, dict) and str(event.get("event") or "").startswith("architecture.waiver."):
                payload = event.get("data")
                waiver_id = str(payload.get("waiver_id") or "") if isinstance(payload, dict) else ""
                if waiver_id:
                    waiver_state[waiver_id] = (index, event)
    current = datetime.now(timezone.utc)
    for waiver_id, (line_number, waiver_event) in waiver_state.items():
        if waiver_event.get("event") != "architecture.waiver.granted":
            continue
        payload = waiver_event.get("data") or {}
        expires_at = _parse_datetime(payload.get("expires_at"))
        if expires_at is not None and expires_at <= current:
            errors.append(f"{events_path.relative_to(root.parent)}:{line_number}: waiver has expired: {waiver_id}")
    return errors
