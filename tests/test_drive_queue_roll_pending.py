"""#2607: the #2587 roll-pending marker must not freeze the drive queue
forever, and an operator must have a way to break it if it does.

#2587 gave the marker a self-bounding escape hatch (`RollPending.expired` —
a TTL and a deferral ceiling). #2607's incident traced why that escape never
fired in practice: every re-arm (`_ensure_roll_pending_marker` /
`coord release nightly-window`'s own "replace a stale marker" step) wrote a
BRAND NEW `RollPending`, resetting `set_at` to now and `deferrals` to 0.
`Auto-release on merge to main` cuts a version on nearly every merge, so a
busy fleet's target had almost always moved by the time either re-arm site
next ran — meaning the "different target -> fresh marker" branch fired on
essentially every attempt, and the bound that was supposed to save the
queue was never actually reachable. 23 waiting entries sat for over an hour
with nothing to cancel it: no self-recovery, no CLI override.

This file covers the two halves of the fix:

1. `TestEnsureRollPendingMarkerPreservesTheEscapeHatch` — the write-site fix
   at the unit level: replacing a marker for a different (newly resolved)
   target must carry the ORIGINAL `set_at`/`deferrals` forward, not reset
   them. Only a marker written from scratch (nothing existing at all) gets a
   fresh clock. `tests/test_cli_release_window.py` and
   `tests/test_cli_release_propagate.py` exercise the same write sites
   through their own CLIs; this file asserts the shared contract directly.
2. `TestCancelRoll` — the new `coord drive-queue cancel-roll` escape hatch:
   clears the marker (and reports when there is none to clear), and clears
   any release cordon still open for the SAME target as a matched pair, so
   cancelling actually resumes the queue rather than trading one hold for
   another.
3. `TestSurfacedInStatusAndDoctor` — scope item 3: `coord status` and
   `coord doctor` name the marker (and the cancel command) where an operator
   already looks, not just in `coord drive-queue status`.

#2889 is the follow-up: #2607's fix (above) is verified working in
production, but it only bounds ONE marker's own re-arms — nothing bounded
how often a FRESH marker (a brand new one, following a PRIOR marker that
reached its own TTL/deferral bound and was cleared) could be armed. Ten of
those in ~15 hours froze the queue for 49 ticks, each individual marker
perfectly well-behaved. This file's remaining classes cover that gap —
`RollLedger`, the memory that survives a marker's own clear:

4. `TestRollLedgerRateLimit` — item 3: a fresh arm within
   `ROLL_LEDGER_MIN_ARM_INTERVAL_SECONDS` of the last one is refused, not
   armed — the black-box shape #2889's own acceptance list asks for ("arm a
   marker, let it expire, assert a SECOND marker for the same target is
   refused/rate-limited rather than armed fresh").
5. `TestRollLedgerCumulativeEscalation` — item 1: cumulative frozen time
   across DISTINCT marker generations (never deferrals of one) crosses
   `ROLL_LEDGER_CUMULATIVE_BOUND_SECONDS` and every further fresh arm is
   refused until `coord drive-queue cancel-roll` clears the ledger.
6. `TestQueueProvablyBusyRefusal` — item 2: a fresh arm is declined when a
   genuine `drive_queue` row is provably occupying the daemon host, even
   though the daemon itself reads busy (the arm's own trigger condition).
7. `TestInvokedByRecorded` — item 4: `coord release nightly-window` reads
   `$COORD_ROLL_INVOKER` and journals it as `invoked_by`, so "what started
   this unit" is answerable from `coord release window-history` alone.
"""

from __future__ import annotations

import time

from click.testing import CliRunner

from coord import machine_pause
from coord.cli import main
from coord.commands import drive_queue as dq_cmd
from coord.commands import release as release_cmd
from coord.drive_queue import (
    ROLL_LEDGER_CUMULATIVE_BOUND_SECONDS,
    ROLL_LEDGER_MIN_ARM_INTERVAL_SECONDS,
    ROLL_PENDING_DEFAULT_TTL_SECONDS,
    RollLedger,
    RollPending,
)


