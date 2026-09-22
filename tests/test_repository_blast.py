"""Behavioral contracts for repository blast radius and conformance."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema

from test_repository_index import repository, run_cli


ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def versioned_repository(tmp_path: Path) -> tuple[Path, str]:
    repo = repository(tmp_path)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Repository Test")
    git(repo, "config", "user.email", "repository@example.com")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    return repo, git(repo, "rev-parse", "HEAD")


def build(repo: Path):
    result = run_cli("index", "build", repo, "--json")
    assert result.returncode == 0, result.stderr


def task_context(repo: Path, *, nodes: list[str], policy: str):
    task_path = repo / ".go" / "tasks" / "open" / "task-schema-smoke.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["repository_context"] = {
        "nodes": nodes,
        "queries": [],
        "max_nodes": 20,
        "max_edges": 40,
        "dependency_depth": 2,
        "include_tests": True,
        "impact_policy": policy,
    }
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")


def test_clean_diff_has_no_changed_or_impacted_nodes(tmp_path: Path):
    repo, base = versioned_repository(tmp_path)
    build(repo)

    result = run_cli("index", "blast", repo, "--base", base, "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    schema = json.loads((ROOT / "schemas" / "repository-blast.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(payload, schema)
    assert payload["changed_paths"] == []
    assert payload["changed_nodes"] == []
    assert payload["impacted_nodes"] == []
    assert payload["recommended_tests"] == []


def test_changed_file_traces_reverse_dependencies_and_recommends_tests(tmp_path: Path):
    repo, base = versioned_repository(tmp_path)
    (repo / "src" / "service.py").write_text(
        "class Service:\n    def run(self) -> str:\n        return 'changed'\n",
        encoding="utf-8",
    )
    build(repo)

    result = run_cli("index", "blast", repo, "--base", base, "--max-nodes", 30, "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "src/service.py" in payload["changed_paths"]
    assert "file:src/service.py" in {node["id"] for node in payload["changed_nodes"]}
    assert "component:application" in {node["id"] for node in payload["impacted_nodes"]}
    assert "test:tests/test_app.py" in {node["id"] for node in payload["recommended_tests"]}
    assert all(node.get("path") or node["kind"] == "component" for node in payload["impacted_nodes"])


def test_declared_change_passes_strict_task_conformance(tmp_path: Path):
    repo, base = versioned_repository(tmp_path)
    (repo / "src" / "service.py").write_text("class Service:\n    pass\n", encoding="utf-8")
    task_context(repo, nodes=["application"], policy="strict")
    build(repo)

    result = run_cli(
        "index", "blast", repo, "--base", base,
        "--task-id", "task-schema-smoke", "--json",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["conformance"]["status"] == "passed"


def test_task_conformance_is_advisory_or_strict_from_the_task_contract(tmp_path: Path):
    repo, base = versioned_repository(tmp_path)
    (repo / "src" / "service.py").write_text("class Service:\n    pass\n", encoding="utf-8")
    task_context(repo, nodes=["tests"], policy="advisory")
    build(repo)

    advisory = run_cli(
        "index", "blast", repo, "--base", base,
        "--task-id", "task-schema-smoke", "--json",
    )
    assert advisory.returncode == 0, advisory.stderr
    advisory_payload = json.loads(advisory.stdout)
    assert advisory_payload["conformance"]["status"] == "findings"
    assert "application" in advisory_payload["conformance"]["missing_declarations"]

    task_context(repo, nodes=["tests"], policy="strict")
    strict = run_cli(
        "index", "blast", repo, "--base", base,
        "--task-id", "task-schema-smoke", "--json",
    )
    assert strict.returncode == 1
    assert json.loads(strict.stdout)["conformance"]["policy"] == "strict"


def test_changed_unsupported_path_is_explicit_unexpected_scope(tmp_path: Path):
    repo, base = versioned_repository(tmp_path)
    (repo / "README.md").write_text("changed docs\n", encoding="utf-8")
    task_context(repo, nodes=["application"], policy="strict")
    build(repo)

    result = run_cli(
        "index", "blast", repo, "--base", base,
        "--task-id", "task-schema-smoke", "--json",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["conformance"]["unexpected_scope"] == ["README.md"]
    assert "unsupported or excluded" in payload["limitations"][0]


def test_stale_graph_fails_closed(tmp_path: Path):
    repo, base = versioned_repository(tmp_path)
    build(repo)
    (repo / "src" / "service.py").write_text("class Service:\n    pass\n", encoding="utf-8")

    result = run_cli("index", "blast", repo, "--base", base, "--json")

    assert result.returncode == 1
    assert "graph is stale" in result.stderr


def test_missing_graph_fails_closed(tmp_path: Path):
    repo, base = versioned_repository(tmp_path)

    result = run_cli("index", "blast", repo, "--base", base, "--json")

    assert result.returncode == 1
    assert "missing repository graph" in result.stderr


def test_intended_dependencies_are_compared_with_observed_code_edges(tmp_path: Path):
    repo, base = versioned_repository(tmp_path)
    map_path = repo / ".go" / "repository-map.json"
    repository_map = json.loads(map_path.read_text(encoding="utf-8"))
    application = next(node for node in repository_map["nodes"] if node["id"] == "application")
    application["depends_on"] = ["tests"]
    map_path.write_text(json.dumps(repository_map, indent=2) + "\n", encoding="utf-8")
    build(repo)

    result = run_cli("index", "blast", repo, "--base", base, "--json")

    assert result.returncode == 0, result.stderr
    drift = json.loads(result.stdout)["architecture_drift"]
    assert {
        "source": "application",
        "target": "tests",
    } in drift["missing_intended_dependencies"]
    assert drift["authority"] == "comparison_only_no_automatic_rewrite"
