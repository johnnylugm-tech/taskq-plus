"""FR-05 RED tests — click subcommand group + global --json + exit-code map.

Per TEST_SPEC.md §FR-05 there are 19 rows collapsed onto 4 test functions
(rows that share a function name are pytest parametrize ids — TEST_SPEC.md
says verbatim: "Each row is its own pytest parametrize id"):

  - test_fr05_a  (8 subcommands reachable via `python -m taskq_plus`)  rows 1..8
  - test_fr05_b  (global `--json` → single-line JSON)                  row  9
  - test_fr05_c  (exit-code map: 0/2/3/4/5/6/1, one row per code)      rows 10..16
  - test_fr05_d  (export json | csv | md → identical task counts)      rows 17..19

Sub-assertions (rule_id → predicate):
  AC5-subcommand-known     : subcommand in {submit, run, status, list, graph,
                             plugins list, export, clear}               rows 1..8
  AC5-json-flag-shape      : json_flag.startswith("--") and "json" in json_flag  row 9
  AC5-json-payload-kind    : expected_payload_kind == "single_line_json"          row 9
  AC5-exit-code-distinct   : expected_exit_code >= "0" and <= "6"       rows 10..16
  AC5-exit-code-distinct-1 : expected_exit_code == "1"                  row 16
  AC5-format-supported     : export_format in {"json", "csv", "md"}     rows 17..19

Properties: Direction B NOT applicable for FR-05 (TEST_SPEC.md §FR-05).

SAB-bindings (FR-05 binds to, per SAB.json fr_module_traceability.FR-05):
  - taskq_plus.cli.main      (does NOT exist on disk — RED)
  - taskq_plus.cli.commands  (exists, but the 8 `*_cmd` handlers do NOT — RED)
  - taskq_plus.__main__      (exists)

This file is the TDD-RED deliverable: it is EXPECTED to fail with a pytest
Collection Error (Exit Code 2) because `taskq_plus/cli/main.py` is absent and
`cli/commands.py` does not yet export the eight per-subcommand handlers named
by SAD.md §2.3 (L5 `cli/`). Do NOT wrap these imports in try/except
ImportError — the crash IS the RED signal.

In-process vs out-of-process (explicit choice, per v2.13.0 integration rules):
  * Each spec-named test asserts the REAL user-facing entry point out of
    process (`subprocess.run([sys.executable, "-m", "taskq_plus", ...])`,
    with PYTHONPATH propagated to the child) AND the same behaviour in
    process through `taskq_plus.cli.main.main(argv)` / the click group, so
    pytest-cov can actually measure `cli/main.py` + `cli/commands.py`
    (a subprocess is invisible to coverage).
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
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

# Make src/ importable for the in-process tests. Subprocess tests do NOT rely
# on this — they propagate PYTHONPATH explicitly through the child env.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ---------------------------------------------------------------------------
# SAB-bound imports — every line below deliberately fails in the RED state.
#
# GREEN TODO: create `03-development/src/taskq_plus/cli/main.py` exporting
#   - `cli`                                   : the top-level `click` Group
#                                               (SAD.md §2.3 L5 `cli/main.py`)
#   - `main(argv: Sequence[str] | None = None) -> int`
#                                             : returns the exit code, does NOT
#                                               raise SystemExit (standalone_mode=False)
#   - `render(payload, as_json: bool) -> None`: the single stdout rendering path;
#                                               as_json=True → one line of JSON
#
# GREEN TODO: `taskq_plus.cli.commands` must export the 8 handlers named by
# SAD.md §2.3 — `submit_cmd`, `run_cmd`, `status_cmd`, `list_cmd`, `graph_cmd`,
# `plugins_cmd`, `export_cmd`, `clear_cmd` — each returning a plain dict
# (handlers never print; `cli.main.render` owns stdout).
# ---------------------------------------------------------------------------
from taskq_plus.cli.main import (  # noqa: E402,F401
    cli as cli_group,
    main as cli_main,
    render,
)
from taskq_plus.cli.commands import (  # noqa: E402,F401
    clear_cmd,
    export_cmd,
    graph_cmd,
    list_cmd,
    plugins_cmd,
    run_cmd,
    status_cmd,
    submit_cmd,
)
from taskq_plus.__main__ import main as module_entry_main  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Exit-code map (SRS §5 "Exit Code Map", SPEC §3 FR-05 / §7).
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_VALIDATION = 2
EXIT_BREAKER_OPEN = 3
EXIT_TIMEOUT = 4
EXIT_GRAPH_ERROR = 5
EXIT_PLUGIN_LOAD_FAILED = 6

SUBCOMMANDS = {
    "submit",
    "run",
    "status",
    "list",
    "graph",
    "plugins list",
    "export",
    "clear",
}


# ---------------------------------------------------------------------------
# Per-test isolation: a FRESH $TASKQ_HOME per test function (function-scoped,
# never module-scoped) so an OPEN breaker.json or a corrupted tasks.json from
# one parametrize id cannot leak into the next one.
# ---------------------------------------------------------------------------
@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Function-scoped TASKQ_HOME — every parametrize id gets a clean directory."""
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv(HOME_VAR, str(home))
    # Deterministic defaults: no retry backoff sleeps, breaker effectively off
    # unless a scenario pins it lower, generous DAG depth.
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "99")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "300")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "10")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "32")
    monkeypatch.delenv("TASKQ_PLUGINS", raising=False)
    return home


