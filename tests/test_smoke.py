import json
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def template_repo() -> Path:
    sibling = ROOT.parent / "go-project-template"
    if (sibling / ".go").is_dir():
        return sibling
    return ROOT / "fixtures" / "minimal"


def test_template_validates():
    result = subprocess.run([sys.executable, str(ROOT / "cli" / "go.py"), "validate", str(template_repo())], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr + result.stdout


def test_stack_uses_local_only_linux_verification():
    assert not (ROOT / ".github" / "workflows").exists()
    verifier = ROOT / "scripts" / "check-linux.sh"
    assert verifier.is_file()
    text = verifier.read_text(encoding="utf-8")
    assert "Python 3.11+ required" in text
    assert "pytest" in text
    assert "import sys, pytest" in text
    assert "template-check" in text

    documentation = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [ROOT / "README.md", ROOT / "docs" / "autonomy-benchmark.md"]
    )
    assert "Linux CI" not in documentation
    assert "GitHub Actions" not in documentation


def run_go(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "cli" / "go.py"), *args], cwd=cwd, text=True, capture_output=True)


def test_template_check_command_reports_pairing_contract():
    result = run_go("template-check", str(template_repo()), "--json")
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema"] == "go-workflow.template-check.v1"
    assert payload["ok"] is True
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["validate"]["ok"] is True
    assert checks["route_repo_local"]["ok"] is True
    assert checks["has_claimable_example_task"]["ok"] is True
    assert checks["first_auto_execute"]["ok"] is True
    if template_repo().name == "go-project-template":
        assert checks["check_script_bootstraps_stack"]["ok"] is True


def test_public_template_first_auto_executes_declared_task(tmp_path: Path):
    repo = tmp_path / "fresh-template"
    shutil.copytree(template_repo(), repo, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed template", "-q"], cwd=repo, check=True)
    env = os.environ.copy()
    env["GO_STACK"] = str(ROOT)
    env["GO_STACK_ALLOW_DEV"] = "1"

    executed = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "go.py"), "auto", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--json"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert executed.returncode == 0, executed.stderr + executed.stdout
    result = json.loads(executed.stdout)
    assert result["status"] == "done"
    assert result["completed_tasks"] == ["task-schema-smoke"]
    assert (repo / ".go" / "tasks" / "done" / "task-schema-smoke.json").is_file()


