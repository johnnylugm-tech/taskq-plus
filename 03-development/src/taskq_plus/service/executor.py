"""Task executor — subprocess state machine + ThreadPoolExecutor batch.

[FR-02] [FR-03]
Citations:
  - SPEC.md §3 FR-02 (single-task run, --all batch, subprocess.run args).
  - SPEC.md §3 FR-02 (state machine: pending → running → done|failed|timeout|blocked).
  - SPEC.md §3 FR-02 (result fields: exit_code, stdout_tail, stderr_tail,
    duration_ms, finished_at; tail bounded to last 2000 chars).
  - SPEC.md §3 FR-02 (ThreadPoolExecutor + DAG topological order; shared Lock).
  - SPEC.md §3 FR-02 (single-task timeout → CLI exit 4).
  - SPEC.md §3 FR-06 (DAG topological ordering).
  - SPEC.md §8 #15 (subprocess invoked without the shell meta-flag).
  - SPEC.md#L108 (FR-03 retry: `TASKQ_BACKOFF_BASE × 2^n`, injectable sleep).
  - SPEC.md#L113 (FR-03 breaker OPEN → exit 3 + stderr `breaker open`).
"""

from __future__ import annotations

import datetime
import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, TypeVar, cast

from taskq_plus.service.breaker import (
    DEFAULT_BACKOFF_BASE_S,
    DEFAULT_COOLDOWN_S,
    DEFAULT_RETRY_LIMIT,
    DEFAULT_THRESHOLD,
    Breaker,
    compute_backoff_seconds,
)
from taskq_plus.storage.breaker_store import read_breaker, write_breaker
from taskq_plus.storage.task_store import (
    find_by_id,
    load_tasks,
    save_tasks,
)


# ---------------------------------------------------------------------------
# Public constants — CLI exit-code mapping per SPEC.md §3 FR-02.
# ---------------------------------------------------------------------------
EXIT_OK = 0          # SPEC.md §3 FR-02 — exit 0 → done.
EXIT_FAILED = 1      # SPEC.md §3 FR-02 — non-zero exit → failed.
EXIT_TIMEOUT = 4     # SPEC.md §3 FR-02 — single-task timeout → exit 4.
EXIT_BREAKER_OPEN = 3  # SPEC.md#L113 / SPEC.md#L140 — breaker open → exit 3.

# SPEC.md#L113 — the exact stderr token emitted when a run is rejected.
BREAKER_OPEN_MESSAGE = "breaker open"

# FR-03 outcomes that count as a retryable / final failure — SPEC.md#L108.
RETRYABLE_EXITS = (EXIT_FAILED, EXIT_TIMEOUT)

# SPEC.md §3 FR-02 — stdout_tail / stderr_tail bounded to last 2000 chars.
TAIL_BOUND = 2000

# Default timeout (seconds) when TASKQ_TASK_TIMEOUT is unset.
DEFAULT_TIMEOUT_S = 30.0

# Default ThreadPoolExecutor size when TASKQ_MAX_WORKERS is unset.
DEFAULT_MAX_WORKERS = 4

UTC = datetime.timezone.utc


# ---------------------------------------------------------------------------
# Shared lock — NFR-03 / SPEC.md §3 FR-02 (storage writes thread-safe).
# Every store mutation MUST be performed under this lock.
# ---------------------------------------------------------------------------
_store_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
T = TypeVar("T")


def _read_env(name: str, parse: Callable[[str], T], default: T) -> T:
    """Read env var `name`; return `default` when unset/empty/unparseable.

    [FR-02]
    Citations:
      - SPEC.md §3 FR-02 (TASKQ_TASK_TIMEOUT / TASKQ_MAX_WORKERS).
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return parse(raw)
    except ValueError:
        return default


def _now_iso() -> str:
    """UTC ISO-8601 timestamp with a trailing 'Z'.

    [FR-02]
    Citations: SPEC.md §3 FR-02 (finished_at field).
    """
    return datetime.datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _truncate_tail(value: Any, bound: int = TAIL_BOUND) -> str:
    """Return the last `bound` characters of `value` coerced to str.

    [FR-02]
    Citations: SPEC.md §3 FR-02 ("末 2000 字元" — last 2000 chars).
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    if len(text) <= bound:
        return text
    return text[-bound:]


def _set_status(task_id: str, updates: Dict[str, Any]) -> None:
    """Atomically merge `updates` into the task record under the shared lock.

    [FR-02] [NFR-03]
    Citations: SPEC.md §3 FR-02 (thread-safe store writes).
    """
    with _store_lock:
        tasks = load_tasks()
        for t in tasks:
            if t.get("id") == task_id:
                t.update(updates)
                break
        save_tasks(tasks)


