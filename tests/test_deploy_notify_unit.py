"""Regression guard for `deploy/coord-notify.service` (#2561 `Environment=PATH`).

Mirrors `tests/test_deploy_drive_queue_unit.py`'s `Environment=PATH=` section
for `coord-notify.service`'s own #2561 fix: this unit carried NO
`Environment=PATH=` at all, so every subprocess it spawns —
`coord_argv()`-resolved escalation dispatch, and the #2464 out-of-band
confirmation pass (`coord.confirm_test`) re-running the repo's own build/test
commands — ran under systemd's bare user-manager PATH instead of the pinned
release. That silent divergence refuted a genuinely clean branch (CC#2556)
over a PATH-dependent test assertion having nothing to do with the diff under
review. See `deploy/coord-notify.service`'s own header comment for the full
story, and `deploy/coord-serve.service`'s for why each PATH entry is there —
this unit's line is byte-identical to that one's (same host, same daemon
family), unlike `coord-drive-queue.service`'s bespoke #2314 ordering.
"""

from __future__ import annotations

import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO_ROOT / "deploy" / "coord-notify.service"
SERVE_UNIT_PATH = REPO_ROOT / "deploy" / "coord-serve.service"


def _parse_unit(path: Path) -> configparser.RawConfigParser:
    # RawConfigParser (not ConfigParser): systemd's `%h`/`%%` specifiers
    # collide with configparser's default `%`-interpolation syntax and raise
    # otherwise (e.g. the `ExecStart=` line below).
    cp = configparser.RawConfigParser(strict=False)
    cp.read(path)
    return cp


def _unit_path_entries(path: Path) -> list[str]:
    """*path*'s `Environment=PATH=` value, split into entries.

    Asserts its presence first: an absent directive is the exact #2561
    regression (the unit then silently inherits the user manager's default),
    and a bare KeyError from configparser would not say so.
    """
    unit = _parse_unit(path)
    assert unit.has_option("Service", "Environment"), (
        f"{path.name} declares no Environment=PATH= — it will inherit the "
        "systemd user manager's default PATH, which has neither "
        "~/.local/bin (the pinned `coord` shim) nor ~/.cargo/bin, so every "
        "coord_argv()-resolved subprocess it spawns silently resolves a "
        "different toolchain than a dev shell would (#2561)."
    )
    value = unit.get("Service", "Environment")
    assert value.startswith("PATH="), value
    return value[len("PATH=") :].split(":")


def test_still_a_oneshot_unit() -> None:
    """The fix's reasoning (systemd's default PATH, not a login shell's)
    assumes `Type=oneshot`. If this ever changes, re-derive rather than
    silently carry the assumption forward."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.get("Service", "Type") == "oneshot"


def test_local_bin_is_on_the_path() -> None:
    """The actual #2561 fix. `~/.local/bin/coord` is the pinned-release
    shim (a symlink onto `~/.coord-venv/bin/coord`); without it on PATH,
    `coord_argv()`'s `shutil.which("coord")` falls back to
    `[sys.executable, "-m", "coord.cli"]`, a different argv shape that
    downstream code (and tests asserting on it) do not expect."""
    assert "%h/.local/bin" in _unit_path_entries(UNIT_PATH)


def test_cargo_bin_is_on_the_path() -> None:
    """rustup installs `cargo` at ~/.cargo/bin and nowhere else — the #2464
    confirmation pass this unit runs re-executes the repo's own
    `build_command`/`test_command`, which is `cargo build`/`cargo test` for a
    `tui/**`-touching branch."""
    assert "%h/.cargo/bin" in _unit_path_entries(UNIT_PATH)


def test_path_is_a_superset_of_the_systemd_user_default() -> None:
    """Declaring an explicit PATH *replaces* the inherited one, so anything
    this unit reaches today via the user manager's default must be carried
    over explicitly or the fix trades one silent breakage for another."""
    entries = set(_unit_path_entries(UNIT_PATH))
    # `systemctl --user show-environment` default, minus the games dirs.
    inherited = [
        "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin",
        "/sbin", "/bin", "/snap/bin",
    ]
    missing = [e for e in inherited if e not in entries]
    assert not missing, f"PATH drops inherited entries: {missing}"


def test_path_has_no_editable_checkout_shadow() -> None:
    """Enforce #1831's rule with #1831's own detector rather than by eye: no
    `.venv/bin` entry may precede ~/.local/bin or ~/.coord-venv/bin, or the
    `unit_drift` health check goes CRIT on this unit the moment it installs."""
    from coord.health.checks.unit_drift import find_path_shadow

    assert find_path_shadow(UNIT_PATH.read_text()) is None


def test_path_matches_coord_serve_byte_for_byte() -> None:
    """#2561 asked for byte-identity with `coord-serve.service`'s line —
    same host, same daemon family, same #1831/#1117 reasoning for each entry.
    Unlike `coord-drive-queue.service` (a deliberately different, #2314-driven
    ordering), there is no reason for this unit's PATH to diverge from
    `coord-serve.service`'s."""
    assert _unit_path_entries(UNIT_PATH) == _unit_path_entries(SERVE_UNIT_PATH)


def test_path_comment_names_the_issue() -> None:
    """Same rationale as `test_deploy_drive_queue_unit.py`'s comment guard —
    the reasoning for why this line must never be dropped stays attached to
    it."""
    text = UNIT_PATH.read_text()
    assert "#2561" in text
    assert "Environment=PATH=" in text


# ── #2572: OnFailure= escalation ─────────────────────────────────────────


def test_on_failure_points_at_the_coord_independent_notifier() -> None:
    """#2572: this unit IS `coord notifier`'s own periodic driver in
    production — the exact channel that was supposed to catch "nobody is
    coming" and was itself dead from the same #2569/#2570 root cause. When
    IT fails, systemd must escalate through a unit with no dependency on
    `coord`/Python/~/.coord-venv.

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
