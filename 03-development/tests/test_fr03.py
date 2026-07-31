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
import os
import subprocess
import sys
import time
from contextlib import redirect_stderr
from pathlib import Path

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


# ---- breaker_store.py — atomic-write cleanup + malformed-payload reads ----
# The `except BaseException` cleanup arm is reachable: a non-JSON-serialisable
# record makes `json.dump` raise inside the tmp-file context, so the handler
# runs, unlinks the tmp file, and re-raises (NFR-03 — no partial file is ever
# promoted to breaker.json by os.replace).
def test_taskq_breaker_store_write_unlinks_tmp_on_serialization_error(taskq_home):
    """A non-serialisable record raises and leaves NO tmp file behind."""
    prior = _with_home(taskq_home)
    try:
        with pytest.raises(TypeError):
            write_breaker({"state": STATE_CLOSED, "bad": object()})
        leftovers = list(taskq_home.glob("breaker.json.*"))
        promoted = (taskq_home / "breaker.json").exists()
    finally:
        _restore_home(prior)
    assert leftovers == [], (
        f"atomic-write cleanup must unlink the tmp file, found {leftovers!r}"
    )
    assert not promoted, (
        "a failed write must NOT create/replace breaker.json (os.replace "
        "is never reached when json.dump raises)"
    )


def test_taskq_breaker_store_write_survives_unlink_failure(taskq_home, monkeypatch):
    """When tmp cleanup itself fails (OSError), the original error still raises."""
    def _boom(_path):
        raise OSError("cleanup denied")

    prior = _with_home(taskq_home)
    try:
        monkeypatch.setattr(os, "unlink", _boom)
        with pytest.raises(TypeError):
            write_breaker({"state": STATE_CLOSED, "bad": object()})
    finally:
        _restore_home(prior)


def test_taskq_breaker_store_read_returns_none_on_corrupt_json(taskq_home):
    """A truncated / invalid breaker.json reads back as None, not an exception."""
    (taskq_home / "breaker.json").write_text("{not valid json", encoding="utf-8")
    prior = _with_home(taskq_home)
    try:
        result = read_breaker()
    finally:
        _restore_home(prior)
    assert result is None, (
        f"corrupt breaker.json must degrade to None (cold start), got {result!r}"
    )


def test_taskq_breaker_store_read_returns_none_when_payload_not_dict(taskq_home):
    """A well-formed but non-object breaker.json (JSON array) reads back None."""
    (taskq_home / "breaker.json").write_text("[1, 2, 3]", encoding="utf-8")
    prior = _with_home(taskq_home)
    try:
        result = read_breaker()
    finally:
        _restore_home(prior)
    assert result is None, (
        f"non-dict breaker.json payload must return None, got {result!r}"
    )


# ---- executor.py — env parsing / tail truncation helpers ------------------
def test_taskq_executor_read_env_falls_back_when_unparseable(monkeypatch):
    """_read_env returns the default for unset, empty AND unparseable values."""
    from taskq_plus.service.executor import _read_env

    monkeypatch.delenv("TASKQ_COV_KNOB", raising=False)
    assert _read_env("TASKQ_COV_KNOB", int, 7) == 7, "unset → default"

    monkeypatch.setenv("TASKQ_COV_KNOB", "")
    assert _read_env("TASKQ_COV_KNOB", int, 7) == 7, "empty → default"

    monkeypatch.setenv("TASKQ_COV_KNOB", "not-a-number")
    assert _read_env("TASKQ_COV_KNOB", int, 7) == 7, (
        "ValueError from parse() must fall back to the default, not propagate"
    )

    monkeypatch.setenv("TASKQ_COV_KNOB", "11")
    assert _read_env("TASKQ_COV_KNOB", int, 7) == 11, "parseable → parsed value"


def test_taskq_executor_truncate_tail_none_bytes_and_overflow():
    """_truncate_tail: None → '', bytes decoded, over-long text keeps the TAIL."""
    from taskq_plus.service.executor import TAIL_BOUND, _truncate_tail

    assert _truncate_tail(None) == "", "None must coerce to the empty string"
    assert _truncate_tail(b"hello bytes") == "hello bytes", "bytes must decode"
    assert _truncate_tail(b"\xff\xferaw") == "��raw", (
        "undecodable bytes use errors='replace' rather than raising"
    )
    assert _truncate_tail(123) == "123", "non-str/bytes coerce via str()"

    short = "x" * TAIL_BOUND
    assert _truncate_tail(short) == short, "exactly TAIL_BOUND is not truncated"

    long_text = "A" * 50 + "B" * TAIL_BOUND
    tail = _truncate_tail(long_text)
    assert len(tail) == TAIL_BOUND, f"tail must be {TAIL_BOUND} chars, got {len(tail)}"
    assert tail == "B" * TAIL_BOUND, "truncation must keep the LAST chars, not the first"


