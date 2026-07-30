"""Settings propagation — `taskq_plus.config` (NFR-06 independence layer).

Declares the public `load_settings()` and `resolve_home()` helpers that the
SAD.md §1 architecture contract pins at the root. Concrete implementations
read directly from `os.environ` at the call site; this module exists so the
SAB layer contract (`config` is importable by any layer, imports no layer)
holds when `cli`, `service`, `__main__` reach for settings in one place.

[FR-05] [NFR-06]
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def resolve_home(env_var: str = "TASKQ_HOME", default: Optional[Path] = None) -> Path:
    """Resolve `$TASKQ_HOME` (or `env_var`) to a concrete `Path`.

    [NFR-06]
    Citations: SPEC.md#L294 (`TASKQ_HOME` 資料檔目錄).
    """
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw).resolve()
    return (default or Path.cwd()).resolve()


def load_settings() -> dict:
    """Return the process-level settings derived from environment variables.

    [NFR-06]
    Citations: SAD.md §1 (Settings loaded once at the top of the call stack).
    """
    return {
        "home": resolve_home(),
        "task_timeout": float(os.environ.get("TASKQ_TASK_TIMEOUT", "30")),
        "retry_limit": int(os.environ.get("TASKQ_RETRY_LIMIT", "0")),
        "breaker_threshold": int(os.environ.get("TASKQ_BREAKER_THRESHOLD", "5")),
        "breaker_cooldown": float(os.environ.get("TASKQ_BREAKER_COOLDOWN", "30")),
        "max_dag_depth": int(os.environ.get("TASKQ_MAX_DAG_DEPTH", "32")),
    }