"""Tests for #4147: route a Test-failure fix leg to a machine that actually
has the failing leg's capability, instead of blindly staying on whatever
machine the original work leg happened to land on.

Covers:
- `coord.dispatch.select_fix_machine`'s new *failing_capability_sets*
  scoring, including the quadraui#2876 regression shape.
- `coord.smoke.fanout_leg_verdicts`/`failing_capability_sets` reading a
  seeded #3182 fan-out back off the board.
- `coord.models.Machine.covers_capabilities`.
- `coord.commands.smoke.dispatch_capability_leg`/`wait_for_leg_verdict`,
  the `coord smoke run-leg` door a fix worker uses to re-verify a
  capability it cannot reproduce locally.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from coord.dispatch import (
    FixMachineSelection,
    describe_fix_machine_failure,
    dispatch,
    fix_machine_capability_note,
    select_fix_machine,
)
from coord.models import Assignment, Machine, Proposal, Repo
from coord.network import StatusResult


# ── Fixtures ────────────────────────────────────────────────────────────────


def _status_fetcher(reachable: set[str]):
    """Fake `status_fetcher`: online for every machine in *reachable*,
    unreachable (network timeout) for everyone else — same shape as
    `tests/test_dispatch.py`'s identical helper."""

    def _fetch(machine, timeout=None):  # noqa: ARG001
        if machine.name in reachable:
            return StatusResult(data={"assignments": []})
        return StatusResult(error="timeout")

    return _fetch


@pytest.fixture
def dell64() -> Machine:
    return Machine(
        name="dell64", host="dell64.tail", capabilities=["gtk", "windows"],
        repos=["quadraui"], repo_paths={"quadraui": "/work/quadraui"},
    )


@pytest.fixture
def macmini() -> Machine:
    return Machine(
        name="macmini", host="macmini.tail", capabilities=["macos"],
        repos=["quadraui"], repo_paths={"quadraui": "/work/quadraui"},
    )


# ── Machine.covers_capabilities ─────────────────────────────────────────────


class TestCoversCapabilities:
    def test_full_match(self, dell64: Machine) -> None:
        assert dell64.covers_capabilities(("gtk", "windows")) is True

    def test_partial_match_fails(self, dell64: Machine) -> None:
        assert dell64.covers_capabilities(("gtk", "macos")) is False

    def test_empty_is_vacuously_covered(self, dell64: Machine) -> None:
        assert dell64.covers_capabilities(()) is True


# ── select_fix_machine: capability-aware routing (#4147) ────────────────────


