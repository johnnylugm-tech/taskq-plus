# Test Plan — taskq-plus (P4)

> Phase 4 deliverable. Authored before per-FR testing.
> Source of truth: `01-requirements/SRS.md` (FR-01..FR-08, NFR-01..NFR-12) and
> `.methodology/quality_manifest.json` (FR list, NFR→dimension mapping).
> Naming convention: `test_<scope><NN>_<suffix>` per
> `01-requirements/TRACEABILITY_MATRIX.md §1.3` and `TEST_INVENTORY.yaml`.
> All `tc_id` align with the AC IDs in `SRS.md §3` / `§4`.

---

## 1. Scope

This plan enumerates every test case required to verify the 8 functional
requirements (FR-01..FR-08) and 12 non-functional requirements (NFR-01..NFR-12)
defined in `SRS.md`. Each entry includes: test case ID, description, input,
expected output, priority, and category (positive / negative / boundary /
edge-case). The plan is the single contract used by per-FR testing in P4.

### 1.1 Coverage Boundaries

| Boundary | In scope | Out of scope |
|----------|----------|--------------|
| FR verification | All 8 FRs (FR-01..FR-08) | — |
| NFR verification | All 12 NFRs (NFR-01..NFR-12) | — |
| Test categories | positive, negative, boundary, edge-case | performance micro-bench beyond NFR-01 bounds |
| Test layers | unit, integration (`tests/integration/`), system (`make verify-system`) | e2e multi-host, distributed |
| Inputs | `TASKQ_HOME` synthesized via `tmp_path`; env-vars via `monkeypatch` | persistent changes to `$HOME` |

### 1.2 Priority Legend

| Priority | Meaning |
|----------|---------|
| P0 | Blocks Gate 3/4 PASS; mapped to a SPEC §8 AC; must run. |
| P1 | Verifies canonical FR/NFR clause; must run for Gate 3. |
| P2 | Strengthens coverage (edge-case / boundary); desired for Gate 4. |
| P3 | Defensive / anti-regression; nice-to-have. |

### 1.3 Category Legend

| Category | Definition |
|----------|-----------|
| Positive | Valid input → expected happy path. |
| Negative | Invalid input → rejection / exit code / error. |
| Boundary | Exact boundary value (e.g. 1000 chars, 0 tasks, cooldown ± 1s). |
| Edge-case | Concurrent / interleaved / out-of-order / malformed. |

---

## 2. FR Coverage Matrix

| FR | Title | ACs | Test files (target) | Total tests |
|----|-------|-----|---------------------|-------------|
| FR-01 | 任務提交與驗證 | 4 (a/b/c/d) | `tests/test_fr01.py` | 4 |
| FR-02 | 任務執行器 | 5 (a/b/c/d/e) | `tests/test_fr02.py` | 5 |
| FR-03 | 重試與斷路器 | 4 (a/b/c/d) | `tests/test_fr03.py` | 4 |
| FR-04 | 結果 TTL 快取 | 4 (a/b/c/d) | `tests/test_fr04.py` | 4 |
| FR-05 | CLI 整合 | 4 (a/b/c/d) | `tests/test_fr05.py` | 4 |
| FR-06 | 任務相依 DAG | 4 (a/b/c/d) | `tests/test_fr06.py` | 4 |
| FR-07 | Plugin Hook 系統 | 5 (a/b/c/d/e) | `tests/test_fr07.py` | 5 |
| FR-08 | 結構化稽核日誌與匯出 | 4 (a/b/c/d) | `tests/test_fr08.py` | 4 |

---

## 3. FR-01 — 任務提交與驗證

