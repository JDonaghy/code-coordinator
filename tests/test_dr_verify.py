"""`coord dr verify` — continuously proving the off-site backup restores (#3119).

Black-box where it counts, in two directions at once:

* a **fake `restic` on `$PATH`** (a trimmed sibling of `tests/test_backup.py`'s
  shim) so the whole fetch path runs through the real `subprocess` boundary,
  with the real argv and the real environment plumbing, on a machine with no
  restic and no Azure container — and so a snapshot can be *deliberately
  corrupted in the repository* to drive the failure paths;
* a **real `coord serve`**, booted as an actual subprocess on an ephemeral
  port against the restored scratch DB, for the parity step. Mocking that one
  would delete the only check that distinguishes *restorable* from
  *recoverable*, which is the whole point of the rung.

The store the tests snapshot is the real coord schema (`coord.db._ensure_schema`
+ the #748 board fixture), not a hand-rolled two-table stand-in: the schema and
parity checks are both assertions *about* the real schema, so a stand-in would
make them vacuous.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import backup as bk
from coord import config as cfgmod
from coord import dr_verify
from coord.cli import main
from coord.config import NotificationsConfig
from coord.db import _DB_SCHEMA_VERSION, _ensure_schema
from coord.gen_board_fixture import build_fixture_db
from coord.notifier.transport import MemoryTransport

# --------------------------------------------------------------------------
# A fake restic, on PATH, speaking the subset of the CLI this lane uses.
# --------------------------------------------------------------------------

_RESTIC_SHIM = r'''#!/usr/bin/env python3
"""Stand-in restic: a real subprocess, a real argv, a real repository on disk."""
import hashlib, json, os, shutil, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

args = sys.argv[1:]
repo_url = os.environ.get("RESTIC_REPOSITORY", "")
if not repo_url:
    sys.stderr.write("Fatal: no repository specified\n"); sys.exit(1)
password = os.environ.get("RESTIC_PASSWORD")
if not password:
    # The real restic refuses too. This is what proves the password reached
    # the child through the environment and not through argv.
    sys.stderr.write("Fatal: no repository password: " + repo_url + "\n"); sys.exit(1)

repo = Path(repo_url)
repo.mkdir(parents=True, exist_ok=True)
(repo / "data").mkdir(exist_ok=True)
with (repo / "calls.log").open("a") as fh:
    fh.write(" ".join(args) + "\n")

snap_file = repo / "snapshots.json"
load = lambda: json.loads(snap_file.read_text()) if snap_file.exists() else []
save = lambda s: snap_file.write_text(json.dumps(s))
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default

cmd = args[0] if args else ""

if cmd == "init":
    save([]); print("created restic repository"); sys.exit(0)

if cmd == "backup":
    path = Path(args[-1])
    tags = [args[i + 1] for i, a in enumerate(args) if a == "--tag"]
    blob = path.read_bytes()
    snaps = load()
    sid = hashlib.sha256(blob + str(len(snaps)).encode()).hexdigest()
    shutil.copy2(path, repo / "data" / sid)
    when = datetime.now(timezone.utc) + timedelta(seconds=len(snaps))
    snaps.append({"id": sid, "short_id": sid[:8], "time": when.isoformat(),
                  "paths": [str(path)], "tags": tags})
    save(snaps)
    print(json.dumps({"message_type": "summary", "snapshot_id": sid,
                      "data_added": len(blob)}))
    sys.exit(0)

if cmd == "snapshots":
    tag = opt("--tag")
    out = [s for s in load() if tag is None or tag in s.get("tags", [])]
    print(json.dumps(out)); sys.exit(0)

if cmd == "restore":
    wanted = args[1]
    target = Path(opt("--target"))
    match = next((s for s in load()
                  if s["id"] == wanted or s["short_id"] == wanted), None)
    if match is None:
        # Deliberately echoes a credential back, the way a real tool's stderr
        # can echo a repository URL carrying a SAS token. Nothing downstream
        # may let this reach a log, a record or an alert.
        sys.stderr.write("Fatal: no matching snapshot " + wanted +
                         " in " + repo_url + " (password=" + password + ")\n")
        sys.exit(1)
    dest = target / Path(match["paths"][0]).relative_to("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo / "data" / match["id"], dest)
    print("restored 1 files"); sys.exit(0)

sys.stderr.write("Fatal: fake restic does not implement " + cmd + "\n")
sys.exit(2)
'''

SECRET = "s3cr3t-repo-password-do-not-leak"


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
    monkeypatch.setenv("RESTIC_PASSWORD", SECRET)
    return repo


def make_store(path: Path) -> Path:
    """A real coord store: the live schema, stamped, with board rows in it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        build_fixture_db(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def hollow_out_assignments(path: Path) -> Path:
    """Empty *path*'s `assignments` table, leaving the store otherwise valid.

    This is what a truncated-but-openable restore looks like: `integrity_check`
    passes, the schema is current, and the board is gone. Two rungs need that
    exact shape — the content check (`assignments` empty in the restore while
    live's is not) and the parity check (`/board` (restored) serves 0 while
    `/board` (live) serves some) — so they share one implementation rather
    than each opening their own connection to write the same DELETE.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM assignments")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def coord_home(tmp_path, monkeypatch) -> Path:
    """An isolated `$COORD_DIR` whose `coord.db` is a real, populated store."""
    home = tmp_path / "coord-home"
    home.mkdir()
    monkeypatch.setenv("COORD_DIR", str(home))
    make_store(home / "coord.db")
    return home


@pytest.fixture
def sqlite_config(tmp_path, monkeypatch) -> Path:
    """A `coordinator.yml` with no `store:` block at all — i.e. SQLite.

    Minimal but *valid*: the parity step boots a real `coord serve` against
    this file, and the daemon refuses to start on a config with no repos.
    """
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        "  - name: claude-coordinator\n"
        "    github: JDonaghy/code-coordinator\n"
        "machines:\n"
        "  - name: dr-verify-scratch\n"
        "    host: 127.0.0.1\n"
        "    repos: [claude-coordinator]\n"
    )
    monkeypatch.setenv("COORD_CONFIG", str(cfg))
    return cfg


@pytest.fixture
def lane(coord_home, sqlite_config, restic_on_path, tmp_path, monkeypatch):
    """Everything a verify needs, plus scratch/record paths and a captured alert."""
    monkeypatch.delenv(dr_verify.MIRROR_ENV, raising=False)
    monkeypatch.delenv(dr_verify.STALENESS_ENV, raising=False)
    transport = MemoryTransport()
    monkeypatch.setattr(dr_verify, "_default_transport", lambda: transport)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    class Lane:
        home = coord_home
        repo = restic_on_path
        config = sqlite_config
        scratch_root = scratch
        record = tmp_path / "last_verify.json"
        alerts = transport.sent

    return Lane()


def run_cli(lane, *extra: str):
    return CliRunner().invoke(
        main,
        ["dr", "verify", "--record", str(lane.record), "--scratch", str(lane.scratch_root), *extra],
    )


def push_backup():
    """`backup.push`, minus retention.

    `--prune` is the one restic verb the shim deliberately does not implement:
    this module is about the restore path, and a retention pass in the middle
    of it would only add a way for these tests to fail for reasons
    `tests/test_backup.py` already covers.
    """
    return bk.push(do_prune=False)


def upload_raw(path: Path) -> str:
    """Upload *path* WITHOUT going through `backup.push`'s verification.

    `push` refuses to upload a snapshot that fails its own gates, which is
    correct — and would make the "a bad snapshot is already off-site" cases
    this module has to cover impossible to set up. A retention pass that
    pruned the wrong side, or a push from an older release, can leave exactly
    such a snapshot in the repository.
    """
    snapshot_id, _ = bk.upload(bk.BackupConfig.from_env(), path)
    return snapshot_id


# ==========================================================================
# Acceptance: the healthy path
# ==========================================================================


def test_verify_against_a_healthy_backup_exits_zero_and_writes_the_record(lane):
    """Exit zero, and a `last_verify.json` carrying timestamp, snapshot id,
    outcome and the measured restore duration — the last of which is #3117's
    Domain-A RTO input, so it has to be measured rather than estimated."""
    pytest.importorskip("uvicorn")
    pushed = push_backup()

    result = run_cli(lane)
    assert result.exit_code == 0, result.output

    record = json.loads(lane.record.read_text())
    assert record["outcome"] == "ok"
    assert record["snapshot_id"] == pushed.snapshot_id
    assert record["timestamp"].endswith("Z")
    assert record["timestamp_epoch"] > 0
    assert record["restore_seconds"] >= 0.0
    assert "restore_seconds" in record and record["duration_seconds"] >= record["restore_seconds"]
    # Success is quiet: no alert, and every check actually ran.
    assert lane.alerts == []
    assert {s["name"] for s in record["steps"]} == {
        "fetch", "structural", "content", "schema", "parity"
    }
    assert all(s["ok"] for s in record["steps"])


def test_verify_mirrors_the_record_somewhere_another_machine_can_read(lane, tmp_path, monkeypatch):
    """The alert must not share a failure domain with the thing it watches.

    An alert originating on the daemon host is not an alert when the daemon
    host is what died, so the last-success timestamp is mirrored off it and
    `coord dr status --record <mirror>` is runnable from anywhere.
    """
    mirror = tmp_path / "offbox"
    monkeypatch.setenv(dr_verify.MIRROR_ENV, str(mirror))
    push_backup()

    assert run_cli(lane, "--no-parity").exit_code == 0

    mirrored = json.loads((mirror / "last_verify.json").read_text())
    assert mirrored["outcome"] == "ok"
    assert dr_verify.evaluate_staleness(path=mirror / "last_verify.json").ok


# ==========================================================================
# Acceptance: the failure paths, each named
# ==========================================================================


def test_truncated_snapshot_fails_and_names_the_structural_failure(lane):
    """A snapshot that uploads fine and restores to a truncated file is the
    failure mode that never announces itself. `integrity_check` is the gate."""
    pushed = push_backup()
    blob = lane.repo / "data" / pushed.snapshot_id
    blob.write_bytes(blob.read_bytes()[: 4096 + 17])

    result = run_cli(lane, "--snapshot", pushed.snapshot_id, "--no-parity")
    assert result.exit_code == 1

    record = json.loads(lane.record.read_text())
    assert record["outcome"] == "failed"
    assert "structural check failed" in record["failure"]
    assert [s["name"] for s in record["steps"] if not s["ok"]] == ["structural"]
    assert lane.alerts, "a failed DR verify must alert"


def test_empty_assignments_table_fails_even_though_integrity_check_passes(lane):
    """The check `integrity_check` cannot make: a perfectly valid database
    whose `assignments` table is empty while live's is not.

    Tolerance deliberately does not apply — empty-in-restore/non-empty-live is
    a hard failure, because it is exactly what a truncated-but-openable
    restore looks like from the content side.
    """
    hollow = hollow_out_assignments(make_store(lane.scratch_root / "hollow.db"))
    snapshot_id = upload_raw(hollow)

    result = run_cli(lane, "--snapshot", snapshot_id, "--no-parity")
    assert result.exit_code == 1

    record = json.loads(lane.record.read_text())
    steps = {s["name"]: s for s in record["steps"]}
    assert steps["structural"]["ok"], "integrity_check passes — that is the point"
    assert not steps["content"]["ok"]
    assert "content check failed" in record["failure"]
    assert "assignments" in record["failure"]
    assert "empty in the restore" in record["failure"]


def test_snapshot_two_migrations_behind_fails_naming_the_schema_gap(lane):
    """A backup that restores cleanly but is two migrations behind the
    installed coord is a recovery that fails at daemon start."""
    behind = make_store(lane.scratch_root / "behind.db")
    conn = sqlite3.connect(str(behind))
    conn.execute("DELETE FROM schema_version")
    conn.execute(
        "INSERT INTO schema_version (version) VALUES (?)", (_DB_SCHEMA_VERSION - 2,)
    )
    conn.commit()
    conn.close()
    snapshot_id = upload_raw(behind)

    result = run_cli(lane, "--snapshot", snapshot_id, "--no-parity")
    assert result.exit_code == 1

    record = json.loads(lane.record.read_text())
    failure = record["failure"]
    assert "schema check failed" in failure
    assert str(_DB_SCHEMA_VERSION - 2) in failure and str(_DB_SCHEMA_VERSION) in failure
    assert "2 migrations behind" in failure
    steps = {s["name"]: s for s in record["steps"]}
    assert steps["structural"]["ok"] and steps["content"]["ok"]


def test_content_check_tolerates_the_snapshot_lagging_live():
    """The snapshot legitimately lags live by up to one backup interval, so a
    small shortfall is not a failure — otherwise this lane pages hourly."""
    live = {"assignments": 1000, "issues": 3}
    lagging = {"assignments": 960, "issues": 2}
    assert dr_verify.check_content(lagging, live).ok

    gutted = {"assignments": 400, "issues": 3}
    with pytest.raises(dr_verify.DRVerifyError, match="short by 600"):
        dr_verify.check_content(gutted, live)


def test_empty_repository_is_reported_rather_than_passing_vacuously(lane):
    """No snapshots at all is the loudest thing this check can find: either
    retention pruned the wrong side or the push lane has never run."""
    bk.init_repository(bk.BackupConfig.from_env())
    result = run_cli(lane, "--no-parity")
    assert result.exit_code == 1
    assert "no snapshots" in json.loads(lane.record.read_text())["failure"]


# ==========================================================================
# Acceptance: parity — a real daemon, a real GET /board
# ==========================================================================


def test_parity_boots_a_real_coord_serve_and_reads_a_well_formed_board(lane):
    """The check that proves *recoverability* rather than *restorability*.

    A real `coord serve` subprocess, an ephemeral port, a real `GET /board`.
    """
    pytest.importorskip("uvicorn")
    pushed = push_backup()
    restored = lane.scratch_root / "coord.db"
    dr_verify.fetch_snapshot(
        pushed.snapshot_id, restored, config=bk.BackupConfig.from_env()
    )
    expected = dr_verify.table_counts(restored)["assignments"]
    assert expected > 0, "the fixture store must carry assignments to prove anything"

    step = dr_verify.check_parity(restored, live_assignments=expected)

    assert step.ok and step.name == "parity"
    assert "/board (restored)" in step.detail
    assert f"{expected} assignment(s)" in step.detail


def test_parity_fails_when_the_board_does_not_match_the_restored_store(lane):
    """Guards the guard: the parity step must be capable of failing."""
    pytest.importorskip("uvicorn")
    pushed = push_backup()
    restored = lane.scratch_root / "coord.db"
    dr_verify.fetch_snapshot(
        pushed.snapshot_id, restored, config=bk.BackupConfig.from_env()
    )

    with pytest.raises(dr_verify.DRVerifyError, match="parity check failed"):
        dr_verify.check_parity(restored, live_assignments=9999)


def test_parity_argv_carries_no_credential_and_the_token_travels_in_the_env(tmp_path):
    """argv is world-readable in /proc — the same rule `coord.backup` applies
    to restic and pg_dump applies to the daemon this step boots."""
    argv = dr_verify.serve_argv(tmp_path / "coordinator.yml", 45123)
    env = dr_verify.serve_env(tmp_path / "scratch", "tok3n-abcdef")

    assert "tok3n-abcdef" not in " ".join(argv)
    assert not any(a.lower().startswith("bearer") for a in argv)
    assert env["COORD_SERVE_TOKEN"] == "tok3n-abcdef"
    assert env["COORD_DIR"] == str(tmp_path / "scratch")
    assert "PYTEST_CURRENT_TEST" not in env


# ==========================================================================
# Acceptance: parity compares /board to /board, never /board to a raw
# table count (#3135)
# ==========================================================================


def add_ancient_terminal_assignments(path: Path, count: int, *, start: int = 90000) -> None:
    """Insert *count* old, terminal, unreferenced assignment rows straight
    into *path*'s `assignments` table.

    This is the shape a real fleet board has after months of churn: far more
    `done` rows than #762's retention cap keeps on `/board` (they are neither
    active, nor recent, nor queued for merge, nor the latest assignment of a
    still-open issue — none of `compute_board_keep_ids`'s keep conditions
    apply). That gap between the raw table and what `/board` serves is
    exactly what #3135's parity check compared against itself and always
    failed on.
    """
    ancient = time.time() - 400 * 86400.0
    conn = sqlite3.connect(str(path))
    try:
        conn.executemany(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "repo_github, issue_number, issue_title, status, type, "
            "dispatched_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"work-ancient-{i}",
                    "precision",
                    "claude-coordinator",
                    "JDonaghy/claude-coordinator",
                    start + i,
                    f"ancient issue {i}",
                    "done",
                    "work",
                    ancient,
                    ancient,
                )
                for i in range(count)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_verify_passes_on_a_production_shaped_store_where_the_table_outnumbers_board(lane):
    """The regression #3135 is about: a board with real history has a raw
    `assignments` table far bigger than what `/board` serves, and the parity
    check must compare `/board` to `/board` — not the raw table to `/board`,
    which fails on every healthy production restore, forever.
    """
    pytest.importorskip("uvicorn")
    live_path = lane.home / "coord.db"
    add_ancient_terminal_assignments(live_path, 500)
    push_backup()

    raw_count = dr_verify.table_counts(live_path)["assignments"]
    served_count = dr_verify.live_board_assignment_count(live_path)
    assert raw_count > served_count, (
        "the fixture must actually diverge — otherwise this test cannot tell "
        "the fix apart from the bug it regression-tests"
    )

    result = run_cli(lane)
    assert result.exit_code == 0, result.output

    record = json.loads(lane.record.read_text())
    assert record["outcome"] == "ok"
    steps = {s["name"]: s for s in record["steps"]}
    assert steps["parity"]["ok"], steps["parity"]["detail"]
    assert lane.alerts == []


def test_parity_fails_hard_when_restored_board_serves_zero_but_live_serves_some(
    sqlite_config, tmp_path
):
    """A restored store whose daemon serves 0 assignments while live serves
    any at all is the daemon-cannot-read-it failure this step exists to
    catch — and must fail even though a live count this small (5) is well
    within the generic tolerance budget (max(50, ceil(5*0.10)) == 50), which
    would otherwise wave a shortfall of 5 through. Tolerance must not be able
    to launder a total loss.
    """
    pytest.importorskip("uvicorn")
    restored_dir = tmp_path / "zero-restore"
    restored_dir.mkdir()
    restored = hollow_out_assignments(make_store(restored_dir / "coord.db"))

    with pytest.raises(dr_verify.DRVerifyError, match="parity check failed") as excinfo:
        dr_verify.check_parity(restored, live_assignments=5)

    assert "served 0 assignment" in str(excinfo.value)


def test_parity_fails_when_restored_serves_materially_fewer_than_live_beyond_tolerance(lane):
    """A restore that comes up and serves *something*, but far less than live
    and past the same lag tolerance `check_content` applies, is still a
    broken restore — not every shortfall is a total loss like the zero case
    above, so this needs its own named failure path.
    """
    pytest.importorskip("uvicorn")
    pushed = push_backup()
    restored = lane.scratch_root / "coord.db"
    dr_verify.fetch_snapshot(
        pushed.snapshot_id, restored, config=bk.BackupConfig.from_env()
    )

    with pytest.raises(dr_verify.DRVerifyError, match="parity check failed") as excinfo:
        dr_verify.check_parity(restored, live_assignments=10_000)

    assert "short by" in str(excinfo.value)


def test_parity_failure_names_both_board_comparands_not_a_table_count(lane):
    """The message must name what each side actually is (`/board` vs
    `/board`), so the next reader is not left guessing which side is
    filtered — the ambiguity that let #3135 ship in the first place.
    """
    pytest.importorskip("uvicorn")
    pushed = push_backup()
    restored = lane.scratch_root / "coord.db"
    dr_verify.fetch_snapshot(
        pushed.snapshot_id, restored, config=bk.BackupConfig.from_env()
    )

    with pytest.raises(dr_verify.DRVerifyError) as excinfo:
        dr_verify.check_parity(restored, live_assignments=10_000)

    message = str(excinfo.value)
    assert "/board (restored)" in message
    assert "/board (live)" in message


# ==========================================================================
# Acceptance: the scratch copy is removed on EVERY exit path
# ==========================================================================


def test_scratch_copy_is_removed_after_a_successful_run(lane):
    push_backup()
    assert run_cli(lane, "--no-parity").exit_code == 0
    assert list(lane.scratch_root.iterdir()) == []


def test_scratch_copy_is_removed_after_a_mid_restore_failure(lane, monkeypatch):
    """A restore that dies half-way still leaves a partial file behind — the
    cleanup has to run on the failure path, not just the happy one."""
    push_backup()

    def half_a_restore(snapshot_id, into, **kwargs):
        Path(into).write_bytes(b"SQLite format 3\x00truncated-mid-transfer")
        raise bk.BackupError("connection reset by peer")

    monkeypatch.setattr(bk, "restore", half_a_restore)
    report = dr_verify.verify(scratch_root=lane.scratch_root, parity=False)

    assert not report.ok
    assert "fetch failed" in report.failure
    assert list(lane.scratch_root.iterdir()) == []


def test_scratch_copy_is_removed_on_keyboard_interrupt(lane, monkeypatch):
    """^C is not a verify verdict: it propagates — but not before the scratch
    copy of the entire coordinator database is gone."""
    push_backup()

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(dr_verify, "check_structure", interrupted)

    with pytest.raises(KeyboardInterrupt):
        dr_verify.verify(scratch_root=lane.scratch_root, parity=False)

    assert list(lane.scratch_root.iterdir()) == []


def test_scratch_directory_is_named_so_a_leak_is_greppable(lane):
    """`ls /tmp | grep dr-verify` is a documented smoke test, so the prefix is
    part of the contract."""
    seen: list[str] = []
    with dr_verify.scratch_dir(lane.scratch_root) as scratch:
        seen.append(scratch.name)
    assert seen[0].startswith(dr_verify.SCRATCH_PREFIX)


# ==========================================================================
# Acceptance: freshness is its own failure condition
# ==========================================================================


def _write_record(path: Path, *, age_hours: float, outcome: str = "ok") -> None:
    import time

    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-01T00:00:00Z",
                "timestamp_epoch": time.time() - age_hours * 3600.0,
                "snapshot_id": "abc123",
                "outcome": outcome,
                "restore_seconds": 41.5,
                "failure": None if outcome == "ok" else "structural check failed: ...",
            }
        )
    )


def test_a_stale_last_verify_is_itself_a_failure(lane):
    """No run has to fail: a verify that has *not run* in the staleness window
    is the failure. That is what makes this check go stale-and-loud instead of
    silently stopping."""
    _write_record(lane.record, age_hours=30)

    result = CliRunner().invoke(main, ["dr", "status", "--record", str(lane.record)])

    assert result.exit_code == 1
    assert "staleness window" in result.output
    assert "30.0h ago" in result.output
    assert lane.alerts, "a stale DR verify must alert — nobody else is watching"


def test_a_fresh_successful_record_is_not_a_failure(lane):
    _write_record(lane.record, age_hours=1)
    result = CliRunner().invoke(main, ["dr", "status", "--record", str(lane.record)])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output
    assert lane.alerts == []


def test_a_missing_record_is_a_failure_not_a_shrug(lane):
    result = CliRunner().invoke(
        main, ["dr", "status", "--record", str(lane.record), "--no-alert"]
    )
    assert result.exit_code == 1
    assert "never been proven restorable" in result.output


def test_a_recent_but_failed_record_still_reports_failure(lane):
    _write_record(lane.record, age_hours=1, outcome="failed")
    result = CliRunner().invoke(
        main, ["dr", "status", "--record", str(lane.record), "--no-alert"]
    )
    assert result.exit_code == 1
    assert "failed" in result.output


def test_staleness_window_is_configurable_per_host(lane, monkeypatch):
    _write_record(lane.record, age_hours=8)
    assert not dr_verify.evaluate_staleness(path=lane.record, max_age_hours=6).ok
    monkeypatch.setenv(dr_verify.STALENESS_ENV, "24")
    assert dr_verify.evaluate_staleness(path=lane.record).ok


# ==========================================================================
# Acceptance: no credential in argv, in a log, or in an alert
# ==========================================================================


def test_no_credential_appears_in_any_emitted_log_record_or_alert(lane, caplog):
    """Driven against a tool whose stderr *does* echo the password back — the
    real leak shape (a repository URL carrying a SAS token), not a hypothesis
    about our own strings."""
    caplog.set_level("DEBUG")
    push_backup()

    result = run_cli(lane, "--snapshot", "no-such-snapshot", "--no-parity")
    assert result.exit_code == 1

    record_text = lane.record.read_text()
    alert_text = "\n".join(f"{m.title}\n{m.body}" for m in lane.alerts)
    assert lane.alerts, "the failure must have alerted, or this proves nothing"
    for surface in (result.output, caplog.text, record_text, alert_text):
        assert SECRET not in surface
    # ...and the redaction really happened rather than the message being empty.
    assert "***" in record_text
    assert "fetch failed" in json.loads(record_text)["failure"]
    # `restic` calls carry no secret in argv either — the shim logs every argv
    # it was invoked with, so this is observed, not asserted about our strings.
    assert SECRET not in (lane.repo / "calls.log").read_text()


def test_alert_goes_through_the_notifier_and_never_raises(lane, monkeypatch):
    """Route through the existing notifier, never a new transport — and an
    alert path that can take down the check it reports on is worse than a
    missed alert."""
    from coord.notifier.transport import NullTransport

    transport = MemoryTransport()
    assert dr_verify.alert("DR verify FAILED", "structural check failed: ...", transport=transport)
    assert transport.sent[0].title == "DR verify FAILED"
    assert "structural check failed" in transport.sent[0].body

    failing = MemoryTransport(fail=True)
    assert dr_verify.alert("t", "b", transport=failing) is False

    class Exploding:
        name = "exploding"

        def send(self, message):
            raise RuntimeError("ntfy is on fire")

    assert dr_verify.alert("t", "b", transport=Exploding()) is False
    # An unconfigured transport is reported loudly rather than counted as sent.
    assert dr_verify.alert("t", "b", transport=NullTransport()) is False


def test_default_transport_honors_the_notifications_master_switch(monkeypatch):
    """#1632's invariant, applied to this rung's alert path: every other
    caller of this exact machinery (coord/notifier/service.py,
    drive_queue.py's self-cordon escalation) skips sending when
    ``notifications.enabled`` is False, even with ntfy_url/ntfy_topic
    populated — an operator pausing notifications, or a host mid-setup. The
    one alert path in dr_verify must not be the exception."""
    from coord.notifier.transport import NullTransport

    class FakeCfg:
        def __init__(self, notifications):
            self.notifications = notifications

    populated_but_disabled = NotificationsConfig(
        enabled=False, ntfy_url="http://dellserver:7440", ntfy_topic="dr-verify"
    )
    monkeypatch.setattr(cfgmod, "load", lambda path: FakeCfg(populated_but_disabled))
    transport = dr_verify._default_transport()
    assert isinstance(transport, NullTransport), (
        "enabled=False must yield a NullTransport regardless of populated "
        "ntfy_url/ntfy_topic — the master switch, not field presence, decides"
    )

    # No notifications: block at all (cfg.notifications is None) — same result.
    monkeypatch.setattr(cfgmod, "load", lambda path: FakeCfg(None))
    assert isinstance(dr_verify._default_transport(), NullTransport)

    # enabled=True with a real (ntfy) transport configured actually builds one.
    enabled = NotificationsConfig(
        enabled=True, ntfy_url="http://dellserver:7440", ntfy_topic="dr-verify"
    )
    monkeypatch.setattr(cfgmod, "load", lambda path: FakeCfg(enabled))
    built = dr_verify._default_transport()
    assert not isinstance(built, NullTransport)


def test_an_unsnapshottable_backend_refuses_rather_than_reporting_green(
    lane, monkeypatch
):
    """#3085's lesson, applied to this rung: a check that cannot prove
    recovery for the configured backend must say so, not report green."""
    monkeypatch.setattr(bk, "resolve_backend", lambda: ("postgres", "host=db"))
    monkeypatch.setattr(
        dr_verify, "check_structure", lambda *a, **k: dr_verify.StepResult("structural", True)
    )
    monkeypatch.setattr(
        dr_verify, "fetch_snapshot", lambda *a, **k: 0.1
    )
    monkeypatch.setattr(dr_verify, "latest_snapshot", lambda *a, **k: {"id": "pg-1"})

    report = dr_verify.verify(
        config=bk.BackupConfig(repository="azure:x:/y"),
        scratch_root=lane.scratch_root,
        parity=False,
    )

    assert not report.ok
    assert "cannot prove recovery" in report.failure
    assert list(lane.scratch_root.iterdir()) == []
