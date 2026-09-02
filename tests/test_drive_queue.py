"""Unit tests for coord/drive_queue.py — the pure half of the drive queue (#1754).

The CLI-level suite (tests/test_cli_drive_queue.py) is the acceptance bar; this
file pins the decisions themselves, the same way tests/test_drive.py pins
``coord.drive.decide`` rather than ``Driver.run``. The two rules that get the
most attention here are the ones that caused real incidents:

* capacity counted from BOARD state, so a drive whose observer hit its
  ``EXIT_DEADLINE`` (#1660) still occupies a slot (2026-08-01);
* unsatisfiable vs merely-unsatisfied, so a pre-req that will never land
  escalates instead of deferring forever;
* the startup grace window (#1794), so a tick firing seconds after a launch
  cannot declare a still-starting drive dead (2026-08-03).
"""

from __future__ import annotations

import pytest

from coord.drive_queue import (
    DEFAULT_MAX_ATTEMPTS,
    DRIVE_STARTUP_GRACE_SECONDS,
    EMPTY_BRANCH_MAX_ATTEMPTS,
    HOLD_ARMED,
    HOLD_FIRED,
    HOLD_RELEASED,
    HOLD_SCOPE_ENTRY,
    HOLD_SCOPE_FLEET,
    PARK_STALE_SECONDS,
    STATE_BLOCKED,
    STATE_DONE,
    STATE_FAILED,
    STATE_PARKED,
    STATE_RUNNING,
    STATE_WAITING,
    UNREACHABLE_WAIT_MIN_DEFERRALS,
    BoardView,
    IssueFacts,
    ProbeResult,
    QueueEntry,
    QueueError,
    add_preflight_notice,
    build_board_view,
    detect_unreachable_waits,
    compute_leg_counts,
    entries_from_rows,
    entry_key,
    find_cycle,
    is_empty_branch_death_reason,
    is_merge_gate_block_reason,
    merge_gate_remedy_command,
    merge_plan_inspect_command,
    parse_after_spec,
    parse_key,
    plan_tick,
    render_plan,
    unreachable_wait_alert,
    validate_enqueue,
)
from coord.models import MERGE_LANDED_MARKER

REPO = "claude-coordinator"

# A fixed wall clock for the #1794 startup-window tests. `plan_tick` takes
# `now` as a parameter precisely so these need no monkeypatching and no real
# sleeping — the module still never reads the clock itself.
NOW = 1_800_000_000.0


def entry(issue: int, **kw) -> QueueEntry:
    base: dict = {"repo": REPO, "issue": issue, "position": issue}
    base.update(kw)
    return QueueEntry(**base)


def board(
    *,
    merged: tuple[int, ...] = (),
    closed: tuple[int, ...] = (),
    open_: tuple[int, ...] = (),
    active: tuple[int, ...] = (),
    sessions: tuple[int, ...] = (),
    ci_pending: tuple[int, ...] = (),
    ci_pending_live: tuple[int, ...] = (),
) -> BoardView:
    facts: dict[str, IssueFacts] = {}
    for issue in {*merged, *closed, *open_, *active, *ci_pending, *ci_pending_live}:
        facts[entry_key(REPO, issue)] = IssueFacts(
            known=True,
            issue_state=(
                "closed" if issue in closed else ("open" if issue in open_ else "")
            ),
            merged=issue in merged,
            active_work=issue in active,
            # #1891: the board's current read of this issue's merge gate —
            # nothing stronger than "CI checks have not reported yet".
            merge_ci_pending=issue in ci_pending or issue in ci_pending_live,
            # #2158: *ci_pending* is the un-refreshable reading (the raw
            # `merge_queue` row's frozen `error`, which only a live `coord
            # merge` rewrites); *ci_pending_live* is the self-refreshing one
            # (the `merge_plan` row's own reason, re-derived every board
            # build). Only the former ages out.
            merge_ci_pending_live=issue in ci_pending_live,
            merge_ci_pending_reason=(
                "CI running: test (3.12)"
                if issue in ci_pending or issue in ci_pending_live
                else ""
            ),
        )
    return BoardView(
        issues=facts,
        live_sessions=frozenset(entry_key(REPO, i) for i in sessions),
    )


# ── keys and --after parsing ─────────────────────────────────────────────────


def test_entry_key_and_parse_key_round_trip():
    assert entry_key(REPO, 1650) == f"{REPO}#1650"
    assert parse_key(f"{REPO}#1650") == (REPO, 1650)


def test_parse_key_rejects_a_non_numeric_tail():
    assert parse_key("claude-coordinator#abc") is None
    assert parse_key("claude-coordinator") is None


def test_bare_numbers_resolve_against_the_entrys_own_repo():
    assert parse_after_spec("1650,1651", REPO) == [
        f"{REPO}#1650",
        f"{REPO}#1651",
    ]


def test_qualified_and_bare_after_entries_mix():
    assert parse_after_spec(("1650", "quadraui#302"), REPO) == [
        f"{REPO}#1650",
        "quadraui#302",
    ]


def test_duplicate_after_entries_collapse_in_declaration_order():
    assert parse_after_spec("1650,1651,1650", REPO) == [
        f"{REPO}#1650",
        f"{REPO}#1651",
    ]


def test_a_malformed_after_entry_raises_rather_than_being_dropped():
    # A silently dropped pre-req launches work early — the exact failure this
    # feature exists to prevent.
    with pytest.raises(QueueError, match="malformed"):
        parse_after_spec("not-an-issue", REPO)


# ── cycle validation (the `add` gate) ────────────────────────────────────────


def test_find_cycle_reports_the_loop_members():
    cycle = find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]})
    assert cycle is not None
    assert set(cycle) == {"a", "b", "c"}


def test_find_cycle_ignores_edges_pointing_outside_the_queue():
    assert find_cycle({"a": ["not-queued"]}) is None


def test_validate_enqueue_refuses_a_self_edge():
    with pytest.raises(QueueError, match="cannot depend on itself"):
        validate_enqueue([], REPO, 1650, [entry_key(REPO, 1650)])


def test_validate_enqueue_refuses_a_two_node_cycle():
    existing = [entry(1650, after=(entry_key(REPO, 1654),))]
    with pytest.raises(QueueError, match="dependency cycle"):
        validate_enqueue(existing, REPO, 1654, [entry_key(REPO, 1650)])


def test_validate_enqueue_allows_a_prereq_that_is_not_queued():
    # `--after` is often "run this after that other thing merges", and that
    # other thing may never be queued at all. Satisfiability is a tick
    # question, not a write-time one.
    validate_enqueue([], REPO, 1654, ["quadraui#302"])


def test_validate_enqueue_uses_the_new_edges_not_the_stored_ones():
    # enqueue upserts, so re-adding 1650 with no `--after` must be judged on
    # the edges being WRITTEN — which is what makes remove+add the documented
    # escape from a queue that has somehow acquired a cycle.
    existing = [
        entry(1650, after=(entry_key(REPO, 1654),)),
        entry(1654, after=(entry_key(REPO, 1650),)),
    ]
    validate_enqueue(existing, REPO, 1650, [])


# ── building the board view ──────────────────────────────────────────────────


def test_build_board_view_reads_merge_and_activity_from_work_like_rows():
    view = build_board_view(
        {
            "assignments": [
                {"repo_name": REPO, "issue_number": 1650, "type": "work", "status": "merged"},
                {"repo_name": REPO, "issue_number": 1654, "type": "work", "status": "running"},
                # A review row must not make 1660 look like live WORK.
                {"repo_name": REPO, "issue_number": 1660, "type": "review", "status": "running"},
            ],
            "issues": [
                {"repo_name": REPO, "number": 1654, "state": "open", "synced_at": 1_700_000_000.0},
                {"repo_name": REPO, "number": 1650, "state": "closed"},
            ],
        },
        [{"repo": REPO, "issue": 1654}],
    )
    assert view.facts(entry_key(REPO, 1650)).landed
    assert view.facts(entry_key(REPO, 1654)).active_work
    assert view.facts(entry_key(REPO, 1654)).open
    assert not view.facts(entry_key(REPO, 1660)).active_work
    assert view.live_sessions == frozenset({entry_key(REPO, 1654)})
    # #2858: `synced_at`, when the `/board` payload carries it, is threaded
    # onto `IssueFacts.issue_synced_at` -- a row that never carried it (1650
    # here) stays `None`, the safe "not stale" default.
    assert view.facts(entry_key(REPO, 1654)).issue_synced_at == 1_700_000_000.0
    assert view.facts(entry_key(REPO, 1650)).issue_synced_at is None


def test_build_board_view_reads_merge_ci_pending_from_the_live_plan_reason():
    """#1891: the live `merge_plan` section (board-render time) is the
    primary source — mirrors `drive_state._merge_entry`'s own resolution."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 1650,
                    "reason": "CI running: build, lint",
                },
                {
                    "repo_name": REPO, "issue_number": 1654,
                    "reason": "CI failed: build (failure)",
                },
            ],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 1650, "error": None},
                {"repo_name": REPO, "issue_number": 1654, "error": None},
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 1650)).merge_ci_pending
    assert not view.facts(entry_key(REPO, 1654)).merge_ci_pending


def test_build_board_view_falls_back_to_the_raw_queue_rows_persisted_error():
    """#1891: when the live plan's re-evaluation comes back with no reason
    (e.g. a `_gate_refresher` snapshot that lagged or gapped a real `coord
    merge` attempt's fresher read — see `CI_PENDING_PREFIX`'s docstring),
    `merge_ci_pending` still sees the checks-pending signal through the raw
    `merge_queue` row's own persisted `error` — exactly the fallback
    `drive_state._merge_entry` uses for `merge_reason`."""
    view = build_board_view(
        {
            "merge_plan": [
                {"repo_name": REPO, "issue_number": 1650, "reason": None},
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1650,
                    "error": "CI running: build",
                },
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 1650)).merge_ci_pending


def test_build_board_view_ci_pending_is_false_with_no_merge_sections_at_all():
    """A board payload predating #1891 (or a standalone fetch with neither
    section populated) degrades to `merge_ci_pending=False` everywhere,
    never raises."""
    view = build_board_view({"assignments": [], "issues": []}, [])
    assert not view.facts(entry_key(REPO, 1650)).merge_ci_pending


# ── #1892: the sibling trigger — a verdictless CI failure ──────────────────

def test_build_board_view_reads_merge_ci_pending_from_a_ci_infra_raw_row():
    """#1892: `_entry_gate_status` (board-render time) never computes the
    CI_INFRA_PREFIX classification — it needs an extra `gh api .../jobs`
    call the board *read* path must never make (`coord.gate_snapshot`'s
    Invariant 1). Only a LIVE `coord merge` attempt computes it and persists
    it onto the raw `merge_queue` row's `error`; the live `merge_plan`
    reason for the SAME entry still reads the generic "checks failed: ..."
    wording. `build_board_view` must prefer the raw row's more specific
    reading — mirroring `drive_state._merge_entry`'s identical recovery —
    or a verdictless failure would never park at all."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 1892,
                    "reason": "checks failed: e2e (cancelled)",
                },
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1892,
                    "error": (
                        "CI infra: e2e (cancelled) — no verdict about the "
                        "code (never assigned a runner, or died before "
                        "checkout)"
                    ),
                },
            ],
        },
        [],
    )
    facts = view.facts(entry_key(REPO, 1892))
    assert facts.merge_ci_pending
    assert facts.merge_ci_pending_reason.startswith("CI infra:")


def test_build_board_view_does_not_park_a_genuine_checks_failed_entry():
    """Regression: a plain 'checks failed' reason on BOTH the plan and the
    raw row — no #1892 classification anywhere — must not park."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 1893,
                    "reason": "checks failed: build (failure)",
                },
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1893,
                    "error": "checks failed: build (failure)",
                },
            ],
        },
        [],
    )
    assert not view.facts(entry_key(REPO, 1893)).merge_ci_pending


def test_build_board_view_live_ci_infra_plan_reason_also_parks():
    """If a future refactor DOES let the plan itself carry the #1892
    wording, `build_board_view` must still recognise it directly — the raw-
    row cross-check is a fallback, not the only path."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 1894,
                    "reason": "CI infra: e2e (cancelled)",
                },
            ],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 1894, "error": None},
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 1894)).merge_ci_pending


# ── #2252: the OTHER sibling trigger — a genuinely-verdicted failure mid its
# one #2252 re-check ────────────────────────────────────────────────────────

def test_build_board_view_reads_merge_ci_pending_from_a_ci_flaky_raw_row():
    """#2252: same recovery as the #1892 test above — `_entry_gate_status`
    has no notion of the raw row's `ci_flaky_reruns`/`ci_flaky_pending`
    state (only a LIVE `coord merge` attempt tracks it), so the live
    `merge_plan` reason for the SAME entry still reads the generic "checks
    failed: ..." wording while the raw row carries the more specific
    CI_FLAKY_PREFIX one. `build_board_view` must prefer the raw reading or
    a pending flake re-check would never park — it would sit as a plain
    `checks_failed` block and burn a drive-queue launch attempt on the
    exact transient #2252 exists to catch."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 2252,
                    "reason": "checks failed: build (failure)",
                },
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 2252,
                    "error": (
                        "CI re-checking: build (failure) — re-running once "
                        "before treating as broken (1/1, #2252)"
                    ),
                },
            ],
        },
        [],
    )
    facts = view.facts(entry_key(REPO, 2252))
    assert facts.merge_ci_pending
    assert facts.merge_ci_pending_reason.startswith("CI re-checking:")


def test_build_board_view_live_ci_flaky_plan_reason_also_parks():
    """If a future refactor DOES let the plan itself carry the #2252
    wording, `build_board_view` must still recognise it directly — the raw-
    row cross-check is a fallback, not the only path."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 2253,
                    "reason": "CI re-checking: build (failure)",
                },
            ],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 2253, "error": None},
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 2253)).merge_ci_pending


# ── #2347: a THIRD sibling trigger — the check-list FETCH itself failed
# (GitHub unreachable), not a real CI verdict of any shape. Unlike #1892/
# #2252 above, this needs NO raw-row recovery test — `_entry_gate_status`
# computes the classification directly (no extra I/O needed, see
# `coord.merge_queue._ci_unreadable_reason`'s docstring), so the LIVE
# `merge_plan` reason already carries it whenever it applies.

def test_build_board_view_reads_merge_ci_pending_from_a_ci_unreadable_plan_reason():
    """#2347: unlike #1892/#2252, the live `merge_plan` reason ALONE is
    enough — no raw `merge_queue` row recovery needed."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 2347,
                    "reason": (
                        "CI unreadable: coord: could not read CI status for "
                        "acme/api#99 (HTTP 503) (unknown) — GitHub could not "
                        "be reached to read CI status; this is not a CI result"
                    ),
                },
            ],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 2347, "error": None},
            ],
        },
        [],
    )
    facts = view.facts(entry_key(REPO, 2347))
    assert facts.merge_ci_pending
    assert facts.merge_ci_pending_reason.startswith("CI unreadable:")


# ── #2158: the frozen `error` string vs the live CI rollup ─────────────────
#
# The raw `merge_queue` row's `error` is written by a live `coord merge`
# attempt and by NOTHING else. For a `parked` entry — which by construction
# runs no merge — it is frozen at the attempt that parked it, so believing it
# over the board's own fresh reading makes the predicate that RELEASES the
# park refreshable only by the action the park WITHHOLDS.
#
# claude-coordinator#2138 (2026-08-12): CI run 31570947900 completed green at
# 06:48:51; the park was written at 06:49:32 quoting "CI running: …"; the
# entry then did not move for 7h25m, over a fully satisfied gate, until an
# unrelated merge happened to rewrite the board.


def _plan_row(issue: int, *, reason=None, ci_summary=None) -> dict:
    """One `/board` `merge_plan` row, shaped as `serve_app` ships it
    (`dataclasses.asdict` of a `PlannedMerge`, so `ci_summary` is a nested
    dict of `coord.ci_store.CiCheckSummary`)."""
    return {
        "repo_name": REPO,
        "issue_number": issue,
        "reason": reason,
        "ci_summary": ci_summary,
    }


def _rollup(passed: int = 0, failed: int = 0, running: int = 0) -> dict:
    return {
        "passed": passed,
        "failed": failed,
        "running": running,
        "failed_names": [],
        "first_failed_url": None,
    }


def test_build_board_view_drops_a_stale_ci_error_the_live_rollup_contradicts():
    """THE #2158 regression, at the fact level.

    The plan re-derived this entry clean (no reason of its own) AND its
    `ci_summary` — `summarize_counts` over the very checks that re-derivation
    consulted — says all 8 checks finished green. The raw row's "CI running:"
    is therefore a frozen write-path string that CI has already outrun, and
    must not hold the park.
    """
    view = build_board_view(
        {
            "merge_plan": [_plan_row(2138, reason=None, ci_summary=_rollup(passed=8))],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 2138,
                    "error": (
                        "CI running: no-gh-on-path, test (3.13), test (3.12)"
                    ),
                },
            ],
        },
        [],
    )
    assert not view.facts(entry_key(REPO, 2138)).merge_ci_pending


def test_build_board_view_keeps_the_park_while_the_rollup_shows_checks_in_flight():
    """The other half: checks genuinely still running is NOT evidence against
    the persisted reading — it agrees with it. Stays parked, no hot loop."""
    view = build_board_view(
        {
            "merge_plan": [
                _plan_row(2138, reason=None, ci_summary=_rollup(passed=5, running=3)),
            ],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 2138, "error": "CI running: test"},
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 2138)).merge_ci_pending


def test_build_board_view_keeps_the_park_when_the_plan_carries_no_rollup():
    """Fail closed, and leave #1891 exactly as it was: absence of a rollup
    (no PR yet, no `ci_store`, a gate snapshot that has not fetched this PR)
    is not evidence of anything. Only a POSITIVE all-green reading overrides
    the persisted string."""
    view = build_board_view(
        {
            "merge_plan": [_plan_row(2138, reason=None, ci_summary=None)],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 2138, "error": "CI running: test"},
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 2138)).merge_ci_pending


def test_build_board_view_keeps_a_ci_infra_park_while_the_rollup_shows_red():
    """#1892's classification lives ONLY on the raw row — the plan can never
    re-derive it. So a rollup that still shows a failed check is not evidence
    the verdictless failure has cleared, and the #2158 override must not fire
    on it. (An all-green rollup would; see the next test.)"""
    view = build_board_view(
        {
            "merge_plan": [
                _plan_row(1892, reason=None, ci_summary=_rollup(passed=7, failed=1)),
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1892,
                    "error": "CI infra: e2e (cancelled) — no verdict about the code",
                },
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 1892)).merge_ci_pending


def test_build_board_view_releases_a_ci_infra_park_once_the_rerun_lands_green():
    """The #1892 auto-rerun landing is exactly what un-parks that entry — and
    an all-green rollup is how the read path can see it happen, without a
    live `coord merge` to rewrite the raw row."""
    view = build_board_view(
        {
            "merge_plan": [_plan_row(1892, reason=None, ci_summary=_rollup(passed=8))],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1892,
                    "error": "CI infra: e2e (cancelled) — no verdict about the code",
                },
            ],
        },
        [],
    )
    assert not view.facts(entry_key(REPO, 1892)).merge_ci_pending


def test_build_board_view_flags_a_ci_infra_override_as_unrefreshable():
    """The realistic #1892-override pairing (review finding on #2158's first
    diff): `_entry_gate_status` re-derives a LIVE, non-infra "CI failed: ..."
    reason on the SAME `checks` read that produced a red `ci_summary` — a
    generic verdict is exactly what a still-failing CI-infra check looks like
    from the plan's side, since the plan can never compute the infra
    classification itself (#1892). The raw row still carries the frozen "CI
    infra:" string from the live merge attempt that parked this entry, so the
    #1892 override fires and `reason` ends up being that raw string — NOT the
    live plan one.

    `merge_ci_pending_live` must follow `reason`'s actual provenance, not
    `bool(plan_reason)`: a non-empty plan reason lost the override fight here,
    so this reading is exactly as unrefreshable as if the plan had been
    silent, and `plan_tick`'s `PARK_STALE_SECONDS` ceiling must still apply to
    it. Before the fix this asserted `merge_ci_pending_live=True` — the same
    "held with no ceiling" bug #2158 was written to close, just reached via a
    plan row that isn't empty."""
    view = build_board_view(
        {
            "merge_plan": [
                _plan_row(
                    1892,
                    reason="CI failed: test (3.12)",
                    ci_summary=_rollup(passed=7, failed=1),
                ),
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1892,
                    "error": "CI infra: e2e (cancelled) — no verdict about the code",
                },
            ],
        },
        [],
    )
    facts = view.facts(entry_key(REPO, 1892))
    assert facts.merge_ci_pending
    assert facts.merge_ci_pending_reason == (
        "CI infra: e2e (cancelled) — no verdict about the code"
    )
    assert not facts.merge_ci_pending_live


def test_build_board_view_never_lets_a_rollup_overrule_a_live_plan_objection():
    """A non-empty plan reason is the live gate still objecting. It wins
    outright — the override only ever applies where the plan is silent."""
    view = build_board_view(
        {
            "merge_plan": [
                _plan_row(
                    2138,
                    reason="CI running: test (3.12)",
                    # Contradictory on purpose: a rollup that lagged the gate.
                    ci_summary=_rollup(passed=8),
                ),
            ],
            "merge_queue": [{"repo_name": REPO, "issue_number": 2138, "error": None}],
        },
        [],
    )
    facts = view.facts(entry_key(REPO, 2138))
    assert facts.merge_ci_pending
    assert facts.merge_ci_pending_live


def test_build_board_view_marks_a_raw_only_ci_reading_as_unrefreshable():
    """Provenance (#2158): a reading with no live plan reason behind it is
    flagged `merge_ci_pending_live=False`, which is what lets `plan_tick` age
    it out instead of trusting it forever."""
    view = build_board_view(
        {
            "merge_plan": [],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 2138, "error": "CI running: test"},
            ],
        },
        [],
    )
    facts = view.facts(entry_key(REPO, 2138))
    assert facts.merge_ci_pending
    assert not facts.merge_ci_pending_live


def test_build_board_view_survives_a_malformed_ci_rollup():
    """A rollup that is not a readable mapping of ints is not evidence — the
    park stands, and nothing raises."""
    for summary in ("green", 3, {"passed": "eight", "failed": 0, "running": 0}, []):
        view = build_board_view(
            {
                "merge_plan": [_plan_row(2138, reason=None, ci_summary=summary)],
                "merge_queue": [
                    {
                        "repo_name": REPO, "issue_number": 2138,
                        "error": "CI running: test",
                    },
                ],
            },
            [],
        )
        assert view.facts(entry_key(REPO, 2138)).merge_ci_pending, summary


def test_unknown_issues_report_nothing_rather_than_raising():
    view = build_board_view({}, [])
    facts = view.facts("nope#1")
    assert not facts.known and not facts.landed and not facts.active_work


def test_entries_from_rows_types_after_json_in_either_encoding():
    typed = entries_from_rows(
        [
            {"repo_name": REPO, "issue_number": 2, "position": 1, "after_json": '["a#1"]'},
            {"repo_name": REPO, "issue_number": 1, "position": 0, "after_json": ["b#2"]},
            {"repo_name": REPO, "issue_number": 3, "position": 2, "after_json": "{oops"},
        ]
    )
    assert [e.issue for e in typed] == [1, 2, 3]
    assert typed[0].after == ("b#2",)
    assert typed[1].after == ("a#1",)
    assert typed[2].after == ()


# ── compute_leg_counts: per-issue assignment leg counts (#3060) ─────────────


def test_compute_leg_counts_groups_by_issue_key_and_type():
    counts = compute_leg_counts([
        (REPO, 1, "work"),
        (REPO, 1, "work"),
        (REPO, 1, "review"),
        ("web", 9, "smoke"),
    ])
    assert counts == {
        entry_key(REPO, 1): {"work": 2, "review": 1},
        entry_key("web", 9): {"smoke": 1},
    }


def test_compute_leg_counts_returns_only_types_actually_present():
    """No fixed `{work, review, smoke}` shape — a type nobody dispatched
    never appears, and (the flip side, #3060's acceptance bar) a type this
    function has never heard of shows up automatically with no code change:
    the grouping is by whatever string sits in the `type` column."""
    counts = compute_leg_counts([(REPO, 1, "a-brand-new-assignment-type")])
    assert counts == {entry_key(REPO, 1): {"a-brand-new-assignment-type": 1}}


def test_compute_leg_counts_empty_input_is_empty_map():
    assert compute_leg_counts([]) == {}


def test_compute_leg_counts_treats_falsy_type_as_work():
    """A row with no `type` (empty string / None, e.g. a hand-built test
    fixture) counts as `work` — the same default `assignments.type` and
    `coord.models.Assignment.type` both carry."""
    counts = compute_leg_counts([(REPO, 1, ""), (REPO, 1, None)])
    assert counts == {entry_key(REPO, 1): {"work": 2}}


# ── plan_tick: the launch decision ───────────────────────────────────────────


def test_first_eligible_wins_the_head_is_not_special():
    entries = [
        entry(1650, position=0, after=("quadraui#302",)),
        entry(1654, position=1),
    ]
    plan = plan_tick(entries, board(open_=(302,)), capacity=1)
    assert plan.launch is not None
    assert plan.launch.issue == 1654


def test_a_deferred_entry_keeps_its_position_and_counts_a_deferral():
    entries = [
        entry(1650, position=0, after=(entry_key(REPO, 1),), deferrals=3),
        entry(1654, position=1),
    ]
    plan = plan_tick(entries, board(open_=(1,)), capacity=1)
    assert [d.key for d in plan.deferrals] == [entry_key(REPO, 1650)]
    updates = plan.deferrals[0].updates
    assert updates["deferrals"] == 4
    assert "position" not in updates  # deferral never reorders (#1750 design note)
    assert "1" in updates["last_reason"]