def test_public_template_project_launcher_discovers_stack(tmp_path: Path):
    repo = tmp_path / "launcher-template"
    shutil.copytree(template_repo(), repo, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
    launcher = repo / "go"
    env = os.environ.copy()
    env["GO_STACK"] = str(ROOT)
    env["GO_STACK_ALLOW_DEV"] = "1"

    launched = subprocess.run([str(launcher), "validate", "."], cwd=repo, text=True, capture_output=True, env=env)

    assert launched.returncode == 0, launched.stderr + launched.stdout
    assert f"ok: {repo / '.go'}" in launched.stdout


def test_template_bootstrap_keeps_explicit_stack_on_pinned_runtime(tmp_path: Path):
    source = tmp_path / "stack-source"
    remote = tmp_path / "stack.git"
    checkout = tmp_path / "go-workflow-stack"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    (source / "cli").mkdir()
    (source / "cli" / "go.py").write_text('STACK_VERSION = "0.3.0"\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "v1", "-q"], cwd=source, check=True)
    subprocess.run(["git", "tag", "v0.3.0"], cwd=source, check=True)
    subprocess.run(["git", "clone", "--bare", "-q", str(source), str(remote)], check=True)
    subprocess.run(["git", "clone", "--branch", "v0.3.0", "-q", str(remote), str(checkout)], check=True)
    pinned_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, text=True, capture_output=True, check=True).stdout.strip()
    (source / "cli" / "go.py").write_text('STACK_VERSION = "0.4.0"\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "v2", "-q"], cwd=source, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=source, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=source, check=True)
    project = tmp_path / "project"
    shutil.copytree(template_repo(), project, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
    env = os.environ.copy()
    env["GO_STACK"] = str(checkout)
    env["GO_STACK_REMOTE"] = str(remote)
    env["GO_STACK_REF"] = "v0.3.0"

    bootstrapped = subprocess.run(["bash", "scripts/bootstrap-stack.sh"], cwd=project, text=True, capture_output=True, env=env)

    assert bootstrapped.returncode == 0, bootstrapped.stderr + bootstrapped.stdout
    assert (checkout / "cli" / "go.py").read_text() == 'STACK_VERSION = "0.3.0"\n'
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, text=True, capture_output=True, check=True).stdout.strip()
    assert head == pinned_head

    subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=checkout, check=True)
    subprocess.run(["git", "checkout", "--detach", "-q", "origin/main"], cwd=checkout, check=True)

    mismatched = subprocess.run(["bash", "scripts/bootstrap-stack.sh"], cwd=project, text=True, capture_output=True, env=env)

    assert mismatched.returncode == 4
    assert "does not provide pinned runtime v0.3.0" in mismatched.stderr


def test_template_bootstrap_rejects_same_version_wrong_commit_without_dev_override(tmp_path: Path):
    source = tmp_path / "stack-source"
    remote = tmp_path / "stack.git"
    checkout = tmp_path / "go-workflow-stack"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    (source / "cli").mkdir()
    (source / "cli" / "go.py").write_text('STACK_VERSION = "0.3.0"\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "tag target", "-q"], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "tag", "-a", "v0.3.0", "-m", "v0.3.0"], cwd=source, check=True)
    (source / "after-tag.txt").write_text("same declared version, different commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "after tag", "-q"], cwd=source, check=True)
    subprocess.run(["git", "clone", "--bare", "-q", str(source), str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True)
    project = tmp_path / "project"
    shutil.copytree(template_repo(), project, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
    env = os.environ.copy()
    env.update({"GO_STACK": str(checkout), "GO_STACK_REMOTE": str(remote), "GO_STACK_REF": "v0.3.0"})

    rejected = subprocess.run(["bash", "scripts/bootstrap-stack.sh"], cwd=project, text=True, capture_output=True, env=env)
    assert rejected.returncode == 4
    assert "does not provide pinned runtime v0.3.0" in rejected.stderr

    env["GO_STACK_ALLOW_DEV"] = "1"
    allowed = subprocess.run(["bash", "scripts/bootstrap-stack.sh"], cwd=project, text=True, capture_output=True, env=env)
    assert allowed.returncode == 0, allowed.stderr + allowed.stdout
    assert "development override" in allowed.stderr


def sample_live_hermes_proof() -> dict:
    return {
        "schema": "go-workflow.live-hermes-proof.v1",
        "status": "proven",
        "created_at": "2026-07-15T12:00:00+00:00",
        "binary": "/home/viggo/.local/bin/hermes",
        "binary_version": "hermes 1.2.3",
        "repo": "/tmp/hermes-go-campaign",
        "completed_tasks": ["phase-one", "phase-two"],
        "protocol_results": [
            {"result_file": "first.json", "task_id": "phase-one", "attempt": 1, "phase": "build", "status": "success", "summary": "built one"},
            {"result_file": "first.json", "task_id": "phase-one", "attempt": 1, "phase": "critic", "status": "success", "summary": "reviewed one"},
            {"result_file": "resumed.json", "task_id": "phase-two", "attempt": 1, "phase": "build", "status": "success", "summary": "built two"},
            {"result_file": "resumed.json", "task_id": "phase-two", "attempt": 1, "phase": "critic", "status": "success", "summary": "reviewed two"},
        ],
        "result_sha256": {name: character * 64 for name, character in {
            "doctor.json": "a", "first.json": "b", "resumed.json": "c",
        }.items()},
    }


def write_live_hermes_raw_results(root: Path, proof: dict) -> None:
    doctor = {
        "schema": "go-workflow.doctor.v1",
        "ready": True,
        "agent": {"name": "hermes", "available": True, "path": proof["binary"]},
    }
    runs = {
        "first.json": {
            "schema": "go-workflow.auto-run-result.v1",
            "status": "budget_exhausted",
            "completed_tasks": ["phase-one"],
            "attempts": [{
                "task_id": "phase-one", "attempt": 1,
                "build": {"result": {"schema": "go-workflow.agent-adapter-result.v1", "phase": "build", "status": "success", "summary": "built one"}},
                "critic": {"result": {"schema": "go-workflow.agent-adapter-result.v1", "phase": "critic", "status": "success", "summary": "reviewed one"}},
                "repair": {"status": "not_needed"},
            }],
        },
        "resumed.json": {
            "schema": "go-workflow.auto-run-result.v1",
            "status": "done",
            "completed_tasks": ["phase-two"],
            "completion_audit": {"project_verification_passed": True},
            "attempts": [{
                "task_id": "phase-two", "attempt": 1,
                "build": {"result": {"schema": "go-workflow.agent-adapter-result.v1", "phase": "build", "status": "success", "summary": "built two"}},
                "critic": {"result": {"schema": "go-workflow.agent-adapter-result.v1", "phase": "critic", "status": "success", "summary": "reviewed two"}},
                "repair": {"status": "not_needed"},
            }],
        },
    }
    payloads = {"doctor.json": doctor, **runs}
    for name, payload in payloads.items():
        path = root / name
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        proof["result_sha256"][name] = hashlib.sha256(path.read_bytes()).hexdigest()


def test_live_hermes_acceptance_refuses_to_claim_proof_without_binary(tmp_path: Path):
    env = os.environ.copy()
    env["GO_RUN_REAL_HERMES_E2E"] = "1"
    env["GO_HERMES_E2E_ROOT"] = str(tmp_path)
    env["PATH"] = "/usr/bin:/bin"

    attempted = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "run-hermes-acceptance.sh")],
        text=True, capture_output=True, env=env,
    )

    assert attempted.returncode == 2
    assert "NOT PROVEN: hermes is not available on PATH" in attempted.stderr
    assert not (tmp_path / "proof.json").exists()
    script = (ROOT / "scripts" / "run-hermes-acceptance.sh").read_text(encoding="utf-8")
    assert "go-workflow.live-hermes-proof.v1" in script
    assert "go-workflow.agent-adapter-result.v1" in script
    assert "hermes --version" in script


def test_live_hermes_proof_cli_validates_and_explicitly_copies_valid_evidence(tmp_path: Path):
    proof = tmp_path / "proof.json"
    copied = tmp_path / "reviewed" / "live-hermes-proof.json"
    data = sample_live_hermes_proof()
    write_live_hermes_raw_results(tmp_path, data)
    proof.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    validated = run_go(
        "proof", "validate", str(proof), "--evidence-root", str(tmp_path),
        "--copy-to", str(copied), "--json",
    )

    assert validated.returncode == 0, validated.stderr + validated.stdout
    result = json.loads(validated.stdout)
    assert result["valid"] is True
    assert result["copied_to"] == str(copied.resolve())
    assert json.loads(copied.read_text(encoding="utf-8"))["status"] == "proven"


def test_live_hermes_proof_rejects_naive_timestamp_without_copying(tmp_path: Path):
    proof = tmp_path / "naive-proof.json"
    copied = tmp_path / "must-not-exist.json"
    data = sample_live_hermes_proof()
    data["created_at"] = "2026-07-15T12:00:00"
    proof.write_text(json.dumps(data), encoding="utf-8")

    rejected = run_go("proof", "validate", str(proof), "--copy-to", str(copied), "--json")

    assert rejected.returncode == 1
    result = json.loads(rejected.stdout)
    assert result["valid"] is False
    assert "timezone" in " ".join(result["errors"])
    assert not copied.exists()


def test_live_hermes_proof_recomputes_result_hashes_before_copy(tmp_path: Path):
    proof = tmp_path / "proof.json"
    copied = tmp_path / "must-not-exist.json"
    proof.write_text(json.dumps(sample_live_hermes_proof()), encoding="utf-8")
    for name in ("doctor.json", "first.json", "resumed.json"):
        (tmp_path / name).write_text(f"actual {name}\n", encoding="utf-8")

    rejected = run_go(
        "proof", "validate", str(proof),
        "--evidence-root", str(tmp_path), "--copy-to", str(copied), "--json",
    )

    assert rejected.returncode == 1
    result = json.loads(rejected.stdout)
    assert "hash mismatch" in " ".join(result["errors"])
    assert not copied.exists()


def test_live_hermes_proof_malformed_nested_values_fail_closed(tmp_path: Path):
    proof = tmp_path / "malformed-proof.json"
    data = sample_live_hermes_proof()
    data["completed_tasks"] = [{"unexpected": "object"}]
    proof.write_text(json.dumps(data), encoding="utf-8")

    rejected = run_go("proof", "validate", str(proof), "--json")

    assert rejected.returncode == 1, rejected.stderr + rejected.stdout
    result = json.loads(rejected.stdout)
    assert result["valid"] is False
    assert "completed_tasks" in " ".join(result["errors"])


def test_live_hermes_proof_rejects_correctly_hashed_but_semantically_empty_results(tmp_path: Path):
    proof = tmp_path / "proof.json"
    data = sample_live_hermes_proof()
    for name in ("doctor.json", "first.json", "resumed.json"):
        path = tmp_path / name
        path.write_text("{}\n", encoding="utf-8")
        data["result_sha256"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    proof.write_text(json.dumps(data), encoding="utf-8")

    rejected = run_go("proof", "validate", str(proof), "--evidence-root", str(tmp_path), "--json")

    assert rejected.returncode == 1
    errors = " ".join(json.loads(rejected.stdout)["errors"])
    assert "doctor.json" in errors
    assert "first.json" in errors
    assert "resumed.json" in errors


def test_live_hermes_proof_copy_requires_raw_evidence_root(tmp_path: Path):
    proof = tmp_path / "proof.json"
    copied = tmp_path / "must-not-exist.json"
    proof.write_text(json.dumps(sample_live_hermes_proof()), encoding="utf-8")

    rejected = run_go("proof", "validate", str(proof), "--copy-to", str(copied), "--json")

    assert rejected.returncode == 1
    errors = " ".join(json.loads(rejected.stdout)["errors"])
    assert "--evidence-root" in errors
    assert not copied.exists()


def test_spike_customizes_a_repository_created_from_public_template(tmp_path: Path):
    repo = tmp_path / "customer-portal"
    shutil.copytree(template_repo(), repo, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    spiked = run_go(
        "spike", str(repo), "--brief", "A customer portal for support requests",
        "--task", "first-slice|Build the first support-request slice",
        "--verification", "git diff --check",
    )

    assert spiked.returncode == 0, spiked.stderr + spiked.stdout
    project = json.loads((repo / ".go" / "project.json").read_text())
    vision = json.loads((repo / ".go" / "vision.json").read_text())
    task = json.loads((repo / ".go" / "tasks" / "open" / "first-slice.json").read_text())
    assert project["id"] == "customer-portal"
    assert project["name"] == "Customer Portal"
    assert vision["project"] == "customer-portal"
    assert vision["wedge"] == "A customer portal for support requests"
    assert task["project"] == "customer-portal"
    assert not list((repo / ".go" / "tasks" / "done").glob("*.json"))


def test_apply_template_creates_project_specific_contract(tmp_path: Path):
    repo = tmp_path / "applied-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = os.environ.copy()
    env["GO_PROJECT_TEMPLATE"] = str(template_repo())
    env["GO_PROJECT_BRIEF"] = "An applied project with its own durable direction"

    applied = subprocess.run(
        ["bash", str(ROOT / "scripts" / "apply-template.sh"), str(repo)],
        text=True,
        capture_output=True,
        env=env,
    )

    assert applied.returncode == 0, applied.stderr + applied.stdout
    project = json.loads((repo / ".go" / "project.json").read_text())
    vision = json.loads((repo / ".go" / "vision.json").read_text())
    assert project["id"] == "applied-project"
    assert vision["project"] == "applied-project"
    assert vision["wedge"] == env["GO_PROJECT_BRIEF"]
    assert list((repo / ".go" / "tasks" / "open").glob("*.json"))


def test_epic_and_decision_authoring_primitives(tmp_path: Path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "adr-epic", "--name", "ADR Epic", "--verification", "git diff --check")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout

    epic = run_go("epic", "create", str(repo), "--id", "workflow-contract", "--title", "Workflow Contract", "--description", "ADR/epic-lite contract")
    assert epic.returncode == 0, epic.stderr + epic.stdout
    assert "workflow-contract" in epic.stdout

    task = run_go("task", "create", str(repo), "--id", "prove-contract", "--summary", "Prove ADR and epic contract", "--epic", "workflow-contract", "--acceptance", "Task is linked to epic", "--verification", "git diff --check")
    assert task.returncode == 0, task.stderr + task.stdout
    hierarchy = json.loads((repo / ".go" / "hierarchy.json").read_text())
    epic_record = next(item for item in hierarchy["epics"] if item["id"] == "workflow-contract")
    assert "prove-contract" in epic_record["tasks"]

    decision = run_go("decision", "create", str(repo), "--id", "adr-001", "--title", "Use ADR-lite", "--context", "Agents need explicit decisions", "--decision", "Store ADR-lite as decision events", "--consequence", "No Markdown ritual required", "--agent", "pytest", "--task-id", "prove-contract")
    assert decision.returncode == 0, decision.stderr + decision.stdout
    assert "adr-001" in decision.stdout
    validate = run_go("validate", str(repo))
    assert validate.returncode == 0, validate.stderr + validate.stdout
    readback = run_go("readback", str(repo))
    assert "Epics: workflow; workflow-contract" in readback.stdout


def test_validate_rejects_cross_file_project_and_hierarchy_drift(tmp_path: Path):
    repo = tmp_path / "contract-drift"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "contract", "--name", "Contract")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go(
        "task", "create", str(repo), "--id", "linked-task", "--summary", "Linked task",
        "--epic", "workflow", "--acceptance", "Task remains linked to its project",
        "--verification", "git diff --check",
    )
    assert task.returncode == 0, task.stderr + task.stdout
    task_path = repo / ".go" / "tasks" / "open" / "linked-task.json"
    task_data = json.loads(task_path.read_text())
    task_data["project"] = "different-project"
    task_path.write_text(json.dumps(task_data, indent=2) + "\n")
    hierarchy_path = repo / ".go" / "hierarchy.json"
    hierarchy = json.loads(hierarchy_path.read_text())
    hierarchy["epics"][0]["tasks"] = []
    hierarchy_path.write_text(json.dumps(hierarchy, indent=2) + "\n")

    validated = run_go("validate", str(repo))

    assert validated.returncode == 1
    assert "task project" in validated.stderr
    assert "not linked from hierarchy" in validated.stderr


def test_spike_bootstraps_repo_local_contract_and_auto_plan(tmp_path: Path):
    repo = tmp_path / "marktplaats-bot"
    spike = run_go(
        "spike",
        str(repo),
        "--project-id",
        "marktplaats-bot",
        "--name",
        "Marktplaats Bot",
        "--brief",
        "Inbox monitor that checks Marktplaats messages and alerts Viggo.",
        "--epic",
        "inbox-monitor|Inbox Monitor",
        "--task",
        "design-monitor|Design the inbox monitor",
        "--task",
        "build-poller|Build the polling loop",
    )
    assert spike.returncode == 0, spike.stderr + spike.stdout
    assert (repo / ".git").is_dir()
    assert (repo / "README.md").is_file()
    assert (repo / ".go" / "vision.json").is_file()
    assert (repo / ".go" / "architecture-principles.json").is_file()
    assert (repo / ".go" / "tasks" / "open" / "design-monitor.json").is_file()
    assert (repo / ".go" / "tasks" / "open" / "build-poller.json").is_file()
    design_task = json.loads((repo / ".go" / "tasks" / "open" / "design-monitor.json").read_text())
    assert "cli/go.py" in design_task["scope"]["read"]
    assert "tests/**" in design_task["scope"]["modify"]

    validate = run_go("validate", str(repo))
    assert validate.returncode == 0, validate.stderr + validate.stdout
    auto = run_go("auto", str(repo), "--max-tasks", "2", "--json")
    assert auto.returncode == 0, auto.stderr + auto.stdout
    handoff = run_go("auto", str(repo), "--max-tasks", "1", "--emit-handoff", "--json")
    assert handoff.returncode == 0, handoff.stderr
    handoff_plan = json.loads(handoff.stdout)
    assert handoff_plan["schema"] == "go-workflow.agent-handoff.v1"
    assert handoff_plan["mode"] == "go-auto"
    assert handoff_plan["target_runtime"] == "codex-or-hermes-agent"
    assert handoff_plan["tasks"][0]["id"] == "design-monitor"
    assert "docs/**" in handoff_plan["tasks"][0]["scope"]["modify"]
    assert "make check" in handoff_plan["expected_evidence"]["verification_commands"]
    assert handoff_plan["run_envelope"]["result_schema"] == "go-workflow.auto-run-result.v1"

    loop_handoff = run_go("go-loop", str(repo), "--max-tasks", "1", "--emit-handoff", "--json")
    assert loop_handoff.returncode == 0, loop_handoff.stderr
    assert json.loads(loop_handoff.stdout)["mode"] == "go-loop"

    plan = json.loads(auto.stdout)
    assert plan["mode"] == "go-auto"
    assert plan["control_handoff"] is True
    assert plan["can_escalate_to"] == ["go-loop"]
    assert plan["loop"] == ["route", "status", "contract-repair-if-needed", "next-or-create-task", "claim", "execute", "verify", "recheck", "devil", "repair", "verify", "commit-or-ship", "finish", "self-reflect", "continue-or-block"]
    assert "does not hand commands back to Viggo" in plan["agent_contract"]["execute"]
    assert "vision/end goal" in plan["agent_contract"]["contract_preflight"]
    assert plan["next_tasks"] == ["design-monitor", "build-poller"]
    assert plan["execution_policy"]["ask_policy"] == "do-not-ask-when-safe-default-exists"
    assert plan["execution_policy"]["may_create_follow_up_tasks"] is True
    assert plan["execution_policy"]["may_continue_after_self_reflect"] is True
    assert "claim_and_execute_open_tasks" in plan["execution_policy"]["allowed_autonomous_actions"]
    assert "external_authority_required" in plan["execution_policy"]["human_gates"]
    assert plan["run_envelope"]["run_until"] == "done_or_blocker_or_budget_or_safety_gate"
    assert plan["run_envelope"]["budget"]["max_tasks"] == 2
    assert plan["run_envelope"]["budget"]["max_minutes"] == 45
    assert plan["run_envelope"]["budget"]["max_commands"] == 36
    assert plan["run_envelope"]["budget"]["checkpoint_every_tasks"] == 1
    assert plan["run_envelope"]["telegram_policy"]["default"] == "silent_until_done_blocker_or_checkpoint"
    assert plan["run_envelope"]["preflight"]["valid_go_state"] is True
    assert plan["run_envelope"]["preflight"]["human_gate_required"] is False
    assert plan["run_envelope"]["result_schema"] == "go-workflow.auto-run-result.v1"
    loop = run_go("loop", str(repo), "--max-tasks", "2", "--json")
    assert loop.returncode == 0, loop.stderr + loop.stdout
    loop_plan = json.loads(loop.stdout)
    assert loop_plan["mode"] == "go-loop"
    assert loop_plan["autonomy"] == "control-handed-off-until-blocker"
    assert loop_plan["continues_beyond_initial_tasks"] is True
    go_loop_alias = run_go("go-loop", str(repo), "--max-tasks", "2", "--json")
    assert go_loop_alias.returncode == 0, go_loop_alias.stderr + go_loop_alias.stdout
    alias_plan = json.loads(go_loop_alias.stdout)
    assert alias_plan["mode"] == "go-loop"
    assert alias_plan["next_tasks"] == ["design-monitor", "build-poller"]


def test_auto_execute_claims_verifies_finishes_and_reflects(tmp_path: Path):
    repo = tmp_path / "exec-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "exec", "--name", "Exec", "--verification", "python3 -c \"print('ok')\"")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go("task", "create", str(repo), "--id", "verify-only", "--summary", "Verify only", "--epic", "workflow", "--acceptance", "Verification command prints verified and exits zero", "--verification", "python3 -c \"print('verified')\"")
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed go state", "-q"], cwd=repo, check=True)

    executed = run_go("auto", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--json")
    assert executed.returncode == 0, executed.stderr + executed.stdout
    result = json.loads(executed.stdout)
    assert result["status"] == "done"
    assert result["completed_tasks"] == ["verify-only"]
    assert result["checks"][0]["returncode"] == 0
    done_task = json.loads((repo / ".go" / "tasks" / "done" / "verify-only.json").read_text())
    assert done_task["evidence"]
    evidence_log = (repo / ".go" / "evidence" / "events.jsonl").read_text()
    assert "task.finished" in evidence_log
    reflection_log = (repo / ".go" / "reflections" / "events.jsonl").read_text()
    assert "auto.reflected" in reflection_log
    assert result["commands_run"] == 2
    assert result["completion_audit"]["project_verification_passed"] is True
    assert result["checkpoints"]
    assert result["attempts"][0]["stages"] == ["build", "verify", "critic", "repair", "judge"]
    assert result["attempts"][0]["critic"]["status"] == "passed"


def test_auto_execute_failure_records_critic_attempt_before_blocking(tmp_path: Path):
    repo = tmp_path / "failing-exec-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "fail", "--name", "Fail")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go("task", "create", str(repo), "--id", "failing", "--summary", "Failing", "--epic", "workflow", "--acceptance", "The failing verification is recorded with critic evidence", "--verification", "python3 -c \"import sys; sys.exit(7)\"")
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed go state", "-q"], cwd=repo, check=True)

    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--json")
    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "blocked"
    assert result["blocked_task"] == "failing"
    attempt = result["attempts"][0]
    assert attempt["verify"]["status"] == "failed"
    assert attempt["critic"]["status"] == "blocking_findings"
    assert attempt["repair"]["status"] == "requires_agent_repair"
    assert attempt["judge"]["status"] == "blocked"
    blocked_task = json.loads((repo / ".go" / "tasks" / "blocked" / "failing.json").read_text())
    assert "critic blocked" in blocked_task["blocked"]["reason"]
    runs_log = (repo / ".go" / "runs" / "events.jsonl").read_text()
    assert "auto.attempt" in runs_log


def test_go_loop_repair_adapter_fixes_failing_task_without_user_intervention(tmp_path: Path):
    repo = tmp_path / "repair-exec-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "repair", "--name", "Repair")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    (repo / "answer.txt").write_text("broken", encoding="utf-8")
    verify = "python3 -c \"from pathlib import Path; import sys; sys.exit(0 if Path('answer.txt').read_text().strip() == 'fixed' else 9)\""
    task = run_go("task", "create", str(repo), "--id", "repair-me", "--summary", "Repair me", "--epic", "workflow", "--read", "answer.txt", "--modify", "answer.txt", "--acceptance", "answer.txt contains exactly fixed", "--verification", verify)
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go", "answer.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed broken repair state", "-q"], cwd=repo, check=True)
    repair_command = "python3 -c \"from pathlib import Path; Path('answer.txt').write_text('fixed', encoding='utf-8')\""

    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "3", "--execute", "--agent", "pytest", "--repair-command", repair_command, "--json")
    assert executed.returncode == 0, executed.stderr + executed.stdout
    result = json.loads(executed.stdout)
    assert result["status"] == "done"
    assert result["completed_tasks"] == ["repair-me"]
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["verify"]["status"] == "failed"
    assert result["attempts"][0]["repair"]["status"] == "passed"
    assert result["attempts"][1]["verify"]["status"] == "passed"
    assert result["attempts"][1]["judge"]["status"] == "passed"
    assert (repo / "answer.txt").read_text().strip() == "fixed"
    assert (repo / ".go" / "tasks" / "done" / "repair-me.json").is_file()
    attempt_dir = repo / ".go" / "runs" / "repair-me" / "attempt-01"
    for name in ["prompt.md", "verify.log", "critic.md", "diff.patch", "verdict.json"]:
        assert (attempt_dir / name).is_file(), name
    verdict = json.loads((attempt_dir / "verdict.json").read_text())
    assert verdict["schema"] == "go-workflow.attempt-verdict.v1"
    assert result["attempts"][0]["artifacts"]["verdict"].endswith("verdict.json")

def test_go_loop_repair_adapter_fixes_real_python_package_without_user_intervention(tmp_path: Path):
    repo = tmp_path / "real-repair-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "realrepair", "--name", "Real Repair")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    verify = "python3 -m pytest test_calc.py -q"
    task = run_go(
        "task", "create", str(repo),
        "--id", "fix-add",
        "--summary", "Fix add implementation",
        "--epic", "workflow",
        "--read", "calc.py",
        "--read", "test_calc.py",
        "--modify", "calc.py",
        "--acceptance", "add(2, 3) returns 5 and the pytest fixture passes",
        "--verification", verify,
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go", "calc.py", "test_calc.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed broken package", "-q"], cwd=repo, check=True)
    repair_command = "python3 -c \"from pathlib import Path; p=Path('calc.py'); p.write_text(p.read_text().replace('return a - b', 'return a + b'), encoding='utf-8')\""

    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "3", "--execute", "--agent", "pytest", "--repair-command", repair_command, "--json")
    assert executed.returncode == 0, executed.stderr + executed.stdout
    result = json.loads(executed.stdout)
    assert result["status"] == "done"
    assert result["completed_tasks"] == ["fix-add"]
    assert len(result["attempts"]) >= 2
    assert result["attempts"][0]["verify"]["status"] == "failed"
    assert result["attempts"][-1]["verify"]["status"] == "passed"
    assert "return a + b" in (repo / "calc.py").read_text()
    assert (repo / ".go" / "tasks" / "done" / "fix-add.json").is_file()


def test_executor_adapter_receives_vision_principles_hierarchy_and_task_context(tmp_path: Path):
    repo = tmp_path / "context-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go(
        "adopt", str(repo), "--project-id", "context", "--name", "Context",
        "--north-star", "Ship context-aware work", "--principle", "small-seams|Keep seams small.|Agents need narrow changes.|Review task scope",
    )
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    verify = "python3 -c \"from pathlib import Path; import sys; sys.exit(0 if Path('context.json').is_file() else 1)\""
    task = run_go(
        "task", "create", str(repo), "--id", "capture-context", "--summary", "Capture context",
        "--epic", "workflow", "--modify", "context.json", "--acceptance", "Adapter receives the durable project contract",
        "--verification", verify,
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed context", "-q"], cwd=repo, check=True)
    repair = "python3 -c \"import os; from pathlib import Path; Path('context.json').write_text(os.environ['GO_CONTEXT_JSON'], encoding='utf-8')\""

    executed = run_go(
        "go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "2", "--execute",
        "--agent", "pytest", "--repair-command", repair, "--json",
    )

    assert executed.returncode == 0, executed.stderr + executed.stdout
    context = json.loads((repo / "context.json").read_text())
    assert context["vision"]["north_star"] == "Ship context-aware work"
    assert context["architecture_principles"]["principles"][0]["id"] == "small-seams"
    assert context["task"]["id"] == "capture-context"
    assert context["hierarchy"]["epics"][0]["id"] == "workflow"
    prompt_artifact = repo / ".go" / "runs" / "capture-context" / "attempt-02" / "prompt.md"
    assert "Ship context-aware work" in prompt_artifact.read_text()
    assert "small-seams" in prompt_artifact.read_text()


def test_go_loop_writes_resume_state_and_can_local_commit_ship(tmp_path: Path):
    repo = tmp_path / "ship-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "ship", "--name", "Ship")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go("task", "create", str(repo), "--id", "ship-me", "--summary", "Ship me", "--epic", "workflow", "--acceptance", "Verification passes", "--verification", "python3 -c 'print(7)'")
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed ship state", "-q"], cwd=repo, check=True)

    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--ship-policy", "local-commit", "--json")
    assert executed.returncode == 0, executed.stderr + executed.stdout
    result = json.loads(executed.stdout)
    assert result["ship"][0]["status"] in {"committed", "clean"}
    latest = json.loads((repo / ".go" / "runs" / "latest.json").read_text())
    assert latest["schema"] == "go-workflow.latest-run.v1"
    assert latest["status"] == "done"
    assert "resume_command" in latest
    status = subprocess.run(["git", "status", "--short"], cwd=repo, text=True, capture_output=True)
    assert status.stdout.strip() == ""


def test_blocked_ship_keeps_verified_task_active(tmp_path: Path):
    repo = tmp_path / "blocked-ship-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "blockedship", "--name", "Blocked Ship")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go(
        "task", "create", str(repo), "--id", "ship-blocked", "--summary", "Ship blocked",
        "--epic", "workflow", "--acceptance", "Verification passes",
        "--verification", "python3 -c 'print(7)'",
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed blocked ship", "-q"], cwd=repo, check=True)

    executed = run_go(
        "go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest",
        "--ship-policy", "push", "--json",
    )

    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "blocked"
    assert result["completed_tasks"] == []
    assert (repo / ".go" / "tasks" / "active" / "ship-blocked.json").is_file()
    assert not (repo / ".go" / "tasks" / "done" / "ship-blocked.json").exists()
    assert not any("task.finished" in line for line in (repo / ".go" / "evidence" / "events.jsonl").read_text().splitlines())


def test_failed_local_commit_restores_verified_task_to_active(tmp_path: Path):
    repo = tmp_path / "failed-commit-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "failedcommit", "--name", "Failed Commit")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go(
        "task", "create", str(repo), "--id", "commit-fails", "--summary", "Commit fails",
        "--epic", "workflow", "--acceptance", "Verification passes",
        "--verification", "python3 -c 'print(7)'",
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed failed commit", "-q"], cwd=repo, check=True)
    hooks = repo / ".hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.hooksPath", str(hooks)], cwd=repo, check=True)

    executed = run_go(
        "go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest",
        "--ship-policy", "local-commit", "--json",
    )

    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "blocked"
    assert (repo / ".go" / "tasks" / "active" / "commit-fails.json").is_file()
    assert not (repo / ".go" / "tasks" / "done" / "commit-fails.json").exists()
    assert not any("task.finished" in line for line in (repo / ".go" / "evidence" / "events.jsonl").read_text().splitlines())


