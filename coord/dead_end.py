"""The driver's dead-end predicate (#2019).

``coord drive`` could not tell **"still working"** apart from **"finished in
a state I cannot act on."**  Both rendered as ``no state change in N m`` and
the second one looped forever — a counter incrementing against an event that
can never occur, holding a tmux session, a queue slot and (since #1972) an
entire repo's capacity lane.  The live case this module is named for burned
**140 minutes** on claude-coordinator#1956 with ``review=done/-`` on screen
the whole time.

WHAT THIS MODULE IS
-------------------
One pure function, :func:`detect_dead_end`, over the driver's already-computed
:class:`coord.drive_state.IssueState`.  It answers a single question:

    Is this row **terminal AND unactionable** — i.e. is there no board
    transition left that any amount of polling could produce?

It is **not** a stall detector.  Per #2019 ask 4, elapsed time is deliberately
NOT an input: a healthy long-running Work stage is legitimately quiet for
hours, and a dead end is knowable at minute zero.  The only clock-shaped
guard here is the hard precondition ``active_count == 0`` — if anything at all
is running on the fleet, this function returns ``None``, however long it has
been running.  That guard is what makes a false positive on a healthy stage
structurally impossible rather than merely unlikely.

WHY THE 140 MINUTES HAPPENED (the field-mismatch bug)
-----------------------------------------------------
``coord.drive._decide_review`` has had a ``_die`` for "review finished with no
verdict" since the original bash port — but it keys on
``state.work_review_state``, the **work** row's projected ``review_state``.
The incident's board showed ``review=done/-``, which is
``state.review_status`` — the **review assignment's own** status.  On #1956
the review row reached ``status="done"`` while the work row's ``review_state``
was never advanced (advancing it is exactly what recording the verdict does),
so the die never fired and ``_decide_review`` fell through to a bare
``_wait()``.  Two readings of "the review is done", one checked, one not.
:data:`_TERMINAL_REVIEW_STATUSES` closes that by keying on the review row.

WHAT IS DELIBERATELY *NOT* COVERED
----------------------------------
* **A Test stage that was never dispatched, with no policy saying so**
  (``test_state=""``, no smoke row, no ``test-mode:*`` label) — from board
  state alone that is indistinguishable from "the daemon will dispatch it on
  the next tick", and the rest of the dispatcher's refusal set
  (``coord.smoke.dispatch_smoke`` returns ``None`` for a superseded row, a
  capability-rule miss on a non-``work`` type, a missing smoke command, …) is
  not re-derivable here without duplicating it.  Guessing would escalate
  healthy rows.  Two variants ARE covered, both because something on the board
  positively states that no dispatch is coming: ``test_state="blocked"``, the
  marker the dispatcher leaves when it gives up (#1672, shape 2), and
  ``test-mode:smoke``, the per-issue POLICY that switches the headless Test
  stage off entirely (#685/#2024, shape 3).
* **A ``done`` smoke row whose verdict has not landed yet** — #1605 already
  established that this has an expected, bounded propagation lag and is not a
  defect.  Only the contradictions #1605 itself detects are terminal.
* **A zero-commit advisory / a refused pre-dispatch guard** — already
  escalated correctly by ``coord.drive._decide_advisory``, the "finished with
  no branch" die, and #1844's ``EXIT_DISPATCH_REFUSED``.  vimcode#634 proved
  that path works; this module deliberately does not touch it.

The registry below is meant to grow one *proven* shape at a time.  A shape
belongs here only when the board state alone makes the row unactionable — not
when it merely looks quiet.
"""

from __future__ import annotations

from dataclasses import dataclass

# A review assignment that reached one of these is FINISHED — no worker is
# coming back to add a verdict.  `"failed"` is deliberately absent: #1584
# owns it with a bounded `coord review` re-dispatch, and a dead-end verdict
# there would steal a retry that genuinely can succeed.
_TERMINAL_REVIEW_STATUSES = frozenset({"done", "cancelled"})


@dataclass(frozen=True)
class DeadEnd:
    """One terminal-and-unactionable board shape, fully described.

    ``reason`` names the SPECIFIC dead end (#2019 ask 3) — "review reached
    done with no verdict" rather than "no state change in 140.558m" — and
    ``recovery`` is the command an operator can paste.  Both end up on the
    ``coord escalate record`` row, in the drive-queue entry's ``last_reason``,
    and in the GitHub escalation comment, so all three surfaces say the same
    thing.
    """

    kind: str
    """Stable slug for the shape, e.g. ``review_terminal_no_verdict``."""

    stage: str
    """Pipeline stage the dead end sits in — ``coord escalate record --stage``."""

    reason: str
    """Why no poll can change this.  One paragraph, operator-facing."""

    recovery: str
    """The command that unblocks it, ready to paste."""

    assignment_id: str = ""
    """The row an operator should look at first (``""`` when unknown)."""

    gates: tuple[tuple[str, str], ...] = ()
    """Observed gate readings, ``(key, value)`` — mirrors ``_escalate_merge``."""


