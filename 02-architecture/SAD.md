# Software Architecture Document (SAD) — taskq-plus

> 漸進式驗證測床第 1 輪 — Python CLI 補洞版。本文件以 `SPEC.md` 為唯一事實來源,模組結構沿用 SPEC §6 規定的五層分層,並依 NFR-06 由 `.importlinter` 強制約束。

---

## 1. Overview

`taskq-plus` 是一個本地任務佇列 CLI,以受控方式(`timeout` / 重試 / 斷路器 / TTL 快取 / DAG 相依)執行使用者提交的 shell 命令,並提供 plugin hook、結構化稽核日誌、任務結果匯出。本文件回答:

1. 8 個 FR 與 12 個 NFR 如何映射到具體模組。
2. 五層分層(`cli > observability > service > storage > models`)之間的呼叫方向、介面、資料流。
3. 哪些介面是高風險介面(會被 mutation、靜態掃描、整合測試特別盯)。

### 1.1 系統驗證目標

> Phase 3 Gate 2 需求:make target 名稱為 `verify-system`(NFR-12)。`make verify-system` 串接:全套 pytest + CLI 冒煙,exit 0 並印出 `verify-system: PASS`。

**Makefile target**: `verify-system`

### 1.2 文件地圖

| 章節 | 內容 |
|------|------|
| §2 | 模組設計 — 五層目錄 × FR→模組矩陣 |
| §3 | 介面與資料流(call graph + 持久化 + DAG) |
| §4 | NFR 處理(每條 NFR 一段:目標/落實點/驗證) |
| §5 | SAB 區塊(machine-readable,`SAB:START` / `SAB:END` HTML 註解標記之間) |
| §6 | Security Design(STRIDE-lite,`SEC:START` / `SEC:END` HTML 註解標記之間) |

> 標記字串在此刻意以純文字書寫、不加 `<!-- -->`:`sab_parser._SAB_BLOCK_RE` / `security_design` 的抽取正則是「第一個 START 註解到第一個 END 註解」,文件前段若出現真正的註解標記,會讓抽取範圍從這裡開始而吞掉 §2–§4。全檔僅 §5 / §6 各有一組真正的標記。

---

## 2. Module Design

### 2.1 分層原則(對齊 SPEC §6 + NFR-06)

```
L5 cli               (entry; click group + 子命令)
   |
L4 observability     (audit + export;讀取任務快照)
   |
L3 service           (executor / breaker / cache / dag / plugins — 業務邏輯)
   |
L2 storage           (四個資料檔的原子讀寫)
   |
L1 models            (pydantic 模型 + 領域例外 — 零內部依賴)
   |
models — 任何層

config              independence:任何層可 import,但 config 不 import 任何層
```

**`.importlinter` contract**(NFR-06,§6):

```
cli > observability > service > storage > models
```

- 上層可 import 下層;下層**不得** import 上層(單向)。
- `config` 為獨立模組,任何層可 import,但**不得** import 任何層。
- `tests/` 不在 layers 契約內,但 `import-linter` 可加 `forbidden_dependencies` 限定測試引用邊界。

**無循環依賴(no_circular_dependencies)**:層序是全序 `models < storage < service < observability < cli`,`config` 為 sink(不 import 任何層),import 只允許由高層指向低層 → 依賴圖是 DAG,**任何兩模組間不可能構成環**。目錄內同層互呼(§2.2 hub 表)亦為單向:`atomic ← 三個 store`、`breaker/cache/dag/plugins ← executor`、`errors ← task`、`audit ← export`、`commands ← main`,無回邊。`lint-imports` 對此為機器判定。

**Output Port 規則(NFR-06 的直接後果,BINDING)**:

`observability`(L4)在 `service`(L3)**之上**,因此 `service.*` 一律**不得** `import taskq_plus.observability.*`。稽核與遮蔽這兩件事在 service 層以**注入的可呼叫物**(plain `collections.abc.Callable`,結構型別,不需要任何 import)完成:

| Port | 型別 | 由誰注入 | 實作 |
|------|------|----------|------|
| `emit` | `Callable[[str, str \| None, dict], None]` | `cli.commands`(L5) | `observability.audit.emit` |
| `redact` | `Callable[[str], str]` | `cli.commands`(L5) | `observability.audit.redact` |

- `service.executor.run_one(task_id, *, emit, redact, sleep=time.sleep)` — 四個 port 全部 default 為 no-op / identity / `time.sleep`,單元測試可用 fake 取代。
- 這是唯一允許 service 產生稽核事件的路徑;任何 `from taskq_plus.observability import ...` 出現在 `service/` 或 `storage/` 即為 NFR-06 違規。

### 2.2 目錄結構(逐檔理由)

> 每個目錄 ≤ 15 檔(NFR-11)。所有原始碼合計約 2,300 行,符合 SPEC §10 「五層分層 + 約 2,300 行」規模聲明,CRG 將自然得到多個 community。

