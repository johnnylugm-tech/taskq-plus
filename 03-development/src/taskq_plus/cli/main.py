"""taskq_plus.cli.main — click subcommand group + global --json + exit-code map.

This module owns the FR-05 user-facing entry point. The 8 subcommand handlers
live in `taskq_plus.cli.commands` and return plain dicts — they NEVER print.
`cli.main.render` is the single stdout rendering path so the `--json` flag
behaves identically across every subcommand.

[FR-05]
Citations:
  - SPEC.md §3 FR-05 (eight-subcommand click group; --json; exit-code map).
  - SPEC.md §3 FR-01 (submit, status, list, clear).
  - SPEC.md §3 FR-02 (run / run --all; FR-02 exit-code propagation).
  - SPEC.md §3 FR-03 (run rejected with exit 3 while breaker is OPEN).
  - SPEC.md §3 FR-04 (run <id> --cached; cache-aware replay).
  - SPEC.md §3 FR-06 (graph --format text|dot).
  - SPEC.md §3 FR-07 (plugins list — allowlist regex rejects path-form names).
  - SPEC.md §3 FR-08 (export --format json|csv|md; NFR-04 secret redaction).
  - SPEC.md §7 (exit-code map: 0/1/2/3/4/5/6).
  - SRS.md §5 (exit-code map cross-reference).
"""

from __future__ import annotations

import json
import sys
from typing import Optional, Sequence

import click

from taskq_plus.cli import commands


# ---------------------------------------------------------------------------
# Public exit-code constants (SPEC §3 FR-05 / §7) — re-exported so the
# click wrapper can map handler exceptions / payloads onto the right codes.
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_VALIDATION = 2
EXIT_BREAKER_OPEN = 3
EXIT_TIMEOUT = 4
EXIT_GRAPH_ERROR = 5
EXIT_PLUGIN_LOAD_FAILED = 6


# ---------------------------------------------------------------------------
# render — the single stdout rendering path.
#
# Handlers return plain dicts; this function is the ONLY writer of stdout.
# Behaviour:
#   * as_json=True  → one line of JSON (`json.dumps(payload)`).
#   * as_json=False → human-readable rendering:
#       - payload["content"]  → verbatim (csv / md / graph bodies).
#       - payload["id"]       → the 8-hex id (submit, status, …).
#       - payload["tasks"]    → id+status one-per-line.
#       - payload["plugins"]  → name one-per-line.
#       - payload["graph"]    → verbatim text body.
#       - else                → `json.dumps(payload, indent=2)`.
# ---------------------------------------------------------------------------
def render(payload: dict, as_json: bool) -> None:
    """Write `payload` to stdout via the FR-05 single rendering path.

    [FR-05]
    Citations: SPEC.md §3 FR-05 (--json emits single-line JSON).
    """
    if as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
        sys.stdout.write("\n")
        return

    if not isinstance(payload, dict):
        sys.stdout.write(str(payload))
        sys.stdout.write("\n")
        return

    if "content" in payload:
        body = payload["content"]
        sys.stdout.write(body)
        if not body.endswith("\n"):
            sys.stdout.write("\n")
        return

    if "graph" in payload:
        sys.stdout.write(str(payload["graph"]))
        sys.stdout.write("\n")
        return

    if "id" in payload:
        sys.stdout.write(str(payload["id"]))
        sys.stdout.write("\n")
        return

    if "tasks" in payload:
        for t in payload["tasks"]:
            tid = t.get("id", "")
            status = t.get("status", "")
            sys.stdout.write(f"{tid} {status}\n")
        return

    if "plugins" in payload:
        for p in payload["plugins"]:
            sys.stdout.write(f"{p.get('name', '')}\n")
        return

    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# argv preprocessor — strip `--json` wherever it appears.
# The click group accepts --json via the test_fr05 in-process flow
# (`["--json", "submit", "..."]`); the legacy argparse path used by
# test_fr01 accepts it after the subcommand (`["submit", "...", "--json"]`).
# Normalising here keeps both call sites first-class.
# ---------------------------------------------------------------------------
def _extract_json(argv: Sequence[str]) -> tuple[bool, list[str]]:
    """Return (as_json_flag_seen, remaining_argv)."""
    as_json = False
    rest: list[str] = []
    for a in argv:
        if a == "--json":
            as_json = True
        else:
            rest.append(a)
    return as_json, rest


# ---------------------------------------------------------------------------
# Click subcommand helpers — pull `as_json` out of ctx.obj and call render().
# ---------------------------------------------------------------------------
def _emit_error(msg: str) -> None:
    """Emit `msg` to stderr (handlers never print; the click wrapper owns it)."""
    click.echo(f"error: {msg}", err=True)


