"""taskq_plus.observability.export — multi-format task export renderers.

# pragma: no error-handling — pure string/JSON rendering over an in-memory
# task list; the file I/O is owned by `cli.commands.export_cmd` (FR-08 entry).

`export_tasks` is the SAB-bound writer for the three FR-08 formats
(`json` / `csv` / `md`). `parse_export` is its inverse: a round-trip reader
used by the cross-format invariant asserted by test_fr08_c. NFR-04
redaction runs on the way IN, so a downstream `grep -c "sk-"` over the
emitted body returns 0 (SPEC §8 #22).

[FR-08] [NFR-04]
Citations:
  - SPEC.md §3 FR-08 (export --format json|csv|md; 欄位同 status).
  - SPEC.md §6 tree: `observability/export.py` — json/csv/md 匯出.
  - SPEC.md §8 #14 (三種格式的 task 數與欄位必須一致).
  - SPEC.md §8 NFR-04 (CLI 必須 redact `sk-…` / `token=…` / `Bearer …`).
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Iterable, List, Mapping, Sequence

from taskq_plus.observability.audit import redact_text


# ---------------------------------------------------------------------------
# Canonical field set — SPEC §3 FR-08 "欄位同 status".  The set is closed:
# every rendering and every parser agrees on this exact key list, so the
# cross-format invariant (SPEC §8 #14) holds byte-for-byte.  Adding a column
# means adding it here AND in the `status` projection.
# ---------------------------------------------------------------------------
EXPORT_FIELDS: tuple[str, ...] = (
    "id",
    "command",
    "name",
    "status",
    "created_at",
    "depends_on",
)

EXPORT_FORMATS: tuple[str, ...] = ("json", "csv", "md")


# ---------------------------------------------------------------------------
# NFR-04 — secret redaction applied on the way in.
# ---------------------------------------------------------------------------
def _redact_leaf(value: Any) -> Any:
    """Recursively redact secret-bearing leaves, leaving non-strings alone."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_leaf(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_leaf(item) for item in value)
    return value


def _normalise_for_export(task: Mapping[str, Any]) -> dict[str, Any]:
    """Project `task` onto EXPORT_FIELDS, with secret redaction applied."""
    out: dict[str, Any] = {}
    for field in EXPORT_FIELDS:
        if field == "depends_on":
            deps = task.get(field) or []
            out[field] = _redact_leaf(list(deps))
        else:
            out[field] = _redact_leaf(task.get(field, ""))
    return out


def _stringify(value: Any) -> str:
    """Render a single cell value as a plain string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def _cell_values(row: Mapping[str, Any]) -> list[str]:
    """Return the stringified EXPORT_FIELDS cells of `row`, in schema order."""
    return [_stringify(row.get(field, "")) for field in EXPORT_FIELDS]


def _row_to_dict(cells: Sequence[str]) -> dict[str, Any]:
    """Project a CSV/MD cell list onto EXPORT_FIELDS, padding missing cells.

    Used by both the CSV and MD branches of `parse_export`. The CSV writer
    and the MD writer both emit one cell per EXPORT_FIELDS entry, so an
    under-supplied input is a malformed document — the `""` default keeps
    the cross-format invariant intact rather than raising mid-parse.
    """
    return {
        field: (cells[index] if index < len(cells) else "")
        for index, field in enumerate(EXPORT_FIELDS)
    }


# ---------------------------------------------------------------------------
# Renderers — one per format.  Each accepts the redacted dict sequence and
# returns a single body string.
# ---------------------------------------------------------------------------
def _render_json(rows: Sequence[Mapping[str, Any]]) -> str:
    """Emit a single JSON ARRAY (SPEC §3 FR-08: "single JSON array")."""
    return json.dumps(list(rows), ensure_ascii=False)


def _render_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    """Emit a header row + RFC-4180-quoted rows (commas / quotes escaped)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(list(EXPORT_FIELDS))
    for row in rows:
        writer.writerow(_cell_values(row))
    return buf.getvalue()


def _render_md(rows: Sequence[Mapping[str, Any]]) -> str:
    """Emit a GitHub-flavoured Markdown pipe table."""
    header = "| " + " | ".join(EXPORT_FIELDS) + " |"
    separator = "|" + "|".join("---" for _ in EXPORT_FIELDS) + "|"
    body_lines = ["| " + " | ".join(_cell_values(row)) + " |" for row in rows]
    return "\n".join([header, separator, *body_lines]) + "\n"


# ---------------------------------------------------------------------------
# Public API — the SAB-bound functions the FR-08 tests import.
# ---------------------------------------------------------------------------
def export_tasks(
    tasks: Iterable[Mapping[str, Any]], fmt: str
) -> str:
    """Render `tasks` as `fmt` ∈ `EXPORT_FORMATS`.

    [FR-08] [NFR-04]
    Citations:
      - SPEC.md §3 FR-08 (json → JSON array; csv → header + rows; md → table).
      - SPEC.md §8 #14 (三種格式數量與欄位一致).
      - SPEC.md §8 NFR-04 (落盤前 redact 機敏字串).
    """
    rows = [_normalise_for_export(t) for t in tasks]
    f = (fmt or "json").lower()
    if f == "json":
        return _render_json(rows)
    if f == "csv":
        return _render_csv(rows)
    if f == "md":
        return _render_md(rows)
    raise ValueError(f"unsupported export format {fmt!r}")


def parse_export(content: str, fmt: str) -> List[dict[str, Any]]:
    """Inverse of `export_tasks` — one dict per task, keyed by EXPORT_FIELDS.

    Every returned record carries EXACTLY the EXPORT_FIELDS keys (extra
    cells in the input are dropped; missing cells default to ""), so the
    cross-format round-trip in test_fr08_c can compare record-by-record.

    [FR-08]
    Citations:
      - SPEC.md §3 FR-08 (欄位同 status — 解析後保持一致).
      - SPEC.md §8 #14 (三種格式解析結果欄位一致).
    """
    f = (fmt or "json").lower()
    if f == "json":
        data = json.loads(content)
        if not isinstance(data, list):
            raise ValueError(
                "export_tasks json output must be a list, got "
                f"{type(data).__name__}"
            )
        return [
            {field: record.get(field, "") for field in EXPORT_FIELDS}
            for record in data
        ]
    if f == "csv":
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        if not rows:
            return []
        out: List[dict[str, Any]] = []
        for row in rows[1:]:
            if not any(cell.strip() for cell in row):
                continue
            out.append(_row_to_dict(row))
        return out
    if f == "md":
        out = []
        first = True
        for line in content.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("|") and stripped.endswith("|")):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Skip the header row (parity with the CSV branch above).
            if first:
                first = False
                continue
            # Skip the separator row (---|---) and any all-empty lines.
            if cells and all(set(c) <= set("-: ") for c in cells):
                continue
            out.append(_row_to_dict(cells))
        return out
    raise ValueError(f"unsupported export format {fmt!r}")


__all__ = [
    "EXPORT_FIELDS",
    "EXPORT_FORMATS",
    "export_tasks",
    "parse_export",
]
