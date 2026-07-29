"""[FR-03] ``$TASKQ_HOME/breaker.json`` envelope store.

Citations:
- SPEC.md §3 FR-03 line 115: 狀態持久化於 ``$TASKQ_HOME/breaker.json``
  (原子寫).
- SAD §3.4: envelope is ``{"version": 1, "state": str,
  "failure_count": int, "opened_at": float | None}`` where ``state``
  is one of ``"CLOSED" | "OPEN" | "HALF_OPEN"`` and ``opened_at`` is
  POSIX epoch seconds (``time.time()``).
- SPEC.md §6 NFR-03: atomic JSON write via ``atomic_write_json``
  (tmp + ``os.replace``).
- SPEC.md §5 環境變數 row ``breaker.json`` line 312.
"""

from __future__ import annotations

import json
from typing import Any

from taskq_plus.config import config
from taskq_plus.storage.atomic import atomic_write_json


# Canonical envelope defaults (SAD §3.4). The breaker is a global,
# cross-process, cross-task state machine — the "no file yet" state is
# implicitly ``CLOSED`` with a zero counter and no cooldown clock.
DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "state": "CLOSED",
    "failure_count": 0,
    "opened_at": None,
}

# Breaker state machine alphabet (SPEC.md §3 FR-03 line 110-115). Any
# value outside this set on disk is clamped to ``CLOSED`` so a corrupted
# envelope can never wedge the breaker.
_VALID_STATES: frozenset[str] = frozenset({"CLOSED", "OPEN", "HALF_OPEN"})


def _path():
    """Return the canonical breaker.json path under ``$TASKQ_HOME``."""
    return config().task_home / "breaker.json"


def load() -> dict[str, Any]:
    """Return the breaker envelope; defaults when absent or unparseable.

    Citations:
    - SAD §3.4: a missing / unreadable breaker file is treated as the
      canonical initial state (CLOSED, count 0, no opened_at) so the
      first ever ``run`` is always admitted.
    """
    path = _path()
    if not path.exists():
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_STATE)
    if not isinstance(data, dict):
        return dict(DEFAULT_STATE)
    state = data.get("state", "CLOSED")
    if state not in _VALID_STATES:
        state = "CLOSED"
    return {
        "version": 1,
        "state": str(state),
        "failure_count": int(data.get("failure_count", 0) or 0),
        "opened_at": data.get("opened_at"),
    }


def save(state: dict[str, Any]) -> None:
    """Atomically persist the breaker envelope to ``breaker.json``.

    Citations:
    - AC-FR-03.2 / NFR-03: single ``atomic_write_json`` call per
      state transition so a concurrent reader never sees a partial
      JSON document.
    """
    payload = {
        "version": 1,
        "state": str(state.get("state", "CLOSED")),
        "failure_count": int(state.get("failure_count", 0) or 0),
        "opened_at": state.get("opened_at"),
    }
    atomic_write_json(_path(), payload)
