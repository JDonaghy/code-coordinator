"""`coord backup` — the off-site, verified store backup lane (#3118, #1822).

Black-box where it counts: a **fake `restic` on `$PATH`** (`_RESTIC_SHIM`
below) implements enough of restic's real CLI — `init`, `backup --json`,
`snapshots --json`, `restore --target`, `forget --prune`, plus block-level
dedup — that the whole push → verify → upload → prune → restore sequence
runs end to end through the real `subprocess` boundary, with the real argv
and the real environment plumbing, on a machine that has no restic and no
Azure container. What we own (the ordering, the refusals, the verification,
the credential routing) is therefore genuinely exercised rather than mocked
out; only the bytes-in-a-cloud part is stood in for.

The shim deliberately **exits non-zero when `RESTIC_PASSWORD` is unset**,
which is what makes "the credential travels in the environment, never in
argv" an observable property rather than an assertion about our own strings.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import backup as bk


# --------------------------------------------------------------------------
# A fake restic, on PATH, speaking the subset of the CLI this lane uses.
# --------------------------------------------------------------------------

_RESTIC_SHIM = r'''#!/usr/bin/env python3
"""Stand-in restic: a real subprocess, a real argv, real block dedup."""
import hashlib, json, os, shutil, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

args = sys.argv[1:]
repo_url = os.environ.get("RESTIC_REPOSITORY", "")
if not repo_url:
    sys.stderr.write("Fatal: no repository specified\n"); sys.exit(1)
if not os.environ.get("RESTIC_PASSWORD"):
    # The real restic refuses too. This is what proves the password reached
    # the child through the environment and not through argv.
    sys.stderr.write("Fatal: no repository password: " + repo_url + "\n"); sys.exit(1)

repo = Path(repo_url)
repo.mkdir(parents=True, exist_ok=True)
(repo / "chunks").mkdir(exist_ok=True)
(repo / "data").mkdir(exist_ok=True)
with (repo / "calls.log").open("a") as fh:
    fh.write(" ".join(args) + "\n")

snap_file = repo / "snapshots.json"

def load():
    return json.loads(snap_file.read_text()) if snap_file.exists() else []

def save(s):
    snap_file.write_text(json.dumps(s))

def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default

cmd = args[0] if args else ""

if cmd == "init":
    save([]); print("created restic repository"); sys.exit(0)

if cmd == "backup":
    path = Path(args[-1])
    tags = [args[i + 1] for i, a in enumerate(args) if a == "--tag"]
    blob = path.read_bytes()
    added = 0
    for i in range(0, len(blob), 4096):
        chunk = blob[i:i + 4096]
        cp = repo / "chunks" / hashlib.sha256(chunk).hexdigest()
        if not cp.exists():
            cp.write_bytes(chunk); added += len(chunk)
    snaps = load()
    sid = hashlib.sha256(blob + str(len(snaps)).encode()).hexdigest()
    shutil.copy2(path, repo / "data" / sid)
    when = datetime.now(timezone.utc) + timedelta(seconds=len(snaps))
    snaps.append({"id": sid, "short_id": sid[:8], "time": when.isoformat(),
                  "paths": [str(path)], "tags": tags})
    save(snaps)
    print(json.dumps({"message_type": "status", "percent_done": 1.0}))
    print(json.dumps({"message_type": "summary", "snapshot_id": sid,
                      "data_added": added, "total_bytes_processed": len(blob)}))
    sys.exit(0)

if cmd == "snapshots":
    tag = opt("--tag")
    out = [s for s in load() if tag is None or tag in s.get("tags", [])]
    print(json.dumps(out)); sys.exit(0)

if cmd == "restore":
    wanted = args[1]
    target = Path(opt("--target"))
    snaps = load()
    if wanted == "latest":
        match = snaps[-1] if snaps else None
    else:
        match = next((s for s in snaps
                      if s["id"] == wanted or s["short_id"] == wanted), None)
    if match is None:
        sys.stderr.write("Fatal: no matching snapshot: " + wanted + "\n"); sys.exit(1)
    dest = target / Path(match["paths"][0]).relative_to("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo / "data" / match["id"], dest)
    print("restored 1 files"); sys.exit(0)

if cmd == "forget":
    tag = opt("--tag")
    keep_last = int(opt("--keep-last", "1"))
    snaps = load()
    mine = [s for s in snaps if tag is None or tag in s.get("tags", [])]
    keep = set(s["id"] for s in mine[-keep_last:])
    kept = [s for s in snaps if s["id"] in keep
            or (tag is not None and tag not in s.get("tags", []))]
    for s in snaps:
        if s not in kept:
            (repo / "data" / s["id"]).unlink(missing_ok=True)
    save(kept)
    print("removed " + str(len(snaps) - len(kept)) + " snapshots"); sys.exit(0)

sys.stderr.write("Fatal: fake restic does not implement " + cmd + "\n")
sys.exit(2)
'''


@pytest.fixture
def restic_on_path(tmp_path, monkeypatch) -> Path:
    """Install the shim as `restic` on `$PATH` and return the repo dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "restic"
    shim.write_text(_RESTIC_SHIM.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    repo = tmp_path / "restic-repo"
    monkeypatch.setenv(bk.REPOSITORY_ENV, str(repo))
    monkeypatch.setenv("RESTIC_PASSWORD", "s3cr3t-repo-password")
    return repo


def _calls(repo: Path) -> list[str]:
    log = repo / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


# --------------------------------------------------------------------------
# A source database that looks enough like coord.db to exercise both gates.
# --------------------------------------------------------------------------


def make_source_db(path: Path, *, assignments: int = 3, padding: int = 0) -> Path:
    """A WAL-mode SQLite db with a non-empty `assignments` table.

    Deliberately hand-rolled rather than built from coord's real schema: what
    this lane cares about is "an `assignments` table exists and has rows",
    and pinning the test to the live schema would make it fail on every
    unrelated migration.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE assignments (id INTEGER PRIMARY KEY, machine TEXT, issue INT)")
    conn.execute("CREATE TABLE test_verdicts (assignment_id INT, verdict TEXT)")
    conn.executemany(
        "INSERT INTO assignments VALUES (?, ?, ?)",
        [(i, f"machine-{i}", 1000 + i) for i in range(assignments)],
    )
    conn.executemany(
        "INSERT INTO test_verdicts VALUES (?, ?)",
        [(i, "passed") for i in range(assignments)],
    )
    if padding:
        conn.execute("CREATE TABLE bulk (id INTEGER PRIMARY KEY, blob BLOB)")
        conn.executemany(
            "INSERT INTO bulk VALUES (?, ?)",
            [(i, bytes([i % 251]) * 4096) for i in range(padding)],
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def coord_home(tmp_path, monkeypatch) -> Path:
    """An isolated `$COORD_DIR` whose `coord.db` is a real, populated db.

    `coord.db.DB_PATH` re-resolves against `$COORD_DIR` on every access
    (#2781's PEP 562 fallback), so setting the env var is enough to point the
    whole lane at a throwaway store.
    """
    home = tmp_path / "coord-home"
    home.mkdir()
    monkeypatch.setenv("COORD_DIR", str(home))
    make_source_db(home / "coord.db")
    return home


@pytest.fixture
def sqlite_config(tmp_path, monkeypatch) -> Path:
    """A `coordinator.yml` with no `store:` block at all — i.e. SQLite."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos: []\nmachines: []\n")
    monkeypatch.setenv("COORD_CONFIG", str(cfg))
    return cfg


# ==========================================================================
# Acceptance: push produces a restorable snapshot that matches row-for-row
# ==========================================================================


def test_push_then_restore_reproduces_the_database_row_for_row(
    tmp_path, coord_home, sqlite_config, restic_on_path
):
    """The headline criterion: a pushed snapshot restores to a database that
    passes `PRAGMA integrity_check` **and matches the source row-for-row on
    every table** — not merely one that opens without erroring."""
    result = bk.push()
    assert result.backend == "sqlite"
    assert result.rows == 3

    into = tmp_path / "restore-probe.db"
    bk.restore(result.snapshot_id, into)

    restored = sqlite3.connect(str(into))
    try:
        assert restored.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
    finally:
        restored.close()

    assert bk.table_fingerprint(into) == bk.table_fingerprint(coord_home / "coord.db")


def test_restore_accepts_the_short_snapshot_id(
    tmp_path, coord_home, sqlite_config, restic_on_path
):
    result = bk.push()
    into = tmp_path / "short-id.db"
    bk.restore(result.snapshot_id[:8], into)
    assert bk.table_fingerprint(into) == bk.table_fingerprint(coord_home / "coord.db")


# ==========================================================================
# Acceptance: WAL-safety — the property a `cp` lacks
# ==========================================================================


def test_snapshot_is_valid_while_a_writer_holds_an_open_wal_transaction(
    tmp_path, coord_home
):
    """A snapshot taken while another connection holds an open write
    transaction is valid, passes `integrity_check`, and contains the
    committed state — excluding the writer's uncommitted row.

    This is exactly what `VACUUM INTO` buys over `cp`: a plain copy of a
    WAL-mode database mid-transaction can capture a torn file, and you find
    out at restore time.
    """
    src = coord_home / "coord.db"
    writer = sqlite3.connect(str(src), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO assignments VALUES (999, 'uncommitted', 9999)")
        # ...and hold it open across the snapshot.
        dest = bk.snapshot_sqlite(tmp_path / "wal-snapshot.db")
        rows = bk.verify_sqlite_snapshot(dest)
    finally:
        writer.rollback()
        writer.close()

    assert rows == 3, "the uncommitted row must not appear in the snapshot"
    conn = sqlite3.connect(str(dest))
    try:
        assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE machine = 'uncommitted'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# ==========================================================================
# Acceptance: dedup — the entire premise of the design
# ==========================================================================


def test_second_push_transfers_far_less_than_the_database(
    tmp_path, coord_home, sqlite_config, restic_on_path
):
    """Scaled-down proof of the design's premise: after a first full push, a
    push of a barely-changed database transfers a small fraction of it.

    The real number (< 10 MB against the ~700 MB production database) is an
    operator measurement recorded on the issue — it needs the real db and a
    real restic. What is *testable* here is that the lane reports a real
    transferred byte count and that count tracks the delta, not the file.
    """
    db = coord_home / "coord.db"
    make_source_db(db, assignments=3, padding=400)  # ~1.6 MB of blobs

    first = bk.push()
    assert first.transferred_bytes > 0

    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO assignments VALUES (77, 'later', 4242)")
    conn.commit()
    conn.close()

    second = bk.push()
    assert second.snapshot_id != first.snapshot_id
    assert second.transferred_bytes < first.transferred_bytes / 4, (
        f"incremental push transferred {second.transferred_bytes} of "
        f"{first.transferred_bytes} — dedup is not working, which is the "
        "entire premise of this design"
    )


# ==========================================================================
# Acceptance: a bad snapshot is never uploaded and never evicts a good one
# ==========================================================================


def test_corrupt_snapshot_is_not_uploaded_and_does_not_age_out_a_good_backup(
    tmp_path, coord_home, sqlite_config, restic_on_path, monkeypatch
):
    good = bk.push()
    before = _calls(restic_on_path)

    def _corrupt(dest, *, source=None):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
        return Path(dest)

    monkeypatch.setattr(bk, "snapshot_sqlite", _corrupt)

    with pytest.raises(bk.BackupError) as exc:
        bk.push()

    assert "verification" in str(exc.value)
    after = _calls(restic_on_path)
    assert [c for c in after[len(before):] if c.startswith(("backup", "forget"))] == [], (
        "a snapshot that failed verification must never be uploaded and must "
        "never trigger retention"
    )
    surviving = [s["id"] for s in bk.list_snapshots(bk.BackupConfig.from_env())]
    assert good.snapshot_id in surviving


def test_truncated_snapshot_is_rejected_and_kept_as_REJECTED(
    tmp_path, coord_home, sqlite_config, restic_on_path, monkeypatch
):
    def _truncated(dest, *, source=None):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"SQLite format 3\x00")
        return Path(dest)

    monkeypatch.setattr(bk, "snapshot_sqlite", _truncated)
    with pytest.raises(bk.BackupError):
        bk.push()

    rejects = list((coord_home / "backup-rejected").glob("*.REJECTED"))
    assert len(rejects) == 1, (
        "a failed snapshot is neither silently deleted nor left under its "
        "normal name, indistinguishable from a good backup"
    )


def test_zero_assignment_snapshot_is_rejected(tmp_path):
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(str(empty))
    conn.execute("CREATE TABLE assignments (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(bk.VerifyFailure) as exc:
        bk.verify_sqlite_snapshot(empty)
    assert "0 assignments" in str(exc.value)


def test_snapshot_without_an_assignments_table_is_rejected(tmp_path):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(bk.VerifyFailure) as exc:
        bk.verify_sqlite_snapshot(other)
    assert "assignments" in str(exc.value)


def test_zero_length_snapshot_is_rejected(tmp_path):
    empty = tmp_path / "zero.db"
    empty.write_bytes(b"")
    with pytest.raises(bk.VerifyFailure):
        bk.verify_sqlite_snapshot(empty)


# ==========================================================================
# Acceptance: retention prunes by policy, never to zero
# ==========================================================================


def test_retention_policy_always_carries_a_keep_last_floor(
    coord_home, sqlite_config, restic_on_path
):
    """`--keep-last 1` is unconditional, so even a clock skew or a policy
    misconfiguration that matched nothing still leaves the newest snapshot."""
    bk.push()
    forgets = [c for c in _calls(restic_on_path) if c.startswith("forget")]
    assert forgets, "a successful push must run retention"
    assert "--keep-last 1" in forgets[-1]
    assert "--keep-hourly 48" in forgets[-1]
    assert "--keep-daily 30" in forgets[-1]
    assert "--keep-weekly 8" in forgets[-1]
    assert "--prune" in forgets[-1]


def test_retention_leaves_a_snapshot_behind(coord_home, sqlite_config, restic_on_path):
    for _ in range(3):
        bk.push()
    assert bk.list_snapshots(bk.BackupConfig.from_env()), (
        "retention must never leave the repository with zero backups"
    )


def test_prune_is_skipped_on_an_empty_repository(coord_home, sqlite_config, restic_on_path):
    """Nothing to prune, and a `forget` against an empty repo is pure risk —
    so it is not issued at all, and the caller is told why."""
    config = bk.BackupConfig.from_env()
    bk.init_repository(config)
    pruned, reason = bk.prune(config)
    assert pruned is False
    assert reason and "no snapshots" in reason
    assert not [c for c in _calls(restic_on_path) if c.startswith("forget")]


def test_retention_never_runs_when_every_candidate_fails_verification(
    coord_home, sqlite_config, restic_on_path, monkeypatch
):
    """The "even when every candidate fails" half of the criterion: a run in
    which nothing verifies prunes nothing, so the good backups already up
    there cannot age out behind a wall of failures."""
    good = bk.push()

    def _junk(dest, *, source=None):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"junk")
        return Path(dest)

    monkeypatch.setattr(bk, "snapshot_sqlite", _junk)
    for _ in range(5):
        with pytest.raises(bk.BackupError):
            bk.push()

    forgets = [c for c in _calls(restic_on_path) if c.startswith("forget")]
    assert len(forgets) == 1, "only the one successful push should have pruned"
    assert good.snapshot_id in [s["id"] for s in bk.list_snapshots(bk.BackupConfig.from_env())]


# ==========================================================================
# Acceptance: the #3085 property — refuse an unsnapshottable backend
# ==========================================================================


def test_push_refuses_an_unknown_store_backend_and_writes_nothing(
    tmp_path, coord_home, restic_on_path, monkeypatch
):
    """With the store backend set to something the lane cannot snapshot,
    `push` exits non-zero **before writing anything**, and names the backend.

    There is no fall-back-to-sqlite path: after a Postgres cutover the old
    `coord.db`, if it is even still there, is a dead frozen file, and a lane
    that assumed sqlite would back it up and offer to roll the fleet back
    to it (#3085).
    """
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos: []\nmachines: []\nstore:\n  backend: mysql\n")
    monkeypatch.setenv("COORD_CONFIG", str(cfg))

    with pytest.raises(bk.BackupError) as exc:
        bk.push()

    assert "mysql" in str(exc.value)
    assert _calls(restic_on_path) == [], "nothing may reach the repository"
    assert not (coord_home / "backup-rejected").exists()


def test_resolve_backend_refuses_a_backend_the_lane_cannot_snapshot(monkeypatch):
    """The defensive arm of the same rule: even if the config layer someday
    validates a backend this module has no snapshot method for, dispatch
    refuses by name rather than silently doing the sqlite thing."""
    from coord import db as _db

    monkeypatch.setattr(_db, "resolve_store_backend", lambda: ("cockroach", None))
    with pytest.raises(bk.BackupError) as exc:
        bk.resolve_backend()
    assert "cockroach" in str(exc.value)
    assert "Nothing was written" in str(exc.value)


def test_resolve_backend_reports_sqlite_for_a_config_with_no_store_block(sqlite_config):
    assert bk.resolve_backend() == ("sqlite", None)


# ==========================================================================
# Acceptance: restore refuses to clobber the live database
# ==========================================================================


def test_restore_refuses_to_overwrite_the_live_db_without_force(
    coord_home, sqlite_config, restic_on_path
):
    result = bk.push()
    live = coord_home / "coord.db"
    before = live.read_bytes()

    with pytest.raises(bk.BackupError) as exc:
        bk.restore(result.snapshot_id, live)

    assert "live database" in str(exc.value)
    assert "--force" in str(exc.value)
    assert live.read_bytes() == before


def test_restore_refuses_to_overwrite_any_existing_file_without_force(
    tmp_path, coord_home, sqlite_config, restic_on_path
):
    result = bk.push()
    occupied = tmp_path / "already-here.db"
    occupied.write_bytes(b"do not clobber me")

    with pytest.raises(bk.BackupError) as exc:
        bk.restore(result.snapshot_id, occupied)
    assert "--force" in str(exc.value)
    assert occupied.read_bytes() == b"do not clobber me"


def test_restore_with_force_does_overwrite_the_live_db(
    coord_home, sqlite_config, restic_on_path
):
    """The escape hatch exists and works — the refusal is a guardrail, not a
    wall, or an operator in a real disaster would route around it."""
    result = bk.push()
    live = coord_home / "coord.db"
    conn = sqlite3.connect(str(live))
    conn.execute("DELETE FROM assignments")
    conn.commit()
    conn.close()

    bk.restore(result.snapshot_id, live, force=True)

    conn = sqlite3.connect(str(live))
    try:
        assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 3
    finally:
        conn.close()


# ==========================================================================
# Acceptance: no credential in coordinator.yml, in argv, or in any log line
# ==========================================================================


def test_no_credential_appears_in_argv_or_in_the_log(
    tmp_path, coord_home, restic_on_path, monkeypatch, caplog
):
    """Asserts on both halves of the criterion: the constructed command and
    the log output. argv is world-readable in `/proc`, and the journal is
    read by anyone who can read the journal."""
    secret = "s3cr3t-repo-password"
    monkeypatch.setenv("AZURE_ACCOUNT_KEY", "azure-account-key-value")

    # A coordinator.yml that *tries* to supply a credential: the lane must
    # ignore it entirely, because it never reads the config for secrets.
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos: []\nmachines: []\n"
        "backup:\n  password: yaml-supplied-password\n  azure_key: yaml-azure-key\n"
    )
    monkeypatch.setenv("COORD_CONFIG", str(cfg))

    config = bk.BackupConfig.from_env()
    argv = bk.restic_argv(config, "backup", "--json", "--tag", config.tag, "/tmp/x")
    assert not any(secret in a for a in argv)
    assert not any("azure-account-key-value" in a for a in argv)

    with caplog.at_level(logging.DEBUG, logger=bk.LOGGER_NAME):
        result = bk.push()

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in log_text
    assert "azure-account-key-value" not in log_text
    assert "yaml-supplied-password" not in log_text

    # The shim records the argv it was actually invoked with — the strongest
    # available statement that nothing secret travelled on the command line.
    recorded = "\n".join(_calls(restic_on_path))
    assert secret not in recorded
    assert "azure-account-key-value" not in recorded
    assert result.snapshot_id


