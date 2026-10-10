"""Tests for environmental-vs-work failure classification (#1590).

The acceptance bullets from the issue, in order:

* a terminal ``result`` with ``api_error_status: 529`` classifies
  ``environmental``; a failing test suite classifies ``work``;
* N environmental failures on a node with zero work failures never reach
  BLOCKED (here: :func:`counts_against_work_budget` never charges them, which
  is the primitive the out-of-repo sequencer's budget consumes);
* a usage-limit kill parks the node and the relaunch is not attempted before
  ``reset_at``;
* after an outage, a relaunch is gated on a successful liveness probe;
* the surfaced reason distinguishes the two classes in text;
* regression: a genuine work failure still reaches the BLOCKED path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coord.failure_class import (
    DEFAULT_BACKOFF_CEILING_SECS,
    DEFAULT_USAGE_LIMIT_PARK_SECS,
    ENVIRONMENTAL,
    ENVIRONMENTAL_API_STATUSES,
    KIND_API_ERROR,
    KIND_INFRA,
    KIND_NETWORK,
    KIND_USAGE_LIMIT,
    KIND_WORK,
    WORK,
    FailureClassification,
    classify_failure,
    classify_log,
    classify_result_event,
    counts_against_work_budget,
    environmental_backoff_secs,
    gate_relaunch,
    parse_reset_at,
    plan_usage_limit_resume,
    probe_environment_liveness,
)
from coord.worker_events import UsageLimitKill, format_usage_limit_reason

CHICAGO = ZoneInfo("America/Chicago")


# ── fakes ───────────────────────────────────────────────────────────────────


@dataclass
class FakeLimits:
    """A ``coord.usage_limits.PlanLimits``-shaped stand-in for the probe."""

    status: str = "ok"
    error: str | None = None
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _alive_probe():
    return lambda: FakeLimits(status="ok")


def _down_probe(detail: str = "no recognisable usage bars in /usage output"):
    return lambda: FakeLimits(
        status="unknown",
        error=detail,
        raw='API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}',
    )


def _ndjson(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


# ── 1. the classification primitive ─────────────────────────────────────────


class TestClassifyApiError:
    def test_terminal_result_with_api_error_status_529_is_environmental(self) -> None:
        """The literal acceptance bullet: #1563's review died on 529."""
        c = classify_result_event(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "api_error_status": 529,
                "num_turns": 1,
                "total_cost_usd": 0.03,
            }
        )
        assert c.failure_class == ENVIRONMENTAL
        assert c.kind == KIND_API_ERROR
        assert c.api_status == 529
        assert not counts_against_work_budget(c)

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
    def test_every_allow_listed_status_is_environmental(self, status: int) -> None:
        c = classify_failure(api_error_status=status)
        assert c.failure_class == ENVIRONMENTAL
        assert c.kind == KIND_API_ERROR
        assert c.api_status == status

    @pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 422])
    def test_non_allow_listed_statuses_stay_work(self, status: int) -> None:
        """A 400/401/403 is our bug or our config — it must SURFACE, not retry
        forever behind an 'environmental' label."""
        assert status not in ENVIRONMENTAL_API_STATUSES
        c = classify_failure(api_error_status=status)
        assert c.failure_class == WORK

    def test_api_error_prose_in_error_result_is_environmental(self) -> None:
        c = classify_failure(
            is_error=True,
            result_text='API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
        )
        assert c.failure_class == ENVIRONMENTAL
        assert c.api_status == 529

    def test_overloaded_token_without_a_status_is_environmental(self) -> None:
        c = classify_failure(
            is_error=True, result_text='{"type":"overloaded_error"}'
        )
        assert c.failure_class == ENVIRONMENTAL
        assert c.kind == KIND_API_ERROR
        assert c.signal == "overloaded_error"

    def test_garbage_api_error_status_does_not_raise(self) -> None:
        c = classify_failure(api_error_status="not-a-number")  # type: ignore[arg-type]
        assert c.failure_class == WORK


