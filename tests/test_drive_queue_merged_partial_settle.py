"""#4218: a `merged-partial` drive-queue row self-heals to `done` once its
issue closes — board-doctor follow-through filing the remainder and closing
the original, or an operator closing it by hand.

Mirrors the #2055/#3368 `parked`/`blocked`/`failed` landed re-check in
`tests/test_drive_queue.py` (same `plan_tick`-level shape), extended to the
one state that sweep always skipped: a `merged-partial` row is ALREADY
`facts.landed` (its own PR merged — that's how it got this state), so the
question this sweep asks for it is narrower than "has it landed" — it is
"has its issue since closed" — and must never fire on the generic `landed`
reading alone, or every still-open `merged-partial` row would get its
reason overwritten with a misleading "done — issue already merged ..."
line every tick even though it stays `merged-partial`.
"""

from __future__ import annotations

from coord.drive_queue import (
    STATE_DONE,
    STATE_MERGED_PARTIAL,
    BoardView,
    IssueFacts,
    QueueEntry,
    entry_key,
    plan_tick,
)

REPO = "claude-coordinator"


def entry(issue: int, **kw) -> QueueEntry:
    base: dict = {"repo": REPO, "issue": issue, "position": issue}
    base.update(kw)
    return QueueEntry(**base)


def board(
    *,
    merged: tuple[int, ...] = (),
    closed: tuple[int, ...] = (),
    partial: tuple[int, ...] = (),
    sessions: tuple[int, ...] = (),
) -> BoardView:
    facts: dict[str, IssueFacts] = {}
    for issue in {*merged, *closed, *partial}:
        facts[entry_key(REPO, issue)] = IssueFacts(
            known=True,
            issue_state="closed" if issue in closed else "open",
            merged=issue in merged,
            merge_resolution="partial" if issue in partial else "resolved",
        )
    return BoardView(
        issues=facts,
        live_sessions=frozenset(entry_key(REPO, i) for i in sessions),
    )


def test_a_merged_partial_row_whose_issue_closes_settles_to_done():
    entries = [
        entry(4218, position=3, state=STATE_MERGED_PARTIAL, attempts=0)
    ]
    plan = plan_tick(
        entries, board(merged=(4218,), closed=(4218,), partial=(4218,)),
        capacity=1,
    )
    assert len(plan.reconciles) == 1
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE
    assert "closed" in reconcile.updates["last_reason"]
    assert plan.launch is None


def test_a_merged_partial_row_whose_issue_is_still_open_is_left_untouched():
    """The `facts.landed` reading alone is already true for every
    `merged-partial` row (its own PR merged) — this pins that the sweep
    does NOT fire off that alone, so a row whose follow-through has not
    (yet) closed the issue keeps its existing reason instead of being
    overwritten with a stale "done" line every tick."""
    entries = [
        entry(
            4219, position=3, state=STATE_MERGED_PARTIAL, attempts=0,
            last_reason="merged, issue intentionally left open (partial)",
        )
    ]
    plan = plan_tick(
        entries, board(merged=(4219,), partial=(4219,)), capacity=1,
    )
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_merged_partial_row_whose_followthrough_was_declined_shows_that_reason():
    """The decline reason an operator or board-doctor already recorded on
    the row (e.g. a declared tranche) is NOT the generic merge line this
    sweep would otherwise write — since the issue stays open, the sweep
    must not touch the row at all, so that reason keeps showing."""
    decline_reason = (
        "follow-through declined: epic declares a tranche convention — "
        "the remainder is tracked by the epic itself, not a new follow-up"
    )
    entries = [
        entry(
            4220, position=3, state=STATE_MERGED_PARTIAL, attempts=0,
            last_reason=decline_reason,
        )
    ]
    plan = plan_tick(
        entries, board(merged=(4220,), partial=(4220,)), capacity=1,
    )
    assert plan.reconciles == ()
    assert entries[0].last_reason == decline_reason


def test_a_merged_partial_row_settles_via_a_live_closed_reading_too():
    """Same settlement, reached through `live_prereq_terminal` instead of
    the cached board — mirrors #3368's live-recovery half of the
    `parked`/`blocked`/`failed` sweep, for the one caller that threads this
    mapping in (a cache lag must not stall the self-heal any longer than
    the `parked`/`blocked`/`failed` rows it already covers)."""
    entries = [
        entry(4221, position=3, state=STATE_MERGED_PARTIAL, attempts=0)
    ]
    plan = plan_tick(
        entries, board(merged=(4221,), partial=(4221,)), capacity=1,
        live_prereq_terminal={entry_key(REPO, 4221): True},
    )
    assert len(plan.reconciles) == 1
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE
    assert "live re-check" in reconcile.updates["last_reason"]


def test_a_merged_partial_dependent_resolves_once_the_prereq_settles_to_done():
    """The dependent-facing half of the acceptance bar: an `after=` edge
    naming a `merged-partial` row whose issue has closed must resolve as
    landed on the SAME tick the row itself settles — `_resolve_prereqs`'s
    existing `STATE_DONE_LIKE` check already covers a `merged-partial` dep
    whose issue is closed; this pins that it keeps doing so once the row
    settles to plain `done` via this sweep."""
    prereq = entry(4222, position=1, state=STATE_MERGED_PARTIAL, attempts=0)
    dependent = entry(
        4223, position=2, state="waiting", attempts=0,
        after=(entry_key(REPO, 4222),),
    )
    plan = plan_tick(
        [prereq, dependent],
        board(merged=(4222,), closed=(4222,), partial=(4222,)),
        capacity=1,
    )
    prereq_reconcile = next(
        r for r in plan.reconciles if r.key == entry_key(REPO, 4222)
    )
    assert prereq_reconcile.updates["state"] == STATE_DONE
    # The dependent must not be told to wait on (or escalated over) an
    # edge that has, this same tick, actually landed.
    assert plan.launch is not None
    assert plan.launch.key == entry_key(REPO, 4223)
