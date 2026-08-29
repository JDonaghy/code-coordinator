"""#1849: `GET /board`'s wire contract is severed from the SQLite DDL.

Before this, the daemon's `/board` response shape **was** the table layout —
`SqliteStore.board_projection()` did `SELECT *` and `coord/serve_app.py` built
`components/schemas` by `PRAGMA table_info`-introspecting a freshly-migrated
in-memory DB.  Three clients parse that shape (the Rust TUI, the React webapp,
`coord/client.py`), so every `coord/db.py` migration was a silent, unannounced
wire change — and, worse, the *storage engine's type system* was load-bearing
on it: SQLite has no boolean, so `INTEGER DEFAULT 0` flags ship as `0`/`1`
while Postgres `BOOLEAN` would ship `true`/`false`, and one unguarded field
blanks the entire TUI board (#632/#546/#628).

The contract now lives in `coord/board_schema.py` as explicit dataclasses.
This file is the acceptance proof, in two halves:

**Safety** — the change is a no-op on the wire.  The DTO field names, order and
types were generated from the pre-#1849 spec, and the byte-for-byte comparison
against the committed golden fixture proved the projection still reproduced it
unmodified.  Since #2899 that fixture lives in the coord-tui repo, so the
byte-comparison is coord-tui's CI gate rather than a test here (see
`tests/test_board_fixture.py`'s header).  That fixture is sorted-key JSON, so
it could never see ordering anyway; `test_board_wire_key_order_is_the_declared_
field_order` below pins the real wire order, and is unaffected by the move
because it projects a seeded DB directly.

**The feature** — adding a nullable column to any of the seven board tables
changes neither `/board`'s response body nor `GET /openapi.json`.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord import board_schema
from coord.board_schema import (
    BOARD_PROJECTIONS,
    INTEGER_BACKED_BOOLEANS,
    BoardAssignment,
    json_fields,
    project_row,
)
from coord.config import Config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.openapi import dataclass_schema, declared_routes, spec_routes, validate_json_schema
from coord.serve_app import build_app as build_serve_app

#: The board tables whose `/board` shape this issue pinned to a DTO.
BOARD_TABLES = tuple(BOARD_PROJECTIONS)

#: A column name no DTO declares — stands in for "the next `ALTER TABLE ...
#: ADD COLUMN` someone lands in coord/db.py".
NEW_COLUMN = "some_future_migration_column"


# ── fixtures ────────────────────────────────────────────────────────────────

def _seeded_db(path: Path, *, extra_column_on: str | None = None) -> Path:
    """Build the #748 seeded fixture DB at *path*.

    When *extra_column_on* names a table, a nullable column is added to it
    **after** the rows land and then populated, so the test proves the column
    is excluded by the DTO rather than merely being NULL everywhere.
    """
    from scripts.gen_board_fixture import build_fixture_db

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    build_fixture_db(conn)
    # The #748 generator seeds no `drive_escalations` row (the golden fixture
    # ships an empty list), and an empty table would make every assertion
    # below vacuously true for that projection — so add one here rather than
    # widening the committed fixture.
    conn.execute(
        "INSERT INTO drive_escalations (repo_name, issue_number, stage, "
        "assignment_id, reason, gate_readings, proposed_command, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            "claude-coordinator", 1849, "merge", "work-1849",
            "NEEDS_ATTENTION", "ci=failing", "coord merge --repo x",
            1000001300.0,
        ),
    )
    conn.commit()
    if extra_column_on is not None:
        conn.execute(f"ALTER TABLE {extra_column_on} ADD COLUMN {NEW_COLUMN} TEXT")
        conn.execute(f"UPDATE {extra_column_on} SET {NEW_COLUMN} = 'leaked'")  # noqa: S608
        conn.commit()
    conn.close()
    return path


def _serve_client(db_path: Path) -> TestClient:
    return TestClient(build_serve_app(SqliteStore(db_path), Config(repos=[], machines=[])))


# ── safety: the wire did not move ───────────────────────────────────────────

def test_board_wire_key_order_is_the_declared_field_order(tmp_path: Path) -> None:
    """Every projected row's JSON key order is its DTO's field order.

    The golden fixture is written with `sort_keys=True`, so it proves the key
    *set* and values are unchanged but is blind to ordering — and JSON object
    order is part of a byte-identical wire.  This is the missing half.
    """
    payload = SqliteStore(_seeded_db(tmp_path / "coord.db")).board_projection()
    wire_key = {"drive_escalations": "escalations"}

    for table, cls in BOARD_PROJECTIONS.items():
        rows = payload[wire_key.get(table, table)]
        assert rows, f"the seeded fixture must carry at least one {table} row"
        expected = [f.name for f in dataclasses.fields(cls)]
        for row in rows:
            assert list(row) == expected, (
                f"{table}: /board key order drifted from {cls.__name__}'s field order"
            )


def test_briefing_is_off_the_board_but_still_on_the_detail_read(tmp_path: Path) -> None:
    """`assignments.briefing` is ~8 MB of an ~12 MB live payload.

    It is absent from the DTO — that is now the *only* thing keeping it off the
    board — while `GET /assignment/{id}`'s full read still serves it.
    """
    db = _seeded_db(tmp_path / "coord.db")
    store = SqliteStore(db)

    assert "briefing" not in {f.name for f in dataclasses.fields(BoardAssignment)}
    for row in store.board_projection()["assignments"]:
        assert "briefing" not in row

    aid = store.board_projection()["assignments"][0]["assignment_id"]
    assert "briefing" in store.get_assignment(aid)


def test_json_encoded_columns_arrive_decoded(tmp_path: Path) -> None:
    """A `list[str]`/`dict` annotation is what decodes a JSON-in-TEXT column.

    `review_findings` is the deliberate counter-example: it is annotated `str`
    because coord-tui consumes it as a raw JSON string (`Option<String>`), so
    decoding it would break that client.
    """
    payload = SqliteStore(_seeded_db(tmp_path / "coord.db")).board_projection()

    assert json_fields(BoardAssignment) == {
        "files_allowed", "files_forbidden", "required_gates",
        "plan", "smoke_tests", "test_plan",
    }
    for row in payload["assignments"]:
        assert isinstance(row["files_allowed"], list)
        assert row["review_findings"] is None or isinstance(row["review_findings"], str)
    for row in payload["drive_queue"]:
        assert isinstance(row["after_json"], list)
    for row in payload["machines"]:
        assert isinstance(row["capabilities"], list)


def test_project_row_reads_sqlite_row_column_names_not_values() -> None:
    """`sqlite3.Row` is a *sequence*: `"x" in row` tests its values.

    Treating it as a mapping silently projects every row to `{}` and blanks the
    whole board, so this pins the normalisation in `board_schema._as_dict`.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("dellserver", "dellserver", '["gtk"]', '["api"]'),
    )
    row = conn.execute("SELECT * FROM machines").fetchone()
    conn.close()

    assert project_row(BOARD_PROJECTIONS["machines"], row) == {
        "name": "dellserver",
        "host": "dellserver",
        "capabilities": ["gtk"],
        "repos": ["api"],
    }