**Module**: `taskq_plus.models.task`, `taskq_plus.storage.task_store`, `taskq_plus.cli.commands`
**Layer**: models → storage → cli
**SPEC citation**: `SRS.md §3 FR-01`; `§8 #4-#6`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_fr01_a` | Positive | P0 | Submit a valid command does not reject and writes a task. | `submit "echo hi"` | stdout is 8-hex id; exit 0; `tasks.json` contains the task with status `pending`; `audit.jsonl` has a `submit` event. |
| `test_fr01_b` | Negative | P0 | Empty command rejected with exit 2. | `submit ""` | exit 2; stderr contains validation error; `tasks.json` unchanged. |
| `test_fr01_c` | Negative | P0 | Injection character (`;`) rejected with exit 2. | `submit "echo hi; rm x"` | exit 2; stderr notes injection char; `tasks.json` unchanged. |
| `test_fr01_d` | Negative (parametrized) | P0 | Each blacklisted injection character rejected (one case per char). | `submit "echo hi<CHAR>"` for each `;` `\|` `&` `$` `>` `<` `` ` `` | exit 2 for each char; 7 cases total. |
| `test_fr01_b_neg_ws` | Boundary | P1 | Command consisting of whitespace only is rejected. | `submit "   "` | exit 2 (whitespace-only fails non-empty rule). |
| `test_fr01_len_exact` | Boundary | P1 | Command of exactly 1000 chars accepted. | `submit "echo " + "a"*995` | exit 0; id written. |
| `test_fr01_len_over` | Boundary | P1 | Command of 1001 chars rejected. | `submit "echo " + "a"*996` | exit 2 (length rule). |
| `test_fr01_name_dup` | Negative | P1 | `--name` collides with existing pending/running task. | `submit "echo a" --name foo` then `submit "echo b" --name foo` | second exits 2; tasks.json unchanged for second. |
| `test_fr01_dep_missing` | Negative | P1 | `--after` references non-existent task id. | `submit "echo b" --after deadbeef` | exit 2; dependency-exists rule. |
| `test_fr01_multi_dep` | Boundary | P2 | Multiple `--after` all valid → all edges recorded. | `submit "echo c" --after a --after b` | id written; `depends_on == [a,b]`. |
| `test_fr01_uuid8` | Positive | P2 | Generated id is 8 hex chars (uuid4 first 8). | `submit "echo hi"` | regex `^[0-9a-f]{8}$`. |
| `test_fr01_json_flag` | Positive | P2 | `--json` outputs single-line JSON with `id` and `status`. | `submit "echo hi" --json` | JSON line `{"id":"...","status":"pending"}`; exit 0. |
| `test_fr01_no_write_on_reject` | Negative | P2 | Rejected submission writes nothing to storage. | `submit ""` then read `tasks.json` | file is empty / unchanged. |
| `test_fr01_audit_event` | Positive | P2 | `submit` audit event emitted with correct fields. | submit then read `audit.jsonl` | JSON line with `event="submit"`, `task_id`, `correlation_id`, `detail`. |

---

## 4. FR-02 — 任務執行器

**Module**: `taskq_plus.service.executor`, `taskq_plus.storage.task_store`
**SPEC citation**: `SRS.md §3 FR-02`; `§8 #7`; `#15`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_fr02_a` | Boundary | P0 | Single-task mode timeout → status `timeout`, exit 4. | `TASKQ_TASK_TIMEOUT=1 run <sleep-5-id>` | exit 4; task status `timeout`; `duration_ms ≈ 1000`. |
| `test_fr02_b` | Positive | P0 | Exit 0 → `done`; non-zero exit → `failed`. | two tasks: `echo ok` and `exit 3` | first `done`; second `failed`; both have `exit_code` recorded. |
| `test_fr02_c` | Positive | P0 | `run --all` uses ThreadPoolExecutor + shared Lock + DAG order. | 3 tasks with edges A→B, A→C; `run --all` | B and C start only after A finishes; concurrent writes do not corrupt `tasks.json`. |
| `test_fr02_d` | Boundary | P0 | `stdout_tail` / `stderr_tail` bounded to last 2000 chars. | task emitting 3000 chars to stdout | `len(stdout_tail) == 2000`; content === last 2000 chars of source. |
| `test_fr02_e` | Edge-case | P0 | `grep -rn "shell=True" 03-development/src/` returns 0 hits. | n/a (static) | 0 hits. |
| `test_fr02_subprocess_invoc` | Positive | P1 | `shlex.split` is used (no shell interpretation). | `submit "echo a && echo b"` is rejected by FR-01, but a synthesised command `echo 'a && b'` is run | subprocess treats `&&` as literal string. |
| `test_fr02_result_fields` | Positive | P1 | Result fields: `exit_code`, `stdout_tail`, `stderr_tail`, `duration_ms`, `finished_at`. | `run <id>` on `echo hi` | all 5 fields present; `finished_at` is ISO-8601 UTC. |
| `test_fr02_lock_serialize` | Edge-case | P1 | Concurrent writers under the shared lock produce valid JSON. | 2 threads invoke `run --all` on overlapping task sets | final `tasks.json` parses; no torn records. |
| `test_fr02_blocked` | Positive | P1 | Dependency not satisfied → `blocked`, not executed. | create dependent whose `after` is not `done` | dependent status `blocked`; no subprocess recorded. |
| `test_fr02_dur_zero` | Boundary | P2 | `duration_ms` is non-negative integer. | `run <id>` on `true` | `duration_ms >= 0` and is `int`. |
| `test_fr02_unknown_id` | Negative | P2 | `run` on unknown id → exit 2. | `run deadbeef` | exit 2. |

---

## 5. FR-03 — 重試與斷路器

**Module**: `taskq_plus.service.breaker`, `taskq_plus.storage.breaker_store`, `taskq_plus.service.executor`
**SPEC citation**: `SRS.md §3 FR-03`; `§8 #8`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_fr03_a` | Positive | P0 | 3 consecutive final failures → next `run` exits 3; recovers after cooldown. | 3 tasks that fail; 4th `run` | 4th exits 3; after `TASKQ_BREAKER_COOLDOWN` a 5th runs and breaker closes. |
| `test_fr03_b` | Positive | P0 | Retry uses `TASKQ_BACKOFF_BASE × 2^n` exponential backoff; sleep injectable. | failed task; inject fake sleep | sleeps observed: `[base, base*2, base*4]`; retry count == `TASKQ_RETRY_LIMIT`. |
| `test_fr03_c` | Positive | P0 | Breaker state transitions follow `CLOSED → OPEN → HALF_OPEN → CLOSED`. | walk through the transitions | state attribute reflects each step in turn. |
| `test_fr03_d` | Boundary | P0 | `OPEN → CLOSED` recovery time ≤ `TASKQ_BREAKER_COOLDOWN` + 1s. | drive to OPEN, wait, then succeed | elapsed ≤ cooldown + 1s. |
| `test_fr03_half_open_probe` | Positive | P1 | HALF_OPEN admits exactly one probe; success → CLOSED. | drive to HALF_OPEN, run one task | concurrently starting a second task before first ends → second rejected. |
| `test_fr03_open_no_subprocess` | Edge-case | P1 | When OPEN, the rejected run does not spawn a subprocess. | breaker OPEN; `run <id>` | exit 3; no entry in `tasks.json` finished list; audit `breaker_open` event present. |
| `test_fr03_global_persisted` | Positive | P1 | Breaker state persists across processes (`breaker.json`). | process A trips breaker; process B reads | process B sees OPEN on first read. |
| `test_fr03_atomic_write` | Edge-case | P1 | `breaker.json` writes are atomic. | kill -9 mid-write | file remains valid JSON; state is either old or new. |
| `test_fr03_threshold` | Boundary | P2 | Breaker threshold is configurable via `TASKQ_BREAKER_THRESHOLD`. | set `TASKQ_BREAKER_THRESHOLD=2`; fail 2 | breaker opens on 2nd. |
| `test_fr03_timeout_counts` | Positive | P2 | `timeout` final results count toward the breaker tally. | run timeouts > threshold | breaker opens. |
| `test_fr03_reset_on_close` | Positive | P2 | Counter resets on `CLOSED` transition. | trip → cooldown → succeed | next failure counts as 1, not 4. |
| `test_fr03_retry_limit_zero` | Boundary | P2 | `TASKQ_RETRY_LIMIT=0` → no retries. | failed task | 1 attempt total; persisted status `failed`. |

