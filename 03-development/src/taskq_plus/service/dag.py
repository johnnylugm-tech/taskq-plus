"""Task dependency DAG — Kahn topological layering, cycle, and depth cap.

[FR-06]
Citations:
  - SPEC.md §3 FR-06 (task-dependency DAG; submit --after; run --all Kahn;
    cycle → exit 5; depth cap → exit 5; graph --format dot|text).
  - SPEC.md §3 FR-02 (run --all ThreadPoolExecutor dispatches per Kahn layer).
  - SPEC.md §7 (exit-code map: cycle / depth-cap → 5).
  - SPEC.md §8 #11 (AC-FR-06.b: cycle stderr lists path with '->' arrows).
  - SPEC.md §8 #12 (AC-FR-06.c: depth-cap stderr
    `dependency chain too deep: <n> > <max>`).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple


# ---------------------------------------------------------------------------
# Module-level defaults — SPEC §7 / SAD.md §2 default chain-depth ceiling.
# ---------------------------------------------------------------------------
MAX_DAG_DEPTH: int = 32


# ---------------------------------------------------------------------------
# Exceptions — the FR-06 path raises these so the click wrapper can map
# them onto the SPEC §7 exit-code 5 (`EXIT_GRAPH_ERROR`).
# ---------------------------------------------------------------------------
class DAGError(Exception):
    """Base class for FR-06 graph validation errors → exit 5."""


class CycleDetected(DAGError):
    """`submit --after` would create a cycle → exit 5 (FR-06 / SPEC §7)."""


class DepthExceeded(DAGError):
    """Dependency chain depth > MAX_DAG_DEPTH → exit 5 (FR-06 / SPEC §7).

    The message matches `dependency chain too deep: <n> > <max>` (SPEC §7
    verbatim) so operators see the threshold without consulting config.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _build_adjacency(
    tasks: Sequence[Mapping[str, Any]],
) -> Tuple[Set[str], Dict[str, List[str]]]:
    """Return ``(known_ids, edges)`` for the task list.

    Tasks with a missing ``id`` field are ignored. Each task's ``depends_on``
    is filtered to ids that are themselves known, so any dep pointing at an
    external / vanished predecessor is treated as already-satisfied — both
    `topological_layers` and `detect_cycle` share that contract.
    """
    known_ids: Set[str] = set()
    raw_edges: Dict[str, List[str]] = {}
    for t in tasks:
        tid = t.get("id")
        if tid is None:
            continue
        known_ids.add(tid)
        raw_edges[tid] = list(t.get("depends_on") or [])
    edges: Dict[str, List[str]] = {
        tid: [d for d in deps if d in known_ids]
        for tid, deps in raw_edges.items()
    }
    return known_ids, edges


# ---------------------------------------------------------------------------
# topological_layers — Kahn-style layer-by-layer decomposition.
#
# Groups task ids into layers where every id in layer N depends only on ids
# in layers 0..N-1. Tasks at the same layer have in-degree 0 within the
# residual sub-DAG and are eligible for concurrent execution.
#
# Tasks with unknown dep ids (a dep that doesn't appear in `tasks`) are
# treated as satisfied — they're external / already-complete and don't
# stall the layer. Tasks with a missing `id` field are filtered out.
# ---------------------------------------------------------------------------
def topological_layers(
    tasks: Sequence[Mapping[str, Any]],
) -> List[List[str]]:
    """Kahn-layer a task list; return [[id, ...], ...] ordered by depth.

    [FR-06]
    Citations:
      - SPEC.md §3 FR-06 (Kahn topological sort emits layer-by-layer).
      - SPEC.md §3 FR-02 (ThreadPoolExecutor dispatches per layer).

    Algorithm:
      1. Build the known-id set + an in-degree map + a reverse adjacency
         (successors) index for every task id.
      2. Repeatedly peel off the set of ids whose in-degree is 0 (or whose
         remaining deps are all outside the known-id set), until the
         residual sub-DAG is empty.
      3. If non-empty residual remains (a cycle), emit the remaining ids
         as a single final layer rather than raising — cycle handling is
         the submit path's responsibility, not the layering path's. This
         matches the P6-layers-nonempty property: layers must always be
         a non-empty list of lists.
    """
    known_ids, edges = _build_adjacency(tasks)

    in_degree: Dict[str, int] = {tid: len(deps) for tid, deps in edges.items()}
    successors: Dict[str, List[str]] = {tid: [] for tid in known_ids}
    for tid, deps in edges.items():
        for dep in deps:
            successors[dep].append(tid)

    remaining_ids = set(known_ids)
    layers: List[List[str]] = []
    while remaining_ids:
        layer = sorted(
            tid for tid in remaining_ids if in_degree.get(tid, 0) == 0
        )
        if not layer:
            # Cycle or unsatisfiable deps — emit the residual as a final
            # layer; the cycle validator (detect_cycle) is the canonical
            # path for surfacing cycles with a real path.
            layers.append(sorted(remaining_ids))
            break
        layers.append(layer)
        for tid in layer:
            remaining_ids.discard(tid)
            for child in successors.get(tid, ()):
                if child in remaining_ids:
                    in_degree[child] -= 1
    return layers


