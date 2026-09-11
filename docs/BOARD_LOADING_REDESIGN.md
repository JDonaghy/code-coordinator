# Board loading redesign — why `/board` blanks the TUI, and what to do about it

> **Status:** proposal, 2026-09-11. Investigation + design only; nothing here is implemented.
> Read alongside [`COCKPIT.md`](COCKPIT.md) (the project-scoped cockpit thesis, which this
> proposal deliberately does *not* lean on as the fix), [`WEB_CONTROL_CENTER.md`](WEB_CONTROL_CENTER.md)
> and [`STORE_SERVICE.md`](STORE_SERVICE.md) §4 (the expand → migrate → contract rule every
> wire change below follows).

## 0. Verdict in four sentences

1. **The flaw is in the daemon's payload design, not in the TUI's polling.** `GET /board` is
   a 3.5 MB document of which more than 99% is *history*: at the moment of measurement there
   was **one** running assignment (1.8 KB) on a board carrying 322 assignments, 1,114 drive-queue
   rows that are **all** `state=done`, 230 merge-queue rows that are **all** `merged`, and
   1,662 notifications that **no client renders**. Polling a 3.5 MB document every 5 s is a
   terrible idea, but the TUI is polling exactly the document it is offered.
2. **The ETag/304 mechanism (#1336) is defeated by three volatile fields and, even once
   repaired, cannot reduce the daemon's cost** — the daemon has to rebuild the whole board to
   discover that nothing changed, because its cache is a 1.5 s TTL, blind to whether anything
   was written. The rebuild (0.65–0.8 s of CPU, measured by the daemon's own
   `fleet_board_latency` check) is what blocks the event loop and produces the 24 s / 80 s
   stalls.
3. **The TUI has a second, independent defect:** five code paths return `BoardData::default()`
   silently, and the cold-start guard is inert, so an unreachable or slow daemon renders as
   "zero machines" with no error anywhere. That defect must be fixed regardless of what happens
   to the payload.
4. **Per-repo scoping is a legitimate UX direction (COCKPIT.md Pillar 1) but is not a fix for
   this problem:** the operator's main repo is 55% of the partitionable bytes, so a
   `claude-coordinator`-scoped board would still be ~2.1 MB of history, and the views that
   actually blank (Machines, status bar, fleet health) are inherently cross-repo.

The recommendation (§7) is staged: an immediate server-side repair that is **more than just the
ETag fix**, a medium-term additive wire change (`GET /board?sections=…&scope=…` with per-section
ETags) that both clients can adopt at their own pace, and an eventual architecture in which
`/board` is a small summary document and history lives behind paginated resource routes.

---

## 1. The problem, grounded

### 1.1 What is on the wire (live sample, 2026-09-11 11:20, dellserver)

Eight `GET /board` fetches 20 s apart, `~/.coord-venv` client, same headers the TUI sends.
Total body 3,604,907–3,609,179 bytes. Section sizes from sample 0:

| Section | KB | Rows | What is actually in it |
|---|---:|---:|---|
| `assignments` | 818 | 322 | 1 `running`; 170 `done`, 77 `merged`, 69 `failed`, 5 other. 122 rows tied to an open issue. `review_findings` previews are 17% of the bytes. |
| `drive_queue` | 737 | 1,114 | **Every row is `state=done`.** No retention policy exists for this table (`coord/housekeeping.py` archives assignments, notifications and MERGED merge-queue rows only). |
| `issues` | 696 | 786 | 532 open / 254 closed. `body` is still 65% of the section after #1939 — the machine-readable residue plus the exempt epic bodies. |
| `issue_stage_projection` | 336 | 851 | One row per issue with a stage; `issue_title` is duplicated here from `issues`. |
| `notifications` | 228 | 1,662 | **Not read by coord-tui at all** (`types.rs:673-676` documents the drop). The Python client uses only the set of `assignment_id`s (`coord/client.py:181`). |
| `fleet_health` | 180 | 5 machines | 167 KB is `machine_health[*].results` — 78 check results per machine. The status bar needs a severity and a count. |
| `plans` | 161 | 14 | 13 of the 14 belong to assignments that are **no longer on the board** — the `plans` table has no retention (`coord/dao.py::_plans`). |
| `merge_queue` | 160 | 230 | **Every row is `merged`.** |
| `merge_plan` | 122 | 230 | Derived by `merge_queue.plan()` over those 230 merged rows; every entry `status=MERGED`. |
| `board_meta` | 43 | 16 keys | 36 KB is one key, `false_merge_audit_clean`. |
| everything else | ~60 | | `plan_roster` 28, `children` 12, `escalations` 8, `milestone_work_orders` 8, `machines` **1.2** |

The `machines` section — the one whose absence is the user-visible symptom — is 1.2 KB.

### 1.2 What changes between polls

`board_version` advanced 138 → 152 in 151 s: a new version roughly every 11 s. Per-section
diff of consecutive samples:

| Pair | Sections whose bytes changed | Genuine change? |
|---|---|---|
| 0→1 | `audit_recent_count`, `fleet_health`, 1/322 assignment rows, 1/851 stage rows, +1 notification | yes (a leg finished) |
| 1→2 | `audit_recent_count` **only** | **no** |
| 2→3 | `audit_recent_count`, `fleet_health`, 1/323 assignment rows | yes |
| 3→4 | `audit_recent_count`, 1/851 stage rows | yes |
| 4→5 | `audit_recent_count`, 1/323 assignment rows, +1 notification | yes |
| 5→6 | `audit_recent_count`, `fleet_health` | **no** |
| 6→7 | `audit_recent_count`, … | yes |

Every genuine change was **one row** out of hundreds or thousands. Every poll re-shipped all of
them.

The three field families that move without anything having happened:

- **`audit_recent_count`** (`coord/dao.py::_audit_recent_count`) — the number of `audit_log`
  rows in a sliding **900 s** window. It changed on 7 of 7 consecutive pairs. This is the field
  the earlier diagnosis missed; it alone guarantees the digest moves every few seconds in a
  working fleet.
- **`fleet_health`** — `refreshed_at`, every `machine_health[*].received_at/checked_at/latency_ms`,
  and the `fleet_checks[*].headroom` strings, including `fleet_board_latency`'s
  `'774ms / 3.4M (payload 3.4M)'` (the board's own latency, fed back by
  `record_board_stats`, `coord/serve_app.py:6523`) and `issues_sync_staleness`'s `'synced Nm ago'`.
  Moves on the 60 s health tick (`COORD_HEALTH_POLL_INTERVAL`), not on every build as
  previously assumed — but 60 s is still far more often than the board content changes when
  the fleet is idle.
- **`issues[*].synced_at`** — rewritten for every open row by `_sync_issues_tick` on its 300 s
  cadence (`coord/serve_app.py:1028`); did not move inside this 150 s window, which is why
  `issues` shows as stable above.

### 1.3 Why a repaired ETag still leaves the daemon doing the work

The `/board` handler (`coord/serve_app.py:5908-6547`) is: refresh config → if the cached build
is younger than `COORD_BOARD_CACHE_TTL` (1.5 s) serve it, else single-flight a full `_build()`
in a threadpool → `store.board_projection()` (every table) → `merge_queue.plan()` →
`staging_items()` → `find_sibling_overlaps()` → `compute_board_stage_projection()` → parse every
epic body twice (`milestone_work_orders`, `children`) → `aggregate_repo_plans()` →
`bound_board_payload()` → `json.dumps` 3.5 MB → sha256 → compare digest → ETag.

**A 304 is decided *after* the rebuild.** With any poller at 5 s the daemon rebuilds every
1.5 s window that is polled, whether or not anything was written. The rebuild runs in a
threadpool, but `json.dumps` of 3.5 MB and the Python-level derivations hold the GIL for most
of their 0.65–0.8 s, which is exactly the intermittent event-loop stall the `/healthz` spikes
(~10% of samples at 1.1–1.3 s) show. Under contention with a reconcile sweep, a merge running
in the same threadpool, or a second poller, the build queues behind itself and the 24 s / 80 s
fetches appear.

`tests/test_board_read_path.py::test_board_version_bumps_when_content_changes` asserts that two
back-to-back builds share a version. It passes because the test DB has a static `audit_log`
and no health refresher — the production board has never satisfied it.

### 1.4 Who is polling, and with what budget

| Consumer | How it reads | Budget | On failure |
|---|---|---|---|
| **coord-tui** (`src/app/data.rs:2728`) | `GET /board` every 5 s (`settings.rs:20`, default `FiveSec`), **plus** `GET /pause` and, per machine, a TCP probe and an agent `GET /health` on the same tick (`data.rs:1875-1932`) | 8 s connect / 8 s read | `BoardData::default()` on five paths (`data.rs:2755, 2759, 2774, 2776, 2779`), silently |
| **coord web** server (`coord/dashboard/server.py`, port 7434) | `read_board()` → `coord.client.fetch_board_payload` → a **fresh, uncached** `GET /board` per `/api/*` request; **no** `If-None-Match` (`coord/client.py:91-95`). SSE `board_updated` every 30 s (`server.py:623`) invalidates four PWA queries at once: Home = 2 board reads, Machines = 3, MachineDetail ≈ 5 | **5 s** (`coord/client.py:43`) | HTTP 500/503 to the PWA |
| **Python thin clients** (`coord status`, `coord gates`, drive-queue reads on laptops — 76 `read_board()` call sites across 20 modules) | same `fetch_remote_board`, then `board_from_payload` uses only `assignments`, `plans`, `round_number`, `notifications[*].assignment_id` | **5 s** | exception |
| **`GET /machines/stats`** (`serve_app.py:9486`) | calls `build_board()` itself — a second full board read per TUI Machines tick (10 s) | TUI: 3 s / 5 s | Machines detail pane blank |

The TUI is the *smallest* of these consumers by bytes-per-minute once the coord-web dashboard
is open: one Home screen on a phone costs the daemon two full builds per 30 s tick plus one
per window focus, with no conditional GET at all.

---

## 2. What the TUI actually needs, per screen

Source: `/home/john/src/coord-tui/src/app/` (wire type `BoardPayload`, `types.rs:679`, 22
fields, every one `#[serde(default)]`; app type `BoardData`, `types.rs:2348`). 70 of the 163
generated wire fields are `#[allow(dead_code)]` — 43% of the surface has no consumer.

**Default view at startup is `SidebarView::Board`** (`mod.rs:4335`, `types.rs:346`).

| View | Reads from the board | Cross-repo? |
|---|---|---|
| **Board** (default) | `machines[*].{name,host,repos}` (+ client-probed `reachable`/`version`); `assignments[*].{id,repo,machine,issue_number,issue_title,status,type,dispatched_at,finished_at,branch,model,exit_code}`; `open_issues[*].{repo_name,number,title,state,labels,milestone_*}` (+ `body_truncated` → lazy `GET /issue/{r}/{n}`); `merge_queue[*].{issue_number,state}` (merged marker); `board_meta.pipeline_repos`; `children` (epic marker) | grouped by repo, shows all |
| **Machines** | `machines[*]` + `local_machine`. **Workers and job history come from `GET /machines/stats`, deliberately not from `assignments`** (`mod.rs:9176-9185`); sparklines from `GET /machines/metrics` | inherently |
| **Pipeline** | the heavy one: `merge_plan` (19 reads), `assignments` (16), `merge_queue` (12), `open_issues`, `issue_stage_projection` (preferred source of stage status, `pipeline.rs:6606`), `milestone_work_orders`, `children`, `merge_staging`, `board_meta.pipeline_*` | flat, shows all |
| **Queue** | `drive_queue`, `roll_pending`, `assignments` (for the `#Work/#Smoke/#Review` columns), `open_issues` (titles) | inherently (`repo#issue` keys) |
| **Merge Queue** | `merge_plan` (or legacy `merge_queue` + `merge_staging`) | grouped, shows all |
| **Plans** / **Milestones** | `plan_roster`, `plan_roster_supported`, `goal_header`; DAG parses `## Work order` **client-side from epic bodies** (`milestone_dag.rs:495`) | Plans scopes by repo (the only real per-repo scoping in the app) |
| **Kanban** | the Board's issue cache + `children` | shows all |
| **Sessions**, **Terminal** | `machines[*].name/host` only; sessions come from `coord sessions --remote` | inherently |
| **Approved work** | `approved_submissions` | inherently |
| **Audit**, **Reports** | **nothing from `/board`** — `GET /audit`, `GET /report` | — |
| **Fleet-health overlay** | `fleet_health` verbatim (`fleet_health.rs:220`) | inherently |

**Always-on, every frame, every view — the status bar** (`mod.rs:9613`): `load_error`,
`drive_queue` summary, `roll_pending`, `fleet_health` severity, `escalations`, `plan_roster`
attention count, `audit_recent_count`. Any per-view lazy loading has to keep this set warm.

**Not read at all:** `notifications`, `schema_version`, `round_number`, `proposals` (section
forced off, `mod.rs:6648`).

**How much of the 3.5 MB does the default view need?** Roughly a tenth, generously counted:
`machines` (1 KB) + assignments that are active or tied to an open issue with the twelve fields
above (≈80 KB of the 264 KB those 122 rows weigh with all 73 fields) + open issues without
bodies (≈160 KB) + the `merge_queue` `(issue_number, state)` pairs (≈5 KB) + `board_meta`
`pipeline_*` (2 KB) + `children` (12 KB) + a *summarised* status-bar set (< 10 KB if the
daemon sends counts instead of 180 KB of check results). Call it **250–300 KB**, of which most
is the open-issue list. Pipeline needs perhaps another 300 KB. Nothing on any screen needs the
1,114 done queue rows, the 230 merged queue rows, their 230 merged plan entries, the 1,662
notifications, or the 13 orphaned plans — together **1.4 MB, 40% of the wire, with zero
readers**.

**Does the TUI have a "current repo"?** A `Workspace { open_projects, active_project }` model
exists, is persisted to `~/.coord/workspace.json` and reconciled every tick (`workspace.rs`),
and **nothing reads it for rendering** — its own doc comment says so (`mod.rs:2358-2364`).
That is A-1 of epic #1325; A-2 (scope every view) and A-3 (tab strip) are unbuilt. The only
repo-ish state today is `board_active_repo()` (which sidebar section the cursor is in, used for
action targeting) and the Plans tree scope.

### 2.1 coord-web

Source: `/home/john/src/coord-web` (the `coord/dashboard/webapp/` directory in this repo is a
husk containing only `dist/`). The PWA never talks to 7435; every screen is a `/api/*` call to
`server.py`, and `server.py` recomputes from raw `assignments` rather than reading the daemon's
`issue_stage_projection`. Sections the PWA never sees: `issue_stage_projection`,
`notifications`, `merge_plan`, raw `machines`. Default screen `/pipeline` needs `/api/pipeline`
+ `/api/sessions`. Repo scoping: one client-side dropdown on `/queue`; `fetchDriveQueue(repo?)`
supports `?repo=` server-side and is never called with it. No global repo selector.

Test surface: 18 of 19 Playwright specs intercept `/api/*` with `page.route()`; the one
real-server spec (`e2e/live-update-fixture.spec.ts`) boots `coord web --fixture` from the
**published** `coord`, so a `/api/*` shape change cannot be exercised by coord-web's CI until
the coord release that carries it is on PyPI (the `--hold-after` pattern). `fixture.py` lifts
exactly four keys from a board capture (`assignments`, `round_number`, `plans`,
`notifications`, plus optional `fleet_health`) — a new top-level `/board` section is silently
absent from every fixture.

### 2.2 Python thin clients

`board_from_payload` (`coord/client.py:161`) needs `assignments`, `plans`, `round_number` and
the `assignment_id` set from `notifications`. Nothing else. Every `coord status` on a laptop
downloads 3.5 MB to use ~1 MB of it, with a 5 s budget.

---

## 3. Is the ETag salvageable as a first-line fix?

**Yes, and it should be done first — but it is a wire and client-parse relief, not a daemon
relief, and it will not stop the stalls on its own.**

If `_stamp_board_version` hashed a stable projection — the body with `audit_recent_count`,
`fleet_health` and `issues[*].synced_at` excluded (or `fleet_health` hashed without its
timestamps, per-machine latencies and headroom strings, and with the self-referential
`fleet_board_latency` result excluded outright) — then in the measured window:

- **2 of 7** consecutive 20 s pairs would have matched (the fleet had one leg finishing every
  30–60 s — a *busy* window). At the TUI's 5 s cadence that is a genuine change roughly every
  6–12 polls, so **80–90% of polls become 304s while work is flowing, ~100% while idle**.
- Each 304 saves 3.5 MB of transfer and, for coord-web/Python, the parse. For the TUI it saves
  only the transfer: `load_data_remote` re-deserialises the cached 3.5 MB body on every 304
  (`data.rs:2757`).
- It saves the daemon **nothing**: the build still runs every polled 1.5 s window, and the
  `sha256` over 3.5 MB still runs after it. CPU stays where it is (22.7% after the retention
  palliative, 42% before).

The ETag repair also makes the *existing* client machinery work: coord-tui already sends
`If-None-Match` and handles 304 (`data.rs:2742-2761`); coord-web's server does not, and that is
a `coord.client` change (§7, A3).

The repair is invisible to clients (a digest change is just a new ETag lineage, which they
already tolerate on daemon restart), needs no wire change, and can land today. It is
**necessary and not sufficient**.

---

## 4. Options, honestly

| Option | Fixes | Does not fix | Effort (serve / tui / web / py) | Wire change | Verdict |
|---|---|---|---|---|---|
| **A. ETag over a stable projection** | 80–100% of polls become bodyless; cuts client bandwidth and web/Python parse | daemon rebuild cost; TUI parse; stalls | S / – / – / – | none (header semantics) | **Do first.** |
| **B. Write-invalidated cache** (rebuild only when the DB changed) | daemon CPU → ~0 when idle; a 304 becomes a `PRAGMA` and a header; stalls stop being self-inflicted | payload size; the cost of a rebuild when something *did* change | S–M / – / – / – | none | **Do with A.** Needs `PRAGMA data_version` (SQLite) + an equivalent for the Postgres lane. |
| **C. Retention for the sections housekeeping ignores** (`drive_queue`, `plans`, `merge_plan` of merged rows, `notifications` off the default wire) | −1.4 MB immediately; rebuild gets proportionally cheaper | the structural "everything, every poll" shape | S–M / – / – / S | none (fewer rows; the `notifications` drop needs care, §6) | **Do with A.** |
| **D. Non-silent TUI failure + no parse on 304 + probes off the board tick** | the actual user-visible symptom; the 2.35 s blocking window per tick | daemon cost | – / M / – / – | none | **Do regardless** of everything else. |
| **E. `GET /board?sections=…&scope=active\|recent\|all`** with per-section ETags | each view fetches what it renders at its own cadence; the status bar set becomes a ~10 KB poll; history is opt-in | nothing structural is left unfixed once C is in | M / M–L / M / S | **additive**: a query parameter; the bare `/board` stays the full document | **Medium-term fix.** Best skew story of all the options (§6). |
| **F. Summary + paginated resource routes** (`/assignments?state=&repo=&before=&limit=`; `/board` becomes the summary) | the Completed tab, Reports and Audit stop needing history inline; `/board` shrinks to < 100 KB permanently | — | M–L / L / M / M | additive routes, then a contract phase to trim `/board` | **Target architecture** (E is the first step toward it). |
| **G. Per-repo scoping** (`?repo=`) | the operator's *legibility* problem (COCKPIT.md) | **this** problem: `claude-coordinator` is 55% of partitionable bytes, 447 KB is cross-repo, and Machines/status bar/fleet health/Merge Queue/Queue/Sessions are inherently fleet-wide; the TUI has no wired current-repo | S / L (A-2/A-3 of #1325) / M / – | additive (`?repo=` on E's routes) | **Not the fix.** Ship it as a filter on E's routes and as the cockpit UX, later. |
| **H. Delta sync** (`since=board_version`) | bandwidth when the document is large | it is unnecessary once the document is small; needs per-row versions + tombstones (housekeeping *moves* rows) + merge logic in Rust, TS and Python; `board_version` is a hash counter, not a journal | L / L / L / M | new protocol | **Defer, probably forever.** |
| **I. Push (SSE/WebSocket)** | *when* to fetch — replaces the timer | *what* is fetched; the TUI's `ureq` + thread model makes a long-lived stream real work; coord-web already has event-driven invalidation and still fetches the whole board behind it | M / M–L / S / – | new endpoint | **Later refinement** once A+B make a poll nearly free. |
| **J. Raise timeouts** | nothing | treats the symptom: latency is 1–80 s, no finite budget is right; lengthens the blank period; hides overload; the Python 5 s and web 5 s would need the same | S / S / S / S | none | **No.** The only timeout-adjacent change worth making is D (make failure visible). |