class TestClassifyWorkFailure:
    def test_failing_test_suite_is_work(self) -> None:
        """The other half of the acceptance bullet."""
        c = classify_failure(
            failure_reason="tests failed: 3 failed, 1201 passed",
            is_error=True,
            result_text="pytest exited 1 — tests/test_board.py::test_cap FAILED",
        )
        assert c.failure_class == WORK
        assert c.kind == KIND_WORK
        assert counts_against_work_budget(c)

    @pytest.mark.parametrize(
        "reason",
        [
            "review requested changes: scope creep in coord/cli.py",
            "no commits produced on issue-1590-foo",
            "worker exited 1 without pushing",
            "STUCK: cannot find the sequencer",
            "acceptance suite red: 2 of 9 assertions failed",
        ],
    )
    def test_representative_work_failures_are_work(self, reason: str) -> None:
        c = classify_failure(failure_reason=reason, is_error=True)
        assert c.failure_class == WORK

    def test_no_evidence_at_all_defaults_to_work(self) -> None:
        """'We have no idea' must never be environmental — an unbounded retry
        on an unknown cause is strictly worse than surfacing it."""
        c = classify_failure()
        assert c.failure_class == WORK

    def test_is_error_alone_is_not_an_environmental_signal(self) -> None:
        c = classify_failure(is_error=True, result_text="something went wrong")
        assert c.failure_class == WORK

    def test_bare_529_in_work_prose_is_not_a_signal(self) -> None:
        """A tight allow-list, not a substring hunt: a test name or a line
        count that happens to contain 529 must not park the node."""
        c = classify_failure(
            failure_reason="tests failed: test_http_529_retry expected 3 got 529",
            is_error=True,
        )
        assert c.failure_class == WORK

    def test_generic_http_status_in_work_prose_is_not_a_signal(self) -> None:
        """A worker fixing an HTTP handler legitimately reports 5xx statuses as
        a TEST failure. Parking that node forever is worse than the bug this
        module fixes, so only `api_error_status` / `API Error: NNN` count."""
        for reason in (
            "tests failed: expected status: 200, got status: 503",
            'assertion failed: {"status":529} != {"status":200}',
            "smoke: GET /board returned HTTP 502",
        ):
            assert classify_failure(failure_reason=reason).failure_class == WORK

    def test_word_overloaded_in_prose_is_not_a_signal(self) -> None:
        c = classify_failure(
            failure_reason="the worker overloaded the board with 900 writes",
            is_error=True,
        )
        assert c.failure_class == WORK

    def test_result_text_is_ignored_when_the_result_was_not_an_error(self) -> None:
        """A worker that merely *discusses* an outage (this issue's own
        transcript) must not classify environmental."""
        prose = (
            "I implemented the api_error_status: 529 branch and the "
            "overloaded_error token match."
        )
        assert classify_failure(is_error=False, result_text=prose).failure_class == WORK
        assert classify_failure(result_text=prose).failure_class == WORK
        # ...but the same prose in a genuinely errored result does count.
        assert (
            classify_failure(is_error=True, result_text=prose).failure_class
            == ENVIRONMENTAL
        )


class TestClassifyUsageLimit:
    def test_stamped_failure_reason_is_environmental(self) -> None:
        reason = format_usage_limit_reason(
            UsageLimitKill(reset_at_raw="8:30pm (America/Chicago)", excerpt="")
        )
        c = classify_failure(failure_reason=reason)
        assert c.failure_class == ENVIRONMENTAL
        assert c.kind == KIND_USAGE_LIMIT
        assert c.reset_at_raw == "8:30pm (America/Chicago)"
        assert not counts_against_work_budget(c)

    def test_usage_limit_reason_channel(self) -> None:
        reason = format_usage_limit_reason(
            UsageLimitKill(reset_at_raw="1:30am (America/Chicago)", excerpt="")
        )
        c = classify_failure(usage_limit_reason=reason, failure_reason="failed")
        assert c.kind == KIND_USAGE_LIMIT

    def test_usage_limit_wins_over_a_concurrent_api_status(self) -> None:
        """The reset time is the only remediation that matters; a 529 seen in
        the same run must not downgrade it to a plain backoff."""
        reason = format_usage_limit_reason(
            UsageLimitKill(reset_at_raw="8:30pm (America/Chicago)", excerpt="")
        )
        c = classify_failure(failure_reason=reason, api_error_status=529)
        assert c.kind == KIND_USAGE_LIMIT

    def test_raw_cli_kill_message_in_error_result(self) -> None:
        c = classify_failure(
            is_error=True,
            result_text="You've hit your session limit · resets 8:30pm (America/Chicago)",
        )
        assert c.kind == KIND_USAGE_LIMIT
        assert c.reset_at_raw == "8:30pm (America/Chicago)"


