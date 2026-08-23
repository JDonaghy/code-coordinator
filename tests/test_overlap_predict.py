"""Unit tests for :mod:`coord.overlap_predict` — #2247's queue-ordering predictor.

The decision half only: parsing a declared file block, intersecting it with
in-flight footprints, and scoring the resulting claim. The CLI half (an
enqueue that actually chains `--after`) is black-box tested in
``tests/test_cli_drive_queue.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from coord.overlap_predict import (
    FANOUT_WARN_THRESHOLD,
    OUTCOME_CONFIRMED,
    OUTCOME_FALSE_POSITIVE,
    OUTCOME_UNKNOWN,
    SOURCE_BRANCH,
    SOURCE_DECLARED,
    Footprint,
    Overlap,
    Prediction,
    classify_outcome,
    collect_candidate_files,
    declared_footprints,
    fanout_warnings,
    inflight_assignments,
    inflight_footprints,
    parse_declared_files,
    paths_overlap,
    predict_overlap,
    predictions_from_audit,
    tally,
)

REPO = "claude-coordinator"
GITHUB = "john/claude-coordinator"


# ── parse_declared_files ─────────────────────────────────────────────────────


def test_parses_a_markdown_files_heading_with_bullets():
    body = (
        "Some intro.\n\n"
        "## Files\n"
        "- `coord/drive_queue.py` — the queue\n"
        "- coord/state.py\n"
        "* tests/test_drive_queue.py\n\n"
        "## Acceptance\n"
        "- something else entirely\n"
    )
    assert parse_declared_files(body) == [
        "coord/drive_queue.py",
        "coord/state.py",
        "tests/test_drive_queue.py",
    ]


def test_parses_a_fenced_block_under_the_heading():
    body = "### Files touched\n```\ncoord/a.py\ncoord/b.py\n```\n"
    assert parse_declared_files(body) == ["coord/a.py", "coord/b.py"]


def test_parses_an_inline_files_line():
    assert parse_declared_files("files: a/b.py, c.py") == ["a/b.py", "c.py"]


def test_a_prose_mention_is_not_a_declaration():
    body = "This will probably touch coord/drive_queue.py, among other things."
    assert parse_declared_files(body) == []


def test_a_files_section_does_not_swallow_the_paragraph_after_it():
    body = "## Files\n- coord/a.py\n\nThis paragraph explains why, at length.\n"
    assert parse_declared_files(body) == ["coord/a.py"]


def test_issue_references_and_urls_are_not_paths():
    body = "## Files\n- #2247\n"
    assert parse_declared_files(body) == []
    assert parse_declared_files("files: https://example.com/x.py") == []


def test_a_missing_or_malformed_body_yields_no_prediction():
    assert parse_declared_files(None) == []
    assert parse_declared_files("") == []
    assert parse_declared_files("## Files\n") == []


def test_duplicate_declarations_collapse_in_declaration_order():
    body = "## Files\n- coord/a.py\n- coord/a.py\n- coord/b.py\n"
    assert parse_declared_files(body) == ["coord/a.py", "coord/b.py"]


# ── overlap ──────────────────────────────────────────────────────────────────


def test_paths_overlap_is_exact_plus_declared_directories():
    assert paths_overlap("coord/a.py", "coord/a.py")
    assert not paths_overlap("coord/a.py", "coord/b.py")
    assert paths_overlap("coord/dashboard/", "coord/dashboard/app.py")
    assert paths_overlap("coord/dashboard/app.py", "coord/dashboard/")
    assert not paths_overlap("coord/a.py", "")


def test_predicts_an_overlap_against_a_live_branch_footprint():
    footprint = Footprint(
        key=f"{REPO}#2230",
        issue_number=2230,
        files=("coord/drive_queue.py", "tests/test_drive_queue.py"),
        source=SOURCE_BRANCH,
        branch="issue-2230",
    )
    prediction = predict_overlap(["coord/drive_queue.py"], [footprint])
    assert prediction
    assert prediction.after_keys == (f"{REPO}#2230",)
    assert prediction.overlaps[0].files == ("coord/drive_queue.py",)
    assert "coord/drive_queue.py" in prediction.reason
    assert f"{REPO}#2230" in prediction.reason


def test_no_intersection_predicts_nothing():
    footprint = Footprint(
        key=f"{REPO}#1", issue_number=1, files=("coord/other.py",), source=SOURCE_BRANCH,
    )
    assert not predict_overlap(["coord/drive_queue.py"], [footprint])


def test_an_empty_candidate_list_is_no_prediction_at_all():
    footprint = Footprint(
        key=f"{REPO}#1", issue_number=1, files=("coord/a.py",), source=SOURCE_BRANCH,
    )
    prediction = predict_overlap([], [footprint])
    assert not prediction
    assert prediction.predicted_files == ()


def test_excluded_keys_never_become_pre_reqs():
    footprint = Footprint(
        key=f"{REPO}#7", issue_number=7, files=("coord/a.py",), source=SOURCE_BRANCH,
    )
    prediction = predict_overlap(
        ["coord/a.py"], [footprint], exclude_keys={f"{REPO}#7"}
    )
    assert not prediction


# ── #2603: an edge's PROVENANCE, not just its conclusion ────────────────────


def test_describe_shows_the_compared_branch_and_head_sha():
    footprint = Footprint(
        key=f"{REPO}#9",
        issue_number=9,
        files=("coord/a.py",),
        source=SOURCE_BRANCH,
        branch="issue-9",
        head_sha="a1b2c3d4e5f6",
    )
    prediction = predict_overlap(["coord/a.py"], [footprint])
    described = prediction.overlaps[0].describe()
    # The bare `[branch]` tag stays verbatim (existing callers match it as a
    # whole word) — the sha is additive detail appended after it.
    assert "[branch]" in described
    assert "issue-9" in described
    assert "a1b2c3d" in described  # first 7 chars of the sha, not the full 12
    assert "a1b2c3d4e5f6" not in described


def test_describe_omits_the_sha_when_it_could_not_be_fetched():
    footprint = Footprint(
        key=f"{REPO}#9", issue_number=9, files=("coord/a.py",),
        source=SOURCE_BRANCH, branch="issue-9",
    )
    prediction = predict_overlap(["coord/a.py"], [footprint])
    described = prediction.overlaps[0].describe()
    assert "issue-9" in described
    assert "@" not in described


def test_describe_flags_an_unconfirmed_liveness_check():
    # #2602's terminal check raised rather than confirming the branch was
    # still open — the footprint survived (fail-open), but describe() must
    # say so rather than reading identically to a confirmed-live edge.
    footprint = Footprint(
        key=f"{REPO}#9", issue_number=9, files=("coord/a.py",),
        source=SOURCE_BRANCH, branch="issue-9", liveness_checked=False,
    )
    prediction = predict_overlap(["coord/a.py"], [footprint])
    assert "liveness check failed" in prediction.overlaps[0].describe()


def test_describe_says_nothing_extra_when_liveness_was_confirmed():
    footprint = Footprint(
        key=f"{REPO}#9", issue_number=9, files=("coord/a.py",),
        source=SOURCE_BRANCH, branch="issue-9", liveness_checked=True,
    )
    prediction = predict_overlap(["coord/a.py"], [footprint])
    assert "liveness" not in prediction.overlaps[0].describe()


def test_declared_source_describe_has_no_branch_detail():
    # No wall clock in this module (see the module docstring) — a `declared`
    # overlap's cache-age note is the CLI layer's job, not describe()'s.
    footprint = Footprint(
        key=f"{REPO}#9", issue_number=9, files=("coord/a.py",),
        source=SOURCE_DECLARED, synced_at=123.0,
    )
    prediction = predict_overlap(["coord/a.py"], [footprint])
    described = prediction.overlaps[0].describe()
    assert described == f"{REPO}#9 [declared]: `coord/a.py`"


def test_reason_includes_the_candidates_own_declared_files():
    footprint = Footprint(
        key=f"{REPO}#7", issue_number=7, files=("coord/a.py",), source=SOURCE_BRANCH,
        branch="issue-7",
    )
    prediction = predict_overlap(["coord/a.py", "coord/b.py"], [footprint])
    # `coord/b.py` didn't collide with anything, but it is still part of
    # what THIS entry declared, and an operator reading the reason should be
    # able to see the full claim, not just the half that matched.
    assert "coord/a.py" in prediction.reason
    assert "coord/b.py" in prediction.reason


# ── fanout_warnings (#2601) ──────────────────────────────────────────────────


def _footprints_declaring_specific_files(count: int) -> list[Footprint]:
    return [
        Footprint(
            key=f"{REPO}#{100 + i}",
            issue_number=100 + i,
            files=(f"tests/test_{i}.py",),
            source=SOURCE_DECLARED,
        )
        for i in range(count)
    ]


def test_a_bare_directory_declaration_matching_many_entries_warns():
    footprints = _footprints_declaring_specific_files(FANOUT_WARN_THRESHOLD + 1)
    prediction = predict_overlap(["tests/"], footprints)
    assert len(prediction.overlaps) == FANOUT_WARN_THRESHOLD + 1

    warnings = fanout_warnings(prediction)
    assert len(warnings) == 1
    assert "`tests/`" in warnings[0]
    assert str(FANOUT_WARN_THRESHOLD + 1) in warnings[0]
    # The order is NOT touched by the warning — every edge is still applied.
    assert len(prediction.after_keys) == FANOUT_WARN_THRESHOLD + 1


def test_a_directory_declaration_at_or_under_the_threshold_does_not_warn():
    footprints = _footprints_declaring_specific_files(FANOUT_WARN_THRESHOLD)
    prediction = predict_overlap(["tests/"], footprints)
    assert fanout_warnings(prediction) == []


def test_a_specific_file_match_never_warns_regardless_of_count():
    footprints = [
        Footprint(
            key=f"{REPO}#{200 + i}",
            issue_number=200 + i,
            files=("coord/drive_queue.py",),
            source=SOURCE_DECLARED,
        )
        for i in range(FANOUT_WARN_THRESHOLD + 5)
    ]
    prediction = predict_overlap(["coord/drive_queue.py"], footprints)
    assert fanout_warnings(prediction) == []


def test_audit_details_carry_both_sides_of_the_claim():
    prediction = Prediction(
        predicted_files=("coord/a.py",),
        overlaps=(
            Overlap(key=f"{REPO}#7", source=SOURCE_BRANCH, files=("coord/a.py",), branch="b"),
        ),
    )
    details = prediction.audit_details()
    assert details["predicted_files"] == ["coord/a.py"]
    assert details["after"] == [f"{REPO}#7"]
    assert details["overlaps"][0]["files"] == ["coord/a.py"]
    assert details["overlaps"][0]["source"] == SOURCE_BRANCH


def test_audit_details_carry_provenance_for_later_readers(): # #2603
    prediction = Prediction(
        predicted_files=("coord/a.py",),
        overlaps=(
            Overlap(
                key=f"{REPO}#7", source=SOURCE_BRANCH, files=("coord/a.py",),
                branch="issue-7", head_sha="abc123", liveness_checked=False,
            ),
        ),
    )
    overlap_details = prediction.audit_details()["overlaps"][0]
    assert overlap_details["head_sha"] == "abc123"
    assert overlap_details["liveness_checked"] is False
    assert overlap_details["branch"] == "issue-7"


# ── gathering footprints ─────────────────────────────────────────────────────


def _assignment(**kw):
    base = {
        "type": "work",
        "status": "running",
        "repo_name": REPO,
        "branch": "issue-1",
        "issue_number": 1,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_inflight_includes_finished_but_unmerged_work():
    board = SimpleNamespace(
        active=[
            _assignment(issue_number=1, status="running", branch="issue-1"),
            # Worker done, PR still open — the exact case #1720's running-only
            # fence misses and both 2026-08-14 collisions actually hit.
            _assignment(issue_number=2, status="done", branch="issue-2"),
            _assignment(issue_number=3, status="merged", branch="issue-3"),
            _assignment(issue_number=4, type="review", branch="issue-4"),
            _assignment(issue_number=5, branch=""),
            _assignment(issue_number=6, repo_name="quadraui", branch="issue-6"),
        ],
    )
    numbers = {a.issue_number for a in inflight_assignments(board, REPO)}
    assert numbers == {1, 2}


def test_inflight_scans_the_completed_bucket_where_open_prs_actually_live():
    # `board.active` is running/pending ONLY (`_board_mapping._ACTIVE_STATUSES`),
    # so a finished worker whose PR is still open is in `completed`. Missing
    # that bucket would mean missing every collision this feature exists for.
    board = SimpleNamespace(
        active=[],
        completed=[
            _assignment(issue_number=2, status="done", branch="issue-2"),
            _assignment(issue_number=3, status="merged", branch="issue-3"),
            _assignment(issue_number=4, status="failed", branch="issue-4"),
        ],
    )
    assert [a.issue_number for a in inflight_assignments(board, REPO)] == [2]


def test_inflight_excludes_the_issue_being_queued():
    board = SimpleNamespace(active=[_assignment(issue_number=9, branch="issue-9")])
    assert inflight_assignments(board, REPO, exclude_issue_number=9) == []


def test_inflight_footprints_read_the_real_diff():
    board = SimpleNamespace(active=[_assignment(issue_number=9, branch="issue-9")])
    prints = inflight_footprints(
        REPO, GITHUB, "main",
        board=board,
        diff_files_fetcher=lambda repo, base, head: ["coord/drive_queue.py"],
    )
    assert [(f.key, f.files, f.source) for f in prints] == [
        (f"{REPO}#9", ("coord/drive_queue.py",), SOURCE_BRANCH)
    ]


def test_inflight_footprints_record_the_compared_head_sha():
    board = SimpleNamespace(active=[_assignment(issue_number=9, branch="issue-9")])
    prints = inflight_footprints(
        REPO, GITHUB, "main",
        board=board,
        diff_files_fetcher=lambda repo, base, head: ["coord/drive_queue.py"],
        head_sha_fetcher=lambda repo, branch: f"sha-for-{branch}",
    )
    assert [f.head_sha for f in prints] == ["sha-for-issue-9"]
    assert prints[0].liveness_checked is True


def test_a_failed_head_sha_fetch_leaves_the_footprint_intact():
    # The sha is detail on top of a real diff, not a precondition for
    # trusting it — a failure here must not drop the footprint.
    board = SimpleNamespace(active=[_assignment(issue_number=9, branch="issue-9")])

    def exploding_sha(repo, branch):
        raise RuntimeError("gh exploded")

    prints = inflight_footprints(
        REPO, GITHUB, "main",
        board=board,
        diff_files_fetcher=lambda repo, base, head: ["coord/drive_queue.py"],
        head_sha_fetcher=exploding_sha,
    )
    assert [f.key for f in prints] == [f"{REPO}#9"]
    assert prints[0].head_sha == ""


def test_a_terminal_checker_that_raises_marks_liveness_unchecked():
    # #2602 fails this open (still-in-flight) — #2603 records that the
    # openness was ASSUMED, not confirmed, so a reader downstream can tell
    # the two apart.
    board = SimpleNamespace(active=[_assignment(issue_number=9, branch="issue-9")])

    def exploding_checker(repo, issue, branch):
        raise RuntimeError("gh exploded")

    prints = inflight_footprints(
        REPO, GITHUB, "main",
        board=board,
        diff_files_fetcher=lambda repo, base, head: ["coord/a.py"],
        terminal_checker=exploding_checker,
    )
    assert [f.key for f in prints] == [f"{REPO}#9"]
    assert prints[0].liveness_checked is False


def test_a_successful_terminal_checker_marks_liveness_checked():
    board = SimpleNamespace(active=[_assignment(issue_number=9, branch="issue-9")])
    prints = inflight_footprints(
        REPO, GITHUB, "main",
        board=board,
        diff_files_fetcher=lambda repo, base, head: ["coord/a.py"],
        terminal_checker=lambda repo, issue, branch: False,
    )
    assert [f.key for f in prints] == [f"{REPO}#9"]
    assert prints[0].liveness_checked is True


def test_one_undiffable_branch_is_skipped_not_fatal():
    board = SimpleNamespace(
        active=[
            _assignment(issue_number=1, branch="issue-1"),
            _assignment(issue_number=2, branch="issue-2"),
        ],
    )

    def fetcher(repo, base, head):
        if head == "issue-1":
            raise RuntimeError("gh exploded")
        return ["coord/b.py"]

    prints = inflight_footprints(REPO, GITHUB, "main", board=board, diff_files_fetcher=fetcher)
    assert [f.key for f in prints] == [f"{REPO}#2"]


def test_an_unreadable_board_yields_no_footprints_rather_than_raising():
    class Exploding:
        @property
        def active(self):
            raise RuntimeError("board unreachable")

    assert inflight_footprints(REPO, GITHUB, "main", board=Exploding()) == []


# ── #2602: a landed branch is not a footprint candidate ─────────────────────
#
# `status == "done"` is deliberately still IN `inflight_assignments`'s output
# (that's the finished-but-unmerged case #2247 exists to catch) — but by the
# time `inflight_footprints` is about to trust that branch's diff, it must
# check whether the branch has ACTUALLY landed on GitHub already (a squash
# merge closes the issue/merges the PR well before the assignment's own
# `status` field catches up). A landed branch's diff is real but permanently
# stale, so it must never become a footprint.


def test_a_landed_done_status_branch_is_excluded_from_footprints():
    board = SimpleNamespace(
        active=[
            _assignment(issue_number=1, status="running", branch="issue-1"),
            # #144 in the incident: status still "done", but already merged
            # on GitHub — the reconcile flip to "merged" hasn't landed yet.
            _assignment(issue_number=2, status="done", branch="issue-2"),
        ],
    )
    fetched: list[str] = []

    def fetcher(repo, base, head):
        fetched.append(head)
        return ["coord/whatever.py"]

    prints = inflight_footprints(
        REPO, GITHUB, "main",
        board=board,
        diff_files_fetcher=fetcher,
        terminal_checker=lambda repo, issue, branch: issue == 2,
    )
    assert [f.key for f in prints] == [f"{REPO}#1"]
    # The landed branch's diff is never even fetched — no wasted compare call
    # and no chance its stale file list leaks into a footprint some other way.
    assert fetched == ["issue-1"]


def test_a_finished_but_unmerged_branch_still_becomes_a_footprint():
    board = SimpleNamespace(
        active=[_assignment(issue_number=2, status="done", branch="issue-2")],
    )
    prints = inflight_footprints(
        REPO, GITHUB, "main",
        board=board,
        diff_files_fetcher=lambda repo, base, head: ["coord/a.py"],
        terminal_checker=lambda repo, issue, branch: False,
    )
    assert [f.key for f in prints] == [f"{REPO}#2"]


def test_the_default_terminal_checker_is_github_ops_work_is_terminal(monkeypatch):
    calls: list[tuple] = []

    def fake_work_is_terminal(repo_github, issue_number, branch, **kwargs):
        calls.append((repo_github, issue_number, branch))
        return issue_number == 2

    monkeypatch.setattr(
        "coord.github_ops.work_is_terminal", fake_work_is_terminal
    )
    board = SimpleNamespace(
        active=[
            _assignment(issue_number=1, status="running", branch="issue-1"),
            _assignment(issue_number=2, status="done", branch="issue-2"),
        ],
    )
    prints = inflight_footprints(
        REPO, GITHUB, "main",
        board=board,
        diff_files_fetcher=lambda repo, base, head: ["coord/a.py"],
    )
    assert [f.key for f in prints] == [f"{REPO}#1"]
    assert (GITHUB, 2, "issue-2") in calls


def test_an_erroring_terminal_checker_fails_open_to_still_in_flight():
    board = SimpleNamespace(
        active=[_assignment(issue_number=2, status="done", branch="issue-2")],
    )

    def exploding_checker(repo, issue, branch):
        raise RuntimeError("gh exploded")

    prints = inflight_footprints(
        REPO, GITHUB, "main",
        board=board,
        diff_files_fetcher=lambda repo, base, head: ["coord/a.py"],
        terminal_checker=exploding_checker,
    )
    assert [f.key for f in prints] == [f"{REPO}#2"]


def test_declared_footprints_only_cover_issues_that_declared_something():
    bodies = {1: "## Files\n- coord/a.py\n", 2: "no declaration here"}
    prints = declared_footprints(
        [(REPO, 1), (REPO, 2)], lambda repo, number: bodies.get(number)
    )
    assert [(f.key, f.files, f.source) for f in prints] == [
        (f"{REPO}#1", ("coord/a.py",), SOURCE_DECLARED)
    ]


def test_declared_footprints_stamp_synced_at_via_the_fetcher(): # #2603
    bodies = {1: "## Files\n- coord/a.py\n"}
    prints = declared_footprints(
        [(REPO, 1)],
        lambda repo, number: bodies.get(number),
        synced_at_fetcher=lambda repo, number: 1000.0,
    )
    assert prints[0].synced_at == 1000.0


def test_declared_footprints_default_to_no_synced_at():
    # The default (`None`) leaves every footprint's `synced_at` unset —
    # identical to pre-#2603 behaviour when a caller doesn't wire one up.
    bodies = {1: "## Files\n- coord/a.py\n"}
    prints = declared_footprints([(REPO, 1)], lambda repo, number: bodies.get(number))
    assert prints[0].synced_at is None


def test_a_raising_synced_at_fetcher_still_yields_the_footprint():
    bodies = {1: "## Files\n- coord/a.py\n"}

    def exploding_fetcher(repo, number):
        raise RuntimeError("db unreachable")

    prints = declared_footprints(
        [(REPO, 1)],
        lambda repo, number: bodies.get(number),
        synced_at_fetcher=exploding_fetcher,
    )
    assert prints[0].key == f"{REPO}#1"
    assert prints[0].synced_at is None


def test_collect_candidate_files_prefers_the_declaration_over_extra_sources():
    files = collect_candidate_files(
        REPO, 1,
        lambda repo, number: "## Files\n- coord/declared.py\n",
        extra_sources=[lambda repo, number: ["coord/guessed.py"]],
    )
    assert files == ["coord/declared.py"]


def test_collect_candidate_files_falls_back_to_an_extra_source():
    files = collect_candidate_files(
        REPO, 1,
        lambda repo, number: "nothing declared",
        extra_sources=[lambda repo, number: ["`coord/guessed.py`"]],
    )
    assert files == ["coord/guessed.py"]


def test_collect_candidate_files_fails_open_when_the_body_cannot_be_read():
    def explode(repo, number):
        raise RuntimeError("no body")

    assert collect_candidate_files(REPO, 1, explode) == []


# ── measuring the predictor ──────────────────────────────────────────────────


def test_a_prediction_borne_out_by_the_real_diffs_is_confirmed():
    assert classify_outcome(
        ["coord/a.py"], ["coord/a.py", "coord/z.py"], ["coord/a.py"],
    ) == OUTCOME_CONFIRMED


def test_a_prediction_the_diffs_contradict_is_a_false_positive():
    assert classify_outcome(
        ["coord/a.py"], ["coord/x.py"], ["coord/y.py"],
    ) == OUTCOME_FALSE_POSITIVE


def test_an_unreadable_diff_is_unknown_never_a_false_positive():
    assert classify_outcome(["coord/a.py"], None, ["coord/a.py"]) == OUTCOME_UNKNOWN
    assert classify_outcome(["coord/a.py"], ["coord/a.py"], None) == OUTCOME_UNKNOWN


def test_precision_counts_only_scored_predictions():
    accuracy = tally([
        OUTCOME_CONFIRMED, OUTCOME_CONFIRMED, OUTCOME_FALSE_POSITIVE, OUTCOME_UNKNOWN,
    ])
    assert (accuracy.confirmed, accuracy.false_positive, accuracy.unknown) == (2, 1, 1)
    assert accuracy.precision == 2 / 3
    assert "false-positive" in accuracy.render()


def test_precision_is_unknown_before_anything_is_scoreable():
    accuracy = tally([OUTCOME_UNKNOWN])
    assert accuracy.precision is None
    assert "precision unknown" in accuracy.render()


def test_audit_rows_flatten_to_one_record_per_predicted_overlap():
    rows = predictions_from_audit([
        {
            "ts": 1.0,
            "repo": REPO,
            "issue": 2247,
            "details": {
                "overlaps": [
                    {"key": f"{REPO}#1", "source": SOURCE_BRANCH, "files": ["coord/a.py"]},
                    {"key": f"{REPO}#2", "source": SOURCE_DECLARED, "files": ["coord/b.py"]},
                ]
            },
        },
        {"ts": 2.0, "repo": REPO, "issue": None, "details": {}},
    ])
    assert [(r["key"], r["other_key"]) for r in rows] == [
        (f"{REPO}#2247", f"{REPO}#1"),
        (f"{REPO}#2247", f"{REPO}#2"),
    ]


def test_audit_rows_carry_provenance_through_to_the_flattened_record(): # #2603
    rows = predictions_from_audit([
        {
            "ts": 1.0,
            "repo": REPO,
            "issue": 2247,
            "details": {
                "overlaps": [
                    {
                        "key": f"{REPO}#1",
                        "source": SOURCE_BRANCH,
                        "branch": "issue-1",
                        "head_sha": "abc123",
                        "liveness_checked": False,
                        "files": ["coord/a.py"],
                    },
                ]
            },
        },
    ])
    assert rows[0]["branch"] == "issue-1"
    assert rows[0]["head_sha"] == "abc123"
    assert rows[0]["liveness_checked"] is False


def test_audit_rows_default_provenance_for_a_pre_2603_row():
    # A row recorded before #2603 shipped has no provenance keys at all —
    # flattening must not raise, and must default `liveness_checked` to
    # `True` (the pre-#2603 implicit assumption) rather than `False`.
    rows = predictions_from_audit([
        {
            "ts": 1.0,
            "repo": REPO,
            "issue": 2247,
            "details": {
                "overlaps": [
                    {"key": f"{REPO}#1", "source": SOURCE_BRANCH, "files": ["coord/a.py"]},
                ]
            },
        },
    ])
    assert rows[0]["branch"] == ""
    assert rows[0]["head_sha"] == ""
    assert rows[0]["synced_at"] is None
    assert rows[0]["liveness_checked"] is True
