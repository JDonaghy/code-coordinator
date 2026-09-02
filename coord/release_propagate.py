"""Decision half of merge-triggered propagation (#1835, PKG-7).

PKG-7 closes the release loop: **merging a PR to `main` is the only human
action in a release.** The pipeline it creates is deliberately cut in two,
and the cut is the whole design:

* **Publish** — fully automatic, on merge. `.github/workflows/auto-release.yml`
  picks the next `vX.Y.Z` from the tag history and pushes it; `publish.yml`
  (#1242, PKG-6) turns that one tag into one Release carrying the wheel, the
  `coord-tui` binaries and the bundled webapp. Publishing touches no running
  host, so it is safe at any instant.

* **Propagate** — automatic, but scheduled against *fleet quiescence*, never
  against the clock. This module is that scheduler's judgement.

Why the cut is not optional: ``coord agent update`` restarts the agent, and
a restart kills every in-flight headless worker (``coord/agent_app.py``'s
``/update`` refuses outright when assignments are live, for exactly this
reason). With overnight drive queues (#56/#1750) the fleet is rarely idle,
so "on merge, upgrade the fleet" would routinely destroy work — and the
better the queue works, the more it destroys.

QUIESCENCE IS THE DRIVE QUEUE'S, NOT A SECOND OPINION
-----------------------------------------------------
#1835 is explicit that propagation must reuse the drive queue's existing
gate mechanism rather than invent a rival definition of "the fleet is busy"
— two competing definitions of quiescence is the same class of defect as two
overseers driving one milestone (#1440). So :func:`assess_quiescence` reads
exactly the states :mod:`coord.drive_queue` already publishes, and imports
its constants rather than re-spelling them:

* a queue entry in :data:`~coord.drive_queue.STATE_RUNNING` is in-flight work
  → **busy**;
* an agent with live (``RUNNING``/``PENDING``) assignments is in-flight work
  → **busy** (the board is authoritative here for the same reason
  ``coord.drive_queue`` rule 1 gives: a drive whose *observer* gave up leaves
  the worker running and invisible to session counts);
* a **fired** deploy gate (:data:`~coord.drive_queue.HOLD_FIRED`) is
  **not** busy — it is the opposite. `--hold-after` means "this entry landed
  a change that crosses a deploy lane; stop launching until a human deploys
  and releases it" (#1757). The queue has *deliberately stopped*. That is the
  best propagation window there is, and propagation is precisely the deploy
  the gate is waiting for. So a fired hold is an *invitation*, and a verified
  propagation releases it (see :func:`holds_to_release`) — the gate stops the
  queue for the deploy, propagation performs the deploy, propagation restarts
  the queue. One mechanism, one loop, no second notion of quiescence.

QUIESCENCE IS PER-HOST, NOT FLEET-WIDE ALL-OR-NOTHING
------------------------------------------------------
#2067: every :class:`Busy` signal already carries the host it belongs to —
a drive-queue entry names the host that launched it, a live assignment
names the machine running it. Collapsing that to one fleet-wide boolean
(``not busy``) means a single continuously-running drive queue, on any one
host, defers propagation *forever*: the queue refills from its backlog
every few minutes, so at least one entry is running essentially always, and
"quiescent" never arrives. That is not a rare edge case, it is the steady
state of a working overnight queue — the busier and more useful the queue
is, the less this fleet-wide reading ever fires.

:attr:`Quiescence.quiescent`/``reason`` remain the fleet-wide summary (still
the right answer for `--force`, and for the one case that genuinely must
stay all-or-nothing — see below). A caller that wants to roll a
partially-busy fleet uses :meth:`Quiescence.rollable_hosts` instead: a host
with no busy signal against it is free to roll *now*, independent of
whatever else is running elsewhere. A signal that cannot be pinned to one
host (the board itself unreadable, or a running drive-queue row for which
NOTHING names a machine — no ``--machine`` pin, no live assignment, and no
assignment at all inside the board's retention window; see
:func:`busy_host_for_entry`) is the one thing that still has to block
everything — see :attr:`Quiescence.fleet_wide_busy` — because there is no
way to tell "busy everywhere" apart from "busy on some host we can't name".

#2240 narrowed that set once more. A row that is ``running`` between legs
— previous assignment closed out, next one not dispatched yet — used to be
unattributable and therefore fleet-blocking, and *that* is what deadlocked
against a release cordon for 70 minutes: the cordon blocked the very review
dispatch that would have ended the between-legs window. Such a row is now
charged to the host that ran its last assignment.

Per-host quiescence does not repeal the daemon-leads invariant below: if
the daemon host itself is occupied (and not already on the target), no
other host's python lane may roll ahead of it either, because that would
put a caller on a newer `coord` than the daemon it talks to — the
documented 405. The shell (`coord/commands/release.py`) enforces this by
deferring the whole run only in that one case; every other combination of
busy hosts rolls whatever it can and defers only what it can't.

#2618 INVESTIGATED THE *REASON* THIS ONE CASE IS "OCCUPIED == DEFER" AT ALL
----------------------------------------------------------------------------
Rolling the daemon host's python lane restarts `coord-serve` — a fleet-wide
event, not a local one — and "the fleet must be quiescent for it" was
inferred (never confirmed against the retry code) from two daemon-dependent
things a drive does: poll the board, and write to it. #2618 checked both.

The POLL path was already fine: `coord.drive.Driver.read_state()` swallows
every exception `BoardFetcher.fetch()` can raise, including a connection
refusal, and returns `None` — `Driver._loop` just sleeps `--poll` and tries
again next cycle. A `coord-serve` restart (systemd, single-digit seconds)
landing inside one poll interval was already invisible there, restart or
not.

The WRITE path was not: every RUN action (`coord assign`/`retry`/`fix`/
`test`/`merge --only`/...) is a `coord` subprocess making exactly one
unretried `httpx` call to the daemon. Land one in the restart's
connection-refused window and the whole drive died — a real in-flight
drive genuinely could not survive the restart, which is why "is anything
running anywhere" was the only safe question to ask before rolling the
daemon host. `coord.drive.Driver._spawn` now retries that one shape (a
clean connection refusal — the daemon never received the request, so
retrying is unambiguous; see its own comment for why a reset or timeout
mid-request is deliberately NOT retried the same way) across a bounded,
few-attempt window sized to ride out an ordinary restart.

That means an in-flight `coord drive` is no longer inherently unable to
survive a `coord-serve` restart — which is the premise "occupied anywhere
defers the daemon host" was built on. This module's `assess_quiescence`
and `Quiescence.busy_hosts()`/`fleet_wide_busy` are unchanged by #2618 on
purpose: the daemon-host gate itself (`daemon_name in busy_hosts` in
`coord/commands/release.py`, plus the cordon "blocked behind daemon host"
message in `coord/release_cordon.py`) lives outside this module and needs
its own look before any loosening — this is the finding, not the fix.

#2854 IS THE LOOK #2618 DEFERRED
---------------------------------
#2618's finding was narrow on purpose: a live drive can now survive a
`coord-serve` restart, so "occupied anywhere" no longer needs to defer the
whole fleet — but `assess_quiescence` kept charging a between-legs `running`
row to its host for the entry's entire remaining lifetime regardless,
because #2240 only ever narrowed WHICH host that charge lands on, never
whether it should still apply once the thing it was protecting (an
in-flight worker process) is gone. Measured 2026-08-27: a propagate run
cordoned two idle hosts because a third issue's *work* leg had started three
minutes earlier, with smoke, review and merge still ahead of it — on that
day's numbers, an hour or more of cordon for hosts with nothing running on
them at all.

`assess_quiescence`'s `now`/`settle_seconds` close that gap: a between-legs
row (no live assignment right now) whose gap has already outlasted a short
debounce is moved out of `busy` into `Quiescence.settled` instead — visible,
never a silent drop, same as `stale` above. The debounce exists because "no
live assignment right now" is one momentary read of a gap that is normally
seconds long (see #2240's section below); requiring it to have already held
for `settle_seconds` (default 20s, matching #2139's identical-shaped
idle-restart debounce) is what keeps a lucky poll from reading a gap that is
about to close as though it were actually open. See `assess_quiescence`'s
own docstring for the exact mechanics, and #2854 for the fuller risk
analysis (`notify.lock`, intra-issue version skew, the drive process itself
never updating mid-run) that this fix does not, and does not need to, change
anything about.

LANE ORDER ANSWERS THE SKEW QUESTION
------------------------------------
#1835 asks whether a fleet mid-roll — hosts at two versions — is safe for
the board protocol, and insists on an explicit answer rather than an
assumption. It is safe **in one direction only**, and the direction is
already a documented failure: a *caller* on a new version calling a *daemon*
that predates the endpoint it wants gets a 405. New callers must therefore
never appear before the daemon can serve them.

:func:`plan_lanes` encodes that as a total order rather than leaving it to
whoever wrote the loop:

1. the **daemon host**'s Python lane first — it must lead, always;
2. every **other machine**'s Python lane;
3. each host's **systemd unit** lane (#1831 — `deploy/**` ships in the wheel
   as ``coord/deploy/``, so this lane can only roll *after* that host's venv
   swapped);
4. the **coord-tui** binaries last — a pure client of the board API, so it is
   the one lane that is safe at any skew.

Propagation is therefore explicitly **not** all-or-nothing; it is ordered so
that every intermediate state is one the protocol already tolerates
(old caller → new daemon), and never the one it does not.

THE GATE'S SCOPE MUST MATCH PROPAGATION'S REACH
-----------------------------------------------
#2052: propagation gates its roll on ``coord release verify``, but verify
grades lanes propagation **cannot roll**. On 2026-08-09, the first run that
ever reached the verify step did everything it was capable of — three python
lanes, three unit lanes, the one ``coord-tui`` it could reach — and still
came back red, because verify also counted ``~/.coord-cli-venv`` (a lane this
module has zero references to), the two *remote* ``coord-tui`` binaries
(which propagation itself reports have no remote install path) and the
``coord-serve`` process (whose venv had swapped but whose process nothing
here restarts). ``--rollback-on-red`` then reverted its own good work — and
would have done so on every run, forever.

:func:`scope_verification` is the fix, and the rule it encodes is general:
**a verify gate must not be able to fail for reasons the thing it gates
cannot influence.** Findings on lanes this run attempted and could have moved
are *blocking*; findings on lanes with no channel are *advisory* — reported,
journalled in full, never grounds for a rollback. Rolling back a good python
roll because a per-host binary could not be installed remotely is a category
error, not a safety measure.

Advisory is emphatically not "ignored", and the exemption is an allow-list of
*known* gaps rather than "anything we failed to classify" — an unrecognised
lane keeps the gate. A check that quietly stopped checking is the failure
this whole module exists to prevent.

Worth naming on its own: this defect was invisible for five runs because
every one of them found live work and correctly deferred. The deferral path
was thoroughly tested by circumstance; the success path was not tested at
all. **A mechanism whose failure mode only appears on the happy path needs
its happy path tested first, not last.**

REACH MUST GROW TO MEET THE GATE, NOT THE OTHER WAY AROUND
------------------------------------------------------------
#2069 (split out of #2067, the window bug): #2052 correctly stopped a run
from being punished for lanes it could not move. It did not make those lanes
move — a run could restart nothing but ``coord-agent``, report success, and
leave ``coord-serve`` serving v0.5.8's ``review.py`` while every readout said
v0.5.13. Advisory is "not ignored", but nothing downstream *acts* on an
advisory finding either, so a half-deploy and a whole one produced the same
green result — quietly, because #2052 had (correctly) silenced the loud
wrong version of this failure.

The fix is not a bigger allow-list; it is closing the gaps :data:`
OUT_OF_REACH_LANES` was recording. ``coord-serve``, ``coord-web`` and
``coord-drive-queue`` now get restarted as the last step of their host's
python lane (see :data:`RESTARTED_BY_PYTHON_LANE` and ``coord/commands/
release.py``'s ``_roll_python``), so their ``<unit> spawns`` findings — and,
for the daemon specifically, its own ``coord-serve process`` version — are
graded exactly like ``coord-agent spawns`` always was: blocking when this run
attempted that host's python lane, advisory when it did not. Coverage and
enforcement move together on purpose: :func:`scope_verification` reads
:data:`OUT_OF_REACH_LANES` and :data:`RESTARTED_BY_PYTHON_LANE` directly, so
shrinking one of those sets is the whole patch — no second edit to the gate
logic is needed to re-arm it for a lane that just gained a channel.

``~/.coord-cli-venv`` and the two remote ``coord-tui`` binaries are still
open — #2069's fix 2 and fix 3, respectively. Neither has a channel yet, so
both stay in :data:`OUT_OF_REACH_LANES` (``coord-tui`` per-host is handled
separately, by :func:`lane_is_out_of_reach`'s "unrollable" path rather than
this set — see :func:`plan_lanes`'s LANE ORDER section).

PURITY
------
Nothing in this module runs a subprocess, opens a socket, touches the DB or
reads the clock — same split ``coord/drive_queue.py`` documents, for the same
reason: every bug worth catching lives in the decision half. The clock is
passed in. ``coord/commands/release.py`` is the I/O shell that gathers the
facts, calls in here, executes the plan and appends the journal.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from coord.drive_queue import HOLD_FIRED, STATE_RUNNING, build_board_view, entry_key

# ── lane kinds ───────────────────────────────────────────────────────────────
#
# One string per *kind* of thing that has to move for the fleet to reach a
# version. These are the lanes #1835's precondition list names, and the same
# lanes `coord release verify` (#1834) grades afterwards. A release that
# propagates only `python` would have shipped #1543's `--dist` flag and none
# of its behaviour (that change was three unit files and a shell script).
LANE_PYTHON = "python"
LANE_UNITS = "units"
LANE_TUI = "tui"

#: Every lane this module knows how to roll, in no particular order — the
#: *order* is :func:`plan_lanes`'s output, not this tuple.
ALL_LANES: tuple[str, ...] = (LANE_PYTHON, LANE_UNITS, LANE_TUI)

# ── release channels (#2898) ─────────────────────────────────────────────────
#
# Until #2898 there was only ONE channel and it did not need a name: `publish.
# yml` turned one `v*` tag in this repo into one GitHub Release carrying the
# wheel AND the coord-tui binaries (#1242), so "the target version" was a
# single number every lane rolled toward, and `_roll_tui` could pass the
# coordinator's version straight to `coord tui update --version`.
#
# Phase 3 of #2894 split the channels. coord-tui tags and releases from its own
# repo, so ONE TAG CANNOT STAMP TWO REPOS and the two version lines move
# independently. The consequence this module has to encode is narrow but sharp:
#
#   A fleet on coord v0.5.x with coord-tui v0.2.y is CORRECT, not five
#   releases behind.
#
# Which means the tui lane's "target version" is not `record.target_version` —
# that is the *coordinator's* channel's tag, which coord-tui's Releases have
# never heard of. Asking for it resolves a tag that does not exist (a 404 every
# run) or, worse, silently matches an unrelated release that happens to share a
# number. The tui lane resolves its own channel's latest instead; see
# ``coord/commands/release.py``'s ``_roll_tui`` and
# :func:`coord.tui_release.fetch_latest_release_tag`.
#
# Naming them makes the split legible in `--dry-run` output rather than
# implicit in which function happens to be called: `coord release propagate
# --dry-run` prints the channel next to each planned roll, so "two channels"
# is something an operator reads, not something they have to know.
CHANNEL_COORD = "code-coordinator"
CHANNEL_TUI = "coord-tui"

#: Which release channel each lane draws its target version FROM.
#:
#: ``python``/``units`` both ship inside the wheel (#1831: the units are
#: package data at ``coord/deploy/``), so they are two lanes of ONE channel and
#: share ``record.target_version``. ``tui`` is the only lane whose version
#: comes from somewhere else.
LANE_CHANNELS: dict[str, str] = {
    LANE_PYTHON: CHANNEL_COORD,
    LANE_UNITS: CHANNEL_COORD,
    LANE_TUI: CHANNEL_TUI,
}


def channel_for_lane(lane: str) -> str:
    """Which release channel *lane* rolls from (#2898).

    Unknown lanes fall back to :data:`CHANNEL_COORD` rather than raising: this
    is a labelling function used in plan output and journal records, and an
    unrecognised lane must not be able to abort a propagation.
    """
    return LANE_CHANNELS.get(lane.strip(), CHANNEL_COORD)

# ── the gate's scope ─────────────────────────────────────────────────────────
#
# #2052: `coord release propagate` gated its roll on `coord release verify`,
# but verify grades lanes propagation **cannot roll**. On the first quiescent
# window the run did everything it was capable of — three python lanes, three
# unit lanes, the one coord-tui it could reach — and still came back red,
# because verify also counted `~/.coord-cli-venv` (a lane this module has no
# model of at all), the two remote `coord-tui` binaries (which propagation
# itself reports have NO remote install path), and the `coord-serve` process
# (whose venv had swapped but whose process nothing here restarts). With
# `--rollback-on-red` that reverted its own good work — every run, forever.
#
# The rule this section encodes: **a verify gate must not be able to fail for
# reasons the thing it gates cannot influence.** Findings on lanes this run
# actually attempted are BLOCKING; findings on lanes propagation has no
# channel for are ADVISORY — reported loudly, never grounds for a rollback.
#
# Advisory is not "ignored". An advisory crit is a real defect somebody must
# fix by hand; it is simply not evidence that *this roll* was bad, and
# reverting a good python roll because a per-host binary could not be
# installed remotely is a category error.

#: ``coord release verify`` lane name -> the propagation lane that could move
#: it. Exact names, matched against :func:`coord.release_verify.lanes_for_host`
#: and :func:`~coord.release_verify.findings_for_host`.
#:
#: ``coord-serve process`` (:func:`coord.release_verify.daemon_lanes`) is the
#: daemon's own introspected version — a different lane than ``coord-serve
#: spawns`` (what it would hand subprocesses), and the one #2069's restart
#: actually targets, so it needs its own exact entry rather than falling out
#: of the ``" spawns"`` suffix rule below.
#:
#: ``coord-agent process`` (:func:`coord.release_verify.lanes_for_host`) is
#: #2069 for ``coord-agent``: the agent's own frozen-at-start ``__version__``,
#: as distinct from ``coord-agent spawns`` (a fresh subprocess re-resolving
#: the venv on every poll, which flips the instant a swap lands whether or
#: not the agent restarted). On the daemon host the agent can *never*
#: self-restart (`agent_app.py`'s `_idle_restart_target` refuses there on
#: purpose), so this is the only lane that still reads a staged-but-
#: unrestarted swap as behind on that host — see the module docstring's
#: "REACH MUST GROW TO MEET THE GATE" section.
_VERIFY_LANE_EXACT: dict[str, str] = {
    "~/.coord-venv": LANE_PYTHON,
    "coord-tui": LANE_TUI,
    "coord-serve process": LANE_PYTHON,
    "coord-agent process": LANE_PYTHON,
}

#: Units whose *live process* a python-lane roll actually replaces. ``POST
#: /update`` swaps the venv and re-execs **the agent**; #2069 closes the rest
#: of the gap it used to leave open — the same roll now also asks the agent
#: to ``systemctl --user restart`` every sibling unit it finds running on
#: that host (``POST /restart-services``, called right after ``/update``
#: lands — see ``coord/commands/release.py``'s ``_roll_python``). Before
#: #2069, ``coord-serve``, ``coord-web`` and ``coord-drive-queue`` kept
#: running the generation they started with until a human restarted them by
#: hand — exactly the third failure in #2052's run: "the venv swapped, but
#: the *process* had not been restarted at the moment verify ran". A host
#: that does not actually run one of these units is unaffected — which unit
#: to restart is read off that host's own ``spawned_coord`` facts (a
#: topology question), never assumed from this list.
RESTARTED_BY_PYTHON_LANE: frozenset[str] = frozenset(
    {"coord-agent", "coord-serve", "coord-web", "coord-drive-queue"}
)

#: Lanes ``coord release verify`` grades that propagation has no channel for,
#: named individually so the exemption is a decision rather than a fallthrough.
#: ``~/.coord-cli-venv`` is the headline: `release_propagate.py` contains zero
#: references to it (#2069's fix 2 is still open — see the module docstring);
#: ``webapp bundle`` is SHA-versioned off a continuous timer and never
#: pip-versioned at all, so it has no "target version" to roll TO in the
#: first place. ``coord-serve process`` used to live here too — #2069 gave it
#: a channel (the restart above), so it now has its own entry in
#: :data:`_VERIFY_LANE_EXACT` instead, graded the same way every other
#: reachable lane is: blocking exactly when this run attempted that host's
#: python lane. Shrinking this set is exactly how new coverage is meant to
#: re-arm the gate (see the module docstring above).
#:
#: #3048 checked whether this set was overreaching for a host whose
#: ``~/.coord-venv``/``coord-agent`` is reachable over the agent API on
#: 7433 (dell64: cordoned, idle, a plain ``POST /update`` fixed it in
#: seconds once someone finally issued one). It is not — ``~/.coord-venv``,
#: ``coord-agent process`` and ``coord-agent spawns`` were never in this
#: set; they are ``LANE_PYTHON`` via :data:`_VERIFY_LANE_EXACT`, a lane
#: this module has always known how to roll. dell64's finding was scored
#: ADVISORY only because THIS run never attempted it (busy, or — #3048's
#: actual fix — a run that reached ``if not rolls:`` before ever calling
#: :func:`scope_verification` at all, see ``coord/commands/release.py``'s
#: ``_scope_gate``), never because ``lane_is_out_of_reach`` misjudged the
#: lane itself. Grow this set only for a lane with genuinely no remote
#: install path — the ``tui`` lane's per-host binary is the honest case
#: (``coord tui update`` has to run ON the host); a reachable venv is not
#: that case and must not be added here.
OUT_OF_REACH_LANES: frozenset[str] = frozenset({"~/.coord-cli-venv", "webapp bundle"})

#: ``"~/.coord-venv (precision)"`` — the label `coord release verify` builds
#: for a lane, and the only form a grouped finding names its lanes by.
_LANE_LABEL = re.compile(r"^(?P<lane>.+?)\s+\((?P<host>[^()]+)\)$")


def parse_lane_label(label: str) -> tuple[str, str] | None:
    """``"~/.coord-venv (precision)"`` -> ``("precision", "~/.coord-venv")``.

    ``coord release verify`` groups an ``--expected`` mismatch into ONE
    finding per offending version, naming its lanes as a comma-joined list of
    these labels. Scoping the gate therefore has to take such a finding apart
    again: "0.5.4, expected 0.5.8" across the CLI venv and the daemon process
    is advisory, the same sentence across a host's ``~/.coord-venv`` is not.
    """
    match = _LANE_LABEL.match(label.strip())
    if not match:
        return None
    return match.group("host").strip(), match.group("lane").strip()


def verify_lane_kind(lane: str) -> str | None:
    """Which propagation lane could move ``coord release verify``'s *lane*.

    ``None`` means "no propagation lane moves this" — which is not the same
    as "this lane is fine", only "this run is not what would fix it".
    """
    name = lane.strip()
    if name in _VERIFY_LANE_EXACT:
        return _VERIFY_LANE_EXACT[name]
    if name.startswith("unit "):
        # `unit coord-agent.service` — the #1831 deploy/** lane, rolled by
        # POST /deploy-units.
        return LANE_UNITS
    if name.endswith(" spawns"):
        unit = name[: -len(" spawns")].strip()
        return LANE_PYTHON if unit in RESTARTED_BY_PYTHON_LANE else None
    return None


def lane_is_out_of_reach(lane: str) -> bool:
    """Is *lane* one propagation structurally cannot influence?

    Deliberately an allow-list of *known* gaps rather than "anything
    :func:`verify_lane_kind` didn't recognise". A lane nobody thought about
    must keep the gate honest — the failure mode this whole module exists for
    is a check that quietly stopped checking.
    """
    name = lane.strip()
    if name in OUT_OF_REACH_LANES:
        return True
    return name.endswith(" spawns") and verify_lane_kind(name) is None


@dataclass(frozen=True)
class GateVerdict:
    """What the post-roll verification means *for this run*.

    ``severity`` is the worst **blocking** severity — the only thing
    ``--rollback-on-red`` may act on. ``advisory`` carries everything real but
    out of reach, so scoping the gate never becomes hiding the finding.
    """

    severity: str = "ok"
    blocking: tuple[dict, ...] = ()
    advisory: tuple[dict, ...] = ()
    #: ``lane@host`` for every lane this run could not roll here at all.
    unrollable: tuple[str, ...] = ()

    @property
    def red(self) -> bool:
        return self.severity == "crit"

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "blocking": [dict(f) for f in self.blocking],
            "advisory": [dict(f) for f in self.advisory],
            "unrollable": list(self.unrollable),
        }


#: Same ranking `coord release verify` uses; re-spelled rather than imported
#: to keep this module import-free of the verifier (the shell owns that seam).
_SEVERITY_RANK = {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}


def _finding_pairs(finding: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(host, lane)`` for every lane a verify finding actually speaks about."""
    lane_field = str(finding.get("lane") or "")
    pairs = [
        parsed
        for part in lane_field.split(", ")
        if (parsed := parse_lane_label(part)) is not None
    ]
    if pairs:
        return pairs
    return [(str(finding.get("host") or ""), lane_field)]


def attempted_scope(lanes: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """``{(lane, host)}`` this run actually attempted and could have moved.

    Read straight off the journalled lane records, so the gate's scope is
    exactly what the history says the run did — not a second, parallel
    account of it that could drift.

    Excluded, on purpose:

    * ``ok is None`` **without** a real attempt — a host skipped because the
      daemon's python lane failed, or already on the target;
    * anything flagged ``unrollable`` — a lane with no channel *on this host*
      (the remote ``coord-tui``, an agent that predates ``/deploy-units``).
      Attempting is not the same as being able to.
    """
    scope: set[tuple[str, str]] = set()
    for row in lanes:
        lane = str(row.get("lane") or "")
        host = str(row.get("host") or "")
        if lane not in ALL_LANES or not host:
            continue
        if row.get("unrollable"):
            continue
        if row.get("ok") is None:
            continue
        scope.add((lane, host))
    return scope


def scope_verification(
    verification: Mapping[str, Any] | None,
    *,
    lanes: Iterable[Mapping[str, Any]] = (),
) -> GateVerdict:
    """Split a ``coord release verify`` report into blocking and advisory.

    *verification* is the report as :meth:`coord.release_verify.VerifyReport.
    to_dict` renders it; *lanes* are this run's journalled lane records.

    A finding is BLOCKING when any lane it names is one this run attempted
    and could have moved, or when it names a lane nothing here recognises (an
    unclassifiable finding must keep the gate honest — see
    :func:`lane_is_out_of_reach`). Everything else is advisory.
    """
    rows = list(lanes)
    scope = attempted_scope(rows)
    unrollable = tuple(
        f"{row.get('lane')}@{row.get('host')}" for row in rows if row.get("unrollable")
    )
    if not verification:
        return GateVerdict(unrollable=unrollable)

    blocking: list[dict] = []
    advisory: list[dict] = []
    for finding in verification.get("findings") or []:
        if not isinstance(finding, Mapping):
            continue
        pairs = _finding_pairs(finding)
        holds = False
        for host, lane in pairs:
            kind = verify_lane_kind(lane)
            if kind is not None:
                holds = holds or (kind, host) in scope
            elif not lane_is_out_of_reach(lane):
                # Not a lane this module has an opinion about — e.g. an
                # unreachable host's "(all lanes)", or a lane added to the
                # verifier since. Fail toward keeping the gate.
                holds = True
        (blocking if holds else advisory).append(dict(finding))

    severity = "ok"
    for finding in blocking:
        sev = str(finding.get("severity") or "unknown")
        if _SEVERITY_RANK.get(sev, 1) > _SEVERITY_RANK[severity]:
            severity = sev
    return GateVerdict(
        severity=severity,
        blocking=tuple(blocking),
        advisory=tuple(advisory),
        unrollable=unrollable,
    )


# ── propagation outcomes ─────────────────────────────────────────────────────
#
# The status recorded in the journal for one propagation attempt. #1835:
# "a silent success is indistinguishable from a silent no-op, which is
# precisely how 2026-08-04 stayed invisible" — so *every* attempt appends a
# record, including the boring "deferred, fleet busy" ones. A timer that
# fired forty times and deferred forty times must look different from a
# timer that never fired at all.
STATUS_DEFERRED = "deferred"
STATUS_UP_TO_DATE = "up-to-date"
STATUS_ROLLED = "rolled"
STATUS_VERIFIED = "verified"
STATUS_ROLLED_BACK = "rolled-back"
STATUS_FAILED = "failed"
#: #2583: the fleet needs a roll (there is a delta) but that delta has not
#: yet reached ``propagation.min_releases_behind``/``--min-behind`` — a
#: REPORTED no-op, deliberately distinct from :data:`STATUS_DEFERRED` (which
#: means "busy, would roll if it could"). A holding run cordons nothing and
#: touches no host; see `coord/commands/release.py`'s gate for where this is
#: decided.
STATUS_HOLDING = "holding"

#: Statuses that mean "this attempt changed nothing on any host". Used by the
#: renderer to keep a long quiet night readable.
NO_OP_STATUSES: frozenset[str] = frozenset(
    {STATUS_DEFERRED, STATUS_UP_TO_DATE, STATUS_HOLDING}
)

#: Board assignment statuses that count as live work. Mirrors the set
#: ``coord/agent_app.py``'s ``/update`` refuses on, deliberately: propagation
#: must not schedule an update the agent would then refuse.
LIVE_ASSIGNMENT_STATUSES: frozenset[str] = frozenset({"RUNNING", "PENDING"})

#: #2854: how long a host must show NO live assignment for an in-flight
#: (but between-legs) drive-queue entry before that gap counts as settled
#: enough to roll. Mirrors the #2139 idle-restart debounce's 20s window —
#: same shape of problem (a momentarily-quiet host that is about to become
#: busy again as the next leg lands), same fix. See :func:`assess_quiescence`
#: for how this is spent.
DEFAULT_BETWEEN_LEGS_SETTLE_SECONDS = 20.0


# ── busy signals ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Busy:
    """One concrete reason the fleet is not quiescent right now.

    Named down to the subject so a deferral is actionable prose ("dellserver
    has 2 live assignments") rather than the useless "fleet busy" — a
    deferral nobody can explain is a deferral nobody can distinguish from a
    wedged timer.
    """

    kind: str
    subject: str
    detail: str = ""
    #: Which host this signal blocks, or ``None`` when it cannot be pinned
    #: to one — a drive-queue entry launched before #1870 recorded
    #: ``launch_host``, or a fleet-level fact like "the board itself is
    #: unreadable" that says nothing about which host is actually busy.
    #: #2067: an unattributable signal must fail toward blocking EVERY
    #: host, never toward blocking none of them — see
    #: :attr:`Quiescence.fleet_wide_busy`.
    host: str | None = None

    def describe(self) -> str:
        base = f"{self.kind}: {self.subject}"
        return f"{base} ({self.detail})" if self.detail else base


def confirmation_lock_busy(lock_path: Path) -> list[Busy]:
    """A #2464 out-of-band test confirmation in flight, as a `Busy` signal
    (#2596).

    2026-08-22: `coord release propagate` restarted `coord-agent` while a
    confirmation's build/test subprocess (`coord.confirm_test.confirm_branch`)
    was running as a child of that unit — systemd reaped its cgroup, the
    subprocess died by SIGTERM (`exit -15`), and while #2527 already stops
    that from being misread as a REFUTATION (see `coord.confirm_test`'s
    `KIND_SIGNAL`), nothing stopped the restart from happening in the first
    place. Two workers got dispatched against a branch with nothing wrong
    with it, one of them on opus. This closes the other half: check for one
    of these BEFORE restarting anything, exactly like a live headless
    assignment already gates a restart (module docstring, "Why the cut is
    not optional").

    A LOCAL, non-blocking probe of *lock_path* — in production
    :func:`coord.filelock.notify_lock_path`, the same lock
    `coord.notify.run_drain` holds for a confirmation's whole duration (see
    `coord.confirm_test.CONFIRM_PASS_BUDGET_SECONDS`) — not a fleet-wide
    fan-out. `deploy/coord-release-propagate.service`'s "WHICH HOST" section
    installs this unit ONLY on the daemon host, the same host `coord
    notify`'s drain (and so any confirmation) actually runs on, so a network
    probe would answer the identical local question at ten times the cost.

    Unattributable on purpose (``host=None``, so it blocks the WHOLE fleet,
    not just one host — see :attr:`Busy.host`'s #2067 note): the restart this
    protects against is specifically the DAEMON's own `coord-agent`, and the
    daemon-leads invariant (:func:`assess_quiescence`'s docstring, "LANE
    ORDER") already turns "the daemon host is busy" into "defer the whole
    roll" — so resolving a host name for this signal (not yet known this
    early in `propagate`, #2176) would add complexity for no behavioural
    difference.

    Fails OPEN: a lock probe that raises for a reason OTHER than contention
    (a permissions problem, a read-only home) is a local host problem this
    call must not turn into "block the entire fleet until someone notices" —
    it returns ``[]`` and the existing per-host/board signals still apply.
    """
    from coord.filelock import FileLock, LockBusy  # noqa: PLC0415

    lock = FileLock(lock_path)
    try:
        lock.acquire(timeout=0.0)
    except LockBusy:
        return [
            Busy(
                kind="confirmation/notify drain running",
                subject=str(lock_path),
                detail=(
                    "a #2464 out-of-band test confirmation (or another "
                    "notify-drain side effect) may be running under this "
                    "lock right now — restarting agents could SIGTERM it "
                    "mid-run and misclassify a good branch (#2596)"
                ),
            )
        ]
    except OSError:
        return []
    else:
        lock.release()
        return []


@dataclass(frozen=True)
class Quiescence:
    """Is there a window right now, and if not, where.

    #2067: quiescence used to be one fleet-wide boolean, computed by
    discarding the host every :class:`Busy` already carries. On a fleet
    whose drive queue runs continuously, *some* host is busy essentially
    always, so that boolean is false essentially always and propagation
    never fires — "correctly, quietly, and uselessly". ``quiescent``/
    ``reason`` stay as the fleet-wide summary (still meaningful — an
    unattributable signal or `--force` both want a single answer), but a
    caller that wants to roll a partially-busy fleet should use
    :meth:`rollable_hosts` instead.
    """

    quiescent: bool
    busy: tuple[Busy, ...] = ()
    #: Fired deploy gates (#1757) found while assessing. Not busy signals —
    #: see the module docstring. Carried through so the caller can release
    #: them after a verified roll.
    fired_holds: tuple[str, ...] = ()
    #: #2110: `running` queue entries this assessment could DISPROVE — the
    #: entry's own issue is landed (merged or closed) per the SAME board read
    #: used everywhere else here, so the row cannot possibly still be
    #: in-flight, whatever its `state` column says. Not a busy signal (the
    #: whole point is that it does NOT block); not silent either (#1616's
    #: "the pipeline has no clock" lesson) — surfaced here so a caller can
    #: log it and point at `coord drive-queue tick --reconcile-only`, the
    #: thing that actually clears the row, rather than the stale state
    #: quietly evaporating from the reasoning with no record it was ever
    #: wrong. See `_reconcile_running` in `coord/drive_queue.py` for the tick
    #: doing the same disproof on its own cadence; this is the same evidence,
    #: re-checked on READ so a stopped timer cannot make it unfalsifiable.
    stale: tuple[str, ...] = ()
    #: #2854: `running` queue rows that are genuinely BETWEEN LEGS (no live
    #: assignment right now) and have stayed that way for at least the
    #: settle window — see :func:`assess_quiescence`'s *now*/*settle_seconds*.
    #: Not a busy signal (the whole point is that it does NOT block); not
    #: silent either, same reasoning as `stale` above: a host rolled while
    #: its issue's drive-queue row is still `running` must leave a readable
    #: trace of why that was safe, not just a quiet absence from `busy`.
    settled: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        if self.quiescent:
            if self.fired_holds:
                return (
                    "quiescent — and "
                    f"{len(self.fired_holds)} deploy gate(s) are waiting on "
                    "exactly this deploy"
                )
            return "quiescent — nothing in flight"
        return "; ".join(b.describe() for b in self.busy) or "busy"

    @property
    def fleet_wide_busy(self) -> tuple[Busy, ...]:
        """Busy signals that cannot be pinned to one host.

        These block every host, not just the (nonexistent) one they name —
        there is no way to know which host an unattributable signal
        actually occupies, so the safe read is "all of them".
        """
        return tuple(b for b in self.busy if b.host is None)

    def busy_hosts(self) -> set[str]:
        """Every host a *specific* busy signal names.

        Excludes unattributable signals — see :attr:`fleet_wide_busy` for
        those; a caller must check both, which :meth:`rollable_hosts` does.
        """
        return {b.host for b in self.busy if b.host}

    def rollable_hosts(self, hosts: Iterable[str]) -> list[str]:
        """Which of *hosts* have no busy signal against them right now.

        Empty whenever any busy signal cannot be attributed to a host: an
        unreadable board, or a drive-queue entry with no recorded launch
        host, means "the fleet is busy somewhere unknown", which is not
        distinguishable from "everywhere" and must be treated as such.
        """
        if self.fleet_wide_busy:
            return []
        occupied = self.busy_hosts()
        return [h for h in hosts if h not in occupied]

    def busy_reason_for_host(self, host: str) -> str:
        """Why *host* specifically is not rollable right now.

        Empty string when nothing names it — the caller's cue that this
        host is free.
        """
        reasons = [b.describe() for b in self.busy if b.host is None or b.host == host]
        return "; ".join(reasons)

    def to_dict(self) -> dict:
        return {
            "quiescent": self.quiescent,
            "reason": self.reason,
            "busy": [asdict(b) for b in self.busy],
            "fired_holds": list(self.fired_holds),
            "stale": list(self.stale),
            "settled": list(self.settled),
        }


def _queue_key(entry: Mapping[str, Any]) -> str:
    """``repo#issue`` for a ``drive_queue`` row, however it reached us.

    ``/board`` publishes the sqlite columns verbatim (``repo_name`` /
    ``issue_number``); an already-rendered row may carry ``key``. Both are
    accepted so this never silently degrades to ``"?"`` — a busy signal
    nobody can name is a busy signal nobody can act on, and ``coord
    drive-queue resume`` needs the real key to release the gate.
    """
    key = entry.get("key")
    if key:
        return str(key)
    repo = entry.get("repo_name") or entry.get("repo")
    issue = entry.get("issue_number") or entry.get("issue")
    if repo and issue is not None:
        try:
            return entry_key(str(repo), int(issue))
        except (TypeError, ValueError):
            return f"{repo}#{issue}"
    return "?"


def _live_assignment_hosts(assignments: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """``repo#issue`` → machine, for every currently-live assignment row.

    The lookup :func:`busy_host_for_entry` needs to resolve an *unpinned*
    running queue entry's real worker host (#2138), built once per
    :func:`assess_quiescence` call rather than re-scanned per entry. Keyed
    the same way :func:`_queue_key` renders a queue row, so the two are
    directly comparable.
    """
    hosts: dict[str, str] = {}
    for row in assignments:
        status = str(row.get("status") or "").upper()
        if status not in LIVE_ASSIGNMENT_STATUSES:
            continue
        machine = str(row.get("machine_name") or row.get("machine") or "") or None
        if not machine:
            continue
        hosts[_queue_key(row)] = machine
    return hosts


def _last_assignment_rows(
    assignments: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """``repo#issue`` → the single MOST RECENT assignment row for that key.

    Any status, terminal included. The shared selection behind
    :func:`_last_assignment_hosts` (which host) and
    :func:`_last_assignment_idle_since` (#2854: how long that host has been
    gone) — both must agree on WHICH row is "last", or a between-legs host
    and its settle-window clock could end up describing two different
    assignments.

    Ordered by ``dispatched_at``, with rows that carry no timestamp treated
    as oldest — a row we cannot place in time must never displace one we can.
    """
    best: dict[str, tuple[float, Mapping[str, Any]]] = {}
    for row in assignments:
        machine = str(row.get("machine_name") or row.get("machine") or "") or None
        if not machine:
            continue
        try:
            when = float(row.get("dispatched_at") or 0.0)
        except (TypeError, ValueError):
            when = 0.0
        key = _queue_key(row)
        previous = best.get(key)
        if previous is None or when >= previous[0]:
            best[key] = (when, row)
    return {key: row for key, (_, row) in best.items()}


def _last_assignment_hosts(assignments: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """``repo#issue`` → the machine that ran its MOST RECENT assignment (#2240).

    Any status, terminal included — this is deliberately the "last known
    host" lookup, used only when :func:`_live_assignment_hosts` has nothing
    because the entry is between legs. ``/board`` publishes terminal
    assignment rows within its retention window
    (``coord.dao.board_projection``'s ``_capped_assignments``), so the
    previous leg of a drive that finished minutes ago is right there; what is
    NOT there is a drive whose every leg fell out of retention, and such an
    entry keeps the old unattributable reading.

    See :func:`_last_assignment_rows` for the row-selection rule this reads.
    """
    return {
        key: str(row.get("machine_name") or row.get("machine") or "")
        for key, row in _last_assignment_rows(assignments).items()
    }


def _last_assignment_idle_since(
    assignments: Iterable[Mapping[str, Any]],
) -> dict[str, float]:
    """``repo#issue`` → when its last-known host stopped running it (#2854).

    ``finished_at`` off the SAME row :func:`_last_assignment_hosts` already
    picked as "most recent" (see :func:`_last_assignment_rows`) — never a
    second, independently-selected row, which could disagree about which
    assignment is even being described.

    A key is absent from the result — not mapped to ``0.0`` or any other
    guess — when that row carries no readable ``finished_at`` (a legacy row,
    or one written before the field existed). :func:`assess_quiescence`
    treats an absent entry exactly like "not yet settled": a between-legs
    gap this function cannot measure must never be read as long enough to
    roll on a guess, the same "missing data fails toward the conservative
    reading" rule the rest of this module already follows (see e.g.
    `DRIFT_UNKNOWN` in `coord/release_cordon.py`).
    """
    out: dict[str, float] = {}
    for key, row in _last_assignment_rows(assignments).items():
        raw = row.get("finished_at")
        if raw is None:
            continue
        try:
            out[key] = float(raw)
        except (TypeError, ValueError):
            continue
    return out


def busy_host_for_entry(
    entry: Mapping[str, Any],
    live_assignment_hosts: Mapping[str, str] | None = None,
    last_assignment_hosts: Mapping[str, str] | None = None,
) -> str | None:
    """Which host a ``running`` drive-queue row actually occupies (#2101, #2138).

    #2067 attributed this to ``launch_host`` (#1870) — the host whose tick
    launched the session — falling back to ``machine``. Measured on
    2026-08-10, that reading pins the WHOLE fleet on every drive:

    * the drive-queue tick runs on the timer host, so ``coord drive --tmux``
      is spawned there and ``launch_host`` is *always* that host;
    * that host is the daemon host, and the daemon must lead every roll, so
      the one host it charges is the one host whose busyness defers every
      other host's python lane (see the module docstring's LANE ORDER).

    Net effect: **any drive anywhere pinned the entire fleet from rolling**,
    which is fact 2 of #2101 and half of why the fleet sat eleven releases
    behind. #2101 inverted the precedence for a ``--machine``-pinned entry —
    charged to the machine that will actually run the WORKER, because the
    worker is what an agent restart destroys (``coord/agent_app.py``'s
    ``/update`` refuses a host with live assignments for exactly that reason).
    The launch host merely hosts an observer tmux session, and #2101's cordon
    is what protects *it*: no new drive is launched onto a cordoned host at
    all, so nothing new starts there while it waits to roll.

    #2101 left the unpinned case reading ``launch_host`` — and measured
    2026-08-12, NO production queue entry sets ``machine`` at all, so
    *every* real entry hit that fallback. Since ``launch_host`` is always the
    timer host, i.e. the daemon host, this was the exact reading #2101 set
    out to remove, just reached by the unpinned path instead of the pinned
    one: one drive anywhere (any repo, any worker) still pinned the daemon
    "busy" and, via the daemon-first rule, deferred the whole fleet (#2138).

    So for an unpinned entry this now resolves the real worker host from the
    live assignment row for the SAME issue (``live_assignment_hosts``, built
    by :func:`_live_assignment_hosts` from the same ``assignments`` read) —
    the one place the auto-picked worker machine is actually recorded. That
    row is a busy signal in its own right, attributed correctly, a few lines
    below in :func:`assess_quiescence`; this just reuses its host instead of
    inventing a second, wrong one. The launch host is never charged for an
    unpinned entry: it hosts an observer tmux session an agent restart does
    not destroy, and #2101's cordon already keeps new drives off it while it
    waits to roll.

    #2138 left one gap and named it: a ``running`` row with no live
    assignment for its issue — between legs, where the previous leg's
    assignment has closed out and the next has not landed yet — had no host
    this function could name, and an unattributable signal blocks every host
    (:attr:`Quiescence.fleet_wide_busy`). That was priced as a bounded cost:
    some rolls defer. #2240 measured the real cost — not, as first written
    here, that a standing cordon stops the next leg from being dispatched
    (a cordon is follow-on-blind; a review for in-flight work routes onto a
    cordoned host fine, #2240 fix 2, corrected by #2741 after that claim was
    found live in an operator-facing message it never should have reached).
    The real cost is narrower and just as bad in practice: an unattributable
    "between legs" row reads as fleet-wide busy for as long as the next leg
    takes to be picked up and dispatched, so every propagate tick in that
    window defers, and a deferral leaves the cordon standing rather than
    lifting it — a normally-seconds-long window can span several ticks.

    So the between-legs case now falls back to the host that ran the entry's
    **last** assignment (*last_assignment_hosts*, any status). That is the
    narrow version of #2138's own fix, extended to the gap it left open: the
    previous leg's row names a machine, the next leg is overwhelmingly likely
    to land on the same one, and being wrong costs one host held that need
    not have been — versus the entire fleet held, which is what "unnameable"
    costs. #2240's fixes 1 and 2 are what actually break the deadlock; this
    one shrinks its blast radius from fleet-wide to one host.

    ``None`` is still returned when NOTHING names a host — no ``machine``
    pin, no live assignment and no assignment in the board's retention
    window at all. That genuinely is "busy somewhere unknown", and it still
    fails toward blocking everything.
    """
    machine = str(entry.get("machine") or "") or None
    if machine:
        return machine
    key = _queue_key(entry)
    if live_assignment_hosts:
        host = live_assignment_hosts.get(key)
        if host:
            return host
    if last_assignment_hosts:
        return last_assignment_hosts.get(key)
    return None


def assess_quiescence(
    *,
    queue_entries: Iterable[Mapping[str, Any]] = (),
    assignments: Iterable[Mapping[str, Any]] = (),
    issues: Iterable[Mapping[str, Any]] = (),
    extra_busy: Iterable[Busy] = (),
    now: float | None = None,
    settle_seconds: float = DEFAULT_BETWEEN_LEGS_SETTLE_SECONDS,
) -> Quiescence:
    """Is the fleet idle enough to restart every agent on it?

    *queue_entries* are ``drive_queue`` rows as they come off the board /
    ``coord drive-queue list --json``; *assignments* are board assignment
    rows; *issues* are board issue rows (``repo_name``/``number``/``state``).
    All three are read as plain mappings — #1523 §2's "typed state, never
    CLI prose", the rule both bugs in the ad-hoc overnight sequencer broke.

    ``extra_busy`` is the seam for host-local signals the board cannot see
    (an interactive tmux session, a machine paused by an operator); the
    shell passes them in rather than this module growing a way to look.
    #2228: the interactive-session half is wired from
    ``coord.commands.release._interactive_session_busy``, fed by the same
    fleet-wide tmux sweep ``coord sessions --remote`` renders — until then
    this half of the seam existed but nothing ever fed it. #2174: the
    operator-pause half is wired from
    ``coord.commands.release._paused_machine_busy`` — a machine under an
    explicit ``coord pause`` or an active quiet-hours window (but NOT a
    #2101 release cordon: that is this module's own drain mechanism, not a
    sign a host is already quiescent, and feeding it back in here would
    make a cordon defer the very roll it exists to unblock) becomes a
    host-pinned :class:`Busy`. Tmux liveness genuinely has no fleet-wide
    read (it is a LOCAL fact, #1870, and propagation may run from any
    host) — pause state does, because it is daemon-aware
    (``coord.machine_pause``) the same way the board is, so both callers
    can read it cheaply with no new probe.

    Because a :class:`Busy` carries a host, #2067's per-host quiescence
    does the obvious thing for a paused NON-daemon host: it defers only
    that host, and the rest of the fleet still rolls. A paused DAEMON host
    is different on purpose — the daemon-leads invariant (see the module
    docstring's LANE ORDER) already defers the WHOLE run whenever the
    daemon host is itself busy and behind, and a pause is just one more
    way for the daemon host to be busy. So ``coord pause <daemon-host>``
    halting all propagation, not just that host's own lane, is read as the
    correct interpretation of "leave this box alone" applied to the one
    host every other host's roll depends on — not an emergent accident.

    #2110: a ``running`` queue row is not, on its own, proof of anything —
    the reconciler that would have moved it to ``done`` lives inside
    ``coord drive-queue tick``, and a stopped timer means nothing ever runs
    it. The 2026-08-10 incident deferred `coord release propagate` for over
    an hour on a row describing a drive that had merged, closed and left no
    trace anywhere on the fleet — the row was simply never re-examined. So
    before trusting ``state == "running"`` this re-derives the SAME
    disproof `coord.drive_queue._reconcile_running` uses on its own tick
    (``coord.drive_queue.build_board_view(...).facts(key).landed`` — merged
    or closed) against *this* read of the board, live at call time, rather
    than only during a tick that may not be running right now. A row that
    fails that check cannot possibly still be in flight, so it is excluded
    from ``busy`` and reported in :attr:`Quiescence.stale` instead — visible,
    not silently dropped (#1616).

    This is deliberately narrower than the tick's own reconciliation: it has
    no local tmux read (liveness is a LOCAL fact, #1870, and this may run on
    any host) and no attempt-tracking, so it can only ever DISPROVE a
    ``running`` row, never retry or block one — that stays the tick's job.
    It closes exactly the gap that let a landed row block a roll forever
    with no clock and no way to contradict it.

    #2854: ROLL BETWEEN LEGS, NOT BETWEEN ISSUES. Even after #2240 narrowed a
    between-legs ``running`` row from fleet-wide to its one last-known host,
    that host stayed charged as busy for the entry's ENTIRE remaining
    lifetime — work, test, review, merge — including every gap between legs
    where no worker process is actually running there. #2618 established
    that a between-legs gap has nothing an agent restart could kill (no
    poll in flight survives a restart uneventfully, and no unretried write
    is outstanding either); this is the other half — actually treating that
    gap as idle instead of only proving it *could* be.

    A between-legs row (no entry in *live_assignment_hosts*) is no longer
    unconditionally busy for its attributed host. It stays busy — same as
    before — unless ALL of: *now* is given (a caller that never asks for the
    settle window gets the old, safer-by-default behaviour), the row's last
    known host can actually be named, and :func:`_last_assignment_idle_since`
    can prove *at least* `settle_seconds` have passed since that host's last
    assignment for this entry actually **finished** (``finished_at`` — not
    when it was dispatched, which would only ever UNDERSTATE how recently a
    worker was alive there). Meeting all three moves the entry into
    :attr:`Quiescence.settled` instead of :attr:`Quiescence.busy` — visible,
    exactly like `stale` above, never a silent drop.

    The settle window itself exists because "no live assignment right now"
    is a single, momentary read, and the between-legs gap it is trying to
    catch is normally seconds long (see the module docstring's #2240
    section) — a read that happens to land exactly inside one is not
    evidence the gap will still be open by the time a roll actually acts on
    it. Requiring the gap to have already lasted `settle_seconds` (default
    :data:`DEFAULT_BETWEEN_LEGS_SETTLE_SECONDS`, the same 20s the #2139
    idle-restart debounce uses for an identical shape of flapping) is a
    debounce, not a probability judgement — same mechanism, same reason.
    """
    queue_entries = list(queue_entries)
    assignments = list(assignments)
    issues = list(issues)
    board = build_board_view({"assignments": assignments, "issues": issues})
    live_assignment_hosts = _live_assignment_hosts(assignments)
    last_assignment_hosts = _last_assignment_hosts(assignments)
    last_assignment_idle_since = _last_assignment_idle_since(assignments)

    busy: list[Busy] = []
    fired: list[str] = []
    stale: list[str] = []
    settled: list[str] = []

    for entry in queue_entries:
        state = str(entry.get("state") or "")
        key = _queue_key(entry)
        if state == STATE_RUNNING:
            if board.facts(key).landed:
                # Disproved: this issue is merged or closed, so the row
                # cannot still be in flight whatever its `state` column
                # says. Not busy — and not silently dropped either.
                stale.append(key)
            else:
                host = busy_host_for_entry(
                    entry, live_assignment_hosts, last_assignment_hosts
                )
                between_legs = (
                    host is not None
                    and not entry.get("machine")
                    and key not in live_assignment_hosts
                )
                # #2854: a between-legs row whose gap has already outlasted
                # the settle window is treated as genuinely idle for its
                # host, not merely "attributed and busy anyway" — see this
                # function's own docstring. `idle_for` stays `None` (never
                # settled) whenever it cannot be PROVEN: no `now`, or no
                # readable `finished_at` for the last assignment.
                idle_for = None
                if between_legs and now is not None:
                    idle_since = last_assignment_idle_since.get(key)
                    if idle_since is not None:
                        idle_for = now - idle_since
                if idle_for is not None and idle_for >= settle_seconds:
                    settled.append(key)
                else:
                    # #2240: say WHICH reading named the host. "between legs,
                    # attributed to its last known host" is the difference
                    # between a signal holding one machine and one holding
                    # the fleet, and a deferral nobody can take apart is a
                    # deferral nobody can act on.
                    detail = "restarting agents now would kill it mid-flight"
                    if between_legs:
                        detail += (
                            "; between legs — attributed to its last known "
                            "host (#2240)"
                        )
                    busy.append(
                        Busy(
                            kind="drive-queue entry running",
                            subject=key,
                            detail=detail,
                            host=host,
                        )
                    )
        # A *fired* gate is the opposite of busy — the queue has stopped
        # itself waiting for precisely this deploy. Recorded, never counted.
        if str(entry.get("hold_state") or "") == HOLD_FIRED:
            fired.append(key)

    for row in assignments:
        status = str(row.get("status") or "").upper()
        if status not in LIVE_ASSIGNMENT_STATUSES:
            continue
        machine = str(row.get("machine_name") or row.get("machine") or "") or None
        subject = str(
            row.get("issue_number")
            or row.get("issue")
            or row.get("assignment_id")
            or "?"
        )
        busy.append(
            Busy(
                kind=f"live {status} assignment",
                subject=f"{machine or '?'}:{subject}",
                detail="`coord agent update` would refuse this host anyway",
                host=machine,
            )
        )

    busy.extend(extra_busy)
    return Quiescence(
        quiescent=not busy,
        busy=tuple(busy),
        fired_holds=tuple(dict.fromkeys(fired)),
        stale=tuple(dict.fromkeys(stale)),
        settled=tuple(dict.fromkeys(settled)),
    )


def holds_to_release(quiescence: Quiescence, *, verified: bool) -> tuple[str, ...]:
    """Which deploy gates a finished propagation should release (#1757).

    Only after a **verified** roll. Releasing a gate on an unverified — or
    rolled-back — propagation would restart the overnight queue into exactly
    the "merged is not live" trap the gate exists to prevent, which is the
    single most expensive recurring failure in this fleet.
    """
    return quiescence.fired_holds if verified else ()


# ── the roll plan ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaneRoll:
    """One unit of propagation work: roll *lane* on *host* to a version."""

    order: int
    lane: str
    host: str
    #: Why this step sits where it does in the order. Rendered in `--dry-run`
    #: and journalled, because the ordering is a protocol-safety argument and
    #: an argument nobody can read is an argument nobody can check.
    rationale: str = ""
    #: Which release channel this lane's target version comes from (#2898).
    #: Defaulted rather than required so an older journal record — or a test
    #: constructing a LaneRoll by hand — still reads as the coordinator's own
    #: channel, which is what every lane meant before the split.
    channel: str = CHANNEL_COORD

    @property
    def label(self) -> str:
        return f"{self.lane}@{self.host}"

    @property
    def plan_line(self) -> str:
        """One rendered line of ``--dry-run``'s plan, channel included.

        #2898's acceptance criterion: the two channels must be named
        *distinctly* in the plan, so an operator can see at a glance that the
        tui lane is not chasing the coordinator's version. Kept here rather
        than in ``coord/commands/release.py`` because this module is the pure
        half — the rendering is testable without any I/O.
        """
        return f"{self.order}. {self.lane}@{self.host} [{self.channel}] — {self.rationale}"


def plan_lanes(
    *,
    daemon_host: str | None,
    hosts: Sequence[str],
    lanes: Iterable[str] = ALL_LANES,
    skip_hosts: Iterable[str] = (),
) -> list[LaneRoll]:
    """The total order in which lanes may roll. See the module docstring.

    The invariant this function exists to hold: **the daemon never lags a
    caller.** A host running a newer ``coord`` than the daemon it talks to
    reproduces the documented 405 (caller wants an endpoint the daemon does
    not serve yet); the reverse — a newer daemon serving older callers — is
    the skew the board protocol is built to tolerate, since that is the
    steady state between every release and every fleet update anyway.

    *skip_hosts* drops hosts already on the target version, so a re-run after
    a partial failure resumes rather than restarting.

    #2898: every roll carries its :data:`LANE_CHANNELS` channel. The order is
    unaffected — the tui lane still goes last for the reason it always did (a
    pure board-API client, safe at any skew) — but the channel is what
    ``--dry-run`` renders, and it is what the tui lane resolves its target
    version from.

    Known gap, stated rather than silently inherited: *skip_hosts* is computed
    by :func:`hosts_already_current` against the **coordinator's** channel
    only, and it skips a host for EVERY lane. So a host already on the target
    coord version is skipped for the tui lane too, even though coord-tui's
    channel may have moved since. That is not new behaviour and it is bounded
    — coord-tui is a per-host binary this module can only install locally
    anyway (``_roll_tui``), so the same operator running ``coord tui update``
    closes it in one command — but it is a real second-channel blind spot,
    and it belongs with the missing remote install path (#2069 fix 3), not
    with the channel split.
    """
    wanted = [lane for lane in ALL_LANES if lane in set(lanes)]
    skip = set(skip_hosts)
    ordered_hosts: list[str] = []
    if daemon_host and daemon_host in hosts:
        ordered_hosts.append(daemon_host)
    ordered_hosts.extend(h for h in hosts if h != daemon_host)

    rolls: list[LaneRoll] = []
    order = 0

    if LANE_PYTHON in wanted:
        for host in ordered_hosts:
            if host in skip:
                continue
            first = host == daemon_host
            order += 1
            rolls.append(
                LaneRoll(
                    order=order,
                    lane=LANE_PYTHON,
                    host=host,
                    channel=CHANNEL_COORD,
                    rationale=(
                        "daemon host leads: a caller must never reach an "
                        "endpoint its daemon predates (the documented 405)"
                        if first
                        else "callers follow the daemon, never lead it"
                    ),
                )
            )

    if LANE_UNITS in wanted:
        for host in ordered_hosts:
            if host in skip:
                continue
            order += 1
            rolls.append(
                LaneRoll(
                    order=order,
                    lane=LANE_UNITS,
                    host=host,
                    channel=CHANNEL_COORD,
                    rationale=(
                        "#1831: the units ship inside the wheel as "
                        "coord/deploy/, so this host's venv must have "
                        "swapped first"
                    ),
                )
            )

    if LANE_TUI in wanted:
        for host in ordered_hosts:
            if host in skip:
                continue
            order += 1
            rolls.append(
                LaneRoll(
                    order=order,
                    lane=LANE_TUI,
                    host=host,
                    channel=CHANNEL_TUI,
                    rationale=(
                        "coord-tui is a pure board-API client — safe at any "
                        "skew, so it goes last and can never block the fleet; "
                        f"#2898: rolls to the latest in the {CHANNEL_TUI} "
                        "channel, NOT this run's coordinator version"
                    ),
                )
            )

    return rolls


def normalize_version(raw: str | None) -> str | None:
    """``v0.4.111`` / ``0.4.111`` -> ``0.4.111``; empty -> ``None``."""
    if not raw:
        return None
    return str(raw).strip().lstrip("vV") or None


def hosts_already_current(
    lane_versions: Mapping[str, Iterable[str | None]], target: str | None
) -> list[str]:
    """Hosts whose every *known* lane already reports *target*.

    A host with an unreadable lane is deliberately **not** current: #1834's
    rule is that ``version=None`` means "no data", which is emphatically not
    "agrees with everyone else". Skipping such a host would let the lane
    nobody can see be the one that stays behind — the 2026-08-04 shape.
    """
    want = normalize_version(target)
    if not want:
        return []
    current: list[str] = []
    for host, versions in lane_versions.items():
        seen = list(versions)
        if not seen or any(normalize_version(v) != want for v in seen):
            continue
        current.append(host)
    return sorted(current)


# ── the journal ──────────────────────────────────────────────────────────────
#
# #1835's fourth acceptance criterion: "the whole sequence is observable
# after the fact: what was published, when each lane rolled, what
# verification said." An append-only JSONL file, one object per attempt, on
# whichever host runs the propagation timer. Deliberately not a DB table:
# this record must survive a half-installed venv and be readable with `tail`
# while the very upgrade it describes is in flight — which is exactly when
# `coord` itself may not import.


#: Filename under the coord state root (``~/.coord`` on Linux — see
#: :func:`coord.platform_paths.default_coord_dir`).
JOURNAL_NAME = "release_propagation.jsonl"

#: Records kept when the journal is trimmed. Small enough to `cat`, long
#: enough to cover a week of a 15-minute timer's deferrals.
JOURNAL_MAX_RECORDS = 2000


@dataclass
class PropagationRecord:
    """One propagation attempt, start to finish, as journalled."""

    started_at: float
    target_version: str | None = None
    status: str = STATUS_DEFERRED
    quiescence: dict = field(default_factory=dict)
    #: ``[{"lane":..., "host":..., "ok":..., "detail":...}, ...]``, in the
    #: order they actually ran.
    lanes: list[dict] = field(default_factory=list)
    #: What `coord release verify` said, as its own JSON report. The WHOLE
    #: report, always — scoping the gate (below) must never shrink the record.
    verification: dict | None = None
    #: :meth:`GateVerdict.to_dict` — which of those findings this run is
    #: actually accountable for (#2052). This, not ``verification``, is what
    #: ``--rollback-on-red`` acts on.
    gate: dict | None = None
    #: #2101: what this run did to the release-cordon store — which hosts it
    #: cordoned so they would drain, which it uncordoned after rolling, which
    #: cordons had lapsed on their own, and any drain-deadline escalation.
    #: Journalled for the same reason everything else here is: a run that
    #: cordoned the fleet and then died must leave a readable trace of having
    #: done so, or the resulting quiet fleet is indistinguishable from #2082.
    cordons: dict = field(default_factory=dict)
    rolled_back: list[str] = field(default_factory=list)
    released_holds: list[str] = field(default_factory=list)
    #: #2583: the min-releases-behind gate's own readings for this run —
    #: ``min_releases_behind`` is whatever this run resolved (flag > config >
    #: default 1), ``releases_behind`` is the delta it measured, ``None``
    #: when the gate was never evaluated (``min_releases_behind <= 1``, the
    #: default — no second PyPI read is spent on a threshold that would
    #: never hold anything).
    releases_behind: int | None = None
    min_releases_behind: int | None = None
    finished_at: float | None = None
    error: str | None = None
    dry_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.status in (
            STATUS_VERIFIED, STATUS_DEFERRED, STATUS_UP_TO_DATE, STATUS_HOLDING,
        )


def journal_path(state_dir: Path) -> Path:
    return Path(state_dir) / JOURNAL_NAME


def append_record(state_dir: Path, record: PropagationRecord) -> Path:
    """Append *record* as one JSON line. Best effort by contract.

    A propagation must never fail *because* it could not write its own
    diary, but a silently-unwritten diary is the 2026-08-04 shape, so the
    caller is told (by the raised error propagating out of here only for
    genuinely unexpected types) — see the shell, which reports a write
    failure as a warning line and still exits on the real outcome.
    """
    path = journal_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    return path


def read_records(state_dir: Path, *, limit: int | None = None) -> list[dict]:
    """Most-recent-last records from the journal; unparseable lines skipped.

    A torn final line (the process died mid-append) must not make the whole
    history unreadable — the history is most valuable in exactly that case.
    """
    path = journal_path(state_dir)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    if limit is not None and limit >= 0:
        out = out[-limit:]
    return out


def latest_crit_advisories(records: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The newest run's CRIT-severity ``gate.advisory`` findings (#2595).

    `coord release propagate` already computes exactly this — a lane
    :func:`scope_verification` could not roll and is therefore advisory
    rather than blocking — and already prints it with the two commands that
    clear it (#2403's remedy line). #2595's finding is that the print goes
    to stderr on a ``Type=oneshot`` timer and nowhere else: a host can sit
    on a stale, unreachable venv for the length of a whole release cycle
    with this line as the only signal an operator ever got.

    This is the read side that lets any OTHER surface (`coord status`,
    `coord doctor`) show the same findings without re-deriving anything —
    same journal :func:`read_records` already exposes, same records
    `render_record` already renders for `coord release history`. Only the
    NEWEST record's advisories are returned: an advisory the very next run
    resolved must stop showing as current the moment that run lands, the
    same "no cleanup step required" contract :class:`Cordon` uses for its
    own expiry.
    """
    if not records:
        return []
    newest = records[-1]
    gate = newest.get("gate") if isinstance(newest, Mapping) else None
    if not isinstance(gate, Mapping):
        return []
    advisory = gate.get("advisory")
    if not isinstance(advisory, list):
        return []
    return [
        dict(finding)
        for finding in advisory
        if isinstance(finding, Mapping) and str(finding.get("severity") or "") == "crit"
    ]


def trim_journal(state_dir: Path, *, keep: int = JOURNAL_MAX_RECORDS) -> int:
    """Truncate the journal to its last *keep* records. Returns records kept."""
    records = read_records(state_dir)
    if len(records) <= keep:
        return len(records)
    kept = records[-keep:]
    path = journal_path(state_dir)
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in kept), encoding="utf-8"
    )
    return len(kept)


# ── rendering ────────────────────────────────────────────────────────────────


def _stamp(ts: float | None) -> str:
    if not ts:
        return "?"
    import datetime as _dt  # noqa: PLC0415 — leaf import, keeps the module light

    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


_STATUS_MARK = {
    STATUS_VERIFIED: "✓",
    STATUS_UP_TO_DATE: "=",
    STATUS_DEFERRED: "·",
    STATUS_HOLDING: "⊖",
    STATUS_ROLLED: "~",
    STATUS_ROLLED_BACK: "↩",
    STATUS_FAILED: "✗",
}


def render_record(record: PropagationRecord | Mapping[str, Any]) -> list[str]:
    """Human-readable lines for one attempt."""
    data = record.to_dict() if isinstance(record, PropagationRecord) else dict(record)
    status = str(data.get("status") or "?")
    mark = _STATUS_MARK.get(status, "?")
    version = data.get("target_version") or "?"
    prefix = "[dry-run] " if data.get("dry_run") else ""
    lines = [
        # #2898: the header version is the COORDINATOR's channel's target, and
        # says so. Before the split there was only one channel and leaving it
        # unqualified was harmless; now an unlabelled `v0.5.31` above a tui
        # lane reads as a claim about coord-tui, which it is not.
        f"{mark} {prefix}{_stamp(data.get('started_at'))}  "
        f"v{version} ({CHANNEL_COORD})  {status}"
    ]

    # #2583: a held run must read as "deliberately holding at N behind",
    # never as a silent no-op indistinguishable from a dead timer.
    if status == STATUS_HOLDING:
        lines.append(
            f"    holding: {data.get('releases_behind')} behind, "
            f"threshold {data.get('min_releases_behind')}"
        )

    quiescence = data.get("quiescence") or {}
    if quiescence.get("reason"):
        lines.append(f"    window: {quiescence['reason']}")

    # #2898's acceptance criterion: `--dry-run` must name BOTH channels
    # distinctly. Summarised once here (so the two-channel fact survives even
    # a plan whose lanes scroll past) and then per lane below.
    channels = [
        c for c in dict.fromkeys(
            str(lane.get("channel")) for lane in (data.get("lanes") or [])
            if lane.get("channel")
        )
    ]
    if channels:
        lines.append(f"    channels: {', '.join(channels)}")

    for lane in data.get("lanes") or []:
        ok = lane.get("ok")
        lane_mark = "✓" if ok else ("·" if ok is None else "✗")
        detail = lane.get("detail") or ""
        channel = lane.get("channel")
        lines.append(
            f"    {lane_mark} {lane.get('lane')}@{lane.get('host')}"
            + (f" [{channel}]" if channel else "")
            + (f" — {detail}" if detail else "")
        )

    verification = data.get("verification")
    if verification:
        sev = verification.get("severity", "?")
        findings = verification.get("findings") or []
        lines.append(
            f"    verify: {sev} ({len(findings)} finding(s))"
        )
        for finding in findings[:5]:
            lines.append(
                f"      - [{finding.get('severity')}] {finding.get('host')} "
                f"{finding.get('lane')}: {finding.get('summary')}"
            )

    # #2052: the gate's scope is part of the answer, not debug output. A run
    # that was held to lanes it could not roll is exactly the defect this
    # block exists to make visible the next time it happens.
    gate = data.get("gate")
    if gate:
        blocking = gate.get("blocking") or []
        advisory = gate.get("advisory") or []
        lines.append(
            f"    gate: {gate.get('severity', '?')} "
            f"({len(blocking)} blocking, {len(advisory)} advisory — "
            "advisory lanes are ones propagation cannot roll)"
        )
        for finding in advisory[:5]:
            lines.append(
                f"      ~ advisory [{finding.get('severity')}] "
                f"{finding.get('host')} {finding.get('lane')}: "
                f"{finding.get('summary')}"
            )
        if gate.get("unrollable"):
            lines.append(
                "      ~ no channel from here: "
                + ", ".join(gate["unrollable"])
            )

    # #2101: the cordon is the thing that CREATED this run's window (or is
    # still creating it), so it belongs in the record's headline lines, not
    # only in the JSON. A deferral that also cordoned reads completely
    # differently from one that just gave up.
    cordons = data.get("cordons") or {}
    if cordons.get("cordoned"):
        lines.append(
            "    cordoned (draining to roll): " + ", ".join(cordons["cordoned"])
        )
    if cordons.get("uncordoned"):
        lines.append("    uncordoned: " + ", ".join(cordons["uncordoned"]))
    # #2176: name the collateral hosts AND the single host they're collateral
    # to — the fact that would have turned a 40-minute puzzle into one line.
    if cordons.get("collateral_spared"):
        lines.append(
            "    spared (blocked behind daemon host "
            f"{cordons.get('blocked_behind')} anyway): "
            + ", ".join(cordons["collateral_spared"])
        )
    if cordons.get("expired"):
        lines.append(
            "    cordons that lapsed on their own: " + ", ".join(cordons["expired"])
        )
    # #2240: the deadlock break is the single most important thing a history
    # read can show — it means the fleet had been unable to work, not that it
    # was upgrading. Never collapsed into the "N no-op attempts" summary,
    # because `render_history` only collapses records it renders as a run and
    # this line belongs to one it prints in full.
    released = cordons.get("released")
    if released:
        lines.append(f"    ! {released.get('message') or released}")
    if cordons.get("cooling_seconds"):
        lines.append(
            "    cordons held off (post-release cooldown, #2240): "
            f"{float(cordons['cooling_seconds']) / 60.0:.0f}m left"
        )
    # #2490: name the hosts that were behind and idle but left uncordoned
    # purely because of the cooldown above — the gap that let `precision`
    # sit stuck for 30 minutes with no automatic path back to rolling.
    if cordons.get("stuck_in_cooldown"):
        lines.append(
            "    ! STUCK (idle, behind, cooldown-suppressed, #2490): "
            + ", ".join(cordons["stuck_in_cooldown"])
        )
    for esc in cordons.get("escalated") or []:
        lines.append(f"    ! {esc.get('message') or esc}")
    for err in cordons.get("errors") or []:
        lines.append(f"    cordon error: {err}")

    if data.get("rolled_back"):
        lines.append(f"    rolled back: {', '.join(data['rolled_back'])}")
    if data.get("released_holds"):
        lines.append(f"    released deploy gates: {', '.join(data['released_holds'])}")
    if data.get("error"):
        lines.append(f"    error: {data['error']}")
    return lines


def render_history(records: Sequence[Mapping[str, Any]], *, verbose: bool = False) -> str:
    """The `coord release history` body.

    Without *verbose*, consecutive no-op attempts (deferred / already
    up-to-date) collapse to one summary line — a 15-minute timer produces
    ~96 of those a day and a history nobody can skim is a history nobody
    reads. The count is always printed: #1835's "a silent success is
    indistinguishable from a silent no-op" cuts both ways, so the no-ops are
    summarised, never dropped.
    """
    if not records:
        return (
            "no propagation attempts recorded yet — if the timer is supposed "
            "to be running, that is itself the finding (see `systemctl --user "
            "status coord-release-propagate.timer`)"
        )
    lines: list[str] = []
    run: list[Mapping[str, Any]] = []

    def _flush() -> None:
        if not run:
            return
        first, last = run[0], run[-1]
        if len(run) == 1:
            lines.extend(render_record(first))
        else:
            lines.append(
                f"· {_stamp(first.get('started_at'))} .. "
                f"{_stamp(last.get('started_at'))}  "
                f"{len(run)} no-op attempt(s) "
                f"(last: {last.get('status')} — "
                f"{(last.get('quiescence') or {}).get('reason', '?')})"
            )
        run.clear()

    for record in records:
        if not verbose and str(record.get("status")) in NO_OP_STATUSES:
            run.append(record)
            continue
        _flush()
        lines.extend(render_record(record))
    _flush()
    return "\n".join(lines)
