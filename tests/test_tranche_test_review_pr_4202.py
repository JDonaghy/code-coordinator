"""Tests for the native tranche row's capability-routed Test leg, three-way
bound review, and single closing PR (#4129 Phase 1, this slice #4202):

- `coord.dispatch.tranche_test_files_likely` / `tranche_test_machine_name` /
  `dispatch_tranche_test` — the ONE Test leg run against the integrated
  tree, routed by the same `coord.smoke.required_capabilities` primitive a
  `type="work"` dispatch reroutes through.
- `coord.review.build_tranche_review_briefing` / `dispatch_tranche_review_
  legs` / `tranche_review_leg_assignments` / `tranche_review_verdict` /
  `tranche_integrated_patch_id` — the three bound review legs (recipe/
  behaviour/gates), their aggregation into one approval decision, and the
  patch-id binding that makes a scoped re-review of a fix delta mandatory.
- `coord.merge_queue.tranche_closing_pr_body` / `open_tranche_closing_pr` —
  the single PR that closes every done child issue.

#4199 (row shape), #4200 (parallel child dispatch) and #4201 (coord-side
merge/integrator) are the sibling slices this builds on.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from coord.config import Config, SmokeRule, SmokeTestsConfig
from coord.dispatch import (
    dispatch_tranche_test,
    tranche_test_files_likely,
    tranche_test_leg_assignment,
    tranche_test_machine_name,
)
from coord.drive import advance_tranche_row
from coord.drive_queue import (
    TRANCHE_ADVANCEABLE_STATES,
    tranche_rows_ready_to_advance,
)
from coord.merge_queue import open_tranche_closing_pr, tranche_closing_pr_body
from coord.models import Assignment, Board, Machine, Repo, TrancheChild, TrancheRow
from coord.review import (
    TRANCHE_REVIEW_FOCUSES,
    build_tranche_review_briefing,
    dispatch_tranche_review_legs,
    tranche_integrated_patch_id,
    tranche_review_leg_assignments,
    tranche_review_target_for,
    tranche_review_verdict,
)


def _row(**overrides) -> TrancheRow:
    defaults: dict = dict(
        id=7,
        repo_name="api",
        issue_number=100,
        issue_title="Decompose the big thing",
        base_sha="deadbeef",
        state="done",
        children=[
            TrancheChild(
                issue_number=101, repo_name="api", title="Child A", state="done",
                result={"outcome": "done", "summary": "ok", "files": ["src/a.py", "src/shared.py"]},
            ),
            TrancheChild(
                issue_number=102, repo_name="api", title="Child B", state="done",
                result={"outcome": "done", "summary": "ok", "files": ["src/b.py", "src/shared.py"]},
            ),
        ],
    )
    defaults.update(overrides)
    return TrancheRow(**defaults)


def _config(**overrides) -> Config:
    defaults: dict = dict(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(name="m1", host="m1.tail", repos=["api"], repo_paths={"api": "/repo/api"}),
            Machine(name="m2", host="m2.tail", repos=["api"], repo_paths={"api": "/repo/api"}),
        ],
    )
    defaults.update(overrides)
    return Config(**defaults)


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload if payload is not None else {"id": "a-1"}
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, timeout: float) -> _FakeHTTPResponse:
        self.calls.append((url, json))
        return _FakeHTTPResponse(self._payload)


class _RejectingHTTPClient:
    def post(self, url: str, *, json: dict, timeout: float):
        import httpx

        raise httpx.ConnectError("connection refused")


# ── Test leg: capability routing ────────────────────────────────────────────


class TestTrancheTestFilesLikely:
    def test_unions_and_dedups_done_children_files(self) -> None:
        row = _row()
        assert tranche_test_files_likely(row) == ["src/a.py", "src/shared.py", "src/b.py"]

    def test_skips_children_with_no_result(self) -> None:
        row = _row(children=[TrancheChild(issue_number=101, repo_name="api")])
        assert tranche_test_files_likely(row) == []


class TestTrancheTestMachineName:
    def test_no_capability_needed_falls_back_to_first_machine(self) -> None:
        row = _row()
        config = _config()
        assert tranche_test_machine_name(row, config, ["m1", "m2"]) == "m1"

    def test_reroutes_to_the_capability_matched_machine(self) -> None:
        row = _row(children=[
            TrancheChild(
                issue_number=101, repo_name="api", state="done",
                result={"outcome": "done", "summary": "", "files": ["src/gtk/widget.c"]},
            ),
        ])
        config = _config(
            machines=[
                Machine(name="m1", host="m1.tail", repos=["api"], repo_paths={"api": "/r"}),
                Machine(
                    name="gtk-box", host="gtk.tail", repos=["api"],
                    repo_paths={"api": "/r"}, capabilities=["gtk"],
                ),
            ],
            smoke_tests=SmokeTestsConfig(
                capability_rules=[SmokeRule(files=["src/gtk/"], requires=["gtk"])],
            ),
        )
        assert tranche_test_machine_name(row, config, ["m1", "gtk-box"]) == "gtk-box"

    def test_raises_on_empty_machine_names(self) -> None:
        row = _row()
        config = _config()
        with pytest.raises(ValueError, match="machine_names is empty"):
            tranche_test_machine_name(row, config, [])

    def test_never_relaxes_when_no_pool_machine_is_capable(self) -> None:
        # A required capability no machine in the CALLER'S pool declares
        # must refuse (`None`), never silently fall back to machine_names[0]
        # the way `route_work_by_capability` does for an ordinary
        # `type="work"` dispatch.
        row = _row(children=[
            TrancheChild(
                issue_number=101, repo_name="api", state="done",
                result={"outcome": "done", "summary": "", "files": ["src/gtk/widget.c"]},
            ),
        ])
        config = _config(
            machines=[
                Machine(name="m1", host="m1.tail", repos=["api"], repo_paths={"api": "/r"}),
                Machine(
                    name="gtk-box", host="gtk.tail", repos=["api"],
                    repo_paths={"api": "/r"}, capabilities=["gtk"],
                ),
            ],
            smoke_tests=SmokeTestsConfig(
                capability_rules=[SmokeRule(files=["src/gtk/"], requires=["gtk"])],
            ),
        )
        # `gtk-box` IS capable, but it is excluded from the caller's pool —
        # must refuse, never silently pick "m1" (which cannot run gtk) nor
        # reach outside the given pool to find "gtk-box" on its own.
        assert tranche_test_machine_name(row, config, ["m1"]) is None


class TestDispatchTrancheTest:
    def test_happy_path_posts_smoke_type_assignment(self) -> None:
        row = _row()
        config = _config()
        board = Board()
        client = _FakeHTTPClient({"id": "test-1"})

        result = dispatch_tranche_test(
            row, "cafef00d", config, ["m1", "m2"], board, http_client=client, now=1000.0,
        )

        assert result == {"ok": True, "assignment_id": "test-1", "machine_name": "m1"}
        assert len(board.active) == 1
        assignment = board.active[0]
        assert assignment.type == "smoke"
        assert assignment.branch == "tranche-7-integrated"
        assert assignment.assignment_id == "test-1"
        assert len(client.calls) == 1
        _url, payload = client.calls[0]
        assert payload["type"] == "smoke"
        assert payload["branch"] == "tranche-7-integrated"
        assert "cafef00d" in payload["briefing"]
        assert "SMOKE: pass" in payload["briefing"]
        assert "SMOKE: fail" in payload["briefing"]

    def test_unreachable_agent_fails_closed_without_phantom_assignment(self) -> None:
        row = _row()
        config = _config()
        board = Board()

        result = dispatch_tranche_test(
            row, "cafef00d", config, ["m1"], board, http_client=_RejectingHTTPClient(),
        )

        assert result["ok"] is False
        assert "error" in result
        assert board.active == []

    def test_machine_with_no_repo_path_fails_closed(self) -> None:
        row = _row()
        config = _config(machines=[Machine(name="bare", host="bare.tail", repos=["api"])])
        board = Board()

        result = dispatch_tranche_test(row, "cafef00d", config, ["bare"], board)

        assert result["ok"] is False
        assert board.active == []

    def test_raises_on_empty_machine_names(self) -> None:
        row = _row()
        config = _config()
        board = Board()
        with pytest.raises(ValueError, match="machine_names is empty"):
            dispatch_tranche_test(row, "cafef00d", config, [], board)

    def test_no_capable_machine_in_pool_fails_closed(self) -> None:
        # Capability matching for the Test leg is never relaxed — a pool
        # with no capable machine must refuse rather than land the leg on
        # one that cannot verify it.
        row = _row(children=[
            TrancheChild(
                issue_number=101, repo_name="api", state="done",
                result={"outcome": "done", "summary": "", "files": ["src/gtk/widget.c"]},
            ),
        ])
        config = _config(
            machines=[Machine(name="m1", host="m1.tail", repos=["api"], repo_paths={"api": "/r"})],
            smoke_tests=SmokeTestsConfig(
                capability_rules=[SmokeRule(files=["src/gtk/"], requires=["gtk"])],
            ),
        )
        board = Board()

        result = dispatch_tranche_test(row, "cafef00d", config, ["m1"], board)

        assert result["ok"] is False
        assert board.active == []

    def test_agent_reported_failure_is_not_a_phantom_success(self) -> None:
        # A 202 whose body reports the agent's own worktree-setup failure
        # must not read as `ok=True` with a fabricated assignment id —
        # mirrors the sibling child/integrator dispatchers' fail-closed
        # read of exactly this shape.
        row = _row()
        config = _config()
        board = Board()
        client = _FakeHTTPClient({
            "id": "test-1", "status": "failed", "error": "worktree not writable",
        })

        result = dispatch_tranche_test(
            row, "cafef00d", config, ["m1"], board, http_client=client,
        )

        assert result["ok"] is False
        assert "worktree not writable" in result["error"]
        assert board.active == []

    def test_agent_response_with_no_id_is_not_a_phantom_success(self) -> None:
        row = _row()
        config = _config()
        board = Board()
        client = _FakeHTTPClient({})

        result = dispatch_tranche_test(
            row, "cafef00d", config, ["m1"], board, http_client=client,
        )

        assert result["ok"] is False
        assert board.active == []


# ── Three-way bound review ──────────────────────────────────────────────────


class TestTrancheReviewTargetFor:
    def test_non_numeric_sentinel_per_focus(self) -> None:
        for focus in TRANCHE_REVIEW_FOCUSES:
            target = tranche_review_target_for(7, focus)
            assert focus in target
            with pytest.raises(ValueError):
                int(target)


class TestBuildTrancheReviewBriefing:
    def test_names_the_focus_and_lists_done_children(self) -> None:
        row = _row()
        briefing = build_tranche_review_briefing(row, "recipe", "cafef00d")
        assert "`recipe` leg" in briefing
        assert "#101" in briefing
        assert "#102" in briefing
        assert "cafef00d" in briefing

    def test_rejects_unknown_focus(self) -> None:
        row = _row()
        with pytest.raises(ValueError, match="not one of"):
            build_tranche_review_briefing(row, "performance", "cafef00d")

    def test_prior_request_changes_adds_scoped_rereview_note(self) -> None:
        row = _row()
        briefing = build_tranche_review_briefing(
            row, "gates", "cafef00d", prior_verdict="request-changes",
        )
        assert "SCOPED RE-REVIEW" in briefing

    def test_prior_approve_adds_no_rereview_note(self) -> None:
        row = _row()
        briefing = build_tranche_review_briefing(
            row, "gates", "cafef00d", prior_verdict="approve",
        )
        assert "SCOPED RE-REVIEW" not in briefing


class TestDispatchTrancheReviewLegs:
    def test_happy_path_dispatches_all_three_legs_round_robin(self) -> None:
        row = _row()
        config = _config()
        board = Board()
        client = _FakeHTTPClient({"id": "rev-1"})

        result = dispatch_tranche_review_legs(
            row, "cafef00d", config, ["m1", "m2"], board, http_client=client, now=1000.0,
            integrated_patch_id="patch-xyz",
        )

        assert result["ok"] is True
        assert set(result["legs"]) == set(TRANCHE_REVIEW_FOCUSES)
        assert len(board.active) == 3
        assert {a.review_target for a in board.active} == {
            tranche_review_target_for(7, f) for f in TRANCHE_REVIEW_FOCUSES
        }
        machines_used = [a.machine_name for a in board.active]
        assert machines_used == ["m1", "m2", "m1"]  # round-robin over 2 machines, 3 legs
        for a in board.active:
            assert a.type == "review"
            # Every leg is bound to the integrated patch-id at dispatch
            # time, so `tranche_review_verdict`'s staleness check has
            # something real to compare against.
            assert a.review_head_sha == "cafef00d"
            assert a.review_patch_id == "patch-xyz"
        _url, payload = client.calls[0]
        from coord.review import REVIEWER_SYSTEM_PROMPT

        assert payload["system_prompt"] == REVIEWER_SYSTEM_PROMPT

    def test_computes_patch_id_itself_when_not_given(self) -> None:
        row = _row()
        config = _config()
        board = Board()
        client = _FakeHTTPClient({"id": "rev-1"})
        diff_fetcher = MagicMock(return_value="diff --git a/x b/x\n")
        patch_id_computer = MagicMock(return_value="computed-patch")

        dispatch_tranche_review_legs(
            row, "cafef00d", config, ["m1"], board, http_client=client,
            diff_fetcher=diff_fetcher, patch_id_computer=patch_id_computer,
        )

        assert all(a.review_patch_id == "computed-patch" for a in board.active)

    def test_one_leg_failing_does_not_block_the_other_two(self) -> None:
        row = _row()
        config = _config(machines=[
            Machine(name="m1", host="m1.tail", repos=["api"], repo_paths={"api": "/r"}),
            Machine(name="bare", host="bare.tail", repos=["api"]),  # no repo_path
        ])
        board = Board()
        client = _FakeHTTPClient({"id": "rev-1"})

        result = dispatch_tranche_review_legs(
            row, "cafef00d", config, ["m1", "bare"], board, http_client=client,
            diff_fetcher=MagicMock(return_value=None),
        )

        assert result["ok"] is False
        oks = [leg["ok"] for leg in result["legs"].values()]
        assert oks.count(True) == 2
        assert oks.count(False) == 1
        assert len(board.active) == 2

    def test_agent_reported_failure_is_not_a_phantom_success(self) -> None:
        # Mirrors the sibling child/integrator dispatchers' fail-closed
        # read of a `status: "failed"` 202 body — no fabricated assignment
        # id, no phantom board row.
        row = _row()
        config = _config()
        board = Board()
        client = _FakeHTTPClient({
            "id": "rev-1", "status": "failed", "error": "worktree not writable",
        })

        result = dispatch_tranche_review_legs(
            row, "cafef00d", config, ["m1"], board, http_client=client,
            diff_fetcher=MagicMock(return_value=None),
        )

        assert result["ok"] is False
        for leg in result["legs"].values():
            assert leg["ok"] is False
            assert "worktree not writable" in leg["error"]
        assert board.active == []

    def test_agent_response_with_no_id_is_not_a_phantom_success(self) -> None:
        row = _row()
        config = _config()
        board = Board()
        client = _FakeHTTPClient({})

        result = dispatch_tranche_review_legs(
            row, "cafef00d", config, ["m1"], board, http_client=client,
            diff_fetcher=MagicMock(return_value=None),
        )

        assert result["ok"] is False
        assert board.active == []

    def test_raises_on_empty_machine_names(self) -> None:
        row = _row()
        config = _config()
        board = Board()
        with pytest.raises(ValueError, match="machine_names is empty"):
            dispatch_tranche_review_legs(row, "cafef00d", config, [], board)

    def test_raises_when_row_id_is_none(self) -> None:
        row = _row(id=None)
        config = _config()
        board = Board()
        with pytest.raises(ValueError, match="row.id is None"):
            dispatch_tranche_review_legs(row, "cafef00d", config, ["m1"], board)

    def test_prior_verdicts_threaded_into_scoped_briefing(self) -> None:
        row = _row()
        config = _config()
        board = Board()
        client = _FakeHTTPClient({"id": "rev-1"})

        dispatch_tranche_review_legs(
            row, "cafef00d", config, ["m1", "m2"], board, http_client=client,
            prior_verdicts={"gates": "request-changes"},
            diff_fetcher=MagicMock(return_value=None),
        )

        gates_calls = [
            payload for _url, payload in client.calls
            if payload["review_target"] == tranche_review_target_for(7, "gates")
        ]
        assert len(gates_calls) == 1
        assert "SCOPED RE-REVIEW" in gates_calls[0]["briefing"]


def _review_leg(
    focus: str, *, row_id: int = 7, verdict: str | None, patch_id: str | None = None,
    dispatched_at: float = 0.0,
) -> Assignment:
    return Assignment(
        machine_name="m1", repo_name="api", issue_number=100,
        issue_title=f"[review:{focus}]", assignment_id=f"{focus}-{dispatched_at}",
        type="review", status="done", review_target=tranche_review_target_for(row_id, focus),
        review_verdict=verdict, review_patch_id=patch_id, dispatched_at=dispatched_at,
    )


class TestTrancheReviewLegAssignments:
    def test_picks_the_most_recently_dispatched_leg_per_focus(self) -> None:
        row = _row()
        board = Board()
        board.completed.append(_review_leg("recipe", verdict="request-changes", dispatched_at=1.0))
        board.active.append(_review_leg("recipe", verdict=None, dispatched_at=2.0))

        legs = tranche_review_leg_assignments(board, row)

        assert legs["recipe"].dispatched_at == 2.0
        assert legs["behaviour"] is None
        assert legs["gates"] is None

    def test_none_when_row_id_is_none(self) -> None:
        row = _row(id=None)
        board = Board()
        legs = tranche_review_leg_assignments(board, row)
        assert all(v is None for v in legs.values())


class TestTrancheReviewVerdict:
    def test_approved_only_when_all_three_approve(self) -> None:
        legs = {f: _review_leg(f, verdict="approve") for f in TRANCHE_REVIEW_FOCUSES}
        verdict = tranche_review_verdict(legs)
        assert verdict == {"approved": True, "pending": (), "blocking": ()}

    def test_missing_focus_is_never_vacuously_approved(self) -> None:
        legs = {"recipe": _review_leg("recipe", verdict="approve")}
        verdict = tranche_review_verdict(legs)
        assert verdict["approved"] is False
        assert "behaviour" in verdict["pending"]
        assert "gates" in verdict["pending"]

    def test_one_request_changes_blocks_the_whole_row(self) -> None:
        legs = {
            "recipe": _review_leg("recipe", verdict="approve"),
            "behaviour": _review_leg("behaviour", verdict="request-changes"),
            "gates": _review_leg("gates", verdict="approve"),
        }
        verdict = tranche_review_verdict(legs)
        assert verdict["approved"] is False
        assert verdict["blocking"] == ("behaviour",)

    def test_stale_patch_id_reads_as_pending_even_if_approved(self) -> None:
        legs = {
            f: _review_leg(f, verdict="approve", patch_id="old-patch")
            for f in TRANCHE_REVIEW_FOCUSES
        }
        verdict = tranche_review_verdict(legs, integrated_patch_id="new-patch")
        assert verdict["approved"] is False
        assert set(verdict["pending"]) == set(TRANCHE_REVIEW_FOCUSES)

    def test_matching_patch_id_counts_as_approved(self) -> None:
        legs = {
            f: _review_leg(f, verdict="approve", patch_id="same-patch")
            for f in TRANCHE_REVIEW_FOCUSES
        }
        verdict = tranche_review_verdict(legs, integrated_patch_id="same-patch")
        assert verdict["approved"] is True

    def test_patch_id_check_skipped_when_not_provided(self) -> None:
        legs = {
            f: _review_leg(f, verdict="approve", patch_id="whatever")
            for f in TRANCHE_REVIEW_FOCUSES
        }
        verdict = tranche_review_verdict(legs)
        assert verdict["approved"] is True

    def test_missing_patch_id_on_a_leg_is_pending_not_vacuously_bound(self) -> None:
        # A leg with no `review_patch_id` at all (the production shape
        # before dispatch stamped one) must read as `pending`, not fall
        # into the permissive "binding satisfied" branch just because
        # it's `None`.
        legs = {
            f: _review_leg(f, verdict="approve", patch_id=None)
            for f in TRANCHE_REVIEW_FOCUSES
        }
        verdict = tranche_review_verdict(legs, integrated_patch_id="new-patch")
        assert verdict["approved"] is False
        assert set(verdict["pending"]) == set(TRANCHE_REVIEW_FOCUSES)


class TestTrancheIntegratedPatchId:
    def test_computes_from_fetched_diff(self) -> None:
        row = _row()
        config = _config()
        diff_fetcher = MagicMock(return_value="diff --git a/x b/x\n")
        patch_id_computer = MagicMock(return_value="patch-abc")

        result = tranche_integrated_patch_id(
            row, "cafef00d", config,
            diff_fetcher=diff_fetcher, patch_id_computer=patch_id_computer,
        )

        assert result == "patch-abc"
        diff_fetcher.assert_called_once_with("acme/api", "deadbeef", "cafef00d")

    def test_none_when_diff_fetch_fails(self) -> None:
        row = _row()
        config = _config()
        diff_fetcher = MagicMock(side_effect=RuntimeError("gh failed"))

        result = tranche_integrated_patch_id(row, "cafef00d", config, diff_fetcher=diff_fetcher)

        assert result is None

    def test_none_when_diff_is_empty(self) -> None:
        row = _row()
        config = _config()
        diff_fetcher = MagicMock(return_value=None)

        result = tranche_integrated_patch_id(row, "cafef00d", config, diff_fetcher=diff_fetcher)

        assert result is None

    def test_none_when_repo_not_configured(self) -> None:
        row = _row(repo_name="unknown")
        config = _config()

        result = tranche_integrated_patch_id(row, "cafef00d", config)

        assert result is None


# ── The single closing PR ───────────────────────────────────────────────────


class TestTrancheClosingPrBody:
    def test_closes_every_done_child_and_refs_the_parent(self) -> None:
        row = _row()
        body = tranche_closing_pr_body(row)
        assert "Closes #101" in body
        assert "Closes #102" in body
        assert "Refs #100" in body

    def test_skips_a_child_that_never_reached_done(self) -> None:
        row = _row(children=[
            TrancheChild(issue_number=101, repo_name="api", state="done"),
            TrancheChild(issue_number=102, repo_name="api", state="failed"),
        ])
        body = tranche_closing_pr_body(row)
        assert "Closes #101" in body
        assert "Closes #102" not in body


class _FakeGhOps:
    def __init__(self, response: dict | None = None, *, raises: bool = False) -> None:
        self._response = response or {"number": 55, "url": "https://x/55", "existed": False}
        self._raises = raises
        self.calls: list[dict] = []

    def create_pr(self, repo: str, *, base: str, head: str, title: str, body: str) -> dict:
        if self._raises:
            raise RuntimeError("gh pr create failed")
        self.calls.append({"repo": repo, "base": base, "head": head, "title": title, "body": body})
        return self._response


_APPROVED_VERDICT = {"approved": True, "pending": (), "blocking": ()}


class TestOpenTrancheClosingPr:
    def test_opens_against_default_branch_with_closing_body(self) -> None:
        row = _row()
        config = _config()
        gh_ops = _FakeGhOps()

        result = open_tranche_closing_pr(
            row, config, test_passed=True, review_verdict=_APPROVED_VERDICT, gh_ops=gh_ops,
        )

        assert result == {"number": 55, "url": "https://x/55", "existed": False}
        assert len(gh_ops.calls) == 1
        call = gh_ops.calls[0]
        assert call["repo"] == "acme/api"
        assert call["base"] == "main"
        assert call["head"] == "tranche-7-integrated"
        assert "Closes #101" in call["body"]
        assert "Closes #102" in call["body"]

    def test_none_when_repo_not_configured(self) -> None:
        row = _row(repo_name="unknown")
        config = _config()
        gh_ops = _FakeGhOps()

        result = open_tranche_closing_pr(
            row, config, test_passed=True, review_verdict=_APPROVED_VERDICT, gh_ops=gh_ops,
        )

        assert result is None
        assert gh_ops.calls == []

    def test_none_when_create_pr_raises(self) -> None:
        row = _row()
        config = _config()
        gh_ops = _FakeGhOps(raises=True)

        result = open_tranche_closing_pr(
            row, config, test_passed=True, review_verdict=_APPROVED_VERDICT, gh_ops=gh_ops,
        )

        assert result is None

    def test_refuses_when_test_has_not_passed(self) -> None:
        # The ordering "Test leg passed AND tranche_review_verdict
        # reports approved=True" is a gate this function enforces itself,
        # not prose a caller might skip.
        row = _row()
        config = _config()
        gh_ops = _FakeGhOps()

        with pytest.raises(ValueError, match="test_passed"):
            open_tranche_closing_pr(
                row, config, test_passed=False, review_verdict=_APPROVED_VERDICT, gh_ops=gh_ops,
            )
        assert gh_ops.calls == []

    def test_refuses_when_review_verdict_is_not_approved(self) -> None:
        row = _row()
        config = _config()
        gh_ops = _FakeGhOps()
        unapproved = {"approved": False, "pending": (), "blocking": ("gates",)}

        with pytest.raises(ValueError):
            open_tranche_closing_pr(
                row, config, test_passed=True, review_verdict=unapproved, gh_ops=gh_ops,
            )
        assert gh_ops.calls == []

    def test_refuses_when_review_verdict_is_none(self) -> None:
        row = _row()
        config = _config()
        gh_ops = _FakeGhOps()

        with pytest.raises(ValueError):
            open_tranche_closing_pr(
                row, config, test_passed=True, review_verdict=None, gh_ops=gh_ops,
            )
        assert gh_ops.calls == []


# ── the Test leg's own board row ────────────────────────────────────────────


def _test_leg(
    *, row_id: int = 7, test_state: str | None, status: str = "done",
    dispatched_at: float = 0.0, assignment_id: str = "smoke-1",
    repo_name: str = "api",
) -> Assignment:
    return Assignment(
        machine_name="m1", repo_name=repo_name, issue_number=100,
        issue_title="[test] Decompose the big thing", assignment_id=assignment_id,
        type="smoke", status=status, branch=f"tranche-{row_id}-integrated",
        review_target=f"tranche-{row_id}-integrated",
        test_state=test_state, dispatched_at=dispatched_at,
    )


class TestTrancheTestLegAssignment:
    def test_picks_the_most_recently_dispatched_leg(self) -> None:
        row = _row()
        board = Board()
        board.completed.append(
            _test_leg(test_state="failed", dispatched_at=1.0, assignment_id="old")
        )
        board.active.append(
            _test_leg(test_state=None, status="running", dispatched_at=2.0, assignment_id="new")
        )

        leg = tranche_test_leg_assignment(board, row)

        assert leg is not None
        assert leg.assignment_id == "new"

    def test_none_when_no_test_leg_was_ever_dispatched(self) -> None:
        assert tranche_test_leg_assignment(Board(), _row()) is None

    def test_ignores_another_rows_integration_branch(self) -> None:
        board = Board()
        board.active.append(_test_leg(row_id=9, test_state="passed"))
        assert tranche_test_leg_assignment(board, _row(id=7)) is None

    def test_none_when_row_id_is_none(self) -> None:
        board = Board()
        board.active.append(_test_leg(test_state="passed"))
        assert tranche_test_leg_assignment(board, _row(id=None)) is None


# ── scoped re-review: dispatching a SUBSET of the three focuses ─────────────


class TestDispatchTrancheReviewLegsFocuses:
    def test_dispatches_only_the_requested_focuses(self) -> None:
        row = _row()
        board = Board()
        client = _FakeHTTPClient({"id": "rev-1"})

        result = dispatch_tranche_review_legs(
            row, "cafef00d", _config(), ["m1", "m2"], board, http_client=client,
            integrated_patch_id="patch-xyz", focuses=["gates"],
        )

        assert result["ok"] is True
        assert list(result["legs"]) == ["gates"]
        assert len(board.active) == 1
        assert board.active[0].review_target == tranche_review_target_for(7, "gates")

    def test_a_focus_keeps_its_machine_whether_dispatched_alone_or_not(self) -> None:
        # "gates" is index 2 of three focuses over two machines, so it
        # belongs to m1 — and must still, when it is re-dispatched on its
        # own as a scoped re-review.
        board_all = Board()
        dispatch_tranche_review_legs(
            _row(), "cafef00d", _config(), ["m1", "m2"], board_all,
            http_client=_FakeHTTPClient({"id": "r"}), integrated_patch_id="p",
        )
        alone = Board()
        dispatch_tranche_review_legs(
            _row(), "cafef00d", _config(), ["m1", "m2"], alone,
            http_client=_FakeHTTPClient({"id": "r"}), integrated_patch_id="p",
            focuses=["gates"],
        )

        by_focus = {
            a.review_target: a.machine_name for a in board_all.active
        }
        assert alone.active[0].machine_name == by_focus[
            tranche_review_target_for(7, "gates")
        ]

    def test_empty_focuses_is_refused_never_a_vacuous_ok(self) -> None:
        with pytest.raises(ValueError, match="focuses is empty"):
            dispatch_tranche_review_legs(
                _row(), "cafef00d", _config(), ["m1"], Board(),
                http_client=_FakeHTTPClient(), integrated_patch_id="p", focuses=[],
            )

    def test_unknown_focus_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown focus"):
            dispatch_tranche_review_legs(
                _row(), "cafef00d", _config(), ["m1"], Board(),
                http_client=_FakeHTTPClient(), integrated_patch_id="p",
                focuses=["security"],
            )


# ── the ordering itself: coord.drive.advance_tranche_row ────────────────────
#
# These are the tests that observe the ORDER as code rather than prose: a
# Test leg only against an integrated tree, review legs only after that
# Test leg passed, the closing PR only after all three legs approved the
# CURRENT integrated patch-id.


_UNSET = object()


class _Advance:
    """Stub bundle for `advance_tranche_row`'s injectable primitives — it
    records what was called so a test can assert that a stage dispatched
    NOTHING, which is the half most of these gates are about."""

    def __init__(
        self, *, sha: str | None = "cafef00d", patch_id: str | None = "patch-now",
        test_result: dict | None = None, review_result: dict | None = None,
        pr: object = _UNSET, integrate: object = None,
    ) -> None:
        self.sha = sha
        self.patch_id = patch_id
        self.test_calls: list[tuple] = []
        self.review_calls: list[dict] = []
        self.pr_calls: list[dict] = []
        self.integrate_calls: list[tuple] = []
        self._test_result = test_result or {
            "ok": True, "assignment_id": "smoke-1", "machine_name": "m1",
        }
        self._review_result = review_result or {"ok": True, "legs": {}}
        self._pr = (
            {"number": 55, "url": "https://x/55", "existed": False}
            if pr is _UNSET else pr
        )
        self._integrate = integrate

    def sha_resolver(self, repo_github: str, branch: str) -> str | None:
        return self.sha

    def fingerprinter(self, row, integrated_sha, config) -> str | None:
        return self.patch_id

    def test_dispatcher(self, row, integrated_sha, config, machine_names, board) -> dict:
        self.test_calls.append((row, integrated_sha, tuple(machine_names)))
        return self._test_result

    def review_dispatcher(self, row, integrated_sha, config, machine_names, board, **kw) -> dict:
        self.review_calls.append(dict(integrated_sha=integrated_sha, **kw))
        return self._review_result

    def pr_opener(self, row, config, *, test_passed, review_verdict) -> dict | None:
        self.pr_calls.append({"test_passed": test_passed, "review_verdict": review_verdict})
        return self._pr

    def integrator(self, row, config, machine_names, **kw):
        self.integrate_calls.append((row, tuple(machine_names)))
        return self._integrate(row) if callable(self._integrate) else (row, None, None)

    def kwargs(self) -> dict:
        return dict(
            sha_resolver=self.sha_resolver,
            integrator=self.integrator,
            test_dispatcher=self.test_dispatcher,
            test_state_reader=lambda _aid: None,
            review_dispatcher=self.review_dispatcher,
            patch_id_fingerprinter=self.fingerprinter,
            pr_opener=self.pr_opener,
        )


def _merge_result(*, ok: bool = True, integrated_sha: str | None, detail: str = "merged"):
    from coord.merge_queue import TrancheMergeResult

    return TrancheMergeResult(ok, False, integrated_sha, (), detail)


class TestAdvanceTrancheRowPreconditions:
    def test_refuses_an_empty_machine_pool(self) -> None:
        with pytest.raises(ValueError, match="machine_names is empty"):
            advance_tranche_row(_row(), _config(), [], Board())

    def test_refuses_without_a_board(self) -> None:
        with pytest.raises(ValueError, match="board is None"):
            advance_tranche_row(_row(), _config(), ["m1"], None)

    def test_refuses_an_unsaved_row(self) -> None:
        with pytest.raises(ValueError, match="row.id is None"):
            advance_tranche_row(_row(id=None), _config(), ["m1"], Board())

    def test_refuses_a_row_whose_children_were_never_dispatched(self) -> None:
        with pytest.raises(ValueError, match="still 'pending'"):
            advance_tranche_row(_row(state="pending"), _config(), ["m1"], Board())

    def test_an_unknown_state_is_refused_not_treated_as_integrated(self) -> None:
        stub = _Advance()
        with pytest.raises(ValueError, match="unknown state"):
            advance_tranche_row(
                _row(state="reticulating"), _config(), ["m1"], Board(), **stub.kwargs()
            )
        assert stub.test_calls == []

    def test_a_failed_row_is_never_advanced(self) -> None:
        stub = _Advance()
        step = advance_tranche_row(
            _row(state="failed"), _config(), ["m1"], Board(), **stub.kwargs()
        )
        assert step["stage"] == "failed"
        assert stub.test_calls == []
        assert stub.pr_calls == []


class TestAdvanceTrancheRowIntegration:
    def test_waits_while_any_child_is_not_terminal(self) -> None:
        row = _row(state="dispatched")
        row.children[1].state = "dispatched"
        stub = _Advance()

        step = advance_tranche_row(row, _config(), ["m1"], Board(), **stub.kwargs())

        assert step["stage"] == "awaiting-children"
        assert "#102" in step["detail"]
        assert stub.integrate_calls == []
        assert stub.test_calls == []

    def test_a_clean_integration_dispatches_the_test_leg_in_the_same_call(self) -> None:
        # The gap the review named: a row that REACHES state="done" must
        # not sit untested waiting for some later tick.
        row = _row(state="dispatched")

        def _integrate(r):
            r.state = "done"
            return r, _merge_result(integrated_sha="freshsha"), None

        stub = _Advance(integrate=_integrate)
        step = advance_tranche_row(row, _config(), ["m1"], Board(), **stub.kwargs())

        assert len(stub.integrate_calls) == 1
        assert step["stage"] == "test-dispatched"
        # The Test leg is bound to the sha the merge itself re-confirmed
        # after pushing, not to a second read of the branch head.
        assert stub.test_calls and stub.test_calls[0][1] == "freshsha"

    def test_falls_back_to_the_branch_head_when_the_merge_reports_no_sha(self) -> None:
        row = _row(state="dispatched")

        def _integrate(r):
            r.state = "done"
            return r, _merge_result(integrated_sha=None), None

        stub = _Advance(integrate=_integrate, sha="branchsha")
        step = advance_tranche_row(row, _config(), ["m1"], Board(), **stub.kwargs())

        assert step["stage"] == "test-dispatched"
        assert stub.test_calls[0][1] == "branchsha"

    def test_a_failed_integration_dispatches_nothing(self) -> None:
        row = _row(state="dispatched")

        def _integrate(r):
            r.state = "failed"
            return r, _merge_result(
                ok=False, integrated_sha=None, detail="child 102 never finished",
            ), None

        stub = _Advance(integrate=_integrate)
        step = advance_tranche_row(row, _config(), ["m1"], Board(), **stub.kwargs())

        assert step["stage"] == "failed"
        assert stub.test_calls == []
        assert stub.pr_calls == []

    def test_an_integrator_leg_in_flight_is_awaited(self) -> None:
        stub = _Advance()
        step = advance_tranche_row(
            _row(state="integrating"), _config(), ["m1"], Board(), **stub.kwargs()
        )
        assert step["stage"] == "awaiting-integrator"
        assert stub.test_calls == []

    def test_an_unreadable_branch_head_binds_nothing(self) -> None:
        stub = _Advance(sha=None)
        step = advance_tranche_row(_row(), _config(), ["m1"], Board(), **stub.kwargs())
        assert step["stage"] == "awaiting-integrated-sha"
        assert stub.test_calls == []
        assert stub.review_calls == []
        assert stub.pr_calls == []


class TestAdvanceTrancheRowTestGate:
    def test_dispatches_the_one_test_leg_when_none_exists(self) -> None:
        stub = _Advance()
        step = advance_tranche_row(_row(), _config(), ["m1"], Board(), **stub.kwargs())
        assert step["stage"] == "test-dispatched"
        assert len(stub.test_calls) == 1
        assert stub.review_calls == []

    def test_a_refused_test_dispatch_is_reported_not_swallowed(self) -> None:
        stub = _Advance(test_result={"ok": False, "error": "no capable machine"})
        step = advance_tranche_row(_row(), _config(), ["m1"], Board(), **stub.kwargs())
        assert step["stage"] == "test-dispatch-failed"
        assert "no capable machine" in step["detail"]
        assert stub.review_calls == []

    def test_a_running_test_leg_blocks_the_review_legs(self) -> None:
        board = Board()
        board.active.append(_test_leg(test_state=None, status="running"))
        stub = _Advance()

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **stub.kwargs())

        assert step["stage"] == "awaiting-test"
        assert stub.test_calls == []
        assert stub.review_calls == []
        assert stub.pr_calls == []

    def test_a_failed_test_leg_blocks_the_review_legs_and_the_pr(self) -> None:
        board = Board()
        board.completed.append(_test_leg(test_state="failed"))
        stub = _Advance()

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **stub.kwargs())

        assert step["stage"] == "test-failed"
        assert stub.review_calls == []
        assert stub.pr_calls == []

    def test_a_skipped_test_verdict_is_not_a_pass(self) -> None:
        board = Board()
        board.completed.append(_test_leg(test_state="skipped"))
        stub = _Advance()

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **stub.kwargs())

        assert step["stage"] == "awaiting-test"
        assert stub.review_calls == []

    def test_the_live_verdict_read_wins_over_a_stale_board_copy(self) -> None:
        board = Board()
        board.completed.append(_test_leg(test_state=None))
        stub = _Advance()
        kwargs = stub.kwargs()
        kwargs["test_state_reader"] = lambda _aid: "passed"

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **kwargs)

        assert step["stage"] == "review-dispatched"


def _passed_board(row_id: int = 7) -> Board:
    board = Board()
    board.completed.append(_test_leg(row_id=row_id, test_state="passed"))
    return board


class TestAdvanceTrancheRowReviewGate:
    def test_dispatches_all_three_focuses_once_the_test_leg_passed(self) -> None:
        stub = _Advance()
        step = advance_tranche_row(
            _row(), _config(), ["m1", "m2"], _passed_board(), **stub.kwargs()
        )

        assert step["stage"] == "review-dispatched"
        assert len(stub.review_calls) == 1
        call = stub.review_calls[0]
        assert sorted(call["focuses"]) == sorted(TRANCHE_REVIEW_FOCUSES)
        assert call["integrated_patch_id"] == "patch-now"
        assert stub.pr_calls == []

    def test_legs_in_flight_against_the_current_patch_id_are_awaited(self) -> None:
        board = _passed_board()
        for focus in TRANCHE_REVIEW_FOCUSES:
            leg = _review_leg(focus, verdict=None, patch_id="patch-now")
            leg.status = "running"
            board.active.append(leg)
        stub = _Advance()

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **stub.kwargs())

        assert step["stage"] == "awaiting-review"
        assert stub.review_calls == []
        assert stub.pr_calls == []

    def test_a_fix_that_moves_the_patch_id_forces_a_scoped_re_review(self) -> None:
        # Three legs approved patch A; the integrated tree has since moved
        # to patch B. The stale approvals must NOT count, and exactly the
        # stale focuses are re-dispatched.
        board = _passed_board()
        for focus in TRANCHE_REVIEW_FOCUSES:
            board.completed.append(
                _review_leg(focus, verdict="approve", patch_id="patch-old")
            )
        stub = _Advance(patch_id="patch-now")

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **stub.kwargs())

        assert step["stage"] == "review-dispatched"
        assert stub.pr_calls == []
        call = stub.review_calls[0]
        assert sorted(call["focuses"]) == sorted(TRANCHE_REVIEW_FOCUSES)
        assert call["integrated_patch_id"] == "patch-now"
        # The re-dispatch carries each focus's prior verdict, so the leg
        # reviews the fix delta rather than starting from scratch.
        assert call["prior_verdicts"] == {f: "approve" for f in TRANCHE_REVIEW_FOCUSES}

    def test_only_the_stale_focus_is_re_dispatched(self) -> None:
        board = _passed_board()
        board.completed.append(_review_leg("recipe", verdict="approve", patch_id="patch-now"))
        board.completed.append(_review_leg("behaviour", verdict="approve", patch_id="patch-now"))
        board.completed.append(_review_leg("gates", verdict="approve", patch_id="patch-old"))
        stub = _Advance(patch_id="patch-now")

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **stub.kwargs())

        assert step["stage"] == "review-dispatched"
        assert stub.review_calls[0]["focuses"] == ["gates"]
        assert stub.pr_calls == []

    def test_a_leg_with_no_patch_id_is_re_dispatched_not_counted(self) -> None:
        board = _passed_board()
        for focus in TRANCHE_REVIEW_FOCUSES:
            board.completed.append(_review_leg(focus, verdict="approve", patch_id=None))
        stub = _Advance(patch_id="patch-now")

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **stub.kwargs())

        assert step["stage"] == "review-dispatched"
        assert stub.pr_calls == []

    def test_a_request_changes_blocks_without_re_asking_the_reviewer(self) -> None:
        board = _passed_board()
        board.completed.append(_review_leg("recipe", verdict="approve", patch_id="patch-now"))
        board.completed.append(_review_leg("behaviour", verdict="approve", patch_id="patch-now"))
        board.completed.append(
            _review_leg("gates", verdict="request-changes", patch_id="patch-now")
        )
        stub = _Advance()

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **stub.kwargs())

        assert step["stage"] == "review-blocking"
        assert "gates" in step["detail"]
        assert stub.review_calls == []
        assert stub.pr_calls == []

    def test_an_unfingerprintable_tree_blocks_instead_of_counting_old_approvals(
        self,
    ) -> None:
        # Three legs approved; the diff could not be fingerprinted, so
        # there is nothing to bind those approvals to. The PR must not
        # open on the strength of an unbound approval.
        board = _passed_board()
        for focus in TRANCHE_REVIEW_FOCUSES:
            board.completed.append(
                _review_leg(focus, verdict="approve", patch_id="patch-old")
            )
        stub = _Advance(patch_id=None)

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **stub.kwargs())

        assert step["stage"] == "awaiting-patch-id"
        assert stub.pr_calls == []
        assert stub.review_calls == []

    def test_a_failed_review_dispatch_is_reported(self) -> None:
        stub = _Advance(
            review_result={
                "ok": False,
                "legs": {"gates": {"ok": False, "error": "agent unreachable"}},
            }
        )
        step = advance_tranche_row(_row(), _config(), ["m1"], _passed_board(), **stub.kwargs())

        assert step["stage"] == "review-dispatch-failed"
        assert "agent unreachable" in step["detail"]
        assert stub.pr_calls == []


class TestAdvanceTrancheRowClosingPr:
    def _approved_board(self) -> Board:
        board = _passed_board()
        for focus in TRANCHE_REVIEW_FOCUSES:
            board.completed.append(
                _review_leg(focus, verdict="approve", patch_id="patch-now")
            )
        return board

    def test_opens_the_single_pr_once_test_and_all_three_legs_are_satisfied(self) -> None:
        stub = _Advance()
        step = advance_tranche_row(
            _row(), _config(), ["m1"], self._approved_board(), **stub.kwargs()
        )

        assert step["stage"] == "pr-opened"
        assert step["pr"] == {"number": 55, "url": "https://x/55", "existed": False}
        assert len(stub.pr_calls) == 1
        assert stub.pr_calls[0]["test_passed"] is True
        assert stub.pr_calls[0]["review_verdict"]["approved"] is True
        assert stub.review_calls == []

    def test_a_pr_that_could_not_be_opened_is_reported_not_claimed(self) -> None:
        stub = _Advance(pr=None)
        step = advance_tranche_row(
            _row(), _config(), ["m1"], self._approved_board(), **stub.kwargs()
        )
        assert step["stage"] == "pr-failed"

    def test_the_real_pr_opener_is_the_default_and_enforces_its_own_gate(self) -> None:
        # No `pr_opener` injected: the default is the real
        # `open_tranche_closing_pr`, whose own gate must agree with the
        # verdict this function computed.
        board = _passed_board()
        board.completed.append(
            _review_leg("recipe", verdict="approve", patch_id="patch-now")
        )
        stub = _Advance()
        kwargs = stub.kwargs()
        del kwargs["pr_opener"]

        step = advance_tranche_row(_row(), _config(), ["m1"], board, **kwargs)

        # Only one focus approved, so the PR is never attempted at all.
        assert step["stage"] == "review-dispatched"


class TestTrancheRowsReadyToAdvance:
    def test_selects_every_state_with_a_step_left(self) -> None:
        rows = [
            _row(id=1, state="pending"),
            _row(id=2, state="dispatched"),
            _row(id=3, state="integrating"),
            _row(id=4, state="done"),
            _row(id=5, state="failed"),
        ]
        assert [r.id for r in tranche_rows_ready_to_advance(rows)] == [2, 3, 4]

    def test_skips_an_unsaved_row(self) -> None:
        assert tranche_rows_ready_to_advance([_row(id=None, state="done")]) == []

    def test_advanceable_states_exclude_the_two_terminal_ends(self) -> None:
        assert "pending" not in TRANCHE_ADVANCEABLE_STATES
        assert "failed" not in TRANCHE_ADVANCEABLE_STATES


# ── the production call site: `coord drive-queue tick` ──────────────────────
#
# A tranche row is not a `QueueEntry`, so it never goes through the
# capacity walk `plan_tick` owns. These tests drive the real CLI tick and
# assert it reaches `coord.drive.advance_tranche_row` for a row that has a
# step left — the wiring, not just the primitives.


_TICK_CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: m1
    host: m1.tail
    repos: [api]
    repo_paths:
      api: /repo/api
"""


