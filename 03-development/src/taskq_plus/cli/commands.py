"""CLI command implementations.

[FR-01] [FR-02] [FR-03] [FR-04] [FR-05] [FR-06] [FR-07] [FR-08]
Citations:
  - SPEC.md §3 FR-01 (submit command, validation, audit emit).
  - SPEC.md §3 FR-02 (run / run --all dispatch).
  - SPEC.md#L113 (FR-03: run rejected with exit 3 while the breaker is OPEN).
  - SPEC.md §3 FR-04 (run --cached: TTL-keyed replay within TASKQ_CACHE_TTL).
  - SPEC.md §3 FR-05 (the eight subcommand handlers wired through click).
  - SPEC.md §3 FR-06 (graph text / dot rendering).
  - SPEC.md §3 FR-07 (plugin allowlist regex for path-form names).
  - SPEC.md §3 FR-08 (export json / csv / md; NFR-04 secret redaction).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from pydantic import ValidationError

from taskq_plus.models.task import TaskSubmission, generate_task_id
from taskq_plus.observability import export as obs_export
from taskq_plus.observability.audit import current_logger
from taskq_plus.service import dag
from taskq_plus.service.executor import run_all as exec_run_all
from taskq_plus.storage.task_store import (
    _now_iso,
    append_task,
    find_by_id,
    find_by_name,
    load_tasks,
    tasks_path,
)


# ---------------------------------------------------------------------------
# Public exit-code constants — SPEC §3 FR-05 / §7 / SRS §5.
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_VALIDATION_ERROR = 2
EXIT_NOT_FOUND = 3
EXIT_TIMEOUT = 4


# ---------------------------------------------------------------------------
# Exception taxonomy — the FR-05 handlers raise one of these so the click
# wrapper in cli/main.py can map them onto the SPEC §3 FR-05 exit-code map.
# Handlers NEVER print (stdout / stderr); the click wrapper owns the
# rendering path (`cli.main.render`) and the stderr emission.
# ---------------------------------------------------------------------------
class HandlerError(Exception):
    """Base class for FR-05 handler errors."""


class SubmitValidationError(HandlerError):
    """FR-01 / FR-05 input validation failure → exit 2."""


class GraphError(HandlerError):
    """FR-05 dependency-graph error → exit 5."""


class RunValidationError(HandlerError):
    """`run` rejected on input shape → exit 2."""


class RunInternalError(HandlerError):
    """`run` produced no usable result → exit 1."""


class StatusValidationError(HandlerError):
    """`status` rejected on input shape → exit 2."""


class StoreCorrupted(HandlerError):
    """tasks.json is corrupt → exit 1 (per SPEC §7 NFR-03, no silent rebuild)."""


class ExportValidationError(HandlerError):
    """`export` rejected on input shape → exit 2."""


class PluginValidationError(HandlerError):
    """`plugins` rejected on input shape → exit 2."""


class PluginLoadError(HandlerError):
    """`plugins` rejected because of allowlist / load failure → exit 6."""


# ---------------------------------------------------------------------------
# Misc helpers (shared by the 8 FR-05 handlers and the legacy argparse path).
# ---------------------------------------------------------------------------
def _emit_audit(event: str, payload: dict, taskq_home: Path) -> None:
    """Append-only audit log write (FR-08-shaped).

    [FR-01] [FR-08]
    Citations: SPEC.md §3 FR-01 ("Write a submit audit event (FR-08)").
    """
    audit_path = Path(taskq_home) / "audit.log"
    line = json.dumps(
        {"event": event, "ts": _now_iso(), **payload}, ensure_ascii=False
    )
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _resolve_max_dag_depth() -> int:
    """Return the effective TASKQ_MAX_DAG_DEPTH (FR-05), defaulting to 32."""
    raw = os.environ.get("TASKQ_MAX_DAG_DEPTH")
    try:
        return int(raw) if raw is not None and raw != "" else 32
    except ValueError:
        return 32


def _tasks_by_id() -> dict[str, dict[str, Any]]:
    """Index the persisted task store by task id.

    Tasks whose `id` field is missing are skipped, so the returned mapping
    satisfies the `Mapping[str, Mapping[str, Any]]` contract that
    `service.dag.chain_length` / `check_depth` / `ancestor_tasks` declare.
    Sharing one builder keeps the submit and depth paths on an identical
    view of the store.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for task in load_tasks():
        task_id = task.get("id")
        if task_id is None:
            continue
        by_id[task_id] = task
    return by_id


