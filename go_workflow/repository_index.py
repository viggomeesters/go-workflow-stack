"""Deterministic repository intelligence derived from source code.

The committed repository map supplies stable semantic identity.  The detailed
graph is a local materialized view and is never canonical workflow state.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .state_io import atomic_json


MAP_SCHEMA = "go-workflow.repository-map.v1"
GRAPH_SCHEMA = "go-workflow.repository-graph.v1"
GRAPH_RELATIVE_PATH = Path(".go/cache/repository-graph.json")
SOURCE_EXTENSIONS = {
    ".c", ".cc", ".clj", ".cpp", ".cs", ".dart", ".ex", ".exs", ".go",
    ".h", ".hpp", ".java", ".js", ".jsx", ".kt", ".kts", ".lua", ".m",
    ".ml", ".mli", ".nix", ".php", ".py", ".r", ".rb", ".rs", ".scala",
    ".sol", ".swift", ".ts", ".tsx", ".vue", ".zig",
}
SKIP_DIRECTORIES = {
    ".git", ".go", ".hg", ".svn", ".venv", "__pycache__", "build",
    "coverage", "dist", "node_modules", "out", "target", "vendor", "venv",
}


class RepositoryIndexError(ValueError):
    """Expected repository-index contract failure."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_json(value))


def repository_map_path(repo: Path) -> Path:
    return repo.resolve() / ".go" / "repository-map.json"


def graph_path(repo: Path) -> Path:
    return repo.resolve() / GRAPH_RELATIVE_PATH


def load_repository_map(repo: Path, *, required: bool = False) -> dict[str, Any] | None:
    path = repository_map_path(repo)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise RepositoryIndexError(f"missing durable repository map: {path}")
        return None
    except json.JSONDecodeError as exc:
        raise RepositoryIndexError(f"invalid JSON in {path}: {exc}") from exc
    findings = validate_repository_map(value, str(path), project=_project_id(repo))
    if findings:
        raise RepositoryIndexError("; ".join(findings))
    return value


def validate_repository_map(value: Any, rel: str = ".go/repository-map.json", *, project: str = "") -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{rel}: root must be an object"]
    if value.get("schema") != MAP_SCHEMA:
        errors.append(f"{rel}: schema mismatch")
    if value.get("kind") != "repository_map":
        errors.append(f"{rel}: kind must be repository_map")
    if not isinstance(value.get("project"), str) or not value.get("project"):
        errors.append(f"{rel}: project required")
    elif project and value.get("project") != project:
        errors.append(f"{rel}: project {value.get('project')!r} does not match project.json id {project!r}")
    nodes = value.get("nodes")
    if not isinstance(nodes, list):
        return [*errors, f"{rel}: nodes must be a list"]
    seen: set[str] = set()
    for index, node in enumerate(nodes, 1):
        prefix = f"{rel}: node {index}"
        if not isinstance(node, dict):
            errors.append(f"{prefix} must be an object")
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append(f"{prefix} id required")
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", node_id):
            errors.append(f"{prefix} id is invalid")
        elif node_id in seen:
            errors.append(f"{prefix} duplicate id {node_id!r}")
        else:
            seen.add(node_id)
        if node.get("kind") not in {"application", "component", "service", "library", "data", "interface", "test_suite"}:
            errors.append(f"{prefix} has invalid kind")
        if not isinstance(node.get("summary"), str) or not node.get("summary", "").strip():
            errors.append(f"{prefix} summary required")
        paths = node.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path and not Path(path).is_absolute() and ".." not in Path(path).parts for path in paths):
            errors.append(f"{prefix} paths must contain safe repo-relative globs")
        for field in ("depends_on", "tests"):
            refs = node.get(field, [])
            if not isinstance(refs, list) or not all(isinstance(ref, str) and ref for ref in refs):
                errors.append(f"{prefix} {field} must be a string list")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for field in ("depends_on", "tests"):
            for ref in node.get(field, []):
                if ref not in seen:
                    errors.append(f"{rel}: node {node.get('id')!r} {field} references unknown node {ref!r}")
    overlays = value.get("overlays", [])
    if not isinstance(overlays, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("node_id"), str)
        and item.get("node_id") in seen
        and isinstance(item.get("notes"), list)
        and all(isinstance(note, str) and note.strip() for note in item["notes"])
        for item in overlays
    ):
        errors.append(f"{rel}: overlays must contain known node_id plus non-empty notes")
    return errors


