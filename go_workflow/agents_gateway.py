"""Managed root ``AGENTS.md`` gateway for repositories that use ``.go``."""

from __future__ import annotations

import hashlib
import json
import re
import os
from pathlib import Path
import stat
from typing import Any
import uuid

from .state_io import atomic_write_text, repository_lock


GATEWAY_SCHEMA = "go-workflow.agents-gateway-plan.v1"
GATEWAY_START = "<!-- go-workflow:agents-gateway:v1:start -->"
GATEWAY_END = "<!-- go-workflow:agents-gateway:v1:end -->"
GATEWAY_BLOCK = f"""{GATEWAY_START}
## Repository-local Go workflow gateway

This repository uses `.go/` as its project workflow source of truth. Keep repository-specific instructions outside this managed block; they remain binding.

When the user invokes `Go`, `Go plan`, `Go <task-id>`, `Go loop`, or asks to continue autonomously:

1. Run the immutable stack-freshness preflight required by the pinned `.go/project.json` before routing or product edits.
2. Read `.go/vision.json`, `.go/architecture-principles.json`, `.go/hierarchy.json`, and the selected task.
3. Run the repository-local `validate`, `status`, and `router` commands, then announce `Route: <selected_route>`.
4. Create or repair a concrete `.go` task before changing product files. Execute one task at a time and stay within its `scope.modify` boundary.
5. Record content-bound verification evidence, run the required critic/recheck, repair blocking findings, and satisfy finish plus any required release evidence.
6. Continue through remaining in-scope work until the goal is met, a declared budget is exhausted, or a real repository gate blocks progress. Never call an empty queue `done` without auditing the original outcomes.
7. “Go tot alle taken klaar” freezes all unfinished in-scope task IDs, including blocked tasks, into the existing campaign controller. Show scope and dependency order; do not invent a five-task or two-hour ceiling. Explicit user budgets still apply.
8. Finish each task through claim, implementation, verification, independent review, commit, authorized push, configured deployment/readback and synchronized closure before announcing done and starting another. New authorized taskwise runs default to push; user/repository restrictions and frozen older-run authority prevail. Deployment without a requirement is not applicable; required unconfigured deployment is blocked.
9. Use the versioned progress outbox and an independently capable transport for task start, phase, repair, amendment, done and final messages. Preflight transport before unattended execution. A separate watcher offers a heartbeat every 300 seconds; no connected transport means no promised chat heartbeat. Retain undelivered events and stop before the next task until delivery recovers.
10. Resume from current canonical task, workspace, proof and publication records. Keep necessary repair tasks linked to original outcomes, show amended future tasks, and continue independent work around concrete blockers. Never count status/log churn as proven progress or expand an old run’s authority implicitly.

Do not redirect repository workflow state to a hidden central queue or retired vault. Nested `AGENTS.md` files may add directory-specific obligations, but they do not replace the root gateway or `.go` source of truth.
{GATEWAY_END}
"""


# Exact released predecessor remains readable at its old immutable pin. Adoption
# and stack update still render the current gateway; this grants no new authority.
LEGACY_GATEWAY_BLOCK = GATEWAY_BLOCK[:GATEWAY_BLOCK.index('7. “Go tot alle taken klaar”')] + GATEWAY_BLOCK[GATEWAY_BLOCK.index('\nDo not redirect repository workflow'):]


def _readable_legacy_gateway(repo, existing):
    try:
        project = json.loads((repo / '.go/project.json').read_text())
        ref = project.get('stack_ref', '')
        version = project.get('required_stack_version', '')
        if ref != 'v' + version or not re.fullmatch(r'\d+\.\d+\.\d+', version):
            return False
        return tuple(map(int, version.split('.'))) < (0, 3, 48) and LEGACY_GATEWAY_BLOCK.strip() in existing
    except (OSError, ValueError, TypeError):
        return False


class AgentsGatewayError(ValueError):
    """The gateway cannot be safely inspected or repaired."""


