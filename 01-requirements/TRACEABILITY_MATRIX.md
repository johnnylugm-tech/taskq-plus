# Traceability Matrix — taskq-plus

> Phase-1 deliverable (Agent A, Round 1).
> H1 anchor: **"Traceability Matrix"** — required by the orchestrator
> loader; any deviation fails the load step.
>
> Project: **taskq-plus** v1.0.0 (2026-07-30).
> Canonical spec: `SPEC.md` (repo root, SSOT).
> Source SRS: `01-requirements/SRS.md` (APPROVED — DO NOT MODIFY).
> Status refresh: harness `build_traceability` on `advance-phase`.

---

## 1. Scope and Conventions

### 1.1 Bidirectional Traceability

This matrix provides bidirectional linkage across the four
traceability levels required by ASPICE SWE.3.B / SYS.4:

```
FR/NFR  →  SRS section  →  Acceptance Criterion (AC)  →  Test Function  →  Code Module
```

Trace direction:

| Direction | Where in this matrix |
|-----------|----------------------|
| Forward (requirement → implementation) | §2, §3, §4 |
| Backward (test → requirement) | §5 (AC ↔ Test ID reverse lookup) |

### 1.2 Status Column

The **Status** column is **machine-refreshed** by
`harness build_traceability` on `advance-phase`:

| Status | Meaning |
|--------|---------|
| `DRAFT` | requirement captured; code / test pending (Phase 1 default) |
| `IN_PROGRESS` | code or module exists; test pending |
| `VERIFIED` | code + test both exist and the test actually ran and passed (per NFR-09 anti-fabrication clause) |
| `NOT_VERIFIED` | test does not exist or did not run — **forbidden** to mark `VERIFIED` |

Seed value for every row in this Round-1 matrix is `DRAFT`. Hand-edits
to Status are overwritten on the next `advance-phase`; the semantic
columns are the only authoritative manual content.

### 1.3 Planned Test ID Naming

Per project convention (`CLAUDE.md`), test function names follow
`test_<scope><NN>_<suffix>`:

- `test_frNN_<x>` for FR-NN-derived tests
- `test_nfrNN_<x>` for NFR-NN-derived tests

Suffix `<x>` uses the same letter as the originating AC
(AC-FR-NN.x → `test_frNN_x`; AC-NFR-NN.x → `test_nfrNN_x`), so the AC ↔
test link is single-grep recoverable.

---

## 2. Functional Requirements Traceability

