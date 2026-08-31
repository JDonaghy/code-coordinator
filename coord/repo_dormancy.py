"""Per-repo dormancy skip for the daemon's periodic GitHub sweeps (#2994).

**The problem this closes.** Two of the daemon's slow-cadence ticks —
``coord.serve_app._sync_issues_tick`` and the stale-PR sweep inside
``coord.reconcile.close_stale_prs`` (driven by ``_reconcile_merges_tick``) —
loop over *every registered repo, unconditionally, every tick* and spend a
``gh`` call (or two) on each. GitHub call volume this way scales with how
many repos have ever been registered, not with how much work is actually in
flight: a repo nobody has touched in months costs exactly as much per tick
as a repo mid-pipeline.

**This module's job.** A repo counts as dormant when it has no open
assignment, no drive-queue entry, and no coord-authored open PR — see
:func:`repo_has_activity`, computed live from in-memory board state plus a
local (non-GitHub) drive-queue read, never cached. Because it is live, the
instant work is queued for a dormant repo the very next tick sees it as
active again — there is no separate "wake" call to remember to make.

A dormant repo is not *permanently* skipped, only skipped until
:data:`DORMANT_SWEEP_FLOOR_S` has passed since its last real sweep — a
floor, not a wall — so out-of-band activity (a human-opened PR, an issue
filed by someone who isn't coord) is still noticed eventually even though
nothing coord-side would otherwise prompt a look. Same best-effort,
JSON-backed, never-raises shape as the sibling starvation-floor tracker
:mod:`coord.issues_sync_status` established for exactly this kind of
bookkeeping — one file, one dict of ``{repo_name: last_swept_at}``, no new
persistence layer.

Both consumers follow the same two-call contract:

* :func:`should_skip_sweep` before spending the real ``gh`` call — True
  means skip this repo this tick.
* :func:`record_swept` right after deciding *not* to skip (regardless of
  whether the sweep itself succeeded) — resets the floor timer.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from coord.platform_paths import default_coord_dir

if TYPE_CHECKING:
    from coord.models import Board

_log = logging.getLogger(__name__)

_STATE_FILENAME = "repo_dormancy_status.json"

# How long a genuinely-idle repo may go without a real sweep before the next
# tick forces one anyway, so out-of-band activity is still noticed within a
# bounded window rather than never. Comfortably above the default 300s tick
# cadence (COORD_RECONCILE_MERGES_INTERVAL) so an idle repo actually saves
# calls on most ticks, short enough that "eventually" reads as "within the
# hour" to an operator rather than "whenever someone notices."
DORMANT_SWEEP_FLOOR_S = 3600.0


def _state_path() -> Path:
    """Resolve the dormancy state file path.

    ``$COORD_REPO_DORMANCY_STATE`` overrides first — same seam
    ``coord.issues_sync_status._state_path`` documents: lets a test redirect
    this with a one-line ``monkeypatch.setenv`` instead of risking a stray
    write into the operator's real ``~/.coord``.
    """
    override = os.environ.get("COORD_REPO_DORMANCY_STATE")
    if override:
        return Path(override).expanduser()
    return default_coord_dir() / _STATE_FILENAME


def _read_all() -> dict[str, float]:
    path = _state_path()
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
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}


def _write_all(data: dict[str, float]) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".repo_dormancy.", suffix=".tmp", dir=path.parent
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
        _log.debug("repo_dormancy: write failed: %s", exc)


def record_swept(repo_name: str, *, now: float | None = None) -> None:
    """Best-effort: stamp *repo_name* as having had a real sweep just now.

    Called right after a tick decides NOT to skip a repo — regardless of
    whether the sweep itself goes on to succeed or fail — so the floor timer
    reflects "a real gh call was spent on this repo", not "and it worked".
    """
    now = now if now is not None else time.time()
    data = _read_all()
    data[repo_name] = now
    _write_all(data)


def _last_swept_at(repo_name: str) -> float | None:
    return _read_all().get(repo_name)


def repo_has_activity(repo_name: str, board: Board) -> bool:
    """True when *repo_name* is NOT idle: an open assignment, a
    coord-authored open PR, or a drive-queue entry.

    * "Open assignment" — anything in ``board.active`` for this repo
      (pending or running work/review/smoke/conflict-fix sessions).
    * "Coord-authored open PR" — a ``board.completed`` row for this repo
      still sitting at ``status == 'done'`` (landed, PR presumably open,
      not yet flipped to ``'merged'``) — this is tracked locally already
      (it's exactly what the merge-reconcile sweeps exist to resolve), so
      no extra GitHub call is needed to answer this half of the question.
    * "Drive-queue entry" — a local (non-GitHub) DB read via
      :func:`coord.state.list_drive_queue`.

    Deliberately fails OPEN on a drive-queue read error: a repo we can't
    positively confirm is idle is treated as active, never skipped.
    """
    for a in board.active:
        if a.repo_name == repo_name:
            return True
    for a in board.completed:
        if a.repo_name == repo_name and a.status == "done":
            return True

    from coord import state  # noqa: PLC0415 -- avoid a state<->reconcile import cycle

    try:
        if state.list_drive_queue(repo_name):
            return True
    except Exception:  # noqa: BLE001 -- best-effort; treat as active on doubt
        return True

    return False


def should_skip_sweep(
    repo_name: str, board: Board, *, now: float | None = None
) -> bool:
    """True when this tick should skip *repo_name*'s GitHub sweep.

    Only ever True for a repo with zero activity (see
    :func:`repo_has_activity`) that was also swept for real inside
    :data:`DORMANT_SWEEP_FLOOR_S`. A repo with no recorded sweep at all
    counts as due, not skippable — there is no prior sweep to protect, and a
    freshly-registered repo should get its first real sweep immediately.
    """
    if repo_has_activity(repo_name, board):
        return False
    last = _last_swept_at(repo_name)
    if last is None:
        return False
    now = now if now is not None else time.time()
    return (now - last) < DORMANT_SWEEP_FLOOR_S


def clear() -> None:
    """Drop all recorded sweep timestamps. Best-effort; never raises. Tests only."""
    try:
        _state_path().unlink()
    except OSError:
        pass
