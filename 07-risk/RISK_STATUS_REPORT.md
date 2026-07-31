# Risk Status Report — taskq-plus (Phase 7)

> **Project**: taskq-plus
> **Phase**: 7 — Risk Management
> **As of**: 2026-08-01 (release-readiness snapshot)
> **Companion docs**: `RISK_REGISTER.md`, `RISK_MITIGATION_PLANS.md`
> **Source of truth**: `.methodology/quality_manifest.json`, `.methodology/bug_hunt_report.json`, Gate 3 / Gate 4 results, `FINAL_SIGN_OFF.md`

---

## 1. Executive Summary

| Indicator | Value | Reading |
|-----------|-------|---------|
| Open risks (HIGH band, L×I ≥ 9) | **6** | All have formal mitigation plans; 5 already Mitigated — verified, 1 Accepted-risk |
| Open risks (MEDIUM band) | **7** | R3, R4, R7, R9, R12, R13, R14 — tracked, no formal plan required |
| Open risks (LOW band) | **1** | R11 (bandit B404) — noise exemption |
| Open critical / high / medium / low issues (Gate 4) | **0 / 0 / 0 / 0** | Clean release verdict |
| Gate 4 composite | **98.71 / 100** | PASS (threshold ≥ 85) |
| Confirmed bug-hunt findings still open | **0 / 7** | All 7 RESOLVED in P6 commits `723b708c` / `c94a01c` |
| Hard-rule compliance (HR-04/05/16/17) | OK | Phase 7 plan followed; no harness edits; P7 doc flow respected |

**Bottom line.** taskq-plus is **release-ready from a risk standpoint**:
every HIGH-band risk has a *closed* code-level mitigation that is exercised
by regression tests, and the only accepted-risk row (R10 audit-log
rotation) is a documented operator responsibility that was declared in
`SPEC.md` §9 before any code was written.

---

## 2. Per-Risk Status

### 2.1 HIGH band (score ≥ 9) — formal plans in `RISK_MITIGATION_PLANS.md`

| ID | Risk | Score | Status | Mitigation Plan | Owner | Target | Latest evidence |
|----|------|-------|--------|-----------------|-------|--------|-----------------|
| R1 | Concurrent `tasks.json` corruption | 15 | **Mitigated — verified** | MP-1 | storage maintainer | 2026-08-15 | bug_hunt `task_store#2` CLOSED (`723b708c`); `test_fr01_store_concurrent` GREEN |
| R2 | Subprocess hang / zombie | 12 | **Mitigated — verified** | MP-2 | executor maintainer | 2026-09-01 | `preflight_reliability_lint` GREEN; `test_nfr03_*` GREEN |
| R5 | Secret written to disk | 15 | **Mitigated — verified** | MP-3 | audit maintainer | 2026-08-15 | bug_hunt `audit#1`, `audit#2` CLOSED; acceptance #22 grep `sk-` = 0 |
| R6 | Plugin → arbitrary-code-execution | 15 | **Mitigated — verified** | MP-4 | plugins maintainer | 2026-08-15 | bug_hunt `plugins#1` CLOSED; bandit 0 HIGH/MEDIUM; import-linter blocks eval/exec |
| R8 | Plugin exception aborts queue | 9 | **Mitigated — verified** | MP-5 | plugins maintainer | 2026-09-01 | bug_hunt `plugins#2` REFUTED; isolation contract GREEN |
| R10 | Audit-log unbounded growth | 10 | **Accepted-risk** | MP-6 | operator (Johnny) | next patch train | Documented in `SPEC.md` §9 R10; reiterated in `FINAL_SIGN_OFF.md`; rotation snippet pending in `09-maintenance/` |

### 2.2 MEDIUM band (score 6–8) — tracked, no formal plan

| ID | Risk | Score | Status | Owner | Re-evaluate on |
|----|------|-------|--------|-------|----------------|
| R3 | Circuit breaker false-positive OPEN | 6 | Mitigated — monitored | service maintainer | Any new failure-pattern heuristic in breaker store |
| R4 | Cache replays stale result on TTL expiry boundary | 6 | Mitigated — monitored | cache maintainer | Any change to cache serializer |
| R7 | Pathological DAG exhausts resources | 6 | Mitigated — monitored | service maintainer | Any new DAG operation (e.g. weights, parallelism hints) |
| R9 | Dependency introduces non-allowlisted license | 6 | Mitigated — continuous | release maintainer | Every release; flipped on next `pip install` beyond allowlist |
| R12 | Mutation testing excluded by feature flag | 6 | Accepted (contractual) | release maintainer | If `features.mutation_testing` flipped to `true` in P9 |
| R13 | Performance dimension recorded as `None` (no benchmarks) | 6 | Accepted (deferred) | service maintainer | Future phase with stable throughput baseline |
| R14 | CRG calibration override `crg_cohesion_healthy=0.2` | 6 | Accepted (calibration evidence filed) | tooling | If CRG output surprises next round |

### 2.3 LOW band (score ≤ 5) — watch-list

| ID | Risk | Score | Status | Mitigation |
|----|------|-------|--------|------------|
| R11 | Bandit LOW B404 in `executor.py:27` | 4 | Accepted | `# noqa: B404` justified; documented dependency of FR-02 |

