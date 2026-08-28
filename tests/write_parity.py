"""#2885: the write-path parity oracle — a differential harness over two backends.

Why this exists
---------------
``tests/test_store_contract.py`` (#1942) is the ``CoordStore`` contract suite
and, by its own docstring, covers **the read surface only**.  Measured on
2026-08-28 the write side had no oracle at all: 313 ``sql.*`` call sites in
``coord/``, 12 of them behind ``CoordStore``, 301 raw statements going straight
through the #1948 statement seam — and the seam makes the SQL *run* on both
backends without making it *mean the same thing*:

- ``INSERT OR REPLACE`` → ``ON CONFLICT DO UPDATE`` was a semantic change at
  each of 37 sites (DELETE+INSERT fires ``ON DELETE`` cascades and resets
  unmentioned columns to defaults; ``DO UPDATE`` does neither).  Every site
  recorded a *judgement* at the call site about whether that mattered.  A wrong
  judgement is invisible on SQLite and shows up only as drifted data.
- Constraint enforcement timing differs: SQLite enforces foreign keys only when
  ``PRAGMA foreign_keys=ON`` is set on the *writing* connection; Postgres always
  enforces.
- ``lastrowid`` → ``RETURNING`` (``sql.insert_returning_id``) is equivalent only
  if every caller reads it the same way.

None of that is caught by "the suite passed on SQLite".

Option B, and why
-----------------
#2885 offered two shapes.  **A** — grow ``CoordStore`` a write surface and
extend the contract suite — is the better long-term artifact but is a semantic
refactor of ``coord/state.py``'s 60 ``_*_local()`` functions, and #1948 already
adjudicated that refactor's *portability* value as zero, so it would have to be
carried on testability alone.  **B** — replay a recorded workload against both
backends and diff the resulting state — is what #828/#829 actually need on
cutover day: it covers **all** the write sites the workload touches rather than
a chosen subset, needs no production change at all, and can ship now.

This module is B.  The trade B makes is stated honestly: it proves the backends
agree **on the workload recorded here**, not that they agree in general.  That
is why :data:`WORKLOAD` is a first-class, extendable list and why
``tests/test_write_parity.py`` asserts mechanically that it still covers every
table ``/board`` projects — the corpus is the thing that has to grow, and the
suite fails when it stops being enough.

How it works
------------
1. :func:`run_workload` points ``coord.db``'s connection singleton at a given
   database, freezes the clock, and executes :data:`WORKLOAD` — the *real*
   ``coord.state`` / ``coord.merge_queue`` / ``coord.commands`` write paths, not
   a re-implementation of them.
2. :func:`dump_database` reads **every** table back out, discovering the table
   list and each table's primary key from the live schema (so a new table in
   ``coord/db.py`` joins the comparison automatically), and canonicalises the
   values.
3. :func:`compare_dumps` matches rows by primary key and returns a
   :class:`ParityReport` — a list of :class:`Difference` records, each naming a
   table, a row key, a column and both values.  **A diff, not a boolean**: the
   whole point is that on cutover day #829 reads the report and classifies the
   entries, and "False" would tell it nothing.

An oracle that has never failed is not an oracle (#2096), so
``tests/test_write_parity.py`` injects real divergences — chiefly reverting
``coord.state._UPSERT_SQL`` to the pre-#2726 ``INSERT OR REPLACE`` semantics —
and asserts this harness catches each one, naming the right table and column.

Running it by hand
------------------
::

    .venv/bin/python -m tests.write_parity                  # sqlite vs sqlite
    .venv/bin/python -m tests.write_parity --b postgres     # the cutover check

Exit status is 0 for a clean report and 1 when differences were found; the
report itself goes to stdout either way.

What is deliberately tolerated
------------------------------
Two normalisations, and no more (see :func:`_canonical_value` /
:func:`_rank_surrogate_keys`):

- **Auto-increment key values** are replaced by their rank within the table.  A
  Postgres identity sequence is not rolled back by a failed statement while a
  SQLite ROWID is, so absolute values can legitimately differ; *relative order*
  is what call sites actually depend on, and rank preserves it exactly.
- **``Decimal`` is coerced to ``float``.**  Purely a driver representation
  choice (psycopg decodes ``NUMERIC`` to ``Decimal``), not a stored-value
  difference.

Everything else is compared as-is on purpose.  In particular floats are **not**
rounded: SQLite's ``REAL`` is 8-byte and Postgres's ``REAL`` is 4-byte, so a
``cost_usd`` that comes back ``0.41999998688697815`` on one side is exactly the
kind of silent write-path divergence this harness exists to surface, and
rounding it away would make the oracle lie.

Comparison is also **type-aware** (:func:`values_differ`), because Python's
``1 == True`` and ``1 == 1.0`` would otherwise silently tolerate two of the
divergences that matter most: an ``INTEGER``-backed flag column arriving as a
Python ``bool`` ships as JSON ``true`` and fails the parse of the whole
``BoardPayload`` (#632/#546/#628), and a JSON ``1`` and a JSON ``1.0``
deserialise differently into a typed Rust struct.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from typing import Any

from coord import sql

# ── the deterministic clock ──────────────────────────────────────────────────


class StepClock:
    """A ``time.time`` stand-in that only moves when the harness says so.

    Both runs execute the same workload steps, so pinning the clock per *step*
    (rather than per *call*) makes every timestamp column byte-identical across
    backends without any timestamp normalisation in the diff — which keeps
    ``finished_at``/``synced_at``/``posted_at`` fully comparable instead of
    blanked.

    Per-step rather than per-call is the important part: a per-call counter
    would drift the moment one backend took a different number of ``time.time()``
    calls (a retry in ``retry_on_locked``, a branch that only fires on one
    dialect), and every downstream timestamp would then differ for a reason that
    has nothing to do with the write path.
    """

    #: An arbitrary fixed epoch, matching the #748 golden fixture's era so a
    #: dump from this harness reads like the fixture data it sits next to.
    BASE = 1_000_000_000.0

    def __init__(self) -> None:
        self.step = 0

    def advance(self) -> None:
        self.step += 1

    def __call__(self) -> float:
        return self.BASE + float(self.step)


class _ClockShim:
    """Stands in for a module's ``time`` import, overriding only ``time()``."""

    def __init__(self, real: Any, clock: StepClock) -> None:
        self._real = real
        self._clock = clock

    def time(self) -> float:
        return self._clock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


