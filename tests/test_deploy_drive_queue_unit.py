"""Regression guards for `deploy/coord-drive-queue.service` (#1830 `KillMode`,
#2532 `Environment=PATH`).

This is a systemd *property*: whether a cgroup gets torn down when a
`Type=oneshot` unit finishes is enforced by the init system, not by any code
this repo runs, so there is no way to exercise the actual failure mode
(tick's own `tmux new-session` spawning a server that then dies with the
unit's cgroup) from pytest. See docs/DRIVE_QUEUE.md §2a for the honest,
systemd-level verification procedure (and why it can only be done with no
tmux server already running — an attended check cannot reproduce the bug).

What *can* be pinned here is the one line the fix actually consists of:
`KillMode=process` on the `[Service]` section of the shipped unit file. Losing
that line silently reopens #1830 on the next `deploy/` install, and nothing
else in the test suite would notice — this file is that notice.

#2532 adds the same kind of guard for `Environment=PATH=`. The tick launches
`coord drive`, which runs the oracle-loop trust gate (`coord acceptance
record`) on this host, which shells out to the repo's acceptance driver
command — `cargo test ...` for every `tui-tuidriver` route. Without an
explicit PATH the unit inherits the systemd *user manager's* default, which
has no `~/.cargo/bin`, so that command exits 127 `cargo: not found` with empty
stdout and the gate records a *false red* verdict (total=0, empty reason)
against a branch whose sealed suite was green. Same shape as the KillMode bug:
unobservable from pytest at the systemd level, but the one line the fix
consists of can be pinned here.
"""

from __future__ import annotations

import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO_ROOT / "deploy" / "coord-drive-queue.service"


def _parse_unit(path: Path) -> configparser.RawConfigParser:
    # RawConfigParser (not ConfigParser): systemd's `%h`/`%%` specifiers
    # collide with configparser's default `%`-interpolation syntax and raise
    # otherwise (e.g. the `ExecStart=` line below).
    cp = configparser.RawConfigParser(strict=False)
    cp.read(path)
    return cp


def test_killmode_process_is_set() -> None:
    """The actual #1830 fix. Without it, systemd's default
    KillMode=control-group reaps any tmux server the tick's own
    `coord drive --tmux` had to spawn, the instant the oneshot tick exits —
    invisibly, whenever a tmux server already existed (the attended case)."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.get("Service", "KillMode") == "process"


def test_still_a_oneshot_unit() -> None:
    """The fix assumes `Type=oneshot` (a short-lived launcher whose own exit
    must not take its children down). If this ever changes, the KillMode
    reasoning above needs re-deriving, not silently carrying forward."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.get("Service", "Type") == "oneshot"


def test_killmode_comment_names_the_issue() -> None:
    """Loose guard against a future edit deleting the explanatory comment
    along with (or instead of) the setting — the reasoning for why this line
    must never be removed needs to stay attached to it."""
    text = UNIT_PATH.read_text()
    assert "#1830" in text
    assert "KillMode=process" in text


# ── #2532: Environment=PATH ──────────────────────────────────────────────


def _unit_path_entries() -> list[str]:
    """The packaged unit's `Environment=PATH=` value, split into entries.

    Asserts its presence first: an absent directive is the exact #2532
    regression (the unit then silently inherits the user manager's default),
    and a bare KeyError from configparser would not say so.
    """
    unit = _parse_unit(UNIT_PATH)
    assert unit.has_option("Service", "Environment"), (
        "coord-drive-queue.service declares no Environment=PATH= — it will "
        "inherit the systemd user manager's default PATH, which has no "
        "~/.cargo/bin, and every cargo-based acceptance driver the tick's "
        "trust gate shells out to will exit 127 as a false red (#2532)."
    )
    value = unit.get("Service", "Environment")
    assert value.startswith("PATH="), value
    return value[len("PATH=") :].split(":")


def test_cargo_bin_is_on_the_path() -> None:
    """The actual #2532 fix. rustup installs `cargo` at ~/.cargo/bin and
    nowhere else, so a trust-gate run of a `tui-tuidriver` acceptance driver
    cannot find it without this entry."""
    assert "%h/.cargo/bin" in _unit_path_entries()


def test_path_is_a_superset_of_the_systemd_user_default() -> None:
    """Declaring an explicit PATH *replaces* the inherited one, so anything
    the tick reaches today via the user manager's default (tmux and git for
    `coord drive --tmux`, gh, snaps) must be carried over explicitly or this
    fix trades one silent breakage for another."""
    entries = set(_unit_path_entries())
    # `systemctl --user show-environment` default, minus the games dirs.
    inherited = [
        "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin",
        "/sbin", "/bin", "/snap/bin",
    ]
    missing = [e for e in inherited if e not in entries]
    assert not missing, f"PATH drops inherited entries: {missing}"


def test_pinned_release_precedes_the_other_entry_points() -> None:
    """#1523/#2314: the tick's own CLI must stay the pinned, non-editable
    install. ExecStart already names it absolutely; keeping ~/.coord-venv/bin
    first means every `shutil.which("coord")` the tick's children make
    (coord_argv(), coord/drive.py) resolves there too."""
    entries = _unit_path_entries()
    assert entries[0] == "%h/.coord-venv/bin"
    assert entries.index("%h/.coord-venv/bin") < entries.index("%h/.local/bin")


def test_path_has_no_editable_checkout_shadow() -> None:
    """Enforce #1831's rule with #1831's own detector rather than by eye: no
    `.venv/bin` entry may precede ~/.local/bin or ~/.coord-venv/bin, or the
    `unit_drift` health check goes CRIT on this unit the moment it installs."""
    from coord.health.checks.unit_drift import find_path_shadow

    assert find_path_shadow(UNIT_PATH.read_text()) is None


def test_path_comment_names_the_issue() -> None:
    """Same rationale as the KillMode comment guard above — the reasoning for
    why this line must never be dropped stays attached to it."""
    text = UNIT_PATH.read_text()
    assert "#2532" in text
    assert "Environment=PATH=" in text


# ── #2572: OnFailure= escalation ─────────────────────────────────────────


def test_on_failure_points_at_the_coord_independent_notifier() -> None:
    """The actual #2572 fix for this unit: when it enters `failed` (e.g. an
    unreadable board aborting the tick), systemd itself must start a
    notifier that has no `coord`/Python/~/.coord-venv dependency — the
    #2569/#2570 incident is exactly the case where all three were the thing
    that broke, taking `coord notifier` down with them.

    #2580 adds a second `OnFailure=` target, coord-fleet-watchdog.service —
    the repair half of the same story, alongside coord-failure-notify.
    service's page-a-human half. `OnFailure=` is a list-type directive, so
    both are named on the one line rather than replacing each other."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.get("Unit", "OnFailure") == "coord-failure-notify.service coord-fleet-watchdog.service"


def test_on_failure_comment_names_the_issue() -> None:
    text = UNIT_PATH.read_text()
    assert "#2572" in text
    assert "#2580" in text
    assert "OnFailure=coord-failure-notify.service coord-fleet-watchdog.service" in text
