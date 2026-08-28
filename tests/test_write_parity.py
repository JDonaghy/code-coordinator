"""#2885: the suite that runs — and deliberately breaks — the write-parity oracle.

``tests/write_parity.py`` is the instrument (its module docstring argues Option
B over Option A, and states what the harness does and does not tolerate).  This
file is the suite that:

1. runs it **sqlite vs sqlite** and asserts a clean report — the no-false-positive
   floor, which is also what proves the deterministic clock actually pins every
   timestamp column;
2. runs it **sqlite vs postgres** when a server happens to be reachable — the
   #829 cutover check, skipped (never silently passed) otherwise;
3. asserts the recorded workload still reaches **every table ``/board``
   projects**, so widening ``BOARD_PROJECTIONS`` without widening the corpus
   fails here rather than on cutover day;
4. **injects real divergences and asserts the harness catches each one.**  An
   oracle that has never failed is not an oracle (#2096), and the headline
   injection is the one #2885's acceptance names: reverting
   ``coord.state._UPSERT_SQL`` to ``INSERT OR REPLACE``, whose DELETE+INSERT
   resets every column the statement does not mention.
"""

from __future__ import annotations

import json

import pytest

from coord import state
from coord.board_schema import BOARD_PROJECTIONS
from tests import backends, write_parity
from tests.write_parity import (
    KIND_CELL,
    KIND_ROW_ONLY_IN_A,
    KIND_ROW_ONLY_IN_B,
    WORKLOAD,
    Difference,
    Dump,
    ParityReport,
    compare_backends,
    compare_dumps,
    dump_database,
    open_prepared,
    run_and_dump,
    run_workload,
)

#: Every table the ``/board`` payload projects, plus the three collections
#: ``SqliteStore.board_projection()`` serves that have no wire DTO of their own
#: (``notifications`` is passed through raw, ``plans`` is JSON-decoded, and
#: ``board_meta`` carries ``round_number``).  Derived from
#: ``coord.board_schema`` rather than hardcoded so the coverage assertion below
#: tightens automatically when the wire grows a table.
BOARD_TABLES: frozenset[str] = frozenset(BOARD_PROJECTIONS) | {
    "notifications",
    "plans",
    "board_meta",
}


def _insert_or_replace_variant() -> str:
    """``coord.state._UPSERT_SQL`` reverted to its pre-#2726 DELETE+INSERT form.

    Derived from the live statement — take everything before ``ON CONFLICT`` and
    swap ``INSERT INTO`` for ``INSERT OR REPLACE INTO`` — rather than pasted as
    a literal, so the injected regression stays a faithful "same INSERT, old
    conflict semantics" as the real statement's 41-column list evolves.  A
    pasted copy would rot into "an unrelated statement that also fails".
    """
    head, sep, _ = state._UPSERT_SQL.partition("ON CONFLICT")
    assert sep, "coord.state._UPSERT_SQL no longer contains an ON CONFLICT clause"
    mutated = head.replace(
        "INSERT INTO assignments", "INSERT OR REPLACE INTO assignments", 1
    )
    assert mutated != head, "coord.state._UPSERT_SQL no longer starts with INSERT INTO"
    return mutated


@pytest.fixture
def control_dump() -> Dump:
    """The unmutated SQLite run — the baseline every injection is diffed against."""
    with open_prepared("sqlite") as conn:
        return run_and_dump(conn, label="control")


def _mutated_dump(label: str) -> Dump:
    """A second SQLite run, executed with whatever the caller has monkeypatched."""
    with open_prepared("sqlite") as conn:
        return run_and_dump(conn, label=label)


# ── 1. the floor: the harness must not invent differences ────────────────────


def test_two_identical_sqlite_runs_report_no_differences() -> None:
    """The no-false-positive floor.

    Every timestamp column in the dump is stamped by production code calling
    ``time.time()``; if :class:`~tests.write_parity.StepClock` failed to pin any
    of them, two runs milliseconds apart would differ and this fails.  It is
    therefore also the test that keeps ``_CLOCKED_MODULES`` honest.
    """
    report = compare_backends("sqlite", "sqlite")
    assert report.is_clean, report.render()