def test_agent_check_reports_real_adapter_availability():
    result = run_go("agent-check", "--json")
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    agents = {item["agent"]: item for item in payload["agents"]}
    assert set(agents) == {"codex", "hermes"}
    assert all(isinstance(item["available"], bool) for item in agents.values())
    cli_text = (ROOT / "cli" / "go.py").read_text() + (ROOT / "go_workflow" / "cli.py").read_text()
    assert "dangerously-bypass" not in cli_text


def test_agent_mode_task_selects_safe_default_codex_executor(tmp_path: Path):
    repo = tmp_path / "default-agent-project"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$GO_REPO/codex-args.txt\"\nif [ \"$GO_HOOK\" = 'critic' ]; then\n  count_file=\"$GO_REPO/.go/runs/fake-critic-count\"\n  count=0\n  if [ -f \"$count_file\" ]; then count=$(cat \"$count_file\"); fi\n  if [ \"$count\" -eq 0 ]; then\n    printf '{\"schema\":\"go-workflow.agent-adapter-result.v1\",\"phase\":\"critic\",\"status\":\"blocked\",\"summary\":\"Exercise the repair loop once\"}\\n'\n  else\n    printf '{\"schema\":\"go-workflow.agent-adapter-result.v1\",\"phase\":\"critic\",\"status\":\"success\",\"summary\":\"No blocking findings\"}\\n'\n  fi\n  echo $((count + 1)) > \"$count_file\"\nelse\n  printf 'built\\n' > \"$GO_REPO/built.txt\"\n  printf '{\"schema\":\"go-workflow.agent-adapter-result.v1\",\"phase\":\"%s\",\"status\":\"success\",\"summary\":\"native phase complete\"}\\n' \"$GO_HOOK\"\nfi\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "defaultagent", "--name", "Default Agent")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go(
        "task", "create", str(repo), "--id", "agent-build", "--summary", "Agent build", "--epic", "workflow",
        "--execution-mode", "agent", "--modify", "built.txt", "--modify", "codex-args.txt",
        "--acceptance", "The selected coding agent creates built.txt", "--verification", "test -f built.txt",
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed default agent", "-q"], cwd=repo, check=True)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

    executed = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "go.py"), "go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--json"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert executed.returncode == 0, executed.stderr + executed.stdout
    result = json.loads(executed.stdout)
    assert result["status"] == "done"
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["build"]["status"] == "passed"
    assert result["attempts"][0]["critic"]["status"] == "blocking_findings"
    assert result["attempts"][1]["critic"]["status"] == "passed"
    assert '"status":"success"' in result["attempts"][1]["critic"]["result"]["verdict_text"]
    args = (repo / "codex-args.txt").read_text()
    assert "--sandbox\nworkspace-write" in args
    assert "--sandbox\nread-only" in args
    assert "--ephemeral" in args
    assert "dangerously-bypass" not in args