def _sha256(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else None


def _agents_entries(repo: Path) -> tuple[Path | None, list[Path]]:
    try:
        matches = [entry for entry in repo.iterdir() if entry.name.casefold() == "agents.md"]
    except OSError as exc:
        raise AgentsGatewayError(f"cannot inspect repository root for AGENTS.md: {exc}") from exc
    exact = next((entry for entry in matches if entry.name == "AGENTS.md"), None)
    return exact, matches


def _read_existing(repo: Path) -> tuple[Path | None, str | None, bool, int | None]:
    exact, matches = _agents_entries(repo)
    if len(matches) > 1:
        names = ", ".join(sorted(path.name for path in matches))
        raise AgentsGatewayError(f"multiple case variants of root AGENTS.md exist: {names}")
    source = exact or (matches[0] if matches else None)
    if source is None:
        return None, None, False, None
    if source.is_symlink() or not source.is_file():
        raise AgentsGatewayError("root AGENTS.md must be a regular file, not a link or directory")
    try:
        with source.open("r", encoding="utf-8", newline="") as handle:
            content = handle.read()
        return source, content, exact is not None, stat.S_IMODE(source.stat().st_mode)
    except (OSError, UnicodeError) as exc:
        raise AgentsGatewayError(f"cannot read root AGENTS.md as UTF-8: {exc}") from exc


def render_gateway(existing: str | None) -> tuple[str, str]:
    """Return desired bytes and the bounded action without touching disk."""
    if existing is None:
        return GATEWAY_BLOCK, "create"
    starts, ends = existing.count(GATEWAY_START), existing.count(GATEWAY_END)
    if starts == 0 and ends == 0:
        separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        return existing + separator + GATEWAY_BLOCK, "append"
    if starts != 1 or ends != 1:
        raise AgentsGatewayError(
            "ambiguous managed gateway markers in root AGENTS.md; restore one matched start/end pair before retrying"
        )
    start = existing.index(GATEWAY_START)
    end = existing.index(GATEWAY_END, start) + len(GATEWAY_END)
    if existing.find(GATEWAY_END) < start:
        raise AgentsGatewayError(
            "ambiguous managed gateway markers in root AGENTS.md; the end marker precedes the start marker"
        )
    desired = existing[:start] + GATEWAY_BLOCK.rstrip("\n") + existing[end:]
    return desired, "none" if desired == existing else "replace"


def plan_agents_gateway(repo: Path) -> dict[str, Any]:
    repo = Path(repo).resolve()
    if not (repo / ".go").is_dir():
        raise AgentsGatewayError(f"cannot install AGENTS.md gateway without repository-local .go: {repo}")
    source, before, exact_case, before_mode = _read_existing(repo)
    after, action = render_gateway(before)
    if source is not None and not exact_case and action == "none":
        action = "rename"
    elif source is not None and not exact_case:
        action = "rename_and_" + action
    return {
        "schema": GATEWAY_SCHEMA,
        "mode": "dry_run",
        "repo": str(repo),
        "path": "AGENTS.md",
        "source_path": source.name if source is not None else None,
        "action": action,
        "before_sha256": _sha256(before),
        "after_sha256": _sha256(after),
        "before_mode": before_mode,
        "before": before,
        "after": after,
    }


def apply_agents_gateway(repo: Path, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    repo = Path(repo).resolve()
    with repository_lock(repo / ".go", "agents-gateway"):
        fresh = plan_agents_gateway(repo)
        if plan is not None:
            compared = ("repo", "path", "source_path", "action", "before_sha256", "after_sha256", "before_mode")
            if any(plan.get(key) != fresh.get(key) for key in compared):
                raise AgentsGatewayError("root AGENTS.md changed after planning; inspect and retry")
        if fresh["action"] != "none":
            target = repo / "AGENTS.md"
            source_name = fresh.get("source_path")
            displaced = None
            if isinstance(source_name, str) and source_name != "AGENTS.md":
                displaced = repo / f".go-agents-case-{uuid.uuid4().hex}"
                os.replace(repo / source_name, displaced)
            try:
                atomic_write_text(target, fresh["after"])
                target.chmod(fresh["before_mode"] if fresh["before_mode"] is not None else 0o644)
                if displaced is not None:
                    displaced.unlink()
            except BaseException:
                if displaced is not None and displaced.exists():
                    os.replace(displaced, repo / source_name)
                raise
    return {key: value for key, value in fresh.items() if key not in {"before", "after"}} | {"mode": "applied"}


def restore_agents_gateway(repo: Path, plan: dict[str, Any]) -> None:
    """Restore the exact pre-plan root instruction bytes after a failed transaction."""
    repo = Path(repo).resolve()
    source_name, before = plan.get("source_path"), plan.get("before")
    for entry in list(repo.iterdir()):
        if entry.name.casefold() == "agents.md" and (before is None or entry.name != source_name):
            entry.unlink()
    if before is not None and isinstance(source_name, str):
        atomic_write_text(repo / source_name, before)
        (repo / source_name).chmod(int(plan.get("before_mode") or 0o644))


def validate_agents_gateway(repo: Path) -> list[str]:
    repo = Path(repo).resolve()
    try:
        source, existing, exact_case, _mode = _read_existing(repo)
        if source is None:
            return [
                "root AGENTS.md is required whenever .go exists; repair with "
                "`go-workflow agents sync . --apply`"
            ]
        if not exact_case:
            return [
                f"root AGENTS.md must use exact casing and root location; found {source.name!r}; repair with "
                "`go-workflow agents sync . --apply`"
            ]
        desired, action = render_gateway(existing)
        if action != "none" or desired != existing:
            if _readable_legacy_gateway(repo, existing):
                return []
            return [
                "root AGENTS.md is missing the current bounded .go gateway block; repair with "
                "`go-workflow agents sync . --apply`"
            ]
        return []
    except AgentsGatewayError as exc:
        return [f"root AGENTS.md gateway invalid: {exc}; repair with `go-workflow agents sync . --apply`"]
