"""Black-box coverage for #2268's board-doctor policy (Phases 2 and 3).

Drives the real, externally-observable seams: :func:`policy_tier_of` against
:data:`coord.board_doctor_policy.ALLOWLIST`, and
:func:`escalation_alert_dispatcher` against the real `drive_escalations`
table via :mod:`coord.state` — the same pattern `tests/test_board_doctor.py`
uses for the harness it plugs into.
"""

from __future__ import annotations

import pytest

import coord.board_doctor_policy as board_doctor_policy
import coord.commands._common as common
from coord import queue_diagnose, state
from coord.board_doctor import (
    ALERT_SKIPPED,
    CAUSE_AFTER_PREREQS_LANDED,
    CAUSE_BASELINE_RED,
    CAUSE_CEILING_BLOCKED_MERGEABLE,
    CAUSE_CONFLICT_FIX_HEAD_UNMOVED,
    CAUSE_LATCH_STALE_MERGEABLE,
    CAUSE_MERGE_PENDING_STUCK,
    CAUSE_SMOKE_NO_VERDICT,
    CONVERGENCE_LIMIT,
    MERGE_PENDING_STALE_AFTER_SECONDS,
    TIER_ALERT,
    TIER_AUTO,
    StuckRow,
    _confirm_merge_pending_stuck,
    _reclassify_after_prereqs_landed,
    _reclassify_stale_latch,
    after_prereqs_landed_for_entry,
    merge_queue_rows,
    sweep,
)
from coord.board_doctor_permissions import SweepPermissionError
from coord.board_doctor_policy import (
    ALLOWLIST,
    AUTO_DISPATCHERS,
    INVESTIGATE_CAUSES,
    _clear_stale_latch,
    _fixit_rebudget_eligible,
    _fixit_rebudget_environmental,
    _investigate,
    _merge_pending_stuck_row,
    _reconcile_merged_row,
    _redispatch_head_unmoved_conflict_fix,
    _register_auto_action,
    _requeue_after_prereqs_landed,
    _requeue_ceiling_blocked_mergeable,
    _requeue_never_dispatched,
    dismiss_resolved_escalation,
    escalation_alert_dispatcher,
    make_investigate_capacity_check,
    policy_tier_of,
)
from coord.config import Config
from coord.drive_queue import parse_key
from coord.models import Assignment, Board, Machine, Repo

_IN_FLIGHT_DETAIL = (
    "acme/widgets#40 already has a running or pending assignment — a "
    "fix-it leg is already in flight, not dispatching a second one"
)
_UNPINNABLE_DETAIL = (
    "acme/widgets#40 fix-it leg dispatch could not be confirmed started — "
    "no capable machine, an unreachable agent, or a declined dispatch"
)


@pytest.fixture(autouse=True)
def _no_drive_sessions(monkeypatch) -> None:
    # `_investigate_in_flight_count` (shared by the finisher tier's own
    # capacity check via `_at_repo_capacity`, #3837) also checks live
    # `coord drive` tmux sessions — keep that hermetic across this whole
    # module rather than in each capacity-check test class individually.
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])


def _row(
    key: str,
    cause: str,
    queue: str = "drive-queue",
    evidence: tuple[str, ...] = (),
    *,
    fresh: bool = True,
    confidence: str = queue_diagnose.CONFIDENCE_HIGH,
) -> StuckRow:
    return StuckRow(
        queue=queue,
        key=key,
        state="stuck",
        cause=cause,
        confidence=confidence,
        evidence=evidence or (f"cause={cause}",),
        fresh=fresh,
    )


def test_parse_row_key_is_the_one_shared_parser() -> None:
    # #3807: this module's private row-key splitter must be the same
    # function as coord.drive_queue.parse_key, not a second
    # implementation that happens to agree with it today.
    assert board_doctor_policy._parse_row_key is parse_key


class TestPolicyTierOf:
    def test_allowlist_holds_exactly_the_3760_entries(self) -> None:
        # No cause is pre-approved for auto-action without a reviewed,
        # known-safe fix behind it. #3760 names three: a
        # ceiling-blocked-but-mergeable row gets a fresh fix-round budget,
        # a row whose PR already merged gets reconciled, and a row whose
        # smoke leg produced no parseable verdict gets its Test verdict
        # cleared so it re-dispatches (Action 5, #2244). #3874 adds a
        # fourth: a row that was never dispatched at all gets requeued, the
        # mechanical equivalent of the operator remove+add that shape
        # otherwise needs. #3887 adds three more mechanical queue repairs,
        # each applied via the SAME #3881 investigator wrapper a
        # human-attended session would use. #3901 (#3887 case 4's
        # remainder) adds an eighth: a frozen blocked/parked row whose
        # `--after` prereqs have since landed gets the SAME requeue. Every
        # other cause stays alert-tier.
        assert ALLOWLIST == {
            CAUSE_CEILING_BLOCKED_MERGEABLE: "drive_queue_requeue",
            queue_diagnose.CAUSE_PR_MERGED: "mark_done",
            CAUSE_SMOKE_NO_VERDICT: "smoke_requeue",
            queue_diagnose.CAUSE_NEVER_DISPATCHED: "drive_queue_requeue",
            CAUSE_LATCH_STALE_MERGEABLE: "clear_latch",
            CAUSE_MERGE_PENDING_STUCK: "merge_approved_green",
            CAUSE_CONFLICT_FIX_HEAD_UNMOVED: "conflict_fix_redispatch",
            CAUSE_AFTER_PREREQS_LANDED: "drive_queue_requeue",
        }

    def test_every_other_cause_is_alert_tier_today(self) -> None:
        # Causes with no safe mechanical fix AND no investigate-tier
        # dispatcher registered (#3769): "nothing actually wrong" states
        # (nothing-blocking, ci-green), a whole-repo condition no single
        # row can fix (baseline-red), and a genuinely unknown, never-
        # registered string. These must never collide with a real
        # queue_diagnose.CAUSE_* value — that would make this test
        # vacuous the moment the real cause gets routed to a tier.
        for cause in (
            "not-a-real-cause",
            "also-not-a-real-cause",
            queue_diagnose.CAUSE_NOTHING_BLOCKING,
            queue_diagnose.CAUSE_CI_GREEN,
            CAUSE_BASELINE_RED,
        ):
            assert cause not in ALLOWLIST
            assert cause not in INVESTIGATE_CAUSES
            assert policy_tier_of(cause) == TIER_ALERT

    def test_registered_3760_causes_are_auto_tier(self) -> None:
        assert policy_tier_of(CAUSE_CEILING_BLOCKED_MERGEABLE) == TIER_AUTO
        assert policy_tier_of(queue_diagnose.CAUSE_PR_MERGED) == TIER_AUTO
        assert policy_tier_of(queue_diagnose.CAUSE_NEVER_DISPATCHED) == TIER_AUTO
        assert policy_tier_of(CAUSE_AFTER_PREREQS_LANDED) == TIER_AUTO

    def test_investigate_eligible_causes_are_auto_tier_via_fixit_leg(self) -> None:
        # #3769: a cause with no safe mechanical fix still reaches
        # TIER_AUTO once it is routed to the investigate tier — it lands in
        # AUTO_DISPATCHERS (what the cause -> TIER_AUTO check actually
        # reads) but never in ALLOWLIST, which stays reserved for fixes
        # that touch the queue row itself (#3760).
        assert INVESTIGATE_CAUSES == {
            queue_diagnose.CAUSE_PR_CLOSED,
            queue_diagnose.CAUSE_MERGE_CONFLICT,
            queue_diagnose.CAUSE_DEAD_LEG,
            queue_diagnose.CAUSE_NO_PR,
            queue_diagnose.CAUSE_CI_RED,
            # #3882: a `ci-red` row whose own branch Test verdict already
            # read green — the merged-tree-only failure shape.
            queue_diagnose.CAUSE_INTEGRATION_RED,
            queue_diagnose.CAUSE_CI_PENDING,
            queue_diagnose.CAUSE_GATE_BLOCKED,
            queue_diagnose.CAUSE_AGENT_UNREACHABLE,
            queue_diagnose.CAUSE_UNKNOWN,
        }
        for cause in INVESTIGATE_CAUSES:
            assert policy_tier_of(cause) == TIER_AUTO
            assert cause in AUTO_DISPATCHERS
            assert cause not in ALLOWLIST

    def test_a_future_allowlist_entry_would_be_honored(self, monkeypatch) -> None:
        # Pins the seam itself, not a specific entry: if a cause is ever
        # registered for a real safe-tier action, policy_tier_of must
        # actually read the mappings rather than hardcoding "always alert".
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "drive_queue_add")
        monkeypatch.setitem(AUTO_DISPATCHERS, "probe_only_cause", lambda row: True)
        assert policy_tier_of("probe_only_cause") == TIER_AUTO

    def test_allowlist_entry_without_a_dispatcher_stays_alert_tier(self, monkeypatch) -> None:
        # An allowlist entry with no real dispatcher behind it must never
        # silently promote to auto-tier — that would suppress the alert
        # while doing nothing.
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "drive_queue_add")
        assert "probe_only_cause" not in AUTO_DISPATCHERS
        assert policy_tier_of("probe_only_cause") == TIER_ALERT

    def test_an_allowlist_entry_naming_an_unsafe_action_stays_alert_tier(
        self, monkeypatch
    ) -> None:
        # Guards against a future bug that assigns into ALLOWLIST /
        # AUTO_DISPATCHERS directly, bypassing _register_auto_action: an
        # entry naming an action outside SAFE_TIER_ACTIONS must not promote
        # to auto-tier even though both mappings "agree".
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "force_merge")
        monkeypatch.setitem(AUTO_DISPATCHERS, "probe_only_cause", lambda row: True)
        assert policy_tier_of("probe_only_cause") == TIER_ALERT


class TestRegisterAutoAction:
    def test_registers_a_safe_tier_action_in_both_mappings(self, monkeypatch) -> None:
        calls: list[StuckRow] = []
        # Claim both keys through monkeypatch first — they are absent, so
        # its undo DELETES them — then let the real _register_auto_action
        # overwrite both. Same teardown as every neighbouring test, with no
        # hand-rolled finally block.
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "unregistered")
        monkeypatch.setitem(AUTO_DISPATCHERS, "probe_only_cause", lambda row: None)

        _register_auto_action("probe_only_cause", "drive_queue_add", calls.append)

        assert ALLOWLIST["probe_only_cause"] == "drive_queue_add"
        assert policy_tier_of("probe_only_cause") == TIER_AUTO

        row = _row("acme/widgets#1", "probe_only_cause")
        AUTO_DISPATCHERS["probe_only_cause"](row)
        assert calls == [row]

    def test_refuses_to_register_an_action_outside_the_safe_tier(self) -> None:
        with pytest.raises(SweepPermissionError, match="force_merge"):
            _register_auto_action("probe_only_cause", "force_merge", lambda row: None)

        assert "probe_only_cause" not in ALLOWLIST
        assert "probe_only_cause" not in AUTO_DISPATCHERS


