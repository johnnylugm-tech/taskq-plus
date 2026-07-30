"""Task executor — subprocess state machine + ThreadPoolExecutor batch.

[FR-02] [FR-03] [FR-04] [FR-07]
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
  - SPEC.md §3 FR-04 (cache miss / expired → executor's `done` result feeds
    the cache write-back in `taskq_plus.service.cache`).
  - SPEC.md §3 FR-07 (pre_run / post_run hook dispatch; plugin_error audit
    on hook failure; 3-strikes-disable state stays inside the run).
"""

from __future__ import annotations

import datetime
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from taskq_plus.service.breaker import (
    DEFAULT_BACKOFF_BASE_S,
    DEFAULT_COOLDOWN_S,
    DEFAULT_RETRY_LIMIT,
    DEFAULT_THRESHOLD,
    Breaker,
    compute_backoff_seconds,
)
from taskq_plus.service.dag import topological_layers
from taskq_plus.service.plugins import (
    PluginFailure,
    dispatch as plugin_dispatch,
    load_plugins as plugin_load_plugins,
)
from taskq_plus.storage.breaker_store import read_breaker, write_breaker
from taskq_plus.storage.task_store import (
    find_by_id,
    load_tasks,
    save_tasks,
)
from taskq_plus.observability.audit import current_logger


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


def _emit_audit(event: str, payload: Dict[str, Any]) -> None:
    """Append a single audit event to `$TASKQ_HOME/audit.log` (legacy FR-01).

    Mirrors the helper in `cli/commands.py` so the executor can write
    `plugin_error` events without importing the CLI module (which would
    cycle back through `service.executor`).

    [FR-07] [FR-08]
    Citations:
      - SPEC.md §3 FR-08 (audit events appended to $TASKQ_HOME).
      - SPEC.md §3 FR-07 (plugin_error events emitted per caught hook failure).
    """
    home = Path(os.environ.get("TASKQ_HOME", ".")).resolve()
    audit_path = home / "audit.log"
    line = json.dumps(
        {"event": event, "ts": _now_iso(), **payload}, ensure_ascii=False
    )
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _emit_plugin_errors(failures: List[PluginFailure], task_id: str) -> None:
    """Emit one `plugin_error` audit event per `PluginFailure` (SPEC §3 FR-07).

    [FR-07] [FR-08]
    Citations:
      - SPEC.md §3 FR-07 (plugin raises → log a `plugin_error` audit event and
        continue; task execution must not be interrupted).
    """
    for failure in failures:
        detail = {
            "task_id": task_id,
            "plugin": failure.plugin,
            "hook": failure.hook,
            "error": failure.error,
        }
        # Legacy journal (audit.log) — kept so old readers still see the
        # event.  The canonical FR-08 journal is written below.
        _emit_audit("plugin_error", detail)
        # FR-08: structured JSONL audit with the invocation-scoped
        # `correlation_id`.  NFR-04 redaction runs on the detail payload.
        current_logger().emit("plugin_error", task_id=task_id, detail=detail)


