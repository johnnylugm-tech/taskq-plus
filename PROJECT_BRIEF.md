# Project Brief — taskq-plus

## canonical_spec
SPEC.md (v1.0.0, 2026-07-30, 8 FR / **12 NFR** / 12 env vars)

## Project Domain
Local task queue CLI tool: submit shell commands as tasks; run with
controlled concurrency, timeout, retry, circuit breaker, TTL result cache
and **dependency DAG ordering**; extend behaviour through an allowlisted
**plugin hook** system; emit a **structured JSONL audit trail** and export
results as json / csv / markdown.

## Stakeholders
- Project owner / product manager: johnnylugm-tech
- Integration test target: harness-methodology pipeline validation —
  **progressive test-bed round 1 of 3** (round 2 = `SPEC-2.md` backend+DB;
  round 3 = TypeScript, deferred)

## Business Goals
- Provide a reliable local task queue CLI (`taskq-plus`) supporting submit,
  run (single / all / cached), dependency graph, status, list, plugins,
  export and clear
- Demonstrate the full Phase 1–8 harness-methodology pipeline on a real
  small project with a layered architecture (8 FR covering concurrency,
  circuit breaker, TTL cache, dependency DAG, plugin loading, audit trail)
- **Exercise the quality dimensions the previous test-bed could not**:
  real pinned third-party dependencies (license_compliance), an enforced
  layering contract (architecture_constraints), mutation testing switched
  on (mutation_testing), and a zero-skip verification rule
  (test_assertion_quality)

## Why this project exists (test-bed intent)

The previous test-bed (`taskq`) passed Gate 4 with composite 97.4, but a
post-hoc audit found **five quality dimensions that could not produce a
signal in that project shape**. Each is addressed by an explicit clause here:

| Previous gap | This round's countermeasure | Clause |
|---|---|---|
| Zero runtime dependencies → `license_compliance` scanned 19 own files, always 100 | Pinned third-party deps; **scan scope must include the installed dependency tree** | NFR-07 |
| No `.importlinter` → `architecture_constraints` (Gate 1 weight 0.25) scored a free 100 | Layering contract is **mandatory**; `lint-imports` must actually run | NFR-06 |
| `mutation_testing` defaulted off → null in all three gates | Explicitly enabled with a score floor | NFR-08 |
| 15 of 16 advanced-NFR tests were `pytest.skip` stubs yet marked VERIFIED | **Zero-skip rule** + anti-fabrication clause | NFR-09 |
| Single flat package (21 CRG nodes) → `architecture` always 100, and `crg_cohesion_healthy` was lowered to accommodate it | Five-layer architecture; **lowering the CRG calibration is forbidden** | NFR-06 / §10 |

## Key Constraints
- **Technical**: Python 3.11; `python -m taskq_plus` CLI entry; `click`
  command groups (FR-05); `pydantic` v2 validation models (FR-01);
  `shell=True` is forbidden everywhere (NFR-02); `ThreadPoolExecutor`
  for `run --all` with shared `threading.Lock` over store (FR-02)
- **Dependencies**: real third-party deps, every one pinned with `==` in
  `requirements.txt`; licenses restricted to MIT / BSD-2-Clause /
  BSD-3-Clause / Apache-2.0; SBOM emitted to `08-config/SBOM.json` (NFR-07)
- **Architecture**: five layers `cli > observability > service > storage >
  models` enforced by a mandatory `.importlinter` layers contract;
  `config` is an independence module (NFR-06)
- **Atomicity**: all four data files (`tasks.json`, `breaker.json`,
  `cache.json`, `audit.jsonl`) written atomically (tmp + `os.replace`;
  audit appends with fsync); mid-write crash must leave valid JSON/JSONL
  (NFR-03)
- **Security**: injection character blacklist (`; | & $ > < \``) on
  `submit` (NFR-02); plugin loading restricted to an env-var allowlist of
  module names matching `^[A-Za-z_][A-Za-z0-9_.]*$` — no `eval`, no `exec`,
  no path or URL loading (FR-07 / NFR-02); secret-line redaction before
  write on `stdout_tail` / `stderr_tail` / audit `detail` (NFR-04)
- **Dependency DAG**: `run --all` executes in Kahn topological order;
  cycles are rejected at submit time (exit 5) with the cycle path printed;
  dependency chain depth is capped by `TASKQ_MAX_DAG_DEPTH` (FR-06)
- **Plugin isolation**: a plugin raising must not abort task execution —
  record a `plugin_error` audit event and continue; disable a plugin after
  3 consecutive failures within one run (FR-07)
- **Verification honesty**: no FR/NFR may be verified by a skipped or
  assertion-free test; `pytest -q` must report **0 skipped**; excluding
  tests via `--ignore` / `-k` / `--deselect` / `collect_ignore` to reach
  that number is forbidden; `TRACEABILITY_MATRIX.md` may only say
  `VERIFIED` when the test actually ran and passed (NFR-09)