class TestEscalationAlertDispatcher:
    def test_drive_queue_row_files_a_real_escalation(self) -> None:
        row = _row("acme/widgets#1762", "merge_conflict")

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 1762)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"
        assert escalation["proposed_command"] == "coord status"
        assert "merge_conflict" in escalation["reason"]
        assert "acme/widgets#1762" in escalation["reason"]
        assert "cause=merge_conflict" in escalation["gate_readings"]

    def test_merge_queue_row_parses_the_same_repo_hash_issue_key(self) -> None:
        row = _row("acme/widgets#99", "gate_blocked", queue="merge-queue")

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 99)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:merge-queue"

    def test_renewing_the_same_cause_preserves_created_at(self) -> None:
        row = _row("acme/widgets#5", "ci_red")

        escalation_alert_dispatcher(row)
        first = state.get_drive_escalation("acme/widgets", 5)
        assert first is not None

        assert escalation_alert_dispatcher(row) is True
        second = state.get_drive_escalation("acme/widgets", 5)
        assert second is not None
        assert second["created_at"] == first["created_at"]

    def test_unparseable_key_does_not_raise(self) -> None:
        row = _row("no-hash-separator", "ci_red")

        # Must not raise — a malformed key is a dropped alert, not a crash.
        assert escalation_alert_dispatcher(row) is False

        assert state.list_drive_escalations() == []

    def test_empty_repo_name_is_rejected_without_writing(self) -> None:
        # A key like "#12" rpartitions to an empty repo_name — must not
        # write an escalation keyed on an empty repo.
        row = _row("#12", "ci_red")

        assert escalation_alert_dispatcher(row) is False

        assert state.list_drive_escalations() == []

    def test_negative_issue_number_is_rejected_without_writing(self) -> None:
        # #3807: a key like "repo#-5" must parse the same way everywhere —
        # the shared parser (coord.drive_queue.parse_key) requires a bare
        # digit string after the last "#", so a leading "-" is rejected
        # here exactly as it is by dismiss_resolved_escalation, instead of
        # one accepting it via int() and the other rejecting it.
        row = _row("acme/widgets#-5", "ci_red")

        assert escalation_alert_dispatcher(row) is False

        assert state.list_drive_escalations() == []

    def test_a_more_specific_existing_escalation_is_left_untouched(self) -> None:
        # A differently-staged escalation (e.g. filed by plan.blocked)
        # already covers this issue — the generic board-doctor alert must
        # not clobber its reason with "coord status".
        state.record_drive_escalation(
            "acme/widgets",
            7,
            stage="plan.blocked",
            reason="blocked on a human decision",
            gate_readings="",
            proposed_command="coord approve 7",
        )
        row = _row("acme/widgets#7", "ci_red")

        # Reported as skipped, NOT delivered: nothing board-doctor filed,
        # so `sweep` must not mark the row escalated and suppress the
        # alert forever — the covering escalation can be dismissed by its
        # own writer at any time.
        assert escalation_alert_dispatcher(row) == ALERT_SKIPPED

        escalation = state.get_drive_escalation("acme/widgets", 7)
        assert escalation is not None
        assert escalation["stage"] == "plan.blocked"
        assert escalation["reason"] == "blocked on a human decision"

    def test_the_alert_is_filed_once_a_covering_escalation_is_dismissed(self) -> None:
        state.record_drive_escalation(
            "acme/widgets",
            11,
            stage="plan.blocked",
            reason="blocked on a human decision",
            gate_readings="",
            proposed_command="coord approve 11",
        )
        row = _row("acme/widgets#11", "ci_red")
        assert escalation_alert_dispatcher(row) == ALERT_SKIPPED

        state.dismiss_drive_escalation("acme/widgets", 11)

        assert escalation_alert_dispatcher(row) is True
        escalation = state.get_drive_escalation("acme/widgets", 11)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"

    def test_a_write_failure_is_reported_as_not_delivered(self, monkeypatch) -> None:
        # The DB being unavailable must read as a failed alert, not as a
        # silently-swallowed success — `sweep` keys the "escalate exactly
        # once" suppression off this return value.
        def boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(state, "record_drive_escalation", boom)

        assert escalation_alert_dispatcher(_row("acme/widgets#12", "ci_red")) is False
        assert state.list_drive_escalations() == []

    def test_a_write_that_does_not_read_back_is_reported_as_not_delivered(
        self, monkeypatch
    ) -> None:
        # Confirmation is an observation taken AFTER the write: a write
        # that reports no error but leaves nothing on file is a failure.
        monkeypatch.setattr(state, "record_drive_escalation", lambda *a, **k: None)

        assert escalation_alert_dispatcher(_row("acme/widgets#13", "ci_red")) is False

    def test_gate_blocked_merge_queue_row_gets_the_override_command(self) -> None:
        # #3761: on a merge-queue row this cause is only ever minted from
        # the queue's own `human_required` state, so the override is
        # always valid there — surfaced as a one-line, ready-to-run command
        # rather than the generic "coord status" pointer. The sweep itself
        # never runs it.
        row = _row(
            "acme/widgets#21", queue_diagnose.CAUSE_GATE_BLOCKED, queue="merge-queue"
        )

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 21)
        assert escalation is not None
        assert escalation["proposed_command"] == (
            "coord merge --only acme/widgets#21 "
            '--override-human-required "<reason the override is safe>"'
        )
        assert "--override-human-required" in escalation["reason"]

    def test_gate_blocked_drive_queue_row_points_at_diagnose_not_override(self) -> None:
        # #3761 review: on a drive-queue row this cause means generic
        # `live.gate_ready is False` (unapproved review, a different red
        # required check, or a non-mergeable PR) — NOT necessarily a
        # merge-queue `human_required` state, so `coord merge
        # --override-human-required` would hard-error there. The surfaced
        # command must be the read-only diagnose, not the override.
        row = _row(
            "acme/widgets#21", queue_diagnose.CAUSE_GATE_BLOCKED, queue="drive-queue"
        )

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 21)
        assert escalation is not None
        assert escalation["proposed_command"] == "coord drive-queue diagnose --json"
        assert "--override-human-required" not in escalation["proposed_command"]

    def test_baseline_red_row_keeps_coord_status_with_an_explanatory_reason(self) -> None:
        # #3761: there is no single per-row command that fixes a red base,
        # so the fallback command stays, but the reason explains why.
        row = _row("acme/widgets#0", CAUSE_BASELINE_RED)

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 0)
        assert escalation is not None
        assert escalation["proposed_command"] == "coord status"
        assert "merge base is red" in escalation["reason"]

    def test_an_unrelated_cause_keeps_the_generic_coord_status_default(self) -> None:
        row = _row("acme/widgets#22", "ci_red")

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 22)
        assert escalation is not None
        assert escalation["proposed_command"] == "coord status"

    def test_the_page_renders_cause_evidence_attempts_and_options_with_commands(
        self,
    ) -> None:
        """#3883 point 1: an escalation must hand the operator the FULL
        diagnosis — what is stuck, the cause with its evidence, what the
        doctor already tried and how it ended, and 2-3 concrete options
        each with an exact command, one marked recommended — rather than
        the bare `cause=... (escalated)` #3883 was filed to replace.
        """
        state.record_board_doctor_decision(
            {
                "sweep_id": "s-1", "queue": "drive-queue", "key": "acme/widgets#55",
                "cause": "gate-blocked", "tier": "alert", "action": "alert",
                "count": 1, "escalated": True, "confidence": "high",
                "evidence": [], "recorded_at": "2026-11-01T00:00:00+00:00",
            }
        )
        row = _row(
            "acme/widgets#55",
            queue_diagnose.CAUSE_GATE_BLOCKED,
            evidence=("coord gates: blocked — review not approved",),
        )

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 55)
        assert escalation is not None
        reason = escalation["reason"]
        # the cause, with its confidence:
        assert "cause=gate-blocked" in reason
        assert "confidence=high" in reason
        # what was already tried, and how it ended:
        assert "already tried: alert (outcome=" in reason
        # 2-3 options, each with its own exact command, one recommended:
        assert "options:" in reason
        assert "[recommended]" in reason
        assert "`coord drive-queue diagnose --json`" in reason
        # the evidence itself lives in `gate_readings`, the same
        # `drive_escalations` row `coord drive-queue doctor-log` now also
        # reads (via its own `reason` field) to surface this diagnosis:
        assert "coord gates: blocked — review not approved" in escalation["gate_readings"]

    def test_baseline_red_env_diagnosis_names_a_credential_and_its_host(
        self, monkeypatch,
    ) -> None:
        """#3883 point 2: a fake 401 from a re-run on the repo's own
        configured host surfaces a "renew credential" option naming that
        host — the coord#3870 shape (an expired opencode key on
        dellserver/precision) that used to cost a whole operator session
        to track down by hand.
        """
        import socket

        from coord.config import Machine, SmokeTestsConfig

        this_host = socket.gethostname()
        config = Config(
            repos=[Repo(name="acme/widgets", github="acme/widgets")],
            machines=[Machine(name="m1", host=this_host, repos=["acme/widgets"])],
            smoke_tests=SmokeTestsConfig(default_command="pytest -q"),
        )
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: config)
        monkeypatch.setattr(
            board_doctor_policy,
            "_default_env_probe_runner",
            lambda host, command, **_kwargs: queue_diagnose.EnvProbeResult(
                exit_code=1,
                output="requests.exceptions.HTTPError: 401 Unauthorized from opencode",
            ),
        )
        row = _row("acme/widgets#0", CAUSE_BASELINE_RED)

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 0)
        assert escalation is not None
        reason = escalation["reason"]
        assert "renew credential" in reason
        assert this_host in reason

    def test_baseline_red_env_probe_never_runs_against_a_non_local_host(
        self, monkeypatch,
    ) -> None:
        """A machine declared for the repo that is NOT the host this
        process is running on must degrade straight to "can't tell"
        without ever invoking the probe runner — the fleet has no
        agent-HTTP exec primitive yet to reach a different host's
        environment from here, so a fake failing re-run must never be
        attributed to a host that was never actually probed.
        """
        from coord.config import Machine, SmokeTestsConfig

        config = Config(
            repos=[Repo(name="acme/widgets", github="acme/widgets")],
            machines=[
                Machine(name="elsewhere", host="elsewhere.ts.net", repos=["acme/widgets"]),
            ],
            smoke_tests=SmokeTestsConfig(default_command="pytest -q"),
        )
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: config)

        def _must_not_run(host, command, **kwargs):
            raise AssertionError("the probe runner must not run for a non-local host")

        monkeypatch.setattr(
            board_doctor_policy, "_default_env_probe_runner", _must_not_run,
        )
        row = _row("acme/widgets#0", CAUSE_BASELINE_RED)

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 0)
        assert escalation is not None
        reason = escalation["reason"]
        assert "renew credential" not in reason
        assert "install" not in reason
        assert escalation["proposed_command"] == "coord status"

    def test_baseline_red_lost_work_names_a_possibly_lost_branch(
        self, monkeypatch,
    ) -> None:
        """#3883 point 3: a POSSIBLY-LOST branch whose own diff touches a
        file the re-run just reported failing is named in the page as a
        likely ready-made fix — the coord#3793 shape.
        """
        import socket

        from coord.config import Machine, SmokeTestsConfig

        this_host = socket.gethostname()
        config = Config(
            repos=[Repo(name="acme/widgets", github="acme/widgets")],
            machines=[Machine(name="m1", host=this_host, repos=["acme/widgets"])],
            smoke_tests=SmokeTestsConfig(default_command="pytest -q"),
        )
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: config)
        monkeypatch.setattr(
            board_doctor_policy,
            "_default_env_probe_runner",
            lambda host, command, **_kwargs: queue_diagnose.EnvProbeResult(
                exit_code=1,
                output="FAILED tests/test_widgets.py::test_frobnicate - AssertionError",
            ),
        )
        board = Board(
            completed=[
                Assignment(
                    assignment_id="asn-lost-1",
                    repo_name="acme/widgets",
                    machine_name="m1",
                    issue_number=99,
                    issue_title="frobnicate",
                    status="merged",
                    branch="issue-99-frobnicate-fix",
                )
            ]
        )
        monkeypatch.setattr(state, "build_board", lambda: board)
        monkeypatch.setattr(
            "coord.branch_model.resolve_base_branch_for_issue_number",
            lambda *a, **k: "develop",
        )
        monkeypatch.setattr(
            "coord.github_ops.branch_exists_on_remote", lambda *a, **k: True,
        )
        monkeypatch.setattr(
            "coord.github_ops.pr_is_merged", lambda *a, **k: False,
        )
        monkeypatch.setattr(
            "coord.github_ops.branch_commits_ahead", lambda *a, **k: 2,
        )
        monkeypatch.setattr(
            "coord.github_ops.get_compare_files",
            lambda *a, **k: ["tests/test_widgets.py"],
        )
        monkeypatch.setattr(
            "coord.github_ops.get_repo_file",
            lambda repo, path, branch: f"content-on-{branch}",
        )
        row = _row("acme/widgets#0", CAUSE_BASELINE_RED)

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 0)
        assert escalation is not None
        assert "issue-99-frobnicate-fix" in escalation["reason"]

    def test_baseline_red_lost_work_skips_a_branch_already_landed_verbatim(
        self, monkeypatch,
    ) -> None:
        """A branch that is ahead of base but whose changed files are
        byte-identical to base's own copy already landed via rebase/squash
        — `coord.reconcile`'s own confirmed-clean rule — and must NOT be
        named as a likely ready-made fix.
        """
        import socket

        from coord.config import Machine, SmokeTestsConfig

        this_host = socket.gethostname()
        config = Config(
            repos=[Repo(name="acme/widgets", github="acme/widgets")],
            machines=[Machine(name="m1", host=this_host, repos=["acme/widgets"])],
            smoke_tests=SmokeTestsConfig(default_command="pytest -q"),
        )
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: config)
        monkeypatch.setattr(
            board_doctor_policy,
            "_default_env_probe_runner",
            lambda host, command, **_kwargs: queue_diagnose.EnvProbeResult(
                exit_code=1,
                output="FAILED tests/test_widgets.py::test_frobnicate - AssertionError",
            ),
        )
        board = Board(
            completed=[
                Assignment(
                    assignment_id="asn-lost-2",
                    repo_name="acme/widgets",
                    machine_name="m1",
                    issue_number=100,
                    issue_title="frobnicate again",
                    status="merged",
                    branch="issue-100-frobnicate-fix",
                )
            ]
        )
        monkeypatch.setattr(state, "build_board", lambda: board)
        monkeypatch.setattr(
            "coord.branch_model.resolve_base_branch_for_issue_number",
            lambda *a, **k: "develop",
        )
        monkeypatch.setattr(
            "coord.github_ops.branch_exists_on_remote", lambda *a, **k: True,
        )
        monkeypatch.setattr(
            "coord.github_ops.pr_is_merged", lambda *a, **k: False,
        )
        monkeypatch.setattr(
            "coord.github_ops.branch_commits_ahead", lambda *a, **k: 2,
        )
        monkeypatch.setattr(
            "coord.github_ops.get_compare_files",
            lambda *a, **k: ["tests/test_widgets.py"],
        )
        # Byte-identical on both ends — a rebase/squash already landed it.
        monkeypatch.setattr(
            "coord.github_ops.get_repo_file", lambda repo, path, branch: "same content",
        )
        row = _row("acme/widgets#0", CAUSE_BASELINE_RED)

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 0)
        assert escalation is not None
        assert "issue-100-frobnicate-fix" not in escalation["reason"]

    def test_integration_red_confined_to_sealed_paths_pages_a_design_decision(
        self, monkeypatch,
    ) -> None:
        """A merged-tree failure whose only failing file — read off the
        failing CI job's own log excerpt, never its check NAME, since a
        check name never contains a path on this repo's own CI — is
        entirely within the repo's own sealed acceptance paths pages
        "needs a design decision" with the sealed lines, rather than the
        generic wording every other cause gets — the quadraui#2028 shape.
        """
        from coord.ci_store import CIFailureDetail
        from coord.config import AcceptanceDriverConfig

        config = Config(repos=[Repo(name="acme/widgets", github="acme/widgets")], machines=[])
        config.acceptance.drivers["acme/widgets"] = AcceptanceDriverConfig(kind="cli-pytest")
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: config)

        board = Board(
            active=[
                Assignment(
                    assignment_id="asn-61",
                    repo_name="acme/widgets",
                    machine_name="m1",
                    issue_number=61,
                    issue_title="widget",
                    status="in_review",
                    branch="issue-61-widget",
                )
            ]
        )
        monkeypatch.setattr(state, "build_board", lambda: board)
        monkeypatch.setattr(
            "coord.github_ops.find_pr_for_branch",
            lambda repo, branch: {"number": 77},
        )
        monkeypatch.setattr(
            "coord.ci_store.build_ci_store", lambda *a, **k: object(),
        )
        monkeypatch.setattr(
            "coord.ci_github.build_ci_failure_detail",
            lambda ci_store, repo, number: CIFailureDetail(
                check_name="coord acceptance run --all --ci (cli-pytest route, ms-37)",
                job_name="coord acceptance run --all --ci (cli-pytest route, ms-37)",
                step_name="pytest",
                log_excerpt=(
                    "FAILED tests/acceptance/test_descriptor.py::test_x - "
                    "AssertionError"
                ),
                run_url="https://example.com/run/1",
            ),
        )
        row = _row(
            "acme/widgets#61",
            queue_diagnose.CAUSE_INTEGRATION_RED,
            evidence=(
                "gh pr checks: 2 check(s) — 1 failure "
                "(coord acceptance run --all --ci (cli-pytest route, ms-37))",
            ),
        )

        assert escalation_alert_dispatcher(row) is True

        escalation = state.get_drive_escalation("acme/widgets", 61)
        assert escalation is not None
        reason = escalation["reason"]
        assert "needs a design decision" in reason
        assert "tests/acceptance/test_descriptor.py" in reason
        assert "redesign" in reason


class TestClassifyEnvProbeOutput:
    """The text classifier behind the baseline-red env diagnosis."""

    def test_names_the_provider_whose_credential_failed(self) -> None:
        category, name = queue_diagnose.classify_env_probe_output(
            "opencode: 401 Unauthorized — invalid api key",
        )
        assert category == queue_diagnose.ENV_DIAGNOSIS_CREDENTIAL
        assert name == "opencode"

    def test_names_the_forge_by_its_full_name(self) -> None:
        category, name = queue_diagnose.classify_env_probe_output(
            "github: HTTP 403 Forbidden",
        )
        assert category == queue_diagnose.ENV_DIAGNOSIS_CREDENTIAL
        assert name == "github"

    def test_an_unrecognised_provider_is_still_a_credential_failure(self) -> None:
        """A provider nobody listed must not be mis-named just because an
        English word in the output happens to contain a short abbreviation
        of some other provider — the category is what drives the option,
        and ``None`` is a legitimate name.
        """
        category, name = queue_diagnose.classify_env_probe_output(
            "forgeproxy: authentication failed halfway through the highlighted run",
        )
        assert category == queue_diagnose.ENV_DIAGNOSIS_CREDENTIAL
        assert name is None

    def test_a_missing_tool_is_named_from_the_shells_own_wording(self) -> None:
        category, name = queue_diagnose.classify_env_probe_output(
            "/bin/sh: line 1: pytest: command not found",
        )
        assert category == queue_diagnose.ENV_DIAGNOSIS_MISSING_TOOL
        assert name == "pytest"

    def test_an_ordinary_test_failure_is_a_red_base(self) -> None:
        category, name = queue_diagnose.classify_env_probe_output(
            "FAILED tests/test_widget.py::test_spin - AssertionError\n1 failed",
        )
        assert category == queue_diagnose.ENV_DIAGNOSIS_BASE_RED
        assert name is None

    def test_a_bare_line_number_matching_401_or_403_stays_a_red_base(self) -> None:
        """A pytest traceback's line number (``:403:``) and a pass/fail
        count (``1401 passed``) must never read as a credential failure —
        only a status code WITH its HTTP wording should.
        """
        category, name = queue_diagnose.classify_env_probe_output(
            "tests/test_widget.py:403: AssertionError\n1 failed, 1401 passed",
        )
        assert category == queue_diagnose.ENV_DIAGNOSIS_BASE_RED
        assert name is None