def test_project_row_skips_fields_the_row_does_not_have() -> None:
    """A declared field absent from the row (an un-migrated DB) is omitted,
    not defaulted and not an exception — matching the old `dict(row)` path."""
    projected = project_row(BOARD_PROJECTIONS["machines"], {"name": "elitebook"})
    assert projected == {"name": "elitebook"}


# ── the feature: a new nullable column is invisible ─────────────────────────

@pytest.mark.parametrize("table", BOARD_TABLES)
def test_new_nullable_column_does_not_change_board_payload(
    tmp_path: Path, table: str
) -> None:
    """Acceptance: adding a nullable column to a board table changes nothing
    in `/board`'s output — byte-for-byte, key order included."""
    baseline = SqliteStore(_seeded_db(tmp_path / "base.db")).board_projection()
    migrated = SqliteStore(
        _seeded_db(tmp_path / "migrated.db", extra_column_on=table)
    ).board_projection()

    # Guard the guard: the column really is in the DB and really does come
    # back from `SELECT *` — i.e. the pre-#1849 projection *would* have
    # leaked it onto the wire.
    conn = sqlite3.connect(str(tmp_path / "migrated.db"))
    conn.row_factory = sqlite3.Row
    raw = conn.execute(f"SELECT * FROM {table}").fetchone()  # noqa: S608
    conn.close()
    assert raw[NEW_COLUMN] == "leaked"

    assert json.dumps(migrated) == json.dumps(baseline)
    assert NEW_COLUMN not in json.dumps(migrated)


