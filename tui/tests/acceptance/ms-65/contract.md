# Gate A Contract — ms-65: coord-tui per-panel document tabs (preview/pin)

_Mock-authored 2026-08-15 for milestone 65 (tracking issue #2289)._
_Issues in scope: **#2282** (Board tab strip), **#2283** (close/navigate/overflow),
**#2284** (Pipeline's own set + round-trip), **#2285** (per-tab sub-state),
**#2286** (persistence, `~/.coord/tabs.json`), **#2287** (discoverability: status
bar / `?` help / right-click menu), **#2288** (side-by-side split)._
_**#2281** (`make_app_with_board_json` fixture seam) has no UI surface of its own —
every mock below assumes its fixtures exist; see §7._
_Driver: `tui-tuidriver`. All mocks: **120×40 terminal**, symbols-only `screen()`
grid (`TestBackend`) — see §1's note on styling before reading §2's preview marker._

---

## 0. Grid geometry (unchanged, pinned for the test-author's reference)

Every mock in this contract uses the shell layout already shipped and pinned by
prior contracts (ms-33 §2, ms-38 §1): column 0–1 = activity bar (accent-or-space +
1-glyph icon, multi-glyph icons like `">_"` (Terminal) render only their first
glyph — observed empirically in ms-38's mocks, not derived from source), column 2
= `│` separator, columns 3–37 = sidebar content (35 cols, `default_sidebar_width`),
columns 38–119 = main panel content (82 cols) — **no separate divider column between
sidebar and main**; whatever the active panel paints at column 38 *is* the visual
boundary. Row 39 (last) is the global status bar; rows 0–38 are panel content. The
Board (`B`) icon is row 0, Pipeline (`▶`) is row 2, of the current 11-icon activity
bar; `⚙` Settings is bottom-pinned at row 38. Column metric: East-Asian *Ambiguous*
counts as 1 (ms-38 §7's rule) — every glyph newly introduced by this contract
(`∘ ▸ ‹ › × ║`) was checked and is Ambiguous or Neutral, i.e. 1 column, consistent
with the existing set.

---

## 1. Known limitation this contract designs around: no styled-cell reads yet

`TuiDriver::screen()` is symbols-only (quadraui#593, a milestone pre-requisite, is
**not yet landed**). The issue bodies describe the preview tab as *"italic"* —
quadraui's `TabItem.is_preview` primitive already exists and the TUI rasteriser
already paints it with `Modifier::ITALIC` + a distinct theme colour (verified
against the pinned quadraui rev, `src/tui/tab_bar.rs`), so **implementing** italic
is not blocked. **Asserting** it from a `.screen` grid is blocked, because style is
not part of the symbol dump.

**Resolution pinned here, since the issue bodies leave it silently assuming a
richer medium:** a preview tab's rendered `TabItem.label` carries a plain-text
marker in addition to (not instead of) the italic style — a leading `∘ ` (U+2218
RING OPERATOR, one column) immediately before the label text, present only when
`is_preview == true`. A pinned tab's label carries no such marker. This makes "is
this tab a preview" a `screen_contains`-testable fact today, independent of #593;
once #593 lands, a slice may additionally assert the `Modifier::ITALIC` cell run,
but that is additive, not a replacement for the marker (removing it would be a
contract amendment, not a JIT-slice decision).

The `∘ ` marker is **not** a design requirement from any of the 8 child issues —
it is this contract's answer to a gap those issues left open. Flagging it per this
agent's brief: read the issue bodies before amending it.

---

## 2. Document tab strip — Board (#2282)

### 2a. Where it renders

Board's main panel today paints, top to bottom: the panel toolbar row
(`panel_toolbar()`, `"[ [A]dd ]  [ [N]otify ]  [ [R]etry ]  [ [P]urge ]"` —
unchanged by this milestone) → the `Board / Issue / Chat / Terminal` sub-tab bar
→ the active sub-tab's content.

**Pinned:** the new document tab strip is a **new row inserted between the panel
toolbar and the existing sub-tab bar** — i.e. toolbar, then doc-tab strip
(only when `doc_tabs.len() > 0`), then `Board / Issue / Chat / Terminal`, then
content. See `mocks/board-preview-tab.screen` and `mocks/board-pinned-3-tabs.screen`.

**When zero tabs are open, the strip renders nothing and reserves no row** — the
sub-tab bar sits directly under the toolbar, byte-identical to pre-ms-65 Board.
See `mocks/board-baseline-no-tabs.screen`, which is also the regression bar
#2285's "behaviour-preserving for a single open tab" is measured against (this
mock is the *zero*-tab case, the strictest form of that bar).

**Testable:**
- `driver.screen_contains("[Board]")` and `driver.screen_contains("Issue")` are
  both `true` on the Board panel regardless of tab state (sub-tab bar always
  renders).
- With zero doc tabs open, the row immediately below the toolbar row **is** the
  `Board / Issue / Chat / Terminal` row — no blank tab-strip row is reserved.
- With ≥1 doc tab open, a tab-strip row renders between the toolbar and the
  sub-tab bar, containing at least one `"#<N>"` substring.

### 2b. Tab label format

Each tab's label is `"#<issue_number> <title>"`, truncated to **20 columns**
(inclusive of the `#<N> ` prefix) with a trailing `…` when the full label would
exceed it — mirrors the existing `trunc()` convention used elsewhere in this app
(e.g. `board_selected_issue_group` truncates issue titles for the detail pane).
A preview tab's label is additionally prefixed `"∘ "` (§1), pushing the same
20-column budget out by those 2 columns (i.e. `"∘ #<N> <title>"` truncates at 22
total columns) — the marker is never itself truncated away.

**This 20/22-column budget applies to a single, undivided doc-tab strip
(§§2–4, 7, 8). A split pane (§9) uses its own, narrower, separately-pinned
14/16-column budget** — see §9 for the split-pane value and why it differs.

**Testable:** `driver.screen_contains("#101")`, `driver.screen_contains("#102")`,
`driver.screen_contains("#103")` are all `true` in `mocks/board-pinned-3-tabs.screen`.

### 2c. Active tab marker

The active tab (of the currently focused tab group) is wrapped in `[` `]` —
`"[<label> ×]"` vs. an inactive tab's `"<label> ×"`. This is a new,
milestone-local convention (not shared with the sub-tab bar's own bracket
convention below, which pre-dates this milestone and is pinned independently).

### 2d. Close glyph

Every open, closable tab renders a trailing `× ` (U+00D7, `quadraui::tui::tab_bar::TAB_CLOSE_CHAR`
— reuse the primitive's constant, do not hand-roll a different glyph).

### 2e. Open semantics (single click / second click / double click)

Pinned by the tracking issue and unchanged here — restated for the test-author,
since it is the black-box behaviour `mocks/board-preview-tab.screen` and
`mocks/board-pinned-3-tabs.screen` together pin:

1. Single-click a sidebar issue row not already open → if a preview tab exists,
   **replace it in place** (same index, same active state); else **append** a new
   preview tab and activate it.
2. Single-click a sidebar issue row already open (preview or pinned) → activate
   its existing tab; no new tab, no replace.
3. Double-click a sidebar issue row → open-or-activate as above, **then** promote
   that tab to pinned (drops `is_preview`, drops the `∘ ` marker, tab is no longer
   the replaceable slot).
4. **At most one preview tab per tab group, ever.** This is the fact `screen_contains`
   can check today without #593: after two single-clicks on two different rows,
   the tab **count** in the strip is 1, not 2 — count the `×` occurrences (or the
   `#<N>` occurrences) in the strip row, not tab colour/style.

**Testable (behavioural, count-based, no styling required):**
- Fixture: single-click row #102 → strip contains exactly one `×` and one `#102`.
- Single-click row #103 next (#102 still a preview) → strip **still** contains
  exactly one `×` and now shows `#103`, not `#102` — the replace-in-place case.
- Double-click row #101, then single-click row #102 → strip contains **two** `×`
  occurrences (`#101` pinned, `#102` preview) — a distinct 2-tab intermediate
  state, **not** depicted in any mock; do not confuse it with the bullet below.
- Double-click row #101, then double-click row #102, then double-click row #103
  (each row not yet open at the moment it is clicked) → strip contains **three**
  `×` occurrences, all pinned, none carrying the `∘ ` marker, `#103` active —
  see `mocks/board-pinned-3-tabs.screen` for this exact end state. Tracing it
  through rules 1/3/4: dbl-click `#101` → not open, no preview exists yet →
  append `#101` as preview, activate, then promote to pinned
  (`[#101 pinned]`). dbl-click `#102` → not open; no preview exists (the only
  open tab, `#101`, is already pinned) → append `#102` as preview, activate,
  promote to pinned (`[#101 pinned, #102 pinned]`). dbl-click `#103` → same
  reasoning (no preview exists) → append, activate, promote
  (`[#101 pinned, #102 pinned, #103 pinned]`, `#103` active) — matching the
  mock's bracketed `[#103 … ×]`. **This is a different sequence from the
  single/double bullet above and does not extend it** — starting from
  `[#101 pinned, #102 preview]` and double-clicking not-yet-open `#103` would
  instead hit rule 1's preview-exists branch (a preview tab, `#102`, already
  exists) and **replace `#102` in place** before promoting, landing on
  `[#101 pinned, #103 pinned]` (2 tabs, `#102` evicted) — not the 3-tab mock.
  Only three separate double-clicks, each landing while no preview tab is open,
  reach the state `mocks/board-pinned-3-tabs.screen` depicts.

### 2f. Reveal-on-activate

Activating a tab (by any path — click, `Ctrl-Tab`, promote) selects the matching
sidebar row **and scrolls it into view**. `mocks/board-pinned-3-tabs.screen`
depicts this concretely: the sidebar's issue list has 5 rows total, but the
visible window starts at `#102` (not `#101`) with a `"⋮ 1 more above"` marker row
(one row, since only the single issue `#101` is scrolled out) rendered within
the scrollable list area, directly below the `▾ claude-coordinator` tree header
and above `#102` — **not** above the `⌕ Filter issues…` search box, which stays
fixed at the top of the sidebar regardless of scroll state — and `▸ #103` (the
active tab's issue) visible and marked — proof the scroll position moved, not
just that a `▸` glyph appeared somewhere off-screen.

**Testable:** with 5 issues in the fixture and the viewport short enough that not
all 5 fit, activating the tab for the issue nearest the bottom must make
`driver.screen_contains("▸ #103")` `true` **and** `driver.screen_contains("#101")`
`false` (scrolled out) in the same frame. `▸` (U+25B8) is this contract's pinned
sidebar-selection marker — distinct from `▶` (Pipeline's activity-bar icon) and
from the `[...]` active-tab bracket convention in §2c; do not conflate the three.

---

## 3. Pipeline owns an independent tab set (#2284)

### 3a. Where it renders

Pipeline's `panel_toolbar()` returns `None` (pinned by `panel_toolbar_pipeline_is_absent`,
already shipped) — Pipeline has **no** toolbar row. So on Pipeline, **the doc-tab
strip is the first row of the main panel**, immediately followed by the existing
`Overview / Issue / Log / Summary / Terminal` sub-tab bar. This is the one
structural asymmetry between the two panels this milestone must get right — see
`mocks/pipeline-tabs-independent.screen`.

**Testable:** on the Pipeline panel with ≥1 doc tab open, the doc-tab strip is the
very first content row of `main_content_bounds` (no toolbar row precedes it); on
Board it is the second (toolbar row precedes it).

### 3b. Independence

Board's and Pipeline's tab sets, active tabs, preview slots and scroll positions
are stored per `PanelScope` (`Board` | `Pipeline`) and never merge, reorder, or
drop into each other when switching panels. `mocks/board-pinned-3-tabs.screen`
(Board: `#101 #102 #103`) and `mocks/pipeline-tabs-independent.screen` (Pipeline:
`#201 #202`) are deliberately numbered in disjoint ranges (100s vs. 200s) so a
side-by-side read of the two mocks is itself the independence proof — no single
screen can show a "before/after panel switch," so the milestone's own headline
scenario (open 3 Board tabs → switch to Pipeline, open tabs there → switch back,
Board's 3 are unchanged) is a **round-trip behavioural test**, not a static mock
fact. The two mocks pin the two endpoints' rendered state; the round trip between
them is the test-author's job to assert via `coord acceptance run`, not something
a screen grid alone proves.

**Testable:**
- Board panel active, 3 Board tabs open → switch to Pipeline (no Pipeline tabs
  yet) → Pipeline shows its baseline (no doc-tab strip row, matching §3a for the
  zero-tab case) — Board's 3 tabs are not visible and not lost.
- Open 2 Pipeline tabs → switch back to Board → strip shows exactly `#101 #102
  #103` again, same order, same active tab, same scroll position as before the
  switch.

---

## 4. Close, navigate, overflow, empty state (#2283)

`mocks/board-tabs-overflow.screen` — 5 Board tabs open, only 4 fit the strip
width; the active tab (`#105`, also the fixture's last/highest issue) is kept
on-screen. Because `#105` is both the active tab *and* the actual rightmost tab
in the fixture (there is no 6th tab beyond it), the strip scrolls to show
`#102`–`#105`: `#101` is scrolled out to the left, so `‹` renders at the strip's
left edge, but nothing is scrolled out to the right, so **`›` is absent** — per
the rule stated two paragraphs below, `›` only renders when tabs exist beyond
the rightmost visible one, which is not the case here. Reuse
`TabBar::fit_active_scroll_offset` / `correct_scroll_offset` per the issue's own
design note — **do not** hand-roll a second fit-to-width algorithm; this
contract does not re-derive that algorithm's math, it only pins that `‹`/`›` are
the rendered overflow glyphs and that the active tab is never scrolled off both
ends simultaneously. (`›` is exercised the same way `‹` is here, just from the
other direction — e.g. activating `#101` instead of `#105` in this same fixture
would scroll to show `#101`–`#104` with `›` present and `‹` absent — no separate
mock is needed to pin that symmetry, but a test-author asserting `›` should
drive that case, not this mock.)

**Testable:**
- `driver.screen_contains("‹")` is `true` when the leftmost open tab is scrolled
  out; `driver.screen_contains("›")` is `true` when the rightmost is. In
  `mocks/board-tabs-overflow.screen` specifically, `‹` is present and `›` is
  **absent** (see above) — do not assert `›` against this mock.
- The active tab's `#<N>` substring is always present in the strip row while that
  tab is active, regardless of scroll offset.
- Clicking a tab's `×` closes exactly that tab (verify via the `#<N>` count in the
  strip row before/after — down by exactly one occurrence).
- Middle-click anywhere on a tab (not just its `×`) closes it — same count check.
- `Ctrl-W` closes the active tab and activates a **defined neighbour**: the tab
  immediately to its left, or if it was the leftmost, the new leftmost. (Pinned
  here because the issue text says "state the rule and pin it" without stating
  one — this is the rule.)
- `Ctrl-Tab` moves active to the next tab, wrapping from the last to the first;
  `Ctrl-Shift-Tab` moves to the previous, wrapping from the first to the last.
- Closing the **last** open tab returns to `mocks/board-baseline-no-tabs.screen`'s
  exact state — no strip row, sub-tab bar directly under the toolbar, and
  (per the issue) the pane returns to selection-follows-tree (sidebar selection
  alone drives the detail pane again, same as before any tab was ever opened).

---

## 5. Per-tab sub-state (#2285)

No new mock — this issue is invisible to a static screen grid (it's about what
does *not* change when switching tabs vs. what wrongly used to). Pinned in prose
for the test-author, from the issue body's own acceptance criteria:

**Testable (requires driving two tabs, not a single static frame):**
- Open tab A on `#102`, switch its sub-tab (`Board / Issue / Chat / Terminal`)
  to `Issue`. Open tab B on `#103` (still on Board's default `Board` sub-tab).
  Switching back to tab A must show `Issue` still active for A — `driver.screen_contains("[Issue]")`
  true while A is active — and switching to B must show `[Board]` active for B,
  not `[Issue]`.
- Scroll position within a tab's sub-tab content is likewise per-tab: scrolling
  tab A's Issue body must not move tab B's (independently-scrolled) content.
- Closing a tab discards its sub-state; re-opening the same issue number starts
  from `Board` / scroll 0 again — it does not remember the old sub-state.
- A background tab's Terminal session (if any) keeps running while another tab
  is active — do not tear down `detail_terminal_sessions` on tab-switch, only on
  tab-close.

---

## 6. Persistence — `~/.coord/tabs.json` (#2286)

No visual mock (this is a file-format contract, not a screen). Pinned here since
the issue body describes the *behaviour* ("persist per scope: the ordered
document keys, which is active, and which if any is the preview") but not the
concrete JSON shape, and `workspace.json` / `settings.toml` (the two precedents
cited) are each their own ad hoc shape — this contract picks one so the
implementor (#2286) and any test-author fixture agree without a shared session:

```json
{
  "board": {
    "tabs": [
      {"repo": "claude-coordinator", "issue": 101},
      {"repo": "claude-coordinator", "issue": 102},
      {"repo": "claude-coordinator", "issue": 103}
    ],
    "active": {"repo": "claude-coordinator", "issue": 103},
    "preview": null
  },
  "pipeline": {
    "tabs": [
      {"repo": "claude-coordinator", "issue": 201},
      {"repo": "claude-coordinator", "issue": 202}
    ],
    "active": {"repo": "claude-coordinator", "issue": 202},
    "preview": {"repo": "claude-coordinator", "issue": 202}
  }
}
```

- Top-level keys are the lowercase `PanelScope` names in use today (`"board"`,
  `"pipeline"`); a scope absent from the file starts with no tabs.
- `tabs` is the **ordered** list of open document keys (order = strip order).
- `active` is `null` when the scope has no tabs.
- `preview` is `null` when there is no preview slot (i.e. every open tab is
  pinned), or the key of the one preview tab — which, per §2e, must also appear
  in `tabs`.
- Per-tab sub-state (§5) is persisted **only where cheap**: sub-tab selection
  (`"Board"` / `"Issue"` / etc., as a string) — yes; scroll offsets — the issue
  explicitly permits dropping these on restart, so this contract does not
  require them in the file, and their absence is not a test failure.

**Testable (file-level, not screen-level):**
- Writing 3 Board tabs + 2 Pipeline tabs, restarting, produces the exact shape
  above (module- and field-name matches aside — the JSON *shape* is what's
  pinned, not the Rust struct's derive output byte-for-byte).
- A tab whose `issue` number no longer exists in the loaded board is dropped on
  load, never rendered, never round-tripped back into a re-saved file.
- If the active document was pruned, a surviving neighbour in `tabs` (order
  preserved) becomes active; if `tabs` is now empty, `active` and `preview` are
  both `null`.
- A missing file, an empty file, or a file that fails to parse as the shape
  above all produce the **same** result: every scope starts with no tabs — never
  a panic, never a partial/best-effort parse.

---

## 7. Test-support seam (#2281)

Every mock and every testable clause above assumes a `CoordApp` built from real
board issues, not the empty-board default. Per #2281's own design (mirroring the
existing `make_app_with_audit_json` / `make_app_with_drive_queue` pattern):

```rust
// tui/src/app/fixtures.rs, behind the `test-support` feature
pub fn make_app_with_board_json(board_json: &str) -> CoordApp;
```

The JSON fed to it must be able to produce, at minimum, the fixture issue set
this contract's mocks use:

```json
{
  "claude-coordinator": [
    {"number": 101, "title": "Fix login race timeout", "milestone": null},
    {"number": 102, "title": "Auth token refresh bug", "milestone": null},
    {"number": 103, "title": "Race condition in poller", "milestone": null},
    {"number": 104, "title": "Flaky CI on macOS runners", "milestone": null},
    {"number": 105, "title": "Memory leak in watch loop", "milestone": null}
  ]
}
```

with an equivalent shape for the Pipeline sidebar's `#201`–`#203`. The exact
outer wire schema (field names, grouping) is #2281's decision to make against
the real `/board` payload shape in `tui/src/app/types.rs` — this contract only
pins that the fixture must be able to produce ≥5 Board issues and ≥3 Pipeline
issues addressable by number, since every scenario above opens issues by number.

---

## 8. Discoverability (#2287)

### 8a. Status bar hint

**Pinned, exact substring, taken verbatim from the issue body:**
`"click=preview  dbl-click=pin  ctrl-w=close  ctrl-tab=next"` (two spaces between
segments, matching this app's existing hint-string convention — see e.g. the
Plans panel's ` j/k=nav  Enter=detail  right-click=menu…` in ms-38 §4f).

**Testable:**
- With **at least one** doc tab open on the active panel,
  `driver.screen_contains("click=preview")` is `true`.
- With **zero** doc tabs open, that string is **absent** — the status bar falls
  back to the pre-ms-65 per-panel hint (Board: `" n=notify  m=merge  R=retry
  P=purge  q=quit "`, unchanged). See `mocks/board-baseline-no-tabs.screen`
  (absent) vs. `mocks/board-preview-tab.screen` (present).

### 8b. `?` help overlay

`mocks/board-tabs-help-overlay.screen` — title `"Board — Help"` (mirrors the
existing `"Plans — Help"` convention, ms-38 §5b), two sections: **Document
tabs** (click / double-click / Ctrl-W / Ctrl-Tab+Ctrl-Shift-Tab / middle-click /
right-click, one row each) and **Split** (§9's four keys). `Esc` closes it;
status bar while open is `" Esc=close "` (mirrors ms-38 §5i / §9i's identical
convention for Plans' own overlay — reuse the existing overlay chrome, do not
build a second one).

**Testable:**
- `driver.press('?')` while the Board panel is active → `driver.screen_contains("Board — Help")`.
- `driver.screen_contains("open preview tab")`, `driver.screen_contains("pin tab")`,
  `driver.screen_contains("close active tab")`, `driver.screen_contains("cycle tabs")`
  are all `true` while the overlay is open — each phrase chosen (per ms-38 §5c's
  own reasoning, quoted here because it is being reapplied) to **not** already
  appear in the status bar hint string in §8a, so the assertion cannot pass with
  the overlay closed.

### 8c. Right-click tab menu

`mocks/board-tab-context-menu.screen` — right-clicking a tab (not a sidebar row;
this is a **new** right-click target distinct from every existing context menu in
this app) opens: `"Close"`, `"Close others"`, `"Close all"`, `"Pin tab"` — exact
labels, taken verbatim from the tracking issue. `"Pin tab"` is hidden or inert
when the clicked tab is already pinned (not depicted in the mock — a dynamic
state the test-author drives, not a static fact).

**Testable:**
- `driver.screen_contains("Close others")` and `driver.screen_contains("Close all")`
  and `driver.screen_contains("Pin tab")` are all `true` after right-clicking an
  open tab.
- "Close others" leaves exactly the clicked tab open (count check, §4's pattern).
- "Close all" leaves none open — the strip disappears, same end state as §4's
  "closing the last tab."
- "Pin tab" on a preview tab drops its `∘ ` marker (§1) without changing tab
  count or order.

---

## 9. Side-by-side split (#2288)

`mocks/board-split-side-by-side.screen` — the Board panel's `main_content_bounds`
divides into two panes separated by a `║` (U+2551, double vertical — reused from
an existing mock's already-cleared glyph budget, ms-38 §7's list) at a fixed
column; **the panel toolbar row still spans the full panel width above both
panes** (toolbar is panel-scoped, not pane-scoped — only the doc-tab strip and
everything below it is duplicated per pane). Each pane owns its own doc-tab
strip, active tab and preview slot — the §2/§3 model applied one level deeper,
per-pane-within-scope rather than per-scope.

**Split-pane tab label truncation is a separate, narrower, pinned constant: 14
columns (16 for a preview tab, i.e. the same `+2` marker rule as §2b, just
applied to the smaller base) — not the §2b single-pane 20/22 budget.** At the
default 50/50 split of the 82-column main panel (minus 1 column for the `║`
divider itself), each pane gets roughly 40 columns of content width; 14/16 is
picked, following the same reasoning as Notes §10.4, to keep 2 tabs visible per
pane at that width without early truncation on the fixture's ~25-char titles.
This is **not** derived by halving 20/22 (which would be 10/11) — it is its own
pinned value, chosen for on-screen legibility, exactly as §2b's 20/22 is its own
pinned value rather than a derived one. `mocks/board-split-side-by-side.screen`
row 2 (the doc-tab strip row) is the truthful rendering of this budget: left
pane `"#101 Fix logi… ×  [#102 Auth tok… ×]"` (two 14-col labels) and right
pane `"[∘ #103 Race con… ×]"` (one 16-col preview label, `"∘ "` + 14-col base).

**Pinned key bindings** (the issue lists the *verbs* — split right, split down,
focus-pane movement, close pane — without pinning keys; this contract pins them,
following this app's existing `Ctrl-W`-as-pane-leader convention already shipped
for the Terminal panel, `"Ctrl-W h = side panel"` etc.):

| Key | Action |
|---|---|
| `Ctrl-W v` | split the focused pane right |
| `Ctrl-W s` | split the focused pane down *(reserved for a future non-side-by-side layout; ms-65 only ships side-by-side / `v`, see Notes §10.2)* |
| `Ctrl-W w` | move focus to the next pane |
| `Ctrl-W x` | close the focused pane (if it is not the last pane in the scope) |

**Testable:**
- `driver.screen_contains("║")` is `true` only when a panel has ≥2 panes; `false`
  on every mock in §§2–4 and §8 (single pane, the default).
- With a single pane, rendering is **byte-identical** to the non-split case — i.e.
  `mocks/board-pinned-3-tabs.screen` is exactly what one pane of a would-be split
  looks like; there is no separate "single-pane-but-split-capable" visual state.
- Closing the last remaining pane in a scope is a no-op (or disabled) — a scope
  always has ≥1 pane.
- The two panes' tab sets, active tabs and preview slots never merge — same
  independence proof pattern as §3, applied within one panel instead of across
  two.

---

## 10. Notes / open questions

1. **`SplitDirection` is inverted between quadraui and vimcode (#2288's own
   warning, repeated here because Gate A is exactly where a wrong guess should
   be caught).** This contract's `Ctrl-W v` = *panes side-by-side* is a
   **quadraui-`Horizontal`**-shaped split (`primitives/split.rs:40-49` per the
   issue body). The implementor must use quadraui's convention, not vimcode's,
   when calling `SplitTree` — get this backwards and the mock's left/right
   panes render as top/bottom instead, silently.

2. **§9's `Ctrl-W s` (split down) has no mock and is out of this milestone's
   shipped scope**, per the tracking issue's own "Out of scope: 2×2 quadrants…"
   line — ms-65 ships side-by-side (`v`) only. The key is pinned here (reserved,
   not yet bound to a working action) so a later milestone extending to a 2×2
   grid does not have to renegotiate the keybinding — this is a forward-
   compatibility placeholder, not a requirement on ms-65's workers. If this
   reads as scope creep, it is not: no acceptance test in this contract requires
   `Ctrl-W s` to do anything in ms-65.

3. **The `∘ ` preview marker (§1) is a mock-author design decision, not a
   pre-existing convention.** Flagging explicitly per this agent's own brief:
   if the operator or an implementor has a different preference for how preview
   state should read in a symbols-only grid (e.g. a different glyph, or a
   suffix instead of a prefix), that is exactly the kind of thing Gate A sign-
   off exists to catch **before** `coord acceptance author` writes tests against
   `∘ ` specifically — amend this contract, not the sealed suite, if so.

4. **Tab-strip label truncation width (20 / 22 columns, §2b) is this contract's
   choice, not derived from any existing constant in the codebase** (searched:
   no existing `TAB_LABEL_MAX` or similar). Picked to keep 3–4 tabs visible in
   the 82-column main panel without early truncation on short titles; revisit if
   real issue titles in this repo run meaningfully longer than the ~25-char
   fixture titles used here. **The split-pane variant (14 / 16 columns, §9) is
   a second, independently-picked constant for the same reason, scaled to each
   pane's roughly-40-column width rather than derived from the 20/22 value** —
   both are this contract's proposals, not codebase constants, and either may
   need revisiting together if real titles run longer.

5. **§6's `tabs.json` shape is likewise this contract's proposal**, not lifted
   from an existing file — `workspace.json` and `settings.toml` are each
   bespoke, so there was no single precedent shape to copy. If #2286's
   implementor finds a structural reason to deviate (e.g. flattening `active`/
   `preview` into the `tabs` array as boolean flags instead of separate keys),
   that is an amendment, not a silent implementation choice — the shape above
   is what `coord acceptance author` will write fixtures against.

6. **Issue #2285's "confirm a session follows its own tab" (Terminal
   sub-state) has no mock and is asserted entirely by behaviour (§5)** — a
   background PTY session is not something a static screen grid can show
   staying alive. Flagging so the test-author knows this section is
   intentionally prose-only, not an oversight.

7. **Work order gating (#2281 → #2282 → …) is unaffected by this contract** —
   Gate A pins the *shape* of the finished feature; the dependency chain in the
   tracking issue's work order is unchanged by anything here.

---

## 11. Mocks index

| File | Scenario | Issues covered |
|---|---|---|
| `mocks/board-baseline-no-tabs.screen` | Board panel, zero doc tabs open — the pre-ms-65 / regression-bar reference | §2a baseline, §4 empty-state target, §8a absent-hint reference |
| `mocks/board-preview-tab.screen` | Single click opens one preview tab (`∘` marker, bracketed active) | #2282 §2b–2e, §8a present-hint reference |
| `mocks/board-pinned-3-tabs.screen` | Headline: 3 pinned Board tabs, reveal-on-activate scrolls the sidebar | #2282 §2c/§2f, #2284 §3b (left half of the independence pair) |
| `mocks/pipeline-tabs-independent.screen` | Pipeline's own 2-tab set (disjoint issue numbers), no toolbar row so the strip is first | #2284 §3a/§3b (right half of the independence pair) |
| `mocks/board-tabs-overflow.screen` | 5 tabs, only 4 fit — `‹` overflow arrow (rightmost tab is the fixture's actual last tab, so `›` does not apply in this mock, see §4), active tab always visible | #2283 §4 |
| `mocks/board-tab-context-menu.screen` | Right-click a tab — Close / Close others / Close all / Pin tab | #2287 §8c |
| `mocks/board-tabs-help-overlay.screen` | `?` overlay — tab + split key bindings | #2287 §8b |
| `mocks/board-split-side-by-side.screen` | Panel split into two tab groups, `║` divider, shared toolbar | #2288 §9 |

All mocks: 120×40, symbols-only `.screen` grid (Ambiguous-width-1 metric, §0).
Column widths were machine-verified at generation time — every content line is
exactly 120 columns, no silent truncation.