| FR ID | FR Description | SRS § | AC IDs | Planned Test IDs | Code Module (P3) | Test File (P3) | Status |
|-------|----------------|-------|--------|------------------|------------------|----------------|--------|
| FR-01 | `submit "<cmd>"` + pydantic `TaskSubmission` validation (non-empty / length / injection blacklist / name uniqueness / dep existence) → uuid4 first-8-hex + atomic write + `submit` audit event | §3 FR-01 | AC-FR-01.a, .b, .c, .d | test_fr01_a, test_fr01_b, test_fr01_c, test_fr01_d | `03-development/src/taskq_plus/service/submission.py` | `03-development/tests/test_submission.py` | DRAFT |
| FR-02 | `run <id>` / `run --all` — `subprocess.run(shlex.split, …, timeout)` no `shell=True`; state machine `pending → running → done\|failed\|timeout\|blocked`; result fields incl. `stdout_tail` / `stderr_tail` last 2000 chars; `ThreadPoolExecutor` + shared `threading.Lock` for `--all` | §3 FR-02 | AC-FR-02.a, .b, .c, .d, .e | test_fr02_a, test_fr02_b, test_fr02_c, test_fr02_d, test_fr02_e | `03-development/src/taskq_plus/service/executor.py` | `03-development/tests/test_executor.py` | DRAFT |
| FR-03 | Retry on `failed`/`timeout` ≤ `TASKQ_RETRY_LIMIT` with exponential backoff `TASKQ_BACKOFF_BASE × 2^n` (sleep injectable); circuit breaker `CLOSED → OPEN` at `TASKQ_BREAKER_THRESHOLD` consecutive failures; `OPEN` rejects with exit 3 + stderr `breaker open`; `OPEN → HALF_OPEN → CLOSED` after `TASKQ_BREAKER_COOLDOWN`; atomic `breaker.json` | §3 FR-03 | AC-FR-03.a, .b, .c, .d | test_fr03_a, test_fr03_b, test_fr03_c, test_fr03_d | `03-development/src/taskq_plus/service/breaker.py` | `03-development/tests/test_breaker.py` | DRAFT |
| FR-04 | TTL result cache — signature `sha256(command)`; `run <id> --cached` replays within `TASKQ_CACHE_TTL` (no subprocess, `cached: true`); miss / expired → normal execution → write `done` to `cache.json`; atomic + thread-safe | §3 FR-04 | AC-FR-04.a, .b, .c, .d | test_fr04_a, test_fr04_b, test_fr04_c, test_fr04_d | `03-development/src/taskq_plus/service/cache.py` | `03-development/tests/test_cache.py` | DRAFT |
| FR-05 | `click` command-grouped CLI: `submit` / `run` / `status` / `list` / `graph` / `plugins list` / `export` / `clear`; global `--json` flag (single-line JSON); exit-code map 0/2/3/4/5/6/1 per SPEC §3 / §7 | §3 FR-05 | AC-FR-05.a, .b, .c, .d | test_fr05_a, test_fr05_b, test_fr05_c, test_fr05_d | `03-development/src/taskq_plus/cli/main.py` | `03-development/tests/test_cli.py` | DRAFT |
| FR-06 | `submit --after` builds `depends_on`; `run --all` uses Kahn topological sort (same-layer concurrent); unsatisfied deps → `blocked` (not executed, not breaker-counted); cycle detection → exit 5 + cycle path; depth cap `TASKQ_MAX_DAG_DEPTH` → exit 5; `graph --format dot\|text` | §3 FR-06 | AC-FR-06.a, .b, .c, .d | test_fr06_a, test_fr06_b, test_fr06_c, test_fr06_d | `03-development/src/taskq_plus/service/dag.py` | `03-development/tests/test_dag.py` | DRAFT |
| FR-07 | Plugin hooks `pre_run(task)` / `post_run(task, result)`; allowlist `TASKQ_PLUGINS` (comma-separated); name regex `^[A-Za-z_][A-Za-z0-9_.]*$` (else exit 6); no `eval` / `exec` / path / URL; exception isolation + 3-strike disable + `plugin_error` audit; `plugins list` | §3 FR-07 | AC-FR-07.a, .b, .c, .d, .e | test_fr07_a, test_fr07_b, test_fr07_c, test_fr07_d, test_fr07_e | `03-development/src/taskq_plus/service/plugins.py` | `03-development/tests/test_plugins.py` | DRAFT |
| FR-08 | Audit log to `$TASKQ_AUDIT_LOG` (default `$TASKQ_HOME/audit.jsonl`) — JSONL append + fsync; fields `ts` (ISO-8601 UTC) / `event` / `task_id` / `correlation_id` / `detail`; one `correlation_id` per CLI invocation; event types per SPEC; NFR-04 redaction pre-write; `export --format json\|csv\|md` consistent field set + count | §3 FR-08 | AC-FR-08.a, .b, .c, .d | test_fr08_a, test_fr08_b, test_fr08_c, test_fr08_d | `03-development/src/taskq_plus/observability/audit.py` | `03-development/tests/test_audit.py` | DRAFT |

---

## 3. Non-Functional Requirements Traceability

