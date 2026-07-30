# Software Requirements Specification (SRS) — taskq-plus

> Phase 1 deliverable. INGESTION MODE: 100% transcription of FR-01..FR-08 and
> NFR-01..NFR-12 from canonical `SPEC.md` (v1.0.0, 2026-07-30).
> Source of truth: `/Users/johnny/projects/taskq-plus/SPEC.md`.
> Project brief: `/Users/johnny/projects/taskq-plus/PROJECT_BRIEF.md`.

---

## 1. Introduction

### 1.1 Purpose
This SRS defines the requirements for `taskq-plus`, a local task-queue
command-line tool. The CLI accepts shell commands as tasks and runs them
under controlled concurrency, timeout, retry, circuit-breaker, TTL result
cache, and **dependency DAG ordering**; behaviour is extended through an
allowlisted **plugin hook** system; a **structured JSONL audit trail** is
emitted and results are exportable as json / csv / markdown.

### 1.2 Scope
- **In scope (this round, round 1 of 3)**:
  - Python 3.11 CLI entry `python -m taskq_plus`
  - `click` command groups (FR-05)
  - `pydantic` v2 validation models (FR-01)
  - `subprocess` execution with `shlex.split`, `shell=True` forbidden (FR-02, NFR-02)
  - `ThreadPoolExecutor` for `run --all` with shared `threading.Lock` over store (FR-02)
  - JSON-file persistence (4 data files) with atomic write (NFR-03)
  - Five-layer architecture enforced by `.importlinter` (NFR-06)
- **Out of scope**: see §6.

### 1.3 Project Context
Round 1 of a 3-round progressive test-bed for the harness-methodology
pipeline. Round 2 = `SPEC-2.md` (backend + DB); round 3 = TypeScript
(deferred). See PROJECT_BRIEF.md §"Why this project exists" for prior-gap
countermeasures embedded in the canonical spec.

### 1.4 Definitions, Acronyms, Abbreviations
See §9 Glossary.

### 1.5 References
- `SPEC.md` v1.0.0 (2026-07-30) — Single Source of Truth
- `PROJECT_BRIEF.md`
- `.env.example`, `.importlinter`, `requirements.txt`, `Makefile`,
  `.methodology/harness_config.json` (project-side carriers of NFRs;
  §5.3 of SPEC.md)

---

## 2. Constraints

| ID | Constraint | Source |
|----|-----------|--------|
| C-01 | Python 3.11; entry `python -m taskq_plus` | SPEC §1 |
| C-02 | `click` for CLI command groups; `pydantic` v2 for validation | SPEC §2 |
| C-03 | `subprocess` with `shlex.split(command)`; `shell=True` forbidden anywhere | FR-02, NFR-02 |
| C-04 | `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)` for `run --all`; shared `threading.Lock` over store writes | FR-02 |
| C-05 | Five-layer architecture `cli > observability > service > storage > models`; `config` independent; enforced by `.importlinter` layers contract | NFR-06 |
| C-06 | Runtime dependencies pinned with `==` in `requirements.txt`; allowed licenses: MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0 | NFR-07 |
| C-07 | All four data files (`tasks.json`, `breaker.json`, `cache.json`, `audit.jsonl`) written atomically (tmp + `os.replace`; audit append + fsync) | NFR-03 |
| C-08 | Injection character blacklist on `submit`: `; \| & $ > < \``  | NFR-02 |
| C-09 | Plugin name allowlist regex `^[A-Za-z_][A-Za-z0-9_.]*$`; no `eval`, no `exec`, no path/URL loading | FR-07, NFR-02 |
| C-10 | DAG cycle rejection at submit time (exit 5) with cycle path printed; chain depth capped by `TASKQ_MAX_DAG_DEPTH` | FR-06 |
| C-11 | Plugin exceptions must NOT abort task execution; record `plugin_error` audit event; disable plugin after 3 consecutive failures within one run | FR-07 |
| C-12 | `pytest -q` must report **0 skipped**; `--ignore` / `-k` / `--deselect` / `collect_ignore` excluded to reach that number is forbidden; `TRACEABILITY_MATRIX.md` `VERIFIED` only when test actually ran and passed | NFR-09 |
| C-13 | SBOM emitted to `08-config/SBOM.json` listing each dependency's `name` / `version` / `license` | NFR-07 |
| C-14 | `Makefile` `verify-system` target must exit 0 and print `verify-system: PASS` | NFR-12 |

---

## 3. Functional Requirements

> Each AC quotes the verbatim canonical phrase from SPEC.md where
> possible (per INGESTION MODE). DERIVED markers flag interpretive
> clauses.

### FR-01: 任務提交與驗證

**Canonical citation**: SPEC.md §3 FR-01.

`taskq-plus submit "<command>" [--name NAME] [--after ID]...`

The submitted fields are validated by the **`pydantic` model
`TaskSubmission`**; any violation → **exit 2** + stderr error message,
no write to storage.