def _pending(**overrides) -> RollPending:
    kwargs = {"target_version": "0.5.235", "set_at": 1000.0, "reason": "nightly-window"}
    kwargs.update(overrides)
    return RollPending(**kwargs)


class TestEnsureRollPendingMarkerPreservesTheEscapeHatch:
    def test_no_existing_marker_starts_a_fresh_clock(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 5000.0)
        assert dq_cmd.read_roll_pending() is None

        release_cmd._ensure_roll_pending_marker("0.5.235", reason="propagate")

        pending = dq_cmd.read_roll_pending()
        assert pending is not None
        assert pending.target_version == "0.5.235"
        assert pending.reason == "propagate"
        assert pending.set_at == 5000.0
        assert pending.deferrals == 0

    def test_same_target_is_a_pure_no_op(self, monkeypatch):
        """Unchanged from before #2607 — asserted here too since it is the
        same function the "different target" branch below lives in."""
        dq_cmd.write_roll_pending(_pending(deferrals=6))
        monkeypatch.setattr(time, "time", lambda: 9999.0)

        release_cmd._ensure_roll_pending_marker("0.5.235", reason="propagate")

        pending = dq_cmd.read_roll_pending()
        assert pending.set_at == 1000.0
        assert pending.deferrals == 6
        assert pending.reason == "nightly-window"  # untouched, not even the reason

    def test_different_target_preserves_set_at_and_deferrals(self, monkeypatch):
        """The exact #2607 defect: PyPI's target climbed (0.5.235 ->
        0.5.236) while the marker was still live and unexpired. Replacing it
        must carry the original clock and deferral count forward — a re-arm
        is a continuation of the same stuck roll, not a new request."""
        dq_cmd.write_roll_pending(_pending(target_version="0.5.235", set_at=1000.0, deferrals=6))
        monkeypatch.setattr(time, "time", lambda: 5000.0)

        release_cmd._ensure_roll_pending_marker("0.5.236", reason="propagate")

        pending = dq_cmd.read_roll_pending()
        assert pending is not None
        assert pending.target_version == "0.5.236"
        assert pending.reason == "propagate"
        assert pending.set_at == 1000.0  # preserved, NOT 5000.0
        assert pending.deferrals == 6  # preserved, NOT reset to 0

    def test_repeated_re_arms_still_expire_on_the_original_schedule(self, monkeypatch):
        """The cumulative-time requirement (#2607 scope item 4): however
        many times the target moves and this gets re-armed, the TTL must
        still measure from the FIRST time the queue got stuck, not the last
        re-arm."""
        dq_cmd.write_roll_pending(_pending(target_version="0.5.235", set_at=1000.0, ttl_seconds=3600))

        for i, target in enumerate(("0.5.236", "0.5.237", "0.5.238"), start=1):
            monkeypatch.setattr(time, "time", lambda i=i: 1000.0 + i * 500.0)
            release_cmd._ensure_roll_pending_marker(target, reason="propagate")

        pending = dq_cmd.read_roll_pending()
        assert pending.target_version == "0.5.238"
        assert pending.set_at == 1000.0
        # Still bounded by the ORIGINAL set_at, not the last re-arm time.
        assert pending.expired(now=1000.0 + 3600.0)
        assert not pending.expired(now=1000.0 + 3599.0)