| NFR ID | NFR Description | SRS § | AC IDs | Planned Test IDs | Code Module / Config (P3) | Test File (P3) | Status |
|--------|-----------------|-------|--------|------------------|---------------------------|----------------|--------|
| NFR-01 | `submit + status` 100 iter → p95 < 50ms; `run --all` topo-sort 200 tasks → p95 < 200ms; measured by `pytest-benchmark` (subprocess execution excluded from the window) | §4 NFR-01 | AC-NFR-01.a, .b | test_nfr01_a, test_nfr01_b | benchmark harness (excludes `03-development/src/taskq_plus/service/executor.py` subprocess path) | `03-development/tests/test_performance.py` | DRAFT |
| NFR-02 | `grep -rn "shell=True\|eval(\|exec("` → 0 hits; per-char injection coverage `; \| & $ > < \``; plugin name allowlist regex; path / URL form rejected (exit 6); `bandit` 0 HIGH / 0 MEDIUM | §4 NFR-02 | AC-NFR-02.a, .b, .c, .d | test_nfr02_a, test_nfr02_b, test_nfr02_c, test_nfr02_d | `03-development/src/taskq_plus/service/plugins.py` (allowlist) + `.importlinter` | `03-development/tests/test_security.py` | DRAFT |
| NFR-03 | Four data files atomic-write (tmp + `os.replace`; audit append + fsync); crash-safe; corrupted `tasks.json` → exit 1 + stderr `store corrupted` (no silent rebuild); no bare `except:` / `except Exception: pass` / swallowing `KeyboardInterrupt` / `SystemExit`; breaker `OPEN → CLOSED` recovery ≤ `TASKQ_BREAKER_COOLDOWN` + 1s | §4 NFR-03 | AC-NFR-03.a, .b, .c, .d | test_nfr03_a, test_nfr03_b, test_nfr03_c, test_nfr03_d | `03-development/src/taskq_plus/storage/tasks_store.py`, `breaker_store.py`, `cache_store.py`, `audit_store.py` | `03-development/tests/test_atomicity.py` | DRAFT |
| NFR-04 | Pre-write redaction of `(sk-[A-Za-z0-9_-]{8,}\|token=\S+\|Bearer\s+\S+)` → `[REDACTED]` for `stdout_tail` / `stderr_tail` / audit `detail`; grep-zero assertion on file contents (NOT on post-load string) | §4 NFR-04 | AC-NFR-04.a, .b | test_nfr04_a, test_nfr04_b | `03-development/src/taskq_plus/observability/redaction.py` | `03-development/tests/test_redaction.py` | DRAFT |
| NFR-05 | All public symbols under `03-development/src/taskq_plus` carry a docstring with `[FR-XX]` / `[NFR-XX]` reference; coverage 100% via `ast-docstrings` | §4 NFR-05 | AC-NFR-05.a | test_nfr05_a | (tool-runner measurement; applies to all P3 modules listed in §2 / §3) | `03-development/tests/test_docstrings.py` | DRAFT |
| NFR-06 | `.importlinter` declares `cli > observability > service > storage > models`; `config` independent; `lint-imports` exit 0; no wildcard `ignore_imports` / single-`forbidden` weakening | §4 NFR-06 | AC-NFR-06.a, .b, .c | test_nfr06_a, test_nfr06_b, test_nfr06_c | `.importlinter` (project root) + 5-layer layout under `03-development/src/taskq_plus/` | `03-development/tests/test_architecture.py` | DRAFT |
| NFR-07 | Runtime deps pinned `==` in `requirements.txt`; allowed licenses MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0; **scan includes installed dependency tree** (NOT own src only); SBOM → `08-config/SBOM.json` with `name` / `version` / `license` | §4 NFR-07 | AC-NFR-07.a, .b, .c | test_nfr07_a, test_nfr07_b, test_nfr07_c | `requirements.txt` + `08-config/SBOM.json` | `03-development/tests/test_licenses.py` | DRAFT |
| NFR-08 | `.methodology/harness_config.json` `features.mutation_testing: true`; `mutmut` score ≥ 70; scope limited to `service/` + `storage/` with annotated rationale | §4 NFR-08 | AC-NFR-08.a, .b | test_nfr08_a, test_nfr08_b | `.methodology/harness_config.json` + `service/` + `storage/` modules | `03-development/tests/test_mutation.py` | DRAFT |
| NFR-09 | `pytest 03-development/tests -q` exits 0 with **0 skipped**; no `pytest.skip` / `mark.skip` / `skipif` / `xfail` / assertion-free stubs; `ast-assertions` `zero_assert == 0`; no `--ignore` / `-k` / `--deselect` / `collect_ignore` / `testpaths` exclusion | §4 NFR-09 | AC-NFR-09.a, .b, .c | test_nfr09_a, test_nfr09_b, test_nfr09_c | (test-runner / tool-runner measurement over `03-development/tests/`) | `03-development/tests/test_authenticity.py` | DRAFT |
| NFR-10 | `03-development/tests/integration/` cross-module integration tests; line coverage ≥ 80%; CLI/CliRunner-driven (NOT direct internal calls); min flows: submit→run→status, DAG multi-layer, breaker open/close, cache hit, plugin hook, three export formats | §4 NFR-10 | AC-NFR-10.a | test_nfr10_a | integration test driver through `python -m taskq_plus` / `click.testing.CliRunner` | `03-development/tests/integration/` | DRAFT |
| NFR-11 | Project MI (LLOC-weighted) ≥ 80; per-function CC ≤ 10; file ≤ 400 LOC; dir ≤ 15 files | §4 NFR-11 | AC-NFR-11.a, .b, .c | test_nfr11_a, test_nfr11_b, test_nfr11_c | n/a (`readability-v2` tool measurement over all P3 modules) | `03-development/tests/test_readability.py` | DRAFT |
| NFR-12 | `Makefile` `verify-system` target chains: full tests + CLI smoke (submit / run / status / graph / export / clear); `make verify-system` exit 0 + stdout `verify-system: PASS` | §4 NFR-12 | AC-NFR-12.a | test_nfr12_a | `Makefile` (project root) | `03-development/tests/test_verify_system.py` | DRAFT |

