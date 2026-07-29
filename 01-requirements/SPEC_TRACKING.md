# Specification Tracking Matrix — taskq-plus

> On-demand Lazy Load template.
>
> Project Info
> - Project Name: taskq-plus
> - Canonical Spec: `SPEC.md` (project root)
> - SRS Source: `01-requirements/SRS.md`
> - Version: v1.0.0
> - Phase: 1 — INGESTION MODE
> - Created: 2026-07-30
> - Owner: Agent A (Sub-Task 2/4)

## Specification Status

> **The Status column is machine-refreshed** — `advance-phase` overwrites each
> FR's Status from `build_traceability`'s live code/test scan (IN_PROGRESS once
> code/module exists, VERIFIED once code+test exist). The authoritative status is
> that scan / `quality_manifest.json`, NOT this hand-filled cell. Fill the
> semantic columns (Spec Description / Intent Class / Decision Framework / Notes);
> leave Status to refresh itself (a hand-edit is overwritten on the next advance).
>
> **Canonical spec source path:** every Ownership / Source / Citation / Reference
> cell that points back to the spec source MUST use bare `SPEC.md` (repo root).
> The non-root directory-prefixed form of the spec filename is **not** the
> canonical path and MUST NOT appear anywhere in this matrix.
> // @rule R-CANONICAL-SPEC-PATH-001

### Functional Requirements

| FR ID | Spec Description | Intent Class | Decision Framework | Status | Notes |
|-------|------------------|--------------|--------------------|--------|-------|
| FR-01 | 任務提交與驗證 (`taskq-plus submit "<cmd>" [--name N] [--after ID]...`):pydantic v2 `TaskSubmission` 驗證;違反 → exit 2;通過 → uuid4 8-hex id,寫入 `$TASKQ_HOME/tasks.json`(atomic),`submit` audit event。 | Form / Input Validation | Reject on violation → exit 2; on pass → atomic write + audit event | DRAFT | Owner: Agent C (service/cli); Source: `SPEC.md` §3 FR-01 + §7 exit-code 2; Phase-3 TDD: `service.submission` |
| FR-02 | 任務執行器 (`run <id>` / `run --all`):`subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=…)`;`shell=True` 禁用;`--all` 用 `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)` + Kahn 拓撲,shared `threading.Lock`;state machine `pending→running→done\|failed\|timeout\|blocked`;timeout → exit 4。 | Execution / Scheduling | shell=False mandatory; state transitions per verbatim table; atomic write per terminal status | DRAFT | Owner: Agent C (service/executor); Source: `SPEC.md` §3 FR-02 + §7 exit-code 4; High-risk module per `SPEC.md` §10 |
| FR-03 | 重試與斷路器:`failed`/`timeout` 自動重試 ≤ `TASKQ_RETRY_LIMIT`,backoff `TASKQ_BACKOFF_BASE × 2^n`(sleep 可注入);連續最終失敗 ≥ `TASKQ_BREAKER_THRESHOLD` → `OPEN`(持久化 `$TASKQ_HOME/breaker.json`),run 拒絕 exit 3;`TASKQ_BREAKER_COOLDOWN` 後 → `HALF_OPEN`,成功 → `CLOSED`,失敗 → `OPEN`。 | Reliability / Retry + Breaker | Retry on terminal failure; OPEN rejects → exit 3; HALF_OPEN admits one task; cooldown transition per canonical | DRAFT | Owner: Agent C (service/breaker); Source: `SPEC.md` §3 FR-03 + §7 exit-code 3; Open issue NFR-99 on counter semantics |
| FR-04 | 結果 TTL 快取:sig = `sha256(command)`;`run <id> --cached` 在 `TASKQ_CACHE_TTL` 秒內同簽名且上次結果為 `done` → 直接回放(`exit_code`/`stdout_tail`),標記 `cached: true`,不執行 subprocess;過期 / 不存在 → 正常執行並原子寫入 `$TASKQ_HOME/cache.json`;讀寫 thread-safe。 | Performance / Cache | sha256(command) is the only key; replay only when cached result == done AND age < TTL | DRAFT | Owner: Agent C (service/cache); Source: `SPEC.md` §3 FR-04 + §8 #9; Open issue NFR-99 on normalisation |
| FR-05 | CLI 整合:`click` group;子命令 `submit` / `run` / `status` / `list` / `graph` / `plugins list` / `export` / `clear`;全域旗標 `--json`;exit-code 表:`0`/`2`/`3`/`4`/`5`/`6`/`1`。 | Interface / CLI | All subcommands reachable; exit codes bind to canonical map; `--json` → single-line JSON | DRAFT | Owner: Agent C (cli layer); Source: `SPEC.md` §3 FR-05 + §7 exit-code map; Bound by C-02 + NFR-06 |
| FR-06 | 任務相依 DAG:`--after` 建立 `depends_on` 邊;`run --all` Kahn 拓撲,同層並發;非 `done` 上游 → 下游 `blocked`,不執行、不計入 breaker;循環 → 拒絕提交,exit 5 + stderr 循環路徑;鏈深度 > `TASKQ_MAX_DAG_DEPTH` → 拒絕,exit 5;`graph --format text\|dot` 對應輸出。 | Scheduling / DAG | Kahn topological sort; blocked ≠ counted toward breaker; cycle/depth → exit 5 | DRAFT | Owner: Agent C (service/dag); Source: `SPEC.md` §3 FR-06 + §7 exit-code 5; High-risk module per `SPEC.md` §10 |
| FR-07 | Plugin Hook 系統:`TASKQ_PLUGINS` allowlist 模組名,`importlib.import_module` named-load;hook = `pre_run(task)` / `post_run(task, result)`;模組名必匹配 `^[A-Za-z_][A-Za-z0-9_.]*$`,否則 exit 6;禁用 `eval`/`exec`/`__import__` 動態字串、禁用路徑 / URL;plugin 例外不中止執行,寫 `plugin_error` audit,連 3 次失敗停用該 plugin(本次 run 內)。 | Extension / Plugin | Allowlist regex; importlib named-load only; exception isolation + consecutive-failure disable | DRAFT | Owner: Agent C (service/plugins); Source: `SPEC.md` §3 FR-07 + §7 exit-code 6 + NFR-02; High-risk module per `SPEC.md` §10 |
| FR-08 | 結構化稽核日誌與匯出:`$TASKQ_AUDIT_LOG`(default `$TASKQ_HOME/audit.jsonl`),JSON Lines,append + fsync;event 種類 `submit`/`run_start`/`run_end`/`retry`/`breaker_open`/`breaker_close`/`cache_hit`/`blocked`/`plugin_error`,每個帶 `correlation_id`(per CLI invocation);`export --format json\|csv\|md` 三格式行數 / 欄位集合一致(NFR-04 redaction 寫入前套用)。 | Observability / Audit | Per-event schema verbatim; correlation_id per CLI invocation; CSV = RFC 4180 quoting | DRAFT | Owner: Agent C (observability); Source: `SPEC.md` §3 FR-08 + §8 #14 / #22 |

