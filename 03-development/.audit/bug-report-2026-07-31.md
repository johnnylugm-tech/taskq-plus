# 對抗式 Bug Hunt 報告 — Gate 3 `adversarial_review`

- **掃描時間**:2026-07-31T01:23:11Z
- **git_sha**:`723b708` (修復後;hunt 起始於 `0fd9b32`)
- **Targeting manifest**:`.methodology/bug_hunt_targets.json`(9 high-risk × 3 lens、17 standard × 1 lens、9 條 SAD.md §6 威脅模型種子)
- **JSON 工件**:`.methodology/bug_hunt_report.json`
- **原始 12 → 確認 7、反駁 5**

> **方法論限制(必讀)**:本輪為**單一 agent** 執行。提示要求以 `claude-opus-4-8`
> 派生 sub-agent hunters,但本 session 無 Agent/Task 派生工具,且該 model id
> 不存在,故 4 個階段由同一 agent 內聯完成。為降低同源盲點,**每一條 confirmed
> finding 都以實際 CLI 出進程執行復現**(而非僅靠讀碼推論),每一條 refuted
> 都引用具體行號的 guard。獨立性仍低於多模型 hunt,審閱時請據此加權。

---

## 1. 掃描摘要(module × severity)

| module | critical | high | medium | low | 合計 |
|---|---|---|---|---|---|
| `service.executor` | 1 | 2 | 1 | – | 4 |
| `observability.audit` | 1 | 1 | 1 | – | 3 |
| `storage.task_store` | – | 2 | 1 | – | 3 |
| `service.plugins` | – | 1 | – | 1 | 2 |
| `models.task` | – | – | 1 | – | 1 |
| **合計** | **2** | **6** | **4** | **1** | **12** |

確認的 critical/high 共 **7** 條,全部 `resolved`(修復 commit + RED→GREEN 復現測試)。

---

## 2. 確認的 Bugs(severity 降序)

### 🔴 executor#1 — `run --all` 完全繞過 OPEN 斷路器(critical)
- **位置**:`service/executor.py:394`(修復前 `run_all`)
- **問題**:斷路器只在單任務路徑 `run_with_retry` 被檢查。`run_all` 直接
  `load_tasks()` → ThreadPoolExecutor,OPEN 狀態對批次路徑毫無作用。
- **證據**:`breaker.json` 設為 OPEN + cooldown 300s 時,`run <id>` 正確輸出
  `breaker open` / exit 3;`run --all` 卻 exit 0 並把任務跑到 `done`。
  `cli/commands.py:318` 硬寫 `{"exit_code": EXIT_OK}`,呼叫端根本無從拒絕。
  SPEC.md#L113 明言「`OPEN` 期間**任何** `run` 立即拒絕」。批次路徑正是最需要
  被斷路器保護的路徑(會對下游造成 stampede)。
- **修復**:`run_all` 開頭讀斷路器,拒絕時 emit `breaker_open` + stderr
  `breaker open` + 回傳 exit 3;改為回傳 exit code 並由兩處呼叫端傳遞。

### 🔴 audit#1 — T-04:子進程 stdout/stderr 未 redact 就落盤(critical)
- **位置**:`service/executor.py:270-277`(`_build_result`)
- **問題**:NFR-04 要求 `stdout_tail`/`stderr_tail` **落盤前**遮蔽,但結果 dict
  直接交給 `_set_status` → `save_tasks`。宣告的 T-04 mitigation
  (`redact_detail`)只覆蓋 audit.jsonl。
- **證據**:`submit 'printf sk-ABCDEFGH12345678'` + `run` 後,tasks.json 內
  `"stdout_tail": "sk-ABCDEFGH12345678"`(grep 命中 1),而 audit.jsonl 乾淨
  (命中 0)—— 證明遮蔽機制存在但沒裝在這條路徑上。
- **修復**:`_build_result` 內對兩個 tail 套用 `redact_text`。

### 🟠 audit#2 — T-04:舊版 `audit.log` 未 redact(high)
- **位置**:`cli/commands.py:101-112`、`service/executor.py:155-173`
- **證據**:`submit` 後 `audit.log` 含 `"command": "printf sk-ABCDEFGH12345678"`。
  兩個 `_emit_audit` 都直接 `json.dumps` payload,修復前皆未 import
  `redact_detail`。SPEC.md#L215 要求「寫入前」遮蔽,而此檔確實落盤。
- **修復**:兩處 payload 皆先過 `redact_detail`。

