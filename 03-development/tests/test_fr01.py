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
from taskq_plus.cli.commands import (  # noqa: E402
    dispatch,
    main,
    submit_cmd,
    run_cmd,
    status_cmd,
    list_cmd,
    graph_cmd,
    plugins_cmd,
    export_cmd,
    clear_cmd,
)
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

    def test_dispatch_routes_run(self, taskq_home, monkeypatch):
        """dispatch(['run', ...]) routes through _run (line 695)."""
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "done", "exit_code": 0}

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        append_task({"id": "abcdef01", "command": "echo hi", "status": "pending"})
        code, out, err = _run_dispatch(["run", "abcdef01"], taskq_home)
        assert code == 0

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


# ===========================================================================
# In-process tests for FR-05 command handlers in cli/commands.py.
# These call `submit_cmd` / `run_cmd` / `status_cmd` / `list_cmd` / `graph_cmd`
# / `plugins_cmd` / `export_cmd` / `clear_cmd` directly so coverage tooling can
# measure them. Each handler returns a plain dict and never prints.
# ===========================================================================


class TestSubmitCmdHandler:
    """submit_cmd (FR-05) — full happy path + cycle/depth/audit."""

    def test_happy_path_returns_dict(self, taskq_home):
        result = submit_cmd("echo hi")
        assert isinstance(result, dict)
        assert result["status"] == "pending"
        assert re.match(r"^[0-9a-f]{8}$", result["id"])
        assert result["command"] == "echo hi"
        assert result["name"] is None

    def test_happy_path_persists_task(self, taskq_home):
        result = submit_cmd("echo hi", name="myname")
        rec = find_by_id(result["id"])
        assert rec is not None
        assert rec["command"] == "echo hi"
        assert rec["name"] == "myname"
        assert rec["status"] == "pending"

    def test_validation_error_raises(self, taskq_home):
        with pytest.raises(cli_commands.SubmitValidationError):
            submit_cmd("")
        with pytest.raises(cli_commands.SubmitValidationError):
            submit_cmd("echo hi; rm x")

    def test_duplicate_name_rejected(self, taskq_home):
        append_task(
            {"id": "abcdef01", "command": "ls", "name": "myname", "status": "pending"}
        )
        with pytest.raises(cli_commands.SubmitValidationError) as exc_info:
            submit_cmd("echo hi", name="myname")
        assert "already used" in str(exc_info.value)

    def test_missing_dependency_rejected(self, taskq_home):
        with pytest.raises(cli_commands.SubmitValidationError) as exc_info:
            submit_cmd("echo hi", after=["deadbeef"])
        assert "does not exist" in str(exc_info.value)

    def test_satisfied_dependency(self, taskq_home):
        append_task({"id": "abcdef01", "command": "ls", "status": "pending"})
        result = submit_cmd("echo hi", after=["abcdef01"])
        assert find_by_id(result["id"])["depends_on"] == ["abcdef01"]

    def test_depth_exceeded_raises_graph_error(self, taskq_home):
        """Chain > MAX_DAG_DEPTH triggers the FR-06 depth cap."""
        # Build a linear chain longer than the cap (33 nodes).
        prev_id = None
        for i in range(34):
            rec = append_task(
                {"id": f"{i:08x}", "command": f"echo {i}", "status": "pending",
                 "depends_on": [prev_id] if prev_id else []}
            )
            prev_id = rec["id"]
        with pytest.raises(cli_commands.GraphError) as exc_info:
            submit_cmd("echo tip", after=[prev_id])
        assert "too deep" in str(exc_info.value).lower()

    def test_cycle_in_existing_store_rejected(self, taskq_home):
        """A pre-existing cycle in tasks.json makes submit refuse (FR-06)."""
        # Manually construct a cyclic store (not via submit_cmd to avoid the
        # same validator running during seeding).
        cyclic = [
            {"id": "aaaaaaaa", "command": "echo a", "depends_on": ["cccccccc"], "status": "pending"},
            {"id": "bbbbbbbb", "command": "echo b", "depends_on": ["aaaaaaaa"], "status": "pending"},
            {"id": "cccccccc", "command": "echo c", "depends_on": ["bbbbbbbb"], "status": "pending"},
        ]
        save_tasks(cyclic)
        with pytest.raises(cli_commands.GraphError) as exc_info:
            submit_cmd("echo new")
        assert "cycle" in str(exc_info.value).lower()

    def test_emit_audit_legacy_line(self, taskq_home):
        submit_cmd("echo hi")
        audit = taskq_home / "audit.log"
        assert audit.exists()
        body = audit.read_text(encoding="utf-8")
        assert "submit" in body

    def test_emit_structured_audit(self, taskq_home):
        """FR-08 audit.jsonl gets a structured record with correlation_id."""
        submit_cmd("echo hi", name="hello")
        journal = taskq_home / "audit.jsonl"
        assert journal.exists()
        lines = [ln for ln in journal.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert lines, "audit.jsonl should have at least one event"
        import json as _json

        evt = _json.loads(lines[-1])
        assert evt["event"] == "submit"
        assert evt["detail"]["name"] == "hello"
        assert "correlation_id" in evt
        assert re.match(r"^[0-9a-f]{8}$", evt["correlation_id"])

    def test_taskq_home_overrides_audit_dir(self, taskq_home, monkeypatch):
        """audit.log is written under TASKQ_HOME (default)."""
        # Submit writes the audit line using TASKQ_HOME; the journal lives
        # under the same directory by default.
        submit_cmd("echo hi")
        assert (taskq_home / "audit.log").exists()


class TestRunCmdHandler:
    """run_cmd (FR-02 / FR-05) — run_all + single-task paths."""

    def test_run_all_returns_dict(self, taskq_home, monkeypatch):
        # Stub exec_run_all so we don't actually fork processes.
        from taskq_plus.service import executor as exec_mod

        calls = []

        def fake_run_all():
            calls.append("ran")

        monkeypatch.setattr(exec_mod, "run_all", fake_run_all)
        # re-import bound name in commands
        monkeypatch.setattr(cli_commands, "exec_run_all", fake_run_all)
        result = run_cmd(run_all=True)
        assert result == {"ran_all": True, "exit_code": cli_commands.EXIT_OK}
        assert calls == ["ran"]

    def test_missing_task_id_raises(self, taskq_home):
        with pytest.raises(cli_commands.RunValidationError):
            run_cmd()
        with pytest.raises(cli_commands.RunValidationError):
            run_cmd(task_id="")

    def test_unknown_task_id_raises(self, taskq_home):
        with pytest.raises(cli_commands.RunValidationError) as exc_info:
            run_cmd(task_id="deadbeef")
        assert "does not exist" in str(exc_info.value)

    def test_run_done_returns_exit_zero(self, taskq_home, monkeypatch):
        append_task({"id": "abcdef01", "command": "echo ok", "status": "pending"})
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "done", "exit_code": 0, "result": "ok"}

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        result = run_cmd(task_id="abcdef01")
        assert result["exit_code"] == 0

    def test_run_timeout_returns_exit_4(self, taskq_home, monkeypatch):
        append_task({"id": "abcdef01", "command": "sleep 9", "status": "pending"})
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "timeout", "exit_code": 4}

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        result = run_cmd(task_id="abcdef01")
        assert result["exit_code"] == 4

    def test_run_int_exit_code_propagates(self, taskq_home, monkeypatch):
        """Cache HIT (exit_code=0) propagates verbatim — covers lines 343-347."""
        append_task({"id": "abcdef01", "command": "echo hi", "status": "pending"})
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "done", "exit_code": 0, "cached": True}

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        result = run_cmd(task_id="abcdef01")
        assert result["exit_code"] == 0
        assert result.get("cached") is True

    def test_run_status_done_without_exit_code(self, taskq_home, monkeypatch):
        """status==done with no int exit_code → EXIT_OK (line 351)."""
        append_task({"id": "abcdef01", "command": "echo hi", "status": "pending"})
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "done"}  # no exit_code int

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        result = run_cmd(task_id="abcdef01")
        assert result["exit_code"] == 0

    def test_run_status_timeout_without_exit_code(self, taskq_home, monkeypatch):
        """status==timeout with no int exit_code → EXIT_TIMEOUT (line 353)."""
        append_task({"id": "abcdef01", "command": "echo hi", "status": "pending"})
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "timeout"}  # no exit_code int

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        result = run_cmd(task_id="abcdef01")
        assert result["exit_code"] == 4

    def test_run_returns_none_raises_internal(self, taskq_home, monkeypatch):
        """execute_with_cache returning None → RunInternalError (line 338)."""
        append_task({"id": "abcdef01", "command": "echo hi", "status": "pending"})
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return None

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        with pytest.raises(cli_commands.RunInternalError):
            run_cmd(task_id="abcdef01")

    def test_run_unknown_status_returns_failed(self, taskq_home, monkeypatch):
        """When status is not done/timeout, return EXIT_FAILED (1) — line 354."""
        append_task({"id": "abcdef01", "command": "echo hi", "status": "pending"})
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "blocked", "exit_code": None}

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        result = run_cmd(task_id="abcdef01")
        assert result["exit_code"] == cli_commands.EXIT_FAILED


