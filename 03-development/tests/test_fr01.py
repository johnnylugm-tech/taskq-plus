"""FR-01 RED tests — submit validation, id generation, audit emit.

Per TEST_SPEC.md §FR-01, there are 11 sub-rows:
  - test_fr01_a  (happy path)        row 1
  - test_fr01_b  (empty command)     row 2
  - test_fr01_c  (injection ';')     row 3  (AC-FR-01.c canonical)
  - test_fr01_d  (per-char)          rows 4..11 (parametrised over 8 chars)

Sub-assertions (rule_id → predicate):
  AC1-cmd-nonempty          : `len(command) > 0`                            rows 1, 3..11
  AC1-cmd-empty-rejected    : `len(command) == 0`                           row 2
  AC1-id-is-8-hex           : `int(expected_id_len) == 8 and expected_exit_code == "0"` row 1
  AC1-reject-semicolon      : `";" in command`                              rows 3, 4
  AC1-reject-pipe           : `"|" in command`                              rows 5, 11
  AC1-reject-ampersand      : `"&" in command`                              row 6
  AC1-reject-dollar         : `"$" in command`                              row 7
  AC1-reject-greater        : `">" in command`                              row 8
  AC1-reject-less           : `"<" in command`                              row 9
  AC1-reject-backtick       : `` "`" in command ``                          row 10

This file is the TDD-RED deliverable: it is EXPECTED to fail because the
source modules declared by SAB.json
(`taskq_plus.models.task`, `taskq_plus.storage.task_store`,
`taskq_plus.cli.commands`) do not exist on disk yet. A pytest Collection
Error (Exit Code 2) or test-time ImportError / assertion failure is a VALID
RED outcome — do not hide the missing source.
"""

import contextlib
import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC = ROOT / "src"
HOME_VAR = "TASKQ_HOME"

# Make src/ importable for in-process tests (subprocess tests do NOT use this,
# they propagate PYTHONPATH via env to the child process).
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# In-process imports (these DO get tracked by coverage; subprocess tests do NOT).
from taskq_plus.cli import commands as cli_commands  # noqa: E402
from taskq_plus.cli.commands import dispatch, main  # noqa: E402
from taskq_plus.models.task import (  # noqa: E402
    MAX_COMMAND_LENGTH,
    TaskSubmission,
    generate_task_id,
)
from taskq_plus.storage.task_store import (  # noqa: E402
    _atomic_write_json,
    _now_iso,
    append_task,
    find_by_id,
    find_by_name,
    load_tasks,
    save_tasks,
    tasks_path,
)


