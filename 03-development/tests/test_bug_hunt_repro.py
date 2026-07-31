"""Adversarial bug-hunt reproduction tests (Gate 3 — adversarial_review).

Each test in this module reproduced a CONFIRMED finding of the Step-4b
adversarial hunt in the RED state before the corresponding source fix landed.
They are the anti-fabrication evidence for
`.methodology/bug_hunt_report.json` resolutions.

Findings reproduced here:
  - audit#1      T-04 — `stdout_tail` / `stderr_tail` persisted unredacted.
  - audit#2      T-04 — legacy `audit.log` persisted `command` unredacted.
  - executor#1   FR-03 — `run --all` bypassed the OPEN circuit breaker.
  - executor#2   FR-08 / T-08 — `run --all` emitted no audit events.
  - task_store#1 T-09 — corrupted `tasks.json` silently rebuilt by submit.
  - plugins#1    FR-07 — plugin load failure exited 1 instead of 6.
  - task_store#2 T-07 — concurrent `submit` lost task records.

[FR-02] [FR-03] [FR-07] [FR-08] [NFR-02] [NFR-03] [NFR-04]
Citations:
  - SPEC.md#L214 (NFR-04: `stdout_tail` / `stderr_tail` / audit `detail`
    redacted before the write).
  - SPEC.md#L113 (FR-03: `OPEN` 期間任何 `run` 立即拒絕:exit 3 + stderr
    `breaker open`,不執行 subprocess).
  - SPEC.md#L169 (FR-08 event types incl. `run_start` / `run_end` / `blocked`).
  - SPEC.md#L392 (§7: 損壞的 `tasks.json` → exit 1 + stderr `store corrupted`,
    不靜默重建).
  - SPEC.md#L206 (NFR-03: 資料檔原子寫;並發寫入不得遺失已記錄狀態).
  - SPEC.md#L396 (§7: plugin 名稱非法 / 模組不存在 → exit 6).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC = ROOT / "src"
HOME_VAR = "TASKQ_HOME"
AUDIT_LOG_VAR = "TASKQ_AUDIT_LOG"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SECRET = "sk-ABCDEFGH12345678"


@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Fresh $TASKQ_HOME with deterministic FR-02 / FR-03 knobs."""
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv(HOME_VAR, str(home))
    monkeypatch.setenv(AUDIT_LOG_VAR, str(home / "audit.jsonl"))
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "99")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "300")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "10")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "32")
    monkeypatch.delenv("TASKQ_PLUGINS", raising=False)
    return home


def _cli(argv, home, **env_overrides):
    """Run `python -m taskq_plus <argv>` out of process (the real entry point)."""
    env = os.environ.copy()
    env[HOME_VAR] = str(home)
    env[AUDIT_LOG_VAR] = str(Path(home) / "audit.jsonl")
    env["TASKQ_RETRY_LIMIT"] = "0"
    env["TASKQ_BACKOFF_BASE"] = "0"
    env["TASKQ_TASK_TIMEOUT"] = "10"
    env.setdefault("TASKQ_BREAKER_THRESHOLD", "99")
    env.setdefault("TASKQ_BREAKER_COOLDOWN", "300")
    env.pop("TASKQ_PLUGINS", None)
    env.update({k: str(v) for k, v in env_overrides.items()})
    inherited = os.environ.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SRC), inherited) if p)
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _submit(home, command, **env_overrides):
    """Submit one task via the CLI and return its 8-hex id."""
    proc = _cli(["submit", command], home, **env_overrides)
    assert proc.returncode == 0, f"submit failed: {proc.stderr!r}"
    return proc.stdout.strip()


def _events(home):
    """Parse `audit.jsonl` into a list of event dicts."""
    path = Path(home) / "audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ===========================================================================
