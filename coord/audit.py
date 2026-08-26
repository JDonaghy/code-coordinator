"""Durable, append-only audit trail (#1036 — Audit Trail epic, issue A).

``record_audit()`` is called from the ``state._*_local`` / ``issue_store``
write choke points — the handful of functions every board mutation, whether
it arrives from a thin client or lands directly on the daemon, funnels
through (canonical example: ``state._record_test_verdict_local``).  Hooking
there guarantees one row per real transition regardless of topology, without
touching the ~30 CLI call sites that ultimately reach those writers.

This module is deliberately dumb: one table, one INSERT, best-effort.  It
does not define an event taxonomy beyond the ``tier``/``category`` columns
described in the issue — callers pick their own ``event_type`` strings,
reusing the ``coord:event=`` names from :mod:`coord.comments` where they
already exist (``EVENT_COMPLETION`` etc.) so the audit log and the GitHub
message bus agree on vocabulary.

**Never raises into the caller.**  A board mutation must always succeed even
if the audit write fails (disk full, locked DB, schema drift on an old
checkout) — the write that rode in on is the one that matters; the audit
row is best-effort observability on top of it.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from typing import Any

from coord import sql
from coord.db import get_connection, is_lock_contention_error, retry_on_locked

_log = logging.getLogger(__name__)

__all__ = [
    "record_audit",
    "query_audit_log",
    "audit_lock_contention_losses",
    "flush_lock_contention_summary",
]

# Valid values are documented in the issue but not enforced here — callers
# are all internal (coord.state / coord.issue_store), and rejecting an
# unrecognized value would defeat "never raises into the caller".
_VALID_TIERS = ("business", "operational")

# #1037: read-side defaults for the paginated `/audit` endpoint / `coord
# audit` CLI.  Hard-capped so a client can't request the whole table in one
# shot (the endpoint is explicitly NOT the /board "everything" snapshot).
DEFAULT_LIMIT = 200
MAX_LIMIT = 500


def record_audit(
    *,
    tier: str,
    category: str,
    event_type: str,
    actor: str,
    summary: str,
    ts: float | None = None,
    repo: str | None = None,
    issue: int | None = None,
    assignment_id: str | None = None,
    machine: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append one row to ``audit_log``.  Best-effort — swallows all failures.

    ``tier`` is ``"business"`` (a real board transition — dispatch, test
    verdict, review verdict, merge, ...) or ``"operational"`` (daemon-tick
    housekeeping; out of scope for this issue, reserved for a later one).
    ``details`` is JSON-serialized into ``details_json``; pass only
    JSON-safe values (str/int/float/bool/None/dict/list).

    Also performs the opportunistic ``audit.max_rows`` trim (#1036
    deliverable 4) after a successful insert, when the config knob is set
    above its default of ``0`` (unlimited).

    #1038: when ``tier="operational"`` and ``audit.level`` is set to
    ``"business"`` (default ``"operational"``), the row is dropped here —
    the single choke point every operational-tier caller funnels through —
    so callers (the daemon-tick hooks) stay unconditional.  Business-tier
    rows are never gated by this check.
    """
    try:
        if tier == "operational" and _resolve_level() == "business":
            return
        _record_audit_unsafe(
            tier=tier,
            category=category,
            event_type=event_type,
            actor=actor,
            summary=summary,
            ts=ts,
            repo=repo,
            issue=issue,
            assignment_id=assignment_id,
            machine=machine,
            details=details,
        )
    except Exception as exc:  # noqa: BLE001 — audit logging must never break the caller
        if is_lock_contention_error(exc):
            # #2597: `_record_audit_unsafe` already retried this write
            # several times (`retry_on_locked`) before this was reached —
            # sustained contention, not a momentary collision. Still
            # best-effort (never raises into the caller), but a WARNING per
            # occurrence is exactly the flood #2597 measured (1,804/day on
            # dellserver, one indistinguishable line each) — count it
            # instead and let `_flush_lock_contention_summary` report the
            # aggregate once per run.
            _record_lock_contention_loss()
            _log.debug("record_audit: write lost to lock contention: %s", exc)
        else:
            _log.warning("record_audit: best-effort write failed: %s", exc)


