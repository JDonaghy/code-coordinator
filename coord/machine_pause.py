"""Persistent pause/resume state for machines (#routing-pause, #1563).

The pause set is a tiny JSON file at ``~/.coord/paused_machines.json``
holding ``{"paused": [<name>, ...]}``.  Both the Python coordinator
(`coord plan`, `coord assign`, auto_loop, reconcile, review,
refine_chat) and the Rust TUI read it to decide whether a given
machine is a candidate for new work — paused machines stay reachable
and visible but never receive new assignments.

Pause does NOT cancel in-flight assignments; the user can `coord stop`
those separately if needed.  This module only governs the routing
decision for *new* work.

#1563: pause is FLEET state, not host state.  The autonomous dispatcher
(`coord serve`'s `_tick_loop` → `reconcile()` / `dispatch_pending_reviews()`
/ `auto_loop`) runs *inside the daemon*, which has no `board_service`
configured for itself — so it always reads the local JSON file below,
same as before.  What used to be broken is a *thin client*: `coord pause`
run on an operator's laptop wrote to the laptop's own copy of this file,
which the daemon never saw.  The public `paused_set()` / `pause()` /
`unpause()` below now check `coord.board_service.resolve()` first: when a
board service IS configured (thin client), they route over HTTP to the
daemon's `/pause` endpoint — which itself calls the same local-only
helpers here, so both the daemon's own tick loop and every thin client
end up reading/writing the *one* copy of this file that actually governs
dispatch.  When no board service is configured (solo/local use, or the
daemon's own in-process calls), behaviour is unchanged: same file, same
atomic tempfile-rename writes.

`pause()`/`unpause()` (explicit user actions) fail LOUDLY on a thin
client — an HTTP/transport error propagates rather than reporting
success (#1563: "there is no configuration in which a thin-client pause
fails loudly. It always reports success and always fails open").
`paused_set()` (the read side, consulted on every dispatch decision)
stays fail-soft on a remote fetch error, consistent with this module's
existing local-read behaviour and every other daemon read-through helper
in `coord.client` (`fetch_issue_context`, `fetch_drive_escalations`, …):
a transient network blip degrades to "nothing is paused" rather than
wedging the dispatcher, matching the pre-existing contract documented
below for a malformed/missing local file.

#1862: per-machine quiet hours.  `Machine.quiet_hours` (parsed by
`coord.config`) declares a recurring daily window during which a machine
should receive no NEW dispatch — the same routing-pause semantics as an
explicit `coord pause`, computed instead of stored.  This module is the
single place that union happens: `paused_set()`/`local_paused_set()`
accept an optional `machines` sequence and, when given one, fold in
`{m.name for m in machines if m.quiet_hours.covers(now)}` — every one of
the eight call sites listed in #1862 already has its `Config` in local
scope, so they pass `config.machines` and inherit quiet hours with no
second routing check anywhere else.  `machines=None` (the default)
degrades to "explicit pauses only", i.e. unchanged pre-#1862 behaviour —
this is what keeps every deployment with no `quiet_hours:` block, and
every caller not yet threading `machines` through, byte-identical to
before.

#2101: RELEASE CORDONS.  A cordon is the same *routing* semantics as a pause —
no new agents route to that machine, in-flight work is untouched — set
automatically by `coord release propagate` so a host behind the released
version drains itself into a rollable state instead of waiting for a window
that never arrives.  It shares `paused_set()` (every dispatcher already reads
it, so nothing else in the fleet needs to learn a second concept) and NOTHING
else:

* it lives under its own `release_cordons` key, with an `owner`, a `reason`,
  a `created_at` and an `expires_at` — `local_pause`/`local_unpause` never
  read or write it, and `set_cordon`/`clear_cordon` never touch `paused`.
  An operator's `coord unpause` therefore cannot lift a cordon mid-drain, and
  the post-roll uncordon cannot clear a pause an operator set deliberately
  (#2101 trap A);
* it EXPIRES.  The cordon lives in daemon state and the daemon is restarted
  by the very roll it gates, so a propagate run killed between cordon and
  uncordon would otherwise leave the fleet refusing work forever — which
  looks exactly like a quiet fleet (#2082 in a new costume).  The read side
  (`local_cordons()`) simply ignores an expired record, so nothing has to run
  for a cordon to lapse; the live loop renews on every run while the host is
  still behind (#2101 trap B).

#2240: A CORDON MEANS "NO NEW WORK", AND A FOLLOW-ON LEG IS NOT NEW WORK.
Folding cordons into the one `paused_set()` is what made #2101 buildable, but
it also gave a cordon a reach a *drain* must not have.  Observed 2026-08-14:
the fleet cordoned to drain for v0.5.77; an entry that had finished Work and
Test needed its REVIEW dispatched; `pick_reviewer_machine` filtered every
cordoned host out of `paused_set()` and returned "no eligible reviewer
machine configured"; the entry therefore stayed `running` forever; a
perpetually-running entry defers the roll; a deferred roll leaves the cordon
up.  Four cycles, 70 minutes, three idle machines, no exit without a human.

The cordon was blocking the completion of the very work it was waiting to
drain.  So `paused_set(..., include_cordons=False)` — reached through
`follow_on_paused_set()` — is the set a dispatch consults when it is
finishing work already in flight rather than starting new work.  It still
honours explicit `coord pause` and quiet hours: those are an operator's or a
policy's decision about a machine and mean what they say.  A cordon is this
fleet's own drain asking a host to go idle, and refusing to dispatch the leg
that would make it idle is self-defeating (a `--merge-of`/review dispatch
onto a cordoned host is what the drain WANTS — it is what ends the work).

#2146: OPERATOR-SET quiet hours.  #1862's window can only be declared in
`coordinator.yml`, which on a thin client is a read-only cache that is
re-fetched and overwritten on essentially every command, and on the daemon
host is a symlink into a git checkout — so setting one cost an ssh round
trip, an edit, a commit and a push.  A window set with `coord quiet-hours`
(or the daemon's `/pause` `set-quiet` action) is instead stored HERE, under
a fourth `quiet_hours` key, alongside `paused` / `quiet_overrides` /
`release_cordons`.  It deliberately does NOT rewrite `coordinator.yml`: that
file is full of load-bearing comments a PyYAML round-trip would eat, and
every edit would leave uncommitted drift for the next `git pull` there to
conflict with.  `coord quiet-hours --print-yaml` is the promotion path when
a window should become permanent and version-controlled.

Precedence is per machine and TOTAL: a store entry overrides that machine's
`coordinator.yml` block entirely rather than merging field-by-field (a
half-config, half-store window is unexplainable at 2AM).  Provenance rides
along to every display surface — `effective_quiet_hours()` carries a
``source``, and `describe_pause_state()` names it — so the two sources can
never be silently confused.  `_quiet_covered_names()` is the single point
that union happens, so nothing else in the fleet learns a second concept.

`coord unpause` during an active quiet window would otherwise be a lie
(#1563's failure class: reports success, changes nothing, the machine is
paused again on the very next read).  `local_unpause_effective()` picks
the "explicit override" resolution named in #1862: unpausing a
quiet-covered, not-explicitly-paused machine records an override that
suppresses quiet hours until the CURRENT window's end (persisted
alongside the explicit-pause list below, under `quiet_overrides`) and
says so — never silently re-paused, never silently accepted as a no-op.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from coord.platform_paths import default_coord_dir

if TYPE_CHECKING:
    from coord.models import Machine, QuietHours

# #2146: provenance of an effective quiet-hours window. Carried on every
# display surface so an operator can tell a window someone set with `coord
# quiet-hours` (mutable, lives in this host's state file) from one declared
# in `coordinator.yml` (version-controlled, needs a commit to change).
SOURCE_STORE = "store"
SOURCE_CONFIG = "config"

_STATE_FILENAME = "paused_machines.json"


def _state_path() -> Path:
    """Return the absolute path to the pause-state file.

    Lives alongside the rest of the runtime state (`assignments.db`,
    `agent_state.json`, etc.) under the coordinator's platform-appropriate
    state root.

    #2683 (W3): this used to build ``Path(os.environ.get("HOME", "/tmp"))``
    directly -- correct on POSIX, but Windows sets ``USERPROFILE``, not
    reliably ``HOME``, so a bare-``HOME`` read silently fell back to
    ``"/tmp"``, which resolves nowhere useful there. `default_coord_dir()`
    is the one seam every other on-disk state root in this package already
    derives from (`coord.db.COORD_DIR`, `coord.state.COORD_DIR`,
    `coord.config.USER_CONFIG_PATH`) -- it resolves via `Path.home()`
    (which honours `USERPROFILE` on Windows, `HOME` on POSIX) rather than a
    raw environment-variable read, so there is no silent fallback to a path
    that happens to exist but means nothing.
    """
    return default_coord_dir() / _STATE_FILENAME


def paused_set(
    machines: Sequence["Machine"] | None = None,
    *,
    now: datetime | None = None,
    include_cordons: bool = True,
) -> set[str]:
    """Read the current set of paused machine names (#1563: daemon-aware).

    Routes through the daemon's `/pause` endpoint when a board service is
    configured (thin client); otherwise reads the local file directly. The
    remote fetch fails SOFT (any error → empty set) — see module docstring
    for why reads stay fail-open while explicit `pause()`/`unpause()` calls
    don't.

    On a thin client *machines*/*now* are ignored — the daemon's own
    `/pause` endpoint already folds its quiet-hours-covered machines into
    the response (see `coord.serve_app.get_pause`), so the thin client's
    view is correct without knowing about quiet hours itself. Pass
    *machines* (almost always `config.machines`, already in scope at every
    call site) to fold quiet hours into the LOCAL computation — the daemon's
    own in-process tick-loop calls (no board service configured for
    itself), and any solo/local use with no daemon at all.

    *include_cordons* (#2240) is False only for a dispatch that FINISHES work
    already in flight — see `follow_on_paused_set()`, which is the name every
    caller should use, and the module docstring for the 70-minute fleet-wide
    deadlock that made this necessary. It never widens the set: explicit
    pauses and quiet hours are untouched, and a cordon read that fails is
    left IN (a cordon we could not resolve stays a pause, the same direction
    every other read here fails in).
    """
    svc = _resolve_service()
    if svc is not None:
        from coord.client import fetch_paused_machines  # noqa: PLC0415

        try:
            names = fetch_paused_machines(svc)
        except Exception:  # noqa: BLE001 — fail-soft read, see module docstring
            return set()
        if include_cordons:
            return names
        # The daemon publishes ONE union (`local_paused_set`), so the cordon
        # half has to be subtracted here from its own endpoint — a second
        # round trip, paid only on the follow-on path, rather than a second
        # union the daemon would have to learn to publish.
        try:
            return names - set(cordons())
        except Exception:  # noqa: BLE001 — see docstring: cordons stay paused
            return names
    return local_paused_set(machines, now=now, include_cordons=include_cordons)


def follow_on_paused_set(
    machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> set[str]:
    """The pause set for a dispatch that COMPLETES in-flight work (#2240).

    `paused_set()` minus release cordons. Use this — and only this — for a
    review / smoke / fix leg of an assignment that is already running: a
    cordon means "route no NEW work here", and the tail of the work the
    cordon is explicitly waiting to drain is not new work. Blocking it is
    self-defeating in the precise way #2240 observed, because the entry can
    then never finish, so the host never drains, so the cordon never lifts,
    so the roll defers and re-cordons.

    Everything else still applies: an explicit `coord pause` and a quiet-hours
    window both mean "this machine is unavailable, full stop", and neither is
    this fleet's own drain talking to itself.
    """
    return paused_set(machines, now=now, include_cordons=False)


def is_paused(
    name: str, machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> bool:
    """Convenience: True when *name* is in the paused set."""
    return name in paused_set(machines, now=now)


def pause(name: str) -> bool:
    """Add *name* to the paused set.  Returns True when the set changed
    (i.e. *name* was not already paused).

    #1563: on a thin client this POSTs to the daemon's `/pause` endpoint
    and raises (`httpx.HTTPError`) if that can't be confirmed — a pause
    that silently fails to reach the daemon is the exact failure mode this
    module exists to close. Callers that must never raise (e.g. an
    interactive command that wants to print a clean error) should catch
    around this call.
    """
    svc = _resolve_service()
    if svc is not None:
        from coord.client import post_pause  # noqa: PLC0415

        result = post_pause(svc, name, "pause")
        return bool(result.get("changed"))
    return local_pause(name)


@dataclass(frozen=True)
class UnpauseOutcome:
    """Result of an #1862-aware unpause — see `local_unpause_effective()`.

    `kind` is one of:
      * ``"resumed"``        — an explicit `coord pause` was lifted.
      * ``"quiet_override"`` — the machine wasn't explicitly paused but WAS
        inside its quiet-hours window; an override now suppresses that
        window until it would have ended anyway (`quiet_until`/`tz`).
      * ``"not_paused"``     — genuinely not paused by any mechanism; a
        true no-op, distinguishable from the two "did something" cases
        above so a caller never reports success for nothing happening.
    """

    changed: bool
    kind: str
    quiet_until: str | None = None  # local "HH:MM" the override lasts until
    tz: str | None = None


def unpause(
    name: str, machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> UnpauseOutcome:
    """Resolve `coord unpause <name>` (#1862-aware).  See `pause()` for the
    thin-client routing / fail-loudly contract, and `UnpauseOutcome` for
    what `.kind` means.

    #1862: unpausing a machine that is inside its quiet-hours window but
    was never explicitly paused must not silently no-op (the machine would
    be paused again on the very next read — #1563's failure class) and
    must not silently pretend to have lifted a pause that was never set.
    Instead it records an explicit override for the remainder of that
    window — see `local_unpause_effective()`.
    """
    svc = _resolve_service()
    if svc is not None:
        from coord.client import post_pause  # noqa: PLC0415

        result = post_pause(svc, name, "unpause")
        changed = bool(result.get("changed"))
        return UnpauseOutcome(
            changed=changed,
            kind=result.get("kind") or ("resumed" if changed else "not_paused"),
            quiet_until=result.get("quiet_until"),
            tz=result.get("tz"),
        )
    return local_unpause_effective(name, machines, now=now)


def _resolve_service():  # -> coord.client.ServiceConfig | None
    from coord.board_service import resolve  # noqa: PLC0415

    return resolve()


# ── local-only (#1563: always used by the daemon's own `/pause` endpoint and
# tick loop, and by every caller when no board service is configured) ───────


def local_paused_set(
    machines: Sequence["Machine"] | None = None,
    *,
    now: datetime | None = None,
    include_cordons: bool = True,
) -> set[str]:
    """The local, effective paused-machine set: explicit pauses UNION any
    machine currently inside its quiet-hours window (#1862) UNION any machine
    under an unexpired release cordon (#2101), minus any machine with an
    active `coord unpause` override for that quiet window.

    Omitting *machines* drops only the quiet-hours half — that is what keeps
    every pre-#1862 caller (and `local_pause`/`local_unpause` below, which use
    `_explicit_paused_set()` directly to test EXPLICIT membership)
    byte-identical. Cordons need no config to resolve, so they are folded in
    unconditionally: a cordon that only applied when the caller happened to
    pass `config.machines` would be a cordon half the dispatchers ignore.
    Malformed/missing local file degrades to "nothing paused" — failure to
    read should never block routing.

    Unlike `paused_set()`, this NEVER goes over the network — it is what
    the daemon's `/pause` endpoint handler calls (passing `config.machines`
    so quiet hours apply), and what `paused_set()` itself falls through to
    when no board service is configured.

    *include_cordons* (#2240) drops the cordon half for a dispatch that
    finishes work already in flight — see `follow_on_paused_set()`. The
    daemon's own `/pause` handler must never pass it: what that endpoint
    publishes is the full routing set, and the subtraction is the caller's
    decision to make, per dispatch.
    """
    effective = _explicit_paused_set()
    if include_cordons:
        effective |= cordoned_names(now=_epoch(now))
    if not machines:
        return effective
    return effective | _quiet_covered_names(machines, now=now)


def local_pause(name: str) -> bool:
    """Add *name* to the local EXPLICIT paused set.  Returns True when the
    set changed (i.e. *name* was not already explicitly paused).

    Deliberately orthogonal to quiet hours: an explicit pause is tracked
    and reported independently of whatever a machine's `quiet_hours` window
    happens to be doing at the same moment.
    """
    current = _explicit_paused_set()
    if name in current:
        return False
    current.add(name)
    _save_state(paused=current)
    return True


def local_unpause(name: str) -> bool:
    """Remove *name* from the local EXPLICIT paused set.  Returns True when
    the set changed (i.e. *name* was actually explicitly paused).

    This is the pre-#1862 primitive: it only ever looks at explicit pauses,
    so it correctly reports "not paused" (False) for a machine that is
    merely inside its quiet-hours window — `local_unpause_effective()`
    below is what gives THAT case a truthful, non-lying answer.
    """
    current = _explicit_paused_set()
    if name not in current:
        return False
    current.discard(name)
    _save_state(paused=current)
    return True


def local_unpause_effective(
    name: str, machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> UnpauseOutcome:
    """#1862: the truthful `coord unpause` — see `UnpauseOutcome` for the
    three possible outcomes and the module docstring for why a bare
    `local_unpause()` (explicit-only) would otherwise let `coord unpause`
    report success and change nothing for a quiet-covered machine.
    """
    if local_unpause(name):
        return UnpauseOutcome(changed=True, kind="resumed")

    now = now if now is not None else datetime.now(timezone.utc)
    # #2146: resolve through the same union routing uses, so `coord unpause`
    # against an operator-SET window grants a real override instead of the
    # "not paused" lie it would report if it only knew about config blocks.
    window = _effective_quiet_windows(machines).get(name)
    qh = window[0] if window is not None else None
    if qh is not None and qh.covers(now):
        until_utc = qh.window_end_instant(now)
        _set_quiet_override(name, until_utc)
        until_local = until_utc.astimezone(ZoneInfo(qh.tz))
        return UnpauseOutcome(
            changed=True,
            kind="quiet_override",
            quiet_until=until_local.strftime("%H:%M"),
            tz=qh.tz,
        )
    return UnpauseOutcome(changed=False, kind="not_paused")


# ── #1862: quiet-hours computation ──────────────────────────────────────────


def quiet_paused_names(
    machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> set[str]:
    """Public: names of machines currently paused SPECIFICALLY because a
    `quiet_hours` window covers *now* (never overridden — an active
    `coord unpause` override excludes a machine from this set, same as it
    excludes it from `local_paused_set()`'s union).

    Always a subset of `local_paused_set(machines, now=now)`. Review finding
    on #1862's original PR: `coord status`'s `describe_pause_state()`
    distinguished a quiet-paused machine from a hand-paused one, but the
    daemon's `/pause` endpoint and the TUI sidebar badge did not — this is
    the choke point both now call so a machine "asleep until 08:00" reads
    differently from one someone explicitly paused, everywhere pause state
    is displayed, not just `coord status`.
    """
    if not machines:
        return set()
    return _quiet_covered_names(machines, now=now)


def _quiet_covered_names(
    machines: Sequence["Machine"], *, now: datetime | None = None,
) -> set[str]:
    """Names of machines whose effective quiet-hours window covers *now*,
    excluding any with an active `coord unpause` override for that window.

    #2146: THE single union point of the two sources — a machine's
    `coordinator.yml` block and an operator-set store entry, the latter
    winning outright. Every routing call site reaches quiet hours through
    `local_paused_set()` → here, so there is exactly one place that
    precedence is decided.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    overridden = _active_quiet_override_names(now)
    return {
        name
        for name, (window, _source) in _effective_quiet_windows(machines).items()
        if name not in overridden and window.covers(now)
    }


def _effective_quiet_windows(
    machines: Sequence["Machine"] | None,
) -> dict[str, tuple["QuietHours", str]]:
    """``{machine: (QuietHours, source)}`` — config blocks, overridden per
    machine by any operator-set store entry (#2146).

    *source* is `SOURCE_CONFIG` or `SOURCE_STORE`. A store entry for a
    machine with no `quiet_hours:` block at all is included too (that is the
    common case: the operator wants a window on a machine whose YAML never
    declared one), as is one for a name that is no longer in *machines* —
    hiding it would leave an entry nothing could see to clear.
    """
    out: dict[str, tuple[QuietHours, str]] = {}
    for m in machines or ():
        if m.quiet_hours is not None:
            out[m.name] = (m.quiet_hours, SOURCE_CONFIG)
    for name, window in _stored_quiet_windows().items():
        out[name] = (window, SOURCE_STORE)  # store wins, entirely
    return out


def _active_quiet_override_names(now: datetime | None = None) -> set[str]:
    now = now if now is not None else datetime.now(timezone.utc)
    active: set[str] = set()
    for name, raw_until in _quiet_overrides().items():
        try:
            until = datetime.fromisoformat(raw_until)
        except ValueError:
            continue
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if now < until:
            active.add(name)
    return active


def _set_quiet_override(name: str, until_utc: datetime) -> None:
    overrides = _quiet_overrides()
    overrides[name] = until_utc.astimezone(timezone.utc).isoformat()
    _save_state(quiet_overrides=overrides)


# ── #2146: operator-set quiet hours (the FOURTH independent axis) ───────────
#
# Same shape as #2101's cordons: its own key, its own writers, and
# `_save_state` preserving whichever axes a writer didn't pass. Setting or
# clearing a window must not clear a pause, a cordon or an unpause override —
# and none of those may clear a window.


def _parse_stored_window(raw: Mapping) -> "QuietHours":
    """Validate one store record into a `QuietHours`, or raise.

    Deliberately the SAME validator `coordinator.yml` uses
    (`coord.config.parse_quiet_hours_window`) rather than a second, local
    one — see that function for why the two paths must never diverge.
    """
    from coord.config import parse_quiet_hours_window  # noqa: PLC0415 — leaf import

    return parse_quiet_hours_window(
        raw.get("start"), raw.get("end"), raw.get("tz"), prefix="quiet_hours"
    )


def _stored_quiet_windows() -> dict[str, "QuietHours"]:
    """``{machine: QuietHours}`` from the operator-set store.

    A malformed record is dropped INDIVIDUALLY, never failing the whole read:
    this is consulted on every dispatch decision in the fleet, so one bad row
    (a hand-edited file, a half-written key from an older build) must not
    blank every other machine's window.
    """
    out: dict[str, QuietHours] = {}
    for name, raw in _quiet_hours_records().items():
        try:
            out[name] = _parse_stored_window(raw)
        except Exception:  # noqa: BLE001, PERF203 — drop this row, keep the rest
            continue
    return out


def local_effective_quiet_hours(
    machines: Sequence["Machine"] | None = None,
) -> dict[str, dict]:
    """``{machine: {start, end, tz, source}}`` for EVERY machine with a
    window — not just the ones a window currently covers.

    The render surface for `coord quiet-hours --list`, the daemon's `GET
    /pause`, and (#2147) the TUI's pre-filled dialog. `start`/`end` are
    ``"HH:MM"`` strings and `source` is `SOURCE_CONFIG`/`SOURCE_STORE`, so
    the payload is JSON-ready with no further conversion.
    """
    return {
        name: {
            "start": window.start.strftime("%H:%M"),
            "end": window.end.strftime("%H:%M"),
            "tz": window.tz,
            "source": source,
        }
        for name, (window, source) in sorted(_effective_quiet_windows(machines).items())
    }


def local_set_quiet_hours(name: str, *, start: str, end: str, tz: str) -> "QuietHours":
    """Write *name*'s operator-set quiet-hours window. Returns the stored
    `QuietHours`; raises `coord.config.ConfigError` on a malformed window.

    Local-only sibling of `local_set_cordon` — never touches `paused`,
    `quiet_overrides` or `release_cordons`.
    """
    window = _parse_stored_window({"start": start, "end": end, "tz": tz})
    records = _quiet_hours_records()
    records[name] = {
        "start": window.start.strftime("%H:%M"),
        "end": window.end.strftime("%H:%M"),
        "tz": window.tz,
    }
    _save_state(quiet_hours=records)
    return window


def local_clear_quiet_hours(name: str) -> bool:
    """Drop *name*'s operator-set window. True when one was actually there.

    False means "nothing was set here" — the caller must report that rather
    than claiming success, since the machine may still have a
    `coordinator.yml` block this cannot clear (only a commit there can).
    """
    records = _quiet_hours_records()
    if name not in records:
        return False
    del records[name]
    _save_state(quiet_hours=records)
    return True


# ── #2146: daemon-aware quiet-hours read/write (same seam as pause/cordon) ───


def effective_quiet_hours(
    machines: Sequence["Machine"] | None = None,
) -> dict[str, dict]:
    """`{machine: {start, end, tz, source}}` from the daemon when one is
    configured (#1563's seam), else locally.

    Fail-soft on a remote error, exactly like `paused_set()`/`cordons()`:
    this is a read, and a network blip should degrade to "no windows known"
    rather than wedge a status render. The WRITES below fail loudly.
    """
    svc = _resolve_service()
    if svc is None:
        return local_effective_quiet_hours(machines)
    from coord.client import fetch_quiet_hours  # noqa: PLC0415

    try:
        return fetch_quiet_hours(svc)
    except Exception:  # noqa: BLE001 — fail-soft read, see module docstring
        return {}


def set_quiet_hours(name: str, *, start: str, end: str, tz: str) -> dict:
    """Set *name*'s quiet-hours window, daemon-first. Returns the stored
    ``{start, end, tz, source}`` record.

    Raises on a transport/HTTP failure (and on a malformed window, which the
    daemon answers 400 for). A thin client's write MUST fail loudly: the
    whole reason this feature exists is that `coordinator.yml` edits on a
    thin client silently revert, and "reported success, changed nothing" is
    #1563's exact failure class — the single worst outcome here.
    """
    svc = _resolve_service()
    if svc is None:
        window = local_set_quiet_hours(name, start=start, end=end, tz=tz)
        return {
            "start": window.start.strftime("%H:%M"),
            "end": window.end.strftime("%H:%M"),
            "tz": window.tz,
            "source": SOURCE_STORE,
        }
    from coord.client import post_quiet_hours  # noqa: PLC0415

    result = post_quiet_hours(svc, name, "set-quiet", start=start, end=end, tz=tz)
    return dict(result.get("window") or {"start": start, "end": end, "tz": tz})


def clear_quiet_hours(name: str) -> bool:
    """Clear *name*'s operator-set window, daemon-first. Raises on a
    transport/HTTP failure. False means nothing was set."""
    svc = _resolve_service()
    if svc is None:
        return local_clear_quiet_hours(name)
    from coord.client import post_quiet_hours  # noqa: PLC0415

    return bool(post_quiet_hours(svc, name, "clear-quiet").get("changed"))


# ── #2101: release cordons ───────────────────────────────────────────────────
#
# A THIRD, independent axis of this file (see the module docstring). The three
# never write each other's key: `paused` is the operator's, `quiet_overrides`
# is `coord unpause`'s, `release_cordons` is `coord release propagate`'s.
# `_save_state` preserves whichever two the caller didn't pass, which is the
# single line that makes "each clears only its own" true rather than merely
# intended.


def _epoch(now: "datetime | float | None") -> float | None:
    """Accept either clock form the callers in this module use.

    Quiet hours are computed from an aware `datetime`; cordon expiry is an
    epoch float (it is compared against `time.time()` stamps written by the
    propagate shell). One helper rather than two clock conventions leaking
    into every signature.
    """
    if now is None:
        return None
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _now_epoch(now: float | None = None) -> float:
    return now if now is not None else datetime.now(timezone.utc).timestamp()


def local_cordons(*, now: float | None = None, include_expired: bool = False) -> dict:
    """``{machine: Cordon}`` — the local cordon store.

    Expired records are dropped unless *include_expired*: a cordon lapses by
    being IGNORED, never by being cleaned up, so a propagate run that died
    mid-drain (or a daemon that was restarted by the very roll it was gating)
    cannot leave the fleet cordoned forever — trap B of #2101. Nothing has to
    run for that to hold.
    """
    from coord.release_cordon import Cordon  # noqa: PLC0415 — leaf import

    when = _now_epoch(now)
    out: dict[str, Cordon] = {}
    for name, raw in _cordon_records().items():
        record = Cordon.from_dict({**raw, "machine": raw.get("machine") or name})
        if include_expired or record.active(when):
            out[name] = record
    return out


def cordoned_names(*, now: float | None = None) -> set[str]:
    """Names of machines under an unexpired release cordon.

    Always a subset of `local_paused_set()`; fail-soft on an unreadable store,
    same contract as `_explicit_paused_set()`.
    """
    try:
        return set(local_cordons(now=now))
    except Exception:  # noqa: BLE001 — a bad store must never wedge routing
        return set()


def local_set_cordon(
    name: str,
    *,
    reason: str = "",
    target_version: str | None = None,
    ttl_seconds: float | None = None,
    owner: str | None = None,
    created_at: float | None = None,
    now: float | None = None,
) -> object:
    """Write (or renew) *name*'s release cordon. Returns the stored `Cordon`.

    Renewal preserves `created_at` — the drain deadline (#2101 trap C)
    measures from the FIRST cordon of this drain, so a wedged host cannot
    postpone its own escalation by being renewed every 20 minutes.
    """
    from coord.release_cordon import (  # noqa: PLC0415
        DEFAULT_TTL_SECONDS,
        OWNER_RELEASE,
        Cordon,
    )

    when = _now_epoch(now)
    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else max(0.0, float(ttl_seconds))
    existing = _cordon_records().get(name) or {}
    try:
        previous_created = float(existing.get("created_at") or 0.0)
    except (TypeError, ValueError):
        previous_created = 0.0
    record = Cordon(
        machine=name,
        owner=owner or OWNER_RELEASE,
        reason=reason,
        target_version=target_version,
        created_at=created_at or previous_created or when,
        renewed_at=when,
        expires_at=when + ttl,
    )
    records = _cordon_records()
    records[name] = record.to_dict()
    _save_state(cordons=records)
    return record


def local_clear_cordon(name: str, *, owner: str | None = None) -> bool:
    """Drop *name*'s cordon. True when one was actually there.

    *owner* (when given) is enforced: an owner may only clear its own cordon.
    An operator's `coord pause` is untouched either way — it is not stored
    here at all.
    """
    records = _cordon_records()
    record = records.get(name)
    if record is None:
        return False
    if owner is not None and str(record.get("owner") or "") != owner:
        return False
    del records[name]
    _save_state(cordons=records)
    return True


def local_prune_cordons(*, now: float | None = None) -> list[str]:
    """Delete expired records and return their names.

    Purely hygiene: `local_cordons()` already ignores them, so nothing
    depends on this having run. It exists so the store does not accumulate
    dead rows and so `coord release cordon --list` can report "these lapsed"
    once rather than forever.
    """
    when = _now_epoch(now)
    live = set(local_cordons(now=when))
    records = _cordon_records()
    dropped = sorted(name for name in records if name not in live)
    if dropped:
        for name in dropped:
            records.pop(name, None)
        _save_state(cordons=records)
    return dropped


# ── #2101: daemon-aware cordon read/write (same seam as pause/unpause) ───────


def cordons(*, now: float | None = None) -> dict:
    """`{machine: Cordon}` from the daemon when one is configured (#1563's
    seam), else the local store. Fail-soft on a remote error, exactly like
    `paused_set()` — see the module docstring for why reads degrade to
    "nothing is cordoned" while writes below fail loudly.
    """
    svc = _resolve_service()
    if svc is None:
        return local_cordons(now=now)
    from coord.client import fetch_cordons  # noqa: PLC0415
    from coord.release_cordon import Cordon  # noqa: PLC0415

    try:
        rows = fetch_cordons(svc)
    except Exception:  # noqa: BLE001 — fail-soft read, see module docstring
        return {}
    when = _now_epoch(now)
    out: dict[str, Cordon] = {}
    for raw in rows:
        record = Cordon.from_dict(raw)
        if record.machine and record.active(when):
            out[record.machine] = record
    return out


def set_cordon(
    name: str,
    *,
    reason: str = "",
    target_version: str | None = None,
    ttl_seconds: float | None = None,
) -> object:
    """Cordon *name*, daemon-first. Raises on a transport/HTTP failure.

    A cordon that silently fails to reach the daemon is worse than no cordon
    at all: `coord release propagate` would go on to restart agents believing
    it had stopped new work, which is the in-flight-worker massacre the whole
    quiescence design exists to prevent. So this fails LOUDLY, like
    `pause()`/`unpause()` and unlike the read side.
    """
    svc = _resolve_service()
    if svc is None:
        return local_set_cordon(
            name,
            reason=reason,
            target_version=target_version,
            ttl_seconds=ttl_seconds,
        )
    from coord.client import post_cordon  # noqa: PLC0415
    from coord.release_cordon import Cordon  # noqa: PLC0415

    result = post_cordon(
        svc,
        name,
        "cordon",
        reason=reason,
        target_version=target_version,
        ttl_seconds=ttl_seconds,
    )
    return Cordon.from_dict(result.get("cordon") or {"machine": name})


def clear_cordon(name: str) -> bool:
    """Uncordon *name*, daemon-first. Raises on transport/HTTP failure."""
    svc = _resolve_service()
    if svc is None:
        return local_clear_cordon(name)
    from coord.client import post_cordon  # noqa: PLC0415

    return bool(post_cordon(svc, name, "uncordon").get("changed"))


# ── internals ────────────────────────────────────────────────────────────────


def _load_raw() -> dict:
    path = _state_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _explicit_paused_set() -> set[str]:
    try:
        data = _load_raw()
    except (OSError, ValueError):
        return set()
    items = data.get("paused")
    if not isinstance(items, list):
        return set()
    return {str(x) for x in items if isinstance(x, str) and x}


def _quiet_overrides() -> dict[str, str]:
    try:
        data = _load_raw()
    except (OSError, ValueError):
        return {}
    items = data.get("quiet_overrides")
    if not isinstance(items, dict):
        return {}
    return {str(k): str(v) for k, v in items.items() if isinstance(k, str) and isinstance(v, str)}


def _cordon_records() -> dict[str, dict]:
    """#2101: the raw `release_cordons` map, `{machine: record}`.

    Malformed entries are dropped individually rather than failing the whole
    read — this is consulted on every dispatch decision in the fleet.
    """
    try:
        data = _load_raw()
    except (OSError, ValueError):
        return {}
    items = data.get("release_cordons")
    if not isinstance(items, dict):
        return {}
    return {
        str(k): dict(v)
        for k, v in items.items()
        if isinstance(k, str) and k and isinstance(v, dict)
    }


def _quiet_hours_records() -> dict[str, dict]:
    """#2146: the raw operator-set `quiet_hours` map, `{machine: record}`.

    Malformed entries are dropped individually rather than failing the whole
    read — same contract as `_cordon_records()` above, and for the same
    reason: this is consulted on every dispatch decision in the fleet.
    """
    try:
        data = _load_raw()
    except (OSError, ValueError):
        return {}
    items = data.get("quiet_hours")
    if not isinstance(items, dict):
        return {}
    return {
        str(k): dict(v)
        for k, v in items.items()
        if isinstance(k, str) and k and isinstance(v, dict)
    }


def _save_state(
    *,
    paused: set[str] | None = None,
    quiet_overrides: dict[str, str] | None = None,
    cordons: dict[str, dict] | None = None,
    quiet_hours: dict[str, dict] | None = None,
) -> None:
    """Read-modify-write the local state file, preserving whichever axes the
    caller doesn't pass.

    Explicit pauses, quiet-hours overrides, release cordons (#2101) and
    operator-set quiet hours (#2146) are four INDEPENDENT axes with four
    different owners; a write to one must never clobber another. This one
    function is what makes #2101 trap A ("each must clear only its own")
    true rather than merely intended, so every new key added here must be
    preserved the same way.
    """
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new_paused = sorted(paused) if paused is not None else sorted(_explicit_paused_set())
    new_overrides = quiet_overrides if quiet_overrides is not None else _quiet_overrides()
    new_cordons = cordons if cordons is not None else _cordon_records()
    new_quiet = quiet_hours if quiet_hours is not None else _quiet_hours_records()
    payload = {
        "paused": new_paused,
        "quiet_overrides": dict(sorted(new_overrides.items())),
        "release_cordons": {k: dict(v) for k, v in sorted(new_cordons.items())},
        "quiet_hours": {k: dict(v) for k, v in sorted(new_quiet.items())},
    }
    # Atomic write: tempfile in the same dir then rename so a crashed
    # writer can never leave a partially-written file in place.
    fd, tmp = tempfile.mkstemp(prefix=".paused_machines.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── #1862: pause-state display (`coord status`, and any future consumer
# that wants to distinguish a hand pause from a quiet-hours one) ────────────


@dataclass(frozen=True)
class PauseState:
    """Why a machine is (or isn't, but would be if not overridden) paused,
    for display purposes only — never consulted for routing.

    `kind` is one of:
      * ``"hand"``             — an explicit `coord pause`.
      * ``"quiet"``            — inside its quiet-hours window right now.
      * ``"quiet_overridden"`` — inside its quiet-hours window, but a
        `coord unpause` override is currently suppressing it (so it is NOT
        in the effective paused set, and IS dispatchable).
      * ``"cordon"``           — a #2101 release cordon: this machine is
        draining so it can be rolled onto the released version. It will lift
        itself the moment the roll lands (or expire on its own if the run
        that set it died), so it must NOT read as "someone paused this".

    #2146: a ``"quiet"`` detail also names WHERE the window came from
    ("set here" vs "from coordinator.yml"), so an operator can tell a knob
    they can flip in seconds from one that needs a commit.
    """

    kind: str
    detail: str


def _source_label(source: str) -> str:
    """Human wording for a window's provenance (#2146)."""
    return "set here" if source == SOURCE_STORE else "from coordinator.yml"


def _resolve_display_window(
    machine: "Machine", quiet_hours: "Mapping[str, Mapping] | None",
) -> "tuple[QuietHours | None, str]":
    """The window to DISPLAY for *machine*, plus its source (#2146).

    Never consulted for routing — `_quiet_covered_names()` is the only place
    precedence decides anything that matters. This mirrors it for display,
    with the extra ability to render from an already-fetched remote map.
    """
    if quiet_hours is not None:
        record = quiet_hours.get(machine.name)
        if record is None:
            return None, SOURCE_CONFIG
        try:
            return _parse_stored_window(record), str(record.get("source") or SOURCE_CONFIG)
        except Exception:  # noqa: BLE001 — a bad remote row must not break a status render
            return None, SOURCE_CONFIG
    stored = _stored_quiet_windows().get(machine.name)
    if stored is not None:
        return stored, SOURCE_STORE
    return machine.quiet_hours, SOURCE_CONFIG


def describe_pause_state(
    machine: "Machine",
    paused: set[str],
    *,
    now: datetime | None = None,
    cordons: "Mapping[str, object] | None" = None,
    quiet_hours: "Mapping[str, Mapping] | None" = None,
) -> PauseState | None:
    """Derive *machine*'s pause state purely from *paused* (the already-
    fetched effective set from `paused_set()`) plus the machine's own
    locally-known `quiet_hours` config — no extra network round trip.

    *cordons* is the already-fetched `{machine: Cordon}` map (#2101). Passing
    it is what lets a caller say "cordoned: draining for v0.5.31" instead of
    the flatly wrong "PAUSED": a cordon is not an operator's decision and is
    not cleared by `coord unpause`, so rendering the two identically is the
    "work stopped and nobody said why" failure #2101 trap E names. Omitting
    it degrades to the pre-#2101 rendering.

    *quiet_hours* (#2146) is an already-fetched `effective_quiet_hours()`
    map. Passing it is what lets a THIN client render an operator-set
    window at all: that window lives in the daemon's state file, so a
    client resolving locally would see the machine in `paused` with no
    local explanation and report the flatly wrong "PAUSED". When given, it
    is authoritative (it already carries config-sourced windows too);
    omitting it degrades to "this host's own store, else the machine's
    `coordinator.yml` block", which is exactly right on the daemon host and
    in solo use.

    Returns ``None`` when the machine isn't paused by any mechanism and
    isn't in an overridden quiet window either.
    """
    window, source = _resolve_display_window(machine, quiet_hours)
    covered = window is not None and window.covers(now)
    in_paused = machine.name in paused
    cordon = (cordons or {}).get(machine.name)
    if cordon is not None:
        # Checked FIRST: a cordoned machine is in `paused` (that is how
        # routing honours it), so every branch below would otherwise claim it.
        describe = getattr(cordon, "describe", None)
        return PauseState(kind="cordon", detail=describe() if callable(describe) else str(cordon))
    if in_paused and covered:
        assert window is not None  # covered implies a window
        return PauseState(
            kind="quiet",
            detail=(
                f"until {window.end.strftime('%H:%M')} ({window.tz})"
                f" · {_source_label(source)}"
            ),
        )
    if in_paused:
        return PauseState(kind="hand", detail="")
    if covered:
        return PauseState(kind="quiet_overridden", detail="override active — dispatchable")
    return None
