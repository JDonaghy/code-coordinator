"""Tests for the Gate A human sign-off gate (#2063).

Three layers, mirroring how the feature is built:

- :mod:`coord.gate_a` — the pure verdict/digest/marker logic (no I/O).
- :func:`coord.milestone_dispatch.issue_oracle_ready` — the refusal, at the
  point where the contract is *consumed* rather than at the merge (the
  Gate-A PR is merged with ``gh pr merge``, outside coord entirely).
- ``coord.drive_queue`` — that the refusal **parks** (re-checked each tick,
  #1891/#1892) instead of landing in terminal ``blocked`` (#2040), which
  ``coord drive-queue add`` cannot clear.
"""

from __future__ import annotations

import pytest

from coord import gate_a
from coord.config import (
    AcceptanceConfig,
    AcceptanceDriverConfig,
    Config,
)
from coord.milestone_dispatch import issue_oracle_ready
from coord.models import Assignment, Machine, Repo

CONTRACT_V1 = "# Contract\n\n- the Save button says `Save`\n"
CONTRACT_V2 = "# Contract\n\n- the Save button says `Publish`\n"


def _cfg() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/tmp/api"},
            )
        ],
        acceptance=AcceptanceConfig(
            drivers={"api": AcceptanceDriverConfig(kind="cli-pytest", run="pytest")}
        ),
    )


def _approved(contract: str = CONTRACT_V1, *, verdict: str = "approved") -> dict:
    return gate_a.make_record(
        repo_name="api",
        milestone_number=37,
        verdict=verdict,
        contract_sha=gate_a.contract_digest(contract),
        tracking_issue=900,
        now=1000.0,
    ).to_dict()


# ── contract_digest ─────────────────────────────────────────────────────────


class TestContractDigest:
    def test_identical_text_hashes_identically(self) -> None:
        assert gate_a.contract_digest(CONTRACT_V1) == gate_a.contract_digest(
            CONTRACT_V1
        )

    def test_pinned_surface_change_changes_the_digest(self) -> None:
        """The whole point: `Save` -> `Publish` is a different contract.

        Those strings become assertions in a sealed suite the worker may
        never edit, so an amend that rewords one must force a fresh look.
        """
        assert gate_a.contract_digest(CONTRACT_V1) != gate_a.contract_digest(
            CONTRACT_V2
        )

    def test_line_endings_and_trailing_newlines_are_not_a_change(self) -> None:
        crlf = CONTRACT_V1.replace("\n", "\r\n")
        assert gate_a.contract_digest(crlf) == gate_a.contract_digest(CONTRACT_V1)
        assert gate_a.contract_digest(CONTRACT_V1 + "\n\n") == gate_a.contract_digest(
            CONTRACT_V1
        )

    def test_accepts_bytes(self) -> None:
        assert gate_a.contract_digest(CONTRACT_V1.encode()) == gate_a.contract_digest(
            CONTRACT_V1
        )


# ── evaluate ────────────────────────────────────────────────────────────────


class TestEvaluate:
    def _evaluate(self, **kw):
        base = dict(
            repo_name="api",
            milestone_number=37,
            contract_text=CONTRACT_V1,
            approval=None,
        )
        base.update(kw)
        return gate_a.evaluate(**base)

    def test_no_verdict_refuses(self) -> None:
        d = self._evaluate()
        assert d.ok is False
        assert d.state == gate_a.STATE_MISSING
        assert "no recorded human sign-off" in d.reason
        assert "coord gate-a --approved api" in d.reason

    def test_matching_approval_passes(self) -> None:
        d = self._evaluate(approval=_approved(CONTRACT_V1))
        assert d.ok is True
        assert d.state == gate_a.STATE_APPROVED
        assert d.reason is None

    def test_amend_invalidates_a_prior_approval(self) -> None:
        """#2063's own trap: approving v1 must not silently approve v2."""
        d = self._evaluate(
            contract_text=CONTRACT_V2, approval=_approved(CONTRACT_V1)
        )
        assert d.ok is False
        assert d.state == gate_a.STATE_STALE
        assert "stale" in d.reason

    def test_changes_requested_refuses_with_the_note(self) -> None:
        record = _approved(CONTRACT_V1, verdict=gate_a.VERDICT_CHANGES)
        record["note"] = "status vocabulary is wrong"
        d = self._evaluate(approval=record)
        assert d.ok is False
        assert d.state == gate_a.STATE_CHANGES
        assert "status vocabulary is wrong" in d.reason
        assert "--amend" in d.reason

    def test_changes_against_an_older_contract_still_refuses(self) -> None:
        record = _approved(CONTRACT_V1, verdict=gate_a.VERDICT_CHANGES)
        d = self._evaluate(contract_text=CONTRACT_V2, approval=record)
        assert d.ok is False
        assert d.state == gate_a.STATE_STALE

    def test_unreadable_contract_fails_closed(self) -> None:
        d = self._evaluate(contract_text=None, approval=_approved(CONTRACT_V1))
        assert d.ok is False
        assert d.state == gate_a.STATE_MISSING

    def test_declared_milestone_exemption_passes(self) -> None:
        d = self._evaluate(exempt=True)
        assert d.ok is True
        assert d.state == gate_a.STATE_EXEMPT
        assert d.exempt_reason == ""

    def test_declared_milestone_exemption_carries_its_reason(self) -> None:
        """#2063's "explicit and declared... reviewable" opt-out only holds
        if the reason an operator wrote in the manifest's `gate_a: {exempt:
        true, reason: ...}` actually survives into the decision, instead of
        being parsed and discarded."""
        d = self._evaluate(exempt=True, exempt_reason="no user-visible surface")
        assert d.exempt_reason == "no user-visible surface"

    def test_unknown_schema_degrades_to_no_approval(self) -> None:
        record = _approved(CONTRACT_V1)
        record["schema"] = 99
        d = self._evaluate(approval=record)
        assert d.ok is False
        assert d.state == gate_a.STATE_MISSING

    def test_every_refusal_carries_the_park_marker(self) -> None:
        """The marker is the only channel that survives the process
        boundary to `coord drive-queue`'s tick — a refusal without it would
        land the queue entry in terminal `blocked` (#2040)."""
        for kw in (
            {},
            {"contract_text": None},
            {"contract_text": CONTRACT_V2, "approval": _approved(CONTRACT_V1)},
            {
                "approval": _approved(
                    CONTRACT_V1, verdict=gate_a.VERDICT_CHANGES
                )
            },
        ):
            d = self._evaluate(**kw)
            assert d.ok is False
            assert gate_a.is_gate_a_refusal_reason(d.reason), kw
            parsed = gate_a.parse_park_marker(d.reason)
            assert parsed is not None
            assert parsed[0] == "api"
            assert parsed[1] == 37