class TestCancelRoll:
    def _invoke(self, *args):
        return CliRunner().invoke(main, ["drive-queue", "cancel-roll", *args])

    def test_no_marker_reports_nothing_to_cancel(self):
        assert dq_cmd.read_roll_pending() is None

        result = self._invoke()

        assert result.exit_code == 0, result.output
        assert "no roll-pending marker to cancel" in result.output
        assert dq_cmd.read_roll_pending() is None

    def test_clears_a_live_marker(self):
        dq_cmd.write_roll_pending(_pending(deferrals=6))

        result = self._invoke()

        assert result.exit_code == 0, result.output
        assert "cancelled roll pending: v0.5.235" in result.output
        assert dq_cmd.read_roll_pending() is None

    def test_clears_a_cordon_open_for_the_same_target(self):
        dq_cmd.write_roll_pending(_pending(target_version="0.5.235"))
        machine_pause.set_cordon("dellserver", reason="draining", target_version="0.5.235")

        result = self._invoke()

        assert result.exit_code == 0, result.output
        assert "dellserver" in result.output
        assert dq_cmd.read_roll_pending() is None
        assert "dellserver" not in machine_pause.cordons()

    def test_leaves_a_cordon_for_a_different_target_alone(self):
        """A cordon draining for some OTHER release is not this marker's to
        clear — clearing it would silently let a host the operator cordoned
        for an unrelated reason start taking work again."""
        dq_cmd.write_roll_pending(_pending(target_version="0.5.235"))
        machine_pause.set_cordon("laptop", reason="draining", target_version="0.5.100")

        result = self._invoke()

        assert result.exit_code == 0, result.output
        assert dq_cmd.read_roll_pending() is None
        assert "laptop" in machine_pause.cordons()

    def test_json_output(self):
        dq_cmd.write_roll_pending(_pending(target_version="0.5.235"))
        machine_pause.set_cordon("dellserver", reason="draining", target_version="0.5.235")

        result = self._invoke("--json")

        assert result.exit_code == 0, result.output
        import json

        payload = json.loads(result.output)
        assert payload["cancelled"]["target_version"] == "0.5.235"
        assert payload["cleared_cordons"] == ["dellserver"]

    def test_json_output_with_nothing_pending(self):
        result = self._invoke("--json")

        assert result.exit_code == 0, result.output
        import json

        payload = json.loads(result.output)
        assert payload == {
            "cancelled": None, "cleared_cordons": [], "ledger_reset": None,
        }


class TestTickResumesAfterCancel:
    """The end-to-end acceptance shape: a marker that would otherwise hold
    the queue down is gone the instant `cancel-roll` runs, and a subsequent
    `coord drive-queue tick` behaves exactly as if the marker had expired on
    its own (#2587's own bound achieves this today; `cancel-roll` is the
    same outcome on operator demand instead of on a clock)."""

    def test_cancel_then_status_shows_no_marker(self):
        dq_cmd.write_roll_pending(_pending())
        assert dq_cmd.read_roll_pending() is not None

        cancel = CliRunner().invoke(main, ["drive-queue", "cancel-roll"])
        assert cancel.exit_code == 0, cancel.output

        status = CliRunner().invoke(main, ["drive-queue", "status"])
        assert status.exit_code == 0, status.output
        assert "roll pending" not in status.output


class TestSurfacedInStatusAndDoctor:
    """#2607 scope item 3: a live marker must be visible from `coord status`
    and `coord doctor`, not just `coord drive-queue status` — an operator
    asking "why is nothing happening" reaches for the general-purpose
    commands first and should not have to already know a roll-pending
    marker exists to find it."""

    def test_status_names_the_marker_and_the_escape_hatch(
        self, valid_config_path, monkeypatch
    ):
        import coord.network as network_mod
        from coord.commands.status import status as status_cmd

        monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: [])
        dq_cmd.write_roll_pending(_pending(target_version="0.5.236", deferrals=2))

        result = CliRunner().invoke(
            status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False,
        )

        assert result.exit_code == 0, result.output
        assert "roll pending: v0.5.236" in result.output
        assert "2/" in result.output  # deferrals count
        assert "coord drive-queue cancel-roll" in result.output

    def test_status_says_nothing_when_no_marker(self, valid_config_path, monkeypatch):
        import coord.network as network_mod
        from coord.commands.status import status as status_cmd

        monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: [])
        assert dq_cmd.read_roll_pending() is None

        result = CliRunner().invoke(
            status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False,
        )

        assert result.exit_code == 0, result.output
        assert "roll pending" not in result.output
        assert "cancel-roll" not in result.output

    def test_doctor_names_the_marker_and_the_escape_hatch(
        self, valid_config_path, monkeypatch
    ):
        import coord.network as network_mod
        from coord.commands.status import doctor as doctor_cmd

        monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: [])
        dq_cmd.write_roll_pending(_pending(target_version="0.5.236", deferrals=2))

        result = CliRunner().invoke(
            doctor_cmd,
            ["--config", str(valid_config_path), "--no-pypi"],
            catch_exceptions=False,
        )

        assert "roll pending: v0.5.236" in result.output
        assert "coord drive-queue cancel-roll" in result.output


