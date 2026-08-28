"""Generate TypeScript wire types from the dashboard's OpenAPI spec (#1550).

There is no single source of truth for any wire type in this repo — every
contract is a hand-maintained mirror on both sides. #750 closed that gap by
generating `coord/dashboard/webapp/src/api/generated.ts` straight from the
Python dataclasses (`coord.models.Assignment`, `coord.pipeline.PipelineStage`
/ `PipelineGate` / `PipelineView`). #1550 moves the source of truth up one
level: this script now reads `coord.dashboard.server.openapi_spec()` —
the same `components/schemas` document served at `GET /openapi.json` and
already regression-tested against the real Starlette route table
(`tests/test_openapi.py`'s `declared_routes(...) == spec_routes(...)`,
#757) — instead of introspecting the dataclasses a second time. Generating
from the *served* contract, rather than from the Python types that happen to
back it today, means a future endpoint whose response shape isn't a bare
`dataclasses.asdict()` (a hand-composed object, a subset of fields, a $ref
array) still gets a correct TS mirror: whatever `coord/openapi.py` says the
wire shape is, is what ships to TypeScript.

`ENUM_OVERRIDES` below exists because JSON Schema (like the dataclasses
before it) can't express "this string is really one of these N values" —
`coord/openapi.py:json_schema_for` maps every `str` field to a bare
`{"type": "string"}`. These are hand-curated — update them alongside the
Python source when a new value is introduced. The `_ENUM_BLOCK` constants
(`AssignmentStatus`, `AssignmentType`, `TestVerdict`, `PipelineAction`) are
themselves hand-authored (not derived from a schema): they encode
wire-contract decisions — including actions the client supports that aren't
dispatched by `compute_pipeline` (e.g. "unstick") and forthcoming values
ahead of their backend implementation — that don't correspond 1:1 to a
single schema.

#2009 (epic #2002) — THIS SCRIPT IS NOW CROSS-REPO. The consumer it writes
for, `src/api/generated.ts`, moved to the `coord-web` repo along with the
rest of the webapp, but the *producer* — `coord.dashboard.server`'s OpenAPI
spec — is necessarily still here. So the destination is no longer a fixed
path inside this repo and must be named explicitly, by `--out PATH` or by
`$COORD_WEB_SRC` pointing at a `coord-web` checkout's root. There is
deliberately no fallback default: silently writing a hard-coded path that
this repo no longer contains would either recreate a dead directory nobody
consumes or, worse, report "up to date" against a file that does not exist.

That also relocates the drift GATE. It used to be `webapp-types` in
`.github/workflows/test.yml` (`python scripts/codegen.py --check`), which
could only work while both halves lived in one checkout; that job is gone.
The check now belongs to `coord-web`'s CI, which has its `generated.ts` and
installs `code-coordinator[server]` from PyPI (docs/ADR_COORD_WEB_CI.md,
#2006) to get this script. What still runs here is
`tests/test_generated_types_fixture.py`, narrowed to what a single checkout
can actually prove: that the generator produces complete, well-formed output
covering every schema in the served spec.

Usage:
    # regenerate into a coord-web checkout
    .venv/bin/python scripts/codegen.py --out ~/src/coord-web/src/api/generated.ts
    COORD_WEB_SRC=~/src/coord-web .venv/bin/python scripts/codegen.py
    # exit 1 (no write) if that file is stale
    COORD_WEB_SRC=~/src/coord-web .venv/bin/python scripts/codegen.py --check

#1941 — THIS SCRIPT ALSO GENERATES coord-tui's RUST WIRE TYPES. Unlike the TS
half above, `tui/` still lives in *this* repo, so there is no cross-repo
`--out`/env-var story: the Rust output path is a fixed, checked-in file,
`tui/src/app/types/generated.rs`. The source of truth is a *different*
OpenAPI document than the TS path reads — `coord.serve_app.openapi_spec()`
(the daemon app, port 7435), not `coord.dashboard.server.openapi_spec()` (the
dashboard app, port 7434) — because `GET /board`, the endpoint the TUI
actually polls, is specified there, from the seven explicit wire DTOs in
`coord/board_schema.py` (#1849). See `generate_rust()` below for the
mechanical schema-walk + hand-curated-override split, which mirrors
`ENUM_OVERRIDES`/`_ENUM_BLOCK` above but at the level of individual struct
fields rather than whole enum types (Rust's richer type system — visibility,
`#[serde(rename/default/deserialize_with)]`, custom deserializers for the
INTEGER-backed-boolean guard — needs a finer-grained override point than TS
did).

    # regenerate tui/src/app/types/generated.rs
    .venv/bin/python scripts/codegen.py --rust
    # exit 1 (no write) if that file is stale
    .venv/bin/python scripts/codegen.py --rust --check
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from typing import Any

from coord.dashboard.server import openapi_spec
from coord.serve_app import openapi_spec as board_openapi_spec

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Fixed, in-repo destination for the generated Rust wire types (#1941).
#: Unlike `OUTPUT_RELPATH` above, `tui/` never left this repo, so there is no
#: cross-repo destination ambiguity to resolve — no `--out`, no env var.
RUST_OUTPUT_PATH = REPO_ROOT / "tui" / "src" / "app" / "types" / "generated.rs"

#: Path of the emitted file RELATIVE to a `coord-web` checkout's root.
OUTPUT_RELPATH = Path("src") / "api" / "generated.ts"

#: Env var naming a `coord-web` checkout root, used when `--out` is absent.
OUTPUT_ENV_VAR = "COORD_WEB_SRC"


class OutputPathError(Exception):
    """No destination was named — see :func:`resolve_output_path`."""


def resolve_output_path(explicit: str | Path | None = None) -> Path:
    """Where to write/check ``generated.ts``: ``--out`` > ``$COORD_WEB_SRC``.

    Raises :class:`OutputPathError` when neither is set, rather than guessing
    (#2009): the old hard-coded ``coord/dashboard/webapp/src/api/generated.ts``
    is not in this repo any more, so a guess is always wrong and — under
    ``--check`` — wrong in the direction that reports success.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    root = os.environ.get(OUTPUT_ENV_VAR)
    if root:
        return Path(root).expanduser() / OUTPUT_RELPATH
    raise OutputPathError(
        "no destination for generated.ts. Since #2009 the webapp lives in "
        f"the coord-web repo, so pass --out PATH or set ${OUTPUT_ENV_VAR} to "
        f"a coord-web checkout root (the file is written to its "
        f"{OUTPUT_RELPATH}). See this script's module docstring."
    )

# Schemas to emit as TS interfaces, in display order — purely cosmetic (TS
# `interface` declarations are hoisted, so forward references within
# generated.ts are legal regardless of order). Anything present in the spec
# but not listed here is appended afterwards, sorted by name, so a newly
# schema-registered dataclass is never silently dropped.
SCHEMA_DISPLAY_ORDER: tuple[str, ...] = (
    "PipelineStage",
    "PipelineGate",
    "PipelineView",
    "Assignment",
)

# (schema name, field name) -> literal TS type, bypassing the mechanical
# JSON-Schema-to-TS mapping below. See module docstring for why these exist
# and where each value set comes from.
ENUM_OVERRIDES: dict[tuple[str, str], str] = {
    # coord/models.py Assignment.status: default "pending"; dao.TERMINAL_STATUSES
    # adds "done"/"merged"/"failed"/"cancelled"/"advisory"; "running" once dispatched.
    ("Assignment", "status"): "AssignmentStatus",
    # coord/models.py Assignment.type — see AssignmentType below for the real
    # value set (#1550 found and fixed a drifted hand enum here, see PR).
    ("Assignment", "type"): "AssignmentType",
    # coord/models.py Assignment.smoke_test docstring: "None | pass | fail".
    ("Assignment", "smoke_test"): "'pass' | 'fail' | null",
    # coord/models.py Assignment.review_state docstring: pending|dispatched|done.
    ("Assignment", "review_state"): "'pending' | 'dispatched' | 'done' | null",
    # coord/models.py Assignment.test_state mirrors pipeline.py's test_verdict.
    # #1395: TestVerdict includes 'running' — a transient, non-verdict value a
    # driver sets while it runs the suite locally; every reader compares
    # against the terminal values explicitly, so this never gates as a verdict.
    ("Assignment", "test_state"): "TestVerdict | null",
    # coord/models.py Assignment.review_verdict docstring: None | approve | request-changes.
    ("Assignment", "review_verdict"): "'approve' | 'request-changes' | null",
    # coord/pipeline.py PipelineView.review_verdict: same 2-value verdict.
    ("PipelineView", "review_verdict"): "'approve' | 'request-changes' | null",
    # coord/pipeline.py PipelineView.test_verdict mirrors Assignment.test_state.
    ("PipelineView", "test_verdict"): "TestVerdict | null",
    # coord/pipeline.py PipelineGate.action: real values emitted by
    # compute_pipeline (test-verdict, dispatch_review, dispatch_smoke, enqueue,
    # post_findings, record-review-verdict, dispatch_fix, merge, retry) are a
    # subset of the full PipelineAction contract below.
    ("PipelineGate", "action"): "PipelineAction",
    # coord/pipeline.py PipelineStage.status: the four literal values
    # compute_pipeline assigns (see its "Build stages list" section) —
    # "active" | "completed" | "skipped" | "waiting". #1550: this was
    # generated as a bare `string` before the OpenAPI-spec switch; verified
    # against the four literal assignments in coord/pipeline.py and tightened
    # here since a JSON Schema `{"type": "string"}` can't express it either.
    ("PipelineStage", "status"): "'active' | 'completed' | 'skipped' | 'waiting'",
}

# Hand-authored wire-contract enums — see module docstring for why these are
# not mechanically derived from a schema.
_ENUM_BLOCK = """\
export type AssignmentStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'advisory'
  | 'merged'

/**
 * coord/models.py Assignment.type's real value set — #1550 found this had
 * drifted: the hand-authored enum this replaces listed 'merge' and 'fix',
 * neither of which is ever a literal `type=` value (coord/config.py's #1137
 * audit note: a dedicated `type="merge"` was tried and reverted; `type="fix"`
 * was deliberately never introduced — both share `type="work"` with their
 * headless counterpart and are distinguished by `provider_name`/
 * `review_of_assignment_id` instead, see `attention_threshold_for`) — while
 * missing seven values that are real: 'audit' (coord/models.py docstring,
 * #885 --audit-of), and the six interactive session types from
 * coord/config.py's `INTERACTIVE_SESSION_TYPES` plus the two headless
 * lightweight-worker types from `_DEFAULT_ATTENTION_THRESHOLDS`.
 */
export type AssignmentType =
  | 'work'
  | 'review'
  | 'plan'
  | 'smoke'
  | 'conflict-fix'
  | 'mock-author'
  | 'test-author'
  | 'audit'
  | 'chat'
  | 'troubleshoot'
  | 'milestone-chat'
  | 'refinement'
  | 'new-issue-chat'
  | 'test-chat'

export type TestVerdict = 'passed' | 'failed' | 'skipped' | 'running'

/**
 * Actions supported by POST /api/pipeline/action.
 *
 * dispatch_review    — kick off an adversarial review assignment
 * dispatch_smoke     — kick off a smoke-test assignment
 * enqueue            — add to merge queue
 * merge              — merge a queued PR (must be in "pending" state)
 * post_findings      — post orphaned review findings to GitHub
 * unstick            — cancel a stuck assignment and mark it failed
 * retry              — (forthcoming) retry a failed work assignment
 * dispatch_fix       — (forthcoming) dispatch a fix for a test failure / review request-changes
 * test-verdict       — (forthcoming) record passed/failed/skipped test verdict
 * record-review-verdict — (forthcoming) record an approved/changes-requested review verdict
 */
export type PipelineAction =
  | 'dispatch_review'
  | 'dispatch_smoke'
  | 'enqueue'
  | 'merge'
  | 'post_findings'
  | 'unstick'
  | 'retry'
  | 'dispatch_fix'
  | 'test-verdict'
  | 'record-review-verdict'\
"""

HEADER = """\
/**
 * AUTO-GENERATED — DO NOT EDIT BY HAND.
 *
 * Generated by `scripts/codegen.py` from the dashboard's OpenAPI 3 spec
 * (`coord.dashboard.server.openapi_spec()`, itself built by
 * `coord/openapi.py` from `coord/models.py` / `coord/pipeline.py`) — #1550
 * (originally #750). Regenerate after any field change:
 *
 *     .venv/bin/python scripts/codegen.py
 *
 * `tests/test_generated_types_fixture.py` fails CI if this file drifts from
 * what the generator produces right now, so a stale checkout can't merge.
 */\
"""


def ts_type_from_schema(schema: dict[str, Any]) -> str:
    """Map a JSON Schema fragment (as produced by ``coord/openapi.py``'s
    ``json_schema_for``/``dataclass_schema``) to a TypeScript type string.

    Mirrors the shape of ``coord/openapi.py:json_schema_for`` structurally,
    just targeting TypeScript instead of building the schema itself.
    """
    if "$ref" in schema:
        base = schema["$ref"].rsplit("/", 1)[-1]
    elif "anyOf" in schema:
        base = " | ".join(ts_type_from_schema(s) for s in schema["anyOf"])
    else:
        json_type = schema.get("type")
        if json_type == "null":
            return "null"
        if json_type == "string":
            base = "string"
        elif json_type == "boolean":
            base = "boolean"
        elif json_type in ("integer", "number"):
            base = "number"
        elif json_type == "array":
            items = schema.get("items") or {}
            base = f"{ts_type_from_schema(items)}[]" if items else "unknown[]"
        elif json_type == "object":
            addl = schema.get("additionalProperties")
            if isinstance(addl, dict):
                base = f"Record<string, {ts_type_from_schema(addl)}>"
            else:
                base = "Record<string, unknown>"
        elif json_type is None:
            base = "unknown"
        else:
            raise TypeError(
                f"scripts/codegen.py: no TS mapping for JSON Schema type {json_type!r} "
                f"(schema={schema!r}) — add one to ts_type_from_schema()."
            )

    return f"{base} | null" if schema.get("nullable") else base


def emit_interface(name: str, schema: dict[str, Any]) -> str:
    properties: dict[str, Any] = schema.get("properties", {})
    lines = [f"export interface {name} {{"]
    for field_name, field_schema in properties.items():
        override = ENUM_OVERRIDES.get((name, field_name))
        ts = override if override is not None else ts_type_from_schema(field_schema)
        lines.append(f"  {field_name}: {ts}")
    lines.append("}")
    return "\n".join(lines)


def _ordered_schema_names(schemas: dict[str, Any]) -> list[str]:
    """#1550: display order for the emitted interfaces — see
    ``SCHEMA_DISPLAY_ORDER``'s docstring. Every schema in the spec is
    emitted; nothing is silently dropped."""
    known = [name for name in SCHEMA_DISPLAY_ORDER if name in schemas]
    unknown = sorted(name for name in schemas if name not in SCHEMA_DISPLAY_ORDER)
    return known + unknown


def generate() -> str:
    spec = openapi_spec()
    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})
    parts = [HEADER, _ENUM_BLOCK]
    parts.extend(emit_interface(name, schemas[name]) for name in _ordered_schema_names(schemas))
    return "\n\n".join(parts) + "\n"