class TestMakeRecord:
    def test_rejects_an_unknown_verdict(self) -> None:
        with pytest.raises(ValueError):
            gate_a.make_record(
                repo_name="api",
                milestone_number=37,
                verdict="maybe",
                contract_sha="deadbeef",
            )

    def test_roundtrips_through_dict(self) -> None:
        rec = gate_a.make_record(
            repo_name="api",
            milestone_number=37,
            verdict=gate_a.VERDICT_APPROVED,
            contract_sha="abc",
            note="ok",
            actor="john",
            now=5.0,
        )
        back = gate_a.GateAApproval.from_dict(rec.to_dict())
        assert back == rec


class TestSummarise:
    """`coord gate-a <repo> <issue>`'s one-line output — the surface an
    operator actually reads."""

    def test_exempt_without_a_reason(self) -> None:
        d = gate_a.GateADecision(state=gate_a.STATE_EXEMPT, ok=True)
        assert gate_a.summarise(d) == (
            "exempt — this milestone declared it needs no human sign-off"
        )

    def test_exempt_surfaces_the_declared_reason(self) -> None:
        """The reason threaded through `evaluate()` must actually reach the
        text an operator sees — that's what makes the opt-out "reviewable"
        rather than just "known to have happened"."""
        d = gate_a.GateADecision(
            state=gate_a.STATE_EXEMPT, ok=True, exempt_reason="no user-visible surface",
        )
        summary = gate_a.summarise(d)
        assert "exempt" in summary
        assert "no user-visible surface" in summary


# ── #3065: the pre-merge blind spot ─────────────────────────────────────────


def _mock_author(
    *,
    branch: str,
    assignment_id: str = "a1",
    issue_number: int = 900,
    repo_name: str = "api",
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title="[gate-a-amend] ms-37 — contract correction",
        assignment_id=assignment_id,
        status="done",
        branch=branch,
        type="mock-author",
        dispatched_at=1000.0,
    )


def _review(
    *,
    of_assignment_id: str,
    verdict: str | None,
    dispatched_at: float = 2000.0,
) -> Assignment:
    return Assignment(
        machine_name="desktop",
        repo_name="api",
        issue_number=900,
        issue_title="review",
        assignment_id=f"review-of-{of_assignment_id}",
        status="done",
        type="review",
        review_of_assignment_id=of_assignment_id,
        review_verdict=verdict,
        dispatched_at=dispatched_at,
    )


