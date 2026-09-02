"""Generate the golden `/board` fixture used by both sides of the wire seam (#748).

The `/board` payload is each board table projected through its explicit wire
DTO in `coord/board_schema.py` (#1849 — before that the wire schema literally
*was* the SQLite DDL).  The Rust structs mirroring those DTOs are generated
from the same served schema (`tui/src/app/types/generated.rs`,
`coord.codegen --rust`, #1941) rather than hand-typed.  A single type mismatch
(the classic case: a SQLite `INTEGER` boolean vs a Rust `bool`) fails the
**entire** `BoardPayload` parse and blanks the board (#632/#546/#628).

This script builds a representative, freshly-migrated coord.db (headless +
interactive assignments, a review, a merge-queue row, a proposal, an open
issue, a machine) with fully deterministic content (no wall-clock timestamps,
fixed IDs) and runs it through the exact same `SqliteStore.board_projection()`
the daemon serves, then writes the result into a **coord-tui checkout**.

#2899: THE FIXTURE IS NOT IN THIS REPO ANY MORE.  It lives at coord-tui's
`tests/fixtures/board_sample.json`, alongside the Rust test that reads it
(`src/app/tests.rs::board_payload_deserializes_real_sample`).  So this script
takes its destination the same way `coord.codegen` takes `generated.rs`'s
(#2897): `--out PATH`, else `$COORD_TUI_SRC` naming a coord-tui checkout root.
There is deliberately **no fallback default** — a guessed path is either a
dead file nobody consumes or, worse, one that makes a freshness check pass
vacuously.  Same reasoning as `codegen.resolve_rust_output_path`.

Who reads the committed fixture:
- Rust: `src/app/tests.rs::board_payload_deserializes_real_sample` parses it
  into `BoardPayload` and asserts the round-trip succeeds — runs in coord-tui's
  own CI.
- coord-tui CI: the byte-comparison freshness gate, which installs
  `code-coordinator[server]` from PyPI and re-runs this generator, exactly as
  the `generated.rs` drift gate does.  Before #2899 that comparison was
  `tests/test_board_fixture.py` in this repo; it could not survive the split,
  because there is no committed fixture here to compare against.
- Python (here): `tests/test_board_fixture.py`, narrowed to what one checkout
  can still prove — that the generator runs and emits the representative shape
  the Rust round-trip asserts on.

Regenerate after any `coord/board_schema.py` change that should be reflected
in the golden fixture (a `coord/db.py` migration on its own no longer moves
the wire — see tests/test_board_schema.py):

    COORD_TUI_SRC=~/src/coord-tui python -m coord.gen_board_fixture

#3045 — THIS MODULE LIVES IN THE INSTALLED PACKAGE, NOT JUST THIS CHECKOUT.
It used to be `scripts/gen_board_fixture.py`, which `scripts/` never ships
(`[tool.setuptools.packages.find]` in pyproject.toml only includes `coord*`)
— so "installs `code-coordinator[server]` from PyPI and re-runs this
generator" above was aspirational for coord-tui's CI, the same gap
`coord/codegen.py`'s docstring describes for the TS half. It now lives at
`coord/gen_board_fixture.py`, a real module of the `coord` package, so
`pip install 'code-coordinator[server]'` is genuinely enough:

    python -m coord.gen_board_fixture ...

`scripts/gen_board_fixture.py` still exists, as a thin shim re-exporting
this module, so existing local invocations and docs that predate the move
keep working from a checkout of *this* repo.

Because this module is now inside the `coord` package, it is also inside the
`coord.sql` dialect-seam ratchet's blast radius (#2768/#827/#1948 — enforced
by `tests/test_sql_dialect.py`, which walks `coord/**` and knows nothing about
`scripts/`). Every statement below therefore goes through `coord.sql`, and the
fixture connection is opened by `sql.connect()`, exactly like the rest of the
package. The seam is a no-op translation for SQLite, so the emitted fixture
bytes are unchanged by the move.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from coord import sql
from coord.dao import SqliteStore
from coord.db import _ensure_schema

#: Path of the emitted fixture RELATIVE to a `coord-tui` checkout's root.
FIXTURE_RELPATH = Path("tests") / "fixtures" / "board_sample.json"

#: Env var naming a `coord-tui` checkout root, used when `--out` is absent.
#: Deliberately the SAME variable `coord.codegen --rust` reads, because
#: it names the same checkout — an operator who has to set two env vars for
#: one repo will eventually set only one of them.
FIXTURE_ENV_VAR = "COORD_TUI_SRC"


class FixtureOutputPathError(Exception):
    """No destination was named — see :func:`resolve_fixture_path`."""


def resolve_fixture_path(explicit: str | Path | None = None) -> Path:
    """Where to write the fixture: ``--out`` > ``$COORD_TUI_SRC``.

    Raises :class:`FixtureOutputPathError` when neither is set, rather than
    guessing (#2899): the old hard-coded ``tui/tests/fixtures/board_sample.json``
    is not in this repo any more, so a guess is always wrong.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    root = os.environ.get(FIXTURE_ENV_VAR)
    if root:
        return Path(root).expanduser() / FIXTURE_RELPATH
    raise FixtureOutputPathError(
        "no destination for board_sample.json. Since #2899 the TUI lives in "
        f"the coord-tui repo, so pass --out PATH or set ${FIXTURE_ENV_VAR} to "
        f"a coord-tui checkout root (the file is written to its "
        f"{FIXTURE_RELPATH}). See this script's module docstring."
    )


def build_fixture_db(conn: sqlite3.Connection) -> None:
    """Populate *conn* (already schema-migrated) with deterministic, representative rows.

    Every timestamp is a fixed epoch value (not `time.time()`) so the
    generated payload — and therefore the committed fixture — never changes
    between runs except when this function or the schema itself changes.
    """
    # ── assignments ──────────────────────────────────────────────────────
    # 1. A finished headless (claude -p) work assignment: smoke_tests + a
    #    test_plan (JSON object — decoded to a native object on the wire,
    #    NOT an array, per #584) + review_findings (kept as a raw JSON
    #    string on the wire) + cost/token accounting.
    sql.execute(
        conn,
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, repo_github, "
        "issue_number, issue_title, status, type, branch, model, dispatched_at, "
        "finished_at, exit_code, cost_usd, smoke_tests, review_findings, test_plan, "
        "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, "
        "is_interactive, test_state, review_verdict) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "work-748a", "precision", "claude-coordinator", "JDonaghy/claude-coordinator",
            748, "seam: golden /board fixture", "done", "work", "issue-748-fixture",
            "sonnet", 1000000000.0, 1000000600.0, 0, 0.42,
            '["fixture loads in the TUI", "round_number is non-zero"]',
            '{"verdict": "approve", "body": "Looks good."}',
            '{"steps": [{"kind": "run", "cmd": "cargo test", "label": "run tui tests"}]}',
            1200, 340, 0, 5000, 0, "passed", "approve",
        ),
    )
    # 2. A running human-attended interactive (Max/Pro) assignment —
    #    is_interactive=1, no cost/token data (the #546 case this fixture
    #    exists to guard).
    sql.execute(
        conn,
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, repo_github, "
        "issue_number, issue_title, status, type, branch, dispatched_at, is_interactive) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "work-748b", "dellserver", "claude-coordinator", "JDonaghy/claude-coordinator",
            749, "interactive follow-up", "running", "work", "issue-749-followup",
            1000001000.0, 1,
        ),
    )
    # 3. A review of assignment 1 (pairs via review_of_assignment_id).
    sql.execute(
        conn,
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, repo_github, "
        "issue_number, issue_title, status, type, review_of_assignment_id, dispatched_at, "
        "finished_at, review_verdict, is_interactive) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "rev-748a", "dellserver", "claude-coordinator", "JDonaghy/claude-coordinator",
            748, "seam: golden /board fixture", "done", "review", "work-748a",
            1000000700.0, 1000000900.0, "approve", 0,
        ),
    )

    # ── machines ─────────────────────────────────────────────────────────
    sql.execute(
        conn,
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("precision", "precision.tailnet", '["python", "rust"]', '["claude-coordinator"]'),
    )
    sql.execute(
        conn,
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("dellserver", "dellserver.tailnet", '["python", "gtk"]', '["claude-coordinator"]'),
    )

    # ── merge_queue ──────────────────────────────────────────────────────
    sql.execute(
        conn,
        "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, branch, "
        "target_branch, issue_number, issue_title, state, pr_number, pr_url, size, "
        "enqueued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "work-748a", "claude-coordinator", "JDonaghy/claude-coordinator",
            "issue-748-fixture", "main", 748, "seam: golden /board fixture",
            "queued", 9001, "https://github.com/JDonaghy/claude-coordinator/pull/9001",
            240, 1000000950.0,
        ),
    )

    # ── proposals ────────────────────────────────────────────────────────
    sql.execute(
        conn,
        "INSERT INTO proposals (machine_name, repo_name, issue_number, issue_title, "
        "rationale, type) VALUES (?,?,?,?,?,?)",
        (
            "precision", "claude-coordinator", 750, "next seam-hardening pass",
            "precision is idle and has touched dao.py recently", "work",
        ),
    )

    # ── issues ───────────────────────────────────────────────────────────
    sql.execute(
        conn,
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at, "
        "milestone_number, milestone_title) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "claude-coordinator", 748, "seam: golden /board fixture + CI round-trip parse test",
            "## Context\n\nThe /board payload is shipped as raw SQLite rows...",
            "open", '["coord", "status:ready"]', 1000001100.0, 12, "Tech Debt: seam hardening",
        ),
    )

    # ── drive_queue ──────────────────────────────────────────────────────
    # #1753/#1755: two rows so the golden fixture exercises BOTH shapes the
    # Rust `BoardDriveQueueEntry` has to survive — a bare appended entry
    # (NULL machine, empty `after_json`, zeroed counters) and a fully-
    # populated one (pinned machine, a decoded `after_json` LIST, non-zero
    # attempts/deferrals, a `last_reason` string, a REAL `launched_at`).
    # The `after_json` column in particular is the trap this guards: it is a
    # JSON *string* in SQLite and a decoded array on the wire (it is typed
    # `list[str]` on `coord.board_schema.BoardDriveQueueEntry`), so a Rust
    # field typed `Vec<String>` without
    # the `after_json` rename silently stays empty — and a field typed
    # `String` fails the whole BoardPayload parse and blanks every panel.
    sql.execute(
        conn,
        "INSERT INTO drive_queue (repo_name, issue_number, position, machine, "
        "after_json, state, attempts, deferrals, last_reason, session_name, "
        "launched_at, enqueued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "claude-coordinator", 748, 0, None, "[]", "waiting", 0, 0, "",
            None, None, 1000001200.0,
        ),
    )
    sql.execute(
        conn,
        "INSERT INTO drive_queue (repo_name, issue_number, position, machine, "
        "after_json, state, attempts, deferrals, last_reason, session_name, "
        "launched_at, enqueued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "claude-coordinator", 750, 1, "dellserver",
            '["claude-coordinator#748"]', "waiting", 1, 2,
            "pre-req claude-coordinator#748 has not merged", None, None,
            1000001250.0,
        ),
    )

    # ── board_meta ───────────────────────────────────────────────────────
    # `sql.upsert` rather than the SQLite-only `INSERT OR REPLACE` these three
    # rows used while this module still lived under `scripts/` — same shape
    # `coord/db.py` already uses for its own board_meta writes.
    for key, value in (
        ("round_number", "3"),
        ("board_initialized", "1"),
        ("pipeline_default_gates", '["test", "review", "merge"]'),
    ):
        sql.upsert(
            conn, "board_meta", ["key", "value"], (key, value),
            conflict_columns=["key"],
        )
    conn.commit()


def build_fixture_payload() -> dict:
    """Build the fixture DB in-memory and return its `/board` projection.

    Uses a real on-disk temp file (not `:memory:`) because `SqliteStore` opens
    its own `mode=ro` connection by URI — it cannot see an in-process
    `:memory:` DB.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "board_fixture.db"
        conn = sql.connect(backend=sql.DIALECT_SQLITE, sqlite_path=db_path)
        sql.apply_row_factory(conn)
        _ensure_schema(conn)
        build_fixture_db(conn)
        conn.close()
        return SqliteStore(db_path).board_projection()


def fixture_json_text() -> str:
    """Deterministic JSON text for the fixture (sorted keys, stable indent)."""
    payload = build_fixture_payload()
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=None,
        help=(
            "destination path for board_sample.json. Defaults to "
            f"${FIXTURE_ENV_VAR}/{FIXTURE_RELPATH}."
        ),
    )
    args = parser.parse_args(argv)
    try:
        dest = resolve_fixture_path(args.out)
    except FixtureOutputPathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(fixture_json_text())
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
