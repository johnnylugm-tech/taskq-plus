"""TDD-RED tests for FR-04 (結果 TTL 快取).

Maps 1:1 to TEST_SPEC.md §FR-04 cases 1-4. The expected RED state is
``ModuleNotFoundError`` at pytest collection time (``taskq_plus.service
.cache`` / ``taskq_plus.storage.cache_store`` do not exist yet), or
every assertion failing; either counts as a valid failing test in this
phase per the harness TDD contract.

GREEN TODO — the modules and contracts these tests bind to:

* ``taskq_plus.storage.cache_store`` persists ``$TASKQ_HOME/cache.json``
  with the SAD §3.4 envelope
  ``{"version": 1, "entries": {sha256: {"result": dict,
  "cached_at": float}}}`` where ``cached_at`` is POSIX epoch seconds
  (``time.time()``). It MUST import the writer as
  ``from taskq_plus.storage.atomic import atomic_write_json`` and call
  it exactly ONCE per cache write (AC-FR-04.3; NFR-03 atomic), guarded
  by a module-level ``threading.Lock`` so concurrent FR-02 execution
  cannot lose an entry (AC-FR-04.4; SAD §3.4 "tmp + os.replace + Lock").
  Public API:
  - ``load() -> dict[str, dict]``   (the ``entries`` mapping)
  - ``get(sig: str) -> dict | None`` (the stored envelope, or None)
  - ``put(sig: str, result: dict) -> None`` (stamps ``cached_at``)
* ``taskq_plus.service.cache`` exposes:
  - ``cache_key(command: str) -> str`` — ``sha256`` hex digest of the
    command's UTF-8 bytes, NO whitespace normalisation (AC-FR-04.1).
  - ``lookup(command: str) -> dict | None`` — the stored ``result``
    when a ``done`` entry exists and ``time.time() - cached_at <
    TASKQ_CACHE_TTL``; ``None`` on miss or expiry (AC-FR-04.2).
  - ``store(command: str, result: dict) -> None`` — write-through on a
    ``done`` outcome only (AC-FR-04.3).
* The ``run`` CLI path accepts ``--cached`` (both for ``run <id>`` and
  ``run --all``). On a cache HIT it replays ``exit_code`` /
  ``stdout_tail`` into the task record, marks the task ``done`` with
  ``cached: true``, and spawns NO subprocess (AC-FR-04.2; SPEC.md §8 #9).
  On a MISS it executes normally and, when the outcome is ``done``,
  writes the result to ``cache.json`` (AC-FR-04.3).

Test design follows the harness canonical pattern — bind the declared
TEST_SPEC Inputs to local variables, capture a single ``result`` record
per invocation, and emit each spec sub-assertion as a bare ``assert``
matching the predicate shape verbatim from TEST_SPEC.md.
"""

from __future__ import annotations

import contextlib
import hashlib
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

# Top-level imports are intentional — ModuleNotFoundError at collection time
# is the expected RED signal per the unit-test contract.
from taskq_plus import cli  # noqa: E402
from taskq_plus.service import cache  # noqa: E402
from taskq_plus.service import executor  # noqa: E402
from taskq_plus.storage import cache_store  # noqa: E402
from taskq_plus.storage.task_store import TaskStore  # noqa: E402


# -------------------------------------------------------------------
# Fixtures — function-scoped so cache.json entries cannot leak between
# cases (a seeded HIT from case 2 must not satisfy case 3's MISS).
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
    yield home


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _home() -> Path:
    """Return the isolated ``$TASKQ_HOME`` for the running test."""
    return Path(os.environ["TASKQ_HOME"])


def _cache_path() -> Path:
    """Return ``$TASKQ_HOME/cache.json`` (SAD §3.4)."""
    return _home() / "cache.json"


def _sha256_hex(text: str) -> str:
    """Reference implementation of the spec signature: ``sha256(command)``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_cache(signature: str, result_payload: dict, cached_at: float) -> dict:
    """Write the SAD §3.4 cache envelope directly to disk.

    Seeding through the file (not through the service API) keeps the
    precondition independent of the implementation under test, and pins
    the on-disk schema the GREEN implementation must read.
    """
    payload = {
        "version": 1,
        "entries": {
            signature: {"result": result_payload, "cached_at": cached_at}
        },
    }
    _cache_path().write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _read_cache() -> dict:
    """Parse ``cache.json``; empty dict when absent."""
    path = _cache_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _task_store() -> TaskStore:
    """Return a ``TaskStore`` bound to the isolated home."""
    return TaskStore(_home() / "tasks.json")


def _cli(argv: list[str]):
    """Invoke ``cli.main(argv)`` in-process, capturing stdout/stderr.

    In-process (NOT subprocess) is the deliberate choice for cases 1-3:
    TEST_SPEC declares ``subprocess_mode="in_process"`` for them, and
    pytest-cov cannot measure coverage across a process boundary.
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


