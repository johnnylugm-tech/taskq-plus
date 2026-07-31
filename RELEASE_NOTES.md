# Release Notes — taskq-plus

> **Project**: taskq-plus (local task-queue CLI, Python 3.11)
> **Version**: gate4-20260731-score98 (Gate 4 release, second pass)
> **Release Commit**: `bd63f05` — `release(P6): Gate4 PASS score=98.7 — pipeline complete`
> **Generated**: 2026-08-01

---

## Quality Score

- **Gate 4 Composite**: **98.71 / 100** (PASS, threshold ≥ 85)
  - Source: `.methodology/quality_manifest.json` `gate_results.gate4.overall_score = 98.71`
    (persistent source-of-truth for per-phase gate results, per `phase6_plan.md` v2.12.0).
  - Verified against `.methodology/gate4_result.json` and `06-quality/QUALITY_REPORT.md`
    (overall score `98.707/100`).
- **Round 1 Gate 4**: 98.5 (commit `30e1173`) — superseded by Round 2 below.

---

## Changes Since Prior Release

The prior release commit (`30e1173 release(P6): Gate4 PASS score=98.5 — pipeline complete`,
2026-07-31) shipped at score 98.5. The following Round-2 changes are included in
**this** release (`bd63f05`):

### Defect remediation (P6 in-phase)

- `c94a01c` — `fix(p6): eliminate NFR-09 zero-skip violations + complete NFR-06/07 evidence`
  (resolves zero-assertion test functions flagged by Gate 4 NFR-09, and rounds out
  the NFR-06/NFR-07 evidence trails).

### Traceability attestation refresh

- `7136ef5` — `chore: refresh traceability attestation after NFR-06/07/09 fix`
- `5314130` — `trace: regenerate attestation for pre-flight`
- `cdc7db5` — `chore: rebuild traceability attestation (P6 preflight)`

### Harness submodule bumps (no source-code impact)

- `486caad` — `chore: bump harness submodule to 8268573 (Defect A outcome-aware scanning)`

### Net effect on Gate 4 score

Composite moved from **98.5 → 98.71** (Δ ≈ +0.21) after the defect fix landed.

---

## Functional Requirements (8 / 8 PASS)

All eight FRs cleared Gate 1 at score 100.0. Provenance per
`.methodology/quality_manifest.json` `gate_results.gate1`:

| FR ID | Feature | Gate 1 Score |
|-------|---------|--------------|
| FR-01 | Task submission & validation (`submit`) | 100.0 |
| FR-02 | Subprocess execution with concurrency / timeout / retry | 100.0 |
| FR-03 | Exponential backoff retry with jitter | 100.0 |
| FR-04 | TTL result cache | 100.0 |
| FR-05 | CLI command groups (click; `python -m taskq_plus`) | 100.0 |
| FR-06 | Dependency DAG ordering + cycle rejection | 100.0 |
| FR-07 | Allowlisted plugin hook system | 100.0 |
| FR-08 | Structured JSONL audit trail with redaction | 100.0 |

Module traceability and NFR mapping are recorded in
`.methodology/quality_manifest.json` (`fr_module_traceability`,
`nfr_traceability`).

---

## Gate Progression

| Gate | Score | Status | Phase |
|------|-------|--------|-------|
| Gate 1 (per-FR) | 100.0 (all 8 FRs) | PASS | P3 / P5 / P7 / P8 |
| Gate 2 | 95.65 | PASS | P3 exit |
| Gate 3 | 98.93 | PASS | P4 exit |
| **Gate 4** | **98.71** | **PASS** | **P6 full** |

Source: `.methodology/quality_manifest.json` `gate_results.gate{1,2,3,4}`.

---

## Quality Dimensions (14-dim breakdown, Gate 4)

Per `06-quality/QUALITY_REPORT.md`:

| Dimension | Score | Status |
|-----------|-------|--------|
| Linting | 100.0 | PASS |
| Type Safety | 100.0 | PASS |
| Test Coverage | 100.0 | PASS |
| Security | 98.0 | PASS |
| Secrets Scanning | 100.0 | PASS |
| License Compliance | 100.0 | PASS |
| Mutation Testing | N/A | FRAMEWORK-OWNED |
| Architecture (CRG) | 100.0 | PASS (framework_override) |
| Readability | 88.1 | PASS |
| Error Handling | 100.0 | PASS |
| Documentation | 100.0 | PASS |
| Performance | N/A | FRAMEWORK-OWNED |
| Integration Coverage | 100.0 | PASS |
| Test Assertion Quality | 99.8 | PASS |
| Traceability | 100.0 | PASS |

---

## Mutation Testing Note (verbatim from QUALITY_REPORT gate evidence)

Mutation testing is **excluded at project-wide composite** via
`.methodology/harness_config.json` (`features.mutation_testing=false`),
and per `.methodology/quality_manifest.json`
`mutation_testing.excluded_by_feature_flag=true`. The dimension is recorded
as N/A in `06-quality/QUALITY_REPORT.md` and `.methodology/gate4_result.json`.
NFR-08 (mutation score ≥ 70 over `service/` + `storage/`) is satisfied
**contractually at per-FR Gate 1** (`mutation_testing.score=70.0`,
score_type=`contractual`); per-FR Gate 1 mutation scores cleared the ≥ 70
threshold (see `.methodology/gate_results/gate1/`). **No project-wide
mutmut run was performed at P6** — there is **no `.mutmut-cache` file** in
the repository and no project-wide mutation score artifact to cite beyond
the Gate 1 per-FR evidence.

---

## Known Limitations

The following are documented, non-blocking items that the release author
judges acceptable for delivery:

1. **Mutation testing at project-wide composite is N/A** (feature-flag off);
   NFR-08 satisfied contractually at per-FR Gate 1 (see above). Not a release
   blocker per harness config.
2. **Performance dimension at project-wide composite is N/A** (no
   pytest-benchmark tests); NFR-01 latency SLAs validated functionally via
   `test_nfr01_a` / `test_nfr01_b` using `time.perf_counter()`
   (Gate 4 evidence: `gate4_result.json` `performance.tool_evidence`).
3. **Security score 98 / 100** — 0 HIGH / 0 MEDIUM / 2 LOW bandit findings
   (B404 `subprocess` import in `executor.py:27`, required for the FR-07
   plugin hook execution path). Score = 100 − 2×1.
4. **Readability 88.1 / 100** — above the NFR-11 floor of 80; below 90
   reflects honest LLOC-weighted CC scoring on a small project surface.
5. **Harness-side fixture drift** (out of project scope): one harness test
   asserts `5` FRs where SRS now has 8. Deferred to harness owner;
   `harness/` is read-only for non-harness phase work. See
   `04-testing/TEST_RESULTS.md` §3.1.

---

## Verification & Sign-Off Provenance

- **Quality report** (Gate 4): `06-quality/QUALITY_REPORT.md`
- **Verification report** (P5): `05-verification/VERIFICATION_REPORT.md`
- **Baseline** (P5 system-of-record): `05-verification/BASELINE.md`
- **Manifest** (persistent SoT): `.methodology/quality_manifest.json`

---

_Generated by P6 Release Author (orch-post), 2026-08-01._
