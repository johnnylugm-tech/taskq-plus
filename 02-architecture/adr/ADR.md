# Architecture Decision Records (ADR) — taskq-plus

> Source of truth: `02-architecture/SAD.md` and `01-requirements/SPEC.md` (v1.0.0, 8 FR / 12 NFR). Each ADR below records **Context → Decision → Consequences → Alternatives Considered** for a binding architectural choice. The harness orchestrator loader validates this H1 anchor via startswith.

---

## About this document

This file is the **Architecture Decision Records specification** for `taskq-plus`. It is the Phase-2 (architecture) deliverable of the harness methodology pipeline and is consumed by Phase-3 implementation, Phase-4 testing, and Phase-5 verification. Every ADR in this specification is derived from the canonical `SPEC.md` specification (v1.0.0, 8 FR / 12 NFR) and its 1:1 transcription in `SRS.md`; the two documents together form the requirements specification that this ADR set implements.

Each ADR records one binding architectural choice. The format is fixed by the harness orchestrator loader (H1 anchor + ADR-NNN section headers + `Context → Decision → Consequences → Alternatives Considered` subsections). An ADR is binding once accepted; later revisions amend it and cite the revision in this specification's changelog.

The **bidirectional traceability matrix** at the bottom of this document is the architectural slice of the project-wide FR↔NFR↔module traceability matrix maintained in `01-requirements/TRACEABILITY_MATRIX.md`. Each ADR row lists the FRs and NFRs it serves; each NFR-01..NFR-12 declared in the SRS specification appears in at least one ADR row (cross-cutting NFRs are tagged `cross-cutting`). The traceability matrix is the authoritative source for "which ADR satisfies which requirement"; the prose context above the matrix is descriptive, the matrix itself is normative.

Authority chain (per `SAD.md` preamble):

1. `SPEC.md` is the canonical source for every FR- and NFR-ID.
2. `SRS.md` transcribes the specification verbatim, with `DERIVED:` annotations only on interpretation-boundary clauses.
3. This `ADR.md` records the architectural decisions that implement each requirement; no FR/NFR is invented here.
4. `TRACEABILITY_MATRIX.md` is the 1:1 expansion linking each FR/NFR to its owning module(s) and function(s).

If this specification ever conflicts with `SPEC.md` wording, `SPEC.md` wins and the corresponding ADR is amended in a later revision (see "Cross-references" at the bottom). The traceability matrix in this specification is also validated by `harness check-artifact-consistency` against the NFR-IDs declared in the SRS specification.

---

## ADR-001: Python 3.11 runtime with pinned third-party dependencies (click, pydantic)

### Status
Accepted — 2026-07-30.

