#!/usr/bin/env python3
"""Repo-local Go Workflow Stack vNext spike CLI.

Operates project-local `.go/` JSON/JSONL state. The CLI is intentionally
clone-local: execution commands read/write the target repo's `.go/` directory,
not the Life OS vault's Agent Workflow Lite task queue.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_ROOT = SCRIPT_DIR.parent
if str(STACK_ROOT) not in sys.path:
    sys.path.insert(0, str(STACK_ROOT))

from go_workflow.constants import CURRENT_CONTRACT_VERSION, STACK_REF, STACK_VERSION
from go_workflow.migrations import plan_contract_migration
from go_workflow.adapter_protocol import build_adapter_request, normalize_adapter_result, validate_adapter_result
from go_workflow.adapters import native_agent_command
from go_workflow.routing import detected_platform, normalize_router_command, recommend_route
from go_workflow.task_state import open_task_records, task_path
from go_workflow.task_state import unfinished_task_ids as task_state_unfinished_task_ids
from go_workflow.stack_update import StackUpdateError, apply_stack_update, plan_stack_update, rollback_stack_update
from go_workflow.state_io import StateLockError, append_jsonl_locked, atomic_json, atomic_move_json, atomic_write_text, repository_lock
from go_workflow.hermes_proof import validate_live_hermes_proof, verify_live_hermes_evidence

CONTRACT_ROOT = STACK_ROOT
SCHEMA_ROOT = CONTRACT_ROOT / "schemas"
if not SCHEMA_ROOT.is_dir():
    SCHEMA_ROOT = Path(sys.prefix) / "schemas"
FIXTURE_ROOT = CONTRACT_ROOT / "fixtures" / "minimal" / ".go"
if not FIXTURE_ROOT.is_dir():
    FIXTURE_ROOT = Path(sys.prefix) / "fixtures" / "minimal" / ".go"

PROJECT_SCHEMA = "go-workflow.repo-local.project.v1"
ARCH_SCHEMA = "go-workflow.repo-local.architecture-principles.v1"
VISION_SCHEMA = "go-workflow.repo-local.vision.v1"
HIERARCHY_SCHEMA = "go-workflow.repo-local.hierarchy.v1"
TASK_SCHEMA = "go-workflow.repo-local.task.v1"
EVENT_SCHEMA = "go-workflow.repo-local.event.v1"
EXPORT_BUNDLE_SCHEMA = "go-workflow.repo-local.export-bundle.v1"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BLOCK_SECRET_RE = re.compile(r"(secret|token|credential|password|\.env|id_rsa|private[-_]key)", re.I)


class RepoLocalError(Exception):
    """Expected repo-local workflow failure."""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return slug or "project"


def parse_pipe_fields(value: str, expected: int, label: str) -> list[str]:
    parts = [part.strip() for part in value.split("|")]
    if len(parts) != expected or any(not part for part in parts):
        raise RepoLocalError(f"{label} must have {expected} pipe-separated non-empty fields")
    return parts


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepoLocalError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RepoLocalError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RepoLocalError(f"JSON root must be an object: {path}")
    return data


def dump_json(path: Path, data: dict[str, Any]) -> None:
    atomic_json(path, data)


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    append_jsonl_locked(path, event)


def require(condition: bool, errors: list[str], message: str) -> None:
    if not condition:
        errors.append(message)


def go_root(repo: Path) -> Path:
    return repo / ".go"


def relative(repo: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def validate_project(data: dict[str, Any], rel: str) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == PROJECT_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "project", errors, f"{rel}: kind must be project")
    require(bool(data.get("id")), errors, f"{rel}: id required")
    require(bool(data.get("name")), errors, f"{rel}: name required")
    require(data.get("source_of_truth") == "repo-local", errors, f"{rel}: source_of_truth must be repo-local")
    contract_version = data.get("contract_version", 1)
    require(isinstance(contract_version, int) and 1 <= contract_version <= CURRENT_CONTRACT_VERSION, errors, f"{rel}: contract_version must be between 1 and {CURRENT_CONTRACT_VERSION}")
    require(data.get("project_mode", "project") in {"project", "template"}, errors, f"{rel}: project_mode must be project or template")
    require(isinstance(data.get("default_verification"), list) and bool(data.get("default_verification")), errors, f"{rel}: default_verification must be a non-empty list")
    required_stack_version = data.get("required_stack_version")
    if required_stack_version is not None:
        require(bool(re.fullmatch(r"\d+\.\d+\.\d+", str(required_stack_version))), errors, f"{rel}: required_stack_version must be semantic version X.Y.Z")
    stack_ref = data.get("stack_ref")
    if stack_ref is not None:
        immutable_ref = bool(re.fullmatch(r"v\d+\.\d+\.\d+|[0-9a-f]{40}", str(stack_ref)))
        require(immutable_ref, errors, f"{rel}: stack_ref must be an immutable version tag (vX.Y.Z) or full commit SHA")
    return errors


def validate_architecture_principles(data: dict[str, Any], rel: str) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == ARCH_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "architecture_principles", errors, f"{rel}: kind must be architecture_principles")
    require(bool(data.get("project")), errors, f"{rel}: project required")
    principles = data.get("principles")
    require(isinstance(principles, list) and bool(principles), errors, f"{rel}: principles must be a non-empty list")
    if isinstance(principles, list):
        for index, principle in enumerate(principles, start=1):
            require(isinstance(principle, dict), errors, f"{rel}: principle {index} must be an object")
            if isinstance(principle, dict):
                for key in ("id", "statement", "rationale", "enforcement"):
                    require(bool(principle.get(key)), errors, f"{rel}: principle {index} missing {key}")
    return errors


def validate_vision(data: dict[str, Any], rel: str) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == VISION_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "vision", errors, f"{rel}: kind must be vision")
    require(data.get("status") in {"draft", "active", "superseded", "archived"}, errors, f"{rel}: invalid status")
    for key in ("project", "north_star", "wedge", "target_user", "core_promise"):
        require(bool(data.get(key)), errors, f"{rel}: {key} required")
    for key in ("product_principles", "non_goals", "success_metrics"):
        require(isinstance(data.get(key), list), errors, f"{rel}: {key} must be a list")
    return errors


def hierarchy_epics(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return canonical epics, accepting legacy feature_groups during migration."""
    epics = data.get("epics")
    if isinstance(epics, list):
        return [epic for epic in epics if isinstance(epic, dict)]
    groups = data.get("feature_groups")
    if isinstance(groups, list):
        return [group for group in groups if isinstance(group, dict)]
    return []


def set_hierarchy_epics(data: dict[str, Any], epics: list[dict[str, Any]]) -> None:
    data["epics"] = epics
    data.pop("feature_groups", None)


def validate_hierarchy(data: dict[str, Any], rel: str) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == HIERARCHY_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "hierarchy", errors, f"{rel}: kind must be hierarchy")
    require(bool(data.get("project")), errors, f"{rel}: project required")
    has_epics = isinstance(data.get("epics"), list)
    has_legacy_groups = isinstance(data.get("feature_groups"), list)
    require(has_epics or has_legacy_groups, errors, f"{rel}: epics must be a list")
    for epic in hierarchy_epics(data):
        require(bool(epic.get("id")) and bool(epic.get("title")), errors, f"{rel}: each epic needs id and title")
        require(isinstance(epic.get("tasks", []), list), errors, f"{rel}: epic {epic.get('id', '<missing>')} tasks must be a list")
        require(isinstance(epic.get("features", []), list), errors, f"{rel}: epic {epic.get('id', '<missing>')} features must be a list")
        for feature in epic.get("features", []):
            require(isinstance(feature, dict) and bool(feature.get("id")) and bool(feature.get("title")), errors, f"{rel}: each feature needs id and title")
            if isinstance(feature, dict):
                require(isinstance(feature.get("tasks", []), list), errors, f"{rel}: feature {feature.get('id', '<missing>')} tasks must be a list")
    return errors


def validate_task(data: dict[str, Any], rel: str, expected_status: str | None = None) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == TASK_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "task", errors, f"{rel}: kind must be task")
    task_id = str(data.get("id") or "")
    require(bool(TASK_ID_RE.fullmatch(task_id)), errors, f"{rel}: invalid id")
    require(data.get("status") in {"open", "active", "blocked", "done"}, errors, f"{rel}: invalid status")
    if expected_status:
        require(data.get("status") == expected_status, errors, f"{rel}: status must match directory {expected_status}")
    for key in ("project", "summary"):
        require(bool(data.get(key)), errors, f"{rel}: {key} required")
    for key in ("acceptance", "verification"):
        require(isinstance(data.get(key), list) and bool(data.get(key)), errors, f"{rel}: {key} must be a non-empty list")
    scope = data.get("scope")
    require(isinstance(scope, dict), errors, f"{rel}: scope must be an object")
    if isinstance(scope, dict):
        require(isinstance(scope.get("read"), list), errors, f"{rel}: scope.read must be a list")
        require(isinstance(scope.get("modify"), list), errors, f"{rel}: scope.modify must be a list")
    require(isinstance(data.get("claim"), dict), errors, f"{rel}: claim must be an object")
    require(data.get("execution_mode", "mechanical") in {"mechanical", "agent"}, errors, f"{rel}: execution_mode must be mechanical or agent")
    return errors


def validate_event(data: dict[str, Any], rel: str, line_number: int) -> list[str]:
    prefix = f"{rel}:{line_number}"
    errors: list[str] = []
    require(data.get("schema") == EVENT_SCHEMA, errors, f"{prefix}: schema mismatch")
    require(data.get("kind") == "event", errors, f"{prefix}: kind must be event")
    require(data.get("event") in {"task.claimed", "task.finished", "task.blocked", "evidence.appended", "decision.recorded", "run.checked", "auto.safety_gate", "auto.reflected", "auto.attempt"}, errors, f"{prefix}: invalid event")
    require(bool(data.get("created_at")), errors, f"{prefix}: created_at required")
    require(bool(data.get("task_id")), errors, f"{prefix}: task_id required")
    return errors


def validate_repo(repo: Path) -> list[str]:
    repo = repo.resolve()
    root = go_root(repo)
    errors: list[str] = []
    if not root.is_dir():
        return [f"missing .go directory: {root}"]
    validators = {
        "project.json": validate_project,
        "architecture-principles.json": validate_architecture_principles,
        "vision.json": validate_vision,
        "hierarchy.json": validate_hierarchy,
    }
    documents: dict[str, dict[str, Any]] = {}
    for filename, validator in validators.items():
        path = root / filename
        try:
            documents[filename] = load_json(path)
            errors.extend(validator(documents[filename], relative(repo, path)))
        except RepoLocalError as exc:
            errors.append(str(exc))
    project_id = str(documents.get("project.json", {}).get("id") or "")
    required_stack_version = str(documents.get("project.json", {}).get("required_stack_version") or "0.0.0")
    if semantic_version_tuple(STACK_VERSION) < semantic_version_tuple(required_stack_version):
        errors.append(f".go/project.json: requires go-workflow-stack >= {required_stack_version}, current runtime is {STACK_VERSION}")
    for filename in ("architecture-principles.json", "vision.json", "hierarchy.json"):
        document_project = documents.get(filename, {}).get("project")
        if project_id and document_project != project_id:
            errors.append(f".go/{filename}: project {document_project!r} does not match project.json id {project_id!r}")
    linked_task_ids: set[str] = set()
    hierarchy = documents.get("hierarchy.json", {})
    for epic in hierarchy_epics(hierarchy):
        linked_task_ids.update(str(task_id) for task_id in epic.get("tasks", []) if task_id)
        for feature in epic.get("features", []):
            if isinstance(feature, dict):
                linked_task_ids.update(str(task_id) for task_id in feature.get("tasks", []) if task_id)
    task_ids: set[str] = set()
    for status in ("open", "active", "blocked", "done"):
        for path in sorted((root / "tasks" / status).glob("*.json")):
            try:
                data = load_json(path)
                errors.extend(validate_task(data, relative(repo, path), expected_status=status))
                task_id = str(data.get("id") or "")
                if task_id in task_ids:
                    errors.append(f"{relative(repo, path)}: duplicate task id {task_id}")
                task_ids.add(task_id)
                if project_id and data.get("project") != project_id:
                    errors.append(f"{relative(repo, path)}: task project {data.get('project')!r} does not match project.json id {project_id!r}")
                if task_id and task_id not in linked_task_ids:
                    errors.append(f"{relative(repo, path)}: task {task_id!r} is not linked from hierarchy")
            except RepoLocalError as exc:
                errors.append(str(exc))
    for task_id in sorted(linked_task_ids - task_ids):
        errors.append(f".go/hierarchy.json: linked task {task_id!r} does not exist in any task state")
    for folder in ("runs", "evidence", "decisions"):
        for path in sorted((root / folder).glob("*.jsonl")):
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{relative(repo, path)}:{index}: invalid JSONL: {exc}")
                    continue
                if not isinstance(event, dict):
                    errors.append(f"{relative(repo, path)}:{index}: event must be an object")
                    continue
                errors.extend(validate_event(event, relative(repo, path), index))
    return errors


def copy_fixture_init(repo: Path, force: bool = False) -> None:
    root = go_root(repo)
    if root.exists() and any(root.iterdir()) and not force:
        raise RepoLocalError(f"refusing to overwrite existing non-empty {root}; pass --force for spike fixtures")
    root.mkdir(parents=True, exist_ok=True)
    for source in FIXTURE_ROOT.rglob("*"):
        if source.is_dir():
            continue
        target = root / source.relative_to(FIXTURE_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def ensure_go_dirs(root: Path) -> None:
    for rel in [
        "tasks/open",
        "tasks/active",
        "tasks/blocked",
        "tasks/done",
        "runs",
        "evidence",
        "decisions",
        "imports",
        "locks",
    ]:
        (root / rel).mkdir(parents=True, exist_ok=True)


def parse_principles(values: list[str]) -> list[dict[str, str]]:
    if not values:
        values = [
            "repo-local-state|Project workflow state lives in .go/ next to code.|A fresh clone should explain current direction and next work.|go-workflow-stack validate/readback",
            "json-first|Current state is JSON and append-only history is JSONL.|Agents need deterministic, diffable contracts.|schema validation and CLI checks",
        ]
    principles = []
    for value in values:
        pid, statement, rationale, enforcement = parse_pipe_fields(value, 4, "--principle")
        principles.append({"id": slugify(pid), "statement": statement, "rationale": rationale, "enforcement": enforcement})
    return principles


def parse_hierarchy(feature_groups: list[str], features: list[str], project_id: str) -> dict[str, Any]:
    epics: dict[str, dict[str, Any]] = {}
    for value in feature_groups or ["workflow|Workflow"]:
        gid, title = parse_pipe_fields(value, 2, "--feature-group")
        epics[slugify(gid)] = {"id": slugify(gid), "title": title, "features": [], "tasks": []}
    for value in features or ["workflow|repo-local-workflow|Repo-local workflow"]:
        gid, fid, title = parse_pipe_fields(value, 3, "--feature")
        gid = slugify(gid)
        if gid not in epics:
            epics[gid] = {"id": gid, "title": gid.replace("-", " ").title(), "features": [], "tasks": []}
        epics[gid]["features"].append({"id": slugify(fid), "title": title, "tasks": []})
    return {"schema": HIERARCHY_SCHEMA, "kind": "hierarchy", "project": project_id, "epics": list(epics.values())}


def append_task_to_epic(root: Path, epic_id: str, task_id: str) -> None:
    if not epic_id:
        return
    path = root / "hierarchy.json"
    hierarchy = load_json(path)
    epics = hierarchy_epics(hierarchy)
    for epic in epics:
        if epic.get("id") != slugify(epic_id):
            continue
        tasks = epic.setdefault("tasks", [])
        if task_id not in tasks:
            tasks.append(task_id)
        set_hierarchy_epics(hierarchy, epics)
        dump_json(path, hierarchy)
        return
    raise RepoLocalError(f"epic not found in hierarchy: {epic_id}")


def append_task_to_hierarchy(root: Path, feature_ref: str, task_id: str) -> None:
    if not feature_ref:
        return
    if "." not in feature_ref:
        raise RepoLocalError("--feature must be formatted as epic_id.feature_id")
    group_id, feature_id = [slugify(part) for part in feature_ref.split(".", 1)]
    path = root / "hierarchy.json"
    hierarchy = load_json(path)
    epics = hierarchy_epics(hierarchy)
    for epic in epics:
        if epic.get("id") != group_id:
            continue
        for feature in epic.get("features", []):
            if feature.get("id") != feature_id:
                continue
            tasks = feature.setdefault("tasks", [])
            if task_id not in tasks:
                tasks.append(task_id)
            set_hierarchy_epics(hierarchy, epics)
            dump_json(path, hierarchy)
            return
    raise RepoLocalError(f"feature not found in hierarchy: {feature_ref}")


def find_task(root: Path, task_id: str) -> tuple[Path, dict[str, Any]]:
    matches: list[Path] = []
    for status in ("open", "active", "blocked", "done"):
        path = task_path(root, status, task_id)
        if path.exists():
            matches.append(path)
    if not matches:
        raise RepoLocalError(f"task not found: {task_id}")
    if len(matches) > 1:
        raise RepoLocalError(f"task exists in multiple status dirs: {task_id}")
    return matches[0], load_json(matches[0])


def open_tasks(repo: Path) -> list[tuple[Path, dict[str, Any]]]:
    return open_task_records(go_root(repo))


def path_matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) or path == pattern for pattern in patterns)


