"""#3333: the fan-out manifest merge must serialize across OS PROCESSES.

Why this file exists at all, rather than another class in
``tests/test_state.py``: the first fix round for #3333 guarded
``coord.state._merge_smoke_fanout_manifest_local``'s read-merge-write cycle
with a bare ``threading.Lock()``, and every test written for it drove the
merge sequentially in one process — so the suite went green while the only
topology the issue is actually about stayed broken.

That topology, from ``docs/AGENT_OPERATIONS.md``:

* the daemon host must NOT have ``client.toml``, so ``_board_service()``
  there resolves to ``None`` and every caller runs the merge **locally**
  instead of routing it to one shared daemon process;
* ``coord notify`` and ``coord drive-queue tick`` run on that host as two
  separate ``Type=oneshot`` systemd units — a brand-new Python process per
  firing, and the incident in #3333 caught both live at once (PIDs 3291642
  and 3291954).

Two such processes each have their own, unrelated ``threading.Lock()``
object, so a process-local lock serializes nothing between them: A's SELECT
can run before B's UPDATE commits, and whichever UPDATE lands last erases the
other's real, live leg from the parent's ``[[smoke-fanout:...]]`` manifest —
exactly the "last writer silently wins" gap the issue named.

So this test spawns **real subprocesses** against a **real on-disk
database**. Nothing here can be satisfied by a process-local lock; it passes
only because the merge takes ``coord.filelock.FileLock`` (``flock(2)``), the
cross-process advisory lock every other coord process already shares.

The race is made deterministic rather than hoped for: process A patches
``coord.state._record_test_verdict_local`` (in ITS OWN memory only — a test
seam that exists only inside the child script) to touch a marker file and
then sleep before writing, so it is guaranteed to be *inside* the critical
section, having already read, when process B starts its own merge. Without a
cross-process lock B reads the pre-A manifest and both writes name one
partition each; with it, B blocks until A's write lands and then folds it in.

SQLite-specific by construction (a second process has to be able to open the
same database *by path*), hence the skip on other backends and the bucket-A
entry in ``tests/test_sqlite_connect_ratchet.py``.

The second half of this file covers the OTHER side of the same seam: the
daemon's ``POST /smoke-fanout-merge``, which is where a thin client's merge
actually runs. Same read-merge-write, same lock, reached over HTTP.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coord import db, sql, state
from coord.db import _ensure_schema
from coord.models import Assignment
from coord.smoke import _parse_fanout_manifest
from tests.backends import BACKEND_SQLITE, active_backend

_REPO_ROOT = Path(__file__).resolve().parent.parent

# How long process A stays inside the critical section after reading. Long
# enough that a process-local-lock-only implementation loses the race every
# time (B's whole merge is microseconds), short enough not to slow the suite.
_HOLD_SECONDS = 3.0

_CHILD_SCRIPT = '''
"""One coord process doing one fan-out manifest merge (#3333 test child)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])

from coord import state

role, assignment_id, marker = sys.argv[2], sys.argv[3], sys.argv[4]
hold_seconds = float(sys.argv[5])
marker_path = Path(marker)

if role == "slow":
    # Patch this CHILD's own copy of the verdict writer so the process is
    # provably still inside the read-merge-write when the other one starts.
    real_write = state._record_test_verdict_local

    def _slow_write(*args, **kwargs):
        marker_path.write_text("inside the critical section")
        time.sleep(hold_seconds)
        return real_write(*args, **kwargs)

    state._record_test_verdict_local = _slow_write
    new_entries = [("leg-gtk-win", ("gtk", "windows"), "make smoke")]
else:
    deadline = time.monotonic() + 60
    while not marker_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not marker_path.exists():
        raise SystemExit("the slow child never entered its critical section")
    new_entries = [("leg-macos", ("macos",), "make smoke")]

state.merge_smoke_fanout_manifest(
    assignment_id=assignment_id, new_entries=new_entries, total_partitions=2,
)
print("ok", role)
'''


def _child_env(coord_dir: Path, home: Path, lock_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    # The child is a real, separate coord process: it resolves its own
    # database from $COORD_DIR and must not inherit pytest's guard rails.
    env["COORD_DIR"] = str(coord_dir)
    env["COORD_SMOKE_FANOUT_MANIFEST_LOCK"] = str(lock_path)
    # Model the DAEMON HOST, which `docs/AGENT_OPERATIONS.md` requires to
    # have no `client.toml`: `_board_service()` resolves to None there, so
    # the merge runs LOCALLY in each of these processes rather than being
    # funnelled through one shared daemon process. That is the whole reason
    # a process-local lock is not enough. (`coord.client.CLIENT_TOML` is
    # bound off `Path.home()` at import, hence $HOME rather than a patch.)
    env["HOME"] = str(home)
    env.pop("COORD_SERVICE_URL", None)
    env.pop("COORD_TOKEN", None)
    # coord.db refuses to open a $COORD_DIR database while this is set
    # (#1960's production guard); the child's database IS an isolated one.
    env.pop("PYTEST_CURRENT_TEST", None)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return env


@pytest.mark.skipif(
    active_backend() != BACKEND_SQLITE,
    reason="needs a database a second OS process can open by path",
)
def test_two_os_processes_merging_different_partitions_never_drop_either(
    tmp_path, coord_db,
) -> None:
    """Two sibling coord processes, each merging a DIFFERENT capability
    partition of the SAME parent, must both survive in the manifest.

    This is the literal `coord notify` / `coord drive-queue tick` pair from
    the issue's reproduction section. Before the cross-process lock, the
    second writer's UPDATE overwrote the first's manifest wholesale and the
    dropped leg's eventual pass/fail was never folded into the aggregate.
    """
    child_home = tmp_path / "home"
    child_home.mkdir()
    coord_dir = child_home / ".coord"
    coord_dir.mkdir()
    db_path = coord_dir / "coord.db"
    lock_path = coord_dir / "smoke-fanout-manifest.lock"

    # Seed the parent work row into the on-disk database the children will
    # both open. `override_connection` is restored to the autouse fixture's
    # connection before the assertions so nothing leaks out of this test.
    seed_conn = sqlite3.connect(str(db_path))
    try:
        sql.apply_row_factory(seed_conn)
        _ensure_schema(seed_conn)
        db.override_connection(seed_conn)
        state.record_dispatched_assignment(
            assignment=Assignment(
                machine_name="dell64", repo_name="quadraui", issue_number=952,
                issue_title="Some work", assignment_id="parent-work",
                type="work", status="done",
            ),
            repo_github="acme/quadraui",
        )
        seed_conn.commit()
    finally:
        db.override_connection(coord_db)
        seed_conn.close()

    script = tmp_path / "merge_child.py"
    script.write_text(_CHILD_SCRIPT)
    marker = tmp_path / "slow-child-is-inside-the-lock"
    env = _child_env(coord_dir, child_home, lock_path)

    def _spawn(role: str) -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable, str(script), str(_REPO_ROOT), role,
                "parent-work", str(marker), str(_HOLD_SECONDS),
            ],
            cwd=str(tmp_path),  # never pick up a checkout's ./coordinator.yml
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    slow = _spawn("slow")
    # Wait for the slow child to be provably inside its critical section
    # before the fast one even starts, so "B read before A wrote" is not a
    # matter of scheduling luck.
    deadline = time.monotonic() + 60
    while not marker.exists() and time.monotonic() < deadline:
        if slow.poll() is not None:
            break
        time.sleep(0.01)
    fast = _spawn("fast")

    slow_out, slow_err = slow.communicate(timeout=120)
    fast_out, fast_err = fast.communicate(timeout=120)
    assert slow.returncode == 0, f"slow child failed:\n{slow_err}\n{slow_out}"
    assert fast.returncode == 0, f"fast child failed:\n{fast_err}\n{fast_out}"

    # Read the result back from the file database the children wrote.
    verify_conn = sqlite3.connect(str(db_path))
    try:
        sql.apply_row_factory(verify_conn)
        row = sql.execute(
            verify_conn,
            "SELECT test_state, test_reason FROM assignments WHERE assignment_id=?",
            ("parent-work",),
        ).fetchone()
    finally:
        verify_conn.close()

    assert row is not None
    test_state = row["test_state"] if hasattr(row, "keys") else row[0]
    test_reason = row["test_reason"] if hasattr(row, "keys") else row[1]
    assert test_state == "running"

    legs = _parse_fanout_manifest(test_reason)
    assert legs is not None, f"no fan-out manifest survived: {test_reason!r}"
    assert {leg_id for leg_id, _, _ in legs} == {"leg-gtk-win", "leg-macos"}, (
        "a concurrent process's live leg was dropped from the parent's "
        f"manifest — the #3333 last-writer-wins bug: {test_reason!r}"
    )


def test_the_lock_file_is_shared_by_every_process_on_one_coord_dir(
    tmp_path, monkeypatch,
) -> None:
    """The guard is only real if both processes agree on the lock's path.

    Two coord processes on the same ``$COORD_DIR`` must resolve the same
    file; two pointed at different ``$COORD_DIR``s must not contend at all
    (they are different databases).
    """
    monkeypatch.delenv("COORD_SMOKE_FANOUT_MANIFEST_LOCK", raising=False)
    monkeypatch.setenv("COORD_DIR", str(tmp_path / "a"))
    first = state.smoke_fanout_manifest_lock_path()
    second = state.smoke_fanout_manifest_lock_path()
    assert first == second
    assert first.parent == tmp_path / "a"

    monkeypatch.setenv("COORD_DIR", str(tmp_path / "b"))
    assert state.smoke_fanout_manifest_lock_path() != first


def test_a_held_lock_does_not_wedge_the_merge_forever(tmp_path, monkeypatch, coord_db) -> None:
    """A lock held past the timeout degrades to an unlocked merge rather
    than failing (or hanging) the dispatch — the same deliberate fallback
    ``coord/confirm_test.py`` documents for its own drain lock.

    Asserted here because the alternative failure mode (a `coord notify`
    tick blocking forever on a lock some other process leaked) would be
    strictly worse than the race this lock closes.
    """
    from coord.filelock import FileLock

    lock_path = tmp_path / "held.lock"
    monkeypatch.setenv("COORD_SMOKE_FANOUT_MANIFEST_LOCK", str(lock_path))
    monkeypatch.setattr(state, "_SMOKE_FANOUT_MANIFEST_LOCK_TIMEOUT", 0.0)

    state.record_dispatched_assignment(
        assignment=Assignment(
            machine_name="dell64", repo_name="quadraui", issue_number=952,
            issue_title="Some work", assignment_id="parent-work",
            type="work", status="done",
        ),
        repo_github="acme/quadraui",
    )

    holder = FileLock(lock_path)
    holder.acquire(timeout=0.0)
    try:
        test_state, test_reason = state.merge_smoke_fanout_manifest(
            assignment_id="parent-work",
            new_entries=[("leg-macos", ("macos",), "make smoke")],
            total_partitions=2,
        )
    finally:
        holder.release()

    assert test_state == "running"
    assert "leg-macos" in (test_reason or "")


# ── the daemon side of the same seam: POST /smoke-fanout-merge ───────────────
#
# This is the route a THIN CLIENT's merge actually runs on (the daemon owns
# the canonical DB), so it is the other half of the guarantee above: the
# cross-process lock only means something if every writer reaches the same
# read-merge-write, whether it got there by a local call or over HTTP.


@pytest.fixture
def rw_db(tmp_path: Path):
    """Thread-safe file-backed DB for TestClient (mirrors test_serve.py)."""
    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    sql.apply_row_factory(conn)
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


@pytest.fixture
def file_db(tmp_path: Path) -> Path:
    """Minimal on-disk coord.db for SqliteStore (read-only DAO)."""
    path = tmp_path / "coord.db"
    conn = sqlite3.connect(str(path))
    sql.apply_row_factory(conn)
    _ensure_schema(conn)
    conn.commit()
    conn.close()
    return path


def _seed_parent(conn) -> None:
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, "
        " issue_number, issue_title, status, type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("parent-work", "dell64", "quadraui", "acme/quadraui", 952, "t", "done", "work"),
    )
    conn.commit()


@pytest.mark.skipif(
    active_backend() != BACKEND_SQLITE,
    reason="SqliteStore resolves the daemon's database by path",
)
def test_daemon_merge_route_folds_two_partitions_into_one_manifest(
    file_db: Path, valid_config_path: Path, rw_db,
) -> None:
    """Two thin-client merges for DIFFERENT partitions, both routed to the
    daemon, must both survive — the remote twin of the cross-process test at
    the top of this file."""
    from starlette.testclient import TestClient

    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    _seed_parent(rw_db)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        first = cli.post(
            "/smoke-fanout-merge",
            json={
                "assignment_id": "parent-work",
                "total_partitions": 2,
                "new_entries": [["leg-gtk-win", ["gtk", "windows"], "make smoke"]],
            },
        )
        second = cli.post(
            "/smoke-fanout-merge",
            json={
                "assignment_id": "parent-work",
                "total_partitions": 2,
                "new_entries": [["leg-macos", ["macos"], "make smoke"]],
            },
        )

    assert first.status_code == 200 and first.json()["ok"] is True
    assert second.status_code == 200
    # The response carries what the row ACTUALLY holds, so the caller can
    # mirror it rather than re-asserting its own partial view.
    assert second.json()["test_state"] == "running"
    assert "leg-gtk-win" in second.json()["test_reason"]

    row = rw_db.execute(
        "SELECT test_state, test_reason FROM assignments "
        "WHERE assignment_id='parent-work'"
    ).fetchone()
    legs = _parse_fanout_manifest(row["test_reason"])
    assert legs is not None
    assert {leg_id for leg_id, _, _ in legs} == {"leg-gtk-win", "leg-macos"}


@pytest.mark.skipif(
    active_backend() != BACKEND_SQLITE,
    reason="SqliteStore resolves the daemon's database by path",
)
def test_daemon_merge_route_rejects_a_body_missing_a_field(
    file_db: Path, valid_config_path: Path, rw_db,
) -> None:
    from starlette.testclient import TestClient

    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/smoke-fanout-merge",
            json={"assignment_id": "parent-work", "total_partitions": 2},
        )
    assert resp.status_code == 400
