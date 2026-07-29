"""[FR-04] 快取簽名 + TTL 命中查詢 + 寫入.

Citations:
- SPEC.md §3 FR-04 lines 116-122: 快取簽名 = sha256(command);
  ``run <id> --cached`` 同簽名且結果為 done 在 TASKQ_CACHE_TTL
  秒內 → 直接回放 (exit_code/stdout_tail);快取過期或不存在 →
  正常執行;成功 (done) 後寫入 $TASKQ_HOME/cache.json.
- SPEC.md §5 環境變數 row ``TASKQ_CACHE_TTL``: 默認 3600.
- TEST_SPEC.md FR-04 ACs AC-FR-04.1 / AC-FR-04.2 / AC-FR-04.3.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

from taskq_plus.storage import cache_store


def _resolve_ttl() -> float:
    """Return ``$TASKQ_CACHE_TTL`` as float seconds (default 3600)."""
    return float(os.environ.get("TASKQ_CACHE_TTL", "3600"))


def cache_key(command: str) -> str:
    """Return the hex ``sha256`` of ``command``'s UTF-8 bytes.

    Citations:
    - AC-FR-04.1: 快取簽名 = sha256(command). Whitespace normalisation
      is NOT applied — distinct command strings map to distinct keys.
    """
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def lookup(command: str) -> dict[str, Any] | None:
    """Return the cached ``result`` when fresh; ``None`` on miss or expiry.

    Citations:
    - AC-FR-04.2: 同簽名且結果為 ``done`` 的最近執行在
      ``TASKQ_CACHE_TTL`` 秒內 → 直接回放.
    """
    sig = cache_key(command)
    entry = cache_store.get(sig)
    if entry is None:
        return None
    cached_at = entry.get("cached_at")
    result = entry.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("status") != "done":
        return None
    if not isinstance(cached_at, (int, float)):
        return None
    if time.time() - float(cached_at) >= _resolve_ttl():
        return None
    return result


def store(command: str, result: dict[str, Any]) -> None:
    """Write ``result`` into the cache keyed by ``sha256(command)``.

    Citations:
    - AC-FR-04.3: 成功 (done) 後寫入;非 done 不寫.
    """
    if not isinstance(result, dict):
        return
    if result.get("status") != "done":
        return
    sig = cache_key(command)
    cache_store.put(sig, result)