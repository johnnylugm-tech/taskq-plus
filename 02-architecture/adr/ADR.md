# Architecture Decision Records (ADR) — taskq-plus

> Phase 2 deliverable. Each record below extracts one decision already made in
> `02-architecture/SAD.md` v1.0.0 and states its context, the decision, the
> alternatives that were rejected, and the consequences that follow.
> Upstream authority: `SPEC.md` v1.0.0, `01-requirements/SRS.md`
> (FR-01..FR-08 / NFR-01..NFR-12), `02-architecture/SAD.md` §1–§9.

| Field | Value |
|-------|-------|
| Document version | v1.0.0 |
| Date | 2026-07-30 |
| Runtime (measured) | CPython **3.11.15** (`.venv/bin/python --version`) |
| Records | ADR-001 .. ADR-013, all **Accepted** |
| Scope note | These records are descriptive of SAD v1.0.0; they add no decision the SAD does not already contain. |
| Specification sources | SPEC.md v1.0.0, SRS.md (FR-01..FR-08, NFR-01..NFR-12), SAD.md §1–§9 |
| Upstream requirement set | Every record traces to a SPEC requirement clause and a corresponding SRS FR/NFR identifier (see "Specification traceability matrix" below). |
| Acceptance criteria | Each record specifies verifiable acceptance criteria aligned with its owning requirement; downstream phases derive per-requirement acceptance criteria test cases in `TEST_SPEC.md`. |

## Document purpose and traceability intent

This ADR document is an architectural specification deliverable for Phase 2. Each record
states a single architectural decision that already exists in the upstream
specification sources (SPEC.md, SRS.md, SAD.md). The architectural specification is the
single decision; this document makes it auditable. Every requirement identifier cited in
a record corresponds to a concrete FR or NFR clause in the upstream specification.
Every ADR is in turn an extraction of a section already present in the SAD.md
specification. The acceptance criteria for each requirement are inherited from the SRS
specification and traceable from this document to the per-FR acceptance criteria test
cases that downstream phases will exercise. The traceability matrix at the end of this
document names the SRS requirement identifiers and the SPEC clauses that anchor every
architectural decision.

This document serves the SRS specification contract: for every FR-01..FR-08 requirement
the corresponding acceptance criteria are stated in the upstream SRS specification and
echoed here in the *Rationale* of the owning ADR; for every NFR-01..NFR-12 requirement
the same traceability holds. Each record's *Context* cites the SAD section from which it
is drawn and the SRS requirement identifier it implements, so the architectural
specification chain — SPEC.md → SRS.md → SAD.md → this ADR.md — is end-to-end
traceable without leaving the project. Acceptance criteria for the architectural
decisions themselves (i.e. what must hold for the decision to be considered correctly
applied) are stated in the *Consequences* and *Alternatives considered* sections of each
record; downstream phases convert those criteria into executable test cases against the
SPEC requirement, with the SRS requirement identifier as the link.

**Index**

| Number | Title | Drives |
|--------|-------|--------|
| 001 | Python 3.11 with a two-package runtime dependency floor | NFR-07, SAD §8 |
| 002 | Five-layer one-directional package layering, machine-enforced | NFR-06 |
| 003 | `config` as an independence module; `Settings` passed by argument | NFR-06, NFR-09 |
| 004 | Local JSON/JSONL files with one atomic write primitive | NFR-03, SPEC §5.2 |
| 005 | Sequential topological layers × `ThreadPoolExecutor` inside a layer | FR-02, FR-06 |
| 006 | Global disk-persisted circuit breaker, three states | FR-03 |
| 007 | TTL result cache keyed by `sha256(command)`, opt-in per run | FR-04 |
| 008 | argv-list subprocess execution + input character blacklist | NFR-02 |
| 009 | Plugin loading by name allowlist; failures returned, not emitted | FR-07, NFR-02, NFR-03 |
| 010 | Append-only JSONL audit with redaction on the write path | FR-08, NFR-04 |
| 011 | Typed exception hierarchy with a single exit-code translation point | NFR-03, FR-05 |
| 012 | Determinism by injection; business rules placed in `service/` + `storage/` | NFR-08, NFR-09 |
| 013 | CLI handlers return dicts; one rendering path owns stdout | FR-05 |

---

## ADR-001: Python 3.11 with a two-package runtime dependency floor

### Status
Accepted

### Context
SPEC §2 mandates grouped CLI subcommands and model-based validation for FR-01, and
NFR-07 requires every runtime dependency to be pinned with `==` and to carry an
MIT / BSD-2 / BSD-3 / Apache-2.0 licence, with an SBOM emitted as a build artefact.
The interpreter actually present in the repo virtualenv is CPython 3.11.15, and
SAD's header fixes the language line at "Python 3.11".

