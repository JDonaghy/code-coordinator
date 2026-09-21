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

# #3428: the `concurrency` block's `occupancy_state` value set — owned by the
# snapshot module that produces it (see `BoardConcurrency` below).  Safe at
# module scope: that module imports nothing from `coord` at import time.
from coord.drive_sessions_snapshot import OCCUPANCY_UNOBSERVED

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
    # #3158: "captured"/"unmeasured"/None — see coord.models.Assignment's
    # field docstring.
    cost_capture_state: str | None = None
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
    # #3339: the explicit operator assertion that clears a terminal
    # `refused_premise` row (`coord.state.mark_premise_rechecked` /
    # `coord drive-queue clear-refusal`). Undeclared here, these two columns
    # would be silently dropped from `/board` per this module's own
    # docstring — and `coord.drive_state.project()`'s
    # `work_premise_rechecked_at`/`_reason` read ONLY this wire payload
    # (`BoardFetcher.fetch()`), so on any daemon-routed fleet `decide()`'s
    # bypass branch in `coord/drive.py` would never see a recheck a human
    # just recorded. Appended last, matching DDL order.
    premise_rechecked_at: float | None = None
    premise_rechecked_reason: str | None = None
    # #3357: whether the `test_state` write above was independently confirmed
    # by an out-of-band suite run — one of
    # `coord.confirm_test.TEST_CONFIRMATION_VALUES` ("confirmed" /
    # "unconfirmed" / "refuted" / "baseline_red"), or `None` when no
    # confirmation was ever attempted for this write
    # (`coord.state._record_test_verdict_local`). Undeclared here, this
    # column would be silently dropped from `/board` per this module's own
    # docstring above — exactly the #3339 mistake this same comment block
    # was written to prevent, repeated one PR later. Appended last, matching
    # DDL order (`_MIGRATE_ADD_COLUMNS` in coord/db.py).
    test_confirmation: str | None = None


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
    # #3384: GitHub's own `stateReason` — `"reopened"` when a human explicitly
    # reopened this issue via `gh issue reopen`, `""` for an issue that has
    # never been closed. See `coord.drive_queue.IssueFacts.reopened`.
    #
    # ABSENT, not `""`, on the wire when it is the empty default: it would
    # otherwise cost ~20 bytes on every issue row of every poll to say nothing
    # (`coord.board_wire._drop_default_state_reason`, which is also why it is
    # not in this DTO's `required` list — it has a default). Read it as
    # `row.get("state_reason") or ""`; the Rust side gets `#[serde(default)]`
    # from `coord.codegen`.
    state_reason: str = ""


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
    # #3236: the apply-verdict gate extension of #1757's --hold-after —
    # see coord.drive_queue.apply_gate_status / plan_is_destructive.
    plan_destructive: int  # 0/1 flag — INTEGER on the wire, never a JSON bool (#1849)
    apply_verdict: str
    apply_verdict_reason: str
    apply_verdict_at: float | None = None


# ── #3428 (#3408 item 3): concurrency ceilings + provenance + occupancy ──────
#
# The three dataclasses below are NOT a table projection — there is no
# `concurrency` table — so they are absent from `BOARD_PROJECTIONS` and
# `decode_row()` never touches them. They exist to give the *computed*
# `coord.drive_queue.CeilingResolution` (already resolved on the daemon host
# by `coord config --effective`'s own machinery — see that command's
# docstring) an explicit wire shape, the same way `BoardAssignment` etc. give
# one to a raw DB row. `coord/serve_app.py`'s `board()` handler builds a
# `BoardConcurrency` per request (never persisted) and adds it to the
# `/board` payload as the sibling key ``concurrency`` — same posture as
# ``roll_pending``/``goal_header``: additive, absent-tolerant, never required.
#
# Deliberately does NOT import `coord.drive_queue` at module scope:
# `coord.drive_queue` transitively imports THIS module (via
# `coord.merge_queue` -> `coord.state` -> `coord.board_schema`), so a
# top-level `from coord.drive_queue import CeilingResolution` here would be a
# real import cycle, not just an ugly one. `CeilingResolution` is referenced
# only for typing (`from __future__ import annotations` makes the annotation
# itself lazy) and duck-typed at runtime by :func:`board_ceiling_from_resolution`.
# (`coord.drive_sessions_snapshot`, which owns the `occupancy_state` value
# set `BoardConcurrency` below defaults to, is by contrast imported at the
# TOP of this file: it imports nothing from `coord` at module scope — its own
# tmux/queue imports are deferred into `refresh()` — so it cannot close a
# cycle back through here.)
if typing.TYPE_CHECKING:  # pragma: no cover — typing only
    from coord.drive_queue import CeilingResolution