def _propagate_json(ctx, _param, value):
    """click callback: set ctx.obj['json']=True when --json is given.

    Returning `value` keeps the option's behavior visible (the flag is still
    on the command line) but `expose_value=False` (see `_json_flag`) drops
    the parameter from the handler signature so handlers never see it.
    """
    if ctx is not None and ctx.obj is not None and value:
        ctx.obj["json"] = True
    return value


def _json_flag():
    """Reusable `--json` click option for the per-subcommand alias.

    The global `--json` flag is parsed by `_extract_json` in `main()` and
    seeds `ctx.obj["json"]`. The per-subcommand `--json` alias (e.g.
    `submit --json`, `export --json`) was preserved so handlers can also
    be invoked as `handler --json` from the legacy argparse path used by
    `test_fr01`. Both paths converge on the same `ctx.obj["json"]` state.
    """
    return click.option(
        "--json",
        is_flag=True,
        default=False,
        help="Emit JSON output (alias for the global --json flag).",
        callback=_propagate_json,
        expose_value=False,
    )


def _render_for(ctx: click.Context, payload: dict) -> None:
    """Render `payload` honouring the `--json` flag from `ctx.obj`."""
    render(payload, bool(ctx.obj.get("json", False)) if ctx.obj else False)


# ---------------------------------------------------------------------------
# Click group + eight subcommands.
# ---------------------------------------------------------------------------
@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """taskq-plus CLI — FR-05 entry point.

    [FR-05]
    Citations: SPEC.md §3 FR-05 (eight-subcommand click group).
    """
    ctx.ensure_object(dict)


@cli.command(name="submit")
@click.argument("command")
@click.option("--name", default=None, help="Optional human-friendly name.")
@click.option(
    "--after",
    multiple=True,
    help="Task id this task depends on (repeatable).",
)
@_json_flag()
@click.pass_context
def submit(
    ctx: click.Context, command: str, name: Optional[str], after: tuple
) -> int:
    """Submit a new task."""
    try:
        payload = commands.submit_cmd(command, name=name, after=list(after))
    except commands.SubmitValidationError as exc:
        _emit_error(str(exc))
        return EXIT_VALIDATION
    except commands.GraphError as exc:
        _emit_error(str(exc))
        return EXIT_GRAPH_ERROR
    _render_for(ctx, payload)
    return EXIT_OK


@cli.command(name="run")
@click.argument("task_id", required=False, default=None)
@click.option("--all", "run_all", is_flag=True, default=False)
@click.option("--cached", "use_cache", is_flag=True, default=False)
@_json_flag()
@click.pass_context
def run(
    ctx: click.Context,
    task_id: Optional[str],
    run_all: bool,
    use_cache: bool,
) -> int:
    """Execute a pending task (or --all)."""
    try:
        payload = commands.run_cmd(task_id=task_id, run_all=run_all, use_cache=use_cache)
    except commands.RunValidationError as exc:
        _emit_error(str(exc))
        return EXIT_VALIDATION
    except commands.RunInternalError as exc:
        _emit_error(str(exc))
        return EXIT_INTERNAL_ERROR
    exit_code = payload.get("exit_code", EXIT_OK)
    # Strip the executor-internal exit_code key from the rendered payload so
    # the stdout body doesn't double up the CLI exit semantics.
    render_payload = {k: v for k, v in payload.items() if k != "exit_code"}
    _render_for(ctx, render_payload)
    return exit_code


@cli.command(name="status")
@click.argument("task_id")
@_json_flag()
@click.pass_context
def status(ctx: click.Context, task_id: str) -> int:
    """Show the full record for a task."""
    try:
        payload = commands.status_cmd(task_id)
    except commands.StatusValidationError as exc:
        _emit_error(str(exc))
        return EXIT_VALIDATION
    _render_for(ctx, payload)
    return EXIT_OK


@cli.command(name="list")
@click.option("--status", "status_filter", default=None)
@_json_flag()
@click.pass_context
def list_cmd(  # noqa: A001 — match click subcommand name `list`
    ctx: click.Context, status_filter: Optional[str]
) -> int:
    """List tasks (optionally filtered by --status)."""
    try:
        payload = commands.list_cmd(status_filter=status_filter)
    except commands.StoreCorrupted as exc:
        _emit_error(f"store corrupted: {exc}")
        return EXIT_INTERNAL_ERROR
    _render_for(ctx, payload)
    return EXIT_OK


