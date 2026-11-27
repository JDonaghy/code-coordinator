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


class TestUnsatisfiedCapabilitiesRecorded:
    """#4147 review: a log line alone is not readable by the dashboard or
    #4122's stats — the fallback must be RECORDED on the assignment too."""

    @patch("coord.dispatch.httpx.post")
    def test_dispatch_attaches_it_and_record_dispatched_persists_it(
        self, mock_post: MagicMock, dell64: Machine, macmini: Machine, coord_db,
    ) -> None:
        from coord.config import Config, SmokeRule, SmokeTestsConfig
        from coord.state import build_board, record_dispatched

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "work-2876"}
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

        # `dispatch()` attaches the gap to its own response dict (same
        # "attach, let the caller persist" discipline as `_provider_name`
        # for #324) — never silently dropped.
        response = dispatch(proposal, config)
        assert response.get("_unsatisfied_capabilities") == ["macos"]

        # `coord approve`/`coord assign`'s own call thread it straight into
        # `record_dispatched` — verify the WRITE path end to end rather than
        # constructing an `Assignment` by hand.
        record_dispatched(
            assignment_id="work-2876",
            proposal=proposal,
            repo_github=repo.github,
            unsatisfied_capabilities=response.get("_unsatisfied_capabilities"),
        )

        recorded = build_board().find_by_id("work-2876")
        assert recorded is not None
        assert recorded.unsatisfied_capabilities == ["macos"]

    def test_defaults_to_empty_when_nothing_is_unmet(
        self, dell64: Machine, coord_db,
    ) -> None:
        """Every dispatch that predates #4147, and every dispatch where the
        chosen machine covers everything, must round-trip to `[]` — never
        `None`, which would read as a missing/unknown value rather than
        "nothing unmet"."""
        from coord.state import build_board, record_dispatched

        repo = Repo(name="quadraui", github="acme/quadraui", default_branch="main")
        proposal = Proposal(
            id=2, machine_name="dell64", repo_name="quadraui",
            issue_number=11, issue_title="plain fix",
            rationale="best fit", files_likely=["gtk/popup.c"],
            briefing="do the thing",
        )
        record_dispatched(
            assignment_id="work-plain", proposal=proposal, repo_github=repo.github,
        )

        recorded = build_board().find_by_id("work-plain")
        assert recorded is not None
        assert recorded.unsatisfied_capabilities == []


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
    def test_returns_terminal_state_once_recorded_by_the_real_notify_path(
        self, coord_db, tmp_path,
    ) -> None:
        """A `--wait` poll must resolve against whatever `coord.notify.
        post_transition` ACTUALLY writes for this leg's completion — not a
        hand-seeded `record_test_verdict` call standing in for it. See
        `TestStandaloneSmokeLegVerdictRecording` for the dedicated coverage
        of that recording path itself; this only proves the two are wired
        together end to end."""
        from coord.comments import EVENT_COMPLETION
        from coord.commands.smoke import wait_for_leg_verdict
        from coord.notify import Transition, post_transition
        from coord.state import _record_dispatched_assignment_local

        leg = Assignment(
            assignment_id="leg-wait", machine_name="macmini", repo_name="quadraui",
            issue_number=0, issue_title="manual leg", type="smoke", status="running",
        )
        _record_dispatched_assignment_local(assignment=leg, repo_github="acme/quadraui")

        log_path = tmp_path / "leg-wait.log"
        log_path.write_text("SMOKE: fail — clipped focus ring\n", encoding="utf-8")
        transition = Transition(
            assignment_id="leg-wait", machine_name="macmini", repo_name="quadraui",
            issue_number=0, event=EVENT_COMPLETION, exit_code=0,
        )
        record = {"repo_github": "acme/quadraui", "type": "smoke"}
        entry = {
            "started_at": 1000.0, "finished_at": 1010.0,
            "branch": "issue-2876", "log_path": str(log_path),
        }
        with (
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        calls: list[int] = []
        state, reason = wait_for_leg_verdict(
            "leg-wait", timeout_s=10, poll_interval_s=1,
            sleep=lambda s: calls.append(s),
        )
        assert state == "failed"
        assert reason is not None and "clipped focus ring" in reason
        assert calls == []  # already terminal on the first read, never slept

    def test_times_out_when_never_terminal(self, coord_db, monkeypatch) -> None:
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

        # A fake, monotonically-increasing clock (one tick per call) so the
        # poll loop genuinely runs several iterations before the deadline —
        # `timeout_s=0` (the pre-fix version of this test) never calls
        # `sleep` at all and proves nothing about multi-poll behaviour.
        clock = {"t": 0.0}

        def fake_monotonic() -> float:
            clock["t"] += 1.0
            return clock["t"]

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        state, reason = wait_for_leg_verdict(
            "leg-stuck", timeout_s=5, poll_interval_s=1, sleep=fake_sleep,
        )
        assert state is None
        assert reason is None
        # Several polls happened (not zero, not merely one) before the fake
        # clock crossed the deadline — proves the loop actually polls
        # repeatedly rather than just checking once.
        assert ticks["n"] >= 2


# ── _record_standalone_smoke_leg_verdict / post_transition wiring (#4147) ────


def _standalone_leg(assignment_id: str) -> Assignment:
    return Assignment(
        assignment_id=assignment_id, machine_name="macmini", repo_name="quadraui",
        issue_number=0, issue_title="manual leg", type="smoke", status="running",
    )


def _post_standalone_transition(
    *, assignment_id: str, event: str, exit_code: int | None,
    log_text: str | None, tmp_path,
) -> None:
    from coord.notify import Transition, post_transition
    from coord.state import _record_dispatched_assignment_local

    _record_dispatched_assignment_local(
        assignment=_standalone_leg(assignment_id), repo_github="acme/quadraui",
    )
    entry = {"started_at": 1000.0, "finished_at": 1010.0, "branch": "issue-2876"}
    if log_text is not None:
        log_path = tmp_path / f"{assignment_id}.log"
        log_path.write_text(log_text, encoding="utf-8")
        entry["log_path"] = str(log_path)
    else:
        entry["log_path"] = None
        entry["error"] = "worker crashed unexpectedly (no classifiable reason)"
    transition = Transition(
        assignment_id=assignment_id, machine_name="macmini", repo_name="quadraui",
        issue_number=0, event=event, exit_code=exit_code,
    )
    # `review_of_assignment_id` deliberately absent — this is the one thing
    # that marks a `type="smoke"` row as a `coord smoke run-leg` standalone
    # leg rather than a #3182 fan-out child.
    record = {"repo_github": "acme/quadraui", "type": "smoke"}
    with (
        patch("coord.notify._capture_cost"),
        patch("coord.notify._capture_smoke_tests"),
        patch("coord.notify._capture_completion_summary"),
        patch("coord.notify._capture_claude_session_id"),
    ):
        post_transition(transition, record, entry)


class TestStandaloneSmokeLegVerdictRecording:
    """`coord smoke run-leg` dispatches a `type="smoke"` leg with no GitHub
    issue (`issue_number=0`) and no `review_of_assignment_id` parent — the
    `issue_number == 0` short-circuit in `coord.notify.post_transition`
    would otherwise return before any `smoke` branch runs, and the ordinary
    `smoke` branches further down both require a `review_of_assignment_id`
    this leg doesn't have, so `coord smoke run-leg --wait` depends entirely
    on `post_transition` recognizing this shape and recording a verdict onto
    the leg's own row. These drive the real recording path (`post_
    transition` itself), not a hand-seeded `record_test_verdict`."""

    def test_completion_with_pass_marker_records_passed(self, coord_db, tmp_path) -> None:
        from coord.state import load_assignment_test_state

        _post_standalone_transition(
            assignment_id="leg-pass", event="completion", exit_code=0,
            log_text="SMOKE: pass\n", tmp_path=tmp_path,
        )
        assert load_assignment_test_state("leg-pass") == "passed"

    def test_completion_with_fail_marker_records_failed_with_reason(
        self, coord_db, tmp_path,
    ) -> None:
        from coord.state import load_assignment_test_reason, load_assignment_test_state

        _post_standalone_transition(
            assignment_id="leg-fail", event="completion", exit_code=0,
            log_text="SMOKE: fail — macOS pixel test clipped\n", tmp_path=tmp_path,
        )
        assert load_assignment_test_state("leg-fail") == "failed"
        assert "macOS pixel test clipped" in (load_assignment_test_reason("leg-fail") or "")

    def test_completion_mute_clean_exit_records_no_verdict(self, coord_db, tmp_path) -> None:
        """A clean exit with no `SMOKE:` marker at all must never be read as
        a pass — the row is left with no terminal verdict so `--wait` times
        out instead of fabricating one."""
        from coord.state import load_assignment_test_state

        _post_standalone_transition(
            assignment_id="leg-mute", event="completion", exit_code=0,
            log_text="(worker said nothing resembling a verdict)\n", tmp_path=tmp_path,
        )
        assert load_assignment_test_state("leg-mute") in (None, "running")

    def test_worker_crash_with_nonzero_exit_records_failed(self, coord_db, tmp_path) -> None:
        """#1605 shape for a standalone leg: the WORKER died (EVENT_FAILURE)
        before ever printing a `SMOKE:` marker — still a `failed` verdict,
        never left `running` forever."""
        from coord.state import load_assignment_test_state

        _post_standalone_transition(
            assignment_id="leg-crash", event="failure", exit_code=1,
            log_text=None, tmp_path=tmp_path,
        )
        assert load_assignment_test_state("leg-crash") == "failed"