# ── Rust wire types (#1941) ─────────────────────────────────────────────────
#
# Mirrors the TS path above structurally: a mechanical JSON-Schema walk
# (`rust_type_from_schema`, the Rust analogue of `ts_type_from_schema`) plus
# hand-curated overrides for anything the mechanical mapping can't express.
# The Rust side needs a *finer-grained* override point than TS's
# `ENUM_OVERRIDES` dict (a bare type-string swap) because Rust structs carry
# per-field serde attributes (`rename`, `default`, `deserialize_with`) and
# visibility that a JSON Schema has no way to encode at all — so the override
# unit here is a whole field declaration (`RustField`), not just a type.
#
# Every field this repo's Rust wire structs declare *today* has an explicit
# override in `RUST_FIELD_OVERRIDES`, pinning its exact current name / type /
# attributes — so re-running the generator right now is a byte-for-byte no-op
# on every field the TUI already consumes (see `tests/test_generated_rust_
# fixture.py`). A schema field with NO override entry — i.e. one board_schema.py
# has declared that nothing in the Rust struct mirrors yet — falls back to
# `_rust_auto_field`: a mechanically-typed, `#[serde(default)]`,
# `#[allow(dead_code)]` field. That fallback is *why* the CI gate works without
# hand-editing this script for every new column: add a field to
# `coord.board_schema`, and it appears in the regenerated output automatically,
# via the fallback, with no override-table edit required — only a field whose
# *behavior* needs to diverge from the mechanical default (a rename, a custom
# deserializer, a deliberately-non-Option type) ever needs a real override.
#
# `RUST_EXTRA_FIELDS` is the one asymmetry with the TS path: `board_wire.py`
# stamps a handful of additive, wire-only fields onto assignment/merge-queue
# rows *after* they leave `coord.board_schema`'s dataclasses (`review_findings_
# truncated`, `body_truncated`, …, #1337/#2497) — real fields on the actual
# `/board` wire that the schema-walk above can never see, because they aren't
# declared on any dataclass. Each entry here is a deliberate, hand-maintained
# exception, not something codegen could discover on its own.