class TestFindPendingAmends:
    """#3065: the incident's own repro — an approved Gate-A `--amend`
    branch is dispatched, reviewed, even approved, but not yet merged.
    `evaluate()` only ever compares against the default branch, so this is
    invisible to it; `find_pending_amends` is the separate read-side check
    that surfaces it."""

    def test_an_approved_unmerged_branch_is_reported(self) -> None:
        mock = _mock_author(branch="issue-122-gate-a-amend-1")
        review = _review(of_assignment_id="a1", verdict="approve")
        pending = gate_a.find_pending_amends(
            repo_name="api",
            tracking_issue=900,
            all_assignments=[mock, review],
            is_merged=lambda _b: False,
        )
        assert len(pending) == 1
        assert pending[0].branch == "issue-122-gate-a-amend-1"
        assert pending[0].review_verdict == "approve"

    def test_a_merged_branch_is_not_reported(self) -> None:
        """A merged amend is `evaluate()`'s job (STATE_STALE) — this must
        not also report it, or the operator sees the same fact twice under
        two different labels."""
        mock = _mock_author(branch="issue-122-gate-a-amend-1")
        pending = gate_a.find_pending_amends(
            repo_name="api",
            tracking_issue=900,
            all_assignments=[mock],
            is_merged=lambda _b: True,
        )
        assert pending == []

    def test_no_mock_author_rows_reports_nothing(self) -> None:
        pending = gate_a.find_pending_amends(
            repo_name="api",
            tracking_issue=900,
            all_assignments=[],
            is_merged=lambda _b: False,
        )
        assert pending == []

    def test_unreviewed_branch_has_no_verdict(self) -> None:
        mock = _mock_author(branch="issue-122-gate-a-amend-1")
        pending = gate_a.find_pending_amends(
            repo_name="api",
            tracking_issue=900,
            all_assignments=[mock],
            is_merged=lambda _b: False,
        )
        assert pending[0].review_verdict is None

    def test_only_this_repo_and_tracking_issue_match(self) -> None:
        other_repo = _mock_author(
            branch="other-repo-branch", repo_name="web", assignment_id="a2",
        )
        other_issue = _mock_author(
            branch="other-issue-branch", issue_number=901, assignment_id="a3",
        )
        pending = gate_a.find_pending_amends(
            repo_name="api",
            tracking_issue=900,
            all_assignments=[other_repo, other_issue],
            is_merged=lambda _b: False,
        )
        assert pending == []

    def test_most_recent_review_verdict_wins(self) -> None:
        """A re-review (e.g. after a request-changes round) replaces the
        earlier verdict — the latest one dispatched is what's true now."""
        mock = _mock_author(branch="issue-122-gate-a-amend-1")
        first_review = _review(
            of_assignment_id="a1", verdict="request-changes", dispatched_at=2000.0,
        )
        second_review = _review(
            of_assignment_id="a1", verdict="approve", dispatched_at=3000.0,
        )
        pending = gate_a.find_pending_amends(
            repo_name="api",
            tracking_issue=900,
            all_assignments=[mock, second_review, first_review],
            is_merged=lambda _b: False,
        )
        assert pending[0].review_verdict == "approve"

    def test_fix_round_sharing_a_branch_reports_the_current_review(self) -> None:
        """#3065 review: a fix-round worker (`auto_loop.py`'s
        `_dispatch_fix`) gets a brand-new `assignment_id` but reuses the
        SAME `branch` as the original mock-author row, linking back via
        `review_of_assignment_id=<original assignment_id>`. After a normal
        request-changes -> fix -> re-review -> approve cycle, the board
        has two `mock-author` rows for one branch: the stale original
        (older `dispatched_at`, tied to the superseded request-changes
        review) and the fix round (newer `dispatched_at`, tied to the
        current approve review). The branch-dedup must keep the fix
        round's own verdict, stamped directly on its `review_verdict`
        field by `record_work_review_verdict` — not fall back to the
        original's long-superseded review."""
        original = _mock_author(
            branch="issue-122-gate-a-amend-1", assignment_id="a1",
        )
        stale_review = _review(
            of_assignment_id="a1", verdict="request-changes", dispatched_at=1500.0,
        )
        fix_round = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=900,
            issue_title="[gate-a-amend] ms-37 — contract correction (fix)",
            assignment_id="a1-fix",
            status="done",
            branch="issue-122-gate-a-amend-1",
            type="mock-author",
            dispatched_at=2000.0,
            review_of_assignment_id="a1",
            # Stamped by `record_work_review_verdict` once the fix round's
            # own re-review approved it.
            review_verdict="approve",
        )
        pending = gate_a.find_pending_amends(
            repo_name="api",
            tracking_issue=900,
            all_assignments=[original, stale_review, fix_round],
            is_merged=lambda _b: False,
        )
        assert len(pending) == 1
        assert pending[0].branch == "issue-122-gate-a-amend-1"
        assert pending[0].assignment_id == "a1-fix"
        assert pending[0].review_verdict == "approve"


class TestSummarisePendingAmends:
    def test_approved_amend_names_the_branch_and_disclaims_the_approval(
        self,
    ) -> None:
        lines = gate_a.summarise_pending_amends(
            [gate_a.PendingAmend(branch="issue-122-gate-a-amend-1", assignment_id="a1", review_verdict="approve")]
        )
        assert any(
            "issue-122-gate-a-amend-1" in line and "approve" in line
            for line in lines
        )
        assert any("NOT that branch" in line for line in lines)

    def test_request_changes_and_pending_get_distinct_wording(self) -> None:
        changes = gate_a.summarise_pending_amends(
            [gate_a.PendingAmend(branch="b1", assignment_id="a1", review_verdict="request-changes")]
        )
        pending = gate_a.summarise_pending_amends(
            [gate_a.PendingAmend(branch="b2", assignment_id="a2", review_verdict=None)]
        )
        assert changes[0] != pending[0]
        assert "request-changes" in changes[0]
        assert "pending" in pending[0]

    def test_nothing_pending_yields_no_lines(self) -> None:
        assert gate_a.summarise_pending_amends([]) == []


# ── #3131: interactive (CSS-only `:target`) mock contract gaps ─────────────


