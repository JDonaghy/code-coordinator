"""Tests for machine-fault classification and tracking (#3367).

#3367's evidence: a dead OAuth credential on `precision` produced four
identical dispatches — each "completed in 0.1s, 1 turns, $0.00" with
`'Failed to authenticate: OAuth session expired and could not be
refreshed'` — and every one was charged to the issue's retry budget instead
of the machine's health. These tests cover the two independent detection
signals, the fail-closed default, the consecutive-fault counter, and the
auto-pause transition.
"""

from __future__ import annotations

import pytest

from coord import machine_pause
from coord.machine_fault import (
    AUTO_PAUSE_THRESHOLD,
    MachineFaultClassification,
    classify_machine_fault,
    clear_fault,
    consecutive_faults,
    describe,
    is_auth_failure_text,
    is_instant_zero_cost_failure,
    maybe_auto_pause,
    record_fault,
)

# ── classify_machine_fault: the auth-text signal ────────────────────────────


def test_auth_failure_text_is_a_machine_fault() -> None:
    result = classify_machine_fault(
        failure_reason=(
            "Failed to authenticate: OAuth session expired and could not "
            "be refreshed"
        ),
    )
    assert result.is_machine_fault
    assert result.signal == "auth_failure"
    assert "auth" in result.reason


@pytest.mark.parametrize(
    "text",
    [
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "invalid_grant: token has been expired or revoked",
        "invalid_client: client authentication failed",
    ],
)
def test_is_auth_failure_text_matches_known_wire_tokens(text: str) -> None:
    assert is_auth_failure_text(text)


def test_is_auth_failure_text_false_for_none_and_unrelated_text() -> None:
    assert not is_auth_failure_text(None)
    assert not is_auth_failure_text("")
    assert not is_auth_failure_text("the test suite failed: AssertionError")


def test_terminal_reason_scanned_unconditionally() -> None:
    """Coordinator-authored `terminal_reason` is always scanned, mirroring
    `coord.failure_class`'s own convention — no `is_error` gate needed."""
    result = classify_machine_fault(terminal_reason="OAuth session expired")
    assert result.is_machine_fault


def test_result_text_only_scanned_when_is_error() -> None:
    """Worker-authored prose that merely *discusses* auth must not
    misclassify unless the run actually errored — same guard
    `coord.failure_class.classify_failure` applies to its own `result_text`.
    """
    discussing_auth = "I investigated the OAuth session expired bug in prod"
    not_an_error = classify_machine_fault(
        result_text=discussing_auth, is_error=False
    )
    assert not not_an_error.is_machine_fault

    genuinely_errored = classify_machine_fault(
        result_text="Failed to authenticate: OAuth session expired",
        is_error=True,
    )
    assert genuinely_errored.is_machine_fault


# ── classify_machine_fault: the instant-zero-cost shape signal ─────────────


@pytest.mark.parametrize(
    ("num_turns", "cost_usd"),
    [(0, 0.0), (1, 0.0), (1, None), (0, None)],
)
def test_instant_zero_cost_shape_is_a_machine_fault(
    num_turns: int, cost_usd: float | None
) -> None:
    result = classify_machine_fault(
        failure_reason="some opaque worker crash with no known signature",
        num_turns=num_turns,
        cost_usd=cost_usd,
    )
    assert result.is_machine_fault
    assert result.signal == "instant_zero_cost"


@pytest.mark.parametrize(
    ("num_turns", "cost_usd"),
    [(5, 0.0), (1, 0.02), (12, 1.5)],
)
def test_real_attempt_is_not_a_machine_fault(
    num_turns: int, cost_usd: float
) -> None:
    """A leg that spent real turns or real money made a genuine attempt —
    must fall through to the ordinary work-failure retry path, never dodge
    its own budget."""
    result = classify_machine_fault(
        failure_reason="assertion failed in test_foo",
        num_turns=num_turns,
        cost_usd=cost_usd,
    )
    assert not result.is_machine_fault


def test_unmeasured_turns_is_not_evidence_either_way() -> None:
    """`num_turns=None` (never measured) must not be treated as "zero
    turns" — only `cost_usd=None` degrades permissively."""
    assert not is_instant_zero_cost_failure(num_turns=None, cost_usd=0.0)


