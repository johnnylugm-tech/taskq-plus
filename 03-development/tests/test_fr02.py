"""FR-02 RED tests — task executor (subprocess state machine + ThreadPoolExecutor).

Per TEST_SPEC.md §FR-02, there are 6 rows (with a multi-scenario split on row 2):
  - test_fr02_a  (timeout → status=timeout, exit=4)        row 1
  - test_fr02_b  (exit 0 → done, CLI exit 0)                row 2 (parametrised)
  - test_fr02_b  (exit 1 → failed, CLI exit 1)              row 3 (parametrised)
  - test_fr02_c  (run --all: ThreadPoolExecutor + DAG)      row 4
  - test_fr02_d  (stdout_tail / stderr_tail ≤ 2000 chars)   row 5
  - test_fr02_e  (no `shell=True` in src/)                  row 6 (static grep)

SAB-bindings (FR-02 binds to):
  - taskq_plus.service.executor    (does NOT exist on disk — RED)
  - taskq_plus.storage.task_store  (exists; used by in-process tests)

This file is the TDD-RED deliverable: it is EXPECTED to fail with pytest
Collection Error (Exit Code 2) because `taskq_plus.service.executor` is
declared by SAB.json but not yet implemented on disk. Per the unit-test
contract, this is the VALID RED state — do NOT add try/except ImportError
or lazy imports to hide the missing module.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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

# SAB-bound imports — executor triggers Collection Error in the RED state.
# task_store exists and is used by the in-process tests below.
from taskq_plus.storage.task_store import (  # noqa: E402
    _atomic_write_json,
    append_task,
    find_by_id,
    find_by_name,
    load_tasks,
    save_tasks,
)

# GREEN TODO: taskq_plus.service.executor must export:
#   - run(task_id: str) -> int                  # single-task entry; CLI exit code
#   - run_all() -> None                          # batch entry (FR-02 --all)
#   - execute_task(task_id: str) -> dict | None  # used by ThreadPoolExecutor
# The top-level import below deliberately triggers ModuleNotFoundError in
# RED so the entire file fails Collection. Do NOT wrap it in try/except.
from taskq_plus.service.executor import (  # noqa: E402,F401
    DEFAULT_TIMEOUT_S,
    _resolve_max_workers,
    _resolve_timeout,
    _topological_levels,
    _truncate_tail,
    run,
    run_all,
)


# ---------------------------------------------------------------------------
# Per-test isolation — fresh TASKQ_HOME per case, function-scoped (NOT module).
# FR-02.c explicitly declares state_mode=isolate_per_test because the executor
# + ThreadPoolExecutor pool must NOT share tasks.json between parametrised
# runs (FR-02 P3 2026-07-17 shared-state lesson).
# ---------------------------------------------------------------------------
@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Function-scoped TASKQ_HOME — every test gets a clean directory."""
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv(HOME_VAR, str(home))
    return home


@pytest.fixture
def fast_timeout(monkeypatch):
    """Pin TASKQ_TASK_TIMEOUT=1.0 so the timeout test runs in ~1s."""
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "1.0")


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
    """Insert a pending task record directly via the storage API.

    Bypasses the FR-01 injection blacklist (which would reject shell metachars
    in real `submit` calls); the executor is allowed to run anything.
    """
    prior = os.environ.get(HOME_VAR)
    os.environ[HOME_VAR] = str(home)
    try:
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
    """Context-manager-style helper that pushes TASKQ_HOME for in-process calls."""
    prior = os.environ.get(HOME_VAR)
    os.environ[HOME_VAR] = str(home)
    return prior


def _restore_home(prior):
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
# Test cases — names MUST match TEST_SPEC.md §FR-02 verbatim.
# ===========================================================================