@dataclasses.dataclass(frozen=True)
class RustField:
    """One emitted Rust struct field.

    ``serde`` holds the *args* of a single ``#[serde(...)]`` attribute (e.g.
    ``("rename = \\"type\\"", "default")``) — empty means no ``#[serde(...)]``
    attribute at all (a required field with no rename, same as today's
    `Assignment::status`). ``extra_attrs`` are whole attribute lines rendered
    verbatim above the field (``#[allow(dead_code)]``). ``doc`` lines are
    rendered as ``///`` doc comments, in order, above any ``extra_attrs``.
    """

    name: str
    ty: str
    serde: tuple[str, ...] = ()
    extra_attrs: tuple[str, ...] = ()
    doc: tuple[str, ...] = ()


def _f(
    name: str,
    ty: str,
    *,
    rename: str | None = None,
    default: bool = True,
    deserialize_with: str | None = None,
    dead_code: bool = False,
    doc: tuple[str, ...] = (),
) -> RustField:
    """Build a `RustField` override — the common case (rename/default/
    deserialize_with knobs) without hand-assembling the `#[serde(...)]` arg
    list each time."""
    serde: list[str] = []
    if rename is not None:
        serde.append(f'rename = "{rename}"')
    if default:
        serde.append("default")
    if deserialize_with is not None:
        serde.append(f'deserialize_with = "{deserialize_with}"')
    return RustField(
        name=name,
        ty=ty,
        serde=tuple(serde),
        extra_attrs=("#[allow(dead_code)]",) if dead_code else (),
        doc=doc,
    )


#: schema name -> Rust struct name, in emission order. Matches
#: `coord.board_schema.BOARD_PROJECTIONS`'s own declaration order.
RUST_STRUCTS: tuple[tuple[str, str], ...] = (
    ("BoardAssignment", "Assignment"),
    ("BoardMachine", "RawMachine"),
    ("BoardMergeQueueEntry", "MergeQueueEntry"),
    ("BoardProposal", "Proposal"),
    ("BoardIssue", "OpenIssue"),
    ("BoardDriveEscalation", "EscalationEntry"),
    ("BoardDriveQueueEntry", "BoardDriveQueueEntry"),
)