def _resolve_timeout() -> float:
    """Return the per-task timeout (seconds) from TASKQ_TASK_TIMEOUT env.

    [FR-02]
    Citations: SPEC.md §3 FR-02 (subprocess.run timeout).
    """
    return _read_env("TASKQ_TASK_TIMEOUT", float, DEFAULT_TIMEOUT_S)


def _resolve_max_workers() -> int:
    """Return the ThreadPoolExecutor size from TASKQ_MAX_WORKERS env.

    [FR-02]
    Citations: SPEC.md §3 FR-02 (ThreadPoolExecutor max_workers).
    """
    return _read_env("TASKQ_MAX_WORKERS", int, DEFAULT_MAX_WORKERS)


def _topological_levels(tasks: List[Dict[str, Any]]) -> List[List[str]]:
    """Group tasks into dependency-respecting execution levels.

    Each level lists task ids whose dependencies are all satisfied by earlier
    levels. Within a level, ids may run concurrently via ThreadPoolExecutor.

    [FR-02]
    Citations:
      - SPEC.md §3 FR-02 (DAG topological order).
      - SPEC.md §3 FR-06 (DAG ordering primitive).
    """
    known_ids = {t.get("id") for t in tasks}
    remaining: List[Dict[str, Any]] = list(tasks)
    satisfied: set = set()
    levels: List[List[str]] = []
    while remaining:
        level = cast(
            List[str],
            [
                tid
                for t in remaining
                if all(
                    dep in satisfied or dep not in known_ids
                    for dep in (t.get("depends_on") or [])
                )
                for tid in (t.get("id"),)
                if tid is not None
            ],
        )
        if not level:
            # Cycle or unsatisfiable deps — emit remaining as a final level.
            remaining_ids = cast(
                List[str],
                [tid for tid in (t.get("id") for t in remaining) if tid is not None],
            )
            levels.append(remaining_ids)
            break
        levels.append(level)
        satisfied.update(level)
        remaining = [t for t in remaining if t.get("id") not in satisfied]
    return levels


