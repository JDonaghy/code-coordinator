"""Off-site, backend-agnostic backup of the coordinator store (#3118, closes #1822).

`~/.coord/coord.db` is 720 MB on the daemon host and, before this module, had
**no copy off that machine at all**. The existing hourly lane
(`coord/deploy/coord-db-backup.sh`) is correct and stays exactly as it is —
but it writes to a USB SSD *in the same chassis*, and says so in its own
docstring: it does not protect against the machine being lost, stolen or
burned. Losing it is not recoverable from GitHub; `reconcile()` rebuilds
in-flight assignment state and nothing else — not test verdicts, not review
verdicts, not merge-queue ordering, not the audit log.

This module is the off-box half. Shape, in order, because the order is the
contract:

1. **Ask the store seam which backend is configured** — never re-parse
   `coordinator.yml`'s `store:` block, and never fall back to assuming
   SQLite. This is #3085's property: the SQLite-only lane would otherwise
   have reported a cheerful green against a frozen pre-cutover file and
   offered to roll the fleet back to it. An unknown/unsnapshottable backend
   is refused **before anything is written**, with the backend named.
2. **Take a consistent snapshot** appropriate to that backend — `VACUUM INTO`
   for SQLite (WAL-safe against a live writer, and it compacts), `pg_dump -Fc`
   for Postgres.
3. **Verify before it counts.** Integrity check *and* a sanity check (the
   `assignments` table is present and non-empty). A snapshot that fails
   either is kept on disk renamed `.REJECTED` — never silently deleted, never
   left under a name indistinguishable from a good backup — and is **never
   uploaded**, so it can never age out a good one.
4. **Only then upload**, and only then prune. Retention never runs on a
   failed push, and carries a `--keep-last 1` floor so it cannot leave the
   repository empty.

**Transport is restic → Azure Blob.** restic speaks `azure:<container>:<path>`
natively; borg would need a filesystem or SSH target, i.e. a host to receive
it. The chunk-level dedup is the entire premise: an hourly snapshot of a
720 MB database transfers roughly the delta, not the database.

**Credentials come from the environment or a managed identity only.** Never
from `coordinator.yml` — this module does not read it — and never from argv,
which is world-readable in `/proc`. The systemd unit reads an
`EnvironmentFile` with mode `0600`. :func:`scrub` additionally strips any
credential *value* out of anything this module logs or embeds in an error, so
a repository URL or SAS token echoed back by restic's own stderr cannot leak
into the journal.

Out of scope, deliberately (see the issue): continuous WAL shipping / PITR
(Litestream stays the escalation if a ~1 h RPO proves insufficient *and*
SQLite is still the store), proving the Postgres branch against a real
Postgres fleet (#829), and restoring onto a replacement host (D3/D4 of
#3117). This rung only has to make a verified, restorable artifact exist off
the machine.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from coord import sql

LOGGER_NAME = "coord.backup"

#: The restic repository URL, e.g. ``azure:coord-backups:/dellserver``.
#: ``RESTIC_REPOSITORY`` is accepted as the fallback spelling so an operator
#: who already exports restic's own variable does not need a second one.
REPOSITORY_ENV = "COORD_BACKUP_REPOSITORY"
RESTIC_REPOSITORY_ENV = "RESTIC_REPOSITORY"

#: Environment variables whose *values* are secrets. Two jobs: they are what
#: :func:`scrub` redacts out of logs and error text, and at least one of the
#: password spellings must be present before a push is attempted (restic
#: would otherwise fail *after* we had already written a snapshot).
CREDENTIAL_ENV_VARS: frozenset[str] = frozenset(
    {
        "RESTIC_PASSWORD",
        "RESTIC_PASSWORD_FILE",
        "RESTIC_PASSWORD_COMMAND",
        "AZURE_ACCOUNT_KEY",
        "AZURE_ACCOUNT_SAS",
        "AZURE_ACCOUNT_NAME",
        "PGPASSWORD",
        "PGPASSFILE",
    }
)

#: Any one of these satisfies "restic can unlock the repository".
_PASSWORD_ENV_VARS: tuple[str, ...] = (
    "RESTIC_PASSWORD",
    "RESTIC_PASSWORD_FILE",
    "RESTIC_PASSWORD_COMMAND",
)

#: Tag every snapshot this lane makes, so `forget` can be scoped to snapshots
#: we own and cannot prune something else sharing the repository.
SNAPSHOT_TAG = "coord-store"

#: Filenames inside the snapshot, per backend. Stable on purpose: a
#: deterministic name makes a snapshot's contents obvious in `restic ls` and
#: keeps dedup chunking aligned run over run.
_ARTIFACT_NAMES = {
    sql.DIALECT_SQLITE: "coord-store.db",
    sql.DIALECT_POSTGRES: "coord-store.dump",
}

#: `pg_dump -Fc` output starts with this magic. Cheap first gate before
#: spending a `pg_restore --list` on a file that is obviously not a dump.
_PGDUMP_MAGIC = b"PGDMP"


class BackupError(RuntimeError):
    """Anything that must stop a push/restore. Always safe to print: the
    message is passed through :func:`scrub` at construction by the callers
    that build one out of subprocess output."""


@dataclass(frozen=True)
class BackupConfig:
    """Everything the lane needs, resolved from the environment only.

    Deliberately has no `from_config()` constructor and no `coordinator.yml`
    reader. The repository URL is not a secret but the credentials that reach
    it are, and a config-file path for one is an invitation to put the other
    there too — so the whole surface stays in the environment, where the
    systemd unit's mode-0600 `EnvironmentFile` (or a managed identity) is the
    only supply route.
    """

    repository: str
    restic_bin: str = "restic"
    keep_hourly: int = 48
    keep_daily: int = 30
    keep_weekly: int = 8
    tag: str = SNAPSHOT_TAG

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BackupConfig":
        """Build from *env* (defaults to `os.environ`).

        Raises :class:`BackupError` when the repository is unset — the
        "provisioning the storage account is operator work; this issue
        consumes an already-configured target and must fail clearly when it
        is absent" requirement.
        """
        env = dict(os.environ if env is None else env)
        repository = (env.get(REPOSITORY_ENV) or env.get(RESTIC_REPOSITORY_ENV) or "").strip()
        if not repository:
            raise BackupError(
                f"no backup repository configured: set ${REPOSITORY_ENV} "
                f"(or ${RESTIC_REPOSITORY_ENV}) to the restic target, e.g. "
                "'azure:coord-backups:/dellserver'. Provisioning the storage "
                "account and container is operator work; this lane consumes an "
                "already-configured target."
            )
        return cls(
            repository=repository,
            restic_bin=env.get("COORD_RESTIC_BIN", "restic"),
            keep_hourly=_int_env(env, "COORD_BACKUP_KEEP_HOURLY", 48),
            keep_daily=_int_env(env, "COORD_BACKUP_KEEP_DAILY", 30),
            keep_weekly=_int_env(env, "COORD_BACKUP_KEEP_WEEKLY", 8),
        )


def _int_env(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BackupError(f"${name} must be an integer, got {raw!r}") from None
    if value < 1:
        raise BackupError(f"${name} must be >= 1, got {value}")
    return value


@dataclass
class PushResult:
    """What a completed push did, for the CLI and for the operator's log."""

    backend: str
    snapshot_id: str
    snapshot_bytes: int
    transferred_bytes: int
    rows: int | None
    pruned: bool
    prune_skipped_reason: str | None = None


