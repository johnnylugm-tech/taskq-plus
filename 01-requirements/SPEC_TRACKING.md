# Specification Tracking Matrix — taskq-plus

> Phase-1 requirements-tracking matrix (Agent A, Round 1).
> H1 anchor: **"Specification Tracking Matrix"** — required by the
> orchestrator loader; any deviation fails the load step.
>
> Canonical spec source: bare `SPEC.md` at repo root
> (`/Users/johnny/projects/taskq-plus/SPEC.md`, v1.0.0, 2026-07-30).
> This file is the **human-readable view**; the authoritative status
> column is refreshed by `harness build_traceability` on
> `advance-phase` (IN_PROGRESS when code/module exists, VERIFIED when
> code + test both exist). Score authority lives in
> `quality_manifest.json`, NOT here.

---

## 1. Project Info

| Field | Value |
|-------|-------|
| Project Name | taskq-plus |
| Round | Round 1 / 3 (Python CLI; Round 2 = `SPEC-2.md`; Round 3 = TypeScript, deferred) |
| Version | v1.0.0 |
| Created | 2026-07-30 |
| Canonical Spec | `SPEC.md` (repo root, SSOT) |
| SRS | `01-requirements/SRS.md` (APPROVED — DO NOT MODIFY) |
| Traceability | `01-requirements/TRACEABILITY_MATRIX.md` |

---

## 2. Specification Status

> The **Status** column is **machine-refreshed** by `advance-phase` from
> `build_traceability`'s live code/test scan: `IN_PROGRESS` once code
> or module exists, `VERIFIED` once code + test both exist. A
> hand-edit is overwritten on the next advance. Fill the **semantic**
> columns (Spec Description / Intent Class / Decision Framework /
> Notes); leave Status for the harness.
>
> Seed value placed here is `DRAFT` so the matrix is renderable even
> before the first `advance-phase` run.

### 2.1 FR Inventory