#: Modules whose ``import time`` the harness pins.  Every module reached by a
#: :data:`WORKLOAD` step that stamps a timestamp column must be here, or that
#: column becomes wall-clock and every run diverges from every other run —
#: which the sqlite-vs-sqlite self-consistency test in
#: ``tests/test_write_parity.py`` catches immediately.
_CLOCKED_MODULES: tuple[str, ...] = (
    "coord.state",
    "coord.audit",
    "coord.issue_store",
    "coord.merge_queue",
)


@contextlib.contextmanager
def _frozen_clock(clock: StepClock) -> Iterator[None]:
    import importlib

    originals: list[tuple[Any, Any]] = []
    try:
        for name in _CLOCKED_MODULES:
            mod = importlib.import_module(name)
            originals.append((mod, mod.time))
            mod.time = _ClockShim(mod.time, clock)  # type: ignore[attr-defined]
        yield
    finally:
        for mod, real in originals:
            mod.time = real


# ── the workload ─────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class WorkloadStep:
    """One named write operation in the recorded corpus.

    ``tables`` is what the step is *claimed* to write.  It is not used to run
    anything — it is what ``tests/test_write_parity.py`` checks the workload's
    ``/board`` coverage against, and what the report cites when a step is the
    obvious suspect for a table's divergence.
    """

    name: str
    tables: tuple[str, ...]
    run: Callable[[], None]


REPO = "api"
REPO_GITHUB = "acme/api"
MACHINE = "laptop"
WORK_ID = "work-2885a"
REVIEW_ID = "rev-2885a"


def _config() -> Any:
    """The fixture ``coordinator.yml`` as a parsed :class:`~coord.config.Config`.

    Reuses ``tests.conftest.VALID_CONFIG`` (the ``api``/``shared`` +
    ``laptop``/``server`` topology the rest of the suite already uses) through
    ``config.parse_mapping``, so this harness never grows a second, drifting
    hand-built Config the way #1538 warns about.
    """
    import yaml

    from coord import config as config_mod
    from tests.conftest import VALID_CONFIG

    return config_mod.parse_mapping(yaml.safe_load(VALID_CONFIG))


def _proposal(**overrides: Any) -> Any:
    from coord.models import Proposal

    fields: dict[str, Any] = {
        "id": 1,
        "machine_name": MACHINE,
        "repo_name": REPO,
        "issue_number": 2885,
        "issue_title": "a write-path parity oracle",
        "rationale": "laptop is idle and has touched coord/sql.py recently",
        "files_likely": ["tests/write_parity.py"],
        "briefing": "Build the differential harness.",
        "model": "sonnet",
        "type": "work",
        "required_gates": ["test", "review", "merge"],
        "driven_by": "drive-2885",
    }
    fields.update(overrides)
    return Proposal(**fields)


def _assignment(**overrides: Any) -> Any:
    from coord.models import Assignment

    fields: dict[str, Any] = {
        "machine_name": MACHINE,
        "repo_name": REPO,
        "issue_number": 2885,
        "issue_title": "a write-path parity oracle",
        "assignment_id": WORK_ID,
        "status": "running",
        "branch": "issue-2885-write-path-parity",
        "type": "work",
        "required_gates": ["test", "review", "merge"],
    }
    fields.update(overrides)
    return Assignment(**fields)


# Each ``_step_*`` below drives a REAL write path.  None of them re-implements
# SQL: that is the whole point — the harness has to exercise the statements
# production actually issues, including their ON CONFLICT clauses.


def _step_snapshot_config() -> None:
    """``machines`` (DELETE + INSERT) and eight ``board_meta`` upserts.

    ``allow_thin_client=False`` is the #2824 flag ``coord serve``'s own
    bootstrap passes — "I am the host that owns this database, do not skip the
    write because a ``client.toml`` happens to exist here".  Without it the
    whole step silently returns on any machine configured as a thin client, and
    the harness would compare two empty ``machines`` tables and call it parity.
    """
    from coord.commands._common import _save_config_snapshot

    _save_config_snapshot(_config(), allow_thin_client=False)


def _step_sync_open_issues() -> None:
    """``issues`` — the close-sweep UPDATE, the 7-day prune DELETE, and the
    ``ON CONFLICT (repo_name, number) DO UPDATE`` upsert, plus the
    ``issue_context`` cleanup DELETE that depends on the open set."""
    from coord import state

    state._upsert_open_issues_local(
        REPO,
        [
            {
                "number": 2885,
                "title": "a write-path parity oracle",
                "body": "The contract suite covers 12 of 313 SQL sites, all reads.",
                "labels": [{"name": "coord"}, {"name": "status:ready"}],
                "milestone": {"number": 60, "title": "Store Service"},
            },
            {
                "number": 2884,
                "title": "test-harness portability",
                "body": "Point the whole suite at a second database.",
                "labels": [{"name": "coord"}],
                "milestone": None,
            },
        ],
    )


