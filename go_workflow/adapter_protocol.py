"""Stable JSON boundary shared by Codex, Hermes, and custom adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model_profiles import validate_model_selection

ADAPTER_REQUEST_SCHEMA = "go-workflow.agent-adapter-request.v1"
ADAPTER_RESULT_SCHEMA = "go-workflow.agent-adapter-result.v1"
ADAPTER_PHASES = {"build", "critic", "repair"}
ADAPTER_STATUSES = {"success", "failure", "blocked"}


def build_adapter_request(
    repo: Path,
    task: dict[str, Any],
    context: dict[str, Any],
    phase: str,
    attempt: int,
    strategy: str,
    context_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if phase not in ADAPTER_PHASES:
        raise ValueError(f"unsupported adapter phase: {phase}")
    request = {
        "schema": ADAPTER_REQUEST_SCHEMA,
        "phase": phase,
        "repo": str(repo),
        "task_id": str(task.get("id") or "unknown"),
        "attempt": attempt,
        "strategy": strategy,
        "task": task,
        "context": context,
        "result_schema": ADAPTER_RESULT_SCHEMA,
    }
    if context_ref is not None:
        request.update(task={'id': task['id']}, context={'snapshot': context_ref}, context_ref=context_ref)
    return request



def validate_adapter_result(data: dict[str, Any], expected_phase: str | None = None, *, worker_payload: bool = False) -> list[str]:
    errors: list[str] = []
    if data.get("schema") != ADAPTER_RESULT_SCHEMA:
        errors.append(f"schema must be {ADAPTER_RESULT_SCHEMA}")
    if data.get("phase") not in ADAPTER_PHASES:
        errors.append("phase must be build, critic, or repair")
    if expected_phase and data.get("phase") != expected_phase:
        errors.append(f"phase must match request phase {expected_phase}")
    if data.get("status") not in ADAPTER_STATUSES:
        errors.append("status must be success, failure, or blocked")
    if not isinstance(data.get("summary"), str) or not data.get("summary", "").strip():
        errors.append("summary must be a non-empty string")
    if "model_selection" in data and not worker_payload:
        errors.extend(validate_model_selection(data["model_selection"]))
    if "behavior_review" in data:
        review = data["behavior_review"]
        if not isinstance(review, dict) or review.get("schema") != "go-workflow.behavior-review.v1":
            errors.append("behavior_review must use go-workflow.behavior-review.v1")
        if data.get("phase") != "critic":
            errors.append("behavior_review is only valid for the critic phase")
    return errors


def normalize_adapter_result(
    phase: str,
    command: str,
    completed: dict[str, Any],
    require_protocol: bool = False,
) -> dict[str, Any]:
    stdout = str(completed.get("stdout") or "")
    protocol_result: dict[str, Any] | None = None
    protocol_looking_result: dict[str, Any] | None = None
    for line in reversed([item.strip() for item in stdout.splitlines() if item.strip()]):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            if candidate.get("schema") == ADAPTER_RESULT_SCHEMA:
                protocol_result = candidate
                break
            if "schema" in candidate or {"phase", "status", "summary"}.issubset(candidate):
                protocol_looking_result = candidate
                break

    returncode = int(completed.get("returncode") or 0)
    if protocol_result is not None:
        errors = validate_adapter_result(protocol_result, expected_phase=phase, worker_payload=True)
        if errors:
            returncode = returncode or 65
            protocol_result = {
                "schema": ADAPTER_RESULT_SCHEMA,
                "phase": phase,
                "status": "failure",
                "summary": "invalid adapter result: " + "; ".join(errors),
            }
    elif protocol_looking_result is not None:
        errors = validate_adapter_result(protocol_looking_result, expected_phase=phase, worker_payload=True)
        returncode = returncode or 65
        protocol_result = {
            "schema": ADAPTER_RESULT_SCHEMA,
            "phase": phase,
            "status": "failure",
            "summary": "invalid adapter result: " + "; ".join(errors),
        }
    elif require_protocol:
        returncode = returncode or 65
        protocol_result = {
            "schema": ADAPTER_RESULT_SCHEMA,
            "phase": phase,
            "status": "failure",
            "summary": "native adapter did not emit a versioned adapter result",
        }
    else:
        protocol_result = {
            "schema": ADAPTER_RESULT_SCHEMA,
            "phase": phase,
            "status": "success" if returncode == 0 else "failure",
            "summary": "adapter process completed" if returncode == 0 else "adapter process failed",
        }

    protocol_result.update({
        "hook": phase,
        "command": command,
        "returncode": returncode,
        "stdout": stdout[-4000:],
        "stderr": str(completed.get("stderr") or "")[-4000:],
        "timed_out": bool(completed.get("timed_out")),
    })
    if protocol_result["status"] != "success" and protocol_result["returncode"] == 0:
        protocol_result["returncode"] = 1
    return protocol_result


def codex_stream_payload(completed: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, int]] | None]:
    """Extract worker text and runtime token counts from Codex's JSON event stream.

    Agent-message text is never parsed as runtime usage or model attestation.
    """
    messages, turns = [], []
    stream_seen = False
    turn_completed = False
    turn_failed = False
    for line in str(completed.get('stdout') or '').splitlines():
        try: record = json.loads(line)
        except ValueError: continue
        if not isinstance(record, dict): continue
        event_type = record.get('type')
        if event_type in ('thread.started', 'turn.started', 'item.started', 'item.completed', 'turn.completed', 'turn.failed'):
            stream_seen = True
        if event_type == 'turn.completed': turn_completed = True
        if event_type == 'turn.failed': turn_failed = True
        if event_type == 'item.completed':
            item = record.get('item')
            if isinstance(item, dict) and item.get('type') == 'agent_message' and isinstance(item.get('text'), str):
                messages.append(item['text'])
        if event_type == 'turn.completed' and isinstance(record.get('usage'), dict):
            usage = record['usage']
            counts = {name: usage[name] for name in ('input_tokens', 'cached_input_tokens', 'output_tokens')
                      if type(usage.get(name)) is int and usage[name] >= 0}
            if counts: turns.append(counts)
    if not stream_seen or not turn_completed or turn_failed:
        reason = 'Codex turn failed' if turn_failed else 'Codex JSON stream has no completed turn'
        completed = {**completed, 'returncode': completed.get('returncode') or 65,
                     'stderr': str(completed.get('stderr') or '') + '\n' + reason}
    return {**completed, 'stdout': '\n'.join(messages)}, turns or None
