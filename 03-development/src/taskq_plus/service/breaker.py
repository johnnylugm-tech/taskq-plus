"""Circuit breaker state machine — CLOSED / OPEN / HALF_OPEN + retry backoff.

The breaker is a pure in-memory state machine: it owns no I/O. Persistence lives
in `taskq_plus.storage.breaker_store`, and the caller (the executor) is
responsible for the read-modify-write cycle. The wall-clock source is injected
so both the retry backoff and the cooldown window are deterministically
testable.

[FR-03]
Citations:
  - SPEC.md#L106 (FR-03: 重試與斷路器).
  - SPEC.md#L108 (第 n 次重試前等待 `TASKQ_BACKOFF_BASE × 2^n` 秒).
  - SPEC.md#L110 (斷路器為全域,跨任務、跨進程).
  - SPEC.md#L112 (連續最終失敗計數 ≥ `TASKQ_BREAKER_THRESHOLD` → `OPEN`).
  - SPEC.md#L114 (經 `TASKQ_BREAKER_COOLDOWN` 秒 → `HALF_OPEN`;成功 → `CLOSED`
    且計數歸零;失敗 → 重新 `OPEN`).
  - SPEC.md#L209 (NFR-03: `OPEN → CLOSED` 恢復時間 ≤ cooldown + 1s).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional


# ---------------------------------------------------------------------------
# Public state names — verbatim from SPEC.md#L112-L114.
# ---------------------------------------------------------------------------
STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"

# Defaults for the FR-03 env knobs — SPEC.md#L297-L300.
DEFAULT_RETRY_LIMIT = 2
DEFAULT_BACKOFF_BASE_S = 0.1
DEFAULT_THRESHOLD = 3
DEFAULT_COOLDOWN_S = 5.0

# breaker.json schema version — SPEC.md#L312.
RECORD_VERSION = 1


def compute_backoff_seconds(attempt_n: int, base_seconds: float) -> float:
    """Return the wait before retry `attempt_n` (zero-indexed): base × 2^n.

    [FR-03]
    Citations:
      - SPEC.md#L108 (第 n 次重試前等待 `TASKQ_BACKOFF_BASE × 2^n` 秒).
      - SPEC.md#L298 (`TASKQ_BACKOFF_BASE` 預設 `0.1`).
    """
    return float(base_seconds) * (2 ** int(attempt_n))


class Breaker:
    """Global circuit breaker over consecutive final task failures.

    Transitions (SPEC.md#L112-L114):
      CLOSED    --(failure_count >= threshold)--> OPEN
      OPEN      --(cooldown elapsed, on admission)--> HALF_OPEN
      HALF_OPEN --(success)--> CLOSED (count reset)
      HALF_OPEN --(failure)--> OPEN (cooldown restarts)

    [FR-03]
    Citations:
      - SPEC.md#L110 (全域,跨任務、跨進程).
      - SPEC.md#L112 (threshold → OPEN).
      - SPEC.md#L114 (cooldown → HALF_OPEN;成功 → CLOSED 且計數歸零;
        失敗 → 重新 OPEN).
      - SPEC.md#L209 (NFR-03 恢復時間界限).
    """

    def __init__(
        self,
        threshold: int = DEFAULT_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_S,
        clock: Optional[Callable[[], float]] = None,
        state: str = STATE_CLOSED,
        failure_count: int = 0,
        opened_at: float = 0.0,
    ) -> None:
        """Build a breaker; `clock` is injectable for deterministic tests.

        The default clock is `time.time` (epoch seconds) rather than a monotonic
        counter because the state is shared across processes: a monotonic value
        written by one process is meaningless to the next.

        [FR-03]
        Citations:
          - SPEC.md#L110 (跨進程共享狀態).
          - SPEC.md#L112 (`TASKQ_BREAKER_THRESHOLD`).
          - SPEC.md#L114 (`TASKQ_BREAKER_COOLDOWN`).
        """
        self._threshold = int(threshold)
        self._cooldown_seconds = float(cooldown_seconds)
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._state = state
        self._failure_count = int(failure_count)
        self._opened_at = float(opened_at)

    # -- observers ---------------------------------------------------------
    @property
    def state(self) -> str:
        """Current state name — one of CLOSED / OPEN / HALF_OPEN.

        Reading the state never advances the machine: an OPEN breaker stays
        OPEN until `allow_request` actually admits a trial call.

        [FR-03]
        Citations: SPEC.md#L112-L114 (state names and transitions).
        """
        return self._state

    # -- transitions -------------------------------------------------------
    def allow_request(self) -> bool:
        """Return True when a task may execute; transition OPEN → HALF_OPEN.

        While OPEN and inside the cooldown window the call is denied and no
        subprocess may be spawned. Once `cooldown_seconds` have elapsed the
        breaker admits exactly one trial call and moves to HALF_OPEN, so the
        recovery latency is bounded by the cooldown itself (NFR-03).

        [FR-03] [NFR-03]
        Citations:
          - SPEC.md#L113 (`OPEN` 期間任何 `run` 立即拒絕,不執行 subprocess).
          - SPEC.md#L114 (經 cooldown 秒後進入 `HALF_OPEN`:放行一個任務).
          - SPEC.md#L209 (`OPEN → CLOSED` 恢復時間 ≤ cooldown + 1s).
        """
        if self._state == STATE_OPEN:
            if float(self._clock()) - self._opened_at >= self._cooldown_seconds:
                self._state = STATE_HALF_OPEN
                return True
            return False
        return True

    def record_failure(self) -> None:
        """Record one final failure (retries exhausted, still failed/timeout).

        A failure during the HALF_OPEN trial re-opens the breaker immediately
        and restarts the cooldown; otherwise the consecutive counter advances
        and trips the breaker once it reaches the threshold.

        [FR-03]
        Citations:
          - SPEC.md#L112 (連續最終失敗計數 ≥ threshold → `OPEN`).
          - SPEC.md#L114 (HALF_OPEN 失敗 → 重新 `OPEN`).
        """
        if self._state == STATE_HALF_OPEN:
            self._trip()
            return
        self._failure_count += 1
        if self._failure_count >= self._threshold:
            self._trip()

    def record_success(self) -> None:
        """Record a success: close the breaker and zero the failure counter.

        [FR-03]
        Citations: SPEC.md#L114 (成功 → `CLOSED` 且計數歸零).
        """
        self._state = STATE_CLOSED
        self._failure_count = 0
        self._opened_at = 0.0

    def _trip(self) -> None:
        """Move to OPEN and stamp the cooldown start with the injected clock.

        [FR-03]
        Citations: SPEC.md#L112 (→ `OPEN`), SPEC.md#L114 (cooldown 起算).
        """
        self._state = STATE_OPEN
        self._opened_at = float(self._clock())

    # -- persistence bridge ------------------------------------------------
    def to_record(self) -> Dict[str, Any]:
        """Serialise to the breaker.json payload shape.

        [FR-03]
        Citations: SPEC.md#L312 (`{version:1, state, failure_count, opened_at}`).
        """
        return {
            "version": RECORD_VERSION,
            "state": self._state,
            "failure_count": self._failure_count,
            "opened_at": self._opened_at,
        }

    @classmethod
    def from_record(
        cls,
        record: Optional[Dict[str, Any]],
        *,
        threshold: int = DEFAULT_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_S,
        clock: Optional[Callable[[], float]] = None,
    ) -> "Breaker":
        """Rebuild a breaker from a persisted record (None → cold CLOSED start).

        [FR-03]
        Citations:
          - SPEC.md#L110 (跨進程狀態共享).
          - SPEC.md#L312 (breaker.json 欄位).
        """
        data = record or {}
        return cls(
            threshold=threshold,
            cooldown_seconds=cooldown_seconds,
            clock=clock,
            state=str(data.get("state") or STATE_CLOSED),
            failure_count=int(data.get("failure_count") or 0),
            opened_at=float(data.get("opened_at") or 0.0),
        )