def _git_files(repo: Path) -> list[str] | None:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return sorted({item for item in result.stdout.split("\0") if item})


def _filesystem_files(repo: Path) -> list[str]:
    return sorted(str(path.relative_to(repo)).replace("\\", "/") for path in repo.rglob("*") if path.is_file())


def source_paths(repo: Path) -> list[str]:
    repo = repo.resolve()
    candidates = _git_files(repo)
    if candidates is None:
        candidates = _filesystem_files(repo)
    selected: list[str] = []
    for relative in candidates:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            continue
        if any(part.startswith(".") or part in SKIP_DIRECTORIES for part in path.parts):
            continue
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        target = repo / path
        if target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(repo):
            selected.append(path.as_posix())
    return sorted(dict.fromkeys(selected))


def _language(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return {"js": "javascript", "jsx": "javascript", "ts": "typescript", "tsx": "typescript", "py": "python"}.get(suffix, suffix)


def _is_test(path: str) -> bool:
    value = Path(path)
    return (
        any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in value.parts[:-1])
        or value.name.startswith("test_")
        or value.stem.endswith(("_test", ".test", ".spec"))
    )


def _matches(path: str, pattern: str) -> bool:
    clean = pattern.strip("/")
    return fnmatch.fnmatchcase(path, clean) or path == clean or path.startswith(clean.rstrip("/*") + "/")


def component_for(path: str, repository_map: dict[str, Any] | None) -> tuple[str, str]:
    if repository_map:
        for node in repository_map.get("nodes", []):
            if any(_matches(path, pattern) for pattern in node.get("paths", [])):
                return str(node["id"]), "durable"
    top = Path(path).parts[0] if Path(path).parts else "root"
    return f"inferred:{top}", "inferred"


def _python_nodes(path: str, source: str) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    imports: list[str] = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return nodes, edges, imports

    def visit(body: Iterable[ast.stmt], owners: list[str]) -> None:
        for item in body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = ".".join([*owners, item.name])
                node_id = f"symbol:{path}#{qualified}"
                kind = "class" if isinstance(item, ast.ClassDef) else ("method" if owners else "function")
                nodes.append({
                    "id": node_id,
                    "kind": kind,
                    "name": qualified,
                    "path": path,
                    "start_line": int(item.lineno),
                    "end_line": int(getattr(item, "end_lineno", item.lineno)),
                })
                edges.append({"source": f"file:{path}", "target": node_id, "kind": "contains"})
                visit(item.body, [*owners, item.name])
            elif isinstance(item, ast.Import):
                imports.extend(alias.name for alias in item.names)
            elif isinstance(item, ast.ImportFrom):
                imports.append("." * item.level + (item.module or ""))

    visit(tree.body, [])
    return nodes, edges, imports


def _resolve_python_import(source_path: str, module: str, available: set[str]) -> str | None:
    if module.startswith("."):
        level = len(module) - len(module.lstrip("."))
        base = Path(source_path).parent
        for _ in range(max(0, level - 1)):
            base = base.parent
        relative = module.lstrip(".").replace(".", "/")
        local = (base / relative).with_suffix(".py").as_posix()
        package_local = (base / relative / "__init__.py").as_posix()
        if local in available:
            return local
        if package_local in available:
            return package_local
        return None
    candidate = module.replace(".", "/") + ".py"
    package_candidate = module.replace(".", "/") + "/__init__.py"
    if candidate in available:
        return candidate
    if package_candidate in available:
        return package_candidate
    return None


