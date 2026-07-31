# Risk Register — taskq-plus

> **Project**: taskq-plus (local task-queue CLI, Python 3.11)
> **Phase**: 7 — Risk Management
> **Generated**: 2026-08-01
> **Seed source**: `SPEC.md` §9 risk matrix (R1–R10) + Gate 3/4 deferred items + `.methodology/bug_hunt_report.json` residuals
> **Owner**: P7 Risk Author (orch-post)
> **Reviewer**: Sub-agent (deferred to P7 POST-FLIGHT)
> **Approver**: Johnny (project owner)

---

## 1. Scoring Convention

Likelihood and impact are scored on a 1–5 ordinal scale.

| Label | Likelihood (L) | Impact (I) |
|-------|----------------|------------|
| 1 Very Low | < 5 % | Trivial / cosmetic only |
| 2 Low | 5–15 % | Minor; recovery < 1 day |
| 3 Medium | 15–40 % | Notable; user-visible defect |
| 4 High | 40–70 % | Functional break; data integrity risk |
| 5 Very High | > 70 % | Catastrophic / security |

**Risk score = L × I** (range 1–25). Severity bands:

| Band | Score | Treatment |
|------|-------|-----------|
| **HIGH** | ≥ 9 | Formal mitigation plan, named owner, deadline (see `RISK_MITIGATION_PLANS.md`) |
| **MEDIUM** | 6–8 | Tracked mitigation in status report; review at next phase |
| **LOW** | ≤ 5 | Watch-list only; document and re-evaluate at major change |

The original `SPEC.md` §9 scale (高/中/低) is mapped as: 高 → 5, 中 → 3, 低 → 2 by default. Where evidence justifies a finer read (e.g. empirical bug-hunt data for resolved defects), the value is overridden and labelled *evidence-based*.

---

## 2. Source Register

| Source | What was extracted |
|--------|--------------------|
| `SPEC.md` §9 (line 428–441) | Seed risks **R1–R10** with original 影響/可能性 cells |
| `.methodology/gate3_result.json` + `gate4_result.json` | Gate scores, open issues, dimension-level residual risks |
| `.methodology/bug_hunt_report.json` | 7 confirmed findings → already RESOLVED in commits `723b708c` / `c94a01c`. Captured as *Closed-Mitigated* rows for traceability; raise again only if regression observed |
| `.methodology/quality_manifest.json` `gate_results.gate4.*` | 0 open critical / 0 open high / 0 open medium / 0 open low at release |
| `FINAL_SIGN_OFF.md` known issues | 2 LOW bandit findings (B404 subprocess import — required) |
| `harness/.methodology/phase7_plan.md` Hard Rules | Constraints inherited (HR-05 harness wins, HR-16 trace floor, HR-17 no harness mutation) |

---

## 3. Risk Register (live items)