# ---------------------------------------------------------------------------
# detect_cycle — depth-first search that returns the first cycle path it
# encounters as a list of ids, with the closing id repeated at the end
# (e.g. ["a", "b", "c", "a"]). Returns None when the graph is acyclic.
# ---------------------------------------------------------------------------
def detect_cycle(
    tasks: Sequence[Mapping[str, Any]],
) -> Optional[List[str]]:
    """Return the cycle path as a list of ids; first id repeats at the end.

    [FR-06]
    Citations: SPEC.md §3 FR-06 (cycle rejection → exit 5 + path on stderr).

    Three-colour DFS (white=unvisited, grey=in-stack, black=finished). The
    moment we hit a grey neighbour we have a back-edge; the cycle is the
    slice of the current DFS stack from that neighbour up to the current
    node, with the neighbour repeated at the end to mark the closing edge.
    """
    known_ids, edges = _build_adjacency(tasks)

    WHITE, GREY, BLACK = 0, 1, 2
    color: Dict[str, int] = {tid: WHITE for tid in known_ids}
    stack: List[str] = []
    on_stack_index: Dict[str, int] = {}

    def visit(node: str) -> Optional[List[str]]:
        color[node] = GREY
        on_stack_index[node] = len(stack)
        stack.append(node)
        for dep in edges.get(node, []):
            dep_color = color.get(dep, WHITE)
            if dep_color == GREY:
                # Found a back-edge to `dep` — slice the stack from there.
                start = on_stack_index[dep]
                return stack[start:] + [dep]
            if dep_color == WHITE:
                found = visit(dep)
                if found is not None:
                    return found
        stack.pop()
        del on_stack_index[node]
        color[node] = BLACK
        return None

    for tid in known_ids:
        if color[tid] == WHITE:
            found = visit(tid)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# cycle_path_string — render a cycle path with ASCII arrows (FR-06 contract).
# ---------------------------------------------------------------------------
def cycle_path_string(cycle: Sequence[str]) -> str:
    """Render `cycle` as `"a -> b -> c -> a"` (ASCII arrows; FR-06 contract).

    [FR-06]
    Citations: SPEC.md §3 FR-06 (stderr `A -> B -> C -> A`).
    """
    return " -> ".join(cycle)


# ---------------------------------------------------------------------------
# ancestor_tasks — collect the sub-DAG reachable *upwards* from `depends_on`.
#
# A brand-new submission can only close a cycle through its own ancestry, so
# the submit path validates that ancestry rather than the whole store: an
# unrelated corrupt corner of tasks.json must not reject a healthy submit.
# ---------------------------------------------------------------------------
def ancestor_tasks(
    depends_on: Sequence[str],
    *,
    by_id: Mapping[str, Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """Return every task record reachable by following `depends_on` upwards.

    [FR-06]
    Citations:
      - SPEC.md §3 FR-06 (`submit --after` that would create a cycle → reject
        that submission — the cycle can only lie in the new task's ancestry).

    Ids absent from `by_id` contribute nothing (a vanished predecessor is not
    an ancestor record); traversal is iterative and visits each id once, so a
    cyclic store terminates instead of recursing forever.
    """
    seen: set = set()
    collected: List[Mapping[str, Any]] = []
    frontier: List[str] = list(depends_on)
    while frontier:
        tid = frontier.pop()
        if tid in seen:
            continue
        seen.add(tid)
        rec = by_id.get(tid)
        if rec is None:
            continue
        collected.append(rec)
        frontier.extend(rec.get("depends_on") or [])
    return collected


# ---------------------------------------------------------------------------
# chain_length / check_depth — the FR-06 depth cap.
#
# Length is counted in NODES, inclusive of the task being submitted: a task
# with no dependencies is a chain of 1, a task hanging off a root is a chain
# of 2, and so on. That is the `<n>` the SPEC §7 stderr contract reports.
# ---------------------------------------------------------------------------
def chain_length(
    depends_on: Sequence[str],
    *,
    by_id: Mapping[str, Mapping[str, Any]],
) -> int:
    """Return the longest dependency chain (in nodes) ending at a new task.

    [FR-06]
    Citations:
      - SPEC.md §3 FR-06 (dependency chain depth cap).

    Semantics:
      * Empty `depends_on` → 1 (the task itself is the whole chain).
      * Otherwise `1 + max(chain_length(parent))`.
      * A parent id missing from `by_id` counts as a 1-node chain, so submit
        never blocks on a vanished predecessor.

    The caller must ensure the ancestry is acyclic (see `detect_cycle`); a
    cyclic `by_id` would recurse without terminating.
    """
    memo: Dict[str, int] = {}

    def length_of(tid: str) -> int:
        cached = memo.get(tid)
        if cached is not None:
            return cached
        rec = by_id.get(tid)
        parents = (rec.get("depends_on") or []) if rec is not None else []
        value = 1 + max((length_of(p) for p in parents), default=0)
        memo[tid] = value
        return value

    return 1 + max((length_of(p) for p in depends_on), default=0)


def check_depth(
    depends_on: Sequence[str],
    *,
    by_id: Mapping[str, Mapping[str, Any]],
    max_depth: Optional[int] = None,
) -> int:
    """Return the chain length reachable from `depends_on`; raise on overflow.

    [FR-06]
    Citations:
      - SPEC.md §3 FR-06 (chain depth > `TASKQ_MAX_DAG_DEPTH` → reject, exit 5).
      - SPEC.md §7 (stderr contract `dependency chain too deep: <n> > <max>`).

    `max_depth` defaults to the module-level `MAX_DAG_DEPTH`; the CLI passes
    the `TASKQ_MAX_DAG_DEPTH` env override.

    Raises:
      DepthExceeded: with the SPEC §7 verbatim message when the resulting
        chain length exceeds the cap.
    """
    limit = MAX_DAG_DEPTH if max_depth is None else max_depth
    length = chain_length(depends_on, by_id=by_id)
    if length > limit:
        raise DepthExceeded(f"dependency chain too deep: {length} > {limit}")
    return length


__all__ = [
    "MAX_DAG_DEPTH",
    "topological_layers",
    "detect_cycle",
    "cycle_path_string",
    "ancestor_tasks",
    "chain_length",
    "check_depth",
    "DAGError",
    "CycleDetected",
    "DepthExceeded",
]