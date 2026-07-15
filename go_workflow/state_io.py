"""Crash-safe JSON writes and process-safe repository state locks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateLockError(RuntimeError):
    pass


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def atomic_move_json(source: Path, target: Path, data: dict[str, Any]) -> None:
    """Update a state record, then atomically move that exact record between queues."""
    atomic_json(source, data)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    for directory in {source.parent, target.parent}:
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ProcessFileLock:
    def __init__(self, path: Path, timeout_seconds: float = 10.0):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.handle = None
        self.recovered_stale = False

    def __enter__(self) -> "ProcessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    self.handle.seek(0)
                    owner = self.handle.read().strip() or "unknown owner"
                    self.handle.close()
                    self.handle = None
                    raise StateLockError(f"live state lock is held at {self.path}: {owner}") from exc
                time.sleep(0.02)
        self.handle.seek(0)
        raw = self.handle.read().strip()
        if raw:
            try:
                previous = json.loads(raw)
            except json.JSONDecodeError:
                previous = {}
            self.recovered_stale = previous.get("status") == "held" and not _pid_alive(int(previous.get("pid") or 0))
        metadata = {
            "schema": "go-workflow.state-lock.v1",
            "status": "held",
            "pid": os.getpid(),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "recovered_stale": self.recovered_stale,
        }
        self.handle.seek(0)
        self.handle.truncate()
        json.dump(metadata, self.handle, separators=(",", ":"))
        self.handle.flush()
        os.fsync(self.handle.fileno())
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.handle is None:
            return
        metadata = {
            "schema": "go-workflow.state-lock.v1",
            "status": "released",
            "pid": os.getpid(),
            "released_at": datetime.now(timezone.utc).isoformat(),
        }
        self.handle.seek(0)
        self.handle.truncate()
        json.dump(metadata, self.handle, separators=(",", ":"))
        self.handle.flush()
        os.fsync(self.handle.fileno())
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def repository_lock(root: Path, name: str, timeout_seconds: float = 10.0) -> ProcessFileLock:
    safe_name = "".join(character if character.isalnum() or character in "._-" else "-" for character in name)
    git_dir = root.parent / ".git"
    if git_dir.is_file():
        marker = git_dir.read_text(encoding="utf-8", errors="ignore").strip()
        candidate = marker.removeprefix("gitdir:").strip()
        git_dir = (git_dir.parent / candidate).resolve() if candidate else git_dir
    lock_root = git_dir / "go-workflow-locks" if git_dir.is_dir() else root / "locks"
    return ProcessFileLock(lock_root / f"{safe_name}.lock", timeout_seconds=timeout_seconds)


def _go_root_for(path: Path) -> Path:
    for parent in (path.parent, *path.parents):
        if parent.name == ".go":
            return parent
    return path.parent


def append_jsonl_locked(path: Path, event: dict[str, Any], timeout_seconds: float = 10.0) -> bool:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    lock = repository_lock(_go_root_for(path), f"jsonl-{digest}", timeout_seconds=timeout_seconds)
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return lock.recovered_stale