| ID | Risk | L | I | Score | Band | Category | Primary Control / Mitigation | Owner | Target Date | Status |
|----|------|---|---|-------|------|----------|------------------------------|-------|-------------|--------|
| **R1** | Concurrent writers corrupt `tasks.json` (lost-update / torn write) | 3 | 5 | 15 | **HIGH** | Data integrity / concurrency | `fcntl` lock + atomic write (`storage/atomic.py`); StoreCorrupted recovery path; per-FR Gate1 evidence `gate1/FR-01`, `gate1/FR-05` | storage maintainer | 2026-08-15 (next patch train) | **Mitigated — verified** (bug_hunt `task_store#2` closed in `723b708c`) |
| **R2** | Subprocess hang / zombie after timeout | 3 | 4 | 12 | **HIGH** | Availability / process lifecycle | Mandatory `timeout=` on every `subprocess.run`/`Popen` (FR-02 + `preflight_reliability_lint`); executor worker pool with bounded submit | executor maintainer | 2026-09-01 | **Mitigated — verified** (NFR-03 handler in `executor.py:run_with_retry`; reliability lint preflight green) |
| **R3** | Circuit breaker locks out a healthy downstream (false-positive OPEN) | 2 | 3 | 6 | MEDIUM | Availability / heuristic | `cooldown_seconds` + `HALF_OPEN` probe state (FR-03); only consecutive failures count | service maintainer | 2026-09-15 | **Mitigated — monitored** (bug_hunt `executor#1` closed; `run_all` breaker gate added in `723b708c`) |
| **R4** | Cache replays stale result on TTL expiry boundary | 3 | 2 | 6 | MEDIUM | Correctness / cache | TTL enforced in `service/cache.py`; expired entries re-execute (FR-04); `cache_store` redaction on rehydrate | cache maintainer | 2026-09-15 | **Mitigated — monitored** |
| **R5** | Secret written to disk via `stdout_tail` / task record redaction gap | 3 | 5 | 15 | **HIGH** | Security / compliance | Two-layer redaction: `audit.AuditLogger.emit` (`audit.jsonl`) + `service.executor._build_result` (`tasks.json`) (NFR-04); bandit 0 HIGH/MEDIUM; gitleaks 100; Gate4 acceptance #22 grep `sk-` = 0 | audit maintainer | 2026-08-15 | **Mitigated — verified** (bug_hunt `audit#1` closed in `723b708c`; `tasks.json` `sk-` count verified = 0) |
| **R6** | Plugin loader becomes arbitrary-code-execution entry-point (path traversal / regex bypass) | 3 | 5 | 15 | **HIGH** | Security / RCE | Named-allowlist load + regex whitelist of plugin symbols; `eval` / `exec` / path-traversal banned by lint (B102-blocked, import-linter enforces `no_eval_exec_dynamic_import`); FR-07 contract test `test_plugins_rejects_*` | plugins maintainer | 2026-08-15 | **Mitigated — verified** (bandit 0 HIGH/MEDIUM; bug_hunt `plugins#1` closed) |
| **R7** | Pathological DAG exhausts memory / CPU (adversarial submit cycles) | 2 | 3 | 6 | MEDIUM | Resource exhaustion / graph | Cycle detection (DFS) + depth cap + `levels` limit in `service/dag.py` (FR-06); per-FR Gate1 `test_fr06_*` | service maintainer | 2026-09-15 | **Mitigated — monitored** |
| **R8** | Plugin hook exception aborts the whole queue run | 3 | 3 | 9 | **HIGH** | Resilience / isolation | Per-hook `try/except` returning `PluginLoadError`; consecutive-failure auto-disable (FR-07); `cli/commands.run_cmd` treats plugin error as task failure, not fatal | plugins maintainer | 2026-09-01 | **Mitigated — verified** (bug_hunt `plugins#2` refuted; isolation contract holds) |
| **R9** | Dependency introduce non-allowlisted license (NFR-07 violation) | 2 | 3 | 6 | MEDIUM | Legal / supply-chain | Pinned versions + `pip-licenses --format=json` + scancode gate (NFR-07); SBOM in `harness/sbom/` | release maintainer | Continuous (every release) | **Mitigated — monitored** (Gate4 license dim 100; 0 unknown) |
| **R10** | Audit log grows unbounded; storage and grep latency degrade | 5 | 2 | 10 | **HIGH** | Operational / capacity | `audit.jsonl` is append-only; **log rotation is the operator's responsibility** — not implemented in this round (declared known limitation in `SPEC.md` §9 R10 + `FINAL_SIGN_OFF.md`) | operator / Johnny | Operator-side; tracked in `RISK_STATUS_REPORT.md` watch-list | **Accepted risk** — explicit limitation; documented in `RELEASE_NOTES.md` and `FINAL_SIGN_OFF.md` |

### Residual risks raised by Gate 3 / Gate 4 / bug hunt

