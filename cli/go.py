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
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BLOCK_SECRET_RE = re.compile(r"(secret|token|credential|password|\.env|id_rsa|private[-_]key)", re.I)


class RepoLocalError(Exception):
    """Expected repo-local workflow failure."""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