def test_scrub_redacts_credential_values_out_of_subprocess_output():
    """restic's own stderr can echo a repository URL with an embedded SAS
    token; that stderr is what lands verbatim in a systemd journal."""
    env = {"AZURE_ACCOUNT_SAS": "sv=2021&sig=DEADBEEFCAFE"}
    text = "Fatal: azure:c:/p?sv=2021&sig=DEADBEEFCAFE is unreachable"
    assert "DEADBEEFCAFE" not in bk.scrub(text, env)
    assert "***" in bk.scrub(text, env)


def test_scrub_ignores_trivially_short_values():
    """A one- or two-character credential value would otherwise redact half
    the log. Real secrets are long; a short one is a config error, not
    something to mangle every message over."""
    assert bk.scrub("backend is ok", {"RESTIC_PASSWORD": "ok"}) == "backend is ok"


def test_push_refuses_before_snapshotting_when_no_password_is_available(
    tmp_path, coord_home, sqlite_config, monkeypatch
):
    """Cheap preflight: moves a guaranteed failure from *after* a 720 MB
    VACUUM INTO to before it."""
    monkeypatch.setenv(bk.REPOSITORY_ENV, str(tmp_path / "repo"))
    for name in ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(bk.BackupError) as exc:
        bk.push()
    assert "password" in str(exc.value)
    assert "coordinator.yml" in str(exc.value)