# audit#1 — T-04: child stdout/stderr persisted to tasks.json unredacted.
# ===========================================================================
def test_bughunt_audit_1_task_tails_redacted_before_write(taskq_home):  # NFR-04
    """`stdout_tail` on disk must be `[REDACTED]`, not the plaintext secret.

    NFR-04's declared scope is `stdout_tail` / `stderr_tail` / audit `detail`
    (SPEC.md#L214) — NOT the `command` field, which is the submitter's own
    input and must stay verbatim because the executor `shlex.split`s it to
    spawn the child. So this asserts the captured-output fields carry no
    plaintext secret, rather than scanning the whole file.

    Citations: SPEC.md#L214 (`stdout_tail` / `stderr_tail` 落盤前 redact).
    """
    task_id = _submit(taskq_home, f"printf {SECRET}")
    proc = _cli(["run", task_id], taskq_home)
    assert proc.returncode == 0, f"run failed: {proc.stderr!r}"

    record = json.loads((taskq_home / "tasks.json").read_text(encoding="utf-8"))[0]
    assert SECRET not in record["stdout_tail"], (
        "NFR-04: the child's secret-bearing stdout must be redacted BEFORE "
        f"the tasks.json write; found plaintext in {record['stdout_tail']!r}"
    )
    assert SECRET not in record["stderr_tail"]
    assert record["stdout_tail"] == "[REDACTED]", (
        "NFR-04 replaces the whole matching LINE with `[REDACTED]`, got "
        f"{record['stdout_tail']!r}"
    )


# ===========================================================================
# audit#2 — T-04: the legacy audit.log journal wrote `command` unredacted.
# ===========================================================================
def test_bughunt_audit_2_legacy_audit_log_redacted(taskq_home):  # NFR-04
    """`$TASKQ_HOME/audit.log` must not contain a plaintext secret.

    Citations: SPEC.md#L215 (遮蔽發生在寫入前,不是讀取後).
    """
    _submit(taskq_home, f"printf token={SECRET}")

    legacy = taskq_home / "audit.log"
    assert legacy.exists(), "submit must write the legacy audit.log line"
    body = legacy.read_text(encoding="utf-8")
    assert SECRET not in body, (
        f"NFR-04: legacy audit.log persisted a plaintext secret: {body!r}"
    )


# ===========================================================================
# executor#1 — FR-03: `run --all` must honour the OPEN circuit breaker.
# ===========================================================================
def test_bughunt_executor_1_run_all_rejected_while_breaker_open(taskq_home):  # FR-03
    """An OPEN breaker must reject `run --all` with exit 3 and run no task.

    Citations: SPEC.md#L113 (`OPEN` 期間任何 `run` 立即拒絕,不執行 subprocess).
    """
    (taskq_home / "breaker.json").write_text(
        json.dumps(
            {
                "version": 1,
                "state": "OPEN",
                "failure_count": 9,
                "opened_at": 9_999_999_999.0,
            }
        ),
        encoding="utf-8",
    )
    task_id = _submit(taskq_home, "echo should-not-run", TASKQ_BREAKER_THRESHOLD="1")

    proc = _cli(
        ["run", "--all"],
        taskq_home,
        TASKQ_BREAKER_THRESHOLD="1",
        TASKQ_BREAKER_COOLDOWN="300",
    )
    assert proc.returncode == 3, (
        f"`run --all` must exit 3 while the breaker is OPEN, got "
        f"{proc.returncode}; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "breaker open" in proc.stderr, (
        f"SPEC §7 stderr token `breaker open` missing: {proc.stderr!r}"
    )

    record = next(t for t in json.loads((taskq_home / "tasks.json").read_text()))
    assert record["id"] == task_id
    assert record["status"] == "pending", (
        f"no subprocess may run while the breaker is OPEN, got {record!r}"
    )


