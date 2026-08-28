"""Per-repo issues-sync success/attempt tracking (#2858).

**The problem this closes.** ``coord.serve_app._sync_issues_tick`` refreshes
the local ``issues`` cache every ``COORD_RECONCILE_MERGES_INTERVAL`` (default
300s) — and before this module existed, a repo whose fetch failed just logged
a ``log.warning`` and moved on (see :mod:`coord.serve_app`'s
``except Exception: log.warning(...)`` in that tick). Nothing recorded HOW
LONG a repo's cache had been failing to refresh, so 39 minutes of silent
staleness looked identical to a cache that was fresh a second ago — both to
an operator (``coord status``/``coord health`` had nothing to show) and to
:mod:`coord.drive_queue`'s ``IssueFacts.landed``, which reads that same
frozen cache with no notion of its own age.

**This module's job.** One small, best-effort, JSON-backed record of each
repo's most recent sync *attempt* and most recent sync *success* — the same
"tiny local state file, never raises, fails open" shape
:mod:`coord.github_throttle` already established for the backoff latch this
module is the direct counterpart to. Two consumers:

* :func:`coord.serve_app._sync_issues_tick` calls :func:`record_success` /
  :func:`record_failure` every tick, and reads :func:`last_success_at` BEFORE
  each repo's fetch to decide whether this repo has gone stale enough to
  earn a starvation-floor bypass of the shared backoff latch (see
  ``coord.github_ops._gh``'s ``force_through_backoff`` parameter, and
  :data:`STARVATION_FLOOR_S` below).
* :mod:`coord.health.checks.issues_sync_staleness` (via
  ``coord.health.fleet_snapshot.FleetHealthRefresher``, which stamps
  ``daemon_host["issues_sync_status"]`` from :func:`all_status` on its own
  tick cadence) turns a repo stuck past :data:`STALENESS_WARN_SECONDS` into a
  ``coord health`` finding — the operator-visible half of the same fact.

Best-effort, unconditionally, same posture as :mod:`coord.github_throttle`:
every public function here can fail (missing/unwritable state dir, a
corrupt file) without ever raising into a caller — this bookkeeping must
never become a new way to break the sync it is only trying to make visible.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

from coord.platform_paths import default_coord_dir

_log = logging.getLogger(__name__)

_STATE_FILENAME = "issues_sync_status.json"

# #2858 proposal 1: a repo that has not synced successfully in this long is
# allowed to bypass the shared `github_throttle` backoff's pre-emptive skip
# (still subject to a REAL 403 if GitHub genuinely still objects) rather than
# wait for the next scheduled tick. Set comfortably above the default 300s
# tick cadence — a single missed tick is normal jitter, not starvation — but
# well below the ~15 minute point at which :mod:`coord.health` starts
# warning, so the self-heal gets a couple of chances before an operator is
# paged. See `coord.github_ops._gh`'s `force_through_backoff` for the other
# half of this mechanism.
STARVATION_FLOOR_S = 600.0

# #2858 proposal 2: how stale a repo's last successful sync must be before
# `coord.health.checks.issues_sync_staleness` starts reporting it. The
# 2026-08-27 incident ran 39 minutes before manual intervention; this is
# comfortably inside that window so the finding fires well before a queue
# actually wedges on it.
STALENESS_WARN_SECONDS = 15 * 60.0
STALENESS_CRIT_SECONDS = 30 * 60.0


def _state_path() -> Path:
    """Resolve the sync-status state file path.

    ``$COORD_ISSUES_SYNC_STATE`` overrides first — same seam
    ``coord.github_throttle._state_path`` documents: lets a test redirect
    this with a one-line ``monkeypatch.setenv`` instead of risking a stray
    write into the operator's real ``~/.coord``.
    """
    override = os.environ.get("COORD_ISSUES_SYNC_STATE")
    if override:
        return Path(override).expanduser()
    return default_coord_dir() / _STATE_FILENAME


class RepoSyncStatus(NamedTuple):
    """One repo's last-known issues-sync attempt/success, best-effort."""

    last_success_at: float | None
    last_attempt_at: float | None
    last_error: str | None

    def age_s(self, *, now: float | None = None) -> float | None:
        """Seconds since ``last_success_at``, or ``None`` if never synced."""
        if self.last_success_at is None:
            return None
        now = now if now is not None else time.time()
        return max(0.0, now - self.last_success_at)