def test_a_merged_prereq_satisfies_and_so_does_a_closed_issue():
    merged_dep = plan_tick(
        [entry(1654, after=(entry_key(REPO, 1650),))], board(merged=(1650,)), capacity=1
    )
    closed_dep = plan_tick(
        [entry(1654, after=(entry_key(REPO, 1650),))], board(closed=(1650,)), capacity=1
    )
    assert merged_dep.launch is not None
    assert closed_dep.launch is not None


def test_an_unknown_prereq_is_unsatisfiable_and_does_not_consume_an_attempt():
    entries = [entry(1654, after=("ghost#99",))]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.launch is None
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1654)]
    updates = plan.blocked[0].updates
    assert updates["state"] == STATE_BLOCKED
    assert "attempts" not in updates
    assert "ghost#99" in updates["last_reason"]


def test_a_live_confirmed_terminal_prereq_satisfies_even_when_the_board_is_unknown():
    """#2602 recovery half: the cached board has no row at all for the dep
    (the exact "unknown issue" shape that used to permanently block, e.g. a
    pre-req that just merged/closed faster than the periodic `/board` build
    could catch up) — but this tick's live re-check
    (`coord.commands.drive_queue._fetch_live_prereq_terminal`) confirms it
    already landed. That must read as satisfied, not blocked."""
    entries = [entry(1654, after=("ghost#99",))]
    plan = plan_tick(
        entries, board(), capacity=1, live_prereq_terminal={"ghost#99": True}
    )
    assert plan.launch is not None
    assert plan.launch.issue == 1654
    assert plan.blocked == ()


def test_a_prereq_queued_but_blocked_is_unsatisfiable():
    entries = [
        entry(1650, position=0, state=STATE_BLOCKED),
        entry(1654, position=1, after=(entry_key(REPO, 1650),)),
    ]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.launch is None
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1654)]
    assert "never satisfy" in plan.blocked[0].reason


def test_a_prereq_with_live_work_but_no_issue_row_defers_rather_than_blocks():
    # The standalone `serialize_board` payload ships assignments only, so the
    # daemon host sees no `issues` rows — an in-flight pre-req must still read
    # as "not yet", not as "unknown".
    entries = [entry(1654, after=(entry_key(REPO, 1650),))]
    plan = plan_tick(entries, board(active=(1650,)), capacity=1)
    assert plan.launch is None
    assert plan.blocked == ()
    assert "work in flight" in plan.deferrals[0].reason


def test_a_cycle_discovered_at_tick_time_blocks_every_member():
    entries = [
        entry(1650, position=0, after=(entry_key(REPO, 1654),)),
        entry(1654, position=1, after=(entry_key(REPO, 1650),)),
    ]
    plan = plan_tick(entries, board(), capacity=2)
    assert plan.launch is None
    assert {b.key for b in plan.blocked} == {
        entry_key(REPO, 1650),
        entry_key(REPO, 1654),
    }
    assert all("cycle" in b.reason for b in plan.blocked)


def test_nothing_eligible_records_exactly_one_queue_level_alert():
    entries = [
        entry(1650, position=0, after=("ghost#1",)),
        entry(1654, position=1, after=("ghost#2",)),
    ]
    plan = plan_tick(entries, board(), capacity=2)
    assert plan.launch is None
    assert plan.alert is not None
    assert "nothing eligible" in plan.alert.reason
    assert len(plan.alert.details) == 2


def test_an_empty_queue_raises_no_alert():
    assert plan_tick([], board(), capacity=1).alert is None


def test_terminal_entries_are_neither_launched_nor_alerted_on():
    entries = [entry(1650, state=STATE_DONE), entry(1654, state=STATE_BLOCKED)]
    plan = plan_tick(entries, board(), capacity=2)
    assert plan.launch is None
    assert plan.alert is None
    assert plan.writes() == []


# ── plan_tick: capacity ──────────────────────────────────────────────────────


def test_a_live_session_occupies_a_slot_and_blocks_the_launch():
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
    ]
    plan = plan_tick(entries, board(sessions=(1650,), active=(1650,)), capacity=1)
    assert plan.occupied == 1
    assert plan.launch is None
    assert plan.alert is None  # at capacity is the queue working, not a problem


def test_a_deadline_expired_drive_still_occupies_a_slot():
    # #1660 / the 2026-08-01 incident: `coord drive` returned EXIT_DEADLINE, so
    # the tmux session is gone — but the worker, test and review are still
    # running on the fleet. Counting this as free is how a sequential batch
    # became concurrent.
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
    ]
    plan = plan_tick(entries, board(active=(1650,)), capacity=1)
    assert plan.occupied == 1
    assert plan.launch is None
    assert [r.outcome for r in plan.reconciles] == ["held"]
    # The row stays `running` — nothing relaunches it while its work is live.
    assert "state" not in plan.reconciles[0].updates


def test_capacity_above_one_launches_while_another_drive_runs():
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
    ]
    # Both entries are the SAME repo, so #1972's per-repo ceiling has to be
    # raised for the global ceiling to be the thing under test here — with the
    # default of 1 this queue deliberately defers (the test right below).
    plan = plan_tick(
        entries, board(sessions=(1650,)), capacity=2, max_parallel_per_repo=2
    )
    assert plan.occupied == 1
    assert plan.launch is not None and plan.launch.issue == 1654


def test_only_one_entry_launches_per_tick():
    entries = [entry(1650, position=0), entry(1654, position=1)]
    plan = plan_tick(entries, board(), capacity=5)
    assert plan.launch is not None and plan.launch.issue == 1650
    assert len(plan.writes()) == 0  # nothing else touched


def test_entries_after_the_launch_are_reported_but_never_counted():
    entries = [
        entry(1650, position=0),
        entry(1654, position=1, after=(entry_key(REPO, 1650),), deferrals=0),
    ]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.launch is not None and plan.launch.issue == 1650
    assert [d.key for d in plan.deferrals] == [entry_key(REPO, 1654)]
    assert plan.deferrals[0].counted is False
    assert plan.deferrals[0].updates == {}
    assert plan.writes() == []  # a launch tick mutates only the launched row
    text = "\n".join(render_plan(plan))
    assert f"defer {entry_key(REPO, 1654)}" in text
    assert entry_key(REPO, 1650) in text
    assert "not reached this tick" in text


# ── plan_tick: the per-repo ceiling (#1972) ─────────────────────────────────
#
# Per-repo serialisation, cross-repo parallelism. The hazard that forced
# serialisation is intra-repo (a merge stales the Test verdicts of the other
# queued branches in THAT repo), so repo is the axis along which extra
# parallelism is safe. Before this, `--max-parallel` was one global counter:
# capacity 3 with a 39-entry claude-coordinator queue would launch the two
# entries most likely to stale each other and never reach the quadraui entry
# that could have run for free.


def cross_repo_board(
    *,
    open_: tuple[str, ...] = (),
    sessions: tuple[str, ...] = (),
    active: tuple[str, ...] = (),
) -> BoardView:
    """A board keyed by fully-qualified ``repo#N``, for the multi-repo tests.

    The module-level ``board()`` helper hardcodes ``REPO``, which is exactly
    the single-repo assumption #1972 exists to break.
    """
    facts = {
        key: IssueFacts(
            known=True,
            issue_state="open",
            active_work=key in active,
        )
        for key in {*open_, *active, *sessions}
    }
    return BoardView(issues=facts, live_sessions=frozenset(sessions))


def other(issue: int, repo: str, position: int, **kw) -> QueueEntry:
    return QueueEntry(repo=repo, issue=issue, position=position, **kw)


def test_a_second_repo_rides_alongside_an_in_progress_repo():
    """#1972's headline scenario, asserted end to end.

    Capacity 3. Position 0 is a claude-coordinator drive in progress;
    positions 1..38 are claude-coordinator and blocked BY DESIGN; position 39
    is quadraui. The tick must launch the quadraui entry — the walk already
    skips deferred entries, so all this needs is for the same-repo entries to
    actually defer.
    """
    entries = [entry(1650, position=0, state=STATE_RUNNING)]
    entries += [entry(1700 + i, position=i + 1) for i in range(38)]
    entries.append(other(302, "quadraui", position=39))
    board_view = cross_repo_board(
        sessions=(entry_key(REPO, 1650),),
        active=(entry_key(REPO, 1650),),
        open_=tuple(entry_key(REPO, 1700 + i) for i in range(38))
        + ("quadraui#302",),
    )

    plan = plan_tick(entries, board_view, capacity=3)

    assert plan.launch is not None
    assert plan.launch.key == "quadraui#302"
    assert plan.occupied == 1
    assert plan.repo_occupied == {REPO: 1}


def test_same_repo_entries_defer_rather_than_block():
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1, deferrals=2),
        other(302, "quadraui", position=2),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650),),
            open_=(entry_key(REPO, 1654), "quadraui#302"),
        ),
        capacity=3,
    )
    assert plan.launch is not None and plan.launch.key == "quadraui#302"
    assert plan.blocked == ()  # a defer, never a block
    deferral = next(d for d in plan.deferrals if d.key == entry_key(REPO, 1654))
    assert deferral.repo_limited is True
    # Position untouched, no attempt consumed — only the deferral counter and
    # the reason an operator reads in `coord drive-queue list` move.
    assert set(deferral.updates) == {"deferrals", "last_reason"}
    assert deferral.updates["deferrals"] == 3
    assert "at its limit (1/1)" in deferral.reason


def test_the_per_repo_ceiling_is_configurable():
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
    ]
    board_view = cross_repo_board(
        sessions=(entry_key(REPO, 1650),), open_=(entry_key(REPO, 1654),)
    )
    assert plan_tick(entries, board_view, capacity=3).launch is None
    raised = plan_tick(entries, board_view, capacity=3, max_parallel_per_repo=2)
    assert raised.launch is not None and raised.launch.issue == 1654
    # 0 disables the ceiling entirely — one global counter, as before #1972.
    off = plan_tick(entries, board_view, capacity=3, max_parallel_per_repo=0)
    assert off.launch is not None and off.launch.issue == 1654


def test_the_global_ceiling_still_wins_over_a_repo_with_headroom():
    """Both apply, GLOBAL first — a full fleet launches nothing, any repo."""
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        other(302, "quadraui", position=1),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650),), open_=("quadraui#302",)
        ),
        capacity=1,
    )
    assert plan.launch is None
    assert plan.deferrals == ()  # the walk never ran; at capacity is not a defer


def test_a_repo_limited_queue_is_saturated_not_stalled():
    """No queue-level alert: this is the queue working, like at-capacity."""
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650),), open_=(entry_key(REPO, 1654),)
        ),
        capacity=3,
    )
    assert plan.launch is None
    assert plan.alert is None
    text = "\n".join(render_plan(plan))
    assert "per-repo: claude-coordinator 1/1" in text
    assert "every waiting entry's repo is at its per-repo limit" in text


def test_a_mixed_benign_defer_set_names_every_cause_in_the_summary():
    """Post-review nit fix: a MIXED benign set (one repo-limited entry, one
    backing off after a retry) must name BOTH causes in the one-line "no
    launch" summary — the earlier `cordoned > repo_limited > backing_off`
    elif chain let the first-matching cause's message silently stand in for
    the whole line, dropping whichever cause lost that race even though
    both are still both true this tick."""
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),  # claude-coordinator's one slot is taken
        other(
            302,
            "quadraui",
            position=2,
            state=STATE_WAITING,
            attempts=1,
            retry_backoff_at=NOW - 10.0,
        ),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650),),
            open_=(entry_key(REPO, 1654), "quadraui#302"),
        ),
        capacity=3,
        now=NOW,
    )
    assert plan.launch is None
    assert plan.alert is None  # both causes are benign — no queue-level alert
    assert {d.repo_limited for d in plan.deferrals} == {True, False}
    assert {d.backing_off for d in plan.deferrals} == {True, False}
    text = "\n".join(render_plan(plan))
    assert "mixed causes" in text
    assert "repo-limited" in text
    assert "pacing a retry after a recent failure" in text


def test_a_genuinely_stuck_entry_still_alerts_alongside_a_repo_limit_defer():
    """Mixed tick: something really is stuck, so the alert names all of it."""
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
        other(302, "quadraui", position=2, after=("quadraui#1",)),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650),), open_=(entry_key(REPO, 1654),)
        ),
        capacity=3,
    )
    assert plan.launch is None
    assert plan.alert is not None
    assert len(plan.alert.details) == 2  # both deferrals explained


def test_the_launch_takes_its_own_repos_slot_in_the_report_only_tail():
    """`--dry-run` must not call the next same-repo entry eligible."""
    entries = [
        other(302, "quadraui", position=0),
        other(303, "quadraui", position=1),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(open_=("quadraui#302", "quadraui#303")),
        capacity=3,
    )
    assert plan.launch is not None and plan.launch.issue == 302
    assert [d.key for d in plan.deferrals] == ["quadraui#303"]
    assert plan.deferrals[0].counted is False  # never competed for the slot
    assert plan.deferrals[0].updates == {}
    assert plan.writes() == []
    assert "at its limit (1/1)" in plan.deferrals[0].reason


def test_an_unsatisfiable_prereq_still_blocks_inside_a_full_repo():
    """Capacity is not an excuse to sit on a permanently broken entry."""
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1, after=("ghost#1",)),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(sessions=(entry_key(REPO, 1650),)),
        capacity=3,
    )
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1654)]
    assert plan.alert is not None  # blocked is a stall, and stalls escalate


def test_the_capacity_line_shows_the_per_repo_breakdown():
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        other(302, "quadraui", position=1, state=STATE_RUNNING),
        entry(1654, position=2),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650), "quadraui#302"),
            open_=(entry_key(REPO, 1654),),
        ),
        capacity=4,
    )
    text = "\n".join(render_plan(plan, dry_run=True))
    assert "2/4 occupied" in text
    assert "per-repo: claude-coordinator 1/1, quadraui 1/1" in text
    # #1660's caveat, restated where it now bites one repo instead of the queue.
    assert "counted from board state" in text


def test_a_plan_without_a_per_repo_ceiling_renders_the_original_line():
    entries = [entry(1650, position=0)]
    plan = plan_tick(entries, board(), capacity=1, max_parallel_per_repo=0)
    assert "per-repo" not in "\n".join(render_plan(plan))


# ── plan_tick: reconciliation ────────────────────────────────────────────────


def test_a_finished_drive_becomes_done():
    entries = [entry(1650, state=STATE_RUNNING)]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=1)
    assert plan.reconciles[0].outcome == "done"
    assert plan.reconciles[0].updates["state"] == STATE_DONE
    assert plan.occupied == 0


# ── plan_tick: a `waiting` entry whose issue already landed (#1873) ─────────
#
# The launch-side counterpart to test_a_finished_drive_becomes_done above:
# that test covers an entry `_reconcile_running` catches because it was
# actually launched.  A `waiting` entry never reaches that function at all —
# #1864 was the live incident: its work landed inside #1862's PR and the
# issue closed, but the queue row was never touched and `drive-queue tick`
# was about to burn a full drive re-discovering that.


def test_a_waiting_entry_whose_issue_is_closed_reconciles_to_done_unlaunched():
    entries = [entry(1864)]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    assert plan.launch is None
    assert [r.outcome for r in plan.reconciles] == ["done"]
    reconcile = plan.reconciles[0]
    assert reconcile.updates["state"] == STATE_DONE
    assert "never launched" in reconcile.reason
    assert "closed" in reconcile.reason


def test_a_waiting_entry_whose_work_merged_but_issue_still_open_also_reconciles():
    # #611 is why both witnesses exist: merged work can leave an issue open.
    entries = [entry(1864)]
    plan = plan_tick(entries, board(merged=(1864,), open_=(1864,)), capacity=1)
    assert plan.launch is None
    assert [r.outcome for r in plan.reconciles] == ["done"]
    reconcile = plan.reconciles[0]
    assert reconcile.updates["state"] == STATE_DONE
    assert "merged" in reconcile.reason


def test_a_landed_waiting_entry_does_not_consume_an_attempt():
    entries = [entry(1864, attempts=2)]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    updates = plan.reconciles[0].updates
    assert "attempts" not in updates


def test_a_landed_waiting_entrys_reason_is_distinct_from_a_real_completion():
    # The reason text must not read as "a drive ran and finished" — nothing
    # was ever launched for this entry.
    entries = [entry(1864)]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    reason = plan.reconciles[0].reason
    assert "drive finished" not in reason
    assert "never launched" in reason


def test_a_genuinely_open_waiting_entry_still_launches():
    entries = [entry(1864)]
    plan = plan_tick(entries, board(open_=(1864,)), capacity=1)
    assert plan.launch is not None and plan.launch.issue == 1864
    assert plan.reconciles == ()


def test_a_landed_entry_does_not_block_downstream_after_entries():
    # `_resolve_prereqs` already reads `facts.landed` straight off the board
    # (:707) — it does not care whether the pre-req's own queue row ever
    # transitioned to `done`.  A landed-but-still-`waiting` upstream entry
    # must not stall its successor.
    entries = [
        entry(1864, position=0, after=()),
        entry(1866, position=1, after=(entry_key(REPO, 1864),)),
    ]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=2)
    assert plan.launch is not None and plan.launch.issue == 1866
    outcomes = {r.key: r.outcome for r in plan.reconciles}
    assert outcomes[entry_key(REPO, 1864)] == "done"


def test_a_landed_entry_writes_are_applied_through_the_normal_writes_path():
    # The `Reconcile` this produces must flow through `TickPlan.writes()` the
    # same as every other reconcile — no separate plumbing for #1873's case.
    entries = [entry(1864)]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    writes = dict(plan.writes())
    assert writes[entry_key(REPO, 1864)]["state"] == STATE_DONE
    assert "attempts" not in writes[entry_key(REPO, 1864)]


def test_a_landed_waiting_entry_does_not_raise_a_stalled_alert():
    # The exact #1864 reproduction from the review: a single `waiting` entry
    # whose issue is closed.  `plan.launch` being `None` is correct, but
    # `plan.alert` must ALSO be `None` — the tick reconciled the entry
    # cleanly, it did not stall.  Before this fix, the `waiting` snapshot
    # taken before the walk still counted this entry as "considered", and it
    # has no `details` line (it was never deferred or blocked), so the queue
    # escalated a `QUEUE: STALLED` record for a tick that had nothing wrong.
    entries = [entry(1864)]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    assert plan.launch is None
    assert plan.alert is None


def test_a_mixed_queue_only_counts_the_genuinely_blocked_entry_in_the_alert():
    # One entry reconciles via #1873 (closed, never launched); the other is
    # genuinely unsatisfiable and blocks.  The alert must describe ONLY the
    # blocked entry — "considered N" and `len(details)` must agree, or the
    # alert contradicts `coord drive-queue status` two lines below it.
    entries = [
        entry(1864, position=0),
        entry(1654, position=1, after=("ghost#99",)),
    ]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=2)
    assert plan.launch is None
    assert plan.alert is not None
    assert "considered 1 waiting entry" in plan.alert.reason
    assert len(plan.alert.details) == 1
    assert entry_key(REPO, 1654) in plan.alert.details[0]
    assert entry_key(REPO, 1864) not in " ".join(plan.alert.details)


def test_a_landed_entry_with_an_unsatisfiable_prereq_still_reconciles_to_done():
    # Ordering matters: the entry's own board state is checked BEFORE its
    # `after=` graph, so a landed entry whose pre-req is unsatisfiable (here,
    # unknown) reconciles to `done` rather than being routed into BLOCKED —
    # which would escalate and demand a manual `remove && add` for an entry
    # that is already finished.
    entries = [entry(1864, after=("ghost#99",))]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    assert plan.launch is None
    assert plan.blocked == ()
    assert [r.outcome for r in plan.reconciles] == ["done"]
    assert plan.reconciles[0].updates["state"] == STATE_DONE
    assert plan.alert is None


def test_a_dead_drive_is_requeued_at_the_same_position_with_an_attempt_spent():
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "retry"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 1
    assert "position" not in reconcile.updates
    # …and it is eligible again on this same tick.
    assert plan.launch is not None and plan.launch.issue == 1650
    # No `launched_at` and no `now`, so #1794's startup window does not apply:
    # a row with nothing to measure keeps the pre-#1794 behaviour exactly.
    assert entries[0].launched_at is None


def test_a_dead_drive_with_a_launch_stamp_still_dies_once_the_window_passes():
    """The `launched_at` path, not just the "no stamp to measure" one.

    #2273 superseded the OLD same-tick-relaunch assertion this test used to
    make: dying past #1794's startup window is no longer enough to relaunch
    in the SAME tick with a real clock — see
    `test_a_dead_drive_with_a_real_clock_paces_its_next_attempt_2273` right
    below for the backoff itself. This test now pins the earlier half only:
    the death is still correctly detected as `retry` (not `starting`, not
    `unknown`) once the window is past.
    """
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_RUNNING,
            attempts=0,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "retry"
    assert plan.reconciles[0].updates["attempts"] == 1


# ── plan_tick: post-death retry spacing (#2273) ──────────────────────────────
#
# 2026-08-15: quadraui#508 and coord-portal#83 each burned their entire
# two-attempt budget inside ~6 minutes — nothing paced the SECOND attempt
# beyond ordinary tick cadence, so a transient dispatch blip converted
# straight into a permanently-parked entry. These tests pin the fix: a
# `retry`-reconciled entry (or one already sitting `waiting` from an earlier
# one) is not relaunched until real wall-clock time — not just another tick —
# has passed.


def test_a_dead_drive_with_a_real_clock_paces_its_next_attempt_2273():
    """The regression: WITH a real clock, a `retry` this tick must NOT
    relaunch in the same tick — that immediate relaunch is exactly what let
    quadraui#508's two attempts land six minutes apart."""
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_RUNNING,
            attempts=0,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "retry"
    assert plan.launch is None
    backoff = [d for d in plan.deferrals if d.key == entry_key(REPO, 1650)]
    assert len(backoff) == 1
    assert backoff[0].backing_off is True
    assert "retry backoff" in backoff[0].reason
    # Benign — this is the fleet pacing itself, not a stall an operator needs
    # to see paged for every tick of the wait.
    assert backoff[0].benign is True


def test_a_dead_drive_relaunches_once_the_backoff_elapses():
    """A SECOND tick, comfortably past `RETRY_BACKOFF_SECONDS[0]` (60s),
    launches normally — the backoff paces the retry, it does not cancel it."""
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_WAITING,
            attempts=1,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 200.0,
            retry_backoff_at=NOW - 61.0,
        )
    ]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)
    assert plan.launch is not None and plan.launch.issue == 1650


def test_the_backoff_widens_with_the_attempt_number():
    """`RETRY_BACKOFF_SECONDS[1]` (5 min) applies before a SECOND retry, not
    just the first — 61s (enough for attempt 1) is not enough for attempt 2."""
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_WAITING,
            attempts=2,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 400.0,
            retry_backoff_at=NOW - 61.0,
        )
    ]
    plan = plan_tick(entries, board(), capacity=1, max_attempts=3, now=NOW)
    assert plan.launch is None
    assert any(d.backing_off for d in plan.deferrals)


def test_the_backoff_deferrals_own_write_never_moves_its_own_anchor():
    """THE #2273 post-review "moving target" regression.

    Production runs a 180s tick against a 300s dispatch-failure floor —
    shorter than the backoff it is supposed to pace. Before this fix, the
    backoff-deferral's own per-tick status write (`deferrals`/`last_reason`)
    re-stamped `reason_at`, which `_retry_backoff_reason` also read its
    anchor from — so every tick that observed the entry still backing off
    reset the very clock the backoff was measured against, and `age` never
    grew past one tick interval. It could never finish waiting.

    Reproduced here WITHOUT any DB layer: each simulated tick applies only
    the fields the deferral's own `updates` dict actually contains back onto
    the entry (exactly what `_apply_writes`/`_update_drive_queue_entry_local`
    would persist) — `retry_backoff_at` must never be one of them, and the
    entry must still relaunch once real elapsed time (measured from the
    ORIGINAL death, never refreshed) clears the widened dispatch-failure
    floor.
    """
    death = NOW
    live = entry(
        1650,
        position=3,
        state=STATE_WAITING,
        attempts=1,
        launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 400.0,
        retry_backoff_at=death,
    )
    # No board-visible assignment at all — the exact #2273 direction-2 tier
    # (DISPATCH_FAILURE_MIN_BACKOFF_SECONDS, 300s) this issue's incident hit.
    facts = IssueFacts(known=True, issue_state="open")
    view = BoardView(issues={entry_key(REPO, 1650): facts})

    # Several ticks, each only 60s apart — shorter than the 300s floor —
    # simulating the production 180s timer against it. `age` measured from
    # the fixed `death` anchor never reaches 300s across any of these (max
    # 179s, at the last iteration), so every one must still defer.
    for tick_now in (death, death + 60, death + 120, death + 179):
        plan = plan_tick([live], view, capacity=1, now=tick_now)
        assert plan.launch is None
        backoff = [d for d in plan.deferrals if d.key == entry_key(REPO, 1650)]
        assert len(backoff) == 1 and backoff[0].backing_off is True
        # THE regression check: the deferral's own persisted write must
        # never touch the anchor it is itself measured against.
        assert "retry_backoff_at" not in backoff[0].updates
        live = entry(
            1650,
            position=3,
            state=STATE_WAITING,
            attempts=1,
            launched_at=live.launched_at,
            retry_backoff_at=live.retry_backoff_at,  # untouched, as above
            deferrals=live.deferrals + 1,
            last_reason=backoff[0].reason,
        )

    # 305s after the ORIGINAL death — past the 300s floor — relaunches.
    plan = plan_tick([live], view, capacity=1, now=death + 305)
    assert plan.launch is not None and plan.launch.issue == 1650


