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
    tranche_test_machine_name,
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
