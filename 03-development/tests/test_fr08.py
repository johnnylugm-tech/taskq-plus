"""FR-08 RED tests — 結構化稽核日誌與匯出 (JSONL audit + correlation_id +
NFR-04 redaction + 3-format export).

Per TEST_SPEC.md §FR-08 there are 6 case rows collapsed onto 4 test functions
(rows 3..5 share the function name `test_fr08_c`, so they are three pytest
parametrize ids on that one function — TEST_SPEC.md shape rules v2.13.0
"multi-scenario split", one parametrize id per row):

  - test_fr08_a  (audit entry carries ts/event/task_id/correlation_id/detail) row 1
  - test_fr08_b  (one CLI invocation → one shared correlation_id)             row 2
  - test_fr08_c  (export_format="json", task_count=3)                         row 3
                 (export_format="csv",  task_count=3)                         row 4
                 (export_format="md",   task_count=3)                         row 5
  - test_fr08_d  (secret `sk-abcdef1234` → 0 hits in audit.jsonl on disk)     row 6

Sub-assertions (rule_id → predicate):
  AC8-correlation-id-shape      : correlation_id_len_expected == "8" and
                                  len(correlation_id_token) == 8          row 1
  AC8-event-field-set-known     : len(expected_event_field_set) > 0 and
                                  len(...split(",")) == 5                 row 1
  AC8-shared-correlation-id     : shared_correlation_id_across_events ==
                                  "true" and len(correlation_id_token) > 0 row 2
  AC8-export-format-known       : export_format in {"json","csv","md"}  rows 3-5
  AC8-task-count-positive       : task_count > "0"                      rows 3-5
  AC8-redaction-pattern-present : len(secret_pattern_in_command) > 0 and
                                  secret_pattern_in_command
                                      .startswith("sk-")                   row 6
  AC8-redaction-hits-zero       : expected_secret_hits == "0"              row 6
  AC8-audit-path-nonempty       : len(audit_log_path_token) > 0            row 6

Properties (TEST_SPEC.md §FR-08 Properties, Direction B):
  P8-redaction-idempotent    : redact_text(redact_text(text)) ==
                               redact_text(text)                           row 6
  P8-redaction-plain-unchanged : redact_text(text) == text for secret-free
                               text (declared with no applies_to — asserted
                               here over a concrete secret-free sample).

SAB-bindings (FR-08 binds to, per SAB.json fr_module_traceability."FR-08"):
  - taskq_plus.observability.audit   (does NOT exist on disk — RED)
  - taskq_plus.observability.export  (does NOT exist on disk — RED)

This file is the TDD-RED deliverable: it is EXPECTED to fail with a pytest
Collection Error (Exit Code 2) because `taskq_plus/observability/audit.py` and
`taskq_plus/observability/export.py` are absent (the `observability/` directory
exists but is empty — not even an `__init__.py`). Do NOT wrap these imports in
try/except ImportError and do NOT defer them into function bodies — the crash
IS the RED signal.

State isolation: every fixture below is function-scoped. The audit journal is
append-only, so a leaked `$TASKQ_HOME` from a previous test would make the
"one invocation → one correlation_id" assertion (row 2) read another test's
events. Each test therefore gets its own `tmp_path`-rooted home and its own
`$TASKQ_AUDIT_LOG`.

In-process vs out-of-process (explicit choice, per v2.13.0 integration rules):
  * Each spec-named test asserts the REAL user-facing entry point out of
    process (`subprocess.run([sys.executable, "-m", "taskq_plus", ...])`, with
    PYTHONPATH propagated to the child — pytest's sys.path edits and
    setup.cfg do NOT reach a child process) AND the same behaviour in process
    through the SAB-declared modules `taskq_plus.observability.audit` /
    `taskq_plus.observability.export`, so pytest-cov can actually measure
    those modules (a subprocess is invisible to coverage).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent                 # 03-development/
SRC = ROOT / "src"
HOME_VAR = "TASKQ_HOME"
AUDIT_LOG_VAR = "TASKQ_AUDIT_LOG"

# Make src/ importable for the in-process tests. Subprocess tests do NOT rely
# on this — they propagate PYTHONPATH explicitly through the child env.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ---------------------------------------------------------------------------
# SAB-bound imports — every line below deliberately fails in the RED state.
#
# GREEN TODO: create `03-development/src/taskq_plus/observability/__init__.py`
# plus `.../observability/audit.py` (SAB.json fr_module_traceability."FR-08"
# = ["taskq_plus.observability.audit", "taskq_plus.observability.export"];
# SAD.md / SPEC.md §6 tree: `observability/audit.py` "JSONL 稽核 + redaction
# (FR-08/NFR-04)"). `audit.py` must export:
#
#   - `AUDIT_FIELDS: tuple[str, ...]` == ("ts", "event", "task_id",
#     "correlation_id", "detail") — the exact, complete per-entry field set
#     (SPEC §3 FR-08).
#
#   - `EVENT_TYPES: frozenset[str]` containing exactly the nine SPEC event
#     names: submit / run_start / run_end / retry / breaker_open /
#     breaker_close / cache_hit / blocked / plugin_error.
#
#   - `new_correlation_id() -> str` : a fresh 8-char lowercase-hex token
#     (uuid4 first 8 hex, same shape as the FR-01 task id). Two calls must
#     not collide.
#
#   - `audit_log_path() -> pathlib.Path` : `$TASKQ_AUDIT_LOG` when set,
#     otherwise `$TASKQ_HOME/audit.jsonl` (SPEC §3 FR-08 default).
#
#   - `redact_text(text: str) -> str` : NFR-04 redaction — any LINE matching
#     `(sk-[A-Za-z0-9_-]{8,}|token=\\S+|Bearer\\s+\\S+)` is replaced IN FULL by
#     the literal `[REDACTED]`; secret-free lines are returned byte-identical.
#     Must be idempotent (P8-redaction-idempotent).
#
#   - `AuditLogger(correlation_id: str | None = None, path: Path | None =
#     None)` : one instance per CLI invocation. Exposes `.correlation_id`
#     (generated via `new_correlation_id()` when not supplied) and
#     `emit(event: str, task_id: str | None = None, detail=None) -> dict`,
#     which appends EXACTLY ONE JSON Lines record (append-only, never
#     rewrites) holding exactly the `AUDIT_FIELDS` keys, with `ts` an
#     ISO-8601 UTC timestamp and NFR-04 redaction applied to `detail`
#     BEFORE the bytes reach the disk.
#
#   - `read_entries(path: Path | None = None) -> list[dict]` : parse the JSONL
#     journal back into dicts (empty list when the file is absent).
#
# GREEN TODO: create `.../observability/export.py` exporting:
#
#   - `EXPORT_FORMATS: tuple[str, ...]` == ("json", "csv", "md").
#   - `EXPORT_FIELDS: tuple[str, ...]` : the single field set shared by all
#     three renderings, matching the `status` output fields (SPEC §3 FR-08
#     "欄位同 status"); must at least include "id", "command", "status".
#   - `export_tasks(tasks, fmt: str) -> str` : `json` → a single JSON ARRAY;
#     `csv` → header row + one row per task with commas/quotes escaped per
#     RFC-4180; `md` → a Markdown table. NFR-04 redaction applied first.
#   - `parse_export(content: str, fmt: str) -> list[dict]` : the inverse
#     reader used by the cross-format round-trip assertion — one dict per
#     task, keyed by `EXPORT_FIELDS`.
#
# GREEN TODO: `taskq_plus.cli.main` must build ONE `AuditLogger` per CLI
# invocation and thread it through the handlers, and
# `taskq_plus.cli.commands.export_cmd` must delegate to
# `observability.export.export_tasks` (replacing its inline csv/md writers and
# its `audit.log` journal, which is neither JSONL-named nor correlation-aware).
# Do NOT add stubs to source files from this RED step — GREEN does that.
# ---------------------------------------------------------------------------
from taskq_plus.observability.audit import (  # noqa: E402,F401
    AUDIT_FIELDS,
    EVENT_TYPES,
    AuditLogger,
    audit_log_path,
    current_logger,
    new_correlation_id,
    read_entries,
    redact_text,
    set_current_logger,
)
from taskq_plus.observability.export import (  # noqa: E402,F401
    EXPORT_FIELDS,
    EXPORT_FORMATS,
    export_tasks,
    parse_export,
)

from taskq_plus.cli.main import main as cli_main  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Constants shared by the cases (mirrors of the TEST_SPEC Inputs columns).
# ---------------------------------------------------------------------------
SECRET_TOKEN = "sk-abcdef1234"          # secret_pattern_in_command (row 6)
CORRELATION_ID_TOKEN = "abcdef12"       # correlation_id_token (rows 1, 2)
HEX8_RE = re.compile(r"^[0-9a-f]{8}$")

# A command whose text stresses the CSV escaping rule (comma + double quote)
# — SPEC §3 FR-08 "含逗號/引號的欄位必須正確跳脫".
TRICKY_COMMAND = 'echo "hello, world" , and "quoted, bits"'


# ---------------------------------------------------------------------------
# Function-scoped fixtures — the audit journal is append-only, so no home may
# survive a test (a leaked journal would let one test read another's events).
# ---------------------------------------------------------------------------
@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Fresh $TASKQ_HOME + $TASKQ_AUDIT_LOG + deterministic knobs."""
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


