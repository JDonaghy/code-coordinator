"""Does THIS host resolve to a machine entry in coordinator.yml? (#3440)

**The incident.** macmini's OS short hostname is ``johns-mac-mini``
(``Johns-Mac-mini.lan``) — macOS's default. In ``coordinator.yml`` the
machine is ``name: macmini``, ``host: macmini.tailf46ef8.ts.net``. Neither
matched, so ``coord.config.resolve_local_machine`` returned ``None`` and
every "is this local or remote" call site (``coord assign --interactive``,
the tmux census, the interactive reapers, Test-stage capability checks)
treated macmini as remote — SSHing to itself, which failed with
``Permission denied`` and left no other symptom anywhere. That single
"resolve to None" outcome is otherwise completely silent: nothing short of
tripping over the SSH failure by hand told the operator their local machine
had quietly become unidentifiable.

This check is that missing signal. It re-runs the exact same resolver every
other seam in the codebase calls (#2096's "one question, one answer" — this
probe does not re-derive the match, it asks :func:`coord.config.
resolve_local_machine` the same question and reports the answer) and warns
when it comes back empty *and* there is local evidence this host is
actually meant to be one of the fleet's machines: a repo checkout that
matches a name any configured machine also declares. Absence of a
``coordinator.yml`` (or a config with no machines) is the common thin-client
case and is never a fault, same convention as every other check here.
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check


@check(
    id="local_machine_identity",
    scope="machine",
    title="local machine identity",
    order=52,
    description=(
        "This host resolves to a machine entry in coordinator.yml — the "
        "single answer every local/remote decision in the fleet depends on."
    ),
)
def probe_local_machine_identity(ctx: HealthContext) -> CheckResult:
    from coord.config import resolve_local_machine  # noqa: PLC0415

    config = ctx.config
    machines = list(getattr(config, "machines", None) or [])
    if config is None or not machines:
        return CheckResult(
            check_id="local_machine_identity",
            scope="machine",
            severity=Severity.OK,
            headroom="no coordinator.yml machines configured",
        )

    machine = resolve_local_machine(config)
    if machine is not None:
        return CheckResult(
            check_id="local_machine_identity",
            scope="machine",
            severity=Severity.OK,
            headroom=machine.name,
        )

    # No match at all. Only escalate when there's local evidence this host
    # is genuinely one of the fleet's working machines — a repo checkout
    # whose name some configured machine also declares — so a plain
    # operator laptop with no fleet role stays silent (#3440's own "the
    # common case is never a fault" convention).
    local_repo_names = {c.name for c in ctx.checkouts}
    known_repo_names = {r for m in machines for r in (m.repos or [])}
    overlap = sorted(local_repo_names & known_repo_names)
    if not overlap:
        return CheckResult(
            check_id="local_machine_identity",
            scope="machine",
            severity=Severity.OK,
            headroom="no local repo checkouts overlap a configured machine's repos",
        )

    return CheckResult(
        check_id="local_machine_identity",
        scope="machine",
        severity=Severity.WARN,
        headroom="no configured machine matches this host",
        threshold="warn when this host cannot be matched to any coordinator.yml machine",
        detail=(
            "coordinator.yml has a machine whose repos overlap this host's own "
            f"checkouts ({', '.join(overlap)}) but the alias list, Tailscale "
            "identity, and OS-hostname fallback all failed to match any "
            "machine — every local/remote decision (coord assign "
            "--interactive, sessions, Test-stage dispatch) will treat this "
            "host as remote and may SSH to itself. Add this host's OS short "
            "hostname to the intended machine's `local_hostnames:` in "
            "coordinator.yml, or set $COORD_LOCAL_MACHINE."
        ),
        values={"overlapping_repos": overlap},
    )