#: schema name -> struct-level declaration metadata (visibility, derives,
#: any struct-level attributes, and the struct's own doc comment).
RUST_STRUCT_META: dict[str, dict[str, Any]] = {
    "BoardAssignment": {
        "vis": "pub",
        "derives": ("Clone", "serde::Deserialize"),
        "attrs": (),
        "doc": (
            "One `assignments` row as `/board` carries it — the wire shape of",
            "`coord.board_schema.BoardAssignment` (#1849/#1941).",
            "",
            "#1042: `pub`, not `pub(crate)` — it's a parameter/return type of the",
            "`test-support`-feature-gated fixtures in `app::fixtures` (e.g.",
            "`make_app_with_assignments(Vec<Assignment>)`), which must be at least as",
            "visible as those `pub fn`s (E0446) to be reachable from an external",
            "integration-test crate. Fields stay `pub(crate)`: nothing outside the",
            "crate constructs one field-by-field, only via `app::fixtures` helpers or",
            "`serde::Deserialize`.",
        ),
    },
    "BoardMachine": {
        "vis": "pub(crate)",
        "derives": ("serde::Deserialize",),
        "attrs": (),
        "doc": (
            "#584: a machine row as it arrives on the `coord serve` /board wire.",
            "",
            "`Machine` itself carries probe-only fields (reachable / active_count /",
            "version / worktree_bytes) that never appear in the payload, so we",
            "deserialize into this minimal shape and let `assemble_board_data` run the",
            "reachability + health probes exactly like the SQLite path does.",
        ),
    },
    "BoardMergeQueueEntry": {
        "vis": "pub(crate)",
        "derives": ("Clone", "serde::Deserialize"),
        "attrs": ("#[allow(dead_code)] // pr_url stored for future display",),
        "doc": (),
    },
    "BoardProposal": {
        "vis": "pub(crate)",
        "derives": ("Clone", "serde::Deserialize"),
        "attrs": (),
        "doc": (),
    },
    "BoardIssue": {
        "vis": "pub(crate)",
        "derives": ("Clone", "serde::Deserialize"),
        "attrs": (),
        "doc": (
            "An open issue from the local `issues` table (synced from GitHub on coord plan).",
        ),
    },
    "BoardDriveEscalation": {
        "vis": "pub(crate)",
        "derives": ("Clone", "Debug", "Default", "serde::Deserialize"),
        "attrs": (),
        "doc": (
            '#1505: one board-visible "driver stuck" record — `coord drive`\'s merge',
            "stage hit a status no amount of retrying can fix (NEEDS_ATTENTION / an",
            "unrecognised status) and escalated instead of burning the merge-attempt",
            "budget on it. A raw dump of the `drive_escalations` table row, one",
            "(repo_name, issue_number) at a time — see `coord/db.py`'s table comment",
            "and `coord.drive._escalate_merge`.",
        ),
    },
    "BoardDriveQueueEntry": {
        "vis": "pub(crate)",
        "derives": ("Clone", "Debug", "Default", "serde::Deserialize"),
        "attrs": (),
        "doc": (
            "#1753 (DQ-1) / #1755 (DQ-3): one row of the operator-declared `coord",
            "drive` work queue — the wire shape of `/board`'s `drive_queue` array",
            "(`coord.board_schema.BoardDriveQueueEntry`, a raw `drive_queue` table dump",
            "via `coord.dao.SqliteStore.board_projection`; see `coord/db.py`'s table",
            "comment for the storage contract).",
            "",
            "**Every field is `#[serde(default)]` on purpose.** `/board` is one",
            "payload: a single type mismatch on a single field fails the *entire*",
            "`BoardPayload` parse and blanks every panel with no error message — the",
            "#632/#546/#628 class this repo has been bitten by three times.",
            "`tests.rs`'s `board_payload_deserializes_real_sample` round-trips the",
            "golden fixture (which now carries a populated `drive_queue`) so schema",
            "drift fails a test instead.",
        ),
    },
}

