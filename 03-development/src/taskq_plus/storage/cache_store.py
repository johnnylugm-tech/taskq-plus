"""[FR-04] ``$TASKQ_HOME/cache.json`` envelope store.

Citations:
- SPEC.md §3 FR-04 lines 116-122 (快取讀寫 原子 + 執行緒安全).
- SAD §3.4: envelope is ``{"version": 1, "entries":
  {sha256: {"result": dict, "cached_at": float}}}`` where
  ``cached_at`` is POSIX epoch seconds (``time.time()``).
- SPEC.md §6 NFR-03: tmp + ``os.replace`` via ``atomic_write_json``.
- SPEC.md §5 環境變數 row ``cache.json``.
- TEST_SPEC.md FR-04 ACs AC-FR-04.3 / AC-FR-04.4.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from taskq_plus.config import config
from taskq_plus.storage.atomic import atomic_write_json


# AC-FR-04.4 / SPEC.md §3 FR-04: concurrent ``service.cache.store``
# calls (from FR-02's ``ThreadPoolExecutor``) serialise their
# read-modify-write on this module-level ``threading.Lock`` so no
# entry is lost and ``cache.json`` stays parseable.
_lock = threading.Lock()


def _path() -> Path:
    """Return the canonical cache.json path under ``$TASKQ_HOME``."""
    return config().task_home / "cache.json"


def _read_envelope() -> dict[str, Any]:
    """Return the raw on-disk envelope; default when absent / unparseable.

    Citations:
    - SAD §3.4: missing file = ``{"version": 1, "entries": {}}``.
    """
    path = _path()
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "entries": {}}
    if not isinstance(data, dict):
        return {"version": 1, "entries": {}}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    return {"version": 1, "entries": entries}


def load() -> dict[str, dict[str, Any]]:
    """Return the ``entries`` mapping; empty on absent / unparseable file.

    Citations:
    - SAD §3.4: the on-disk shape is ``{version, entries}``; this
      returns only ``entries`` for the caller's convenience.
    """
    return _read_envelope().get("entries", {})


def get(sig: str) -> dict[str, Any] | None:
    """Return the stored envelope ``{result, cached_at}`` for ``sig``.

    Citations: SAD §3.4 — entry shape.
    """
    return load().get(sig)


def put(sig: str, result: dict[str, Any]) -> None:
    """Atomically write ``{sig: {result, cached_at}}`` to ``cache.json``.

    Citations:
    - AC-FR-04.3: writes are atomic via ``atomic_write_json`` (tmp +
      ``os.replace``).
    - AC-FR-04.4 / NFR-03: module-level ``threading.Lock`` serialises
      concurrent writers from FR-02's pool so no entry is lost.
    - SAD §3.4: ``cached_at`` is POSIX epoch seconds (``time.time()``).
    """
    path = _path()
    with _lock:
        data = _read_envelope()
        entries = data.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            data["entries"] = entries
        entries[sig] = {"result": result, "cached_at": time.time()}
        atomic_write_json(path, data)