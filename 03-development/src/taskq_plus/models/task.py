"""Task domain model and validation.

# pragma: no error-handling — Pydantic model + a uuid helper; no I/O.

[FR-01]
Citations: SPEC.md §3 FR-01 (TaskSubmission pydantic model, validation rules).
"""

from __future__ import annotations

import uuid
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# Per SPEC.md §3 FR-01 — injection blacklist (NFR-02).
INJECTION_CHARS = (";", "|", "&", "$", ">", "<", "`")

# Per SPEC.md §3 FR-01 — command length cap.
MAX_COMMAND_LENGTH = 1000


class TaskSubmission(BaseModel):
    """Validated submission payload for FR-01.

    [FR-01]
    Citations: SPEC.md §3 FR-01 ("TaskSubmission" pydantic model).
    """

    command: str = Field(..., description="Shell command to validate.")
    name: Optional[str] = Field(default=None, description="Optional human-friendly name.")
    depends_on: list[str] = Field(default_factory=list, description="Task ids this depends on.")

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        """Reject empty / whitespace, oversized, and injection-char commands.

        [FR-01] [NFR-02]
        Citations: SPEC.md §3 FR-01 (rules: non-empty, length, injection chars).
        """
        if not value or not value.strip():
            raise ValueError("command must not be empty or whitespace-only")
        if len(value) > MAX_COMMAND_LENGTH:
            raise ValueError(
                f"command length {len(value)} exceeds max {MAX_COMMAND_LENGTH}"
            )
        for ch in INJECTION_CHARS:
            if ch in value:
                raise ValueError(f"command contains blacklisted injection character: {ch!r}")
        return value


def generate_task_id() -> str:
    """Return an 8-character lowercase hex id (uuid4 prefix).

    [FR-01]
    Citations: SPEC.md §3 FR-01 ("uuid4 first 8 hex").
    """
    return uuid.uuid4().hex[:8]
