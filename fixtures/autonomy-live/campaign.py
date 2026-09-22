#!/usr/bin/env python3
"""Run the bounded, synthetic, native-Codex autonomy proof.

The proof is deliberately local-only: it creates a new repository and bare Git
remote, seeds one observable defect, runs semantic intake and the campaign
controller, interrupts the controller after the first release, and resumes it.
No existing consumer repository is opened or changed.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TASK_IDS = ["normalize-notes", "render-report"]


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task(seed: dict, *, task_id: str, summary: str, description: str,
          acceptance: list[str], verification: list[str], model: dict, order: int) -> dict:
    task = copy.deepcopy(seed)
    task.update(
        id=task_id,
        project="autonomy-live-notes",
        summary=summary,
        description=description,
        execution_mode="agent",
        shareable_delivery="none",
        order=order,
        scope={
            "read": [".go/**", "notes.py", "tests/**", "README.md", "VERSION", "CHANGELOG.md"],
            "modify": ["notes.py", "README.md", "VERSION", "CHANGELOG.md"],
        },
        acceptance=acceptance,
        verification=verification,
        work_status="pending",
        review_status="none",
        execution_contract={
            "schema": "go-workflow.execution-contract.v1",
            "task_kind": "product",
            "model": model,
            "release": {"mode": "required", "profile": "local"},
            "workspace": {
                "mode": "task_worktree",
                "base_branch": "main",
                "control_state": "repo_local_single_writer",
            },
        },
    )
    task.pop("requested_outcomes", None)
    task.pop("outcome_tracking_version", None)
    return task


def _decision_event() -> dict:
    return {
        "schema": "go-workflow.repo-local.event.v1",
        "kind": "event",
        "event": "decision.recorded",
        "created_at": now(),
        "task_id": "autonomy-live",
        "agent": "autonomy-live-controller",
        "data": {
            "decision_id": "autonomy-live-local-only",
            "title": "Confine the live proof to a disposable local repository",
            "status": "accepted",
            "context": "A real agent run needs observable Git effects without touching consumer data or hosted services.",
            "decision": "Use synthetic notes data, a new repository, a local bare remote, and no deployment.",
            "consequences": ["Every task still requires its own annotated tag and remote readback."],
        },
    }


def prepare(work_root: Path, *, runtime: Path = ROOT, pilot_path: Path = HERE / "pilot.json") -> dict:
    """Create the never-before-used synthetic repository and its frozen tasks."""
    work_root = Path(work_root).resolve()
    runtime = Path(runtime).resolve()
    pilot_path = Path(pilot_path).resolve()
    if work_root.exists():
        raise ValueError("Live pilot work root must be new; existing evidence is never overwritten")
    pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
    work_root.mkdir(parents=True)
    repo = work_root / "project"
    shutil.copytree(runtime / pilot["repository"]["seed"], repo)
    remote = work_root / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "autonomy-live@example.invalid")
    git(repo, "config", "user.name", "Autonomy live pilot")
    git(repo, "remote", "add", "origin", str(remote))

    version = subprocess.check_output(
        [sys.executable, str(runtime / "cli/go.py"), "version"], text=True
    ).strip()
    project_path = repo / ".go/project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project.update(
        id=pilot["repository"]["project_id"],
        name="Synthetic autonomy live notes",
        required_stack_version=version,
        stack_ref="v" + version,
    )
    project["default_verification"] = ["python3 -m pytest tests/test_notes.py -q"]
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
            "deployment": {"mode": "none", "reason": pilot["deployment"]["reason"]},
        }
    }
    write(project_path, project)

    seed_path = repo / ".go/tasks/open/task-schema-smoke.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    seed_path.unlink()
    descriptions = {
        "normalize-notes": (
            "Repair the deliberately faulty normalize_notes implementation. Strip surrounding whitespace, "
            "discard blank entries, and de-duplicate while preserving first-seen order. Work only on this task; "
            "do not delegate, start subagents, commit, push, tag, edit .go, VERSION, or CHANGELOG. The controller "
            "owns workflow state and publication. The critic must inspect and report through the required protocol only."
        ),
        "render-report": (
            "Add render_report(lines) using normalize_notes. Return a Markdown '# Notes' heading and bullet list, "
            "or '# Notes' plus '_No notes._' for an empty result. Update README with the public behavior. Work only "
            "on this task; do not delegate, start subagents, commit, push, tag, edit .go, VERSION, or CHANGELOG. "
            "The controller owns workflow state and publication. The critic reports through the required protocol only."
        ),
    }
    tasks = [
        _task(
            seed,
            task_id="normalize-notes",
            summary="Repair deterministic note normalization",
            description=descriptions["normalize-notes"],
            acceptance=["Whitespace and blanks are removed; duplicates retain stable first-seen order."],
            verification=["python3 -m pytest tests/test_notes.py -q -k normalize"],
            model={key: pilot["models"][0][key] for key in ("id", "effort")},
            order=1,
        ),
        _task(
            seed,
            task_id="render-report",
            summary="Add a normalized Markdown notes report",
            description=descriptions["render-report"],
            acceptance=["Markdown rendering uses normalized notes and has an explicit empty state."],
            verification=["python3 -m pytest tests/test_notes.py -q -k render"],
            model={key: pilot["models"][1][key] for key in ("id", "effort")},
            order=2,
        ),
    ]
    tasks[1]["dependencies"] = [{
        "project": project["id"],
        "task_id": tasks[0]["id"],
        "requires": "done_with_required_release_evidence",
    }]
    for task in tasks:
        write(repo / ".go/tasks/open" / f"{task['id']}.json", task)

    hierarchy_path = repo / ".go/hierarchy.json"
    hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8"))
    hierarchy["project"] = project["id"]
    hierarchy["epics"][0]["features"][0]["tasks"] = TASK_IDS
    write(hierarchy_path, hierarchy)
    for name in ("vision.json", "architecture-principles.json"):
        path = repo / ".go" / name
        value = json.loads(path.read_text(encoding="utf-8"))
        value["project"] = project["id"]
        write(path, value)
    decisions = repo / ".go/decisions/events.jsonl"
    decisions.parent.mkdir(parents=True, exist_ok=True)
    decisions.write_text(json.dumps(_decision_event(), ensure_ascii=False) + "\n", encoding="utf-8")

    (repo / "notes.py").write_text(
        '"""Small synthetic notes library used by the autonomy proof."""\n\n'
        "def normalize_notes(lines):\n"
        "    return list(lines)\n",
        encoding="utf-8",
    )
    tests = repo / "tests/test_notes.py"
    tests.parent.mkdir(parents=True)
    tests.write_text(
        "import notes\n\n\n"
        "def test_normalize_notes_strips_blanks_and_stably_deduplicates():\n"
        "    assert notes.normalize_notes([' alpha ', '', 'beta', 'alpha', '  ']) == ['alpha', 'beta']\n\n\n"
        "def test_render_report_uses_cleaned_entries():\n"
        "    assert notes.render_report([' alpha ', 'alpha', 'beta']) == '# Notes\\n\\n- alpha\\n- beta\\n'\n\n\n"
        "def test_render_report_has_empty_state():\n"
        "    assert notes.render_report([' ', '']) == '# Notes\\n\\n_No notes._\\n'\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# Synthetic notes\n\nDisposable local autonomy proof.\n", encoding="utf-8")
    (repo / "VERSION").write_text("1.1.0\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.1.0\n\n- Seed synthetic proof.\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(
        "# Synthetic autonomy pilot\n\n"
        "This is a disposable local proof. Follow the repository-local `.go/` task exactly. "
        "Never create subagents or external effects. Product workers may edit only `notes.py` and `README.md`; "
        "the controller exclusively owns `.go/`, Git, versions, changelog, tags, and publication.\n",
        encoding="utf-8",
    )
    synced = subprocess.run(
        [sys.executable, str(runtime / "cli/go.py"), "agents", "sync", str(repo), "--apply", "--json"],
        cwd=repo, text=True, capture_output=True, timeout=30,
    )
    if synced.returncode:
        raise RuntimeError(synced.stdout + synced.stderr)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "seed synthetic live autonomy pilot")
    git(repo, "tag", "-a", "v1.1.0", "-m", "synthetic pilot baseline")
    git(repo, "push", "origin", "main", "refs/tags/v1.1.0")
    return {
        "work_root": work_root,
        "repo": repo,
        "remote": remote,
        "runtime": runtime,
        "pilot_path": pilot_path,
        "pilot": pilot,
    }


def _raw(path: Path, args: list[str], result: subprocess.CompletedProcess[str], elapsed: float) -> dict:
    payload = {
        "argv": args,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "elapsed_seconds": round(elapsed, 3),
    }
    write(path, payload)
    return {"path": path.name, "sha256": sha(path)}


def _run(args: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    return result, time.monotonic() - started


def _contract(repo: Path, pilot: dict) -> dict:
    tasks = {task_id: json.loads((repo / f".go/tasks/open/{task_id}.json").read_text()) for task_id in TASK_IDS}
    outcomes = []
    for index, task_id in enumerate(TASK_IDS, 1):
        requested = tasks[task_id]["requested_outcomes"]
        # Intake may retain multiple requirements; bind all of them to the matching goal outcome.
        outcomes.append({
            "id": f"O{index}",
            "text": requested[-1]["text"],
            "task_outcomes": [{
                "task_id": task_id,
                "requirement_id": item["id"],
                "text_sha256": hashlib.sha256(item["text"].encode()).hexdigest(),
            } for item in requested],
            "required_evidence": ["verification", "critic", "release"],
        })
    return {
        "schema": "go-workflow.campaign-contract.v1",
        "id": pilot["id"],
        "project": pilot["repository"]["project_id"],
        "revision": 1,
        "previous_sha256": None,
        "intent": {
            "text": pilot["intent"],
            "sha256": hashlib.sha256(pilot["intent"].encode()).hexdigest(),
            "source_ref": pilot["authority"]["source_ref"],
        },
        "goal": {
            "text": "Deliver both synthetic notes outcomes as separate verified local releases.",
            "non_goals": ["Hosted publication", "Deployment", "Private consumer repositories"],
            "outcomes": outcomes,
        },
        "basis": {
            "vision_sha256": sha(repo / ".go/vision.json"),
            "principles_sha256": sha(repo / ".go/architecture-principles.json"),
            "decision_ids": ["autonomy-live-local-only"],
        },
        "authority": {
            "mode": "execute",
            "source_ref": pilot["authority"]["source_ref"],
            "permitted_tasks": TASK_IDS,
            "expansion": {
                "research": {"max_tasks": 0, "modify": [], "outcome_ids": []},
                "repair": {"max_tasks": 0, "modify": [], "outcome_ids": []},
            },
            "models": [{"id": item["id"], "effort": item["effort"]} for item in pilot["models"]],
            "budget": {key: pilot["budget"][key] for key in ("wall_seconds", "max_tasks", "max_attempts")},
            "release": {"profiles": ["local"], "allow_push": True,
                        "source_ref": pilot["authority"]["source_ref"]},
            "deployment": {"targets": [], "source_ref": None},
            "stop_conditions": ["goal_verified", "budget_exhausted", "no_eligible_tasks",
                                "authority_required", "unsafe_repository", "unknown_external_effect"],
        },
        "decisions": [{
            "id": "autonomy-live-local-only",
            "status": "accepted",
            "source_ref": "decision:autonomy-live-local-only",
            "owner": "autonomy-live-controller",
            "resolution_gate": "existing campaign, completion, and release gates",
            "task_ids": TASK_IDS,
            "bounds": "New synthetic repository and local bare remote only",
        }],
    }


def _tag_commit(repo: Path, tag: str) -> str:
    return git(repo, "rev-list", "-n", "1", tag)


def _remote_tag_commit(repo: Path, tag: str) -> str:
    output = git(repo, "ls-remote", "origin", f"refs/tags/{tag}^{{}}")
    if not output:
        output = git(repo, "ls-remote", "origin", f"refs/tags/{tag}")
    return output.split()[0] if output else ""


def _usage_records(repo: Path) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    for path in sorted((repo / ".go").rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                usage = item.get("usage")
                if isinstance(usage, dict) and usage:
                    digest = hashlib.sha256(json.dumps(usage, sort_keys=True).encode()).hexdigest()
                    if digest not in seen:
                        seen.add(digest)
                        records.append({"source": str(path.relative_to(repo)), "raw": usage})
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return records


def run_live(prepared: dict, *, proof_dir: Path) -> dict:
    proof_dir = Path(proof_dir).resolve()
    if proof_dir.exists():
        raise ValueError("Proof directory must be new; failed attempts are immutable evidence")
    proof_dir.mkdir(parents=True)
    repo = prepared["repo"]
    runtime = prepared["runtime"]
    pilot = prepared["pilot"]
    env = dict(os.environ)
    env["GO_STACK_ALLOW_DEV"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started_at = now()
    started = time.monotonic()
    commands: dict[str, dict] = {}
    failure: str | None = None
    try:
        baseline_args = [sys.executable, "-m", "pytest", "tests/test_notes.py", "-q", "-k", "normalize"]
        result, elapsed = _run(baseline_args, cwd=repo, env=env, timeout=60)
        commands["baseline"] = _raw(proof_dir / "000-baseline.json", baseline_args, result, elapsed)
        if result.returncode == 0:
            raise RuntimeError("seeded normalization fault unexpectedly passed")

        intake_profile = pilot["intake_model"]
        intake_args = [
            sys.executable, str(runtime / "cli/go.py"), "intake", "explore", str(repo),
            "--intent", pilot["intent"], "--source-ref", pilot["authority"]["source_ref"],
            "--authority", "execute", "--executor-agent", "codex", "--model", intake_profile["id"],
            "--effort", intake_profile["effort"], "--timeout-seconds",
            str(pilot["budget"]["command_timeout_seconds"]), "--write", "--agent",
            "autonomy-live-controller", "--json",
        ]
        result, elapsed = _run(intake_args, cwd=repo, env=env,
                               timeout=pilot["budget"]["command_timeout_seconds"] + 60)
        commands["intake"] = _raw(proof_dir / "001-intake.json", intake_args, result, elapsed)
        if result.returncode:
            raise RuntimeError("native intake failed: " + (result.stderr or result.stdout)[-2000:])
        intake_payload = json.loads(result.stdout)
        if intake_payload.get("task_ids") != TASK_IDS:
            raise RuntimeError(f"native intake did not retain frozen tasks: {intake_payload.get('task_ids')}")

        contract_path = prepared["work_root"] / "campaign.json"
        write(contract_path, _contract(repo, pilot))
        git(repo, "add", ".go")
        git(repo, "commit", "-qm", "bind semantic intake to autonomy campaign")
        git(repo, "push", "origin", "main")

        controller_args = [
            sys.executable, str(runtime / "cli/go.py"), "auto", str(repo),
            "--campaign", str(contract_path), "--campaign-workspace-root",
            str(prepared["work_root"] / "workspaces"), "--execute", "--agent",
            "autonomy-live-controller", "--executor-agent", "codex", "--max-commands",
            str(pilot["budget"]["max_commands"]), "--max-minutes",
            str(max(1, pilot["budget"]["wall_seconds"] // 60)), "--max-attempts", "4",
            "--command-timeout-seconds", str(pilot["budget"]["command_timeout_seconds"]),
            "--ship-policy", "push", "--allow-push", "--json",
        ]
        initial_started = time.monotonic()
        process = subprocess.Popen(
            controller_args, cwd=repo, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        deadline = initial_started + pilot["budget"]["wall_seconds"]
        first_tag = pilot["interruption"]["after_remote_tag"]
        interrupted = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if git(repo, "ls-remote", "origin", f"refs/tags/{first_tag}"):
                os.killpg(process.pid, signal.SIGTERM)
                interrupted = True
                break
            time.sleep(2)
        if process.poll() is None and not interrupted:
            os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)
        initial = subprocess.CompletedProcess(controller_args, process.returncode, stdout, stderr)
        commands["initial_controller"] = _raw(
            proof_dir / "002-controller-initial.json", controller_args, initial,
            time.monotonic() - initial_started,
        )
        if not interrupted or initial.returncode in {0, None}:
            raise RuntimeError("controller was not automatically interrupted after the first remote release")

        remaining = max(60, int(deadline - time.monotonic()))
        resumed, elapsed = _run(controller_args, cwd=repo, env=env, timeout=remaining)
        commands["resume_controller"] = _raw(
            proof_dir / "003-controller-resume.json", controller_args, resumed, elapsed
        )
        if resumed.returncode:
            raise RuntimeError("resumed controller failed: " + (resumed.stderr or resumed.stdout)[-3000:])
        resumed_payload = json.loads(resumed.stdout)
        if resumed_payload.get("status") != "goal_verified" or resumed_payload.get("goal_verified") is not True:
            raise RuntimeError("resumed controller did not verify the original goal")

        final_check = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_notes.py", "-q"], cwd=repo,
            env=env, text=True, capture_output=True, timeout=60,
        )
        if final_check.returncode:
            raise RuntimeError("final seeded behavior is not green: " + final_check.stdout + final_check.stderr)

        if git(repo, "status", "--porcelain"):
            git(repo, "add", ".go")
            git(repo, "commit", "-qm", "record final unattended campaign audit")
            git(repo, "push", "origin", "main")
        local_main = git(repo, "rev-parse", "main")
        remote_main = git(repo, "ls-remote", "origin", "refs/heads/main").split()[0]
        task_evidence = []
        for task_id, tag in zip(TASK_IDS, ("v1.2.0", "v1.3.0"), strict=True):
            task_evidence.append({
                "id": task_id,
                "tag": tag,
                "commit": _tag_commit(repo, tag),
                "remote_commit": _remote_tag_commit(repo, tag),
            })
        usage = _usage_records(repo)
        if not usage:
            raise RuntimeError("native adapter run did not preserve observable usage metadata")
        finished_at = now()
        manifest = {
            "schema": "go-workflow.autonomy-live-proof.v1",
            "status": "passed",
            "mode": "live-native-codex-unattended",
            "pilot": {"path": "fixtures/autonomy-live/pilot.json", "sha256": sha(prepared["pilot_path"])},
            "runtime": {"candidate": True, "revision": git(runtime, "rev-parse", "HEAD")},
            "timing": {"started_at": started_at, "finished_at": finished_at,
                       "elapsed_seconds": round(time.monotonic() - started, 3)},
            "human_interventions": 0,
            "commands": commands,
            "interruption": {"kind": "SIGTERM", "automatic": True, "after_remote_tag": first_tag,
                             "initial_returncode": initial.returncode},
            "fault": {"kind": "real_seeded_bug", "baseline_failed": True, "final_passed": True},
            "workers": [{"task_id": item["task_id"], "adapter": "codex",
                         "model": item["id"], "effort": item["effort"]} for item in pilot["models"]],
            "tasks": task_evidence,
            "goal_audit": {"status": resumed_payload["completion_audit"]["status"],
                           "goal_verified": resumed_payload["goal_verified"]},
            "git": {"clean": git(repo, "status", "--porcelain") == "",
                    "remote_main": remote_main, "local_main": local_main},
            "usage": {"status": "observed", "records": usage},
            "privacy": {"synthetic_only": True, "private_repositories_touched": []},
            "limitations": [
                "This was a bounded synthetic run, not an eight-hour production night.",
                "Effective provider model identity and billing are reported by the adapter, not independently attested.",
                "Publication used a local bare Git remote and there was no deployment target.",
            ],
        }
        write(proof_dir / "manifest.json", manifest)
        (proof_dir / "morning-report.md").write_text(
            "# Synthetic unattended autonomy pilot\n\n"
            f"- Result: passed\n- Elapsed: {manifest['timing']['elapsed_seconds']} seconds\n"
            "- Human interventions after launch: 0\n- Automatic interruption: SIGTERM after v1.2.0, then automatic resume\n"
            "- Releases: v1.2.0 and v1.3.0, each read back from the local bare remote\n"
            "- Fault: the seeded normalization failure was observed before the run and passed afterward\n"
            "- Boundary: synthetic repository only; no private consumer repository, hosted publication, or deployment\n",
            encoding="utf-8",
        )
        return manifest
    except Exception as exc:
        failure = str(exc)
        write(proof_dir / "failed-attempt.json", {
            "schema": "go-workflow.autonomy-live-attempt.v1",
            "status": "failed",
            "started_at": started_at,
            "finished_at": now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "commands": commands,
            "error": failure,
        })
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True,
                        help="Required acknowledgement that real model adapters will run")
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--proof-dir", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=ROOT)
    parser.add_argument("--pilot", type=Path, default=HERE / "pilot.json")
    args = parser.parse_args()
    prepared = prepare(args.work_root, runtime=args.runtime, pilot_path=args.pilot)
    manifest = run_live(prepared, proof_dir=args.proof_dir)
    print(json.dumps({"status": manifest["status"], "proof": str(args.proof_dir.resolve()),
                      "elapsed_seconds": manifest["timing"]["elapsed_seconds"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
