# CONFIG_RECORDS.md - taskq-plus

> On-demand Lazy Load template.

## 1. Version Information
- Version: vharness-v4-20260801-score99-14-gac8a444
- Git Commit: ac8a444
- Release Date: 2026-07-31

## 2. Runtime Configuration
| Environment | Config |
|-------------|--------|
| Development | {{config}} |
| Production | {{config}} |

## 3. Dependency List
```
{{pip freeze / npm lock output}}
```

## 4. Environment Variables
| Variable | Type | Description |
|----------|------|-------------|
| {{VAR}} | secret | {{description}} |

## 5. Deployment Log
| Date | Version | Method | Executor |
|------|---------|--------|----------|
| 2026-07-31 | harness-v4-20260801-score99-14-gac8a444 | {{method}} | {{name}} |

## 6. Configuration Change Log
| Phase | Change | Rationale |
|-------|--------|----------|
| Phase 8 | {{change}} | {{reason}} |

## 7. Rollback SOP
**Trigger Condition**: {{condition}}
**Commands**:
```bash
{{rollback commands}}
```

## 8. Configuration Compliance
- [ ] Phase 7 risk mitigations implemented
- [ ] Monitoring thresholds configured
- [ ] Circuit breaker enabled

## Human Context (P8 append)

### Configuration ownership

| Config item | Owner | Source of truth |
|-------------|-------|-----------------|
| `TASKQ_HOME` | Storage maintainer / deployment operator | `03-development/src/taskq_plus/config.py`; `storage/{task,cache,breaker}_store.py` |
| `TASKQ_TASK_TIMEOUT`, `TASKQ_MAX_WORKERS` | Executor maintainer | `03-development/src/taskq_plus/service/executor.py` |
| `TASKQ_RETRY_LIMIT`, `TASKQ_BACKOFF_BASE` | Executor maintainer | `03-development/src/taskq_plus/service/executor.py` |
| `TASKQ_BREAKER_THRESHOLD`, `TASKQ_BREAKER_COOLDOWN` | Breaker maintainer | `03-development/src/taskq_plus/service/breaker.py`; `service/executor.py` |
| `TASKQ_CACHE_TTL` | Cache maintainer | `03-development/src/taskq_plus/service/cache.py` |
| `TASKQ_MAX_DAG_DEPTH` | DAG maintainer | `03-development/src/taskq_plus/service/dag.py`; `cli/commands.py` |
| `TASKQ_PLUGINS` | Plugins maintainer | `03-development/src/taskq_plus/service/plugins.py` |
| `TASKQ_AUDIT_LOG` | Audit maintainer / deployment operator | `03-development/src/taskq_plus/observability/audit.py` |
| `features.mutation_testing`, `values.phase_truth_threshold` | Harness owner | `.methodology/harness_config.json` |

### Secret rotation cadence

The current configuration inventory contains no application-owned credential or secret value, so scheduled rotation is not applicable. Credentials inherited by submitted task processes remain externally owned and follow their provider's rotation policy; rotate immediately after suspected exposure.

### Access audit log reference

Operational access and task events are recorded in the append-only JSONL journal selected by `$TASKQ_AUDIT_LOG`, defaulting to `$TASKQ_HOME/audit.jsonl`. The implementation reference is `03-development/src/taskq_plus/observability/audit.py`; host filesystem permissions govern journal access, and secret-pattern redaction occurs before each write.
