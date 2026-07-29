"""[FR-02] engine layer — subprocess execution and DAG scheduling.

Citations:
- SPEC.md §3 FR-02 lines 94-104 (任務執行器).
- SPEC.md §6 套件佈局: ``engines`` 為 L3 層,依賴 storage(L2) 與 util(L1).
"""

from taskq_plus.engines import executor

__all__ = ["executor"]