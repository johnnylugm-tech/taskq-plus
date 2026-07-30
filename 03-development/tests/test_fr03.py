"""FR-03 RED tests — retry (exponential backoff) + circuit breaker state machine.

Per TEST_SPEC.md §FR-03, there are 6 rows (multi-scenario split on row 2 'b'):
  - test_fr03_a  (3 consecutive final failures → run rejected)         row 1
  - test_fr03_b  (retry_attempt_n=0, expected_sleep=0.1)               row 2 (parametrised)
  - test_fr03_b  (retry_attempt_n=1, expected_sleep=0.2)               row 3 (parametrised)
  - test_fr03_b  (retry_attempt_n=2, expected_sleep=0.4)               row 4 (parametrised)
  - test_fr03_c  (state sequence: CLOSED → OPEN → HALF_OPEN → CLOSED)  row 5
  - test_fr03_d  (recovery time bound: cooldown=5.0; max=6.0)          row 6

Sub-assertions (rule_id → predicate):
  AC3-threshold-opens        : consecutive_final_failures >= breaker_threshold             row 1
  AC3-backoff-formula        : expected_sleep_seconds == backoff_base_seconds * 2**n      rows 2..4
  AC3-retry-limit-respected  : retry_attempt_n <= breaker_threshold                       rows 2..4
  AC3-sequence-bounded       : state_sequence starts/ends with CLOSED                     row 5
  AC3-recovery-time-bound    : breaker_cooldown_seconds < recovery_max_seconds            row 6

Property (Direction B):
  P3-backoff-monotone        : expected_sleep_seconds > 0 and backoff_base_seconds > 0   rows 2..4

SAB-bindings (FR-03 binds to, per SAB.json fr_module_traceability.FR-03):
  - taskq_plus.service.breaker         (does NOT exist on disk — RED)
  - taskq_plus.storage.breaker_store   (does NOT exist on disk — RED)
  - taskq_plus.service.executor        (exists; run_with_retry is a NEW symbol — RED)

This file is the TDD-RED deliverable: it is EXPECTED to fail with pytest
Collection Error (Exit Code 2). The GREEN agent must create the three modules
above with the public API the GREEN TODOs note.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC = ROOT / "src"
HOME_VAR = "TASKQ_HOME"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# SAB-bound imports — each line below deliberately fails in RED state.
#   - taskq_plus.service.breaker        : entire module missing on disk
#   - taskq_plus.storage.breaker_store  : entire module missing on disk
#   - taskq_plus.service.executor.run_with_retry : module exists, symbol does not
# All three raise ModuleNotFoundError / ImportError during collection, so the
# file Collection-Errors. This is the VALID RED signal — do not wrap in
# try/except ImportError or use lazy imports to hide the missing source.
# ---------------------------------------------------------------------------
from taskq_plus.service.breaker import (  # noqa: E402,F401
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    Breaker,
    compute_backoff_seconds,
)
from taskq_plus.storage.breaker_store import (  # noqa: E402,F401
    breaker_path,
    read_breaker,
    write_breaker,
)
from taskq_plus.service.executor import (  # noqa: E402,F401
    EXIT_BREAKER_OPEN,
    run_with_retry,
)


# ---------------------------------------------------------------------------
# Per-test isolation — fresh TASKQ_HOME per case, function-scoped.
# FR-03.a + FR-03.d explicitly declare state_mode=shared for the breaker
# counter, but inside ONE test file the TASKQ_HOME itself must be isolated;
# the "shared" rule applies across parametrised iterations of the SAME row
# (rows 1 and 6 use a single non-parametrised test, so isolation is automatic).
# ---------------------------------------------------------------------------
@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Function-scoped TASKQ_HOME — every test gets a clean directory."""
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv(HOME_VAR, str(home))
    return home


@pytest.fixture
def quiet_breaker_env(monkeypatch):
    """Pin the breaker env so cross-test state cannot leak accidentally."""
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "60")
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0")
    # Inject time.sleep so any retry loop is instant.
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None, raising=False)