def test_omitting_the_clock_disables_the_backoff_entirely():
    """`now=None` is the pure-logic caller's opt-out — same posture #1794's
    own window already takes (`test_omitting_the_clock_disables_the_window_
    entirely`)."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles[0].outcome == "retry"
    assert plan.launch is not None and plan.launch.issue == 1650


def test_a_2230_resume_is_never_backed_off():
    """A `blocked` entry #2230 resumes on POSITIVE gate evidence must launch
    in the SAME tick, unpaced — its `attempts` reset to 0 is real evidence
    the condition cleared, unlike a plain `retry`, which has none."""
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_BLOCKED,
            attempts=DEFAULT_MAX_ATTEMPTS,
            last_reason="checks failed: lint",
            reason_at=NOW - 5.0,
        )
    ]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        now=NOW,
        live_blocked_gate={entry_key(REPO, 1650): False},
    )
    assert [r.outcome for r in plan.reconciles] == ["resumed"]
    assert plan.launch is not None and plan.launch.issue == 1650


def test_a_dispatch_failure_that_created_no_assignment_backs_off_longer():
    """#2273 direction 2: a died launch with NO board-visible assignment gets
    at least `DISPATCH_FAILURE_MIN_BACKOFF_SECONDS`, wider than the plain
    `RETRY_BACKOFF_SECONDS[0]` a code-side death would get."""
    launched_at = NOW - DRIVE_STARTUP_GRACE_SECONDS - 400.0
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_WAITING,
            attempts=1,
            launched_at=launched_at,
            # Comfortably past RETRY_BACKOFF_SECONDS[0] (60s) but nowhere
            # near DISPATCH_FAILURE_MIN_BACKOFF_SECONDS (300s).
            retry_backoff_at=NOW - 90.0,
        )
    ]
    facts = IssueFacts(known=True, issue_state="open")  # no assignment at all
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(entries, view, capacity=1, now=NOW)
    assert plan.launch is None
    backoff = [d for d in plan.deferrals if d.key == entry_key(REPO, 1650)]
    assert len(backoff) == 1 and backoff[0].backing_off is True


def test_a_merge_gate_block_retry_does_not_get_the_widened_backoff():
    """#2424 follow-up: identical setup to the test above (no board-visible
    assignment for this launch — by itself indistinguishable from a genuine
    dispatch failure) EXCEPT the entry's own `last_reason` already names a
    merge-gate block. `_retry_backoff_reason` must not widen the spacing to
    `DISPATCH_FAILURE_MIN_BACKOFF_SECONDS` (300s) on top of a reason that
    already says the real cause is a merge-gate block, not a dispatch
    failure — the same "one question, one answer" fix `_is_merge_gate_block_
    reason` already applies to the escalation text, now also applied to the
    retry-pacing decision that answers the same question independently."""
    launched_at = NOW - DRIVE_STARTUP_GRACE_SECONDS - 400.0
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_WAITING,
            attempts=1,
            launched_at=launched_at,
            # Same 90s elapsed as the widened-backoff test above: clears the
            # plain RETRY_BACKOFF_SECONDS[0] (60s) but not the widened 300s
            # floor.
            retry_backoff_at=NOW - 90.0,
            last_reason=(
                "merge attempted 3 times without landing. (attempt 2/5) — "
                "requeued at position 3"
            ),
        )
    ]
    facts = IssueFacts(known=True, issue_state="open")  # no assignment at all
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(entries, view, capacity=1, now=NOW)
    assert plan.launch is not None and plan.launch.issue == 1650


def test_a_dispatched_run_that_died_later_gets_the_plain_backoff_only():
    """The counterpart: a launch that DID dispatch (an assignment exists,
    created after `launched_at`) is NOT treated as a pure dispatch failure —
    90s already clears the plain 60s backoff even though it would not clear
    the widened one."""
    launched_at = NOW - DRIVE_STARTUP_GRACE_SECONDS - 400.0
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_WAITING,
            attempts=1,
            launched_at=launched_at,
            retry_backoff_at=NOW - 90.0,
        )
    ]
    facts = IssueFacts(
        known=True, issue_state="open", last_dispatched_at=launched_at + 5.0
    )
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(entries, view, capacity=1, now=NOW)
    assert plan.launch is not None and plan.launch.issue == 1650


def test_exhausted_reason_names_a_dispatch_only_failure():
    """The give-up escalation text itself must say "no assignment" plainly —
    the point of #2273 direction 3 is that a human reading it does not need
    to reconstruct that from a bare exit code.

    No `exit_reasons` entry is supplied here, so `own_reason` is empty —
    this is the #2442 reap shape (a killed/crashed session, not a clean
    exit): the note must name the effect ("no assignment was ever created")
    without asserting the unknowable cause. See
    `test_exhausted_dispatch_only_with_own_reason_still_names_infra_failure`
    for the sibling case where `own_reason` IS present and the confident
    "likely an infrastructure/dispatch-layer failure" wording is still
    correct."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=DEFAULT_MAX_ATTEMPTS - 1,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    facts = IssueFacts(known=True, issue_state="open")
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(entries, view, capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "exhausted"
    reason = plan.blocked[0].reason
    assert "no assignment was ever created for this run" in reason
    assert "the session left no exit reason to diagnose why" in reason
    assert "infrastructure/dispatch-layer failure" not in reason


def test_exhausted_dispatch_only_with_own_reason_still_names_infra_failure():
    """#2442 sibling: when `own_reason` IS present (a clean exit, not a
    reap) and it does not name a merge-gate block, the confident "likely an
    infrastructure/dispatch-layer failure" wording is still correct — only
    the reap-without-exit-reason case (no `own_reason` at all) gets the
    softened wording."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=DEFAULT_MAX_ATTEMPTS - 1,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    own_reason = "drive exited for claude-coordinator#1650 (exit_code=1): boom"
    facts = IssueFacts(known=True, issue_state="open")
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(
        entries, view, capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "exhausted"
    reason = plan.blocked[0].reason
    assert "no assignment was ever created for this run" in reason
    assert "likely an infrastructure/dispatch-layer failure" in reason
    assert "left no exit reason to diagnose why" not in reason


def test_exhausted_merge_gate_block_does_not_get_the_dispatch_note():
    """#2424: claude-coordinator#2405 / coord-web#2 — a relaunch whose only
    job is retrying the Merge stage dispatches no NEW assignment by design
    (Work/Test/Review already completed), so `_dispatch_produced_nothing`'s
    comparison reads exactly like a genuine dispatch failure. The own exit
    reason already names the real cause (a merge-gate block); the #2273 note
    must not be layered on top of it, or an operator reading the escalation
    is misdirected toward `drive-queue remove && add` (a wasted Work/Test/
    Review cycle) instead of `coord merge --revalidate`."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=DEFAULT_MAX_ATTEMPTS - 1,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    own_reason = (
        "drive exited for claude-coordinator#1650 (exit_code=1): merge "
        "attempted 3 times without landing.\n"
        "   Last board state: status='CONFLICT' reason='smoke test verdict "
        "is stale'"
    )
    # No assignment dispatched AFTER `launched_at` — exactly the shape a
    # merge-only relaunch produces, and exactly what used to trip the #2273
    # note despite three real assignments already having done real work.
    facts = IssueFacts(known=True, issue_state="open")
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(
        entries, view, capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "exhausted"
    reason = plan.blocked[0].reason
    assert own_reason in reason
    assert "no assignment was ever created" not in reason
    assert "infrastructure/dispatch-layer failure" not in reason


def test_exhausted_checks_failed_does_not_get_the_dispatch_note():
    """Same #2424 fix, the red-CI shape (coord-web#2): `own_reason` names a
    `checks_failed` merge-gate block, not a dispatch failure."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=DEFAULT_MAX_ATTEMPTS - 1,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    own_reason = (
        "drive exited for claude-coordinator#1650 (exit_code=1): merge "
        "attempted 3 times without landing.\n"
        "   Last board state: status='CONFLICT' reason='checks failed: "
        "checks'"
    )
    facts = IssueFacts(known=True, issue_state="open")
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(
        entries, view, capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "exhausted"
    reason = plan.blocked[0].reason
    assert "no assignment was ever created" not in reason


def test_exhausted_immediate_escalation_merge_status_does_not_get_the_dispatch_note():
    """#2424's fourth documented shape: `_escalate_merge`'s #1505
    immediate-escalation path (`coord/drive.py:2809`), whose `Action.message`
    is `f"merge escalated: {reason}\\n   gates: {gates_summary}\\n..."`
    (`coord/drive.py:2836`) and whose `reason` for a terminal/unrecognized
    merge status is `f"merge_status={status or '(empty)'} — no number of
    retries..."`. `_drive_exit_summary` then wraps that whole message as
    `f"drive exited for {ident} (exit_code={exit_code}): {reason}"`
    (`coord/drive.py:3677`) before it is read back verbatim as `own_reason` —
    so the real string never STARTS WITH "merge_status=", it merely CONTAINS
    it (both in the `reason` line and again in `gates_summary`'s own
    `merge_status=...` pair). A prior `.startswith("merge_status=")` check
    left this one shape unmatched — dead code against real data — so a drive
    that hit this exact path still got the false #2273 dispatch-note."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=DEFAULT_MAX_ATTEMPTS - 1,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    own_reason = (
        "drive exited for claude-coordinator#1650 (exit_code=1): merge "
        "escalated: merge_status=CONFLICT — no number of retries changes "
        "this; escalating on first encounter instead of burning the "
        "merge-attempt budget (#1505)\n"
        "   gates: merge_status=CONFLICT | merge_reason=(none) | "
        "review_verdict=approved | test_state=passed | pr_url=(none)\n"
        "   proposed: coord merge --plan --repo claude-coordinator   "
        "# inspect the gates, then decide\n"
        "   Recorded on the board — see: coord escalate list --repo "
        "claude-coordinator"
    )
    facts = IssueFacts(known=True, issue_state="open")
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(
        entries, view, capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "exhausted"
    reason = plan.blocked[0].reason
    assert own_reason in reason
    assert "no assignment was ever created" not in reason
    assert "infrastructure/dispatch-layer failure" not in reason


def test_exhausted_empty_branch_advisory_does_not_get_the_dispatch_note():
    """#2334: the queue's own misdiagnosis half of the issue. `own_reason`
    here is `_decide_advisory`'s #2416 exhaustion message — `coord drive`
    read a terminal, zero-commit ADVISORY row, spent its own bounded
    `coord retry` budget on it, and gave up. That is `coord drive`
    DECLINING to dispatch anything further, not a dispatch-layer failure —
    the #2273 note ("likely an infrastructure/dispatch-layer failure, not a
    code defect") directly contradicts `own_reason`'s own "this needs an
    operator decision: coord retry ..." wording, exactly the space-invaders#3
    incident the issue documents. `_is_empty_branch_death_reason` is the
    same additive-note guard `_is_merge_gate_block_reason` already provides
    for a merge-gate death — see the sibling tests above."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=EMPTY_BRANCH_MAX_ATTEMPTS - 1,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    own_reason = (
        "drive exited for claude-coordinator#1650 (exit_code=1): work w1 "
        "exited ADVISORY with no commits on its branch 1 time(s) in a row "
        "(budget 1, #2416) — nothing was pushed, so there is nothing to "
        "test, review, or merge, and retrying has not produced a different "
        "outcome.\n"
        "   inspect: coord log w1 --machine precision\n"
        "   this needs an operator decision: coord retry w1 by hand once "
        "the underlying blocker is understood, or dispatch an independent "
        "follow-up issue if another attempt at this one is not the right "
        "fix."
    )
    facts = IssueFacts(known=True, issue_state="open")
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(
        entries, view, capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "exhausted"
    reason = plan.blocked[0].reason
    assert own_reason in reason
    assert "no assignment was ever created" not in reason
    assert "infrastructure/dispatch-layer failure" not in reason


def test_exhausted_empty_branch_acceptance_author_does_not_get_the_dispatch_note():
    """#2334's own mirrored acceptance-author shape — the exact wording
    `_decide_acceptance_author`'s bounded retry now produces once its
    budget is spent (claude-coordinator#2531: six attempts burned on this
    exact reason, each also carrying the misleading dispatch-layer note)."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=EMPTY_BRANCH_MAX_ATTEMPTS - 1,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    own_reason = (
        "drive exited for claude-coordinator#1650 (exit_code=1): "
        "acceptance author ta1 exited ADVISORY with no commits on its "
        "branch 1 time(s) in a row (budget 1, #2334) — nothing was "
        "authored, so there is no slice to land, and retrying has not "
        "produced a different outcome."
    )
    facts = IssueFacts(known=True, issue_state="open")
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(
        entries, view, capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "exhausted"
    reason = plan.blocked[0].reason
    assert own_reason in reason
    assert "no assignment was ever created" not in reason
    assert "infrastructure/dispatch-layer failure" not in reason


def test_a_genuine_dispatch_failure_still_gets_the_note_alongside_own_reason():
    """Regression guard for #2424's fix: an own_reason that does NOT name a
    merge-gate block (a genuine pre-`coord assign` crash) must still get the
    #2273 note — the fix narrows the false-positive, it does not remove the
    signal entirely."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=DEFAULT_MAX_ATTEMPTS - 1,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    own_reason = (
        "drive exited for claude-coordinator#1650 (exit_code=1): unhandled "
        "exception in dispatch"
    )
    facts = IssueFacts(known=True, issue_state="open")
    view = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(
        entries, view, capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "exhausted"
    reason = plan.blocked[0].reason
    assert own_reason in reason
    assert "no assignment was ever created for this run" in reason


def test_a_dead_drive_out_of_attempts_blocks_and_escalates():
    entries = [
        entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles[0].outcome == "exhausted"
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1650)]
    assert plan.blocked[0].updates["state"] == STATE_BLOCKED
    assert plan.blocked[0].updates["attempts"] == DEFAULT_MAX_ATTEMPTS
    assert plan.launch is None


def test_max_attempts_is_injectable():
    entries = [entry(1650, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1, max_attempts=1)
    assert plan.reconciles[0].outcome == "exhausted"


# ── plan_tick: the drive's own exit reason wins over "died" (#1845/#1844) ────
#
# `_reconcile_running`'s death branch ("no session, no active work, nothing
# landed") also matches a drive that exited DELIBERATELY — a clean
# `exit_code=1` after diagnosing its own blocker (a merge-queue race in
# #1845, an oracle refusal in #1844) — and used to overwrite that already-
# recorded diagnosis with a synthesised "drive session died" every time. The
# shell reads the drive's own `drive_exited` audit summary and hands it in as
# `exit_reasons`; these tests pin what `_reconcile_running` does with it.


def test_a_dead_drives_own_exit_reason_replaces_the_synthesised_death():
    """#1845: a drive that exited on its own diagnosis must have THAT
    diagnosis carried forward as `last_reason`, not "drive session died"."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    own_reason = (
        "drive exited for api#1650 (exit_code=1): merge attempted 3 times "
        "without landing."
    )
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "retry"  # state transition is unchanged
    assert own_reason in reconcile.reason
    assert own_reason in reconcile.updates["last_reason"]
    assert "drive session died" not in reconcile.reason


def test_an_exhausted_drives_own_exit_reason_replaces_the_synthesised_death():
    """Same fix, at the `exhausted` branch — no regression on retry exhausting
    to `blocked` (the path that recovered #1845's overnight incidents)."""
    entries = [
        entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    own_reason = (
        "drive exited for api#1650 (exit_code=1): a permanent refusal, not "
        "a crash"
    )
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "exhausted"
    assert own_reason in plan.blocked[0].reason
    assert own_reason in plan.blocked[0].updates["last_reason"]
    assert "drive session died" not in plan.blocked[0].reason
    # The reason changed; the outcome — still exhausts, still blocks — did not.
    assert plan.blocked[0].updates["state"] == STATE_BLOCKED
    assert plan.blocked[0].updates["attempts"] == DEFAULT_MAX_ATTEMPTS


def test_no_exit_reason_falls_back_to_the_synthesised_death_wording():
    """No regression: a genuine crash (no `drive_exited` row, or the shell's
    audit fetch came back empty) keeps the pre-#1845 wording exactly."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1, exit_reasons={})
    assert (
        "drive session died without landing the work"
        in plan.reconciles[0].reason
    )


# ═══════════════════════════════════════════════════════════════════════════
# #2363: the "claimed success, wrote nothing" signature — an acceptance-author
# or plain work session that exited DONE/ADVISORY claiming success while its
# branch carried zero commits (`coord/drive.py:850-865`, `:887-918`, and
# `_decide_advisory`) — gets a WIDER attempt budget than
# `DEFAULT_MAX_ATTEMPTS` before blocking. Every recorded instance of this
# shape in `~/.coord/queue-block-log.jsonl` self-healed 0% of the time inside
# the default ceiling (the issue's own evidence table); this is additive —
# every OTHER death reason keeps today's flat ceiling unchanged.
# ═══════════════════════════════════════════════════════════════════════════

ACCEPTANCE_ADVISORY_EMPTY_BRANCH_REASON = (
    "acceptance author api#42 exited ADVISORY with no commits on its branch "
    "— nothing was authored, so there is no slice to land.\n"
    "   inspect: coord log api#42 --machine dellserver\n"
    "   Continue by hand, or re-run coord drive with --no-acceptance to "
    "skip JIT authoring."
)
ACCEPTANCE_DONE_EMPTY_BRANCH_REASON = (
    "acceptance author api#42 exited DONE, but its branch 'work/api-1650' "
    "carries no commits — nothing was authored, so there is no slice to "
    "land, and DONE is terminal: it will never change on its own.\n"
    "   inspect: coord log api#42 --machine dellserver\n"
    "   Re-author by hand: coord acceptance author claude-coordinator "
    "1600 --issue 1650\n"
    "   or re-run coord drive with --no-acceptance to skip JIT authoring."
)
WORK_ADVISORY_EMPTY_BRANCH_REASON = (
    "work api#42 exited ADVISORY with no commits on its branch —\n"
    "   nothing was pushed, so there is nothing to test, review, or merge.\n"
    "   inspect: coord log api#42 --machine dellserver"
)


@pytest.mark.parametrize(
    "own_reason",
    [
        ACCEPTANCE_ADVISORY_EMPTY_BRANCH_REASON,
        ACCEPTANCE_DONE_EMPTY_BRANCH_REASON,
        WORK_ADVISORY_EMPTY_BRANCH_REASON,
    ],
)
def test_empty_branch_death_survives_past_the_default_attempt_ceiling(own_reason):
    """#2363 acceptance: a `DEFAULT_MAX_ATTEMPTS`-th death of this shape must
    NOT yet be blocked — the whole point is a wider budget than the flat
    ceiling every other death reason gets."""
    entries = [
        entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "retry"
    assert plan.blocked == ()
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == DEFAULT_MAX_ATTEMPTS
    assert f"{DEFAULT_MAX_ATTEMPTS}/{EMPTY_BRANCH_MAX_ATTEMPTS}" in reconcile.reason
    assert "#2363" in reconcile.reason
    assert own_reason in reconcile.reason


def test_empty_branch_death_still_blocks_once_its_own_wider_budget_is_exhausted():
    """No silent infinite retry: once `EMPTY_BRANCH_MAX_ATTEMPTS` is itself
    hit, the entry blocks with the same diagnosis-and-recovery text the
    drive itself wrote (`own_reason`, verbatim — the `coord log`/`coord
    acceptance author` instructions an operator would otherwise have to
    reconstruct by hand)."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=EMPTY_BRANCH_MAX_ATTEMPTS - 1,
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): ACCEPTANCE_DONE_EMPTY_BRANCH_REASON},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "exhausted"
    assert plan.blocked[0].updates["state"] == STATE_BLOCKED
    assert plan.blocked[0].updates["attempts"] == EMPTY_BRANCH_MAX_ATTEMPTS
    assert ACCEPTANCE_DONE_EMPTY_BRANCH_REASON in plan.blocked[0].reason
    assert "coord acceptance author claude-coordinator 1600" in plan.blocked[0].reason


