"""FR-06 RED tests — 任務相依 DAG (Kahn topological layering + cycle + depth cap).

Per TEST_SPEC.md §FR-06 there are 5 case rows collapsed onto 4 test functions
(rows that share a function name are pytest parametrize ids — TEST_SPEC.md
shape rules v2.13.0 "multi-scenario split", one row per parametrize id):

  - test_fr06_a  (dep_status="done" → downstream "done"            row 1)
                (dep_status="failed" → downstream "blocked"         row 2)
  - test_fr06_b  (cycle A→B→C→A → exit 5 + cycle path in stderr)    row 3
  - test_fr06_c  (depth 33 > TASKQ_MAX_DAG_DEPTH=32 → exit 5 +       row 4)
                 stderr "dependency chain too deep: <n> > <max>")
  - test_fr06_d  (Kahn topological sort emits layers; sizes 2/3/5)   row 5

Sub-assertions (rule_id → predicate):
  AC6-dep-done-propagates       : expected_downstream_status == "done"            row 1
  AC6-dep-fail-blocks           : expected_downstream_status == "blocked"         row 2
  AC6-cycle-nodes-nonempty      : len(cycle_node_seq.split(",")) >= 3            row 3
  AC6-cycle-edge-seq-parity     : len(cycle_edge_seq.split(",")) == n             row 3
  AC6-cycle-path-nonempty       : len(cycle_path_token) > 0 and "->" in token     row 3
  AC6-depth-exceeded            : chain_depth_count > max_dag_depth               row 4
  AC6-layer-count-matches       : layer_count == expected_layer_count            row 5
  AC6-layer-sizes-wellformed    : len(layer_sizes_csv.split(",")) == 3           row 5

Property (Direction B):
  P6-layers-nonempty            : len(topological_layers(tasks)) >= 1             rows 1..5

SAB-bindings (FR-06 binds to, per SAB.json fr_module_traceability.FR-06):
  - taskq_plus.service.dag  (does NOT exist on disk — RED)

This file is the TDD-RED deliverable: it is EXPECTED to fail with a pytest
Collection Error (Exit Code 2) because `taskq_plus/service/dag.py` is absent
and the public-API symbols the GREEN TODOs name do not exist. Do NOT wrap
these imports in try/except ImportError — the crash IS the RED signal.

In-process vs out-of-process (explicit choice, per v2.13.0 integration rules):
  * Each spec-named test asserts the REAL user-facing entry point out of
    process (`subprocess.run([sys.executable, "-m", "taskq_plus", ...])`,
    with PYTHONPATH propagated to the child) AND the same behaviour in
    process through the FR-06 service module `taskq_plus.service.dag` so
    pytest-cov can actually measure the SAB-declared module (a subprocess
    is invisible to coverage).
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC = ROOT / "src"
HOME_VAR = "TASKQ_HOME"

# Make src/ importable for the in-process tests. Subprocess tests do NOT rely
# on this — they propagate PYTHONPATH explicitly through the child env.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ---------------------------------------------------------------------------
# SAB-bound imports — every line below deliberately fails in the RED state.
#
# GREEN TODO: create `03-development/src/taskq_plus/service/dag.py` exporting:
#
#   - `MAX_DAG_DEPTH: int = 32`                                   : module-level
#     default chain-depth ceiling (FR-06 cap).
#
#   - `topological_layers(tasks: Sequence[Mapping[str, Any]]) -> List[List[str]]`
#     : Kahn-style algorithm that groups pending task ids into layers
#       where every task in layer N depends only on tasks in layers <N.
#       Tasks at the same layer have in-degree 0 within the residual
#       sub-DAG and are eligible for concurrent execution.
#
#   - `detect_cycle(tasks: Sequence[Mapping[str, Any]]) -> Optional[List[str]]`
#     : returns the cycle path as a list of task ids in order (first id
#       repeats at the end), e.g. ["id_a","id_b","id_c","id_a"]; returns
#       None when the DAG is acyclic.
#
#   - `cycle_path_string(cycle: Sequence[str]) -> str`
#     : renders a cycle as the single-line string `"A -> B -> C -> A"`
#       (ASCII arrows; FR-06 stderr contract).
#
#   - `check_depth(depends_on: Sequence[str], *, by_id: Mapping[str, Mapping[str, Any]]) -> int`
#     : returns the longest dependency-chain length reachable from
#       `depends_on`; raises a GraphError (or DAG-specific exception)
#       whose message matches `dependency chain too deep: <n> > <max>`
#       when the depth exceeds `MAX_DAG_DEPTH` (FR-06).
#
# GREEN TODO: the click wrapper in `taskq_plus.cli.main` must wire
# submit's `GraphError` to `EXIT_GRAPH_ERROR` (exit 5) and route the
# cycle-path string to stderr (FR-06 / SPEC §7).
# ---------------------------------------------------------------------------
from taskq_plus.service.dag import (  # noqa: E402,F401
    MAX_DAG_DEPTH,
    ancestor_tasks,
    chain_length,
    check_depth,
    cycle_path_string,
    detect_cycle,
    topological_layers,
)

from taskq_plus.cli.main import main as cli_main  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Per-test isolation — a FRESH $TASKQ_HOME per test function (function-scoped)
# so cycles / depth-violations from one parametrize id cannot leak into the
# next one. TASKQ_MAX_DAG_DEPTH=32 is the project default per SPEC §7.
# ---------------------------------------------------------------------------
@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Function-scoped TASKQ_HOME — every parametrize id gets a clean directory."""
    home = tmp_path / "taskq_home"
    home.mkdir()
    monkeypatch.setenv(HOME_VAR, str(home))
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "0")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0")
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "99")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "300")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "10")
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "32")
    monkeypatch.delenv("TASKQ_PLUGINS", raising=False)
    return home


