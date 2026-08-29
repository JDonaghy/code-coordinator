# Gate A Contract — ms-67: TUI portal bridge — project↔repo mapping + briefed decomposition

_Mock-authored 2026-08-21 for milestone 67 (tracking issue #2530)._
_Issues in scope: **#2531** (PB-1, config: project↔repo mapping — no UI surface, §2),
**#2532** (PB-2, TUI panel: "Approved work items", §3), **#2533** (PB-3, TUI action:
pull into a briefed decomposition session, §4)._
_Driver: `tui-tuidriver`. All screen mocks: **120×40 terminal**, symbols-only `screen()`
grid (`TestBackend`) — see §0 for the pinned shell geometry this contract inherits, and
§1 for what is and is not machine-verified in this render._
_Dependency order (per #2530's own `## Work order`): #2531 and #2532 are independent;
#2533 depends on both. This contract covers all three because #2533's mock cannot be
drawn without pinning what #2531/#2532 produce first._

---

## 0. Grid geometry (inherited, unchanged — pinned by ms-33/ms-38/ms-65)

Same shell layout as every prior TUI contract: column 0 = active-panel accent (`▎` or
space), column 1 = one-glyph activity-bar icon, column 2 = `│` separator, columns 3–37 =
sidebar content (35 cols), columns 38–119 = main panel content (82 cols, **no separate
divider column** — whatever the active panel paints at column 38 is the boundary). Row
39 (last) is the global status bar; rows 0–38 are panel content. `⚙` Settings stays
bottom-pinned at row 38. East-Asian *Ambiguous* counts as 1 column (ms-38 §7); every
glyph this contract newly introduces (`✓ ⇢ ⚠` used as a panel/action icon, plus the
box-drawing already established by ms-38's context-menu mocks) was checked against that
rule and is Ambiguous or Neutral, i.e. 1 column.

## 1. Known limitation this contract designs around: no machine-rendered ground truth

None of #2531/#2532/#2533 exist in `tui/src/` yet, so — unlike a contract amending an
already-shipped panel — there is no `TuiDriver`/`TestBackend` output to verify column
boundaries against. Every `.screen` mock below is hand-authored against the **pinned**
geometry in §0 (real, existing today) plus this contract's own **proposed** column
widths for the new content (not derived from any existing constant — flagged per-item in
§6). **What is pinned as `Testable` in §§3–5 is text presence/order via
`screen_contains`, never an exact column x-offset** — the test-author should assert
substrings, not fixed positions, exactly the posture ms-65 §4 already established for
facts a symbols-only grid can't fully guarantee.

A second, related limit: §4's dispatch reuses the `new-issue-chat`/`milestone-chat`
**stream-json `claude -p` + `ChatController` overlay** family (`coord/commands/chat.py`,
`tui/src/app/dialogs.rs::dispatch_board_chat_new_issue` /
`maybe_bind_pending_milestone_chat`) — **not** the `InteractiveLaunchMode` PTY/tmux
family `chat-about-issue`/Fix/Troubleshoot use (`tui/src/app/sessions.rs::
launch_interactive_session_for_selected_issue`, which spawns a real
`quadraui::terminal_engine::TerminalSession`). This distinction matters because only the
PTY family is excluded from `TestBackend` reach per `docs/ORACLE_LOOP.md`'s own
dogfooding note ("raw-terminal/PTY behavior... out of TestBackend reach") — the
`ChatController` overlay is an ordinary quadraui widget rendering plain text into the
symbol grid, so it **is** mockable and testable, which is why §4 is drawn the way it is.
No existing contract has pinned a `ChatController` transcript's rendered shape before —
§4 is this contract's own proposal for that, flagged explicitly.

---

## 2. PB-1 — portal project↔repo mapping (#2531): no visual mock

Config-only; invisible to a screen grid, like ms-65 §6's `tabs.json` or its own §7
fixture seam. Pinned here in prose so #2531's implementor and #2532/#2533's consumers
agree on the exact shape without a shared session.

**Home: `coordinator.yml`'s existing `portal:` block** (`coord/config.py::PortalConfig`),
not `coord.db`. #2531's own issue body left this an open choice ("pick based on how
often the mapping changes... likely rare"); this contract picks the static,
operator-authored config file — no migration, no new CLI write path, consistent with
"a project's repo set is set once and rarely moves" from the same issue body. **Flagged
as this contract's decision, not a foregone conclusion** — see §6.4.

```yaml
portal:
  enabled: true
  base_url: "https://intake.example.com"
  project_repos:
    - project_id: "proj_9f2a"
      repos: [natal-chart]
    - project_id: "proj_44de"
      repos: [code-coordinator, quadraui]
```

New field on `PortalConfig`:

```python
@dataclass
class PortalProjectRepo:
    project_id: str          # portal-side id, opaque to coord
    repos: list[str]         # must each name a configured repos[].name

@dataclass
class PortalConfig:
    ...
    project_repos: list[PortalProjectRepo] = field(default_factory=list)
```

**Testable (config-load level, not a screen fact):**
- `project_id` must be a non-empty string; duplicate `project_id` entries → `ConfigError`
  at load (mirrors this module's existing "reject at load, never at use" posture for
  every other `portal.*` field).
- `repos` must be a non-empty list; every entry must name a `repos[].name` already
  declared in the same `coordinator.yml` — an unknown repo name → `ConfigError` at load,
  same posture as the file's other cross-reference validations (e.g. `depends_on`).
- A `project_id` **absent** from `project_repos` is **not** a load error — it is a valid,
  common state (a brand-new portal project the operator hasn't mapped yet). Looking it up
  returns an empty list, never raises. Pinned lookup surface:
  `Config.portal.repos_for_project(project_id: str) -> list[str]` (this contract's
  proposed name — #2531's implementor may pick a different one, but the **empty-list,
  never-raise** contract for an unmapped project is the pinned fact §3/§4 depend on).

---

## 3. PB-2 — "Approved work items" panel (#2532)

### 3a. Activity-bar entry

New `PanelDefinition` appended **after** the existing `panel:queue` entry (`⇅`) and
before the bottom-pinned `panel:settings` (`⚙`) — i.e. the 12th top-level icon. Icon
`✓` (U+2713 CHECK MARK) — collides with none of the existing activity-bar icons
(`B M ▶ >_ ▦ ≣ ◆ ◉ § ▤ ⇅` or the pinned `⚙`; that set is `tui/src/app/mod.rs`'s own
collision-checked list, most recently extended by `⇅`/Queue). Semantically apt (✓ =
approved) and reuses a glyph this app already trusts as "approved/done" in the
**action**-icon namespace (`icon_for_action`: `mark-refined`, `approve-gate-a`, `ready`
all already render `✓`) — a different namespace (panel icons vs. row-action icons) so
this is not a re-collision, just a deliberate echo. Tooltip: **"Approved work items"**.
Title (shown in shell chrome): **"APPROVED WORK ITEMS"**. Widget id: `panel:approved`.
Proposed `SidebarView` variant name: `SidebarView::Approved`.

**No panel toolbar** — every verb here is row-scoped (§4's one action), the same
reasoning `panel_toolbar()` already applies to Queue (`tui/src/app/sidebar.rs:341`):
"a panel-level toolbar button would have no row to bind to." Main content therefore
starts directly with this panel's own content at row 0 of `main_content_bounds` — no
toolbar row precedes it, same structural shape as Queue and Audit.

**No fetch of its own.** Like Queue (`tui/src/app/mod.rs`'s own comment on `SidebarView::
Queue`), this panel's data rides the existing `/board` poll — see §5's wire shape — not
a new view-gated fetch block. The daemon-host-only rule that gates `coord portal outbox/
events` (#2336, `coord/skills/portal-followup/SKILL.md`) does **not** apply here the same
way: those are CLI commands reading `~/.coord/coord.db` directly on whatever machine
runs them, whereas the TUI already only ever talks to `/board` over HTTP on the board
daemon (`coord serve`) — there is no "thin client's empty local DB" failure mode for a
widget that never opens a local DB in the first place. Flagged so nobody re-derives
#2336's guard here by mistake; see §6.6.

### 3b. Sidebar — aggregate reading

Mirrors `queue_sidebar()`'s plain-list convention (`tui/src/app/drive_queue.rs:1107`),
not Board's tree:

```
 APPROVED WORK ITEMS
  N ready to pull
  ⚠ M missing a repo mapping        (only rendered when M > 0 — mirrors
                                      queue_sidebar's `if s.blocked > 0`)
```

**Testable:** `driver.screen_contains("APPROVED WORK ITEMS")` is `true` whenever this
panel is active, regardless of row count. `driver.screen_contains("missing a repo
mapping")` is `true` iff at least one visible row's mapped-repo list is empty; `false`
when every row has ≥1 mapped repo (including the zero-row empty state, §3d).

### 3c. Main panel — the row list

Plain aligned-column list (**not** the bordered `DataTable` grid Queue uses) — modeled
on the Audit panel's own list (`tests/acceptance/ms-33/mocks/audit-panel-populated.
screen`, "a scrollable, newest-first list... modeled on the Plans panel",
`tui/src/app/types.rs:236-239`), the closer existing precedent for a panel with no
sortable numeric columns and no drag-reorder verb. One header row (this contract's own
addition — Audit's list has none, because Audit's five fields read as a log line; this
panel's four fields are a genuine record the operator scans for repo-routing, so a
header earns its row here) followed by one row per approved submission, **oldest-first**
(mirrors `coord.portal_store.list_submissions()`'s own `ORDER BY first_seen_at ASC` —
picked so the panel reads as a FIFO backlog to work through, not a reverse-chronological
feed; flagged as a product decision in §6.5).

Column order follows #2532's own issue prose verbatim ("submission reference, outcome
summary, which client/project it belongs to, and the mapped repo(s)") **except** outcome
and client/project are swapped from that prose order — identity columns
(Submission, Client / Project) first, the free-text summary (Outcome) last and widest,
mirroring Audit's own "identity columns, then widest free-text summary last" shape.
Flagged in §6.1 as a deliberate, not-obviously-forced deviation from the issue's literal
listing order.

**Pinned column widths (this contract's own values, not derived from any existing
constant — same posture ms-65 §2b/§9 took for its own truncation widths):**

| Column | Width | Truncation |
|---|---|---|
| Submission | 12 cols | `…` past 11 chars |
| Client / Project | 22 cols | `…` past 21 chars |
| Repo(s) | 16 cols | `…` past 15 chars; comma-joined when >1 mapped repo |
| Outcome | 32 cols | `…` past 31 chars |

A row whose `repos_for_project(project_id)` is empty (§2) renders the literal string
`"— no mapping —"` in the Repo(s) column — not a blank cell, so "no mapping" and "not
yet loaded" are never visually indistinguishable.

See `mocks/approved-items-populated.screen` (two rows: `sub_2f6a1c` mapped to
`natal-chart`; `sub_77b0e4` unmapped, showing the placeholder).

**Testable:**
- `driver.screen_contains("Submission")`, `driver.screen_contains("Client / Project")`,
  `driver.screen_contains("Repo(s)")`, `driver.screen_contains("Outcome")` are all `true`
  on the header row whenever ≥1 approved submission exists.
- `driver.screen_contains("sub_2f6a1c")` and `driver.screen_contains("natal-chart")` are
  both `true` for a mapped row.
- `driver.screen_contains("— no mapping —")` is `true` for a row whose project has no
  `project_repos` entry (or an entry with an empty `repos:` list).

### 3d. Empty state

Zero approved submissions: sidebar shows `"0 ready to pull"` and **no** "missing a repo
mapping" line (§3b's conditional). Main panel shows one line, no header row (mirrors
Audit's own "No audit events yet..." — a header with nothing under it reads as broken,
same reasoning Queue's empty-grid comment (`tui/src/app/drive_queue.rs:1251`) gives):

```
No approved work items yet — check back after a customer signs off.
```

(deliberately **not** "No approved *submissions* yet" — that phrasing would make
`screen_contains("Submission")`, used below to assert the header row is absent, an
unreliable check under a case-insensitive matcher, since "submission" is a substring of
"submissions". Picking wording that never contains the header word at all sidesteps the
question of matcher case-sensitivity entirely, rather than relying on an assumption
about it.)

See `mocks/approved-items-empty.screen`. Status-bar hint in this state:
`" no approved submissions  q=quit "` (mirrors Queue's own empty-state hint shape,
`tui/src/app/mod.rs:9374`).

**Testable:** `driver.screen_contains("No approved work items yet")` is `true` iff the
row count is 0; `driver.screen_contains("Client / Project")` (only present on the header
row) is `false` in the same state.

### 3e. Row detail (`Enter`)

Selecting a row and pressing `Enter` opens a detail region below the list — same
"grid stays, detail appends below" shape as Audit's own `Enter`=detail (`tests/
acceptance/ms-33/mocks/audit-panel-detail.screen`), reusing its `"── <Title> ──"`
divider convention verbatim (here: `"── Submission Detail ──"`). The fields shown are
**exactly** the four submission fields #2533's briefing consumes (§4b) plus routing
context — deliberate: what the operator reviews here before pulling is the same
substance the decomposition session receives, so there is nothing hidden between
"looks right in the panel" and "is what the session was told."

```
submission:  sub_2f6a1c
client:      Heuron Technologies
project:     proj_9f2a (Portal redesign)
outcome:     Customers can self-serve a billing address change instead of
             emailing support.
audience:    Existing subscription customers on the billing portal
done:        Customer edits and saves a new billing address from their account
             page, sees it reflected immediately, and gets a confirmation email.
constraints: Must reuse the existing Stripe customer object — no new payment
             fields.
repos:       natal-chart
received:    3d ago
```

Long values wrap, continuation lines indented to the value column (13 chars) — this
wrapping convention is this contract's own addition (Audit's key:value fields were all
single-line and never needed it); flagged in §6.2. See `mocks/approved-items-detail.
screen`. Status-bar hint: `" j/k=nav  Esc=close detail  right-click=menu  q=quit "`
(mirrors Audit's `"Esc=close detail"` exactly, `tests/acceptance/ms-33/mocks/
audit-panel-detail.screen`'s own status line).

**Testable:**
- `driver.press(Enter)` on a selected row → `driver.screen_contains("── Submission
  Detail ──")` is `true`.
- All four of `driver.screen_contains("outcome:")`, `driver.screen_contains("audience:")`,
  `driver.screen_contains("done:")`, `driver.screen_contains("constraints:")` are `true`
  while the detail is open, and the visible text after each label matches that row's
  full (untruncated) field — the one place in this panel where the 32-column Outcome
  truncation from §3c does **not** apply.
- `Esc` closes the detail region; the list above is unaffected (selection, scroll
  unchanged) — same "detail is additive, not a replace" behaviour Audit already pins.

---

## 4. PB-3 — "Pull into decomposition session" (#2533)

### 4a. The context-menu item

Right-click a row → one new item, top of the menu, mirroring the "one primary
verb, plain label" convention `dialogs.rs` already uses for `"Chat about issue"` /
`"Open milestone chat"`:

```
Pull into decomposition session
```

Action id: `pull-into-decomposition-session`. Icon: `⇢` (U+21E2 RIGHTWARDS DASHED ARROW)
— a fresh glyph, not reused from `icon_for_action`'s existing set, and **deliberately
not** `⌨` (which that table already spends on the `InteractiveLaunchMode` PTY family —
`start-work-interactive`, `reattach-live-session`, etc.). Reusing `⌨` here would
misleadingly imply this action is that same PTY family; it is not (§1). Flagged as a
fresh-glyph pick in §6.3.

**Disabled** (present, greyed, inert — same convention ms-65 §8c pinned for "Pin tab" on
an already-pinned tab, "not depicted in the mock" there and not here either) when the
row's `Repo(s)` column reads `"— no mapping —"` (§3c) — i.e. `repos_for_project` returned
empty. No separate mock renders this state; the populated mock's second row
(`sub_77b0e4`) is the fixture a test-author drives to exercise it.

See `mocks/approved-items-context-menu.screen` (right-click on the *mapped* row,
`sub_2f6a1c` — item enabled).

**Testable:**
- Right-click a row with ≥1 mapped repo → `driver.screen_contains("Pull into
  decomposition session")` is `true` and the item is enabled (clickable).
- Right-click `sub_77b0e4` (no mapping) → the same string is present but the item is
  `disabled` (`ContextMenuItem.disabled == true`) — clicking it is a no-op, same
  assertion shape ms-65 uses for its own disabled-item case.

### 4b. What "briefed" means — the four fields + routing context

Per #2533's own body, the dispatched session's briefing carries: the submission's
**outcome**, **audience**, **done-definition**, **constraints** (reached via the
existing bridge, never a new direct portal read) plus the **mapped repo(s)** from §2 and
`coordinator.yml` topology context for those repo(s) ("the same way `docs/
CUSTOMER_PORTAL.md`'s design-round step already uses it"). §3e's detail pane already
renders exactly these four fields plus the resolved repo(s) — this contract pins that
identity explicitly so nothing drifts between "what the operator saw" and "what the
session got."

### 4c. Dispatch mechanism — reuses the `new-issue-chat`/`milestone-chat` family, not the PTY family

Per #2533's own body ("Follow... `type="milestone-chat"`... and `type="new-issue-chat"`...
this is a new `type=` alongside them, not a repurposing of either") and this contract's
own source reading (§1): the click fires a plain CLI dispatch —

```
coord portal decompose-chat <submission_id> [--machine NAME]
```

— a **new top-level command**, nested under the existing `coord portal` group (alongside
`outbox`/`events`/`sync`/`enqueue-*`/`link`) since `submission_id` is a portal-domain
concept end to end; unlike `new-issue-chat` (top-level, no group) this one has an
obvious, already-populated home. Prints the new assignment id to stdout, same contract
`new-issue-chat` already documents (`coord/commands/chat.py:166`) — "the TUI shells this
out and binds a `ChatController` overlay to the returned id."

**Type string:** `type="decomposition-chat"`. Unlike `new-issue-chat`'s read-only tool
ACL (deny-lists `gh issue create`, `git push`, etc. — "submission is handled by the
TUI"), this session's whole job (#2533 body) is to actually run `coord issue create`
(never raw `gh`, this repo's own house rule) and `coord drive-queue add` (never `coord
assign`/`coord drive --tmux`, this repo's own standing preference) once it has decided
whether the work is oracle-loop-shaped. So its tool posture is closer to a scoped `Bash`
allowlist for exactly those two `coord` subcommands than to `new-issue-chat`'s blanket
deny-list — pinned here as a **behavioural** fact (which commands the session is
expected/permitted to run), not as an exact ACL string, which is #2533's implementation
detail.

**Machine selection:** mirrors `pick_new_issue_chat_machine` (`coord/new_issue_chat.py`)
generalized to *every* mapped repo — the picked machine must list **all** of the
submission's mapped repos (`m.can_work_on(r)` for every `r` in `repos_for_project
(project_id)`), not just one. A submission mapped to repos with no single common machine
has no valid target; refuse with a clear CLI error rather than silently picking a machine
that can't reach every repo. **Flagged as an open question in §6.7** — #2533's own body
doesn't address the multi-repo-machine-mismatch case, and this contract does not invent
a resolution beyond "refuse clearly," since a silent partial-repo session would be worse.

### 4d. What the TUI shows once the session binds

Mirrors `dispatch_board_chat_new_issue` / `maybe_bind_pending_board_chat`
(`tui/src/app/dialogs.rs:4772-4928`) exactly: a toast fires immediately on dispatch —

```
Decomposition chat
sub_2f6a1c: chat ready — type to start.
```

(mirrors `"{}: chat ready — type to start."`, the exact `new-issue-chat` toast body,
substituting the submission id for the repo name) — then, once the polled assignment
appears, the TUI **switches `active_view` to Board and its Chat sub-tab**
(`self.switch_active_view(SidebarView::Board); self.board_detail_tab =
BoardDetailTab::Chat;`) — the same redirect `milestone-chat` already performs regardless
of which panel triggered it, because `inject_chat` is one shared overlay slot. This
panel does **not** grow its own chat pane; the operator is deliberately moved to the
already-established Chat tab. A `ChatController` is opened with an **empty** transcript
(`chat.set_transcript(Vec::new())`) and its own internal status line, mirroring
`new-issue-chat`'s exact format string:

```
  Decomposition chat → sub_2f6a1c  (Ctrl+S/Alt+Enter = send · Esc = close)
```

(the real `new-issue-chat` hint additionally carries `· Ctrl+F = file issue` — dropped
here since filing is the *session's* job via `coord issue create`, not a manual TUI
shortcut; see §4c.)

See `mocks/approved-items-chat-opened.screen`. **Not pinned:** any actual transcript
turn text (the session's own words are non-deterministic — no mock asserts specific
dialogue) and the **global** status-bar (row 39) text while the Chat tab is focused —
only the `ChatController` widget's own internal status line above is grounded in
existing source; the mock's row-39 hint is Board's ordinary baseline
(`" n=notify  m=merge  R=retry  P=purge  q=quit "`) shown for visual completeness, not
independently verified for this exact state (flagged in §6.8).

**Testable:**
- Triggering §4a's action → `driver.screen_contains("chat ready — type to start")` is
  `true` (the toast) within one tick.
- The active panel becomes Board (`driver.screen_contains("[Chat]")` true, i.e. the
  `Board / Issue / Chat / Terminal` sub-tab row shows `Chat` bracketed-active) and
  `driver.screen_contains("sub_2f6a1c")` is `true` somewhere in the Chat pane's own
  status line.
- The transcript area contains no turn text immediately after binding — only the
  `ChatController`'s status line and an empty body.

---

## 5. Wire shape — `/board`'s `approved_submissions` (PB-2/PB-3's shared read)

Not specified by any of #2531/#2532/#2533's issue bodies at the field-name level — they
say *which* four submission fields matter (outcome/audience/done-definition/constraints)
but not their JSON key spelling, since that's coord-portal's schema and coord-portal is
a separate repo not visible from here (flagged, §6.9, per this agent's brief: say so
rather than silently inventing a fact nobody can check). This contract proposes the
following `/board` addition (`coord/serve_app.py`) as the shape §3/§4's mocks assume:

```json
{
  "approved_submissions": [
    {
      "submission_id": "sub_2f6a1c",
      "client": "Heuron Technologies",
      "project_id": "proj_9f2a",
      "project_label": "Portal redesign",
      "outcome": "Customers can self-serve a billing address change instead of emailing support.",
      "audience": "Existing subscription customers on the billing portal",
      "done_definition": "Customer edits and saves a new billing address from their account page, sees it reflected immediately, and gets a confirmation email.",
      "constraints": "Must reuse the existing Stripe customer object — no new payment fields.",
      "repos": ["natal-chart"],
      "received_at": "2026-08-18T09:14:00Z"
    }
  ]
}
```

- `repos` is server-computed via §2's `repos_for_project(project_id)` — the TUI never
  reads `coordinator.yml` directly, same "server resolves, client renders" split every
  other `/board` field already follows. An empty array is the "no mapping" case §3c/§4a
  render.
- `received_at` maps to `coord.portal_store.SubmissionRecord.first_seen_at` (already a
  real coord-side field, just never serialized to `/board` before).
- Source, today, is exactly `signoff.approved` submissions per PB-2/PB-3's own text —
  once `coord-portal`#132 (the operator "start work" override) lands on the portal side,
  more submissions become eligible, but this contract adds **no** `source`/`status`
  field to distinguish them: at Gate-A time nothing coord-side can produce the override
  state yet, and inventing a column for a value that can never appear would be exactly
  the kind of unwarranted invention this agent's brief says to avoid. If/when #132 lands,
  that is a contract amendment (`coord acceptance mock ... --amend`), not a silent
  implementation choice by whoever picks up #2532/#2533.

---

## 6. Notes / open questions (mock-author decisions — flagged per this agent's own brief)

1. **§3c's column order (Submission, Client/Project, Repo(s), Outcome) reorders #2532's
   own prose listing** (submission, outcome, client/project, repos). A deliberate
   readability call, not a misreading — flagged so a reviewer who diffs against the
   issue text literally doesn't file it as a bug.
2. **§3e's multi-line value-wrapping convention is new** — no prior contract's detail
   pane needed it (Audit's key:values were always short). If #2532's implementor finds
   quadraui's own detail-pane primitive already wraps differently, that's an amendment,
   not a silent deviation.
3. **§4a's `⇢` icon is a fresh pick**, chosen specifically to avoid implying the PTY
   family (§1, §4c). If the operator has a preferred glyph, amend here before `coord
   acceptance author` writes a slice against `⇢` specifically.
4. **§2's "coordinator.yml, not coord.db" call is this contract's resolution of #2531's
   own explicitly-left-open question.** If the mapping turns out to change often in
   practice (contrary to the issue body's own "likely rare" guess), that is grounds for
   an amendment to move it to `coord.db`, not a bug in #2531's implementation.
5. **§3c's oldest-first ordering is a product decision**, not derived from any existing
   panel (Audit is newest-first; Queue is run-order). Revisit if operators actually want
   newest-first (e.g. "what did the customer just approve").
6. **§3a's claim that #2336's daemon-host guard doesn't apply to this panel** rests on
   the TUI always talking to `/board` over HTTP, never opening `~/.coord/coord.db`
   locally — true for every other panel in this app today, but if #2532's implementation
   somehow reads portal state through a different path, this note is the thing to
   revisit first.
7. **§4c's multi-repo machine-selection gap is a genuine open question, not resolved
   here.** #2533's own body is silent on what happens when a submission maps to repos
   with no common machine. This contract only pins "refuse clearly" as the floor; the
   implementor should treat the exact refusal UX as theirs to design, or raise it back
   to Gate A if it turns out to need a mock of its own.
8. **§4d's row-39 status-bar text for the live-chat state is unverified** — shown for
   visual completeness only, not a pinned fact. Do not write a test asserting the exact
   global status-bar string while the Chat tab is focused; assert the `ChatController`'s
   own internal status line instead (the part this contract could actually ground in
   existing source, §1/§4d).
9. **§5's exact JSON field names (`outcome`, `audience`, `done_definition`,
   `constraints`, `project_id`, ...) are this contract's own proposal**, not read off
   coord-portal's real schema (a separate, non-checked-out repo). #2531/#2532/#2533's
   implementors should treat these as a starting proposal to confirm against
   `coord-portal`'s actual `submissions` table shape (via `coord/portal_bridge.py`'s
   existing pull path) and amend this contract if the real field names differ — silently
   renaming them during implementation would be exactly the kind of drift Gate A exists
   to catch before `coord acceptance author` writes a sealed suite against the wrong
   names.
10. **Dependency ordering (#2531, #2532 independent; #2533 after both) is unaffected by
    anything in this contract** — restated from #2530's own `## Work order` for the
    test-author's reference, same as ms-65 §10.7's equivalent note.

---

## 7. Mocks index

| File | Scenario | Issue(s) |
|---|---|---|
| `mocks/approved-items-empty.screen` | Zero approved submissions — baseline/regression reference | #2532 §3d |
| `mocks/approved-items-populated.screen` | Two rows: one mapped (`natal-chart`), one unmapped (`— no mapping —`) | #2532 §3a–3c |
| `mocks/approved-items-detail.screen` | `Enter` on the mapped row — full outcome/audience/done/constraints + repo(s) | #2532 §3e |
| `mocks/approved-items-context-menu.screen` | Right-click the mapped row — "Pull into decomposition session" enabled | #2533 §4a |
| `mocks/approved-items-chat-opened.screen` | After dispatch: toast + redirected to Board's `[Chat]` sub-tab, empty transcript | #2533 §4c–4d |

All mocks: 120×40, symbols-only `.screen` grid (Ambiguous-width-1 metric, §0). Column
widths are this contract's own illustrative layout (§1) — the pinned, testable facts are
the substrings and their order, not fixed x-offsets.