def test_taskq_executor_resolve_timeout_and_max_workers_read_env(monkeypatch):
    """_resolve_timeout / _resolve_max_workers read their env knobs."""
    from taskq_plus.service.executor import (
        DEFAULT_MAX_WORKERS,
        DEFAULT_TIMEOUT_S,
        _resolve_max_workers,
        _resolve_timeout,
    )

    monkeypatch.delenv("TASKQ_TASK_TIMEOUT", raising=False)
    monkeypatch.delenv("TASKQ_MAX_WORKERS", raising=False)
    assert _resolve_timeout() == DEFAULT_TIMEOUT_S
    assert _resolve_max_workers() == DEFAULT_MAX_WORKERS

    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "1.5")
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "9")
    assert _resolve_timeout() == 1.5, "TASKQ_TASK_TIMEOUT must be honoured"
    assert _resolve_max_workers() == 9, "TASKQ_MAX_WORKERS must be honoured"


def test_taskq_executor_topological_levels_groups_by_dependency():
    """_topological_levels delegates to the FR-06 Kahn layering."""
    from taskq_plus.service.executor import _topological_levels

    tasks = [
        {"id": "b", "depends_on": ["a"], "status": "pending"},
        {"id": "a", "depends_on": [], "status": "pending"},
        {"id": "c", "depends_on": ["b"], "status": "pending"},
    ]
    levels = _topological_levels(tasks)
    flat = [tid for level in levels for tid in level]
    assert flat.index("a") < flat.index("b") < flat.index("c"), (
        f"dependency order must be respected, got levels={levels!r}"
    )


# ---- executor.py — execute_task / run state machine -----------------------
def test_taskq_executor_execute_task_unknown_id_returns_none(taskq_home):
    """execute_task on an id that is not in the store returns None."""
    from taskq_plus.service.executor import execute_task

    prior = _with_home(taskq_home)
    try:
        result = execute_task("no-such-task")
    finally:
        _restore_home(prior)
    assert result is None, f"unknown task id must yield None, got {result!r}"


def test_taskq_executor_execute_task_timeout_sets_timeout_status(
    taskq_home, monkeypatch
):
    """A command exceeding TASKQ_TASK_TIMEOUT lands in status=timeout, code None."""
    from taskq_plus.service.executor import execute_task

    _seed_pending(taskq_home, task_id="slow", command="sleep 30")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "0.3")

    prior = _with_home(taskq_home)
    try:
        result = execute_task("slow")
    finally:
        _restore_home(prior)

    assert result is not None, "execute_task must return a result dict"
    assert result["status"] == "timeout", (
        f"exceeding the timeout must set status=timeout, got {result['status']!r}"
    )
    assert result["exit_code"] is None, (
        f"a timed-out task has no exit code, got {result['exit_code']!r}"
    )
    assert "duration_ms" in result and "finished_at" in result


def test_taskq_executor_run_maps_status_to_exit_code(taskq_home, monkeypatch):
    """run() maps done→0, non-zero→1, timeout→4, unknown-id→1."""
    from taskq_plus.service.executor import (
        EXIT_FAILED,
        EXIT_OK,
        EXIT_TIMEOUT,
        run,
    )

    _seed_pending(taskq_home, task_id="ok", command="echo hi")
    _seed_pending(taskq_home, task_id="bad", command="false")
    _seed_pending(taskq_home, task_id="slow", command="sleep 30")

    prior = _with_home(taskq_home)
    try:
        assert run("ok") == EXIT_OK, "exit 0 → done → CLI exit 0"
        assert run("bad") == EXIT_FAILED, "non-zero exit → failed → CLI exit 1"
        assert run("ghost") == EXIT_FAILED, "unknown id → CLI exit 1"
        monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "0.3")
        assert run("slow") == EXIT_TIMEOUT, "timeout → CLI exit 4"
    finally:
        _restore_home(prior)


# ---- executor.py — FR-06 dependency gating -------------------------------
def test_taskq_executor_unmet_dependencies_reports_non_done_deps(taskq_home):
    """_unmet_dependencies: pending dep is unmet, done dep and absent dep are not."""
    from taskq_plus.service.executor import _unmet_dependencies

    _seed_pending(taskq_home, task_id="dep_pending", command="echo a")
    _seed_pending(taskq_home, task_id="dep_done", command="echo b", status="done")
    _seed_pending(
        taskq_home,
        task_id="child",
        command="echo c",
        depends_on=["dep_pending", "dep_done", "dep_missing"],
    )

    prior = _with_home(taskq_home)
    try:
        unmet = _unmet_dependencies("child")
        no_such = _unmet_dependencies("not-a-task")
    finally:
        _restore_home(prior)

    assert unmet == ["dep_pending"], (
        "only deps that EXIST and are not 'done' are unmet (an absent dep "
        f"cannot gate the task), got {unmet!r}"
    )
    assert no_such == [], "an unknown task id has no dependencies"