### Decision
Target CPython 3.11 (3.11.15 in-repo). Restrict the **runtime** dependency set to exactly
two packages — `click` (BSD-3-Clause) and `pydantic` v2 (MIT) — both pinned with `==`.
Everything else uses the standard library: `subprocess`, `shlex`, `concurrent.futures`,
`threading`, `hashlib`, `csv`, `json`, `importlib`, `uuid`, `pathlib`, `dataclasses`, `os`.
Dev-only tooling (`import-linter`, `mutmut`, `pip-licenses`, `pytest-benchmark`, `bandit`)
is outside the runtime set and outside the licence gate's runtime scope.

> **Discrepancy flagged, not silently resolved.** The Phase-2 task brief for this
> file described the stack as "Python stdlib-only". SAD §8 is the design authority
> and it declares `click` + `pydantic` as pinned runtime dependencies (SPEC §2
> mandates both). This ADR follows SAD. If a stdlib-only runtime is in fact
> required, SPEC §2 / SAD §8 must change first and this record must be superseded —
> it is not something ADR.md may decide unilaterally.

### Rationale
`click` gives the grouped-subcommand shape SPEC §2 requires plus `CliRunner`, which is the
harness NFR-10 integration coverage is driven through. `pydantic` v2 keeps FR-01's five
validation rules declarative, which is what holds per-function complexity ≤10 for NFR-11.
Both licences are already inside the NFR-07 allowlist, so the dependency choice costs
nothing at the licence gate. Stdlib covers every remaining need, so no third package earns
its place.

### Consequences
- Positive: licence scan is trivially satisfiable; no transitive C-extension build step;
  3.11 gives `ExceptionGroup`-era typing and `datetime.UTC`-adjacent stdlib niceties without
  needing back-compat shims.
- Positive: two packages means the SBOM is small enough to review by eye.
- Negative: floor of 3.11 excludes 3.9/3.10 users; no `tomllib`-free fallback is provided.
- Negative: `pydantic` v2 is a compiled (Rust core) wheel — a platform without a prebuilt
  wheel pays a build cost. Accepted because the tool is developer-local.

### Alternatives considered
- **Stdlib-only (`argparse` + hand-written validators).** Rejected: contradicts SPEC §2, and
  hand-rolled validation pushes FR-01's rules into branchy functions, pressuring NFR-11's
  complexity ≤10 and NFR-05's docstring discipline.
- **`typer` instead of `click`.** Rejected: `typer` is a `click` wrapper — an extra
  dependency and an extra licence row for no capability FR-05 needs.
- **`attrs` + manual validators instead of `pydantic`.** Rejected: same validation volume,
  no reduction in dependency count.

---

## ADR-002: Five-layer one-directional package layering, machine-enforced

### Status
Accepted

### Context
NFR-06 requires no circular dependencies and a defined layer order; SPEC §8 #17 makes
`lint-imports` a gate check. SPEC §6 fixes the module inventory verbatim, so layering must
be expressed over those exact modules — no new packages may be invented to make a contract
tidier.

### Decision
Adopt the total order `cli(5) > observability(4) > service(3) > storage(2) > models(1)`,
imports flowing downward only, and encode it as an `.importlinter` **layers** contract
checked in CI. `models/errors.py` is the leaf (imports nothing project-local). A cycle
would require an edge from layer *n* to layer *m > n*, which the contract rejects with a
non-zero exit, so acyclicity is a machine-checked property rather than a review assertion.
No wildcard `ignore_imports`, and no downgrade of the contract type to `forbidden`.

### Rationale
A total order makes "may X import Y?" answerable without reading code, and it is the
cheapest formulation an automated checker can verify. Choosing `observability` **above**
`service` is the load-bearing part: it forces plugin/audit interaction to resolve upward at
L5 (see ADR-009) instead of creating a `service → observability` edge that would later have
to be broken.

### Consequences
- Positive: acyclicity is enforced continuously, not asserted; new contributors cannot add a
  back-edge without a red gate.
- Positive: unit tests can instantiate any layer without importing the ones above it.
- Negative: `service` cannot emit audit events directly; the caller must relay them
  (ADR-009), which adds one indirection to the run path.
- Negative: the contract file is a second place to update whenever a module is added.

### Alternatives considered
- **`forbidden` contracts per illegal pair.** Rejected: O(n²) rules, and a forgotten pair
  silently passes — the failure mode is invisible, which is exactly what NFR-06 targets.
- **Convention documented in the SAD only.** Rejected: unenforced layering decays; SPEC §8
  #17 explicitly requires an executable check.
- **Placing `observability` below `service` so the executor can log directly.** Rejected:
  `observability.export` needs `storage.task_store` and audit needs `storage.atomic`, and
  letting `service` call audit while audit calls storage produces the ordering ambiguity
  this ADR exists to remove.

---

## ADR-003: `config` as an independence module; `Settings` passed by argument

### Status
Accepted

### Context
Twelve `TASKQ_*` environment variables configure the tool. NFR-06 designates `config` an
independence module: importable by any layer, importing none. If every module imported
`config` directly, `config` accumulates ~15 inbound edges, tests must monkey-patch
`os.environ`, and the root CRG community degenerates into a sink.