class TestIsSealedConfinedFailure:
    """The pure predicate behind the point-4 design-decision page."""

    def test_every_failing_path_under_a_directory_sealed_path_is_confined(self) -> None:
        assert queue_diagnose.is_sealed_confined_failure(
            ("tests/acceptance/ms-37/test_descriptor.py",), ("tests/acceptance/",),
        ) is True

    def test_a_failing_path_outside_every_sealed_path_is_not_confined(self) -> None:
        assert queue_diagnose.is_sealed_confined_failure(
            ("coord/board_doctor_policy.py",), ("tests/acceptance/",),
        ) is False

    def test_a_mix_of_sealed_and_unsealed_paths_is_not_confined(self) -> None:
        assert queue_diagnose.is_sealed_confined_failure(
            ("tests/acceptance/test_descriptor.py", "coord/dispatch.py"),
            ("tests/acceptance/",),
        ) is False

    def test_a_file_sealed_path_requires_an_exact_or_suffix_match(self) -> None:
        """An `entrypoint:`-style sealed path with no trailing slash names
        ONE exact file — a failing path that merely shares a substring with
        it must not be read as confined (the trailing-slash branch's
        substring rule is for directories only).
        """
        assert queue_diagnose.is_sealed_confined_failure(
            ("tui/tests/acceptance.rs",), ("tui/tests/acceptance.rs",),
        ) is True
        assert queue_diagnose.is_sealed_confined_failure(
            ("tui/tests/acceptance.rs.bak",), ("tui/tests/acceptance.rs",),
        ) is False

    def test_no_failing_paths_is_never_confined(self) -> None:
        assert queue_diagnose.is_sealed_confined_failure((), ("tests/acceptance/",)) is False

    def test_no_sealed_paths_is_never_confined(self) -> None:
        assert queue_diagnose.is_sealed_confined_failure(("tests/acceptance/test_x.py",), ()) is False


class TestDismissResolvedEscalation:
    def test_dismisses_an_escalation_this_module_filed(self) -> None:
        row = _row("acme/widgets#8", "ci_red")
        escalation_alert_dispatcher(row)
        assert state.get_drive_escalation("acme/widgets", 8) is not None

        dismiss_resolved_escalation("drive-queue", "acme/widgets#8")

        assert state.get_drive_escalation("acme/widgets", 8) is None

    def test_leaves_a_differently_staged_escalation_untouched(self) -> None:
        state.record_drive_escalation(
            "acme/widgets",
            9,
            stage="plan.blocked",
            reason="blocked on a human decision",
            gate_readings="",
            proposed_command="coord approve 9",
        )

        dismiss_resolved_escalation("drive-queue", "acme/widgets#9")

        escalation = state.get_drive_escalation("acme/widgets", 9)
        assert escalation is not None
        assert escalation["stage"] == "plan.blocked"

    def test_malformed_key_does_not_raise(self) -> None:
        # Must not raise — a malformed key means nothing to clean up.
        dismiss_resolved_escalation("drive-queue", "no-hash-separator")
        dismiss_resolved_escalation("drive-queue", "#12")

    def test_negative_issue_number_does_not_raise_or_dismiss(self) -> None:
        # #3807: "repo#-5" must be rejected here exactly as it is by
        # escalation_alert_dispatcher — both now route through the same
        # coord.drive_queue.parse_key, which requires a bare digit tail.
        state.record_drive_escalation(
            "acme/widgets",
            5,
            stage="board-doctor:drive-queue",
            reason="ci_red",
            gate_readings="",
            proposed_command="coord status",
        )

        dismiss_resolved_escalation("drive-queue", "acme/widgets#-5")

        assert state.get_drive_escalation("acme/widgets", 5) is not None

    def test_missing_escalation_does_not_raise(self) -> None:
        dismiss_resolved_escalation("drive-queue", "acme/widgets#404")


class TestInvestigate:
    """#3769's C1 wiring: `_investigate` is the one handler behind every
    entry in :data:`INVESTIGATE_CAUSES`.
    """

    def test_dispatches_a_fixit_leg_when_the_drive_loop_has_given_up(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: object())
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {
                "dispatched": True,
                "assignment_id": "asn-1",
                "machine": "mac1",
                "detail": "acme/widgets#40 fix-it leg dispatched to mac1 (iteration 1)",
            },
        )
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is True
        # A started leg is not an alert-worthy outcome.
        assert state.list_drive_escalations() == []

    @pytest.mark.parametrize(
        "cause", [queue_diagnose.CAUSE_CI_RED, queue_diagnose.CAUSE_INTEGRATION_RED],
    )
    def test_ci_shaped_causes_carry_the_merge_ref_instruction(
        self, monkeypatch, cause,
    ) -> None:
        """`_fixit_leg_briefing` (`coord.board_doctor_permissions`) has no
        merge-ref/CI-excerpt section of its own the way
        `coord.ci_fix.build_ci_fix_briefing` does — it only ever renders
        *evidence* verbatim. So a dispatched `ci-red`/`integration-red`
        fix-it leg must get the merge-the-base instruction THERE, or the
        leg reproduces nothing but the branch, exactly the blind spot
        this cause exists to close."""
        config = Config(
            repos=[Repo(name="acme/widgets", github="acme/widgets")], machines=[],
        )
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: config)
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: Board(active=[]))
        monkeypatch.setattr(
            "coord.branch_model.resolve_base_branch_for_issue_number",
            lambda *a, **k: "develop",
        )

        captured: dict = {}

        def fake_dispatch(action, repo_name, issue_number, board, config, **kwargs):
            captured["evidence"] = kwargs["evidence"]
            return {
                "dispatched": True, "assignment_id": "asn-1", "machine": "mac1",
                "detail": "dispatched",
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)

        row = _row("acme/widgets#40", cause, evidence=("gh pr checks: failing — build",))

        assert _investigate(row) is True
        briefing_evidence = "\n".join(captured["evidence"])
        assert "git fetch origin && git rebase origin/develop" in briefing_evidence
        assert "merge ref" in briefing_evidence
        # The diagnosis's own decisive line (the closest thing to a CI
        # excerpt this tier has) is preserved, not replaced.
        assert "gh pr checks: failing — build" in captured["evidence"]

    def test_non_ci_shaped_cause_gets_no_merge_ref_instruction(
        self, monkeypatch,
    ) -> None:
        config = Config(
            repos=[Repo(name="acme/widgets", github="acme/widgets")], machines=[],
        )
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: config)
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: Board(active=[]))

        captured: dict = {}

        def fake_dispatch(action, repo_name, issue_number, board, config, **kwargs):
            captured["evidence"] = kwargs["evidence"]
            return {
                "dispatched": True, "assignment_id": "asn-1", "machine": "mac1",
                "detail": "dispatched",
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)

        row = _row(
            "acme/widgets#40", queue_diagnose.CAUSE_MERGE_CONFLICT,
            evidence=("gh pr view: mergeable=NO",),
        )

        assert _investigate(row) is True
        assert captured["evidence"] == ("gh pr view: mergeable=NO",)

    def test_leaves_an_in_flight_row_alone(self, monkeypatch) -> None:
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: object())
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {
                "dispatched": False,
                "assignment_id": None,
                "machine": None,
                "detail": _IN_FLIGHT_DETAIL,
            },
        )
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is False
        # The drive loop (or a prior fix-it leg) is already on this row —
        # left alone, no human-visible alert filed.
        assert state.list_drive_escalations() == []

    def test_falls_back_to_the_alert_tier_when_no_machine_can_be_pinned(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: object())
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {
                "dispatched": False,
                "assignment_id": None,
                "machine": None,
                "detail": _UNPINNABLE_DETAIL,
            },
        )
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is False
        escalation = state.get_drive_escalation("acme/widgets", 40)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"

    def test_falls_back_to_the_alert_tier_when_config_cannot_be_resolved(
        self, monkeypatch
    ) -> None:
        def boom(path, **kwargs):
            raise common.ConfigError("no config file found")

        monkeypatch.setattr(common, "resolve_and_load_config", boom)
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is False
        escalation = state.get_drive_escalation("acme/widgets", 40)
        assert escalation is not None

    def test_never_dispatches_a_fixit_leg_for_a_sealed_confined_integration_red(
        self, monkeypatch,
    ) -> None:
        # A merged-tree failure confined to the repo's own sealed
        # acceptance paths is never fixable by a retry — a fix-it leg
        # would only re-confirm the `STUCK:` its own ci-fix briefing
        # already refuses to touch, so this must page a design decision
        # instead of dispatching.
        from coord.ci_store import CIFailureDetail
        from coord.config import AcceptanceDriverConfig

        config = Config(repos=[Repo(name="acme/widgets", github="acme/widgets")], machines=[])
        config.acceptance.drivers["acme/widgets"] = AcceptanceDriverConfig(kind="cli-pytest")
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: config)
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)

        board = Board(
            active=[
                Assignment(
                    assignment_id="asn-61",
                    repo_name="acme/widgets",
                    machine_name="m1",
                    issue_number=61,
                    issue_title="widget",
                    status="in_review",
                    branch="issue-61-widget",
                )
            ]
        )
        monkeypatch.setattr(state, "build_board", lambda: board)
        monkeypatch.setattr(
            "coord.github_ops.find_pr_for_branch",
            lambda repo, branch: {"number": 77},
        )
        monkeypatch.setattr(
            "coord.ci_store.build_ci_store", lambda *a, **k: object(),
        )
        monkeypatch.setattr(
            "coord.ci_github.build_ci_failure_detail",
            lambda ci_store, repo, number: CIFailureDetail(
                check_name="coord acceptance run --all --ci (cli-pytest route, ms-37)",
                job_name="coord acceptance run --all --ci (cli-pytest route, ms-37)",
                step_name="pytest",
                log_excerpt=(
                    "FAILED tests/acceptance/test_descriptor.py::test_x - "
                    "AssertionError"
                ),
                run_url="https://example.com/run/1",
            ),
        )

        def dispatch_must_not_be_called(*args, **kwargs):
            raise AssertionError(
                "dispatch_sweep_action must not run for a sealed-confined row"
            )

        monkeypatch.setattr(
            board_doctor_policy, "dispatch_sweep_action", dispatch_must_not_be_called
        )
        row = _row(
            "acme/widgets#61",
            queue_diagnose.CAUSE_INTEGRATION_RED,
            evidence=(
                "gh pr checks: 1 check(s) — 1 failure "
                "(coord acceptance run --all --ci (cli-pytest route, ms-37))",
            ),
        )

        assert _investigate(row) is False
        escalation = state.get_drive_escalation("acme/widgets", 61)
        assert escalation is not None
        assert "needs a design decision" in escalation["reason"]

    def test_falls_back_to_the_alert_tier_when_the_fix_round_ceiling_blocks_it(
        self, monkeypatch
    ) -> None:
        # #3756/#3454: an issue whose drive-queue row already gave up on the
        # #2972 fix-round ceiling must not get an unbounded stream of
        # investigate-dispatched legs on top of that (vimcode#2018's ~14
        # legs/$31 overnight is exactly the failure mode this guards
        # against) — `_investigate` must consult the ceiling BEFORE it ever
        # resolves config or calls `dispatch_sweep_action`.
        calls: list[tuple[str, int]] = []

        def fake_ceiling_blocked(repo_name: str, issue_number: int) -> bool:
            calls.append((repo_name, issue_number))
            return True

        monkeypatch.setattr(
            "coord.drive_queue.fix_round_ceiling_blocked_for_issue",
            fake_ceiling_blocked,
        )

        def boom_if_called(path, **kwargs):
            raise AssertionError(
                "config must not be resolved once the ceiling check blocks"
            )

        monkeypatch.setattr(common, "resolve_and_load_config", boom_if_called)

        def dispatch_must_not_be_called(*args, **kwargs):
            raise AssertionError(
                "dispatch_sweep_action must not run once the ceiling check blocks"
            )

        monkeypatch.setattr(
            board_doctor_policy, "dispatch_sweep_action", dispatch_must_not_be_called
        )

        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is False
        assert calls == [("acme/widgets", 40)]

        escalation = state.get_drive_escalation("acme/widgets", 40)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"

    def test_ceiling_bypass_dispatches_a_fixit_leg_for_a_high_confidence_merge_conflict(
        self, monkeypatch
    ) -> None:
        # #3777: the ceiling exists to stop a BLIND relaunch loop, not to
        # suppress a fix-it leg a fresh, high-confidence diagnosis
        # specifically calls for — a merge-conflict row still dispatches
        # even though the drive-queue row already gave up on the #2972
        # ceiling.
        monkeypatch.setattr(
            "coord.drive_queue.fix_round_ceiling_blocked_for_issue",
            lambda repo_name, issue_number: True,
        )
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: object())
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {
                "dispatched": True,
                "assignment_id": "asn-1",
                "machine": "mac1",
                "detail": "acme/widgets#40 fix-it leg dispatched to mac1 (iteration 1)",
            },
        )
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_MERGE_CONFLICT)

        assert _investigate(row) is True
        assert state.list_drive_escalations() == []

    def test_ceiling_still_blocks_a_merge_conflict_row_below_high_confidence(
        self, monkeypatch
    ) -> None:
        # The bypass is HIGH-confidence only — a merge-conflict diagnosis
        # that is not the fleet's strongest signal still falls back to the
        # alert tier exactly like any other ceiling-blocked row.
        monkeypatch.setattr(
            "coord.drive_queue.fix_round_ceiling_blocked_for_issue",
            lambda repo_name, issue_number: True,
        )

        def boom_if_called(path, **kwargs):
            raise AssertionError(
                "config must not be resolved once the ceiling check blocks"
            )

        monkeypatch.setattr(common, "resolve_and_load_config", boom_if_called)

        def dispatch_must_not_be_called(*args, **kwargs):
            raise AssertionError(
                "dispatch_sweep_action must not run once the ceiling check blocks"
            )

        monkeypatch.setattr(
            board_doctor_policy, "dispatch_sweep_action", dispatch_must_not_be_called
        )

        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#40",
            state="stuck",
            cause=queue_diagnose.CAUSE_MERGE_CONFLICT,
            confidence=queue_diagnose.CONFIDENCE_MEDIUM,
            evidence=("cause=merge-conflict",),
            fresh=True,
        )

        assert _investigate(row) is False
        escalation = state.get_drive_escalation("acme/widgets", 40)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"

    def test_ceiling_still_blocks_a_stale_high_confidence_merge_conflict_row(
        self, monkeypatch
    ) -> None:
        # #3777: a HIGH-confidence merge-conflict row is only new evidence
        # when it is this sweep's own live verdict (`StuckRow.fresh`) — a
        # row reconstructed from a possibly arbitrarily-old stored
        # `block_log` diagnosis (`fresh=False`, e.g. via
        # `coord.board_doctor._episode_stuck_row`) must not bypass the
        # ceiling just because it happens to carry a stale high-confidence
        # cause/confidence pair.
        monkeypatch.setattr(
            "coord.drive_queue.fix_round_ceiling_blocked_for_issue",
            lambda repo_name, issue_number: True,
        )

        def boom_if_called(path, **kwargs):
            raise AssertionError(
                "config must not be resolved once the ceiling check blocks"
            )

        monkeypatch.setattr(common, "resolve_and_load_config", boom_if_called)

        def dispatch_must_not_be_called(*args, **kwargs):
            raise AssertionError(
                "dispatch_sweep_action must not run once the ceiling check blocks"
            )

        monkeypatch.setattr(
            board_doctor_policy, "dispatch_sweep_action", dispatch_must_not_be_called
        )

        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#40",
            state="stuck",
            cause=queue_diagnose.CAUSE_MERGE_CONFLICT,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=("cause=merge-conflict",),
            fresh=False,
        )

        assert _investigate(row) is False
        escalation = state.get_drive_escalation("acme/widgets", 40)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"

    def test_unparseable_key_does_not_raise(self) -> None:
        row = _row("no-hash-separator", queue_diagnose.CAUSE_CI_RED)

        assert _investigate(row) is False
        assert state.list_drive_escalations() == []