#: (schema name -> {python field name -> RustField}) — see module docstring.
RUST_FIELD_OVERRIDES: dict[str, dict[str, RustField]] = {
    "BoardAssignment": {
        "assignment_id": _f("id", "String", rename="assignment_id", default=False),
        "machine_name": _f("machine", "String", rename="machine_name", default=False),
        "repo_name": _f("repo", "String", rename="repo_name", default=False),
        "issue_number": _f("issue_number", "u64", default=False),
        "issue_title": _f("issue_title", "String", default=False),
        "status": _f("status", "String", default=False),
        "type": _f("assignment_type", "Option<String>", rename="type"),
        "branch": _f("branch", "Option<String>"),
        "model": _f("model", "Option<String>"),
        "dispatched_at": _f("dispatched_at", "Option<f64>"),
        "finished_at": _f("finished_at", "Option<f64>"),
        "exit_code": _f("exit_code", "Option<i32>"),
        "test_state": _f(
            "test_state", "Option<String>",
            doc=(
                '#200: human-driven Test gate verdict for type="work" assignments.',
                'None | "passed" | "failed" | "skipped".',
            ),
        ),
        "review_verdict": _f(
            "review_verdict", "Option<String>",
            doc=(
                '#253: parsed adversarial-review verdict for type="review" assignments.',
                'None | "approve" | "request-changes".  Drives the merge-gate hint',
                "swap so the user sees the block before pressing m.",
            ),
        ),
        "review_of_assignment_id": _f(
            "review_of_assignment_id", "Option<String>",
            doc=(
                "#253: links a review assignment back to the work assignment it",
                "reviews — needed to pair review_verdict with the merge entry.",
            ),
        ),
        "cost_usd": _f(
            "cost_usd", "Option<f64>",
            doc=(
                "#208: worker cost captured from the final stream-json result event.",
                "`None` for in-flight workers and for pre-#208 rows.",
            ),
        ),
        "smoke_tests": _f(
            "smoke_tests", "Option<Vec<String>>",
            doc=(
                "#252: worker-emitted smoke-test list, parsed from the SMOKE_TESTS",
                "block in the worker's log.",
                "",
                "* `None`     — no block found (graceful degradation: TUI shows",
                '              "inspect the diff" placeholder).',
                '* `Some([])` — explicit "(none — change is internal)" form.',
                "* `Some(vec)` — bullets to render under the Test stage.",
                "",
                "#584: on the /board wire this is a real JSON array (already decoded),",
                "so plain serde handles it.",
            ),
        ),
        "review_findings": _f(
            "review_findings", "Option<String>",
            doc=(
                "#bounce: cached review findings (verdict + body), JSON-encoded",
                "in the DB column.  `None` for non-review assignments and for",
                "reviews completed before the cache landed.",
                "",
                "#584: intentionally kept as a raw JSON STRING on the /board wire, so",
                "plain serde deserialization works.",
            ),
        ),
        "test_plan": _f(
            "test_plan", "Option<Vec<TestPlanStep>>",
            deserialize_with="deserialize_test_plan",
            doc=(
                '#349 Phase B: AI-generated smoke-test plan for type="work" assignments.',
                "Parsed from the JSON blob in `assignments.test_plan`.  `None` = not",
                "yet generated (TUI will spawn `coord test-plan` to fill it in).",
                "",
                '#584: on the /board wire this is a decoded OBJECT `{"steps":[...]}`,',
                "not an array — see [`deserialize_test_plan`].",
            ),
        ),
        "test_plan_branch_head": _f(
            "test_plan_branch_head", "Option<String>",
            doc=(
                "#349 Phase B: git branch HEAD SHA at the time the cached test_plan was",
                "generated.  `None` when no plan exists or when it was generated without",
                "branch tracking.  Used to detect staleness (branch advanced →",
                "auto-refresh via `coord test-plan --refresh`).",
            ),
        ),
        "input_tokens": _f(
            "input_tokens", "i64",
            doc=(
                "#546: token counts for automated (claude -p) assignments, parsed from",
                "the final stream-json result event alongside `cost_usd`.",
                "0 for interactive (Claude Max / OAuth) sessions — those have no",
                'per-token billing and the TUI shows "Max" instead of a $ figure.',
            ),
        ),
        "output_tokens": _f("output_tokens", "i64"),
        "cache_creation_tokens": _f(
            "cache_creation_tokens", "i64", dead_code=True,
            doc=(
                "populated from DB / SSE; not yet read in TUI render (#818 removed the "
                "Stages tab that displayed them)",
            ),
        ),
        "cache_read_tokens": _f(
            "cache_read_tokens", "i64", dead_code=True,
            doc=("see `cache_creation_tokens` above",),
        ),
        "is_interactive": _f(
            "is_interactive", "bool",
            deserialize_with="de_bool_from_int_or_bool",
            doc=(
                "#546: true when the assignment ran as a human-attended interactive session",
                "(Max / Pro subscription).  Set by `finalize_interactive_exit`; prevents",
                "misidentifying old automated rows (which also have cost_usd=NULL and zero",
                "token counts) as Max sessions.",
                "",
                "#628 hotfix: the daemon serializes this SQLite boolean as an int (0/1), so",
                "a strict `bool` here fails the ENTIRE /board parse on `is_interactive:0`,",
                "returning BoardData::default() and blanking every panel. Accept int-or-bool.",
            ),
        ),
        "failure_reason": _f(
            "failure_reason", "Option<String>",
            doc=(
                "#618: short human-readable reason written immediately when an interactive",
                'session fails to launch (e.g. "branch already checked out at <path>").',
                "`None` for assignments that launched successfully.  Shown in the",
                "assignment detail panel so the TUI explains the red box without a log file.",
            ),
        ),
        "review_iteration": _f(
            "review_iteration", "i64",
            doc=(
                "#803: fix-round counter — 0 on the original work assignment, N on the",
                "N-th fix.  Used to compute the next iteration's escalated model via",
                "[`fix_model_for_iteration`]: `next_iteration = review_iteration + 1`.",
            ),
        ),
        "acceptance_state": _f(
            "acceptance_state", "Option<String>",
            doc=(
                "#932/#944: the Acceptance-gate verdict (oracle loop,",
                'docs/ORACLE_LOOP.md) for type="work" assignments, stamped by `coord',
                "acceptance record --issue N --sha <sha>` — the coordinator's",
                "external re-run of the sealed suite against the pushed SHA.",
                'None | "passed" | "failed". Reported and gated SEPARATELY from',
                "`test_state` — its own box, its own verdict.",
            ),
        ),
        "acceptance_reason": _f(
            "acceptance_reason", "Option<String>",
            doc=('Short failing-test summary when `acceptance_state == "failed"`.',),
        ),
        "acceptance_sha": _f(
            "acceptance_sha", "Option<String>",
            doc=("SHA the last `acceptance record` verdict was recorded against.",),
        ),
        "acceptance_total": _f(
            "acceptance_total", "Option<i64>",
            doc=(
                "#932: per-test counts from the same verdict, so the Acceptance box",
                'can read as partial progress ("3/7 acceptance green") rather than a',
                "bare pass/fail — a growing suite is expected to be sub-100% until",
                "the feature completes. `None` for rows predating this column.",
            ),
        ),
        "acceptance_passed": _f("acceptance_passed", "Option<i64>"),
        "test_reason": _f(
            "test_reason", "Option<String>",
            doc=(
                "#876: test failure reason entered by the operator via `coord test --fail`",
                "(written by `record_test_verdict`).  `None` for assignments without a",
                "failed test or for pre-#876 rows.",
            ),
        ),
        "review_state": _f(
            "review_state", "Option<String>", dead_code=True,
            doc=(
                'Internal review-state machine value: "pending" | "done" etc.',
                "`None` for non-review assignments and for pre-#876 rows.",
                "Deserialized from the board for future display; not yet read in",
                "production rendering paths.",
            ),
        ),
        "pr_url": _f(
            "pr_url", "Option<String>",
            doc=(
                "URL to the pull request for this work assignment (populated when the",
                "worker pushes a branch and the coordinator records it).",
                "`None` when no PR URL is known.",
            ),
        ),
        "audit_goals_json": _f(
            "audit_goals_json", "Option<String>", dead_code=True,
            doc=(
                "#886 Phase 2: Milestone Outcome Audit structured verdict, set only on",
                '`type="audit"` assignments (see #885\'s `--audit-of`). Kept as a raw',
                "JSON STRING on the wire, same convention as `review_findings` above.",
                "The Plans panel renders the *aggregated* latest-run-per-milestone view",
                "via `PlanRosterEntry`'s `outcome_*` fields, not this raw column — these",
                "are present for a possible future per-assignment audit detail view.",
            ),
        ),
        "audit_bottom_line": _f("audit_bottom_line", "Option<String>", dead_code=True),
        "audit_run_number": _f("audit_run_number", "Option<i64>", dead_code=True),
        "for_issue_number": _f(
            "for_issue_number", "Option<u64>",
            doc=(
                '#1084: for `type="test-author"` JIT-mode assignments, the specific',
                "work-order member issue this dispatch is extending the acceptance",
                "suite for (`coord.test_author.dispatch_test_author`'s `issue_number`",
                "argument) — NOT the same as `issue_number` above, which test-author",
                "always sets to the milestone's *tracking* issue (every JIT dispatch",
                "for a milestone shares one branch/PR). `None` for milestone-mode",
                "(Gate A) authoring, every other assignment type, and rows predating",
                "this column. Used by the per-issue Acceptance-Authoring mini-",
                "pipeline to attribute a shared-branch assignment row back to the",
                "right member issue's row.",
            ),
        ),
        "driven_by": _f(
            "driven_by", "Option<String>",
            doc=(
                '#1499: durable provenance — `Some("drive:<repo>#<issue>")` when this',
                "assignment was dispatched by `coord drive` (via `coord assign",
                "--driven-by`), `None` for a hand `coord assign` and for rows",
                "predating this column. This is what lets the Pipeline distinguish a",
                "drive-dispatched row from a hand dispatch (and, combined with",
                '`drive_sessions`, "drive exited unfinished" from "never driven")',
                "even after the driver's tmux session is long gone — see",
                "`CoordApp::issue_has_drive_provenance` in `drive.rs`.",
            ),
        ),
        "dispatched_by_assignment_id": _f(
            "dispatched_by_assignment_id", "Option<String>",
            doc=(
                "#2417: the CALLING assignment's id when this row was dispatched by a",
                "`coord` subcommand run from INSIDE another worker's own turn (e.g. a",
                '`type="work"` session shelling out to `coord acceptance author repo',
                "ms --issue N` or `coord fix <other-id>`), as opposed to a human",
                "typing the same command in their own shell. `None` for a hand or",
                "coordinator/brain dispatch, and for rows predating this column.",
                "Mirrors `coord.models.Assignment.dispatched_by_assignment_id`. Read",
                "in reverse via [`crate::app::CoordApp::dispatched_children`]: given",
                "an ORIGIN row, find every assignment whose value here equals the",
                "origin's `id` — that reverse lookup is what lets the origin row show",
                '"→ dispatched test-author <id> — running/done" instead of the',
                "operator having to grep the raw worker transcript (#2417).",
            ),
        ),
    },
    "BoardMachine": {
        "name": _f("name", "String", default=False),
        "host": _f("host", "String", default=False),
        "repos": _f("repos", "Vec<String>"),
    },
    "BoardMergeQueueEntry": {
        "assignment_id": _f("assignment_id", "String", default=False),
        "issue_number": _f("issue_number", "Option<u64>"),
        "state": _f("state", "String", default=False),
        "pr_number": _f("pr_number", "Option<i64>"),
        "pr_url": _f("pr_url", "Option<String>"),
        "repo_github": _f(
            "repo_github", "String", default=False,
            doc=(
                "Repo slug (owner/name) — keys the board-synced CI summary lookup in",
                "`pipeline_ci_checks` (see [`PlannedMergeEntry::ci_summary`]).",
                "Joined from the `merge_queue.repo_github` column.",
            ),
        ),
        "target_branch": _f(
            "target_branch", "Option<String>",
            doc=(
                'Target branch the PR merges into (e.g. "main").  `None` for entries',
                "written before this column was read by the TUI.",
            ),
        ),
        "error": _f(
            "error", "Option<String>",
            doc=(
                "Last gate-eval error string from `coord merge`, if any.  Non-empty",
                "is the single most useful clue when a merge is stalled.",
            ),
        ),
        "branch": _f(
            "branch", "Option<String>",
            doc=(
                "Worker branch name — joined from `assignments.branch`.  Used by",
                "`compute_staging_local` for branch-level dedup (#778): a fix worker",
                "that shares a branch with an already-queued original work assignment",
                "must be excluded from staging even though it has a different assignment_id.",
            ),
        ),
        "last_attempt": _f(
            "last_attempt", "Option<f64>",
            doc=(
                "Unix timestamp of the last merge attempt (`coord/merge_queue.py` sets",
                "this immediately before calling `gh pr merge`, and leaves it untouched",
                'on success) — for a `state == "merged"` entry this IS the merge time.',
                "#913: the Pipeline Done section's recency window + sort use this (via",
                "`issue_done_at`) instead of the work assignment's `finished_at`, so a",
                "freshly-merged item lands in Done immediately rather than being keyed",
                "off however long ago the work itself finished.",
            ),
        ),
    },
    "BoardProposal": {
        "id": _f("id", "i64", default=False),
        "machine_name": _f("machine", "String", rename="machine_name", default=False),
        "repo_name": _f("repo", "String", rename="repo_name", default=False),
        "issue_number": _f("issue_number", "u64", default=False),
        "issue_title": _f("issue_title", "String", default=False),
        "rationale": _f("rationale", "String", default=False),
        "type": _f("proposal_type", "String", rename="type", default=False),
    },
    "BoardIssue": {
        "repo_name": _f("repo_name", "String", default=False),
        "number": _f("number", "u64", default=False),
        "title": _f("title", "String", default=False),
        "body": _f(
            "body", "String",
            doc=(
                "Issue body, synced from GitHub via `coord sync`.  Empty string when",
                "the issue has no description.",
            ),
        ),
        "labels": _f(
            "labels", "Vec<String>",
            doc=(
                "GitHub labels on this issue. Used by the Board Issue tab to render the",
                "same context the Pipeline Issue tab shows.",
            ),
        ),
        "state": _f(
            "state", "String", default=False,
            doc=(
                '"open" | "closed".  We load both into `data.open_issues` so the Board',
                "Issue tab can display bodies for closed issues (e.g. in the Completed",
                'group), but only "open" entries get injected as Pending rows.',
            ),
        ),
        "milestone_number": _f(
            "milestone_number", "Option<i64>",
            doc=("#406: GitHub milestone number.  `None` for issues without a milestone.",),
        ),
        "milestone_title": _f(
            "milestone_title", "Option<String>",
            doc=('#406: GitHub milestone title (e.g. "v0.5").  `None` when no milestone.',),
        ),
    },
    "BoardDriveEscalation": {
        "id": _f("id", "i64", default=False, dead_code=True),
        "repo_name": _f("repo_name", "String", default=False),
        "issue_number": _f("issue_number", "i64", default=False),
        "stage": _f(
            "stage", "String",
            doc=(
                '#2427: which stage box on the Overview strip gets the "blocked" mark',
                "— see `CoordApp::build_pipeline_widget`'s `esc.stage == name` check.",
            ),
        ),
        "assignment_id": _f("assignment_id", "Option<String>", dead_code=True),
        "reason": _f("reason", "String", default=False),
        "gate_readings": _f(
            "gate_readings", "String", dead_code=True,
            doc=(
                'Human-readable "key=value | key=value" summary of the gate readings',
                "the driver observed — deliberately NOT JSON (see `coord/db.py`): this",
                "record exists to be read by a human, not machine-parsed.",
                "",
                "Not yet surfaced in the TUI (unlike `stage`, #2427).",
            ),
        ),
        "proposed_command": _f("proposed_command", "String", default=False),
        "created_at": _f("created_at", "Option<f64>", dead_code=True),
    },
    "BoardDriveQueueEntry": {
        "repo_name": _f("repo_name", "String"),
        "issue_number": _f("issue_number", "i64"),
        "position": _f(
            "position", "i64",
            doc=(
                "Dense, 0-based run order (`coord/db.py`: no fractional positions —",
                "`move` renumbers the affected span in one transaction).",
            ),
        ),
        "machine": _f(
            "machine", "Option<String>",
            doc=(
                '`None`/absent means "let `coord drive` route it" — NOT "unpinned is',
                'an error".',
            ),
        ),
        "after_json": _f(
            "after", "Vec<String>", rename="after_json",
            doc=(
                'Pre-req keys (`"repo#N"`) that must land before this entry may start.',
                "",
                "Named `after` here but renamed from the wire's `after_json`: the",
                "column is a JSON *string* in SQLite and `coord.dao._JSON_COLUMNS`",
                "decodes it to a real array on the way out, keeping the column name.",
                "A plain `after` field would silently stay empty forever.",
            ),
        ),
        "state": _f(
            "state", "String",
            doc=(
                "`waiting` | `running` | `blocked` | `done` | … — decided by DQ-2's",
                "tick, never re-derived here (same posture as `fleet_health.rs`'s",
                '"renderers consume `severity` verbatim").',
            ),
        ),
        "attempts": _f("attempts", "i64"),
        "deferrals": _f(
            "deferrals", "i64",
            doc=(
                "How many ticks skipped this entry because it wasn't eligible yet.",
                "Surfaced in the overlay next to `last_reason` — a row that keeps",
                "being passed over is the thing an operator most needs to see.",
            ),
        ),
        "last_reason": _f("last_reason", "String"),
        "reason_at": _f(
            "reason_at", "Option<f64>",
            doc=(
                "#2133: capture time of `last_reason` — `last_reason` is a",
                "point-in-time observation, never re-validated, so the Queue panel",
                "age-stamps it (`queue_row`) rather than rendering it bare as if it",
                "were current state. `None` for a row predating the migration or one",
                'whose `last_reason` is still the `""` default.',
            ),
        ),
        "launched_at": _f(
            "launched_at", "Option<f64>", dead_code=True,
            doc=("wire parity; the Queue panel shows `state`, not the clock",),
        ),
        "hold_after": _f(
            "hold_after", "i64",
            doc=(
                "#1757: this entry ends with a DEPLOY GATE — when it completes, the",
                "tick launches nothing until a human deploys and releases it.",
                "",
                "SQLite stores 0/1 in an `INTEGER` column, so this is `i64`, not",
                "`bool`: an `INTEGER` column deserialised into a Rust `bool` is the",
                "exact #632/#546/#628 type mismatch that blanks the whole board (see",
                "`tests/test_board_fixture.py::",
                "test_no_unguarded_integer_bool_columns_reach_the_wire`).",
            ),
        ),
        "hold_reason": _f(
            "hold_reason", "String",
            doc=("What the operator must do while the gate is held — rendered verbatim.",),
        ),
        "hold_state": _f(
            "hold_state", "String",
            doc=(
                '`""` | `armed` | `fired` | `released` — decided by the tick, consumed',
                "verbatim here (same posture as `state` above).",
            ),
        ),
        "hold_probes": _f(
            "hold_probes", "i64",
            doc=(
                "Consecutive failed `resume_when` runs since the gate fired. A rising",
                "count is how a gate that never clears becomes visible instead of",
                "silent, so it is carried on the wire rather than re-derived.",
            ),
        ),
        "hold_scope": _f(
            "hold_scope", "String",
            doc=(
                '#2186: how FAR a fired gate reaches — `"entry"` (the default) holds',
                'only entries whose own `after` names this gate\'s key; `"fleet"` is',
                "the pre-#2186 whole-queue stop, now opt-in only.",
                "",
                "Fail-closed, mirroring `coord.drive_queue.QueueEntry.",
                '_normalize_hold_scope`: `""` (a row predating this column, or any',
                "other server this build has never heard of) reads as entry-scoped,",
                "the narrower/safer reading — NEVER as fleet-wide. See",
                "`drive_queue::stops_fleet`, the one place this is consulted.",
            ),
        ),
    },
}