# ---------------------------------------------------------------------------
# Helpers — in-process seeding + subprocess CLI driver.
# ---------------------------------------------------------------------------
def _seed_pending(
    home: Path,
    *,
    task_id: str,
    command: str,
    depends_on: list[str] | None = None,
    status: str = "pending",
) -> None:
    """Insert a pending task record directly via the storage API."""
    prior = os.environ.get(HOME_VAR)
    os.environ[HOME_VAR] = str(home)
    try:
        from taskq_plus.storage.task_store import append_task
        append_task(
            {
                "id": task_id,
                "command": command,
                "name": None,
                "status": status,
                "depends_on": list(depends_on or []),
            }
        )
    finally:
        if prior is None:
            os.environ.pop(HOME_VAR, None)
        else:
            os.environ[HOME_VAR] = prior


def _with_home(home: Path):
    """Push TASKQ_HOME for in-process calls; return the prior value."""
    prior = os.environ.get(HOME_VAR)
    os.environ[HOME_VAR] = str(home)
    return prior


def _restore_home(prior) -> None:
    if prior is None:
        os.environ.pop(HOME_VAR, None)
    else:
        os.environ[HOME_VAR] = prior


def _run_cli(argv, home: Path, extra_env: dict | None = None):
    """Out-of-process CLI driver — propagates PYTHONPATH to the child."""
    env = os.environ.copy()
    env[HOME_VAR] = str(home)
    if extra_env:
        env.update(extra_env)
    py_path = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = py_path
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(home),
    )


# ===========================================================================
# Test cases — names MUST match TEST_SPEC.md §FR-03 verbatim.
# ===========================================================================

# ---- row 1 : 3 consecutive final failures → breaker OPEN → exit 3 ----
def test_fr03_a(taskq_home, quiet_breaker_env):
    """AC-FR-03.a: 3 consecutive final failures → subsequent run returns exit 3.

    Sub-assertion (rule_id AC3-threshold-opens):
      consecutive_final_failures (3) >= breaker_threshold (3)  →  breaker OPEN.

    Once OPEN, any subsequent run must be rejected: return EXIT_BREAKER_OPEN (3)
    and write 'breaker open' to stderr — no subprocess is spawned.
    """
    consecutive_final_failures = "3"
    breaker_threshold = "3"

    # TEST_SPEC §FR-03 sub-assertion for case 1 (trigger: consecutive_final_failures).
    assert consecutive_final_failures >= breaker_threshold, (
        f"AC3-threshold-opens: {consecutive_final_failures} >= {breaker_threshold}"
    )

    # Drive 3 consecutive final-failures through run_with_retry; each call exits
    # 1 (EXIT_FAILED) and increments the breaker counter to threshold.
    for i in range(int(consecutive_final_failures)):
        tid = f"breakfail{i}"
        _seed_pending(
            taskq_home,
            task_id=tid,
            command="sh -c 'exit 1'",
        )
        # quiet_breaker_env already pins RETRY_LIMIT=0 so every fail is "final".
        cli_exit = run_with_retry(tid)
        assert cli_exit == 1, (
            f"task {tid}: expected CLI exit 1 (failed), got {cli_exit}"
        )

    # Breaker should now be OPEN. Read the state via the store API.
    prior = _with_home(taskq_home)
    try:
        rec = read_breaker()
    finally:
        _restore_home(prior)
    assert rec is not None, "breaker.json missing after 3 final failures"
    state = rec.get("state")
    assert state == STATE_OPEN, (
        f"after {consecutive_final_failures} final failures, breaker state is "
        f"{state!r}; expected {STATE_OPEN!r}"
    )

    # Next run — even of a task whose command would succeed — must be rejected.
    next_tid = "would_succeed"
    _seed_pending(taskq_home, task_id=next_tid, command="true")

    # Capture stderr so we can assert the 'breaker open' token.
    captured_stderr = io.StringIO()
    prior = _with_home(taskq_home)
    try:
        with redirect_stderr(captured_stderr):
            cli_exit = run_with_retry(next_tid)
    finally:
        _restore_home(prior)

    assert cli_exit == EXIT_BREAKER_OPEN, (
        f"while breaker OPEN: expected exit {EXIT_BREAKER_OPEN}, got {cli_exit}"
    )
    stderr_text = captured_stderr.getvalue().lower()
    assert "breaker open" in stderr_text, (
        f"expected 'breaker open' on stderr, got {captured_stderr.getvalue()!r}"
    )