### 4.1 Per-repo scoping in more detail — because it was the operator's idea

Measured split of the 3,094 KB that *can* be partitioned by `repo_name`:
`claude-coordinator` 1,698 KB (54.9%), `quadraui` 467 KB, `vimcode` 280 KB, unmapped
notifications 184 KB, `coord-tui` 165 KB, everything else < 100 KB each. The remaining
447 KB (`fleet_health`, `plans`, `board_meta`, `machines`) has no repo.

So `GET /board?repo=claude-coordinator` would be ~2.1 MB — still a document that is >99%
history, still rebuilt every 1.5 s, still over the Python 5 s budget on a bad day. For a small
repo (`coord-web`, 78 KB) it would be transformative, but the operator lives in the large one.

What per-repo scoping *does* buy is legibility, and that is the COCKPIT.md thesis: a repo as
the unit of attention, Board/Pipeline/Kanban scoped to it, the fleet-wide views left alone.
That work (A-2/A-3 of #1325) should proceed on its own merits and will naturally use E's
`?repo=` parameter when it lands. It should not be on the critical path of this incident.

"Significant redesign of the TUI is not off the table" — the redesign that this evidence
supports is of the TUI's **loader** (D + its half of E), not of its screens. The screens already
read narrow field sets; it is the transport that hands them everything.

### 4.2 Per-section endpoints in more detail — why E over a family of new routes

The daemon already has the resource half: `GET /issues?repo_name=`, `GET /drive-queue?repo_name=`,
`GET /assignment/{id}`, `GET /issue/{r}/{n}`, `GET /pause`, `GET /audit`, `GET /report`,
`GET /machines/metrics`, `GET /leg-counts`. What it lacks is a *cheap* way to get the
derived sections (`merge_plan`, `issue_stage_projection`, `plan_roster`, `children`) and the
status-bar set without the whole document — and those derivations are computed *from* the
whole board snapshot inside `_build()`, so they are correct only as a set.

`?sections=` on the existing endpoint keeps that property: build once (cached per B), compute
one sha256 **per section** at build time, and answer any sub-selection from the cached build
with an ETag composed from the selected sections' hashes. A request for
`sections=machines,summary` costs a dictionary lookup and a 10 KB serialisation. Per-section
304s fall out for free. `scope=active|recent|all` applies the row filter (non-terminal, or
terminal within `COORD_BOARD_RETENTION_DAYS`, or everything) to the collection sections, with
`all` being today's behaviour.

Skew in both directions is graceful without a probe-and-fallback dance:

- old client → new daemon: no parameters → the full document, byte-for-byte today's shape;
- new client → old daemon: unknown query parameters are ignored → the full document → the
  client renders (slowly) from the superset it already knows how to parse.

That is strictly better than the #1946 404-memo pattern, which E does not need.

---

## 5. Where the blame sits — is the TUI the flaw?

No. The TUI's polling *cadence* is fine: 5 s against a bodyless 304 is what #1336 designed for.
The TUI's genuine defects are (a) the silent `BoardData::default()` fallback, (b) re-parsing
3.5 MB on a 304, and (c) the per-tick agent probes that block its loader thread for up to
2.35 s. Fixing all three does not touch the wire.

The daemon's defects are structural: (a) the digest covers volatile data, (b) the cache is
time-based rather than write-based, so freshness is bought with a full rebuild every 1.5 s,
(c) three tables and two derived sections have no retention, (d) one 180 KB advisory block
and one 228 KB section nobody renders are on every poll, (e) `GET /machines/stats` builds the
board a second time. All five are the daemon's to fix and none requires a client change.

coord web's dashboard server compounds it: no conditional GET, no per-process memo, and a
5 s budget for a document that is regularly slower than that.

---

## 6. Backwards compatibility and rollout

The fleet runs five hosts with `coord serve` on one, `coord-tui` as a per-host binary with no
remote install path, and `coord web` on the daemon host. Mixed versions are the steady state,
not an accident. Rules every issue in §8 follows:

1. **The daemon leads.** Nothing in a client depends on a daemon change until the release that
   carries it has propagated (`coord release propagate`, `--min-behind 1 --drain`).
2. **`/board` is additive-only until a contract phase.** New sections and new top-level keys are
   safe by construction on every client (Rust `#[serde(default)]` on all 22 fields, TS `?.`,
   Python `.get`). Removing or renaming a key is a contract-phase change that needs the
   deprecated-route telemetry (#1945) to show no old client on the wire.
3. **Query parameters, not new routes, for the section/scope selection** (§4.2). A new client
   on an old daemon gets the superset and works.
4. **Type changes are forbidden on the wire.** A single int→bool mismatch fails the entire
   Rust parse and blanks every panel (#632/#546/#628). The codegen gate in coord-tui's CI
   (`generated.rs`, `board_sample.json`) catches renames but **not** ten of the 22 sections
   (`merge_plan`, `fleet_health`, `issue_stage_projection`, `milestone_work_orders`,
   `children`, `plan_roster`, `goal_header`, `roll_pending`, `approved_submissions`,
   `merge_staging` are absent from the golden fixture). Any issue touching those regenerates
   the fixture *and* adds the section to it.
5. **Capability flags over version sniffing.** The `plan_roster_supported` pattern (#976): a
   daemon that supports section selection says so in `/healthz` (`board_sections: true`), so a
   client can tell "empty because scoped" from "empty because the daemon predates this".
6. **coord-web release ordering.** `server.py` is in this repo, so A3/E-web land here and are
   exercised by this repo's tests; the PWA's `/api/*` contract does not change in stages 0–1.
   If a later stage changes `/api/*` shapes, the coord-web e2e that boots the published
   `coord` needs the `--hold-after` on the coordinator entry.
7. **The ETag digest change is not a wire change.** Clients already tolerate a new lineage on
   daemon restart.

---

## 7. Recommendation, staged

### Stage 0 — immediate (server-side + the TUI's own defect; days, no wire change)

**Is the immediate fix just the ETag repair? No.** The ETag repair is the cheapest and most
visible piece, but on its own it leaves the daemon rebuilding 3.5 MB every 1.5 s and the TUI
blanking silently. Stage 0 is four independent, individually landable changes:

- **A1 ETag over a stable projection** — exclude `audit_recent_count`, `fleet_health` and
  `issues[*].synced_at` from the digest (or hash `fleet_health` minus its clocks, and drop the
  self-referential `fleet_board_latency` *from the hash*). Extend
  `test_board_version_bumps_when_content_changes` with a production-shaped fixture (audit rows
  inside the 900 s window, a health refresh between two builds) so the test fails today and
  passes after.
- **A2 Write-invalidated board cache** — keep the single-flight, replace "younger than 1.5 s"
  with "no write since this build": `PRAGMA data_version` on the read connection (changes when
  any other connection commits, including the drive-queue timer and `coord notify` when they
  write locally), plus the existing POST busts. Keep a TTL as an upper bound (30–60 s) for
  safety. Postgres lane: a `board_revision` sequence bumped by the write choke points, or
  `LISTEN/NOTIFY`; the interface is "give me a cheap change token".
- **A3 coord web / Python client: conditional GET + per-process memo** — `fetch_board_payload`
  keeps `(etag, body)` per service URL and sends `If-None-Match`; `server.py` memoises the
  parsed board for ~2 s so one SSE tick or one screen costs one fetch, not two to five.
- **C1 Retention for the untouched tables** — `coord housekeeping` archives terminal
  `drive_queue` rows and `plans` rows whose assignment is archived, with the same
  move-not-delete pattern as #1107; `merge_queue.plan()` (or the wire) stops carrying MERGED
  rows. Also drop the 36 KB `false_merge_audit_clean` blob from `board_meta` on the wire.
- **D1 coord-tui: never blank silently** — replace the five `BoardData::default()` returns with
  a typed load error; on cold start render "board unreachable: <cause>" in the status bar and
  keep retrying; on warm ticks keep the #620 last-good behaviour but do not let the banner
  expire while the failure persists. Add a `TuiDriver` acceptance test for the cold-start case.
- **D2 coord-tui: stop re-parsing on 304; move agent probes off the board tick** — cache the
  parsed `BoardPayload` beside the ETag; run the TCP/`/health` probes on their own cadence so
  the loader thread never blocks 2.35 s per tick.

Expected effect: daemon board CPU near zero while idle and bounded by *write* rate while busy;
80–100% of polls bodyless; the TUI never shows an unexplained empty board again; the wire
drops by ~1.4 MB. The 24 s / 80 s stalls should disappear because the daemon stops competing
with itself; if they persist, the cause is elsewhere in the threadpool and Stage 0's telemetry
(`fleet_board_latency`) will say so.

### Stage 1 — medium term (additive wire change; weeks)

- **E1 `GET /board?sections=…&scope=…`** with per-section hashes and composed ETags;
  `summary` as a new, small section (machine roster, active counts, queue/merge/fleet-health
  severities, `roll_pending`, `escalations` count, `plan_roster` attention, `audit_recent_count`).
  `/healthz` advertises `board_sections: true`.
- **E2 Fleet health off the hot path** — `machine_health[*].results` moves to
  `GET /fleet-health` on 7435 (7434 already has `/api/machines/health`); the inline block
  keeps `refreshed_at`, per-machine severity and the fleet checks. Two-step: add the route,
  migrate the TUI overlay, then trim (contract phase).
- **E3 coord-tui section-aware loader** — status-bar `summary` + Board set at cadence; Pipeline,
  Queue, Plans, Merge Queue request their sections on view entry and at their own cadence;
  falls back to the full document when `/healthz` lacks `board_sections`.
- **E4 coord web server per-route selection** — each `/api/*` route asks for the sections it
  derives from; `/api/pipeline` reads the daemon's `issue_stage_projection` instead of
  recomputing from raw assignments (this is #632's "two hand-maintained contracts" debt as
  well).
- **E5 `notifications` off the default selection** — the Python `infer_review_state` gets a
  `notified` marker on the assignment row (additive) or asks for `sections=notifications`
  explicitly; the TUI never needed it.
- **E6 `GET /machines/stats` reuses the cached build** instead of `build_board()`.

### Stage 2 — target architecture

- `/board` **is** the summary document (< 100 KB): machines, active + recent assignments with
  bounded fields, open issues without bodies, queue/merge/fleet summaries, capability flags.
- Collections are resource routes with filters and keyset pagination:
  `GET /assignments?state=&repo=&before=&limit=`, `GET /issues?repo_name=` (exists),
  `GET /drive-queue?repo_name=&state=` (exists, add `state`), `GET /merge-plan?repo=`,
  `GET /stage-projection?repo=`. The Completed tab, Reports and Audit already work this way.
- `?repo=` on every collection route is what the cockpit's A-2/A-3 consumes. Per-repo scoping
  arrives as a feature of the routes, not as a variant of the document.
- Push: a daemon `GET /events` stream of `board_version` bumps (the shape coord-web's `/events`
  already has) lets both clients drop the timer; the poll behind the event is then a
  sub-10 KB summary fetch.
- Delta sync is not planned. If the summary document ever grows past a few hundred KB, revisit.

---

## 8. Proposed issue breakdown (drive-queue ready)

Sequenced; "wire" marks issues that change what `/board` carries and therefore need
write-order care with each other (chain `--after`). Everything in Stage 0 is independent of
everything else in Stage 0 except where noted. Repo names are coord names
(`claude-coordinator` for this repo).

| # | Repo | Title | One-line scope | After | Wire |
|---|---|---|---|---|---|
| A1 | claude-coordinator | `/board` ETag digests a stable projection, not the volatile fields | `_stamp_board_version` hashes the body minus `audit_recent_count`, `issues[*].synced_at`, and `fleet_health`'s clocks/headrooms/`fleet_board_latency`; production-shaped regression test in `tests/test_board_read_path.py` | — | no |
| A2 | claude-coordinator | Board cache invalidates on writes, not on a 1.5 s clock | `PRAGMA data_version` (+ Postgres change token) gates the rebuild; POST busts kept; TTL becomes a 30–60 s upper bound; single-flight unchanged; `fleet_board_latency` records build count per minute | A1 | no |
| A3 | claude-coordinator | `coord.client.fetch_board_payload` sends `If-None-Match`; `coord web` memoises the board per process | per-URL `(etag, body)` cache in `coord/client.py`; ~2 s memo in `server.py::_read_board`; `_read_board_and_machine_health` / `_read_fleet_health` share it | — | no |
| C1 | claude-coordinator | Housekeeping archives terminal `drive_queue` rows and orphaned `plans` | move-not-delete into `drive_queue_archive` / `plans_archive` past `COORD_ARCHIVE_RETENTION_DAYS`; `board_projection` excludes them; `GET /drive-queue` gains `?state=` and reads the archive for history | — | rows only |
| C2 | claude-coordinator | Merged rows leave `merge_plan`; `false_merge_audit_clean` leaves `board_meta` on the wire | `merge_queue.plan()` output on `/board` omits `MERGED`; `board_meta` wire drops the 36 KB audit blob (detail via `GET /audit`) | — | **yes** (rows removed; TUI Merge Queue tolerates empty) |
| D1 | coord-tui | Board load failures are never silent | typed load error replaces `BoardData::default()`; cold-start banner; warm-tick last-good banner persists while failing; `TuiDriver` acceptance test | — | no |
| D2 | coord-tui | 304 does not re-parse; agent probes leave the board tick | parsed `BoardPayload` cached beside the ETag; TCP/`/health` probes on their own thread and cadence | — | no |
| E1 | claude-coordinator | `GET /board?sections=&scope=` with per-section ETags and a `summary` section | per-section sha256 at build; composed ETag; `scope=active\|recent\|all`; `summary` section; `/healthz` `board_sections: true`; OpenAPI + `gen_board_fixture` updated | A2 | **yes** (additive) |
| E2 | claude-coordinator | `GET /fleet-health` on the daemon; `/board` carries the fleet-health summary only | new route with the full `machine_health[*].results`; inline block trimmed **after** E3b | E1 | **yes** (two-step) |
| E3a | coord-tui | Section-aware loader: summary + Board at cadence, view sections on entry | uses `?sections=` when `/healthz` advertises it, full `/board` otherwise; per-view cadences; golden fixture gains the ten missing sections | E1 (published) | no (consumer) |
| E3b | coord-tui | Fleet-health overlay fetches `/fleet-health` on open | overlay stops reading `data.fleet_health.machine_health[*].results` | E2 (published) | no (consumer) |
| E4 | claude-coordinator | `coord web` `/api/*` routes request only the sections they derive from | `/api/pipeline` reads `issue_stage_projection`; `/api/machines*` read `summary` + `/fleet-health`; `/api/drive-queue` uses `GET /drive-queue` | E1, E2 | no (`/api/*` shape unchanged) |
| E5 | claude-coordinator | `notifications` off the default `/board` selection | `notified` marker on assignment rows (additive); `board_from_payload` prefers it; `sections=notifications` still served | E1 | **yes** (default selection) |
| E6 | claude-coordinator | `GET /machines/stats` serves from the cached board build | drop the second `build_board()`; same `build_machine_stats` | A2 | no |
| F1 | claude-coordinator | `GET /assignments` collection with `state`/`repo`/`before`/`limit` | keyset-paginated history from live + archive tables; `/board` `scope=recent` becomes the default once E3a/E4 are on it | E1 | additive route |
| F2 | claude-coordinator | Daemon `GET /events` — `board_version` change stream | SSE; coord web's `/events` proxies it instead of its own 30 s timer | A2 | additive route |
| G1 | coord-tui | Workspace A-2: scope Board/Pipeline/Kanban to the active project | consumes `?repo=` on E1/F1; the COCKPIT.md chassis, not part of this incident's fix | E3a | no |

Write-order notes: C2, E1, E2 and E5 all edit `coord/serve_app.py`'s `_build()` and
`coord/board_wire.py`; chain them. A1 and A2 both edit the handler's cache block; chain A2
after A1. D1/D2 and E3a/E3b touch `src/app/data.rs` in coord-tui; chain within that repo.
Nothing in coord-web's own repo is required for Stages 0–1.

Two incidental coord-web bugs found while tracing the data path, out of scope here but worth
filing in `coord-web`: `fetchPortalNeedsInput` expects a bare array where `server.py` returns
`{"submissions": […]}` (the Answers screen throws against a real server; both test layers
encode the wrong shape), and `DriveQueueData` omits the `titles`/`leg_counts` the server sends,
so `/queue` keeps the whole `['pipeline']` query warm just to look up titles.

---

## Appendix A — measurement

- Sampler: `httpx.get(f"{svc.url}/board", headers=_headers(svc) + If-None-Match)` from
  `~/.coord-venv`, eight fetches at 20 s gaps, 2026-09-11 11:20:45–11:23:16 against
  `http://dellserver:7435`. Fetch times 0.43–2.02 s (an uncontended window; the 24.5 s / 80 s
  samples in the incident notes were taken under contention and are not contradicted here).
  `If-None-Match` with the just-issued ETag returned 200 + full body on all seven attempts.
- Per-section diff keyed on `assignment_id` / `(repo_name, number)` / `id` /
  `(repo_name, issue_number)`; "stable projection" = body minus `board_version`,
  `audit_recent_count`, `fleet_health`, `issues[*].synced_at`.
- Section-size and field-weight figures are from sample 0; row-state counts from the same.
- Consumer maps were traced from `/home/john/src/coord-tui/src/app/{data,types,mod,render,
  settings_ui,pipeline,drive_queue,fleet_health,workspace}.rs`, `/home/john/src/coord-web/src`,
  `coord/dashboard/server.py`, `coord/client.py`, `coord/board_service.py`.

## Appendix B — open questions

1. Does the daemon host's `~/.coord/client.toml` point `coord web` and the drive-queue timer at
   `http://127.0.0.1:7435`, or do they open SQLite directly? Either way A2's `PRAGMA
   data_version` covers it; the answer decides whether A3's memo is the bigger or smaller of
   the two web-side wins.
2. `merge_queue.plan()` carrying 230 MERGED entries — is any consumer relying on them (the TUI
   Merge Queue panel renders `status`)? C2 assumes not; verify against `pipeline.rs:3987`
   before dropping.
3. `_AUDIT_RECENT_WINDOW_SECONDS = 900` makes `audit_recent_count` a poor status-bar signal as
   well as a poor hash input (#1039 was to replace its semantics). E1's `summary` is the place
   to give it per-client read-cursor semantics if that is still wanted.

---

## Epilogue — what shipped, and what this document got wrong (2026-09-11)

Written the same day as the body above, after Stage 0 shipped and was measured.
**Read this before acting on anything in §4-§8.**

### The central measurements in this document were taken over a broken link

Every timing in §1 was measured from `elitebook`, which was routing all LAN traffic
over WiFi at **10% packet loss and 978 ms average RTT** while an idle gigabit ethernet
sat unused on the same subnet (0% loss, 0.78 ms). Cause: both NICs on `192.168.1.0/24`
with `arp_ignore=0`, so the WiFi interface answered the ARP probe for the ethernet's own
address; NetworkManager read its own MAC as an address conflict and withdrew the wired
route. Fixed with `net.ipv4.conf.all.arp_ignore=1` / `arp_announce=2`.

Re-measured over the healthy link, on the **unmodified pre-Stage-0 code**:

| | over WiFi (as reported in §1) | over ethernet, same build |
|---|---|---|
| `/board` | 47.9 s; then 1.0 s / 24.5 s / 80 s | **0.67 s cold, 0.044 s warm** |
| `If-None-Match` | 200 + full body, 7/7 | **304** |

So the headline problem — "the board overruns the TUI's 8 s timeout" — was **mostly the
network**. The TUI's blank Machines panel was a slow link, not an oversized payload.

### What was nonetheless real, and did get fixed

The code-level findings were read from source, not inferred from timings, and they stand:

- The ETag digest included volatile fields (`audit_recent_count`, a 900 s sliding count,
  chiefly). The 304s above happen on an **idle** board; under write load the digest moved
  every build. **A1** (#3293) hashes a stable projection. Measured after: **15/15 polls
  returned 304 with work in flight**, against 0/7 before.
- The board cache was a 1.5 s TTL, so a 5 s poller rebuilt every time and the 304 was
  decided *after* the rebuild. **A2** (#3294) made it write-invalidated. Cold `/board`
  ttfb went **0.63 s -> 0.079 s**, and the `/healthz` spikes (~10% of samples at
  1.1-1.3 s) stopped appearing.
- `coord web` rebuilt the whole board per `/api/*` request. **A3** (#3295) memoises it.
- `drive_queue` / `plans` had no retention. **C1** (#3296) archives them.

Shipped in v0.5.451-v0.5.455; fleet rolled 2026-09-11.

### Stage 1 is DROPPED, not deferred

§7's Stage 1 (twelve issues: `?sections=`, per-section ETags, `/fleet-health`, a
section-aware TUI loader) was scoped against a board believed to take 24-80 s. It takes
**0.08 s** and caches correctly. Splitting the `/board` wire contract across two
consumers — with a version-skew story, coord-web's e2e booting a published `coord web`,
and coord-tui shipping as an unmanaged per-host binary — is now cost with no benefit.
Do not file those issues. Stage 2 likewise.

### Still open

- **`coord serve` sits at ~22.7% CPU** before and after A2, on a freshly restarted
  process. A2 was expected to move it and did not. Unexplained; not board rebuilds.
- The 15/15 result was measured at `2 running` drives. The worst case in the body (an
  assignment row written every ~2 minutes by a failing retry loop) was not reproduced.

### The lesson worth keeping

A 1300 ms ping was observed early in the investigation and dismissed as a transient blip
after a later sample read 4 ms. It was neither transient nor a blip — it was the whole
problem, sampled during a good window. **Measure the transport before attributing latency
to the payload**, and treat a wildly bimodal sample as evidence of an unstable path rather
than noise to average away. What survived here is what was read from source; what did not
is what was inferred from wall-clock numbers over an unexamined link.