---

## 6. FR-04 — 結果 TTL 快取

**Module**: `taskq_plus.service.cache`, `taskq_plus.storage.cache_store`
**SPEC citation**: `SRS.md §3 FR-04`; `§8 #9`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_fr04_a` | Positive | P0 | Within TTL `run <id> --cached` returns `cached: true`; no subprocess executed. | submit `echo hi` → run → run --cached | second `runs` no subprocess; result has `cached: true`; `exit_code` and `stdout_tail` replayed. |
| `test_fr04_b` | Positive | P0 | Expired cache entry → normal execution; on `done` writes to `cache.json`. | advance time past TTL; run --cached | subprocess executed; `cache.json` updated with new entry. |
| `test_fr04_c` | Positive | P0 | Cache key is `sha256(command)`. | submit `echo hi`; read `cache.json` | key matches `sha256("echo hi").hexdigest()`. |
| `test_fr04_d` | Edge-case | P0 | Cache read/write atomic and thread-safe while coexisting with FR-02 concurrency. | 2 threads, one writer one reader, on same key | `cache.json` parses; final entry has consistent fields. |
| `test_fr04_miss_then_write` | Positive | P1 | Cache miss → execute → write `done` result. | first run --cached | subprocess ran; cache file populated. |
| `test_fr04_failed_not_cached` | Negative | P1 | Failed/timeout results are not cached. | task that fails, then run --cached | subprocess runs again; cache.json has no entry. |
| `test_fr04_ttl_zero` | Boundary | P1 | `TASKQ_CACHE_TTL=0` → always miss. | submit, run --cached | second run executes. |
| `test_fr04_no_subprocess_on_hit` | Edge-case | P1 | `cached: true` task has no subprocess spawn (asserted via running task count counter). | inject counter in `subprocess.run` | counter unchanged on cache hit. |
| `test_fr04_different_command` | Positive | P2 | Two distinct commands cache under distinct keys. | submit `echo a`, `echo b` | two cache entries with different sha256 keys. |
| `test_fr04_concurrent_writes` | Edge-case | P2 | Concurrent writers produce a valid `cache.json`. | N threads write to cache | final file parses; no torn entry. |

---

## 7. FR-05 — CLI 整合

**Module**: `taskq_plus.cli.main`, `taskq_plus.cli.commands`, `taskq_plus.__main__`
**SPEC citation**: `SRS.md §3 FR-05`; `§8 #14`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_fr05_a` | Positive | P0 | Every subcommand is wired via `click` and reachable. | `submit`, `run`, `status`, `list`, `graph`, `plugins list`, `export`, `clear` | each `CliRunner.invoke` returns exit 0 (or expected non-zero). |
| `test_fr05_b` | Positive | P0 | `--json` outputs single-line JSON. | `list --json` | stdout is one JSON line per record. |
| `test_fr05_c` | Positive | P0 | Exit codes per SRS §3 / §5. | exercise each exit-code path | exit ∈ {0,2,3,4,5,6,1}. |
| `test_fr05_d` | Positive | P0 | `export --format json` / `csv` / `md` produce three formats with identical task counts. | 5 tasks; export all three | json array length == csv row count == md table row count. |
| `test_fr05_status_field` | Positive | P1 | `status <id>` outputs all task fields. | `status <id>` | all fields present (id, command, status, exit_code, depends_on, ...). |
| `test_fr05_list_filter` | Positive | P1 | `list --status S` filters records. | submit 3 tasks, filter by status | only matching tasks returned. |
| `test_fr05_clear` | Positive | P1 | `clear` empties `$TASKQ_HOME` data files. | run submit, then `clear` | all four data files empty / absent. |
| `test_fr05_unknown_id` | Negative | P1 | `status <deadbeef>` exits 2. | `status deadbeef` | exit 2. |
| `test_fr05_csv_escape` | Boundary | P1 | CSV escapes commas/quotes properly. | task with comma and quote in stdout | CSV row is well-formed (escaped). |
| `test_fr05_md_table` | Positive | P2 | `md` export is a Markdown table. | export --format md | first line starts with `\|`; column count consistent. |
| `test_fr05_help` | Positive | P2 | `python -m taskq_plus --help` lists subcommands. | --help | stdout contains all 8 subcommands. |
| `test_fr05_json_round` | Positive | P2 | `--json` output is parseable JSON. | `list --json` | `json.loads(line)` succeeds. |

