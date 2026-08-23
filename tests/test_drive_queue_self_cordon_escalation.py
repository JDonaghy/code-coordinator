"""#2572: escalate a PERSISTENT self-cordon — one of two guards that caught
the 2026-08-22 outage (#2569/#2570) and neither escalated.

`coord.commands.drive_queue._editable_drift_alert` already writes a correct
"no launch — this host's coord is an editable checkout ... not its default
branch" alert into `drive_escalations` on EVERY tick the condition holds —
but `coord.state.record_drive_escalation`'s `ON CONFLICT ... DO UPDATE SET
... created_at=excluded.created_at` means each re-record resets that row's
own timestamp, so nothing about it distinguishes "just noticed" from "been
true for 40 minutes". The only thing that was ever going to turn that row
into a phone push was a separate process (`coord notifier`'s own tick), and
during the incident that process was dead from the identical root cause.

This file exercises `_escalate_persistent_self_cordon` (and its two
plumbing helpers, `_push_self_cordon_escalation` and the marker
read/write/clear trio) directly — the pure clock-tracking logic never
touches the DB or the network, only the actual threshold-crossing push does,
and that is monkeypatched out here so this suite never dials a real ntfy
server.
"""

from __future__ import annotations

from types import SimpleNamespace

from coord.commands import drive_queue as dq_cmd
from coord.notifier.transport import SendResult
from coord.state import get_drive_escalation


def _disabled_config(_path=None):
    """A `_load_config` stand-in whose `notifications.enabled` is False —
    the default posture, and the one every test not specifically about the
    ntfy push should use so no test accidentally reaches for a network call.
    """
    return SimpleNamespace(notifications=SimpleNamespace(enabled=False))