# ==========================================================================
# Configuration
# ==========================================================================


def test_from_env_requires_a_repository(monkeypatch):
    monkeypatch.delenv(bk.REPOSITORY_ENV, raising=False)
    monkeypatch.delenv(bk.RESTIC_REPOSITORY_ENV, raising=False)
    with pytest.raises(bk.BackupError) as exc:
        bk.BackupConfig.from_env()
    assert bk.REPOSITORY_ENV in str(exc.value)


def test_from_env_accepts_restic_own_repository_variable(monkeypatch):
    monkeypatch.delenv(bk.REPOSITORY_ENV, raising=False)
    monkeypatch.setenv(bk.RESTIC_REPOSITORY_ENV, "azure:container:/path")
    assert bk.BackupConfig.from_env().repository == "azure:container:/path"


def test_retention_knobs_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(bk.REPOSITORY_ENV, "azure:c:/p")
    monkeypatch.setenv("COORD_BACKUP_KEEP_HOURLY", "12")
    config = bk.BackupConfig.from_env()
    assert config.keep_hourly == 12
    assert (config.keep_daily, config.keep_weekly) == (30, 8)


def test_a_nonsense_retention_knob_is_refused(monkeypatch):
    monkeypatch.setenv(bk.REPOSITORY_ENV, "azure:c:/p")
    monkeypatch.setenv("COORD_BACKUP_KEEP_DAILY", "0")
    with pytest.raises(bk.BackupError):
        bk.BackupConfig.from_env()