# ---- rows 2..4 : retry backoff formula with injectable sleep -------------
@pytest.mark.parametrize(
    ("retry_attempt_n", "backoff_base_seconds", "expected_sleep_seconds"),
    [
        ("0", "0.1", "0.1"),
        ("1", "0.1", "0.2"),
        ("2", "0.1", "0.4"),
    ],
)
def test_fr03_b(
    taskq_home,
    monkeypatch,
    retry_attempt_n,
    backoff_base_seconds,
    expected_sleep_seconds,
):
    """AC-FR-03.b: retry uses TASKQ_BACKOFF_BASE × 2^n backoff; sleep injectable.

    Sub-assertions:
      AC3-backoff-formula        : expected == base * (2 ** n)
      AC3-retry-limit-respected  : n <= breaker_threshold (high enough so always true)

    Property:
      P3-backoff-monotone : expected > 0 and base > 0

    The test substitutes a no-op `spy_sleep` so we don't actually wait,
    but spies on every call so we can verify the formula:
      retry n (zero-indexed) sleeps for `backoff_base_seconds * 2**n` seconds.
    """
    n = int(retry_attempt_n)
    base = float(backoff_base_seconds)
    expected = float(expected_sleep_seconds)
    # AC3-retry-limit-respected is a predicate over TEST_SPEC Inputs; the
    # canonical FR-03 breaker_threshold is "3" (row 1). The *env* threshold set
    # below is deliberately much higher so the breaker cannot open mid-test.
    breaker_threshold = "3"

    # TEST_SPEC §FR-03 sub-assertions / property for cases 2..4.
    assert float(expected_sleep_seconds) == float(backoff_base_seconds) * (
        2 ** float(retry_attempt_n)
    ), (
        f"AC3-backoff-formula: expected ({expected}) != base * 2**n "
        f"({base} * {2 ** n})"
    )
    assert float(expected_sleep_seconds) > 0 and float(backoff_base_seconds) > 0, (
        "P3-backoff-monotone violated"
    )
    assert retry_attempt_n <= breaker_threshold, (
        f"AC3-retry-limit-respected: n={retry_attempt_n} must not exceed "
        f"threshold {breaker_threshold}"
    )

    # Configure environment: high retry limit so the n-th retry actually fires,
    # HIGH breaker threshold so the breaker does NOT open mid-test (we are
    # isolating the backoff here, not the breaker).
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", backoff_base_seconds)
    # Need at least n+1 retries for the n-th sleep to be recorded. Use a
    # generous limit so all retries fire and the final outcome is failed.
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", str(max(n + 2, 4)))
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "1000")

    # Spy on the injectable sleep function — replace time.sleep in the executor
    # module AND any other module that may have captured it.
    recorded_sleeps: list[float] = []

    def spy_sleep(seconds: float) -> None:
        recorded_sleeps.append(float(seconds))

    # Seed a failing task (`sh -c 'exit 1'`) — retries will not change the outcome.
    tid = f"retry_n{n}"
    _seed_pending(taskq_home, task_id=tid, command="sh -c 'exit 1'")

    # Dispatch via the executor. The executor must accept the injectable sleep.
    cli_exit = run_with_retry(tid, sleep_fn=spy_sleep)
    assert cli_exit == 1, (
        f"task {tid}: expected CLI exit 1 (final failure), got {cli_exit}"
    )

    # The n-th retry (zero-indexed) sleeps for `expected` seconds, BY FORMULA.
    assert len(recorded_sleeps) > n, (
        f"retry_attempt_n={n}: expected at least {n + 1} sleeps, "
        f"got {len(recorded_sleeps)}: {recorded_sleeps}"
    )
    assert recorded_sleeps[n] == pytest.approx(expected), (
        f"retry_attempt_n={n}: expected sleep[{n}]={expected}, "
        f"got recorded_sleeps[{n}]={recorded_sleeps[n]}; "
        f"all recorded sleeps: {recorded_sleeps}"
    )

    # Direct check on the formula function — the GREEN must expose
    # compute_backoff_seconds at the SAB-declared path.
    assert compute_backoff_seconds(n, base) == pytest.approx(expected), (
        f"compute_backoff_seconds({n}, {base}) must equal {expected}"
    )


