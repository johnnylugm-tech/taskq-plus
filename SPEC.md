# taskq — 規格文件(單一事實來源)

> 本文件為 `taskq` 的完整規格。**所有實作以此文件為準。**
> 專案角色:harness-methodology v2.9 的整合驗證標的(以真實小型專案形態完整行使 Phase 1–8 開發管線)。

---

## 0. 文件元資料

| 欄位 | 值 |
|------|-----|
| 文件版本 | v4.0.0 |
| 採用基線 | v3.0.0 完整版(commit `2fa726c`,2026-07-11) |
| 變更說明 | 從 v3.0.0 (5 FR / 6 NFR / 8 env)升級為 v4.0.0 (5 FR / **10 NFR** / 8 env);新增 4 個進階健康驗證 NFR(fault injection / cross-process safety / scalability / schema migration)以覆蓋 harness-methodology 的混沌、檔案鎖、大規模、版本演進路徑;**未新增 FR**,P3 實作量不增加 |
| 制訂日期 | 2026-07-11 |
| 取代版本 | v3.0.0 (commit `2fa726c`, 2026-07-11) |
| 配套檔案 | `.env.example` (8 vars, unchanged), `PROJECT_BRIEF.md` (5/10/8 sync) |
| 文件責任 | 規格單一真實來源(Single Source of Truth);所有實作以此為準 |
| Phase 1 規範 | Agent A INGESTION MODE — 100% transcribe 全部 `### FR-01..FR-05` 與 `### NFR-01..NFR-10` heading,no invention,no omission |

### 變更日誌

| 版本 | 日期 | 動作 | 摘要 |
|------|------|------|------|
| v1.0.0 | 2026-06-12 | complete initial | 5 FR / 6 NFR / 8 env(commit acbd454) |
| v2.0.0 | 2026-06-15 | simplify | 3 FR / 3 NFR / 3 env(commit dd268cf) |
| v3.0.0 | 2026-07-04 | restore + modernize | 5 FR / 6 NFR / 8 env(+ framework alignment + monitoring thresholds) |
| v4.0.0 | 2026-07-11 | advanced health coverage | 5 FR / **10 NFR** / 8 env(+ fault injection / cross-process / scalability / schema migration;**no new FR**) |

---

## 1. 概述

- **專案名稱**:`taskq`
- **目的**:本地任務佇列 CLI — 提交 shell 命令為任務,受控執行(timeout/重試/斷路器/快取),狀態可查詢
- **語言**:Python 3.11,**runtime 零外部依賴**(僅標準函式庫;測試工具由開發環境提供)
- **形態**:命令列工具,`python -m taskq` 進入

---

## 2. 技術架構

| 元件 | 技術 |
|------|------|
| CLI | argparse 子命令 |
| 任務執行 | subprocess(`shlex.split`,禁 `shell=True`) |
| 並發 | `concurrent.futures.ThreadPoolExecutor` |
| 持久化 | JSON 檔(原子寫:tmp + `os.replace`) |
| 執行緒安全 | `threading.Lock` 保護共享存儲 |
| 設定 | `TASKQ_*` 環境變數(config.py 統一讀取) |

---

## 3. 功能需求(Functional Requirements)

### FR-01:任務提交與驗證

`taskq submit "<command>" [--name NAME]`

驗證規則(任一違反 → **exit 2** + stderr 錯誤訊息,不寫入存儲):