_EMPTY_STATUS = RepoSyncStatus(last_success_at=None, last_attempt_at=None, last_error=None)


def _read_all(path: Path | None = None) -> dict[str, dict]:
    path = path if path is not None else _state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _write_all(data: dict[str, dict]) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".issues_sync_status.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001 -- best-effort, never raises
        _log.debug("issues_sync_status: write failed: %s", exc)


def _row_to_status(row: dict) -> RepoSyncStatus:
    def _f(key: str) -> float | None:
        v = row.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    err = row.get("last_error")
    return RepoSyncStatus(
        last_success_at=_f("last_success_at"),
        last_attempt_at=_f("last_attempt_at"),
        last_error=err if isinstance(err, str) else None,
    )


def record_attempt(repo_name: str, *, now: float | None = None) -> None:
    """Best-effort: stamp *repo_name*'s most recent sync ATTEMPT.

    Called unconditionally (success or failure) so ``last_attempt_at`` alone
    can answer "is the tick still running for this repo at all" independent
    of whether it's succeeding.
    """
    now = now if now is not None else time.time()
    data = _read_all()
    row = dict(data.get(repo_name) or {})
    row["last_attempt_at"] = now
    data[repo_name] = row
    _write_all(data)


def record_success(repo_name: str, *, now: float | None = None) -> None:
    """Best-effort: stamp *repo_name*'s most recent successful sync."""
    now = now if now is not None else time.time()
    data = _read_all()
    row = dict(data.get(repo_name) or {})
    row["last_success_at"] = now
    row["last_attempt_at"] = now
    row["last_error"] = None
    data[repo_name] = row
    _write_all(data)


def record_failure(repo_name: str, error: str, *, now: float | None = None) -> None:
    """Best-effort: stamp *repo_name*'s most recent failed sync attempt.

    Does NOT touch ``last_success_at`` — that field only ever advances on an
    actual success, which is the whole point (it is the staleness clock).
    """
    now = now if now is not None else time.time()
    data = _read_all()
    row = dict(data.get(repo_name) or {})
    row["last_attempt_at"] = now
    row["last_error"] = str(error)[:500]
    data[repo_name] = row
    _write_all(data)


def last_success_at(repo_name: str) -> float | None:
    """The epoch timestamp of *repo_name*'s last successful sync, or
    ``None`` when it has never synced successfully (fresh install, or every
    attempt so far has failed)."""
    return _row_to_status(_read_all().get(repo_name) or {}).last_success_at


def status_for(repo_name: str) -> RepoSyncStatus:
    """*repo_name*'s full recorded status, or an all-``None`` row when it
    has never been recorded."""
    row = _read_all().get(repo_name)
    return _row_to_status(row) if row else _EMPTY_STATUS


def all_status() -> dict[str, RepoSyncStatus]:
    """``{repo_name: RepoSyncStatus}`` for every repo ever recorded."""
    return {name: _row_to_status(row) for name, row in _read_all().items()}


def is_starved(repo_name: str, *, now: float | None = None) -> bool:
    """True when *repo_name* has gone long enough without a successful sync
    to earn the #2858 starvation-floor bypass (:data:`STARVATION_FLOOR_S`).

    A repo that has NEVER synced successfully (``last_success_at is None``)
    counts as starved too — there is no fresher evidence to protect, so
    there is nothing lost by letting the very next attempt through for real.
    """
    now = now if now is not None else time.time()
    last_ok = last_success_at(repo_name)
    if last_ok is None:
        return True
    return (now - last_ok) >= STARVATION_FLOOR_S


def clear() -> None:
    """Drop all recorded status. Best-effort; never raises. Tests only."""
    try:
        _state_path().unlink()
    except OSError:
        pass
