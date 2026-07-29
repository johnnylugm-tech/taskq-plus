"""TDD-RED tests for FR-03 (重試與斷路器).

Maps 1:1 to TEST_SPEC.md §FR-03 cases 1-6. The expected RED state is
``ModuleNotFoundError`` at pytest collection time (``taskq_plus.service
.breaker`` / ``taskq_plus.storage.breaker_store`` do not exist yet), or
every assertion failing; either counts as a valid failing test in this
phase per the harness TDD contract.

GREEN TODO — the modules and contracts these tests bind to:

* ``taskq_plus.storage.breaker_store`` persists ``$TASKQ_HOME/
  breaker.json`` with the SAD §3.4 envelope
  ``{"version": 1, "state": str, "failure_count": int,
  "opened_at": float | None}``, where ``state`` is one of
  ``"CLOSED" | "OPEN" | "HALF_OPEN"`` and ``opened_at`` is POSIX
  epoch seconds (``time.time()``). It MUST import the writer as
  ``from taskq_plus.storage.atomic import atomic_write_json`` and call
  it exactly once per state transition (AC-FR-03.2; NFR-03 atomic).
* ``taskq_plus.service.breaker`` exposes:
  - ``current_state() -> str``
  - ``failure_count() -> int``
  - ``record_failure() -> int``  (returns the NEW count; monotone +1)
  - ``record_success() -> int``  (returns the NEW count; resets to 0)
  - ``assert_closed() -> None``  (raises when the breaker rejects)
  reading ``TASKQ_BREAKER_THRESHOLD`` / ``TASKQ_BREAKER_COOLDOWN``
  from the environment (AC-FR-03.2, AC-FR-03.4).
* ``taskq_plus.service.executor.execute_task(store, task_id, *,
  sleep=time.sleep)`` retries a ``failed`` / ``timeout`` outcome up to
  ``TASKQ_RETRY_LIMIT`` times, waiting
  ``TASKQ_BACKOFF_BASE * 2 ** n`` seconds before retry ``n``
  (n = 0, 1, ...) via the INJECTED ``sleep`` callable — never
  ``time.sleep`` directly (AC-FR-03.1).
* The ``run`` CLI path calls ``breaker.assert_closed()`` BEFORE
  spawning any subprocess; while ``OPEN`` it writes ``breaker open``
  to stderr and returns exit code 3 (AC-FR-03.3; SAD §3.3 step 2).
* Only post-retry final failures feed ``breaker.record_failure``
  (SPEC.md §3 FR-03 「重試耗盡仍 failed/timeout」).

Test design follows the harness canonical pattern — bind the declared
TEST_SPEC Inputs to local variables, capture a single ``result`` record
per invocation, and emit each spec sub-assertion as a bare ``assert``
matching the predicate shape verbatim from TEST_SPEC.md.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# Top-level imports are intentional — ModuleNotFoundError at collection time
# is the expected RED signal per the unit-test contract.
from taskq_plus import cli  # noqa: E402
from taskq_plus.service import breaker  # noqa: E402
from taskq_plus.service import executor  # noqa: E402
from taskq_plus.storage import breaker_store  # noqa: E402
from taskq_plus.storage.task_store import TaskStore  # noqa: E402


# -------------------------------------------------------------------
# Fixtures — function-scoped so breaker.json state cannot leak between
# cases (an OPEN file from case 3 must not affect case 4/5).
# -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_taskq_home(monkeypatch, tmp_path):
    """Per-test $TASKQ_HOME isolation (TEST_SPEC state_mode=isolate_per_test)."""
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv("TASKQ_HOME", str(home))
    monkeypatch.setenv("TASKQ_AUDIT_LOG", str(home / "audit.jsonl"))
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "4")
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "2")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0.1")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "5.0")
    monkeypatch.setenv("TASKQ_CACHE_TTL", "3600")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "32")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "30")
    yield home


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _home() -> Path:
    """Return the isolated ``$TASKQ_HOME`` for the running test."""
    return Path(os.environ["TASKQ_HOME"])


def _breaker_path() -> Path:
    """Return ``$TASKQ_HOME/breaker.json`` (SAD §3.4)."""
    return _home() / "breaker.json"


def _seed_breaker(state: str, *, failure_count: int, opened_at: float | None):
    """Write the SAD §3.4 breaker envelope directly to disk.

    Seeding through the file (not through the service API) keeps the
    precondition independent of the implementation under test, and
    pins the on-disk schema the GREEN implementation must read.
    """
    payload = {
        "version": 1,
        "state": state,
        "failure_count": failure_count,
        "opened_at": opened_at,
    }
    _breaker_path().write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _read_breaker() -> dict:
    """Parse ``breaker.json``; empty dict when absent."""
    path = _breaker_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _task_store() -> TaskStore:
    """Return a ``TaskStore`` bound to the isolated home."""
    return TaskStore(_home() / "tasks.json")


def _cli(argv: list[str]):
    """Invoke ``cli.main(argv)`` in-process, capturing stdout/stderr.

    In-process (NOT subprocess) is the deliberate choice for cases 1-5:
    TEST_SPEC declares ``subprocess_mode="in_process"`` for them, and
    pytest-cov cannot measure coverage across a process boundary.
    """
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            exit_code = cli.main(argv)
            if exit_code is None:
                exit_code = 0
            if not isinstance(exit_code, int):
                exit_code = 1
        except SystemExit as exc:
            code = exc.code
            exit_code = code if isinstance(code, int) else 1
    return SimpleNamespace(
        exit_code=exit_code,
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
    )


def _submit(command: str) -> str:
    """Submit ``command`` via the CLI and return its 8-hex task id."""
    result = _cli(["submit", command])
    task_id = result.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{8}", task_id), (
        f"submit did not emit an 8-hex id: {result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    return task_id


def _child_env(home: Path) -> dict:
    """Compose subprocess env with $TASKQ_HOME + PYTHONPATH for the child.

    pytest's sys.path bootstrap (tests/conftest.py) does NOT propagate to
    a child interpreter, so ``03-development/src`` is pushed explicitly.
    """
    child_env = os.environ.copy()
    child_env["TASKQ_HOME"] = str(home)
    child_env["TASKQ_AUDIT_LOG"] = str(home / "audit.jsonl")
    src_root = Path(__file__).resolve().parents[2] / "03-development" / "src"
    child_env["PYTHONPATH"] = str(src_root) + os.pathsep + child_env.get(
        "PYTHONPATH", ""
    )
    return child_env


# -------------------------------------------------------------------
# FR-03 Case 1 — retry with exponential backoff (happy_path, in-process)
# -------------------------------------------------------------------


# NFR-02 — execution safety: retry path must use shell=False (the executor's
# run_subprocess is the same path the retry loop calls); NFR-03 — sleep is
# injected so no real wall-clock waits; NFR-09 — zero-skip (always runs).
# GREEN TODO: taskq_plus.service.executor must have
#   run_subprocess(command: str, timeout: float) -> subprocess.CompletedProcess
# and execute_task(store, task_id, *, sleep=time.sleep) -> tuple[str, dict|None]
# where every backoff wait goes through the injected ``sleep`` callable.
def test_fr03_retry_with_exponential_backoff(monkeypatch):
    """AC-FR-03.1 — retry up to TASKQ_RETRY_LIMIT with exponential backoff.

    SPEC.md §3 FR-03 verbatim: 結果為 ``failed``/``timeout`` 時自動重試,
    上限 ``TASKQ_RETRY_LIMIT`` 次;第 n 次重試前等待
    ``TASKQ_BACKOFF_BASE × 2^n`` 秒(sleep 函式必須可注入以利測試).
    """
    retry_limit = 2
    backoff_base = 0.1
    sleep_inject = "fake"
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", str(retry_limit))
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", str(backoff_base))

    task_id = _submit("false")

    invocations: list[str] = []

    def _always_failing(command, timeout):
        invocations.append(command)
        return subprocess.CompletedProcess(
            args=[command], returncode=1, stdout="", stderr="boom"
        )

    monkeypatch.setattr(executor, "run_subprocess", _always_failing)

    sleep_calls: list[float] = []

    def _fake_sleep(seconds):
        sleep_calls.append(seconds)

    status, record = executor.execute_task(
        _task_store(), task_id, sleep=_fake_sleep
    )

    result = SimpleNamespace(
        # attempt_count = number of RETRIES performed (initial invocation
        # excluded), which is what the spec caps at TASKQ_RETRY_LIMIT.
        attempt_count=max(0, len(invocations) - 1),
        invocations_total=len(invocations),
        sleep_calls=sleep_calls,
        task_status=status,
        record=record,
    )

    # AC3-retry-count-le-limit: `result.attempt_count <= retry_limit`
    assert result.attempt_count <= retry_limit
    # A failing task must exhaust the budget, not give up early.
    assert result.attempt_count == retry_limit
    assert result.invocations_total == retry_limit + 1
    # AC3-backoff-exponential:
    # `result.sleep_calls == [backoff_base, backoff_base * 2]`
    assert result.sleep_calls == pytest.approx([backoff_base, backoff_base * 2])
    # AC3-sleep-injectable: `sleep_inject == "fake"` — the executor waited
    # ONLY through the injected callable (real time.sleep would leave
    # sleep_calls empty).
    assert sleep_inject == "fake"
    assert len(result.sleep_calls) == retry_limit
    # Post-retry outcome is still a failure — this is what the breaker counts.
    assert result.task_status == "failed"


# -------------------------------------------------------------------
# FR-03 Case 2 — threshold persists OPEN (state_transition, in-process)
# -------------------------------------------------------------------


# NFR-03 — atomic persistence: breaker.json is written via
# atomic_write_json (tmp + os.replace); NFR-09 — zero-skip.
# GREEN TODO: taskq_plus.storage.breaker_store must have
#   save(state: dict) -> None  which calls the module-level name
#   ``atomic_write_json(path, payload)`` exactly once per transition.
def test_fr03_breaker_threshold_persists_open(monkeypatch):
    """AC-FR-03.2 — N consecutive final failures → OPEN, persisted atomically.

    SPEC.md §3 FR-03: 連續最終失敗(重試耗盡仍 failed/timeout)計數 ≥
    ``TASKQ_BREAKER_THRESHOLD`` → ``OPEN``;狀態持久化於
    ``$TASKQ_HOME/breaker.json``(原子寫).
    """
    threshold = 3
    final_failures = 3
    expected_state = "OPEN"
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", str(threshold))
    # Retries are orthogonal to this case; a single attempt per task keeps
    # "final failure" unambiguous (SPEC.md §3 FR-03 重試耗盡仍 failed).
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")

    write_count = {"n": 0}
    real_write = breaker_store.atomic_write_json

    def _counting_write(path, payload):
        if Path(path).name == "breaker.json":
            write_count["n"] += 1
        return real_write(path, payload)

    monkeypatch.setattr(breaker_store, "atomic_write_json", _counting_write)

    counts_seen: list[int] = []
    for _ in range(final_failures - 1):
        task_id = _submit("false")
        _cli(["run", task_id])
        counts_seen.append(int(_read_breaker().get("failure_count", -1)))

    # Count writes for the tripping transition only.
    write_count["n"] = 0
    tripping_task = _submit("false")
    _cli(["run", tripping_task])
    counts_seen.append(int(_read_breaker().get("failure_count", -1)))

    persisted = _read_breaker()
    result = SimpleNamespace(
        breaker_state=persisted.get("state"),
        failure_count=persisted.get("failure_count"),
        persist_writes=write_count["n"],
        counts_seen=counts_seen,
    )

    # AC3-threshold-persisted-open: `result.breaker_state == "OPEN"`
    assert result.breaker_state == expected_state
    assert result.failure_count >= threshold
    # AC3-atomic-persist: `result.persist_writes == 1`
    assert result.persist_writes == 1
    # P3-monotone-failure-count: `record_failure(c) == c + 1`
    assert result.counts_seen == [1, 2, 3]


# -------------------------------------------------------------------
# FR-03 Case 3 — OPEN rejects with exit 3 (validation, in-process)
# -------------------------------------------------------------------


# NFR-02 — execution safety: the breaker check must happen BEFORE any
# subprocess is spawned (this test pins that ordering); NFR-09 — zero-skip.
# GREEN TODO: the ``run`` CLI path must call
#   taskq_plus.service.breaker.assert_closed() BEFORE
#   taskq_plus.service.executor.run_subprocess(command, timeout) is reached.
def test_fr03_breaker_open_rejects_with_exit_3(monkeypatch):
    """AC-FR-03.3 — while OPEN, run exits 3 + stderr `breaker open`.

    SPEC.md §3 FR-03: ``OPEN`` 期間任何 ``run`` 立即拒絕:exit 3 +
    stderr ``breaker open``,不執行 subprocess (SPEC.md §8 #8 負向路徑).
    """
    initial_state = "OPEN"
    expected_exit = 3

    task_id = _submit("true")
    # OPEN and still inside the cooldown window → must reject outright.
    _seed_breaker(initial_state, failure_count=3, opened_at=time.time())

    spawned = {"n": 0}
    real_run = executor.run_subprocess

    def _counting_run(command, timeout):
        spawned["n"] += 1
        return real_run(command, timeout)

    monkeypatch.setattr(executor, "run_subprocess", _counting_run)

    cli_result = _cli(["run", task_id])

    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        subprocess_spawned=spawned["n"],
        stderr_text=cli_result.stderr,
    )

    # AC3-open-exit-3: `result.exit_code == 3`
    assert result.exit_code == expected_exit
    # SPEC.md §3 FR-03 verbatim stderr marker.
    assert "breaker open" in result.stderr_text
    # AC3-no-subprocess-on-open: `result.subprocess_spawned == 0`
    assert result.subprocess_spawned == 0
    # The rejected run must not mutate the task out of ``pending``.
    assert _task_store().load()[task_id]["status"] == "pending"


# -------------------------------------------------------------------
# FR-03 Case 4 — HALF_OPEN + success → CLOSED (state_transition, in-process)
# -------------------------------------------------------------------


# NFR-03 — atomic persistence: counter reset re-persists CLOSED atomically;
# NFR-09 — zero-skip.
def test_fr03_breaker_half_open_success_closes():
    """AC-FR-03.4 — HALF_OPEN admits one task; success → CLOSED, count 0.

    SPEC.md §3 FR-03: 經 cooldown 秒後進入 ``HALF_OPEN``:放行一個任務 —
    成功 → ``CLOSED`` 且計數歸零.
    """
    prior_state = "HALF_OPEN"
    admit_outcome = "success"
    expected_state = "CLOSED"
    counter_expected = 0

    task_id = _submit("true")
    _seed_breaker(prior_state, failure_count=3, opened_at=time.time() - 10.0)

    cli_result = _cli(["run", task_id])

    persisted = _read_breaker()
    # P3-record-success-noop-on-closed — the breaker is CLOSED now; another
    # success must leave the counter where it is (reset-to-zero only, never
    # decrement below zero).
    count_before_extra = int(persisted.get("failure_count", -1))
    count_after_extra = breaker.record_success()

    result = SimpleNamespace(
        breaker_state=persisted.get("state"),
        failure_count=count_before_extra,
        exit_code=cli_result.exit_code,
        state_after_extra_success=breaker.current_state(),
        count_after_extra=count_after_extra,
    )

    assert admit_outcome == "success"
    assert result.exit_code == 0
    # AC3-half-open-success-closes: `result.breaker_state == "CLOSED"`
    assert result.breaker_state == expected_state
    # AC3-half-open-counter-reset: `result.failure_count == counter_expected`
    assert result.failure_count == counter_expected
    # P3-record-success-noop-on-closed: `record_success(CLOSED, c) == c`
    assert result.count_after_extra == count_before_extra
    assert result.state_after_extra_success == expected_state
    # The admitted task actually ran (HALF_OPEN lets exactly one through).
    assert _task_store().load()[task_id]["status"] == "done"


# -------------------------------------------------------------------
# FR-03 Case 5 — HALF_OPEN + failure → OPEN (state_transition, in-process)
# -------------------------------------------------------------------


# NFR-03 — atomic persistence: failure re-opens and re-persists state
# atomically; NFR-09 — zero-skip.
def test_fr03_breaker_half_open_failure_reopens(monkeypatch):
    """AC-FR-03.4 — HALF_OPEN admits one task; failure → back to OPEN.

    SPEC.md §3 FR-03: ...失敗 → 重新 ``OPEN``.
    """
    prior_state = "HALF_OPEN"
    admit_outcome = "failure"
    expected_state = "OPEN"
    # One attempt per task so the admitted probe reaches its FINAL failure
    # immediately (SPEC.md §3 FR-03 重試耗盡仍 failed/timeout).
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")

    task_id = _submit("false")
    _seed_breaker(prior_state, failure_count=3, opened_at=time.time() - 10.0)

    _cli(["run", task_id])

    persisted = _read_breaker()
    result = SimpleNamespace(
        breaker_state=persisted.get("state"),
        failure_count=persisted.get("failure_count"),
        opened_at=persisted.get("opened_at"),
    )

    assert admit_outcome == "failure"
    # AC3-half-open-failure-reopens: `result.breaker_state == "OPEN"`
    assert result.breaker_state == expected_state
    # Re-opening restarts the cooldown clock.
    assert isinstance(result.opened_at, (int, float))
    # The probe failure is not forgiven — the counter stays at/above threshold.
    assert result.failure_count >= 1


# -------------------------------------------------------------------
# FR-03 Case 6 — recovery after cooldown (integration, out-of-process)
# -------------------------------------------------------------------


# NFR-02 — execution safety: subprocess uses shell=False explicitly;
# NFR-03 — atomic persistence (recovery path persists CLOSED state); NFR-09 — zero-skip.
def test_fr03_breaker_recovers_after_cooldown():
    """AC-FR-03.5 — after cooldown a task is admitted and succeeds (exit 0).

    SPEC.md §8 #8 正向路徑: 計數達閾值 → cooldown 過後 → 任務成功 →
    後續 run 正常放行.

    Out-of-process (subprocess) is the deliberate choice here: TEST_SPEC
    declares ``subprocess_mode="out_of_process"`` for this case, so the
    REAL ``python -m taskq_plus`` entry point is exercised end to end.

    Elapsed time is produced by back-dating ``opened_at`` rather than
    sleeping a real 5 seconds — ``now - opened_at`` is the quantity the
    cooldown comparison actually reads, so the assertion stays truthful
    while the suite stays fast.
    """
    threshold = 3
    cooldown_seconds = 5
    prior_state = "OPEN"

    home = _home()
    task_id = _submit("true")

    opened_at = time.time() - (cooldown_seconds + 0.5)
    _seed_breaker(prior_state, failure_count=threshold, opened_at=opened_at)

    completed = subprocess.run(
        [sys.executable, "-m", "taskq_plus", "run", task_id],
        capture_output=True,
        text=True,
        timeout=60,
        env=_child_env(home),
        cwd=str(Path(__file__).resolve().parents[2]),
        shell=False,
    )

    persisted = _read_breaker()
    result = SimpleNamespace(
        exit_code=completed.returncode,
        elapsed_seconds=time.time() - opened_at,
        breaker_state=persisted.get("state"),
        failure_count=persisted.get("failure_count"),
        stderr_text=completed.stderr,
    )

    # AC3-cooldown-elapsed: `result.elapsed_seconds >= cooldown_seconds`
    assert result.elapsed_seconds >= cooldown_seconds
    # AC3-recovery-exit-0: `result.exit_code == 0`
    assert result.exit_code == 0, (
        f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
    # Cooldown elapsed → the probe is admitted, so no rejection is emitted.
    assert "breaker open" not in result.stderr_text
    # Success on the admitted probe closes the breaker and zeroes the counter.
    assert result.breaker_state == "CLOSED"
    assert result.failure_count == 0