| Rule | Condition |
|------|-----------|
| non-empty | command empty or all whitespace → reject |
| length | command > 1000 characters → reject |
| injection characters | command contains `;` `|` `&` `$` `>` `<` `` ` `` any → reject (NFR-02) |
| name unique | `--name` duplicates existing pending/running task → reject |
| dependency exists | `--after` points to non-existent task id → reject |

On validation pass:

- Generate task id (uuid4 first 8 hex)
- Status `pending`, record `command`, `name`, `created_at`, `depends_on` (list[str])
- Atomic write to `$TASKQ_HOME/tasks.json`
- stdout outputs task id (with `--json` outputs `{"id": ..., "status": "pending"}`)
- Write a `submit` audit event (FR-08)

**Acceptance Criteria**
- **AC-FR-01.a** `python -m taskq_plus submit "echo hi"` — stdout is 8-hex id, exit 0. (SPEC §8 #4)
- **AC-FR-01.b** `python -m taskq_plus submit ""` — exit 2. (SPEC §8 #5)
- **AC-FR-01.c** `python -m taskq_plus submit "echo hi; rm x"` — exit 2 (injection character). (SPEC §8 #6)
- DERIVED: SPEC §4 NFR-02 "FR-01 injection character blacklist must have test coverage (one case per character)" — rationale: one test per listed injection char to satisfy the "one case per character" clause verbatim.
- **AC-FR-01.d** A test exists per blacklisted injection character (`; | & $ > < \``) asserting exit 2. (NFR-02)

---

### FR-02: 任務執行器

**Canonical citation**: SPEC.md §3 FR-02.

`taskq-plus run <id>` or `taskq-plus run --all`

- Execute with `subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=TASKQ_TASK_TIMEOUT)`; **no path may use `shell=True`**
- State machine: `pending → running → done | failed | timeout | blocked`
  - exit 0 → `done`; non-zero → `failed`; `TimeoutExpired` → `timeout`
  - dependency not satisfied → `blocked` (FR-06)
- Result fields: `exit_code`, `stdout_tail` (last 2000 chars), `stderr_tail` (last 2000 chars), `duration_ms`, `finished_at`
- `--all`: use `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)` to run all executable `pending` tasks concurrently in **DAG topological order** (FR-06); store writes must be thread-safe (shared Lock)
- In single-task mode, `timeout` result → **exit 4**