| FR ID | Spec Description | Intent Class | Decision Framework | Status | Notes |
|-------|------------------|--------------|--------------------|--------|-------|
| FR-01 | `submit "<cmd>" [--name N] [--after ID...]` — pydantic `TaskSubmission` validates non-empty/length/`;\|& $ > < \`` injection blacklist/name uniqueness/dep existence; on pass generate 8-hex uuid, write pending to `tasks.json` atomically, emit `submit` audit event, exit 0. | Input contract / form validation | pydantic `TaskSubmission` rule-set + injection-character blacklist (`C-08`, NFR-02) + uuid4 first-8-hex + atomic write (`C-07`) | DRAFT | AC: AC-FR-01.a..d (zero-hit grep + per-char coverage). Source: `SPEC.md` §3 FR-01. |
| FR-02 | `run <id>` / `run --all` — `subprocess.run(shlex.split(command), …, timeout=TASKQ_TASK_TIMEOUT)`, no `shell=True`; state machine `pending → running → done\|failed\|timeout\|blocked`; result fields `exit_code` / `stdout_tail` (last 2000) / `stderr_tail` (last 2000) / `duration_ms` / `finished_at`; `--all` uses `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)` with shared `threading.Lock` over the store. | Task execution + concurrency | subprocess + shlex.split + state-machine mapping (exit-0→`done`, non-zero→`failed`, `TimeoutExpired`→`timeout`, dep-unsatisfied→`blocked`) + ThreadPoolExecutor DAG scheduling (FR-06) | DRAFT | AC: AC-FR-02.a..e; single-task timeout → exit 4. Source: `SPEC.md` §3 FR-02. |
| FR-03 | Retry on `failed`/`timeout` up to `TASKQ_RETRY_LIMIT` with `TASKQ_BACKOFF_BASE × 2^n` exponential backoff (sleep injectable); global circuit breaker `CLOSED → OPEN` when consecutive final failures ≥ `TASKQ_BREAKER_THRESHOLD`; `OPEN` rejects `run` (exit 3 + stderr `breaker open`); after `TASKQ_BREAKER_COOLDOWN` → `HALF_OPEN`, admit one probe, success→`CLOSED`, failure→`OPEN`; state persisted atomically to `breaker.json`. | Resilience (retry + circuit breaker) | exponential backoff with injectable sleep + breaker state machine (CLOSED/OPEN/HALF_OPEN) + cross-task cross-process counter + atomic `breaker.json` write | DRAFT | AC: AC-FR-03.a..d (recovery bounded by `TASKQ_BREAKER_COOLDOWN` + 1s). Source: `SPEC.md` §3 FR-03. |
| FR-04 | TTL result cache — signature `sha256(command)`; `run <id> --cached` replays within `TASKQ_CACHE_TTL` (no subprocess, `cached: true`); miss/expired → normal execution → write `done` result to `cache.json`; reads/writes atomic + thread-safe. | Performance optimisation (replay cache) | sha256 command-fingerprint + TTL bound + atomic `cache.json` write + thread-safe coexistence with FR-02 | DRAFT | AC: AC-FR-04.a..d; see Open Issue NFR-99-02 on whether `duration_ms`/`finished_at` are replayed or fresh. Source: `SPEC.md` §3 FR-04. |
| FR-05 | `click` command-grouped CLI: `submit` / `run` / `status` / `list` / `graph` / `plugins list` / `export` / `clear`; global `--json` flag = single-line JSON; exit-code map `0` success / `2` validation (incl. unknown id) / `3` breaker open / `4` task timeout / `5` cycle or depth exceeded / `6` plugin load failure / `1` other internal error. | Interface contract (CLI surface) | click subcommand table + `--json` JSON serialisation + exit-code map (`SPEC.md` §3 / §7) | DRAFT | AC: AC-FR-05.a..d (incl. 3-format export). Source: `SPEC.md` §3 FR-05. |
| FR-06 | `submit --after` repeatable builds `depends_on`; `run --all` uses Kahn topological sort (same in-degree-0 layer may run concurrently); unsatisfied deps → downstream marked `blocked`, not executed, not breaker-counted; cycle detection rejects submission (exit 5 + cycle path `A → B → C → A`); depth cap `TASKQ_MAX_DAG_DEPTH` rejects chain depth > max (exit 5); `graph --format dot\|text` outputs Graphviz DOT or indented tree. | Dependency DAG / graph scheduling | Kahn topological sort (layer-by-layer concurrency) + cycle rejection (exit 5 with cycle path) + depth cap (exit 5) + `dot` / `text` graph renderer | DRAFT | AC: AC-FR-06.a..d; see Open Issue NFR-99-01 for `submit --after <blocked-id>` semantics. Source: `SPEC.md` §3 FR-06. |
| FR-07 | Plugin hook system — `pre_run(task)` / `post_run(task, result)`; loaded by `importlib.import_module` from the `TASKQ_PLUGINS` allowlist (comma-separated); module name must match `^[A-Za-z_][A-Za-z0-9_.]*$` (else exit 6); forbid `eval` / `exec` / path / URL load; plugin exception must not abort task execution (record `plugin_error`, continue); 3 consecutive failures within one run → disable plugin for that run; `plugins list` prints module / hooks / load status. | Plugin extensibility (security-critical loader) | allowlist regex + `importlib` by name only + exception isolation + 3-strike disable + `plugins list` reporting | DRAFT | AC: AC-FR-07.a..e; security iron rules (NFR-02). Source: `SPEC.md` §3 FR-07. |
| FR-08 | Audit log to `$TASKQ_AUDIT_LOG` (default `$TASKQ_HOME/audit.jsonl`) — JSON Lines, append-only with fsync; fields `ts` (ISO-8601 UTC) / `event` / `task_id` / `correlation_id` / `detail`; one `correlation_id` per CLI invocation; event types `submit` / `run_start` / `run_end` / `retry` / `breaker_open` / `breaker_close` / `cache_hit` / `blocked` / `plugin_error`; NFR-04 redaction applied before write; export `--format json\|csv\|md` with consistent field set + count. | Observability (audit + export) | JSONL append + fsync + per-invocation correlation_id + NFR-04 redaction-before-write + 3-format export with field-set consistency | DRAFT | AC: AC-FR-08.a..d; redaction gates audit write. Source: `SPEC.md` §3 FR-08. |

