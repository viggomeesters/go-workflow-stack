"""Behavioral contracts for task-bound repository context."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from go_workflow.cli import build_execution_context, validate_repo, validate_task
from go_workflow.repository_index import (
    RepositoryIndexError,
    build_graph,
    select_repository_context,
    validate_repository_context,
)
from test_repository_index import repository, run_cli
from test_abc_worktrees import create, setup_repo


def context(**overrides):
    value = {
        "nodes": ["application"],
        "queries": ["service"],
        "max_nodes": 12,
        "max_edges": 20,
        "dependency_depth": 2,
        "include_tests": True,
        "impact_policy": "strict",
    }
    value.update(overrides)
    return value


def test_repository_context_contract_is_strict_and_matches_task_validation():
    assert validate_repository_context(context(), "task.repository_context") == []
    task = {
        "schema": "go-workflow.repo-local.task.v1",
        "kind": "task",
        "id": "context-task",
        "project": "example",
        "status": "open",
        "summary": "Use bounded repository context",
        "scope": {"read": [], "modify": []},
        "acceptance": ["Context is bounded"],
        "verification": ["git diff --check"],
        "claim": {"agent": None, "claimed_at": None},
        "repository_context": context(),
    }
    assert validate_task(task, "task.json") == []

    invalid = context(extra="not allowed", max_nodes=0)
    findings = validate_repository_context(invalid, "task.repository_context")
    assert any("unknown properties" in finding for finding in findings)
    assert any("max_nodes" in finding for finding in findings)


def test_context_selection_is_deterministic_bounded_and_includes_dependencies_and_tests(tmp_path: Path):
    repo = repository(tmp_path)
    build_graph(repo)

    first = select_repository_context(repo, context())
    second = select_repository_context(repo, context())

    assert first == second
    assert first["authority"] == "derived_routing_context_not_source_of_truth"
    assert first["fresh"] is True
    assert len(first["nodes"]) <= 12
    assert len(first["edges"]) <= 20
    node_ids = {node["id"] for node in first["nodes"]}
    assert "component:application" in node_ids
    assert "file:src/app.py" in node_ids
    assert "component:tests" in node_ids
    assert "test:tests/test_app.py" in node_ids
    assert {source["path"] for source in first["sources"]} <= {
        node["path"] for node in first["nodes"] if node.get("path")
    }
    assert all(len(source["sha256"]) == 64 for source in first["sources"])


def test_direct_nodes_are_mandatory_and_invalid_nodes_fail_actionably(tmp_path: Path):
    repo = repository(tmp_path)
    build_graph(repo)

    selected = select_repository_context(
        repo,
        context(nodes=["application", "tests"], queries=[], max_nodes=2, max_edges=1),
    )
    assert {node["id"] for node in selected["nodes"]} == {
        "component:application",
        "component:tests",
    }

    with pytest.raises(RepositoryIndexError, match="unknown repository-map node"):
        select_repository_context(repo, context(nodes=["missing"], queries=[]))


def test_context_selection_fails_closed_on_missing_or_stale_graph(tmp_path: Path):
    repo = repository(tmp_path)
    with pytest.raises(RepositoryIndexError, match="missing repository graph"):
        select_repository_context(repo, context())

    build_graph(repo)
    (repo / "src" / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(RepositoryIndexError, match="graph is stale"):
        select_repository_context(repo, context())


def test_repo_validation_resolves_stable_node_references_but_legacy_tasks_remain_valid(tmp_path: Path):
    repo = repository(tmp_path)
    task_path = repo / ".go" / "tasks" / "open" / "task-schema-smoke.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    assert "repository_context" not in task
    assert validate_repo(repo) == []

    task["repository_context"] = context(nodes=["missing"], queries=[])
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    findings = validate_repo(repo)
    assert any("unknown repository-map node 'missing'" in finding for finding in findings)


def test_task_create_accepts_a_valid_repository_context_file(tmp_path: Path):
    repo = repository(tmp_path)
    context_path = tmp_path / "repository-context.json"
    context_path.write_text(json.dumps(context()), encoding="utf-8")

    result = run_cli(
        "task", "create", repo,
        "--id", "bounded-context",
        "--summary", "Use bounded repository context",
        "--feature", "workflow-contract.schema-validation",
        "--repository-context", context_path,
    )

    assert result.returncode == 0, result.stderr
    task = json.loads((repo / ".go" / "tasks" / "open" / "bounded-context.json").read_text())
    assert task["repository_context"] == context()


@pytest.mark.parametrize("phase", ["build", "critic", "repair"])
def test_execution_context_contains_only_the_selected_repository_subgraph(tmp_path: Path, phase: str):
    repo = repository(tmp_path)
    build_graph(repo)
    task_path = repo / ".go" / "tasks" / "open" / "task-schema-smoke.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["repository_context"] = context(max_nodes=6, max_edges=8)

    execution_context = build_execution_context(repo, task, phase=phase)

    selected = execution_context["repository_context"]
    assert selected["contract"] == task["repository_context"]
    assert len(selected["nodes"]) <= 6
    assert len(selected["edges"]) <= 8
    assert "graph" not in execution_context
    assert "coverage" not in selected


def test_runtime_payloads_match_task_and_execution_context_schemas(tmp_path: Path):
    repo = repository(tmp_path)
    build_graph(repo)
    task_path = repo / ".go" / "tasks" / "open" / "task-schema-smoke.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["repository_context"] = context()
    task_schema = json.loads((Path(__file__).parents[1] / "schemas" / "task.schema.json").read_text())
    context_schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "execution-context.schema.json").read_text()
    )["properties"]["context"]["properties"]["repository_context"]

    assert not list(Draft202012Validator(task_schema).iter_errors(task))
    selected = select_repository_context(repo, task["repository_context"])
    assert not list(Draft202012Validator(context_schema).iter_errors(selected))


def test_durable_snapshot_retains_and_verifies_the_bounded_subgraph(tmp_path: Path):
    from go_workflow.execution_context import create_snapshot, verify_snapshot

    control, base = setup_repo(tmp_path)
    workspace = tmp_path / "worker"
    assert create(control, base, workspace).returncode == 0
    task_path = control / ".go" / "tasks" / "active" / "task-schema-smoke.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["repository_context"] = context(nodes=["application"], queries=[], max_nodes=4, max_edges=6)
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    build_graph(workspace)
    execution = build_execution_context(workspace, task, phase="build")

    ref = create_snapshot(workspace, task, "build", 1, "direct", execution)
    snapshot = verify_snapshot(workspace, task["id"], ref["path"], ref["sha256"])

    repository_context = snapshot["context"]["repository_context"]
    assert repository_context["fresh"] is True
    assert len(repository_context["nodes"]) <= 4
    assert repository_context == execution["repository_context"]