def _submit(command: str) -> str:
    """Submit ``command`` via the CLI and return its 8-hex task id."""
    cli_result = _cli(["submit", command])
    task_id = cli_result.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{8}", task_id), (
        f"submit did not emit an 8-hex id: {cli_result.stdout!r} "
        f"stderr={cli_result.stderr!r}"
    )
    return task_id


def _child_env(home: Path) -> dict:
    """Compose subprocess env with $TASKQ_HOME + PYTHONPATH for the child.

    pytest's sys.path bootstrap (tests/conftest.py) does NOT propagate to
    a child interpreter, so ``03-development/src`` is pushed explicitly.
    """
    child_env = os.environ.copy()
    child_env["TASKQ_HOME"] = str(home)
    child_env["TASKQ_AUDIT_LOG"] = str(home / "audit.jsonl")
    src_root = Path(__file__).resolve().parents[1] / "src"
    child_env["PYTHONPATH"] = str(src_root) + os.pathsep + child_env.get(
        "PYTHONPATH", ""
    )
    return child_env


# -------------------------------------------------------------------
# FR-04 Case 1 — signature is sha256(command) (happy_path, in-process)
# -------------------------------------------------------------------


# NFR-02 — the cache key is a pure hash, never an eval/exec of the command;
# NFR-09 — zero-skip (this test always runs).
# GREEN TODO: taskq_plus.service.cache must have
#   cache_key(command: str) -> str  returning
#   hashlib.sha256(command.encode("utf-8")).hexdigest().
def test_fr04_cache_signature_is_sha256_of_command():
    """AC-FR-04.1 — 快取簽名 = ``sha256(command)``, deterministic.

    SPEC.md §3 FR-04 verbatim: 快取簽名 = ``sha256(command)``.
    SAD §FR-04 DERIVED: the key is ``sha256`` of the canonical command
    string; whitespace normalisation is NOT implied by "canonical".

    Sub-assertions: AC4-sig-matches-sha256, AC4-sig-deterministic.
    Property: P4-sig-idempotent.
    """
    command = "echo hi"
    expected_sig = (
        "56a79f3b115448072387c2480044bfa2cf8f90e4f5fddd8c943b4e051b81f80b"
    )

    result = SimpleNamespace(
        cache_key_str=cache.cache_key(command),
        cache_key_again=cache.cache_key(command),
        cache_key_padded=cache.cache_key("  " + command),
        cache_key_inner_double_space=cache.cache_key("echo  hi"),
    )

    # AC4-sig-matches-sha256: `result.cache_key_str == expected_sig`
    assert result.cache_key_str == expected_sig
    # AC4-sig-deterministic: `result.cache_key_str == sha256_hex(command)`
    assert result.cache_key_str == _sha256_hex(command)
    # P4-sig-idempotent: `cache_key(command) == cache_key(command)`
    assert result.cache_key_again == result.cache_key_str
    # The digest is 64 lowercase hex chars — sha256, not a shorter hash.
    assert re.fullmatch(r"[0-9a-f]{64}", result.cache_key_str)
    # DERIVED (SAD §FR-04): no whitespace normalisation — distinct command
    # strings must map to distinct signatures.
    assert result.cache_key_padded != result.cache_key_str
    assert result.cache_key_inner_double_space != result.cache_key_str


# -------------------------------------------------------------------
# FR-04 Case 2 — cache hit replays without subprocess (happy_path)
# -------------------------------------------------------------------


