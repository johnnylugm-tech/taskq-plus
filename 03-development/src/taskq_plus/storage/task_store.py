"""Task persistence store — atomic JSON writes.

[FR-01]
Citations: SPEC.md §3 FR-01 (atomic write to $TASKQ_HOME/tasks.json).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterator, List, Optional


TASKS_FILENAME = "tasks.json"
LOCK_FILENAME = "tasks.json.lock"
UTC = dt.timezone.utc

# Task statuses that occupy a name slot — finished tasks release theirs.
ACTIVE_STATUSES: FrozenSet[str] = frozenset({"pending", "running"})


def _home() -> Path:
    """Resolve the TASKQ_HOME directory (env override)."""
    return Path(os.environ.get("TASKQ_HOME", ".")).resolve()


def tasks_path() -> Path:
    """Return the tasks.json path under TASKQ_HOME."""
    return _home() / TASKS_FILENAME


def lock_path() -> Path:
    """Return the advisory-lock path guarding tasks.json read-modify-write."""
    return _home() / LOCK_FILENAME


@contextmanager
def _store_write_lock() -> Iterator[None]:
    """Hold an exclusive inter-process lock over the tasks.json mutation.

    Uses `fcntl.flock` on a sidecar lock file (POSIX). The lock is advisory
    and process-scoped, which is exactly the FR-01 requirement: concurrent
    `submit` invocations serialise their load → append → save cycles instead
    of racing and losing records.

    [FR-01] [NFR-03]
    Citations: SPEC.md#L206 (並發寫入不得遺失已記錄狀態).
    """
    import fcntl

    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _now_iso() -> str:
    """UTC ISO-8601 timestamp with 'Z' suffix."""
    return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically: write to temp file in same dir, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp_name, path)
    finally:
        # Cleanup on both paths: success (already moved away — unlink 404s
        # and is swallowed) and failure (real leftover temp file is removed).
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def load_tasks() -> List[Dict[str, Any]]:
    """Read all tasks from disk; returns [] when file is missing or empty."""
    p = tasks_path()
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        return data["tasks"]
    return []


def save_tasks(tasks: List[Dict[str, Any]]) -> None:
    """Persist tasks atomically."""
    _atomic_write_json(tasks_path(), tasks)


def find_by_name(name: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the first active task with a matching name (case-sensitive)."""
    if name is None:
        return None
    for t in load_tasks():
        if t.get("name") == name and t.get("status") in ACTIVE_STATUSES:
            return t
    return None


def find_by_id(task_id: str) -> Optional[Dict[str, Any]]:
    """Return the task with the given id, or None."""
    for t in load_tasks():
        if t.get("id") == task_id:
            return t
    return None


def append_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Append a fully-formed task record to disk atomically.

    The load → append → save cycle runs under an inter-process file lock, so
    two concurrent `submit` processes cannot both read the same base list and
    have the second `os.replace` discard the first one's record (NFR-03: 並發
    寫入不得遺失已記錄狀態).

    [FR-01] [NFR-03]
    Citations: SPEC.md#L206 (資料檔原子寫;並發寫入後已記錄狀態不遺失).
    """
    task.setdefault("created_at", _now_iso())
    with _store_write_lock():
        tasks = load_tasks()
        tasks.append(task)
        save_tasks(tasks)
    return task
