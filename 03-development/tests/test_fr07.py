"""FR-07 RED tests — Plugin Hook 系統 (allowlist named import + exception
isolation + 3-strikes disable).

Per TEST_SPEC.md §FR-07 there are 6 case rows collapsed onto 5 test functions
(rows 1 and 2 share the function name `test_fr07_a`, so they are two pytest
parametrize ids on that one function — TEST_SPEC.md shape rules v2.13.0
"multi-scenario split", one parametrize id per row):

  - test_fr07_a  (plugin_name="../evil.py"            → rejected, exit 6)  row 1
                 (plugin_name="https://evil.example/x.py" → rejected, 6)  row 2
  - test_fr07_b  (pre_run raises → task completes + `plugin_error` audit)  row 3
  - test_fr07_c  (3 consecutive failures → plugin disabled for this run)   row 4
  - test_fr07_d  (`plugins list` → module name + hooks + load status)      row 5
  - test_fr07_e  (grep "eval(\\|exec(" over 03-development/src/ → 0 hits)   row 6

Sub-assertions (rule_id → predicate):
  AC7-path-form-rejected     : "/" in plugin_name or ".." in plugin_name    row 1
  AC7-url-form-rejected      : "://" in plugin_name or startswith("http")   row 2
  AC7-name-rejection-flag    : plugin_name_expected_rejected == "true"    rows 1,2
  AC7-plugin-error-isolates  : task_should_complete == "true" and
                               audit_event_kind == "plugin_error"           row 3
  AC7-three-strikes-disable  : consecutive_failure_count == "3" and
                               expected_disabled_for_this_run == "true"     row 4
  AC7-plugin-list-shape      : expected_load_status == "loaded" and
                               len(expected_hook_list.split(",")) >= 1      row 5
  AC7-grep-target-nonempty   : len(grep_pattern) > 0 and
                               len(src_dir_relpath) > 0                     row 6

Properties: Direction B NOT applicable for FR-07 (TEST_SPEC.md §FR-07 —
plugin loading is a security gate, not an algebraic invariant over inputs).

Security traceability: SEC T-05 (plugin RCE) declares
`verified_by: test_fr07_a` — the path-form / URL-form rejection cases below
ARE the threat's verifying tests (TEST_SPEC.md §Step 1c).

SAB-bindings (FR-07 binds to, per SAB.json fr_module_traceability.FR-07):
  - taskq_plus.service.plugins  (does NOT exist on disk — RED)

This file is the TDD-RED deliverable: it is EXPECTED to fail with a pytest
Collection Error (Exit Code 2) because `taskq_plus/service/plugins.py` is
absent and the public-API symbols the GREEN TODOs name do not exist. Do NOT
wrap these imports in try/except ImportError — the crash IS the RED signal.

State isolation (TEST_SPEC.md row 4 `state_mode="isolate_per_test"`):
the 3-strike counter lives inside a single run's dispatch state
(`PluginDispatchResult.disabled` / per-`LoadedPlugin` failure counter) and
must NOT bleed across tests — every fixture below is function-scoped and
every test loads its plugins freshly.

In-process vs out-of-process (explicit choice, per v2.13.0 integration rules):
  * Each spec-named test asserts the REAL user-facing entry point out of
    process (`subprocess.run([sys.executable, "-m", "taskq_plus", ...])`,
    with PYTHONPATH propagated to the child — pytest's sys.path edits do NOT
    reach a child process) AND the same behaviour in process through the
    SAB-declared module `taskq_plus.service.plugins`, so pytest-cov can
    actually measure that module (a subprocess is invisible to coverage).
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
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
ROOT = TESTS_DIR.parent                 # 03-development/
SRC = ROOT / "src"
REPO_ROOT = ROOT.parent                 # project root (holds 03-development/)
HOME_VAR = "TASKQ_HOME"
PLUGINS_VAR = "TASKQ_PLUGINS"
PLUGIN_LOG_VAR = "TASKQ_TEST_PLUGIN_LOG"

# Make src/ importable for the in-process tests. Subprocess tests do NOT rely
# on this — they propagate PYTHONPATH explicitly through the child env.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ---------------------------------------------------------------------------
# SAB-bound imports — every line below deliberately fails in the RED state.
#
# GREEN TODO: create `03-development/src/taskq_plus/service/plugins.py`
# (SAB.json fr_module_traceability."FR-07" = ["taskq_plus.service.plugins"];
# SAD.md §2 L4 `service/plugins.py`) exporting:
#
#   - `PLUGIN_NAME_RE`                                : compiled regex whose
#     `.pattern` is `^[A-Za-z_][A-Za-z0-9_.]*$` (SPEC §3 FR-07 iron rule).
#     A path form (`../evil.py`) or URL form (`https://…`) must NOT match.
#
#   - `MAX_CONSECUTIVE_FAILURES: int = 3`             : the 3-strikes ceiling
#     after which a plugin is disabled for the remainder of THIS run.
#
#   - `load_plugins(settings=None) -> list[LoadedPlugin]`
#     : reads the comma-separated allowlist from `settings` when given, else
#       from `os.environ["TASKQ_PLUGINS"]`; drops empty entries; validates
#       every name against `PLUGIN_NAME_RE`; imports each accepted name with
#       `importlib.import_module` (NEVER `eval`/`exec`/`__import__` of a
#       dynamic string, NEVER a file path or URL). An illegal name — or a
#       module that will not import — raises `PluginLoadError` (exit 6).
#       Each returned `LoadedPlugin` exposes at least `.name` (module name),
#       `.hooks` (list of the registered hook names present on the module,
#       in `pre_run, post_run` order) and `.status` (== "loaded").
#
#   - `dispatch(hook: str, plugins, *args) -> PluginDispatchResult`
#     : calls `getattr(plugin_module, hook)(*args)` for every NOT-disabled
#       plugin that registers `hook`; catches `Exception` from plugin code
#       and NEVER re-raises (task execution must continue); accumulates a
#       `PluginFailure(hook=…, plugin=…, error=…)` per caught exception;
#       counts CONSECUTIVE failures per plugin across the calls made with the
#       same `plugins` list (one run) and, on the 3rd, marks that plugin
#       disabled so later dispatches skip it. Performs NO I/O — the caller
#       (`cli.commands`) turns `.failures` into `plugin_error` audit events.
#
#   - `describe(plugins) -> list[dict]`               : one dict per plugin
#     with at least the keys `name`, `hooks`, `status` — the payload behind
#     `plugins list` (FR-07 output requirement).
#
#   - `PluginFailure` dataclass  {hook, plugin, error}
#   - `PluginDispatchResult` dataclass {disabled, failures}
#   - `PluginLoadError`                               : exit-code-6 exception
#     (may be re-exported from `taskq_plus.models.errors`).
#
# GREEN TODO: `taskq_plus.cli.commands.plugins_cmd` must delegate to
# `service.plugins.load_plugins` + `describe` (rather than its current inline
# regex) so the CLI reports hooks + load status, and must emit one
# `plugin_error` audit event per `PluginFailure` returned by the executor.
# Do NOT add stubs to source files from this RED step — GREEN does that.
# ---------------------------------------------------------------------------
from taskq_plus.service.plugins import (  # noqa: E402,F401
    MAX_CONSECUTIVE_FAILURES,
    PLUGIN_NAME_RE,
    PluginDispatchResult,
    PluginFailure,
    PluginLoadError,
    describe,
    dispatch,
    load_plugins,
)

from taskq_plus.cli.main import main as cli_main  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Plugin module sources written into a temp dir per test (never onto the
# project tree). They record every hook invocation into $TASKQ_TEST_PLUGIN_LOG
# so both the in-process and the out-of-process tests can count calls.
# ---------------------------------------------------------------------------
_LOG_HELPER = '''
import os


def _log(entry):
    path = os.environ.get("TASKQ_TEST_PLUGIN_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry + "\\n")
'''

GOOD_PLUGIN_SRC = _LOG_HELPER + '''

def pre_run(task):
    _log("pre_run")


def post_run(task, result):
    _log("post_run")
'''

FAILING_PLUGIN_SRC = _LOG_HELPER + '''

def pre_run(task):
    _log("pre_run")
    raise RuntimeError("plugin exploded in pre_run")


def post_run(task, result):
    _log("post_run")
'''


# ---------------------------------------------------------------------------
# Function-scoped fixtures — TEST_SPEC row 4 declares
# `state_mode="isolate_per_test"`, so no state (TASKQ_HOME, plugin dir,
# invocation log, imported plugin module) may survive a test.
# ---------------------------------------------------------------------------
@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Fresh $TASKQ_HOME + deterministic executor knobs for one test."""
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv(HOME_VAR, str(home))
    monkeypatch.setenv("TASKQ_AUDIT_LOG", str(home / "audit.jsonl"))
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "99")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "300")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "10")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "32")
    monkeypatch.delenv(PLUGINS_VAR, raising=False)
    return home


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    """A temp directory on sys.path where test plugin modules are written."""
    directory = tmp_path / "plugin_pkgs"
    directory.mkdir()
    monkeypatch.syspath_prepend(str(directory))
    importlib.invalidate_caches()
    return directory


