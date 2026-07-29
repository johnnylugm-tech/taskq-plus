"""[FR-01] CLI entry surface.

Citations:
- SPEC.md §3 FR-01 (任務提交與驗證) lines 72-92
- TEST_SPEC.md FR-01 ACs (AC-FR-01.1 .. AC-FR-01.7)
- SPEC.md §7 錯誤處理 (行 379-389): validation failures → exit 2
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Sequence

from pydantic import ValidationError

from taskq_plus.config import config
from taskq_plus.models.task import TaskSubmission
from taskq_plus.observability.audit import write_event
from taskq_plus.storage.task_store import TaskStore
from taskq_plus.util import utc_now_iso


EXIT_OK = 0
EXIT_VALIDATION = 2


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse tree.

    Citations:
    - SPEC.md §3 FR-01: ``submit "<command>" [--name NAME]
      [--after ID]...``
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
    return parser


def _format_validation_error(exc: Exception) -> str:
    """Render a pydantic ``ValidationError`` as a single stderr message.

    Strips the ``Value error, `` prefix pydantic prepends so the
    surfaced message matches the spec-canonical text (e.g.
    ``unknown dependency: deadbeef``).
    """
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
    except ValidationError as exc:  # SPEC.md §7 row: validation → exit 2
        sys.stderr.write(
            f"taskq_plus: {_format_validation_error(exc)}\n"
        )
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


def main(argv: Sequence[str]) -> int:
    """Top-level CLI entry (consumed by ``taskq_plus.__main__`` and tests).

    Citations:
    - SPEC.md §3 FR-01: ``python -m taskq_plus submit ...``
    - ``argv`` starts at the subcommand (``argv[0] == "submit"``).
    """
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    if args.subcommand == "submit":
        return _handle_submit(args)
    # argparse ``add_subparsers(required=True)`` guarantees
    # ``args.subcommand`` is set — no defensive fallback (rule 4).
    return 1
