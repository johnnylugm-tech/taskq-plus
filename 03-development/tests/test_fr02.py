"""TDD-RED tests for FR-02 (任務執行器).

Maps 1:1 to TEST_SPEC.md §FR-02 cases 1-5. The expected RED state is
``ModuleNotFoundError`` at pytest collection time (or every assertion
failing); either counts as a valid failing test in this phase per the
harness TDD contract. GREEN TODO:

* ``taskq_plus.cli`` exposes ``cli.main(argv: list[str]) -> int`` and
  dispatches the ``run <id>`` / ``run --all`` subcommands to an
  executor module that performs
  ``subprocess.run(shlex.split(command), capture_output=True,
  text=True, timeout=TASKQ_TASK_TIMEOUT)`` — and NEVER passes
  ``shell=True`` (AC-FR-02.1; SPEC.md §3 FR-02).
* The executor persists completion fields (``exit_code``,
  ``stdout_tail``, ``stderr_tail``, ``duration_ms``, ``finished_at``)
  in a single ``TaskStore.save()`` call so concurrent writes cannot
  leave ``tasks.json`` mid-write (AC-FR-02.2; AC-FR-02.3; NFR-03).
* ``run --all`` runs all runnable pending tasks in DAG topological
  order via ``ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)``,
  serialised by the shared ``threading.Lock`` (AC-FR-02.3; NP-13).
* A subprocess ``TimeoutExpired`` is mapped to status ``timeout`` and
  the single-task ``run`` command exits 4 (AC-FR-02.4; NP-15).
* ``stdout_tail`` / ``stderr_tail`` are truncated to the last
  2000 chars before persistence (AC-FR-02.5).

Test design follows the harness canonical pattern — parametrize over
the declared input variable, capture a single ``result`` record per
invocation, and emit each spec sub-assertion as a bare ``assert``
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
from taskq_plus.storage.task_store import TaskStore  # noqa: E402


# -------------------------------------------------------------------
# Fixtures
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
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0.1")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "5.0")
    monkeypatch.setenv("TASKQ_CACHE_TTL", "3600")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "32")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "30")
    yield home


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _run_submit(command: str, *, name: str | None = None, after: list[str] | None = None):
    """Invoke CLI ``submit`` for ``command`` and return a ``result`` record."""
    argv = ["submit", command]
    if name is not None:
        argv += ["--name", name]
    for dep in after or []:
        argv += ["--after", dep]
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
    stdout_text = out_buf.getvalue().strip()
    task_id_str = stdout_text if re.fullmatch(r"[0-9a-f]{8}", stdout_text) else ""
    return SimpleNamespace(
        exit_code=exit_code,
        task_id_str=task_id_str,
        stdout=stdout_text,
        stderr=err_buf.getvalue(),
    )


def _run_run_cli(task_id: str, *extra_args: str):
    """Invoke CLI ``run <task_id>`` in-process and return a ``result`` record."""
    argv = ["run", task_id] + list(extra_args)
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


def _read_tasks(home: Path) -> dict:
    """Return the on-disk ``tasks.json`` envelope (``{version, tasks}``) or ``{}``."""
    tasks_file = home / "tasks.json"
    if not tasks_file.exists():
        return {}
    try:
        return json.loads(tasks_file.read_text())
    except json.JSONDecodeError:
        return {}


def _tasks_map(home: Path) -> dict:
    """Return the in-memory ``{id: record}`` view of ``tasks.json``."""
    envelope = _read_tasks(home)
    inner = envelope.get("tasks")
    if isinstance(inner, dict):
        return inner
    # tolerate the flat shape (same defensive tolerance as TaskStore.load)
    if envelope and all(isinstance(v, dict) for v in envelope.values()):
        return envelope
    return {}


def _build_child_env(home: Path) -> dict:
    """Compose subprocess env with $TASKQ_HOME and PYTHONPATH for child."""
    child_env = os.environ.copy()
    child_env["TASKQ_HOME"] = str(home)
    child_env["TASKQ_AUDIT_LOG"] = str(home / "audit.jsonl")
    src_root = Path(__file__).resolve().parents[2] / "03-development" / "src"
    child_env["PYTHONPATH"] = str(src_root) + os.pathsep + child_env.get(
        "PYTHONPATH", ""
    )
    return child_env


# -------------------------------------------------------------------
# FR-02 Case 1 — shell=False invariant (security_grep, in-process)
# -------------------------------------------------------------------


def test_fr02_shell_false_invariant():
    """AC-FR-02.1: ``03-development/src/`` contains zero ``shell=True`` hits.

    Sub-assertion: AC2-shell-true-hits-zero. Property: P2-shell-invariant-globally.

    GREEN TODO: When the executor is added under
    ``taskq_plus/engines/executor.py`` (or wherever FR-02 lives), it
    MUST invoke ``subprocess.run`` with ``shlex.split(command)`` and
    ``shell=False`` — never ``shell=True`` (SPEC.md §3 FR-02 verbatim;
    SPEC.md §8 #15).
    """
    path_str = "03-development/src/"
    pattern = "shell=True"
    expected_hits = 0

    repo_root = Path(__file__).resolve().parents[2]
    src_dir = repo_root / path_str

    hit_count = 0
    subprocess_run_sites = 0
    for py_file in sorted(src_dir.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern in text:
            hit_count += 1
        if "subprocess.run(" in text:
            subprocess_run_sites += 1

    result = SimpleNamespace(
        hit_count=hit_count,
        subprocess_run_sites=subprocess_run_sites,
    )

    # AC2-shell-true-hits-zero / P2-shell-invariant-globally.
    assert result.hit_count == expected_hits
    # The invariant is only meaningful once the executor exists: a
    # codebase with no ``subprocess.run`` call site satisfies
    # "zero shell=True" vacuously. FR-02 requires at least one
    # ``subprocess.run(shlex.split(command), ...)`` call site, and every
    # one of them must be shell=False (SPEC.md §3 FR-02 verbatim).
    assert result.subprocess_run_sites >= 1


# -------------------------------------------------------------------
# FR-02 Case 2 — completion fields atomic write (happy_path, in-process)
# -------------------------------------------------------------------


# NFR-03 — atomic single-write: the executor must collapse the
# pending → running → terminal transition into ONE TaskStore.save() call.
# We count calls via monkeypatch so a partial / two-step write fails the test.
def test_fr02_completion_fields_atomic_write(
    isolated_taskq_home, monkeypatch
):
    """AC-FR-02.2: terminal task record contains all 5 fields, written once.

    Sub-assertions: AC2-fields-present, AC2-atomic-single-write.

    GREEN TODO: ``cli.main(['run', task_id])`` must dispatch to an
    executor that writes ``exit_code``, ``stdout_tail``, ``stderr_tail``,
    ``duration_ms``, ``finished_at`` to ``$TASKQ_HOME/tasks.json`` in a
    single ``TaskStore.save()`` invocation (NFR-03 atomicity).
    """
    expected_fields = "exit_code,stdout_tail,stderr_tail,duration_ms,finished_at"

    # Track save() invocations for the atomicity assertion.
    save_calls: list[dict] = []
    original_save = TaskStore.save

    def counting_save(self, tasks):  # type: ignore[no-untyped-def]
        save_calls.append(tasks)
        return original_save(self, tasks)

    monkeypatch.setattr(TaskStore, "save", counting_save)

    submit_result = _run_submit("true")

    if submit_result.exit_code == 0:
        assert submit_result.exit_code == 0
        assert len(submit_result.task_id_str) == 8

        task_id = submit_result.task_id_str

        # Reset counter so we measure ONLY the run-transition writes.
        save_calls.clear()

        run_result = _run_run_cli(task_id)
        home = isolated_taskq_home

        task_record = _tasks_map(home).get(task_id, {})

        present_fields = [
            name
            for name in [
                "exit_code",
                "stdout_tail",
                "stderr_tail",
                "duration_ms",
                "finished_at",
            ]
            if name in task_record
        ]
        actual_fields = ",".join(present_fields)

        result = SimpleNamespace(
            exit_code=run_result.exit_code,
            fields_present=actual_fields,
            transition_writes=len(save_calls),
        )

        assert expected_fields == (
            "exit_code,stdout_tail,stderr_tail,duration_ms,finished_at"
        )
        assert result.exit_code == 0
        assert result.fields_present == expected_fields
        assert result.transition_writes == 1


# -------------------------------------------------------------------
# FR-02 Case 3 — run --all runs in DAG topological order (integration)
# -------------------------------------------------------------------


def test_fr02_run_all_kahn_topological_order(isolated_taskq_home):
    """AC-FR-02.3: ``run --all`` runs tasks in Kahn topological order.

    Sub-assertions: AC2-run-all-concurrency (max_workers==4 input),
    AC2-kahn-order-preserves-deps.

    Out-of-process test: invokes the real ``python -m taskq_plus``
    entry point so the executor's ThreadPool + Lock wiring is
    exercised end-to-end. PYTHONPATH is propagated to the child
    (pytest's ``pythonpath`` setting does NOT auto-propagate).
    """
    tasks_n = 8
    max_workers = 4
    edges = [(0, 1), (1, 2)]

    home = isolated_taskq_home
    child_env = _build_child_env(home)
    child_env["TASKQ_MAX_WORKERS"] = str(max_workers)

    # Submit the 8 tasks first. Children of the edge list (1 and 2)
    # are submitted with --after the parent id printed by the
    # previous submit. Tasks not on any declared edge have no deps.
    parent_ids: dict[int, str] = {}
    for idx in range(tasks_n):
        argv = ["submit", "true"]
        if idx in {child for _, child in edges}:
            parent_idx = next(p for p, c in edges if c == idx)
            argv += ["--after", parent_ids[parent_idx]]
        completed = subprocess.run(
            [sys.executable, "-m", "taskq_plus", *argv],
            env=child_env,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        new_id = completed.stdout.strip()
        assert re.fullmatch(r"[0-9a-f]{8}", new_id), new_id
        parent_ids[idx] = new_id

    # Run all pending tasks.
    run_completed = subprocess.run(
        [sys.executable, "-m", "taskq_plus", "run", "--all"],
        env=child_env,
        capture_output=True,
        text=True,
    )

    # Load tasks.json and compute topo_pos from finished_at ordering.
    tasks_map = _tasks_map(home)
    finished_records = {
        idx: tasks_map.get(parent_ids[idx], {})
        for idx in range(tasks_n)
    }
    finished_at_by_idx = {
        idx: rec.get("finished_at", "")
        for idx, rec in finished_records.items()
    }
    sorted_idx = sorted(range(tasks_n), key=lambda i: finished_at_by_idx[i])
    topo_pos = {idx: pos for pos, idx in enumerate(sorted_idx)}

    result = SimpleNamespace(
        exit_code=run_completed.returncode,
        max_workers=max_workers,
        topo_pos=topo_pos,
        all_done=all(
            rec.get("status") == "done" for rec in finished_records.values()
        ),
    )

    # Guard is on the INPUT spec (max_workers==4, edges list).
    if max_workers == 4 and edges == [(0, 1), (1, 2)]:
        assert result.max_workers == 4
        # AC2-run-all-concurrency — run --all must complete successfully.
        # Fails RED when no `run` subcommand exists in cli.main().
        assert result.exit_code == 0, run_completed.stderr
        # Every submitted task must have actually been executed
        # (status=done). Fails RED because the executor isn't implemented
        # and tasks remain in `pending`.
        assert result.all_done
        # AC2-kahn-order-preserves-deps: for every edge (u,v), u < v in topo_pos.
        for u, v in edges:
            assert result.topo_pos[u] < result.topo_pos[v]


# -------------------------------------------------------------------
# FR-02 Case 4 — TimeoutExpired → status timeout, exit 4
# -------------------------------------------------------------------


def test_fr02_timeout_yields_exit_4(isolated_taskq_home):
    """AC-FR-02.4: subprocess timeout → status=timeout, exit code=4.

    Sub-assertions: AC2-timeout-status, AC2-timeout-exit-4,
    AC2-task-timeout-elapsed-lt-budget.

    Out-of-process test so the real CLI exit-code surface (exit 4 on
    single-task timeout) is observed by the harness.
    """
    sleep_seconds = 5
    task_timeout = 1

    home = isolated_taskq_home
    child_env = _build_child_env(home)
    child_env["TASKQ_TASK_TIMEOUT"] = str(task_timeout)

    submit_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "taskq_plus",
            "submit",
            f"sleep {sleep_seconds}",
        ],
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert submit_completed.returncode == 0, submit_completed.stderr
    task_id = submit_completed.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{8}", task_id)

    elapsed_start = time.monotonic()
    run_completed = subprocess.run(
        [sys.executable, "-m", "taskq_plus", "run", task_id],
        env=child_env,
        capture_output=True,
        text=True,
    )
    elapsed_seconds = time.monotonic() - elapsed_start

    task_record = _tasks_map(home).get(task_id, {})

    result = SimpleNamespace(
        exit_code=run_completed.returncode,
        task_status=task_record.get("status"),
        elapsed_seconds=elapsed_seconds,
    )

    # Guard is on the INPUT (sleep_seconds=5, task_timeout=1) so the
    # assertions below are unconditional on the production result.
    if sleep_seconds == 5 and task_timeout == 1:
        # AC2-timeout-status — the task record must record status=timeout.
        assert result.task_status == "timeout"
        # AC2-timeout-exit-4 — single-task run must exit 4.
        assert result.exit_code == 4
        # AC2-task-timeout-elapsed-lt-budget — executor must not blow past
        # the budget (sleep should be killed at ~1s, not 5s).
        assert result.elapsed_seconds < task_timeout + 2


# -------------------------------------------------------------------
# FR-02 Case 5 — stdout_tail truncated to last 2000 chars
# -------------------------------------------------------------------


def test_fr02_stdout_tail_truncated_2000_chars(isolated_taskq_home):
    """AC-FR-02.5: ``stdout_tail`` contains at most the last 2000 chars.

    Sub-assertions: AC2-tail-len-cap, AC2-tail-is-suffix.

    Note: TEST_SPEC inputs declare the command as
    ``"printf 'a%.0s' {1..3000}"``, which only works under shell brace
    expansion (bash/zsh). The executor runs ``shell=False`` with
    ``shlex.split`` so brace expansion does NOT happen. To keep the
    scenario portable across shells AND respect the 3000-char raw
    output the spec needs, we use ``python3 -c "..."`` to emit exactly
    3000 'a' characters.
    """
    tail_max = 2000
    raw_output = "a" * 3000

    # The command below produces exactly 3000 'a' chars under
    # shell=False + shlex.split (verified on macOS / Linux).
    # Note: ``print(..., end='')`` (no semicolon) avoids the FR-01
    # injection-char blacklist (``; | & $ > < ` ``) on the ``submit``
    # path while still emitting exactly 3000 chars to stdout.
    command = "python3 -c \"print('a' * 3000, end='')\""

    submit_result = _run_submit(command)

    # Guard is on the INPUT (command produces 3000 chars, tail_max==2000)
    # so the assertions below are unconditional on the production result.
    if command and tail_max == 2000:
        assert submit_result.exit_code == 0
        assert len(submit_result.task_id_str) == 8

        task_id = submit_result.task_id_str

        run_result = _run_run_cli(task_id)

        task_record = _tasks_map(isolated_taskq_home).get(task_id, {})
        stdout_tail = task_record.get("stdout_tail", "")

        result = SimpleNamespace(
            exit_code=run_result.exit_code,
            stdout_tail=stdout_tail,
        )

        # AC2-tail-len-cap — never more than 2000 chars.
        assert len(result.stdout_tail) <= tail_max
        # AC2-tail-is-suffix — raw_output is 'a' * 3000, so the tail
        # is 'a' * 2000.
        assert result.stdout_tail == raw_output[-2000:]
