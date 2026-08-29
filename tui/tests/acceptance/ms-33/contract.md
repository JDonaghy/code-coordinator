# Gate A Contract — ms-33: Audit Trail TUI

_Mock-authored 2026-07-10 for milestone #33 (tracking issue #1041)._
_Issues in scope: **#1039** (Audit panel) · **#1040** (filters).  
Issues #1036–#1038 and #1042 are backend-only; no UI surface, excluded._

---

## 1. Panel registration (shell_config)

| Field | Value |
|---|---|
| `PanelDefinition.id` | `"panel:audit"` |
| `PanelDefinition.icon` | `"§"` |
| `PanelDefinition.tooltip` | `"Audit"` |
| `PanelDefinition.title` | `"AUDIT"` |
| Position in activity bar | After `"panel:sessions"` (◉), before the bottom-pinned `"panel:settings"` (⚙) |
| `SidebarView` variant | `SidebarView::Audit` |
| `SidebarView::label()` arm | `"Audit"` |
| `panel_widget_id()` arm | `Some(WidgetId::new("panel:audit"))` |

**Testable:** `shell_config()` panels list contains a `PanelDefinition` whose `id.as_str() == "panel:audit"` and `icon == "§"`.

---

## 2. Activity bar — rendered text (#1039)

The `§` character **must appear** in the rendered screen when the Audit panel is active.  
`driver.find("§")` must return `Some((x, y))` with `x < 3.0` (within the activity bar columns 0–2).

When Audit is the active panel:
- Col 0 of the Audit row: `▎` (accent bar, U+258E)
- Col 1: `§` (icon)
- Col 2: `│` (separator)

When Audit is inactive: col 0 is a space, col 1 is `§`, col 2 is `│`.

---

## 3. Sidebar content — key screen strings (#1039)

The sidebar header (row 0 of the sidebar, cols 3–37) shows: **` AUDIT `** (the panel title, exactly as rendered by `ShellConfig.with_status_bar()` from `title: "AUDIT".into()`).

**Required:** `driver.screen_contains(" AUDIT ")` is `true` when `active_view == SidebarView::Audit`.

The sidebar content area shows:

| Line | Required to contain |
|---|---|
| Count line | A decimal integer immediately followed by `" entries"` (e.g., `"42 entries"`) |
| Recent badge | A decimal integer followed by `" recent"` (e.g., `"7 recent"`) when `audit_recent_count > 0` from `/board` |
| Recent badge absent | Line omitted or shows `"0 recent"` when `audit_recent_count == 0` |

The count comes from the cached `AuditPage` (fetched via `spawn_audit_fetch`); it is **not** the `/board` `audit_recent_count` field (that drives only the badge).

---

## 4. Main panel — audit list rows (#1039)

### 4a. Populated state

When `AuditPage.entries` is non-empty, each row in the main content area renders:

```
  <time_ago>  <category>  <actor>  <repo>#<issue>  <summary>
```

Exact column semantics:

| Field | Source | Format | Required to appear |
|---|---|---|---|
| `time_ago` | `entry.ts` via `format_unix_time()` | `"Xs"` / `"Xm"` / `"XhYm ago"` | yes |
| `category` | `entry.category` | verbatim string | yes, value `"dispatch"` / `"test"` / `"review"` / `"merge"` / `"override"` / `"plan"` / `"error"` |
| `actor` | `entry.actor` | verbatim string | yes, value `"coordinator"` / `"user"` / `"worker"` / `"daemon"` |
| `repo#issue` | `entry.repo` + `entry.issue` | `"<repo>#<number>"` or issue number only when repo is absent | yes |
| `summary` | `entry.summary` | verbatim, truncated with `…` to fit column | yes |

**Testable strings** (must appear for a board seeded with the mock data):
- `"dispatch"` — at least one entry of this category
- `"test"` — at least one entry
- `"review"` — at least one entry
- `"merge"` — at least one entry
- `"coordinator"` — actor string
- `"ago"` — substring of every relative-time field

Ordering: **newest first** (highest `ts` at the top of the list). This is invariant.

### 4b. Empty state (#1039 edge case)

When `AuditPage.entries` is empty (or the fetch has not yet completed):

```
  No audit events yet.
```

**Required:** `driver.screen_contains("No audit events yet.")` is `true` for an empty-board fixture.

The empty-state message is rendered in the main content rect (not the sidebar).