class TestFixitRebudgetEnvironmental:
    """#3847's pure classifier: cause/confidence/freshness only, zero
    `coord.state` reads — the half of :func:`_fixit_rebudget_eligible`
    that never costs anything regardless of how many rows a sweep
    classifies.
    """

    def test_high_confidence_fresh_merge_conflict_is_environmental(self) -> None:
        row = _row(
            "acme/widgets#40",
            queue_diagnose.CAUSE_MERGE_CONFLICT,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            fresh=True,
        )

        assert _fixit_rebudget_environmental(row) is True

    def test_medium_confidence_merge_conflict_is_flailing(self) -> None:
        # #3777: merge-conflict keeps its original HIGH-only bar — a
        # weaker diagnosis is not the new evidence the bypass exists for.
        row = _row(
            "acme/widgets#40",
            queue_diagnose.CAUSE_MERGE_CONFLICT,
            confidence=queue_diagnose.CONFIDENCE_MEDIUM,
            fresh=True,
        )

        assert _fixit_rebudget_environmental(row) is False

    def test_ceiling_blocked_mergeable_at_medium_confidence_is_environmental(
        self,
    ) -> None:
        # queue_diagnose never assigns this cause CONFIDENCE_HIGH, so the
        # bar is "not LOW" rather than "exactly HIGH" (otherwise this
        # cause could never be admitted at all).
        row = _row(
            "acme/widgets#40",
            CAUSE_CEILING_BLOCKED_MERGEABLE,
            confidence=queue_diagnose.CONFIDENCE_MEDIUM,
            fresh=True,
        )

        assert _fixit_rebudget_environmental(row) is True

    def test_ci_green_at_medium_confidence_is_environmental(self) -> None:
        row = _row(
            "acme/widgets#40",
            queue_diagnose.CAUSE_CI_GREEN,
            confidence=queue_diagnose.CONFIDENCE_MEDIUM,
            fresh=True,
        )

        assert _fixit_rebudget_environmental(row) is True

    def test_low_confidence_ci_green_is_flailing(self) -> None:
        row = _row(
            "acme/widgets#40",
            queue_diagnose.CAUSE_CI_GREEN,
            confidence=queue_diagnose.CONFIDENCE_LOW,
            fresh=True,
        )

        assert _fixit_rebudget_environmental(row) is False

    def test_a_stale_row_never_reads_as_environmental(self) -> None:
        # #3777: cause/confidence only carry "new evidence" meaning when
        # THIS sweep computed them live — a row rebuilt from a possibly
        # arbitrarily old `block_log` entry must not bypass anything on
        # the strength of a stale verdict.
        row = _row(
            "acme/widgets#40",
            CAUSE_CEILING_BLOCKED_MERGEABLE,
            confidence=queue_diagnose.CONFIDENCE_MEDIUM,
            fresh=False,
        )

        assert _fixit_rebudget_environmental(row) is False

    def test_an_unrelated_cause_is_flailing_regardless_of_confidence(self) -> None:
        row = _row(
            "acme/widgets#40",
            queue_diagnose.CAUSE_CI_RED,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            fresh=True,
        )

        assert _fixit_rebudget_environmental(row) is False

    def test_never_touches_coord_state(self, monkeypatch) -> None:
        # #3767: the classifier itself must add zero cost to the
        # decision path — not even a state-module import, let alone a
        # DB read or an LLM/agent dispatch.
        import coord.state as real_state

        def boom(*args, **kwargs):
            raise AssertionError(
                "the pure classifier must never call into coord.state"
            )

        monkeypatch.setattr(real_state, "board_doctor_fixit_rebudget_spent", boom)
        monkeypatch.setattr(
            real_state, "record_board_doctor_fixit_rebudget_spent", boom
        )

        row = _row(
            "acme/widgets#40",
            queue_diagnose.CAUSE_MERGE_CONFLICT,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            fresh=True,
        )

        assert _fixit_rebudget_environmental(row) is True


class TestFixitRebudgetEligible:
    """The gated half of #3847's decision: the global, remove+add-
    resistant cap layered on top of :func:`_fixit_rebudget_environmental`.
    """

    def _environmental_row(self) -> StuckRow:
        return _row(
            "acme/widgets#40",
            CAUSE_CEILING_BLOCKED_MERGEABLE,
            confidence=queue_diagnose.CONFIDENCE_MEDIUM,
            fresh=True,
        )

    def test_environmental_and_unspent_is_eligible(self) -> None:
        row = self._environmental_row()

        assert _fixit_rebudget_eligible(row, "acme/widgets", 40) is True

    def test_flailing_and_unspent_is_not_eligible(self) -> None:
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _fixit_rebudget_eligible(row, "acme/widgets", 40) is False

    def test_environmental_but_already_spent_is_not_eligible(self) -> None:
        state.record_board_doctor_fixit_rebudget_spent("acme/widgets", 40)
        row = self._environmental_row()

        assert _fixit_rebudget_eligible(row, "acme/widgets", 40) is False

    def test_spent_marker_is_scoped_to_its_own_issue(self) -> None:
        state.record_board_doctor_fixit_rebudget_spent("acme/widgets", 40)
        row = self._environmental_row()

        assert _fixit_rebudget_eligible(row, "acme/widgets", 41) is True
        assert _fixit_rebudget_eligible(row, "other/repo", 40) is True


class TestRequeueCeilingBlockedMergeable:
    """#3760 Action 1, previously covered only via the allowlist's
    cause->action string mapping — this exercises the function's actual
    behavior, gated through #3847's shared eligibility check.
    """

    def _row(
        self, confidence: str = queue_diagnose.CONFIDENCE_MEDIUM
    ) -> StuckRow:
        return _row(
            "acme/widgets#40",
            CAUSE_CEILING_BLOCKED_MERGEABLE,
            confidence=confidence,
            fresh=True,
        )

    def test_dispatches_and_records_the_spend_on_confirmed_requeue(
        self, monkeypatch
    ) -> None:
        calls: list[tuple] = []

        def fake_dispatch(action, repo_name, issue_number, *args, **kwargs):
            calls.append((action, repo_name, issue_number))
            return {"requeued": True, "detail": "requeued"}

        monkeypatch.setattr(
            board_doctor_policy, "dispatch_sweep_action", fake_dispatch
        )

        row = self._row()

        assert _requeue_ceiling_blocked_mergeable(row) is True
        assert calls == [("drive_queue_requeue", "acme/widgets", 40)]
        assert state.board_doctor_fixit_rebudget_spent("acme/widgets", 40) is True

    def test_already_spent_refuses_without_dispatching(self, monkeypatch) -> None:
        state.record_board_doctor_fixit_rebudget_spent("acme/widgets", 40)

        def boom(*args, **kwargs):
            raise AssertionError(
                "dispatch_sweep_action must not run once the row already "
                "spent its one lifetime fix-it auto-rebudget leg"
            )

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", boom)

        row = self._row()

        assert _requeue_ceiling_blocked_mergeable(row) is False

    def test_flailing_row_refuses_without_dispatching(self, monkeypatch) -> None:
        def boom(*args, **kwargs):
            raise AssertionError(
                "dispatch_sweep_action must not run for a flailing row"
            )

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", boom)

        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        assert _requeue_ceiling_blocked_mergeable(row) is False
        assert state.board_doctor_fixit_rebudget_spent("acme/widgets", 40) is False

    def test_a_reported_noop_does_not_burn_the_lifetime_leg(self, monkeypatch) -> None:
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {"requeued": False, "detail": "no longer queued"},
        )

        row = self._row()

        assert _requeue_ceiling_blocked_mergeable(row) is False
        assert state.board_doctor_fixit_rebudget_spent("acme/widgets", 40) is False

    def test_unparseable_key_does_not_raise(self) -> None:
        row = _row(
            "no-hash-separator",
            CAUSE_CEILING_BLOCKED_MERGEABLE,
            confidence=queue_diagnose.CONFIDENCE_MEDIUM,
            fresh=True,
        )

        assert _requeue_ceiling_blocked_mergeable(row) is False


class TestRequeueNeverDispatched:
    """#3874: `queue_diagnose.CAUSE_NEVER_DISPATCHED` fires straight off a
    live signal — no eligibility gate like #3847's rebudget — but the
    action still re-confirms "nothing was ever dispatched" against the
    board before it removes anything, because a `StuckRow` rebuilt from a
    STORED diagnosis (`coord.board_doctor._episode_stuck_row`, `fresh=
    False`) can be arbitrarily old."""

    def _row(self) -> StuckRow:
        return _row(
            "acme/widgets#2035",
            queue_diagnose.CAUSE_NEVER_DISPATCHED,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            fresh=True,
        )

    def _empty_board(self, monkeypatch) -> None:
        monkeypatch.setattr(state, "build_board", lambda: Board())

    def _board_with_live_work(self, monkeypatch) -> None:
        monkeypatch.setattr(
            state,
            "build_board",
            lambda: Board(
                active=[
                    Assignment(
                        machine_name="mac1",
                        repo_name="acme/widgets",
                        issue_number=2035,
                        issue_title="t",
                        status="running",
                        type="work",
                        branch="issue-2035-live",
                    )
                ]
            ),
        )

    def test_dispatches_the_requeue(self, monkeypatch) -> None:
        calls: list[tuple] = []

        def fake_dispatch(action, repo_name, issue_number, *args, **kwargs):
            calls.append((action, repo_name, issue_number))
            return {"requeued": True, "detail": "requeued"}

        self._empty_board(monkeypatch)
        monkeypatch.setattr(
            board_doctor_policy, "dispatch_sweep_action", fake_dispatch
        )

        assert _requeue_never_dispatched(self._row()) is True
        assert calls == [("drive_queue_requeue", "acme/widgets", 2035)]

    def test_a_reported_noop_returns_false(self, monkeypatch) -> None:
        self._empty_board(monkeypatch)
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {"requeued": False, "detail": "no longer queued"},
        )

        assert _requeue_never_dispatched(self._row()) is False

    def test_a_mid_flight_deploy_gate_refusal_returns_false(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise SweepPermissionError("acme/widgets#2035's deploy gate is mid-flight")

        self._empty_board(monkeypatch)
        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", boom)

        assert _requeue_never_dispatched(self._row()) is False

    def test_unparseable_key_does_not_raise(self) -> None:
        row = _row(
            "no-hash-separator",
            queue_diagnose.CAUSE_NEVER_DISPATCHED,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            fresh=True,
        )

        assert _requeue_never_dispatched(row) is False

    def test_a_row_that_now_has_live_work_is_never_requeued(self, monkeypatch) -> None:
        """The hazard the re-check exists for: a stored `never-dispatched`
        verdict reaching the sweep after the issue HAS been dispatched.
        `drive_queue_requeue` is a remove plus a re-add, so firing it here
        would orphan the live branch and re-dispatch Work on top of it."""
        calls: list[tuple] = []

        self._board_with_live_work(monkeypatch)
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: calls.append(a) or {"requeued": True},
        )

        assert _requeue_never_dispatched(self._row()) is False
        assert calls == []

    def test_an_unreadable_board_refuses_rather_than_assuming_empty(
        self, monkeypatch
    ) -> None:
        """A board read that raises says nothing about whether work exists,
        so it must fail closed — the same "absence of a reading is not a
        reading" rule `queue_diagnose` mints the cause under."""
        calls: list[tuple] = []

        def boom() -> Board:
            raise TimeoutError("board read timed out")

        monkeypatch.setattr(state, "build_board", boom)
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: calls.append(a) or {"requeued": True},
        )

        assert _requeue_never_dispatched(self._row()) is False
        assert calls == []

    def test_the_recheck_matches_a_tracking_issue_assignment(
        self, monkeypatch
    ) -> None:
        """The re-check reuses `coord.gates.assignments_for_issue`, so an
        assignment booked to a tracking issue but FOR this one (#1553's
        effective issue) counts as dispatched here too — one question, one
        answer, rather than a second `issue_number ==` scan."""
        monkeypatch.setattr(
            state,
            "build_board",
            lambda: Board(
                completed=[
                    Assignment(
                        machine_name="mac1",
                        repo_name="acme/widgets",
                        issue_number=9000,
                        for_issue_number=2035,
                        issue_title="t",
                        status="completed",
                        type="work",
                    )
                ]
            ),
        )

        assert (
            board_doctor_policy._never_dispatched_still_true("acme/widgets", 2035)
            is False
        )


class TestFixitRebudgetGlobalCap:
    """#3847's load-bearing safety property: at most one auto-rebudget
    per issue, fleet-wide, surviving a `drive-queue remove`+`add` — not a
    per-row budget either call site can reset on its own.
    """

    def test_a_second_ceiling_hit_stays_terminal_across_remove_and_add(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {"requeued": True, "detail": "requeued"},
        )

        first_row = _row(
            "acme/widgets#40",
            CAUSE_CEILING_BLOCKED_MERGEABLE,
            confidence=queue_diagnose.CONFIDENCE_MEDIUM,
            fresh=True,
        )
        assert _requeue_ceiling_blocked_mergeable(first_row) is True

        # Simulate `drive-queue remove`+`add`: a brand-new row for the
        # SAME (repo, issue), freshly re-diagnosed as environmental all
        # over again — the row-level `fresh` flag and ceiling check both
        # reset, but the global cap must not.
        second_row = _row(
            "acme/widgets#40",
            CAUSE_CEILING_BLOCKED_MERGEABLE,
            confidence=queue_diagnose.CONFIDENCE_MEDIUM,
            fresh=True,
        )

        def boom(*args, **kwargs):
            raise AssertionError(
                "dispatch_sweep_action must not run a second time for an "
                "issue that already spent its one lifetime fix-it "
                "auto-rebudget leg"
            )

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", boom)

        assert _requeue_ceiling_blocked_mergeable(second_row) is False

    def test_the_cap_is_shared_across_both_call_sites(self, monkeypatch) -> None:
        # Spend the lifetime leg via the mechanical requeue call site...
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {"requeued": True, "detail": "requeued"},
        )
        requeue_row = _row(
            "acme/widgets#40",
            CAUSE_CEILING_BLOCKED_MERGEABLE,
            confidence=queue_diagnose.CONFIDENCE_MEDIUM,
            fresh=True,
        )
        assert _requeue_ceiling_blocked_mergeable(requeue_row) is True

        # ...and confirm the investigate tier's independent ceiling-bypass
        # branch, for the SAME issue, is also refused — proving this is
        # one shared cap, not two call sites each keeping their own
        # judgment (#2096).
        monkeypatch.setattr(
            "coord.drive_queue.fix_round_ceiling_blocked_for_issue",
            lambda repo_name, issue_number: True,
        )

        def boom(*args, **kwargs):
            raise AssertionError(
                "config must not be resolved once the shared cap refuses"
            )

        monkeypatch.setattr(common, "resolve_and_load_config", boom)

        investigate_row = _row(
            "acme/widgets#40",
            queue_diagnose.CAUSE_MERGE_CONFLICT,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            fresh=True,
        )

        assert _investigate(investigate_row) is False
        escalation = state.get_drive_escalation("acme/widgets", 40)
        assert escalation is not None
        assert escalation["stage"] == "board-doctor:drive-queue"