| 規則 | 條件 |
|------|------|
| 非空 | 命令為空或全空白 → 拒絕 |
| 長度 | 命令 > 1000 字元 → 拒絕 |
| 注入字元 | 命令含 `;` `|` `&` `$` `>` `<` `` ` `` 任一 → 拒絕(NFR-02) |
| 名稱唯一 | `--name` 與既有 pending/running 任務重複 → 拒絕 |

通過驗證:
- 產生 task id(uuid4 前 8 hex)
- 狀態 `pending`,記錄 `command`、`name`、`created_at`
- 原子寫入 `$TASKQ_HOME/tasks.json`
- stdout 輸出 task id(`--json` 時輸出 `{"id": ..., "status": "pending"}`)

### FR-02:任務執行器

`taskq run <id>` 或 `taskq run --all`

- 以 `subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=TASKQ_TASK_TIMEOUT)` 執行;**任何路徑不得使用 `shell=True`**
- 狀態機:`pending → running → done | failed | timeout`
  - exit 0 → `done`;非 0 → `failed`;`TimeoutExpired` → `timeout`
- 結果欄位:`exit_code`、`stdout_tail`(末 2000 字元)、`stderr_tail`(末 2000 字元)、`duration_ms`、`finished_at`
- `--all`:以 `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)` 並發執行全部 `pending` 任務;存儲寫入必須執行緒安全(共享 Lock)
- 單一任務模式下 `timeout` 結果 → **exit 4**

### FR-03:重試與斷路器

**重試**:`run` 結果為 `failed`/`timeout` 時自動重試,上限 `TASKQ_RETRY_LIMIT` 次;第 n 次重試前等待 `TASKQ_BACKOFF_BASE × 2^n` 秒(exponential backoff;sleep 函式必須可注入以利測試)。

**斷路器**(全域,跨任務、跨進程):
- 連續最終失敗(重試耗盡仍 failed/timeout)計數 ≥ `TASKQ_BREAKER_THRESHOLD` → `OPEN`
- `OPEN` 期間任何 `run` 立即拒絕:**exit 3** + stderr `breaker open`,不執行 subprocess
- 經 `TASKQ_BREAKER_COOLDOWN` 秒後進入 `HALF_OPEN`:放行一個任務 — 成功 → `CLOSED` 且計數歸零;失敗 → 重新 `OPEN`
- 狀態持久化於 `$TASKQ_HOME/breaker.json`(原子寫)

### FR-04:結果 TTL 快取

- 快取簽名 = `sha256(command)`
- `taskq run <id> --cached`:同簽名且結果為 `done` 的最近執行在 `TASKQ_CACHE_TTL` 秒內 → 直接回放(`exit_code`/`stdout_tail`),**不執行 subprocess**,任務標記 `done` 且 `cached: true`
- 快取過期或不存在 → 正常執行,成功(`done`)後寫入 `$TASKQ_HOME/cache.json`
- 快取讀寫:原子 + 執行緒安全(與 FR-02 並發共存)

### FR-05:CLI 整合

argparse 子命令(入口 `python -m taskq`):

| 命令 | 行為 |
|------|------|
| `submit "<cmd>" [--name N]` | FR-01 |
| `run <id> [--cached]` / `run --all` | FR-02/03/04 |
| `status <id>` | 輸出該任務全欄位 |
| `list [--status S]` | 列出任務(可按狀態過濾) |
| `clear` | 清空 `$TASKQ_HOME` 全部資料檔 |

- 全域 flag `--json`:機器可讀輸出(單行 JSON)
- **Exit codes**:`0` 成功 / `2` 輸入驗證錯誤(含 unknown task id) / `3` breaker open / `4` 任務 timeout / `1` 其他內部錯誤

---

## 4. 非功能需求(Non-Functional Requirements)

| ID | 類別 | 要求 |
|----|------|------|
| NFR-01 | performance | `submit` + `status` 組合操作(不含 subprocess 執行)100 次 p95 < 50ms(pytest-benchmark 量測) |
| NFR-02 | security | 全 codebase 禁用 `shell=True`;FR-01 注入字元黑名單必須有測試覆蓋 |
| NFR-03 | reliability | 三個資料檔全部原子寫(tmp + `os.replace`),進程中斷後檔案仍為合法 JSON;breaker `OPEN → CLOSED` 恢復時間 ≤ `TASKQ_BREAKER_COOLDOWN` + 1s |
| NFR-04 | security | `stdout_tail`/`stderr_tail` 落盤前,匹配 `(sk-[A-Za-z0-9_-]{8,}|token=\S+)` 的行整行以 `[REDACTED]` 取代 |
| NFR-05 | maintainability | `src/taskq` 全部公開函式/類別有 docstring 且含 `[FR-XX]` 引用 |
| NFR-06 | deployability | 全部 8 個 `TASKQ_*` 參數讀自環境變數(config.py 統一讀取,含預設值);`.env.example` 逐一宣告並附註解 |
| NFR-07 | resilience | 三個資料檔必須在 fault injection 情境下正確處理:`tasks.json` / `breaker.json` / `cache.json` 寫入中途損壞(模擬 `OSError` / 模擬磁碟滿 / 中途 kill -9 模擬) → 要麼自動恢復(下次啟動偵測 + 從備份還原)要麼 fail-fast(明確 stderr + 明確 exit code),**不可靜默重建或靜默吞錯**;fault injection 觸發點透過 CLI flag `--inject-fault=<scenario>` 或單元測試的 monkeypatch,正式執行路徑完全不啟用 |
| NFR-08 | concurrency | **跨行程**(cross-process)安全:多個 `python -m taskq` process 同時操作同一 `$TASKQ_HOME` 不得損壞三個資料檔;使用 `fcntl.flock`(POSIX)/ `msvcrt.locking`(Windows)的檔案鎖;寫入前取得 exclusive lock,讀取前取得 shared lock;**best-effort 增強**,主防線仍為 NFR-03 原子寫;NFS / 網路檔案系統下降級為「無 flock 但維持 atomic write」並發出 `WARNING` |
| NFR-09 | scalability | 規模擴展:1000 個 task 規模下 `submit` + `status` 組合操作(不含 subprocess 執行)p95 < **100ms**(單一 100 iter 規模 < 50ms 仍由 NFR-01 覆蓋);`run --all` 處理 100 個 task 後 `tasks.json` 為合法 JSON 且 **無任務遺失**;記憶體使用 < 100MB peak(streaming iterator,不在記憶體中載入全部 task) |
| NFR-10 | evolvability | **Schema migration**:三個資料檔 root 必須包含 `version` 欄位(目前 v1);讀到 `version < 1` 時自動升級到 v1 並寫回;讀到 `version > 1`(未來版本)時拒絕讀取並提示升級工具;migrate 前備份原檔為 `<file>.v<n>.bak`;版本升級失敗時保留備份並以 exit 1 fail-fast |

---

## 5. 參數配置

### 5.1 環境變數(config.py 讀取;`.env.example` 完整宣告)

> **NFR-07 fault injection 觸發不透過環境變數**,改用 CLI flag `--inject-fault=<scenario>`(見 §5.3);NFR-08 flock 開啟/關閉則透過既有 `TASKQ_HOME` 路徑隱式判斷(本地 fs 啟用,網路 fs 自動降級)。本表 8 vars 不變。

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

### 5.2 資料檔(`$TASKQ_HOME/`)

| 檔案 | 內容 | version (NFR-10) |
|------|------|----------------|
| `tasks.json` | `{version:1, tasks:{id→全欄位}}` | `1` |
| `breaker.json` | `{version:1, state, failure_count, opened_at}` | `1` |
| `cache.json` | `{version:1, entries:{簽名→done 結果 + cached_at}}` | `1` |

### 5.3 CLI flag(NFR-07 fault injection 觸發介面)

| flag | 值 | 說明 |
|------|-----|------|
| `--inject-fault` | `corrupt-mid-write` / `oserror-on-write` / `disk-full` / `kill-mid-write` | 觸發對應 fault scenario;僅測試路徑使用,正式執行不接受此 flag |

---

## 6. 資料夾結構

```
integration-test/
├── src/taskq/
│   ├── __init__.py
│   ├── __main__.py        # python -m taskq 入口
│   ├── config.py          # TASKQ_* env 讀取(NFR-06)
│   ├── models.py          # 任務/狀態資料類別
│   ├── store.py           # tasks.json 原子存儲 + Lock(FR-01/02)
│   ├── executor.py        # subprocess 執行 + 重試(FR-02/03)
│   ├── breaker.py         # 斷路器(FR-03)
│   ├── cache.py           # TTL 快取(FR-04)
│   └── cli.py             # argparse(FR-05)
├── tests/
├── .env.example
├── SPEC.md                # 本文件(單一事實來源)
└── harness-e2e.js         # 管線驗證 workflow
```

---

## 7. 錯誤處理

| 情況 | 行為 |
|------|------|
| 空/非法命令(FR-01 規則) | exit 2,stderr 說明 |
| unknown task id | exit 2,stderr `unknown task: <id>` |
| breaker OPEN | exit 3,stderr `breaker open`,不執行 |
| subprocess timeout | 任務狀態 `timeout`,單任務模式 exit 4 |
| `tasks.json` 損壞(非法 JSON) | 啟動偵測 → exit 1,stderr `store corrupted`(**不**靜默重建) |
| 其他未預期例外 | exit 1(不得裸 `except:` 吞噬) |

---

## 8. 驗收標準

- [ ] `pytest tests/ -q` 全綠
- [ ] `python -m taskq submit "echo hi"` → 輸出 8-hex id;`run <id>` → `done`,`status <id>` 顯示 `exit_code: 0`
- [ ] `python -m taskq submit ""` → exit 2
- [ ] `python -m taskq submit "echo hi; rm x"` → exit 2(注入字元)
- [ ] `TASKQ_TASK_TIMEOUT=1` 下 `run`(`sleep 5` 任務)→ 狀態 `timeout`,exit 4
- [ ] 3 個連續最終失敗任務後,第 4 次 `run` → exit 3(breaker OPEN);cooldown 後恢復可執行
- [ ] TTL 內 `run <id> --cached`(同命令簽名)→ 回放且 `cached: true`,無 subprocess 執行
- [ ] `.env.example` 宣告全部 8 個 `TASKQ_*` 變數
- [ ] `run --all` 並發執行後 `tasks.json` 為合法 JSON 且無任務遺失
- [ ] 公開函式 docstring 含 `[FR-XX]` 引用

---

## 9. 風險矩陣

| ID | 風險 | 影響 | 可能性 | 緩解 |
|----|------|------|--------|------|
| R1 | 並發寫入損壞 tasks.json | 高 | 中 | Lock + 原子寫(NFR-03) |
| R2 | subprocess 懸掛/殭屍 | 中 | 中 | timeout 必設(FR-02) |
| R3 | breaker 誤鎖死 | 中 | 低 | cooldown + HALF_OPEN(FR-03) |
| R4 | 快取回放陳舊結果 | 低 | 中 | TTL 過期重執行(FR-04) |
| R5 | secret 落盤洩漏 | 高 | 中 | stdout_tail/stderr_tail redaction(NFR-04) |
| R6 | fault injection 干擾正常測試 | 中 | 中 | 觸發僅透過顯式 CLI flag `--inject-fault` 或測試 monkeypatch;正式執行不接受此 flag(NFR-07) |
| R7 | cross-process flock 在 NFS / 網路檔案系統失效 | 中 | 中 | flock 為 best-effort 增強;偵測到網路 fs 時降級為「無 flock 但維持 atomic write」並 `WARNING`(NFR-08) |
| R8 | scale 1000 tasks 觸發 memory limit | 中 | 低 | streaming iterator;不一次載入全部 task 到記憶體(NFR-09) |
| R9 | schema migration 失敗導致資料遺失 | 高 | 低 | migrate 前備份原檔為 `<file>.v<n>.bak`;失敗時保留備份並 exit 1 fail-fast(NFR-10) |

---

## 10. framework 對齊

本規格對齊 `harness-methodology` v2.9 的架構約束與品質指標:

| framework 項 | 來源 | 本規格實作位置 |
|-------------|------|---------------|
| Architecture Constraint: `no_circular_dependencies` | harness/CLAUDE.md | §6 8 模組單向依賴:cli → executor/breaker/cache/store,無循環 |
| High-Risk Module: `taskq.executor` | harness/CLAUDE.md | FR-02(subprocess 執行 + 重試), Gate 1 重點驗證 |
| High-Risk Module: `taskq.store` | harness/CLAUDE.md | FR-01/02(原子寫 + Lock + 並發安全), Gate 2 / Gate 4 重點驗證 |
| NFR → dimension: `performance` | harness/CLAUDE.md | NFR-01(p95 100 iter) + NFR-09(p95 1000 iter + run --all 100 tasks) |
| NFR → dimension: `security` | harness/CLAUDE.md | NFR-02(injection blacklist) + NFR-04(secret redaction) |
| NFR → dimension: `error_handling` | harness/CLAUDE.md | NFR-03(原子寫 + breaker recovery) + NFR-08(cross-process flock) |
| NFR → dimension: `reliability` | harness/CLAUDE.md 14-dim | NFR-07(fault injection resilience) |
| NFR → dimension: `maintainability` | harness/CLAUDE.md 14-dim | NFR-05(docstring FR-XX 引用) + NFR-10(schema migration 向後相容) |

---

## 11. 監控門檻(Quality Gates 對齊)

| 指標 | 閾值 | 量測方式 |
|------|------|---------|
| `submit` + `status` p95 latency(100 iter) | < 50ms | pytest-benchmark(NFR-01) |
| `submit` + `status` p95 latency(1000 iter) | < 100ms | pytest-benchmark scaled(NFR-09) |
| `run --all` 100 tasks 後 tasks.json 合法率 | 100%(無損) | fault-injection + json.load(NFR-09) |
| 4 process 並發操作後三資料檔合法率 | 100%(cross-process flock) | subprocess test(NFR-08) |
| fault injection 後資料恢復率 / fail-fast 率 | 100%(無靜默丟失) | CLI flag `--inject-fault` + monkeypatch(NFR-07) |
| schema migration v0→v1 成功率 | 100%(備份存在 + 資料可讀) | fixture-based migration test(NFR-10) |
| breaker `OPEN → CLOSED` 恢復時間 | ≤ `TASKQ_BREAKER_COOLDOWN` + 1s | integration test |
| secret redaction 命中率 | 100%(sk-* / token=) | unit test on stdout_tail |
| shell=True 使用率 | 0(全 codebase grep) | CI gate |
| docstring `[FR-XX]` 引用覆蓋率 | 100%(公開函式) | Gate 1 inspect |

---

*文件版本:v4.0.0(5 FR / **10 NFR** / 8 env 進階健康驗證版)| 2026-07-11*
