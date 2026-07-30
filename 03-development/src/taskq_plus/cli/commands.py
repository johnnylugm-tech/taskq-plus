"""CLI command implementations.

[FR-01]
Citations: SPEC.md §3 FR-01 (submit command, validation, audit emit).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from pydantic import ValidationError

from taskq_plus.models.task import TaskSubmission, generate_task_id
from taskq_plus.storage.task_store import (
    append_task,
    find_by_id,
    find_by_name,
    load_tasks,
)


EXIT_OK = 0
EXIT_VALIDATION_ERROR = 2
EXIT_NOT_FOUND = 3

UTC = dt.timezone.utc


def _now_iso() -> str:
    """UTC ISO-8601 timestamp with 'Z' suffix."""
    return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser (subcommand dispatch only)."""
    parser = argparse.ArgumentParser(prog="taskq_plus", description="taskq-plus CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("submit", help="Submit a new task.")
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

    [FR-01]
    Citations: SPEC.md §3 FR-01.
    """
    if not argv:
        _build_parser().print_help()
        return EXIT_OK
    sub = argv[0]
    if sub == "submit":
        return _submit(argv[1:], taskq_home=taskq_home)
    print(f"error: unknown command {sub!r}", file=sys.stderr)
    return EXIT_VALIDATION_ERROR


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Programmatic entry point used by `__main__`.

    [FR-01] [NFR-05]
    Citations: SPEC.md §3 FR-01.
    """
    if argv is None:
        argv = sys.argv[1:]
    return dispatch(argv)
