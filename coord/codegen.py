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
#2006) to get this generator. What still runs here is
`tests/test_generated_types_fixture.py`, narrowed to what a single checkout
can actually prove: that the generator produces complete, well-formed output
covering every schema in the served spec.

#3045 — THIS MODULE LIVES IN THE INSTALLED PACKAGE, NOT JUST THIS CHECKOUT.
It used to be `scripts/codegen.py`, which `[tool.setuptools.packages.find]`
never shipped (`include = ["coord*"]` — `scripts/` is not a package), so
"installs `code-coordinator[server]` from PyPI ... to get this script" above
was aspirational, not true: a consumer repo's CI had no way to actually reach
it. It now lives at `coord/codegen.py`, a real module of the `coord` package,
so `pip install 'code-coordinator[server]'` is genuinely enough. Run it with:

    python -m coord.codegen ...
    coord codegen ...

`scripts/codegen.py` still exists, as a thin shim re-exporting this module,
so existing local invocations and docs that predate the move keep working
from a checkout of *this* repo — but it is never what a consumer repo's CI
should install for, since it is not in the wheel.

Usage:
    # regenerate into a coord-web checkout
    python -m coord.codegen --out ~/src/coord-web/src/api/generated.ts
    COORD_WEB_SRC=~/src/coord-web python -m coord.codegen
    # exit 1 (no write) if that file is stale
    COORD_WEB_SRC=~/src/coord-web python -m coord.codegen --check