def _gates(state) -> tuple[tuple[str, str], ...]:
    """The board readings worth recording on any dead end.

    Same shape as ``coord.drive._escalate_merge``'s ``gate_pairs`` so the two
    escalation kinds render identically in ``coord escalate list``.
    """
    return (
        ("work_status", state.work_status or "(empty)"),
        ("test_state", state.work_test_state or "(none)"),
        # #2024: the per-issue Test-stage POLICY is a reading in its own right
        # — for shape 3 it is the entire proof, and on every other shape it is
        # the difference between "no verdict yet" and "no verdict, ever".
        ("issue_test_mode", state.issue_test_mode or "(none)"),
        ("review_status", state.review_status or "(none)"),
        ("review_verdict", state.review_verdict or "(none)"),
        ("active", str(state.active_count)),
    )


def detect_dead_end(state, *, can_waive_test_gate: bool = False) -> DeadEnd | None:
    """The predicate: ``None`` when the row can still move on its own.

    *state* is a :class:`coord.drive_state.IssueState` (untyped here purely to
    keep this module import-light — it reads a handful of attributes and
    nothing else).

    *can_waive_test_gate* (#2024) is the caller saying "I still have a Test-
    stage move of my own" — today that is ``coord drive --skip-test``, whose
    ``_decide_test`` records ``skipped`` for a verdict-less row. Shape 3 below
    is exactly the shape that flag exists to unblock, so with it set that shape
    is not a dead end: the driver must be allowed to use the move the operator
    explicitly asked for rather than escalate past it.

    Pure, cheap, and safe to call on every poll: no I/O, no clock, no
    counters.  The caller (``coord.drive.decide``) turns a non-``None`` result
    into an ``EXIT`` action carrying ``coord.drive.EXIT_DEAD_END``.
    """
    # #1672's "the fleet cannot route this Test stage" marker.  IMPORTED (not
    # mirrored as a literal) so the two can never drift: `dispatch_smoke`
    # bails out on this exact value, and its refusal to re-probe is precisely
    # why a driver polling against it can never make progress.  Deferred to
    # call time purely to keep THIS module importable on its own without
    # dragging in `coord.smoke`'s `httpx` dependency — `coord.drive` already
    # pulls httpx in via `coord.usage_limits`, so nothing is saved there, but
    # a predicate this small should stay cheap to import and test in
    # isolation.
    from coord.smoke import TEST_STATE_BLOCKED  # noqa: PLC0415
    # THE conservative guard, and it stays first.  #2019 acceptance: "a
    # genuinely long-running work stage (active=1) does NOT escalate, however
    # long it runs."  Anything in flight — work, test, review, a fix round,
    # the acceptance author — makes every question below premature.
    if state.active_count > 0:
        return None

    # ── shape 1: the review row is terminal and carries no verdict ──────────
    # claude-coordinator#1956, 2026-08-08.  The driver can neither dispatch a
    # fix (there is no `request-changes` to fix) nor proceed to merge (there
    # is no `approve` to merge on), and the row that would have supplied
    # either is finished.  Nothing about waiting changes that.
    if (
        state.review_aid
        and state.review_status in _TERMINAL_REVIEW_STATUSES
        and not state.review_verdict
        # Belt-and-braces: an actionable failed test outranks a stale review
        # on the same work row — let `_decide_test`'s bounded fix loop have
        # it rather than dead-ending a row that still has a live move.
        and state.work_test_state != "failed"
    ):
        if state.review_status == "cancelled":
            return DeadEnd(
                kind="review_cancelled_no_verdict",
                stage="review",
                reason=(
                    f"review {state.review_aid} was CANCELLED before recording "
                    "a verdict — no verdict is coming, and this driver has "
                    "nothing to approve, fix, or merge on. Terminal on the "
                    "board; polling cannot change it (#2019)."
                ),
                recovery=f"coord review {state.work_aid}",
                assignment_id=state.review_aid,
                gates=_gates(state),
            )
        # status == "done": the session finalised CLEANLY.  Saying so matters
        # — #2019 acceptance requires this text to distinguish an
        # END_REVIEW-without-verdict from a crashed session, and to stop
        # citing the CLOSED #812 (which was about INTERACTIVE reviews that
        # never started) at a headless review that ran to completion.  A
        # review worker that actually died lands `status="failed"`, which is
        # `_decide_review`'s #1584 bounded-retry arm, not this one.
        #
        # #812 is deliberately absent from the text below — even as a
        # disclaimer.  An operator who greps the reason for an issue number
        # to open must never land on a closed one, and "not #812" reads as
        # "#812" to every mechanical reader (and to a tired one at 2am).  The
        # correction lives here, in the source, where it belongs.
        return DeadEnd(
            kind="review_terminal_no_verdict",
            stage="review",
            reason=(
                f"review {state.review_aid} reached status=done carrying NO "
                "verdict. The review session finalised cleanly — a crashed, "
                "killed or never-started session lands status='failed' (which "
                "this driver retries), so this is the END_REVIEW-without-"
                "verdict class: the reviewer finished and its REVIEW_VERDICT "
                "header was never emitted or never parsed (#1956, "
                "coord.review.detect_end_review_without_verdict). The verdict "
                "is very likely already sitting in the transcript. With no "
                "verdict there is no fix to dispatch and no approval to merge "
                "on, and the review row is terminal — no number of polls "
                "changes that (#2019)."
            ),
            # Straight out of docs/OPERATING_GOTCHAS.md's "Recovery — do NOT
            # re-dispatch" block: re-running the review costs a full cycle to
            # re-derive a conclusion already in the log, and the drop
            # reproduces at a documented ~14% rate (#873).
            recovery=(
                f"coord report-result --assignment {state.review_aid} "
                "--status done --verdict <approve|request-changes> "
                "--verdict-source recovered --verdict-reason 'REVIEW_VERDICT "
                "header missing, recovered from transcript (#1956)' "
                "--body-file <extracted-review.md>"
            ),
            assignment_id=state.review_aid,
            gates=_gates(state),
        )

    # ── shape 2: the Test stage is BLOCKED (#1672/#2272) ───────────────────
    # `dispatch_smoke` stamps `test_state="blocked"` when no capability-
    # matched machine could run the suite, and then REFUSES to re-probe on
    # every tick — deliberately, that spin is what #1672 closed.  So the
    # driver polling for a verdict is polling for something the dispatcher
    # has already decided not to produce.  vimcode#635's shape, in the one
    # variant the board makes provable.
    #
    # #2272 parks the SAME value for a second, unrelated cause: N Test-stage
    # legs in a row finished without printing a `SMOKE:` marker and the retry
    # budget ran out.  Both are "no verdict is coming", which is why they
    # share a state — but the recovery is completely different (fix the fleet
    # vs. find out why the worker went mute, usually the 600s Bash ceiling),
    # so they must not share a REASON.  Telling the operator "no capability-
    # matched machine" about a row whose machines were fine all along is the
    # symptom-not-cause failure #2235 exists to measure, and it is one
    # `mute_smoke_legs` call away from being avoided.
    if state.work_test_state == TEST_STATE_BLOCKED:
        from coord.smoke import (  # noqa: PLC0415
            abandoned_background_smoke_legs,
            mute_smoke_legs,
        )

        # #3737: a THIRD, unrelated cause parks the same value — N Test-stage
        # legs in a row ended their one-shot turn with the smoke command
        # still running in the background (no verdict marker, because the
        # worker never came back to print one). Checked before the plain
        # mute-leg tally since the two markers never co-occur in the same
        # `test_reason`, but keeping the order explicit avoids depending on
        # that.
        abandoned_legs = abandoned_background_smoke_legs(state.work_test_reason)
        mute_legs = mute_smoke_legs(state.work_test_reason)
        if abandoned_legs:
            cause = (
                f"{abandoned_legs} Test-stage leg(s) in a row ended their "
                "turn with the smoke command still running in the "
                "background instead of to completion, exhausting the "
                "#3737 retry budget (a separate budget from #2272's mute-"
                "leg one, so this is not a report of a mute suite). Nothing "
                "is known to be wrong with the BRANCH or the suite's "
                "result — the worker backgrounded the smoke command past "
                "its own turn and never reported an outcome. Re-dispatch "
                "with an instruction to run the smoke command "
                "SYNCHRONOUSLY to completion rather than backgrounding it."
            )
        elif mute_legs:
            cause = (
                f"{mute_legs} Test-stage leg(s) in a row finished without "
                "printing a `SMOKE:` verdict marker, exhausting the retry "
                "budget, so coord.notify parked the row rather than "
                "re-dispatching a cause that had already repeated identically "
                "every lap (#2272). Nothing is known to be wrong with the "
                "BRANCH — the commonest cause is the smoke command exceeding "
                "the worker's 600s Bash ceiling, which backgrounds the run "
                "and leaves the session with no exit status to report. Check "
                "the last Test-stage transcript for a backgrounded command "
                "before you blame the diff."
            )
        else:
            cause = (
                "coord.smoke.dispatch_smoke found no capability-matched "
                "machine and recorded 'blocked' rather than re-probing a "
                "broken fleet on every tick (#1672). It will not try again "
                "on its own, so waiting for a verdict here waits forever "
                "(#2019). Fix the fleet (or the capability rules), then "
                "clear the marker."
            )
        return DeadEnd(
            kind="test_stage_blocked",
            stage="test",
            reason=(
                f"the Test stage for {state.work_aid} is BLOCKED: "
                f"{state.work_test_reason or 'no reason recorded'} — {cause}"
            ),
            recovery=(
                f"coord diagnose {state.repo} {state.issue} --stage test --reset"
            ),
            assignment_id=state.work_aid,
            gates=_gates(state),
        )

    # ── shape 3: the Test stage is HUMAN-ATTENDED by policy (#685/#2024) ────
    # `test-mode:smoke` means "the headless Test stage does not run for this
    # issue; the TUI offers an interactive smoke agent instead" —
    # `dispatch_pending_smoke` skips the row unconditionally, on every tick,
    # forever. Review dispatch is meanwhile gated on a passed/skipped verdict
    # for THIS work row (`pipeline.test_precedes_review()`, honoured by both
    # `dispatch_pending_reviews` and `auto_loop.run_for_fix_transition`), so a
    # completed row with no verdict of its own cannot advance: one component
    # requires a verdict and, by policy, no component will produce one.
    #
    # Why this bites `--fix-of` rounds specifically (#2024): a fix round is a
    # NEW work row on the SAME branch, and it carries its own empty
    # `test_state`. The parent's verdict satisfies the branch-scoped MERGE
    # gate (`coord gates` reads `test: passed` off it — see the note
    # `coord.gates.build_gate_report` now emits), which is why the stall reads
    # as "slow" rather than "blocked". Round 0 gets attended because a human is
    # watching the first pass; rounds 1..N complete unattended at 3am and sit
    # there. Observed twice on JDonaghy/vimcode#635 (2026-08-08): 25 minutes,
    # then 160 minutes on the same issue, each cleared within minutes of an
    # operator running `coord test <fix_aid> --passed` by hand.
    #
    # Deliberately narrow, so it can never fire on a healthy row:
    #   * `active_count == 0` (above) — an interactive smoke session IS a live
    #     board row, so an attended Test stage in progress never reaches here.
    #   * `smoke_aid` empty — a Test-stage child dispatched for THIS row (by a
    #     human, `--smoke-of`, or an earlier auto pass) means the stage did
    #     happen; #1605 owns a `done` smoke whose verdict hasn't landed yet.
    #   * `work_test_state` empty — not "running" (#1395's transient marker,
    #     `_decide_test` waits on it), not a terminal verdict, not "blocked"
    #     (shape 2 above owns that, and reads better).
    if (
        not can_waive_test_gate
        and state.issue_test_mode == "smoke"
        and state.work_status == "done"
        and state.work_aid
        and not state.work_test_state
        and not state.smoke_aid
    ):
        return DeadEnd(
            kind="test_stage_human_attended",
            stage="test",
            reason=(
                f"work {state.work_aid} finished with NO Test verdict, and "
                f"{state.repo}#{state.issue} is labelled `test-mode:smoke` — "
                "the per-issue policy (#685) that switches the HEADLESS Test "
                "stage off: coord.smoke.dispatch_pending_smoke skips this "
                "issue on every tick by design, so no smoke assignment is "
                "coming. Review dispatch is meanwhile held until THIS row "
                "carries a passed/skipped verdict "
                "(pipeline.test_precedes_review), so nothing can advance "
                "without a human. This is the #2024 shape: a --fix-of round "
                "is a new work row with its own empty test_state, and the "
                "parent's verdict satisfies only the branch-scoped merge gate "
                "(`coord gates` reads `test: passed` off the parent row) — "
                "which is why this reads as slow rather than blocked (#2019)."
            ),
            # Two doors, cheapest first: record the verdict directly, or run
            # the attended Test stage the label asked for. Both are real; the
            # operator picks by whether the suite actually needs running.
            recovery=(
                f"coord test {state.work_aid} --passed   # or --skipped "
                "--reason '<why>'; to actually run it, `coord assign "
                f"<machine> {state.repo} {state.issue} --smoke-of "
                f"{state.work_aid} --interactive`"
            ),
            assignment_id=state.work_aid,
            gates=_gates(state),
        )

    return None