def _step_resync_open_issues() -> None:
    """The same sync again with one issue dropped and one edited — the second
    pass is where ``ON CONFLICT ... DO UPDATE`` actually fires, and where the
    close-sweep has a row to flip."""
    from coord import state

    state._upsert_open_issues_local(
        REPO,
        [
            {
                "number": 2885,
                "title": "a write-path parity oracle (edited)",
                "body": "Second sync: the upsert's DO UPDATE branch.",
                "labels": [{"name": "coord"}],
                "milestone": {"number": 60, "title": "Store Service"},
            },
        ],
    )


def _step_relabel_issue() -> None:
    """``issues.labels`` through the cache-mirror UPDATE, under ``retry_on_locked``.

    ``_update_issue_labels_local`` rather than its ``_apply_issue_labels_local``
    caller on purpose: the latter calls GitHub first, and this harness must
    never need the network (nor ``gh``, which is on the worker deny-list).  The
    write path — the ``UPDATE issues SET labels`` inside ``retry_on_locked``,
    and the ``cursor.rowcount`` read that decides the return value — is the
    same one, and it is the part that can differ per backend.
    """
    from coord import state

    state._update_issue_labels_local(REPO, 2885, ["coord", "status:in-progress"])
    # A key that does not exist: the rowcount==0 path, where SQLite and
    # Postgres each have to report "nothing matched" the same way.
    state._update_issue_labels_local(REPO, 404, ["coord"])


def _step_save_proposals() -> None:
    """``proposals`` — DELETE-all + re-INSERT with an explicit ``id``."""
    from coord import state

    state.save_proposals(
        [
            _proposal(),
            _proposal(
                id=2,
                issue_number=2884,
                issue_title="test-harness portability",
                files_likely=[],
                required_gates=["review"],
                driven_by=None,
            ),
        ]
    )


def _step_dispatch_work() -> None:
    """``assignments`` (``ON CONFLICT DO NOTHING`` INSERT) + an ``audit_log``
    row written only when ``cur.rowcount > 0`` — a rowcount reading that is
    itself a per-driver behaviour worth diffing."""
    from coord import state

    state._record_dispatched_local(
        assignment_id=WORK_ID,
        proposal=_proposal(),
        repo_github=REPO_GITHUB,
        provider_name="claude",
    )


def _step_redispatch_is_a_noop() -> None:
    """The same dispatch again: ``DO NOTHING`` must leave the row untouched and
    write no second audit row."""
    from coord import state

    state._record_dispatched_local(
        assignment_id=WORK_ID,
        proposal=_proposal(issue_title="a DIFFERENT title that must not land"),
        repo_github="acme/wrong",
        provider_name="codex",
    )


def _step_record_test_verdict() -> None:
    """``assignments.test_state``/``test_reason``/``smoke_test`` via the
    single-row seam writer — the columns ``_UPSERT_SQL`` deliberately excludes
    from its ``ON CONFLICT`` clause (#1482)."""
    from coord import state

    state._record_test_verdict_local(
        assignment_id=WORK_ID,
        test_state="passed",
        test_reason="pytest tests/test_write_parity.py",
        smoke_test="pass",
        smoke_test_reason="harness reports a diff",
    )


def _step_save_board() -> None:
    """``assignments`` through ``_UPSERT_SQL`` + two ``board_meta`` upserts.

    This is the richest write path in the tree: a 41-column upsert whose
    ``DO UPDATE`` clause is a pile of ``CASE``/``COALESCE`` guards (#1451 status
    CAS, #1482 test-state exclusion, #1565 review-state guard, #1456/#821/#1475
    preserve-once-set).  Every one of those is a *semantic* claim that
    ``INSERT OR REPLACE`` did not make.
    """
    from coord import state
    from coord.models import Board

    work = _assignment(
        status="done",
        finished_at=StepClock.BASE + 100.0,
        dispatched_at=StepClock.BASE + 1.0,
        cost_usd=0.42,
        review_state="pending",
        smoke_tests=["fixture loads", "report renders"],
    )
    review = _assignment(
        assignment_id=REVIEW_ID,
        type="review",
        status="done",
        branch=None,
        review_of_assignment_id=WORK_ID,
        review_verdict="approve",
        review_head_sha="0123456789abcdef",
        dispatched_at=StepClock.BASE + 2.0,
        finished_at=StepClock.BASE + 120.0,
    )
    state.save_board(Board(active=[], completed=[work, review], round_number=3))


def _step_save_stale_board() -> None:
    """A deliberately STALE whole-board snapshot.

    The board carries the row as it looked before :func:`_step_record_test_verdict`
    and :func:`_step_save_board` ran — exactly the #1451/#1482/#1565 shape.  A
    correct ``ON CONFLICT DO UPDATE`` preserves ``status='done'``,
    ``test_state='passed'`` and ``review_verdict='approve'``; a DELETE+INSERT
    resets all three (and ``repo_github``/``briefing``/``driven_by``, which the
    upsert's INSERT column list does not even mention).  This step is what makes
    the falsifiability demo in ``tests/test_write_parity.py`` bite.
    """
    from coord import state
    from coord.models import Board

    stale = _assignment(status="running", finished_at=None, review_state=None)
    state.save_board(Board(active=[stale], completed=[], round_number=3))


