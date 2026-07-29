"""[FR-01] pydantic models — Layer L1 (零內部依賴).

Citations:
- SPEC.md §3 FR-01 (任務提交與驗證) lines 72-92
- SPEC.md §6 套件佈局 lines 338-341 (models 層)
"""

from taskq_plus.models.errors import TaskqValidationError
from taskq_plus.models.task import INJECTION_CHARS, COMMAND_MAX_LEN, TaskSubmission

__all__ = [
    "COMMAND_MAX_LEN",
    "INJECTION_CHARS",
    "TaskSubmission",
    "TaskqValidationError",
]
