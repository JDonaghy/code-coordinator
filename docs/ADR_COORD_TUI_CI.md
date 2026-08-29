# ADR: coord-tui's CI shape, and retiring the bool guard

**Status:** Accepted
**Date:** 2026-08-28
**Issue:** #2897 (Phase 3 of #2894). Depends on #2895
([Phase 1 of #2894](#phase-1-landed-what-it-changed) — landed).

## Context

`tui/` has not moved out of this repo yet. What #2897 has to decide is the
*shape* the eventual coord-tui checkout's CI will need, so that when the move
does happen it does not also have to invent this from scratch — the same
sequencing #2009/#2006 used for the webapp (design first, in
[`docs/ADR_COORD_WEB_CI.md`](ADR_COORD_WEB_CI.md), move second).

Two Python gates in this repo currently read Rust source text or write to a
fixed, checked-in Rust path, both of which assume one checkout:

1. **The generator.** `scripts/codegen.py --rust` writes
   `tui/src/app/types/generated.rs` — a hard-coded in-repo path, unlike its
   TS half (`--out PATH` / `$COORD_WEB_SRC`, #2009) which already had to
   solve this when the webapp moved to `coord-web`.
2. **The bool guard.** `coord/board_bool_guard.py` regex-scrapes
   `tui/src/app/types.rs` (+ its generated sibling) for plain `bool` struct
   fields and cross-references them against a freshly-migrated SQLite
   schema, closing the #632/#546/#628 blank-board class: a SQLite `INTEGER`
   column deserialized into a strict Rust `bool` fails the *entire*
   `BoardPayload` parse the first time it is `0`. This needs **both** a Rust
   source tree and a live DB schema — the harder half to give a cross-repo
   shape, because after a split those two things live in different repos.

### Phase 1 landed: what it changed

#2895 (Phase 1 of #2894, landed immediately ahead of this issue) deleted
coord-tui's last direct SQL reader: `tui/src/app/data.rs` used to open
`~/.coord/coord.db` read-only and hand-roll the board projection SQL — a raw
column list, typed by rusqlite's own `FromSql`, entirely outside the JSON
wire contract. `rusqlite` is gone from `tui/Cargo.toml`; every board read now
goes through `coord serve`'s HTTP `/board` endpoint. That direct-SQL path
never actually had the #632 boolean bug (rusqlite converts an `INTEGER`
column to a Rust `bool` in-driver, no JSON in between) — but it meant the Rust
binary had two categories of "consumer of column types": the hand-rolled SQL
path, and the generated wire-struct path. Phase 1 leaves exactly one: the
generated wire structs in `tui/src/app/types/generated.rs` (+ any hand-added
mirror in `types.rs`), populated *only* from the `/board` JSON.

That fact is what makes the bool-guard decision below tractable now, and is
why this story is sequenced after Phase 1 rather than before it.

## Decision 1 — the generator: mirror the TS half exactly

`scripts/codegen.py --rust` now takes `--out PATH`, and resolves
`$COORD_TUI_SRC` (a checkout root whose `tui/` holds coord-tui's crate) when
`--out` is absent — the same two-rung resolution `resolve_output_path` already
gave the TS half, implemented as `resolve_rust_output_path`. **No fallback
default**, for the identical reason #2009 gave: silently writing a
hard-coded path that a future checkout no longer contains would either
recreate a dead directory nobody consumes, or, worse, report "up to date"
against a file that does not exist — wrong in exactly the direction that
looks like success.

```bash
# today, while tui/ still lives in this repo:
COORD_TUI_SRC=. python scripts/codegen.py --rust --check
# a real future coord-tui checkout:
COORD_TUI_SRC=~/src/coord-tui python scripts/codegen.py --rust
```

`RUST_OUTPUT_RELPATH` keeps the `tui/` prefix (`<root>/tui/src/app/types/
generated.rs`), unlike the TS `OUTPUT_RELPATH` (no `webapp/` prefix) — because
today `$COORD_TUI_SRC` names *this* checkout, whose crate lives under `tui/`,
and coord-tui's own eventual root layout is the still-open "move" story's
decision, not this one's. If that story flattens the layout, updating one
constant is the entire fix.

### The drift gate splits the same way #2009 split the TS one

- **Stays here:** `tests/test_generated_rust_fixture.py`, narrowed to what
  one checkout can prove — the generator runs, produces well-formed output
  (no accidental rustdoc doctests, see #1941's original incident), and
  covers every schema in the served `/board` spec. This is the exact same
  narrowing `tests/test_generated_types_fixture.py` got in #2009.
- **Moves to coord-tui's CI**, once it exists: the byte-comparison of the
  committed `generated.rs` against `generate_rust()`'s output right now —
  `coord-web`'s equivalent already does this with
  `python scripts/codegen.py --check --out src/api/generated.ts`. Until that
  CI exists, `.github/workflows/test.yml`'s `test` job runs
  `COORD_TUI_SRC=. python scripts/codegen.py --rust --check` directly as an
  interim step (not a pytest test) — deletable in one line the day coord-tui
  CI takes over, with zero pytest file to un-narrow.

### Which `coord` that CI installs, and why (mirrors ADR_COORD_WEB_CI.md)

Once coord-tui's CI exists and runs the byte-comparison above, it needs
`scripts/codegen.py` — i.e. a `coord` install — on its runner, the same
cross-repo-contract shape #2006 solved for `coord-web`. The answer here is
**identical, for the identical reasons**, not a new decision:

- **`pip install 'code-coordinator[server]'`, tracking latest, never an
  exact pin.** `scripts/codegen.py --rust` imports `coord.serve_app`, which
  imports Starlette at module scope — gated behind the `server` extra
  (#1237) — so the bare distribution is not enough; `ModuleNotFoundError`
  the moment `--rust` is used. And per [ADR_COORD_WEB_CI](ADR_COORD_WEB_CI.md)
  part 1, an exact pin answers "does this compile against the coord we froze
  in March", not "does this compile against the coord people actually have"
  — the wrong question, answered green while a real drift ships.
- **`code-coordinator`, never the `claude-coordinator` tombstone** (#2106) —
  same trap, same fix, see that ADR's part 2.
- A `>=` floor documenting the oldest `coord` whose `--rust`/`--out`/
  `$COORD_TUI_SRC` flags the CI job relies on is encouraged, same as there.

### What a red coord-tui CI job means

Copying [ADR_COORD_WEB_CI's section verbatim in spirit](ADR_COORD_WEB_CI.md#what-a-red-coord-web-ci-job-means),
because the failure shape is the same contract, just Rust instead of
TypeScript: when coord-tui's drift-gate job fails on a PR that touched no
Rust wire code, there are exactly two legitimate readings, and one forbidden
response.

1. **`coord.board_schema`'s wire contract changed** (a field added, removed,
   or retyped on one of the seven `/board` projections) **without
   regenerating.** This is a genuine contract break and the red is
   *correct* — regenerate `generated.rs` from the new `code-coordinator`
   release and commit the result.
2. **A bad `code-coordinator` release** changed what `scripts/codegen.py
   --rust` emits for an unrelated reason (a bug in the generator itself).
   Yank/fix the release. Do **not** exact-pin coord-tui's CI to route around
   it — that trades a loud, dated outage for the silent, undated kind.

The forbidden response, same as the web ADR: silencing the job
(`continue-on-error`, deleting the install step, pinning to last-known-green
and walking away). That is exactly how a drift gate rots unnoticed.

## Decision 2 — the bool guard: retire it (option 1 of 3)

Three options were on the table:

1. **Retire it.** If Phase 1 leaves no Rust consumer of column types outside
   the generated wire structs, the text-scraping check has no subject, and
   `tests/test_board_schema.py`'s DTO assertions are the guard that remains.
2. **Move it to coord-tui's CI**, running against its own sources plus a
   schema obtained from the daemon's OpenAPI spec rather than a live DB.
3. **Keep it here** against a vendored copy of the Rust types. Rejected on
   sight: a vendored copy is a mirror, and mirrors are exactly what this
   program has spent its "Phase A" removing everywhere else (`GOAL.md`,
   `docs/ARCHITECTURE.md`).

**Decision: (1), retire it.** Phase 1 (#2895) landed first, confirming the
premise: `rusqlite` is gone from `tui/Cargo.toml`, and `tui/src/app/data.rs`
no longer opens a database at all. The *only* Rust consumer of `/board`
column types left is `tui/src/app/types/generated.rs` (+ any hand mirror in
`types.rs`), and that consumer is now doubly closed off from ever receiving
an unguarded bool, independent of the retired scrape:

- `tests/test_board_schema.py::test_integer_backed_booleans_are_declared_
  integers` asserts every column in `board_schema.INTEGER_BACKED_BOOLEANS`
  serializes as a JSON `integer` in the served spec, never `boolean`.
- `tests/test_board_schema.py::test_no_board_dto_field_is_typed_bool` is the
  **blanket** form: *no* board DTO field is ever typed JSON `boolean`,
  because nothing in SQLite can produce one. Given that, `scripts/codegen.py`'s
  own mechanical mapping (`rust_type_from_schema`) can *never* auto-emit a
  Rust `bool` for a board wire field — a JSON `type: boolean` schema is the
  only input that maps to `bool`, and the DTO layer guarantees no such input
  exists.

That closes the mechanical path completely: a future
`ALTER TABLE ... ADD COLUMN x INTEGER` meant as a flag, added correctly to
`board_schema.py` per its own convention (an `int`-typed dataclass field,
registered in `INTEGER_BACKED_BOOLEANS` if it is genuinely boolean-shaped),
can *never* reach the wire as a JSON boolean and can *never* be
mechanically generated as an unguarded Rust `bool`. The only residual risk —
a hand-authored `RUST_FIELD_OVERRIDES` entry in `scripts/codegen.py` that
mistypes an `INTEGER`-backed field as plain `bool` — is a one-file, human-
reviewed diff in *this* repo, not a drift class a text scrape across two
repos could catch any more cheaply than ordinary review already does.

Option (2) was rejected because it buys nothing options (1) doesn't already
cover: the OpenAPI-derived schema it would compare against is exactly the
same DTO-level information `test_board_schema.py` already asserts, just
re-fetched a second time from a different vantage point, at the cost of a
new schema-from-OpenAPI-spec plumbing path and a second repo's CI job to
keep green. Cheapest, and the honest reading of the seam.

`coord/board_bool_guard.py` is deleted. `tests/test_board_fixture.py` keeps
only its fixture-freshness half (`test_board_sample_fixture_is_up_to_date`,
`test_board_sample_fixture_parses_as_representative_payload`) — unrelated to
the bool guard, never read Rust source text, and stays exactly as before.

## Consequences

- `scripts/codegen.py --rust` requires `--out PATH` or `$COORD_TUI_SRC`; no
  argument and no env var is a refusal (exit 2), not a guess.
- `tests/test_generated_rust_fixture.py` no longer byte-compares a
  checked-in `tui/` path — it proves the generator runs and covers every
  schema, mirroring `tests/test_generated_types_fixture.py`.
- `.github/workflows/test.yml`'s `test` job runs
  `COORD_TUI_SRC=. python scripts/codegen.py --rust --check` as an explicit
  step, standing in for the byte-comparison gate until coord-tui's own CI
  exists to take it over.
- `coord/board_bool_guard.py` is deleted. The #632 blank-board class stays
  closed by `tests/test_board_schema.py`'s DTO-level assertions
  (`INTEGER_BACKED_BOOLEANS` + the blanket no-JSON-boolean check) — one
  guard instead of two, at the layer where the invariant is actually
  enforceable mechanically.
- Standing up coord-tui's own CI workflow (the byte-comparison job, and
  whatever `cargo test`/acceptance jobs it needs) remains open — that is the
  still-separate "move" story's job, same posture #2006/#2009 took for
  `coord-web`.

## Rejected alternatives

See "Decision 2" above for the bool-guard options. For the generator split,
the rejected alternatives are the same ones #2009 already rejected for the
TS half (an exact-pin coord-tui CI install, a git-ref install instead of
PyPI, asserting the pin from inside coord-tui's own CI) — see
[ADR_COORD_WEB_CI](ADR_COORD_WEB_CI.md#rejected-alternatives) for the
reasoning, which applies unchanged.