class TestClassifyNetwork:
    @pytest.mark.parametrize(
        "state", ["timeout", "dns_error", "offline", "rate_limited", "TIMEOUT"]
    )
    def test_allow_listed_network_states(self, state: str) -> None:
        c = classify_failure(network_error_state=state)
        assert c.failure_class == ENVIRONMENTAL
        assert c.kind == KIND_NETWORK

    @pytest.mark.parametrize("state", ["http_error", "unknown", "online", ""])
    def test_non_allow_listed_network_states_stay_work(self, state: str) -> None:
        """An HTTP 400 from an agent server is a coordinator bug, not weather."""
        c = classify_failure(network_error_state=state)
        assert c.failure_class == WORK

    @pytest.mark.parametrize(
        "token",
        [
            "read ECONNRESET",
            "connect ECONNREFUSED 100.64.0.2:7433",
            "Temporary failure in name resolution",
            "TypeError: fetch failed",
            "Connection error.",
            "socket hang up",
        ],
    )
    def test_named_transport_tokens(self, token: str) -> None:
        c = classify_failure(failure_reason=token)
        assert c.failure_class == ENVIRONMENTAL
        assert c.kind == KIND_NETWORK


# ── #3670: infra-outage signatures (worktree deleted mid-run, no exit ──────
# status produced, runner killed before it could report) classify
# environmental, never as a code defect.


class TestClassifyInfra:
    @pytest.mark.parametrize(
        "reason",
        [
            "worktree c376752262ba was deleted out from under the running "
            "pytest suite mid-run before an exit status was produced; "
            "infra issue, not a test result",
            "deleted out from under the running test suite",
            "no exit status was produced",
            "no exit status produced",
            "runner killed before it could report",
            "runner was killed before it could report",
            "infra issue, not a test result",
        ],
    )
    def test_infra_signatures_classify_environmental(self, reason: str) -> None:
        c = classify_failure(failure_reason=reason)
        assert c.failure_class == ENVIRONMENTAL
        assert c.kind == KIND_INFRA

    def test_infra_reason_says_environmental_and_names_3670(self) -> None:
        c = classify_failure(failure_reason="infra issue, not a test result")
        assert "environmental" in c.reason
        assert "#3670" in c.reason

    @pytest.mark.parametrize(
        "reason",
        [
            "3 tests failed: test_foo, test_bar (exit code 1)",
            "AssertionError: expected 2, got 3",
            "pytest reported 5 failures",
            # Mentions "worktree"/"deleted"/"killed" individually, but not
            # the anchored infra phrasing — a genuine test asserting a
            # worktree cleanup path must never be reclassified away.
            "test_cleanup_deletes_the_worktree failed: AssertionError",
            "test_process_is_killed_on_timeout failed",
        ],
    )
    def test_genuine_failures_are_not_misclassified_as_infra(
        self, reason: str
    ) -> None:
        c = classify_failure(failure_reason=reason)
        assert c.failure_class == WORK
        assert c.kind == KIND_WORK


class TestSurfacedReasonNamesTheClass:
    """Acceptance: the surfaced reason distinguishes the two classes in text."""

    def test_environmental_reasons_say_environmental(self) -> None:
        for c in (
            classify_failure(api_error_status=529),
            classify_failure(network_error_state="timeout"),
            classify_failure(
                failure_reason=format_usage_limit_reason(
                    UsageLimitKill(reset_at_raw="8:30pm", excerpt="")
                )
            ),
        ):
            assert "environmental" in c.reason
            assert "work failure" not in c.reason

    def test_work_reason_says_work_failure(self) -> None:
        c = classify_failure(failure_reason="tests failed: 3 failed")
        assert c.reason.startswith("work failure")
        assert "environmental" not in c.reason
        # ...and it carries the original detail forward, so morning triage
        # starts in the right place.
        assert "3 failed" in c.reason

    def test_api_status_is_named_in_the_reason(self) -> None:
        assert "529" in classify_failure(api_error_status=529).reason

    def test_reset_time_is_named_in_the_usage_limit_reason(self) -> None:
        c = classify_failure(
            failure_reason=format_usage_limit_reason(
                UsageLimitKill(reset_at_raw="8:30pm (America/Chicago)", excerpt="")
            )
        )
        assert "8:30pm (America/Chicago)" in c.reason

    def test_to_dict_round_trips_the_fields(self) -> None:
        d = classify_failure(api_error_status=503).to_dict()
        assert d["failure_class"] == ENVIRONMENTAL
        assert d["kind"] == KIND_API_ERROR
        assert d["api_status"] == 503


