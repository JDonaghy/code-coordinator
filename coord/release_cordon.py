"""Release cordons: **create** a propagation window instead of waiting for one
(#2101).

`coord release propagate` (#1835/#2067) waits for fleet quiescence. On a fleet
whose drive queue refills every three minutes, that window is a coincidence —
and two facts make the coincidence rare enough to never arrive:

1. the daemon host may not be rolled while it is busy (a caller on a newer
   ``coord`` than its daemon is the documented 405), and nothing may roll
   ahead of the daemon, so **a busy daemon host defers the whole fleet**;
2. every drive-queue entry charges *some* host as busy for the entire life of
   the drive, and the queue relaunches on a 3-minute tick.

Observed on 2026-08-10: the fleet sat eleven releases behind for a day with
elitebook idle and rollable the whole time.

This module is the decision half of the fix. Waiting is replaced by a loop
that manufactures the window:

    detect → **cordon** each behind host → drain (nothing is killed) →
    roll the moment it empties → **uncordon immediately** → repeat.

THE CORDON IS A ROUTING PAUSE WITH AN OWNER
--------------------------------------------
`coord pause` already means exactly "no NEW agents route here; in-flight work
is untouched" (#1563 made it daemon-backed, which is the only reason this is
buildable at all). A cordon reuses that routing semantics — see
:mod:`coord.machine_pause`, which folds active cordons into the one
``paused_set()`` every dispatcher already consults — but it is emphatically
**not** the same flag:

* an operator's ``coord unpause`` must not lift a cordon mid-drain;
* the post-roll uncordon must not clear a pause an operator set deliberately.

So a cordon is stored under its own key with an ``owner``, and each side
clears only its own (:func:`coord.machine_pause.clear_cordon` vs
``local_unpause``). Trap A of #2101.

EVERY CORDON EXPIRES, BECAUSE THE THING THAT SET IT GETS RESTARTED
-------------------------------------------------------------------
The cordon lives in daemon state and the daemon itself is restarted by the
roll it is gating. A propagate run killed between "cordon" and "uncordon"
would otherwise leave every machine refusing work forever — which looks
exactly like a quiet fleet, i.e. #2082 in a new costume. Trap B.

So a :class:`Cordon` carries ``owner``, ``reason``, ``created_at`` and
``expires_at``, and **the read side ignores an expired record** (nothing has
to run for it to lapse — a dead propagate loop cannot fail to clean up,
because cleanup is not an action). The live loop renews on every run while
the host is still behind, so a TTL comfortably longer than the propagate
timer's interval is invisible in normal operation and self-healing after a
crash.

"Every cordon expires" is therefore a guarantee about the CRASHED-loop case
only (trap B). While the loop stays healthy — the normal case — it is not a
bound on how long a cordon can run at all: ``expires_at`` is pushed forward
a full TTL on every renewal, so a host that stays behind for as long as the
loop keeps ticking stays cordoned for just as long. #2136 hit exactly this:
a driven issue in a multi-round `request-changes` fix loop (#1692) never
reached the quiescent window `coord release propagate` waits for before
rolling, so the fleet-wide cordon it had set on the way past renewed every
20 minutes for the life of that loop — hours, not the ~1h TTL. See the
escalation section below for the only mechanism that currently notices.

A HOST THAT NEVER DRAINS IS AN ESCALATION, NEVER A SILENT WAIT
---------------------------------------------------------------
A wedged worker means the cordon never lifts. :func:`plan_cordons` measures
the drain from ``created_at`` (preserved across renewals — a renewal is not
a new cordon) and, past :data:`DEFAULT_DRAIN_DEADLINE_SECONDS`, emits a
:class:`DrainEscalation`. The cordon is still renewed — the host really is
behind, and lifting it would just start work that the next run has to drain
again — but it is now loud, and its message names the override. Trap C.

#2136: loud is *all* it currently does. Nothing here clears the cordon,
bounds the drive loop that is keeping the host busy, or otherwise breaks a
standoff between "the fix loop keeps the host non-quiescent" and "the
cordon can only lift once the host is quiescent" — the fleet stays
launch-blocked until an operator reads the escalation and runs the override
command it names. Whether the deadline should act on its own (clear the
cordon and defer the roll; roll idle hosts individually instead of gating
the whole fleet on one busy one; evaluate quiescence per host rather than
fleet-wide) is a policy call the issue deliberately leaves open rather than
one this module has made unilaterally.

A DEFERRAL IS NOT PASSIVE — IT LEAVES THE CORDON STANDING (#2240)
------------------------------------------------------------------
The escalation above is loud, and loud was enough only while rolls were
manual. On 2026-08-14, with `coord-release-propagate.timer` enabled, the
mechanism closed a loop on itself and took the whole fleet down for 70
minutes with all three machines idle:

1. `propagate` cordons all three hosts to drain them;
2. a cordoned host cannot accept new dispatch — **including a review**;
3. a drive-queue entry that has finished Work and Test is waiting for its
   review to be dispatched, and cannot get one;
4. its queue row therefore stays ``running`` with no live assignment (it is
   between legs), which :func:`coord.release_propagate.busy_host_for_entry`
   cannot attribute to any host;
5. an unattributable busy signal blocks *every* host by design, so the roll
   **defers**;
6. a deferred run leaves the cordon in place, and we are back at (2).

:mod:`coord.release_window` had already predicted the input — "a row that is
``running`` with no live assignment right now (between legs) still reads as
unattributable and blocks every host" — but priced it as a *bounded* cost:
some rolls defer. What that misses is that the deferral is not passive. The
"between legs" window is normally seconds; a standing cordon makes it
permanent, because the cordon is what prevents the next leg from ever being
dispatched. An unattributable row is then not a delay, it is a trap.

The TTL (trap B) is the obvious escape hatch and it cannot fire: the
propagate timer runs every 20 minutes and every run renews, so the renewal
interval is shorter than any sane TTL. The safety net is real and
unreachable.

So a cordon now has a second bound, one that does not depend on anything
else running: :data:`DEFAULT_MAX_DEFERRALS` consecutive *deferred* runs for
the same target version and the cordons are **released outright**
(:class:`DeadlockRelease`), with cordoning held off for
:data:`DEFAULT_RELEASE_COOLDOWN_SECONDS` afterwards so the fleet gets a real
window in which to finish the work the drain is waiting for. The counter and
the cooldown are both read back out of the propagation journal
(:func:`deferral_pressure`), so this survives the process being restarted by
the very roll it gates — the same reason ``expires_at`` is stored rather
than held in memory.

The reasoning, as first written here, was deliberately blunt: "a cordon that
has failed to produce quiescence twice running is not draining anything; it
is blocking the work whose completion it is waiting for." Trading two
cordoned ticks for one uncordoned window costs at worst one deferred release
cycle. Not trading costs the entire fleet, indefinitely, until a human runs
``coord release cordon --clear --all``.

#2741: THAT REASONING'S OWN PREMISE WAS FALSE, AND NOBODY CHECKED
--------------------------------------------------------------------
"It is blocking the work whose completion it is waiting for" was itself an
assumption, and Fix 2 above had already made it false by construction: a
cordon is follow-on-blind (:func:`coord.machine_pause.follow_on_paused_set`)
— a review, fix, or smoke leg for work already in flight dispatches onto a
cordoned host exactly as if it were not cordoned. Nothing here was ever
gating leg dispatch; only :func:`coord.drive_queue.plan_tick`'s NEW-launch
path reads the cordon at all.

Live audit trail, 2026-08-24 (the v0.5.244 roll): two review dispatches
landed on the two cordoned hosts, 8 and 17 minutes before the deadlock
release fired — while :class:`DeadlockRelease`'s own message told the
operator the opposite. The consequence was not cosmetic: the drain was
converging normally (both drives were advancing through their pipelines,
and the fleet reached genuine quiescence within the release-cooldown window
that followed), and the release cut it short anyway, on a diagnosis that did
not match the log. #2490's cooldown then suppressed re-cordoning for 30
minutes on top of that, with no automatic path back to rolling.

Two consecutive deferrals is not, on its own, evidence of anything —
`propagate`'s tick interval is shorter than a normal review/fix/smoke leg
takes to run, so a *healthy*, converging drain reliably produces at least one
or two deferred ticks while it finishes. The distinguishing signal is
**whether the fleet's own busy signal changed within the trailing window
that count is measured over** (:func:`deferral_pressure`'s ``progressed``,
from comparing each deferred run's journalled
:class:`~coord.release_propagate.Quiescence` snapshot against the OTHER
SNAPSHOTS IN THAT SAME TRAILING WINDOW — the newest ``max_deferrals`` of
them, the same span the tick count itself is checked against, not the whole
streak back to the last release): a different assignment, a different queue
key, a different host — anything — means legs are completing and new ones
are being dispatched, i.e. the drain is doing exactly what it is supposed
to. Only a WINDOW whose busy signal is *identical* on every tick inside it
is the condition the blunt reasoning above actually describes, and
:func:`plan_cordons` now requires that in addition to the tick count before
it releases anything. Windowed on purpose, not "changed anywhere in the
whole streak": an unbounded comparison lets one early, genuine hop (review
leg -> fix leg producing a different signature) permanently poison
``progressed`` to ``True`` even after the very next leg wedges forever and
every later tick repeats the same stale signature — exactly the indefinite
hang #2240 exists to end, reintroduced by #2741's own first cut. A stall
that develops after some earlier progress now ages out of the window and
reads as a stall again, rather than being masked by history outside the
window that stopped being relevant. Data too sparse or too old to carry a
``quiescence`` snapshot degrades to the pre-#2741 behaviour (releases on
count alone) — never the reverse, which would fail toward holding the fleet
cordoned longer on missing data, the same wrong direction #2101's read-side
failures already refuse everywhere else in this module.

THE TRIGGER IS COUPLED TO RELEASE FREQUENCY, SO IT IS A KNOB
--------------------------------------------------------------
Cordon-on-any-drift costs one fleet drain per release. Before #2081 landed,
releases cut roughly every 40 minutes — at that cadence this mechanism would
leave the fleet draining more often than working. #2081 reduced the cadence,
so the **default is any drift** (:data:`DEFAULT_DRIFT_THRESHOLD` = 1): a
fleet that is one release behind is a fleet running code nobody is testing,
and #2082 is the cost of tolerating that. The threshold is a knob
(``coord release propagate --cordon-after N``) precisely so a future cadence
change does not need a code change. Trap F.

CORDON-EVERY-BEHIND-HOST DOES NOT COVER A HOST THAT CANNOT ROLL EITHER WAY
----------------------------------------------------------------------------
Observed 2026-08-13 00:18 UTC: one busy signal, on dellserver, and the fleet
cordoned all three machines anyway. elitebook and precision reported
``online • idle`` throughout and stayed cordoned — launch-blocked — for
34+ minutes, unable to accept the very review dispatch #2240 needed.

The cordon-every-behind-host rule above is correct for a host whose OWN
draining is what a run is waiting on: skip it and it never drains, and a run
that defers without cordoning defers again in 20 minutes for the same
reason. It does not cover a host that has no busy signal against it AND is
blocked from rolling by the daemon-leads invariant (see
:mod:`coord.release_propagate`'s LANE ORDER section) rather than by its own
work — nothing may roll ahead of a busy daemon host, so such a host cannot
roll regardless of how drained it gets. Cordoning it protects nothing: its
drained state cannot be consumed until the daemon host rolls first, which
may be unbounded (any drive on the daemon host reproduces this, not just a
wedged one).

:func:`plan_cordons`'s *daemon_host* parameter is the fix: while the daemon
host is itself busy and behind, only it (and any host with its own busy
signal) is cordoned. Everyone else stays uncordoned and dispatchable —
released immediately if a previous run already cordoned them — until the
daemon host becomes rollable, at which point ordinary drain-everyone-behind
resumes. :attr:`CordonPlan.collateral_spared` /
:attr:`CordonPlan.blocked_behind` name exactly who was spared and why, so
the next puzzle like this one is one line of output, not a 40-minute read of
`/board`.

PURITY
------
Nothing here reads the clock, the filesystem, the network or the DB — same
split :mod:`coord.release_propagate` documents, for the same reason. The
clock is passed in; ``coord/commands/release.py`` is the I/O shell.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: Who set a cordon. An operator's ``coord pause`` is NOT an owner of any
#: cordon — it lives under a different key entirely (see the module docstring
#: and :mod:`coord.machine_pause`). This exists so a future second automated
#: owner (a deploy gate, a maintenance window) can share the mechanism
#: without either being able to clear the other's flag.
OWNER_RELEASE = "release"

#: How long a cordon record stays effective without being renewed. The
#: propagate timer runs every 15-20 minutes and renews on every run while the
#: host is still behind, so this is invisible in normal operation; what it
#: bounds is the crash case (trap B): a run killed between cordon and roll
#: leaves the fleet refusing work for at most this long, with no cleanup
#: process required — an expired record is ignored on READ.
DEFAULT_TTL_SECONDS = 3600.0

#: How long a host may fail to drain before the cordon escalates (trap C).
#: Measured from the cordon's ``created_at``, which survives renewal — a
#: renewal is the same cordon, not a new one, so a wedged host cannot reset
#: its own deadline by being cordoned again. Deliberately longer than a
#: normal drive (the thing being drained) and shorter than a night.
DEFAULT_DRAIN_DEADLINE_SECONDS = 5400.0

#: How many releases behind a host must be before it is cordoned. 1 = any
#: drift. See the module docstring's trap-F section for why this is a knob.
DEFAULT_DRIFT_THRESHOLD = 1

#: How many consecutive DEFERRED propagate runs may hold a cordon for the same
#: target version before it is released outright (#2240). 2, at the propagate
#: timer's 20-minute interval, is ~40 minutes of a cordoned fleet — long enough
#: that a normal drain finishes inside it, short enough that the deadlock in
#: the module docstring is a blip rather than a night. Emphatically NOT derived
#: from :data:`DEFAULT_DRAIN_DEADLINE_SECONDS`: that deadline only makes noise,
#: and the whole finding of #2240 is that noise does not break a cycle which
#: sustains itself.
DEFAULT_MAX_DEFERRALS = 2

#: How long cordoning stays OFF after a deadlock release (#2240). Without a
#: cooldown the very next run re-cordons — the hosts are still behind — and
#: the deadlock re-arms 20 minutes later, so the release would buy exactly one
#: tick. Longer than the propagate interval on purpose: the released window has
#: to be long enough for the between-legs entry to actually get its next leg
#: dispatched and run.
DEFAULT_RELEASE_COOLDOWN_SECONDS = 1800.0

#: ``coord.release_propagate.STATUS_DEFERRED``, re-spelled rather than imported
#: to keep this module import-free of the propagation shell (same seam, and the
#: same reason, as ``_SEVERITY_RANK`` there). One string, asserted equal by the
#: tests.
_STATUS_DEFERRED = "deferred"

#: Returned by :func:`version_drift` when a host's version cannot be compared
#: to the target at all (unreadable lane, or a different minor series). A host
#: whose version we cannot read is NEVER cordoned — cordoning stops real work,
#: and doing that on a guess is the failure this fleet keeps repeating.
DRIFT_UNKNOWN = None

#: The drift reported for a host on a different ``major.minor`` series than
#: the target: larger than any sane threshold, because it genuinely is.
CROSS_SERIES_DRIFT = 9999


def normalize_version(raw: str | None) -> str | None:
    """``v0.5.31`` / ``0.5.31`` -> ``0.5.31``; empty -> ``None``."""
    if not raw:
        return None
    return str(raw).strip().lstrip("vV") or None


def _parts(raw: str | None) -> tuple[int, ...] | None:
    version = normalize_version(raw)
    if not version:
        return None
    out: list[int] = []
    for chunk in version.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        out.append(int(digits))
    return tuple(out) if out else None


def version_drift(current: str | None, target: str | None) -> int | None:
    """How many releases *current* is behind *target*.

    ``0`` means level or ahead; ``None`` (:data:`DRIFT_UNKNOWN`) means the two
    cannot be compared — an unreadable version, or a target on a different
    ``major.minor`` series, where "how many releases" has no answer from the
    two strings alone.

    Deliberately arithmetic on the patch component rather than a lookup
    against the index: this runs on every propagate tick, and a decision that
    needs a network call to be made is a decision that stops being made the
    moment the network hiccups. A cross-minor gap reports
    :data:`CROSS_SERIES_DRIFT` — "definitely behind, by more than any
    threshold" — which is the only honest reading of ``0.4.x`` vs ``0.5.y``.
    """
    a, b = _parts(current), _parts(target)
    if a is None or b is None:
        return DRIFT_UNKNOWN
    if a >= b:
        return 0
    if a[:2] != b[:2]:
        return CROSS_SERIES_DRIFT
    patch_a = a[2] if len(a) > 2 else 0
    patch_b = b[2] if len(b) > 2 else 0
    return max(0, patch_b - patch_a)


@dataclass(frozen=True)
class Cordon:
    """One machine's release cordon, as stored and as read back.

    ``created_at`` is the moment the host was FIRST cordoned for this drain
    and is preserved across renewals — the drain deadline (trap C) measures
    from it, so a wedged host cannot postpone its own escalation forever by
    being renewed. ``renewed_at``/``expires_at`` move on every renewal.
    """

    machine: str
    owner: str = OWNER_RELEASE
    reason: str = ""
    target_version: str | None = None
    created_at: float = 0.0
    renewed_at: float = 0.0
    expires_at: float = 0.0

    def active(self, now: float) -> bool:
        """Is this record still in force at *now*?

        An ``expires_at`` of 0 (a hand-written record with no expiry) is
        treated as ACTIVE — a cordon nobody can express an expiry for is
        still a cordon — but every record this module writes has one.
        """
        return not self.expires_at or now < self.expires_at

    def expired(self, now: float) -> bool:
        return not self.active(now)

    def age(self, now: float) -> float:
        return max(0.0, now - self.created_at) if self.created_at else 0.0

    def overdue(self, now: float, deadline: float = DEFAULT_DRAIN_DEADLINE_SECONDS) -> bool:
        """Has this host failed to drain within *deadline* seconds?"""
        return bool(self.created_at) and self.age(now) >= deadline > 0

    def describe(self) -> str:
        """The one sentence every surface shows (#2101 trap E).

        Work stopping with no stated reason is the thing this fleet keeps
        doing to itself, so this is deliberately a whole explanation rather
        than a status word: ``cordoned: draining for v0.5.31``.
        """
        if self.target_version:
            return f"cordoned: draining for v{self.target_version}"
        return self.reason or "cordoned: draining for a release"

    def to_dict(self) -> dict:
        return {
            "machine": self.machine,
            "owner": self.owner,
            "reason": self.reason,
            "target_version": self.target_version,
            "created_at": self.created_at,
            "renewed_at": self.renewed_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Cordon":
        """Type one stored record, tolerantly.

        A malformed field degrades to its default rather than raising: this
        is read on every dispatch decision in the fleet, and a cordon store
        nobody can parse must not be able to wedge routing.
        """

        def _float(key: str) -> float:
            try:
                return float(raw.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        target = raw.get("target_version")
        return cls(
            machine=str(raw.get("machine") or ""),
            owner=str(raw.get("owner") or OWNER_RELEASE),
            reason=str(raw.get("reason") or ""),
            target_version=str(target) if target else None,
            created_at=_float("created_at"),
            renewed_at=_float("renewed_at"),
            expires_at=_float("expires_at"),
        )


@dataclass(frozen=True)
class DrainEscalation:
    """A host that has been cordoned longer than the drain deadline.

    Surfaced, never merely recorded: #2101's acceptance criterion 4 asks for
    the *message*, not an internal state change, because a silent forever-wait
    is the failure this whole mechanism is meant to replace.
    """

    machine: str
    waited_seconds: float
    deadline_seconds: float
    target_version: str | None = None
    #: What is still holding the host, in the same prose
    #: ``coord release propagate`` uses for a deferral.
    busy_reason: str = ""

    @property
    def message(self) -> str:
        minutes = self.waited_seconds / 60.0
        limit = self.deadline_seconds / 60.0
        version = f"v{self.target_version}" if self.target_version else "the release"
        holding = f" — still busy: {self.busy_reason}" if self.busy_reason else ""
        return (
            f"DRAIN OVERDUE: {self.machine} has been cordoned for "
            f"{minutes:.0f}m waiting to drain for {version}, past the "
            f"{limit:.0f}m deadline{holding}. New work is NOT being routed "
            f"there. Override with `coord release cordon --clear "
            f"{self.machine}` (which lets work resume and leaves the host "
            f"behind), or clear whatever is wedged and let it drain."
        )

    @property
    def command(self) -> str:
        return f"coord release cordon --clear {self.machine}"

    def to_dict(self) -> dict:
        return {
            "machine": self.machine,
            "waited_seconds": self.waited_seconds,
            "deadline_seconds": self.deadline_seconds,
            "target_version": self.target_version,
            "busy_reason": self.busy_reason,
            "message": self.message,
        }


@dataclass(frozen=True)
class CordonedIdle:
    """A host that is cordoned, has ZERO active work, and is past the drain
    deadline (#2595).

    :class:`DrainEscalation` above already fires past the same deadline, but
    only as a stderr line inside `coord release propagate`'s own oneshot
    invocation — a `Type=oneshot` timer's stdout goes to the journal and
    nowhere else. #2595's incident (`precision` stranded 22 releases behind
    and cordoned, `dellserver` cordoned 27.9 hours past a 90-minute deadline)
    was found only because an operator happened to read a journal by hand.

    This is the sharper, easier-to-act-on cousin of that condition: a
    cordon whose host has genuinely nothing left to drain (zero active
    assignments) has no reason left to still be cordoned at all — it is not
    "in progress", it is a whole machine quietly pulled from the fleet.
    Built to be read by three different surfaces (`coord status`, `coord
    doctor`, and the `release_cordon_idle` health check) off the SAME pure
    decision (:func:`idle_overdue_cordons`), so the wording and the
    threshold can never drift apart between them.
    """

    machine: str
    waited_seconds: float
    deadline_seconds: float
    target_version: str | None = None
    #: Releases *machine* is behind, when known (``None`` when the caller
    #: could not resolve a version to compare — never rendered as "0 behind",
    #: which would read as "basically current").
    drift: int | None = None

    @property
    def message(self) -> str:
        minutes = self.waited_seconds / 60.0
        limit = self.deadline_seconds / 60.0
        version = f"v{self.target_version}" if self.target_version else "the release"
        if self.drift == CROSS_SERIES_DRIFT:
            # #2595 review: CROSS_SERIES_DRIFT (9999) is a sentinel meaning
            # "different major/minor series, no patch-count comparison is
            # possible" — not an actual release count. Rendering the raw
            # number verbatim (this is the first caller that renders
            # `version_drift()`'s output at all) would print a nonsensical
            # "9999 releases behind" for a host stuck across a minor-version
            # boundary (this fleet's own history has crossed 0.4.x -> 0.5.x).
            behind = ", a major/minor version behind"
        elif self.drift:
            behind = f", {self.drift} release{'s' if self.drift != 1 else ''} behind"
        else:
            behind = ""
        return (
            f"{self.machine} is cordoned and IDLE — {minutes:.0f}m past the "
            f"{limit:.0f}m drain deadline with nothing left to drain "
            f"(draining for {version}{behind}). It is taking NO WORK and "
            "nothing will change that on its own. Fix by hand: "
            f"`coord agent update --machine {self.machine}` then "
            f"`coord release cordon --clear {self.machine}`."
        )

    @property
    def command(self) -> str:
        return f"coord release cordon --clear {self.machine}"

    def to_dict(self) -> dict:
        return {
            "machine": self.machine,
            "waited_seconds": self.waited_seconds,
            "deadline_seconds": self.deadline_seconds,
            "target_version": self.target_version,
            "drift": self.drift,
            "message": self.message,
        }


def idle_overdue_cordons(
    cordons: Mapping[str, Cordon] | Iterable[Cordon],
    *,
    now: float,
    idle_hosts: Iterable[str] = (),
    deadline: float = DEFAULT_DRAIN_DEADLINE_SECONDS,
    host_versions: Mapping[str, str | None] | None = None,
) -> tuple[CordonedIdle, ...]:
    """Every currently-active cordon on an IDLE host, past *deadline*. Pure.

    *cordons* is the store as read (:func:`coord.machine_pause.cordons`'s
    ``{machine: Cordon}``, or any iterable of :class:`Cordon`) — expired
    records are ignored, same as everywhere else in this module. *idle_hosts*
    is the caller's own idea of "has zero active work right now" (a live
    `/status` poll's empty ``active`` list for `coord status`, or
    ``Board.idle_machines()`` for `coord doctor` / the health check) — this
    function does not read a board or an agent itself, so it stays as pure
    and network-free as the rest of this module.

    *host_versions*, when given, is used to compute :attr:`CordonedIdle.drift`
    via :func:`version_drift` against the cordon's own ``target_version`` —
    optional, because not every caller has a fleet-wide version map handy
    (the health check, in particular, only knows about its own host), and a
    missing drift number is far less misleading than a wrong one.

    Sorted by machine name for a stable rendering order.
    """
    idle = set(idle_hosts)
    records = _as_records(cordons)
    out: list[CordonedIdle] = []
    for name, cordon in records.items():
        if not cordon.active(now):
            continue
        if name not in idle:
            continue
        if not cordon.overdue(now, deadline):
            continue
        drift: int | None = None
        if host_versions is not None:
            computed = version_drift(host_versions.get(name), cordon.target_version)
            drift = computed if isinstance(computed, int) else None
        out.append(
            CordonedIdle(
                machine=name,
                waited_seconds=cordon.age(now),
                deadline_seconds=deadline,
                target_version=cordon.target_version,
                drift=drift,
            )
        )
    return tuple(sorted(out, key=lambda c: c.machine))


@dataclass(frozen=True)
class DeferralPressure:
    """How long the cordon has been failing to produce a window (#2240).

    Read back out of the propagation journal by :func:`deferral_pressure`,
    not held in memory: the process that set the cordon is restarted by the
    very roll it gates, so an in-memory counter would reset exactly when the
    deadlock is worst. Same reasoning as ``Cordon.expires_at``.
    """

    #: Consecutive deferred runs for this target that HELD a cordon, counted
    #: back from the newest journal record and stopping at the last release.
    consecutive: int = 0
    #: When the last deadlock release happened (0.0 = never / not in this run
    #: of deferrals). Starts the cooldown in :func:`plan_cordons`.
    last_release_at: float = 0.0
    target_version: str | None = None
    #: The ``--cordon-max-deferrals`` value the most recent matched run
    #: actually used, read back out of that run's journal record. #2240
    #: review: `describe_deferral_pressure()` used to only ever see
    #: `DEFAULT_MAX_DEFERRALS`, so an operator running with a non-default
    #: `--cordon-max-deferrals` got a "NOT DRAINING" label at the wrong
    #: count. Defaults to `DEFAULT_MAX_DEFERRALS` for a record written before
    #: this field existed, or when there is no matched run at all.
    max_deferrals: int = DEFAULT_MAX_DEFERRALS
    #: #2741: did the fleet's own busy signal (each deferred run's journalled
    #: ``quiescence`` snapshot) actually CHANGE somewhere within the
    #: TRAILING WINDOW this pressure reading is checked over — the newest
    #: ``max_deferrals`` cordoned-deferral snapshots, the same span
    #: ``consecutive >= max_deferrals`` is itself counting, NOT the whole
    #: streak back to the last release? True only when at least two runs in
    #: that window carried a readable snapshot and those snapshots were not
    #: all identical — positive evidence that legs are completing and new
    #: ones are being dispatched, i.e. the drain is converging rather than
    #: stuck. Bounded on purpose: comparing across the unbounded streak let
    #: one early, genuine change permanently poison this to ``True`` even
    #: after a later leg wedged forever and every subsequent tick repeated
    #: the same stale signature — the streak resets only on a non-deferral,
    #: a version change, or a release, so an unbounded read could never
    #: recover from that inside one streak. Windowing means a stall that
    #: develops AFTER some earlier progress ages out of the evidence and
    #: reads as a stall again, same as if it had happened alone. Defaults to
    #: ``False`` (no evidence of progress, same as the pre-#2741 read) for a
    #: record too old or too sparse to carry the snapshot at all —
    #: "unreadable degrades toward the existing behaviour, never toward a
    #: stronger claim" is the same rule the rest of this module follows on a
    #: read failure.
    progressed: bool = False

    def cooling_for(self, now: float, cooldown: float) -> float:
        """Seconds of cooldown left at *now*; 0 when cordoning may resume."""
        if not self.last_release_at or cooldown <= 0:
            return 0.0
        return max(0.0, self.last_release_at + cooldown - now)

    def to_dict(self) -> dict:
        return {
            "consecutive": self.consecutive,
            "last_release_at": self.last_release_at,
            "max_deferrals": self.max_deferrals,
            "target_version": self.target_version,
            "progressed": self.progressed,
        }


@dataclass(frozen=True)
class DeadlockRelease:
    """The cordon being dropped because deferring is no longer passive (#2240).

    Distinct from :class:`DrainEscalation` on purpose: an escalation is a
    *message* and changes nothing, which is precisely why the fleet sat
    cordoned for 70 minutes. This one is an ACTION, and the message merely
    describes it.
    """

    hosts: tuple[str, ...] = ()
    consecutive_deferrals: int = 0
    max_deferrals: int = DEFAULT_MAX_DEFERRALS
    cooldown_seconds: float = DEFAULT_RELEASE_COOLDOWN_SECONDS
    target_version: str | None = None

    @property
    def message(self) -> str:
        version = f"v{self.target_version}" if self.target_version else "the release"
        who = ", ".join(self.hosts) if self.hosts else "no host (already clear)"
        minutes = self.cooldown_seconds / 60.0
        # #2741 review: `progressed` only ever looks at the newest
        # `max_deferrals` cordoned-deferral signatures (see
        # `deferral_pressure`), not the whole streak — so when the streak
        # ran longer than that (e.g. an earlier release attempt was itself
        # blocked by evidence that has since aged out), "every one of them"
        # would overclaim. Name the actual window that was checked.
        window_desc = (
            "every one of them"
            if self.consecutive_deferrals <= self.max_deferrals
            else f"the most recent {self.max_deferrals} of them"
        )
        return (
            f"CORDON RELEASED (#2240/#2741): {self.consecutive_deferrals} "
            f"consecutive propagate runs deferred {version} while holding a "
            f"cordon, and the fleet's busy signal held IDENTICAL across "
            f"{window_desc} — no leg completed, no new leg dispatched. That "
            f"is a genuine stall, not a cordon blocking dispatch (a review "
            f"for in-flight work already routes onto a cordoned host "
            f"regardless — only NEW drive launches are gated) and not a "
            f"converging drain that just hasn't hit a quiescent tick yet. "
            f"Uncordoning {who} and "
            f"not cordoning again for {minutes:.0f}m, so whatever is actually "
            f"wedged has a window to be noticed and fixed; the roll will be "
            f"retried after that."
        )

    def to_dict(self) -> dict:
        return {
            "hosts": list(self.hosts),
            "consecutive_deferrals": self.consecutive_deferrals,
            "max_deferrals": self.max_deferrals,
            "cooldown_seconds": self.cooldown_seconds,
            "target_version": self.target_version,
            "message": self.message,
        }


def _busy_signature(record: Mapping[str, Any]) -> frozenset[tuple[str, str, str | None]] | None:
    """A comparable snapshot of one journal record's busy signal (#2741).

    *record* is one whole propagation-journal entry — the ``quiescence`` key
    is :meth:`coord.release_propagate.Quiescence.to_dict`, written by the
    shell on every run regardless of outcome. Reduced to
    ``{(kind, subject, host), ...}`` — ``detail`` is deliberately left out:
    it is free-form prose (e.g. "between legs — attributed to its last known
    host"), and two runs describing the SAME still-running item in slightly
    different words must not read as "different work", which is the false
    positive the opposite mistake (comparing full dicts) would make.

    ``None`` — not an empty set — when the snapshot cannot be read at all
    (missing, malformed, or a ``busy`` list that is not a list): an empty
    fleet really did have a quiescent moment recorded, which is itself
    meaningful (arguably a converging drain), and must not be confused with
    "we have no idea what this run saw".

    #2741 review: can a record this function is actually called against (a
    ``DEFERRED`` record that HELD a cordon — see the caller in
    :func:`deferral_pressure`) ever carry a *readable but empty* ``busy``
    list, i.e. ``frozenset()``? No, by construction of the one place that
    writes ``STATUS_DEFERRED``: every branch that finishes with
    ``STATUS_DEFERRED`` does so because ``fully_busy`` or a non-empty
    ``still_busy``/``busy_hosts`` held, and all three are derived from
    ``quiescence.busy`` being non-empty (see
    ``coord.commands.release.release_propagate``, the one caller of
    ``_finish(rp.STATUS_DEFERRED, ...)``). So a well-formed deferred+cordoned
    record cannot carry an empty-but-readable snapshot today; if that
    invariant is ever broken elsewhere, the frozenset() it produces would
    still only ever read as "changed" relative to a DIFFERENT non-empty
    snapshot, which the trailing-window bound in :func:`deferral_pressure`
    already limits the blast radius of (see that function's docstring).
    """
    quiescence = record.get("quiescence")
    if not isinstance(quiescence, Mapping):
        return None
    busy = quiescence.get("busy")
    if not isinstance(busy, list):
        return None
    out: set[tuple[str, str, str | None]] = set()
    for item in busy:
        if not isinstance(item, Mapping):
            continue
        host = item.get("host")
        out.add((
            str(item.get("kind") or ""),
            str(item.get("subject") or ""),
            str(host) if host else None,
        ))
    return frozenset(out)


def deferral_pressure(
    records: Iterable[Mapping[str, Any]],
    *,
    target_version: str | None = None,
) -> DeferralPressure:
    """How many consecutive deferrals have held a cordon (#2240). Pure.

    *records* are propagation-journal objects oldest-first, exactly as
    :func:`coord.release_propagate.read_records` returns them. Walked
    newest-first and stopped at the first record that is:

    * not a deferral — a roll, a rollback or an "already up to date" means
      the mechanism is working and the count is meaningless;
    * for a different target version — a new release restarts the clock;
    * the last deadlock release — everything before it has already been paid
      for, so the count restarts from there and the cooldown takes over.

    A deferral that cordoned NOTHING does not increment: the count is
    specifically about a STANDING cordon that is producing no observable
    change (see ``progressed`` below), and a run that held no cordon at all
    has nothing standing to be stalled. It does not break the walk either —
    a transient cordon-store write error in the middle of a standoff must not
    silently reset the counter.

    #2240 review: also reads back the ``--cordon-max-deferrals`` value the
    *newest* matched run actually used (from that run's own journal record,
    written by :func:`coord.commands.release._apply_cordons`), rather than
    leaving every caller of :func:`describe_deferral_pressure` to assume
    :data:`DEFAULT_MAX_DEFERRALS` — an operator running propagate with a
    non-default ``--cordon-max-deferrals`` was otherwise shown "NOT DRAINING"
    at the wrong count.

    #2741: also compares each cordoned deferral's journalled busy signal
    (:func:`_busy_signature`) against the others in the streak and sets
    :attr:`DeferralPressure.progressed` when they are not all identical — a
    deferral count alone cannot distinguish a genuine deadlock from a drain
    that is converging normally (legs completing, new ones dispatching) but
    has simply not yet hit a fully-quiescent tick. See the module docstring's
    "#2741" section for the incident that showed the count-alone read firing
    while two reviews were actively dispatching onto the cordoned hosts.

    Review of the first #2741 cut: comparing signatures across the WHOLE
    streak lets one early, genuine change permanently poison ``progressed``
    to ``True`` — a drive can advance once (review leg -> fix leg, a
    different signature) and then wedge forever on the very next leg (a
    worker that crashes without ever updating its assignment row), and every
    tick after that keeps re-affirming the same stale signature. Because the
    comparison looked at "changed anywhere, ever", that one earlier hop was
    enough to block the deadlock-release for the rest of the streak — no
    matter how many ticks the fleet had actually been stuck since. That is
    the exact failure #2240 exists to end, defeated by #2741's own
    safeguard. So the comparison is now bounded to the same trailing window
    :func:`plan_cordons` actually acts on: only the newest ``max_deferrals``
    cordoned-deferral signatures (the ones ``consecutive >= max_deferrals``
    is itself counting) are considered. A stall that develops AFTER some
    earlier progress ages out of the window exactly when that progress
    stops being recent enough to matter, so it reads as a stall again —
    while a streak still shorter than the window behaves exactly as before
    (the window is the whole streak so far).
    """
    want = normalize_version(target_version)
    consecutive = 0
    last_release_at = 0.0
    max_deferrals: int | None = None
    signatures: list[frozenset[tuple[str, str, str | None]]] = []
    for raw in reversed(list(records)):
        if not isinstance(raw, Mapping):
            break
        if str(raw.get("status") or "") != _STATUS_DEFERRED:
            break
        if want is not None and normalize_version(raw.get("target_version")) != want:
            break
        cordons = raw.get("cordons")
        cordons = cordons if isinstance(cordons, Mapping) else {}
        if max_deferrals is None:
            # Only the newest matched record's value counts — a record
            # written before this field existed (or holding a non-numeric
            # value) falls back to the default.
            raw_max_deferrals = cordons.get("max_deferrals")
            try:
                max_deferrals = (
                    DEFAULT_MAX_DEFERRALS if raw_max_deferrals is None
                    else int(raw_max_deferrals)
                )
            except (TypeError, ValueError):
                max_deferrals = DEFAULT_MAX_DEFERRALS
        try:
            released_at = float(cordons.get("released_at") or 0.0)
        except (TypeError, ValueError):
            released_at = 0.0
        if released_at:
            last_release_at = released_at
            break
        if cordons.get("cordoned"):
            consecutive += 1
            signature = _busy_signature(raw)
            if signature is not None:
                signatures.append(signature)
    effective_max_deferrals = (
        DEFAULT_MAX_DEFERRALS if max_deferrals is None else max_deferrals
    )
    # `signatures` was built newest-first (the walk itself is newest-first),
    # so the first `effective_max_deferrals` entries ARE the trailing window
    # — the same one `consecutive >= max_deferrals` counts against. Bounded
    # on purpose (see the docstring's "#2741 review" section): a signature
    # that changed once, long enough ago to have aged out of this window,
    # must not keep reading as "progress" forever. `effective_max_deferrals
    # <= 0` (an operator running `--cordon-max-deferrals 0`, #2101's
    # original re-arm-immediately knob) slices to an empty window, which
    # safely defaults `progressed` to `False` below — harmless either way,
    # since `plan_cordons`'s own `max_deferrals > 0` guard already keeps the
    # release path unreachable whenever that knob is 0.
    window = signatures[:max(effective_max_deferrals, 0)]
    # Positive evidence only: at least two readable snapshots in the window,
    # and they were not all the same. Fewer than two, or none readable at
    # all, leaves `progressed` at its safe default (False) — see the field's
    # own docstring for why that direction, not the reverse, is safe.
    progressed = len(window) >= 2 and len(set(window)) > 1
    return DeferralPressure(
        consecutive=consecutive,
        last_release_at=last_release_at,
        target_version=want,
        max_deferrals=(
            DEFAULT_MAX_DEFERRALS if max_deferrals is None else max_deferrals
        ),
        progressed=progressed,
    )


def describe_deferral_pressure(
    pressure: DeferralPressure,
    *,
    max_deferrals: int | None = None,
) -> str:
    """The short suffix every cordon surface appends (#2240 acceptance 4).

    Empty string while the drain is normal. `coord status` renders
    "CORDONED: DRAINING FOR V0.5.77" either way, and #2240's whole
    observability finding is that those two states read identically: one is
    a fleet upgrading itself, the other is a fleet that has been unable to
    work for an hour.

    *max_deferrals* defaults to ``pressure.max_deferrals`` — the value the
    newest matched propagate run actually used, read back out of the journal
    by :func:`deferral_pressure` — rather than :data:`DEFAULT_MAX_DEFERRALS`.
    Review finding on #2240: both of this function's callers
    (`coord status` and `coord release cordon`) used to pass nothing and
    silently get the hardcoded default, so an operator running propagate
    with a non-default ``--cordon-max-deferrals`` saw "NOT DRAINING" at the
    wrong count. Pass an explicit value only when *pressure* predates this
    field (e.g. a hand-built `DeferralPressure` in a test).
    """
    if pressure.consecutive <= 0:
        return ""
    effective = pressure.max_deferrals if max_deferrals is None else max_deferrals
    plural = "" if pressure.consecutive == 1 else "s"
    stalled = " — NOT DRAINING" if pressure.consecutive >= max(1, effective) else ""
    return f"deferred {pressure.consecutive} run{plural}{stalled}"


@dataclass(frozen=True)
class CordonPlan:
    """What one propagate run wants the cordon store to look like.

    Applied by the shell; nothing here writes. ``cordon`` holds both brand-new
    cordons and renewals of existing ones (they are the same write — see
    :class:`Cordon` for why ``created_at`` survives).
    """

    cordon: tuple[Cordon, ...] = ()
    uncordon: tuple[str, ...] = ()
    escalations: tuple[DrainEscalation, ...] = ()
    #: Records that lapsed on their own since the last run (trap B working).
    #: Reported so a self-healed cordon leaves a trace rather than silently
    #: evaporating from the reasoning.
    expired: tuple[str, ...] = ()
    #: Cordoned hosts this run could neither prove current nor prove behind
    #: (unreadable version, or drift under the threshold). Left exactly as
    #: they are — see :func:`plan_cordons` for why neither direction is safe.
    unknown: tuple[str, ...] = ()
    #: #2240: the deadlock break. Set when this run is dropping the cordon
    #: because deferring has stopped being passive. Its ``hosts`` are cleared
    #: IN ADDITION to ``uncordon`` (which stays what it always was: hosts
    #: proven to be on the target already).
    released: "DeadlockRelease | None" = None
    #: #2240: seconds of post-release cooldown still to run. Non-zero means
    #: this run deliberately cordoned nothing even though hosts are behind —
    #: recorded rather than silent, because "no cordons planned" and "cordons
    #: suppressed on purpose" are the same output otherwise.
    cooling_seconds: float = 0.0
    #: #2176: behind hosts this run deliberately did NOT cordon (and, if
    #: already cordoned, released) because they have no busy signal of their
    #: own and cannot roll ahead of `blocked_behind` anyway. Reported
    #: separately from `uncordon` (which is "proven current") so the journal
    #: and CLI output can name them as collateral rather than as a drain that
    #: completed.
    collateral_spared: tuple[str, ...] = ()
    #: #2176: the daemon host these hosts are spared on account of — set
    #: exactly when `collateral_spared` is non-empty. The single fact that
    #: would have turned a 40-minute puzzle into one line of output.
    blocked_behind: str | None = None
    #: #2490: hosts that are BEHIND *target_version*, have no busy signal of
    #: their own this run (idle), and are not being cordoned only because an
    #: unexpired deadlock-release cooldown is suppressing all new cordons —
    #: set exactly when ``cooling_seconds > 0``. This is the gap the issue
    #: names: a host in this state has no automatic path back to rolling for
    #: up to the cooldown's full length, and nothing surfaced that it was
    #: sitting there idle, behind, and unflagged. Reported separately from
    #: ``collateral_spared`` (blocked behind a busy daemon host — a different
    #: reason a behind host stays uncordoned) so each surface can name the
    #: actual cause instead of collapsing both into "not cordoned".
    stuck_in_cooldown: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.cordon
            or self.uncordon
            or self.escalations
            or self.expired
            or self.released
            or self.collateral_spared
            or self.stuck_in_cooldown
        )

    def to_dict(self) -> dict:
        return {
            "cordon": [c.to_dict() for c in self.cordon],
            "uncordon": list(self.uncordon),
            "escalations": [e.to_dict() for e in self.escalations],
            "expired": list(self.expired),
            "unknown": list(self.unknown),
            "released": self.released.to_dict() if self.released else None,
            "cooling_seconds": self.cooling_seconds,
            "collateral_spared": list(self.collateral_spared),
            "blocked_behind": self.blocked_behind,
            "stuck_in_cooldown": list(self.stuck_in_cooldown),
        }

    def render(self) -> list[str]:
        """Human lines for `coord release propagate`'s output."""
        lines: list[str] = []
        for item in self.cordon:
            lines.append(f"  ⊘ cordon {item.machine}: {item.describe()}")
        if self.collateral_spared:
            lines.append(
                "  · "
                + ", ".join(sorted(self.collateral_spared))
                + f": left uncordoned — blocked behind daemon host "
                f"{self.blocked_behind} either way, cordoning would not help "
                "(#2176)"
            )
        for name in self.uncordon:
            lines.append(f"  ✓ uncordon {name}: up to date, work may resume")
        for name in self.expired:
            lines.append(
                f"  · cordon on {name} expired on its own (no propagate run "
                "renewed it) — work may resume there"
            )
        for name in self.unknown:
            lines.append(
                f"  ? {name}: cordoned, but this run could neither prove it "
                "current nor prove it behind — left as-is, and it will lapse "
                "on its own if no later run renews it"
            )
        if self.released is not None:
            lines.append(f"  ! {self.released.message}")
        if self.cooling_seconds > 0:
            lines.append(
                "  · cordons held off for another "
                f"{self.cooling_seconds / 60.0:.0f}m — a recent deadlock "
                "release (#2240) is still letting the fleet work; nothing was "
                "cordoned this run"
            )
        for host in self.stuck_in_cooldown:
            lines.append(
                f"  ! STUCK: {host} is behind and idle, but cordoning is "
                "suppressed by the deadlock-release cooldown above (#2490) — "
                f"it has no automatic path back to rolling until the cooldown "
                f"lifts. Consider `coord agent update --machine {host}`."
            )
        for esc in self.escalations:
            lines.append(f"  ! {esc.message}")
        return lines


@dataclass(frozen=True)
class HostDrift:
    """Which hosts are behind, current, or neither — and why.

    Four buckets, not two, because the two "neither" cases must never be
    collapsed into "current": a host whose version could not be read is not
    evidence of agreement (#1834), and a host one release behind a threshold
    of three is deliberately tolerated rather than proven level.
    """

    behind: frozenset[str] = frozenset()
    current: frozenset[str] = frozenset()
    #: Version unreadable — never cordoned (a cordon on a guess stops real
    #: work) and never uncordoned (an HTTP blip must not open the fleet up
    #: mid-roll).
    unreadable: frozenset[str] = frozenset()
    #: Behind, but by less than the threshold (trap F).
    under_threshold: frozenset[str] = frozenset()

    @property
    def undecided(self) -> frozenset[str]:
        return self.unreadable | self.under_threshold


def classify_hosts(
    host_versions: Mapping[str, str | None],
    target: str | None,
    *,
    threshold: int = DEFAULT_DRIFT_THRESHOLD,
) -> HostDrift:
    """Bucket *host_versions* against *target*. See :class:`HostDrift`.

    *host_versions* maps a machine name to the version its python lane
    reports, ``None`` when no lane could be read. "Current" requires proof:
    an unreadable version is ``unreadable``, never ``current`` — the same rule
    :func:`coord.release_propagate.hosts_already_current` applies, and for
    the same reason (#1834: ``version=None`` means "no data", which is
    emphatically not "agrees with everyone else").
    """
    want = max(1, int(threshold))
    behind: set[str] = set()
    current: set[str] = set()
    unreadable: set[str] = set()
    under: set[str] = set()
    for host, version in host_versions.items():
        drift = version_drift(version, target)
        if drift is DRIFT_UNKNOWN:
            unreadable.add(host)
        elif drift == 0:
            current.add(host)
        elif drift >= want:
            behind.add(host)
        else:
            under.add(host)
    return HostDrift(
        behind=frozenset(behind),
        current=frozenset(current),
        unreadable=frozenset(unreadable),
        under_threshold=frozenset(under),
    )


def plan_cordons(
    *,
    target_version: str | None,
    host_versions: Mapping[str, str | None],
    existing: Mapping[str, Cordon] | Iterable[Cordon] = (),
    now: float,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    drain_deadline: float = DEFAULT_DRAIN_DEADLINE_SECONDS,
    threshold: int = DEFAULT_DRIFT_THRESHOLD,
    busy_reasons: Mapping[str, str] | None = None,
    enabled: bool = True,
    pressure: DeferralPressure | None = None,
    max_deferrals: int = DEFAULT_MAX_DEFERRALS,
    release_cooldown: float = DEFAULT_RELEASE_COOLDOWN_SECONDS,
    daemon_host: str | None = None,
) -> CordonPlan:
    """Decide this run's cordon writes. Pure.

    *existing* is the store as read (expired records included — this function
    is what notices they lapsed). *busy_reasons* maps a host to why it is not
    yet drained, purely so an escalation can say what is holding it — and
    (#2176) so this function can tell a genuinely busy host from an idle one
    when deciding who is collateral.

    ``enabled=False`` (``coord release propagate --no-cordon``) plans no new
    cordons but STILL clears the ones this owner already set: turning the
    mechanism off must release the fleet, not freeze it in whatever state the
    last run left behind.

    *pressure* (#2240) is :func:`deferral_pressure` over the propagation
    journal. Two things come out of it, in this order:

    * an unexpired **cooldown** from a previous release suppresses all new
      cordons — without it the next run re-cordons (the hosts really are
      still behind) and the deadlock re-arms 20 minutes later;
    * ``consecutive >= max_deferrals`` AND NOT ``progressed`` **releases**
      every live cordon (:class:`DeadlockRelease`) and starts that cooldown.
      #2741: the count alone used to be sufficient, and that fired the
      release while two reviews were actively dispatching onto the cordoned
      hosts — a converging drain, not a deadlock. ``progressed`` (see
      :func:`deferral_pressure`) is positive evidence the fleet's busy signal
      changed somewhere in the streak; a streak that never released still
      must also show that signal was IDENTICAL on every tick before this
      concludes nothing is moving.

    Proven-current hosts are uncordoned in every one of these branches: that
    is never the wrong move, and skipping it during a cooldown would leave a
    rolled host cordoned for the length of the cooldown.

    *daemon_host* (#2176) is the machine `coord release propagate` will roll
    first (see `coord.release_propagate`'s LANE ORDER section) — the
    daemon-leads invariant means no OTHER host's python lane may roll ahead
    of it while it is itself busy and behind. A behind host with no busy
    signal of its own is therefore collateral, not protected, by a cordon
    while that holds: it cannot roll either way, so draining it buys nothing
    and just spends fleet capacity for a wait that is unbounded from its own
    perspective (#2067 made quiescence per-host; this closes the cordon's
    matching gap). Such a host is left uncordoned — and released immediately
    if a previous run already cordoned it — until the daemon host is
    rollable (quiescent, or already on the target), at which point ordinary
    drain-everyone-behind resumes exactly as before #2176. A host that is
    itself busy is cordoned regardless: its own drain is still the thing
    being waited on, whatever the daemon host is doing.
    """
    records = _as_records(existing)
    live = {name: c for name, c in records.items() if c.active(now)}
    expired = tuple(sorted(name for name in records if name not in live))

    drift = classify_hosts(host_versions, target_version, threshold=threshold)

    if not enabled:
        return CordonPlan(uncordon=tuple(sorted(live)), expired=expired)

    # ── #2176: who is collateral to a busy, behind daemon host ────────────
    daemon_blocked = bool(
        daemon_host
        and (busy_reasons or {}).get(daemon_host)
        and daemon_host not in drift.current
    )
    collateral = frozenset(
        host
        for host in drift.behind
        if daemon_blocked and host != daemon_host and not (busy_reasons or {}).get(host)
    )
    blocked_behind = daemon_host if collateral else None

    # Uncordon: PROVEN current, plus (#2176) any collateral host a previous
    # run already cordoned — "spared" means dispatchable now, not "merely not
    # renewed". A host whose version could not be read otherwise keeps
    # whatever cordon it has until that cordon EXPIRES — clearing on "we
    # couldn't read the version" would open the fleet up mid-roll on the
    # strength of one failed HTTP call.
    to_uncordon = tuple(
        sorted(
            {name for name in live if name in drift.current} | (collateral & set(live))
        )
    )

    # ── #2240: the deadlock bound ────────────────────────────────────────
    pressure = pressure or DeferralPressure()
    cooling = pressure.cooling_for(now, release_cooldown)
    if cooling > 0:
        # #2490: a behind host with no busy signal of its own has nothing
        # left holding it back except this cooldown — it is idle, it is
        # behind, and it will sit exactly like that until the cooldown
        # lifts (up to `release_cooldown` seconds) with no cordon and,
        # before this, no signal anywhere that it needs attention.
        # `collateral` hosts are excluded: those are uncordoned for an
        # unrelated reason (blocked behind a busy daemon host) that already
        # has its own name (`collateral_spared`/`blocked_behind`).
        stuck = tuple(
            sorted(
                host
                for host in (drift.behind - collateral)
                if not (busy_reasons or {}).get(host)
            )
        )
        return CordonPlan(
            uncordon=to_uncordon,
            expired=expired,
            cooling_seconds=cooling,
            unknown=tuple(sorted(drift.undecided & set(live))),
            collateral_spared=tuple(sorted(collateral)),
            blocked_behind=blocked_behind,
            stuck_in_cooldown=stuck,
        )
    if (
        max_deferrals > 0
        and pressure.consecutive >= max_deferrals
        and not pressure.progressed
    ):
        return CordonPlan(
            uncordon=to_uncordon,
            expired=expired,
            released=DeadlockRelease(
                hosts=tuple(sorted(set(live) - set(to_uncordon))),
                consecutive_deferrals=pressure.consecutive,
                max_deferrals=max_deferrals,
                cooldown_seconds=release_cooldown,
                target_version=target_version,
            ),
            collateral_spared=tuple(sorted(collateral)),
            blocked_behind=blocked_behind,
        )

    to_cordon: list[Cordon] = []
    escalations: list[DrainEscalation] = []
    for host in sorted(drift.behind - collateral):
        previous = live.get(host)
        created = previous.created_at if previous and previous.created_at else now
        to_cordon.append(
            Cordon(
                machine=host,
                owner=OWNER_RELEASE,
                reason=f"draining for v{target_version}" if target_version else "draining for a release",
                target_version=target_version,
                created_at=created,
                renewed_at=now,
                expires_at=now + max(0.0, float(ttl_seconds)),
            )
        )
        if previous is not None and previous.overdue(now, drain_deadline):
            escalations.append(
                DrainEscalation(
                    machine=host,
                    waited_seconds=previous.age(now),
                    deadline_seconds=drain_deadline,
                    target_version=target_version,
                    busy_reason=(busy_reasons or {}).get(host, ""),
                )
            )

    return CordonPlan(
        cordon=tuple(to_cordon),
        uncordon=to_uncordon,
        escalations=tuple(escalations),
        expired=expired,
        unknown=tuple(sorted(drift.undecided & set(live))),
        collateral_spared=tuple(sorted(collateral)),
        blocked_behind=blocked_behind,
    )


def _as_records(
    existing: Mapping[str, Cordon] | Iterable[Cordon],
) -> dict[str, Cordon]:
    if isinstance(existing, Mapping):
        return {str(k): v for k, v in existing.items()}
    return {c.machine: c for c in existing}


@dataclass
class CordonOutcome:
    """What the shell actually did, for the propagation journal."""

    cordoned: list[str] = field(default_factory=list)
    uncordoned: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    escalated: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: #2240: when this run broke the cordon deadlock. THE field the next
    #: run's :func:`deferral_pressure` looks for — it is both the counter
    #: reset and the cooldown's start, so a run that releases and fails to
    #: journal it would release again on the very next tick.
    released_at: float = 0.0
    #: :meth:`DeadlockRelease.to_dict`, when there was one.
    released: dict | None = None
    #: Seconds of cooldown left when this run decided not to cordon (#2240).
    cooling_seconds: float = 0.0
    #: What :func:`deferral_pressure` read, so the journal shows the input to
    #: the release decision and not just its output.
    pressure: dict = field(default_factory=dict)
    #: The ``--cordon-max-deferrals`` this run actually resolved to (the flag
    #: value, or :data:`DEFAULT_MAX_DEFERRALS` when unset). #2240 review:
    #: recorded on EVERY run, not just a releasing one, so
    #: :func:`deferral_pressure` can read the operator's real setting back
    #: out of the newest record instead of every caller of
    #: :func:`describe_deferral_pressure` assuming the default.
    max_deferrals: int = DEFAULT_MAX_DEFERRALS
    #: #2176: behind hosts this run spared (or actively released) because
    #: they have no busy signal of their own and cannot roll ahead of a busy,
    #: behind daemon host anyway. Mirrors `CordonPlan.collateral_spared`.
    collateral_spared: list[str] = field(default_factory=list)
    #: #2176: the daemon host `collateral_spared` was spared on account of.
    #: Mirrors `CordonPlan.blocked_behind`.
    blocked_behind: str | None = None
    #: #2490: mirrors `CordonPlan.stuck_in_cooldown` — behind, idle hosts this
    #: run left uncordoned only because the #2240 deadlock cooldown is still
    #: active. Journalled (not just printed) so a read-only surface like
    #: `coord status` can flag them without recomputing `plan_cordons` itself
    #: — see `coord.commands.status._stuck_in_cooldown_hosts`.
    stuck_in_cooldown: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cordoned": list(self.cordoned),
            "uncordoned": list(self.uncordoned),
            "expired": list(self.expired),
            "escalated": [dict(e) for e in self.escalated],
            "errors": list(self.errors),
            "released_at": self.released_at,
            "released": dict(self.released) if self.released else None,
            "cooling_seconds": self.cooling_seconds,
            "pressure": dict(self.pressure),
            "max_deferrals": self.max_deferrals,
            "collateral_spared": list(self.collateral_spared),
            "blocked_behind": self.blocked_behind,
            "stuck_in_cooldown": list(self.stuck_in_cooldown),
        }