def test_no_evidence_is_not_a_machine_fault() -> None:
    """Fail-closed default, mirroring `coord.failure_class.classify_failure`:
    no signal supplied at all classifies as NOT a machine fault, so a caller
    with nothing to go on keeps using the pre-existing work-failure path."""
    result = classify_machine_fault()
    assert not result.is_machine_fault
    assert isinstance(result, MachineFaultClassification)


def test_auth_text_takes_precedence_over_shape() -> None:
    """When both signals are present, the more specific (text) reason wins
    — a caller reading `.reason` gets the named cause, not the generic one.
    """
    result = classify_machine_fault(
        failure_reason="Failed to authenticate: OAuth session expired",
        num_turns=1,
        cost_usd=0.0,
    )
    assert result.signal == "auth_failure"


# ── consecutive-fault tracking + auto-pause ─────────────────────────────────


def test_record_fault_increments_and_persists_reason() -> None:
    assert consecutive_faults("precision") == 0
    assert record_fault("precision", "machine fault (auth): dead OAuth") == 1
    assert record_fault("precision", "machine fault (auth): dead OAuth") == 2
    assert consecutive_faults("precision") == 2
    assert describe("precision") == (
        "2 consecutive machine faults — last: machine fault (auth): dead OAuth"
    )


def test_clear_fault_resets_to_zero() -> None:
    record_fault("precision", "machine fault (auth): dead OAuth")
    record_fault("precision", "machine fault (auth): dead OAuth")
    clear_fault("precision")
    assert consecutive_faults("precision") == 0
    assert describe("precision") is None


def test_describe_none_for_machine_with_no_fault() -> None:
    assert describe("macmini") is None


def test_faults_are_tracked_independently_per_machine() -> None:
    record_fault("precision", "machine fault (auth): dead OAuth")
    record_fault("precision", "machine fault (auth): dead OAuth")
    assert consecutive_faults("precision") == 2
    assert consecutive_faults("macmini") == 0


def test_maybe_auto_pause_below_threshold_does_not_pause() -> None:
    for _ in range(AUTO_PAUSE_THRESHOLD - 1):
        record_fault("precision", "machine fault (auth): dead OAuth")
    just_paused, consecutive = maybe_auto_pause("precision")
    assert not just_paused
    assert consecutive == AUTO_PAUSE_THRESHOLD - 1
    assert "precision" not in machine_pause.local_paused_set()


def test_maybe_auto_pause_at_threshold_pauses_the_machine() -> None:
    for _ in range(AUTO_PAUSE_THRESHOLD):
        record_fault("precision", "machine fault (auth): dead OAuth")
    just_paused, consecutive = maybe_auto_pause("precision")
    assert just_paused
    assert consecutive == AUTO_PAUSE_THRESHOLD
    assert "precision" in machine_pause.local_paused_set()


def test_maybe_auto_pause_does_not_repeat_the_transition() -> None:
    """A caller polling this on every retry must only see `just_paused=True`
    once — `coord.machine_pause.pause()` already reports "no-op" for an
    already-paused machine, so this must not re-report a fresh pause."""
    for _ in range(AUTO_PAUSE_THRESHOLD):
        record_fault("precision", "machine fault (auth): dead OAuth")
    first_pause, _ = maybe_auto_pause("precision")
    assert first_pause

    record_fault("precision", "machine fault (auth): dead OAuth")
    second_pause, consecutive = maybe_auto_pause("precision")
    assert not second_pause
    assert consecutive == AUTO_PAUSE_THRESHOLD + 1


def test_a_genuinely_broken_machine_gets_auto_paused_end_to_end() -> None:
    """#3367's own repro, mechanised: four identical auth-failure
    classifications on the same machine must pull it out of the routing
    pool no later than the fourth — matching the issue's own evidence that
    a human intervened after exactly four."""
    machine = "precision"
    for _ in range(4):
        fault = classify_machine_fault(
            failure_reason=(
                "Failed to authenticate: OAuth session expired and could "
                "not be refreshed"
            ),
            num_turns=1,
            cost_usd=0.0,
        )
        assert fault.is_machine_fault
        record_fault(machine, fault.reason)
        maybe_auto_pause(machine)

    assert machine in machine_pause.local_paused_set()
