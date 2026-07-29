"""[FR-02] 任務執行器:subprocess 執行 + DAG 拓撲排程.

Citations:
- SPEC.md §3 FR-02 lines 94-104 (執行器狀態機、結果欄位、
  ``ThreadPoolExecutor`` 並發、單任務 timeout → exit 4).
- SPEC.md §6 NFR-03: 共享 ``threading.Lock`` 序列化存儲寫入.
- TEST_SPEC.md FR-02 ACs AC-FR-02.1..5.
- SPEC.md §8 #15: 任何 ``subprocess.run`` 不得使用 shell-true 旗標.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from taskq_plus.storage.task_store import TaskStore
from taskq_plus.util import utc_now_iso


# SPEC.md §3 FR-02 result fields: 末 2000 字元 truncation.
TAIL_MAX = 2000

# Shared lock — AC-FR-02.3 / NFR-03: serialise ``TaskStore.save()`` so
# concurrent writes cannot leave ``tasks.json`` mid-write.
_tasks_store_lock = threading.Lock()

# Thread-safe monotonic timestamp sequence — guarantees distinct
# ``finished_at`` values for the AC2-finished_at-distinct assertion
# even when multiple tasks complete within the same microsecond.
_timestamp_lock = threading.Lock()
_timestamp_seq = 0
_last_timestamp = ""


def _resolve_timeout() -> float:
    """Return ``$TASKQ_TASK_TIMEOUT`` as float (default 30s)."""
    return float(os.environ.get("TASKQ_TASK_TIMEOUT", "30"))


def _resolve_max_workers() -> int:
    """Return ``$TASKQ_MAX_WORKERS`` as int (default 4)."""
    return int(os.environ.get("TASKQ_MAX_WORKERS", "4"))


def _truncate(text: str, limit: int = TAIL_MAX) -> str:
    """Return the trailing ``limit`` characters of ``text``.

    Citations: AC-FR-02.5 — ``stdout_tail`` / ``stderr_tail`` capped
    at last 2000 chars.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _decode_stream(stream: Any) -> str:
    """Normalise ``subprocess.TimeoutExpired.stdout/stderr`` to ``str``.

    With ``text=True`` the captured payload is already a string, but on
    the timeout-exception path some Python builds surface ``bytes``.
    Tolerate both.
    """
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return str(stream)


def _unique_finished_at() -> str:
    """Return a UTC ISO-8601 timestamp strictly greater than the last call.

    The monotonic counter below ``_last_timestamp`` guarantees two
    threads that hit ``utc_now_iso()`` in the same microsecond still
    produce distinct strings (NP-13 / FR-06 topo-order assertion).
    """
    global _timestamp_seq, _last_timestamp
    with _timestamp_lock:
        ts = utc_now_iso()
        _timestamp_seq += 1
        if ts <= _last_timestamp:
            # Bump one microsecond past the last issued timestamp.
            ts = _last_timestamp
            # ISO-8601 with microsecond precision; rewrite the last
            # 6 digits so order remains chronologically ascending.
            head, dot, tail = ts.rpartition(".")
            micros = int(tail[:6]) if dot else 0
            micros += 1
            ts = f"{head}.{micros:06d}"
        _last_timestamp = ts
        return ts


def run_subprocess(command: str, timeout: float) -> subprocess.CompletedProcess:
    """Execute ``command`` via ``subprocess.run`` with ``shell=False``.

    Citations:
    - SPEC.md §3 FR-02 line 98 verbatim: ``subprocess.run(shlex.split
      (command), capture_output=True, text=True, timeout=TASKQ_TASK
      _TIMEOUT)``.
    - AC-FR-02.1 / SPEC.md §8 #15: NEVER the shell-true flag.
    """
    argv = shlex.split(command)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


