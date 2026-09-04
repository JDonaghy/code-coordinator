"""`coord/deploy/` must stay byte-identical to the repo-root `deploy/` (#1927).

The unit-drift check (`coord/health/checks/unit_drift.py`) diffs each host's
installed systemd unit against the units *packaged with the installed
release*. Those live under `coord/deploy/` so they ship in the wheel — a
reference that cannot drift with the host's git checkout.

The reviewed source of truth is still the repo-root `deploy/`: it is what
every unit header, doc and provisioning script names, and what the other
`tests/test_deploy_*.py` modules read. The packaged copy is exactly that,
copied. This module is what stops the copy from going stale — the failure
mode is silent and nasty, because a stale packaged unit would make the
release *look* verified while comparing against the wrong file.

Nothing here needs a fleet, a config or a clock: it is a repo-layout
assertion.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "deploy"
PACKAGED_DIR = REPO_ROOT / "coord" / "deploy"

UNIT_GLOBS = ("*.service", "*.timer")

# #2561: units whose ExecStart runs a `coord` subcommand that itself launches
# FURTHER `coord` subprocesses via `coord_argv()` (coord/drive.py) —
# `shutil.which("coord")`, i.e. resolved from *this unit's own* PATH, not a
# login shell's. `coord-notify.service` runs the #2464 out-of-band
# confirmation pass and escalation dispatch; `coord-drive-queue.service`
# launches `coord drive --tmux` / `coord merge --only` from
# coord/commands/drive_queue.py. Both went a release cycle with NO
# `Environment=PATH=` at all, so that resolution silently fell through
# systemd's bare user-manager PATH (no ~/.local/bin, no ~/.cargo/bin) instead
# of the pinned release — a real dispatch got refuted over it (#2561's CC#2556
# incident). `coord-serve.service` and `coord-agent.service` are the other two
# daemons already on this list; they got their PATH fix in earlier issues
# (#1831/#1671) and are included here so this test guards them too, not just
# the two #2561 named.
#
# `coord-web.service`, `coord-db-backup.service`,
# `coord-release-propagate.service` and `coord-release-window.service` are
# DELIBERATELY not in this set — #2561 asked that the other four units get a
# considered "does this need it" rather than an accident, not that this test
# assume the answer. Auditing them is follow-up work, not something to fold in
# here silently.
_COORD_ARGV_SPAWNING_UNITS = frozenset(
    {
        "coord-notify.service",
        "coord-drive-queue.service",
        "coord-serve.service",
        "coord-agent.service",
    }
)

_PATH_LINE_RE = re.compile(r"^Environment\s*=\s*PATH=(.*)$", re.MULTILINE)

# A PATH entry resolving to the pinned release — `~/.local/bin/coord` is a
# symlink onto `~/.coord-venv/bin/coord` (see deploy/coord-serve.service's
# header comment). Either makes `shutil.which("coord")` find the real binary
# instead of coord_argv()'s `python -m coord.cli` fallback.
_RELEASE_MARKERS = ("/.local/bin", "/.coord-venv/bin")


def _units(directory: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for pattern in UNIT_GLOBS:
        for path in directory.glob(pattern):
            out[path.name] = path
    return out


SOURCE_UNITS = _units(SOURCE_DIR)


def _shared_non_unit_files() -> list[str]:
    """Files that exist under BOTH `deploy/` and `coord/deploy/` and are not
    `*.service`/`*.timer` — i.e. everything the parametrized unit-identity
    test below does not already cover.

    `coord/deploy/README.md` says the `*.sh` helpers "are not copied here",
    but `coord-db-backup.sh` *is* (since #2098), and `[tool.setuptools.
    package-data]` ships the whole directory via a `deploy/*` glob — so that
    copy really does go out in the wheel. #3085 found it a release behind:
    the repo-root script had grown the `coord store-backend` refusal and the
    packaged one still had none, so the wheel shipped the exact pre-fix
    script this issue exists to replace. Nothing reads the packaged copy
    today, which is precisely why the drift was silent.
    """
    packaged_units = set(_units(PACKAGED_DIR))
    shared: list[str] = []
    for path in sorted(PACKAGED_DIR.iterdir()):
        if not path.is_file() or path.name in packaged_units:
            continue
        if (SOURCE_DIR / path.name).is_file():
            shared.append(path.name)
    return shared


SHARED_NON_UNIT_FILES = _shared_non_unit_files()


def test_source_deploy_dir_has_units() -> None:
    """Guards the guard: an empty source dir would make every other
    assertion here vacuously true."""
    assert "coord-serve.service" in SOURCE_UNITS
    assert "coord-agent.service" in SOURCE_UNITS


def test_packaged_dir_covers_every_source_unit() -> None:
    missing = sorted(set(SOURCE_UNITS) - set(_units(PACKAGED_DIR)))
    assert not missing, (
        f"deploy/ has units the wheel would not ship: {missing}. "
        "Run: cp deploy/*.service deploy/*.timer coord/deploy/"
    )


def test_packaged_dir_has_no_units_of_its_own() -> None:
    extra = sorted(set(_units(PACKAGED_DIR)) - set(SOURCE_UNITS))
    assert not extra, (
        f"coord/deploy/ carries units deploy/ does not: {extra}. The "
        "repo-root deploy/ is the reviewed source of truth; delete these or "
        "add them there."
    )


@pytest.mark.parametrize("name", sorted(SOURCE_UNITS))
def test_packaged_unit_is_byte_identical(name: str) -> None:
    """A drifted copy is worse than no copy: the check would report a
    confident green against the wrong reference."""
    packaged = PACKAGED_DIR / name
    assert packaged.exists(), f"missing coord/deploy/{name}"
    assert packaged.read_bytes() == (SOURCE_DIR / name).read_bytes(), (
        f"coord/deploy/{name} has drifted from deploy/{name}. deploy/ is the "
        f"source of truth — run: cp deploy/{name} coord/deploy/{name}"
    )


def test_shared_non_unit_files_are_known() -> None:
    """Guards the guard: if the only shared non-unit file ever stops being
    copied, the parametrized test below silently covers nothing."""
    assert "coord-db-backup.sh" in SHARED_NON_UNIT_FILES, (
        "coord/deploy/coord-db-backup.sh is gone. If that removal was "
        "deliberate (coord/deploy/README.md does say *.sh helpers are not "
        "copied here), delete this test with it — but do not leave a "
        "packaged copy around unguarded."
    )


@pytest.mark.parametrize("name", SHARED_NON_UNIT_FILES)
def test_packaged_non_unit_file_is_byte_identical(name: str) -> None:
    """#3085: `coord/deploy/coord-db-backup.sh` sat a release behind
    `deploy/coord-db-backup.sh` because only `*.service`/`*.timer` were
    checked, while `package-data`'s `deploy/*` glob shipped it regardless."""
    assert (PACKAGED_DIR / name).read_bytes() == (SOURCE_DIR / name).read_bytes(), (
        f"coord/deploy/{name} has drifted from deploy/{name}, and the wheel "
        f"ships the stale copy. deploy/ is the source of truth — run: "
        f"cp deploy/{name} coord/deploy/{name}"
    )


def test_pyproject_ships_the_packaged_units() -> None:
    """The copy is only worth having if setuptools puts it in the wheel."""
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    patterns = data["tool"]["setuptools"]["package-data"]["coord"]
    assert any(p.startswith("deploy/") for p in patterns), (
        "coord/deploy/ is not in [tool.setuptools.package-data]; the wheel "
        "would ship no reference units and every host would fall back to its "
        "own unverified checkout (#1927)"
    )


def test_coord_argv_spawning_units_are_known_source_units() -> None:
    """Guards the guard: a typo'd/renamed entry in the set below would make
    every assertion in :func:`test_coord_argv_spawning_unit_declares_path`
    vacuously true for that unit."""
    missing = sorted(_COORD_ARGV_SPAWNING_UNITS - set(SOURCE_UNITS))
    assert not missing, (
        f"_COORD_ARGV_SPAWNING_UNITS names units deploy/ does not have: {missing}"
    )


@pytest.mark.parametrize("name", sorted(_COORD_ARGV_SPAWNING_UNITS))
def test_coord_argv_spawning_unit_declares_path(name: str) -> None:
    """#2561: `coord-notify.service` and `coord-drive-queue.service` carried
    NO `Environment=PATH=` at all, so `coord_argv()`'s `shutil.which("coord")`
    (coord/drive.py) resolved every subprocess they spawn — the #2464
    confirmation pass, escalation dispatch, `coord drive --tmux` — under
    systemd's bare user-manager PATH instead of the pinned release. That
    silent divergence refuted a genuinely clean branch (CC#2556) over a
    PATH-dependent test assertion unrelated to the diff under review.

    This is a byte-existence/marker check, not a byte-identity one: unlike
    `test_packaged_unit_is_byte_identical`, the units in
    `_COORD_ARGV_SPAWNING_UNITS` do NOT all carry the same PATH= value as each
    other (coord-drive-queue.service's #2314 pin ordering is deliberately
    different from coord-serve.service's #1117 repo-venv entry) — only that
    each one HAS a PATH= line, and that line lets `shutil.which("coord")`
    reach the pinned release rather than falling through to it.
    """
    text = (SOURCE_DIR / name).read_text()
    matches = _PATH_LINE_RE.findall(text)
    assert matches, (
        f"deploy/{name} declares no Environment=PATH= — every subprocess it "
        "spawns via coord_argv() falls back to systemd's bare user-manager "
        "PATH, which does not contain ~/.local/bin or ~/.cargo/bin (#2561). "
        "Add one — see deploy/coord-serve.service's line for the reasoning."
    )
    # Only the LAST directive takes effect if the unit repeats the key,
    # mirroring coord.health.checks.unit_drift.find_path_shadow.
    entries = [e for e in matches[-1].split(":") if e]
    has_release_marker = any(
        entry.rstrip("/").endswith(marker)
        for entry in entries
        for marker in _RELEASE_MARKERS
    )
    assert has_release_marker, (
        f"deploy/{name}'s Environment=PATH= ({matches[-1]!r}) contains none of "
        f"{_RELEASE_MARKERS} — shutil.which(\"coord\") in subprocesses this "
        "unit spawns cannot resolve the pinned release (#2561)."
    )
