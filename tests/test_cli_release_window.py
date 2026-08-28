"""Black-box tests for `coord release nightly-window` (#2112, rewritten #2587).

`coord release propagate` cannot roll the daemon host past a busy fleet on
its own: the daemon leads every roll (the documented 405), and dellserver's
own drive-queue tick charges itself busy for essentially any queued drive —
see `coord/release_window.py`'s module docstring for the full mechanism.

#2587 REWRITE: this command used to manufacture the window itself — stop
`coord-drive-queue.timer`, poll a bounded drain (up to an hour), roll, ALWAYS
restart the timer. Measured 2026-08-22: that drain ran the full 60-minute
deadline, drained nothing, rolled nothing, with the timer (and therefore ALL
reconciliation and ALL new dispatch) stopped the entire time — the fleet-wide
quiescent window it waited for never arrives on a continuously-busy queue.
Now this command sets a roll-pending marker naming the target version and
returns immediately; `coord drive-queue tick` is what watches for the
fleet's own natural inter-drive gap (several times an hour, not never) and
fires the actual roll — see `tests/test_release_roll_window_handoff.py` for
that side of the mechanism, and `tests/test_release_window.py` for the pure
judgement (`needs_roll`, the journal shape, `STATUS_ROLL_PENDING`).

What's tested here is this command's own wiring:

1. a needed roll with nothing already pending -> sets the marker, touches no
   timer, calls `coord release propagate` zero times, returns immediately.
2. a marker already pending for a DIFFERENT (stale) target -> replaced, no
   fire attempt.
3. a marker already pending for the SAME target -> one best-effort attempt to
   fire it directly (never `--force`), for the operator-at-the-console case
   where the fleet happens to already be idle:
   a. verified/rolled/up-to-date -> marker cleared, reported as a success.
   b. still deferred (fleet busy) -> marker left exactly as it was, for the
      tick to keep watching; a normal, quiet, non-escalated outcome.
   c. a genuine failure -> marker survives (bounded by its own TTL/deferral
      ceiling — see `RollPending`), and this is loud (escalated).
4. the fleet already current -> nothing is touched at all.
5. success is never reported for a roll that was not actually confirmed
   (#2187's original bug, preserved through the rewrite via the SAME
   `_run_propagate` interpretation logic).
6. `--ensure-queue-running` (the legacy escape hatch) still just starts the
   timer and exits — nothing else in this command reaches for it any more.

Nothing here touches a real fleet or a real systemd: `_systemctl`,
`_run_propagate`, and `coord.release_verify.gather`/`.verify` are all seams.
The retired `_drain`/`_run_reconcile_tick` helpers are still directly unit
tested further down in this file — they remain valid, callable code, simply
no longer wired into this command's default path.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from coord import release_propagate as rp
from coord import release_window as rw
from coord.cli import main
from coord.commands import drive_queue as dq_cmd
from coord.commands import release as release_cmd
from coord.drive_queue import RollPending


@pytest.fixture(autouse=True)
def _own_pause_store(tmp_path, monkeypatch):
    """Give every test in this module its own pause store (#2174).

    `test_drain_is_blocked_by_a_paused_daemon_host` calls
    `mp.local_pause("server")`, and that store is per-`$HOME`, not
    per-test. `conftest._no_real_pause_store` redirects only when the
    resolved path lands under the REAL home, so under
    `scripts/run_tests_in_populated_home.sh` (#2170) — where `$HOME` is one
    throwaway directory shared by the whole run — the pause survives this
    test and every later one reads a machine it never paused. See
    `tests/test_cli_release_propagate.py::_own_pause_store` for the full
    write-up; this is the same hazard in the sibling module that shares the
    seam.
    """
    home = tmp_path / "home"
    (home / ".coord").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point the window journal at a tmp dir, never the real ~/.coord."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(release_cmd, "_state_dir", lambda: d)
    return d


@pytest.fixture()
def no_network(monkeypatch):
    """No PyPI lookup, no /board read unless a test says so."""
    monkeypatch.setattr(release_cmd, "_fetch_board", lambda: ({}, None))


@pytest.fixture()
def escalations(monkeypatch):
    """Capture every `record_drive_escalation` call `_escalate_window` makes."""
    calls: list[dict] = []

    def _fake(repo, issue, *, stage, reason, gate_readings, proposed_command,
             assignment_id=None):
        calls.append({"repo": repo, "issue": issue, "stage": stage, "reason": reason,
                      "gate_readings": gate_readings, "proposed_command": proposed_command})
        return 1

    monkeypatch.setattr("coord.state.record_drive_escalation", _fake)
    return calls


def _records(state_dir):
    return rw.read_records(state_dir)


def _serve_health(host: str) -> dict:
    return {
        "version": "0.5.31",
        "health": {"schema": 1, "results": [
            {"check_id": "spawned_coord", "subject": "coord-serve",
             "severity": "ok", "values": {"unit": "coord-serve", "pid": 1,
                                          "version": "0.5.31"}},
        ]},
    }


def _stub_verify(monkeypatch, *, daemon_version: str | None, daemon: str = "server",
                 extra_lanes=()):
    """Replace `coord.release_verify`'s fleet sweep with a canned daemon-host
    python lane — same seam `tests/test_cli_release_propagate.py` uses.

    ``extra_lanes`` lets a test add other python-lane rows for the same
    host (e.g. a `coord-agent process` lane, #2841) without duplicating this
    whole stub — `_python_lane_versions` takes the OLDEST of everything
    `verify_lane_kind` grades as `LANE_PYTHON`, so a stale extra lane here is
    exactly how a test proves that oldest-wins reach.
    """
    from coord import release_verify as rv

    lanes = [rv.Lane(host=daemon, lane="~/.coord-venv", version=daemon_version),
             *extra_lanes]
    machine_health = {daemon: _serve_health(daemon)}
    monkeypatch.setattr(rv, "gather",
                        lambda *a, **k: (machine_health, {}, None, daemon))
    monkeypatch.setattr(
        rv, "verify",
        lambda **kwargs: rv.VerifyReport(expected=kwargs.get("expected"), lanes=lanes,
                                         findings=[]),
    )


def _stub_systemctl(monkeypatch, *, stop_ok=True, start_ok=True):
    calls: list[tuple[str, str]] = []

    def _fake(unit, action, **kwargs):
        calls.append((unit, action))
        ok = stop_ok if action == "stop" else start_ok
        return ok, f"{action} {'ok' if ok else 'failed'}"

    monkeypatch.setattr(release_cmd, "_systemctl", _fake)
    return calls


def _stub_drain(monkeypatch, *, drained: bool, elapsed: float = 5.0, detail: str = ""):
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return rw.DrainOutcome(drained=drained, elapsed_seconds=elapsed, detail=detail)

    monkeypatch.setattr(release_cmd, "_drain", _fake)
    return calls


def _stub_propagate(monkeypatch, *, status: str, exit_code: int, output: str = "ok",
                    started_at: float | None = None):
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return status, exit_code, output, started_at

    monkeypatch.setattr(release_cmd, "_run_propagate", _fake)
    return calls


def _pending(**overrides):
    kwargs = {"target_version": "0.5.31", "set_at": 1000.0, "reason": "nightly-window"}
    kwargs.update(overrides)
    return RollPending(**kwargs)


# ── the fleet already current -> nothing is touched at all ───────────────


def test_an_already_current_daemon_never_touches_the_queue(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.31")
    systemctl_calls = _stub_systemctl(monkeypatch)
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert systemctl_calls == []
    assert prop_calls == []
    assert not escalations
    assert dq_cmd.read_roll_pending() is None
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_UP_TO_DATE
    assert record["queue_stopped"] is None
    assert record["queue_restarted"] is None


def test_an_already_current_daemon_clears_a_now_stale_pending_marker(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """The daemon reached the target some OTHER way (a human ran `coord
    release propagate` by hand) while a marker from an earlier run was still
    pending — leaving it standing would force `--reconcile-only` posture on
    the queue for nothing, up to its own TTL/deferral ceiling. No systemctl
    call and no `coord release propagate` subprocess either way (acceptance
    3 is still "nothing EXTERNAL is touched")."""
    _stub_verify(monkeypatch, daemon_version="0.5.31")
    systemctl_calls = _stub_systemctl(monkeypatch)
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    dq_cmd.write_roll_pending(_pending(target_version="0.5.31"))

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert systemctl_calls == []
    assert prop_calls == []
    assert dq_cmd.read_roll_pending() is None
    assert _records(state_dir)[0]["status"] == rw.STATUS_UP_TO_DATE


# ── a needed roll with nothing pending yet: set the marker, return ───────


def test_a_needed_roll_with_no_marker_pending_sets_one_and_returns_immediately(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2587's whole point: no stopped timer, no drain, no synchronous wait —
    a needed roll with nothing already pending just arms the marker the
    drive-queue tick watches, and returns."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    systemctl_calls = _stub_systemctl(monkeypatch)
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    assert dq_cmd.read_roll_pending() is None

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert systemctl_calls == []  # never touches any timer
    assert prop_calls == []  # nothing fired yet — the drive-queue tick does that

    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.5.31"
    assert pending.reason == "nightly-window"
    assert pending.deferrals == 0

    assert not escalations
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLL_PENDING
    assert record["status"] in rw.OK_STATUSES
    assert record["queue_stopped"] is None
    assert record["queue_restarted"] is None


def test_a_provably_busy_queue_declines_the_fresh_arm_outright(
    valid_config_path, state_dir, escalations, monkeypatch,
):
    """#2889 item 2 — this command's OWN fresh-arm site (distinct from
    `coord release propagate`'s, which routes through
    `_ensure_roll_pending_marker`): a genuine `drive_queue` row provably
    occupying the daemon host declines the arm outright — a marker cannot
    roll any faster than the tick's own reconciliation already will, so
    freezing capacity for one now would only spend TTL learning that for
    free. Routine (OK-tier, no escalation): the queue keeps launching
    normally, and a LATER run of this same nightly timer re-checks."""
    from coord.drive_queue import STATE_RUNNING

    _stub_verify(monkeypatch, daemon_version="0.5.30")
    systemctl_calls = _stub_systemctl(monkeypatch)
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: (
            {
                "drive_queue": [
                    {"repo_name": "api", "issue_number": 7,
                     "state": STATE_RUNNING, "launch_host": "server"},
                ],
                "assignments": [], "issues": [],
            },
            None,
        ),
    )

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )

    assert result.exit_code == 0, result.output
    assert systemctl_calls == []
    assert prop_calls == []
    assert dq_cmd.read_roll_pending() is None, (
        "a provably busy queue must decline the fresh arm outright — no "
        "marker, no capacity-0 freeze"
    )
    assert not escalations  # routine, not an escalation
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ARM_DEFERRED
    assert record["status"] in rw.OK_STATUSES


def test_a_staged_but_unrestarted_agent_reads_as_behind_not_up_to_date(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2841: the daemon host's `~/.coord-venv` swap can land — matching the
    target — while its `coord-agent` process is still running the old
    release, because the agent can NEVER self-restart on the daemon host
    (`agent_app.py`'s `_idle_restart_target` returns `None` there on
    purpose; only the ordered `/update` + `/restart-services` path this
    command's sibling, `coord release propagate`, drives can move it).

    Before the `coord-agent process` lane existed, `~/.coord-venv` was the
    ONLY python lane this command could see for that host, so a staged-but-
    unrestarted swap read as fully up to date — the exact false green #2069
    closed for `coord-serve`. `_python_lane_versions`' oldest-wins rule must
    now pick up the stale `coord-agent process` reading instead, so this run
    sets a roll-pending marker rather than reporting `up-to-date`.

    Pins the oldest-wins rule across all three python lanes at once — venv
    and `coord-serve process` already on the target, `coord-agent process`
    (this issue) the lone straggler — the shape the acceptance criteria
    names explicitly."""
    from coord import release_verify as rv

    _stub_verify(
        monkeypatch, daemon_version="0.5.31",
        extra_lanes=[
            rv.Lane(host="server", lane="coord-serve process", version="0.5.31"),
            rv.Lane(host="server", lane="coord-agent process", version="0.5.30"),
        ],
    )
    systemctl_calls = _stub_systemctl(monkeypatch)
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert systemctl_calls == []
    assert prop_calls == []  # marker set, not fired synchronously

    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.5.31"

    record = _records(state_dir)[0]
    # The oldest lane wins — the stale agent process, not the swapped venv.
    assert record["daemon_version"] == "0.5.30"
    assert record["status"] == rw.STATUS_ROLL_PENDING
    assert not escalations


def test_a_marker_pending_for_a_different_target_is_replaced(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """A stale marker (an older release still waiting for its window) must
    not silently block or shadow the newly resolved target.

    #2607: the target/reason move to the newly resolved version, but
    `set_at`/`deferrals` are PRESERVED rather than reset — see
    `test_a_marker_pending_for_a_different_target_preserves_the_escape_hatch`
    below for the regression this guards (PyPI's "latest" climbing on every
    merge meant this replacement fired almost every re-arm, and resetting
    the clock here made the TTL/deferral bound unreachable in practice)."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    dq_cmd.write_roll_pending(_pending(target_version="0.5.29", set_at=1000.0))

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert prop_calls == []  # a different, stale target — never attempted a fire

    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.5.31"
    assert pending.set_at == 1000.0  # #2607: preserved, not reset

    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLL_PENDING
    assert not escalations


def test_a_marker_pending_for_a_different_target_preserves_the_escape_hatch(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """#2607: re-arming for a climbing target must not reset the TTL/
    deferral escape hatch — a marker one tick away from its own bound must
    still expire on schedule even though the target it names moved
    underneath it moments before."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    dq_cmd.write_roll_pending(
        _pending(target_version="0.5.235", set_at=1000.0, deferrals=6, ttl_seconds=3600)
    )

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.236", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output

    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.5.236"
    assert pending.set_at == 1000.0  # original clock survives the re-arm
    assert pending.deferrals == 6  # accumulated count survives too
    assert pending.expired(now=1000.0 + 3600.0)  # still bounded by the ORIGINAL set_at


# ── a marker already pending for the SAME target: one best-effort fire ───


def test_a_marker_pending_for_the_same_target_is_fired_and_cleared_on_success(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    dq_cmd.write_roll_pending(_pending(target_version="0.5.31"))

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert len(prop_calls) == 1
    assert prop_calls[0]["daemon_host"] == "server"
    assert prop_calls[0]["target_version"] == "0.5.31"
    # Never --force from an unattended/semi-attended path (trap 1) — the
    # seam itself (`_run_propagate`) never accepts one, so its absence here
    # is structural, not just an unasserted default.
    assert "force" not in prop_calls[0]

    assert dq_cmd.read_roll_pending() is None  # fired -> cleared
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLLED
    assert not escalations


def test_a_marker_pending_for_the_same_target_discharges_at_its_own_arm_threshold(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2870: the belt-and-braces `coord release propagate` call this
    command makes for an existing marker must be gated at the threshold the
    marker was ARMED at (`RollPending.min_releases_behind`), never at
    whatever `propagation.min_releases_behind` (or this run's OWN
    `--min-behind`) happens to resolve to. Before #2870 this kwarg was never
    passed at all, so a marker armed via `--min-behind 1` against a fleet
    configured `min_releases_behind: 5` could never discharge — every
    belt-and-braces attempt (and every tick-fired `coord-release-window.
    service` re-entry) re-resolved the fleet default and held forever."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    # Armed at threshold 7 (e.g. by a prior `--min-behind 7` run) — a value
    # deliberately unreachable by THIS run's own resolution (no `--min-behind`
    # / no `propagation:` block here, so it defaults to 1), proving the
    # marker's own threshold is what's threaded through, not this run's.
    dq_cmd.write_roll_pending(_pending(target_version="0.5.31", min_releases_behind=7))

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert len(prop_calls) == 1
    assert prop_calls[0]["min_behind"] == 7  # the marker's own arm threshold, not 1


def test_a_marker_with_no_recorded_threshold_falls_back_to_this_runs_own(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """A marker written before #2870 (or whose arming run never evaluated
    the #2583 gate at all) carries no `min_releases_behind` — the discharge
    call falls back to THIS run's own effective threshold, exactly the
    pre-#2870 behaviour, rather than guessing or omitting the flag."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    dq_cmd.write_roll_pending(_pending(target_version="0.5.31", min_releases_behind=None))

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert len(prop_calls) == 1
    # No `--min-behind` and no `propagation:` block on THIS run -> defaults
    # to 1, exactly as `_resolve_min_behind` always has.
    assert prop_calls[0]["min_behind"] == 1


def test_a_marker_pending_for_the_same_target_up_to_date_race_clears_it_too(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """A race: the daemon host became current between this run's own check
    and the fire attempt (e.g. a human rolled it by hand in the meantime).
    Not a defect — `coord release propagate` itself said up-to-date, and the
    marker's job is done either way."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    _stub_propagate(monkeypatch, status=rp.STATUS_UP_TO_DATE, exit_code=0)
    dq_cmd.write_roll_pending(_pending(target_version="0.5.31"))

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert dq_cmd.read_roll_pending() is None
    assert _records(state_dir)[0]["status"] == rw.STATUS_UP_TO_DATE


def test_a_marker_pending_for_the_same_target_still_busy_leaves_it_untouched(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """The fleet is still busy: a completely normal, quiet outcome — the
    marker survives EXACTLY as it was (this attempt spends none of its own
    TTL/deferral bound; only the drive-queue tick's per-tick deferrals do)
    for the tick to keep watching."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    _stub_propagate(monkeypatch, status=rp.STATUS_DEFERRED, exit_code=0,
                    output='{"status": "deferred"}')
    dq_cmd.write_roll_pending(_pending(target_version="0.5.31", deferrals=3))

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.5.31"
    assert pending.deferrals == 3  # untouched by this command
    assert pending.set_at == 1000.0  # untouched — TTL still measures the ORIGINAL set

    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLL_PENDING
    assert record["status"] in rw.OK_STATUSES
    assert not escalations


def test_a_marker_pending_for_the_same_target_a_genuine_failure_is_escalated(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """A real failure, not a mere deferral, IS loud (trap 3) — but the
    marker survives so the tick keeps trying too, bounded by its own
    TTL/deferral ceiling rather than given up on after one bad attempt."""
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    _stub_propagate(monkeypatch, status=rp.STATUS_ROLLED_BACK, exit_code=2,
                    output='{"status": "rolled-back"}')
    dq_cmd.write_roll_pending(_pending(target_version="0.5.31"))

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code == 2, result.output
    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.5.31"
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_PROPAGATE_FAILED
    assert len(escalations) == 1


def test_a_propagate_subprocess_that_cannot_even_run_is_reported_not_a_success(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    dq_cmd.write_roll_pending(_pending(target_version="0.5.31"))

    monkeypatch.setattr(release_cmd, "_run_propagate",
                        lambda **k: ("error: TimeoutError: propagate subprocess timed out",
                                    1, "TimeoutError: propagate subprocess timed out", None))
    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )
    assert result.exit_code != 0
    assert _records(state_dir)[0]["status"] == rw.STATUS_PROPAGATE_FAILED
    assert dq_cmd.read_roll_pending() is not None  # survives for the tick to retry


# ── no resolvable target / no daemon host: setup failures are loud too ───


def test_no_resolvable_target_fails_loudly(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    monkeypatch.setattr(
        release_cmd, "_resolve_expected", lambda *a, **k: (None, "PyPI unreachable")
    )
    result = CliRunner().invoke(
        main, ["release", "nightly-window", "--config", str(valid_config_path)]
    )
    assert result.exit_code == 1
    assert _records(state_dir)[0]["status"] == rw.STATUS_ERROR
    assert len(escalations) == 1
    assert dq_cmd.read_roll_pending() is None


def test_an_unidentifiable_daemon_host_refuses_instead_of_guessing(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    monkeypatch.setattr(release_cmd, "_daemon_machine_name", lambda *a, **k: None)
    _stub_verify(monkeypatch, daemon_version="0.5.30", daemon="server")
    systemctl_calls = _stub_systemctl(monkeypatch)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31"],
    )
    assert result.exit_code == 1, result.output
    assert systemctl_calls == []  # never touched a timer it couldn't safely resume
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ERROR
    assert len(escalations) == 1
    assert dq_cmd.read_roll_pending() is None


# ── --dry-run: the plan, without touching anything ────────────────────────


def test_a_dry_run_prints_the_plan_and_touches_nothing(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    systemctl_calls = _stub_systemctl(monkeypatch)
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert systemctl_calls == []
    assert prop_calls == []
    assert not escalations
    assert _records(state_dir) == []  # dry-run writes nothing
    assert dq_cmd.read_roll_pending() is None  # dry-run sets nothing either


def test_a_dry_run_with_an_existing_marker_describes_the_fire_attempt(
    valid_config_path, state_dir, no_network, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    dq_cmd.write_roll_pending(_pending(target_version="0.5.31"))

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert prop_calls == []
    assert "already pending" in result.output
    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.5.31"  # untouched by --dry-run


# ── #2866: a stale marker must never be proposed as a fire target ────────


def test_a_dry_run_drops_a_marker_the_daemon_already_passed(
    valid_config_path, state_dir, no_network, monkeypatch
):
    """The 2026-08-28 incident, reproduced: a marker set for an OLDER
    version survives (TTL not yet lapsed) after the daemon reached a newer
    version some other way (a human ran `coord release propagate` by hand).
    Firing it would roll the daemon BACKWARDS — this must never be
    proposed, TTL or not."""
    _stub_verify(monkeypatch, daemon_version="0.5.258")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    dq_cmd.write_roll_pending(
        _pending(target_version="0.5.254", set_at=1000.0, reason="propagate")
    )

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.259", "--daemon-host", "server", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert prop_calls == []
    assert "--target 0.5.254" not in result.output
    assert "would replace it with v0.5.259" in result.output
    assert "stale target" in result.output
    # --dry-run still touches no state — the stale marker survives on disk
    # for the NEXT (non-dry-run) invocation to actually replace.
    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.5.254"


def test_a_non_dry_run_replaces_a_marker_the_daemon_already_passed(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """Same incident shape, but for real: the stale/backwards-pointing
    marker must be replaced with the freshly resolved (necessarily-ahead)
    target rather than ever reaching `_run_propagate` with the old one."""
    _stub_verify(monkeypatch, daemon_version="0.5.258")
    prop_calls = _stub_propagate(monkeypatch, status=rp.STATUS_VERIFIED, exit_code=0)
    dq_cmd.write_roll_pending(
        _pending(target_version="0.5.254", set_at=1000.0, reason="propagate")
    )

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.259", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    assert prop_calls == []  # never fired against the stale/backwards target

    pending = dq_cmd.read_roll_pending()
    assert pending is not None
    assert pending.target_version == "0.5.259"  # replaced, not 0.5.254
    assert not escalations
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLL_PENDING


# ── ensure-queue-running: the legacy escape hatch, unchanged by #2587 ────


def test_ensure_queue_running_only_starts_the_timer_and_exits(
    valid_config_path, monkeypatch
):
    """The legacy hand-invocation escape hatch: does ONLY `systemctl --user
    start <timer>`, regardless of anything else — no board read, no version
    resolution, no daemon lookup. #2587 no longer wires this as
    `deploy/coord-release-window.service`'s ExecStopPost= (the timer is
    never stopped in the first place), but the flag itself still works for
    an operator who stopped it by hand."""
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: pytest.fail("--ensure-queue-running must not touch the board"),
    )
    calls = _stub_systemctl(monkeypatch, start_ok=True)
    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--ensure-queue-running",
         "--config", str(valid_config_path)],
    )
    assert result.exit_code == 0, result.output
    assert calls == [("coord-drive-queue.timer", "start")]


def test_ensure_queue_running_reports_failure_honestly(valid_config_path, monkeypatch):
    _stub_systemctl(monkeypatch, start_ok=False)
    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--ensure-queue-running",
         "--config", str(valid_config_path)],
    )
    assert result.exit_code == 1


# ── window-history ─────────────────────────────────────────────────────


def test_window_history_reads_back_what_was_journalled(
    valid_config_path, state_dir, no_network, monkeypatch
):
    _stub_verify(monkeypatch, daemon_version="0.5.30")
    CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.31", "--daemon-host", "server"],
    )

    result = CliRunner().invoke(main, ["release", "window-history"])
    assert result.exit_code == 0, result.output
    assert rw.STATUS_ROLL_PENDING in result.output
    assert "v0.5.31" in result.output


def test_window_history_on_an_empty_journal_says_so(state_dir):
    result = CliRunner().invoke(main, ["release", "window-history"])
    assert result.exit_code == 0
    assert "no nightly-window attempts" in result.output


# ── _drain itself: retired from this command's default path (#2587), but
#    still a callable, directly-tested helper (the bounded loop, with an
#    injected clock) — kept as a manual escape hatch. ─────────────────────


def test_drain_stops_as_soon_as_the_daemon_host_is_free():
    reconcile_calls = []
    boards = iter([
        {"drive_queue": [{"repo_name": "api", "issue_number": 1,
                          "state": "running", "machine": "server"}]},
        {"drive_queue": []},
    ])
    times = iter([0.0, 0.0, 10.0, 10.0])

    outcome = release_cmd._drain(
        daemon_host="server",
        config_path=None,
        deadline=3600.0,
        poll_interval=30.0,
        reconcile=lambda: reconcile_calls.append(1),
        board_fetch=lambda: (next(boards), None),
        now=lambda: next(times),
        sleep=lambda s: None,
    )
    assert outcome.drained is True
    assert outcome.elapsed_seconds == 10.0
    assert len(reconcile_calls) == 2


def test_drain_gives_up_at_the_deadline():
    times = [0.0]

    def _now():
        return times[0]

    def _sleep(s):
        times[0] += s

    outcome = release_cmd._drain(
        daemon_host="server",
        config_path=None,
        deadline=90.0,
        poll_interval=30.0,
        reconcile=lambda: None,
        board_fetch=lambda: (
            {"drive_queue": [{"repo_name": "api", "issue_number": 1,
                              "state": "running", "machine": "server"}]},
            None,
        ),
        now=_now,
        sleep=_sleep,
    )
    assert outcome.drained is False
    assert outcome.elapsed_seconds >= 90.0
    assert "api#1" in outcome.detail


def test_drain_treats_an_unreadable_board_as_fleet_wide_busy():
    """Same rule `coord release propagate` itself applies: a board this run
    cannot read is not proof of anything, least of all that the daemon host
    is free."""
    outcome = release_cmd._drain(
        daemon_host="server",
        config_path=None,
        deadline=10.0,
        poll_interval=5.0,
        reconcile=lambda: None,
        board_fetch=lambda: ({}, "ConnectError: refused"),
        now=(lambda ts=[0.0]: (ts.__setitem__(0, ts[0] + 5.0), ts[0])[1]),
        sleep=lambda s: None,
    )
    assert outcome.drained is False
    assert "board unreadable" in outcome.detail


def test_drain_is_blocked_by_a_paused_daemon_host(valid_config_path, monkeypatch):
    """#2174: `_drain`'s default `extra_busy_fetch` must also see `coord
    pause`/quiet-hours state, not just tmux. A paused daemon host must never
    read as 'drained' just because the board and tmux are both quiet; before
    the fix nothing here ever consulted the pause store at all."""
    from coord import machine_pause as mp
    from coord.config import load as load_config

    config = load_config(str(valid_config_path))
    monkeypatch.setattr(release_cmd, "_interactive_session_busy", lambda config: [])
    mp.local_pause("server")

    outcome = release_cmd._drain(
        daemon_host="server",
        config_path=None,
        config=config,
        deadline=10.0,
        poll_interval=5.0,
        reconcile=lambda: None,
        board_fetch=lambda: ({}, None),
        now=(lambda ts=[0.0]: (ts.__setitem__(0, ts[0] + 5.0), ts[0])[1]),
        sleep=lambda s: None,
    )
    assert outcome.drained is False
    assert "machine paused" in outcome.detail
    assert "server" in outcome.detail


# ── #2187: a VERIFIED, exit-0 propagate must never be reported as
#    `propagate-failed` — the whole bug this issue is about ─────────────────
#
# These exercise `_parse_trailing_json`, `_latest_propagate_record_since` and
# `_run_propagate` itself directly, then drive the full CLI command with a
# faked subprocess boundary (not `_run_propagate` itself) so the fix is
# proven end to end, the same way the real bug reached production. Unaffected
# by #2587 — `_run_propagate`'s own interpretation is reused verbatim by the
# marker-fire path (see the tests above).


def test_parse_trailing_json_reads_a_pretty_printed_indent2_payload():
    """The exact shape `coord release propagate --json` prints
    (`json.dumps(..., indent=2, sort_keys=True)`) — this is the shape the
    old single-line heuristic never matched (#2187's root cause)."""
    import json as _json

    payload = {"status": "verified", "target_version": "0.5.50"}
    stdout = (
        "note: some warning on stdout\n"
        + _json.dumps(payload, indent=2, sort_keys=True)
        + "\n"
    )
    parsed = release_cmd._parse_trailing_json(stdout)
    assert parsed == payload


def test_parse_trailing_json_still_reads_a_compact_single_line_payload():
    import json as _json

    stdout = "some preamble\n" + _json.dumps({"status": "deferred"}, sort_keys=True)
    assert release_cmd._parse_trailing_json(stdout) == {"status": "deferred"}


def test_parse_trailing_json_on_no_json_at_all_is_none():
    assert release_cmd._parse_trailing_json("just some plain log output\nnothing here") is None


def test_parse_trailing_json_on_empty_stdout_is_none():
    assert release_cmd._parse_trailing_json("") is None


def test_latest_propagate_record_since_finds_the_run_just_launched(tmp_path, monkeypatch):
    from coord import release_propagate as rp

    rp.append_record(tmp_path, rp.PropagationRecord(started_at=100.0, status=rp.STATUS_FAILED))
    rp.append_record(tmp_path, rp.PropagationRecord(started_at=200.0, status=rp.STATUS_VERIFIED))
    record = release_cmd._latest_propagate_record_since(tmp_path, 150.0)
    assert record["started_at"] == 200.0
    assert record["status"] == rp.STATUS_VERIFIED


def test_latest_propagate_record_since_ignores_older_runs(tmp_path):
    from coord import release_propagate as rp

    rp.append_record(tmp_path, rp.PropagationRecord(started_at=100.0, status=rp.STATUS_VERIFIED))
    assert release_cmd._latest_propagate_record_since(tmp_path, 150.0) is None


def test_latest_propagate_record_since_on_no_journal_is_none(tmp_path):
    assert release_cmd._latest_propagate_record_since(tmp_path, 0.0) is None


def test_run_propagate_prefers_the_journal_over_stdout(tmp_path):
    """Ground truth (#2187 proposal 1): even if stdout were unparseable, a
    matching journal entry is what decides the status — and it stamps
    `propagate_started_at`, the join key (#2187 proposal 2)."""
    from coord import release_propagate as rp

    def _fake_runner(argv, **kwargs):
        import subprocess as _subprocess

        rp.append_record(
            tmp_path,
            rp.PropagationRecord(started_at=500.0, status=rp.STATUS_VERIFIED,
                                 target_version="0.5.50", finished_at=505.0),
        )
        return _subprocess.CompletedProcess(argv, 0, stdout="not json at all", stderr="")

    status, exit_code, output, started_at = release_cmd._run_propagate(
        daemon_host="dellserver", target_version="0.5.50",
        config_path=tmp_path / "coordinator.yml", state_dir=tmp_path,
        runner=_fake_runner, now_fn=lambda: 499.0,
    )
    assert status == rp.STATUS_VERIFIED
    assert exit_code == 0
    assert started_at == 500.0


def test_run_propagate_falls_back_to_pretty_printed_stdout_when_no_journal_entry(tmp_path):
    """#2187's exact root-cause repro: no journal record can be found (the
    write races or fails), but stdout carries the SAME pretty-printed
    (`indent=2`) `--json` payload the real command emits. The old
    single-line heuristic returned the `f"exit {code}"` placeholder here for
    every successful, exit-0 roll — this must now read `verified` instead."""
    import json as _json
    import subprocess as _subprocess

    def _fake_runner(argv, **kwargs):
        payload = {"status": "verified", "target_version": "0.5.50"}
        stdout = _json.dumps(payload, indent=2, sort_keys=True)
        return _subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    status, exit_code, output, started_at = release_cmd._run_propagate(
        daemon_host="dellserver", target_version="0.5.50",
        config_path=tmp_path / "coordinator.yml", state_dir=tmp_path,
        runner=_fake_runner, now_fn=lambda: 0.0,
    )
    assert status == "verified"
    assert exit_code == 0
    assert started_at is None  # nothing to join to — no journal entry found


def test_run_propagate_with_no_journal_and_no_parseable_stdout_names_the_gap(tmp_path):
    """Neither ground truth is available: falls back to the honest
    `f"exit {code}"` placeholder — the CALLER (below) is responsible for
    turning that into a message that names what's missing, not one that
    misreports it as a real, examined status."""
    import subprocess as _subprocess

    def _fake_runner(argv, **kwargs):
        return _subprocess.CompletedProcess(argv, 0, stdout="garbage, no json", stderr="")

    status, exit_code, output, started_at = release_cmd._run_propagate(
        daemon_host="dellserver", target_version="0.5.50",
        config_path=tmp_path / "coordinator.yml", state_dir=tmp_path,
        runner=_fake_runner, now_fn=lambda: 0.0,
    )
    assert status == "exit 0"
    assert exit_code == 0
    assert started_at is None


def _fake_propagate_subprocess(monkeypatch, state_dir, *, status: str, exit_code: int,
                               target_version: str = "0.5.50", write_journal: bool = True,
                               stderr: str = ""):
    """Stands in for a REAL `python -m coord.cli release propagate --json`
    subprocess: appends the same journal record `_finish` would (#2187's
    ground truth) and returns the SAME pretty-printed (`indent=2`) --json
    stdout shape the real command emits, so the whole `_run_propagate`
    boundary — not just its already-stubbed replacement — is exercised.

    *stderr* stands in for the advisory-finding lines the real command
    writes with `click.echo(..., err=True)` (`release.py` lines 996-1002) —
    #2178 acceptance arm 3 needs those present in `propagate_output` even
    though they never affect *status* or *exit_code*."""
    import json as _json
    import subprocess as _subprocess
    import time as _time

    from coord import release_propagate as rp

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if write_journal:
            # A REAL `started_at` — not a fixed constant — because
            # `_run_propagate` compares this against `time.time()` captured
            # right before launch (`_latest_propagate_record_since`'s
            # `since`); a hardcoded past timestamp would look like an OLDER,
            # unrelated run and be filtered out exactly like a real stale
            # entry would be.
            rp.append_record(
                state_dir,
                rp.PropagationRecord(
                    started_at=_time.time(), target_version=target_version,
                    status=status, finished_at=_time.time(),
                ),
            )
        payload = {"status": status, "target_version": target_version}
        stdout = _json.dumps(payload, indent=2, sort_keys=True)
        return _subprocess.CompletedProcess(argv, exit_code, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(_subprocess, "run", _fake_run)
    return calls


def test_window_end_to_end_a_verified_roll_is_never_reported_as_failed(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2187 acceptance arm 1: a propagate that exits 0 and records
    `verified` produces a clean window-history entry and a clean exit —
    through the REAL `_run_propagate`, not a stub of it. Exercised via the
    #2587 "marker already pending for this target" fire path — the only
    path that ever calls `_run_propagate` at all."""
    _stub_verify(monkeypatch, daemon_version="0.5.49")
    dq_cmd.write_roll_pending(_pending(target_version="0.5.50"))
    _fake_propagate_subprocess(monkeypatch, state_dir, status=rp.STATUS_VERIFIED,
                               exit_code=0, target_version="0.5.50")

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.50", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLLED
    assert record["status"] in rw.OK_STATUSES
    assert record["propagate_status"] == rp.STATUS_VERIFIED
    # The join key (#2187 proposal 2): stamped from the propagation
    # journal's OWN `started_at`, proving `window-history` can now be
    # correlated to `history` for this exact run.
    assert record["propagate_started_at"] is not None
    assert record["propagate_started_at"] > 0
    assert not record["error"]
    assert not escalations
    assert dq_cmd.read_roll_pending() is None


def test_window_end_to_end_a_genuine_failure_is_still_reported_failed(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2187 acceptance arm 2: a propagate that genuinely fails still
    produces `propagate-failed` and a non-zero exit — the fix must not turn
    every outcome green."""
    _stub_verify(monkeypatch, daemon_version="0.5.49")
    dq_cmd.write_roll_pending(_pending(target_version="0.5.50"))
    _fake_propagate_subprocess(monkeypatch, state_dir, status=rp.STATUS_FAILED,
                               exit_code=1, target_version="0.5.50")

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.50", "--daemon-host", "server"],
    )
    assert result.exit_code == 1, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_PROPAGATE_FAILED
    assert record["status"] not in rw.OK_STATUSES
    assert record["propagate_status"] == rp.STATUS_FAILED
    assert len(escalations) == 1
    assert dq_cmd.read_roll_pending() is not None  # survives for the tick to retry


def test_window_end_to_end_an_unconfirmable_exit_0_names_the_missing_evidence(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """No journal entry AND no parseable stdout, despite exit 0 (#2187
    proposal 3): the error must name the specific missing artifacts, never
    read as `status=exit 0, exit=0` with nothing further explained."""
    import subprocess as _subprocess

    _stub_verify(monkeypatch, daemon_version="0.5.49")
    dq_cmd.write_roll_pending(_pending(target_version="0.5.50"))

    def _fake_run(argv, **kwargs):
        return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.50", "--daemon-host", "server"],
    )
    assert result.exit_code != 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_PROPAGATE_FAILED
    assert "status=exit 0, exit=0" not in (record["error"] or "")
    assert "no matching entry" in (record["error"] or "")
    assert "no parseable" in (record["error"] or "")
    assert len(escalations) == 1


def test_window_end_to_end_an_advisory_only_gate_is_still_a_success(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2178 acceptance arm 3: a lane propagation structurally cannot roll
    (`~/.coord-cli-venv`, stale on some OTHER host) makes `coord release
    verify` read CRIT, but `release_propagate.scope_verification` classifies
    that finding as ADVISORY rather than blocking (`release_propagate.py`
    lines 404-455) — so the real subprocess exits 0 with `status=verified`
    regardless. The window must record that as a plain success, not pin the
    fleet's release status to failed for as long as that one lane stays
    stale (#2178's point 2: "advisory lanes must not fail the run").

    The advisory finding itself must not be swallowed either — it travels
    in `propagate_output`, exactly as the real subprocess's own stderr
    would carry it, so a human reading `window-history` sees the lane that
    needs fixing by hand without needing to cross-reference `coord release
    history` separately."""
    advisory_line = (
        "  ~ advisory [crit] elitebook ~/.coord-cli-venv: on 0.5.46, expected "
        "0.5.49 — outside propagation's reach, fix by hand"
    )
    _stub_verify(monkeypatch, daemon_version="0.5.49")
    dq_cmd.write_roll_pending(_pending(target_version="0.5.50"))
    _fake_propagate_subprocess(monkeypatch, state_dir, status=rp.STATUS_VERIFIED,
                               exit_code=0, target_version="0.5.50", stderr=advisory_line)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.50", "--daemon-host", "server"],
    )
    assert result.exit_code == 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_ROLLED
    assert record["status"] in rw.OK_STATUSES
    assert record["propagate_status"] == rp.STATUS_VERIFIED
    assert not record["error"]
    assert advisory_line.strip() in record["propagate_output"]
    assert not escalations


def test_window_never_prints_a_zero_exit_code_next_to_a_failure_assertion(
    valid_config_path, state_dir, no_network, escalations, monkeypatch
):
    """#2178 point 3: whatever the window says about a run, `exit 0` must
    never appear next to language asserting the roll didn't happen — that
    exact contradiction ("did not verify a roll ... exit=0") is what made
    diagnosing #2178 take real time on a verified, successful roll.

    Drives the one arm where a real `exit 0` and a FAILED window status
    legitimately coexist — no journal entry and no parseable stdout, so the
    outcome is genuinely unconfirmable rather than known-bad — and checks
    it two ways: the specific honest wording is present, and (generically,
    scanning every line of output) no line anywhere pairs an exit-0 mention
    with either failure phrase."""
    import subprocess as _subprocess

    _stub_verify(monkeypatch, daemon_version="0.5.49")
    dq_cmd.write_roll_pending(_pending(target_version="0.5.50"))

    def _fake_run(argv, **kwargs):
        return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        main,
        ["release", "nightly-window", "--config", str(valid_config_path),
         "--target", "0.5.50", "--daemon-host", "server"],
    )
    assert result.exit_code != 0, result.output
    record = _records(state_dir)[0]
    assert record["status"] == rw.STATUS_PROPAGATE_FAILED
    error = record["error"] or ""
    # The only sanctioned way for "exit 0" and a failed status to appear
    # together: naming the exact missing evidence, never asserting the roll
    # itself didn't happen.
    assert "outcome could not be confirmed" in error
    assert "did not verify a roll" not in error
    for line in (result.output or "").splitlines():
        if "exit 0" in line or "exit=0" in line:
            assert "did not verify a roll" not in line
            assert "propagate-failed" not in line
