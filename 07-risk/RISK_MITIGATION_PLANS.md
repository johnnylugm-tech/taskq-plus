# Risk Mitigation Plans — taskq-plus (Phase 7)

> **Project**: taskq-plus
> **Phase**: 7 — Risk Management
> **Generated**: 2026-08-01
> **Scope**: Formal mitigation plans for risks with score ≥ 9 (HIGH band) per `RISK_REGISTER.md` §3
> **Companion docs**: `RISK_REGISTER.md`, `RISK_STATUS_REPORT.md`

This document only covers the **seven** HIGH-band items (`L × I ≥ 9`):

| Risk | Score | Plan below |
|------|-------|------------|
| R1 Concurrent write corruption | 15 | **MP-1** |
| R2 Subprocess hang / zombie | 12 | **MP-2** |
| R5 Secret written to disk | 15 | **MP-3** |
| R6 Plugin → arbitrary-code-execution | 15 | **MP-4** |
| R8 Plugin exception aborts queue | 9 | **MP-5** |
| R10 Audit-log unbounded growth | 10 | **MP-6** (accepted-risk plan) |
| (R3 6 / R4 6 / R7 6 / R9 6 / R12 6 / R13 6 / R14 6) | MEDIUM | Tracked only in `RISK_STATUS_REPORT.md`; not promoted here |

---

## MP-1 — R1 Concurrent `tasks.json` write corruption (score 15)

**Risk statement.** Two or more `submit` processes racing on the same
`tasks.json` may lose updates or write torn JSON, because the gate-level
`fcntl` lock + atomic-rename contract can be bypassed if a third-party tool
ever edits the file directly.

**Goals (verifiable).**
1. Concurrent `submit` of N=8 tasks from 4 parallel processes yields exactly
   8 distinct records with strict ordering on disk. *(regression test:
   `tests/test_fr01_store_concurrent.py` — already in suite, passing)*
2. Partial-write / corrupt file is detected and recovered via
   `StoreCorrupted`, never silently re-built. *(regression test:
   `tests/test_bug_hunt_repro.py::test_task_store_corrupt_recovery` — fixed in
   `723b708c`)*

**Mitigation steps.**
1. **Already in place** — `storage/atomic.py` does `O_CREAT | O_EXCL` write +
   `fsync` + `os.replace` under `fcntl.flock`. Coverage 100 % in `tests/`.
2. **Already in place** — `task_store._read_or_init` checks `json.JSONDecodeError`
   and raises `StoreCorrupted`; `submit` surfaces the error to the user.
3. **To add (next patch train)** — emit a `WARN` log entry on
   `StoreCorrupted` so operators can spot tampering attempts early.
4. **To add** — document a "do not hand-edit `tasks.json`" warning in the
   CLI `--help` for `submit` and `status`.

**Owner**: storage maintainer (FR-01, FR-05 module ownership).
**Target date**: 2026-08-15 (next patch train).
**Verification**: run `pytest -q tests/test_fr01_store_concurrent.py
tests/test_bug_hunt_repro.py`; both must be GREEN.

---

## MP-2 — R2 Subprocess hang / zombie (score 12)

**Risk statement.** A child process that ignores SIGTERM after `timeout=` can
pin a worker thread, leak file descriptors, and accumulate zombies across
long runs.

**Goals.**
1. Every `subprocess.run` / `subprocess.Popen` call carries an explicit
   `timeout=` (HR-Reliability lint gate must stay GREEN).
2. After timeout, child is escalated to SIGKILL via the same
   `kill(grace → SIGTERM → SIGKILL)` ladder that `subprocess.run` itself uses
   on `TimeoutExpired`.

**Mitigation steps.**
1. **Already in place** — `executor.run_with_retry` always passes `timeout=`.
   `preflight_reliability_lint` enforces this contract; P7 entry verified
   GREEN.
2. **Already in place** — `try/finally` around `Popen` ensures `wait()` is
   called even on caller exceptions (no zombies).
3. **To add (defence in depth)** — when NFR-03 raises, log the surviving PID
   to `audit.jsonl` so operators can `kill -9` manually if the kernel has
   leaked a stuck child.
4. **To add** — extend `test_nfr03_*` to mock a subprocess that *ignores*
   SIGTERM, and assert SIGKILL escalation.