def test_a_clean_report_still_renders_a_diff_not_a_boolean() -> None:
    report = compare_backends("sqlite", "sqlite")
    rendered = report.render()
    assert "write-path parity: sqlite#1 vs sqlite#2" in rendered
    assert "tables compared" in rendered
    assert "no differences" in rendered


def test_report_has_no_truthiness_protocol() -> None:
    """#2885 asks for a diff, not a boolean.

    ``bool(report)`` on a dataclass with no ``__bool__``/``__len__`` is always
    ``True``, so a caller who writes ``if report:`` gets a constant rather than
    an answer.  That is deliberate — the failure mode of *defining* truthiness
    is far worse (``if not report:`` would silently mean "clean") — and this
    pins it so nobody adds one thinking they are being helpful.
    """
    assert not hasattr(ParityReport, "__bool__")
    assert not hasattr(ParityReport, "__len__")


# ── 2. the cutover check ─────────────────────────────────────────────────────


def test_sqlite_and_postgres_agree_on_the_write_workload() -> None:
    """#829's actual question, asked directly.

    Skipped — loudly, with the reason — when no Postgres is reachable, because
    a laptop with no server must not get a green tick that means nothing.  This
    is expected to FAIL until #829's bring-up work lands; that failure is the
    instrument working, and its report is the worklist.
    """
    unavailable = backends.postgres_available()
    if unavailable:
        pytest.skip(f"no Postgres backend available: {unavailable}")
    report = compare_backends("sqlite", "postgres")
    assert report.is_clean, report.render()


# ── 3. coverage of the corpus ────────────────────────────────────────────────


def test_workload_writes_every_table_the_board_projects(control_dump: Dump) -> None:
    """#2885 acceptance: "covers, at minimum, every table ``/board`` projects".

    Asserted against the *dump* rather than against the steps' declared
    ``tables``, so a step that silently stops writing (the way
    ``_save_config_snapshot`` does on a thin-client host, swallowing its own
    exceptions) fails here instead of quietly comparing two empty tables.
    """
    empty = sorted(t for t in BOARD_TABLES if control_dump.row_count(t) == 0)
    assert not empty, (
        f"the recorded workload left {empty} empty, so the harness would compare "
        "two empty tables and call it parity; add a step to WORKLOAD"
    )


def test_every_workload_step_declares_tables_it_actually_writes() -> None:
    """Each step's ``tables`` must name real tables — the report cites them."""
    with open_prepared("sqlite") as conn:
        known = set(write_parity.table_names(conn))
    for step in WORKLOAD:
        assert step.tables, f"step {step.name!r} declares no tables"
        unknown = sorted(set(step.tables) - known)
        assert not unknown, f"step {step.name!r} names unknown table(s) {unknown}"


def test_workload_step_names_are_unique() -> None:
    names = [step.name for step in WORKLOAD]
    assert len(names) == len(set(names))


def test_dump_covers_every_table_in_the_schema() -> None:
    """The comparison is over the whole database, not a chosen subset.

    ``table_names`` is discovered from the live schema, so this also pins the
    property that a table added to ``coord/db.py`` joins the diff with no edit
    to the harness.
    """
    with open_prepared("sqlite") as conn:
        discovered = set(write_parity.table_names(conn))
        dumped = set(dump_database(conn, label="x").rows)
    assert discovered == dumped
    assert BOARD_TABLES <= discovered
    # The schema is ~28 tables; assert the floor rather than an exact count so
    # a new table does not fail this test for the wrong reason.
    assert len(discovered) >= 25, sorted(discovered)


# ── 4. falsifiability: the oracle must fail on a real divergence ─────────────


