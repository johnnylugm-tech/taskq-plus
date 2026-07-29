"""[FR-01] Atomic file writers.

Citations:
- SPEC.md §6 NFR-03: ``tasks.json`` / ``breaker.json`` /
  ``cache.json`` are tmp + ``os.replace`` for atomicity.
- SPEC.md §6 NFR-03: ``audit.jsonl`` is ``append + fsync`` JSONL.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically rewrite ``path`` with JSON-serialised ``payload``.

    Writes to a sibling tmp file, fsyncs, then ``os.replace``s.
    Citations: SPEC.md §6 NFR-03 (tmp + os.replace).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_append_line(path: Path, line: str) -> None:
    """Append ``line`` to ``path`` with fsync.

    Citations: SPEC.md §6 NFR-03 (audit append + fsync).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