class TestFindInteractiveControls:
    def test_finds_a_navigating_anchor_with_its_own_id(self) -> None:
        html = '<a id="nav-error" href="#s-error">Error</a>'
        controls = gate_a.find_interactive_controls(html)
        assert controls == [
            gate_a.InteractiveControl(control_id="nav-error", target_id="s-error")
        ]

    def test_falls_back_to_a_synthetic_id_when_the_anchor_has_none(self) -> None:
        html = '<a href="#s-empty">Empty state</a>'
        controls = gate_a.find_interactive_controls(html)
        assert controls == [
            gate_a.InteractiveControl(control_id="->#s-empty", target_id="s-empty")
        ]

    def test_bare_hash_href_is_not_a_navigating_control(self) -> None:
        """coord-portal#307's exact inert shape — `href="#"` with an empty
        fragment — must not be reported as a control at all: there is no
        target to pin a destination against, so this is a different (and
        already-covered) "does the control do anything" concern."""
        html = '<a id="nav-dead" href="#">Nowhere</a>'
        assert gate_a.find_interactive_controls(html) == []

    def test_ignores_anchors_with_no_href(self) -> None:
        html = '<a id="just-an-anchor">Not a link</a>'
        assert gate_a.find_interactive_controls(html) == []

    def test_finds_multiple_controls_across_a_document(self) -> None:
        html = (
            '<nav><a id="nav-start" href="#s-start">Start</a>'
            '<a id="nav-error" href="#s-error">Error</a></nav>'
        )
        controls = gate_a.find_interactive_controls(html)
        assert {c.control_id for c in controls} == {"nav-start", "nav-error"}

    def test_no_mocks_yields_no_controls(self) -> None:
        assert gate_a.find_interactive_controls("<p>just a static screen</p>") == []

    def test_single_quoted_href_is_recognised(self) -> None:
        """#3131 review: `href='#s-error'` is just as valid HTML as the
        double-quoted form — the double-quote-only regex silently yielded
        zero controls for this, a quiet false-negative."""
        html = "<a id='nav-error' href='#s-error'>Error</a>"
        controls = gate_a.find_interactive_controls(html)
        assert controls == [
            gate_a.InteractiveControl(control_id="nav-error", target_id="s-error")
        ]

    def test_single_quoted_id_is_recognised(self) -> None:
        """Same either-quote-style fix as href, applied to the `id`
        attribute — `id='nav-error'` must not silently fall back to the
        synthetic `->#target` id."""
        html = '<a id=\'nav-error\' href="#s-error">Error</a>'
        controls = gate_a.find_interactive_controls(html)
        assert controls == [
            gate_a.InteractiveControl(control_id="nav-error", target_id="s-error")
        ]

    def test_mismatched_quote_styles_do_not_cross_match(self) -> None:
        """The fragment must be closed by the SAME quote character it
        opened with — `href="#s-error'` (mismatched) is malformed markup,
        not a control this should extract a fragment from."""
        html = "<a id=\"nav-error\" href=\"#s-error'>Error</a>"
        assert gate_a.find_interactive_controls(html) == []


class TestUnpinnedInteractiveControls:
    def test_control_and_target_named_together_is_pinned(self) -> None:
        contract = (
            "# Contract\n\n"
            "- clicking `nav-error` makes `#s-error` the only visible "
            "`.screen`\n"
        )
        controls = [gate_a.InteractiveControl("nav-error", "s-error")]
        assert gate_a.unpinned_interactive_controls(contract, controls) == []

    def test_control_mentioned_without_its_destination_is_a_gap(self) -> None:
        """The coord-portal#307 shape in prose form: the contract names the
        control but never says where it goes."""
        contract = "# Contract\n\n- there is a `nav-error` link\n"
        controls = [gate_a.InteractiveControl("nav-error", "s-error")]
        assert gate_a.unpinned_interactive_controls(contract, controls) == controls

    def test_control_not_mentioned_at_all_is_a_gap(self) -> None:
        contract = "# Contract\n\nno mention of any control here\n"
        controls = [gate_a.InteractiveControl("nav-error", "s-error")]
        assert gate_a.unpinned_interactive_controls(contract, controls) == controls

    def test_case_insensitive(self) -> None:
        contract = "- Clicking `NAV-ERROR` shows `#S-ERROR`\n"
        controls = [gate_a.InteractiveControl("nav-error", "s-error")]
        assert gate_a.unpinned_interactive_controls(contract, controls) == []

    def test_control_and_target_on_different_lines_still_a_gap(self) -> None:
        """Same-line proximity is the whole check — two facts scattered
        across the document is not "pinned together"."""
        contract = "- there is a `nav-error` control\n- `#s-error` exists\n"
        controls = [gate_a.InteractiveControl("nav-error", "s-error")]
        assert gate_a.unpinned_interactive_controls(contract, controls) == controls

    def test_substring_of_another_id_does_not_falsely_pin(self) -> None:
        """#3131 review: a naive `token in line` check would read a control
        id that happens to be a substring of another (longer) id as
        "mentioned" — e.g. `err` inside `nav-error` — and silently
        under-report a real gap. `nav-error`/`s-error` are pinned together
        here, but `err`/`error` (neither of which is really named on its
        own) must NOT be read as pinned just because they are substrings of
        what IS on the line."""
        contract = "- clicking `nav-error` makes `#s-error` the only visible screen\n"
        real = gate_a.InteractiveControl("nav-error", "s-error")
        fake = gate_a.InteractiveControl("err", "error")
        assert gate_a.unpinned_interactive_controls(contract, [real]) == []
        assert gate_a.unpinned_interactive_controls(contract, [fake]) == [fake]


