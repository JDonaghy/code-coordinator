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
"""

from __future__ import annotations

import time

from click.testing import CliRunner

from coord import machine_pause
from coord.cli import main
from coord.commands import drive_queue as dq_cmd
from coord.commands import release as release_cmd
from coord.drive_queue import RollPending


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
        assert payload == {"cancelled": None, "cleared_cordons": []}


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
