"""Regression: the autouse `_no_real_pause_store` guard must never chain into
another guard.

A guard that wrapped a *second* guard re-entered it through `original()` on
every call, and with each layer doing a symlink-following `Path.resolve()` the
chain spun until the full suite hung (reached ~15% in, victim test varying by
run). `_unwrap_pause_store_guard` breaks the chain by always resolving back to
the genuine `_state_path`.
"""

from __future__ import annotations

from coord import machine_pause

from .conftest import _unwrap_pause_store_guard


def test_unwrap_resolves_a_stacked_guard_back_to_the_real_function() -> None:
    def real() -> str:
        return "real"

    def guard_one() -> str:  # pragma: no cover - never called
        return "one"

    guard_one._pause_store_guard = True
    guard_one._unguarded_state_path = real

    def guard_two() -> str:  # pragma: no cover - never called
        return "two"

    guard_two._pause_store_guard = True
    guard_two._unguarded_state_path = guard_one

    # However many guards are stacked, unwrap lands on the real function.
    assert _unwrap_pause_store_guard(guard_two) is real
    assert _unwrap_pause_store_guard(guard_one) is real
    assert _unwrap_pause_store_guard(real) is real


def test_live_guard_wraps_the_real_function_not_another_guard() -> None:
    # The autouse fixture has installed its guard for this very test.
    installed = machine_pause._state_path
    assert getattr(installed, "_pause_store_guard", False)
    # Its wrapped original must itself be unguarded — a guard-on-guard is the
    # chain that hung the suite.
    assert not getattr(installed._unguarded_state_path, "_pause_store_guard", False)
    # Resolving terminates (would recurse/hang if the chain were present).
    assert machine_pause._state_path() is not None