def build_graph(repo: Path, *, write: bool = True) -> dict[str, Any]:
    repo = repo.resolve()
    repository_map = load_repository_map(repo)
    paths = source_paths(repo)
    available = set(paths)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    sources: list[dict[str, Any]] = []
    component_ids: set[str] = set()
    pending_imports: list[tuple[str, str]] = []

    for path in paths:
        data = (repo / path).read_bytes()
        sha256 = digest_bytes(data)
        language = _language(path)
        component_id, component_source = component_for(path, repository_map)
        component_ids.add(component_id)
        node_id = f"test:{path}" if _is_test(path) else f"file:{path}"
        sources.append({"path": path, "sha256": sha256, "bytes": len(data), "language": language})
        nodes.append({
            "id": node_id,
            "kind": "test" if _is_test(path) else "file",
            "name": Path(path).name,
            "path": path,
            "component": component_id,
            "sha256": sha256,
            "language": language,
        })
        edges.append({"source": f"component:{component_id}", "target": node_id, "kind": "contains"})
        if language == "python":
            try:
                source = data.decode("utf-8")
            except UnicodeDecodeError:
                source = ""
            symbol_nodes, symbol_edges, imports = _python_nodes(path, source)
            # Test files use a test node ID rather than file:; keep containment exact.
            if node_id.startswith("test:"):
                for edge in symbol_edges:
                    if edge["source"] == f"file:{path}":
                        edge["source"] = node_id
            for node in symbol_nodes:
                node["component"] = component_id
            nodes.extend(symbol_nodes)
            edges.extend(symbol_edges)
            pending_imports.extend((path, module) for module in imports)

    map_nodes = {str(node["id"]): node for node in (repository_map or {}).get("nodes", [])}
    component_ids.update(map_nodes)
    for component_id in sorted(component_ids):
        durable = map_nodes.get(component_id)
        nodes.append({
            "id": f"component:{component_id}",
            "kind": "component",
            "name": component_id,
            "path": "",
            "summary": str((durable or {}).get("summary") or "Inferred source component"),
            "origin": "durable" if durable else "inferred",
        })
    for component_id, node in sorted(map_nodes.items()):
        for dependency in node.get("depends_on", []):
            edges.append({
                "source": f"component:{component_id}",
                "target": f"component:{dependency}",
                "kind": "depends_on",
            })
        for test_suite in node.get("tests", []):
            edges.append({
                "source": f"component:{test_suite}",
                "target": f"component:{component_id}",
                "kind": "tests",
            })
    for source_path, module in pending_imports:
        target = _resolve_python_import(source_path, module, available)
        if target:
            source_id = f"test:{source_path}" if _is_test(source_path) else f"file:{source_path}"
            target_id = f"test:{target}" if _is_test(target) else f"file:{target}"
            edges.append({"source": source_id, "target": target_id, "kind": "imports"})

    nodes.sort(key=lambda item: item["id"])
    edges = sorted({(edge["source"], edge["target"], edge["kind"]) for edge in edges})
    edge_values = [{"source": source, "target": target, "kind": kind} for source, target, kind in edges]
    source_digest = digest_json(sources)
    map_digest = digest_json(repository_map) if repository_map is not None else None
    graph = {
        "schema": GRAPH_SCHEMA,
        "kind": "repository_graph",
        "project": str((repository_map or {}).get("project") or _project_id(repo)),
        "authority": "derived_from_source",
        "fresh": True,
        "source_digest": source_digest,
        "map_digest": map_digest,
        "sources": sources,
        "nodes": nodes,
        "edges": edge_values,
        "coverage": {
            "languages": sorted({item["language"] for item in sources}),
            "files": len(sources),
            "nodes": len(nodes),
            "edges": len(edge_values),
            "limitations": [
                "Generic languages expose file/component/test topology only.",
                "Python imports are resolved only when the target module is present in the repository.",
                "Dynamic runtime dependencies may be absent.",
            ],
        },
    }
    if write:
        target = graph_path(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(target, graph)
    return graph


def _project_id(repo: Path) -> str:
    try:
        value = json.loads((repo / ".go" / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return repo.name
    return str(value.get("id") or repo.name)


def load_graph(repo: Path, *, require_fresh: bool = True) -> dict[str, Any]:
    path = graph_path(repo)
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepositoryIndexError(f"missing repository graph: run `go index build {repo}`") from exc
    except json.JSONDecodeError as exc:
        raise RepositoryIndexError(f"invalid repository graph: {exc}") from exc
    findings = validate_repository_graph(graph)
    if findings:
        raise RepositoryIndexError("invalid repository graph; rebuild the index: " + "; ".join(findings))
    status = index_status(repo, graph=graph)
    graph["fresh"] = status["fresh"]
    if require_fresh and not status["fresh"]:
        raise RepositoryIndexError("repository graph is stale; run `go index build`")
    return graph


def validate_repository_graph(value: Any, rel: str = ".go/cache/repository-graph.json") -> list[str]:
    if not isinstance(value, dict):
        return [f"{rel}: root must be an object"]
    errors: list[str] = []
    if value.get("schema") != GRAPH_SCHEMA:
        errors.append(f"{rel}: schema mismatch")
    if value.get("kind") != "repository_graph" or value.get("authority") != "derived_from_source":
        errors.append(f"{rel}: graph kind/authority mismatch")
    if not isinstance(value.get("project"), str) or not value.get("project"):
        errors.append(f"{rel}: project required")
    if not isinstance(value.get("source_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["source_digest"]):
        errors.append(f"{rel}: source_digest must be sha256")
    sources = value.get("sources")
    nodes = value.get("nodes")
    edges = value.get("edges")
    if not isinstance(sources, list) or not isinstance(nodes, list) or not isinstance(edges, list):
        return [*errors, f"{rel}: sources, nodes and edges must be lists"]
    node_ids = [node.get("id") for node in nodes if isinstance(node, dict)]
    if len(node_ids) != len(nodes) or any(not isinstance(node_id, str) or not node_id for node_id in node_ids):
        errors.append(f"{rel}: every node requires an id")
    elif len(node_ids) != len(set(node_ids)):
        errors.append(f"{rel}: duplicate node id")
    known = set(node_ids)
    for index, edge in enumerate(edges, 1):
        if not isinstance(edge, dict) or set(edge) != {"source", "target", "kind"}:
            errors.append(f"{rel}: edge {index} is invalid")
        elif edge["source"] not in known or edge["target"] not in known:
            errors.append(f"{rel}: edge {index} references an unknown node")
    return errors


def index_status(repo: Path, *, graph: dict[str, Any] | None = None) -> dict[str, Any]:
    repo = repo.resolve()
    if graph is None:
        try:
            graph = json.loads(graph_path(repo).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": "go-workflow.repository-index-status.v1", "present": False, "fresh": False, "reason": "missing"}
        except json.JSONDecodeError:
            return {"schema": "go-workflow.repository-index-status.v1", "present": True, "fresh": False, "reason": "invalid_json"}
    current_sources = []
    for path in source_paths(repo):
        data = (repo / path).read_bytes()
        current_sources.append({"path": path, "sha256": digest_bytes(data), "bytes": len(data), "language": _language(path)})
    current_map = load_repository_map(repo)
    source_digest = digest_json(current_sources)
    map_digest = digest_json(current_map) if current_map is not None else None
    fresh = graph.get("source_digest") == source_digest and graph.get("map_digest") == map_digest
    return {
        "schema": "go-workflow.repository-index-status.v1",
        "present": True,
        "fresh": fresh,
        "reason": "fresh" if fresh else "source_or_map_changed",
        "source_digest": source_digest,
        "indexed_source_digest": graph.get("source_digest"),
        "map_digest": map_digest,
        "indexed_map_digest": graph.get("map_digest"),
        "graph_path": str(GRAPH_RELATIVE_PATH),
    }


def query_graph(repo: Path, query: str, *, limit: int = 20) -> dict[str, Any]:
    if not query.strip():
        raise RepositoryIndexError("query must not be empty")
    if limit < 1 or limit > 500:
        raise RepositoryIndexError("limit must be between 1 and 500")
    graph = load_graph(repo)
    terms = [term for term in query.lower().replace("/", " ").replace("_", " ").split() if term]

    def score(node: dict[str, Any]) -> tuple[int, str]:
        haystack = " ".join(str(node.get(key) or "") for key in ("id", "name", "path", "summary", "component")).lower()
        return (sum(2 if term in str(node.get("id", "")).lower() else 1 for term in terms if term in haystack), str(node["id"]))

    ranked = [(score(node), node) for node in graph["nodes"]]
    selected = [node for (rank, _), node in sorted(ranked, key=lambda item: (-item[0][0], item[0][1])) if rank > 0][:limit]
    selected_ids = {node["id"] for node in selected}
    edges = [edge for edge in graph["edges"] if edge["source"] in selected_ids and edge["target"] in selected_ids]
    return {
        "schema": "go-workflow.repository-query.v1",
        "query": query,
        "fresh": True,
        "authority": "routing_aid_not_source_of_truth",
        "nodes": selected,
        "edges": edges,
        "limit": limit,
    }
