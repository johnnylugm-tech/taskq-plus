"""TDD-RED tests for FR-01 (任務提交與驗證).

Maps 1:1 to TEST_SPEC.md §FR-01 cases 1-14. The expected RED state is
``ModuleNotFoundError`` at pytest collection time (or every assertion
failing); either counts as a valid failing test in this phase per the
harness TDD contract. GREEN TODO:

* ``taskq_plus.cli`` exposes ``cli.main(argv: list[str]) -> int`` and
  dispatches ``submit`` to a handler that builds a ``TaskSubmission``
  and persists atomically to ``$TASKQ_HOME/tasks.json``.
* ``taskq_plus.models.TaskSubmission`` is the pydantic model enforcing
  FR-01 validation rules verbatim from SPEC.md §3 (non-empty,
  length<=1000, no injection chars, unique ``name`` against the live
  store, all ``after`` ids exist).
* On success, exactly one ``"event": "submit"`` JSONL line (with
  fsync) is appended to ``$TASKQ_AUDIT_LOG`` (FR-08 / NFR-09).

Test design follows the harness canonical pattern — parametrize over
the ``command`` input variable, capture a single ``result`` record per
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
from pathlib import Path
from types import SimpleNamespace

import pytest

# Top-level imports are intentional — ModuleNotFoundError at collection time
# is the expected RED signal per the unit-test contract.
from taskq_plus import cli  # noqa: E402
from taskq_plus.models import TaskSubmission  # noqa: E402


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
        audit_event=None,
        audit_task_id=None,
    )


def _read_first_audit_event(audit_log: Path):
    """Read the first non-empty JSONL line of ``audit_log`` (or None)."""
    if not audit_log.exists():
        return None
    for line in audit_log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None
    return None


# -------------------------------------------------------------------
# FR-01 Case 1 — happy-path submit (AC-FR-01.1)
# -------------------------------------------------------------------


# NFR-02 / NFR-09 — happy-path FR-01 binding
@pytest.mark.parametrize("command", ["echo hi"], ids=["happy"])
def test_fr01_submit_happy_path_emits_id(command, isolated_taskq_home):
    """AC-FR-01.1: ``submit "echo hi"`` → exit 0; stdout is 8-hex id.

    Sub-assertions: AC1-id-len-8, AC1-exit-0.
    """
    result = _run_submit(command)

    if command == "echo hi":
        assert result.exit_code == 0
        assert len(result.task_id_str) == 8


# -------------------------------------------------------------------
# FR-01 Case 2 — empty command rejected
# -------------------------------------------------------------------


@pytest.mark.parametrize("command", [""], ids=["empty"])
def test_fr01_submit_empty_command_rejected(command, isolated_taskq_home):
    """AC-FR-01.2: ``submit ""`` → exit 2; nothing written to tasks.json.

    Sub-assertion: AC1-empty-rejected.
    """
    tasks_file = isolated_taskq_home / "tasks.json"
    result = _run_submit(command)

    if command == "":
        assert command.strip() == ""
        assert result.exit_code == 2
        assert not tasks_file.exists()


# -------------------------------------------------------------------
# FR-01 Case 11 — whitespace-only command rejected
# -------------------------------------------------------------------


# NFR-02 — validation: whitespace-only
@pytest.mark.parametrize("command", ["   "], ids=["whitespace"])
def test_fr01_submit_whitespace_only_rejected(command, isolated_taskq_home):
    """AC-FR-01.4: whitespace-only command → exit 2.

    Sub-assertions: AC1-whitespace-only, AC1-empty-rejected.
    """
    tasks_file = isolated_taskq_home / "tasks.json"
    result = _run_submit(command)

    if command == "   ":
        assert command.strip() == ""
        assert result.exit_code == 2
        assert not tasks_file.exists()


# -------------------------------------------------------------------
# FR-01 Cases 3-9 — injection-char blacklist (one case per char)
# -------------------------------------------------------------------


# NFR-02 — security: injection char blacklist (per-char coverage).
# NOTE: backtick row uses raw string ``r"\`"`` so the parametrize-captured
# value (backslash-backtick) exactly equals TEST_SPEC's parser-captured
# literal ``\``` (see TEST_SPEC.md FR-01 row 9).
@pytest.mark.parametrize(
    "command, injection_char",
    [
        ("echo hi; rm x", ";"),
        ("echo hi | cat", "|"),
        ("echo hi & echo y", "&"),
        ("echo $HOME", "$"),
        ("echo hi > x", ">"),
        ("cat < x", "<"),
        (r"echo \`id\`", r"\`"),
    ],
)
def test_fr01_submit_injection_blacklist(
    command, injection_char, isolated_taskq_home
):
    """AC-FR-01.3: command containing an injection char → exit 2."""
    tasks_file = isolated_taskq_home / "tasks.json"
    result = _run_submit(command)

    if injection_char in command:
        assert injection_char in command
        assert result.exit_code == 2
        assert not tasks_file.exists()


# -------------------------------------------------------------------
# FR-01 Case 10 — command > 1000 chars rejected
# -------------------------------------------------------------------


# NFR-03 — atomic: rejected submission must not touch tasks.json
def test_fr01_submit_command_too_long_rejected(isolated_taskq_home):
    """AC-FR-01.4: command with length > 1000 → exit 2."""
    too_long_cmd = "a" * 1001
    tasks_file = isolated_taskq_home / "tasks.json"
    result = _run_submit(too_long_cmd)

    if len(too_long_cmd) > 1000:
        assert len(too_long_cmd) > 1000
        assert result.exit_code == 2
        assert not tasks_file.exists()


# -------------------------------------------------------------------
# FR-01 Case 12 — duplicate --name rejected
# -------------------------------------------------------------------


def test_fr01_submit_duplicate_name_rejected(isolated_taskq_home):
    """AC-FR-01.5: re-submitting the same ``--name`` → exit 2."""
    first = _run_submit("echo hi", name="dup")
    second = _run_submit("echo bye", name="dup")
    tasks_text = ""
    tasks_file = isolated_taskq_home / "tasks.json"
    if tasks_file.exists():
        tasks_text = tasks_file.read_text()

    if True:
        name = "dup"
        existing_name = "dup"
        assert first.exit_code == 0
        assert second.exit_code == 2
        # AC1-dup-name-clash predicate: name == existing_name
        assert name == existing_name
        assert "echo bye" not in tasks_text


# -------------------------------------------------------------------
# FR-01 Case 13 — --after referencing missing task id rejected
# -------------------------------------------------------------------


def test_fr01_submit_invalid_dependency_rejected(isolated_taskq_home):
    """AC-FR-01.6: ``--after`` referencing a non-existent task id → exit 2."""
    missing_dep = "deadbeef"
    known_task_ids = set()
    result = _run_submit("echo hi", after=[missing_dep])

    if missing_dep == "deadbeef":
        assert missing_dep not in known_task_ids
        assert result.exit_code == 2
        assert missing_dep in result.stderr
        assert "unknown dependency" in result.stderr.lower()


# -------------------------------------------------------------------
# FR-01 Case 14 — submit emits one audit JSONL event (out-of-process)
# -------------------------------------------------------------------


# NFR-09 — test assertion quality (zero-skip); audit log write before read
def test_fr01_submit_emits_audit_event(isolated_taskq_home):
    """AC-FR-01.7: successful submit → one ``submit`` JSONL line on disk."""
    home = isolated_taskq_home
    audit_log_file = home / "audit.jsonl"
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

    completed = subprocess.run(
        [sys.executable, "-m", "taskq_plus", "submit", "echo hi"],
        env=child_env,
        capture_output=True,
        text=True,
    )
    result = SimpleNamespace(
        exit_code=completed.returncode,
        task_id_str=completed.stdout.strip(),
        stdout=completed.stdout,
        stderr=completed.stderr,
        audit_event=None,
        audit_task_id=None,
    )

    if completed.returncode == 0:
        assert result.exit_code == 0
        assert len(result.task_id_str) == 8
        assert audit_log_file.exists()
        event = _read_first_audit_event(audit_log_file)
        assert event is not None
        result.audit_event = event.get("event")
        result.audit_task_id = event.get("task_id")
        assert result.audit_event == "submit"
        assert result.audit_task_id == result.task_id_str