### Non-Functional Requirements

| FR ID | Spec Description | Intent Class | Decision Framework | Status | Notes |
|-------|------------------|--------------|--------------------|--------|-------|
| NFR-01 | 效能預算:`submit`+`status` 100 次 p95 < 50ms;`run --all` 200 任務拓撲排序 p95 < 200ms;量測工具 `pytest-benchmark`,結果 JSON 落盤。 | Performance / Latency Budget | p95 < 50ms (submit+status); p95 < 200ms (topo sort, 200 tasks); no subprocess wall-clock | DRAFT | Owner: Agent D (perf bench); Source: `SPEC.md` §4 NFR-01; Open issue NFR-99 on measurement boundary |
| NFR-02 | 執行與載入安全:全 codebase 禁用 `shell=True`(grep 0 命中)、禁用 `eval(`/`exec(`/`__import__(`(grep 0 命中);FR-01 注入字元(`;\|&$><` `` ` ``)每個一 case;plugin 名稱必通過 `^[A-Za-z_][A-Za-z0-9_.]*$` 白名單,拒絕路徑 / URL;`bandit -r 03-development/src/` → 0 HIGH / 0 MEDIUM。 | Security / Execution + Loading | grep-based blacklists + allowlist regex + bandit 0/0 | DRAFT | Owner: Agent C + Agent D (security); Source: `SPEC.md` §4 NFR-02 + §8 #15 / #19; binds FR-01 / FR-07 |
| NFR-03 | 錯誤處理與原子性:四個資料檔(`tasks.json`/`breaker.json`/`cache.json`/`audit.jsonl`)全部 atomic write(tmp + `os.replace`,audit 為 append + fsync);禁裸 `except:` / `except Exception: pass` / 吞 `KeyboardInterrupt`/`SystemExit`;`OPEN → CLOSED` 恢復 ≤ `TASKQ_BREAKER_COOLDOWN` + 1s。 | Reliability / Atomicity + Exception Hygiene | tmp + os.replace; fsync on append; except-blocks must reraise / translate / exit-with-code | DRAFT | Owner: Agent C (storage layer); Source: `SPEC.md` §4 NFR-03 + C-07; Open issue NFR-99 on breaker-clock basis |
| NFR-04 | 敏感資料遮蔽:`stdout_tail`/`stderr_tail`/audit `detail` 寫入前套用 redaction regex `(sk-[A-Za-z0-9_-]{8,}\|token=\S+\|Bearer\s+\S+)`,命中行整行以 `[REDACTED]` 取代;遮蔽在寫入前(以「檔案內容不含明文 secret」斷言)。 | Security / Redaction | regex applied before persistence; verifiable by `grep -c "sk-"` == 0 | DRAFT | Owner: Agent C (observability); Source: `SPEC.md` §4 NFR-04 + §8 #22 |
| NFR-05 | 文件覆蓋:`03-development/src/taskq_plus` 公開函式 / 類別 docstring 100%(`ast-docstrings`),且每條 docstring 含 `[FR-XX]` 或 `[NFR-XX]` 引用。 | Documentation / Docstring Quality | 100% coverage + bracketed tag per symbol | DRAFT | Owner: Agent D (docs gate); Source: `SPEC.md` §4 NFR-05 |
| NFR-06 | 架構分層契約:repo root `.importlinter` 宣告 `cli > observability > service > storage > models`,`config` 為 independence 模組;`lint-imports` exit 0;禁止以刪除 `.importlinter` / `ignore_imports` 萬用字元 / 縮減為單條 `forbidden` 取得通過。 | Architecture / Layering | .importlinter contract verbatim; lint-imports exit 0; no weakening | DRAFT | Owner: Agent D (arch gate); Source: `SPEC.md` §4 NFR-06 + §8 #17; binds C-09 |
| NFR-07 | 依賴與授權合規:`requirements.txt` 全 runtime dep 以 `==` 釘版;allowlist {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0};掃描含已安裝依賴樹(`pip-licenses` 或 `scancode`);`08-config/SBOM.json` 列出 `name`/`version`/`license`。 | License Compliance / SBOM | `==` pinned only; allowlist set reproducible; SBOM artefact present | DRAFT | Owner: Agent D (license scan); Source: `SPEC.md` §4 NFR-07 + §8 #18 |
| NFR-08 | 變異測試:`.methodology/harness_config.json` 設 `features.mutation_testing: true`;`mutmut run` + `mutmut results` mutation score ≥ 70;範圍限 `03-development/src/taskq_plus/service/` 與 `.../storage/`(並於 harness_config 註記理由)。 | Test Quality / Mutation Testing | score ≥ 70 over service/ + storage/; scope annotated | DRAFT | Owner: Agent D (mutmut); Source: `SPEC.md` §4 NFR-08 + §8 #20 |
| NFR-09 | 驗證真實性(零 skip 鐵律):`pytest 03-development/tests -q` 報告 `skipped` = 0;每個測試函式 ≥ 1 個 `assert`(`ast-assertions` `zero_assert == 0`);不得以 `--ignore` / `-k` / `--deselect` / `collect_ignore` / `testpaths` 排除;`TRACEABILITY_MATRIX.md` 的 `VERIFIED` 僅在測試實際執行並通過時才給出。 | Test Quality / Zero-Skip | pytest summary 0 skipped; zero_assert == 0; no exclusion; VERIFIED bound to real run | DRAFT | Owner: Agent D (gate); Source: `SPEC.md` §4 NFR-09 + §8 #1; binds C-10 |
| NFR-10 | 整合覆蓋:`03-development/tests/integration/` 行覆蓋 ≥ 80%;經由 CLI 入口(`python -m taskq_plus`)或 `click.testing.CliRunner`;涵蓋 submit→run→status 全鏈 / DAG 多層 / breaker 開闔 / cache 命中 / plugin hook / export 三格式。 | Test Quality / Integration | coverage ≥ 80% on integration subset; CLI-driven only; scenario list verbatim | DRAFT | Owner: Agent D (integration); Source: `SPEC.md` §4 NFR-10 + §8 #3 |
| NFR-11 | 可讀性:專案 MI(LLOC 加權)≥ 80;單函式 cyclomatic complexity ≤ 10;單檔 ≤ 400 行;單一目錄 ≤ 15 檔。 | Code Quality / Readability | MI ≥ 80; CC ≤ 10; file ≤ 400 LOC; dir ≤ 15 files | DRAFT | Owner: Agent D (readability gate); Source: `SPEC.md` §4 NFR-11 |
| NFR-12 | 系統驗證目標:`Makefile` 提供 `verify-system` target,串接全套測試 + CLI 冒煙(`submit`/`run`/`status`/`graph`/`export`/`clear`);`make verify-system` exit 0 且 stdout 印 `verify-system: PASS`。 | Verification Target / Make Target | exit 0 + literal `verify-system: PASS` on stdout | DRAFT | Owner: Agent D (verify-system); Source: `SPEC.md` §4 NFR-12 + §8 #21 |

### Coverage Cross-Check (machine-checked at every advance-phase)

| Check | Target | Actual | Status |
|-------|--------|--------|--------|
| FR-01..FR-08 rows present | 8 / 8 | 8 | OK |
| NFR-01..NFR-12 rows present | 12 / 12 | 12 | OK |
| Total rows | 20 | 20 | OK |
| Canonical source path uses bare `SPEC.md` | 100% | 100% | OK |
| Every row cites `SPEC.md` § anchor | 100% | 100% | OK |

---

## Update log

| Date | Change | By |
|------|--------|----|
| 2026-07-30 | Round 1 — populated FR-01..FR-08 + NFR-01..NFR-12 from `SRS.md` §3 / §4; H1 anchored to "Specification Tracking Matrix — taskq-plus"; canonical spec source locked to root `SPEC.md` per R-CANONICAL-SPEC-PATH-001 | Agent A |