def _step_mark_notified() -> None:
    """``notifications`` (``ON CONFLICT(assignment_id) DO UPDATE``) plus the
    ``assignments`` sync-back the same function performs.

    The event names come from ``coord.comments`` rather than string literals:
    ``_mark_notified_local`` branches on them, and an unrecognised value falls
    into the bare ``else`` that stamps ``status='failed'`` — which would still
    be *identical* on both backends (so the harness would stay green) while
    quietly recording a workload that exercises the wrong branch.
    """
    from coord import state
    from coord.comments import EVENT_COMPLETION, EVENT_FAILURE

    state._mark_notified_local(
        WORK_ID, EVENT_COMPLETION, branch="issue-2885-write-path-parity"
    )
    state._mark_notified_local(
        REVIEW_ID, EVENT_FAILURE, failure_reason="reviewer timed out", exit_code=124
    )
    # Second write to the same key: the DO UPDATE branch.
    state._mark_notified_local(
        WORK_ID, EVENT_COMPLETION, branch="issue-2885-write-path-parity"
    )


def _step_save_plan() -> None:
    """``plans`` — ``ON CONFLICT(assignment_id) DO UPDATE`` (#2726)."""
    from coord import state

    state.save_plan(WORK_ID, {"steps": ["dump", "diff", "report"]})
    state.save_plan(WORK_ID, {"steps": ["dump", "diff", "report", "argue A vs B"]})


def _step_save_merge_queue() -> None:
    """``merge_queue`` — DELETE-all + re-INSERT under ``retry_on_locked``.

    Run twice so the auto-increment key is exercised *after* a delete, which is
    where SQLite's ``AUTOINCREMENT`` and Postgres's identity sequence are most
    likely to disagree on absolute values (and where the harness's rank
    normalisation earns its place).
    """
    from coord import merge_queue

    items = [
        merge_queue.QueuedMerge(
            assignment_id=WORK_ID,
            repo_name=REPO,
            repo_github=REPO_GITHUB,
            branch="issue-2885-write-path-parity",
            target_branch="main",
            issue_number=2885,
            issue_title="a write-path parity oracle",
            state="queued",
            pr_number=9001,
            pr_url="https://github.com/acme/api/pull/9001",
            size=240,
            enqueued_at=StepClock.BASE + 200.0,
            required_gates=["test", "review", "merge"],
        ),
        merge_queue.QueuedMerge(
            assignment_id=REVIEW_ID,
            repo_name=REPO,
            repo_github=REPO_GITHUB,
            branch="issue-2884-harness-portability",
            target_branch="main",
            issue_number=2884,
            issue_title="test-harness portability",
            state="queued",
            pr_number=9002,
            pr_url="https://github.com/acme/api/pull/9002",
            size=17,
            enqueued_at=StepClock.BASE + 210.0,
        ),
    ]
    merge_queue.save_queue(items)
    merge_queue.save_queue(items[:1])


def _step_enqueue_drive_queue() -> None:
    """``drive_queue`` — ``sql.insert_returning_id`` (``lastrowid`` on SQLite,
    ``RETURNING`` on Postgres), then the by-key UPDATE branch of the same
    function on a repeat enqueue."""
    from coord import state

    state._enqueue_drive_queue_local(REPO, 2885, machine=MACHINE)
    state._enqueue_drive_queue_local(REPO, 2884, after=[f"{REPO}#2885"], hold_after=True)
    # Same key again: the "already queued, update in place" branch.
    state._enqueue_drive_queue_local(REPO, 2885, machine="server", no_acceptance=True)


def _step_update_drive_queue() -> None:
    """``drive_queue`` — the dynamic ``UPDATE ... SET`` builder."""
    from coord import state

    state._update_drive_queue_entry_local(
        REPO, 2885, state="running", attempts=1, last_reason="dispatched"
    )


def _step_dequeue_drive_queue() -> None:
    """``drive_queue`` DELETE — the row must go, and the surviving row's
    ``position`` must be whatever the real path leaves behind."""
    from coord import state

    state._dequeue_drive_queue_local(REPO, 2884)


def _step_record_escalations() -> None:
    """``drive_escalations`` — ``ON CONFLICT(repo_name, issue_number) DO UPDATE``."""
    from coord import state

    state._record_drive_escalation_local(
        REPO,
        2885,
        stage="merge",
        reason="NEEDS_ATTENTION",
        gate_readings="ci=failing",
        proposed_command="coord merge --repo api",
        assignment_id=WORK_ID,
    )
    state._record_drive_escalation_local(
        REPO,
        2884,
        stage="review",
        reason="NO_REVIEWER",
        gate_readings="review=pending",
        proposed_command="coord plan",
    )
    # Same key again: the DO UPDATE branch.
    state._record_drive_escalation_local(
        REPO,
        2885,
        stage="merge",
        reason="CI_RED",
        gate_readings="ci=failing,checks=1",
        proposed_command="coord merge --repo api --force-merge",
        assignment_id=WORK_ID,
    )


def _step_dismiss_escalation() -> None:
    """``drive_escalations`` DELETE."""
    from coord import state

    state._dismiss_drive_escalation_local(REPO, 2884)


