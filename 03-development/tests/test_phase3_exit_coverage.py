"""Surgical coverage tests for the residual gap between Gate 2 and Phase 3 exit.

Phase-3 exit `advance-phase` requires 100% whole-project coverage on
`03-development/src`. After Gate 2 (composite 95.65) the only uncovered
lines were small branches whose behaviour is well-defined: this file
exercises them with minimal in-process calls — no re-implementation,
no new fixtures. Each test maps to exactly one uncovered line/branch.

Uncovered lines before this file:
  - 03-development/src/taskq_plus/cli/commands.py  137, 183, 189-191
  - 03-development/src/taskq_plus/cli/main.py      273-275
  - 03-development/src/taskq_plus/service/executor.py
        167-173, 185-196, 366, 371, 389-390
  - 03-development/src/taskq_plus/storage/breaker_store.py  79-80, 83
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

# Make src/ importable (mirrors the rest of the test suite).
TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

HOME_VAR = "TASKQ_HOME"


@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Function-scoped TASKQ_HOME — fresh per test, no leakage."""
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv(HOME_VAR, str(home))
    # Deterministic breaker / DAG defaults so unrelated paths don't trip.
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "99")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "300")
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "10")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "32")
    return home


# ---------------------------------------------------------------------------
# cli/commands.py:137  — _tasks_by_id skips records without an `id`.
# ---------------------------------------------------------------------------
def test_tasks_by_id_skips_record_without_id(taskq_home):
    """A malformed tasks.json entry lacking `id` must be skipped, not crash."""
    from taskq_plus.cli import commands as cmd

    (taskq_home / "tasks.json").write_text(
        json.dumps([{"command": "orphan-no-id"}, {"id": "abcd1234", "command": "ok"}]),
        encoding="utf-8",
    )
    by_id = cmd._tasks_by_id()
    assert by_id == {"abcd1234": {"id": "abcd1234", "command": "ok"}}


# ---------------------------------------------------------------------------
# cli/commands.py:183  — _redact string branch replaces NFR-04 secret.
# ---------------------------------------------------------------------------
def test_redact_string_substitutes_secret_pattern():
    """`_redact(str)` returns a copy with each NFR-04 pattern replaced."""
    from taskq_plus.cli import commands as cmd

    out = cmd._redact("token=abc123def456ghi789")
    assert "[REDACTED]" in out and "abc123def456ghi789" not in out
    assert "[REDACTED]" in cmd._redact("Bearer xyzzy12345")
    assert "[REDACTED]" in cmd._redact("sk-abcdefghijklmnop")


# ---------------------------------------------------------------------------
# cli/commands.py:189-191 — _redact_task copies + redacts the `command` field.
# ---------------------------------------------------------------------------
def test_redact_task_replaces_command_and_keeps_other_fields():
    """`_redact_task` shallow-copies the dict and redacts only `command`."""
    from taskq_plus.cli import commands as cmd

    src = {"id": "abcd1234", "command": "token=abc123def456", "status": "done"}
    out = cmd._redact_task(src)
    assert out is not src, "_redact_task must return a fresh dict"
    assert out["id"] == "abcd1234"
    assert out["status"] == "done"
    assert "[REDACTED]" in out["command"]
    # Original is untouched (no in-place mutation).
    assert "token=abc123def456" in src["command"]


