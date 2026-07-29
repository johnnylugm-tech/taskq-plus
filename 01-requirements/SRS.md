# Software Requirements Specification (SRS) — taskq-plus

**Document version:** v1.0.0 (aligned to SPEC.md v1.0.0, 2026-07-30)
**Project:** taskq-plus (local task-queue CLI)
**Phase:** 1 — INGESTION MODE
**Canonical source:** `/Users/johnny/projects/taskq-plus/SPEC.md`
**Authority rule:** every `### FR-XX` / `### NFR-XX` clause below is transcribed from SPEC.md §3 / §4. Where a verbatim phrase leaves a measurement or interpretation boundary ambiguous, the boundary is owned by the test harness per the cited canonical line; any extra interpretation by Agent A is annotated with a `DERIVED:` tag above the AC.

---

## 1. Introduction

`taskq-plus` is a local task-queue CLI tool (Python 3.11; entry `python -m taskq_plus`). Users submit shell commands as tasks; the tool runs them with controlled concurrency, per-task timeout, retry, a circuit breaker, a TTL result cache, and dependency-DAG ordering; behaviour can be extended through an allowlisted plugin-hook system; the run emits a structured JSONL audit trail; results can be exported as JSON, CSV, or Markdown.

This SRS is the deliverable Phase-1 artefact; it transcribes 100% of `### FR-01..FR-08` and `### NFR-01..NFR-12` from `SPEC.md` §3 / §4 — no invention, no omission. TBD / TODO / `<placeholder>` markers from SPEC.md (if any) are emitted as `NFR-99` / `FR-XX-deferred` entries in §7 Open Issues rather than silently dropped.

Reference anchors for downstream phases:
- exit-code table — SPEC.md §3 / §7 (reproduced in §5 Acceptance Criteria Summary)
- env var table — SPEC.md §5.1 (reproduced in §3 dependency of FR-05 / FR-02 / FR-03 / FR-04 / FR-06 / FR-07 / FR-08)
- data-file table — SPEC.md §5.2 (reproduced as the AC storage contract for FR-01..FR-04 / FR-08)
- module layout — SPEC.md §6 (consumed by Phase 3; high-risk modules flagged in §8 Risks)

---

## 2. Constraints

These constraints are transcribed from SPEC.md §1 / §2 / §5 / §10 and the `PROJECT_BRIEF.md` "Key Constraints" block. Every FR/NFR below must be implementable under them without further invention.

| # | Constraint | Source |
|---|------------|--------|
| C-01 | Python 3.11; CLI entry `python -m taskq_plus` | SPEC.md §1 / `PROJECT_BRIEF.md` |
| C-02 | CLI uses `click` command groups; data validation uses `pydantic` v2 | SPEC.md §2 |
| C-03 | Subprocess execution via `subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=...)`; **`shell=True` forbidden anywhere** | SPEC.md §3 FR-02 / NFR-02 |
| C-04 | Concurrency via `concurrent.futures.ThreadPoolExecutor` (FR-02 `--all`); shared `threading.Lock` over the store | SPEC.md §2 / FR-02 |
| C-05 | Dependency scheduling: Kahn topological sort; cycles must be rejected | SPEC.md §2 / FR-06 |
| C-06 | Plugin loading: `importlib.import_module` by allowlisted module name; `eval` / `exec` / path / URL loading forbidden | SPEC.md §2 / FR-07 / NFR-02 |
| C-07 | Atomic persistence (tmp + `os.replace`) for all four data files; audit log is append + fsync | SPEC.md §2 / NFR-03 |
| C-08 | Real third-party dependencies pinned with `==` in `requirements.txt`; allowed licenses: MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0; SBOM at `08-config/SBOM.json` | SPEC.md §4 NFR-07 / `PROJECT_BRIEF.md` |
| C-09 | Five-layer architecture enforced by `.importlinter`: `cli > observability > service > storage > models`; `config` is an independence module | SPEC.md §2 / §6 / NFR-06 |
| C-10 | No-skip verification: `pytest -q` reports **0 skipped**; no `--ignore` / `-k` / `--deselect` / `collect_ignore` reaches that number; VERIFIED only when the test actually ran and passed | SPEC.md §4 NFR-09 |
| C-11 | CRG calibration (`crg_cohesion_healthy`) must not be lowered to accommodate this project | SPEC.md §10 framework alignment |

---

## 3. Functional Requirements

### FR-01: 任務提交與驗證

`taskq-plus submit "<command>" [--name NAME] [--after ID]...`

Command: `taskq-plus submit "<command>" [--name NAME] [--after ID]...`

Submitted fields are validated by the **`pydantic` model `TaskSubmission`**; any violation → **exit 2** + stderr error message, no write to store (verbatim — SPEC.md §3 FR-01).

| Rule | Condition |
|------|-----------|
| Non-empty | command empty or all whitespace → reject |
| Length | command > 1000 chars → reject |
| Injection chars | command contains any of `;` `|` `&` `$` `>` `< <code>`  → reject (NFR-02) |
| Name unique | `--name` collides with existing pending/running task → reject |
| Dependency exists | `--after` points to a non-existent task id → reject |

On pass:
- task id is generated (uuid4 first 8 hex)
- status `pending`, records `command`, `name`, `created_at`, `depends_on` (list[str])
- atomic write to `$TASKQ_HOME/tasks.json`
- stdout prints the task id (with `--json`, prints `{"id": ..., "status": "pending"}`)
- one `submit` audit event is written (FR-08)

**DERIVED: SPEC.md §3 FR-01 + §8 #4–6** — ACs elaborate the rule table by binding each rule to a single machine-decidable input/output pair; no clause beyond canonical is asserted.

