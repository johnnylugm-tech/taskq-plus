"""[FR-01] ``tasks.json`` store.

Citations:
- SPEC.md §3 FR-01 原子寫入 ``$TASKQ_HOME/tasks.json``
- SPEC.md §5 環境變數 row ``tasks.json``: ``{version:1,
  tasks:{id→全欄位含 depends_on}}``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from taskq_plus.storage.atomic import atomic_write_json


class TaskStore:
    """Wrapper around ``tasks.json``.

    In-memory shape is ``{task_id: task_record}``; on disk it is
    ``{"version": 1, "tasks": {task_id: task_record}}``.
    """

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        """Return in-memory task mapping; empty on absent / unparseable."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        # canonical envelope: {"version": 1, "tasks": {id: record}}
        inner = data.get("tasks")
        if isinstance(inner, dict):
            return {k: v for k, v in inner.items() if isinstance(v, dict)}
        # tolerate old/flat shape: {id: record}
        if all(isinstance(v, dict) for v in data.values()):
            return data  # type: ignore[return-value]
        return {}

    def save(self, tasks: dict[str, dict[str, Any]]) -> None:
        """Atomic replace with on-disk ``{version, tasks}`` envelope."""
        atomic_write_json(self.path, {"version": 1, "tasks": tasks})