**Owner**: executor maintainer (FR-02).
**Target date**: 2026-09-01.
**Verification**: `pytest -q tests/test_nfr03_*` + extended SIGKILL test.

---

## MP-3 — R5 Secret written to disk via `tasks.json` (score 15)

**Risk statement.** A task whose `command` or stdout/stderr embeds a secret
(`sk-...`, AWS key, GH token) may persist that secret in plaintext through
`stdout_tail` or `stderr_tail` fields, defeating NFR-04's "no secret on disk"
acceptance criterion #22.

**Goals.**
1. Acceptance #22 (`grep -c "sk-" $TASKQ_HOME/tasks.json == 0`) holds for
   every released commit.
2. `audit.jsonl` writes go through `redact_text` *before* `emit`; the
   regexes are unit-tested against a fixture of known secret shapes.
3. `tasks.json` writes go through `redact_text` *before* `_set_status`.

**Mitigation steps.**
1. **Already in place (P6 fix `723b708c`)** — `executor._build_result`
   applies `redact_text` to `stdout_tail` / `stderr_tail` before handing the
   dict back to `_set_status`. Bug-hunt finding `audit#1` is CLOSED.
2. **Already in place** — `audit.AuditLogger.redact_detail` and
   `audit.export.redact_text` apply the same allow-listed regex set.
3. **Already in place** — Acceptance test #22 (`make verify-system`) runs
   `grep -c "sk-" $TASKQ_HOME/audit.jsonl` and asserts 0; P7-extended to
   also assert against `tasks.json`.
4. **To add** — make `redact_text` additive: introduce a regex list file
   (`audit/patterns.toml`) so new secret shapes can be added without a
   code change.
5. **To add** — pre-commit hook (`detect-secrets` or `gitleaks`) to fail
   any commit that introduces a known-shaped secret into test fixtures.

**Owner**: audit maintainer (FR-08, NFR-04).
**Target date**: 2026-08-15.
**Verification**: `make verify-system` exit 0 + grep test on `tasks.json`
must report 0 occurrences.

---

## MP-4 — R6 Plugin loader → arbitrary-code-execution entry-point (score 15)

**Risk statement.** A misconfigured plugin (path-traversal `..` in name,
`eval`/`exec` in plugin source, dynamic import through `__import__`) could
turn the plugin hook into a privilege-escalation vector.

**Goals.**
1. Plugin names must match `^[A-Za-z_][A-Za-z0-9_]{0,63}$` — any other
   shape is rejected with `PluginNameError`, not silently loaded.
2. `import-linter` enforces `no_eval_exec_dynamic_import` as a hard
   architecture constraint (SPEC.md Architecture Constraints section).
3. `bandit -r 03-development/src/` reports 0 HIGH, 0 MEDIUM (Gate 4: 2 LOW
   only, both sanctioned).

**Mitigation steps.**
1. **Already in place** — `service/plugins.py:_load_named` enforces the
   regex whitelist; `FR-07` contract test
   `test_plugins_rejects_malformed_name` is GREEN.
2. **Already in place** — `import-linter` block in `pyproject.toml` forbids
   `import ast, eval, exec, __import__, importlib.import_module` inside
   `service/plugins.py`.
3. **Already in place** — `plugins#1` bug-hunt finding closed: the safety
   net now returns a typed error instead of `exit 1`.
4. **To add** — add a fuzz test that submits 1,000 random plugin names and
   asserts every one is either loaded (matches the allowlist) or rejected
   with a typed error, never raises.
5. **To add** — add SBOM-level constraint: any plugin runtime that contains
   `os.system`, `subprocess.Popen` (with `shell=True`), `pickle.loads`, or
   `marshal.loads` is forbidden by `bandit` config.

**Owner**: plugins maintainer (FR-07, NFR-02).
**Target date**: 2026-08-15.
**Verification**: `pytest -q tests/test_plugins_rejects_*` + fuzz test +
`bandit -r 03-development/src/` 0 HIGH/MEDIUM.

---

## MP-5 — R8 Plugin exception aborts the queue (score 9)

**Risk statement.** A plugin whose `pre_run` or `post_run` hook raises an
uncaught exception currently propagates and aborts the calling
`run_cmd`, leaving tasks in inconsistent partial states.