#: Rust struct name -> extra fields with NO backing schema field at all —
#: `coord/board_wire.py` stamps these onto the row after it leaves
#: `coord.board_schema`'s dataclasses (see module docstring above).
RUST_EXTRA_FIELDS: dict[str, tuple[RustField, ...]] = {
    "Assignment": (
        RustField(
            name="review_findings_truncated", ty="bool", serde=("default",),
            doc=(
                "#1337: true when the daemon bounded `review_findings` on the /board",
                "wire (the collection carries a preview; the full body lives on",
                "`GET /assignment/{id}`).  Absent (→ false) on pre-#1337 daemons and",
                "on the local-SQLite path, both of which carry the full text.",
                "",
                "Not part of `coord.board_schema.BoardAssignment` — stamped onto the row",
                "afterward by `coord/board_wire.py`'s per-field wire-bounding policy, so",
                "it can't be picked up by the schema-driven codegen above; kept here by",
                "hand alongside its `_len` sibling.",
            ),
        ),
        RustField(
            name="review_findings_len", ty="Option<i64>", serde=("default",),
            doc=(
                "#1337: full stored length of `review_findings` when truncated —",
                "combined with the assignment id as the detail-fetch cache key, so a",
                "force-overwritten review (different length) re-fetches.",
            ),
        ),
    ),
    "MergeQueueEntry": (
        RustField(
            name="milestone_title", ty="Option<String>", serde=("default",),
            doc=(
                "Milestone title — client-side join from `open_issues` keyed on",
                "`(repo_name, issue_number)`.  `None` when the issue carries no",
                "milestone or the issue row is absent from `open_issues`.",
                "",
                "Not part of `coord.board_schema.BoardMergeQueueEntry` — populated by a",
                "client-side join, not the wire itself; kept here by hand.",
            ),
        ),
    ),
    "OpenIssue": (
        RustField(
            name="body_truncated", ty="bool", serde=("default",),
            doc=(
                "#2497: true when the `/board` wire bounded `body`. Set for every",
                "NON-EPIC issue, closed or open: `board_wire.bound_issue_row` drops a",
                "closed body to 0 chars (#1791) and an open one to its machine-parsed",
                "`**Allowed:**` residue (#1939), because the Issue tabs hydrate the",
                "real text lazily from `GET /issue/{repo}/{number}`. Absent only for",
                "epic bodies, which stay inline for the client-side Milestone DAG",
                "parse. `#[serde(default)]` so an older daemon (pre-#2497, never",
                "stamps this) deserializes as `false` — the pre-existing behavior of",
                "trusting `body` verbatim.",
                "",
                "Not part of `coord.board_schema.BoardIssue` — stamped onto the row",
                "afterward by `coord/board_wire.py`; kept here by hand alongside its",
                "`_len` sibling, same posture as `Assignment::review_findings_truncated`.",
            ),
        ),
        RustField(
            name="body_len", ty="Option<i64>", serde=("default",),
            doc=(
                "#2497: the issue's full body length before truncation. `Some` only",
                "when `body_truncated` is true; drives the Issue tab's hydration gate",
                "(`CoordApp::issue_body_fetch_target`).",
            ),
        ),
    ),
}


