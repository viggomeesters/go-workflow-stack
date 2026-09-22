#!/usr/bin/env python3
"""Validate preserved live-autonomy proof without replaying external effects."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


SHA = re.compile(r"^[0-9a-f]{40}$")


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return value


def bound_raw(proof: Path, reference: dict) -> dict:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ValueError("raw command reference is malformed")
    path = proof / str(reference["path"])
    if not path.is_file() or path.parent.resolve() != proof.resolve():
        raise ValueError("raw command path is unavailable or escapes proof directory")
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ValueError("raw command hash does not match preserved bytes")
    return read_json(path)


def parsed_stdout(raw: dict) -> dict:
    try:
        value = json.loads(raw.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("controller raw stdout is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("controller raw stdout must be an object")
    return value


def validate_proof(proof: Path, *, pilot_path: Path) -> dict:
    proof = Path(proof).resolve()
    pilot_path = Path(pilot_path).resolve()
    manifest = read_json(proof / "manifest.json")
    if manifest.get("schema") != "go-workflow.autonomy-live-proof.v1" or manifest.get("status") != "passed":
        raise ValueError("proof must be a passed autonomy live proof")
    if manifest.get("mode") != "live-native-codex-unattended":
        raise ValueError("proof is not a real native Codex unattended run")
    pilot = manifest.get("pilot") or {}
    if hashlib.sha256(pilot_path.read_bytes()).hexdigest() != pilot.get("sha256"):
        raise ValueError("pilot authority hash does not match the frozen contract")
    if manifest.get("human_interventions") != 0:
        raise ValueError("proof required human interventions after launch")
    elapsed = (manifest.get("timing") or {}).get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed <= 0:
        raise ValueError("proof lacks measured elapsed time")

    commands = manifest.get("commands") or {}
    required = {"baseline", "intake", "initial_controller", "resume_controller"}
    if set(commands) != required:
        raise ValueError("proof does not preserve every required raw command")
    raw = {name: bound_raw(proof, commands[name]) for name in sorted(required)}
    if raw["baseline"].get("returncode") == 0 or not (manifest.get("fault") or {}).get("baseline_failed"):
        raise ValueError("seeded fault was not observed before live work")
    if not (manifest.get("fault") or {}).get("final_passed"):
        raise ValueError("seeded fault has no final passing observation")
    intake = parsed_stdout(raw["intake"])
    expected_tasks = ["normalize-notes", "render-report"]
    if raw["intake"].get("returncode") != 0 or intake.get("status") != "applied" or intake.get("task_ids") != expected_tasks:
        raise ValueError("real intake did not materialize the frozen two-task work")

    interruption = manifest.get("interruption") or {}
    if interruption.get("automatic") is not True or interruption.get("kind") != "SIGTERM":
        raise ValueError("proof lacks an automatic interruption")
    if raw["initial_controller"].get("returncode") != interruption.get("initial_returncode"):
        raise ValueError("interruption return code differs from raw controller evidence")
    if interruption.get("initial_returncode") in {0, None}:
        raise ValueError("initial controller was not actually interrupted")
    resumed = parsed_stdout(raw["resume_controller"])
    if (raw["resume_controller"].get("returncode") != 0 or resumed.get("status") != "goal_verified"
            or resumed.get("goal_verified") is not True or resumed.get("completed_tasks") != expected_tasks):
        raise ValueError("automatic resume did not verify the complete goal")

    workers = manifest.get("workers")
    if not isinstance(workers, list) or [item.get("task_id") for item in workers] != expected_tasks:
        raise ValueError("proof lacks both real worker bindings")
    if any(item.get("adapter") != "codex" or not item.get("model") or not item.get("effort") for item in workers):
        raise ValueError("worker binding is not native Codex with an explicit profile")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or [item.get("id") for item in tasks] != expected_tasks:
        raise ValueError("proof lacks both delivered tasks")
    if len({item.get("tag") for item in tasks}) != 2:
        raise ValueError("tasks do not have separate releases")
    for item in tasks:
        if not SHA.fullmatch(str(item.get("commit") or "")) or item.get("remote_commit") != item.get("commit"):
            raise ValueError("task remote readback does not match its released commit")
    audit = manifest.get("goal_audit") or {}
    if audit.get("status") != "achieved" or audit.get("goal_verified") is not True:
        raise ValueError("goal audit did not prove the original goal")
    git_state = manifest.get("git") or {}
    if git_state.get("clean") is not True or git_state.get("local_main") != git_state.get("remote_main"):
        raise ValueError("final Git state is not clean and remotely read back")
    usage = manifest.get("usage") or {}
    if usage.get("status") != "observed" or not usage.get("records"):
        raise ValueError("proof lacks observed adapter usage")
    privacy = manifest.get("privacy") or {}
    if privacy.get("synthetic_only") is not True or privacy.get("private_repositories_touched") != []:
        raise ValueError("proof touched or disclosed a private repository")
    if not manifest.get("limitations"):
        raise ValueError("proof must retain live-runtime limitations")
    return {"ok": True, "task_count": len(tasks), "elapsed_seconds": elapsed,
            "tags": [item["tag"] for item in tasks]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--pilot", type=Path, default=Path(__file__).resolve().parents[1] / "fixtures/autonomy-live/pilot.json")
    args = parser.parse_args()
    result = validate_proof(args.proof, pilot_path=args.pilot)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
