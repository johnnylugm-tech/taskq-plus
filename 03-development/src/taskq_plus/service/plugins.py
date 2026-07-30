"""Plugin hook system — allowlist-loaded modules with exception isolation.

[FR-07]
Citations:
  - SPEC.md §3 FR-07 (plugin is a Python module exposing `pre_run(task)` /
    `post_run(task, result)`, loaded by name from `TASKQ_PLUGINS`).
  - SPEC.md §3 FR-07 (security iron rules: allowlist regex + importlib only;
    path-form / URL-form names rejected).
  - SPEC.md §3 FR-07 (plugin raises → task continues; 3 consecutive failures
    disable the plugin for the remainder of the run).
  - SPEC.md §3 FR-07 (`plugins list` reports module name, hooks, load status).
  - SPEC.md §8 #15 (NFR-02: no `eval` / `exec` / dynamic `__import__`).
"""

from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Sequence, Union


# ---------------------------------------------------------------------------
# Public constants — the FR-07 iron rules.
# ---------------------------------------------------------------------------
# SPEC §3 FR-07: module name must match `^[A-Za-z_][A-Za-z0-9_.]*$`. Path forms
# (`../evil.py`) and URL forms (`https://…`) MUST NOT match.
PLUGIN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

# The 3-strikes ceiling — a plugin that fails this many times in one run is
# disabled for the remainder of that run (SPEC §3 FR-07).
MAX_CONSECUTIVE_FAILURES: int = 3

# Hook names recognised by the FR-07 dispatch loop. `pre_run` and `post_run`
# are the only SPEC-mandated hooks; any other attribute on the module is
# ignored.
_HOOK_NAMES: tuple[str, ...] = ("pre_run", "post_run")


# ---------------------------------------------------------------------------
# Exception taxonomy — the FR-07 load failure (SPEC §7 exit-code 6).
# ---------------------------------------------------------------------------
class PluginLoadError(Exception):
    """A plugin allowlist entry or import failed (SPEC §3 FR-07 → exit 6)."""

    exit_code: int = 6


# ---------------------------------------------------------------------------
# Public dataclasses — the FR-07 dispatch contract.
# ---------------------------------------------------------------------------
@dataclass
class LoadedPlugin:
    """A successfully-imported plugin module, with mutable in-run state.

    `failure_count` and `disabled` are mutated by `dispatch()` so the
    3-strikes counter PERSISTS across multiple `dispatch()` calls within
    one run (SPEC §3 FR-07 state lives inside the dispatch loop, not at
    module scope — `state_mode="isolate_per_test"`).
    """

    name: str
    module: Any
    hooks: List[str]
    status: str = "loaded"
    failure_count: int = 0
    disabled: bool = False


@dataclass
class PluginFailure:
    """One caught exception from a plugin hook (SPEC §3 FR-07)."""

    hook: str
    plugin: str
    error: str