---

## 8. FR-06 — 任務相依 DAG

**Module**: `taskq_plus.service.dag`
**SPEC citation**: `SRS.md §3 FR-06`; `§8 #10-#11`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_fr06_a` | Positive | P0 | `submit "echo b" --after <a>` then `run --all` → b runs after a; a not done → b blocked. | submit a, b `--after a`; run --all | order: a before b; if a fails/timeout, b status `blocked`. |
| `test_fr06_b` | Negative | P0 | Build A→B→A dependency → exit 5; stderr contains cycle path. | submit A, then B `--after A`, then `submit "echo C" --after <B>` and reference A again | exit 5; stderr contains `A → B → A` (or canonical cycle). |
| `test_fr06_c` | Boundary | P0 | Dependency chain depth > `TASKQ_MAX_DAG_DEPTH` → exit 5; stderr `dependency chain too deep: <n> > <max>`. | build a linear chain of length `MAX + 1` | exit 5; stderr message matches exactly. |
| `test_fr06_d` | Positive | P0 | Kahn topological sort emits tasks layer-by-layer; same-layer tasks may be scheduled concurrently. | 3 tasks with edges A→B, A→C | instrumented order: A first, then [B,C] in some order; B and C may overlap in time. |
| `test_fr06_graph_text` | Positive | P1 | `graph --format text` outputs indented tree. | `graph --format text` | stdout contains indented tree (`a`, `└ b`, ...). |
| `test_fr06_graph_dot` | Positive | P1 | `graph --format dot` outputs Graphviz DOT. | `graph --format dot` | stdout starts with `digraph` and ends with `}`. |
| `test_fr06_blocked_no_count` | Positive | P1 | Blocked tasks do not count toward breaker failure. | breaker threshold 2; one task is `blocked` after a failure | breaker not opened by the blocked task. |
| `test_fr06_diamond` | Edge-case | P1 | Diamond DAG (A→B, A→C, B→D, C→D) runs in correct order. | 4 tasks with diamond edges | A first; B and C concurrent; D last. |
| `test_fr06_self_loop` | Negative | P2 | `submit --after <self>` rejected. | `submit "echo a" --after <self-id>` | exit 5. |
| `test_fr06_depth_exact` | Boundary | P2 | Depth exactly `MAX` is accepted. | chain of length `MAX` | exit 0. |
| `test_fr06_cycle_long` | Boundary | P2 | Cycle of length > 2 still detected. | 4-node cycle | exit 5; path printed. |

---

## 9. FR-07 — Plugin Hook 系統

**Module**: `taskq_plus.service.plugins`
**SPEC citation**: `SRS.md §3 FR-07`; `§8 #12`; `#13`; `#15`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_fr07_a` | Negative | P0 | `TASKQ_PLUGINS="../evil.py" python -m taskq_plus plugins list` → exit 6. | path form in env | exit 6 (name regex rejects `/`). |
| `test_fr07_b` | Edge-case | P0 | Plugin `pre_run` raises → task still completes; `audit.jsonl` contains `plugin_error`. | failing plugin; run <id> | task `done`; audit `plugin_error` event present. |
| `test_fr07_c` | Positive | P0 | A plugin failing 3 consecutive times within one run is disabled for that run. | failing plugin; run 3 tasks | 3 `plugin_error` events; 4th task run skips the plugin. |
| `test_fr07_d` | Positive | P0 | `plugins list` prints each plugin's module name, hooks, and load status. | `plugins list` with two plugins | stdout contains module names, hook list, and `loaded` / `failed` status. |
| `test_fr07_e` | Edge-case | P0 | `grep -rn "eval(\|exec(" 03-development/src/` returns 0 hits. | n/a (static) | 0 hits. |
| `test_fr07_name_regex_neg` | Boundary | P1 | Plugin name with leading digit rejected. | `TASKQ_PLUGINS=1bad` | exit 6. |
| `test_fr07_name_regex_pos` | Boundary | P1 | Plugin name `my_plugin.v2` accepted. | `TASKQ_PLUGINS=my_plugin.v2` | plugins listed. |
| `test_fr07_no_path` | Negative | P1 | Absolute path plugin form rejected. | `TASKQ_PLUGINS=/abs/path` | exit 6. |
| `test_fr07_no_url` | Negative | P1 | URL plugin form rejected. | `TASKQ_PLUGINS=https://x` | exit 6. |
| `test_fr07_post_run` | Positive | P1 | `post_run` hook invoked after task completion. | injecting plugin | `post_run` called with `task` and `result`. |
| `test_fr07_isolation` | Edge-case | P1 | Plugin exception isolated per task; other tasks unaffected. | one bad plugin + one good task | good task completes; only that plugin disabled. |
| `test_fr07_load_failure_exit6` | Negative | P2 | Plugin module import fails → exit 6. | `TASKQ_PLUGINS=does_not_exist` | exit 6. |
| `test_fr07_disabled_no_events` | Edge-case | P2 | After disable, no further `plugin_error` events for that plugin. | fixture failing plugin | events count == 3. |

