"""[FR-01] Environment-backed runtime configuration.

Citations:
- SPEC.md §5 環境變數 lines 304-318 (``TASKQ_HOME``,
  ``TASKQ_AUDIT_LOG``, ...)
- SPEC.md §6 套件佈局: ``config.py`` 為 independence 模組
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TaskqConfig:
    """Resolved runtime paths and tunables for FR-01.

    Citations:
    - SPEC.md §5 環境變數 table lines 304-318
    """

    task_home: Path
    audit_log: Path


def config() -> TaskqConfig:
    """Build a snapshot of the process environment.

    Citations:
    - ``TASKQ_HOME`` defaults to ``./.taskq`` per SPEC.md §5.
    - ``TASKQ_AUDIT_LOG`` defaults to ``$TASKQ_HOME/audit.jsonl``
      per SPEC.md §5 row ``TASKQ_AUDIT_LOG``.
    """
    home = Path(os.environ.get("TASKQ_HOME", "./.taskq")).resolve()
    audit_log = Path(
        os.environ.get("TASKQ_AUDIT_LOG", str(home / "audit.jsonl"))
    )
    home.mkdir(parents=True, exist_ok=True)
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    return TaskqConfig(task_home=home, audit_log=audit_log)
