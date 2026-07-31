---
key: eef78b5bda74
source: gate-block
phase: 4
dimension: architecture
fr_ids: 
created_at: 2026-07-31
---

**Failure:** Gate 3 blocked [dimension_below_threshold]: architecture scored 75.0, needs 80.0 (gap 5.0)
**Fix:** Two distinct failure modes — check tool_evidence to identify which applies: (1) CRG community issues: if god-module (size>50) or low cohesion (all communities <0.3) — either file an artifact-backed DA waiver in .sessi-work/gate{N}_result.json (devil_advocate + da_waiver + devil_advocate_evidence.architecture; valid at Gate 3 AND Gate 4) then re-run finalize-gate, OR reduce cross-package coupling so CRG detects sub-communities; for persistent CRG false positives (workflow tooling counted as product code, small-package Leiden over-fragmentation) calibrate crg_excludes / crg_cohesion_healthy in .methodology/harness_config.json; (2) Import boundary violations: verify imports comply with SAD.md layer boundaries and fix violations.