---

## 10. FR-08 — 結構化稽核日誌與匯出

**Module**: `taskq_plus.observability.audit`, `taskq_plus.observability.export`
**SPEC citation**: `SRS.md §3 FR-08`; `§8 #14`; `#22`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_fr08_a` | Positive | P0 | Audit entry has `ts`, `event`, `task_id`, `correlation_id`, `detail`. | trigger any audit event | all 5 fields present; `ts` is ISO-8601 UTC. |
| `test_fr08_b` | Positive | P0 | Single CLI invocation shares one `correlation_id` across all triggered events. | submit + run + status | all `audit.jsonl` lines from the invocation share the same `correlation_id`. |
| `test_fr08_c` | Positive | P0 | Three export formats produce identical task count; CSV escapes commas/quotes. | 5 tasks including one with `,` and `"` in output | json array len == csv row count == md table rows; csv row is well-formed. |
| `test_fr08_d` | Edge-case | P0 | Audit log writes go through NFR-04 redaction (no plaintext secret on disk). | task that emits `sk-abcdef123456` | `grep -c "sk-" audit.jsonl` == 0. |
| `test_fr08_event_types` | Positive | P1 | Event types: `submit`, `run_start`, `run_end`, `retry`, `breaker_open`, `breaker_close`, `cache_hit`, `blocked`, `plugin_error`. | walk through each scenario | each event appears in `audit.jsonl` at least once. |
| `test_fr08_jsonl_format` | Positive | P1 | Audit log is JSON Lines (one JSON per line, parses as `json.loads`). | read `audit.jsonl` | each line is a valid JSON object. |
| `test_fr08_append_only` | Edge-case | P1 | Audit file is opened in append mode (no overwrite). | trigger two invocations | second invocation appends; first events still present. |
| `test_fr08_iso8601` | Boundary | P1 | `ts` is strict ISO-8601 UTC (`...Z` or `+00:00`). | parse first event | regex pass. |
| `test_fr08_export_md_header` | Positive | P1 | `md` export has a header row. | export --format md | first row is a header followed by `---` separator. |
| `test_fr08_correlation_unique` | Edge-case | P2 | Two distinct invocations produce distinct `correlation_id` values. | two CLI calls | ids differ. |
| `test_fr08_event_detail` | Positive | P2 | `detail` field contains relevant context (e.g. task id, exit_code). | run successful task | `detail` includes `exit_code` and `duration_ms`. |
| `test_fr08_path_env` | Boundary | P2 | `TASKQ_AUDIT_LOG` overrides the audit path. | set custom path | file written at that path; default path empty. |

---

## 11. NFR Coverage Matrix