#1941 (extended by #2897) — THIS SCRIPT ALSO GENERATES coord-tui's RUST WIRE
TYPES, and as of #2897 the Rust half is cross-repo capable the SAME SHAPE as
the TS half above. `tui/` still lives in *this* repo today, but the eventual
split (tracked separately — this story produces the shape, not the move)
means the destination can't stay a fixed, checked-in path either: it is named
explicitly by `--out PATH` or by `$COORD_TUI_SRC` pointing at a checkout root
whose `tui/` holds coord-tui's crate, with the same no-fallback-default
reasoning as `resolve_output_path` above — a guess is always wrong
post-split, and under `--check` it is wrong in the direction that reports
success. Today `$COORD_TUI_SRC=.` (this checkout's own root) reproduces the
old fixed-path behaviour exactly (`<root>/tui/src/app/types/generated.rs`,
i.e. `tui/src/app/types/generated.rs`).

The source of truth is a *different* OpenAPI document than the TS path reads
— `coord.serve_app.openapi_spec()` (the daemon app, port 7435), not
`coord.dashboard.server.openapi_spec()` (the dashboard app, port 7434) —
because `GET /board`, the endpoint the TUI actually polls, is specified
there, from the seven explicit wire DTOs in `coord/board_schema.py` (#1849).
See `generate_rust()` below for the mechanical schema-walk + hand-curated-
override split, which mirrors `ENUM_OVERRIDES`/`_ENUM_BLOCK` above but at the
level of individual struct fields rather than whole enum types (Rust's richer
type system — visibility, `#[serde(rename/default/deserialize_with)]`,
custom deserializers for the INTEGER-backed-boolean guard — needs a
finer-grained override point than TS did).

The drift GATE splits the same way #2009 split the TS one (docs/
ADR_COORD_TUI_CI.md, #2897): what stays here is
`tests/test_generated_rust_fixture.py`, narrowed to what one checkout can
prove — the generator runs, produces well-formed output, and covers every
schema in the served `/board` spec. The byte-for-byte comparison against a
committed `generated.rs` belongs to coord-tui's own CI once it exists,
installing `code-coordinator[server]` from PyPI to get this generator (now
genuinely reachable as `coord/codegen.py`, #3045) — the same shape
`docs/ADR_COORD_WEB_CI.md` (#2006) already gave the TS half, just not stood
up yet (standing it up is the still-open "move" story's job, not this one's).

The producer-side INTEGER-backed-boolean guard that used to cross-reference
the Rust source text against a live SQLite schema (`coord/board_bool_guard.py`)
is RETIRED as of #2897 — see `docs/ADR_COORD_TUI_CI.md` for the reasoning.
Short version: #2895 (Phase 1 of #2894) deleted coord-tui's last direct SQL
reader, so the structs this file generates are now the *only* Rust consumer
of column types, and they are already pinned int-vs-bool by the DTO-level
assertion in `tests/test_board_schema.py` (`board_schema.INTEGER_BACKED_BOOLEANS`).
The text-scraping half had no remaining subject once that was true.

#2900 — `--rust` NOW GENERATES BOTH HALVES OF THE WIRE CONTRACT. Everything
above describes the READ path (`generated.rs`, the `/board` response DTOs).
The WRITE path — the request/response types for every daemon route coord-tui
POSTs or PATCHes — is generated into a sibling `generated_requests.rs` by the
SAME `--rust` invocation. See the "#2900 — THE WRITE HALF" block comment
further down for the design (which endpoints, why an explicit list, and the
absent-vs-null and `X-Coord-Schema` semantics a JSON Schema cannot express).

One flag rather than two, deliberately: a separate `--rust-requests` would be
one more thing coord-tui's `codegen-drift.yml` could quietly stop invoking,
which is the exact silent failure `coord.health.checks.coord_tui_ci_pin` had
to be written to detect from this side of the repo boundary. `--out` still
names the read file; the write file is written beside it (override with
`--requests-out`).

    # from a coord-tui checkout root — writes/checks BOTH files
    COORD_TUI_SRC=. python -m coord.codegen --rust
    # exit 1 (no write) if EITHER file is stale; reports both, not just the first
    COORD_TUI_SRC=. python -m coord.codegen --rust --check
    # or name the files directly
    python -m coord.codegen --rust \\
        --out src/app/types/generated.rs \\
        --requests-out src/app/types/generated_requests.rs
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

#: Path of the emitted file RELATIVE to a `coord-web` checkout's root.
OUTPUT_RELPATH = Path("src") / "api" / "generated.ts"

#: Env var naming a `coord-web` checkout root, used when `--out` is absent.
OUTPUT_ENV_VAR = "COORD_WEB_SRC"

#: Path of the emitted file RELATIVE to a `coord-tui` checkout's root —
#: i.e. `<coord-tui>/src/app/types/generated.rs`.
#:
#: #2897 introduced this with a `tui/` prefix, because `$COORD_TUI_SRC` then
#: named *this* checkout and the crate lived under its `tui/`. #2899 moved
#: the crate to the standalone `coord-tui` repo, where it sits at the repo
#: ROOT — so the prefix is gone and this is now exactly the shape of the TS
#: analogue (`OUTPUT_RELPATH`, relative to a `coord-web` checkout root).
RUST_OUTPUT_RELPATH = Path("src") / "app" / "types" / "generated.rs"

#: Env var naming a `coord-tui` checkout root, used when `--out` is absent
#: (#2897). Since #2899 that is a real, separate checkout (`~/src/coord-tui`),
#: not this one — `$COORD_TUI_SRC=.` is only correct when run FROM a coord-tui
#: checkout, which is exactly what coord-tui's own CI drift gate does.
RUST_OUTPUT_ENV_VAR = "COORD_TUI_SRC"


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


def resolve_rust_output_path(explicit: str | Path | None = None) -> Path:
    """Where to write/check ``generated.rs``: ``--out`` > ``$COORD_TUI_SRC``.

    Mirrors ``resolve_output_path`` above, for the Rust half (#2897). Raises
    :class:`OutputPathError` when neither is set, rather than guessing: since
    #2899 the crate is not in this repo at all, so any fallback would either
    recreate a dead directory nobody consumes or — worse, under ``--check`` —
    report "up to date" against a file that is not the one actually named.
    That is the same failure shape #2009 called out for the TS half.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    root = os.environ.get(RUST_OUTPUT_ENV_VAR)
    if root:
        return Path(root).expanduser() / RUST_OUTPUT_RELPATH
    raise OutputPathError(
        "no destination for generated.rs. Since #2899 the TUI lives in the "
        "coord-tui repo, so pass --out PATH or set "
        f"${RUST_OUTPUT_ENV_VAR} to a coord-tui checkout root (the file is "
        f"written to its {RUST_OUTPUT_RELPATH}). See this script's module "
        "docstring."
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
    "GateAPacket",
    "GateAApprovalWire",
    "GateAMockWire",
    "MilestoneListResponse",
    "MilestoneSummaryWire",
    "MilestoneDetail",
    "MilestoneEntryWire",
    "MilestoneGateColumnsWire",
    "MilestoneGateAWire",
    "JournalResponse",
    "JournalEntryWire",
    "JournalLinkWire",
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
    # coord/gate_a.py GateADecision.state — the five STATE_* constants
    # (coord/dashboard/server.py GateAPacket.state mirrors decision.state
    # verbatim, #3069). A JSON Schema `{"type": "string"}` can't express this
    # either.
    ("GateAPacket", "state"): (
        "'approved' | 'missing' | 'stale' | 'changes' | 'exempt'"
    ),
    # coord/gate_a.py GateAApproval.verdict — the two VERDICTS a human can
    # record (`coord gate-a --approved|--changes`).
    ("GateAApprovalWire", "verdict"): "'approved' | 'changes'",
    # #3072 — the milestone roster (coord/dashboard/server.py). Same two
    # hand-curated enums as the Gate-A packet above, plus GitHub's own
    # milestone/issue state vocabulary and the four board gate columns,
    # which are the SAME value sets `Assignment` declares above — kept in
    # sync by pointing at the same shared type names where one exists
    # (`AssignmentStatus`, `TestVerdict`) rather than re-spelling them.
    ("MilestoneSummaryWire", "state"): "'open' | 'closed'",
    ("MilestoneDetail", "state"): "'open' | 'closed'",
    # An entry whose issue could not be resolved reports null, not a guess —
    # see MilestoneEntryWire's docstring.
    ("MilestoneEntryWire", "state"): "'open' | 'closed' | null",
    ("MilestoneGateAWire", "state"): (
        "'approved' | 'missing' | 'stale' | 'changes' | 'exempt'"
    ),
    ("MilestoneGateAWire", "verdict"): "'approved' | 'changes' | null",
    ("MilestoneGateColumnsWire", "status"): "AssignmentStatus | null",
    ("MilestoneGateColumnsWire", "test_state"): "TestVerdict | null",
    ("MilestoneGateColumnsWire", "smoke_test"): "'pass' | 'fail' | null",
    ("MilestoneGateColumnsWire", "review_state"): (
        "'pending' | 'dispatched' | 'done' | null"
    ),
    ("MilestoneGateColumnsWire", "review_verdict"): (
        "'approve' | 'request-changes' | null"
    ),
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
 * Generated by `coord.codegen` (`pip install 'code-coordinator[server]'`)
 * from the dashboard's OpenAPI 3 spec (`coord.dashboard.server.openapi_spec()`,
 * itself built by `coord/openapi.py` from `coord/models.py` /
 * `coord/pipeline.py`) — #1550 (originally #750). Regenerate after any field
 * change:
 *
 *     python -m coord.codegen
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
                f"coord/codegen.py: no TS mapping for JSON Schema type {json_type!r} "
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
        f"coord/codegen.py: no Rust mapping for JSON Schema type {json_type!r} "
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
//! Generated by `coord.codegen --rust` (`pip install 'code-coordinator[server]'`)
//! from `coord.serve_app.openapi_spec()`'s `/board` response schema — itself
//! built from the explicit wire DTOs in `coord/board_schema.py` (#1849) —
//! #1941. Regenerate after any field change:
//!
//! ```text
//! COORD_TUI_SRC=<coord-tui checkout root> python -m coord.codegen --rust
//! ```
//!
//! (The fence is load-bearing, and `text` is the load-bearing part of *it*:
//! a 4-space-indented block in a doc comment is a Markdown code block, and
//! rustdoc compiles an unannotated code block as a Rust doctest — so writing
//! that command as an indented block, or in a bare ``` fence, breaks
//! `cargo test --doc` with `error: expected item, found `.``.)
//!
//! #2897: the destination is named explicitly (`--out PATH` / `$COORD_TUI_SRC`
//! — see this script's module docstring), the same shape #2009 gave the TS
//! half. `tests/test_generated_rust_fixture.py` proves the generator runs and
//! covers every schema; the byte-for-byte freshness check against *this file
//! as committed* is coord-tui's own CI job once it exists
//! (`docs/ADR_COORD_TUI_CI.md`) rather than a test in this repo.
//!
//! **Every field defaults.** `/board` is one JSON payload: a single type
//! mismatch on a single field fails the *entire* `BoardPayload` parse and
//! blanks every TUI panel — the #632/#546/#628 failure class this repo has
//! been bitten by three times. INTEGER-backed boolean columns
//! (`is_interactive`, `hold_after`, `no_acceptance`, `review_scoped`) stay
//! typed as an integer, or go through a coercing deserializer — never a
//! plain `bool` — for the same reason (`coord/board_schema.py`'s
//! `INTEGER_BACKED_BOOLEANS`, asserted int-typed by `tests/test_board_schema.py`).
//!
//! Field types for anything already consumed by the TUI are hand-pinned in
//! `RUST_FIELD_OVERRIDES` (`coord/codegen.py`) to match this file's
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
            f"coord/codegen.py: coord.serve_app.openapi_spec() is missing "
            f"expected /board schema(s) {missing} — did "
            "board_schema.BOARD_PROJECTIONS change?"
        )
    parts = [RUST_HEADER]
    parts.extend(
        emit_rust_struct(schema_name, rust_name, schemas)
        for schema_name, rust_name in RUST_STRUCTS
    )
    return "\n\n".join(parts) + "\n"


# ═════════════════════════════════════════════════════════════════════════════
# #2900 — THE WRITE HALF
# ═════════════════════════════════════════════════════════════════════════════
#
# Everything above generates coord-tui's READ path: the `/board` response DTOs.
# The write path was, until this story, ungenerated — coord-tui hand-built
# `serde_json::json!({...})` literals against the daemon's verb-shaped routes
# (`post_daemon_json(&url, token, "/test-verdict", &json!({...}))`,
# settings_ui.rs). Inside one repo that was tolerable: a single PR changed
# both sides and one CI run saw it. Across the #2899 repo boundary each one
# became an ungenerated contract, free to drift silently — and a drifted write
# body does not blank a panel the way a drifted read does, it silently writes
# the wrong thing (or 400s) at the moment a human is trying to record a
# verdict.
#
# WHAT IS GENERATED. Only the endpoints coord-tui actually calls, listed
# explicitly in `RUST_WRITE_ENDPOINTS` below — not all ~50 daemon routes. A
# generated client for routes nobody calls is dead code that still has to be
# kept green. The list is short on purpose and grows when coord-tui grows a
# call, which is a deliberate, reviewable edit.
#
# The SHAPES, though, are not hand-written: each entry names a `(path,
# method)` and the fields come from that operation's `requestBody` /
# `200` response schema in the SAME served spec (`coord.serve_app.
# openapi_spec()`) the read half reads. That is the whole point — rename a
# field in `coord/board_schema.py` or `coord/rest_schema.py`, or retype one
# in `openapi_spec()`, and this output changes, and coord-tui's
# `--rust --check` gate goes red.
#
# ABSENT-vs-NULL is the one semantic that cannot be read off a JSON Schema,
# so it is declared per endpoint as `partial`:
#
#   partial=False (every verb/RPC route) — an optional field serializes as an
#     explicit `null`, byte-identical to the `json!` literal it replaces
#     (`record_test_verdict_remote` sends `"test_reason": null` today, and a
#     migration that silently started omitting the key would be a behaviour
#     change smuggled in under "no functional change").
#
#   partial=True (the PATCH resource routes, #1944) — absent means "leave
#     alone" and `null` means "clear", which are DIFFERENT operations
#     (`coord/rest_schema.py`: "Absent is not null"). Those fields are emitted
#     as `Option<Option<T>>` + `skip_serializing_if`, the only Rust shape that
#     can express all three states. A plain `Option<T>` would make
#     "clear the milestone" unreachable from a generated client.
#
# X-COORD-SCHEMA (#1943) is derived, not declared: a resource-shaped path is
# one with `{...}` path parameters, and those — and only those — send the
# header. Verb routes deliberately do not: absence means "today's shape", so
# an un-migrated verb call keeps working unchanged, which is exactly the
# property that lets coord-tui migrate endpoint-by-endpoint on its own deploy
# lane (docs/STORE_SERVICE.md §4).

#: Path of the emitted write-client file RELATIVE to a `coord-tui` checkout
#: root, i.e. `<coord-tui>/src/app/types/generated_requests.rs`. A SIBLING of
#: `RUST_OUTPUT_RELPATH` rather than more content appended to it: the read
#: file is `Deserialize`-only wire *rows*, this one is `Serialize` request
#: bodies plus their route metadata, and they are regenerated by the same
#: command but consumed by different call sites.
RUST_REQUESTS_OUTPUT_RELPATH = Path("src") / "app" / "types" / "generated_requests.rs"


@dataclasses.dataclass(frozen=True)
class WriteEndpoint:
    """One daemon route coord-tui writes to, and the Rust names for its DTOs.

    ``base`` is the Rust type-name stem: ``TestVerdict`` yields
    ``TestVerdictRequest`` and (when the operation declares a 200 body)
    ``TestVerdictResponse``.
    """

    path: str
    method: str  # "post" / "patch" — lower-case, as OpenAPI keys them
    base: str
    #: See the ABSENT-vs-NULL note above.
    partial: bool = False
    #: Rendered as `///` doc comments on the request struct, after the
    #: mechanically-derived summary line.
    doc: tuple[str, ...] = ()

    @property
    def is_resource_shaped(self) -> bool:
        """True for `/issue/{repo_name}/{number}`, false for `/issue-label`.

        Derived from the path rather than declared, so a route that gains a
        resource shape cannot be left behind by a stale hand-maintained flag.
        """
        return "{" in self.path


#: The daemon routes coord-tui writes to. Kept in step with its
#: `post_daemon_json` call sites; see the block comment above for why this is
#: an explicit list and not "every POST in the spec".
RUST_WRITE_ENDPOINTS: tuple[WriteEndpoint, ...] = (
    WriteEndpoint(
        path="/test-verdict",
        method="post",
        base="TestVerdict",
        doc=(
            "Replaces `record_test_verdict_remote`'s hand-built `json!` body",
            "(#200/#590, settings_ui.rs). The `smoke_test` / `smoke_test_reason`",
            "mirror is NOT derived here — deriving it is a client-side policy",
            "decision (`coord test --pass/--fail` does the same derivation on the",
            "Python side so `coord fix`'s `smoke_test == \"fail\"` guard sees it),",
            "and this file only describes the wire.",
        ),
    ),
    WriteEndpoint(
        path="/issue-label",
        method="post",
        base="IssueLabel",
        doc=(
            "Replaces `apply_issue_labels_remote`'s hand-built `json!` body (#1012).",
        ),
    ),
    WriteEndpoint(
        path="/issue/{repo_name}/{number}",
        method="patch",
        base="IssuePatch",
        partial=True,
        doc=(
            "#1944's resource-shaped successor to `/issue-label` (and six other",
            "RPC routes). Send `X-Coord-Schema` with this one — see",
            "`SCHEMA_HEADER` below.",
        ),
    ),
    WriteEndpoint(
        path="/issue-upsert",
        method="post",
        base="IssueUpsert",
        doc=(
            "Replaces `upsert_issue_remote`'s hand-built `json!` body (#2895).",
        ),
    ),
    WriteEndpoint(
        path="/purge",
        method="post",
        base="Purge",
        doc=(
            "Replaces `purge_request`'s hand-built `json!` body (#2895) — and its",
            "`resp.get(key).and_then(as_u64).unwrap_or(0)` response digging, which",
            "is what `PurgeResponse` is for.",
        ),
    ),
)


class WriteEndpointError(Exception):
    """A `RUST_WRITE_ENDPOINTS` entry names something the spec does not have."""


def _resolve_ref(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    """Follow a top-level ``$ref`` into ``components/schemas``.

    Only the outermost level — nested ``$ref``s stay as references and are
    emitted as the referenced Rust type name by `rust_type_from_schema`,
    which is what we want (a nested object becomes its own named struct).
    """
    ref = schema.get("$ref")
    if not ref:
        return schema
    name = ref.rsplit("/", 1)[-1]
    if name not in schemas:
        raise WriteEndpointError(
            f"coord/codegen.py: $ref {ref} is not in components/schemas — "
            "did coord/rest_schema.py change?"
        )
    return schemas[name]


def _operation(ep: WriteEndpoint, spec: dict[str, Any]) -> dict[str, Any]:
    op = (spec.get("paths", {}).get(ep.path) or {}).get(ep.method)
    if not isinstance(op, dict):
        raise WriteEndpointError(
            f"coord/codegen.py: coord.serve_app.openapi_spec() declares no "
            f"{ep.method.upper()} {ep.path} — RUST_WRITE_ENDPOINTS names a route "
            "that no longer exists. Remove the entry, or restore the route."
        )
    return op


def _json_schema(container: dict[str, Any] | None) -> dict[str, Any] | None:
    """The ``application/json`` schema out of a requestBody / response."""
    if not isinstance(container, dict):
        return None
    content = container.get("content")
    if not isinstance(content, dict):
        return None
    entry = content.get("application/json")
    if not isinstance(entry, dict):
        return None
    schema = entry.get("schema")
    return schema if isinstance(schema, dict) else None


def _request_schema(ep: WriteEndpoint, spec: dict[str, Any], schemas: dict[str, Any]):
    schema = _json_schema(_operation(ep, spec).get("requestBody"))
    if schema is None:
        raise WriteEndpointError(
            f"coord/codegen.py: {ep.method.upper()} {ep.path} declares no "
            "application/json requestBody — nothing to generate a request "
            "struct from."
        )
    return _resolve_ref(schema, schemas)


def _response_schema(ep: WriteEndpoint, spec: dict[str, Any], schemas: dict[str, Any]):
    """The 200 body schema, or None when the operation declares none.

    None is legitimate — a route whose success body is uninteresting to the
    client (`{"ok": true}` after a fire-and-forget write) needs no response
    struct, and inventing an empty one would be noise. It is emitted only
    when the spec actually describes something.
    """
    responses = _operation(ep, spec).get("responses") or {}
    schema = _json_schema(responses.get("200"))
    return None if schema is None else _resolve_ref(schema, schemas)


def _path_params(ep: WriteEndpoint, spec: dict[str, Any]) -> list[dict[str, Any]]:
    op = _operation(ep, spec)
    return [
        p
        for p in (op.get("parameters") or [])
        if isinstance(p, dict) and p.get("in") == "path"
    ]


def _rust_param_type(schema: dict[str, Any]) -> str:
    """Rust argument type for a path parameter — borrowed, not owned.

    A path builder should not force its caller to allocate a `String` just to
    interpolate it.
    """
    return "&str" if schema.get("type") == "string" else "u64"


def _request_field(
    python_name: str, schema: dict[str, Any], *, required: bool, partial: bool
) -> RustField:
    """One field of a generated request struct — see the ABSENT-vs-NULL note."""
    base = rust_type_from_schema(schema)
    nullable = bool(schema.get("nullable"))
    if required and not nullable:
        return RustField(name=python_name, ty=base, serde=(), doc=())
    if not partial:
        # Verb route: absent and null are the same thing to the handler
        # (`body.get(...)`), so a plain Option — serialized as an explicit
        # null — reproduces today's hand-built body byte for byte.
        return RustField(name=python_name, ty=f"Option<{base}>", serde=(), doc=())
    # Partial (PATCH) route: three states, so a nested Option. `None` is
    # skipped entirely (leave alone); `Some(None)` serializes as an explicit
    # null (clear); `Some(Some(v))` sets.
    ty = f"Option<Option<{base}>>" if nullable else f"Option<{base}>"
    return RustField(
        name=python_name,
        ty=ty,
        serde=("default", 'skip_serializing_if = "Option::is_none"'),
        doc=(),
    )


def _response_field(python_name: str, schema: dict[str, Any], *, required: bool) -> RustField:
    """One field of a generated response struct.

    Every field is `#[serde(default)]`, for the same reason every `/board`
    field is (`RUST_HEADER`): one missing or retyped key must not fail the
    whole parse and turn a successful write into a displayed error.
    """
    base = rust_type_from_schema(schema)
    ty = base if (required and not schema.get("nullable")) else f"Option<{base}>"
    return RustField(name=python_name, ty=ty, serde=("default",), doc=())


def _emit_write_struct(
    name: str,
    schema: dict[str, Any],
    *,
    request: bool,
    partial: bool,
    doc: tuple[str, ...],
) -> str:
    required = set(schema.get("required") or ())
    derives = (
        ("Clone", "Debug", "Default", "serde::Serialize")
        if request
        else ("Clone", "Debug", "Default", "serde::Deserialize")
    )
    lines = [f"/// {d}".rstrip() if d else "///" for d in doc]
    lines.append("#[derive(" + ", ".join(derives) + ")]")
    lines.append("#[allow(dead_code)]")
    lines.append(f"pub(crate) struct {name} {{")
    for field_name, field_schema in (schema.get("properties") or {}).items():
        field = (
            _request_field(
                field_name, field_schema, required=field_name in required, partial=partial
            )
            if request
            else _response_field(field_name, field_schema, required=field_name in required)
        )
        lines.extend(_render_rust_field(field))
    lines.append("}")
    return "\n".join(lines)


def _emit_route_impl(
    ep: WriteEndpoint, params: list[dict[str, Any]], schema_version: int
) -> str:
    """The `impl <Base>Request` block: route metadata + a path builder."""
    name = f"{ep.base}Request"
    lines = ["#[allow(dead_code)]", f"impl {name} {{"]
    lines.append("    /// The route template, exactly as the daemon declares it.")
    lines.append(f'    pub(crate) const PATH: &\'static str = "{ep.path}";')
    lines.append("    /// HTTP method for this route.")
    lines.append(f'    pub(crate) const METHOD: &\'static str = "{ep.method.upper()}";')
    if ep.is_resource_shaped:
        lines.append(
            "    /// #1943: a resource-shaped route, so send "
            "`X-Coord-Schema: <this>`."
        )
        lines.append(
            f"    pub(crate) const SCHEMA_HEADER: Option<u32> = Some({schema_version});"
        )
    else:
        lines.append(
            "    /// #1943: a verb route — send NO `X-Coord-Schema` header. Its"
        )
        lines.append(
            "    /// absence means \"today's shape\", which is what keeps an"
        )
        lines.append(
            "    /// un-migrated call working unchanged (docs/STORE_SERVICE.md §4)."
        )
        lines.append("    pub(crate) const SCHEMA_HEADER: Option<u32> = None;")
    if params:
        args = ", ".join(
            f"{p['name']}: {_rust_param_type(p.get('schema') or {})}" for p in params
        )
        # `format!` with inline captures: every path parameter is in scope as
        # a local of the same name, so the template is the literal route.
        lines.append(
            "    /// The concrete request path, with parameters interpolated."
        )
        lines.append(f"    pub(crate) fn path({args}) -> String {{")
        lines.append(f'        format!("{ep.path}")')
        lines.append("    }")
    else:
        lines.append("    /// The concrete request path (no parameters).")
        lines.append("    pub(crate) fn path() -> &'static str {")
        lines.append("        Self::PATH")
        lines.append("    }")
    lines.append("}")
    return "\n".join(lines)


RUST_REQUESTS_HEADER = """\
//! AUTO-GENERATED — DO NOT EDIT BY HAND.
//!
//! coord-tui's daemon **write** client (#2900, Phase 4 of code-coordinator#2894),
//! generated by `coord.codegen --rust` alongside its read-path sibling
//! `generated.rs`. Both come from `coord.serve_app.openapi_spec()`; this file
//! covers the `requestBody` / `200` response of every route coord-tui POSTs
//! or PATCHes. Regenerate after any wire change:
//!
//! ```text
//! COORD_TUI_SRC=<coord-tui checkout root> python -m coord.codegen --rust
//! ```
//!
//! (`text` is load-bearing: rustdoc compiles an unannotated code block in a
//! doc comment as a doctest.)
//!
//! **Why this file exists.** Before #2900 these bodies were hand-built
//! `serde_json::json!({...})` literals. Inside one repo that was a tolerable
//! mirror — one PR changed both sides and one CI run saw it. Since #2899 put
//! coord-tui in its own repo it is a cross-repo contract per endpoint, each
//! free to drift in silence; and unlike a drifted READ (which blanks a panel
//! loudly) a drifted WRITE silently records the wrong thing.
//!
//! **`X-Coord-Schema` (#1943).** Each request type carries a `SCHEMA_HEADER`
//! const: `Some(v)` on the resource-shaped routes (`/issue/{repo}/{n}`),
//! `None` on the verb routes, whose handlers read an absent header as
//! "today's shape". Send the header when, and only when, it is `Some` — that
//! asymmetry is what lets coord-tui migrate one endpoint at a time on its own
//! deploy lane, with a client-side one-liner as the rollback
//! (docs/STORE_SERVICE.md §4).
//!
//! **Absent is not null on a PATCH.** Fields of a partial-update route are
//! `Option<Option<T>>`: `None` omits the key (leave alone), `Some(None)`
//! sends an explicit `null` (clear), `Some(Some(v))` sets. Fields of a verb
//! route are a plain `Option<T>` and serialize their `None` as an explicit
//! `null`, byte-identical to the literal each one replaces.
//!
//! Every response field is `#[serde(default)]` for the same reason every
//! `/board` field is: one unexpected key must not turn a successful write
//! into a displayed parse error.\
"""


def _wrap_doc(text: str, width: int = 88) -> tuple[str, ...]:
    """Wrap one long line into ``///``-able doc lines.

    The route summaries come out of ``openapi_spec()`` as single sentences of
    arbitrary length (``/issue-label``'s deprecation pointer is ~230 chars).
    Emitting those verbatim gives the generated file lines four times the
    width of everything around it — legal Rust, unreadable diff. Wrapped here
    rather than at the source, because the summary is the *spec's* text and
    shortening it there would cost the human reading `/openapi.json`.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return tuple(lines)


def generate_rust_requests() -> str:
    """coord-tui's generated write client — see `RUST_REQUESTS_HEADER`."""
    from coord.dao import SCHEMA_VERSION  # noqa: PLC0415

    spec = board_openapi_spec()
    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})
    parts = [RUST_REQUESTS_HEADER]

    # Nested request DTOs the endpoints below `$ref` (e.g. `/issue-upsert`'s
    # `issue`). Emitted first so the file reads top-down, and discovered from
    # the referenced schemas rather than listed by hand.
    nested: dict[str, dict[str, Any]] = {}
    for ep in RUST_WRITE_ENDPOINTS:
        req = _request_schema(ep, spec, schemas)
        for field_schema in (req.get("properties") or {}).values():
            ref = field_schema.get("$ref")
            if ref:
                nested[ref.rsplit("/", 1)[-1]] = _resolve_ref(field_schema, schemas)
    for name in sorted(nested):
        parts.append(
            _emit_write_struct(
                name,
                nested[name],
                request=True,
                partial=False,
                doc=(f"A nested request DTO — `coord.rest_schema.{name}` (#2900).",),
            )
        )

    for ep in RUST_WRITE_ENDPOINTS:
        req = _request_schema(ep, spec, schemas)
        summary = _operation(ep, spec).get("summary") or ""
        head = _wrap_doc(f"`{ep.method.upper()} {ep.path}` — {summary}".rstrip(" —"))
        parts.append(
            _emit_write_struct(
                f"{ep.base}Request", req, request=True, partial=ep.partial, doc=head + ep.doc
            )
        )
        parts.append(_emit_route_impl(ep, _path_params(ep, spec), SCHEMA_VERSION))
        resp = _response_schema(ep, spec, schemas)
        if resp is not None:
            parts.append(
                _emit_write_struct(
                    f"{ep.base}Response",
                    resp,
                    request=False,
                    partial=False,
                    doc=(f"`200` body of `{ep.method.upper()} {ep.path}`.",),
                )
            )
    return "\n\n".join(parts) + "\n"


def resolve_rust_requests_output_path(
    explicit: str | Path | None = None, *, board_out: Path | None = None
) -> Path:
    """Where to write/check ``generated_requests.rs``.

    ``--requests-out`` > a sibling of an explicit ``--out`` > ``$COORD_TUI_SRC``.

    The middle rule is what keeps `--rust` a single command: `--out` names the
    read file, and the write file is its sibling in the same directory, which
    is where `$COORD_TUI_SRC` would have put it anyway. Same no-fallback-
    default reasoning as :func:`resolve_rust_output_path` — a guessed path
    under ``--check`` fails in the direction that reports success.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    if board_out is not None:
        return board_out.parent / RUST_REQUESTS_OUTPUT_RELPATH.name
    root = os.environ.get(RUST_OUTPUT_ENV_VAR)
    if root:
        return Path(root).expanduser() / RUST_REQUESTS_OUTPUT_RELPATH
    raise OutputPathError(
        "no destination for generated_requests.rs. Pass --requests-out PATH, "
        f"or --out PATH (it is written alongside), or set ${RUST_OUTPUT_ENV_VAR} "
        f"to a coord-tui checkout root (the file is written to its "
        f"{RUST_REQUESTS_OUTPUT_RELPATH}). See this script's module docstring."
    )


def _check_or_write(output_path: Path, content: str, *, check: bool, label: str) -> int:
    """Shared `--check`/write tail — see :func:`_main_rust`."""
    if check:
        # #2897: a MISSING file is a hard failure, not "stale vs empty".
        # Absence usually means --out/$COORD_TUI_SRC is pointing somewhere
        # that is not a coord-tui checkout, and treating that as ordinary
        # staleness would send an operator off to regenerate into the wrong
        # directory.
        if not output_path.exists():
            print(
                f"{output_path} does not exist — is --out/${RUST_OUTPUT_ENV_VAR} "
                "pointing at a coord-tui checkout?",
                file=sys.stderr,
            )
            return 1
        if output_path.read_text() != content:
            print(
                f"{output_path} is stale — run `python -m coord.codegen --rust` "
                f"to regenerate ({label}).",
                file=sys.stderr,
            )
            return 1
        print(f"{output_path} is up to date.")
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)
    print(f"wrote {output_path}")
    return 0


def _parse_requests_out(args: list[str]) -> str | None:
    """``--requests-out PATH`` / ``--requests-out=PATH`` from *args*, or None."""
    for i, arg in enumerate(args):
        if arg.startswith("--requests-out="):
            return arg.split("=", 1)[1]
        if arg == "--requests-out":
            if i + 1 >= len(args):
                raise OutputPathError("--requests-out requires a PATH argument")
            return args[i + 1]
    return None


def _main_rust(args: list[str]) -> int:
    """`--rust` writes/checks BOTH generated Rust files (#2900).

    One flag, one CI step, both halves of the wire contract — a separate flag
    for the write half would be one more thing coord-tui's `codegen-drift.yml`
    could quietly stop invoking, which is precisely the failure
    `coord.health.checks.coord_tui_ci_pin` had to be written to detect.
    """
    try:
        board_out = resolve_rust_output_path(_parse_out(args))
        requests_out = resolve_rust_requests_output_path(
            _parse_requests_out(args),
            board_out=board_out if _parse_out(args) is not None else None,
        )
    except OutputPathError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    check = "--check" in args
    rc = _check_or_write(board_out, generate_rust(), check=check, label="board DTOs")
    rc_req = _check_or_write(
        requests_out, generate_rust_requests(), check=check, label="write DTOs"
    )
    # Report BOTH before returning: a run that stops at the first stale file
    # sends the reader back for a second round trip to discover the other.
    return rc or rc_req


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
        # #2897: the Rust path is now cross-repo capable the same shape as
        # the TS path below (--out / $COORD_TUI_SRC, no fallback default) —
        # see resolve_rust_output_path's docstring.
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
                f"{output_path} is stale — run `python -m coord.codegen "
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
