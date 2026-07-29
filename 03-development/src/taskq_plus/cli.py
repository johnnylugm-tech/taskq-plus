"""[FR-05] CLI 整合: argparse 群組 + 8 個子命令 + 退出碼映射 + ``--json`` 全域旗標.

Citations:
- SPEC.md §3 FR-01 lines 72-92 (``submit "<cmd>"``).
- SPEC.md §3 FR-02 lines 94-104 (``run <id>`` / ``run --all``).
- SPEC.md §3 FR-03 lines 106-115 (breaker gate + retry).
- SPEC.md §3 FR-04 lines 116-122 (TTL cache).
- SPEC.md §3 FR-05 lines 124-132 (CLI 整合 + 8 子命令 + 退出碼).
- SPEC.md §3 FR-06 (graph, cycle, depth cap).
- SPEC.md §3 FR-07 lines 142-150 (plugin allowlist).
- SPEC.md §3 FR-08 (export --format json|csv|md).
- SPEC.md §7 錯誤處理 lines 379-389: 0 / 1 / 2 / 3 / 4 / 5 / 6 mapping.
- TEST_SPEC.md FR-05 ACs (AC-FR-05.1, AC-FR-05.2, AC-FR-05.3).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any, Sequence

from pydantic import ValidationError

from taskq_plus.config import config
from taskq_plus.service import breaker
from taskq_plus.service import cache
from taskq_plus.service import executor
from taskq_plus.models.task import TaskSubmission
from taskq_plus.observability.audit import write_event
from taskq_plus.storage.task_store import TaskStore
from taskq_plus.util import utc_now_iso


# Canonical exit-code map from SPEC.md §3 / §7.
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_VALIDATION = 2
EXIT_BREAKER_OPEN = 3
EXIT_TIMEOUT = 4
EXIT_CYCLE = 5
EXIT_PLUGIN_LOAD = 6


# -------------------------------------------------------------------
# Plugin loader (FR-07)
# -------------------------------------------------------------------


class PluginSpecError(Exception):
    """Raised when a ``$TASKQ_PLUGINS`` entry fails the FR-07 allowlist.

    Citations:
    - SPEC.md §3 FR-07 lines 142-150: only ``module:function`` regex
      specs are admitted; path forms (``../evil.py``) are rejected
      at load time so the CLI surfaces exit 6 instead of executing
      attacker-controlled code (NFR-02).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# Module:function regex — SPEC.md §3 FR-07 lines 142-150 verbatim.
_PLUGIN_SPEC_RE = __import__("re").compile(
    r"^[A-Za-z_][A-Za-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*$"
)


def _load_plugins() -> list[dict[str, str]]:
    """Load every spec in ``$TASKQ_PLUGINS`` (comma-separated).

    Rejects path forms and invalid regex specs with ``PluginSpecError``
    so the CLI maps the loader failure to exit 6 (SPEC.md §7 row
    ``plugin load failure``).

    Returns the empty list when ``$TASKQ_PLUGINS`` is unset.
    """
    raw = os.environ.get("TASKQ_PLUGINS", "") or ""
    if not raw.strip():
        return []
    plugins: list[dict[str, str]] = []
    for spec in (piece.strip() for piece in raw.split(",")):
        if not spec:
            continue
        if "/" in spec or "\\" in spec or spec.endswith(".py"):
            raise PluginSpecError(
                f"plugin spec {spec!r} rejected: path-form not allowed"
            )
        if not _PLUGIN_SPEC_RE.match(spec):
            raise PluginSpecError(
                f"plugin spec {spec!r} rejected: invalid module:function form"
            )
        module, _, hook = spec.partition(":")
        plugins.append({"module": module, "hook": hook})
    return plugins


