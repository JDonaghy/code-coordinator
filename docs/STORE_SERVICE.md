# Store Service — a stable API contract over a pluggable backend

**Milestone:** `Store Service` (#60) · **Tracking epic:** #1949 · **Status:** Phases A and C
complete; B and D each down to their last issue · *last reconciled against the tree
2026-08-31*

> **Where this doc and #1949 disagree, #1949 wins.** The epic is rescoped more often than
> this narrative is; it also carries the machine-readable `## Work order`, and
> `coord milestone order claude-coordinator 1949` is authoritative for what is ready and
> what is blocked.

**Phase A** (#1849, #1939, #1941, #1942) and **Phase C** (#1948, via slices #2719–#2768 plus
#2782/#2784) are closed. Since 2026-08-28 the whole of Phase D's confidence apparatus and its
migration tool have landed too — #827, #2884, #2886, #828 and #2885 are all closed, as are
Phase B's #1944, #1945 and #1946.

**Two issues remain in the milestone**: **#1947** (retire the RPC surface on telemetry
evidence) and **#829** (the Postgres cutover). They are independent of each other: Phase B
reshapes the wire, Phase D swaps the engine.

**But #829's own acceptance bar is not close.** The Postgres CI lane #2886 built has never
once been green — see Phase D below. That is the live constraint on this milestone, and it is
tracked outside it, in #2982 and #2983.

This is the plan of record for turning `coord serve` into what it is already trying to
be: **one storage service with a contract that does not change when the storage engine
does.** Read it before starting any issue in this milestone.

The organising constraint is that **none of this may disturb the running fleet.** Every
phase is additive; the old surface keeps working until telemetry proves nothing uses it.

---

## 1. The reframe: the service already exists

It is worth being precise about what is missing, because it is smaller and more specific
than "build a DB microservice."

`coord serve` **is** the storage microservice today. Thin clients carry no local DB;
`coord.board_service.resolve()` routes reads and writes to it; `coordinator.remote.yml`
is a cache of what it serves; the TUI, the webapp and the Python CLI all read through it.
That architecture is built and in production.

What is missing is **contract discipline**. Three specific defects, measured against the
tree on 2026-08-07:

| defect | measurement |
|---|---|
| The wire schema *is* the SQLite DDL | `serve_app.py:1482` builds `GET /board`'s OpenAPI schema by `PRAGMA`-introspecting an in-memory SQLite DB (`openapi.py:178`) |
| The API is RPC-shaped, not resource-shaped | 55 routes; ~50 verb-per-endpoint, **3** resource-shaped |
| The store seam holds 6% of the SQL | **226** `execute` calls across 23 files — `state.py` **128 (57%)**, `dao.py` **13 (6%)** |

Sizes for scale: `serve_app.py` 6,581 lines, `state.py` 5,201, `dao.py` 483,
`models.py` 825 (9 dataclasses, used internally — never as the wire schema).

**Two of those three defects are now closed.** Re-measured 2026-08-28:

| defect | then (2026-08-07) | now |
|---|---|---|
| wire schema *is* the DDL | `PRAGMA`-introspected | **fixed** — explicit DTOs in `coord/board_schema.py` (#1849) |
| RPC-shaped, not resource-shaped | 55 routes, 3 resource-shaped | **unchanged** — Phase B is open |
| SQL is SQLite-dialect | 226 `execute` calls, 6% behind a seam | **fixed differently** — 313 sites through `coord/sql.py`, 0 raw driver calls, ratcheted |

The third line is the one that changed shape rather than size, and §2 explains why.

---

## 2. Why "swap the DAO" is not the job

`CoordStore` / `coord/dao.py` is the **read** waist for the board projection. It is not
where writes live: #590 landed in `coord/state.py` + `coord/board_service.py`, and
`dao.py`'s three write methods are dead stubs that raise `NotImplementedError` (#1823).

So implementing a Postgres adapter "behind the storage-agnostic DAO" yields a Postgres
**read** adapter while 128 write paths still speak SQLite dialect.

**But routing those writes through `CoordStore` was not the fix either.** Two incompatible
positions were live in the tree and both were adjudicated wrong on 2026-08-24:

- `dao.py`'s docstring (#1823) claimed *"DB-API 2.0 already abstracts `sqlite3` vs
  `psycopg` at the connection layer."* It does not. PEP 249 makes `paramstyle` a **module
  attribute callers must interrogate**, not something it standardizes — `sqlite3` is
  `qmark` (`?`), psycopg is `pyformat` (`%s`). Corrected by #2708.
- The original Phase C claimed portability required a `CoordStore` **write** interface.
  Store methods *relocate* dialect-specific SQL without translating it: finishing that
  work would have left all ~220 placeholders and all 37 `INSERT OR REPLACE` sites intact,
  in a different module, after a semantic refactor of the highest-traffic file in the repo.

The remedy was a **dialect seam** — `coord/sql.py` — and it shipped (#1948). A `CoordStore`
write interface may still be worth building, but for **testability**, not portability; that
argument is #2885's, not this program's.

*"#827 is gated on milestone 19"* — asserted by the original version of this section — is
**cleared**. Milestone 19 is 17/18 done and its only open child is unrelated.

---

## 3. What is already built (assets, not work)

This program is mostly *applying* machinery that exists:

- **`openapi.dataclass_schema()`** (`coord/openapi.py:120`) — the explicit-DTO path
  `POST /assign` already uses. `/board`'s seven tables simply never adopted it.
- **`scripts/codegen.py`** (#750) — generates the webapp's TS types from the schema, with
  `--check` enforced in CI (`webapp-types` job, `tests/test_generated_types_fixture.py`).
  Declared DTOs get client regeneration nearly free.
- **`_DROP_COLUMNS` / `_JSON_COLUMNS`** (`coord/dao.py`) — column-to-wire *policy* already
  sits on the storage-neutral side of the seam. Only the introspection *mechanism* is
  SQLite-bound.
- **Golden `/board` fixture + round-trip parse test** (#748) — a contract test harness
  already exists to extend.
- **`SCHEMA_VERSION`** (`coord/dao.py:35`, currently `1`) — served on `/healthz` and in
  the board payload. A version *signal* exists; a version *negotiation* does not.

---

## 4. Strategy: expand → migrate → contract

Every phase adds alongside what exists. Nothing is removed until usage telemetry says
zero. This is not a stylistic preference — it is forced by two facts about this fleet:

**Agents run pinned releases.** A merged change is not a live change (see
[`docs/OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md)). Server and client update on
different lanes, days apart.

**Endpoint and caller must never change in one commit.** Deploy server-first, always;
the alternative is the 405 trap where a new client meets an old daemon.

### Why a server-side feature flag is the wrong mechanism

A boolean on the daemon (`use_dto_schema: true`) flips the contract for **every client at
once** — the Rust TUI, the React webapp, the Python CLI, and every pinned agent — which is
precisely the disturbance this program is supposed to avoid.

The right mechanism is **client-driven negotiation**: a request header
(`X-Coord-Schema: 2`, or `Accept: application/vnd.coord.v2+json`) whose **absence means
today's shape**. Old clients are then unaffected *by construction* rather than by
discipline, each client migrates on its own deploy lane, and rollback is a client-side
one-liner rather than a daemon restart.

A server-side flag is still appropriate for one thing: **choosing the storage backend**
(Phase D), because that is genuinely a deployment property and not a per-client contract.

### Retirement is evidence-driven

The contract phase (removing the old surface) starts only when deprecation telemetry
shows zero calls from any client version over a defined window. "We think everyone
upgraded" is how the 405 trap happens.

---

## 5. The phases

| phase | issues | what | status |
|---|---|---|---|
| **A — Contract** | #1849, #1942, #1939, #1941 | Declare DTOs; sever the wire from the DDL; gate the clients; build the store contract suite | ✅ **closed** |
| **B — REST** | #1943 → #1944 → #1945 → #1946 → #1947 | Negotiate; add resource routes; measure; migrate per client; retire on evidence | #1943–#1946 closed; **#1947 open** |
| **C — Dialect seam** | #1948 (slices #2719–#2768, #2782, #2784) | Translate the SQL — paramstyle, upserts, DDL, driver exceptions — **not** a `CoordStore` write interface | ✅ **closed** |
| **D — Second backend** | #827 → #2884/#2886 → #828/#2885 → #829 | Postgres proves the seam is real, and the instruments prove the proof | #827, #2884, #2886, #828, #2885 closed; **#829 open and blocked in practice** |

### Phase A — Contract

**Closed.** The cheapest phase and the one that paid off first, because before it a
column rename in `coord/db.py` was a silent breaking wire change to three clients.

- **#1849** — `/board`'s projections are explicit dataclasses in `coord/board_schema.py`,
  not `PRAGMA table_info`. This also took the bool-as-int hazard off Phase D's plate: no
  API consumer sees a column type, in either backend.
- **#1939** — exercised the boundary for real: `issues.body` (2.22 MB, 35× the titles) is
  no longer shipped in the collection projection.
- **#1941** — coord-tui's Rust wire types are generated from the declared schema and
  CI-gated, as the webapp's TS types already were.
- **#1942** — `tests/test_store_contract.py`, the backend-parametrised `CoordStore`
  contract suite. Adding a backend is appending one `Backend` entry, no new test code.
  **Scoped to the read surface**; the write half is #2885.

### Phase B — REST

`PATCH /issue/{repo}/{n}` replacing ten verb endpoints, `PATCH /assignment/{id}` replacing
four field-setters. Mechanical, wide, and low-risk *if* sequenced as expand/migrate/contract.

Order matters: negotiation first, then routes, then telemetry, then per-client migration,
then retirement. Telemetry before migration, so retirement has evidence rather than hope.

**#1943–#1946 are closed.** `X-Coord-Schema` (absent meaning today's shape) is the mechanism
every slice depends on; the resource routes exist alongside the RPC surface, deprecation
telemetry is recording, and the Python CLI is migrated (client 1 of 4). **#1947 — retirement
— is the only one left**, and by design it does not start until the telemetry shows zero calls
from any client version over a defined window. It is **independent of Phase D**: nothing in
the REST reshaping blocks or is blocked by the backend swap.

### Phase C — Dialect seam

**Closed 2026-08-26.** Rescoped on 2026-08-24 from *"get `state.py`'s 128 SQL calls behind
`CoordStore`"* to *"translate the SQL"* — see §2 for the adjudication.

`coord/sql.py` is the seam. It **detects the dialect from the connection object**, never a
config flag, and owns paramstyle translation, upserts, the row factory, `AUTOINCREMENT`/WAL
DDL, `lastrowid`→`RETURNING`, and driver-exception mapping. It migrated per file, not per
domain: `coord/sql.py` (new) → `commands/_common.py` → `issue_store.py` → `housekeeping.py`
→ `portal_store.py` → `db.py` → `state.py` → `dao.py` → the 15 remaining modules.

Measured on the tree 2026-08-28:

| construct | state |
|---|---|
| `sql.*` call sites | **313** |
| raw driver `execute()` outside the seam | **0** |
| raw `?` placeholders | **0** |
| `INSERT OR REPLACE` / `OR IGNORE` | **0** (13 remaining hits are migration comments) |
| `AUTOINCREMENT`, `PRAGMA`/WAL, `lastrowid` | **0** outside the seam |
| `except sqlite3.*` | **0** outside the seam (#2784) |

#2768's ratchet keeps it there. It is an AST walk with four legs — raw `execute`, raw `?`,
driver-named exceptions, SQLite-only statement text — so the seam cannot silently reopen.

Two numbers that looked like work and were not: the 84 `ON CONFLICT … excluded.` sites were
already Postgres-compatible (SQLite adopted the syntax in 3.24), and all 24 `strftime` calls
are Python's `time.strftime`, not SQL's.

### Phase D — Second backend

**Its code has landed; its proof has not.** Its value is as **proof**: a second backend
is the only way to know the seam is real rather than nominal. Postgres is the chosen
prover; **SQLite remains supported**, which is also what makes the cutover's rollback a
config flip rather than a restore.

After Phase C, the port itself is small. There are exactly **two** connection factories in
the tree — `coord/db.py:109` and `coord/dao.py:294` — and both already delegate everything
downstream to the seam. What is left is getting a non-SQLite connection into them, plus the
things a connection swap does not cover: `DB_PATH` is a module-level `Path` and a Postgres
deployment has a DSN, and `db.py` hands out a singleton with `check_same_thread=False`,
which is a SQLite affordance that psycopg3 does not share.

**The confidence apparatus is the other half, and it was unfiled until 2026-08-28.** It was
then built in two days — all three issues below are closed, as is #828's migration tool
(`coord migrate-to-postgres`, `coord/store_migrate.py`). The code is portable and the
machinery exists. What it *reports* is the problem: see "the lane is red" below.

- **#2884 (closed)** — the suite cannot be pointed at a second backend. The autouse `coord_db`
  fixture (`tests/conftest.py:797`) is a single chokepoint covering all 393 test files,
  which makes this much cheaper than the raw `sqlite3.connect` count suggests; 38 files
  open their own connections and need triage. **On the critical path — build it before
  #828.** A migration tool with no way to verify its output is a tool you have to trust.
- **#2885 (closed)** — the write path has no parity oracle. #1942's contract suite is scoped to
  reads and reaches 12 of the 313 SQL sites. The 37 `INSERT OR REPLACE` rewrites recorded a
  judgement per site, not a test.
- **#2886 (closed)** — `coord/sql.py`'s Postgres branches have never executed. `psycopg` is
  not a declared dependency, no CI workflow mentions Postgres, and the Postgres-aware tests
  use mock connections.

Order: **#827 → #2884 / #2886 → #828 / #2885 → #829.** Everything before #829 has landed.

#### The lane is red — this is what actually gates #829

`coord/sql.py`'s Postgres branches now execute in CI against a real Postgres 16 service
(#2886, a separate `postgres` job in `.github/workflows/test.yml`). **It has never been
green.** On `main` at `a706a8d9` (run `33342821643`):

```
12 skipped, 13884 errors in 2414.26s (0:40:14)      <- 13,896 collected, 0 passed
```

A 100% error rate, from the first test, invisible because the job carries
`continue-on-error: true`.

The cause is a single missing line, not a deep portability problem.
`coord/db.py`'s `_migrate_add_columns` swallows the driver error from a duplicate
`ALTER TABLE ... ADD COLUMN` without calling `conn.rollback()`. On SQLite that is harmless; on
Postgres the first swallowed error aborts the transaction, so every statement after it — the
remaining 73 ALTERs and `_set_schema_version`'s `DELETE FROM schema_version` — fails with
`InFailedSqlTransaction`. Since `_ensure_schema()` is what the autouse `coord_db` fixture
calls, the fixture dies before any test body runs.

Tracked in **#2982** (the rollback + a regression test; reports the residual failure list
rather than fixing it) and **#2983** (the other 20 `except sql.driver_errors():` handlers that
continue on the same connection — `coord/` has 21 such sites and exactly one `conn.rollback()`
tree-wide). Until #2982 lands, #829's *"full test suite green with the Postgres backend
selected"* stands at zero, and #2885's write-parity oracle — built precisely to make this
cutover safe — is itself among the errors.

A whole-day outage is an acceptable cost for this cutover, which is what keeps #828 a
one-shot importer: no dual-write, no online migration, no cutover choreography.

---

## 6. What this program does not claim

It does not claim the fleet needs Postgres. SQLite on the daemon host is serving a
three-machine fleet adequately, and #1825's framing — *"Postgres locally, Azure only if it
pays"* — is the right posture. Phase D is a proof of the seam and an option on
multi-writer, not a performance fix for a problem we have measured.

It does not claim the current API is bad engineering. RPC endpoints accreted because each
one solved a real dispatch problem quickly; the codebase has been honest about it (`_DROP_COLUMNS`
is described in its own comments as a patch over a known leak). The cost only becomes
material once there are three client languages and a second backend — which is now.

It did not promise that Phase C was safe to rush, and Phase C ended up being a different
job than that warning anticipated — translation rather than relocation, which is why it
decomposed into ten mechanical slices instead of a semantic refactor of `state.py`.

It does not claim Phase D is finished when the code compiles against psycopg. **A seam is
nominal until something has run on both sides of it.** As of 2026-08-31 this repo *does* now
connect to a real Postgres server in CI — #2884/#2885/#2886 delivered that, and sequencing
them before the migration tool was right. But connecting is not passing: the lane is at a 100%
error rate (see Phase D), so the seam is still nominal in the only sense that matters. The
apparatus was worth building precisely because it is what turned "we believe the seam is real"
into a measurable, currently-failing number.

---

## 7. References

- **#1849** — sever `/board`'s wire schema from the SQLite DDL (Phase A, closed)
- **#1939** — `/board` shipped 2.22 MB of issue bodies; the DTO boundary's first real decision (closed)
- **#1942** — the backend-parametrised `CoordStore` contract suite; read surface only (closed)
- **#1948** — the dialect seam, and the adjudication in §2 (closed; slices #2719–#2768)
- **#2708** — corrects `dao.py`'s "DB-API 2.0 abstracts the dialect" claim (closed)
- **#2768 / #2784** — the ratchet that keeps the seam closed (closed)
- **#827 / #828** — connection factory + config seam; `coord migrate-to-postgres` (both closed)
- **#829** — the cutover (open; gated in practice on the red Postgres lane)
- **#2884 / #2885 / #2886** — test-harness portability, write-path parity oracle,
  psycopg + Postgres CI (all closed)
- **#2982 / #2983** — the missing `conn.rollback()` that keeps the Postgres lane at 0 passes,
  and the tree-wide sweep for the same defect
- **#1825** — state durability & the relocatable daemon
- **#282** — multi-user / team mode, the eventual consumer of a real store seam
- **#750 / #748 / #757** — codegen, golden fixture, OpenAPI spec (the assets §3 lists)
- [`docs/PLATFORM_EVOLUTION.md`](PLATFORM_EVOLUTION.md) — the cloud/portal direction
- [`docs/OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) — deploy lanes; why server-first is mandatory
