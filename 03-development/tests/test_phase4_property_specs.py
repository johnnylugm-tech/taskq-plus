"""Phase 4 property-based tests — exercises the four Direction-B invariants
declared in 02-architecture/TEST_SPEC.md using Hypothesis @given.

The 4 property invariants (single FR per property):
  FR-03 P3-backoff-monotone        : expected_sleep_seconds > 0 and base > 0
  FR-04 P4-cache-key-deterministic : signature(cmd) == signature(cmd)
  FR-06 P6-layers-nonempty         : len(topological_layers(tasks)) >= 1
  FR-08 P8-redaction-idempotent    : redact(redact(t)) == redact(t)

These tests are NOT a replacement for the spec-named tests in test_frNN.py;
they are a separate, fuzz-driven companion that exercises each declared
property over a randomised input domain. Pre-flight requires at least one
hypothesis @given test per property; this file is the single container.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st

# Make src/ importable so the property tests can reach the SAB-bound modules.
TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ===========================================================================
# FR-03 — P3-backoff-monotone (TEST_SPEC.md §FR-03 Properties table).
#
# Invariant: float(expected_sleep_seconds) > 0 AND float(base) > 0,
# over rows 2..4 of FR-03 (retry_attempt_n ∈ {0, 1, 2}).
# ===========================================================================
@given(
    base_seconds=st.floats(
        min_value=1e-6,
        max_value=3600.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    attempt_n=st.integers(min_value=0, max_value=10),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_fr03_property_backoff_monotone(base_seconds: float, attempt_n: int):
    """Property: for any base>0 and attempt_n>=0, the computed backoff is >0.

    The Direction-B invariant P3-backoff-monotone states that
    expected_sleep_seconds and backoff_base_seconds are both positive
    whenever they appear in a TEST_SPEC row. The underlying formula
    (base * 2**n) is monotone in base, so the property holds iff
    base>0 and the implementation does not floor/ceil the result to 0.
    """
    from taskq_plus.service.breaker import compute_backoff_seconds

    computed = compute_backoff_seconds(attempt_n, base_seconds)
    assert computed > 0, (
        f"P3-backoff-monotone violated: compute_backoff_seconds({attempt_n}, "
        f"{base_seconds}) = {computed} (must be > 0)"
    )
    assert base_seconds > 0, "base_seconds must be > 0 by strategy"


# ===========================================================================
# FR-04 — P4-cache-key-deterministic (TEST_SPEC.md §FR-04 Properties table).
#
# Invariant: signature(command) == signature(command) for the SAME command.
# Trivially true for any pure function — the property test fuzzes many
# commands (incl. empty, unicode, binary-ish, very long) and asserts the
# signature is byte-stable.
# ===========================================================================
@given(command=st.text(min_size=0, max_size=200))
@settings(max_examples=50, deadline=None)
def test_fr04_property_cache_key_deterministic(command: str):
    """Property: cache_signature is a pure function of its input.

    For any string s, cache_signature(s) == cache_signature(s).
    Secondary invariants (held by the property as side-effects of purity):
      - signature is exactly 64 chars (sha256 hex)
      - signature uses only [0-9a-f]
    """
    from taskq_plus.service.cache import cache_signature

    sig_a = cache_signature(command)
    sig_b = cache_signature(command)
    assert sig_a == sig_b, (
        f"P4-cache-key-deterministic violated: cache_signature({command!r}) "
        f"returned {sig_a!r} then {sig_b!r}"
    )
    assert len(sig_a) == 64, (
        f"cache_signature must be 64-char sha256 hex; got len={len(sig_a)} "
        f"for command={command!r}"
    )
    assert set(sig_a) <= set("0123456789abcdef"), (
        f"cache_signature must be lowercase hex; got charset diff "
        f"{set(sig_a) - set('0123456789abcdef')} for command={command!r}"
    )


# ===========================================================================
# FR-06 — P6-layers-nonempty (TEST_SPEC.md §FR-06 Properties table).
#
# Invariant: len(topological_layers(tasks)) >= 1 over a NON-EMPTY
# sequence of task dicts.
# ===========================================================================
@given(
    n=st.integers(min_value=1, max_value=12),
    ids=st.lists(
        st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd"),
                whitelist_characters="_-",
            ),
            min_size=1,
            max_size=8,
        ),
        min_size=1,
        max_size=12,
        unique=True,
    ),
)
@settings(max_examples=50, deadline=None)
def test_fr06_property_topological_layers_nonempty(n: int, ids: list):
    """Property: a non-empty DAG has at least one Kahn layer.

    Build a linear chain of length n over a unique id list; that is always
    a valid DAG (acyclic). topological_layers must produce >= 1 layer, and
    every id must appear exactly once across the layers.
    """
    from taskq_plus.service.dag import topological_layers

    # Truncate to n unique ids (strategy may yield more than n).
    chosen = list(ids[:n])
    if not chosen:
        chosen = ["only"]
    tasks = []
    for i, tid in enumerate(chosen):
        deps = [] if i == 0 else [chosen[i - 1]]
        tasks.append({"id": tid, "command": f"echo {tid}", "depends_on": deps})

    layers = topological_layers(tasks)
    assert len(layers) >= 1, (
        f"P6-layers-nonempty violated: linear chain of {len(tasks)} tasks "
        f"produced {len(layers)} layers (must be >= 1)"
    )
    flattened = [tid for layer in layers for tid in layer]
    assert sorted(flattened) == sorted(chosen), (
        f"topological_layers must include every input id exactly once; "
        f"got flattened={sorted(flattened)} expected={sorted(chosen)}"
    )


# ===========================================================================
# FR-08 — P8-redaction-idempotent (TEST_SPEC.md §FR-08 Properties table).
#
# Invariant: redact_text(redact_text(text)) == redact_text(text) for any text.
# ===========================================================================
@given(text=st.text(max_size=200))
@settings(max_examples=50, deadline=None)
def test_fr08_property_redaction_idempotent(text: str):
    """Property: redact_text is idempotent.

    A second pass over already-redacted text must be a no-op:
    redact(redact(t)) == redact(t). Fuzzed across arbitrary unicode input.
    """
    from taskq_plus.observability.audit import redact_text

    once = redact_text(text)
    twice = redact_text(once)
    assert once == twice, (
        f"P8-redaction-idempotent violated: redact(redact({text!r})) "
        f"= {twice!r}, but redact({text!r}) = {once!r}"
    )