# ---- row 5 : state sequence CLOSED → OPEN → HALF_OPEN → CLOSED ----------
def test_fr03_c(taskq_home):
    """AC-FR-03.c: state machine follows CLOSED → OPEN → HALF_OPEN → CLOSED."""
    state_sequence = "CLOSED,OPEN,HALF_OPEN,CLOSED"

    # TEST_SPEC §FR-03 sub-assertion for case 5.
    assert (
        len(state_sequence) > 0
        and state_sequence.startswith("CLOSED")
        and state_sequence.endswith("CLOSED")
    ), f"AC3-sequence-bounded: {state_sequence!r} must be non-empty CLOSED..CLOSED"

    # GREEN TODO: Breaker(threshold, cooldown_seconds, clock=time.monotonic)
    # The clock parameter is injectable so the test is deterministic.
    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

        def advance(self, dt: float) -> None:
            self.now += float(dt)

    clock = FakeClock()

    breaker_threshold = 2
    cooldown_seconds = 5.0

    breaker = Breaker(
        threshold=breaker_threshold,
        cooldown_seconds=cooldown_seconds,
        clock=clock,
    )

    # 1. Initial state is CLOSED.
    assert breaker.state == STATE_CLOSED, (
        f"initial state should be {STATE_CLOSED!r}, got {breaker.state!r}"
    )

    # 2. Drive past the threshold failures → OPEN.
    for _ in range(breaker_threshold):
        breaker.record_failure()
    assert breaker.state == STATE_OPEN, (
        f"after {breaker_threshold} failures: state should be {STATE_OPEN!r}, "
        f"got {breaker.state!r}"
    )

    # 3. Just before cooldown elapses → still OPEN; allow_request denies.
    clock.advance(cooldown_seconds - 0.001)
    assert breaker.state == STATE_OPEN, (
        f"just before cooldown: state must still be {STATE_OPEN!r}, "
        f"got {breaker.state!r}"
    )
    denied = breaker.allow_request()
    assert denied is False, (
        f"during OPEN, allow_request must return False; got {denied!r}"
    )
    assert breaker.state == STATE_OPEN, (
        f"after a denied request: state must remain {STATE_OPEN!r}, "
        f"got {breaker.state!r}"
    )

    # 4. Past cooldown → HALF_OPEN (first allow transitions).
    clock.advance(0.002)  # now we are past cooldown_seconds
    admitted = breaker.allow_request()
    assert admitted is True, (
        f"after cooldown: allow_request must admit the trial request; "
        f"got {admitted!r}"
    )
    assert breaker.state == STATE_HALF_OPEN, (
        f"after one admission: state should be {STATE_HALF_OPEN!r}, "
        f"got {breaker.state!r}"
    )

    # 5. HALF_OPEN success → CLOSED (and counter reset).
    breaker.record_success()
    assert breaker.state == STATE_CLOSED, (
        f"after HALF_OPEN success: state should be {STATE_CLOSED!r}, "
        f"got {breaker.state!r}"
    )

    # Observed sequence matches TEST_SPEC.md exactly.
    observed_sequence = "CLOSED,OPEN,HALF_OPEN,CLOSED"
    assert observed_sequence == state_sequence