class TestInvestigateCapacityCheck:
    """The investigate tier's own per-repo fix-it-leg budget
    (`FIXIT_LEG_BUDGET_DEFAULT`, deliberately independent of a repo's
    `max_parallel` throughput ceiling) bounds how many fix-it legs one
    sweep dispatches — the rest defer to the next hourly sweep instead of
    fanning out unbounded, and a deferred row carries no convergence or
    escalation side effect of its own. Every test here is given a
    `max_parallel` far above the budget, so a dispatch that got through
    on ordinary queue capacity rather than the budget would show up as a
    failure here.
    """

    @staticmethod
    def _config(max_parallel: int) -> Config:
        return Config(
            repos=[
                Repo(name="acme/widgets", github="acme/widgets", max_parallel=max_parallel)
            ],
            machines=[],
        )

    def test_at_the_cap_the_rest_defer_with_no_escalation(self, monkeypatch) -> None:
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: Board(active=[]))

        dispatched: list[str] = []

        def fake_dispatch(action, repo_name, issue_number, board, config, **kwargs):
            dispatched.append(f"{repo_name}#{issue_number}")
            return {
                "dispatched": True,
                "assignment_id": "asn-1",
                "machine": "mac1",
                "detail": "dispatched",
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)

        def alert_must_not_fire(row: StuckRow):
            raise AssertionError("a deferred row must never reach the alert tier")

        config = self._config(max_parallel=5)
        capacity_check = make_investigate_capacity_check(config)
        cause = queue_diagnose.CAUSE_CI_RED
        rows = [_row(f"acme/widgets#{n}", cause) for n in (40, 41, 42)]

        report = sweep(
            rows,
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=alert_must_not_fire,
            capacity_check=capacity_check,
        )

        actions = {d.key: d.action for d in report.decisions}
        assert actions["acme/widgets#40"] == "auto-dispatch"
        assert actions["acme/widgets#41"] == "investigate-deferred"
        assert actions["acme/widgets#42"] == "investigate-deferred"
        assert dispatched == ["acme/widgets#40"]

        # Deferred rows never had their convergence counter bumped, and no
        # escalation was ever filed for them.
        assert state.board_doctor_row_state("drive-queue", "acme/widgets#41") == {}
        assert state.board_doctor_row_state("drive-queue", "acme/widgets#42") == {}
        for d in report.decisions:
            if d.action == "investigate-deferred":
                assert d.escalated is False
        assert state.list_drive_escalations() == []

    def test_the_next_sweep_dispatches_the_deferred_row(self, monkeypatch) -> None:
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: Board(active=[]))

        dispatched: list[str] = []

        def fake_dispatch(action, repo_name, issue_number, board, config, **kwargs):
            dispatched.append(f"{repo_name}#{issue_number}")
            return {
                "dispatched": True,
                "assignment_id": "asn-1",
                "machine": "mac1",
                "detail": "dispatched",
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)

        config = self._config(max_parallel=5)
        cause = queue_diagnose.CAUSE_CI_RED
        rows = [_row(f"acme/widgets#{n}", cause) for n in (40, 41, 42)]

        # First sweep: one gate, built fresh for this sweep (as a real
        # hourly sweep does) — only the first row gets through.
        sweep(
            rows,
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False,
            capacity_check=make_investigate_capacity_check(config),
        )
        assert dispatched == ["acme/widgets#40"]

        # Second (hourly) sweep: row 40 has since resolved (no longer part
        # of the stuck set `collect_rows` would hand `sweep`) and its slot
        # is free again — a brand-new gate, same board/config, now lets
        # row 41 through instead of re-dispatching row 40 a second time.
        # This is the actual acceptance criterion: "the next sweep
        # dispatches the next one," not the same row forever.
        rows2 = [_row(f"acme/widgets#{n}", cause) for n in (41, 42)]
        report2 = sweep(
            rows2,
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False,
            capacity_check=make_investigate_capacity_check(config),
        )

        actions2 = {d.key: d.action for d in report2.decisions}
        assert actions2["acme/widgets#41"] == "auto-dispatch"
        assert actions2["acme/widgets#42"] == "investigate-deferred"
        assert dispatched == ["acme/widgets#40", "acme/widgets#41"]

    def test_ordinary_work_does_not_count_toward_the_fixit_budget(self, monkeypatch) -> None:
        # The fix-it-leg budget is the doctor's OWN, separate from a
        # repo's `max_parallel` ceiling — an ordinary "work" assignment
        # busy on the repo's normal queue must not starve the investigate
        # tier.
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        busy_board = Board(
            active=[
                Assignment(
                    machine_name="mac1",
                    repo_name="acme/widgets",
                    issue_number=40,
                    issue_title="t",
                    status="running",
                    type="work",
                )
            ]
        )
        monkeypatch.setattr(state, "build_board", lambda: busy_board)

        dispatched: list[str] = []

        def fake_dispatch(action, repo_name, issue_number, board, config, **kwargs):
            dispatched.append(f"{repo_name}#{issue_number}")
            return {
                "dispatched": True,
                "assignment_id": "asn-41",
                "machine": "mac1",
                "detail": "dispatched",
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)

        config = self._config(max_parallel=1)
        capacity_check = make_investigate_capacity_check(config)
        cause = queue_diagnose.CAUSE_CI_RED
        rows = [_row("acme/widgets#41", cause)]

        report = sweep(
            rows,
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False,
            capacity_check=capacity_check,
        )

        # `max_parallel=1`, and that single slot already busy with
        # ordinary work: a gate keyed off the repo's throughput ceiling
        # would defer this row; the dedicated budget dispatches it.
        assert report.decisions[0].action == "auto-dispatch"
        assert dispatched == ["acme/widgets#41"]

    def test_a_dispatched_fixit_leg_counts_toward_its_own_budget(self, monkeypatch) -> None:
        # Only a leg THIS doctor dispatched — identified by the
        # `fixit_leg_dispatched` audit event it wrote on its own confirmed
        # dispatch — occupies the dedicated budget.
        from coord.audit import record_audit

        record_audit(
            tier="operational",
            category="board_doctor",
            event_type="fixit_leg_dispatched",
            actor="daemon",
            summary="board-doctor dispatched a fix-it leg",
            repo="acme/widgets",
            issue=40,
            assignment_id="asn-40",
            machine="mac1",
        )
        busy_board = Board(
            active=[
                Assignment(
                    machine_name="mac1",
                    repo_name="acme/widgets",
                    issue_number=40,
                    issue_title="t",
                    status="running",
                    type="work",
                    assignment_id="asn-40",
                )
            ]
        )
        monkeypatch.setattr(state, "build_board", lambda: busy_board)

        def dispatch_must_not_be_called(*args, **kwargs):
            raise AssertionError("an at-capacity repo must not dispatch")

        monkeypatch.setattr(
            board_doctor_policy, "dispatch_sweep_action", dispatch_must_not_be_called
        )

        config = self._config(max_parallel=5)
        capacity_check = make_investigate_capacity_check(config)
        cause = queue_diagnose.CAUSE_CI_RED
        rows = [_row("acme/widgets#41", cause)]

        report = sweep(
            rows,
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False,
            capacity_check=capacity_check,
        )

        assert report.decisions[0].action == "investigate-deferred"

    def test_a_non_investigate_cause_is_never_capacity_gated(self) -> None:
        # #3760's C2 allowlist (mechanical fixes) is untouched by the
        # investigate tier's cap — only INVESTIGATE_CAUSES rows ever reach
        # the gate's cap-comparison logic at all.
        config = self._config(max_parallel=1)
        capacity_check = make_investigate_capacity_check(config)
        row = _row("acme/widgets#40", CAUSE_CEILING_BLOCKED_MERGEABLE)

        assert capacity_check(row) is False

    def test_an_unlimited_repo_still_gets_only_its_fixit_budget(
        self, monkeypatch
    ) -> None:
        # `max_parallel=0` means "no per-repo ceiling at all" for ordinary
        # drive-queue throughput — it does NOT buy unlimited fix-it legs.
        # Such a repo still dispatches when its budget is free, and still
        # defers once a leg this doctor started occupies it.
        from coord.audit import record_audit

        config = self._config(max_parallel=0)
        row = _row("acme/widgets#40", queue_diagnose.CAUSE_CI_RED)

        monkeypatch.setattr(state, "build_board", lambda: Board(active=[]))
        assert make_investigate_capacity_check(config)(row) is False

        record_audit(
            tier="operational",
            category="board_doctor",
            event_type="fixit_leg_dispatched",
            actor="daemon",
            summary="board-doctor dispatched a fix-it leg",
            repo="acme/widgets",
            issue=41,
            assignment_id="asn-41",
            machine="mac1",
        )
        monkeypatch.setattr(
            state,
            "build_board",
            lambda: Board(
                active=[
                    Assignment(
                        machine_name="mac1",
                        repo_name="acme/widgets",
                        issue_number=41,
                        issue_title="t",
                        status="running",
                        type="work",
                        assignment_id="asn-41",
                    )
                ]
            ),
        )
        assert make_investigate_capacity_check(config)(row) is True


class TestInvestigateConvergence:
    def test_same_row_escalates_once_after_the_convergence_limit(
        self, monkeypatch
    ) -> None:
        # #3769's last acceptance criterion: this is `coord.board_doctor
        # .sweep`'s existing #3758 convergence contract (untouched by this
        # issue) exercised against the real `policy_tier_of` /
        # `AUTO_DISPATCHERS` / `escalation_alert_dispatcher` this module
        # wires in, not a new mechanism — the same row left alone every
        # sweep (drive loop still on it, so `_investigate` never alerts on
        # its own) nonetheless escalates exactly once once the sweep count
        # reaches CONVERGENCE_LIMIT, and stops being handed to
        # `_investigate` at all after that.
        monkeypatch.setattr(common, "resolve_and_load_config", lambda path, **k: object())
        monkeypatch.setattr(board_doctor_policy, "_investigate_tier_enabled", lambda *a, **k: True)
        monkeypatch.setattr(state, "build_board", lambda: object())

        calls: list[int] = []

        def fake_dispatch(*args, **kwargs):
            calls.append(1)
            return {
                "dispatched": False,
                "assignment_id": None,
                "machine": None,
                "detail": _IN_FLIGHT_DETAIL,
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)

        escalation_calls: list[StuckRow] = []

        def counting_alert_dispatcher(row: StuckRow):
            escalation_calls.append(row)
            return escalation_alert_dispatcher(row)

        key = "acme/widgets#40"
        cause = queue_diagnose.CAUSE_CI_RED
        for _ in range(5):
            sweep(
                [_row(key, cause)],
                tier_of=policy_tier_of,
                auto_dispatchers=AUTO_DISPATCHERS,
                alert_dispatcher=counting_alert_dispatcher,
                convergence_limit=CONVERGENCE_LIMIT,
            )

        # Sweeps 1-2 dispatch (left alone, no alert); sweep 3 hits the
        # convergence limit and escalates via the alert tier exactly once,
        # bypassing `_investigate` entirely; sweeps 4-5 neither re-dispatch
        # nor re-alert. Counting the alert dispatcher's own calls (not just
        # the end-state escalation row) pins "exactly once", not merely
        # "at least once".
        assert len(calls) == CONVERGENCE_LIMIT - 1
        assert len(escalation_calls) == 1
        row_state = state.board_doctor_row_state("drive-queue", key)
        assert row_state["escalated"] is True

        escalation = state.get_drive_escalation("acme/widgets", 40)
        assert escalation is not None


class TestFinisherTierEnabled:
    """#3835 Trigger A: `_finisher_tier_enabled` mirrors
    `_investigate_tier_enabled`'s three independent gates, but reads the
    `finish`-named fields.
    """

    @staticmethod
    def _config(*, fleet_finish: bool, repo_finish: bool) -> Config:
        from coord.config import BoardDoctorConfig
        from coord.models import BoardDoctorRepoConfig

        return Config(
            repos=[
                Repo(
                    name="acme/widgets",
                    github="acme/widgets",
                    board_doctor=BoardDoctorRepoConfig(finish=repo_finish),
                )
            ],
            machines=[],
            board_doctor=BoardDoctorConfig(finish=fleet_finish),
        )

    def test_disabled_when_repo_has_not_opted_in(self) -> None:
        config = self._config(fleet_finish=True, repo_finish=False)
        assert board_doctor_policy._finisher_tier_enabled("acme/widgets", config) is False

    def test_disabled_when_fleet_switch_is_off(self) -> None:
        config = self._config(fleet_finish=False, repo_finish=True)
        assert board_doctor_policy._finisher_tier_enabled("acme/widgets", config) is False

    def test_enabled_when_both_gates_are_on(self) -> None:
        config = self._config(fleet_finish=True, repo_finish=True)
        assert board_doctor_policy._finisher_tier_enabled("acme/widgets", config) is True

    def test_env_var_kill_switch_overrides_everything(self, monkeypatch) -> None:
        monkeypatch.setenv(board_doctor_policy.NO_FINISHER_ENV_VAR, "1")
        config = self._config(fleet_finish=True, repo_finish=True)
        assert board_doctor_policy._finisher_tier_enabled("acme/widgets", config) is False

    def test_unknown_repo_is_disabled(self) -> None:
        config = self._config(fleet_finish=True, repo_finish=True)
        assert board_doctor_policy._finisher_tier_enabled("no-such-repo", config) is False


class TestStaleRaceTierEnabled:
    """#3859: `_stale_race_tier_enabled` mirrors `_investigate_tier_enabled`/
    `_finisher_tier_enabled`'s three independent gates, reading the
    `stale_race`-named fields.
    """

    @staticmethod
    def _config(*, fleet_stale_race: bool, repo_stale_race: bool) -> Config:
        from coord.config import BoardDoctorConfig
        from coord.models import BoardDoctorRepoConfig

        return Config(
            repos=[
                Repo(
                    name="acme/widgets",
                    github="acme/widgets",
                    board_doctor=BoardDoctorRepoConfig(stale_race=repo_stale_race),
                )
            ],
            machines=[],
            board_doctor=BoardDoctorConfig(stale_race=fleet_stale_race),
        )

    def test_disabled_when_repo_has_not_opted_in(self) -> None:
        config = self._config(fleet_stale_race=True, repo_stale_race=False)
        assert board_doctor_policy._stale_race_tier_enabled("acme/widgets", config) is False

    def test_disabled_when_fleet_switch_is_off(self) -> None:
        config = self._config(fleet_stale_race=False, repo_stale_race=True)
        assert board_doctor_policy._stale_race_tier_enabled("acme/widgets", config) is False

    def test_enabled_when_both_gates_are_on(self) -> None:
        config = self._config(fleet_stale_race=True, repo_stale_race=True)
        assert board_doctor_policy._stale_race_tier_enabled("acme/widgets", config) is True

    def test_env_var_kill_switch_overrides_everything(self, monkeypatch) -> None:
        monkeypatch.setenv(board_doctor_policy.NO_STALE_RACE_ENV_VAR, "1")
        config = self._config(fleet_stale_race=True, repo_stale_race=True)
        assert board_doctor_policy._stale_race_tier_enabled("acme/widgets", config) is False

    def test_unknown_repo_is_disabled(self) -> None:
        config = self._config(fleet_stale_race=True, repo_stale_race=True)
        assert board_doctor_policy._stale_race_tier_enabled("no-such-repo", config) is False


class TestDispatchNitOnlyFinisher:
    """#3835 Trigger A: `dispatch_nit_only_finisher` is a plain, directly
    called function — not part of the `StuckRow` sweep's
    `ALLOWLIST`/`AUTO_DISPATCHERS` machinery — so it is exercised here
    directly rather than through `sweep`/`policy_tier_of`.
    """


    def test_returns_false_without_dispatching_when_tier_disabled(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            board_doctor_policy, "_finisher_tier_enabled", lambda *a, **k: False
        )

        def dispatch_must_not_be_called(*args, **kwargs):
            raise AssertionError("tier-off must never reach dispatch_sweep_action")

        monkeypatch.setattr(
            board_doctor_policy, "dispatch_sweep_action", dispatch_must_not_be_called
        )
        config = Config(repos=[Repo(name="acme/widgets", github="acme/widgets")], machines=[])

        result = board_doctor_policy.dispatch_nit_only_finisher(
            "acme/widgets", 40, Board(), config
        )

        assert result is False

    def test_returns_true_when_the_sweep_action_dispatches(self, monkeypatch) -> None:
        monkeypatch.setattr(
            board_doctor_policy, "_finisher_tier_enabled", lambda *a, **k: True
        )
        calls: list[tuple] = []

        def fake_dispatch(action, *args, **kwargs):
            calls.append((action, args, kwargs))
            return {
                "dispatched": True,
                "assignment_id": "asn-1",
                "machine": "mac1",
                "detail": "acme/widgets#40 finisher leg dispatched to mac1",
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)
        config = Config(repos=[Repo(name="acme/widgets", github="acme/widgets")], machines=[])
        board = Board()

        result = board_doctor_policy.dispatch_nit_only_finisher(
            "acme/widgets", 40, board, config, evidence=("blocking=0",)
        )

        assert result is True
        assert len(calls) == 1
        action, args, kwargs = calls[0]
        assert action == board_doctor_policy._FINISHER_ACTION
        assert args == ("acme/widgets", 40, board, config)
        assert kwargs["cause"] == board_doctor_policy.CAUSE_NIT_ONLY_REQUEST_CHANGES
        assert kwargs["evidence"] == ("blocking=0",)

    def test_returns_false_when_the_sweep_action_declines(self, monkeypatch) -> None:
        monkeypatch.setattr(
            board_doctor_policy, "_finisher_tier_enabled", lambda *a, **k: True
        )
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {
                "dispatched": False,
                "assignment_id": None,
                "machine": None,
                "detail": _IN_FLIGHT_DETAIL,
            },
        )
        config = Config(repos=[Repo(name="acme/widgets", github="acme/widgets")], machines=[])

        result = board_doctor_policy.dispatch_nit_only_finisher(
            "acme/widgets", 40, Board(), config
        )

        assert result is False