---

## 4. SRS Section ↔ Code Module Mapping (Forward Reference — Populated in P3)

> This table is the **forward** half of the traceability: SRS § →
> code module that implements it. Populated when Phase 3 (implementation)
> runs; rows below carry `(P3)` markers as placeholders. Once P3 lands,
> `build_traceability` fills the Code Module / Function columns and
> sets Status → `IN_PROGRESS`.

| SRS § | Code Module (P3) | Function / Class | Status |
|-------|------------------|------------------|--------|
| §3 FR-01 | `03-development/src/taskq_plus/service/submission.py` | (P3) | DRAFT |
| §3 FR-02 | `03-development/src/taskq_plus/service/executor.py` | (P3) | DRAFT |
| §3 FR-03 | `03-development/src/taskq_plus/service/breaker.py` | (P3) | DRAFT |
| §3 FR-04 | `03-development/src/taskq_plus/service/cache.py` | (P3) | DRAFT |
| §3 FR-05 | `03-development/src/taskq_plus/cli/main.py` | (P3) | DRAFT |
| §3 FR-06 | `03-development/src/taskq_plus/service/dag.py` | (P3) | DRAFT |
| §3 FR-07 | `03-development/src/taskq_plus/service/plugins.py` | (P3) | DRAFT |
| §3 FR-08 | `03-development/src/taskq_plus/observability/audit.py` | (P3) | DRAFT |
| §4 NFR-01 | `03-development/tests/test_performance.py` + benchmark harness | (P3) | DRAFT |
| §4 NFR-02 | `03-development/src/taskq_plus/service/plugins.py` (allowlist) + `.importlinter` | (P3) | DRAFT |
| §4 NFR-03 | `03-development/src/taskq_plus/storage/tasks_store.py`, `breaker_store.py`, `cache_store.py`, `audit_store.py` | (P3) | DRAFT |
| §4 NFR-04 | `03-development/src/taskq_plus/observability/redaction.py` | (P3) | DRAFT |
| §4 NFR-05 | all P3 modules under `03-development/src/taskq_plus/` (5 layers) | (P3) | DRAFT |
| §4 NFR-06 | `.importlinter` (project root) + 5-layer layout | (P3) | DRAFT |
| §4 NFR-07 | `requirements.txt` + `08-config/SBOM.json` | (P3) | DRAFT |
| §4 NFR-08 | `.methodology/harness_config.json` + `service/` + `storage/` modules | (P3) | DRAFT |
| §4 NFR-09 | (test-runner / tool-runner measurement) | (P3) | DRAFT |
| §4 NFR-10 | `03-development/tests/integration/` driver | (P3) | DRAFT |
| §4 NFR-11 | n/a (`readability-v2` tool measurement over all P3 modules) | (P3) | DRAFT |
| §4 NFR-12 | `Makefile` (project root) | (P3) | DRAFT |

---

## 5. AC ↔ Test ID Reverse Lookup (Backward Traceability)

> Backward lookup table — given a test ID, find the originating AC,
> SRS §, and FR/NFR. Each row is grep-recoverable from any test
> function name.

### 5.1 FR Test Lookup