**Acceptance criteria** (verbatim command + expected output, per SPEC.md §8 #4–6):

- **AC-FR-01.1** — `python -m taskq_plus submit "echo hi"` → stdout is an 8-hex id; exit 0 (SPEC.md §8 #4).
- **AC-FR-01.2** — `python -m taskq_plus submit ""` → exit 2 (SPEC.md §8 #5).
- **AC-FR-01.3** — `python -m taskq_plus submit "echo hi; rm x"` → exit 2 due to injection char (SPEC.md §8 #6).
- **AC-FR-01.4** — submitting a `>1000`-char command (or all-whitespace) → exit 2; nothing written to `$TASKQ_HOME/tasks.json`.
- **AC-FR-01.5** — re-submitting with the same `--name` while a previous task with that name is `pending` / `running` → exit 2.
- **AC-FR-01.6** — `--after` referencing a non-existent task id → exit 2, stderr identifies the bad dependency (per SPEC.md §7 row `--after` 指向不存在的 id).
- **AC-FR-01.7** — successful submission results in one `submit` JSONL audit event written to `$TASKQ_AUDIT_LOG` (FR-08 §3). Cycle path is not yet relevant here; see FR-06.

### FR-02: 任務執行器

`taskq-plus run <id>` or `taskq-plus run --all`.

Verbatim from SPEC.md §3 FR-02:
> "`subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=TASKQ_TASK_TIMEOUT)` 執行;任何路徑不得使用 `shell=True`"

State machine (verbatim): `pending → running → done | failed | timeout | blocked`.
- exit 0 → `done`; non-zero → `failed`; `TimeoutExpired` → `timeout`
- dependency unsatisfied → `blocked` (FR-06)

Result fields (verbatim): `exit_code`, `stdout_tail` (last 2000 chars), `stderr_tail` (last 2000 chars), `duration_ms`, `finished_at`.

`--all` (verbatim): `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)` runs all runnable `pending` tasks in DAG topological order (FR-06); store writes are thread-safe (shared lock).

Single-task mode `timeout` result → **exit 4** (verbatim).

**DERIVED: SPEC.md §3 FR-02** — ACs restate each verbatim clause as a single test binding; `last 2000 chars` in AC-FR-02.5 translates "末 2000 字元" verbatim, no extra slice semantics added.

**Acceptance criteria** (verbatim where applicable):

- **AC-FR-02.1** — `subprocess.run` is invoked with `shell=False` (i.e. no `shell=True`) — verified by `grep -rn "shell=True" 03-development/src/` returning 0 hits (SPEC.md §8 #15).
- **AC-FR-02.2** — completion fields (`exit_code`, `stdout_tail`, `stderr_tail`, `duration_ms`, `finished_at`) are written atomically for every terminal status.
- **AC-FR-02.3** — `--all` runs tasks in DAG order with `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)`; concurrent writes to `$TASKQ_HOME/tasks.json` are serialised by the shared `threading.Lock`; mid-write crash leaves `tasks.json` valid JSON (NFR-03 atomicity).
- **AC-FR-02.4** — `TASKQ_TASK_TIMEOUT=1 python -m taskq_plus run <sleep-5-id>` → status `timeout`, exit 4 (SPEC.md §8 #7).
- **AC-FR-02.5** — `stdout_tail` / `stderr_tail` contain at most the last 2000 chars of the captured streams.

### FR-03: 重試與斷路器

**Retry** (verbatim, SPEC.md §3 FR-03):
> "結果為 `failed`/`timeout` 時自動重試,上限 `TASKQ_RETRY_LIMIT` 次;第 n 次重試前等待 `TASKQ_BACKOFF_BASE × 2^n` 秒(exponential backoff;sleep 函式必須可注入以利測試)"

**Breaker** (verbatim, SPEC.md §3 FR-03):
- 連續最終失敗(重試耗盡仍 failed/timeout)計數 ≥ `TASKQ_BREAKER_THRESHOLD` → `OPEN`
- `OPEN` 期間任何 `run` 立即拒絕:**exit 3** + stderr `breaker open`,不執行 subprocess
- 經 `TASKQ_BREAKER_COOLDOWN` 秒後進入 `HALF_OPEN`:放行一個任務 — 成功 → `CLOSED` 且計數歸零;失敗 → 重新 `OPEN`
- 狀態持久化於 `$TASKQ_HOME/breaker.json`(原子寫)

**DERIVED: SPEC.md §3 FR-03 "重試耗盡仍 failed/timeout"** — Agent A reproduces the canonical counting rule; whether the breaker counts only final post-retry outcomes (verbatim reading) is verified by the harness test, not by Agent A.

**Acceptance criteria**:

- **AC-FR-03.1** — retry on each `failed` / `timeout` up to `TASKQ_RETRY_LIMIT`; nth retry waits `TASKQ_BACKOFF_BASE × 2^n` seconds before invocation (sleep injectable for test isolation).
- **AC-FR-03.2** — `TASKQ_BREAKER_THRESHOLD` consecutive final failures (post-retry) → breaker state becomes `OPEN`; the state is persisted atomically in `$TASKQ_HOME/breaker.json`.
- **AC-FR-03.3** — while `OPEN`, any `run` immediately rejects with exit 3 + stderr `breaker open`; no subprocess executes (SPEC.md §8 #8 negative path).
- **AC-FR-03.4** — `OPEN → HALF_OPEN` transition occurs after `TASKQ_BREAKER_COOLDOWN` seconds; one task is admitted; success → `CLOSED` and failure counter resets to 0; failure → returns to `OPEN`.
- **AC-FR-03.5** — once the counter reaches threshold, `cooldown` passes, and a task succeeds, a subsequent run is admitted normally (SPEC.md §8 #8 positive path).

### FR-04: 結果 TTL 快取

Verbatim from SPEC.md §3 FR-04:
> "快取簽名 = `sha256(command)`"
> "`taskq-plus run <id> --cached`:同簽名且結果為 `done` 的最近執行在 `TASKQ_CACHE_TTL` 秒內 → 直接回放(`exit_code`/`stdout_tail`),不執行 subprocess,任務標記 `done` 且 `cached: true`"
> "快取過期或不存在 → 正常執行,成功(`done`)後寫入 `$TASKQ_HOME/cache.json`"
> "快取讀寫:原子 + 執行緒安全(與 FR-02 並發共存)"

**DERIVED: SPEC.md §3 FR-04 "同簽名"** — the cache key is `sha256(command)` of the canonical command string; Agent A does not redefine what fields are hashed. The harness may assert additionally that whitespace normalisation is not implied by canonical.

**Acceptance criteria**:

- **AC-FR-04.1** — cache signature is `sha256(command)` (binary content); the same string always maps to the same key.
- **AC-FR-04.2** — within `TASKQ_CACHE_TTL` of a `done` run, `python -m taskq_plus run <id> --cached` does not spawn a subprocess (verifiable: child-process counter unchanged), replays `exit_code` and `stdout_tail`, marks the task `done` with `cached: true` (SPEC.md §8 #9).
- **AC-FR-04.3** — cache miss or TTL expiry → normal execution; on `done`, the result is written atomically (tmp + `os.replace`) to `$TASKQ_HOME/cache.json` under the `sha256(command)` key with `cached_at`.
- **AC-FR-04.4** — cache read/write is thread-safe with FR-02 concurrent execution.

### FR-05: CLI 整合

`click` group subcommands; entry `python -m taskq_plus` (verbatim, SPEC.md §3 FR-05):

| Command | Behaviour |
|---------|-----------|
| `submit "<cmd>" [--name N] [--after ID]...` | FR-01 |
| `run <id> [--cached]` / `run --all` | FR-02/03/04/06 |
| `status <id>` | prints full task fields |
| `list [--status S]` | lists tasks (optionally filterable by status) |
| `graph [--format text\|dot]` | prints dependency graph (FR-06) |
| `plugins list` | lists loaded plugins and their hooks (FR-07) |
| `export --format json\|csv\|md` | exports task results (FR-08) |
| `clear` | clears all `$TASKQ_HOME` data files |

Global flag `--json`: machine-readable (single-line JSON) output.

**Exit codes** (verbatim, SPEC.md §3 / §7):
`0` success / `2` input validation error (incl. unknown task id) / `3` breaker open / `4` task timeout (single-task mode) / `5` dependency cycle or depth cap exceeded / `6` plugin load failure / `1` other internal error.

**DERIVED: SPEC.md §3 FR-05** — ACs enumerate the subcommand table as discrete reachability / format / exit-code checks; no new subcommand is added.

**Acceptance criteria**:

- **AC-FR-05.1** — every subcommand listed above is reachable as `python -m taskq_plus <command>` (or `python -m taskq_plus <group> <sub>` for nested ones).
- **AC-FR-05.2** — `--json` flag (where applicable) produces single-line JSON on stdout.
- **AC-FR-05.3** — exit code matches the canonical map for every condition enumerated in SPEC.md §3 / §7 (see §5 Acceptance Criteria Summary table below for the full mapping).

### FR-06: 任務相依 DAG

Verbatim clauses from SPEC.md §3 FR-06:
- `submit --after <id>` 可重複指定,建立 `depends_on` 邊
- `run --all` 以 **Kahn 拓撲排序**決定執行順序;同一層(入度為 0)的任務才可並發
- 相依任務結果非 `done` → 下游任務標記 `blocked`,**不執行**,且不計入斷路器失敗計數
- **循環偵測**:`submit --after` 若會造成循環 → 拒絕該次提交,**exit 5** + stderr 列出循環路徑(`A → B → C → A`)
- **深度上限**:相依鏈深度 > `TASKQ_MAX_DAG_DEPTH` → 拒絕,exit 5(防止病態輸入耗盡資源)
- `graph --format dot` 輸出 Graphviz DOT;`--format text` 輸出縮排樹

**DERIVED: SPEC.md §3 FR-06** — ACs elaborate each verbatim DAG clause (Kahn ordering, blocked semantics, cycle rejection, depth cap, graph output, breaker non-counting) as a single test binding; no new DAG semantics added.

**Acceptance criteria**:

- **AC-FR-06.1** — after `python -m taskq_plus submit "echo b" --after <a>` and `run --all`, task `b` executes only after `a` finishes; if `a` is not `done`, `b` is marked `blocked` and does not run (SPEC.md §8 #10).
- **AC-FR-06.2** — `tasks.json` carries `depends_on` per task, encoded as a list of task ids.
- **AC-FR-06.3** — submitting edges that close a cycle (e.g. A→B→C→A) → submit is rejected, exit 5, stderr prints the cycle path (SPEC.md §8 #11).
- **AC-FR-06.4** — dependency chain depth > `TASKQ_MAX_DAG_DEPTH` → submit is rejected, exit 5, stderr `dependency chain too deep: <n> > <max>` (SPEC.md §7 row).
- **AC-FR-06.5** — `graph --format text` outputs an indented tree; `graph --format dot` outputs Graphviz DOT.
- **AC-FR-06.6** — a task whose dependency is not `done` does **not** count towards the breaker failure counter (per verbatim clause above).

### FR-07: Plugin Hook 系統

Verbatim from SPEC.md §3 FR-07:

Plugin = Python module exposing `pre_run(task) -> None` and/or `post_run(task, result) -> None`.

Loading source: **`TASKQ_PLUGINS` env-var module names only** (comma-separated allowlist), via `importlib.import_module` named-load.

Security invariants (verbatim, NFR-02):
- 禁止 `eval` / `exec` / `__import__` 動態字串
- 禁止從檔案路徑或 URL 載入(只接受已安裝的模組名)
- Plugin 模組名必須匹配 `^[A-Za-z_][A-Za-z0-9_.]*$`,不符 → 拒絕載入,**exit 6**

Exception isolation (verbatim): Plugin exception must **not** abort task execution — a `plugin_error` audit event is recorded (FR-08) and execution continues; a plugin is disabled for the current run after 3 consecutive failures.

`plugins list` prints each plugin's module name, registered hooks, load status.

**DERIVED: SPEC.md §3 FR-07 "連續 3 次失敗"** — "consecutive failure" is counted per-plugin per-run; whether the counter resets on a successful hook call is asserted by the harness (canonical wording supports both readings; Agent A does not pin one).

**Acceptance criteria**:

- **AC-FR-07.1** — `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` → exit 6 (SPEC.md §8 #12) — paths and URLs are rejected by the regex allowlist.
- **AC-FR-07.2** — plugin module name not matching `^[A-Za-z_][A-Za-z0-9_.]*$` → exit 6, stderr `plugin load failed: <name>: <reason>` (SPEC.md §7 row).
- **AC-FR-07.3** — a plugin whose `pre_run` raises → the run still completes; `audit.jsonl` contains a `plugin_error` event (SPEC.md §8 #13).
- **AC-FR-07.4** — 3 consecutive plugin failures → the plugin is disabled for the remainder of the current run; further calls from that plugin's hooks are skipped without re-raising.
- **AC-FR-07.5** — full-codebase grep `grep -rn "eval(\|exec(\|__import__(" 03-development/src/` returns 0 hits (NFR-02 binding clause).

### FR-08: 結構化稽核日誌與匯出

**Audit log** (verbatim, SPEC.md §3 FR-08):
- path `$TASKQ_AUDIT_LOG` (default `$TASKQ_HOME/audit.jsonl`), **JSON Lines**, append-only
- per-event fields: `ts` (ISO-8601 UTC), `event`, `task_id`, `correlation_id`, `detail`
- `correlation_id` is generated per CLI invocation; all events triggered by the same invocation share it
- event types: `submit` / `run_start` / `run_end` / `retry` / `breaker_open` / `breaker_close` / `cache_hit` / `blocked` / `plugin_error`
- NFR-04 redaction is applied **before writing**

**Export** (verbatim):
- `export --format json` → single JSON array, fields match `status`
- `export --format csv` → header row + one row per task; comma/quote fields escaped per CSV conventions
- `export --format md` → Markdown table
- all three formats must report the same task count and the same field set (asserted by test)

**DERIVED: SPEC.md §3 FR-08** — ACs elaborate the verbatim audit/export contracts by binding each field/event-class to a single observable artefact; "RFC 4180 quoting" in AC-FR-08.4 names the conventional CSV escape rule referenced by canonical "逗號/引號的欄位必須正確跳脫"; no formatting rule beyond canonical is asserted.

**Acceptance criteria**:

- **AC-FR-08.1** — every CLI invocation emits at least one audit line containing a single `correlation_id` shared across all that invocation's events; the path is `$TASKQ_AUDIT_LOG`, default `$TASKQ_HOME/audit.jsonl`; format is one JSON object per line; append-only; `fsync` after each write.
- **AC-FR-08.2** — running a command containing an `sk-`-style secret → `grep -c "sk-" $TASKQ_HOME/audit.jsonl` returns 0 (SPEC.md §8 #22) (see also NFR-04).
- **AC-FR-08.3** — `python -m taskq_plus export --format json` / `csv` / `md` produce row-count-equal and field-set-equal output (SPEC.md §8 #14).
- **AC-FR-08.4** — CSV fields containing commas or quotes are escaped per CSV conventions (RFC 4180 quoting); the row count matches the JSON output's object count.

---

## 4. Non-Functional Requirements

### NFR-01: 效能預算

- **dimension:** `performance`

Verbatim from SPEC.md §4 NFR-01:
> "`submit` + `status` 組合操作(不含 subprocess 執行)100 次 **p95 < 50ms**"
> "`run --all` 對 200 個任務的**拓撲排序階段**(不含 subprocess 執行)**p95 < 200ms**"
> 量測方式:`pytest-benchmark`,結果寫入 benchmark JSON

**DERIVED: SPEC.md §4 NFR-01 "(不含 subprocess 執行)"** — measurement scope: only the in-process store/serialise/lookup/derivation work is profiled; subprocess spawn and child-process runtime are excluded by canonical phrasing. The harness wall-clock measurement boundary (e.g. whether `python -m app` startup is included) is owned by the test harness per the canonical line. Agent A does not prescribe the wall-clock boundary.

**Acceptance criteria** (testable, machine-decidable):

- **AC-NFR-01.1** — a `pytest-benchmark` suite of 100 iterations of `submit`+`status` (in-process only — no subprocess) reports a p95 latency strictly **less than 50 ms**; result is written to a benchmark JSON artefact.
- **AC-NFR-01.2** — a `pytest-benchmark` suite of 200 tasks through the topological-sort stage only (no subprocess invocation) reports a p95 latency strictly **less than 200 ms**.

### NFR-02: 執行與載入安全

- **dimension:** `security`

Verbatim clauses from SPEC.md §4 NFR-02:
- 全 codebase **禁用 `shell=True`**(以 grep 驗證,0 命中)
- FR-01 注入字元黑名單必須有測試覆蓋(每個字元一個 case)
- **Plugin 載入面**(FR-07):全 codebase 禁用 `eval(` / `exec(` / `__import__(`;plugin 名稱必須通過 `^[A-Za-z_][A-Za-z0-9_.]*$` 白名單正則;不得接受檔案路徑或 URL
- `bandit -r 03-development/src/` 結果:**0 HIGH、0 MEDIUM**

**DERIVED: SPEC.md §4 NFR-02** — ACs translate each verbatim rule into one observable check command; the per-character injection coverage in AC-NFR-02.2 binds the verbatim "每個字元一個 case" to the seven characters in the canonical set.

**Acceptance criteria**:

- **AC-NFR-02.1** — `grep -rn "shell=True" 03-development/src/` → 0 hits.
- **AC-NFR-02.2** — at least one test case per FR-01 injection character (`;` `|` `&` `$` `>` `< `` ` ``) verifies the corresponding command is rejected with exit 2.
- **AC-NFR-02.3** — `grep -rn "eval(\|exec(\|__import__(" 03-development/src/` → 0 hits.
- **AC-NFR-02.4** — plugin module names that do not match `^[A-Za-z_][A-Za-z0-9_.]*$` (e.g. path-like or URL-like strings) are rejected with exit 6.
- **AC-NFR-02.5** — `bandit -r 03-development/src/` reports 0 HIGH and 0 MEDIUM findings (SPEC.md §8 #19).

### NFR-03: 錯誤處理與原子性

- **dimension:** `error_handling`

Verbatim clauses from SPEC.md §4 NFR-03:
- 四個資料檔(`tasks.json`/`breaker.json`/`cache.json`/`audit.jsonl`)全部原子寫(tmp + `os.replace`;audit 為 append + fsync),進程中斷後檔案仍為合法 JSON / JSONL
- **不得**出現裸 `except:`、`except Exception: pass`、吞掉 `KeyboardInterrupt`/`SystemExit`
- 每個 `except` 區塊必須是三者之一:重新拋出、轉譯為明確的領域例外、記錄後以明確 exit code 結束
- breaker `OPEN → CLOSED` 恢復時間 ≤ `TASKQ_BREAKER_COOLDOWN` + 1s

**DERIVED: SPEC.md §4 NFR-03 "OPEN → CLOSED 恢復時間 ≤ TASKQ_BREAKER_COOLDOWN + 1s"** — the "+1s" slack is verbatim from SPEC.md; Agent A does not elaborate on which clock (monotonic vs wall-clock) measures it.

**Acceptance criteria**:

- **AC-NFR-03.1** — every write to `tasks.json`, `breaker.json`, `cache.json` is implemented as `tmp` file in the same directory + `os.replace`; killing the process mid-write leaves the file readable as valid JSON (`json.loads` succeeds).
- **AC-NFR-03.2** — `audit.jsonl` writes use append + `fsync`; killing the process mid-write preserves all complete lines and never produces a half-line.
- **AC-NFR-03.3** — repository-wide scan finds no bare `except:` clause and no `except Exception: pass` clause; every `except` block either re-raises, translates to a named domain exception (per SPEC.md §3 NFR-03), or logs and exits with a definite code from §7.
- **AC-NFR-03.4** — once the breaker is `OPEN`, the elapsed wall-clock to `OPEN → CLOSED` (i.e. half-open admission + success) is `≤ TASKQ_BREAKER_COOLDOWN + 1 s`.

### NFR-04: 敏感資料遮蔽

- **dimension:** `security`

Verbatim from SPEC.md §4 NFR-04:
> "`stdout_tail` / `stderr_tail` / 稽核日誌 `detail` 落盤前,匹配 `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` 的行整行以 `[REDACTED]` 取代"
> "遮蔽發生在**寫入前**,不是讀取後(以「檔案內容不含明文 secret」斷言)"

**DERIVED: SPEC.md §4 NFR-04** — ACs name the redaction regex verbatim and bind each output channel to a single observable artefact; "secret" in AC-NFR-04.3 is the class of strings matched by the canonical regex.

**Acceptance criteria**:

- **AC-NFR-04.1** — redaction regex `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` is applied before writing to `stdout_tail`, `stderr_tail`, and any audit `detail` field.
- **AC-NFR-04.2** — after running a command whose output contains an `sk-`-style secret, `grep -c "sk-" $TASKQ_HOME/audit.jsonl` returns 0 (SPEC.md §8 #22).
- **AC-NFR-04.3** — redaction happens at write time; the in-memory result (before redaction) is not persisted anywhere on disk (assertion: file content does not contain plaintext secret strings).

### NFR-05: 文件覆蓋

- **dimension:** `documentation`

Verbatim from SPEC.md §4 NFR-05:
> "`03-development/src/taskq_plus` 全部公開函式/類別有 docstring 且含 `[FR-XX]` 或 `[NFR-XX]` 引用"
> "覆蓋率 **100%**(`ast-docstrings` 量測)"

**DERIVED: SPEC.md §4 NFR-05** — ACs split the verbatim docstring contract into a coverage check and a tag-format check; no extra docstring content rule is asserted.

**Acceptance criteria**:

- **AC-NFR-05.1** — `ast-docstrings` measurement reports 100% coverage on `03-development/src/taskq_plus` public functions/classes.
- **AC-NFR-05.2** — every covered symbol's docstring contains at least one bracketed reference of the form `[FR-XX]` or `[NFR-XX]`.

### NFR-06: 架構分層契約

- **dimension:** `architecture_constraints`

Verbatim from SPEC.md §4 NFR-06:
- 專案根目錄**必須存在 `.importlinter`**,宣告 layers contract:

  ```
  cli > observability > service > storage > models
  ```

  上層可 import 下層,**下層不得 import 上層**;`config` 為 independence 模組,任何層都可 import 它,但它不得 import 任何層
- `lint-imports` 必須 **exit 0**
- **禁止**以刪除 `.importlinter`、把 contract 放寬成萬用字元 `ignore_imports`、或降級為單條 `forbidden` 的方式取得通過

**DERIVED: SPEC.md §4 NFR-06** — ACs split the verbatim layering contract into an existence check, an exit-code check, and a non-weakening audit check; "no blanket `ignore_imports`" in AC-NFR-06.3 mirrors the canonical "把 contract 放寬成萬用字元 `ignore_imports`".

**Acceptance criteria**:

- **AC-NFR-06.1** — `.importlinter` exists at the project root and declares the layering `cli > observability > service > storage > models` with `config` as an independence module.
- **AC-NFR-06.2** — `lint-imports` exits 0 (SPEC.md §8 #17).
- **AC-NFR-06.3** — the contract is not weakened (no blanket `ignore_imports`, no reduction to a single `forbidden` rule, no deletion of `.importlinter`); the audit log records no such weakening.

### NFR-07: 依賴與授權合規

- **dimension:** `license_compliance`

Verbatim from SPEC.md §4 NFR-07:
- 全部 runtime 依賴在 `requirements.txt` 以 `==` **釘版**(不得 `>=` / `~=` / 無版本)
- 允許的 license:**MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0**;出現其他 license → 該依賴不得使用
- **掃描範圍必須包含已安裝的依賴樹**,不得只掃自家原始碼。可接受的證據命令(擇一):
  - `pip-licenses --format=json --with-urls`
  - `scancode --license <venv>/lib/python3.11/site-packages --json-pp -`
- 產出 SBOM 於 `08-config/SBOM.json`,列出每個依賴的 `name` / `version` / `license`

**DERIVED: SPEC.md §4 NFR-07** — ACs split the verbatim license/dependency contract into pinning, scanning, and SBOM artefact checks; the allowlist-set reproduction in AC-NFR-07.2 mirrors the canonical {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0}.

**Acceptance criteria**:

- **AC-NFR-07.1** — every runtime dependency in `requirements.txt` is pinned with `==`; no `>=`, `~=`, or version-less specifiers.
- **AC-NFR-07.2** — running `pip-licenses --format=json` (or equivalent `scancode` invocation) reports only licenses from the allowlist {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0} (SPEC.md §8 #18); the scan covers the installed dependency tree, not just first-party source.
- **AC-NFR-07.3** — `08-config/SBOM.json` exists and lists each dependency with `name`, `version`, `license`.

### NFR-08: 變異測試

- **dimension:** `mutation_testing`

Verbatim from SPEC.md §4 NFR-08:
- `.methodology/harness_config.json` 必須設 `features.mutation_testing: true`
- **mutation score ≥ 70**
- 範圍限定於 `03-development/src/taskq_plus/service/` 與 `.../storage/` 兩層(核心邏輯),並在 `harness_config.json` 以註記說明限定理由(執行時間預算)

**DERIVED: SPEC.md §4 NFR-08 "範圍限定於 … service/ 與 storage/"** — Agent A reproduces the verbatim scope limit; any expansion beyond `service/` + `storage/` is out of scope for NFR-08 in this round.

**Acceptance criteria**:

- **AC-NFR-08.1** — `.methodology/harness_config.json` has `features.mutation_testing` set to `true`; the file annotates the `service/` + `storage/` scope limit.
- **AC-NFR-08.2** — `mutmut run` followed by `mutmut results` reports a mutation score **≥ 70** over the `service/` and `storage/` layers (SPEC.md §8 #20).

### NFR-09: 驗證真實性(零 skip 鐵律)

- **dimension:** `test_assertion_quality`

Verbatim from SPEC.md §4 NFR-09:
- **任何 FR / NFR 的驗證測試不得是 `pytest.skip` / `pytest.mark.skip` / `skipif` / `xfail` / 無斷言的 stub**
- `pytest 03-development/tests -q` 的輸出中 **skipped 計數必須為 0**
- 每個測試函式至少一個 `assert`(`ast-assertions` 量測 `zero_assert == 0`)
- **反造假條款**:不得以 `--ignore` / `-k` / `--deselect` / `collect_ignore` / 從 `testpaths` 移除目錄等方式排除測試來達成上述數字
- `TRACEABILITY_MATRIX.md` 的 `VERIFIED` 標記,只能在該需求的驗證測試**實際執行並通過**時給出;測試若不存在或未執行,狀態必須是 `NOT_VERIFIED`

**DERIVED: SPEC.md §4 NFR-09** — ACs split the verbatim zero-skip contract into a pytest summary check, an `ast-assertions` zero-assert check, a configuration-hygiene check, and a traceability-audit check; no new test-design rule is asserted.

**Acceptance criteria**:

- **AC-NFR-09.1** — `pytest 03-development/tests -q` exits 0 and its summary line reports 0 skipped tests (SPEC.md §8 #1).
- **AC-NFR-09.2** — `ast-assertions` (or equivalent) reports `zero_assert == 0` test functions in the suite.
- **AC-NFR-09.3** — `pytest --ignore`, `-k`, `--deselect`, `collect_ignore`, and `testpaths` exclusion are not used (in source control history or in current configuration) to manipulate the skip count.
- **AC-NFR-09.4** — `TRACEABILITY_MATRIX.md` marks `VERIFIED` only for requirements whose verification test actually ran and passed; an audit reconciles every `VERIFIED` row against the test runner's machine-decidable record.

### NFR-10: 整合覆蓋

- **dimension:** `integration_coverage`

Verbatim from SPEC.md §4 NFR-10:
- `03-development/tests/integration/` 的跨模組整合測試,行覆蓋 **≥ 80%**
- 整合測試必須經由 CLI 入口(`python -m taskq_plus`)或 `click.testing.CliRunner` 驅動,不得直接呼叫內部函式
- 至少涵蓋:submit→run→status 全鏈、DAG 多層執行、breaker 開闔、cache 命中、plugin hook 觸發、export 三格式

**Acceptance criteria**:

- **AC-NFR-10.1** — running the integration suite with `pytest ... --cov=03-development/src --cov-report=term` reports a `TOTAL` line ≥ 80% for the integration subset (SPEC.md §8 #3).
- **AC-NFR-10.2** — integration tests are driven through `python -m taskq_plus` or `click.testing.CliRunner`; no integration test imports an internal function/module directly.
- **AC-NFR-10.3** — coverage spans at least: submit→run→status; multi-layer DAG; breaker open/close; cache hit; plugin hook fires; export in json/csv/md.

### NFR-11: 可讀性

- **dimension:** `readability`

Verbatim from SPEC.md §4 NFR-11:
- 專案 MI(LLOC 加權)**≥ 80**
- 單一函式 cyclomatic complexity **≤ 10**
- 單一檔案 ≤ 400 行;單一目錄 ≤ 15 檔

**DERIVED: SPEC.md §4 NFR-11** — ACs split the verbatim readability contract into MI, cyclomatic-complexity, file-length, and dir-count checks; the threshold value "≤ 10" / "≤ 400" / "≤ 15" reproduces the canonical numbers without further derivation.

**Acceptance criteria**:

- **AC-NFR-11.1** — `readability-v2` (or equivalent) reports a project MI ≥ 80 (LLOC-weighted).
- **AC-NFR-11.2** — no function in `03-development/src/taskq_plus/` has cyclomatic complexity > 10.
- **AC-NFR-11.3** — no source file exceeds 400 lines; no source directory exceeds 15 files.

### NFR-12: 系統驗證目標

- **dimension:** `execute_verification_target`

Verbatim from SPEC.md §4 NFR-12:
- `Makefile` 必須提供 `verify-system` target,串接:全套測試 + CLI 冒煙(submit / run / status / graph / export / clear)
- `make verify-system` 必須 **exit 0** 並在 stdout 印出 `verify-system: PASS`

**Acceptance criteria**:

- **AC-NFR-12.1** — `Makefile` has a `verify-system` target that runs the full test suite plus CLI smoke (`submit`, `run`, `status`, `graph`, `export`, `clear`).
- **AC-NFR-12.2** — `make verify-system` exits 0 and prints the literal string `verify-system: PASS` on stdout (SPEC.md §8 #21).

---

## 5. Acceptance Criteria Summary

The 22 acceptance items from SPEC.md §8 are reproduced below as a single machine-decidable index. Each item is its own SPEC check; per-FR/NFR ACs above cite these IDs.

| # | Command | Expected output | Cites |
|---|---------|-----------------|-------|
| 1 | `pytest 03-development/tests -q` | all-green; `skipped` count = 0 | NFR-09 |
| 2 | `pytest 03-development/tests --cov=03-development/src --cov-report=term` | TOTAL 100% | (line coverage contract) |
| 3 | `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term` | TOTAL ≥ 80% | NFR-10 |
| 4 | `python -m taskq_plus submit "echo hi"` | stdout 8-hex id; exit 0 | FR-01 |
| 5 | `python -m taskq_plus submit ""` | exit 2 | FR-01 |
| 6 | `python -m taskq_plus submit "echo hi; rm x"` | exit 2 (injection char) | FR-01 / NFR-02 |
| 7 | `TASKQ_TASK_TIMEOUT=1 python -m taskq_plus run <sleep-5-id>` | status `timeout`; exit 4 | FR-02 |
| 8 | 3 consecutive final failures, then `python -m taskq_plus run <id>` | exit 3; recovers after cooldown | FR-03 |
| 9 | Within TTL, `python -m taskq_plus run <id> --cached` | prints `cached: true`; no subprocess | FR-04 |
| 10 | After `python -m taskq_plus submit "echo b" --after <a>`, then `run --all` | b runs after a; b=`blocked` if a not done | FR-06 |
| 11 | Build A→B→A dependency | exit 5; stderr cycle path | FR-06 |
| 12 | `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` | exit 6 (path form rejected) | FR-07 / NFR-02 |
| 13 | plugin `pre_run` raises, then `run <id>` | task completes; `audit.jsonl` has `plugin_error` | FR-07 |
| 14 | `python -m taskq_plus export --format json` / `csv` / `md` | row counts equal; CSV escapes commas/quotes | FR-08 |
| 15 | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` | 0 hits | NFR-02 |
| 16 | `grep -c "^TASKQ_" .env.example` | 12 | (env-var completeness) |
| 17 | `lint-imports` | exit 0 | NFR-06 |
| 18 | `pip-licenses --format=json` | every license ∈ {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0} | NFR-07 |
| 19 | `bandit -r 03-development/src/` | 0 HIGH, 0 MEDIUM | NFR-02 |
| 20 | `mutmut run` then `mutmut results` | mutation score ≥ 70 | NFR-08 |
| 21 | `make verify-system` | exit 0; stdout `verify-system: PASS` | NFR-12 |
| 22 | After running a command with a secret, `grep -c "sk-" $TASKQ_HOME/audit.jsonl` | 0 | NFR-04 |

Exit code map (SPEC.md §3 / §7) — referenced by every FR's exit-coded AC:

| Code | Meaning |
|------|---------|
| 0 | success |
| 2 | input validation error (incl. unknown task id / unknown dependency) |
| 3 | breaker OPEN |
| 4 | task timeout (single-task mode only) |
| 5 | dependency cycle or depth cap exceeded |
| 6 | plugin load failure |
| 1 | other internal error |

Env-var contract (SPEC.md §5.1) — these bind the FRs that read each variable:

| Variable | Default | Purpose | Cited by |
|----------|---------|---------|----------|
| `TASKQ_HOME` | `.taskq` | data file directory | FR-01..FR-08 |
| `TASKQ_MAX_WORKERS` | `4` | `run --all` concurrent worker count | FR-02 |
| `TASKQ_TASK_TIMEOUT` | `10.0` | per-task subprocess timeout (seconds) | FR-02 |
| `TASKQ_RETRY_LIMIT` | `2` | retry cap on failed/timeout tasks | FR-03 |
| `TASKQ_BACKOFF_BASE` | `0.1` | exponential backoff base (seconds) | FR-03 |
| `TASKQ_BREAKER_THRESHOLD` | `3` | consecutive final failures before breaker OPEN | FR-03 |
| `TASKQ_BREAKER_COOLDOWN` | `5.0` | OPEN → HALF_OPEN cooldown (seconds) | FR-03 / NFR-03 |
| `TASKQ_CACHE_TTL` | `3600` | TTL for cached task results (seconds) | FR-04 |
| `TASKQ_MAX_DAG_DEPTH` | `32` | dependency chain depth cap | FR-06 |
| `TASKQ_PLUGINS` | (empty) | comma-separated plugin module allowlist | FR-07 |
| `TASKQ_AUDIT_LOG` | `$TASKQ_HOME/audit.jsonl` | audit trail path | FR-08 |
| `TASKQ_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | (logging only) |

Data-file contract (SPEC.md §5.2):

| File | Content | FR | Write mode |
|------|---------|----|----|
| `$TASKQ_HOME/tasks.json` | `{version:1, tasks:{id→fields incl. depends_on}}` | FR-01/02/06 | atomic |
| `$TASKQ_HOME/breaker.json` | `{version:1, state, failure_count, opened_at}` | FR-03 | atomic |
| `$TASKQ_HOME/cache.json` | `{version:1, entries:{sig→done result + cached_at}}` | FR-04 | atomic |
| `$TASKQ_AUDIT_LOG` | one JSON object per line | FR-08 | append + fsync |

---

## 6. Out-of-Scope

Explicitly excluded from this round (per SPEC.md §9 risk R10 and other boundary statements):

- **Audit log rotation** — SPEC.md §9 R10 records audit-growth as a known limitation; rotation is the operator's responsibility and is **not implemented this round**.
- **Real-time scheduling / cron — `taskq-plus` runs on explicit invocation only; no daemon, no scheduler.**
- **Distributed execution — single-host, single-process only. No remote worker, no message broker.**
- **Persistent UI / dashboard — CLI surface only; no web UI.**
- **Plugin auto-discovery — plugins are loaded only via `TASKQ_PLUGINS` allowlist; no filesystem scan, no entry-point discovery.**
- **Multi-user / multi-tenant isolation — `TASKQ_HOME` is single-writer-per-host.**
- **Cross-spec round 2 (`SPEC-2.md`) and round 3 (TypeScript) features — these are later test-bed rounds (per PROJECT_BRIEF.md §Stakeholders).**

---

## 7. Open Issues

Items deferred or requiring downstream resolution. These are **not** invented clauses; each one points to a verbatim canonical line whose interpretation boundary the harness must resolve.

- **`NFR-99` — Resolve NFR-01 measurement boundary (verbatim "不含 subprocess 執行", SPEC.md §4 NFR-01).** Canonical phrase is ambiguous between (a) bench-script-level exclusion of any `subprocess.run` call and (b) exclusion of OS-process-level fork/exec wall-clock variance; test harness to confirm with stakeholder. Until resolved, Agent A records the canonical phrase verbatim and tags it `DERIVED:` above AC-NFR-01.1 / AC-NFR-01.2; no additional prescriptive clause is added.
- **`NFR-99` — Resolve FR-07 "連續 3 次失敗" counter semantics (verbatim, SPEC.md §3 FR-07).** Canonical phrase is ambiguous between (a) counter resets on any successful hook call and (b) counter persists across the whole run lifetime; test harness to confirm. Agent A records verbatim and tags `DERIVED:` above AC-FR-07.4.
- **`NFR-99` — Resolve NFR-03 breaker-clock basis for `+1s` slack (verbatim "OPEN → CLOSED 恢復時間 ≤ TASKQ_BREAKER_COOLDOWN + 1s", SPEC.md §4 NFR-03).** Canonical phrase is ambiguous between monotonic and wall-clock; test harness to confirm.
- **`NFR-99` — SPEC.md §3 FR-04 "同簽名" — clarify whether `sha256(command)` operates on the canonical (post-`shlex.split`-normalised) command string or the raw user-supplied form.** Agent A does not modify FR-04's "verbatim" reading.
- **`NFR-99` — SPEC.md §3 FR-07 plugin load failure error message format ("plugin load failed: <name>: <reason>", SPEC.md §7 row) — confirm reason granularity with stakeholder** (e.g. allowlist-regex failure vs `ImportError` vs not-in-`TASKQ_PLUGINS`). No prescriptive format added.
- **`FR-XX-deferred` — Round 2 (`SPEC-2.md`) and Round 3 (TypeScript) features** are out of scope for this round per §6; they will be re-transcribed when their canonical specs are active.
- **`NFR-99` — `.env.example` completeness check (`grep -c "^TASKQ_" .env.example` = 12, SPEC.md §8 #16).** Agent A records the expected count; no prescriptive `.env.example` content is added by SRS.

---

## 8. Risks

Reproduced from SPEC.md §9 (verbatim table) — these inform Phase 3 test prioritisation but do not generate new FR/NFR clauses.

| ID | Risk | Impact | Likelihood | Mitigation |
|----|------|--------|-----------|------------|
| R1 | 並發寫入損壞 tasks.json | 高 | 中 | Lock + 原子寫 (NFR-03) |
| R2 | subprocess 懸掛/殭屍 | 中 | 中 | timeout (FR-02) |
| R3 | breaker 誤鎖死 | 中 | 低 | cooldown + HALF_OPEN (FR-03) |
| R4 | 快取回放陳舊結果 | 低 | 中 | TTL 過期重執行 (FR-04) |
| R5 | secret 落盤洩漏 | 高 | 中 | 寫入前 redaction (NFR-04) |
| R6 | **plugin 成為任意程式碼執行入口** | **高** | 中 | allowlist + 名稱正則 + 禁 eval/exec/路徑 (FR-07 / NFR-02) |
| R7 | 病態相依圖耗盡資源 | 中 | 低 | 循環偵測 + 深度上限 (FR-06) |
| R8 | plugin 例外中斷主流程 | 中 | 中 | 例外隔離 + 連續失敗停用 (FR-07) |
| R9 | 依賴引入不相容 license | 中 | 低 | 釘版 + allowlist + SBOM (NFR-07) |
| R10 | 稽核日誌無限成長 | 低 | 高 | append-only,輪替由使用者負責 — **本輪不實作輪替** (see §6) |

High-risk modules flagged in SPEC.md §10 for per-module TDD coverage:
- `taskq_plus.service.executor` (subprocess execution) — FR-02 / FR-03
- `taskq_plus.service.plugins` (dynamic loading) — FR-07 / NFR-02
- `taskq_plus.storage.task_store` (concurrent writes) — FR-01 / FR-02

---

## 9. Glossary

| Term | Definition | Source |
|------|------------|--------|
| `taskq-plus` | Local task-queue CLI; project name | SPEC.md §1 |
| `task id` | uuid4 first 8 hex characters, per submit | SPEC.md §3 FR-01 |
| `correlation_id` | Per-CLI-invocation identifier shared by every audit event emitted by that invocation | SPEC.md §3 FR-08 |
| `pending` / `running` / `done` / `failed` / `timeout` / `blocked` | Task state machine values; verbatim from SPEC.md §3 FR-02 | FR-02 |
| `CLOSED` / `HALF_OPEN` / `OPEN` | Circuit-breaker states | FR-03 |
| Plugin | A Python module exposing `pre_run(task)` and/or `post_run(task, result)`; loaded only via `TASKQ_PLUGINS` allowlist | FR-07 |
| Allowlist regex | `^[A-Za-z_][A-Za-z0-9_.]*$` for plugin module names | NFR-02 |
| Atomic write | `tmp` file in the same directory, `fsync`/`flush`, then `os.replace` | NFR-03 |
| `depends_on` | List of task ids a task awaits; tasks blocked by a non-`done` dependency | FR-06 |
| Kahn topological sort | Layered DAG ordering: same level (in-degree 0) may run concurrently | FR-06 |
| `$TASKQ_HOME` | Directory holding tasks/breaker/cache data files (default `.taskq`) | SPEC.md §5.1 |
| `$TASKQ_AUDIT_LOG` | JSON Lines audit log path (default `$TASKQ_HOME/audit.jsonl`) | SPEC.md §5.1 |
| Redaction | Replacement of lines matching `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` with `[REDACTED]`, performed before write | NFR-04 |
| SBOM | Software Bill of Materials; emitted to `08-config/SBOM.json` | NFR-07 |
| DERIVED (tag) | Annotation indicating Agent A's interpretation choice beyond verbatim canonical; required by R-CANONICAL-INTERP-001 to prevent false-positive over-spec scoring | (workflow rule) |

---

*End of SRS — taskq-plus v1.0.0.*