class TestFinisherCapacityCheck:
    """#3837: the finisher tier's per-repo cap, resolved via the same
    shared `_at_repo_capacity` helper the investigate tier's
    `InvestigateCapacityGate` uses, but called inline on every
    `dispatch_nit_only_finisher` call rather than via a gate built once per
    sweep — there is no sweep here, just the one call site.
    """

    @staticmethod
    def _config(max_parallel: int) -> Config:
        from coord.config import BoardDoctorConfig
        from coord.models import BoardDoctorRepoConfig

        return Config(
            repos=[
                Repo(
                    name="acme/widgets",
                    github="acme/widgets",
                    max_parallel=max_parallel,
                    board_doctor=BoardDoctorRepoConfig(finish=True),
                )
            ],
            machines=[],
            board_doctor=BoardDoctorConfig(finish=True),
        )

    def test_at_capacity_does_not_dispatch(self, monkeypatch) -> None:
        busy_board = Board(
            active=[
                Assignment(
                    machine_name="mac1",
                    repo_name="acme/widgets",
                    issue_number=40,
                    issue_title="t",
                    status="running",
                    type="work",
                )
            ]
        )

        def dispatch_must_not_be_called(*args, **kwargs):
            raise AssertionError("an at-capacity repo must not dispatch a finisher leg")

        monkeypatch.setattr(
            board_doctor_policy, "dispatch_sweep_action", dispatch_must_not_be_called
        )
        config = self._config(max_parallel=1)

        result = board_doctor_policy.dispatch_nit_only_finisher(
            "acme/widgets", 41, busy_board, config
        )

        assert result is False

    def test_below_capacity_dispatches(self, monkeypatch) -> None:
        calls: list[tuple] = []

        def fake_dispatch(action, *args, **kwargs):
            calls.append((action, args, kwargs))
            return {
                "dispatched": True,
                "assignment_id": "asn-1",
                "machine": "mac1",
                "detail": "acme/widgets#41 finisher leg dispatched to mac1",
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)
        config = self._config(max_parallel=2)
        board = Board(
            active=[
                Assignment(
                    machine_name="mac1",
                    repo_name="acme/widgets",
                    issue_number=40,
                    issue_title="t",
                    status="running",
                    type="work",
                )
            ]
        )

        result = board_doctor_policy.dispatch_nit_only_finisher(
            "acme/widgets", 41, board, config
        )

        assert result is True
        assert len(calls) == 1

    def test_zero_max_parallel_means_unlimited_not_zero_capacity(self, monkeypatch) -> None:
        # `max_parallel=0` means "no per-repo ceiling at all", mirroring
        # `coord.drive_queue`'s own resolution semantics — never "never
        # dispatch" — same contract the investigate tier's capacity check
        # already guarantees, exercised here against the delegating
        # finisher-tier wrapper instead.
        calls: list[tuple] = []

        def fake_dispatch(action, *args, **kwargs):
            calls.append((action, args, kwargs))
            return {
                "dispatched": True,
                "assignment_id": "asn-1",
                "machine": "mac1",
                "detail": "acme/widgets#41 finisher leg dispatched to mac1",
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)
        config = self._config(max_parallel=0)
        busy_board = Board(
            active=[
                Assignment(
                    machine_name="mac1",
                    repo_name="acme/widgets",
                    issue_number=40,
                    issue_title="t",
                    status="running",
                    type="work",
                )
            ]
        )

        result = board_doctor_policy.dispatch_nit_only_finisher(
            "acme/widgets", 41, busy_board, config
        )

        assert result is True
        assert len(calls) == 1


# ── #3881: investigator recommendation replay ────────────────────────────


class TestRecommendInvestigatorAction:
    """Replays the shape of each historical row #3881's own evidence table
    names, confirming `recommend_investigator_action` names the verb that
    actually fixed it — and that `coord.board_doctor_permissions`'s
    wrapper (not this function) is still the one that decides whether
    acting on that recommendation is safe right now."""

    def test_frozen_blocked_rows_recommend_requeue(self) -> None:
        # quadraui#2035/#2047/#2045/#2051/#2052/#2058/#2059, coord#3875.
        row = _row("acme/widgets#2035", queue_diagnose.CAUSE_NEVER_DISPATCHED)

        assert board_doctor_policy.recommend_investigator_action(row) == "requeue"

    def test_ceiling_blocked_mergeable_recommends_requeue(self) -> None:
        row = _row("acme/widgets#3874b", board_doctor_policy.CAUSE_CEILING_BLOCKED_MERGEABLE)

        assert board_doctor_policy.recommend_investigator_action(row) == "requeue"

    def test_pr_merged_recommends_mark_done(self) -> None:
        # quadraui#2038: merged by hand, the queue slot still held.
        row = _row("acme/widgets#2038", queue_diagnose.CAUSE_PR_MERGED)

        assert board_doctor_policy.recommend_investigator_action(row) == "mark_done"

    def test_baseline_red_recommends_integration_fix(self) -> None:
        # coord#3870: a repo-wide baseline-red streak.
        row = _row("acme/widgets#0", board_doctor_policy.CAUSE_BASELINE_RED)

        assert board_doctor_policy.recommend_investigator_action(row) == "integration_fix"

    def test_a_cause_with_no_mechanical_recommendation_is_judgment_not_a_guess(self) -> None:
        # A gate-blocked HUMAN_REQUIRED latch is only sometimes a
        # clear_latch candidate (conflict-fix-retry-cap + rebaseable) —
        # this function must not guess that it always is.
        row = _row("acme/widgets#2034", queue_diagnose.CAUSE_GATE_BLOCKED)

        assert board_doctor_policy.recommend_investigator_action(row) is None

    def test_every_recommended_verb_is_in_the_investigators_closed_set(self) -> None:
        from coord.board_doctor_permissions import INVESTIGATOR_ACTIONS

        for verb in board_doctor_policy.INVESTIGATOR_RECOMMENDED_ACTION.values():
            assert verb in INVESTIGATOR_ACTIONS

    def test_recommendation_alone_does_not_bypass_the_wrappers_own_precondition(
        self,
    ) -> None:
        # coord#3875's already-requeued-by-the-time-we-looked shape: the
        # recommendation still names "requeue", but the row is no longer
        # queued (a human, or an earlier sweep, already handled it) — the
        # wrapper must refuse cleanly rather than trust the recommendation.
        from coord import state
        from coord.board_doctor_permissions import investigator_requeue

        row = _row("acme/widgets#3875", queue_diagnose.CAUSE_NEVER_DISPATCHED)
        assert board_doctor_policy.recommend_investigator_action(row) == "requeue"
        assert state.get_drive_queue_entry("acme/widgets", 3875) is None

        result = investigator_requeue(
            "acme/widgets", 3875, Board(), reason="recommended requeue, already gone",
        )

        assert result["acted"] is False
        assert "not in the drive queue" in result["detail"]


# ── #3887 (#3875 ask A): the sweep applies the commonest queue repairs ─────
# itself, via the SAME #3881 wrapper, with no agent session and no
# `max_parallel` slot — replaying the cases the issue's own evidence table
# names.


class TestMergeQueueRowHeadUnmovedAndStalePendingClassification:
    """`coord.board_doctor.merge_queue_rows`'s two #3887 refinements: a
    sharper reading of live evidence it already has (the HUMAN_REQUIRED
    head-unmoved shape), and a first classification for a shape it
    otherwise never looks at at all (a PENDING entry stale since its last
    attempt)."""

    @staticmethod
    def _entry(**overrides):
        from coord import merge_queue as mq

        base = dict(
            assignment_id="w1", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-1-fix", target_branch="main", issue_number=1,
            issue_title="Fix the thing",
        )
        base.update(overrides)
        return mq.QueuedMerge(**base)

    def test_head_unmoved_human_required_gets_the_sharper_cause(self) -> None:
        from coord import merge_queue as mq

        entry = self._entry(
            state=mq.HUMAN_REQUIRED,
            error=(
                f"conflict-fix failed; the {mq.CONFLICT_FIX_HEAD_UNMOVED_SIGNAL} "
                "(still deadbeef) — the push appears to be missing, and a "
                "fresh automated conflict-fix could not be dispatched"
            ),
        )

        rows = merge_queue_rows([entry])

        assert len(rows) == 1
        assert rows[0].cause == CAUSE_CONFLICT_FIX_HEAD_UNMOVED

    def test_ordinary_human_required_keeps_the_generic_cause(self) -> None:
        from coord import merge_queue as mq

        entry = self._entry(state=mq.HUMAN_REQUIRED, error="conflict-fix worker did not resolve")

        rows = merge_queue_rows([entry])

        assert len(rows) == 1
        assert rows[0].cause == queue_diagnose.CAUSE_GATE_BLOCKED

    def test_stale_pending_entry_gets_flagged(self) -> None:
        from coord import merge_queue as mq

        entry = self._entry(state=mq.PENDING, last_attempt=1000.0)

        rows = merge_queue_rows([entry], now=1000.0 + MERGE_PENDING_STALE_AFTER_SECONDS)

        assert len(rows) == 1
        assert rows[0].cause == CAUSE_MERGE_PENDING_STUCK

    def test_freshly_attempted_pending_entry_is_not_this_doctors_problem(self) -> None:
        # Waiting its ordinary turn — must never be flagged, or every
        # routine PENDING row would alert after a few sweeps.
        from coord import merge_queue as mq

        entry = self._entry(state=mq.PENDING, last_attempt=1000.0)

        rows = merge_queue_rows([entry], now=1000.0 + 60.0)

        assert rows == []

    def test_pending_entry_never_attempted_is_not_this_doctors_problem(self) -> None:
        from coord import merge_queue as mq

        entry = self._entry(state=mq.PENDING, last_attempt=None)

        rows = merge_queue_rows([entry], now=1_000_000.0)

        assert rows == []


class TestConfirmMergePendingStuck:
    """#3887 review: `merge_queue_rows`'s ``last_attempt``-staleness read is
    only a cheap candidate filter — it fires on two healthy shapes
    (CI re-running after a conflict-fix rebase; a row deferred behind
    another entry's #3886 per-repo merge-gate reservation) that are not
    actually stuck. `_confirm_merge_pending_stuck` re-confirms live via
    `coord.merge_queue.entry_gate_status` before the cause survives."""

    @staticmethod
    def _entry(**overrides):
        from coord import merge_queue as mq

        base = dict(
            assignment_id="w2045", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2045-fix", target_branch="main", issue_number=2045,
            issue_title="Fix the thing", state=mq.PENDING, last_attempt=1000.0,
            # No open PR and an explicit, gate-free required_gates list:
            # keeps `entry_gate_status` hermetic (no `gh` call at all) so
            # these tests exercise the LIVE-CONFIRM CONTROL FLOW, not a
            # live network read.
            pr_number=None, required_gates=["merge"],
        )
        base.update(overrides)
        return mq.QueuedMerge(**base)

    @staticmethod
    def _row() -> StuckRow:
        return StuckRow(
            queue="merge-queue", key="acme/widgets#2045", state="pending",
            cause=CAUSE_MERGE_PENDING_STUCK, confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(), fresh=True,
        )

    def test_keeps_the_cause_when_the_live_gate_reads_ready(self) -> None:
        entry = self._entry()
        confirmed = _confirm_merge_pending_stuck(
            [self._row()], [entry], board=Board(), config=_widgets_config(),
        )
        assert len(confirmed) == 1
        assert confirmed[0].cause == CAUSE_MERGE_PENDING_STUCK

    def test_drops_the_row_when_deferred_behind_a_merge_gate_reservation(self) -> None:
        # #3886: a DIFFERENT entry in the same repo holds the reservation —
        # `last_attempt` is stale only because this row has been correctly
        # waiting its turn, not because the merge tick is missing it.
        from coord.state import reserve_merge_slot

        entry = self._entry()
        assert reserve_merge_slot("acme/widgets", "acme/widgets#9999", 3600.0)

        confirmed = _confirm_merge_pending_stuck(
            [self._row()], [entry], board=Board(), config=_widgets_config(),
        )
        assert confirmed == []

    def test_drops_the_row_when_no_matching_entry_is_found(self) -> None:
        confirmed = _confirm_merge_pending_stuck(
            [self._row()], [], board=Board(), config=_widgets_config(),
        )
        assert confirmed == []

    def test_drops_the_row_when_the_live_confirm_raises(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("gh hiccup")

        monkeypatch.setattr("coord.merge_queue.entry_gate_status", _boom)

        entry = self._entry()
        confirmed = _confirm_merge_pending_stuck(
            [self._row()], [entry], board=Board(), config=_widgets_config(),
        )
        assert confirmed == []

    def test_leaves_other_causes_and_queues_untouched(self) -> None:
        other = _row("acme/widgets#1", queue_diagnose.CAUSE_PR_MERGED, queue="drive-queue")
        confirmed = _confirm_merge_pending_stuck(
            [other], [], board=Board(), config=_widgets_config(),
        )
        assert confirmed == [other]


class TestReclassifyStaleLatch:
    """#3887 case 2, quadraui#2034: a CONFLICT/HUMAN_REQUIRED merge-queue
    row whose approval is already confirmed bound to the current branch
    content gets promoted to the mechanical `clear_latch` cause — a row
    whose approval is NOT confirmed, or a drive-queue row carrying the
    same cause NAME by coincidence, is left alone."""

    @staticmethod
    def _merge_row(cause: str) -> StuckRow:
        return StuckRow(
            queue="merge-queue",
            key="acme/widgets#2034",
            state="human_required",
            cause=cause,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=("merge_queue.state=human_required",),
            fresh=True,
        )

    @staticmethod
    def _entry():
        from coord import merge_queue as mq

        return mq.QueuedMerge(
            assignment_id="w1", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2034-fix", target_branch="main", issue_number=2034,
            issue_title="Fix the thing", state=mq.HUMAN_REQUIRED,
        )

    def test_promotes_once_approval_is_bound_to_current_content(self, monkeypatch) -> None:
        from coord import merge_queue as mq

        entry = self._entry()
        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)
        row = self._merge_row(queue_diagnose.CAUSE_GATE_BLOCKED)

        reclassified = _reclassify_stale_latch(
            [row], [entry], board=Board(), config=Config(repos=[], machines=[]),
        )

        assert reclassified[0].cause == CAUSE_LATCH_STALE_MERGEABLE

    def test_leaves_the_row_alone_when_approval_is_not_bound(self, monkeypatch) -> None:
        from coord import merge_queue as mq

        entry = self._entry()
        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: False)
        row = self._merge_row(queue_diagnose.CAUSE_MERGE_CONFLICT)

        reclassified = _reclassify_stale_latch(
            [row], [entry], board=Board(), config=Config(repos=[], machines=[]),
        )

        assert reclassified[0].cause == queue_diagnose.CAUSE_MERGE_CONFLICT

    def test_never_promotes_a_genuine_live_conflict_even_with_bound_approval(
        self, monkeypatch,
    ) -> None:
        # #3887 review: CAUSE_MERGE_CONFLICT means GitHub's own live
        # `mergeable is False` reading — approval bound to current content
        # does not establish that a REAL conflict has cleared, so this row
        # must keep CAUSE_MERGE_CONFLICT (and its own #3769 investigate-lane
        # conflict-fix remediation) rather than being relabelled into the
        # mechanical `clear_latch` dead end, even when approval is bound.
        from coord import merge_queue as mq

        entry = self._entry()
        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)
        row = self._merge_row(queue_diagnose.CAUSE_MERGE_CONFLICT)

        reclassified = _reclassify_stale_latch(
            [row], [entry], board=Board(), config=Config(repos=[], machines=[]),
        )

        assert reclassified[0].cause == queue_diagnose.CAUSE_MERGE_CONFLICT
        # Still routes to the #3769 investigate lane, not the mechanical
        # clear_latch allowlist entry — the whole point of not relabelling it.
        assert queue_diagnose.CAUSE_MERGE_CONFLICT in INVESTIGATE_CAUSES
        assert queue_diagnose.CAUSE_MERGE_CONFLICT not in ALLOWLIST
        assert policy_tier_of(queue_diagnose.CAUSE_MERGE_CONFLICT) == TIER_AUTO

    def test_never_touches_a_drive_queue_row_with_the_same_cause_name(self, monkeypatch) -> None:
        from coord import merge_queue as mq

        entry = self._entry()
        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)
        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#2034",
            state="blocked",
            cause=queue_diagnose.CAUSE_GATE_BLOCKED,
            confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(),
            fresh=True,
        )

        reclassified = _reclassify_stale_latch(
            [row], [entry], board=Board(), config=Config(repos=[], machines=[]),
        )

        assert reclassified[0].cause == queue_diagnose.CAUSE_GATE_BLOCKED