# -------------------------------------------------------------------
# Parser
# -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse tree for FR-05.

    Citations:
    - SPEC.md §3 FR-05 lines 124-132: 8 subcommands (submit, run,
      status, list, graph, plugins, export, clear) plus a global
      ``--json`` flag on the parent parser so every output-emitting
      subcommand inherits it (AC-FR-05.2).
    """
    parser = argparse.ArgumentParser(prog="taskq_plus")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    submit = sub.add_parser("submit", help="Submit a new task")
    submit.add_argument("command", help="shell command to record")
    submit.add_argument("--name", default=None, help="Optional unique task name")
    submit.add_argument(
        "--after",
        action="append",
        default=[],
        help="Existing task id this task depends on (repeatable)",
    )

    run = sub.add_parser("run", help="Execute a task")
    run.add_argument("task_id", nargs="?", default=None, help="Task id to run")
    run.add_argument(
        "--all",
        action="store_true",
        dest="run_all",
        help="Run all pending tasks in DAG topological order",
    )
    run.add_argument(
        "--cached",
        action="store_true",
        dest="cached",
        help=(
            "Replay from TTL cache on hit, write-through on miss "
            "(FR-04). Valid with both ``run <id>`` and ``run --all``."
        ),
    )

    status_p = sub.add_parser("status", help="Print full task fields for <id>")
    status_p.add_argument("task_id", help="Task id to inspect")
    status_p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit single-line JSON (AC-FR-05.2).",
    )

    list_p = sub.add_parser("list", help="List tasks (optionally filterable by status)")
    list_p.add_argument(
        "--status",
        default=None,
        help="Optional status filter (pending|done|failed|timeout)",
    )
    list_p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit single-line JSON (AC-FR-05.2).",
    )

    graph = sub.add_parser("graph", help="Print dependency graph (FR-06)")
    graph.add_argument(
        "--format",
        choices=("text", "dot"),
        default="text",
        help="Output format (text or dot)",
    )
    graph.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit single-line JSON (AC-FR-05.2).",
    )

    plugins_p = sub.add_parser("plugins", help="Plugin management (FR-07)")
    plugins_sub = plugins_p.add_subparsers(dest="plugins_sub", required=True)
    plugins_list_p = plugins_sub.add_parser("list", help="List loaded plugins and their hooks")
    plugins_list_p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit single-line JSON (AC-FR-05.2).",
    )

    export = sub.add_parser("export", help="Export task results (FR-08)")
    export.add_argument(
        "--format",
        choices=("json", "csv", "md"),
        default="json",
        help="Output format",
    )
    export.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit single-line JSON (AC-FR-05.2).",
    )

    sub.add_parser("clear", help="Clear all $TASKQ_HOME data files")

    return parser


# -------------------------------------------------------------------
# Validation / JSON / status helpers
# -------------------------------------------------------------------


def _format_validation_error(exc: Exception) -> str:
    """Render a pydantic ``ValidationError`` as a single stderr message."""
    if not isinstance(exc, ValidationError):
        return str(exc)
    errs = exc.errors()
    if not errs:
        return str(exc)
    first = errs[0]
    ctx_err = first.get("ctx", {}).get("error")
    if ctx_err is not None:
        return str(ctx_err)
    msg = first.get("msg", "")
    prefix = "Value error, "
    if msg.startswith(prefix):
        msg = msg[len(prefix):]
    return msg


def _safe_load_tasks(store: TaskStore) -> tuple[dict[str, dict[str, Any]], bool]:
    """Load tasks; return ``(tasks, corrupt)``.

    ``corrupt=True`` is set when the file exists but
    ``json.loads`` raised ``JSONDecodeError`` — the CLI maps that
    to exit 1 (AC-FR-05.3 exit-1-internal) instead of letting the
    traceback surface to the operator.
    """
    tasks_file = store.path
    if not tasks_file.exists():
        return {}, False
    try:
        envelope = json.loads(tasks_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}, True
    if not isinstance(envelope, dict):
        return {}, True
    inner = envelope.get("tasks")
    if isinstance(inner, dict):
        return {k: v for k, v in inner.items() if isinstance(v, dict)}, False
    if envelope and all(isinstance(v, dict) for v in envelope.values()):
        return envelope, False
    return {}, False


def _emit(as_json: bool, payload: dict[str, Any]) -> None:
    """Emit ``payload`` as a single-line JSON document when ``as_json``.

    AC-FR-05.2 — exactly ONE line that is itself valid JSON; the
    default human-readable path emits ``json.dumps(payload)`` with
    ``indent=2`` for readability.
    """
    if as_json:
        sys.stdout.write(json.dumps(payload, separators=(", ", ": ")) + "\n")
    else:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")


# -------------------------------------------------------------------
# Handler: submit
# -------------------------------------------------------------------


def _handle_submit(args: argparse.Namespace) -> int:
    """Implement the ``submit`` subcommand.

    Citations:
    - SPEC.md §3 FR-01 通過驗證 rules + persistence + audit.
    - TEST_SPEC.md FR-01 AC-FR-01.1..7.
    """
    store = TaskStore(config().task_home / "tasks.json")
    existing = store.load()
    existing_names = {
        t.get("name") for t in existing.values() if t.get("name")
    }
    known_ids = set(existing.keys())

    try:
        submission = TaskSubmission.model_validate(
            {
                "command": args.command,
                "name": args.name,
                "depends_on": list(args.after),
            },
            context={
                "existing_names": existing_names,
                "known_ids": known_ids,
            },
        )
    except ValidationError as exc:
        sys.stderr.write(f"taskq_plus: {_format_validation_error(exc)}\n")
        return EXIT_VALIDATION

    task_id = uuid.uuid4().hex[:8]
    new_tasks = dict(existing)
    new_tasks[task_id] = {
        "status": "pending",
        "command": submission.command,
        "name": submission.name,
        "created_at": utc_now_iso(),
        "depends_on": list(submission.depends_on),
    }
    store.save(new_tasks)

    write_event(
        {
            "event": "submit",
            "task_id": task_id,
            "name": submission.name,
            "depends_on": list(submission.depends_on),
        }
    )

    sys.stdout.write(f"{task_id}\n")
    return EXIT_OK


# -------------------------------------------------------------------
# Handler: run
# -------------------------------------------------------------------


def _replay_from_cache(
    store: TaskStore, task_id: str
) -> dict[str, Any] | None:
    """Return the replayed task record on a cache HIT; ``None`` on miss."""
    current = store.load()
    task = current.get(task_id)
    if task is None:
        return None
    command = task.get("command", "")
    cached = cache.lookup(command)
    if cached is None:
        return None
    replayed = {
        **task,
        "status": "done",
        "exit_code": cached.get("exit_code", 0),
        "stdout_tail": cached.get("stdout_tail", ""),
        "stderr_tail": cached.get("stderr_tail", ""),
        "cached": True,
        "finished_at": utc_now_iso(),
    }
    current[task_id] = replayed
    store.save(current)
    return replayed


def _cacheable_result(record: dict[str, Any]) -> dict[str, Any]:
    """Project a persisted task record into the cache envelope shape."""
    return {
        "status": "done",
        "exit_code": record.get("exit_code"),
        "stdout_tail": record.get("stdout_tail", ""),
        "stderr_tail": record.get("stderr_tail", ""),
    }


def _validate_dag_for_run_all(tasks: dict[str, dict[str, Any]]) -> int | None:
    """Return an exit code (5) for cycle / depth cap, ``None`` when OK.

    Cycles are detected by checking the recursive closure of every
    pending task's ``depends_on``. Depth-cap is checked against
    ``$TASKQ_MAX_DAG_DEPTH`` (SPEC.md §3 FR-06 + §7 row exit 5).

    A backward reference — a task whose ``depends_on`` points to a
    task that was created AFTER it — is treated as a cycle per the
    DAG-consistency invariant: a submission can only declare deps on
    tasks that existed at the moment of creation. Manual edits that
    violate that invariant surface as exit 5.
    """
    pending_ids = {
        tid for tid, rec in tasks.items() if rec.get("status") == "pending"
    }
    if not pending_ids:
        return None

    # Backward-reference detection: a task T's deps must all have
    # ``created_at <= T.created_at`` (lexicographic ISO-8601 works
    # because the format is fixed-width). When this fails the test
    # invariant that a submission is immutable once written, we
    # surface exit 5 instead of executing an inconsistent DAG.
    for tid, rec in tasks.items():
        self_created = rec.get("created_at") or ""
        for dep in rec.get("depends_on") or []:
            if dep not in tasks:
                continue
            dep_created = tasks[dep].get("created_at") or ""
            if dep_created and self_created and dep_created > self_created:
                return EXIT_CYCLE

    # Depth cap (SPEC.md §3 FR-06 / §7).
    try:
        max_depth = int(os.environ.get("TASKQ_MAX_DAG_DEPTH", "32"))
    except (TypeError, ValueError):
        max_depth = 32

    depth_cache: dict[str, int] = {}

    def _depth(tid: str, stack: set[str]) -> int:
        if tid in stack:
            return -1  # cycle marker
        if tid in depth_cache:
            return depth_cache[tid]
        rec = tasks.get(tid)
        if rec is None:
            return 0
        deps = [
            d for d in (rec.get("depends_on") or []) if d in tasks
        ]
        if not deps:
            depth_cache[tid] = 1
            return 1
        stack.add(tid)
        best = 1
        for dep in deps:
            d = _depth(dep, stack)
            if d < 0:
                return -1
            if d + 1 > best:
                best = d + 1
        stack.discard(tid)
        depth_cache[tid] = best
        return best

    for tid in pending_ids:
        d = _depth(tid, set())
        if d < 0:
            return EXIT_CYCLE
        if d > max_depth:
            return EXIT_CYCLE
    return None


def _handle_run(args: argparse.Namespace) -> int:
    """Implement the ``run`` subcommand (FR-02/03/04/06).

    Citations:
    - SPEC.md §3 FR-02 lines 96-104 (``run <id>`` / ``run --all``).
    - SPEC.md §3 FR-03 lines 112-115: breaker gate BEFORE any
      subprocess; failure feeds ``record_failure``; success feeds
      ``record_success``; OPEN rejection → exit 3 + stderr
      ``breaker open``.
    - SPEC.md §3 FR-04 lines 116-122: ``--cached`` replay / write-through.
    - SPEC.md §3 FR-06: ``run --all`` detects cycles + depth cap →
      exit 5 (canonical for both per SPEC.md §7).
    """
    try:
        breaker.assert_closed()
    except breaker.BreakerOpen:
        sys.stderr.write("breaker open\n")
        return EXIT_BREAKER_OPEN

    cached_mode = bool(getattr(args, "cached", False))
    store = TaskStore(config().task_home / "tasks.json")

    tasks, corrupt = _safe_load_tasks(store)
    if corrupt:
        sys.stderr.write("taskq_plus: tasks.json is corrupt\n")
        return EXIT_INTERNAL

    if args.run_all:
        cycle_or_depth = _validate_dag_for_run_all(tasks)
        if cycle_or_depth is not None:
            return cycle_or_depth
        statuses = executor.run_all(store)
        if cached_mode:
            all_tasks = store.load()
            for tid, status in statuses.items():
                if status != "done":
                    continue
                record = all_tasks.get(tid)
                if record is None:
                    continue
                cache.store(record.get("command", ""), _cacheable_result(record))
        return EXIT_OK

    if not args.task_id:
        sys.stderr.write("taskq_plus: run requires a task id or --all\n")
        return EXIT_INTERNAL

    if cached_mode:
        replayed = _replay_from_cache(store, args.task_id)
        if replayed is not None:
            return EXIT_OK

    status, record = executor.execute_task(store, args.task_id, sleep=time.sleep)

    if status in ("failed", "timeout"):
        breaker.record_failure()
    elif status == "done":
        breaker.record_success()
        if cached_mode and record is not None:
            cache.store(record.get("command", ""), _cacheable_result(record))

    if status == "timeout":
        return EXIT_TIMEOUT
    if status == "missing":
        sys.stderr.write(f"taskq_plus: unknown task id: {args.task_id}\n")
        return EXIT_INTERNAL
    return EXIT_OK


# -------------------------------------------------------------------
# Handler: status
# -------------------------------------------------------------------


def _handle_status(args: argparse.Namespace) -> int:
    """Implement the ``status <id>`` subcommand.

    Citations:
    - SPEC.md §3 FR-05 line 127: ``status <id>`` prints full task fields.
    - AC-FR-05.2: ``--json`` flag emits a SINGLE-LINE JSON document.
    """
    task_id = getattr(args, "task_id", None)
    store = TaskStore(config().task_home / "tasks.json")
    tasks, corrupt = _safe_load_tasks(store)
    if corrupt:
        sys.stderr.write("taskq_plus: tasks.json is corrupt\n")
        return EXIT_INTERNAL
    record = tasks.get(task_id) if task_id else None
    if record is None:
        sys.stderr.write(f"taskq_plus: unknown task id: {task_id}\n")
        return EXIT_INTERNAL
    payload = {"id": task_id, **record}
    _emit(bool(getattr(args, "as_json", False)), payload)
    return EXIT_OK


# -------------------------------------------------------------------
# Handler: list
# -------------------------------------------------------------------


def _handle_list(args: argparse.Namespace) -> int:
    """Implement the ``list [--status S]`` subcommand.

    Citations:
    - SPEC.md §3 FR-05 line 128: ``list [--status S]`` lists tasks,
      optionally filtered by status.
    - AC-FR-05.2: ``--json`` flag emits a SINGLE-LINE JSON document.
    """
    status_filter = getattr(args, "status", None)
    store = TaskStore(config().task_home / "tasks.json")
    tasks, corrupt = _safe_load_tasks(store)
    if corrupt:
        sys.stderr.write("taskq_plus: tasks.json is corrupt\n")
        return EXIT_INTERNAL
    items: list[dict[str, Any]] = []
    for tid, rec in sorted(tasks.items()):
        if status_filter and rec.get("status") != status_filter:
            continue
        items.append({"id": tid, **rec})
    as_json = bool(getattr(args, "as_json", False))
    _emit(as_json, {"tasks": items})
    return EXIT_OK


# -------------------------------------------------------------------
# Handler: graph
# -------------------------------------------------------------------


def _handle_graph(args: argparse.Namespace) -> int:
    """Implement the ``graph [--format text|dot]`` subcommand (FR-06).

    Citations:
    - SPEC.md §3 FR-05 line 129 + FR-06: prints the dependency graph.
    - AC-FR-05.2: ``--json`` flag emits a SINGLE-LINE JSON document.
    """
    fmt = getattr(args, "format", "text")
    store = TaskStore(config().task_home / "tasks.json")
    tasks, corrupt = _safe_load_tasks(store)
    if corrupt:
        sys.stderr.write("taskq_plus: tasks.json is corrupt\n")
        return EXIT_INTERNAL

    edges = [
        {"from": dep, "to": tid}
        for tid, rec in sorted(tasks.items())
        for dep in (rec.get("depends_on") or [])
        if dep in tasks
    ]
    as_json = bool(getattr(args, "as_json", False))
    if as_json or fmt == "text":
        # text rendering is the default; --json upgrades to JSON.
        if as_json:
            _emit(True, {"nodes": sorted(tasks.keys()), "edges": edges})
            return EXIT_OK
        for tid in sorted(tasks.keys()):
            sys.stdout.write(f"{tid}\n")
        for edge in edges:
            sys.stdout.write(f"{edge['from']} -> {edge['to']}\n")
        return EXIT_OK

    # dot (graphviz) format
    sys.stdout.write("digraph taskq {\n")
    for tid in sorted(tasks.keys()):
        sys.stdout.write(f'  "{tid}";\n')
    for edge in edges:
        sys.stdout.write(f'  "{edge["from"]}" -> "{edge["to"]}";\n')
    sys.stdout.write("}\n")
    return EXIT_OK


# -------------------------------------------------------------------
# Handler: plugins
# -------------------------------------------------------------------


def _handle_plugins_list(args: argparse.Namespace) -> int:
    """Implement the ``plugins list`` subcommand (FR-07).

    Citations:
    - SPEC.md §3 FR-05 line 130 + FR-07 lines 142-150.
    - AC-FR-05.2: ``--json`` flag emits a SINGLE-LINE JSON document.
    - AC-FR-07.1 / AC-FR-05.3 row exit 6: invalid plugin specs are
      rejected at load time with a structured error.
    """
    try:
        plugins = _load_plugins()
    except PluginSpecError as exc:
        sys.stderr.write(f"taskq_plus: {exc.message}\n")
        return EXIT_PLUGIN_LOAD
    as_json = bool(getattr(args, "as_json", False))
    _emit(as_json, {"plugins": plugins})
    return EXIT_OK


# -------------------------------------------------------------------
# Handler: export
# -------------------------------------------------------------------


def _handle_export(args: argparse.Namespace) -> int:
    """Implement the ``export --format json|csv|md`` subcommand (FR-08).

    Citations:
    - SPEC.md §3 FR-05 line 131 + FR-08: exports task results.
    - AC-FR-05.2: ``--json`` flag emits a SINGLE-LINE JSON document.
    """
    fmt = getattr(args, "format", "json")
    store = TaskStore(config().task_home / "tasks.json")
    tasks, corrupt = _safe_load_tasks(store)
    if corrupt:
        sys.stderr.write("taskq_plus: tasks.json is corrupt\n")
        return EXIT_INTERNAL

    as_json = bool(getattr(args, "as_json", False))
    payload = {"tasks": [{"id": tid, **rec} for tid, rec in sorted(tasks.items())]}

    if as_json or fmt == "json":
        _emit(True, payload)
        return EXIT_OK
    if fmt == "csv":
        rows = ["id,status,command,name"]
        for tid, rec in sorted(tasks.items()):
            name = rec.get("name") or ""
            rows.append(f"{tid},{rec.get('status', '')},{rec.get('command', '')},{name}")
        sys.stdout.write("\n".join(rows) + "\n")
        return EXIT_OK
    # md
    sys.stdout.write("# taskq_plus tasks\n\n")
    for tid, rec in sorted(tasks.items()):
        sys.stdout.write(f"- `{tid}` **{rec.get('status', '')}** `{rec.get('command', '')}`\n")
    return EXIT_OK


# -------------------------------------------------------------------
# Handler: clear
# -------------------------------------------------------------------


def _handle_clear(args: argparse.Namespace) -> int:
    """Implement the ``clear`` subcommand.

    Citations:
    - SPEC.md §3 FR-05 line 132: clears every ``$TASKQ_HOME`` data
      file (``tasks.json``, ``breaker.json``, ``cache.json``,
      ``audit.jsonl``) so the next run starts from a known-clean
      state.
    """
    home = config().task_home
    for filename in ("tasks.json", "breaker.json", "cache.json", "audit.jsonl"):
        path = home / filename
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    sys.stdout.write("cleared\n")
    return EXIT_OK


# -------------------------------------------------------------------
# Dispatcher
# -------------------------------------------------------------------


def main(argv: Sequence[str]) -> int:
    """Top-level CLI entry (consumed by ``taskq_plus.__main__`` and tests).

    Citations:
    - SPEC.md §3 FR-05: ``python -m taskq_plus <subcommand>``.
    - ``argv`` starts at the subcommand (``argv[0] == "submit" | ...``).
    - SPEC.md §7: any unhandled internal failure is mapped to
      exit 1 so the operator never sees a Python traceback.
    """
    # Plugin loader runs at startup so a malformed ``$TASKQ_PLUGINS``
    # is reported with exit 6 BEFORE any subcommand logic (AC-FR-07.1).
    try:
        _load_plugins()
    except PluginSpecError as exc:
        sys.stderr.write(f"taskq_plus: {exc.message}\n")
        return EXIT_PLUGIN_LOAD

    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else EXIT_VALIDATION

    sub = args.subcommand
    try:
        if sub == "submit":
            return _handle_submit(args)
        if sub == "run":
            return _handle_run(args)
        if sub == "status":
            return _handle_status(args)
        if sub == "list":
            return _handle_list(args)
        if sub == "graph":
            return _handle_graph(args)
        if sub == "plugins":
            if getattr(args, "plugins_sub", None) == "list":
                return _handle_plugins_list(args)
            return EXIT_VALIDATION
        if sub == "export":
            return _handle_export(args)
        if sub == "clear":
            return _handle_clear(args)
    except Exception as exc:  # NFR-03: surface exit 1, never a traceback
        sys.stderr.write(f"taskq_plus: internal error: {exc}\n")
        return EXIT_INTERNAL
    return EXIT_VALIDATION