### 4c. Entry detail pane (#1039)

Pressing **Enter** on a selected entry opens an inline detail view within the main content area (a horizontal split: list above, detail below). The detail pane shows the full entry fields, including the JSON-decoded `details` object.

**Required strings in the detail pane:**
- `"Entry Detail"` — section header
- `"ts:"` — raw timestamp field label
- `"category:"` — category field label
- `"event_type:"` — event_type field label
- `"actor:"` — actor field label
- `"summary:"` — summary field label
- `"details:"` — details JSON field label (may be absent / `"{}"` when `details` is null)

**Esc** closes the detail pane and returns to the list-only view.

---

## 5. Background fetch — `spawn_audit_fetch` (#1039)

- The fetch is **armed only while `active_view == SidebarView::Audit`**.
- Navigating away stops/drops the receiver; navigating back re-arms.
- The fetch hits `GET /audit` with no filters (first page, default limit).
- The fetched `AuditPage` is cached on `CoordApp` as `audit_page: Option<AuditPage>` — **resolved 2026-07-12 (#1095, amendment, was "TBD by implementor")**. `AuditPage`/`AuditEntry` (`tui/src/app/types.rs`) deserialize verbatim from the contract §6 wire shape.

**Test-support seeding seam (pinned, #1095):** `coord_tui::fixtures::make_app_with_audit_json(data: BoardData, audit_json: &str) -> CoordApp` (feature `test-support`, same visibility/reachability as `make_test_app`) builds a `CoordApp` with `audit_page` pre-populated by deserializing `audit_json` (a raw §6-shaped `/audit` response body) — no live daemon, no background fetch thread. Malformed JSON is a silent no-op (`audit_page` stays `None`). This is the seam for the §3 (count/badge), §4a (populated list), §4c (entry-detail), and §7-detail-mode acceptance assertions that were deferred pending this resolution — a test-author extending `tests/acceptance/ms-33/audit_1039.rs` for issue #1039 should use it directly rather than treating the seam as unresolved.

---

## 6. `/audit` endpoint wire shape

`GET /audit` query params accepted by `serve_app.py`:

| Param | Type | Meaning |
|---|---|---|
| `since` | float or ISO-8601 | lower bound on `ts` |
| `until` | float or ISO-8601 | upper bound on `ts` |
| `type` | string | filter on `event_type` |
| `category` | string | filter on `category` |
| `repo` | string | filter on repo |
| `issue` | integer | filter on issue number |
| `assignment` | string | filter on `assignment_id` |
| `tier` | string | filter on tier (`"business"` / `"operational"`) |
| `limit` | integer | page size (1–500, default 200) |
| `cursor` | opaque string | keyset cursor for next page |

Response shape:
```json
{
  "entries": [
    {
      "id":            1,
      "ts":            1752156191.0,
      "tier":          "business",
      "category":      "dispatch",
      "event_type":    "dispatched",
      "actor":         "coordinator",
      "repo":          "claude-coordinator",
      "issue":         1039,
      "assignment_id": "a1b2c3d4",
      "machine":       "laptop",
      "summary":       "Dispatched work to laptop: …",
      "details":       { "branch": "issue-1039-fix-filters", "type": "work" }
    }
  ],
  "next_cursor": "1752156191.0:1",
  "has_more": false
}
```

`entries` is ordered **newest first** (`ts DESC, id DESC`). `details` is `null` when the original `details_json` column is NULL.

The `/board` payload carries `audit_recent_count` (integer, count of rows in the last 15 minutes) for the sidebar badge — **not** a full page.

---

## 7. Status bar hints (#1039)

When `active_view == SidebarView::Audit` and no detail pane is open:

**Required:** `driver.screen_contains("j/k=nav")` is `true`.  
**Required:** `driver.screen_contains("Enter=detail")` is `true`.  
**Required:** `driver.screen_contains("r=refresh")` is `true`.  
**Required:** `driver.screen_contains("q=quit")` is `true`.

When the detail pane is open:

**Required:** `driver.screen_contains("Esc=close detail")` is `true`.  
`"Enter=detail"` may or may not remain.

---

## 8. Filter controls — time-range picker (#1040)

A **time-range** enum, cycled with the **`t`** key. Possible values (exact strings used in display and wired to `/audit` params):

| Display label | `/audit` params |
|---|---|
| `"Last hour"` | `since = now - 3600` |
| `"Today"` | `since = start_of_today_epoch` |
| `"7d"` | `since = now - 604800` |
| `"All"` | no `since`/`until` params |

Default: `"All"`.

**Required when filter is active:**
- The current time-range value appears in the sidebar (e.g., `"Today"`) or status bar.
- `driver.screen_contains("Today")` is `true` when `"Today"` is selected.
- `driver.screen_contains("Last hour")` is `true` when `"Last hour"` is selected.

The time-range selection updates the `/audit` query params and re-arms `spawn_audit_fetch` (resets the cursor).

---

## 9. Filter controls — category picker (#1040)

A **category** filter, navigated with the **Tab** key (cycles through values) or typed via a `SidebarFilter`-style text input. Possible enum values:

`"all"` · `"dispatch"` · `"test"` · `"review"` · `"merge"` · `"override"` · `"plan"` · `"error"`

Default: `"all"` (no category filter applied).

When a non-`"all"` category is selected, `category=<value>` is added to the `/audit` request.

**Required when category filter is active:**
- `driver.screen_contains("dispatch")` is `true` when dispatch is the selected category (the label must appear in the sidebar or status bar).

---

## 10. Filter status bar hints (#1040)

When `active_view == SidebarView::Audit` and filters are available:

**Required:** `driver.screen_contains("t=time-range")` is `true`.  
**Required:** `driver.screen_contains("Tab=category")` is `true`.  
**Required:** `driver.screen_contains("Esc=clear")` is `true` (clears active filters back to defaults).

The current time-range selection appears inline in the status bar hint, e.g., `"t=time-range (Today)"`.

---

## 11. Filter refresh semantics (#1040)

Changing either filter (time-range or category) triggers:
1. Reset cursor to `None` (start from newest).
2. Re-arm `spawn_audit_fetch` with the updated params.
3. Replace the cached `AuditPage` with the new result on receipt.

The current selection is preserved across panel navigations away and back (state persists on `CoordApp`).

---

## 12. Mocks index

| File | Scenario | Issues tested |
|---|---|---|
| `mocks/audit-panel-populated.screen` | Audit panel active, 6 entries visible, no detail open | #1039 (happy path) |
| `mocks/audit-panel-detail.screen` | Entry 0 selected, inline detail pane showing all fields | #1039 (detail view) |
| `mocks/audit-panel-empty.screen` | Panel active, zero entries | #1039 (empty state) |
| `mocks/audit-panel-filters.screen` | Filters active: Time=Today, Category=dispatch, 3 filtered entries | #1040 (filter UI) |

All mocks: 120 × 40 terminal, `driver_with_shell(app, CoordApp::shell_config(), 120, 40)`.

---

## Notes / open questions

1. **Icon choice `§` is a contract proposal.** The issue body (#1039) does not specify the icon. If the implementor chooses a different icon, the contract must be amended before acceptance tests are authored.

2. **Detail pane layout is a proposal.** The exact split ratio (rows occupied by list vs. detail) is an implementation detail; the contract pins only the *required strings* in the detail pane, not their row positions.

3. **`SidebarFilter` vs. enum-only for category (#1040).** The issue body allows either a `SidebarFilter` text input or a pure enum-tab. The contract pins the *displayed strings* and the *status-bar hint text*, not the exact widget type. If the free-text SidebarFilter is chosen, `"f=filter"` must appear in the status bar hints alongside `"Tab=category"`.

4. **Time-range display location (#1040).** The mock shows the current selection in both the sidebar and the status bar. The contract requires it appears *somewhere* visible; location is implementor's choice. The status bar variant (`"t=time-range (Today)"`) is the minimum required string.

5. **Backfill / pre-deploy entries.** Per the epic: no backfill, starts fresh. An empty state on first deploy is expected and tested.

6. **§5 seam amendment (2026-07-12, #1095).** #1039 landed `audit_page: Option<AuditPage>` on `CoordApp` plus the test-support seeding helper `coord_tui::fixtures::make_app_with_audit_json(data, audit_json)` — exactly the shape this contract originally left "TBD by implementor". A JIT test-author round (#1095) declined to use it without an explicit contract pin, reasonably reading §5's original wording as reserving the seam to the implementor. §5 above now pins it; the §3/§4a/§4c/§7-detail assertions deferred in `audit_1039.rs`'s TODO block should be authored against it in the next JIT round.
