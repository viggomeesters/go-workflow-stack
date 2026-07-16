"""Resolve immutable stack identity for source and PEP 610 VCS installs."""

from __future__ import annotations

import json
import re
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any, Callable


FULL_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
OFFICIAL_REPOSITORY = "github.com/viggomeesters/go-workflow-stack"


def _official_repository_url(value: object) -> bool:
    url = str(value or "").strip().lower().removeprefix("git+").rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("https://"):
        url = url.removeprefix("https://")
    elif url.startswith("ssh://git@"):
        url = url.removeprefix("ssh://git@")
    elif url.startswith("git@github.com:"):
        url = "github.com/" + url.removeprefix("git@github.com:")
    else:
        return False
    return url == OFFICIAL_REPOSITORY


def _distribution_matches_stack_root(distribution: Any, stack_root: Path) -> bool:
    for package_path in getattr(distribution, "files", None) or []:
        if str(package_path).replace("\\", "/") != "go_workflow/__init__.py":
            continue
        try:
            installed = Path(distribution.locate_file(package_path)).resolve()
        except (AttributeError, OSError, TypeError, ValueError):
            return False
        return installed == (stack_root / "go_workflow" / "__init__.py").resolve()
    return False


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
    ).stdout.strip()


def _source_checkout_identity(stack_root: Path, required_ref: str) -> dict[str, Any] | None:
    checkout_root = _git(stack_root, "rev-parse", "--show-toplevel")
    if not checkout_root or Path(checkout_root).resolve() != stack_root.resolve():
        return None

    git_head = _git(stack_root, "rev-parse", "HEAD")
    if not required_ref:
        pinned_commit = None
        exact_ref = True
    elif FULL_COMMIT_RE.fullmatch(required_ref):
        pinned_commit = required_ref
        exact_ref = git_head == required_ref
    else:
        pinned_commit = _git(stack_root, "rev-parse", "-q", "--verify", f"refs/tags/{required_ref}^{{commit}}") or None
        exact_ref = bool(pinned_commit) and git_head == pinned_commit
    return {
        "source": "git-checkout",
        "git_head": git_head or None,
        "pinned_commit": pinned_commit,
        "requested_ref": required_ref or None,
        "exact_ref": exact_ref,
    }


def _package_identity(
    stack_root: Path,
    required_ref: str,
    expected_version: str,
    distribution_lookup: Callable[[str], Any],
) -> dict[str, Any] | None:
    try:
        distribution = distribution_lookup("go-workflow-stack")
        direct_url_text = distribution.read_text("direct_url.json")
        direct_url = json.loads(direct_url_text) if direct_url_text else None
    except (metadata.PackageNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    if (
        str(getattr(distribution, "version", "")) != expected_version
        or not isinstance(direct_url, dict)
        or not _distribution_matches_stack_root(distribution, stack_root)
    ):
        return None

    if not _official_repository_url(direct_url.get("url")):
        return None
    vcs_info = direct_url.get("vcs_info")
    if not isinstance(vcs_info, dict) or vcs_info.get("vcs") != "git":
        return None
    commit_id = str(vcs_info.get("commit_id") or "")
    requested_revision = str(vcs_info.get("requested_revision") or "")
    if not FULL_COMMIT_RE.fullmatch(commit_id):
        return None
    if FULL_COMMIT_RE.fullmatch(required_ref):
        return None
    exact_ref = required_ref == f"v{expected_version}" and requested_revision == required_ref
    if not exact_ref:
        return None
    return {
        "source": "pep610-vcs",
        "git_head": None,
        "pinned_commit": commit_id,
        "requested_ref": requested_revision or None,
        "exact_ref": True,
    }


def resolve_runtime_identity(
    stack_root: Path,
    required_ref: str,
    *,
    expected_version: str,
    distribution_lookup: Callable[[str], Any] = metadata.distribution,
) -> dict[str, Any]:
    source_identity = _source_checkout_identity(stack_root, required_ref)
    if source_identity is not None:
        return source_identity
    package_identity = _package_identity(stack_root, required_ref, expected_version, distribution_lookup)
    if package_identity is not None:
        return package_identity
    return {
        "source": "unverified",
        "git_head": None,
        "pinned_commit": None,
        "requested_ref": required_ref or None,
        "exact_ref": False,
    }