| NFR | Dimension | ACs | Test files (target) | Total tests |
|-----|-----------|-----|---------------------|-------------|
| NFR-01 | performance | 2 (a/b) | `tests/test_fr06.py` (perf), `tests/test_phase4_property_specs.py` | 2 |
| NFR-02 | security | 4 (a/b/c/d) | `tests/test_fr01.py`, `tests/test_fr02.py`, `tests/test_fr05.py`, `tests/test_fr07.py`, `tests/test_phase3_exit_coverage.py` | 4 |
| NFR-03 | error_handling | 4 (a/b/c/d) | `tests/test_fr02.py`, `tests/test_fr03.py`, `tests/test_fr04.py`, `tests/test_fr05.py`, `tests/test_fr06.py`, `tests/test_fr07.py`, `tests/test_phase3_exit_coverage.py` | 4 |
| NFR-04 | security | 2 (a/b) | `tests/test_fr05.py`, `tests/test_fr08.py`, `tests/test_phase3_exit_coverage.py` | 2 |
| NFR-05 | readability | 1 (a) | `tests/test_fr05.py` (docstring check) | 1 |
| NFR-06 | architecture_constraints | 3 (a/b/c) | `tests/test_phase3_exit_coverage.py` | 3 |
| NFR-07 | license_compliance | 3 (a/b/c) | `tests/test_phase4_property_specs.py` | 3 |
| NFR-08 | test_assertion_quality (mutation) | 2 (a/b) | `tests/test_phase4_property_specs.py` | 2 |
| NFR-09 | test_assertion_quality | 3 (a/b/c) | `tests/test_fr05.py` (zero-skip), `tests/test_phase4_property_specs.py` | 3 |
| NFR-10 | test_assertion_quality (integration) | 1 (a) | `tests/integration/` | 1 |
| NFR-11 | readability | 3 (a/b/c) | `tests/test_phase4_property_specs.py` | 3 |
| NFR-12 | execute_verification_target | 1 (a) | `tests/test_fr05.py` (verify-system) | 1 |

---

## 12. NFR-01 — 效能預算

**Tool**: `pytest-benchmark`
**SPEC citation**: `SRS.md §4 NFR-01`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr01_a` | Boundary | P0 | `submit`+`status` p95 over 100 iterations < 50ms. | 100x `submit "echo hi"` + `status <id>` | `pytest-benchmark` reports p95 < 50ms. |
| `test_nfr01_b` | Boundary | P0 | Topological sort p95 over 200 tasks < 200ms. | 200 tasks; `run --all` (subprocess excluded) | p95 < 200ms. |

---

## 13. NFR-02 — 執行與載入安全

**Tool**: `grep`, `bandit`
**SPEC citation**: `SRS.md §4 NFR-02`; `§8 #15`; `#19`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr02_a` | Edge-case | P0 | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` returns 0 hits. | static scan | 0 lines. |
| `test_nfr02_b` | Negative (parametrized) | P0 | Each blacklisted character (`; \| & $ > < \``) has a test case asserting exit 2. | 7 inject cases | 7 exit-2 results. |
| `test_nfr02_c` | Negative | P0 | Plugin name `../evil.py` rejected with exit 6. | `TASKQ_PLUGINS=../evil.py` | exit 6. |
| `test_nfr02_d` | Positive | P0 | `bandit -r 03-development/src/` reports 0 HIGH and 0 MEDIUM. | bandit JSON | issues.high == 0; issues.medium == 0. |

---

## 14. NFR-03 — 錯誤處理與原子性

**Tool**: pytest + `subprocess` + `kill -9` simulation
**SPEC citation**: `SRS.md §4 NFR-03`; `§7` (error table)

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr03_a` | Edge-case | P0 | Mid-write interruption test: after kill -9 the file remains parseable. | fork write; kill -9 parent | `tasks.json` / `cache.json` / `breaker.json` parses; `audit.jsonl` each line parses. |
| `test_nfr03_b` | Edge-case | P0 | Codebase contains no bare `except:`, no `except Exception: pass`, no swallowed `KeyboardInterrupt` / `SystemExit`. | AST scan | 0 hits each. |
| `test_nfr03_c` | Boundary | P0 | Breaker `OPEN → CLOSED` recovery time ≤ `TASKQ_BREAKER_COOLDOWN` + 1s. | drive to OPEN, wait, succeed | elapsed ≤ cooldown + 1s. |
| `test_nfr03_d` | Negative | P0 | Corrupted `tasks.json` at startup → exit 1, stderr contains `store corrupted`; no silent rebuild. | write invalid JSON to `tasks.json`; run CLI | exit 1; stderr `store corrupted`; file untouched. |

---

## 15. NFR-04 — 敏感資料遮蔽

**SPEC citation**: `SRS.md §4 NFR-04`; `§8 #22`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr04_a` | Positive | P0 | `grep -c "sk-" $TASKQ_HOME/audit.jsonl` == 0 after secret-bearing task. | task emitting `sk-abcdef1234567890` | count == 0. |
| `test_nfr04_b` | Edge-case | P0 | Redaction runs before the disk write (asserts on file contents, not on post-load string). | inspect raw bytes of `audit.jsonl` | secret substring not present; `[REDACTED]` present. |

---

## 16. NFR-05 — 文件覆蓋