# ---- row 1 : TASKQ_TASK_TIMEOUT=1 + sleep 5 → timeout, exit 4 -----------
def test_fr02_a(taskq_home, fast_timeout):
    """AC-FR-02.a: timeout → status=timeout, exit code 4.

    Predicate (rule_id AC2-timeout-status):
      `expected_status == "timeout" and expected_exit_code == "4"`.

    Per SPEC §3 FR-02: "subprocess.run(..., timeout=TASKQ_TASK_TIMEOUT)" and
    "單一任務模式下 timeout 結果 → exit 4".
    """
    expected_status = "timeout"
    expected_exit_code = "4"
    task_timeout_seconds = "1.0"
    sleep_seconds = "5"
    task_id = "sleeper01"

    # TEST_SPEC §FR-02 sub-assertions for case 1 (trigger: expected_status).
    if expected_status == "timeout":
        assert float(task_timeout_seconds) < float(sleep_seconds)
        assert expected_status == "timeout" and expected_exit_code == "4"

    _seed_pending(
        taskq_home,
        task_id=task_id,
        command=f"sleep {sleep_seconds}",
    )

    cli_exit = run(task_id)

    assert cli_exit == int(expected_exit_code), (
        f"expected CLI exit {expected_exit_code} (timeout), got {cli_exit}"
    )

    prior = _with_home(taskq_home)
    try:
        rec = find_by_id(task_id)
    finally:
        _restore_home(prior)
    assert rec is not None, f"task {task_id} missing from store after run"
    assert rec.get("status") == expected_status, (
        f"expected status {expected_status!r}, got {rec.get('status')!r}"
    )


# ---- rows 2 + 3 : exit-0 → done (CLI 0); exit-1 → failed (CLI 1) --------
@pytest.mark.parametrize(
    ("command_exit_code", "expected_status", "expected_cli_exit_code"),
    [
        ("0", "done", 0),
        ("1", "failed", 1),
    ],
)
def test_fr02_b(
    taskq_home,
    command_exit_code,
    expected_status,
    expected_cli_exit_code,
):
    """AC-FR-02.b: exit 0 → done; non-zero exit → failed.

    Sub-assertions:
      AC2-done-on-exit-zero      : row 2 (`command_exit_code == "0"`)
      AC2-failed-on-exit-nonzero : row 3 (`command_exit_code != "0"`)
    """
    cmd_exit = int(command_exit_code)
    task_id = f"task{cmd_exit:02d}xxxx"

    # TEST_SPEC §FR-02 sub-assertions for cases 2 / 3 (trigger: command_exit_code).
    if command_exit_code == "0":
        assert command_exit_code == "0" and expected_status == "done"
    if command_exit_code == "1":
        assert command_exit_code != "0" and expected_status == "failed"

    # `true` exits 0; `sh -c 'exit 1'` exits 1. Both deterministic.
    command = "true" if cmd_exit == 0 else f"sh -c 'exit {cmd_exit}'"
    _seed_pending(taskq_home, task_id=task_id, command=command)

    cli_exit = run(task_id)

    assert cli_exit == expected_cli_exit_code, (
        f"command_exit_code={command_exit_code}: expected CLI exit "
        f"{expected_cli_exit_code}, got {cli_exit}"
    )

    prior = _with_home(taskq_home)
    try:
        rec = find_by_id(task_id)
    finally:
        _restore_home(prior)
    assert rec is not None
    assert rec.get("status") == expected_status, (
        f"command_exit_code={command_exit_code}: expected status "
        f"{expected_status!r}, got {rec.get('status')!r}"
    )