class TestClockTracking:
    def test_no_reason_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr("coord.commands._common._load_config", _disabled_config)
        dq_cmd._escalate_persistent_self_cordon("", now=1000.0, config_path=None)

        assert dq_cmd._read_self_cordon_state() is None
        assert get_drive_escalation(dq_cmd.SELF_CORDON_ALERT_REPO, dq_cmd.SELF_CORDON_ALERT_ISSUE) is None

    def test_first_occurrence_starts_the_clock_without_escalating(self):
        dq_cmd._escalate_persistent_self_cordon("drifted onto 'x'", now=1000.0, config_path=None)

        state = dq_cmd._read_self_cordon_state()
        assert state == {"reason": "drifted onto 'x'", "first_seen_at": 1000.0, "escalated_at": None}
        assert get_drive_escalation(dq_cmd.SELF_CORDON_ALERT_REPO, dq_cmd.SELF_CORDON_ALERT_ISSUE) is None

    def test_same_reason_just_under_threshold_does_not_escalate(self):
        reason = "drifted onto 'x'"
        dq_cmd._escalate_persistent_self_cordon(reason, now=1000.0, config_path=None)
        dq_cmd._escalate_persistent_self_cordon(
            reason, now=1000.0 + dq_cmd.SELF_CORDON_ESCALATE_AFTER_SECONDS - 1, config_path=None
        )

        assert get_drive_escalation(dq_cmd.SELF_CORDON_ALERT_REPO, dq_cmd.SELF_CORDON_ALERT_ISSUE) is None

    def test_same_reason_past_threshold_escalates_exactly_once(self, monkeypatch):
        pushed = []

        def _fake_push(reason, *, age_seconds, config_path):
            pushed.append((reason, age_seconds))
            return True  # simulated successful push

        monkeypatch.setattr(dq_cmd, "_push_self_cordon_escalation", _fake_push)
        reason = "drifted onto 'x'"
        t0 = 1000.0
        dq_cmd._escalate_persistent_self_cordon(reason, now=t0, config_path=None)
        # First tick past the threshold: fires.
        dq_cmd._escalate_persistent_self_cordon(
            reason, now=t0 + dq_cmd.SELF_CORDON_ESCALATE_AFTER_SECONDS, config_path=None
        )
        # Every subsequent tick with the SAME reason: does not fire again —
        # "fire once per persisted occurrence", mirroring the notifier's own
        # rule (docs/NOTIFIER.md) so a stuck self-cordon doesn't re-page
        # forever.
        dq_cmd._escalate_persistent_self_cordon(
            reason, now=t0 + dq_cmd.SELF_CORDON_ESCALATE_AFTER_SECONDS + 10_000, config_path=None
        )

        assert len(pushed) == 1
        assert pushed[0][0] == reason
        assert pushed[0][1] == dq_cmd.SELF_CORDON_ESCALATE_AFTER_SECONDS

    def test_reason_change_resets_the_clock_instead_of_escalating_immediately(self, monkeypatch):
        pushed = []

        def _fake_push(reason, *, age_seconds, config_path):
            pushed.append(reason)
            return True  # simulated successful push

        monkeypatch.setattr(dq_cmd, "_push_self_cordon_escalation", _fake_push)
        t0 = 1000.0
        dq_cmd._escalate_persistent_self_cordon("drifted onto 'a'", now=t0, config_path=None)
        dq_cmd._escalate_persistent_self_cordon(
            "drifted onto 'a'",
            now=t0 + dq_cmd.SELF_CORDON_ESCALATE_AFTER_SECONDS,
            config_path=None,
        )
        assert pushed == ["drifted onto 'a'"]

        # The branch changed under it (a NEW self-cordon reason) right after
        # the first one escalated — this must restart the clock, not
        # instantly re-escalate on the new reason's very first tick.
        dq_cmd._escalate_persistent_self_cordon(
            "drifted onto 'b'",
            now=t0 + dq_cmd.SELF_CORDON_ESCALATE_AFTER_SECONDS + 1,
            config_path=None,
        )
        assert pushed == ["drifted onto 'a'"]
        state = dq_cmd._read_self_cordon_state()
        assert state["reason"] == "drifted onto 'b'"
        assert state["escalated_at"] is None

    def test_condition_resolving_clears_state_and_a_recurrence_starts_fresh(self, monkeypatch):
        pushed = []

        def _fake_push(reason, *, age_seconds, config_path):
            pushed.append(reason)
            return True  # simulated successful push

        monkeypatch.setattr(dq_cmd, "_push_self_cordon_escalation", _fake_push)
        reason = "drifted onto 'x'"
        t0 = 1000.0
        dq_cmd._escalate_persistent_self_cordon(reason, now=t0, config_path=None)
        dq_cmd._escalate_persistent_self_cordon(
            reason, now=t0 + dq_cmd.SELF_CORDON_ESCALATE_AFTER_SECONDS, config_path=None
        )
        assert pushed == [reason]

        # Fixed — the drift is gone this tick.
        dq_cmd._escalate_persistent_self_cordon("", now=t0 + 5000.0, config_path=None)
        assert dq_cmd._read_self_cordon_state() is None

        # Recurs later: starts a fresh clock, does not immediately re-fire.
        dq_cmd._escalate_persistent_self_cordon(reason, now=t0 + 6000.0, config_path=None)
        assert pushed == [reason]
        state = dq_cmd._read_self_cordon_state()
        assert state["first_seen_at"] == t0 + 6000.0

    def test_failed_push_leaves_the_marker_unescalated_so_the_next_tick_retries(self, monkeypatch):
        """A transient failure (bad URL, DNS blip, server briefly down) at
        the exact moment the threshold is crossed must not permanently
        forfeit the push — mirrors `coord.notifier.service.deliver`'s "a
        failed send is not ledgered" policy, one layer down (#2572 review)."""
        pushed = []

        def _failing_push(reason, *, age_seconds, config_path):
            pushed.append(reason)
            return False  # simulated transient ntfy failure

        monkeypatch.setattr(dq_cmd, "_push_self_cordon_escalation", _failing_push)
        reason = "drifted onto 'x'"
        t0 = 1000.0
        dq_cmd._escalate_persistent_self_cordon(reason, now=t0, config_path=None)
        dq_cmd._escalate_persistent_self_cordon(
            reason, now=t0 + dq_cmd.SELF_CORDON_ESCALATE_AFTER_SECONDS, config_path=None
        )
        state = dq_cmd._read_self_cordon_state()
        assert state["escalated_at"] is None

        # Next tick, same still-unbroken reason: retries rather than staying
        # silently forfeited for the rest of this occurrence's lifetime.
        dq_cmd._escalate_persistent_self_cordon(
            reason, now=t0 + dq_cmd.SELF_CORDON_ESCALATE_AFTER_SECONDS + 60, config_path=None
        )
        assert pushed == [reason, reason]