def rust_type_from_schema(schema: dict[str, Any]) -> str:
    """Map a JSON Schema fragment to a Rust type string — the Rust analogue
    of `ts_type_from_schema` above. Nullability is handled by the caller
    (`_rust_auto_field` wraps in `Option<...>`), not folded in here, because
    an override's exact `Option`-ness is a per-field editorial decision on
    the Rust side (e.g. `is_interactive` is nullable in the schema but a
    plain non-`Option` `bool` in Rust, defaulting via `#[serde(default)]`).
    """
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        return "serde_json::Value"
    json_type = schema.get("type")
    if json_type == "string":
        return "String"
    if json_type == "boolean":
        return "bool"
    if json_type == "integer":
        return "i64"
    if json_type == "number":
        return "f64"
    if json_type == "array":
        items = schema.get("items") or {}
        inner = rust_type_from_schema(items) if items else "serde_json::Value"
        return f"Vec<{inner}>"
    if json_type == "object":
        return "serde_json::Value"
    if json_type is None:
        return "serde_json::Value"
    raise TypeError(
        f"scripts/codegen.py: no Rust mapping for JSON Schema type {json_type!r} "
        f"(schema={schema!r}) — add one to rust_type_from_schema()."
    )


def _rust_auto_field(python_name: str, schema: dict[str, Any]) -> RustField:
    """Fallback for a schema field with no `RUST_FIELD_OVERRIDES` entry — see
    module docstring for why this, not a hand-edit, is what makes the CI
    `--check` gate a real drift detector."""
    base = rust_type_from_schema(schema)
    ty = f"Option<{base}>" if schema.get("nullable") else base
    return RustField(
        name=python_name,
        ty=ty,
        serde=("default",),
        extra_attrs=("#[allow(dead_code)]",),
        doc=("Wire field from `coord.board_schema` (#1941) — no TUI consumer yet.",),
    )