# NFR-02 — the replay path spawns nothing, so no execution surface is
# reached; NFR-03 — the replayed record is persisted through the same
# atomic TaskStore.save; NFR-09 — zero-skip.
# GREEN TODO: taskq_plus.service.executor must keep
#   run_subprocess(command: str, timeout: float)
#   -> subprocess.CompletedProcess  as the SINGLE spawn site, and the
#   ``run --cached`` path must consult taskq_plus.service.cache.lookup
#   BEFORE reaching it. cache_store must have
#   put(sig: str, result: dict) -> None and get(sig: str) -> dict | None.
def test_fr04_cache_hit_replays_without_subprocess(monkeypatch):
    """AC-FR-04.2 — fresh ``done`` entry → replay, no subprocess, cached=true.

    SPEC.md §3 FR-04 verbatim: ``taskq-plus run <id> --cached``:同簽名且
    結果為 ``done`` 的最近執行在 ``TASKQ_CACHE_TTL`` 秒內 → 直接回放
    (``exit_code``/``stdout_tail``),不執行 subprocess,任務標記
    ``done`` 且 ``cached: true`` (SPEC.md §8 #9).

    Sub-assertions: AC4-cached-flag-set, AC4-no-subprocess-on-hit,
    AC4-replayed-exit-code. Property: P4-cache-roundtrip.
    """
    cached_at_offset = 10
    ttl_seconds = 3600
    expected_cached_flag = "true"
    command = "echo hi"
    monkeypatch.setenv("TASKQ_CACHE_TTL", str(ttl_seconds))

    task_id = _submit(command)

    # Sentinel payload — a REAL run of "echo hi" would produce "hi\n",
    # so replaying this exact string proves the value came from cache.
    stored_result = {
        "status": "done",
        "exit_code": 0,
        "stdout_tail": "cached-sentinel-hi\n",
        "stderr_tail": "",
    }
    signature = _sha256_hex(command)
    _seed_cache(signature, stored_result, time.time() - cached_at_offset)

    spawned = {"n": 0}
    real_run = executor.run_subprocess

    def _counting_run(command_arg, timeout):
        spawned["n"] += 1
        return real_run(command_arg, timeout)

    monkeypatch.setattr(executor, "run_subprocess", _counting_run)

    cli_result = _cli(["run", task_id, "--cached"])
    record = _task_store().load()[task_id]

    # P4-cache-roundtrip: `cache_lookup(cache_store(sig, result_str)) == result_str`
    roundtrip_sig = _sha256_hex("echo roundtrip")
    cache_store.put(roundtrip_sig, stored_result)
    roundtrip_entry = cache_store.get(roundtrip_sig)

    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        subprocess_spawned=spawned["n"],
        stored_exit_code=stored_result["exit_code"],
        replayed_exit_code=record.get("exit_code"),
        replayed_stdout_tail=record.get("stdout_tail"),
        task_status=record.get("status"),
        cached_flag=record.get("cached"),
        roundtrip_result=(roundtrip_entry or {}).get("result"),
    )

    # AC4-cached-flag-set: `expected_cached_flag == "true"`
    assert expected_cached_flag == "true"
    assert result.cached_flag is True
    # AC4-no-subprocess-on-hit: `result.subprocess_spawned == 0`
    assert result.subprocess_spawned == 0
    # AC4-replayed-exit-code:
    # `result.replayed_exit_code == result.stored_exit_code`
    assert result.replayed_exit_code == result.stored_exit_code
    # 直接回放 exit_code / stdout_tail — the sentinel proves the source.
    assert result.replayed_stdout_tail == stored_result["stdout_tail"]
    # 任務標記 done.
    assert result.task_status == "done"
    assert result.exit_code == 0
    # P4-cache-roundtrip — write then read returns the original payload.
    assert result.roundtrip_result == stored_result


# -------------------------------------------------------------------
# FR-04 Case 3 — cache miss executes + writes atomically (boundary)
# -------------------------------------------------------------------


# NFR-03 — atomic persistence: cache.json is written via
# atomic_write_json (tmp + os.replace) exactly once; NFR-09 — zero-skip.
# GREEN TODO: taskq_plus.storage.cache_store must import the writer as
#   ``from taskq_plus.storage.atomic import atomic_write_json`` and call
#   the module-level name ``atomic_write_json(path, payload)`` exactly
#   once per cache write.
def test_fr04_cache_miss_atomic_write(monkeypatch):
    """AC-FR-04.3 — miss → normal execution → atomic write to cache.json.

    SPEC.md §3 FR-04 verbatim: 快取過期或不存在 → 正常執行,成功
    (``done``)後寫入 ``$TASKQ_HOME/cache.json``;快取讀寫:原子.

    Sub-assertions: AC4-miss-triggers-run, AC4-miss-atomic-write.
    Property: P4-cache-roundtrip.
    """
    cache_present = False
    command = "echo hi"

    # Precondition: the cache file genuinely does not exist.
    assert cache_present is False
    assert not _cache_path().exists()

    task_id = _submit(command)

    spawned = {"n": 0}
    real_run = executor.run_subprocess

    def _counting_run(command_arg, timeout):
        spawned["n"] += 1
        return real_run(command_arg, timeout)

    monkeypatch.setattr(executor, "run_subprocess", _counting_run)

    write_count = {"n": 0}
    real_write = cache_store.atomic_write_json

    def _counting_write(path, payload):
        if Path(path).name == "cache.json":
            write_count["n"] += 1
        return real_write(path, payload)

    monkeypatch.setattr(cache_store, "atomic_write_json", _counting_write)

    before = time.time()
    cli_result = _cli(["run", task_id, "--cached"])
    after = time.time()

    signature = _sha256_hex(command)
    persisted = _read_cache()
    entries = persisted.get("entries", {})
    entry = entries.get(signature, {})

    # P4-cache-roundtrip on the freshly-written entry.
    roundtrip_entry = cache_store.get(signature)

    result = SimpleNamespace(
        exit_code=cli_result.exit_code,
        subprocess_spawned=spawned["n"],
        persist_writes=write_count["n"],
        entry_keys=sorted(entries.keys()),
        cached_at=entry.get("cached_at"),
        stored_result=entry.get("result"),
        roundtrip_result=(roundtrip_entry or {}).get("result"),
        task_status=_task_store().load()[task_id].get("status"),
    )

    # AC4-miss-triggers-run: `result.subprocess_spawned == 1`
    assert result.subprocess_spawned == 1
    # AC4-miss-atomic-write: `result.persist_writes == 1`
    assert result.persist_writes == 1
    # 寫入 $TASKQ_HOME/cache.json,鍵為 sha256(command).
    assert result.entry_keys == [signature]
    # ...with cached_at stamped at write time (POSIX epoch seconds).
    assert isinstance(result.cached_at, (int, float))
    assert before <= result.cached_at <= after
    # 成功(done)後才寫入 — the replayable fields are present.
    assert result.task_status == "done"
    assert result.exit_code == 0
    assert result.stored_result["exit_code"] == 0
    assert "hi" in result.stored_result["stdout_tail"]
    # P4-cache-roundtrip: `cache_lookup(cache_store(sig, result_str)) == result_str`
    assert result.roundtrip_result == result.stored_result