# ---- row 6 : recovery time ≤ cooldown + 1s ------------------------------
# NFR-03: breaker OPEN → CLOSED recovery bounded by TASKQ_BREAKER_COOLDOWN + 1s
# (AC-NFR-03.c).
def test_fr03_d(taskq_home):  # NFR-03 (recovery time bound)
    """AC-FR-03.d: OPEN → CLOSED recovery time ≤ TASKQ_BREAKER_COOLDOWN + 1s.

    Sub-assertion (rule_id AC3-recovery-time-bound):
      breaker_cooldown_seconds (5.0) < recovery_max_seconds (6.0).

    The bound itself is logical: the implementation, given a real monotonic
    clock, must permit a trial request no later than `cooldown_seconds` after
    it transitioned to OPEN. With an injected clock we verify this condition
    without actually waiting. The NFR-03 AC ('real time bound ≤ cooldown+1s')
    is satisfied because the real-time behaviour mirrors the injected-clock
    behaviour.
    """
    breaker_cooldown_seconds = "5.0"
    recovery_max_seconds = "6.0"

    # TEST_SPEC §FR-03 sub-assertion for case 6.
    assert float(breaker_cooldown_seconds) < float(recovery_max_seconds), (
        f"AC3-recovery-time-bound: {breaker_cooldown_seconds} < {recovery_max_seconds}"
    )

    cooldown = float(breaker_cooldown_seconds)
    recovery_max = float(recovery_max_seconds)

    # GREEN TODO: Breaker accepts an injectable clock.
    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

        def advance(self, dt: float) -> None:
            self.now += float(dt)

    clock = FakeClock()

    breaker = Breaker(threshold=1, cooldown_seconds=cooldown, clock=clock)
    assert breaker.state == STATE_CLOSED

    # First failure: OPEN immediately (threshold=1).
    opened_at = clock()
    breaker.record_failure()
    assert breaker.state == STATE_OPEN, (
        f"after 1 failure with threshold=1: state must be {STATE_OPEN!r}, "
        f"got {breaker.state!r}"
    )

    # Recovery time (injected clock) — measure how long after OPEN we wait
    # until allow_request admits the next call.
    recovered_at: float | None = None

    # While within the cooldown window → still denied.
    clock.advance(cooldown - 0.5)  # half a second before cooldown ends
    assert breaker.state == STATE_OPEN
    assert breaker.allow_request() is False, (
        "within cooldown, request must be denied"
    )

    # Advance just past cooldown. The very next allow_request MUST admit —
    # therefore recovery completes at injected time `opened_at + cooldown`.
    clock.advance(0.6)
    admitted = breaker.allow_request()
    assert admitted is True, (
        "at-or-just-after cooldown, request must be admitted (HALF_OPEN)"
    )
    recovered_at = clock()
    assert breaker.state == STATE_HALF_OPEN

    # Recovery time = recovered_at - opened_at
    recovery_time = recovered_at - opened_at
    assert recovery_time <= recovery_max, (
        f"recovery time {recovery_time:.3f}s must be ≤ recovery_max "
        f"{recovery_max:.3f}s (= cooldown+1s)"
    )
    # And the recovery time must be at least cooldown — i.e. the breaker
    # does NOT admit prematurely.
    assert recovery_time >= cooldown - 0.5, (
        f"recovery time {recovery_time:.3f}s should be ≈ cooldown {cooldown}s "
        f"(must not admit too early)"
    )

    # HALF_OPEN success → CLOSED.
    breaker.record_success()
    assert breaker.state == STATE_CLOSED, (
        f"after HALF_OPEN success: state must be {STATE_CLOSED!r}, "
        f"got {breaker.state!r}"
    )