| Test ID | AC | SRS § | FR |
|---------|----|-------|----|
| test_fr01_a | AC-FR-01.a | §3 FR-01 | FR-01 |
| test_fr01_b | AC-FR-01.b | §3 FR-01 | FR-01 |
| test_fr01_c | AC-FR-01.c | §3 FR-01 | FR-01 |
| test_fr01_d | AC-FR-01.d | §3 FR-01 | FR-01 |
| test_fr02_a | AC-FR-02.a | §3 FR-02 | FR-02 |
| test_fr02_b | AC-FR-02.b | §3 FR-02 | FR-02 |
| test_fr02_c | AC-FR-02.c | §3 FR-02 | FR-02 |
| test_fr02_d | AC-FR-02.d | §3 FR-02 | FR-02 |
| test_fr02_e | AC-FR-02.e | §3 FR-02 | FR-02 |
| test_fr03_a | AC-FR-03.a | §3 FR-03 | FR-03 |
| test_fr03_b | AC-FR-03.b | §3 FR-03 | FR-03 |
| test_fr03_c | AC-FR-03.c | §3 FR-03 | FR-03 |
| test_fr03_d | AC-FR-03.d | §3 FR-03 | FR-03 |
| test_fr04_a | AC-FR-04.a | §3 FR-04 | FR-04 |
| test_fr04_b | AC-FR-04.b | §3 FR-04 | FR-04 |
| test_fr04_c | AC-FR-04.c | §3 FR-04 | FR-04 |
| test_fr04_d | AC-FR-04.d | §3 FR-04 | FR-04 |
| test_fr05_a | AC-FR-05.a | §3 FR-05 | FR-05 |
| test_fr05_b | AC-FR-05.b | §3 FR-05 | FR-05 |
| test_fr05_c | AC-FR-05.c | §3 FR-05 | FR-05 |
| test_fr05_d | AC-FR-05.d | §3 FR-05 | FR-05 |
| test_fr06_a | AC-FR-06.a | §3 FR-06 | FR-06 |
| test_fr06_b | AC-FR-06.b | §3 FR-06 | FR-06 |
| test_fr06_c | AC-FR-06.c | §3 FR-06 | FR-06 |
| test_fr06_d | AC-FR-06.d | §3 FR-06 | FR-06 |
| test_fr07_a | AC-FR-07.a | §3 FR-07 | FR-07 |
| test_fr07_b | AC-FR-07.b | §3 FR-07 | FR-07 |
| test_fr07_c | AC-FR-07.c | §3 FR-07 | FR-07 |
| test_fr07_d | AC-FR-07.d | §3 FR-07 | FR-07 |
| test_fr07_e | AC-FR-07.e | §3 FR-07 | FR-07 |
| test_fr08_a | AC-FR-08.a | §3 FR-08 | FR-08 |
| test_fr08_b | AC-FR-08.b | §3 FR-08 | FR-08 |
| test_fr08_c | AC-FR-08.c | §3 FR-08 | FR-08 |
| test_fr08_d | AC-FR-08.d | §3 FR-08 | FR-08 |

### 5.2 NFR Test Lookup

| Test ID | AC | SRS § | NFR |
|---------|----|-------|-----|
| test_nfr01_a | AC-NFR-01.a | §4 NFR-01 | NFR-01 |
| test_nfr01_b | AC-NFR-01.b | §4 NFR-01 | NFR-01 |
| test_nfr02_a | AC-NFR-02.a | §4 NFR-02 | NFR-02 |
| test_nfr02_b | AC-NFR-02.b | §4 NFR-02 | NFR-02 |
| test_nfr02_c | AC-NFR-02.c | §4 NFR-02 | NFR-02 |
| test_nfr02_d | AC-NFR-02.d | §4 NFR-02 | NFR-02 |
| test_nfr03_a | AC-NFR-03.a | §4 NFR-03 | NFR-03 |
| test_nfr03_b | AC-NFR-03.b | §4 NFR-03 | NFR-03 |
| test_nfr03_c | AC-NFR-03.c | §4 NFR-03 | NFR-03 |
| test_nfr03_d | AC-NFR-03.d | §4 NFR-03 | NFR-03 |
| test_nfr04_a | AC-NFR-04.a | §4 NFR-04 | NFR-04 |
| test_nfr04_b | AC-NFR-04.b | §4 NFR-04 | NFR-04 |
| test_nfr05_a | AC-NFR-05.a | §4 NFR-05 | NFR-05 |
| test_nfr06_a | AC-NFR-06.a | §4 NFR-06 | NFR-06 |
| test_nfr06_b | AC-NFR-06.b | §4 NFR-06 | NFR-06 |
| test_nfr06_c | AC-NFR-06.c | §4 NFR-06 | NFR-06 |
| test_nfr07_a | AC-NFR-07.a | §4 NFR-07 | NFR-07 |
| test_nfr07_b | AC-NFR-07.b | §4 NFR-07 | NFR-07 |
| test_nfr07_c | AC-NFR-07.c | §4 NFR-07 | NFR-07 |
| test_nfr08_a | AC-NFR-08.a | §4 NFR-08 | NFR-08 |
| test_nfr08_b | AC-NFR-08.b | §4 NFR-08 | NFR-08 |
| test_nfr09_a | AC-NFR-09.a | §4 NFR-09 | NFR-09 |
| test_nfr09_b | AC-NFR-09.b | §4 NFR-09 | NFR-09 |
| test_nfr09_c | AC-NFR-09.c | §4 NFR-09 | NFR-09 |
| test_nfr10_a | AC-NFR-10.a | §4 NFR-10 | NFR-10 |
| test_nfr11_a | AC-NFR-11.a | §4 NFR-11 | NFR-11 |
| test_nfr11_b | AC-NFR-11.b | §4 NFR-11 | NFR-11 |
| test_nfr11_c | AC-NFR-11.c | §4 NFR-11 | NFR-11 |
| test_nfr12_a | AC-NFR-12.a | §4 NFR-12 | NFR-12 |