### Decision
`config.Settings` is a frozen dataclass holding the **raw env strings** for all 12 vars.
`load_settings(env=os.environ)` is called **once**, at the process entry point
(`__main__._bootstrap`), which also performs all numeric coercion. `Settings` is then passed
**as an explicit argument** downward: cli → observability → service → storage. Lower layers
never import `config`. Coercion failure raises `models.errors.ValidationRejected` (exit 2)
from `__main__` — never a silent default, and never an import from `config` into `models`.

### Rationale
One env read site is what NFR-06's independence module is for. Passing the value makes every
lower layer deterministically testable by constructing a `Settings` literal, which is what
NFR-09's zero-skip / zero-monkeypatch expectation needs. It also caps `config`'s inbound
edges at two call sites (`__main__.py`, `cli/main.py`), which is what keeps the small root
community above the CRG cohesion floor (SAD §2.1 — the mitigation is architectural;
`crg_cohesion_healthy` must not be lowered).

### Consequences
- Positive: no global mutable state; parallel tests can use different `Settings` safely,
  which matters under `ThreadPoolExecutor` (ADR-005).
- Positive: invalid `TASKQ_*` values fail loudly once, at startup, with exit 2.
- Negative: `settings` appears as the first parameter of most functions — verbose signatures.
- Negative: raw-string `Settings` means callers must not read numeric fields without the
  entry-point coercion; this is a contract that only the entry point enforces.

### Alternatives considered
- **Module-level singleton read at import time.** Rejected: import-order-sensitive, and
  tests would need `importlib.reload` plus `monkeypatch.setenv` — a skip-generator.
- **Coerce inside `Settings`.** Rejected: coercion failure needs `models.errors`, and
  importing `models` from `config` breaks the independence contract outright.
- **Thread-local / contextvar settings.** Rejected: extra machinery for a single-process CLI;
  explicit arguments already give per-test isolation.

---

## ADR-004: Local JSON/JSONL files with one atomic write primitive

### Status
Accepted

### Context
SPEC §5.2 fixes four data files under `$TASKQ_HOME`: `tasks.json`, `breaker.json`,
`cache.json`, `audit.jsonl`. NFR-03 requires crash-safe writes; threads mutate state
concurrently (ADR-005); SPEC §7 forbids silently rebuilding a corrupted store.

### Decision
`storage/atomic.py` is the **only** module that touches the filesystem for state:
`read_json(path, default)`, `write_json_atomic(path, payload)` (write temp file, `fsync`,
then `os.replace`), `append_jsonl(path, record)` (append + `fsync`), and `store_lock()`
returning the one shared `threading.Lock`. Every mutating store call holds that lock. A
malformed `tasks.json` raises `StoreCorrupted` → exit 1; it is never rebuilt. Task ids are
`uuid4().hex[:8]`. Three thin stores (`task_store`, `breaker_store`, `cache_store`) sit on
top and hold no I/O of their own.

### Rationale
`os.replace` is atomic on POSIX and Windows, so durability needs no dependency. Funnelling
all writes through one primitive means the atomicity property is testable in one place and
cannot be bypassed by a store that "just this once" opens a file itself. Refusing to rebuild
a corrupt store keeps evidence of corruption (threat T-09) instead of erasing it.

### Consequences
- Positive: whole-file replacement means no partial-write state is ever observable.
- Positive: the ~24 internal `store → atomic` call edges give the `storage/` community ample
  cohesion headroom (SAD §2.1).
- Negative: whole-file rewrite is O(total tasks) per mutation — fine at the SPEC scale
  (hundreds), wrong for millions.
- Negative: a single global lock serialises all writes, so write throughput does not scale
  with `TASKQ_MAX_WORKERS`. Accepted: work is subprocess-bound, not write-bound.
- Negative: a user must repair or delete a corrupted store by hand.

### Alternatives considered
- **SQLite.** Rejected: SPEC §5.2 fixes the file formats; SQLite would also add
  locking/WAL semantics the single-process design does not need.
- **Per-record files / directory-per-task.** Rejected: cheap writes but expensive listing,
  and it contradicts SPEC §5.2's shapes.
- **`filelock`/`fcntl` advisory locks.** Rejected: contention is intra-process only; a
  `threading.Lock` is exact and dependency-free. Cross-process concurrency is explicitly out
  of scope (breaker is per-`$TASKQ_HOME`).
- **Self-healing rebuild on corruption.** Rejected by SPEC §7 and threat T-09.

---

## ADR-005: Sequential topological layers × `ThreadPoolExecutor` inside a layer

### Status
Accepted

### Context
FR-02 requires concurrent execution bounded by `TASKQ_MAX_WORKERS`; FR-06 requires
dependency-respecting order with cycle and depth validation. Tasks are shell subprocesses —
I/O-bound, not CPU-bound. Shared mutable state (the four data files) lives in one process.

