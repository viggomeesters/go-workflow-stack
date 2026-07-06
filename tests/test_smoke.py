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
    import json
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

    validate = run_go("validate", str(repo))
    assert validate.returncode == 0, validate.stderr + validate.stdout
    auto = run_go("auto", str(repo), "--max-tasks", "2", "--json")
    assert auto.returncode == 0, auto.stderr + auto.stdout
    import json
    plan = json.loads(auto.stdout)
    assert plan["mode"] == "go-auto"
    assert plan["control_handoff"] is True
    assert plan["can_escalate_to"] == ["go-loop"]
    assert plan["loop"] == ["status", "next", "claim", "execute", "verify", "recheck", "devil", "finish", "self-reflect", "summarize", "continue-or-escalate"]
    assert plan["next_tasks"] == ["design-monitor", "build-poller"]
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


def test_go_router_normalizes_go_variants_and_detects_repo_state(tmp_path: Path):
    missing = tmp_path / "missing-project"
    missing_route = run_go("router", str(missing), "--command", "gOo", "--intent", "marktplaats inbox bot", "--json")
    assert missing_route.returncode == 0, missing_route.stderr + missing_route.stdout
    import json
    missing_plan = json.loads(missing_route.stdout)
    assert missing_plan["normalized_command"] == "go"
    assert missing_plan["state"]["repo_exists"] is False
    assert missing_plan["recommended"]["command"] == "spike"

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
    import json
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
