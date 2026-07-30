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
def test_fr01_a(taskq_home):
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
def test_fr01_b(taskq_home):
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
def test_fr01_c(taskq_home):
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
def test_fr01_d(taskq_home, command, char_name):
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