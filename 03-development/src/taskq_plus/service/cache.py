"""TTL cache service — sha256(command)-keyed replay within TASKQ_CACHE_TTL.

[FR-04]
Citations:
  - SPEC.md §3 FR-04 (cache signature = sha256(command)).
  - SPEC.md §3 FR-04 (within TTL → directly replay, no subprocess;
    `cached: true`).
  - SPEC.md §3 FR-04 (miss/expired → normal execution; on `done` write
    $TASKQ_HOME/cache.json).
  - SPEC.md §3 FR-04 (atomic + thread-safe; coexists with FR-02 concurrency).
  - SPEC.md §8 NFR-03 (cache.json atomic write).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from taskq_plus.service.executor import run_with_retry as exec_run_with_retry
from taskq_plus.storage.cache_store import (
    cache_path,
    read_cache,
    write_cache,
)
from taskq_plus.storage.task_store import find_by_id


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------
CACHE_FILENAME = "cache.json"

# Default TTL (seconds) when TASKQ_CACHE_TTL is unset / unparseable.
DEFAULT_CACHE_TTL_S = 86400

UTC = dt.timezone.utc

# Internal lock — read-modify-write inside write_cache_entry. The on-disk
# atomic write (tmp + os.replace in cache_store) keeps the file valid; this
# lock is the merging safety net so concurrent upserts do not clobber each
# other's entries (test_fr04_d).
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _now_iso(now_fn: Optional[Callable[[], float]] = None) -> str:
    """Return UTC ISO-8601 timestamp at second precision (trailing 'Z')."""
    fn = now_fn if now_fn is not None else time.time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(fn())))


def _parse_iso(s: Any) -> Optional[float]:
    """Parse an ISO-8601 'Z' timestamp into epoch seconds. None on failure."""
    if not isinstance(s, str) or not s:
        return None
    raw = s
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        return None


def _resolve_ttl(ttl_seconds: Optional[int]) -> int:
    """Pick the effective TTL: explicit arg > TASKQ_CACHE_TTL > default."""
    if ttl_seconds is not None:
        try:
            return int(ttl_seconds)
        except (TypeError, ValueError):
            pass
    raw = os.environ.get("TASKQ_CACHE_TTL")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_CACHE_TTL_S


def _extract_result(task: Dict[str, Any]) -> Dict[str, Any]:
    """Project the executor's task record onto the cache entry's `result`."""
    return {
        "status": task.get("status"),
        "exit_code": task.get("exit_code"),
        "stdout_tail": task.get("stdout_tail", "") or "",
        "stderr_tail": task.get("stderr_tail", "") or "",
        "duration_ms": task.get("duration_ms", 0) or 0,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def cache_signature(command: str) -> str:
    """Return the cache key for `command`: sha256(command), lowercase hex.

    [FR-04]
    Citations: SPEC.md §3 FR-04 (cache signature = sha256(command)).
    """
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def build_cache_entry(
    command: str,
    result: Dict[str, Any],
    now_fn: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """Assemble a single cache entry `{signature, result, cached_at}`.

    [FR-04]
    Citations:
      - SPEC.md §3 FR-04 (signature, result, cached_at entry shape).
    """
    return {
        "signature": cache_signature(command),
        "result": result,
        "cached_at": _now_iso(now_fn),
    }


def lookup_cached_result(
    command: str,
    ttl_seconds: Optional[int] = None,
    now_fn: Optional[Callable[[], float]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the cache entry for `command` when fresh, else None.

    A "fresh" entry is one whose `cached_at` is within `ttl_seconds` of the
    wall clock. Returns None for missing entries, malformed timestamps, and
    entries older than the TTL.

    [FR-04]
    Citations: SPEC.md §3 FR-04 (within TASKQ_CACHE_TTL seconds → directly
    replay).
    """
    store = read_cache()
    if not store:
        return None
    entries = store.get("entries") or {}
    sig = cache_signature(command)
    entry = entries.get(sig)
    if not isinstance(entry, dict):
        return None
    parsed = _parse_iso(entry.get("cached_at"))
    if parsed is None:
        return None
    ttl = _resolve_ttl(ttl_seconds)
    clock = now_fn if now_fn is not None else time.time
    age = float(clock()) - parsed
    if age > ttl:
        return None
    return entry


def write_cache_entry(
    command: str,
    result: Dict[str, Any],
    ttl_seconds: Optional[int] = None,
    now_fn: Optional[Callable[[], float]] = None,
) -> None:
    """Persist a fresh `done` result for `command` into cache.json.

    Read-modify-write under the internal Lock so concurrent writers do not
    lose each other's entries; the on-disk write itself is atomic
    (tmp + os.replace inside `cache_store.write_cache`).

    [FR-04] [NFR-03]
    Citations:
      - SPEC.md §3 FR-04 (on success → write to $TASKQ_HOME/cache.json).
      - SPEC.md §8 NFR-03 (atomic + thread-safe cache write).
    """
    entry = build_cache_entry(command, result, now_fn=now_fn)
    sig = entry["signature"]
    with _cache_lock:
        current = read_cache()
        if not isinstance(current, dict):
            current = {"version": 1, "entries": {}}
        current.setdefault("version", 1)
        entries = current.get("entries")
        if not isinstance(entries, dict):
            entries = {}
            current["entries"] = entries
        entries[sig] = entry
        write_cache(current)


def execute_with_cache(
    task_id: str,
    *,
    use_cache: bool = True,
    ttl_seconds: Optional[int] = None,
    now_fn: Optional[Callable[[], float]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    run_fn: Optional[Callable[[str], int]] = None,
) -> Optional[Dict[str, Any]]:
    """Run `task_id` with cache-aware replay.

    On cache HIT (and `use_cache=True`): return the cached payload enriched
    with `cached: True`, printing `cached: true` to stdout — no subprocess
    fires. On miss / expired / `use_cache=False`: delegate to the executor
    (`run_fn` if injected, otherwise `run_with_retry`); on `done` persist
    the fresh result into cache.json.

    [FR-04]
    Citations:
      - SPEC.md §3 FR-04 (within TTL → replay, no subprocess, `cached: true`).
      - SPEC.md §3 FR-04 (miss/expired → normal execution; on `done` → write
        to $TASKQ_HOME/cache.json).
    """
    _ = sleep_fn  # accepted for API parity; tests inject run_fn instead.

    rec = find_by_id(task_id)
    if rec is None:
        return None
    command = rec.get("command") or ""

    if use_cache:
        hit = lookup_cached_result(
            command, ttl_seconds=ttl_seconds, now_fn=now_fn
        )
        if hit is not None:
            payload = dict(hit.get("result") or {})
            payload["cached"] = True
            print("cached: true")
            return payload

    # Cache miss / expired / cache disabled: delegate to the executor.
    runner = run_fn if run_fn is not None else exec_run_with_retry
    exit_code = runner(task_id)

    after = find_by_id(task_id) or {}
    result = _extract_result(after)
    # Override with the runner's actual exit code so the CLI can propagate
    # breaker (3), timeout (4), and failed (1) outcomes verbatim — a HIT path
    # never reaches here, so the override only affects the miss/expired path.
    result["exit_code"] = exit_code

    if result.get("status") == "done":
        write_cache_entry(
            command, result, ttl_seconds=ttl_seconds, now_fn=now_fn
        )

    return result