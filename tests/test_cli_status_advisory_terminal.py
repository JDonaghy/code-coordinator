"""#1472: `coord status`'s advisory block ("needs attention — worker exited
cleanly with 0 commits") is sourced from each agent's own completed-
assignment map (`/status`'s `completed` list), which the agent only prunes
by *count* (`_COMPLETED_HISTORY_CAP`), never by GitHub outcome. So once an
advisory entry's issue closes or its branch merges — including the common
case where a human rescues and merges the work by hand — the CLI kept
re-serving the same "The work is UNVERIFIED — review it before testing or
merging." warning forever, even though the work is long since done.

Fix: filter each advisory entry through the shared #522 chokepoint guard
(`github_ops.work_is_terminal`) before rendering, exactly like every other
terminal-state check in this codebase. Fail-open (a lookup failure keeps the
entry visible) and cached per invocation.
"""

from __future__ import annotations

import time as _time

import coord.network as network_mod
from click.testing import CliRunner

from coord import github_ops
from coord.commands.status import status as status_cmd
from coord.network import MachineStatus, StatusResult

# Captured at import time — the real function, immune to the conftest
# autouse `_non_terminal_work` stub which reassigns the module attribute to
# always return False for every other test in the suite.
_REAL_WORK_IS_TERMINAL = github_ops.work_is_terminal


def _advisory_status_payload(
    *, issue_number: int = 1472, branch: str = "issue-1472-fix",
    assignment_id: str = "adv-1",
) -> dict:
    return {
        "active": [],
        "completed": [
            {
                "id": assignment_id,
                "status": "advisory",
                "branch": branch,
                "finished_at": 100.0,
                "zero_commit_reason": "worker exited cleanly but pushed 0 commits",
                "spec": {
                    "repo_name": "api",
                    "issue_number": issue_number,
                    "issue_title": "Some fixed issue",
                },
            }
        ],
    }