# ---------------------------------------------------------------------------
# Per-test isolation: fresh TASKQ_HOME, no shared state across parametrize ids.
# ---------------------------------------------------------------------------
@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Function-scoped TASKQ_HOME — every test gets a clean directory."""
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv(HOME_VAR, str(home))
    return home


# ---------------------------------------------------------------------------
# Subprocess helper (out-of-process, mirrors the FR-01 AC literally).
# Propagates PYTHONPATH to the child so the child's `taskq_plus` package is
# importable; pytest's `pythonpath = ...` does NOT propagate to subprocesses.
# ---------------------------------------------------------------------------
def _run_cli(argv, taskq_home_path):
    env = os.environ.copy()
    env[HOME_VAR] = str(taskq_home_path)
    py_path = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = py_path
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(taskq_home_path),
    )


# ===========================================================================
# Test cases — names MUST match TEST_SPEC.md §FR-01 verbatim.
# ===========================================================================

# ---- row 1 : happy path --------------------------------------------------
# NFR-02: command must be a non-empty string with no blacklisted injection chars
def test_fr01_a(taskq_home):  # NFR-02 (valid-input happy path)
    """AC-FR-01.a: `python -m taskq_plus submit "echo hi"` → stdout 8-hex id, exit 0.

    Predicate: `len(command) > 0 and int(expected_id_len) == 8 and expected_exit_code == "0"`.
    """
    expected_id_len = 8
    expected_exit_code = 0

    proc = _run_cli(["submit", "echo hi"], taskq_home)

    assert proc.returncode == expected_exit_code, (
        f"expected exit {expected_exit_code}, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    stdout_id = proc.stdout.strip()
    assert re.match(r"^[0-9a-f]{8}$", stdout_id), (
        f"stdout {stdout_id!r} is not an 8-char lowercase hex id"
    )
    assert len(stdout_id) == expected_id_len


# ---- row 2 : empty command rejected ------------------------------------
def test_fr01_b(taskq_home):  # NFR-05 (FR-01 validation contract)
    """AC-FR-01.b: `python -m taskq_plus submit ""` → exit 2.

    Predicate: `len(command) == 0`.
    """
    command = ""

    proc = _run_cli(["submit", command], taskq_home)

    assert proc.returncode == 2, (
        f"expected exit 2 for empty command, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )


# ---- row 3 : canonical injection example -------------------------------
def test_fr01_c(taskq_home):  # NFR-02 (injection blacklist canonical)
    """AC-FR-01.c: `python -m taskq_plus submit "echo hi; rm x"` → exit 2.

    Predicate: `";" in command`.
    """
    command = "echo hi; rm x"

    proc = _run_cli(["submit", command], taskq_home)

    assert proc.returncode == 2, (
        f"expected exit 2 for ';' injection, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )


# ---- rows 4..11 : one test per blacklisted injection character ---------
# Per NFR-02: "FR-01 注入字元黑名單必須有測試覆蓋(每個字元一個 case)".
# Parametrised by `char_name` so each blacklisted char gets its own pytest id.
@pytest.mark.parametrize(
    ("command", "char_name"),
    [
        ("echo hi;", "semicolon"),       # row  4 — ';' blacklist
        ("echo hi | wc", "pipe"),        # row  5 — '|' blacklist
        ("echo hi &", "ampersand"),      # row  6 — '&' blacklist
        ("echo hi $x", "dollar"),        # row  7 — '$' blacklist
        ("echo hi > file", "greater"),   # row  8 — '>' blacklist
        ("echo hi < file", "less"),      # row  9 — '<' blacklist
        ("echo hi `cmd`", "backtick"),   # row 10 — '`' blacklist
        ("echo hi|wc", "escaped_pipe"),  # row 11 — escaped-pipe edge case
    ],
)
def test_fr01_d(taskq_home, command, char_name):  # NFR-02 (per-char injection coverage)
    r"""AC-FR-01.d: one test per blacklisted injection char → exit 2.

    Per-char coverage (NFR-02 verbatim): `; | & $ > < \``.
    The 8th row uses a literal `|` written `\|` in TEST_SPEC.md markdown —
    the predicate namespace still receives the real `|` character.
    """
    proc = _run_cli(["submit", command], taskq_home)

    assert proc.returncode == 2, (
        f"command={command!r} (char={char_name}) should exit 2, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )


# ===========================================================================
# In-process unit tests — provide measurable coverage for the source modules.
# ===========================================================================
# The subprocess tests above verify the real CLI entry point but cannot be
# measured by pytest-cov (subprocesses are out-of-process). The tests below
# call `cli_commands.dispatch` / `cli_commands._submit` / storage functions
# directly so coverage tooling can see the source lines exercised.


def _run_dispatch(argv, taskq_home_path):
    """Run dispatch in-process; return (exit_code, stdout, stderr).

    argparse errors call sys.exit(2) — capture SystemExit so the test does
    not propagate it. Real exit code is preserved in the returned tuple.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = dispatch(list(argv), taskq_home=Path(taskq_home_path))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def _run_main(argv, taskq_home_path):
    """Run main() in-process with TASKQ_HOME set; return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    prior = os.environ.get(HOME_VAR)
    os.environ[HOME_VAR] = str(taskq_home_path)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
    finally:
        if prior is None:
            os.environ.pop(HOME_VAR, None)
        else:
            os.environ[HOME_VAR] = prior
    return code, out.getvalue(), err.getvalue()


# ---- TaskSubmission direct validation ---------------------------------
class TestTaskSubmissionValidation:
    """Direct pydantic validation tests for TaskSubmission."""

    def test_valid_command(self):
        """Non-empty valid command round-trips."""
        sub = TaskSubmission(command="echo hi")
        assert sub.command == "echo hi"
        assert sub.name is None
        assert sub.depends_on == []

    def test_empty_string_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskSubmission(command="")

    def test_whitespace_only_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskSubmission(command="   ")

    def test_too_long_rejected(self):
        from pydantic import ValidationError

        long_cmd = "a" * (MAX_COMMAND_LENGTH + 1)
        with pytest.raises(ValidationError) as exc_info:
            TaskSubmission(command=long_cmd)
        assert "exceeds max" in str(exc_info.value)

    @pytest.mark.parametrize(
        "char",
        [";", "|", "&", "$", ">", "<", "`"],
    )
    def test_injection_chars_rejected(self, char):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            TaskSubmission(command=f"echo hi{char}x")
        assert "injection" in str(exc_info.value).lower()

    def test_optional_name_and_depends_on(self):
        sub = TaskSubmission(command="ls", name="myname", depends_on=["abc12345"])
        assert sub.name == "myname"
        assert sub.depends_on == ["abc12345"]


# ---- generate_task_id -------------------------------------------------
class TestGenerateTaskId:
    """UUID4-prefix id generation."""

    def test_id_is_eight_hex(self):
        tid = generate_task_id()
        assert len(tid) == 8
        assert re.match(r"^[0-9a-f]{8}$", tid)

    def test_ids_are_unique(self):
        ids = {generate_task_id() for _ in range(64)}
        assert len(ids) == 64


# ---- storage.task_store in-process -------------------------------------
class TestTaskStore:
    """Direct tests for atomic JSON store functions."""

    def test_load_missing_returns_empty(self, taskq_home):
        assert load_tasks() == []

    def test_save_and_load_roundtrip(self, taskq_home):
        sample = [{"id": "abc12345", "command": "ls", "status": "pending"}]
        save_tasks(sample)
        assert load_tasks() == sample

    def test_save_creates_file(self, taskq_home):
        save_tasks([{"id": "x", "command": "y"}])
        assert tasks_path().exists()

    def test_atomic_write_direct(self, taskq_home):
        target = taskq_home / "data.json"
        _atomic_write_json(target, {"k": "v"})
        import json as _json

        assert _json.loads(target.read_text()) == {"k": "v"}

    def test_atomic_write_creates_parent_dirs(self, taskq_home):
        nested = taskq_home / "deep" / "nested" / "x.json"
        _atomic_write_json(nested, [1, 2, 3])
        assert nested.exists()

    def test_load_corrupted_json_returns_empty(self, taskq_home):
        tasks_path().write_text("not valid json {", encoding="utf-8")
        assert load_tasks() == []

    def test_load_dict_with_tasks_key_returns_inner_list(self, taskq_home):
        tasks_path().write_text(
            '{"tasks": [{"id": "abc12345"}]}', encoding="utf-8"
        )
        assert load_tasks() == [{"id": "abc12345"}]

    def test_load_other_shape_returns_empty(self, taskq_home):
        tasks_path().write_text('{"foo": "bar"}', encoding="utf-8")
        assert load_tasks() == []

    def test_append_task_persists_with_created_at(self, taskq_home):
        rec = append_task({"id": "abc12345", "command": "ls"})
        assert "created_at" in rec
        assert rec["created_at"].endswith("Z")
        assert load_tasks() == [rec]

    def test_append_task_does_not_overwrite_existing_created_at(self, taskq_home):
        first = append_task({"id": "abc12345", "command": "ls", "created_at": "FIXED"})
        assert first["created_at"] == "FIXED"
        assert load_tasks()[0]["created_at"] == "FIXED"

    def test_find_by_id_present(self, taskq_home):
        save_tasks([{"id": "abc12345", "command": "ls"}])
        assert find_by_id("abc12345") == {"id": "abc12345", "command": "ls"}

    def test_find_by_id_missing(self, taskq_home):
        assert find_by_id("missing") is None

    def test_find_by_name_none_returns_none(self, taskq_home):
        assert find_by_name(None) is None

    def test_find_by_name_active_only(self, taskq_home):
        # done tasks should NOT be considered as name-occupiers
        save_tasks(
            [
                {"id": "id_done", "name": "dup", "status": "done"},
                {"id": "id_pending", "name": "dup", "status": "pending"},
            ]
        )
        assert find_by_name("dup") == {
            "id": "id_pending",
            "name": "dup",
            "status": "pending",
        }

    def test_find_by_name_case_sensitive(self, taskq_home):
        save_tasks([{"id": "id_p", "name": "Foo", "status": "pending"}])
        assert find_by_name("foo") is None
        assert find_by_name("Foo") is not None

    def test_now_iso_utc_z_suffix(self):
        stamp = _now_iso()
        assert stamp.endswith("Z")
        assert "+00:00" not in stamp

    def test_atomic_write_cleans_up_temp_on_failure(self, taskq_home):
        """When json.dump raises, the temp file is removed (cleanup branch)."""
        from unittest.mock import patch

        target = taskq_home / "data.json"
        with patch(
            "taskq_plus.storage.task_store.json.dump",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                _atomic_write_json(target, {"k": "v"})
        # no leftover temp files and no target written
        temps = [p for p in taskq_home.iterdir() if p.name.startswith("data.json.")]
        assert temps == []
        assert not target.exists()

    def test_atomic_write_unlink_failure_swallowed(self, taskq_home):
        """When unlink itself raises OSError, the inner except-OSError swallows it."""
        from unittest.mock import patch

        target = taskq_home / "data.json"
        with patch(
            "taskq_plus.storage.task_store.json.dump",
            side_effect=OSError("disk full"),
        ), patch(
            "taskq_plus.storage.task_store.os.unlink",
            side_effect=OSError("permission denied"),
        ):
            with pytest.raises(OSError):
                _atomic_write_json(target, {"k": "v"})
        # the original OSError from json.dump is what propagates, not the unlink one


# ---- dispatch + _submit in-process -------------------------------------
class TestDispatchInProcess:
    """In-process dispatch tests (measure coverage of cli/commands.py)."""

    def test_main_happy_path(self, taskq_home):
        code, out, err = _run_main(["submit", "echo hi"], taskq_home)
        assert code == 0
        tid = out.strip()
        assert re.match(r"^[0-9a-f]{8}$", tid)

    def test_dispatch_empty_argv_shows_help(self, taskq_home):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dispatch([], taskq_home=taskq_home)
        assert code == 0
        # argparse prints usage on the captured stdout stream
        assert "usage" in out.getvalue().lower()

    def test_dispatch_unknown_command(self, taskq_home):
        code, out, err = _run_dispatch(["nope"], taskq_home)
        assert code == cli_commands.EXIT_VALIDATION_ERROR
        assert "unknown command" in err

    def test_submit_empty_command(self, taskq_home):
        code, out, err = _run_dispatch(["submit", ""], taskq_home)
        assert code == cli_commands.EXIT_VALIDATION_ERROR
        assert "error:" in err

    def test_submit_whitespace_command(self, taskq_home):
        code, out, err = _run_dispatch(["submit", "   "], taskq_home)
        assert code == cli_commands.EXIT_VALIDATION_ERROR

    def test_submit_too_long_command(self, taskq_home):
        code, out, err = _run_dispatch(["submit", "a" * (MAX_COMMAND_LENGTH + 1)], taskq_home)
        assert code == cli_commands.EXIT_VALIDATION_ERROR

    @pytest.mark.parametrize(
        ("command", "char_name"),
        [
            ("echo hi;", "semicolon"),
            ("echo hi|wc", "pipe"),
            ("echo hi &", "ampersand"),
            ("echo hi $x", "dollar"),
            ("echo hi > file", "greater"),
            ("echo hi < file", "less"),
            ("echo hi `cmd`", "backtick"),
        ],
    )
    def test_submit_injection_chars_rejected(self, taskq_home, command, char_name):
        code, out, err = _run_dispatch(["submit", command], taskq_home)
        assert code == cli_commands.EXIT_VALIDATION_ERROR, (
            f"injection char {char_name} not rejected (cmd={command!r})"
        )

    def test_submit_json_output(self, taskq_home):
        code, out, err = _run_dispatch(["submit", "echo hi", "--json"], taskq_home)
        assert code == 0
        import json as _json

        payload = _json.loads(out.strip())
        assert payload["status"] == "pending"
        assert re.match(r"^[0-9a-f]{8}$", payload["id"])

    def test_submit_persists_task_and_audit(self, taskq_home):
        code, out, err = _run_dispatch(["submit", "echo hi"], taskq_home)
        assert code == 0
        # task should be on disk
        tasks = load_tasks()
        assert len(tasks) == 1
        assert tasks[0]["command"] == "echo hi"
        assert tasks[0]["status"] == "pending"
        # audit line should exist
        audit = taskq_home / "audit.log"
        assert audit.exists()
        line = audit.read_text(encoding="utf-8").strip().splitlines()[-1]
        import json as _json

        evt = _json.loads(line)
        assert evt["event"] == "submit"
        assert evt["command"] == "echo hi"
        assert evt["id"] == tasks[0]["id"]

    def test_submit_with_name_persists_name(self, taskq_home):
        code, out, err = _run_dispatch(
            ["submit", "echo hi", "--name", "myname"], taskq_home
        )
        assert code == 0
        tasks = load_tasks()
        assert tasks[0]["name"] == "myname"

    def test_submit_duplicate_name_rejected(self, taskq_home):
        # seed a pending task with name=myname
        append_task({"id": "abcdef01", "command": "ls", "name": "myname", "status": "pending"})
        code, out, err = _run_dispatch(
            ["submit", "echo hi", "--name", "myname"], taskq_home
        )
        assert code == cli_commands.EXIT_VALIDATION_ERROR
        assert "already used" in err

    def test_submit_done_task_name_can_be_reused(self, taskq_home):
        # done tasks release their name slot
        append_task({"id": "abcdef01", "command": "ls", "name": "myname", "status": "done"})
        code, out, err = _run_dispatch(
            ["submit", "echo hi", "--name", "myname"], taskq_home
        )
        assert code == 0

    def test_submit_missing_dependency_returns_3(self, taskq_home):
        code, out, err = _run_dispatch(
            ["submit", "echo hi", "--after", "ffffffff"], taskq_home
        )
        assert code == cli_commands.EXIT_NOT_FOUND
        assert "does not exist" in err

    def test_submit_satisfied_dependency(self, taskq_home):
        # dependency exists → submission succeeds
        append_task({"id": "abcdef01", "command": "ls", "status": "pending"})
        code, out, err = _run_dispatch(
            ["submit", "echo hi", "--after", "abcdef01"], taskq_home
        )
        assert code == 0
        tasks = load_tasks()
        assert tasks[-1]["depends_on"] == ["abcdef01"]

    def test_submit_no_args_calls_parser_error(self, taskq_home):
        # argparse error from missing positional → SystemExit(2)
        code, out, err = _run_dispatch(["submit"], taskq_home)
        assert code != 0  # argparse returns 2 on usage error

    def test_main_none_argv_uses_sys_argv(self, taskq_home, monkeypatch):
        """main() with argv=None must read sys.argv[1:] (the CLI entry path)."""
        monkeypatch.setattr(sys, "argv", ["taskq_plus", "submit", "echo hi"])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main()  # argv=None branch — covers line 159
        assert code == 0
        assert re.match(r"^[0-9a-f]{8}$", out.getvalue().strip())