class TestSelectFixMachineCapabilityRouting:
    def test_backward_compatible_when_no_failing_sets_given(
        self, dell64: Machine, macmini: Machine,
    ) -> None:
        """Every caller that predates #4147 passes nothing — behaviour must
        be byte-for-byte the pre-#4147 "original machine first" order."""
        selection = select_fix_machine(
            original_machine_name="dell64",
            repo_name="quadraui",
            machines=[dell64, macmini],
            status_fetcher=_status_fetcher({"dell64", "macmini"}),
        )
        assert selection.machine is not None
        assert selection.machine.name == "dell64"
        assert selection.uncovered_failing_capabilities == ()

    def test_2876_shape_routes_the_failing_macos_leg_to_macmini(
        self, dell64: Machine, macmini: Machine,
    ) -> None:
        """quadraui#2876: original machine dell64 (gtk+windows), failing
        set [macos], passing set [gtk+windows] — the fix must land on
        macmini, the only machine that can even reproduce the failure."""
        selection = select_fix_machine(
            original_machine_name="dell64",
            repo_name="quadraui",
            machines=[dell64, macmini],
            status_fetcher=_status_fetcher({"dell64", "macmini"}),
            failing_capability_sets=[("macos",)],
        )
        assert selection.machine is not None
        assert selection.machine.name == "macmini"
        assert selection.uncovered_failing_capabilities == ()

    def test_prefers_original_when_it_already_covers_the_failing_leg(
        self, dell64: Machine, macmini: Machine,
    ) -> None:
        """A windows-only failure: dell64 already covers it, so there is
        no reason to move the fix off the machine with the warm cache."""
        selection = select_fix_machine(
            original_machine_name="dell64",
            repo_name="quadraui",
            machines=[dell64, macmini],
            status_fetcher=_status_fetcher({"dell64", "macmini"}),
            failing_capability_sets=[("windows",)],
        )
        assert selection.machine is not None
        assert selection.machine.name == "dell64"
        assert selection.uncovered_failing_capabilities == ()

    def test_no_machine_covers_all_failing_sets_picks_most_covering_and_names_the_rest(
        self, dell64: Machine, macmini: Machine,
    ) -> None:
        """macos and windows both failing, no single machine declares
        both — pick whichever covers the most, and report what it still
        can't verify rather than silently dropping it."""
        winbox = Machine(
            name="winbox", host="winbox.tail", capabilities=["windows"],
            repos=["quadraui"], repo_paths={"quadraui": "/w"},
        )
        selection = select_fix_machine(
            original_machine_name="dell64",
            repo_name="quadraui",
            machines=[dell64, macmini, winbox],
            status_fetcher=_status_fetcher({"dell64", "macmini", "winbox"}),
            failing_capability_sets=[("macos",), ("windows",)],
        )
        assert selection.machine is not None
        # dell64 and winbox both cover exactly one of the two failing legs
        # (windows); dell64 wins the tie as the original.
        assert selection.machine.name == "dell64"
        assert selection.uncovered_failing_capabilities == ("macos",)

    def test_best_covering_machine_is_tried_before_an_unreachable_original(
        self, dell64: Machine, macmini: Machine,
    ) -> None:
        """Original is down; the capability-aware ordering must still try
        the macOS-capable machine, not fall through machines-declaration
        order blind to coverage — macmini is tried (and succeeds) before
        the unreachable dell64 is ever probed."""
        selection = select_fix_machine(
            original_machine_name="dell64",
            repo_name="quadraui",
            machines=[dell64, macmini],
            status_fetcher=_status_fetcher({"macmini"}),  # dell64 is down
            failing_capability_sets=[("macos",)],
        )
        assert selection.machine is not None
        assert selection.machine.name == "macmini"
        assert selection.tried == []  # macmini won on the first try

    def test_falls_back_to_a_lower_covering_machine_when_the_best_is_down(
        self, dell64: Machine, macmini: Machine,
    ) -> None:
        """Both the original AND the best-covering machine are down —
        falls through to whatever's left, naming both refusals."""
        selection = select_fix_machine(
            original_machine_name="dell64",
            repo_name="quadraui",
            machines=[dell64, macmini],
            status_fetcher=_status_fetcher(set()),  # nobody answers
            failing_capability_sets=[("macos",)],
        )
        assert selection.machine is None
        assert ("macmini", "timeout") in selection.tried
        assert ("dell64", "timeout") in selection.tried

    def test_override_machine_bypasses_capability_routing(
        self, dell64: Machine, macmini: Machine,
    ) -> None:
        """`coord fix --machine` is an explicit assertion — no capability
        reordering, but the uncovered set is still reported."""
        selection = select_fix_machine(
            original_machine_name="dell64",
            repo_name="quadraui",
            machines=[dell64, macmini],
            override_machine_name="dell64",
            status_fetcher=_status_fetcher({"dell64", "macmini"}),
            failing_capability_sets=[("macos",)],
        )
        assert selection.machine is not None
        assert selection.machine.name == "dell64"
        assert selection.uncovered_failing_capabilities == ("macos",)

    def test_describe_fix_machine_failure_still_works_with_the_new_field(
        self, dell64: Machine,
    ) -> None:
        selection = FixMachineSelection(None, [("dell64", "timeout")])
        detail = describe_fix_machine_failure("dell64", selection)
        assert "dell64" in detail
        assert "timeout" in detail


class TestFixMachineCapabilityNote:
    def test_names_the_uncovered_capability_and_the_rerun_command(self) -> None:
        note = fix_machine_capability_note("dell64", ("macos",))
        assert "dell64" in note
        assert "macos" in note
        assert "coord smoke run-leg" in note
        assert "UNVERIFIED: macos" in note


# ── dispatch(): the original-work-dispatch fallback is logged (#4147) ───────


