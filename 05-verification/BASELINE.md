# BASELINE.md — taskq-plus

> Phase 5 (Verification) baseline snapshot. Establishes the system-of-record
> state at P5 entry so any later regression can be diffed against this point.
> Reference: `harness/templates/BASELINE.md`.

## 1. Baseline Overview
- Author: P5 Verification Author (orch-post)
- Reviewer: Agent B sub-agent (HybridWorkflow HR-04)
- session_id: orch-post/P5-verification-2026-07-31
- Date: 2026-07-31
- Project: `taskq-plus` (Round 1 of 3; Python 3.11 CLI)
- Current commit: `6fcfce3` (feat(FR-08): Gate1 PASS — score=100.0 [phase=5])
- Phase: 5 — Verification & Delivery
- Last gate cleared: Gate 1 (FR-08, last of 8); Gate 3 PASS at P4 exit
- Source artifact: `/Users/johnny/projects/taskq-plus`

## 2. Functional Baseline (maps to SRS FR, 100% complete)

| FR ID | Feature Description | Baseline Status | Notes |
|-------|--------------------|-----------------|-------|
| FR-01 | Task submission & validation (`submit`) | PASS | pydantic TaskSubmission; uuid4 8-hex id; atomic write; exit 2 on validation failure |
| FR-02 | Subprocess execution with controlled concurrency / timeout / retry | PASS | `shlex.split`, `shell=True` forbidden; ThreadPoolExecutor + threading.Lock |
| FR-03 | Exponential backoff retry with jitter | PASS | monotonicity enforced (property-based) |
| FR-04 | TTL result cache | PASS | cache-key determinism (property-based); `cache.json` |
| FR-05 | CLI command groups via `click` | PASS | 11 commands; `python -m taskq_plus` entry |
| FR-06 | Dependency DAG ordering + cycle rejection | PASS | topological sort p95 < 200ms (NFR-01.b); exit 5 on cycle |
| FR-07 | Allowlisted plugin hook system | PASS | name regex `^[A-Za-z_][A-Za-z0-9_.]*$`; no eval/exec; auto-disable after 3 failures |
| FR-08 | Structured JSONL audit trail with redaction | PASS | redaction idempotence (property-based); `audit.jsonl` append + fsync |

All 8 FRs are Gate 1 PASS (score 100.0); reference `04-testing/TEST_RESULTS.md` and
`.methodology/gate_results/`. Per-FR scoring: FR-01..FR-08 → 100.0 each.

## 3. Quality Baseline

| Metric | Threshold | Actual | Status |
|--------|-----------|--------|--------|
| Gate 1 composite (per-FR) | ≥ 80% | 100.0 (all 8 FRs) | PASS |
| Gate 3 composite | ≥ 85 | **98.93** | PASS |
| Test coverage (overall, `04-testing/COVERAGE_REPORT.md`) | ≥ 80% | **99%** (1,367 stmts; 5 missed) | PASS |
| Test coverage (`03-development/src/`) | ≥ 80% | **100%** (0 missing lines) | PASS |
| Linting (`ruff check 03-development/src`) | ≥ 90 | 100.0 | PASS |
| Type safety (`pyright 03-development/src/`) | ≥ 85 | 100.0 (0 errors / 0 warnings) | PASS |
| Security (`bandit -r 03-development/src/`) | 0 HIGH / 0 MEDIUM | 0 HIGH, 0 MEDIUM, 2 LOW | PASS |
| Secrets scanning (`gitleaks detect`) | 100 | 100.0 (no leaks) | PASS |
| License compliance (`scancode`) | 100 | 100.0 (59 files; 0 unknown) | PASS |
| Integration coverage (CLI entry, NFR-10) | ≥ 80% | 97.29% (`cli/main.py`) / 99.29% (`cli/commands.py`) | PASS |
| Readability (project_score) | ≥ 80 | 88.3 (avg CC 3.23; 1,724 LLOC) | PASS |
| Error handling (`ast-error-handling`) | ≥ 80 | 100.0 (10/10 with handler; 0 anti-pattern) | PASS |
| Documentation (`ast-docstrings`) | ≥ 75 | 100.0 (108/108 with docstring) | PASS |
| Test assertion quality | ≥ 60 | 100.0 | PASS |
| Mutation testing (Gate 1 per-FR) | ≥ 70 | per-FR PASS at Gate 1 (feature-flag off at P5; not re-run) | REFERENCE |
| Architecture (CRG community_cohesion) | ≥ 80 | 100.0 (framework_override) | PASS |