class TestInteractiveContractGaps:
    def test_static_mock_with_no_controls_yields_no_gaps(self) -> None:
        mocks = {"screen.html": "<p>static picture, no links</p>"}
        assert gate_a.interactive_contract_gaps(mocks, "# Contract\n") == {}

    def test_fully_pinned_walkthrough_yields_no_gaps(self) -> None:
        mocks = {
            "walkthrough.html": '<a id="nav-error" href="#s-error">Error</a>'
        }
        contract = "- clicking `nav-error` makes `#s-error` visible\n"
        assert gate_a.interactive_contract_gaps(mocks, contract) == {}

    def test_only_files_with_gaps_appear_in_the_result(self) -> None:
        mocks = {
            "pinned.html": '<a id="nav-ok" href="#s-ok">Ok</a>',
            "gap.html": '<a id="nav-error" href="#s-error">Error</a>',
        }
        contract = "- clicking `nav-ok` makes `#s-ok` visible\n"
        gaps = gate_a.interactive_contract_gaps(mocks, contract)
        assert list(gaps.keys()) == ["gap.html"]
        assert gaps["gap.html"] == [
            gate_a.InteractiveControl("nav-error", "s-error")
        ]


class TestSummariseInteractiveGaps:
    def test_names_the_file_control_and_destination(self) -> None:
        gaps = {
            "walkthrough.html": [gate_a.InteractiveControl("nav-error", "s-error")]
        }
        lines = gate_a.summarise_interactive_gaps(gaps)
        assert len(lines) == 1
        assert "walkthrough.html" in lines[0]
        assert "nav-error" in lines[0]
        assert "s-error" in lines[0]

    def test_no_gaps_yields_no_lines(self) -> None:
        assert gate_a.summarise_interactive_gaps({}) == []


# ── the park marker ─────────────────────────────────────────────────────────


class TestParkMarker:
    def test_roundtrip(self) -> None:
        m = gate_a.park_marker("coord-portal", 3, "abc123")
        assert gate_a.parse_park_marker(f"blah {m} blah") == (
            "coord-portal",
            3,
            "abc123",
        )

    def test_unrelated_prose_is_not_a_gate_a_refusal(self) -> None:
        assert gate_a.parse_park_marker("drive session died") is None
        assert gate_a.is_gate_a_refusal_reason(None) is False
        assert gate_a.is_gate_a_refusal_reason("CI running: checks pending") is False

    def test_fingerprint_changes_when_the_verdict_changes(self) -> None:
        none_fp = gate_a.approval_fingerprint(None)
        assert none_fp == gate_a.NO_VERDICT
        approved = gate_a.approval_fingerprint(_approved(CONTRACT_V1))
        changes = gate_a.approval_fingerprint(
            _approved(CONTRACT_V1, verdict=gate_a.VERDICT_CHANGES)
        )
        assert len({none_fp, approved, changes}) == 3

    def test_fingerprint_is_stable_for_an_unchanged_verdict(self) -> None:
        assert gate_a.approval_fingerprint(
            _approved(CONTRACT_V1)
        ) == gate_a.approval_fingerprint(_approved(CONTRACT_V1))


# ── issue_oracle_ready: the refusal ─────────────────────────────────────────


def _fetch(mapping: dict[str, str]):
    def _f(repo_github: str, path: str, branch: str) -> str | None:
        return mapping.get(path)

    return _f


MANIFEST_PATH = "tests/acceptance/ms-37/manifest.yml"
CONTRACT_PATH = "tests/acceptance/ms-37/contract.md"


class TestIssueOracleReadyGateA:
    def _ready(self, *, manifest: str, contract: str | None, approval):
        files = {MANIFEST_PATH: manifest}
        if contract is not None:
            files[CONTRACT_PATH] = contract
        return issue_oracle_ready(
            _cfg().repo("api"),
            _cfg(),
            37,
            1118,
            file_exists=lambda *a: True,
            fetch_manifest=_fetch(files),
            fetch_gate_a_approval=lambda *a: approval,
        )

    def test_refuses_when_contract_has_no_recorded_verdict(self) -> None:
        r = self._ready(
            manifest="tests:\n  ms37::a: 1118\n", contract=CONTRACT_V1, approval=None
        )
        assert r.applies is True
        assert r.has_slice is True  # the slice gate is satisfied...
        assert r.gate_a_state == gate_a.STATE_MISSING
        assert r.reason is not None  # ...and it still refuses
        assert "coord gate-a --approved api" in r.reason

    def test_proceeds_once_a_verdict_is_recorded(self) -> None:
        r = self._ready(
            manifest="tests:\n  ms37::a: 1118\n",
            contract=CONTRACT_V1,
            approval=_approved(CONTRACT_V1),
        )
        assert r.reason is None
        assert r.gate_a_state == gate_a.STATE_APPROVED

    def test_amended_contract_refuses_again(self) -> None:
        r = self._ready(
            manifest="tests:\n  ms37::a: 1118\n",
            contract=CONTRACT_V2,
            approval=_approved(CONTRACT_V1),
        )
        assert r.gate_a_state == gate_a.STATE_STALE
        assert r.reason is not None

    def test_gate_a_refusal_wins_over_the_slice_refusal(self) -> None:
        """Both gates are unsatisfied; the human one is reported.

        Gate A is the cheap moment to change direction — telling the
        operator to author a slice against a contract nobody approved is
        exactly the sequence that burned ~$2.70 on coord-portal ms-2.
        """
        r = self._ready(manifest="", contract=CONTRACT_V1, approval=None)
        assert r.has_slice is False
        assert "no recorded human sign-off" in r.reason
        assert "coord acceptance author" not in r.reason

    def test_issue_level_exempt_does_not_bypass_the_human_gate(self) -> None:
        """`exempt:` says "this ISSUE doesn't consume the sealed suite" —
        it says nothing about whether a human read the milestone's
        contract, which every sibling issue is built against."""
        r = self._ready(
            manifest="exempt: [1118]\n", contract=CONTRACT_V1, approval=None
        )
        assert r.reason is not None
        assert r.gate_a_state == gate_a.STATE_MISSING

    def test_declared_milestone_opt_out_bypasses_it(self) -> None:
        r = self._ready(
            manifest=(
                "tests:\n  ms37::a: 1118\n"
                "gate_a:\n  exempt: true\n  reason: no user-visible surface\n"
            ),
            contract=CONTRACT_V1,
            approval=None,
        )
        assert r.reason is None
        assert r.gate_a_state == gate_a.STATE_EXEMPT

    def test_no_driver_configured_is_still_a_no_op(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[],
        )
        r = issue_oracle_ready(
            cfg.repo("api"), cfg, 37, 1118, file_exists=lambda *a: True
        )
        assert r.applies is False
        assert r.reason is None
        assert r.gate_a_state == ""

    def test_missing_contract_is_still_gate_a_status_s_refusal(self) -> None:
        """Contract absent entirely => `gate_a_status` already refuses with
        its own message; this gate must not double-block."""
        r = issue_oracle_ready(
            _cfg().repo("api"),
            _cfg(),
            37,
            1118,
            file_exists=lambda *a: False,
        )
        assert r.applies is False
        assert r.reason is None