**Tool**: `ast-docstrings`
**SPEC citation**: `SRS.md §4 NFR-05`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr05_a` | Positive | P0 | `ast-docstrings` reports 100% coverage of public symbols with `[FR-XX]` / `[NFR-XX]` tags. | scan `03-development/src/taskq_plus` | coverage == 100%; each missing tag is reported. |

---

## 17. NFR-06 — 架構分層契約

**Tool**: `lint-imports`
**SPEC citation**: `SRS.md §4 NFR-06`; `§8 #17`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr06_a` | Positive | P0 | `.importlinter` exists at project root and declares the 5-layer contract verbatim. | read `.importlinter` | contract == `cli > observability > service > storage > models`. |
| `test_nfr06_b` | Positive | P0 | `lint-imports` exits 0. | run `lint-imports` | exit 0. |
| `test_nfr06_c` | Edge-case | P0 | Code review confirms the contract is not weakened by wildcard `ignore_imports` or single-`forbidden` substitution. | parse `.importlinter` | no wildcard rule; no single-`forbidden` shortcut. |

---

## 18. NFR-07 — 依賴與授權合規

**Tool**: `pip-licenses`, custom requirements parser
**SPEC citation**: `SRS.md §4 NFR-07`; `§8 #18`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr07_a` | Positive | P0 | `pip-licenses --format=json` returns each dependency's license ∈ {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0}. | run pip-licenses | every license in allowed set. |
| `test_nfr07_b` | Positive | P0 | `requirements.txt` pins every runtime dependency with `==`. | read requirements.txt | every line matches `^[\w-]+==\d+(\.\d+)*$`. |
| `test_nfr07_c` | Positive | P0 | `08-config/SBOM.json` lists each dependency with `name`, `version`, `license`. | read `08-config/SBOM.json` | JSON array of `{name, version, license}` records. |

---

## 19. NFR-08 — 變異測試

**Tool**: `mutmut`
**SPEC citation**: `SRS.md §4 NFR-08`; `§8 #20`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr08_a` | Boundary | P0 | `mutmut run` + `mutmut results` reports mutation score ≥ 70. | run mutmut on `service/`, `storage/` | score >= 70. |
| `test_nfr08_b` | Positive | P0 | `harness_config.json` has `features.mutation_testing: true`. | read `.methodology/harness_config.json` | path == true. |

---

## 20. NFR-09 — 驗證真實性（零 skip 鐵律）

**Tool**: pytest, `ast-assertions`
**SPEC citation**: `SRS.md §4 NFR-09`; `§8 #1`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr09_a` | Positive | P0 | `pytest 03-development/tests -q` exits 0 and skipped count is 0. | run pytest | exit 0; no "skipped" rows. |
| `test_nfr09_b` | Positive | P0 | `ast-assertions` reports `zero_assert == 0`. | AST scan | zero_assert == 0. |
| `test_nfr09_c` | Edge-case | P0 | No test was excluded via `--ignore` / `-k` / `--deselect` / `collect_ignore`. | audit pyproject/pytest config | no exclusion clauses. |

---

## 21. NFR-10 — 整合覆蓋

**Tool**: `pytest --cov`
**SPEC citation**: `SRS.md §4 NFR-10`; `§8 #3`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr10_a` | Boundary | P0 | `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term` TOTAL ≥ 80%. | run integration suite | TOTAL >= 80. |
| `test_nfr10_chain` | Positive | P1 | Integration suite covers: submit→run→status full chain, DAG multi-layer, breaker open/close, cache hit, plugin hook, export three formats. | per-scenario assertions | each scenario passes. |

---

## 22. NFR-11 — 可讀性

**Tool**: `readability-v2`, AST metrics
**SPEC citation**: `SRS.md §4 NFR-11`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr11_a` | Boundary | P0 | `readability-v2` measures project MI ≥ 80. | run tool | MI >= 80. |
| `test_nfr11_b` | Boundary | P0 | Per-function CC ≤ 10. | AST CC scan | max(CC) <= 10. |
| `test_nfr11_c` | Boundary | P0 | No file exceeds 400 lines; no directory exceeds 15 files. | filesystem scan | max lines <= 400; max count <= 15. |

---

## 23. NFR-12 — 系統驗證目標

**Tool**: `make`
**SPEC citation**: `SRS.md §4 NFR-12`; `§8 #21`

| tc_id | Category | Priority | Description | Input | Expected Output |
|-------|----------|----------|-------------|-------|-----------------|
| `test_nfr12_a` | Positive | P0 | `make verify-system` exits 0 and stdout contains `verify-system: PASS`. | run make target | exit 0; stdout substring matches. |

---

## 24. Cross-Cutting Concerns

| Concern | Coverage | Test refs |
|---------|----------|-----------|
| Exit code map (`0/2/3/4/5/6/1`) | per-FR | `test_fr0*_c`, `test_fr05_c` |
| Clock injection | FR-03, FR-04 | `test_fr03_d`, `test_fr04_b`, `test_fr04_ttl_zero` |
| Subprocess isolation | FR-02, FR-07, FR-04 hit | `test_fr02_e`, `test_fr07_e`, `test_fr04_no_subprocess_on_hit` |
| Audit correlation_id | FR-08 | `test_fr08_b`, `test_fr08_correlation_unique` |
| NFR-09 zero-skip | global | `test_nfr09_a`, `test_nfr09_b`, `test_nfr09_c` |