@dataclass
class VerifyFailure(Exception):
    """A snapshot failed integrity or sanity checking.

    Carries the path it was *kept* at (the `.REJECTED` rename) so the caller
    can name it — the local lane's contract, unchanged: never silently
    deleted, never left under its normal name.
    """

    reason: str
    rejected_path: Path | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.rejected_path is not None:
            return f"{self.reason} (kept as {self.rejected_path})"
        return self.reason


# --------------------------------------------------------------------------
# Secret hygiene
# --------------------------------------------------------------------------


def scrub(text: str, env: dict[str, str] | None = None) -> str:
    """Replace every credential *value* present in *text* with ``***``.

    Applied to everything this module logs or folds into an exception. argv
    never carries a credential by construction (see :func:`restic_argv` and
    :func:`pg_dump_command`), but restic's and pg_dump's own stderr can echo
    a repository URL with an embedded SAS token, and that stderr is what ends
    up verbatim in a systemd journal.
    """
    env = os.environ if env is None else env
    out = text
    for name in CREDENTIAL_ENV_VARS:
        value = env.get(name)
        if value and len(value) >= 4:
            out = out.replace(value, "***")
    return out


# --------------------------------------------------------------------------
# Backend resolution — the #3085 property
# --------------------------------------------------------------------------


def resolve_backend() -> tuple[str, str | None]:
    """``(backend, dsn)`` for the store this machine is configured against.

    The backend name comes from :func:`coord.db.resolve_store_backend` — the
    same seam `coord store-backend` and `coord-db-backup.sh` use, so this can
    never disagree with the backend a connection actually opens against. The
    DSN (Postgres only) comes from the private resolver behind it, because
    the public one deliberately redacts and `pg_dump` needs the real thing.

    A malformed or unsnapshottable `store:` block raises :class:`BackupError`
    naming the offending backend. There is no fall-back-to-SQLite path here,
    by design.
    """
    from coord import db as _db  # noqa: PLC0415
    from coord.config import ConfigError  # noqa: PLC0415

    try:
        backend, _redacted = _db.resolve_store_backend()
    except ConfigError as exc:
        named = _peek_configured_backend_name()
        suffix = f" (store.backend={named!r})" if named else ""
        raise BackupError(
            f"cannot determine the store backend{suffix}: {exc} — refusing to "
            "assume sqlite. Nothing was written."
        ) from None

    if backend not in _ARTIFACT_NAMES:
        raise BackupError(
            f"the configured store backend is {backend!r}, which this backup "
            "lane does not know how to snapshot (it handles "
            f"{', '.join(sorted(_ARTIFACT_NAMES))}). Nothing was written."
        )

    if backend != sql.DIALECT_POSTGRES:
        return backend, None
    return backend, _db._resolve_store_target().dsn