def _widgets_config() -> Config:
    repo = Repo(
        name="acme/widgets", github="acme/widgets",
        default_branch="main", test_command="pytest",
    )
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                repos=["acme/widgets"], repo_paths={"acme/widgets": "/work"},
            ),
        ],
    )


class TestClearStaleLatchEndToEnd:
    """#3887 case 2: the sweep clears a stale latch itself, via the SAME
    #3881 wrapper a human-attended investigator session would use."""

    def test_clears_the_latch_in_one_sweep(self, monkeypatch) -> None:
        from coord import merge_queue as mq
        from coord.audit import query_audit_log

        entry = mq.QueuedMerge(
            assignment_id="w2034", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2034-fix", target_branch="main", issue_number=2034,
            issue_title="Fix the thing", state=mq.HUMAN_REQUIRED, pr_number=9,
        )
        mq.save_queue([entry])

        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())
        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)
        monkeypatch.setattr(
            mq, "reconcile_conflict_entries",
            lambda gh_ops: [mq.MergeEvent(entry, "reopened", "cleared for real")],
        )

        row = StuckRow(
            queue="merge-queue", key="acme/widgets#2034", state="human_required",
            cause=CAUSE_LATCH_STALE_MERGEABLE, confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(), fresh=True,
        )

        report = sweep(
            [row], tier_of=policy_tier_of, auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False, sweep_id="sweep-2034-latch",
        )

        assert report.decisions[0].tier == TIER_AUTO
        assert report.decisions[0].action == "auto-dispatch"

        # #3887 review: the audit ROW itself, not just the kwarg this
        # dispatcher passed, must carry the sweep's own attribution.
        log = query_audit_log(event_type="investigator_clear_latch", repo="acme/widgets", issue=2034)
        assert len(log["entries"]) == 1
        assert log["entries"][0]["actor"] == "sweep"

    def test_a_second_sweep_on_the_same_row_is_a_noop(self, monkeypatch) -> None:
        # Once cleared, `reconcile_conflict_entries` no longer returns an
        # event for this row (it is PENDING, not latched) — a second sweep
        # pass re-handed the SAME StuckRow (production would have stopped
        # classifying this cause for it by now; this pins the handler's
        # own idempotence regardless) must not re-mutate or re-report it.
        from coord import merge_queue as mq

        entry = mq.QueuedMerge(
            assignment_id="w2034b", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2034b-fix", target_branch="main", issue_number=20342,
            issue_title="Fix the thing", state=mq.HUMAN_REQUIRED, pr_number=9,
        )
        mq.save_queue([entry])

        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())
        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)

        def _reconcile(gh_ops):
            queue = mq.load_queue()
            live = next(e for e in queue if e.assignment_id == "w2034b")
            if live.state != mq.HUMAN_REQUIRED:
                return []
            live.state = mq.PENDING
            mq.save_queue(queue)
            return [mq.MergeEvent(live, "reopened", "cleared for real")]

        monkeypatch.setattr(mq, "reconcile_conflict_entries", _reconcile)

        row = StuckRow(
            queue="merge-queue", key="acme/widgets#20342", state="human_required",
            cause=CAUSE_LATCH_STALE_MERGEABLE, confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(), fresh=True,
        )

        first = sweep(
            [row], tier_of=policy_tier_of, auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False, sweep_id="sweep-20342-latch-first",
        )
        assert first.decisions[0].action == "auto-dispatch"
        assert mq.load_queue()[0].state == mq.PENDING

        second = sweep(
            [row], tier_of=policy_tier_of, auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False, sweep_id="sweep-20342-latch-second",
        )
        assert second.decisions[0].action == "auto-dispatch-noop"
        assert mq.load_queue()[0].state == mq.PENDING

    def test_already_fixed_when_we_act_refuses_with_no_write(self, monkeypatch) -> None:
        # A human (or an earlier sweep) already un-latched this row by the
        # time this one acts — no write, no false success.
        from coord import merge_queue as mq

        entry = mq.QueuedMerge(
            assignment_id="w2035", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2035-fix", target_branch="main", issue_number=2035,
            issue_title="Fix the thing", state=mq.PENDING,
        )
        mq.save_queue([entry])
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        row = StuckRow(
            queue="merge-queue", key="acme/widgets#2035", state="human_required",
            cause=CAUSE_LATCH_STALE_MERGEABLE, confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(), fresh=True,
        )

        report = sweep(
            [row], tier_of=policy_tier_of, auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False, sweep_id="sweep-2035-already-fixed",
        )

        assert report.decisions[0].action == "auto-dispatch-noop"
        assert mq.load_queue()[0].state == mq.PENDING


class TestMergePendingStuckEndToEnd:
    """#3887 case 5: the sweep merges a stale, green/approved PENDING row
    itself, via plain `coord merge --only` (#3881's wrapper)."""

    def test_merges_a_stale_pending_green_approved_row(self, monkeypatch) -> None:
        from coord import merge_queue as mq
        from coord.audit import query_audit_log

        entry = mq.QueuedMerge(
            assignment_id="w2045", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2045-fix", target_branch="main", issue_number=2045,
            issue_title="Fix the thing", state=mq.PENDING, last_attempt=1.0,
        )
        mq.save_queue([entry])

        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())
        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)
        monkeypatch.setattr(
            mq, "process",
            lambda items, gh_ops, **kwargs: [mq.MergeEvent(entry, "merged", "merged cleanly")],
        )

        row = StuckRow(
            queue="merge-queue", key="acme/widgets#2045", state="pending",
            cause=CAUSE_MERGE_PENDING_STUCK, confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(), fresh=True,
        )

        report = sweep(
            [row], tier_of=policy_tier_of, auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False, sweep_id="sweep-2045-pending",
        )

        assert report.decisions[0].tier == TIER_AUTO
        assert report.decisions[0].action == "auto-dispatch"

        # #3887 review: the audit ROW itself, not just the kwarg this
        # dispatcher passed, must carry the sweep's own attribution.
        log = query_audit_log(event_type="investigator_merge", repo="acme/widgets", issue=2045)
        assert len(log["entries"]) == 1
        assert log["entries"][0]["actor"] == "sweep"

    def test_already_fixed_when_we_act_refuses_with_no_write(self, monkeypatch) -> None:
        # The row merged (by the daemon's own ordinary tick) between this
        # sweep's diagnosis and its act — `investigator_merge`'s own
        # `entry.state != PENDING` precondition refuses cleanly.
        from coord import merge_queue as mq

        entry = mq.QueuedMerge(
            assignment_id="w2046", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2046-fix", target_branch="main", issue_number=2046,
            issue_title="Fix the thing", state=mq.MERGED, last_attempt=1.0,
        )
        mq.save_queue([entry])
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        row = StuckRow(
            queue="merge-queue", key="acme/widgets#2046", state="pending",
            cause=CAUSE_MERGE_PENDING_STUCK, confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(), fresh=True,
        )

        report = sweep(
            [row], tier_of=policy_tier_of, auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False, sweep_id="sweep-2046-already-fixed",
        )

        assert report.decisions[0].action == "auto-dispatch-noop"
        assert mq.load_queue()[0].state == mq.MERGED


class TestRedispatchHeadUnmovedConflictFixEndToEnd:
    """#3887 case 3, pairs with #3871: the periodic net for #3871's own
    synchronous completion-time redispatch, for when that one-shot
    attempt had no capable machine right then — still within the SAME
    retry cap, never a fresh budget."""

    def test_redispatches_within_the_existing_retry_cap(self, monkeypatch) -> None:
        from coord import merge_queue as mq

        entry = mq.QueuedMerge(
            assignment_id="w2059", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2059-fix", target_branch="main", issue_number=2059,
            issue_title="Fix the thing", state=mq.HUMAN_REQUIRED,
            error=(
                f"conflict-fix failed; the {mq.CONFLICT_FIX_HEAD_UNMOVED_SIGNAL} "
                "(still deadbeef) — the push appears to be missing, and a "
                "fresh automated conflict-fix could not be dispatched"
            ),
        )
        mq.save_queue([entry])
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        fake_fix = Assignment(
            machine_name="laptop", repo_name="acme/widgets", issue_number=2059,
            issue_title="t", assignment_id="fix-2", type="conflict-fix", status="pending",
        )
        monkeypatch.setattr(
            "coord.conflict_fix.dispatch_conflict_fix",
            lambda entry_, board, config, **kwargs: fake_fix,
        )

        row = StuckRow(
            queue="merge-queue", key="acme/widgets#2059", state="human_required",
            cause=CAUSE_CONFLICT_FIX_HEAD_UNMOVED, confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(), fresh=True,
        )

        report = sweep(
            [row], tier_of=policy_tier_of, auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False, sweep_id="sweep-2059-head-unmoved",
        )

        assert report.decisions[0].tier == TIER_AUTO
        assert report.decisions[0].action == "auto-dispatch"
        after = mq.load_queue()[0]
        assert after.state == mq.CONFLICT
        assert "fix-2" in after.error

    def test_already_fixed_when_we_act_refuses_with_no_write(self, monkeypatch) -> None:
        # #3871's own synchronous redispatch already fired by the time
        # this sweep acts — the row is no longer the head-unmoved shape,
        # so the periodic net must not attempt a second dispatch.
        from coord import merge_queue as mq

        entry = mq.QueuedMerge(
            assignment_id="w2060", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2060-fix", target_branch="main", issue_number=2060,
            issue_title="Fix the thing", state=mq.CONFLICT,
            error="conflict-fix failed",
        )
        mq.save_queue([entry])
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        def _must_not_dispatch(*a, **k):
            raise AssertionError(
                "must not attempt a redispatch once the row is no longer "
                "the head-unmoved HUMAN_REQUIRED shape"
            )

        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", _must_not_dispatch)

        row = StuckRow(
            queue="merge-queue", key="acme/widgets#2060", state="conflict",
            cause=CAUSE_CONFLICT_FIX_HEAD_UNMOVED, confidence=queue_diagnose.CONFIDENCE_HIGH,
            evidence=(), fresh=True,
        )

        report = sweep(
            [row], tier_of=policy_tier_of, auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=lambda row: False, sweep_id="sweep-2060-already-fixed",
        )

        assert report.decisions[0].action == "auto-dispatch-noop"
        assert mq.load_queue()[0].state == mq.CONFLICT


class TestMechanicalRepairsAuditAsSweep:
    """#3887 acceptance: audit events this module's four new/changed
    dispatchers write must carry ``actor="sweep"`` — never
    ``"board_doctor_investigator"`` (the human-attended CLI's own
    attribution) — so #3875's outcome tracking credits the doctor, not a
    session that never ran."""

    def test_reconcile_merged_row_dispatches_mark_done_as_sweep(self, monkeypatch) -> None:
        captured: dict = {}

        def fake_dispatch(action, repo_name, issue_number, *args, **kwargs):
            captured["action"] = action
            captured["actor"] = kwargs.get("actor")
            return {"acted": True, "action": action, "repo": repo_name, "issue": issue_number, "detail": "ok"}

        monkeypatch.setattr(board_doctor_policy, "dispatch_investigator_action", fake_dispatch)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        row = _row("acme/widgets#1", queue_diagnose.CAUSE_PR_MERGED, queue="drive-queue")
        assert _reconcile_merged_row(row) is True
        assert captured["action"] == "mark_done"
        assert captured["actor"] == "sweep"

    def test_clear_stale_latch_dispatches_clear_latch_as_sweep(self, monkeypatch) -> None:
        captured: dict = {}

        def fake_dispatch(action, repo_name, issue_number, *args, **kwargs):
            captured["action"] = action
            captured["actor"] = kwargs.get("actor")
            return {"acted": True, "action": action, "repo": repo_name, "issue": issue_number, "detail": "ok"}

        monkeypatch.setattr(board_doctor_policy, "dispatch_investigator_action", fake_dispatch)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        row = _row("acme/widgets#2034", CAUSE_LATCH_STALE_MERGEABLE, queue="merge-queue")
        assert _clear_stale_latch(row) is True
        assert captured["action"] == "clear_latch"
        assert captured["actor"] == "sweep"

    def test_merge_pending_stuck_dispatches_merge_as_sweep(self, monkeypatch) -> None:
        captured: dict = {}

        def fake_dispatch(action, repo_name, issue_number, *args, **kwargs):
            captured["action"] = action
            captured["actor"] = kwargs.get("actor")
            return {"acted": True, "action": action, "repo": repo_name, "issue": issue_number, "detail": "ok"}

        monkeypatch.setattr(board_doctor_policy, "dispatch_investigator_action", fake_dispatch)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        row = _row("acme/widgets#2045", CAUSE_MERGE_PENDING_STUCK, queue="merge-queue")
        assert _merge_pending_stuck_row(row) is True
        assert captured["action"] == "merge"
        assert captured["actor"] == "sweep"

    def test_redispatch_head_unmoved_conflict_fix_audits_as_sweep(self, monkeypatch) -> None:
        # No #3881 investigator verb exists for this shape (#3871 built it
        # as a sweep-only primitive), so this one dispatches via
        # `dispatch_sweep_action` and audits itself, directly, with
        # ``actor="sweep"`` — not through `_investigator_result` at all.
        # Drives the real handler (not a mocked dispatch boundary, unlike
        # the three tests above) so the audit row under test is the one
        # `sweep_redispatch_head_unmoved_conflict_fix` itself writes.
        from coord import merge_queue as mq
        from coord.audit import query_audit_log

        entry = mq.QueuedMerge(
            assignment_id="w2059", repo_name="acme/widgets", repo_github="acme/widgets",
            branch="issue-2059-fix", target_branch="main", issue_number=2059,
            issue_title="Fix the thing", state=mq.HUMAN_REQUIRED,
            error=(
                f"conflict-fix failed; the {mq.CONFLICT_FIX_HEAD_UNMOVED_SIGNAL} "
                "(still deadbeef) — the push appears to be missing, and a "
                "fresh automated conflict-fix could not be dispatched"
            ),
        )
        mq.save_queue([entry])
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())
        fake_fix = Assignment(
            machine_name="laptop", repo_name="acme/widgets", issue_number=2059,
            issue_title="t", assignment_id="fix-2", type="conflict-fix", status="pending",
        )
        monkeypatch.setattr(
            "coord.conflict_fix.dispatch_conflict_fix",
            lambda entry_, board, config, **kwargs: fake_fix,
        )

        row = _row("acme/widgets#2059", CAUSE_CONFLICT_FIX_HEAD_UNMOVED, queue="merge-queue")
        assert _redispatch_head_unmoved_conflict_fix(row) is True

        log = query_audit_log(event_type="conflict_fix_redispatched", repo="acme/widgets", issue=2059)
        assert len(log["entries"]) == 1
        assert log["entries"][0]["actor"] == "sweep"