def test_catches_insert_or_replace_reverting_a_do_update_site(
    control_dump: Dump, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline injection named by #2885's acceptance.

    ``coord.state._UPSERT_SQL`` is the tree's richest write path — a 41-column
    upsert whose ``DO UPDATE`` clause is nothing but semantic guards.  Reverted
    to ``INSERT OR REPLACE``, its DELETE+INSERT resets every column the INSERT
    list does not mention.  ``repo_github``, ``briefing``, ``provider_name`` and
    ``driven_by`` are written by the *dispatch* INSERT and never re-supplied by
    the board snapshot, so they are exactly the "unmentioned columns reset to
    defaults" hazard — invisible on a single backend, and invisible to "the
    suite passed".
    """
    monkeypatch.setattr(state, "_UPSERT_SQL", _insert_or_replace_variant())
    report = compare_dumps(control_dump, _mutated_dump("insert-or-replace"))

    assert not report.is_clean, "the harness did not notice a DELETE+INSERT upsert"
    assert "assignments" in report.tables_with_differences()

    reset = {
        d.column: (d.value_a, d.value_b)
        for d in report.for_table("assignments")
        if d.kind == KIND_CELL
    }
    # ``briefing`` resets to the empty string rather than NULL — the INSERT
    # column list does carry it, but only as ``a.briefing or ""`` off a board
    # snapshot that has never held it (#1337).  Either way the dispatch-time
    # value is gone, which is what "reset to its default" means here.
    for column in ("repo_github", "briefing", "provider_name", "driven_by"):
        assert column in reset, f"{column} was not reported; got {sorted(reset)}"
        control_value, mutated_value = reset[column]
        assert control_value, f"{column} was not populated in the control run"
        assert not mutated_value, (
            f"{column} should have been reset to its default by DELETE+INSERT, "
            f"got {mutated_value!r}"
        )


def test_catches_the_test_verdict_clobber_a_do_update_site_prevents(
    control_dump: Dump, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #1482/#1451 guards, measured rather than asserted at the call site.

    ``_UPSERT_SQL`` deliberately excludes ``test_state``/``test_reason``/
    ``smoke_test`` from its ``ON CONFLICT`` clause and CASes ``status``/
    ``finished_at`` against the stored ``finished_at`` — so the stale board
    snapshot the workload replays last must not undo the recorded verdict.
    Under ``INSERT OR REPLACE`` it does, and the harness has to say so by
    column name.
    """
    monkeypatch.setattr(state, "_UPSERT_SQL", _insert_or_replace_variant())
    report = compare_dumps(control_dump, _mutated_dump("insert-or-replace"))

    cells = {d.column: d for d in report.for_table("assignments") if d.kind == KIND_CELL}

    assert cells["test_state"].value_a == "passed"
    assert cells["test_state"].value_b is None
    assert cells["status"].value_a == "done"
    assert cells["status"].value_b == "running"
    assert cells["finished_at"].value_a is not None
    assert cells["finished_at"].value_b is None


def test_the_injected_regression_is_reported_as_readable_text(
    control_dump: Dump, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diff a human can act on — table, row key, column, both values."""
    monkeypatch.setattr(state, "_UPSERT_SQL", _insert_or_replace_variant())
    rendered = compare_dumps(control_dump, _mutated_dump("insert-or-replace")).render()

    assert "write-path parity: control vs insert-or-replace" in rendered
    assert "assignments:" in rendered
    assert "assignments['work-2885a'].repo_github:" in rendered
    assert "control='acme/api'" in rendered
    assert "insert-or-replace=None" in rendered


def test_catches_a_write_that_silently_does_nothing(control_dump: Dump) -> None:
    """The other real-world shape: a step that fails and is swallowed.

    ``_save_config_snapshot`` catches every exception so a CLI never aborts on
    a non-critical snapshot write — which on a second backend would present as
    an empty ``machines`` table rather than as an error.  Dropping the step
    reproduces that exactly.
    """
    reduced = tuple(s for s in WORKLOAD if s.name != "snapshot_config")
    with open_prepared("sqlite") as conn:
        dump_b = run_and_dump(conn, label="no-snapshot", workload=reduced)

    report = compare_dumps(control_dump, dump_b)
    assert not report.is_clean
    assert "machines" in report.tables_with_differences()
    assert all(
        d.kind == KIND_ROW_ONLY_IN_A for d in report.for_table("machines")
    ), report.render()
    # board_meta loses the eight snapshot keys but keeps save_board's two.
    board_meta_only_in_a = [
        d.key for d in report.for_table("board_meta") if d.kind == KIND_ROW_ONLY_IN_A
    ]
    assert "'pipeline_default_gates'" in board_meta_only_in_a


def test_catches_an_extra_row_on_one_side(control_dump: Dump) -> None:
    """A duplicate write — the shape a broken ``ON CONFLICT DO NOTHING`` takes."""
    extra = write_parity.WorkloadStep(
        "extra_escalation",
        ("drive_escalations",),
        lambda: state._record_drive_escalation_local(
            "api",
            1,
            stage="test",
            reason="EXTRA",
            gate_readings="",
            proposed_command="coord test",
        ),
    )
    with open_prepared("sqlite") as conn:
        dump_b = run_and_dump(conn, label="extra-row", workload=(*WORKLOAD, extra))

    report = compare_dumps(control_dump, dump_b)
    assert not report.is_clean
    escalations = report.for_table("drive_escalations")
    assert [d.kind for d in escalations] == [KIND_ROW_ONLY_IN_B], report.render()


def test_catches_a_value_divergence_in_a_json_column(control_dump: Dump) -> None:
    """A single cell, one table, one column — the finest grain the report has."""
    with open_prepared("sqlite") as conn:
        run_workload(conn)
        from coord import db

        previous = db._conn
        try:
            db.override_connection(conn)
            from coord import sql

            sql.execute(
                conn,
                "UPDATE issues SET labels = ? WHERE repo_name = ? AND number = ?",
                (json.dumps(["coord", "drifted"]), "api", 2885),
            )
            conn.commit()
        finally:
            db.override_connection(previous)
        dump_b = dump_database(conn, label="drifted")

    report = compare_dumps(control_dump, dump_b)
    issues = report.for_table("issues")
    assert len(issues) == 1, report.render()
    assert issues[0].kind == KIND_CELL
    assert issues[0].column == "labels"
    assert issues[0].key == "'api'|2885"


# ── the diff engine's own edges ──────────────────────────────────────────────


def _dump(label: str, rows: dict, *, keys: dict, columns: dict) -> Dump:
    return Dump(label=label, columns=columns, keys=keys, rows=rows)


def test_compare_reports_a_table_present_on_only_one_side() -> None:
    columns = {"t": ("id",)}
    keys = {"t": ("id",)}
    a = _dump("a", {"t": [{"id": "#1"}]}, keys=keys, columns=columns)
    b = _dump("b", {}, keys={}, columns={})
    report = compare_dumps(a, b)
    assert [d.kind for d in report.differences] == ["table-only-in-a"]
    assert "present in a, absent in b" in report.render()


def test_compare_reports_a_column_list_mismatch_without_row_noise() -> None:
    a = _dump(
        "a",
        {"t": [{"id": "#1", "extra": 1}]},
        keys={"t": ("id",)},
        columns={"t": ("id", "extra")},
    )
    b = _dump("b", {"t": [{"id": "#1"}]}, keys={"t": ("id",)}, columns={"t": ("id",)})
    report = compare_dumps(a, b)
    assert [d.kind for d in report.differences] == ["columns"], report.render()
    assert "column list differs" in report.render()


def test_compare_falls_back_to_row_matching_without_a_primary_key() -> None:
    columns = {"t": ("v",)}
    keys: dict[str, tuple[str, ...]] = {"t": ()}
    a = _dump("a", {"t": [{"v": 1}, {"v": 2}]}, keys=keys, columns=columns)
    b = _dump("b", {"t": [{"v": 2}, {"v": 3}]}, keys=keys, columns=columns)
    report = compare_dumps(a, b)
    kinds = sorted(d.kind for d in report.differences)
    assert kinds == [KIND_ROW_ONLY_IN_A, KIND_ROW_ONLY_IN_B]


def test_surrogate_keys_are_ranked_so_sequence_gaps_are_tolerated() -> None:
    """A Postgres identity sequence is not rolled back by a failed statement;
    a SQLite ROWID is.  Absolute values may therefore differ where relative
    order does not, and only order is load-bearing."""
    rows_a = [{"id": 1, "v": "x"}, {"id": 2, "v": "y"}]
    rows_b = [{"id": 7, "v": "x"}, {"id": 41, "v": "y"}]
    ranked_a = write_parity._rank_surrogate_keys(rows_a, ("id",))
    ranked_b = write_parity._rank_surrogate_keys(rows_b, ("id",))
    assert ranked_a == ranked_b == [{"id": "#1", "v": "x"}, {"id": "#2", "v": "y"}]


def test_surrogate_ranking_leaves_natural_keys_alone() -> None:
    """``issues(repo_name, number)`` and ``plans(assignment_id)`` are data, not
    surrogates — ranking them away would hide a genuinely wrong key."""
    rows = [{"repo_name": "api", "number": 2885}]
    assert write_parity._rank_surrogate_keys(rows, ("repo_name", "number")) == rows
    plans = [{"assignment_id": "work-2885a"}]
    assert write_parity._rank_surrogate_keys(plans, ("assignment_id",)) == plans


def test_boolean_is_not_folded_into_an_integer() -> None:
    """#632/#546/#628: an INTEGER-backed flag arriving as a Python ``bool``
    ships as JSON ``true`` and fails the parse of the whole ``BoardPayload``.
    The dump must therefore report ``True`` and ``1`` as different."""
    columns = {"t": ("k", "flag")}
    keys = {"t": ("k",)}
    a = _dump("a", {"t": [{"k": "x", "flag": 1}]}, keys=keys, columns=columns)
    b = _dump("b", {"t": [{"k": "x", "flag": True}]}, keys=keys, columns=columns)
    report = compare_dumps(a, b)
    assert [d.column for d in report.differences] == ["flag"]


def test_integer_and_float_are_not_conflated() -> None:
    """``1 == 1.0`` in Python, but a JSON ``1`` and a JSON ``1.0`` deserialise
    differently into a typed Rust struct — same class of wire break as the
    bool/int split above."""
    assert write_parity.values_differ(1, 1.0)
    assert write_parity.values_differ(True, 1)
    assert not write_parity.values_differ(1, 1)
    assert not write_parity.values_differ(None, None)
    assert not write_parity.values_differ("x", "x")
    assert write_parity.values_differ(None, "")


def test_unkeyed_row_matching_is_also_type_aware() -> None:
    """The primary-key-less fallback must not lose the bool/int distinction
    just because it compares whole rows."""
    columns = {"t": ("flag",)}
    keys: dict[str, tuple[str, ...]] = {"t": ()}
    a = _dump("a", {"t": [{"flag": 1}]}, keys=keys, columns=columns)
    b = _dump("b", {"t": [{"flag": True}]}, keys=keys, columns=columns)
    report = compare_dumps(a, b)
    assert sorted(d.kind for d in report.differences) == [
        KIND_ROW_ONLY_IN_A,
        KIND_ROW_ONLY_IN_B,
    ]


def test_decimal_is_coerced_to_float() -> None:
    """The one value normalisation: psycopg decodes NUMERIC to ``Decimal``
    where sqlite3 hands back a ``float`` for the same stored number."""
    from decimal import Decimal

    assert write_parity._canonical_value(Decimal("0.42")) == 0.42


def test_floats_are_not_rounded() -> None:
    """SQLite's REAL is 8-byte and Postgres's is 4-byte, so a ``cost_usd`` that
    comes back ``0.41999998688697815`` is a genuine finding, not noise."""
    columns = {"t": ("k", "cost")}
    keys = {"t": ("k",)}
    a = _dump("a", {"t": [{"k": "x", "cost": 0.42}]}, keys=keys, columns=columns)
    b = _dump(
        "b", {"t": [{"k": "x", "cost": 0.41999998688697815}]}, keys=keys, columns=columns
    )
    assert not compare_dumps(a, b).is_clean


def test_difference_renders_every_kind() -> None:
    """Every ``kind`` the diff engine emits has a rendering — a missing branch
    would print a cell-shaped line for a table-shaped difference."""
    kinds = [
        write_parity.KIND_TABLE_ONLY_IN_A,
        write_parity.KIND_TABLE_ONLY_IN_B,
        write_parity.KIND_COLUMNS,
        KIND_ROW_ONLY_IN_A,
        KIND_ROW_ONLY_IN_B,
        KIND_CELL,
    ]
    for kind in kinds:
        rendered = Difference("t", kind, key="k", column="c", value_a=1, value_b=2).render(
            "left", "right"
        )
        assert rendered.strip(), kind
        assert "t" in rendered


# ── the workload runner's own contract ───────────────────────────────────────


def test_a_failing_step_names_itself_rather_than_being_swallowed() -> None:
    """A step that crashes on one backend must not present as an empty table."""
    boom = write_parity.WorkloadStep(
        "explode", ("assignments",), lambda: (_ for _ in ()).throw(ValueError("nope"))
    )
    with open_prepared("sqlite") as conn:
        with pytest.raises(write_parity.WorkloadStepError, match="'explode'"):
            run_workload(conn, workload=(boom,))


def test_run_workload_restores_the_connection_and_the_stubbed_seams(
    coord_db,
) -> None:
    """The harness runs *inside* a suite whose autouse fixture already installed
    a connection; leaving ``coord.db``'s singleton (or ``coord.state``'s config
    /board-service seams) pointed somewhere else would corrupt every later test
    in the file."""
    from coord import db

    before_conn = db._conn
    before_gate = state._dispatch_target_config
    before_service = state._board_service

    with open_prepared("sqlite") as conn:
        run_workload(conn)

    assert db._conn is before_conn
    assert state._dispatch_target_config is before_gate
    assert state._board_service is before_service


def test_clock_is_pinned_per_step_not_per_call() -> None:
    """A per-*call* counter would drift the moment one backend made a different
    number of ``time.time()`` calls (a ``retry_on_locked`` retry, a
    dialect-specific branch)."""
    clock = write_parity.StepClock()
    clock.advance()
    assert clock() == clock() == clock()
    first = clock()
    clock.advance()
    assert clock() > first


# ── the CLI ──────────────────────────────────────────────────────────────────


def test_cli_prints_the_report_and_exits_zero_when_clean(capsys) -> None:
    exit_code = write_parity.main(["--a", "sqlite", "--b", "sqlite"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "write-path parity:" in out
    assert "no differences" in out


def test_cli_reports_an_unreachable_backend_distinctly(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """"Postgres isn't reachable" and "Postgres is reachable and wrong" must be
    distinguishable by exit status — an operator scripting the cutover check
    reads one as "go install a server" and the other as "stop the cutover"."""
    monkeypatch.setattr(backends, "postgres_available", lambda: "no server here")
    exit_code = write_parity.main(["--b", "postgres"])
    captured = capsys.readouterr()
    assert exit_code == write_parity.EXIT_UNAVAILABLE
    assert "no server here" in captured.err
    assert captured.out == ""


def test_cli_exits_non_zero_when_the_backends_disagree(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The status an operator actually branches on.

    The divergence is injected by giving the *second* side a shortened workload
    — the same "a write silently did nothing on one backend" shape as
    :func:`test_catches_a_write_that_silently_does_nothing`, driven all the way
    through ``main()`` so the exit code and the printed report are both real.
    """
    real_run_and_dump = write_parity.run_and_dump
    reduced = tuple(s for s in WORKLOAD if s.name != "snapshot_config")
    sides: list[str] = []

    def _second_side_skips_a_step(conn, *, label, workload=WORKLOAD):
        sides.append(label)
        chosen = workload if len(sides) == 1 else reduced
        return real_run_and_dump(conn, label=label, workload=chosen)

    monkeypatch.setattr(write_parity, "run_and_dump", _second_side_skips_a_step)
    exit_code = write_parity.main([])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "difference(s)" in out
    assert "machines:" in out