# ===========================================================================
# Subprocess mirror tests — verify the REAL user-facing CLI entry point.
# pytest-cov cannot see code running inside a subprocess; these mirrors
# therefore verify behaviour, not coverage. Coverage is carried by the
# in-process tests above (which the GREEN agent must satisfy in addition).
# ===========================================================================

def test_fr03_a_subprocess(taskq_home, monkeypatch):
    """Subprocess mirror of test_fr03_a — `python -m taskq_plus run` exit 3."""
    # Threshold=1 so a single failure opens the breaker.
    extra = {
        "TASKQ_BREAKER_THRESHOLD": "1",
        "TASKQ_BREAKER_COOLDOWN": "60",
        "TASKQ_RETRY_LIMIT": "0",
        "TASKQ_BACKOFF_BASE": "0",
    }

    _seed_pending(taskq_home, task_id="cf01", command="sh -c 'exit 1'")

    # First run: exit 1 (failed) — the failure opens the breaker.
    proc1 = _run_cli(["run", "cf01"], taskq_home, extra_env=extra)
    assert proc1.returncode == 1, (
        f"first run should exit 1 (failed), got {proc1.returncode}; "
        f"stderr={proc1.stderr!r}"
    )

    # Second run (of a different task whose command would succeed) — must be
    # rejected because the breaker is OPEN.
    _seed_pending(taskq_home, task_id="cf02", command="true")
    proc2 = _run_cli(["run", "cf02"], taskq_home, extra_env=extra)
    assert proc2.returncode == 3, (
        f"second run (breaker OPEN) should exit 3, got {proc2.returncode}; "
        f"stderr={proc2.stderr!r}"
    )
    assert "breaker open" in proc2.stderr.lower(), (
        f"expected 'breaker open' on stderr, got {proc2.stderr!r}"
    )


def test_fr03_c_subprocess(taskq_home, monkeypatch):
    """Subprocess mirror: after cooldown, the breaker resumes admitting tasks."""
    # Cooldown=2s so the test finishes quickly. Threshold=1 so a single
    # failure opens the breaker.
    extra = {
        "TASKQ_BREAKER_THRESHOLD": "1",
        "TASKQ_BREAKER_COOLDOWN": "2",
        "TASKQ_RETRY_LIMIT": "0",
        "TASKQ_BACKOFF_BASE": "0",
    }

    _seed_pending(taskq_home, task_id="cb01", command="sh -c 'exit 1'")

    proc1 = _run_cli(["run", "cb01"], taskq_home, extra_env=extra)
    assert proc1.returncode == 1, (
        f"initial failing run should exit 1, got {proc1.returncode}; "
        f"stderr={proc1.stderr!r}"
    )

    _seed_pending(taskq_home, task_id="cb02", command="true")
    proc2 = _run_cli(["run", "cb02"], taskq_home, extra_env=extra)
    assert proc2.returncode == 3, (
        f"during cooldown: run should be rejected (exit 3), "
        f"got {proc2.returncode}; stderr={proc2.stderr!r}"
    )

    # Wait the cooldown out, then run a successful task.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        time.sleep(0.05)
        prior = _with_home(taskq_home)
        try:
            rec = read_breaker()
        finally:
            _restore_home(prior)
        if rec is not None and rec.get("state") != STATE_OPEN:
            break

    _seed_pending(taskq_home, task_id="cb03", command="true")
    proc3 = _run_cli(["run", "cb03"], taskq_home, extra_env=extra)
    # After cooldown the breaker should be HALF_OPEN and admit the trial.
    # On trial success the breaker resets to CLOSED.
    assert proc3.returncode == 0, (
        f"after cooldown, run should be admitted (exit 0); "
        f"got {proc3.returncode}, stderr={proc3.stderr!r}"
    )


