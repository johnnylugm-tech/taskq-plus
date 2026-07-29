"""TDD-RED tests for FR-01 (任務提交與驗證).

Maps 1:1 to TEST_SPEC.md §FR-01 cases 1-14. The expected RED state is
``ModuleNotFoundError`` at pytest collection time (Exit Code 2) because no
GREEN implementation exists yet — see the task contract: a Collection Error
counts as a valid failing test in this phase.

Layering hint for the GREEN agent (do NOT implement here, only mark):

* ``taskq_plus.cli`` must expose ``cli.main(argv) -> int`` and dispatch
  ``submit`` to a handler that builds a ``TaskSubmission`` and persists to
  ``$TASKQ_HOME/tasks.json``.
* ``taskq_plus.models.TaskSubmission`` is the pydantic model that performs
  validation: non-empty, length<=1000, no injection chars in
  ``; | & $ > < ` ``, unique ``name`` against the live store, and an
  ``after`` list whose ids all exist.
* On success, exactly one ``"event": "submit"`` line is appended (with
  fsync) to ``$TASKQ_AUDIT_LOG`` (one line per invocation, FR-08 / NFR-09).
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Top-level imports are intentional — ModuleNotFoundError at collection time
# is the expected RED signal per the unit-test contract. GREEN TODO:
#   * taskq_plus.cli must define cli.main(argv: list[str]) -> int and
#     dispatch the "submit" subcommand to a handler.
#   * taskq_plus.models must define the TaskSubmission pydantic model with
#     the rules listed in §FR-01 (verbatim from SPEC.md §3).
from taskq_plus import cli  # noqa: E402
from taskq_plus.models import TaskSubmission  # noqa: E402


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_taskq_home(monkeypatch, tmp_path):
    """Per-test $TASKQ_HOME isolation (TEST_SPEC state_mode=isolate_per_test).

    Every test sees a fresh directory so that:
      * re-submitting the same ``--name`` across cases cannot leak state
        (test_fr01_submit_duplicate_name_rejected needs this).
      * audit log lines from one case cannot pollute another.
    """
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv("TASKQ_HOME", str(home))
    monkeypatch.setenv("TASKQ_AUDIT_LOG", str(home / "audit.jsonl"))
    # Pin FR-01-relevant env defaults so behaviour is deterministic.
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "4")
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "2")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0.1")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "5.0")
    monkeypatch.setenv("TASKQ_CACHE_TTL", "3600")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "32")
    yield home


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _run_in_process(argv):
    """Invoke ``cli.main(argv)`` capturing stdout + stderr.

    Returns ``(exit_code, stdout_text, stderr_text)``.

    The GREEN agent must implement ``cli.main`` to either return an int
    exit code, or call ``sys.exit(int_code)``; we normalise both to an int.
    The "submit" subcommand must write the 8-hex id on stdout.
    """
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            exit_code = cli.main(list(argv))
        except SystemExit as exit_exc:
            code = exit_exc.code
            exit_code = code if isinstance(code, int) else 1
    return exit_code, out_buf.getvalue(), err_buf.getvalue()


# -------------------------------------------------------------------
# FR-01 cases (names MUST match TEST_SPEC.md exactly).
# -------------------------------------------------------------------


def test_fr01_submit_happy_path_emits_id(isolated_taskq_home):
    """AC-FR-01.1: ``submit "echo hi"`` → exit 0; stdout is 8-hex id.

    Sub-assertions: AC1-id-len-8, AC1-exit-0.
    """
    exit_code, stdout_text, _ = _run_in_process(["submit", "echo hi"])

    assert exit_code == 0, (
        f"AC-FR-01.1: happy-path submit must exit 0, got {exit_code}; "
        f"stdout={stdout_text!r}"
    )
    stripped = stdout_text.strip()
    assert re.fullmatch(r"[0-9a-f]{8}", stripped), (
        f"AC-FR-01.1: stdout must be exactly 8 hex chars (uuid4 prefix), "
        f"got {stdout_text!r}"
    )


def test_fr01_submit_empty_command_rejected(isolated_taskq_home):
    """AC-FR-01.2: ``submit ""`` → exit 2 (empty command rejected).

    Sub-assertions: AC1-empty-rejected.
    """
    exit_code, _, _ = _run_in_process(["submit", ""])
    assert exit_code == 2, (
        f"AC-FR-01.2: empty command must exit 2, got {exit_code}"
    )


def test_fr01_submit_injection_semicolon_rejected(isolated_taskq_home):
    """Injection char ';' → exit 2 (FR-01 + NFR-02 AC-NFR-02.2).

    Sub-assertions: AC1-injection-char-present, AC1-injection-exit-2.
    """
    exit_code, _, _ = _run_in_process(["submit", "echo hi; rm x"])
    assert exit_code == 2, (
        f"AC-FR-01.3 / NFR-02: ';' is on the injection blacklist, got {exit_code}"
    )


def test_fr01_submit_injection_pipe_rejected(isolated_taskq_home):
    """Injection char '|' → exit 2."""
    exit_code, _, _ = _run_in_process(["submit", "echo hi | cat"])
    assert exit_code == 2, (
        f"NFR-02: '|' is on the injection blacklist, got {exit_code}"
    )


def test_fr01_submit_injection_ampersand_rejected(isolated_taskq_home):
    """Injection char '&' → exit 2."""
    exit_code, _, _ = _run_in_process(["submit", "echo hi & echo y"])
    assert exit_code == 2, (
        f"NFR-02: '&' is on the injection blacklist, got {exit_code}"
    )


def test_fr01_submit_injection_dollar_rejected(isolated_taskq_home):
    """Injection char '$' → exit 2."""
    exit_code, _, _ = _run_in_process(["submit", "echo $HOME"])
    assert exit_code == 2, (
        f"NFR-02: '$' is on the injection blacklist, got {exit_code}"
    )


def test_fr01_submit_injection_gt_rejected(isolated_taskq_home):
    """Injection char '>' → exit 2."""
    exit_code, _, _ = _run_in_process(["submit", "echo hi > x"])
    assert exit_code == 2, (
        f"NFR-02: '>' is on the injection blacklist, got {exit_code}"
    )


def test_fr01_submit_injection_lt_rejected(isolated_taskq_home):
    """Injection char '<' → exit 2."""
    exit_code, _, _ = _run_in_process(["submit", "cat < x"])
    assert exit_code == 2, (
        f"NFR-02: '<' is on the injection blacklist, got {exit_code}"
    )


def test_fr01_submit_injection_backtick_rejected(isolated_taskq_home):
    """Injection char '`' (backtick) → exit 2."""
    exit_code, _, _ = _run_in_process(["submit", "echo `id`"])
    assert exit_code == 2, (
        f"NFR-02: '`' is on the injection blacklist, got {exit_code}"
    )


def test_fr01_submit_command_too_long_rejected(isolated_taskq_home):
    """AC-FR-01.4: command > 1000 chars → exit 2; nothing written to tasks.json.

    Sub-assertions: AC1-command-too-long, AC1-too-long-exit-2.
    """
    too_long_cmd = "a" * 1001
    exit_code, _, _ = _run_in_process(["submit", too_long_cmd])
    assert exit_code == 2, (
        f"AC-FR-01.4: 1001-char command must exit 2, got {exit_code}"
    )
    tasks_file = isolated_taskq_home / "tasks.json"
    # Per AC-FR-01.4: nothing should have been written. state_mode isolates
    # this case in a fresh tmp_path, so the file must not exist.
    assert not tasks_file.exists(), (
        f"AC-FR-01.4: tasks.json must not be created on a too-long rejection, "
        f"but exists at {tasks_file}"
    )


def test_fr01_submit_whitespace_only_rejected(isolated_taskq_home):
    """AC-FR-01.4: whitespace-only command → exit 2; nothing written.

    Sub-assertions: AC1-whitespace-only, AC1-empty-rejected.
    """
    exit_code, _, _ = _run_in_process(["submit", "   "])
    assert exit_code == 2, (
        f"AC-FR-01.4: whitespace-only command must exit 2, got {exit_code}"
    )
    tasks_file = isolated_taskq_home / "tasks.json"
    assert not tasks_file.exists(), (
        f"AC-FR-01.4: tasks.json must not be created on a whitespace "
        f"rejection, but exists at {tasks_file}"
    )


def test_fr01_submit_duplicate_name_rejected(isolated_taskq_home):
    """AC-FR-01.5: re-submitting same ``--name`` while first is pending → exit 2.

    Sub-assertions: AC1-dup-name-clash, AC1-dup-name-exit-2.
    """
    exit_code_first, _, _ = _run_in_process(
        ["submit", "echo hi", "--name", "dup"]
    )
    assert exit_code_first == 0, (
        f"first submit with --name=dup must succeed (pending state), "
        f"got {exit_code_first}"
    )
    exit_code_second, _, _ = _run_in_process(
        ["submit", "echo bye", "--name", "dup"]
    )
    assert exit_code_second == 2, (
        f"AC-FR-01.5: duplicate --name='dup' must be rejected (exit 2), "
        f"got {exit_code_second}"
    )


def test_fr01_submit_invalid_dependency_rejected(isolated_taskq_home):
    """AC-FR-01.6: ``--after`` referencing non-existent task id → exit 2; stderr names it.

    Sub-assertions: AC1-missing-dep, AC1-missing-dep-exit-2.
    """
    missing_id = "deadbeef"
    exit_code, _, stderr_text = _run_in_process(
        ["submit", "echo hi", "--after", missing_id]
    )
    assert exit_code == 2, (
        f"AC-FR-01.6: --after=<unknown id> must exit 2, got {exit_code}"
    )
    assert missing_id in stderr_text, (
        f"AC-FR-01.6: stderr must identify the unknown dependency id "
        f"{missing_id!r}; got {stderr_text!r}"
    )


def test_fr01_submit_emits_audit_event(isolated_taskq_home):
    """AC-FR-01.7: successful submit writes exactly one ``submit`` audit JSONL event.

    This is the only FR-01 case driven out-of-process (per TEST_SPEC
    subprocess_mode="out_of_process"; shared_TASKQ_HOME="false") so it
    exercises the real ``python -m taskq_plus`` entry point instead of an
    in-process helper.

    Sub-assertions: AC1-audit-event-name, AC1-audit-task-id-matches.
    """
    home = isolated_taskq_home
    audit_log_file = home / "audit.jsonl"

    # Propagate PYTHONPATH explicitly: pytest's pythonpath setting does NOT
    # propagate to the child process (per integration FR guidelines).
    child_env = os.environ.copy()
    child_env["TASKQ_HOME"] = str(home)
    child_env["TASKQ_AUDIT_LOG"] = str(audit_log_file)
    child_env["TASKQ_MAX_WORKERS"] = "4"
    child_env["TASKQ_RETRY_LIMIT"] = "2"
    child_env["TASKQ_BREAKER_THRESHOLD"] = "3"
    src_root = Path(__file__).resolve().parents[2] / "03-development" / "src"
    child_env["PYTHONPATH"] = str(src_root) + os.pathsep + child_env.get(
        "PYTHONPATH", ""
    )

    # GREEN TODO: child_env must make the audit log survive the round-trip.
    completed = subprocess.run(
        [sys.executable, "-m", "taskq_plus", "submit", "echo hi"],
        env=child_env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, (
        f"AC-FR-01.7: out-of-process submit must exit 0, got "
        f"{completed.returncode}; stderr={completed.stderr!r}"
    )
    task_id_text = completed.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{8}", task_id_text), (
        f"AC-FR-01.7: out-of-process stdout must be 8-hex id, "
        f"got {task_id_text!r}"
    )

    assert audit_log_file.exists(), (
        f"AC-FR-01.7: $TASKQ_AUDIT_LOG must exist after submit; "
        f"checked {audit_log_file}"
    )
    log_text = audit_log_file.read_text()
    lines = [ln for ln in log_text.splitlines() if ln.strip()]
    assert len(lines) == 1, (
        f"AC-FR-01.7: exactly one audit event per submit (FR-08 / NFR-09), "
        f"found {len(lines)} lines: {lines!r}"
    )
    event = json.loads(lines[0])
    assert event.get("event") == "submit", (
        f"AC1-audit-event-name: audit event must be 'submit', got {event!r}"
    )
    assert event.get("task_id") == task_id_text, (
        f"AC1-audit-task-id-matches: audit task_id {event.get('task_id')!r} "
        f"must match stdout id {task_id_text!r}"
    )