class TestDispatchLogsUnmetCapabilityFallback:
    @patch("coord.dispatch.httpx.post")
    def test_logs_a_warning_when_no_machine_fully_covers_the_diff(
        self, mock_post: MagicMock, dell64: Machine, macmini: Machine, caplog,
    ) -> None:
        """#4147 acceptance: `route_work_by_capability`'s fallback (no
        single configured machine covers every matched capability) must be
        LOGGED, not silently folded into the briefing note alone."""
        from coord.config import Config, SmokeRule, SmokeTestsConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        repo = Repo(name="quadraui", github="acme/quadraui", default_branch="main")
        config = Config(
            repos=[repo], machines=[dell64, macmini],
            smoke_tests=SmokeTestsConfig(capability_rules=[
                SmokeRule(files=["gtk/"], requires=["gtk"]),
                SmokeRule(files=["macos/"], requires=["macos"]),
            ]),
        )
        proposal = Proposal(
            id=1, machine_name="dell64", repo_name="quadraui",
            issue_number=2876, issue_title="group-B cleanup",
            rationale="best fit",
            files_likely=["gtk/popup.c", "macos/backend.rs"],
            briefing="do the thing",
        )

        with caplog.at_level(logging.WARNING, logger="coord.dispatch"):
            dispatch(proposal, config)

        assert any(
            "#4147" in r.message and "unmet" in r.message.lower()
            for r in caplog.records
        )


# ── coord.smoke: reading a fan-out's per-leg verdicts (#4147) ───────────────


def _seed_fanout_parent(*, parent_id: str = "w-fanout") -> None:
    from coord.state import _record_dispatched_assignment_local

    work = Assignment(
        assignment_id=parent_id, machine_name="dell64", repo_name="quadraui",
        issue_number=2876, issue_title="group-B cleanup", type="work",
        status="done", branch="issue-2876",
    )
    _record_dispatched_assignment_local(assignment=work, repo_github="acme/quadraui")


def _seed_leg(*, leg_id: str, state: str | None, reason: str | None) -> None:
    from coord.state import _record_dispatched_assignment_local, record_test_verdict

    leg = Assignment(
        assignment_id=leg_id, machine_name="dell64", repo_name="quadraui",
        issue_number=2876, issue_title="[smoke:macos] leg", type="smoke",
        status="done" if state not in (None, "running") else "running",
        branch="issue-2876", review_of_assignment_id="w-fanout",
    )
    _record_dispatched_assignment_local(assignment=leg, repo_github="acme/quadraui")
    if state is not None or reason is not None:
        record_test_verdict(assignment_id=leg_id, test_state=state, test_reason=reason)


class TestFanoutLegVerdicts:
    def test_2876_shape_reports_one_failing_and_one_passing_leg(self, coord_db) -> None:
        from coord.state import record_test_verdict

        _seed_fanout_parent()
        _seed_leg(leg_id="leg-gtkwin", state="passed", reason="ok")
        _seed_leg(leg_id="leg-macos", state="failed", reason="focus ring clipped")
        record_test_verdict(
            assignment_id="w-fanout", test_state="running",
            test_reason=(
                "[[smoke-fanout:leg-gtkwin=gtk+windows,leg-macos=macos]]\n"
                "Test stage running across 2 capability-partition leg(s) "
                "(#3182): [gtk+windows]; [macos]."
            ),
        )

        from coord.smoke import failing_capability_sets, fanout_leg_verdicts

        verdicts = fanout_leg_verdicts("w-fanout")
        assert {v.leg_id: v.state for v in verdicts} == {
            "leg-gtkwin": "passed", "leg-macos": "failed",
        }
        assert failing_capability_sets("w-fanout") == (("macos",),)

    def test_non_fanout_row_reports_nothing(self, coord_db) -> None:
        _seed_fanout_parent(parent_id="w-plain")

        from coord.smoke import failing_capability_sets, fanout_leg_verdicts

        assert fanout_leg_verdicts("w-plain") == []
        assert failing_capability_sets("w-plain") == ()


# ── coord smoke run-leg (#4147) ──────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, json_body: dict) -> None:
        self._json = json_body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._json


