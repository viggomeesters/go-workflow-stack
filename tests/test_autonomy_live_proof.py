from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_PATH = ROOT / "fixtures/autonomy-live/campaign.py"
CHECKER_PATH = ROOT / "scripts/check-autonomy-proof.py"
PILOT_PATH = ROOT / "fixtures/autonomy-live/pilot.json"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def raw_record(path: Path, *, returncode: int, stdout: str) -> dict:
    value = {"argv": ["python3", "cli/go.py", "auto"], "returncode": returncode,
             "stdout": stdout, "stderr": "", "elapsed_seconds": 3.0}
    write_json(path, value)
    return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def valid_proof(tmp_path: Path) -> Path:
    proof = tmp_path / "proof"
    proof.mkdir()
    intake = raw_record(proof / "001-intake.json", returncode=0,
                        stdout=json.dumps({"status": "applied", "task_ids": ["normalize-notes", "render-report"]}))
    initial = raw_record(proof / "002-controller-initial.json", returncode=-15, stdout="")
    resumed = raw_record(proof / "003-controller-resume.json", returncode=0,
                         stdout=json.dumps({"status": "goal_verified", "goal_verified": True,
                                            "completed_tasks": ["normalize-notes", "render-report"]}))
    baseline = raw_record(proof / "000-baseline.json", returncode=1, stdout="1 failed")
    pilot_sha = hashlib.sha256(PILOT_PATH.read_bytes()).hexdigest()
    manifest = {
        "schema": "go-workflow.autonomy-live-proof.v1",
        "status": "passed",
        "mode": "live-native-codex-unattended",
        "pilot": {"path": "fixtures/autonomy-live/pilot.json", "sha256": pilot_sha},
        "timing": {"started_at": "2026-09-22T20:00:00Z", "finished_at": "2026-09-22T20:20:00Z",
                   "elapsed_seconds": 1200.0},
        "human_interventions": 0,
        "commands": {"baseline": baseline, "intake": intake, "initial_controller": initial,
                     "resume_controller": resumed},
        "interruption": {"kind": "SIGTERM", "automatic": True, "after_remote_tag": "v1.2.0",
                         "initial_returncode": -15},
        "fault": {"kind": "real_seeded_bug", "baseline_failed": True, "final_passed": True},
        "workers": [
            {"task_id": "normalize-notes", "adapter": "codex", "model": "gpt-5.6-terra", "effort": "high"},
            {"task_id": "render-report", "adapter": "codex", "model": "gpt-6-astra", "effort": "medium"},
        ],
        "tasks": [
            {"id": "normalize-notes", "tag": "v1.2.0", "commit": "a" * 40, "remote_commit": "a" * 40},
            {"id": "render-report", "tag": "v1.3.0", "commit": "b" * 40, "remote_commit": "b" * 40},
        ],
        "goal_audit": {"status": "achieved", "goal_verified": True},
        "git": {"clean": True, "remote_main": "c" * 40, "local_main": "c" * 40},
        "usage": {"status": "observed", "records": [{"source": "intake", "raw": {"input_tokens": 10}}]},
        "privacy": {"synthetic_only": True, "private_repositories_touched": []},
        "limitations": ["Effective provider model identity is not independently attested."],
    }
    write_json(proof / "manifest.json", manifest)
    return proof


def test_pilot_freezes_disposable_scope_models_budget_and_local_only_effects():
    pilot = json.loads(PILOT_PATH.read_text())
    assert pilot["schema"] == "go-workflow.autonomy-live-pilot.v1"
    assert pilot["repository"]["kind"] == "new_disposable"
    assert pilot["authority"]["source_ref"].startswith("conversation:")
    assert pilot["authority"]["allowed_work"] == ["notes.py", "README.md"]
    assert pilot["models"] == [
        {"task_id": "normalize-notes", "id": "gpt-5.6-terra", "effort": "high"},
        {"task_id": "render-report", "id": "gpt-6-astra", "effort": "medium"},
    ]
    assert pilot["budget"]["wall_seconds"] <= 3600
    assert pilot["publication"] == {"provider": "git-tag", "remote": "new_local_bare", "allow_push": True}
    assert pilot["deployment"]["mode"] == "none"
    assert pilot["privacy"]["private_repositories"] == "forbidden"


