import json
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
    if template_repo().name == "go-project-template":
        assert checks["check_script_bootstraps_stack"]["ok"] is True


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
    assert handoff_plan["target_runtime"] == "hermes-bertus"
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
    assert "missing_credentials" in plan["execution_policy"]["human_gates"]
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
    task = run_go("task", "create", str(repo), "--id", "verify-only", "--summary", "Verify only", "--epic", "workflow", "--verification", "python3 -c \"print('verified')\"")
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
    assert result["commands_run"] == 1
    assert result["checkpoints"]


def test_auto_execute_continues_across_multiple_tasks(tmp_path: Path):
    repo = tmp_path / "multi-exec-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "multi", "--name", "Multi", "--verification", "python3 -c \"print('ok')\"")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    first = run_go("task", "create", str(repo), "--id", "first", "--summary", "First", "--epic", "workflow", "--verification", "python3 -c \"print('first')\"")
    second = run_go("task", "create", str(repo), "--id", "second", "--summary", "Second", "--epic", "workflow", "--verification", "python3 -c \"print('second')\"")
    assert first.returncode == 0, first.stderr + first.stdout
    assert second.returncode == 0, second.stderr + second.stdout
    subprocess.run(["git", "add", ".go"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Pytest", "-c", "user.email=pytest@example.com", "commit", "-m", "seed go state", "-q"], cwd=repo, check=True)

    executed = run_go("auto", str(repo), "--max-tasks", "2", "--execute", "--agent", "pytest", "--json")
    assert executed.returncode == 0, executed.stderr + executed.stdout
    result = json.loads(executed.stdout)
    assert result["status"] == "done"
    assert result["completed_tasks"] == ["first", "second"]
    assert result["commands_run"] == 2
    assert len(result["checkpoints"]) == 2
    assert (repo / ".go" / "tasks" / "done" / "first.json").is_file()
    assert (repo / ".go" / "tasks" / "done" / "second.json").is_file()


def test_auto_execute_blocks_on_preflight_gate(tmp_path: Path):
    repo = tmp_path / "dirty-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "dirty", "--name", "Dirty")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    task = run_go("task", "create", str(repo), "--id", "blocked", "--summary", "Blocked", "--epic", "workflow")
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


def test_bare_go_creates_task_from_intent_and_returns_loop_plan(tmp_path: Path):
    repo = tmp_path / "bare-go-project"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    adopt = run_go("adopt", str(repo), "--project-id", "bare", "--name", "Bare")
    assert adopt.returncode == 0, adopt.stderr + adopt.stdout
    # The adopted repo has no executable open tasks; bare go must materialize intent into .go state.
    result = run_go("go", str(repo), "--intent", "Add bare go task routing", "--json")
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema"] == "go-workflow.bare-go.v1"
    assert payload["created_task"]["id"] == "add-bare-go-task-routing"
    assert payload["action"] == "go-auto"
    assert payload["plan"]["next_tasks"] == ["add-bare-go-task-routing"]
    assert (repo / ".go" / "tasks" / "open" / "add-bare-go-task-routing.json").is_file()


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