def test_a_non_empty_branch_death_keeps_the_default_attempt_ceiling():
    """Additive, not a global increase: an ordinary drive death (a genuine
    code-defect exit) must still block at the plain `DEFAULT_MAX_ATTEMPTS`,
    unaffected by #2363."""
    entries = [
        entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    own_reason = "drive exited for api#1650 (exit_code=1): a genuine test failure"
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "exhausted"
    assert plan.blocked[0].updates["attempts"] == DEFAULT_MAX_ATTEMPTS
    assert "#2363" not in plan.blocked[0].reason


def test_finished_with_no_branch_reason_is_not_classified_as_empty_branch_death():
    """The `_decide` 'done'-status-with-no-branch shape (`work ... finished
    with no branch — nothing was pushed (0-commit advisory)`) is a THIRD,
    narrower reading `coord/drive.py` can produce — it is not one of the two
    shapes #2363's acceptance criteria name, has no evidence of its own in
    `queue-block-log.jsonl`, and must keep the default ceiling rather than
    being silently folded into the wider one."""
    entries = [
        entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    own_reason = (
        "work api#1650 finished with no branch — nothing was pushed "
        "(0-commit advisory).\n"
        "   inspect: coord log api#1650 --machine dellserver"
    )
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "exhausted"
    assert plan.blocked[0].updates["attempts"] == DEFAULT_MAX_ATTEMPTS


# ═══════════════════════════════════════════════════════════════════════════
# #2411: `last_reason` must not go blind mid-backoff. Before this fix,
# `_backoff_reason` (`plan_tick`'s waiting walk) persisted `_retry_backoff_
# reason`'s purely mechanical "next attempt permitted in Ns" sentence as the
# entry's WHOLE `last_reason` on every tick spent backing off — overwriting
# the real death cause `_reconcile_running`'s `retry` branch had just
# recorded, visible for exactly one tick and then gone. An entry stuck
# backing off on a widened #2363 empty-branch budget (claude-coordinator
# #2005's own repro) could burn most of its 6-attempt ceiling with `coord
# drive-queue list` showing nothing but spacing text the whole time.
# ═══════════════════════════════════════════════════════════════════════════


def test_augment_backoff_reason_keeps_the_first_line_of_the_previous_reason():
    """Direct unit test of the combinator: the real cause leads, the fresh
    backoff sentence follows on its own line."""
    from coord.drive_queue import _augment_backoff_reason

    combined = _augment_backoff_reason(
        "work api#1650 exited ADVISORY with no commits on its branch — "
        "nothing was pushed (attempt 1/6) — requeued at position 3",
        "retry backoff: the previous attempt failed 5s ago, next attempt "
        "permitted in 55s (60s spacing after 1 attempt(s) — #2273, so a "
        "transient dispatch failure cannot spend the whole retry budget "
        "inside one tick cadence)",
    )
    lines = combined.splitlines()
    assert len(lines) == 2
    assert lines[0] == (
        "work api#1650 exited ADVISORY with no commits on its branch — "
        "nothing was pushed (attempt 1/6) — requeued at position 3"
    )
    # 3-space continuation indent, matching `coord/drive.py`'s own
    # multi-line exit reasons.
    assert lines[1].startswith("   retry backoff:")


def test_augment_backoff_reason_is_idempotent_across_repeated_calls():
    """#2411's anti-growth property: feeding a PREVIOUS combined (two-line)
    result back in as *previous_reason* — exactly what happens tick over
    tick, since the combined text is what gets persisted to `last_reason` —
    must not accumulate a third line. The base line the second call reads
    back out has to be the ORIGINAL cause, not the first call's backoff
    sentence."""
    from coord.drive_queue import _augment_backoff_reason

    first = _augment_backoff_reason(
        "own cause here", "retry backoff: attempt 1 text"
    )
    second = _augment_backoff_reason(first, "retry backoff: attempt 1 text, later")
    assert second.splitlines() == [
        "own cause here",
        "   retry backoff: attempt 1 text, later",
    ]


def test_augment_backoff_reason_falls_back_to_the_backoff_text_alone():
    """No prior reason to keep (a fresh row, or one hand-built in a test,
    same as `entry()`'s `last_reason=""` default) — degrades to exactly the
    pre-#2411 text."""
    from coord.drive_queue import _augment_backoff_reason

    assert _augment_backoff_reason("", "retry backoff: ...") == "retry backoff: ..."
    assert (
        _augment_backoff_reason("   \n  ", "retry backoff: ...")
        == "retry backoff: ..."
    )


def test_a_dead_drives_own_reason_survives_into_the_same_ticks_backoff():
    """A death and its backoff check can land in the SAME tick (a `running`
    entry reconciled straight to `waiting` in step 1, then walked for backoff
    in step 4 — see `test_a_dead_drive_with_a_real_clock_paces_its_next_
    attempt_2273`). The `own_reason` `_reconcile_running` just recorded must
    already be in the deferral's reason, not just on the NEXT tick — this is
    what `effective_last_reason` (the same "prefer this tick's fresh write"
    trick `effective_attempts`/`retry_backoff_at_map` already use) is for."""
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_RUNNING,
            attempts=0,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    own_reason = (
        "work api#1650 exited ADVISORY with no commits on its branch — "
        "nothing was pushed"
    )
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "retry"
    assert plan.launch is None
    backoff = [d for d in plan.deferrals if d.key == entry_key(REPO, 1650)]
    assert len(backoff) == 1 and backoff[0].backing_off is True
    assert own_reason in backoff[0].reason
    assert "retry backoff" in backoff[0].reason
    # The persisted write carries the same combined text an operator would
    # see on the next `coord drive-queue list` — the whole point of #2411.
    assert own_reason in backoff[0].updates["last_reason"]


def test_the_real_death_cause_survives_multiple_backoff_ticks_2411():
    """The full incident repro: an empty-branch death (#2363's widened
    budget — claude-coordinator#2005's own shape) sits `waiting` across
    SEVERAL ticks of backoff. Every tick's `last_reason` must still name the
    real cause on its first line — not just the tick that recorded the
    death — and the text must never grow past two lines."""
    own_reason = (
        "acceptance author api#42 exited DONE, but its branch carries no "
        "commits — nothing was authored, so there is no slice to land"
    )
    launched_at = NOW - DRIVE_STARTUP_GRACE_SECONDS - 1
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_RUNNING,
            attempts=0,
            launched_at=launched_at,
        )
    ]
    # No board-visible assignment (`last_dispatched_at` unset) — the #2273
    # dispatch-only tier, so the backoff floor is
    # DISPATCH_FAILURE_MIN_BACKOFF_SECONDS (300s), comfortably wider than
    # the plain RETRY_BACKOFF_SECONDS[0] (60s) this test's tick spacing
    # would otherwise outrun.
    facts = IssueFacts(known=True, issue_state="open")
    view = BoardView(issues={entry_key(REPO, 1650): facts})

    # Tick 1: the death itself. Not yet backing off (retry_backoff_at == now
    # == age 0), so this only pins the recorded reason.
    plan = plan_tick(
        entries, view, capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "retry"
    assert own_reason in reconcile.updates["last_reason"]
    # The FULL death-tick reason (own_reason plus the #2273/#2363 notes
    # `_reconcile_running` appends) — this, not the bare `own_reason`, is
    # what every later backoff tick's first line must keep verbatim.
    death_reason = reconcile.updates["last_reason"]
    assert "#2363" in death_reason  # sanity: this IS the widened-budget shape
    live = entry(
        1650,
        position=3,
        state=STATE_WAITING,
        attempts=reconcile.updates["attempts"],
        launched_at=launched_at,
        last_reason=reconcile.updates["last_reason"],
        retry_backoff_at=reconcile.updates["retry_backoff_at"],
    )

    # Several later ticks, comfortably inside the widened dispatch-failure
    # backoff floor (DISPATCH_FAILURE_MIN_BACKOFF_SECONDS == 300s — no
    # board-visible assignment was ever created for this synthetic entry).
    for tick_now in (NOW + 30, NOW + 90, NOW + 200):
        plan = plan_tick([live], view, capacity=1, now=tick_now)
        assert plan.launch is None
        backoff = [d for d in plan.deferrals if d.key == entry_key(REPO, 1650)]
        assert len(backoff) == 1 and backoff[0].backing_off is True
        reason = backoff[0].updates["last_reason"]
        lines = reason.splitlines()
        assert len(lines) == 2, f"expected exactly 2 lines, got: {lines!r}"
        assert lines[0] == death_reason
        assert lines[1].strip().startswith("retry backoff:")
        live = entry(
            1650,
            position=3,
            state=STATE_WAITING,
            attempts=live.attempts,
            launched_at=live.launched_at,
            retry_backoff_at=live.retry_backoff_at,
            deferrals=live.deferrals + 1,
            last_reason=reason,
        )


# ═══════════════════════════════════════════════════════════════════════════
# #1891: `parked` — a CI verdict that has not arrived must not consume merge
# budget. Same "no session, no active work, nothing landed" evidence as
# `retry`/`exhausted`, but the board's OWN current read of the issue names
# nothing stronger than "CI checks have not reported yet"
# (`IssueFacts.merge_ci_pending`) — so this goes straight to `STATE_PARKED`
# instead: no attempt spent, no `blocked`, no escalation, and — unlike
# `blocked` — no operator command needed to release it. See
# `coord.drive_queue.STATE_PARKED`'s docstring.
# ═══════════════════════════════════════════════════════════════════════════


def test_a_dead_drive_still_ci_pending_parks_without_spending_an_attempt():
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(ci_pending=(1650,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "parked"
    assert reconcile.updates["state"] == "parked"
    assert "attempts" not in reconcile.updates
    assert plan.blocked == ()
    assert plan.launch is None  # nothing to launch — it is parked, not waiting


def test_a_parked_entry_never_reaches_blocked_even_deep_into_the_attempt_budget():
    """The whole point: an entry that would have exhausted retries (attempts
    already at max_attempts - 1) still parks, not blocks, when the board
    shows nothing but CI silence — attempts genuinely never move."""
    entries = [
        entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    plan = plan_tick(entries, board(ci_pending=(1650,)), capacity=1)
    assert plan.reconciles[0].outcome == "parked"
    assert plan.blocked == ()
    assert plan.alert is None


# ═══════════════════════════════════════════════════════════════════════════
# #2858: a `running` entry whose board `issues` cache row is STALE must not
# have a negative `landed` reading (still "open") trusted enough to spend a
# retry/exhausted attempt on — the cache may simply not have caught up with
# a merge/close yet (a starved `coord.serve_app._sync_issues_tick`). Same
# "park, don't spend an attempt" treatment as #1891's CI-pending case just
# above; `_issue_cache_stale` is the predicate, `ISSUE_CACHE_STALE_CEILING_S`
# the threshold (aliased from `coord.issues_sync_status.
# STALENESS_WARN_SECONDS` — the same point `coord.health` starts warning).
# ═══════════════════════════════════════════════════════════════════════════


def _stale_open_board(*, synced_at: float, active: bool = False) -> BoardView:
    facts = IssueFacts(
        known=True, issue_state="open", issue_synced_at=synced_at, active_work=active,
    )
    return BoardView(issues={entry_key(REPO, 1650): facts})


def test_a_dead_drive_with_a_stale_cache_parks_instead_of_retrying():
    from coord.drive_queue import ISSUE_CACHE_STALE_CEILING_S

    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    stale_board = _stale_open_board(
        synced_at=NOW - ISSUE_CACHE_STALE_CEILING_S - 1.0
    )
    plan = plan_tick(entries, stale_board, capacity=1, now=NOW)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "parked"
    assert reconcile.updates["state"] == "parked"
    assert "attempts" not in reconcile.updates
    assert plan.blocked == ()
    assert plan.launch is None
    assert "stale" in reconcile.reason


def test_a_stale_cache_park_never_reaches_blocked_even_deep_into_the_budget():
    """Mirrors the #1891 CI-pending sibling: attempts genuinely never move,
    however close to the ceiling the entry already was."""
    from coord.drive_queue import ISSUE_CACHE_STALE_CEILING_S

    entries = [entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)]
    stale_board = _stale_open_board(
        synced_at=NOW - ISSUE_CACHE_STALE_CEILING_S - 1.0
    )
    plan = plan_tick(entries, stale_board, capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "parked"
    assert plan.blocked == ()
    assert plan.alert is None


def test_a_dead_drive_with_a_fresh_cache_still_retries_normally():
    """A cache row synced comfortably within the ceiling is trusted exactly
    as before #2858 — the negative `landed` reading stands, and a dead
    drive with nothing else going for it still retries."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    fresh_board = _stale_open_board(synced_at=NOW - 5.0)
    plan = plan_tick(entries, fresh_board, capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "retry"


def test_a_dead_drive_with_no_synced_at_at_all_still_retries_normally():
    """#2858 backward compatibility: `issue_synced_at=None` (every board
    payload/test fixture that predates this field, or a board with no
    `issues` row at all for this key) must never be treated as stale — the
    safe default is trusting the cache exactly as it always did."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(open_=(1650,)), capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "retry"


def test_stale_cache_never_overrides_a_positive_landed_reading():
    """Staleness only ever softens a NEGATIVE `landed` reading — a stale
    cache that already shows the issue merged/closed is still trusted
    outright and reconciles straight to `done`, same as always."""
    from coord.drive_queue import ISSUE_CACHE_STALE_CEILING_S

    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    facts = IssueFacts(
        known=True, issue_state="closed",
        issue_synced_at=NOW - ISSUE_CACHE_STALE_CEILING_S - 1.0,
    )
    stale_but_closed = BoardView(issues={entry_key(REPO, 1650): facts})
    plan = plan_tick(entries, stale_but_closed, capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "done"


def test_a_parked_entry_resumes_to_waiting_and_launches_once_ci_reports():
    """Acceptance: a parked entry resumes automatically on a later tick once
    checks report, with no operator command — modelled here as the SAME
    entry, now persisted as `parked`, ticked again against a board that no
    longer shows `merge_ci_pending` for it."""
    entries = [entry(1650, position=3, state="parked", attempts=0)]
    plan = plan_tick(entries, board(), capacity=1)  # ci_pending cleared
    resumed = [r for r in plan.reconciles if r.key == entry_key(REPO, 1650)]
    assert [r.outcome for r in resumed] == ["resumed"]
    assert resumed[0].updates["state"] == STATE_WAITING
    # #2273 post-review: resets `attempts` to 0 on resume, same as #2230's
    # blocked-resume — positive evidence the gate cleared, so the NEXT
    # launch (if this one also dies) must not be paced against a stale
    # attempt count left over from before this entry ever parked.
    assert resumed[0].updates["attempts"] == 0
    # …and it falls straight into this SAME tick's launch selection — no
    # human, no separate tick required.
    assert plan.launch is not None and plan.launch.issue == 1650


def test_a_still_parked_entry_is_not_relaunched_while_ci_is_still_pending():
    entries = [entry(1650, position=3, state="parked", attempts=0)]
    plan = plan_tick(entries, board(ci_pending=(1650,)), capacity=1)
    assert plan.reconciles == ()  # still gated — nothing to report or write
    assert plan.launch is None


# ── #2347: rewrite `last_reason` while still parked, when the real cause has
# become "GitHub unreachable" ────────────────────────────────────────────────
#
# The gap: #1891/#1892's "CONFIRMED still shut ⇒ no reconcile, no write,
# nothing to report" rule (see `test_a_still_parked_entry_is_not_relaunched_
# while_ci_is_still_pending` above) is correct for a genuine ongoing wait —
# but leaves `last_reason` FROZEN at whatever it said when the entry first
# parked, even across a run of *unrelated* transient GitHub API failures on
# every later tick's own re-check. The observed incident: a stale "CI
# running: … launched 1691s ago" reason survived a run of HTTP 503s for most
# of `PARK_STALE_SECONDS` with no operator-visible signal that GitHub — not
# CI — was the actual blocker. `live_ci_gate_reason` carries this tick's
# fresh reading through so the still-parked entry's `last_reason` can be
# corrected without resuming it or spending an attempt.


def test_plan_tick_rewrites_last_reason_when_still_parked_but_now_unreadable():
    entries = [
        entry(
            2347, position=3, state="parked", attempts=0,
            last_reason="CI running: build, lint (launched 1691s ago)",
        )
    ]
    live_reason = (
        "CI unreadable: coord: could not read CI status for acme/api#99 "
        "(HTTP 503) (unknown) — GitHub could not be reached to read CI "
        "status; this is not a CI result"
    )
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        live_ci_gate={entry_key(REPO, 2347): True},
        live_ci_gate_reason={entry_key(REPO, 2347): live_reason},
    )
    reconciles = [r for r in plan.reconciles if r.key == entry_key(REPO, 2347)]
    assert [r.outcome for r in reconciles] == ["reparked"]
    reconcile = reconciles[0]
    assert "state" not in reconcile.updates  # stays parked — no state write
    assert "attempts" not in reconcile.updates  # no attempt spent
    assert "GitHub could not be reached" in reconcile.updates["last_reason"]
    assert plan.launch is None


def test_plan_tick_does_not_rewrite_when_the_reason_is_already_current():
    """No duplicate reconcile spam once `last_reason` already reflects the
    live GitHub-unreachable reading."""
    live_reason = "CI unreadable: coord: could not read CI status (unknown)"
    entries = [
        entry(2347, position=3, state="parked", attempts=0, last_reason=live_reason)
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        live_ci_gate={entry_key(REPO, 2347): True},
        live_ci_gate_reason={entry_key(REPO, 2347): live_reason},
    )
    assert not any(r.key == entry_key(REPO, 2347) for r in plan.reconciles)


def test_plan_tick_still_reports_nothing_with_no_live_reason_supplied():
    """Baseline unchanged: absent `live_ci_gate_reason` (the entry isn't
    CI-unreadable, or the shell's live check couldn't classify it) keeps
    #1891/#1892's original "still shut ⇒ no reconcile, no write" behaviour —
    #2347 only ADDS a narrower case, never removes the baseline."""
    entries = [
        entry(
            2347, position=3, state="parked", attempts=0,
            last_reason="CI running: build, lint",
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        live_ci_gate={entry_key(REPO, 2347): True},
    )
    assert not any(r.key == entry_key(REPO, 2347) for r in plan.reconciles)


def test_plan_tick_does_not_rewrite_a_still_pending_reason_as_unreadable():
    """Only a live reading that is ITSELF `CI_UNREADABLE_PREFIX`-shaped
    triggers the rewrite — a fresh, still-genuinely-pending reading (the
    live re-check succeeded, CI is simply still running) must not be
    misread as the #2347 case."""
    entries = [
        entry(
            2347, position=3, state="parked", attempts=0,
            last_reason="CI running: build, lint",
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        live_ci_gate={entry_key(REPO, 2347): True},
        live_ci_gate_reason={entry_key(REPO, 2347): "CI running: build, lint, windows"},
    )
    assert not any(r.key == entry_key(REPO, 2347) for r in plan.reconciles)


# ── #2556: a terminal (not just pending) live CI reading must resume a
# `parked` entry, and must do so even at capacity ──────────────────────────
#
# The gap #2347 left behind: `live_ci_gate[key] = True` means only "the fresh
# read isn't PLAN_READY" — true both while checks are still genuinely running
# AND once they've reported a confirmed FAILURE. Before this, both shapes hit
# the same "still shut ⇒ stay parked" branch, and only the narrower
# `CI_UNREADABLE_PREFIX` shape got its `last_reason` rewritten. A completed,
# failing run was therefore indistinguishable from a slow one — coord-portal
# #131 sat parked 1h40m past its watched check reporting FAILURE, with the
# `last_reason` still reading "CI running: ..." the whole time.


def test_a_parked_entry_resumes_on_a_confirmed_ci_failure():
    """The core regression: a fresh, terminal ("CI failed: ...") live reading
    must resume the entry from `parked`, not re-confirm the park — so it
    falls into the normal `waiting`/launch path and a relaunched `coord
    drive` can route it through the existing checks_failed handling."""
    entries = [
        entry(
            2556, position=3, state="parked", attempts=0,
            last_reason="CI running: e2e smoke (playwright), launched 2789s ago",
        )
    ]
    live_reason = "CI failed: e2e smoke (playwright) (FAILURE)"
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        live_ci_gate={entry_key(REPO, 2556): True},
        live_ci_gate_reason={entry_key(REPO, 2556): live_reason},
    )
    resumed = [r for r in plan.reconciles if r.key == entry_key(REPO, 2556)]
    assert [r.outcome for r in resumed] == ["resumed"]
    assert resumed[0].updates["state"] == STATE_WAITING
    # No attempt spent — a fresh GitHub read is not a failed launch attempt.
    assert resumed[0].updates["attempts"] == 0
    assert "CI failed" in resumed[0].updates["last_reason"]
    # Falls straight into this SAME tick's launch selection.
    assert plan.launch is not None and plan.launch.issue == 2556


def test_a_parked_entry_resumes_on_confirmed_ci_failure_even_at_capacity():
    """Acceptance: this re-evaluation happens even when the fleet is fully
    occupied — #1891 step 1b runs before the capacity check, not after, so a
    full fleet must never freeze a parked row that has real evidence to act
    on."""
    running = running_since(1762, 5.0)
    parked = entry(
        2556, position=3, state="parked", attempts=0,
        last_reason="CI running: e2e smoke (playwright), launched 2789s ago",
    )
    live_reason = "CI failed: e2e smoke (playwright) (FAILURE)"
    plan = plan_tick(
        [running, parked],
        board(sessions=(1762,)),
        capacity=1,  # the one slot is already occupied by `running`
        now=NOW,
        live_ci_gate={entry_key(REPO, 2556): True},
        live_ci_gate_reason={entry_key(REPO, 2556): live_reason},
    )
    assert plan.occupied == 1
    assert plan.free_slots == 0
    resumed = [r for r in plan.reconciles if r.key == entry_key(REPO, 2556)]
    assert [r.outcome for r in resumed] == ["resumed"]
    assert resumed[0].updates["state"] == STATE_WAITING
    # No free slot, so nothing launches this tick — but the row itself has
    # left `parked`, which is the whole point: it no longer wedges silently.
    assert plan.launch is None


def test_a_parked_entry_still_stays_parked_while_genuinely_pending():
    """Baseline unchanged: a fresh reading that is STILL `CI_PENDING_PREFIX`
    shaped (checks exist, still running) must not be mistaken for a terminal
    read — this is the ordinary #1891/#2182 "still shut" case."""
    entries = [
        entry(
            2556, position=3, state="parked", attempts=0,
            last_reason="CI running: e2e smoke (playwright), launched 2789s ago",
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        live_ci_gate={entry_key(REPO, 2556): True},
        live_ci_gate_reason={
            entry_key(REPO, 2556): "CI running: e2e smoke (playwright)"
        },
    )
    assert not any(r.key == entry_key(REPO, 2556) for r in plan.reconciles)
    assert plan.launch is None


# ── #2158: a park that cannot refresh itself must age out ──────────────────


def test_a_park_on_an_unrefreshable_reading_ages_out_to_waiting():
    """THE #2158 regression, at the decision level.

    `merge_ci_pending` here is `merge_ci_pending_live=False` — it came only
    from the raw `merge_queue` row's persisted `error`, which no read path
    rewrites. Nothing on this tick's lane can ever refresh it (the board has
    no `merge_plan` section — the daemon-host tick — or the plan carried no
    rollup), so past `PARK_STALE_SECONDS` the tick stops believing it rather
    than holding the entry forever.
    """
    entries = [
        entry(
            2138, position=3, state="parked", attempts=0,
            last_reason="CI running: test (3.12) — parking without spending an attempt",
            reason_at=NOW - PARK_STALE_SECONDS - 60,
        )
    ]
    plan = plan_tick(entries, board(ci_pending=(2138,)), capacity=1, now=NOW)
    resumed = [r for r in plan.reconciles if r.key == entry_key(REPO, 2138)]
    assert [r.outcome for r in resumed] == ["resumed"]
    assert resumed[0].updates["state"] == STATE_WAITING
    # #2273 post-review: see the #1891 resume test above — same reset.
    assert resumed[0].updates["attempts"] == 0
    assert "#2158" in resumed[0].reason
    # It does NOT claim CI reported — nothing here knows that.
    assert "have reported" not in resumed[0].reason
    # …and falls into this same tick's launch selection.
    assert plan.launch is not None and plan.launch.issue == 2138


def test_a_park_on_an_unrefreshable_reading_is_held_until_the_ceiling():
    """No hot loop: the ceiling is a backstop, not a second CI timeout. A
    park younger than it stays exactly where it is."""
    entries = [
        entry(
            2138, position=3, state="parked", attempts=0,
            reason_at=NOW - PARK_STALE_SECONDS + 60,
        )
    ]
    plan = plan_tick(entries, board(ci_pending=(2138,)), capacity=1, now=NOW)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_park_on_a_live_plan_reason_never_ages_out():
    """A reading the board re-derives on every build is not stale, however
    old the park is — it will go false by itself the moment CI reports, and
    resuming over a live objection is the hot loop #1891 exists to avoid."""
    entries = [
        entry(
            2138, position=3, state="parked", attempts=0,
            reason_at=NOW - 30 * 3600,  # 30 hours, far past the ceiling
        )
    ]
    plan = plan_tick(entries, board(ci_pending_live=(2138,)), capacity=1, now=NOW)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_park_with_no_capture_time_stays_parked():
    """Fail closed on an unmeasurable age: a row predating #2133's `reason_at`
    (or one whose `last_reason` is still '') must degrade to today's
    behaviour, not to a park that expires by accident."""
    entries = [entry(2138, position=3, state="parked", attempts=0, reason_at=None)]
    plan = plan_tick(entries, board(ci_pending=(2138,)), capacity=1, now=NOW)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_park_stamped_in_the_future_stays_parked():
    """A clock that jumped backwards must not expire a park it cannot age."""
    entries = [
        entry(2138, position=3, state="parked", attempts=0, reason_at=NOW + 10_000)
    ]
    plan = plan_tick(entries, board(ci_pending=(2138,)), capacity=1, now=NOW)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_pure_logic_tick_with_no_clock_never_expires_a_park():
    """`plan_tick` still reads no clock of its own — a caller that passes none
    gets the pre-#2158 behaviour, not an entry aged against `None`."""
    entries = [entry(2138, position=3, state="parked", attempts=0, reason_at=1.0)]
    plan = plan_tick(entries, board(ci_pending=(2138,)), capacity=1)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_an_aged_gate_a_park_is_still_gated_on_the_human():
    """#2063 stays fail-closed THROUGH the #2158 expiry: a Gate-A park waits
    on a human, not on CI, so ageing the CI reading out must not release it.
    """
    from coord import gate_a

    marker = f"parked ... {gate_a.park_marker('api', 37)}"
    entries = [
        entry(
            2138, position=3, state="parked", attempts=0,
            last_reason=marker,
            reason_at=NOW - PARK_STALE_SECONDS - 60,
        )
    ]
    plan = plan_tick(
        entries,
        board(ci_pending=(2138,)),
        capacity=1,
        now=NOW,
        gate_a_pending={entry_key(REPO, 2138): True},
    )
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_parked_entry_that_lands_while_parked_reconciles_to_done():
    entries = [entry(1650, position=3, state="parked", attempts=0)]
    plan = plan_tick(entries, board(merged=(1650,), ci_pending=(1650,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE
    assert plan.launch is None


# ═══════════════════════════════════════════════════════════════════════════
# #2234: a policy refusal — a worker that reaped `coord.agent.REFUSED_POLICY`
# (clean exit, 0 commits, its own final message cites a standing repo-rule
# prohibition) — parks like Gate-A (#2063) rather than blocking like a
# genuine #1844 pre-dispatch refusal. UNLIKE Gate-A it never resumes itself:
# a Gate-A park waits on a human VERDICT that can arrive; this waits on a
# human doing coordinator-side work that nothing on the board signals when
# it's done, so the only symmetry with Gate-A is "park instead of blocked,
# no attempt spent" — see coord.models.is_policy_refusal_reason's docstring.
# ═══════════════════════════════════════════════════════════════════════════

POLICY_REFUSAL_REASON = (
    "drive exited for claude-coordinator#2195 (exit_code=1): work "
    "b1a090a5e011 refused on a standing repo-rule prohibition rather than "
    "doing the dispatched work — the worker did the CORRECT thing (#2234). "
    "[refused-by-policy #2234]"
)


def test_a_policy_refusal_parks_without_spending_an_attempt():
    """The #2234 acceptance criterion, asserted the way #1844's sibling test
    does: on the attempt counter AND the terminal bucket, not just the verb."""
    entries = [entry(2195, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 2195): POLICY_REFUSAL_REASON},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "parked"
    assert reconcile.updates["state"] == "parked"
    assert "attempts" not in reconcile.updates
    assert plan.blocked == ()  # NOT terminal `blocked`
    assert plan.launch is None
    assert POLICY_REFUSAL_REASON in reconcile.reason
    assert POLICY_REFUSAL_REASON in reconcile.updates["last_reason"]


def test_a_policy_refusal_park_reason_names_a_remedy_that_actually_clears_it():
    """#2871: before this fix, the printed remedy (`coord drive-queue
    remove` once handled) was misleading — `coord.drive.decide()` re-read
    the SAME stale `refused_policy` row on every relaunch regardless of
    what the issue said by then, so `remove`+`add` looked like the fix but
    did nothing. The park reason must name the precondition that actually
    matters (retargeting the issue) rather than pure queue surgery."""
    entries = [entry(2195, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 2195): POLICY_REFUSAL_REASON},
    )
    reason = plan.reconciles[0].reason
    assert "retarget the issue" in reason
    assert "dispatches fresh work automatically" in reason


def test_a_policy_refusal_parks_even_on_a_fresh_entrys_first_tick():
    entries = [entry(2195, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 2195): POLICY_REFUSAL_REASON},
    )
    assert plan.reconciles[0].outcome == "parked"
    assert plan.blocked == ()
    assert plan.launch is None


def test_a_policy_refusal_deep_into_the_attempt_budget_still_spends_none():
    entries = [
        entry(2195, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 2195): POLICY_REFUSAL_REASON},
    )
    assert plan.reconciles[0].outcome == "parked"  # not "exhausted"
    assert plan.blocked == ()


def test_a_parked_policy_refusal_does_not_auto_resume_next_tick():
    """The bug this fix exists to prevent: a parked entry with no
    `merge_ci_pending` reads exactly like a CI park whose gate has cleared,
    and the pre-#2234 pre-pass would flip it straight back to `waiting` —
    relaunching `coord drive` into the identical refusal every tick,
    forever, without ever spending an attempt or ever stopping either."""
    entries = [
        entry(2195, position=3, state="parked", attempts=0,
              last_reason=POLICY_REFUSAL_REASON)
    ]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles == ()  # left alone — still parked
    assert plan.launch is None


def test_a_parked_policy_refusal_stays_parked_through_the_2158_ceiling():
    """Unlike a CI park, this never ages out — there is no refreshable
    reading; the standing rule it names does not get less standing with
    time, so #2158's staleness ceiling must not apply to it."""
    entries = [
        entry(
            2195, position=3, state="parked", attempts=0,
            last_reason=POLICY_REFUSAL_REASON,
            reason_at=NOW - PARK_STALE_SECONDS - 60,
        )
    ]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_policy_refusal_still_reconciles_to_done_if_it_lands_by_hand():
    """The #2055 landed-check still applies on top of #2234: a human doing
    the coordinator-side work and merging it out of band must not leave the
    entry parked forever — it is re-checked against the board like every
    other parked entry."""
    entries = [entry(2195, position=3, state="parked", attempts=0,
                      last_reason=POLICY_REFUSAL_REASON)]
    plan = plan_tick(entries, board(merged=(2195,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE


# ═══════════════════════════════════════════════════════════════════════════
# #2977: a throttle-SKIPPED `gh` call — `_gh`'s pre-call guard found a shared
# GitHub rate-limit backoff (coord.github_throttle) already active and
# raised `GhRateLimitError(from_cache=True)` WITHOUT ever making a network
# call — must not be charged like a real dispatch failure. It parks (like
# Gate-A/policy above) rather than blocking, spends no attempt, AND —
# unlike Gate-A/policy, which wait on an external verdict — resumes itself
# the instant the embedded wall-clock `until=` timestamp passes, no live
# re-check and no operator action needed. coord-portal#161, 2026-08-30: two
# such skips in a row parked the keystone of a 9-entry milestone.
# ═══════════════════════════════════════════════════════════════════════════


def _throttle_skip_reason(*, now: float, retry_after_s: float = 59.0) -> str:
    """A realistic `own_reason` string for a `running` entry whose launch
    died on a throttle-skipped `coord assign` — i.e. exactly what
    `coord/commands/drive_queue.py`'s `_fetch_exit_reasons` would read back
    from the `drive_exited` audit row once `coord.drive.Driver._spawn`
    folds the child's stderr (built by `coord/commands/dispatch.py`'s
    `assign` command from `github_ops.format_throttle_skip_reason`) into
    the summary — see this module's own #2977 comment for the full chain.
    """
    from coord.github_ops import GhRateLimitError, format_throttle_skip_reason

    exc = GhRateLimitError(
        "gh issue view 161 --repo JDonaghy/coord-portal --json "
        "number,title,body,state,milestone,labels skipped: GitHub "
        "secondary_rate_limit backoff active for "
        f"{retry_after_s:.0f}s more (status=403, "
        "request_id=A654:2496C0:1E674B6:66A49BB:6A94A599)",
        status_code=403,
        request_id="A654:2496C0:1E674B6:66A49BB:6A94A599",
        retry_after_s=retry_after_s,
        secondary=True,
        from_cache=True,
    )
    skip_reason = format_throttle_skip_reason(exc, now=now)
    return (
        f"drive exited for {REPO}#161 (exit_code=75): coord assign precision "
        f"{REPO} 161 --driven-by drive:{REPO}#161 exited 75\n   output: "
        f"error: could not fetch issue #161: {skip_reason}"
    )


def test_format_and_parse_throttle_skip_reason_round_trip():
    """The seam `coord/drive_queue.py` actually depends on: the marker and
    the absolute `until=` timestamp `format_throttle_skip_reason` embeds
    must survive being read back by `is_throttle_skip_reason`/
    `parse_throttle_skip_until` with no clock and no I/O."""
    from coord.github_ops import (
        GhRateLimitError,
        format_throttle_skip_reason,
        is_throttle_skip_reason,
        parse_throttle_skip_until,
    )

    exc = GhRateLimitError(
        "gh ... skipped: GitHub secondary_rate_limit backoff active for "
        "10s more (status=403, request_id=abc)",
        retry_after_s=10.0, secondary=True, from_cache=True,
    )
    reason = format_throttle_skip_reason(exc, now=NOW)
    assert is_throttle_skip_reason(reason)
    assert parse_throttle_skip_until(reason) == pytest.approx(NOW + 10.0)
    assert not is_throttle_skip_reason("an ordinary death, nothing to do with gh")
    assert parse_throttle_skip_until("an ordinary death") is None


def test_a_throttle_skipped_gh_call_parks_without_spending_an_attempt():
    reason = _throttle_skip_reason(now=NOW)
    entries = [entry(161, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 161): reason},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "parked"
    assert reconcile.updates["state"] == STATE_PARKED
    assert "attempts" not in reconcile.updates
    assert plan.blocked == ()  # NOT terminal `blocked`
    assert plan.launch is None


def test_a_throttle_skipped_gh_call_deep_into_the_attempt_budget_still_spends_none():
    reason = _throttle_skip_reason(now=NOW)
    entries = [entry(161, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 161): reason},
    )
    assert plan.reconciles[0].outcome == "parked"  # not "exhausted"
    assert plan.blocked == ()


def test_a_parked_throttle_skip_stays_parked_before_the_backoff_clears():
    reason = _throttle_skip_reason(now=NOW, retry_after_s=120.0)
    entries = [
        entry(161, position=3, state="parked", attempts=0,
              last_reason=reason, reason_at=NOW)
    ]
    plan = plan_tick(entries, board(), capacity=1, now=NOW + 30.0)
    assert plan.reconciles == ()  # still parked — backoff has not cleared yet
    assert plan.launch is None


def test_a_parked_throttle_skip_resumes_the_instant_the_backoff_clears():
    """The acceptance criterion: no operator action, no `remove`+`add` — the
    entry wakes itself exactly when `Backoff.until` passes."""
    reason = _throttle_skip_reason(now=NOW, retry_after_s=59.0)
    entries = [
        entry(161, position=3, state="parked", attempts=1,
              last_reason=reason, reason_at=NOW)
    ]
    plan = plan_tick(entries, board(), capacity=1, now=NOW + 60.0)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 0


def test_a_parked_throttle_skip_with_no_clock_never_resumes():
    """`now=None` (a pure-logic caller) must never guess a resume — same
    fail-closed posture #1794's grace window uses for the same input."""
    reason = _throttle_skip_reason(now=NOW, retry_after_s=1.0)
    entries = [
        entry(161, position=3, state="parked", attempts=0, last_reason=reason)
    ]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles == ()


def test_a_parked_throttle_skip_with_unparseable_until_resumes_past_the_2158_ceiling():
    """Defensive: text carrying the marker with no parseable `until=` (a
    hand-edited row, or a future format change) must still be FINITE — never
    a silent infinite park — bounded by the same `PARK_STALE_SECONDS`
    ceiling #2158 already gives the CI park."""
    from coord.github_ops import THROTTLE_SKIP_MARKER

    reason = f"drive exited: {THROTTLE_SKIP_MARKER} gh skipped, no until recorded"
    entries = [
        entry(161, position=3, state="parked", attempts=0,
              last_reason=reason, reason_at=NOW - PARK_STALE_SECONDS - 60)
    ]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING


def test_a_throttle_skip_still_reconciles_to_done_if_it_lands_by_hand():
    reason = _throttle_skip_reason(now=NOW)
    entries = [entry(161, position=3, state="parked", attempts=0,
                      last_reason=reason)]
    plan = plan_tick(entries, board(merged=(161,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE


# ═══════════════════════════════════════════════════════════════════════════
# #2850: a drive that exits 0 having MERGED must reconcile straight to
# `done`, never `retry` — before this fix, `own_reason` carried the drive's
# own "✓ MERGED — … has landed" text but `_reconcile_running` never read it
# for anything but the gate_a/policy/refused/dead_end special cases, so a
# confirmed-landed drive fell all the way through to the generic "no
# session, no active work, nothing landed" death diagnosis and got
# requeued — the reported vimcode#536 incident. Two independent recoveries,
# reproduced separately: `own_reason` naming a confirmed merge (#2850 fix 1),
# and a live re-check confirming landed independent of `own_reason` (#2850
# fix 2) — and, on the DEPENDENT side, a pre-req's queue row no longer
# shadowing that same live re-check just because it is present and not yet
# `done` (#2850 fix 3).
# ═══════════════════════════════════════════════════════════════════════════

MERGED_OWN_REASON = (
    "drive exited for claude-coordinator#1650 (exit_code=0): ✓ MERGED — "
    f"issue-1650-example has landed on develop\n   {MERGE_LANDED_MARKER}"
)


def test_an_own_reason_confirming_merge_reconciles_to_done_not_retry():
    """#2850 fix 1, the literal reported shape: exit_code=0, `own_reason`
    narrates a confirmed merge — must mark `done` on the FIRST tick that
    observes it, not spend a retry attempt requeuing a launch with nothing
    left to do."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): MERGED_OWN_REASON},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE
    assert "attempts" not in reconcile.updates  # never spent
    assert plan.blocked == ()
    assert plan.launch is None


def test_a_merge_landed_own_reason_wins_even_deep_into_the_attempt_budget():
    entries = [
        entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): MERGED_OWN_REASON},
    )
    assert plan.reconciles[0].outcome == "done"  # not "exhausted"
    assert plan.blocked == ()


def test_an_own_reason_that_merely_mentions_merge_without_the_marker_still_retries():
    """The marker, not the human-readable "MERGED" text, is what decides
    this — a reason that talks ABOUT a merge (e.g. a merge-gate failure
    diagnosis) without #2850's `MERGE_LANDED_MARKER` must not be
    misread as a confirmed landing."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=0,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    own_reason = (
        "drive exited for claude-coordinator#1650 (exit_code=1): merge "
        "attempted 3 times without landing."
    )
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "retry"


def test_a_live_recheck_marks_a_dead_running_entry_done_with_no_own_reason_at_all():
    """#2850 fix 2: `live_prereq_terminal` is now ALSO consulted for a
    `running` entry's OWN key, independent of whatever (if anything) its
    `own_reason` says — a crash that left no exit reason at all, or an exit
    reason unrelated to landing, must still be caught if a live re-check
    this tick confirms the issue already closed or merged."""
    entries = [
        entry(
            1650,
            state=STATE_RUNNING,
            attempts=0,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW,
        live_prereq_terminal={entry_key(REPO, 1650): True},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE
    assert plan.blocked == ()


def test_a_dependent_is_released_when_its_queued_but_not_done_prereq_has_live_landed():
    """#2850 fix 3: `_resolve_prereqs`'s `dep_state is not None` branch used
    to return "waiting on {dep} (queued, {dep_state})" unconditionally for
    any state short of `done` — never consulting `live_prereq_terminal`,
    unlike its own final branch for a dep absent from the queue altogether
    (#2602). A pre-req sitting in the queue under ANY other state must
    release its dependent the moment a live re-check confirms it actually
    landed. Position 0 goes to the DEPENDENT (not the pre-req) so a passing
    assertion on ``plan.launch`` is unambiguous — `plan_tick` launches at
    most one entry per tick, position order first, so a `STATE_WAITING`
    pre-req sitting at an earlier position would otherwise win the single
    launch slot on its own unrelated eligibility and mask the very thing
    under test."""
    entries = [
        entry(1654, position=0, after=(entry_key(REPO, 1650),)),
        entry(1650, position=1, state=STATE_WAITING),
    ]
    plan = plan_tick(
        entries, board(), capacity=2,
        live_prereq_terminal={entry_key(REPO, 1650): True},
    )
    assert plan.launch is not None
    assert plan.launch.issue == 1654


def test_a_dependent_is_released_when_its_prereq_is_wrongly_stuck_running():
    """The exact reported incident, reproduced end to end: a pre-req sits in
    a bogus `running` row (a drive that exited 0 having merged, requeued —
    the shape #2850 fixes 1/2 now prevent, but this pins the DEPENDENT side
    even if some other cause ever leaves a pre-req wrongly `running` again).
    A live re-check confirming the pre-req landed must release the
    dependent on THIS tick, whatever the pre-req's own row still claims —
    and the pre-req's own bogus `running` row is corrected in the SAME
    tick, not requeued into yet another relaunch."""
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1, after=(entry_key(REPO, 1650),)),
    ]
    plan = plan_tick(
        entries, board(), capacity=2,
        live_prereq_terminal={entry_key(REPO, 1650): True},
    )
    assert plan.launch is not None
    assert plan.launch.issue == 1654
    prereq_reconcile = next(
        r for r in plan.reconciles if r.key == entry_key(REPO, 1650)
    )
    assert prereq_reconcile.outcome == "done"


def test_a_dependent_is_released_when_its_prereq_is_blocked_but_live_landed():
    """Fix 3 pinned in ISOLATION from fix 2: a `blocked` pre-req never goes
    through `_reconcile_running`'s step-1 walk at all (that loop only
    touches `STATE_RUNNING` rows), so `states[dep]` stays `STATE_BLOCKED`
    for the whole tick — the ONLY thing that can release the dependent here
    is `_resolve_prereqs` itself consulting `live_prereq_terminal`. Before
    #2850 this branch (`dep_state in (STATE_BLOCKED, STATE_FAILED)`)
    returned "it will never satisfy" unconditionally, matching #2055's own
    rationale for re-checking a `blocked` pre-req's OWN row — a human can
    merge a blocked issue's work out of band just as easily as a running
    one's."""
    entries = [
        entry(1654, position=0, after=(entry_key(REPO, 1650),)),
        entry(1650, position=1, state=STATE_BLOCKED, attempts=2),
    ]
    plan = plan_tick(
        entries, board(), capacity=1,
        live_prereq_terminal={entry_key(REPO, 1650): True},
    )
    assert plan.launch is not None
    assert plan.launch.issue == 1654


# ── #2055: `blocked`/`failed` re-checked against the board too ─────────────
#
# #1891's landed re-check above was `parked`-only. `blocked`/`failed` got no
# such check, so an entry that merges by hand while blocked showed as
# `blocked` forever — the board never asked again. These extend the SAME
# `landed` branch to `blocked`/`failed`, without granting them the `parked`
# branch's CI-pending resume (that would resurrect a gave-up entry for
# dispatch, which is explicitly not the fix). See #1956 for the live
# instance this was spotted from.
# ═══════════════════════════════════════════════════════════════════════════


def test_a_blocked_entry_that_lands_reconciles_to_done():
    entries = [entry(1650, position=3, state=STATE_BLOCKED, attempts=2)]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE
    assert plan.launch is None


def test_a_blocked_entry_that_closed_without_merging_also_reconciles_to_done():
    entries = [entry(1650, position=3, state=STATE_BLOCKED, attempts=2)]
    plan = plan_tick(entries, board(closed=(1650,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE


def test_a_still_blocked_entry_is_left_untouched_not_resumed_to_waiting():
    """The whole point of scoping this to the `landed` branch only: a
    blocked entry whose issue has NOT landed must stay blocked — it must
    NOT fall into the `parked` branch's CI-pending resume, which would
    relaunch a gave-up entry outside its attempt budget."""
    entries = [entry(1650, position=3, state=STATE_BLOCKED, attempts=2)]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles == ()  # nothing to report or write
    assert plan.launch is None


def test_a_failed_entry_that_lands_reconciles_to_done():
    entries = [entry(1650, position=3, state=STATE_FAILED, attempts=2)]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE


def test_a_still_failed_entry_is_left_untouched():
    entries = [entry(1650, position=3, state=STATE_FAILED, attempts=2)]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles == ()
    assert plan.launch is None


# ── #2230: `blocked` reconciliation — re-evaluable vs permanent ────────────
#
# quadraui#309 sat `blocked attempts=2` for ~11h while its merge was landable
# for most of that window. These pin the split: a `blocked` entry whose cause
# is a re-evaluable gate reading resumes, with attempts reset, the moment
# that reading clears; a PERMANENTLY-blocked entry (#1844/#2019) never does,
# however clear the gate reads; and an entry with no evidence either way is
# left exactly as untouched as #2055 already pinned above.


def _blocked_entry(issue: int, **kw) -> QueueEntry:
    kw.setdefault("state", STATE_BLOCKED)
    kw.setdefault("attempts", DEFAULT_MAX_ATTEMPTS)
    kw.setdefault(
        "last_reason",
        "drive session died without landing the work, launched 90s ago "
        f"(attempt {DEFAULT_MAX_ATTEMPTS}/{DEFAULT_MAX_ATTEMPTS}) — giving up",
    )
    return entry(issue, **kw)


def test_a_blocked_entry_whose_live_gate_reads_clear_resumes_with_attempts_reset():
    entries = [_blocked_entry(309, position=3, resumes=0)]
    plan = plan_tick(
        entries, board(), capacity=1, live_blocked_gate={entry_key(REPO, 309): False}
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 0
    assert reconcile.updates["resumes"] == 1
    # …and it falls straight into this SAME tick's launch selection, exactly
    # like a released `parked`/deploy-gate entry already does.
    assert plan.launch is not None and plan.launch.issue == 309


def test_a_blocked_entry_whose_live_gate_still_reads_blocked_stays_blocked():
    entries = [_blocked_entry(309, position=3)]
    plan = plan_tick(
        entries, board(), capacity=1, live_blocked_gate={entry_key(REPO, 309): True}
    )
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_blocked_entry_with_no_gate_evidence_stays_blocked_untouched():
    """No live override, no cached board signal: #2230's sweep never
    guesses — same outcome as pre-#2230 (`test_a_still_blocked_entry_is_left_
    untouched_not_resumed_to_waiting` above), now pinned with the new
    parameter wired in and explicitly empty."""
    entries = [_blocked_entry(309, position=3)]
    plan = plan_tick(entries, board(), capacity=1, live_blocked_gate={})
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_blocked_entry_with_the_2273_dispatch_failure_text_still_resumes_on_a_clear_gate():
    """#2635: `_reconcile_blocked`'s own resume mechanism (`_blocked_gate_
    reading` + `live_blocked_gate`) never consulted `is_pre_dispatch_block_
    reason`/`is_dispatch_failure_reason` in the first place — only
    `is_permanent_block_reason` gates it. Pinning that explicitly, with the
    EXACT #2273 wording named in the claude-coordinator#2569 incident, so a
    future change cannot accidentally wire that text classification into
    this mechanical resume path and re-strand a live entry: the bug #2635
    reports is confined to `coord.commands.drive_queue`'s `list` rendering,
    never this reconcile."""
    entries = [
        _blocked_entry(
            2569,
            position=3,
            last_reason=(
                "drive exited for claude-coordinator#2569 (exit_code=3): "
                "deadline of 240m exceeded (2/2 attempts) — giving up — no "
                "assignment was ever created for this run (#2273): likely "
                "an infrastructure/dispatch-layer failure, not a code defect"
            ),
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, live_blocked_gate={entry_key(REPO, 2569): False}
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 0
    assert plan.launch is not None and plan.launch.issue == 2569


# ── #2806: "could not read" vs "confirmed still shut" ──────────────────────
#
# vimcode#555 sat `blocked` across four ticks with its merge gate fully clear
# because `_fetch_live_blocked_gate`'s probe came back with no key for it —
# silently, and indistinguishably from a gate the sweep had genuinely
# re-confirmed still shut (both used to collapse into `_reconcile_blocked`
# returning `None`, no report, no write). `live_blocked_unreadable` is the
# shell's way of saying WHICH of the two happened; these pin the split.


def test_a_blocked_entry_the_probe_could_not_read_reports_distinctly():
    """A present `live_blocked_unreadable` note — the shell's live probe was
    attempted against this exact entry and still came back with no gate
    reading — must produce its OWN outcome, distinct from both `resumed` and
    silence, and must not touch `state`/`attempts` (no evidence means no
    relaunch, only a distinct report)."""
    entries = [_blocked_entry(555, position=1)]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        live_blocked_gate={},
        live_blocked_unreadable={
            entry_key(REPO, 555): "no merge-queue row for this entry, even "
            "after the self-heal enqueue attempt"
        },
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "gate_unreadable"
    assert "state" not in reconcile.updates
    assert "could not be read" in reconcile.reason
    assert "no merge-queue row" in reconcile.reason
    assert plan.launch is None


def test_a_blocked_entry_with_no_unreadable_note_stays_exactly_silent():
    """The pre-#2806 shape: no `live_blocked_gate` key AND no
    `live_blocked_unreadable` note (the probe was never even attempted —
    e.g. the whole fetch failed closed before reaching any entry) must
    still render as it always has — nothing to report, nothing to write."""
    entries = [_blocked_entry(555, position=1)]
    plan = plan_tick(
        entries, board(), capacity=1, live_blocked_gate={}, live_blocked_unreadable={}
    )
    assert plan.reconciles == ()
    assert plan.launch is None


def test_an_unreadable_note_never_overrides_a_confirmed_still_shut_reading():
    """A `live_blocked_gate[key] is True` reading — CONFIRMED still shut —
    must win even if `live_blocked_unreadable` also carries a (stale/
    unrelated) note for the same key: confirmed evidence outranks "could not
    read", which only ever applies when there is no reading at all."""
    entries = [_blocked_entry(555, position=1)]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        live_blocked_gate={entry_key(REPO, 555): True},
        live_blocked_unreadable={entry_key(REPO, 555): "stale note"},
    )
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_pre_dispatch_reason_text_does_not_suppress_a_real_unreadable_note():
    """#2635's own lesson, applied to #2806: `is_pre_dispatch_block_reason`'s
    text match is per-RUN, not per-ENTRY, and can be wrong — a retry's own
    launch can carry the #2273 marker purely because an earlier attempt's
    work was still in flight, even though a real branch/PR exists. So the
    PURE reconcile layer must not re-derive that suppression from the text
    itself: it trusts whatever `live_blocked_unreadable` the shell handed
    it. The shell (`coord.commands.drive_queue._fetch_live_blocked_gate`)
    is where the #2589 pre-dispatch suppression actually lives now — it
    only omits a key when it ALSO found no merge-queue row at all, never
    on the text alone once real evidence (any queue row, even without a PR
    yet) exists. A present key here therefore always means something worth
    reporting."""
    entries = [
        _blocked_entry(
            2569,
            position=1,
            last_reason=(
                "drive exited for claude-coordinator#2569 (exit_code=3): "
                "deadline of 240m exceeded (2/2 attempts) — giving up — no "
                "assignment was ever created for this run (#2273): likely "
                "an infrastructure/dispatch-layer failure, not a code defect"
            ),
        )
    ]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        live_blocked_gate={},
        live_blocked_unreadable={
            entry_key(REPO, 2569): "merge-queue row has no PR number yet"
        },
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "gate_unreadable"
    assert plan.launch is None


def test_a_permanently_refused_entry_never_reports_unreadable():
    """#1844: a permanent guard refusal is excluded before `_blocked_gate_
    reading` is even consulted — an unreadable note for it (which the shell
    should never produce in the first place, since `_fetch_live_blocked_
    gate` also excludes these) must still not surface a report."""
    entries = [
        _blocked_entry(
            70,
            position=1,
            last_reason=(
                "dispatch failed: ... (exit_code=5) — refused by a "
                "pre-dispatch guard, which cannot change on retry (#1844); "
                "blocking without spending an attempt"
            ),
        )
    ]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        live_blocked_gate={},
        live_blocked_unreadable={entry_key(REPO, 70): "entry_gate_status raised"},
    )
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_permanently_refused_blocked_entry_is_never_resumed():
    """#1844: even a live gate reading of 'clear now' must not resume a
    permanent refusal — relaunching a deterministic guard refusal changes
    nothing, so this sweep must not even ask."""
    entries = [
        _blocked_entry(
            70,
            position=1,
            last_reason=(
                "dispatch failed: ... (exit_code=5) — refused by a "
                "pre-dispatch guard, which cannot change on retry (#1844); "
                "blocking without spending an attempt"
            ),
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, live_blocked_gate={entry_key(REPO, 70): False}
    )
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_dead_end_blocked_entry_is_never_resumed():
    """#2019: same posture as the #1844 refusal above."""
    entries = [
        _blocked_entry(
            88,
            position=1,
            last_reason=(
                "the board row is terminal and unactionable (nothing "
                "active, no gate transition available), which cannot "
                "change on retry (#2019); blocking without spending an "
                "attempt"
            ),
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, live_blocked_gate={entry_key(REPO, 88): False}
    )
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_blocked_entry_that_has_hit_the_resume_ceiling_stays_blocked_and_says_so():
    """#2230's churn bound: an entry already resumed MAX_BLOCKED_RESUMES times
    must not oscillate forever — it stays `blocked`, but `last_reason` is
    rewritten so the oscillation itself is visible, not silently swallowed."""
    from coord.drive_queue import MAX_BLOCKED_RESUMES

    entries = [_blocked_entry(309, position=3, resumes=MAX_BLOCKED_RESUMES)]
    plan = plan_tick(
        entries, board(), capacity=1, live_blocked_gate={entry_key(REPO, 309): False}
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "oscillating"
    assert "state" not in reconcile.updates  # stays blocked — no state write
    assert "resumes" not in reconcile.updates  # ceiling hit: not bumped again
    assert str(MAX_BLOCKED_RESUMES) in reconcile.reason
    assert plan.launch is None


# ── #2935: a never-dispatched after=-blocked entry must never reach the ────
# merge-gate sweep at all
#
# claude-coordinator#2935: a `waiting` entry blocked by `_resolve_prereqs`'s
# own unsatisfiable `after=` verdict has no assignment and no branch — there
# is categorically no merge-queue row for #2230's sweep to have an opinion
# about, "unreadable" or otherwise. Before this fix, `_reconcile_blocked`
# never special-cased this shape: it fell through to `_blocked_gate_reading`
# (which correctly finds no evidence) and then to `_reconcile_blocked_
# unreadable`, which — whenever the shell's live probe had actually been
# attempted (`live_blocked_unreadable` carrying a note, exactly what
# `coord.commands.drive_queue._fetch_live_blocked_gate`'s #2806 self-heal
# produces for a row with no merge-queue row) — WROTE `last_reason` to a
# "could not be read" sentence. That overwrite destroyed the only marker
# `_reconcile_blocked_after` uses to recognise this row as after=-caused at
# all, so every later tick — even long after the prerequisite landed — took
# the SAME "no evidence, probe unreadable" branch forever: a permanent
# block, never resumed. These pin the fix: the merge-gate sweep must bail
# out before ever consulting `live_blocked_unreadable` for this shape, and
# the entry must still resume once its prereq actually lands.


def test_a_never_dispatched_after_blocked_entry_never_reports_gate_unreadable():
    """The shell's live probe (a real `live_blocked_unreadable` note, the
    exact shape `_fetch_live_blocked_gate` emits for a row with no
    merge-queue row) must not produce a `gate_unreadable` reconcile — or any
    write at all — for an entry whose only cause is an unsatisfiable
    `after=` verdict with its prereq still not landed."""
    dep_key = entry_key(REPO, 2897)
    entries = [
        entry(2897, position=0, state=STATE_BLOCKED),
        _blocked_entry(
            2899,
            position=1,
            after=(dep_key,),
            last_reason=f"pre-req {dep_key} is queued but blocked — it will never satisfy",
        ),
    ]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        live_blocked_gate={},
        live_blocked_unreadable={
            entry_key(REPO, 2899): "no merge-queue row for this entry, even "
            "after the self-heal enqueue attempt"
        },
    )
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_never_dispatched_after_blocked_entry_still_resumes_once_its_prereq_lands():
    """End-to-end regression for the #2935 incident: a tick where the
    prereq has NOT yet landed and the shell's probe comes back unreadable
    must leave `last_reason` untouched (nothing in `plan.reconciles` to
    write) — so a LATER tick, once the prereq lands, still recognises the
    original marker and resumes normally, exactly as it would have if the
    spurious probe had never run."""
    dep_key = entry_key(REPO, 2897)
    blocked_dep = entry(2897, position=0, state=STATE_BLOCKED)
    dependent = _blocked_entry(
        2899,
        position=1,
        after=(dep_key,),
        resumes=0,
        last_reason=f"pre-req {dep_key} is queued but blocked — it will never satisfy",
    )

    # Tick 1: prereq still blocked, shell's probe comes back unreadable —
    # must be a total no-op (the #2935 fix).
    tick1 = plan_tick(
        [blocked_dep, dependent],
        board(),
        capacity=1,
        live_blocked_gate={},
        live_blocked_unreadable={
            entry_key(REPO, 2899): "no merge-queue row for this entry, even "
            "after the self-heal enqueue attempt"
        },
    )
    assert tick1.reconciles == ()

    # Tick 2: the prereq has since landed. `dependent`'s `last_reason` is
    # STILL the original unsatisfiable-prereq marker — untouched by tick 1 —
    # so `_reconcile_blocked_after` recognises it and resumes it, even
    # though a merge-gate probe against it failed in between.
    tick2 = plan_tick(
        [entry(2897, position=0, state=STATE_DONE), dependent],
        board(merged=(2897,)),
        capacity=1,
    )
    reconcile = next(r for r in tick2.reconciles if r.key == entry_key(REPO, 2899))
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 0
    assert tick2.launch is not None and tick2.launch.issue == 2899


# ── #2362: a blocked entry resumes once its unsatisfiable after= lands ─────
#
# `_resolve_prereqs` blocks a `waiting` entry the instant one of its named
# `after=` pre-reqs is itself `blocked`/`failed` — correctly, since an entry
# whose pre-req is provably dead cannot ever satisfy on its own. But once
# blocked, the entry is terminal for dispatch and step 4 (where
# `_resolve_prereqs` lives) never looks at it again — #2230's gate re-check
# has NOTHING to say here either, because `_blocked_gate_reading` returns
# `None` ("no evidence") for an entry that never reached the merge queue.
# Without this sweep, claude-coordinator#2284-#2288 (the live incident that
# prompted this issue) would stay `blocked` forever, even once their prereq
# chain's root cause (#2283) reaches `done` — exactly what happened twice
# before an operator noticed and hand-removed/re-added the rows.


def test_a_blocked_entry_resumes_once_its_unsatisfiable_prereq_lands():
    dep_key = entry_key(REPO, 1650)
    entries = [
        entry(1650, position=0, state=STATE_DONE),
        entry(
            1654,
            position=1,
            after=(dep_key,),
            state=STATE_BLOCKED,
            attempts=2,
            resumes=0,
            last_reason=f"pre-req {dep_key} is queued but blocked — it will never satisfy",
        ),
    ]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=2)
    reconcile = next(r for r in plan.reconciles if r.key == entry_key(REPO, 1654))
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 0
    assert reconcile.updates["resumes"] == 1
    # …and falls straight into this SAME tick's launch selection, exactly
    # like #2230's gate-cleared resume already does.
    assert plan.launch is not None and plan.launch.issue == 1654


def test_a_blocked_entry_stays_blocked_while_its_prereq_is_still_blocked():
    """The dependent's own re-derivation must agree with `_resolve_prereqs`:
    if the pre-req has NOT landed (still `blocked` itself), nothing resumes —
    the "it will never satisfy" reason is still true."""
    dep_key = entry_key(REPO, 1650)
    entries = [
        entry(1650, position=0, state=STATE_BLOCKED),
        entry(
            1654,
            position=1,
            after=(dep_key,),
            state=STATE_BLOCKED,
            attempts=2,
            last_reason=f"pre-req {dep_key} is queued but blocked — it will never satisfy",
        ),
    ]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_blocked_entry_with_an_unrelated_cause_is_not_resumed_by_a_landed_prereq():
    """A `blocked` entry that merely HAS an `after=` list, but whose
    `last_reason` names a DIFFERENT cause (exhausted attempts, not an
    unsatisfiable pre-req), must not be resumed just because that pre-req
    later lands — that would be a false resume of an entry the queue
    genuinely gave up on for an unrelated reason."""
    entries = [
        entry(1650, position=0, state=STATE_DONE),
        entry(
            1654,
            position=1,
            after=(entry_key(REPO, 1650),),
            state=STATE_BLOCKED,
            attempts=2,
            last_reason=(
                "drive session died without landing the work 2/2 times — giving up"
            ),
        ),
    ]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=1)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_permanently_blocked_entry_is_not_resumed_even_once_its_after_lands():
    """#1844/#2019 permanent-block markers outrank #2362 exactly like they
    outrank #2230 — a deterministic refusal cannot be un-refused by an
    unrelated pre-req landing."""
    entries = [
        entry(
            1654,
            position=1,
            after=(entry_key(REPO, 1650),),
            state=STATE_BLOCKED,
            attempts=0,
            last_reason=(
                "dispatch failed: ... (exit_code=5) — refused by a "
                "pre-dispatch guard, which cannot change on retry (#1844); "
                "blocking without spending an attempt"
            ),
        )
    ]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=1)
    assert plan.reconciles == ()
    assert plan.launch is None


# ── #2602 recovery half: a live re-check resumes the "unknown pre-req" ────
# blocked shape too, not just "queued but blocked/failed" (#2362's original
# scope). Before this, `pre-req is not queued, not merged and not open on
# the board` was PERMANENT — the module docstring said so outright — and
# claude-coordinator#2602 (coord-portal#145/#149/#150, 2026-08-22) is the
# live incident: a pre-req that merged/closed left the queue faster than the
# periodic `/board` build could catch up, and every entry chained `--after`
# it sat `blocked` until an operator hand-removed and re-added them.


def test_a_blocked_entry_resumes_once_a_live_recheck_confirms_its_unknown_prereq_landed():
    dep_key = entry_key(REPO, 1650)
    entries = [
        entry(
            1654,
            position=1,
            after=(dep_key,),
            state=STATE_BLOCKED,
            attempts=2,
            resumes=0,
            last_reason=(
                f"pre-req {dep_key} is not queued, not merged and not open on "
                "the board (unknown issue, or the board has not synced it — "
                "try `coord sync`)"
            ),
        ),
    ]
    # The cached board still has NOTHING for the dep (exactly the incident
    # shape) — only the live re-check confirms it landed.
    plan = plan_tick(
        entries, board(), capacity=1, live_prereq_terminal={dep_key: True}
    )
    reconcile = next(r for r in plan.reconciles if r.key == entry_key(REPO, 1654))
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 0
    assert reconcile.updates["resumes"] == 1
    assert plan.launch is not None and plan.launch.issue == 1654


def test_a_blocked_entry_with_the_unknown_prereq_reason_stays_blocked_without_live_evidence():
    """Same shape as above, but no live re-check ran this tick (or it ran and
    came back inconclusive) — this must stay EXACTLY the pre-#2602 behaviour:
    still blocked, no false resume from board facts alone."""
    dep_key = entry_key(REPO, 1650)
    entries = [
        entry(
            1654,
            position=1,
            after=(dep_key,),
            state=STATE_BLOCKED,
            attempts=2,
            last_reason=(
                f"pre-req {dep_key} is not queued, not merged and not open on "
                "the board (unknown issue, or the board has not synced it — "
                "try `coord sync`)"
            ),
        ),
    ]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles == ()
    assert plan.launch is None


# ── #2715: the queue's OWN `done` record satisfies a pre-req immediately ───
#
# `_resolve_prereqs` had exactly one proof a pre-req was satisfied —
# `board.facts(dep).landed`, i.e. the CACHED `issues` row — with no case for
# the dep's own `states` entry already reading `STATE_DONE`. That cache is a
# periodic `/board` build, not a live read, so a pre-req the queue itself
# just merged (e.g. #2350's `coord merge --only` fast path, which writes
# `STATE_DONE` straight from the merge queue's own confirmed `MERGED` row —
# see `_run_merge_only_candidates` in coord/commands/drive_queue.py) reads as
# still-outstanding here for however long that cache takes to catch up —
# observed at over 10 minutes / three ticks on claude-coordinator#2706
# (2026-08-24). Every `STATE_DONE` write in the queue is gated on a landed
# fact (either `board.facts(...).landed` itself, or the merge queue's own
# live-verified `MERGED` row), never written speculatively — so the dep's own
# `states` entry reading `done` is exactly as trustworthy as `facts.landed`,
# and does not need to wait for the cache to independently confirm it.


def test_a_waiting_entrys_prereq_is_satisfied_by_the_queues_own_done_state_before_the_board_cache_catches_up():
    """The dep's queue row already reads `STATE_DONE` (the queue's own
    record of having landed it), but the cached board has NOT caught up yet
    — `board()` here carries nothing for the dep at all, exactly like a
    stale `issues` cache. A `waiting` entry naming it as a pre-req must
    launch THIS tick rather than defer on 'waiting on ... (queued, done)'."""
    dep_key = entry_key(REPO, 1650)
    entries = [
        entry(1650, position=0, state=STATE_DONE),
        entry(1654, position=1, after=(dep_key,), state=STATE_WAITING),
    ]
    plan = plan_tick(entries, board(), capacity=2)
    assert plan.deferrals == ()
    assert plan.blocked == ()
    assert plan.launch is not None and plan.launch.issue == 1654


def test_a_blocked_entry_resumes_once_its_prereqs_queue_row_reads_done_even_before_the_board_cache_catches_up():
    """The exact claude-coordinator#2706 shape: #1654(-analog) was already
    `blocked` on #1650(-analog) reading `blocked` at the time — an
    unsatisfiable verdict, correctly recorded. #1650 then landed via the
    queue's own #2350 merge-only fast path, flipping its OWN row straight to
    `STATE_DONE` — but the cached board (`board()` here, carrying nothing for
    #1650) has not caught up. Before #2715 this sat blocked for however long
    that cache took (observed 10m14s / three ticks live); the fix must
    resume it on the very next tick, no cache round trip required."""
    dep_key = entry_key(REPO, 1650)
    entries = [
        entry(
            1650,
            position=0,
            state=STATE_DONE,
            last_reason=(
                "merged directly from the tick — Test/Review were already "
                "satisfied, Merge was the only gate left (#2350)"
            ),
        ),
        entry(
            1654,
            position=1,
            after=(dep_key,),
            state=STATE_BLOCKED,
            attempts=2,
            resumes=0,
            last_reason=f"pre-req {dep_key} is queued but blocked — it will never satisfy",
        ),
    ]
    plan = plan_tick(entries, board(), capacity=2)
    reconcile = next(r for r in plan.reconciles if r.key == entry_key(REPO, 1654))
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 0
    assert reconcile.updates["resumes"] == 1
    # …and falls straight into this SAME tick's launch selection — no cache
    # round trip, no extra tick spent doing nothing (#2715).
    assert plan.launch is not None and plan.launch.issue == 1654


# ── #2756: partial satisfaction — resume to `waiting`, not stay `blocked` ──
#
# `_resolve_prereqs` walks `entry.after` in order and returns the FIRST
# unsatisfiable verdict it finds, so a `blocked` entry's frozen `last_reason`
# may name only ONE of several pre-reqs. Before this fix, `_reconcile_blocked_
# after` only resumed once EVERY named pre-req had landed (`dependency_reason`
# empty) — so an entry stayed `blocked`, wearing its now-false "will never
# satisfy" reason about a pre-req that had already cleared, for as long as ANY
# *other* pre-req remained merely in flight. claude-coordinator#2741
# (2026-08-24) is the live incident: nine `after=` entries from an overly
# broad `tests/` declaration, one of which (#2731) merged while five others
# were still queued/running — the row stayed red with a false claim about
# #2731 specifically until the slowest sibling finally landed.


def test_a_blocked_entry_resumes_to_waiting_when_its_unsatisfiable_prereq_clears_but_a_sibling_prereq_is_still_in_flight():
    """The #2741 shape, minimised: two named pre-reqs, one dead (the one the
    frozen `last_reason` names) and one merely queued. Once the dead one
    lands, the re-derived verdict is no longer unsatisfiable — even though
    the second pre-req is still outstanding — so the entry must resume to
    `waiting` (an accurate, live "waiting on ..." reason takes over on the
    next real tick) rather than stay `blocked` asserting an impossibility
    about a pre-req that already merged."""
    dep_done = entry_key(REPO, 1650)
    dep_waiting = entry_key(REPO, 1651)
    entries = [
        entry(1650, position=0, state=STATE_DONE),
        entry(1651, position=1, state=STATE_WAITING),
        entry(
            1654,
            position=2,
            after=(dep_done, dep_waiting),
            state=STATE_BLOCKED,
            attempts=2,
            resumes=0,
            last_reason=f"pre-req {dep_done} is queued but blocked — it will never satisfy",
        ),
    ]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=3)
    reconcile = next(r for r in plan.reconciles if r.key == entry_key(REPO, 1654))
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 0
    assert reconcile.updates["resumes"] == 1
    # It does NOT launch this same tick — `dep_waiting` has not landed — but
    # critically it is no longer pinned `blocked`; it falls into step 4's
    # ordinary deferral, exactly like any other `waiting` entry with an
    # outstanding pre-req.
    assert plan.launch is None or plan.launch.issue != 1654


def test_a_blocked_entry_stays_blocked_when_a_different_named_prereq_is_independently_unsatisfiable():
    """Guard against over-widening the resume: the FIRST dep clears, but a
    SECOND named pre-req is independently dead (itself `blocked`/`failed`).
    The re-derived verdict is STILL unsatisfiable — just for a different
    dep — so #2756 must not resume this entry; only the merely-in-flight
    case should."""
    dep_done = entry_key(REPO, 1650)
    dep_dead = entry_key(REPO, 1651)
    entries = [
        entry(1650, position=0, state=STATE_DONE),
        entry(1651, position=1, state=STATE_FAILED),
        entry(
            1654,
            position=2,
            after=(dep_done, dep_dead),
            state=STATE_BLOCKED,
            attempts=2,
            last_reason=f"pre-req {dep_done} is queued but blocked — it will never satisfy",
        ),
    ]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=3)
    assert all(r.key != entry_key(REPO, 1654) for r in plan.reconciles)
    assert plan.launch is None


# ── #2404 review: a CHAINED (two-hop) after= block, not just single-hop ────
#
# The live incident this issue is about (claude-coordinator#2284-#2288) was
# never a single `after=` edge: #2284-#2288 all declared `after=2283`, and
# #2283 was ITSELF `blocked` on `after=2282` — a two-hop chain (root 2282 ->
# middle 2283 -> leaf 2284..2288), not the one-hop shape every test above
# pins. `_reconcile_blocked_after`'s resume check is driven entirely by
# `board.facts(dep).landed` — a real, board-derived merge fact, never by a
# dependency's in-tick `states` entry — so in principle each hop should
# resolve independently the moment ITS OWN named pre-req actually lands,
# with no special-casing needed for the chain shape. These two tests are the
# repro the review asked for: they exercise `plan_tick` against an actual
# two-hop graph to confirm that holds (it does), and pin the one subtlety a
# naive "chain" fix could get wrong — a leaf must not resume just because
# its immediate pre-req was ITSELF re-queued to `waiting` this same tick;
# only that pre-req actually landing (merged/closed) may resume the leaf.


def test_a_two_hop_after_chain_resumes_in_one_tick_once_both_hops_have_landed():
    """The exact live-incident shape: by the time anyone looked, BOTH the
    root (2282-analog) and the middle (2283-analog) pre-reqs had already
    merged — the middle's queue row just hadn't been reconciled yet. A
    single tick must resolve the WHOLE chain: the middle reconciles to
    `done` via #2055's landed check, and the leaf (2284..2288-analog),
    reading the middle's REAL board fact rather than its stale on-disk
    `blocked` state, resumes to `waiting` in that same tick — no operator
    action, and no second tick needed once both merges are real."""
    root = entry_key(REPO, 1650)
    middle = entry_key(REPO, 1654)
    entries = [
        entry(1650, position=0, state=STATE_DONE),
        entry(
            1654,
            position=1,
            after=(root,),
            state=STATE_BLOCKED,
            attempts=2,
            last_reason=f"pre-req {root} is queued but blocked — it will never satisfy",
        ),
        entry(
            1658,
            position=2,
            after=(middle,),
            state=STATE_BLOCKED,
            attempts=1,
            last_reason=f"pre-req {middle} is queued but blocked — it will never satisfy",
        ),
    ]
    # Both the root AND the middle have actually merged — mirrors the live
    # incident, where #2283 (middle) itself showed `done — issue already
    # merged while blocked (#2055)` once anyone looked.
    plan = plan_tick(entries, board(merged=(1650, 1654)), capacity=3)

    middle_reconcile = next(r for r in plan.reconciles if r.key == middle)
    assert middle_reconcile.outcome == "done"
    assert middle_reconcile.updates["state"] == STATE_DONE

    leaf_reconcile = next(r for r in plan.reconciles if r.key == entry_key(REPO, 1658))
    assert leaf_reconcile.outcome == "resumed"
    assert leaf_reconcile.updates["state"] == STATE_WAITING
    assert leaf_reconcile.updates["attempts"] == 0
    assert leaf_reconcile.updates["resumes"] == 1

    # …and, exactly like the single-hop case, the freshly-resumed leaf falls
    # straight into this SAME tick's launch selection.
    assert plan.launch is not None and plan.launch.issue == 1658


def test_a_two_hop_after_chain_leaf_cascades_to_waiting_but_does_not_launch_until_its_own_prereq_lands():
    """Once the root lands, the middle resumes to `waiting` THIS tick
    (single-hop #2362 behaviour) — and, because the root is fully landed,
    the middle also happens to be immediately launchable and takes this
    tick's `plan.launch`. The leaf's own named pre-req is the MIDDLE, which
    has only been re-queued, not merged — so the leaf must NOT be treated
    as satisfied/launchable, even though `states[middle]` already reads
    `waiting` by the time the leaf is walked in this same tick.

    #2756: the middle is no longer `blocked`/`failed` either, though — so
    the leaf's frozen "it will never satisfy" claim about the middle is now
    equally false, one hop up the chain from the issue's original repro.
    The same partial-satisfaction resume that clears the middle must
    cascade to the leaf too: it comes back `waiting`, with a live
    "waiting on middle" reason, rather than staying pinned `blocked` with a
    stale, now-false reason of its own. Resuming to `waiting` is NOT the
    same as declaring it launchable — :func:`_resolve_prereqs` still gates
    the leaf's actual launch on the middle reaching real `facts.landed`,
    which is the guard against a naive "propagate through `states`" chain
    fix this test originally pinned."""
    root = entry_key(REPO, 1650)
    middle = entry_key(REPO, 1654)
    entries = [
        entry(1650, position=0, state=STATE_DONE),
        entry(
            1654,
            position=1,
            after=(root,),
            state=STATE_BLOCKED,
            attempts=2,
            last_reason=f"pre-req {root} is queued but blocked — it will never satisfy",
        ),
        entry(
            1658,
            position=2,
            after=(middle,),
            state=STATE_BLOCKED,
            attempts=1,
            last_reason=f"pre-req {middle} is queued but blocked — it will never satisfy",
        ),
    ]
    # Only the root has landed — the middle has not (it is only about to be
    # re-queued to `waiting` THIS tick, not merged).
    plan = plan_tick(entries, board(merged=(1650,)), capacity=3)

    middle_reconcile = next(r for r in plan.reconciles if r.key == middle)
    assert middle_reconcile.outcome == "resumed"
    assert middle_reconcile.updates["state"] == STATE_WAITING
    # The middle's OWN pre-req (root) is fully landed, so it is immediately
    # launchable and claims this tick's single launch slot.
    assert plan.launch is not None and plan.launch.issue == 1654

    # The leaf ALSO resumes to `waiting` this same tick — no longer pinned
    # `blocked` with a false "will never satisfy" claim about the middle —
    # but it is not the one that launches: its own pre-req (the middle) has
    # not itself reached `facts.landed` yet.
    leaf_reconcile = next(r for r in plan.reconciles if r.key == entry_key(REPO, 1658))
    assert leaf_reconcile.outcome == "resumed"
    assert leaf_reconcile.updates["state"] == STATE_WAITING
    assert plan.launch.issue != 1658


def test_is_unsatisfiable_prereq_reason_recognises_only_the_exact_verdict_shapes():
    from coord.drive_queue import _is_unsatisfiable_prereq_reason

    assert _is_unsatisfiable_prereq_reason(
        "pre-req claude-coordinator#1650 is queued but blocked — it will never satisfy"
    )
    # #2602: the "unknown to the cached board" verdict is now ALSO recognised
    # — `_reconcile_blocked_after`'s self-heal covers it too, given a live
    # re-check.
    assert _is_unsatisfiable_prereq_reason(
        "pre-req claude-coordinator#1650 is not queued, not merged and not "
        "open on the board (unknown issue, or the board has not synced it "
        "— try `coord sync`)"
    )
    assert not _is_unsatisfiable_prereq_reason("dependency cycle: a -> b -> a")
    assert not _is_unsatisfiable_prereq_reason(
        "drive session died without landing the work 2/2 times — giving up"
    )
    assert not _is_unsatisfiable_prereq_reason("")
    assert not _is_unsatisfiable_prereq_reason(None)


def test_a_blocked_entry_that_lands_still_reconciles_to_done_before_any_gate_check():
    """#2055's landed check runs BEFORE #2230's gate re-check — a merged
    issue reconciles to `done` even if a live override would say 'blocked'."""
    entries = [_blocked_entry(309, position=3)]
    plan = plan_tick(
        entries,
        board(merged=(309,)),
        capacity=1,
        live_blocked_gate={entry_key(REPO, 309): True},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE


def test_a_blocked_entry_resumes_from_the_cheap_cached_board_signal_too():
    """No live override at all (the thin-client lane, where `/board` already
    carries a `merge_plan` section for free) — `IssueFacts.merge_gate_status`
    alone is enough evidence to resume."""
    key = entry_key(REPO, 309)
    view = BoardView(issues={key: IssueFacts(known=True, merge_gate_status="READY")})
    entries = [_blocked_entry(309, position=3)]
    plan = plan_tick(entries, view, capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 0


def test_a_blocked_entry_stays_blocked_on_a_cached_still_blocked_board_signal():
    key = entry_key(REPO, 309)
    view = BoardView(
        issues={key: IssueFacts(known=True, merge_gate_status="BLOCKED")}
    )
    entries = [_blocked_entry(309, position=3)]
    plan = plan_tick(entries, view, capacity=1)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_blocked_entry_takes_the_merge_only_path_when_test_review_are_also_clear():
    """#2350: Merge is the only remaining gate — the board already shows
    Test passed and Review approved — so the tick attempts `coord merge
    --only` directly instead of writing `STATE_WAITING` for a relaunch."""
    key = entry_key(REPO, 309)
    entries = [_blocked_entry(309, position=3, resumes=0)]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        live_blocked_gate={key: False},
        merge_only_ready={key: True},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "merge_only"
    # No state write here — the shell decides the entry's real next state
    # from the live outcome of the merge attempt (see `Reconcile`'s
    # `merge_only` outcome docstring).
    assert reconcile.updates == {}
    assert plan.merge_only == (entries[0],)
    # No capacity spent, no relaunch competing for a slot.
    assert plan.launch is None


def test_a_blocked_entry_with_review_still_pending_falls_through_to_the_ordinary_resume():
    """The negative case: the gate reads clear, but `merge_only_ready` is
    False (Test not yet run, or Review not yet in) — the shortcut must not
    fire early; this takes EXACTLY the pre-#2350 `resumed` path."""
    key = entry_key(REPO, 309)
    entries = [_blocked_entry(309, position=3, resumes=0)]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        live_blocked_gate={key: False},
        merge_only_ready={key: False},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["resumes"] == 1
    assert plan.merge_only == ()
    assert plan.launch is not None and plan.launch.issue == 309


def test_a_blocked_entry_with_no_merge_only_signal_at_all_also_falls_through():
    """Absence (the shell's `_fetch_merge_only_ready` never computed a
    reading for this key — no PR yet, an unreadable config, etc.) must
    degrade to today's behaviour too, not just an explicit `False`."""
    key = entry_key(REPO, 309)
    entries = [_blocked_entry(309, position=3, resumes=0)]
    plan = plan_tick(
        entries, board(), capacity=1, live_blocked_gate={key: False}
    )
    assert plan.reconciles[0].outcome == "resumed"
    assert plan.merge_only == ()


def test_a_blocked_entry_past_the_resume_ceiling_never_takes_the_merge_only_path():
    """The #2230 oscillation ceiling outranks #2350: an entry that has
    already cycled MAX_BLOCKED_RESUMES times stays blocked — whether the
    next chance would have been a relaunch or a direct merge attempt."""
    from coord.drive_queue import MAX_BLOCKED_RESUMES

    key = entry_key(REPO, 309)
    entries = [_blocked_entry(309, position=3, resumes=MAX_BLOCKED_RESUMES)]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        live_blocked_gate={key: False},
        merge_only_ready={key: True},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "oscillating"
    assert plan.merge_only == ()
    assert plan.launch is None


def test_a_parked_entry_takes_the_merge_only_path_on_a_live_gate_recheck():
    """The `parked` counterpart (#1891/#2182's live re-check), via the SAME
    `live_ci_gate`-clear branch a `resumed` reconcile would otherwise take."""
    key = entry_key(REPO, 2350)
    entries = [entry(2350, position=3, state="parked", attempts=0)]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        live_ci_gate={key: False},
        merge_only_ready={key: True},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "merge_only"
    assert reconcile.updates == {}
    assert plan.merge_only == (entries[0],)
    assert plan.launch is None


def test_a_parked_entry_with_review_still_pending_falls_through_to_the_ordinary_resume():
    key = entry_key(REPO, 2350)
    entries = [entry(2350, position=3, state="parked", attempts=0)]
    plan = plan_tick(
        entries,
        board(),
        capacity=1,
        live_ci_gate={key: False},
        merge_only_ready={key: False},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "resumed"
    assert reconcile.updates["state"] == STATE_WAITING
    assert plan.merge_only == ()
    assert plan.launch is not None and plan.launch.issue == 2350


def test_is_permanent_block_reason_recognises_both_markers_and_nothing_else():
    from coord.drive_queue import is_permanent_block_reason

    assert is_permanent_block_reason("... (#1844); blocking without spending an attempt")
    assert is_permanent_block_reason("... (#2019); blocking without spending an attempt")
    assert not is_permanent_block_reason(
        "drive session died without landing the work 2/2 times — giving up"
    )
    assert not is_permanent_block_reason("")
    assert not is_permanent_block_reason(None)


def test_a_genuinely_dead_drive_without_ci_pending_still_retries_normally():
    """No regression: without `merge_ci_pending`, a dead drive takes the
    EXACT pre-#1891 path — this is byte-for-byte
    `test_a_dead_drive_is_requeued_at_the_same_position_with_an_attempt_spent`
    with an explicit (empty) `board()`, pinning that the new branch is
    opt-in, not a default."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "retry"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 1


# ── plan_tick: a permanent dispatch refusal blocks straight away (#1844) ────
#
# The defect #1845 did NOT fix: `_reconcile_running`'s death branch treats a
# drive refused by a deterministic pre-dispatch guard (`enforce_oracle_
# readiness`, `enforce_epic_dispatch_guard`) exactly like a genuine crash —
# it retries the identical guaranteed-to-fail dispatch, burns an attempt, and
# only reaches `blocked` after `max_attempts` is exhausted. This is the exact
# #1817 overnight shape: two identical, fully actionable refusals were
# retried and only THEN blocked, discarding the guard's own remedy along the
# way (`exit_reasons` alone, #1845's fix, only changes the WORDING of that
# outcome — see the tests above). `exit_refused` is what changes the
# DECISION: straight to `blocked`, attempts untouched, on the FIRST tick.


REFUSAL = (
    "drive exited for claude-coordinator#1817 (exit_code=5): dispatch failed: "
    "Issue #1817 is part of oracle-opted-in milestone ms-51 (Gate A "
    "satisfied) but has no acceptance slice yet — run `coord acceptance "
    "author claude-coordinator <tracking_issue> --issue 1817` first."
)


def test_a_permanent_refusal_blocks_immediately_with_attempts_unspent():
    """The acceptance criterion, asserted the way the issue insists on: on
    the attempt counter, not just the final state."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): REFUSAL},
        exit_refused={entry_key(REPO, 1650): True},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "refused"
    assert reconcile.occupies is False
    # NOT `retry`: no requeue, no attempt spent — check both the reconcile
    # and the paired Blocked never touch `attempts` at all (a bare `0` would
    # also satisfy "attempts == 0" but wrongly imply a write happened).
    assert "attempts" not in reconcile.updates
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1650)]
    blocked = plan.blocked[0]
    assert "attempts" not in blocked.updates
    assert blocked.updates["state"] == STATE_BLOCKED
    # The guard's own message — remedy included — survives verbatim into
    # both the reconcile log line and what `coord drive-queue list`/`status`
    # will show as `last_reason`.
    assert REFUSAL in reconcile.reason
    assert REFUSAL in blocked.reason
    assert REFUSAL in blocked.updates["last_reason"]
    assert "coord acceptance author" in blocked.updates["last_reason"]
    assert "drive session died" not in blocked.reason


def test_a_permanent_refusal_blocks_even_on_a_fresh_entrys_first_tick():
    """Not just "no MORE attempts spent" — no attempt at all, ever, for a
    refusal observed on attempt 0. `entry.attempts` (the pre-tick value)
    must be what an operator sees after re-adding this exact entry."""
    entries = [entry(1817, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1817): REFUSAL},
        exit_refused={entry_key(REPO, 1817): True},
    )
    assert plan.reconciles[0].outcome == "refused"
    assert plan.blocked[0].updates.get("attempts") is None
    assert plan.launch is None


def test_exit_reason_without_the_refused_flag_still_retries_normally():
    """`exit_reasons` alone (a genuine death that happened to narrate why,
    #1845) must NOT trip the new `refused` branch — only `exit_refused`
    does. No regression on the #1845 behaviour pinned above."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): REFUSAL},
        exit_refused={},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "retry"
    assert reconcile.updates["attempts"] == 1


def test_a_refused_entry_deep_into_its_attempt_budget_still_spends_none():
    """`exit_refused` short-circuits regardless of how many attempts this
    entry has already burned on genuine deaths — the LAST attempt is not
    "closer to exhausted", it is still a refusal, still costs nothing."""
    entries = [
        entry(1817, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1817): REFUSAL},
        exit_refused={entry_key(REPO, 1817): True},
    )
    assert plan.reconciles[0].outcome == "refused"  # not "exhausted"
    assert "attempts" not in plan.blocked[0].updates


def test_a_genuine_death_still_retries_and_exhausts_with_no_refused_flag():
    """Regression bar: the retry mechanism this issue explicitly leaves
    alone. Three genuine deaths recovered on attempt 2 the same overnight
    run #1844 is named for — this must keep working exactly as #1794/#1845
    left it when `exit_refused` says nothing (the default, `None`)."""
    entries = [entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles[0].outcome == "exhausted"
    assert plan.blocked[0].updates["attempts"] == DEFAULT_MAX_ATTEMPTS


# ── plan_tick: a dead-end exit blocks the entry (#2019) ──────────────────────
#
# The second PERMANENT cause, sharing #1844's branch. Before #2019 this shape
# never reached the queue at all — the drive did not exit, it counted `no
# state change` for 140 minutes while holding the tmux session, the queue slot
# and (since #1972) the whole repo's capacity lane. Now it exits within one
# poll, and the tick's job is to make that visible in `coord drive-queue
# list`/`status` with the SPECIFIC reason, without spending an attempt on a
# relaunch that would reproduce the identical dead end.

DEAD_END_REASON = (
    "drive exited for claude-coordinator#1956 (exit_code=6): DEAD END "
    "[review_terminal_no_verdict] — this row is terminal and unactionable; "
    "exiting instead of polling (#2019).\n   review c9b489b2333e reached "
    "status=done carrying NO verdict.\n   Recover: coord report-result "
    "--assignment c9b489b2333e --status done --verdict "
    "<approve|request-changes> --verdict-source recovered ..."
)


def test_a_dead_end_exit_blocks_immediately_with_attempts_unspent():
    """#2019 acceptance: "the queue entry is left `blocked` with a reason
    naming the specific dead end and the recovery command"."""
    entries = [entry(1956, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1956): DEAD_END_REASON},
        exit_dead_end={entry_key(REPO, 1956): True},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "dead_end"
    assert reconcile.occupies is False
    # No relaunch, no attempt spent — a dead end is not a flaky death.
    assert "attempts" not in reconcile.updates
    assert plan.launch is None
    blocked = plan.blocked[0]
    assert blocked.key == entry_key(REPO, 1956)
    assert "attempts" not in blocked.updates
    assert blocked.updates["state"] == STATE_BLOCKED
    # The specific dead end AND its recovery command reach `last_reason`,
    # which is what `coord drive-queue list`/`status` render. "no state
    # change in 140.558m" is what this replaces.
    last_reason = blocked.updates["last_reason"]
    assert "review_terminal_no_verdict" in last_reason
    assert "coord report-result --assignment c9b489b2333e" in last_reason
    assert "drive session died" not in last_reason


def test_a_dead_end_reason_is_not_reported_as_a_pre_dispatch_refusal():
    """Two permanent causes, one branch — but never one wording. #1844's
    "refused by a pre-dispatch guard" would send an operator hunting for a
    guard that never fired."""
    entries = [entry(1956, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1956): DEAD_END_REASON},
        exit_dead_end={entry_key(REPO, 1956): True},
    )
    reason = plan.blocked[0].reason
    assert "pre-dispatch guard" not in reason
    assert "terminal and unactionable" in reason
    assert "#2019" in reason


def test_a_dead_end_entry_deep_into_its_attempt_budget_still_spends_none():
    """Same short-circuit property #1844 asserts: how many attempts a genuine
    death already burned is irrelevant — a dead end still costs nothing and
    reports as `dead_end`, never `exhausted`."""
    entries = [entry(1956, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1956): DEAD_END_REASON},
        exit_dead_end={entry_key(REPO, 1956): True},
    )
    assert plan.reconciles[0].outcome == "dead_end"
    assert "attempts" not in plan.blocked[0].updates


def test_exit_reason_without_the_dead_end_flag_still_retries_normally():
    """The #1845 regression bar, restated for the new flag: narrating a
    reason must not by itself block anything."""
    entries = [entry(1956, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1956): DEAD_END_REASON},
        exit_dead_end={},
    )
    assert plan.reconciles[0].outcome == "retry"
    assert plan.reconciles[0].updates["attempts"] == 1


def test_refused_still_wins_and_still_reads_as_refused():
    """#1844's path must be untouched by #2019 sharing its branch. The two
    exit codes are mutually exclusive by construction, but if both flags ever
    arrive for one key the pre-dispatch wording is what shows."""
    entries = [entry(1817, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1817): REFUSAL},
        exit_refused={entry_key(REPO, 1817): True},
        exit_dead_end={entry_key(REPO, 1817): True},
    )
    assert plan.reconciles[0].outcome == "refused"
    assert "pre-dispatch guard" in plan.blocked[0].reason


# ── plan_tick: the startup grace window (#1794) ──────────────────────────────
#
# 2026-08-03, the first unattended run of the #1756 timer: a tick 40s after a
# launch found no tmux session and no board work (the drive was still coming
# up — measured 19:13:09 launch → 19:15:22 `drive loop started`), fell through
# every branch of `_reconcile_running` to `retry`, spent an attempt, and
# launched a SECOND `coord drive` for the same issue. The two ticks were 40s
# apart because `docs/DRIVE_QUEUE.md` §2's install sequence fires one
# (`enable --now`) and then its own verification step fires another.


def running_since(issue: int, age: float, **kw) -> QueueEntry:
    """A `running` entry launched *age* seconds before :data:`NOW`."""
    return entry(issue, state=STATE_RUNNING, launched_at=NOW - age, **kw)


def test_a_tick_seconds_after_the_launch_leaves_the_entry_running():
    """THE regression for #1794 — the 40s-later tick from the incident."""
    entries = [running_since(1762, 40.0, position=1, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)

    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "starting"
    assert reconcile.occupies is True
    # The three things the incident got wrong, asserted one by one.
    assert "state" not in reconcile.updates  # stays `running`
    assert "attempts" not in reconcile.updates  # no attempt spent
    assert plan.launch is None  # no duplicate drive
    assert plan.occupied == 1
    assert "40s ago" in reconcile.reason


def test_a_starting_drive_holds_its_slot_against_the_rest_of_the_queue():
    entries = [
        running_since(1762, 5.0, position=0),
        entry(1763, position=1),
    ]
    plan = plan_tick(entries, board(open_=(1763,)), capacity=1, now=NOW)
    assert plan.occupied == 1
    assert plan.launch is None
    # At capacity is the queue working, not a stall — no escalation.
    assert plan.alert is None


def test_a_starting_entry_is_never_relaunched_even_if_something_requeues_it():
    """The launch-side half of the guard.

    A `waiting` row with a fresh `launched_at` means a drive went up moments
    ago whatever the queue state now says. Starting a second one is exactly
    the #1794 failure, so the walk refuses it rather than leaning on `coord
    drive`'s per-issue flock to catch it.
    """
    entries = [entry(1762, position=0, launched_at=NOW - 10.0, deferrals=0)]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)
    assert plan.launch is None
    assert [d.key for d in plan.deferrals] == [entry_key(REPO, 1762)]
    assert "second `coord drive` is refused" in plan.deferrals[0].reason
    assert plan.deferrals[0].updates["deferrals"] == 1


def test_the_window_never_starves_a_later_entry_that_is_genuinely_ready():
    """The cooldown defers ONE entry; it does not close the queue."""
    entries = [
        entry(1762, position=0, launched_at=NOW - 10.0),
        entry(1763, position=1),
    ]
    plan = plan_tick(entries, board(), capacity=2, now=NOW)
    assert plan.launch is not None and plan.launch.issue == 1763


def test_a_live_session_still_wins_over_the_startup_window():
    entries = [running_since(1762, 5.0)]
    plan = plan_tick(entries, board(sessions=(1762,)), capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "alive"


def test_a_merged_issue_still_wins_over_the_startup_window():
    entries = [running_since(1762, 5.0)]
    plan = plan_tick(entries, board(merged=(1762,)), capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "done"
    assert plan.reconciles[0].updates["state"] == STATE_DONE


def test_1660_held_is_unchanged_by_the_startup_window():
    """#1660's `held` keeps its own branch, inside the window and outside it."""
    for age in (5.0, DRIVE_STARTUP_GRACE_SECONDS + 60.0):
        plan = plan_tick(
            [running_since(1762, age)], board(active=(1762,)), capacity=1, now=NOW
        )
        assert plan.reconciles[0].outcome == "held", age
        assert plan.reconciles[0].occupies is True
        assert "state" not in plan.reconciles[0].updates
        assert plan.launch is None


def test_death_detection_still_reaches_blocked_at_max_attempts():
    """The window delays a death by at most one interval; it never hides one."""
    old = DRIVE_STARTUP_GRACE_SECONDS + 1
    first = plan_tick(
        [running_since(1762, old, attempts=0)], board(), capacity=1, now=NOW
    )
    assert first.reconciles[0].outcome == "retry"
    assert first.reconciles[0].updates["attempts"] == 1

    second = plan_tick(
        [running_since(1762, old, attempts=DEFAULT_MAX_ATTEMPTS - 1)],
        board(),
        capacity=1,
        now=NOW,
    )
    assert second.reconciles[0].outcome == "exhausted"
    assert [b.key for b in second.blocked] == [entry_key(REPO, 1762)]
    assert second.blocked[0].updates["state"] == STATE_BLOCKED
    assert second.blocked[0].updates["attempts"] == DEFAULT_MAX_ATTEMPTS


def test_a_row_with_no_launch_stamp_keeps_the_pre_1794_behaviour():
    """A pre-DQ-1 row, or one a human flipped to `running` by hand."""
    plan = plan_tick(
        [entry(1762, state=STATE_RUNNING, launched_at=None)],
        board(),
        capacity=1,
        now=NOW,
    )
    assert plan.reconciles[0].outcome == "retry"


def test_a_backwards_clock_jump_cannot_pin_an_entry_in_the_window():
    """A `launched_at` in the future must not make an entry un-retryable."""
    plan = plan_tick(
        [entry(1762, state=STATE_RUNNING, launched_at=NOW + 10_000.0)],
        board(),
        capacity=1,
        now=NOW,
    )
    assert plan.reconciles[0].outcome == "retry"


def test_omitting_the_clock_disables_the_window_entirely():
    """`now=None` is the pure-logic caller's opt-out, not a silent grace."""
    plan = plan_tick([running_since(1762, 5.0)], board(), capacity=1)
    assert plan.reconciles[0].outcome == "retry"


def test_the_grace_window_is_injectable():
    entries = [running_since(1762, 60.0)]
    assert (
        plan_tick(entries, board(), capacity=1, now=NOW, grace_seconds=30.0)
        .reconciles[0]
        .outcome
        == "retry"
    )
    assert (
        plan_tick(entries, board(), capacity=1, now=NOW, grace_seconds=120.0)
        .reconciles[0]
        .outcome
        == "starting"
    )


def test_the_default_window_clears_the_measured_startup_time():
    """~2 min measured on a loaded dellserver; the default must beat it."""
    assert DRIVE_STARTUP_GRACE_SECONDS >= 300.0


# ── plan_tick: the cross-host guard (#1870) ──────────────────────────────────
#
# 2026-08-06: two live `coord drive` sessions on the same issue at once. One
# was launched by hand on `elitebook` and was 47 minutes (2841s) into a
# healthy run — `work=done`, `test=running`. The other was a duplicate the
# TIMER's own tick launched on `dellserver` after concluding, from ITS local
# (and therefore blind) tmux read, that the elitebook session had "died
# without landing the work". #1794's grace window does not help here: the
# session was three orders of magnitude past any plausible grace and still
# invisible — the miss is not transient, it is structural, because liveness
# is always a local `tmux list-sessions` and the queue is fleet-global.


def test_a_drive_launched_on_another_host_is_unknown_not_dead():
    """THE regression for #1870 — the elitebook/dellserver duplicate launch."""
    entries = [
        running_since(
            1811,
            DRIVE_STARTUP_GRACE_SECONDS + 2841.0,
            position=0,
            attempts=0,
            launch_host="elitebook",
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW, local_host="dellserver"
    )

    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "unknown"
    assert reconcile.occupies is True
    # The three things the incident got wrong, asserted one by one — the same
    # shape as #1794's own regression test above.
    assert "state" not in reconcile.updates  # stays `running`
    assert "attempts" not in reconcile.updates  # no attempt spent
    assert plan.launch is None  # no duplicate drive
    assert plan.occupied == 1
    assert "elitebook" in reconcile.reason
    assert "dellserver" in reconcile.reason


def test_a_cross_host_entry_is_never_relaunched_even_with_free_capacity():
    """AC: no second drive for an entry with a live session on another host."""
    entries = [
        running_since(
            1811,
            DRIVE_STARTUP_GRACE_SECONDS + 100.0,
            position=0,
            launch_host="elitebook",
        ),
        entry(1812, position=1),
    ]
    plan = plan_tick(
        entries,
        board(open_=(1812,)),
        capacity=5,
        now=NOW,
        local_host="dellserver",
        # Same repo on both rows: the #1870 guard is what this test is about,
        # so #1972's per-repo ceiling is raised out of the way (with the
        # default of 1, #1811's cross-host slot would legitimately defer
        # #1812 and the assertion below would be testing the wrong feature).
        max_parallel_per_repo=5,
    )
    # Free capacity and a fully eligible successor — #1812 launches, #1811
    # does not get a second drive.
    assert plan.launch is not None and plan.launch.issue == 1812
    assert plan.occupied == 1


def test_a_same_host_entry_still_reconciles_normally():
    """The guard is scoped to a MISMATCH — this host's own launch is unaffected."""
    entries = [
        running_since(
            1811,
            DRIVE_STARTUP_GRACE_SECONDS + 1.0,
            attempts=0,
            launch_host="dellserver",
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW, local_host="dellserver"
    )
    assert plan.reconciles[0].outcome == "retry"
    assert plan.reconciles[0].updates["attempts"] == 1


def test_the_host_match_is_case_insensitive():
    entries = [
        running_since(
            1811,
            DRIVE_STARTUP_GRACE_SECONDS + 1.0,
            launch_host="DellServer",
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW, local_host="dellserver"
    )
    assert plan.reconciles[0].outcome == "retry"


def test_an_entry_with_no_recorded_launch_host_keeps_the_pre_1870_behaviour():
    """AC: entries predating the column (or hand-edited) behave exactly as today."""
    entries = [
        running_since(1811, DRIVE_STARTUP_GRACE_SECONDS + 1.0, attempts=0)
    ]
    assert entries[0].launch_host == ""
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW, local_host="dellserver"
    )
    assert plan.reconciles[0].outcome == "retry"


def test_omitting_local_host_disables_the_cross_host_check_entirely():
    """`local_host=None` is the pure-logic caller's opt-out, like `now=None`."""
    entries = [
        running_since(
            1811,
            DRIVE_STARTUP_GRACE_SECONDS + 1.0,
            launch_host="elitebook",
        )
    ]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "retry"


def test_a_live_session_still_wins_over_a_host_mismatch():
    """A real positive signal always outranks the cross-host guard."""
    entries = [running_since(1811, 5.0, launch_host="elitebook")]
    plan = plan_tick(
        entries,
        board(sessions=(1811,)),
        capacity=1,
        now=NOW,
        local_host="dellserver",
    )
    assert plan.reconciles[0].outcome == "alive"


def test_active_work_still_wins_over_a_host_mismatch():
    """#1660's `held` is a board-global fact; it must not be shadowed by #1870."""
    entries = [running_since(1811, 5.0, launch_host="elitebook")]
    plan = plan_tick(
        entries,
        board(active=(1811,)),
        capacity=1,
        now=NOW,
        local_host="dellserver",
    )
    assert plan.reconciles[0].outcome == "held"


def test_landed_still_wins_over_a_host_mismatch():
    entries = [running_since(1811, 5.0, launch_host="elitebook")]
    plan = plan_tick(
        entries,
        board(merged=(1811,)),
        capacity=1,
        now=NOW,
        local_host="dellserver",
    )
    assert plan.reconciles[0].outcome == "done"


def test_a_cross_host_entry_holds_its_slot_against_the_rest_of_the_queue():
    entries = [
        running_since(1811, 5.0, position=0, launch_host="elitebook"),
        entry(1812, position=1),
    ]
    plan = plan_tick(
        entries, board(open_=(1812,)), capacity=1, now=NOW, local_host="dellserver"
    )
    assert plan.occupied == 1
    assert plan.launch is None
    # At capacity is the queue working, not a stall — no escalation.
    assert plan.alert is None


# ── rendering ────────────────────────────────────────────────────────────────


def test_render_plan_names_the_launch_and_the_defer_reason():
    entries = [
        entry(1650, position=0, after=(entry_key(REPO, 1),)),
        entry(1654, position=1, machine="dellserver"),
    ]
    lines = render_plan(
        plan_tick(entries, board(open_=(1,)), capacity=1), dry_run=True
    )
    text = "\n".join(lines)
    assert "would launch claude-coordinator#1654 on dellserver" in text
    assert "defer claude-coordinator#1650" in text
    assert "0/1 occupied" in text


def test_render_plan_narrates_a_starting_drive_and_the_full_slot():
    """#1794 was diagnosed from a journal, so the journal has to say it."""
    entries = [
        entry(1762, position=0, state=STATE_RUNNING, launched_at=NOW - 41.0),
        entry(1763, position=1),
    ]
    text = "\n".join(
        render_plan(plan_tick(entries, board(open_=(1763,)), capacity=1, now=NOW))
    )
    assert "reconcile claude-coordinator#1762: starting" in text
    assert "startup grace window (#1794)" in text
    assert "no launch — at capacity (1/1 occupied)" in text
    assert "retry" not in text


def test_render_plan_narrates_a_cross_host_entry_as_unknown():
    """#1870 was diagnosed from a journal too; the journal has to say it."""
    entries = [
        entry(
            1811,
            position=0,
            state=STATE_RUNNING,
            launched_at=NOW - (DRIVE_STARTUP_GRACE_SECONDS + 2841.0),
            launch_host="elitebook",
        ),
    ]
    text = "\n".join(
        render_plan(
            plan_tick(entries, board(), capacity=1, now=NOW, local_host="dellserver")
        )
    )
    assert "reconcile claude-coordinator#1811: unknown" in text
    assert "elitebook" in text
    assert "not this host" in text
    assert "no launch — at capacity (1/1 occupied)" in text
    assert "retry" not in text


# ── plan_tick: deploy gates (#1757, scoped by #2186) ─────────────────────────
#
# `merged != live`. These pin the decision half of the gate: what fires it,
# what does NOT fire it, and — since #2186 — HOW FAR a fired gate reaches.
# The default scope (`entry`, unset in `held()` below) holds only entries
# that name the gated key in their own `after=`; `HOLD_SCOPE_FLEET` is the
# pre-#2186 whole-queue stop, kept for an explicit `--scope=fleet`.


def held(issue: int, **kw) -> QueueEntry:
    """A `--hold-after` entry whose gate has already fired (default scope)."""
    base = {
        "state": STATE_DONE,
        "hold_after": True,
        "hold_reason": "restart coord-serve",
        "hold_state": HOLD_FIRED,
    }
    base.update(kw)
    return entry(issue, **base)


def test_a_gate_fires_the_tick_its_entry_reaches_done():
    plan = plan_tick(
        [
            entry(
                1,
                state=STATE_RUNNING,
                hold_after=True,
                hold_reason="deploy",
                hold_state=HOLD_ARMED,
            ),
            entry(2, after=(entry_key(REPO, 1),)),
        ],
        board(merged=(1,), open_=(2,)),
        capacity=1,
    )
    assert plan.launch is None
    assert plan.held is not None
    assert plan.held.outcome == "fired"
    assert plan.held.scope == HOLD_SCOPE_ENTRY
    assert dict(plan.writes())[entry_key(REPO, 1)]["hold_state"] == HOLD_FIRED


def test_2186_a_fired_entry_scoped_gate_does_not_block_an_unrelated_successor():
    """THE #2186 fix, in one assertion: entry-scoped is the default.

    Black-box acceptance scenario from the issue: a fired gate on entry A
    (position 0) and a fully eligible, UNRELATED entry B (position 1) — B
    launches in the same tick, even though A's gate is still closed.
    """
    plan = plan_tick(
        [held(1), entry(2)],
        board(open_=(2,)),
        capacity=4,
    )
    assert plan.free_slots == 4
    assert plan.launch is not None and plan.launch.issue == 2
    # The gate is still on record as closed — it just never stopped the tick.
    assert plan.held is not None
    assert plan.held.outcome == "held"
    assert not plan.held.stops_fleet


def test_a_fired_entry_scoped_gate_still_holds_its_own_dependent():
    """The other half of #2186: scoping the hold must not mean removing it."""
    plan = plan_tick(
        [held(1), entry(2, after=(entry_key(REPO, 1),))],
        board(),
        capacity=4,
    )
    assert plan.launch is None
    assert [d.key for d in plan.deferrals] == [entry_key(REPO, 2)]
    reason = plan.deferrals[0].reason
    assert "deploy gate" in reason
    assert "restart coord-serve" in reason
    # #2186 acceptance: the reason is written to the row every tick (via the
    # ordinary deferral path), not frozen the way a queue-wide short-circuit
    # would leave it — this is what keeps `coord drive-queue list` honest.
    assert dict(plan.writes())[entry_key(REPO, 2)]["last_reason"] == reason


def test_a_fired_fleet_scoped_gate_still_blocks_a_fully_eligible_successor():
    """The pre-#2186 behaviour, preserved for an explicit `--scope=fleet`."""
    plan = plan_tick(
        [held(1, hold_scope=HOLD_SCOPE_FLEET), entry(2)],
        board(open_=(2,)),
        capacity=4,
    )
    assert plan.free_slots == 4
    assert plan.launch is None
    assert plan.deferrals == ()
    assert "restart coord-serve" in plan.alert.reason
    assert plan.alert.command == "coord drive-queue resume"
    assert plan.held.stops_fleet


def test_an_armed_gate_on_an_unlanded_entry_holds_nothing():
    plan = plan_tick(
        [entry(1, hold_after=True, hold_state=HOLD_ARMED), entry(2)],
        board(open_=(1, 2)),
        capacity=1,
    )
    assert plan.held is None
    assert plan.launch is not None and plan.launch.issue == 1


def test_a_released_gate_holds_nothing():
    plan = plan_tick(
        [held(1, hold_state=HOLD_RELEASED), entry(2)],
        board(open_=(2,)),
        capacity=1,
    )
    assert plan.held is None
    assert plan.launch is not None and plan.launch.issue == 2


def test_a_hold_after_entry_that_dies_out_of_attempts_blocks_and_never_fires():
    """`blocked` already stops the queue — a second alert would just be noise."""
    plan = plan_tick(
        [
            entry(
                1,
                state=STATE_RUNNING,
                attempts=DEFAULT_MAX_ATTEMPTS - 1,
                hold_after=True,
                hold_state=HOLD_ARMED,
            )
        ],
        board(),
        capacity=1,
    )
    assert plan.held is None
    assert plan.holds == ()
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1)]
    assert "HELD" not in (plan.alert.reason if plan.alert else "")


def test_a_failing_probe_stays_held_and_increments_a_typed_attempt_count():
    key = entry_key(REPO, 1)
    plan = plan_tick(
        [
            held(1, resume_when="curl -sf x", hold_probes=2),
            entry(2, after=(key,)),
        ],
        board(open_=(2,)),
        capacity=1,
        probes={key: ProbeResult(key, False, "exit 7")},
    )
    assert plan.launch is None
    assert plan.held.probes == 3
    assert dict(plan.writes())[key]["hold_probes"] == 3
    # #2186: the dependent's own deferral carries the probe detail now — the
    # fleet-wide `QUEUE HELD` alert this used to come from only fires for an
    # explicit `--scope=fleet` gate.
    alert_text = " ".join(plan.alert.details) if plan.alert else ""
    assert "attempt 3 failed" in alert_text
    assert dict(plan.writes())[entry_key(REPO, 2)]["last_reason"] == (
        plan.deferrals[0].reason
    )


def test_a_passing_probe_releases_and_launches_in_the_same_tick():
    key = entry_key(REPO, 1)
    plan = plan_tick(
        [held(1, resume_when="curl -sf x", hold_probes=4), entry(2)],
        board(open_=(2,)),
        capacity=1,
        probes={key: ProbeResult(key, True, "exit 0")},
    )
    assert plan.held is None
    assert plan.launch is not None and plan.launch.issue == 2
    writes = dict(plan.writes())
    assert writes[key]["hold_state"] == HOLD_RELEASED
    assert writes[key]["hold_probes"] == 0
    assert plan.alert is None


def test_a_fleet_scoped_gate_with_no_probe_result_stays_held_and_writes_nothing():
    """Manual-resume-only, and a probe the shell could not run. Fail closed."""
    plan = plan_tick(
        [held(1, hold_scope=HOLD_SCOPE_FLEET), entry(2)],
        board(open_=(2,)),
        capacity=1,
    )
    assert plan.launch is None
    assert plan.writes() == []
    assert "release manually" in " ".join(plan.alert.details)


def test_an_entry_scoped_gate_with_no_probe_result_defers_its_dependent_live():
    """Same fail-closed rule, but scoped: only the dependent is affected, and
    its reason is written fresh every tick rather than frozen."""
    plan = plan_tick(
        [held(1), entry(2, after=(entry_key(REPO, 1),))],
        board(),
        capacity=4,
    )
    assert plan.launch is None
    assert len(plan.deferrals) == 1
    d = plan.deferrals[0]
    assert d.key == entry_key(REPO, 2)
    assert "restart coord-serve" in d.reason
    assert dict(plan.writes())[entry_key(REPO, 2)]["last_reason"] == d.reason


def test_only_an_already_fired_gate_is_offered_for_probing():
    from coord.drive_queue import pending_probe_targets

    entries = [
        entry(1, hold_after=True, hold_state=HOLD_ARMED, resume_when="a"),
        held(2, resume_when="b"),
        held(3),  # fired, but no probe declared
        held(4, hold_state=HOLD_RELEASED, resume_when="d"),
    ]
    assert [e.issue for e in pending_probe_targets(entries)] == [2]


def test_render_plan_says_why_a_fleet_scoped_hold_stopped_everything():
    plan = plan_tick(
        [held(1, hold_scope=HOLD_SCOPE_FLEET), entry(2)],
        board(open_=(2,)),
        capacity=1,
    )
    text = "\n".join(render_plan(plan))
    assert "hold claude-coordinator#1: held" in text
    assert "[scope=fleet]" in text
    assert "no launch — HELD" in text
    assert "fleet-wide" in text
    assert "coord drive-queue resume" in text


def test_render_plan_narrates_an_entry_scoped_hold_as_a_defer_not_a_queue_stop():
    plan = plan_tick(
        [held(1), entry(2, after=(entry_key(REPO, 1),))],
        board(),
        capacity=1,
    )
    text = "\n".join(render_plan(plan))
    assert "hold claude-coordinator#1: held" in text
    assert "[scope=fleet]" not in text
    assert "no launch — HELD" not in text
    assert (
        f"defer {entry_key(REPO, 2)}: waiting on {entry_key(REPO, 1)}'s deploy gate"
        in text
    )


# ══════════════════════════════════════════════════════════════════════════
# #2314: the tick refuses to launch when THIS host's own `coord` is a
# drifted editable checkout — escalating `coord.cli`'s advisory-only
# `_warn_if_editable_checkout_moved` warning into an actual refusal. Mirrors
# the #2101 cordon tests in tests/test_release_cordon_2101.py: same shape
# (host-scoped, reconciliation still runs, launch is refused before
# capacity), different trigger.
# ══════════════════════════════════════════════════════════════════════════


def test_the_tick_refuses_to_launch_when_this_hosts_coord_has_drifted():
    plan = plan_tick(
        [entry(1)],
        board(open_=(1,)),
        capacity=1,
        editable_drift=("/home/dev/src/code-coordinator", "'issue-9999-scratch'"),
    )

    assert plan.launch is None
    assert plan.drift_reason == (
        "drifted onto 'issue-9999-scratch' (/home/dev/src/code-coordinator)"
    )
    # Same "say why" contract the #2101 cordon alert carries.
    assert plan.alert is not None
    assert "issue-9999-scratch" in plan.alert.reason
    assert plan.alert.command == "git -C /home/dev/src/code-coordinator checkout main"
    assert any("issue-9999-scratch" in line for line in render_plan(plan))


def test_a_clean_checkout_launches_normally():
    """`editable_drift=None` (the default, and what a release install or a
    checkout still cleanly on `main` produces) must not change behaviour at
    all — this is the overwhelming common case."""
    plan = plan_tick(
        [entry(1)],
        board(open_=(1,)),
        capacity=1,
        editable_drift=None,
    )

    assert plan.launch is not None
    assert plan.drift_reason == ""


def test_a_drifted_tick_still_reconciles_so_the_drift_can_actually_be_fixed():
    """Same #2110 reasoning the cordon gate already relies on: a refusal
    that also froze the queue's view of reality would leave a finished
    drive's `running` row pinning propagation forever. A drifted tick is
    exactly `--reconcile-only`, not a frozen one."""
    b = board(closed=(1,))
    plan = plan_tick(
        [entry(1, state=STATE_RUNNING, launch_host="dellserver")],
        b,
        capacity=1,
        local_host="dellserver",
        editable_drift=("/home/dev/src/code-coordinator", "'stale-branch'"),
    )

    assert plan.launch is None
    assert [r.outcome for r in plan.reconciles] == ["done"]
    assert plan.writes(), "the drained row must still be written back"


def test_drift_is_checked_even_when_no_cordon_is_present():
    """The drift gate must not accidentally be wired as cordon-only —
    exercising it with `cordons=None`/no `local_host` (the shape a
    single-machine dev fleet actually passes) still refuses to launch."""
    plan = plan_tick(
        [entry(1)],
        board(open_=(1,)),
        capacity=1,
        cordons=None,
        editable_drift=("/home/dev/src/code-coordinator", "(detached HEAD)"),
    )
    assert plan.launch is None
    assert "detached HEAD" in plan.drift_reason


# ── #2339: `add`-time preflight for a terminal zero-commit ADVISORY ──────────


_EMPTY_BRANCH_REASON = (
    "drive exited for claude-coordinator#3 (exit_code=1): work adv-1 exited "
    "ADVISORY with no commits on its branch 1 time(s) in a row (budget 1, "
    "#2416) — nothing was pushed, so there is nothing to test, review, or "
    "merge."
)


def test_preflight_is_silent_for_an_unqueued_issue_with_no_advisory_row():
    assert add_preflight_notice(REPO, 3, None) == ""
    assert (
        add_preflight_notice(REPO, 3, entry(3), work_aid="a1", work_status="running")
        == ""
    )


def test_preflight_names_coord_retry_with_the_real_assignment_id():
    notice = add_preflight_notice(
        REPO, 3, None, work_aid="adv-1", work_status="advisory", work_machine="precision"
    )
    assert "coord retry adv-1" in notice
    assert "TERMINAL" in notice
    assert "#1606" in notice
    # The escape hatch for the OTHER advisory shape is named too, so an
    # operator whose branch does carry commits is not sent down a path
    # `coord retry` will refuse.
    assert "--accept-advisory" in notice
    assert "coord log adv-1 --machine precision" in notice


def test_preflight_reports_a_stuck_entry_that_this_add_did_not_requeue():
    notice = add_preflight_notice(
        REPO, 3, entry(3, state=STATE_BLOCKED, attempts=2), work_aid="", work_status=""
    )
    assert "did NOT requeue it" in notice
    assert "2 attempt(s) spent" in notice
    assert f"coord drive-queue remove {REPO} 3" in notice
    # No board row was resolvable, so nothing claims to know the cause.
    assert "coord retry" not in notice


def test_preflight_reports_both_halves_cause_before_mechanical_reset():
    notice = add_preflight_notice(
        REPO,
        3,
        entry(3, state=STATE_BLOCKED, attempts=2, last_reason=_EMPTY_BRANCH_REASON),
        work_aid="adv-1",
        work_status="advisory",
    )
    lines = notice.splitlines()
    assert "coord retry adv-1" in notice
    assert "did NOT requeue it" in notice
    # Cause first, mechanical reset second — an operator who runs the
    # remove+add without clearing the row just burns the attempts again.
    retry_at = next(i for i, line in enumerate(lines) if "coord retry adv-1" in line)
    requeue_at = next(i for i, line in enumerate(lines) if "did NOT requeue it" in line)
    assert retry_at < requeue_at


def test_preflight_falls_back_to_the_queues_own_recorded_death():
    """The board leg failed (or the row was already cleared), but the entry's
    own `last_reason` is the zero-commit shape `coord drive` writes."""
    notice = add_preflight_notice(
        REPO, 3, entry(3, last_reason=_EMPTY_BRANCH_REASON)
    )
    assert "zero-commit" in notice
    assert "coord retry <assignment_id>" in notice
    # No id was resolvable, so none is invented.
    assert "coord retry adv-1" not in notice


def test_a_running_entrys_ordinary_death_gets_no_zero_commit_note():
    assert is_empty_branch_death_reason("drive exited: CI red on issue-3") is False
    assert add_preflight_notice(REPO, 3, entry(3, last_reason="CI red")) == ""


def test_is_empty_branch_death_reason_matches_the_drive_advisory_text():
    assert is_empty_branch_death_reason(_EMPTY_BRANCH_REASON) is True
    assert is_empty_branch_death_reason(None) is False


# ── #2944: the guaranteed-false wait — `state ∈ {blocked, parked}`, ─────────
# `attempts == 0`, past the grace window. See claude-coordinator#2900/#2907:
# both sat this way for 10h/22.7h with `coord drive-queue status` reading
# `alert: (none)` the entire time, because nothing was ever launched for
# them, so no sweep — #2230's merge-gate probe included — could ever have
# anything positive to report.


def test_detect_unreachable_waits_flags_a_never_dispatched_blocked_entry():
    stuck = entry(
        2900,
        state=STATE_BLOCKED,
        attempts=0,
        deferrals=207,
        last_reason="exhausted 2/2 attempts",
    )
    waits = detect_unreachable_waits([stuck])
    assert [w.key for w in waits] == [entry_key(REPO, 2900)]
    assert waits[0].deferrals == 207
    assert waits[0].state == STATE_BLOCKED


def test_detect_unreachable_waits_flags_a_never_dispatched_parked_entry():
    # `parked` realistically implies `attempts > 0` (a park is only ever set
    # after a real merge attempt), but the predicate is defined over the
    # state alone — see the module docstring on why this checks the SHAPE,
    # not the mechanism that is known to produce it today.
    stuck = entry(2907, state=STATE_PARKED, attempts=0, deferrals=186)
    waits = detect_unreachable_waits([stuck])
    assert [w.key for w in waits] == [entry_key(REPO, 2907)]


def test_detect_unreachable_waits_does_not_trip_below_the_grace_threshold():
    """A genuinely transient block — freshly blocked, few deferrals — must
    not trip: that would false-positive on the very first tick a `waiting`
    entry lands in `blocked` at all."""
    fresh = entry(1, state=STATE_BLOCKED, attempts=0, deferrals=0)
    at_threshold = entry(
        2, state=STATE_BLOCKED, attempts=0, deferrals=UNREACHABLE_WAIT_MIN_DEFERRALS
    )
    assert detect_unreachable_waits([fresh, at_threshold]) == []
    just_past = entry(
        3,
        state=STATE_BLOCKED,
        attempts=0,
        deferrals=UNREACHABLE_WAIT_MIN_DEFERRALS + 1,
    )
    assert [w.key for w in detect_unreachable_waits([just_past])] == [
        entry_key(REPO, 3)
    ]


def test_detect_unreachable_waits_does_not_trip_for_a_real_attempt():
    """`attempts > 0` means the entry WAS dispatched at least once — it has
    (or had) a real branch/PR/merge-queue row, so a long `blocked` wait is a
    legitimate wait on a real gate, not an unreachable one."""
    real_attempt = entry(4, state=STATE_BLOCKED, attempts=1, deferrals=500)
    assert detect_unreachable_waits([real_attempt]) == []


def test_detect_unreachable_waits_ignores_waiting_and_running_states():
    waiting = entry(5, state=STATE_WAITING, attempts=0, deferrals=500)
    running = entry(6, state=STATE_RUNNING, attempts=0, deferrals=500)
    assert detect_unreachable_waits([waiting, running]) == []


def test_unreachable_wait_alert_names_the_entry_deferrals_and_remedy():
    waits = detect_unreachable_waits(
        [entry(2900, state=STATE_BLOCKED, attempts=0, deferrals=207)]
    )
    alert = unreachable_wait_alert(waits)
    assert f"{REPO}#2900" in alert.reason
    assert "207 deferrals" in alert.reason
    assert "coord drive-queue remove" in alert.command
    assert "coord drive-queue add" in alert.command


# ── #2978: root-only, and the #2273-exhausted root is no longer excluded ────
#
# #2944's alert got today's ms-5 incident exactly backwards: it named eight
# dependents that needed nothing (each blocked on an unsatisfiable `after=`
# pre-req — #2756's to self-heal) and omitted the one entry — the root,
# `attempts > 0` — that actually needed an operator. These tests pin both
# halves of the fix plus the exact incident shape as a regression.

_DISPATCH_FAILURE_REASON = (
    "drive session died without landing the work (2/2 attempts) — giving up "
    "— no assignment was ever created for this run (#2273): likely an "
    "infrastructure/dispatch-layer failure, not a code defect"
)


def test_detect_unreachable_waits_excludes_an_unsatisfiable_after_block():
    """#2756 owns this shape (`_reconcile_blocked_after` self-heals it the
    moment the named pre-req clears) — it must never also be a #2944
    target, no matter how many deferrals it has accumulated."""
    dep_key = entry_key(REPO, 161)
    dependent = entry(
        162,
        state=STATE_BLOCKED,
        attempts=0,
        deferrals=40,
        after=(dep_key,),
        last_reason=f"pre-req {dep_key} is queued but blocked — it will never satisfy",
    )
    assert detect_unreachable_waits([dependent]) == []


def test_detect_unreachable_waits_flags_an_exhausted_dispatch_layer_root():
    """#2978: an entry that exhausted its attempts without #2273's dispatch
    layer ever producing a board-visible assignment IS the #2944 shape —
    `attempts > 0` alone must no longer exclude it."""
    root = entry(
        161,
        state=STATE_BLOCKED,
        attempts=2,
        deferrals=1,
        last_reason=_DISPATCH_FAILURE_REASON,
    )
    waits = detect_unreachable_waits([root])
    assert [w.key for w in waits] == [entry_key(REPO, 161)]


def test_detect_unreachable_waits_still_ignores_a_real_dispatched_attempt():
    """An ordinary `blocked` entry with `attempts > 0` and a last_reason that
    does NOT carry #2273's dispatch-failure marker is still excluded — it
    was dispatched, has (or had) a real branch/PR, and is a legitimate wait,
    not an unreachable one."""
    real_attempt = entry(
        4, state=STATE_BLOCKED, attempts=1, deferrals=500, last_reason="CI red"
    )
    assert detect_unreachable_waits([real_attempt]) == []


def test_detect_unreachable_waits_reproduces_the_ms5_incident_shape():
    """The exact claude-coordinator#2978 incident: one root `blocked
    attempts=2` with a #2273 dispatch-layer death, and eight dependents
    chained behind it via `after=`, each blocked with the frozen
    unsatisfiable-pre-req caption. The alert must name the root only — none
    of the eight dependents, and the root's `dependents` count must be 8 so
    the rendered alert can tell the operator they self-heal."""
    root_key = entry_key(REPO, 161)
    root = entry(
        161,
        state=STATE_BLOCKED,
        attempts=2,
        deferrals=1,
        last_reason=_DISPATCH_FAILURE_REASON,
    )
    dependents = [
        entry(
            issue,
            state=STATE_BLOCKED,
            attempts=0,
            deferrals=40,
            after=(root_key,),
            last_reason=f"pre-req {root_key} is queued but blocked — it will never satisfy",
        )
        for issue in range(162, 170)
    ]
    assert len(dependents) == 8

    waits = detect_unreachable_waits([root, *dependents])
    assert [w.key for w in waits] == [root_key]
    assert waits[0].dependents == 8

    alert = unreachable_wait_alert(waits)
    assert root_key in alert.reason
    for dep in dependents:
        assert dep.key not in alert.reason
    assert "8 dependent entries" in alert.reason
    assert "self-heal" in alert.reason


# ── #3016: the escalation-time remedy for a merge-gate block must be the ──
# gate-specific fix, never the blanket `drive-queue remove && add` requeue —
# a requeue discards a completed Work/Test/Review cycle instead of fixing
# the one gate actually stuck. `merge_gate_remedy_command` is the mapping;
# `is_merge_gate_block_reason` (already exercised elsewhere via #2424) is
# what a caller gates on before reaching for it.


def test_merge_gate_remedy_command_is_the_2983_regression_fixture():
    """The exact claude-coordinator#2983 shape (2026-08-31): a `merge
    attempted N times without landing` death whose OWN reason already names
    `coord merge --revalidate` as the fix must map to a scoped revalidate
    command, never a requeue."""
    reason = (
        "drive exited (exit_code=1): merge attempted 3 times without "
        "landing.\n   Last board state: status='PENDING' reason='CI stale: "
        "checks predate the current base (...); auto-rerun budget exhausted "
        "(2/2) — re-run CI (`coord merge --revalidate`) before merging'"
    )
    assert is_merge_gate_block_reason(reason) is True
    command = merge_gate_remedy_command(reason, REPO, 2983)
    assert command == f"coord merge --revalidate --only {REPO}#2983"
    assert "drive-queue remove" not in command
    assert "drive-queue add" not in command


def test_merge_gate_remedy_command_handles_a_stale_smoke_verdict():
    """The #1479 staleness race — `is_stale_smoke_reason` matches even
    without the `CI stale:` prefix at all — same remedy, same reasoning."""
    reason = "smoke test verdict is stale: recorded against base abc123, base is now def456"
    assert is_merge_gate_block_reason(reason) is True
    command = merge_gate_remedy_command(reason, REPO, 1650)
    assert command == f"coord merge --revalidate --only {REPO}#1650"


def test_merge_gate_remedy_command_falls_back_to_inspect_for_red_ci():
    """`checks failed` (red CI) has no safe one-line auto-fix — the correct
    remedy is "fix the named check", which is not a command this can hand
    an operator to run blind. Falls back to the read-only inspect command
    rather than a requeue or a guess."""
    reason = "checks failed: build (exit 1)"
    assert is_merge_gate_block_reason(reason) is True
    command = merge_gate_remedy_command(reason, REPO, 1650)
    assert command == merge_plan_inspect_command(REPO)
    assert "revalidate" not in command
    assert "drive-queue remove" not in command


def test_merge_gate_remedy_command_falls_back_to_inspect_for_review_required():
    reason = (
        "review_required — coord merge's own gate reports 'review missing', "
        "but this driver's OWN view already shows review_verdict='approved'"
    )
    assert is_merge_gate_block_reason(reason) is True
    assert merge_gate_remedy_command(reason, REPO, 1650) == merge_plan_inspect_command(REPO)


def test_merge_gate_remedy_command_falls_back_to_inspect_for_smoke_required():
    reason = "smoke_required — coord merge's own gate reports 'no verdict'"
    assert is_merge_gate_block_reason(reason) is True
    assert merge_gate_remedy_command(reason, REPO, 1650) == merge_plan_inspect_command(REPO)


def test_merge_gate_remedy_command_falls_back_to_inspect_for_opaque_merge_status():
    reason = "merge_status=NEEDS_ATTENTION — no number of retries changes this"
    assert is_merge_gate_block_reason(reason) is True
    assert merge_gate_remedy_command(reason, REPO, 1650) == merge_plan_inspect_command(REPO)


def test_merge_gate_remedy_command_is_the_safe_inspect_fallback_for_none_and_non_block_reasons():
    """Never raises — called on a reason that isn't a merge-gate block at
    all (or is missing entirely), it still returns the safe, read-only
    fallback rather than asserting a precondition on its caller."""
    assert merge_gate_remedy_command(None, REPO, 1650) == merge_plan_inspect_command(REPO)
    ordinary = "no candidate machine available for claude-coordinator#1650"
    assert is_merge_gate_block_reason(ordinary) is False
    assert merge_gate_remedy_command(ordinary, REPO, 1650) == merge_plan_inspect_command(REPO)
