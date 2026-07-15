import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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

    launched = subprocess.run([str(launcher), "validate", "."], cwd=repo, text=True, capture_output=True, env=env)

    assert launched.returncode == 0, launched.stderr + launched.stdout
    assert f"ok: {repo / '.go'}" in launched.stdout


def test_template_bootstrap_keeps_explicit_stack_on_pinned_runtime(tmp_path: Path):
    source = tmp_path / "stack-source"
    remote = tmp_path / "stack.git"
    checkout = tmp_path / "go-workflow-stack"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    (source / "cli").mkdir()
    (source / "cli" / "go.py").write_text('STACK_VERSION = "0.2.0"\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "v1", "-q"], cwd=source, check=True)
    subprocess.run(["git", "tag", "v0.2.0"], cwd=source, check=True)
    subprocess.run(["git", "clone", "--bare", "-q", str(source), str(remote)], check=True)
    subprocess.run(["git", "clone", "--branch", "v0.2.0", "-q", str(remote), str(checkout)], check=True)
    pinned_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, text=True, capture_output=True, check=True).stdout.strip()
    (source / "cli" / "go.py").write_text('STACK_VERSION = "0.3.0"\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "v2", "-q"], cwd=source, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=source, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=source, check=True)
    project = tmp_path / "project"
    shutil.copytree(template_repo(), project, ignore=shutil.ignore_patterns(".git", ".DS_Store"))
    env = os.environ.copy()
    env["GO_STACK"] = str(checkout)
    env["GO_STACK_REMOTE"] = str(remote)

    bootstrapped = subprocess.run(["bash", "scripts/bootstrap-stack.sh"], cwd=project, text=True, capture_output=True, env=env)

    assert bootstrapped.returncode == 0, bootstrapped.stderr + bootstrapped.stdout
    assert (checkout / "cli" / "go.py").read_text() == 'STACK_VERSION = "0.2.0"\n'
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, text=True, capture_output=True, check=True).stdout.strip()
    assert head == pinned_head

    subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=checkout, check=True)
    subprocess.run(["git", "checkout", "--detach", "-q", "origin/main"], cwd=checkout, check=True)

    mismatched = subprocess.run(["bash", "scripts/bootstrap-stack.sh"], cwd=project, text=True, capture_output=True, env=env)

    assert mismatched.returncode == 4
    assert "does not provide pinned runtime v0.2.0" in mismatched.stderr


def test_live_hermes_acceptance_refuses_to_claim_proof_without_binary():
    env = os.environ.copy()
    env["GO_RUN_REAL_HERMES_E2E"] = "1"
    env["PATH"] = "/usr/bin:/bin"

    attempted = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "run-hermes-acceptance.sh")],
        text=True, capture_output=True, env=env,
    )

    assert attempted.returncode == 2
    assert "NOT PROVEN: hermes is not available on PATH" in attempted.stderr


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
    assert "dangerously-bypass" not in (ROOT / "cli" / "go.py").read_text()


def test_agent_mode_task_selects_safe_default_codex_executor(tmp_path: Path):
    repo = tmp_path / "default-agent-project"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$GO_REPO/codex-args.txt\"\nout=''\nprevious=''\nfor arg in \"$@\"; do\n  if [ \"$previous\" = '-o' ]; then out=\"$arg\"; fi\n  previous=\"$arg\"\ndone\nif [ -n \"$out\" ]; then\n  count_file=\"$GO_REPO/.go/runs/fake-critic-count\"\n  count=0\n  if [ -f \"$count_file\" ]; then count=$(cat \"$count_file\"); fi\n  if [ \"$count\" -eq 0 ]; then\n    printf 'GO_CRITIC_VERDICT: BLOCK\\nExercise the repair loop once.\\n' > \"$out\"\n  else\n    printf 'GO_CRITIC_VERDICT: PASS\\nNo blocking findings.\\n' > \"$out\"\n  fi\n  echo $((count + 1)) > \"$count_file\"\nelse\n  printf 'built\\n' > \"$GO_REPO/built.txt\"\nfi\n",
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
    assert "GO_CRITIC_VERDICT: PASS" in result["attempts"][1]["critic"]["result"]["verdict_text"]
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
    verdict = "BLOCK" if task_id == "parse-headings" and count == 0 else "PASS"
    print(f"GO_CRITIC_VERDICT: {verdict}")
    print("Add one repair pass." if verdict == "BLOCK" else "Evidence is sufficient.")
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
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "doctor", "--name", "Doctor")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    project_path = repo / ".go" / "project.json"
    project = json.loads(project_path.read_text())
    project["required_stack_version"] = "0.2.0"
    project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

    diagnosed = subprocess.run(
        [sys.executable, str(ROOT / "cli" / "go.py"), "doctor", str(repo), "--platform", "wsl", "--agent", "hermes", "--json"],
        text=True, capture_output=True, env=env,
    )

    assert diagnosed.returncode == 0, diagnosed.stderr + diagnosed.stdout
    result = json.loads(diagnosed.stdout)
    assert result["platform"]["kind"] == "wsl"
    assert result["agent"] == {"name": "hermes", "available": True, "path": str(fake_hermes)}
    assert result["stack"]["version"] == "0.2.0"
    assert result["stack"]["ref"] == "v0.2.0"
    assert result["stack"]["required_ref"] == "v0.2.0"
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
    assert project["stack_ref"] == "v0.2.0"

    project["stack_ref"] = "main"
    project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    validated = run_go("validate", str(repo))
    assert validated.returncode == 1
    assert "stack_ref must be an immutable version tag" in validated.stderr


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


def test_release_preflight_is_local_and_version_synchronized():
    env = os.environ.copy()
    env["GO_RELEASE_SKIP_TESTS"] = "1"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "release-check.sh"), "0.2.0"],
        cwd=ROOT, text=True, capture_output=True, env=env,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "release preflight: v0.2.0" in result.stdout
    assert "publish: not performed" in result.stdout


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