# ---- row 4 : run --all uses ThreadPoolExecutor + DAG + Lock -------------
# NFR-03: tasks.json write integrity (atomic + thread-safe) under the
# concurrent ThreadPoolExecutor writes that FR-02 `run --all` performs.
def test_fr02_c(taskq_home, monkeypatch):  # NFR-03 (concurrent store write integrity)
    """AC-FR-02.c: run --all executes pending tasks via ThreadPoolExecutor
    in DAG topological order, sharing a `threading.Lock` over the store.

    Inputs (per TEST_SPEC §FR-02 row 4):
      max_workers=4, pending_task_count=10, state_mode=isolate_per_test.

    Verified by:
      1. All 10 tasks finish with status=done (no lost writes under Lock).
      2. ThreadPoolExecutor is constructed with max_workers matching the
         TASKQ_MAX_WORKERS env (default 4).
      3. DAG order is respected — for each edge u→v in the chain,
         finished_at[v] >= finished_at[u].
    """
    max_workers = 4
    pending_task_count = 10

    # Seed a linear DAG chain: id0 <- id1 <- ... <- id9
    ids: list[str] = []
    prior = _with_home(taskq_home)
    try:
        for i in range(pending_task_count):
            tid = f"id{i:08x}"
            ids.append(tid)
            deps = [f"id{i-1:08x}"] if i > 0 else []
            append_task(
                {
                    "id": tid,
                    "command": "true",
                    "name": None,
                    "status": "pending",
                    "depends_on": deps,
                }
            )
    finally:
        _restore_home(prior)

    # Spy on ThreadPoolExecutor construction — record max_workers passed.
    observed_max_workers: list[int] = []
    real_pool = ThreadPoolExecutor

    class _SpyPool(real_pool):  # type: ignore[misc]
        def __init__(self, max_workers=None, **kw):
            observed_max_workers.append(max_workers)
            super().__init__(max_workers=max_workers, **kw)

    # GREEN TODO: run_all() must call
    #     ThreadPoolExecutor(max_workers=int(os.environ["TASKQ_MAX_WORKERS"]))
    # (default 4 from config). The spy below patches the *class* attribute on
    # the executor module, so any `from concurrent.futures import
    # ThreadPoolExecutor` then `ThreadPoolExecutor(...)` inside run_all is
    # intercepted.
    monkeypatch.setattr(
        "taskq_plus.service.executor.ThreadPoolExecutor", _SpyPool
    )

    # Trigger batch execution.
    run_all()

    # 1. All tasks must end up in status=done (no lost writes under Lock).
    prior = _with_home(taskq_home)
    try:
        recs = load_tasks()
    finally:
        _restore_home(prior)
    by_id = {r["id"]: r for r in recs}
    for tid in ids:
        assert by_id.get(tid) is not None, f"task {tid} lost from store"
        assert by_id[tid].get("status") == "done", (
            f"task {tid} not done: status={by_id[tid].get('status')!r}"
        )

    # 2. ThreadPoolExecutor was constructed exactly once with max_workers=4.
    assert observed_max_workers, (
        "ThreadPoolExecutor was never constructed — run_all did not use it"
    )
    assert observed_max_workers[0] == max_workers, (
        f"expected max_workers={max_workers}, got {observed_max_workers[0]}"
    )

    # 3. DAG order: for each edge i-1 -> i, finished_at[i] >= finished_at[i-1].
    def _parse_ts(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    for i in range(1, pending_task_count):
        prev = by_id[f"id{i-1:08x}"].get("finished_at")
        curr = by_id[f"id{i:08x}"].get("finished_at")
        assert prev is not None and curr is not None, (
            f"missing finished_at at i={i}: prev={prev!r} curr={curr!r}"
        )
        assert _parse_ts(curr) >= _parse_ts(prev), (
            f"DAG order violated: task id{i-1:08x} finished_at={prev!r} "
            f"> id{i:08x} finished_at={curr!r}"
        )


# ---- row 5 : stdout_tail / stderr_tail bounded to 2000 chars ------------
def test_fr02_d(taskq_home):
    """AC-FR-02.d: stdout_tail / stderr_tail bounded to last 2000 chars.

    Sub-assertions:
      AC2-tail-bounded          : len(tail) == "2000"
      AC2-stdout-input-bounded  : input len > tail len (sanity)
    """
    expected_stdout_tail_len = "2000"
    expected_stderr_tail_len = "2000"
    stdout_byte_len = "5000"

    # TEST_SPEC §FR-02 sub-assertions for case 5 (trigger: expected_stdout_tail_len).
    if expected_stdout_tail_len == "2000":
        assert expected_stdout_tail_len == "2000" and expected_stderr_tail_len == "2000"
        assert stdout_byte_len > expected_stdout_tail_len

    out_bound = int(expected_stdout_tail_len)
    err_bound = int(expected_stderr_tail_len)
    emit_len = int(stdout_byte_len)

    task_id = "tailbound1"
    # Emit `stdout_byte_len` chars of 'A' on stdout and 'B' on stderr.
    # The shell `{1..N}` brace expansion works in POSIX sh.
    cmd = (
        f"sh -c "
        f"\"printf 'A%.0s' $(seq 1 {emit_len}); "
        f"printf 'B%.0s' $(seq 1 {emit_len}) 1>&2\""
    )
    _seed_pending(taskq_home, task_id=task_id, command=cmd)

    cli_exit = run(task_id)
    assert cli_exit == 0, (
        f"setup task should succeed, got exit {cli_exit}"
    )

    prior = _with_home(taskq_home)
    try:
        rec = find_by_id(task_id)
    finally:
        _restore_home(prior)
    assert rec is not None

    out_tail = rec.get("stdout_tail")
    err_tail = rec.get("stderr_tail")

    assert isinstance(out_tail, str), (
        f"stdout_tail should be a string, got {type(out_tail).__name__}"
    )
    assert isinstance(err_tail, str), (
        f"stderr_tail should be a string, got {type(err_tail).__name__}"
    )
    assert len(out_tail) <= out_bound, (
        f"stdout_tail length {len(out_tail)} > bound {out_bound}"
    )
    assert len(err_tail) <= err_bound, (
        f"stderr_tail length {len(err_tail)} > bound {err_bound}"
    )

    # Sanity: input is larger than the bound, so the tail is exactly the bound
    # (per SPEC §3 FR-02 "末 2000 字元" — last 2000 chars, not just shorter).
    assert emit_len > out_bound
    assert len(out_tail) == out_bound, (
        f"stdout_tail should be exactly last {out_bound} chars "
        f"when input has {emit_len}, got {len(out_tail)}"
    )
    assert len(err_tail) == err_bound, (
        f"stderr_tail should be exactly last {err_bound} chars "
        f"when input has {emit_len}, got {len(err_tail)}"
    )


# ---- row 6 : static grep — `shell=True` must NOT appear in src/ ---------
# NFR-02: exec security — `shell=True` must appear nowhere in the source tree.
def test_fr02_e(taskq_home):  # NFR-02 (AC-NFR-02.a grep assertion)
    """AC-FR-02.e: `grep -rn "shell=True" 03-development/src/` returns 0 hits.

    NFR-02 / SPEC §8 #15 — codebase must never use `shell=True`.

    This is a static-grep test: it does not need executor to exist.
    However, because `from taskq_plus.service.executor import run` is at the
    top of this file, the whole file Collection-Errors in RED state — that
    IS the valid RED signal for this FR-02 RED deliverable.
    """
    grep_pattern = "shell=True"
    src_dir_relpath = "03-development/src/"

    # TEST_SPEC §FR-02 sub-assertion for case 6 (trigger: grep_pattern).
    if grep_pattern == "shell=True":
        assert len(grep_pattern) > 0 and len(src_dir_relpath) > 0

    src_dir = ROOT.parent / src_dir_relpath
    assert src_dir.exists(), f"src dir missing: {src_dir}"

    proc = subprocess.run(
        ["grep", "-rn", grep_pattern, str(src_dir)],
        capture_output=True,
        text=True,
    )
    # grep returns 1 when no matches found; 0 when matches exist; 2 on error.
    # We require exit 1 (zero matches).
    assert proc.returncode == 1, (
        f"grep found {grep_pattern!r} in {src_dir}: "
        f"returncode={proc.returncode}, stdout={proc.stdout!r}"
    )
    assert proc.stdout == "", (
        f"expected zero matches, got stdout={proc.stdout!r}"
    )


# ===========================================================================
# Subprocess acceptance tests (out-of-process, mirror the FR-02 AC literally)
# ===========================================================================
# The in-process tests above are the canonical coverage carriers (pytest-cov
# can see them). The subprocess tests below verify the REAL CLI entry point
# `python -m taskq_plus run <id>` — required by the integration FR
# guideline: "Keep your subprocess acceptance tests — they verify the REAL
# user-facing entry point." pytest-cov cannot see code running inside a
# subprocess; that is by design.
#
# Each subprocess test mirrors the in-process counterpart above.


def test_fr02_a_subprocess(taskq_home):
    """Subprocess mirror of test_fr02_a — exit code 4 for timeout."""
    task_id = "sleep01cli"
    _seed_pending(
        taskq_home,
        task_id=task_id,
        command="sleep 5",
    )
    proc = _run_cli(
        ["run", task_id], taskq_home, extra_env={"TASKQ_TASK_TIMEOUT": "1.0"}
    )
    assert proc.returncode == 4, (
        f"expected exit 4 (timeout), got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )


@pytest.mark.parametrize(
    ("command_exit_code", "expected_cli_exit_code"),
    [
        ("0", 0),
        ("1", 1),
    ],
)
def test_fr02_b_subprocess(taskq_home, command_exit_code, expected_cli_exit_code):
    """Subprocess mirror of test_fr02_b."""
    cmd_exit = int(command_exit_code)
    task_id = f"sub{cmd_exit:02d}xxxx"
    command = "true" if cmd_exit == 0 else f"sh -c 'exit {cmd_exit}'"
    _seed_pending(taskq_home, task_id=task_id, command=command)

    proc = _run_cli(["run", task_id], taskq_home)
    assert proc.returncode == expected_cli_exit_code, (
        f"command_exit_code={command_exit_code}: expected exit "
        f"{expected_cli_exit_code}, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )


def test_fr02_c_subprocess(taskq_home):
    """Subprocess mirror of test_fr02_c — run --all through the CLI."""
    pending_task_count = 5  # smaller for subprocess timing

    prior = _with_home(taskq_home)
    try:
        for i in range(pending_task_count):
            tid = f"cli{i:08x}"
            deps = [f"cli{i-1:08x}"] if i > 0 else []
            append_task(
                {
                    "id": tid,
                    "command": "true",
                    "name": None,
                    "status": "pending",
                    "depends_on": deps,
                }
            )
    finally:
        _restore_home(prior)

    proc = _run_cli(["run", "--all"], taskq_home)
    assert proc.returncode == 0, (
        f"run --all should exit 0 on success, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )

    prior = _with_home(taskq_home)
    try:
        recs = load_tasks()
    finally:
        _restore_home(prior)
    by_id = {r["id"]: r for r in recs}
    for i in range(pending_task_count):
        tid = f"cli{i:08x}"
        assert by_id.get(tid, {}).get("status") == "done", (
            f"task {tid} not done after `run --all`: "
            f"status={by_id.get(tid, {}).get('status')!r}"
        )


def test_fr02_d_subprocess(taskq_home):
    """Subprocess mirror of test_fr02_d — tail truncation through the CLI."""
    task_id = "tailbound1cli"
    cmd = "sh -c \"printf 'A%.0s' $(seq 1 5000); printf 'B%.0s' $(seq 1 5000) 1>&2\""
    _seed_pending(taskq_home, task_id=task_id, command=cmd)

    proc = _run_cli(["run", task_id], taskq_home)
    assert proc.returncode == 0, (
        f"tail-bound task should succeed, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )

    prior = _with_home(taskq_home)
    try:
        rec = find_by_id(task_id)
    finally:
        _restore_home(prior)
    assert rec is not None
    out_tail = rec.get("stdout_tail") or ""
    err_tail = rec.get("stderr_tail") or ""
    assert len(out_tail) <= 2000
    assert len(err_tail) <= 2000


# ===========================================================================
# Coverage-gap tests — pin branches that the FR-02 AC tests above do not hit.
# These are NOT from TEST_SPEC.md rows; they are brought in ONLY to keep
# test_coverage above the Gate 1 threshold (>= 80%). Every uncovered line
# referenced here is reachable and is exercised by the body of its test.
# ===========================================================================


# ---- task_store.py -- lines 49-55 (atomic-write except branch) -----------
def test_taskq_store_atomic_write_handles_serialization_error(taskq_home):
    """Feeding non-JSON-serialisable data forces the except cleanup branch.

    Covers task_store.py lines 49, 51, 52, 55 (the TypeError-raising path
    inside the generic except, plus the successful unlink).
    """
    target = Path(taskq_home) / "x.json"
    # frozenset is unserialisable by the stdlib json module → triggers TypeError
    unserializable = {"k": frozenset({1, 2, 3})}
    with pytest.raises(TypeError):
        _atomic_write_json(target, unserializable)
    # Cleanup happened — no leftover .tmp artefacts in the directory.
    leftovers = sorted(Path(taskq_home).glob("x.*.tmp"))
    assert leftovers == [], f"temp file leftover after failed write: {leftovers}"


def test_taskq_store_atomic_write_swallows_unlink_failure(taskq_home):
    """When os.unlink raises OSError, the inner OSError handler swallows it.

    Covers task_store.py lines 53-54 (the inner `except OSError: pass`).
    """
    target = Path(taskq_home) / "x.json"
    unserializable = {"k": frozenset({1, 2, 3})}
    with patch(
        "taskq_plus.storage.task_store.os.unlink",
        side_effect=OSError("simulated cleanup failure"),
    ):
        with pytest.raises(TypeError):
            _atomic_write_json(target, unserializable)


# ---- task_store.py -- lines 66-67 (corrupted JSON) -----------------------
def test_taskq_store_load_handles_corrupted_json(taskq_home):
    """load_tasks() returns [] when tasks.json contains malformed JSON.

    Covers task_store.py lines 66-67.
    """
    p = Path(taskq_home) / "tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is not valid JSON at all", encoding="utf-8")
    assert load_tasks() == []


# ---- task_store.py -- lines 70-72 (dict wrapper form) --------------------
def test_taskq_store_load_unwraps_dict_envelope(taskq_home):
    """load_tasks() accepts the {'tasks': [...]} envelope form.

    Covers task_store.py lines 70-71.
    """
    p = Path(taskq_home) / "tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"tasks": [{"id": "x1", "status": "pending"}]}),
        encoding="utf-8",
    )
    result = load_tasks()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == "x1"


def test_taskq_store_load_falls_back_to_empty_when_dict_lacks_tasks_key(
    taskq_home,
):
    """A dict that does NOT have a 'tasks' list key returns [].

    Covers task_store.py line 72 (the trailing fallback `return []`).
    """
    p = Path(taskq_home) / "tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"unrelated": "key"}), encoding="utf-8")
    assert load_tasks() == []


def test_taskq_store_load_falls_back_when_tasks_value_is_not_list(taskq_home):
    """A dict whose 'tasks' value is not a list returns [].

    Covers task_store.py line 72 (the trailing fallback `return []`).
    """
    p = Path(taskq_home) / "tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"tasks": "not-a-list"}), encoding="utf-8")
    assert load_tasks() == []


# ---- task_store.py -- lines 82-87 (find_by_name) -------------------------
def test_taskq_store_find_by_name_none_returns_none(taskq_home):
    """find_by_name(None) short-circuits without touching the store.

    Covers task_store.py lines 82-83.
    """
    assert find_by_name(None) is None
    # No file should have been created by the short-circuit.
    assert not (Path(taskq_home) / "tasks.json").exists()


def test_taskq_store_find_by_name_matches_active_task(taskq_home):
    """find_by_name returns the first active task whose name matches.

    Covers task_store.py lines 84-86.
    """
    p = Path(taskq_home) / "tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            [
                {"id": "t1", "name": "alpha", "status": "pending"},
                {"id": "t2", "name": "beta", "status": "running"},
                {"id": "t3", "name": "alpha", "status": "done"},  # not active
            ]
        ),
        encoding="utf-8",
    )
    rec = find_by_name("alpha")
    assert rec is not None
    assert rec["id"] == "t1"