@pytest.mark.parametrize("table", BOARD_TABLES)
def test_new_nullable_column_does_not_change_board_response_body(
    tmp_path: Path, table: str
) -> None:
    """The same assertion through the real HTTP handler, not just the store."""
    baseline = _serve_client(_seeded_db(tmp_path / "base.db")).get("/board")
    migrated = _serve_client(
        _seeded_db(tmp_path / "migrated.db", extra_column_on=table)
    ).get("/board")

    assert baseline.status_code == migrated.status_code == 200
    assert migrated.content == baseline.content


@pytest.mark.parametrize("table", BOARD_TABLES)
def test_new_nullable_column_does_not_change_openapi_spec(
    tmp_path: Path, table: str
) -> None:
    """Acceptance: the published contract is unmoved by a migration too.

    This is the assertion the old `PRAGMA table_info` build could not make —
    it read the live schema, so the very same `ALTER TABLE` rewrote the spec
    three clients are generated from.
    """
    baseline = _serve_client(_seeded_db(tmp_path / "base.db")).get("/openapi.json")
    migrated = _serve_client(
        _seeded_db(tmp_path / "migrated.db", extra_column_on=table)
    ).get("/openapi.json")

    assert baseline.status_code == migrated.status_code == 200
    assert migrated.json() == baseline.json()
    assert NEW_COLUMN not in json.dumps(migrated.json())