def test_restartable_agent_campaign_builds_repairs_commits_and_completes(tmp_path: Path):
    repo = tmp_path / "release-notes-campaign"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_hermes = bin_dir / "hermes"
    fake_hermes.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

repo = Path(os.environ["GO_REPO"])
task_id = os.environ["GO_TASK_ID"]
run_root = repo / ".go" / "runs"
with (run_root / "adapter-invocations.log").open("a", encoding="utf-8") as handle:
    handle.write(f"{task_id} {os.environ['GO_HOOK']} {os.environ['GO_ATTEMPT']}\\n")

if os.environ["GO_HOOK"] == "critic":
    counter = run_root / f"{task_id}.critic-count"
    count = int(counter.read_text()) if counter.exists() else 0
    status = "blocked" if task_id == "parse-headings" and count == 0 else "success"
    summary = "Add one repair pass." if status == "blocked" else "Evidence is sufficient."
    print(json.dumps({"schema": "go-workflow.agent-adapter-result.v1", "phase": "critic", "status": status, "summary": summary}))
    counter.write_text(str(count + 1), encoding="utf-8")
elif task_id == "parse-headings":
    (repo / "release_notes.py").write_text(
        "def headings(text):\\n    return [line[2:].strip() for line in text.splitlines() if line.startswith('# ')]\\n",
        encoding="utf-8",
    )
    (repo / "test_release_notes.py").write_text(
        "import unittest\\nfrom release_notes import headings\\n\\nclass TestReleaseNotes(unittest.TestCase):\\n    def test_headings(self):\\n        self.assertEqual(headings(chr(10).join(['# One', 'body', '# Two'])), ['One', 'Two'])\\n",
        encoding="utf-8",
    )
elif task_id == "render-json":
    (repo / "release_notes.py").write_text(
        "import json\\n\\ndef headings(text):\\n    return [line[2:].strip() for line in text.splitlines() if line.startswith('# ')]\\n\\ndef render_json(text):\\n    return json.dumps({'headings': headings(text)}, sort_keys=True)\\n",
        encoding="utf-8",
    )
    (repo / "test_release_notes.py").write_text(
        "import unittest\\nfrom release_notes import headings, render_json\\n\\nclass TestReleaseNotes(unittest.TestCase):\\n    def test_headings(self):\\n        self.assertEqual(headings(chr(10).join(['# One', 'body', '# Two'])), ['One', 'Two'])\\n    def test_json(self):\\n        self.assertEqual(render_json('# Two'), '{\\\"headings\\\": [\\\"Two\\\"]}')\\n",
        encoding="utf-8",
    )
if os.environ["GO_HOOK"] != "critic":
    print(json.dumps({"schema": "go-workflow.agent-adapter-result.v1", "phase": os.environ["GO_HOOK"], "status": "success", "summary": "native phase complete"}))