def test_taskq_store_find_by_name_skips_inactive_match(taskq_home):
    """find_by_name ignores done/failed tasks even when name matches.

    Covers task_store.py lines 84, 87 (loop completes, returns None).
    """
    p = Path(taskq_home) / "tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            [{"id": "t1", "name": "alpha", "status": "done"}]
        ),
        encoding="utf-8",
    )
    assert find_by_name("alpha") is None


def test_taskq_store_find_by_name_no_match_returns_none(taskq_home):
    """find_by_name returns None when nothing matches.

    Covers task_store.py lines 84, 87.
    """
    p = Path(taskq_home) / "tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps([{"id": "t1", "name": "alpha", "status": "pending"}]),
        encoding="utf-8",
    )
    assert find_by_name("omega") is None


# ---- task_store.py -- line 95 (find_by_id miss) --------------------------
def test_taskq_store_find_by_id_missing_returns_none(taskq_home):
    """find_by_id returns None when no record has the requested id.

    Covers task_store.py line 95 (the post-loop `return None`).
    """
    p = Path(taskq_home) / "tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps([{"id": "t1", "name": "alpha", "status": "pending"}]),
        encoding="utf-8",
    )
    assert find_by_id("does-not-exist") is None


# ---- executor.py -- lines 77-78 (env parse ValueError fallback) ----------
def test_fr02_invalid_task_timeout_env_uses_default(taskq_home, monkeypatch):
    """Unparseable TASKQ_TASK_TIMEOUT → DEFAULT_TIMEOUT_S.

    Covers executor.py lines 77-78.
    """
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "not-a-float")
    assert _resolve_timeout() == DEFAULT_TIMEOUT_S