```
03-development/
└── src/taskq_plus/
    ├── __init__.py            # package marker;暴露 `__version__`
    ├── __main__.py            # `python -m taskq_plus` 入口(轉呼 cli.main:main)
    ├── config.py              # TASKQ_* env 讀取(independence 層,NFR-06)
    │
    ├── models/                # L1 — 零內部依賴;只依賴 pydantic + stdlib
    │   ├── __init__.py
    │   ├── task.py            # TaskSubmission / Task / TaskStatus / TaskResult(pydantic v2)
    │   └── errors.py          # 領域例外:ValidationError / UnknownTaskError / CycleDetectedError / BreakerOpenError / PluginLoadError / PluginExecutionError / DagDepthExceededError
    │
    ├── storage/               # L2 — 依賴 models + config
    │   ├── __init__.py
    │   ├── atomic.py          # tmp + os.replace 原子寫;append+fsync(NFR-03)
    │   ├── task_store.py      # tasks.json;threading.Lock 保護並發寫(FR-01/02)
    │   ├── breaker_store.py   # breaker.json 狀態持久化(FR-03)
    │   └── cache_store.py     # cache.json 簽名索引(FR-04)
    │
    ├── service/               # L3 — 依賴 storage + models + config
    │   ├── __init__.py
    │   ├── executor.py        # subprocess.run + 重試 + 狀態機(FR-02/03) [HIGH RISK]
    │   ├── breaker.py         # CLOSED/OPEN/HALF_OPEN 狀態(FR-03)
    │   ├── cache.py           # TTL + sha256(command) 簽名(FR-04)
    │   ├── dag.py             # Kahn 拓撲排序 + 循環 + 深度上限(FR-06)
    │   └── plugins.py         # allowlist 載入 + pre_run/post_run hook(FR-07) [HIGH RISK]
    │
    ├── observability/         # L4 — 依賴 service + storage + models + config
    │   ├── __init__.py
    │   ├── audit.py           # JSONL 稽核 + 寫入前 redaction(FR-08 / NFR-04)
    │   └── export.py          # json / csv / md 三格式(FR-08)
    │
    └── cli/                   # L5 — 依賴所有下層 + click
        ├── __init__.py
        ├── main.py            # click group + 全域 --json + exit code 對映(FR-05)
        └── commands.py        # submit / run / status / list / graph / plugins / export / clear
```

**檔案/目錄規模表**(每行 ≤ NFR-11 的 400/15 限制):

| 目錄 | 檔案數 | 預估總行數 |
|------|--------|------------|
| `taskq_plus/`(root) | 3 | ~150 |
| `models/` | 3 | ~250 |
| `storage/` | 5 | ~500 |
| `service/` | 6 | ~800 |
| `observability/` | 3 | ~250 |
| `cli/` | 3 | ~350 |
| **合計** | **23** | **~2,300** |

> 沒有 god-module:`executor.py` 預估 ≤ 350 行;`commands.py` 因八個 click command 聚合為 ~250 行;`task_store.py` ≤ 250 行。

**CRG hub-module 規約**(對齊 `harness/templates/SAD.md` §2.1 Principles 2 + 4 — 每個 ≥ 2 檔的目錄需有一個被同層函式**在函式體內呼叫**的 hub,使 community cohesion ≥ 0.3):

| 目錄 | hub module | 被誰在函式體內呼叫 |
|------|------------|--------------------|
| `models/` | `errors.py` | `task.py` 的驗證失敗路徑 raise `models.errors.ValidationError` |
| `storage/` | `atomic.py` | `task_store` / `breaker_store` / `cache_store` 三者的每次寫入都呼叫 `atomic.write_json` |
| `service/` | `breaker.py` | `executor.run_one` 呼叫 `breaker.assert_closed` / `record_failure` / `record_success`;`dag` / `cache` / `plugins` 均由 `executor` 呼叫 → `executor` 同時是 service 的 fan-out 中心 |
| `observability/` | `audit.py` | `export.py` 匯出時呼叫 `audit.redact` 對輸出欄位再遮蔽一次 |
| `cli/` | `main.py` | `commands.py` 的每個子命令由 `main` 的 click group 註冊並呼叫 |

> 這是設計時的硬約束,不是「實作時再說」:若實作階段某目錄退化成彼此不呼叫的平行檔案,`architecture` 維度分數會掉,屬 NFR-06 之外的獨立風險。

### 2.3 FR → 模組映射矩陣

> 每一個 FR 必須至少有 1 個 owner module;有必要的標 **[HIGH RISK]** 對應 SPEC §10 高風險模組清單(executor / plugins / task_store)。

