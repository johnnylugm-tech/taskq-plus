# Traceability Matrix — taskq-plus

> Requirements Traceability Matrix
> Framework: harness-methodology
> Version: v1.0
> Phase: 1 — INGESTION MODE
> Owner: Agent A (Sub-Task 3/4)
> Created: 2026-07-30

---

## Overview

Provides complete **FR ↔ SRS ↔ Code ↔ Test** bidirectional traceability supporting ASPICE SWE.3 / SYS.4 compliance.

Each `### FR-XX` and `### NFR-XX` clause transcribed from `SPEC.md` §3 / §4 (canonical source) is bound to its `SRS.md` §3 / §4 anchor, to the code module that implements it (`SPEC.md` §6 directory layout), and to the verification test that proves it. The matrix is the single machine-decidable index that `advance-phase` reads before flipping any `DRAFT` → `IN_PROGRESS` → `VERIFIED` cell.

Per NFR-09.4: `VERIFIED` is given only when the verification test actually ran and passed; otherwise the status is `NOT_VERIFIED` / `DRAFT`.

---

## 1. FR ↔ SRS Mapping (Forward)

| FR ID | Functional Requirement | SRS § | Priority | Test Inventory Anchor | Status |
|-------|----------------------|-------|----------|------------------------|--------|
| FR-01 | 任務提交與驗證 (`submit "<cmd>" [--name] [--after]…`) — pydantic `TaskSubmission`; violation → exit 2; pass → uuid4 8-hex id + atomic write to `$TASKQ_HOME/tasks.json` + `submit` audit event | §3 FR-01 | HIGH | TEST_INVENTORY.yaml FR-01 (unit + integration) | DRAFT |
| FR-02 | 任務執行器 (`run <id>` / `run --all`) — `subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=…)`; `shell=True` 禁用; `--all` via `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)` + Kahn topology + shared `threading.Lock`; state machine `pending→running→done\|failed\|timeout\|blocked`; timeout → exit 4 | §3 FR-02 | HIGH | TEST_INVENTORY.yaml FR-02 (unit + integration) | DRAFT |
| FR-03 | 重試與斷路器 — `failed`/`timeout` 自動重試 ≤ `TASKQ_RETRY_LIMIT`, backoff `TASKQ_BACKOFF_BASE × 2^n`; 連續最終失敗 ≥ `TASKQ_BREAKER_THRESHOLD` → `OPEN` (persisted); OPEN 期間 `run` 拒絕 exit 3; cooldown 後 → `HALF_OPEN`; 成功 → `CLOSED`, 失敗 → `OPEN` | §3 FR-03 | HIGH | TEST_INVENTORY.yaml FR-03 (unit + integration) | DRAFT |
| FR-04 | 結果 TTL 快取 — sig = `sha256(command)`; `--cached` 在 `TASKQ_CACHE_TTL` 內同簽名 + 上次 `done` → 直接回放 (`exit_code` / `stdout_tail`), 標記 `cached: true`; 過期 / 不存在 → 正常執行 + atomic write to `cache.json`; thread-safe | §3 FR-04 | HIGH | TEST_INVENTORY.yaml FR-04 (unit + integration) | DRAFT |
| FR-05 | CLI 整合 — `click` group; 子命令 `submit` / `run` / `status` / `list` / `graph` / `plugins list` / `export` / `clear`; 全域旗標 `--json`; exit-code map `0`/`2`/`3`/`4`/`5`/`6`/`1` | §3 FR-05 | HIGH | TEST_INVENTORY.yaml FR-05 (unit + integration) | DRAFT |
| FR-06 | 任務相依 DAG — `--after` 建立 `depends_on` 邊; `run --all` Kahn 拓撲, 同層並發; 非 `done` 上游 → 下游 `blocked`, 不執行、不計入 breaker; 循環 → exit 5 + stderr 循環路徑; 鏈深度 > `TASKQ_MAX_DAG_DEPTH` → exit 5; `graph --format text\|dot` | §3 FR-06 | HIGH | TEST_INVENTORY.yaml FR-06 (unit + integration) | DRAFT |
| FR-07 | Plugin Hook 系統 — `TASKQ_PLUGINS` allowlist; `importlib.import_module` named-load; `pre_run(task)` / `post_run(task, result)`; 模組名必匹配 `^[A-Za-z_][A-Za-z0-9_.]*$`, 否則 exit 6; 禁用 `eval` / `exec` / `__import__` 動態字串、禁用路徑 / URL; plugin 例外不中止, 寫 `plugin_error` audit, 連 3 次失敗停用該 plugin | §3 FR-07 | HIGH | TEST_INVENTORY.yaml FR-07 (unit + integration) | DRAFT |
| FR-08 | 結構化稽核日誌與匯出 — `$TASKQ_AUDIT_LOG` (default `$TASKQ_HOME/audit.jsonl`), JSON Lines, append + fsync; 事件種類 `submit`/`run_start`/`run_end`/`retry`/`breaker_open`/`breaker_close`/`cache_hit`/`blocked`/`plugin_error` 帶 `correlation_id`; `export --format json\|csv\|md` 三格式行數 / 欄位集合一致; NFR-04 redaction 寫入前套用 | §3 FR-08 | HIGH | TEST_INVENTORY.yaml FR-08 (unit + integration) | DRAFT |
| NFR-01 | 效能預算 — `submit` + `status` 100 次 p95 < 50ms; `run --all` 200 任務拓撲排序 p95 < 200ms; 量測 `pytest-benchmark` | §4 NFR-01 | HIGH | TEST_INVENTORY.yaml NFR-01 (perf) | DRAFT |
| NFR-02 | 執行與載入安全 — 禁用 `shell=True` (grep 0); FR-01 注入字元各 1 case; plugin 名稱 `^[A-Za-z_][A-Za-z0-9_.]*$` allowlist; `bandit -r 03-development/src/` → 0 HIGH / 0 MEDIUM | §4 NFR-02 | HIGH | TEST_INVENTORY.yaml NFR-02 (security) | DRAFT |
| NFR-03 | 錯誤處理與原子性 — 四個資料檔全部 atomic write (tmp + `os.replace`, audit append + fsync); 禁裸 `except:` / `except Exception: pass` / 吞 `KeyboardInterrupt`/`SystemExit`; `OPEN → CLOSED` 恢復 ≤ `TASKQ_BREAKER_COOLDOWN` + 1s | §4 NFR-03 | HIGH | TEST_INVENTORY.yaml NFR-03 (error handling) | DRAFT |
| NFR-04 | 敏感資料遮蔽 — `stdout_tail` / `stderr_tail` / audit `detail` 寫入前套用 redaction regex `(sk-[A-Za-z0-9_-]{8,}\|token=\S+\|Bearer\s+\S+)`; 命中行整行以 `[REDACTED]` 取代 | §4 NFR-04 | HIGH | TEST_INVENTORY.yaml NFR-04 (security) | DRAFT |
| NFR-05 | 文件覆蓋 — `03-development/src/taskq_plus` 公開函式 / 類別 docstring 100%; 每條 docstring 含 `[FR-XX]` 或 `[NFR-XX]` 引用 | §4 NFR-05 | MEDIUM | TEST_INVENTORY.yaml NFR-05 (docs) | DRAFT |
| NFR-06 | 架構分層契約 — repo root `.importlinter` 宣告 `cli > observability > service > storage > models`, `config` 為 independence 模組; `lint-imports` exit 0; 禁止以刪除 `.importlinter` / 萬用字元 `ignore_imports` / 縮減為單條 `forbidden` 取得通過 | §4 NFR-06 | HIGH | TEST_INVENTORY.yaml NFR-06 (arch gate) | DRAFT |
| NFR-07 | 依賴與授權合規 — `requirements.txt` 全 runtime dep 以 `==` 釘版; allowlist {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0}; 掃描含已安裝依賴樹 (`pip-licenses` 或 `scancode`); `08-config/SBOM.json` 列出 `name`/`version`/`license` | §4 NFR-07 | HIGH | TEST_INVENTORY.yaml NFR-07 (license) | DRAFT |
| NFR-08 | 變異測試 — `.methodology/harness_config.json` 設 `features.mutation_testing: true`; `mutmut run` + `mutmut results` mutation score ≥ 70; 範圍限 `03-development/src/taskq_plus/service/` 與 `.../storage/` | §4 NFR-08 | HIGH | TEST_INVENTORY.yaml NFR-08 (mutation) | DRAFT |
| NFR-09 | 驗證真實性(零 skip 鐵律) — `pytest 03-development/tests -q` 報告 `skipped` = 0; 每個測試函式 ≥ 1 個 `assert` (`ast-assertions` `zero_assert == 0`); 不得以 `--ignore` / `-k` / `--deselect` / `collect_ignore` / `testpaths` 排除; `TRACEABILITY_MATRIX.md` 的 `VERIFIED` 僅在測試實際執行並通過時才給出 | §4 NFR-09 | HIGH | TEST_INVENTORY.yaml NFR-09 (zero-skip) | DRAFT |
| NFR-10 | 整合覆蓋 — `03-development/tests/integration/` 行覆蓋 ≥ 80%; 經由 CLI 入口 (`python -m taskq_plus`) 或 `click.testing.CliRunner`; 涵蓋 submit→run→status 全鏈 / DAG 多層 / breaker 開闔 / cache 命中 / plugin hook / export 三格式 | §4 NFR-10 | HIGH | TEST_INVENTORY.yaml NFR-10 (integration) | DRAFT |
| NFR-11 | 可讀性 — 專案 MI (LLOC 加權) ≥ 80; 單函式 cyclomatic complexity ≤ 10; 單檔 ≤ 400 行; 單一目錄 ≤ 15 檔 | §4 NFR-11 | MEDIUM | TEST_INVENTORY.yaml NFR-11 (readability) | DRAFT |
| NFR-12 | 系統驗證目標 — `Makefile` 提供 `verify-system` target, 串接全套測試 + CLI 冒煙 (`submit`/`run`/`status`/`graph`/`export`/`clear`); `make verify-system` exit 0 且 stdout 印 `verify-system: PASS` | §4 NFR-12 | HIGH | TEST_INVENTORY.yaml NFR-12 (verify target) | DRAFT |