@pytest.fixture
def tick_cli(tmp_path, monkeypatch):
    """Invoke the real `coord drive-queue tick` against a seeded config,
    with the host-local bits (tick lock, tmux, block log) pointed at
    tmp_path — same isolation tests/test_cli_drive_queue.py uses."""
    from click.testing import CliRunner

    from coord.cli import main

    config_file = tmp_path / "coordinator.yml"
    config_file.write_text(_TICK_CONFIG_YAML)
    monkeypatch.setattr(
        "coord.filelock.drive_queue_lock_path", lambda: tmp_path / "queue.lock"
    )
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])
    monkeypatch.setenv("COORD_BLOCK_LOG", str(tmp_path / "block-log.jsonl"))

    def run(*args: str):
        return CliRunner().invoke(
            main, ["drive-queue", "tick", *args, "--config", str(config_file)]
        )

    return run


@pytest.fixture
def saved_row():
    """Persist a tranche row so the tick's own DB read finds it."""
    from coord.state import load_tranche_row, save_tranche_row

    def _save(**overrides) -> TrancheRow:
        row = _row(id=None, **overrides)
        row.id = save_tranche_row(row)
        return load_tranche_row(row.id)

    return _save


class TestLoadTrancheRowsInStates:
    def test_returns_only_the_requested_states_oldest_first(self, saved_row) -> None:
        from coord.state import load_tranche_rows_in_states

        done = saved_row(state="done")
        saved_row(state="pending")
        dispatched = saved_row(state="dispatched")

        rows = load_tranche_rows_in_states(["done", "dispatched"])

        assert [r.id for r in rows] == [done.id, dispatched.id]

    def test_an_empty_filter_selects_nothing_never_everything(self, saved_row) -> None:
        from coord.state import load_tranche_rows_in_states

        saved_row(state="done")
        assert load_tranche_rows_in_states([]) == []