| FR | 標題 | Primary module(s) | 涉及之 NFR |
|----|------|-------------------|------------|
| FR-01 | 任務提交與驗證 | `cli.commands.submit` → `models.task.TaskSubmission` → `storage.task_store.TaskStore.put` | NFR-02(注入黑名單),NFR-03(原子寫) |
| FR-02 | 任務執行器 | `cli.commands.run` → `service.executor.run_one` / `run_all` → `storage.task_store`(**Lock**) | NFR-01(p95),NFR-03(並發安全),NFR-12 |
| FR-03 | 重試與斷路器 | `service.executor`(重試退避)+ `service.breaker` + `storage.breaker_store` | NFR-03(原子持久化,cooldown 時序) |
| FR-04 | 結果 TTL 快取 | `cli.commands.run --cached` → `service.cache.get_or_compute` → `storage.cache_store` | NFR-03(原子寫) |
| FR-05 | CLI 整合 | `cli.main`(click group + exit code 表)+ `cli.commands` | NFR-05(docstring 覆蓋),NFR-11(可讀性) |
| FR-06 | 任務相依 DAG | `cli.commands.submit --after` / `run --all` / `graph` → `service.dag`(拓撲 + 循環 + 深度) | NFR-01(200 task 拓撲 p95 < 200ms) |
| FR-07 | Plugin Hook 系統 | `cli.commands.plugins` / `run` → `service.plugins.load_allowlisted` + `dispatch_hook` | **NFR-02(allowlist 正則 + 禁 eval/exec/路徑)** |
| FR-08 | 結構化稽核日誌與匯出 | `observability.audit.emit`(全 CLI 命令裝飾)+ `observability.export.json/csv/md` | NFR-04(寫入前 redaction),NFR-10 |

### 2.4 模組詳述(僅列 owner module 與 FR 之外有顯著契約者)

| Attribute | `service.executor` |
|-----------|-------------------|
| Responsibility | subprocess 執行 + 狀態機 + 重試退避 |
| External Interface | `run_one(task_id, *, emit, redact, sleep=time.sleep) -> TaskResult`;`run_all(*, emit, redact) -> list[TaskResult]` |
| Dependencies | `storage.task_store`, `service.breaker`, `service.cache`, `service.plugins`, `service.dag`, `models.task`, `config`(**不得** import `observability` — 稽核/遮蔽走 §2.1 的注入 port) |
| 高風險 | YES — `subprocess.run` + `shlex.split` 是 NFR-02 的核心防線;`sleep` 必須可注入以利 NFR-01 benchmark |
| 契約(FR-06,BINDING) | 相依任務結果非 `done` → 下游標記 `blocked`、**不執行 subprocess**、**不呼叫 `breaker.record_failure`**。`blocked` 不是失敗,不得計入斷路器計數(SPEC §3 FR-06 逐字條款)。 |
| 契約(FR-03,BINDING) | `run_all` 進入任何一層之前先 `breaker.assert_closed()`;OPEN → 在 spawn 任何 subprocess **之前**即 exit 3。 |

| Attribute | `service.plugins` |
|-----------|-------------------|
| Responsibility | allowlist 載入 + 模組名正則白名單 + hook 派發 + 例外隔離 |
| External Interface | `load_allowlisted(spec: str) -> list[Plugin]`;`dispatch(hook: str, payload, *, emit)` |
| Dependencies | `models.errors.PluginLoadError`, `config`(**不得** import `observability`;`plugin_error` 事件經注入的 `emit` port 送出) |
| 高風險 | YES — SPEC §9 R6 「plugin 成為任意程式碼執行入口」,NFR-02 明文列出 |

| Attribute | `storage.task_store` |
|-----------|-------------------|
| Responsibility | tasks.json 的 thread-safe 讀寫 + 依賴查詢 + DAG 邊維護 |
| External Interface | `put(task)`, `get(task_id)`, `list(*, status)`, `mark_running/done/...`, `topological_levels() -> list[list[task_id]]` |
| Dependencies | `storage.atomic`, `models.task`, `config` |
| 高風險 | YES — SPEC §9 R1 「並發寫入損壞 tasks.json」;並發 run --all 期間 Lock 是唯一保證 |

| Attribute | `service.dag` |
|-----------|-------------------|
| Responsibility | Kahn 拓撲排序、循環偵測、深度上限檢查 |
| External Interface | `validate_and_toposort(tasks, max_depth) -> list[list[task_id]]`;`detect_cycle(tasks) -> list[str] \| None` |
| Dependencies | `models.task`, `models.errors.CycleDetectedError / DagDepthExceededError` |
| 高風險 | NO,但被 SPEC §10 點名需 benchmark 覆蓋(NFR-01 第二條) |

| Attribute | `observability.audit` |
|-----------|-------------------|
| Responsibility | JSONL append + 寫入前 redaction(行級 regex 取代為 `[REDACTED]`) |
| External Interface | `emit(event, task_id, *, detail)`, `redact(text) -> str`, `correlation_scope()` context manager |
| Dependencies | `storage.atomic`(append+fsync), `config`, `models.task` |
| 備註 | `emit` / `redact` 由 `cli.commands` 注入 service 層(§2.1 Output Port),故 audit 本身不需要知道 service 的存在。 |

---

## 3. Interfaces & Data Flows

### 3.1 控制流總覽(call graph,自上而下)