# ---------------------------------------------------------------------------
# cli/main.py:273-275 — `status <id>` non-JSON mode prints `id status`.
# ---------------------------------------------------------------------------
def test_status_non_json_writes_id_and_status(taskq_home):
    """Without `--json`, `status` prints one line of `id status`."""
    from taskq_plus.cli.main import main as cli_main_fn

    assert cli_main_fn(["submit", "echo red-status-coverage"]) == 0
    recs = json.loads((taskq_home / "tasks.json").read_text(encoding="utf-8"))
    real_id = recs[0]["id"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main_fn(["status", real_id])
    assert rc == 0
    rendered = buf.getvalue().strip()
    assert rendered.startswith(real_id), rendered
    assert rendered.endswith(" pending"), rendered


# ---------------------------------------------------------------------------
# service/executor.py:167-173 — _emit_audit appends to audit.log.
# ---------------------------------------------------------------------------
def test_emit_audit_writes_legacy_audit_log(taskq_home):
    """`_emit_audit` appends one JSONL line to `$TASKQ_HOME/audit.log`."""
    from taskq_plus.service import executor as exec_mod

    exec_mod._emit_audit("test_event", {"foo": "bar"})
    log = taskq_home / "audit.log"
    assert log.exists()
    line = log.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["event"] == "test_event"
    assert rec["foo"] == "bar"
    assert "ts" in rec


# ---------------------------------------------------------------------------
# service/executor.py:185-196 — _emit_plugin_errors writes legacy + journal.
# ---------------------------------------------------------------------------
def test_emit_plugin_errors_writes_audit_and_journal(taskq_home, monkeypatch):
    """Each PluginFailure yields one legacy `plugin_error` log + one journal event."""
    from taskq_plus.service import executor as exec_mod
    from taskq_plus.service.plugins import PluginFailure

    monkeypatch.setenv("TASKQ_AUDIT_LOG", str(taskq_home / "audit.jsonl"))

    failures = [PluginFailure(hook="pre_run", plugin="cov_plugin", error="boom")]
    exec_mod._emit_plugin_errors(failures, task_id="deadbeef")

    legacy = (taskq_home / "audit.log").read_text(encoding="utf-8")
    assert "plugin_error" in legacy
    assert "deadbeef" in legacy
    assert "cov_plugin" in legacy

    journal = (taskq_home / "audit.jsonl").read_text(encoding="utf-8")
    assert "plugin_error" in journal


# ---------------------------------------------------------------------------
# service/executor.py:366, 371 — _unmet_dependencies edge cases.
# ---------------------------------------------------------------------------
def test_unmet_dependencies_returns_empty_for_missing_task(taskq_home):
    """`_unmet_dependencies` returns [] when the task itself does not exist."""
    from taskq_plus.service import executor as exec_mod
    from taskq_plus.storage.task_store import save_tasks

    save_tasks([{"id": "real0001", "depends_on": [], "status": "pending"}])
    assert exec_mod._unmet_dependencies("does-not-exist") == []


def test_unmet_dependencies_lists_pending_parent(taskq_home):
    """`_unmet_dependencies` returns parent ids whose status != 'done'."""
    from taskq_plus.service import executor as exec_mod
    from taskq_plus.storage.task_store import save_tasks

    save_tasks([
        {"id": "parent01", "depends_on": [], "status": "pending"},
        {"id": "child001", "depends_on": ["parent01"], "status": "pending"},
    ])
    assert exec_mod._unmet_dependencies("child001") == ["parent01"]


# ---------------------------------------------------------------------------
# service/executor.py:389-390 — _execute_or_block marks blocked + returns None.
# ---------------------------------------------------------------------------
def test_execute_or_block_marks_unmet_dep_blocked(taskq_home):
    """A task whose dependency is not `done` is marked `blocked`, not executed."""
    from taskq_plus.service import executor as exec_mod
    from taskq_plus.storage.task_store import save_tasks, find_by_id

    save_tasks([
        {"id": "parent01", "depends_on": [], "status": "pending"},
        {"id": "child001", "depends_on": ["parent01"], "status": "pending"},
    ])
    rc = exec_mod._execute_or_block("child001")
    assert rc is None
    rec = find_by_id("child001")
    assert rec is not None and rec["status"] == "blocked"


# ---------------------------------------------------------------------------
# storage/breaker_store.py:79-80 — JSONDecodeError / OSError → None.
# ---------------------------------------------------------------------------
def test_read_breaker_returns_none_on_corrupt_json(taskq_home):
    """`read_breaker` returns None (not raises) when breaker.json is corrupt."""
    from taskq_plus.storage import breaker_store as bs

    (taskq_home / "breaker.json").write_text("not valid json {", encoding="utf-8")
    assert bs.read_breaker() is None


# ---------------------------------------------------------------------------
# storage/breaker_store.py:83 — non-dict JSON payload → None.
# ---------------------------------------------------------------------------
def test_read_breaker_returns_none_when_payload_is_list(taskq_home):
    """`read_breaker` returns None when the persisted JSON is not a dict."""
    from taskq_plus.storage import breaker_store as bs

    (taskq_home / "breaker.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert bs.read_breaker() is None


# ---------------------------------------------------------------------------
# taskq_plus.config — NFR-06 independence layer (SAB contract).
# ---------------------------------------------------------------------------
def test_config_resolve_home_prefers_env_then_default(monkeypatch, tmp_path):
    """`resolve_home` reads `$TASKQ_HOME`, falling back to `default`."""
    from taskq_plus import config

    default = tmp_path / "fallback_home"
    monkeypatch.delenv("TASKQ_HOME", raising=False)
    assert config.resolve_home(default=default) == default.resolve()

    monkeypatch.setenv("TASKQ_HOME", str(tmp_path / "env_home"))
    assert config.resolve_home(default=default) == (tmp_path / "env_home").resolve()


def test_config_load_settings_returns_expected_keys(monkeypatch):
    """`load_settings` returns the documented env-derived dict."""
    from taskq_plus import config

    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "7.5")
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "2")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "4")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "11")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "9")
    settings = config.load_settings()
    assert settings["task_timeout"] == 7.5
    assert settings["retry_limit"] == 2
    assert settings["breaker_threshold"] == 4
    assert settings["breaker_cooldown"] == 11.0
    assert settings["max_dag_depth"] == 9