def _run_status(valid_config_path, monkeypatch, *, payload: dict) -> str:
    # One online machine ("laptop", per VALID_CONFIG) whose /status reports
    # the advisory entry — exercises the real code path that builds
    # `agent_completed` rather than seeding the board directly.
    def _fake_check_all(machines, timeout=3.0, **kw):
        found = next((m for m in machines if m.name == "laptop"), None)
        assert found is not None
        return [MachineStatus(machine=found, state="online", latency_ms=1.0)]

    monkeypatch.setattr(network_mod, "check_all", _fake_check_all)
    monkeypatch.setattr(
        network_mod, "fetch_status", lambda *a, **k: StatusResult(data=payload)
    )

    runner = CliRunner()
    result = runner.invoke(
        status_cmd,
        ["--config", str(valid_config_path), "--no-reconcile"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_advisory_hidden_when_issue_already_closed(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """The #1472 case: the advisory's issue is closed on GitHub (rescued and
    merged out of band) — the stale "UNVERIFIED" nag must not render."""
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: True)

    output = _run_status(
        valid_config_path, monkeypatch, payload=_advisory_status_payload()
    )

    assert "Advisory" not in output, output
    assert "UNVERIFIED" not in output, output


def test_advisory_shown_when_work_still_live(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """Sanity check: a genuine 0-commit advisory whose issue is still open
    and branch unmerged must keep showing — the fix must not blanket-hide
    real advisories (the autouse fixture already stubs work_is_terminal to
    False; asserted explicitly here for clarity)."""
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: False)

    output = _run_status(
        valid_config_path, monkeypatch, payload=_advisory_status_payload()
    )

    assert "Advisory (needs attention" in output, output
    assert "#1472: Some fixed issue [api]" in output, output


def test_advisory_terminal_check_is_cached_per_invocation(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """#1472: two advisory entries sharing the same (repo, issue, branch) —
    e.g. a rework that retried on the same branch — must cost exactly one
    ``gh`` round-trip, not two. Restores the REAL ``work_is_terminal`` (the
    conftest autouse fixture stubs it to always-False) so the ``cache=``
    plumbing between ``_live_advisory_entries`` and
    ``github_ops.work_is_terminal`` is exercised end to end.
    """
    monkeypatch.setattr(github_ops, "work_is_terminal", _REAL_WORK_IS_TERMINAL)

    calls = []

    def _fake_issue_is_closed(repo, issue_number):
        calls.append((repo, issue_number))
        return False

    monkeypatch.setattr(github_ops, "issue_is_closed", _fake_issue_is_closed)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda *a, **k: False)

    payload = _advisory_status_payload()
    # Duplicate the single advisory entry under a different assignment id,
    # same (repo, issue, branch) — the shape a same-branch rework leaves.
    dup = dict(payload["completed"][0])
    dup["id"] = "adv-2"
    payload["completed"].append(dup)

    output = _run_status(valid_config_path, monkeypatch, payload=payload)

    assert output.count("#1472: Some fixed issue [api]") == 2, output
    assert len(calls) == 1, calls


# ══════════════════════════════════════════════════════════════════════════
# #2595: a host cordoned past its drain deadline with ZERO active work is a
# distinct CRIT state on both `coord status` and `coord doctor` — not the
# ordinary "CORDONED: DRAINING FOR..." label a normal in-progress roll gets.
# Black-box: seed a cordon + an empty board (no active assignments at all,
# the default `coord_db` state) and assert both commands say so.
# ══════════════════════════════════════════════════════════════════════════


def _cordon_server_past_deadline(monkeypatch, tmp_path) -> None:
    """Isolate the cordon store under *tmp_path* (same isolation
    `tests/test_release_cordon_2101.py`'s `tmp_home` fixture uses — the
    store lives at ``$HOME/.coord/paused_machines.json``) and cordon
    ``server`` (from ``VALID_CONFIG``) well past the default 90-minute
    drain deadline."""
    import time as _time

    from coord import machine_pause as mp

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir(exist_ok=True)
    mp.local_set_cordon(
        "server", target_version="0.5.232",
        created_at=_time.time() - 7200, ttl_seconds=3600,
    )


def test_status_shows_a_distinct_crit_for_a_cordoned_idle_host(
    valid_config_path, monkeypatch, coord_db, tmp_path,
) -> None:
    _cordon_server_past_deadline(monkeypatch, tmp_path)

    def _fake_check_all(machines, timeout=3.0, **kw):
        return [MachineStatus(machine=m, state="online", latency_ms=1.0) for m in machines]

    monkeypatch.setattr(network_mod, "check_all", _fake_check_all)
    monkeypatch.setattr(
        network_mod, "fetch_status",
        lambda *a, **k: StatusResult(data={"active": [], "completed": []}),
    )

    runner = CliRunner()
    result = runner.invoke(
        status_cmd,
        ["--config", str(valid_config_path), "--no-reconcile"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    # The ordinary cordon label is still there (still true: it IS cordoned)...
    assert "CORDONED: DRAINING FOR V0.5.232" in result.output
    # ...but a plain "online • idle" cordon label alone is exactly the
    # failure #2595 names — this line is what makes it unmistakable.
    assert "CRIT: server is cordoned and IDLE" in result.output
    assert "coord agent update --machine server" in result.output
    assert "coord release cordon --clear server" in result.output


def test_doctor_reports_crit_for_a_cordoned_idle_host(
    valid_config_path, monkeypatch, coord_db, tmp_path,
) -> None:
    from coord.commands.status import doctor as doctor_cmd

    _cordon_server_past_deadline(monkeypatch, tmp_path)

    def _fake_check_all(machines, timeout=3.0, **kw):
        return [
            MachineStatus(
                machine=m, state="online", latency_ms=1.0,
                health={"version": "0.5.210"},
            )
            for m in machines
        ]

    monkeypatch.setattr(network_mod, "check_all", _fake_check_all)

    runner = CliRunner()
    result = runner.invoke(
        doctor_cmd,
        ["--config", str(valid_config_path), "--no-pypi", "--expected", "0.5.232"],
        catch_exceptions=False,
    )

    assert "release cordons (coord release cordon):" in result.output
    assert "CRIT: server is cordoned and IDLE" in result.output
    assert "22 releases behind" in result.output
    assert result.exit_code == 1, result.output


def test_status_shows_outside_reach_crit_advisories(
    valid_config_path, monkeypatch, coord_db, tmp_path,
) -> None:
    """#2595: a CRIT `gate.advisory` finding (a lane `coord release
    propagate` could not roll at all, e.g. a stale `~/.coord-cli-venv`)
    already carries the exact host/lane/remedy (#2403) — it just never
    reached anywhere but the timer's own stderr. `coord status` is that
    destination."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir(exist_ok=True)

    from coord import release_propagate as rp
    from coord.commands.release import _state_dir

    record = rp.PropagationRecord(
        started_at=1.0,
        target_version="0.5.232",
        gate={
            "severity": "ok",
            "blocking": [],
            "advisory": [
                {
                    "host": "server",
                    "lane": "~/.coord-cli-venv (server)",
                    "severity": "crit",
                    "summary": "on 0.5.210, expected 0.5.232",
                },
            ],
            "unrollable": [],
        },
    )
    rp.append_record(_state_dir(), record)

    def _fake_check_all(machines, timeout=3.0, **kw):
        return [MachineStatus(machine=m, state="online", latency_ms=1.0) for m in machines]

    monkeypatch.setattr(network_mod, "check_all", _fake_check_all)
    monkeypatch.setattr(
        network_mod, "fetch_status",
        lambda *a, **k: StatusResult(data={"active": [], "completed": []}),
    )

    runner = CliRunner()
    result = runner.invoke(
        status_cmd,
        ["--config", str(valid_config_path), "--no-reconcile"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert (
        "CRIT: ~/.coord-cli-venv (server): on 0.5.210, expected 0.5.232 "
        "— outside propagation's reach, fix by hand — run `coord agent "
        "update --machine server` then `coord release cordon --clear "
        "server`" in result.output
    ), result.output
    # Never attributed to the wrong machine.
    assert "laptop" in result.output
    laptop_section = result.output.split("server", 1)[0]
    assert "outside propagation's reach" not in laptop_section


# ══════════════════════════════════════════════════════════════════════════
# #615/#906 thin-client guard for the #2595 cordon check above.
#
# `coord doctor` has no `daemon_reroute_target()` early-return, so it runs
# in-process on a thin client. Deciding "is this cordoned host idle?" off
# `coord.state.build_board()` would read the thin client's EMPTY local DB,
# conclude every configured machine is idle, and print a fabricated CRIT for
# a host that is in fact mid-roll with a running assignment. The read must go
# through `board_service.read_board()`, which GETs the daemon's canonical
# board instead.
# ══════════════════════════════════════════════════════════════════════════


def _thin_client_doctor(monkeypatch, valid_config_path, *, active: list) -> "object":
    """Invoke `coord doctor` as a thin client whose daemon reports *active*
    as the board's active assignments and `server` as cordoned past its
    drain deadline. Every daemon round trip is stubbed; the local DB
    (`coord_db`) stays empty, which is exactly the thin-client shape."""
    from coord import client as cc
    from coord.commands.status import doctor as doctor_cmd
    from coord.models import Board

    monkeypatch.setattr(
        cc, "resolve_board_service",
        lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
    )
    # A thin client refuses to trust a local coordinator.yml (#1080) — hand
    # it the fixture config as if the daemon had served it.
    monkeypatch.setattr(
        cc, "fetch_remote_config", lambda *a, **k: valid_config_path
    )
    # The cordon store also lives on the daemon for a thin client (#1563).
    monkeypatch.setattr(
        cc, "fetch_cordons",
        lambda *a, **k: [
            {
                "machine": "server",
                "target_version": "0.5.232",
                "created_at": _time.time() - 7200,
                "expires_at": _time.time() + 3600,
            }
        ],
    )
    monkeypatch.setattr(
        cc, "fetch_remote_board", lambda *a, **k: Board(active=list(active))
    )

    def _fake_check_all(machines, timeout=3.0, **kw):
        return [
            MachineStatus(
                machine=m, state="online", latency_ms=1.0,
                health={"version": "0.5.210"},
            )
            for m in machines
        ]

    monkeypatch.setattr(network_mod, "check_all", _fake_check_all)

    return CliRunner().invoke(
        doctor_cmd,
        ["--config", str(valid_config_path), "--no-pypi", "--expected", "0.5.232"],
        catch_exceptions=False,
    )


def test_doctor_on_a_thin_client_uses_the_daemon_board_for_idleness(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """The daemon's board says `server` is BUSY, the empty local DB says
    nothing — no fabricated cordoned-and-IDLE CRIT."""
    from coord.models import Assignment

    result = _thin_client_doctor(
        monkeypatch, valid_config_path,
        active=[
            Assignment(
                machine_name="server", repo_name="api", issue_number=42,
                issue_title="Still working", status="running",
            )
        ],
    )

    assert "cordoned and IDLE" not in result.output, result.output
    assert "release cordons (coord release cordon):" not in result.output, result.output


def test_doctor_on_a_thin_client_still_reports_a_genuinely_idle_cordon(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """Same thin client, same stubs — but the daemon's board has no running
    assignment for `server`, so the #2595 CRIT still fires. Without this the
    test above would pass for the wrong reason (check silently disabled)."""
    result = _thin_client_doctor(monkeypatch, valid_config_path, active=[])

    assert "release cordons (coord release cordon):" in result.output, result.output
    assert "CRIT: server is cordoned and IDLE" in result.output, result.output
    assert result.exit_code == 1, result.output