def _compute_depth(depends_on: Sequence[str]) -> int:
    """Compute the longest dependency-chain length reachable from `depends_on`.

    Returns the depth (0 = no dependencies, 1 = one parent, …). A missing
    parent is treated as depth 0 (so submit never blocks on a vanished
    predecessor). Pure traversal over the persisted task store; safe to call
    inside submit because each new task is appended only after this check.

    Depth is edge-count based, i.e. one less than the node-count chain length
    that `service.dag.chain_length` reports (SPEC §7 reports node counts).
    """
    return dag.chain_length(depends_on, by_id=_tasks_by_id()) - 1


def _strict_load_tasks() -> list:
    """Load tasks.json; raise StoreCorrupted on parse failure (FR-05 exit 1)."""
    p = tasks_path()
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise StoreCorrupted(f"tasks.json is corrupt: {exc}") from exc
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        return data["tasks"]
    raise StoreCorrupted("tasks.json is not a task list")


# FR-07 plugin allowlist regex — NFR-02.c "rejects path-form names".
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

# NFR-04 secret redaction patterns — AC-NFR-04.a.
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+")


def _redact(value):
    """Return `value` with NFR-04 secret patterns replaced by `[REDACTED]`."""
    if isinstance(value, str):
        return _SECRET_RE.sub("[REDACTED]", value)
    return value


def _redact_task(task: dict) -> dict:
    """Return a shallow copy of `task` with the `command` field redacted."""
    out = dict(task)
    out["command"] = _redact(out.get("command", ""))
    return out


# ---------------------------------------------------------------------------
# FR-05 command handlers — each returns a plain dict (NEVER prints).
# The click wrapper in cli/main.py owns stdout (via `render`) and stderr.
# ---------------------------------------------------------------------------
def submit_cmd(
    command: str,
    *,
    name: Optional[str] = None,
    after: Optional[Sequence[str]] = None,
) -> dict:
    """Submit a new task; return a plain dict, never print.

    [FR-01] [FR-05] [FR-06]
    Citations:
      - SPEC.md §3 FR-01 (TaskSubmission validation, id generation, audit).
      - SPEC.md §3 FR-05 (submit wired through click; depth error → exit 5).
      - SPEC.md §3 FR-06 (`--after` repeatable; cycle → exit 5 + cycle path;
        chain depth > `TASKQ_MAX_DAG_DEPTH` → exit 5).
      - SPEC.md §7 (exit-code map: graph error → 5).
    """
    deps: list[str] = list(after) if after else []

    try:
        submission = TaskSubmission(
            command=command,
            name=name,
            depends_on=deps,
        )
    except ValidationError as exc:
        msg = exc.errors()[0]["msg"] if exc.errors() else str(exc)
        raise SubmitValidationError(msg) from exc

    if submission.name is not None and find_by_name(submission.name) is not None:
        raise SubmitValidationError(
            f"name {submission.name!r} already used by a pending/running task"
        )

    for dep_id in submission.depends_on:
        if find_by_id(dep_id) is None:
            raise SubmitValidationError(
                f"dependency task id {dep_id!r} does not exist"
            )

    by_id = _tasks_by_id()

    # FR-06 cycle rejection — validate that the *whole* store remains acyclic
    # after this submission is accepted. A new task with a fresh id cannot
    # itself close a loop, but the store may already be cyclic (e.g. corrupted
    # tasks.json or an earlier buggy submit), and any further submit into a
    # cyclic store must be rejected (SPEC §8 #11 / AC-FR-06.b).
    full_graph = list(by_id.values())
    cycle = dag.detect_cycle(full_graph)
    if cycle is not None:
        raise GraphError(
            f"dependency cycle detected: {dag.cycle_path_string(cycle)}"
        )

    # FR-06 depth cap — SPEC §7 stderr `dependency chain too deep: <n> > <max>`.
    try:
        dag.check_depth(
            submission.depends_on,
            by_id=by_id,
            max_depth=_resolve_max_dag_depth(),
        )
    except dag.DepthExceeded as exc:
        raise GraphError(str(exc)) from exc

    task_id = generate_task_id()
    task = {
        "id": task_id,
        "command": submission.command,
        "name": submission.name,
        "status": "pending",
        "created_at": _now_iso(),
        "depends_on": list(submission.depends_on),
    }
    append_task(task)

    # Legacy FR-01 audit line (audit.log) — kept so callers that read the
    # old journal still find a record.  The FR-08 journal (audit.jsonl) is
    # written below via `current_logger()`.
    _emit_audit(
        "submit",
        {"id": task_id, "command": submission.command, "name": submission.name},
        Path(os.environ.get("TASKQ_HOME", ".")),
    )

    # FR-08: structured JSONL audit emit with the invocation-scoped
    # `correlation_id`.  NFR-04 redaction runs on the `detail` payload
    # before the bytes reach the disk.
    current_logger().emit(
        "submit",
        task_id=task_id,
        detail={
            "command": submission.command,
            "name": submission.name,
            "depends_on": list(submission.depends_on),
        },
    )

    return {
        "id": task_id,
        "status": "pending",
        "command": submission.command,
        "name": submission.name,
    }


