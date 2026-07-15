"""Validation contract for portable live Hermes acceptance evidence."""

from __future__ import annotations

import re
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

LIVE_HERMES_PROOF_SCHEMA = "go-workflow.live-hermes-proof.v1"
REQUIRED_TASKS = {"phase-one", "phase-two"}
REQUIRED_RESULT_FILES = {"doctor.json", "first.json", "resumed.json"}
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PROOF_FIELDS = {
    "schema", "status", "created_at", "binary", "binary_version", "repo",
    "completed_tasks", "protocol_results", "result_sha256",
}
PROTOCOL_FIELDS = {"result_file", "task_id", "attempt", "phase", "status", "summary"}


def validate_live_hermes_proof(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    unexpected_fields = sorted(set(data) - PROOF_FIELDS)
    if unexpected_fields:
        errors.append("unexpected proof fields: " + ", ".join(unexpected_fields))
    if data.get("schema") != LIVE_HERMES_PROOF_SCHEMA:
        errors.append(f"schema must be {LIVE_HERMES_PROOF_SCHEMA}")
    if data.get("status") != "proven":
        errors.append("status must be proven")
    created_at = data.get("created_at")
    try:
        parsed_created_at = datetime.fromisoformat(str(created_at))
        if parsed_created_at.utcoffset() is None:
            errors.append("created_at must include a timezone offset")
    except (TypeError, ValueError):
        errors.append("created_at must be an ISO-8601 timestamp")
    binary = data.get("binary")
    if not isinstance(binary, str) or not binary.strip() or not Path(binary).is_absolute():
        errors.append("binary must be a non-empty absolute path")
    if not isinstance(data.get("binary_version"), str) or not data.get("binary_version", "").strip():
        errors.append("binary_version must be a non-empty string")
    if not isinstance(data.get("repo"), str) or not data.get("repo", "").strip():
        errors.append("repo must be a non-empty string")
    completed = data.get("completed_tasks")
    if not isinstance(completed, list) or not all(isinstance(item, str) for item in completed) or set(completed) != REQUIRED_TASKS:
        errors.append("completed_tasks must contain exactly phase-one and phase-two")

    protocol = data.get("protocol_results")
    successful: set[tuple[str, str]] = set()
    if not isinstance(protocol, list) or not protocol:
        errors.append("protocol_results must be a non-empty array")
    else:
        for index, item in enumerate(protocol):
            prefix = f"protocol_results[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            unexpected_item_fields = sorted(set(item) - PROTOCOL_FIELDS)
            if unexpected_item_fields:
                errors.append(f"{prefix} has unexpected fields: " + ", ".join(unexpected_item_fields))
            task_id = item.get("task_id")
            phase = item.get("phase")
            if task_id not in REQUIRED_TASKS:
                errors.append(f"{prefix}.task_id must be phase-one or phase-two")
            if phase not in {"build", "critic", "repair"}:
                errors.append(f"{prefix}.phase must be build, critic, or repair")
            if not isinstance(item.get("attempt"), int) or item.get("attempt", 0) < 1:
                errors.append(f"{prefix}.attempt must be a positive integer")
            if item.get("status") not in {"success", "failure", "blocked"}:
                errors.append(f"{prefix}.status must be success, failure, or blocked")
            if not isinstance(item.get("summary"), str) or not item.get("summary", "").strip():
                errors.append(f"{prefix}.summary must be a non-empty string")
            if item.get("result_file") not in {"first.json", "resumed.json"}:
                errors.append(f"{prefix}.result_file must be first.json or resumed.json")
            if task_id in REQUIRED_TASKS and phase in {"build", "critic"} and item.get("status") == "success":
                successful.add((task_id, phase))
    required_success = {(task, phase) for task in REQUIRED_TASKS for phase in ("build", "critic")}
    missing_success = sorted(required_success - successful)
    if missing_success:
        errors.append("missing successful native protocol evidence: " + ", ".join(f"{task}/{phase}" for task, phase in missing_success))

    hashes = data.get("result_sha256")
    if not isinstance(hashes, dict) or set(hashes) != REQUIRED_RESULT_FILES:
        errors.append("result_sha256 must contain exactly doctor.json, first.json, and resumed.json")
    elif any(not isinstance(value, str) or not HASH_RE.fullmatch(value) for value in hashes.values()):
        errors.append("result_sha256 values must be lowercase SHA-256 hex digests")
    return errors


def verify_live_hermes_evidence(data: dict[str, Any], evidence_root: Path) -> list[str]:
    errors: list[str] = []
    hashes = data.get("result_sha256")
    if not isinstance(hashes, dict):
        return ["result_sha256 must be valid before evidence hashes can be verified"]
    results: dict[str, dict[str, Any]] = {}
    for name in sorted(REQUIRED_RESULT_FILES):
        path = evidence_root / name
        if not path.is_file():
            errors.append(f"evidence file is missing: {path}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if hashes.get(name) != actual:
            errors.append(f"evidence hash mismatch for {name}: expected {hashes.get(name)}, actual {actual}")
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{name} must contain valid JSON: {exc}")
            continue
        if not isinstance(result, dict):
            errors.append(f"{name} JSON root must be an object")
            continue
        results[name] = result

    doctor = results.get("doctor.json")
    if doctor is not None:
        agent = doctor.get("agent")
        if doctor.get("schema") != "go-workflow.doctor.v1" or doctor.get("ready") is not True:
            errors.append("doctor.json must be a ready go-workflow.doctor.v1 result")
        if not isinstance(agent, dict) or agent.get("name") != "hermes" or agent.get("available") is not True or not agent.get("path"):
            errors.append("doctor.json must record an available Hermes agent path")

    expected_runs = {
        "first.json": ("budget_exhausted", ["phase-one"]),
        "resumed.json": ("done", ["phase-two"]),
    }
    reconstructed_protocol: list[dict[str, Any]] = []
    for name, (expected_status, expected_tasks) in expected_runs.items():
        result = results.get(name)
        if result is None:
            continue
        if result.get("schema") != "go-workflow.auto-run-result.v1":
            errors.append(f"{name} must be a go-workflow.auto-run-result.v1 result")
        if result.get("status") != expected_status or result.get("completed_tasks") != expected_tasks:
            errors.append(f"{name} must have status {expected_status} and completed_tasks {expected_tasks}")
        if name == "resumed.json":
            audit = result.get("completion_audit")
            if not isinstance(audit, dict) or audit.get("project_verification_passed") is not True:
                errors.append("resumed.json must contain a passing project completion audit")
        attempts = result.get("attempts")
        if not isinstance(attempts, list):
            errors.append(f"{name}.attempts must be an array")
            continue
        for attempt in attempts:
            if not isinstance(attempt, dict):
                errors.append(f"{name}.attempts entries must be objects")
                continue
            for phase in ("build", "critic", "repair"):
                phase_container = attempt.get(phase)
                if not isinstance(phase_container, dict) or phase_container.get("result") is None:
                    continue
                phase_result = phase_container.get("result")
                if not isinstance(phase_result, dict) or phase_result.get("schema") != "go-workflow.agent-adapter-result.v1" or phase_result.get("phase") != phase:
                    errors.append(f"{name} contains invalid native {phase} protocol evidence")
                    continue
                reconstructed_protocol.append({
                    "result_file": name,
                    "task_id": attempt.get("task_id"),
                    "attempt": attempt.get("attempt"),
                    "phase": phase,
                    "status": phase_result.get("status"),
                    "summary": phase_result.get("summary"),
                })
    if reconstructed_protocol != data.get("protocol_results"):
        errors.append("protocol_results must exactly match native adapter results reconstructed from first.json and resumed.json")
    return errors