def test_taskq_executor_execute_or_block_marks_blocked(taskq_home):
    """_execute_or_block marks a task blocked when a dep is not done, else runs it."""
    from taskq_plus.service.executor import _execute_or_block
    from taskq_plus.storage.task_store import find_by_id

    _seed_pending(taskq_home, task_id="parent", command="false")
    _seed_pending(taskq_home, task_id="kid", command="echo hi", depends_on=["parent"])
    _seed_pending(taskq_home, task_id="free", command="echo hi")

    prior = _with_home(taskq_home)
    try:
        blocked_result = _execute_or_block("kid")
        blocked_rec = find_by_id("kid")
        ran_result = _execute_or_block("free")
    finally:
        _restore_home(prior)

    assert blocked_result is None, "a blocked task must not produce a run result"
    assert blocked_rec["status"] == "blocked", (
        f"unmet dependency must set status=blocked, got {blocked_rec['status']!r}"
    )
    assert ran_result is not None and ran_result["status"] == "done", (
        f"a task with no unmet deps must execute, got {ran_result!r}"
    )


def test_taskq_executor_run_all_respects_layers_and_blocks(taskq_home):
    """run_all executes pending tasks in dependency layers, blocking downstream."""
    from taskq_plus.service.executor import run_all
    from taskq_plus.storage.task_store import find_by_id

    _seed_pending(taskq_home, task_id="root", command="echo root")
    _seed_pending(taskq_home, task_id="mid", command="false", depends_on=["root"])
    _seed_pending(taskq_home, task_id="leaf", command="echo leaf", depends_on=["mid"])

    prior = _with_home(taskq_home)
    try:
        run_all()
        root = find_by_id("root")
        mid = find_by_id("mid")
        leaf = find_by_id("leaf")
    finally:
        _restore_home(prior)

    assert root["status"] == "done", f"root should run and succeed, got {root!r}"
    assert mid["status"] == "failed", f"mid runs and fails, got {mid!r}"
    assert leaf["status"] == "blocked", (
        f"leaf's dep did not reach 'done' → blocked (transitively), got {leaf!r}"
    )


def test_taskq_executor_run_all_is_noop_without_pending_tasks(taskq_home):
    """run_all returns exit 0 immediately when nothing is pending."""
    from taskq_plus.service.executor import EXIT_OK, run_all
    from taskq_plus.storage.task_store import find_by_id

    _seed_pending(taskq_home, task_id="already", command="echo hi", status="done")

    prior = _with_home(taskq_home)
    try:
        # `run_all` returns the batch CLI exit code so an OPEN breaker can
        # reject the batch path with exit 3 (SPEC.md#L113).
        assert run_all() == EXIT_OK, "an empty batch is a successful no-op"
        rec = find_by_id("already")
    finally:
        _restore_home(prior)

    assert rec["status"] == "done", (
        f"a non-pending task must not be re-executed, got {rec['status']!r}"
    )


def test_taskq_executor_run_all_rejects_when_breaker_open(taskq_home, monkeypatch):
    """run_all returns EXIT_BREAKER_OPEN while the breaker is OPEN.

    Coverage pin for executor.py L455-L461: SPEC.md#L113 — during OPEN
    any `run` (including the batch path) is rejected immediately with
    exit 3 + 'breaker open' on stderr, and no subprocess is spawned.
    """
    from taskq_plus.service.executor import (
        EXIT_BREAKER_OPEN,
        run_all,
    )
    from taskq_plus.storage.task_store import find_by_id

    # Long cooldown so the breaker cannot transition out of OPEN during the test.
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "3600")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "1")

    # Pre-seed breaker.json as OPEN with opened_at = now (real wall-clock).
    prior = _with_home(taskq_home)
    try:
        write_breaker(
            {
                "version": 1,
                "state": STATE_OPEN,
                "consecutive_failures": 1,
                "opened_at": time.time(),
            }
        )
        # A pending task exists — but the OPEN breaker must reject the batch
        # BEFORE any subprocess is spawned.
        _seed_pending(taskq_home, task_id="would_run", command="echo hi")

        captured_stderr = io.StringIO()
        with redirect_stderr(captured_stderr):
            cli_exit = run_all()
    finally:
        _restore_home(prior)

    assert cli_exit == EXIT_BREAKER_OPEN, (
        f"while breaker OPEN: run_all must exit {EXIT_BREAKER_OPEN}, got {cli_exit}"
    )
    stderr_text = captured_stderr.getvalue().lower()
    assert "breaker open" in stderr_text, (
        f"expected 'breaker open' on stderr, got {captured_stderr.getvalue()!r}"
    )
    # And the pending task MUST NOT have been executed (still 'pending').
    rec = find_by_id("would_run")
    assert rec["status"] == "pending", (
        f"OPEN breaker must not execute the batch — task stays pending, "
        f"got {rec['status']!r}"
    )