# ── #2889: bound the RATE of fresh markers, not just one marker's own life ──


class TestRollLedgerRateLimit:
    """Item 3 — and the issue's own acceptance list, bullet 1: "arm a
    marker ... let it expire, and assert a SECOND marker for the same
    target is refused (or rate-limited) rather than armed fresh." None of
    this exists on unfixed `main`: `_ensure_roll_pending_marker` always
    returns `None` there and always writes a fresh marker with no refusal
    of any kind — every assertion below fails against it.

    The rate limit's clock is the marker's own CLEAR, not its arm — see
    `RollLedger.record_expiry`'s docstring for why measuring from the arm
    would let the exact "re-arm right after natural TTL expiry" case sail
    through (by the time a full-TTL marker clears, 3600s has already
    elapsed — more than the 900s rate limit on its own). `_expire_a_marker`
    below simulates exactly what the tick's own capacity-0 branch does:
    fold the live marker's duration into the ledger, THEN clear it.
    """

    @staticmethod
    def _expire_a_marker(*, now: float) -> None:
        pending = dq_cmd.read_roll_pending()
        assert pending is not None, "nothing to expire"
        ledger = dq_cmd.read_roll_ledger().record_expiry(pending, now=now)
        dq_cmd.write_roll_ledger(ledger)
        dq_cmd.clear_roll_pending()

    def test_a_second_fresh_arm_right_after_expiry_is_refused(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10_000.0)
        armed = release_cmd._ensure_roll_pending_marker("0.5.235", reason="propagate")
        assert armed is True

        expiry = 10_000.0 + ROLL_PENDING_DEFAULT_TTL_SECONDS + 60.0
        monkeypatch.setattr(time, "time", lambda: expiry)
        self._expire_a_marker(now=expiry)

        refused = release_cmd._ensure_roll_pending_marker("0.5.236", reason="propagate")

        assert refused is False
        assert dq_cmd.read_roll_pending() is None, (
            "a rate-limited fresh arm must not write a marker at all — the "
            "queue keeps launching normally"
        )

    def test_a_fresh_arm_after_the_interval_elapses_proceeds(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10_000.0)
        release_cmd._ensure_roll_pending_marker("0.5.235", reason="propagate")
        expiry = 10_000.0 + ROLL_PENDING_DEFAULT_TTL_SECONDS + 60.0
        monkeypatch.setattr(time, "time", lambda: expiry)
        self._expire_a_marker(now=expiry)

        monkeypatch.setattr(
            time, "time", lambda: expiry + ROLL_LEDGER_MIN_ARM_INTERVAL_SECONDS + 1.0,
        )

        armed = release_cmd._ensure_roll_pending_marker("0.5.236", reason="propagate")

        assert armed is True
        pending = dq_cmd.read_roll_pending()
        assert pending is not None
        assert pending.target_version == "0.5.236"

    def test_a_refusal_never_bumps_the_rate_limit_clock(self, monkeypatch):
        """A refused attempt spends nothing — only an EXPIRY moves
        `last_expired_at`, so a caller retrying every few seconds while
        rate-limited does not somehow push its own unblock time later."""
        monkeypatch.setattr(time, "time", lambda: 10_000.0)
        release_cmd._ensure_roll_pending_marker("0.5.235", reason="propagate")
        expiry = 10_000.0 + ROLL_PENDING_DEFAULT_TTL_SECONDS + 60.0
        monkeypatch.setattr(time, "time", lambda: expiry)
        self._expire_a_marker(now=expiry)

        monkeypatch.setattr(time, "time", lambda: expiry + 100.0)
        release_cmd._ensure_roll_pending_marker("0.5.236", reason="propagate")  # refused
        monkeypatch.setattr(time, "time", lambda: expiry + 200.0)
        release_cmd._ensure_roll_pending_marker("0.5.237", reason="propagate")  # refused

        monkeypatch.setattr(
            time, "time", lambda: expiry + ROLL_LEDGER_MIN_ARM_INTERVAL_SECONDS + 1.0,
        )
        armed = release_cmd._ensure_roll_pending_marker("0.5.238", reason="propagate")
        assert armed is True  # unblocked from the EXPIRY's clock, not pushed later


