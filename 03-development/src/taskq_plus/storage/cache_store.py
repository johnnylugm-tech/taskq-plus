"""Cache persistence — atomic JSON reads/writes of cache.json.

[FR-04] [NFR-03]
Citations:
  - SPEC.md §3 FR-04 ($TASKQ_HOME/cache.json: ttl-keyed replay entries).
  - SPEC.md §3 FR-04 (atomic + thread-safe; coexists with FR-02 concurrency).
  - SPEC.md §8 NFR-03 (cache.json is one of the four atomic-write data files:
    tmp + os.replace in the same directory; a reader never observes a
    partial / unparseable document).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional


CACHE_FILENAME = "cache.json"

# Module-level Lock serialises read-modify-write inside the cache service so
# concurrent writers cannot lose each other's entries. The on-disk atomic
# write (tmp + os.replace) keeps the document valid regardless; the Lock is
# the merging safety net for callers that must upsert under contention.
_cache_lock = threading.Lock()


def _home() -> Path:
    """Resolve the TASKQ_HOME directory (env override, default cwd).

    [FR-04]
    Citations: SPEC.md §3 FR-04 (cache.json lives under $TASKQ_HOME).
    """
    return Path(os.environ.get("TASKQ_HOME", ".")).resolve()


def cache_path() -> Path:
    """Return the cache.json path under TASKQ_HOME.

    [FR-04]
    Citations: SPEC.md §3 FR-04 ($TASKQ_HOME/cache.json).
    """
    return _home() / CACHE_FILENAME


def write_cache(record: Dict[str, Any]) -> None:
    """Persist the cache record atomically (tmp + os.replace).

    The temp file is created in the destination directory so `os.replace` is
    a same-filesystem rename: a reader either sees the previous complete
    JSON or the new complete JSON, never a partial write.

    [FR-04] [NFR-03]
    Citations:
      - SPEC.md §3 FR-04 (atomic write of cache.json).
      - SPEC.md §8 NFR-03 (tmp + os.replace; the file on disk is always a
        valid JSON document, even mid-process interruption).
    """
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
        os.replace(tmp_name, path)
    finally:
        # Best-effort cleanup of the temp file on both success (already moved
        # away — unlink 404s, swallowed) and failure (real leftover removed).
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def read_cache() -> Optional[Dict[str, Any]]:
    """Return the persisted cache record, or None when missing/unreadable.

    A missing cache.json is the normal cold-start case (no cached results
    yet), so it is reported as None rather than raised. A corrupt / truncated
    file is also None: the atomic-write guarantee means this only happens
    after a crash mid-rename, in which case re-running will simply rebuild.

    [FR-04] [NFR-03]
    Citations:
      - SPEC.md §3 FR-04 (cache.json read path).
      - SPEC.md §8 NFR-03 (single valid JSON document at all times).
    """
    path = cache_path()
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