"""[FR-01] JSONL audit log writer.

Citations:
- SPEC.md §3 FR-08 (audit events written append-only JSONL)
- SPEC.md §3 FR-01 通過驗證 → 寫一筆 ``submit`` 稽核事件
- SPEC.md §6 NFR-03 audit ``append + fsync``
"""

from __future__ import annotations

import json
from typing import Any

from taskq_plus.config import config
from taskq_plus.storage.atomic import atomic_append_line
from taskq_plus.util import utc_now_iso


def write_event(event: dict[str, Any]) -> None:
    """Append a single JSONL event to ``$TASKQ_AUDIT_LOG``.

    Citations:
    - SPEC.md §3 FR-08: each line is one event (``event``, ``task_id``,
      ``ts`` ...).
    - SPEC.md §6 NFR-03: ``append + fsync``.
    """
    payload = dict(event)
    payload.setdefault("ts", utc_now_iso())
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
    atomic_append_line(config().audit_log, line)