#: The recorded corpus, in order.  **Append here to widen coverage** — that is
#: the maintenance story for Option B, and ``tests/test_write_parity.py``
#: asserts the corpus still reaches every ``/board``-projected table.
WORKLOAD: tuple[WorkloadStep, ...] = (
    WorkloadStep("snapshot_config", ("machines", "board_meta"), _step_snapshot_config),
    WorkloadStep("sync_open_issues", ("issues",), _step_sync_open_issues),
    WorkloadStep("relabel_issue", ("issues",), _step_relabel_issue),
    WorkloadStep("save_proposals", ("proposals",), _step_save_proposals),
    WorkloadStep("dispatch_work", ("assignments", "audit_log"), _step_dispatch_work),
    WorkloadStep(
        "redispatch_is_a_noop", ("assignments", "audit_log"), _step_redispatch_is_a_noop
    ),
    WorkloadStep("record_test_verdict", ("assignments",), _step_record_test_verdict),
    WorkloadStep("save_board", ("assignments", "board_meta"), _step_save_board),
    WorkloadStep("mark_notified", ("notifications", "assignments"), _step_mark_notified),
    WorkloadStep("save_plan", ("plans",), _step_save_plan),
    WorkloadStep("save_merge_queue", ("merge_queue",), _step_save_merge_queue),
    WorkloadStep("enqueue_drive_queue", ("drive_queue",), _step_enqueue_drive_queue),
    WorkloadStep("update_drive_queue", ("drive_queue",), _step_update_drive_queue),
    WorkloadStep("dequeue_drive_queue", ("drive_queue",), _step_dequeue_drive_queue),
    WorkloadStep(
        "record_escalations", ("drive_escalations",), _step_record_escalations
    ),
    WorkloadStep(
        "dismiss_escalation", ("drive_escalations",), _step_dismiss_escalation
    ),
    WorkloadStep("resync_open_issues", ("issues",), _step_resync_open_issues),
    WorkloadStep("save_stale_board", ("assignments",), _step_save_stale_board),
)


class WorkloadStepError(RuntimeError):
    """A workload step raised on one backend.

    Deliberately fatal rather than swallowed: a step that blew up on Postgres
    and was quietly skipped would produce an *empty* table on that side, which
    reads in the report like a write-path divergence when it is really a crash.
    The harness would still flag it, but it would flag it as the wrong thing.
    """


def run_workload(
    conn: Any,
    *,
    workload: Sequence[WorkloadStep] = WORKLOAD,
) -> None:
    """Execute *workload* against *conn*.

    Installs *conn* as ``coord.db``'s connection singleton for the duration (the
    same seam ``tests/conftest.py``'s autouse ``coord_db`` fixture uses) and
    restores whatever was there before, so a caller running two backends
    back-to-back inside one test leaves the fixture's own connection intact.

    Two ``coord.state`` seams are neutralised for the run, for the same reason
    and no others.  Both are *environment* questions that issue no SQL, so
    stubbing them removes nothing this harness measures — while leaving them
    live would couple the oracle to whatever happens to be installed on the
    machine running it, which is exactly what ``tests/conftest.py``'s
    ``_no_dispatch_target_validation`` / ``_no_board_service`` autouse fixtures
    already prevent for the rest of the suite:

    - ``_dispatch_target_config`` — the #2087 dispatch-target gate, which reads
      the real ``~/.coord/coordinator.yml`` to decide whether a repo/machine is
      allowed.
    - ``_board_service`` — the #615 thin-client guard, which warns (or, under
      ``COORD_STRICT_LOCAL_BOARD``, raises) when a local board write happens on
      a host with a ``client.toml`` or ``$COORD_SERVICE_URL``.  The harness is
      *deliberately* writing a local board; on a thin client that guard would
      turn every ``save_board``/``save_plan`` step into noise or, in strict
      mode, into a crash that has nothing to do with either backend.
    """
    from coord import db, state

    previous_conn = db._conn
    previous_gate = state._dispatch_target_config
    previous_service = state._board_service
    clock = StepClock()
    try:
        db.override_connection(conn)
        state._dispatch_target_config = lambda: None  # type: ignore[assignment]
        state._board_service = lambda: None  # type: ignore[assignment]
        with _frozen_clock(clock):
            for step in workload:
                clock.advance()
                try:
                    step.run()
                except Exception as exc:  # noqa: BLE001 -- re-raised with the step name
                    raise WorkloadStepError(
                        f"workload step {step.name!r} failed: {type(exc).__name__}: {exc}"
                    ) from exc
    finally:
        state._board_service = previous_service  # type: ignore[assignment]
        state._dispatch_target_config = previous_gate  # type: ignore[assignment]
        db.override_connection(previous_conn)


# ── the canonical dump ───────────────────────────────────────────────────────


def table_names(conn: Any) -> tuple[str, ...]:
    """Every user table on *conn*, sorted.

    Discovered from the live database rather than from a hand-maintained list,
    so a table added to ``coord/db.py``'s schema joins the comparison with no
    edit here — the same "append one entry, no new test code" property
    ``tests/test_store_contract.py``'s ``BACKENDS`` has, applied to tables.

    The dialect branch lives here rather than in ``coord/sql.py`` on purpose:
    production has no reason to enumerate its own tables, and a seam function
    with exactly one caller — a test harness — would be dead weight in the
    shipped module.  ``sql.detect_dialect`` is still what decides, so this stays
    consistent with the rest of the seam and fails the same way on an unknown
    connection.
    """
    dialect = sql.detect_dialect(conn)
    if dialect == sql.DIALECT_SQLITE:
        rows = sql.execute(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'",
        ).fetchall()
    elif dialect == sql.DIALECT_POSTGRES:
        rows = sql.execute(
            conn,
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'",
        ).fetchall()
    else:  # pragma: no cover -- detect_dialect already raised
        raise sql.UnsupportedDialectError(dialect)
    return tuple(sorted(_cell(row, "name", 0) for row in rows))