def test_board_response_schema_never_touches_sqlite(monkeypatch) -> None:
    """The strongest form of the criterion above: the published `/board`
    schema is built with **no database at all**.

    The parametrised spec test can only add a column to a live DB; the old
    `PRAGMA table_info` build read a *freshly-migrated in-memory* DB instead,
    so it drifted whenever `coord/db.py`'s DDL changed rather than whenever a
    deployed DB did.  Making any SQLite connection fatal for the duration
    proves the DDL is not consulted at all, whichever way it moves.
    """
    from coord.serve_app import _board_response_schema

    def _boom(*args, **kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError(
            "_board_response_schema opened a SQLite connection — the /board "
            "wire contract must come from coord/board_schema.py, not the DDL"
        )

    monkeypatch.setattr(sqlite3, "connect", _boom)
    components: dict = {}
    schema = _board_response_schema(components)

    assert schema["properties"]["assignments"]["items"] == {
        "$ref": "#/components/schemas/BoardAssignment"
    }
    for cls in BOARD_PROJECTIONS.values():
        assert components[cls.__name__]["properties"], f"{cls.__name__} came out empty"


# ── the DTO pins the JSON type, not the storage engine ──────────────────────

@pytest.mark.parametrize("column", sorted(INTEGER_BACKED_BOOLEANS))
def test_integer_backed_booleans_are_declared_integers(column: str) -> None:
    """The #632-class guard, moved up to the DTO.

    These columns are semantically booleans stored as `INTEGER`, so they ship
    as `0`/`1`.  Under Postgres they would be real `BOOLEAN`s and ship
    `true`/`false` — which fails the parse of the *entire* `BoardPayload` and
    blanks the whole TUI board.  Pinning the JSON type here makes the wire
    shape a property of the contract rather than of whichever engine is
    underneath, so a backend swap cannot reintroduce the failure.
    """
    components: dict = {}
    for cls in BOARD_PROJECTIONS.values():
        dataclass_schema(cls, components)

    seen = False
    for cls in BOARD_PROJECTIONS.values():
        schema = components[cls.__name__]["properties"].get(column)
        if schema is None:
            continue
        seen = True
        assert schema["type"] == "integer", (
            f"{cls.__name__}.{column} must stay a JSON integer on the wire "
            "(see coord/board_schema.INTEGER_BACKED_BOOLEANS)"
        )
    assert seen, f"{column} is in INTEGER_BACKED_BOOLEANS but on no board DTO"


def test_no_board_dto_field_is_typed_bool() -> None:
    """Blanket form of the above: nothing on a board DTO may be a JSON
    `boolean`, because nothing in SQLite can produce one."""
    components: dict = {}
    for cls in BOARD_PROJECTIONS.values():
        dataclass_schema(cls, components)
    offenders = [
        f"{cls.__name__}.{name}"
        for cls in BOARD_PROJECTIONS.values()
        for name, schema in components[cls.__name__]["properties"].items()
        if schema.get("type") == "boolean"
    ]
    assert offenders == [], (
        f"{offenders} would ship as JSON true/false — SQLite sends 0/1 and the "
        "TUI would fail the whole BoardPayload parse (#632/#546/#628)"
    )


# ── the DDL coupling is actually gone ───────────────────────────────────────

def test_dao_no_longer_curates_drop_and_json_column_tables() -> None:
    """`_DROP_COLUMNS` / `_JSON_COLUMNS` were hand-curated patches over the
    leak; with an explicit schema an unwanted column is simply absent from the
    DTO and a JSON column is a typed field."""
    import coord.dao as dao

    assert not hasattr(dao, "_DROP_COLUMNS")
    assert not hasattr(dao, "_JSON_COLUMNS")


def test_openapi_module_no_longer_introspects_sqlite() -> None:
    """`sqlite_table_schema` had exactly two call sites (the daemon's seven
    board projections and the dashboard's `BoardDriveQueueEntry`); both now go
    through `dataclass_schema`, so the helper — and any knowledge of SQLite in
    `coord/openapi.py` — is gone."""
    import coord.openapi as openapi

    assert not hasattr(openapi, "sqlite_table_schema")
    assert "sqlite3" not in vars(openapi)


def test_board_dtos_and_projection_cannot_drift(tmp_path: Path) -> None:
    """The published schema and the served projection come from one source.

    Asserted the hard way: every key the projection emits must be a declared
    property of the schema `/openapi.json` advertises for that table.
    """
    client = _serve_client(_seeded_db(tmp_path / "coord.db"))
    spec = client.get("/openapi.json").json()
    payload = client.get("/board").json()
    wire_key = {"drive_escalations": "escalations"}

    for table, cls in BOARD_PROJECTIONS.items():
        declared = set(spec["components"]["schemas"][cls.__name__]["properties"])
        for row in payload[wire_key.get(table, table)]:
            assert set(row) <= declared, (
                f"{table}: {set(row) - declared} on the wire but not in the spec"
            )


def test_board_schema_still_validates_the_golden_fixture(tmp_path: Path) -> None:
    """The generated `/board` schema still fully specifies the real payload.

    #2899: generated in-process rather than read from a committed file. The
    golden fixture moved to the coord-tui repo with the crate, so there is no
    on-disk copy in this checkout — and the generator is the thing that
    produces that copy, so validating its live output is strictly the stronger
    assertion anyway (it cannot pass against a stale commit).
    """
    from scripts.gen_board_fixture import fixture_json_text

    fixture = json.loads(fixture_json_text())
    client = _serve_client(_seeded_db(tmp_path / "coord.db"))
    spec = client.get("/openapi.json").json()
    board = spec["paths"]["/board"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    errors = validate_json_schema(fixture, board, spec["components"]["schemas"])
    assert errors == [], f"golden /board fixture fails the generated schema: {errors}"


def test_route_drift_check_still_holds(tmp_path: Path) -> None:
    """#757's acceptance criterion is unaffected by the schema swap."""
    client = _serve_client(_seeded_db(tmp_path / "coord.db"))
    spec = client.get("/openapi.json").json()
    assert declared_routes(client.app.routes) == spec_routes(spec)


def test_every_projected_table_has_a_dto() -> None:
    """The seven projections `coord/serve_app.py` publishes are exactly the
    seven DTOs — a table added to one and not the other is a wire leak."""
    assert set(BOARD_PROJECTIONS) == {
        "assignments", "machines", "merge_queue", "proposals",
        "issues", "drive_escalations", "drive_queue",
    }
    assert all(dataclasses.is_dataclass(cls) for cls in BOARD_PROJECTIONS.values())
    assert board_schema.decode_row("notifications", {"id": 1}) == {"id": 1}