---

## 2. SRS ↔ Code Mapping

> Code locations follow `SPEC.md` §6 directory layout (`taskq-plus` five-layer architecture).
> Per Phase-1 INGESTION MODE the source tree under `03-development/src/` is empty — these are
> the **planned** anchors that `03-development` Phase-3 implementation MUST populate. The
> `Status` column reflects whether the code file currently exists on disk.

| SRS § | Code Module (planned, per SPEC §6) | Key Symbol(s) | Lines | Exists on disk | Status |
|-------|------------------------------------|----------------|-------|----------------|--------|
| §3 FR-01 | `03-development/src/taskq_plus/models/task.py` | `class TaskSubmission` (pydantic v2) | — | NO | NOT_IMPLEMENTED |
| §3 FR-01 | `03-development/src/taskq_plus/storage/task_store.py` | `submit(task)` | — | NO | NOT_IMPLEMENTED |
| §3 FR-02 | `03-development/src/taskq_plus/service/executor.py` | `run(task)`, `run_all()` | — | NO | NOT_IMPLEMENTED |
| §3 FR-03 | `03-development/src/taskq_plus/service/breaker.py` | `class CircuitBreaker`, `should_admit()` | — | NO | NOT_IMPLEMENTED |
| §3 FR-03 | `03-development/src/taskq_plus/storage/breaker_store.py` | `persist(state)` | — | NO | NOT_IMPLEMENTED |
| §3 FR-04 | `03-development/src/taskq_plus/service/cache.py` | `lookup(sig)`, `store(sig, result)` | — | NO | NOT_IMPLEMENTED |
| §3 FR-04 | `03-development/src/taskq_plus/storage/cache_store.py` | `read()`, `write(entries)` | — | NO | NOT_IMPLEMENTED |
| §3 FR-05 | `03-development/src/taskq_plus/cli/main.py` | `cli = click.Group(...)` | — | NO | NOT_IMPLEMENTED |
| §3 FR-05 | `03-development/src/taskq_plus/cli/commands.py` | subcommands `submit` / `run` / `status` / `list` / `graph` / `plugins` / `export` / `clear` | — | NO | NOT_IMPLEMENTED |
| §3 FR-06 | `03-development/src/taskq_plus/service/dag.py` | `kahn_order(tasks)`, `detect_cycle(edges)` | — | NO | NOT_IMPLEMENTED |
| §3 FR-07 | `03-development/src/taskq_plus/service/plugins.py` | `load_plugins(allowlist)`, `run_pre_run(task)` | — | NO | NOT_IMPLEMENTED |
| §3 FR-08 | `03-development/src/taskq_plus/observability/audit.py` | `emit(event, **detail)` | — | NO | NOT_IMPLEMENTED |
| §3 FR-08 | `03-development/src/taskq_plus/observability/export.py` | `export(format)` | — | NO | NOT_IMPLEMENTED |
| §4 NFR-03 | `03-development/src/taskq_plus/storage/atomic.py` | `atomic_write_json(path, obj)` | — | NO | NOT_IMPLEMENTED |
| §4 NFR-04 | `03-development/src/taskq_plus/observability/audit.py` | `REDACT_RE` (verbatim from SPEC §4 NFR-04) | — | NO | NOT_IMPLEMENTED |
| §4 NFR-05 | every public symbol across `03-development/src/taskq_plus/` | docstring `[FR-XX]` / `[NFR-XX]` tag | — | NO | NOT_IMPLEMENTED |
| §4 NFR-06 | `03-development/.importlinter` (project root, repo-root `.importlinter`) | contract: `cli > observability > service > storage > models` | — | NO | NOT_IMPLEMENTED |
| §4 NFR-07 | `03-development/requirements.txt` + `08-config/SBOM.json` | pinned deps + license metadata | — | NO | NOT_IMPLEMENTED |
| §4 NFR-08 | `.methodology/harness_config.json` (`features.mutation_testing: true`) | `service/` + `storage/` scope annotation | — | partial (config file exists in `.methodology/`, mutation_testing flag TBD) | NOT_IMPLEMENTED |
| §4 NFR-09 | `03-development/tests/` (no `--ignore`, `-k`, `--deselect`, `collect_ignore`, `testpaths` exclusions) | enforcement = `pytest -q` summary | — | NO | NOT_IMPLEMENTED |
| §4 NFR-10 | `03-development/tests/integration/` (CLI-driven only) | submit→run→status / DAG / breaker / cache / plugin / export | — | NO | NOT_IMPLEMENTED |
| §4 NFR-11 | every source file under `03-development/src/taskq_plus/` | MI ≥ 80, CC ≤ 10, ≤ 400 LOC/file, ≤ 15 files/dir | — | NO | NOT_IMPLEMENTED |
| §4 NFR-12 | `Makefile` (`verify-system` target) | smoke chain submit / run / status / graph / export / clear | — | NO | NOT_IMPLEMENTED |

