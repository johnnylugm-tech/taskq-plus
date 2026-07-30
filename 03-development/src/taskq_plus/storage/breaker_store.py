"""Circuit-breaker state persistence — atomic JSON writes to breaker.json.

[FR-03]
Citations:
  - SPEC.md#L115 (狀態持久化於 `$TASKQ_HOME/breaker.json`(原子寫)).
  - SPEC.md#L312 (breaker.json = `{version:1, state, failure_count, opened_at}`).
  - SPEC.md#L206 (NFR-03: 四個資料檔全部原子寫 — tmp + `os.replace`).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


BREAKER_FILENAME = "breaker.json"


def _home() -> Path:
    """Resolve the TASKQ_HOME directory (env override, default cwd).

    [FR-03]
    Citations: SPEC.md#L294 (`TASKQ_HOME` 資料檔目錄).
    """
    return Path(os.environ.get("TASKQ_HOME", ".")).resolve()


def breaker_path() -> Path:
    """Return the breaker.json path under TASKQ_HOME.

    [FR-03]
    Citations: SPEC.md#L115 (狀態持久化於 `$TASKQ_HOME/breaker.json`).
    """
    return _home() / BREAKER_FILENAME


def write_breaker(record: Dict[str, Any]) -> None:
    """Persist the breaker record atomically (tmp file + `os.replace`).

    The temp file is created in the destination directory so `os.replace` is a
    same-filesystem rename: a reader either sees the previous complete JSON or
    the new complete JSON, never a partial write.

    [FR-03] [NFR-03]
    Citations:
      - SPEC.md#L115 (breaker.json 原子寫).
      - SPEC.md#L206 (tmp + `os.replace`;進程中斷後檔案仍為合法 JSON).
    """
    path = breaker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False)
    os.replace(tmp_name, path)


def read_breaker() -> Optional[Dict[str, Any]]:
    """Return the persisted breaker record, or None when absent/unreadable.

    A missing breaker.json is the normal cold-start case (breaker CLOSED), so it
    is reported as None rather than raised.

    [FR-03]
    Citations:
      - SPEC.md#L112 (跨進程連續失敗計數 — read-modify-write of breaker.json).
      - SPEC.md#L312 (breaker.json 內容).
    """
    path = breaker_path()
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(data, dict):
        return data
    return None
