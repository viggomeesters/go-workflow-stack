"""Read-only task state paths and queries shared by workflow frontends."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def task_path(root: Path, status: str, task_id: str) -> Path:
    return root / "tasks" / status / f"{task_id}.json"


def _load_task(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"task JSON root must be an object: {path}")
    return data


def open_task_records(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    tasks = [(path, _load_task(path)) for path in sorted((root / "tasks" / "open").glob("*.json"))]
    return sorted(tasks, key=lambda item: (item[1].get("order", 999999), item[0].name))


def unfinished_task_ids(root: Path) -> dict[str, list[str]]:
    return {
        state: [str(_load_task(path).get("id") or path.stem) for path in sorted((root / "tasks" / state).glob("*.json"))]
        for state in ("active", "blocked")
    }


def pending_review_task_ids(root: Path) -> list[str]:
    pending: list[str] = []
    for path in sorted((root / "tasks" / "done").glob("*.json")):
        task = _load_task(path)
        if task.get("work_status") == "completed" and task.get("review_status") in {"review", "needs_fix"}:
            pending.append(str(task.get("id") or path.stem))
    return pending
