"""[FR-03] 全域斷路器狀態機 (CLOSED / OPEN / HALF_OPEN).

Citations:
- SPEC.md §3 FR-03 lines 110-115: 連續最終失敗計數 ≥
  ``TASKQ_BREAKER_THRESHOLD`` → ``OPEN``;``OPEN`` 期間任何 ``run``
  立即拒絕 (exit 3 + stderr ``breaker open``);經
  ``TASKQ_BREAKER_COOLDOWN`` 秒後進入 ``HALF_OPEN`` 放行一個任務 —
  成功 → ``CLOSED`` 且計數歸零;失敗 → 重新 ``OPEN``.
- SPEC.md §5 環境變數 lines 299-300: ``TASKQ_BREAKER_THRESHOLD`` 默認
  3;``TASKQ_BREAKER_COOLDOWN`` 默認 5.0.
- SAD §3.4: 持久化 envelope 由 ``breaker_store`` 負責原子寫.
- AC-FR-03.2..AC-FR-03.5 驗收準則.

狀態機輸入:
  - ``record_failure()`` — 由 CLI 在 execute_task 回報 ``failed`` /
    ``timeout`` (重試耗盡) 時呼叫;計數 +1;達閾值 → ``OPEN``.
  - ``record_success()`` — 由 CLI 在 execute_task 回報 ``done`` 時
    呼叫;計數歸零;任何非 ``CLOSED`` 狀態 → ``CLOSED``.
  - ``assert_closed()`` — 由 CLI 在 ``run`` 入口呼叫;``OPEN`` 且
    仍在 cooldown 內 → 拋 ``BreakerOpen``;``OPEN`` 且 cooldown 已過
    → 自動 ``OPEN → HALF_OPEN`` 然後放行;``CLOSED`` / ``HALF_OPEN``
    → 直接放行.
"""

from __future__ import annotations

import os
import time
from typing import Any

from taskq_plus.storage import breaker_store


class BreakerOpen(Exception):
    """Raised by ``assert_closed`` when the breaker is OPEN inside cooldown.

    Citations:
    - SPEC.md §3 FR-03 line 113: ``OPEN`` 期間任何 ``run`` 立即拒絕.
    - SPEC.md §7 錯誤處理 row ``breaker OPEN``: exit 3, stderr
      ``breaker open``, 不執行 subprocess.
    """

    def __init__(self, message: str = "breaker open") -> None:
        super().__init__(message)
        self.message = message


def _threshold() -> int:
    """Return ``$TASKQ_BREAKER_THRESHOLD`` as int (default 3, SPEC.md §5)."""
    return int(os.environ.get("TASKQ_BREAKER_THRESHOLD", "3"))


def _cooldown() -> float:
    """Return ``$TASKQ_BREAKER_COOLDOWN`` as float seconds (default 5.0)."""
    return float(os.environ.get("TASKQ_BREAKER_COOLDOWN", "5.0"))


def _persist(state: str, failure_count: int, opened_at: Any) -> None:
    """Write the breaker envelope atomically (NFR-03)."""
    breaker_store.save(
        {
            "state": state,
            "failure_count": failure_count,
            "opened_at": opened_at,
        }
    )


def current_state() -> str:
    """Return the current breaker state (``CLOSED`` / ``OPEN`` / ``HALF_OPEN``)."""
    # ``breaker_store.load`` already normalises state to the canonical
    # alphabet, so direct key access is sufficient.
    return breaker_store.load()["state"]


def failure_count() -> int:
    """Return the current consecutive-final-failure counter."""
    return breaker_store.load()["failure_count"]


def assert_closed() -> None:
    """Gate a ``run`` invocation.

    Behavior:
    - ``CLOSED`` → return None (admit).
    - ``OPEN`` and ``now - opened_at >= cooldown`` → auto-transition to
      ``HALF_OPEN`` (one atomic write) and admit the probe.
    - ``OPEN`` and still inside the cooldown window → raise
      ``BreakerOpen`` (caller maps to exit 3 + stderr ``breaker open``).
    - ``HALF_OPEN`` → admit (the single in-flight probe).

    Citations:
    - AC-FR-03.3 / AC-FR-03.4: OPEN rejection and cooldown-driven
      HALF_OPEN transition.
    - SPEC.md §3 FR-03 line 114: 經 cooldown 秒後進入 HALF_OPEN.
    """
    data = breaker_store.load()
    state = data["state"]
    count = data["failure_count"]
    opened_at = data["opened_at"]

    if state != "OPEN":
        # CLOSED or HALF_OPEN: admit unconditionally.
        return

    # An OPEN record without a clock is treated as "cooldown already
    # elapsed" so the breaker cannot lock forever (SPEC.md R3
    # mitigation).
    cooldown_elapsed = opened_at is None or (
        time.time() - float(opened_at)
    ) >= _cooldown()
    if cooldown_elapsed:
        _persist("HALF_OPEN", count, opened_at)
        return
    raise BreakerOpen("breaker open")


def record_failure() -> int:
    """Record a final (post-retry) failure; trip OPEN at threshold.

    Returns the NEW consecutive-failure count (monotone +1).

    Citations:
    - SPEC.md §3 FR-03 line 112: 連續最終失敗 (重試耗盡仍
      failed/timeout) 計數 ≥ ``TASKQ_BREAKER_THRESHOLD`` → ``OPEN``.
    - AC-FR-03.2: state persisted atomically in ``breaker.json``.
    """
    new_count = breaker_store.load()["failure_count"] + 1
    if new_count >= _threshold():
        next_state, opened_at = "OPEN", time.time()
    else:
        # Stay CLOSED but advance the counter (one atomic write per
        # recorded failure — the breaker envelope is the single source
        # of truth for the counter).
        next_state, opened_at = "CLOSED", None
    _persist(next_state, new_count, opened_at)
    return new_count


def record_success() -> int:
    """Record a successful run; reset counter to 0 and transition to CLOSED.

    No-op when the breaker is already CLOSED (the counter is preserved
    — P3-record-success-noop-on-closed: ``record_success(CLOSED, c) == c``).

    Returns the counter value AFTER the call (0 on a real transition,
    the existing value when already CLOSED).

    Citations:
    - SPEC.md §3 FR-03 line 114: 成功 → ``CLOSED`` 且計數歸零.
    - AC-FR-03.4: HALF_OPEN + success → CLOSED.
    """
    data = breaker_store.load()
    if data["state"] == "CLOSED":
        return data["failure_count"]
    _persist("CLOSED", 0, None)
    return 0
