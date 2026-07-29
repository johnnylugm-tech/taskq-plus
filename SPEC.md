# taskq-plus — 規格文件(單一事實來源)

> 本文件為 `taskq-plus` 的完整規格。**所有實作以此文件為準。**
> 專案角色:harness-methodology 漸進式驗證測床**第 1 輪**(Python CLI 補洞版)——
> 以真實小型專案形態完整行使 Phase 1–8 開發管線,並**點亮前一個測床(taskq)無法行使的品質維度**。

---

## 0. 文件元資料

| 欄位 | 值 |
|------|-----|
| 文件版本 | v1.0.0 |
| 專案名稱 | `taskq-plus` |
| 驗證輪次 | 第 1 輪 / 共 3 輪(第 2 輪 `SPEC-2.md` 後端+DB;第 3 輪 TypeScript,暫緩) |
| 前一測床 | `taskq`(run-all-by-workflow,SPEC v4.0.0,5 FR / 10 NFR) |
| 制訂日期 | 2026-07-30 |
| 配套檔案 | `PROJECT_BRIEF.md`(8 FR / 12 NFR / 12 env 同步)、`.env.example`、`.importlinter`、`requirements.txt`、`Makefile` |
| 文件責任 | 規格單一真實來源(Single Source of Truth);所有實作以此為準 |
| Phase 1 規範 | Agent A INGESTION MODE — 100% transcribe 全部 `### FR-01..FR-08` 與 `### NFR-01..NFR-12` heading,no invention,no omission |

### 本輪設計意圖(為何不是 taskq 的翻版)

前一個測床在 Gate 4 拿到 composite 97.4 全綠,但事後盤點發現**五個品質維度在該專案形態下無法產生信號**。本規格逐條針對:

| 前輪缺口 | 本輪對策 | 條款 |
|---|---|---|
| runtime 零外部依賴 → `license_compliance` 掃 19 個自家檔案恆滿分 | 引入釘版第三方依賴,且**掃描範圍必須含已安裝依賴樹** | NFR-07 |
| 無 `.importlinter` → `architecture_constraints`(Gate 1 權重 0.25)無條件送分 | **強制**宣告分層契約,`lint-imports` 必須實際執行 | NFR-06 |
| `mutation_testing` 預設關閉 → 三個 Gate 全 null | 明文開啟並設定分數門檻 | NFR-08 |
| 進階 NFR 的驗證測試 15/16 是 `pytest.skip` 空殼卻標記 VERIFIED | **零 skip 鐵律** + 反造假條款 | NFR-09 |
| 單一扁平 package(21 nodes)→ `architecture` 恆 100,且需調降 `crg_cohesion_healthy` 遷就 | 五層分層架構,**禁止調降** CRG 校準值 | NFR-06 / §10 |

### 變更日誌

| 版本 | 日期 | 動作 | 摘要 |
|------|------|------|------|
| v1.0.0 | 2026-07-30 | initial | 8 FR / 12 NFR / 12 env — 第 1 輪測床基線 |

---

## 1. 概述

- **專案名稱**:`taskq-plus`
- **目的**:本地任務佇列 CLI — 提交 shell 命令為任務,受控執行(timeout / 重試 / 斷路器 / 快取 / **相依 DAG**),支援 **plugin hook** 與**結構化稽核日誌**,狀態可查詢可匯出
- **語言**:Python 3.11
- **依賴策略**:**明確引入釘版第三方依賴**(與前輪的「零依賴」相反,這是本輪的設計目的之一 — 見 NFR-07)
- **形態**:命令列工具,`python -m taskq_plus` 進入

---

## 2. 技術架構

| 元件 | 技術 |
|------|------|
| CLI | `click` 群組化子命令 |
| 資料驗證 | `pydantic` v2 模型 |
| 任務執行 | `subprocess`(`shlex.split`,禁 `shell=True`) |
| 並發 | `concurrent.futures.ThreadPoolExecutor` |
| 相依排程 | 有向圖拓撲排序(Kahn 演算法),循環必須拒絕 |
| Plugin | `importlib` 依 allowlist 具名載入,禁 `eval`/`exec`/路徑注入 |
| 持久化 | JSON 檔(原子寫:tmp + `os.replace`) |
| 稽核日誌 | JSON Lines,每筆含 `correlation_id` |
| 執行緒安全 | `threading.Lock` 保護共享存儲 |
| 設定 | `TASKQ_*` 環境變數(`config.py` 統一讀取) |
| 分層約束 | `import-linter` layers contract(見 NFR-06) |

