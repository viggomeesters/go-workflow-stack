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


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def architecture_briefs(root: Path) -> dict[str, dict[str, Any]]:
    briefs: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "architecture" / "briefs").glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("id"):
            briefs[str(item["id"])] = item
    return briefs


def latest_decisions(root: Path) -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    for decision_event in _jsonl(root / "decisions" / "events.jsonl"):
        if decision_event.get("event") != "decision.recorded":
            continue
        payload = decision_event.get("data")
        decision_id = str(payload.get("decision_id") or "") if isinstance(payload, dict) else ""
        if decision_id:
            decisions[decision_id] = decision_event
    return decisions


def architecture_status(root: Path) -> dict[str, Any]:
    briefs = architecture_briefs(root)
    events = _jsonl(root / "architecture" / "events.jsonl")
    deviations: dict[str, dict[str, Any]] = {}
    waivers: dict[str, dict[str, Any]] = {}
    for architecture_event in events:
        payload = architecture_event.get("data") if isinstance(architecture_event.get("data"), dict) else {}
        if architecture_event.get("event") == "architecture.deviation.recorded":
            deviations[str(payload.get("deviation_id") or f"{architecture_event.get('task_id')}:{architecture_event.get('scope_id')}")] = architecture_event
        if str(architecture_event.get("event") or "").startswith("architecture.waiver."):
            waiver_id = str(payload.get("waiver_id") or "")
            if waiver_id:
                waivers[waiver_id] = architecture_event
    return {
        "briefs": {
            "total": len(briefs),
            "accepted": sum(1 for brief in briefs.values() if brief.get("status") == "accepted"),
            "draft": sum(1 for brief in briefs.values() if brief.get("status") == "draft"),
        },
        "open_deviations": sum(1 for event in deviations.values() if (event.get("data") or {}).get("status", "open") == "open"),
        "active_waivers": sum(1 for event in waivers.values() if event.get("event") == "architecture.waiver.granted"),
        "scope_ids": sorted(briefs),
    }