def test_restic_env_carries_the_repository_and_not_argv(monkeypatch):
    monkeypatch.setenv(bk.REPOSITORY_ENV, "azure:coord-backups:/dellserver")
    config = bk.BackupConfig.from_env()
    assert bk.restic_env(config)[bk.RESTIC_REPOSITORY_ENV] == "azure:coord-backups:/dellserver"
    assert "-r" not in bk.restic_argv(config, "snapshots")


def test_missing_restic_binary_is_a_clear_refusal(monkeypatch, tmp_path):
    monkeypatch.setenv(bk.REPOSITORY_ENV, str(tmp_path / "repo"))
    monkeypatch.setenv("COORD_RESTIC_BIN", str(tmp_path / "no-such-restic"))
    config = bk.BackupConfig.from_env()
    with pytest.raises(bk.BackupError) as exc:
        bk.ResticRunner(config).run("snapshots")
    assert "restic not found" in str(exc.value)


# ==========================================================================
# The Postgres branch — written and tested here; proved against a real
# Postgres fleet by #829, per this issue's out-of-scope list.
# ==========================================================================


def test_pg_dump_command_moves_the_password_out_of_argv():
    argv, env = bk.pg_dump_command(
        "postgresql://coord:hunter2@db.internal:5432/coord", Path("/tmp/out.dump")
    )
    joined = " ".join(argv)
    assert "hunter2" not in joined, "argv is world-readable in /proc"
    assert env["PGPASSWORD"] == "hunter2"
    assert "coord@db.internal:5432" in joined
    assert "--format=custom" in joined
    assert "--file=/tmp/out.dump" in joined