def _record_audit_unsafe(
    *,
    tier: str,
    category: str,
    event_type: str,
    actor: str,
    summary: str,
    ts: float | None,
    repo: str | None,
    issue: int | None,
    assignment_id: str | None,
    machine: str | None,
    details: dict[str, Any] | None,
) -> None:
    """The actual write.  Split out from :func:`record_audit` so the
    try/except wrapper is the ONLY thing between this and the caller —
    keeps the swallow-and-log behavior in one obvious place."""
    conn = get_connection()

    def _write() -> None:
        sql.execute(
            conn,
            """INSERT INTO audit_log (
                ts, tier, category, event_type, actor,
                repo, issue, assignment_id, machine, summary, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts if ts is not None else time.time(),
                tier,
                category,
                event_type,
                actor,
                repo,
                issue,
                assignment_id,
                machine,
                summary,
                json.dumps(details) if details is not None else None,
            ),
        )
        conn.commit()

    # #2597: this table is the coordinator's primary diagnostic surface —
    # "coord audit" — and 1,804 rows/24h were measured lost to ordinary,
    # momentary lock contention (a concurrent writer holding the DB for a
    # beat) on dellserver, indistinguishable from a real gap in the trail.
    # Give this write the same short, backed-off retry every other
    # load-bearing write in this codebase already gets before falling
    # through to record_audit's best-effort swallow.
    retry_on_locked(_write)
    # #2597-review: the trim below is opportunistic housekeeping over rows
    # that are already durably committed above — isolate its own retry/
    # failure from the insert's. Left unguarded, a `DELETE` that hits lock
    # contention here would propagate out to `record_audit`'s except clause
    # and get classified (by `is_lock_contention_error` alone, with no way
    # to tell "the row itself didn't write" apart from "the row wrote fine
    # but the trim afterward didn't run") as a *lost audit write* — over-
    # counting genuine data loss for a row that made it into audit_log just
    # fine. Give the trim the same retry budget as any other write, but
    # keep a trim-only failure from ever touching that counter.
    try:
        retry_on_locked(lambda: _maybe_trim(conn))
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        if not is_lock_contention_error(exc):
            raise
        _log.debug(
            "record_audit: audit_log trim skipped due to lock contention "
            "(row itself was already written): %s", exc,
        )


# ── Lock-contention loss counter (#2597) ────────────────────────────────────
#
# `retry_on_locked` above turns most lock collisions into a successful write
# a few hundred milliseconds later, so a write reaching `record_audit`'s
# `except` clause with `is_lock_contention_error(exc)` true means sustained
# contention outlasted that whole retry budget — rare, but not impossible
# under a genuinely busy DB. Logging each occurrence at WARNING is exactly
# the flood #2597 measured (1,804 identical lines/day) — count them instead
# and report ONE aggregated warning per process (`atexit`, so it fires for a
# short-lived CLI invocation like `coord merge` and, if it ever shuts down
# cleanly, the long-running daemon too) rather than leaving the loss
# invisible or drowning the log in duplicates.

_lock_contention_losses = 0
_summary_flush_registered = False
# #2597-review: `coord serve` calls `record_audit` from multiple
# threads/greenlets (the tick loop's `run_in_threadpool` calls, concurrent
# request handlers) — a bare `+= 1` on a module global can lose increments
# under concurrent contention (read-modify-write race). The counter is
# purely diagnostic (never gates a decision), but the whole point of it is
# to make the loss rate trustworthy, so it should not itself be lossy.
_lock_contention_lock = threading.Lock()


def _record_lock_contention_loss() -> None:
    """Bump the run-scoped lost-write counter and arrange for the aggregate
    to be reported once this process exits (see
    :func:`_flush_lock_contention_summary`)."""
    global _lock_contention_losses, _summary_flush_registered
    with _lock_contention_lock:
        _lock_contention_losses += 1
        register = not _summary_flush_registered
        _summary_flush_registered = True
    if register:
        atexit.register(_flush_lock_contention_summary)


def _flush_lock_contention_summary() -> None:
    """Emit ONE aggregated warning for this run's audit writes lost to
    sustained lock contention, if any, and reset the counter.

    Registered via :func:`atexit.register` the first time a write is lost
    (see :func:`_record_lock_contention_loss`) rather than called from a
    fixed call site, so it fires for every entry point — `coord merge`,
    `coord notify`, the daemon — without each one needing its own hook.
    Also callable directly (tests; a caller that wants to flush mid-run
    without waiting for process exit).
    """
    global _lock_contention_losses
    with _lock_contention_lock:
        pending = _lock_contention_losses
        _lock_contention_losses = 0
    if pending:
        _log.warning("audit: %d writes lost to lock contention", pending)


def audit_lock_contention_losses() -> int:
    """Current count of audit writes lost to sustained lock contention this
    run, not yet flushed by :func:`_flush_lock_contention_summary`.

    Exposed for tests and diagnostics — advisory only, like the rest of the
    audit log; nothing gates dispatch/review/merge decisions on this value.
    """
    with _lock_contention_lock:
        return _lock_contention_losses


def flush_lock_contention_summary() -> None:
    """Public entry point for :func:`_flush_lock_contention_summary`.

    #2597-review: the `atexit` registration above is the right default for
    a short-lived CLI invocation (`coord merge`, `coord notify`) — it always
    fires exactly once, at exit. It is a much weaker guarantee for a
    long-running process (`coord serve`), which the code's own comment
    already conceded only flushes "if it ever shuts down cleanly" — for a
    daemon that runs for days between restarts, losses can accumulate for
    its entire uptime with zero visibility. Call this on a periodic cadence
    (e.g. once per daemon tick) to close that gap; it is a no-op cost-wise
    when there is nothing pending (see :func:`_flush_lock_contention_summary`).
    """
    _flush_lock_contention_summary()


def _maybe_trim(conn) -> None:
    """Opportunistic cap: when ``audit.max_rows`` is set (> 0), delete the
    oldest rows past that count after every insert.

    Default (``max_rows=0``) is unlimited — this is a no-op in the common
    case.  Config is read via :func:`_cached_config` rather than re-parsed
    on every call — see that function's docstring for why (#2654).
    """
    max_rows = _resolve_max_rows()
    if max_rows <= 0:
        return
    sql.execute(
        conn,
        "DELETE FROM audit_log WHERE id NOT IN "
        "(SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)",
        (max_rows,),
    )
    conn.commit()


# ── #2654: cached config reads for the two per-write resolvers below ───────
#
# `_resolve_level` and `_resolve_max_rows` both used to call `coord.config.
# load()` — a full disk read + `yaml.safe_load` + `parse_mapping()`
# validation — on literally every `record_audit()` call: once for the level
# gate, again (via `_maybe_trim`) after every successful insert. That
# docstring used to justify it as "audit writes are not hot-loop-frequency
# (one per board transition, not per daemon tick)" — #1896's forge-
# availability instrumentation made that false: ~2,900 writes/hour measured
# on a busy host, each paying two full parse-and-validate cycles against a
# config that is tens of KB on the daemon host.
#
# Cached by *resolved path + mtime* rather than a time-based TTL — an mtime
# check is a single stat() syscall, cheaper even than a TTL comparison, and
# it preserves the exact promise the old docstring made ("a config edit
# takes effect on the next write without a process restart") instead of
# only approximating it within some TTL window.
_config_cache: dict[str, tuple[tuple[float, int], Any]] = {}
_config_cache_lock = threading.Lock()


def _cached_config() -> Any:
    """Return the parsed ``coordinator.yml``, cached by resolved path+mtime.

    Falls through to an uncached, un-memoized :func:`coord.config.load` (and
    lets any exception propagate) when the resolved path can't even be
    ``stat()``'d — both call sites below already wrap this in a broad
    ``except Exception`` for exactly that "no resolvable config" case.
    """
    from coord.config import load as _load_config  # noqa: PLC0415
    from coord.config import resolve_config_path  # noqa: PLC0415

    path = resolve_config_path()
    try:
        st = os.stat(path)
    except OSError:
        return _load_config()

    key = str(path)
    stamp = (st.st_mtime, st.st_size)
    with _config_cache_lock:
        cached = _config_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]

    cfg = _load_config(path)
    with _config_cache_lock:
        _config_cache[key] = (stamp, cfg)
    return cfg


def _resolve_max_rows() -> int:
    """Read ``audit.max_rows`` from coordinator.yml.  Returns 0 (unlimited)
    on any failure — a missing/invalid config must not block audit writes,
    let alone the board mutation that triggered them."""
    try:
        cfg = _cached_config()
        return max(0, int(cfg.audit.max_rows))
    except Exception:  # noqa: BLE001 — best-effort; unlimited is the safe default
        return 0


def _resolve_level() -> str:
    """Read ``audit.level`` from coordinator.yml.  Returns ``"operational"``
    (the default — capture everything) on any failure, so a missing/invalid
    config never silently suppresses audit rows."""
    try:
        cfg = _cached_config()
        level = cfg.audit.level
        return level if level in _VALID_TIERS else "operational"
    except Exception:  # noqa: BLE001 — best-effort; capture-everything is the safe default
        return "operational"


# ── Read side (#1037): paginated query over audit_log ──────────────────────

_AUDIT_COLUMNS = (
    "id", "ts", "tier", "category", "event_type", "actor",
    "repo", "issue", "assignment_id", "machine", "summary", "details_json",
)


def _encode_cursor(ts: float, row_id: int) -> str:
    """Opaque-ish keyset cursor over ``(ts, id)``.  Not meant to be parsed by
    callers — just round-tripped through ``cursor`` on the next request."""
    return f"{ts!r}:{row_id}"


def _decode_cursor(cursor: str | None) -> tuple[float, int] | None:
    """Parse a cursor produced by :func:`_encode_cursor`.  Returns ``None``
    for a blank/malformed cursor — callers treat that as "first page" rather
    than raising, since a stale/garbled cursor should degrade gracefully
    (start over), not 400 the whole request."""
    if not cursor:
        return None
    try:
        ts_part, id_part = cursor.rsplit(":", 1)
        return float(ts_part), int(id_part)
    except (ValueError, TypeError):
        return None


def _row_to_entry(row: Any) -> dict[str, Any]:
    entry = {col: row[col] for col in _AUDIT_COLUMNS}
    details_raw = entry.pop("details_json")
    entry["details"] = json.loads(details_raw) if details_raw else None
    return entry


def query_audit_log(
    *,
    since: float | None = None,
    until: float | None = None,
    event_type: str | None = None,
    category: str | None = None,
    repo: str | None = None,
    issue: int | None = None,
    assignment_id: str | None = None,
    tier: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Keyset-paginated, newest-first read over ``audit_log``.

    Ordered by ``(ts, id) DESC`` — a cursor (not ``OFFSET``) carries the last
    row of the previous page, so pagination stays O(page size) as the table
    grows.  All filters are optional and AND together.  ``limit`` is clamped
    to ``(1, MAX_LIMIT]``, defaulting to ``DEFAULT_LIMIT``.

    Returns ``{"entries": [...], "next_cursor": str | None, "has_more": bool}``.
    Each entry has ``details_json`` decoded into a ``details`` dict (``None``
    when absent), matching the shape callers actually want on the wire.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))

    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts <= ?")
        params.append(until)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if category:
        clauses.append("category = ?")
        params.append(category)
    if repo:
        clauses.append("repo = ?")
        params.append(repo)
    if issue is not None:
        clauses.append("issue = ?")
        params.append(issue)
    if assignment_id:
        clauses.append("assignment_id = ?")
        params.append(assignment_id)
    if tier:
        clauses.append("tier = ?")
        params.append(tier)

    decoded_cursor = _decode_cursor(cursor)
    if decoded_cursor is not None:
        cur_ts, cur_id = decoded_cursor
        clauses.append("(ts < ? OR (ts = ? AND id < ?))")
        params.extend([cur_ts, cur_ts, cur_id])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_connection()
    # Fetch one extra row to detect has_more without a second COUNT query.
    rows = sql.execute(
        conn,
        f"SELECT {', '.join(_AUDIT_COLUMNS)} FROM audit_log {where} "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    entries = [_row_to_entry(r) for r in rows]
    next_cursor = _encode_cursor(rows[-1]["ts"], rows[-1]["id"]) if has_more and rows else None
    return {"entries": entries, "next_cursor": next_cursor, "has_more": has_more}