#: `BoardCeiling.source_kind` / `BoardCeilingSource.source_kind`'s closed
#: value set (#3428) — a machine-readable classification of
#: `CeilingResolution.source`'s free-text prose (written for `coord config
#: --effective`'s human-readable output), so a client can style/filter a
#: ceiling's provenance without string-matching wording that is free to
#: change. See :func:`classify_ceiling_source` for the mapping.
CEILING_SOURCE_KIND_SYSTEMD_FLAG = "systemd_flag"
CEILING_SOURCE_KIND_CLI_FLAG = "cli_flag"
CEILING_SOURCE_KIND_COORDINATOR_YML_REPO = "coordinator_yml_repo"
CEILING_SOURCE_KIND_COORDINATOR_YML_PIPELINE = "coordinator_yml_pipeline"
CEILING_SOURCE_KIND_COORDINATOR_YML_CONCURRENCY = "coordinator_yml_concurrency"
CEILING_SOURCE_KIND_DERIVED = "derived"
CEILING_SOURCE_KIND_DEFAULT = "default"
#: A `CeilingResolution.source` this classifier doesn't recognise — should
#: never actually appear on a real `/board` payload (every source string
#: `coord.drive_queue`'s resolvers produce is covered below), but a client
#: must still be able to parse it rather than choke: unlike the DTOs above,
#: `source_kind` degrading to "unknown" is a display nit, not a #632-class
#: parse failure.
CEILING_SOURCE_KIND_UNKNOWN = "unknown"

CEILING_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        CEILING_SOURCE_KIND_SYSTEMD_FLAG,
        CEILING_SOURCE_KIND_CLI_FLAG,
        CEILING_SOURCE_KIND_COORDINATOR_YML_REPO,
        CEILING_SOURCE_KIND_COORDINATOR_YML_PIPELINE,
        CEILING_SOURCE_KIND_COORDINATOR_YML_CONCURRENCY,
        CEILING_SOURCE_KIND_DERIVED,
        CEILING_SOURCE_KIND_DEFAULT,
        CEILING_SOURCE_KIND_UNKNOWN,
    }
)


def classify_ceiling_source(source: str) -> str:
    """Map one `CeilingResolution.source` (or one of its `losing` entries'
    source names) prose string to a :data:`CEILING_SOURCE_KINDS` enum value.

    Ordered by specificity, matching the exact strings
    `coord.drive_queue`'s resolvers / `coord.commands.setup._print_effective_
    concurrency` actually construct today:

    * ``"systemd ExecStart --max-parallel..."`` (`read_systemd_max_parallel_
      flags` via `coord.commands.setup`'s ``per_repo_source``/
      ``max_parallel_source``) -> ``systemd_flag`` — the #3408 incident
      source: a machine-local flag a thin client can never see for itself.
    * ``"--max-parallel[-per-repo] flag"`` (an explicit CLI override on
      `coord drive-queue tick`'s own invocation) -> ``cli_flag``.
    * ``"coordinator.yml repos[<name>].max_parallel"`` (#3423 per-repo
      override) -> ``coordinator_yml_repo``.
    * ``"coordinator.yml pipeline.max_parallel..."`` -> ``coordinator_yml_pipeline``.
    * ``"coordinator.yml concurrency.max_workers"`` (this module's own
      synthetic resolution for the fleet worker cap — see
      :func:`board_ceiling_from_resolution`'s caller in `coord/serve_app.py`)
      -> ``coordinator_yml_concurrency``.
    * ``"derived (...)"`` (`default_max_parallel`'s repo-shape derivation)
      -> ``derived``.
    * ``"default (...)"`` (the hardcoded fallback, config unreadable) ->
      ``default``.
    * anything else -> ``unknown`` (fail-soft; see that constant's docstring).
    """
    if source.startswith("systemd ExecStart"):
        return CEILING_SOURCE_KIND_SYSTEMD_FLAG
    if source.startswith("coordinator.yml repos["):
        return CEILING_SOURCE_KIND_COORDINATOR_YML_REPO
    if source.startswith("coordinator.yml pipeline."):
        return CEILING_SOURCE_KIND_COORDINATOR_YML_PIPELINE
    if source.startswith("coordinator.yml concurrency."):
        return CEILING_SOURCE_KIND_COORDINATOR_YML_CONCURRENCY
    if source.startswith("derived"):
        return CEILING_SOURCE_KIND_DERIVED
    if source.startswith("default"):
        return CEILING_SOURCE_KIND_DEFAULT
    if source.startswith("--") and source.endswith("flag"):
        return CEILING_SOURCE_KIND_CLI_FLAG
    return CEILING_SOURCE_KIND_UNKNOWN


@dataclasses.dataclass(kw_only=True)
class BoardCeilingSource:
    """One LOSING source's own opinion for a :class:`BoardCeiling` (#3428) —
    the wire twin of one ``(name, value)`` pair in a
    `coord.drive_queue.CeilingResolution.losing` tuple. This is the whole
    point of shipping provenance at all: it is what tells an operator that
    editing ``coordinator.yml``/``coord-settings`` is futile while a
    machine-local systemd flag outranks it (#3408's reported incident).
    """

    source: str
    source_kind: str
    value: int


