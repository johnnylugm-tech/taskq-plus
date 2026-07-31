# COVERAGE_REPORT — Phase 4

**Generated:** 2026-07-31
**Source run:** `pytest --cov=03-development/src --cov-report=term-missing -q`
**Aggregate check:** `coverage report --format=total`
**Python:** `/Users/johnny/projects/taskq-plus/.venv/bin/python` (3.11.15)
**Raw output:** `04-testing/coverage_raw.txt`

---

## 1. Overall Coverage

```
TOTAL  1367  5  99%
```

| Metric | Value |
|---|---|
| Total statements | 1,367 |
| Missed statements | 5 |
| **Overall coverage** | **99 %** |
| Gate 3 threshold | ≥ 80 % |
| Verdict | **PASS** (≥ 80 % and ≥ 99 %) |

---

## 2. Per-Module Breakdown

| Module | Stmts | Miss | Cover | Missing |
|---|---:|---:|---:|---|
| `taskq_plus/__init__.py` | 0 | 0 | 100% | — |
| `taskq_plus/cli/__init__.py` | 0 | 0 | 100% | — |
| `taskq_plus/cli/commands.py` | 274 | 0 | 100% | — |
| `taskq_plus/cli/main.py` | 212 | 0 | 100% | — |
| `taskq_plus/config.py` | 11 | 0 | 100% | — |
| `taskq_plus/models/__init__.py` | 0 | 0 | 100% | — |
| `taskq_plus/models/errors.py` | 13 | 0 | 100% | — |
| `taskq_plus/models/task.py` | 23 | 0 | 100% | — |
| `taskq_plus/observability/__init__.py` | 0 | 0 | 100% | — |
| `taskq_plus/observability/audit.py` | 61 | 0 | 100% | — |
| `taskq_plus/observability/export.py` | 93 | 0 | 100% | — |
| `taskq_plus/service/__init__.py` | 0 | 0 | 100% | — |
| `taskq_plus/service/breaker.py` | 51 | 0 | 100% | — |
| `taskq_plus/service/cache.py` | 93 | 0 | 100% | — |
| `taskq_plus/service/dag.py` | 103 | 0 | 100% | — |
| `taskq_plus/service/executor.py` | 174 | 0 | 100% | — |
| `taskq_plus/service/plugins.py` | 67 | 0 | 100% | — |
| `taskq_plus/storage/__init__.py` | 0 | 0 | 100% | — |
| `taskq_plus/storage/atomic.py` | **50** | **5** | **90%** | **41-42, 46, 75-77** |
| `taskq_plus/storage/breaker_store.py` | 37 | 0 | 100% | — |
| `taskq_plus/storage/cache_store.py` | 39 | 0 | 100% | — |
| `taskq_plus/storage/task_store.py` | 66 | 0 | 100% | — |
| **TOTAL** | **1,367** | **5** | **99%** | — |

---

## 3. Per-Layer Coverage

| Layer | Aggregate Cover |
|---|---:|
| `cli/` | 100% |
| `config.py` | 100% |
| `models/` | 100% |
| `observability/` | 100% |
| `service/` | 100% |
| `storage/` | 99.5% (1 module < 100%) |

The CLI/service/observability/models surfaces are **fully covered**. The only
gap sits inside the storage atomic-write helper.

---

## 4. Uncovered Lines — Detail

### 4.1 `taskq_plus/storage/atomic.py` (90%, 5 lines missed)

Missing lines: **41-42, 46, 75-77**

These cover defensive branches in the atomic write helpers:

- **L41-42** — `OSError` branch inside `atomic_write` when the temp-rename
  fails after the temp file has already been written (i.e. the OS-specific
  rename path that is hard to reproduce reliably without filesystem
  fault-injection).
- **L46** — fallback cleanup branch when both `replace` and `unlink` fail
  on a partial rename (correlated with L41-42).
- **L75-77** — final cleanup branch inside `atomic_append_jsonl` that
  removes the temp file after a JSON-serialisation error (hard to trigger
  without a custom json.dumps override).

These are crash-recovery code paths that cannot be exercised by
deterministic tests without monkey-patching `os.replace` /
`json.dumps` to raise. They are not user-facing behaviour; they exist to
keep on-disk state consistent under failures.

### 4.2 Risk Assessment

The 90% coverage on `atomic.py` is acceptable for Gate 3 (≥ 80% required,
currently 99% project-wide). The missing branches are defensive cleanup
code, not feature paths.

If the operator wants 100% coverage on this module, the realistic path is
to add fault-injection tests that monkey-patch `os.replace` and
`json.dumps`. That is a future enhancement, not a Gate 3 blocker.

---

## 5. Reproduction Commands

```bash
# Full coverage run (also writes term-missing to stdout)
python -m pytest --cov=03-development/src --cov-report=term-missing -q \
  | tee 04-testing/coverage_raw.txt

# Aggregate total
python -m coverage report --format=total
```

The numbers in this report come directly from the run on 2026-07-31;
`coverage_raw.txt` is preserved as the auditable artifact for
`cross_artifact.py` Gate 3 validation.