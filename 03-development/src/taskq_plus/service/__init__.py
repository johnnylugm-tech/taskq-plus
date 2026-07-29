"""[FR-02 / FR-03 / FR-04 / FR-06 / FR-07] service layer — business orchestration.

Citations:
- SPEC.md §6 套件佈局: ``service`` 為 L3 層,依賴 storage(L2) + models(L1) +
  config(independence).
- 各 FR owner module 對應 SAD.md §2.3:
  - ``executor`` → FR-02 (subprocess + DAG 排程)
  - ``breaker``  → FR-03 (斷路器狀態機)
  - ``cache``    → FR-04 (TTL 快取)
  - ``dag``      → FR-06 (拓撲排序 + 循環偵測)
  - ``plugins``  → FR-07 (allowlist hook 派發)
"""