### 2.2 NFR Inventory

| NFR ID | Spec Description | Intent Class (dimension) | Decision Framework | Status | Notes |
|--------|------------------|--------------------------|--------------------|--------|-------|
| NFR-01 | `submit + status` combined (excl. subprocess) 100 iter → p95 < 50ms; `run --all` topo-sort phase (excl. subprocess) for 200 tasks → p95 < 200ms; measured by `pytest-benchmark`. | Performance budget (`performance`) | pytest-benchmark (two named scenarios) — verbatim canonical budgets | DRAFT | AC: AC-NFR-01.a..b; Open Issue NFR-99-03 defines the subprocess-exclusion window. Source: `SPEC.md` §4 NFR-01. |
| NFR-02 | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` → 0 hits; per-char injection coverage (`; \| & $ > < \``); plugin name `^[A-Za-z_][A-Za-z0-9_.]*$` allowlist regex; path / URL form rejected (exit 6); `bandit -r 03-development/src/` → 0 HIGH / 0 MEDIUM. | Security (exec-time + load-time) | grep-zero + per-char test cases + allowlist regex + `bandit` H/M = 0 | DRAFT | AC: AC-NFR-02.a..d. Source: `SPEC.md` §4 NFR-02. |
| NFR-03 | Four data files atomic-write (tmp + `os.replace`; audit append + fsync); interrupted process → files remain valid JSON / JSONL; corrupted `tasks.json` (invalid JSON) at startup → exit 1 + stderr `store corrupted` (NO silent rebuild); no bare `except:` / `except Exception: pass` / swallowing `KeyboardInterrupt` / `SystemExit`; every `except` re-raise / translate / record-then-exit; breaker `OPEN → CLOSED` recovery ≤ `TASKQ_BREAKER_COOLDOWN` + 1s. | Atomicity + error-handling integrity (`error_handling`) | tmp + `os.replace` + audit fsync + post-crash JSON validity check (exit 1 `store corrupted`) + no-swallowing rule + recovery-time bound | DRAFT | AC: AC-NFR-03.a..d. Source: `SPEC.md` §4 NFR-03. |
| NFR-04 | Before write, lines matching `(sk-[A-Za-z0-9_-]{8,}\|token=\S+\|Bearer\s+\S+)` wholly replaced by `[REDACTED]` — applies to `stdout_tail` / `stderr_tail` / audit `detail`; redaction BEFORE disk write (asserted by file-content grep, not post-load string). | Secret redaction (`security`) | pre-write redaction regex + grep-zero assertion on file contents | DRAFT | AC: AC-NFR-04.a..b; AC overlaps with FR-08.a audit-write path. Source: `SPEC.md` §4 NFR-04. |
| NFR-05 | All public functions/classes under `03-development/src/taskq_plus` carry a docstring with `[FR-XX]` or `[NFR-XX]` reference; coverage **100%** measured by `ast-docstrings`. | Docstring coverage (`documentation`) | ast-docstrings 100% public-symbol coverage with `[FR-XX]` / `[NFR-XX]` tags | DRAFT | AC: AC-NFR-05.a. Source: `SPEC.md` §4 NFR-05. |
| NFR-06 | Project root contains `.importlinter` declaring `cli > observability > service > storage > models`; upper may import lower, lower must NOT import upper; `config` independent (any layer may import it; it must not import any layer); `lint-imports` must exit 0; forbidden to weaken by deleting `.importlinter`, widening `ignore_imports`, or degrading to a single `forbidden` entry. | Architecture layering (`architecture_constraints`) | import-linter layers contract + `lint-imports` exit-0 + anti-weakening clause | DRAFT | AC: AC-NFR-06.a..c; prior-round gap (tool_runner returns exit 0 when contract absent). Source: `SPEC.md` §4 NFR-06. |
| NFR-07 | All runtime deps pinned `==` in `requirements.txt`; allowed licenses MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0; scan MUST include installed dependency tree (e.g. `pip-licenses --format=json --with-urls` or `scancode --license <venv>/lib/python3.11/site-packages …`); SBOM emitted to `08-config/SBOM.json` with `name` / `version` / `license`. | License + dependency hygiene (`license_compliance`) | `==` pinning + allowed-license set + dependency-tree scan (NOT own src only) + SBOM JSON to `08-config/SBOM.json` | DRAFT | AC: AC-NFR-07.a..c. Source: `SPEC.md` §4 NFR-07. |
| NFR-08 | `.methodology/harness_config.json` sets `features.mutation_testing: true`; `mutmut run` + `mutmut results` reports mutation score ≥ 70; scope limited to `03-development/src/taskq_plus/service/` and `.../storage/` (budget rationale in harness_config.json). | Mutation score (`mutation_testing`) | harness_config flag + mutmut score ≥ 70 + scope restriction with annotated rationale | DRAFT | AC: AC-NFR-08.a..b. Source: `SPEC.md` §4 NFR-08. |
| NFR-09 | `pytest 03-development/tests -q` exits 0 with **0 skipped**; no `pytest.skip` / `pytest.mark.skip` / `skipif` / `xfail` / assertion-free stubs; each test has ≥ 1 `assert` (`ast-assertions` `zero_assert == 0`); no `--ignore` / `-k` / `--deselect` / `collect_ignore` / `testpaths` exclusion to reach the numbers; `TRACEABILITY_MATRIX.md` `VERIFIED` only when the test actually ran and passed (else `NOT_VERIFIED`). | Test authenticity (`test_assertion_quality`) | zero-skip + zero-stub + `ast-assertions == 0` + anti-fabrication clause | DRAFT | AC: AC-NFR-09.a..c. Source: `SPEC.md` §4 NFR-09. |
| NFR-10 | `03-development/tests/integration/` cross-module tests, line coverage ≥ 80%; integration tests drive through CLI entry (`python -m taskq_plus`) or `click.testing.CliRunner` (NOT direct internal calls); minimum flows: submit→run→status full chain, DAG multi-layer execution, breaker open/close, cache hit, plugin hook trigger, three export formats. | Integration coverage (`integration_coverage`) | CLI/CliRunner-driven integration tests + ≥ 80% line cov + named flow list | DRAFT | AC: AC-NFR-10.a. Source: `SPEC.md` §4 NFR-10. |
| NFR-11 | Project MI (LLOC-weighted) ≥ 80; single-function CC ≤ 10; single file ≤ 400 lines; single directory ≤ 15 files. | Readability metrics (`readability`) | MI ≥ 80 + per-fn CC ≤ 10 + file ≤ 400 LOC + dir ≤ 15 files | DRAFT | AC: AC-NFR-11.a..c. Source: `SPEC.md` §4 NFR-11. |
| NFR-12 | `Makefile` provides `verify-system` target chaining: full test suite + CLI smoke (submit / run / status / graph / export / clear); `make verify-system` exit 0 + stdout `verify-system: PASS`. | System verification target (`execute_verification_target`) | Makefile target + smoke-chain + exit-0 + `verify-system: PASS` literal | DRAFT | AC: AC-NFR-12.a; SPEC §8 #21. Source: `SPEC.md` §4 NFR-12. |