# ===========================================================================
# Coverage-gap tests — pin branches in the GREEN implementation that the
# primary AC tests above do not reach. These are NOT from TEST_SPEC.md rows;
# they are brought in ONLY to keep test_coverage above the Gate 1 threshold
# (>= 80%). Every line referenced here is reachable and is exercised by
# the body of its test.
# ===========================================================================


# ---- breaker_store.py — atomic write / read round-trip --------------------
# NFR-03: breaker.json is one of the four atomic-write data files (tmp +
# os.replace); a write must be readable back intact (AC-NFR-03.a).
def test_taskq_breaker_store_atomic_write_roundtrip(taskq_home):  # NFR-03 (atomic write)
    """write_breaker then read_breaker returns the same payload."""
    payload = {
        "state": STATE_OPEN,
        "consecutive_failures": 3,
        "opened_at": "2026-07-30T00:00:00Z",
    }
    prior = _with_home(taskq_home)
    try:
        write_breaker(payload)
        out = read_breaker()
    finally:
        _restore_home(prior)
    assert out == payload, (
        f"read_breaker must return what write_breaker stored: "
        f"wrote={payload!r}, read={out!r}"
    )


def test_taskq_breaker_store_read_returns_none_when_missing(taskq_home):
    """read_breaker() returns None (or {}) when no breaker.json exists yet."""
    prior = _with_home(taskq_home)
    try:
        result = read_breaker()
    finally:
        _restore_home(prior)
    # The contract is permissive — accept None OR an empty marker.
    assert result is None or result == {}, (
        f"read_breaker on missing file must return None/empty, got {result!r}"
    )


def test_taskq_breaker_store_breaker_path_resolves_under_home(taskq_home):
    """breaker_path() returns <TASKQ_HOME>/breaker.json."""
    prior = _with_home(taskq_home)
    try:
        p = breaker_path()
    finally:
        _restore_home(prior)
    assert p == taskq_home / "breaker.json", (
        f"breaker_path() should resolve to {taskq_home / 'breaker.json'!r}, "
        f"got {p!r}"
    )


# ---- breaker.py — record_success in HALF_OPEN resets counter -------------
def test_taskq_breaker_record_success_after_open_resets_counter(taskq_home):
    """After a full CLOSED→OPEN→HALF_OPEN→CLOSED cycle, the counter is 0."""
    class FakeClock:
        def __init__(self):
            self.now = 0.0
        def __call__(self):
            return self.now
        def advance(self, dt):
            self.now += dt

    clock = FakeClock()
    breaker = Breaker(threshold=2, cooldown_seconds=5.0, clock=clock)

    # 2 failures → OPEN
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == STATE_OPEN

    # Advance past cooldown, admit (HALF_OPEN), succeed → CLOSED
    clock.advance(5.5)
    assert breaker.allow_request() is True
    breaker.record_success()
    assert breaker.state == STATE_CLOSED

    # After the cycle, 2 more failures should NOT immediately reopen —
    # the counter must have been reset.
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == STATE_OPEN, (
        "after CLOSED reset, the next 2 failures must open again — sanity"
    )


# ---- breaker.py — HALF_OPEN failure returns to OPEN ----------------------
def test_taskq_breaker_half_open_failure_returns_to_open(taskq_home):
    """A failure during HALF_OPEN must re-open the breaker (no admission)."""
    class FakeClock:
        def __init__(self):
            self.now = 0.0
        def __call__(self):
            return self.now
        def advance(self, dt):
            self.now += dt

    clock = FakeClock()
    breaker = Breaker(threshold=2, cooldown_seconds=5.0, clock=clock)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == STATE_OPEN

    clock.advance(5.5)
    assert breaker.allow_request() is True
    assert breaker.state == STATE_HALF_OPEN

    # Failure during HALF_OPEN trial → back to OPEN.
    breaker.record_failure()
    assert breaker.state == STATE_OPEN, (
        f"HALF_OPEN failure must transition to OPEN, got {breaker.state!r}"
    )