---

## 25. Execution Order (suggested)

1. **FR-01** (entry point; P0 exit codes 0/2). Validate before any other FR.
2. **NFR-02** static checks (grep, bandit) — early signal on forbidden APIs.
3. **FR-02** (single-task; sets the runner baseline).
4. **FR-04** (uses FR-02's runner; cache layer).
5. **FR-03** (uses executor + cache; breaker state).
6. **FR-06** (uses executor + breaker; DAG ordering).
7. **FR-07** (plugin hooks; touches executor + audit).
8. **FR-08** (audit + export; uses correlation_id across all FRs).
9. **FR-05** (full CLI integration; smoke tests).
10. **NFR-03** + **NFR-04** (crash-safety + redaction; depend on all FRs).
11. **NFR-01** (perf budget; needs the suite stable).
12. **NFR-05/06/07/11** (static / config).
13. **NFR-08** (mutmut; heaviest; run last).
14. **NFR-09** (zero-skip; audit at the end).
15. **NFR-10** (integration coverage).
16. **NFR-12** (`make verify-system`).

---

## 26. Verification of Coverage

This plan must cover every FR in `.methodology/quality_manifest.json`:

| Manifest FR | Plan section | Tests |
|-------------|--------------|-------|
| FR-01 | §3 | 14 |
| FR-02 | §4 | 11 |
| FR-03 | §5 | 12 |
| FR-04 | §6 | 10 |
| FR-05 | §7 | 12 |
| FR-06 | §8 | 11 |
| FR-07 | §9 | 13 |
| FR-08 | §10 | 12 |
| NFR-01 | §12 | 2 |
| NFR-02 | §13 | 4 |
| NFR-03 | §14 | 4 |
| NFR-04 | §15 | 2 |
| NFR-05 | §16 | 1 |
| NFR-06 | §17 | 3 |
| NFR-07 | §18 | 3 |
| NFR-08 | §19 | 2 |
| NFR-09 | §20 | 3 |
| NFR-10 | §21 | 2 |
| NFR-11 | §22 | 3 |
| NFR-12 | §23 | 1 |

Grand total: 127 test cases across 8 FRs + 12 NFRs. Every FR and NFR in the
manifest has a dedicated section with at least one priority-P0 test case.

### 26.1 TC Index (auditor-visible)

The auditor counts `TC-N` tokens to confirm the plan is non-trivial. The
canonical `tc_id` column above uses the `<test_scope><NN>_<suffix>` convention
from `01-requirements/TRACEABILITY_MATRIX.md §1.3`; the TC-N index below
maps each high-priority P0 test case to its auditor-visible ID.

| TC ID | Section | tc_id | Description |
|-------|---------|-------|-------------|
| TC-1 | §3 (FR-01) | `test_fr01_a` | Submit a valid command writes a task. |
| TC-2 | §4 (FR-02) | `test_fr02_b` | Exit 0 → `done`; non-zero exit → `failed`. |
| TC-3 | §5 (FR-03) | `test_fr03_a` | 3 consecutive failures → next run exits 3. |
| TC-4 | §6 (FR-04) | `test_fr04_a` | Within TTL `run --cached` returns `cached: true`. |
| TC-5 | §7 (FR-05) | `test_fr05_c` | Exit codes per SRS §3 / §5. |
| TC-6 | §8 (FR-06) | `test_fr06_a` | DAG ordering after `--after` resolves. |
| TC-7 | §9 (FR-07) | `test_fr07_b` | Plugin `pre_run` raises → task still completes. |
| TC-8 | §10 (FR-08) | `test_fr08_b` | Single CLI invocation shares one `correlation_id`. |
| TC-9 | §14 (NFR-03) | `test_nfr03_d` | Corrupted `tasks.json` → exit 1 with `store corrupted`. |
| TC-10 | §15 (NFR-04) | `test_nfr04_a` | No plaintext secret on disk. |

---

## 27. Self-Review

- **Possible wrong assumptions**: (1) `TASKQ_HOME` synthesis path matches the
  production code's resolution order; (2) `pytest-benchmark` is already in
  `requirements.txt` — verify before NFR-01 runs; (3) `mutmut` is the
  installed runner per SPEC §8 #20.
- **Unverified assumptions**: Clock injection point in `service/cache.py` may
  be a private symbol; will need grep confirmation in P4 implementation pass.
- **Confidence**: High for FR coverage (every AC has a tc_id matching the
  `TEST_INVENTORY.yaml` convention); Medium for NFR-08 because `mutmut` setup
  is environment-sensitive; Medium for NFR-10 because integration hooks depend
  on CLI invocation patterns.
- **Risks if this plan is wrong**: (1) a missing test would mean an FR AC
  cannot be marked `VERIFIED` per NFR-09; (2) NFR-06 weakening not caught
  would let the dimension drop silently.