class _FakeHttpClient:
    def __init__(self, response_id: str = "leg-123") -> None:
        self.response_id = response_id
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, json, timeout=None):  # noqa: ARG002
        self.posts.append((url, json))
        return _FakeResponse({"id": self.response_id})


@pytest.fixture
def quadraui_repo() -> Repo:
    return Repo(
        name="quadraui", github="acme/quadraui", default_branch="main",
        test_command="cargo test",
    )


@pytest.fixture
def run_leg_config(quadraui_repo: Repo, dell64: Machine, macmini: Machine):
    from coord.config import Config

    return Config(repos=[quadraui_repo], machines=[dell64, macmini])


class TestDispatchCapabilityLeg:
    def test_routes_to_the_capable_machine_and_records_the_leg(
        self, coord_db, run_leg_config, monkeypatch,
    ) -> None:
        from coord.commands.smoke import dispatch_capability_leg

        monkeypatch.setattr(
            "coord.network.probe_reachable", lambda m, status_fetcher=None, timeout=None: (True, ""),
        )
        fake_client = _FakeHttpClient(response_id="leg-macos-1")
        leg, error = dispatch_capability_leg(
            run_leg_config, repo_name="quadraui", branch="issue-2876",
            capability="macos", http_client=fake_client,
        )

        assert error is None
        assert leg is not None
        assert leg.machine_name == "macmini"
        assert leg.assignment_id == "leg-macos-1"
        assert len(fake_client.posts) == 1
        url, payload = fake_client.posts[0]
        assert "macmini" in url
        assert payload["type"] == "smoke"
        assert payload["target_branch"] == "issue-2876"

        from coord.state import load_assignment_test_state

        # Recorded on the board — a `--wait` poll can find it.
        assert load_assignment_test_state("leg-macos-1") is None  # still running

    def test_no_machine_declares_the_capability(
        self, coord_db, run_leg_config, monkeypatch,
    ) -> None:
        from coord.commands.smoke import dispatch_capability_leg

        monkeypatch.setattr(
            "coord.network.probe_reachable", lambda m, status_fetcher=None, timeout=None: (True, ""),
        )
        leg, error = dispatch_capability_leg(
            run_leg_config, repo_name="quadraui", branch="issue-2876",
            capability="browser", http_client=_FakeHttpClient(),
        )
        assert leg is None
        assert error is not None
        assert "browser" in error


class TestWaitForLegVerdict:
    def test_returns_terminal_state_once_recorded(self, coord_db) -> None:
        from coord.commands.smoke import wait_for_leg_verdict
        from coord.state import _record_dispatched_assignment_local, record_test_verdict

        leg = Assignment(
            assignment_id="leg-wait", machine_name="macmini", repo_name="quadraui",
            issue_number=0, issue_title="manual leg", type="smoke", status="running",
        )
        _record_dispatched_assignment_local(assignment=leg, repo_github="acme/quadraui")
        record_test_verdict(
            assignment_id="leg-wait", test_state="failed", test_reason="clipped focus ring",
        )

        calls: list[int] = []
        state, reason = wait_for_leg_verdict(
            "leg-wait", timeout_s=10, poll_interval_s=1,
            sleep=lambda s: calls.append(s),
        )
        assert state == "failed"
        assert reason == "clipped focus ring"
        assert calls == []  # already terminal on the first read, never slept

    def test_times_out_when_never_terminal(self, coord_db) -> None:
        from coord.commands.smoke import wait_for_leg_verdict
        from coord.state import _record_dispatched_assignment_local

        leg = Assignment(
            assignment_id="leg-stuck", machine_name="macmini", repo_name="quadraui",
            issue_number=0, issue_title="manual leg", type="smoke", status="running",
        )
        _record_dispatched_assignment_local(assignment=leg, repo_github="acme/quadraui")

        ticks = {"n": 0}

        def fake_sleep(_s):
            ticks["n"] += 1

        # Use a monotonic-looking fake clock via a tiny poll budget instead
        # of real wall-clock sleeps.
        state, reason = wait_for_leg_verdict(
            "leg-stuck", timeout_s=0, poll_interval_s=1, sleep=fake_sleep,
        )
        assert state is None
        assert reason is None