class TestMechanicalDispatchersNeverAbortTheSweep:
    """#3887 review: `coord.board_doctor.sweep` calls ``performed =
    dispatcher(row)`` with no shield of its own (reintroducing #3805's
    whole-sweep abort otherwise) — every live-GitHub/network-bound call
    these four #3887 mechanical dispatchers reach must report an uncaught
    exception as a confirmed no-op instead of letting it propagate."""

    def test_reconcile_merged_row_reports_instead_of_raising(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("gh hiccup")

        monkeypatch.setattr(board_doctor_policy, "dispatch_investigator_action", _boom)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        row = _row("acme/widgets#1", queue_diagnose.CAUSE_PR_MERGED, queue="drive-queue")
        assert _reconcile_merged_row(row) is False

    def test_clear_stale_latch_reports_instead_of_raising(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("gh hiccup")

        monkeypatch.setattr(board_doctor_policy, "dispatch_investigator_action", _boom)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        row = _row("acme/widgets#2034", CAUSE_LATCH_STALE_MERGEABLE, queue="merge-queue")
        assert _clear_stale_latch(row) is False

    def test_merge_pending_stuck_reports_instead_of_raising(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("gh hiccup")

        monkeypatch.setattr(board_doctor_policy, "dispatch_investigator_action", _boom)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        row = _row("acme/widgets#2045", CAUSE_MERGE_PENDING_STUCK, queue="merge-queue")
        assert _merge_pending_stuck_row(row) is False

    def test_redispatch_head_unmoved_conflict_fix_reports_instead_of_raising(
        self, monkeypatch,
    ) -> None:
        def _boom(*a, **k):
            raise RuntimeError("gh hiccup")

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", _boom)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        row = _row("acme/widgets#2059", CAUSE_CONFLICT_FIX_HEAD_UNMOVED, queue="merge-queue")
        assert _redispatch_head_unmoved_conflict_fix(row) is False

    def test_board_read_failure_also_reports_instead_of_raising(self, monkeypatch) -> None:
        # #3887 review nit: `_resolve_board_and_config_for_mechanical_action`
        # used to call `build_board()` outside any guard — a board-read
        # failure must land exactly like a config-load failure, not
        # propagate into the (now-shielded, but this still must not rely
        # on that) dispatcher call above.
        def _boom_board():
            raise RuntimeError("db locked")

        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())
        monkeypatch.setattr("coord.state.build_board", _boom_board)

        row = _row("acme/widgets#2045", CAUSE_MERGE_PENDING_STUCK, queue="merge-queue")
        assert _merge_pending_stuck_row(row) is False

    def test_a_raising_dispatcher_does_not_abort_the_rest_of_the_sweep(self, monkeypatch) -> None:
        # The actual #3805 shape: one row's mechanical action raises; the
        # sweep must still classify/dispatch every row ordered after it.
        def _boom(*a, **k):
            raise RuntimeError("gh hiccup")

        monkeypatch.setattr(board_doctor_policy, "dispatch_investigator_action", _boom)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: _widgets_config())

        raising_row = _row("acme/widgets#2034", CAUSE_LATCH_STALE_MERGEABLE, queue="merge-queue")
        later_row = _row("acme/widgets#99", CAUSE_SMOKE_NO_VERDICT, queue="smoke-no-verdict")

        called: list[str] = []

        def _smoke_ok(row: StuckRow) -> bool:
            called.append(row.key)
            return True

        dispatchers = dict(AUTO_DISPATCHERS)
        dispatchers[CAUSE_SMOKE_NO_VERDICT] = _smoke_ok

        report = sweep(
            [raising_row, later_row], tier_of=policy_tier_of, auto_dispatchers=dispatchers,
            alert_dispatcher=lambda row: False, sweep_id="sweep-abort-guard",
        )

        assert report.decisions[0].action == "auto-dispatch-noop"
        assert report.decisions[1].action == "auto-dispatch"
        assert called == ["acme/widgets#99"]


# ── #3901 (#3887 case 4's remainder): a frozen blocked/parked row whose
# `--after` prereqs have since landed gets requeued too, not just the
# never-dispatched shape #3874 already covers ──────────────────────────────


def _after_entry(**kw):
    from coord.drive_queue import QueueEntry

    base: dict = {
        "repo": "acme/widgets",
        "issue": 40,
        "position": 1,
        "state": "blocked",
        "after": ("acme/widgets#39",),
        "last_reason": (
            "pre-req acme/widgets#39 is queued but blocked — it will never "
            "satisfy"
        ),
    }
    base.update(kw)
    return QueueEntry(**base)


def _dispatched_board(issue: int = 40) -> Board:
    return Board(
        completed=[
            Assignment(
                machine_name="laptop", repo_name="acme/widgets", issue_number=issue,
                issue_title="t", status="done", type="work",
                branch=f"issue-{issue}-fix", assignment_id=f"w{issue}",
            )
        ],
    )


def _landed_board_view(key: str):
    from coord.drive_queue import BoardView, IssueFacts

    return BoardView(issues={key: IssueFacts(known=True, merged=True, issue_state="")})


class TestAfterPrereqsLandedForEntry:
    """The ONE predicate :func:`_reclassify_after_prereqs_landed`
    (classification) and :func:`_requeue_after_prereqs_landed` (the
    pre-dispatch live re-check) both call — never two independently
    re-derived answers to the same question (#2096)."""

    def test_true_once_the_dep_is_done_in_the_queue(self) -> None:
        entry = _after_entry()
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        entries_by_key = {entry.key: entry, dep.key: dep}

        assert (
            after_prereqs_landed_for_entry(entry, entries_by_key, _dispatched_board())
            is True
        )

    def test_true_when_the_dep_left_the_queue_but_a_board_view_confirms_landed(
        self,
    ) -> None:
        entry = _after_entry()
        entries_by_key = {entry.key: entry}  # the dep is no longer queued at all

        assert (
            after_prereqs_landed_for_entry(
                entry,
                entries_by_key,
                _dispatched_board(),
                _landed_board_view("acme/widgets#39"),
            )
            is True
        )

    def test_false_without_a_board_view_when_the_dep_left_the_queue(self) -> None:
        # No way to confirm the dep landed without a BoardView to read
        # `.facts()` off — this must refuse, never guess "landed" from
        # absence alone (#2096: unconfirmed success is a defect).
        entry = _after_entry()
        entries_by_key = {entry.key: entry}

        assert (
            after_prereqs_landed_for_entry(entry, entries_by_key, _dispatched_board())
            is False
        )

    def test_false_for_a_row_blocked_on_an_unrelated_reason(self) -> None:
        entry = _after_entry(last_reason="merge conflict detected on this branch")
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        entries_by_key = {entry.key: entry, dep.key: dep}

        assert (
            after_prereqs_landed_for_entry(entry, entries_by_key, _dispatched_board())
            is False
        )

    def test_false_when_a_named_prereq_is_still_in_flight(self) -> None:
        entry = _after_entry()
        dep = _after_entry(issue=39, position=0, after=(), state="waiting", last_reason="")
        entries_by_key = {entry.key: entry, dep.key: dep}

        assert (
            after_prereqs_landed_for_entry(entry, entries_by_key, _dispatched_board())
            is False
        )

    def test_false_when_the_row_was_never_dispatched(self) -> None:
        # The #3874 `CAUSE_NEVER_DISPATCHED` -> `drive_queue_requeue` wiring
        # already covers this shape — this predicate is deliberately its
        # complement, not a second path to the same place.
        entry = _after_entry()
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        entries_by_key = {entry.key: entry, dep.key: dep}

        assert (
            after_prereqs_landed_for_entry(entry, entries_by_key, Board()) is False
        )

    def test_false_once_an_operator_already_resumed_the_row(self) -> None:
        # The row is back to `waiting` — no longer `blocked`/`parked` — so
        # this predicate must read "no longer this shape", which is what
        # lets the SAME call double as the dispatcher's live pre-write
        # re-check.
        entry = _after_entry(state="waiting")
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        entries_by_key = {entry.key: entry, dep.key: dep}

        assert (
            after_prereqs_landed_for_entry(entry, entries_by_key, _dispatched_board())
            is False
        )


class TestReclassifyAfterPrereqsLanded:
    def _row(self, **kw) -> StuckRow:
        base = dict(
            queue="drive-queue", key="acme/widgets#40", state="blocked",
            cause=queue_diagnose.CAUSE_UNKNOWN, confidence=queue_diagnose.CONFIDENCE_NONE,
            evidence=(), fresh=True,
        )
        base.update(kw)
        return StuckRow(**base)

    def test_promotes_a_fresh_row_whose_prereq_landed(self) -> None:
        entry = _after_entry()
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        entries_by_key = {entry.key: entry, dep.key: dep}

        reclassified = _reclassify_after_prereqs_landed(
            [self._row()], entries_by_key, board=_dispatched_board(),
        )

        assert reclassified[0].cause == CAUSE_AFTER_PREREQS_LANDED

    def test_leaves_a_stale_episode_row_alone(self) -> None:
        # `fresh=False` — a reconstructed episode diagnosis — must never be
        # promoted: the live entry could have changed shape since whatever
        # sweep concluded that stored cause.
        entry = _after_entry()
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        entries_by_key = {entry.key: entry, dep.key: dep}

        reclassified = _reclassify_after_prereqs_landed(
            [self._row(fresh=False)], entries_by_key, board=_dispatched_board(),
        )

        assert reclassified[0].cause == queue_diagnose.CAUSE_UNKNOWN

    def test_leaves_a_merge_queue_row_with_the_same_key_alone(self) -> None:
        entry = _after_entry()
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        entries_by_key = {entry.key: entry, dep.key: dep}

        reclassified = _reclassify_after_prereqs_landed(
            [self._row(queue="merge-queue")], entries_by_key, board=_dispatched_board(),
        )

        assert reclassified[0].cause == queue_diagnose.CAUSE_UNKNOWN

    def test_leaves_a_row_with_no_matching_live_entry_alone(self) -> None:
        reclassified = _reclassify_after_prereqs_landed(
            [self._row()], {}, board=_dispatched_board(),
        )

        assert reclassified[0].cause == queue_diagnose.CAUSE_UNKNOWN


class TestRequeueAfterPrereqsLanded:
    """#3901 acceptance: a worked row blocked on `--after X`, X now landed,
    is requeued exactly once; an already-fixed row, or one blocked for an
    unrelated reason, is left alone with no write."""

    def _row(self) -> StuckRow:
        return _row(
            "acme/widgets#40", CAUSE_AFTER_PREREQS_LANDED,
            confidence=queue_diagnose.CONFIDENCE_NONE, fresh=True,
        )

    def _patch_live_reads(self, monkeypatch, *, entries, board_view=None) -> None:
        monkeypatch.setattr(state, "build_board", lambda: _dispatched_board())
        monkeypatch.setattr(
            "coord.drive_queue.entries_from_rows", lambda rows: list(entries),
        )
        monkeypatch.setattr(state, "list_drive_queue", lambda *a, **k: [])
        monkeypatch.setattr(
            "coord.commands.drive_queue._fetch_board_view_with_retry",
            lambda: board_view if board_view is not None else _landed_board_view(
                "acme/widgets#39"
            ),
        )

    def test_dispatches_the_requeue_once(self, monkeypatch) -> None:
        entry = _after_entry()
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        self._patch_live_reads(monkeypatch, entries=[entry, dep])

        calls: list[tuple] = []
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda action, repo_name, issue_number: (
                calls.append((action, repo_name, issue_number))
                or {"requeued": True, "detail": "requeued"}
            ),
        )

        assert _requeue_after_prereqs_landed(self._row()) is True
        assert calls == [("drive_queue_requeue", "acme/widgets", 40)]

    def test_a_second_sweep_on_an_already_resumed_row_is_a_noop(self, monkeypatch) -> None:
        # Simulates the SAME row, re-read live, the tick after the first
        # `drive_queue_requeue` already put it back as `waiting` — the
        # "already fixed" shape the acceptance criteria name explicitly.
        resumed = _after_entry(state="waiting", attempts=0)
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        self._patch_live_reads(monkeypatch, entries=[resumed, dep])

        calls: list[tuple] = []
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: calls.append(a) or {"requeued": True},
        )

        assert _requeue_after_prereqs_landed(self._row()) is False
        assert calls == []

    def test_a_row_blocked_for_an_unrelated_reason_is_never_requeued(
        self, monkeypatch,
    ) -> None:
        entry = _after_entry(last_reason="merge conflict detected on this branch")
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        self._patch_live_reads(monkeypatch, entries=[entry, dep])

        calls: list[tuple] = []
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: calls.append(a) or {"requeued": True},
        )

        assert _requeue_after_prereqs_landed(self._row()) is False
        assert calls == []

    def test_a_reported_noop_returns_false(self, monkeypatch) -> None:
        entry = _after_entry()
        dep = _after_entry(issue=39, position=0, after=(), state="done", last_reason="")
        self._patch_live_reads(monkeypatch, entries=[entry, dep])
        monkeypatch.setattr(
            board_doctor_policy,
            "dispatch_sweep_action",
            lambda *a, **k: {"requeued": False, "detail": "no longer queued"},
        )

        assert _requeue_after_prereqs_landed(self._row()) is False

    def test_unparseable_key_does_not_raise(self) -> None:
        row = _row(
            "no-hash-separator", CAUSE_AFTER_PREREQS_LANDED,
            confidence=queue_diagnose.CONFIDENCE_NONE, fresh=True,
        )

        assert _requeue_after_prereqs_landed(row) is False

    def test_a_raising_live_read_reports_instead_of_raising(self, monkeypatch) -> None:
        # `_run_mechanical_action` shields this dispatcher exactly like the
        # four #3887 ones — a live board/queue read is network-bound and
        # must never abort the whole sweep.
        monkeypatch.setattr(
            state, "build_board", lambda: (_ for _ in ()).throw(RuntimeError("db locked")),
        )

        assert _requeue_after_prereqs_landed(self._row()) is False


class TestSweepMarkDoneThenTickWritesMergedPartial:
    """#3901 (#3887 case 1's partial variant, never confirmed by a test):
    `sweep_mark_done` only ever flips a `done` work assignment to
    `merged` (`coord.reconcile.reconcile_board_merges`'s own #609 sweep) —
    the drive-queue row itself only ever gets corrected to
    `merged-partial` on the NEXT tick, via `coord.drive_queue._merge_
    landed_state`'s #3863 self-heal pass. This drives both REAL functions
    in sequence (never mocking the mechanism under test) to confirm the
    row actually ends up `merged-partial`, not `done`."""

    def test_sweep_mark_done_then_a_tick_lands_merged_partial(self, monkeypatch) -> None:
        from coord import github_ops
        from coord.drive_queue import (
            STATE_DONE,
            STATE_MERGED_PARTIAL,
            QueueEntry,
            build_board_view,
            plan_tick,
        )
        from coord.reconcile import reconcile_board_merges

        assignment = Assignment(
            machine_name="laptop", repo_name="acme/widgets", issue_number=80,
            issue_title="t", status="done", type="work", branch="issue-80-fix",
            assignment_id="w80",
            completion_summary=(
                "All done.\n\nISSUE_RESOLUTION: partial — blocked on upstream\n"
            ),
        )
        board = Board(completed=[assignment])
        # Network-bound probes `reconcile_board_merges` would otherwise hit
        # for this candidate — stubbed the same way `tests/test_reconcile_
        # merges.py`'s own `_patch_probes` does, never touching a real `gh`.
        monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: True)
        monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

        actions = reconcile_board_merges(
            board, _widgets_config(), repo="acme/widgets", issue=80, dry_run=False,
        )

        assert any(action.startswith("mark merged") for action in actions)
        assert board.completed[0].status == "merged"

        # #951/#609: `state.mark_assignment_merged` et al. write against
        # this test's own sandboxed local DB; `assignment_id="w80"` was
        # never inserted there, so every one of those writes no-ops
        # silently (each is documented to do exactly that for a missing
        # row) — harmless, and lets this test exercise the REAL #609 flip
        # rather than a mock of it.

        payload = {
            "assignments": [
                {
                    "repo_name": "acme/widgets",
                    "issue_number": 80,
                    "type": "work",
                    "status": board.completed[0].status,
                    "completion_summary": board.completed[0].completion_summary,
                },
            ],
        }
        board_view = build_board_view(payload)
        entries = [QueueEntry(repo="acme/widgets", issue=80, position=1, state=STATE_DONE)]

        plan = plan_tick(entries, board_view, capacity=1)

        reconcile = next(r for r in plan.reconciles if r.key == "acme/widgets#80")
        assert reconcile.updates["state"] == STATE_MERGED_PARTIAL
        assert reconcile.updates["state"] != STATE_DONE