def git_status(repo: Path) -> list[tuple[str, str]]:
    result = subprocess.run(["git", "status", "--porcelain"], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return []
    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        code = line[:2]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append((code, path))
    return entries


def managed_task_transition(repo: Path, path: str) -> bool:
    if not path.startswith(".go/tasks/"):
        return False
    root = go_root(repo)
    return any((root / "tasks" / state / Path(path).name).is_file() for state in ("open", "active", "blocked", "done"))


def classify_dirty(repo: Path, owned_patterns: list[str]) -> dict[str, list[str]]:
    result = {"blocking": [], "report_only": []}
    for code, path in git_status(repo):
        reason = ""
        if "U" in code or code in {"AA", "DD"}:
            reason = "merge conflict"
        elif BLOCK_SECRET_RE.search(path):
            reason = "secret-looking path"
        elif (code.strip().startswith("D") or code.endswith("D")) and not managed_task_transition(repo, path):
            reason = "delete requires explicit review"
        elif path.startswith(".go/locks/"):
            reason = "workflow lock state"
        elif path_matches(path, owned_patterns):
            reason = "owned-path dirty state"
        if reason:
            result["blocking"].append(f"{code} {path} — {reason}")
        else:
            result["report_only"].append(f"{code} {path} — unrelated dirty state")
    return result


def event(task_id: str, event_name: str, agent: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schema": EVENT_SCHEMA, "kind": "event", "event": event_name, "created_at": now_iso(), "task_id": task_id, "agent": agent, "data": data or {}}


def write_missing_text(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def ensure_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        result = subprocess.run(["git", "init", "-q"], cwd=repo, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise RepoLocalError(result.stderr.strip() or "git init failed")


def write_repo_complete_starter(repo: Path, name: str) -> list[str]:
    created: list[str] = []
    files = {
        "README.md": f"# {name}\n\nRepo-local agent workflow project.\n\n## Development\n\n```bash\nmake check\n```\n",
        ".gitignore": ".env\n.env.*\n.DS_Store\n__pycache__/\n.pytest_cache/\nnode_modules/\ndist/\nbuild/\n",
        "LICENSE": "MIT License\n\nCopyright (c) 2026 Viggo Meesters\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\nof this software and associated documentation files (the \"Software\"), to deal\nin the Software without restriction, including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.\n",
        "SECURITY.md": "# Security\n\nDo not commit credentials, private data, tokens, cookies, or production secrets.\n",
        "CONTRIBUTING.md": "# Contributing\n\nUse repo-local `.go/` tasks, verify before finishing, and keep changes scoped.\n",
        "CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n- Initial repo-local spike scaffold.\n",
        "Makefile": "GO_STACK ?= ../go-workflow-stack\n\n.PHONY: check\ncheck:\n\tpython3 $(GO_STACK)/cli/go.py validate .\n\tpython3 $(GO_STACK)/cli/go.py readback .\n",
        "scripts/check.sh": "#!/usr/bin/env bash\nset -euo pipefail\nmake check\n",
        "go": "#!/usr/bin/env bash\nset -euo pipefail\nREPO_ROOT=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\nSTACK=\"${GO_STACK:-}\"\nif [ -z \"$STACK\" ] || [ ! -f \"$STACK/cli/go.py\" ]; then\n  for candidate in \"$REPO_ROOT/../go-workflow-stack\" \"$HOME/github/go-workflow-stack\" \"$HOME/Dev/go-workflow-stack\"; do\n    if [ -f \"$candidate/cli/go.py\" ]; then STACK=\"$candidate\"; break; fi\n  done\nfi\nif [ -z \"$STACK\" ] || [ ! -f \"$STACK/cli/go.py\" ]; then\n  echo \"go-workflow-stack not found; set GO_STACK or clone it beside this repository\" >&2\n  exit 2\nfi\nexport GO_STACK=\"$STACK\"\nexec python3 \"$STACK/cli/go.py\" \"$@\"\n",
    }
    for rel, content in files.items():
        if write_missing_text(repo / rel, content):
            created.append(rel)
    check = repo / "scripts" / "check.sh"
    if check.exists():
        check.chmod(check.stat().st_mode | 0o111)
    launcher = repo / "go"
    if launcher.exists():
        launcher.chmod(launcher.stat().st_mode | 0o111)
    return created


def parse_spike_task(value: str) -> tuple[str, str]:
    task_id, summary = parse_pipe_fields(value, 2, "--task")
    return slugify(task_id), summary


def default_spike_tasks() -> list[tuple[str, str]]:
    return [
        ("write-vision", "Write or refine the repo-local vision"),
        ("write-architecture-principles", "Write architecture principles and constraints"),
        ("repo-complete", "Bootstrap repo-complete hygiene and local checks"),
        ("implementation-slice", "Build the first working implementation slice"),
        ("verification", "Run focused and repository verification"),
        ("hardening-devil-review", "Run recheck, devil review, and hardening pass"),
        ("self-reflect", "Self-reflect on architecture and workflow improvements"),
        ("handoff-summary", "Write concise user handoff and next steps"),
    ]


def cmd_adopt(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    repo.mkdir(parents=True, exist_ok=True)
    root = go_root(repo)
    if root.exists() and any(root.iterdir()) and not args.force:
        raise RepoLocalError(f"{root} already exists; use status/task create, or pass --force to replace")
    if args.force and root.exists():
        shutil.rmtree(root)
    ensure_go_dirs(root)
    project_id = slugify(args.project_id or repo.name)
    name = args.name or repo.name
    default_verification = args.verification or ["git diff --check"]
    dump_json(root / "project.json", {
        "schema": PROJECT_SCHEMA,
        "kind": "project",
        "id": project_id,
        "name": name,
        "source_of_truth": "repo-local",
        "contract_version": CURRENT_CONTRACT_VERSION,
        "project_mode": "project",
        "required_stack_version": STACK_VERSION,
        "stack_ref": STACK_REF,
        "default_verification": default_verification,
        "links": {"repo": args.repo_url or ""},
    })
    dump_json(root / "architecture-principles.json", {
        "schema": ARCH_SCHEMA,
        "kind": "architecture_principles",
        "project": project_id,
        "principles": parse_principles(args.principle or []),
    })
    dump_json(root / "vision.json", {
        "schema": VISION_SCHEMA,
        "kind": "vision",
        "project": project_id,
        "status": "active",
        "north_star": args.north_star or f"{name} is understandable and operable from its repo-local .go contract.",
        "wedge": args.wedge or "Repo-local project state with clone-readable agent continuity.",
        "target_user": args.target_user or "Project maintainers and future agents.",
        "core_promise": args.core_promise or "A fresh clone can explain current direction, constraints, hierarchy, and next work.",
        "product_principles": args.product_principle or ["repo-local", "json-first", "proof-first"],
        "non_goals": args.non_goal or ["no central execution database", "no broad migration by default"],
        "success_metrics": args.success_metric or ["go-workflow-stack validate passes", "go-workflow-stack readback is useful"],
    })
    dump_json(root / "hierarchy.json", parse_hierarchy(args.feature_group or [], args.feature or [], project_id))
    for jsonl in [root / "runs" / "events.jsonl", root / "evidence" / "events.jsonl", root / "decisions" / "events.jsonl"]:
        jsonl.touch(exist_ok=True)
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"adopted: {root}")
    return 0


def spike_task_scope(scope_name: str) -> dict[str, list[str]]:
    if scope_name == "docs":
        return {"read": [".go/**", "README.md", "docs/**"], "modify": [".go/**", "README.md", "docs/**", "Makefile"]}
    return {
        "read": [".go/**", "README.md", "docs/**", "cli/go.py", "tests/**", "Makefile"],
        "modify": [".go/**", "README.md", "docs/**", "cli/go.py", "tests/**", "Makefile"],
    }


def cmd_spike(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    ensure_git_repo(repo)
    name = args.name or repo.name.replace("-", " ").title()
    project_id = slugify(args.project_id or repo.name)
    root = go_root(repo)
    created_repo_files: list[str] = [] if args.skip_repo_complete else write_repo_complete_starter(repo, name)
    if (root / "project.json").is_file():
        existing_project = load_json(root / "project.json")
        if existing_project.get("id") == "go-project-template" and project_id != "go-project-template":
            shutil.rmtree(root)
    if not (root / "project.json").exists():
        ensure_go_dirs(root)
        dump_json(root / "project.json", {
            "schema": PROJECT_SCHEMA,
            "kind": "project",
            "id": project_id,
            "name": name,
            "source_of_truth": "repo-local",
            "contract_version": CURRENT_CONTRACT_VERSION,
            "project_mode": "project",
            "required_stack_version": STACK_VERSION,
            "stack_ref": STACK_REF,
            "default_verification": args.verification or ["make check"],
            "links": {"repo": args.repo_url or ""},
        })
        dump_json(root / "architecture-principles.json", {
            "schema": ARCH_SCHEMA,
            "kind": "architecture_principles",
            "project": project_id,
            "principles": parse_principles(args.principle or []),
        })
        brief = args.brief or f"{name} repo-local spike."
        dump_json(root / "vision.json", {
            "schema": VISION_SCHEMA,
            "kind": "vision",
            "project": project_id,
            "status": "active",
            "north_star": args.north_star or f"{name} can be designed, built, verified, and continued from repo-local .go state.",
            "wedge": args.wedge or brief,
            "target_user": args.target_user or "Viggo and future autonomous agents continuing this project.",
            "core_promise": args.core_promise or "A single go spike/go auto loop can turn rough intent into repo, vision, principles, tasks, verified work, reflection, and concise handoff.",
            "product_principles": args.product_principle or ["repo-local", "proof-first", "autonomous-but-scoped", "feedback-turns-into-tasks"],
            "non_goals": args.non_goal or ["no hidden central execution state", "no unbounded public/destructive actions", "no technical fluff handoff"],
            "success_metrics": args.success_metric or ["repo validates", "next tasks are claimable", "go auto plan is explicit"],
        })
        epics = args.epic or ["delivery|Delivery"]
        dump_json(root / "hierarchy.json", parse_hierarchy(epics, [], project_id))
        for jsonl in [root / "runs" / "events.jsonl", root / "evidence" / "events.jsonl", root / "decisions" / "events.jsonl"]:
            jsonl.touch(exist_ok=True)
    else:
        errors = validate_repo(repo)
        if errors:
            raise RepoLocalError("existing .go state is invalid:\n- " + "\n- ".join(errors))
        project_id = str(load_json(root / "project.json").get("id") or project_id)
        for epic_value in args.epic or []:
            epic_id, title = parse_pipe_fields(epic_value, 2, "--epic")
            hierarchy = load_json(root / "hierarchy.json")
            epics = hierarchy_epics(hierarchy)
            if not any(epic.get("id") == slugify(epic_id) for epic in epics):
                epics.append({"id": slugify(epic_id), "title": title, "features": [], "tasks": []})
                set_hierarchy_epics(hierarchy, epics)
                dump_json(root / "hierarchy.json", hierarchy)
    hierarchy = load_json(root / "hierarchy.json")
    epics = hierarchy_epics(hierarchy)
    if not epics:
        epics = [{"id": "delivery", "title": "Delivery", "features": [], "tasks": []}]
        set_hierarchy_epics(hierarchy, epics)
        dump_json(root / "hierarchy.json", hierarchy)
    target_epic = slugify(args.target_epic or str(epics[0]["id"]))
    task_values = [parse_spike_task(value) for value in args.task] if args.task else default_spike_tasks()
    default_task_scope = spike_task_scope(args.task_scope)
    created_tasks: list[str] = []
    project = load_json(root / "project.json")
    for order, (task_id, summary) in enumerate(task_values, start=1):
        if any(task_path(root, state, task_id).exists() for state in ("open", "active", "blocked", "done")):
            continue
        task = {
            "schema": TASK_SCHEMA,
            "kind": "task",
            "execution_mode": args.execution_mode,
            "id": task_id,
            "project": project_id,
            "status": "open",
            "summary": summary,
            "description": summary,
            "order": order,
            "scope": default_task_scope,
            "acceptance": [
                f"Outcome is observable: {summary}.",
                "All task verification commands pass and evidence is recorded.",
            ],
            "verification": project.get("default_verification") or ["make check"],
            "claim": {"agent": None, "claimed_at": None},
            "evidence": [],
        }
        dump_json(task_path(root, "open", task_id), task)
        append_task_to_epic(root, target_epic, task_id)
        created_tasks.append(task_id)
    append_jsonl(root / "decisions" / "events.jsonl", event(
        "go-spike",
        "decision.recorded",
        args.agent,
        {
            "decision_id": "go-spike-created",
            "title": "Initialize go spike contract",
            "status": "accepted",
            "context": args.brief or "go spike command",
            "decision": "Use repo-local .go vision, principles, epics, tasks, evidence, and go auto loop for this project.",
            "consequences": ["Feedback becomes tasks", "go auto can continue from repo state"],
        },
    ))
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("spike produced invalid .go state:\n- " + "\n- ".join(errors))
    result = {
        "repo": str(repo),
        "project_id": project_id,
        "created_repo_files": created_repo_files,
        "created_tasks": created_tasks,
        "next": None if not open_tasks(repo) else open_tasks(repo)[0][1]["id"],
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"spike: {repo}")
        print(f"project: {project_id}")
        print("created_tasks: " + (", ".join(created_tasks) if created_tasks else "none"))
        print("next: " + str(result["next"] or "none"))
    return 0


def arg_int(args: argparse.Namespace, name: str, default: int) -> int:
    return int(getattr(args, name, default) or default)


def build_loop_plan(repo: Path, args: argparse.Namespace, mode: str = "go-auto") -> dict[str, Any]:
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError(f"cannot run {mode} on invalid .go state:\n- " + "\n- ".join(errors))
    root = go_root(repo)
    project = load_json(root / "project.json")
    max_tasks = max(arg_int(args, "max_tasks", 3), 1)
    max_minutes = max(arg_int(args, "max_minutes", 45), 1)
    max_commands = max(arg_int(args, "max_commands", max_tasks * 12), 1)
    command_timeout_seconds = max(arg_int(args, "command_timeout_seconds", 900), 1)
    checkpoint_every_tasks = max(arg_int(args, "checkpoint_every_tasks", 1), 1)
    tasks = [task[1] for task in open_tasks(repo)[:max_tasks]]
    is_loop = mode == "go-loop"
    stop_conditions = [
        "no_open_tasks_and_no_self_reflect_follow_up",
        "repository_safety_gate",
        "external_authority_required",
        "outcome_ambiguity",
        "scope_tradeoff_requires_direction",
    ]
    execution_policy = {
        "ask_policy": "do-not-ask-when-safe-default-exists",
        "authority": "high-autonomy-bounded-by-repo-scope-and-human-gates",
        "may_create_follow_up_tasks": True,
        "may_continue_after_self_reflect": True,
        "may_escalate_to_go_loop": not is_loop,
        "allowed_autonomous_actions": [
            "claim_and_execute_open_tasks",
            "edit_files_within_task_scope",
            "run_tests_checks_and_smokes",
            "fix_verification_failures_within_scope",
            "run_recheck_devil_hardening",
            "append_evidence_and_decisions",
            "create_same_scope_follow_up_tasks",
            "summarize_compactly_without_waiting_for_prompt",
        ],
        "human_gates": stop_conditions[1:],
    }
    preflight = build_auto_preflight(repo, tasks, max_tasks)
    run_envelope = {
        "schema": "go-workflow.auto-run-envelope.v1",
        "result_schema": "go-workflow.auto-run-result.v1",
        "run_until": "done_or_blocker_or_budget_or_safety_gate",
        "budget": {"max_tasks": max_tasks, "max_minutes": max_minutes, "max_commands": max_commands, "command_timeout_seconds": command_timeout_seconds, "checkpoint_every_tasks": checkpoint_every_tasks, "summary_chars": args.summary_chars},
        "preflight": preflight,
        "checkpoint_after": ["checkpoint_every_tasks", "blocker", "budget_exhausted", "safety_gate", "done"],
        "telegram_policy": {"default": "silent_until_done_blocker_or_checkpoint", "checkpoint_every_tasks": checkpoint_every_tasks, "summary_chars": args.summary_chars},
        "final_result_fields": ["status", "completed_tasks", "blocked_task", "evidence", "checks", "completion_audit", "summary", "next_action"],
    }
    return {
        "mode": mode,
        "repo": str(repo),
        "project_id": project.get("id"),
        "next_tasks": [task["id"] for task in tasks],
        "control_handoff": True,
        "autonomy": "control-handed-off-until-blocker" if is_loop else "high-autonomy-bounded-batch-with-loop-escalation",
        "can_escalate_to": [] if is_loop else ["go-loop"],
        "continues_beyond_initial_tasks": is_loop,
        "execution_policy": execution_policy,
        "run_envelope": run_envelope,
        "loop": ["route", "status", "contract-repair-if-needed", "next-or-create-task", "claim", "execute", "verify", "recheck", "devil", "repair", "verify", "commit-or-ship", "finish", "self-reflect", "continue-or-block"],
        "agent_contract": {
            "execute": "The invoking coding agent does not hand commands back to Viggo. It starts tool calls now: repair or confirm .go contract, create/claim one task, execute inside scope, verify, critic/recheck, repair if needed, finish with evidence, then continue until done, a repository gate, or budget.",
            "contract_preflight": "Before implementation, ensure vision/end goal, architecture principles, hierarchy, executable task, acceptance, and verification are present or create/repair them.",
            "control": "Viggo has handed off control with go/go-auto: do not stop after one phase and wait for another go; keep executing until done, blocker, budget, or safety gate.",
            "loop_escalation": "go-auto may invoke go-loop when self-reflect creates follow-up work, verification/review fails, first green is not trustworthy, or the project needs continued autonomous repair beyond the initial batch.",
            "feedback": "New Viggo input is converted into .go tasks/decisions before another go auto/go loop pass.",
            "summary_max_chars": args.summary_chars,
            "telegram_policy": "quiet until done/blocker/checkpoint; do not stream command transcripts",
            "stop_conditions": stop_conditions,
        },
    }


def task_contract_findings(task: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    acceptance = task.get("acceptance") or []
    generic_acceptance = {
        "task result is implemented and verified.",
        "task result is implemented and verified with evidence.",
    }
    if not acceptance:
        findings.append("acceptance criteria are missing")
    elif any(str(item).strip().lower() in generic_acceptance for item in acceptance):
        findings.append("acceptance criteria are generic and cannot prove the requested outcome")
    if not task.get("verification"):
        findings.append("verification commands are missing")
    scope = task.get("scope") or {}
    if not isinstance(scope, dict) or not isinstance(scope.get("modify"), list):
        findings.append("task modify scope is missing or invalid")
    return findings


def unfinished_task_ids(repo: Path) -> dict[str, list[str]]:
    return task_state_unfinished_task_ids(go_root(repo))


def build_auto_preflight(repo: Path, selected_tasks: list[dict[str, Any]], max_tasks: int) -> dict[str, Any]:
    root = go_root(repo)
    dirty_entries: list[str] = []
    blockers: list[str] = []
    for code, path in git_status(repo):
        entry = f"{code} {path}"
        dirty_entries.append(entry)
        conflict = "U" in code or code in {"AA", "DD"}
        secret_like = bool(BLOCK_SECRET_RE.search(path))
        destructive = code.strip().startswith("D") or code.endswith("D")
        task_transition = destructive and managed_task_transition(repo, path)
        lock_state = path.startswith(".go/locks/")
        if conflict:
            blockers.append(f"{entry} — merge conflict")
        elif secret_like:
            blockers.append(f"{entry} — secret-looking path")
        elif destructive and not task_transition:
            blockers.append(f"{entry} — delete requires explicit review")
        elif lock_state:
            blockers.append(f"{entry} — workflow lock state")
    lock_files = [] if not (root / "locks").is_dir() else [relative(repo, path) for path in sorted((root / "locks").glob("*")) if path.is_file()]
    for lock in lock_files:
        blockers.append(f"{lock} — active workflow lock")
    contract_findings = [
        {"task_id": task.get("id"), "findings": findings}
        for task in selected_tasks
        if (findings := task_contract_findings(task))
    ]
    unfinished = unfinished_task_ids(repo)
    return {
        "valid_go_state": True,
        "open_task_count": len(open_tasks(repo)),
        "selected_task_count": len(selected_tasks),
        "max_tasks": max_tasks,
        "dirty_entries": dirty_entries,
        "lock_files": lock_files,
        "human_gate_required": bool(blockers),
        "human_gate_blockers": blockers,
        "contract_gate_required": bool(contract_findings),
        "contract_findings": contract_findings,
        "unfinished_tasks": unfinished,
    }


def run_shell_with_timeout(repo: Path, command: str, env: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
    process = subprocess.Popen(
        command,
        cwd=repo,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr, "timed_out": False}
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        return {
            "returncode": 124,
            "stdout": stdout,
            "stderr": (stderr + f"\ncommand timed out after {timeout_seconds}s").strip(),
            "timed_out": True,
        }


def run_verification_commands(repo: Path, task: dict[str, Any], command_budget: int | None = None, timeout_seconds: int = 900) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    commands_run = 0
    for command in task.get("verification", []) or []:
        if command_budget is not None and commands_run >= command_budget:
            checks.append({
                "task_id": task.get("id"),
                "command": command,
                "returncode": 124,
                "stdout": "",
                "stderr": "command budget exhausted before verification command",
                "budget_exhausted": True,
            })
            break
        env = os.environ.copy()
        with tempfile.TemporaryDirectory(prefix="go-verify-pycache-") as pycache_dir:
            env["PYTHONPYCACHEPREFIX"] = pycache_dir
            completed = run_shell_with_timeout(repo, command, env, timeout_seconds)
        commands_run += 1
        checks.append({
            "task_id": task.get("id"),
            "command": command,
            "returncode": completed["returncode"],
            "stdout": completed["stdout"][-2000:],
            "stderr": completed["stderr"][-2000:],
            "timed_out": completed["timed_out"],
        })
        if completed["returncode"] != 0:
            break
    if not checks:
        checks.append({"task_id": task.get("id"), "command": None, "returncode": 0, "stdout": "", "stderr": "no verification commands configured"})
    return checks


def finish_task_record(repo: Path, root: Path, active_path: Path, task: dict[str, Any], agent: str, evidence_summary: str) -> Path:
    with repository_lock(root, f"task-{task['id']}"):
        if not active_path.is_file():
            raise StateLockError(f"active task disappeared before finish: {task['id']}")
        task["status"] = "done"
        task.setdefault("evidence", []).append({"created_at": now_iso(), "agent": agent, "summary": evidence_summary})
        target = task_path(root, "done", task["id"])
        atomic_move_json(active_path, target, task)
        append_jsonl(root / "evidence" / "events.jsonl", event(task["id"], "task.finished", agent, {"evidence": evidence_summary}))
    return target


def restore_active_after_failed_ship(
    active_path: Path,
    done_path: Path,
    active_task: dict[str, Any],
    evidence_path: Path,
    evidence_before: str,
) -> None:
    atomic_move_json(done_path, active_path, active_task)
    atomic_write_text(evidence_path, evidence_before)


def block_task_record(repo: Path, root: Path, active_path: Path, task: dict[str, Any], agent: str, reason: str, checks: list[dict[str, Any]]) -> Path:
    with repository_lock(root, f"task-{task['id']}"):
        if not active_path.is_file():
            raise StateLockError(f"active task disappeared before block: {task['id']}")
        task["status"] = "blocked"
        task["blocked"] = {"created_at": now_iso(), "agent": agent, "reason": reason}
        target = task_path(root, "blocked", task["id"])
        atomic_move_json(active_path, target, task)
        append_jsonl(root / "runs" / "events.jsonl", event(task["id"], "task.blocked", agent, {"reason": reason, "checks": checks}))
    return target


def format_hook_command(command: str, repo: Path, task: dict[str, Any], attempt: int, strategy: str) -> str:
    replacements = {
        "{repo}": str(repo),
        "{repo_shell}": shell_quote(str(repo)),
        "{task_id}": str(task.get("id", "unknown")),
        "{attempt}": str(attempt),
        "{strategy}": strategy,
    }
    rendered = command
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def build_execution_context(repo: Path, task: dict[str, Any]) -> dict[str, Any]:
    root = go_root(repo)
    return {
        "schema": "go-workflow.execution-context.v1",
        "project": load_json(root / "project.json"),
        "vision": load_json(root / "vision.json"),
        "architecture_principles": load_json(root / "architecture-principles.json"),
        "hierarchy": load_json(root / "hierarchy.json"),
        "task": task,
        "recent_evidence": load_jsonl_events(root / "evidence" / "events.jsonl", limit=10),
        "recent_decisions": load_jsonl_events(root / "decisions" / "events.jsonl", limit=10),
    }


def run_hook_command(repo: Path, command: str, task: dict[str, Any], attempt: int, strategy: str, hook: str, timeout_seconds: int = 900, require_protocol: bool = False) -> dict[str, Any]:
    rendered = format_hook_command(command, repo, task, attempt, strategy)
    context = build_execution_context(repo, task)
    request = build_adapter_request(repo, task, context, hook, attempt, strategy)
    env = os.environ.copy()
    env.update({
        "GO_REPO": str(repo),
        "GO_TASK_ID": str(task.get("id", "unknown")),
        "GO_TASK_JSON": json.dumps(task, ensure_ascii=False),
        "GO_CONTEXT_JSON": json.dumps(context, ensure_ascii=False),
        "GO_ADAPTER_REQUEST_JSON": json.dumps(request, ensure_ascii=False),
        "GO_ATTEMPT": str(attempt),
        "GO_STRATEGY": strategy,
        "GO_HOOK": hook,
    })
    completed = run_shell_with_timeout(repo, rendered, env, timeout_seconds)
    return normalize_adapter_result(hook, rendered, completed, require_protocol=require_protocol)


def arg_str(args: argparse.Namespace, name: str, default: str = "") -> str:
    value = getattr(args, name, default)
    return value or default


def git_status_paths(repo: Path) -> list[str]:
    completed = subprocess.run(["git", "status", "--porcelain"], cwd=repo, text=True, capture_output=True)
    if completed.returncode != 0:
        return []
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        raw = line[3:]
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        paths.append(raw.strip().strip('"'))
    return paths


def worktree_path_fingerprint(repo: Path, path: str) -> str:
    target = repo / path
    digest = hashlib.sha256()
    if target.is_symlink():
        return "symlink:" + os.readlink(target)
    if not target.exists():
        return "missing"
    if target.is_file():
        try:
            digest.update(target.read_bytes())
            return "file:" + digest.hexdigest()
        except OSError as exc:
            return f"unreadable:{type(exc).__name__}:{exc}"
    for child in sorted(item for item in target.rglob("*") if not item.is_dir()):
        digest.update(str(child.relative_to(target)).encode("utf-8", errors="surrogateescape"))
        if child.is_symlink():
            digest.update(os.readlink(child).encode("utf-8", errors="surrogateescape"))
            continue
        try:
            digest.update(child.read_bytes())
        except OSError as exc:
            digest.update(f"unreadable:{type(exc).__name__}:{exc}".encode())
    return "dir:" + digest.hexdigest()


def git_dirty_snapshot(repo: Path) -> dict[str, str]:
    return {path: worktree_path_fingerprint(repo, path) for path in git_status_paths(repo)}


def allowed_runtime_path(path: str) -> bool:
    return path.startswith((
        ".go/tasks/",
        ".go/runs/",
        ".go/evidence/",
        ".go/reflections/",
        ".go/locks/",
    ))


def task_allowed_paths(task: dict[str, Any]) -> set[str]:
    scope = task.get("scope", {}) or {}
    return {path.strip("/") for path in (scope.get("modify", []) or []) if path}


def path_allowed_by_task(path: str, task: dict[str, Any], allow_runtime: bool = True) -> bool:
    clean = path.strip("/")
    if allow_runtime and allowed_runtime_path(clean):
        return True
    for allowed in task_allowed_paths(task):
        if fnmatch.fnmatch(clean, allowed) or clean == allowed or clean.startswith(allowed.rstrip("/") + "/"):
            return True
    return False


def ignorable_generated_path(path: str) -> bool:
    clean = path.strip("/")
    return clean == "__pycache__" or clean.startswith("__pycache__/") or "/__pycache__/" in clean or clean.startswith(".pytest_cache/")


def scope_violations(repo: Path, task: dict[str, Any]) -> list[str]:
    return [path for path in git_status_paths(repo) if not ignorable_generated_path(path) and not path_allowed_by_task(path, task)]


def scope_violations_after(repo: Path, task: dict[str, Any], before: dict[str, str]) -> list[str]:
    after = git_dirty_snapshot(repo)
    return [
        path for path, fingerprint in after.items()
        if before.get(path) != fingerprint
        and not ignorable_generated_path(path)
        and not path_allowed_by_task(path, task)
    ]


def ensure_budget(result: dict[str, Any], max_commands: int, started_at: float, max_minutes: int, stage: str) -> bool:
    elapsed_minutes = (time.monotonic() - started_at) / 60
    if result["commands_run"] >= max_commands or elapsed_minutes >= max_minutes:
        result.update({
            "status": "budget_exhausted",
            "budget_exhausted": True,
            "summary": f"Budget exhausted before {stage}.",
            "next_action": "resume with go-loop using a larger budget or narrower task scope",
        })
        return False
    return True


def shell_quote(value: str) -> str:
    return shlex.quote(str(value))


def attempt_markdown(task: dict[str, Any], attempt: dict[str, Any], context: dict[str, Any]) -> str:
    vision = context.get("vision") or {}
    principles = (context.get("architecture_principles") or {}).get("principles") or []
    hierarchy = context.get("hierarchy") or {}
    epic_ids = [str(epic.get("id")) for epic in hierarchy.get("epics", []) if epic.get("id")]
    principle_lines = [
        f"- `{principle.get('id', 'unnamed')}`: {principle.get('statement', '')}"
        for principle in principles
    ] or ["- none declared"]
    return "\n".join([
        f"# Attempt {attempt.get('attempt')} — {task.get('id')}",
        "",
        f"Strategy: `{attempt.get('strategy')}`",
        "",
        "## Project contract",
        "",
        f"North star: {vision.get('north_star', '')}",
        "",
        "Success metrics:",
        *[f"- {metric}" for metric in vision.get("success_metrics", [])],
        "",
        "Architecture principles:",
        *principle_lines,
        "",
        f"Hierarchy epics: {', '.join(epic_ids) if epic_ids else 'none declared'}",
        "",
        "## Task",
        "",
        f"Summary: {task.get('summary', '')}",
        "",
        str(task.get('description', '')),
        "",
        "## Acceptance",
        "",
        *[f"- {item}" for item in task.get("acceptance", [])],
        "",
        "## Verification",
        "",
        *[f"- `{item}`" for item in task.get("verification", [])],
        "",
    ])


def checks_log(checks: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for check in checks:
        chunks.extend([
            f"$ {check.get('command')}",
            f"returncode: {check.get('returncode')}",
            "stdout:",
            str(check.get("stdout", "")),
            "stderr:",
            str(check.get("stderr", "")),
            "",
        ])
    return "\n".join(chunks)


def critic_markdown(attempt: dict[str, Any]) -> str:
    critic = attempt.get("critic", {})
    findings = critic.get("blocking_findings") or []
    lines = [f"# Critic — {attempt.get('task_id')} attempt {attempt.get('attempt')}", "", f"Status: `{critic.get('status')}`", "", "## Blocking findings"]
    lines.extend(f"- {finding}" for finding in findings)
    if not findings:
        lines.append("- none")
    if critic.get("repair_hint"):
        lines.extend(["", "## Repair hint", str(critic.get("repair_hint"))])
    if critic.get("result"):
        lines.extend(["", "## Adapter result", "```json", json.dumps(critic.get("result"), indent=2), "```"])
    return "\n".join(lines) + "\n"


def git_diff_text(repo: Path) -> str:
    completed = subprocess.run(["git", "diff", "--no-ext-diff", "--"], cwd=repo, text=True, capture_output=True)
    return completed.stdout if completed.returncode == 0 else completed.stderr


def record_attempt(repo: Path, root: Path, task: dict[str, Any], agent: str, attempt: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    attempt_no = int(attempt.get("attempt") or 0)
    attempt_dir = root / "runs" / str(task.get("id", "unknown")) / f"attempt-{attempt_no:02d}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    context = build_execution_context(repo, task)
    (attempt_dir / "prompt.md").write_text(attempt_markdown(task, attempt, context), encoding="utf-8")
    (attempt_dir / "verify.log").write_text(checks_log(checks), encoding="utf-8")
    (attempt_dir / "critic.md").write_text(critic_markdown(attempt), encoding="utf-8")
    (attempt_dir / "diff.patch").write_text(git_diff_text(repo), encoding="utf-8")
    verdict = {
        "schema": "go-workflow.attempt-verdict.v1",
        "task_id": task.get("id"),
        "attempt": attempt_no,
        "strategy": attempt.get("strategy"),
        "build_status": (attempt.get("build") or {}).get("status"),
        "verify_status": (attempt.get("verify") or {}).get("status"),
        "critic_status": (attempt.get("critic") or {}).get("status"),
        "repair_status": (attempt.get("repair") or {}).get("status"),
        "judge_status": (attempt.get("judge") or {}).get("status"),
        "created_at": now_iso(),
    }
    dump_json(attempt_dir / "verdict.json", verdict)
    attempt["artifacts"] = {
        "prompt": str(attempt_dir.relative_to(root.parent) / "prompt.md"),
        "verify_log": str(attempt_dir.relative_to(root.parent) / "verify.log"),
        "critic": str(attempt_dir.relative_to(root.parent) / "critic.md"),
        "diff": str(attempt_dir.relative_to(root.parent) / "diff.patch"),
        "verdict": str(attempt_dir.relative_to(root.parent) / "verdict.json"),
    }
    append_jsonl(root / "runs" / "events.jsonl", event(str(task.get("id", "unknown")), "auto.attempt", agent, attempt))


def create_followup_task(repo: Path, task: dict[str, Any], findings: list[str], agent: str) -> dict[str, Any]:
    root = go_root(repo)
    followup_id = slugify(f"followup-{task.get('id', 'task')}-{len(findings)}").lower()
    base = followup_id
    index = 2
    while task_path(root, "open", followup_id).exists() or task_path(root, "done", followup_id).exists() or task_path(root, "active", followup_id).exists():
        followup_id = f"{base}-{index}"
        index += 1
    project = load_json(root / "project.json")
    followup = {
        "schema": TASK_SCHEMA,
        "kind": "task",
        "execution_mode": task.get("execution_mode", "agent"),
        "id": followup_id,
        "project": project["id"],
        "status": "open",
        "summary": f"Resolve critic findings for {task.get('id')}",
        "description": "\n".join(findings),
        "scope": task.get("scope", {"read": [], "modify": []}),
        "acceptance": ["All listed critic findings are resolved or explicitly reclassified as non-blocking."],
        "verification": task.get("verification", []),
        "evidence": [],
        "created_from": {"task_id": task.get("id"), "agent": agent, "created_at": now_iso(), "findings": findings},
    }
    dump_json(task_path(root, "open", followup_id), followup)
    append_jsonl(root / "runs" / "events.jsonl", event(followup_id, "run.checked", agent, {"action": "critic.followup_created", "source_task": task.get("id"), "findings": findings}))
    return followup


def builtin_semantic_findings(repo: Path, task: dict[str, Any], checks: list[dict[str, Any]]) -> list[str]:
    findings: list[str] = []
    acceptance = task.get("acceptance") or []
    if not acceptance:
        findings.append("task has no acceptance criteria; first-green cannot prove done")
    if acceptance == ["Task result is implemented and verified."]:
        findings.append("task uses generic default acceptance criteria; first-green cannot prove done")
    if not task.get("verification"):
        findings.append("task has no verification commands; loop cannot prove behavior")
    failed = [check for check in checks if check.get("returncode") != 0]
    if failed:
        findings.append(f"verification still failing: {failed[0].get('command')}")
    return findings


def repair_agent_available(agent: str) -> dict[str, Any]:
    binary = "codex" if agent == "codex" else "hermes" if agent == "hermes" else agent
    path = shutil.which(binary)
    return {"agent": agent, "binary": binary, "available": bool(path), "path": path}


def default_repair_agent_command(agent: str, task: dict[str, Any]) -> str:
    availability = repair_agent_available(agent)
    if not availability["available"]:
        raise RepoLocalError(f"repair agent '{agent}' is not available on PATH")
    instructions = " ".join([
        "You are the repair adapter for go-workflow-stack.",
        "In the repository named by GO_REPO, fix the task named by GO_TASK_ID using the GO_ATTEMPT and GO_STRATEGY context.",
        "Read GO_TASK_JSON from the environment.",
        "Read GO_CONTEXT_JSON and obey its vision, architecture principles, hierarchy, acceptance, verification, and task scope.",
        "Edit only paths allowed by the task scope.",
        "Run the task verification commands before exiting.",
        "Exit non-zero if you cannot safely repair within scope.",
    ])
    return native_agent_command(agent, "repair", instructions)


def select_executor_agent(requested: str) -> str:
    if requested == "none":
        return ""
    if requested in {"codex", "hermes"}:
        if not repair_agent_available(requested)["available"]:
            raise RepoLocalError(f"executor agent '{requested}' is not available on PATH")
        return requested
    for candidate in ("codex", "hermes"):
        if repair_agent_available(candidate)["available"]:
            return candidate
    raise RepoLocalError("no Codex or Hermes executor agent is available on PATH")


def executor_agent_default() -> str:
    requested = os.environ.get("GO_EXECUTOR_AGENT", "auto").strip().lower()
    return requested if requested in {"auto", "codex", "hermes", "none"} else "auto"


def default_executor_agent_command(agent: str, task: dict[str, Any]) -> str:
    selected = select_executor_agent(agent)
    instructions = " ".join([
        "You are the build executor for a repo-local .go task.",
        "Work in the repository named by GO_REPO on the task named by GO_TASK_ID using the GO_ATTEMPT and GO_STRATEGY context.",
        "Read GO_CONTEXT_JSON and obey its vision, architecture principles, hierarchy, acceptance, verification, and modify scope.",
        "Implement the task, run focused verification, and leave only scoped changes.",
        "Do not merely describe commands; perform the work and exit non-zero when the task cannot be completed safely.",
    ])
    return native_agent_command(selected, "build", instructions)


def run_default_critic_agent(
    repo: Path,
    agent: str,
    task: dict[str, Any],
    attempt: int,
    strategy: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    output = go_root(repo) / "runs" / str(task.get("id", "unknown")) / f"attempt-{attempt:02d}" / "deep-critic.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    instructions = " ".join([
        "You are the blocking critic for a repo-local .go task.",
        "Review the current repository result for task {task_id} against GO_CONTEXT_JSON, including vision, architecture principles, acceptance, verification, scope, and diff.",
        "Do not edit files.",
        "Return status success only when there are no blocking findings; otherwise return status blocked and summarize the findings.",
    ])
    command = native_agent_command(agent, "critic", instructions)
    result = run_hook_command(repo, command, task, attempt, strategy, "critic", timeout_seconds, require_protocol=True)
    output.write_text(result.get("stdout") or "", encoding="utf-8")
    verdict_text = output.read_text(encoding="utf-8") if output.is_file() else ""
    result["verdict_text"] = verdict_text
    return result


def ship_changes(repo: Path, policy: str, allow_push: bool, message: str, task: dict[str, Any] | None = None) -> dict[str, Any]:
    if policy == "none":
        return {"policy": policy, "status": "skipped"}
    if policy == "push" and not allow_push:
        return {"policy": policy, "status": "blocked", "reason": "push requires --allow-push"}
    status = subprocess.run(["git", "status", "--short"], cwd=repo, text=True, capture_output=True)
    if status.returncode != 0:
        return {"policy": policy, "status": "failed", "stderr": status.stderr}
    changed = git_status_paths(repo)
    if not changed:
        return {"policy": policy, "status": "clean"}
    allowed_paths = [path for path in changed if allowed_runtime_path(path) or (task is not None and path_allowed_by_task(path, task, allow_runtime=False))]
    unrelated = [path for path in changed if path not in allowed_paths]
    if not allowed_paths:
        return {"policy": policy, "status": "blocked", "reason": "no scoped paths to ship", "unrelated_dirty": unrelated}
    add = subprocess.run(["git", "add", "--", *allowed_paths], cwd=repo, text=True, capture_output=True)
    if add.returncode != 0:
        return {"policy": policy, "status": "failed", "stderr": add.stderr, "scoped_paths": allowed_paths, "unrelated_dirty": unrelated}
    commit = subprocess.run(["git", "-c", "user.name=Go Workflow", "-c", "user.email=go-workflow@example.com", "commit", "-m", message], cwd=repo, text=True, capture_output=True)
    result = {"policy": policy, "status": "committed" if commit.returncode == 0 else "failed", "stdout": commit.stdout[-2000:], "stderr": commit.stderr[-2000:], "scoped_paths": allowed_paths, "unrelated_dirty": unrelated}
    if commit.returncode != 0:
        return result
    if policy == "push":
        push = subprocess.run(["git", "push"], cwd=repo, text=True, capture_output=True)
        result["push"] = {"returncode": push.returncode, "stdout": push.stdout[-2000:], "stderr": push.stderr[-2000:]}
        result["status"] = "pushed" if push.returncode == 0 else "push_failed"
    return result


def ship_policy_blocker(policy: str, allow_push: bool) -> str | None:
    if policy == "push" and not allow_push:
        return "push requires --allow-push"
    return None


def build_resume_args(mode: str, args: argparse.Namespace) -> list[str]:
    parts = [mode, ".", "--execute"]
    valued = [
        ("--max-tasks", arg_int(args, "max_tasks", 1)),
        ("--summary-chars", arg_int(args, "summary_chars", 1200)),
        ("--max-minutes", arg_int(args, "max_minutes", 45)),
        ("--max-commands", arg_int(args, "max_commands", 12)),
        ("--command-timeout-seconds", arg_int(args, "command_timeout_seconds", 900)),
        ("--max-attempts", arg_int(args, "max_attempts", 5)),
        ("--checkpoint-every-tasks", arg_int(args, "checkpoint_every_tasks", 1)),
        ("--agent", getattr(args, "agent", "agent")),
    ]
    for flag, value in valued:
        parts.extend([flag, str(value)])
    for flag, name in [
        ("--build-command", "build_command"),
        ("--critic-command", "critic_command"),
        ("--repair-command", "repair_command"),
        ("--repair-agent", "repair_agent"),
        ("--executor-agent", "executor_agent"),
        ("--ship-policy", "ship_policy"),
    ]:
        value = arg_str(args, name)
        if value:
            parts.extend([flag, value])
    parts.append("--semantic-critic" if bool(getattr(args, "semantic_critic", True)) else "--no-semantic-critic")
    for flag, name in [
        ("--followup-on-block", "followup_on_block"),
        ("--allow-dirty", "allow_dirty"),
        ("--allow-push", "allow_push"),
        ("--json", "json"),
    ]:
        if bool(getattr(args, name, False)):
            parts.append(flag)
    return parts


def write_resume_script(root: Path, mode: str, args: argparse.Namespace) -> Path:
    resume_args = " ".join(shell_quote(part) for part in build_resume_args(mode, args))
    script = "\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"',
        'STACK="${GO_STACK:-}"',
        'if [ -z "$STACK" ] || [ ! -f "$STACK/cli/go.py" ]; then',
        '  for candidate in "$REPO_ROOT/../go-workflow-stack" "$HOME/github/go-workflow-stack" "$HOME/Dev/go-workflow-stack"; do',
        '    if [ -f "$candidate/cli/go.py" ]; then STACK="$candidate"; break; fi',
        "  done",
        "fi",
        'if [ -z "$STACK" ] || [ ! -f "$STACK/cli/go.py" ]; then',
        '  echo "go-workflow-stack not found; set GO_STACK or clone it beside this repository" >&2',
        "  exit 2",
        "fi",
        'cd "$REPO_ROOT"',
        f'exec python3 "$STACK/cli/go.py" {resume_args}',
        "",
    ])
    path = root / "runs" / "resume.sh"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def build_resume_command(mode: str, args: argparse.Namespace) -> str:
    return "bash .go/runs/resume.sh"


def write_latest_run_state(repo: Path, root: Path, result: dict[str, Any], args: argparse.Namespace, mode: str) -> None:
    write_resume_script(root, mode, args)
    latest = {
        "schema": "go-workflow.latest-run.v1",
        "updated_at": now_iso(),
        "mode": mode,
        "status": result.get("status"),
        "completed_tasks": result.get("completed_tasks", []),
        "blocked_task": result.get("blocked_task"),
        "budget_exhausted": result.get("budget_exhausted", False),
        "resume_command": build_resume_command(mode, args),
        "resume_args": build_resume_args(mode, args),
        "runtime_resolution": ["GO_STACK", "sibling checkout", "~/github/go-workflow-stack", "~/Dev/go-workflow-stack"],
        "effective_flags": {
            "max_tasks": arg_int(args, "max_tasks", 1),
            "summary_chars": arg_int(args, "summary_chars", 1200),
            "max_minutes": arg_int(args, "max_minutes", 45),
            "max_commands": arg_int(args, "max_commands", 12),
            "command_timeout_seconds": arg_int(args, "command_timeout_seconds", 900),
            "max_attempts": arg_int(args, "max_attempts", 5),
            "build_command": arg_str(args, "build_command"),
            "critic_command": arg_str(args, "critic_command"),
            "repair_command": arg_str(args, "repair_command"),
            "repair_agent": arg_str(args, "repair_agent"),
            "executor_agent": arg_str(args, "executor_agent", "auto"),
            "semantic_critic": bool(getattr(args, "semantic_critic", False)),
            "followup_on_block": bool(getattr(args, "followup_on_block", False)),
            "ship_policy": arg_str(args, "ship_policy", "none"),
            "allow_dirty": bool(getattr(args, "allow_dirty", False)),
            "allow_push": bool(getattr(args, "allow_push", False)),
        },
        "next_action": result.get("next_action"),
    }
    dump_json(root / "runs" / "latest.json", latest)


def execute_loop_plan(repo: Path, args: argparse.Namespace, mode: str) -> tuple[int, dict[str, Any]]:
    plan = build_loop_plan(repo, args, mode=mode)
    root = go_root(repo)
    preflight = plan["run_envelope"]["preflight"]
    result: dict[str, Any] = {
        "schema": "go-workflow.auto-run-result.v1",
        "mode": mode,
        "repo": str(repo),
        "run_envelope": plan["run_envelope"],
        "status": "running",
        "completed_tasks": [],
        "blocked_task": None,
        "evidence": [],
        "checks": [],
        "summary": "",
        "next_action": None,
        "checkpoints": [],
        "commands_run": 0,
        "budget_exhausted": False,
        "attempts": [],
        "ship": [],
        "completion_audit": None,
    }
    started_at = time.monotonic()
    budget = plan["run_envelope"]["budget"]
    max_commands = int(budget.get("max_commands") or 1)
    max_minutes = int(budget.get("max_minutes") or 1)
    command_timeout_seconds = int(budget.get("command_timeout_seconds") or 900)
    checkpoint_every_tasks = int(budget.get("checkpoint_every_tasks") or 1)
    max_attempts = max(int(getattr(args, "max_attempts", 5) or 5), 1)
    strategies = ["direct_fix", "re_approach", "simplify", "last_stand", "block_with_evidence"]
    build_command = arg_str(args, "build_command")
    critic_command = arg_str(args, "critic_command")
    repair_command = arg_str(args, "repair_command")
    executor_agent = arg_str(args, "executor_agent", "auto")
    semantic_critic = bool(getattr(args, "semantic_critic", False))
    followup_on_block = bool(getattr(args, "followup_on_block", False))
    ship_policy = arg_str(args, "ship_policy", "none")
    allow_push = bool(getattr(args, "allow_push", False))

    if preflight.get("contract_gate_required"):
        result.update({
            "status": "contract_gate",
            "summary": "Task contract is not executable enough for autonomous completion.",
            "next_action": "repair acceptance, verification, or scope using preflight.contract_findings, then rerun",
        })
        return 1, result

    initial_unfinished = preflight.get("unfinished_tasks", {})
    if not plan.get("next_tasks") and (initial_unfinished.get("active") or initial_unfinished.get("blocked")):
        blocked_id = (initial_unfinished.get("blocked") or initial_unfinished.get("active") or [None])[0]
        result.update({
            "status": "blocked",
            "blocked_task": blocked_id,
            "summary": "No open task is claimable while active or blocked task state remains.",
            "next_action": "repair, requeue, or explicitly resolve the unfinished task before claiming goal completion",
        })
        return 1, result

    if preflight["human_gate_required"] and not args.allow_dirty:
        result.update({"status": "safety_gate", "summary": "Preflight blocked by dirty/lock/conflict/secret state.", "next_action": "resolve human_gate_blockers or rerun with explicit override"})
        append_jsonl(root / "runs" / "events.jsonl", event("go-auto", "auto.safety_gate", args.agent, {"blockers": preflight["human_gate_blockers"]}))
        return 1, result

    for _ in range(max(arg_int(args, "max_tasks", 3), 1)):
        elapsed_minutes = (time.monotonic() - started_at) / 60
        if result["commands_run"] >= max_commands or elapsed_minutes >= max_minutes:
            result.update({"status": "budget_exhausted", "budget_exhausted": True, "summary": "Budget exhausted before selecting another task.", "next_action": "continue with go-loop using a larger budget"})
            break
        tasks = open_tasks(repo)
        if not tasks:
            result["status"] = "done"
            result["summary"] = "No open tasks remain."
            break
        open_path, task = tasks[0]
        task_build_command = build_command
        default_executor_selected = False
        selected_executor_agent = ""
        if task.get("execution_mode", "mechanical") == "agent" and not task_build_command:
            try:
                selected_executor_agent = select_executor_agent(executor_agent)
                task_build_command = default_executor_agent_command(selected_executor_agent, task)
                default_executor_selected = True
            except RepoLocalError as exc:
                result.update({
                    "status": "adapter_gate",
                    "blocked_task": task.get("id"),
                    "summary": str(exc),
                    "next_action": "install/select an executor agent, provide --build-command, or explicitly mark the task mechanical",
                })
                break
        task_owned_patterns = [
            pattern for pattern in task.get("scope", {}).get("modify", [])
            if not str(pattern).startswith(".go/")
        ]
        dirty = classify_dirty(repo, task_owned_patterns)
        # In an executed batch, finishing earlier tasks intentionally mutates .go evidence/runs/task files.
        # Do not let our own lifecycle writes block the next task; pre-existing dirty state is already gated by preflight.
        if result["completed_tasks"]:
            dirty = {"blocking": [], "report_only": dirty.get("blocking", []) + dirty.get("report_only", [])}
        if dirty["blocking"] and not args.allow_dirty:
            result.update({"status": "safety_gate", "blocked_task": task.get("id"), "summary": "Task scope dirty gate blocked execution.", "next_action": "resolve dirty state or rerun with explicit override"})
            result["run_envelope"]["preflight"]["human_gate_required"] = True
            result["run_envelope"]["preflight"].setdefault("human_gate_blockers", []).extend(dirty["blocking"])
            append_jsonl(root / "runs" / "events.jsonl", event(task.get("id", "unknown"), "auto.safety_gate", args.agent, {"blockers": dirty["blocking"]}))
            return 1, result
        try:
            with repository_lock(root, f"task-{task['id']}"):
                if not open_path.is_file():
                    continue
                task = load_json(open_path)
                if task.get("status") != "open" or task.get("claim", {}).get("agent"):
                    continue
                task["status"] = "active"
                task["claim"] = {"agent": args.agent, "claimed_at": now_iso()}
                active_path = task_path(root, "active", task["id"])
                atomic_move_json(open_path, active_path, task)
                append_jsonl(root / "runs" / "events.jsonl", event(task["id"], "task.claimed", args.agent, {"report_only_dirty": dirty["report_only"], "executor": mode}))
        except StateLockError as exc:
            result.update({"status": "safety_gate", "blocked_task": task.get("id"), "summary": str(exc), "next_action": "wait for the live task owner or recover the stale lock"})
            break
        task_passed = False
        final_checks: list[dict[str, Any]] = []
        last_failed_command = "verification"
        task_repair_command = repair_command
        repair_agent = arg_str(args, "repair_agent", "")
        if repair_agent and not task_repair_command:
            try:
                task_repair_command = default_repair_agent_command(repair_agent, task)
            except RepoLocalError as exc:
                block_task_record(repo, root, active_path, task, args.agent, str(exc), final_checks)
                result.update({"status": "blocked", "blocked_task": task["id"], "summary": str(exc), "next_action": "install/configure the repair agent or use --repair-command"})
                break
        elif default_executor_selected and not task_repair_command:
            task_repair_command = default_repair_agent_command(selected_executor_agent, task)
        if result.get("status") == "blocked":
            break
        for attempt_number in range(1, max_attempts + 1):
            strategy = strategies[min(attempt_number - 1, len(strategies) - 1)]
            attempt = {
                "task_id": task["id"],
                "attempt": attempt_number,
                "strategy": strategy,
                "stages": ["build", "verify", "critic", "repair", "judge"],
                "build": {"status": "skipped", "note": "No build adapter command configured; assuming task artifacts are already produced by the invoking agent."},
                "verify": {"status": "running", "checks": []},
                "critic": {"status": "pending", "blocking_findings": []},
                "repair": {"status": "not_needed"},
                "judge": {"status": "pending"},
            }
            if task_build_command:
                if not ensure_budget(result, max_commands, started_at, max_minutes, "build adapter"):
                    break
                before_paths = git_dirty_snapshot(repo)
                build = run_hook_command(repo, task_build_command, task, attempt_number, strategy, "build", command_timeout_seconds, require_protocol=default_executor_selected)
                result["commands_run"] += 1
                violations = scope_violations_after(repo, task, before_paths)
                if violations:
                    build["returncode"] = build["returncode"] or 126
                    build["stderr"] = (build.get("stderr") or "") + "\nscope violations after build adapter: " + ", ".join(violations)
                attempt["build"] = {"status": "passed" if build["returncode"] == 0 else "failed", "result": build}
                if build["returncode"] != 0:
                    attempt["critic"] = {"status": "blocking_findings", "blocking_findings": ["build adapter failed"], "repair_hint": build["stderr"] or build["stdout"]}
                    attempt["judge"] = {"status": "retry_or_block", "reason": "build adapter failed"}
                    last_failed_command = build["command"]
                    result["attempts"].append(attempt)
                    record_attempt(repo, root, task, args.agent, attempt, final_checks)
                    if task_repair_command and attempt_number < max_attempts:
                        if not ensure_budget(result, max_commands, started_at, max_minutes, "repair adapter"):
                            break
                        before_paths = git_dirty_snapshot(repo)
                        repair = run_hook_command(repo, task_repair_command, task, attempt_number, strategy, "repair", command_timeout_seconds, require_protocol=bool(repair_agent) or default_executor_selected)
                        result["commands_run"] += 1
                        violations = scope_violations_after(repo, task, before_paths)
                        if violations:
                            repair["returncode"] = repair["returncode"] or 126
                            repair["stderr"] = (repair.get("stderr") or "") + "\nscope violations after repair adapter: " + ", ".join(violations)
                        attempt["repair"] = {"status": "passed" if repair["returncode"] == 0 else "failed", "result": repair}
                        continue
                    break
            if not ensure_budget(result, max_commands, started_at, max_minutes, "verification"):
                break
            checks = run_verification_commands(repo, task, command_budget=max_commands - result["commands_run"], timeout_seconds=command_timeout_seconds)
            final_checks = checks
            attempt["verify"] = {"status": "passed" if all(check["returncode"] == 0 for check in checks) else "failed", "checks": checks}
            result["commands_run"] += len([check for check in checks if check.get("command") and not check.get("budget_exhausted")])
            result["checks"].extend(checks)
            if any(check.get("budget_exhausted") for check in checks):
                result.update({"status": "budget_exhausted", "budget_exhausted": True, "summary": "Budget exhausted during verification.", "next_action": "resume with go-loop using a larger budget or narrower task scope"})
            failed = next((check for check in checks if check["returncode"] != 0), None)
            if failed:
                last_failed_command = failed["command"] or "verification"
                attempt["critic"] = {
                    "status": "blocking_findings",
                    "blocking_findings": [f"verification failed: {last_failed_command}"],
                    "repair_hint": "Fix the failing command output inside task scope, then rerun verification.",
                }
            else:
                builtin_findings = builtin_semantic_findings(repo, task, checks) if semantic_critic else []
                if builtin_findings:
                    attempt["critic"] = {
                        "status": "blocking_findings",
                        "blocking_findings": builtin_findings,
                        "repair_hint": "Resolve the critic findings or create scoped follow-up work before finishing.",
                    }
                    last_failed_command = "semantic critic"
                elif critic_command:
                    if not ensure_budget(result, max_commands, started_at, max_minutes, "critic adapter"):
                        break
                    before_paths = git_dirty_snapshot(repo)
                    critic = run_hook_command(repo, critic_command, task, attempt_number, strategy, "critic", command_timeout_seconds)
                    result["commands_run"] += 1
                    violations = scope_violations_after(repo, task, before_paths)
                    if violations:
                        critic["returncode"] = critic["returncode"] or 126
                        critic["stderr"] = (critic.get("stderr") or "") + "\nscope violations after critic adapter: " + ", ".join(violations)
                    attempt["critic"] = {
                        "status": "passed" if critic["returncode"] == 0 else "blocking_findings",
                        "blocking_findings": [] if critic["returncode"] == 0 else [critic["stderr"] or critic["stdout"] or "critic adapter failed"],
                        "result": critic,
                    }
                    if critic["returncode"] != 0:
                        last_failed_command = critic["command"]
                elif default_executor_selected and selected_executor_agent:
                    if not ensure_budget(result, max_commands, started_at, max_minutes, "deep critic agent"):
                        break
                    before_paths = git_dirty_snapshot(repo)
                    critic = run_default_critic_agent(
                        repo,
                        selected_executor_agent,
                        task,
                        attempt_number,
                        strategy,
                        command_timeout_seconds,
                    )
                    result["commands_run"] += 1
                    violations = scope_violations_after(repo, task, before_paths)
                    if violations:
                        critic["returncode"] = critic["returncode"] or 126
                        critic["stderr"] = (critic.get("stderr") or "") + "\nscope violations after deep critic: " + ", ".join(violations)
                    attempt["critic"] = {
                        "status": "passed" if critic["returncode"] == 0 else "blocking_findings",
                        "blocking_findings": [] if critic["returncode"] == 0 else [critic.get("verdict_text") or critic.get("stderr") or "deep critic blocked"],
                        "result": critic,
                    }
                    if critic["returncode"] != 0:
                        last_failed_command = "deep critic agent"
                else:
                    attempt["critic"] = {"status": "passed", "blocking_findings": []}
            if attempt["verify"]["status"] == "passed" and attempt["critic"]["status"] == "passed":
                attempt["judge"] = {"status": "passed", "reason": "verification and critic passed"}
                result["attempts"].append(attempt)
                record_attempt(repo, root, task, args.agent, attempt, final_checks)
                task_passed = True
                break
            if task_repair_command and attempt_number < max_attempts:
                if not ensure_budget(result, max_commands, started_at, max_minutes, "repair adapter"):
                    break
                before_paths = git_dirty_snapshot(repo)
                repair = run_hook_command(repo, task_repair_command, task, attempt_number, strategy, "repair", command_timeout_seconds, require_protocol=bool(repair_agent) or default_executor_selected)
                result["commands_run"] += 1
                violations = scope_violations_after(repo, task, before_paths)
                if violations:
                    repair["returncode"] = repair["returncode"] or 126
                    repair["stderr"] = (repair.get("stderr") or "") + "\nscope violations after repair adapter: " + ", ".join(violations)
                attempt["repair"] = {"status": "passed" if repair["returncode"] == 0 else "failed", "result": repair}
                attempt["judge"] = {"status": "retry", "reason": "repair attempted; rerun loop strategy"}
                result["attempts"].append(attempt)
                record_attempt(repo, root, task, args.agent, attempt, final_checks)
                if repair["returncode"] == 0:
                    continue
                break
            attempt["repair"] = {"status": "requires_agent_repair", "reason": "no repair adapter available or max attempts reached"}
            attempt["judge"] = {"status": "blocked", "reason": "failed after bounded attempt"}
            result["attempts"].append(attempt)
            record_attempt(repo, root, task, args.agent, attempt, final_checks)
            break
        if result.get("budget_exhausted"):
            result.update({"blocked_task": task["id"]})
            break
        if not task_passed:
            block_reason = f"critic blocked after bounded executor attempts: {last_failed_command}"
            if followup_on_block and result["attempts"]:
                last_attempt = result["attempts"][-1]
                findings = (last_attempt.get("critic") or {}).get("blocking_findings") or [block_reason]
                followup = create_followup_task(repo, task, findings, args.agent)
                result.setdefault("created_followups", []).append(followup["id"])
            block_task_record(repo, root, active_path, task, args.agent, block_reason, final_checks)
            result.update({"status": "blocked", "blocked_task": task["id"], "summary": "Bounded executor attempts failed; critic/repair evidence recorded and task moved to blocked.", "next_action": "repair failing gate or configure build/critic/repair adapter, then rerun go-loop"})
            break
        evidence_summary = "; ".join(check["command"] or "no verification configured" for check in final_checks)
        ship_blocker = ship_policy_blocker(ship_policy, allow_push)
        if ship_blocker:
            result.update({
                "status": "blocked",
                "blocked_task": task["id"],
                "summary": "Ship policy blocked completion after verification; task remains active.",
                "next_action": ship_blocker,
            })
            result["ship"].append({"task_id": task["id"], "policy": ship_policy, "status": "blocked", "reason": ship_blocker})
            break
        active_task_before_finish = json.loads(json.dumps(task))
        evidence_path = root / "evidence" / "events.jsonl"
        evidence_before_finish = evidence_path.read_text(encoding="utf-8") if evidence_path.exists() else ""
        done_path = finish_task_record(repo, root, active_path, task, args.agent, f"auto-execute verified: {evidence_summary}")
        ship = ship_changes(repo, ship_policy, allow_push, f"go-loop: finish {task['id']}", task)
        result["ship"].append({"task_id": task["id"], **ship})
        if ship.get("status") in {"blocked", "failed"}:
            restore_active_after_failed_ship(active_path, done_path, active_task_before_finish, evidence_path, evidence_before_finish)
            result.update({"status": "blocked", "blocked_task": task["id"], "summary": "Ship failed; verified task was restored to active.", "next_action": ship.get("reason") or ship.get("stderr")})
            break
        result["completed_tasks"].append(task["id"])
        result["evidence"].append({"task_id": task["id"], "summary": f"auto-execute verified: {evidence_summary}"})
        if ship.get("status") == "push_failed":
            result.update({"status": "ship_pending", "summary": "Task is complete in a local commit, but push failed.", "next_action": "repair the remote/push failure and run git push"})
            break
        result["status"] = "done"
        result["summary"] = f"Completed {len(result['completed_tasks'])} task(s)."
        if len(result["completed_tasks"]) % checkpoint_every_tasks == 0:
            result["checkpoints"].append({"created_at": now_iso(), "completed_tasks": list(result["completed_tasks"]), "status": result["status"], "telegram_policy": plan["run_envelope"].get("telegram_policy", {})})
        if mode != "go-loop" and len(result["completed_tasks"]) >= max(arg_int(args, "max_tasks", 3), 1):
            result["next_action"] = "budget_exhausted_or_batch_complete"
            break
    remaining_tasks = open_tasks(repo)
    if result.get("status") == "done" and remaining_tasks:
        result.update({
            "status": "budget_exhausted",
            "budget_exhausted": True,
            "summary": f"Completed {len(result['completed_tasks'])} task(s); {len(remaining_tasks)} open task(s) remain.",
            "next_action": "resume with the persisted go-loop command",
        })
    elif result.get("status") == "done":
        unfinished = unfinished_task_ids(repo)
        if unfinished.get("active") or unfinished.get("blocked"):
            blocked_id = (unfinished.get("blocked") or unfinished.get("active") or [None])[0]
            result.update({
                "status": "blocked",
                "blocked_task": blocked_id,
                "summary": "Open work is exhausted, but active or blocked task state prevents goal completion.",
                "next_action": "repair, requeue, or explicitly resolve the unfinished task",
            })
        else:
            project = load_json(root / "project.json")
            if project.get("project_mode") == "template":
                contract_errors = validate_repo(repo)
                result["completion_audit"] = {
                    "schema": "go-workflow.template-smoke-completion.v1",
                    "setup_required": True,
                    "contract_valid": not contract_errors,
                    "contract_errors": contract_errors,
                    "completed_example_tasks": list(result["completed_tasks"]),
                }
                if contract_errors:
                    result.update({
                        "status": "goal_incomplete",
                        "summary": "The template smoke ran, but the starter contract is invalid.",
                        "next_action": "repair the template contract before project customization",
                    })
            else:
                vision = load_json(root / "vision.json")
                done_tasks = [load_json(path) for path in sorted((root / "tasks" / "done").glob("*.json"))]
                task_evidence_complete = bool(done_tasks) and all(bool(task.get("evidence")) for task in done_tasks)
                contract_errors = validate_repo(repo)
                goal_checks: list[dict[str, Any]] = []
                if ensure_budget(result, max_commands, started_at, max_minutes, "goal completion verification"):
                    goal_checks = run_verification_commands(
                        repo,
                        {"id": "goal-completion", "verification": project.get("default_verification", [])},
                        command_budget=max_commands - result["commands_run"],
                        timeout_seconds=command_timeout_seconds,
                    )
                    result["commands_run"] += len([check for check in goal_checks if check.get("command") and not check.get("budget_exhausted")])
                    result["checks"].extend(goal_checks)
                project_verification_passed = bool(goal_checks) and all(check.get("command") and check.get("returncode") == 0 for check in goal_checks)
                completion_audit = {
                    "schema": "go-workflow.goal-completion-audit.v1",
                    "vision_status": vision.get("status"),
                    "success_metrics": vision.get("success_metrics", []),
                    "success_metrics_declared": bool(vision.get("success_metrics")),
                    "contract_valid": not contract_errors,
                    "contract_errors": contract_errors,
                    "task_evidence_complete": task_evidence_complete,
                    "project_verification": goal_checks,
                    "project_verification_passed": project_verification_passed,
                    "open_tasks": 0,
                    "active_tasks": unfinished.get("active", []),
                    "blocked_tasks": unfinished.get("blocked", []),
                }
                result["completion_audit"] = completion_audit
                completion_proven = (
                    completion_audit["vision_status"] == "active"
                    and completion_audit["success_metrics_declared"]
                    and completion_audit["contract_valid"]
                    and completion_audit["task_evidence_complete"]
                    and completion_audit["project_verification_passed"]
                )
                if not completion_proven and not result.get("budget_exhausted"):
                    result.update({
                        "status": "goal_incomplete",
                        "summary": "Tasks are exhausted, but the vision-level completion audit is not proven.",
                        "next_action": "create a follow-up task for the failing completion-audit evidence and resume go-loop",
                    })
    append_jsonl(root / "reflections" / "events.jsonl", event("go-auto", "auto.reflected", args.agent, {"mode": mode, "status": result["status"], "completed_tasks": result["completed_tasks"], "blocked_task": result["blocked_task"], "next_action": result["next_action"]}))
    write_latest_run_state(repo, root, result, args, mode)
    if ship_policy != "none" and result.get("completed_tasks"):
        final_ship = ship_changes(repo, ship_policy, allow_push, "go-loop: finalize run state")
        result["ship"].append({"task_id": "run-state", **final_ship})
    return (0 if result["status"] in {"done", "budget_exhausted"} else 1), result


def build_agent_handoff(repo: Path, args: argparse.Namespace, mode: str) -> dict[str, Any]:
    plan = build_loop_plan(repo, args, mode=mode)
    selected: list[dict[str, Any]] = []
    verification_commands: list[str] = []
    for _, task in open_tasks(repo)[: max(args.max_tasks, 1)]:
        selected.append({
            "id": task.get("id"),
            "execution_mode": task.get("execution_mode", "mechanical"),
            "summary": task.get("summary"),
            "description": task.get("description", ""),
            "scope": task.get("scope", {}),
            "acceptance": task.get("acceptance", []),
            "verification": task.get("verification", []),
        })
        verification_commands.extend(task.get("verification", []) or [])
    return {
        "schema": "go-workflow.agent-handoff.v1",
        "mode": mode,
        "target_runtime": "codex-or-hermes-agent",
        "repo": str(repo),
        "project_id": plan["project_id"],
        "command": f"{mode} --execute",
        "control_handoff": True,
        "tasks": selected,
        "run_envelope": plan["run_envelope"],
        "execution_policy": plan["execution_policy"],
        "gates": plan["execution_policy"]["human_gates"],
        "expected_evidence": {
            "verification_commands": verification_commands,
            "task_state": ".go/tasks/done/<task-id>.json or .go/tasks/blocked/<task-id>.json",
            "events": [".go/evidence/events.jsonl", ".go/runs/events.jsonl", ".go/reflections/events.jsonl"],
            "final_result_schema": "go-workflow.auto-run-result.v1",
        },
        "agent_instructions": [
            "Do not ask when a safe default exists.",
            "Edit only within task scope.",
            "Run verification before finish.",
            "Finish with evidence or block with check output.",
            "Escalate to go-loop when self-reflect or review finds same-scope repair work.",
        ],
    }



def create_task_from_intent(repo: Path, intent: str, agent: str = "agent") -> dict[str, Any]:
    root = go_root(repo)
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot create intent task in invalid .go state:\n- " + "\n- ".join(errors))
    project = load_json(root / "project.json")
    summary = intent.strip() or "Continue toward project goal"
    task_id = slugify(summary).lower()[:48].strip("-") or "continue-project-goal"
    base_id = task_id
    index = 2
    while any(task_path(root, state, task_id).exists() for state in ("open", "active", "blocked", "done")):
        suffix = f"-{index}"
        task_id = (base_id[: 48 - len(suffix)] + suffix).strip("-")
        index += 1
    verification = project.get("default_verification") or ["git diff --check"]
    task = {
        "schema": TASK_SCHEMA,
        "kind": "task",
        "execution_mode": "agent",
        "id": task_id,
        "project": project["id"],
        "status": "open",
        "summary": summary,
        "description": f"Created from bare go intent: {summary}",
        "scope": {"read": [".go/**", "README.md", "docs/**", "src/**", "tests/**"], "modify": [".go/**", "README.md", "docs/**", "src/**", "tests/**"]},
        "acceptance": ["Intent is implemented or explicitly blocked with evidence.", "Result is verified and summarized compactly."],
        "verification": verification,
        "claim": {"agent": None, "claimed_at": None},
        "evidence": [],
    }
    target = task_path(root, "open", task_id)
    dump_json(target, task)
    hierarchy = load_json(root / "hierarchy.json")
    epics = hierarchy_epics(hierarchy)
    if epics:
        epics[0].setdefault("tasks", [])
        if task_id not in epics[0]["tasks"]:
            epics[0]["tasks"].append(task_id)
        set_hierarchy_epics(hierarchy, epics)
        dump_json(root / "hierarchy.json", hierarchy)
    append_jsonl(root / "runs" / "events.jsonl", event(task_id, "run.checked", agent, {"action": "task.created_from_go_intent", "intent": summary, "path": relative(repo, target)}))
    errors = validate_repo(repo)
    if errors:
        target.unlink(missing_ok=True)
        raise RepoLocalError("created intent task invalidated .go state:\n- " + "\n- ".join(errors))
    return {"id": task_id, "summary": summary, "path": relative(repo, target)}


def cmd_go(args: argparse.Namespace) -> int:
    """Bare go universal router: route loose vs repo-local work and optionally execute."""
    repo = Path(args.repo).resolve()
    intent = (args.intent or "").strip()
    root = go_root(repo)
    state = {
        "repo_exists": repo.exists(),
        "has_go": root.is_dir(),
        "has_project": (root / "project.json").is_file(),
        "has_vision": (root / "vision.json").is_file(),
        "has_principles": (root / "architecture-principles.json").is_file(),
        "has_hierarchy": (root / "hierarchy.json").is_file(),
    }
    result: dict[str, Any] = {
        "schema": "go-workflow.bare-go.v1",
        "repo": str(repo),
        "intent": intent,
        "state": state,
        "created_task": None,
        "action": None,
        "plan": None,
    }
    if not state["repo_exists"]:
        result.update({"action": "spike", "reason": "repo missing; create repo-local .go contract first", "next_command": f"python3 {Path(__file__).resolve()} spike {repo} --brief \"{intent or '<intent>'}\""})
    elif not state["has_project"]:
        result.update({"action": "spike", "reason": "repo has no .go/project.json; repair/adopt contract first", "next_command": f"python3 {Path(__file__).resolve()} spike {repo} --brief \"{intent or '<intent>'}\" --skip-repo-complete"})
    else:
        errors = validate_repo(repo)
        if errors:
            result.update({"action": "contract_repair_required", "reason": "repo-local .go contract is invalid", "errors": errors})
        else:
            mode = "go-loop" if args.loop or any(word in intent.lower() for word in ["loop", "ralph", "groen", "controle afgeven", "tot bare go echt werkt"]) else "go-auto"
            has_open_tasks = bool(open_tasks(repo))
            may_write_intent_task = bool(args.write or args.execute)
            if not has_open_tasks and intent and may_write_intent_task:
                result["created_task"] = create_task_from_intent(repo, intent, agent=args.agent)
                has_open_tasks = True
            elif not has_open_tasks and intent:
                proposed_id = slugify(intent).lower()[:48].strip("-") or "continue-project-goal"
                result["proposed_task"] = {
                    "id": proposed_id,
                    "summary": intent,
                    "write_required": True,
                    "write_flag": "--write",
                }
                result["write_boundary"] = "dry_run: no .go state was changed; rerun with --write or --execute to materialize the intent task"
            result["action"] = mode
            plan = build_loop_plan(repo, args, mode=mode)
            if result.get("proposed_task") and not plan.get("next_tasks"):
                plan["next_tasks"] = [result["proposed_task"]["id"]]
                plan["run_envelope"]["preflight"]["selected_task_count"] = 0
                plan["run_envelope"]["preflight"]["dry_run_proposed_task"] = result["proposed_task"]
            result["plan"] = plan
            if args.execute:
                exit_code, executed = execute_loop_plan(repo, args, mode=mode)
                result["execution"] = executed
                if args.json:
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                else:
                    print(f"go: {executed['status']}")
                return exit_code
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"go action: {result.get('action')}")
        if result.get("created_task"):
            print(f"created_task: {result['created_task']['id']}")
        if result.get("plan"):
            print("next_tasks: " + (", ".join(result["plan"].get("next_tasks", [])) or "none"))
        elif result.get("next_command"):
            print(f"next: {result['next_command']}")
    return 0

def cmd_auto(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if args.emit_handoff:
        handoff = build_agent_handoff(repo, args, mode="go-auto")
        print(json.dumps(handoff, indent=2, ensure_ascii=False) if args.json else handoff["command"])
        return 0
    if args.execute:
        exit_code, result = execute_loop_plan(repo, args, mode="go-auto")
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"go-auto execute: {result['status']}")
            print("completed_tasks: " + (", ".join(result["completed_tasks"]) if result["completed_tasks"] else "none"))
            if result.get("blocked_task"):
                print(f"blocked_task: {result['blocked_task']}")
        return exit_code
    plan = build_loop_plan(repo, args, mode="go-auto")
    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        print(f"go-auto: {plan['project_id']}")
        print("control: handed off until blocker")
        print("next_tasks: " + (", ".join(plan["next_tasks"]) if plan["next_tasks"] else "none"))
        print("can_escalate_to: " + (", ".join(plan["can_escalate_to"]) if plan["can_escalate_to"] else "none"))
        print("loop: " + " → ".join(plan["loop"]))
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if args.emit_handoff:
        handoff = build_agent_handoff(repo, args, mode="go-loop")
        print(json.dumps(handoff, indent=2, ensure_ascii=False) if args.json else handoff["command"])
        return 0
    if args.execute:
        exit_code, result = execute_loop_plan(repo, args, mode="go-loop")
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"go-loop execute: {result['status']}")
            print("completed_tasks: " + (", ".join(result["completed_tasks"]) if result["completed_tasks"] else "none"))
            if result.get("blocked_task"):
                print(f"blocked_task: {result['blocked_task']}")
        return exit_code
    plan = build_loop_plan(repo, args, mode="go-loop")
    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        print(f"go-loop: {plan['project_id']}")
        print("control: handed off until blocker")
        print("next_tasks: " + (", ".join(plan["next_tasks"]) if plan["next_tasks"] else "none"))
        print("loop: " + " → ".join(plan["loop"]))
    return 0


def cmd_router(args: argparse.Namespace) -> int:
    raw_command = args.command or "go"
    normalized = normalize_router_command(raw_command)
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    state = {
        "repo_exists": repo.exists(),
        "is_git_repo": (repo / ".git").is_dir(),
        "has_go": root.is_dir(),
        "has_project": (root / "project.json").is_file(),
        "has_vision": (root / "vision.json").is_file(),
        "has_principles": (root / "architecture-principles.json").is_file(),
        "has_hierarchy": (root / "hierarchy.json").is_file(),
        "open_task_count": 0,
        "active_task_count": 0,
        "blocked_task_count": 0,
        "done_task_count": 0,
        "valid": False,
        "errors": [],
    }
    if state["has_go"]:
        for status in ("open", "active", "blocked", "done"):
            state[f"{status}_task_count"] = len(list((root / "tasks" / status).glob("*.json")))
    if state["has_project"]:
        errors = validate_repo(repo)
        state["valid"] = not errors
        state["errors"] = errors
    intent = (args.intent or "").strip().lower()
    recommended: dict[str, Any] = recommend_route(normalized, intent, state)
    if recommended["command"] == "spike":
        repair = recommended.get("mode") != "create_repo"
        brief = args.intent or ("<repair intent>" if repair else "<intent>")
        recommended["example"] = f"python3 {Path(__file__).resolve()} spike {repo} --brief \"{brief}\"" + (" --skip-repo-complete" if recommended.get("mode") == "repair_existing_repo" else "")
    elif recommended["command"] in {"auto", "go-loop"}:
        recommended["example"] = f"python3 {Path(__file__).resolve()} {recommended['command']} {repo} --max-tasks {args.max_tasks}"
    elif recommended["command"] == "task create":
        recommended["example"] = f"python3 {Path(__file__).resolve()} task create {repo} --summary \"<next task>\""
    result = {
        "schema": "go-workflow.router.v1",
        "normalized_command": normalized,
        "raw_command": raw_command,
        "intent": args.intent,
        "repo": str(repo),
        "state": state,
        "recommended": recommended,
        "router_policy": "Normalize /^go+$/i tokens (go, GO, Go, GOO, gOo) to the repo-local go router; normalize loop/go-loop/goloop to go-loop; then choose spike/auto/go-loop/task-create from repo state.",
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"command: {raw_command} -> {normalized}")
        print(f"repo_exists: {state['repo_exists']}")
        print(f"has_go: {state['has_go']}")
        print(f"has_vision: {state['has_vision']}")
        print(f"has_principles: {state['has_principles']}")
        print(f"open_tasks: {state['open_task_count']}")
        print(f"recommended: {recommended['command']} — {recommended['reason']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    route = route_repo(repo)
    status: dict[str, Any] = {"repo": str(repo), "route": route}
    if route["mode"] == "repo-local" and route["valid"]:
        root = go_root(repo)
        project = load_json(root / "project.json")
        project_mode = str(project.get("project_mode") or "project")
        status["project"] = {"id": project.get("id"), "name": project.get("name"), "mode": project_mode}
        counts = {}
        for state in ("open", "active", "blocked", "done"):
            counts[state] = len(list((root / "tasks" / state).glob("*.json")))
        status["tasks"] = counts
        tasks = open_tasks(repo)
        status["setup_required"] = project_mode == "template"
        if status["setup_required"]:
            status["setup_command"] = './go spike . --brief "<project intent>"'
            status["next"] = None
        else:
            status["next"] = None if not tasks else {"id": tasks[0][1].get("id"), "summary": tasks[0][1].get("summary")}
        status["dirty"] = classify_dirty(repo, [".go/**"])
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    else:
        print(f"mode: {route['mode']}")
        print(f"valid: {route['valid']}")
        if status.get("project"):
            print(f"project: {status['project']['name']} ({status['project']['id']})")
            print("tasks: " + ", ".join(f"{k}={v}" for k, v in status["tasks"].items()))
            nxt = status.get("next")
            print("next: none" if not nxt else f"next: {nxt['id']} — {nxt['summary']}")
            blocking = status.get("dirty", {}).get("blocking", [])
            print(f"dirty_blocking: {len(blocking)}")
        if route.get("errors"):
            print("errors:")
            for error in route["errors"]:
                print(f"- {error}")
    return 0 if route["valid"] else 1


def cmd_template_check(args: argparse.Namespace) -> int:
    """Validate that a project template still works with this stack checkout."""
    template = Path(args.template_repo).resolve()
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    record("template_exists", template.is_dir(), str(template))
    record("template_has_go", go_root(template).is_dir(), str(go_root(template)))
    errors = validate_repo(template) if go_root(template).is_dir() else [f"missing .go directory: {go_root(template)}"]
    record("validate", not errors, "; ".join(errors))
    if not errors:
        route = route_repo(template)
        tasks = open_tasks(template)
        record("route_repo_local", route.get("mode") == "repo-local" and route.get("valid") is True, json.dumps(route, sort_keys=True))
        record("has_claimable_example_task", bool(tasks), tasks[0][1].get("id", "") if tasks else "")
    makefile = template / "Makefile"
    check_script = template / "scripts" / "check.sh"
    record("makefile_present", makefile.is_file(), str(makefile))
    record("check_script_present", check_script.is_file(), str(check_script))
    if makefile.is_file():
        make_text = makefile.read_text(encoding="utf-8")
        record("makefile_uses_stack_cli", "cli/go.py validate" in make_text and "cli/go.py readback" in make_text, "Makefile should validate and read back via stack CLI")
    if check_script.is_file():
        script_text = check_script.read_text(encoding="utf-8")
        record("check_script_bootstraps_stack", "bootstrap-stack.sh" in script_text, "check.sh should make a fresh template clone self-checkable")
    if not errors and open_tasks(template):
        with tempfile.TemporaryDirectory(prefix="go-template-check-") as temp_dir:
            clone = Path(temp_dir) / "template"
            shutil.copytree(template, clone, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
            subprocess.run(["git", "init", "-q", str(clone)], text=True, capture_output=True, check=False)
            subprocess.run(["git", "add", "."], cwd=clone, text=True, capture_output=True, check=False)
            seeded = subprocess.run(
                ["git", "-c", "user.name=Template Check", "-c", "user.email=template-check@example.com", "commit", "-m", "seed template check", "-q"],
                cwd=clone,
                text=True,
                capture_output=True,
                check=False,
            )
            env = os.environ.copy()
            env["GO_STACK"] = str(STACK_ROOT)
            env["GO_STACK_ALLOW_DEV"] = "1"
            executed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "auto", str(clone), "--max-tasks", "1", "--max-attempts", "1", "--execute", "--agent", "template-check", "--json"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            try:
                auto_result = json.loads(executed.stdout)
            except json.JSONDecodeError:
                auto_result = {}
            auto_ok = seeded.returncode == 0 and executed.returncode == 0 and auto_result.get("status") == "done" and bool(auto_result.get("completed_tasks"))
            detail = json.dumps({
                "seed_returncode": seeded.returncode,
                "auto_returncode": executed.returncode,
                "status": auto_result.get("status"),
                "completed_tasks": auto_result.get("completed_tasks", []),
                "stderr": (executed.stderr or "")[-500:],
            }, ensure_ascii=False)
            record("first_auto_execute", auto_ok, detail)
    else:
        record("first_auto_execute", False, "template must validate and contain an open task")

    ok = all(item["ok"] for item in checks)
    result = {
        "schema": "go-workflow.template-check.v1",
        "stack": str(STACK_ROOT),
        "template": str(template),
        "ok": ok,
        "checks": checks,
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"template: {template}")
        for item in checks:
            marker = "ok" if item["ok"] else "FAIL"
            suffix = f" — {item['detail']}" if item["detail"] else ""
            print(f"{marker}: {item['name']}{suffix}")
    return 0 if ok else 1


def cmd_task_create(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    project = load_json(root / "project.json")
    task_id = slugify(args.id or args.summary)
    if not TASK_ID_RE.fullmatch(task_id):
        raise RepoLocalError(f"invalid task id: {task_id}")
    for status in ("open", "active", "blocked", "done"):
        if task_path(root, status, task_id).exists():
            raise RepoLocalError(f"task already exists: {task_id}")
    acceptance = args.acceptance or ["Task result is implemented and verified."]
    verification = args.verification or project.get("default_verification") or ["git diff --check"]
    task = {
        "schema": TASK_SCHEMA,
        "kind": "task",
        "execution_mode": args.execution_mode,
        "id": task_id,
        "project": project["id"],
        "status": "open",
        "summary": args.summary,
        "description": args.description or args.summary,
        "scope": {"read": args.read or [".go/**"], "modify": args.modify or [".go/**"]},
        "acceptance": acceptance,
        "verification": verification,
        "claim": {"agent": None, "claimed_at": None},
        "evidence": [],
    }
    if args.feature and args.epic:
        raise RepoLocalError("use either --feature or --epic, not both")
    target = task_path(root, "open", task_id)
    dump_json(target, task)
    try:
        if args.epic:
            append_task_to_epic(root, args.epic, task_id)
        else:
            append_task_to_hierarchy(root, args.feature, task_id)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    errors = validate_repo(repo)
    if errors:
        target.unlink(missing_ok=True)
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(relative(repo, target))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    repo.mkdir(parents=True, exist_ok=True)
    copy_fixture_init(repo, force=args.force)
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"initialized: {repo / '.go'}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    try:
        plan, documents = plan_contract_migration(
            load_json(root / "project.json"),
            load_json(root / "hierarchy.json"),
        )
    except ValueError as exc:
        raise RepoLocalError(str(exc)) from exc
    if args.apply and plan["changes"]:
        project_path = root / "project.json"
        hierarchy_path = root / "hierarchy.json"
        before_project = project_path.read_text(encoding="utf-8")
        before_hierarchy = hierarchy_path.read_text(encoding="utf-8")
        dump_json(project_path, documents["project.json"])
        dump_json(hierarchy_path, documents["hierarchy.json"])
        errors = validate_repo(repo)
        if errors:
            atomic_write_text(project_path, before_project)
            atomic_write_text(hierarchy_path, before_hierarchy)
            raise RepoLocalError("migration produced an invalid contract:\n- " + "\n- ".join(errors))
        plan["applied"] = True
        append_jsonl(
            root / "runs" / "events.jsonl",
            event("contract-migration", "run.checked", args.agent, {
                "action": "contract.migrated",
                "from_version": plan["from_version"],
                "to_version": plan["to_version"],
                "changes": plan["changes"],
            }),
        )
    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        mode = "applied" if plan["applied"] else "dry-run"
        print(f"migration {mode}: v{plan['from_version']} -> v{plan['to_version']}")
        for change in plan["changes"]:
            print(f"- {change['path']}: " + "; ".join(change["operations"]))
        if not plan["changes"]:
            print("- no changes")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"ok: {repo / '.go'}")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    tasks = open_tasks(repo)
    if not tasks:
        print("no open tasks")
        return 0
    _, data = tasks[0]
    print(f"{data['id']} — {data['summary']}")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    try:
        with repository_lock(root, f"task-{args.task_id}"):
            path, data = find_task(root, args.task_id)
            if data.get("status") != "open":
                raise RepoLocalError(f"task is not open: {data.get('status')}")
            if data.get("claim", {}).get("agent"):
                raise RepoLocalError(f"task already claimed by {data['claim']['agent']}")
            dirty = classify_dirty(repo, data.get("scope", {}).get("modify", []))
            if dirty["blocking"] and not args.allow_dirty:
                raise RepoLocalError("blocking dirty state before claim:\n- " + "\n- ".join(dirty["blocking"]))
            data["status"] = "active"
            data["claim"] = {"agent": args.agent, "claimed_at": now_iso()}
            target = task_path(root, "active", data["id"])
            atomic_move_json(path, target, data)
            append_jsonl(root / "runs" / "events.jsonl", event(data["id"], "task.claimed", args.agent, {"report_only_dirty": dirty["report_only"]}))
    except StateLockError as exc:
        raise RepoLocalError(str(exc)) from exc
    print(relative(repo, target))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    try:
        with repository_lock(root, f"task-{args.task_id}"):
            path, data = find_task(root, args.task_id)
            if data.get("status") != "active":
                raise RepoLocalError(f"task is not active: {data.get('status')}")
            if args.agent and data.get("claim", {}).get("agent") not in {args.agent, None, ""}:
                raise RepoLocalError(f"task claimed by {data.get('claim', {}).get('agent')}, not {args.agent}")
            if not args.evidence.strip():
                raise RepoLocalError("finish requires evidence")
            data["status"] = "done"
            data.setdefault("evidence", []).append({"created_at": now_iso(), "agent": args.agent, "summary": args.evidence})
            target = task_path(root, "done", data["id"])
            atomic_move_json(path, target, data)
            append_jsonl(root / "evidence" / "events.jsonl", event(data["id"], "task.finished", args.agent, {"evidence": args.evidence}))
    except StateLockError as exc:
        raise RepoLocalError(str(exc)) from exc
    print(relative(repo, target))
    return 0


def cmd_dirty_check(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    owned = args.owned or [".go/**"]
    dirty = classify_dirty(repo, owned)
    print(json.dumps(dirty, indent=2, ensure_ascii=False))
    return 1 if dirty["blocking"] and args.fail_on_blocking else 0



def load_jsonl_events(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            events.append(data)
    if limit <= 0:
        return []
    return events[-limit:]


def collect_tasks(root: Path, include_done: bool = False) -> dict[str, Any]:
    states = ("open", "active", "blocked", "done")
    result: dict[str, Any] = {"counts": {}, "records": {}}
    for state in states:
        records = []
        for path in sorted((root / "tasks" / state).glob("*.json")):
            data = load_json(path)
            if state != "done" or include_done:
                records.append({
                    "id": data.get("id"),
                    "status": data.get("status"),
                    "summary": data.get("summary"),
                    "scope": data.get("scope"),
                    "acceptance": data.get("acceptance", []),
                    "verification": data.get("verification", []),
                    "evidence": data.get("evidence", []),
                })
        result["counts"][state] = len(list((root / "tasks" / state).glob("*.json")))
        result["records"][state] = records
    return result


def build_export_bundle(repo: Path, include_done: bool = False, max_events: int = 20) -> dict[str, Any]:
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot export invalid .go state:\n- " + "\n- ".join(errors))
    root = go_root(repo)
    project = load_json(root / "project.json")
    vision = load_json(root / "vision.json")
    principles = load_json(root / "architecture-principles.json")
    hierarchy = load_json(root / "hierarchy.json")
    tasks = collect_tasks(root, include_done=include_done)
    next_tasks = tasks["records"].get("open", [])
    return {
        "schema": EXPORT_BUNDLE_SCHEMA,
        "kind": "export_bundle",
        "bundle_id": f"{slugify(str(project.get('id') or repo.name))}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}",
        "created_at": now_iso(),
        "source": {
            "repo_name": repo.name,
            "project_id": project.get("id"),
            "project_name": project.get("name"),
        },
        "readback": {
            "north_star": vision.get("north_star"),
            "wedge": vision.get("wedge"),
            "principles": [p.get("id") for p in principles.get("principles", []) if isinstance(p, dict)],
            "epics": [g.get("id") for g in hierarchy_epics(hierarchy)],
            "next_task": None if not next_tasks else {"id": next_tasks[0].get("id"), "summary": next_tasks[0].get("summary")},
        },
        "tasks": tasks,
        "history": {
            "runs": load_jsonl_events(root / "runs" / "events.jsonl", max_events),
            "evidence": load_jsonl_events(root / "evidence" / "events.jsonl", max_events),
            "decisions": load_jsonl_events(root / "decisions" / "events.jsonl", max_events),
        },
    }


def validate_export_bundle(data: dict[str, Any]) -> None:
    if data.get("schema") != EXPORT_BUNDLE_SCHEMA:
        raise RepoLocalError("bundle schema mismatch")
    if data.get("kind") != "export_bundle":
        raise RepoLocalError("bundle kind must be export_bundle")
    source = data.get("source")
    if not isinstance(source, dict) or not source.get("project_id"):
        raise RepoLocalError("bundle source.project_id required")
    if not data.get("bundle_id"):
        raise RepoLocalError("bundle_id required")


def cmd_bundle_export(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    bundle = build_export_bundle(repo, include_done=args.include_done, max_events=args.max_events)
    text = json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(output)
    else:
        print(text, end="")
    return 0


def cmd_bundle_import(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot import into invalid .go state:\n- " + "\n- ".join(errors))
    bundle = load_json(Path(args.bundle).resolve())
    validate_export_bundle(bundle)
    root = go_root(repo)
    project = load_json(root / "project.json")
    source = bundle["source"]
    import_name = slugify(str(bundle["bundle_id"])) + ".json"
    target = root / "imports" / import_name
    plan = {
        "schema": "go-workflow.repo-local.import-plan.v1",
        "kind": "import_plan",
        "mode": "write" if args.write else "dry_run",
        "target_project": {"id": project.get("id"), "name": project.get("name")},
        "source_project": {"id": source.get("project_id"), "name": source.get("project_name")},
        "bundle_id": bundle.get("bundle_id"),
        "target_path": relative(repo, target),
        "source_task_counts": bundle.get("tasks", {}).get("counts", {}),
        "actions": [
            "validate target .go state",
            "validate export bundle schema",
            "write immutable import artifact under .go/imports/" if args.write else "dry-run only; no files written",
            "append decision.recorded event" if args.write else "skip event append until --write",
        ],
    }
    if not args.write:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    if target.exists() and not args.force:
        raise RepoLocalError(f"import artifact already exists: {relative(repo, target)}; pass --force to replace")
    target.parent.mkdir(parents=True, exist_ok=True)
    dump_json(target, {"plan": plan, "bundle": bundle})
    append_jsonl(root / "decisions" / "events.jsonl", event(
        args.task_id,
        "decision.recorded",
        args.agent,
        {
            "decision": "imported repo-local export bundle as review/reconcile artifact",
            "bundle_id": bundle.get("bundle_id"),
            "source_project": source.get("project_id"),
            "target_path": relative(repo, target),
        },
    ))
    errors = validate_repo(repo)
    if errors:
        target.unlink(missing_ok=True)
        raise RepoLocalError("import wrote invalid .go state:\n- " + "\n- ".join(errors))
    print(relative(repo, target))
    return 0

def cmd_decision_create(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot record decision in invalid .go state:\n- " + "\n- ".join(errors))
    decision_id = slugify(args.id or args.title)
    if not TASK_ID_RE.fullmatch(decision_id):
        raise RepoLocalError(f"invalid decision id: {decision_id}")
    append_jsonl(root / "decisions" / "events.jsonl", event(
        args.task_id or "project-decision",
        "decision.recorded",
        args.agent,
        {
            "decision_id": decision_id,
            "title": args.title,
            "status": args.status,
            "context": args.context,
            "decision": args.decision,
            "consequences": args.consequence or [],
        },
    ))
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("decision wrote invalid .go state:\n- " + "\n- ".join(errors))
    print(f"{decision_id} — {args.title}")
    return 0


def cmd_epic_create(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("cannot create epic in invalid .go state:\n- " + "\n- ".join(errors))
    epic_id = slugify(args.id or args.title)
    path = root / "hierarchy.json"
    hierarchy = load_json(path)
    epics = hierarchy_epics(hierarchy)
    if any(epic.get("id") == epic_id for epic in epics):
        raise RepoLocalError(f"epic already exists: {epic_id}")
    epics.append({
        "id": epic_id,
        "title": args.title,
        "description": args.description,
        "features": [],
        "tasks": [],
    })
    set_hierarchy_epics(hierarchy, epics)
    dump_json(path, hierarchy)
    errors = validate_repo(repo)
    if errors:
        raise RepoLocalError("epic wrote invalid .go state:\n- " + "\n- ".join(errors))
    print(f"{epic_id} — {args.title}")
    return 0


def cmd_readback(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    errors = validate_repo(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    root = go_root(repo)
    project = load_json(root / "project.json")
    vision = load_json(root / "vision.json")
    principles = load_json(root / "architecture-principles.json")
    hierarchy = load_json(root / "hierarchy.json")
    tasks = open_tasks(repo)
    print(f"Project: {project['name']} ({project['id']})")
    print(f"North star: {vision['north_star']}")
    print(f"Wedge: {vision['wedge']}")
    print("Principles: " + "; ".join(p["id"] for p in principles.get("principles", [])))
    print("Epics: " + "; ".join(g["id"] for g in hierarchy_epics(hierarchy)))
    if tasks:
        print(f"Next task: {tasks[0][1]['id']} — {tasks[0][1]['summary']}")
    else:
        print("Next task: none")
    return 0


def route_repo(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    root = go_root(repo)
    project_file = root / "project.json"
    if project_file.exists():
        errors = validate_repo(repo)
        project_id = ""
        project_name = ""
        try:
            project = load_json(project_file)
            project_id = str(project.get("id") or "")
            project_name = str(project.get("name") or "")
        except RepoLocalError:
            pass
        return {
            "repo": str(repo),
            "mode": "repo-local",
            "state_root": ".go",
            "project_id": project_id,
            "project_name": project_name,
            "valid": not errors,
            "reason": ".go/project.json exists",
            "fallback": "aw-lite",
            "errors": errors,
        }
    return {
        "repo": str(repo),
        "mode": "aw-lite-fallback",
        "state_root": "system/agent-workflow",
        "project_id": "",
        "project_name": "",
        "valid": True,
        "reason": "no .go/project.json in target repo",
        "fallback": "aw-lite",
        "errors": [],
    }


def cmd_route(args: argparse.Namespace) -> int:
    route = route_repo(Path(args.repo))
    if args.json:
        print(json.dumps(route, indent=2, ensure_ascii=False))
    else:
        print(f"mode: {route['mode']}")
        print(f"repo: {route['repo']}")
        print(f"state_root: {route['state_root']}")
        if route.get("project_id"):
            print(f"project: {route['project_name']} ({route['project_id']})")
        print(f"reason: {route['reason']}")
        if route.get("errors"):
            print("errors:")
            for error in route["errors"]:
                print(f"- {error}")
    return 0 if route["valid"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    version = sub.add_parser("version", help="Report the stack and contract runtime versions")
    version.add_argument("--json", action="store_true")
    version.set_defaults(func=cmd_version)
    adopt = sub.add_parser("adopt", help="Adopt a repo by creating real repo-local .go state")
    adopt.add_argument("repo", nargs="?", default=".")
    adopt.add_argument("--project-id")
    adopt.add_argument("--name")
    adopt.add_argument("--repo-url", default="")
    adopt.add_argument("--verification", action="append", default=[])
    adopt.add_argument("--north-star", default="")
    adopt.add_argument("--wedge", default="")
    adopt.add_argument("--target-user", default="")
    adopt.add_argument("--core-promise", default="")
    adopt.add_argument("--product-principle", action="append", default=[])
    adopt.add_argument("--non-goal", action="append", default=[])
    adopt.add_argument("--success-metric", action="append", default=[])
    adopt.add_argument("--principle", action="append", default=[], help="id|statement|rationale|enforcement")
    adopt.add_argument("--feature-group", action="append", default=[], help="legacy alias for epic: id|title")
    adopt.add_argument("--feature", action="append", default=[], help="epic_id|feature_id|title")
    adopt.add_argument("--force", action="store_true")
    adopt.set_defaults(func=cmd_adopt)
    spike = sub.add_parser("spike", help="Bootstrap a repo and .go contract from rough intent")
    spike.add_argument("repo")
    spike.add_argument("--project-id")
    spike.add_argument("--name")
    spike.add_argument("--repo-url", default="")
    spike.add_argument("--brief", default="")
    spike.add_argument("--north-star", default="")
    spike.add_argument("--wedge", default="")
    spike.add_argument("--target-user", default="")
    spike.add_argument("--core-promise", default="")
    spike.add_argument("--product-principle", action="append", default=[])
    spike.add_argument("--non-goal", action="append", default=[])
    spike.add_argument("--success-metric", action="append", default=[])
    spike.add_argument("--principle", action="append", default=[], help="id|statement|rationale|enforcement")
    spike.add_argument("--epic", action="append", default=[], help="epic_id|title")
    spike.add_argument("--target-epic", default="", help="epic id to attach generated tasks to")
    spike.add_argument("--task", action="append", default=[], help="task_id|summary")
    spike.add_argument("--task-scope", choices=["code", "docs"], default="code", help="default scope preset for generated tasks")
    spike.add_argument("--execution-mode", choices=["mechanical", "agent"], default="agent", help="execution mode for generated tasks")
    spike.add_argument("--verification", action="append", default=[])
    spike.add_argument("--skip-repo-complete", action="store_true")
    spike.add_argument("--agent", default="agent")
    spike.add_argument("--json", action="store_true")
    spike.set_defaults(func=cmd_spike)
    go = sub.add_parser("go", help="Bare go universal router: loose command vs repo-local .go autonomous loop")
    go.add_argument("repo", nargs="?", default=".")
    go.add_argument("--intent", default="")
    go.add_argument("--loop", action="store_true", help="force go-loop rather than go-auto")
    go.add_argument("--write", action="store_true", help="materialize intent-created tasks; default --json/plan mode is non-mutating")
    go.add_argument("--execute", action="store_true", help="execute selected auto/go-loop lifecycle")
    go.add_argument("--max-tasks", type=int, default=10)
    go.add_argument("--summary-chars", type=int, default=900)
    go.add_argument("--max-minutes", type=int, default=90)
    go.add_argument("--max-commands", type=int, default=120)
    go.add_argument("--command-timeout-seconds", type=int, default=900)
    go.add_argument("--max-attempts", type=int, default=5)
    go.add_argument("--build-command", default="", help="optional adapter command run before verification; supports {repo}, {task_id}, {attempt}, {strategy}")
    go.add_argument("--critic-command", default="", help="optional adapter command run after passing verification; non-zero blocks/repairs")
    go.add_argument("--repair-command", default="", help="optional adapter command run after failed verify/critic before next attempt")
    go.add_argument("--repair-agent", choices=["codex", "hermes"], default="", help="use a built-in repair adapter command template")
    go.add_argument("--executor-agent", choices=["auto", "codex", "hermes", "none"], default=executor_agent_default(), help="executor for agent tasks (default: GO_EXECUTOR_AGENT or auto)")
    go.add_argument("--semantic-critic", action=argparse.BooleanOptionalAction, default=True, help="run built-in semantic critic before finish (default: enabled)")
    go.add_argument("--followup-on-block", action="store_true", help="create a scoped follow-up task when critic blocks")
    go.add_argument("--checkpoint-every-tasks", type=int, default=1)
    go.add_argument("--ship-policy", choices=["none", "local-commit", "push"], default="none")
    go.add_argument("--allow-push", action="store_true")
    go.add_argument("--agent", default="agent")
    go.add_argument("--allow-dirty", action="store_true")
    go.add_argument("--json", action="store_true")
    go.set_defaults(func=cmd_go)
    auto = sub.add_parser("auto", help="Emit the go-auto control-handoff contract; the invoking coding agent executes until done/gate/budget")
    auto.add_argument("repo", nargs="?", default=".")
    auto.add_argument("--max-tasks", type=int, default=3)
    auto.add_argument("--summary-chars", type=int, default=900)
    auto.add_argument("--max-minutes", type=int, default=45)
    auto.add_argument("--max-commands", type=int, default=36)
    auto.add_argument("--command-timeout-seconds", type=int, default=900)
    auto.add_argument("--max-attempts", type=int, default=5)
    auto.add_argument("--build-command", default="", help="optional adapter command run before verification; supports {repo}, {task_id}, {attempt}, {strategy}")
    auto.add_argument("--critic-command", default="", help="optional adapter command run after passing verification; non-zero blocks/repairs")
    auto.add_argument("--repair-command", default="", help="optional adapter command run after failed verify/critic before next attempt")
    auto.add_argument("--repair-agent", choices=["codex", "hermes"], default="", help="use a built-in repair adapter command template")
    auto.add_argument("--executor-agent", choices=["auto", "codex", "hermes", "none"], default=executor_agent_default(), help="executor for agent tasks (default: GO_EXECUTOR_AGENT or auto)")
    auto.add_argument("--semantic-critic", action=argparse.BooleanOptionalAction, default=True, help="run built-in semantic critic before finish (default: enabled)")
    auto.add_argument("--followup-on-block", action="store_true", help="create a scoped follow-up task when critic blocks")
    auto.add_argument("--checkpoint-every-tasks", type=int, default=1)
    auto.add_argument("--ship-policy", choices=["none", "local-commit", "push"], default="none")
    auto.add_argument("--allow-push", action="store_true")
    auto.add_argument("--execute", action="store_true", help="execute the lifecycle: preflight, claim, run verification, finish/block, reflect")
    auto.add_argument("--emit-handoff", action="store_true", help="emit a Codex/Hermes-compatible agent handoff JSON")
    auto.add_argument("--agent", default="agent")
    auto.add_argument("--allow-dirty", action="store_true", help="explicitly override dirty/lock preflight gates")
    auto.add_argument("--json", action="store_true")
    auto.set_defaults(func=cmd_auto)
    adapter = sub.add_parser("adapter", help="Inspect and validate the versioned agent-adapter protocol")
    adapter_sub = adapter.add_subparsers(dest="adapter_command", required=True)
    adapter_validate = adapter_sub.add_parser("validate-result", help="Validate one adapter result JSON document")
    adapter_validate.add_argument("result")
    adapter_validate.add_argument("--phase", choices=["build", "critic", "repair"])
    adapter_validate.add_argument("--json", action="store_true")
    adapter_validate.set_defaults(func=cmd_adapter_validate_result)
    proof = sub.add_parser("proof", help="Validate and explicitly copy live runtime proof artifacts")
    proof_sub = proof.add_subparsers(dest="proof_command", required=True)
    proof_validate = proof_sub.add_parser("validate", help="Fail-closed validation for live Hermes proof")
    proof_validate.add_argument("proof")
    proof_validate.add_argument("--evidence-root", default="", help="recompute doctor/first/resumed hashes from this directory")
    proof_validate.add_argument("--copy-to", default="", help="copy only after successful validation with --evidence-root")
    proof_validate.add_argument("--json", action="store_true")
    proof_validate.set_defaults(func=cmd_proof_validate)
    stack = sub.add_parser("stack", help="Plan or apply immutable project stack pin updates")
    stack_sub = stack.add_subparsers(dest="stack_command", required=True)
    stack_update = stack_sub.add_parser("update", help="Validate and update required_stack_version and stack_ref")
    stack_update.add_argument("repo", nargs="?", default=".")
    stack_update.add_argument("--to", required=True, help="immutable target tag vX.Y.Z")
    stack_update.add_argument("--stack-repo", default=str(STACK_ROOT), help="stack git checkout used to resolve and inspect the tag")
    stack_update.add_argument("--apply", action="store_true", help="apply transaction; default is dry-run")
    stack_update.add_argument("--agent", default="agent")
    stack_update.add_argument("--json", action="store_true")
    stack_update.set_defaults(func=cmd_stack_update)
    agent_check = sub.add_parser("agent-check", help="Report repair-agent adapter availability")
    agent_check.add_argument("--agent", action="append", choices=["codex", "hermes"], default=[])
    agent_check.add_argument("--json", action="store_true")
    agent_check.set_defaults(func=cmd_agent_check)
    doctor = sub.add_parser("doctor", help="Check Linux/WSL prerequisites, agent availability, and stack compatibility")
    doctor.add_argument("repo", nargs="?", default=".")
    doctor.add_argument("--platform", choices=["auto", "linux", "wsl"], default="auto")
    doctor.add_argument("--agent", choices=["codex", "hermes"], default="hermes")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)
    for loop_command, loop_help in (
        ("loop", "Run the stronger go-loop control-handoff contract until blocker"),
        ("go-loop", "Alias for loop; explicit go-loop control-handoff contract"),
    ):
        loop = sub.add_parser(loop_command, help=loop_help)
        loop.add_argument("repo", nargs="?", default=".")
        loop.add_argument("--max-tasks", type=int, default=10)
        loop.add_argument("--summary-chars", type=int, default=900)
        loop.add_argument("--max-minutes", type=int, default=90)
        loop.add_argument("--max-commands", type=int, default=120)
        loop.add_argument("--command-timeout-seconds", type=int, default=900)
        loop.add_argument("--max-attempts", type=int, default=5)
        loop.add_argument("--build-command", default="", help="optional adapter command run before verification; supports {repo}, {task_id}, {attempt}, {strategy}")
        loop.add_argument("--critic-command", default="", help="optional adapter command run after passing verification; non-zero blocks/repairs")
        loop.add_argument("--repair-command", default="", help="optional adapter command run after failed verify/critic before next attempt")
        loop.add_argument("--repair-agent", choices=["codex", "hermes"], default="", help="use a built-in repair adapter command template")
        loop.add_argument("--executor-agent", choices=["auto", "codex", "hermes", "none"], default=executor_agent_default(), help="executor for agent tasks (default: GO_EXECUTOR_AGENT or auto)")
        loop.add_argument("--semantic-critic", action=argparse.BooleanOptionalAction, default=True, help="run built-in semantic critic before finish (default: enabled)")
        loop.add_argument("--followup-on-block", action="store_true", help="create a scoped follow-up task when critic blocks")
        loop.add_argument("--checkpoint-every-tasks", type=int, default=1)
        loop.add_argument("--ship-policy", choices=["none", "local-commit", "push"], default="none")
        loop.add_argument("--allow-push", action="store_true")
        loop.add_argument("--execute", action="store_true", help="execute the lifecycle until done/blocker/budget/safety gate")
        loop.add_argument("--emit-handoff", action="store_true", help="emit a Codex/Hermes-compatible agent handoff JSON")
        loop.add_argument("--agent", default="agent")
        loop.add_argument("--allow-dirty", action="store_true", help="explicitly override dirty/lock preflight gates")
        loop.add_argument("--json", action="store_true")
        loop.set_defaults(func=cmd_loop)
    router = sub.add_parser("router", help="Normalize go/GO/GOO commands and choose spike/auto/task route from repo state")
    router.add_argument("repo", nargs="?", default=".")
    router.add_argument("--command", default="go")
    router.add_argument("--intent", default="")
    router.add_argument("--max-tasks", type=int, default=3)
    router.add_argument("--json", action="store_true")
    router.set_defaults(func=cmd_router)
    status = sub.add_parser("status", help="Summarize route, project, task counts, next work, and dirty state")
    status.add_argument("repo", nargs="?", default=".")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)
    template_check = sub.add_parser("template-check", help="Validate a go-project-template checkout against this stack")
    template_check.add_argument("template_repo", nargs="?", default="../go-project-template")
    template_check.add_argument("--json", action="store_true")
    template_check.set_defaults(func=cmd_template_check)
    task = sub.add_parser("task", help="Author repo-local tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_create = task_sub.add_parser("create", help="Create an open repo-local task")
    task_create.add_argument("repo")
    task_create.add_argument("--id")
    task_create.add_argument("--summary", required=True)
    task_create.add_argument("--description", default="")
    task_create.add_argument("--feature", default="", help="epic_id.feature_id")
    task_create.add_argument("--epic", default="", help="epic_id; attaches the task directly to an epic")
    task_create.add_argument("--read", action="append", default=[])
    task_create.add_argument("--modify", action="append", default=[])
    task_create.add_argument("--execution-mode", choices=["mechanical", "agent"], default="mechanical")
    task_create.add_argument("--acceptance", action="append", default=[])
    task_create.add_argument("--verification", action="append", default=[])
    task_create.set_defaults(func=cmd_task_create)
    epic = sub.add_parser("epic", help="Author repo-local epics")
    epic_sub = epic.add_subparsers(dest="epic_command", required=True)
    epic_create = epic_sub.add_parser("create", help="Create an epic in hierarchy.json")
    epic_create.add_argument("repo")
    epic_create.add_argument("--id")
    epic_create.add_argument("--title", required=True)
    epic_create.add_argument("--description", default="")
    epic_create.set_defaults(func=cmd_epic_create)
    decision = sub.add_parser("decision", help="Record ADR-lite repo-local decisions")
    decision_sub = decision.add_subparsers(dest="decision_command", required=True)
    decision_create = decision_sub.add_parser("create", help="Append a decision.recorded event")
    decision_create.add_argument("repo")
    decision_create.add_argument("--id")
    decision_create.add_argument("--title", required=True)
    decision_create.add_argument("--status", default="accepted", choices=["proposed", "accepted", "superseded", "rejected"])
    decision_create.add_argument("--context", required=True)
    decision_create.add_argument("--decision", required=True)
    decision_create.add_argument("--consequence", action="append", default=[])
    decision_create.add_argument("--agent", default="agent")
    decision_create.add_argument("--task-id", default="project-decision")
    decision_create.set_defaults(func=cmd_decision_create)
    migrate = sub.add_parser("migrate", help="Plan or explicitly apply versioned .go contract migrations")
    migrate.add_argument("repo", nargs="?", default=".")
    migrate.add_argument("--apply", action="store_true", help="write the proposed migration; default is dry-run")
    migrate.add_argument("--agent", default="agent")
    migrate.add_argument("--json", action="store_true")
    migrate.set_defaults(func=cmd_migrate)
    init = sub.add_parser("init", help="Initialize .go fixture state in a repo")
    init.add_argument("repo", nargs="?", default=".")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)
    validate = sub.add_parser("validate", help="Validate repo-local .go state")
    validate.add_argument("repo", nargs="?", default=".")
    validate.set_defaults(func=cmd_validate)
    nxt = sub.add_parser("next", help="Print the first claimable open task")
    nxt.add_argument("repo", nargs="?", default=".")
    nxt.set_defaults(func=cmd_next)
    claim = sub.add_parser("claim", help="Claim one repo-local task")
    claim.add_argument("task_id")
    claim.add_argument("--repo", default=".")
    claim.add_argument("--agent", default="agent")
    claim.add_argument("--allow-dirty", action="store_true")
    claim.set_defaults(func=cmd_claim)
    finish = sub.add_parser("finish", help="Finish one repo-local active task")
    finish.add_argument("task_id")
    finish.add_argument("--repo", default=".")
    finish.add_argument("--agent", default="agent")
    finish.add_argument("--evidence", required=True)
    finish.set_defaults(func=cmd_finish)
    dirty = sub.add_parser("dirty-check", help="Classify dirty git state against owned paths")
    dirty.add_argument("repo", nargs="?", default=".")
    dirty.add_argument("--owned", action="append", default=[])
    dirty.add_argument("--fail-on-blocking", action="store_true")
    dirty.set_defaults(func=cmd_dirty_check)
    readback = sub.add_parser("readback", help="Summarize a repo from .go state only")
    readback.add_argument("repo", nargs="?", default=".")
    readback.set_defaults(func=cmd_readback)
    route = sub.add_parser("route", help="Classify a target repo as repo-local .go or AW Lite fallback")
    route.add_argument("repo", nargs="?", default=".")
    route.add_argument("--json", action="store_true")
    route.set_defaults(func=cmd_route)
    bundle = sub.add_parser("bundle", help="Export/import compact repo-local .go bundles")
    bundle_sub = bundle.add_subparsers(dest="bundle_command", required=True)
    bundle_export = bundle_sub.add_parser("export", help="Export a compact .go readback/task/history bundle")
    bundle_export.add_argument("repo", nargs="?", default=".")
    bundle_export.add_argument("--output", default="")
    bundle_export.add_argument("--include-done", action="store_true", help="Include done task summaries/evidence instead of counts only")
    bundle_export.add_argument("--max-events", type=int, default=20, help="Maximum recent events per JSONL stream")
    bundle_export.set_defaults(func=cmd_bundle_export)
    bundle_import = bundle_sub.add_parser("import", help="Validate and optionally write an import/reconcile artifact")
    bundle_import.add_argument("repo")
    bundle_import.add_argument("bundle")
    bundle_import.add_argument("--write", action="store_true", help="Write .go/imports/<bundle_id>.json and append a decision event")
    bundle_import.add_argument("--force", action="store_true", help="Replace an existing import artifact with the same bundle id")
    bundle_import.add_argument("--agent", default="agent")
    bundle_import.add_argument("--task-id", default="bundle-import")
    bundle_import.set_defaults(func=cmd_bundle_import)
    return parser


def cmd_agent_check(args: argparse.Namespace) -> int:
    agents = args.agent or ["codex", "hermes"]
    payload = {"schema": "go-workflow.agent-check.v1", "agents": [repair_agent_available(agent) for agent in agents]}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for item in payload["agents"]:
            status = "available" if item["available"] else "missing"
            print(f"{item['agent']}: {status} {item.get('path') or ''}".rstrip())
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    payload = {
        "schema": "go-workflow.runtime-version.v1",
        "stack_version": STACK_VERSION,
        "stack_ref": STACK_REF,
        "contract_version": CURRENT_CONTRACT_VERSION,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(STACK_VERSION)
    return 0


def cmd_stack_update(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    stack_repo = Path(args.stack_repo).resolve()
    try:
        plan = plan_stack_update(repo, stack_repo, args.to)
    except StackUpdateError as exc:
        raise RepoLocalError(str(exc)) from exc
    result = plan
    if args.apply:
        result = apply_stack_update(repo, plan)
        errors = validate_repo(repo)
        if errors:
            rollback_stack_update(repo, result["rollback_record"])
            raise RepoLocalError("stack update rolled back because the resulting contract is invalid:\n- " + "\n- ".join(errors))
        append_jsonl(
            go_root(repo) / "runs" / "events.jsonl",
            event("stack-update", "run.checked", args.agent, {
                "action": "stack.updated",
                "from_ref": result.get("from_ref"),
                "to_ref": result["to_ref"],
                "resolved_commit": result["resolved_commit"],
                "rollback_record": result["rollback_record"],
            }),
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"stack update {result['mode']}: {result.get('from_ref')} -> {result['to_ref']}")
        print(f"commit: {result['resolved_commit']}")
        if result.get("rollback_record"):
            print(f"rollback: {result['rollback_record']}")
    return 0


def cmd_adapter_validate_result(args: argparse.Namespace) -> int:
    data = load_json(Path(args.result))
    errors = validate_adapter_result(data, expected_phase=args.phase)
    payload = {
        "schema": "go-workflow.agent-adapter-validation.v1",
        "valid": not errors,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("valid adapter result" if not errors else "invalid adapter result: " + "; ".join(errors))
    return 0 if not errors else 1


def cmd_proof_validate(args: argparse.Namespace) -> int:
    source = Path(args.proof).resolve()
    data = load_json(source)
    errors = validate_live_hermes_proof(data)
    if args.copy_to and not args.evidence_root:
        errors.append("--copy-to requires --evidence-root so raw result hashes and semantics are verified")
    if args.evidence_root and not errors:
        errors.extend(verify_live_hermes_evidence(data, Path(args.evidence_root).resolve()))
    copied_to = None
    if not errors and args.copy_to:
        target = Path(args.copy_to).resolve()
        atomic_json(target, data)
        copied_to = str(target)
    payload = {
        "schema": "go-workflow.live-proof-validation.v1",
        "proof": str(source),
        "valid": not errors,
        "errors": errors,
        "evidence_root": str(Path(args.evidence_root).resolve()) if args.evidence_root else None,
        "copied_to": copied_to,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("valid live Hermes proof" if not errors else "invalid live Hermes proof: " + "; ".join(errors))
        if copied_to:
            print(f"copied: {copied_to}")
    return 0 if not errors else 1


def semantic_version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def cmd_doctor(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    project_path = go_root(repo) / "project.json"
    project = load_json(project_path) if project_path.is_file() else {}
    required_version = str(project.get("required_stack_version") or "0.0.0")
    required_ref = str(project.get("stack_ref") or "")
    compatible = semantic_version_tuple(STACK_VERSION) >= semantic_version_tuple(required_version)
    git_head = subprocess.run(
        ["git", "-C", str(STACK_ROOT), "rev-parse", "HEAD"], text=True, capture_output=True,
    ).stdout.strip()
    if not required_ref:
        pinned_commit = ""
        exact_ref = True
    elif re.fullmatch(r"[0-9a-f]{40}", required_ref):
        pinned_commit = required_ref
        exact_ref = git_head == pinned_commit
    else:
        pinned_commit = subprocess.run(
            ["git", "-C", str(STACK_ROOT), "rev-parse", "-q", "--verify", f"refs/tags/{required_ref}^{{commit}}"],
            text=True,
            capture_output=True,
        ).stdout.strip()
        exact_ref = bool(pinned_commit) and git_head == pinned_commit
    development_override = os.environ.get("GO_STACK_ALLOW_DEV") == "1" and not exact_ref
    ref_compatible = exact_ref or development_override
    prerequisites: list[dict[str, Any]] = [
        {"name": "python", "available": sys.version_info >= (3, 11), "version": ".".join(str(part) for part in sys.version_info[:3]), "path": sys.executable},
    ]
    for name in ("git", "bash", "make", "uv"):
        path = shutil.which(name)
        prerequisites.append({"name": name, "available": bool(path), "path": path})
    agent_availability = repair_agent_available(args.agent)
    agent = {"name": args.agent, "available": agent_availability["available"], "path": agent_availability["path"]}
    contract_errors = validate_repo(repo) if project_path.is_file() else [".go/project.json is missing"]
    actions: list[str] = []
    for item in prerequisites:
        if not item["available"]:
            actions.append(f"install {item['name']}")
    if not agent["available"]:
        actions.append(f"install {args.agent} and make it available on PATH")
    if not compatible:
        actions.append(f"update go-workflow-stack to at least {required_version}")
    if not ref_compatible:
        actions.append(f"checkout the pinned go-workflow-stack ref {required_ref}")
    if contract_errors:
        actions.append("repair the .go project contract")
    ready = not actions
    payload = {
        "schema": "go-workflow.doctor.v1",
        "repo": str(repo),
        "platform": detected_platform(args.platform),
        "prerequisites": prerequisites,
        "agent": agent,
        "stack": {
            "version": STACK_VERSION,
            "ref": STACK_REF,
            "git_head": git_head or None,
            "pinned_commit": pinned_commit or None,
            "required_version": required_version,
            "required_ref": required_ref or None,
            "exact_ref": exact_ref,
            "development_override": development_override,
            "compatible": compatible and ref_compatible,
        },
        "contract": {"valid": not contract_errors, "errors": contract_errors},
        "ready": ready,
        "actions": actions,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"platform: {payload['platform']['kind']}")
        print(f"stack: {STACK_VERSION} (required {required_version})")
        print(f"agent: {args.agent} ({'available' if agent['available'] else 'missing'})")
        print("ready" if ready else "not ready: " + "; ".join(actions))
    return 0 if ready else 1


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except (RepoLocalError, StateLockError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