### Cross-Layer Layering Contract (NFR-06 binding)

```
cli (L5)  >  observability (L4)  >  service (L3)  >  storage (L2)  >  models (L1)
                       config (independence module — any layer may import)
```

Enforced by `.importlinter`. `lint-imports` MUST exit 0.

---

## 3. Code ↔ Test Mapping

> Test files follow SPEC §6 (`tests/unit/` + `tests/integration/`) and the
> `TEST_INVENTORY.yaml` naming authority at the project root. Phase-3 Agent A
> implements tests verbatim from the names declared here.

| Code Module | Unit Test File | Integration Test File | Coverage Target | Exists on disk | Status |
|-------------|---------------|----------------------|------------------|----------------|--------|
| `models/task.py` | `03-development/tests/unit/test_models_task.py` | — | ≥ 100% (per SPEC §8 #2) | NO | NOT_IMPLEMENTED |
| `storage/atomic.py` | `03-development/tests/unit/test_storage_atomic.py` | — | ≥ 100% | NO | NOT_IMPLEMENTED |
| `storage/task_store.py` | `03-development/tests/unit/test_storage_task_store.py` | `03-development/tests/integration/test_storage_task_store.py` | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |
| `storage/breaker_store.py` | `03-development/tests/unit/test_storage_breaker_store.py` | `03-development/tests/integration/test_storage_breaker_store.py` | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |
| `storage/cache_store.py` | `03-development/tests/unit/test_storage_cache_store.py` | `03-development/tests/integration/test_storage_cache_store.py` | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |
| `service/executor.py` | `03-development/tests/unit/test_service_executor.py` | `03-development/tests/integration/test_service_executor.py` | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |
| `service/breaker.py` | `03-development/tests/unit/test_service_breaker.py` | `03-development/tests/integration/test_service_breaker.py` | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |
| `service/cache.py` | `03-development/tests/unit/test_service_cache.py` | `03-development/tests/integration/test_service_cache.py` | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |
| `service/dag.py` | `03-development/tests/unit/test_service_dag.py` | `03-development/tests/integration/test_service_dag.py` | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |
| `service/plugins.py` | `03-development/tests/unit/test_service_plugins.py` | `03-development/tests/integration/test_service_plugins.py` | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |
| `observability/audit.py` | `03-development/tests/unit/test_observability_audit.py` | `03-development/tests/integration/test_observability_audit.py` | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |
| `observability/export.py` | `03-development/tests/unit/test_observability_export.py` | `03-development/tests/integration/test_observability_export.py` | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |
| `cli/main.py` + `cli/commands.py` | `03-development/tests/unit/test_cli_*.py` (one per subcommand) | `03-development/tests/integration/test_cli_smoke.py` (submit/run/status/graph/export/clear chain) | ≥ 100% / ≥ 80% | NO | NOT_IMPLEMENTED |

### High-Risk Modules — Per-Module TDD Required (per SPEC §10)

| Module | FR / NFR Bounded | Required Test Density |
|--------|------------------|-----------------------|
| `taskq_plus.service.executor` | FR-02 / FR-03 | every state-machine transition, every exit code |
| `taskq_plus.service.plugins` | FR-07 / NFR-02 | every allowlist regex branch, every eval/exec blacklist grep |
| `taskq_plus.storage.task_store` | FR-01 / FR-02 | concurrent write race, mid-write crash, atomic-replace |

---

## 4. SPEC §8 Acceptance Criteria — FR / NFR Index

> Each row is one of the 22 machine-decidable SPEC §8 acceptance items; the
> matrix binds it to its FR/NFR and the test that asserts it. Status is the
> matrix-mandated `VERIFIED` cell, only writable after the test actually ran and passed.

| # | SPEC §8 Command | Expected | FR / NFR | Test Reference | Status |
|---|----------------|----------|----------|----------------|--------|
| 1 | `pytest 03-development/tests -q` | all-green; `skipped` count = 0 | NFR-09 | `tests/` summary line | DRAFT |
| 2 | `pytest 03-development/tests --cov=03-development/src --cov-report=term` | TOTAL 100% | (line coverage contract) | `tests/` coverage gate | DRAFT |
| 3 | `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term` | TOTAL ≥ 80% | NFR-10 | `tests/integration/` coverage gate | DRAFT |
| 4 | `python -m taskq_plus submit "echo hi"` | stdout 8-hex id; exit 0 | FR-01 / AC-FR-01.1 | `test_cli_submit_happy_path` | DRAFT |
| 5 | `python -m taskq_plus submit ""` | exit 2 | FR-01 / AC-FR-01.2 | `test_cli_submit_empty` | DRAFT |
| 6 | `python -m taskq_plus submit "echo hi; rm x"` | exit 2 (injection char) | FR-01 / NFR-02 / AC-FR-01.3 | `test_cli_submit_injection_*` (7 char cases per NFR-02.2) | DRAFT |
| 7 | `TASKQ_TASK_TIMEOUT=1 python -m taskq_plus run <sleep-5-id>` | status `timeout`; exit 4 | FR-02 / AC-FR-02.4 | `test_service_executor_timeout` | DRAFT |
| 8 | 3 consecutive final failures, then `python -m taskq_plus run <id>` | exit 3; recovers after cooldown | FR-03 / AC-FR-03.2..4 | `test_service_breaker_open_close` | DRAFT |
| 9 | Within TTL, `python -m taskq_plus run <id> --cached` | prints `cached: true`; no subprocess | FR-04 / AC-FR-04.2 | `test_service_cache_hit` | DRAFT |
| 10 | After `python -m taskq_plus submit "echo b" --after <a>`, then `run --all` | b runs after a; b=`blocked` if a not done | FR-06 / AC-FR-06.1 | `test_service_dag_kahn_blocked` | DRAFT |
| 11 | Build A→B→A dependency | exit 5; stderr cycle path | FR-06 / AC-FR-06.3 | `test_service_dag_cycle` | DRAFT |
| 12 | `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` | exit 6 | FR-07 / NFR-02 / AC-FR-07.1 | `test_service_plugins_allowlist_path` | DRAFT |
| 13 | plugin `pre_run` raises, then `run <id>` | task completes; `audit.jsonl` has `plugin_error` | FR-07 / AC-FR-07.3 | `test_service_plugins_isolation` | DRAFT |
| 14 | `python -m taskq_plus export --format json` / `csv` / `md` | row counts equal; CSV escapes commas/quotes | FR-08 / AC-FR-08.3..4 | `test_observability_export_three_formats` | DRAFT |
| 15 | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` | 0 hits | NFR-02 / AC-NFR-02.1..3 | `test_security_blacklist_grep` | DRAFT |
| 16 | `grep -c "^TASKQ_" .env.example` | 12 | (env-var completeness) | `test_config_env_example_complete` | DRAFT |
| 17 | `lint-imports` | exit 0 | NFR-06 / AC-NFR-06.2 | `test_arch_importlinter` | DRAFT |
| 18 | `pip-licenses --format=json` | every license ∈ allowlist | NFR-07 / AC-NFR-07.2 | `test_license_allowlist` | DRAFT |
| 19 | `bandit -r 03-development/src/` | 0 HIGH, 0 MEDIUM | NFR-02 / AC-NFR-02.5 | `test_security_bandit_clean` | DRAFT |
| 20 | `mutmut run` then `mutmut results` | mutation score ≥ 70 | NFR-08 / AC-NFR-08.2 | `test_mutation_score` | DRAFT |
| 21 | `make verify-system` | exit 0; stdout `verify-system: PASS` | NFR-12 / AC-NFR-12.2 | `test_makefile_verify_system` | DRAFT |
| 22 | After running a command with a secret, `grep -c "sk-" $TASKQ_HOME/audit.jsonl` | 0 | NFR-04 / AC-NFR-04.2 | `test_observability_redaction_no_plaintext` | DRAFT |

---

## 5. Completeness Verification

> Computed by `advance-phase` / `build_traceability` against this matrix. Status is
> the live scan result; a hand-edit to flip a `DRAFT` cell to `VERIFIED` is overwritten
> on the next advance (per R-CANONICAL-SPEC-PATH-001 style ownership rule).

| Check | Target | Actual | Status |
|-------|--------|--------|--------|
| FR → SRS mapping | 100% (8 / 8 FRs bound to §3) | 8 / 8 (FR-01..FR-08) | OK |
| NFR → SRS mapping | 100% (12 / 12 NFRs bound to §4) | 12 / 12 (NFR-01..NFR-12) | OK |
| FR / NFR → Code module | 100% (20 / 20 bound to a §6 module) | 20 / 20 (planned anchors; 0 implemented on disk in Phase 1) | PLANNED — DEFERRED to Phase 3 |
| FR / NFR → Test | 100% (each AC bound to one test function name from `TEST_INVENTORY.yaml` + §8) | 20 / 20 (placeholder names declared; tests not yet written) | PLANNED — DEFERRED to Phase 3 |
| Unit-test line coverage | ≥ 100% | 0% (no tests yet) | NOT_VERIFIED |
| Integration-test line coverage | ≥ 80% | 0% (no tests yet) | NOT_VERIFIED |
| Skipped tests count | 0 | N/A (no tests yet) | NOT_VERIFIED |
| Mutex with `.ignore` / `collect_ignore` / `testpaths` exclusion | none | none configured | OK |
| `lint-imports` | exit 0 | N/A (no `.importlinter` yet) | NOT_IMPLEMENTED |
| `mutmut` mutation score (over `service/` + `storage/`) | ≥ 70 | N/A | NOT_VERIFIED |
| SPEC §8 #22 redaction audit (`grep -c "sk-" audit.jsonl` == 0) | 0 | N/A | NOT_VERIFIED |
| Bidirectional consistency: every FR row cites a SPEC § anchor | 100% | 100% (all 20 rows cite `SPEC.md` §3 / §4) | OK |

---

## 6. ASPICE Compliance

| ASPICE Capability | Evidence in this Matrix | Status |
|-------------------|--------------------------|--------|
| SWE.3.B.SP1 — Task-to-work-product traceability (each FR has an SRS § + code module + test) | §1 / §2 / §3 / §4 of this matrix | PLANNED — material complete; live binding awaits Phase 3 implementation |
| SWE.3.B.SP2 — Bidirectional traceability (every SRS § clause and every code symbol can be traced to ≥ 1 FR / NFR) | §1 (FR→SRS), §2 (SRS→Code), §3 (Code→Test) — fully reversible via the SRS § column | PLANNED — material complete; live binding awaits Phase 3 implementation |
| SWE.3.B.SP3 — Traceability consistency (no orphan rows; SPEC §8 #1–22 each bound to a row) | §4 (all 22 SPEC §8 items enumerated; no orphans) | OK (matrix-side); test-side awaits Phase 3 |

---

## 7. Cross-References

- Spec source: `/Users/johnny/projects/taskq-plus/SPEC.md` (root, canonical per R-CANONICAL-SPEC-PATH-001)
- Spec tracking: `/Users/johnny/projects/taskq-plus/01-requirements/SPEC_TRACKING.md`
- SRS: `/Users/johnny/projects/taskq-plus/01-requirements/SRS.md`
- Test inventory (P1 naming authority): `/Users/johnny/projects/taskq-plus/TEST_INVENTORY.yaml`
- Test spec (P2 single source of truth): `/Users/johnny/projects/taskq-plus/02-architecture/TEST_SPEC.md`
- Architecture: `/Users/johnny/projects/taskq-plus/02-architecture/SAD.md`
- Acceptance index: `SRS.md` §5 (reproduces SPEC §8 #1–22)
- Exit-code map: `SRS.md` §5 (`0`/`2`/`3`/`4`/`5`/`6`/`1` from SPEC §3 / §7)
- Env-var contract: `SRS.md` §5 (12 `TASKQ_*` variables from SPEC §5.1)
- Data-file contract: `SRS.md` §5 (`tasks.json` / `breaker.json` / `cache.json` / `audit.jsonl`)
- High-risk modules: SPEC §10 / SRS §8 — `executor` / `plugins` / `task_store`

---

## Update log

| Date | Change | By |
|------|--------|----|
| 2026-07-30 | Round 1 — rebuilt matrix from `SPEC.md` §3 / §4 (canonical), bound to `SRS.md` §3 / §4, mapped to `SPEC.md` §6 code anchors, linked to `TEST_INVENTORY.yaml` + `TEST_SPEC.md` + SPEC §8 acceptance index; H1 anchored to "Traceability Matrix — taskq-plus" | Agent A (Sub-Task 3/4) |