class TestStatusCmdHandler:
    """status_cmd (FR-05) — full record + validation errors."""

    def test_returns_full_record(self, taskq_home):
        append_task(
            {
                "id": "abcdef01",
                "command": "echo hi",
                "name": "foo",
                "status": "pending",
                "depends_on": [],
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
        rec = status_cmd("abcdef01")
        assert rec["id"] == "abcdef01"
        assert rec["name"] == "foo"

    def test_missing_id_raises(self, taskq_home):
        with pytest.raises(cli_commands.StatusValidationError):
            status_cmd("")

    def test_unknown_id_raises(self, taskq_home):
        with pytest.raises(cli_commands.StatusValidationError) as exc_info:
            status_cmd("deadbeef")
        assert "does not exist" in str(exc_info.value)


class TestListCmdHandler:
    """list_cmd (FR-05) — full list + status filter."""

    def test_empty_store(self, taskq_home):
        result = list_cmd()
        assert result == {"tasks": [], "count": 0}

    def test_returns_all_tasks(self, taskq_home):
        append_task({"id": "aaaaaaaa", "command": "echo a", "status": "pending"})
        append_task({"id": "bbbbbbbb", "command": "echo b", "status": "done"})
        result = list_cmd()
        assert result["count"] == 2
        assert {t["id"] for t in result["tasks"]} == {"aaaaaaaa", "bbbbbbbb"}

    def test_status_filter(self, taskq_home):
        append_task({"id": "aaaaaaaa", "command": "echo a", "status": "pending"})
        append_task({"id": "bbbbbbbb", "command": "echo b", "status": "done"})
        result = list_cmd(status_filter="pending")
        assert result["count"] == 1
        assert result["tasks"][0]["id"] == "aaaaaaaa"


class TestGraphCmdHandler:
    """graph_cmd (FR-05 / FR-06) — text and dot renderings."""

    def test_text_format_with_no_tasks(self, taskq_home):
        result = graph_cmd(format="text")
        assert result["format"] == "text"
        assert result["graph"] == ""

    def test_text_format_with_tasks(self, taskq_home):
        append_task(
            {"id": "aaaaaaaa", "command": "echo a", "status": "pending", "depends_on": []}
        )
        append_task(
            {
                "id": "bbbbbbbb",
                "command": "echo b",
                "status": "pending",
                "depends_on": ["aaaaaaaa"],
            }
        )
        result = graph_cmd(format="text")
        body = result["graph"]
        assert "aaaaaaaa <- [(root)]" in body
        assert "bbbbbbbb <- [aaaaaaaa]" in body

    def test_dot_format_with_tasks(self, taskq_home):
        append_task(
            {"id": "aaaaaaaa", "command": "echo a", "status": "pending", "depends_on": []}
        )
        append_task(
            {
                "id": "bbbbbbbb",
                "command": "echo b",
                "status": "pending",
                "depends_on": ["aaaaaaaa"],
            }
        )
        result = graph_cmd(format="dot")
        body = result["graph"]
        assert body.startswith("digraph tasks {")
        assert body.rstrip().endswith("}")
        assert '"aaaaaaaa" -> "bbbbbbbb";' in body

    def test_default_format_is_text(self, taskq_home):
        result = graph_cmd()
        assert result["format"] == "text"


class TestPluginsCmdHandler:
    """plugins_cmd (FR-05 / FR-07) — allowlist + missing-fallback."""

    def test_empty_allowlist(self, taskq_home, monkeypatch):
        monkeypatch.delenv("TASKQ_PLUGINS", raising=False)
        result = plugins_cmd("list")
        assert result["count"] == 0
        assert result["plugins"] == []

    def test_unknown_subcommand(self, taskq_home):
        with pytest.raises(cli_commands.PluginValidationError):
            plugins_cmd("not-list")

    def test_invalid_plugin_name_rejected(self, taskq_home, monkeypatch):
        """Path-form plugin name rejected by allowlist regex (FR-07)."""
        monkeypatch.setenv("TASKQ_PLUGINS", "../evil.py")
        with pytest.raises(cli_commands.PluginLoadError) as exc_info:
            plugins_cmd("list")
        assert "rejected by allowlist" in str(exc_info.value)

    def test_valid_name_missing_module_fallback(self, taskq_home, monkeypatch):
        """When a validly-named plugin can't be imported, fall back to 'missing'."""
        monkeypatch.setenv("TASKQ_PLUGINS", "definitely_not_a_real_module_xyz")
        result = plugins_cmd("list")
        assert result["count"] == 1
        assert result["plugins"][0]["name"] == "definitely_not_a_real_module_xyz"
        assert result["plugins"][0]["status"] == "missing"
        assert result["plugins"][0]["hooks"] == []

    def test_valid_allowlist_with_loaded_plugin(self, taskq_home, monkeypatch):
        """A loaded plugin reports hooks + status=loaded."""
        import sys as _sys
        import types as _types

        # Create a fake plugin module with one hook.
        fake = _types.ModuleType("taskq_plus_fake_plugin_xyz")
        fake.pre_run = lambda task: None  # noqa: E731
        _sys.modules["taskq_plus_fake_plugin_xyz"] = fake
        monkeypatch.setenv("TASKQ_PLUGINS", "taskq_plus_fake_plugin_xyz")
        try:
            result = plugins_cmd("list")
            assert result["count"] == 1
            entry = result["plugins"][0]
            assert entry["status"] == "loaded"
            assert "pre_run" in entry["hooks"]
        finally:
            _sys.modules.pop("taskq_plus_fake_plugin_xyz", None)

    def test_plugin_service_unavailable(self, taskq_home, monkeypatch):
        """When service.plugins import fails, raise PluginLoadError (line 435-437)."""
        import builtins as _builtins

        real_import = _builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "taskq_plus.service.plugins":
                raise ImportError("service unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(_builtins, "__import__", fake_import)
        monkeypatch.setenv("TASKQ_PLUGINS", "any_name")
        with pytest.raises(cli_commands.PluginLoadError) as exc_info:
            plugins_cmd("list")
        assert "plugin service unavailable" in str(exc_info.value)


class TestExportCmdHandler:
    """export_cmd (FR-05 / FR-08) — json / csv / md + validation error."""

    @pytest.fixture
    def seeded(self, taskq_home):
        append_task(
            {
                "id": "aaaaaaaa",
                "command": "echo hi",
                "name": "n1",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00Z",
                "depends_on": [],
            }
        )
        append_task(
            {
                "id": "bbbbbbbb",
                "command": "ls",
                "name": None,
                "status": "done",
                "created_at": "2026-01-02T00:00:00Z",
                "depends_on": ["aaaaaaaa"],
            }
        )
        return taskq_home

    def test_export_json(self, seeded):
        result = export_cmd(format="json")
        assert result["format"] == "json"
        assert result["content"].startswith("[")
        assert len(result["tasks"]) == 2

    def test_export_csv(self, seeded):
        result = export_cmd(format="csv")
        assert result["format"] == "csv"
        # header row first
        assert result["content"].splitlines()[0].startswith("id,command")

    def test_export_md(self, seeded):
        result = export_cmd(format="md")
        assert result["format"] == "md"
        assert "| id | command |" in result["content"]

    def test_export_unsupported_format(self, seeded):
        with pytest.raises(cli_commands.ExportValidationError):
            export_cmd(format="xml")

    def test_export_redacts_command(self, seeded):
        """NFR-04 — secret patterns redacted in the emitted payload."""
        append_task(
            {
                "id": "cccccccc",
                "command": "echo sk-abcdef12345xyz",
                "name": None,
                "status": "pending",
                "created_at": "2026-01-03T00:00:00Z",
                "depends_on": [],
            }
        )
        result = export_cmd(format="json")
        assert "sk-abcdef12345xyz" not in result["content"]
        assert "[REDACTED]" in result["content"]


class TestClearCmdHandler:
    """clear_cmd (FR-01 / FR-05) — wipe data files."""

    def test_clear_empty_home(self, taskq_home):
        result = clear_cmd()
        assert result == {"cleared": True, "removed": []}

    def test_clear_removes_present_files(self, taskq_home):
        for name in ("tasks.json", "cache.json", "breaker.json", "audit.log"):
            (taskq_home / name).write_text("{}", encoding="utf-8")
        result = clear_cmd()
        assert set(result["removed"]) == {"tasks.json", "cache.json", "breaker.json", "audit.log"}

    def test_clear_partial_present(self, taskq_home):
        (taskq_home / "tasks.json").write_text("[]", encoding="utf-8")
        result = clear_cmd()
        assert result["removed"] == ["tasks.json"]

    def test_clear_swallows_unlink_oserror(self, taskq_home, monkeypatch):
        """If unlink raises OSError, clear_cmd swallows it (line 508-510)."""
        (taskq_home / "tasks.json").write_text("[]", encoding="utf-8")
        from unittest.mock import patch

        def fake_unlink(self, *args, **kwargs):
            raise OSError("permission denied")

        with patch.object(Path, "unlink", fake_unlink):
            result = clear_cmd()
        # nothing removed because unlink was blocked
        assert result["removed"] == []
        assert result["cleared"] is True


class TestRedactHelpers:
    """_redact + _redact_task (NFR-04 helpers in commands.py)."""

    def test_redact_string_substitutes(self):
        out = cli_commands._redact("Bearer abcdef")
        assert "Bearer abcdef" not in out
        assert "[REDACTED]" in out

    def test_redact_non_string_returns_unchanged(self):
        assert cli_commands._redact(123) == 123
        assert cli_commands._redact(None) is None
        assert cli_commands._redact(["x"]) == ["x"]

    def test_redact_task_replaces_command_field(self):
        out = cli_commands._redact_task(
            {"id": "x", "command": "echo sk-abcdef12345xyz"}
        )
        assert out["command"] != "echo sk-abcdef12345xyz"
        assert "[REDACTED]" in out["command"]
        assert out["id"] == "x"

    def test_redact_task_handles_missing_command(self):
        out = cli_commands._redact_task({"id": "x"})
        assert out["command"] == ""


class TestStrictLoadTasks:
    """_strict_load_tasks (FR-05 NFR-03) — file shape variants."""

    def test_missing_file_returns_empty(self, taskq_home):
        assert cli_commands._strict_load_tasks() == []

    def test_list_shape(self, taskq_home):
        tasks_path().write_text('[{"id": "a"}]', encoding="utf-8")
        assert cli_commands._strict_load_tasks() == [{"id": "a"}]

    def test_dict_with_tasks_key(self, taskq_home):
        tasks_path().write_text('{"tasks": [{"id": "a"}]}', encoding="utf-8")
        assert cli_commands._strict_load_tasks() == [{"id": "a"}]

    def test_corrupt_json_raises(self, taskq_home):
        tasks_path().write_text("not valid json {", encoding="utf-8")
        with pytest.raises(cli_commands.StoreCorrupted):
            cli_commands._strict_load_tasks()

    def test_unexpected_shape_raises(self, taskq_home):
        tasks_path().write_text('{"foo": "bar"}', encoding="utf-8")
        with pytest.raises(cli_commands.StoreCorrupted):
            cli_commands._strict_load_tasks()


class TestResolveMaxDagDepth:
    """_resolve_max_dag_depth — env-var override + invalid fallback."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("TASKQ_MAX_DAG_DEPTH", raising=False)
        assert cli_commands._resolve_max_dag_depth() == 32

    def test_empty_string_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "")
        assert cli_commands._resolve_max_dag_depth() == 32

    def test_explicit_value(self, monkeypatch):
        monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "10")
        assert cli_commands._resolve_max_dag_depth() == 10

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "not-a-number")
        assert cli_commands._resolve_max_dag_depth() == 32


class TestTasksById:
    """_tasks_by_id — skip tasks with missing ids."""

    def test_empty_store_returns_empty(self, taskq_home):
        assert cli_commands._tasks_by_id() == {}

    def test_skips_tasks_without_id(self, taskq_home):
        tasks_path().write_text(
            '[{"command": "ls"}, {"id": "a", "command": "echo a"}]',
            encoding="utf-8",
        )
        result = cli_commands._tasks_by_id()
        assert "a" in result
        assert len(result) == 1

    def test_returns_by_id_mapping(self, taskq_home):
        append_task({"id": "aaaaaaaa", "command": "echo a", "status": "pending"})
        append_task({"id": "bbbbbbbb", "command": "echo b", "status": "pending"})
        result = cli_commands._tasks_by_id()
        assert set(result) == {"aaaaaaaa", "bbbbbbbb"}


class TestComputeDepth:
    """_compute_depth — calls into dag.chain_length."""

    def test_depth_with_no_deps(self, taskq_home):
        assert cli_commands._compute_depth([]) == 0

    def test_depth_with_present_parent(self, taskq_home):
        append_task({"id": "aaaaaaaa", "command": "echo a", "status": "pending"})
        # depth counts edges: parent→child ⇒ depth=1; chain_length=2, minus 1.
        assert cli_commands._compute_depth(["aaaaaaaa"]) == 1

    def test_depth_with_missing_parent(self, taskq_home):
        """Missing parent contributes 1 to chain_length (don't block submit)."""
        # chain_length(["deadbeef"]) = 1 (new) + 1 (missing parent as 1-node
        # chain) = 2; depth = edges = 2 - 1 = 1.
        assert cli_commands._compute_depth(["deadbeef"]) == 1


class TestLegacyRunDispatch:
    """_run (legacy argparse-based run dispatch) — exit-code branches."""

    def test_run_unknown_task_id_returns_failed(self, taskq_home, monkeypatch):
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return None

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        code = cli_commands._run(["deadbeef"])
        assert code == cli_commands.EXIT_FAILED

    def test_run_no_task_id_returns_validation_error(self, taskq_home):
        code = cli_commands._run([])
        assert code == cli_commands.EXIT_VALIDATION_ERROR

    def test_run_all_returns_ok(self, taskq_home, monkeypatch):
        from taskq_plus.service import executor as exec_mod

        monkeypatch.setattr(exec_mod, "run_all", lambda: None)
        monkeypatch.setattr(cli_commands, "exec_run_all", lambda: None)
        code = cli_commands._run(["--all"])
        assert code == cli_commands.EXIT_OK

    def test_run_int_exit_code_propagates(self, taskq_home, monkeypatch):
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "done", "exit_code": 0}

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        append_task({"id": "abcdef01", "command": "echo hi", "status": "pending"})
        code = cli_commands._run(["abcdef01"])
        assert code == 0

    def test_run_status_done_branch(self, taskq_home, monkeypatch):
        """Legacy _run status==done branch (line 610-611)."""
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "done"}  # no int exit_code

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        append_task({"id": "abcdef01", "command": "echo hi", "status": "pending"})
        code = cli_commands._run(["abcdef01"])
        assert code == 0

    def test_run_status_timeout_branch(self, taskq_home, monkeypatch):
        """Legacy _run status==timeout branch (line 612-613)."""
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "timeout"}  # no int exit_code

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        append_task({"id": "abcdef01", "command": "echo hi", "status": "pending"})
        code = cli_commands._run(["abcdef01"])
        assert code == 4

    def test_run_status_other_branch(self, taskq_home, monkeypatch):
        """Legacy _run falls through to EXIT_FAILED (line 614)."""
        from taskq_plus.service import cache as cache_mod

        def fake_execute_with_cache(task_id, use_cache=True):
            return {"status": "blocked"}

        monkeypatch.setattr(cache_mod, "execute_with_cache", fake_execute_with_cache)
        append_task({"id": "abcdef01", "command": "echo hi", "status": "pending"})
        code = cli_commands._run(["abcdef01"])
        assert code == 1


class TestAtomicWriteExtraBranches:
    """_atomic_write_json — outer FileNotFoundError re-raise branch."""

    def test_filenotfounderror_outer_branch_propagates(self, taskq_home, monkeypatch):
        """Outer except FileNotFoundError branch re-raises — line 58."""
        import tempfile as _tempfile
        from unittest.mock import patch

        target = taskq_home / "data.json"
        with patch.object(_tempfile, "mkstemp", side_effect=FileNotFoundError("nope")):
            with pytest.raises(FileNotFoundError):
                _atomic_write_json(target, {"k": "v"})
        # no target written
        assert not target.exists()