```
[operator shell]
     |
     v
cli.main (click group)
     |
     v
cli.commands.{submit|run|status|list|graph|plugins|export|clear}
     |
     +---> models.task.* (validation, pydantic) ──────────────┐
     |                                                        |
     +---> observability.audit.emit / .redact ◄───────────────┤
     |          ▲ (L5 取得 bound callable,作為 port 注入 L3)  |
     |          |                                             |
     +---> service.executor(emit=…, redact=…) ──> storage.task_store (Lock)
     |        |                       |                       |
     |        |                       +---> service.breaker ──> storage.breaker_store
     |        |                       +---> service.cache   ──> storage.cache_store
     |        |                       +---> service.plugins (allowlist, hooks)
     |        +---> service.dag (topological_levels)
     |
     +---> observability.export.{json|csv|md}
     |
     +---> config.TASKQ_* (env reads, never imports upward)
```

> 箭頭方向即 import 方向,唯一例外是 `emit` / `redact`:那是**值**(callable)由 L5 傳進 L3,不是 import。因此圖中沒有任何一條由 `service` / `storage` 指向 `observability` 的邊 —— `lint-imports` 契約(NFR-06)成立。

### 3.2 一次 `submit "<cmd>" --after <id>` 的資料流

```
1. operator → cli.commands.submit
2. models.task.TaskSubmission 驗證(注入字元黑名單、名稱唯一、--after 存在)
3. service.dag.validate_and_toposort(既有圖 + 本次新邊)  ← **submit 時的 DAG 閘門(FR-06)**
   - 新邊造成循環 → stderr 列出循環路徑 + **exit 5**(AC-FR-06.3),不寫入 task store
   - 加入後深度 > TASKQ_MAX_DAG_DEPTH → **exit 5**(AC-FR-06.4),不寫入 task store
4. storage.task_store.put (Lock acquire → atomic tmp write → os.replace → Lock release)
5. observability.audit.emit("submit", task_id, detail={command, depends_on})  ← 由 cli 層直接呼叫(L5→L4,合法)
6. cli.main 列印 task id(8-hex);若 --json 列印 {"id":..., "status":"pending"};exit 0
```

### 3.3 一次 `run --all` 的資料流(200 task DAG)

```
1. cli.commands.run --all → service.executor.run_all(emit=audit.emit, redact=audit.redact)
2. service.breaker.assert_closed()  ← **批次起點的第一件事**(FR-03)
   - 若 breaker 為 OPEN:在 spawn 任何 subprocess 之前立即 stderr `breaker open` + **exit 3**,
     整批不執行。此路徑不會走到第 5 步的 exit 0。
3. storage.task_store.topological_levels() → service.dag.validate_and_toposort
   (與 §3.2 第 3 步同一函式的**再驗證**;submit 時的閘門為主,此處防外部竄改 task store)
   - 循環偵測失敗 → exit 5 + stderr 列出循環路徑
   - 深度 > TASKQ_MAX_DAG_DEPTH → exit 5
4. 對每一層(list[task_id]):
   - ThreadPoolExecutor.map → service.executor.run_one(id, emit=…, redact=…)
   - 前置閘門一:上游相依有任一結果非 done → 標記 blocked,**不執行 subprocess**,
     **不呼叫 breaker.record_failure**(FR-06 逐字條款:blocked 不計入斷路器失敗計數),
     emit("blocked") 後直接進入下一個 task
   - 前置閘門二:service.breaker.assert_closed → storage.breaker_store
   - 有 cache 命中(FR-04)→ 直接回放,標 cached:true
   - 否則 subprocess.run(shlex.split(cmd), timeout=TASKQ_TASK_TIMEOUT, capture_output=True)
   - stdout_tail / stderr_tail 先過 redact(NFR-04)再寫 storage.task_store.mark_done|failed|timeout
   - **重試耗盡後仍為 failed/timeout** → service.breaker.record_failure → 可能 OPEN
     (中間的每次重試不重複計數;blocked 永不計數)
5. emit:run_start / run_end / retry / breaker_open / cache_hit / blocked / plugin_error
6. 走完第 4 步的 --all 模式,最終 exit code = 0(個別 task 的 failed/timeout/blocked
   不影響整批退出碼 — FR-02)。與第 2 步的 exit 3 不衝突:兩者互斥,
   OPEN 在批次起點就已整批拒絕,不存在「跑到一半回 0」的情形。
   若批次中途 breaker 轉為 OPEN,剩餘任務由第 4 步的前置閘門二逐一拒絕並標記,整批仍 exit 0。
```

### 3.4 持久化資料檔 schema(對齊 SPEC §5.2)

| 檔案 | 寫入者 | 讀取者 | 格式 | 原子性策略 |
|------|--------|--------|------|------------|
| `$TASKQ_HOME/tasks.json` | `storage.task_store` | `service.executor`, `cli.status/list` | JSON `{version:1, tasks:{id→...}}` | tmp + `os.replace` + Lock |
| `$TASKQ_HOME/breaker.json` | `storage.breaker_store` | `service.breaker` | JSON `{version:1, state, failure_count, opened_at}` | tmp + `os.replace` |
| `$TASKQ_HOME/cache.json` | `storage.cache_store` | `service.cache` | JSON `{version:1, entries:{sha256→{result, cached_at}}}` | tmp + `os.replace` + Lock |
| `$TASKQ_AUDIT_LOG` | `observability.audit` | 外部讀檔 / `export` | JSONL(append-only) | append + `fsync` |

