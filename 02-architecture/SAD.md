# Software Architecture Document (SAD) — taskq-plus

> Phase 2 deliverable. Design authority derived from `SPEC.md` v1.0.0 (single source of
> truth) and `01-requirements/SRS.md` (Phase 1 ingestion, 8 FR / 12 NFR / 12 env).
> Every module name, directory, and layer below traces to **SPEC.md §6** verbatim —
> no invented modules, no invented directories.

| Field | Value |
|-------|-------|
| Document version | v1.0.0 |
| Date | 2026-07-30 |
| Language / runtime | Python 3.11 |
| Package | `taskq_plus` (entry `python -m taskq_plus`) |
| Layer contract | `cli > observability > service > storage > models`; `config` independent (NFR-06) |
| Upstream | SPEC.md §1–§11, SRS.md FR-01..FR-08 / NFR-01..NFR-12 |

---

## 1. Architecture Overview

`taskq-plus` is a single-process, local CLI task queue. A user submits a shell command as a
task; the tool validates it, persists it to JSON files under `$TASKQ_HOME`, and executes it
under a controlled policy envelope (timeout, retry with exponential backoff, global circuit
breaker, TTL result cache, dependency DAG), emitting a structured JSON-Lines audit trail.

Five source layers, strictly one-directional:

```
              ┌──────────────────────────────────────────┐
   L5  cli/   │ main.py (click group)  commands.py       │  user-facing I/O, exit codes
              └───────────────┬──────────────────────────┘
                              │ (imports downward only)
              ┌───────────────▼──────────────────────────┐
   L4  observability/         │ audit.py    export.py    │  audit events, redaction, export
              └───────────────┬──────────────────────────┘
              ┌───────────────▼──────────────────────────┐
   L3  service/  executor.py  breaker.py  cache.py       │  policy + orchestration
              │               dag.py      plugins.py     │
              └───────────────┬──────────────────────────┘
              ┌───────────────▼──────────────────────────┐
   L2  storage/  atomic.py  task_store.py                │  durable state, atomic writes
              │             breaker_store.py cache_store.py
              └───────────────┬──────────────────────────┘
              ┌───────────────▼──────────────────────────┐
   L1  models/   task.py     errors.py                   │  pydantic models, domain errors
              └──────────────────────────────────────────┘

   config.py  (independence module — importable by ANY layer, imports NO layer)
```

