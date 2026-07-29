"""[FR-01] pydantic ``TaskSubmission`` model.

Citations:
- SPEC.md §3 FR-01 (任務提交與驗證) lines 72-92
  * 非空、長度 ≤1000、注入字元黑名單、名稱唯一、相依存在
- TEST_SPEC.md FR-01 acceptance criteria

The model enforces FR-01 rule table verbatim. Store-aware checks
(uniqueness of ``name``, existence of every ``depends_on`` id) are
performed via the ``context`` argument passed to
``model_validate(..., context={...})``.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, ValidationInfo, model_validator

# Injection char blacklist — ``; | & $ > < ` `` verbatim from SPEC.md §3 FR-01.
INJECTION_CHARS: frozenset[str] = frozenset(";|&$><`")
COMMAND_MAX_LEN: int = 1000


class TaskSubmission(BaseModel):
    """Validated submission payload for FR-01.

    Citations:
    - SPEC.md §3 FR-01 通過驗證 states ``command``, ``name``,
      ``created_at``, ``depends_on`` are recorded on pass.
    """

    command: str
    name: Optional[str] = None
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_command_rules(self) -> "TaskSubmission":
        """Command-level FR-01 rules (self-contained, no context).

        Citations:
        - SPEC.md §3 FR-01 非空 / 長度 / 注入字元 blacklisted chars.
        """
        if not self.command or not self.command.strip():
            raise ValueError("command must not be empty or whitespace-only")
        if len(self.command) > COMMAND_MAX_LEN:
            raise ValueError(
                f"command exceeds maximum length of {COMMAND_MAX_LEN} chars"
            )
        for ch in INJECTION_CHARS:
            if ch in self.command:
                raise ValueError(
                    f"command contains forbidden character: {ch!r}"
                )
        return self

    @model_validator(mode="after")
    def _validate_store_rules(self, info: ValidationInfo) -> "TaskSubmission":
        """Store-aware FR-01 rules (require ``context``).

        Citations:
        - SPEC.md §3 FR-01 名稱唯一 (與既有 pending/running 重複 → 拒絕)
        - SPEC.md §3 FR-01 相依存在 (``--after`` 指向不存在 id → 拒絕)
        - SPEC.md §7 錯誤處理 row: ``--after`` 指向不存在的 id →
          exit 2,stderr ``unknown dependency: <id>``
        """
        ctx = info.context or {}
        if self.name is not None:
            existing_names = ctx.get("existing_names", set())
            if self.name in existing_names:
                raise ValueError(f"duplicate name: {self.name!r}")
        known_ids = ctx.get("known_ids", set())
        for dep in self.depends_on:
            if dep not in known_ids:
                raise ValueError(f"unknown dependency: {dep}")
        return self