# ── 2. the budget invariant ─────────────────────────────────────────────────


class TestWorkBudget:
    def test_n_environmental_failures_never_charge_the_work_budget(self) -> None:
        """Acceptance: N environmental failures on a node with zero work
        failures never reach BLOCKED. Modelled the way the sequencer counts:
        only `counts_against_work_budget` increments."""
        max_attempts = 3
        failures = [
            classify_failure(api_error_status=529),
            classify_failure(api_error_status=500),
            classify_failure(network_error_state="timeout"),
            classify_failure(
                failure_reason=format_usage_limit_reason(
                    UsageLimitKill(reset_at_raw="8:30pm (America/Chicago)", excerpt="")
                )
            ),
            classify_failure(api_error_status=503),
            classify_failure(is_error=True, result_text='{"type":"overloaded_error"}'),
        ]
        work_attempts = sum(1 for c in failures if counts_against_work_budget(c))
        assert work_attempts == 0
        assert work_attempts < max_attempts  # never BLOCKED

    def test_genuine_work_failures_still_reach_the_blocked_threshold(self) -> None:
        """Regression bullet: MAX_ATTEMPTS_PER_ISSUE must still bite."""
        max_attempts = 3
        failures = [
            classify_failure(failure_reason="tests failed: 3 failed"),
            classify_failure(failure_reason="review requested changes"),
            classify_failure(failure_reason="no commits produced"),
        ]
        work_attempts = sum(1 for c in failures if counts_against_work_budget(c))
        assert work_attempts == max_attempts  # BLOCKED, correctly

    def test_mixed_run_charges_only_the_work_half(self) -> None:
        failures = [
            classify_failure(api_error_status=529),
            classify_failure(failure_reason="tests failed"),
            classify_failure(network_error_state="offline"),
            classify_failure(failure_reason="review requested changes"),
        ]
        assert sum(1 for c in failures if counts_against_work_budget(c)) == 2


# ── 3. resume from the captured reset time ──────────────────────────────────


class TestParseResetAt:
    def test_time_only_with_zone_resolves_to_that_zone(self) -> None:
        now = datetime(2026, 7, 29, 19, 0, tzinfo=CHICAGO)
        got = parse_reset_at("8:30pm (America/Chicago)", now=now)
        assert got is not None
        assert got.astimezone(CHICAGO) == datetime(2026, 7, 29, 20, 30, tzinfo=CHICAGO)

    def test_time_already_passed_today_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 7, 29, 21, 0, tzinfo=CHICAGO)
        got = parse_reset_at("8:30pm (America/Chicago)", now=now)
        assert got is not None
        assert got.astimezone(CHICAGO) == datetime(2026, 7, 30, 20, 30, tzinfo=CHICAGO)

    def test_date_and_time(self) -> None:
        now = datetime(2026, 7, 26, 12, 0, tzinfo=CHICAGO)
        got = parse_reset_at("Jul 27, 1:30am (America/Chicago)", now=now)
        assert got is not None
        assert got.astimezone(CHICAGO) == datetime(2026, 7, 27, 1, 30, tzinfo=CHICAGO)

    def test_date_in_the_past_rolls_to_next_year(self) -> None:
        now = datetime(2026, 12, 30, 12, 0, tzinfo=CHICAGO)
        got = parse_reset_at("Jan 2, 3pm (America/Chicago)", now=now)
        assert got is not None
        assert got.astimezone(CHICAGO).year == 2027

    def test_bare_hour_meridiem(self) -> None:
        now = datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO)
        got = parse_reset_at("12pm (America/Chicago)", now=now)
        assert got is not None
        assert got.astimezone(CHICAGO) == datetime(2026, 7, 29, 12, 0, tzinfo=CHICAGO)

    def test_midnight_is_12am_not_noon(self) -> None:
        now = datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO)
        got = parse_reset_at("12am (America/Chicago)", now=now)
        assert got is not None
        assert got.astimezone(CHICAGO) == datetime(2026, 7, 30, 0, 0, tzinfo=CHICAGO)

    def test_24_hour_form_with_utc(self) -> None:
        now = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
        got = parse_reset_at("20:30 (UTC)", now=now)
        assert got is not None
        assert got.astimezone(timezone.utc) == datetime(
            2026, 7, 29, 20, 30, tzinfo=timezone.utc
        )

    def test_time_component_is_not_reread_as_a_date(self) -> None:
        """'Jan 30' must not be invented out of '1:30am'."""
        now = datetime(2026, 7, 29, 0, 0, tzinfo=CHICAGO)
        got = parse_reset_at("1:30am (America/Chicago)", now=now)
        assert got is not None
        assert got.astimezone(CHICAGO) == datetime(2026, 7, 29, 1, 30, tzinfo=CHICAGO)

    @pytest.mark.parametrize(
        "raw", [None, "", "   ", "soon", "later today", "(America/Chicago)", "99:99"]
    )
    def test_unparseable_returns_none_never_raises(self, raw: str | None) -> None:
        assert parse_reset_at(raw) is None

    def test_unknown_timezone_falls_back_without_raising(self) -> None:
        now = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
        assert parse_reset_at("8:30pm (Mars/Olympus_Mons)", now=now) is not None

    def test_impossible_date_returns_none(self) -> None:
        now = datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO)
        assert parse_reset_at("Feb 30, 3pm (America/Chicago)", now=now) is None