@dataclasses.dataclass(kw_only=True)
class BoardCeiling:
    """One resolved concurrency ceiling + provenance (#3428) — the `/board`
    wire twin of `coord.drive_queue.CeilingResolution`, the exact resolution
    `coord config --effective` and `coord drive-queue tick` itself already
    use (#2085 "one question, one answer": this block is never a second,
    independently-derived answer).

    ``source`` carries the same free-text `CeilingResolution.source` a human
    reads in `coord config --effective`'s output; ``source_kind`` is the
    machine-readable classification of it (:func:`classify_ceiling_source`)
    a client should actually branch on. ``losing`` is ``None`` (not ``[]``)
    when nothing else was configured, matching `CeilingResolution.losing`'s
    own "empty means nobody else had an opinion" contract.
    """

    name: str
    value: int
    source: str
    source_kind: str
    losing: list[BoardCeilingSource] | None = None


def board_ceiling_from_resolution(resolution: "CeilingResolution") -> BoardCeiling:
    """`coord.drive_queue.CeilingResolution` -> its `/board` wire shape.

    Duck-typed on purpose (reads ``.name``/``.value``/``.source``/``.losing``
    rather than importing the real class) — see the module note above this
    section for why `coord.drive_queue` cannot be imported here at runtime.
    """
    losing = [
        BoardCeilingSource(
            source=name, source_kind=classify_ceiling_source(name), value=value
        )
        for name, value in resolution.losing
    ] or None
    return BoardCeiling(
        name=resolution.name,
        value=resolution.value,
        source=resolution.source,
        source_kind=classify_ceiling_source(resolution.source),
        losing=losing,
    )


@dataclasses.dataclass(kw_only=True)
class BoardConcurrency:
    """The `/board` payload's ``concurrency`` block (#3428, #3408 item 3):
    every ceiling `coord drive-queue tick` actually enforces, resolved on
    THIS (the daemon) host, plus current occupancy against each.

    **Resolved server-side, always** — the systemd-flag source
    (:data:`CEILING_SOURCE_KIND_SYSTEMD_FLAG`) is machine-local and invisible
    to a thin client (a Rust TUI, a phone webapp) by construction; a client
    deriving ceilings from its own cached ``coordinator.yml`` would
    confidently print the config value while a host-local flag silently
    outranked it — the exact #3408 failure this block exists to end.

    ``repo_overrides`` carries only the repos whose OWN ``repos[].max_parallel``
    (#3423) actually differs from ``max_parallel_per_repo``'s fleet-wide
    value — mirroring `coord.drive_queue.effective_repo_capacities`'s own
    "only the repos that disagree" trim (see that function's docstring):
    listing every repo would bury the ones that matter and make an
    all-default fleet look like something was overridden everywhere.

    ``occupied``/``repo_occupied`` are `coord.drive_queue.
    compute_running_occupancy`'s own output, verbatim — the SAME "is this
    entry still occupying a slot" verdict `plan_tick` enforces, never a
    second, independently-recounted number (#2085 "one question, one
    answer").

    **Occupancy is nullable, and says why (#2096).** That verdict's first
    question about a `running` entry is "is its drive session live?", which
    only a ``tmux list-sessions`` reading can answer — and that reading is a
    SUBPROCESS, so it is taken on the daemon's tick cadence
    (`coord.drive_sessions_snapshot`), never inline off this read path (the
    `/board` invariant 1 that `tests/test_board_read_path.py` enforces). When
    the daemon has not taken a reading yet, or its refresh loop has stalled
    (#2862) and the last one is too old to be evidence about now,
    ``occupied``/``repo_occupied`` are ``None`` and ``occupancy_state`` says
    which — never a confidently-wrong ``0`` that a client would render as
    "all slots free". ``occupancy_observed_at`` is the wall-clock moment of
    the reading behind the numbers (``None`` when there has never been one),
    so a client can show — and age out — the numbers itself.

    The CEILINGS are unaffected by any of that: they are resolved from
    config + this host's systemd unit on every build, so they stay populated
    even when occupancy is unknown.

    Absent-tolerant in both directions (#3428 acceptance): an older daemon
    simply omits the ``concurrency`` key entirely (never ships it as e.g. an
    empty object), and a newer client must not require the key to parse an
    otherwise-valid board.
    """

    max_parallel: BoardCeiling
    max_parallel_per_repo: BoardCeiling
    max_workers: BoardCeiling
    repo_overrides: dict[str, BoardCeiling]
    #: ``None`` unless ``occupancy_state`` is
    #: :data:`~coord.drive_sessions_snapshot.OCCUPANCY_OBSERVED`.
    occupied: int | None = None
    repo_occupied: dict[str, int] | None = None
    #: One of :data:`~coord.drive_sessions_snapshot.OCCUPANCY_STATES`.
    occupancy_state: str = OCCUPANCY_UNOBSERVED
    occupancy_observed_at: float | None = None


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
    {"is_interactive", "review_scoped", "hold_after", "no_acceptance", "plan_destructive"}
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
