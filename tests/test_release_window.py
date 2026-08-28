"""The nightly release-window decision half (#2112).

`coord/release_window.py` decides one thing a nightly, unattended job lives
or dies by: is there anything for tonight's window to do at all, and — via
its journal — did last night's attempt actually confirm a roll or just say
so. Both are testable without a fleet, a clock, or systemd; the I/O shell
that stops/starts the queue timer and shells out to `coord release
propagate` is covered black-box in `tests/test_cli_release_window.py`.
"""

from __future__ import annotations

import pytest

from coord import release_window as rw


# ── needs_roll: acceptance 3, "already current -> the queue is never
#    touched" starts here ──────────────────────────────────────────────────


def test_a_current_daemon_needs_no_roll():
    assert rw.needs_roll("0.5.31", "0.5.31") is False


def test_an_ahead_daemon_needs_no_roll():
    """PyPI resolution lag, or a hand-rolled host: never behind, never
    grounds to stop the queue."""
    assert rw.needs_roll("0.5.32", "0.5.31") is False


def test_a_behind_daemon_needs_a_roll():
    assert rw.needs_roll("0.5.30", "0.5.31") is True


def test_a_cross_series_behind_daemon_needs_a_roll():
    assert rw.needs_roll("0.4.9", "0.5.1") is True


def test_unreadable_daemon_version_needs_a_roll_not_a_guess():
    """#1834's rule, reapplied here: `None` is "no data", never "agrees with
    the target". Reading it as "current" would silently skip the ONE window
    this issue exists to guarantee, on a host that could not even be read."""
    assert rw.needs_roll(None, "0.5.31") is True


def test_no_target_version_needs_no_roll():
    """There is nothing to roll TO — the caller's own setup failure, not a
    verdict about the daemon host."""
    assert rw.needs_roll("0.5.30", None) is False
    assert rw.needs_roll(None, None) is False


# ── WindowRecord ─────────────────────────────────────────────────────────


def test_ok_statuses_are_exactly_the_happy_and_correctly_inert_ones():
    assert rw.OK_STATUSES == {
        rw.STATUS_UP_TO_DATE, rw.STATUS_ROLLED, rw.STATUS_DRY_RUN,
        # #2587: a set marker is a GOOD outcome — the drive-queue tick, not
        # this command, fires the actual roll at the next inter-drive gap.
        rw.STATUS_ROLL_PENDING,
        # #2583: holding below the min-releases-behind threshold is also a
        # GOOD, correctly-inert outcome — not a night propagation failed to
        # happen, but one that deliberately declined to.
        rw.STATUS_HOLDING,
        # #2889: declining a FRESH arm (rate-limited, or a genuine
        # drive-queue entry provably occupying the daemon host) is the SAME
        # shape — the queue keeps launching, a later run tries again.
        rw.STATUS_ARM_DEFERRED,
    }


def test_loud_statuses_are_everything_else():
    """Trap 3: every status that means "a night propagation was supposed to
    happen and did not" must be outside OK_STATUSES, so the shell's decision
    to escalate can key off the same closed set the record itself carries."""
    assert rw.LOUD_STATUSES == {
        rw.STATUS_DRAIN_TIMEOUT,
        rw.STATUS_PROPAGATE_DEFERRED,
        rw.STATUS_PROPAGATE_FAILED,
        rw.STATUS_ERROR,
        # #2889: the roll ledger crossing its cumulative bound means this
        # target has now failed to roll unattended across SEVERAL marker
        # generations, not just one busy night — an operator must
        # intervene (`coord drive-queue cancel-roll`), same "supposed to
        # happen and did not" tier as a genuine propagate failure.
        rw.STATUS_LEDGER_ESCALATED,
    }
    assert not (rw.OK_STATUSES & rw.LOUD_STATUSES)


@pytest.mark.parametrize("status", sorted(rw.OK_STATUSES))
def test_record_ok_property_matches_ok_statuses(status):
    record = rw.WindowRecord(started_at=1.0, status=status)
    assert record.ok is True


@pytest.mark.parametrize("status", sorted(rw.LOUD_STATUSES))
def test_record_ok_property_is_false_for_loud_statuses(status):
    record = rw.WindowRecord(started_at=1.0, status=status)
    assert record.ok is False


# ── the journal: #2112's "report the outcome ... whether or not anything
#    rolled" starts with a record surviving to be read back ───────────────


def test_append_and_read_round_trips(tmp_path):
    record = rw.WindowRecord(
        started_at=100.0, target_version="0.5.31", daemon_host="dellserver",
        daemon_version="0.5.30", status=rw.STATUS_ROLLED, queue_stopped=True,
        drained=True, drain_seconds=12.5, queue_restarted=True,
        propagate_status="verified", propagate_exit_code=0, finished_at=200.0,
    )
    rw.append_record(tmp_path, record)
    records = rw.read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["target_version"] == "0.5.31"
    assert records[0]["status"] == rw.STATUS_ROLLED
    assert records[0]["drained"] is True


def test_read_records_on_a_torn_final_line_keeps_the_earlier_ones(tmp_path):
    """A process killed mid-append (#2112 acceptance 4) must not make the
    whole history unreadable — the history is most valuable in exactly that
    case."""
    rw.append_record(tmp_path, rw.WindowRecord(started_at=1.0, status=rw.STATUS_UP_TO_DATE))
    path = rw.journal_path(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"started_at": 2.0, "status": "roll')  # torn, no trailing newline
    records = rw.read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == rw.STATUS_UP_TO_DATE


def test_read_records_respects_limit(tmp_path):
    for i in range(5):
        rw.append_record(tmp_path, rw.WindowRecord(started_at=float(i), status=rw.STATUS_UP_TO_DATE))
    records = rw.read_records(tmp_path, limit=2)
    assert len(records) == 2
    assert records[-1]["started_at"] == 4.0  # most-recent-last


def test_trim_journal_keeps_only_the_most_recent(tmp_path):
    for i in range(10):
        rw.append_record(tmp_path, rw.WindowRecord(started_at=float(i), status=rw.STATUS_UP_TO_DATE))
    kept = rw.trim_journal(tmp_path, keep=3)
    assert kept == 3
    records = rw.read_records(tmp_path)
    assert [r["started_at"] for r in records] == [7.0, 8.0, 9.0]


def test_read_records_on_missing_journal_is_empty(tmp_path):
    assert rw.read_records(tmp_path) == []


# ── rendering ────────────────────────────────────────────────────────────


def test_render_record_names_the_status_and_version():
    record = rw.WindowRecord(
        started_at=1_700_000_000.0, target_version="0.5.31", status=rw.STATUS_ROLLED,
    )
    lines = rw.render_record(record)
    assert "v0.5.31" in lines[0]
    assert rw.STATUS_ROLLED in lines[0]


def test_render_record_surfaces_the_error_line():
    record = rw.WindowRecord(
        started_at=1.0, status=rw.STATUS_DRAIN_TIMEOUT,
        error="drain deadline (3600s) hit with dellserver still busy",
    )
    lines = rw.render_record(record)
    assert any("drain deadline" in line for line in lines)


def test_render_record_marks_dry_run():
    record = rw.WindowRecord(started_at=1.0, status=rw.STATUS_DRY_RUN, dry_run=True)
    lines = rw.render_record(record)
    assert "[dry-run]" in lines[0]
