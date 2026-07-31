"""JSONL audit journal + NFR-04 pre-write redaction.

One `AuditLogger` models one CLI invocation: it owns the `correlation_id`
every event of that invocation shares, and `emit()` appends exactly one JSON
Lines record per event to `$TASKQ_AUDIT_LOG` (default
`$TASKQ_HOME/audit.jsonl`). Redaction runs on the `detail` payload BEFORE the
bytes reach the disk, so a `grep -c "sk-"` over the journal returns 0.

[FR-08] [NFR-04]
Citations:
  - SPEC.md#L162 (§3 FR-08 結構化稽核日誌與匯出).
  - SPEC.md#L166 (路徑 `$TASKQ_AUDIT_LOG`,預設 `$TASKQ_HOME/audit.jsonl`,
    JSON Lines,append-only).
  - SPEC.md#L167 (每筆欄位:`ts`(ISO-8601 UTC)、`event`、`task_id`、
    `correlation_id`、`detail`).
  - SPEC.md#L168 (`correlation_id` 由一次 CLI 呼叫產生,該次呼叫觸發的所有
    事件共用同一個值).
  - SPEC.md#L169 (事件種類:submit / run_start / run_end / retry /
    breaker_open / breaker_close / cache_hit / blocked / plugin_error).
  - SPEC.md#L170 (落盤前套用 NFR-04 的 redaction).
  - SPEC.md#L214 (NFR-04:匹配 `(sk-[A-Za-z0-9_-]{8,}|token=\\S+|Bearer\\s+\\S+)`
    的行整行以 `[REDACTED]` 取代).
  - SPEC.md#L215 (遮蔽發生在寫入前,不是讀取後).
  - SPEC.md#L304 (§5.1 `TASKQ_AUDIT_LOG` 預設 `$TASKQ_HOME/audit.jsonl`).
  - SPEC.md#L314 (§5.2 `audit.jsonl` 每行一筆稽核事件).
  - SPEC.md#L424 (§8 #22 `grep -c "sk-" $TASKQ_HOME/audit.jsonl` → 0).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Public constants — SPEC.md#L167 / #L169 / #L304.
# ---------------------------------------------------------------------------
#: The exact, complete per-entry field set, in SPEC order (SPEC.md#L167).
AUDIT_FIELDS: tuple[str, ...] = ("ts", "event", "task_id", "correlation_id", "detail")

#: The nine SPEC event names (SPEC.md#L169).
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "submit",
        "run_start",
        "run_end",
        "retry",
        "breaker_open",
        "breaker_close",
        "cache_hit",
        "blocked",
        "plugin_error",
    }
)

#: Env var holding the journal path, and the default filename under TASKQ_HOME.
AUDIT_LOG_ENV = "TASKQ_AUDIT_LOG"
HOME_ENV = "TASKQ_HOME"
AUDIT_FILENAME = "audit.jsonl"

#: NFR-04 secret pattern and replacement marker (SPEC.md#L214).
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)")
REDACTED = "[REDACTED]"

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Timestamp / id / path helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    """Return the current UTC ISO-8601 timestamp with a trailing `Z`.

    [FR-08]
    Citations: SPEC.md#L167 (`ts` 為 ISO-8601 UTC).
    """
    return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")


def new_correlation_id() -> str:
    """Return a fresh 8-char lowercase-hex correlation id (uuid4 prefix).

    Same shape as the FR-01 task id, so journal lines and task ids read alike.

    [FR-08]
    Citations: SPEC.md#L168 (`correlation_id` 由一次 CLI 呼叫產生).
    """
    return uuid.uuid4().hex[:8]


def audit_log_path() -> Path:
    """Return the journal path: `$TASKQ_AUDIT_LOG`, else `$TASKQ_HOME/audit.jsonl`.

    [FR-08]
    Citations:
      - SPEC.md#L166 (路徑 `$TASKQ_AUDIT_LOG`,預設 `$TASKQ_HOME/audit.jsonl`).
      - SPEC.md#L304 (§5.1 env 表:`TASKQ_AUDIT_LOG` 預設值).
    """
    override = os.environ.get(AUDIT_LOG_ENV)
    if override:
        return Path(override)
    return Path(os.environ.get(HOME_ENV, ".")) / AUDIT_FILENAME


# ---------------------------------------------------------------------------
# NFR-04 redaction — whole-line replacement, applied before the write.
# ---------------------------------------------------------------------------
def redact_text(text: str) -> str:
    """Replace every secret-matching LINE of `text` in full with `[REDACTED]`.

    Secret-free lines are returned byte-identical, and the transform is
    idempotent (`[REDACTED]` itself never matches the pattern).

    [FR-08] [NFR-04]
    Citations:
      - SPEC.md#L214 (匹配的行整行以 `[REDACTED]` 取代).
      - SPEC.md#L170 (稽核日誌落盤前套用 NFR-04 的 redaction).
    """
    lines = str(text).split("\n")
    return "\n".join(REDACTED if SECRET_RE.search(line) else line for line in lines)


def redact_detail(detail: Any) -> Any:
    """Return `detail` with NFR-04 redaction applied to every nested string.

    Non-string leaves (ints, bools, None) are returned unchanged — they cannot
    carry a secret pattern.

    [FR-08] [NFR-04]
    Citations:
      - SPEC.md#L170 (稽核日誌 `detail` 落盤前套用 redaction).
      - SPEC.md#L215 (遮蔽發生在寫入前).
    """
    if isinstance(detail, dict):
        return {key: redact_detail(value) for key, value in detail.items()}
    if isinstance(detail, str):
        return redact_text(detail)
    return detail


# ---------------------------------------------------------------------------
# The journal writer / reader
# ---------------------------------------------------------------------------
class AuditLogger:
    """One audit journal writer per CLI invocation (append-only JSON Lines).

    [FR-08] [NFR-04]
    Citations:
      - SPEC.md#L166 (JSON Lines,append-only).
      - SPEC.md#L168 (該次呼叫觸發的所有事件共用同一個 `correlation_id`).
    """

    def __init__(
        self,
        correlation_id: Optional[str] = None,
        path: Optional[Path] = None,
    ) -> None:
        """Bind a correlation id (generated when absent) and journal path.

        [FR-08]
        Citations: SPEC.md#L168 (`correlation_id` 由一次 CLI 呼叫產生).
        """
        self.correlation_id = correlation_id or new_correlation_id()
        self._path = Path(path) if path is not None else None

    @property
    def path(self) -> Path:
        """Return the journal path, resolved from the env when not pinned.

        [FR-08]
        Citations: SPEC.md#L166 (預設 `$TASKQ_HOME/audit.jsonl`).
        """
        return self._path if self._path is not None else audit_log_path()

    def emit(
        self,
        event: str,
        task_id: Optional[str] = None,
        detail: Any = None,
    ) -> Dict[str, Any]:
        """Append exactly one redacted JSON Lines record; return that record.

        [FR-08] [NFR-04]
        Citations:
          - SPEC.md#L166 (JSON Lines,append-only).
          - SPEC.md#L167 (欄位:ts / event / task_id / correlation_id / detail).
          - SPEC.md#L170 (落盤前套用 NFR-04 的 redaction).
          - SPEC.md#L314 (§5.2 `audit.jsonl` append + fsync).
        """
        entry: Dict[str, Any] = {
            "ts": _now_iso(),
            "event": str(event),
            "task_id": task_id,
            "correlation_id": self.correlation_id,
            "detail": redact_detail(detail),
        }
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # Audit logging is best-effort: a full disk or revoked permission
            # must not crash the CLI. The in-memory `entry` is still returned.
            pass
        return entry


def read_entries(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Parse the JSONL journal back into dicts ([] when the file is absent).

    [FR-08]
    Citations:
      - SPEC.md#L166 (JSON Lines 每行一筆).
      - SPEC.md#L314 (§5.2 `audit.jsonl` 每行一筆稽核事件).
    """
    target = Path(path) if path is not None else audit_log_path()
    raw = target.read_text(encoding="utf-8") if target.exists() else ""
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Per-invocation logger — `cli.main.main` installs one before dispatching, so
# every handler of that invocation writes with the same correlation id.
# ---------------------------------------------------------------------------
_CURRENT_LOGGER = AuditLogger()


def set_current_logger(logger: AuditLogger) -> AuditLogger:
    """Install `logger` as the journal writer of the current CLI invocation.

    [FR-08]
    Citations: SPEC.md#L168 (一次 CLI 呼叫產生一個 `correlation_id`).
    """
    global _CURRENT_LOGGER
    _CURRENT_LOGGER = logger
    return logger


def current_logger() -> AuditLogger:
    """Return the invocation-scoped logger installed by `cli.main.main`.

    [FR-08]
    Citations: SPEC.md#L168 (該次呼叫觸發的所有事件共用同一個值).
    """
    return _CURRENT_LOGGER


__all__ = [
    "AUDIT_FIELDS",
    "EVENT_TYPES",
    "AUDIT_LOG_ENV",
    "AUDIT_FILENAME",
    "REDACTED",
    "SECRET_RE",
    "AuditLogger",
    "audit_log_path",
    "current_logger",
    "new_correlation_id",
    "read_entries",
    "redact_detail",
    "redact_text",
    "set_current_logger",
]