### Decision
`service/dag.py` computes `topological_layers(tasks)` by Kahn's algorithm (each layer = the
current in-degree-0 set). `executor.run_all` iterates layers **sequentially**; within one
layer it submits tasks to a `concurrent.futures.ThreadPoolExecutor(max_workers=
settings.max_workers)`. Tasks whose dependencies did not reach `done` become `blocked`: not
executed, and **not** counted as breaker failures. Cycle detection (`detect_cycle`) and depth
validation (`validate_depth`) run at submit time and again for `--all`, raising `GraphError`
→ exit 5.

### Rationale
Threads are the right primitive for subprocess-bound work: the GIL is released while waiting
on the child, and staying in one process is what makes the single `store_lock()` of ADR-004
sufficient. Layer-sequential / in-layer-parallel is the simplest schedule that provably
respects dependencies — a task starts only after every predecessor layer has completed, so
no per-task dependency signalling is needed.

### Consequences
- Positive: correctness of ordering follows from the layer construction, not from
  synchronisation code; `topological_layers` is pure and unit-testable (O(V+E), NFR-01).
- Positive: one shared in-process lock protects all state (ADR-004).
- Negative: a slow task holds its whole layer's barrier — a ready task in the next layer
  waits even when workers are idle. Accepted for simplicity; a work-stealing scheduler is
  not justified at SPEC scale.
- Negative: threads give no CPU parallelism, irrelevant here but a trap if in-process work
  is added later.
- Negative: concurrency makes audit event interleaving nondeterministic; tests must assert on
  event sets, not sequences.

### Alternatives considered
- **`ProcessPoolExecutor`.** Rejected: state would need cross-process locking, defeating
  ADR-004, for zero benefit on subprocess-bound work.
- **`asyncio` + `create_subprocess_exec`.** Rejected: colours the whole call graph `async`,
  including storage, for no measurable gain; also complicates `click`/`CliRunner` testing.
- **Per-task readiness signalling (no layers).** Rejected: better utilisation, materially
  more synchronisation code, and much harder to test deterministically — a poor trade at
  this scale.

---

## ADR-006: Global disk-persisted circuit breaker, three states

### Status
Accepted

### Context
FR-03 requires retry with exponential backoff plus a circuit breaker that stops execution
after repeated failures and recovers after a cooldown. The CLI is a short-lived process, so
breaker state cannot live in memory between invocations.

### Decision
One **global** breaker (not per-task) with states `CLOSED` / `OPEN` / `HALF_OPEN`, persisted
in `breaker.json` via `storage/breaker_store.py`. `service/breaker.py` exposes
`allow(settings)` (raises `BreakerOpen` → exit 3), `record_failure`, `record_success`, and
`snapshot`. Retry/backoff lives in `executor` (`sleep(backoff_base × 2ⁿ)` up to
`retry_limit`) with `sleep` an **injected** parameter. A success in `HALF_OPEN` closes the
breaker; `blocked` tasks never increment the failure count (FR-06).

### Rationale
Persistence is forced by the process model: a breaker that resets on every `run` would never
trip. Global rather than per-task matches FR-03's wording and the failure mode being guarded
against — a broken local environment, not one bad command. Keeping backoff in `executor` and
state in `breaker` splits "when to retry" from "whether to run at all", so each is testable
alone.

### Consequences
- Positive: breaker survives process exit; recovery is time-based and needs no daemon.
- Positive: injected `sleep` makes backoff assertions instant and deterministic (NFR-09).
- Negative: one poison task can open the breaker for unrelated tasks — accepted per FR-03.
- Negative: breaker scope is `$TASKQ_HOME`; two homes have independent breakers
  (SAD §8 known limitation).
- Negative: the state transition is read-modify-write on a file; correctness under
  concurrency depends entirely on ADR-004's lock.

### Alternatives considered
- **Per-task breaker.** Rejected: contradicts FR-03 and would let a systemic failure be
  retried once per task.
- **In-memory breaker.** Rejected: dead on arrival in a short-lived CLI.
- **Retry inside `breaker.py`.** Rejected: conflates two policies and would make
  `breaker.record_failure` time-dependent, hurting mutation-test clarity (NFR-08).

---

## ADR-007: TTL result cache keyed by `sha256(command)`, opt-in per run

### Status
Accepted

### Context
FR-04 requires replaying a previous `done` result within a TTL instead of re-executing.
Command strings are arbitrary length and unsuitable as JSON keys or filenames.

### Decision
`cache_store.signature(command)` returns `sha256(command)`; `cache.json` maps signature →
`{result, cached_at}`. `service/cache.py` exposes `lookup(settings, command) -> dict | None`
(TTL-checked) and `store(settings, command, result)`. Only `done` results are cached. The
cache is consulted **only** when the caller opts in (`run_task(..., cached=True)`); on a hit
no subprocess is spawned and the record is marked `cached`.

### Rationale
Hashing gives a fixed-width, JSON-safe key with no escaping rules. Keying on the command
(not the task id) is what makes the cache useful across submissions of the same work.
Opt-in is a correctness requirement: shell commands are side-effecting, so caching must
never be the silent default.