def primary_key(conn: Any, table: str) -> tuple[str, ...]:
    """*table*'s primary-key columns, in key order (empty when it has none).

    Row matching in :func:`compare_dumps` is by primary key, so the report can
    say "``assignments['work-2885a'].repo_github`` differs" instead of "row 3
    differs".  Derived from the schema for the same reason as
    :func:`table_names`: a hand-maintained key map is one more thing to drift.
    """
    dialect = sql.detect_dialect(conn)
    if dialect == sql.DIALECT_SQLITE:
        rows = sql.execute(conn, f"PRAGMA table_info({table})").fetchall()  # noqa: S608
        keyed = [(int(row[5]), row[1]) for row in rows if int(row[5]) > 0]
        return tuple(name for _, name in sorted(keyed))
    if dialect == sql.DIALECT_POSTGRES:
        rows = sql.execute(
            conn,
            "SELECT kcu.column_name AS name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            " AND tc.table_schema = kcu.table_schema "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            "  AND tc.table_schema = current_schema() AND tc.table_name = ? "
            "ORDER BY kcu.ordinal_position",
            (table,),
        ).fetchall()
        return tuple(_cell(row, "name", 0) for row in rows)
    raise sql.UnsupportedDialectError(dialect)  # pragma: no cover


def _cell(row: Any, name: str, index: int) -> Any:
    """Read a column off a row that may be a tuple or a mapping.

    ``sql.apply_row_factory`` may or may not have run on the connection handed
    to this harness, and psycopg's ``dict_row`` and ``sqlite3.Row`` disagree
    about which access works — the same fallback shape ``sql.table_columns``
    already uses.
    """
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        return row[index]


def _canonical_value(value: Any) -> Any:
    """Normalise one cell to a comparable, JSON-renderable form.

    See the module docstring for what is deliberately *not* normalised here.
    """
    if isinstance(value, Decimal):
        # Driver representation, not stored value: psycopg decodes NUMERIC to
        # Decimal where sqlite3 hands back a float for the same number.
        return float(value)
    if isinstance(value, memoryview):
        return ("bytes", value.tobytes().hex())
    if isinstance(value, (bytes, bytearray)):
        return ("bytes", bytes(value).hex())
    if isinstance(value, (list, dict)):
        # A json/jsonb column decoded by the driver.  coord/db.py stores JSON in
        # TEXT columns today, so this is defensive — but a future jsonb column
        # must not make the dump unhashable/unsortable.
        return json.dumps(value, sort_keys=True)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _rank_surrogate_keys(
    rows: list[dict[str, Any]], key_columns: Sequence[str]
) -> list[dict[str, Any]]:
    """Replace a single-column integer primary key with its ``#rank``.

    Only fires for a one-column integer key — i.e. the ``id`` surrogate on
    ``merge_queue``/``audit_log``/``drive_queue``/…, never a natural key like
    ``issues(repo_name, number)`` or ``plans(assignment_id)``, whose values are
    genuine data and must be compared exactly.

    See the module docstring: absolute sequence values can legitimately differ
    between backends (a Postgres identity sequence is not rolled back by a
    failed statement), while *relative order* is what call sites depend on and
    is preserved exactly by rank.
    """
    if len(key_columns) != 1:
        return rows
    column = key_columns[0]
    values = [row.get(column) for row in rows]
    if not values or not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return rows
    ranks = {v: i + 1 for i, v in enumerate(sorted(values))}  # type: ignore[arg-type]
    return [{**row, column: f"#{ranks[row[column]]}"} for row in rows]


@dataclasses.dataclass(frozen=True)
class Dump:
    """A whole database, canonicalised for comparison."""

    label: str
    #: table → column names, in schema order
    columns: dict[str, tuple[str, ...]]
    #: table → primary-key column names (possibly empty)
    keys: dict[str, tuple[str, ...]]
    #: table → rows, each a ``{column: canonical value}`` mapping
    rows: dict[str, list[dict[str, Any]]]

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(sorted(self.rows))

    def row_count(self, table: str) -> int:
        return len(self.rows.get(table, []))


def dump_database(conn: Any, *, label: str) -> Dump:
    """Read every table on *conn* back out in canonical form."""
    columns: dict[str, tuple[str, ...]] = {}
    keys: dict[str, tuple[str, ...]] = {}
    rows: dict[str, list[dict[str, Any]]] = {}
    for table in table_names(conn):
        table_cols = tuple(name for name, _type in sql.table_columns(conn, table))
        columns[table] = table_cols
        key_columns = primary_key(conn, table)
        keys[table] = key_columns
        fetched = sql.execute(conn, f"SELECT * FROM {table}").fetchall()  # noqa: S608
        table_rows = [
            {
                name: _canonical_value(_cell(row, name, index))
                for index, name in enumerate(table_cols)
            }
            for row in fetched
        ]
        rows[table] = _rank_surrogate_keys(table_rows, key_columns)
    return Dump(label=label, columns=columns, keys=keys, rows=rows)


# ── the diff ─────────────────────────────────────────────────────────────────

KIND_TABLE_ONLY_IN_A = "table-only-in-a"
KIND_TABLE_ONLY_IN_B = "table-only-in-b"
KIND_COLUMNS = "columns"
KIND_ROW_ONLY_IN_A = "row-only-in-a"
KIND_ROW_ONLY_IN_B = "row-only-in-b"
KIND_CELL = "cell"