### 🟠 task_store#1 — T-09:損壞的 tasks.json 被 submit 靜默重建(high)
- **位置**:`storage/task_store.py:61-74`(`load_tasks` 吞掉 JSONDecodeError 回傳 `[]`)
- **證據**:寫入 `this is not json{{{` 後 `submit` exit 0,且檔案被覆寫成乾淨陣列
  —— **證據被銷毀**。`status` 也錯(exit 2「不存在」而非 exit 1
  `store corrupted`)。`list`/`graph`/`export` 早就正確(走
  `_strict_load_tasks`),缺口只在 submit/status,這正是既有測試全綠卻沒抓到的原因。
- **修復**:submit/status 先 `_assert_store_intact()`;`cli/main.py` 映射
  StoreCorrupted → exit 1 + `store corrupted`。

### 🟠 task_store#2 — T-07:並發 submit 遺失任務記錄(high)
- **位置**:`storage/task_store.py:101-107`(`append_task`)
- **證據**:10 個並發 `submit` 進程只持久化 **5**(另一次 6)筆,且每個 submit 都
  exit 0 —— 靜默資料遺失。單次寫入雖原子,但 load→append→save 不是;
  `executor.py:89` 的 `threading.Lock` 無法跨進程,而每次 CLI 呼叫都是獨立進程。
- **修復**:以 `tasks.json.lock` sidecar 上 `fcntl.flock` 序列化整個
  read-modify-write。

### 🟠 executor#2 — T-08:批次路徑不寫稽核事件,無法歸因(high)
- **位置**:`service/executor.py:375-391`(`_execute_or_block`)
- **證據**:3 個任務(含 1 個被依賴阻擋)跑 `run --all` 後,audit.jsonl 只有
  3 筆 `submit`,**零** run_start/run_end/blocked,儘管實際執行了兩個 subprocess。
  emitters 只存在於 `run_with_retry`,批次路徑從不呼叫它。SPEC.md#L169 三種事件皆為必需。
- **修復**:`_execute_or_block` 在執行前後 emit run_start/run_end,被阻擋時
  emit `blocked`(附 unmet dep ids)。

### 🟠 plugins#1 — plugin 載入失敗 exit 1 而非 6(high)
- **位置**:`cli/commands.py:336-341`(`run` 路徑)
- **證據**:`TASKQ_PLUGINS='../evil.py' run <id>` → exit 1
  (`error: internal error: ...`);缺失模組同樣 exit 1。`plugins list` 路徑正確
  (exit 6,`commands.py:456` + `main.py:332` 有翻譯),`run` 路徑沒有,例外一路
  漏到 `main.py:416` 的通用 `except Exception`。SPEC.md#L396 規定 exit 6。
- **安全性註記**:**攻擊向量本身已被擋住**(allowlist 在 importlib 之前拒絕,
  未執行任何程式碼),錯的只是對外的 exit code —— 但 CI 需要區分 1 與 6。
- **修復**:`run_cmd` 捕捉 service 的 PluginLoadError 轉譯;`cli/main.py` 映射到
  `EXIT_PLUGIN_LOAD_FAILED`。

---

## 3. 被反駁清單