# ── gate_a_signoff_status: the milestone-level wrapper ──────────────────────


class TestGateASignoffStatus:
    """The seam `coord acceptance author` gates on — the path that actually
    burned money on coord-portal ms-2 (a sealed slice authored against an
    unapproved contract)."""

    def _status(self, *, contract: str | None, approval):
        from coord.milestone_dispatch import gate_a_signoff_status

        files = {MANIFEST_PATH: "tests:\n  ms37::a: 1118\n"}
        if contract is not None:
            files[CONTRACT_PATH] = contract
        return gate_a_signoff_status(
            _cfg().repo("api"),
            _cfg(),
            37,
            fetch_manifest=_fetch(files),
            fetch_gate_a_approval=lambda *a: approval,
        )

    def test_refuses_without_a_verdict(self) -> None:
        reason = self._status(contract=CONTRACT_V1, approval=None)
        assert reason is not None
        assert "coord gate-a --approved api" in reason

    def test_passes_with_a_matching_verdict(self) -> None:
        assert self._status(
            contract=CONTRACT_V1, approval=_approved(CONTRACT_V1)
        ) is None

    def test_refuses_after_an_amend(self) -> None:
        assert self._status(
            contract=CONTRACT_V2, approval=_approved(CONTRACT_V1)
        ) is not None

    def test_no_contract_is_gate_a_status_s_refusal_not_this_one(self) -> None:
        assert self._status(contract=None, approval=None) is None

    def test_no_driver_is_a_no_op(self) -> None:
        from coord.milestone_dispatch import gate_a_signoff_status

        cfg = Config(repos=[Repo(name="api", github="acme/api")], machines=[])
        assert gate_a_signoff_status(cfg.repo("api"), cfg, 37) is None

    def test_contract_is_fetched_once(self) -> None:
        """The existence probe and the content read share one memoised
        fetch — otherwise every call pulls contract.md twice over `gh`."""
        from coord.milestone_dispatch import gate_a_signoff_status

        calls: list[str] = []

        def _f(repo_github: str, path: str, branch: str) -> str | None:
            calls.append(path)
            if path == CONTRACT_PATH:
                return CONTRACT_V1
            if path == MANIFEST_PATH:
                return "tests:\n  ms37::a: 1118\n"
            return None

        gate_a_signoff_status(
            _cfg().repo("api"),
            _cfg(),
            37,
            fetch_manifest=_f,
            fetch_gate_a_approval=lambda *a: _approved(CONTRACT_V1),
        )
        assert calls.count(CONTRACT_PATH) == 1


# ── the manifest opt-out ────────────────────────────────────────────────────


class TestManifestGateAKey:
    def test_absent_key_leaves_the_gate_on(self) -> None:
        from coord.acceptance import parse_manifest_text

        assert parse_manifest_text("tests: {a: 1}\n").gate_a_exempt is False

    def test_block_form(self) -> None:
        from coord.acceptance import parse_manifest_text

        data = parse_manifest_text("gate_a:\n  exempt: true\n  reason: internal\n")
        assert data.gate_a_exempt is True
        assert data.gate_a_exempt_reason == "internal"

    def test_shorthand_bool_form(self) -> None:
        from coord.acceptance import parse_manifest_text

        assert parse_manifest_text("gate_a: true\n").gate_a_exempt is True

    def test_garbage_value_leaves_the_gate_on(self) -> None:
        from coord.acceptance import parse_manifest_text

        assert parse_manifest_text("gate_a: [1, 2]\n").gate_a_exempt is False