def _kahn_waves(tasks: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Topological waves of pending tasks (AC-FR-02.3 / FR-06).

    Returns ``waves`` such that ``waves[i]`` contains task ids whose
    ``depends_on`` all live in earlier waves. The Kahn invariant
    guarantees ``for u, v in edges: wave_index[u] < wave_index[v]``.
    """
    pending = {
        tid for tid, rec in tasks.items() if rec.get("status") == "pending"
    }
    in_degree: dict[str, int] = {tid: 0 for tid in pending}
    dependents: dict[str, list[str]] = {tid: [] for tid in pending}
    for tid in pending:
        deps = [d for d in (tasks[tid].get("depends_on") or []) if d in pending]
        in_degree[tid] = len(deps)
        for dep in deps:
            dependents[dep].append(tid)

    waves: list[list[str]] = []
    finished: set[str] = set()
    current = sorted(tid for tid in pending if in_degree[tid] == 0)
    while current:
        waves.append(current)
        next_wave: set[str] = set()
        for tid in current:
            finished.add(tid)
            for child in dependents.get(tid, []):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    next_wave.add(child)
        current = sorted(next_wave - finished)
    return waves


def execute_task(
    store: TaskStore, task_id: str
) -> tuple[str, dict[str, Any] | None]:
    """Run a single pending task and persist its terminal record atomically.

    Citations:
    - SPEC.md §3 FR-02 lines 99-102 (state machine + result fields).
    - AC-FR-02.2: five completion fields written in a SINGLE
      ``TaskStore.save()`` call.
    - AC-FR-02.3 / NFR-03: store write guarded by ``_tasks_store_lock``.
    - AC-FR-02.4: ``TimeoutExpired`` → status ``timeout``; caller maps
      to exit code 4.
    - AC-FR-02.5: stdout / stderr truncated to last ``TAIL_MAX`` chars.

    Returns the terminal ``status`` plus the saved record (or ``None``
    when the task id is unknown).
    """
    pre_tasks = store.load()
    task = pre_tasks.get(task_id)
    if task is None:
        return "missing", None

    timeout = _resolve_timeout()
    start = time.monotonic()

    try:
        result = run_subprocess(task.get("command", ""), timeout)
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        exit_code = -1
        stdout_tail = _truncate(_decode_stream(getattr(exc, "stdout", None)))
        stderr_tail = _truncate(_decode_stream(getattr(exc, "stderr", None)))
        status = "timeout"
    except FileNotFoundError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        exit_code = 127
        stdout_tail = ""
        stderr_tail = _truncate(str(exc))
        status = "failed"
    else:
        duration_ms = int((time.monotonic() - start) * 1000)
        exit_code = int(result.returncode)
        stdout_tail = _truncate(result.stdout or "")
        stderr_tail = _truncate(result.stderr or "")
        status = "done" if exit_code == 0 else "failed"

    finished_at = _unique_finished_at()

    with _tasks_store_lock:
        current = store.load()
        current_task = current.get(task_id)
        if current_task is None:
            return "missing", None
        updated = dict(current)
        updated[task_id] = {
            **current_task,
            "status": status,
            "exit_code": exit_code,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "duration_ms": duration_ms,
            "finished_at": finished_at,
        }
        store.save(updated)
    return status, updated[task_id]


def run_all(store: TaskStore) -> dict[str, str]:
    """Execute every pending task in DAG topological order via ThreadPool.

    Citations:
    - SPEC.md §3 FR-02 line 103: ``ThreadPoolExecutor(max_workers=
      TASKQ_MAX_WORKERS)``, 並發執行全部可執行的 pending 任務.
    - AC-FR-02.3: tasks within a wave share the pool; wave boundaries
      enforce DAG topo order; store writes serialised by the lock.
    """
    tasks = store.load()
    waves = _kahn_waves(tasks)
    max_workers = max(1, _resolve_max_workers())
    statuses: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for wave in waves:
            futures = {
                pool.submit(execute_task, store, tid): tid for tid in wave
            }
            for fut in futures:
                tid = futures[fut]
                try:
                    status, _ = fut.result()
                except Exception:  # defensive — surface as failed, keep going
                    status = "failed"
                statuses[tid] = status
    return statuses