@pytest.fixture
def plugin_log(tmp_path, monkeypatch):
    """Path of the hook-invocation journal written by the test plugins."""
    journal = tmp_path / "plugin_calls.log"
    monkeypatch.setenv(PLUGIN_LOG_VAR, str(journal))
    return journal


@pytest.fixture
def install_plugin(plugin_dir, monkeypatch):
    """Write a plugin module into `plugin_dir` and register the env allowlist."""

    def _install(module_name: str, source: str, *, allowlist: bool = True):
        (plugin_dir / f"{module_name}.py").write_text(source, encoding="utf-8")
        importlib.invalidate_caches()
        # Never let a previous test's copy of the module satisfy the import.
        sys.modules.pop(module_name, None)
        if allowlist:
            monkeypatch.setenv(PLUGINS_VAR, module_name)
        return module_name

    return _install


# ---------------------------------------------------------------------------
# Out-of-process helper — the REAL user-facing entry point.
# PYTHONPATH must be propagated explicitly: pytest's sys.path manipulation and
# setup.cfg `pythonpath` do NOT reach a child process.
# ---------------------------------------------------------------------------
def _run_cli(argv, home, *, plugins_value=None, plugin_path=None,
             plugin_log_path=None):
    """Run `python -m taskq_plus <argv>` out of process; return (code, out, err)."""
    env = os.environ.copy()
    env[HOME_VAR] = str(home)
    env["TASKQ_AUDIT_LOG"] = str(Path(home) / "audit.jsonl")
    env["TASKQ_RETRY_LIMIT"] = "0"
    env["TASKQ_BACKOFF_BASE"] = "0"
    env["TASKQ_BREAKER_THRESHOLD"] = "99"
    env["TASKQ_TASK_TIMEOUT"] = "10"
    search_path = [str(SRC)]
    if plugin_path is not None:
        search_path.insert(0, str(plugin_path))
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in search_path + [inherited] if p]
    )
    if plugins_value is None:
        env.pop(PLUGINS_VAR, None)
    else:
        env[PLUGINS_VAR] = plugins_value
    if plugin_log_path is not None:
        env[PLUGIN_LOG_VAR] = str(plugin_log_path)
    proc = subprocess.run(
        [sys.executable, "-m", "taskq_plus", *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _main_capture(argv):
    """Run the CLI IN PROCESS so pytest-cov can measure the handlers."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            code = cli_main(list(argv))
        except SystemExit as exc:  # click standalone-mode escape hatch
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out_buf.getvalue(), err_buf.getvalue()


def _audit_events(home):
    """Parse the FR-08 audit journal (`audit.jsonl`) into a list of dicts."""
    events = []
    for filename in ("audit.jsonl", "audit.log"):
        journal = Path(home) / filename
        if not journal.exists():
            continue
        for line in journal.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                events.append(json.loads(stripped))
            except ValueError:
                events.append({"event": stripped, "raw": stripped})
    return events


def _event_kinds(events):
    kinds = []
    for evt in events:
        for key in ("event", "kind", "type", "name"):
            value = evt.get(key)
            if isinstance(value, str):
                kinds.append(value)
    return kinds


def _hook_calls(journal):
    if not journal.exists():
        return []
    return [ln.strip() for ln in journal.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ===========================================================================
# Row 1 + Row 2 — AC-FR-07.a: illegal plugin names are rejected with exit 6.
# ===========================================================================
@pytest.mark.parametrize(
    "plugin_name,plugin_name_expected_rejected",
    [
        pytest.param("../evil.py", "true", id="path_form"),           # row 1
        pytest.param("https://evil.example/x.py", "true", id="url_form"),  # row 2
    ],
)
def test_fr07_a(taskq_home, plugin_name, plugin_name_expected_rejected,
                monkeypatch):
    """`TASKQ_PLUGINS="<path|url>" plugins list` → exit 6 (SPEC §8 #12, T-05).

    Rows 1 + 2 of TEST_SPEC.md §FR-07. Path forms and URL forms are NOT
    installed module names, so `PLUGIN_NAME_RE` must reject them BEFORE any
    import is attempted — the whole point of the FR-07 allowlist iron rule.
    """
    # rule_id: AC7-name-rejection-flag (rows 1, 2)
    assert plugin_name_expected_rejected == "true"

    # rule_id: AC7-path-form-rejected (row 1) / AC7-url-form-rejected (row 2)
    is_path_form = "/" in plugin_name or ".." in plugin_name
    is_url_form = "://" in plugin_name or plugin_name.startswith("http")
    assert is_path_form or is_url_form, (
        f"case input {plugin_name!r} must be a path form or a URL form"
    )

    # The regex itself is the security gate (SPEC §3 FR-07 iron rule).
    assert PLUGIN_NAME_RE.pattern == r"^[A-Za-z_][A-Za-z0-9_.]*$", (
        f"FR-07 plugin-name regex must be the SPEC pattern, "
        f"got {PLUGIN_NAME_RE.pattern!r}"
    )
    assert not PLUGIN_NAME_RE.match(plugin_name), (
        f"{plugin_name!r} must NOT match the allowlist regex"
    )

    # In process (coverage-visible): load_plugins refuses the name with the
    # exit-6 exception rather than importing anything.
    monkeypatch.setenv(PLUGINS_VAR, plugin_name)
    with pytest.raises(PluginLoadError) as excinfo:
        load_plugins()
    assert getattr(excinfo.value, "exit_code", 6) == 6, (
        "PluginLoadError must carry exit code 6 (SPEC §7)"
    )

    # In process, through the CLI handler — `plugins list` must exit 6.
    code_inproc, _out_inproc, _err_inproc = _main_capture(["plugins", "list"])
    assert code_inproc == 6, (
        f"in-process `plugins list` with TASKQ_PLUGINS={plugin_name!r} must "
        f"exit 6, got {code_inproc}"
    )

    # Out of process — the REAL user-facing acceptance command (SPEC §8 #12).
    code, out, err = _run_cli(
        ["plugins", "list"], taskq_home, plugins_value=plugin_name
    )
    assert code == 6, (
        f"`TASKQ_PLUGINS={plugin_name!r} python -m taskq_plus plugins list` "
        f"must exit 6, got {code}; stdout={out!r} stderr={err!r}"
    )


# ===========================================================================
# Row 3 — AC-FR-07.b: a raising hook must not interrupt task execution;
# a `plugin_error` audit event is recorded and the task still completes.
# ===========================================================================
def test_fr07_b(taskq_home, install_plugin, plugin_dir, plugin_log):
    """pre_run raises → task completes; audit journal gets `plugin_error`.

    Row 3 of TEST_SPEC.md §FR-07 (SPEC §8 #13).
    Inputs: hook_name="pre_run", plugin_raises_in_hook="true",
            task_should_complete="true", audit_event_kind="plugin_error".
    """
    hook_name = "pre_run"
    plugin_raises_in_hook = "true"
    task_should_complete = "true"
    audit_event_kind = "plugin_error"

    # rule_id: AC7-plugin-error-isolates (row 3)
    assert task_should_complete == "true" and audit_event_kind == "plugin_error"
    assert plugin_raises_in_hook == "true"

    module_name = install_plugin("fr07_failing_plugin", FAILING_PLUGIN_SRC)

    # --- in process (coverage-visible): dispatch isolates the exception ---
    loaded = load_plugins()
    assert [p.name for p in loaded] == [module_name]
    result = dispatch(hook_name, loaded, {"id": "abcdef12", "command": "echo hi"})
    assert isinstance(result, PluginDispatchResult), (
        "dispatch must return a PluginDispatchResult (SAD §2 L4 plugins.py)"
    )
    assert len(result.failures) == 1, (
        f"a raising {hook_name} must yield exactly one PluginFailure, "
        f"got {result.failures!r}"
    )
    failure = result.failures[0]
    assert isinstance(failure, PluginFailure)
    assert failure.hook == hook_name
    assert failure.plugin == module_name
    assert str(failure.error), "PluginFailure.error must carry the plugin message"
    # One failure is NOT three — the plugin stays enabled for this run.
    assert module_name not in list(result.disabled), (
        "a single failure must not disable the plugin (3-strikes rule)"
    )
    # dispatch performs no I/O: the audit event is the caller's job (SAD §2.3).
    assert _hook_calls(plugin_log) == [hook_name], (
        "the failing hook must actually have been invoked once"
    )

    # --- out of process: the task still completes end to end ---
    code_submit, out_submit, err_submit = _run_cli(
        ["submit", "echo fr07"], taskq_home,
        plugins_value=module_name, plugin_path=plugin_dir,
        plugin_log_path=plugin_log,
    )
    assert code_submit == 0, (
        f"submit must succeed, got {code_submit}; stderr={err_submit!r}"
    )
    task_id = out_submit.strip().splitlines()[-1].strip()

    code_run, out_run, err_run = _run_cli(
        ["run", task_id], taskq_home,
        plugins_value=module_name, plugin_path=plugin_dir,
        plugin_log_path=plugin_log,
    )
    assert code_run == 0, (
        f"a raising {hook_name} must NOT interrupt the task: expected exit 0, "
        f"got {code_run}; stdout={out_run!r} stderr={err_run!r}"
    )

    code_status, out_status, _err_status = _run_cli(
        ["status", task_id], taskq_home
    )
    assert code_status == 0
    assert "done" in out_status, (
        f"task {task_id} must have completed despite the plugin error, "
        f"got status output {out_status!r}"
    )

    kinds = _event_kinds(_audit_events(taskq_home))
    assert audit_event_kind in kinds, (
        f"audit journal must contain a {audit_event_kind!r} event (FR-08), "
        f"got kinds={kinds!r}"
    )


# ===========================================================================
# Row 4 — AC-FR-07.c: 3 consecutive failures inside ONE run disable the plugin.
# state_mode = isolate_per_test → fresh load_plugins()/dispatch state here.
# ===========================================================================
def test_fr07_c(taskq_home, install_plugin, plugin_log):
    """A plugin failing 3 consecutive times in one run is disabled for that run.

    Row 4 of TEST_SPEC.md §FR-07 (DERIVED from SPEC §3 FR-07
    "連續 3 次失敗的 plugin 於本次執行內停用").
    Inputs: consecutive_failure_count="3",
            expected_disabled_for_this_run="true",
            state_mode="isolate_per_test".
    """
    consecutive_failure_count = "3"
    expected_disabled_for_this_run = "true"

    # rule_id: AC7-three-strikes-disable (row 4)
    assert (
        consecutive_failure_count == "3"
        and expected_disabled_for_this_run == "true"
    )
    assert MAX_CONSECUTIVE_FAILURES == 3, (
        f"FR-07 fixes the strike ceiling at 3, got {MAX_CONSECUTIVE_FAILURES}"
    )

    module_name = install_plugin("fr07_striking_plugin", FAILING_PLUGIN_SRC)

    # ONE run == one `plugins` list threaded through consecutive dispatches.
    loaded = load_plugins()
    strikes = int(consecutive_failure_count)
    last_result = None
    for attempt in range(1, strikes + 1):
        last_result = dispatch(
            "pre_run", loaded, {"id": f"task{attempt:04d}", "command": "echo x"}
        )
        assert last_result.failures, (
            f"attempt {attempt} must record a PluginFailure"
        )
        if attempt < strikes:
            assert module_name not in list(last_result.disabled), (
                f"plugin must stay enabled after {attempt} failure(s), "
                f"disabled={list(last_result.disabled)!r}"
            )

    assert module_name in list(last_result.disabled), (
        f"after {strikes} consecutive failures the plugin must be disabled "
        f"for this run, disabled={list(last_result.disabled)!r}"
    )
    assert len(_hook_calls(plugin_log)) == strikes, (
        f"the hook must have been invoked exactly {strikes} times before "
        f"being disabled, got {_hook_calls(plugin_log)!r}"
    )

    # A 4th dispatch in the SAME run must skip the disabled plugin entirely:
    # no new invocation, no new failure.
    after = dispatch("pre_run", loaded, {"id": "task0004", "command": "echo x"})
    assert len(_hook_calls(plugin_log)) == strikes, (
        "a disabled plugin must NOT be invoked again within the same run"
    )
    assert not after.failures, (
        f"a disabled plugin must produce no further failures, got {after.failures!r}"
    )
    assert module_name in list(after.disabled)


# ===========================================================================
# Row 5 — AC-FR-07.d: `plugins list` prints module name, hooks, load status.
# ===========================================================================
def test_fr07_d(taskq_home, install_plugin, plugin_dir, plugin_log):
    """`plugins list` reports each plugin's module name, hooks and load status.

    Row 5 of TEST_SPEC.md §FR-07 (DERIVED from SPEC §3 FR-07
    "plugins list 輸出每個 plugin 的模組名、註冊的 hook、載入狀態").
    Inputs: plugin_module_name="my_plugin", expected_load_status="loaded",
            expected_hook_list="pre_run,post_run".
    """
    plugin_module_name = "my_plugin"
    expected_load_status = "loaded"
    expected_hook_list = "pre_run,post_run"

    # rule_id: AC7-plugin-list-shape (row 5)
    assert (
        expected_load_status == "loaded"
        and len(expected_hook_list.split(",")) >= 1
    )

    install_plugin(plugin_module_name, GOOD_PLUGIN_SRC)
    expected_hooks = expected_hook_list.split(",")

    # --- in process (coverage-visible) ---
    loaded = load_plugins()
    described = describe(loaded)
    assert isinstance(described, list) and len(described) == 1, (
        f"describe() must return one entry per plugin, got {described!r}"
    )
    entry = described[0]
    assert entry["name"] == plugin_module_name, (
        f"describe() must report the module name, got {entry!r}"
    )
    assert list(entry["hooks"]) == expected_hooks, (
        f"describe() must report the registered hooks {expected_hooks!r}, "
        f"got {entry!r}"
    )
    assert entry["status"] == expected_load_status, (
        f"describe() must report load status {expected_load_status!r}, "
        f"got {entry!r}"
    )
    # Loading must not invoke any hook.
    assert _hook_calls(plugin_log) == [], (
        "load_plugins() must import only — it must not call pre_run/post_run"
    )

    code_inproc, out_inproc, err_inproc = _main_capture(["plugins", "list"])
    assert code_inproc == 0, (
        f"in-process `plugins list` must exit 0, got {code_inproc}; "
        f"stderr={err_inproc!r}"
    )
    for token in [plugin_module_name, expected_load_status, *expected_hooks]:
        assert token in out_inproc, (
            f"in-process `plugins list` output must mention {token!r}, "
            f"got {out_inproc!r}"
        )

    # --- out of process: the real `python -m taskq_plus plugins list` ---
    code, out, err = _run_cli(
        ["plugins", "list"], taskq_home,
        plugins_value=plugin_module_name, plugin_path=plugin_dir,
        plugin_log_path=plugin_log,
    )
    assert code == 0, (
        f"`plugins list` must exit 0 for a legal module name, got {code}; "
        f"stderr={err!r}"
    )
    for token in [plugin_module_name, expected_load_status, *expected_hooks]:
        assert token in out, (
            f"`plugins list` output must mention {token!r}, got {out!r}"
        )

    # The `--json` rendering must carry the same three fields (FR-05 §NFR-10).
    code_json, out_json, err_json = _run_cli(
        ["--json", "plugins", "list"], taskq_home,
        plugins_value=plugin_module_name, plugin_path=plugin_dir,
        plugin_log_path=plugin_log,
    )
    assert code_json == 0, (
        f"`--json plugins list` must exit 0, got {code_json}; stderr={err_json!r}"
    )
    payload = json.loads(out_json.strip().splitlines()[-1])
    listed = payload["plugins"]
    assert listed[0]["name"] == plugin_module_name
    assert list(listed[0]["hooks"]) == expected_hooks
    assert listed[0]["status"] == expected_load_status


# ===========================================================================
# Row 6 — AC-FR-07.e / NFR-02 / SEC: no `eval(` or `exec(` anywhere in src.
# ===========================================================================
def test_fr07_e():
    """`grep -rn "eval(\\|exec(" 03-development/src/` must return 0 hits.

    Row 6 of TEST_SPEC.md §FR-07 (NFR-02 / SPEC §8 #15). The FR-07 loader is
    only safe if dynamic-code execution is absent from the whole source tree,
    so this asserts on the grep result over the SAB-declared src root — not
    just over `service/plugins.py`.
    """
    grep_pattern = r"eval(\|exec("
    src_dir_relpath = "03-development/src/"

    # rule_id: AC7-grep-target-nonempty (row 6)
    assert len(grep_pattern) > 0 and len(src_dir_relpath) > 0

    src_root = REPO_ROOT / src_dir_relpath
    assert src_root.is_dir(), f"grep target {src_dir_relpath!r} must exist"

    # Same alternation as the acceptance grep, expressed as a Python regex.
    scanner = re.compile(r"eval\(|exec\(")
    hits = []
    for py_file in sorted(src_root.rglob("*.py")):
        for lineno, line in enumerate(
            py_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if scanner.search(line):
                hits.append(f"{py_file.relative_to(REPO_ROOT)}:{lineno}:{line.strip()}")

    assert hits == [], (
        "NFR-02 forbids eval(/exec( anywhere under "
        f"{src_dir_relpath}; found {len(hits)} hit(s):\n" + "\n".join(hits)
    )