### 2.3 Completeness Check

| Check | Target | State | Status |
|-------|--------|-------|--------|
| FR rows emitted (FR-01..FR-08) | 8 | 8 | Done |
| NFR rows emitted (NFR-01..NFR-12) | 12 | 12 | Done |
| Each row has Spec Description / Intent Class / Decision Framework / Notes | 100% | 100% | Done |
| Every row cites `SPEC.md` (root SSOT) | 100% | 100% | Done |
| Forbidden path `01-requirements/SPEC.md` absent | 0 hits | 0 hits | Done |
| Forbidden Gates-score column absent | 0 hits | 0 hits | Done |

---

## 3. Forward References (legal framework filenames)

> The harness `check_forward_refs` gate (artifact_consistency.py)
> blocks any invented filename. Only the legal filenames per stage
> may be referenced. **NEVER** invent filenames like `ARCHITECTURE.md`
> for the P2 architecture deliverable — use `SAD.md`.

| Phase | File (legal) | Used by this matrix? |
|-------|--------------|----------------------|
| 01-requirements | `SPEC.md` (canonical, root) | yes (bare `SPEC.md`) |
| 01-requirements | `01-requirements/SRS.md` | yes (Source of Truth for AC/cite) |
| 01-requirements | `01-requirements/SPEC_TRACKING.md` | yes (this file) |
| 01-requirements | `01-requirements/TRACEABILITY_MATRIX.md` | yes (NFR-09 + cross-link) |
| 01-requirements | `01-requirements/TEST_INVENTORY.yaml` | downstream P3 |
| 02-architecture | `02-architecture/SAD.md` | downstream P2 |
| 02-architecture | `02-architecture/ADR.md` | downstream P2 |
| 02-architecture | `02-architecture/TEST_SPEC.md` | downstream P2 |
| 04-testing | `04-testing/TEST_PLAN.md` | downstream P4 |
| 04-testing | `04-testing/TEST_RESULTS.md` | downstream P4 |
| 05-verification | `05-verification/BASELINE.md` | downstream P5 |
| 05-verification | `05-verification/VERIFICATION_REPORT.md` | downstream P5 |
| 06-quality | `06-quality/QUALITY_REPORT.md` | downstream P6 |
| 06-quality | `06-quality/FINAL_SIGN_OFF.md` | downstream P6 |
| 06-quality | `06-quality/RELEASE_NOTES.md` | downstream P6 |
| 07-risk | `07-risk/RISK_REGISTER.md` | downstream P7 |
| 07-risk | `07-risk/RISK_MITIGATION_PLANS.md` | downstream P7 |
| 07-risk | `07-risk/RISK_STATUS_REPORT.md` | downstream P7 |
| 08-config | `08-config/CONFIG_RECORDS.md` | downstream P8 |
| 08-config | `08-config/RELEASE_CHECKLIST.md` | downstream P8 |
| 08-config | `08-config/SBOM.json` | downstream P8 (FR-07 / NFR-07 target) |