def _peek_configured_backend_name() -> str | None:
    """Best-effort read of the raw ``store.backend`` string, **for the error
    message only**.

    Never load-bearing: nothing branches on this. It exists so a refusal can
    say *which* backend was configured, which `_parse_store`'s own
    ConfigError ("must be one of: sqlite, postgres") does not include, and
    which is the first thing an on-call engineer reading the journal wants.
    Any failure here degrades to ``None`` and a slightly less specific
    message — it must never turn a config problem into a crash.
    """
    try:
        import yaml  # noqa: PLC0415

        from coord.config import resolve_config_path  # noqa: PLC0415

        raw = yaml.safe_load(resolve_config_path().read_text())
        value = raw["store"]["backend"]
    except Exception:
        return None
    return value if isinstance(value, str) else None


# --------------------------------------------------------------------------
# Snapshotting
# --------------------------------------------------------------------------


def live_db_path() -> Path:
    """The SQLite file `get_connection()` would open on this machine."""
    from coord import db as _db  # noqa: PLC0415

    return Path(_db.DB_PATH)


def snapshot_sqlite(dest: Path, *, source: Path | None = None) -> Path:
    """`VACUUM INTO` *source* → *dest*, and return *dest*.

    `VACUUM INTO`, never `cp`: it takes a point-in-time consistent snapshot
    of a live WAL-mode database while `coord-serve` keeps writing, and it
    compacts on the way out. A plain copy of a WAL-mode db under concurrent
    writes can capture a torn file — the failure you only discover at
    restore. In WAL mode a reader does not block on a writer, so this
    succeeds (and correctly excludes uncommitted rows) even with another
    connection holding an open write transaction.
    """
    src = live_db_path() if source is None else Path(source)
    if not src.exists():
        raise BackupError(f"source database not found: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    conn = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=src)
    try:
        # Bound parameter, not an f-string: the destination is a path we
        # control, but quoting a path into SQL by hand is a habit worth not
        # having. SQLite evaluates VACUUM INTO's argument as an expression.
        sql.execute(conn, "VACUUM INTO ?", (str(dest),))
    except sql.driver_errors() as exc:
        raise BackupError(f"VACUUM INTO failed: {exc}") from None
    finally:
        conn.close()
    return dest


def _is_uri_dsn(dsn: str) -> bool:
    """True for libpq's URI form (`postgresql://...` / `postgres://...`).

    Anything else is libpq's other valid form: space-separated
    `keyword=value` pairs (e.g. ``"host=h port=5432 password=hunter2"``),
    which is what ``store.dsn`` documents itself as accepting in
    :class:`coord.config.StoreConfig` and what psycopg happily parses.
    """
    return dsn.startswith("postgresql://") or dsn.startswith("postgres://")


def _parse_keyword_value_dsn(dsn: str) -> dict[str, str]:
    """Parse libpq's `keyword=value` connection-string form.

    Minimal hand-rolled parser (no hard dependency on psycopg, which is an
    optional extra — see ``coord/sql.py``'s ``_import_psycopg``): values may
    be single-quoted to hold whitespace, with ``\\'`` and ``\\\\`` as the only
    escapes, matching
    https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNSTRING.
    """
    params: dict[str, str] = {}
    i, n = 0, len(dsn)
    while i < n:
        while i < n and dsn[i].isspace():
            i += 1
        if i >= n:
            break
        key_start = i
        while i < n and dsn[i] != "=" and not dsn[i].isspace():
            i += 1
        key = dsn[key_start:i]
        while i < n and dsn[i].isspace():
            i += 1
        if i >= n or dsn[i] != "=":
            break  # malformed tail; stop rather than loop forever
        i += 1
        while i < n and dsn[i].isspace():
            i += 1
        value_chars: list[str] = []
        if i < n and dsn[i] == "'":
            i += 1
            while i < n:
                c = dsn[i]
                if c == "\\" and i + 1 < n:
                    value_chars.append(dsn[i + 1])
                    i += 2
                    continue
                if c == "'":
                    i += 1
                    break
                value_chars.append(c)
                i += 1
        else:
            while i < n and not dsn[i].isspace():
                value_chars.append(dsn[i])
                i += 1
        if key:
            params[key] = "".join(value_chars)
    return params


def _format_keyword_value_dsn(params: dict[str, str]) -> str:
    """Inverse of :func:`_parse_keyword_value_dsn`, quoting where needed."""
    parts = []
    for key, value in params.items():
        if value == "" or any(c.isspace() for c in value) or "'" in value or "\\" in value:
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            parts.append(f"{key}='{escaped}'")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def pg_dump_command(dsn: str, dest: Path) -> tuple[list[str], dict[str, str]]:
    """`(argv, extra_env)` for `pg_dump -Fc` against *dsn*.

    **The password is moved out of the DSN and into `PGPASSWORD`**, because
    argv is world-readable in `/proc` and a DSN with an inline password on
    the command line is exactly the leak this lane is not allowed to have.
    Everything else about the DSN is passed through unchanged.

    Handles both DSN forms libpq (and thus psycopg and ``pg_dump --dbname``)
    accept: the URI form (`postgresql://user:pass@host/db`) and the
    space-separated `keyword=value` form
    (`host=h port=5432 dbname=coord user=u password=hunter2`) — see
    :class:`coord.config.StoreConfig`'s docstring. Checking only the URI form
    left a keyword/value password sailing straight into argv unchanged.
    """
    extra_env: dict[str, str] = {}
    safe_dsn = dsn
    if _is_uri_dsn(dsn):
        from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415

        parts = urlsplit(dsn)
        if parts.password:
            extra_env["PGPASSWORD"] = parts.password
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            netloc = f"{parts.username}@{host}" if parts.username else host
            safe_dsn = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    else:
        params = _parse_keyword_value_dsn(dsn)
        password = params.pop("password", None)
        if password:
            extra_env["PGPASSWORD"] = password
            safe_dsn = _format_keyword_value_dsn(params)
    argv = ["pg_dump", "--format=custom", f"--file={dest}", "--dbname", safe_dsn]
    return argv, extra_env


def snapshot_postgres(
    dsn: str,
    dest: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> Path:
    """`pg_dump -Fc` *dsn* → *dest*.

    Written and unit-tested here (command shape, credential routing,
    verification); proving it against a real Postgres fleet belongs to the
    #829 cutover, per this issue's out-of-scope list.
    """
    if not dsn:
        raise BackupError("store backend is postgres but no DSN is configured")
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv, extra_env = pg_dump_command(dsn, dest)
    env = dict(os.environ)
    env.update(extra_env)
    run = runner or subprocess.run
    logging.getLogger(LOGGER_NAME).info("running: %s", scrub(" ".join(argv), env))
    proc = run(argv, capture_output=True, text=True, env=env, check=False)
    if proc.returncode != 0:
        raise BackupError(
            "pg_dump failed (exit "
            f"{proc.returncode}): {scrub((proc.stderr or '').strip(), env)}"
        )
    return dest


def take_snapshot(
    backend: str,
    dsn: str | None,
    dest_dir: Path,
    *,
    source: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> Path:
    """Dispatch to the right snapshot method for *backend*."""
    name = _ARTIFACT_NAMES.get(backend)
    if name is None:  # pragma: no cover - resolve_backend() already refused
        raise BackupError(f"no snapshot method for backend {backend!r}")
    dest = Path(dest_dir) / name
    if backend == sql.DIALECT_POSTGRES:
        return snapshot_postgres(dsn or "", dest, runner=runner)
    return snapshot_sqlite(dest, source=source)


# --------------------------------------------------------------------------
# Verification — "verify before it counts"
# --------------------------------------------------------------------------


def verify_sqlite_snapshot(path: Path) -> int:
    """`PRAGMA integrity_check` + `assignments` present and non-empty.

    Returns the assignment count. Raises :class:`VerifyFailure` (without a
    `rejected_path` — the caller does the rename, since only it knows where
    rejects are kept) on anything wrong.
    """
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        raise VerifyFailure("snapshot is missing or zero-length")
    try:
        conn = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=path, read_only=True)
    except sql.driver_errors() as exc:
        raise VerifyFailure(f"cannot open snapshot: {exc}") from None
    try:
        try:
            result = sql.sqlite_integrity_check(conn)
        except sql.driver_errors() as exc:
            raise VerifyFailure(f"integrity_check failed: {exc}") from None
        if result != "ok":
            raise VerifyFailure(f"integrity_check on snapshot: {result}")
        # Prove it is a coord db and not an empty file that passed
        # integrity_check — the local lane's second gate, kept.
        try:
            count = int(sql.execute(conn, "SELECT COUNT(*) FROM assignments;").fetchone()[0])
        except sql.driver_errors() as exc:
            raise VerifyFailure(f"snapshot has no usable assignments table: {exc}") from None
        if count == 0:
            raise VerifyFailure(
                "snapshot has 0 assignments — refusing to count this as a backup"
            )
        return count
    finally:
        conn.close()


def verify_postgres_snapshot(
    path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> int | None:
    """Magic-byte check + `pg_restore --list` must show an `assignments` table.

    Returns ``None`` for the row count — a custom-format dump's table of
    contents proves the table is *in* the dump, not how many rows it holds;
    counting would mean restoring it, which is D2's job (`coord dr verify`),
    not this rung's.
    """
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        raise VerifyFailure("snapshot is missing or zero-length")
    with path.open("rb") as fh:
        if fh.read(len(_PGDUMP_MAGIC)) != _PGDUMP_MAGIC:
            raise VerifyFailure("snapshot is not a pg_dump custom-format archive")
    run = runner or subprocess.run
    try:
        proc = run(
            ["pg_restore", "--list", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise VerifyFailure(
            "pg_restore not found — cannot verify a Postgres snapshot, and an "
            "unverified snapshot never counts"
        ) from None
    if proc.returncode != 0:
        raise VerifyFailure(f"pg_restore --list failed: {scrub((proc.stderr or '').strip())}")
    if "assignments" not in (proc.stdout or ""):
        raise VerifyFailure("snapshot's table of contents has no 'assignments' table")
    return None


def verify_snapshot(
    path: Path,
    backend: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> int | None:
    if backend == sql.DIALECT_POSTGRES:
        return verify_postgres_snapshot(path, runner=runner)
    return verify_sqlite_snapshot(path)


def reject(path: Path, reject_dir: Path) -> Path:
    """Move a failed snapshot to ``<reject_dir>/<name>.<stamp>.REJECTED``.

    The local lane's contract, carried forward verbatim in spirit: a bad
    snapshot is neither silently deleted nor left lying around under its
    normal name, indistinguishable from a good backup — which is what
    retention would eventually rotate out without anyone noticing.
    """
    reject_dir = Path(reject_dir)
    reject_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = reject_dir / f"{Path(path).name}.{stamp}.REJECTED"
    shutil.move(str(path), str(dest))
    return dest


# --------------------------------------------------------------------------
# restic transport
# --------------------------------------------------------------------------


def restic_argv(config: BackupConfig, *args: str) -> list[str]:
    """The full argv for a restic invocation.

    The repository is passed via `RESTIC_REPOSITORY` in the child env rather
    than `-r` on the command line, and no credential is ever an argument:
    argv is world-readable in `/proc`, so anything secret has to travel in
    the environment. `tests/test_backup.py` asserts on exactly this.
    """
    return [config.restic_bin, *args]


def restic_env(config: BackupConfig, env: dict[str, str] | None = None) -> dict[str, str]:
    """Child environment for restic: the parent's, plus the repository.

    Credentials (`RESTIC_PASSWORD*`, `AZURE_*`) are inherited as-is — or,
    with a managed identity, are absent entirely and restic authenticates
    against IMDS. Either way they are never materialised here from a config
    file.
    """
    out = dict(os.environ if env is None else env)
    out[RESTIC_REPOSITORY_ENV] = config.repository
    return out


@dataclass
class ResticRunner:
    """Thin, injectable seam over `restic`.

    Everything about *what* to run lives in this module; tests substitute a
    runner (or a `restic` shim on PATH) so the whole push/verify/upload/prune
    sequence is exercised end to end without a real Azure container.
    """

    config: BackupConfig
    env: dict[str, str] | None = None
    _log: logging.Logger = field(
        default_factory=lambda: logging.getLogger(LOGGER_NAME), repr=False
    )

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        argv = restic_argv(self.config, *args)
        env = restic_env(self.config, self.env)
        self._log.info("running: %s", scrub(" ".join(argv), env))
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
        except FileNotFoundError:
            raise BackupError(
                f"restic not found: {self.config.restic_bin!r} is not on $PATH. "
                "The daemon host pins it as a managed unit dependency — see "
                "docs/AGENT_OPERATIONS.md."
            ) from None
        if check and proc.returncode != 0:
            raise BackupError(
                f"restic {args[0] if args else ''} failed (exit {proc.returncode}): "
                f"{scrub((proc.stderr or proc.stdout or '').strip(), env)}"
            )
        return proc


def init_repository(config: BackupConfig, *, runner: ResticRunner | None = None) -> None:
    (runner or ResticRunner(config)).run("init")


def list_snapshots(config: BackupConfig, *, runner: ResticRunner | None = None) -> list[dict]:
    """Snapshots this lane owns, oldest first (restic's own ordering)."""
    proc = (runner or ResticRunner(config)).run(
        "snapshots", "--tag", config.tag, "--json"
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupError(f"could not parse `restic snapshots --json`: {exc}") from None
    if not isinstance(parsed, list):
        raise BackupError("`restic snapshots --json` did not return a list")
    return [s for s in parsed if isinstance(s, dict)]


def _parse_backup_summary(stdout: str) -> tuple[str, int]:
    """`(snapshot_id, data_added)` out of `restic backup --json` output.

    restic emits one JSON object per line; the last `summary` message is the
    one carrying `data_added` — the number that is the entire premise of this
    design (an hourly push of a 720 MB db should transfer megabytes, not
    gigabytes), so it is surfaced rather than discarded.
    """
    snapshot_id = ""
    data_added = 0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("message_type") == "summary":
            snapshot_id = str(msg.get("snapshot_id") or snapshot_id)
            raw_added = msg.get("data_added_packed", msg.get("data_added", 0))
            try:
                data_added = int(raw_added)
            except (TypeError, ValueError):
                data_added = 0
    if not snapshot_id:
        raise BackupError(
            "restic backup produced no summary message — cannot confirm a "
            "snapshot was created, so this push does not count"
        )
    return snapshot_id, data_added


def upload(
    config: BackupConfig, path: Path, *, runner: ResticRunner | None = None
) -> tuple[str, int]:
    proc = (runner or ResticRunner(config)).run(
        "backup", "--json", "--tag", config.tag, str(path)
    )
    return _parse_backup_summary(proc.stdout or "")


def prune(
    config: BackupConfig, *, runner: ResticRunner | None = None
) -> tuple[bool, str | None]:
    """Apply the retention policy. Returns `(pruned, skipped_reason)`.

    Two independent guarantees that retention can never leave zero backups:

    * it is skipped outright when the repository has no snapshots (there is
      nothing to prune and a `forget` against an empty repo is pure risk),
    * `--keep-last 1` is always in the policy, so even a clock skew or a
      policy misconfiguration that matched nothing still keeps the most
      recent snapshot.

    And it is only ever *reached* from a push whose snapshot verified, so a
    run where every candidate fails verification prunes nothing at all.
    """
    runner = runner or ResticRunner(config)
    snapshots = list_snapshots(config, runner=runner)
    if not snapshots:
        return False, "repository has no snapshots — nothing to prune"
    runner.run(
        "forget",
        "--tag",
        config.tag,
        "--keep-last",
        "1",
        "--keep-hourly",
        str(config.keep_hourly),
        "--keep-daily",
        str(config.keep_daily),
        "--keep-weekly",
        str(config.keep_weekly),
        "--prune",
    )
    return True, None


# --------------------------------------------------------------------------
# The lane
# --------------------------------------------------------------------------


def push(
    config: BackupConfig | None = None,
    *,
    source: Path | None = None,
    work_dir: Path | None = None,
    reject_dir: Path | None = None,
    runner: ResticRunner | None = None,
    subprocess_runner: Callable[..., subprocess.CompletedProcess] | None = None,
    do_prune: bool = True,
) -> PushResult:
    """Snapshot → verify → upload → prune. In that order, always.

    Every failure before the upload leaves the repository untouched: no
    snapshot is uploaded, no retention runs, and no existing backup ages out.
    """
    log = logging.getLogger(LOGGER_NAME)

    # 1. Backend first, before anything is written anywhere — #3085.
    backend, dsn = resolve_backend()

    # 2. Then the target, so a missing container also fails before we write.
    config = config or BackupConfig.from_env()
    _require_password(config)

    coord_dir = live_db_path().parent
    reject_dir = Path(reject_dir) if reject_dir else coord_dir / "backup-rejected"

    with _staging_dir(work_dir) as staging:
        snap = take_snapshot(
            backend, dsn, staging, source=source, runner=subprocess_runner
        )
        size = snap.stat().st_size

        # 3. Verify before it counts.
        try:
            rows = verify_snapshot(snap, backend, runner=subprocess_runner)
        except VerifyFailure as failure:
            kept = reject(snap, reject_dir)
            log.error("snapshot rejected: %s (kept as %s)", failure.reason, kept)
            raise BackupError(
                f"snapshot failed verification: {failure.reason} (kept as {kept}). "
                "Nothing was uploaded and retention did not run, so no existing "
                "backup was aged out."
            ) from None

        # 4. Only now does it leave the machine.
        runner = runner or ResticRunner(config)
        snapshot_id, transferred = upload(config, snap, runner=runner)
        log.info(
            "uploaded %s (%s bytes on disk, %s bytes transferred) as %s",
            snap.name,
            size,
            transferred,
            snapshot_id,
        )

    pruned = False
    skipped: str | None = None
    if do_prune:
        pruned, skipped = prune(config, runner=runner)

    return PushResult(
        backend=backend,
        snapshot_id=snapshot_id,
        snapshot_bytes=size,
        transferred_bytes=transferred,
        rows=rows,
        pruned=pruned,
        prune_skipped_reason=skipped,
    )


def _require_password(config: BackupConfig) -> None:
    """Fail *before* snapshotting when restic has no way to unlock the repo.

    Cheap, and it moves a guaranteed failure from after a 720 MB
    `VACUUM INTO` to before it.
    """
    if any(os.environ.get(name) for name in _PASSWORD_ENV_VARS):
        return
    raise BackupError(
        "no restic repository password available: set one of "
        f"{', '.join('$' + n for n in _PASSWORD_ENV_VARS)} in the unit's "
        "EnvironmentFile (mode 0600). Credentials are never read from "
        "coordinator.yml or passed on the command line."
    )


class _staging_dir:
    """`work_dir` if given (kept), else a temp dir (removed)."""

    def __init__(self, work_dir: Path | None) -> None:
        self._explicit = Path(work_dir) if work_dir else None
        self._tmp: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> Path:
        if self._explicit is not None:
            self._explicit.mkdir(parents=True, exist_ok=True)
            return self._explicit
        self._tmp = tempfile.TemporaryDirectory(prefix="coord-backup-")
        return Path(self._tmp.name)

    def __exit__(self, *exc: Any) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()


def restore(
    snapshot_id: str,
    into: Path,
    *,
    config: BackupConfig | None = None,
    runner: ResticRunner | None = None,
    force: bool = False,
    verify: bool = True,
) -> Path:
    """Materialise *snapshot_id* at *into*.

    Refuses, without `force`, to write over the live database — the one
    mistake in this whole lane that destroys the thing it exists to protect.
    Also refuses to clobber any other existing file, for the same reason at
    lower stakes.
    """
    into = Path(into).expanduser()
    live = live_db_path()
    try:
        same_as_live = into.resolve() == live.resolve()
    except OSError:  # pragma: no cover - unresolvable path
        same_as_live = str(into) == str(live)
    if same_as_live and not force:
        raise BackupError(
            f"refusing to restore over the live database at {live} — pass "
            "--force if that is genuinely what you want. Restoring to a "
            "scratch path first and diffing is almost always the right move."
        )
    if into.exists() and not force:
        raise BackupError(f"refusing to overwrite existing file {into} — pass --force")

    config = config or BackupConfig.from_env()
    runner = runner or ResticRunner(config)

    with tempfile.TemporaryDirectory(prefix="coord-restore-") as tmp:
        runner.run("restore", snapshot_id, "--target", tmp)
        candidates = [p for p in Path(tmp).rglob("*") if p.is_file()]
        if len(candidates) != 1:
            raise BackupError(
                f"expected exactly one file in snapshot {snapshot_id}, found "
                f"{len(candidates)}"
            )
        into.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(candidates[0]), str(into))

    if verify and into.suffix == ".db":
        verify_sqlite_snapshot(into)
    return into


def table_fingerprint(path: Path) -> dict[str, list[tuple]]:
    """Every user table's full contents, keyed by table name.

    Used by the restore test to assert a restored database matches its source
    *row for row on every table* rather than merely opening cleanly — an
    acceptance criterion, and the only check that would have caught a
    snapshot method that silently dropped a table.
    """
    conn = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=Path(path), read_only=True)
    try:
        names = [
            row[0]
            for row in sql.execute(
                conn,
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name",
            )
        ]
        out: dict[str, list[tuple]] = {}
        for name in names:
            rows = sql.execute(conn, f'SELECT * FROM "{name}"').fetchall()
            out[name] = sorted(rows, key=repr)
        return out
    finally:
        conn.close()


def format_bytes(n: int) -> str:
    """Human-readable size for the operator-facing transfer line."""
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TiB"  # pragma: no cover - unreachable


def summarize_snapshots(snapshots: Sequence[dict]) -> list[str]:
    """One display line per snapshot, newest first."""
    lines = []
    for snap in sorted(snapshots, key=lambda s: str(s.get("time", "")), reverse=True):
        short = str(snap.get("short_id") or str(snap.get("id", ""))[:8])
        when = str(snap.get("time", "?"))
        paths = ", ".join(snap.get("paths") or [])
        lines.append(f"{short}  {when}  {paths}")
    return lines