- **Performance**: `submit` + `status` combined p95 < 50ms over 100
  iterations; topological sort of 200 tasks p95 < 200ms (NFR-01)

## FR Inventory (canonical: SPEC.md §3)

| ID | Title | Section |
|----|-------|---------|
| FR-01 | 任務提交與驗證 | pydantic `TaskSubmission`; empty / length / injection / name-unique / dependency-exists |
| FR-02 | 任務執行器 | subprocess.run + ThreadPoolExecutor `--all` in DAG order + thread-safe store |
| FR-03 | 重試與斷路器 | exponential backoff + OPEN/HALF_OPEN/CLOSED state machine |
| FR-04 | 結果 TTL 快取 | sha256(command) cache, atomic + thread-safe write |
| FR-05 | CLI 整合 | click groups + `--json` + 7 exit codes |
| FR-06 | 任務相依 DAG | Kahn topological sort, cycle rejection (exit 5), depth cap, `graph` output |
| FR-07 | Plugin Hook 系統 | allowlisted `importlib` load, `pre_run` / `post_run`, exception isolation |
| FR-08 | 結構化稽核日誌與匯出 | JSONL audit with `correlation_id`; export json / csv / md |

## NFR Inventory (canonical: SPEC.md §4)

> Every `dimension` below is a real key in
> `harness/toolchains/registry.py::DIMENSION_TOOLS["python"]`. The previous
> test-bed labelled its NFR-06 `deployability` — not a valid dimension — and
> the row was silently dropped from the NFR→dimension mapping (10 declared,
> 9 mapped).

| ID | dimension | Requirement |
|----|-----------|-------------|
| NFR-01 | `performance` | submit+status p95 < 50ms (100 iter); topo-sort p95 < 200ms (200 tasks) |
| NFR-02 | `security` | no `shell=True` / `eval(` / `exec(`; injection blacklist tested per character; plugin name allowlist regex; bandit 0 HIGH / 0 MEDIUM |
| NFR-03 | `error_handling` | four data files atomic; no bare `except:` / `except Exception: pass`; every handler re-raises, translates, or exits with a definite code |
| NFR-04 | `security` | redaction before write on stdout_tail / stderr_tail / audit detail |
| NFR-05 | `documentation` | 100% public docstrings carrying `[FR-XX]` / `[NFR-XX]` |
| NFR-06 | `architecture_constraints` | **mandatory** `.importlinter` layers contract; `lint-imports` exit 0; weakening the contract to pass is forbidden |
| NFR-07 | `license_compliance` | deps pinned `==`; license allowlist; **scan scope includes the installed dependency tree**; SBOM emitted |
| NFR-08 | `mutation_testing` | `features.mutation_testing: true`; mutation score ≥ 70 over service/ + storage/ |
| NFR-09 | `test_assertion_quality` | **0 skipped tests**, 0 assertion-free test functions, anti-fabrication clause, VERIFIED only when the test really ran |
| NFR-10 | `integration_coverage` | integration line coverage ≥ 80%, driven through the CLI entry point |
| NFR-11 | `readability` | project MI ≥ 80; per-function CC ≤ 10; ≤ 400 lines/file; ≤ 15 files/dir |
| NFR-12 | `execute_verification_target` | `make verify-system` exit 0 printing `verify-system: PASS` |

## Env Var Inventory (canonical: SPEC.md §5.1 + .env.example)

| Variable | Default | Purpose |
|----------|---------|---------|
| `TASKQ_HOME` | `.taskq` | data file directory |
| `TASKQ_MAX_WORKERS` | `4` | `run --all` concurrent worker count |
| `TASKQ_TASK_TIMEOUT` | `10.0` | per-task subprocess timeout (seconds) |
| `TASKQ_RETRY_LIMIT` | `2` | retry cap on failed/timeout tasks |
| `TASKQ_BACKOFF_BASE` | `0.1` | exponential backoff base (seconds) |
| `TASKQ_BREAKER_THRESHOLD` | `3` | consecutive final failures before breaker OPEN |
| `TASKQ_BREAKER_COOLDOWN` | `5.0` | OPEN → HALF_OPEN cooldown (seconds) |
| `TASKQ_CACHE_TTL` | `3600` | TTL for cached task results (seconds) |
| `TASKQ_MAX_DAG_DEPTH` | `32` | dependency chain depth cap (FR-06) |
| `TASKQ_PLUGINS` | (empty) | comma-separated plugin module allowlist (FR-07) |
| `TASKQ_AUDIT_LOG` | `$TASKQ_HOME/audit.jsonl` | audit trail path (FR-08) |
| `TASKQ_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |

## Data Files (canonical: SPEC.md §5.2)

| File | Content | FR | Write mode |
|------|---------|----|----|
| `$TASKQ_HOME/tasks.json` | `{version:1, tasks:{id→fields incl. depends_on}}` | FR-01/02/06 | atomic |
| `$TASKQ_HOME/breaker.json` | `{version:1, state, failure_count, opened_at}` | FR-03 | atomic |
| `$TASKQ_HOME/cache.json` | `{version:1, entries:{sig→done result + cached_at}}` | FR-04 | atomic |
| `$TASKQ_AUDIT_LOG` | one JSON object per line | FR-08 | append + fsync |

## Module Layout (canonical: SPEC.md §6)

```
03-development/src/taskq_plus/
├── __init__.py
├── __main__.py            # python -m taskq_plus entry
├── config.py              # TASKQ_* env (independence module)
├── models/                # L1 — no internal deps
│   ├── task.py            # pydantic models (FR-01)
│   └── errors.py          # domain exceptions (NFR-03)
├── storage/               # L2 — depends on models
│   ├── atomic.py          # tmp + os.replace (NFR-03)
│   ├── task_store.py      # tasks.json (FR-01/02) — high-risk
│   ├── breaker_store.py   # breaker.json (FR-03)
│   └── cache_store.py     # cache.json (FR-04)
├── service/               # L3 — depends on storage + models
│   ├── executor.py        # subprocess + retry (FR-02/03) — high-risk
│   ├── breaker.py         # circuit breaker (FR-03)
│   ├── cache.py           # TTL cache (FR-04)
│   ├── dag.py             # topological sort + cycle detection (FR-06)
│   └── plugins.py         # allowlist loading + hooks (FR-07) — high-risk
├── observability/         # L4
│   ├── audit.py           # JSONL audit + redaction (FR-08/NFR-04)
│   └── export.py          # json/csv/md export (FR-08)
└── cli/                   # L5 — top layer
    ├── main.py            # click group (FR-05)
    └── commands.py
