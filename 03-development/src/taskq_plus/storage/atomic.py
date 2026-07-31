"""Atomic file helpers — `taskq_plus.storage.atomic` (NFR-03).

Single source of truth for atomic JSON / JSONL writes used by every store
under `storage/`. Each store calls `read_json` / `write_json_atomic` /
`append_jsonl` instead of inlining the tmp-file + `os.replace` pattern.

[FR-01] [FR-03] [NFR-03]
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, List


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON atomically: tmp file in same dir, then `os.replace`.

    A reader observes either the previous complete JSON or the new complete
    JSON; never a partial write.

    [NFR-03]
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp_name, path)
    finally:
        # Cleanup runs on success too: `os.replace` moved tmp_name away, so
        # unlink hits FileNotFoundError (a subclass of OSError) — swallowed.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def read_json(path: Path) -> Any:
    """Read JSON from `path`; return None when missing or unreadable.

    [NFR-03]
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def append_jsonl(path: Path, entry: Any) -> None:
    """Append one JSONL record (`json.dumps` + `\\n`) to `path`.

    [NFR-03]
    Citations: SPEC.md#L314 (`audit.jsonl` append + fsync).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # fsync rarely fails on regular files; ignore and continue.
            pass


def read_jsonl(path: Path) -> List[Any]:
    """Parse a JSONL file into a list (skipping blank lines).

    [NFR-03]
    """
    if not path.exists():
        return []
    out: List[Any] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out