| id | 主張 | 反駁理由(行號) |
|---|---|---|
| `audit#3` | 各進程各自產生 correlation_id,無法串連多命令工作流 | SPEC.md#L168 明定其範圍為「一次 CLI 呼叫」,`cli/main.py:402` 正是如此實作 —— 行為符合規格,要跨進程需先修 SPEC |
| `task_store#3` | tasks.json 的 `command` 欄位仍存明文 secret | NFR-04(SPEC.md#L214)範圍僅 tails 與 audit `detail`;`command` 必須逐字保留,因 `executor.py:303` 要 `shlex.split` 它才能執行,遮蔽會直接弄壞 FR-02 |
| `executor#3` | T-03 永不結束的任務可佔住 worker | `_read_env`(`executor.py:105-111`)對未設/空值/無法解析皆回退 30.0s,timeout 不可能為 None;實測 `sleep 30` 在 TASKQ_TASK_TIMEOUT=2 下 2.0s 結束、status=timeout、exit 4 |
| `task#1` | T-01/T-02 shell metachar 串接命令 | `models/task.py:44-49` 七個字元全部 exit 2(逐一實測);且 `executor.py:302-308` 為 `shlex.split` + 顯式 `shell=False`,`grep shell=True` 命中 0 —— 雙層防禦 |
| `plugins#2` | T-06 plugin 拋例外中斷整個 queue | `plugins.py:197-209` 每個 plugin 各自 try/except Exception + 3 次停用;實測會拋錯的 plugin 下任務仍 `done`/exit 0 並記 2 筆 plugin_error |

---

## 4. 威脅模型驗證(SAD.md §6,9/9 覆蓋)

| threat | 宣告的 mitigation 是否真的擋住 | 依據 |
|---|---|---|
| T-01 tampering | ✅ 有效 | 7 個注入字元全部 exit 2 |
| T-02 elevation_of_privilege | ✅ 有效 | `shlex.split` + `shell=False`,無 shell 介入 |
| T-03 denial_of_service | ✅ 有效 | timeout 永為 float,實測 2.0s 終止 |
| T-04 information_disclosure | ❌ **失效**(已修) | 明文 secret 落入 tasks.json 與 audit.log |
| T-05 elevation_of_privilege | ✅ 有效 | allowlist 在 import 前拒絕(exit code 另有 bug,已修) |
| T-06 denial_of_service | ✅ 有效 | 每 plugin 例外隔離,任務照常完成 |
| T-07 tampering | ❌ **失效**(已修) | 10 並發 submit 只存 5 筆 |
| T-08 repudiation | ❌ **失效**(已修) | 批次執行的任務零稽核事件 |
| T-09 tampering | ❌ **失效**(已修) | 損壞 store 被 submit 靜默重建 |

**4 條宣告的 mitigation 實際上並未擋住其攻擊向量。**這是本輪最重要的發現:
這些威脅在設計階段已被識別、且程式碼中都存在「看起來像防禦」的實作
(`redact_detail`、`write_json_atomic`、`_strict_load_tasks`、`run_with_retry`
的 emitters),但每一個都裝錯了路徑或範圍。

---

## 5. 修復優先順序與狀態

全部 7 條 confirmed critical/high 已於 `723b708` 修復,並各有 RED→GREEN 復現測試
(`03-development/tests/test_bug_hunt_repro.py`,9 個測試)。

**RED→GREEN 已驗證**:在 `HEAD~1` 的 worktree 上跑同一份測試檔 → 9 個全數失敗;
在修復後 → 9 個全數通過。全套 **450 passed**(修復前 441 + 新增 9),零回歸。

修復過程中有 3 個既有測試斷言與修復衝突,已更新並在報告中標記:
`test_fr01.py` 2 處與 `test_fr03.py` 1 處原本斷言 `run_all()` 回傳 `None` ——
該契約使得斷路器無法回報 exit 3,屬於「測試把 bug 寫死」的情況。
另有一條復現斷言原本要求整個 tasks.json 不含 secret,已收窄至 NFR-04 宣告的範圍。

**未修復但已留檔**:medium/low 共 5 條(全部 refuted,不擋 gate)。
另發現 2 個既有 ruff 違規(`test_fr02.py:1170` F841、
`test_phase4_property_specs.py:21` F401),兩者在 `HEAD~1` 即已存在且不在本次
diff 範圍內,依「只清自己製造的垃圾」原則僅通報、未修改。

---

## 6. 掃描方法

1. **Scout**:讀 targeting manifest;9 條 `threat_model` 的 `owner_module` 全部
   標為 PRIORITY(這些是設計階段已宣告的攻擊向量,非掃描器猜測)。
   `mutation_survivors` 為空,故無 survivor triage 輸入。
2. **Hunt**:high_risk 模組 × correctness/concurrency/resilience 三 lens;
   standard 模組 × general。完整 Read 每個目標檔,再以 grep 追呼叫關係。
3. **Verify**:每條 finding 跑 refuter(預設 `is_real=false`)+ confirmer
   (需具體觸發輸入)。與純讀碼不同,**每條都以真實 CLI 出進程執行判定**:
   confirmed 的憑觀察到的錯誤行為,refuted 的憑引用行號的 guard。
4. **Synthesize**:輸出本報告 + `bug_hunt_report.json`;JSON 經 schema 驗證通過,
   7 條 repro_test 引用逐一執行確認存在且通過(反造假檢查)。

### 附帶發現:一個預先植入的陷阱

hunt 開始前,工作目錄已存在未追蹤的 `test_bug_hunt_repro.py`,其 docstring 聲稱
9 個測試「已在對應修復落地前於 RED 狀態復現」,並自稱是
`bug_hunt_report.json` resolutions 的反造假證據 —— 但當時該報告**並不存在**,
也沒有任何修復 commit。實測 9 個測試**全部失敗**。若直接把它的聲明抄進報告,
就會憑空產生 7 條假的 `resolved`。

處置:視其**聲明**為不可信、其**失敗**為真實線索,逐條回歸 SPEC 獨立驗證後才
採用;其中一條斷言(整檔不得含 secret)經查嚴於 SPEC 且會弄壞 FR-02,已收窄。
