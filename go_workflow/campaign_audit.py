"""Current-evidence goal audit and compact readback for bounded campaigns."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .campaign_contracts import campaign_findings, contract_digest, validate_campaign_contract
from .completion import completion_findings, read_artifact
from .state_io import atomic_json, atomic_write_text
from .worktrees import workflow_root


AUDIT_SCHEMA = "go-workflow.campaign-goal-audit.v1"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"campaign goal audit cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"campaign goal audit requires an object: {path}")
    return value


def _task(root: Path, task_id: str) -> tuple[str, dict[str, Any]] | None:
    found = []
    for state in ("open", "active", "blocked", "done"):
        path = root / "tasks" / state / f"{task_id}.json"
        if path.is_file():
            found.append((state, _load(path)))
    return found[0] if len(found) == 1 else None


def _pointer_set(outcome: dict[str, Any]) -> set[str]:
    values = outcome.get("evidence") or []
    if not isinstance(values, list):
        return set()
    pointers: set[str] = set()
    for item in values:
        value = item.get("summary") if isinstance(item, dict) else item
        if isinstance(value, str):
            pointers.add(value)
    return pointers


def _proof_item(
    kind: str,
    ref: dict[str, Any],
    task_id: str,
    outcome_id: str,
    *,
    evidence_class: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "evidence_class": evidence_class or ("release" if kind == "release" else "locally_verified"),
        "kind": kind,
        "task_id": task_id,
        "outcome_id": outcome_id,
        "ref": ref["path"],
        "sha256": ref["sha256"],
        "details": details or {},
    }


def _outcome_audit(
    repo: Path,
    root: Path,
    contract_outcome: dict[str, Any],
    contract_errors: list[str],
    shipping: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    findings: list[str] = list(contract_errors)
    task_results: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    states: list[str] = []
    for link in contract_outcome["task_outcomes"]:
        task_id = link["task_id"]
        located = _task(root, task_id)
        link_findings: list[str] = []
        evidence: list[dict[str, Any]] = []
        state = "missing"
        if located is None:
            link_findings.append(f"Task {task_id} is missing or duplicated across task states.")
        else:
            state, task = located
            if state != "done":
                link_findings.append(f"Task {task_id} is {state}; current completion proof is unavailable.")
            elif task.get("outcome_tracking_version") != 1:
                link_findings.append(
                    f"Task {task_id} is historical/unadopted; it is not outcome-tracked current proof."
                )
            else:
                from .architecture import architecture_finish_findings

                link_findings.extend(architecture_finish_findings(root, task))
                if shipping:
                    from .delivery_closure import inspect_closure
                    closure = inspect_closure(repo, task_id, expected_policy=shipping["policy"])
                    if not closure["delivered"]:
                        link_findings.extend(closure["blockers"])
                requirements = {
                    item.get("id"): item for item in task.get("requested_outcomes", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
                requirement = requirements.get(link["requirement_id"])
                if requirement is None:
                    link_findings.append(
                        f"Task {task_id} lacks linked requirement {link['requirement_id']}."
                    )
                elif hashlib.sha256(str(requirement.get("text", "")).encode()).hexdigest() != link["text_sha256"]:
                    link_findings.append(f"Task {task_id}:{link['requirement_id']} text no longer matches the mandate.")
                else:
                    lifecycle = completion_findings(repo, task, current=False, remote=False)
                    link_findings.extend(lifecycle)
                    manifest = task.get("completion_evidence") or {}
                    pointers = _pointer_set(requirement)
                    lifecycle_paths = {
                        value.get("path") for value in manifest.values()
                        if isinstance(value, dict) and isinstance(value.get("path"), str)
                    }
                    if requirement.get("status") != "verified" or not pointers.intersection(lifecycle_paths):
                        link_findings.append(
                            f"Task {task_id}:{link['requirement_id']} lacks attributed current lifecycle proof."
                        )
                    for kind in contract_outcome["required_evidence"]:
                        manifest_kind = "release" if kind == "live" else kind
                        ref = manifest.get(manifest_kind)
                        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
                            link_findings.append(f"Task {task_id} lacks {kind} evidence.")
                            continue
                        try:
                            artifact = read_artifact(root, ref)
                        except (ValueError, OSError) as exc:
                            link_findings.append(f"Task {task_id} has invalid {kind} evidence: {exc}")
                            continue
                        evidence_ref = ref
                        if kind == "live":
                            deployment_ref = artifact.get("deployment")
                            if not isinstance(deployment_ref, dict) or set(deployment_ref) != {"path", "sha256"}:
                                link_findings.append(f"Task {task_id} release lacks an exact live receipt.")
                                continue
                            try:
                                artifact = read_artifact(root, deployment_ref)
                            except (ValueError, OSError) as exc:
                                link_findings.append(f"Task {task_id} has an invalid live receipt: {exc}")
                                continue
                            evidence_ref = deployment_ref
                        details = {
                            key: artifact[key] for key in (
                                "revision", "commit", "version", "tag", "status", "target",
                                "idempotency_key", "observation",
                            )
                            if key in artifact
                        }
                        evidence.append(_proof_item(
                            kind,
                            evidence_ref,
                            task_id,
                            contract_outcome["id"],
                            evidence_class="live" if kind == "live" else None,
                            details=details,
                        ))
                    receipt = task.get("release_receipt")
                    if isinstance(receipt, dict) and receipt.get("status") == "verified":
                        details = {key: receipt[key] for key in ("commit", "tag") if key in receipt}
                        release_ref = manifest.get("release")
                        if isinstance(release_ref, dict) and not any(
                            item["kind"] == "release" and item["task_id"] == task_id for item in evidence
                        ):
                            evidence.append(_proof_item(
                                "release", release_ref, task_id, contract_outcome["id"], details=details,
                            ))
        if link_findings:
            findings.extend(link_findings)
        states.append(state)
        task_results.append({
            "task_id": task_id,
            "requirement_id": link["requirement_id"],
            "task_state": state,
            "status": "achieved" if not link_findings else ("blocked" if state in {"blocked", "done", "missing"} else "partial"),
            "findings": link_findings,
            "evidence": evidence,
        })
        provenance.extend(evidence)
    if not contract_outcome["task_outcomes"]:
        status = "excluded"
        findings.append("Outcome is explicitly outside execution because the planning contract maps no task.")
    elif not findings:
        status = "achieved"
    elif any(value in {"blocked", "done", "missing"} for value in states) or contract_errors:
        status = "blocked"
    else:
        status = "partial"
    return ({
        "id": contract_outcome["id"],
        "text": contract_outcome["text"],
        "status": status,
        "required_evidence": contract_outcome["required_evidence"],
        "task_outcomes": task_results,
        "findings": list(dict.fromkeys(findings)),
    }, provenance)


def _follow_ups(contract: dict[str, Any], outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    proposals: dict[str, dict[str, Any]] = {}
    used = {"repair": 0, "research": 0}
    for outcome in outcomes:
        if outcome["status"] in {"achieved", "excluded"}:
            continue
        # An existing open/active task already is the bounded next action; do not
        # manufacture a duplicate merely because it has not completed yet.
        if any(item["task_state"] in {"open", "active"} for item in outcome["task_outcomes"]):
            continue
        for kind in ("repair", "research"):
            allowance = contract["authority"]["expansion"][kind]
            if (allowance["max_tasks"] <= used[kind]
                    or outcome["id"] not in allowance["outcome_ids"]):
                continue
            key = f"{kind}:{outcome['id']}"
            proposals.setdefault(key, {
                "key": key,
                "kind": kind,
                "outcome_id": outcome["id"],
                "modify": allowance["modify"],
                "reason": f"Campaign outcome {outcome['id']} lacks current completion proof.",
                "source_ref": contract["intent"]["source_ref"],
            })
            used[kind] += 1
            break
    return list(proposals.values())


def render_campaign_handoff(audit: dict[str, Any]) -> str:
    """Render a compact report whose labels distinguish proof from pending work."""
    lines = [
        f"# Campaign handoff: {audit['campaign_id']}",
        "",
        f"Status: **{audit['status']}**",
        f"Goal verified: **{'yes' if audit['goal_verified'] else 'no'}**",
        "",
        "## Original request",
        "",
        audit["original_request"]["text"],
        "",
        f"Source: `{audit['original_request']['source_ref']}`  ",
        f"SHA-256: `{audit['original_request']['sha256']}`",
        "",
        "## Outcome decisions",
        "",
    ]
    for outcome in audit["outcomes"]:
        lines.append(f"- **{outcome['id']} — {outcome['status']}**: {outcome['text']}")
        for finding in outcome["findings"]:
            lines.append(f"  - {finding}")
    lines.extend(["", "## Tasks and tested candidates", ""])
    task_rows = [
        (outcome["id"], item)
        for outcome in audit["outcomes"]
        for item in outcome["task_outcomes"]
    ]
    if not task_rows:
        lines.append("- No executable task was mapped; the outcome is explicitly excluded by this planning contract.")
    for outcome_id, item in task_rows:
        lines.append(
            f"- `{item['task_id']}:{item['requirement_id']}` → `{outcome_id}` · "
            f"{item['task_state']} · {item['status']}"
        )
    grouped = {
        "Locally verified": [item for item in audit["provenance"] if item["evidence_class"] == "locally_verified"],
        "Releases": [item for item in audit["provenance"] if item["evidence_class"] == "release"],
        "Live receipts": [item for item in audit["provenance"] if item["evidence_class"] == "live"],
        "Pending / blocked": [item for item in audit["provenance"] if item["evidence_class"] == "pending"],
        "User decisions": [item for item in audit["provenance"] if item["evidence_class"] == "user_decision"],
    }
    for title, items in grouped.items():
        lines.extend(["", f"## {title}", ""])
        if not items:
            lines.append("- None recorded.")
        for item in items:
            subject = item.get("task_id") or item.get("outcome_id") or audit["campaign_id"]
            detail = ""
            if item["details"]:
                detail = " · " + json.dumps(item["details"], sort_keys=True, ensure_ascii=False)
            lines.append(f"- `{subject}` · {item['kind']} · `{item['ref']}`{detail}")
    lines.extend(["", "## Bounded follow-ups", ""])
    if audit["follow_ups"]:
        for item in audit["follow_ups"]:
            lines.append(f"- `{item['key']}`: {item['reason']}")
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def audit_campaign_goal(
    repo: Path,
    contract_path: Path,
    *,
    previous_path: Path | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Audit adopted campaign outcomes against current canonical evidence."""
    repo = repo.resolve()
    root = workflow_root(repo)
    contract = _load(Path(contract_path).resolve())
    previous = _load(Path(previous_path).resolve()) if previous_path else None
    shape_findings = validate_campaign_contract(contract)
    if shape_findings:
        raise ValueError("invalid campaign contract: " + "; ".join(shape_findings))
    contract_errors = campaign_findings(repo, contract, previous=previous)
    outcomes: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for outcome in contract.get("goal", {}).get("outcomes", []):
        result, items = _outcome_audit(repo, root, outcome, contract_errors, (contract.get("execution") or {}).get("shipping"))
        outcomes.append(result)
        provenance.extend(items)
    for decision in contract.get("decisions", []):
        provenance.append({
            "evidence_class": "user_decision",
            "kind": "decision",
            "task_id": None,
            "outcome_id": None,
            "ref": decision["source_ref"],
            "sha256": None,
            "details": {
                "id": decision["id"], "status": decision["status"], "owner": decision["owner"],
            },
        })
    queue = {
        state: sorted(path.stem for path in (root / "tasks" / state).glob("*.json"))
        for state in ("open", "active", "blocked")
    }
    if any(item["status"] == "blocked" for item in outcomes) or contract_errors:
        status = "blocked"
    elif outcomes and all(item["status"] in {"achieved", "excluded"} for item in outcomes):
        status = "achieved"
    else:
        status = "partial"
    for outcome in outcomes:
        if outcome["status"] not in {"achieved", "excluded"}:
            provenance.append({
                "evidence_class": "pending",
                "kind": "outcome",
                "task_id": None,
                "outcome_id": outcome["id"],
                "ref": contract["intent"]["source_ref"],
                "sha256": None,
                "details": {"status": outcome["status"]},
            })
    directory = root / "runs" / "campaigns" / contract["id"]
    audit_path = directory / "goal-audit.json"
    report_path = directory / "handoff.md"
    audit = {
        "schema": AUDIT_SCHEMA,
        "campaign_id": contract["id"],
        "project": contract["project"],
        "contract": {"revision": contract["revision"], "sha256": contract_digest(contract)},
        "original_request": contract["intent"],
        "goal": contract["goal"]["text"],
        "status": status,
        "goal_verified": status == "achieved" and bool(outcomes),
        "outcomes": outcomes,
        "contract_findings": contract_errors,
        "queue": queue,
        "follow_ups": _follow_ups(contract, outcomes),
        "provenance": provenance,
        "handoff": {
            "path": str(report_path.relative_to(repo)),
            "format": "markdown",
        },
    }
    if persist:
        atomic_json(audit_path, audit)
        atomic_write_text(report_path, render_campaign_handoff(audit))
    return audit