@cli.command(name="graph")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(["text", "dot"], case_sensitive=False),
    default="text",
    show_default=True,
)
@_json_flag()
@click.pass_context
def graph(ctx: click.Context, format_name: str) -> int:
    """Render the dependency graph as text or dot."""
    try:
        payload = commands.graph_cmd(format_name)
    except commands.StoreCorrupted as exc:
        _emit_error(f"store corrupted: {exc}")
        return EXIT_INTERNAL_ERROR
    _render_for(ctx, payload)
    return EXIT_OK


@cli.group(name="plugins")
def plugins() -> None:
    """Plugin commands."""


@plugins.command(name="list")
@_json_flag()
@click.pass_context
def plugins_list(ctx: click.Context) -> int:
    """List loaded plugins and their hooks."""
    try:
        payload = commands.plugins_cmd("list")
    except commands.PluginValidationError as exc:
        _emit_error(str(exc))
        return EXIT_VALIDATION
    except commands.PluginLoadError as exc:
        _emit_error(str(exc))
        return EXIT_PLUGIN_LOAD_FAILED
    _render_for(ctx, payload)
    return EXIT_OK


@cli.command(name="export")
@click.option(
    "--format",
    "format_name",
    type=click.Choice(["json", "csv", "md"], case_sensitive=False),
    default="json",
    show_default=True,
)
@_json_flag()
@click.pass_context
def export_cmd(  # noqa: A001 — match click subcommand name `export`
    ctx: click.Context, format_name: str
) -> int:
    """Export task records as json, csv, or md."""
    try:
        payload = commands.export_cmd(format_name)
    except commands.ExportValidationError as exc:
        _emit_error(str(exc))
        return EXIT_VALIDATION
    except commands.StoreCorrupted as exc:
        _emit_error(f"store corrupted: {exc}")
        return EXIT_INTERNAL_ERROR
    # Export renders the raw payload["content"] (csv / md body) verbatim;
    # --json still wraps it as a single-line JSON envelope so callers can
    # distinguish the formats.
    _render_for(ctx, payload)
    return EXIT_OK


@cli.command(name="clear")
@_json_flag()
@click.pass_context
def clear_cmd(ctx: click.Context) -> int:  # noqa: A001
    """Remove every data file under $TASKQ_HOME."""
    payload = commands.clear_cmd()
    _render_for(ctx, payload)
    return EXIT_OK


# ---------------------------------------------------------------------------
# main — the entry point invoked by `taskq_plus.__main__`.
#
# Returns an int exit code; does NOT raise SystemExit (click is invoked with
# `standalone_mode=False`). The wrapping try/except is the safety net for
# handler-level exceptions that should never have been raised (e.g. a bug
# in a handler that lets a non-HandlerError exception escape) — those map
# to exit 1 ("other internal error") per SPEC §7.
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Translate argv into an int exit code (SPEC §3 FR-05 / §7 map).

    [FR-05]
    Citations:
      - SPEC.md §3 FR-05 (eight-subcommand click group, --json).
      - SPEC.md §7 (exit-code map: 0/1/2/3/4/5/6).
      - SRS.md §5 (exit-code map cross-reference).
    """
    if argv is None:
        argv = sys.argv[1:]

    as_json, cleaned = _extract_json(list(argv))
    ctx_obj: dict = {"json": as_json}
    try:
        result = cli.main(args=cleaned, standalone_mode=False, obj=ctx_obj)
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.exceptions.UsageError as exc:
        _emit_error(str(exc))
        return EXIT_VALIDATION
    except click.exceptions.ClickException as exc:
        _emit_error(str(exc.message))
        return int(exc.exit_code or EXIT_INTERNAL_ERROR)
    except SystemExit as exc:
        return int(exc.code if isinstance(exc.code, int) else EXIT_INTERNAL_ERROR)
    except Exception as exc:  # pragma: no cover — last-resort safety net
        _emit_error(f"internal error: {exc}")
        return EXIT_INTERNAL_ERROR

    # click returns the subcommand's return value when standalone_mode=False;
    # some code paths return None (e.g. --help), which collapses to exit 0.
    if result is None:
        return EXIT_OK
    return int(result)


__all__ = [
    "cli",
    "main",
    "render",
    "EXIT_OK",
    "EXIT_INTERNAL_ERROR",
    "EXIT_VALIDATION",
    "EXIT_BREAKER_OPEN",
    "EXIT_TIMEOUT",
    "EXIT_GRAPH_ERROR",
    "EXIT_PLUGIN_LOAD_FAILED",
]