def _render_rust_field(field: RustField) -> list[str]:
    lines = [f"    /// {d}".rstrip() if d else "    ///" for d in field.doc]
    lines.extend(f"    {attr}" for attr in field.extra_attrs)
    if field.serde:
        lines.append(f"    #[serde({', '.join(field.serde)})]")
    lines.append(f"    pub(crate) {field.name}: {field.ty},")
    return lines


def emit_rust_struct(schema_name: str, rust_name: str, schemas: dict[str, Any]) -> str:
    meta = RUST_STRUCT_META[schema_name]
    overrides = RUST_FIELD_OVERRIDES.get(schema_name, {})
    properties: dict[str, Any] = schemas[schema_name].get("properties", {})

    lines: list[str] = [f"/// {d}".rstrip() if d else "///" for d in meta["doc"]]
    lines.extend(meta["attrs"])
    lines.append(f"#[derive({', '.join(meta['derives'])})]")
    lines.append(f"{meta['vis']} struct {rust_name} {{")
    extras = RUST_EXTRA_FIELDS.get(rust_name, ())
    # #1939: an extras entry WINS over the mechanical walk when the spec also
    # declares that property. `RUST_EXTRA_FIELDS` exists for the wire-only
    # `<field>_truncated`/`<field>_len` flags `board_wire.py` stamps on, and
    # `/openapi.json` now declares them too (it always should have — they were
    # just rare enough on the wire that nothing noticed). Without this the walk
    # and the table would BOTH emit them: duplicate struct fields, and the
    # auto-derived `body_len: i64` would shadow the hand-written
    # `Option<i64>` that `issue_body_fetch_target`'s `len?` gate depends on.
    extra_names = {f.name for f in extras}
    for python_name, field_schema in properties.items():
        if python_name in extra_names:
            continue
        field = overrides.get(python_name) or _rust_auto_field(python_name, field_schema)
        lines.extend(_render_rust_field(field))
    for field in extras:
        lines.extend(_render_rust_field(field))
    lines.append("}")
    return "\n".join(lines)


RUST_HEADER = """\
//! AUTO-GENERATED — DO NOT EDIT BY HAND.
//!
//! Generated by `scripts/codegen.py --rust` from `coord.serve_app.openapi_spec()`'s
//! `/board` response schema — itself built from the explicit wire DTOs in
//! `coord/board_schema.py` (#1849) — #1941. Regenerate after any field change:
//!
//! ```text
//! .venv/bin/python scripts/codegen.py --rust
//! ```
//!
//! (The fence is load-bearing, and `text` is the load-bearing part of *it*:
//! a 4-space-indented block in a doc comment is a Markdown code block, and
//! rustdoc compiles an unannotated code block as a Rust doctest — so writing
//! that command as an indented block, or in a bare ``` fence, breaks
//! `cargo test --doc` with `error: expected item, found `.``.)
//!
//! `tests/test_generated_rust_fixture.py` fails CI if this file drifts from what
//! the generator produces right now, so a stale checkout can't merge — the Rust
//! equivalent of `tests/test_generated_types_fixture.py` on the TS side.
//!
//! **Every field defaults.** `/board` is one JSON payload: a single type
//! mismatch on a single field fails the *entire* `BoardPayload` parse and
//! blanks every TUI panel — the #632/#546/#628 failure class this repo has
//! been bitten by three times (see `coord/board_bool_guard.py`). INTEGER-backed
//! boolean columns (`is_interactive`, `hold_after`, `no_acceptance`,
//! `review_scoped`) stay typed as an integer, or go through a coercing
//! deserializer — never a plain `bool` — for the same reason.
//!
//! Field types for anything already consumed by the TUI are hand-pinned in
//! `RUST_FIELD_OVERRIDES` (`scripts/codegen.py`) to match this file's
//! pre-existing behaviour exactly; anything else is generated mechanically
//! from the schema and marked `#[allow(dead_code)]` until a consumer reads it.

use super::{TestPlanStep, de_bool_from_int_or_bool, deserialize_test_plan};
"""


def generate_rust() -> str:
    spec = board_openapi_spec()
    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})
    missing = [name for name, _ in RUST_STRUCTS if name not in schemas]
    if missing:
        raise KeyError(
            f"scripts/codegen.py: coord.serve_app.openapi_spec() is missing "
            f"expected /board schema(s) {missing} — did "
            "board_schema.BOARD_PROJECTIONS change?"
        )
    parts = [RUST_HEADER]
    parts.extend(
        emit_rust_struct(schema_name, rust_name, schemas)
        for schema_name, rust_name in RUST_STRUCTS
    )
    return "\n\n".join(parts) + "\n"


def _main_rust(args: list[str]) -> int:
    content = generate_rust()
    if "--check" in args:
        if not RUST_OUTPUT_PATH.exists():
            print(
                f"{RUST_OUTPUT_PATH} does not exist — run `python scripts/codegen.py "
                "--rust` to generate it.",
                file=sys.stderr,
            )
            return 1
        if RUST_OUTPUT_PATH.read_text() != content:
            print(
                f"{RUST_OUTPUT_PATH} is stale — run `python scripts/codegen.py --rust` "
                "to regenerate.",
                file=sys.stderr,
            )
            return 1
        print(f"{RUST_OUTPUT_PATH} is up to date.")
        return 0
    RUST_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUST_OUTPUT_PATH.write_text(content)
    print(f"wrote {RUST_OUTPUT_PATH}")
    return 0


def _parse_out(args: list[str]) -> str | None:
    """``--out PATH`` / ``--out=PATH`` from *args*, or None."""
    for i, arg in enumerate(args):
        if arg.startswith("--out="):
            return arg.split("=", 1)[1]
        if arg == "--out":
            if i + 1 >= len(args):
                raise OutputPathError("--out requires a PATH argument")
            return args[i + 1]
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--rust" in args:
        # #1941: the Rust path has a fixed in-repo destination (no --out/env
        # var story — see RUST_OUTPUT_PATH's docstring), so it skips the
        # TS-only path resolution below entirely.
        return _main_rust(args)
    try:
        output_path = resolve_output_path(_parse_out(args))
    except OutputPathError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    content = generate()
    if "--check" in args:
        # #2009: a MISSING file is now a hard failure, not "stale vs empty".
        # Pre-split, absence meant a fresh checkout that had simply never run
        # the generator; post-split it means `--out`/$COORD_WEB_SRC is
        # pointing somewhere that is not a coord-web checkout, and treating
        # that as ordinary staleness would send an operator off to regenerate
        # a file into the wrong directory.
        if not output_path.exists():
            print(
                f"{output_path} does not exist — is --out/${OUTPUT_ENV_VAR} "
                "pointing at a coord-web checkout?",
                file=sys.stderr,
            )
            return 1
        if output_path.read_text() != content:
            print(
                f"{output_path} is stale — run `python scripts/codegen.py "
                f"--out {output_path}` to regenerate.",
                file=sys.stderr,
            )
            return 1
        print(f"{output_path} is up to date.")
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
