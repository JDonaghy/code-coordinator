"""#3789: regression guard for the pause-store fixture's self-recursion hang.

The autouse ``_no_real_pause_store`` fixture (tests/conftest.py) wraps
``coord.machine_pause._state_path`` with a ``_guarded`` closure so a test can
never touch the operator's real ``~/.coord/paused_machines.json``. ``_guarded``
includes a ``$HOME``-probe that re-invokes the resolver to decide whether
``$HOME`` is load-bearing.

The bug (coord#3777 / #3785): the fixture captured its ``original`` resolver by
re-reading ``machine_pause._state_path`` **at fixture-setup time**. In the
full-suite ``populated-home`` context the attribute was already the ``_guarded``
wrapper, so ``original()`` inside the probe called ``_guarded`` again and
recursed without bound — each level re-probing — and the suite hung until the
CI runner's 6h ceiling (no per-job timeout at the time).

The fix anchors ``original`` on the GENUINE resolver captured once at
collection (``conftest._GENUINE_PAUSE_STATE_PATH``), before any patching, so
``_guarded`` can never route back into itself. These tests fail fast on a
revert rather than re-introducing the hang (which no per-test assertion could
catch without a timeout, and which only reproduces on the clean CI runner).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import tests.conftest as conftest
from coord import machine_pause


def test_guard_anchors_on_the_genuine_resolver_not_the_live_attribute() -> None:
    """The captured genuine resolver is the real function, and the live
    attribute is the installed guard — so resolving through the genuine one
    cannot re-enter the guard."""
    # Collected before any patching: the real resolver, callable, returns a Path.
    genuine = conftest._GENUINE_PAUSE_STATE_PATH
    assert callable(genuine)
    assert isinstance(genuine(), Path)

    # The autouse fixture has installed its guard as the live attribute, so the
    # live attribute is NOT the genuine resolver. If the fixture had captured
    # `original` from this live attribute (the bug), `_guarded` would call
    # `_guarded`.
    assert machine_pause._state_path is not genuine


def test_fixture_does_not_recapture_the_live_state_path_attribute() -> None:
    """Source guard: the fixture must bind `original` to the genuine resolver
    captured at collection, never re-read `machine_pause._state_path` (which may
    already be the guard). This is the exact anti-pattern that caused the hang."""
    src = inspect.getsource(conftest._no_real_pause_store)
    assert "original = _GENUINE_PAUSE_STATE_PATH" in src, (
        "pause-store guard must anchor on the collection-time genuine resolver"
    )
    assert "original = machine_pause._state_path" not in src, (
        "re-reading the live _state_path attribute reintroduces the #3789 "
        "self-recursion hang under populated-home"
    )