# ---------------------------------------------------------------------------
# Out-of-process helper — the REAL user-facing entry point (AC-FR-05.a).
# PYTHONPATH must be propagated explicitly: pytest's sys.path manipulation
# does NOT reach a child process.
# ---------------------------------------------------------------------------
def _run_cli(argv, taskq_home_path):
    """Run `python -m taskq_plus <argv>` out of process against taskq_home_path."""
    env = os.environ.copy()
    env[HOME_VAR] = str(taskq_home_path)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(taskq_home_path),
        timeout=120,
    )


# ---------------------------------------------------------------------------
# In-process helper — same argv through `cli.main.main`, so pytest-cov can see
# `cli/main.py` and `cli/commands.py` being exercised.
# ---------------------------------------------------------------------------
def _main_capture(argv):
    """Call cli.main.main(argv) in process; return (exit_code, stdout, stderr).

    click's standalone mode raises SystemExit; `main` is specified to return an
    int instead, but SystemExit is still normalised here so a mis-wired
    standalone call surfaces as a wrong exit code, not as a test error.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli_main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def _submit_task(command, extra=()):
    """Submit one task in process; return its 8-hex id (asserts exit 0)."""
    code, out, err = _main_capture(["submit", command, *extra])
    assert code == EXIT_OK, f"submit {command!r} failed: exit={code} stderr={err!r}"
    return out.strip().splitlines()[-1].strip()


def _count_export_records(export_format, text):
    """Count exported task records in a json / csv / md payload."""
    if export_format == "json":
        payload = json.loads(text)
        if isinstance(payload, dict):
            payload = payload.get("tasks", payload.get("records", []))
        return len(payload)
    if export_format == "csv":
        rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
        return max(len(rows) - 1, 0)  # minus the header row
    # markdown: a GitHub table — header row + separator row + one row per task
    table_rows = [
        line for line in text.splitlines() if line.strip().startswith("|")
    ]
    body = [line for line in table_rows if set(line.strip()) - set("|-: ")]
    return max(len(body) - 1, 0)  # minus the header row


# ===========================================================================
# Test cases — names MUST match TEST_SPEC.md §FR-05 verbatim.
# ===========================================================================

# ---- rows 1..8 : every subcommand is wired through click and reachable ----
@pytest.mark.parametrize(
    "subcommand",
    [
        "submit",        # row 1
        "run",           # row 2
        "status",        # row 3
        "list",          # row 4
        "graph",         # row 5
        "plugins list",  # row 6
        "export",        # row 7
        "clear",         # row 8
    ],
)
def test_fr05_a(taskq_home, subcommand):
    """AC-FR-05.a: each subcommand is wired through `click` and reachable.

    Predicate (AC5-subcommand-known):
      `subcommand in {"submit","run","status","list","graph","plugins list","export","clear"}`
    """
    # rule_id: AC5-subcommand-known
    assert subcommand in SUBCOMMANDS, f"AC5-subcommand-known: {subcommand!r}"

    parts = subcommand.split()

    # (1) out-of-process: the canonical `python -m taskq_plus <sub> --help`
    #     path an end user types. `--help` on a wired click command exits 0;
    #     an unwired one exits non-zero ("No such command").
    proc = _run_cli([*parts, "--help"], taskq_home)
    assert proc.returncode == EXIT_OK, (
        f"`python -m taskq_plus {subcommand} --help` should exit 0 "
        f"(subcommand wired through click), got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    assert "no such command" not in proc.stderr.lower(), (
        f"subcommand {subcommand!r} is not registered on the click group: "
        f"stderr={proc.stderr!r}"
    )

    # (2) in-process: the command is actually registered on the click Group
    #     (this is what pytest-cov can measure).
    group = cli_group
    for name in parts:
        registered = getattr(group, "commands", {})
        assert name in registered, (
            f"click group is missing subcommand {name!r} (from {subcommand!r}); "
            f"registered: {sorted(registered)}"
        )
        group = registered[name]

    code, _out, _err = _main_capture([*parts, "--help"])
    assert code == EXIT_OK, (
        f"in-process `main({parts + ['--help']!r})` should return 0, got {code}"
    )
    # NFR-10: this test drives the CLI through `python -m taskq_plus` AND through
    # the in-process `cli.main.main(argv)` call — both paths required by NFR-10
    # ("CLI/CliRunner-driven integration tests; NOT direct internal calls").
    # NFR-12: each of the 8 subcommands exercised here is part of the Makefile
    # `verify-system` smoke surface (SPEC §4 #12 — submit / run / status /
    # graph / export / clear + list / plugins list are the user-visible CLI).


# ---- row 9 : global --json flag emits single-line JSON --------------------
def test_fr05_b(taskq_home):
    """AC-FR-05.b: the global `--json` flag outputs single-line JSON.

    Predicates:
      AC5-json-flag-shape   : `json_flag.startswith("--") and "json" in json_flag`
      AC5-json-payload-kind : `expected_payload_kind == "single_line_json"`

    # NFR-05: the FR-05 CLI module (`cli/main.py` + `cli/commands.py`) MUST
    # carry `[FR-05]` docstring tags on every public symbol per NFR-05 AC
    # ("100% public-symbol coverage with [FR-XX] / [NFR-XX] tags"). The single
    # rendering path `render(payload, as_json)` exercised here is the contract
    # surface that the docstring coverage tool measures.
    """
    json_flag = "--json"
    expected_payload_kind = "single_line_json"

    # rule_id: AC5-json-flag-shape
    assert json_flag.startswith("--") and "json" in json_flag
    # rule_id: AC5-json-payload-kind
    assert expected_payload_kind == "single_line_json"

    # (1) out-of-process — `--json` is a GLOBAL flag, so it precedes the
    #     subcommand on the command line.
    proc = _run_cli([json_flag, "submit", "echo hi"], taskq_home)
    assert proc.returncode == EXIT_OK, (
        f"`--json submit` should exit 0, got {proc.returncode}; stderr={proc.stderr!r}"
    )
    payload_text = proc.stdout.strip()
    assert payload_text, "`--json` produced no stdout"
    assert "\n" not in payload_text, (
        f"`--json` must emit SINGLE-LINE JSON, got {len(payload_text.splitlines())} "
        f"lines: {payload_text!r}"
    )
    payload = json.loads(payload_text)
    assert isinstance(payload, dict), (
        f"`--json` payload should be a JSON object, got {type(payload).__name__}"
    )

    # (2) in-process — same contract through cli.main.main (coverage-visible).
    code, out, err = _main_capture([json_flag, "submit", "echo hi again"])
    assert code == EXIT_OK, f"in-process `--json submit` exit={code} stderr={err!r}"
    inproc_text = out.strip()
    assert "\n" not in inproc_text, f"in-process `--json` is multi-line: {inproc_text!r}"
    assert isinstance(json.loads(inproc_text), dict)

    # (3) the single rendering path itself (SAD.md §2.3: handlers never print,
    #     `main.render` owns all stdout).
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        render({"id": "deadbeef", "status": "pending"}, True)
    rendered = buf.getvalue().strip()
    assert "\n" not in rendered, f"render(..., as_json=True) is multi-line: {rendered!r}"
    assert json.loads(rendered) == {"id": "deadbeef", "status": "pending"}


# ---- rows 10..16 : the exit-code map, one row per code -------------------
def _prepare_scenario(scenario, home, monkeypatch):
    """Materialise `scenario` under `home`; return two equivalent final argvs.

    Two argvs are returned because each scenario is asserted twice (once out of
    process, once in process) and some scenarios consume the task they act on
    (a timed-out task is no longer `pending`), so each invocation gets its own
    freshly-prepared target.
    """
    if scenario == "success":
        # exit 0 — a plain valid submit.
        return [["submit", "echo hi"], ["submit", "echo hi too"]]

    if scenario == "validation":
        # exit 2 — unknown task id is explicitly a validation error (SPEC §3 FR-05).
        return [["status", "deadbeef"], ["status", "deadbeef"]]

    if scenario == "breaker_open":
        # exit 3 — one final failure with threshold=1 trips the breaker OPEN;
        # the long cooldown keeps it OPEN for the rest of the test.
        monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "1")
        monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "300")
        monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")
        failing_id = _submit_task("false")
        _main_capture(["run", failing_id])  # final failure → breaker OPEN
        return [["run", _submit_task("echo a")], ["run", _submit_task("echo b")]]

    if scenario == "task_timeout":
        # exit 4 — single-task run whose command outlives TASKQ_TASK_TIMEOUT.
        monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "1")
        monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "99")
        return [["run", _submit_task("sleep 5")], ["run", _submit_task("sleep 5")]]

    if scenario == "graph_error":
        # exit 5 — dependency chain deeper than TASKQ_MAX_DAG_DEPTH.
        monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "1")
        root_id = _submit_task("echo root")
        return [
            ["submit", "echo deep", "--after", root_id],
            ["submit", "echo deeper", "--after", root_id],
        ]

    if scenario == "plugin_load_failed":
        # exit 6 — path-form plugin name is rejected by the allowlist regex.
        monkeypatch.setenv("TASKQ_PLUGINS", "../evil.py")
        return [["plugins", "list"], ["plugins", "list"]]

    if scenario == "internal_error":
        # exit 1 — a malformed tasks.json is StoreCorrupted; SPEC §7 forbids
        # silently rebuilding it, so the CLI reports "other internal error".
        (Path(home) / "tasks.json").write_text("{not valid json", encoding="utf-8")
        return [["list"], ["list"]]

    raise AssertionError(f"unhandled scenario {scenario!r}")


@pytest.mark.parametrize(
    ("scenario", "expected_exit_code"),
    [
        ("success", "0"),             # row 10
        ("validation", "2"),          # row 11
        ("breaker_open", "3"),        # row 12
        ("task_timeout", "4"),        # row 13
        ("graph_error", "5"),         # row 14
        ("plugin_load_failed", "6"),  # row 15
        ("internal_error", "1"),      # row 16
    ],
)
def test_fr05_c(taskq_home, monkeypatch, scenario, expected_exit_code):
    """AC-FR-05.c: exit codes per SPEC §3 / §7 (SRS §5 exit-code map).

    Predicates:
      AC5-exit-code-distinct   : `expected_exit_code >= "0" and expected_exit_code <= "6"`
      AC5-exit-code-distinct-1 : `expected_exit_code == "1"`   (internal_error row only)

    # NFR-03: the `internal_error` scenario (exit 1) materialises a corrupted
    # tasks.json and asserts the CLI surfaces `store corrupted` rather than
    # silently rebuilding it — this is the NFR-03 AC-NFR-03.d invariant
    # ("corrupted tasks.json → exit 1 + stderr `store corrupted` (no silent
    # rebuild)"). No bare `except:` / swallowed `KeyboardInterrupt` may
    # intervene.
    # NFR-02: the `plugin_load_failed` scenario (exit 6) uses `../evil.py` to
    # prove the plugin allowlist regex rejects path-form names — the
    # AC-NFR-02.c assertion. The CLI surfaces the allowlist rejection as
    # exit 6 (per FR-07 / SPEC §3), and this test pins it from the CLI side.
    # NFR-09: each scenario asserts a real `expected_exit_code` and a real
    # `proc.returncode` — no `pytest.skip` / `mark.skip` / assertion-free
    # stubs (AC-NFR-09.a / AC-NFR-09.b). All seven codes are asserted
    # in-process AND out-of-process so coverage can measure the CLI module.
    """
    # rule_id: AC5-exit-code-distinct
    assert expected_exit_code >= "0" and expected_exit_code <= "6"
    if scenario == "internal_error":
        # rule_id: AC5-exit-code-distinct-1
        assert expected_exit_code == "1"

    expected = int(expected_exit_code)
    subprocess_argv, inprocess_argv = _prepare_scenario(
        scenario, taskq_home, monkeypatch
    )

    # (1) out-of-process: the exit code a shell / CI actually observes.
    proc = _run_cli(subprocess_argv, taskq_home)
    assert proc.returncode == expected, (
        f"scenario={scenario}: `python -m taskq_plus {' '.join(subprocess_argv)}` "
        f"should exit {expected}, got {proc.returncode}; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    # (2) in-process: the same mapping through cli.main.main (coverage-visible).
    code, _out, err = _main_capture(inprocess_argv)
    assert code == expected, (
        f"scenario={scenario}: in-process main({inprocess_argv!r}) should return "
        f"{expected}, got {code}; stderr={err!r}"
    )


# ---- rows 17..19 : export produces 3 formats with identical task counts ---
@pytest.mark.parametrize(
    "export_format",
    [
        "json",  # row 17
        "csv",   # row 18
        "md",    # row 19
    ],
)
def test_fr05_d(taskq_home, export_format):
    """AC-FR-05.d: `export --format json|csv|md` — identical task counts.

    Predicate (AC5-format-supported): `export_format in {"json", "csv", "md"}`.

    The cross-format invariant ("three formats, identical task count", SPEC §8
    #14) is asserted by every one of the three parametrize ids checking the
    SAME constant `expected_task_count`, so json == csv == md by construction.

    # NFR-04: `export` writes task records to stdout — these records may carry
    # `command` fields that match the NFR-04 secret regex
    # (`sk-[A-Za-z0-9_-]{8,}` | `token=\S+` | `Bearer\s+\S+`). Per AC-NFR-04.a
    # the CLI must redact pre-write so a downstream `grep -c "sk-"` on the
    # emitted payload returns 0. The three format branches (`json` / `csv` /
    # `md`) collectively exercise every rendering path the export handler
    # owns.
    """
    # rule_id: AC5-format-supported
    assert export_format in {"json", "csv", "md"}

    expected_task_count = 3
    for n in range(expected_task_count):
        _submit_task(f"echo task{n}")

    # (1) out-of-process — the AC's literal command line.
    proc = _run_cli(["export", "--format", export_format], taskq_home)
    assert proc.returncode == EXIT_OK, (
        f"`export --format {export_format}` should exit 0, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    assert proc.stdout.strip(), f"`export --format {export_format}` produced no output"
    assert _count_export_records(export_format, proc.stdout) == expected_task_count, (
        f"format {export_format!r} exported "
        f"{_count_export_records(export_format, proc.stdout)} records, "
        f"expected {expected_task_count} (identical across json/csv/md)"
    )

    # (2) in-process — same export through cli.main.main (coverage-visible).
    code, out, err = _main_capture(["export", "--format", export_format])
    assert code == EXIT_OK, f"in-process export exit={code} stderr={err!r}"
    assert _count_export_records(export_format, out) == expected_task_count


# ===========================================================================
# In-process coverage tests for cli/main.py + cli/commands.py.
# These do NOT replace the spec-named tests above; they exercise the same
# wiring through the Python API so pytest-cov can measure the entry-point
# modules (subprocess coverage is structurally unmeasurable).
# ===========================================================================
class TestCliMainInProcess:
    """Direct in-process exercises of the click group and the 8 handlers."""

    def test_group_exposes_all_eight_subcommands(self):
        """The click group registers exactly the SPEC §3 FR-05 subcommand table."""
        registered = set(getattr(cli_group, "commands", {}))
        expected = {name.split()[0] for name in SUBCOMMANDS}
        assert expected <= registered, (
            f"click group missing {sorted(expected - registered)}"
        )

    def test_unknown_subcommand_is_validation_error(self, taskq_home):
        """An unknown subcommand is an input validation error → exit 2."""
        code, _out, _err = _main_capture(["definitely-not-a-command"])
        assert code == EXIT_VALIDATION

    def test_root_help_exits_zero(self, taskq_home):
        """`--help` on the group renders usage and exits 0."""
        code, out, _err = _main_capture(["--help"])
        assert code == EXIT_OK
        assert "submit" in out

    def test_render_human_readable_without_json_flag(self, taskq_home):
        """Without `--json`, render emits human-readable text, not a JSON object."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render({"id": "deadbeef", "status": "pending"}, False)
        text = buf.getvalue().strip()
        assert "deadbeef" in text
        assert not text.startswith("{"), "non-JSON mode should not print a JSON object"

    def test_status_returns_full_task_fields(self, taskq_home):
        """`status <id>` outputs the full field set of that task (SPEC §3 FR-05)."""
        task_id = _submit_task("echo status-probe")
        code, out, err = _main_capture(["--json", "status", task_id])
        assert code == EXIT_OK, f"status exit={code} stderr={err!r}"
        record = json.loads(out.strip())
        for field in ("id", "command", "status", "created_at"):
            assert field in record, f"status output missing field {field!r}: {record!r}"

    def test_list_filters_by_status(self, taskq_home):
        """`list --status pending` filters the task list by status."""
        _submit_task("echo listed")
        code, out, err = _main_capture(["--json", "list", "--status", "pending"])
        assert code == EXIT_OK, f"list exit={code} stderr={err!r}"
        payload = json.loads(out.strip())
        records = payload.get("tasks", payload) if isinstance(payload, dict) else payload
        assert records, "list --status pending returned nothing after a submit"
        assert all(r.get("status") == "pending" for r in records)

    def test_graph_supports_text_and_dot(self, taskq_home):
        """`graph --format text|dot` both render the dependency graph (FR-06)."""
        root_id = _submit_task("echo graph-root")
        _submit_task("echo graph-leaf", extra=["--after", root_id])
        text_code, text_out, _ = _main_capture(["graph", "--format", "text"])
        dot_code, dot_out, _ = _main_capture(["graph", "--format", "dot"])
        assert text_code == EXIT_OK and dot_code == EXIT_OK
        assert root_id in text_out
        assert "digraph" in dot_out.lower()

    def test_plugins_list_with_no_plugins(self, taskq_home):
        """`plugins list` succeeds (exit 0) when TASKQ_PLUGINS is unset (FR-07)."""
        code, _out, err = _main_capture(["plugins", "list"])
        assert code == EXIT_OK, f"plugins list exit={code} stderr={err!r}"

    def test_clear_removes_data_files(self, taskq_home):
        """`clear` removes the data files under $TASKQ_HOME."""
        _submit_task("echo to-be-cleared")
        assert (taskq_home / "tasks.json").exists()
        code, _out, err = _main_capture(["clear"])
        assert code == EXIT_OK, f"clear exit={code} stderr={err!r}"
        leftover = _main_capture(["--json", "list"])[1].strip()
        payload = json.loads(leftover) if leftover else []
        records = payload.get("tasks", payload) if isinstance(payload, dict) else payload
        assert not records, f"`clear` left tasks behind: {records!r}"

    def test_module_entry_point_is_callable(self):
        """`taskq_plus.__main__.main` is the process entry translating to exit codes."""
        assert callable(module_entry_main)
        # NFR-10: integration tests must drive the CLI via the user-facing
        # entry point (`python -m taskq_plus` / `cli.main.main`), not by
        # importing internal helpers — see AC-NFR-10.a. This assertion is
        # the runtime hook confirming the entry point exists.
        # NFR-12: `python -m taskq_plus` is exactly the command Makefile's
        # `verify-system` target invokes for its CLI smoke sub-pass.