**Acyclicity.** The dependency relation is the total order `cli(5) > observability(4) >
service(3) > storage(2) > models(1)`, with `config` a sink (in-degree only, out-degree zero
w.r.t. project modules). A cycle would require an edge from layer `n` to layer `m > n`,
which `.importlinter` (NFR-06) rejects with a non-zero exit. Therefore **no circular
dependencies** exist by construction, and the property is machine-checked, not asserted
(`lint-imports`, SPEC §8 #17).

**Settings propagation (design decision).** `config.Settings` is loaded **once** at the
process entry point (`__main__.py`) and passed **as an argument** down through cli →
service → storage. Lower layers never import `config`. Rationale: (a) it keeps env reads in
one place, which is what NFR-06's "independence module" is for; (b) it makes every lower
layer deterministically testable without monkey-patching `os.environ`, which NFR-09's
zero-skip rule needs; (c) it prevents `config` from accumulating an inbound edge from every
module (a CRG cohesion sink — see §2.1).

### 1.1 System Verification Target

Gate 2 executes `make verify-system` (NFR-12 / SPEC §8 #21).

**Makefile target**: `verify-system` — runs the full pytest suite, then a CLI smoke
sequence (`submit` → `run` → `status` → `graph` → `export` → `clear`) against a temporary
`TASKQ_HOME`, then prints `verify-system: PASS` and exits 0.

---

## 2. Module Design

### 2.1 Directory Structure & CRG Community Design

Six source directories (target band 3–6), each one CRG community, each with a designated
**hub** module that siblings call from **function bodies** (not only module level):

| Dir | Files | Hub module | Hub functions (≥2 where ≥4 siblings) |
|-----|-------|-----------|--------------------------------------|
| `taskq_plus/` (root) | `__init__.py`, `__main__.py`, `config.py` | `config.py` | `load_settings`, `resolve_home` |
| `models/` | `__init__.py`, `task.py`, `errors.py` | `errors.py` | `invalid_submission`, `exit_code_for` |
| `storage/` | `__init__.py`, `atomic.py`, `task_store.py`, `breaker_store.py`, `cache_store.py` | `atomic.py` | `read_json`, `write_json_atomic`, `append_jsonl` |
| `service/` | `__init__.py`, `executor.py`, `breaker.py`, `cache.py`, `dag.py`, `plugins.py` | `executor.py` (hub-and-spoke centre; calls every sibling) | calls `breaker.*`, `cache.*`, `dag.*`, `plugins.*` from 5 function bodies |
| `observability/` | `__init__.py`, `audit.py`, `export.py` | `audit.py` | `redact_text`, `redact_record` |
| `cli/` | `__init__.py`, `main.py`, `commands.py` | `commands.py` (imported by `main.py`; 8 callbacks → 8 handlers) | 8 handler functions |

Constraints satisfied: ≤15 files per directory (max is 6), ≤400 lines per file (NFR-11),
no god-module (largest planned module `service/executor.py` ≈ 180 lines / 6 functions),
no flat dump (zero source files outside these six directories).

**Edge budget** (CRG requires internal `I ≥ 0.4286 × E` external, community ≤ 50 nodes):

| Community | E (est.) | I required | I planned | Source of planned internal edges |
|-----------|---------|-----------|-----------|----------------------------------|
| root | ~6 | 3 | 4 | `__main__.main()` and `__main__._bootstrap()` each call `config.load_settings` + `config.resolve_home` |
| `models/` | ~11 | 5 | 6 | 5 `task.py` validators call `errors.invalid_submission`; `task.py` status mapper calls `errors.exit_code_for` |
| `storage/` | ~14 | 6 | ~24 | 3 stores × ~4 function bodies each calling `atomic.read_json` / `atomic.write_json_atomic` |
| `service/` | ~25 | 11 | ~16 | `executor.run_task/run_all/_attempt/_finalize/_run_layer` call `breaker.allow/record_failure/record_success`, `cache.lookup/store`, `dag.topological_layers/detect_cycle/validate_depth`, `plugins.load_plugins/dispatch` |
| `observability/` | ~14 | 6 | ~7 | `export._as_json/_as_csv/_as_md` each call `audit.redact_record`; `export.export_tasks` calls `audit.redact_text` + `audit.emit_event` |
| `cli/` | ~14 | 6 | 8 | `main.py`'s 8 click callbacks each call one `commands.*` handler |

Node counts per community: root ≈ 6, `models/` ≈ 12, `storage/` ≈ 16, `service/` ≈ 26,
`observability/` ≈ 11, `cli/` ≈ 18 — all far below the 50-node cap.

Cross-file calls use the standalone-assignment form (`result = atomic.read_json(path,
default)`), never nested in an argument position, per CRG edge-detection limits.

**Known CRG risk (declared, not silenced).** The root community is small (3 files) and
`config.py` is the only project module every layer is *permitted* to import. The Settings
propagation decision in §1 caps `config`'s inbound edges at two call sites (`__main__.py`,
`cli/main.py`) instead of ~15, which is what keeps the root community above the cohesion
floor. Per SPEC §10 the mitigation is architectural — `crg_cohesion_healthy` **must not** be
lowered.

### 2.2 FR → Module Mapping (every FR maps to ≥1 module)

| FR | Primary module(s) | Supporting |
|----|-------------------|-----------|
| FR-01 task submission & validation | `models.task` (field rules), `storage.task_store` (id, uniqueness, atomic insert) | `service.dag` (dependency existence/cycle/depth), `cli.commands`, `observability.audit` |
| FR-02 task executor | `service.executor` | `storage.task_store`, `service.dag`, `models.errors`, `observability.audit` |
| FR-03 retry & circuit breaker | `service.breaker`, `service.executor` (retry/backoff) | `storage.breaker_store` |
| FR-04 result TTL cache | `service.cache` | `storage.cache_store`, `service.executor` |
| FR-05 CLI integration | `cli.main`, `cli.commands` | `models.errors` (exit-code mapping), `__main__` |
| FR-06 dependency DAG | `service.dag` | `service.executor` (layered concurrency), `cli.commands` (`graph`) |
| FR-07 plugin hooks | `service.plugins` | `service.executor` (dispatch points), `cli.commands` (`plugin_error` audit emission from `result.plugin_failures`), `observability.audit` (`emit_event` consumer) |
| FR-08 audit log & export | `observability.audit`, `observability.export` | `storage.atomic` (`append_jsonl`), `cli.commands` |

### 2.3 Module Specifications

#### `config.py` (independence)

| Attribute | Value |
|-----------|-------|
| Responsibility | Read the 12 `TASKQ_*` env vars into a frozen `Settings` dataclass; resolve/create `$TASKQ_HOME` |
| External interface | `Settings` (frozen dataclass, 12 fields); `load_settings(env=os.environ) -> Settings`; `resolve_home(settings) -> Path` |
| Dependencies | stdlib only (`os`, `pathlib`, `dataclasses`) — **imports no project layer** |

Logical constraints: `Settings` fields hold the **raw env string** for every
`TASKQ_*` value; numeric coercion (`int(...)` / `float(...)`) is the caller's
job, performed once at the process entry point (`__main__._bootstrap`). On
coercion failure `__main__` raises `models.errors.ValidationRejected` (exit 2)
— **never** a silent default, and **never** an import from `config` into
`models.errors`. This keeps the independence contract (NFR-06): `config`
imports no project layer, and `ValidationRejected` lives in `models/errors.py`
where every layer above may import it. `TASKQ_AUDIT_LOG` defaults to
`resolve_home(settings)/audit.jsonl`; `TASKQ_PLUGINS` is split on `,` with
empty entries dropped.

#### L1 `models/`

| Module | Responsibility | External interface | Dependencies |
|--------|---------------|--------------------|--------------|
| `task.py` | pydantic v2 validation + record shape (FR-01, FR-02) | `TaskSubmission` (`command`, `name`, `depends_on`), `TaskRecord` (all persisted fields), `INJECTION_CHARS` | `errors` |
| `errors.py` | Domain exception hierarchy + exit-code mapping (NFR-03, FR-05) | `TaskQError(exit_code)`, `ValidationRejected(2)`, `UnknownTask(2)`, `BreakerOpen(3)`, `TaskTimeout(4)`, `GraphError(5)`, `PluginLoadError(6)`, `StoreCorrupted(1)`; `invalid_submission(reason) -> NoReturn`, `exit_code_for(exc) -> int` | none (leaf) |

Logical constraints: `TaskSubmission` rejects empty/whitespace command, `len > 1000`, and
any of `; | & $ > < \`` (one validator per rule, each raising through
`errors.invalid_submission`); cross-record rules (name uniqueness, dependency existence,
cycles) are **not** here — they need store state and live in L2/L3 so that NFR-08 mutation
testing (scope `service/` + `storage/`) actually covers them.

#### L2 `storage/`

| Module | Responsibility | External interface | Dependencies |
|--------|---------------|--------------------|--------------|
| `atomic.py` | Crash-safe file primitives + the shared write lock (NFR-03) | `read_json(path, default) -> dict`, `write_json_atomic(path, payload) -> None`, `append_jsonl(path, record) -> None`, `store_lock() -> threading.Lock` | `models.errors` |
| `task_store.py` | `tasks.json` CRUD, id generation, name uniqueness (FR-01, FR-02) | `create_task(settings, submission) -> TaskRecord`, `get_task`, `update_task`, `list_tasks(settings, status=None)`, `name_in_use(settings, name)`, `clear(settings)` | `atomic`, `models.*` |
| `breaker_store.py` | `breaker.json` persistence (FR-03) | `load_state(settings) -> BreakerState`, `save_state(settings, state)` | `atomic`, `models.*` |
| `cache_store.py` | `cache.json` persistence + signature (FR-04) | `signature(command) -> str` (`sha256`), `get_entry(settings, sig)`, `put_entry(settings, sig, result)` | `atomic`, `models.*` |

Logical constraints: every write is `tmp + os.replace` (audit log is `append + fsync`);
every mutating call holds `store_lock()`; a malformed `tasks.json` raises `StoreCorrupted`
(exit 1) and is **never** silently rebuilt (SPEC §7); ids are `uuid4().hex[:8]`.

#### L3 `service/`

| Module | Responsibility | External interface | Dependencies |
|--------|---------------|--------------------|--------------|
| `executor.py` | subprocess execution, state machine, retry/backoff, concurrency (FR-02, FR-03) | `run_task(settings, task_id, *, cached=False, sleep=time.sleep) -> TaskRecord` (TaskRecord carries `plugin_failures: list[PluginFailure]`); `run_all(settings, *, sleep=time.sleep) -> RunAllResult` (`{per_task: dict[id→TaskRecord], plugin_failures: list[PluginFailure], retries: int, breaker_events: list[str]}`) | `breaker`, `cache`, `dag`, `plugins`, `storage.task_store`, `models.*` |
| `breaker.py` | Global CLOSED/OPEN/HALF_OPEN breaker (FR-03) | `allow(settings) -> None` (raises `BreakerOpen`), `record_failure(settings)`, `record_success(settings)`, `snapshot(settings) -> dict` | `storage.breaker_store`, `models.*` |
| `cache.py` | TTL replay of `done` results (FR-04) | `lookup(settings, command) -> dict \| None`, `store(settings, command, result)` | `storage.cache_store`, `models.*` |
| `dag.py` | Kahn topological layering, cycle + depth validation, rendering (FR-06) | `topological_layers(tasks) -> list[list[str]]`, `detect_cycle(tasks, new_id, new_depends_on) -> list[str] \| None`, `validate_depth(tasks, new_id, new_depends_on, max_depth) -> None`, `render(tasks, fmt) -> str` | `models.*` |
| `plugins.py` | Allowlist plugin loading + hook dispatch (FR-07) | `PLUGIN_NAME_RE = ^[A-Za-z_][A-Za-z0-9_.]*$`, `load_plugins(settings) -> list[LoadedPlugin]`, `dispatch(hook, plugins, *args) -> PluginDispatchResult` (returns disabled names AND the per-failure list — no I/O), `describe(plugins) -> list[dict]`, `PluginFailure` dataclass `{hook, plugin, error}`, `PluginDispatchResult` dataclass `{disabled, failures}` | `models.*` |

Logical constraints: `subprocess.run(shlex.split(cmd), capture_output=True, text=True,
timeout=settings.task_timeout)` — `shell=True` appears nowhere (NFR-02); `sleep` is an
injected parameter so backoff is deterministic in tests (FR-03); `blocked` tasks do **not**
increment breaker failures (FR-06); `plugins.dispatch` catches only `Exception` from plugin
code, **accumulates** failures into `PluginDispatchResult.failures` (does not write audit
events — the L3 service layer has no upward edge into L4 observability), and disables a
plugin after 3 consecutive failures within one run (FR-07) — it never swallows
`KeyboardInterrupt`/`SystemExit` (NFR-03). The structured failure list is propagated: the
caller of `executor.run_task` / `executor.run_all` reads the per-task record's
`plugin_failures` field (or the run-all envelope's `plugin_failures` list) and emits one
`plugin_error` audit event per entry via `observability.audit.emit_event`. That emission
lives in `cli.commands` (L5), the only layer that may import both `service` and
`observability`, so the cycle `service → observability` never forms. Plugin loading uses
`importlib.import_module` on a name that matched `PLUGIN_NAME_RE`, so no path, URL,
`eval`, `exec`, or `__import__` string form is reachable.

#### L4 `observability/`

| Module | Responsibility | External interface | Dependencies |
|--------|---------------|--------------------|--------------|
| `audit.py` | JSONL audit events + write-time redaction (FR-08, NFR-04) | `new_correlation_id() -> str`, `emit_event(settings, event, task_id, correlation_id, detail)`, `redact_text(text) -> str`, `redact_record(record) -> dict` | `storage.atomic`, `models.*` |
| `export.py` | json / csv / md export (FR-08) | `export_tasks(settings, fmt) -> str` | `audit`, `storage.task_store`, `models.*` |

Logical constraints: redaction regex `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+)`
replaces the **whole line** with `[REDACTED]` and runs **before** the write call, never
after a read (NFR-04); all three export formats are produced from one collector, so task
count and field set are identical by construction (FR-08); CSV goes through `csv.writer`
so commas/quotes are escaped by the stdlib, not by hand.

#### L5 `cli/` + `__main__.py`

| Module | Responsibility | External interface | Dependencies |
|--------|---------------|--------------------|--------------|
| `__main__.py` | Process entry: bootstrap settings, dispatch, translate exceptions to exit codes | `main() -> int`, `_bootstrap() -> Settings` | `config`, `cli.main` |
| `cli/main.py` | `click` group, global `--json`, output rendering | `cli` (click group), `render(payload, as_json) -> None`, `main() -> int` | `cli.commands`, `config` |
| `cli/commands.py` | 8 handlers, one per subcommand; return plain dicts | `submit_cmd`, `run_cmd`, `status_cmd`, `list_cmd`, `graph_cmd`, `plugins_cmd`, `export_cmd`, `clear_cmd` | `observability.*`, `service.*`, `storage.task_store`, `models.*` |

Logical constraints: handlers never print — `main.render` owns all stdout, so `--json` is a
single rendering path (FR-05); every handler receives the `correlation_id` created once per
invocation (FR-08); `__main__.main` is the only place that converts `TaskQError` into an
exit code via `errors.exit_code_for`.

---

## 3. Interfaces & Data Flows

### 3.1 submit (FR-01, FR-06 cycle/depth, FR-08 audit)

```
argv ──► cli.main (click)
          │  render()
          ▼
        cli.commands.submit_cmd(settings, command, name, after[])
          │ 1. models.task.TaskSubmission(...)               → ValidationRejected → exit 2
          │ 2. storage.task_store.name_in_use()              → ValidationRejected → exit 2
          │ 3. storage.task_store.get_task(dep) ∀ dep        → UnknownTask        → exit 2
          │ 4. candidate_id = uuid4().hex[:8]                # generated HERE, BEFORE cycle/depth checks
          │ 5. service.dag.detect_cycle(tasks, candidate_id, depends_on)
          │                                                    → GraphError         → exit 5 (+cycle path)
          │ 6. service.dag.validate_depth(tasks, candidate_id, depends_on, max)
          │                                                    → GraphError         → exit 5
          │ 7. storage.task_store.create_task(candidate_id, submission)
          │    ──► atomic.write_json_atomic(tasks.json)
          │ 8. observability.audit.emit_event("submit")      ──► atomic.append_jsonl(audit.jsonl)
          ▼
        {"id": "<8hex>", "status": "pending"}  ──► cli.main.render
```

### 3.2 run \<id\> (FR-02, FR-03, FR-04, FR-07)

> **Layer rule.** All `audit "..."` emissions in this diagram come from
> `cli.commands.run_cmd` (L5), the only layer that may import both
> `service` (L3) and `observability` (L4). Executor (L3) returns a result
> object that carries the per-task outcome AND a `plugin_failures` list
> (per FR-07 / §2.3); `cli.commands.run_cmd` iterates that list and emits
> one `plugin_error` event per entry via `audit.emit_event`. Same caller
> emits `run_start` / `run_end` / `retry` / `breaker_open` /
> `breaker_close` / `cache_hit` / `blocked` once executor returns the
> corresponding signal. The L3→L4 edge never forms.

```
cli.commands.run_cmd ──► service.executor.run_task(settings, id, cached, sleep)
   │
   ├─ service.breaker.allow()                              ── OPEN ──► BreakerOpen → exit 3
   ├─ if cached: service.cache.lookup(command)             ── hit ──► result.cached=True; status=done (NO subprocess)
   ├─ service.dag: deps not all done                       ──► result.blocked=True; breaker NOT incremented
   ├─ result.pre = service.plugins.dispatch("pre_run",...) # returns PluginDispatchResult; no I/O
   │     accumulator: each caught Exception → result.plugin_failures += PluginFailure(hook,plugin,error)
   ├─ _attempt(): subprocess.run(shlex.split(cmd), timeout=…)      ← shell=True forbidden
   │     exit 0 → done │ non-zero → failed │ TimeoutExpired → timeout
   │     failed/timeout and attempts ≤ retry_limit
   │        → sleep(backoff_base × 2ⁿ); result.retried += 1
   ├─ _finalize(): redact tails ──► task_store.update_task ──► atomic.write_json_atomic
   │     done  → breaker.record_success() (+ cache.store)   → result.breaker_state="HALF_OPEN→CLOSED"
   │     other → breaker.record_failure()                    → result.breaker_state="threshold reached"
   ├─ result.post = service.plugins.dispatch("post_run",...) # failures append to result.plugin_failures
   ▼
cli.commands.run_cmd (after executor returns) emits audit events:
   audit "run_start" | audit "cache_hit" | audit "blocked"
   for f in result.plugin_failures: audit "plugin_error" (f.hook, f.plugin, f.error)
   audit "retry" × len(result.retried)
   if result.breaker_state == "HALF_OPEN→CLOSED": audit "breaker_close"
   if result.breaker_state == "threshold reached": audit "breaker_open"
   audit "run_end"   (single-task timeout ⇒ exit 4)
```

### 3.3 run --all (FR-02 concurrency + FR-06 topological order)

```
service.executor.run_all  ──► returns RunAllResult({per_task: dict[id→TaskRecord], plugin_failures: list[PluginFailure], retries: int, breaker_events: list[str]})
   │ layers = service.dag.topological_layers(tasks)        # Kahn; in-degree 0 per layer
   │ service.dag.validate_depth(tasks, max_dag_depth)      # exit 5 if exceeded
   ▼
 for layer in layers:                                       # layers are SEQUENTIAL
     ThreadPoolExecutor(max_workers=settings.max_workers)
        └─ run_task(t) for t in layer                       # in-layer tasks CONCURRENT
             every store mutation holds storage.atomic.store_lock()
             each run_task's result.plugin_failures is appended to RunAllResult.plugin_failures
     tasks whose deps did not reach `done` → blocked (not executed, not counted by breaker)
   ▼
cli.commands.run_cmd (caller) iterates RunAllResult and emits one audit event per signal
   (same dispatch contract as §3.2: layer rule keeps audit emission in L5)
```

### 3.4 export / graph / plugins list (FR-05, FR-06, FR-07, FR-08)

```
export  : cli.commands.export_cmd ─► observability.export.export_tasks(fmt)
                                       ├─ storage.task_store.list_tasks()   (one collector)
                                       ├─ audit.redact_record() per task
                                       └─ _as_json | _as_csv (csv.writer) | _as_md
graph   : cli.commands.graph_cmd  ─► service.dag.render(tasks, "text"|"dot")
plugins : cli.commands.plugins_cmd ─► service.plugins.load_plugins() ─► describe()
                                       illegal name / missing module → PluginLoadError → exit 6
clear   : cli.commands.clear_cmd  ─► storage.task_store.clear() (removes the 4 data files)
```

### 3.5 Data files (SPEC §5.2)

| File | Writer | Shape |
|------|--------|-------|
| `tasks.json` | `storage.task_store` via `atomic.write_json_atomic` | `{version:1, tasks:{id → record incl. depends_on}}` |
| `breaker.json` | `storage.breaker_store` | `{version:1, state, failure_count, opened_at}` |
| `cache.json` | `storage.cache_store` | `{version:1, entries:{sha256(command) → result + cached_at}}` |
| `audit.jsonl` | `observability.audit` via `atomic.append_jsonl` | one JSON object per line: `ts, event, task_id, correlation_id, detail` |

---

## 4. NFR Handling

Dimension names are taken verbatim from SPEC §4 / §10 (`DIMENSION_TOOLS["python"]` keys).

| NFR | Dimension (SPEC) | Architectural mechanism | Owning module(s) | Machine check |
|-----|------------------|------------------------|------------------|---------------|
| NFR-01 latency | `performance` | Hot paths touch no subprocess: `submit`+`status` are two `atomic` file ops; topological sort is O(V+E) Kahn with adjacency built once | `storage.task_store`, `service.dag` | `pytest-benchmark`: p95 < 50 ms (100 iter), topo p95 < 200 ms (200 tasks) |
| NFR-02 exec/load security | `security` | `shlex.split` + `shell=True` absent; injection blacklist in `models.task`; plugin allowlist regex + named `importlib` import only; no `eval`/`exec`/`__import__` | `models.task`, `service.executor`, `service.plugins` | `grep -rn "shell=True\|eval(\|exec("` = 0 hits; `bandit -r` 0 HIGH/0 MEDIUM |
| NFR-03 error handling & atomicity | `error_handling` | One primitive module (`atomic`) owns tmp+`os.replace` and append+fsync; typed exception hierarchy in `models.errors`; each `except` re-raises, translates, or exits with a code | `storage.atomic`, `models.errors` | `ast-error-handling`; corrupted-store test → exit 1; breaker recovery ≤ cooldown+1 s |
| NFR-04 secret redaction | `security` | `audit.redact_text` / `redact_record` are called on the **write path** inside `audit` and `executor._finalize`, before any `atomic` call | `observability.audit` | `grep -c "sk-" audit.jsonl` = 0; file-content assertion |
| NFR-05 documentation | `documentation` | Every public function/class in the tree above carries a docstring citing its `[FR-XX]`/`[NFR-XX]` | all modules | `ast-docstrings` = 100 % |
| NFR-06 layering | `architecture_constraints` | The five-layer total order of §1 + `config` as independence module; enforced by `.importlinter` layers contract, not by review | project-wide | `lint-imports` exit 0 (no wildcard `ignore_imports`, no downgrade to `forbidden`) |
| NFR-07 dependency/licence | `license_compliance` | Runtime deps limited to `click` + `pydantic`, pinned with `==`; scan covers the installed tree, and the SBOM is a build artefact | `requirements.txt`, `08-config/SBOM.json` | `pip-licenses --format=json` ⊆ {MIT, BSD-2/3-Clause, Apache-2.0} |
| NFR-08 mutation testing | `mutation_testing` | Business rules deliberately placed in `service/` + `storage/` (see §2.3 `models/` constraint) so the mutation scope covers real logic, not glue | `service/*`, `storage/*` | `mutmut` score ≥ 70 |
| NFR-09 verification honesty | `test_assertion_quality` | Testability is a design property: injected `sleep`, injected `env`, `Settings` passed as an argument, handlers returning dicts — nothing needs a skip | all modules | `pytest -q` skipped = 0; `ast-assertions` zero_assert = 0 |
| NFR-10 integration coverage | `integration_coverage` | Single entry point (`python -m taskq_plus` / `CliRunner`) and no hidden side doors, so CLI-driven tests can reach every layer | `cli/*`, `__main__` | `pytest tests/integration --cov` ≥ 80 % |
| NFR-11 readability | `readability` | ≤6 files/dir, ≤400 lines/file, per-function complexity ≤10 (the state machine is split into `_attempt` / `_finalize` rather than one branchy function) | all modules | `readability-v2` MI ≥ 80; complexity ≤ 10 |
| NFR-12 system verification | `execute_verification_target` | `verify-system` target chains suite + CLI smoke over a temp `TASKQ_HOME` | `Makefile` | `make verify-system` exit 0 and prints `verify-system: PASS` |

**Cost.** No network service, no database, no daemon: runtime cost is the user's own
process. The only recurring cost driver is `mutmut`, which is why NFR-08 scopes mutation to
`service/` + `storage/` (SPEC §4 NFR-08 time-budget rationale).

---

## 5. SAB Block (machine-readable — BINDING CONTRACT)

> **CONTRACT**: field names, the `sab:` root key, and `phase` as an int must match
> `core/quality_gate/sab_parser.py:render_canonical_sab_template()`. Values below are the
> project's real values; `nfr_dimension_mapping`, `advisory_only`, and
> `gate_score_overrides` are intentionally left empty for the parser to derive.
> Validation and `.methodology/SAB.json` generation happen in the SAB Generation step.

<!-- SAB:START -->
```yaml
sab:
  version: "1.0"
  created_at: "2026-07-30"
  phase: 2
  project: "taskq-plus"

  layers:
    - name: cli
      modules:
        - name: "taskq_plus.cli.main"
        - name: "taskq_plus.cli.commands"
        - name: "taskq_plus.__main__"
      allowed_dependencies: ["observability", "service", "storage", "models", "config"]
    - name: observability
      modules:
        - name: "taskq_plus.observability.audit"
        - name: "taskq_plus.observability.export"
      allowed_dependencies: ["service", "storage", "models"]
    - name: service
      modules:
        - name: "taskq_plus.service.executor"
        - name: "taskq_plus.service.breaker"
        - name: "taskq_plus.service.cache"
        - name: "taskq_plus.service.dag"
        - name: "taskq_plus.service.plugins"
      allowed_dependencies: ["storage", "models"]
    - name: storage
      modules:
        - name: "taskq_plus.storage.atomic"
        - name: "taskq_plus.storage.task_store"
        - name: "taskq_plus.storage.breaker_store"
        - name: "taskq_plus.storage.cache_store"
      allowed_dependencies: ["models"]
    - name: models
      modules:
        - name: "taskq_plus.models.task"
        - name: "taskq_plus.models.errors"
      allowed_dependencies: []
    - name: config
      modules:
        - name: "taskq_plus.config"
      allowed_dependencies: []

  allowed_dependencies:
    - from: cli
      to: observability
    - from: cli
      to: service
    - from: cli
      to: storage
    - from: cli
      to: models
    - from: cli
      to: config
    - from: observability
      to: service
    - from: observability
      to: storage
    - from: observability
      to: models
    - from: service
      to: storage
    - from: service
      to: models
    - from: storage
      to: models

  quality_targets:
    max_complexity: 10
    min_coverage: 100
    max_coupling: 0.3

  nfr_dimension_mapping: {}

  nfr_traceability:
    NFR-01:
      type: performance
      target: "submit+status p95 < 50ms; 200-task topological sort p95 < 200ms"
      module: taskq_plus.service.dag
    NFR-02:
      type: security
      target: "0 shell=True/eval(/exec( hits; bandit 0 HIGH 0 MEDIUM"
      module: taskq_plus.service.plugins
    NFR-03:
      type: reliability
      target: "atomic write on all 4 data files; no bare except; breaker recovery <= cooldown + 1s"
      module: taskq_plus.storage.atomic
    NFR-04:
      type: security
      target: "0 plaintext secrets on disk; redaction before write"
      module: taskq_plus.observability.audit
    NFR-05:
      type: maintainability
      target: "100% docstring coverage with [FR-XX]/[NFR-XX] citation"
      module: taskq_plus.cli.commands
    NFR-06:
      type: maintainability
      target: "lint-imports exit 0 on layers contract cli > observability > service > storage > models"
      module: taskq_plus.config
    NFR-07:
      type: maintainability
      target: "all runtime deps pinned with == and licensed MIT/BSD-2/BSD-3/Apache-2.0; SBOM emitted"
      module: taskq_plus.config
    NFR-08:
      type: testability
      target: "mutation score >= 70 over service/ and storage/"
      module: taskq_plus.service.executor
    NFR-09:
      type: testability
      target: "0 skipped tests; 0 zero-assertion test functions"
      module: taskq_plus.service.breaker
    NFR-10:
      type: testability
      target: "integration line coverage >= 80 driven through the CLI entry point"
      module: taskq_plus.cli.main
    NFR-11:
      type: maintainability
      target: "project MI >= 80; per-function complexity <= 10; <= 400 lines per file"
      module: taskq_plus.cli.commands
    NFR-12:
      type: testability
      target: "make verify-system exit 0 and prints verify-system: PASS"
      module: taskq_plus.__main__

  advisory_only: []

  gate_score_overrides: {}

  fr_module_traceability:
    FR-01: ["taskq_plus.models.task", "taskq_plus.storage.task_store", "taskq_plus.cli.commands"]
    FR-02: ["taskq_plus.service.executor", "taskq_plus.storage.task_store"]
    FR-03: ["taskq_plus.service.breaker", "taskq_plus.storage.breaker_store", "taskq_plus.service.executor"]
    FR-04: ["taskq_plus.service.cache", "taskq_plus.storage.cache_store"]
    FR-05: ["taskq_plus.cli.main", "taskq_plus.cli.commands", "taskq_plus.__main__"]
    FR-06: ["taskq_plus.service.dag"]
    FR-07: ["taskq_plus.service.plugins"]
    FR-08: ["taskq_plus.observability.audit", "taskq_plus.observability.export"]

  architecture_constraints:
    - "no_circular_dependencies"
    - "layers_cli_observability_service_storage_models"
    - "config_is_independence_module"
    - "no_shell_true"
    - "no_eval_exec_dynamic_import"
    - "max_15_files_per_directory"

  high_risk_modules:
    - "taskq_plus.service.executor"
    - "taskq_plus.service.plugins"
    - "taskq_plus.storage.task_store"
```
<!-- SAB:END -->

---

## 6. Security Design (STRIDE-lite — machine-readable, BINDING CONTRACT)

> **CONTRACT**: parsed by `core/quality_gate/security_design.py:extract_security_block()`.
> Pasted from `render_canonical_security_template()` with EXAMPLE values replaced by real
> project values. Every `owner_module` is declared in the §5 SAB block; every `nfr` exists
> in SRS.md; every `verified_by` names a single test already enumerated in
> `TEST_INVENTORY.yaml`. `applicability` is `full`: this tool executes arbitrary shell
> commands and dynamically imports plugin modules — it has a real attack surface.

<!-- SEC:START -->
```yaml
security_design:
  version: "1.0"
  applicability: full
  justification: ""
  trust_boundaries:
    - id: TB-01
      name: "CLI argument input"
      description: "user-supplied command string, --name and --after values crossing from argv into validation"
    - id: TB-02
      name: "subprocess execution boundary"
      description: "task command handed to an OS child process, and its stdout/stderr coming back"
    - id: TB-03
      name: "plugin module load boundary"
      description: "TASKQ_PLUGINS environment value resolved into imported third-party Python code"
    - id: TB-04
      name: "local state files under $TASKQ_HOME"
      description: "tasks.json / breaker.json / cache.json / audit.jsonl written concurrently by worker threads"
  threats:
    - id: T-01
      boundary: TB-01
      category: tampering
      description: "shell metacharacters in the submitted command chain an extra command onto the intended one"
      mitigation: "pydantic validator rejects any of ; | & $ > < ` before persistence, exit 2, nothing written"
      owner_module: "taskq_plus.models.task"
      nfr: NFR-02
      verified_by: "test_fr01_d"
    - id: T-02
      boundary: TB-02
      category: elevation_of_privilege
      description: "command string interpreted by a shell would grant shell-language power to task input"
      mitigation: "subprocess.run(shlex.split(cmd)) only; shell=True absent from the codebase, grep-enforced"
      owner_module: "taskq_plus.service.executor"
      nfr: NFR-02
      verified_by: "test_nfr02_a"
    - id: T-03
      boundary: TB-02
      category: denial_of_service
      description: "a task that never exits pins a worker thread and blocks the queue indefinitely"
      mitigation: "mandatory TASKQ_TASK_TIMEOUT on every subprocess.run; timeout state plus exit 4"
      owner_module: "taskq_plus.service.executor"
      nfr: NFR-03
      verified_by: "test_fr02_a"
    - id: T-04
      boundary: TB-02
      category: information_disclosure
      description: "API keys or bearer tokens present in child stdout/stderr get persisted in plaintext"
      mitigation: "line-level redaction of sk-/token=/Bearer patterns applied before the write call, not after read"
      owner_module: "taskq_plus.observability.audit"
      nfr: NFR-04
      verified_by: "test_nfr04_b"
    - id: T-05
      boundary: TB-03
      category: elevation_of_privilege
      description: "a file path or URL in TASKQ_PLUGINS would turn plugin loading into arbitrary code execution"
      mitigation: "allowlist of module names matching ^[A-Za-z_][A-Za-z0-9_.]*$ imported via importlib.import_module; no eval/exec/__import__, no path or URL form; exit 6 on rejection"
      owner_module: "taskq_plus.service.plugins"
      nfr: NFR-02
      verified_by: "test_fr07_a"
    - id: T-06
      boundary: TB-03
      category: denial_of_service
      description: "a plugin raising in pre_run/post_run aborts the queue run for every task"
      mitigation: "hook dispatch isolates plugin exceptions, returns a structured PluginFailure list, continues, and disables a plugin after 3 consecutive failures in one run; cli.commands emits the plugin_error audit event from the returned list"
      owner_module: "taskq_plus.service.plugins"
      nfr: NFR-03
      verified_by: "test_fr07_b"
    - id: T-07
      boundary: TB-04
      category: tampering
      description: "concurrent or interrupted writes leave tasks.json truncated so recorded state is lost or altered"
      mitigation: "single atomic primitive (tmp file + os.replace, append + fsync for JSONL) guarded by one shared threading.Lock"
      owner_module: "taskq_plus.storage.atomic"
      nfr: NFR-03
      verified_by: "test_nfr03_a"
    - id: T-08
      boundary: TB-04
      category: repudiation
      description: "executed commands cannot be attributed to the invocation that triggered them"
      mitigation: "append-only JSONL audit trail where one correlation_id is generated per CLI invocation and stamped on every event it causes"
      owner_module: "taskq_plus.observability.audit"
      nfr: NFR-04
      verified_by: "test_fr08_b"
    - id: T-09
      boundary: TB-04
      category: tampering
      description: "a corrupted store silently rebuilt on startup would erase evidence of the corruption"
      mitigation: "malformed tasks.json raises StoreCorrupted, exits 1 with 'store corrupted' on stderr, and never rebuilds silently"
      owner_module: "taskq_plus.storage.task_store"
      nfr: NFR-03
      verified_by: "test_nfr03_d"
```
<!-- SEC:END -->

---

## 7. Error Handling & Exit Codes

| Condition | Raised by | Exception | Exit |
|-----------|-----------|-----------|------|
| empty / >1000 chars / injection char | `models.task` | `ValidationRejected` | 2 |
| duplicate `--name`, unknown task id, unknown `--after` id | `storage.task_store` | `ValidationRejected` / `UnknownTask` | 2 |
| breaker OPEN | `service.breaker` | `BreakerOpen` (`breaker open`) | 3 |
| subprocess timeout (single-task mode) | `service.executor` | `TaskTimeout` | 4 |
| dependency cycle / depth > `TASKQ_MAX_DAG_DEPTH` | `service.dag` | `GraphError` (+ cycle path) | 5 |
| illegal plugin name / missing module | `service.plugins` | `PluginLoadError` | 6 |
| plugin raises at runtime | `service.plugins` | swallowed **by design** → `plugin_error` event | 0 (run continues) |
| corrupted `tasks.json` | `storage.task_store` | `StoreCorrupted` | 1 |
| anything else | — | propagates to `__main__` | 1 |

Escalation policy (three legal `except` outcomes per NFR-03): re-raise, translate into a
`TaskQError` subclass, or log-and-exit with a code. No bare `except:`, no
`except Exception: pass`, and `KeyboardInterrupt`/`SystemExit` are never caught. The single
deliberate isolation point is `service.plugins.dispatch`, which is required by FR-07.

---

## 8. Technology Choices

| Technology | Rationale |
|-----------|-----------|
| `click` (pinned, BSD-3-Clause) | SPEC §2 mandates grouped subcommands; `CliRunner` is what NFR-10 integration tests drive |
| `pydantic` v2 (pinned, MIT) | SPEC §2 mandates model validation for FR-01; declarative validators keep per-function complexity ≤10 (NFR-11) |
| stdlib `subprocess` + `shlex` | NFR-02 requires argv-list execution with no shell |
| `concurrent.futures.ThreadPoolExecutor` | tasks are subprocess-bound (I/O), so threads suffice and keep state in one process for the shared lock |
| JSON / JSONL files + `os.replace` | SPEC §5.2; `os.replace` is atomic on POSIX and Windows, no dependency needed |
| `import-linter` (dev) | NFR-06 requires the layer contract be executable, not documentary |
| `mutmut`, `pip-licenses`, `pytest-benchmark`, `bandit` (dev) | the four dimensions SPEC §0 exists to light up (NFR-07/08/01/02) |

**Known limitations** (SPEC §9 R10): audit log rotation is out of scope this round; the
breaker is per-`$TASKQ_HOME`, so two homes have independent breakers.

---

## 9. Traceability Summary

- FR coverage: FR-01..FR-08 → §2.2 table, all 8 mapped to ≥1 module; mirrored in
  `sab.fr_module_traceability` (§5).
- NFR coverage: NFR-01..NFR-12 → §4 table with dimension + machine check; mirrored in
  `sab.nfr_traceability` (§5).
- Security: TB-01..TB-04 / T-01..T-09 → §6, each `verified_by` an existing
  `TEST_INVENTORY.yaml` test name.
- Structure: §2.1 matches SPEC §6 file-for-file; no module was added or removed.

*SAD v1.0.0 | 2026-07-30 | source: SPEC.md v1.0.0 §1–§11, SRS.md FR-01..08 / NFR-01..12*