def _dispatch_and_emit(
    hook: str, plugins: list, *args: Any
) -> "Any":
    """Run `plugin_dispatch(hook, plugins, *args)` and emit audit events.

    `task_id` is taken from `args[0]["id"]` when present (FR-07 dispatches
    always receive the task dict as the first positional argument). Falls
    back to an empty string when no id is available.

    Returns the raw `PluginDispatchResult` so callers (e.g. kernel code
    outside the executor) can inspect disabled / failures directly.
    """
    task_id = ""
    if args:
        first = args[0]
        if isinstance(first, dict):
            task_id = str(first.get("id") or "")
    result = plugin_dispatch(hook, plugins, *args)
    _emit_plugin_errors(result.failures, task_id)
    return result


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

    Delegates to `service.dag.topological_layers`, the FR-06-owned Kahn
    implementation, so the batch scheduler and the `graph` command share one
    layering algorithm.

    [FR-02] [FR-06]
    Citations:
      - SPEC.md §3 FR-02 (DAG topological order).
      - SPEC.md §3 FR-06 (Kahn topological sort; same-layer tasks concurrent).
    """
    return topological_layers(tasks)


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


def _unmet_dependencies(task_id: str) -> List[str]:
    """Return the dep ids of `task_id` that have not reached status `done`.

    A dep id with no matching record is treated as satisfied (it was cleared
    or never existed, so it cannot gate this task). A dep that exists but sits
    in any non-`done` status — `failed`, `timeout`, `blocked`, still `pending`
    — is unmet.

    [FR-06]
    Citations:
      - SPEC.md §3 FR-06 (dependency result not `done` → downstream task is
        marked `blocked`, not executed).
    """
    rec = find_by_id(task_id)
    if rec is None:
        return []
    unmet: List[str] = []
    for dep_id in rec.get("depends_on") or []:
        dep = find_by_id(dep_id)
        if dep is not None and dep.get("status") != "done":
            unmet.append(dep_id)
    return unmet


def _execute_or_block(task_id: str) -> Optional[Dict[str, Any]]:
    """Run `task_id`, or mark it `blocked` when a dependency is not `done`.

    Blocking is transitive without extra bookkeeping: layers are barriers, so
    a task whose parent was blocked in an earlier layer observes that parent's
    `blocked` status here and blocks in turn. A blocked task never shells out,
    so it also never feeds the FR-03 breaker failure count.

    [FR-06]
    Citations:
      - SPEC.md §3 FR-06 (dependency result not `done` → downstream `blocked`,
        not executed, not counted toward breaker failure count).
    """
    if _unmet_dependencies(task_id):
        _set_status(task_id, {"status": "blocked"})
        return None
    return execute_task(task_id)


def run_all() -> None:
    """Batch entry — execute every pending task via ThreadPoolExecutor.

    Tasks are dispatched in Kahn topological layers: a layer's tasks may run
    concurrently, but a later layer never starts until every dependency in
    earlier layers is finished. A task whose dependency did not reach `done`
    is marked `blocked` instead of being executed. All store writes share the
    module Lock.

    [FR-02] [FR-06]
    Citations:
      - SPEC.md §3 FR-02 (ThreadPoolExecutor + DAG topological order).
      - SPEC.md §3 FR-02 (shared Lock over store).
      - SPEC.md §3 FR-06 (Kahn topological sort; same-layer concurrency;
        dependency not `done` → downstream `blocked`).
    """
    tasks = load_tasks()
    pending = [t for t in tasks if t.get("status") == "pending"]
    if not pending:
        return
    levels = _topological_levels(pending)
    max_workers = _resolve_max_workers()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for level_ids in levels:
            futures = [pool.submit(_execute_or_block, tid) for tid in level_ids]
            for fut in futures:
                fut.result()


# ---------------------------------------------------------------------------
# FR-03 — retry with exponential backoff + global circuit breaker.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetryConfig:
    """Bundle of the four FR-03 env knobs for one `run_with_retry` invocation.

    [FR-03]
    Citations:
      - SPEC.md#L108 (上限 `TASKQ_RETRY_LIMIT`；`TASKQ_BACKOFF_BASE × 2^n`).
      - SPEC.md#L112 (`TASKQ_BREAKER_THRESHOLD` → `OPEN`).
      - SPEC.md#L114 (`TASKQ_BREAKER_COOLDOWN` → `HALF_OPEN`).
      - SPEC.md#L297-L300 (env knob defaults).
    """

    retry_limit: int
    backoff_base_seconds: float
    breaker_threshold: int
    breaker_cooldown_seconds: float


def _load_retry_config() -> RetryConfig:
    """Read the four FR-03 env knobs into a single `RetryConfig` snapshot.

    [FR-03]
    Citations:
      - SPEC.md#L108 (上限 `TASKQ_RETRY_LIMIT`；`TASKQ_BACKOFF_BASE × 2^n`).
      - SPEC.md#L297 (`TASKQ_RETRY_LIMIT` 預設 `2`).
      - SPEC.md#L298 (`TASKQ_BACKOFF_BASE` 預設 `0.1`).
      - SPEC.md#L299 (`TASKQ_BREAKER_THRESHOLD` 預設 `3`).
      - SPEC.md#L300 (`TASKQ_BREAKER_COOLDOWN` 預設 `5.0`).
    """
    return RetryConfig(
        retry_limit=_read_env("TASKQ_RETRY_LIMIT", int, DEFAULT_RETRY_LIMIT),
        backoff_base_seconds=_read_env(
            "TASKQ_BACKOFF_BASE", float, DEFAULT_BACKOFF_BASE_S
        ),
        breaker_threshold=_read_env(
            "TASKQ_BREAKER_THRESHOLD", int, DEFAULT_THRESHOLD
        ),
        breaker_cooldown_seconds=_read_env(
            "TASKQ_BREAKER_COOLDOWN", float, DEFAULT_COOLDOWN_S
        ),
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

    [FR-03] [FR-07]
    Citations:
      - SPEC.md#L108 (重試上限與 `TASKQ_BACKOFF_BASE × 2^n`;sleep 可注入).
      - SPEC.md#L112 (連續最終失敗計數 ≥ threshold → `OPEN`).
      - SPEC.md#L113 (`OPEN` 期間立即拒絕:exit 3 + stderr `breaker open`,
        不執行 subprocess).
      - SPEC.md#L114 (`HALF_OPEN` 放行一個任務:成功 → `CLOSED` 計數歸零;
        失敗 → 重新 `OPEN`).
      - SPEC.md#L140 (exit code `3` breaker open).
      - SPEC.md §3 FR-07 (pre_run / post_run dispatch; plugin_error audit
        events; 3-strikes-disable stays inside one run).
    """
    config = _load_retry_config()
    breaker = Breaker.from_record(
        read_breaker(),
        threshold=config.breaker_threshold,
        cooldown_seconds=config.breaker_cooldown_seconds,
    )
    if not breaker.allow_request():
        current_logger().emit(
            "breaker_open",
            task_id=task_id,
            detail={"cooldown_seconds": config.breaker_cooldown_seconds},
        )
        print(BREAKER_OPEN_MESSAGE, file=sys.stderr)
        return EXIT_BREAKER_OPEN

    # Persist the OPEN → HALF_OPEN admission before spawning anything, so a
    # concurrent process observes that the trial slot is taken.
    write_breaker(breaker.to_record())

    sleep = sleep_fn if sleep_fn is not None else time.sleep

    # FR-07: load the allowlist once per run; plugins=[] when TASKQ_PLUGINS
    # is unset so the non-plugin paths (FR-02/FR-03/FR-04) pay no cost.
    plugins = plugin_load_plugins()
    task_record = dict(find_by_id(task_id) or {"id": task_id})

    # FR-08: `run_start` carries the command being executed (NFR-04 redacted
    # before the bytes reach the disk).
    current_logger().emit(
        "run_start",
        task_id=task_id,
        detail={"command": task_record.get("command", "")},
    )

    _dispatch_and_emit("pre_run", plugins, task_record)

    exit_code = run(task_id)
    attempt = 0
    while exit_code in RETRYABLE_EXITS and attempt < config.retry_limit:
        # FR-08: emit a `retry` event per FR-03 exponential-backoff attempt
        # so the journal shows the retry sequence verbatim.
        current_logger().emit(
            "retry",
            task_id=task_id,
            detail={
                "attempt": attempt + 1,
                "backoff_seconds": compute_backoff_seconds(
                    attempt, config.backoff_base_seconds
                ),
            },
        )
        sleep(compute_backoff_seconds(attempt, config.backoff_base_seconds))
        exit_code = run(task_id)
        attempt += 1

    # FR-07: post_run receives the final task record (after the executor has
    # written `status` / `exit_code` / `stdout_tail` / `stderr_tail`).
    finished = dict(find_by_id(task_id) or {"id": task_id})
    _dispatch_and_emit("post_run", plugins, finished, finished)

    # FR-08: `run_end` mirrors `run_start` and records the final outcome.
    current_logger().emit(
        "run_end",
        task_id=task_id,
        detail={
            "status": finished.get("status"),
            "exit_code": exit_code,
        },
    )

    if exit_code == EXIT_OK:
        breaker.record_success()
    else:
        breaker.record_failure()
    write_breaker(breaker.to_record())
    return exit_code