def test_prepare_creates_a_new_buggy_project_with_real_release_contracts(tmp_path):
    campaign = load(CAMPAIGN_PATH, "autonomy_live_campaign")
    prepared = campaign.prepare(tmp_path / "work", runtime=ROOT, pilot_path=PILOT_PATH)

    assert prepared["repo"].is_dir()
    assert prepared["remote"].is_dir()
    assert campaign.git(prepared["repo"], "status", "--porcelain") == ""
    assert campaign.git(prepared["repo"], "ls-remote", "origin", "refs/tags/v1.1.0")
    assert "return list(lines)" in (prepared["repo"] / "notes.py").read_text()
    tests = (prepared["repo"] / "tests/test_notes.py").read_text()
    assert "import notes" in tests
    assert "from notes import normalize_notes, render_report" not in tests
    assert "test_render_report_uses_normalized_notes" not in tests
    for task_id in ("normalize-notes", "render-report"):
        task = json.loads((prepared["repo"] / f".go/tasks/open/{task_id}.json").read_text())
        assert task["execution_contract"]["release"] == {"mode": "required", "profile": "local"}
        assert task["execution_contract"]["model"] in [
            {"id": "gpt-5.6-terra", "effort": "high"},
            {"id": "gpt-6-astra", "effort": "medium"},
        ]

    (prepared["repo"] / "notes.py").write_text(
        "def normalize_notes(lines):\n"
        "    return list(dict.fromkeys(item.strip() for item in lines if item.strip()))\n"
    )
    focused = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_notes.py", "-q", "-k", "normalize"],
        cwd=prepared["repo"], text=True, capture_output=True,
    )
    assert focused.returncode == 0, focused.stdout + focused.stderr
    assert "1 passed, 2 deselected" in focused.stdout


def test_checker_accepts_content_bound_raw_process_and_remote_evidence(tmp_path):
    checker = load(CHECKER_PATH, "autonomy_live_checker")
    proof = valid_proof(tmp_path)

    result = checker.validate_proof(proof, pilot_path=PILOT_PATH)

    assert result["ok"] is True
    assert result["task_count"] == 2
    assert result["elapsed_seconds"] == 1200.0


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda value: value.update(mode="deterministic-fixture"), "real native Codex"),
        (lambda value: value.update(human_interventions=1), "human interventions"),
        (lambda value: value["timing"].update(elapsed_seconds=0), "measured elapsed"),
        (lambda value: value["interruption"].update(automatic=False), "automatic interruption"),
        (lambda value: value["tasks"][1].update(remote_commit="d" * 40), "remote readback"),
        (lambda value: value["privacy"].update(private_repositories_touched=["hearthhold"]), "private repository"),
    ],
)
def test_checker_rejects_fixture_hand_steering_unmeasured_or_false_remote_claims(tmp_path, mutation, expected):
    checker = load(CHECKER_PATH, "autonomy_live_checker_reject")
    proof = valid_proof(tmp_path)
    manifest_path = proof / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    mutation(manifest)
    write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match=expected):
        checker.validate_proof(proof, pilot_path=PILOT_PATH)


def test_checker_rejects_changed_raw_controller_output(tmp_path):
    checker = load(CHECKER_PATH, "autonomy_live_checker_hash")
    proof = valid_proof(tmp_path)
    (proof / "003-controller-resume.json").write_text("tampered\n")

    with pytest.raises(ValueError, match="raw command hash"):
        checker.validate_proof(proof, pilot_path=PILOT_PATH)