# -------------------------------------------------------------------
# FR-04 Case 4 — thread-safe with run --all (integration, out-of-process)
# -------------------------------------------------------------------


# NFR-02 — the child runs the real ``python -m taskq_plus`` entry with
# shell=False; NFR-03 — concurrent cache writes must leave cache.json
# parseable (no torn file); NFR-09 — zero-skip.
# GREEN TODO: the ``run`` subcommand must accept ``--cached`` together
#   with ``--all``; cache_store must serialise its read-modify-write
#   under a module-level threading.Lock so 20 tasks across 4 workers
#   produce 20 entries (no lost update).
def test_fr04_cache_thread_safe_with_run_all():
    """AC-FR-04.4 — cache read/write is thread-safe under FR-02 concurrency.

    SPEC.md §3 FR-04 verbatim: 快取讀寫:原子 + 執行緒安全(與 FR-02
    並發共存).

    Out-of-process (subprocess) is the deliberate choice here: TEST_SPEC
    declares ``subprocess_mode="out_of_process"`` for this case, so the
    REAL ``python -m taskq_plus run --all --cached`` entry point drives
    the ThreadPoolExecutor end to end.

    Sub-assertion: AC4-thread-safe-final-state.
    """
    tasks_n = 20
    max_workers = 4
    home = _home()

    # Distinct commands → distinct signatures → 20 concurrent cache
    # writes contending for the same cache.json (the thread-safety load).
    commands = [f"echo t{i:02d}" for i in range(tasks_n)]
    for command in commands:
        _submit(command)

    child_env = _child_env(home)
    child_env["TASKQ_MAX_WORKERS"] = str(max_workers)
    child_env["TASKQ_CACHE_TTL"] = "3600"

    completed = subprocess.run(
        [sys.executable, "-m", "taskq_plus", "run", "--all", "--cached"],
        capture_output=True,
        text=True,
        timeout=120,
        env=child_env,
        cwd=str(Path(__file__).resolve().parents[2]),
        shell=False,
    )

    raw = _cache_path().read_text(encoding="utf-8")
    parse_ok = True
    entries: dict = {}
    try:
        entries = json.loads(raw).get("entries", {})
    except json.JSONDecodeError:
        parse_ok = False

    expected_sigs = sorted(_sha256_hex(command) for command in commands)
    statuses = [
        record.get("status") for record in _task_store().load().values()
    ]

    result = SimpleNamespace(
        exit_code=completed.returncode,
        cache_json_parse_ok=parse_ok,
        entry_count=len(entries),
        entry_keys=sorted(entries.keys()),
        done_count=statuses.count("done"),
        stderr_text=completed.stderr,
    )

    assert max_workers == 4
    # AC4-thread-safe-final-state: `result.cache_json_parse_ok == True`
    assert result.cache_json_parse_ok is True, (
        f"cache.json was torn by concurrent writes: {raw!r}"
    )
    assert result.exit_code == 0, f"stderr={result.stderr_text!r}"
    # 與 FR-02 並發共存 — every task ran to done under the pool.
    assert result.done_count == tasks_n
    # No lost update: each of the 20 signatures survived the interleaving.
    assert result.entry_count == tasks_n
    assert result.entry_keys == expected_sigs
