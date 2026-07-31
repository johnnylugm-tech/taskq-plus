"""Domain error classes — `taskq_plus.models.errors`.

# pragma: no error-handling — exception class definitions only.

Single source for typed exceptions raised by the `service` layer and
translated to CLI exit codes by `cli.commands` (FR-05). Each error maps to
one exit code in the SRS §5 exit-code map.

[FR-02] [FR-05]
"""

from __future__ import annotations

from typing import Optional


class TaskQError(Exception):
    """Base class for all taskq_plus domain errors."""


class ValidationRejected(TaskQError):
    """Raised when input fails the FR-01 validation rules (→ exit 2)."""

    def __init__(self, message: str, *, field: Optional[str] = None) -> None:
        super().__init__(message)
        self.field = field


class TaskNotFound(TaskQError):
    """Raised when an id does not match any stored task (→ exit 2)."""


class DagDepthExceeded(TaskQError):
    """Raised when a dependency chain exceeds the configured depth."""


class BreakerOpen(TaskQError):
    """Raised when the circuit breaker rejects an execution (→ exit 3)."""


class TaskTimeout(TaskQError):
    """Raised when a subprocess exceeds the configured timeout (→ exit 4)."""


class PluginLoadFailed(TaskQError):
    """Raised when a plugin module fails the allowlist / import (→ exit 6)."""


class StoreCorrupted(TaskQError):
    """Raised when a persisted file is unrecoverably malformed (→ exit 1)."""