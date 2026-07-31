"""Coverage-gap tests — exercise every remaining uncovered source line.

These tests target specific lines that the framework reported as uncovered at
the previous gate (Phase 4 entry: coverage 97.28%, 15 missing source lines).
Each test's docstring cites the exact `file:line` it covers so the gap-closure
audit trail is self-evident in `git blame` / `pytest --cov` output.

Lines covered (Phase 4 gap-closure round 1):

  cli/commands.py:359   _ServicePluginLoadError → PluginLoadError rewrap
  cli/commands.py:362   PluginLoadError(f"plugin load failed: ...") message
  cli/main.py:211-212   submit_cmd → StoreCorrupted → exit 1 + stderr
  cli/main.py:239-240   run_cmd → PluginLoadError → exit 6 + stderr
  cli/main.py:269-270   status_cmd → StoreCorrupted → exit 1 + stderr
  observability/audit.py:209-212  fsync OSError swallowed (audit-write best-effort)
  service/executor.py:455-461     run_all: breaker OPEN branch (emit + stderr + EXIT_BREAKER_OPEN)
  storage/atomic.py:70-72         append_jsonl fsync OSError swallowed (best-effort)

[FR-01] [FR-02] [FR-03] [FR-05] [FR-07] [FR-08] [NFR-03] [NFR-04]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC = ROOT / "src"
HOME_VAR = "TASKQ_HOME"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv(HOME_VAR, str(home))
    return home


def _run_cli(argv, taskq_home_path, *, timeout: int = 30):
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
        timeout=timeout,
    )


# ===========================================================================
# cli/commands.py :359 / :362 — _ServicePluginLoadError → PluginLoadError
# ===========================================================================

def test_run_cmd_rewraps_service_plugin_load_error(taskq_home, monkeypatch):
    """Cover commands.py:359 / :362: when execute_with_cache raises
    service.plugins.PluginLoadError, the CLI rewrap raises the
    cli.commands.PluginLoadError that the click wrapper maps to exit 6.
    """
    from taskq_plus.cli.commands import (
        PluginLoadError,
        submit_cmd,
        run_cmd,
    )
    from taskq_plus.service.plugins import PluginLoadError as _SvcPLE

    # Seed a task so run_cmd has an id to operate on.
    rec = submit_cmd("echo hi")
    task_id = rec["id"]

    # Patch execute_with_cache to raise the SERVICE-level PluginLoadError.
    # The CLI must rewrap it into cli.commands.PluginLoadError so the
    # click wrapper (cli/main.py:238-240) can map it to exit 6.
    from taskq_plus.cli import commands as cli_commands

    def _raise_svc(*args, **kwargs):
        raise _SvcPLE("service-side plugin load failed")

    monkeypatch.setattr(
        "taskq_plus.service.cache.execute_with_cache",
        _raise_svc,
    )
    with pytest.raises(PluginLoadError) as ei:
        run_cmd(task_id=task_id, run_all=False, use_cache=False)
    assert "plugin load failed" in str(ei.value)


# ===========================================================================
# cli/main.py :211-212 / :269-270 — StoreCorrupted → exit 1 + stderr
# ===========================================================================

def test_submit_cmd_store_corrupted_exits_1(taskq_home):
    """Cover main.py:211-212: submit_cmd → commands.StoreCorrupted → exit 1.

    A corrupt tasks.json that slips past `submit`'s lenient loader (the
    FR-01 invariant is that submit is the only lenient path) must surface
    as StoreCorrupted from the strict-loader pre-check when the file is
    syntactically invalid. Call main() in-process so coverage tracks the
    `except StoreCorrupted` branch in cli/main.py (subprocess invocations
    are invisible to pytest-cov).
    """
    from taskq_plus.cli import main as cli_main
    from taskq_plus.cli.main import EXIT_INTERNAL_ERROR

    # Write a syntactically broken tasks.json so commands.StoreCorrupted
    # is raised from submit_cmd.
    (taskq_home / "tasks.json").write_text("not-json{{{", encoding="utf-8")

    rc = cli_main.main(["submit", "echo hi"])
    assert rc == EXIT_INTERNAL_ERROR, (
        f"expected EXIT_INTERNAL_ERROR (1) for StoreCorrupted; got {rc}"
    )


def test_status_cmd_store_corrupted_exits_1(taskq_home):
    """Cover main.py:269-270: status_cmd → commands.StoreCorrupted → exit 1.

    status goes through the strict loader (commands._strict_load_tasks)
    and must surface StoreCorrupted to the click wrapper which emits
    `store corrupted:` and returns EXIT_INTERNAL_ERROR (= 1). In-process
    main() so coverage tracks the branch.
    """
    from taskq_plus.cli import main as cli_main
    from taskq_plus.cli.main import EXIT_INTERNAL_ERROR

    (taskq_home / "tasks.json").write_text("not-json{{{", encoding="utf-8")

    rc = cli_main.main(["status", "deadbeef"])
    assert rc == EXIT_INTERNAL_ERROR, (
        f"expected EXIT_INTERNAL_ERROR (1) for StoreCorrupted; got {rc}"
    )


# ===========================================================================
# cli/main.py :239-240 — run_cmd → PluginLoadError → exit 6 + stderr
# ===========================================================================

def test_run_cmd_plugin_load_error_exits_6(taskq_home):
    """Cover main.py:239-240: run_cmd → cli.PluginLoadError → exit 6."""
    # Patch the CLI dispatch to raise PluginLoadError — the click wrapper
    # in cli/main.py catches it and returns EXIT_PLUGIN_LOAD_FAILED (= 6).
    # We invoke the in-process main() (not a subprocess) so the patch takes
    # effect — subprocess isolation would defeat the monkeypatch.
    from taskq_plus.cli import commands as cli_commands
    from taskq_plus.cli import main as cli_main
    from taskq_plus.cli.main import EXIT_PLUGIN_LOAD_FAILED
    from taskq_plus.cli.commands import PluginLoadError

    def _raise_ple(*args, **kwargs):
        raise PluginLoadError("plugin load failed: simulated")

    real_run_cmd = cli_commands.run_cmd
    cli_commands.run_cmd = _raise_ple  # type: ignore[assignment]
    try:
        rc = cli_main.main(["run", "deadbeef"])
    finally:
        cli_commands.run_cmd = real_run_cmd  # type: ignore[assignment]

    assert rc == EXIT_PLUGIN_LOAD_FAILED, (
        f"expected EXIT_PLUGIN_LOAD_FAILED (6) for PluginLoadError; got {rc}"
    )


# ===========================================================================
# observability/audit.py :209-212 — fsync OSError swallowed (best-effort)
# ===========================================================================

def test_audit_emit_swallows_fsync_oserror(taskq_home):
    """Cover audit.py:209-212: AuditLogger.emit must NOT propagate fsync
    OSError (audit logging is best-effort; a full disk must not crash the
    CLI).  We patch os.fsync to raise so the except-branch is exercised.
    """
    from taskq_plus.observability.audit import AuditLogger

    audit_file = taskq_home / "audit.jsonl"
    logger = AuditLogger(path=audit_file)

    real_fsync = os.fsync
    def _raise_fsync(fd):
        raise OSError("simulated fsync failure")

    with mock.patch.object(os, "fsync", side_effect=_raise_fsync):
        # Must NOT raise — the OSError handler swallows and returns the entry.
        entry = logger.emit(event="submit", task_id="abc", detail={})
    assert entry["event"] == "submit"
    assert entry["task_id"] == "abc"

    # The handler returned BEFORE fsync could be called on a real file,
    # so the file need not exist (or it exists with the partial bytes —
    # either is acceptable per the best-effort contract).
    _ = real_fsync  # silence linter


# ===========================================================================
# service/executor.py :455-461 — run_all breaker OPEN branch
# ===========================================================================

def test_run_all_emits_breaker_open_audit_and_returns_exit3(taskq_home, monkeypatch):
    """Cover executor.py:455-461: when breaker.allow_request() is False,
    run_all must emit a `breaker_open` audit event, print
    BREAKER_OPEN_MESSAGE to stderr, and return EXIT_BREAKER_OPEN (= 3).

    Direct in-process invocation — the CLI's `run --all` routes through
    run_cmd → execute_with_cache → run_all; we call run_all directly
    so the deterministic pre-state (OPEN breaker) holds.
    """
    # Pre-populate a tasks.json with one pending task so run_all has
    # something to do once it gets past the breaker check.
    from taskq_plus.storage.task_store import save_tasks

    save_tasks(
        [
            {
                "id": "12345678",
                "command": "echo breaker-open-test",
                "name": "bot",
                "depends_on": [],
                "status": "pending",
                "created_at": "2026-07-31T00:00:00Z",
            }
        ]
    )

    # Write an OPEN breaker.json with opened_at far in the future so the
    # cooldown hasn't elapsed.
    from taskq_plus.storage.breaker_store import write_breaker

    write_breaker(
        {
            "version": 1,
            "state": "OPEN",
            "failure_count": 99,
            "opened_at": 9_999_999_999.0,  # far future — never cools down
        }
    )

    from taskq_plus.service.executor import run_all, EXIT_BREAKER_OPEN

    # Capture audit emissions via the singleton audit logger.
    from taskq_plus.observability.audit import current_logger

    captured: list[dict] = []
    real_emit = current_logger().emit

    def _capture(event, task_id, detail):
        captured.append({"event": event, "task_id": task_id, "detail": detail})
        return real_emit(event, task_id, detail)

    monkeypatch.setattr(current_logger(), "emit", _capture)

    rc = run_all()
    assert rc == EXIT_BREAKER_OPEN, (
        f"run_all under OPEN breaker must return EXIT_BREAKER_OPEN; got {rc}"
    )
    assert any(c["event"] == "breaker_open" for c in captured), (
        f"run_all must emit a `breaker_open` audit event; got {captured!r}"
    )


# ===========================================================================
# storage/atomic.py :70-72 — append_jsonl fsync OSError swallowed
# ===========================================================================

def test_append_jsonl_swallows_fsync_oserror(taskq_home):
    """Cover atomic.py:70-72: append_jsonl must NOT propagate fsync OSError
    (audit logging is best-effort per NFR-03).
    """
    from taskq_plus.storage import atomic

    path = taskq_home / "audit.jsonl"
    atomic.append_jsonl(path, {"event": "submit", "ts": "2026-07-31T00:00:00Z"})

    # File was written successfully (before the fsync line).
    assert path.exists()
    raw = path.read_text(encoding="utf-8")
    assert "submit" in raw

    # Now patch os.fsync to raise — append_jsonl must swallow.
    def _raise_fsync(fd):
        raise OSError("simulated fsync failure on jsonl append")

    with mock.patch.object(os, "fsync", side_effect=_raise_fsync):
        # Must NOT raise.
        atomic.append_jsonl(path, {"event": "run_start", "ts": "2026-07-31T00:00:01Z"})

    raw2 = path.read_text(encoding="utf-8")
    assert "run_start" in raw2
    assert "simulated fsync failure" not in raw2  # error not persisted