def run_cmd(
    task_id: Optional[str] = None,
    *,
    run_all: bool = False,
    use_cache: bool = False,
) -> dict:
    """Execute one or every pending task; return a dict, never print.

    [FR-02] [FR-03] [FR-04] [FR-05]
    Citations:
      - SPEC.md §3 FR-02 (single-task run, batch --all via ThreadPoolExecutor).
      - SPEC.md §3 FR-02 (timeout → exit 4; non-zero → exit 1; ok → exit 0).
      - SPEC.md#L113 (breaker OPEN → exit 3 + stderr `breaker open`).
      - SPEC.md §3 FR-04 (run <id> --cached: TTL-keyed replay within
        TASKQ_CACHE_TTL).
    """
    if run_all:
        exec_run_all()
        return {"ran_all": True, "exit_code": EXIT_OK}

    if not task_id:
        raise RunValidationError(
            "task_id required (or pass --all to execute every pending task)"
        )

    rec = find_by_id(task_id)
    if rec is None:
        raise RunValidationError(f"task id {task_id!r} does not exist")

    # Cache-aware single-task run: --cached enables HIT replay; without
    # --cached the executor still runs and a `done` result is written back
    # into cache.json so subsequent --cached runs can replay it.
    from taskq_plus.service.cache import execute_with_cache

    result = execute_with_cache(task_id, use_cache=bool(use_cache))
    if result is None:
        raise RunInternalError("execution returned no result")

    # The executor's run_with_retry returns the runner's exit code on every
    # path (cache HIT, breaker OPEN, timeout, ok, failed) so propagate it
    # verbatim — FR-02 (timeout=4) / FR-03 (breaker open=3) flow through.
    code = result.get("exit_code")
    if isinstance(code, int):
        out = dict(result)
        out["exit_code"] = code
        return out

    status = result.get("status")
    if status == "done":
        return {**result, "exit_code": EXIT_OK}
    if status == "timeout":
        return {**result, "exit_code": EXIT_TIMEOUT}
    return {**result, "exit_code": EXIT_FAILED}


def status_cmd(task_id: str) -> dict:
    """Return the full task record for `task_id`.

    [FR-01] [FR-05]
    Citations: SPEC.md §3 FR-05 (status outputs full fields of that task).
    """
    if not task_id:
        raise StatusValidationError("task_id required")
    rec = find_by_id(task_id)
    if rec is None:
        raise StatusValidationError(f"task id {task_id!r} does not exist")
    return dict(rec)


def list_cmd(status_filter: Optional[str] = None) -> dict:
    """List tasks, optionally filtered by status.

    [FR-01] [FR-05] [NFR-03]
    Citations:
      - SPEC.md §3 FR-05 (list [--status S] filter).
      - SPEC.md §7 NFR-03 (corrupted tasks.json → exit 1, no silent rebuild).
    """
    tasks = _strict_load_tasks()
    if status_filter is not None:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    return {"tasks": tasks, "count": len(tasks)}


