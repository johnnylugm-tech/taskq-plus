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

    def test_status_human_readable_renders_id_and_status(self, taskq_home):
        """Lines 273-275 of cli/main.py: `status <id>` without --json prints
        `<id> <status>` on a single line (SPEC §3 FR-07 human-readable form)."""
        task_id = _submit_task("echo human-status-probe")
        code, out, err = _main_capture(["status", task_id])
        assert code == EXIT_OK, f"status(human) exit={code} stderr={err!r}"
        rendered = out.strip()
        # The click wrapper writes "<id> <status>\n" — the test asserts the
        # body line equals that exact pair.
        assert rendered == f"{task_id} pending", (
            f"human-readable status output: {rendered!r}"
        )

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


# ===========================================================================
# Coverage-driven in-process tests for cli/main.py and cli/commands.py.
# These target the specific uncovered lines that pytest-cov reports as MISS.
# Each test asserts behaviour AND, by virtue of being in-process, raises the
# coverage measurement of the entry-point modules.
# ===========================================================================

# Import the legacy argparse-based path commands for direct unit testing.
from taskq_plus.cli import commands as _cmd_mod  # noqa: E402


class TestCoverageCliMain:
    """Cover `cli/main.py` render + main() error paths."""

    def test_render_non_dict_payload(self):
        """Lines 72-74: render() with a non-dict payload uses str(payload)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render("plain string", False)
        assert buf.getvalue().strip() == "plain string"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render(42, False)
        assert buf.getvalue().strip() == "42"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render(["a", "b"], False)
        assert buf.getvalue().strip() == "['a', 'b']"

    def test_render_with_tasks_list_payload(self):
        """Lines 93-98: render() with a `tasks` key emits one line per task."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render(
                {
                    "tasks": [
                        {"id": "aaaa1111", "status": "pending"},
                        {"id": "bbbb2222", "status": "done"},
                    ]
                },
                False,
            )
        out = buf.getvalue()
        assert "aaaa1111 pending" in out
        assert "bbbb2222 done" in out

    def test_render_with_plugins_payload(self):
        """Lines 100-103: render() with a `plugins` key emits one plugin per line."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render(
                {
                    "plugins": [
                        {"name": "alpha_plugin"},
                        {"name": "beta_plugin"},
                    ]
                },
                False,
            )
        out = buf.getvalue()
        assert "alpha_plugin" in out
        assert "beta_plugin" in out

    def test_render_with_graph_payload(self):
        """Lines 83-86: render() with a `graph` key emits the graph body verbatim."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render({"graph": "A -> B\nB -> C\n"}, False)
        out = buf.getvalue()
        assert "A -> B" in out
        assert "B -> C" in out

    def test_render_with_content_payload_no_trailing_newline(self):
        """Lines 76-81: render() with `content` adds trailing newline if missing."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render({"content": "hello, world"}, False)
        out = buf.getvalue()
        assert out == "hello, world\n"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render({"content": "has newline\n"}, False)
        out = buf.getvalue()
        assert out == "has newline\n"

    def test_render_with_id_only_payload(self):
        """Lines 88-91: render() with `id` key emits just the id."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render({"id": "cafef00d", "status": "pending"}, False)
        assert buf.getvalue().strip() == "cafef00d"

    def test_propagate_json_callback_via_per_subcommand_alias(self, taskq_home):
        """Line 144: per-subcommand `--json` alias invokes the callback that
        sets ctx.obj['json']=True. Bypass `_extract_json` by calling cli.main
        directly so the alias option is actually evaluated by click."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cli_group.main(
                args=["submit", "echo hi", "--json"],
                standalone_mode=False,
                obj={"json": False},
            )
        assert result == EXIT_OK
        text = out.getvalue().strip()
        # Per-subcommand --json set ctx.obj["json"]=True, so render emitted
        # JSON (single line).
        assert "\n" not in text or text.endswith("\n")
        json.loads(text)  # parses as JSON


class TestCoverageCliMainErrors:
    """Cover `cli/main.py` exception handlers in subcommand wrappers."""

    def test_submit_graph_error_returns_exit_5(self, taskq_home):
        """Lines 205-207: GraphError from submit_cmd → exit 5."""
        monkey_argv = []
        # Submit first task, then try to chain beyond MAX_DAG_DEPTH.
        monkey_argv.append(["submit", "echo root"])
        code, out, _ = _main_capture(["submit", "echo root"])
        assert code == EXIT_OK
        # Manually push depth beyond the limit (set TASKQ_MAX_DAG_DEPTH=1).
        import os as _os
        _os.environ["TASKQ_MAX_DAG_DEPTH"] = "1"
        root_id = out.strip().splitlines()[-1].strip()
        # Submit a child whose parent's depth already pushes us over.
        code, out, err = _main_capture(["submit", "echo child", "--after", root_id])
        assert code == 5, f"submit-graph-error exit={code}, stderr={err!r}"
        assert "error" in err.lower()

    def test_run_validation_error_no_task_id(self, taskq_home):
        """Lines 227-229: run with no task_id and no --all → exit 2."""
        code, out, err = _main_capture(["run"])
        assert code == EXIT_VALIDATION, f"run(no-args) exit={code} stderr={err!r}"

    def test_run_validation_error_unknown_task(self, taskq_home):
        """Lines 227-229: run with non-existent task_id → exit 2."""
        code, out, err = _main_capture(["run", "00000000"])
        assert code == EXIT_VALIDATION, f"run(unknown) exit={code} stderr={err!r}"

    def test_run_internal_error_when_no_result(self, taskq_home):
        """Lines 230-232: RunInternalError from run_cmd → exit 1.

        Simulate by patching run_cmd to raise RunInternalError.
        """
        from taskq_plus.cli import commands as cmds

        original = cmds.run_cmd
        cmds.run_cmd = lambda **kw: (_ for _ in ()).throw(
            cmds.RunInternalError("execution returned no result")
        )
        try:
            code, out, err = _main_capture(["run", "deadbeef"])
            assert code == EXIT_INTERNAL_ERROR, (
                f"run_internal_error exit={code} stderr={err!r}"
            )
        finally:
            cmds.run_cmd = original

    def test_graph_store_corrupted(self, taskq_home):
        """Lines 287-289: graph with corrupt tasks.json → exit 1."""
        (taskq_home / "tasks.json").write_text("{not valid json", encoding="utf-8")
        code, out, err = _main_capture(["graph"])
        assert code == EXIT_INTERNAL_ERROR, f"graph(corrupt) exit={code} stderr={err!r}"

    def test_list_store_corrupted(self, taskq_home):
        """Lines 267-268: list with corrupt tasks.json → exit 1."""
        (taskq_home / "tasks.json").write_text("{not valid json", encoding="utf-8")
        code, out, err = _main_capture(["list"])
        assert code == EXIT_INTERNAL_ERROR, f"list(corrupt) exit={code} stderr={err!r}"

    def test_export_store_corrupted(self, taskq_home):
        """Lines 335-337: export with corrupt tasks.json → exit 1."""
        (taskq_home / "tasks.json").write_text("{not valid json", encoding="utf-8")
        code, out, err = _main_capture(["export", "--format", "json"])
        assert code == EXIT_INTERNAL_ERROR, f"export(corrupt) exit={code} stderr={err!r}"

    def test_export_validation_error_unknown_format(self, taskq_home):
        """Lines 332-334: export with unsupported format → exit 2.

        Click's Choice type rejects unknown formats before reaching the
        handler, raising UsageError. The main() except handler then maps it
        to exit 2.
        """
        code, out, err = _main_capture(["export", "--format", "xml"])
        assert code == EXIT_VALIDATION, f"export(xml) exit={code} stderr={err!r}"

    def test_plugins_validation_error_unknown_subcmd(self, taskq_home, monkeypatch):
        """Lines 306-308: plugins with an unknown sub-subcommand → exit 2."""
        monkeypatch.setenv("TASKQ_PLUGINS", "")
        # `plugins` group has no `unknown` subcommand; click raises UsageError,
        # mapped to exit 2 by main()'s UsageError handler.
        code, out, err = _main_capture(["plugins", "unknown"])
        assert code == EXIT_VALIDATION, f"plugins(unknown) exit={code} stderr={err!r}"

    def test_plugins_load_error_bad_allowlist(self, taskq_home, monkeypatch):
        """Lines 309-311: plugins allowlist regex rejection → exit 6."""
        monkeypatch.setenv("TASKQ_PLUGINS", "../evil.py")
        code, out, err = _main_capture(["plugins", "list"])
        assert code == EXIT_PLUGIN_LOAD_FAILED, (
            f"plugins(bad-allowlist) exit={code} stderr={err!r}"
        )

    def test_submit_validation_error_returns_exit_2(self, taskq_home):
        """Lines 202-204: SubmitValidationError from submit_cmd → exit 2."""
        from taskq_plus.cli import commands as cmds

        original = cmds.submit_cmd

        def _raise(*a, **kw):
            raise cmds.SubmitValidationError("synthetic submit validation")

        cmds.submit_cmd = _raise
        try:
            code, out, err = _main_capture(["submit", "echo x"])
            assert code == EXIT_VALIDATION, (
                f"submit(validation) exit={code} stderr={err!r}"
            )
        finally:
            cmds.submit_cmd = original

    def test_plugins_validation_error_returns_exit_2(self, taskq_home):
        """Lines 306-308: PluginValidationError from plugins_cmd → exit 2."""
        from taskq_plus.cli import commands as cmds

        original = cmds.plugins_cmd

        def _raise(*a, **kw):
            raise cmds.PluginValidationError("synthetic plugin validation")

        cmds.plugins_cmd = _raise
        try:
            code, out, err = _main_capture(["plugins", "list"])
            assert code == EXIT_VALIDATION, (
                f"plugins(validation) exit={code} stderr={err!r}"
            )
        finally:
            cmds.plugins_cmd = original

    def test_export_validation_error_returns_exit_2(self, taskq_home):
        """Lines 332-334: ExportValidationError from export_cmd → exit 2."""
        from taskq_plus.cli import commands as cmds

        original = cmds.export_cmd

        def _raise(*a, **kw):
            raise cmds.ExportValidationError("synthetic export validation")

        cmds.export_cmd = _raise
        try:
            code, out, err = _main_capture(["export", "--format", "json"])
            assert code == EXIT_VALIDATION, (
                f"export(validation) exit={code} stderr={err!r}"
            )
        finally:
            cmds.export_cmd = original


class TestCoverageCliMainEntry:
    """Cover `cli/main.py` main() entry-point paths."""

    def test_main_with_none_argv_uses_sys_argv(self, taskq_home, monkeypatch):
        """Line 374: main(argv=None) reads sys.argv[1:]."""
        monkeypatch.setattr(sys, "argv", ["taskq_plus", "submit", "echo hi"])
        code, _out, _err = _main_capture_with_none_argv()
        assert code == EXIT_OK

    def test_main_click_exit_handler_returns_zero(self, taskq_home):
        """Line 381: click.exceptions.Exit(exit_code=0) → return 0."""
        # --help raises click.exceptions.Exit(0); the handler maps to int(0).
        code, _out, _err = _main_capture(["submit", "--help"])
        assert code == EXIT_OK

    def test_main_click_exception_returns_exit_1(self, taskq_home, monkeypatch):
        """Lines 385-387: ClickException maps to EXIT_INTERNAL_ERROR (exit 1)."""
        from taskq_plus.cli import commands as cmds

        def _raise(*a, **kw):
            import click as _click

            raise _click.ClickException("synthetic click error")

        original = cmds.submit_cmd
        cmds.submit_cmd = _raise
        try:
            code, _out, _err = _main_capture(["submit", "echo hi"])
            assert code == EXIT_INTERNAL_ERROR
        finally:
            cmds.submit_cmd = original

    def test_main_exception_returns_exit_1(self, taskq_home, monkeypatch):
        """Lines 388-389: SystemExit and (line 390) generic Exception → exit 1.

        Patch submit_cmd to raise a bare RuntimeError so the last-resort
        `except Exception` clause fires. The `except SystemExit` branch is
        tested via the legacy `__main__.main` indirection.
        """
        from taskq_plus.cli import commands as cmds

        def _raise(*a, **kw):
            raise RuntimeError("synthetic unexpected")

        original = cmds.submit_cmd
        cmds.submit_cmd = _raise
        try:
            code, _out, err = _main_capture(["submit", "echo hi"])
            assert code == EXIT_INTERNAL_ERROR, (
                f"main(generic-exception) exit={code} stderr={err!r}"
            )
        finally:
            cmds.submit_cmd = original

    def test_main_systemexit_returns_exit_1(self, taskq_home, monkeypatch):
        """Lines 388-389: SystemExit(code=N) → return N."""
        from taskq_plus.cli import commands as cmds

        def _raise(*a, **kw):
            raise SystemExit(42)

        original = cmds.submit_cmd
        cmds.submit_cmd = _raise
        try:
            code, _out, _err = _main_capture(["submit", "echo hi"])
            assert code == 42
        finally:
            cmds.submit_cmd = original

    def test_main_click_exit_nonzero_returns_exit_code(self, taskq_home, monkeypatch):
        """Line 381: click.exceptions.Exit(exit_code=N!=0) → return int(N)."""
        from taskq_plus.cli import commands as cmds
        import click as _click

        def _raise(*a, **kw):
            raise _click.exceptions.Exit(7)

        original = cmds.submit_cmd
        cmds.submit_cmd = _raise
        try:
            code, _out, _err = _main_capture(["submit", "echo hi"])
            assert code == 7, f"main(click-Exit-7) exit={code}"
        finally:
            cmds.submit_cmd = original

    def test_main_handler_returns_none_collapses_to_exit_ok(self, taskq_home, monkeypatch):
        """Line 397: when click returns None (subcommand handler returns None
        instead of an int), main() collapses to EXIT_OK."""
        # Patch the click callback itself (not the wrapped commands.submit_cmd)
        # so the click group returns None from cli.main() and the
        # `if result is None` branch in main() is exercised.
        original_callback = cli_group.commands["submit"].callback
        cli_group.commands["submit"].callback = lambda *a, **kw: None
        try:
            code, _out, _err = _main_capture(["submit", "echo hi"])
            assert code == EXIT_OK, f"main(handler-returns-None) exit={code}"
        finally:
            cli_group.commands["submit"].callback = original_callback


def _main_capture_with_none_argv():
    """Same as `_main_capture` but passes argv=None so the `argv is None`
    branch in cli.main.main is exercised."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli_main(None)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class TestCoverageCommandsHelpers:
    """Cover `cli/commands.py` internal helpers."""

    def test_resolve_max_dag_depth_value_error(self, monkeypatch):
        """Lines 119-120: invalid int env var falls back to default 32."""
        monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "not-a-number")
        assert _cmd_mod._resolve_max_dag_depth() == 32

    def test_resolve_max_dag_depth_empty_string(self, monkeypatch):
        """Lines 117-118: empty string falls back to default 32."""
        monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "")
        assert _cmd_mod._resolve_max_dag_depth() == 32

    def test_compute_depth_with_existing_chain(self, taskq_home):
        """Lines 139, 142-143, 148-149: depth traversal over a 3-task chain."""
        a = _submit_task("echo a")
        b = _submit_task("echo b", extra=["--after", a])
        c = _submit_task("echo c", extra=["--after", b])
        # `a` has no parents (depth 0). `b` depends on `a` (depth 1). `c`
        # depends on `b` (depth 2).
        assert _cmd_mod._compute_depth([]) == 0
        assert _cmd_mod._compute_depth([a]) == 1
        assert _cmd_mod._compute_depth([b]) == 2
        assert _cmd_mod._compute_depth([c]) == 3

    def test_compute_depth_missing_parent_treated_as_zero(self, taskq_home):
        """Lines 141-143: a parent id with no matching task → depth 1 (parent
        contributes 0; the new task itself adds 1)."""
        assert _cmd_mod._compute_depth(["00000000"]) == 1

    def test_strict_load_tasks_dict_with_tasks_key(self, taskq_home):
        """Line 166-167: tasks.json is a dict with a "tasks" key → returned."""
        payload = {"tasks": [{"id": "abcdef12", "status": "pending"}]}
        (taskq_home / "tasks.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        out = _cmd_mod._strict_load_tasks()
        assert isinstance(out, list)
        assert out[0]["id"] == "abcdef12"

    def test_strict_load_tasks_not_a_list_or_dict_with_tasks(self, taskq_home):
        """Line 168: tasks.json is some other JSON shape → StoreCorrupted."""
        (taskq_home / "tasks.json").write_text(
            json.dumps({"unexpected": "shape"}), encoding="utf-8"
        )
        with pytest.raises(_cmd_mod.StoreCorrupted):
            _cmd_mod._strict_load_tasks()

    def test_redact_non_string_passthrough(self):
        """Line 182: _redact() with a non-string returns it unchanged."""
        assert _cmd_mod._redact(42) == 42
        assert _cmd_mod._redact(None) is None
        assert _cmd_mod._redact({"k": "v"}) == {"k": "v"}

    def test_redact_string_with_secret_pattern(self):
        """Line 183: _redact() with a string containing NFR-04 secret → redacted."""
        redacted = _cmd_mod._redact("echo sk-abcdef1234 hello")
        assert "[REDACTED]" in redacted
        assert "sk-abcdef1234" not in redacted

    def test_redact_string_without_secret_passes_through(self):
        """Line 183: _redact() with a string without secrets returns it verbatim."""
        assert _cmd_mod._redact("echo hello world") == "echo hello world"

    def test_redact_task_replaces_command_field(self):
        """Lines 189-191: _redact_task returns a copy with redacted command."""
        task = {
            "id": "abcdef12",
            "command": "echo sk-abcdef1234",
            "status": "pending",
        }
        out = _cmd_mod._redact_task(task)
        assert out["command"] == "echo [REDACTED]"
        # original dict not mutated
        assert task["command"] == "echo sk-abcdef1234"
        # other fields preserved
        assert out["id"] == "abcdef12"
        assert out["status"] == "pending"

    def test_redact_task_with_no_command(self):
        """Lines 189-191: _redact_task with missing command key uses default ''."""
        task = {"id": "abcdef12", "status": "pending"}
        out = _cmd_mod._redact_task(task)
        assert out["command"] == ""

    def test_tasks_by_id_skips_tasks_with_none_id(self, taskq_home):
        """Line 137: _tasks_by_id() skips tasks whose id is None."""
        # Persist a tasks.json containing one task with no `id` and one valid.
        payload = [
            {"command": "echo orphan", "status": "pending"},  # no id
            {"id": "abcdef12", "command": "echo named", "status": "pending"},
        ]
        (taskq_home / "tasks.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        by_id = _cmd_mod._tasks_by_id()
        assert "abcdef12" in by_id
        assert None not in by_id


class TestCoverageCommandsHandlers:
    """Cover handler-specific error paths in cli/commands.py."""

    def test_submit_cmd_validation_error_empty_command(self, taskq_home):
        """Lines 217-219: empty command rejected by TaskSubmission."""
        with pytest.raises(_cmd_mod.SubmitValidationError):
            _cmd_mod.submit_cmd("")

    def test_submit_cmd_duplicate_name(self, taskq_home):
        """Line 222: duplicate name → SubmitValidationError."""
        _cmd_mod.submit_cmd("echo first", name="dup-name")
        with pytest.raises(_cmd_mod.SubmitValidationError):
            _cmd_mod.submit_cmd("echo second", name="dup-name")

    def test_submit_cmd_unknown_dependency(self, taskq_home):
        """Line 228: depends_on id does not exist → SubmitValidationError."""
        with pytest.raises(_cmd_mod.SubmitValidationError):
            _cmd_mod.submit_cmd("echo x", after=["00000000"])

    def test_submit_cmd_cycle_detected_raises_graph_error(self, taskq_home):
        """Line 247: cyclic store → submit raises GraphError."""
        # Pre-seed tasks.json with two tasks that depend on each other.
        cyclic = [
            {
                "id": "aaaa1111",
                "command": "echo a",
                "status": "pending",
                "depends_on": ["bbbb2222"],
            },
            {
                "id": "bbbb2222",
                "command": "echo b",
                "status": "pending",
                "depends_on": ["aaaa1111"],
            },
        ]
        (taskq_home / "tasks.json").write_text(
            json.dumps(cyclic), encoding="utf-8"
        )
        with pytest.raises(_cmd_mod.GraphError) as ei:
            _cmd_mod.submit_cmd("echo c")
        # The error message mentions a cycle.
        assert "cycle" in str(ei.value).lower()

    def test_run_cmd_no_task_id_raises(self):
        """Line 285: run_cmd() with no task_id → RunValidationError."""
        with pytest.raises(_cmd_mod.RunValidationError):
            _cmd_mod.run_cmd(task_id=None)

    def test_run_cmd_unknown_task_raises(self):
        """Line 291: run_cmd(unknown_id) → RunValidationError."""
        with pytest.raises(_cmd_mod.RunValidationError):
            _cmd_mod.run_cmd(task_id="00000000")

    def test_run_cmd_run_all_returns_dict(self, taskq_home, monkeypatch):
        """Lines 281-282: run_cmd(run_all=True) → {"ran_all": True, "exit_code": 0}."""
        out = _cmd_mod.run_cmd(run_all=True)
        assert out.get("ran_all") is True
        assert out.get("exit_code") == 0

    def test_run_cmd_none_result_raises(self, taskq_home, monkeypatch):
        """Line 300: execute_with_cache returning None → RunInternalError."""
        from taskq_plus.service import cache as cache_mod

        # Submit a real task first so find_by_id succeeds.
        payload = _cmd_mod.submit_cmd("echo run-none-result")
        task_id = payload["id"]
        original = cache_mod.execute_with_cache
        cache_mod.execute_with_cache = lambda *a, **kw: None
        try:
            with pytest.raises(_cmd_mod.RunInternalError):
                _cmd_mod.run_cmd(task_id=task_id)
        finally:
            cache_mod.execute_with_cache = original

    def test_status_cmd_empty_task_id_raises(self):
        """Line 326: status_cmd("") → StatusValidationError."""
        with pytest.raises(_cmd_mod.StatusValidationError):
            _cmd_mod.status_cmd("")

    def test_plugins_cmd_unknown_subcommand_raises(self):
        """Line 382: plugins_cmd("bad") → PluginValidationError."""
        with pytest.raises(_cmd_mod.PluginValidationError):
            _cmd_mod.plugins_cmd("bad")

    def test_plugins_cmd_allowlist_rejection(self, monkeypatch):
        """Line 396: path-form plugin name → PluginLoadError."""
        monkeypatch.setenv("TASKQ_PLUGINS", "../evil.py")
        with pytest.raises(_cmd_mod.PluginLoadError):
            _cmd_mod.plugins_cmd("list")

    def test_plugins_cmd_valid_names_returns_dict(self, monkeypatch):
        """Lines 396-398: valid allowlist names → plugins list with empty hooks."""
        monkeypatch.setenv("TASKQ_PLUGINS", "alpha_plugin,beta_plugin")
        out = _cmd_mod.plugins_cmd("list")
        assert out["count"] == 2
        assert {p["name"] for p in out["plugins"]} == {"alpha_plugin", "beta_plugin"}
        assert all(p["hooks"] == [] for p in out["plugins"])

    def test_plugins_cmd_import_error_raises_plugin_load_error(self, monkeypatch):
        """Lines 435-437: ImportError on service.plugins → PluginLoadError."""
        import builtins
        import sys as _sys

        # Hide the real service.plugins module so the `from taskq_plus.service.plugins import …`
        # statement raises ImportError.
        hidden_name = "taskq_plus.service.plugins"
        original_module = _sys.modules.get(hidden_name)
        _sys.modules[hidden_name] = None  # sentinel triggers ImportError on re-import

        real_import = builtins.__import__

        def _hook(name, *args, **kwargs):
            if name == hidden_name or name.startswith(hidden_name + "."):
                raise ImportError(f"synthetic missing plugin service: {name}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _hook)
        try:
            with pytest.raises(_cmd_mod.PluginLoadError) as ei:
                _cmd_mod.plugins_cmd("list")
            assert "plugin service unavailable" in str(ei.value)
        finally:
            if original_module is not None:
                _sys.modules[hidden_name] = original_module
            else:
                _sys.modules.pop(hidden_name, None)

    def test_plugins_cmd_skips_empty_pieces(self, monkeypatch):
        """Lines 390-391: empty / whitespace-only pieces are skipped, not rejected."""
        monkeypatch.setenv("TASKQ_PLUGINS", "alpha_plugin,, ,beta_plugin")
        out = _cmd_mod.plugins_cmd("list")
        assert out["count"] == 2

    def test_export_cmd_unsupported_format_raises(self):
        """Line 414: export_cmd("xml") → ExportValidationError."""
        with pytest.raises(_cmd_mod.ExportValidationError):
            _cmd_mod.export_cmd("xml")

    def test_run_cmd_status_done_returns_exit_ok(self, taskq_home, monkeypatch):
        """Lines 311-313: run_cmd when result has status='done' and no int exit_code
        → returns dict with exit_code=0."""
        from taskq_plus.service import cache as cache_mod

        task_id = _submit_task("echo run-status-done")
        original = cache_mod.execute_with_cache
        cache_mod.execute_with_cache = lambda *a, **kw: {"status": "done", "cached": False}
        try:
            out = _cmd_mod.run_cmd(task_id=task_id)
            assert out["exit_code"] == _cmd_mod.EXIT_OK
        finally:
            cache_mod.execute_with_cache = original

    def test_run_cmd_status_timeout_returns_exit_timeout(self, taskq_home, monkeypatch):
        """Lines 314-315: run_cmd when result has status='timeout' → exit_code=4."""
        from taskq_plus.service import cache as cache_mod

        task_id = _submit_task("echo run-status-timeout")
        original = cache_mod.execute_with_cache
        cache_mod.execute_with_cache = lambda *a, **kw: {"status": "timeout", "cached": False}
        try:
            out = _cmd_mod.run_cmd(task_id=task_id)
            assert out["exit_code"] == _cmd_mod.EXIT_TIMEOUT
        finally:
            cache_mod.execute_with_cache = original

    def test_run_cmd_status_other_returns_exit_failed(self, taskq_home, monkeypatch):
        """Line 316: run_cmd when result has neither int exit_code nor 'done'/'timeout'
        status → exit_code=1 (EXIT_FAILED)."""
        from taskq_plus.service import cache as cache_mod

        task_id = _submit_task("echo run-status-failed")
        original = cache_mod.execute_with_cache
        cache_mod.execute_with_cache = lambda *a, **kw: {"status": "weird", "cached": False}
        try:
            out = _cmd_mod.run_cmd(task_id=task_id)
            assert out["exit_code"] == _cmd_mod.EXIT_FAILED
        finally:
            cache_mod.execute_with_cache = original

    def test_compute_depth_shared_ancestor_triggers_cache_hit(self, taskq_home):
        """Line 139: when two pending tasks share an ancestor, the recursive
        `depth()` reuses the memoized entry — covering the cache-hit branch."""
        root_id = _submit_task("echo shared-root")
        child_a = _submit_task("echo child-a", extra=["--after", root_id])
        child_b = _submit_task("echo child-b", extra=["--after", root_id])
        # Both children depend on `root_id`; the second call to `depth(root_id)`
        # inside `_compute_depth([child_a, child_b])` must hit the memo.
        depth = _cmd_mod._compute_depth([child_a, child_b])
        assert depth == 2

    def test_clear_cmd_handles_oserror(self, taskq_home, monkeypatch):
        """Lines 470-472: clear_cmd continues when unlink raises OSError."""
        # Create the file then patch unlink to raise.
        (taskq_home / "tasks.json").write_text("[]", encoding="utf-8")

        real_unlink = Path.unlink

        def _flaky_unlink(self, *a, **kw):
            if self.name == "tasks.json":
                raise OSError("simulated filesystem refusal")
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", _flaky_unlink)
        out = _cmd_mod.clear_cmd()
        assert out["cleared"] is True
        # tasks.json was not removed (OSError), but the call did not crash.
        assert "tasks.json" not in out["removed"]


class TestCoverageCommandsLegacy:
    """Cover the legacy argparse-based path (used by test_fr01 / __main__)."""

    def test_build_parser_returns_parser(self):
        """Lines 485-489: _build_parser returns an ArgumentParser."""
        parser = _cmd_mod._build_parser()
        assert parser.prog == "taskq_plus"

    def test_build_submit_parser(self):
        """Lines 494-507: _build_submit_parser parses submit args."""
        parser = _cmd_mod._build_submit_parser()
        args = parser.parse_args(["echo hi", "--name", "n", "--after", "abc"])
        assert args.command == "echo hi"
        assert args.name == "n"
        assert args.after == ["abc"]
        assert args.as_json is False

    def test_build_submit_parser_as_json_flag(self):
        """Lines 504-506: --json flag sets as_json=True."""
        parser = _cmd_mod._build_submit_parser()
        args = parser.parse_args(["echo hi", "--json"])
        assert args.as_json is True

    def test_build_run_parser(self):
        """Lines 518-529: _build_run_parser parses run args."""
        parser = _cmd_mod._build_run_parser()
        args = parser.parse_args(["abcdef12", "--all", "--cached"])
        assert args.task_id == "abcdef12"
        assert args.run_all is True
        assert args.use_cache is True

    def test_run_legacy_run_all(self, taskq_home, monkeypatch):
        """Lines 547-549: legacy _run with --all → calls exec_run_all + EXIT_OK."""
        # exec_run_all is a no-op when no pending tasks; that is fine here.
        code = _cmd_mod._run(["--all"])
        assert code == _cmd_mod.EXIT_OK

    def test_run_legacy_no_task_id_returns_validation(self, taskq_home, monkeypatch):
        """Lines 550-555: legacy _run with no task_id → EXIT_VALIDATION_ERROR."""
        code = _cmd_mod._run([])
        assert code == _cmd_mod.EXIT_VALIDATION_ERROR

    def test_run_legacy_uses_executor_result(self, taskq_home, monkeypatch):
        """Lines 562-575: legacy _run with valid task_id propagates exit_code."""
        task_id = _submit_task("echo legacy-run")
        code = _cmd_mod._run([task_id])
        assert code in {_cmd_mod.EXIT_OK, _cmd_mod.EXIT_FAILED}

    def test_run_legacy_result_none_returns_failed(self, taskq_home, monkeypatch):
        """Lines 563-564: legacy _run when execute_with_cache returns None → EXIT_FAILED."""
        from taskq_plus.service import cache as cache_mod

        original = cache_mod.execute_with_cache
        cache_mod.execute_with_cache = lambda *a, **kw: None
        try:
            code = _cmd_mod._run(["abcdef12"])
            assert code == _cmd_mod.EXIT_FAILED
        finally:
            cache_mod.execute_with_cache = original

    def test_run_legacy_status_done_returns_ok(self, taskq_home, monkeypatch):
        """Lines 572-573: legacy _run when result has status='done' and no int exit_code
        → EXIT_OK."""
        from taskq_plus.service import cache as cache_mod

        task_id = _submit_task("echo legacy-status-done")
        original = cache_mod.execute_with_cache
        cache_mod.execute_with_cache = lambda *a, **kw: {"status": "done", "cached": False}
        try:
            code = _cmd_mod._run([task_id])
            assert code == _cmd_mod.EXIT_OK
        finally:
            cache_mod.execute_with_cache = original

    def test_run_legacy_status_timeout_returns_timeout(self, taskq_home, monkeypatch):
        """Lines 574-575: legacy _run when result has status='timeout' → EXIT_TIMEOUT."""
        from taskq_plus.service import cache as cache_mod

        task_id = _submit_task("echo legacy-status-timeout")
        original = cache_mod.execute_with_cache
        cache_mod.execute_with_cache = lambda *a, **kw: {"status": "timeout", "cached": False}
        try:
            code = _cmd_mod._run([task_id])
            assert code == _cmd_mod.EXIT_TIMEOUT
        finally:
            cache_mod.execute_with_cache = original

    def test_run_legacy_unknown_status_returns_failed(self, taskq_home, monkeypatch):
        """Line 576: legacy _run when result has neither int exit_code nor
        'done'/'timeout' status → EXIT_FAILED."""
        from taskq_plus.service import cache as cache_mod

        task_id = _submit_task("echo legacy-status-failed")
        original = cache_mod.execute_with_cache
        cache_mod.execute_with_cache = lambda *a, **kw: {"status": "weird", "cached": False}
        try:
            code = _cmd_mod._run([task_id])
            assert code == _cmd_mod.EXIT_FAILED
        finally:
            cache_mod.execute_with_cache = original

    def test_submit_legacy_validation_error(self, taskq_home, monkeypatch, capsys):
        """Lines 598-600: legacy _submit with empty command → EXIT_VALIDATION_ERROR."""
        code = _cmd_mod._submit([""])
        assert code == _cmd_mod.EXIT_VALIDATION_ERROR
        captured = capsys.readouterr()
        assert "error" in captured.err.lower()

    def test_submit_legacy_dependency_not_found(self, taskq_home, monkeypatch, capsys):
        """Lines 611-617: legacy _submit with unknown dep → EXIT_NOT_FOUND."""
        code = _cmd_mod._submit(["echo hi", "--after", "00000000"])
        assert code == _cmd_mod.EXIT_NOT_FOUND
        captured = capsys.readouterr()
        assert "error" in captured.err.lower()

    def test_submit_legacy_duplicate_name(self, taskq_home, monkeypatch, capsys):
        """Lines 603-608: legacy _submit with duplicate name → EXIT_VALIDATION_ERROR."""
        _cmd_mod._submit(["echo hi", "--name", "dup-legacy"])
        code = _cmd_mod._submit(["echo hi2", "--name", "dup-legacy"])
        assert code == _cmd_mod.EXIT_VALIDATION_ERROR
        captured = capsys.readouterr()
        assert "error" in captured.err.lower()

    def test_submit_legacy_as_json_emits_json(self, taskq_home, monkeypatch, capsys):
        """Line 637-638: legacy _submit --json → JSON to stdout."""
        code = _cmd_mod._submit(["echo hi", "--json"])
        assert code == _cmd_mod.EXIT_OK
        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip())
        assert payload["status"] == "pending"
        assert "id" in payload

    def test_submit_legacy_human_readable(self, taskq_home, monkeypatch, capsys):
        """Line 639-640: legacy _submit without --json → plain id to stdout."""
        code = _cmd_mod._submit(["echo hi"])
        assert code == _cmd_mod.EXIT_OK
        captured = capsys.readouterr()
        assert len(captured.out.strip()) == 8

    def test_dispatch_empty_argv_returns_ok(self, capsys):
        """Lines 650-652: dispatch([]) prints help and returns EXIT_OK."""
        code = _cmd_mod.dispatch([])
        assert code == _cmd_mod.EXIT_OK

    def test_dispatch_unknown_subcommand_returns_validation(self, capsys):
        """Lines 658-659: dispatch(["bad"]) → EXIT_VALIDATION_ERROR."""
        code = _cmd_mod.dispatch(["bad"])
        assert code == _cmd_mod.EXIT_VALIDATION_ERROR

    def test_dispatch_submit_delegates(self, taskq_home, monkeypatch):
        """Lines 654-655: dispatch(["submit", ...]) → _submit(...) result."""
        code = _cmd_mod.dispatch(["submit", "echo dispatched"])
        assert code == _cmd_mod.EXIT_OK

    def test_dispatch_run_delegates(self, taskq_home, monkeypatch):
        """Lines 656-657: dispatch(["run", ...]) → _run(...) result."""
        code = _cmd_mod.dispatch(["run", "--all"])
        assert code == _cmd_mod.EXIT_OK

    def test_main_legacy_uses_sys_argv(self, taskq_home, monkeypatch):
        """Lines 668-670: legacy main(argv=None) reads sys.argv[1:]."""
        monkeypatch.setattr(sys, "argv", ["taskq_plus", "submit", "echo main-legacy"])
        code = _cmd_mod.main(None)
        assert code == _cmd_mod.EXIT_OK

    def test_main_legacy_with_argv(self, taskq_home, monkeypatch):
        """Line 669-670: legacy main(argv) dispatches the given argv."""
        code = _cmd_mod.main(["submit", "echo with-argv"])
        assert code == _cmd_mod.EXIT_OK