def test_pg_dump_command_leaves_a_passwordless_dsn_alone():
    argv, env = bk.pg_dump_command("postgresql://db.internal/coord", Path("/tmp/o.dump"))
    assert env == {}
    assert "postgresql://db.internal/coord" in argv


def test_pg_dump_command_moves_the_password_out_of_argv_for_keyword_value_dsn():
    # libpq / psycopg / pg_dump --dbname all accept this form just as much as
    # the URI form, per StoreConfig's docstring — a password here must be
    # scrubbed exactly like a URI-embedded one.
    argv, env = bk.pg_dump_command(
        "host=db.internal port=5432 dbname=coord user=coord password=hunter2",
        Path("/tmp/out.dump"),
    )
    joined = " ".join(argv)
    assert "hunter2" not in joined, "argv is world-readable in /proc"
    assert env["PGPASSWORD"] == "hunter2"
    assert "host=db.internal" in joined
    assert "dbname=coord" in joined
    assert "user=coord" in joined
    assert "--format=custom" in joined
    assert "--file=/tmp/out.dump" in joined


def test_pg_dump_command_leaves_a_passwordless_keyword_value_dsn_alone():
    argv, env = bk.pg_dump_command(
        "host=db.internal dbname=coord", Path("/tmp/o.dump")
    )
    assert env == {}
    assert "host=db.internal dbname=coord" in argv