def graph_cmd(format: str = "text") -> dict:  # noqa: A002 — match FR-05 keyword
    """Render the dependency graph in `text` or `dot` form.

    [FR-05] [FR-06]
    Citations: SPEC.md §3 FR-06 (graph --format text|dot).
    """
    tasks = _strict_load_tasks()
    fmt = format or "text"
    if fmt == "dot":
        lines = ["digraph tasks {"]
        for t in tasks:
            node_id = t.get("id", "")
            for dep in (t.get("depends_on") or []):
                lines.append(f'  "{dep}" -> "{node_id}";')
        lines.append("}")
        return {"format": "dot", "graph": "\n".join(lines)}

    # default — text rendering: one line per task, parent ids in `[...]`.
    lines = []
    for t in tasks:
        node_id = t.get("id", "")
        deps = ", ".join(t.get("depends_on") or []) or "(root)"
        lines.append(f"{node_id} <- [{deps}]")
    return {"format": "text", "graph": "\n".join(lines)}


def plugins_cmd(subcommand: str = "list") -> dict:
    """List loaded plugins (and their hooks).

    [FR-05] [FR-07] [NFR-02]
    Citations:
      - SPEC.md §3 FR-05 (plugins list).
      - SPEC.md §3 FR-07 (plugin allowlist regex; path-form names rejected; hook
        registration surfaced via `describe`).
    """
    if subcommand != "list":
        raise PluginValidationError(
            f"unknown plugins subcommand {subcommand!r}"
        )

    # Delegate the allowlist + import to service.plugins so the CLI and the
    # executor share one source of truth for the FR-07 regex, import path,
    # and exit-6 error contract (SAD §2 L4 plugins.py).
    try:
        from taskq_plus.service.plugins import (
            PLUGIN_NAME_RE,
            PluginLoadError as _ServicePluginLoadError,
            describe as _describe_plugins,
            load_plugins as _load_plugins,
        )
    except ImportError as exc:  # pragma: no cover — module is part of SAB.
        raise PluginLoadError(f"plugin service unavailable: {exc}") from exc

    # Parse the allowlist FIRST so the regex validator can reject path-form /
    # URL-form names with the documented exit-6 message (SPEC §3 FR-07).
    raw = os.environ.get("TASKQ_PLUGINS", "") or ""
    names = [piece.strip() for piece in raw.split(",") if piece.strip()]
    for name in names:
        if not PLUGIN_NAME_RE.match(name):
            raise PluginLoadError(
                f"plugin name {name!r} rejected by allowlist"
            )

    # Try the real import path (FR-07). If a name passes the regex but the
    # module is not installed, fall back to reporting it with empty hooks so
    # the CLI stays usable as a "what's in the allowlist?" probe (FR-05
    # backward-compat: `plugins_cmd` always returns a dict for valid names).
    try:
        loaded = _load_plugins()
        described = _describe_plugins(loaded)
    except _ServicePluginLoadError:
        described = [
            {"name": name, "hooks": [], "status": "missing"} for name in names
        ]

    return {"plugins": described, "count": len(described)}


def export_cmd(format: str = "json") -> dict:  # noqa: A002
    """Export task records as `json` / `csv` / `md` (FR-08 / NFR-04).

    [FR-05] [FR-08] [NFR-04]
    Citations:
      - SPEC.md §3 FR-05 (export --format json|csv|md).
      - SPEC.md §3 FR-08 (export writes task records to stdout;
        欄位同 status).
      - SPEC.md §8 #14 (三種格式 task 數與欄位一致).
      - SPEC.md §8 NFR-04 (CLI must redact `sk-…` / `token=…` / `Bearer …`
        pre-write; a downstream `grep -c "sk-"` on the emitted payload
        returns 0).
    """
    fmt = (format or "json").lower()
    if fmt not in {"json", "csv", "md"}:
        raise ExportValidationError(f"unsupported export format {fmt!r}")

    tasks = _strict_load_tasks()

    # FR-08: delegate the rendering to `observability.export` so the SAB-bound
    # module owns the json / csv / md serialisation, the EXPORT_FIELDS set,
    # and the NFR-04 redaction (applied before the bytes reach stdout).
    body = obs_export.export_tasks(tasks, fmt)

    # Project each task onto the canonical EXPORT_FIELDS set so the
    # `tasks` payload round-trips through `parse_export` byte-for-byte.
    exported_records = [obs_export._normalise_for_export(t) for t in tasks]
    return {"format": fmt, "content": body, "tasks": exported_records}