def resolve_applicable_architecture(root: Path, task: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = task.get("architecture") if isinstance(task, dict) else None
    briefs_by_id = architecture_briefs(root)
    decisions_by_id = latest_decisions(root)
    if not isinstance(metadata, dict):
        return {
            "enabled": False,
            "classification": {"impact": "none"},
            "briefs": [],
            "principles": [],
            "decisions": [],
            "quality_attributes": [],
            "open_deviations": [],
            "active_waivers": [],
            "missing_scope_refs": [],
            "missing_decision_ids": [],
        }
    scope_refs = [str(item) for item in metadata.get("scope_refs", [])]
    briefs = [briefs_by_id[item] for item in scope_refs if item in briefs_by_id]
    decision_ids = list(dict.fromkeys(
        [str(item) for item in metadata.get("decision_ids", [])]
        + [str(item) for brief in briefs for item in brief.get("decision_ids", [])]
    ))
    architecture_events = _jsonl(root / "architecture" / "events.jsonl")
    latest_deviations: dict[str, dict[str, Any]] = {}
    latest_waivers: dict[str, dict[str, Any]] = {}
    for architecture_event in architecture_events:
        if architecture_event.get("scope_id") not in scope_refs:
            continue
        payload = architecture_event.get("data") if isinstance(architecture_event.get("data"), dict) else {}
        if architecture_event.get("event") == "architecture.deviation.recorded":
            latest_deviations[str(payload.get("deviation_id") or architecture_event.get("task_id"))] = architecture_event
        if str(architecture_event.get("event") or "").startswith("architecture.waiver."):
            waiver_id = str(payload.get("waiver_id") or "")
            if waiver_id:
                latest_waivers[waiver_id] = architecture_event
    principles_path = root / "architecture-principles.json"
    principle_doc = json.loads(principles_path.read_text(encoding="utf-8")) if principles_path.is_file() else {}
    return {
        "enabled": True,
        "classification": {
            "impact": metadata.get("impact", "none"),
            "concerns": metadata.get("concerns", []),
            "conformance_required": metadata.get("conformance_required", False),
            "human_gate": metadata.get("human_gate", "none"),
        },
        "briefs": briefs,
        "principles": principle_doc.get("principles", []),
        "decisions": [decisions_by_id[item] for item in decision_ids if item in decisions_by_id],
        "quality_attributes": [attribute for brief in briefs for attribute in brief.get("quality_attributes", [])],
        "open_deviations": [event for event in latest_deviations.values() if (event.get("data") or {}).get("status", "open") == "open"],
        "active_waivers": [event for event in latest_waivers.values() if event.get("event") == "architecture.waiver.granted"],
        "missing_scope_refs": [item for item in scope_refs if item not in briefs_by_id],
        "missing_decision_ids": [item for item in decision_ids if item not in decisions_by_id],
    }


IMPACT_ORDER = {"none": 0, "local": 1, "material": 2, "foundational": 3}
FOUNDATIONAL_SIGNALS = (
    "source of truth", "trust boundary", "authentication", "authorization", "identity",
    "data ownership", "public contract", "irreversible", "platform choice", "vendor lock-in",
)
MATERIAL_SIGNALS = (
    "schema", "migration", "database", "api", "event contract", "integration", "security",
    "privacy", "infrastructure", "deployment", "network", "retention",
)
MATERIAL_PATH_SIGNALS = ("migration", "schema", "infra", "auth", "api", "deploy", "terraform", "network")


def deterministic_minimum_impact(task: dict[str, Any]) -> tuple[str, list[str]]:
    text = " ".join([
        str(task.get("summary") or ""),
        str(task.get("description") or ""),
        " ".join(str(item) for item in (task.get("acceptance") or [])),
    ]).lower()
    paths = " ".join(str(item) for item in ((task.get("scope") or {}).get("modify") or [])).lower()
    foundational = sorted({signal for signal in FOUNDATIONAL_SIGNALS if signal in text})
    if foundational:
        return "foundational", foundational
    material = sorted({signal for signal in MATERIAL_SIGNALS if signal in text})
    material.extend(sorted({f"path:{signal}" for signal in MATERIAL_PATH_SIGNALS if signal in paths}))
    if material:
        return "material", list(dict.fromkeys(material))
    return "none", []


def effective_architecture_impact(task: dict[str, Any]) -> tuple[str, str, list[str]]:
    metadata = task.get("architecture") if isinstance(task.get("architecture"), dict) else {}
    explicit = str(metadata.get("impact") or "none")
    minimum, signals = deterministic_minimum_impact(task)
    effective = max((explicit, minimum), key=lambda item: IMPACT_ORDER.get(item, -1))
    return effective, minimum, signals


def _latest_architecture_events(root: Path, task_id: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for architecture_event in _jsonl(root / "architecture" / "events.jsonl"):
        if architecture_event.get("task_id") != task_id:
            continue
        event_name = str(architecture_event.get("event") or "")
        payload = architecture_event.get("data") if isinstance(architecture_event.get("data"), dict) else {}
        scope_id = str(architecture_event.get("scope_id") or "project")
        key = f"{event_name}:{scope_id}"
        if event_name.startswith("architecture.waiver."):
            key = f"waiver:{payload.get('waiver_id')}"
        latest[key] = architecture_event
    return latest


def architecture_claim_findings(root: Path, task: dict[str, Any]) -> list[str]:
    has_metadata = isinstance(task.get("architecture"), dict)
    if not has_metadata and not (root / "architecture").exists():
        return []
    effective, minimum, signals = effective_architecture_impact(task)
    metadata = task.get("architecture") if isinstance(task.get("architecture"), dict) else {}
    material = effective in {"material", "foundational"}
    # Explicit governing references are prerequisites even when the change is
    # small. Absence of metadata remains lightweight; no semantic score is inferred.
    if not material and not (metadata.get("scope_refs") or metadata.get("decision_ids")):
        return []
    findings: list[str] = []
    if IMPACT_ORDER.get(str(metadata.get("impact") or "none"), 0) < IMPACT_ORDER[minimum]:
        findings.append(f"architecture impact must be raised to deterministic minimum {minimum}: {', '.join(signals)}")
    applicable = resolve_applicable_architecture(root, task)
    if material and not metadata.get("scope_refs"):
        findings.append("material/foundational task requires architecture.scope_refs")
    if applicable["missing_scope_refs"]:
        findings.append("missing architecture briefs: " + ", ".join(applicable["missing_scope_refs"]))
    draft_briefs = [str(brief.get("id")) for brief in applicable["briefs"] if brief.get("status") != "accepted"]
    if draft_briefs:
        findings.append("architecture briefs must be accepted before claim: " + ", ".join(draft_briefs))
    governing_decision_ids = {
        str(item)
        for item in metadata.get("decision_ids", [])
    }
    governing_decision_ids.update(
        str(item)
        for brief in applicable["briefs"]
        for item in brief.get("decision_ids", [])
    )
    if material and not governing_decision_ids:
        findings.append("material/foundational task requires at least one accepted governing architecture decision from task metadata or an applicable brief")
    if applicable["missing_decision_ids"]:
        findings.append("missing architecture decisions: " + ", ".join(applicable["missing_decision_ids"]))
    unresolved = [
        str((event.get("data") or {}).get("decision_id"))
        for event in applicable["decisions"]
        if (event.get("data") or {}).get("status") != "accepted"
    ]
    if unresolved:
        findings.append("architecture decisions must be accepted: " + ", ".join(unresolved))
    if material and not applicable["quality_attributes"]:
        findings.append("material/foundational task requires measurable quality attributes in an applicable brief")
    return findings


def architecture_finish_findings(root: Path, task: dict[str, Any]) -> list[str]:
    has_metadata = isinstance(task.get("architecture"), dict)
    if not has_metadata and not (root / "architecture").exists():
        return []
    findings = architecture_claim_findings(root, task)
    metadata = task.get("architecture") if isinstance(task.get("architecture"), dict) else {}
    effective, _minimum, _signals = effective_architecture_impact(task)
    if effective in {"none", "local"}:
        return findings
    applicable = resolve_applicable_architecture(root, task)
    latest = _latest_architecture_events(root, str(task.get("id") or ""))
    scope_refs = [str(item) for item in metadata.get("scope_refs", [])]
    task_decision_ids = {str(item) for item in metadata.get("decision_ids", [])}
    briefs_by_scope = {
        str(brief.get("id")): brief
        for brief in applicable["briefs"]
        if isinstance(brief, dict) and brief.get("id")
    }
    if metadata.get("conformance_required", True):
        for scope_id in scope_refs:
            scope_brief = briefs_by_scope.get(scope_id, {})
            expected_decisions = task_decision_ids | {
                str(item) for item in scope_brief.get("decision_ids", [])
            }
            expected_quality_attributes = {
                str(item.get("id"))
                for item in scope_brief.get("quality_attributes", [])
                if isinstance(item, dict) and item.get("id")
            }
            conformance = latest.get(f"architecture.conformance.recorded:{scope_id}")
            if not conformance:
                findings.append(f"architecture conformance event required before finish for scope {scope_id}")
                continue
            payload = conformance.get("data") or {}
            status = payload.get("status")
            if status not in {"passed", "passed_with_waiver"}:
                findings.append(f"architecture conformance is not passing for scope {scope_id}: {status}")
            if not payload.get("evidence_refs"):
                findings.append(f"architecture conformance requires evidence_refs for scope {scope_id}")
            decision_checks = payload.get("decision_checks") or []
            checked_decisions = {str(item.get("id")) for item in decision_checks if isinstance(item, dict) and item.get("status") in {"passed", "waived"}}
            missing_decisions = sorted(expected_decisions - checked_decisions)
            if missing_decisions:
                findings.append(f"conformance missing passing decision checks for scope {scope_id}: " + ", ".join(missing_decisions))
            quality_checks = payload.get("quality_attribute_checks") or []
            checked_quality = {str(item.get("id")) for item in quality_checks if isinstance(item, dict) and item.get("status") in {"passed", "waived"}}
            missing_quality = sorted(expected_quality_attributes - checked_quality)
            if missing_quality:
                findings.append(f"conformance missing passing quality-attribute checks for scope {scope_id}: " + ", ".join(missing_quality))
            if status == "passed_with_waiver":
                waiver_ids = payload.get("waiver_ids") or []
                active_ids = {
                    str((event.get("data") or {}).get("waiver_id"))
                    for event in applicable["active_waivers"]
                }
                missing = [str(item) for item in waiver_ids if str(item) not in active_ids]
                if not waiver_ids:
                    findings.append(f"passed_with_waiver requires waiver_ids for scope {scope_id}")
                elif missing:
                    findings.append("conformance references inactive waivers: " + ", ".join(missing))
    human_gate = str(metadata.get("human_gate") or "none")
    if human_gate != "none" or effective == "foundational":
        for scope_id in scope_refs:
            review = latest.get(f"architecture.reviewed:{scope_id}")
            payload = review.get("data") if review else {}
            if not review or payload.get("status") != "approved" or payload.get("human") is not True:
                findings.append(f"explicit human architecture approval required for scope {scope_id} and gate {human_gate if human_gate != 'none' else 'foundational'}")
    if applicable["open_deviations"] and not applicable["active_waivers"]:
        findings.append("open architecture deviations require repair or an active time-bounded waiver")
    return findings