**Acceptance Criteria**
- **AC-FR-02.a** `TASKQ_TASK_TIMEOUT=1 python -m taskq_plus run <sleep-5-id>` — status `timeout`, exit 4. (SPEC §8 #7)
- DERIVED: SPEC §3 FR-02 — "exit 0 → `done`; 非 0 → `failed`" — rationale: state-machine mapping is verbatim from the canonical FR-02 state list; restating the rule as a testable AC.
- **AC-FR-02.b** Successful task (exit 0) → `done`; non-zero exit code → `failed`.
- DERIVED: SPEC §3 FR-02 — "--all: 用 ThreadPoolExecutor ... DAG 拓撲順序 (FR-06) 並發執行 ... 存儲寫入必須執行緒安全 (共享 Lock)" — rationale: restating the threading contract as a testable AC.
- **AC-FR-02.c** `run --all` runs DAG-topologically-ordered tasks via `ThreadPoolExecutor`, sharing a `threading.Lock` over the store.
- DERIVED: SPEC §3 FR-02 — "結果欄位:`exit_code`、`stdout_tail`(末 2000 字元)、`stderr_tail`(末 2000 字元)、`duration_ms`、`finished_at`" — rationale: the "last 2000 chars" bound is canonical; restating as a length invariant AC.
- **AC-FR-02.d** `stdout_tail` / `stderr_tail` are bounded to last 2000 chars.
- **AC-FR-02.e** `grep -rn "shell=True" 03-development/src/` returns **0 hits** (NFR-02 / SPEC §8 #15).

---

### FR-03: 重試與斷路器

**Canonical citation**: SPEC.md §3 FR-03.

**Retry**: when `run` result is `failed`/`timeout`, auto-retry up to
`TASKQ_RETRY_LIMIT` times; before the n-th retry wait
`TASKQ_BACKOFF_BASE × 2^n` seconds (exponential backoff; the sleep
function must be injectable for testability).

**Circuit breaker** (global, cross-task, cross-process):

- Consecutive final failures (retries exhausted, still failed/timeout) ≥ `TASKQ_BREAKER_THRESHOLD` → `OPEN`
- During `OPEN` any `run` is rejected immediately: **exit 3** + stderr `breaker open`, no subprocess executed
- After `TASKQ_BREAKER_COOLDOWN` seconds → `HALF_OPEN`: admit one task — success → `CLOSED` and counter reset; failure → `OPEN` again
- State persisted in `$TASKQ_HOME/breaker.json` (atomic write)

**Acceptance Criteria**
- **AC-FR-03.a** 3 consecutive final failures → subsequent `python -m taskq_plus run <id>` returns exit 3; after cooldown resumes execution. (SPEC §8 #8)
- DERIVED: SPEC §3 FR-03 — "第 n 次重試前等待 TASKQ_BACKOFF_BASE × 2^n 秒(exponential backoff;sleep 函式必須可注入以利測試)" — rationale: restating the canonical injectability requirement as a testable AC.
- **AC-FR-03.b** Retry uses `TASKQ_BACKOFF_BASE × 2^n` exponential backoff; sleep is injectable (verified via deterministic test).
- DERIVED: SPEC §3 FR-03 — state machine transitions across CLOSED / OPEN / HALF_OPEN — rationale: state names and transitions are verbatim from the canonical FR-03 specification.
- **AC-FR-03.c** Breaker state transitions follow `CLOSED → OPEN → HALF_OPEN → CLOSED` per SPEC.
- DERIVED: SPEC §4 NFR-03 — "breaker OPEN → CLOSED 恢復時間 ≤ TASKQ_BREAKER_COOLDOWN + 1s" — rationale: restating the canonical recovery-time bound as a testable AC (overlaps with AC-NFR-03.c).
- **AC-FR-03.d** `OPEN → CLOSED` recovery time ≤ `TASKQ_BREAKER_COOLDOWN` + 1s (NFR-03).

---

### FR-04: 結果 TTL 快取

**Canonical citation**: SPEC.md §3 FR-04.

- Cache signature = `sha256(command)`
- `taskq-plus run <id> --cached`: same signature and result `done` within `TASKQ_CACHE_TTL` seconds → directly replay (`exit_code` / `stdout_tail`), **do not execute subprocess**, task marked `done` and `cached: true`
- Cache miss or expired → normal execution; on success (`done`) write to `$TASKQ_HOME/cache.json`
- Cache read/write: atomic + thread-safe (coexists with FR-02 concurrency)

**Acceptance Criteria**
- **AC-FR-04.a** Within TTL `python -m taskq_plus run <id> --cached` — output `cached: true`, no subprocess executed. (SPEC §8 #9)
- DERIVED: SPEC §3 FR-04 — "快取過期或不存在 → 正常執行,成功(`done`)後寫入 $TASKQ_HOME/cache.json" — rationale: restating the canonical miss-path as a testable AC.
- **AC-FR-04.b** Expired cache entry → normal execution, then write `done` result into `$TASKQ_HOME/cache.json`.
- DERIVED: SPEC §3 FR-04 — "快取簽名 = sha256(command)" — rationale: the canonical signature formula is verbatim; restating as an invariant AC.
- **AC-FR-04.c** Cache key is `sha256(command)`.
- DERIVED: SPEC §3 FR-04 — "快取讀寫:原子 + 執行緒安全(與 FR-02 並發共存)" — rationale: restating the canonical cache-read/write clause as a testable AC; the canonical does not specify lock sharing with the FR-02 lock — measurement / interpretation boundary is owned by the test harness per canonical line.
- **AC-FR-04.d** Cache read/write are atomic and thread-safe while coexisting with FR-02 concurrency.

---

### FR-05: CLI 整合

**Canonical citation**: SPEC.md §3 FR-05.

`click` command-grouped subcommands (entry `python -m taskq_plus`):

| Command | Behaviour |
|---------|-----------|
| `submit "<cmd>" [--name N] [--after ID]...` | FR-01 |
| `run <id> [--cached]` / `run --all` | FR-02/03/04/06 |
| `status <id>` | output full fields of that task |
| `list [--status S]` | list tasks (optionally filter by status) |
| `graph [--format text\|dot]` | output dependency graph (FR-06) |
| `plugins list` | list loaded plugins and their hooks (FR-07) |
| `export --format json\|csv\|md` | export task results (FR-08) |
| `clear` | clear all data files in `$TASKQ_HOME` |

- Global flag `--json`: machine-readable output (single-line JSON)
- **Exit codes**: `0` success / `2` input validation error (incl. unknown task id) / `3` breaker open / `4` task timeout / `5` dependency graph has cycle or depth exceeded / `6` plugin load failure / `1` other internal error

**Acceptance Criteria**
- DERIVED: SPEC §3 FR-05 — subcommand table — rationale: enumerating the canonical subcommand list as testable reachability ACs.
- **AC-FR-05.a** Each subcommand listed above is wired through `click` and reachable via `python -m taskq_plus`.
- DERIVED: SPEC §3 FR-05 — "全域 flag --json:機器可讀輸出(單行 JSON)" — rationale: restating the canonical JSON-output requirement.
- **AC-FR-05.b** `--json` outputs single-line JSON.
- DERIVED: SPEC §3 FR-05 — "Exit codes: 0 成功 / 2 輸入驗證錯誤(...) / 3 breaker open / 4 任務 timeout / 5 相依圖存在循環或深度超限 / 6 plugin 載入失敗 / 1 其他內部錯誤" — rationale: restating the canonical exit-code map as a cross-reference AC.
- **AC-FR-05.c** Exit codes per SPEC §3 / §7 (also see §5 of this SRS for the exit code map).
- **AC-FR-05.d** `python -m taskq_plus export --format json` / `csv` / `md` produces three formats with identical task counts (SPEC §8 #14).

---

### FR-06: 任務相依 DAG

**Canonical citation**: SPEC.md §3 FR-06.

- `submit --after <id>` repeatable, establishes `depends_on` edges
- `run --all` uses **Kahn topological sort** to determine execution order; tasks at the same layer (in-degree 0) may run concurrently
- Dependency task result not `done` → downstream task marked `blocked`, **not executed**, and not counted toward breaker failure count
- **Cycle detection**: `submit --after` that would create a cycle → reject that submission, **exit 5** + stderr listing the cycle path (`A → B → C → A`)
- **Depth cap**: dependency chain depth > `TASKQ_MAX_DAG_DEPTH` → reject, exit 5 (prevent pathological input exhausting resources)
- `graph --format dot` outputs Graphviz DOT; `--format text` outputs indented tree

**Acceptance Criteria**
- **AC-FR-06.a** `python -m taskq_plus submit "echo b" --after <a>` then `run --all` — b runs after a; a not `done` → b is `blocked`. (SPEC §8 #10)
- **AC-FR-06.b** Build A→B→A dependency → exit 5, stderr contains cycle path. (SPEC §8 #11)
- **AC-FR-06.c** Dependency chain depth exceeding `TASKQ_MAX_DAG_DEPTH` → exit 5 with stderr `dependency chain too deep: <n> > <max>`. (SPEC §7)
- DERIVED: SPEC §3 FR-06 — "run --all 以 Kahn 拓撲排序決定執行順序;同一層(入度為 0)的任務才可並發" — rationale: restating the canonical same-layer concurrency rule as a testable AC.
- **AC-FR-06.d** Kahn topological sort emits tasks layer-by-layer; same-layer tasks may be scheduled concurrently.

---

### FR-07: Plugin Hook 系統

**Canonical citation**: SPEC.md §3 FR-07.

- A plugin is a Python module providing `pre_run(task) -> None` and/or `post_run(task, result) -> None`
- Load source: **only** the module names listed in the `TASKQ_PLUGINS` environment variable (comma-separated **allowlist**), loaded by name via `importlib.import_module`
- **Security iron rules** (NFR-02):
  - forbid `eval` / `exec` / `__import__` of dynamic strings
  - forbid loading from file path or URL (only installed module names accepted)
  - Plugin module name must match `^[A-Za-z_][A-Za-z0-9_.]*$`; otherwise → reject load, **exit 6**
- Plugin raises exception → **must not** interrupt task execution: log a `plugin_error` audit event (FR-08) and continue; a plugin failing 3 consecutive times within one run is disabled
- `plugins list` outputs each plugin's module name, registered hooks, load status

**Acceptance Criteria**
- **AC-FR-07.a** `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` — exit 6 (path form rejected). (SPEC §8 #12)
- **AC-FR-07.b** Plugin `pre_run` raises → task still completes; `audit.jsonl` contains a `plugin_error` event. (SPEC §8 #13)
- DERIVED: SPEC §3 FR-07 — "連續 3 次失敗的 plugin 於本次執行內停用" — rationale: restating the canonical 3-strikes-disable clause as a testable AC.
- **AC-FR-07.c** A plugin failing 3 consecutive times within one run is disabled for that run.
- DERIVED: SPEC §3 FR-07 — "plugins list 輸出每個 plugin 的模組名、註冊的 hook、載入狀態" — rationale: restating the canonical `plugins list` output requirement as a testable AC.
- **AC-FR-07.d** `plugins list` prints each plugin's module name, hooks, and load status.
- **AC-FR-07.e** `grep -rn "eval(\|exec(" 03-development/src/` returns **0 hits** (NFR-02 / SPEC §8 #15).

---

### FR-08: 結構化稽核日誌與匯出

**Canonical citation**: SPEC.md §3 FR-08.

**Audit log**:

- Path `$TASKQ_AUDIT_LOG` (default `$TASKQ_HOME/audit.jsonl`), **JSON Lines**, append-only
- Each entry fields: `ts` (ISO-8601 UTC), `event`, `task_id`, `correlation_id`, `detail`
- `correlation_id` is generated per CLI invocation; all events triggered by that invocation share the same value
- Event types: `submit` / `run_start` / `run_end` / `retry` / `breaker_open` / `breaker_close` / `cache_hit` / `blocked` / `plugin_error`
- NFR-04 redaction applied before write

**Export**:

- `export --format json`: single JSON array, fields match `status`
- `export --format csv`: header row + one row per task; fields with commas/quotes properly escaped
- `export --format md`: Markdown table
- Task count and field set must be consistent across the three formats (asserted by tests)

**Acceptance Criteria**
- DERIVED: SPEC §3 FR-08 — "每筆欄位:`ts`(ISO-8601 UTC)、`event`、`task_id`、`correlation_id`、`detail`" — rationale: restating the canonical field set as a testable AC.
- **AC-FR-08.a** Audit entry written with `ts`, `event`, `task_id`, `correlation_id`, `detail` fields per SPEC.
- DERIVED: SPEC §3 FR-08 — "`correlation_id` 由一次 CLI 呼叫產生,該次呼叫觸發的所有事件共用同一個值" — rationale: restating the canonical correlation-id lifetime rule as a testable AC.
- **AC-FR-08.b** Single CLI invocation shares one `correlation_id` across all triggered events.
- **AC-FR-08.c** Three export formats produce identical task count and identical field set; CSV commas/quotes escaped. (SPEC §8 #14)
- **AC-FR-08.d** Audit log writes go through NFR-04 redaction (no secret on disk — SPEC §8 #22).

---

## 4. Non-Functional Requirements

### NFR-01: 效能預算

**Canonical citation**: SPEC.md §4 NFR-01.
**dimension**: `performance` (real key in `DIMENSION_TOOLS["python"]`).

- `submit` + `status` combined operation (excluding subprocess execution) 100 iterations, **p95 < 50ms**
- `run --all` topological-sort phase (excluding subprocess execution) for 200 tasks, **p95 < 200ms**
- Measurement method: `pytest-benchmark`, results written to benchmark JSON

**Acceptance Criteria**
- DERIVED: SPEC §4 NFR-01 — "submit + status 組合操作(不含 subprocess 執行)100 次 p95 < 50ms" — rationale: restating the verbatim canonical budget; measurement / interpretation boundary is owned by the test harness per canonical line.
- **AC-NFR-01.a** `submit`+`status` p95 over 100 iterations < 50ms (measured by pytest-benchmark).
- DERIVED: SPEC §4 NFR-01 — "run --all 對 200 個任務的拓撲排序階段(不含 subprocess 執行)p95 < 200ms" — rationale: restating the verbatim canonical budget; measurement / interpretation boundary is owned by the test harness per canonical line.
- **AC-NFR-01.b** Topological sort p95 over 200 tasks < 200ms (measured by pytest-benchmark).

---

### NFR-02: 執行與載入安全

**Canonical citation**: SPEC.md §4 NFR-02.
**dimension**: `security`.

- Entire codebase **forbids `shell=True`** (verified by grep, 0 hits)
- FR-01 injection character blacklist must have test coverage (one case per character)
- **Plugin loading surface** (FR-07): entire codebase forbids `eval(` / `exec(` / `__import__(`; plugin name must pass `^[A-Za-z_][A-Za-z0-9_.]*$` allowlist regex; file path or URL must NOT be accepted
- `bandit -r 03-development/src/` result: **0 HIGH, 0 MEDIUM**

**Acceptance Criteria**
- **AC-NFR-02.a** `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` returns **0 hits**. (SPEC §8 #15)
- DERIVED: SPEC §4 NFR-02 — "FR-01 注入字元黑名單必須有測試覆蓋(每個字元一個 case)" — rationale: restating the canonical "one case per character" rule as a testable AC.
- **AC-NFR-02.b** Each blacklisted character (`; | & $ > < \``) has a test case asserting exit 2.
- **AC-NFR-02.c** Plugin name `../evil.py` → rejected with exit 6. (SPEC §8 #12)
- **AC-NFR-02.d** `bandit -r 03-development/src/` reports 0 HIGH and 0 MEDIUM. (SPEC §8 #19)

---

### NFR-03: 錯誤處理與原子性

**Canonical citation**: SPEC.md §4 NFR-03.
**dimension**: `error_handling`.

- Four data files (`tasks.json` / `breaker.json` / `cache.json` / `audit.jsonl`) all atomic-write (tmp + `os.replace`; audit is append + fsync); after process interruption files remain valid JSON / JSONL
- `tasks.json` corrupted (invalid JSON) detected at startup → **exit 1** + stderr `store corrupted` (**no** silent rebuild) (SPEC §7 error table)
- **Must not** contain bare `except:`, `except Exception: pass`, swallowing `KeyboardInterrupt` / `SystemExit`
- Every `except` block must be one of: re-raise, translate to a clear domain exception, or record then exit with a definite code
- Breaker `OPEN → CLOSED` recovery time ≤ `TASKQ_BREAKER_COOLDOWN` + 1s

**Acceptance Criteria**
- DERIVED: SPEC §4 NFR-03 — "四個資料檔(...)/...全部原子寫(tmp + os.replace;...)/...進程中斷後檔案仍為合法 JSON / JSONL" — rationale: restating the canonical crash-safety invariant as a testable AC.
- **AC-NFR-03.a** Mid-write interruption test: after kill -9 the file remains parseable as valid JSON / JSONL.
- DERIVED: SPEC §4 NFR-03 — "不得出現裸 except:、except Exception: pass、吞掉 KeyboardInterrupt/SystemExit" — rationale: restating the canonical no-swallowing rule as a testable AC.
- **AC-NFR-03.b** Static check: codebase contains no bare `except:`, no `except Exception: pass`, no swallowing of `KeyboardInterrupt` / `SystemExit`.
- DERIVED: SPEC §4 NFR-03 — "breaker OPEN → CLOSED 恢復時間 ≤ TASKQ_BREAKER_COOLDOWN + 1s" — rationale: verbatim canonical bound; restating as a testable AC.
- **AC-NFR-03.c** Breaker `OPEN → CLOSED` recovery time ≤ `TASKQ_BREAKER_COOLDOWN` + 1s.
- DERIVED: SPEC §7 error table — "`tasks.json` 損壞(非法 JSON) | 啟動偵測 → exit 1,stderr `store corrupted`(**不**靜默重建)" — rationale: restating the verbatim canonical startup-corruption behavior as a testable AC.
- **AC-NFR-03.d** Corrupted `tasks.json` (invalid JSON) at startup → exit 1, stderr contains `store corrupted`; no silent rebuild.

---

### NFR-04: 敏感資料遮蔽

**Canonical citation**: SPEC.md §4 NFR-04.
**dimension**: `security`.

- Before writing to disk `stdout_tail` / `stderr_tail` / audit-log `detail`, lines matching `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` are wholly replaced by `[REDACTED]`
- Redaction happens **before write**, not after read (asserted by "file content contains no plaintext secret")

**Acceptance Criteria**
- **AC-NFR-04.a** Run a command containing a secret then `grep -c "sk-" $TASKQ_HOME/audit.jsonl` → **0**. (SPEC §8 #22)
- DERIVED: SPEC §4 NFR-04 — "遮蔽發生在寫入前,不是讀取後(以「檔案內容不含明文 secret」斷言)" — rationale: restating the canonical "before write, not after read" rule as a testable AC.
- **AC-NFR-04.b** Redaction runs before the disk write (test asserts on file contents, not on post-load string).

---

### NFR-05: 文件覆蓋

**Canonical citation**: SPEC.md §4 NFR-05.
**dimension**: `documentation`.

- All public functions/classes under `03-development/src/taskq_plus` carry a docstring with `[FR-XX]` or `[NFR-XX]` reference
- Coverage **100%** (measured by `ast-docstrings`)

**Acceptance Criteria**
- DERIVED: SPEC §4 NFR-05 — "03-development/src/taskq_plus 全部公開函式/類別有 docstring 且含 [FR-XX] 或 [NFR-XX] 引用 ... 覆蓋率 100%(ast-docstrings 量測)" — rationale: restating the canonical docstring-coverage rule as a testable AC.
- **AC-NFR-05.a** `ast-docstrings` measurement reports 100% coverage of public symbols with `[FR-XX]` / `[NFR-XX]` tags.

---

### NFR-06: 架構分層契約

**Canonical citation**: SPEC.md §4 NFR-06.
**dimension**: `architecture_constraints`.

- Project root **must contain `.importlinter`** declaring the layers contract:

  ```
  cli > observability > service > storage > models
  ```

  Upper layers may import lower layers; **lower layers must not import upper layers**; `config` is an independence module — any layer may import it, but it must not import any layer
- `lint-imports` must **exit 0**
- **Forbidden** to pass by deleting `.importlinter`, by widening the contract to a wildcard `ignore_imports`, or by degrading it to a single `forbidden` entry
- Prior-round gap note: `harness/tool_runners.py:69-72` returns exit 0 when `.importlinter` is absent → that dimension (Gate 1 weight **0.25**) becomes unconditional full marks. This clause exists solely to make that weight actually execute.

**Acceptance Criteria**
- DERIVED: SPEC §4 NFR-06 — "專案根目錄必須存在 .importlinter,宣告 layers contract ... lint-imports 必須 exit 0" — rationale: restating the canonical layering requirement as a testable AC.
- **AC-NFR-06.a** `.importlinter` exists at project root and declares the 5-layer contract verbatim.
- **AC-NFR-06.b** `lint-imports` exits **0**. (SPEC §8 #17)
- DERIVED: SPEC §4 NFR-06 — "禁止以刪除 .importlinter、把 contract 放寬成萬用字元 ignore_imports、或降級為單條 forbidden 的方式取得通過" — rationale: restating the canonical anti-weakening rule as a testable AC.
- **AC-NFR-06.c** Code review confirms the contract is not weakened by wildcard `ignore_imports` or single-`forbidden` substitution.

---

### NFR-07: 依賴與授權合規

**Canonical citation**: SPEC.md §4 NFR-07.
**dimension**: `license_compliance`.

- All runtime dependencies pinned with `==` in `requirements.txt` (no `>=` / `~=` / unversioned)
- Allowed licenses: **MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0**; other license → that dependency must not be used
- **Scan scope must include the installed dependency tree**, not only own source code. Acceptable evidence commands (pick one):
  - `pip-licenses --format=json --with-urls`
  - `scancode --license <venv>/lib/python3.11/site-packages --json-pp -`
- Produce SBOM at `08-config/SBOM.json` listing each dependency's `name` / `version` / `license`
- Prior-round gap note: taskq had zero runtime dependencies; the dimension's evidence was literally "19 source files scanned" — that scans own src, always 100 with no signal.

**Acceptance Criteria**
- **AC-NFR-07.a** `pip-licenses --format=json` returns each dependency's license ∈ {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0}. (SPEC §8 #18)
- DERIVED: SPEC §4 NFR-07 — "全部 runtime 依賴在 requirements.txt 以 == 釘版" — rationale: restating the canonical pinning rule as a testable AC.
- **AC-NFR-07.b** `requirements.txt` pins every runtime dependency with `==`.
- DERIVED: SPEC §4 NFR-07 — "產出 SBOM 於 08-config/SBOM.json,列出每個依賴的 name / version / license" — rationale: restating the canonical SBOM-output requirement as a testable AC.
- **AC-NFR-07.c** `08-config/SBOM.json` lists each dependency with `name`, `version`, `license`.

---

### NFR-08: 變異測試

**Canonical citation**: SPEC.md §4 NFR-08.
**dimension**: `mutation_testing`.

- `.methodology/harness_config.json` must set `features.mutation_testing: true`
- **mutation score ≥ 70**
- Scope limited to `03-development/src/taskq_plus/service/` and `.../storage/` (core logic), with the limitation rationale annotated in `harness_config.json` (execution-time budget)
- Prior-round gap note: the flag defaulted `False` (`core/harness_config.py:56`), taskq did not override → Gate 2/3/4 `mutation_testing` was all `null`, weight 0.08 renormalised away.

**Acceptance Criteria**
- **AC-NFR-08.a** `mutmut run` followed by `mutmut results` reports mutation score **≥ 70**. (SPEC §8 #20)
- DERIVED: SPEC §4 NFR-08 — ".methodology/harness_config.json 必須設 features.mutation_testing: true" — rationale: restating the canonical config-flag requirement as a testable AC.
- **AC-NFR-08.b** `harness_config.json` has `features.mutation_testing: true`.

---

### NFR-09: 驗證真實性（零 skip 鐵律）

**Canonical citation**: SPEC.md §4 NFR-09.
**dimension**: `test_assertion_quality`.

- **No FR / NFR verification test may be `pytest.skip` / `pytest.mark.skip` / `skipif` / `xfail` / an assertion-free stub**
- `pytest 03-development/tests -q` output must report **0 skipped**
- Each test function has at least one `assert` (measured by `ast-assertions` `zero_assert == 0`)
- **Anti-fabrication clause**: must not exclude tests via `--ignore` / `-k` / `--deselect` / `collect_ignore` / removing directories from `testpaths` to reach the above numbers
- `TRACEABILITY_MATRIX.md` `VERIFIED` may only be marked when the requirement's verification test **actually ran and passed**; if the test does not exist or did not run, status must be `NOT_VERIFIED`
- Prior-round gap note: taskq's NFR-07~10 dedicated tests **15/16 were `pytest.skip("... deferred to P5")` stubs**, while P5 had long finished and Gate 3/4 were all green; `TRACEABILITY_MATRIX.md` still marked all four as `VERIFIED`. This clause is the only mechanism preventing a repeat.

**Acceptance Criteria**
- **AC-NFR-09.a** `pytest 03-development/tests -q` exits 0 and skipped count is **0**. (SPEC §8 #1)
- DERIVED: SPEC §4 NFR-09 — "每個測試函式至少一個 assert(ast-assertions 量測 zero_assert == 0)" — rationale: restating the canonical zero-assertion rule as a testable AC.
- **AC-NFR-09.b** `ast-assertions` reports `zero_assert == 0` for the test tree.
- DERIVED: SPEC §4 NFR-09 — "反造假條款:不得以 --ignore / -k / --deselect / collect_ignore / 從 testpaths 移除目錄等方式排除測試來達成上述數字" — rationale: restating the canonical anti-fabrication clause as a testable AC.
- **AC-NFR-09.c** No test was excluded via `--ignore` / `-k` / `--deselect` / `collect_ignore` to reach the numbers above.

---

### NFR-10: 整合覆蓋

**Canonical citation**: SPEC.md §4 NFR-10.
**dimension**: `integration_coverage`.

- `03-development/tests/integration/` cross-module integration tests, **line coverage ≥ 80%**
- Integration tests must be driven through the CLI entry (`python -m taskq_plus`) or `click.testing.CliRunner`; must NOT directly call internal functions
- At least covers: submit→run→status full chain, DAG multi-layer execution, breaker open/close, cache hit, plugin hook trigger, export three formats

**Acceptance Criteria**
- **AC-NFR-10.a** `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term` TOTAL **≥ 80%**. (SPEC §8 #3)

---

### NFR-11: 可讀性

**Canonical citation**: SPEC.md §4 NFR-11.
**dimension**: `readability`.

- Project MI (LLOC-weighted) **≥ 80**
- Single function cyclomatic complexity **≤ 10**
- Single file ≤ 400 lines; single directory ≤ 15 files

**Acceptance Criteria**
- DERIVED: SPEC §4 NFR-11 — "專案 MI(LLOC 加權) ≥ 80" — rationale: restating the canonical MI bound as a testable AC.
- **AC-NFR-11.a** `readability-v2` measures project MI ≥ 80.
- DERIVED: SPEC §4 NFR-11 — "單一函式 cyclomatic complexity ≤ 10" — rationale: restating the canonical CC bound as a testable AC.
- **AC-NFR-11.b** Per-function CC ≤ 10 across the project.
- DERIVED: SPEC §4 NFR-11 — "單一檔案 ≤ 400 行;單一目錄 ≤ 15 檔" — rationale: restating the canonical size bounds as testable ACs.
- **AC-NFR-11.c** No file exceeds 400 lines; no directory exceeds 15 files.

---

### NFR-12: 系統驗證目標

**Canonical citation**: SPEC.md §4 NFR-12.
**dimension**: `execute_verification_target`.

- `Makefile` must provide a `verify-system` target chaining: full test suite + CLI smoke (submit / run / status / graph / export / clear)
- `make verify-system` must **exit 0** and print `verify-system: PASS` on stdout

**Acceptance Criteria**
- DERIVED: SPEC §4 NFR-12 — "Makefile 必須提供 verify-system target,串接:全套測試 + CLI 冒煙(submit / run / status / graph / export / clear);make verify-system 必須 exit 0 並在 stdout 印出 verify-system: PASS" — rationale: restating the canonical verify-system contract as a single testable AC mapped to SPEC §8 #21.
- **AC-NFR-12.a** `make verify-system` exits 0 and stdout contains `verify-system: PASS`. (SPEC §8 #21)

---

## 5. Acceptance Criteria Summary

> Each row is a single machine-decidable command + expected output
> (transcribed verbatim from SPEC.md §8).

| # | Command | Expected |
|---|---------|----------|
| 1 | `pytest 03-development/tests -q` | all green, **skipped count is 0** (NFR-09) |
| 2 | `pytest 03-development/tests --cov=03-development/src --cov-report=term` | TOTAL **100%** |
| 3 | `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term` | TOTAL **≥ 80%** (NFR-10) |
| 4 | `python -m taskq_plus submit "echo hi"` | stdout is 8-hex id, exit 0 |
| 5 | `python -m taskq_plus submit ""` | exit 2 |
| 6 | `python -m taskq_plus submit "echo hi; rm x"` | exit 2 (injection character) |
| 7 | `TASKQ_TASK_TIMEOUT=1 python -m taskq_plus run <sleep-5-id>` | status `timeout`, exit 4 |
| 8 | After 3 consecutive final failures, `python -m taskq_plus run <id>` | exit 3; recovers after cooldown |
| 9 | Within TTL `python -m taskq_plus run <id> --cached` | outputs `cached: true`, no subprocess executed |
| 10 | `python -m taskq_plus submit "echo b" --after <a>` then `run --all` | b runs after a; a not `done` → b is `blocked` |
| 11 | Build A→B→A dependency | exit 5, stderr contains cycle path |
| 12 | `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` | exit 6 (path form rejected) |
| 13 | Plugin `pre_run` raises exception, then `run <id>` | task still completes; `audit.jsonl` contains `plugin_error` event |
| 14 | `python -m taskq_plus export --format json` / `csv` / `md` | three formats identical task count; csv comma/quote escaping correct |
| 15 | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` | **0 hits** (NFR-02) |
| 16 | `grep -c "^TASKQ_" .env.example` | **12** (§5.1 fully declared) |
| 17 | `lint-imports` | **exit 0** (NFR-06) |
| 18 | `pip-licenses --format=json` | each dependency license ∈ {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0} (NFR-07) |
| 19 | `bandit -r 03-development/src/` | 0 HIGH, 0 MEDIUM (NFR-02) |
| 20 | After `mutmut run`, `mutmut results` | mutation score **≥ 70** (NFR-08) |
| 21 | `make verify-system` | exit 0 and stdout contains `verify-system: PASS` (NFR-12) |
| 22 | Run a command containing a secret, then `grep -c "sk-" $TASKQ_HOME/audit.jsonl` | **0** (NFR-04) |

### Exit Code Map (SPEC §3 / §7)

| Code | Meaning |
|------|---------|
| 0 | success |
| 2 | input validation error (incl. unknown task id / unknown dependency) |
| 3 | breaker OPEN |
| 4 | task timeout (single-task mode only) |
| 5 | dependency cycle or depth cap exceeded |
| 6 | plugin load failure |
| 1 | other internal error |

---

## 6. Out-of-Scope

- **Round 2 / Round 3 deliverables**: backend + DB (`SPEC-2.md`); TypeScript port.
- **Audit log rotation**: append-only; rotation is the operator's responsibility — **not implemented this round** (Risk R10).
- **Long-running daemon mode**: this round ships a CLI invoked per-task; no daemon process.
- **Distributed / multi-node task queue**: local-only; no remote submission.
- **Per-task plugin selection**: plugins are loaded globally via `TASKQ_PLUGINS`; no per-task plugin override.
- **CRG calibration lowering**: this round **forbids** lowering `crg_cohesion_healthy` below its default value (SPEC §10 iron rule).

---

## 7. Open Issues

> Items deferred from the canonical spec or flagged for downstream
> confirmation. No TBD/TODO markers were detected in SPEC.md §3 / §4,
> so no FR-deferred rows are emitted this round.

| ID | Issue | Resolution path |
|----|-------|-----------------|
| **NFR-99-01** | SPEC.md does not specify what happens if the user submits a task with `--after <id>` where `<id>` is itself in `blocked` status at submit time (cycle detection runs, but `blocked` semantics are only defined at `run --all` time). | Confirm with stakeholder during Phase 3 module scaffolding; defer to `taskq_plus.service.dag` design. |
| **NFR-99-02** | SPEC.md §3 FR-04 says cache hit "replays `exit_code` / `stdout_tail`" — does NOT specify whether `duration_ms` and `finished_at` are also replayed or freshly computed. | Confirm with stakeholder; SRS proposes no default behavior — interpretation boundary is owned by the test harness per canonical line. |
| **NFR-99-03** | SPEC.md §4 NFR-01 `p95 < 50ms` measurement excludes subprocess execution — boundary is owned by the test harness per SPEC §4 NFR-01 verbatim. Implementation must NOT include subprocess fork/exec inside the measured window. |
| **NFR-99-04** | SPEC.md §4 NFR-04 redaction regex `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)` is verbatim; downstream may interpret `\S+` as "until whitespace" — measurement / interpretation boundary is owned by the test harness per canonical line. |

---

## 8. Risks

| ID | Risk | Impact | Likelihood | Mitigation | Source |
|----|------|--------|-----------|-----------|--------|
| R1 | concurrent write corrupts tasks.json | high | medium | Lock + atomic write (NFR-03) | SPEC §9 |
| R2 | subprocess hangs / zombies | medium | medium | timeout mandatory (FR-02) | SPEC §9 |
| R3 | breaker false lock | medium | low | cooldown + HALF_OPEN (FR-03) | SPEC §9 |
| R4 | cache replays stale results | low | medium | TTL expiry forces re-execute (FR-04) | SPEC §9 |
| R5 | secret-on-disk leak | high | medium | redaction before write (NFR-04) | SPEC §9 |
| R6 | **plugin becomes an arbitrary-code-execution entry point** | **high** | medium | allowlist + name regex + no eval/exec/path (FR-07 / NFR-02) | SPEC §9 |
| R7 | pathological dependency graph exhausts resources | medium | low | cycle detection + depth cap (FR-06) | SPEC §9 |
| R8 | plugin exception aborts the main flow | medium | medium | exception isolation + disable after 3 failures (FR-07) | SPEC §9 |
| R9 | dependency with incompatible license | medium | low | pinning + allowlist + SBOM (NFR-07) | SPEC §9 |
| R10 | audit log grows without bound | low | high | append-only; rotation is the operator's job — **not implemented this round**, recorded as a known limitation | SPEC §9 |

---

## 9. Glossary

| Term | Definition |
|------|-----------|
| `taskq-plus` | The local task-queue CLI project (this document). |
| Canonical spec / SPEC.md | The single source of truth at `/Users/johnny/projects/taskq-plus/SPEC.md` (v1.0.0, 2026-07-30). All FRs and NFRs in this SRS are transcribed from it. |
| Round 1 / 2 / 3 | Progressive test-bed rounds. This SRS covers Round 1 (Python CLI). |
| DAG | Directed Acyclic Graph. Used for task dependency ordering (FR-06). |
| Kahn algorithm | Topological sort algorithm that drives `run --all` execution order (FR-06). |
| HALF_OPEN | Circuit-breaker state in which one probe task is admitted (FR-03). |
| correlation_id | Per-CLI-invocation identifier shared across all triggered audit events (FR-08). |
| Allowlist | Explicit permit-list (regex `^[A-Za-z_][A-Za-z0-9_.]*$`) for plugin module names (FR-07). |
| SBOM | Software Bill of Materials; emitted to `08-config/SBOM.json` (NFR-07). |
| Atomic write | tmp-file write + `os.replace` so the on-disk file is either the old or new content, never partial (NFR-03). |
| Redaction | Replacement of secret-matching lines with `[REDACTED]` before disk write (NFR-04). |
| DIMENSION_TOOLS | The framework's `DIMENSION_TOOLS["python"]` registry; every NFR's `dimension` field is a real key in that registry (SPEC §4 iron rule). |
| crg_cohesion_healthy | CRG calibration threshold; this round **forbids** lowering the default (SPEC §10). |

---

*End of SRS — taskq-plus v1.0.0, 2026-07-30. Transcribed from SPEC.md v1.0.0.*
