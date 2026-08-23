"""Regression guards for `deploy/coord-failure-notify.service` +
`deploy/coord-failure-notify.sh` (#2572).

#2569/#2570: two guards independently caught the 2026-08-22 outage
(`agent_venv`'s health CRIT, and coord-drive-queue.service's own
well-written self-cordon refusal) and neither reached a human for 11 hours,
because the only "nobody is coming" channel — `coord notifier` — runs from
the exact `~/.coord-venv` that broke. This unit + script pair is the
systemd-native escalation path that does NOT share that failure domain:
`OnFailure=coord-failure-notify.service` on coord-drive-queue.service /
coord-notify.service (asserted in their own test files) starts this the
instant either enters `failed`, and this script's only dependencies are
`systemctl`, `logger`, `wall`, `curl` and (optionally) network reachability
— never `coord`, Python, or a git checkout.

What's pinned here, mirroring the sibling `test_deploy_*_unit.py` files:
the unit shape (oneshot, no [Install], the EnvironmentFile= sourcing an
override-able plain-KEY=VALUE file, ExecStart naming the installed script
location) and the script's own "never coord-dependent, never itself fails"
invariants — not the actual systemd failure-triggering behaviour, which
(like #1830's KillMode fix) is a systemd property pytest cannot exercise.
"""

from __future__ import annotations

import configparser
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO_ROOT / "deploy" / "coord-failure-notify.service"
SCRIPT_PATH = REPO_ROOT / "deploy" / "coord-failure-notify.sh"
PACKAGED_UNIT_PATH = REPO_ROOT / "coord" / "deploy" / "coord-failure-notify.service"


def _parse_unit(path: Path) -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser(strict=False)
    cp.read(path)
    return cp


def test_unit_and_script_exist() -> None:
    assert UNIT_PATH.exists(), "deploy/coord-failure-notify.service is missing"
    assert SCRIPT_PATH.exists(), "deploy/coord-failure-notify.sh is missing"


def test_script_is_executable() -> None:
    mode = SCRIPT_PATH.stat().st_mode
    assert mode & stat.S_IXUSR, "coord-failure-notify.sh must be committed +x (chmod +x)"


def test_is_a_oneshot_unit_with_no_install_section() -> None:
    """Never `enable`d directly — only ever started via another unit's
    `OnFailure=`, so it must not carry a [Install] section (which would
    invite `systemctl --user enable` on it, a harmless but misleading
    no-op)."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.get("Service", "Type") == "oneshot"
    assert not unit.has_section("Install")


def test_exec_start_runs_the_installed_script_not_a_checkout_path() -> None:
    """Must resolve to a stable, non-checkout location (`~/.local/bin/`,
    mirroring coord-db-backup.service/coord-web-dist-build.service) — never
    a path inside a git worktree, which is exactly the class of thing that
    can vanish out from under a running daemon (#2569's deleted worktree)."""
    unit = _parse_unit(UNIT_PATH)
    exec_start = unit.get("Service", "ExecStart")
    assert exec_start == "%h/.local/bin/coord-failure-notify.sh"


def test_environment_file_is_optional_and_outside_any_checkout() -> None:
    """The leading `-` makes a missing env file non-fatal (systemd's own
    syntax) — this script must still run (degraded to logger/wall only, per
    its own header) with no config present at all."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.has_option("Service", "EnvironmentFile")
    value = unit.get("Service", "EnvironmentFile")
    assert value.startswith("-"), (
        f"EnvironmentFile={value!r} must be prefixed '-' so a missing file "
        "does not fail this unit"
    )
    assert "coordinator.yml" not in value, (
        "must be a plain KEY=VALUE file, never coordinator.yml — reading "
        "that needs coord's own YAML parser, the exact dependency this unit "
        "exists to route around"
    )


def test_never_depends_on_anything_coord_related() -> None:
    """This unit's entire reason to exist is to still fire when `coord`
    (and whatever it depends on) is broken — an `After=`/`Requires=` on
    coord-agent.service or similar would reintroduce that coupling."""
    text = UNIT_PATH.read_text()
    for directive in ("After=coord", "Requires=coord", "Wants=coord", "BindsTo=coord"):
        assert directive not in text, f"{directive} reintroduces a coord dependency"


def test_has_no_on_failure_of_its_own() -> None:
    """Must not point `OnFailure=` at itself (or anything else) — this is
    the last-resort leaf of the chain; it terminates here."""
    unit = _parse_unit(UNIT_PATH)
    assert not unit.has_option("Unit", "OnFailure")


def test_bounded_timeout() -> None:
    """A wedged run here (e.g. curl hanging past its own -m) must not stay
    a job forever — it is the alarm, and an alarm that can hang is worse
    than one that fails fast."""
    unit = _parse_unit(UNIT_PATH)
    assert unit.has_option("Service", "TimeoutStartSec")


def test_packaged_copy_matches() -> None:
    """Mirrors `tests/test_packaged_deploy_units.py`'s byte-identity check —
    called out explicitly here too since a missing packaged copy is exactly
    the kind of thing a `.service`-only diff review misses."""
    assert PACKAGED_UNIT_PATH.exists(), "coord/deploy/coord-failure-notify.service is missing"
    assert PACKAGED_UNIT_PATH.read_bytes() == UNIT_PATH.read_bytes()


# ── the script itself ────────────────────────────────────────────────────


def _code_lines(text: str) -> str:
    """*text* with comment-only lines dropped — the header extensively
    (and deliberately) DISCUSSES `coord`/`~/.coord-venv` as the thing this
    script avoids depending on, so a raw substring scan over the whole file
    would trip on its own explanation. Only the executable lines matter
    here."""
    return "\n".join(
        line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
    )


def test_script_has_no_coord_or_python_dependency() -> None:
    code = _code_lines(SCRIPT_PATH.read_text()).lower()
    assert "import coord" not in code
    assert "coord.cli" not in code
    assert ".coord-venv" not in code
    assert "coordinator.yml" not in code
    first_line = SCRIPT_PATH.read_text().splitlines()[0]
    assert "python" not in first_line
    assert "coord" not in first_line
    assert "sh" in first_line


def test_script_always_exits_zero() -> None:
    """The last-resort channel must never itself become `failed` — that
    would either loop (if it ever got an OnFailure= of its own, which
    `test_has_no_on_failure_of_its_own` forbids) or just silently eat the
    escalation. `set -uo pipefail` (not `-e`) plus an explicit trailing
    `exit 0` is the actual contract; this pins the trailing exit."""
    text = SCRIPT_PATH.read_text().rstrip()
    assert text.splitlines()[-1].strip() == "exit 0"
    assert "set -e" not in text.replace("set -uo pipefail", "")


def test_script_names_the_issue() -> None:
    text = SCRIPT_PATH.read_text()
    assert "#2572" in text
