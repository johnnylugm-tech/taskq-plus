"""[FR-01] Domain-level error helpers.

Citations:
- SPEC.md §7 錯誤處理 lines 379-389
"""

class TaskqValidationError(ValueError):
    """Raised by the CLI when a submission violates an FR-01 rule.

    Citations:
    - SPEC.md §3 FR-01 任一違反 → exit 2
    """