```

Layering (enforced by `.importlinter`, NFR-06):
`cli > observability > service > storage > models`; `config` is independent.

## Exit Code Map (canonical: SPEC.md §3 / §7)

| Code | Meaning |
|------|---------|
| 0 | success |
| 2 | input validation error (incl. unknown task id / unknown dependency) |
| 3 | breaker OPEN |
| 4 | task timeout (single-task mode only) |
| 5 | dependency cycle or depth cap exceeded |
| 6 | plugin load failure |
| 1 | other internal error |

## Acceptance Criteria (canonical: SPEC.md §8)

22 acceptance items, **each a single machine-decidable command with an
expected output** — no prose criteria. Covers: full suite green with
**0 skipped**; 100% line coverage; ≥80% integration coverage; the CLI happy
path; six negative paths (empty / injection / timeout / breaker-open /
dependency cycle / illegal plugin name); cache replay; DAG ordering and
blocking; plugin exception isolation; three export formats; grep gates for
`shell=True` / `eval(` / `exec(`; `.env.example` completeness;
`lint-imports` exit 0; license allowlist; bandit clean; mutation score ≥ 70;
`make verify-system`; and no secret on disk in the audit trail.

## Risk Matrix (canonical: SPEC.md §9)

| ID | Risk | Mitigation |
|----|------|-----------|
| R1 | concurrent write corruption | Lock + atomic write (NFR-03) |
| R2 | subprocess hangs/zombies | timeout (FR-02) |
| R3 | breaker false-lock | cooldown + HALF_OPEN (FR-03) |
| R4 | cache stale results | TTL expiry forces re-execute (FR-04) |
| R5 | secret-on-disk leak | redaction before write (NFR-04) |
| R6 | **plugin becomes an arbitrary-code-execution entry point** | allowlist + name regex + no eval/exec/path (FR-07/NFR-02) |
| R7 | pathological dependency graph exhausts resources | cycle detection + depth cap (FR-06) |
| R8 | plugin exception aborts the main flow | exception isolation + disable after 3 failures (FR-07) |
| R9 | dependency with an incompatible license | pinning + allowlist + SBOM (NFR-07) |
| R10 | audit log grows without bound | append-only; rotation is the operator's job — **not implemented this round**, recorded as a known limitation |

## Source of Truth

All functional and non-functional requirements are fully specified in
`SPEC.md` (v1.0.0, 2026-07-30) at the project root — including the §10
framework alignment table and §11 monitoring thresholds.

Phase 1 workflow rules:
- Agent A must operate in INGESTION MODE: transcribe 100% of
  `### FR-01..FR-08` and `### NFR-01..NFR-12` headings from SPEC.md —
  no invention, no omission.
- TBD / TODO / `<placeholder>` markers from SPEC.md must be captured as
  `NFR-99` or `FR-XX-deferred` (not silently dropped).
- §10 framework alignment table is mandatory context for Phase 3 module
  scaffolding (high-risk modules `taskq_plus.service.executor`,
  `taskq_plus.service.plugins`, `taskq_plus.storage.task_store` require
  per-module TDD coverage).
- The §5.3 project-side config files (`.importlinter`, `requirements.txt`,
  `.env.example`, `harness_config.json`, `Makefile`) are **not optional** —
  they are the carriers of NFR-06 / NFR-07 / NFR-08 / NFR-12 and their
  absence silently turns those dimensions back into free points.
