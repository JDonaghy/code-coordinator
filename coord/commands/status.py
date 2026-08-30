"""`coord status`/`usage`/`show-plan`/`diagnose` — read-only board and
machine reporting. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from coord import __version__, github_ops

from coord.commands._common import _CONFIG_OPTION, _load_config

if TYPE_CHECKING:
    from coord.config import Config


def _cordon_stall_suffix(cordons: dict) -> str:
    """The " (deferred N runs — NOT DRAINING)" tag for a stalled cordon (#2240).

    `coord status` renders "CORDONED: DRAINING FOR V0.5.77" for a fleet that
    is upgrading itself in thirty seconds and for one that has been unable to
    dispatch anything for seventy minutes, identically. That is the whole
    observability half of #2240: "nothing surfaced it either — `coord status`
    shows `CORDONED: DRAINING FOR V0.5.77`, which reads as normal in-progress
    behaviour rather than a 70-minute stall."

    The count lives in the propagation journal, which is a file on whichever
    host runs the propagate timer. A thin client has no such file, so this
    degrades to "" — a missing journal must never turn a normal drain into a
    reported stall, and the surface it is missing from is precisely the one
    that is not running the loop.
    """
    if not cordons:
        return ""
    from coord import release_cordon as rc  # noqa: PLC0415
    from coord import release_propagate as rp  # noqa: PLC0415
    from coord.commands.release import _state_dir  # noqa: PLC0415

    target = next(
        (getattr(c, "target_version", None) for c in cordons.values()
         if getattr(c, "target_version", None)),
        None,
    )
    try:
        pressure = rc.deferral_pressure(
            rp.read_records(_state_dir()), target_version=target
        )
    except Exception:  # noqa: BLE001 — a status render must never fail on this
        return ""
    described = rc.describe_deferral_pressure(pressure)
    return f" ({described})" if described else ""


def _stuck_in_cooldown_hosts() -> dict[str, str | None]:
    """Machine name -> target version, for hosts stuck-in-cooldown (#2490).

    Thin wrapper around :func:`coord.commands.release._stuck_hosts_from_journal`
    — same journal, same "newest record only" reading, reused rather than
    reimplemented. A host in this dict is behind the target version, was
    idle as of the last `coord release propagate` tick, and is NOT cordoned
    (the #2240 cooldown is suppressing it) — which is exactly why it shows
    up everywhere else in this command as a flatly ordinary `online • idle`.
    That silence is the whole finding of #2490: a human noticed only because
    they happened to be watching `coord status` themselves.

    Degrades to ``{}`` on any error (no journal, an unreadable one, a thin
    client) — never load-bearing, same contract as `_cordon_stall_suffix`.
    """
    from coord.commands.release import _stuck_hosts_from_journal  # noqa: PLC0415

    try:
        hosts, target = _stuck_hosts_from_journal()
    except Exception:  # noqa: BLE001 — a status render must never fail on this
        return {}
    return {h: target for h in hosts}


def _outside_reach_crit_advisories() -> list[dict]:
    """CRIT ``gate.advisory`` findings from the newest `coord release
    propagate` run (#2595) — same journal, same degrade-to-nothing contract
    as `_cordon_stall_suffix`/`_stuck_in_cooldown_hosts` above.

    These are findings propagation itself could not roll (an out-of-reach
    lane like ``~/.coord-cli-venv``) and already prints with the exact
    remedy commands — but only to the timer's own stderr. Degrades to `[]`
    on any error (no journal, an unreadable one, a thin client): never
    load-bearing, purely advisory the same way the finding itself is.
    """
    from coord import release_propagate as rp  # noqa: PLC0415
    from coord.commands.release import _state_dir  # noqa: PLC0415

    try:
        return rp.latest_crit_advisories(rp.read_records(_state_dir()))
    except Exception:  # noqa: BLE001 — a status render must never fail on this
        return []


def _cordoned_idle_advisories(
    cordons: dict, *, idle_hosts: set[str], host_versions: dict | None = None,
) -> tuple:
    """Every cordoned host past its drain deadline with zero active work
    (#2595) — the single pure decision `coord status`, `coord doctor` and
    the ``release_cordon_idle`` health check all render off of. See
    :func:`coord.release_cordon.idle_overdue_cordons`.

    Wrapped here only to import ``time`` locally (matches the rest of this
    module's lazy-import convention) and to fail soft: a status/doctor
    render must never break because the cordon store had a bad record.
    """
    import time as _time  # noqa: PLC0415

    from coord.release_cordon import idle_overdue_cordons  # noqa: PLC0415

    try:
        return idle_overdue_cordons(
            cordons, now=_time.time(), idle_hosts=idle_hosts, host_versions=host_versions,
        )
    except Exception:  # noqa: BLE001 — a status render must never fail on this
        return ()


def _live_advisory_entries(
    entries: list[dict],
    cfg: "Config",
    *,
    cache: dict | None = None,
) -> list[dict]:
    """Drop advisory entries (#448) whose work is already terminal on GitHub.

    #1472: an advisory entry is agent-local state served verbatim from the
    worker's own completed-assignment map on every ``/status`` poll — nothing
    tells the agent the issue closed or the branch merged, so a genuinely
    resolved advisory (e.g. rescued, reviewed, and merged by hand) keeps
    showing "The work is UNVERIFIED — review it before testing or merging."
    forever. That trains the operator to skim past the whole advisory block,
    which is exactly where a real 0-commit rescue needs to be noticed.

    Reuses the shared #522 chokepoint guard
    (:func:`coord.github_ops.work_is_terminal` — "issue closed OR PR merged")
    rather than a second ad hoc check. **Fail-open**: an entry whose repo
    isn't in *cfg* (or has no ``github`` slug configured) is kept rather than
    silently dropped — ``work_is_terminal`` itself already fails open on any
    GitHub/CLI error. *cache* defaults to a dict scoped to this call so a
    repeated ``(repo, issue, branch)`` triple across entries costs one ``gh``
    round-trip, not one per entry — this list renders on every ``coord
    status``.
    """
    from coord.models import trust_issue_closed_for  # noqa: PLC0415

    if cache is None:
        cache = {}
    live = []
    for e in entries:
        spec = e.get("spec") or {}
        repo_name = spec.get("repo_name")
        repo_cfg = cfg.repo(repo_name) if repo_name else None
        if repo_cfg is None or not repo_cfg.github:
            live.append(e)
            continue
        # #2639: trust_issue_closed_for(spec type) — a test-author/mock-author
        # advisory's issue_number is the milestone's tracking issue, not its
        # own deliverable, so a closed tracking epic must not read as "this
        # advisory's work is terminal" (it would silently drop a genuinely
        # unresolved advisory from view).
        if github_ops.work_is_terminal(
            repo_cfg.github,
            spec.get("issue_number"),
            e.get("branch"),
            cache=cache,
            trust_issue_closed=trust_issue_closed_for(spec.get("type", "work")),
        ):
            continue
        live.append(e)
    return live


@click.command(help="Show all machines, assignments, and connectivity.")
@_CONFIG_OPTION
@click.option("--machine", "machine_filter", default=None, help="Only show this machine.")
@click.option("--timeout", default=3.0, show_default=True, type=float, help="Per-machine health-check timeout (seconds).")
@click.option("--no-reconcile", is_flag=True, help="Skip auto-reconciliation of the board with live agent state.")
@click.option(
    "--freshness",
    is_flag=True,
    help="Also report per-machine repo freshness vs GitHub HEADs.",
)


def status(config_path: Path, machine_filter: str | None, no_reconcile: bool, timeout: float, freshness: bool) -> None:
    from coord import freshness as fresh
    from coord.deps import blocked_repos as compute_blocked, build_dep_graph
    from coord.board_service import read_board, write_board
    from coord.client import resolve_board_service
    from coord.network import check_all, fetch_repos, fetch_status
    from coord.state import load_dispatched, load_notified

    # #584/#1080: when a board service is configured, read the board + config
    # from the daemon instead of local SQLite. _load_config() itself now always
    # fetches the daemon's config on a thin client (never trusts a local file
    # that happens to exist — the config-fetch pre-step that used to live here
    # was a redundant duplicate of that same buggy "local file exists" check,
    # removed in #1080). `svc` below is still needed to gate the local-only
    # reads (queue/notified/session) further down. Unset ⇒ unchanged local
    # behaviour.
    svc = resolve_board_service()
    cfg = _load_config(config_path)

    # Dependency graph (only when --machine isn't narrowing the view).
    if not machine_filter:
        graph = build_dep_graph(cfg.repos)
        if any(deps for deps in graph.values()):
            click.echo("Dependency graph:")
            for repo in cfg.repos:
                deps = graph.get(repo.name, [])
                if deps:
                    click.echo(f"  {repo.name} → {', '.join(deps)}")
                else:
                    click.echo(f"  {repo.name} (no dependencies)")
            click.echo()

    machines = cfg.machines
    if machine_filter:
        machines = [m for m in machines if m.name == machine_filter]
        if not machines:
            click.echo(
                f"error: machine {machine_filter!r} not in coordinator.yml "
                f"(have: {[m.name for m in cfg.machines]})",
                err=True,
            )
            sys.exit(2)

    # #1563: paused_set() is daemon-aware — on a thin client it fetches the
    # daemon's own `/pause` copy, so this renders the state that actually
    # governs dispatch instead of a host-local file the daemon never reads.
    # #1862: passing `cfg.machines` folds quiet-hours windows into that same
    # set (a no-op on any machine with no `quiet_hours:` block).
    # #2101: a release cordon is IN `paused` (that is how every dispatcher
    # honours it), so without the cordon map alongside it this line would
    # render "PAUSED" for a machine nobody paused and no `coord unpause` will
    # free — work stopping with no stated reason, which is the exact failure
    # the cordon mechanism is supposed to stop repeating.
    # #2146: an operator-SET quiet-hours window lives in the daemon's state
    # file, not in coordinator.yml, so a thin client resolving it locally
    # would see the machine in `paused` with no local explanation and print
    # the flatly wrong "PAUSED". Fetch the effective window map (daemon-aware,
    # fail-soft) and hand it to describe_pause_state, which also names where
    # each window came from — "set here" vs "from coordinator.yml".
    from coord.machine_pause import (  # noqa: PLC0415
        cordons as fetch_cordons,
        describe_pause_state,
        effective_quiet_hours,
        paused_set,
    )
    paused = paused_set(cfg.machines)
    cordons = fetch_cordons()
    # #2240: how many consecutive propagate runs this cordon has now failed to
    # produce a window for. Computed once, appended to every cordoned
    # machine's label below — "draining" and "deadlocked" must not render the
    # same, which is why the 70-minute stall was invisible.
    cordon_stall = _cordon_stall_suffix(cordons)
    quiet_windows = effective_quiet_hours(cfg.machines)
    # #2490: hosts the last propagate run flagged as behind + idle + stuck
    # behind an active #2240 cooldown — see `_stuck_in_cooldown_hosts`.
    stuck_in_cooldown = _stuck_in_cooldown_hosts()
    # #2595: CRIT advisories (lanes propagation could not reach at all) from
    # the newest `coord release propagate` run — see
    # `_outside_reach_crit_advisories`.
    outside_reach = _outside_reach_crit_advisories()

    statuses = check_all(machines, timeout=timeout)
    agent_completed: dict[str, dict] = {}
    click.echo("Machines:")
    for s in statuses:
        m = s.machine
        latency = f" ({s.latency_ms:.0f}ms)" if s.latency_ms is not None else ""
        if s.is_online:
            status_result = fetch_status(m, timeout=timeout)
            if status_result.ok:
                active = (status_result.data or {}).get("active", [])
                if active:
                    a = active[0]
                    spec = a.get("spec", {})
                    spec_type = spec.get("type", "work")
                    badge_map = {"review": "[review] ", "smoke": "[smoke] ", "plan": "[plan] "}
                    badge = badge_map.get(spec_type, "")
                    target = spec.get("review_target")
                    if spec_type == "review" and target:
                        target_str = f" reviewing PR #{target}"
                    elif spec_type == "smoke" and target:
                        target_str = f" smoking branch `{target}`"
                    else:
                        target_str = ""
                    # #1707: the wire payload only carries `provider` when
                    # the resolved name differs from the implicit "claude"
                    # default (coord/dispatch.py's dispatch()), so this is
                    # absent for the common case and present exactly when a
                    # mixed fleet needs it surfaced.
                    provider_val = spec.get("provider")
                    provider_str = f" (provider={provider_val})" if provider_val else ""
                    detail = (
                        f"busy — {badge}#{spec.get('issue_number', '?')}: "
                        f"{spec.get('issue_title', '?')}{target_str}{provider_str}"
                    )
                else:
                    detail = "idle"
            else:
                active = []
                detail = f"status unavailable ({status_result.error})"
            if status_result.ok and status_result.data:
                for entry in status_result.data.get("completed", []):
                    eid = entry.get("id") or entry.get("assignment_id")
                    if eid:
                        agent_completed[eid] = entry
            label = f"{s.state} • {detail}{latency}"
        else:
            status_result = None
            label = f"{s.state} — {s.reason}{latency}"

        # #1563: surface pause state the same way regardless of whether
        # `paused_set()` resolved it locally or from the daemon — a thin
        # client that just ran `coord pause` needs to SEE that it took,
        # otherwise a pause that silently didn't reach the daemon looks
        # identical to one that did (the whole bug this closes).
        # #1862: a quiet-hours pause must not look identical to a hand
        # pause — an operator debugging a stalled queue at 1AM needs to
        # know whether the machine will wake itself up or is waiting on
        # `coord unpause`.
        pause_state = describe_pause_state(
            m, paused, cordons=cordons, quiet_hours=quiet_windows
        )
        if pause_state is not None and pause_state.kind == "cordon":
            # #2101 trap E: name the version it is draining for, so a stopped
            # machine reads as "the fleet is upgrading itself" rather than as
            # a mystery.
            # #2240: ...and say when it has stopped being that, so it does
            # not read as normal in-progress behaviour for an hour.
            label = f"{pause_state.detail.upper()}{cordon_stall} — {label}"
        elif pause_state is not None and pause_state.kind == "hand":
            label = f"PAUSED — {label}"
        elif pause_state is not None and pause_state.kind == "quiet":
            label = f"QUIET ({pause_state.detail}) — {label}"
        elif pause_state is not None and pause_state.kind == "quiet_overridden":
            label = f"{label}  [quiet hours overridden]"

        # Extract agent version from /status response (added in #104).
        agent_version: str | None = None
        if status_result and status_result.ok and status_result.data:
            agent_version = status_result.data.get("version")

        repos = ", ".join(m.repos) if m.repos else "(none)"
        click.echo(f"  {m.name:15s} [{label}]")
        version_line = ""
        if agent_version:
            if agent_version != __version__:
                version_line = f"  agent-version: {agent_version} ⚠ (coord is {__version__})"
            else:
                version_line = f"  agent-version: {agent_version}"
        click.echo(f"    host: {m.host}  repos: {repos}{version_line}")

        # #1886 Path B: `/health` exposes `installed_version` (a disk read
        # that advances the instant `pip` writes to site-packages)
        # separately from `version` (the running process's loaded module,
        # fixed at import time — never advances without a restart). A
        # process that never restarted after `coord agent update` — the
        # execv-under-systemd stall, #404 — is otherwise invisible: `pip
        # show` and this agent's own `version` field both "agree" with
        # whatever was installed last, even though the code actually
        # executing hasn't changed. Surfacing the drift here means it's
        # visible on every `coord status`, not only when an update happens
        # to be running.
        if s.is_online and s.health:
            installed_version = s.health.get("installed_version")
            running_version = s.health.get("version")
            if (
                installed_version
                and running_version
                and installed_version != running_version
            ):
                click.echo(
                    f"    ⚠ running v{running_version} but installed "
                    f"v{installed_version} — process hasn't restarted since "
                    "its last update (`systemctl --user restart coord-agent` "
                    "on that machine, or `coord agent update` to retry)"
                )

        # #1527: `/health`'s `degraded` dict names any configured repo whose
        # `repo_path` is missing/unconfigured on this machine — the machine
        # can still be green/idle above while every dispatch for that one
        # repo silently 400s. Surface it here instead of leaving it only
        # discoverable by sshing in and reading the filesystem.
        degraded = (s.health or {}).get("degraded") if s.is_online else None
        if degraded:
            for repo_name, reason in degraded.items():
                click.echo(f"    ⚠ degraded: {repo_name} — {reason}")

        # #2490: this host is behind the release, was idle last tick, and
        # nothing is cordoning it because a #2240 deadlock-release cooldown
        # is suppressing all new cordons fleet-wide. Without this line the
        # machine above reads as a plain `online • idle` — indistinguishable
        # from a machine with nothing to do, which is exactly how the
        # 2026-08-20 incident went unnoticed for the length of the cooldown.
        if m.name in stuck_in_cooldown:
            stuck_target = stuck_in_cooldown[m.name]
            stuck_version = f"v{stuck_target}" if stuck_target else "the release"
            click.echo(
                f"    ⚠ STUCK: behind {stuck_version} and idle, but cordoning "
                "is suppressed by an active #2240 deadlock-release cooldown "
                "(#2490) — no automatic path back to rolling until it lifts; "
                f"consider `coord agent update --machine {m.name}`"
            )

        # #2595: a cordoned host with ZERO active work and past its drain
        # deadline is not "draining" — it is a whole machine pulled from the
        # fleet with nothing left to bring it back on its own. Distinct from
        # the #2240 `cordon_stall` suffix folded into the label above (which
        # flags a cordon that keeps failing to produce a *window*): this
        # fires purely off the drain deadline and the live `/status` active
        # list, whether or not #2240's own deferral bookkeeping ever saw it.
        if status_result is not None and status_result.ok and not active and m.name in cordons:
            for overdue in _cordoned_idle_advisories(
                {m.name: cordons[m.name]},
                idle_hosts={m.name},
                host_versions={m.name: agent_version},
            ):
                click.echo(f"    ✗ CRIT: {overdue.message}")

        # #2595: CRIT advisories from the newest `coord release propagate`
        # run naming THIS host — a lane propagation could not reach at all
        # (e.g. a stale `~/.coord-cli-venv`) and already prints with its
        # remedy commands, but only to the timer's own stderr.
        for finding in outside_reach:
            if finding.get("host") != m.name:
                continue
            click.echo(
                f"    ✗ CRIT: {finding.get('lane')}: {finding.get('summary')} "
                "— outside propagation's reach, fix by hand — run `coord "
                f"agent update --machine {m.name}` then `coord release "
                f"cordon --clear {m.name}`"
            )

        if status_result and status_result.ok and status_result.data:
            for entry in status_result.data.get("active", []):
                progress = entry.get("progress")
                if not progress:
                    continue
                if progress.get("stuck"):
                    click.echo(f"    !! STUCK: {progress['stuck']}")
                for w in progress.get("warnings", []):
                    click.echo(f"    !! {w}")
                updates = progress.get("updates", [])
                if updates:
                    click.echo(f"    latest: {updates[-1]}")

    # Reconcile board with live agent data
    #
    # #1631 (H-4): the fleet-health footer needs the SAME `fleet_health`
    # block a thin client's `/board` GET already carries — fetching it a
    # second time would double the board round-trip (this repo has hit
    # multi-MB /board payloads before; see fleet_snapshot.py's own budget
    # comment), so on a thin client this replaces the plain `read_board()`
    # call with the raw-payload equivalent and pulls `fleet_health` off the
    # SAME response instead of issuing a second GET. Host mode (no
    # board_service) has no such payload to share, so it falls back to
    # reassembling an equivalent block from the local DB.
    if svc is not None:
        from coord.client import board_from_payload, fetch_board_payload

        board_payload = fetch_board_payload(svc)
        board = board_from_payload(board_payload)
        fleet_health_block = board_payload.get("fleet_health")
    else:
        from coord.health.aggregate import local_fleet_health_block

        board = read_board()
        fleet_health_block = local_fleet_health_block([m.name for m in cfg.machines])
    if not no_reconcile and agent_completed:
        # #749: write_board() routes to the daemon's /board upsert when a
        # board service is configured, so a thin client's reconciliation now
        # actually lands on the shared DB instead of being skipped entirely.
        reconciled = 0
        for a in board.active[:]:
            if a.assignment_id is None:
                continue
            entry = agent_completed.get(a.assignment_id)
            if entry is None:
                continue
            branch = entry.get("branch")
            agent_status = entry.get("status")
            if agent_status == "done":
                done = board.mark_done_by_id(
                    a.assignment_id,
                    finished_at=entry.get("finished_at"),
                    branch=branch,
                )
                # #1566: mirror reconcile.py — a review agent reporting
                # "done" has only finished the LLM session; the verdict is
                # parsed + persisted by `coord notify`, a separate, slower
                # step. Leaving status="done" here would show a finished
                # review with no verdict, indistinguishable from a dropped
                # one. "finalizing" isn't in drive_state.TERMINAL_STATUSES,
                # so `coord drive` still correctly waits on it.
                if done is not None and done.type == "review":
                    done.status = "finalizing"
            elif agent_status == "advisory":
                # #448: 0-commit clean exit — treat as done on the board so
                # the assignment doesn't block; the advisory section below
                # flags it for human attention.  Mirror reconcile.py: set
                # status="advisory" (mark_done_by_id leaves it as "done")
                # and review_state="advisory" on work assignments so that
                # the review-dispatch loop in coord notify skips them.
                done = board.mark_done_by_id(
                    a.assignment_id,
                    finished_at=entry.get("finished_at"),
                    branch=branch,
                )
                if done is not None:
                    done.status = "advisory"
                    if done.type == "work":
                        done.review_state = "advisory"
            elif agent_status == "refused_policy":
                # #2234: worker exited cleanly, pushed 0 commits, and its own
                # final message cited a standing repo-rule prohibition (the
                # #2195 shape). Mirror reconcile.py's `reconcile()`: preserve
                # the distinct status rather than folding it into "advisory"
                # — without this branch it falls to the `else` below and gets
                # recorded FAILED, feeding `auto_reassign` on a condition
                # retrying can never fix.
                done = board.mark_done_by_id(
                    a.assignment_id,
                    finished_at=entry.get("finished_at"),
                    branch=branch,
                )
                if done is not None:
                    done.status = "refused_policy"
                    if done.type == "work":
                        done.review_state = "advisory"
            else:
                board.mark_failed_by_id(
                    a.assignment_id,
                    finished_at=entry.get("finished_at"),
                )
            # #1461: stamp a usage-limit-kill diagnostic (if the agent
            # flagged one — AgentServer._reap, agent.py) onto the persisted
            # `failure_reason` column. Routed through the already-daemon-aware
            # `set_assignment_failure_reason` (#618) rather than a raw
            # get_connection() write — `coord status` is thin-client
            # reachable, and a raw local write would silently land on the
            # thin client's empty local DB instead of the daemon's (the same
            # #906 audit gap `get_issue_test_mode` was fixed for). This also
            # normalises status to 'failed' even when the branch above set
            # 'advisory' — a usage-limit kill is the one terminal state known
            # safe to re-dispatch unchanged (drive.py's FAILED bucket),
            # mirrors coord.reconcile._record_usage_limit_reason exactly.
            usage_limit_reason = entry.get("usage_limit_reason")
            if usage_limit_reason and a.assignment_id:
                try:
                    from coord.state import set_assignment_failure_reason

                    set_assignment_failure_reason(a.assignment_id, usage_limit_reason)
                except Exception:  # noqa: BLE001 — diagnostic only
                    pass
            reconciled += 1
        if reconciled:
            write_board(board)
            click.echo(f"\n  (reconciled {reconciled} assignment(s) from live agent data)")

    # #1461: surface usage-limit kills as a distinct fleet-level condition —
    # a known-safe-to-retry-once-reset wait, not a defect. Shown ahead of (and
    # excluded from) the Advisory/plain-failure buckets below so a confusing
    # evening reads as "3 killed by the usage limit, resets 8:30pm" instead of
    # N unrelated-looking advisory/failed rows.
    usage_limit_entries = [
        e for e in agent_completed.values()
        if e.get("usage_limit_reason")
    ]
    if usage_limit_entries:
        click.echo("")
        click.echo(
            "⏳ Usage limit (worker killed by the account's usage limit — "
            "safe to retry unchanged once reset):"
        )
        for e in usage_limit_entries:
            spec = e.get("spec", {})
            click.echo(
                f"  #{spec.get('issue_number', '?')}: "
                f"{spec.get('issue_title', '?')} "
                f"[{spec.get('repo_name', '?')}]  — {e['usage_limit_reason']}"
            )

    # #448: surface advisory assignments (0 commits, clean exit) so the
    # operator knows they need attention without having to dig into logs.
    # Usage-limit kills are excluded — surfaced separately above, since they
    # are a wait condition rather than something needing human attention.
    #
    # #1472: an advisory entry is agent-local state — it lives in the
    # worker's own completed-assignment map (`_COMPLETED_HISTORY_CAP` prunes
    # it by *count*, not by GitHub outcome) — so it keeps being re-served
    # here forever, even after the issue closes or the branch merges out
    # from under it. Re-check terminal state on every render rather than
    # trusting the agent to have cleared it.
    #
    # Both filters apply: #1461 drops usage-limit kills (a wait condition,
    # shown above) and #1472 drops work that has since gone terminal.
    advisory_entries = _live_advisory_entries(
        [
            e for e in agent_completed.values()
            if e.get("status") == "advisory" and not e.get("usage_limit_reason")
        ],
        cfg,
    )
    if advisory_entries:
        click.echo("")
        click.echo("⚠ Advisory (needs attention — worker exited cleanly with 0 commits):")
        for e in advisory_entries:
            spec = e.get("spec", {})
            reason = e.get("zero_commit_reason") or "0 commits pushed"
            click.echo(
                f"  #{spec.get('issue_number', '?')}: "
                f"{spec.get('issue_title', '?')} "
                f"[{spec.get('repo_name', '?')}]  — {reason}"
            )

    # #2234: surface a policy refusal in its OWN section rather than folding
    # it into Advisory above — same #1472 "still live on GitHub" filter, but
    # distinct wording: this is a routing decision for the coordinator, not
    # a "did the worker get stuck" question.
    policy_refusal_entries = _live_advisory_entries(
        [
            e for e in agent_completed.values()
            if e.get("status") == "refused_policy" and not e.get("usage_limit_reason")
        ],
        cfg,
    )
    if policy_refusal_entries:
        click.echo("")
        click.echo(
            "🚫 Needs the coordinator (worker refused on a standing repo-rule "
            "prohibition):"
        )
        for e in policy_refusal_entries:
            spec = e.get("spec", {})
            reason = e.get("policy_refusal_reason") or "cited a repo-rule prohibition"
            click.echo(
                f"  #{spec.get('issue_number', '?')}: "
                f"{spec.get('issue_title', '?')} "
                f"[{spec.get('repo_name', '?')}]  — {reason}"
            )

    blocked = compute_blocked(cfg.repos, board.active)
    if blocked:
        click.echo("")
        click.echo("Blocked repos:")
        for repo_name, reasons in blocked.items():
            click.echo(f"  {repo_name}:")
            for reason in reasons:
                click.echo(f"    - {reason}")

    if freshness:
        click.echo("")
        click.echo("Repo freshness:")
        github_heads: dict[str, str | None] = {}
        for repo_cfg in cfg.repos:
            try:
                github_heads[repo_cfg.name] = github_ops.get_default_branch_head(
                    repo_cfg.github, repo_cfg.default_branch
                )
            except RuntimeError as e:
                github_heads[repo_cfg.name] = None
                click.echo(f"  (github HEAD lookup failed for {repo_cfg.name}: {e})", err=True)
        for s in statuses:
            if not s.is_online:
                click.echo(f"  {s.machine.name}: (offline, skipping)")
                continue
            agent_repos = fetch_repos(s.machine, timeout=timeout) or {}
            click.echo(f"  {s.machine.name}:")
            for repo_name in s.machine.repos:
                rf = fresh.compare(repo_name, agent_repos.get(repo_name), github_heads.get(repo_name))
                local = (rf.local_sha or "?")[:7]
                remote = (rf.remote_sha or "?")[:7]
                tag = f"[{rf.state}]"
                detail = f"local {local} remote {remote}"
                if rf.dirty:
                    detail += " (dirty)"
                if rf.error:
                    detail += f" — {rf.error}"
                click.echo(f"    {repo_name:20s} {tag:10s} {detail}")

    # Merge queue
    from coord import merge_queue as mq

    # #584: merge_queue lives in the (host-local) DB; skip it for a thin client.
    queue = [] if svc else mq.load_queue()
    by_repo = mq.pending_summary(queue) if queue else {}
    if by_repo:
        click.echo("")
        click.echo("Merge queue:")
        for repo_name, entries in sorted(by_repo.items()):
            click.echo(f"  {repo_name}:")
            for e in entries:
                size = f"+{e.size}" if e.size is not None else "?"
                pr = f"PR #{e.pr_number}" if e.pr_number else "no PR yet"
                tag = f"[{e.state}]"
                line = f"    {tag:11s} #{e.issue_number} ({e.branch} → {e.target_branch}) {pr} size={size}"
                click.echo(line)
                # #420: recompute the review/smoke gate error live rather than
                # echoing the stored string verbatim — it's only refreshed on
                # a real merge attempt, so an approval/verdict that landed
                # since then would otherwise show as stale as "blocked".
                live_error = mq.display_error(e, board, cfg)
                if live_error:
                    click.echo(f"      error: {live_error}")

    # #2607: the #2587 roll-pending marker — while it is live the drive-queue
    # tick refuses to launch anything, and #2607 found a re-arm path that
    # could reset its own TTL/deferral escape hatch on every attempt, freezing
    # the queue with no operator-visible sign anywhere OTHER than `coord
    # drive-queue status`. Surfaced here too so an operator staring at plain
    # `coord status` — the first thing anyone reaches for — sees it without
    # having to know the marker exists. Host-local (same file the daemon
    # host's own `coord-drive-queue.timer` reads), so skipped on a thin
    # client exactly like the merge queue above.
    if not svc:
        import time as _time

        from coord.commands.drive_queue import read_roll_pending

        roll_pending = read_roll_pending()
        if roll_pending is not None:
            age = _time.time() - roll_pending.set_at
            click.echo("")
            click.echo(
                f"⏸ {roll_pending.describe()} — set {age:.0f}s ago, "
                f"{roll_pending.deferrals}/{roll_pending.max_deferrals} "
                "deferrals — drive queue launches nothing until it fires or "
                "expires. Cancel with `coord drive-queue cancel-roll`."
            )

    # #920: sibling-overlap warnings — approved (PENDING) queue entries that
    # touch overlapping files and have been aging.  Mirrors the merge-queue
    # skip above: the queue is host-local, so this is a no-op on a thin
    # client (use `coord merge --plan`, which fetches the daemon-computed
    # equivalent via /board, from a thin client instead).
    if not svc:
        from coord.commands.merge import _print_sibling_overlap_warnings

        overlaps = mq.find_sibling_overlaps(board, cfg)
        _print_sibling_overlap_warnings(overlaps)

    # Auto-loop iteration-cap blockers: assignments where the review→fix loop
    # exhausted all allowed iterations without receiving an approval.  These
    # require manual intervention (bump pipeline.max_review_iterations or
    # dispatch a fix with `coord assign`) and are shown prominently so the
    # operator notices them on the first `coord status` after the cap fires.
    cap_hit_blocked = [
        a for a in board.completed
        if a.type == "work" and a.review_state == "cap_hit"
    ]
    if cap_hit_blocked:
        click.echo("")
        click.echo("⚠ Auto-loop blockers (manual action required):")
        for a in cap_hit_blocked:
            click.echo(
                f"  #{a.issue_number}: {a.issue_title} ({a.repo_name})"
                f"  [iteration cap hit]"
            )
            click.echo(
                f"    Options: bump pipeline.max_review_iterations in coordinator.yml"
                f" or 'coord assign' to dispatch a fix manually,"
                f" or 'coord merge --force-merge' to merge as-is."
            )

    # #586: branch-not-on-remote blockers — work that completed but the branch
    # was never pushed.  Downstream review/fix dispatch is blocked until the
    # operator pushes from the original worker machine.
    branch_not_pushed = [
        a for a in board.completed
        if a.type == "work" and a.review_state == "branch_not_on_remote"
    ]
    if branch_not_pushed:
        click.echo("")
        click.echo("⚠ Push required (review blocked — branch not on remote):")
        for a in branch_not_pushed:
            click.echo(
                f"  #{a.issue_number}: {a.issue_title} ({a.repo_name})"
                f"  [branch not on remote]"
            )
            click.echo(
                f"    Branch '{a.branch}' exists only on {a.machine_name}."
                f" Push it with: ssh {a.machine_name} 'cd <repo-path> && git push origin {a.branch}'"
                f" then re-run 'coord notify' to retry review dispatch."
            )

    # #904: no-eligible-reviewer blockers — every configured candidate machine
    # definitively rejected the review dispatch (drifted coordinator.yml vs.
    # an agent's actual `/health` repos list, most commonly). Mirrors the
    # branch-not-on-remote block above so this stall is operator-visible
    # instead of only a log.error() line.
    no_eligible_reviewer = [
        a for a in board.completed
        if a.type == "work" and a.review_state == "no_eligible_reviewer"
    ]
    if no_eligible_reviewer:
        click.echo("")
        click.echo("⚠ No reviewer available (review blocked — all candidates rejected):")
        for a in no_eligible_reviewer:
            click.echo(
                f"  #{a.issue_number}: {a.issue_title} ({a.repo_name})"
                f"  [no eligible reviewer]"
            )
            click.echo(
                "    Every configured machine for this repo rejected the dispatch."
                " Check that each agent's /health 'repos' list matches coordinator.yml,"
                " then re-run 'coord notify' to retry review dispatch."
            )

    # Show completed work assignments with review lifecycle state.
    _REVIEW_STATE_TAGS = {
        "pending": "[awaiting review]",
        "dispatched": "[review dispatched]",
        "done": "[review done]",
        "cap_hit": "[⚠ iteration cap hit — manual action required]",
        "branch_not_on_remote": "[⚠ branch not on remote — push required]",
        "no_eligible_reviewer": "[⚠ no reviewer available — check agent /health vs coordinator.yml]",
        # #1534: the branch carries no commits over its base, so there is
        # nothing to review. Almost always means the work assignment produced
        # nothing (e.g. a usage-limit kill) and should be re-dispatched.
        "zero_commits": "[⚠ branch has 0 commits — nothing to review, re-dispatch the work]",
    }
    work_completed = [a for a in board.completed if a.type == "work"]
    if work_completed:
        by_time = sorted(work_completed, key=lambda a: a.finished_at or 0, reverse=True)[:10]
        click.echo("")
        click.echo("Completed work assignments:")
        for a in by_time:
            # test_state="failed" takes priority: the review gate is correctly
            # held but "[awaiting review]" would mislead the operator into
            # thinking the item is queued to move forward (real incident: #1116).
            #
            # #2579: test_state="contested" (coord.notify.TEST_STATE_CONTESTED)
            # gets the same priority-override treatment, but its own distinct
            # tag — this is the #1116 failure mode reintroduced for a row whose
            # review already reached "done": without this branch a contested
            # row would show "[review done]", which reads as fine when a
            # confirmation just refuted the pass claim it was approved on.
            if getattr(a, "test_state", None) == "contested":
                rs_tag = "[⚠ CONTESTED — review approved, but a re-run refuted the pass; needs a human]"
            elif getattr(a, "test_state", None) == "failed":
                rs_tag = "[✗ test FAILED — needs fix]"
            else:
                rs_tag = _REVIEW_STATE_TAGS.get(a.review_state or "", "")
            rs_suffix = f"  {rs_tag}" if rs_tag else ""
            # #1707: surface the resolved provider (already persisted at
            # dispatch time, coord/models.py Assignment.provider_name) so a
            # mixed claude/opencode fleet is legible here — not just in
            # `coord gates`. Omit the plain "claude" case (the overwhelming
            # majority of rows) so the common path stays uncluttered; only a
            # non-default backend earns the tag.
            provider_tag = (
                f"  [provider={a.provider_name}]"
                if a.provider_name and a.provider_name != "claude"
                else ""
            )
            click.echo(
                f"  #{a.issue_number}: {a.issue_title} ({a.repo_name})"
                f"{provider_tag}{rs_suffix}"
            )

    notified = {} if svc else load_notified()
    if notified:
        dispatched_by_id = {r["assignment_id"]: r for r in load_dispatched()}
        items = sorted(notified.items(), key=lambda kv: kv[1].get("posted_at", 0), reverse=True)[:5]
        click.echo("")
        click.echo("Recent issue comment activity:")
        for aid, info in items:
            record = dispatched_by_id.get(aid, {})
            repo = record.get("repo_github", "?")
            issue = record.get("issue_number", "?")
            click.echo(f"  [{info['event']}] {repo}#{issue} (assignment {aid})")

    # Burn-rate warning: show a one-liner when spend rate is high.
    try:
        from coord.state import load_session
        from coord.usage import build_session_usage, format_burn_rate_line
        import datetime

        sess = None if svc else load_session()
        started_at: float | None = None
        if sess and sess.get("started_at"):
            try:
                dt = datetime.datetime.fromisoformat(
                    sess["started_at"].rstrip("Z").replace("Z", "+00:00")
                )
                started_at = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
            except (ValueError, AttributeError):
                pass

        all_assignments = list(board.active) + list(board.completed)
        session_usage = build_session_usage(all_assignments, started_at=started_at)
        burn_line = format_burn_rate_line(session_usage)
        if burn_line:
            click.echo("")
            click.echo(burn_line)
    except (ImportError, OSError, ValueError, KeyError):
        pass  # Never let usage tracking break the status command.

    # #1631 (H-4): the always-visible fleet-health footer. Printed
    # unconditionally, every run — including the all-OK case ("OK states its
    # OK-ness rather than printing nothing": a check nobody ever sees run is
    # indistinguishable from a check that's silently broken, the exact
    # failure mode #1631 exists to close). Aggregation itself lives in
    # coord.health.aggregate — this command only renders it.
    try:
        from coord.health.aggregate import render_fleet_footer, summarize_fleet_health

        click.echo("")
        click.echo(render_fleet_footer(summarize_fleet_health(fleet_health_block)))
    except Exception:  # noqa: BLE001 — the footer must never break `coord status`
        click.echo("")
        click.echo("FLEET: ?  (health footer unavailable — coord health for detail)")


def _host_resolution_lines(machine, ts_map: dict[str, tuple[str, str]] | None) -> list[tuple[bool, str]]:
    """#2912: does ``host:`` actually resolve to THIS machine's tailnet
    address? A LAN DHCP/DNS entry that shares a name with a tailnet node
    (e.g. a WSL2 box named ``dell64`` whose Windows host also registers
    ``dell64.lan``) shadows MagicDNS in the resolver order — HTTP-by-name
    times out against the wrong LAN device while ``tailscale ping`` and the
    agent's own ``/health`` are both perfectly healthy, so the symptom is
    indistinguishable from a dead agent, a firewall, or a crashed unit.

    Fires regardless of whether the machine is currently reachable — that's
    the whole point: it names the cause a plain "unreachable" cannot, and it
    also catches a collision BEFORE it manifests as an outage. Silent when
    ``coord.network.check_host_resolution`` can't determine an answer (no
    local ``tailscale``, node not in this box's peer list, ``host:`` doesn't
    resolve at all) — those are absence-of-evidence, not a finding.

    Pure function over :func:`coord.network.check_host_resolution`'s result
    — no I/O of its own — so it's testable without a live tailnet.
    """
    from coord import network  # noqa: PLC0415

    result = network.check_host_resolution(machine, ts_map)
    if result.matches is not False:
        return []
    return [(
        True,
        f"  ✗ CRIT machines.host_resolves_offtailnet: {result.reason} "
        f"(#2912). Remedy: set `host: {result.magicdns_fqdn}` in "
        "coordinator.yml — the MagicDNS FQDN — instead of the bare "
        "hostname, which a LAN DNS entry can shadow.",
    )]


def _health_vs_config_lines(machine, health: dict) -> list[tuple[bool, str]]:
    """Cross-check a machine's ``/health`` against what ``coordinator.yml``
    declares for it. Returns ``(is_problem, line)`` pairs (#1712).

    A machine that publishes ``capabilities: []`` while the config declares
    capabilities for it is a **misconfiguration**, not an absence — and the two
    are indistinguishable from ``/health`` alone, which is exactly how #1673
    stayed "unexplained" for so long: precision was silently ineligible for
    every ``rust``/``python``/``gtk`` dispatch and nothing anywhere said so.
    Same shape for ``repos: []`` (#1485's review-router misread).

    #1801: that inference only holds for a **standing** agent — one with its
    own ``coordinator.yml`` that is expected to publish from it. A
    **config-free** agent (``health["config_free"]`` set) is DESIGNED to
    publish empty capabilities/repos: the coordinator supplies both at
    dispatch time instead. Before this fix, a config-free agent whose
    machine entry in the coordinator's OWN ``coordinator.yml`` happened to
    declare capabilities/repos (azure-epic1709: ``['rust', 'python']`` /
    ``['claude-coordinator']``) still hit the CRIT branch below — and the
    CRIT's own detail line ("the agent is running config-free") flatly
    contradicted the CRIT's headline ("every dispatch ... will be refused"),
    while dispatch in fact worked. A config-free mismatch is now reported at
    WARN (not a problem), while a *configured* agent publishing nothing
    still CRITs — that's the #1485/#1712 case this check exists for.

    Pure function — no I/O — so it's testable without a live fleet.
    """
    out: list[tuple[bool, str]] = []
    # None on the normal path; a reason string when the agent came up with no
    # config at all (#1712). Absent entirely on agents predating #1712.
    config_free = health.get("config_free")

    declared_caps = list(getattr(machine, "capabilities", None) or [])
    published_caps = list(health.get("capabilities") or [])
    declared_repos = list(getattr(machine, "repos", None) or [])
    published_repos = list(health.get("repos") or [])
    degraded = health.get("degraded") or {}

    if config_free and not declared_caps and not declared_repos:
        # Legitimately config-free (ephemeral worker) AND the config agrees
        # there's nothing to declare — worth surfacing, but not a failure.
        out.append((False, f"  ⚠ running config-free — {config_free}"))

    if declared_caps and not published_caps:
        if config_free:
            # #1801: expected shape for a config-free agent — the coordinator
            # supplies capabilities at dispatch time, not this machine's own
            # config, so an empty /health is not a dispatch blocker.
            out.append((
                False,
                f"  ⚠ capabilities: coordinator.yml declares {declared_caps} "
                "but /health publishes none — the agent is running "
                f"config-free ({config_free}); capabilities come from the "
                "coordinator at dispatch time, not this machine's own "
                "config, so this is expected, not a dispatch blocker (#1801)",
            ))
        else:
            out.append((
                True,
                f"  ✗ CRIT capabilities: coordinator.yml declares "
                f"{declared_caps} but /health publishes none — this machine "
                "is silently ineligible for every capability-matched "
                "dispatch (#1712)",
            ))
            out.append((
                True,
                "        the agent published no capabilities despite a "
                "loadable config: check that its unit's --machine names this "
                "machine, then `coord agent update` + restart coord-agent",
            ))

    if declared_repos and not published_repos:
        if config_free:
            out.append((
                False,
                f"  ⚠ repos: coordinator.yml declares {declared_repos} but "
                "/health advertises none — the agent is running config-free "
                f"({config_free}); repos come from the coordinator at "
                "dispatch time, not this machine's own config, so this is "
                "expected, not a dispatch blocker (#1801)",
            ))
        else:
            out.append((
                True,
                f"  ✗ CRIT repos: coordinator.yml declares {declared_repos} "
                "but /health advertises none — every dispatch to this "
                "machine will be refused, and any reader trusting /health "
                "sees a repo-less machine (#1485/#1712)",
            ))
            for repo, reason in sorted(degraded.items()):
                out.append((True, f"        {repo}: {reason}"))
    elif declared_repos and published_repos and not config_free:
        # #2219: the ABOVE branch only fires when /health publishes NOTHING
        # — it silently missed the partial-drift shape that actually bit
        # stick-demo#1: a repo added to coordinator.yml after the agent
        # process started stays off /health's list while everything ELSE
        # the agent publishes (including all its OTHER repos) looks
        # perfectly healthy. `coord config`/`coord status` both read
        # config, so both still advertised it; only the live agent —
        # and, at dispatch cost, the drive queue that burned both retry
        # attempts on it — knew better.
        #
        # `published_repos` (``/health``'s ``repos``) is
        # ``AgentServer._servable_repos()``'s FILTERED list (#1527) — it
        # also drops any repo that IS configured on that machine but is
        # DEGRADED (no ``repo_paths`` entry, or the configured path is
        # missing on disk). A drifted repo that's actually degraded is not
        # the #2219 stale-config story: `AgentServer.assign()` gates on the
        # UNFILTERED `self.repos`, so it would still accept that repo and
        # fail later with a distinct "repo path does not exist" error —
        # restarting coord-agent cannot repair a missing/misconfigured
        # `repo_paths` entry. Split on `degraded` so each drifted repo gets
        # the accurate story and remedy.
        drifted = [r for r in declared_repos if r not in published_repos]
        stale = [r for r in drifted if r not in degraded]
        if stale:
            out.append((
                True,
                f"  ✗ CRIT repos: coordinator.yml declares {declared_repos} "
                f"but /health only advertises {published_repos} — {stale} "
                "were added to config after this agent process started and "
                "it hasn't re-read them; a dispatch to any of "
                f"{stale} on this machine will be refused despite `coord "
                "config`/`coord status` showing it as supported (#2219). "
                "Agents re-read coordinator.yml on their own /health poll "
                "since #2299, so re-check in a moment before doing anything: "
                "a gap that persists means this machine's copy of the config "
                "is stale (`git pull` the settings checkout HERE), the edit "
                "is malformed (`journalctl --user -u coord-agent` will say "
                "`failed to reload`), or this agent predates #2299 "
                "(`coord agent update`).",
            ))
        for repo in drifted:
            if repo in degraded:
                out.append((
                    True,
                    f"  ✗ CRIT repos: coordinator.yml declares {repo!r} for "
                    "this machine and the live agent agrees it's "
                    f"configured, but it's DEGRADED there, not just "
                    f"unrefreshed: {degraded[repo]} (#1527). This is not "
                    "the #2219 stale-config case — restarting coord-agent "
                    f"will not fix it; repair repo_paths[{repo!r}] on this "
                    "machine instead.",
                ))

    return out


def _unit_drift_lines(health: dict) -> list[tuple[bool, str]]:
    """Render a machine's ``unit_drift`` H-1 results (``coord/health/checks/
    unit_drift.py``, #1831) as ``coord doctor`` lines.

    ``/health``'s ``health`` block already carries every machine/checkout
    check this agent ran (``_cached_local_health`` in ``coord/agent.py``) —
    this just projects the one check id ``coord doctor`` cares about into its
    own report, the same way the tool_versions section below projects
    ``health["tool_versions"]``. An agent predating #1831 simply has no
    ``unit_drift`` entries here, so this renders nothing for it — never a
    false "clean".

    Pure function — no I/O — so it's testable without a live fleet.
    """
    out: list[tuple[bool, str]] = []
    results = ((health.get("health") or {}).get("results") or [])
    for r in results:
        if r.get("check_id") != "unit_drift":
            continue
        severity = r.get("severity")
        subject = r.get("subject")
        headroom = r.get("headroom", "")
        label = f" {subject}" if subject else ""
        if severity == "crit":
            out.append((True, f"  ✗ CRIT unit drift{label}: {headroom}"))
            detail = r.get("detail")
            if detail:
                out.append((True, f"        {detail}"))
        elif severity == "warn":
            out.append((True, f"  ⚠ unit drift{label}: {headroom}"))
            detail = r.get("detail")
            if detail:
                out.append((True, f"        fix: {detail}"))
        elif severity == "unknown":
            out.append((False, f"  ? unit drift{label}: {headroom}"))
    return out


def _unit_enablement_lines(health: dict) -> list[tuple[bool, str]]:
    """Render a machine's ``unit_enablement`` H-1 results (``coord/health/
    checks/unit_enablement.py``, #2098) as ``coord doctor`` lines.

    Mirrors ``_unit_drift_lines`` immediately above: ``unit_drift`` answers
    "does an installed unit's content match ``deploy/``", this answers "is
    an installed, manifest-listed unit actually ``systemctl --user
    enable``d" — the state that hid ``coord-release-propagate.timer`` for a
    day, because a disabled timer and a deferring one produce identical
    evidence otherwise. Before this renderer existed, that WARN only
    surfaced in the per-machine aggregate ``severity`` (the "FLEET: WARN"
    footer / coord-tui indicator) and in ``coord health``'s own per-unit
    detail — an operator reading ``coord doctor``'s printed report, which
    ``docs/AGENT_OPERATIONS.md`` explicitly points to for this, saw nothing
    naming which unit was disabled or how to fix it.

    An agent predating #2098 simply has no ``unit_enablement`` entries here,
    so this renders nothing for it — never a false "clean".

    Pure function — no I/O — so it's testable without a live fleet.
    """
    out: list[tuple[bool, str]] = []
    results = ((health.get("health") or {}).get("results") or [])
    for r in results:
        if r.get("check_id") != "unit_enablement":
            continue
        severity = r.get("severity")
        subject = r.get("subject")
        headroom = r.get("headroom", "")
        label = f" {subject}" if subject else ""
        if severity == "warn":
            out.append((True, f"  ⚠ unit enablement{label}: {headroom}"))
            detail = r.get("detail")
            if detail:
                out.append((True, f"        fix: {detail}"))
        elif severity == "unknown":
            out.append((False, f"  ? unit enablement{label}: {headroom}"))
    return out


def _dispatch_blocker_lines_for_config_free(machine, cfg) -> list[tuple[bool, str]]:
    """Real dispatch blockers on a **config-free** agent's machine (#1801).

    The #1712 capabilities/repos cross-check above CRIT'd azure-epic1709 for
    the wrong reason (an expected config-free shape) while staying silent
    about the two things that actually *would* refuse dispatch there: a
    declared repo with no ``repo_paths`` entry (``coord.dispatch.dispatch``
    raises ``ValueError`` on exactly this), and a declared repo whose
    resolved provider (``Repo.provider`` > ``providers.default``) the
    machine hasn't declared support for via ``provider:<type>`` (the #1711
    structural gate, ``coord.providers.guard_provider_machine_capability``).

    Both are derivable from ``coordinator.yml`` alone — no ``/health``
    round-trip needed — so this runs regardless of whether the machine is
    currently reachable. Scoped to config-free machines because that's the
    gap #1801 found; a standing machine's own config predates this check and
    is out of scope here.

    Pure function — no I/O — so it's testable without a live fleet.
    """
    from coord.config import provider_capability
    from coord.providers import (
        machine_supports_provider,
        provider_type_for,
        resolve_provider_name,
    )

    out: list[tuple[bool, str]] = []

    missing_paths = sorted(r for r in machine.repos if not machine.repo_path(r))
    if missing_paths:
        out.append((
            True,
            f"  ✗ CRIT repo_paths: {missing_paths} declared under repos but "
            "have no repo_paths entry in coordinator.yml — dispatch to this "
            "machine for these repos will be refused (#1801)",
        ))

    missing_caps: list[str] = []
    for repo_name in machine.repos:
        repo = cfg.repo(repo_name)
        repo_provider = repo.provider if repo is not None else None
        provider_name = resolve_provider_name(None, repo_provider, cfg.providers)
        if not machine_supports_provider(machine, provider_name, cfg.providers):
            ptype = provider_type_for(provider_name, cfg.providers)
            cap = provider_capability(ptype)
            if cap not in missing_caps:
                missing_caps.append(cap)
    if missing_caps:
        out.append((
            True,
            "  ✗ CRIT provider capability: this machine's declared repos "
            f"expect {sorted(missing_caps)} but coordinator.yml capabilities "
            "don't include it — dispatch with that provider will be refused "
            "(#1711/#1801)",
        ))

    return out


def _repo_onboarding_lines(cfg, statuses) -> list[tuple[bool, str]]:
    """Render every configured repo's onboarding residue as ``coord doctor``
    lines (:mod:`coord.repo_onboard`, #2220).

    This is the wiring #2220 asks for: a half-onboarded repo shows up in the
    fleet report *without anyone remembering to ask*. ``stick-demo`` sat
    two-thirds onboarded for days and the only thing that ever noticed was a
    queue entry dying of it.

    Runs with ``probe_github=False`` and no local-disk graph probe, so it costs
    **zero** extra round trips — it re-reads the ``/health`` bodies this
    command has already fetched. That is enough for the two findings that
    actually bit: the #2219 agent/config repo skew, and (since #2237) a repo
    with no graph on any machine that runs workers — both visible from
    ``/health`` alone. The GitHub and repo-contents layers need ``coord repo
    doctor <name>``, which the footer below points at.

    Mirrors :func:`_unit_drift_lines`/:func:`_release_lag_lines`: same
    computation as the dedicated command (#2096's "two surfaces, one
    function" rule), projected into doctor's output. Best-effort — a repo whose
    facts can't be gathered is skipped rather than breaking the fleet report.
    """
    from coord import repo_onboard  # noqa: PLC0415

    out: list[tuple[bool, str]] = []
    for repo in getattr(cfg, "repos", None) or []:
        try:
            facts = repo_onboard.gather_facts(
                cfg, repo.name, statuses=statuses, probe_github=False
            )
            report = repo_onboard.evaluate(facts)
        except Exception:  # noqa: BLE001 — never break the fleet report
            continue
        out.extend(repo_onboard.doctor_summary_lines(report))
    return out


def _machine_onboarding_lines(cfg, machine, statuses, ts_map) -> list[tuple[bool, str]]:
    """Render one machine's onboarding residue as ``coord doctor`` lines
    (:mod:`coord.machine_onboard`, #2915).

    The machine-side counterpart of :func:`_repo_onboarding_lines`, and the
    wiring #2915 asks for: a half-onboarded *machine* shows up in the fleet
    report without anyone remembering to ask. ``dell64`` was onboarded through
    six separately-silent failures and the only thing that ever noticed any of
    them was an operator reading logs by hand.

    Costs **zero** extra round trips — it re-reads the ``/health`` body this
    command already fetched, and the ``ts_map`` it already resolved once for
    the #2912 host check. No SSH, so ``runtime.linger`` stays UNKNOWN here and
    is never surfaced (UNKNOWN is not a problem); ``coord machine doctor
    <name> --ssh`` is what answers it.

    Mirrors :func:`_unit_drift_lines`/:func:`_release_lag_lines`: same
    computation as the dedicated command (#2096's "two surfaces, one function"
    rule), projected into doctor's output. Best-effort — a machine whose facts
    can't be gathered is skipped rather than breaking the fleet report.
    """
    from coord import machine_onboard  # noqa: PLC0415

    try:
        facts = machine_onboard.gather_facts(
            cfg, machine.name, statuses=statuses, ts_map=ts_map or {}
        )
        report = machine_onboard.evaluate(facts)
    except Exception:  # noqa: BLE001 — never break the fleet report
        return []
    return machine_onboard.doctor_summary_lines(report)


def _release_lag_lines(report) -> list[tuple[bool, str]]:
    """Render a :class:`coord.release_verify.VerifyReport`'s findings as
    ``coord doctor`` lines (#2082).

    #2082's whole complaint: ``coord release verify`` already computes
    whether the fleet's running version matches the released one, and
    already returns CRIT when it doesn't — nothing *routine* ever called
    it, so a fleet could sit eleven releases behind PyPI with every other
    readout silent. This projects that SAME computation (not a second one —
    see the epic's "two surfaces, one function" rule) into doctor's output,
    the same way :func:`_unit_drift_lines` above projects ``unit_drift``.

    Filters out ``unit `` findings: those are ``unit_drift`` results folded
    into ``coord release verify`` (#1834), and doctor already renders that
    same data via :func:`_unit_drift_lines` — showing it twice would be
    noise, not a second opinion. UNKNOWN findings (an unreachable host, a
    lane with no data yet, "no expected version to grade against") are
    likewise not surfaced here: they never made a problem elsewhere in this
    command either, and their causes are already visible above (the
    "unreachable" line, tool_versions gaps).
    """
    out: list[tuple[bool, str]] = []
    for f in report.findings:
        if f.severity not in ("crit", "warn"):
            continue
        if f.lane.startswith("unit "):
            continue
        mark = "✗ CRIT" if f.severity == "crit" else "⚠ WARN"
        out.append((True, f"  {mark} release version: {f.host}/{f.lane}: {f.summary}"))
        if f.detail:
            out.append((True, f"        {f.detail}"))
    return out


@click.command(
    help=(
        "Fleet-wide prereq report: is this machine fit to be routed work?\n\n"
        "#1570 E -- \"One command, whole fleet, prereq status per machine. "
        "Would have answered [the #1564 gh-version incident] in seconds.\" "
        "Reads each machine's already-probed tool_versions straight out of "
        "/health (#1570 B), so it costs exactly what `coord status` costs "
        "-- no SSHing around the fleet by hand.\n\n"
        "Exits 1 if any machine is unreachable, hasn't upgraded to publish "
        "tool_versions yet, fails a baseline prereq, or claims a capability "
        "its own probe can't back up."
    )
)
@_CONFIG_OPTION
@click.option("--machine", "machine_filter", default=None, help="Only check this machine.")
@click.option(
    "--timeout", default=3.0, show_default=True, type=float,
    help="Per-machine /health timeout (seconds).",
)
@click.option(
    "--expected", default=None,
    help=(
        "The version every lane should be on (leading 'v' optional) — same "
        "semantics as `coord release verify --expected` (#2082)."
    ),
)
@click.option(
    "--pypi/--no-pypi", "use_pypi", default=True, show_default=True,
    help=(
        "Resolve --expected from the PyPI simple index when not given "
        "explicitly, so a fleet that is uniformly behind the released "
        "version reads as CRIT here rather than clean (#2052's lesson, "
        "applied to doctor by #2082) — same default `coord release verify "
        "--pypi` uses, and the same resolution (`_resolve_expected`)."
    ),
)
def doctor(
    config_path: Path,
    machine_filter: str | None,
    timeout: float,
    expected: str | None,
    use_pypi: bool,
) -> None:
    from coord import network
    from coord.network import check_all
    from coord.prereqs import ToolProbe, unmet_capabilities

    cfg = _load_config(config_path)
    machines = cfg.machines
    if machine_filter:
        machines = [m for m in machines if m.name == machine_filter]
        if not machines:
            click.echo(
                f"error: machine {machine_filter!r} not in coordinator.yml "
                f"(have: {[m.name for m in cfg.machines]})",
                err=True,
            )
            sys.exit(2)

    statuses = check_all(machines, timeout=timeout)
    # #2912: resolved once, locally, up front — every machine's `host:` is
    # checked against the SAME local tailscale peer list, and a single
    # `tailscale status --json` covers the whole fleet. `None` when
    # tailscale isn't available on THIS box; `_host_resolution_lines`
    # renders nothing in that case rather than fabricating a mismatch.
    ts_map = network.tailscale_ip_map(timeout=timeout)
    any_problem = False
    for s in statuses:
        m = s.machine
        click.echo(f"{m.name} ({m.host}):")
        # Runs even when unreachable — that's the case this exists for:
        # naming why "unreachable" is misleading instead of leaving it
        # indistinguishable from a dead agent or a crashed unit.
        for is_problem, line in _host_resolution_lines(m, ts_map):
            click.echo(line)
            if is_problem:
                any_problem = True
        if not s.is_online:
            click.echo(f"  ✗ unreachable — {s.reason}")
            any_problem = True
            continue

        health = s.health or {}
        # #1712: run the config-vs-/health cross-check BEFORE the
        # tool_versions early-continue below — "declares capabilities,
        # publishes none" is the loudest thing this command can say about a
        # machine, and it must not be skipped just because the agent is also
        # too old to report tool_versions.
        for is_problem, line in _health_vs_config_lines(m, health):
            click.echo(line)
            if is_problem:
                any_problem = True

        # #1801: for a config-free agent, the #1712 check above is silenced
        # (empty /health capabilities/repos is the designed shape) — so run
        # the checks that actually catch what blocks dispatch there instead:
        # missing repo_paths, missing provider:* capability. Both come
        # straight out of coordinator.yml.
        if health.get("config_free"):
            for is_problem, line in _dispatch_blocker_lines_for_config_free(m, cfg):
                click.echo(line)
                if is_problem:
                    any_problem = True

        # #1831: installed systemd units silently drift from deploy/ — this
        # is the same class of blind spot #1712 closed for capabilities/repos,
        # projected from the machine's own unit_drift H-1 check rather than
        # SSHing in to diff unit files by hand.
        for is_problem, line in _unit_drift_lines(health):
            click.echo(line)
            if is_problem:
                any_problem = True

        # #2098: same class of blind spot as unit_drift immediately above,
        # but for enablement rather than content — an installed unit that
        # the manifest says this host should run and that isn't actually
        # `systemctl --user enable`d.
        for is_problem, line in _unit_enablement_lines(health):
            click.echo(line)
            if is_problem:
                any_problem = True

        # #2915: is this MACHINE only half-onboarded? Costs nothing extra —
        # reuses the `/health` body and the `ts_map` already resolved above.
        # Deliberately only graphify + the agent venv — the two layers
        # nothing else in this command renders. Repo-clone presence is
        # deliberately NOT folded in a second time here: the #1712
        # cross-check immediately above (`_health_vs_config_lines`) already
        # answers "is a declared repo actually being served" for both the
        # total-loss and #2219 partial-drift shapes, under its own
        # `CRIT repos: ...` name — folding `clones` in too printed the same
        # defect twice under two names (caught in review of #2915). See
        # `machine_onboard.DOCTOR_LIVE_LAYERS` for the full reasoning, and
        # for why network/agent are excluded the same way. Placed BEFORE
        # the tool_versions early-continue below for the same reason #1712's
        # cross-check is: "the graph CLI is missing" and "this repo isn't
        # cloned" must not be skipped just because the agent is also too old
        # to report probes.
        for is_problem, line in _machine_onboarding_lines(cfg, m, statuses, ts_map):
            click.echo(line)
            if is_problem:
                any_problem = True

        raw_probes = health.get("tool_versions")
        if not raw_probes:
            click.echo(
                "  ⚠ no tool_versions in /health — agent predates #1570 B "
                "(`coord agent update` to see prereq status)"
            )
            any_problem = True
            continue

        probes = {
            tool: ToolProbe(
                tool=tool,
                capability=info.get("capability"),
                found=bool(info.get("found", False)),
                version=info.get("version"),
                min_version=info.get("min_version"),
                meets_floor=info.get("meets_floor"),
                what_breaks="",
            )
            for tool, info in raw_probes.items()
            if isinstance(info, dict)
        }
        for tool, p in sorted(probes.items()):
            marker = "✓" if p.ok else "✗"
            if not p.ok:
                any_problem = True
            if not p.found:
                detail = "not found"
            else:
                detail = p.version or "found (version unknown)"
            floor = f"  (>= {p.min_version} required)" if p.min_version else ""
            click.echo(f"  {marker} {tool}: {detail}{floor}")

        unmet = unmet_capabilities(m.capabilities, probes)
        for cap, reasons in unmet.items():
            any_problem = True
            for reason in reasons:
                click.echo(f"  ✗ capability {cap!r} claimed but unmet — {reason}")

    # #2220: is any repo only half-onboarded? Costs nothing extra — reuses the
    # `/health` bodies fetched above. Skipped under `--machine`, where the
    # per-machine slice would report every OTHER machine's repos as
    # "unreachable" purely because this run never probed them.
    if not machine_filter:
        onboarding_lines = _repo_onboarding_lines(cfg, statuses)
        if onboarding_lines:
            click.echo("")
            click.echo("repo onboarding (coord repo doctor):")
            for is_problem, line in onboarding_lines:
                click.echo(line)
                if is_problem:
                    any_problem = True
            click.echo(
                "  → this is the LIVE-state layer only. GitHub labels, the "
                "pull_request trigger, CLAUDE.md, the Test-stage command and "
                "graph freshness are NOT checked here: run "
                "`coord repo doctor <name>`"
            )

    # #2082: is the fleet actually running the released version? On
    # 2026-08-10 it was eleven releases behind (v0.5.15 vs PyPI's v0.5.26)
    # and nothing routine said so — `coord release verify` already computed
    # this exact comparison and already returned CRIT, it was just never
    # called anywhere an operator would see without being told to look.
    # Reuses that SAME function (not a second comparison — #2096's "two
    # surfaces, one function" rule) over the `/health` bodies this command
    # already fetched above, so this costs no extra per-machine round trip —
    # only the one-time PyPI resolution below.
    from coord import release_verify as rv  # noqa: PLC0415
    from coord.commands.release import _resolve_expected  # noqa: PLC0415

    index_url = getattr(getattr(cfg, "health", None), "pypi_index_url",
                        "https://pypi.org/simple")
    resolved_expected, resolve_warning = _resolve_expected(
        expected, use_pypi=use_pypi, index_url=index_url, timeout=timeout
    )
    if resolve_warning:
        click.echo(f"⚠ {resolve_warning}")

    machine_health = {s.machine.name: (s.health if s.is_online else None) for s in statuses}
    unreachable = {
        s.machine.name: (s.reason or "offline") for s in statuses if not s.is_online
    }
    release_report = rv.verify(
        machine_health=machine_health, unreachable=unreachable, expected=resolved_expected,
    )
    lag_lines = _release_lag_lines(release_report)
    if lag_lines:
        click.echo("")
        click.echo("release version (coord release verify):")
        for is_problem, line in lag_lines:
            click.echo(line)
            if is_problem:
                any_problem = True

    # #2595: a cordoned host with ZERO active assignments and past its drain
    # deadline is functionally removed from the fleet — reachable, healthy,
    # and taking nothing. `coord doctor`'s whole question is "is this
    # machine fit to be routed work?", and a stuck cordon is the starkest
    # way there is to fail that. Same pure decision `coord status` and the
    # `release_cordon_idle` health check use
    # (`coord.release_cordon.idle_overdue_cordons`) — only the "idle" input
    # differs (here: every `coordinator.yml` machine with no `running` board
    # row). Deliberately NOT `Board.idle_machines()` — that filters
    # `Board.machines`, which is populated by machine registration/dispatch
    # history rather than by `coordinator.yml`, so a machine `coord doctor`
    # has never dispatched to would read as neither idle nor busy instead of
    # idle.
    #
    # #615/#906: the board read below goes through `board_service.read_board()`,
    # NOT `coord.state.build_board()` — `doctor` has no
    # `daemon_reroute_target()` early-return, so a direct local read would see
    # an EMPTY board on a thin client, mark every configured machine idle, and
    # print a fabricated CRIT for a cordoned host that is in fact mid-roll
    # with a running assignment. `read_board()` GETs the daemon's canonical
    # board when `board_service` is configured and falls back to the local DB
    # otherwise.
    import time as _time  # noqa: PLC0415

    from coord.board_service import read_board  # noqa: PLC0415
    from coord.machine_pause import cordons as fetch_release_cordons  # noqa: PLC0415
    from coord.release_cordon import idle_overdue_cordons  # noqa: PLC0415

    try:
        busy_names: set[str] | None = {
            a.machine_name for a in read_board().active if a.status == "running"
        }
    except Exception:  # noqa: BLE001 — doctor must still report everything else
        # Fail CLOSED: without a trustworthy board we cannot tell idle from
        # busy, and guessing "idle" would invent a CRIT for every cordoned
        # host. Skip the check rather than report a fabricated one.
        busy_names = None
    host_versions = {
        name: (health or {}).get("version") for name, health in machine_health.items()
    }
    overdue_idle: tuple = ()
    if busy_names is not None:
        idle_names = {mach.name for mach in cfg.machines} - busy_names
        try:
            overdue_idle = idle_overdue_cordons(
                fetch_release_cordons(),
                now=_time.time(),
                idle_hosts=idle_names,
                host_versions=host_versions,
            )
        except Exception:  # noqa: BLE001
            overdue_idle = ()
    if overdue_idle:
        click.echo("")
        click.echo("release cordons (coord release cordon):")
        for overdue in overdue_idle:
            any_problem = True
            click.echo(f"  ✗ CRIT: {overdue.message}")

    # #2607: name the #2587 roll-pending marker here too, not just in `coord
    # drive-queue status` — an operator running `coord doctor` to ask "why
    # isn't the fleet doing anything" must see this without already knowing
    # the marker exists. Host-local (the file the daemon host's own
    # `coord-drive-queue.timer` reads), so this only has something to say
    # when run ON that host — same caveat as the release-verify section
    # above resolving PyPI locally.
    from coord.commands.drive_queue import read_roll_pending  # noqa: PLC0415

    roll_pending = read_roll_pending()
    if roll_pending is not None:
        import time as _time  # noqa: PLC0415

        age = _time.time() - roll_pending.set_at
        click.echo("")
        click.echo(
            f"⏸ {roll_pending.describe()} — set {age:.0f}s ago, "
            f"{roll_pending.deferrals}/{roll_pending.max_deferrals} deferrals "
            "— the drive queue on this host launches nothing until it fires "
            "or expires. Escape hatch: `coord drive-queue cancel-roll`."
        )

    # #1862: a quiet-hours window that removes the only machine with a
    # capability makes matching work silently unroutable (`dispatch_smoke`
    # already has this failure shape — #1678: it refuses to route and the
    # Test stage retries forever with no error). Cheap heuristic, not full
    # time-overlap math: if EVERY machine advertising a capability has some
    # `quiet_hours` window, coverage isn't guaranteed around the clock; if
    # at least one machine offering it has none, it's always coverable.
    # Runs over the FULL fleet regardless of `--machine`.
    caps_with_quiet_hours: dict[str, list[str]] = {}
    caps_without_quiet_hours: set[str] = set()
    for m in cfg.machines:
        for cap in m.capabilities:
            if m.quiet_hours is not None:
                caps_with_quiet_hours.setdefault(cap, []).append(m.name)
            else:
                caps_without_quiet_hours.add(cap)
    for cap, quiet_machine_names in sorted(caps_with_quiet_hours.items()):
        if cap in caps_without_quiet_hours:
            continue
        any_problem = True
        names = ", ".join(sorted(quiet_machine_names))
        click.echo(
            f"⚠ capability {cap!r} is only ever offered by machine(s) with "
            f"quiet_hours configured ({names}) — an overlapping window "
            "could leave it with no awake machine to route to"
        )

    if any_problem:
        sys.exit(1)


@click.command("show-plan", help="Pretty-print the structured plan for a plan-only assignment.")
@click.argument("assignment_id")
def show_plan(assignment_id: str) -> None:
    from coord.board_service import read_board
    from coord.plan_parser import WorkerPlan, parse_plan_from_log
    from coord.state import COORD_DIR, load_plans

    board = read_board()
    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    if assignment.type != "plan":
        atype = assignment.type
        click.echo(
            f"error: assignment {assignment_id} is type {atype!r}, not 'plan'",
            err=True,
        )
        sys.exit(1)

    # 1. Try the plan cached on the board/assignment record.
    plan_dict = assignment.plan
    if plan_dict is None:
        plans = load_plans()
        plan_dict = plans.get(assignment_id)

    # 2. Fall back to parsing the log directly (works when agent is local).
    if plan_dict is None:
        local_log = COORD_DIR / "logs" / f"{assignment_id}.log"
        try:
            worker_plan = parse_plan_from_log(local_log)
        except Exception:  # noqa: BLE001
            worker_plan = None
        if worker_plan is not None:
            plan_dict = worker_plan.to_dict()

    if plan_dict is None:
        click.echo(
            f"No structured plan found for assignment {assignment_id}.\n"
            "Possible reasons: the worker has not completed yet, the log is on "
            "a remote machine, or the worker did not output plan sections.\n"
            "Run 'coord notify' after the worker finishes to parse and cache the plan."
        )
        return

    _display_plan(WorkerPlan.from_dict(plan_dict), assignment)


def _display_plan(plan: object, assignment: object) -> None:
    """Pretty-print a WorkerPlan to stdout."""
    from coord.plan_parser import WorkerPlan  # noqa: PLC0415

    assert isinstance(plan, WorkerPlan)

    repo_name = getattr(assignment, "repo_name", "?")
    issue_number = getattr(assignment, "issue_number", "?")
    issue_title = getattr(assignment, "issue_title", "")
    machine_name = getattr(assignment, "machine_name", "?")
    assignment_id = getattr(assignment, "assignment_id", "?")

    click.echo(
        f"## Plan — {repo_name} #{issue_number}: {issue_title}"
    )
    click.echo(f"Assignment: {assignment_id}  Machine: {machine_name}")

    if plan.plan:
        click.echo("")
        click.echo("### Summary")
        click.echo(plan.plan)

    if plan.files_read:
        click.echo("")
        click.echo("### Files Read")
        for f in plan.files_read:
            click.echo(f"  {f}")

    if plan.files_modify:
        click.echo("")
        click.echo("### Files to Modify")
        for f in plan.files_modify:
            click.echo(f"  {f}")

    if plan.approach:
        click.echo("")
        click.echo("### Approach")
        click.echo(plan.approach)

    if plan.risks:
        click.echo("")
        click.echo("### Risks")
        click.echo(plan.risks)

    if plan.estimate:
        click.echo("")
        click.echo("### Estimate")
        click.echo(plan.estimate)


def _diagnose_via_daemon(svc, params: dict) -> None:
    """#diagnose: run ``coord diagnose`` on the daemon host (canonical board +
    gh + ssh access to the fleet) and relay its output, so the per-stage doctor
    does real work from a thin client instead of no-opping against an empty
    local board.  Mirrors ``_reconcile_via_daemon``."""
    from coord.client import post_record  # noqa: PLC0415

    try:
        resp = post_record(svc, "/diagnose", params, timeout=180.0)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: diagnose via daemon failed: {exc}", err=True)
        sys.exit(1)
    output = resp.get("output") or ""
    if output:
        click.echo(output, nl=False)
    if resp.get("error"):
        click.echo(f"error: {resp['error']}", err=True)
    code = resp.get("exit_code") or 0
    if code:
        sys.exit(int(code))


@click.command(
    help=(
        "Diagnose and fix a specific pipeline stage of an issue.\n\n"
        "Inspects the stage (phantom 'running' rows, dropped review findings, "
        "stale-but-live sessions, merged-but-grey boxes, orphaned worktrees), "
        "makes a BEST-EFFORT non-destructive recovery of THAT stage (finalize, "
        "recover review findings from the session transcript, reconcile "
        "merges), and ALWAYS scans this issue's OTHER board rows for phantom "
        "'running' rows. When recovery isn't possible it reports "
        "needs_reset=true; re-run with --reset to clear the stage's "
        "rows/claim/worktree and stop a live session — KEEPING the branch + "
        "commits, so the stage is re-dispatchable. A phantom row found on the "
        "issue-wide scan (#1658) is only ever a RECOMMENDATION without "
        "--reset — it is reported, never finalized, until --reset is passed.\n\n"
        "Pass --orphan-worktrees instead of REPO/ISSUE to run a local fleet sweep "
        "that removes coordinator worktrees with no live tmux session and no "
        "uncommitted work.  Dirty worktrees are reported but never auto-deleted."
    )
)


@click.argument("repo", required=False, default=None)
@click.argument("issue", type=int, required=False, default=None)
@click.option(
    "--stage",
    # #2087: "smoke" added — it was previously unreachable via an explicit
    # --stage (rejected here before ever reaching diagnose_stage()) even
    # though an implicit (no --stage) pick could already land on a
    # type="smoke" row and then dead-end inside diagnose_stage() with "no
    # diagnosis available". See STAGE_ASSIGNMENT_TYPES["smoke"] (diagnose.py).
    type=click.Choice(["plan", "work", "review", "test", "merge", "smoke"]),
    default=None,
    help="Which stage to diagnose (default: the issue's most-recent stage).",
)


@click.option(
    "--reset",
    is_flag=True,
    help="Non-destructive reset: clear the stage's rows/claim/worktree and stop "
    "a live session, KEEPING the branch + commits (stage re-dispatchable).",
)


@click.option("--dry-run", is_flag=True, help="Report findings without writing.")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help=(
        "#935: emit the DiagnoseResult as a JSON object on stdout (in addition to "
        "the human-readable lines and the DIAGNOSE_RESULT trailer).  The JSON block "
        "is printed BEFORE the trailer so callers can parse it without grepping."
    ),
)
@click.option(
    "--orphan-worktrees",
    is_flag=True,
    help=(
        "#618: local fleet sweep — find and remove coordinator worktrees "
        "(~/.coord/worktrees/*) whose assignment has no live tmux session and "
        "no uncommitted work.  Dirty worktrees are reported but never deleted."
    ),
)
@click.option(
    "--graph",
    "graph_health",
    is_flag=True,
    help=(
        "Report graphify knowledge-graph freshness for this machine's local "
        "checkouts: whether the graph matches HEAD, and whether "
        "core.hooksPath is set so worktrees get a linked graph.  Read-only."
    ),
)
@click.option(
    "--config-provenance",
    "config_provenance_check",
    is_flag=True,
    help=(
        "#1779: report whether THIS machine's live ~/.coord/coordinator.yml "
        "is still a symlink into the coord-settings checkout (vs. having "
        "been silently replaced by `coord init`, scp, or an editor), "
        "whether that checkout has uncommitted changes, and whether it's "
        "behind/ahead of origin.  Neutral skip on any machine with no "
        "coord-settings checkout — that is normal everywhere except the "
        "daemon host / operator box.  Read-only, no network required."
    ),
)
@click.option(
    "--capability-rules",
    "capability_rules_check",
    is_flag=True,
    help=(
        "#2953: report dead/partial smoke_tests.capability_rules[].files "
        "prefixes — a prefix that matches no tracked file in any repo "
        "checkout available on this machine (a #1072-shaped stray '**' or "
        "typo), or matches in some repos but not others where the same "
        "directory shape exists deeper in the tree (#2953's own "
        "src/gtk/ vs. <crate>/src/gtk/ shape). Also flags a `requires:` "
        "capability no machine declares at all. Repos with no local "
        "checkout are skipped, never reported as dead. Read-only, no "
        "network required."
    ),
)
@click.option(
    "--test-coverage",
    "test_coverage_check",
    is_flag=True,
    help=(
        "#2967: report repos whose effective Test-stage command (same "
        "ci_command > smoke_tests.default_command > test_command precedence "
        "the Test gate itself uses) enables fewer cargo `--features` than "
        "the repo's build_command — e.g. quadraui building "
        "tui+gtk+terminal but testing only tui, so a `passed` verdict never "
        "compiled the gtk/terminal code. Repos with no explicit "
        "`--features` on either side are silently skipped (no signal). "
        "Read-only, no checkout or network required."
    ),
)
@click.option(
    "--self",
    "self_check",
    is_flag=True,
    help=(
        "#2436: report whether THIS coordinator process's own editable "
        "`coord` install — wherever `coord.__file__` actually resolves, "
        "never an assumed ~/src/<name> path — is behind "
        "origin/<default_branch>.  A merged coord/** fix (other than "
        "agent.py/serve_app.py) is silently inert here until this checkout "
        "is pulled (docs/OPERATING_GOTCHAS.md #1).  Fetches from origin "
        "unless --no-fetch is passed."
    ),
)
@click.option(
    "--no-fetch",
    "self_no_fetch",
    is_flag=True,
    help=(
        "With --self, skip the `git fetch` and compare HEAD against "
        "whatever origin/<default_branch> the last fetch already left "
        "behind."
    ),
)
@click.option(
    "--forge-availability",
    "forge_availability",
    is_flag=True,
    help=(
        "#1896: report forge/CI availability over a trailing window — "
        "uptime %, longest contiguous unavailable stretch, and merge-gate "
        "CI refusal counts by reason — from observations recorded at the "
        "gh/CI/merge-gate seams. Read-only, local audit_log only. Pair "
        "with --window-days to change the window (default 30)."
    ),
)
@click.option(
    "--window-days",
    "forge_availability_window_days",
    default=30.0,
    show_default=True,
    type=float,
    help="With --forge-availability, the trailing window (days) to summarize.",
)


@_CONFIG_OPTION
def diagnose(
    repo: str | None,
    issue: int | None,
    stage: str | None,
    reset: bool,
    dry_run: bool,
    config_path: Path,
    output_json: bool = False,
    orphan_worktrees: bool = False,
    graph_health: bool = False,
    config_provenance_check: bool = False,
    capability_rules_check: bool = False,
    test_coverage_check: bool = False,
    self_check: bool = False,
    self_no_fetch: bool = False,
    forge_availability: bool = False,
    forge_availability_window_days: float = 30.0,
) -> None:
    """Per-stage doctor — diagnose, best-effort recover, optional reset."""
    # ── graphify graph freshness sweep (read-only) ───────────────────────────
    if graph_health:
        _diagnose_graph_health(config_path)
        return

    # ── #1896: forge/CI availability read-out (read-only) ───────────────────
    if forge_availability:
        _diagnose_forge_availability(forge_availability_window_days)
        return

    # ── #1779: fleet coordinator.yml provenance (read-only) ─────────────────
    if config_provenance_check:
        _diagnose_config_provenance()
        return

    # ── #2953: dead/partial capability_rules prefixes (read-only) ───────────
    if capability_rules_check:
        _diagnose_capability_rules(config_path)
        return

    # ── #2967: test_command feature-flag coverage vs build_command ──────────
    if test_coverage_check:
        _diagnose_test_coverage(config_path)
        return

    # ── #2436: THIS process's own editable coord install freshness ──────────
    if self_check:
        _diagnose_self(fetch=not self_no_fetch)
        return

    # ── #618: --orphan-worktrees fleet sweep ─────────────────────────────────
    if orphan_worktrees:
        _diagnose_orphan_worktrees(config_path, dry_run=dry_run)
        return

    if repo is None or issue is None:
        click.echo(
            "error: REPO and ISSUE are required (or pass --orphan-worktrees for a fleet sweep).",
            err=True,
        )
        sys.exit(2)

    # #584: the canonical board + gh + fleet ssh live on the daemon host, so on
    # a thin client this must run there (an empty local board would no-op).
    # COORD_DIAGNOSE_ON_DAEMON guards the daemon against re-routing to itself.
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _svc = daemon_reroute_target("COORD_DIAGNOSE_ON_DAEMON")
    if _svc is not None:
        _diagnose_via_daemon(
            _svc,
            {
                "repo": repo,
                "issue": issue,
                "stage": stage,
                "reset": reset,
                "dry_run": dry_run,
                "output_json": output_json,
            },
        )
        return

    from coord.diagnose import current_stage, diagnose_stage  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    cfg = _load_config(config_path)
    board = build_board()
    resolved_stage = stage or current_stage(board, repo, issue)
    res = diagnose_stage(
        board, cfg, repo, issue, resolved_stage, reset=reset, dry_run=dry_run
    )
    # NOTE: deliberately NO save_board here.  Every diagnose write goes through
    # the authoritative seam (finalize→post_completion, recover→post_result,
    # reconcile→state.update_*), which writes the canonical DB directly.  A
    # save_board would persist the STALE in-memory snapshot (built before those
    # seam writes) and clobber them — e.g. flip a just-finalized phantom back to
    # 'running' (caught live on quadraui #366).

    click.echo(f"diagnose {repo} #{issue} — stage={resolved_stage}"
               + (" [reset]" if reset else "") + (" [dry-run]" if dry_run else ""))
    for f in res.findings:
        click.echo(f"  · {f}")
    for a in res.actions_taken:
        click.echo(f"  ✓ {a}")
    if res.needs_reset and not reset:
        click.echo("  ⚠ still wedged — re-run with --reset to clear the stage "
                   "(keeps the branch + commits).")
    # #935 Part C: emit JSON dict before the trailer when --json is requested.
    # The daemon handler also passes output_json through so remote calls relay it.
    if output_json:
        import json  # noqa: PLC0415
        click.echo("DIAGNOSE_JSON:" + json.dumps(res.to_json_dict()))
    click.echo(res.summary_line())


def _diagnose_graph_health(config_path: Path) -> None:
    """Report graphify graph freshness for this machine's local checkouts.

    Read-only by design.  The graph is a *navigation* aid, so a stale one is a
    warning, not a failure — the point is to make drift visible in a routine
    health check instead of leaving an agent to discover it mid-task.  It also
    checks ``core.hooksPath``, the one-time per-machine setting that decides
    whether worktrees on this box get a linked graph at all.

    #2211: also reports HEAD vs ``origin/<default_branch>``, alongside the
    existing graph-vs-HEAD comparison.  The base checkout is fetched but
    never pulled by design (worktrees always branch from a fresh
    ``origin/<default>``, so a stale base never breaks dispatch) — which
    means a graph can match HEAD exactly while HEAD itself sits arbitrarily
    far behind the remote, and the graph-vs-HEAD check alone reports a clean
    ``✓ in sync`` for it. This never fetches or pulls; it only reads whatever
    ``origin/<default_branch>`` the last fetch left behind.

    Local-machine only, same scope as ``--orphan-worktrees``: it inspects the
    checkouts named in ``coordinator.yml`` that actually exist here.
    """
    from coord.graph_health import (  # noqa: PLC0415
        format_status_lines,
        graph_status,
        hooks_path_status,
    )

    cfg = _load_config(config_path)

    seen: set[Path] = set()
    checkouts: list[tuple[str, Path, str]] = []
    for machine in cfg.machines:
        for repo_cfg in cfg.repos:
            rp = machine.repo_path(repo_cfg.name)
            if not rp:
                continue
            path = Path(rp).expanduser()
            if path in seen or not (path / ".git").exists():
                continue
            seen.add(path)
            checkouts.append((repo_cfg.name, path, repo_cfg.default_branch or "main"))

    if not checkouts:
        click.echo("no local checkouts from coordinator.yml exist on this machine.")
        return

    stale_count = 0
    origin_behind_count = 0
    for repo_name, path, default_branch in checkouts:
        click.echo(f"── {repo_name}")
        # #2211: default_branch drives the HEAD-vs-origin comparison
        # alongside the existing graph-vs-HEAD one — never fetches, only
        # reads whatever `origin/<default_branch>` the last fetch left.
        st = graph_status(path, default_branch)
        for line in format_status_lines(st):
            click.echo(f"  {line}")
        if st.stale:
            stale_count += 1
            click.echo(
                "    fix: run `graphify update .` in the checkout "
                "(the hooks skip rebase/merge/cherry-pick and reset --hard)"
            )
        if st.origin_behind:
            origin_behind_count += 1
            click.echo(
                f"    fix: HEAD is behind origin/{default_branch} — the base "
                "checkout is fetched but never auto-pulled (by design; "
                "checkouts are sometimes deliberately parked). Review and "
                "pull by hand if it isn't."
            )
        ok, detail = hooks_path_status(path)
        click.echo(f"  {'✓' if ok else '⚠'} {detail}")

    click.echo(
        f"GRAPH_HEALTH: checkouts={len(checkouts)} stale={stale_count} "
        f"origin_behind={origin_behind_count}"
    )


def _diagnose_forge_availability(window_days: float) -> None:
    """Report forge/CI availability over the trailing *window_days* (#1896).

    Read-only: summarizes the ``forge_availability``-category rows in the
    local ``audit_log`` (recorded best-effort at the ``gh`` call seam, the
    CI check-fetch seam, and the merge-gate refusal seam — see
    :mod:`coord.forge_availability`). Same family as ``--graph``/
    ``--config-provenance``/``--self``: local-machine only, no network call
    of its own.
    """
    from coord.forge_availability import (  # noqa: PLC0415
        availability_report,
        format_report_lines,
        summary_line as forge_summary_line,
    )

    report = availability_report(window_days=window_days)
    for line in format_report_lines(report):
        click.echo(f"  {line}")
    click.echo(forge_summary_line(report))


def _diagnose_config_provenance() -> None:
    """Report whether THIS machine's live ``coordinator.yml`` is still the
    reviewed one (#1779).

    Read-only, local-machine only — same family as ``--graph`` and
    ``--orphan-worktrees``. Neutral (not a warning) when the coord-settings
    checkout is absent, which is the normal, correct state on every machine
    except the daemon host and the operator box: the checkout is
    deliberately excluded from the fleet's own repo list so a dispatched
    worker can never edit the file governing its own concurrency limits,
    capability routing, and review gates (see ``coord/fleet_config_health.py``
    for the three failure modes this distinguishes).

    Deliberately takes no ``config_path`` — unlike ``--graph``/
    ``--orphan-worktrees`` this does not read ``coordinator.yml`` for a list
    of checkouts to inspect; it inspects one fixed pair of paths
    (``$COORD_CONFIG``/``~/.coord/coordinator.yml`` and
    ``$COORD_SETTINGS_DIR``/``~/src/coord-settings``).
    """
    from coord.fleet_config_health import (  # noqa: PLC0415
        config_provenance,
        format_provenance_lines,
        summary_line,
    )

    prov = config_provenance()
    for line in format_provenance_lines(prov):
        click.echo(f"  {line}")
    click.echo(summary_line(prov))


def _diagnose_capability_rules(config_path: Path) -> None:
    """Report dead/partial ``smoke_tests.capability_rules[].files`` prefixes
    (#2953).

    Read-only, local-machine only — same family as ``--graph``/
    ``--config-provenance``. Unlike ``--config-provenance`` this DOES read
    ``coordinator.yml`` for its repo list (like ``--graph``), because a
    prefix's health is judged against every configured repo's local
    checkout, not one fixed pair of paths. A repo with no local checkout on
    this machine is skipped, never reported as dead — #1779's precedent,
    applied per repo instead of per config file (see
    ``coord/fleet_config_health.py``).
    """
    from coord.fleet_config_health import (  # noqa: PLC0415
        capability_rule_health,
        capability_rule_summary_line,
        format_capability_rule_lines,
        unclaimed_capability_requirements,
    )

    cfg = _load_config(config_path)
    findings = capability_rule_health(cfg)
    unclaimed = unclaimed_capability_requirements(cfg)
    for line in format_capability_rule_lines(findings, unclaimed):
        click.echo(f"  {line}")
    click.echo(capability_rule_summary_line(findings, unclaimed))


def _diagnose_test_coverage(config_path: Path) -> None:
    """Report repos whose effective Test-stage command enables fewer cargo
    ``--features`` than their ``build_command`` (#2967).

    Read-only, local-machine only, same family as ``--capability-rules``.
    Unlike ``--capability-rules`` this needs no repo checkout at all — it's
    a pure comparison of two command strings already in ``coordinator.yml``
    — so it reports identically wherever it's run, including a thin client
    with no local checkouts (see ``coord/fleet_config_health.py``).
    """
    from coord.fleet_config_health import (  # noqa: PLC0415
        feature_coverage_findings,
        feature_coverage_summary_line,
        format_feature_coverage_lines,
    )

    cfg = _load_config(config_path)
    findings = feature_coverage_findings(cfg)
    for line in format_feature_coverage_lines(findings):
        click.echo(f"  {line}")
    click.echo(feature_coverage_summary_line(findings))


def _diagnose_self(*, fetch: bool) -> None:
    """Report whether THIS process's own editable ``coord`` install is
    behind origin (#2436).

    Read-only, local-machine only — same family as ``--graph``/
    ``--config-provenance``/``--orphan-worktrees``. Answers the question
    ``docs/OPERATING_GOTCHAS.md`` #1 names but nothing previously checked:
    "did the `git pull` this editable install depends on for `coord/**`
    fixes to go live actually happen?" A coordinator host running last
    week's code gives zero signal of it otherwise — every board row and
    escalation reads exactly as if a merged fix were still unfixed (the
    #2286/#2426 incident this closes).

    Locates the install via ``coord.self_health.default_install_path()`` —
    ``Path(coord.__file__).resolve().parents[1]``, never an assumed
    ``~/src/<name>`` path (the doc's own assumption had drifted from reality
    on the host that hit this).  Each coordinator/drive-queue host has its
    own independent editable checkout that can drift independently, so this
    is deliberately per-machine, run where it's invoked, exactly like
    ``--graph``.
    """
    from coord.self_health import (  # noqa: PLC0415
        default_install_path,
        format_status_lines,
        self_freshness,
        summary_line as self_summary_line,
    )

    st = self_freshness(install_path=default_install_path(), fetch=fetch)
    for line in format_status_lines(st):
        click.echo(f"  {line}")
    click.echo(self_summary_line(st))


def _diagnose_orphan_worktrees(config_path: Path, *, dry_run: bool) -> None:
    """#618: local fleet sweep — find and prune orphaned coordinator worktrees.

    An orphaned worktree is one under ``~/.coord/worktrees/`` whose
    assignment_id has no live tmux session and no running/pending DB row.
    Dirty worktrees (uncommitted changes) are reported but never deleted.

    #1445: also runs :func:`coord.agent.check_worktree_writable` against
    ``~/.coord/worktrees/`` first and reports a DEGRADED line naming the
    path and (when it's a permission rule at fault) the exact rule/file —
    this machine invariant is otherwise invisible until a dispatched worker
    burns a full session discovering it can't save anything. This check is
    LOCAL-machine only, same as the rest of this sweep; it does not reach
    out to other machines in the fleet.

    #1445 review: ``--dry-run`` means "report findings without writing," so
    the OS-level half of the writability probe — which ``mkdir(parents=True,
    exist_ok=True)``s ``worktrees_dir`` (a real, persistent creation on a
    machine that never had one) and does a write+unlink of a probe file — is
    skipped under ``--dry-run``. The deny-rule scan
    (:func:`coord.agent.find_blocking_deny_rule`) is read-only and still runs.
    On a machine that has never had a worktree, ``--dry-run`` therefore
    reports "does not exist yet" rather than creating it just to say it's
    empty.
    """
    from coord.diagnose import (  # noqa: PLC0415
        _find_orphaned_worktrees,
        _prune_orphaned_worktrees,
    )
    from coord.interactive import (  # noqa: PLC0415
        tmux_available,
        tmux_session_name,
        tmux_session_running,
    )
    from coord.agent import check_worktree_writable, find_blocking_deny_rule  # noqa: PLC0415
    from coord.board_service import read_board  # noqa: PLC0415
    from coord.state import COORD_DIR  # noqa: PLC0415

    cfg = _load_config(config_path)
    board = read_board()
    worktrees_dir = COORD_DIR / "worktrees"

    # #1445: surface a machine that can't write its own worktrees as
    # DEGRADED rather than silently idle-and-ready — this is the same
    # fleet invariant `AgentServer.assign()` now preflights before every
    # dispatch, checked here so an operator (or `scripts/drive-issue.sh`)
    # can catch it ahead of time with a plain `coord diagnose --orphan-worktrees`.
    if dry_run:
        if not worktrees_dir.exists():
            click.echo(
                f"~/.coord/worktrees/ does not exist yet — nothing to sweep "
                f"(dry-run: skipping writability probe to avoid creating it)."
            )
            return
        blocked_by = find_blocking_deny_rule(worktrees_dir)
        if blocked_by is not None:
            click.echo(
                f"⚠ DEGRADED: workers on this machine cannot write to "
                f"{worktrees_dir}: a Claude Code permission rule denies "
                f"Edit/Write under {worktrees_dir}: {blocked_by}"
            )
        else:
            click.echo(
                f"✓ {worktrees_dir} is writable by workers "
                f"(dry-run: OS-level write probe skipped)."
            )
    else:
        write_issue = check_worktree_writable(worktrees_dir)
        if write_issue is not None:
            click.echo(f"⚠ DEGRADED: workers on this machine cannot write to {worktrees_dir}: {write_issue}")
        else:
            click.echo(f"✓ {worktrees_dir} is writable by workers.")

    # Outside --dry-run, check_worktree_writable() just mkdir'd worktrees_dir
    # (parents=True, exist_ok=True) as part of its probe, so it always exists
    # by this point — an empty directory just means no worktrees are
    # currently checked out. Under --dry-run we already returned above when
    # it didn't exist, so it's safe to iterate here too.
    if not any(worktrees_dir.iterdir()):
        click.echo("~/.coord/worktrees/ has no worktrees — nothing to sweep.")
        return

    # Collect all assignment_ids with live tmux sessions.
    # #2541: tmux_session_running (alive AND pane not dead) — remain-on-exit
    # keeps a session's has-session bit True after ANY pane exit (clean
    # success or crash) until a reaper notices, so the bare alive check
    # would count a finished session's worktree as still in use and skip it
    # in this sweep.
    tmux_ok = tmux_available()
    live_tmux: set[str] = set()
    if tmux_ok:
        for entry in worktrees_dir.iterdir():
            if not entry.is_dir():
                continue
            aid = entry.name
            if tmux_session_running(tmux_session_name(aid)):
                live_tmux.add(aid)

    # All running/pending assignment_ids from the board (includes live tmux ones
    # from the board's active set; combine with live_tmux for sessions whose DB
    # rows may already be gone).
    running_ids: set[str] = {
        a.assignment_id
        for a in board.active
        if a.assignment_id
    }
    active_ids = running_ids | live_tmux

    total_removed: list[Path] = []
    total_skipped: list[Path] = []

    for repo in cfg.repos:
        # Find any local checkout for this repo.
        repo_path: Path | None = None
        for machine in cfg.machines:
            rp = machine.repo_path(repo.name)
            if rp:
                candidate = Path(rp).expanduser()
                if candidate.exists():
                    repo_path = candidate
                    break
        if repo_path is None:
            continue

        # Delegate porcelain parsing to the shared helper (branch=None → any branch).
        orphans = _find_orphaned_worktrees(
            repo_path, None, active_assignment_ids=active_ids, worktrees_dir=worktrees_dir
        )
        if not orphans:
            continue

        click.echo(f"{repo.name}: found {len(orphans)} orphaned worktree(s)")
        for wt in orphans:
            click.echo(f"  {wt}")
        if dry_run:
            click.echo(f"  (dry-run) would prune {len(orphans)} worktree(s)")
            total_skipped.extend(orphans)
            continue

        removed, skipped = _prune_orphaned_worktrees(repo_path, orphans)
        for wt in removed:
            click.echo(f"  ✓ removed {wt}")
        for wt in skipped:
            click.echo(f"  ⚠ skipped (uncommitted work) {wt}")
        total_removed.extend(removed)
        total_skipped.extend(skipped)

    click.echo(
        f"orphan-worktrees sweep: {len(total_removed)} removed"
        + (f", {len(total_skipped)} skipped (dirty — inspect manually)" if total_skipped else "")
        + (" [dry-run]" if dry_run else "")
    )


@click.command(help="Show per-assignment and per-model cost breakdown with burn rate.")
@_CONFIG_OPTION
@click.option(
    "--remote",
    is_flag=True,
    help="Fetch cost data from agent servers for assignments without local logs.",
)


@click.option(
    "--timeout",
    default=3.0,
    show_default=True,
    type=float,
    help="Per-machine HTTP timeout for --remote lookups (seconds).",
)
@click.option(
    "--today",
    is_flag=True,
    help="Limit the view to the local calendar day (#1115).",
)
@click.option(
    "--week",
    is_flag=True,
    help="Limit the view to the current ISO week, Monday 00:00 -> next Monday (#1119).",
)
@click.option(
    "--month",
    is_flag=True,
    help="Limit the view to the current calendar month (#1119).",
)
@click.option(
    "--since",
    "since_spec",
    default=None,
    help="Limit the view to legs since <ISO date | Nd | Nh> (#1115).",
)
@click.option(
    "--by-issue",
    "by_issue",
    is_flag=True,
    help="Group daemon-board usage by GitHub issue for the time window, sorted desc (#1115).",
)
@click.option(
    "--issue",
    "issue_number",
    type=int,
    default=None,
    help="Per-stage drill-down for one issue number — all legs, oldest-first (#1115).",
)
@click.option(
    "--by",
    "by_dim",
    type=click.Choice(["repo", "week", "month", "issue"]),
    default=None,
    help="Cross-cut daemon-board usage by dimension: repo (cross-repo rollup), "
    "week/month (time-bucketed spend series), or issue (same as --by-issue) (#1119).",
)
@click.option(
    "--by-time",
    "by_time",
    is_flag=True,
    help="Time-spent view: rank wall-clock by stage-type, or by issue when combined "
    "with --by issue (#1119).",
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["cost", "tokens", "time"]),
    default=None,
    help="Sort order (always descending). Default: cost for rollup views, time for --by-time.",
)
@click.option(
    "--limits",
    "show_limits",
    is_flag=True,
    help="Show the account's Max-plan 5h/weekly usage-window probe (#1466) "
    "instead of the cost breakdown — server-side/account-wide, ~60s cached.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="With --limits, emit the probe as structured JSON instead of text.",
)
def usage(
    config_path: Path,
    remote: bool,
    timeout: float,
    today: bool,
    week: bool,
    month: bool,
    since_spec: str | None,
    by_issue: bool,
    issue_number: int | None,
    by_dim: str | None,
    by_time: bool,
    sort_by: str | None,
    show_limits: bool,
    as_json: bool,
) -> None:
    if as_json and not show_limits:
        raise click.BadParameter(
            "--json only applies to --limits", param_hint="'--json'"
        )
    if show_limits:
        import json as _json

        from coord.usage_limits import format_plan_limits, get_plan_limits

        limits = get_plan_limits()
        if as_json:
            click.echo(_json.dumps(limits.to_dict(), indent=2))
        else:
            click.echo(format_plan_limits(limits))
        return
    if issue_number is not None:
        _usage_issue_drill(config_path, issue_number, today=today, week=week, month=month, since_spec=since_spec)
        return
    if by_time:
        if by_dim in ("repo", "week", "month"):
            raise click.BadParameter(
                f"--by-time only supports --by issue (or no --by); got --by {by_dim}",
                param_hint="'--by'",
            )
        _usage_by_time(
            config_path,
            today=today, week=week, month=month, since_spec=since_spec,
            by_dim=by_dim, sort_by=sort_by,
        )
        return
    if by_issue or by_dim == "issue":
        _usage_by_issue(
            config_path,
            today=today, week=week, month=month, since_spec=since_spec,
            sort_by=_usage_resolve_sort(sort_by, default="cost"),
        )
        return
    if by_dim in ("repo", "week", "month"):
        _usage_by_dim(
            config_path, by_dim,
            today=today, week=week, month=month, since_spec=since_spec,
            sort_by=_usage_resolve_sort(sort_by, default="cost"),
        )
        return

    from coord.board_service import read_board
    from coord.state import load_session
    from coord.usage import build_session_usage, filter_assignments_in_window, format_usage_report

    board = read_board()
    all_assignments = list(board.active) + list(board.completed)

    # Resolve + apply the window flags to the legacy (no --by/--by-time/
    # --by-issue/--issue) view too (#1119 review finding #1) — previously
    # --today/--week/--month/--since were silently ignored here, with no
    # validation (even --week --month together fell through to this branch
    # unchecked). Calling _usage_resolve_window unconditionally means the
    # existing n_set > 1 guard now fires for the bare case as well. Only set
    # window_label when a flag was actually given, so the default
    # (unwindowed) report stays byte-for-byte unchanged.
    window_label: str | None = None
    if today or week or month or since_spec:
        window = _usage_resolve_window(today, week, month, since_spec)
        window_label = window.label
        all_assignments = filter_assignments_in_window(all_assignments, window)

    # Resolve session start time from session.json
    started_at: float | None = None
    sess = load_session()
    if sess and sess.get("started_at"):
        import datetime
        try:
            dt = datetime.datetime.fromisoformat(
                sess["started_at"].rstrip("Z").replace("Z", "+00:00")
            )
            started_at = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        except (ValueError, AttributeError):
            pass

    # Optionally fetch remote cost data for assignments without local logs.
    remote_by_id: dict[str, dict] = {}
    if remote and all_assignments:
        cfg = _load_config(config_path)
        from coord.network import fetch_status

        # Build a map from machine_name → assignments on that machine.
        by_machine: dict[str, list] = {}
        for a in all_assignments:
            if a.assignment_id:
                by_machine.setdefault(a.machine_name, []).append(a)

        for machine in cfg.machines:
            if machine.name not in by_machine:
                continue
            try:
                result = fetch_status(machine, timeout=timeout)
            except Exception:
                continue
            # #2128: fetch_status returns a StatusResult dataclass, never a
            # dict — it has no .get(). A dataclass instance is always
            # truthy (no __bool__), so `if not result` never fired even for
            # an unreachable machine's StatusResult(data=None, error=...);
            # `.ok` (data is not None) is the actual reachability check.
            if not result.ok:
                continue
            data = result.data
            for entry in (data.get("active") or []) + (data.get("completed") or []):
                aid = entry.get("id") or entry.get("assignment_id")
                if aid:
                    remote_by_id[aid] = entry

    session = build_session_usage(
        all_assignments,
        remote_by_id=remote_by_id if remote_by_id else None,
        started_at=started_at,
    )
    click.echo(format_usage_report(session, window_label=window_label))


_SINCE_WEEKS_RE = re.compile(r"^(\d+)\s*w$", re.IGNORECASE)


def _normalize_since_spec(spec: str) -> str:
    """Expand a ``Nw`` (weeks) shorthand to ``(N*7)d`` before handing it to
    Core's ``Window.since`` (#1119 requirement 1 / acceptance example
    ``--since 8w``). Core's ``since`` regex only knows ``Nd``/``Nh`` (see
    ``coord.usage_rollup._SINCE_RELATIVE_RE``) — this is a thin CLI-side
    syntactic convenience, not new window-resolution logic, so it stays here
    rather than in Core. Anything else (ISO date, ``Nd``, ``Nh``) passes
    through unchanged for Core to validate.
    """
    match = _SINCE_WEEKS_RE.match(spec.strip())
    if match:
        return f"{int(match.group(1)) * 7}d"
    return spec


def _usage_resolve_window(today: bool, week: bool, month: bool, since_spec: str | None):
    """Resolve the ``--today``/``--week``/``--month``/``--since`` flags to a
    :class:`coord.usage_rollup.TimeWindow` for the daemon-sourced rollup views
    (#1115/#1119). At most one of the four may be given. None given falls back
    to the current session's start time (open-ended); no session at all falls
    back to an unbounded window.

    ``--week``/``--month`` are resolved via :mod:`coord.usage_rollup`'s own
    ``window_week``/``window_month`` presets — called, not reimplemented,
    per #1119's "consumes Core, no new aggregation logic" scope.
    """
    n_set = sum([bool(today), bool(week), bool(month), bool(since_spec)])
    if n_set > 1:
        raise click.BadParameter(
            "pass at most one of --today, --week, --month, --since",
            param_hint="'--today'/'--week'/'--month'/'--since'",
        )

    from coord.usage_rollup import Window, window_month, window_week

    if today:
        return Window.today()
    if week:
        return window_week()
    if month:
        return window_month()
    if since_spec:
        try:
            window = Window.since(_normalize_since_spec(since_spec))
        except ValueError as e:
            raise click.BadParameter(str(e), param_hint="'--since'") from e
        # Preserve the human-readable spec the user actually typed in the
        # printed label (e.g. "since 8w"), even though it was expanded to
        # "56d" for Core's date math above.
        from dataclasses import replace

        return replace(window, label=f"since {since_spec}")

    from coord.state import load_session

    sess = load_session()
    if sess and sess.get("started_at"):
        import datetime
        try:
            dt = datetime.datetime.fromisoformat(
                sess["started_at"].rstrip("Z").replace("Z", "+00:00")
            )
            started_at = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
            return Window(start=started_at, end=None, label="session")
        except (ValueError, AttributeError):
            pass
    return Window(start=None, end=None, label="all")


def _usage_resolve_sort(sort_by: str | None, *, default: str) -> str:
    """Resolve the ``--sort`` flag: explicit value wins, else *default*
    (#1119 — different views default to a different natural ranking key)."""
    return sort_by or default


def _usage_sort_key(sort_by: str):
    """Key function for sorting an :func:`~coord.usage_rollup.aggregate`
    ``groups`` list by *sort_by* (``cost``/``tokens``/``time``), descending."""
    if sort_by == "tokens":
        def _total_tokens(group: dict) -> int:
            t = group["tokens"]
            return t["input"] + t["output"] + t["cache_read"] + t["cache_creation"]
        return _total_tokens
    if sort_by == "time":
        return lambda group: group["duration_secs"]
    return lambda group: group["cost_total"]


def _usage_by_issue(
    config_path: Path, *, today: bool, week: bool, month: bool, since_spec: str | None, sort_by: str
) -> None:
    """``coord usage --by-issue`` (contract Mock 1, #1115) — daemon-board-
    sourced per-issue cost/token rollup for the resolved time window."""
    from coord.usage import fetch_usage_rows, format_usage_by_issue, pricing_dict_from_config
    from coord.usage_rollup import aggregate

    cfg = _load_config(config_path)
    window = _usage_resolve_window(today, week, month, since_spec)
    rows = fetch_usage_rows()
    pricing = pricing_dict_from_config(cfg.pricing)
    result = aggregate(rows, by="issue", window=window, pricing=pricing)
    result["groups"].sort(key=_usage_sort_key(sort_by), reverse=True)

    click.echo(format_usage_by_issue(result, window.label))


def _usage_issue_drill(
    config_path: Path, issue_number: int, *, today: bool, week: bool, month: bool, since_spec: str | None
) -> None:
    """``coord usage --issue N`` (contract Mock 2, #1115) — per-stage drill
    for one issue's legs. Unbounded (all history) unless --today/--week/
    --month/--since is also given."""
    from coord.usage import fetch_usage_rows, format_usage_issue_drill
    from coord.usage_rollup import leg_in_window, row_issue_number

    cfg = _load_config(config_path)
    has_window_flag = today or week or month or since_spec
    window = _usage_resolve_window(today, week, month, since_spec) if has_window_flag else None
    # #1553: select by the *attributed* issue, matching the `--by issue`
    # summary above. Selecting on the raw `issue_number` while the summary
    # groups on `for_issue_number` would make the two views disagree — the
    # epic's drill would list slice legs the summary had already moved to
    # the child, and the child's drill would be empty.
    rows = [
        row
        for row in fetch_usage_rows()
        if row_issue_number(row) == issue_number
        and (window is None or leg_in_window(row, window))
    ]
    click.echo(format_usage_issue_drill(rows, issue_number, cfg.pricing))


def _usage_by_dim(
    config_path: Path,
    by_dim: str,
    *,
    today: bool,
    week: bool,
    month: bool,
    since_spec: str | None,
    sort_by: str,
) -> None:
    """``coord usage --by repo|week|month`` (#1119) — cross-cut daemon-board
    usage by *by_dim* for the resolved time window. ``repo`` is the
    cross-repo rollup (contract Mock 3); ``week``/``month`` are a
    time-bucketed spend series over a wider window (e.g. ``--since 8w --by
    week``)."""
    from coord.usage import fetch_usage_rows, format_usage_by_group, pricing_dict_from_config
    from coord.usage_rollup import aggregate

    cfg = _load_config(config_path)
    window = _usage_resolve_window(today, week, month, since_spec)
    rows = fetch_usage_rows()
    pricing = pricing_dict_from_config(cfg.pricing)
    result = aggregate(rows, by=by_dim, window=window, pricing=pricing)
    result["groups"].sort(key=_usage_sort_key(sort_by), reverse=True)

    click.echo(format_usage_by_group(result, window.label, by_dim))


def _usage_by_time(
    config_path: Path,
    *,
    today: bool,
    week: bool,
    month: bool,
    since_spec: str | None,
    by_dim: str | None,
    sort_by: str | None,
) -> None:
    """``coord usage --by-time`` (contract Mock 4, #1119) — ranks where
    wall-clock is going: by stage-type (default) or, combined with
    ``--by issue``, by issue. Defaults to ranking by ``time`` (that's the
    point of the view) unless ``--sort`` explicitly overrides."""
    from coord.usage import fetch_usage_rows, format_usage_by_time, pricing_dict_from_config
    from coord.usage_rollup import aggregate

    dim = "issue" if by_dim == "issue" else "stage"
    resolved_sort = _usage_resolve_sort(sort_by, default="time")

    cfg = _load_config(config_path)
    window = _usage_resolve_window(today, week, month, since_spec)
    rows = fetch_usage_rows()
    pricing = pricing_dict_from_config(cfg.pricing)
    result = aggregate(rows, by=dim, window=window, pricing=pricing)
    result["groups"].sort(key=_usage_sort_key(resolved_sort), reverse=True)

    click.echo(format_usage_by_time(result, window.label, dim))