def test_pg_dump_command_handles_a_quoted_keyword_value_password():
    # libpq allows single-quoting a value that contains whitespace or a
    # quote/backslash itself; the parser must still find and scrub it.
    argv, env = bk.pg_dump_command(
        "host=db.internal password='hunter two' dbname=coord",
        Path("/tmp/out.dump"),
    )
    joined = " ".join(argv)
    assert "hunter two" not in joined
    assert env["PGPASSWORD"] == "hunter two"
    assert "host=db.internal" in joined
    assert "dbname=coord" in joined


def test_snapshot_postgres_passes_the_password_only_in_the_environment(tmp_path):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        out = next(a for a in argv if a.startswith("--file="))
        Path(out.split("=", 1)[1]).write_bytes(b"PGDMP-fake")
        return subprocess.CompletedProcess(argv, 0, "", "")

    dest = tmp_path / "pg.dump"
    bk.snapshot_postgres("postgresql://u:pw-secret@h/db", dest, runner=fake_run)
    assert not any("pw-secret" in a for a in seen["argv"])
    assert seen["env"]["PGPASSWORD"] == "pw-secret"
    assert dest.read_bytes() == b"PGDMP-fake"


def test_snapshot_postgres_surfaces_a_failed_dump(tmp_path):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "could not connect")

    with pytest.raises(bk.BackupError) as exc:
        bk.snapshot_postgres("postgresql://h/db", tmp_path / "x.dump", runner=fake_run)
    assert "could not connect" in str(exc.value)


