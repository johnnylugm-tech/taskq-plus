# RELEASE_CHECKLIST

## Pre-Release Checks
- [ ] All P1-P7 phases completed and artifacts generated.
- [ ] CI pipeline fully passed.
- [ ] Final Sign Off approved.
- [ ] Production environment provisioned.
- [ ] Rollback plan documented.

## Human Context (P8 append)

- **Deployment runbook URL**: <https://github.com/johnnylugm-tech/taskq-plus/blob/main/08-config/RELEASE_CHECKLIST.md> (this release checklist; taskq-plus is a local CLI with no remote deployment target).
- **Rollback owner + on-call**: repository operator Johnny owns rollback. No separate on-call rotation is configured; escalate release incidents to the repository owner.
- **Post-release monitoring dashboard**: <https://github.com/johnnylugm-tech/taskq-plus/actions/workflows/harness_quality_gate.yml>. Runtime monitoring is host-local via `$TASKQ_AUDIT_LOG` (default `$TASKQ_HOME/audit.jsonl`); no centralized runtime dashboard is configured.
- **Customer communications template**:
  > **Subject:** taskq-plus `<version>` release update
  > taskq-plus `<version>` (`<git-hash>`) was `<released|rolled back>` at `<UTC timestamp>`. User impact: `<impact or none>`. Validation: Gate 4 `<score>`, FR coverage `<passed>/<total>`. Required action: `<action or none>`. Status and updates: `<URL>`.