### 3.5 Plugin 載入契約(FR-07 + NFR-02)

```
TASKQ_PLUGINS="mod_a,mod_b"  →  service.plugins.load_allowlisted
  ↓
for name in spec.split(","):
   if not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_.]*$", name):
       raise PluginLoadError(name)        → cli.main.exit_code = 6
   module = importlib.import_module(name) # 具名載入,禁路徑 / URL
   plugin = module                       # 期望有 pre_run / post_run
   registry.add(plugin)
```

- 載入面**全 codebase** 禁用 `eval(`、`exec(`、`__import__(`。
- Hook 拋例外 → 經注入的 `emit("plugin_error", …)` port 記錄並繼續;同 plugin 連續 3 次失敗 → 本次 process 停用。

---

## 4. NFR Handling(每條 NFR:目標 / 落實點 / 驗證)

> 維度名稱皆取自 `harness/toolchains/registry.py::DIMENSION_TOOLS["python"]` 的實際 key(SPEC §10)。

| NFR | dimension | 目標 | 落實模組 | 驗證命令 |
|-----|-----------|------|----------|----------|
| NFR-01 | performance | submit+status 100 iter p95 < 50ms;200 task 拓撲 p95 < 200ms | `cli.commands` + `service.dag` | `pytest-benchmark` 寫 benchmark JSON |
| NFR-02 | security | `shell=True` / `eval(` / `exec(` 全 codebase 0 命中;plugin 名稱正則;`bandit` 0 H/M | `service.executor`(禁 shell=True), `service.plugins`(allowlist 正則 + 禁動態字串) | grep + bandit + plugin path 拒絕測試 |
| NFR-03 | error_handling | 四檔原子寫;無裸 except / 吞 KeyboardInterrupt;breaker OPEN→CLOSED ≤ cooldown+1s | `storage.atomic` + 全 codebase except 規約 | ast-error-handling + 整合測試(breaker 開闔) |
| NFR-04 | security | stdout_tail / stderr_tail / audit.detail 寫入前 redaction(`sk-*` / `token=` / `Bearer`) | `observability.audit.redact`(單一實作)+ `service.executor`(經注入的 `redact` port,寫 store 前套用) | unit test 對 audit.jsonl 的 grep + 對 tasks.json 的 grep |
| NFR-05 | documentation | 全部 public 函式/類別 100% docstring,含 `[FR-XX]` / `[NFR-XX]` 引用 | 全部模組 | `ast-docstrings` 100% gate |
| NFR-06 | architecture_constraints | `.importlinter` 存在,五層契約 `cli > observability > service > storage > models`,`lint-imports` exit 0 | `.importlinter` + 全部 import 邊界 + §2.1 Output Port 規則(service/storage 不得 import observability) | `lint-imports` |
| NFR-07 | license_compliance | runtime 依賴 == 釘版;license ∈ {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0};掃描依賴樹,產 SBOM | `requirements.txt` + `requirements-dev.txt` + `08-config/SBOM.json` | `pip-licenses --format=json --with-urls` |
| NFR-08 | mutation_testing | `.methodology/harness_config.json` 的 `features.mutation_testing: true`;`mutmut` score ≥ 70;範圍限 `03-development/src/taskq_plus/service/` + `.../storage/`,限定理由(執行時間預算)寫在同檔 `_comment_mutation_testing_scope` 鍵 | `service.executor` + `service.breaker` + `service.cache` + `service.dag` + `storage.task_store` | `mutmut run` + `mutmut results`;範圍註記以 `python3 -c "import json;print(json.load(open('.methodology/harness_config.json'))['_comment_mutation_testing_scope'])"` 驗其存在 |
| NFR-09 | test_assertion_quality | skipped = 0;每個 test ≥ 1 assert;不可用 skipif / xfail / `--ignore` 排除;`VERIFIED` 僅在測試實跑通過後給 | `03-development/tests/`(所有測試檔) | `pytest -q` 輸出 + `ast-assertions` |
| NFR-10 | integration_coverage | `tests/integration/` 行覆蓋 ≥ 80%;經 CLI 入口 / `CliRunner`;涵蓋 submit→run→status 全鏈、DAG、breaker、cache、plugin、export 三格式 | `tests/integration/`(驅動 `cli.main`) | `pytest-cov-integration` |
| NFR-11 | readability | MI ≥ 80;cyclomatic ≤ 10/函式;檔案 ≤ 400 行;目錄 ≤ 15 檔 | 全部模組 | `readability-v2` + `radon cc` |
| NFR-12 | execute_verification_target | `Makefile` 有 `verify-system`;exit 0;stdout 含 `verify-system: PASS` | `Makefile` + `cli.main`(smoke) | `make verify-system` |