def _build_result(
    *,
    status: str,
    exit_code: Optional[int],
    stdout: Any,
    stderr: Any,
    duration_ms: int,
) -> Dict[str, Any]:
    """Assemble the standard task-result dict written to the store.

    [FR-02]
    Citations: SPEC.md §3 FR-02 (result fields; tail bounded to last 2000 chars).
    """
    return {
        "status": status,
        "exit_code": exit_code,
        "stdout_tail": _truncate_tail(stdout),
        "stderr_tail": _truncate_tail(stderr),
        "duration_ms": duration_ms,
        "finished_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def execute_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Execute one pending task and write the result back to the store.

    [FR-02]
    Citations:
      - SPEC.md §3 FR-02 (subprocess.run args, state machine, result fields).
      - SPEC.md §3 FR-02 (timeout → status=timeout).
      - SPEC.md §3 FR-02 (tail bounded to last 2000 chars).
    """
    rec = find_by_id(task_id)
    if rec is None:
        return None

    command = rec.get("command") or ""
    _set_status(task_id, {"status": "running"})

    started = time.monotonic()
    timeout_s = _resolve_timeout()
    try:
        proc = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            # Explicit: subprocess invoked without the shell meta-flag (NFR-02).
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = _build_result(
            status="timeout",
            exit_code=None,
            stdout=exc.stdout,
            stderr=exc.stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    else:
        result = _build_result(
            status="done" if proc.returncode == 0 else "failed",
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    _set_status(task_id, result)
    return result


def run(task_id: str) -> int:
    """Single-task entry; return the CLI exit code.

    [FR-02]
    Citations:
      - SPEC.md §3 FR-02 (single-task run).
      - SPEC.md §3 FR-02 (timeout → exit 4).
      - SPEC.md §3 FR-02 (exit 0 → done, non-zero → failed).
    """
    result = execute_task(task_id)
    if result is None:
        return EXIT_FAILED
    status = result.get("status")
    if status == "timeout":
        return EXIT_TIMEOUT
    if status == "done":
        return EXIT_OK
    return EXIT_FAILED


def run_all() -> None:
    """Batch entry — execute every pending task via ThreadPoolExecutor.

    Tasks are dispatched in DAG topological layers: a layer's tasks may run
    concurrently, but a later layer never starts until every dependency in
    earlier layers is finished. All store writes share the module Lock.

    [FR-02]
    Citations:
      - SPEC.md §3 FR-02 (ThreadPoolExecutor + DAG topological order).
      - SPEC.md §3 FR-02 (shared Lock over store).
      - SPEC.md §3 FR-06 (DAG ordering).
    """
    tasks = load_tasks()
    pending = [t for t in tasks if t.get("status") == "pending"]
    if not pending:
        return
    levels = _topological_levels(pending)
    max_workers = _resolve_max_workers()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for level_ids in levels:
            futures = [pool.submit(execute_task, tid) for tid in level_ids]
            for fut in futures:
                fut.result()


# ---------------------------------------------------------------------------
# FR-03 — retry with exponential backoff + global circuit breaker.
# ---------------------------------------------------------------------------
def _resolve_retry_limit() -> int:
    """Return the retry ceiling from TASKQ_RETRY_LIMIT env.

    [FR-03]
    Citations: SPEC.md#L108 (上限 `TASKQ_RETRY_LIMIT` 次),
    SPEC.md#L297 (預設 `2`).
    """
    return _read_env("TASKQ_RETRY_LIMIT", int, DEFAULT_RETRY_LIMIT)


def _resolve_backoff_base() -> float:
    """Return the backoff base (seconds) from TASKQ_BACKOFF_BASE env.

    [FR-03]
    Citations: SPEC.md#L108 (`TASKQ_BACKOFF_BASE × 2^n`),
    SPEC.md#L298 (預設 `0.1`).
    """
    return _read_env("TASKQ_BACKOFF_BASE", float, DEFAULT_BACKOFF_BASE_S)


def _resolve_breaker_threshold() -> int:
    """Return the breaker trip threshold from TASKQ_BREAKER_THRESHOLD env.

    [FR-03]
    Citations: SPEC.md#L112 (計數 ≥ `TASKQ_BREAKER_THRESHOLD` → `OPEN`),
    SPEC.md#L299 (預設 `3`).
    """
    return _read_env("TASKQ_BREAKER_THRESHOLD", int, DEFAULT_THRESHOLD)


def _resolve_breaker_cooldown() -> float:
    """Return the OPEN → HALF_OPEN cooldown from TASKQ_BREAKER_COOLDOWN env.

    [FR-03]
    Citations: SPEC.md#L114 (經 `TASKQ_BREAKER_COOLDOWN` 秒後 `HALF_OPEN`),
    SPEC.md#L300 (預設 `5.0`).
    """
    return _read_env("TASKQ_BREAKER_COOLDOWN", float, DEFAULT_COOLDOWN_S)


def _load_breaker() -> Breaker:
    """Rebuild the global breaker from `$TASKQ_HOME/breaker.json`.

    Reading on every entry (and writing back on exit) is what makes the counter
    global across tasks AND across processes.

    [FR-03]
    Citations:
      - SPEC.md#L110 (斷路器為全域,跨任務、跨進程).
      - SPEC.md#L115 (狀態持久化於 `$TASKQ_HOME/breaker.json`).
    """
    return Breaker.from_record(
        read_breaker(),
        threshold=_resolve_breaker_threshold(),
        cooldown_seconds=_resolve_breaker_cooldown(),
    )


def run_with_retry(
    task_id: str, sleep_fn: Optional[Callable[[float], None]] = None
) -> int:
    """Breaker-guarded single-task entry with exponential-backoff retries.

    Order of operations:
      1. Consult the persisted breaker. While OPEN (inside cooldown) the run is
         rejected with exit 3 and `breaker open` on stderr — no subprocess runs.
      2. Otherwise execute the task, retrying a failed/timeout outcome up to
         `TASKQ_RETRY_LIMIT` times, waiting `TASKQ_BACKOFF_BASE × 2^n` before
         retry n. `sleep_fn` is injectable so tests never really wait.
      3. Feed the final outcome back into the breaker and persist it: success
         closes it and zeroes the counter, a final failure advances the counter
         (and trips it at the threshold).

    [FR-03]
    Citations:
      - SPEC.md#L108 (重試上限與 `TASKQ_BACKOFF_BASE × 2^n`;sleep 可注入).
      - SPEC.md#L112 (連續最終失敗計數 ≥ threshold → `OPEN`).
      - SPEC.md#L113 (`OPEN` 期間立即拒絕:exit 3 + stderr `breaker open`,
        不執行 subprocess).
      - SPEC.md#L114 (`HALF_OPEN` 放行一個任務:成功 → `CLOSED` 計數歸零;
        失敗 → 重新 `OPEN`).
      - SPEC.md#L140 (exit code `3` breaker open).
    """
    breaker = _load_breaker()
    if not breaker.allow_request():
        print(BREAKER_OPEN_MESSAGE, file=sys.stderr)
        return EXIT_BREAKER_OPEN

    # Persist the OPEN → HALF_OPEN admission before spawning anything, so a
    # concurrent process observes that the trial slot is taken.
    write_breaker(breaker.to_record())

    sleep = sleep_fn if sleep_fn is not None else time.sleep
    retry_limit = _resolve_retry_limit()
    backoff_base = _resolve_backoff_base()

    exit_code = run(task_id)
    attempt = 0
    while exit_code in RETRYABLE_EXITS and attempt < retry_limit:
        sleep(compute_backoff_seconds(attempt, backoff_base))
        exit_code = run(task_id)
        attempt += 1

    if exit_code == EXIT_OK:
        breaker.record_success()
    else:
        breaker.record_failure()
    write_breaker(breaker.to_record())
    return exit_code