class TestPushSelfCordonEscalation:
    def test_records_a_distinct_escalation_key(self, monkeypatch):
        """Must not collide with the routine `plan.alert` recording under
        `QUEUE_ALERT_REPO`/`QUEUE_ALERT_ISSUE` (`_escalate`, called
        separately every tick) — a distinct key so this escalation survives
        independently of whatever the rest of the tick's ordinary alert
        handling does to its own."""
        monkeypatch.setattr("coord.commands._common._load_config", _disabled_config)

        dq_cmd._escalate(
            dq_cmd.QUEUE_ALERT_REPO,
            dq_cmd.QUEUE_ALERT_ISSUE,
            reason="no launch — routine per-tick alert",
            gates="",
            command="",
        )
        result = dq_cmd._push_self_cordon_escalation(
            "drifted onto 'x'", age_seconds=1800.0, config_path=None
        )
        assert result is True  # nothing to push — disabled is a legitimate reason

        routine = get_drive_escalation(dq_cmd.QUEUE_ALERT_REPO, dq_cmd.QUEUE_ALERT_ISSUE)
        persistent = get_drive_escalation(
            dq_cmd.SELF_CORDON_ALERT_REPO, dq_cmd.SELF_CORDON_ALERT_ISSUE
        )
        assert routine is not None and routine["reason"] == "no launch — routine per-tick alert"
        assert persistent is not None
        assert persistent["stage"] == dq_cmd.SELF_CORDON_ALERT_STAGE
        assert "30" in persistent["reason"] or "1800" in persistent["gate_readings"]
        assert "drifted onto 'x'" in persistent["reason"]

    def test_never_raises_when_notifier_config_is_unreachable(self, monkeypatch):
        """The DB record is the floor; a broken import/config on the push
        half must not take the tick down with it (#2572, same isolation
        rule docs/NOTIFIER.md states for the notifier's own tick). But the
        push itself never happened, so this must read as a retry-able
        failure, not a silently-forfeited success."""

        def _boom(_path):
            raise RuntimeError("simulated config load failure")

        monkeypatch.setattr("coord.commands._common._load_config", _boom)

        result = dq_cmd._push_self_cordon_escalation(
            "drifted onto 'x'", age_seconds=1800.0, config_path=None
        )

        assert result is False
        assert get_drive_escalation(
            dq_cmd.SELF_CORDON_ALERT_REPO, dq_cmd.SELF_CORDON_ALERT_ISSUE
        ) is not None

    def test_never_raises_when_recording_the_escalation_fails(self, monkeypatch):
        """`record_drive_escalation` can route over the network to the board
        daemon — plausible to fail during exactly the kind of fleet
        instability that makes this feature necessary. Mirrors
        `_escalate_roll_pending_expired`'s identical guard, and this
        function's own docstring promise to "never raise" (#2572 review)."""
        monkeypatch.setattr("coord.commands._common._load_config", _disabled_config)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated board daemon failure")

        monkeypatch.setattr("coord.state.record_drive_escalation", _boom)

        # Must not raise, and since nothing is configured to push through
        # (disabled), this still counts as "nothing left to retry".
        result = dq_cmd._push_self_cordon_escalation(
            "drifted onto 'x'", age_seconds=1800.0, config_path=None
        )
        assert result is True

    def test_attempts_an_ntfy_push_when_notifications_are_enabled(self, monkeypatch):
        sent = []

        def _fake_safe_send(transport, message):
            sent.append((transport, message))
            return SendResult(ok=True)

        monkeypatch.setattr("coord.notifier.transport.safe_send", _fake_safe_send)
        notif_cfg = SimpleNamespace(
            enabled=True,
            transport="ntfy",
            ntfy_url="http://dellserver:7440",
            ntfy_topic="coord-fleet",
            ntfy_token=None,
            timeout_secs=5.0,
        )
        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda _path=None: SimpleNamespace(notifications=notif_cfg),
        )

        result = dq_cmd._push_self_cordon_escalation(
            "drifted onto 'x'", age_seconds=1800.0, config_path=None
        )

        assert result is True
        assert len(sent) == 1
        _, message = sent[0]
        assert "self-cordon" in message.title.lower() or "cordon" in message.title.lower()
        assert "drifted onto 'x'" in message.body

    def test_returns_false_without_raising_when_the_ntfy_push_fails(self, monkeypatch):
        """The core of the #2572 review finding: a transient send failure
        (bad URL, DNS blip, server briefly down) must be reported back as
        "not escalated" so the caller retries next tick, instead of being
        silently treated the same as a successful push."""
        monkeypatch.setattr(
            "coord.notifier.transport.safe_send",
            lambda transport, message: SendResult(ok=False, error="connection refused"),
        )
        notif_cfg = SimpleNamespace(
            enabled=True,
            transport="ntfy",
            ntfy_url="http://dellserver:7440",
            ntfy_topic="coord-fleet",
            ntfy_token=None,
            timeout_secs=5.0,
        )
        monkeypatch.setattr(
            "coord.commands._common._load_config",
            lambda _path=None: SimpleNamespace(notifications=notif_cfg),
        )

        result = dq_cmd._push_self_cordon_escalation(
            "drifted onto 'x'", age_seconds=1800.0, config_path=None
        )

        assert result is False
        # The drive_escalations record still lands — only the ntfy push
        # (and, in turn, whether the marker advances to "escalated") is
        # gated on the send outcome.
        assert get_drive_escalation(
            dq_cmd.SELF_CORDON_ALERT_REPO, dq_cmd.SELF_CORDON_ALERT_ISSUE
        ) is not None

    def test_does_not_push_when_notifications_are_disabled(self, monkeypatch):
        sent = []
        monkeypatch.setattr(
            "coord.notifier.transport.safe_send",
            lambda transport, message: sent.append((transport, message)) or SendResult(ok=True),
        )
        monkeypatch.setattr("coord.commands._common._load_config", _disabled_config)

        result = dq_cmd._push_self_cordon_escalation(
            "drifted onto 'x'", age_seconds=1800.0, config_path=None
        )

        assert result is True
        assert sent == []
        assert get_drive_escalation(
            dq_cmd.SELF_CORDON_ALERT_REPO, dq_cmd.SELF_CORDON_ALERT_ISSUE
        ) is not None


class TestSelfCordonStateFileIsolation:
    def test_state_path_honours_the_env_override(self, monkeypatch, tmp_path):
        """Guards the guard: `tests/conftest.py`'s `_no_real_self_cordon_state`
        autouse fixture relies on this env var actually being read."""
        custom = tmp_path / "custom-marker.json"
        monkeypatch.setenv("COORD_SELF_CORDON_STATE", str(custom))

        assert dq_cmd._self_cordon_state_path() == custom