# ===========================================================================
# executor#2 — FR-08 / T-08: `run --all` must be attributable in the journal.
# ===========================================================================
def test_bughunt_executor_2_run_all_emits_audit_events(taskq_home):  # FR-08
    """The batch path must journal `run_start` / `run_end` per executed task.

    Citations: SPEC.md#L169 (事件種類 incl. run_start / run_end / blocked).
    """
    parent = _submit(taskq_home, "false")
    child = _submit(taskq_home, "echo downstream")
    proc = _cli(["submit", "echo leaf", "--after", parent], taskq_home)
    assert proc.returncode == 0, proc.stderr
    blocked_id = proc.stdout.strip()

    mark = len(_events(taskq_home))
    run = _cli(["run", "--all"], taskq_home)
    assert run.returncode == 0, f"run --all failed: {run.stderr!r}"
    batch = _events(taskq_home)[mark:]
    kinds = [e["event"] for e in batch]

    assert "run_start" in kinds and "run_end" in kinds, (
        f"`run --all` must emit run_start / run_end (FR-08), got {kinds}"
    )
    started = {e["task_id"] for e in batch if e["event"] == "run_start"}
    assert {parent, child} <= started, (
        f"every executed task needs a run_start, got {started}"
    )
    assert "blocked" in kinds, (
        f"a dependency-blocked task must emit `blocked` (FR-08), got {kinds}"
    )
    assert blocked_id in {e["task_id"] for e in batch if e["event"] == "blocked"}
    assert len({e["correlation_id"] for e in batch}) == 1, (
        "all events of one CLI invocation share one correlation_id (T-08)"
    )


# ===========================================================================
# task_store#1 — T-09: a corrupted store must never be silently rebuilt.
# ===========================================================================
@pytest.mark.parametrize("argv", [["submit", "echo hi"], ["status", "abcdef12"]])
def test_bughunt_task_store_1_corrupt_store_never_rebuilt(taskq_home, argv):  # NFR-03
    """Malformed `tasks.json` → exit 1 + `store corrupted`, file untouched.

    Citations: SPEC.md#L392 (啟動偵測 → exit 1,stderr `store corrupted`,
    **不**靜默重建).
    """
    corrupt = "this is not json{{{"
    store = taskq_home / "tasks.json"
    store.write_text(corrupt, encoding="utf-8")

    proc = _cli(argv, taskq_home)
    assert proc.returncode == 1, (
        f"`{argv[0]}` over a corrupt store must exit 1, got {proc.returncode}; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "store corrupted" in proc.stderr, (
        f"SPEC §7 stderr token `store corrupted` missing: {proc.stderr!r}"
    )
    assert store.read_text(encoding="utf-8") == corrupt, (
        "the corrupt store is evidence — it must NOT be rebuilt/overwritten"
    )


# ===========================================================================
# plugins#1 — FR-07: a plugin load failure exits 6, not 1.
# ===========================================================================
@pytest.mark.parametrize(
    "plugin_name",
    ["../evil.py", "definitely_not_installed_xyz"],
)
def test_bughunt_plugins_1_load_failure_exits_6(taskq_home, plugin_name):  # NFR-02
    """`run` with a rejected / unimportable plugin must exit 6.

    Citations: SPEC.md#L396 (plugin 名稱非法 / 模組不存在 → exit 6).
    """
    task_id = _submit(taskq_home, "echo hi")
    proc = _cli(["run", task_id], taskq_home, TASKQ_PLUGINS=plugin_name)
    assert proc.returncode == 6, (
        f"plugin load failure must exit 6, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )


# ===========================================================================
# task_store#2 — T-07: concurrent submits must not lose records.
# ===========================================================================
def test_bughunt_task_store_2_concurrent_submit_loses_no_task(taskq_home):  # NFR-03
    """N concurrent `submit` processes must yield N persisted tasks.

    Citations: SPEC.md#L206 (並發/中斷寫入後檔案仍合法且已記錄狀態不遺失).
    """
    total = 10
    env = os.environ.copy()
    env[HOME_VAR] = str(taskq_home)
    env[AUDIT_LOG_VAR] = str(taskq_home / "audit.jsonl")
    env.pop("TASKQ_PLUGINS", None)
    inherited = os.environ.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SRC), inherited) if p)

    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "taskq_plus", "submit", f"echo t{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for i in range(total)
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0, f"submit failed: {proc.stderr.read()!r}"

    stored = json.loads((taskq_home / "tasks.json").read_text(encoding="utf-8"))
    assert len(stored) == total, (
        f"{total} concurrent submits must persist {total} tasks (read-modify-write "
        f"must be serialised across processes), got {len(stored)}"
    )
    assert len({t["id"] for t in stored}) == total, "task ids must be unique"