def test_postgres_snapshot_verification_requires_the_dump_magic(tmp_path):
    bogus = tmp_path / "not-a-dump"
    bogus.write_bytes(b"this is a text file")
    with pytest.raises(bk.VerifyFailure) as exc:
        bk.verify_postgres_snapshot(bogus)
    assert "custom-format" in str(exc.value)


def test_postgres_snapshot_verification_requires_an_assignments_table(tmp_path):
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"PGDMP" + b"\x00" * 32)

    def toc_without_assignments(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "TABLE DATA public repos coord\n", "")

    with pytest.raises(bk.VerifyFailure) as exc:
        bk.verify_postgres_snapshot(dump, runner=toc_without_assignments)
    assert "assignments" in str(exc.value)


def test_postgres_snapshot_verification_accepts_a_good_dump(tmp_path):
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"PGDMP" + b"\x00" * 32)

    def toc(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, "TABLE DATA public assignments coord\n", ""
        )

    assert bk.verify_postgres_snapshot(dump, runner=toc) is None


def test_an_unverifiable_postgres_snapshot_never_counts(tmp_path):
    """No `pg_restore` means no verification, and an unverified snapshot is
    not a backup — it must not quietly pass."""
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"PGDMP" + b"\x00" * 32)

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    with pytest.raises(bk.VerifyFailure) as exc:
        bk.verify_postgres_snapshot(dump, runner=missing)
    assert "pg_restore not found" in str(exc.value)


