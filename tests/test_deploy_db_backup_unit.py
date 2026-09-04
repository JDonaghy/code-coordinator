"""Regression guard for `deploy/coord-db-backup.service` (#3085 fix-review
`Environment=PATH`).

Mirrors `tests/test_deploy_drive_queue_unit.py` / `tests/test_deploy_notify_unit.py`'s
`Environment=PATH=` sections for this unit's own gap: it shipped with the
#3085 refusal (`coord-db-backup.sh` now shells out to `coord store-backend`
before touching the filesystem) but carried NO `Environment=PATH=` line at
all — unlike every sibling systemd *user* unit in this directory
(`coord-serve.service`, `coord-notify.service`, `coord-drive-queue.service`,
each fixed for the identical #1831/#2561/#2569 gap). A systemd user unit's
default PATH has neither `~/.local/bin` (where the `coord` shim lives) nor
`~/.coord-venv/bin`, so `command -v coord` inside the script would fail on
every single hourly fire — turning the new safety refusal into a permanent
false negative for the honest `store.backend: sqlite` case, the exact
scenario the issue's acceptance criterion says must be unaffected.
"""

from __future__ import annotations

import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO_ROOT / "deploy" / "coord-db-backup.service"
PACKAGED_UNIT_PATH = REPO_ROOT / "coord" / "deploy" / "coord-db-backup.service"


def _parse_unit(path: Path) -> configparser.RawConfigParser:
    # RawConfigParser (not ConfigParser): systemd's `%h`/`%%` specifiers
    # collide with configparser's default `%`-interpolation syntax and raise
    # otherwise (e.g. the `ExecStart=` line below).
    cp = configparser.RawConfigParser(strict=False)
    cp.read(path)
    return cp


def _unit_path_entries(path: Path) -> list[str]:
    """*path*'s `Environment=PATH=` value, split into entries.

    Asserts its presence first: an absent directive is the exact regression
    (the unit then silently inherits the user manager's default), and a bare
    KeyError from configparser would not say so.
    """
    unit = _parse_unit(path)
    assert unit.has_option("Service", "Environment"), (
        f"{path.name} declares no Environment=PATH= — it will inherit the "
        "systemd user manager's default PATH, which has neither "
        "~/.local/bin (the pinned `coord` shim) nor ~/.coord-venv/bin, so "
        "`coord-db-backup.sh`'s `command -v coord`/`coord store-backend` "
        "call would fail on every fire, even for the honest "
        "store.backend: sqlite case (#3085)."
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
    """The actual fix. `~/.local/bin/coord` is the pinned-release shim; the
    script's `command -v "$COORD_BIN"` / `coord store-backend` call (#3085)
    resolves through it."""
    assert "%h/.local/bin" in _unit_path_entries(UNIT_PATH)


def test_coord_venv_bin_is_on_the_path() -> None:
    """`~/.coord-venv/bin` is where the pinned release itself lives — kept
    on PATH for the same belt-and-suspenders reason as
    `coord-drive-queue.service`'s identical ordering."""
    assert "%h/.coord-venv/bin" in _unit_path_entries(UNIT_PATH)


def test_packaged_copy_matches() -> None:
    """`coord/deploy/coord-db-backup.service` is the wheel-packaged copy
    (#2098); `tests/test_packaged_deploy_units.py` already asserts the two
    are byte-identical, but this pins the specific PATH-carrying line
    directly so a future edit to just one copy fails loudly here too."""
    assert _unit_path_entries(PACKAGED_UNIT_PATH) == _unit_path_entries(UNIT_PATH)