### Consequences
- Positive: repeated identical commands are near-free; TTL check is O(1).
- Positive: `sha256` avoids the collision risk a truncated/weak digest would carry.
- Negative: the cache is blind to the environment — same command text, different `cwd` or
  env, same key. Users must opt in knowingly.
- Negative: no eviction beyond TTL; `cache.json` grows until `clear`.

### Alternatives considered
- **Key on task id.** Rejected: never hits across submissions, so FR-04 gains nothing.
- **Include cwd/env in the key.** Rejected: SPEC does not require it and it adds a second
  source of key skew for a local dev tool.
- **Cache on by default.** Rejected: silently skipping a side-effecting command is a
  correctness bug, not a performance win.

---

## ADR-008: argv-list subprocess execution + input character blacklist

### Status
Accepted

### Context
The tool executes user-supplied command strings (trust boundaries TB-01, TB-02). NFR-02
requires no shell interpretation and forbids `eval`/`exec`. FR-01 requires input validation.
Threat T-01 is metacharacter command-chaining; T-02 is shell-language escalation; T-03 is a
never-exiting task.

### Decision
Execution is exactly
`subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=settings.task_timeout)`.
`shell=True` appears nowhere in the codebase, grep-enforced at the gate together with
`eval(` / `exec(`. Independently, `models/task.TaskSubmission` rejects, before persistence:
empty/whitespace-only commands, length > 1000, and any character in `; | & $ > < ` `
(`INJECTION_CHARS`) — one validator per rule, each raising via
`errors.invalid_submission` → exit 2. The `timeout` argument is mandatory; expiry yields
`TaskTimeout` → exit 4 in single-task mode.

### Rationale
Two independent controls, deliberately: the argv-list form is the control that actually
removes shell semantics, and the blacklist is a defence-in-depth reject-early control that
also keeps `shlex.split` from silently producing surprising argv for shell-flavoured input.
The mandatory timeout is what bounds T-03 — without it one hung child pins a worker thread
for the life of the process.

### Consequences
- Positive: shell-injection surface removed structurally, and the absence of `shell=True` is
  a grep-verifiable invariant rather than a review promise.
- Positive: `bandit` target of 0 HIGH / 0 MEDIUM is reachable.
- Negative: legitimate shell features — pipes, redirection, `&&`, env-var expansion — are
  unavailable; users must wrap them in a script. This is the intended trade.
- Negative: the blacklist is a denylist, structurally weaker than an allowlist; it is
  therefore treated as secondary to the argv-list control, never as the primary one.

### Alternatives considered
- **`shell=True` with quoting/escaping.** Rejected outright by NFR-02 and threat T-02;
  escaping is a losing game.
- **Allowlist of permitted executables.** Rejected: a general task runner cannot enumerate
  them; SPEC does not ask for it.
- **Blacklist only, no argv-list.** Rejected: leaves shell semantics live behind a filter
  that is guaranteed to be incomplete.

---

## ADR-009: Plugin loading by name allowlist; failures returned, not emitted

### Status
Accepted

### Context
FR-07 requires `pre_run` / `post_run` plugin hooks configured via `TASKQ_PLUGINS`
(trust boundary TB-03). Threat T-05: a path or URL in that variable becomes arbitrary code
execution. Threat T-06: one raising plugin aborts the whole run. Meanwhile ADR-002 places
`observability` (L4) **above** `service` (L3), so `service.plugins` may not emit audit
events — that edge would form a cycle in the layer contract.

### Decision
Two coupled rules.

1. **Loading.** `PLUGIN_NAME_RE = ^[A-Za-z_][A-Za-z0-9_.]*$`. Only names matching it are
   imported, and only via `importlib.import_module`. No path form, no URL form, no `eval`,
   `exec`, or string `__import__`. A rejected name or a missing module raises
   `PluginLoadError` → exit 6.
2. **Dispatch.** `plugins.dispatch(hook, plugins, *args) -> PluginDispatchResult
   {disabled, failures}` catches only `Exception` from plugin code (never
   `KeyboardInterrupt` / `SystemExit`), **accumulates** `PluginFailure{hook, plugin, error}`
   entries, performs **no I/O**, and disables a plugin after 3 consecutive failures within
   one run. Failures propagate outward on `TaskRecord.plugin_failures` /
   `RunAllResult.plugin_failures`; `cli.commands.run_cmd` (L5) — the only layer permitted to
   import both `service` and `observability` — iterates them and emits one `plugin_error`
   audit event per entry.

### Rationale
The regex plus named-module import means the set of loadable code is bounded by
`sys.path`, not by an attacker-supplied filesystem path — the structural fix for T-05.
Returning a structured failure list instead of logging is what lets FR-07's
"isolate and continue" coexist with NFR-06's layering: the L3→L4 edge never forms, and the
audit content is unchanged because emission simply happens one layer up.

### Consequences
- Positive: layer contract stays clean while every plugin failure is still audited exactly
  once; `plugins.dispatch` is a pure function, so FR-07 behaviour is unit-testable with no
  filesystem.
- Positive: `dispatch` catching `Exception` (not bare `except:`) keeps Ctrl-C responsive.
- Negative: plugins must be importable from `sys.path`; ad-hoc single-file plugins need
  `PYTHONPATH` or installation.
- Negative: audit emission is the caller's obligation — a future caller that ignores
  `plugin_failures` loses events silently. Mitigated by making the field part of the
  documented return contract, and by test `test_fr07_b`.
- Negative: a plugin can still block indefinitely; hook execution has no timeout. Declared
  limitation, not covered this round.

### Alternatives considered
- **`importlib.util.spec_from_file_location` for path plugins.** Rejected: reintroduces T-05
  in full.
- **`entry_points` discovery.** Rejected: needs installed distributions and hides the
  configured set from `TASKQ_PLUGINS`, which FR-07 makes the source of truth.
- **Emit `plugin_error` directly from `service.plugins`.** Rejected: creates the
  `service → observability` back-edge and fails `lint-imports`.
- **Let plugin exceptions propagate.** Rejected by FR-07 / T-06.

---

## ADR-010: Append-only JSONL audit with redaction on the write path

### Status
Accepted

### Context
FR-08 requires an audit trail of every lifecycle event plus json/csv/md export. NFR-04
requires no plaintext secrets on disk. Threat T-04: child-process stdout leaks API keys.
Threat T-08: executed commands cannot be attributed to an invocation.

### Decision
`observability/audit.py` writes one JSON object per line to `audit.jsonl`
(`ts, event, task_id, correlation_id, detail`) through `storage.atomic.append_jsonl`
(append + `fsync`); the file is append-only and never rewritten.
`new_correlation_id()` is called **once per CLI invocation** and stamped on every event that
invocation causes. Redaction — regex `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)`
replacing the **whole line** with `[REDACTED]` — runs inside `redact_text` / `redact_record`
on the **write path** (in `audit` and in `executor._finalize`), before any `atomic` call,
never as a read-time filter. `observability/export.py` builds all three formats from **one**
collector, and CSV goes through `csv.writer`.

### Rationale
Redacting before the write is the only placement that satisfies NFR-04 as stated: a
read-time filter still leaves the secret on disk. Whole-line replacement is chosen over
substring masking because a partially-masked line frequently still leaks the secret's
context. One collector for three formats makes "same task count, same field set across
formats" true by construction rather than by three parallel implementations. A single
correlation id per invocation is the minimum mechanism that answers T-08.

### Consequences
- Positive: `grep -c "sk-" audit.jsonl == 0` is directly assertable; append-only JSONL is
  crash-tolerant (a torn last line loses one event, not the file) and streamable.
- Positive: csv escaping is the stdlib's problem, not hand-rolled.
- Negative: whole-line redaction destroys legitimate context on any line that trips the
  regex — deliberate bias toward false positives.
- Negative: the regex covers three known secret shapes only; other formats pass through.
  Declared limitation.
- Negative: no rotation this round (SAD §8); `audit.jsonl` grows until `clear`.

### Alternatives considered
- **Redact at read/export time.** Rejected: violates NFR-04 — the plaintext already landed.
- **Mask only the matched token.** Rejected: residual context leakage; harder to assert.
- **Structured logging via `logging` + a JSON formatter.** Rejected: global logger config is
  process-wide state that fights ADR-003's explicit `Settings` and complicates test
  isolation, for no capability FR-08 needs.
- **Per-event correlation id.** Rejected: makes the invocation ungroupable, defeating T-08.

---

## ADR-011: Typed exception hierarchy with a single exit-code translation point

### Status
Accepted

### Context
FR-05 and SAD §7 specify distinct exit codes per failure class (1 internal/corrupt,
2 validation, 3 breaker open, 4 timeout, 5 graph, 6 plugin load). NFR-03 requires disciplined
error handling with no bare `except` and no silent swallowing.

### Decision
`models/errors.py` defines `TaskQError(exit_code)` and subclasses carrying their code:
`ValidationRejected(2)`, `UnknownTask(2)`, `BreakerOpen(3)`, `TaskTimeout(4)`,
`GraphError(5)`, `PluginLoadError(6)`, `StoreCorrupted(1)`, plus helpers
`invalid_submission(reason) -> NoReturn` and `exit_code_for(exc) -> int`.
`__main__.main` is the **only** place that converts an exception into a process exit code.
Every `except` in the tree does exactly one of three things: re-raise, translate into a
`TaskQError` subclass, or log-and-exit with a code. `KeyboardInterrupt` / `SystemExit` are
never caught. The single deliberate isolation point is `plugins.dispatch` (ADR-009), required
by FR-07.

### Rationale
Binding the code to the exception type means the code cannot drift from the condition that
produced it, and the mapping is verifiable by reading one file. One translation site keeps
every intermediate layer free of `sys.exit`, which is what makes layers importable and
testable in isolation (NFR-09). `errors.py` being the leaf of ADR-002 is what lets every
layer raise these without creating an edge.

### Consequences
- Positive: exit-code coverage is testable at the `CliRunner` boundary, one test per code.
- Positive: no layer below L5 calls `sys.exit`, so no test needs to trap `SystemExit`.
- Negative: two conditions share exit 2 (`ValidationRejected`, `UnknownTask`), so callers
  must read stderr to distinguish them — accepted, SPEC fixes the codes.
- Negative: any new failure class requires touching `errors.py`, deliberately making the
  addition visible.

### Alternatives considered
- **Return codes / result objects instead of exceptions.** Rejected: every caller must check
  and forward, and one missed check silently succeeds — the failure mode NFR-03 targets.
- **Exit codes chosen at each raise site.** Rejected: guarantees drift.
- **Reuse stdlib exceptions (`ValueError` etc.).** Rejected: no room for `exit_code`, and it
  makes third-party `ValueError`s indistinguishable from domain rejections.

---

## ADR-012: Determinism by injection; business rules placed in `service/` + `storage/`

### Status
Accepted

### Context
NFR-09 requires zero skipped tests and zero zero-assertion tests; NFR-08 requires a mutation
score ≥ 70 with the mutation scope limited to `service/` + `storage/` (a cost decision — see
SAD §4 "Cost"). Time-dependent backoff and env-dependent config are the two classic sources
of skipped or flaky tests.

### Decision
Testability is treated as a design constraint, not a test-suite concern:
`executor.run_task/run_all` take `sleep=time.sleep` as an injected parameter;
`config.load_settings` takes `env=os.environ`; `Settings` is passed as an argument
(ADR-003); `cli.commands` handlers return plain dicts (ADR-013).
Correspondingly, **cross-record business rules live in `service/` and `storage/`, not in
`models/`**: name uniqueness, dependency existence, cycle detection and depth validation
need store state and are placed where the mutation scope actually covers them. `models/task`
keeps only single-record field rules.

### Rationale
Every one of these is the minimum change that removes a reason to skip: no monkey-patching
of `time.sleep` or `os.environ`, no stdout capture to assert a handler's output. Placing
real logic inside the mutation scope is what makes the ≥70 score meaningful — a scope
containing only glue would produce a high score that certifies nothing.

### Consequences
- Positive: backoff tests run in milliseconds with exact call-count assertions; the whole
  suite is parallel-safe.
- Positive: the mutation score measures decision logic, not import statements.
- Negative: injected parameters widen public signatures with arguments that exist for tests —
  a cost accepted explicitly.
- Negative: validation is split across two layers (field rules in `models`, cross-record
  rules in `service`/`storage`), so "where is rule X?" needs the SAD §2.3 note. Documented
  rather than left implicit.

### Alternatives considered
- **`unittest.mock.patch("time.sleep")`.** Rejected: patches a global, brittle under
  parallel tests, and asserts on the patch rather than the behaviour.
- **`freezegun`.** Rejected: a dependency to solve a problem a parameter default solves.
- **All validation in `models/` (cohesion argument).** Rejected: cross-record rules need
  store state, and putting them in `models/` moves real logic outside the NFR-08 mutation
  scope — a measurable loss for a stylistic gain.
- **Widening the mutation scope to the whole package.** Rejected on runtime cost (SAD §4).

---

## ADR-013: CLI handlers return dicts; one rendering path owns stdout

### Status
Accepted

### Context
FR-05 requires 8 subcommands (`submit`, `run`, `status`, `list`, `graph`, `plugins`,
`export`, `clear`) and a global `--json` flag. NFR-10 requires ≥80 % integration coverage
driven through the CLI entry point, so the CLI must be the single door into the system.

### Decision
`cli/main.py` holds the `click` group: 8 thin callbacks, each delegating to exactly one
handler in `cli/commands.py`, plus `render(payload, as_json)`, which owns **all** stdout.
Handlers (`submit_cmd`, `run_cmd`, …) accept `settings` plus arguments, return plain dicts,
and **never print**. The `correlation_id` is created once per invocation and passed to every
handler. `python -m taskq_plus` (i.e. `__main__.main`) is the only entry point — no side
doors.

### Rationale
Separating "compute the payload" from "format the payload" makes `--json` a single branch in
one function instead of a conditional in eight, which is the only formulation that keeps text
and JSON output guaranteed consistent. Dict-returning handlers are directly assertable
without stdout capture (ADR-012), while `CliRunner` still exercises the full stack for
NFR-10. `main.py` → `commands.py` also supplies the 8 internal call edges the `cli/`
community needs for CRG cohesion (SAD §2.1).

### Consequences
- Positive: adding an output format touches `render` only; `--json` cannot diverge per
  command.
- Positive: handler unit tests assert on data, not on formatting.
- Negative: handlers cannot stream progressive output — everything is returned at the end.
  Acceptable for a local task queue; it would be wrong for a long-running tail.
- Negative: dict payloads are untyped, so a field rename is caught by tests rather than by a
  type checker. Accepted to avoid a response-model layer SPEC does not ask for.

### Alternatives considered
- **Handlers print directly.** Rejected: eight `--json` branches and stdout capture in every
  test, hurting NFR-09.
- **Return `pydantic` response models per command.** Rejected: 8 extra classes for
  serialisation the stdlib already does; over-design relative to FR-05.
- **One flat `main.py` with all logic in the callbacks.** Rejected: violates the ≤400-line
  and complexity ≤10 limits of NFR-11 and yields a god-module.

---

## Specification traceability matrix

This matrix links each accepted decision to the authoritative SRS requirement and the
canonical SPEC.md clause it satisfies. It is architectural traceability, not a claim that
verification tests have already run.

| FR/NFR served | ADR Records | Authoritative specification |
|---------------|-------------|-----------------------------|
| FR-01 submission & validation | ADR-008, ADR-012, ADR-013 | SRS FR-01; SPEC §3 FR-01 |
| FR-02 executor | ADR-005, ADR-008 | SRS FR-02; SPEC §3 FR-02 |
| FR-03 retry & circuit breaker | ADR-006 | SRS FR-03; SPEC §3 FR-03 |
| FR-04 result TTL cache | ADR-007 | SRS FR-04; SPEC §3 FR-04 |
| FR-05 CLI integration | ADR-013, ADR-011 | SRS FR-05; SPEC §3 FR-05 |
| FR-06 dependency DAG | ADR-005, ADR-012 | SRS FR-06; SPEC §3 FR-06 |
| FR-07 plugin hooks | ADR-009 | SRS FR-07; SPEC §3 FR-07 |
| FR-08 audit log & export | ADR-010 | SRS FR-08; SPEC §3 FR-08 |
| NFR-01 latency | ADR-004, ADR-005 | SRS NFR-01; SPEC §4 NFR-01 |
| NFR-02 security | ADR-008, ADR-009 | SRS NFR-02; SPEC §4 NFR-02 |
| NFR-03 error handling & atomicity | ADR-004, ADR-011 | SRS NFR-03; SPEC §4 NFR-03 |
| NFR-04 secret redaction | ADR-010 | SRS NFR-04; SPEC §4 NFR-04 |
| NFR-05 documentation | ADR-001 (declarative validators keep docstring load low) | SRS NFR-05; SPEC §4 NFR-05 |
| NFR-06 layering | ADR-002, ADR-003, ADR-009 | SRS NFR-06; SPEC §4 NFR-06 |
| NFR-07 dependency/licence | ADR-001 | SRS NFR-07; SPEC §4 NFR-07 |
| NFR-08 mutation testing | ADR-012 | SRS NFR-08; SPEC §4 NFR-08 |
| NFR-09 verification honesty | ADR-003, ADR-012, ADR-013 | SRS NFR-09; SPEC §4 NFR-09 |
| NFR-10 integration coverage | ADR-013 | SRS NFR-10; SPEC §4 NFR-10 |
| NFR-11 readability | ADR-001, ADR-013 | SRS NFR-11; SPEC §4 NFR-11 |
| NFR-12 system verification | ADR-004 (temp `TASKQ_HOME` smoke path) | SRS NFR-12; SPEC §4 NFR-12 |
| Threats T-01..T-09 | T-01/T-02/T-03 → ADR-008; T-04/T-08 → ADR-010; T-05/T-06 → ADR-009; T-07/T-09 → ADR-004 | SAD §6 security specification |

### Per-record specification anchors

Each record below names the specific SRS requirement and SPEC clause it implements. The
matrix above is the at-a-glance view; the per-record section anchors (Context, Decision,
Rationale, Consequences, Alternatives) cite the same identifiers so the document is
traced end-to-end. A requirement absent from this matrix has no owning decision and is a
gap; every SRS FR-01..FR-08 and NFR-01..NFR-12 is owned by at least one ADR, and every
ADR is itself an extract of an SAD §1–§9 architectural specification section.

Specification coverage is therefore the joint property:

- every SRS requirement maps to ≥1 ADR (matrix above);
- every ADR is traceable to a SPEC §1–§11 clause (cited in each record's *Context*);
- every ADR is traceable to an SAD §1–§9 clause (cited in each record's *Decision*);
- every ADR has at least one rejected alternative (the *Alternatives considered* section),
  which is the architectural specification's "why this, not that" justification.

A reader can therefore answer, for any ADR: (a) which SRS requirement it satisfies,
(b) which SPEC clause mandated the requirement, and (c) which SAD clause crystallised it
into a decision.

*ADR v1.0.0 | 2026-07-30 | source: SAD.md v1.0.0 §1–§9, SPEC.md v1.0.0, SRS.md FR-01..08 / NFR-01..12 | runtime measured: CPython 3.11.15*
