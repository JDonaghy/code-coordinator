"""#2884: the classification of every hardcoded ``sqlite3.connect`` in ``tests/``,
and a ratchet that keeps it true.

Why a ratchet
-------------
``tests/conftest.py``'s ``coord_db`` fixture is autouse and routes through
``coord.db.override_connection()``, so #2884 could point the *whole* suite at
a second backend by changing one function (``tests/backends.py``).  The 38
files that build their **own** ``sqlite3.connect`` are the exception, and
they are the thing that quietly re-couples the suite to SQLite: without a
ratchet the next 50 tests reintroduce the coupling one call site at a time,
exactly the way #2689/#2802/#2846 reintroduced one bug shape one call site at
a time until ``tests/test_lock_contention_ratchet.py`` existed.

So this file does two things:

1. **Records the classification** (:data:`SQLITE_CONNECT_ALLOWLIST`) so the
   next reader does not have to re-derive it — including the judgement calls,
   which are the part that is expensive to redo and cheap to write down.
2. **Fails when a new, unclassified ``sqlite3.connect`` appears in
   ``tests/``**, or when a classified file's count changes.  Adding one is
   allowed; adding one *silently* is not.

The three buckets
-----------------
``A`` — **legitimately SQLite-specific; leave alone.** The test is *about*
SQLite machinery: WAL/``journal_mode``, ``busy_timeout``, ``PRAGMA``
behaviour, ``sqlite_master``, ``ProductionDatabaseGuardError``,
``coord/db.py``'s ``_open()``/on-disk migration path, on-disk DB file backup,
or ``sqlite3.Row``'s own semantics.  These *should* keep hardcoding
``sqlite3.connect`` — a "portable" version of them would assert nothing.
Marked so nobody later "fixes" them.

``B`` — **should use the autouse fixture and doesn't.** A hand-rolled
``sqlite3.connect(":memory:")`` + ``_ensure_schema`` + ``override_connection``
that re-implements ``coord_db`` verbatim.  **This bucket is empty by
construction**: every member found by the #2884 audit was converted in the
same PR (``tests/test_db.py``'s ``isolated_conn``, two tests in
``tests/test_dispatch.py``, ``tests/test_board_fixture.py``'s
``_migrated_schema_columns``).  It is documented here because the *next*
audit's job is to keep it empty, and because the ratchet's failure message
needs to be able to name it as the likely fix.

``C`` — **genuinely needs a second, separate connection.** Two concrete,
recurring mechanisms in this tree, neither of which the autouse fixture can
serve:

  * ``coord/dao.py``'s ``SqliteStore`` opens its **own** ``mode=ro``
    connection *by path*.  It can never see another connection's
    ``:memory:`` database, so any test exercising the daemon's read path has
    to seed a real file DB.
  * Starlette's ``TestClient`` runs handlers on a worker thread.  The autouse
    fixture's connection is created without ``check_same_thread=False`` and
    is therefore unusable from that thread, so these tests install their own
    thread-safe ``rw_db`` override.

Both are *fixtures of the SQLite deployment shape*, not sloppiness — which is
why they are classified rather than converted.  A ``C`` site that wants to
follow the active backend should use
:func:`tests.backends.scratch_database`; a ``C`` site that needs a real
filesystem path (a subprocess, a CLI invocation, ``SqliteStore(db_path)``) is
inherently SQLite-shaped and stays as-is until #829 gives ``dao.py`` a
Postgres store.

Counts, not line numbers
------------------------
The allowlist pins a **per-file count**, not line numbers: line numbers churn
on every unrelated edit and would make this ratchet a permanent tax.  A count
is stable, it is trivially updated in the same commit that changes it, and it
still catches the thing that matters — a *new* coupling appearing.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent

BUCKET_A = "A"  # legitimately SQLite-specific — leave alone
BUCKET_B = "B"  # should use the autouse coord_db fixture (kept empty)
BUCKET_C = "C"  # genuinely needs a second/separate connection
BUCKET_HARNESS = "H"  # the backend harness itself


@dataclass(frozen=True)
class Classification:
    """One file's ``sqlite3.connect`` classification.

    *sites* is the total number of ``sqlite3.connect(...)`` **calls** in the
    file (AST calls, so a ``sqlite3.connect`` named in a comment or docstring
    doesn't count).  *buckets* lists every bucket represented in the file,
    most-populous first.  *why* is the record — write it for the reader who
    finds this file three years from now wondering whether they may delete a
    line.
    """

    sites: int
    buckets: tuple[str, ...]
    why: str


# ── The classification (audited 2026-08-28 for #2884) ─────────────────────────
SQLITE_CONNECT_ALLOWLIST: dict[str, Classification] = {
    # ── the harness itself ────────────────────────────────────────────────
    "backends.py": Classification(
        2, (BUCKET_HARNESS,),
        "This module IS the backend switch. One site is the default SQLite "
        "`:memory:` session behind the autouse coord_db fixture; the other is "
        "scratch_database()'s SQLite arm. Both are inside `if backend == "
        "sqlite` branches, which is the only correct place in tests/ to name "
        "a driver.",
    ),

    # ── A: legitimately SQLite-specific ───────────────────────────────────
    "test_db.py": Classification(
        37, (BUCKET_A,),
        "The canonical bucket-A file: it tests coord/db.py's SQLite "
        "connection machinery itself — pre-migration CREATE TABLEs read back "
        "with PRAGMA table_info, busy_timeout contention across two "
        "connections, override_connection/close semantics, _open()'s on-disk "
        "path + ProductionDatabaseGuardError, schema_version collapse. A "
        "portable rewrite would assert nothing. The file's autouse "
        "`isolated_conn` was the one bucket-B member here and is now an alias "
        "for coord_db.",
    ),
    "test_sql_dialect.py": Classification(
        7, (BUCKET_A,),
        "The SQLite half of the dialect seam's own tests: dialect detection "
        "from a real sqlite3 connection, journal_mode=WAL, "
        "busy_timeout/query_only pragmas, a `mode=ro` URI connection. These "
        "must hardcode the driver — asserting `detect_dialect(conn) == "
        "'sqlite'` against a connection whose type an env var chooses is "
        "circular.",
    ),
    "test_deploy_coord_db_backup.py": Classification(
        2, (BUCKET_A,),
        "On-disk coord.db file backup/copy and snapshot verification — the "
        "unit under test is a filesystem operation on a SQLite database file. "
        "Postgres has no analogue to back up this way.",
    ),
    "test_test_orchestrator.py": Classification(
        2, (BUCKET_A,),
        "Judgement call: _migrate_add_columns idempotency + PRAGMA "
        "table_info on a fresh schema. Mechanically these are 'soft B' (a "
        "bare :memory: conn, no override_connection, would work on the "
        "fixture conn) — but they exercise coord/db.py's *migration* "
        "machinery, the same subject as test_db.py, so they are filed A "
        "alongside it rather than split across two buckets.",
    ),
    "test_board_schema.py": Classification(
        3, (BUCKET_A, BUCKET_C),
        "Judgement call, reclassified from B during review: "
        "test_project_row_reads_sqlite_row_column_names_not_values exists "
        "specifically to pin that `sqlite3.Row` is a *sequence*, so `\"x\" in "
        "row` tests values not keys — the #632-class trap that blanks the "
        "whole board. Handing it a dict_row connection under "
        "COORD_TEST_BACKEND=postgres would silently stop testing the thing it "
        "was written to test. The other two sites are C: a seeded fixture DB "
        "for SqliteStore plus a reopen to prove a column really leaked.",
    ),

    # ── C: genuinely needs a second / separate connection ─────────────────
    "test_serve.py": Classification(
        14, (BUCKET_C, BUCKET_A),
        "12 C: file DBs handed to SqliteStore/TestClient, reopen-to-mutate "
        "checks, and the thread-safe `rw_db` override (two of them carry "
        "docstrings already stating the autouse conn is unusable from the "
        "TestClient worker thread). 2 A: PRAGMA journal_mode=WAL + the WAL "
        "checkpoint tick.",
    ),
    "test_board_read_path.py": Classification(
        8, (BUCKET_C,),
        "All file DBs for SqliteStore/TestClient; two are deliberate second "
        "connections to an already-existing file DB, asserting "
        "cross-connection visibility — the definition of bucket C.",
    ),
    "test_needs_attention.py": Classification(
        4, (BUCKET_C,),
        "Twice over: an inline thread-safe rw_db (write side) plus a separate "
        "file_db (SqliteStore read side). Two connections to one database is "
        "the whole point of the test.",
    ),
    "test_dispatch_target_validation.py": Classification(
        4, (BUCKET_C,),
        "Two class-scoped (rw_db, file_db) fixture pairs driving the daemon's "
        "HTTP write path.",
    ),
    "test_portal_store.py": Classification(
        3, (BUCKET_C,),
        "A file board.db opened with check_same_thread=False and also handed "
        "to SqliteStore by path.",
    ),
    "test_milestone_gate.py": Classification(
        3, (BUCKET_C,),
        "rw_db fixture plus two separate on-disk board.db files for "
        "SqliteStore.",
    ),
    "test_thin_client_daemon_routing_906.py": Classification(
        2, (BUCKET_C,),
        "rw_db + file_db: the thin client's routing is precisely the "
        "two-connection (daemon writes, store reads) shape.",
    ),
    "test_store_contract.py": Classification(
        2, (BUCKET_C,),
        "One throwaway :memory: DB built purely to *extract* the canonical "
        "backend-agnostic dataset — deliberately NOT the database under test, "
        "so it must not be the fixture conn. The other is the SQLite backend "
        "adapter's own file DB, i.e. the contract's SQLite arm.",
    ),
    "test_review_verdict_relay.py": Classification(
        2, (BUCKET_C,),
        "The standard daemon pair: a thread-safe rw_db override for the write "
        "side and a separate file_db that the SqliteStore read side opens by "
        "path. The relay assertion is precisely that a verdict written on one "
        "connection becomes visible on the other.",
    ),
    "test_review_verdict_override_audit.py": Classification(
        2, (BUCKET_C,),
        "Hand-rolled pre-#1456 schema databases passed to scan(path) as "
        "fixture data — the point is that they are NOT the current schema.",
    ),
    "test_reports.py": Classification(
        2, (BUCKET_C,),
        "_make_daemon_db builds an on-disk board that the report renderer "
        "reads back through SqliteStore, plus the thread-safe rw_db override "
        "the TestClient worker thread needs to write it.",
    ),
    "test_fleet_health_snapshot.py": Classification(
        2, (BUCKET_C,),
        "A detail_db file DB that SqliteStore opens by path, plus the "
        "thread-safe rw_db override for the snapshot endpoint served under "
        "TestClient.",
    ),
    "test_cli_milestone_remove_and_issue_close.py": Classification(
        2, (BUCKET_C,),
        "_make_file_db seeds a real board file for the SqliteStore-backed "
        "daemon endpoint the CLI talks to, plus the thread-safe rw_db "
        "override for the handler thread that mutates it.",
    ),
    "test_cli_milestone_assign.py": Classification(
        2, (BUCKET_C,),
        "_make_file_db seeds a real board file for the SqliteStore-backed "
        "daemon endpoint the CLI talks to, plus the thread-safe rw_db "
        "override for the handler thread that mutates it.",
    ),
    "test_board_cap_762.py": Classification(
        2, (BUCKET_C,),
        "A file DB for board_projection, plus a check_same_thread=False "
        "connection for the daemon threadpool sweep.",
    ),
    "test_approved_work_2532.py": Classification(
        2, (BUCKET_C,),
        "A thread-safe rw_db override for the write side plus a detail_db "
        "file that SqliteStore opens by path for the read side.",
    ),
    "test_dao.py": Classification(
        2, (BUCKET_C,),
        "coord/dao.py's own tests: a read_db fixture and a hand-rolled "
        "pre-migration DB, both opened by SqliteStore *by path*. SqliteStore "
        "is the SQLite arm of the store seam, so its tests own a SQLite "
        "connection by definition.",
    ),
    "test_serve_app_board_trim.py": Classification(
        1, (BUCKET_C,),
        "_seed_big_board writes an oversized board into a real file so the "
        "trim path can be exercised through SqliteStore, which opens that "
        "file with its own mode=ro connection.",
    ),
    "test_release_cordon_2101.py": Classification(
        1, (BUCKET_C,),
        "A daemon_db on-disk fixture: the cordon state has to be readable by "
        "the serve app's own SqliteStore connection, which resolves the "
        "database by path.",
    ),
    "test_openapi.py": Classification(
        1, (BUCKET_C,),
        "_serve_db is a file DB backing the serve app while its responses are "
        "validated against the OpenAPI schema — the app opens it itself, so an "
        "in-memory fixture connection is invisible to it.",
    ),
    "test_milestone_seam.py": Classification(
        1, (BUCKET_C,),
        "_make_file_db seeds a real board file so the milestone seam can be "
        "driven through the daemon endpoint, whose SqliteStore opens the "
        "database by path.",
    ),
    "test_interactive_token_attribution.py": Classification(
        1, (BUCKET_C,),
        "The fixture's own docstring already spells out the C rationale: "
        "SqliteStore's mode=ro connection cannot see a :memory: database.",
    ),
    "test_gate_a.py": Classification(
        1, (BUCKET_C,),
        "One file DB serving both roles at once: opened with "
        "check_same_thread=False for the handler thread, and handed to "
        "SqliteStore(db_path) for the read side.",
    ),
    "test_dashboard.py": Classification(
        1, (BUCKET_C,),
        "The rw_db fixture: the dashboard runs under Starlette's TestClient, "
        "whose handlers execute on a worker thread that the autouse fixture's "
        "thread-bound connection cannot be used from.",
    ),
    "test_cli_issue_reopen.py": Classification(
        1, (BUCKET_C,),
        "_make_file_db seeds a real board file for the SqliteStore-backed "
        "daemon endpoint the reopen command routes through; SqliteStore "
        "resolves it by path, not by connection.",
    ),
    "test_cli_issue_comment.py": Classification(
        1, (BUCKET_C,),
        "_make_file_db seeds a real board file for the SqliteStore-backed "
        "daemon endpoint. Note this file spells the call `_sqlite3.connect` "
        "via `import sqlite3 as _sqlite3` — the concrete reason this ratchet "
        "resolves import aliases instead of grepping for a literal.",
    ),
    "test_cli_acceptance.py": Classification(
        1, (BUCKET_C,),
        "`:memory:` but with check_same_thread=False on purpose — a "
        "background-thread race whose own comment already states the autouse "
        "connection cannot serve it.",
    ),
    "test_drive_state.py": Classification(
        1, (BUCKET_C,),
        "A real on-disk DB for the SqliteStore/HTTP half, deliberately "
        "compared against the coord_db fixture's local half — the test IS the "
        "two-database comparison.",
    ),
    "test_board_wire.py": Classification(
        1, (BUCKET_C,),
        "_ensure_schema_db(path) builds a real file DB for the end-to-end "
        "/board integration, which goes through the serve app's own store "
        "rather than the fixture connection.",
    ),
}


def _connect_sites(tree: ast.Module) -> int:
    """Count ``sqlite3.connect(...)`` **calls** under *tree*.

    Deliberately AST-based rather than a grep, for three reasons this repo's
    own tree demonstrates:

    * ``sqlite3.connect`` appears in prose in half a dozen docstrings
      ("wraps a real sqlite3 connection…"), which a naive grep counts.
    * ``tests/test_cli_issue_comment.py`` writes ``_sqlite3.connect(...)``
      via ``import sqlite3 as _sqlite3``, which a grep for the literal
      ``sqlite3.connect`` misses entirely.
    * ``from sqlite3 import connect`` would evade both.

    All three import spellings are resolved here, so the ratchet cannot be
    sidestepped by renaming the import.
    """
    module_aliases = {"sqlite3"}
    func_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3" and alias.asname:
                    module_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            for alias in node.names:
                if alias.name == "connect":
                    func_aliases.add(alias.asname or alias.name)

    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "connect":
            if isinstance(func.value, ast.Name) and func.value.id in module_aliases:
                count += 1
        elif isinstance(func, ast.Name) and func.id in func_aliases:
            count += 1
    return count


@lru_cache(maxsize=1)
def _audit() -> dict[str, int]:
    """``{relative_path: site_count}`` for every ``tests/**`` Python file with
    at least one ``sqlite3.connect`` call.

    Cached: parsing ~390 test files takes a few seconds and all four tests
    below want the same answer. The tree cannot change mid-session, so a
    process-lifetime cache is safe (and the returned dict is only ever read).
    """
    found: dict[str, int] = {}
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "sqlite3" not in source:  # cheap pre-filter, avoids parsing most files
            continue
        sites = _connect_sites(ast.parse(source, filename=str(path)))
        if sites:
            found[path.relative_to(_TESTS_DIR).as_posix()] = sites
    return found


def test_no_unclassified_sqlite_connect_in_tests():
    """A ``tests/`` file that hardcodes ``sqlite3.connect`` must be classified.

    Adding a new one is fine — it just has to be classified in
    :data:`SQLITE_CONNECT_ALLOWLIST` in the same commit, with a bucket and a
    reason, so the suite's remaining SQLite coupling is always something
    somebody deliberately signed for rather than something that accumulated.
    """
    unclassified = sorted(set(_audit()) - set(SQLITE_CONNECT_ALLOWLIST))
    assert not unclassified, (
        "these tests/ file(s) hardcode `sqlite3.connect` but are not "
        "classified in SQLITE_CONNECT_ALLOWLIST (#2884):\n  "
        + "\n  ".join(unclassified)
        + "\n\nMost new tests need none of this: the autouse `coord_db` "
        "fixture in tests/conftest.py already hands every test an isolated, "
        "schema'd database on whatever backend COORD_TEST_BACKEND selects "
        "(bucket B — just declare `coord_db`). If you genuinely need a "
        "second, separate database, use tests.backends.scratch_database() so "
        "it follows the active backend too. If the test is really about "
        "SQLite itself (WAL, pragmas, sqlite_master, on-disk files), add a "
        "bucket-A entry here saying so."
    )


def test_classified_files_still_exist_and_still_self_connect():
    """The allowlist must not rot into a list of files that no longer qualify.

    A stale entry is worse than no entry: it looks like a considered decision
    while silently permitting a fresh ``sqlite3.connect`` in a file whose
    original justification is long gone.
    """
    audited = _audit()
    stale = sorted(name for name in SQLITE_CONNECT_ALLOWLIST if name not in audited)
    assert not stale, (
        "these SQLITE_CONNECT_ALLOWLIST entries (#2884) name a tests/ file "
        "that no longer contains any `sqlite3.connect` call (or no longer "
        "exists). Delete the entry:\n  " + "\n  ".join(stale)
    )


def test_sqlite_connect_site_counts_are_pinned():
    """Per-file counts are pinned, so growth inside an already-classified file
    is caught too — that is the realistic re-coupling path, since the daemon
    read-path files (``test_serve.py``, ``test_board_read_path.py``) are where
    new two-connection tests naturally land."""
    audited = _audit()
    drift = {
        name: (SQLITE_CONNECT_ALLOWLIST[name].sites, actual)
        for name, actual in audited.items()
        if name in SQLITE_CONNECT_ALLOWLIST
        and SQLITE_CONNECT_ALLOWLIST[name].sites != actual
    }
    assert not drift, (
        "the number of `sqlite3.connect` call sites changed in these "
        "classified files (#2884) — expected vs actual:\n  "
        + "\n  ".join(f"{n}: pinned {exp}, found {act}" for n, (exp, act) in sorted(drift.items()))
        + "\n\nIf you REMOVED sites, lower the pinned count. If you ADDED "
        "one, first check it isn't bucket B (the autouse `coord_db` fixture "
        "already gives you an isolated schema'd DB) or convertible to "
        "tests.backends.scratch_database(); only then raise the count and "
        "extend that entry's `why` to cover the new site."
    )


def test_bucket_b_is_empty():
    """Bucket B — 'should use the fixture and doesn't' — is a *defect* bucket.

    Every member the #2884 audit found was converted in that same PR, so the
    steady state is empty. An entry appearing here means known-convertible
    duplication was knowingly left in the tree; make that loud rather than
    letting it sit in a table nobody re-reads.
    """
    offenders = sorted(
        name for name, c in SQLITE_CONNECT_ALLOWLIST.items() if BUCKET_B in c.buckets
    )
    assert not offenders, (
        "bucket B is the 'convert it' bucket, not a parking lot — these "
        "files re-implement the autouse coord_db fixture and should just "
        "declare it:\n  " + "\n  ".join(offenders)
    )


def test_every_classification_carries_a_bucket_and_a_reason():
    """The record is only worth having if it is actually filled in."""
    valid = {BUCKET_A, BUCKET_B, BUCKET_C, BUCKET_HARNESS}
    bad: list[str] = []
    for name, c in sorted(SQLITE_CONNECT_ALLOWLIST.items()):
        if not c.buckets or set(c.buckets) - valid:
            bad.append(f"{name}: buckets={c.buckets!r}")
        elif len(c.why.split()) < 8:
            bad.append(f"{name}: rationale too thin to be useful ({c.why!r})")
    assert not bad, "malformed SQLITE_CONNECT_ALLOWLIST entries (#2884):\n  " + "\n  ".join(bad)