---

## 5. SAB Block(machine-readable — BINDING CONTRACT)

> **CONTRACT**:Field names、types、`sab:` root key、`phase` as int 必須對齊 `harness/core/quality_gate/sab_parser.py:render_canonical_sab_template()`。結構逐欄取自 canonical template,**值已於 Round 2 填為 taskq-plus 真值**(Round 1 的 `app.api.webhooks` 等 EXAMPLE 值已移除),使 §6 的 `owner_module` 可通過 `security_design.py` SEC-R6 交叉檢查。
> SAB Generation 階段仍會以 `python3 scripts/generate_sab.py --project .` 重新產出 `.methodology/SAB.json`;本區塊是它的輸入。
> 驗證:`python3 scripts/generate_sab.py --validate --project .`

<!-- SAB:START -->
```yaml
sab:
  version: "1.0"
  created_at: "2026-07-30"
  phase: 2  # MUST be int, NOT a string — parser raises on 'phase: "2"'
  project: "taskq-plus"

  layers:
    - name: models
      modules:
        - name: "taskq_plus.models.task"
        - name: "taskq_plus.models.errors"
      allowed_dependencies: []
    - name: storage
      modules:
        - name: "taskq_plus.storage.atomic"
        - name: "taskq_plus.storage.task_store"
        - name: "taskq_plus.storage.breaker_store"
        - name: "taskq_plus.storage.cache_store"
      allowed_dependencies: ["models", "config"]
    - name: service
      modules:
        - name: "taskq_plus.service.executor"
        - name: "taskq_plus.service.breaker"
        - name: "taskq_plus.service.cache"
        - name: "taskq_plus.service.dag"
        - name: "taskq_plus.service.plugins"
      allowed_dependencies: ["storage", "models", "config"]
    - name: observability
      modules:
        - name: "taskq_plus.observability.audit"
        - name: "taskq_plus.observability.export"
      allowed_dependencies: ["service", "storage", "models", "config"]
    - name: cli
      modules:
        - name: "taskq_plus.cli.main"
        - name: "taskq_plus.cli.commands"
      allowed_dependencies: ["observability", "service", "storage", "models", "config"]
    - name: config
      modules:
        - name: "taskq_plus.config"
      allowed_dependencies: []

  allowed_dependencies:
    - from: storage
      to: models
    - from: storage
      to: config
    - from: service
      to: storage
    - from: service
      to: models
    - from: service
      to: config
    - from: observability
      to: service
    - from: observability
      to: storage
    - from: observability
      to: models
    - from: observability
      to: config
    - from: cli
      to: observability
    - from: cli
      to: service
    - from: cli
      to: storage
    - from: cli
      to: models
    - from: cli
      to: config

  quality_targets:
    max_complexity: 10   # NFR-11:cyclomatic <= 10/function(嚴於 template 預設 15)
    min_coverage: 80     # NFR-10:integration line coverage >= 80
    max_coupling: 0.3

  nfr_dimension_mapping: {}  # OPTIONAL — auto-derived from nfr_traceability.type

  nfr_traceability:
    # type MUST be one of 8 legal values:
    #   Enforceable: performance, security, maintainability, reliability, testability
    #   Advisory:    deployability, scalability, usability
    # NFR-05/06/07/08/10/12 有 SPEC §10 指定的 gate dimension
    # (documentation / architecture_constraints / license_compliance /
    #  mutation_testing / integration_coverage / execute_verification_target),
    # 但那些 dimension 不在上述 8 個合法 type 內。硬塞一個近似 type 會把門檻
    # 掛到錯誤的維度(例:mutation score 70 掛到 test_assertion_quality),
    # 因此這裡刻意省略 type — 那 6 條由 harness toolchain 直接量測,不經 SAB 映射。
    NFR-01:
      type: performance
      target: "submit+status p95 < 50ms (100 iter); toposort p95 < 200ms (200 tasks)"
      module: taskq_plus.service.dag
    NFR-02:
      type: security
      target: "bandit HIGH=0 MEDIUM=0; shell=True/eval(/exec( hits = 0"
      module: taskq_plus.service.plugins
    NFR-03:
      type: reliability
      target: "4 state files atomic; no bare except; breaker OPEN->CLOSED <= cooldown+1s"
      module: taskq_plus.storage.atomic
    NFR-04:
      type: security
      target: "0 secret hits in audit.jsonl and tasks.json"
      module: taskq_plus.observability.audit
    NFR-05:
      target: "100% public docstrings carrying [FR-XX]/[NFR-XX] (dimension: documentation)"
      module: taskq_plus.cli.commands
    NFR-06:
      target: "lint-imports exit 0 (dimension: architecture_constraints)"
      module: taskq_plus.config
    NFR-07:
      target: "0 non-allowlist licenses (dimension: license_compliance)"
      module: taskq_plus.config
    NFR-08:
      target: "mutation score >= 70 over service/ + storage/ (dimension: mutation_testing)"
      module: taskq_plus.service.executor
    NFR-09:
      type: testability
      target: "skipped = 0; zero-assert test functions = 0"
      module: taskq_plus.service.executor
    NFR-10:
      target: "integration line coverage >= 80 (dimension: integration_coverage)"
      module: taskq_plus.cli.main
    NFR-11:
      type: maintainability
      target: "MI >= 80"
      module: taskq_plus.cli.commands
    NFR-12:
      target: "make verify-system exit 0 (dimension: execute_verification_target)"
      module: taskq_plus.cli.main

  advisory_only: []  # AUTO-FILLED by parser — omit or leave []

  gate_score_overrides: {}  # AUTO-DERIVED by parser — omit or leave {}

  fr_module_traceability:
    FR-01: "taskq_plus.models.task"
    FR-02: "taskq_plus.service.executor"
    FR-03: "taskq_plus.service.breaker"
    FR-04: "taskq_plus.service.cache"
    FR-05: "taskq_plus.cli.main"
    FR-06: "taskq_plus.service.dag"
    FR-07: "taskq_plus.service.plugins"
    FR-08: "taskq_plus.observability.audit"

  architecture_constraints:
    - "no_circular_dependencies"
    - "layers: cli > observability > service > storage > models"
    - "config is independence: importable by all layers, imports none"
    - "service/ and storage/ must not import observability (audit/redact are injected ports)"

  high_risk_modules:
    - "taskq_plus.service.executor"
    - "taskq_plus.service.plugins"
    - "taskq_plus.storage.task_store"
```
<!-- SAB:END -->