Mutation testing is gated per-FR at Gate 1 (P3 exit); per
`.methodology/quality_manifest.json` it is `excluded_by_feature_flag=true` at the
project-wide composite level. Per-FR Gate 1 mutation scores cleared the ≥ 70
threshold (see `.methodology/gate_results/`); P5 does **not** re-run mutmut.

## 4. Performance Baseline (A/B monitoring — NFR-01)

| Metric | Threshold | Baseline Value | Source |
|--------|-----------|----------------|--------|
| submit + status p95 | < 50 ms | < 50 ms (100 cycles) | `03-development/tests/test_nfr_cross_cutting.py::test_nfr01_a` |
| 200-task topological sort p95 | < 200 ms | < 200 ms | `03-development/tests/test_nfr_cross_cutting.py::test_nfr01_b` |
| pytest wall-clock (full sweep, 6,866 tests) | — | 115.25 s | `04-testing/TEST_RESULTS.md` §1 |
| pytest wall-clock (project suite, 441 tests) | — | 20.25 s | `04-testing/TEST_RESULTS.md` §2 |
| Atomic write (4 data files) | all 4 atomic | yes (tmp + `os.replace`; audit append + fsync) | `taskq_plus.storage.atomic` |

Performance benchmarks are evaluated by the NFR-01 SLA tests
(`test_nfr01_a`, `test_nfr01_b`) — both PASS per `04-testing/TEST_RESULTS.md`.
No A/B comparator run is in scope at Round 1 baseline (single-shot baseline).

## 5. Known Issues

| Severity | Count | Description |
|----------|-------|-------------|
| HIGH | 0 | None |
| MEDIUM | 0 | None |
| LOW | 2 | `bandit` LOW findings in `03-development/src/` (per Gate 3); see `.methodology/quality_manifest.json` `security.score=98` (100 − 2×1). Non-blocking; documented for future hardening. |

> HIGH severity count = 0 — baseline establishment precondition met.

Out-of-scope observation (not a regression in `taskq-plus`):
- `harness/tests/test_generate_full_plan.py::TestParseSrsFrSectionsMergesJson::test_real_srs_md_extracts_all_5_frs` is a harness-side fixture drift (hard-coded `5` should be `8`). Deferred to harness owner per `04-testing/TEST_RESULTS.md` §3.1; `harness/` is read-only for non-harness phase work.

## 6. Change Log

| Date | Change | Commit / Ref |
|------|--------|--------------|
| 2026-07-31 | feat(FR-08): Gate1 PASS — score=100.0 [phase=5] | `6fcfce3` |
| 2026-07-31 | test(FR-08): add coverage tests and pragma exclusions | `165c419` |
| 2026-07-31 | feat(FR-07): Gate1 PASS — score=100.0 [phase=5] | `ad69769` |
| 2026-07-31 | feat(FR-06): Gate1 PASS — score=100.0 [phase=5] | `4f9b481` |
| 2026-07-31 | feat(FR-05): Gate1 PASS — score=100.0 [phase=5] | `5c6642b` |
| 2026-07-31 | test(FR-05): add coverage tests and pragma exclusions | `f28b25a` |
| 2026-07-31 | feat(FR-04): Gate1 PASS — score=100.0 [phase=5] | `662a536` |
| 2026-07-31 | feat(FR-01): Gate1 PASS — score=100.0 [phase=5] | `58c99d0` |
| 2026-07-31 | chore: bump harness submodule to b45cf04 (FSM backwards-check fix) | `b08f874` |
| 2026-07-31 | feat(FR-03): Gate1 PASS — score=100.0 [phase=5] | `c021c52` |

Source: `git -C /Users/johnny/projects/taskq-plus log --oneline -10`.

## 7. Acceptance Sign-off
- Agent A (Author, P5 Verification): orch-post/P5-verification-2026-07-31 — 2026-07-31
- Agent B (Reviewer, HybridWorkflow HR-04): deferred to sub-agent review at P5 POST-FLIGHT
- Approver: Johnny (project owner) — pending Gate 4 (Phase 6) final acceptance
- Phase exit: P4 → P5 transition recorded in `.methodology/state.json`
  (`last_milestone_command=advance-phase --completed-phase 4`,
   `last_milestone_at=2026-07-31T08:29:10.100107+00:00`)
- This BASELINE.md is the P5-entry system-of-record snapshot.