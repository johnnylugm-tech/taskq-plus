"""[FR-01] Small shared helpers used by more than one layer.

Citations:
- SPEC.md §6 套件佈局 — utility helpers belong to the independence
  layer (no internal dependencies).
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Shared by ``cli`` and ``observability.audit`` so both layers emit
    timestamps in identical format.
    """
    return datetime.now(timezone.utc).isoformat()