> 一致性不變式:`fr_module_traceability` 的 8 個 module、`high_risk_modules` 的 3 個 module、§6 每個 `owner_module`,都必須出現在上方 `layers[].modules[].name`。已逐一對照,無孤兒。
> `implemented_in` 全數省略 —— 邏輯名與實體檔案 1:1(`taskq_plus/service/executor.py`),不需要別名(`sab_amender.sab_module_candidate` 會優先取 `implemented_in`,填了反而讓 SEC-R6 比對到別的字串)。

---

## 6. Security Design(STRIDE-lite — machine-readable,BINDING CONTRACT)

> **CONTRACT**:Field names 與 `security_design:` root key 由 `harness/core/quality_gate/security_design.py:extract_security_block()` 解析。結構逐欄取自 `render_canonical_security_template()`,值已填為 taskq-plus 真值。
> `applicability: full | none`,若為 `none` 必須附 ≥ 20 字 justification 且略過其餘欄位。本專案 `full`:CLI 直接 spawn 使用者提供的命令、以 env allowlist 動態 import 第三方模組、把命令輸出落盤,三者都是真實攻擊面。
> 驗證:`python3 harness_cli.py check-artifact-consistency --project .`

<!-- SEC:START -->
```yaml
security_design:
  version: "1.0"
  applicability: full   # full | none — none REQUIRES justification and skips the rest
  justification: ""     # required (>=20 chars) when applicability: none
  trust_boundaries:
    - id: TB-01
      name: "operator shell -> CLI argument surface"
      description: "task command strings and --after ids crossing from the operator (or a calling script) into cli.commands before any validation"
    - id: TB-02
      name: "taskq-plus -> OS subprocess"
      description: "the validated command string crossing out of service.executor into a spawned OS process with the operator's own privileges"
    - id: TB-03
      name: "TASKQ_PLUGINS env -> dynamic module import"
      description: "third-party Python module names crossing from the environment into service.plugins and being imported into this process"
    - id: TB-04
      name: "process -> local state files under $TASKQ_HOME"
      description: "task state, breaker state, cache entries and audit records crossing from memory onto disk, concurrently, from multiple threads and processes"
  threats:              # STRIDE-lite — every boundary needs >=1 threat
    - id: T-01
      boundary: TB-01
      category: tampering
      description: "shell metacharacters (; | & $() backtick newline) embedded in a submitted command string alter what is later executed"
      mitigation: "models.task.TaskSubmission rejects the SPEC FR-01 metacharacter blacklist at submit time; empty/whitespace-only commands rejected; exit 2"
      owner_module: "taskq_plus.models.task"
      nfr: NFR-02
      verified_by: "test_sec_t01_command_metacharacters_rejected_at_submit"
    - id: T-02
      boundary: TB-01
      category: denial_of_service
      description: "a pathological --after chain builds an unbounded dependency depth and exhausts memory/stack during topological sort"
      mitigation: "service.dag enforces TASKQ_MAX_DAG_DEPTH and rejects with exit 5 before any execution; cycle detection rejects self-feeding graphs"
      owner_module: "taskq_plus.service.dag"
      nfr: NFR-01
      verified_by: "test_sec_t02_dag_depth_cap_rejects_deep_chain"
    - id: T-03
      boundary: TB-02
      category: elevation_of_privilege
      description: "using shell=True (or string-concatenated commands) would let a stored command string reach a shell interpreter and run beyond the submitted program"
      mitigation: "service.executor always calls subprocess.run(shlex.split(cmd), shell=False); a repo-wide grep gate keeps shell=True / eval( / exec( / __import__( at zero hits"
      owner_module: "taskq_plus.service.executor"
      nfr: NFR-02
      verified_by: "test_sec_t03_executor_never_uses_shell_true"
    - id: T-04
      boundary: TB-02
      category: denial_of_service
      description: "a hanging or output-flooding child process pins the executor thread and never returns"
      mitigation: "TASKQ_TASK_TIMEOUT on every subprocess.run, capped retries (TASKQ_RETRY_LIMIT with exponential backoff), and the global breaker opening after TASKQ_BREAKER_THRESHOLD final failures"
      owner_module: "taskq_plus.service.breaker"
      nfr: NFR-03
      verified_by: "test_sec_t04_subprocess_timeout_marks_task_timeout"
    - id: T-05
      boundary: TB-03
      category: elevation_of_privilege
      description: "a plugin name pointing at a filesystem path, URL, or dynamic string turns TASKQ_PLUGINS into an arbitrary-code-execution entry point (SPEC §9 R6)"
      mitigation: "service.plugins accepts only ^[A-Za-z_][A-Za-z0-9_.]*$ module names from the TASKQ_PLUGINS allowlist and loads them via importlib.import_module; path/URL forms and eval/exec/__import__ are rejected with exit 6"
      owner_module: "taskq_plus.service.plugins"
      nfr: NFR-02
      verified_by: "test_sec_t05_plugin_path_or_url_spec_rejected"
    - id: T-06
      boundary: TB-04
      category: information_disclosure
      description: "API keys or bearer tokens present in a task's stdout/stderr are persisted verbatim into tasks.json and audit.jsonl"
      mitigation: "observability.audit.redact replaces sk-* / token= / Bearer patterns with [REDACTED]; it is applied before the audit append and is injected into service.executor as the redact port so stdout_tail/stderr_tail are scrubbed before the store write"
      owner_module: "taskq_plus.observability.audit"
      nfr: NFR-04
      verified_by: "test_sec_t06_secrets_absent_from_audit_log_and_task_store"
    - id: T-07
      boundary: TB-04
      category: tampering
      description: "concurrent run --all writers interleave and leave tasks.json truncated or half-written, silently losing or corrupting task state (SPEC §9 R1)"
      mitigation: "storage.atomic writes to a temp file then os.replace (atomic rename) under a threading.Lock held by storage.task_store; a corrupted store is detected at startup and exits 1 instead of being silently rebuilt"
      owner_module: "taskq_plus.storage.atomic"
      nfr: NFR-03
      verified_by: "test_sec_t07_concurrent_writes_leave_task_store_parseable"
    - id: T-08
      boundary: TB-04
      category: repudiation
      description: "an execution cannot be reconstructed afterwards because events are missing, unordered, or cannot be tied back to the CLI invocation that caused them"
      mitigation: "observability.audit appends JSON Lines with fsync, append-only, every record carrying ts/event/task_id/correlation_id, with one correlation_id per CLI invocation shared by all events it triggers"
      owner_module: "taskq_plus.observability.audit"
      verified_by: "test_sec_t08_audit_events_share_one_correlation_id"
```
<!-- SEC:END -->

