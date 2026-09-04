"""Behavioural tests for deploy/coord-db-backup.sh (#2098).

Every other deploy/*.sh script has a dedicated test that drives it for real
via `subprocess.run` against scratch directories -- see
`tests/test_deploy_coord_web_rollback.py`'s own docstring ("exercised for
real under pytest via subprocess... independent of the manual drill
transcript"), and `tests/test_deploy_coord_web_dist.py`. This script had none
despite being the single most safety-critical piece of #2098: the mountpoint
guard is the only thing standing between an hourly timer and it overwriting
its own protection with garbage the moment the external SSD is unplugged.

The script is already parameterized via COORD_DB / COORD_BACKUP_DIR /
COORD_BACKUP_RETAIN env vars specifically so it can be driven like this --
see its own header comment.

Two seams are faked with test doubles placed first on $PATH, both narrowly
scoped and each justified on its own:

- `mountpoint`: a real bind-mount needs root, and `tmp_path` itself may or
  may not sit on a mount boundary depending on the host (e.g. tmpfs `/tmp`),
  which would make a real-`mountpoint`-based "refuses" test flaky across
  environments. The fake's result is controlled by $FAKE_MOUNTPOINT_EXIT so
  both the mounted and not-mounted branches are deterministic.
- `sqlite3` (one test only, `test_rejects_a_corrupt_snapshot_as_REJECTED`):
  `VACUUM INTO` rebuilds a fresh, well-formed file from whatever it can
  still read, so a genuinely corrupt *source* db almost always fails
  `VACUUM INTO` itself (see `test_refuses_when_vacuum_into_fails`) rather
  than producing a corrupt *output* that passes `VACUUM INTO` but fails the
  subsequent `integrity_check` -- the real-world case this branch guards
  against is I/O corruption on the destination between the two calls, which
  isn't reproducible by feeding the script a bad source. Every other test in
  this file uses the real `sqlite3` binary.

#2170 -- THE `sqlite3` CLI IS A HARD, UNPROVISIONED DEPENDENCY OF THIS WHOLE
FILE, AND IS SKIP-GUARDED.

The paragraph above used to say the dependency was "one test only". That was
wrong, and the wrongness was invisible in CI: `deploy/coord-db-backup.sh`
shells out to `sqlite3` on *every* path that gets as far as taking a
snapshot, so four of these tests die with `sqlite3: command not found` on a
machine that doesn't have the binary. `sqlite3` is not a provisioned fleet
dependency (absent on `precision`; present on `elitebook` only incidentally
via the Android SDK's `platform-tools/sqlite3`), and `pyproject.toml`'s
`[dev]` extras cannot install a system binary -- so there is no config change
that makes this file runnable everywhere.

Hence the module-level `pytestmark` below: `shutil.which("sqlite3")` or the
file skips wholesale, with a reason that names the missing binary. A skip is
the right verdict rather than a red suite because the thing being tested is
a *shell script's* behaviour when the binary is present -- on a machine
without it, this file has nothing to say, and saying nothing must not look
like "the branch broke the backup script". The stdlib `sqlite3` *module* is
always available, so the guard keys on the CLI only.

#3085 -- A THIRD SEAM: `coord` ITSELF, ON A REAL $PATH.

The refusal tests below (`test_refuses_when_backend_is_*`,
`test_refuses_when_coord_command_is_unavailable`) exercise the script's new
first move -- asking `coord store-backend` (coord.db.resolve_store_backend(),
#3084) which storage engine is actually configured, and refusing before
touching the filesystem when the answer isn't sqlite. Unlike `mountpoint` and
`sqlite3` above, `coord` itself is NOT faked: it is the real, installed
`coord` command (this repo's own dev/CI venv provides it), driven through a
real `coordinator.yml` fixture -- there is no seam to fake, `coord
store-backend` is the thing under test. `_run`'s `coord_config` /
`extra_env` (for `COORD_BIN`) parameters exist for exactly this. Every
existing (pre-#3085) test in this file, and any new one that doesn't pass
`coord_config`, gets `$COORD_CONFIG` pinned to a path that never exists --
see `_run`'s own comment -- so "no store: block" resolves to sqlite
deterministically, never from whatever happens to be sitting in the real
`$HOME` of the machine running the suite (the exact ambient-leak class
`tests/test_ambient_home_isolation.py` exists to catch, one paragraph above).
These new tests need no `sqlite3` CLI at all -- the backend refusal exits
before the script's first `sqlite3` invocation -- but they still fall under
the file's module-wide skip for consistency with the rest of this file.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = REPO_ROOT / "deploy" / "coord-db-backup.sh"

#: The real `sqlite3` CLI, resolved once. `None` on a machine without it --
#: see the "#2170" section of the module docstring.
SQLITE3_CLI = shutil.which("sqlite3")

pytestmark = pytest.mark.skipif(
    SQLITE3_CLI is None,
    reason=(
        "the sqlite3 CLI is not on $PATH -- deploy/coord-db-backup.sh shells "
        "out to it on every snapshot path, so this whole file needs it and no "
        "[dev] extra can install a system binary (#2170)"
    ),
)


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _fake_mountpoint(fakebin: Path) -> None:
    _write_executable(
        fakebin / "mountpoint",
        "#!/usr/bin/env bash\n"
        'exit "${FAKE_MOUNTPOINT_EXIT:-1}"\n',
    )


def _make_source_db(path: Path, *, rows: int = 3, with_assignments_table: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    try:
        if with_assignments_table:
            conn.execute("CREATE TABLE assignments (id INTEGER PRIMARY KEY)")
            for i in range(rows):
                conn.execute("INSERT INTO assignments (id) VALUES (?)", (i,))
        else:
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
    finally:
        conn.close()


def _run(
    tmp_path: Path,
    *,
    src: Path,
    dest_dir: Path,
    retain: int | None = None,
    mounted: bool = True,
    extra_path: Path | None = None,
    coord_config: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    _fake_mountpoint(fakebin)

    env = dict(os.environ)
    path_entries = [str(fakebin)]
    if extra_path is not None:
        path_entries.append(str(extra_path))
    # Running the venv's interpreter directly (`.venv/bin/python -m pytest`,
    # as scripts/coord-test-runner.sh does) does NOT put `.venv/bin` on
    # $PATH -- only `activate` does -- even though that's exactly where this
    # venv's `coord` console script lives. Resolve it from the running
    # interpreter rather than trusting the ambient $PATH, so `coord
    # store-backend` (the #3085 guard, the script's first move) can find the
    # real binary either way. Appended after fakebin/extra_path but before
    # the ambient $PATH so an activated-venv or system-install environment,
    # where ambient $PATH already resolves `coord` correctly, is unaffected.
    path_entries.append(str(Path(sys.executable).parent))
    path_entries.append(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join(path_entries)
    env["FAKE_MOUNTPOINT_EXIT"] = "0" if mounted else "1"
    env["COORD_DB"] = str(src)
    env["COORD_BACKUP_DIR"] = str(dest_dir)
    if retain is not None:
        env["COORD_BACKUP_RETAIN"] = str(retain)
    else:
        env.pop("COORD_BACKUP_RETAIN", None)
    # #3085: the script now shells out to `coord store-backend`, which reads
    # $COORD_CONFIG (falling back to ~/.coord/coordinator.yml, then
    # ./coordinator.yml) -- exactly the ambient-state leak
    # tests/test_ambient_home_isolation.py exists to catch. Pin it to a path
    # that never exists by default, so every existing test in this file keeps
    # exercising the documented "absent store: block == sqlite" fail-open
    # behaviour deterministically, regardless of what happens to be sitting
    # in the real $HOME or $COORD_CONFIG of the machine running the suite.
    env["COORD_CONFIG"] = str(coord_config) if coord_config is not None else str(tmp_path / "no-such-coordinator.yml")
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _snapshots(dest_dir: Path) -> list[Path]:
    return sorted(p for p in dest_dir.glob("coord.db.2*") if not p.name.endswith(".REJECTED"))


def test_refuses_to_run_when_ssd_not_mounted(tmp_path: Path) -> None:
    """The mountpoint guard the issue calls out by name: a plain directory
    standing in for an unmounted external SSD must refuse the run, not
    silently write "backups" onto the disk it exists to protect."""
    src = tmp_path / "coord.db"
    _make_source_db(src)
    dest_dir = tmp_path / "media" / "crucial" / "coord-backups"

    result = _run(tmp_path, src=src, dest_dir=dest_dir, mounted=False)

    assert result.returncode != 0
    assert "not a mountpoint" in result.stderr
    assert not dest_dir.exists() or not list(dest_dir.glob("coord.db.2*"))


def test_refuses_when_source_db_missing(tmp_path: Path) -> None:
    src = tmp_path / "coord.db"  # never created
    dest_dir = tmp_path / "backups"

    result = _run(tmp_path, src=src, dest_dir=dest_dir, mounted=True)

    assert result.returncode != 0
    assert "source db not found" in result.stderr


def test_successful_snapshot_matches_source_row_count(tmp_path: Path) -> None:
    """The acceptance criterion from the issue: snapshot row counts must
    match the live db exactly. VACUUM INTO, integrity_check and the
    assignments sanity check all run for real here against real sqlite3."""
    src = tmp_path / "coord.db"
    _make_source_db(src, rows=5)
    dest_dir = tmp_path / "backups"

    result = _run(tmp_path, src=src, dest_dir=dest_dir, mounted=True)

    assert result.returncode == 0, result.stderr
    snapshots = _snapshots(dest_dir)
    assert len(snapshots) == 1
    latest = dest_dir / "coord.db.latest"
    assert latest.is_symlink()
    assert latest.resolve() == snapshots[0].resolve()

    conn = sqlite3.connect(str(snapshots[0]))
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM assignments").fetchone()
    finally:
        conn.close()
    assert count == 5
    assert "5 assignments" in result.stdout


def test_refuses_when_vacuum_into_fails(tmp_path: Path) -> None:
    """A source that exists but isn't a real sqlite database (the
    genuinely-corrupt-source case) must fail VACUUM INTO itself, cleanly,
    rather than produce anything downstream."""
    src = tmp_path / "coord.db"
    src.write_text("this is not a sqlite database\n")
    dest_dir = tmp_path / "backups"

    result = _run(tmp_path, src=src, dest_dir=dest_dir, mounted=True)

    assert result.returncode != 0
    assert "VACUUM INTO failed" in result.stderr
    assert not list(dest_dir.glob("coord.db.2*")) if dest_dir.exists() else True


def test_rejects_a_corrupt_snapshot_as_REJECTED(tmp_path: Path) -> None:
    """The integrity-check-and-.REJECTED path: when the snapshot fails
    PRAGMA integrity_check after VACUUM INTO reports success, the script
    must keep the bad file (renamed .REJECTED, not deleted) and fail loudly
    rather than let a corrupt file masquerade as a good backup.

    VACUUM INTO rebuilds a fresh well-formed file from whatever it can
    read, so this specific ("vacuum succeeded, output still corrupt") case
    isn't reachable by corrupting the source -- see the module docstring.
    A fake `sqlite3` on $PATH simulates it directly: it fakes only the two
    calls this path makes (VACUUM INTO, integrity_check) and would delegate
    anything else to the real binary.
    """
    fake_sqlite_dir = tmp_path / "fake-sqlite"
    fake_sqlite_dir.mkdir()
    _write_executable(
        fake_sqlite_dir / "sqlite3",
        "#!/usr/bin/env bash\n"
        "db=\"$1\"\n"
        "sql=\"$2\"\n"
        "case \"$sql\" in\n"
        "  VACUUM\\ INTO*)\n"
        "    out=\"${sql#*\\'}\"\n"
        "    out=\"${out%\\'*}\"\n"
        "    : > \"$out\"\n"
        "    ;;\n"
        "  *integrity_check*)\n"
        "    echo malformed\n"
        "    ;;\n"
        "  *)\n"
        # Resolved via shutil.which rather than hardcoded to /usr/bin/sqlite3:
        # the binary is not fleet-provisioned, so where it lives varies per
        # machine (#2170) -- and it can't be spelled bare `sqlite3` here
        # because this fake is itself on $PATH and would exec itself.
        f"    exec {SQLITE3_CLI} \"$@\"\n"
        "    ;;\n"
        "esac\n",
    )

    src = tmp_path / "coord.db"
    _make_source_db(src)
    dest_dir = tmp_path / "backups"

    result = _run(tmp_path, src=src, dest_dir=dest_dir, mounted=True, extra_path=fake_sqlite_dir)

    assert result.returncode != 0
    assert "integrity_check on snapshot" in result.stderr
    rejected = list(dest_dir.glob("coord.db.*.REJECTED"))
    assert len(rejected) == 1
    assert not _snapshots(dest_dir)
    assert not (dest_dir / "coord.db.latest").exists()


def test_refuses_when_assignments_table_missing(tmp_path: Path) -> None:
    """Same .REJECTED contract as the integrity_check failure above: a
    snapshot that fails the assignments sanity check must not survive under
    its normal name (that would leave it indistinguishable from a good
    backup to both an operator and retention pruning)."""
    src = tmp_path / "coord.db"
    _make_source_db(src, with_assignments_table=False)
    dest_dir = tmp_path / "backups"

    result = _run(tmp_path, src=src, dest_dir=dest_dir, mounted=True)

    assert result.returncode != 0
    assert "no assignments table" in result.stderr
    assert not _snapshots(dest_dir)
    rejected = list(dest_dir.glob("coord.db.*.REJECTED"))
    assert len(rejected) == 1
    assert not (dest_dir / "coord.db.latest").exists()


def test_refuses_when_assignments_table_empty(tmp_path: Path) -> None:
    """Guards against an empty file that happens to pass integrity_check --
    zero assignments must not be allowed to count as a backup, and (like the
    other rejection paths) must not survive under its normal, unmarked
    name."""
    src = tmp_path / "coord.db"
    _make_source_db(src, rows=0)
    dest_dir = tmp_path / "backups"

    result = _run(tmp_path, src=src, dest_dir=dest_dir, mounted=True)

    assert result.returncode != 0
    assert "0 assignments" in result.stderr or "refusing to count" in result.stderr
    assert not _snapshots(dest_dir)
    rejected = list(dest_dir.glob("coord.db.*.REJECTED"))
    assert len(rejected) == 1
    assert not (dest_dir / "coord.db.latest").exists()


def test_retention_prunes_oldest_beyond_RETAIN_and_never_touches_REJECTED(tmp_path: Path) -> None:
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir(parents=True)
    old_stamps = [
        "20200101T000000Z",
        "20200102T000000Z",
        "20200103T000000Z",
        "20200104T000000Z",
        "20200105T000000Z",
    ]
    for stamp in old_stamps:
        (dest_dir / f"coord.db.{stamp}").write_text("dummy old snapshot")
    rejected = dest_dir / "coord.db.20200101T000000Z-oops.REJECTED"
    rejected.write_text("must never be pruned")

    src = tmp_path / "coord.db"
    _make_source_db(src, rows=2)

    result = _run(tmp_path, src=src, dest_dir=dest_dir, retain=3, mounted=True)

    assert result.returncode == 0, result.stderr
    remaining = _snapshots(dest_dir)
    assert len(remaining) == 3
    # The 3 kept must be the 3 newest: the two youngest fakes plus the
    # brand-new real snapshot this run just wrote (its ISO-8601 stamp sorts
    # after every 2020 fake).
    assert remaining[0].name == "coord.db.20200104T000000Z"
    assert remaining[1].name == "coord.db.20200105T000000Z"
    assert remaining[2].name not in old_stamps
    # .REJECTED is never touched by pruning, regardless of RETAIN.
    assert rejected.exists()
    assert "3 snapshots retained" in result.stdout


# ── #3085: refuse a dead SQLite file after a Postgres cutover ──────────────


def _write_store_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(f"repos: []\nmachines: []\n{body}")
    return p


def test_refuses_when_backend_is_postgres_and_writes_no_snapshot(tmp_path: Path) -> None:
    """The issue's own refusal-path smoke test, driven for real: with a
    postgres `store:` block, the script must exit non-zero, name the backend
    and #1822, and leave the backup directory empty -- even though the
    SQLite source file is perfectly healthy (VACUUM INTO, integrity_check
    and the assignments check would all happily pass against it, which is
    exactly the false-green failure this issue is about)."""
    cfg = _write_store_config(
        tmp_path,
        "store:\n  backend: postgres\n  dsn: postgresql://user:pass@dbhost:5432/coord\n",
    )
    src = tmp_path / "coord.db"
    _make_source_db(src, rows=5)
    dest_dir = tmp_path / "backups"

    result = _run(tmp_path, src=src, dest_dir=dest_dir, mounted=True, coord_config=cfg)

    assert result.returncode != 0
    assert "postgres" in result.stderr
    assert "1822" in result.stderr
    assert not dest_dir.exists() or not list(dest_dir.iterdir())


def test_backend_refusal_takes_priority_over_the_mountpoint_check(tmp_path: Path) -> None:
    """The backend check runs first, before any filesystem guard -- a
    Postgres-configured host must get the backend message even when the SSD
    also happens to be unplugged, not a misleading mountpoint complaint."""
    cfg = _write_store_config(
        tmp_path,
        "store:\n  backend: postgres\n  dsn: postgresql://user:pass@dbhost:5432/coord\n",
    )
    src = tmp_path / "coord.db"
    _make_source_db(src)
    dest_dir = tmp_path / "backups"

    result = _run(tmp_path, src=src, dest_dir=dest_dir, mounted=False, coord_config=cfg)

    assert result.returncode != 0
    assert "postgres" in result.stderr
    assert "not a mountpoint" not in result.stderr
    assert not dest_dir.exists() or not list(dest_dir.iterdir())


def test_succeeds_with_an_explicit_sqlite_backend_block(tmp_path: Path) -> None:
    """Acceptance criterion: `store.backend: sqlite` (written out explicitly,
    not just the default absent-block case) must behave byte-for-byte like
    today -- a real snapshot gets written."""
    cfg = _write_store_config(tmp_path, "store:\n  backend: sqlite\n")
    src = tmp_path / "coord.db"
    _make_source_db(src, rows=2)
    dest_dir = tmp_path / "backups"

    result = _run(tmp_path, src=src, dest_dir=dest_dir, mounted=True, coord_config=cfg)

    assert result.returncode == 0, result.stderr
    assert len(_snapshots(dest_dir)) == 1


def test_refuses_when_coord_command_is_unavailable(tmp_path: Path) -> None:
    """If the accessor itself can't be reached at all (here: `$COORD_BIN`
    points at a name that isn't on `$PATH`), the script must refuse loudly
    rather than fall back to assuming sqlite -- the issue's own "grep-free
    failure, not a silent SQLite assumption" requirement."""
    src = tmp_path / "coord.db"
    _make_source_db(src)
    dest_dir = tmp_path / "backups"

    result = _run(
        tmp_path,
        src=src,
        dest_dir=dest_dir,
        mounted=True,
        extra_env={"COORD_BIN": "coord-does-not-exist-3085"},
    )

    assert result.returncode != 0
    assert "cannot determine the store backend" in result.stderr
    assert not dest_dir.exists() or not list(dest_dir.iterdir())