# ---------------------------------------------------------------------------
# Out-of-process helper — the REAL user-facing entry point.
# PYTHONPATH must be propagated explicitly: pytest's sys.path manipulation and
# setup.cfg do NOT reach a child process.
# ---------------------------------------------------------------------------
def _run_cli(argv, home):
    """Run `python -m taskq_plus <argv>` out of process; return CompletedProcess."""
    env = os.environ.copy()
    env[HOME_VAR] = str(home)
    env[AUDIT_LOG_VAR] = str(Path(home) / "audit.jsonl")
    env["TASKQ_RETRY_LIMIT"] = "0"
    env["TASKQ_BACKOFF_BASE"] = "0"
    env["TASKQ_BREAKER_THRESHOLD"] = "99"
    env["TASKQ_TASK_TIMEOUT"] = "10"
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in (str(SRC), inherited) if p]
    )
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _main_capture(argv):
    """Run the CLI IN PROCESS so pytest-cov can measure the handlers."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            code = cli_main(list(argv))
        except SystemExit as exc:  # click standalone-mode escape hatch
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out_buf.getvalue(), err_buf.getvalue()


def _journal_text(home):
    """Return the raw on-disk bytes of the audit journal, decoded as UTF-8."""
    path = Path(os.environ.get(AUDIT_LOG_VAR, str(Path(home) / "audit.jsonl")))
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _journal_entries(home):
    """Parse the audit journal on disk into dicts (independent of read_entries)."""
    entries = []
    for line in _journal_text(home).splitlines():
        stripped = line.strip()
        if stripped:
            entries.append(json.loads(stripped))
    return entries


def _submit_via_cli(home, command, *, out_of_process=True):
    """Submit one task and return its 8-hex id."""
    if out_of_process:
        proc = _run_cli(["submit", command], home)
        assert proc.returncode == 0, (
            f"submit should exit 0, got {proc.returncode}; stderr={proc.stderr!r}"
        )
        return proc.stdout.strip()
    code, out, err = _main_capture(["submit", command])
    assert code == 0, f"submit should exit 0, got {code}; stderr={err!r}"
    return out.strip()


def _is_iso8601_utc(value):
    """True when `value` is an ISO-8601 timestamp anchored to UTC."""
    if not isinstance(value, str) or not value:
        return False
    normalised = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0


# ===========================================================================
# Row 1 — AC-FR-08.a: audit entry carries the canonical five fields.
# NFR-04 (pre-write redaction), NFR-05 (docstring coverage on audit.py).
# ===========================================================================
def test_fr08_a(taskq_home):  # NFR-04, NFR-05
    """AC-FR-08.a: audit entry written with the exact SPEC field set.

    Inputs (TEST_SPEC row 1): correlation_id_len_expected="8";
    correlation_id_token="abcdef12";
    expected_event_field_set="ts,event,task_id,correlation_id,detail".
    """
    correlation_id_len_expected = "8"
    correlation_id_token = CORRELATION_ID_TOKEN
    expected_event_field_set = "ts,event,task_id,correlation_id,detail"

    # rule_id: AC8-correlation-id-shape
    assert correlation_id_len_expected == "8" and len(correlation_id_token) == 8
    # rule_id: AC8-event-field-set-known
    assert (
        len(expected_event_field_set) > 0
        and len(expected_event_field_set.split(",")) == 5
    )

    # (1) in-process — the SAB-declared module is the unit under test, so
    # pytest-cov can actually measure `observability/audit.py`.
    assert tuple(AUDIT_FIELDS) == tuple(expected_event_field_set.split(",")), (
        f"AUDIT_FIELDS must be the SPEC field set in order, got {AUDIT_FIELDS!r}"
    )
    generated = new_correlation_id()
    assert HEX8_RE.match(generated), (
        f"new_correlation_id() must be 8 lowercase hex chars, got {generated!r}"
    )
    assert len(generated) == len(correlation_id_token) == 8
    assert new_correlation_id() != generated, (
        "new_correlation_id() must not repeat itself across calls"
    )

    logger = AuditLogger(correlation_id=correlation_id_token)
    assert logger.correlation_id == correlation_id_token
    assert audit_log_path() == taskq_home / "audit.jsonl", (
        "audit_log_path() must honour $TASKQ_AUDIT_LOG "
        "(default $TASKQ_HOME/audit.jsonl)"
    )
    logger.emit("submit", task_id="deadbeef", detail={"command": "echo hi"})

    entries = _journal_entries(taskq_home)
    assert len(entries) == 1, (
        f"emit() must append exactly one JSON Lines record, got {len(entries)}"
    )
    entry = entries[0]
    assert set(entry) == set(expected_event_field_set.split(",")), (
        f"audit entry fields {sorted(entry)} != SPEC field set "
        f"{sorted(expected_event_field_set.split(','))}"
    )
    assert entry["event"] == "submit" and entry["event"] in EVENT_TYPES
    assert entry["task_id"] == "deadbeef"
    assert entry["correlation_id"] == correlation_id_token
    assert entry["detail"] == {"command": "echo hi"}
    assert _is_iso8601_utc(entry["ts"]), (
        f"`ts` must be ISO-8601 UTC, got {entry['ts']!r}"
    )
    assert EVENT_TYPES >= {
        "submit", "run_start", "run_end", "retry", "breaker_open",
        "breaker_close", "cache_hit", "blocked", "plugin_error",
    }, f"EVENT_TYPES is missing SPEC event names: {sorted(EVENT_TYPES)}"

    # `read_entries()` is the module's own reader — it must agree with the
    # raw file parse above.
    assert read_entries() == entries

    # (2) out-of-process — the REAL entry point must write the same shape.
    task_id = _submit_via_cli(taskq_home, "echo hello")
    assert HEX8_RE.match(task_id), f"submit must print an 8-hex id, got {task_id!r}"
    cli_entries = [e for e in _journal_entries(taskq_home) if e["event"] == "submit"]
    assert len(cli_entries) >= 2, (
        "`submit` through the CLI must append a `submit` audit event to "
        "$TASKQ_AUDIT_LOG (SPEC §3 FR-01 → FR-08)"
    )
    cli_entry = cli_entries[-1]
    assert set(cli_entry) == set(AUDIT_FIELDS), (
        f"CLI-written audit entry fields {sorted(cli_entry)} != {sorted(AUDIT_FIELDS)}"
    )
    assert cli_entry["task_id"] == task_id
    assert _is_iso8601_utc(cli_entry["ts"])
    assert HEX8_RE.match(str(cli_entry["correlation_id"]))


# ===========================================================================
# Row 2 — AC-FR-08.b: one CLI invocation → one shared correlation_id.
# NFR-04 (audit append-only; no secret on disk), NFR-10 (cross-module integration).
# ===========================================================================
def test_fr08_b(taskq_home):  # NFR-04, NFR-10
    """AC-FR-08.b: all events of a single CLI invocation share one id.

    Inputs (TEST_SPEC row 2): correlation_id_token="abcdef12";
    shared_correlation_id_across_events="true".
    """
    correlation_id_token = CORRELATION_ID_TOKEN
    shared_correlation_id_across_events = "true"

    # rule_id: AC8-shared-correlation-id
    assert (
        shared_correlation_id_across_events == "true"
        and len(correlation_id_token) > 0
    )

    # (1) in-process — one AuditLogger models one invocation: every event it
    # emits carries its id, and a second invocation gets a different one.
    logger = AuditLogger(correlation_id=correlation_id_token)
    logger.emit("run_start", task_id="deadbeef", detail={"attempt": 1})
    logger.emit("retry", task_id="deadbeef", detail={"attempt": 2})
    logger.emit("run_end", task_id="deadbeef", detail={"status": "done"})

    first_invocation = _journal_entries(taskq_home)
    assert len(first_invocation) == 3
    ids_seen = {e["correlation_id"] for e in first_invocation}
    assert ids_seen == {correlation_id_token}, (
        f"all events of one invocation must share one correlation_id, "
        f"saw {sorted(ids_seen)}"
    )

    second = AuditLogger()
    second.emit("run_start", task_id="cafebabe", detail={"attempt": 1})
    tail = _journal_entries(taskq_home)[len(first_invocation):]
    assert len(tail) == 1
    assert tail[0]["correlation_id"] != correlation_id_token, (
        "a NEW invocation must generate a NEW correlation_id"
    )
    assert HEX8_RE.match(str(tail[0]["correlation_id"]))

    # (2) out-of-process — the REAL entry point. `run <id>` triggers at least
    # run_start + run_end within ONE invocation, so those events must share
    # one id, and that id must differ from the earlier `submit` invocation's.
    before = len(_journal_entries(taskq_home))
    task_id = _submit_via_cli(taskq_home, "echo hello")
    submit_events = _journal_entries(taskq_home)[before:]
    assert submit_events, "`submit` must emit at least one audit event"
    submit_ids = {e["correlation_id"] for e in submit_events}
    assert len(submit_ids) == 1, (
        f"one `submit` invocation must use one correlation_id, got {submit_ids}"
    )

    mark = len(_journal_entries(taskq_home))
    proc = _run_cli(["run", task_id], taskq_home)
    assert proc.returncode == 0, (
        f"`run {task_id}` should exit 0, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    run_events = _journal_entries(taskq_home)[mark:]
    assert len(run_events) >= 2, (
        "one `run` invocation must emit at least run_start + run_end "
        f"(SPEC §3 FR-08 event types), got {[e['event'] for e in run_events]}"
    )
    run_ids = {e["correlation_id"] for e in run_events}
    assert len(run_ids) == 1, (
        f"all events of one `run` invocation must share one correlation_id, "
        f"saw {sorted(run_ids)}"
    )
    assert run_ids.isdisjoint(submit_ids), (
        "a distinct CLI invocation must generate a distinct correlation_id"
    )
    assert {"run_start", "run_end"} <= {e["event"] for e in run_events}


# ===========================================================================
# Rows 3-5 — AC-FR-08.c: three export formats, identical count + field set.
# ===========================================================================
@pytest.mark.parametrize(
    "export_format",
    [
        "json",  # row 3
        "csv",   # row 4
        "md",    # row 5
    ],
)
def test_fr08_c(taskq_home, export_format):  # NFR-04, NFR-05
    """AC-FR-08.c: json/csv/md agree on task count and field set; CSV escapes.

    Inputs (TEST_SPEC rows 3-5): export_format="json"|"csv"|"md";
    task_count="3".
    """
    task_count = "3"

    # rule_id: AC8-export-format-known
    assert export_format in {"json", "csv", "md"}
    # rule_id: AC8-task-count-positive
    assert task_count > "0"

    expected_count = int(task_count)
    assert tuple(EXPORT_FORMATS) == ("json", "csv", "md")
    assert {"id", "command", "status"} <= set(EXPORT_FIELDS), (
        f"EXPORT_FIELDS must match the `status` field set, got {EXPORT_FIELDS!r}"
    )

    # Three tasks; the last one carries commas AND double quotes so the CSV
    # escaping rule (SPEC §3 FR-08) is exercised by the round trip.
    commands = ["echo task0", "echo task1", TRICKY_COMMAND]
    task_ids = [_submit_via_cli(taskq_home, cmd) for cmd in commands]
    assert len(task_ids) == expected_count

    # (1) in-process — render through the SAB-declared export module so
    # pytest-cov can measure `observability/export.py`.
    tasks = [
        {field: "" for field in EXPORT_FIELDS} | {
            "id": task_ids[index],
            "command": commands[index],
            "status": "pending",
        }
        for index in range(expected_count)
    ]
    body = export_tasks(tasks, export_format)
    assert isinstance(body, str) and body.strip(), (
        f"export_tasks(..., {export_format!r}) produced no output"
    )

    records = parse_export(body, export_format)
    assert len(records) == expected_count, (
        f"format {export_format!r} exported {len(records)} records, "
        f"expected {expected_count} (identical across json/csv/md)"
    )
    for record in records:
        assert set(record) == set(EXPORT_FIELDS), (
            f"format {export_format!r} field set {sorted(record)} != "
            f"{sorted(EXPORT_FIELDS)}"
        )

    # Cross-format invariant: the json rendering is the baseline every format
    # must agree with, on BOTH count and field set (SPEC §8 #14).
    baseline = parse_export(export_tasks(tasks, "json"), "json")
    assert len(records) == len(baseline)
    assert [set(r) for r in records] == [set(r) for r in baseline]
    assert [r["id"] for r in records] == [r["id"] for r in baseline]

    # Round trip must preserve the comma/quote-laden command verbatim.
    assert records[-1]["command"] == TRICKY_COMMAND, (
        f"format {export_format!r} mangled a field containing commas/quotes: "
        f"{records[-1]['command']!r}"
    )

    if export_format == "json":
        decoded = json.loads(body)
        assert isinstance(decoded, list), (
            "`export --format json` must emit a single JSON array (SPEC §3 FR-08)"
        )
        assert len(decoded) == expected_count
    elif export_format == "csv":
        lines = [ln for ln in body.splitlines() if ln.strip()]
        assert lines[0].split(",")[0].strip('"') == EXPORT_FIELDS[0], (
            "CSV must start with a header row naming EXPORT_FIELDS"
        )
        assert f'"{TRICKY_COMMAND}"' in body or '""' in body, (
            "CSV fields containing commas/quotes must be quoted/escaped "
            "per SPEC §3 FR-08"
        )
    else:
        lines = [ln for ln in body.splitlines() if ln.strip()]
        assert lines[0].lstrip().startswith("|"), (
            "`export --format md` must emit a Markdown table"
        )
        assert set(lines[1].replace("|", "").replace(" ", "")) <= {"-", ":"}, (
            "Markdown table needs a separator row under the header"
        )

    # (2) out-of-process — the REAL user-facing command line.
    proc = _run_cli(["export", "--format", export_format], taskq_home)
    assert proc.returncode == 0, (
        f"`export --format {export_format}` should exit 0, got "
        f"{proc.returncode}; stderr={proc.stderr!r}"
    )
    cli_records = parse_export(proc.stdout, export_format)
    assert len(cli_records) == expected_count, (
        f"CLI `export --format {export_format}` returned {len(cli_records)} "
        f"records, expected {expected_count}"
    )
    for record in cli_records:
        assert set(record) == set(EXPORT_FIELDS)

    # (3) in-process CLI — same command through cli.main (coverage-visible).
    code, out, err = _main_capture(["export", "--format", export_format])
    assert code == 0, f"in-process export exited {code}; stderr={err!r}"
    assert len(parse_export(out, export_format)) == expected_count


# ===========================================================================
# Row 6 — AC-FR-08.d: NFR-04 redaction happens BEFORE the write.
# NFR-04 (write-time redaction, grep-zero on disk), NFR-09 (every test has
# at least one assert — anti-fabrication).
# ===========================================================================
def test_fr08_d(taskq_home):  # NFR-04, NFR-09
    """AC-FR-08.d: no plaintext secret ever reaches the audit journal.

    Inputs (TEST_SPEC row 6): secret_pattern_in_command="sk-abcdef1234";
    audit_log_path_token="$TASKQ_HOME/audit.jsonl"; expected_secret_hits="0".
    """
    secret_pattern_in_command = SECRET_TOKEN
    audit_log_path_token = "$TASKQ_HOME/audit.jsonl"
    expected_secret_hits = "0"

    # rule_id: AC8-redaction-pattern-present
    assert (
        len(secret_pattern_in_command) > 0
        and secret_pattern_in_command.startswith("sk-")
    )
    # rule_id: AC8-redaction-hits-zero
    assert expected_secret_hits == "0"
    # rule_id: AC8-audit-path-nonempty
    assert len(audit_log_path_token) > 0

    # Properties (TEST_SPEC §FR-08 Properties, Direction B).
    secret_line = f"curl -H 'Authorization: Bearer {secret_pattern_in_command}' url"
    plain_line = "echo hello world"
    # property_id: P8-redaction-plain-unchanged
    assert redact_text(plain_line) == plain_line, (
        "secret-free text must survive redaction byte-identical"
    )
    # property_id: P8-redaction-idempotent
    assert redact_text(redact_text(secret_line)) == redact_text(secret_line)
    # NFR-04: the whole matching LINE is replaced by `[REDACTED]`.
    assert redact_text(secret_line) == "[REDACTED]", (
        "NFR-04 replaces the entire matching line with `[REDACTED]`"
    )
    mixed = f"{plain_line}\n{secret_line}\n{plain_line}"
    assert redact_text(mixed).splitlines() == [plain_line, "[REDACTED]", plain_line]
    assert secret_pattern_in_command not in redact_text(mixed)

    # (1) in-process — redaction must happen on the WRITE path, so the
    # assertion is over the file bytes, not over a post-load string.
    logger = AuditLogger(correlation_id=CORRELATION_ID_TOKEN)
    logger.emit("submit", task_id="deadbeef", detail={"command": secret_line})
    on_disk = _journal_text(taskq_home)
    assert on_disk.count(secret_pattern_in_command) == int(expected_secret_hits), (
        f"plaintext secret {secret_pattern_in_command!r} reached "
        f"{audit_log_path()} — NFR-04 redaction must run before the write"
    )
    assert "[REDACTED]" in on_disk, (
        "the redacted audit entry must carry the `[REDACTED]` marker"
    )
    entries = _journal_entries(taskq_home)
    assert len(entries) == 1 and set(entries[0]) == set(AUDIT_FIELDS)

    # (2) out-of-process — the secret enters through the REAL entry point and
    # the assertion is a file-content grep over $TASKQ_AUDIT_LOG bytes.
    task_id = _submit_via_cli(taskq_home, f"echo {secret_pattern_in_command}")
    assert HEX8_RE.match(task_id)
    proc = _run_cli(["run", task_id], taskq_home)
    assert proc.returncode == 0, (
        f"`run {task_id}` should exit 0, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    journal = _journal_text(taskq_home)
    assert journal.count(secret_pattern_in_command) == int(expected_secret_hits), (
        f"plaintext secret {secret_pattern_in_command!r} found in the audit "
        f"journal ({audit_log_path_token}) after submit+run — expected "
        f"{expected_secret_hits} hits"
    )
    # Nothing may leak through the export path either (SPEC §8 #22).
    export_proc = _run_cli(["export", "--format", "json"], taskq_home)
    assert export_proc.returncode == 0
    assert export_proc.stdout.count(secret_pattern_in_command) == 0, (
        "export must emit redacted records (NFR-04)"
    )


# ===========================================================================
# Coverage-extension tests — drive every line in
# `observability/audit.py` and `observability/export.py`. These cover the
# remaining branches that the four spec tests do not exercise:
#
#   audit.py
#     line 107 — `audit_log_path` default fallback when $TASKQ_AUDIT_LOG unset
#     line 248 — `current_logger` returns the module-global default logger
#
#   export.py
#     line  55 — `_redact_leaf` tuple branch
#     line 148 — `export_tasks` rejects an unknown format
#     line 167 — `parse_export(json)` rejects a non-list payload
#     line 179 — `parse_export(csv)` returns [] on empty input
#     line 183 — `parse_export(csv)` skips an all-empty row
#     line 192 — `parse_export(md)` skips a non-table line
#     line 203 — `parse_export` rejects an unknown format
#
# Each test stays inside the same fresh-home contract as the spec tests:
# every test owns its own `$TASKQ_HOME` / `$TASKQ_AUDIT_LOG`.
# ===========================================================================


# ---------------------------------------------------------------------------
# Coverage: audit.py line 107 — default fallback when `$TASKQ_AUDIT_LOG` is
# unset, the path must default to `$TASKQ_HOME/audit.jsonl` (SPEC.md#L166).
# ---------------------------------------------------------------------------
def test_audit_log_path_default_fallback(taskq_home, monkeypatch):
    """`audit_log_path()` honours the env-var precedence documented in SPEC §5.1."""
    monkeypatch.delenv(AUDIT_LOG_VAR, raising=False)
    expected = taskq_home / "audit.jsonl"
    assert audit_log_path() == expected, (
        f"audit_log_path() must default to $TASKQ_HOME/audit.jsonl "
        f"when $TASKQ_AUDIT_LOG is unset, got {audit_log_path()!r}"
    )
    # The default-only branch is the SAME branch the env-override path took
    # for the rest of the suite — re-confirm the override precedence.
    monkeypatch.setenv(AUDIT_LOG_VAR, str(expected))
    assert audit_log_path() == expected


# ---------------------------------------------------------------------------
# Coverage: audit.py line 248 — `current_logger()` returns the module-global
# logger installed at import time. Also exercises `set_current_logger` so the
# per-invocation contract (SPEC.md#L168) is observable.
# ---------------------------------------------------------------------------
def test_current_logger_round_trip(taskq_home):
    """`current_logger()` returns the module-global writer; `set_current_logger` swaps it."""
    baseline = current_logger()
    assert isinstance(baseline, AuditLogger), (
        "current_logger() must return an AuditLogger instance"
    )
    assert HEX8_RE.match(baseline.correlation_id), (
        "the module-global logger must carry a valid 8-hex correlation_id"
    )

    # Swap the invocation-scoped logger; current_logger() must surface it.
    pinned = AuditLogger(correlation_id=CORRELATION_ID_TOKEN)
    set_current_logger(pinned)
    assert current_logger() is pinned, (
        "set_current_logger() must install the logger that current_logger() returns"
    )
    assert current_logger().correlation_id == CORRELATION_ID_TOKEN

    # The pinned logger must write through the same journal the CLI uses
    # (same path resolution via the env).
    current_logger().emit("submit", task_id="deadbeef", detail={"attempt": 1})
    entries = _journal_entries(taskq_home)
    assert len(entries) == 1
    assert entries[0]["correlation_id"] == CORRELATION_ID_TOKEN


# ---------------------------------------------------------------------------
# Coverage: export.py line 55 — `_redact_leaf` tuple branch. A tuple value
# in a non-`depends_on` field (e.g. a `command` slot supplied as a tuple)
# must be redacted just like a list — NFR-04 has no opinion on container
# type, only on string leaves.
# ---------------------------------------------------------------------------
def test_export_redact_tuple_branch(taskq_home):
    """NFR-04 redaction must recurse into tuple values too, not just lists."""
    secret = SECRET_TOKEN
    # `command` is normally a string, but `_normalise_for_export` delegates to
    # `_redact_leaf` for every non-`depends_on` field, so a tuple value here
    # exercises the tuple branch.
    tasks = [
        {
            "id": "deadbeef",
            "command": ("echo hi", secret, "echo done"),
            "name": "tuple-task",
            "status": "pending",
            "created_at": "2026-07-30T00:00:00Z",
            "depends_on": [],
        }
    ]
    body = export_tasks(tasks, "json")
    assert secret not in body, (
        f"tuple branch must redact nested secrets, body={body!r}"
    )
    assert "[REDACTED]" in body, (
        "the redacted tuple element must surface as `[REDACTED]` in the JSON"
    )
    # Round-trip must agree with the cross-format invariant.
    records = parse_export(body, "json")
    assert len(records) == 1
    decoded_command = records[0]["command"]
    assert secret not in str(decoded_command), (
        "round-trip must NOT reintroduce the secret after JSON parse"
    )


# ---------------------------------------------------------------------------
# Coverage: export.py line 148 — `export_tasks` raises ValueError when
# `fmt` is not one of `EXPORT_FORMATS`. The SAB-bound module is the public
# surface; callers must get a loud failure, not silent coercion.
# ---------------------------------------------------------------------------
def test_export_tasks_unknown_format_raises(taskq_home):
    """`export_tasks` must reject any fmt outside `EXPORT_FORMATS` (no silent coercion)."""
    tasks = [
        {
            "id": "deadbeef",
            "command": "echo hi",
            "name": "n",
            "status": "pending",
            "created_at": "2026-07-30T00:00:00Z",
            "depends_on": [],
        }
    ]
    with pytest.raises(ValueError, match="unsupported export format"):
        export_tasks(tasks, "xml")
    # The lowercase-normalisation branch — uppercase must also raise.
    with pytest.raises(ValueError, match="unsupported export format"):
        export_tasks(tasks, "YAML")


# ---------------------------------------------------------------------------
# Coverage: export.py line 167 — `parse_export(json)` raises ValueError
# when the payload is NOT a JSON array (SPEC §3 FR-08 mandates a single
# JSON ARRAY).
# ---------------------------------------------------------------------------
def test_parse_export_json_not_a_list_raises(taskq_home):
    """`parse_export(json)` must reject an object/dict payload with ValueError."""
    not_a_list = '{"id":"x","command":"echo hi"}'   # a JSON object, not an array
    with pytest.raises(ValueError, match="must be a list"):
        parse_export(not_a_list, "json")
    # Scalar JSON is also not a list.
    with pytest.raises(ValueError, match="must be a list"):
        parse_export('"just a string"', "json")


# ---------------------------------------------------------------------------
# Coverage: export.py line 179 — `parse_export(csv)` returns [] when the
# input has no rows at all. The empty-journal contract is mirrored here:
# no header, no rows, no records.
# ---------------------------------------------------------------------------
def test_parse_export_csv_empty_input(taskq_home):
    """Empty CSV content must round-trip to an empty record list."""
    assert parse_export("", "csv") == []
    # Whitespace-only input is also "no rows".
    assert parse_export("\n\n  \n", "csv") == []
    # A header-only CSV has a row (the header) but no data rows — `parse_export`
    # must drop the header and return [].
    header_only = ",".join(EXPORT_FIELDS) + "\n"
    assert parse_export(header_only, "csv") == []


# ---------------------------------------------------------------------------
# Coverage: export.py line 183 — `parse_export(csv)` skips rows whose cells
# are all empty/whitespace. (The CSV writer never emits such rows, but
# hand-edited or concatenated inputs do — the parser must be tolerant.)
# ---------------------------------------------------------------------------
def test_parse_export_csv_skips_empty_rows(taskq_home):
    """`parse_export(csv)` must drop rows where every cell is blank."""
    header = ",".join(EXPORT_FIELDS)
    blank_row = ",".join([""] * len(EXPORT_FIELDS))
    data_row = ",".join(
        [
            "deadbeef",  # id
            "echo hi",  # command
            "name",  # name
            "pending",  # status
            "2026-07-30T00:00:00Z",  # created_at
            "",  # depends_on
        ]
    )
    body = "\n".join([header, blank_row, data_row, "   ,   ,   ,   ,   ,   "]) + "\n"
    records = parse_export(body, "csv")
    assert len(records) == 1, (
        f"blank rows must be skipped, got {records!r}"
    )
    assert records[0]["id"] == "deadbeef"
    assert records[0]["command"] == "echo hi"


# ---------------------------------------------------------------------------
# Coverage: export.py line 192 — `parse_export(md)` skips lines that are
# not a pipe-table row. (Markdown files often carry prose above/below the
# table; the parser must not crash on a stray heading or paragraph.)
# ---------------------------------------------------------------------------
def test_parse_export_md_skips_non_table_lines(taskq_home):
    """`parse_export(md)` must ignore headings, paragraphs, and blank lines."""
    body = (
        "# Tasks\n"
        "\n"
        "A short preamble that is not a table.\n"
        "\n"
        "| " + " | ".join(EXPORT_FIELDS) + " |\n"
        "|" + "|".join("---" for _ in EXPORT_FIELDS) + "|\n"
        "| deadbeef | echo hi | name | pending | 2026-07-30T00:00:00Z |  |\n"
        "\n"
        "Trailing prose that should not be parsed.\n"
    )
    records = parse_export(body, "md")
    assert len(records) == 1, (
        f"only the one table row must round-trip, got {records!r}"
    )
    assert records[0]["id"] == "deadbeef"
    assert records[0]["command"] == "echo hi"
    # A second table row in the same body increases the record count to 2.
    body_with_two = body + "| cafebabe | echo bye | name2 | done | ts | x |\n"
    records_two = parse_export(body_with_two, "md")
    assert len(records_two) == 2


# ---------------------------------------------------------------------------
# Coverage: export.py line 203 — `parse_export` raises ValueError on an
# unknown `fmt`, mirroring the writer-side check at line 148.
# ---------------------------------------------------------------------------
def test_parse_export_unknown_format_raises(taskq_home):
    """`parse_export` must reject any fmt outside `EXPORT_FORMATS`."""
    with pytest.raises(ValueError, match="unsupported export format"):
        parse_export("anything", "xml")
    # The lowercase-normalisation branch — uppercase must also raise.
    with pytest.raises(ValueError, match="unsupported export format"):
        parse_export("anything", "YAML")