@dataclass
class PluginDispatchResult:
    """Aggregate outcome of a single `dispatch()` call.

    `disabled` is the list of plugin names newly disabled by THIS call
    (a plugin is appended at most once when its 3rd consecutive failure
    lands in the same call). `failures` is the list of caught exceptions
    for this call — the caller turns each into a `plugin_error` audit
    event (FR-08).
    """

    disabled: List[str] = field(default_factory=list)
    failures: List[PluginFailure] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _coerce_names(settings: Union[None, str, Sequence[str]]) -> List[str]:
    """Return the trimmed, non-empty list of plugin names from `settings`.

    A `None` falls back to `TASKQ_PLUGINS` (the only source the FR-07 spec
    names). A string is split on commas; a sequence is iterated as-is. Empty
    entries are dropped — `TASKQ_PLUGINS=",,,foo,,"` loads only `foo`.
    """
    if settings is None:
        raw = os.environ.get("TASKQ_PLUGINS", "") or ""
        pieces: Iterable[str] = raw.split(",")
    elif isinstance(settings, str):
        pieces = settings.split(",")
    else:
        pieces = settings
    return [str(piece).strip() for piece in pieces if str(piece).strip()]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def load_plugins(
    settings: Union[None, str, Sequence[str]] = None,
) -> List[LoadedPlugin]:
    """Resolve the FR-07 allowlist into a list of successfully loaded plugins.

    Each name is matched against `PLUGIN_NAME_RE` (SPEC §3 FR-07 iron rule);
    a non-matching name — including any path form (`../evil.py`) or URL form
    (`https://…`) — raises `PluginLoadError` BEFORE any `importlib` call so
    no dynamic code is ever routed through `eval` / `exec` / file paths /
    URLs. Modules that fail to import also raise `PluginLoadError` (exit 6).

    The returned `LoadedPlugin.status` is always `"loaded"`; a partial-failure
    import attempt is a hard error, not a "loaded-with-warning" status.
    """
    plugins: List[LoadedPlugin] = []
    for name in _coerce_names(settings):
        if not PLUGIN_NAME_RE.match(name):
            raise PluginLoadError(
                f"plugin name {name!r} rejected by allowlist "
                f"(must match {PLUGIN_NAME_RE.pattern!r})"
            )
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # ImportError + everything transitive.
            raise PluginLoadError(
                f"plugin {name!r} failed to import: {exc}"
            ) from exc
        hooks = [hook for hook in _HOOK_NAMES if hasattr(module, hook)]
        plugins.append(
            LoadedPlugin(
                name=name,
                module=module,
                hooks=hooks,
                status="loaded",
            )
        )
    return plugins


def describe(plugins: Sequence[LoadedPlugin]) -> List[dict]:
    """Return one `{name, hooks, status}` dict per plugin (SPEC §3 FR-07).

    Hooks are returned in the canonical `pre_run, post_run` order so the
    `plugins list` rendering is stable across runs.
    """
    described: List[dict] = []
    for plugin in plugins:
        described.append(
            {
                "name": plugin.name,
                "hooks": list(plugin.hooks),
                "status": plugin.status,
            }
        )
    return described


def dispatch(hook: str, plugins: Sequence[LoadedPlugin], *args: Any) -> PluginDispatchResult:
    """Invoke `hook` on every enabled plugin that registers it.

    Implements SPEC §3 FR-07 exception isolation: every plugin call is wrapped
    in a `try / except Exception` so a misbehaving plugin CANNOT interrupt
    task execution. Each caught exception is recorded as a `PluginFailure`;
    `KeyboardInterrupt`/`SystemExit` are deliberately NOT caught so the
    operator can still interrupt the run.

    The 3-strikes counter lives on each `LoadedPlugin` (its `failure_count`
    and `disabled` fields) so multiple `dispatch()` calls within ONE run see
    the same counter — `state_mode="isolate_per_test"` is satisfied because
    each test loads a fresh plugin list.

    `result.disabled` is the list of EVERY plugin currently disabled at the
    end of this call (not just the ones disabled THIS call), so callers can
    observe the run-wide disabled set without sharing extra state.
    """
    result = PluginDispatchResult()
    for plugin in plugins:
        if plugin.disabled:
            continue
        if hook not in plugin.hooks:
            continue
        try:
            getattr(plugin.module, hook)(*args)
        except Exception as exc:  # noqa: BLE001 — exception isolation is the whole point.
            plugin.failure_count += 1
            result.failures.append(
                PluginFailure(
                    hook=hook,
                    plugin=plugin.name,
                    error=str(exc),
                )
            )
            if plugin.failure_count >= MAX_CONSECUTIVE_FAILURES:
                plugin.disabled = True
    for plugin in plugins:
        if plugin.disabled and plugin.name not in result.disabled:
            result.disabled.append(plugin.name)
    return result


__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "PLUGIN_NAME_RE",
    "PluginDispatchResult",
    "PluginFailure",
    "PluginLoadError",
    "LoadedPlugin",
    "describe",
    "dispatch",
    "load_plugins",
]