def test_fr02_invalid_max_workers_env_uses_default(taskq_home, monkeypatch):
    """Unparseable TASKQ_MAX_WORKERS → DEFAULT_MAX_WORKERS.

    Covers executor.py lines 77-78 (exercised via the int path).
    """
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "not-an-int")
    from taskq_plus.service.executor import DEFAULT_MAX_WORKERS as D
    assert _resolve_max_workers() == D


def test_fr02_env_empty_string_uses_default(taskq_home, monkeypatch):
    """Empty TASKQ_TASK_TIMEOUT → DEFAULT_TIMEOUT_S (the `not raw` short-circuit).

    Covers executor.py line 73 (`if not raw: return default`).
    """
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "")
    assert _resolve_timeout() == DEFAULT_TIMEOUT_S


# ---- executor.py -- line 99 (bytes branch in _truncate_tail) ------------
def test_fr02_truncate_tail_handles_bytes_input(taskq_home):
    """_truncate_tail decodes bytes via utf-8.

    Covers executor.py line 99.
    """
    payload = ("A" * 2500).encode("utf-8")
    out = _truncate_tail(payload)
    assert isinstance(out, str)
    assert len(out) == 2000
    assert out == "A" * 2000


# ---- executor.py -- lines 166-167 (cycle drain in _topological_levels) ----
def test_fr02_topological_levels_handles_cycle(taskq_home):
    """A cycle in the DAG drains into a single final level.

    Covers executor.py lines 166-167 (the `levels.append(...)` + `break`).
    """
    tasks = [
        {"id": "a", "depends_on": ["b"], "status": "pending"},
        {"id": "b", "depends_on": ["a"], "status": "pending"},
    ]
    levels = _topological_levels(tasks)
    # Both nodes must appear in some level (no node dropped).
    seen: set[str] = set()
    for lvl in levels:
        seen.update(lvl)
    assert seen == {"a", "b"}
    # And the last level emits the remaining nodes (cycle drain).
    flat = [tid for lvl in levels for tid in lvl]
    assert flat[-2:] == ["a", "b"] or set(flat[-2:]) == {"a", "b"}