---

## 3. Recently Closed Risks (audit trail)

The 7 confirmed findings from `.methodology/bug_hunt_report.json`
(generated 2026-07-31, sha `723b708c`) were all RESOLVED in P6 fix commits
and now sit in this status report as **closed-mitigated**. They remain
listed because:

- Any future regression should be traceable back to the original
  risk row (R1 / R3 / R5 / R6 / R8).
- They give the next maintainer evidence that the project has a working
  bug-hunt → mitigation → regression-test loop.

| Bug-hunt ID | Severity | Mapped risk | Status | Fix commit |
|-------------|----------|-------------|--------|-----------|
| `executor#1` | critical | R3, R8 | CLOSED | `723b708c` |
| `audit#1` | critical | R5 | CLOSED | `723b708c` |
| `audit#2` | high | R5 | CLOSED | `723b708c` |
| `task_store#1` | high | R1 | CLOSED | `723b708c` |
| `task_store#2` | high | R1 | CLOSED | `723b708c` |
| `executor#2` | high | R8 | CLOSED | `723b708c` |
| `plugins#1` | high | R6 | CLOSED | `723b708c` |

Refuted findings (`audit#3`, `task_store#3`, `executor#3`, `task#1`,
`plugins#2`) confirmed that the original concerns were already covered by
existing guards; recorded for the next bug-hunt to avoid re-investigating.

---

## 4. Open / Accepted-Risk Action Queue

| # | Action | Owner | Trigger | Verification |
|---|--------|-------|---------|--------------|
| 1 | Emit `WARN` log on `StoreCorrupted` (MP-1) | storage maintainer | next patch train | new test asserting log line |
| 2 | SIGKILL escalation test for `run_with_retry` (MP-2) | executor maintainer | 2026-09-01 | new `test_nfr03_*` |
| 3 | `audit/patterns.toml` regex extension (MP-3) | audit maintainer | 2026-08-15 | new pattern + test |
| 4 | Plugin-name fuzz test (MP-4) | plugins maintainer | 2026-08-15 | `test_plugins_fuzz.py` |
| 5 | `TASKQ_PLUGINS_AUTO_DISABLE` override (MP-5) | plugins maintainer | 2026-09-01 | config-tested in CLI |
| 6 | `09-maintenance/AUDIT_LOG_ROTATION.md` (MP-6) | operator (Johnny) | next patch train | file existence + non-empty |

---

## 5. Risk Heat Map (current)

```
                L=1   L=2   L=3   L=4   L=5
                ┌─────┬─────┬─────┬─────┬─────┐
         I=5    │     │     │R1,R5│ R6  │     │
                ├─────┼─────┼─────┼─────┼─────┤
         I=4    │     │     │ R2  │     │     │
                ├─────┼─────┼─────┼─────┼─────┤
         I=3    │     │R3,R4,│ R8  │     │ R10 │
                │     │R7,R9,│     │     │     │
                │     │R12-14│     │     │     │
                ├─────┼─────┼─────┼─────┼─────┤
         I=2    │     │     │ R13 │ R11 │     │
                ├─────┼─────┼─────┼─────┼─────┤
         I=1    │     │     │     │ R11 │     │
                └─────┴─────┴─────┴─────┴─────┘
```

Reading: cells in the upper-right are the highest concern. The cluster
of HIGH-band items in column L=3, row I=5 (R1 / R5 / R6) is the
**security / data-integrity frontier** and is fully covered by MP-1,
MP-3, MP-4.

---

## 6. Re-evaluation Triggers

This status report should be re-issued (or refreshed) whenever any of the
following occurs:

1. A new Gate 4 / Gate 3 round produces a dimension score below its
   threshold.
2. A new FR is added that touches any of the three high-risk modules
   (`executor`, `plugins`, `task_store`).
3. `gitleaks` reports > 0 leaks; bandit reports a new HIGH/MEDIUM.
4. `features.mutation_testing` in `.methodology/harness_config.json`
   is flipped from `false` to `true` (R12 changes band).
5. `tasks.json` or `audit.jsonl` exceeds 100 MB on an operator machine
   (R10 capacity warning).
6. CRG `community_cohesion.score` falls below the per-round threshold
   (R14 calibration drift).

---

## 7. Provenance

| Datum | Source path |
|-------|-------------|
| Gate 4 composite 98.71 | `.methodology/quality_manifest.json` `gate_results.gate4.overall_score` |
| Gate 3 composite 98.93 | `.methodology/gate3_result.json` |
| Bug-hunt 12 findings (7 confirmed, 5 refuted) | `.methodology/bug_hunt_report.json` |
| Risk matrix R1–R10 | `SPEC.md` §9 (lines 428–441) |
| Known limitations | `FINAL_SIGN_OFF.md` (mutation testing, performance dim, audit rotation) |
| Phase 7 plan constraints | `.methodology/phase7_plan.md` |
| Per-FR Gate 1 evidence | `.methodology/gate_results/gate1/` (per FR) |

---

_End of `RISK_STATUS_REPORT.md`._
