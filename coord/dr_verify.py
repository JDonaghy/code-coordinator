"""Continuously prove the off-site backup actually restores (#3119).

**Rung D2 of epic #3117.** :mod:`coord.backup` (D0) makes an off-box copy
*exist*; nothing before this module proved that copy can be turned back into a
working board. Every failure mode that matters here is silent by nature — an
expired credential, a retention policy that pruned the wrong side, a snapshot
that uploads fine and restores to a truncated file, a schema migration that
landed on the live DB but not on whatever the restore path reconstructs. None
announce themselves, and all are discovered at the worst possible moment.

The fleet already owns the cautionary version: until #3085,
``coord-db-backup.sh`` would have printed a cheerful green "ok" for a snapshot
of a dead pre-cutover file. **Verifying the artifact is not the same as
verifying the recovery**, so this module does the recovery — every run, end to
end:

1. **Fetch** the latest off-site snapshot through D0's ``restore --into``.
   Deliberately *not* a re-read of the local copy: exercising the off-site
   path, credentials included, is the whole point.
2. **Structural check** — ``PRAGMA integrity_check`` (SQLite) or a
   ``pg_restore --list`` round trip (Postgres).
3. **Content check** — per-table row counts against live, within a tolerance
   that accounts for the snapshot lagging live by up to one backup interval.
   A table that is *empty* in the restore while live is not is a hard failure
   regardless of tolerance — that is the shape a truncated-but-openable
   restore takes, and `integrity_check` says "ok" to it.
4. **Schema check** — the restored schema matches what the *installed* coord
   expects. A backup that restores cleanly but is three migrations behind is a
   recovery that fails at daemon start.
5. **Parity check** — boot a throwaway ``coord serve`` against the scratch DB
   on an ephemeral port and ``GET /board``, then compare its served assignment
   count against what ``/board`` serves for the *live* store — never against
   the live store's raw table count, which #762's retention cap always makes
   larger once a board has any history (#3135). This is the check that proves
   *recoverability* rather than merely *restorability*.
6. **Clean up** the scratch copy on every exit path, including a mid-restore
   failure and a ``KeyboardInterrupt``.
7. **Report** — success is quiet; failure alerts.

**Freshness is its own failure.** A verify that has not run in
:data:`DEFAULT_STALENESS_HOURS` hours is a failure, not an absence of news, so
:func:`evaluate_staleness` is a first-class condition that needs no run to fail
— it is what makes this check go stale-and-loud rather than silent when it
stops running at all. :func:`write_record` persists ``last_verify.json``
(timestamp, snapshot id, outcome, measured restore duration); the restore
duration is #3117's Domain-A RTO input, recorded rather than estimated later.

**The alert must not share a failure domain with the thing it watches.** The
2026-08-22 venv incident fired detection correctly into an alert channel that
was inside the blast radius. Two mitigations here, both deliberately cheap:
the alert goes out over the existing notifier (an *off-box* ntfy server —
never a new transport, per ``docs/NOTIFIER.md``), and ``last_verify.json`` is
mirrored to every path in ``$COORD_DR_VERIFY_MIRROR`` so a *different* machine
can read the last-success timestamp and apply :func:`evaluate_staleness`
itself when the daemon host is the thing that died.

**No credential ever reaches argv or a log line.** This module builds no
restic argv of its own (``coord.backup`` owns that seam, and routes every
secret through the child environment); the one credential it mints — the
throwaway daemon's bearer token — travels in the child's environment too, and
everything logged, persisted or alerted goes through :func:`scrub` first.

Out of scope, per the issue: standing up a replacement server (D3/D4), the
full ``coord dr drill`` (D6), and any form of self-repair. This alerts; a
human decides.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from coord import backup, sql

LOGGER_NAME = "coord.dr_verify"

#: A verify older than this is itself a failure (#3119: "a verify that has not
#: run in 12 hours is a failure, not an absence of news"). Overridable per host
#: via ``$COORD_DR_VERIFY_MAX_AGE_HOURS`` so a fleet running the timer at a
#: different cadence does not have to patch code to stay honest.
DEFAULT_STALENESS_HOURS = 12.0
STALENESS_ENV = "COORD_DR_VERIFY_MAX_AGE_HOURS"

#: ``os.pathsep``-separated extra paths ``last_verify.json`` is copied to.
#: The off-host half of the alert's failure-domain separation — point this at
#: a synced/exported directory another machine can read.
MIRROR_ENV = "COORD_DR_VERIFY_MIRROR"

#: Scratch directories are named so an operator can spot a leaked one with
#: `ls /tmp | grep dr-verify` — which is a smoke test in the issue, so the
#: prefix is part of the contract, not an implementation detail.
SCRATCH_PREFIX = "dr-verify-"

#: Row-count tolerance for the content check. The snapshot legitimately lags
#: live by up to one backup interval, so *some* shortfall is expected and only
#: a shortfall past BOTH bounds is a failure: proportional for big tables,
#: absolute so a 3-row table is not condemned for being one row behind.
DEFAULT_TOLERANCE_FRACTION = 0.10
DEFAULT_TOLERANCE_ROWS = 50

#: How long the throwaway daemon gets to answer `GET /board` before the parity
#: step calls it dead.
PARITY_BOOT_TIMEOUT = 60.0

#: The notifier condition this lane raises under. Not in
#: ``coord.notifier.models.CONDITION_ORDER`` on purpose — `condition_rank`
#: ranks an unknown condition last rather than raising, and this event is
#: delivered directly rather than competing in the per-subject ranking.
ALERT_CONDITION = "dr_verify_failed"
ALERT_SUBJECT = "dr-verify"


class DRVerifyError(RuntimeError):
    """A check failed, or could not be run at all.

    Always safe to print: every message is built through :func:`scrub`.
    """


@dataclass(frozen=True)
class StepResult:
    """One named check, and whether it passed."""

    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class VerifyReport:
    """What one `coord dr verify` run learned.

    ``restore_seconds`` is the number #3117 wants for its RTO estimate, which
    is why it is measured and persisted rather than inferred from the total.
    """

    ok: bool
    snapshot_id: str
    started_at: float
    duration_seconds: float = 0.0
    restore_seconds: float = 0.0
    steps: list[StepResult] = field(default_factory=list)
    failure: str | None = None
    backend: str = sql.DIALECT_SQLITE

    def to_record(self, *, host: str | None = None) -> dict[str, Any]:
        """The ``last_verify.json`` body."""
        return {
            "timestamp": _isoformat(self.started_at),
            "timestamp_epoch": self.started_at,
            "snapshot_id": self.snapshot_id,
            "outcome": "ok" if self.ok else "failed",
            "restore_seconds": round(self.restore_seconds, 3),
            "duration_seconds": round(self.duration_seconds, 3),
            "backend": self.backend,
            "host": host or _hostname(),
            "failure": self.failure,
            "steps": [s.to_dict() for s in self.steps],
        }

    def summary(self) -> str:
        if self.ok:
            return (
                f"dr verify: ok snapshot={self.snapshot_id[:12]} "
                f"restore={self.restore_seconds:.1f}s total={self.duration_seconds:.1f}s"
            )
        return f"dr verify: FAILED snapshot={self.snapshot_id[:12] or '?'}: {self.failure}"


@dataclass(frozen=True)
class Staleness:
    """The freshness verdict for a persisted ``last_verify.json``."""

    ok: bool
    reason: str
    age_seconds: float | None = None
    record: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Secret hygiene
# --------------------------------------------------------------------------


def scrub(text: str, *, extra: Iterable[str] = ()) -> str:
    """:func:`coord.backup.scrub`, plus any secret this module itself minted.

    ``coord.backup.scrub`` already redacts every credential *value* present in
    the environment. *extra* carries the throwaway daemon's bearer token,
    which exists only for the lifetime of one parity check and is therefore in
    no environment `backup.scrub` knows about.
    """
    out = backup.scrub(text)
    for secret in extra:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***")
    return out


def _log() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def _isoformat(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - gethostname does not realistically fail
        return "?"


# --------------------------------------------------------------------------
# last_verify.json — freshness is its own failure condition
# --------------------------------------------------------------------------


def last_verify_path() -> Path:
    """``~/.coord/last_verify.json`` (re-resolved against ``$COORD_DIR``)."""
    from coord import db as _db  # noqa: PLC0415

    return Path(_db.COORD_DIR) / "last_verify.json"


def mirror_paths(env: dict[str, str] | None = None) -> list[Path]:
    """Extra destinations for ``last_verify.json``, from ``$COORD_DR_VERIFY_MIRROR``.

    The failure-domain separation the issue insists on: an alert that
    originates on the daemon host is not an alert when the daemon host is what
    died, so the last-success timestamp has to be readable from somewhere
    else. Empty by default — a mirror nobody configured is not a failure, it
    is simply the single-host default.
    """
    env = os.environ if env is None else env
    raw = (env.get(MIRROR_ENV) or "").strip()
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def staleness_window_hours(env: dict[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    raw = (env.get(STALENESS_ENV) or "").strip()
    if not raw:
        return DEFAULT_STALENESS_HOURS
    try:
        value = float(raw)
    except ValueError:
        raise DRVerifyError(f"${STALENESS_ENV} must be a number, got {raw!r}") from None
    if value <= 0:
        raise DRVerifyError(f"${STALENESS_ENV} must be > 0, got {value}")
    return value


def write_record(
    report: VerifyReport,
    *,
    path: Path | None = None,
    mirrors: Sequence[Path] | None = None,
    host: str | None = None,
) -> Path:
    """Persist *report* as ``last_verify.json``, plus every mirror.

    Written on **success and failure alike**: a record saying the last run
    failed is the difference between "broken" and "stopped running", and the
    staleness check needs to be able to tell those apart.

    A mirror that cannot be written is logged and skipped — a broken NFS mount
    must not turn a passing verify into a failing one, because that would put
    the alerting path back inside the failure domain it is supposed to escape.
    """
    path = last_verify_path() if path is None else Path(path)
    mirrors = mirror_paths() if mirrors is None else [Path(m) for m in mirrors]
    body = json.dumps(report.to_record(host=host), indent=2, sort_keys=True) + "\n"
    body = scrub(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    for mirror in mirrors:
        # A mirror entry is a directory unless it names a `.json` file
        # outright. Decided by the *spelling*, never by whether the path
        # happens to exist yet: a mount that was not ready the first time
        # this ran must not silently switch a directory mirror into a file
        # called `offbox`, which is then never a directory again.
        dest = mirror if mirror.suffix == ".json" else mirror / path.name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body, encoding="utf-8")
        except OSError as exc:
            _log().warning("could not mirror %s to %s: %s", path.name, dest, exc)
    return path


def read_record(path: Path | None = None) -> dict[str, Any] | None:
    """The persisted record, or ``None`` when absent/unreadable."""
    path = last_verify_path() if path is None else Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def evaluate_staleness(
    *,
    path: Path | None = None,
    now: float | None = None,
    max_age_hours: float | None = None,
) -> Staleness:
    """Is the *last* verify recent enough to be believed?

    Three distinct not-ok answers, deliberately not collapsed into one: no
    record at all (the lane has never run, or its state was lost), a record
    whose last run *failed* (the alert for that already fired, but the
    condition is still true), and a record that is simply too old — which is
    the one that catches the timer silently no longer firing, the failure this
    whole rung exists to make loud.
    """
    now = time.time() if now is None else now
    window = staleness_window_hours() if max_age_hours is None else float(max_age_hours)
    record = read_record(path)
    if record is None:
        return Staleness(
            ok=False,
            reason=(
                f"no DR verify has ever been recorded at "
                f"{last_verify_path() if path is None else path} — the backup has "
                "never been proven restorable on this host"
            ),
        )
    stamp = record.get("timestamp_epoch")
    try:
        stamp = float(stamp)
    except (TypeError, ValueError):
        return Staleness(
            ok=False,
            reason="last_verify.json carries no usable timestamp",
            record=record,
        )
    age = now - stamp
    if age > window * 3600.0:
        return Staleness(
            ok=False,
            reason=(
                f"last DR verify was {age / 3600.0:.1f}h ago "
                f"({record.get('timestamp')}), past the {window:.1f}h staleness "
                "window — nothing is proving the off-site backup restores"
            ),
            age_seconds=age,
            record=record,
        )
    if str(record.get("outcome")) != "ok":
        return Staleness(
            ok=False,
            reason=(
                f"the last DR verify ({record.get('timestamp')}) failed: "
                f"{record.get('failure')}"
            ),
            age_seconds=age,
            record=record,
        )
    return Staleness(
        ok=True,
        reason=f"last DR verify {age / 3600.0:.1f}h ago, outcome ok",
        age_seconds=age,
        record=record,
    )


# --------------------------------------------------------------------------
# Step 1 — fetch, through D0's restore path
# --------------------------------------------------------------------------


def latest_snapshot(
    config: backup.BackupConfig,
    *,
    runner: backup.ResticRunner | None = None,
) -> dict[str, Any]:
    """The newest snapshot this lane owns.

    Raises rather than silently verifying nothing: an empty repository is the
    single loudest thing this check can find (retention pruned the wrong side,
    or the push lane has never run at all).
    """
    snapshots = backup.list_snapshots(config, runner=runner)
    if not snapshots:
        raise DRVerifyError(
            "the off-site repository holds no snapshots at all — there is "
            "nothing to restore, so there is no backup"
        )
    newest = max(snapshots, key=lambda s: str(s.get("time", "")))
    if not newest.get("id"):
        raise DRVerifyError("the newest snapshot has no id — cannot restore it")
    return newest


def fetch_snapshot(
    snapshot_id: str,
    dest: Path,
    *,
    config: backup.BackupConfig,
    runner: backup.ResticRunner | None = None,
) -> float:
    """Restore *snapshot_id* to *dest*; return the measured seconds it took.

    ``verify=False`` on purpose. D0's restore would otherwise run its own
    SQLite sanity check and raise `VerifyFailure` before this module ever sees
    the file — which would make a truncated snapshot surface as a backup-lane
    error instead of a *named structural failure* from the check whose job
    that is.
    """
    started = time.monotonic()
    try:
        backup.restore(snapshot_id, dest, config=config, runner=runner, verify=False)
    except backup.BackupError as exc:
        raise DRVerifyError(f"fetch failed: {scrub(str(exc))}") from None
    return time.monotonic() - started


# --------------------------------------------------------------------------
# Step 2 — structural
# --------------------------------------------------------------------------


def check_structure(
    path: Path,
    *,
    backend: str = sql.DIALECT_SQLITE,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> StepResult:
    """``PRAGMA integrity_check`` (SQLite) / ``pg_restore --list`` (Postgres)."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        raise DRVerifyError(
            "structural check failed: the restored snapshot is missing or zero-length"
        )
    if backend == sql.DIALECT_POSTGRES:
        try:
            backup.verify_postgres_snapshot(path, runner=runner)
        except backup.VerifyFailure as failure:
            raise DRVerifyError(
                f"structural check failed: {scrub(failure.reason)}"
            ) from None
        return StepResult("structural", True, "pg_restore --list round trip ok")

    try:
        conn = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=path, read_only=True)
    except sql.driver_errors() as exc:
        raise DRVerifyError(
            f"structural check failed: cannot open the restored snapshot: {exc}"
        ) from None
    try:
        try:
            verdict = sql.sqlite_integrity_check(conn)
        except sql.driver_errors() as exc:
            raise DRVerifyError(
                f"structural check failed: integrity_check could not run "
                f"({exc}) — the restored file is not a usable database"
            ) from None
        if verdict != "ok":
            raise DRVerifyError(f"structural check failed: integrity_check: {verdict}")
    finally:
        conn.close()
    # Deliberately not spelling the PRAGMA in a string literal: #2782's
    # dialect ratchet (tests/test_sql_dialect.py) reads any SQLite-only
    # construct in statement text outside `coord/db.py` and the `coord.sql`
    # seam as a leak, and it cannot tell a detail line from a query. The
    # statement itself is `sql.sqlite_integrity_check`'s, where it belongs.
    return StepResult("structural", True, "integrity_check: ok")