class TestRollLedgerCumulativeEscalation:
    """Item 1 — acceptance bullet 2: cumulative frozen time across DISTINCT
    marker generations (never deferrals of one already-live marker — that
    half is #2607's, and stays green) crosses the bound and refuses every
    further fresh arm until an operator intervenes. Fails against unfixed
    `main`: there is no `RollLedger`, no escalation, and no refusal — a
    fresh arm always just succeeds.
    """

    def test_escalates_only_after_the_bound_is_crossed_by_distinct_markers(self):
        """Pure arithmetic on `RollLedger` itself — the exact shape the
        issue's acceptance calls for: N *distinct* markers, not N deferrals
        of one. Four markers, each contributing a quarter of the bound,
        cross it on the fourth; three alone do not."""
        ledger = RollLedger()
        now = 0.0
        chunk = ROLL_LEDGER_CUMULATIVE_BOUND_SECONDS / 4
        for _ in range(3):
            pending = RollPending(target_version="x", set_at=now)
            now += chunk
            ledger = ledger.record_expiry(pending, now=now)
        assert ledger.marker_count == 3
        assert not ledger.escalated, "three quarters of the bound must not escalate yet"

        pending = RollPending(target_version="x", set_at=now)
        now += chunk
        ledger = ledger.record_expiry(pending, now=now)

        assert ledger.marker_count == 4
        assert ledger.escalated

    def test_an_escalated_ledger_refuses_every_fresh_arm(self, monkeypatch):
        """The integration half: once the ledger reads `escalated`,
        `_ensure_roll_pending_marker`'s one FRESH-arm branch must refuse,
        whatever target is asked for — not just the one that ran up the
        history."""
        monkeypatch.setattr(time, "time", lambda: 50_000.0)
        dq_cmd.write_roll_ledger(
            RollLedger(
                cumulative_frozen_seconds=ROLL_LEDGER_CUMULATIVE_BOUND_SECONDS,
                marker_count=4,
                last_expired_at=0.0,
            )
        )

        refused = release_cmd._ensure_roll_pending_marker("9.9.9", reason="propagate")

        assert refused is False
        assert dq_cmd.read_roll_pending() is None

    def test_cancel_roll_clears_an_escalated_ledger_with_no_live_marker(self):
        """The operator escape hatch item 1 requires: "refuse ... until an
        operator intervenes." `coord drive-queue cancel-roll` is that
        intervention — and must work even when the marker that ran up the
        ledger already expired and cleared on its own, leaving only the
        ledger's escalation behind."""
        dq_cmd.write_roll_ledger(
            RollLedger(
                cumulative_frozen_seconds=ROLL_LEDGER_CUMULATIVE_BOUND_SECONDS + 1.0,
                marker_count=5,
            )
        )
        assert dq_cmd.read_roll_pending() is None  # nothing LIVE to cancel

        result = CliRunner().invoke(main, ["drive-queue", "cancel-roll"])

        assert result.exit_code == 0, result.output
        assert "roll ledger" in result.output
        ledger = dq_cmd.read_roll_ledger()
        assert not ledger.escalated
        assert ledger.cumulative_frozen_seconds == 0.0
        assert ledger.marker_count == 0


