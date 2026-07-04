#!/usr/bin/env python3
"""Repo-local Go Workflow Stack vNext spike CLI.

Operates project-local `.go/` JSON/JSONL state. The CLI is intentionally
clone-local: execution commands read/write the target repo's `.go/` directory,
not the Life OS vault's Agent Workflow Lite task queue.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_ROOT = SCRIPT_DIR.parents[0]
CONTRACT_ROOT = STACK_ROOT
SCHEMA_ROOT = CONTRACT_ROOT / "schemas"
FIXTURE_ROOT = CONTRACT_ROOT / "fixtures" / "minimal" / ".go"

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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


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
    require(isinstance(data.get("default_verification"), list), errors, f"{rel}: default_verification must be a list")
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


def validate_hierarchy(data: dict[str, Any], rel: str) -> list[str]:
    errors: list[str] = []
    require(data.get("schema") == HIERARCHY_SCHEMA, errors, f"{rel}: schema mismatch")
    require(data.get("kind") == "hierarchy", errors, f"{rel}: kind must be hierarchy")
    require(bool(data.get("project")), errors, f"{rel}: project required")
    groups = data.get("feature_groups")
    require(isinstance(groups, list), errors, f"{rel}: feature_groups must be a list")
    if isinstance(groups, list):
        for group in groups:
            require(isinstance(group, dict) and bool(group.get("id")) and isinstance(group.get("features"), list), errors, f"{rel}: each feature_group needs id and features")
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
    return errors


def validate_event(data: dict[str, Any], rel: str, line_number: int) -> list[str]:
    prefix = f"{rel}:{line_number}"
    errors: list[str] = []
    require(data.get("schema") == EVENT_SCHEMA, errors, f"{prefix}: schema mismatch")
    require(data.get("kind") == "event", errors, f"{prefix}: kind must be event")
    require(data.get("event") in {"task.claimed", "task.finished", "evidence.appended", "decision.recorded", "run.checked"}, errors, f"{prefix}: invalid event")
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
    for filename, validator in validators.items():
        path = root / filename
        try:
            errors.extend(validator(load_json(path), relative(repo, path)))
        except RepoLocalError as exc:
            errors.append(str(exc))
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
            except RepoLocalError as exc:
                errors.append(str(exc))
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
    groups: dict[str, dict[str, Any]] = {}
    for value in feature_groups or ["workflow|Workflow"]:
        gid, title = parse_pipe_fields(value, 2, "--feature-group")
        groups[slugify(gid)] = {"id": slugify(gid), "title": title, "features": []}
    for value in features or ["workflow|repo-local-workflow|Repo-local workflow"]:
        gid, fid, title = parse_pipe_fields(value, 3, "--feature")
        gid = slugify(gid)
        if gid not in groups:
            groups[gid] = {"id": gid, "title": gid.replace("-", " ").title(), "features": []}
        groups[gid]["features"].append({"id": slugify(fid), "title": title, "tasks": []})
    return {"schema": HIERARCHY_SCHEMA, "kind": "hierarchy", "project": project_id, "feature_groups": list(groups.values())}


def append_task_to_hierarchy(root: Path, feature_ref: str, task_id: str) -> None:
    if not feature_ref:
        return
    if "." not in feature_ref:
        raise RepoLocalError("--feature must be formatted as group_id.feature_id")
    group_id, feature_id = [slugify(part) for part in feature_ref.split(".", 1)]
    path = root / "hierarchy.json"
    hierarchy = load_json(path)
    for group in hierarchy.get("feature_groups", []):
        if group.get("id") != group_id:
            continue
        for feature in group.get("features", []):
            if feature.get("id") != feature_id:
                continue
            tasks = feature.setdefault("tasks", [])
            if task_id not in tasks:
                tasks.append(task_id)
            dump_json(path, hierarchy)
            return
    raise RepoLocalError(f"feature not found in hierarchy: {feature_ref}")


def task_path(root: Path, status: str, task_id: str) -> Path:
    return root / "tasks" / status / f"{task_id}.json"


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
    root = go_root(repo)
    tasks = []
    for path in sorted((root / "tasks" / "open").glob("*.json")):
        tasks.append((path, load_json(path)))
    return tasks


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


def classify_dirty(repo: Path, owned_patterns: list[str]) -> dict[str, list[str]]:
    result = {"blocking": [], "report_only": []}
    for code, path in git_status(repo):
        reason = ""
        if "U" in code or code in {"AA", "DD"}:
            reason = "merge conflict"
        elif BLOCK_SECRET_RE.search(path):
            reason = "secret-looking path"
        elif code.strip().startswith("D") or code.endswith("D"):
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


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    route = route_repo(repo)
    status: dict[str, Any] = {"repo": str(repo), "route": route}
    if route["mode"] == "repo-local" and route["valid"]:
        root = go_root(repo)
        project = load_json(root / "project.json")
        status["project"] = {"id": project.get("id"), "name": project.get("name")}
        counts = {}
        for state in ("open", "active", "blocked", "done"):
            counts[state] = len(list((root / "tasks" / state).glob("*.json")))
        status["tasks"] = counts
        tasks = open_tasks(repo)
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
    target = task_path(root, "open", task_id)
    dump_json(target, task)
    try:
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
    dump_json(target, data)
    path.unlink()
    append_jsonl(root / "runs" / "events.jsonl", event(data["id"], "task.claimed", args.agent, {"report_only_dirty": dirty["report_only"]}))
    print(relative(repo, target))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    root = go_root(repo)
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
    dump_json(target, data)
    path.unlink()
    append_jsonl(root / "evidence" / "events.jsonl", event(data["id"], "task.finished", args.agent, {"evidence": args.evidence}))
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
        "bundle_id": f"{slugify(str(project.get('id') or repo.name))}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
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
            "feature_groups": [g.get("id") for g in hierarchy.get("feature_groups", []) if isinstance(g, dict)],
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
    target.write_text(json.dumps({"plan": plan, "bundle": bundle}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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
    print("Feature groups: " + "; ".join(g["id"] for g in hierarchy.get("feature_groups", [])))
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
    adopt.add_argument("--feature-group", action="append", default=[], help="id|title")
    adopt.add_argument("--feature", action="append", default=[], help="group_id|feature_id|title")
    adopt.add_argument("--force", action="store_true")
    adopt.set_defaults(func=cmd_adopt)
    status = sub.add_parser("status", help="Summarize route, project, task counts, next work, and dirty state")
    status.add_argument("repo", nargs="?", default=".")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)
    task = sub.add_parser("task", help="Author repo-local tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_create = task_sub.add_parser("create", help="Create an open repo-local task")
    task_create.add_argument("repo")
    task_create.add_argument("--id")
    task_create.add_argument("--summary", required=True)
    task_create.add_argument("--description", default="")
    task_create.add_argument("--feature", default="", help="group_id.feature_id")
    task_create.add_argument("--read", action="append", default=[])
    task_create.add_argument("--modify", action="append", default=[])
    task_create.add_argument("--acceptance", action="append", default=[])
    task_create.add_argument("--verification", action="append", default=[])
    task_create.set_defaults(func=cmd_task_create)
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


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except RepoLocalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
