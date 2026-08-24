"""#1942: the backend-parametrised ``CoordStore`` contract suite.

``coord/dao.py`` declares :class:`~coord.dao.CoordStore` as a ``Protocol``, but
until this file nothing verified that an implementation actually *behaved*
correctly — only that it type-checked.  ``isinstance(store, CoordStore)`` on a
``runtime_checkable`` Protocol checks method **names**, not signatures and
certainly not semantics, so a store that returns rows in the wrong order, drops
a column, or ships a JSON ``true`` where the wire wants ``1`` satisfies it
perfectly.

That gap is why Phase D of the Store Service program (#827 → #828 → #829,
[`docs/STORE_SERVICE.md`](../docs/STORE_SERVICE.md)) has no oracle.  #829's
acceptance is "``/board`` parity", and parity needs something to measure
against that is not itself SQLite-specific: "no behaviour change; the existing
suite stays green" only proves the *SQLite* path still works, because the
existing suite instantiates ``SqliteStore``.

**How it is structured** (and why it is structured that way):

- The behavioural assertions are plain functions of one argument — a
  ``CoordStore`` — collected in :data:`CONTRACT_CHECKS`.  They are *not*
  ``test_`` functions, because they have to be runnable two ways: once per
  backend (the real suite) and once per *deliberately-broken* store (the
  meta-suite that proves the checks can actually fail).
- Backends live in :data:`BACKENDS`.  Adding one is appending a single
  :class:`Backend` entry whose ``build`` seeds the canonical dataset and
  returns a store — **no new test code**, which is the first acceptance
  criterion.
- The canonical dataset is :func:`canonical_rows`: the #748 golden ``/board``
  fixture (``scripts/gen_board_fixture.py``, already the shared source of truth
  for the Rust↔Python wire seam) plus the handful of extra rows the *read
  contract* needs and the golden fixture does not carry — a second row in every
  ordered collection so an ordering assertion is falsifiable, and rows in the
  tables the fixture leaves empty (``notifications``, ``plans``, ``audit_log``,
  ``drive_escalations``).  It is extracted as **plain dicts**, so it carries no
  SQLite INSERT idiom and a future backend can load it with its own paramstyle.
- Each check declares which ``CoordStore`` methods it ``covers``, and
  :func:`test_every_coordstore_method_is_covered` asserts the union is the
  whole protocol.  When Phase C (#1948) widens ``CoordStore`` with the write
  surface, that test goes red until the new methods are covered here — which is
  the mechanism by which "and the write surface as Phase C lands it" holds
  without anyone having to remember it.

**Scope.**  The read surface as it exists today.  Two things are deliberately
*out*:

- The #762 retention cap.  ``COORD_BOARD_RETENTION_DAYS=0`` is forced for this
  module (see :func:`_disable_board_retention_cap`) so ``board_projection()``
  is a pure composition of the collection reads.  The cap's own rules live in
  the backend-agnostic pure function ``dao.compute_board_keep_ids`` and are
  covered by ``tests/test_board_cap_762.py``; re-asserting them per backend
  would test that function twice, not the store.
- Writes.  They are not on ``CoordStore`` yet (#1823 removed the dead stubs).
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from coord import board_schema
from coord.board_schema import BOARD_PROJECTIONS, INTEGER_BACKED_BOOLEANS
from coord.dao import SCHEMA_VERSION, CoordStore, SqliteStore
from coord.db import _ensure_schema

# ── the canonical dataset ────────────────────────────────────────────────────

#: Every table the read contract touches, in dependency-free insert order.
DATASET_TABLES: tuple[str, ...] = (
    "board_meta",
    "machines",
    "issues",
    "assignments",
    "merge_queue",
    "proposals",
    "drive_queue",
    "drive_escalations",
    "notifications",
    "plans",
    "audit_log",
)


def _seed_contract_extras(conn: sqlite3.Connection) -> None:
    """Rows the #748 golden fixture does not carry but the read contract needs.

    Two reasons a row is here rather than in ``scripts/gen_board_fixture.py``:

    1. **Falsifiability.**  An ordering assertion over a one-row collection is
       vacuously true, so every ``ORDER BY`` in ``SqliteStore`` needs at least
       two rows — and for ``list_issues`` (``ORDER BY repo_name, number``) the
       rows must span two repos, inserted out of order, or the *repo* half of
       the sort is never exercised.
    2. **Non-empty tables.**  The golden fixture seeds no ``notifications``,
       ``plans``, ``audit_log`` or ``drive_escalations`` rows, so four
       ``CoordStore`` methods would be asserted against ``[]``/``{}``.

    Widening the committed golden fixture instead would churn a file that two
    languages' CI compares byte-for-byte, for assertions only this suite makes.
    """
    # A second issue in a repo that sorts *before* claude-coordinator, plus a
    # higher-numbered and a closed one — inserted deliberately out of order.
    conn.executemany(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "synced_at, milestone_number, milestone_title) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "claude-coordinator", 900, "closed: superseded by #1942",
                "Closed so the projection carries a non-open row too.",
                "closed", '["coord"]', 1000001100.0, None, None,
            ),
            (
                "apex-api", 3, "sorts first by repo_name",
                "Second repo, so ORDER BY repo_name is not vacuous.",
                "open", '[]', 1000001100.0, None, None,
            ),
            (
                "claude-coordinator", 200, "sorts between #3 and #748",
                "Third issue so ORDER BY number is not vacuous either.",
                "open", '["coord", "status:ready"]', 1000001100.0, None, None,
            ),
        ],
    )

    # A second merge-queue row, so `ORDER BY id` has something to order.
    conn.execute(
        "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, branch, "
        "target_branch, issue_number, issue_title, state, pr_number, pr_url, size, "
        "enqueued_at, required_gates) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "rev-748a", "claude-coordinator", "JDonaghy/claude-coordinator",
            "issue-749-followup", "main", 749, "interactive follow-up",
            "queued", 9002, "https://github.com/JDonaghy/claude-coordinator/pull/9002",
            17, 1000000960.0, '["test", "review", "merge"]',
        ),
    )

    # A second proposal, with a populated `files_likely` JSON column.
    conn.execute(
        "INSERT INTO proposals (machine_name, repo_name, issue_number, issue_title, "
        "rationale, files_likely, type, required_gates) VALUES (?,?,?,?,?,?,?,?)",
        (
            "dellserver", "apex-api", 3, "sorts first by repo_name",
            "dellserver is idle", '["coord/dao.py"]', "work", '["review"]',
        ),
    )

    # Escalations: the golden fixture seeds none.
    conn.executemany(
        "INSERT INTO drive_escalations (repo_name, issue_number, stage, "
        "assignment_id, reason, gate_readings, proposed_command, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                "claude-coordinator", 748, "merge", "work-748a",
                "NEEDS_ATTENTION", "ci=failing", "coord merge --repo claude-coordinator",
                1000001300.0,
            ),
            (
                "apex-api", 3, "review", None,
                "NO_REVIEWER", "review=pending", "coord plan",
                1000001400.0,
            ),
        ],
    )

    # Notifications: keyed by assignment_id, no DTO (passed through raw).
    conn.executemany(
        "INSERT INTO notifications (assignment_id, event, branch, posted_at) "
        "VALUES (?,?,?,?)",
        [
            ("work-748a", "completed", "issue-748-fixture", 1000000650.0),
            ("rev-748a", "review_posted", None, 1000000950.0),
        ],
    )

    # Plans: JSON-in-TEXT decoded by `list_plans`, keyed by assignment_id.
    conn.executemany(
        "INSERT INTO plans (assignment_id, plan_data) VALUES (?,?)",
        [
            ("work-748a", json.dumps({"steps": ["read dao.py", "write the suite"]})),
            ("work-748b", json.dumps({"steps": [], "notes": "not started"})),
        ],
    )

    # One *ancient* audit row: `audit_recent_count` is a 900s rolling window,
    # so a correct store reports 0 here and a store that counts the whole
    # table reports 1.
    conn.execute(
        "INSERT INTO audit_log (ts, tier, category, event_type, actor, repo, "
        "issue, assignment_id, machine, summary, details_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            1000000600.0, "info", "assignment", "completed", "coord",
            "claude-coordinator", 748, "work-748a", "precision",
            "work-748a completed", None,
        ),
    )
    conn.commit()


def canonical_rows() -> dict[str, list[dict]]:
    """The dataset every backend under test is seeded with, as plain dicts.

    Built by running the #748 golden-fixture generator (plus
    :func:`_seed_contract_extras`) into an in-memory SQLite DB and reading
    every row back out.  Extracting rather than hand-writing the literals is
    what keeps this dataset from drifting away from the golden fixture the
    Rust and Python sides of the wire seam already share.

    The result carries **no SQLite idiom** — just column names and scalar
    values — so a second backend loads it with its own DDL and paramstyle and
    the assertions below stay identical.
    """
    from scripts.gen_board_fixture import build_fixture_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        build_fixture_db(conn)
        _seed_contract_extras(conn)
        return {
            table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]  # noqa: S608
            for table in DATASET_TABLES
        }
    finally:
        conn.close()


# ── backends ─────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Backend:
    """One ``CoordStore`` implementation under contract.

    ``build(rows, tmp_path)`` must create the backend's schema, load *rows*
    (the output of :func:`canonical_rows`), and return a live store.  Adding a
    backend is appending one of these to :data:`BACKENDS` — the checks below do
    not change.
    """

    name: str
    build: Callable[[dict[str, list[dict]], Path], CoordStore]


def _build_sqlite_store(rows: dict[str, list[dict]], tmp_path: Path) -> CoordStore:
    """The SQLite backend adapter: migrate a fresh DB, load the dataset, return
    a ``mode=ro`` :class:`~coord.dao.SqliteStore` over it."""
    db_path = tmp_path / "coord.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        for table, table_rows in rows.items():
            for row in table_rows:
                columns = list(row)
                placeholders = ",".join("?" * len(columns))
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) "  # noqa: S608
                    f"VALUES ({placeholders})",
                    [row[c] for c in columns],
                )
        conn.commit()
    finally:
        conn.close()
    # The writer is closed before the reader opens, so the store's own
    # `mode=ro` connection sees a fully committed database.
    return SqliteStore(db_path)


#: Every backend the contract runs against.  **Append here to add one.**
BACKENDS: tuple[Backend, ...] = (Backend(name="sqlite", build=_build_sqlite_store),)


# ── expected values (properties of the dataset, not of any backend) ──────────

EXPECTED_ASSIGNMENT_IDS = ["work-748b", "rev-748a", "work-748a"]  # dispatched_at DESC
EXPECTED_MACHINE_NAMES = ["dellserver", "precision"]  # name ASC
EXPECTED_MERGE_QUEUE_ASSIGNMENTS = ["work-748a", "rev-748a"]  # id ASC
EXPECTED_PROPOSAL_ISSUES = [750, 3]  # id ASC
EXPECTED_ISSUE_KEYS = [  # repo_name ASC, number ASC
    ("apex-api", 3),
    ("claude-coordinator", 200),
    ("claude-coordinator", 748),
    ("claude-coordinator", 900),
]
EXPECTED_DRIVE_QUEUE_ISSUES = [748, 750]  # position ASC
EXPECTED_ESCALATION_ISSUES = [748, 3]  # id ASC
EXPECTED_ROUND_NUMBER = 3
EXPECTED_NOTIFICATION_IDS = {"work-748a", "rev-748a"}
EXPECTED_PLAN_IDS = {"work-748a", "work-748b"}

#: ``board_projection()`` key → the collection read it must agree with.
PROJECTION_COLLECTIONS: dict[str, str] = {
    "assignments": "list_assignments",
    "machines": "list_machines",
    "merge_queue": "list_merge_queue",
    "proposals": "list_proposals",
    "issues": "list_issues",
    "escalations": "list_drive_escalations",
    "drive_queue": "list_drive_queue",
    "notifications": "list_notifications",
}


def _wire_fields(table: str) -> list[str]:
    """The declared wire field names for *table*, in contract order."""
    return [f.name for f in dataclasses.fields(BOARD_PROJECTIONS[table])]


def _is_wire_integer(value: object) -> bool:
    """True when *value* is a JSON integer.

    ``isinstance(True, int)`` is ``True`` in Python, so the ``bool`` exclusion
    is the entire point: a Postgres ``BOOLEAN`` column arriving as ``True``
    would sail through a naive ``isinstance`` check and then ship as JSON
    ``true``, which fails the parse of the *whole* ``BoardPayload`` and blanks
    the TUI board (#632/#546/#628).
    """
    return isinstance(value, int) and not isinstance(value, bool)


# ── the contract checks ──────────────────────────────────────────────────────


def check_protocol_conformance(store: CoordStore) -> None:
    """Every declared ``CoordStore`` method exists and is callable."""
    assert isinstance(store, CoordStore)
    for name in protocol_methods():
        assert callable(getattr(store, name, None)), f"missing {name!r}"


def check_assignments_projection(store: CoordStore) -> None:
    """``list_assignments`` is newest-dispatched-first, projected through
    ``BoardAssignment`` — every declared field, in declared order, and nothing
    else.  ``briefing`` is the named exclusion: ~8 MB of an ~12 MB live payload
    that no board view renders."""
    rows = store.list_assignments()
    assert [r["assignment_id"] for r in rows] == EXPECTED_ASSIGNMENT_IDS
    expected_keys = _wire_fields("assignments")
    for row in rows:
        assert list(row) == expected_keys
        assert "briefing" not in row


def check_machines_projection(store: CoordStore) -> None:
    """``list_machines`` is name-ordered and projected through ``BoardMachine``."""
    rows = store.list_machines()
    assert [r["name"] for r in rows] == EXPECTED_MACHINE_NAMES
    for row in rows:
        assert list(row) == _wire_fields("machines")


def check_merge_queue_projection(store: CoordStore) -> None:
    """``list_merge_queue`` is insertion-ordered (``id``) and DTO-projected."""
    rows = store.list_merge_queue()
    assert [r["assignment_id"] for r in rows] == EXPECTED_MERGE_QUEUE_ASSIGNMENTS
    for row in rows:
        assert list(row) == _wire_fields("merge_queue")


def check_proposals_projection(store: CoordStore) -> None:
    """``list_proposals`` is insertion-ordered (``id``) and DTO-projected."""
    rows = store.list_proposals()
    assert [r["issue_number"] for r in rows] == EXPECTED_PROPOSAL_ISSUES
    for row in rows:
        assert list(row) == _wire_fields("proposals")


def check_issues_projection(store: CoordStore) -> None:
    """``list_issues`` sorts by ``(repo_name, number)`` across repos, and
    carries closed issues as well as open ones."""
    rows = store.list_issues()
    assert [(r["repo_name"], r["number"]) for r in rows] == EXPECTED_ISSUE_KEYS
    assert {r["state"] for r in rows} == {"open", "closed"}
    for row in rows:
        assert list(row) == _wire_fields("issues")


def check_drive_queue_projection(store: CoordStore) -> None:
    """``list_drive_queue`` is in ``position`` order — the order the queue will
    actually run — not insertion order."""
    rows = store.list_drive_queue()
    assert [r["issue_number"] for r in rows] == EXPECTED_DRIVE_QUEUE_ISSUES
    assert [r["position"] for r in rows] == sorted(r["position"] for r in rows)
    for row in rows:
        assert list(row) == _wire_fields("drive_queue")


def check_drive_escalations_projection(store: CoordStore) -> None:
    """``list_drive_escalations`` is insertion-ordered (``id``) and DTO-projected."""
    rows = store.list_drive_escalations()
    assert [r["issue_number"] for r in rows] == EXPECTED_ESCALATION_ISSUES
    for row in rows:
        assert list(row) == _wire_fields("drive_escalations")


def check_json_columns_round_trip(store: CoordStore) -> None:
    """A field the DTO types ``list``/``dict`` arrives **decoded**, whatever the
    backend stores underneath.

    ``review_findings`` is the deliberate counter-example: it is annotated
    ``str`` because coord-tui consumes it as a raw JSON string
    (``Option<String>``), so a store that "helpfully" decodes it breaks that
    client.
    """
    assignments = {r["assignment_id"]: r for r in store.list_assignments()}
    done = assignments["work-748a"]
    assert done["smoke_tests"] == [
        "fixture loads in the TUI",
        "round_number is non-zero",
    ]
    assert done["test_plan"] == {
        "steps": [{"kind": "run", "cmd": "cargo test", "label": "run tui tests"}]
    }
    assert isinstance(done["review_findings"], str), (
        "review_findings must stay a raw JSON *string* on the wire — coord-tui "
        "parses it as Option<String>"
    )

    for row in store.list_machines():
        assert isinstance(row["capabilities"], list)
        assert isinstance(row["repos"], list)
    for row in store.list_issues():
        assert isinstance(row["labels"], list)
    for row in store.list_drive_queue():
        assert isinstance(row["after_json"], list)
    for row in store.list_merge_queue():
        assert row["required_gates"] is None or isinstance(row["required_gates"], list)
    for row in store.list_proposals():
        assert isinstance(row["files_likely"], list)


def check_integer_backed_booleans_stay_integers(store: CoordStore) -> None:
    """#748/#632: a semantically-boolean column ships as ``0``/``1``, never as
    a JSON ``true``/``false`` and never as a string.

    This is the single most backend-sensitive assertion in the suite and the
    reason the issue calls it out by name.  SQLite has no boolean type, so
    these columns are ``INTEGER``; Postgres would make them real ``BOOLEAN``s
    and a naive adapter would hand back Python ``True``.  The DTOs annotate
    them ``int`` (``board_schema.INTEGER_BACKED_BOOLEANS``) precisely so the
    wire type is a property of the contract rather than of the storage engine —
    but an annotation is not enforcement, and ``project_row`` deliberately does
    not coerce.  This check is the enforcement.
    """
    rows_by_table = {
        "assignments": store.list_assignments(),
        "drive_queue": store.list_drive_queue(),
    }
    seen: set[str] = set()
    for table, rows in rows_by_table.items():
        declared = set(_wire_fields(table)) & INTEGER_BACKED_BOOLEANS
        for column in declared:
            for row in rows:
                value = row[column]
                assert value is None or _is_wire_integer(value), (
                    f"{table}.{column} came back as {value!r} ({type(value).__name__}); "
                    "it must be a plain int (0/1) on the wire"
                )
                seen.add(column)
    assert seen == INTEGER_BACKED_BOOLEANS, (
        f"not every INTEGER-backed boolean was exercised: missing "
        f"{sorted(INTEGER_BACKED_BOOLEANS - seen)}"
    )
    # And the values themselves survived the round trip, not just their types.
    by_id = {r["assignment_id"]: r for r in store.list_assignments()}
    assert by_id["work-748b"]["is_interactive"] == 1
    assert by_id["work-748a"]["is_interactive"] == 0


def check_plans_decode(store: CoordStore) -> None:
    """``list_plans`` is a mapping of ``assignment_id`` → the **decoded** plan."""
    plans = store.list_plans()
    assert set(plans) == EXPECTED_PLAN_IDS
    assert plans["work-748a"] == {"steps": ["read dao.py", "write the suite"]}
    assert isinstance(plans["work-748b"], dict)


def check_notifications(store: CoordStore) -> None:
    """``notifications`` has no DTO, so it is passed through — but it must still
    come back as one dict per row, keyed by ``assignment_id``."""
    rows = store.list_notifications()
    assert {r["assignment_id"] for r in rows} == EXPECTED_NOTIFICATION_IDS
    for row in rows:
        assert {"assignment_id", "event", "branch", "posted_at"} <= set(row)


def check_board_meta_is_raw_strings(store: CoordStore) -> None:
    """``board_meta`` values are served as **raw strings**; each client parses
    the keys it knows.  In particular ``pipeline_default_gates`` stays an
    undecoded JSON string — decoding it here would be a wire change."""
    meta = store.board_meta()
    assert meta["round_number"] == "3"
    assert meta["board_initialized"] == "1"
    assert meta["pipeline_default_gates"] == '["test", "review", "merge"]'
    assert all(isinstance(v, str) for v in meta.values())


def check_round_number(store: CoordStore) -> None:
    """``round_number`` is the ``board_meta`` string coerced to an ``int``."""
    value = store.round_number()
    assert _is_wire_integer(value)
    assert value == EXPECTED_ROUND_NUMBER


def check_get_assignment(store: CoordStore) -> None:
    """The point read serves the **full** row — ``briefing`` included, JSON
    columns still decoded — and ``None`` for an id that does not exist."""
    row = store.get_assignment("work-748a")
    assert row is not None
    assert "briefing" in row, "the detail read must serve the full row"
    assert row["assignment_id"] == "work-748a"
    assert row["smoke_tests"] == [
        "fixture loads in the TUI",
        "round_number is non-zero",
    ]
    assert _is_wire_integer(row["is_interactive"])
    assert store.get_assignment("no-such-assignment") is None


def check_get_issue(store: CoordStore) -> None:
    """The point read serves the full issue body with ``labels`` decoded, and
    ``None`` for a ``(repo, number)`` that does not exist."""
    row = store.get_issue("claude-coordinator", 748)
    assert row is not None
    assert row["number"] == 748
    assert row["labels"] == ["coord", "status:ready"]
    assert row["body"].startswith("## Context")
    assert store.get_issue("claude-coordinator", 424242) is None
    assert store.get_issue("no-such-repo", 748) is None


def check_board_projection_composes_the_collection_reads(store: CoordStore) -> None:
    """``board_projection()`` is one consistent snapshot whose collections agree
    with the individual reads — including the ``drive_escalations`` →
    ``escalations`` wire rename, which is the one place the projection key and
    the table name differ."""
    payload = store.board_projection()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["round_number"] == EXPECTED_ROUND_NUMBER
    assert payload["board_meta"] == store.board_meta()
    assert payload["plans"] == store.list_plans()
    for key, reader in PROJECTION_COLLECTIONS.items():
        assert payload[key] == getattr(store, reader)(), (
            f"board_projection()[{key!r}] disagrees with {reader}()"
        )


def check_board_projection_is_json_serialisable(store: CoordStore) -> None:
    """Nothing storage-engine-shaped may reach the wire.

    ``json.dumps`` with no ``default=`` hook is the cheapest total assertion
    that no backend-native type — a ``datetime``, a ``Decimal``, a ``UUID``, a
    ``memoryview`` — leaked out of the store.  SQLite cannot produce any of
    those; a DB-API driver with type adaptation very much can.
    """
    json.dumps(store.board_projection())


def check_audit_recent_count_is_a_bounded_window(store: CoordStore) -> None:
    """``audit_recent_count`` is a rolling 900s recency window, not a row count.

    The dataset's only audit row is dated to 2001, so a correct store reports
    ``0``; a store that returns ``SELECT COUNT(*)`` reports ``1``.
    """
    payload = store.board_projection()
    count = payload["audit_recent_count"]
    assert _is_wire_integer(count)
    assert count == 0, "an ancient audit row must fall outside the recency window"


def check_reads_are_repeatable(store: CoordStore) -> None:
    """The same read twice returns the same thing.

    Cheap, but it is what catches a backend that leaks cursor/connection state
    between calls (an exhausted server-side cursor returning ``[]`` the second
    time is a classic DB-API adapter bug, and invisible to a suite that reads
    each method once).
    """
    assert store.list_assignments() == store.list_assignments()
    assert store.list_issues() == store.list_issues()
    assert store.board_projection() == store.board_projection()


# ── the check registry ───────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Check:
    """One behavioural assertion, plus the ``CoordStore`` methods it exercises.

    ``covers`` is what makes :func:`test_every_coordstore_method_is_covered`
    possible: it turns "is this contract complete?" into an assertion instead
    of a promise.
    """

    name: str
    covers: tuple[str, ...]
    run: Callable[[CoordStore], None]


CONTRACT_CHECKS: tuple[Check, ...] = (
    Check("protocol_conformance", (), check_protocol_conformance),
    Check("assignments_projection", ("list_assignments",), check_assignments_projection),
    Check("machines_projection", ("list_machines",), check_machines_projection),
    Check("merge_queue_projection", ("list_merge_queue",), check_merge_queue_projection),
    Check("proposals_projection", ("list_proposals",), check_proposals_projection),
    Check("issues_projection", ("list_issues",), check_issues_projection),
    Check("drive_queue_projection", ("list_drive_queue",), check_drive_queue_projection),
    Check(
        "drive_escalations_projection",
        ("list_drive_escalations",),
        check_drive_escalations_projection,
    ),
    Check("json_columns_round_trip", (), check_json_columns_round_trip),
    Check(
        "integer_backed_booleans_stay_integers",
        (),
        check_integer_backed_booleans_stay_integers,
    ),
    Check("plans_decode", ("list_plans",), check_plans_decode),
    Check("notifications", ("list_notifications",), check_notifications),
    Check("board_meta_is_raw_strings", ("board_meta",), check_board_meta_is_raw_strings),
    Check("round_number", ("round_number",), check_round_number),
    Check("get_assignment", ("get_assignment",), check_get_assignment),
    Check("get_issue", ("get_issue",), check_get_issue),
    Check(
        "board_projection_composes_the_collection_reads",
        ("board_projection",),
        check_board_projection_composes_the_collection_reads,
    ),
    Check(
        "board_projection_is_json_serialisable",
        (),
        check_board_projection_is_json_serialisable,
    ),
    Check(
        "audit_recent_count_is_a_bounded_window",
        (),
        check_audit_recent_count_is_a_bounded_window,
    ),
    Check("reads_are_repeatable", (), check_reads_are_repeatable),
)


def protocol_methods() -> frozenset[str]:
    """The public method names declared on the ``CoordStore`` Protocol."""
    return frozenset(
        name
        for name, value in vars(CoordStore).items()
        if callable(value) and not name.startswith("_")
    )


def run_checks(make_store: Callable[[], CoordStore], checks: Iterable[Check]) -> list[str]:
    """Run *checks*, each against a **freshly built** store; return the names of
    the ones that failed.

    A factory rather than an instance because one of the failure modes under
    test — :class:`NonRepeatableStore`, the exhausted-cursor bug — is *stateful*:
    run against a shared instance it would be exhausted by whichever check
    happened to read first, and the check that actually targets it would then
    compare two empty lists and pass.  Building per check keeps each check's
    verdict attributable to the check itself.
    """
    failed: list[str] = []
    for check in checks:
        try:
            check.run(make_store())
        except AssertionError:
            failed.append(check.name)
        except Exception as exc:  # noqa: BLE001 — a raising store fails too
            failed.append(f"{check.name} ({type(exc).__name__}: {exc})")
    return failed


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _disable_board_retention_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn off the #762 age cap for this module.

    The canonical dataset is dated to 2001 so it never changes between runs,
    which means every terminal row is far outside the default 14-day retention
    window.  With the cap on, ``board_projection()`` would drop rows that
    ``list_assignments()`` still returns and the composition check would be
    asserting #762's rules rather than the store's.  Those rules live in the
    backend-agnostic pure function ``dao.compute_board_keep_ids`` and are
    covered by ``tests/test_board_cap_762.py``.
    """
    monkeypatch.setenv("COORD_BOARD_RETENTION_DAYS", "0")


@pytest.fixture(scope="session")
def canonical_dataset() -> dict[str, list[dict]]:
    return canonical_rows()


@pytest.fixture(params=[b.name for b in BACKENDS])
def store(
    request: pytest.FixtureRequest,
    canonical_dataset: dict[str, list[dict]],
    tmp_path: Path,
) -> CoordStore:
    """A live ``CoordStore`` seeded with the canonical dataset.

    Parametrised over :data:`BACKENDS`, so every test below automatically runs
    against every registered backend.
    """
    backend = next(b for b in BACKENDS if b.name == request.param)
    return backend.build(canonical_dataset, tmp_path)


# ── the suite ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("check", CONTRACT_CHECKS, ids=lambda c: c.name)
def test_store_satisfies_the_contract(store: CoordStore, check: Check) -> None:
    """Acceptance: ``SqliteStore`` passes the contract today with no production
    change — and any backend appended to :data:`BACKENDS` is held to exactly
    the same assertions, with no new test code."""
    check.run(store)


def test_adding_a_backend_needs_no_new_test_code() -> None:
    """Acceptance criterion 1, asserted structurally.

    The suite's coupling to a backend is exactly one thing: an entry in
    :data:`BACKENDS`.  Nothing in :data:`CONTRACT_CHECKS` may name a concrete
    implementation, or "parametrised" is a claim rather than a property.
    """
    assert BACKENDS, "no backends registered"
    assert {b.name for b in BACKENDS} >= {"sqlite"}
    assert len({b.name for b in BACKENDS}) == len(BACKENDS), "duplicate backend name"

    for check in CONTRACT_CHECKS:
        body = inspect.getsource(check.run)
        for sqlite_only in ("SqliteStore", "sqlite3", "_ensure_schema", "coord.db"):
            assert sqlite_only not in body, (
                f"check {check.name!r} mentions {sqlite_only!r} — the checks must "
                "assert behaviour, not the SQLite implementation, or a second "
                "backend cannot be held to them"
            )


def test_every_coordstore_method_is_covered() -> None:
    """Acceptance: the contract covers the whole protocol, not a sample of it.

    This is also the forward guard for Phase C (#1948): when ``CoordStore``
    grows the write surface, this test goes red until the new methods have
    checks — so "the write surface as Phase C lands it" is enforced rather than
    remembered.
    """
    covered = {method for check in CONTRACT_CHECKS for method in check.covers}
    declared = protocol_methods()
    assert declared, "CoordStore declared no methods — enumeration is broken"
    assert covered == declared, (
        f"uncovered CoordStore methods: {sorted(declared - covered)}; "
        f"covered-but-undeclared: {sorted(covered - declared)}"
    )


def test_check_names_are_unique() -> None:
    """Duplicate names would silently collapse the parametrised ids (and the
    broken-store expectations below key off them)."""
    names = [c.name for c in CONTRACT_CHECKS]
    assert len(set(names)) == len(names)


# ── the meta-suite: a contract that cannot fail proves nothing ───────────────


class _BrokenStore:
    """Delegates every read to a real store, then corrupts one of them.

    Subclasses model the failure modes the issue names — wrong ordering, a
    dropped column, a stringified boolean — plus the two that a real second
    backend is most likely to hit: a native ``BOOLEAN`` and an undecoded JSON
    column.
    """

    def __init__(self, inner: CoordStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str):  # pragma: no cover - non-protocol attrs
        return getattr(self._inner, name)


def _delegate(name: str) -> Callable[..., object]:
    def delegating(self: _BrokenStore, *args: object, **kwargs: object) -> object:
        return getattr(self._inner, name)(*args, **kwargs)

    delegating.__name__ = name
    delegating.__qualname__ = f"_BrokenStore.{name}"
    delegating.__doc__ = f"Delegates {name}() to the wrapped store."
    return delegating


# Delegation has to be **real class attributes**, not just `__getattr__`:
# `runtime_checkable` protocol `isinstance` uses `inspect.getattr_static`
# (Python 3.12+), which reads the class dict and deliberately does *not* fire
# `__getattr__`.  A `__getattr__`-only wrapper therefore fails
# `isinstance(store, CoordStore)` and makes every broken store fail
# `protocol_conformance` for the wrong reason — masking whether the corruption
# it actually models was caught.  Subclass overrides still win via the MRO.
for _protocol_method in sorted(protocol_methods()):
    setattr(_BrokenStore, _protocol_method, _delegate(_protocol_method))
del _protocol_method


class WrongOrderStore(_BrokenStore):
    """Returns issues in the wrong order — the #829 parity failure that a
    "the existing suite is green" claim cannot see."""

    def list_issues(self) -> list[dict]:
        return list(reversed(self._inner.list_issues()))


class DroppedColumnStore(_BrokenStore):
    """Silently omits a declared column from every assignment row."""

    def list_assignments(self) -> list[dict]:
        return [
            {k: v for k, v in row.items() if k != "review_verdict"}
            for row in self._inner.list_assignments()
        ]


class StringifiedBooleanStore(_BrokenStore):
    """Ships ``is_interactive`` as ``"0"``/``"1"`` — the shape a driver with
    everything-is-text adaptation produces."""

    def list_assignments(self) -> list[dict]:
        return [
            {**row, "is_interactive": str(row["is_interactive"])}
            for row in self._inner.list_assignments()
        ]


class NativeBooleanStore(_BrokenStore):
    """Ships ``is_interactive`` as a Python ``bool`` — exactly what a Postgres
    ``BOOLEAN`` column yields, and exactly the #632 blank-board bug."""

    def list_assignments(self) -> list[dict]:
        return [
            {**row, "is_interactive": bool(row["is_interactive"])}
            for row in self._inner.list_assignments()
        ]


class UndecodedJsonStore(_BrokenStore):
    """Leaves a JSON-in-TEXT column as its raw string."""

    def list_machines(self) -> list[dict]:
        return [
            {**row, "capabilities": json.dumps(row["capabilities"])}
            for row in self._inner.list_machines()
        ]


class StaleProjectionStore(_BrokenStore):
    """``board_projection()`` disagrees with the collection reads."""

    def board_projection(self) -> dict:
        payload = dict(self._inner.board_projection())
        payload["issues"] = payload["issues"][:1]
        return payload


class MissingPointReadStore(_BrokenStore):
    """``get_assignment`` returns ``None`` for a row that exists."""

    def get_assignment(self, assignment_id: str) -> dict | None:
        return None


class UnboundedAuditCountStore(_BrokenStore):
    """``audit_recent_count`` counts the whole table instead of the window."""

    def board_projection(self) -> dict:
        payload = dict(self._inner.board_projection())
        payload["audit_recent_count"] = 1
        return payload


class NonRepeatableStore(_BrokenStore):
    """Second and later reads come back empty — an exhausted server-side
    cursor, the classic DB-API adapter bug."""

    def __init__(self, inner: CoordStore) -> None:
        super().__init__(inner)
        self._served = False

    def list_assignments(self) -> list[dict]:
        if self._served:
            return []
        self._served = True
        return self._inner.list_assignments()


#: ``broken store`` → the check that must catch it.  Naming the *specific*
#: check (rather than asserting "something failed") is what proves each check
#: is load-bearing rather than incidentally red.
BROKEN_STORES: tuple[tuple[type[_BrokenStore], str], ...] = (
    (WrongOrderStore, "issues_projection"),
    (DroppedColumnStore, "assignments_projection"),
    (StringifiedBooleanStore, "integer_backed_booleans_stay_integers"),
    (NativeBooleanStore, "integer_backed_booleans_stay_integers"),
    (UndecodedJsonStore, "json_columns_round_trip"),
    (StaleProjectionStore, "board_projection_composes_the_collection_reads"),
    (MissingPointReadStore, "get_assignment"),
    (UnboundedAuditCountStore, "audit_recent_count_is_a_bounded_window"),
    (NonRepeatableStore, "reads_are_repeatable"),
)


@pytest.mark.parametrize(
    ("broken_cls", "expected_check"), BROKEN_STORES, ids=lambda v: getattr(v, "__name__", v)
)
def test_contract_rejects_a_deliberately_broken_store(
    store: CoordStore, broken_cls: type[_BrokenStore], expected_check: str
) -> None:
    """Acceptance criterion 2: the suite fails when given a broken store.

    Each wrapper delegates everything to a *passing* store and corrupts exactly
    one read, so the named check going red is attributable to that corruption
    and nothing else.
    """
    failed = run_checks(lambda: broken_cls(store), CONTRACT_CHECKS)
    assert expected_check in failed, (
        f"{broken_cls.__name__} passed {expected_check!r} — that check does not "
        f"actually constrain the store (failures were: {failed})"
    )


def test_every_broken_store_is_caught_by_a_declared_check() -> None:
    """The expectations above must name checks that really exist."""
    names = {c.name for c in CONTRACT_CHECKS}
    for broken_cls, expected in BROKEN_STORES:
        assert expected in names, f"{broken_cls.__name__} expects unknown check {expected!r}"


def test_an_uncorrupted_store_passes_every_check(store: CoordStore) -> None:
    """The control for the meta-suite: with nothing corrupted, nothing fails.

    Without this, "the broken stores fail" would be consistent with the checks
    failing for everyone — a suite that always goes red proves as little as one
    that never does.
    """
    assert run_checks(lambda: store, CONTRACT_CHECKS) == []


def test_delegation_wrapper_does_not_itself_break_the_contract(store: CoordStore) -> None:
    """And the wrapper machinery is transparent — a ``_BrokenStore`` that
    overrides nothing passes every check, so a failure above is attributable to
    the corruption rather than to the delegation."""
    assert run_checks(lambda: _BrokenStore(store), CONTRACT_CHECKS) == []


def test_canonical_dataset_is_derived_from_the_golden_fixture(
    canonical_dataset: dict[str, list[dict]],
) -> None:
    """The dataset is the #748 golden fixture plus contract extras — not a
    hand-written parallel copy that can drift away from it."""
    assert set(canonical_dataset) == set(DATASET_TABLES)
    for table in DATASET_TABLES:
        assert canonical_dataset[table], f"{table} seeded no rows"
    # The golden fixture's own rows are present, unmodified.
    assert {r["assignment_id"] for r in canonical_dataset["assignments"]} == set(
        EXPECTED_ASSIGNMENT_IDS
    )
    # Plain data only: no sqlite3.Row, no bytes, nothing a second backend's
    # loader would have to special-case.
    for rows in canonical_dataset.values():
        for row in rows:
            assert type(row) is dict
            for value in row.values():
                assert value is None or isinstance(value, (str, int, float)), (
                    f"{value!r} is not a portable scalar"
                )


def test_dataset_makes_every_ordering_assertion_falsifiable(
    canonical_dataset: dict[str, list[dict]],
) -> None:
    """An ``ORDER BY`` asserted over a one-row table is vacuously true.

    Every ordered collection therefore needs at least two rows, and
    ``list_issues`` needs at least two *repos* or the ``repo_name`` half of its
    sort is never exercised.
    """
    for table in (
        "assignments",
        "machines",
        "merge_queue",
        "proposals",
        "issues",
        "drive_queue",
        "drive_escalations",
    ):
        assert len(canonical_dataset[table]) >= 2, (
            f"{table} has fewer than two rows — its ordering check cannot fail"
        )
    assert len({r["repo_name"] for r in canonical_dataset["issues"]}) >= 2


def test_notifications_has_no_dto_and_is_passed_through() -> None:
    """Pinned because :func:`check_notifications` asserts raw column names: if
    ``notifications`` ever gains a DTO, that check needs updating with it."""
    assert "notifications" not in BOARD_PROJECTIONS
    assert board_schema.decode_row("notifications", {"id": 1}) == {"id": 1}
