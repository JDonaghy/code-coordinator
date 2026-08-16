"""Tests for ``coord.dao`` — the read-only board data-access layer (#584/#589).

#1823 narrowed ``CoordStore`` to the read contract it actually serves and
deleted three ``NotImplementedError`` write stubs (``record_result`` /
``record_completion`` / ``record_dispatched``) that described a design which
was not taken — routing writes through the daemon landed in ``coord.state`` +
``coord.board_service`` (#590), not here.  These tests guard that narrowing so
the stubs (or the misleading "write side declared for #590" docstring) do not
creep back.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from coord.dao import CoordStore, SqliteStore
from coord.db import _ensure_schema


@pytest.fixture
def read_db(tmp_path: Path) -> Path:
    """An on-disk, schema-migrated ``coord.db`` for read-only ``SqliteStore``.

    Empty is enough — every read method returns ``[]``/``None``/``{}`` against a
    migrated DB with no rows, so we can invoke the whole read contract without
    seeding data.  The writer commits and closes before ``SqliteStore`` opens
    its own ``mode=ro`` connection.
    """
    p = tmp_path / "coord.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    conn.close()
    return p


def _dummy_for(param: inspect.Parameter) -> object:
    """A safe positional arg for *param* — ``""`` for str, ``0`` for int, else
    ``None``.  Used only to drive each read method's body so a
    ``NotImplementedError`` (the #1823 dead stub) is observable."""
    ann = param.annotation
    if ann is inspect.Parameter.empty:
        return None
    if ann is str or ann == "str":
        return ""
    if ann is int or ann == "int":
        return 0
    return None


def test_coordstore_protocol_methods_are_all_implemented(read_db: Path) -> None:
    """Every ``CoordStore`` protocol method is callable on ``SqliteStore`` and
    none raise ``NotImplementedError``.

    Named regression guard for #1823.  ``dao.py`` used to declare three write
    stubs (``record_result`` / ``record_completion`` / ``record_dispatched``)
    that raised ``NotImplementedError`` pointing at #590.  #590 landed in
    ``coord.state`` + ``coord.board_service`` instead, so the stubs were dead
    code.  This test enumerates the protocol and invokes each method on a
    ``SqliteStore`` — it MUST fail against the pre-#1823 code (the stubs were
    protocol members and raised when called) and pass once the protocol is
    narrowed to the read contract that is actually served.
    """
    store = SqliteStore(read_db)
    protocol_methods = sorted(
        name
        for name, value in vars(CoordStore).items()
        if callable(value) and not name.startswith("_")
    )
    assert protocol_methods, "CoordStore declared no methods — enumeration is broken"

    for name in protocol_methods:
        bound = getattr(store, name, None)
        assert callable(bound), f"SqliteStore is missing protocol method {name!r}"
        args = [_dummy_for(p) for p in inspect.signature(bound).parameters.values()]
        try:
            bound(*args)
        except NotImplementedError as exc:
            pytest.fail(
                f"CoordStore.{name}() raised NotImplementedError — a dead write "
                f"stub leaked back into the read protocol: {exc}"
            )
        except Exception:  # noqa: BLE001 — not the contract under test
            # Read methods run against a migrated DB with dummy args, so they
            # normally return empty/None.  Any *other* error is tolerated: the
            # #1823 contract is solely "no NotImplementedError stubs".
            pass


def test_coordstore_protocol_omits_dead_write_stubs() -> None:
    """The three ``NotImplementedError`` write stubs removed in #1823 must stay
    out of the read protocol and off ``SqliteStore`` — writes live in
    ``coord.state``'s ``_*_local()`` family, not here."""
    names = {n for n in vars(CoordStore) if not n.startswith("_")}
    assert "record_result" not in names
    assert "record_completion" not in names
    assert "record_dispatched" not in names
    for dead in ("record_result", "record_completion", "record_dispatched", "_not_yet"):
        assert not hasattr(SqliteStore, dead), (
            f"SqliteStore still carries dead stub {dead!r}"
        )


def test_coordstore_is_runtime_checkable_against_sqlite_store(read_db: Path) -> None:
    """``SqliteStore`` satisfies the narrowed ``CoordStore`` read protocol —
    the ``runtime_checkable`` ``isinstance`` still holds after the write
    methods were dropped from both the protocol and the class."""
    store = SqliteStore(read_db)
    assert isinstance(store, CoordStore)