""",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go(
        "adopt", str(repo), "--project-id", "release-notes", "--name", "Release Notes",
        "--north-star", "Turn Markdown release notes into dependable structured output",
        "--verification", "python3 -m unittest -q",
    )
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    for task_id, summary, acceptance in [
        ("parse-headings", "Parse Markdown headings", "Heading parsing returns every level-one heading in order"),
        ("render-json", "Render headings as JSON", "JSON output contains the parsed headings under a stable headings key"),
    ]:
        created = run_go(
            "task", "create", str(repo), "--id", task_id, "--summary", summary, "--epic", "workflow",
            "--execution-mode", "agent", "--modify", "release_notes.py", "--modify", "test_release_notes.py",
            "--acceptance", acceptance, "--verification", "python3 -m unittest -q",
        )
        assert created.returncode == 0, created.stderr + created.stdout
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed campaign", "-q"], cwd=repo, check=True)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["GO_EXECUTOR_AGENT"] = "hermes"
    env["GO_STACK"] = str(ROOT)

    first = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "go.py"), "go-loop", str(repo), "--max-tasks", "1", "--max-commands", "20", "--execute", "--agent", "pytest", "--ship-policy", "local-commit", "--json"],
        text=True, capture_output=True, env=env,
    )

    assert first.returncode == 0, first.stderr + first.stdout
    first_result = json.loads(first.stdout)
    assert first_result["status"] == "budget_exhausted"
    assert first_result["completed_tasks"] == ["parse-headings"]
    assert first_result["attempts"][0]["critic"]["status"] == "blocking_findings"
    assert first_result["attempts"][1]["critic"]["status"] == "passed"
    latest = json.loads((repo / ".go" / "runs" / "latest.json").read_text())
    assert latest["status"] == "budget_exhausted"
    assert latest["effective_flags"]["ship_policy"] == "local-commit"
    assert (repo / ".go" / "tasks" / "done" / "parse-headings.json").is_file()
    assert (repo / ".go" / "tasks" / "open" / "render-json.json").is_file()

    resumed = subprocess.run(shlex.split(latest["resume_command"]), cwd=repo, text=True, capture_output=True, env=env)

    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    resumed_result = json.loads(resumed.stdout)
    assert resumed_result["status"] == "done"
    assert resumed_result["completed_tasks"] == ["render-json"]
    assert resumed_result["completion_audit"]["project_verification_passed"] is True
    assert (repo / ".go" / "runs" / "parse-headings" / "attempt-01" / "deep-critic.txt").is_file()
    assert (repo / ".go" / "runs" / "parse-headings" / "attempt-02" / "verdict.json").is_file()
    assert (repo / ".go" / "runs" / "render-json" / "attempt-01" / "prompt.md").is_file()
    subjects = subprocess.run(["git", "log", "--format=%s"], cwd=repo, text=True, capture_output=True, check=True).stdout
    assert "go-loop: finish parse-headings" in subjects
    assert "go-loop: finish render-json" in subjects
    assert subprocess.run(["git", "status", "--short"], cwd=repo, text=True, capture_output=True, check=True).stdout.strip() == ""


def test_go_loop_blocks_repair_agent_when_binary_missing(tmp_path: Path):
    repo = tmp_path / "missing-agent-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "missingagent", "--name", "Missing Agent")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go("task", "create", str(repo), "--id", "needs-agent", "--summary", "Needs agent", "--epic", "workflow", "--acceptance", "Verification passes", "--verification", "python3 -c 'import sys; sys.exit(1)'")
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed missing agent", "-q"], cwd=repo, check=True)

    if json.loads(run_go("agent-check", "--agent", "codex", "--json").stdout)["agents"][0]["available"]:
        return
    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--repair-agent", "codex", "--json")
    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "blocked"
    assert "not available" in result["summary"]


def test_adapter_scope_violation_blocks_task(tmp_path: Path):
    repo = tmp_path / "scope-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "scope", "--name", "Scope")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    (repo / "allowed.txt").write_text("bad", encoding="utf-8")
    (repo / "forbidden.txt").write_text("safe", encoding="utf-8")
    verify = "python3 -c \"from pathlib import Path; import sys; sys.exit(0 if Path('allowed.txt').read_text() == 'good' else 1)\""
    task = run_go("task", "create", str(repo), "--id", "scope-task", "--summary", "Scope task", "--epic", "workflow", "--read", "allowed.txt", "--modify", "allowed.txt", "--acceptance", "Only allowed file changes", "--verification", verify)
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go", "allowed.txt", "forbidden.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed scope", "-q"], cwd=repo, check=True)
    repair = "python3 -c \"from pathlib import Path; Path('allowed.txt').write_text('good', encoding='utf-8'); Path('forbidden.txt').write_text('changed', encoding='utf-8')\""
    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "2", "--execute", "--agent", "pytest", "--repair-command", repair, "--json")
    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "blocked"
    assert "scope violations" in json.dumps(result["attempts"])
    assert (repo / ".go" / "tasks" / "blocked" / "scope-task.json").is_file()


def test_custom_adapter_receives_and_returns_versioned_json_protocol(tmp_path: Path):
    repo = tmp_path / "protocol-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopted = run_go("adopt", str(repo), "--project-id", "protocol", "--name", "Protocol")
    assert adopted.returncode == 0, adopted.stderr + adopted.stdout
    (repo / "result.txt").write_text("bad", encoding="utf-8")
    verify = "python3 -c \"from pathlib import Path; import sys; sys.exit(0 if Path('result.txt').read_text() == 'good' else 1)\""
    task = run_go(
        "task", "create", str(repo), "--id", "protocol-task", "--summary", "Protocol task",
        "--epic", "workflow", "--modify", "result.txt", "--acceptance", "Result becomes good",
        "--verification", verify,
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed", "-q"], cwd=repo, check=True)
    repair = (
        "python3 -c \"import json,os; from pathlib import Path; "
        "r=json.loads(os.environ['GO_ADAPTER_REQUEST_JSON']); "
        "assert r['schema']=='go-workflow.agent-adapter-request.v1' and r['phase']=='repair'; "
        "Path('result.txt').write_text('good', encoding='utf-8'); "
        "print(json.dumps({'schema':'go-workflow.agent-adapter-result.v1','phase':'repair','status':'success','summary':'repaired'}))\""
    )

    executed = run_go(
        "go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "2", "--execute",
        "--agent", "pytest", "--repair-command", repair, "--json",
    )
    assert executed.returncode == 0, executed.stderr + executed.stdout
    result = json.loads(executed.stdout)
    adapter_result = result["attempts"][0]["repair"]["result"]
    assert adapter_result["schema"] == "go-workflow.agent-adapter-result.v1"
    assert adapter_result["phase"] == "repair"
    assert adapter_result["status"] == "success"


def test_adapter_result_validator_fails_closed_on_invalid_protocol(tmp_path: Path):
    result_path = tmp_path / "adapter-result.json"
    result_path.write_text(json.dumps({
        "schema": "go-workflow.agent-adapter-result.v1",
        "phase": "repair",
        "status": "success",
        "summary": "repaired",
    }), encoding="utf-8")
    valid = run_go("adapter", "validate-result", str(result_path), "--phase", "repair", "--json")
    assert valid.returncode == 0, valid.stderr + valid.stdout
    assert json.loads(valid.stdout)["valid"] is True

    result_path.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    invalid = run_go("adapter", "validate-result", str(result_path), "--phase", "repair", "--json")
    assert invalid.returncode == 1
    assert json.loads(invalid.stdout)["valid"] is False


def test_protocol_looking_adapter_output_never_falls_back_to_legacy_success():
    from go_workflow.adapter_protocol import normalize_adapter_result

    result = normalize_adapter_result("repair", "fake-agent", {
        "returncode": 0,
        "stdout": json.dumps({
            "schema": "go-workflow.agent-adapter-result.v2",
            "phase": "repair",
            "status": "success",
            "summary": "wrong protocol version",
        }),
        "stderr": "",
        "timed_out": False,
    })

    assert result["status"] == "failure"
    assert result["returncode"] == 65
    assert "invalid adapter result" in result["summary"]

    missing = normalize_adapter_result("build", "native-agent", {
        "returncode": 0,
        "stdout": "ordinary prose only",
        "stderr": "",
        "timed_out": False,
    }, require_protocol=True)
    assert missing["status"] == "failure"
    assert missing["returncode"] == 65
    assert "did not emit" in missing["summary"]


def test_native_codex_and_hermes_commands_require_v1_result_output():
    from go_workflow.adapters import native_agent_command

    for agent in ("codex", "hermes"):
        command = native_agent_command(agent, "repair")
        assert command.startswith(agent + " ")
        assert "go-workflow.agent-adapter-result.v1" in command
        assert '"phase":"repair"' in command
        assert "final non-empty line" in command
        if agent == "codex":
            assert "-C {repo_shell}" in command
            assert "-C {repo} " not in command


def test_native_codex_adapter_quotes_metacharacter_repository_path(tmp_path: Path):
    repo = tmp_path / "agent repo; touch PWNED; #"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "if [ \"$GO_HOOK\" = build ]; then printf 'built\\n' > \"$GO_REPO/built.txt\"; fi\n"
        "printf '{\"schema\":\"go-workflow.agent-adapter-result.v1\",\"phase\":\"%s\",\"status\":\"success\",\"summary\":\"safe native phase\"}\\n' \"$GO_HOOK\"\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopted = run_go("adopt", str(repo), "--project-id", "quoted", "--name", "Quoted")
    assert adopted.returncode == 0, adopted.stderr + adopted.stdout
    task = run_go(
        "task", "create", str(repo), "--id", "agent-build", "--summary", "Agent build",
        "--epic", "workflow", "--execution-mode", "agent", "--modify", "built.txt",
        "--acceptance", "built.txt exists", "--verification", "test -f built.txt",
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed", "-q"], cwd=repo, check=True)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

    executed = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "go.py"), "go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--json"],
        text=True, capture_output=True, env=env,
    )

    assert executed.returncode == 0, executed.stderr + executed.stdout
    assert json.loads(executed.stdout)["status"] == "done"
    assert not (repo / "PWNED").exists()


def test_routing_and_task_state_domains_are_importable(tmp_path: Path):
    from go_workflow.routing import normalize_router_command, recommend_route
    from go_workflow.task_state import open_task_records, task_path, unfinished_task_ids

    assert normalize_router_command("GOO") == "go"
    recommendation = recommend_route("go-loop", "werk tot groen", {
        "repo_exists": True,
        "has_go": True,
        "has_vision": True,
        "has_principles": True,
        "has_hierarchy": True,
        "valid": True,
        "open_task_count": 1,
    })
    assert recommendation["command"] == "go-loop"

    root = tmp_path / ".go"
    open_path = task_path(root, "open", "later")
    open_path.parent.mkdir(parents=True)
    open_path.write_text(json.dumps({"id": "later", "order": 2}), encoding="utf-8")
    active_path = task_path(root, "active", "now")
    active_path.parent.mkdir(parents=True)
    active_path.write_text(json.dumps({"id": "now"}), encoding="utf-8")
    assert [task["id"] for _, task in open_task_records(root)] == ["later"]
    assert unfinished_task_ids(root) == {"active": ["now"], "blocked": []}


def test_modular_core_and_adapter_protocol_are_published_as_repo_contracts():
    imported = subprocess.run(
        [sys.executable, "-c", "from go_workflow import *; from go_workflow.adapter_protocol import *; print(STACK_VERSION, STACK_REF, CURRENT_CONTRACT_VERSION, ADAPTER_REQUEST_SCHEMA, ADAPTER_RESULT_SCHEMA)"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout.strip() == "0.3.1 v0.3.1 2 go-workflow.agent-adapter-request.v1 go-workflow.agent-adapter-result.v1"
    for path in [
        ROOT / "schemas" / "agent-adapter-request.schema.json",
        ROOT / "schemas" / "agent-adapter-result.schema.json",
        ROOT / "schemas" / "migration-plan.schema.json",
        ROOT / "schemas" / "stack-update-plan.schema.json",
        ROOT / "schemas" / "live-hermes-proof.schema.json",
        ROOT / "docs" / "agent-adapter-protocol.md",
        ROOT / "docs" / "contract-migrations.md",
        ROOT / "docs" / "stack-updates.md",
        ROOT / "docs" / "state-safety.md",
    ]:
        assert path.is_file(), path


def test_adapter_cannot_modify_read_only_path(tmp_path: Path):
    repo = tmp_path / "read-only-scope-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "readonly", "--name", "Read Only")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    (repo / "owned.txt").write_text("bad", encoding="utf-8")
    (repo / "context.txt").write_text("keep", encoding="utf-8")
    verify = "python3 -c \"from pathlib import Path; import sys; sys.exit(0 if Path('owned.txt').read_text() == 'good' else 1)\""
    task = run_go(
        "task", "create", str(repo), "--id", "read-only-scope", "--summary", "Read-only scope",
        "--epic", "workflow", "--read", "context.txt", "--modify", "owned.txt",
        "--acceptance", "Only the owned file changes", "--verification", verify,
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go", "owned.txt", "context.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed read-only scope", "-q"], cwd=repo, check=True)
    repair = "python3 -c \"from pathlib import Path; Path('owned.txt').write_text('good', encoding='utf-8'); Path('context.txt').write_text('changed', encoding='utf-8')\""

    executed = run_go(
        "go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "2", "--execute",
        "--agent", "pytest", "--repair-command", repair, "--json",
    )

    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "blocked"
    assert "context.txt" in json.dumps(result["attempts"])


def test_adapter_scope_globs_allow_intended_descendants(tmp_path: Path):
    repo = tmp_path / "glob-scope-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "globscope", "--name", "Glob Scope")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    (repo / "src").mkdir()
    (repo / "src" / "answer.txt").write_text("bad", encoding="utf-8")
    verify = "python3 -c \"from pathlib import Path; import sys; sys.exit(0 if Path('src/answer.txt').read_text() == 'good' else 1)\""
    task = run_go(
        "task", "create", str(repo), "--id", "glob-scope", "--summary", "Glob scope",
        "--epic", "workflow", "--modify", "src/**", "--acceptance", "Nested source changes",
        "--verification", verify,
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go", "src/answer.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed glob scope", "-q"], cwd=repo, check=True)
    repair = "python3 -c \"from pathlib import Path; Path('src/answer.txt').write_text('good', encoding='utf-8')\""

    executed = run_go(
        "go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "2", "--execute",
        "--agent", "pytest", "--repair-command", repair, "--json",
    )

    assert executed.returncode == 0, executed.stderr + executed.stdout
    assert json.loads(executed.stdout)["status"] == "done"


def test_adapter_cannot_hide_scope_violation_in_preexisting_dirty_path(tmp_path: Path):
    repo = tmp_path / "preexisting-dirty-scope-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "dirtyscope", "--name", "Dirty Scope")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    (repo / "owned.txt").write_text("bad", encoding="utf-8")
    (repo / "unrelated.txt").write_text("clean", encoding="utf-8")
    verify = "python3 -c \"from pathlib import Path; import sys; sys.exit(0 if Path('owned.txt').read_text() == 'good' else 1)\""
    task = run_go(
        "task", "create", str(repo), "--id", "dirty-scope", "--summary", "Dirty scope",
        "--epic", "workflow", "--modify", "owned.txt", "--acceptance", "Only owned changes",
        "--verification", verify,
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go", "owned.txt", "unrelated.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed dirty scope", "-q"], cwd=repo, check=True)
    (repo / "unrelated.txt").write_text("user work", encoding="utf-8")
    repair = "python3 -c \"from pathlib import Path; Path('owned.txt').write_text('good', encoding='utf-8'); Path('unrelated.txt').write_text('adapter overwrite', encoding='utf-8')\""

    executed = run_go(
        "go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "2", "--execute",
        "--agent", "pytest", "--repair-command", repair, "--json",
    )

    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "blocked"
    assert "unrelated.txt" in json.dumps(result["attempts"])


def test_hard_command_budget_stops_before_second_command(tmp_path: Path):
    repo = tmp_path / "budget-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "budget", "--name", "Budget")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go("task", "create", str(repo), "--id", "budget-task", "--summary", "Budget task", "--epic", "workflow", "--acceptance", "Commands are budgeted", "--verification", "python3 -c 'print(1)'", "--verification", "python3 -c 'print(2)'")
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed budget", "-q"], cwd=repo, check=True)
    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--max-commands", "1", "--execute", "--agent", "pytest", "--json")
    assert executed.returncode == 0
    result = json.loads(executed.stdout)
    assert result["status"] == "budget_exhausted"
    assert result["commands_run"] == 1
    assert any(check.get("budget_exhausted") for check in result["checks"])


def test_verification_command_timeout_stops_hung_task(tmp_path: Path):
    repo = tmp_path / "timeout-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "timeout", "--name", "Timeout")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go(
        "task", "create", str(repo), "--id", "hangs", "--summary", "Hangs", "--epic", "workflow",
        "--acceptance", "Hung verification is terminated and reported", "--verification", "python3 -c 'import time; time.sleep(30)'",
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed timeout", "-q"], cwd=repo, check=True)

    executed = run_go(
        "go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "1", "--execute",
        "--agent", "pytest", "--command-timeout-seconds", "1", "--json",
    )

    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "blocked"
    assert result["checks"][0]["returncode"] == 124
    assert result["checks"][0]["timed_out"] is True


def test_ship_policy_stages_only_scope_and_go_runtime(tmp_path: Path):
    repo = tmp_path / "scoped-ship-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "scopedship", "--name", "Scoped Ship")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    (repo / "owned.txt").write_text("bad", encoding="utf-8")
    (repo / "unrelated.txt").write_text("old", encoding="utf-8")
    task = run_go("task", "create", str(repo), "--id", "ship-scope", "--summary", "Ship scope", "--epic", "workflow", "--read", "owned.txt", "--modify", "owned.txt", "--acceptance", "Owned file fixed", "--verification", "python3 -c \"from pathlib import Path; import sys; sys.exit(0 if Path('owned.txt').read_text() == 'good' else 1)\"")
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go", "owned.txt", "unrelated.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed scoped ship", "-q"], cwd=repo, check=True)
    (repo / "unrelated.txt").write_text("dirty", encoding="utf-8")
    repair = "python3 -c \"from pathlib import Path; Path('owned.txt').write_text('good', encoding='utf-8')\""
    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "2", "--execute", "--allow-dirty", "--agent", "pytest", "--repair-command", repair, "--ship-policy", "local-commit", "--json")
    assert executed.returncode == 0, executed.stderr + executed.stdout
    result = json.loads(executed.stdout)
    assert "unrelated.txt" in json.dumps(result["ship"])
    status = subprocess.run(["git", "status", "--short"], cwd=repo, text=True, capture_output=True)
    assert "unrelated.txt" in status.stdout
    log = subprocess.run(["git", "show", "HEAD~1", "--name-only", "--pretty=format:"], cwd=repo, text=True, capture_output=True)
    assert "owned.txt" in log.stdout
    assert "unrelated.txt" not in log.stdout


def test_latest_resume_command_preserves_effective_flags(tmp_path: Path):
    repo = tmp_path / "resume-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "resume", "--name", "Resume")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go("task", "create", str(repo), "--id", "resume-task", "--summary", "Resume task", "--epic", "workflow", "--acceptance", "Verification passes", "--verification", "python3 -c 'print(1)'")
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed resume", "-q"], cwd=repo, check=True)
    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "4", "--max-commands", "5", "--execute", "--agent", "pytest", "--semantic-critic", "--followup-on-block", "--ship-policy", "none", "--json")
    assert executed.returncode == 0, executed.stderr + executed.stdout
    latest = json.loads((repo / ".go" / "runs" / "latest.json").read_text())
    cmd = latest["resume_command"]
    resume_args = latest["resume_args"]
    assert cmd == "bash .go/runs/resume.sh"
    assert "--semantic-critic" in resume_args
    assert "--followup-on-block" in resume_args
    assert resume_args[resume_args.index("--ship-policy") + 1] == "none"
    assert latest["effective_flags"]["max_attempts"] == 4
    assert latest["effective_flags"]["max_commands"] == 5


def test_executor_agent_environment_default_is_persisted(tmp_path: Path):
    repo = tmp_path / "executor-env-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "executor-env", "--name", "Executor Env")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go(
        "task", "create", str(repo), "--id", "mechanical", "--summary", "Mechanical task", "--epic", "workflow",
        "--acceptance", "Verification succeeds without invoking an agent", "--verification", "python3 -c 'print(1)'",
    )
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed executor env", "-q"], cwd=repo, check=True)
    env = os.environ.copy()
    env["GO_EXECUTOR_AGENT"] = "hermes"

    executed = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "go.py"), "go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--json"],
        text=True, capture_output=True, env=env,
    )

    assert executed.returncode == 0, executed.stderr + executed.stdout
    latest = json.loads((repo / ".go" / "runs" / "latest.json").read_text())
    assert latest["effective_flags"]["executor_agent"] == "hermes"
    assert latest["resume_args"][latest["resume_args"].index("--executor-agent") + 1] == "hermes"

    explicit = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "go.py"), "go-loop", str(repo), "--execute", "--executor-agent", "codex", "--agent", "pytest", "--json"],
        text=True, capture_output=True, env=env,
    )
    assert explicit.returncode == 0, explicit.stderr + explicit.stdout
    latest = json.loads((repo / ".go" / "runs" / "latest.json").read_text())
    assert latest["effective_flags"]["executor_agent"] == "codex"


def test_resume_command_uses_relocated_stack_runtime(tmp_path: Path):
    repo = tmp_path / "portable-resume-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "portable-resume", "--name", "Portable Resume")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    for task_id in ("first", "second"):
        task = run_go(
            "task", "create", str(repo), "--id", task_id, "--summary", f"Task {task_id}", "--epic", "workflow",
            "--acceptance", f"{task_id} verification succeeds", "--verification", "python3 -c 'print(1)'",
        )
        assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed portable resume", "-q"], cwd=repo, check=True)
    first = run_go("go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--json")
    assert first.returncode == 0, first.stderr + first.stdout
    latest = json.loads((repo / ".go" / "runs" / "latest.json").read_text())
    moved_repo = tmp_path / "moved" / "portable-resume-project"
    shutil.copytree(repo, moved_repo)
    relocated_stack = tmp_path / "relocated-stack"
    (relocated_stack / "cli").mkdir(parents=True)
    shutil.copy2(ROOT / "cli" / "go.py", relocated_stack / "cli" / "go.py")
    shutil.copytree(ROOT / "go_workflow", relocated_stack / "go_workflow")
    env = os.environ.copy()
    env["GO_STACK"] = str(relocated_stack)

    resumed = subprocess.run(shlex.split(latest["resume_command"]), cwd=moved_repo, text=True, capture_output=True, env=env)

    assert str(ROOT) not in latest["resume_command"]
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    result = json.loads(resumed.stdout)
    assert result["status"] == "done"
    assert result["completed_tasks"] == ["second"]
    assert (moved_repo / ".go" / "runs" / "resume.sh").is_file()


def test_repair_agent_codex_option_is_available():
    help_result = run_go("go-loop", "--help")
    assert help_result.returncode == 0
    assert "--repair-agent" in help_result.stdout
    assert "codex" in help_result.stdout


def test_doctor_reports_wsl_hermes_readiness_and_version_contract(tmp_path: Path):
    repo = tmp_path / "doctor-project"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_hermes = bin_dir / "hermes"
    fake_hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_hermes.chmod(0o755)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_uv.chmod(0o755)
    fake_make = bin_dir / "make"
    fake_make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_make.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "doctor", "--name", "Doctor")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    project_path = repo / ".go" / "project.json"
    project = json.loads(project_path.read_text())
    project["required_stack_version"] = "0.2.0"
    project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

    rejected = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "go.py"), "doctor", str(repo), "--platform", "wsl", "--agent", "hermes", "--json"],
        text=True, capture_output=True, env=env,
    )
    assert rejected.returncode == 1, rejected.stderr + rejected.stdout
    rejected_result = json.loads(rejected.stdout)
    assert rejected_result["stack"]["exact_ref"] is False
    assert rejected_result["stack"]["development_override"] is False

    env["GO_STACK_ALLOW_DEV"] = "1"
    diagnosed = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "go.py"), "doctor", str(repo), "--platform", "wsl", "--agent", "hermes", "--json"],
        text=True, capture_output=True, env=env,
    )

    assert diagnosed.returncode == 0, diagnosed.stderr + diagnosed.stdout
    result = json.loads(diagnosed.stdout)
    assert result["platform"]["kind"] == "wsl"
    assert result["agent"] == {"name": "hermes", "available": True, "path": str(fake_hermes)}
    assert result["stack"]["version"] == "0.3.1"
    assert result["stack"]["ref"] == "v0.3.1"
    assert result["stack"]["required_ref"] == "v0.3.1"
    assert result["stack"]["exact_ref"] is False
    assert result["stack"]["development_override"] is True
    assert result["stack"]["compatible"] is True
    assert result["ready"] is True
    assert {item["name"] for item in result["prerequisites"]} >= {"python", "git", "bash", "make", "uv"}


def test_adopt_writes_and_validate_enforces_deterministic_stack_ref(tmp_path: Path):
    repo = tmp_path / "pinned-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    adopted = run_go("adopt", str(repo), "--project-id", "pinned", "--name", "Pinned")
    assert adopted.returncode == 0, adopted.stderr + adopted.stdout
    project_path = repo / ".go" / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    assert project["stack_ref"] == "v0.3.1"

    project["stack_ref"] = "main"
    project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    validated = run_go("validate", str(repo))
    assert validated.returncode == 1
    assert "stack_ref must be an immutable version tag" in validated.stderr


def test_stack_update_is_dry_run_first_and_apply_records_rollback(tmp_path: Path):
    repo = tmp_path / "update-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopted = run_go("adopt", str(repo), "--project-id", "update", "--name", "Update")
    assert adopted.returncode == 0, adopted.stderr + adopted.stdout
    project_path = repo / ".go" / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project.update({"required_stack_version": "0.2.0", "stack_ref": "v0.2.0"})
    project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    before = project_path.read_text(encoding="utf-8")

    planned = run_go("stack", "update", str(repo), "--to", "v0.3.0", "--json")
    assert planned.returncode == 0, planned.stderr + planned.stdout
    plan = json.loads(planned.stdout)
    assert plan["schema"] == "go-workflow.stack-update-plan.v1"
    assert plan["mode"] == "dry_run"
    assert plan["from_ref"] == "v0.2.0"
    assert plan["to_ref"] == "v0.3.0"
    assert len(plan["resolved_commit"]) == 40
    assert project_path.read_text(encoding="utf-8") == before
    assert not (repo / ".go" / "updates").exists()

    applied = run_go("stack", "update", str(repo), "--to", "v0.3.0", "--apply", "--json")
    assert applied.returncode == 0, applied.stderr + applied.stdout
    result = json.loads(applied.stdout)
    assert result["mode"] == "applied"
    updated = json.loads(project_path.read_text(encoding="utf-8"))
    assert updated["required_stack_version"] == "0.3.0"
    assert updated["stack_ref"] == "v0.3.0"
    rollback = repo / result["rollback_record"]
    rollback_data = json.loads(rollback.read_text(encoding="utf-8"))
    assert rollback_data["before_project"]["stack_ref"] == "v0.2.0"
    assert rollback_data["after_project"]["stack_ref"] == "v0.3.0"


def test_stack_update_rejects_missing_and_incompatible_refs_before_writing(tmp_path: Path):
    repo = tmp_path / "reject-update"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopted = run_go("adopt", str(repo), "--project-id", "reject", "--name", "Reject")
    assert adopted.returncode == 0, adopted.stderr + adopted.stdout
    project_path = repo / ".go" / "project.json"
    before = project_path.read_text(encoding="utf-8")

    missing = run_go("stack", "update", str(repo), "--to", "v99.0.0", "--apply", "--json")
    assert missing.returncode == 1
    assert "does not exist" in missing.stderr
    assert project_path.read_text(encoding="utf-8") == before

    fake_stack = tmp_path / "fake-stack"
    subprocess.run(["git", "init", "-q", "-b", "main", str(fake_stack)], check=True)
    (fake_stack / "go_workflow").mkdir()
    (fake_stack / "go_workflow" / "constants.py").write_text(
        'STACK_VERSION = "1.2.3"\nCURRENT_CONTRACT_VERSION = 2\n', encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=fake_stack, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "fake", "-q"], cwd=fake_stack, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "tag", "-a", "v9.9.9", "-m", "v9.9.9"], cwd=fake_stack, check=True)
    incompatible = run_go(
        "stack", "update", str(repo), "--to", "v9.9.9", "--stack-repo", str(fake_stack), "--apply", "--json",
    )
    assert incompatible.returncode == 1
    assert "declares version 1.2.3" in incompatible.stderr
    assert project_path.read_text(encoding="utf-8") == before


def test_concurrent_claims_have_exactly_one_winner(tmp_path: Path):
    repo = tmp_path / "claim-race"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopted = run_go("adopt", str(repo), "--project-id", "race", "--name", "Race")
    assert adopted.returncode == 0, adopted.stderr + adopted.stdout
    task = run_go("task", "create", str(repo), "--id", "one", "--summary", "One", "--epic", "workflow")
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed", "-q"], cwd=repo, check=True)
    command = [sys.executable, str(ROOT / "cli" / "go.py"), "claim", "one", "--repo", str(repo), "--allow-dirty"]
    first = subprocess.Popen(command + ["--agent", "first"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen(command + ["--agent", "second"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    results = [first.communicate(), second.communicate()]
    returncodes = [first.returncode, second.returncode]

    assert sorted(returncodes) == [0, 1], results
    active = json.loads((repo / ".go" / "tasks" / "active" / "one.json").read_text(encoding="utf-8"))
    assert active["claim"]["agent"] in {"first", "second"}
    assert not (repo / ".go" / "tasks" / "open" / "one.json").exists()
    claimed_events = [line for line in (repo / ".go" / "runs" / "events.jsonl").read_text().splitlines() if '"event": "task.claimed"' in line]
    assert len(claimed_events) == 1


def test_live_lock_is_not_stolen_and_dead_owner_is_recovered(tmp_path: Path):
    from go_workflow.state_io import StateLockError, repository_lock

    root = tmp_path / ".go"
    script = (
        "import sys,time; from pathlib import Path; "
        "from go_workflow.state_io import repository_lock; "
        "lock=repository_lock(Path(sys.argv[1]),'held'); lock.__enter__(); "
        "print('locked', flush=True); time.sleep(30)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(root)], cwd=ROOT,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
    try:
        try:
            with repository_lock(root, "held", timeout_seconds=0.1):
                raise AssertionError("live lock was stolen")
        except StateLockError as exc:
            assert "live state lock" in str(exc)
    finally:
        holder.kill()
        holder.wait(timeout=5)

    with repository_lock(root, "held", timeout_seconds=1) as recovered:
        assert recovered.recovered_stale is True


def test_jsonl_appends_are_process_locked_and_parseable(tmp_path: Path):
    path = tmp_path / ".go" / "runs" / "events.jsonl"
    script = (
        "import sys; from pathlib import Path; from go_workflow.state_io import append_jsonl_locked; "
        "p=Path(sys.argv[1]); worker=int(sys.argv[2]); "
        "[append_jsonl_locked(p, {'worker':worker,'index':i}) for i in range(30)]"
    )
    workers = [subprocess.Popen([sys.executable, "-c", script, str(path), str(index)], cwd=ROOT) for index in range(3)]
    assert [worker.wait(timeout=15) for worker in workers] == [0, 0, 0]
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 90
    assert {(event["worker"], event["index"]) for event in events} == {(worker, index) for worker in range(3) for index in range(30)}

def test_status_reports_template_setup_instead_of_next_project_work(tmp_path: Path):
    repo = tmp_path / "starter"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopted = run_go("adopt", str(repo), "--project-id", "starter", "--name", "Starter")
    assert adopted.returncode == 0, adopted.stderr + adopted.stdout
    task = run_go(
        "task", "create", str(repo), "--id", "example", "--summary", "Synthetic example",
        "--epic", "workflow", "--acceptance", "Example is present", "--verification", "git diff --check",
    )
    assert task.returncode == 0, task.stderr + task.stdout
    project_path = repo / ".go" / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["project_mode"] = "template"
    project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")

    result = run_go("status", str(repo), "--json")
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["project"]["mode"] == "template"
    assert payload["setup_required"] is True
    assert payload["setup_command"] == "./go spike . --brief \"<project intent>\""
    assert payload["next"] is None


def test_release_preflight_is_local_and_version_synchronized(tmp_path: Path):
    repo = tmp_path / "release-repo"
    shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "release", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "tag", "-a", "v0.3.1", "-m", "v0.3.1"], cwd=repo, check=True)
    env = os.environ.copy()
    env["GO_RELEASE_SKIP_TESTS"] = "1"
    result = subprocess.run(
        ["bash", "scripts/release-check.sh", "0.3.1"],
        cwd=repo, text=True, capture_output=True, env=env,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "release preflight: v0.3.1" in result.stdout
    assert "publish: not performed" in result.stdout


def test_release_preflight_rejects_tag_that_does_not_point_to_head(tmp_path: Path):
    repo = tmp_path / "release-repo"
    shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "release", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "tag", "-a", "v0.3.1", "-m", "v0.3.1"], cwd=repo, check=True)
    (repo / "after-tag.txt").write_text("later\n", encoding="utf-8")
    subprocess.run(["git", "add", "after-tag.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "later", "-q"], cwd=repo, check=True)
    env = os.environ.copy()
    env["GO_RELEASE_SKIP_TESTS"] = "1"

    result = subprocess.run(["bash", "scripts/release-check.sh", "0.3.1"], cwd=repo, text=True, capture_output=True, env=env)
    assert result.returncode == 1
    assert "does not point to HEAD" in result.stderr


def test_migrate_plans_then_applies_legacy_contract_without_implicit_writes(tmp_path: Path):
    repo = tmp_path / "legacy-contract"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopted = run_go("adopt", str(repo), "--project-id", "legacy", "--name", "Legacy")
    assert adopted.returncode == 0, adopted.stderr + adopted.stdout
    project_path = repo / ".go" / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project.pop("project_mode", None)
    project.pop("stack_ref", None)
    project.pop("contract_version", None)
    project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    hierarchy_path = repo / ".go" / "hierarchy.json"
    hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8"))
    hierarchy["feature_groups"] = hierarchy.pop("epics")
    hierarchy_path.write_text(json.dumps(hierarchy, indent=2) + "\n", encoding="utf-8")
    before = project_path.read_text(encoding="utf-8")

    planned = run_go("migrate", str(repo), "--json")
    assert planned.returncode == 0, planned.stderr + planned.stdout
    plan = json.loads(planned.stdout)
    assert plan["schema"] == "go-workflow.migration-plan.v1"
    assert plan["from_version"] == 1
    assert plan["to_version"] == 2
    assert plan["applied"] is False
    assert plan["changes"]
    assert project_path.read_text(encoding="utf-8") == before

    applied = run_go("migrate", str(repo), "--apply", "--json")
    assert applied.returncode == 0, applied.stderr + applied.stdout
    result = json.loads(applied.stdout)
    assert result["applied"] is True
    migrated = json.loads(project_path.read_text(encoding="utf-8"))
    assert migrated["contract_version"] == 2
    assert migrated["project_mode"] == "project"
    assert migrated["stack_ref"] == "v0.3.1"
    assert "epics" in json.loads(hierarchy_path.read_text(encoding="utf-8"))

    repeated = run_go("migrate", str(repo), "--json")
    assert json.loads(repeated.stdout)["changes"] == []


def test_migrate_rolls_back_when_proposed_contract_is_invalid(tmp_path: Path):
    repo = tmp_path / "invalid-legacy-contract"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopted = run_go("adopt", str(repo), "--project-id", "invalid", "--name", "Invalid")
    assert adopted.returncode == 0, adopted.stderr + adopted.stdout
    project_path = repo / ".go" / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project.pop("contract_version", None)
    project["project_mode"] = "unknown"
    project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    before = project_path.read_text(encoding="utf-8")

    applied = run_go("migrate", str(repo), "--apply", "--json")
    assert applied.returncode == 1
    assert "migration produced an invalid contract" in applied.stderr
    assert project_path.read_text(encoding="utf-8") == before


def test_autonomous_execution_rejects_newer_required_stack_version(tmp_path: Path):
    repo = tmp_path / "future-stack-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "future", "--name", "Future")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go(
        "task", "create", str(repo), "--id", "future-task", "--summary", "Future task", "--epic", "workflow",
        "--acceptance", "Execution is version gated", "--verification", "python3 -c 'print(1)'",
    )
    assert task.returncode == 0, task.stderr + task.stdout
    project_path = repo / ".go" / "project.json"
    project = json.loads(project_path.read_text())
    project["required_stack_version"] = "999.0.0"
    project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")

    executed = run_go("go-loop", str(repo), "--execute", "--json")

    assert executed.returncode == 1
    assert "requires go-workflow-stack >= 999.0.0" in executed.stderr
    assert not (repo / ".go" / "tasks" / "active" / "future-task.json").exists()


def test_go_loop_contract_gate_rejects_generic_acceptance_before_claim(tmp_path: Path):
    repo = tmp_path / "semantic-critic-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "semantic", "--name", "Semantic")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go("task", "create", str(repo), "--id", "vague-task", "--summary", "Vague task", "--epic", "workflow", "--verification", "python3 -c 'print(1)'")
    assert task.returncode == 0, task.stderr + task.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed semantic critic state", "-q"], cwd=repo, check=True)

    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--json")
    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "contract_gate"
    assert result["commands_run"] == 0
    assert (repo / ".go" / "tasks" / "open" / "vague-task.json").is_file()
    assert not (repo / ".go" / "tasks" / "active" / "vague-task.json").exists()
    assert "generic" in json.dumps(result["run_envelope"]["preflight"]["contract_findings"])


def test_auto_execute_continues_across_multiple_tasks(tmp_path: Path):
    repo = tmp_path / "multi-exec-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "multi", "--name", "Multi", "--verification", "python3 -c \"print('ok')\"")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    first = run_go("task", "create", str(repo), "--id", "first", "--summary", "First", "--epic", "workflow", "--acceptance", "First verification prints first and exits zero", "--verification", "python3 -c \"print('first')\"")
    second = run_go("task", "create", str(repo), "--id", "second", "--summary", "Second", "--epic", "workflow", "--acceptance", "Second verification prints second and exits zero", "--verification", "python3 -c \"print('second')\"")
    assert first.returncode == 0, first.stderr + first.stdout
    assert second.returncode == 0, second.stderr + second.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed go state", "-q"], cwd=repo, check=True)

    executed = run_go("auto", str(repo), "--max-tasks", "2", "--execute", "--agent", "pytest", "--json")
    assert executed.returncode == 0, executed.stderr + executed.stdout
    result = json.loads(executed.stdout)
    assert result["status"] == "done"
    assert result["completed_tasks"] == ["first", "second"]
    assert result["commands_run"] == 3
    assert result["completion_audit"]["project_verification_passed"] is True
    assert len(result["checkpoints"]) == 2
    assert (repo / ".go" / "tasks" / "done" / "first.json").is_file()
    assert (repo / ".go" / "tasks" / "done" / "second.json").is_file()


def test_go_loop_reports_task_budget_exhaustion_when_open_work_remains(tmp_path: Path):
    repo = tmp_path / "bounded-loop-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "bounded", "--name", "Bounded")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    for task_id in ("first", "second"):
        created = run_go(
            "task", "create", str(repo), "--id", task_id, "--summary", task_id.title(), "--epic", "workflow",
            "--acceptance", f"{task_id.title()} verification exits zero", "--verification", "python3 -c 'print(1)'",
        )
        assert created.returncode == 0, created.stderr + created.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed bounded loop", "-q"], cwd=repo, check=True)

    executed = run_go(
        "go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--json",
    )

    assert executed.returncode == 0
    result = json.loads(executed.stdout)
    assert result["status"] == "budget_exhausted"
    assert result["completed_tasks"] == ["first"]
    assert result["budget_exhausted"] is True
    assert (repo / ".go" / "tasks" / "open" / "second.json").is_file()
    assert "resume" in result["next_action"]


def test_go_loop_does_not_report_done_while_blocked_tasks_remain(tmp_path: Path):
    repo = tmp_path / "blocked-goal-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "blockedgoal", "--name", "Blocked Goal")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    created = run_go(
        "task", "create", str(repo), "--id", "cannot-finish", "--summary", "Cannot finish", "--epic", "workflow",
        "--acceptance", "The failing task remains visible to goal completion", "--verification", "python3 -c 'import sys; sys.exit(9)'",
    )
    assert created.returncode == 0, created.stderr + created.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed blocked goal", "-q"], cwd=repo, check=True)
    first = run_go("go-loop", str(repo), "--max-tasks", "1", "--max-attempts", "1", "--execute", "--agent", "pytest", "--json")
    assert first.returncode == 1
    assert json.loads(first.stdout)["status"] == "blocked"

    resumed = run_go("go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--allow-dirty", "--json")

    assert resumed.returncode == 1
    result = json.loads(resumed.stdout)
    assert result["status"] == "blocked"
    assert result["blocked_task"] == "cannot-finish"
    assert result["completed_tasks"] == []
    assert "blocked task" in result["summary"].lower()


def test_goal_completion_runs_project_verification_after_tasks_finish(tmp_path: Path):
    repo = tmp_path / "goal-verification-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go(
        "adopt", str(repo), "--project-id", "goalverify", "--name", "Goal Verify",
        "--verification", "python3 -c 'import sys; sys.exit(8)'",
    )
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    created = run_go(
        "task", "create", str(repo), "--id", "task-passes", "--summary", "Task passes", "--epic", "workflow",
        "--acceptance", "The focused task verification exits zero", "--verification", "python3 -c 'print(1)'",
    )
    assert created.returncode == 0, created.stderr + created.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed goal verification", "-q"], cwd=repo, check=True)

    executed = run_go("go-loop", str(repo), "--max-tasks", "1", "--execute", "--agent", "pytest", "--json")

    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "goal_incomplete"
    assert result["completed_tasks"] == ["task-passes"]
    assert result["completion_audit"]["project_verification_passed"] is False
    assert result["completion_audit"]["task_evidence_complete"] is True
    assert (repo / ".go" / "tasks" / "done" / "task-passes.json").is_file()
    assert "follow-up" in result["next_action"]


def test_auto_execute_blocks_on_preflight_gate(tmp_path: Path):
    repo = tmp_path / "dirty-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "dirty", "--name", "Dirty")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go("task", "create", str(repo), "--id", "blocked", "--summary", "Blocked", "--epic", "workflow", "--acceptance", "Execution does not start while secret-looking dirty state exists")
    assert task.returncode == 0, task.stderr + task.stdout
    (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    executed = run_go("auto", str(repo), "--execute", "--json")
    assert executed.returncode == 1
    result = json.loads(executed.stdout)
    assert result["status"] == "safety_gate"
    assert result["run_envelope"]["preflight"]["human_gate_required"] is True
    assert "secret-looking" in "\n".join(result["run_envelope"]["preflight"]["human_gate_blockers"])
    assert (repo / ".go" / "tasks" / "open" / "blocked.json").is_file()


def test_go_router_normalizes_go_variants_and_detects_repo_state(tmp_path: Path):
    missing = tmp_path / "missing-project"
    missing_route = run_go("router", str(missing), "--command", "gOo", "--intent", "marktplaats inbox bot", "--json")
    assert missing_route.returncode == 0, missing_route.stderr + missing_route.stdout
    missing_plan = json.loads(missing_route.stdout)
    assert missing_plan["normalized_command"] == "go"
    assert missing_plan["state"]["repo_exists"] is False
    assert missing_plan["recommended"]["command"] == "spike"

    existing_without_go = tmp_path / "existing-without-go"
    existing_without_go.mkdir()
    subprocess.run(["git", "init", "-q", str(existing_without_go)], check=True)
    repair_route = run_go("router", str(existing_without_go), "--command", "GO", "--intent", "existing repo needs workflow", "--json")
    assert repair_route.returncode == 0, repair_route.stderr + repair_route.stdout
    repair_plan = json.loads(repair_route.stdout)
    assert repair_plan["state"]["repo_exists"] is True
    assert repair_plan["state"]["is_git_repo"] is True
    assert repair_plan["state"]["has_go"] is False
    assert repair_plan["recommended"]["command"] == "spike"
    assert repair_plan["recommended"]["mode"] == "repair_existing_repo"
    assert "--skip-repo-complete" in repair_plan["recommended"]["example"]

    repo = tmp_path / "ready-project"
    spike = run_go("spike", str(repo), "--project-id", "ready", "--name", "Ready", "--task", "first|First task")
    assert spike.returncode == 0, spike.stderr + spike.stdout
    ready_route = run_go("router", str(repo), "--command", "GOO", "--intent", "ga verder", "--json")
    assert ready_route.returncode == 0, ready_route.stderr + ready_route.stdout
    ready_plan = json.loads(ready_route.stdout)
    assert ready_plan["state"]["has_vision"] is True
    assert ready_plan["state"]["has_principles"] is True
    assert ready_plan["state"]["open_task_count"] == 1
    assert ready_plan["recommended"]["command"] == "auto"

    handoff_route = run_go("router", str(repo), "--command", "go", "--intent", "controle afgeven werk tot groen", "--json")
    assert handoff_route.returncode == 0, handoff_route.stderr + handoff_route.stdout
    handoff_plan = json.loads(handoff_route.stdout)
    assert handoff_plan["recommended"]["command"] == "go-loop"
    loop_route = run_go("router", str(repo), "--command", "GOO", "--intent", "controle afgeven werk tot groen", "--json")
    assert loop_route.returncode == 0, loop_route.stderr + loop_route.stdout
    loop_plan = json.loads(loop_route.stdout)
    assert loop_plan["recommended"]["command"] == "go-loop"
    # Direct command tokens for the stronger loop should also route, not only `go` + intent words.
    direct_loop_route = run_go("router", str(repo), "--command", "go-loop", "--intent", "", "--json")
    assert direct_loop_route.returncode == 0, direct_loop_route.stderr + direct_loop_route.stdout
    direct_loop_plan = json.loads(direct_loop_route.stdout)
    assert direct_loop_plan["normalized_command"] == "go-loop"
    assert direct_loop_plan["recommended"]["command"] == "go-loop"


def test_router_command_examples_shell_quote_paths_and_free_form_text(tmp_path: Path):
    repo = tmp_path / "missing; touch PWNED; #'s project"
    intent = 'brief; touch PWNED; "quoted"'

    routed = run_go("router", str(repo), "--command", "go", "--intent", intent, "--json")
    assert routed.returncode == 0, routed.stderr + routed.stdout
    example = json.loads(routed.stdout)["recommended"]["example"]
    assert shlex.split(example) == [
        "python3",
        str(ROOT / "go_workflow" / "cli.py"),
        "spike",
        str(repo.resolve()),
        "--brief",
        intent,
    ]

    bare_go = run_go("go", str(repo), "--intent", intent, "--json")
    assert bare_go.returncode == 0, bare_go.stderr + bare_go.stdout
    next_command = json.loads(bare_go.stdout)["next_command"]
    assert shlex.split(next_command) == [
        "python3",
        str(ROOT / "go_workflow" / "cli.py"),
        "spike",
        str(repo.resolve()),
        "--brief",
        intent,
    ]


def test_bare_go_dry_run_does_not_create_task_from_intent_without_write(tmp_path: Path):
    repo = tmp_path / "bare-go-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "bare", "--name", "Bare")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed go state", "-q"], cwd=repo, check=True)

    # The adopted repo has no executable open tasks; bare go JSON inspection must not mutate state.
    result = run_go("go", str(repo), "--intent", "Add bare go task routing", "--json")
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema"] == "go-workflow.bare-go.v1"
    assert payload["created_task"] is None
    assert payload["proposed_task"]["id"] == "add-bare-go-task-routing"
    assert payload["write_boundary"].startswith("dry_run")
    assert payload["action"] == "go-auto"
    assert payload["plan"]["next_tasks"] == ["add-bare-go-task-routing"]
    assert not (repo / ".go" / "tasks" / "open" / "add-bare-go-task-routing.json").exists()
    status = subprocess.run(["git", "status", "--short"], cwd=repo, text=True, capture_output=True, check=True)
    assert status.stdout == ""

    written = run_go("go", str(repo), "--intent", "Add bare go task routing", "--write", "--json")
    assert written.returncode == 0, written.stderr + written.stdout
    written_payload = json.loads(written.stdout)
    assert written_payload["created_task"]["id"] == "add-bare-go-task-routing"
    assert (repo / ".go" / "tasks" / "open" / "add-bare-go-task-routing.json").is_file()


def test_autonomy_benchmark_tracks_ralph_equivalence_with_adapter_boundary():
    benchmark = (ROOT / "docs" / "autonomy-benchmark.md").read_text()
    assert "One prompt routes repo-local work from `go` | `PASS`" in benchmark
    assert "Adapter-boundary build/edit executor | `PASS`" in benchmark
    assert "Adapter availability proof | `PASS`" in benchmark
    assert "Dangerous adapter bypass avoided | `PASS`" in benchmark
    assert "Diff/scope enforcement after adapters | `PASS`" in benchmark
    assert "Hard command and time budget | `PASS`" in benchmark
    assert "Exact resume state | `PASS`" in benchmark
    assert "Scoped transactional ship policy | `PASS`" in benchmark
    assert "Semantic critic/judge | `PASS`" in benchmark
    assert "Follow-up task generation | `PASS`" in benchmark
    assert "Vision/principles execution context | `PASS`" in benchmark
    assert "Template-to-project pairing | `PASS`" in benchmark
    assert "Vision-level completion audit | `PASS`" in benchmark
    assert "Oh-My-Codex/Ralph-style integrated runtime | `PARTIAL`" in benchmark
    assert "Unconstrained self-improving agent | `PARTIAL`" in benchmark


def test_bundle_export_import_smoke(tmp_path: Path):
    bundle = tmp_path / "bundle.json"
    source = template_repo()
    export_result = subprocess.run([
        sys.executable,
        str(ROOT / "cli" / "go.py"),
        "bundle",
        "export",
        str(source),
        "--output",
        str(bundle),
    ], text=True, capture_output=True)
    assert export_result.returncode == 0, export_result.stderr + export_result.stdout
    second_bundle = tmp_path / "bundle-second.json"
    second_export = subprocess.run([
        sys.executable,
        str(ROOT / "cli" / "go.py"),
        "bundle",
        "export",
        str(source),
        "--output",
        str(second_bundle),
    ], text=True, capture_output=True)
    assert second_export.returncode == 0, second_export.stderr + second_export.stdout
    first_id = json.loads(bundle.read_text())["bundle_id"]
    second_id = json.loads(second_bundle.read_text())["bundle_id"]
    assert first_id != second_id
    target = tmp_path / "target"
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    adopt_result = subprocess.run([
        sys.executable,
        str(ROOT / "cli" / "go.py"),
        "adopt",
        str(target),
        "--project-id",
        "target",
        "--name",
        "Target",
        "--feature-group",
        "workflow|Workflow",
        "--feature",
        "workflow|repo-local|Repo-local",
        "--verification",
        "git diff --check",
    ], text=True, capture_output=True)
    assert adopt_result.returncode == 0, adopt_result.stderr + adopt_result.stdout
    dry_run = subprocess.run([
        sys.executable,
        str(ROOT / "cli" / "go.py"),
        "bundle",
        "import",
        str(target),
        str(bundle),
    ], text=True, capture_output=True)
    assert dry_run.returncode == 0, dry_run.stderr + dry_run.stdout
    assert '"mode": "dry_run"' in dry_run.stdout
    write_result = subprocess.run([
        sys.executable,
        str(ROOT / "cli" / "go.py"),
        "bundle",
        "import",
        str(target),
        str(bundle),
        "--write",
        "--agent",
        "pytest",
        "--task-id",
        "import-smoke",
    ], text=True, capture_output=True)
    assert write_result.returncode == 0, write_result.stderr + write_result.stdout
    assert list((target / ".go" / "imports").glob("*.json"))
    validate_result = subprocess.run([sys.executable, str(ROOT / "cli" / "go.py"), "validate", str(target)], text=True, capture_output=True)
    assert validate_result.returncode == 0, validate_result.stderr + validate_result.stdout