---

## 4. Owner / Hand-off Map

| Track | Owner (Role) | Hand-off deliverable |
|-------|--------------|----------------------|
| Architecture split (5-layer) | Architect (Agent B for `SAD.md`) | `02-architecture/SAD.md`, `02-architecture/ADR.md`, `02-architecture/TEST_SPEC.md` |
| Implementation + tests | Implementer (Agent for `03-development/`) | code + tests satisfying ACs in §2.1 / §2.2 |
| Quality gate | Quality Lead (Agent for `06-quality/`) | `06-quality/QUALITY_REPORT.md`, `06-quality/FINAL_SIGN_OFF.md`, `06-quality/RELEASE_NOTES.md` |
| Risk | Risk Lead (Agent for `07-risk/`) | `07-risk/RISK_REGISTER.md`, `07-risk/RISK_MITIGATION_PLANS.md`, `07-risk/RISK_STATUS_REPORT.md` |
| Config / Release | Release Lead (Agent for `08-config/`) | `08-config/CONFIG_RECORDS.md`, `08-config/RELEASE_CHECKLIST.md`, `08-config/SBOM.json` |
| Status refresh | Harness (`build_traceability` on `advance-phase`) | overwrites Status column |

---

## 5. Update log

| Date | Change | By |
|------|--------|----|
| 2026-07-30 | Initial creation; placeholder template replaced with full FR-01..FR-08 + NFR-01..NFR-12 matrix using standard columns (Spec Description / Intent Class / Decision Framework / Status / Notes); all `SPEC.md` citations use root bare `SPEC.md`; legal downstream filenames listed; Status left to machine-refresh. | Agent A (Requirements Engineer) |