> STRIDE-lite 覆蓋說明:`spoofing` 無對應威脅 —— taskq-plus 是單一使用者的本地 CLI,沒有身分宣稱、沒有網路端點、沒有多租戶,無可偽冒的主體。其餘五類皆已建模。
> 交叉檢查(已於本輪對齊):8 條威脅的 `owner_module` 全部出現在 §5 `sab.layers[].modules[].name`;`nfr` 只引用 SPEC/SRS 存在的 NFR-01/02/03/04;§5 中 `type: security` 的 NFR-02、NFR-04 各至少被一條威脅引用(SEC-R7);`verified_by` 皆為單一測試名。
> `verified_by` 的 8 個測試名於 Phase 5 前必須實際存在於 `03-development/tests/`(SEC-R8),由 TEST_SPEC 承接。

---

*文件版本:v1.1.0 — 對齊 SPEC v1.0.0 (8 FR / 12 NFR) | 2026-07-30 | 漸進式驗證測床第 1 輪 SAD Round 2*

**Round 2 變更**:(1) §2.1 新增 Output Port 規則 — 修正 Round 1 中 `service.executor` / `service.plugins` 宣告依賴 `observability.audit` 這條會直接違反 NFR-06 `lint-imports` 契約的向上 import;(2) §2.2 補 CRG hub-module 規約;(3) §2.4 + §3.3 明文化 FR-06「blocked 不計入斷路器」與 FR-03「批次起點 OPEN 即 exit 3」;(4) §4 NFR-08 指名 `harness_config.json` 的註記鍵;(5) §5 SAB 與 §6 SEC 由 EXAMPLE 佔位改為 taskq-plus 真值,兩區塊 module 名已對齊。