@dataclasses.dataclass(frozen=True)
class Difference:
    """One concrete disagreement between two dumps."""

    table: str
    kind: str
    key: str
    column: str | None = None
    value_a: Any = None
    value_b: Any = None

    def render(self, label_a: str, label_b: str) -> str:
        if self.kind == KIND_TABLE_ONLY_IN_A:
            return f"  {self.table}: present in {label_a}, absent in {label_b}"
        if self.kind == KIND_TABLE_ONLY_IN_B:
            return f"  {self.table}: absent in {label_a}, present in {label_b}"
        if self.kind == KIND_COLUMNS:
            return (
                f"  {self.table}: column list differs\n"
                f"      {label_a}: {self.value_a}\n"
                f"      {label_b}: {self.value_b}"
            )
        if self.kind == KIND_ROW_ONLY_IN_A:
            return f"  {self.table}[{self.key}]: only in {label_a} -> {self.value_a}"
        if self.kind == KIND_ROW_ONLY_IN_B:
            return f"  {self.table}[{self.key}]: only in {label_b} -> {self.value_b}"
        return (
            f"  {self.table}[{self.key}].{self.column}: "
            f"{label_a}={self.value_a!r} {label_b}={self.value_b!r}"
        )


@dataclasses.dataclass(frozen=True)
class ParityReport:
    """The harness's output: **a diff, not a boolean**.

    ``bool(report)`` is deliberately *not* defined.  #2885's acceptance is that
    the mechanism "reports a diff, not a boolean", and a truthiness protocol is
    how a caller accidentally collapses it back into one.  Read
    :attr:`differences`, or print :meth:`render`.
    """

    label_a: str
    label_b: str
    differences: tuple[Difference, ...]
    tables_compared: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not self.differences

    def tables_with_differences(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for diff in self.differences:
            seen[diff.table] = None
        return tuple(seen)

    def for_table(self, table: str) -> tuple[Difference, ...]:
        return tuple(d for d in self.differences if d.table == table)

    def render(self) -> str:
        header = (
            f"write-path parity: {self.label_a} vs {self.label_b} "
            f"({len(self.tables_compared)} tables compared, "
            f"{len(self.differences)} difference(s))"
        )
        if self.is_clean:
            return f"{header}\n  no differences"
        lines = [header]
        for table in self.tables_with_differences():
            lines.append(f"{table}:")
            lines.extend(
                d.render(self.label_a, self.label_b) for d in self.for_table(table)
            )
        return "\n".join(lines)


def _row_key(row: dict[str, Any], key_columns: Sequence[str]) -> str:
    return "|".join(repr(row.get(c)) for c in key_columns)


def values_differ(value_a: Any, value_b: Any) -> bool:
    """Cell comparison, **type-aware** — the reason this is not a bare ``!=``.

    Python's ``1 == True`` and ``1 == 1.0`` are exactly the two equalities a
    storage-engine swap must not be allowed to hide:

    - ``True`` where the other backend has ``1``.  Every flag column in
      ``coord/db.py`` is declared ``INTEGER DEFAULT 0`` (``sql.FLAG_COLUMN_DDL``)
      precisely so the wire keeps shipping ``0``/``1``; a real ``BOOLEAN``
      column would serialise to JSON ``true``, which fails the parse of the
      **whole** ``BoardPayload`` and blanks the TUI board (#632/#546/#628).
      ``tests/test_store_contract.py`` guards this on the read side with the
      same ``isinstance(True, int)`` reasoning; this is its write-side twin.
    - ``1`` where the other backend has ``1.0``.  Same class of problem one
      column over: a JSON ``1`` and a JSON ``1.0`` deserialise differently into
      a typed Rust struct.

    A bare ``!=`` reports neither, and would make the module docstring's
    "``True`` is not folded into ``1``" a false claim.
    """
    if type(value_a) is not type(value_b):
        return True
    return value_a != value_b


def _row_signature(row: dict[str, Any], columns: Sequence[str]) -> tuple:
    """A row reduced to something hashable, sortable and **type-aware**.

    Carries the type name alongside each value for the same reason
    :func:`values_differ` exists — a plain tuple of values would compare equal
    across the ``True``/``1`` and ``1``/``1.0`` splits.
    """
    return tuple(
        (column, type(row.get(column)).__name__, repr(row.get(column)))
        for column in columns
    )


def _sort_key(row: dict[str, Any], columns: Sequence[str]) -> str:
    return json.dumps(_row_signature(row, columns))


def compare_dumps(dump_a: Dump, dump_b: Dump) -> ParityReport:
    """Diff two dumps, matching rows by primary key where one exists.

    Tables with no primary key (none in today's schema, but the harness must
    not assume that) fall back to a multiset comparison of whole rows: the
    report then says "this row is only on one side" rather than pinning a
    column, which is the most it can honestly claim without a key.
    """
    differences: list[Difference] = []
    tables = sorted(set(dump_a.rows) | set(dump_b.rows))
    for table in tables:
        in_a, in_b = table in dump_a.rows, table in dump_b.rows
        if not in_b:
            differences.append(Difference(table, KIND_TABLE_ONLY_IN_A, key=""))
            continue
        if not in_a:
            differences.append(Difference(table, KIND_TABLE_ONLY_IN_B, key=""))
            continue

        cols_a, cols_b = dump_a.columns[table], dump_b.columns[table]
        if set(cols_a) != set(cols_b):
            differences.append(
                Difference(
                    table,
                    KIND_COLUMNS,
                    key="",
                    value_a=sorted(cols_a),
                    value_b=sorted(cols_b),
                )
            )
            continue

        shared_columns = list(cols_a)
        key_columns = dump_a.keys.get(table) or dump_b.keys.get(table) or ()
        if key_columns:
            differences.extend(
                _compare_keyed(table, dump_a, dump_b, key_columns, shared_columns)
            )
        else:
            differences.extend(_compare_unkeyed(table, dump_a, dump_b, shared_columns))
    return ParityReport(
        label_a=dump_a.label,
        label_b=dump_b.label,
        differences=tuple(differences),
        tables_compared=tuple(tables),
    )


def _compare_keyed(
    table: str,
    dump_a: Dump,
    dump_b: Dump,
    key_columns: Sequence[str],
    columns: Sequence[str],
) -> list[Difference]:
    by_key_a = {_row_key(r, key_columns): r for r in dump_a.rows[table]}
    by_key_b = {_row_key(r, key_columns): r for r in dump_b.rows[table]}
    out: list[Difference] = []
    for key in sorted(set(by_key_a) - set(by_key_b)):
        out.append(Difference(table, KIND_ROW_ONLY_IN_A, key, value_a=by_key_a[key]))
    for key in sorted(set(by_key_b) - set(by_key_a)):
        out.append(Difference(table, KIND_ROW_ONLY_IN_B, key, value_b=by_key_b[key]))
    for key in sorted(set(by_key_a) & set(by_key_b)):
        row_a, row_b = by_key_a[key], by_key_b[key]
        for column in columns:
            if values_differ(row_a.get(column), row_b.get(column)):
                out.append(
                    Difference(
                        table,
                        KIND_CELL,
                        key,
                        column=column,
                        value_a=row_a.get(column),
                        value_b=row_b.get(column),
                    )
                )
    return out


def _compare_unkeyed(
    table: str, dump_a: Dump, dump_b: Dump, columns: Sequence[str]
) -> list[Difference]:
    sorted_a = sorted(dump_a.rows[table], key=lambda r: _sort_key(r, columns))
    sorted_b = sorted(dump_b.rows[table], key=lambda r: _sort_key(r, columns))
    remaining_b = [(_row_signature(r, columns), r) for r in sorted_b]
    out: list[Difference] = []
    for row in sorted_a:
        signature = _row_signature(row, columns)
        match = next((pair for pair in remaining_b if pair[0] == signature), None)
        if match is not None:
            remaining_b.remove(match)
        else:
            out.append(Difference(table, KIND_ROW_ONLY_IN_A, key="?", value_a=row))
    for _signature, row in remaining_b:
        out.append(Difference(table, KIND_ROW_ONLY_IN_B, key="?", value_b=row))
    return out


# ── running both sides ───────────────────────────────────────────────────────


def run_and_dump(
    conn: Any,
    *,
    label: str,
    workload: Sequence[WorkloadStep] = WORKLOAD,
) -> Dump:
    """Replay *workload* against *conn* and return the canonical dump."""
    run_workload(conn, workload=workload)
    return dump_database(conn, label=label)


@contextlib.contextmanager
def open_prepared(backend: str) -> Iterator[Any]:
    """An empty, freshly-migrated database on *backend*, set up like production.

    Opens through ``tests.backends.open_named_session`` — the same
    ``:memory:``-on-SQLite / private-schema-on-Postgres isolation the autouse
    ``coord_db`` fixture gets, so the harness never touches a shared database —
    and migrates it with ``coord.db._ensure_schema``, the one migration
    implementation both backends share (#827).

    The connection then gets ``sql.apply_connection_setup()``, which the test
    fixture deliberately skips (#2884: turning ``PRAGMA foreign_keys=ON`` on for
    the whole suite would have been a behaviour change).  Here it is required,
    not optional: SQLite enforces foreign keys **only** when that pragma is set
    on the writing connection while Postgres always enforces, so a harness that
    left it off would run one side with referential integrity and the other
    without — and would then report every resulting divergence as a finding it
    manufactured itself.
    """
    from coord.db import _ensure_schema
    from tests.backends import open_named_session

    session = open_named_session(backend)
    try:
        sql.apply_connection_setup(session.conn)
        _ensure_schema(session.conn)
        yield session.conn
    finally:
        session.close()


def compare_backends(
    backend_a: str = "sqlite",
    backend_b: str = "sqlite",
    *,
    workload: Sequence[WorkloadStep] = WORKLOAD,
) -> ParityReport:
    """Open both backends, replay the workload on each, and diff the result."""
    dumps: list[Dump] = []
    for index, backend in enumerate((backend_a, backend_b)):
        with open_prepared(backend) as conn:
            label = backend if backend_a != backend_b else f"{backend}#{index + 1}"
            dumps.append(run_and_dump(conn, label=label, workload=workload))
    return compare_dumps(dumps[0], dumps[1])


#: :func:`main`'s exit status when a requested backend could not be opened at
#: all.  Distinct from 1 ("the backends disagree") on purpose: an operator
#: scripting the cutover check must be able to tell "Postgres isn't reachable"
#: apart from "Postgres is reachable and wrong".
EXIT_UNAVAILABLE = 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.write_parity",
        description=(
            "#2885: replay the recorded write workload against two backends and "
            "report the differences between the resulting databases."
        ),
    )
    parser.add_argument("--a", default="sqlite", help="first backend (default: sqlite)")
    parser.add_argument("--b", default="sqlite", help="second backend (default: sqlite)")
    args = parser.parse_args(argv)

    from tests.backends import BACKEND_POSTGRES, postgres_available

    if BACKEND_POSTGRES in (args.a, args.b):
        unavailable = postgres_available()
        if unavailable:
            # An actionable line, not a driver traceback: "psycopg is missing"
            # and "no server at that DSN" are setup problems with known fixes,
            # and the message already names both.
            print(f"cannot run: {unavailable}", file=sys.stderr)
            return EXIT_UNAVAILABLE

    report = compare_backends(args.a, args.b)
    print(report.render())
    return 0 if report.is_clean else 1


if __name__ == "__main__":  # pragma: no cover -- exercised via main() in tests
    sys.exit(main())