# --------------------------------------------------------------------------
# Step 3 — content
# --------------------------------------------------------------------------


def table_counts(path: Path) -> dict[str, int]:
    """``{table: row count}`` for every user table in a SQLite database."""
    conn = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=Path(path), read_only=True)
    try:
        names = [
            row[0]
            for row in sql.execute(
                conn,
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name",
            ).fetchall()
        ]
        return {
            name: int(sql.execute(conn, f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in names
        }
    finally:
        conn.close()


def allowed_shortfall(
    live_rows: int,
    *,
    fraction: float = DEFAULT_TOLERANCE_FRACTION,
    absolute: int = DEFAULT_TOLERANCE_ROWS,
) -> int:
    """How many rows a table may legitimately be behind live by.

    The snapshot lags live by up to one backup interval, so the bound is the
    looser of a proportional and an absolute allowance: proportional keeps a
    large table honest, absolute keeps a small one from failing because a
    single row landed after the snapshot.
    """
    return max(absolute, int(math.ceil(live_rows * fraction)))


def check_content(
    restored: dict[str, int],
    live: dict[str, int],
    *,
    fraction: float = DEFAULT_TOLERANCE_FRACTION,
    absolute: int = DEFAULT_TOLERANCE_ROWS,
) -> StepResult:
    """Per-table row counts against live, within tolerance.

    **Empty-in-restore while non-empty-live is a hard failure regardless of
    tolerance.** That is exactly the shape a truncated-but-openable restore
    takes, and `integrity_check` is perfectly happy with it — so a purely
    proportional tolerance would wave through the one content failure most
    likely to actually happen.

    A table present live and *absent* from the restore is the same class of
    failure and is reported the same way. The reverse (a table in the restore
    that live no longer has) is not: that is a migration that dropped a table
    after the snapshot was taken, which is not evidence the backup is bad.
    """
    problems: list[str] = []
    for name, live_rows in sorted(live.items()):
        if name not in restored:
            if live_rows > 0:
                problems.append(f"{name}: missing from the restore ({live_rows} rows live)")
            continue
        got = restored[name]
        if live_rows > 0 and got == 0:
            problems.append(
                f"{name}: empty in the restore but {live_rows} rows live — "
                "a restored-but-empty table is a hard failure, tolerance does "
                "not apply"
            )
            continue
        shortfall = live_rows - got
        budget = allowed_shortfall(live_rows, fraction=fraction, absolute=absolute)
        if shortfall > budget:
            problems.append(
                f"{name}: {got} rows restored vs {live_rows} live "
                f"(short by {shortfall}, tolerance {budget})"
            )
    if problems:
        raise DRVerifyError("content check failed: " + "; ".join(problems))
    return StepResult(
        "content", True, f"{len(live)} table(s) within tolerance of live"
    )


# --------------------------------------------------------------------------
# Step 4 — schema
# --------------------------------------------------------------------------


def installed_schema_version() -> int:
    """The schema version the *installed* coord expects to open."""
    from coord import db as _db  # noqa: PLC0415

    return int(_db._DB_SCHEMA_VERSION)


def restored_schema_version(path: Path) -> int:
    """``MAX(version)`` from the restored store's ``schema_version`` table.

    ``0`` when the table is absent — a pre-#2598 database, or something that
    is not a coord store at all. Both are a schema gap, and both are named as
    one rather than crashing.

    Delegates to :func:`coord.db._read_schema_version` rather than
    re-running the same query by hand — that function's docstring documents
    non-obvious care (notably the #2983 rollback a Postgres "table doesn't
    exist yet" abort needs before the connection is usable again) that a
    from-scratch reimplementation here would silently drop.
    """
    from coord import db as _db  # noqa: PLC0415

    conn = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=Path(path), read_only=True)
    try:
        return _db._read_schema_version(conn)
    finally:
        conn.close()


def check_schema(path: Path, *, expected: int | None = None) -> StepResult:
    """The restored schema must match what the installed coord expects.

    A backup that restores cleanly but is three migrations behind is a
    recovery that fails at daemon start, so the message names *both* versions
    and the size of the gap: "two migrations behind" is the actionable fact,
    not "schema mismatch".

    A restore *ahead* of the installed coord is also a failure, and a
    differently-shaped one: it means this host is running older code than the
    store it would have to recover into.
    """
    want = installed_schema_version() if expected is None else int(expected)
    got = restored_schema_version(path)
    if got == want:
        return StepResult("schema", True, f"schema version {got} matches the installed coord")
    if got < want:
        gap = want - got
        raise DRVerifyError(
            f"schema check failed: the restored store is at schema version "
            f"{got}, the installed coord expects {want} — the backup is "
            f"{gap} migration{'s' if gap != 1 else ''} behind, so restoring it "
            "would not come up as a working board"
        )
    raise DRVerifyError(
        f"schema check failed: the restored store is at schema version {got}, "
        f"ahead of the installed coord's {want} — this host is running older "
        "code than the store it would have to recover into"
    )


# --------------------------------------------------------------------------
# Step 5 — parity: a real daemon, a real GET /board
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def serve_argv(config_path: Path, port: int) -> list[str]:
    """argv for the throwaway daemon.

    Carries **no credential**: the bearer token the parity step mints travels
    in the child's environment (see :func:`serve_env`), never here, because
    argv is world-readable in ``/proc`` — the same rule ``coord.backup``
    applies to restic and ``pg_dump``.
    """
    return [
        sys.executable,
        "-m",
        "coord.cli",
        "serve",
        "--config",
        str(config_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def serve_env(scratch_dir: Path, token: str) -> dict[str, str]:
    """Child environment for the throwaway daemon.

    ``COORD_DIR`` is the scratch directory, so the daemon opens the *restored*
    ``coord.db`` and can touch nothing of the live host's state.
    ``PYTEST_CURRENT_TEST`` is stripped because ``coord.db``'s production
    guard refuses to open "the" database under pytest — and under this
    module's own tests, "the" database is the scratch copy, which is precisely
    what we want opened.

    #3135: deliberately does **not** force ``COORD_BOARD_RETENTION_DAYS``.
    #762 caps `/board`'s assignments to active + pipeline-referenced + the
    last ``COORD_BOARD_RETENTION_DAYS`` of terminal rows, so a real fleet
    board's `/board` count is *always* smaller than its raw `assignments`
    table once it has any history. A parity check that disabled the cap here
    and then compared against the raw table count (the pre-#3135 shape of
    this function) was comparing a filtered view to an unfiltered one and
    failed permanently on production. The throwaway daemon now inherits
    whatever retention window the parent process has — the same one the
    *live* board applies (see :func:`live_board_assignment_count`) — so both
    sides of the parity comparison are `/board` answers, filtered identically.
    """
    env = dict(os.environ)
    env["COORD_DIR"] = str(scratch_dir)
    env["COORD_SERVE_TOKEN"] = token
    env.pop("PYTEST_CURRENT_TEST", None)
    # A stray client.toml/COORD_SERVICE_URL must not make the throwaway
    # daemon proxy someone else's board (#2824's failure, in miniature).
    env.pop("COORD_SERVICE_URL", None)
    return env


def live_board_assignment_count(live_db_path: Path) -> int:
    """How many assignments the **live** `/board` serves, right now.

    #3135: the comparand the restored side's `/board` count must be checked
    against is another `/board` count — never the live store's raw table
    count, which is always larger once a board has any history (#762's
    retention cap). There is no need to *boot* a second daemon to answer this:
    the live daemon (if one is even running) is not what is being tested here
    — only "what would `/board` report for this file" is needed, and that is
    exactly :meth:`coord.dao.SqliteStore.board_projection`, the same function
    `coord/serve_app.py`'s `/board` route calls. Calling it directly, read-only,
    against the live file is the "one question, one answer" version of asking
    a second daemon to boot just to answer a question the daemon already
    knows how to answer without booting.
    """
    from coord.dao import SqliteStore  # noqa: PLC0415

    try:
        payload = SqliteStore(live_db_path).board_projection()
    except sql.driver_errors() as exc:
        raise DRVerifyError(
            f"parity check failed: could not read the live board's /board "
            f"count from {live_db_path}: {exc}"
        ) from None
    rows = payload.get("assignments")
    if not isinstance(rows, list):
        raise DRVerifyError(
            "parity check failed: the live store's /board projection carried "
            "no `assignments` list — cannot establish what /board (live) serves"
        )
    return len(rows)


def check_parity(
    db_path: Path,
    *,
    config_path: Path | None = None,
    live_assignments: int | None = None,
    tolerance_fraction: float = DEFAULT_TOLERANCE_FRACTION,
    tolerance_rows: int = DEFAULT_TOLERANCE_ROWS,
    timeout: float = PARITY_BOOT_TIMEOUT,
) -> StepResult:
    """Boot a real ``coord serve`` against the scratch DB and ``GET /board``.

    The check that proves *recoverability* rather than *restorability*: a file
    that opens, passes integrity_check and carries the right rows can still
    fail to come up as a board (a schema the daemon's projections do not
    understand, a JSON column that no longer decodes). Nothing short of
    booting the daemon finds that.

    *live_assignments* is what `/board` (live) serves right now — see
    :func:`live_board_assignment_count` — **not** a raw table count (#3135).
    Compared with the same lag tolerance :func:`check_content` applies, plus a
    hard zero-check tolerance cannot waive: a restore that serves 0 while live
    serves any is a broken restore no matter how small live's count is (the
    generic tolerance formula's absolute floor would otherwise wave through
    "0 of 10" as within budget).
    """
    from coord.config import resolve_config_path  # noqa: PLC0415

    db_path = Path(db_path)
    scratch_dir = db_path.parent
    config_path = resolve_config_path() if config_path is None else Path(config_path)
    token = secrets.token_urlsafe(24)
    port = _free_port()
    argv = serve_argv(config_path, port)
    _log().info("parity: %s", scrub(" ".join(argv), extra=[token]))

    proc = subprocess.Popen(  # noqa: S603 — argv is built here, never shell
        argv,
        env=serve_env(scratch_dir, token),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        payload = _poll_board(proc, port, token, timeout=timeout)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()

    if not isinstance(payload, dict) or "schema_version" not in payload:
        raise DRVerifyError(
            "parity check failed: the throwaway daemon answered /board with "
            "something that is not a board payload"
        )
    rows = payload.get("assignments")
    if not isinstance(rows, list):
        raise DRVerifyError(
            "parity check failed: /board carried no `assignments` list — the "
            "restored store did not project as a board"
        )
    served = len(rows)
    if live_assignments is not None:
        # Hard zero-check, ahead of tolerance: a restore that serves nothing
        # while live serves plenty is the daemon-can't-read-it failure this
        # step exists to catch, and must fail even when live's count is small
        # enough that the tolerance budget below would otherwise wave a
        # shortfall of exactly `live_assignments` through.
        if live_assignments > 0 and served == 0:
            raise DRVerifyError(
                f"parity check failed: /board (restored) served 0 assignment(s) "
                f"while /board (live) serves {live_assignments} — the restore "
                "does not come up as the board it was taken from"
            )
        shortfall = live_assignments - served
        budget = allowed_shortfall(
            live_assignments, fraction=tolerance_fraction, absolute=tolerance_rows
        )
        if shortfall > budget:
            raise DRVerifyError(
                f"parity check failed: /board (restored) served {served} "
                f"assignment(s), /board (live) serves {live_assignments} "
                f"(short by {shortfall}, tolerance {budget}) — the restore "
                "does not come up as the board it was taken from"
            )
    return StepResult(
        "parity",
        True,
        f"/board (restored) served {served} assignment(s)"
        + (
            f", /board (live) serves {live_assignments}"
            if live_assignments is not None
            else ""
        ),
    )


def _poll_board(
    proc: subprocess.Popen, port: int, token: str, *, timeout: float
) -> Any:
    """Wait for the daemon to answer ``GET /board``, or explain why it never did."""
    import httpx  # noqa: PLC0415

    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/board"
    headers = {"Authorization": f"Bearer {token}"}
    last_error = "never answered"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = scrub((proc.stdout.read() if proc.stdout else "") or "", extra=[token])
            raise DRVerifyError(
                f"parity check failed: the throwaway daemon exited "
                f"({proc.returncode}) before serving /board: {output.strip()[-800:]}"
            )
        try:
            response = httpx.get(url, headers=headers, timeout=5.0)
        except Exception as exc:  # noqa: BLE001 — still booting, or genuinely dead
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)
            continue
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise DRVerifyError(
                    f"parity check failed: /board returned unparseable JSON: {exc}"
                ) from None
        last_error = f"HTTP {response.status_code}"
        time.sleep(0.2)
    raise DRVerifyError(
        f"parity check failed: the throwaway daemon did not serve /board within "
        f"{timeout:.0f}s ({scrub(last_error, extra=[token])})"
    )


# --------------------------------------------------------------------------
# The lane
# --------------------------------------------------------------------------


class scratch_dir:
    """A scratch directory that is removed on **every** exit path.

    A plain `finally` around the body is not enough on its own — the issue
    names `KeyboardInterrupt` and a mid-restore failure explicitly — so this
    is a context manager whose `__exit__` runs for BaseException too, and
    which never lets a cleanup error mask the real one.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root).expanduser() if root else None
        self.path: Path | None = None

    def __enter__(self) -> Path:
        if self._root is not None:
            self._root.mkdir(parents=True, exist_ok=True)
        self.path = Path(
            tempfile.mkdtemp(
                prefix=SCRATCH_PREFIX, dir=str(self._root) if self._root else None
            )
        )
        return self.path

    def __exit__(self, *exc: Any) -> None:
        if self.path is None:
            return
        try:
            shutil.rmtree(self.path, ignore_errors=True)
        finally:
            self.path = None


def verify(
    *,
    snapshot_id: str | None = None,
    config: backup.BackupConfig | None = None,
    runner: backup.ResticRunner | None = None,
    scratch_root: Path | None = None,
    live_db: Path | None = None,
    config_path: Path | None = None,
    expected_schema_version: int | None = None,
    parity: bool = True,
    tolerance_fraction: float = DEFAULT_TOLERANCE_FRACTION,
    tolerance_rows: int = DEFAULT_TOLERANCE_ROWS,
    parity_timeout: float = PARITY_BOOT_TIMEOUT,
    subprocess_runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> VerifyReport:
    """Restore the latest off-site snapshot and prove it is usable.

    Never raises for a *check* failure — those are the expected outcome on a
    bad day and belong in the returned report, which is what gets persisted
    and alerted on. It does propagate `KeyboardInterrupt` (after cleaning up),
    because a human pressing ^C is not a verify verdict.
    """
    started_at = time.time()
    started_mono = time.monotonic()
    report = VerifyReport(ok=False, snapshot_id=snapshot_id or "", started_at=started_at)

    with scratch_dir(scratch_root) as scratch:
        try:
            backend, _dsn = backup.resolve_backend()
            report.backend = backend
            config = config or backup.BackupConfig.from_env()

            if snapshot_id is None:
                snapshot = latest_snapshot(config, runner=runner)
                report.snapshot_id = str(snapshot["id"])
            else:
                report.snapshot_id = snapshot_id

            suffix = ".db" if backend == sql.DIALECT_SQLITE else ".dump"
            restored = scratch / f"coord{suffix}"
            report.restore_seconds = fetch_snapshot(
                report.snapshot_id, restored, config=config, runner=runner
            )
            report.steps.append(
                StepResult(
                    "fetch",
                    True,
                    f"restored {report.snapshot_id[:12]} in {report.restore_seconds:.1f}s",
                )
            )

            report.steps.append(
                check_structure(restored, backend=backend, runner=subprocess_runner)
            )

            if backend != sql.DIALECT_SQLITE:
                # #3085's lesson, applied to this rung: a check that cannot
                # actually prove recovery for the configured backend must say
                # so, not report green. Content/schema/parity all need the
                # dump loaded into a scratch cluster, which is D3/D4 work.
                raise DRVerifyError(
                    f"the configured store backend is {backend!r}: this lane can "
                    "structurally verify the dump but cannot yet restore it into "
                    "a scratch cluster, so it cannot prove recovery. Refusing to "
                    "report a green DR verify it did not earn."
                )

            live_path = Path(live_db) if live_db else backup.live_db_path()
            live_counts = table_counts(live_path)
            restored_counts = table_counts(restored)
            report.steps.append(
                check_content(
                    restored_counts,
                    live_counts,
                    fraction=tolerance_fraction,
                    absolute=tolerance_rows,
                )
            )

            report.steps.append(check_schema(restored, expected=expected_schema_version))

            if parity:
                report.steps.append(
                    check_parity(
                        restored,
                        config_path=config_path,
                        live_assignments=live_board_assignment_count(live_path),
                        tolerance_fraction=tolerance_fraction,
                        tolerance_rows=tolerance_rows,
                        timeout=parity_timeout,
                    )
                )
            else:
                report.steps.append(
                    StepResult("parity", True, "skipped (--no-parity)")
                )

            report.ok = True
        except DRVerifyError as exc:
            report.failure = scrub(str(exc))
            report.steps.append(StepResult(_step_name(str(exc)), False, report.failure))
        except backup.BackupError as exc:
            report.failure = scrub(str(exc))
            report.steps.append(StepResult("backup", False, report.failure))
        finally:
            report.duration_seconds = time.monotonic() - started_mono

    return report


def _step_name(message: str) -> str:
    """The check a failure message belongs to, for the persisted step list."""
    for name in ("structural", "content", "schema", "parity", "fetch"):
        if message.startswith(name) or message.startswith(f"{name} check"):
            return name
    return "verify"


# --------------------------------------------------------------------------
# Alerting — through the notifier that exists, never a new transport
# --------------------------------------------------------------------------


def build_alert(title: str, body: str, *, now: float | None = None) -> Any:
    """A :class:`coord.notifier.models.NotifyEvent` for a DR-verify failure."""
    from coord.notifier.models import NotifyEvent  # noqa: PLC0415

    return NotifyEvent(
        subject=ALERT_SUBJECT,
        condition=ALERT_CONDITION,
        title=scrub(title),
        body=scrub(body),
        created_at=time.time() if now is None else now,
    )


def alert(title: str, body: str, *, transport: Any = None) -> bool:
    """Push a failure through the existing notifier. Returns whether it landed.

    ``docs/NOTIFIER.md``: the notifier *"tells you when NOBODY IS COMING — and
    nothing else"*. A DR verify that failed or stopped running qualifies
    exactly — nothing else is watching this, and there is no other signal that
    it broke.

    Never raises. An alert path that can take down the check it is reporting
    on is worse than a missed alert, and the condition is still true next run
    (it re-derives from `last_verify.json`, not from a ledger).
    """
    from coord.notifier.digest import to_message  # noqa: PLC0415
    from coord.notifier.transport import NullTransport, safe_send  # noqa: PLC0415

    if transport is None:
        transport = _default_transport()
    if isinstance(transport, NullTransport):
        _log().error(
            "dr verify failed and no notifier transport is configured — this "
            "alert reached nobody: %s",
            scrub(body),
        )
        return False
    result = safe_send(transport, to_message(build_alert(title, body)))
    if not result.ok:
        _log().error("dr verify alert could not be delivered: %s", result.error)
    return bool(result.ok)


def _default_transport() -> Any:
    from coord.notifier.transport import NullTransport, build_transport  # noqa: PLC0415

    try:
        from coord.config import load, resolve_config_path  # noqa: PLC0415

        cfg = load(resolve_config_path())
    except Exception as exc:  # noqa: BLE001 — a bad config must not eat the alert path
        _log().warning("dr verify: could not load coordinator.yml for the alert: %s", exc)
        return NullTransport()
    notif = getattr(cfg, "notifications", None)
    if notif is None or not getattr(notif, "enabled", False):
        # Master switch is off (or the block is absent entirely) — every other
        # caller of this machinery (coord/notifier/service.py, drive_queue.py's
        # self-cordon escalation) honors this, and dr verify must not be the one
        # alert path in the fleet that pages a phone an operator explicitly muted.
        return NullTransport()
    return build_transport(notif)
