"""Reproducible rough-intake-to-two-release autonomy campaign fixture."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROFILES = [
    {"id": "gpt-5.6-terra", "effort": "high"},
    {"id": "gpt-6-astra", "effort": "medium"},
]
ROUGH_REQUEST = """Ship a bounded two-step local campaign.
1. Deliver alpha as its own verified release
2. Deliver beta as its own verified release"""


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def worker_source() -> str:
    return (HERE / "worker.py").read_text(encoding="utf-8")


def _record_invocation(destination: Path, name: str, args: list[str], result: subprocess.CompletedProcess[str]) -> dict:
    invocations = destination / "invocations"
    invocations.mkdir(exist_ok=True)
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        parsed = None
    payload = {
        "name": name,
        "argv": args,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "result": parsed,
    }
    write(invocations / f"{len(list(invocations.glob('*.json'))) + 1:03d}-{name}.json", payload)
    return payload


def _decision_event() -> dict:
    return {
        "schema": "go-workflow.repo-local.event.v1",
        "kind": "event",
        "event": "decision.recorded",
        "created_at": "2026-09-22T00:00:00Z",
        "task_id": "autonomy-fixture",
        "agent": "fixture",
        "data": {
            "decision_id": "autonomy-fixture-policy",
            "title": "Use local Git-only releases for deterministic autonomy proof",
            "status": "accepted",
            "context": "The fixture must exercise real Git effects without consumer or hosted mutations.",
            "decision": "Use a local bare remote, deterministic worker boundary and no deployment target.",
            "consequences": ["Every release still requires exact annotated-tag and remote readback."],
        },
    }


def _task(seed: dict, index: int) -> dict:
    task = copy.deepcopy(seed)
    task_id = "deliver-alpha" if index == 1 else "deliver-beta"
    artifact = "alpha.txt" if index == 1 else "beta.txt"
    expected = "alpha\n" if index == 1 else "beta\n"
    task.update(
        id=task_id,
        summary=f"Deliver {artifact} as its own verified release",
        description="Prepared release task; bounded intake will bind the exact rough-request outcome.",
        execution_mode="agent",
        shareable_delivery="none",
        order=index,
        scope={"read": [".go/**", artifact, "VERSION", "CHANGELOG.md"],
               "modify": [artifact, "VERSION", "CHANGELOG.md"]},
        acceptance=[f"{artifact} contains {expected!r}; its incremented release has verified remote readback"],
        verification=[
            f"python3 -c \"from pathlib import Path; assert Path('{artifact}').read_text() == {expected!r}; "
            "assert Path('VERSION').read_text().strip().startswith('1.')\""
        ],
        work_status="pending",
        review_status="none",
        execution_contract={
            "schema": "go-workflow.execution-contract.v1",
            "task_kind": "product",
            "model": PROFILES[index - 1],
            "release": {"mode": "required", "profile": "local"},
            "workspace": {"mode": "task_worktree", "base_branch": "main",
                          "control_state": "repo_local_single_writer"},
        },
    )
    task.pop("requested_outcomes", None)
    task.pop("outcome_tracking_version", None)
    return task


def _contract(repo: Path, *, unresolved_task: str | None = None) -> dict:
    tasks = {
        task_id: json.loads((repo / f".go/tasks/open/{task_id}.json").read_text())
        for task_id in ("deliver-alpha", "deliver-beta")
    }
    outcomes = []
    for index, task_id in enumerate(("deliver-alpha", "deliver-beta"), 1):
        outcome = tasks[task_id]["requested_outcomes"][0]
        outcomes.append({
            "id": f"O{index}",
            "text": outcome["text"],
            "task_outcomes": [{
                "task_id": task_id,
                "requirement_id": outcome["id"],
                "text_sha256": hashlib.sha256(outcome["text"].encode()).hexdigest(),
            }],
            "required_evidence": ["verification", "critic", "release"],
        })
    decisions = [{
        "id": "autonomy-fixture-policy",
        "status": "accepted",
        "source_ref": "decision:autonomy-fixture-policy",
        "owner": "fixture-maintainer",
        "resolution_gate": "existing campaign and release gates",
        "task_ids": ["deliver-alpha", "deliver-beta"],
        "bounds": "Local disposable repository and bare remote only",
    }]
    if unresolved_task:
        decisions.append({
            "id": "unresolved-alpha-choice",
            "status": "unresolved",
            "source_ref": "user:autonomy-fixture#open-choice",
            "owner": "fixture-user",
            "resolution_gate": "explicit choice before affected task execution",
            "task_ids": [unresolved_task],
            "bounds": "Only the named task is blocked; independent work may proceed",
        })
    return {
        "schema": "go-workflow.campaign-contract.v1",
        "id": "autonomy-fixture",
        "project": "repo-local-spike-fixture",
        "revision": 1,
        "previous_sha256": None,
        "intent": {
            "text": ROUGH_REQUEST,
            "sha256": hashlib.sha256(ROUGH_REQUEST.encode()).hexdigest(),
            "source_ref": "user:autonomy-fixture",
        },
        "goal": {
            "text": "Deliver both rough-request outcomes as separate verified local releases.",
            "non_goals": ["Hosted publication", "Real deployment", "Real model identity claims"],
            "outcomes": outcomes,
        },
        "basis": {
            "vision_sha256": hashlib.sha256((repo / ".go/vision.json").read_bytes()).hexdigest(),
            "principles_sha256": hashlib.sha256((repo / ".go/architecture-principles.json").read_bytes()).hexdigest(),
            "decision_ids": ["autonomy-fixture-policy"],
        },
        "authority": {
            "mode": "execute",
            "source_ref": "user:autonomy-fixture",
            "permitted_tasks": ["deliver-alpha", "deliver-beta"],
            "expansion": {
                "research": {"max_tasks": 0, "modify": [], "outcome_ids": []},
                "repair": {"max_tasks": 0, "modify": [], "outcome_ids": []},
            },
            "models": PROFILES,
            "budget": {"wall_seconds": 600, "max_tasks": 3, "max_attempts": 6},
            "release": {"profiles": ["local"], "allow_push": True,
                        "source_ref": "user:autonomy-fixture"},
            "deployment": {"targets": [], "source_ref": None},
            "stop_conditions": ["goal_verified", "budget_exhausted", "no_eligible_tasks",
                                "authority_required", "unsafe_repository", "unknown_external_effect"],
        },
        "decisions": decisions,
    }


def setup(destination: Path, *, runtime: Path = ROOT, unresolved_task: str | None = None,
          omit_requirement: bool = False, expect_failure: bool = False) -> dict:
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Campaign destination must be new; existing evidence is never overwritten")
    destination.mkdir(parents=True)
    repo = destination / "project"
    shutil.copytree(ROOT / "fixtures/minimal", repo)
    remote = destination / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "autonomy-campaign@example.invalid")
    git(repo, "config", "user.name", "Autonomy campaign fixture")
    git(repo, "remote", "add", "origin", str(remote))

    version = subprocess.check_output([sys.executable, str(runtime / "cli/go.py"), "version"], text=True).strip()
    project_path = repo / ".go/project.json"
    project = json.loads(project_path.read_text())
    project.update(required_stack_version=version, stack_ref="v" + version)
    project["release_profiles"] = {
        "local": {
            "provider": "git-tag",
            "remote": "origin",
            "branch": "main",
            "publication": {
                "version": {"path": "VERSION", "format": "text"},
                "bump": "minor",
                "tag_prefix": "v",
                "changelog": "CHANGELOG.md",
            },
            "deployment": {"mode": "none", "reason": "Disposable local Git proof has no deployment target"},
        }
    }
    write(project_path, project)
    seed_path = repo / ".go/tasks/open/task-schema-smoke.json"
    seed = json.loads(seed_path.read_text())
    seed_path.unlink()
    for index in (1, 2):
        task = _task(seed, index)
        write(repo / f".go/tasks/open/{task['id']}.json", task)
    hierarchy_path = repo / ".go/hierarchy.json"
    hierarchy = json.loads(hierarchy_path.read_text())
    hierarchy["epics"][0]["features"][0]["tasks"] = ["deliver-alpha", "deliver-beta"]
    write(hierarchy_path, hierarchy)
    decisions = repo / ".go/decisions/events.jsonl"
    decisions.parent.mkdir(parents=True, exist_ok=True)
    decisions.write_text(json.dumps(_decision_event()) + "\n", encoding="utf-8")
    (repo / "VERSION").write_text("1.1.0\n")
    (repo / "CHANGELOG.md").write_text("# Disposable autonomy campaign\n")
    (repo / "AGENTS.md").write_text(
        "Disposable local autonomy proof. Follow .go, use one worker at a time, and never create hosted effects.\n"
    )
    synced = subprocess.run(
        [sys.executable, str(runtime / "cli/go.py"), "agents", "sync", str(repo), "--apply", "--json"],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if synced.returncode:
        raise RuntimeError(synced.stdout + synced.stderr)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "seed two release-ready tasks")
    git(repo, "tag", "-a", "v1.1.0", "-m", "local baseline")
    git(repo, "push", "origin", "main", "refs/tags/v1.1.0")

    capture = destination / "intake-requests.jsonl"
    args = [
        sys.executable, str(runtime / "cli/go.py"), "intake", "explore", str(repo),
        "--intent", ROUGH_REQUEST,
        "--source-ref", "user:autonomy-fixture",
        "--authority", "execute",
        "--assessment-command", f"{sys.executable} {HERE / 'assessor.py'}",
        "--write", "--json",
    ]
    env = {**os.environ, "AUTONOMY_INTAKE_CAPTURE": str(capture), "PYTHONDONTWRITEBYTECODE": "1"}
    if omit_requirement:
        env["AUTONOMY_OMIT_REQUIREMENT"] = "1"
    intake = subprocess.run(args, cwd=repo, env=env, text=True, capture_output=True, timeout=60)
    recorded = _record_invocation(destination, "intake", args, intake)
    if intake.returncode:
        if not expect_failure:
            raise RuntimeError(intake.stdout + intake.stderr)
        return {**recorded, "repo": repo}
    if expect_failure:
        raise AssertionError("fixture intake unexpectedly succeeded")

    contract_path = destination / "campaign.json"
    write(contract_path, _contract(repo, unresolved_task=unresolved_task))
    git(repo, "add", ".go")
    git(repo, "commit", "-qm", "explore rough request and bind outcomes")
    git(repo, "push", "origin", "main")
    return {"repo": repo, "contract": contract_path, "destination": destination, "runtime": Path(runtime).resolve()}


def run(prepared: dict, *, env: dict[str, str]) -> dict:
    repo = prepared["repo"]
    destination = prepared["destination"]
    args = [
        sys.executable, str(prepared["runtime"] / "cli/go.py"), "auto", str(repo),
        "--campaign", str(prepared["contract"]),
        "--campaign-workspace-root", str(destination / "workspaces"),
        "--execute", "--agent", "autonomy-fixture-owner", "--executor-agent", "codex",
        "--max-commands", "80", "--max-minutes", "10", "--max-attempts", "4",
        "--command-timeout-seconds", "180", "--ship-policy", "push", "--allow-push", "--json",
    ]
    result = subprocess.run(args, cwd=repo, env=env, text=True, capture_output=True, timeout=600)
    return _record_invocation(destination, "controller", args, result)