# ==========================================================================
# restic output parsing
# ==========================================================================


def test_backup_summary_parsing_picks_the_summary_line():
    stdout = "\n".join(
        [
            json.dumps({"message_type": "status", "percent_done": 0.5}),
            "not json at all",
            json.dumps(
                {"message_type": "summary", "snapshot_id": "abc123", "data_added": 4096}
            ),
        ]
    )
    assert bk._parse_backup_summary(stdout) == ("abc123", 4096)


def test_a_backup_with_no_summary_does_not_count():
    with pytest.raises(bk.BackupError) as exc:
        bk._parse_backup_summary(json.dumps({"message_type": "status"}))
    assert "does not count" in str(exc.value)


def test_format_bytes_is_readable():
    assert bk.format_bytes(512) == "512 B"
    assert bk.format_bytes(1536) == "1.5 KiB"
    assert bk.format_bytes(10 * 1024 * 1024) == "10.0 MiB"


def test_summarize_snapshots_is_newest_first():
    lines = bk.summarize_snapshots(
        [
            {"short_id": "aaaaaaaa", "time": "2026-09-01T00:00:00Z", "paths": ["/a"]},
            {"short_id": "bbbbbbbb", "time": "2026-09-04T00:00:00Z", "paths": ["/b"]},
        ]
    )
    assert lines[0].startswith("bbbbbbbb")


# ==========================================================================
# CLI surface
# ==========================================================================


def _cli():
    from coord.cli import main

    return main


def test_cli_push_prints_the_transfer_size(coord_home, sqlite_config, restic_on_path):
    """The transferred byte count is printed on every run on purpose: if it
    is ever ~700 MB, dedup is not working and the lane costs far more than
    it should."""
    result = CliRunner().invoke(_cli(), ["backup", "push"])
    assert result.exit_code == 0, result.output
    assert "backup: ok" in result.output
    assert "transferred=" in result.output
    assert "backend=sqlite" in result.output


def test_cli_push_exits_non_zero_on_an_unsnapshottable_backend(
    tmp_path, coord_home, restic_on_path, monkeypatch
):
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos: []\nmachines: []\nstore:\n  backend: mysql\n")
    monkeypatch.setenv("COORD_CONFIG", str(cfg))

    result = CliRunner().invoke(_cli(), ["backup", "push"])
    assert result.exit_code == 1
    assert "mysql" in result.output
    assert _calls(restic_on_path) == []


def test_cli_list_reports_an_empty_repository(coord_home, sqlite_config, restic_on_path):
    CliRunner().invoke(_cli(), ["backup", "init"])
    result = CliRunner().invoke(_cli(), ["backup", "list"])
    assert result.exit_code == 0
    assert "no snapshots" in result.output


def test_cli_list_shows_the_pushed_snapshot(coord_home, sqlite_config, restic_on_path):
    pushed = bk.push()
    result = CliRunner().invoke(_cli(), ["backup", "list"])
    assert result.exit_code == 0
    assert pushed.snapshot_id[:8] in result.output


def test_cli_restore_refuses_the_live_db_without_force(
    coord_home, sqlite_config, restic_on_path
):
    pushed = bk.push()
    result = CliRunner().invoke(
        _cli(),
        ["backup", "restore", pushed.snapshot_id, "--into", str(coord_home / "coord.db")],
    )
    assert result.exit_code == 1
    assert "live database" in result.output


def test_cli_restore_writes_the_snapshot(tmp_path, coord_home, sqlite_config, restic_on_path):
    pushed = bk.push()
    into = tmp_path / "probe.db"
    result = CliRunner().invoke(
        _cli(), ["backup", "restore", pushed.snapshot_id, "--into", str(into)]
    )
    assert result.exit_code == 0, result.output
    assert into.exists()
    assert bk.table_fingerprint(into) == bk.table_fingerprint(coord_home / "coord.db")


def test_cli_push_no_prune_skips_retention(coord_home, sqlite_config, restic_on_path):
    result = CliRunner().invoke(_cli(), ["backup", "push", "--no-prune"])
    assert result.exit_code == 0, result.output
    assert not [c for c in _calls(restic_on_path) if c.startswith("forget")]