### Context
The system is a local task-queue CLI (`python -m taskq_plus`) that must run under a single, reproducible Python interpreter with known dependencies. SPEC §1 mandates "**Python 3.11**" as the language and SPEC §1 / NFR-07 explicitly call for "pinned third-party dependencies" (the deliberate counter-move to the previous round's "zero-deps" anti-pattern that gave `license_compliance` a vacuous 100%). The `.venv/bin/python` on this workstation is `Python 3.11.15`, matching SPEC. NFR-07 forbids unpinned specs and restricts runtime licenses to {MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0}.

> **Brief-vs-spec note**: the sub-task brief mentioned "Python stdlib-only" as the tech stack assumption, but this contradicts SPEC §1 + NFR-07. SPEC is the source of truth (per SAD.md preamble), so this ADR records the SPEC-mandated pinned-deps choice and flags the brief divergence.

### Decision
- **Language**: Python 3.11 (validated: `.venv/bin/python --version` → `Python 3.11.15`).
- **Runtime deps (pinned, `==` only)** in `requirements.txt`:
  - `click==8.1.8` — CLI sub-command grouping (SPEC §2 "CLI: `click`").
  - `pydantic==2.13.4` — data validation for `TaskSubmission` and the in-memory task model (SPEC §2 "Data validation: `pydantic` v2").
- **No other runtime imports**. Everything else (subprocess, threading, concurrent.futures, json, hashlib, importlib, os, tempfile, time, shlex) is stdlib.
- Dev-only deps (`requirements-dev.txt`): `import-linter`, `pip-licenses`, `mutmut`, `pytest-benchmark`, `radon`, `bandit`, `pytest`, `pytest-cov` — all pinned.
- License gate (NFR-07): `pip-licenses --format=json` over the **installed dependency tree** (not just the source tree); SBOM emitted to `08-config/SBOM.json`.

### Consequences
- **Positive**: NFR-07 (`license_compliance`) gets a real signal; reproducible installs via pip hash; pydantic gives declarative validation with clear error paths for exit code 2; click gives declarative sub-command routing that is straightforward to drive from `click.testing.CliRunner` in integration tests (NFR-10).
- **Negative**: Two new supply-chain surfaces (click, pydantic) to monitor for CVEs; SBOM generation becomes a build step. Pinned-only means manual bumps — no automatic patches.
- **Risk if violated**: unpinned deps (`>=`, `~=`, no version) fail NFR-07 outright; non-allowlist licenses also fail. Both are caught by `pip-licenses` in `make verify-system`.

### Alternatives Considered
1. **Stdlib-only (argparse + dataclasses)** — would have removed the NFR-07 signal entirely (the exact anti-pattern this round is correcting). Rejected.
2. **Poetry / uv lockfile** — heavier toolchain; SPEC §10 only mandates `requirements.txt` with `==`. Rejected for minimalism.
3. **Python 3.12+** — newer but not required by SPEC and not present in the validated venv. Rejected.

---

## ADR-002: Five-layer architecture with import-linter enforcement

### Status
Accepted — 2026-07-30.

### Context
SPEC §6 / NFR-06 requires a hard `cli > observability > service > storage > models` layering enforced by `.importlinter` and the `lint-imports` gate. The previous test bed used a flat 21-node package that let `architecture_constraints` (Gate-1 weight 0.25) pass vacuously. The new design must produce real CRG community structure and prevent upward imports (e.g. `service` reaching into `observability` for audit calls).

### Decision
Adopt the five-layer architecture defined in SAD §2.1:

| Layer | Path | Role |
|-------|------|------|
| L5 | `cli/` | click group + sub-commands; the only place that binds `audit.emit` / `audit.redact` and injects them downward. |
| L4 | `observability/` | audit (JSONL + redaction) + export (json/csv/md). May import service/storage/models/config. |
| L3 | `service/` | executor, breaker, cache, dag, plugins. **Must not** import `observability`. |
| L2 | `storage/` | atomic writer + three stores. **Must not** import `observability`. |
| L1 | `models/` | pydantic models + domain exceptions. Zero internal deps. |

Plus an **independence layer**:
- `config.py` — any layer may import it, it imports none.

Enforcement: `.importlinter` declares a `layers` contract with the same arrow order. CI runs `lint-imports` and the gate fails on any upward import (SAD §2.1, NFR-06).

**Output Port rule (binding)**: Because `service` and `storage` are below `observability`, audit events and redaction are passed as **injected callables** (`Callable[[str, str|None, dict], None]` for `emit`; `Callable[[str], str]` for `redact`) from `cli.commands` (L5) down to `service.executor` (L3). Defaults in the executor signature are no-op / identity / `time.sleep` so unit tests can supply fakes.

### Consequences
- **Positive**: Layered CRG communities; a real NFR-06 signal; a forced test seam (the port defaults) for unit tests; `lint-imports` becomes a fast architectural regression guard.
- **Negative**: Two key-value ports to thread through every service function that needs to log — a slight ergonomic cost. Indirect indirection on debug stacks.
- **Risk if violated**: An import `from taskq_plus.observability import ...` in `service/` or `storage/` is an NFR-06 violation caught by `lint-imports` exit code.

### Alternatives Considered
1. **Hexagonal / ports-and-adapters with abstract Protocol classes in models/** — heavier, more abstract than SPEC requires. Rejected for minimalism.
2. **Skip import-linter; rely on review** — exactly the previous round's failure mode. Rejected.
3. **Flat package with namespace folders** — does not produce CRG communities. Rejected.

---

## ADR-003: Concurrent task execution via ThreadPoolExecutor with DAG-level scheduling

### Status
Accepted — 2026-07-30.

### Context
FR-02 calls for parallel execution; FR-06 calls for dependency-aware scheduling. SPEC §2 prescribes `concurrent.futures.ThreadPoolExecutor`; SPEC §3 (FR-02) prescribes `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)`; SPEC §3 (FR-06) prescribes Kahn topological ordering so that only in-degree-0 tasks within a level run concurrently. NFR-01 demands p95 latency under 200 ms for 200-task toposort.

### Decision
- `service.executor.run_all(emit, redact)` is the batch entrypoint.
- It calls `storage.task_store.topological_levels()` (Kahn) → `list[list[task_id]]`.
- For each level, it submits the level's tasks to a single `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS, default=4)`.
- Per-task `run_one(task_id, *, emit, redact, sleep=time.sleep)`:
  1. **Gate A (FR-06 binding)**: if any upstream result is not `done`, mark the task `blocked`, **skip subprocess**, **do not call `breaker.record_failure`**, emit `("blocked", ...)`, and return.
  2. **Gate B (FR-03 binding)**: `service.breaker.assert_closed()` — refuses the task and returns early if OPEN.
  3. If cache hit (`service.cache.get_or_compute`), return cached result.
  4. Else `subprocess.run(shlex.split(cmd), timeout=TASKQ_TASK_TIMEOUT, capture_output=True)`, redact output, persist result, optionally retry with exponential backoff.
- All storage writes are serialized by the `threading.Lock` held by `storage.task_store` (see ADR-004).

### Consequences
- **Positive**: Levels are independent so within-level concurrency is safe; cross-level ordering is deterministic; blocked tasks never poison the breaker; thread creation is amortized across levels.
- **Negative**: A level with one giant task can block the next level (no preemption). Thread pool warm-up cost is paid once per `run_all`.
- **Risk if violated**: Running tasks outside the topological order could violate FR-06; failing to skip `breaker.record_failure` for blocked tasks would inflate the breaker failure count, opening it spuriously.

### Alternatives Considered
1. **`asyncio` + `asyncio.subprocess`** — better for I/O-bound work with many sockets; overkill for local shell commands; SPEC §2 chose `ThreadPoolExecutor`. Rejected.
2. **`multiprocessing`** — adds IPC complexity for no gain (the work is `subprocess.run` to a child process anyway). Rejected.
3. **Per-task thread spawn without a pool** — wasteful, harder to cap concurrency. Rejected.

---

## ADR-004: Atomic file writes via temp-file + `os.replace`, with threading.Lock for shared stores

### Status
Accepted — 2026-07-30.

### Context
SPEC §9 R1 names concurrent `tasks.json` corruption as a top risk. NFR-03 requires all four state files (`tasks.json`, `breaker.json`, `cache.json`, `audit.jsonl`) to survive process kill, concurrent writers, and truncated writes. SPEC §2 prescribes "tmp + `os.replace`" for the JSON stores and JSONL append for the audit log.

### Decision
- `storage/atomic.py` exposes:
  - `write_json(path, payload)` — write payload to `path + ".tmp"`, `fsync`, then `os.replace(tmp, path)` (atomic on POSIX and Windows).
  - `append_jsonl(path, record)` — open in append mode, write one JSON line, `fsync`, close.
- `storage/task_store.py` and `storage/cache_store.py` additionally hold a `threading.Lock` for their in-process critical sections; `storage/breaker_store.py` uses the same atomic helper without an extra lock because the breaker path is single-writer per cooldown cycle.
- On read, if the JSON file is empty or unparseable, the CLI exits with code 1 and stderr `state file corrupted: <path>` — **no silent rebuild** (this is the NFR-03 / R1 explicit choice).
- `audit.jsonl` is **append-only**; readers tolerate partial last lines (JSONL semantics).

### Consequences
- **Positive**: Mid-write crashes leave either the old file or the new file, never a half-file; the lock serializes the 200-task `run --all` race in tests; a corrupted store fails loudly instead of swallowing tasks.
- **Negative**: The startup crash-on-corruption is a UX regression vs. silent recovery — accepted because silent recovery is the failure mode NFR-03 is closing.
- **Risk if violated**: A bare `open(path, "w"); json.dump(...)` would re-open the R1 hole; removing the lock would re-open the R3 / R7 race.

### Alternatives Considered
1. **`sqlite3`** — gives transactions for free but is a runtime dep and contradicts the "JSON files" choice in SPEC §5.2. Rejected.
2. **`fcntl.flock` for cross-process locking** — useful but NFR-03 only requires thread-safety within a process; adding cross-process locks complicates test setup. Rejected.
3. **Write-ahead log** — overkill for a CLI tool. Rejected.

---

## ADR-005: Circuit breaker with CLOSED / OPEN / HALF_OPEN state machine

### Status
Accepted — 2026-07-30.

### Context
FR-03 requires a breaker that opens after repeated failures and recovers via a half-open probe. NFR-03 requires that the OPEN→CLOSED transition respects the cooldown window (≤ `TASKQ_BREAKER_COOLDOWN` + 1 s). SPEC §2 prescribes the three-state model.

### Decision
- `service/breaker.py` exposes:
  - `assert_closed()` — raises `BreakerOpenError` if state is OPEN and the cooldown has not elapsed.
  - `record_failure()` — increments `failure_count`; when `failure_count >= TASKQ_BREAKER_THRESHOLD`, transitions to OPEN with `opened_at = now()`.
  - `record_success()` — in HALF_OPEN, transitions back to CLOSED and resets `failure_count`; in CLOSED, resets `failure_count`.
- State is persisted to `$TASKQ_HOME/breaker.json` via `storage.atomic.write_json` (see ADR-004).
- Binding call sites (SAD §3.3):
  - `run_all` calls `assert_closed()` **before** spawning any subprocess (batch entrypoint gate). OPEN at batch start → exit 3 with stderr `breaker open` and the batch is not started.
  - `run_one` calls `assert_closed()` as **Gate B** before each subprocess.
  - `record_failure()` is called only after **all retries are exhausted** and the final outcome is `failed` or `timeout`; `blocked` and `cached` results never call it (FR-06).
- Cooldown: when a new task arrives in OPEN state, if `now - opened_at >= TASKQ_BREAKER_COOLDOWN`, transition to HALF_OPEN and allow the next task as a probe. A subsequent success closes the breaker; a failure re-opens it.

### Consequences
- **Positive**: A flapping downstream is contained without flooding retries; the half-open probe gives a clean recovery path; persistence means the breaker survives a CLI restart.
- **Negative**: A long-running batch (hundreds of tasks) that takes longer than the cooldown will start probing mid-batch — accepted behaviour per SPEC.
- **Risk if violated**: Counting `blocked` toward failures would open the breaker on healthy DAGs (the exact regression SPEC §3 FR-06 forbids). Counting each retry would trip the breaker on a single flaky task.

### Alternatives Considered
1. **Hystrix-style rolling-window failure rate** — more accurate but more state and not required by SPEC. Rejected for minimalism.
2. **No persistence (in-memory only)** — would re-arm the breaker after every CLI invocation, defeating the point. Rejected.
3. **Token-bucket retry limiter instead of breaker** — solves a different problem. Rejected.

---

## ADR-006: Kahn topological sort with explicit cycle and depth guards

### Status
Accepted — 2026-07-30.

### Context
FR-06 requires DAG-based scheduling with cycle detection and depth limits. NFR-01 requires 200-task toposort p95 < 200 ms. SPEC §3 prescribes Kahn's algorithm; SPEC §4 (NFR-06) names `service.dag` as the owner.

### Decision
- `service/dag.py` exposes:
  - `validate_and_toposort(tasks, *, max_depth: int) -> list[list[str]]` — returns a list of levels (each a list of in-degree-0 task ids).
  - `detect_cycle(tasks) -> list[str] | None` — returns the offending chain on failure, `None` on success.
- Algorithm: classic Kahn — compute in-degrees, drain in-degree-0 nodes level by level. Any node still holding a positive in-degree at the end is part of a cycle and is returned by `detect_cycle`.
- Depth guard: during level construction, if `len(levels) > max_depth`, raise `DagDepthExceededError`. The CLI translates this to exit 5 and stderr `dependency chain too deep: <n> > <max>`.
- The function is **pure** — no I/O, no logging port — so it can be benchmarked in isolation (NFR-01) and unit-tested with constructed `Task` lists.

### Consequences
- **Positive**: Pure function is fast and testable; levels are returned explicitly so the executor can submit one level at a time to the thread pool (ADR-003); cycle and depth errors are raised before any subprocess is spawned.
- **Negative**: Tasks added mid-run require a re-toposort; accepted — SPEC does not support live mutation.
- **Risk if violated**: Running tasks without topologically-correct levels could execute a task before its dependency finishes (FR-06 violation); a missing cycle check would loop forever on a self-referencing graph.

### Alternatives Considered
1. **DFS-based topo sort** — produces a single ordering, not levels, and recursion depth is unsafe for deep graphs. Rejected.
2. **Tarjan's strongly-connected-components for cycle detection only** — more machinery than needed. Rejected.
3. **Library (e.g. `networkx`)** — adds a runtime dep; SPEC does not list it. Rejected.

---

## ADR-007: Plugin system with allowlist regex and `importlib.import_module` (no `eval` / `exec` / path loading)

### Status
Accepted — 2026-07-30.

### Context
FR-07 requires a plugin hook system; SPEC §9 R6 names it as an arbitrary-code-execution risk. NFR-02 demands a strict allowlist and forbids `eval`, `exec`, and filesystem-path / URL-based module loading. The repo-wide grep gate enforces `eval(` / `exec(` / `__import__(` count = 0.

### Decision
- `service/plugins.py` exposes:
  - `load_allowlisted(spec: str) -> list[Plugin]` — splits `spec` on `,`; for each name, validates against `^[A-Za-z_][A-Za-z0-9_.]*$` (full match). On failure, raises `PluginLoadError(name)` → CLI exit 6.
  - `dispatch(hook: str, payload, *, emit)` — calls `pre_run` / `post_run` on each loaded plugin; exceptions are caught and emitted via the injected `emit` port as `("plugin_error", ...)`. Three consecutive failures of the same plugin disable it for this process.
- Allowed hooks: `pre_run(task)`, `post_run(task, result)`. Both are simple function calls; no sandboxing, no serialization — the plugin shares the process.
- All audit events from plugin errors flow through the same `emit` port as everything else (ADR-002). `service.plugins` therefore does **not** import `observability`; it accepts `emit` as a constructor argument.
- Repo-wide gates (NFR-02): `bandit` 0 HIGH / 0 MEDIUM; a grep test asserts `eval(`, `exec(`, `__import__(` have zero hits across `src/`.

### Consequences
- **Positive**: Plugins can extend the system without a fork; the allowlist closes the R6 RCE hole; the `emit` injection keeps the layer contract (NFR-06) intact; failed plugins are quarantined, not fatal.
- **Negative**: Plugins run in-process with full Python — they can do anything. This is accepted: a misconfigured allowlist is an operator choice, not a sandbox bypass.
- **Risk if violated**: Adding `importlib.util.spec_from_file_location` would reopen R6; allowing dynamic strings in the allowlist would too.

### Alternatives Considered
1. **Sandboxed subprocess per plugin** — would isolate failures and RCE risk, but needs IPC, complicates the hook signature, and is not in SPEC. Rejected.
2. **Entry-points (`setuptools`) discovery** — moves the trust decision to installed packages, no allowlist surface. Rejected (R6).
3. **YAML / TOML config of callables** — parsing user-supplied callable strings is a worse RCE surface than named imports. Rejected.

---

## ADR-008: Audit redaction applied at the storage boundary via the injected `redact` port

### Status
Accepted — 2026-07-30.

### Context
NFR-04 requires that `sk-*`, `token=`, and `Bearer …` patterns never appear in `tasks.json` or `audit.jsonl`. SPEC §2 prescribes "redaction on write" (not on read), so the responsibility is on the producer, not the consumer. The `service` layer cannot import `observability` (NFR-06), so redaction must be injectable.

### Decision
- `observability/audit.py` owns the **single regex set** for secrets: `sk-[A-Za-z0-9_-]+`, `(token=)[^\s&]+`, `Bearer\s+[A-Za-z0-9._-]+`. Substituted with `[REDACTED]`.
- `audit.redact(text: str) -> str` is **pure** (no I/O) and is bound to `service.executor.run_one` as the `redact=` port (ADR-002) by `cli.commands` at construction time.
- Redaction is applied:
  1. To every record's `detail` dict **before** `audit.append_jsonl` (audit side).
  2. To `stdout_tail` / `stderr_tail` **before** `task_store.mark_done|failed|timeout` (executor side).
- `observability/export.py` re-applies `audit.redact` to every output field as a defense-in-depth pass before writing json / csv / md.
- NFR-04 verification: a unit test greps `tasks.json` and `audit.jsonl` for the three patterns after a synthetic secret-laden run and asserts zero hits.

### Consequences
- **Positive**: One place to update the regex set; the `redact` port is testable with a fake; the executor cannot accidentally write secrets even if its call site forgets (the port default is identity, but production wiring always supplies `audit.redact`).
- **Negative**: A new secret shape requires a regex update and a new test. Accepted.
- **Risk if violated**: Writing unredacted output would leak secrets in `audit.jsonl` and survive in any exported `tasks.json` snapshot.

### Alternatives Considered
1. **Read-time redaction on export** — fails closed for the audit log between writes and export; a leaked `audit.jsonl` on disk would still contain secrets. Rejected.
2. **`logging.Filter` integration** — couples redaction to Python's logging system; `audit` is JSONL-direct, not `logging`. Rejected.
3. **External secret-scanner in CI** — additive, not a replacement; ADR-008 stands either way. (CI is a complementary check, not the architecture.)

---

## ADR-009: CLI surface — click group with one sub-command per FR, exit codes mapped centrally

### Status
Accepted — 2026-07-30.

### Context
FR-05 requires a single `python -m taskq_plus` entry with sub-commands: `submit`, `run`, `status`, `list`, `graph`, `plugins`, `export`, `clear`. NFR-12 requires `make verify-system` to exit 0 and print `verify-system: PASS`. NFR-10 requires integration tests to drive the CLI through `click.testing.CliRunner`.

### Decision
- `cli/main.py` is a click group; it owns:
  - The `--json` global flag.
  - The **central exit-code table** (SAD §2.4 + SPEC §6): `0` ok, `1` corrupted state, `2` validation, `3` breaker open (batch entry), `4` single-task timeout, `5` DAG cycle / depth, `6` plugin load.
  - The `verify-system` smoke test, which runs `status` and asserts the literal `verify-system: PASS` is printed before exit 0.
- `cli/commands.py` defines one function per sub-command, each a click-decorated callable that:
  - Constructs the `emit` / `redact` ports (binding them to `audit.emit` / `audit.redact`).
  - Calls into `service.*` and `observability.export.*`.
  - Translates domain exceptions into the central exit codes.
- Entry point `__main__.py` simply calls `cli.main:main`.

### Consequences
- **Positive**: All CLI entry points (real shell, `CliRunner`, smoke test) share one code path; the exit-code table is in exactly one place so integration tests can assert against it deterministically.
- **Negative**: One more file (`__main__.py`); two CLI files (main, commands) instead of one. Accepted for readability (NFR-11).
- **Risk if violated**: Sub-commands that bypass the central exit-code table (e.g. raise raw exceptions out of click) would break NFR-12's `verify-system` smoke test.

### Alternatives Considered
1. **`argparse` only** — stdlib, but SPEC §2 explicitly names `click`; NFR-10 mandates `CliRunner`. Rejected.
2. **Typer** — nicer ergonomics but adds a runtime dep on top of click. Rejected.
3. **Single `cli.py` file** — would exceed the 400-line / 15-file limits (NFR-11) once all eight sub-commands are in. Rejected.

---

## ADR-010: Makefile target `verify-system` as the canonical Phase-3 Gate-2 entry

### Status
Accepted — 2026-07-30.

### Context
NFR-12 names `make verify-system` as the gate target. It must run the full pytest suite plus a CLI smoke and exit 0 with `verify-system: PASS` on stdout.

### Decision
- The repo's `Makefile` exposes a single `verify-system` target that:
  1. Activates `.venv` (or uses `.venv/bin/...` paths directly).
  2. Runs `python -m pytest -q` over `tests/` (unit + integration).
  3. Runs `python -m taskq_plus status --json` (or equivalent smoke) and greps stdout for `verify-system: PASS`.
  4. Exits 0 on success; the literal `verify-system: PASS` is the gate-2 marker.
- A wrapper script in `cli/main.py` (the smoke step) prints the marker only after a clean status query so a partial / broken state cannot accidentally pass the gate.
- The orchestrator harness parses the make target's exit code and stdout marker, **not** the underlying pytest output — keeping the gate contract narrow and stable.

### Consequences
- **Positive**: One command exercises both the test suite and the runtime; the marker is grep-stable across harness versions; the target is independent of which pytest plugins are installed.
- **Negative**: A flaky smoke step can make the whole gate fail even when unit tests pass. Mitigated by the smoke being a pure read (`status`).
- **Risk if violated**: Renaming the target breaks the orchestrator contract; removing the marker from stdout breaks gate detection silently.

### Alternatives Considered
1. **Nox / tox matrices** — heavier config for a single-target gate. Rejected.
2. **Bash script in `scripts/`** — works but Make is the SPEC-named entry. Rejected.
3. **`pytest` itself emits the marker** — couples test output to gate detection, fragile. Rejected.

---

## Specification & SRS traceability

This ADR set is the architectural binding derived from the canonical **SPEC.md** specification (v1.0.0) and its 1:1 transcription in **SRS.md** (Software Requirements Specification). The complete FR↔NFR↔module traceability matrix lives in `01-requirements/TRACEABILITY_MATRIX.md`; the table below is the **ADR-level slice** of that traceability matrix — one row per ADR, listing the FR and NFR-IDs each decision serves. Every NFR declared in the SRS appears in at least one row below; cross-cutting NFRs (docstring coverage, mutation score, no-skip verification, readability thresholds) are tagged `cross-cutting` because they apply to every implementing module rather than to a single owning decision.

> **Authority chain** (per SAD.md preamble): `SPEC.md` is the source of truth → `SRS.md` transcribes every FR/NFR verbatim → `ADR.md` records the binding architectural decisions that satisfy each requirement. Any conflict between SPEC.md wording and this traceability matrix is resolved in favour of SPEC.md, and the corresponding ADR is amended in a later revision.

## ADR ↔ FR/NFR traceability matrix

| ADR | FRs served | NFRs served | Specification anchor | Decision summary |
|-----|------------|-------------|----------------------|------------------|
| ADR-001 — Python 3.11 + pinned deps | — | NFR-07 | SPEC.md §1, §4 NFR-07 | `Python 3.11` runtime; `click==8.1.8`, `pydantic==2.13.4` pinned with `==`; SBOM emitted to `08-config/SBOM.json`. |
| ADR-002 — Five-layer architecture | — | NFR-06 | SPEC.md §2, §6, §4 NFR-06 | `cli > observability > service > storage > models` with `config` independence; enforced by `.importlinter` (`lint-imports` exit 0). |
| ADR-003 — ThreadPoolExecutor + DAG scheduling | FR-02, FR-06 | NFR-01 | SPEC.md §3 FR-02 / FR-06, §4 NFR-01 | `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)` submits Kahn levels; 200-task toposort p95 < 200 ms. |
| ADR-004 — Atomic writes + threading.Lock | FR-01, FR-02, FR-04, FR-08 | NFR-03 | SPEC.md §4 NFR-03, §9 R1 | `tmp + os.replace` (fsync before replace); JSONL append + fsync; `threading.Lock` over in-process critical sections. |
| ADR-005 — Circuit breaker state machine | FR-03 | NFR-03 | SPEC.md §3 FR-03, §4 NFR-03 | CLOSED/OPEN/HALF_OPEN; cooldown `≤ TASKQ_BREAKER_COOLDOWN + 1s`; persisted to `$TASKQ_HOME/breaker.json`. |
| ADR-006 — Kahn topological sort + depth guard | FR-06 | NFR-01 | SPEC.md §3 FR-06, §4 NFR-01 | Pure Kahn levels; cycle → `detect_cycle` chain; `len(levels) > max_depth` → exit 5. |
| ADR-007 — Plugin allowlist + `importlib` | FR-07 | NFR-02 | SPEC.md §3 FR-07, §4 NFR-02, §9 R6 | `^[A-Za-z_][A-Za-z0-9_.]*$` regex; `importlib.import_module` only; 0 hits for `eval(`, `exec(`, `__import__(`. |
| ADR-008 — Redaction at storage boundary | FR-08 | NFR-04 | SPEC.md §3 FR-08, §4 NFR-04 | Single regex set in `observability.audit.redact`; `redact=` injected port from L5 → L3; write-time, not read-time. |
| ADR-009 — click CLI surface + central exit codes | FR-05 | NFR-10, NFR-12 | SPEC.md §3 FR-05, §4 NFR-10 / NFR-12 | `cli/main.py` owns the exit-code table (0/1/2/3/4/5/6); `verify-system` smoke prints the literal marker. |
| ADR-010 — `make verify-system` gate | — | NFR-12 | SPEC.md §4 NFR-12, §8 #21 | Single Makefile target: pytest + `python -m taskq_plus status` smoke; stdout marker `verify-system: PASS`. |
| Cross-cutting — docstring coverage | FR-01..FR-08 | NFR-05 (cross-cutting) | SPEC.md §4 NFR-05 | 100% `ast-docstrings` coverage with `[FR-XX]` / `[NFR-XX]` tags on every public symbol; owned by the module-architecture contract (ADR-002). |
| Cross-cutting — mutation testing scope | FR-02, FR-03, FR-04, FR-06 | NFR-08 (cross-cutting) | SPEC.md §4 NFR-08, §8 #20 | `mutmut` over `service/` + `storage/` only (execution-time budget); score ≥ 70. |
| Cross-cutting — zero-skip verification | FR-01..FR-08 | NFR-09 (cross-cutting) | SPEC.md §4 NFR-09, §8 #1 | `pytest -q` reports 0 skipped; no `--ignore` / `-k` / `--deselect` / `collect_ignore` to game the count; TRACEABILITY_MATRIX `VERIFIED` only on real pass. |
| Cross-cutting — readability thresholds | FR-01..FR-08 | NFR-11 (cross-cutting) | SPEC.md §4 NFR-11 | MI ≥ 80; cyclomatic ≤ 10; file ≤ 400 LOC; dir ≤ 15 files; enforced by `radon` / `readability-v2`. |

> **Cross-cutting convention**: where a requirement applies to every implementing module (e.g. NFR-05 docstring tags, NFR-09 no-skip, NFR-11 readability budgets), the table above marks the row `cross-cutting` and cites the owning module-architecture decision (ADR-002). The `TRACEABILITY_MATRIX.md` 1:1 rows expand these into per-module owners.

---

## Cross-references

- SAD.md §2.1 — Layer rules and Output Port table.
- SAD.md §3.3 — Binding call-site contracts (FR-03 batch entry, FR-06 blocked-doesn't-count).
- SAD.md §5 SAB block — module names bound to each FR / NFR.
- SAD.md §6 Security block — STRIDE-lite threats aligned to the owner modules named in the ADRs above.
- SPEC.md §1 (Python 3.11, pinned deps), §2 (tech table), §6 (layering + NFR-06), §10 (high-risk modules: executor, plugins, task_store).