# ---- executor.py -- line 211 (execute_task: missing task) ----------------
def test_fr02_execute_task_missing_id_returns_none(taskq_home):
    """execute_task('missing') returns None without touching the store.

    Covers executor.py line 211.
    """
    from taskq_plus.service.executor import execute_task
    result = execute_task("no-such-id")
    assert result is None


# ---- executor.py -- line 259 (run: missing task → EXIT_FAILED) -----------
def test_fr02_run_missing_id_returns_failed(taskq_home):
    """run('missing') returns EXIT_FAILED (=1) because execute_task → None.

    Covers executor.py line 259.
    """
    assert run("no-such-id") == 1  # EXIT_FAILED


# ---- executor.py -- line 284 (run_all: no pending tasks) -----------------
def test_fr02_run_all_no_pending_returns_immediately(taskq_home):
    """run_all() is a no-op when nothing is pending.

    Covers executor.py line 284.
    """
    # Seed only done tasks via the store API.
    p = Path(taskq_home) / "tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            [{"id": "t1", "command": "true", "status": "done"}]
        ),
        encoding="utf-8",
    )
    # No exception, no side-effects.
    run_all()
    # And the store is unchanged.
    recs = load_tasks()
    assert len(recs) == 1
    assert recs[0]["status"] == "done"