# ── persistence + the daemon seam ───────────────────────────────────────────


class TestPersistence:
    def test_upsert_replaces_the_milestones_verdict(self, coord_db) -> None:
        from coord.state import list_gate_a_approvals, save_gate_a_approval

        save_gate_a_approval(_approved(CONTRACT_V1, verdict="changes"))
        save_gate_a_approval(_approved(CONTRACT_V2))
        rows = list_gate_a_approvals()
        assert len(rows) == 1
        assert rows[0]["verdict"] == "approved"
        assert rows[0]["contract_sha"] == gate_a.contract_digest(CONTRACT_V2)

    def test_verdicts_for_different_milestones_coexist(self, coord_db) -> None:
        from coord.state import get_gate_a_approval, save_gate_a_approval

        save_gate_a_approval(_approved(CONTRACT_V1))
        other = _approved(CONTRACT_V1)
        other["milestone_number"] = 38
        save_gate_a_approval(other)
        assert get_gate_a_approval(repo_name="api", milestone_number=37) is not None
        assert get_gate_a_approval(repo_name="api", milestone_number=38) is not None
        assert get_gate_a_approval(repo_name="api", milestone_number=99) is None

    def test_record_without_a_key_is_rejected(self, coord_db) -> None:
        from coord.state import _save_gate_a_approval_local

        with pytest.raises(ValueError):
            _save_gate_a_approval_local({"verdict": "approved"})

    def test_write_routes_to_the_daemon_when_configured(self, monkeypatch) -> None:
        """A thin client must not write a verdict into a local DB nobody
        reads — same posture as `save_milestone_gate` (#1929)."""
        from coord import client as cc
        from coord import state

        monkeypatch.setattr(
            cc,
            "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://d:7435"),
        )
        captured: dict = {}
        monkeypatch.setattr(
            cc,
            "post_record",
            lambda svc, path, payload, **kw: captured.update(
                path=path, payload=payload
            )
            or {"ok": True},
        )
        record = _approved(CONTRACT_V1)
        state.save_gate_a_approval(record)
        assert captured["path"] == "/gate-a-approval"
        assert captured["payload"] == {"record": record}
        assert state.list_gate_a_approvals() == []  # routed → no local write

    def test_unreachable_daemon_reads_as_no_verdict(self, monkeypatch) -> None:
        """Fails CLOSED: "couldn't ask" collapsing to "no verdict" makes the
        guard refuse, which is the safe direction for this gate."""
        import httpx

        from coord import client as cc

        def _boom(*a, **k):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(cc.httpx, "get", _boom)
        assert (
            cc.fetch_gate_a_approval(cc.ServiceConfig("http://d:7435"), "api", 37)
            is None
        )


