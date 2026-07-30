"""Task persistence store — atomic JSON writes.

[FR-01]
Citations: SPEC.md §3 FR-01 (atomic write to $TASKQ_HOME/tasks.json).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional


TASKS_FILENAME = "tasks.json"
UTC = dt.timezone.utc

# Task statuses that occupy a name slot — finished tasks release theirs.
ACTIVE_STATUSES: FrozenSet[str] = frozenset({"pending", "running"})


def _home() -> Path:
    """Resolve the TASKQ_HOME directory (env override)."""
    return Path(os.environ.get("TASKQ_HOME", ".")).resolve()


def tasks_path() -> Path:
    """Return the tasks.json path under TASKQ_HOME."""
    return _home() / TASKS_FILENAME


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
    except Exception:
        # Best-effort cleanup of the temp file on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
    """Append a fully-formed task record to disk atomically."""
    task.setdefault("created_at", _now_iso())
    tasks = load_tasks()
    tasks.append(task)
    save_tasks(tasks)
    return task