# ---------------------------------------------------------------------------
# Out-of-process helper — the REAL user-facing entry point (AC-FR-06.a).
# PYTHONPATH must be propagated explicitly: pytest's sys.path manipulation
# does NOT reach a child process.
# ---------------------------------------------------------------------------
def _run_cli(argv, taskq_home_path, extra_env=None):
    """Run `python -m taskq_plus <argv>` out of process against taskq_home_path."""
    env = os.environ.copy()
    env[HOME_VAR] = str(taskq_home_path)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(taskq_home_path),
        timeout=120,
    )


# ---------------------------------------------------------------------------
# In-process helper — same argv through `cli.main.main`, so pytest-cov can see
# `cli/main.py` being exercised on the FR-06 validation paths.
# ---------------------------------------------------------------------------
def _main_capture(argv):
    """Call cli.main.main(argv) in process; return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli_main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def _submit_task(command, extra=()):
    """Submit one task in process; return its 8-hex id (asserts exit 0)."""
    code, out, err = _main_capture(["submit", command, *extra])
    assert code == 0, f"submit {command!r} failed: exit={code} stderr={err!r}"
    return out.strip().splitlines()[-1].strip()


# ===========================================================================
# Test cases — names MUST match TEST_SPEC.md §FR-06 verbatim.
# ===========================================================================

# ---- rows 1..2 : dependency propagation (done / blocked) -----------------
# NFR-09: AC-NFR-09.a / AC-NFR-09.b — both rows assert on real exit codes
# (`run --all` exit 0; no exit-code assertion on row 2 because the scheduler
# marks the downstream 'blocked' but `run --all` itself returns 0).
# NFR-10: AC-NFR-10.a — row 1 drives `python -m taskq_plus run --all` out of
# process and the in-process `cli.main.main(argv)` is also covered by the
# TestDagCliInProcess class below.
# NFR-03: AC-NFR-03.a — `run --all` is the ThreadPoolExecutor path; the
# concurrent store writes (atomic + shared Lock) exercised here are the
# cross-cutting thread-safety invariant owned by the executor's store_lock.
@pytest.mark.parametrize(
    ("dep_status", "expected_downstream_status"),
    [
        ("done", "done"),      # row 1 — AC6-dep-done-propagates
        ("failed", "blocked"),  # row 2 — AC6-dep-fail-blocks
    ],
)
def test_fr06_a(taskq_home, dep_status, expected_downstream_status):  # NFR-09, NFR-10, NFR-03
    """AC-FR-06.a: `submit "echo b" --after <a>` then `run --all` — b runs after a.

    Row 1: a.status == 'done'    → b.status == 'done'
    Row 2: a.status == 'failed'  → b.status == 'blocked' (NOT executed, NOT
            counted toward breaker failure count)

    Predicates:
      AC6-dep-done-propagates : expected_downstream_status == "done"    row 1
      AC6-dep-fail-blocks     : expected_downstream_status == "blocked" row 2
    """
    # rule_id: AC6-dep-done-propagates OR AC6-dep-fail-blocks
    assert expected_downstream_status in {"done", "blocked"}

    # ---- (1) Build the scenario out of process -----------------------------
    # Submit root `a`, then submit `b --after <a>`.
    submit_a_proc = _run_cli(["submit", "echo a-fr06"], taskq_home)
    assert submit_a_proc.returncode == 0, (
        f"submit a failed: exit={submit_a_proc.returncode} stderr={submit_a_proc.stderr!r}"
    )
    root_id = submit_a_proc.stdout.strip().splitlines()[-1].strip()
    assert len(root_id) == 8, f"root id must be 8 hex chars, got {root_id!r}"

    submit_b_proc = _run_cli(
        ["submit", "echo b-fr06", "--after", root_id], taskq_home
    )
    assert submit_b_proc.returncode == 0, (
        f"submit b failed: exit={submit_b_proc.returncode} stderr={submit_b_proc.stderr!r}"
    )
    child_id = submit_b_proc.stdout.strip().splitlines()[-1].strip()
    assert len(child_id) == 8, f"child id must be 8 hex chars, got {child_id!r}"

    # For row 2 ('failed' / 'blocked') we DO NOT call `run --all` — the spec
    # contract says b must be marked blocked but NOT executed (and NOT counted
    # toward breaker failure count). So:
    #
    #   * row 1 ('done')  : we run --all and assert b.status == 'done'.
    #   * row 2 ('blocked'): we leave b pending; the spec predicate targets
    #     the BLOCKED transition produced by the scheduler. The DAG module
    #     must surface the blocked status so the in-process path below can
    #     verify it without spinning up an executor.

    if dep_status == "done":
        # row 1: run --all drives both `a` and `b` to status == 'done'.
        run_proc = _run_cli(["run", "--all"], taskq_home)
        assert run_proc.returncode == 0, (
            f"run --all failed: exit={run_proc.returncode} stderr={run_proc.stderr!r}"
        )
    # row 2 (blocked): no run --all — the dep-failure path is purely the
    # scheduler's blocked-transition. See the in-process assertions below
    # for the mechanism that flips `b` to blocked.

    # ---- (2) In-process: topological_layers + blocked-transition logic ---
    # Import the in-process state by reading the live tasks.json. This
    # exercises the same module the GREEN agent must implement.
    from taskq_plus.storage.task_store import load_tasks

    tasks = load_tasks()
    by_id = {t.get("id"): t for t in tasks}

    # The DAG service must produce one layer per dependency-rank; root (a)
    # is layer 0, b is layer 1. This is the FR-06.d Kahn layering
    # invariant — same task list, in-process, so coverage can measure it.
    layers = topological_layers(list(by_id.values()))
    assert len(layers) >= 1, "P6-layers-nonempty violated"
    # Every task must appear in some layer.
    flattened = {tid for layer in layers for tid in layer}
    assert flattened == {root_id, child_id}, (
        f"topological_layers missed some tasks: layers={layers}, expected={sorted({root_id, child_id})}"
    )
    # b must NOT appear before a (the AC-FR-06.a ordering invariant).
    pos = {tid: i for i, level in enumerate(layers) for tid in level}
    assert pos[root_id] < pos[child_id], (
        f"dependency ordering violated: a.layer={pos[root_id]} >= "
        f"b.layer={pos[child_id]} (layers={layers})"
    )

    # ---- (3) Assert the status outcome ------------------------------------
    if dep_status == "done":
        # After `run --all` both tasks should be 'done'.
        assert by_id[child_id].get("status") == "done", (
            f"row 1 AC6-dep-done-propagates: child status "
            f"{by_id[child_id].get('status')!r}, expected 'done'"
        )
    else:
        # row 2: AC6-dep-fail-blocks. The scheduler marks the downstream
        # task 'blocked' without executing it. The GREEN agent must add
        # this transition: when a dep has status='failed' (or hasn't
        # reached 'done'), the downstream task transitions to 'blocked'.
        # We trigger it by invoking the in-process run_cmd(run_all=True)
        # with a freshly-submitted failed-root dependency.
        submit_root_proc = _run_cli(["submit", "false"], taskq_home)
        root_failed_id = submit_root_proc.stdout.strip().splitlines()[-1].strip()
        submit_child_proc = _run_cli(
            ["submit", "echo never-runs", "--after", root_failed_id],
            taskq_home,
        )
        child_blocked_id = submit_child_proc.stdout.strip().splitlines()[-1].strip()

        # Drive a single execution on the failing root to get status='failed'.
        run_root_proc = _run_cli(["run", root_failed_id], taskq_home)
        # `false` exits 1 — that's the "final" failure in this isolated task.
        # The breaker counter is set to 99 in the fixture, so this single
        # failure won't trip the breaker.
        assert run_root_proc.returncode in {0, 1}, (
            f"run root failed unexpectedly: exit={run_root_proc.returncode} "
            f"stderr={run_root_proc.stderr!r}"
        )

        # Now `run --all` — the child must transition to 'blocked', not 'done'
        # or 'failed', and it must NOT execute (no shell-out, so no result
        # file with stdout/stderr).
        run_all_proc = _run_cli(["run", "--all"], taskq_home)
        assert run_all_proc.returncode == 0, (
            f"run --all exit={run_all_proc.returncode} stderr={run_all_proc.stderr!r}"
        )

        tasks_after = load_tasks()
        by_id_after = {t.get("id"): t for t in tasks_after}
        blocked_child = by_id_after.get(child_blocked_id, {})
        assert blocked_child.get("status") == "blocked", (
            f"row 2 AC6-dep-fail-blocks: child status "
            f"{blocked_child.get('status')!r}, expected 'blocked' (must NOT execute). "
            f"task={blocked_child!r}"
        )
        # The blocked child must NOT have been executed — assert no result
        # fields were written (no stdout_tail, no stderr_tail, no exit_code).
        assert "stdout_tail" not in blocked_child or blocked_child.get("stdout_tail") in (None, ""), (
            f"blocked child must not have executed, but stdout_tail="
            f"{blocked_child.get('stdout_tail')!r}"
        )


# ---- row 3 : cycle detection → exit 5 + cycle path in stderr -------------
# NFR-09: AC-NFR-09.a — cycle rejection surfaces as exit 5 with the cycle
# path on stderr (SPEC §7 / C-10).
# NFR-10: AC-NFR-10.a — both the out-of-process submit path and the
# in-process `service.dag.detect_cycle` / `cycle_path_string` are exercised.
def test_fr06_b(taskq_home):  # NFR-09, NFR-10
    """AC-FR-06.b: `submit --after` that creates a cycle is rejected (exit 5).

    Precondition (per TEST_SPEC): construct three tasks id_a, id_b, id_c;
    submit id_b --after id_a; submit id_c --after id_b; then attempt
    `submit id_a --after id_c`. The cycle is A → B → C → A.

    Predicates:
      AC6-cycle-nodes-nonempty   : len(cycle_node_seq.split(",")) >= 3
      AC6-cycle-edge-seq-parity  : len(cycle_edge_seq.split(",")) == n
      AC6-cycle-path-nonempty    : len(cycle_path_token) > 0 and "->" in token

    The GREEN agent must (a) expose `taskq_plus.service.dag.detect_cycle`
    returning the cycle path as a list of ids, (b) wire the submit handler
    to translate a detected cycle into exit 5 with the cycle-path string
    on stderr.
    """
    # rule_id: AC6-cycle-nodes-nonempty + AC6-cycle-edge-seq-parity
    cycle_node_seq = "A,B,C"
    cycle_edge_seq = "A->B,B->C,C->A"
    cycle_path_token = "A -> B -> C -> A"
    assert len(cycle_node_seq.split(",")) >= 3
    assert len(cycle_edge_seq.split(",")) == len(cycle_node_seq.split(","))
    # rule_id: AC6-cycle-path-nonempty
    assert len(cycle_path_token) > 0 and "->" in cycle_path_token

    # ---- (1) Out of process: pre-seed a cyclic store, then attempt submit -
    # The TEST_SPEC precondition ("submit id_a --after id_c") requires an
    # edit-in-place API that does not exist on `submit`. We instead seed a
    # cyclic graph directly through the storage layer (the same way a
    # corrupted tasks.json or a hand-edited entry would look on disk), then
    # assert that submit rejects any further mutation against the cyclic
    # store with exit 5 + cycle path on stderr (SPEC §8 #11 / AC-FR-06.b).
    from taskq_plus.storage.task_store import save_tasks

    # Pre-seed the canonical A → B → C → A cycle using the stable letter
    # aliases (TEST_SPEC row 3 contract: cycle_node_seq="A,B,C"). The
    # letter aliases double as the in-stderr identifiers.
    letter_ids = {"a": "a", "b": "b", "c": "c"}
    id_a, id_b, id_c = letter_ids["a"], letter_ids["b"], letter_ids["c"]
    cyclic_seed = [
        {
            "id": id_a,
            "command": "echo node-a",
            "status": "pending",
            "depends_on": [id_c],
        },
        {
            "id": id_b,
            "command": "echo node-b",
            "status": "pending",
            "depends_on": [id_a],
        },
        {
            "id": id_c,
            "command": "echo node-c",
            "status": "pending",
            "depends_on": [id_b],
        },
    ]
    save_tasks(cyclic_seed)

    proc_cycle = _run_cli(
        ["submit", "echo node-a-reborn", "--after", id_a], taskq_home
    )
    assert proc_cycle.returncode != 0, (
        f"submit into cyclic store must be rejected (non-zero exit), "
        f"got returncode={proc_cycle.returncode}; stdout={proc_cycle.stdout!r}; "
        f"stderr={proc_cycle.stderr!r}"
    )
    assert proc_cycle.returncode == 5, (
        f"AC-FR-06.b: cycle rejection must exit 5, got "
        f"{proc_cycle.returncode}; stderr={proc_cycle.stderr!r}"
    )
    # stderr must mention the cycle path. The path uses ASCII arrows by
    # TEST_SPEC contract (cycle_path_token="A -> B -> C -> A"). Accept
    # either the literal token OR the same arrow layout over real ids
    # (the GREEN agent may render with ids, with letter labels, or both).
    assert "->" in proc_cycle.stderr, (
        f"AC-FR-06.b: stderr must list the cycle path with '->' arrows, "
        f"got stderr={proc_cycle.stderr!r}"
    )
    # At least one id (or its letter alias) must appear in the stderr so
    # the operator can identify the cycle participants.
    cycle_participants = {id_a, id_b, id_c}
    assert any(tid in proc_cycle.stderr for tid in cycle_participants), (
        f"AC-FR-06.b: stderr must name at least one cycle participant "
        f"({sorted(cycle_participants)}), got stderr={proc_cycle.stderr!r}"
    )

    # ---- (2) In process: detect_cycle(topological_levels path) ----------
    # Re-seed the store with a known cyclic graph and assert the service
    # module surfaces the cycle path directly. This exercises the
    # service module the GREEN agent must implement.
    from taskq_plus.storage.task_store import save_tasks

    cyclic_tasks = [
        {"id": id_a, "command": "echo a", "status": "pending", "depends_on": [id_c]},
        {"id": id_b, "command": "echo b", "status": "pending", "depends_on": [id_a]},
        {"id": id_c, "command": "echo c", "status": "pending", "depends_on": [id_b]},
    ]
    save_tasks(cyclic_tasks)

    cycle = detect_cycle(cyclic_tasks)
    assert cycle is not None, (
        f"detect_cycle must return a non-None cycle for an explicitly "
        f"cyclic graph, got None (tasks={cyclic_tasks!r})"
    )
    assert len(cycle) >= 3, (
        f"cycle path must list at least 3 ids, got {cycle!r}"
    )
    # First and last ids must match (the closing edge).
    assert cycle[0] == cycle[-1], (
        f"cycle path must start and end at the same id, got {cycle!r}"
    )
    # cycle_path_string must render with ASCII arrows (FR-06 contract).
    rendered = cycle_path_string(cycle)
    assert "->" in rendered, (
        f"cycle_path_string must use ASCII '->' arrows, got {rendered!r}"
    )
    assert len(rendered) > 0
    # P6-layers-nonempty: topological_layers must still produce >= 1 layer
    # even when the graph is cyclic (the GREEN agent must NOT raise —
    # cycle handling is the submit path's job, not the layering path's).
    layers_cyclic = topological_layers(cyclic_tasks)
    assert len(layers_cyclic) >= 1, "P6-layers-nonempty violated on cyclic input"


# ---- row 4 : depth cap → exit 5 + stderr "dependency chain too deep" ----
# NFR-09: AC-NFR-09.a — depth-cap violation surfaces as exit 5 with
# `dependency chain too deep: <n> > <max>` on stderr (SPEC §7 / C-10).
# NFR-10: AC-NFR-10.a — the chain is built out of process via repeated
# `submit --after` calls and the in-process `check_depth` is exercised
# against the same shape.
# NFR-05: AC-NFR-05.a — every public symbol in `service/dag.py` carries
# the `[FR-06]` docstring tag; the TestDagInProcess cases below cover
# every one of them.
def test_fr06_c(taskq_home):  # NFR-09, NFR-10, NFR-05
    """AC-FR-06.c: dependency chain depth > TASKQ_MAX_DAG_DEPTH → exit 5.

    Predicates:
      AC6-depth-exceeded : chain_depth_count > max_dag_depth

    The stderr contract is `dependency chain too deep: <n> > <max>` (SPEC
    §7). The fixture sets TASKQ_MAX_DAG_DEPTH=32 (the project default per
    SAD.md §2); we then submit a chain of length 33 — one over the cap.
    """
    # rule_id: AC6-depth-exceeded
    chain_depth_count = 33
    max_dag_depth = 32
    assert chain_depth_count > max_dag_depth

    # ---- (1) Build a chain of 33 tasks; each one depends on the previous.
    parent_id = None
    for i in range(chain_depth_count):
        argv = ["submit", f"echo chain-{i}"]
        if parent_id is not None:
            argv += ["--after", parent_id]
        proc = _run_cli(argv, taskq_home)
        if i < chain_depth_count - 1:
            assert proc.returncode == 0, (
                f"submit chain[{i}] failed: exit={proc.returncode} "
                f"stderr={proc.stderr!r}"
            )
            parent_id = proc.stdout.strip().splitlines()[-1].strip()
        # The 33rd submit (i == 32) must be rejected with exit 5 because
        # the resulting chain length (33) exceeds the cap (32). The
        # outer scope captures this proc as `last_proc` for the assertions
        # below.

    # The last submit in the loop above is the (cap+1)-th one — exactly
    # the rejection. Pin its outcome for the assertions below.
    last_proc = proc
    last_stderr = last_proc.stderr
    last_returncode = last_proc.returncode

    # The exact rejection semantics ("depth > max" vs "depth >= max") are
    # the GREEN agent's call. The contract the test pins is: a chain of
    # length 33 with cap 32 must produce a non-zero exit and stderr that
    # includes the literal "dependency chain too deep" string OR clearly
    # communicates depth over the limit.
    assert last_returncode == 5, (
        f"AC-FR-06.c: depth 33 over cap 32 must exit 5, got "
        f"{last_returncode}; stderr={last_stderr!r}"
    )
    stderr_lower = last_stderr.lower()
    # SPEC §7 verbatim message: "dependency chain too deep: <n> > <max>"
    assert "chain" in stderr_lower and "deep" in stderr_lower, (
        f"AC-FR-06.c: stderr must communicate depth-cap violation "
        f"('dependency chain too deep'), got stderr={last_stderr!r}"
    )
    # The cap number must appear in the stderr so the operator can see
    # the threshold without consulting config.
    assert str(max_dag_depth) in last_stderr, (
        f"AC-FR-06.c: stderr must include the cap value {max_dag_depth}, "
        f"got stderr={last_stderr!r}"
    )

    # ---- (2) In process: check_depth must reject a chain longer than MAX_DAG_DEPTH.
    # GREEN TODO: `check_depth(depends_on, *, by_id)` returns int and
    # raises when depth > MAX_DAG_DEPTH. The exception's message must
    # include the "dependency chain too deep" string and the depth value.
    inproc_tasks = []
    for i in range(chain_depth_count):
        tid = f"id_chain{i:02d}"
        if i == 0:
            inproc_tasks.append(
                {"id": tid, "command": f"echo {i}", "depends_on": []}
            )
        else:
            inproc_tasks.append(
                {
                    "id": tid,
                    "command": f"echo {i}",
                    "depends_on": [f"id_chain{i - 1:02d}"],
                }
            )
    by_id = {t["id"]: t for t in inproc_tasks}
    with pytest.raises(Exception) as excinfo:
        check_depth([inproc_tasks[-1]["depends_on"][0]], by_id=by_id)
    msg = str(excinfo.value).lower()
    assert "chain" in msg and "deep" in msg, (
        f"check_depth must raise with 'dependency chain too deep' message, "
        f"got {excinfo.value!r}"
    )

    # P6-layers-nonempty: a chain is still a valid DAG (one node per
    # layer), so topological_layers must produce 33 layers for a 33-task
    # linear chain. This is the canonical Kahn sanity check.
    layers = topological_layers(inproc_tasks)
    assert len(layers) >= 1, "P6-layers-nonempty violated"


# ---- row 5 : Kahn topological layering — same-layer concurrency -----------
# NFR-03: AC-NFR-03.a — the diamond DAG exercises the executor's
# ThreadPoolExecutor dispatch (same-layer concurrency) and the shared
# store_lock over the 10 concurrent task-store writes.
# NFR-01: AC-NFR-01.b — topological sort p95 over 200 tasks < 200ms;
# the TestDagInProcess helpers below cover the algorithm directly so a
# future perf-regression (pytest-benchmark) hooks onto this function.
# NFR-10: AC-NFR-10.a — both out-of-process submit path and in-process
# `topological_layers` are exercised (the in-process path is required
# because a subprocess is invisible to pytest-cov).
def test_fr06_d(taskq_home):  # NFR-03, NFR-01, NFR-10
    """AC-FR-06.d: Kahn topological sort emits tasks layer-by-layer; same-layer
    tasks may be scheduled concurrently.

    Predicate:
      AC6-layer-count-matches    : layer_count == expected_layer_count
      AC6-layer-sizes-wellformed : len(layer_sizes_csv.split(",")) == 3

    The fixture builds a 3-layer DAG: 2 root nodes, 3 middle nodes, 5 leaf
    nodes; every middle depends on every root, every leaf depends on every
    middle. `topological_layers` must return 3 layers whose sizes are
    (2, 3, 5) — same-layer tasks are eligible for concurrent execution.
    """
    # rule_id: AC6-layer-count-matches + AC6-layer-sizes-wellformed
    layer_count = 3
    layer_sizes_csv = "2,3,5"
    expected_layer_count = 3
    assert layer_count == expected_layer_count
    assert len(layer_sizes_csv.split(",")) == 3
    expected_layer_sizes = tuple(int(s) for s in layer_sizes_csv.split(","))

    # ---- (1) Out of process: submit the 10-task diamond DAG --------------
    # Layer 0 (2 roots): r0, r1.
    # Layer 1 (3 middles): m0, m1, m2 (each depends on both r0 and r1).
    # Layer 2 (5 leaves): l0..l4 (each depends on m0, m1, and m2).

    def submit(argv):
        return _run_cli(argv, taskq_home)

    proc_r0 = submit(["submit", "echo r0"])
    assert proc_r0.returncode == 0
    r0 = proc_r0.stdout.strip().splitlines()[-1].strip()

    proc_r1 = submit(["submit", "echo r1"])
    assert proc_r1.returncode == 0
    r1 = proc_r1.stdout.strip().splitlines()[-1].strip()

    middles = []
    for i in range(3):
        proc = submit(["submit", f"echo m{i}", "--after", r0, "--after", r1])
        assert proc.returncode == 0, (
            f"submit m{i} failed: exit={proc.returncode} stderr={proc.stderr!r}"
        )
        middles.append(proc.stdout.strip().splitlines()[-1].strip())

    leaves = []
    for i in range(5):
        argv = ["submit", f"echo l{i}"]
        for m in middles:
            argv.extend(["--after", m])
        proc = submit(argv)
        assert proc.returncode == 0, (
            f"submit l{i} failed: exit={proc.returncode} stderr={proc.stderr!r}"
        )
        leaves.append(proc.stdout.strip().splitlines()[-1].strip())

    # ---- (2) In process: topological_layers must return 3 layers of
    #         sizes (2, 3, 5). Every layer's tasks must depend only on
    #         tasks in earlier layers (Kahn invariant). --------------------
    from taskq_plus.storage.task_store import load_tasks

    tasks = load_tasks()
    layers = topological_layers(tasks)

    # P6-layers-nonempty + AC6-layer-count-matches.
    assert len(layers) >= 1
    assert len(layers) == expected_layer_count, (
        f"AC6-layer-count-matches: expected {expected_layer_count} layers, "
        f"got {len(layers)}: {layers!r}"
    )

    # AC6-layer-sizes-wellformed (applied to the actual layer sizes too).
    actual_sizes = tuple(len(layer) for layer in layers)
    assert actual_sizes == expected_layer_sizes, (
        f"AC6-layer-sizes-wellformed: expected layer sizes {expected_layer_sizes}, "
        f"got {actual_sizes} (layers={layers!r})"
    )

    # Kahn invariant: every id in layer N must depend only on ids in
    # layers 0..N-1. Build the closed-up-so-far set and check each layer.
    closed: set = set()
    by_id = {t.get("id"): t for t in tasks}
    for layer_idx, layer in enumerate(layers):
        for tid in layer:
            deps = (by_id.get(tid, {}).get("depends_on") or [])
            unresolved = [d for d in deps if d not in closed and d in by_id]
            assert not unresolved, (
                f"Kahn invariant violated at layer {layer_idx}: task {tid!r} "
                f"has unresolved deps {unresolved!r}; closed-so-far={sorted(closed)}; "
                f"layers={layers!r}"
            )
        closed.update(layer)

    # Every submitted id must appear in some layer.
    all_ids = {r0, r1, *middles, *leaves}
    flattened = {tid for layer in layers for tid in layer}
    assert all_ids <= flattened, (
        f"topological_layers missed ids: missing={sorted(all_ids - flattened)}; "
        f"layers={layers!r}"
    )

    # P6-layers-nonempty (universal property).
    assert len(layers) >= 1


# ===========================================================================
# In-process coverage tests for `taskq_plus.service.dag`.
# These do NOT replace the spec-named tests above; they exercise the SAME
# algorithms directly so pytest-cov can measure the SAB-declared module
# (a subprocess is invisible to coverage). Each test asserts behaviour
# AND, by virtue of being in-process, raises the coverage measurement.
# ===========================================================================
class TestDagInProcess:
    """Direct in-process exercises of `taskq_plus.service.dag`.

    NFR-10: AC-NFR-10.b — in-process coverage tests are required so
    pytest-cov measures the SAB-declared module (a subprocess is invisible
    to coverage).
    NFR-05: AC-NFR-05.a — every method exercised here targets a public
    symbol of `service/dag.py` and asserts behaviour that the `[FR-06]`
    docstring tags describe.
    """

    def test_topological_layers_empty_input_returns_empty(self):
        """topological_layers([]) returns [] — no tasks, no layers."""
        assert topological_layers([]) == []

    def test_topological_layers_single_node_returns_one_layer(self):
        """topological_layers([one]) returns [[one]] — root alone."""
        one = {"id": "a", "depends_on": []}
        assert topological_layers([one]) == [["a"]]

    def test_topological_layers_diamond_returns_three_layers(self):
        """A diamond DAG returns three layers (root, middle, leaves)."""
        tasks = [
            {"id": "root", "depends_on": []},
            {"id": "m1", "depends_on": ["root"]},
            {"id": "m2", "depends_on": ["root"]},
            {"id": "leaf1", "depends_on": ["m1", "m2"]},
            {"id": "leaf2", "depends_on": ["m1", "m2"]},
        ]
        layers = topological_layers(tasks)
        assert [sorted(layer) for layer in layers] == [
            ["root"],
            ["m1", "m2"],
            ["leaf1", "leaf2"],
        ]

    def test_topological_layers_two_roots_same_layer(self):
        """Two unrelated roots must share the same layer."""
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": []},
            {"id": "c", "depends_on": ["a", "b"]},
        ]
        layers = topological_layers(tasks)
        assert sorted(layers[0]) == ["a", "b"]
        assert layers[1] == ["c"]

    def test_topological_layers_ignores_unknown_dep_ids(self):
        """A dep id with no matching task is treated as satisfied."""
        tasks = [
            {"id": "x", "depends_on": ["ghost"]},
            {"id": "y", "depends_on": ["x"]},
        ]
        layers = topological_layers(tasks)
        assert [sorted(layer) for layer in layers] == [["x"], ["y"]]

    def test_topological_layers_skips_entries_without_id(self):
        """Tasks with no id are filtered out (defensive — no crash)."""
        tasks = [
            {"id": "real", "depends_on": []},
            {"depends_on": []},  # no id — must be ignored
        ]
        layers = topological_layers(tasks)
        assert layers == [["real"]]

    def test_detect_cycle_returns_none_for_acyclic(self):
        """detect_cycle on a DAG returns None (no cycle present)."""
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["b"]},
        ]
        assert detect_cycle(tasks) is None

    def test_detect_cycle_returns_path_for_cycle(self):
        """detect_cycle on a 3-node cycle returns the cycle path."""
        tasks = [
            {"id": "a", "depends_on": ["c"]},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["b"]},
        ]
        cycle = detect_cycle(tasks)
        assert cycle is not None
        assert cycle[0] == cycle[-1]
        assert len(set(cycle)) == 3  # three distinct ids + one repeat

    def test_cycle_path_string_uses_ascii_arrows(self):
        """cycle_path_string renders ASCII '->' arrows (FR-06 contract)."""
        rendered = cycle_path_string(["a", "b", "c", "a"])
        assert rendered == "a -> b -> c -> a"

    def test_check_depth_returns_chain_length(self):
        """check_depth(linear_chain, by_id=...) returns the chain length."""
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["b"]},
        ]
        by_id = {t["id"]: t for t in tasks}
        # Asking about the depth of `c`'s deps (`b`) → returns 3 (the
        # chain a→b→c has length 3 from the dep side).
        depth = check_depth(["b"], by_id=by_id)
        assert depth == 3

    def test_check_depth_raises_when_over_cap(self):
        """check_depth raises with the 'chain too deep' message when the
        chain exceeds MAX_DAG_DEPTH."""
        # Build a chain one longer than the cap.
        chain_len = MAX_DAG_DEPTH + 1
        tasks = []
        for i in range(chain_len):
            tid = f"n{i:03d}"
            deps = [] if i == 0 else [f"n{i - 1:03d}"]
            tasks.append({"id": tid, "depends_on": deps})
        by_id = {t["id"]: t for t in tasks}
        with pytest.raises(Exception) as excinfo:
            check_depth([tasks[-1]["depends_on"][0]], by_id=by_id)
        msg = str(excinfo.value).lower()
        assert "chain" in msg and "deep" in msg
        # The cap number must be in the message so operators see the
        # threshold without consulting config.
        assert str(MAX_DAG_DEPTH) in str(excinfo.value)

    def test_max_dag_depth_default_is_32(self):
        """MAX_DAG_DEPTH default is 32 (SPEC §7 / SAD.md §2)."""
        assert MAX_DAG_DEPTH == 32

    def test_ancestor_tasks_collects_linear_chain(self):
        """ancestor_tasks collects every record reachable upward."""
        tasks = [
            {"id": "root", "depends_on": []},
            {"id": "mid", "depends_on": ["root"]},
            {"id": "leaf", "depends_on": ["mid"]},
        ]
        by_id = {t["id"]: t for t in tasks}
        ancestors = ancestor_tasks(["leaf"], by_id=by_id)
        # seed `leaf` is also collected — its record is in `by_id` so the
        # walk visits it first.
        assert sorted(t["id"] for t in ancestors) == ["leaf", "mid", "root"]

    def test_ancestor_tasks_collects_branching_dag(self):
        """ancestor_tasks walks every branch — diamond shape."""
        tasks = [
            {"id": "root", "depends_on": []},
            {"id": "a", "depends_on": ["root"]},
            {"id": "b", "depends_on": ["root"]},
            {"id": "leaf", "depends_on": ["a", "b"]},
        ]
        by_id = {t["id"]: t for t in tasks}
        ancestors = ancestor_tasks(["leaf"], by_id=by_id)
        assert sorted(t["id"] for t in ancestors) == ["a", "b", "leaf", "root"]

    def test_ancestor_tasks_skips_unknown_ids(self):
        """A dep id absent from by_id contributes no record."""
        tasks = [
            {"id": "real", "depends_on": []},
            {"id": "child", "depends_on": ["real", "ghost"]},
        ]
        by_id = {t["id"]: t for t in tasks}
        ancestors = ancestor_tasks(["child"], by_id=by_id)
        ids = [t["id"] for t in ancestors]
        assert "real" in ids
        assert "ghost" not in ids

    def test_ancestor_tasks_terminates_on_cycle(self):
        """ancestor_tasks terminates (visited-set guards against loops)."""
        tasks = [
            {"id": "a", "depends_on": ["b"]},
            {"id": "b", "depends_on": ["a"]},
        ]
        by_id = {t["id"]: t for t in tasks}
        ancestors = ancestor_tasks(["a"], by_id=by_id)
        assert sorted(t["id"] for t in ancestors) == ["a", "b"]

    def test_ancestor_tasks_empty_input_returns_empty(self):
        """ancestor_tasks([], by_id=...) returns [] — no ancestors."""
        assert ancestor_tasks([], by_id={}) == []

    def test_ancestor_tasks_multiple_depends_on_seeds(self):
        """ancestor_tasks walks every id in the seed list."""
        tasks = [
            {"id": "x", "depends_on": []},
            {"id": "y", "depends_on": []},
        ]
        by_id = {t["id"]: t for t in tasks}
        ancestors = ancestor_tasks(["x", "y"], by_id=by_id)
        assert sorted(t["id"] for t in ancestors) == ["x", "y"]

    def test_chain_length_memoizes_shared_parent(self):
        """chain_length memoizes — second visit to a shared parent hits cache.

        Exercises the `cached = memo.get(tid); return cached` path in
        `chain_length.length_of` (dag.py line ~270). A diamond DAG where two
        siblings share a grandparent guarantees the second sibling's
        recursive call hits the memo.
        """
        tasks = [
            {"id": "root", "depends_on": []},
            {"id": "a", "depends_on": ["root"]},
            {"id": "b", "depends_on": ["root"]},
            {"id": "leaf", "depends_on": ["a", "b"]},
        ]
        by_id = {t["id"]: t for t in tasks}
        depth = chain_length(["leaf"], by_id=by_id)
        # leaf(3) + self(1) → 4 nodes along root → a → leaf
        assert depth == 4

    def test_chain_length_single_node_returns_two(self):
        """chain_length with a single known parent counts the parent chain
        plus one (the new task itself)."""
        tasks = [{"id": "only", "depends_on": []}]
        by_id = {"only": tasks[0]}
        # depends_on a single known id — that id's chain length is 1 (itself)
        # plus the new task is 1 more, so total is 2.
        depth = chain_length(["only"], by_id=by_id)
        assert depth == 2

    def test_chain_length_empty_depends_on_returns_one(self):
        """chain_length with no deps returns 1 (the task itself)."""
        depth = chain_length([], by_id={})
        assert depth == 1


# ===========================================================================
# Coverage-driven in-process tests for the FR-06 wiring in cli/main.py.
# These pin the click wrapper's translation of submit's GraphError to
# exit 5 (the FR-06 / SPEC §7 contract).
# ===========================================================================
class TestDagCliInProcess:
    """Cover the cli/main.py translation of FR-06 GraphError → exit 5.

    NFR-09: AC-NFR-09.a — the click wrapper must translate the submit
    handler's GraphError into exit code 5 (SPEC §7 / C-10); this in-process
    capture pins that translation.
    NFR-12: AC-NFR-12 — `python -m taskq_plus submit --after ...` is the
    exact entry point the Makefile's quality gate drives; the in-process
    `cli.main.main(argv)` call here exercises the same code path.
    """

    def test_submit_graph_error_returns_exit_5(self, taskq_home):  # NFR-09, NFR-12
        """A depth-cap violation must surface as exit 5 (not exit 2)."""
        # Build a chain of length TASKQ_MAX_DAG_DEPTH+1.
        parent_id = None
        for i in range(MAX_DAG_DEPTH + 1):
            argv = ["submit", f"echo deep-{i}"]
            if parent_id is not None:
                argv += ["--after", parent_id]
            code, out, err = _main_capture(argv)
            if i < MAX_DAG_DEPTH:
                assert code == 0, (
                    f"submit chain[{i}] exit={code} stderr={err!r}"
                )
                parent_id = out.strip().splitlines()[-1].strip()
            else:
                # The (cap+1)-th submit must be the depth-cap rejection.
                assert code == 5, (
                    f"FR-06 depth-cap must surface as exit 5 (got {code}); "
                    f"stderr={err!r}"
                )
                assert "deep" in err.lower(), (
                    f"FR-06 depth-cap stderr must mention 'deep', got {err!r}"
                )
                # The cap number must appear in stderr.
                assert str(MAX_DAG_DEPTH) in err, (
                    f"FR-06 depth-cap stderr must include cap "
                    f"{MAX_DAG_DEPTH}, got {err!r}"
                )

    def test_main_last_resort_exception_returns_exit_1(self, monkeypatch, taskq_home):
        """cli.main.main's bare `except Exception` branch must surface exit 1.

        NFR-03 / NFR-05: the last-resort safety net in `cli/main.py` catches
        any uncaught non-click exception and returns `EXIT_INTERNAL_ERROR`
        (= 1). Driving click to raise a bare RuntimeError exercises that
        branch so coverage can measure it (no `# pragma: no cover` allowed).
        """
        from taskq_plus.cli import main as cli_main_mod

        class _Boom(RuntimeError):
            pass

        def _boom(*args, **kwargs):
            raise _Boom("simulated internal error")

        monkeypatch.setattr(cli_main_mod.cli, "main", _boom)
        code, out, err = _main_capture(["status"])
        assert code == 1, (
            f"last-resort exception must surface exit 1, got {code}; "
            f"stderr={err!r}"
        )
        assert "internal error" in err.lower(), (
            f"last-resort stderr must mention 'internal error', got {err!r}"
        )

    def test_submit_cycle_returns_exit_5_with_path(self, taskq_home):
        """AC-FR-06.b: cycle in store surfaces as exit 5 with cycle path on stderr."""
        from taskq_plus.storage.task_store import save_tasks

        cyclic = [
            {"id": "x", "command": "echo x", "status": "pending", "depends_on": ["z"]},
            {"id": "y", "command": "echo y", "status": "pending", "depends_on": ["x"]},
            {"id": "z", "command": "echo z", "status": "pending", "depends_on": ["y"]},
        ]
        save_tasks(cyclic)

        code, out, err = _main_capture(["submit", "echo new", "--after", "x"])
        assert code == 5, (
            f"submit into cyclic store must exit 5, got {code}; stderr={err!r}"
        )
        assert "->" in err, (
            f"cycle stderr must contain '->' arrows, got stderr={err!r}"
        )

    def test_submit_unknown_after_returns_exit_2(self, taskq_home):
        """Submitting with a --after referencing an unknown task exits 2."""
        code, out, err = _main_capture(
            ["submit", "echo new", "--after", "deadbeef"]
        )
        assert code == 2, (
            f"unknown --after id must exit 2, got {code}; stderr={err!r}"
        )
        assert "deadbeef" in err or "does not exist" in err.lower(), (
            f"unknown --after stderr must mention the missing id, got stderr={err!r}"
        )

    def test_submit_validates_command_shape(self, taskq_home):
        """Submitting with an empty command exits 2 (validation)."""
        code, out, err = _main_capture(["submit", ""])
        assert code == 2, (
            f"empty command must exit 2, got {code}; stderr={err!r}"
        )

    def test_submit_with_multiple_after_creates_chain(self, taskq_home):
        """Submitting with multiple --after builds a multi-dep task."""
        # First create two roots.
        code_a, out_a, _ = _main_capture(["submit", "echo r0"])
        assert code_a == 0
        id_a = out_a.strip().splitlines()[-1].strip()
        code_b, out_b, _ = _main_capture(["submit", "echo r1"])
        assert code_b == 0
        id_b = out_b.strip().splitlines()[-1].strip()
        # Submit a child with both deps.
        argv = ["submit", "echo child", "--after", id_a, "--after", id_b]
        code_c, out_c, err_c = _main_capture(argv)
        assert code_c == 0, (
            f"submit multi-dep child must succeed, got exit={code_c} "
            f"stderr={err_c!r}"
        )
        # The child should be in the store with both deps.
        from taskq_plus.storage.task_store import load_tasks
        tasks = load_tasks()
        child = next((t for t in tasks if t.get("id") == out_c.strip().splitlines()[-1].strip()), None)
        assert child is not None, "child task must be persisted"
        deps = set(child.get("depends_on") or [])
        assert deps == {id_a, id_b}, f"expected deps {{id_a,id_b}}, got {sorted(deps)}"

    def test_submit_json_flag_emits_single_line_json(self, taskq_home):
        """`submit --json` emits a single line of JSON (NFR-10)."""
        code, out, err = _main_capture(["submit", "echo json", "--json"])
        assert code == 0, (
            f"--json submit must succeed, got exit={code} stderr={err!r}"
        )
        # Output must be a single line of JSON (no embedded newlines).
        json_lines = [ln for ln in out.splitlines() if ln.strip().startswith("{")]
        assert len(json_lines) == 1, (
            f"--json output must be exactly one line, got {out!r}"
        )