| ID | Risk | L | I | Score | Band | Category | Mitigation | Status |
|----|------|---|---|-------|------|----------|------------|--------|
| **R11** | Bandit LOW B404 in `executor.py:27` (subprocess import) | 4 | 1 | 4 | LOW | Static-analysis noise | `subprocess` is required for FR-02 hook; documented exemption under `# noqa: B404` + comment. Bandit dim stays 98 (penalty 2 × 1) | **Accepted** |
| **R12** | Mutation testing excluded by `features.mutation_testing=false` in `.methodology/harness_config.json` | 2 | 3 | 6 | MEDIUM | Test-quality / regression | NFR-08 mutation contract is satisfied **per-FR at Gate 1**; project-wide mutmut run is contractually N/A. Re-enabling `features.mutation_testing=true` in P9 if mutation score stability becomes a release blocker | **Accepted (contractual)** |
| **R13** | `performance` dimension recorded as `None` (no pytest-benchmark fixtures) | 3 | 2 | 6 | MEDIUM | Latency SLA / regression | NFR-01 SLAs are functionally validated in-process via `time.perf_counter()` (test_nfr01_a, test_nfr01_b). Adding benchmark fixtures would duplicate coverage without strengthening the SLA; deferred to a future phase with stable throughput baselines | **Accepted (deferred)** |
| **R14** | CRG architecture dimension depends on `crg_cohesion_healthy=0.2` calibration override | 2 | 3 | 6 | MEDIUM | Tooling calibration | Defence captured in Gate4 devil-advocate evidence: 12/12 healthy communities, 0 oversized, 0 large_functions_penalty; calibration is per-project protocol-blessed for ≤ ~10-file packages (CRG docs) | **Accepted (calibration evidence filed)** |

### Closed-mitigated rows (audit trail only — already RESOLVED in current release)

These items were confirmed in `.methodology/bug_hunt_report.json` and were closed
during Phase 6 fix commits `723b708c` and `c94a01c`. They are retained here so
that any future regression is quickly traced back to the originating risk.

| ID (legacy) | Original finding | Severity | Fix commit | Mapped risk |
|-------------|------------------|----------|------------|-------------|
| `executor#1` | `run --all` bypasses the OPEN circuit breaker | critical | `723b708c` | R3, R8 |
| `audit#1` | `stdout_tail`/`stderr_tail` persisted without NFR-04 redaction | critical | `723b708c` | R5 |
| `audit#2` | Legacy `audit.log` recorded raw command without redaction | high | `723b708c` | R5 |
| `task_store#1` | Corrupt `tasks.json` silently rebuilt, destroying evidence | high | `723b708c` | R1 |
| `task_store#2` | Concurrent `submit` lost task records (lost update) | high | `723b708c` | R1 |
| `executor#2` | `run --all` batch path emitted no audit events | high | `723b708c` | R8 |
| `plugins#1` | Rejected plugin name exited 1 via generic safety net | high | `723b708c` | R6 |

---

## 4. Watch-list Triggers (re-evaluate when any of these occur)

1. Any Gate 4 dimension score drops below the threshold on a future round.
2. A new FR is added that touches `executor`, `plugins`, or `task_store` (the
   three high-risk modules flagged in `SPEC.md` §10).
3. `gitleaks` reports > 0 leak; bandit reports a new HIGH/MEDIUM.
4. `harness_config.json` `features.mutation_testing` is flipped from `false` to
   `true` (re-enable R12 mitigation plan).
5. `tasks.json` or `audit.jsonl` exceeds 100 MB on an operator machine (R10
   capacity ceiling; rotate or migrate to logrotate / vector).
6. CRG `community_cohesion.score` falls below the per-round threshold (R14).

---

## 5. Provenance

- **SPEC §9 authoritative cell content** read at `/Users/johnny/projects/taskq-plus/SPEC.md` lines 428–441.
- **Gate 3 / Gate 4 result files** at `/Users/johnny/projects/taskq-plus/.methodology/gate{3,4}_result.json`.
- **Bug-hunt findings** at `/Users/johnny/projects/taskq-plus/.methodology/bug_hunt_report.json` (generated `2026-07-31T01:23:11Z`, sha `723b708c`).
- **Quality manifest** at `/Users/johnny/projects/taskq-plus/.methodology/quality_manifest.json` (`gate_results.gate4.open_critical/high/medium/low = 0`).
- **Final sign-off** at `/Users/johnny/projects/taskq-plus/FINAL_SIGN_OFF.md` (Gate 4 composite 98.71, release READY).
- **Phase 7 plan constraints** at `/Users/johnny/projects/taskq-plus/.methodology/phase7_plan.md` (Hard Rules HR-04/05/16/17 apply).

---

_End of `RISK_REGISTER.md`._