# ---------------------------------------------------------------------------
# taskq_plus.storage.atomic — NFR-03 atomic write helpers.
# ---------------------------------------------------------------------------
def test_atomic_write_then_read_roundtrip(tmp_path):
    """`write_json_atomic` then `read_json` round-trips the payload."""
    from taskq_plus.storage import atomic

    target = tmp_path / "store.json"
    atomic.write_json_atomic(target, {"hello": "world"})
    assert atomic.read_json(target) == {"hello": "world"}


def test_atomic_read_returns_none_for_missing_or_corrupt(tmp_path):
    """`read_json` returns None on missing / corrupt / undecodable input."""
    from taskq_plus.storage import atomic

    missing = tmp_path / "absent.json"
    assert atomic.read_json(missing) is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not json {", encoding="utf-8")
    assert atomic.read_json(corrupt) is None


def test_atomic_append_jsonl_and_read_jsonl(tmp_path):
    """`append_jsonl` appends one JSON line; `read_jsonl` parses them back."""
    from taskq_plus.storage import atomic

    target = tmp_path / "audit.jsonl"
    atomic.append_jsonl(target, {"event": "x", "i": 1})
    atomic.append_jsonl(target, {"event": "y", "i": 2})
    lines = atomic.read_jsonl(target)
    assert lines == [{"event": "x", "i": 1}, {"event": "y", "i": 2}]


def test_atomic_read_jsonl_returns_empty_for_missing(tmp_path):
    """`read_jsonl` returns [] when the journal file does not exist."""
    from taskq_plus.storage import atomic

    assert atomic.read_jsonl(tmp_path / "nope.jsonl") == []


def test_atomic_read_jsonl_skips_blank_lines(tmp_path):
    """`read_jsonl` ignores blank lines between records."""
    from taskq_plus.storage import atomic

    target = tmp_path / "audit.jsonl"
    target.write_text(
        '{"event": "x"}\n\n   \n{"event": "y"}\n',
        encoding="utf-8",
    )
    assert atomic.read_jsonl(target) == [{"event": "x"}, {"event": "y"}]


def test_atomic_write_cleans_tmp_on_failure(tmp_path, monkeypatch):
    """A failed write removes its tmp file so `os.replace` is never partial."""
    from taskq_plus.storage import atomic

    target = tmp_path / "store.json"
    # Force json.dump to raise after the tmp file has been created.
    import taskq_plus.storage.atomic as atomic_mod
    def boom(*a, **kw):
        raise RuntimeError("simulated dump failure")
    monkeypatch.setattr(atomic_mod.json, "dump", boom)
    with pytest.raises(RuntimeError):
        atomic.write_json_atomic(target, {"x": 1})
    # No leftover .tmp files in the directory.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("store.json.") and p.suffix == ".tmp"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# taskq_plus.models.errors — typed exception classes for exit-code mapping.
# ---------------------------------------------------------------------------
def test_models_errors_are_instantiable_and_carry_field():
    """Each error class accepts a message (and `field` for ValidationRejected)."""
    from taskq_plus.models import errors

    assert isinstance(errors.ValidationRejected("bad", field="x"), errors.TaskQError)
    assert errors.ValidationRejected("bad", field="x").field == "x"
    for cls in (
        errors.TaskNotFound,
        errors.DagDepthExceeded,
        errors.BreakerOpen,
        errors.TaskTimeout,
        errors.PluginLoadFailed,
        errors.StoreCorrupted,
    ):
        assert isinstance(cls("oops"), errors.TaskQError)
        assert str(cls("oops")) == "oops"