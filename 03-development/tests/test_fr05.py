"""TDD-RED tests for FR-05 (CLI 整合: click group + 8 子命令 + exit-code map).

Maps 1:1 to TEST_SPEC.md §FR-05 cases 1-16. The expected RED state is
"unknown command" / argparse errors for the not-yet-implemented
subcommands (``status``, ``list``, ``graph``, ``plugins``, ``export``,
``clear``), and/or every assertion failing; either counts as a valid
failing test in this phase per the harness TDD contract.

GREEN TODO — the modules and contracts these tests bind to:

* ``taskq_plus.cli`` exposes ``cli.main(argv: list[str]) -> int`` and
  dispatches every FR-05 subcommand to a handler. The subcommands
  declared verbatim by SPEC.md §3 are:
    - ``submit "<cmd>" [--name N] [--after ID]...``         (FR-01)
    - ``run <id> [--cached]`` / ``run --all``               (FR-02/03/04/06)
    - ``status <id>``                                       (FR-05 wiring)
    - ``list [--status S]``                                 (FR-05 wiring)
    - ``graph [--format text|dot]``                         (FR-06)
    - ``plugins list``                                      (FR-07)
    - ``export --format json|csv|md``                       (FR-08)
    - ``clear``                                             (FR-05 wiring)
* A global ``--json`` flag (where applicable — ``status``, ``list``,
  ``graph``, ``plugins list``, ``export``) emits a SINGLE-LINE JSON
  document on stdout (AC-FR-05.2).
* The exit-code map from SPEC.md §3 / §7 is wired end-to-end:
    0 success / 2 validation / 3 breaker open / 4 timeout
    5 cycle or depth cap / 6 plugin load failure / 1 internal error
* ``plugins list`` rejects invalid plugin specs (path forms /
  invalid regex) with exit 6; if ``$TASKQ_PLUGINS`` cannot be loaded
  on CLI startup, the next subcommand exits 6 (AC-FR-07.1 / 5.3).
* A corrupt ``$TASKQ_HOME/tasks.json`` is detected (exit 1) instead
  of crashing with an unhandled ``JSONDecodeError`` (AC-FR-05.3
  exit-1-internal).

Test design follows the harness canonical pattern — bind the declared
TEST_SPEC Inputs to local variables, capture a single ``result`` record
per invocation, and emit each spec sub-assertion as a bare ``assert``
matching the predicate shape verbatim from TEST_SPEC.md.

NOTE on subprocess coverage — TEST_SPEC declares every FR-05 case
``subprocess_mode="out_of_process"``. The integration-test coverage gate
(NFR-10) handles in-process coverage via the cross-cutting
``test_int_*`` family, so this file stays out-of-process for fidelity
to the user-visible CLI surface.
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

# Top-level imports are intentional — ModuleNotFoundError at collection
# time is the expected RED signal per the unit-test contract.
from taskq_plus import cli  # noqa: E402
from taskq_plus.storage.task_store import TaskStore  # noqa: E402


# -------------------------------------------------------------------
# Fixtures — function-scoped so tasks.json / breaker.json cannot leak
# between cases (an OPEN breaker from case 12 must not affect case 13).
# -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_taskq_home(monkeypatch, tmp_path):
    """Per-test $TASKQ_HOME isolation (TEST_SPEC state_mode=isolate_per_test)."""
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv("TASKQ_HOME", str(home))
    monkeypatch.setenv("TASKQ_AUDIT_LOG", str(home / "audit.jsonl"))
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "4")
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0.1")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "5.0")
    monkeypatch.setenv("TASKQ_CACHE_TTL", "3600")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "32")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "30")
    monkeypatch.setenv("TASKQ_PLUGINS", "")
    yield home


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _home() -> Path:
    """Return the isolated ``$TASKQ_HOME`` for the running test."""
    return Path(os.environ["TASKQ_HOME"])


def _breaker_path() -> Path:
    return _home() / "breaker.json"


def _tasks_path() -> Path:
    return _home() / "tasks.json"


def _child_env(home: Path, **overrides: str) -> dict:
    """Compose subprocess env with $TASKQ_HOME + PYTHONPATH for the child.

    pytest's sys.path bootstrap (tests/conftest.py) does NOT propagate
    to a child interpreter, so ``03-development/src`` is pushed
    explicitly. Per-task overrides win over the inherited environment.
    """
    child_env = os.environ.copy()
    child_env["TASKQ_HOME"] = str(home)
    child_env["TASKQ_AUDIT_LOG"] = str(home / "audit.jsonl")
    src_root = (
        Path(__file__).resolve().parents[2] / "03-development" / "src"
    )
    child_env["PYTHONPATH"] = (
        str(src_root) + os.pathsep + child_env.get("PYTHONPATH", "")
    )
    for key, value in overrides.items():
        child_env[key] = value
    return child_env


def _run_cli(argv: list[str], *, env_overrides: dict | None = None):
    """Invoke ``python -m taskq_plus`` out-of-process and capture output.

    Returns a ``result`` SimpleNamespace with ``exit_code``, ``stdout``,
    ``stderr`` populated from the child process. ``env_overrides`` win
    over the autouse fixture's environment.
    """
    home = _home()
    overrides = env_overrides or {}
    child_env = _child_env(home, **overrides)
    completed = subprocess.run(
        [sys.executable, "-m", "taskq_plus", *argv],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        shell=False,
    )
    return SimpleNamespace(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _run_cli_inprocess(argv: list[str]):
    """Invoke ``cli.main(argv)`` in-process, capturing stdout/stderr.

    Used sparingly for cases that need the same in-process surface
    the subprocess tests probe (so the spec predicates map 1:1 to
    real assertions).
    """
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


def _submit_task(command: str, *, env_overrides: dict | None = None) -> str:
    """Submit ``command`` and return its 8-hex task id from stdout."""
    cli_result = _run_cli(["submit", command], env_overrides=env_overrides)
    if cli_result.exit_code != 0:
        raise AssertionError(
            f"submit {command!r} failed (exit {cli_result.exit_code}): "
            f"stdout={cli_result.stdout!r} stderr={cli_result.stderr!r}"
        )
    task_id = cli_result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{8}", task_id):
        raise AssertionError(
            f"submit did not emit an 8-hex id: stdout={cli_result.stdout!r} "
            f"stderr={cli_result.stderr!r}"
        )
    return task_id


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
    """Return the in-memory ``{id: record}`` view of ``tasks.json``.

    Tolerates the flat shape (same defensive tolerance as TaskStore.load).
    """
    envelope = _read_tasks(home)
    inner = envelope.get("tasks")
    if isinstance(inner, dict):
        return inner
    if envelope and all(isinstance(v, dict) for v in envelope.values()):
        return envelope
    return {}


def _seed_breaker_open(failure_count: int = 3, opened_at: float | None = None):
    """Write an OPEN breaker envelope to ``$TASKQ_HOME/breaker.json``.

    GREEN TODO: ``taskq_plus.storage.breaker_store`` reads this exact
    envelope shape (``{version, state, failure_count, opened_at}``).
    """
    if opened_at is None:
        opened_at = time.time()
    payload = {
        "version": 1,
        "state": "OPEN",
        "failure_count": failure_count,
        "opened_at": opened_at,
    }
    _breaker_path().write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _help_contains(result: SimpleNamespace, subcommand_name: str) -> bool:
    """Return True when ``subcommand_name`` appears in either help stream.

    AC5-subcommand-reachable: ``result.subcommand_in_help == True``.
    """
    return subcommand_name in result.stdout or subcommand_name in result.stderr


# -------------------------------------------------------------------
# FR-05 Cases 1-8 — subcommand reachability (8 happy_path / out_of_process)
# -------------------------------------------------------------------


# All 8 reachability cases share the same wiring shape; the per-test
# GREEN TODO is the same surface — every one of the eight subcommands
# listed in SPEC.md §3 FR-05 must be wired into ``cli.main()``'s parser.
# GREEN TODO: ``taskq_plus.cli._build_parser`` must register all
#   eight subparsers: submit, run, status, list, graph, plugins,
#   export, clear — and ``plugins`` must itself expose a
#   ``list`` sub-subcommand.
def _assert_subcommand_reachable(home: Path, subcommand_argv: list[str], subcommand_name: str):
    """Probe ``python -m taskq_plus <subcommand_argv> --help``.

    The pytest reachability criterion is that argparse recognises the
    subcommand — i.e. ``--help`` does NOT exit with the
    "invalid choice" error. Exit 0 (real help) or a sub-help exit
    both count as reachable; the only failure is the "invalid choice"
    stderr that argparse emits when the subcommand is unknown.
    """
    help_result = _run_cli([*subcommand_argv, "--help"])
    text = help_result.stdout + help_result.stderr
    invalid_choice = "invalid choice" in text.lower() or "no such option" in text.lower()
    result = SimpleNamespace(
        exit_code=help_result.exit_code,
        subcommand_in_help=not invalid_choice,
        stdout=help_result.stdout,
        stderr=help_result.stderr,
    )
    assert result.subcommand_in_help is True, (
        f"subcommand {subcommand_name!r} is not reachable via "
        f"python -m taskq_plus {' '.join(subcommand_argv)!r}: "
        f"exit={result.exit_code} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_fr05_cli_reachable_submit(isolated_taskq_home):
    """AC-FR-05.1 (case 1) — ``python -m taskq_plus submit ...`` is reachable.

    Sub-assertion: AC5-subcommand-reachable.
    """
    subcommand = "submit"
    assert _help_contains.__name__ == "_help_contains"  # sanity
    _assert_subcommand_reachable(
        isolated_taskq_home, ["submit"], subcommand
    )


def test_fr05_cli_reachable_run(isolated_taskq_home):
    """AC-FR-05.1 (case 2) — ``python -m taskq_plus run ...`` is reachable."""
    subcommand = "run"
    _assert_subcommand_reachable(
        isolated_taskq_home, ["run"], subcommand
    )


def test_fr05_cli_reachable_status(isolated_taskq_home):
    """AC-FR-05.1 (case 3) — ``python -m taskq_plus status <id>`` is reachable.

    Sub-assertion: AC5-subcommand-reachable.
    """
    subcommand = "status"
    _assert_subcommand_reachable(
        isolated_taskq_home, ["status", "deadbeef"], subcommand
    )


def test_fr05_cli_reachable_list(isolated_taskq_home):
    """AC-FR-05.1 (case 4) — ``python -m taskq_plus list ...`` is reachable."""
    subcommand = "list"
    _assert_subcommand_reachable(
        isolated_taskq_home, ["list"], subcommand
    )


def test_fr05_cli_reachable_graph(isolated_taskq_home):
    """AC-FR-05.1 (case 5) — ``python -m taskq_plus graph ...`` is reachable.

    Sub-assertion: AC5-subcommand-reachable.
    """
    subcommand = "graph"
    _assert_subcommand_reachable(
        isolated_taskq_home, ["graph"], subcommand
    )


def test_fr05_cli_reachable_plugins_list(isolated_taskq_home):
    """AC-FR-05.1 (case 6) — ``python -m taskq_plus plugins list`` is reachable.

    Nested subcommand: ``plugins`` exposes ``list`` as a sub-sub command.
    """
    subcommand = "plugins list"
    _assert_subcommand_reachable(
        isolated_taskq_home, ["plugins", "list"], subcommand
    )


def test_fr05_cli_reachable_export(isolated_taskq_home):
    """AC-FR-05.1 (case 7) — ``python -m taskq_plus export ...`` is reachable."""
    subcommand = "export"
    _assert_subcommand_reachable(
        isolated_taskq_home, ["export", "--format", "json"], subcommand
    )


def test_fr05_cli_reachable_clear(isolated_taskq_home):
    """AC-FR-05.1 (case 8) — ``python -m taskq_plus clear`` is reachable."""
    subcommand = "clear"
    _assert_subcommand_reachable(
        isolated_taskq_home, ["clear"], subcommand
    )


# -------------------------------------------------------------------
# FR-05 Case 9 — ``--json`` flag produces single-line JSON
# -------------------------------------------------------------------


# AC-FR-05.2 — global ``--json`` flag is recognised by every subcommand
# that emits structured output (``status``, ``list``, ``graph``,
# ``plugins list``, ``export``).
# GREEN TODO: the cli parser must accept ``--json`` as a global flag
#   on click / argparse, and the status handler must produce one
#   single-line JSON document on stdout when ``--json`` is set.
def test_fr05_cli_json_flag_produces_single_line_json(isolated_taskq_home):
    """AC-FR-05.2 — ``--json`` flag emits exactly one line of JSON.

    Sub-assertion: AC5-json-single-line (``stdout_newlines ==
    expected_newlines`` = 1).

    We submit a task to exercise ``status <id> --json`` — the canonical
    "structured output" surface per SPEC.md §3 FR-05. ``--json`` MUST
    emit exactly ONE line that is itself valid JSON.
    """
    subcommand = "status"
    expected_newlines = 1
    task_id = _submit_task("echo hi")

    cli_result = _run_cli(["status", task_id, "--json"])
    out_text = cli_result.stdout
    raw_line = out_text.rstrip("\n")
    stdout_newlines = raw_line.count("\n") + (1 if raw_line else 0)

    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        stdout_newlines=stdout_newlines,
        stdout_raw=out_text,
        stdout_json_ok=False,
        decoded=None,
    )

    # AC5-json-single-line: result.stdout_newlines == expected_newlines
    assert result.stdout_newlines == expected_newlines, (
        f"--json must emit exactly 1 line, got {result.stdout_newlines}: "
        f"{result.stdout_raw!r}"
    )
    # Belt-and-braces: the single line must itself be valid JSON.
    try:
        result.decoded = json.loads(raw_line)
        result.stdout_json_ok = True
    except json.JSONDecodeError:
        result.stdout_json_ok = False
    assert result.stdout_json_ok, (
        f"--json produced non-JSON output: {result.stdout_raw!r}"
    )


# -------------------------------------------------------------------
# FR-05 Case 10 — exit code 0 (success) — happy_submit
# -------------------------------------------------------------------


def test_fr05_exit_code_0_success(isolated_taskq_home):
    """AC-FR-05.3 — happy submit returns exit 0.

    Sub-assertion: AC5-exit-0-success (``result.exit_code == 0``).

    Out-of-process: the ``python -m taskq_plus submit ``echo hi"``
    invocation must round-trip the CLI surface and end with exit 0,
    echoing back the 8-hex task id.
    """
    scenario = "happy_submit"
    expected_exit = 0
    cli_result = _run_cli(["submit", "echo hi"])
    task_id = cli_result.stdout.strip()
    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        task_id_str=task_id,
        stdout=cli_result.stdout,
        stderr=cli_result.stderr,
    )
    assert scenario == "happy_submit"
    # AC5-exit-0-success: `result.exit_code == 0`
    assert result.exit_code == expected_exit
    # Sanity: the canonical happy-path signature — 8-hex id on stdout.
    assert re.fullmatch(r"[0-9a-f]{8}", result.task_id_str)


# -------------------------------------------------------------------
# FR-05 Case 11 — exit code 2 (validation) — submit_empty
# -------------------------------------------------------------------


def test_fr05_exit_code_2_validation_error(isolated_taskq_home):
    """AC-FR-05.3 — empty submit command returns exit 2.

    Sub-assertion: AC5-exit-2-validation (``result.exit_code == 2``).
    """
    scenario = "submit_empty"
    expected_exit = 2
    cli_result = _run_cli(["submit", ""])
    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        stdout=cli_result.stdout,
        stderr=cli_result.stderr,
        tasks_file=_tasks_path(),
    )
    assert scenario == "submit_empty"
    # AC5-exit-2-validation: `result.exit_code == 2`
    assert result.exit_code == expected_exit
    # Must not have persisted a task.
    assert not result.tasks_file.exists() or result.tasks_file.stat().st_size == 0
    # stderr should explain the rejection (FR-01 wording).
    assert result.stderr.strip() != ""


# -------------------------------------------------------------------
# FR-05 Case 12 — exit code 3 (breaker open) — run_while_open
# -------------------------------------------------------------------


def test_fr05_exit_code_3_breaker_open(isolated_taskq_home):
    """AC-FR-05.3 — while breaker is OPEN, ``run`` returns exit 3.

    Sub-assertion: AC5-exit-3-breaker-open (``result.exit_code == 3``).
    """
    scenario = "run_while_open"
    expected_exit = 3
    task_id = _submit_task("true")
    # Seed an OPEN breaker inside the cooldown window so the run must
    # be rejected without executing any subprocess (FR-03 step 2).
    _seed_breaker_open()

    cli_result = _run_cli(["run", task_id])
    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        stdout=cli_result.stdout,
        stderr=cli_result.stderr,
    )
    assert scenario == "run_while_open"
    # AC5-exit-3-breaker-open: `result.exit_code == 3`
    assert result.exit_code == expected_exit
    # SPEC.md §3 FR-03 verbatim stderr marker.
    assert "breaker open" in result.stderr


# -------------------------------------------------------------------
# FR-05 Case 13 — exit code 4 (timeout) — run_sleep_5_with_timeout_1
# -------------------------------------------------------------------


def test_fr05_exit_code_4_timeout(isolated_taskq_home):
    """AC-FR-05.3 — single-task timeout returns exit 4.

    Sub-assertion: AC5-exit-4-timeout (``result.exit_code == 4``).
    """
    scenario = "run_sleep_5_with_timeout_1"
    expected_exit = 4
    sleep_seconds = 5
    task_timeout = 1
    task_id = _submit_task(f"sleep {sleep_seconds}")

    elapsed_start = time.monotonic()
    cli_result = _run_cli(
        ["run", task_id], env_overrides={"TASKQ_TASK_TIMEOUT": str(task_timeout)}
    )
    elapsed_seconds = time.monotonic() - elapsed_start

    task_record = _tasks_map(_home()).get(task_id, {})

    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        task_status=task_record.get("status"),
        elapsed_seconds=elapsed_seconds,
        stderr=cli_result.stderr,
    )
    assert scenario == "run_sleep_5_with_timeout_1"
    # AC5-exit-4-timeout: `result.exit_code == 4`
    assert result.exit_code == expected_exit
    # The executor must kill the subprocess well within the budget.
    assert result.elapsed_seconds < task_timeout + 3
    # The task record carries the timeout state for downstream traceability.
    assert result.task_status == "timeout"


# -------------------------------------------------------------------
# FR-05 Case 14 — exit code 5 (dependency cycle) — cycle_a_b_a
# -------------------------------------------------------------------


# Cycle construction: A → B (OK), then mutate A.depends_on = [B.id]
# so the chain becomes B → A → B (a directed cycle). ``run --all``
# performs Kahn topological sort and detects the cycle.
# GREEN TODO: the ``run --all`` (or submit DAG-validation) path must
#   detect a directed cycle in the ``depends_on`` graph and exit with
#   the canonical code 5 (SPEC.md §3 FR-06 + §7).
def test_fr05_exit_code_5_dependency_cycle(isolated_taskq_home):
    """AC-FR-05.3 — a directed cycle in ``depends_on`` returns exit 5.

    Sub-assertion: AC5-exit-5-cycle (``result.exit_code == 5``).
    """
    scenario = "cycle_a_b_a"
    expected_exit = 5
    a_id = _submit_task("echo a")
    b_id = _submit_task("echo b", env_overrides={"TASKQ_TASK_TIMEOUT": "30"})

    # Re-submit b with --after a so the DAG declares the edge a → b.
    # We do this by editing tasks.json directly — submit-time validation
    # would otherwise reject cycles at creation.
    tasks_file = _tasks_path()
    payload = _read_tasks(_home())
    inner = payload.get("tasks")
    if isinstance(inner, dict):
        tasks_dict = dict(inner)
    elif isinstance(payload, dict) and payload:
        tasks_dict = dict(payload)
    else:
        tasks_dict = {}
    a_record = dict(tasks_dict.get(a_id, {}))
    a_record["depends_on"] = [b_id]
    tasks_dict[a_id] = a_record
    if "version" in payload:
        payload["tasks"] = tasks_dict
        tasks_file.write_text(json.dumps(payload), encoding="utf-8")
    else:
        tasks_file.write_text(json.dumps(tasks_dict), encoding="utf-8")

    cli_result = _run_cli(["run", "--all"])
    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        stdout=cli_result.stdout,
        stderr=cli_result.stderr,
    )
    assert scenario == "cycle_a_b_a"
    # AC5-exit-5-cycle: `result.exit_code == 5`
    assert result.exit_code == expected_exit


# -------------------------------------------------------------------
# FR-05 Case 15 — exit code 6 (plugin load failure)
# -------------------------------------------------------------------


# FR-07 enforces an allowlist on plugin specifications; a path-form or
# invalid-regex plugin spec causes the CLI startup-time plugin loader
# to abort, surfacing exit 6 (SPEC.md §7).
# GREEN TODO: the cli startup OR a ``plugins`` subcommand must call the
#   plugin loader with ``$TASKQ_PLUGINS`` and reject invalid specs at
#   load time, producing exit 6.
def test_fr05_exit_code_6_plugin_load_failure(isolated_taskq_home):
    """AC-FR-05.3 — plugin load failure returns exit 6.

    Sub-assertion: AC5-exit-6-plugin (``result.exit_code == 6``).

    Construction: set ``$TASKQ_PLUGINS`` to ``"../evil.py"`` (path form,
    rejected by FR-07 plugin allowlist). Any CLI subcommand that
    triggers plugin loading must then exit with the canonical code 6.
    """
    scenario = "plugins_path_rejected"
    expected_exit = 6
    invalid_spec = "../evil.py"

    # Probe with the ``plugins list`` subcommand (the most direct path
    # to the plugin loader); fall back to ``list`` if the subcommand
    # isn't wired yet.
    primary = _run_cli(
        ["plugins", "list"],
        env_overrides={"TASKQ_PLUGINS": invalid_spec},
    )
    fallback = _run_cli(
        ["list"],
        env_overrides={"TASKQ_PLUGINS": invalid_spec},
    )
    cli_result = primary if primary.exit_code in (6,) else fallback

    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        stdout=cli_result.stdout,
        stderr=cli_result.stderr,
    )
    assert scenario == "plugins_path_rejected"
    # AC5-exit-6-plugin: `result.exit_code == 6`
    assert result.exit_code == expected_exit
    # The loader rejection must be visible to the operator.
    assert result.stderr.strip() != ""


# -------------------------------------------------------------------
# FR-05 Case 16 — exit code 1 (internal error) — corrupt_tasks_json
# -------------------------------------------------------------------


# AC-FR-05.3 row "internal error" + AC5-corrupt-store-detected.
# GREEN TODO: ``taskq_plus.storage.task_store`` and / or the cli
#   startup must catch ``json.JSONDecodeError`` raised when reading a
#   malformed ``tasks.json`` and return / surface exit 1 (not let the
#   interpreter die with a traceback, exit 2 from click, or similar).
def test_fr05_exit_code_1_internal_error(isolated_taskq_home):
    """AC-FR-05.3 — corrupt ``tasks.json`` returns exit 1.

    Sub-assertion: AC5-exit-1-internal (``result.exit_code == 1``).
    """
    scenario = "corrupt_tasks_json"
    expected_exit = 1
    # Write a syntactically invalid tasks.json so any reader raises
    # ``json.JSONDecodeError`` on access.
    _tasks_path().write_text("{not valid json,,,", encoding="utf-8")

    # Any subcommand that opens tasks.json will do; ``list`` is the
    # simplest one with no required positional args.
    cli_result = _run_cli(["list"])
    text = cli_result.stdout + cli_result.stderr
    detected_corruption = (
        "json" in text.lower()
        or "decode" in text.lower()
        or "parse" in text.lower()
        or "corrupt" in text.lower()
    )

    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        stdout=cli_result.stdout,
        stderr=cli_result.stderr,
        detected_corruption=detected_corruption,
    )
    assert scenario == "corrupt_tasks_json"
    # AC5-exit-1-internal: `result.exit_code == 1`
    assert result.exit_code == expected_exit
    # AC5-corrupt-store-detected: `result.detected_corruption == True`
    assert result.detected_corruption is True