def test_daemon_gate_a_approval_endpoints(tmp_path) -> None:
    """#2063: POST/GET `/gate-a-approval` — the thin-client seam, mirroring
    `/milestone-gate`."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord import db, state
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema
    from coord.serve_app import build_app

    cfg_path = tmp_path / "coordinator-gate-a.yml"
    cfg_path.write_text(
        "repos:\n  - name: api\n    github: acme/api\n\n"
        "machines:\n  - name: laptop\n    host: laptop.tailnet\n"
        "    repos: [api]\n    repo_paths:\n      api: /tmp/api\n"
    )
    db_path = tmp_path / "board.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    db.override_connection(conn)

    record = _approved(CONTRACT_V1)
    app = build_app(SqliteStore(db_path), load_config(cfg_path))
    with TestClient(app) as cli:
        missing = cli.get(
            "/gate-a-approval", params={"repo_name": "api", "milestone_number": 37}
        )
        ok = cli.post("/gate-a-approval", json={"record": record})
        bad = cli.post("/gate-a-approval", json={"record": "not-an-object"})
        found = cli.get(
            "/gate-a-approval", params={"repo_name": "api", "milestone_number": 37}
        )
        other = cli.get(
            "/gate-a-approval", params={"repo_name": "api", "milestone_number": 99}
        )
        bad_params = cli.get("/gate-a-approval", params={"repo_name": "api"})

    assert missing.json() == {"entries": []}
    assert ok.status_code == 200
    assert bad.status_code == 400
    assert found.json()["entries"] == [record]
    assert other.json() == {"entries": []}
    assert bad_params.status_code == 400
    assert (
        state._get_gate_a_approval_local(repo_name="api", milestone_number=37)
        == record
    )


# ── the drive-queue disposition (#2040: park, never terminal `blocked`) ─────


class TestDriveQueueParksNotBlocks:
    def _entry(self, **kw):
        from coord.drive_queue import QueueEntry

        base = dict(
            repo="api",
            issue=1118,
            position=1,
            state="running",
            attempts=0,
            session_name="drive-api-1118",
            launched_at=0.0,
        )
        base.update(kw)
        return QueueEntry(**base)

    def _board(self):
        from coord.drive_queue import build_board_view

        return build_board_view({"active": [], "completed": []}, [])

    def test_gate_a_refusal_parks_without_spending_an_attempt(self) -> None:
        from coord.drive_queue import STATE_PARKED, _reconcile_running

        entry = self._entry()
        reason = (
            "drive exited for api#1118: Gate A has no recorded human sign-off "
            f"for ms-37. {gate_a.park_marker('api', 37)} (exit_code=5)"
        )
        reconcile, blocked = _reconcile_running(
            entry,
            self._board(),
            max_attempts=2,
            now=10_000.0,
            exit_reasons={entry.key: reason},
            exit_refused={entry.key: True},
        )
        assert blocked is None, "a Gate-A refusal must never escalate"
        assert reconcile.outcome == "parked"
        assert reconcile.updates["state"] == STATE_PARKED
        assert "attempts" not in reconcile.updates

    def test_an_ordinary_refusal_still_blocks(self) -> None:
        from coord.drive_queue import STATE_BLOCKED, _reconcile_running

        entry = self._entry()
        reconcile, blocked = _reconcile_running(
            entry,
            self._board(),
            max_attempts=2,
            now=10_000.0,
            exit_reasons={entry.key: "machine lacks the capability"},
            exit_refused={entry.key: True},
        )
        assert blocked is not None
        assert reconcile.outcome == "refused"
        assert blocked.updates["state"] == STATE_BLOCKED

    def test_parked_entry_stays_parked_until_a_verdict_is_recorded(self) -> None:
        from coord.drive_queue import STATE_PARKED, plan_tick

        park_reason = f"parked ... {gate_a.park_marker('api', 37)}"
        entry = self._entry(state=STATE_PARKED, last_reason=park_reason)
        plan = plan_tick(
            [entry],
            self._board(),
            capacity=4,
            now=10_000.0,
            gate_a_pending={entry.key: True},
        )
        assert plan.launch is None
        assert not [r for r in plan.reconciles if r.outcome == "resumed"]

    def test_parked_entry_resumes_once_the_verdict_lands(self) -> None:
        from coord.drive_queue import STATE_PARKED, STATE_WAITING, plan_tick

        park_reason = f"parked ... {gate_a.park_marker('api', 37)}"
        entry = self._entry(state=STATE_PARKED, last_reason=park_reason)
        plan = plan_tick(
            [entry],
            self._board(),
            capacity=4,
            now=10_000.0,
            gate_a_pending={entry.key: False},
        )
        resumed = [r for r in plan.reconciles if r.outcome == "resumed"]
        assert len(resumed) == 1
        assert resumed[0].updates["state"] == STATE_WAITING
        assert "#2063" in resumed[0].reason

    def test_shell_resolver_keeps_an_unchanged_verdict_parked(
        self, coord_db
    ) -> None:
        """`_fetch_gate_a_pending` is the shell half: it re-reads the
        recorded verdict for the (repo, milestone) in the park marker."""
        from coord.commands.drive_queue import _fetch_gate_a_pending
        from coord.drive_queue import STATE_PARKED

        entry = self._entry(
            state=STATE_PARKED,
            last_reason=f"... {gate_a.park_marker('api', 37, gate_a.NO_VERDICT)}",
        )
        assert _fetch_gate_a_pending([entry]) == {entry.key: True}

    def test_shell_resolver_clears_once_a_verdict_is_recorded(
        self, coord_db
    ) -> None:
        from coord.commands.drive_queue import _fetch_gate_a_pending
        from coord.drive_queue import STATE_PARKED
        from coord.state import save_gate_a_approval

        entry = self._entry(
            state=STATE_PARKED,
            last_reason=f"... {gate_a.park_marker('api', 37, gate_a.NO_VERDICT)}",
        )
        save_gate_a_approval(_approved(CONTRACT_V1))
        assert _fetch_gate_a_pending([entry]) == {entry.key: False}

    def test_shell_resolver_re_parks_after_a_changes_verdict(
        self, coord_db
    ) -> None:
        """A `--changes` verdict refuses too. Once the entry has re-parked
        AGAINST that verdict, it must stay parked — otherwise the queue
        relaunches into the identical refusal every tick."""
        from coord.commands.drive_queue import _fetch_gate_a_pending
        from coord.drive_queue import STATE_PARKED
        from coord.state import save_gate_a_approval

        record = _approved(CONTRACT_V1, verdict=gate_a.VERDICT_CHANGES)
        save_gate_a_approval(record)
        fingerprint = gate_a.approval_fingerprint(record)
        entry = self._entry(
            state=STATE_PARKED,
            last_reason=f"... {gate_a.park_marker('api', 37, fingerprint)}",
        )
        assert _fetch_gate_a_pending([entry]) == {entry.key: True}

    def test_shell_resolver_ignores_non_gate_a_parks(self, coord_db) -> None:
        from coord.commands.drive_queue import _fetch_gate_a_pending
        from coord.drive_queue import STATE_PARKED

        entry = self._entry(
            state=STATE_PARKED, last_reason="CI running: checks pending"
        )
        assert _fetch_gate_a_pending([entry]) == {}

    def test_unresolvable_gate_a_park_stays_parked(self) -> None:
        """Fail closed: no entry in `gate_a_pending` (the shell could not
        resolve it) must not be read as "cleared"."""
        from coord.drive_queue import STATE_PARKED, plan_tick

        park_reason = f"parked ... {gate_a.park_marker('api', 37)}"
        entry = self._entry(state=STATE_PARKED, last_reason=park_reason)
        plan = plan_tick(
            [entry], self._board(), capacity=4, now=10_000.0, gate_a_pending={}
        )
        assert not [r for r in plan.reconciles if r.outcome == "resumed"]