class TestPlanUsageLimitResume:
    def test_plan_uses_the_captured_reset_time(self) -> None:
        now = datetime(2026, 7, 29, 19, 0, tzinfo=CHICAGO)
        reason = format_usage_limit_reason(
            UsageLimitKill(reset_at_raw="8:30pm (America/Chicago)", excerpt="")
        )
        plan = plan_usage_limit_resume(failure_reason=reason, now=now)
        assert plan.from_reset_time
        assert plan.resume_at.astimezone(CHICAGO) == datetime(
            2026, 7, 29, 20, 30, tzinfo=CHICAGO
        )
        assert not plan.due(now=now)
        assert plan.seconds_remaining(now=now) == pytest.approx(90 * 60)
        assert plan.due(now=now + timedelta(hours=2))
        assert plan.seconds_remaining(now=now + timedelta(hours=2)) == 0.0

    def test_unparseable_reset_falls_back_to_a_bounded_park(self) -> None:
        """A park with no exit is the same bug one level down."""
        now = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
        plan = plan_usage_limit_resume(reset_at_raw="whenever", failed_at=now, now=now)
        assert not plan.from_reset_time
        assert plan.resume_at == now + timedelta(seconds=DEFAULT_USAGE_LIMIT_PARK_SECS)
        assert "no usable reset time" in plan.reason

    def test_no_reset_time_at_all_still_produces_a_plan(self) -> None:
        now = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
        plan = plan_usage_limit_resume(now=now, failed_at=now)
        assert plan.resume_at > now
        assert "no reset time captured" in plan.reason

    def test_reset_is_anchored_at_the_failure_not_at_now(self) -> None:
        """A bare wall-clock reset like "8:30pm" means the next 8:30pm AFTER
        THE KILL. Anchoring on `now` instead would push an already-elapsed
        reset a full day forward every time the plan is recomputed — the node
        would never resume."""
        failed_at = datetime(2026, 7, 29, 19, 0, tzinfo=CHICAGO)
        recomputed_at = datetime(2026, 7, 29, 21, 0, tzinfo=CHICAGO)
        plan = plan_usage_limit_resume(
            reset_at_raw="8:30pm (America/Chicago)",
            failed_at=failed_at,
            now=recomputed_at,
        )
        assert plan.resume_at.astimezone(CHICAGO) == datetime(
            2026, 7, 29, 20, 30, tzinfo=CHICAGO
        )
        assert plan.due(now=recomputed_at)

    def test_explicit_raw_wins_over_the_stamped_reason(self) -> None:
        now = datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO)
        reason = format_usage_limit_reason(
            UsageLimitKill(reset_at_raw="8:30pm (America/Chicago)", excerpt="")
        )
        plan = plan_usage_limit_resume(
            failure_reason=reason, reset_at_raw="11am (America/Chicago)", now=now
        )
        assert plan.resume_at.astimezone(CHICAGO).hour == 11


# ── 4. backoff + liveness probe + relaunch gate ─────────────────────────────