class TestTickAdvancesTrancheRows:
    def test_the_tick_dispatches_the_test_leg_for_an_integrated_row(
        self, tick_cli, saved_row, monkeypatch
    ) -> None:
        saved_row(state="done")
        monkeypatch.setattr(
            "coord.github_ops.get_branch_sha", lambda *a, **k: "cafef00d"
        )
        calls: list[tuple] = []

        def _fake_test_dispatch(row, integrated_sha, config, machine_names, board):
            calls.append((row.id, integrated_sha, tuple(machine_names)))
            return {"ok": True, "assignment_id": "smoke-1", "machine_name": "m1"}

        monkeypatch.setattr(
            "coord.dispatch.dispatch_tranche_test", _fake_test_dispatch
        )

        result = tick_cli()

        assert result.exit_code == 0, result.output
        assert calls, f"the tick never dispatched a Test leg: {result.output}"
        assert calls[0][1] == "cafef00d"
        assert calls[0][2] == ("m1",)
        assert "test-dispatched" in result.output

    def test_the_tick_opens_the_closing_pr_once_every_gate_is_satisfied(
        self, tick_cli, saved_row, monkeypatch
    ) -> None:
        row = saved_row(state="done")
        monkeypatch.setattr(
            "coord.github_ops.get_branch_sha", lambda *a, **k: "cafef00d"
        )
        monkeypatch.setattr(
            "coord.review.tranche_integrated_patch_id", lambda *a, **k: "patch-now"
        )
        board = _passed_board(row.id)
        for focus in TRANCHE_REVIEW_FOCUSES:
            board.completed.append(
                _review_leg(focus, row_id=row.id, verdict="approve", patch_id="patch-now")
            )
        from coord.state import save_board

        save_board(board)
        opened: list[dict] = []

        def _fake_open_pr(r, config, *, test_passed, review_verdict, **kw):
            opened.append({"test_passed": test_passed, "verdict": review_verdict})
            return {"number": 55, "url": "https://x/55", "existed": False}

        monkeypatch.setattr("coord.merge_queue.open_tranche_closing_pr", _fake_open_pr)

        result = tick_cli()

        assert result.exit_code == 0, result.output
        assert opened, f"the tick never opened the closing PR: {result.output}"
        assert opened[0]["test_passed"] is True
        assert opened[0]["verdict"]["approved"] is True
        assert "pr-opened" in result.output

    def test_reconcile_only_starts_nothing_for_a_tranche_row_either(
        self, tick_cli, saved_row, monkeypatch
    ) -> None:
        saved_row(state="done")
        monkeypatch.setattr(
            "coord.github_ops.get_branch_sha", lambda *a, **k: "cafef00d"
        )
        calls: list[tuple] = []
        monkeypatch.setattr(
            "coord.dispatch.dispatch_tranche_test",
            lambda *a, **k: calls.append(a) or {"ok": True, "assignment_id": "x"},
        )

        result = tick_cli("--reconcile-only")

        assert result.exit_code == 0, result.output
        assert calls == []

    def test_a_pending_row_is_left_to_the_child_fan_out(
        self, tick_cli, saved_row, monkeypatch
    ) -> None:
        # `advance_tranche_row` refuses a "pending" row outright; the tick
        # must never hand it one (that is the child dispatcher's job), so
        # no refusal ever surfaces.
        saved_row(state="pending")
        calls: list[tuple] = []
        monkeypatch.setattr(
            "coord.dispatch.dispatch_tranche_test",
            lambda *a, **k: calls.append(a) or {"ok": True, "assignment_id": "x"},
        )

        result = tick_cli()

        assert result.exit_code == 0, result.output
        assert calls == []
        assert "could not advance" not in result.output
