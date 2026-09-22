"""Behavioral contracts for the repository-intelligence index."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "cli" / "go.py"), *map(str, args)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    shutil.copytree(ROOT / "fixtures" / "minimal", repo)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text(
        "from .service import Service\n\n"
        "def main() -> Service:\n"
        "    return Service()\n",
        encoding="utf-8",
    )
    (repo / "src" / "service.py").write_text(
        "class Service:\n"
        "    def run(self) -> str:\n"
        "        return 'ok'\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_app.py").write_text(
        "from src.app import main\n\n"
        "def test_main():\n"
        "    assert main().run() == 'ok'\n",
        encoding="utf-8",
    )
    return repo


def test_index_build_creates_a_local_graph_and_never_indexes_go_state(tmp_path: Path):
    repo = repository(tmp_path)

    result = run_cli("index", "build", repo, "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "go-workflow.repository-graph.v1"
    assert payload["fresh"] is True
    graph_path = repo / ".go" / "cache" / "repository-graph.json"
    assert graph_path.is_file()
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    node_ids = {node["id"] for node in graph["nodes"]}
    assert "file:src/app.py" in node_ids
    assert "symbol:src/app.py#main" in node_ids
    assert "test:tests/test_app.py" in node_ids
    assert all(".go" not in node["path"].split("/") for node in graph["nodes"] if node.get("path"))
    assert {
        "source": "file:src/app.py",
        "target": "file:src/service.py",
        "kind": "imports",
    } in graph["edges"]


def test_index_build_is_deterministic_and_maps_durable_components(tmp_path: Path):
    repo = repository(tmp_path)

    first = run_cli("index", "build", repo, "--json")
    assert first.returncode == 0, first.stderr
    graph_path = repo / ".go" / "cache" / "repository-graph.json"
    first_bytes = graph_path.read_bytes()
    second = run_cli("index", "build", repo, "--json")

    assert second.returncode == 0, second.stderr
    assert graph_path.read_bytes() == first_bytes
    graph = json.loads(first_bytes)
    node_ids = {node["id"] for node in graph["nodes"]}
    assert {"component:application", "component:tests"} <= node_ids
    assert {
        "source": "component:tests",
        "target": "component:application",
        "kind": "tests",
    } in graph["edges"]


def test_index_status_detects_source_drift(tmp_path: Path):
    repo = repository(tmp_path)
    assert run_cli("index", "build", repo, "--json").returncode == 0

    fresh = run_cli("index", "status", repo, "--json")
    assert fresh.returncode == 0
    assert json.loads(fresh.stdout)["fresh"] is True

    (repo / "src" / "service.py").write_text("class Service:\n    pass\n", encoding="utf-8")
    stale = run_cli("index", "status", repo, "--json")
    assert stale.returncode == 1
    assert json.loads(stale.stdout)["reason"] == "source_or_map_changed"


def test_index_query_returns_a_fresh_bounded_subgraph(tmp_path: Path):
    repo = repository(tmp_path)
    assert run_cli("index", "build", repo, "--json").returncode == 0

    result = run_cli("index", "query", repo, "service", "--limit", 2, "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["authority"] == "routing_aid_not_source_of_truth"
    assert payload["fresh"] is True
    assert 0 < len(payload["nodes"]) <= 2
    selected = {node["id"] for node in payload["nodes"]}
    assert all(edge["source"] in selected and edge["target"] in selected for edge in payload["edges"])


def test_index_query_refuses_stale_or_invalid_requests(tmp_path: Path):
    repo = repository(tmp_path)
    assert run_cli("index", "build", repo, "--json").returncode == 0
    (repo / "src" / "new.py").write_text("VALUE = 1\n", encoding="utf-8")

    stale = run_cli("index", "query", repo, "new", "--json")
    invalid_limit = run_cli("index", "query", repo, "new", "--limit", 0, "--json")

    assert stale.returncode == 1
    assert "graph is stale" in stale.stderr
    assert invalid_limit.returncode == 1
    assert "limit must be between" in invalid_limit.stderr


def test_validate_checks_optional_repository_map_but_keeps_it_optional(tmp_path: Path):
    repo = repository(tmp_path)
    valid = run_cli("validate", repo, "--json")
    assert valid.returncode == 0, valid.stderr

    map_path = repo / ".go" / "repository-map.json"
    repository_map = json.loads(map_path.read_text(encoding="utf-8"))
    repository_map["project"] = "other-project"
    map_path.write_text(json.dumps(repository_map), encoding="utf-8")
    invalid = run_cli("validate", repo, "--json")
    assert invalid.returncode == 1
    assert "does not match project.json" in invalid.stdout

    map_path.unlink()
    compatible = run_cli("validate", repo, "--json")
    assert compatible.returncode == 0, compatible.stderr


def test_index_emits_generic_file_nodes_and_excludes_dependencies_and_builds(tmp_path: Path):
    repo = repository(tmp_path)
    (repo / "src" / "main.go").write_text("package main\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "ignored.js").write_text("export {}\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / "ignored.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = run_cli("index", "build", repo, "--json")

    assert result.returncode == 0, result.stderr
    graph = json.loads(result.stdout)
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["file:src/main.go"]["language"] == "go"
    assert all("node_modules" not in node_id and "dist/" not in node_id for node_id in nodes)


def test_repository_map_and_graph_match_their_published_schemas(tmp_path: Path):
    repo = repository(tmp_path)
    result = run_cli("index", "build", repo, "--json")
    assert result.returncode == 0, result.stderr

    repository_map = json.loads((repo / ".go" / "repository-map.json").read_text(encoding="utf-8"))
    graph = json.loads(result.stdout)
    map_schema = json.loads((ROOT / "schemas" / "repository-map.schema.json").read_text(encoding="utf-8"))
    graph_schema = json.loads((ROOT / "schemas" / "repository-graph.schema.json").read_text(encoding="utf-8"))

    assert repository_map["schema"] == map_schema["properties"]["schema"]["const"]
    assert graph["schema"] == graph_schema["properties"]["schema"]["const"]
    assert set(map_schema["required"]) <= repository_map.keys()
    assert set(graph_schema["required"]) <= graph.keys()
    allowed_kinds = set(map_schema["properties"]["nodes"]["items"]["properties"]["kind"]["enum"])
    assert all(node["kind"] in allowed_kinds for node in repository_map["nodes"])