class TestBackoff:
    def test_ladder_doubles_from_the_base(self) -> None:
        assert environmental_backoff_secs(1, base=60.0) == 60.0
        assert environmental_backoff_secs(2, base=60.0) == 120.0
        assert environmental_backoff_secs(3, base=60.0) == 240.0

    def test_ceiling_is_in_the_tens_of_minutes(self) -> None:
        """The issue is explicit: a 60s base for 3 tries covers a blip, not a
        20-minute provider incident."""
        assert DEFAULT_BACKOFF_CEILING_SECS >= 10 * 60
        assert environmental_backoff_secs(50) == DEFAULT_BACKOFF_CEILING_SECS

    def test_absurd_attempt_count_is_clamped_not_exploded(self) -> None:
        assert environmental_backoff_secs(10_000) == DEFAULT_BACKOFF_CEILING_SECS

    def test_attempt_zero_is_no_wait(self) -> None:
        assert environmental_backoff_secs(0) == 0.0


class TestLivenessProbe:
    def test_plan_bars_mean_alive(self) -> None:
        r = probe_environment_liveness(probe=_alive_probe())
        assert r.alive and r.probed

    def test_outage_signal_in_raw_output_means_down(self) -> None:
        r = probe_environment_liveness(probe=_down_probe())
        assert not r.alive
        assert r.probed
        assert "529" in r.detail

    def test_timeout_means_down(self) -> None:
        def probe() -> FakeLimits:
            return FakeLimits(
                status="unknown", error="TimeoutExpired: claude -p /usage"
            )

        r = probe_environment_liveness(probe=probe)
        assert not r.alive

    @pytest.mark.parametrize(
        "error",
        [
            "no recognisable usage bars in /usage output",
            "FileNotFoundError: claude",
            "non-JSON output from claude -p /usage",
            "",
        ],
    )
    def test_inconclusive_probe_fails_open(self, error: str) -> None:
        """An API-key/Bedrock deployment (where /usage means nothing) must
        never be held hostage to a probe that can never succeed here."""
        r = probe_environment_liveness(
            probe=lambda: FakeLimits(status="unknown", error=error)
        )
        assert r.alive
        assert not r.probed

    def test_probe_that_raises_fails_open(self) -> None:
        def boom():
            raise RuntimeError("subprocess exploded")

        r = probe_environment_liveness(probe=boom)
        assert r.alive and not r.probed
        assert "RuntimeError" in r.detail


class TestGateRelaunch:
    def test_work_failure_is_not_gated_and_spends_no_probe(self) -> None:
        probed: list[int] = []

        def probe():
            probed.append(1)
            return FakeLimits(status="ok")

        gate = gate_relaunch(classify_failure(failure_reason="tests failed"), probe=probe)
        assert gate.allow
        assert gate.wait_secs == 0.0
        assert probed == []
        assert "work failure" in gate.reason

    def test_usage_limit_relaunch_is_refused_before_reset_at(self) -> None:
        """Acceptance: a usage-limit kill parks the node and the relaunch is
        not attempted before `reset_at` — and no probe is burnt while parked."""
        now = datetime(2026, 7, 29, 19, 0, tzinfo=CHICAGO)
        probed: list[int] = []

        def probe():
            probed.append(1)
            return FakeLimits(status="ok")

        c = classify_failure(
            failure_reason=format_usage_limit_reason(
                UsageLimitKill(reset_at_raw="8:30pm (America/Chicago)", excerpt="")
            )
        )
        gate = gate_relaunch(c, now=now, probe=probe)
        assert not gate.allow
        assert gate.wait_secs == pytest.approx(90 * 60)
        assert probed == []
        assert "usage limit" in gate.reason

    def test_usage_limit_relaunch_allowed_after_reset_at(self) -> None:
        failed_at = datetime(2026, 7, 29, 19, 0, tzinfo=CHICAGO)
        now = datetime(2026, 7, 29, 21, 0, tzinfo=CHICAGO)
        c = classify_failure(
            failure_reason=format_usage_limit_reason(
                UsageLimitKill(reset_at_raw="8:30pm (America/Chicago)", excerpt="")
            )
        )
        gate = gate_relaunch(c, failed_at=failed_at, now=now, probe=_alive_probe())
        assert gate.allow
        assert gate.liveness is not None and gate.liveness.alive

    def test_usage_limit_past_reset_still_gated_on_the_probe(self) -> None:
        failed_at = datetime(2026, 7, 29, 19, 0, tzinfo=CHICAGO)
        now = datetime(2026, 7, 29, 21, 0, tzinfo=CHICAGO)
        c = classify_failure(
            failure_reason=format_usage_limit_reason(
                UsageLimitKill(reset_at_raw="8:30pm (America/Chicago)", excerpt="")
            )
        )
        gate = gate_relaunch(c, failed_at=failed_at, now=now, probe=_down_probe())
        assert not gate.allow
        assert gate.wait_secs > 0

    def test_outage_relaunch_is_gated_on_a_successful_probe(self) -> None:
        """Acceptance: after an outage, a relaunch is gated on a successful
        liveness probe."""
        failed_at = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
        now = failed_at + timedelta(minutes=30)
        c = classify_failure(api_error_status=529)

        down = gate_relaunch(c, attempt=1, failed_at=failed_at, now=now, probe=_down_probe())
        assert not down.allow
        assert "still down" in down.reason

        up = gate_relaunch(c, attempt=1, failed_at=failed_at, now=now, probe=_alive_probe())
        assert up.allow
        assert "liveness probe passed" in up.reason

    def test_outage_relaunch_waits_out_the_backoff_first(self) -> None:
        failed_at = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
        now = failed_at + timedelta(seconds=10)
        c = classify_failure(api_error_status=529)
        gate = gate_relaunch(
            c, attempt=1, failed_at=failed_at, now=now, probe=_alive_probe()
        )
        assert not gate.allow
        assert gate.wait_secs == pytest.approx(50.0)
        assert "backing off" in gate.reason

    def test_repeated_outages_escalate_the_wait_up_to_the_ceiling(self) -> None:
        failed_at = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
        c = classify_failure(api_error_status=529)
        waits = [
            gate_relaunch(
                c, attempt=n, failed_at=failed_at, now=failed_at, probe=_down_probe()
            ).wait_secs
            for n in (1, 2, 3, 12)
        ]
        assert waits == [60.0, 120.0, 240.0, DEFAULT_BACKOFF_CEILING_SECS]

    def test_gate_to_dict_is_serialisable(self) -> None:
        gate = gate_relaunch(classify_failure(api_error_status=529), probe=_alive_probe())
        d = gate.to_dict()
        assert json.loads(json.dumps(d))["classification"]["kind"] == KIND_API_ERROR