def clear_cmd() -> dict:
    """Remove every data file under $TASKQ_HOME.

    [FR-01] [FR-05]
    Citations: SPEC.md §3 FR-05 (clear — clear all data files).
    """
    home = Path(os.environ.get("TASKQ_HOME", ".")).resolve()
    removed: list[str] = []
    for filename in ("tasks.json", "cache.json", "breaker.json", "audit.log"):
        path = home / filename
        if path.exists():
            try:
                path.unlink()
                removed.append(filename)
            except OSError:
                # Best-effort: leave the file in place if the OS refuses.
                pass
    return {"cleared": True, "removed": removed}


# ---------------------------------------------------------------------------
# Legacy argparse-based dispatch — kept for test_fr01 (which imports
# `dispatch` / `main` from this module) and for `taskq_plus.__main__`'s
# backward-compatible entry point. The FR-05 click group in cli/main.py is
# the canonical user-facing path; this block is the safety net for direct
# `cli.commands.dispatch(...)` callers.
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser (subcommand dispatch only)."""
    parser = argparse.ArgumentParser(prog="taskq_plus", description="taskq-plus CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("submit", help="Submit a new task.")
    subparsers.add_parser("run", help="Execute pending tasks.")
    return parser


def _build_submit_parser() -> argparse.ArgumentParser:
    """Build the `submit` subcommand parser."""
    parser = argparse.ArgumentParser(prog="taskq_plus submit")
    parser.add_argument("command", help="Shell command to schedule.")
    parser.add_argument("--name", default=None, help="Optional human-friendly name.")
    parser.add_argument(
        "--after",
        action="append",
        default=[],
        dest="after",
        help="Task id this task depends on (repeatable).",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="Emit JSON output."
    )
    return parser


def _build_run_parser() -> argparse.ArgumentParser:
    """Build the `run` subcommand parser.

    [FR-02] [FR-04]
    Citations:
      - SPEC.md §3 FR-02 (run <id> | run --all).
      - SPEC.md §3 FR-04 (run <id> --cached: replay within TASKQ_CACHE_TTL).
    """
    parser = argparse.ArgumentParser(prog="taskq_plus run")
    parser.add_argument(
        "task_id", nargs="?", default=None, help="Task id to execute."
    )
    parser.add_argument(
        "--all", action="store_true", dest="run_all", help="Execute every pending task."
    )
    parser.add_argument(
        "--cached", action="store_true", dest="use_cache",
        help="Replay the cached result when TASKQ_CACHE_TTL has not elapsed.",
    )
    return parser


def _run(argv: Sequence[str]) -> int:
    """Execute the `run` subcommand.

    [FR-02] [FR-03] [FR-04]
    Citations:
      - SPEC.md §3 FR-02 (single-task run, batch --all via ThreadPoolExecutor).
      - SPEC.md §3 FR-02 (timeout → exit 4; exit 0 → exit 0; non-zero → exit 1).
      - SPEC.md#L108 (single-task run retries with exponential backoff).
      - SPEC.md#L113 (breaker OPEN → exit 3 + stderr `breaker open`).
      - SPEC.md §3 FR-04 (run <id> --cached: cache-aware replay; otherwise
        the executor's `done` result is still written back to cache.json).
    """
    parser = _build_run_parser()
    args = parser.parse_args(list(argv))

    if args.run_all:
        exec_run_all()
        return EXIT_OK
    if not args.task_id:
        print(
            "error: task_id required (or pass --all to execute every pending task)",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_ERROR

    # Cache-aware single-task run: --cached enables HIT replay; without
    # --cached the executor still runs and a `done` result is written back
    # into cache.json so subsequent --cached runs can replay it.
    from taskq_plus.service.cache import execute_with_cache

    result = execute_with_cache(args.task_id, use_cache=bool(args.use_cache))
    if result is None:
        return EXIT_FAILED
    # Propagate the runner's exit code verbatim so FR-02 (timeout=4) and
    # FR-03 (breaker open=3) outcomes flow through unchanged; a HIT path
    # carries exit_code=0 from the cached payload.
    code = result.get("exit_code")
    if isinstance(code, int):
        return code
    status = result.get("status")
    if status == "done":
        return EXIT_OK
    if status == "timeout":
        return EXIT_TIMEOUT
    return EXIT_FAILED


def _submit(
    argv: Sequence[str], taskq_home: Optional[Path] = None
) -> int:
    """Execute the `submit` subcommand.

    [FR-01]
    Citations: SPEC.md §3 FR-01 (TaskSubmission validation, id generation,
    storage write, audit emit, --json output).
    """
    parser = _build_submit_parser()
    args = parser.parse_args(list(argv))

    # Build the pydantic model — every validation rule lives here.
    try:
        submission = TaskSubmission(
            command=args.command,
            name=args.name,
            depends_on=list(args.after),
        )
    except ValidationError as exc:
        print(f"error: {exc.errors()[0]['msg']}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    # Name uniqueness (pending/running tasks).
    if submission.name is not None and find_by_name(submission.name) is not None:
        print(
            f"error: name {submission.name!r} already used by a pending/running task",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_ERROR

    # Dependency existence.
    for dep_id in submission.depends_on:
        if find_by_id(dep_id) is None:
            print(
                f"error: dependency task id {dep_id!r} does not exist",
                file=sys.stderr,
            )
            return EXIT_NOT_FOUND

    task_id = generate_task_id()
    task = {
        "id": task_id,
        "command": submission.command,
        "name": submission.name,
        "status": "pending",
        "created_at": _now_iso(),
        "depends_on": list(submission.depends_on),
    }
    append_task(task)

    home = taskq_home or Path.cwd()
    _emit_audit(
        "submit",
        {"id": task_id, "command": submission.command, "name": submission.name},
        home,
    )

    if args.as_json:
        print(json.dumps({"id": task_id, "status": "pending"}))
    else:
        print(task_id)
    return EXIT_OK


def dispatch(argv: Sequence[str], taskq_home: Optional[Path] = None) -> int:
    """Dispatch argv to the correct subcommand.

    [FR-01] [FR-02] [FR-04]
    Citations: SPEC.md §3 FR-01, §3 FR-02, §3 FR-04.
    """
    if not argv:
        _build_parser().print_help()
        return EXIT_OK
    sub = argv[0]
    if sub == "submit":
        return _submit(argv[1:], taskq_home=taskq_home)
    if sub == "run":
        return _run(argv[1:])
    print(f"error: unknown command {sub!r}", file=sys.stderr)
    return EXIT_VALIDATION_ERROR


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Programmatic entry point used by `__main__`.

    [FR-01] [FR-02] [NFR-05]
    Citations: SPEC.md §3 FR-01, §3 FR-02.
    """
    if argv is None:
        argv = sys.argv[1:]
    return dispatch(argv)


__all__ = [
    # FR-05 handlers
    "submit_cmd",
    "run_cmd",
    "status_cmd",
    "list_cmd",
    "graph_cmd",
    "plugins_cmd",
    "export_cmd",
    "clear_cmd",
    # Exception taxonomy
    "HandlerError",
    "SubmitValidationError",
    "GraphError",
    "RunValidationError",
    "RunInternalError",
    "StatusValidationError",
    "StoreCorrupted",
    "ExportValidationError",
    "PluginValidationError",
    "PluginLoadError",
    # Legacy dispatch (kept for test_fr01)
    "dispatch",
    "main",
    "_submit",
    "_run",
    # Constants
    "EXIT_OK",
    "EXIT_FAILED",
    "EXIT_VALIDATION_ERROR",
    "EXIT_NOT_FOUND",
    "EXIT_TIMEOUT",
]
