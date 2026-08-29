"""Explicit wire DTOs for the daemon's ``GET /board`` projections (#1849).

Before this module the ``/board`` response shape **was** the SQLite table
layout: ``SqliteStore.board_projection()`` did ``SELECT *`` and
``coord/serve_app.py`` built ``components/schemas`` by ``PRAGMA``-introspecting
a freshly-migrated in-memory DB.  Three consequences, all of them real:

1. Every ``coord/db.py`` migration was a potential breaking wire change that
   nothing announced.  The hand-curated ``_DROP_COLUMNS`` / ``_JSON_COLUMNS``
   patches this module replaces were evidence the seam was half-built.
2. Three independent clients parse this shape — the Rust TUI, the React
   webapp, and ``coord/client.py`` — and only one of them has a compile step
   that might notice a rename.
3. **The storage engine's type system was load-bearing on the wire
   contract.**  SQLite has no boolean type, so flag columns declared
   ``INTEGER DEFAULT 0`` ship as raw ``0``/``1``; under Postgres the same
   columns become real ``BOOLEAN`` and ship as ``true``/``false``.  An
   unguarded ``bool`` field fails the parse of the *entire* ``BoardPayload``
   and blanks the whole TUI board — #632, #546 and #628.

So the dataclasses below are the contract, and the storage engine is an
implementation detail underneath them:

- A column that is **not** declared here is **not on the wire**, however many
  ``ALTER TABLE ... ADD COLUMN`` migrations land.  Adding a nullable column to
  a board table is now provably a no-op on both ``/board`` and
  ``/openapi.json`` (``tests/test_board_schema.py``).
- A column's **JSON type is pinned by its Python annotation**, not by SQLite
  affinity.  In particular every INTEGER-backed boolean
  (:data:`INTEGER_BACKED_BOOLEANS`) is annotated ``int``, so it stays a JSON
  integer no matter what the storage engine's own type system does — the
  #632-class blank-board failure cannot be reintroduced by a backend swap.
- A JSON-encoded TEXT column is simply a field typed ``list[str]`` / ``dict``;
  :func:`project_row` decodes it on the way out.

The field **names, order, and types were generated from the pre-#1849
generated spec**, so the change that introduced this file was provably a no-op
on the wire: ``tests/test_board_fixture.py::test_board_sample_fixture_is_up_to_date``
still reproduces the *unmodified* #748 golden fixture byte-for-byte.

**Field order is part of the contract** — ``/board``'s JSON object key order is
this file's declaration order.  The golden fixture is written with
``sort_keys=True`` and so cannot see that; the ordering half is pinned by
``tests/test_board_schema.py::test_board_wire_key_order_is_the_declared_field_order``.
Keep new fields in DDL order, and **append** rather than insert.

Nullability convention (inherited verbatim from the ``PRAGMA table_info``
walk it replaces): a column SQLite reports ``notnull=1`` is a required field
with no default; everything else is ``X | None = None``, which
``coord.openapi.dataclass_schema`` renders as ``nullable: true`` and omits
from ``required``.  ``kw_only=True`` is what lets a defaulted field precede a
non-defaulted one, so the declaration order can follow the DDL exactly.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import types as _types
import typing
from collections.abc import Mapping
from typing import Any, Union

#: What the read path actually hands the projection.  ``sqlite3.Row`` is *not*
#: a ``Mapping`` (see :func:`_as_dict`), so it has to be named explicitly.
RowLike = Union[Mapping[str, Any], sqlite3.Row]


@dataclasses.dataclass(kw_only=True)
class BoardAssignment:
    """One `assignments` row as `/board` carries it.

    `briefing` is deliberately absent: it is ~8 MB of an ~12 MB live
    payload and no board view reads it.  The full row (briefing
    included) is still served by `GET /assignment/{id}`."""

    assignment_id: str | None = None
    machine_name: str
    repo_name: str
    repo_github: str | None = None
    issue_number: int
    issue_title: str
    status: str
    type: str
    branch: str | None = None
    pr_url: str | None = None
    files_allowed: list[str] | None = None
    files_forbidden: list[str] | None = None
    model: str | None = None
    dispatched_at: float | None = None
    finished_at: float | None = None
    smoke_test: str | None = None
    smoke_test_reason: str | None = None
    review_state: str | None = None
    review_of_assignment_id: str | None = None
    review_target: str | None = None
    required_gates: list[str] | None = None
    plan: dict | None = None
    unreachable_count: int | None = None
    exit_code: int | None = None
    review_iteration: int | None = None
    review_posted_at: float | None = None
    test_state: str | None = None
    test_reason: str | None = None
    uat_state: str | None = None
    uat_reason: str | None = None
    cost_usd: float | None = None
    smoke_tests: list[str] | None = None
    review_findings: str | None = None
    test_plan: dict | None = None
    review_verdict: str | None = None
    claude_session_id: str | None = None
    test_plan_branch_head: str | None = None
    provider_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    is_interactive: int | None = None  # 0/1 flag — INTEGER on the wire, never a JSON bool (#1849)
    failure_reason: str | None = None
    review_head_sha: str | None = None
    acceptance_state: str | None = None
    acceptance_reason: str | None = None
    acceptance_sha: str | None = None
    acceptance_total: int | None = None
    acceptance_passed: int | None = None
    completion_summary: str | None = None
    audit_goals_json: str | None = None
    audit_bottom_line: str | None = None
    audit_run_number: int | None = None
    for_issue_number: int | None = None
    review_verdict_original: str | None = None
    review_verdict_override_reason: str | None = None
    review_patch_id: str | None = None
    test_head_sha: str | None = None
    test_patch_id: str | None = None
    test_base_sha: str | None = None
    review_scoped: int | None = None  # 0/1 flag — INTEGER on the wire, never a JSON bool (#1849)
    review_scope_base_sha: str | None = None
    driven_by: str | None = None
    test_toolchain: str | None = None
    verdict_source: str | None = None
    verdict_source_reason: str | None = None
    stop_reason: str | None = None
    dispatched_by_assignment_id: str | None = None
    # #2786: worker-reported turn count — see the four token columns above
    # (input_tokens/output_tokens/cache_creation_tokens/cache_read_tokens)
    # for the sibling fields this rides alongside. Appended last, matching
    # DDL order (`_MIGRATE_ADD_COLUMNS` in coord/db.py).
    num_turns: int | None = None


@dataclasses.dataclass(kw_only=True)
class BoardMachine:
    """One `machines` row as `/board` carries it."""

    name: str | None = None
    host: str
    capabilities: list[str] | None = None
    repos: list[str] | None = None


@dataclasses.dataclass(kw_only=True)
class BoardMergeQueueEntry:
    """One `merge_queue` row as `/board` carries it."""

    id: int | None = None
    assignment_id: str
    repo_name: str
    repo_github: str
    branch: str
    target_branch: str
    issue_number: int
    issue_title: str
    state: str
    pr_number: int | None = None
    pr_url: str | None = None
    size: int | None = None
    last_attempt: float | None = None
    error: str | None = None
    enqueued_at: float | None = None
    assignment_type: str | None = None
    required_gates: list[str] | None = None
    ci_infra_reruns: int
    ci_stale_reruns: int
    ci_flaky_reruns: int
    ci_flaky_pending: str
    ci_unreadable_reruns: int
    ci_fix_dispatches: int


@dataclasses.dataclass(kw_only=True)
class BoardProposal:
    """One `proposals` row as `/board` carries it."""

    id: int | None = None
    machine_name: str
    repo_name: str
    issue_number: int
    issue_title: str
    rationale: str | None = None
    files_likely: list[str] | None = None
    briefing: str | None = None
    model: str | None = None
    type: str | None = None
    required_gates: list[str] | None = None


@dataclasses.dataclass(kw_only=True)
class BoardIssue:
    """One `issues` row as `/board` carries it."""

    repo_name: str
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    synced_at: float | None = None
    milestone_number: int | None = None
    milestone_title: str | None = None


@dataclasses.dataclass(kw_only=True)
class BoardDriveEscalation:
    """One `drive_escalations` row as `/board` carries it (#1505)."""

    id: int | None = None
    repo_name: str
    issue_number: int
    stage: str
    assignment_id: str | None = None
    reason: str
    gate_readings: str
    proposed_command: str
    created_at: float


@dataclasses.dataclass(kw_only=True)
class BoardDriveQueueEntry:
    """One `drive_queue` row as `/board` carries it (#1753)."""

    id: int | None = None
    repo_name: str
    issue_number: int
    position: int
    machine: str | None = None
    after_json: list[str]
    state: str
    attempts: int
    deferrals: int
    last_reason: str
    reason_at: float | None = None
    session_name: str | None = None
    launched_at: float | None = None
    enqueued_at: float
    hold_after: int  # 0/1 flag — INTEGER on the wire, never a JSON bool (#1849)
    hold_reason: str
    resume_when: str
    hold_state: str
    hold_probes: int
    launch_host: str
    hold_scope: str
    resumes: int
    retry_backoff_at: float | None = None
    max_fix_rounds: int | None = None
    no_acceptance: int  # 0/1 flag — INTEGER on the wire, never a JSON bool (#1849)



#: ``table name`` → the DTO that defines its ``/board`` wire shape.  These are
#: exactly the seven projections ``coord/serve_app.py`` publishes under
#: ``components/schemas``; a table absent from this mapping (e.g.
#: ``notifications``) is passed through untouched by :func:`decode_row`.
BOARD_PROJECTIONS: dict[str, type] = {
    "assignments": BoardAssignment,
    "machines": BoardMachine,
    "merge_queue": BoardMergeQueueEntry,
    "proposals": BoardProposal,
    "issues": BoardIssue,
    "drive_escalations": BoardDriveEscalation,
    "drive_queue": BoardDriveQueueEntry,
}

#: Columns that are semantically booleans but are stored as ``INTEGER`` and
#: therefore ship as ``0``/``1``.  Their DTO fields are annotated ``int`` on
#: purpose; ``tests/test_board_schema.py`` asserts the generated JSON Schema
#: types them ``integer`` and never ``boolean``, so the wire shape survives a
#: storage-engine swap (Postgres would make these real ``BOOLEAN``s).  This
#: WAS the DTO-level counterpart of ``coord/board_bool_guard.py``'s consumer-
#: side check against the real Rust wire structs (``tui/src/app/types.rs``
#: and its generated ``types/generated.rs`` sibling, #1941); that
#: text-scraping check is retired as of #2897 (docs/ADR_COORD_TUI_CI.md) —
#: this assertion is now the sole remaining guard.
INTEGER_BACKED_BOOLEANS: frozenset[str] = frozenset(
    {"is_interactive", "review_scoped", "hold_after", "no_acceptance"}
)


def _is_json_encoded(tp: Any) -> bool:
    """True when *tp* is a container type — i.e. the column is JSON-in-TEXT in
    SQLite and must be decoded to a native list/dict before hitting the wire."""
    if typing.get_origin(tp) in (typing.Union, _types.UnionType):
        return any(
            _is_json_encoded(a) for a in typing.get_args(tp) if a is not type(None)
        )
    return tp in (list, dict) or typing.get_origin(tp) in (list, dict)


_WIRE_FIELDS_CACHE: dict[type, tuple[tuple[str, bool], ...]] = {}


def wire_fields(cls: type) -> tuple[tuple[str, bool], ...]:
    """``((field_name, is_json_encoded), ...)`` for *cls*, in wire order."""
    cached = _WIRE_FIELDS_CACHE.get(cls)
    if cached is None:
        hints = typing.get_type_hints(cls)
        cached = tuple(
            (f.name, _is_json_encoded(hints[f.name])) for f in dataclasses.fields(cls)
        )
        _WIRE_FIELDS_CACHE[cls] = cached
    return cached


def json_fields(cls: type) -> frozenset[str]:
    """The subset of *cls*'s fields backed by a JSON-encoded TEXT column."""
    return frozenset(name for name, is_json in wire_fields(cls) if is_json)


def _decode_json_value(value: Any) -> Any:
    """JSON-decode a value read out of a JSON-encoded TEXT column.

    Non-strings pass through untouched (already decoded, or NULL); an empty
    string and unparseable JSON both degrade to ``None`` rather than blowing
    up the whole board read.
    """
    if not isinstance(value, (str, bytes, bytearray)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _as_dict(row: RowLike) -> dict[str, Any]:
    """*row* as a plain ``dict``.

    ``sqlite3.Row`` is a **sequence**, not a mapping: ``"foo" in row`` tests
    its *values*, not its column names, and would silently drop every field.
    Normalising up front is the only safe way to ask "does this row have that
    column?".
    """
    return row if type(row) is dict else dict(row)


def project_row(cls: type, row: RowLike) -> dict[str, Any]:
    """Project one raw DB row through *cls* into its ``/board`` wire dict.

    Only fields **declared on the DTO** survive, in declaration order — so a
    column added by a later migration is absent from the wire until someone
    adds it here deliberately.  A declared field missing from *row* (an
    un-migrated DB) is skipped rather than raising, matching the old
    ``dict(row)`` behaviour.

    Values are **not coerced** to their declared types: the annotation defines
    the contract, and coercing here would be a silent wire change on any row
    whose stored value disagrees with it.
    """
    src = _as_dict(row)
    out: dict[str, Any] = {}
    for name, is_json in wire_fields(cls):
        if name not in src:
            continue
        value = src[name]
        out[name] = _decode_json_value(value) if is_json else value
    return out


def decode_row(table: str, row: RowLike, *, full: bool = False) -> dict[str, Any]:
    """One DB row as the wire carries it.

    ``full=True`` keeps every column (used by the single-resource *detail*
    reads, #1336/#1337, which serve the complete row including ``briefing``)
    and only applies the JSON decoding; the collection projection goes through
    :func:`project_row` and is therefore bounded by the DTO.

    A table with no DTO is returned as a plain dict, unchanged.
    """
    cls = BOARD_PROJECTIONS.get(table)
    if cls is None:
        return dict(row)
    if not full:
        return project_row(cls, row)
    out = dict(_as_dict(row))
    for name in json_fields(cls):
        if name in out:
            out[name] = _decode_json_value(out[name])
    return out