**Goals.**
1. A raising plugin hook is contained: the *hook* fails, the *task* is
   recorded with `status=failed` (or `status=skipped` if pre-run hook
   fails), the *queue* continues with the next task.
2. Consecutive plugin failures beyond a configured threshold auto-disable
   that plugin and emit a `WARN` audit entry (FR-07 requirement).

**Mitigation steps.**
1. **Already in place** — `service.plugins.invoke` wraps every hook in
   `try/except`, returning `PluginHookError`. `cli/commands.run_cmd`
   treats it as a task failure, not a fatal.
2. **Already in place** — `plugins#2` bug-hunt finding refuted:
   `test_plugins_hook_exception_isolated` is GREEN.
3. **Already in place** — `breaker.increment_on_plugin_error` records the
   failure for the consecutive-failure auto-disable path.
4. **To add** — surface plugin-auto-disable events in
   `status` (column "warnings").
5. **To add** — operator override flag
   `TASKQ_PLUGINS_AUTO_DISABLE=false` for trusted plugin sets.

**Owner**: plugins maintainer (FR-07).
**Target date**: 2026-09-01.
**Verification**: integration test simulating 5 consecutive plugin errors
must show the queue still finishes the remaining 3 healthy tasks.

---

## MP-6 — R10 Audit-log unbounded growth (score 10) — **accepted-risk plan**

**Risk statement.** `audit.jsonl` is append-only and **log rotation is the
operator's responsibility** per `SPEC.md` §9 R10 and `FINAL_SIGN_OFF.md`
Known Limitations. Storage and `grep` latency degrade as the file grows.

**Goals (operator-facing).**
1. Document the operator-side rotation contract before any 1.0.0 tag.
2. Provide a one-line `newsyslog.conf` snippet (or equivalent systemd /
   logrotate stanza) suitable for macOS + Linux.

**Mitigation steps.**
1. **Already documented** — `SPEC.md` §9 R10 explicitly notes "append-only,
   rotation is operator's responsibility — **not implemented this round**,
   listed as a known limitation".
2. **Already documented** — `FINAL_SIGN_OFF.md` re-states the limitation in
   the release evidence trail.
3. **To add (this phase, low cost)** — write `09-maintenance/AUDIT_LOG_ROTATION.md`
   with copy-pasteable `logrotate` / `newsyslog.conf` snippets and a
   rationale (`audit.jsonl` is the source of truth; rotating silently
   could drop events that an incident post-mortem needs).
4. **To add (next phase)** — investigate a `taskq-plus audit rotate`
   subcommand that ships a windowed compaction tool; only if operators
   request it (don't gold-plate).

**Owner**: operator (Johnny) — this is **not** an in-code mitigation.
**Target date**: before next minor release cut.
**Verification**: document file exists and is non-empty;
`grep -c "" audit.jsonl` remains bounded after a rotation dry run on a
sample 50 MB file.

**Why this is "accepted" rather than "fixed":** the limitation was
declared in `SPEC.md` before implementation; rotating the file would
silently destroy evidence needed for security audits and would conflict
with NFR-08 / NFR-09 traceability claims. Bounded growth is a deployment
concern, not a code-correctness concern.

---

## Status Snapshot

| Plan | Status | Next action | Trigger |
|------|--------|-------------|---------|
| MP-1 (R1) | Mitigated — verified | Add `WARN` log on `StoreCorrupted` | next patch train (≤ 2026-08-15) |
| MP-2 (R2) | Mitigated — verified | Add SIGKILL escalation test | 2026-09-01 |
| MP-3 (R5) | Mitigated — verified | Add `patterns.toml` + pre-commit `gitleaks` hook | 2026-08-15 |
| MP-4 (R6) | Mitigated — verified | Add plugin-name fuzz test | 2026-08-15 |
| MP-5 (R8) | Mitigated — verified | Add `TASKQ_PLUGINS_AUTO_DISABLE` override | 2026-09-01 |
| MP-6 (R10) | Accepted-risk | Write `09-maintenance/AUDIT_LOG_ROTATION.md` | next patch train |

---

_End of `RISK_MITIGATION_PLANS.md`._