---

## 3. 功能需求(Functional Requirements)

### FR-01: 任務提交與驗證

`taskq-plus submit "<command>" [--name NAME] [--after ID]...`

提交的欄位由 **`pydantic` 模型 `TaskSubmission`** 驗證,任一違反 → **exit 2** + stderr 錯誤訊息,不寫入存儲:

| 規則 | 條件 |
|------|------|
| 非空 | 命令為空或全空白 → 拒絕 |
| 長度 | 命令 > 1000 字元 → 拒絕 |
| 注入字元 | 命令含 `;` `\|` `&` `$` `>` `<` `` ` `` 任一 → 拒絕(NFR-02) |
| 名稱唯一 | `--name` 與既有 pending/running 任務重複 → 拒絕 |
| 相依存在 | `--after` 指向不存在的 task id → 拒絕 |

通過驗證:

- 產生 task id(uuid4 前 8 hex)
- 狀態 `pending`,記錄 `command`、`name`、`created_at`、`depends_on`(list[str])
- 原子寫入 `$TASKQ_HOME/tasks.json`
- stdout 輸出 task id(`--json` 時輸出 `{"id": ..., "status": "pending"}`)
- 寫一筆 `submit` 稽核事件(FR-08)

### FR-02: 任務執行器

`taskq-plus run <id>` 或 `taskq-plus run --all`

- 以 `subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=TASKQ_TASK_TIMEOUT)` 執行;**任何路徑不得使用 `shell=True`**
- 狀態機:`pending → running → done | failed | timeout | blocked`
  - exit 0 → `done`;非 0 → `failed`;`TimeoutExpired` → `timeout`
  - 相依未滿足 → `blocked`(FR-06)
- 結果欄位:`exit_code`、`stdout_tail`(末 2000 字元)、`stderr_tail`(末 2000 字元)、`duration_ms`、`finished_at`
- `--all`:以 `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)` 依 **DAG 拓撲順序**(FR-06)並發執行全部可執行的 `pending` 任務;存儲寫入必須執行緒安全(共享 Lock)
- 單一任務模式下 `timeout` 結果 → **exit 4**

### FR-03: 重試與斷路器

**重試**:`run` 結果為 `failed`/`timeout` 時自動重試,上限 `TASKQ_RETRY_LIMIT` 次;第 n 次重試前等待 `TASKQ_BACKOFF_BASE × 2^n` 秒(exponential backoff;sleep 函式必須可注入以利測試)。

**斷路器**(全域,跨任務、跨進程):

- 連續最終失敗(重試耗盡仍 failed/timeout)計數 ≥ `TASKQ_BREAKER_THRESHOLD` → `OPEN`
- `OPEN` 期間任何 `run` 立即拒絕:**exit 3** + stderr `breaker open`,不執行 subprocess
- 經 `TASKQ_BREAKER_COOLDOWN` 秒後進入 `HALF_OPEN`:放行一個任務 — 成功 → `CLOSED` 且計數歸零;失敗 → 重新 `OPEN`
- 狀態持久化於 `$TASKQ_HOME/breaker.json`(原子寫)

### FR-04: 結果 TTL 快取

- 快取簽名 = `sha256(command)`
- `taskq-plus run <id> --cached`:同簽名且結果為 `done` 的最近執行在 `TASKQ_CACHE_TTL` 秒內 → 直接回放(`exit_code`/`stdout_tail`),**不執行 subprocess**,任務標記 `done` 且 `cached: true`
- 快取過期或不存在 → 正常執行,成功(`done`)後寫入 `$TASKQ_HOME/cache.json`
- 快取讀寫:原子 + 執行緒安全(與 FR-02 並發共存)

### FR-05: CLI 整合

`click` 群組化子命令(入口 `python -m taskq_plus`):

| 命令 | 行為 |
|------|------|
| `submit "<cmd>" [--name N] [--after ID]...` | FR-01 |
| `run <id> [--cached]` / `run --all` | FR-02/03/04/06 |
| `status <id>` | 輸出該任務全欄位 |
| `list [--status S]` | 列出任務(可按狀態過濾) |
| `graph [--format text\|dot]` | 輸出相依圖(FR-06) |
| `plugins list` | 列出已載入的 plugin 與其 hook(FR-07) |
| `export --format json\|csv\|md` | 匯出任務結果(FR-08) |
| `clear` | 清空 `$TASKQ_HOME` 全部資料檔 |

- 全域 flag `--json`:機器可讀輸出(單行 JSON)
- **Exit codes**:`0` 成功 / `2` 輸入驗證錯誤(含 unknown task id) / `3` breaker open / `4` 任務 timeout / `5` 相依圖存在循環或深度超限 / `6` plugin 載入失敗 / `1` 其他內部錯誤

### FR-06: 任務相依 DAG

- `submit --after <id>` 可重複指定,建立 `depends_on` 邊
- `run --all` 以 **Kahn 拓撲排序**決定執行順序;同一層(入度為 0)的任務才可並發
- 相依任務結果非 `done` → 下游任務標記 `blocked`,**不執行**,且不計入斷路器失敗計數
- **循環偵測**:`submit --after` 若會造成循環 → 拒絕該次提交,**exit 5** + stderr 列出循環路徑(`A → B → C → A`)
- **深度上限**:相依鏈深度 > `TASKQ_MAX_DAG_DEPTH` → 拒絕,exit 5(防止病態輸入耗盡資源)
- `graph --format dot` 輸出 Graphviz DOT;`--format text` 輸出縮排樹

### FR-07: Plugin Hook 系統

- Plugin 是一個 Python 模組,提供 `pre_run(task) -> None` 與/或 `post_run(task, result) -> None`
- 載入來源:**僅限** `TASKQ_PLUGINS` 環境變數列出的模組名(逗號分隔 **allowlist**),以 `importlib.import_module` 具名載入
- **安全鐵律**(NFR-02):
  - 禁止 `eval` / `exec` / `__import__` 動態字串
  - 禁止從檔案路徑或 URL 載入(只接受已安裝的模組名)
  - Plugin 模組名必須匹配 `^[A-Za-z_][A-Za-z0-9_.]*$`,不符 → 拒絕載入,**exit 6**
- Plugin 拋出例外 → **不得**中斷任務執行:記錄 `plugin_error` 稽核事件(FR-08)並繼續;連續 3 次失敗的 plugin 於本次執行內停用
- `plugins list` 輸出每個 plugin 的模組名、註冊的 hook、載入狀態

### FR-08: 結構化稽核日誌與匯出

**稽核日誌**:

- 路徑 `$TASKQ_AUDIT_LOG`(預設 `$TASKQ_HOME/audit.jsonl`),**JSON Lines**,append-only
- 每筆欄位:`ts`(ISO-8601 UTC)、`event`、`task_id`、`correlation_id`、`detail`
- `correlation_id` 由一次 CLI 呼叫產生,該次呼叫觸發的所有事件共用同一個值
- 事件種類:`submit` / `run_start` / `run_end` / `retry` / `breaker_open` / `breaker_close` / `cache_hit` / `blocked` / `plugin_error`
- 落盤前套用 NFR-04 的 redaction

**匯出**:

- `export --format json`:單一 JSON 陣列,欄位同 `status`
- `export --format csv`:標頭列 + 每任務一列,含逗號/引號的欄位必須正確跳脫
- `export --format md`:Markdown 表格
- 三種格式的任務筆數與欄位集合必須一致(以測試斷言)

---

## 4. 非功能需求(Non-Functional Requirements)

> **維度映射鐵律**:以下每一條 NFR 的 `dimension` 欄位都必須是
> `harness/toolchains/registry.py::DIMENSION_TOOLS["python"]` 實際存在的 key。
> 前一輪測床把 NFR-06 標為 `deployability`(不是有效維度),結果該條在
> `CLAUDE.md` 的 NFR→dimension 映射中被靜默丟棄 — 10 條只映射了 9 條。

### NFR-01: 效能預算

- **dimension**:`performance`
- `submit` + `status` 組合操作(不含 subprocess 執行)100 次 **p95 < 50ms**
- `run --all` 對 200 個任務的**拓撲排序階段**(不含 subprocess 執行)**p95 < 200ms**
- 量測方式:`pytest-benchmark`,結果寫入 benchmark JSON

### NFR-02: 執行與載入安全

- **dimension**:`security`
- 全 codebase **禁用 `shell=True`**(以 grep 驗證,0 命中)
- FR-01 注入字元黑名單必須有測試覆蓋(每個字元一個 case)
- **Plugin 載入面**(FR-07):全 codebase 禁用 `eval(` / `exec(` / `__import__(`;plugin 名稱必須通過 `^[A-Za-z_][A-Za-z0-9_.]*$` 白名單正則;不得接受檔案路徑或 URL
- `bandit -r 03-development/src/` 結果:**0 HIGH、0 MEDIUM**

### NFR-03: 錯誤處理與原子性

- **dimension**:`error_handling`
- 四個資料檔(`tasks.json`/`breaker.json`/`cache.json`/`audit.jsonl`)全部原子寫(tmp + `os.replace`;audit 為 append + fsync),進程中斷後檔案仍為合法 JSON / JSONL
- **不得**出現裸 `except:`、`except Exception: pass`、吞掉 `KeyboardInterrupt`/`SystemExit`
- 每個 `except` 區塊必須是三者之一:重新拋出、轉譯為明確的領域例外、記錄後以明確 exit code 結束
- breaker `OPEN → CLOSED` 恢復時間 ≤ `TASKQ_BREAKER_COOLDOWN` + 1s

### NFR-04: 敏感資料遮蔽

- **dimension**:`security`
- `stdout_tail` / `stderr_tail` / 稽核日誌 `detail` 落盤前,匹配 `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` 的行整行以 `[REDACTED]` 取代
- 遮蔽發生在**寫入前**,不是讀取後(以「檔案內容不含明文 secret」斷言)

### NFR-05: 文件覆蓋

- **dimension**:`documentation`
- `03-development/src/taskq_plus` 全部公開函式/類別有 docstring 且含 `[FR-XX]` 或 `[NFR-XX]` 引用
- 覆蓋率 **100%**(`ast-docstrings` 量測)

### NFR-06: 架構分層契約

- **dimension**:`architecture_constraints`
- 專案根目錄**必須存在 `.importlinter`**,宣告 layers contract:

  ```
  cli > observability > service > storage > models
  ```

  上層可 import 下層,**下層不得 import 上層**;`config` 為 independence 模組,任何層都可 import 它,但它不得 import 任何層
- `lint-imports` 必須 **exit 0**
- **禁止**以刪除 `.importlinter`、把 contract 放寬成萬用字元 `ignore_imports`、或降級為單條 `forbidden` 的方式取得通過
- 前一輪缺口記錄:`harness/tool_runners.py:69-72` 在 `.importlinter` 缺席時直接回傳 exit 0 → 該維度(Gate 1 權重 **0.25**)成為無條件滿分。本條款存在的唯一目的就是讓這個權重真的被行使。

### NFR-07: 依賴與授權合規

- **dimension**:`license_compliance`
- 全部 runtime 依賴在 `requirements.txt` 以 `==` **釘版**(不得 `>=` / `~=` / 無版本)
- 允許的 license:**MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0**;出現其他 license → 該依賴不得使用
- **掃描範圍必須包含已安裝的依賴樹**,不得只掃自家原始碼。可接受的證據命令(擇一):
  - `pip-licenses --format=json --with-urls`
  - `scancode --license <venv>/lib/python3.11/site-packages --json-pp -`
- 產出 SBOM 於 `08-config/SBOM.json`,列出每個依賴的 `name` / `version` / `license`
- 前一輪缺口記錄:taskq runtime 零依賴,該維度的證據逐字是「19 source files scanned」——掃的是自家 src,恆 100 分無信號。

### NFR-08: 變異測試

- **dimension**:`mutation_testing`
- `.methodology/harness_config.json` 必須設 `features.mutation_testing: true`
- **mutation score ≥ 70**
- 範圍限定於 `03-development/src/taskq_plus/service/` 與 `.../storage/` 兩層(核心邏輯),並在 `harness_config.json` 以註記說明限定理由(執行時間預算)
- 前一輪缺口記錄:該旗標預設 `False`(`core/harness_config.py:56`),taskq 未覆寫 → Gate 2/3/4 的 `mutation_testing` 全部是 `null`,權重 0.08 被 renormalise 掉。

### NFR-09: 驗證真實性(零 skip 鐵律)

- **dimension**:`test_assertion_quality`
- **任何 FR / NFR 的驗證測試不得是 `pytest.skip` / `pytest.mark.skip` / `skipif` / `xfail` / 無斷言的 stub**
- `pytest 03-development/tests -q` 的輸出中 **skipped 計數必須為 0**
- 每個測試函式至少一個 `assert`(`ast-assertions` 量測 `zero_assert == 0`)
- **反造假條款**:不得以 `--ignore` / `-k` / `--deselect` / `collect_ignore` / 從 `testpaths` 移除目錄等方式排除測試來達成上述數字
- `TRACEABILITY_MATRIX.md` 的 `VERIFIED` 標記,只能在該需求的驗證測試**實際執行並通過**時給出;測試若不存在或未執行,狀態必須是 `NOT_VERIFIED`
- 前一輪缺口記錄:taskq 的 NFR-07~10 專屬測試 **15/16 是 `pytest.skip("... deferred to P5")` 空殼**,而 P5 早已走完、Gate 3/4 全綠,`TRACEABILITY_MATRIX.md` 仍將四條全標為 `VERIFIED`。本條是防止重演的唯一機制。

### NFR-10: 整合覆蓋

- **dimension**:`integration_coverage`
- `03-development/tests/integration/` 的跨模組整合測試,行覆蓋 **≥ 80%**
- 整合測試必須經由 CLI 入口(`python -m taskq_plus`)或 `click.testing.CliRunner` 驅動,不得直接呼叫內部函式
- 至少涵蓋:submit→run→status 全鏈、DAG 多層執行、breaker 開闔、cache 命中、plugin hook 觸發、export 三格式

### NFR-11: 可讀性

- **dimension**:`readability`
- 專案 MI(LLOC 加權)**≥ 80**
- 單一函式 cyclomatic complexity **≤ 10**
- 單一檔案 ≤ 400 行;單一目錄 ≤ 15 檔

### NFR-12: 系統驗證目標

- **dimension**:`execute_verification_target`
- `Makefile` 必須提供 `verify-system` target,串接:全套測試 + CLI 冒煙(submit / run / status / graph / export / clear)
- `make verify-system` 必須 **exit 0** 並在 stdout 印出 `verify-system: PASS`

---

## 5. 參數配置

### 5.1 環境變數(`config.py` 讀取;`.env.example` 完整宣告)

| 變數 | 預設 | 說明 |
|------|------|------|
| `TASKQ_HOME` | `.taskq` | 資料檔目錄 |
| `TASKQ_MAX_WORKERS` | `4` | `run --all` 並發 worker 數 |
| `TASKQ_TASK_TIMEOUT` | `10.0` | 單任務 subprocess timeout(秒) |
| `TASKQ_RETRY_LIMIT` | `2` | 失敗自動重試上限 |
| `TASKQ_BACKOFF_BASE` | `0.1` | 重試退避基數(秒) |
| `TASKQ_BREAKER_THRESHOLD` | `3` | 連續失敗 → OPEN 閾值 |
| `TASKQ_BREAKER_COOLDOWN` | `5.0` | OPEN → HALF_OPEN 冷卻(秒) |
| `TASKQ_CACHE_TTL` | `3600` | 結果快取存活(秒) |
| `TASKQ_MAX_DAG_DEPTH` | `32` | 相依鏈深度上限(FR-06) |
| `TASKQ_PLUGINS` | (空字串) | plugin 模組名 allowlist,逗號分隔(FR-07) |
| `TASKQ_AUDIT_LOG` | `$TASKQ_HOME/audit.jsonl` | 稽核日誌路徑(FR-08) |
| `TASKQ_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

### 5.2 資料檔(`$TASKQ_HOME/`)

| 檔案 | 內容 | 格式 |
|------|------|------|
| `tasks.json` | `{version:1, tasks:{id→全欄位含 depends_on}}` | 原子寫 JSON |
| `breaker.json` | `{version:1, state, failure_count, opened_at}` | 原子寫 JSON |
| `cache.json` | `{version:1, entries:{簽名→done 結果 + cached_at}}` | 原子寫 JSON |
| `audit.jsonl` | 每行一筆稽核事件(FR-08) | append + fsync JSONL |

### 5.3 專案側必備設定檔(非可選)

| 檔案 | 用途 | 對應 |
|------|------|------|
| `.importlinter` | 分層契約 | NFR-06 |
| `requirements.txt` | 釘版 runtime 依賴 | NFR-07 |
| `requirements-dev.txt` | 開發工具(含 `import-linter`、`pip-licenses`、`mutmut`、`pytest-benchmark`) | NFR-06/07/08 |
| `.env.example` | 全部 12 個 `TASKQ_*` 逐一宣告並附註解 | §5.1 |
| `.methodology/harness_config.json` | `features.mutation_testing: true`;**不得**調降 `crg_cohesion_healthy` | NFR-08 / §10 |
| `Makefile` | `verify-system` target | NFR-12 |

---

## 6. 資料夾結構

```
taskq-plus/
├── 03-development/
│   ├── src/taskq_plus/
│   │   ├── __init__.py
│   │   ├── __main__.py            # python -m taskq_plus 入口
│   │   ├── config.py              # TASKQ_* env 讀取(independence 模組)
│   │   ├── models/                # L1 最底層 — 零內部依賴
│   │   │   ├── __init__.py
│   │   │   ├── task.py            # pydantic 模型(FR-01)
│   │   │   └── errors.py          # 領域例外(NFR-03)
│   │   ├── storage/               # L2 — 依賴 models
│   │   │   ├── __init__.py
│   │   │   ├── atomic.py          # tmp + os.replace(NFR-03)
│   │   │   ├── task_store.py      # tasks.json(FR-01/02)
│   │   │   ├── breaker_store.py   # breaker.json(FR-03)
│   │   │   └── cache_store.py     # cache.json(FR-04)
│   │   ├── service/               # L3 — 依賴 storage + models
│   │   │   ├── __init__.py
│   │   │   ├── executor.py        # subprocess + 重試(FR-02/03)
│   │   │   ├── breaker.py         # 斷路器(FR-03)
│   │   │   ├── cache.py           # TTL 快取(FR-04)
│   │   │   ├── dag.py             # 拓撲排序 + 循環偵測(FR-06)
│   │   │   └── plugins.py         # allowlist 載入 + hook(FR-07)
│   │   ├── observability/         # L4 — 依賴 service 以下
│   │   │   ├── __init__.py
│   │   │   ├── audit.py           # JSONL 稽核 + redaction(FR-08/NFR-04)
│   │   │   └── export.py          # json/csv/md 匯出(FR-08)
│   │   └── cli/                   # L5 最上層
│   │       ├── __init__.py
│   │       ├── main.py            # click group(FR-05)
│   │       └── commands.py
│   └── tests/
│       ├── unit/
│       └── integration/           # NFR-10
├── .importlinter                  # NFR-06
├── .env.example                   # §5.1
├── requirements.txt               # NFR-07
├── requirements-dev.txt
├── Makefile                       # NFR-12
├── PROJECT_BRIEF.md
└── SPEC.md                        # 本文件(單一事實來源)
```

**分層規則**(由 NFR-06 的 `.importlinter` 強制):`cli > observability > service > storage > models`;`config` 為 independence 模組。

---

## 7. 錯誤處理

| 情況 | 行為 |
|------|------|
| 空/非法命令(FR-01 規則) | exit 2,stderr 說明 |
| unknown task id | exit 2,stderr `unknown task: <id>` |
| `--after` 指向不存在的 id | exit 2,stderr `unknown dependency: <id>` |
| breaker OPEN | exit 3,stderr `breaker open`,不執行 |
| subprocess timeout | 任務狀態 `timeout`,單任務模式 exit 4 |
| 相依圖存在循環 | exit 5,stderr 列出循環路徑 |
| 相依鏈深度超限 | exit 5,stderr `dependency chain too deep: <n> > <max>` |
| plugin 名稱非法 / 模組不存在 | exit 6,stderr `plugin load failed: <name>: <reason>` |
| plugin 執行期拋例外 | **不中斷**;寫 `plugin_error` 稽核事件;連續 3 次失敗則停用該 plugin |
| `tasks.json` 損壞(非法 JSON) | 啟動偵測 → exit 1,stderr `store corrupted`(**不**靜默重建) |
| 其他未預期例外 | exit 1(不得裸 `except:` 吞噬 — NFR-03) |

---

## 8. 驗收標準

> 每條都是**可機器判定的單一命令 + 期望輸出**。不得以散文式描述取代。

| # | 命令 | 期望 |
|---|------|------|
| 1 | `pytest 03-development/tests -q` | 全綠,且輸出的 **skipped 計數為 0**(NFR-09) |
| 2 | `pytest 03-development/tests --cov=03-development/src --cov-report=term` | TOTAL **100%** |
| 3 | `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term` | TOTAL **≥ 80%**(NFR-10) |
| 4 | `python -m taskq_plus submit "echo hi"` | stdout 為 8-hex id,exit 0 |
| 5 | `python -m taskq_plus submit ""` | exit 2 |
| 6 | `python -m taskq_plus submit "echo hi; rm x"` | exit 2(注入字元) |
| 7 | `TASKQ_TASK_TIMEOUT=1 python -m taskq_plus run <sleep-5-id>` | 狀態 `timeout`,exit 4 |
| 8 | 3 個連續最終失敗後 `python -m taskq_plus run <id>` | exit 3;cooldown 後恢復可執行 |
| 9 | TTL 內 `python -m taskq_plus run <id> --cached` | 輸出 `cached: true`,無 subprocess 執行 |
| 10 | `python -m taskq_plus submit "echo b" --after <a>` 後 `run --all` | b 在 a 之後執行;a 非 done 時 b 為 `blocked` |
| 11 | 建立 A→B→A 的相依 | exit 5,stderr 含循環路徑 |
| 12 | `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` | exit 6(路徑形式被拒) |
| 13 | plugin 的 `pre_run` 拋例外後 `run <id>` | 任務仍完成;`audit.jsonl` 含 `plugin_error` 事件 |
| 14 | `python -m taskq_plus export --format json` / `csv` / `md` | 三者任務筆數相同;csv 逗號/引號正確跳脫 |
| 15 | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` | **0 命中**(NFR-02) |
| 16 | `grep -c "^TASKQ_" .env.example` | **12**(§5.1 全部宣告) |
| 17 | `lint-imports` | **exit 0**(NFR-06) |
| 18 | `pip-licenses --format=json` | 每個依賴的 license ∈ {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0}(NFR-07) |
| 19 | `bandit -r 03-development/src/` | 0 HIGH,0 MEDIUM(NFR-02) |
| 20 | `mutmut run` 後 `mutmut results` | mutation score **≥ 70**(NFR-08) |
| 21 | `make verify-system` | exit 0 且 stdout 含 `verify-system: PASS`(NFR-12) |
| 22 | 執行含 secret 的命令後 `grep -c "sk-" $TASKQ_HOME/audit.jsonl` | **0**(NFR-04) |

---

## 9. 風險矩陣

| ID | 風險 | 影響 | 可能性 | 緩解 |
|----|------|------|--------|------|
| R1 | 並發寫入損壞 tasks.json | 高 | 中 | Lock + 原子寫(NFR-03) |
| R2 | subprocess 懸掛/殭屍 | 中 | 中 | timeout 必設(FR-02) |
| R3 | breaker 誤鎖死 | 中 | 低 | cooldown + HALF_OPEN(FR-03) |
| R4 | 快取回放陳舊結果 | 低 | 中 | TTL 過期重執行(FR-04) |
| R5 | secret 落盤洩漏 | 高 | 中 | 寫入前 redaction(NFR-04) |
| R6 | **plugin 成為任意程式碼執行入口** | **高** | 中 | allowlist 具名載入 + 正則白名單 + 禁 eval/exec/路徑(FR-07 / NFR-02) |
| R7 | 病態相依圖耗盡資源 | 中 | 低 | 循環偵測 + 深度上限(FR-06) |
| R8 | plugin 例外中斷主流程 | 中 | 中 | 例外隔離 + 連續失敗停用(FR-07) |
| R9 | 依賴引入不相容 license | 中 | 低 | 釘版 + allowlist + SBOM(NFR-07) |
| R10 | 稽核日誌無限成長 | 低 | 高 | append-only,輪替由使用者負責 — **本輪不實作輪替**,列為已知限制 |

---

## 10. framework 對齊

本規格對齊 `harness-methodology` 的維度模型。**每個 dimension 名稱都取自 `DIMENSION_TOOLS["python"]` 的實際 key**:

| dimension(真實 key) | 工具 | 本規格條款 |
|---|---|---|
| `performance` | pytest-benchmark | NFR-01 |
| `security` | bandit | NFR-02、NFR-04 |
| `error_handling` | ast-error-handling | NFR-03 |
| `documentation` | ast-docstrings | NFR-05 |
| `architecture_constraints` | import-linter | **NFR-06** |
| `license_compliance` | scancode / pip-licenses | **NFR-07** |
| `mutation_testing` | mutmut | **NFR-08** |
| `test_assertion_quality` | ast-assertions | **NFR-09** |
| `integration_coverage` | pytest-cov-integration | NFR-10 |
| `readability` | readability-v2 | NFR-11 |
| `execute_verification_target` | system-verification | NFR-12 |
| `linting` / `type_safety` / `test_coverage` | ruff / pyright / pytest-cov | §8 驗收 #1 / #2 + 框架預設門檻 |
| `architecture` | code-review-graph | §6 五層分層(框架自算,見下方鐵律) |
| `secrets_scanning` | gitleaks | 框架預設門檻 100 |

**CRG 校準鐵律**:`.methodology/harness_config.json` 的 `crg_cohesion_healthy` **必須保持預設值**。前一輪測床把它從 0.3 調到 0.2,好讓單一扁平 package(21 nodes)通過 —— 那是調鬆框架遷就測床,方向相反。本專案以五層分層 + 約 2,300 行的規模,讓 `architecture` 維度自然產生區辨力。

**高風險模組**:`taskq_plus.service.executor`(subprocess 執行)、`taskq_plus.service.plugins`(動態載入)、`taskq_plus.storage.task_store`(並發寫入)。三者需 per-module TDD 覆蓋。

---

## 11. 監控門檻(Quality Gates 對齊)

| 指標 | 閾值 | 量測方式 |
|------|------|---------|
| `submit`+`status` p95(100 iter) | < 50ms | pytest-benchmark(NFR-01) |
| 拓撲排序 p95(200 tasks) | < 200ms | pytest-benchmark(NFR-01) |
| 測試 skip 數 | **0** | `pytest -q` 輸出(NFR-09) |
| 零斷言測試函式數 | **0** | ast-assertions(NFR-09) |
| 行覆蓋率 | 100% | pytest-cov(§8 #2) |
| 整合覆蓋率 | ≥ 80% | pytest-cov-integration(NFR-10) |
| mutation score | ≥ 70 | mutmut(NFR-08) |
| `lint-imports` 違規 | 0 | import-linter(NFR-06) |
| 非 allowlist license 的依賴數 | 0 | pip-licenses(NFR-07) |
| `shell=True` / `eval(` / `exec(` 命中 | 0 | grep CI gate(NFR-02) |
| bandit HIGH / MEDIUM | 0 / 0 | bandit(NFR-02) |
| secret 落盤命中 | 0 | 對 `audit.jsonl` 的 unit test(NFR-04) |
| docstring `[FR-XX]` / `[NFR-XX]` 覆蓋 | 100% | ast-docstrings(NFR-05) |
| 專案 MI | ≥ 80 | readability-v2(NFR-11) |
| `make verify-system` | exit 0 | Makefile(NFR-12) |

---

*文件版本:v1.0.0(8 FR / 12 NFR / 12 env)| 2026-07-30 | 漸進式驗證測床第 1 輪*