class TestQueueProvablyBusyRefusal:
    """Item 2: a FRESH arm is declined when a genuine `drive_queue` row is
    provably occupying the daemon host right now — even though the daemon
    itself reading busy is the arm site's own TRIGGER condition. Fails
    against unfixed `main`: `_ensure_roll_pending_marker` has no
    `queue_provably_busy` parameter at all, and always arms."""

    def test_a_provably_busy_queue_declines_a_fresh_arm(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 10_000.0)

        refused = release_cmd._ensure_roll_pending_marker(
            "0.5.235", reason="propagate", queue_provably_busy=True,
        )

        assert refused is False
        assert dq_cmd.read_roll_pending() is None, (
            "declining to arm must not write a marker — the queue keeps "
            "launching normally rather than being frozen for a marker that "
            "cannot roll any faster than the tick's own reconciliation will"
        )

    def test_a_re_arm_of_an_existing_marker_ignores_queue_provably_busy(self, monkeypatch):
        """#2889 item 2 only ever gates a FRESH arm — a re-arm of an
        already-live marker (a continuation of a campaign in progress) must
        proceed regardless, or it would reintroduce #2607's own bug by a
        different door (a marker that cannot be kept current fires against
        a target it no longer names)."""
        dq_cmd.write_roll_pending(_pending(target_version="0.5.235", set_at=1000.0))
        monkeypatch.setattr(time, "time", lambda: 10_000.0)

        armed = release_cmd._ensure_roll_pending_marker(
            "0.5.236", reason="propagate", queue_provably_busy=True,
        )

        assert armed is True
        pending = dq_cmd.read_roll_pending()
        assert pending is not None
        assert pending.target_version == "0.5.236"
        assert pending.set_at == 1000.0  # #2607's own preservation, untouched


class TestInvokedByRecorded:
    """Item 4: `coord release nightly-window` reads `$COORD_ROLL_INVOKER`
    off its own process environment and journals it as
    `WindowRecord.invoked_by`, so "what started this unit" is answerable
    from `coord release window-history` — no live reproduction required.
    Fails against unfixed `main`: `WindowRecord` has no `invoked_by` field,
    and nothing reads the env var."""

    def test_the_env_var_is_read_back_in_the_journal(
        self, valid_config_path, monkeypatch, tmp_path,
    ):
        import coord.release_verify as rv
        import coord.release_window as rw
        from coord.commands import release as release_cmd_mod

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setattr(release_cmd_mod, "_state_dir", lambda: state_dir)
        monkeypatch.setattr(
            rv, "gather",
            lambda *a, **k: (
                {"server": {"version": "0.5.31", "health": {"schema": 1, "results": []}}},
                {}, None, "server",
            ),
        )
        monkeypatch.setattr(
            rv, "verify",
            lambda **kwargs: rv.VerifyReport(
                expected=kwargs.get("expected"),
                lanes=[rv.Lane(host="server", lane="~/.coord-venv", version="0.5.31")],
                findings=[],
            ),
        )
        monkeypatch.setenv("COORD_ROLL_INVOKER", "drive-queue-tick")

        result = CliRunner().invoke(
            main,
            ["release", "nightly-window", "--config", str(valid_config_path),
             "--target", "0.5.31", "--daemon-host", "server"],
        )

        assert result.exit_code == 0, result.output
        assert "invoked by: drive-queue-tick" in result.output
        records = rw.read_records(state_dir)
        assert records[-1]["invoked_by"] == "drive-queue-tick"
