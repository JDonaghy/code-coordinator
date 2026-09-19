"""#3366: one place that answers "what restarts `coord agent` on this host,
and what does the operator run by hand if it doesn't."

Before this module existed the answer was hardcoded eight separate times as
literal ``systemctl --user restart coord-agent`` text, and the SSH-driven
escalation in :mod:`coord.commands.agent_ops` was a hardcoded systemd call
with no fallback at all. Both were correct for every Linux host in the
fleet and permanently wrong for a macOS one: macOS ships no systemd,
``coord agent`` runs there under **launchd** (``scripts/setup-macmini.sh``,
``docs/MAC_MINI.md``), and the working command is
``launchctl kickstart -k gui/<uid>/<label>`` — which nothing in the
codebase ever suggested. A host that got wedged (#3363) had no restart
channel at all, on either the automated or the human path, and sat six
days and ten releases stale.

This is deliberately the ONLY place that knows the launchd label and the
two command shapes — every caller (the SSH escalation, ``/health``'s
self-report, and every operator-facing remediation string) goes through
:func:`restart_shell_command` / :func:`restart_hint` / :func:`local_supervisor`
so a host can never be told two different things by two call sites that
happen to disagree (see #2085 / the "one question, one answer" rule).
"""

from __future__ import annotations

import os
import sys

#: Label of the per-user launchd job `scripts/setup-macmini.sh` writes.
#: One name, fleet-wide, not per-machine: a launchd job is already scoped
#: to its own host (each mac has its own `~/Library/LaunchAgents`), and
#: this single-operator fleet's setup script always writes the same
#: Label — see `docs/MAC_MINI.md` and the script's `PLIST`/`Label` values.
#: `_escalate_restart` below does not actually need this to be right (it
#: discovers the real label from the plist on the target host at
#: escalation time) — it exists as the last-resort fallback there, and as
#: the value every operator-facing message names.
LAUNCHD_LABEL = "com.jdonaghy.coord-agent"

#: The two supervisor names this module knows about. Any other value
#: (including ``None``) is treated as "unknown" — never silently as
#: systemd, which is exactly the bug this module exists to end.
SYSTEMD = "systemd"
LAUNCHD = "launchd"


def running_under_systemd() -> bool:
    """True when THIS process was started by systemd (a user unit — see
    ``deploy/coord-agent.service``).

    ``INVOCATION_ID`` is set by systemd for every unit invocation since
    v232 — the standard "am I running under systemd" signal, and unlike a
    parent-PID check it survives the process being reparented.
    """
    return bool(os.environ.get("INVOCATION_ID"))


def local_supervisor() -> str | None:
    """Best-effort name of the init system supervising THIS process.

    ``"systemd"`` when :func:`running_under_systemd` says so. ``"launchd"``
    on any other darwin process — macOS ships no systemd at all, and every
    macOS host in this fleet is provisioned via launchd, so there is no
    other supervisor to weigh there. This does not try to tell a genuine
    launchd job apart from a hand-started foreground/tmux session
    (``docs/MAC_MINI.md`` notes the latter is a valid, if unmanaged, way to
    run the agent before a launchd plist exists) — both answer "launchd"
    here because a launchd-shaped restart (or the advice to run one) is
    harmless against a tmux session: it simply does not find a job to
    kick. ``None`` otherwise (Windows, or a Linux host not under systemd —
    a dev box) — callers must treat ``None`` as "unknown", never silently
    as ``"systemd"``.
    """
    if running_under_systemd():
        return SYSTEMD
    if sys.platform == "darwin":
        return LAUNCHD
    return None


def restart_shell_command(supervisor: str | None) -> str:
    """The shell command that force-restarts `coord-agent` under
    *supervisor*.

    ``supervisor == "launchd"`` gets the launchd command; anything else
    (``"systemd"``, ``None``, or an unrecognized value) gets the systemd
    one — the only command that ever existed before this module, so a
    machine with no known supervisor (every config that predates #3366,
    or a probe that couldn't run) sees exactly the pre-#3366 text, not a
    behaviour change.
    """
    if supervisor == LAUNCHD:
        return f"launchctl kickstart -k gui/$(id -u)/{LAUNCHD_LABEL}"
    return "XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-agent"


def restart_hint(supervisor: str | None) -> str:
    """Backtick-quoted, operator-facing form of :func:`restart_shell_command`."""
    return f"`{restart_shell_command(supervisor)}`"


def resolve_supervisor(machine, live: str | None = None) -> str | None:
    """The best-known supervisor for *machine*.

    *live* — a value already read from that host's own ``/health``
    ``"supervisor"`` field, when the caller happens to have one at hand —
    wins: it is the freshest possible truth, the host naming its own
    supervisor right now. Falls back to the static ``coordinator.yml``
    ``supervisor:`` override (``machine.supervisor``, #3366), for callers
    that only have a possibly-unresponsive host to reason about (the whole
    point of an escalation: the live answer is exactly what's missing).
    Never inferred from ``capabilities`` or from matching ``host`` —  a
    launchd host may carry no ``macos`` capability at all (macmini's
    ``[python, rust]``, ``docs/MAC_MINI.md``), so that would be a guess,
    not a fact, and guessing wrong on the last-resort recovery path is
    the exact failure mode #3366 reports.
    """
    if live:
        return live
    return getattr(machine, "supervisor", None)