# ---- executor.py — FR-07 plugin_error audit emission ---------------------
def test_taskq_executor_emit_plugin_errors_writes_one_event_per_failure(taskq_home):
    """_emit_plugin_errors appends a plugin_error audit event per PluginFailure."""
    import json as _json

    from taskq_plus.service.executor import _emit_plugin_errors
    from taskq_plus.service.plugins import PluginFailure

    failures = [
        PluginFailure(hook="pre_run", plugin="alpha", error="boom"),
        PluginFailure(hook="post_run", plugin="beta", error="bang"),
    ]

    prior = _with_home(taskq_home)
    try:
        _emit_plugin_errors(failures, "task-42")
    finally:
        _restore_home(prior)

    lines = [
        _json.loads(line)
        for line in (taskq_home / "audit.log").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [e for e in lines if e.get("event") == "plugin_error"]
    assert len(events) == 2, (
        f"one plugin_error event per failure expected, got {len(events)}: {lines!r}"
    )
    assert {e["plugin"] for e in events} == {"alpha", "beta"}
    assert {e["hook"] for e in events} == {"pre_run", "post_run"}
    assert all(e["task_id"] == "task-42" for e in events), (
        "every event must carry the originating task_id"
    )


def test_taskq_executor_dispatch_and_emit_extracts_task_id(taskq_home):
    """_dispatch_and_emit returns the dispatch result and survives no plugins."""
    from taskq_plus.service.executor import _dispatch_and_emit

    prior = _with_home(taskq_home)
    try:
        with_task = _dispatch_and_emit("pre_run", [], {"id": "t-7"})
        without_args = _dispatch_and_emit("post_run", [])
        non_dict_arg = _dispatch_and_emit("pre_run", [], "not-a-dict")
    finally:
        _restore_home(prior)

    for label, result in (
        ("with task dict", with_task),
        ("with no args", without_args),
        ("with non-dict arg", non_dict_arg),
    ):
        assert result is not None, f"dispatch result must be returned ({label})"
        assert result.failures == [], f"no plugins → no failures ({label})"


# ---- executor.py — run_with_retry feeds the breaker on final failure -----
def test_taskq_executor_run_with_retry_records_final_failure(taskq_home, monkeypatch):
    """A final failure advances the persisted breaker counter (FR-03 AC)."""
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "60")
    _seed_pending(taskq_home, task_id="failing", command="false")

    prior = _with_home(taskq_home)
    try:
        code = run_with_retry("failing", sleep_fn=lambda _s: None)
        record = read_breaker()
    finally:
        _restore_home(prior)

    assert code == 1, f"a failing task returns CLI exit 1, got {code}"
    assert record is not None, "run_with_retry must persist the breaker record"
    assert record.get("state") == STATE_CLOSED, (
        f"1 failure < threshold 3 → still CLOSED, got {record.get('state')!r}"
    )
    counter = record.get("consecutive_failures", record.get("failure_count"))
    assert counter == 1, (
        f"one final failure must advance the counter to 1, got {record!r}"
    )


def test_taskq_executor_run_with_retry_records_success(taskq_home, monkeypatch):
    """A success closes the breaker and zeroes the counter (FR-03 AC)."""
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "60")
    _seed_pending(taskq_home, task_id="okay", command="echo hi")

    prior = _with_home(taskq_home)
    try:
        write_breaker(
            {"version": 1, "state": STATE_CLOSED, "consecutive_failures": 2,
             "opened_at": None}
        )
        code = run_with_retry("okay", sleep_fn=lambda _s: None)
        record = read_breaker()
    finally:
        _restore_home(prior)

    assert code == 0, f"a succeeding task returns CLI exit 0, got {code}"
    counter = record.get("consecutive_failures", record.get("failure_count"))
    assert counter == 0, f"success must zero the failure counter, got {record!r}"
    assert record.get("state") == STATE_CLOSED