# ── classify_log: the whole-transcript convenience ──────────────────────────


class TestClassifyLog:
    def test_api_error_result_event_in_a_log(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    {"type": "system", "subtype": "init", "model": "sonnet"},
                    {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "api_error_status": 529,
                    },
                ]
            )
        )
        c = classify_log(p)
        assert c.failure_class == ENVIRONMENTAL
        assert c.api_status == 529

    def test_usage_limit_kill_tail_in_a_log(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson([{"type": "system", "subtype": "init"}])
            + "You’ve hit your session limit · resets 8:30pm (America/Chicago)\n"
        )
        c = classify_log(p)
        assert c.kind == KIND_USAGE_LIMIT
        assert c.reset_at_raw == "8:30pm (America/Chicago)"

    def test_successful_log_with_a_work_failure_reason_is_work(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    {"type": "system", "subtype": "init"},
                    {"type": "result", "subtype": "success", "is_error": False},
                ]
            )
        )
        c = classify_log(p, failure_reason="tests failed: 2 failed")
        assert c.failure_class == WORK
        assert "2 failed" in c.reason

    def test_missing_log_is_work_not_an_assumed_outage(self, tmp_path: Path) -> None:
        c = classify_log(tmp_path / "nope.log")
        assert c.failure_class == WORK

    def test_stamped_reason_short_circuits_the_log_read(self, tmp_path: Path) -> None:
        reason = format_usage_limit_reason(
            UsageLimitKill(reset_at_raw="8:30pm (America/Chicago)", excerpt="")
        )
        c = classify_log(tmp_path / "nope.log", failure_reason=reason)
        assert c.kind == KIND_USAGE_LIMIT

    def test_last_result_event_wins(self, tmp_path: Path) -> None:
        """A resumed session can carry two result events; the terminal one is
        the one that decides."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    {"type": "result", "subtype": "success", "is_error": False},
                    {"type": "result", "is_error": True, "api_error_status": 503},
                ]
            )
        )
        assert classify_log(p).api_status == 503

    def test_non_dict_result_event_payload_is_work(self) -> None:
        assert classify_result_event(None).failure_class == WORK
        assert classify_result_event([]).failure_class == WORK  # type: ignore[arg-type]


def test_classification_is_frozen() -> None:
    c = classify_failure(api_error_status=529)
    with pytest.raises(Exception):
        c.failure_class = WORK  # type: ignore[misc]
    assert isinstance(c, FailureClassification)