---

## 6. Completeness Verification

| Check | Target | State | Status |
|-------|--------|-------|--------|
| FR rows emitted (FR-01..FR-08) | 8 | 8 | OK |
| NFR rows emitted (NFR-01..NFR-12) | 12 | 12 | OK |
| Each FR has at least 1 AC | 8/8 | 8/8 | OK |
| Each NFR has at least 1 AC | 12/12 | 12/12 | OK |
| AC ↔ Planned Test ID 1:1 mapping | 34 FR-ACs + 29 NFR-ACs = 63 | 63 | OK |
| FR ↔ SRS section linkage | 8/8 | 8/8 | OK |
| NFR ↔ SRS section linkage | 12/12 | 12/12 | OK |
| Code ↔ Test linkage (P3) | pending P3 | pending | DRAFT |
| ASPICE SWE.3.B SP1 (req → test) | 100% | 100% planned (63 / 63) | DRAFT |
| ASPICE SWE.3.B SP2 (bidirectional) | yes | yes (§2 + §3 + §5) | OK |
| ASPICE SWE.3.B SP3 (consistency) | machine-refreshed | machine-refreshed | OK |

---

## 7. ASPICE Compliance Notes

ASPICE SWE.3.B base practices (per orchestrator-loader convention):

- **SP1 — Task-to-work-product traceability**: each FR/NFR has at
  least one AC and one planned test ID. Mapping covered by §2 (FRs)
  + §3 (NFRs) + §5 (backward lookup). **63 tests planned across
  34 FR-ACs + 29 NFR-ACs**.
- **SP2 — Bidirectional traceability**: forward direction
  (FR/NFR → AC → test) in §2 / §3; backward direction
  (test → AC → FR/NFR) in §5.
- **SP3 — Traceability consistency**: Status column
  machine-refreshed by `harness build_traceability` on
  `advance-phase`; manual edits to Status are overwritten.

---

## 8. Forward References (Legal Framework Filenames)

> The harness `check_forward_refs` gate (`artifact_consistency.py`)
> blocks any invented filename. Only the legal filenames per stage
> may be referenced. **NEVER** invent filenames like `ARCHITECTURE.md`
> for the P2 architecture deliverable — use `SAD.md`.

| Phase | File (legal) | Used by this matrix? |
|-------|--------------|----------------------|
| 01-requirements | `01-requirements/SPEC_TRACKING.md` | yes (FR/NFR inventory cross-link) |
| 01-requirements | `01-requirements/SRS.md` | yes (SSOT for ACs / sections) |
| 01-requirements | `01-requirements/TRACEABILITY_MATRIX.md` | yes (this file) |
| 01-requirements | `01-requirements/TEST_INVENTORY.yaml` | downstream P3 (consumed by P3 / P4) |
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
| 08-config | `08-config/SBOM.json` | downstream P8 (NFR-07 target) |

---

## 9. Update Log

| Date | Change | By |
|------|--------|----|
| 2026-07-30 | Initial creation — placeholder skeleton replaced with full FR-01..FR-08 + NFR-01..NFR-12 bidirectional traceability matrix (FR/NFR ↔ SRS § ↔ AC ↔ Planned Test ID); 63 tests planned (34 FR-derived + 29 NFR-derived); Code Module / Test File columns carry forward-reference placeholders for Phase 3 implementation; ASPICE SWE.3.B SP1 / SP2 / SP3 compliance covered; legal downstream framework filenames catalogued; Status seed `DRAFT` for all rows. | Agent A (Requirements Engineer) |