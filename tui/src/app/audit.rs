//! Audit ActivityBar panel (#1039).
//!
//! Newest-first list of the audit trail (`/audit`, #1037) with an inline
//! entry-detail view — modeled on the Plans panel (`plans.rs`): no
//! `SidebarSystem`/tree in the main panel, a simple `usize` selection index
//! clamped on navigation, and a background fetch gated to only run while
//! this panel is the active view (mirrors `spawn_artifact_fetch`'s
//! Pipeline-only gating in `data.rs`).
//!
//! **Read-only in this slice.** Filters (time-range `t`, category `Tab`)
//! are #1040 — out of scope here per the #1039 issue body.
//!
//! **#1094 update:** the main-panel row list was migrated from a joined-
//! string `ListView`/`draw_list` to a quadraui `DataTable`/`draw_data_table`
//! — real columns (Time/Category/Actor/Repo#Issue/Summary) with user-driven
//! column resize (drag a header divider). Column **sort** is explicitly
//! deferred (`/audit` is server-paginated newest-first; a client-side sort
//! would only reorder whatever page happens to be loaded — see the #1094
//! issue body). The detail pane below (`audit_detail_items`, a bordered
//! `ListView`) is unchanged by #1094 — this only touches the list pane.
//!
//! **Contract:** `tests/acceptance/ms-33/contract.md` (Gate A, mock-authored
//! before this issue dispatched) pins the exact panel registration, screen
//! strings, hint text, and `/audit` wire shape this module implements
//! against. The sealed acceptance slice (`tests/acceptance/ms-33/
//! audit_1039.rs`) currently covers only the no-seeding-required behaviours
//! (panel registration, sidebar header, empty state, list-mode hints); the
//! populated-list/detail-pane/badge assertions are deferred there pending a
//! seeding seam — see `app::fixtures::make_app_with_audit_json`. **#1094
//! note:** no Gate-A mock/contract amendment for #1094 itself has been
//! authored yet (see the durable finding on issue #1094) — this module's
//! populated-list rendering is covered only by the in-crate tests below.
#[allow(unused_imports)]
use super::*;

impl CoordApp {
    /// The cached audit entries, or an empty slice when nothing has been
    /// fetched yet (or the fetch found a genuinely empty log). Both cases
    /// render identically — contract §4b deliberately treats "not fetched
    /// yet" the same as "fetched, zero rows" so a slow first fetch never
    /// reads as broken.
    pub(crate) fn audit_entries(&self) -> &[AuditEntry] {
        self.audit_page
            .as_ref()
            .map(|p| p.entries.as_slice())
            .unwrap_or(&[])
    }

    /// Selected row index, clamped against the current entry count. `0` on
    /// an empty list (never read in that case — callers check emptiness
    /// first).
    pub(crate) fn audit_selected_idx(&self) -> usize {
        let n = self.audit_entries().len();
        if n == 0 {
            0
        } else {
            self.audit_sel.min(n - 1)
        }
    }

    /// The currently-selected audit entry, or `None` on an empty list.
    pub(crate) fn audit_selected(&self) -> Option<&AuditEntry> {
        self.audit_entries().get(self.audit_selected_idx())
    }

    /// Sidebar content (#1039 contract §3; #1040 contract §8/§9): the panel
    /// title (" AUDIT ", rendered by `ShellConfig.with_status_bar()` from
    /// `shell_config()`'s `PanelDefinition.title`, mirrored here on the
    /// `ListView` itself same as the other list-backed sidebars) plus an
    /// entry-count line
    /// (suffixed `" (filtered)"` — mock `audit-panel-filters.screen` —
    /// whenever any filter is non-default), an optional "N recent" badge
    /// sourced from `/board`'s `audit_recent_count` (kept separate from the
    /// cached page's own entry count per contract §3: "The count comes from
    /// the cached AuditPage ... not the /board audit_recent_count field"),
    /// and the current time-range / category / type-text filter selection.
    pub(crate) fn audit_sidebar(&self) -> ListView {
        let n = self.audit_entries().len();
        let count_line = format!(
            "  {n} entr{}{}",
            if n == 1 { "y" } else { "ies" },
            if self.audit_filters_active() { " (filtered)" } else { "" },
        );
        let mut items = vec![activity_item(&count_line, Color::rgb(160, 160, 160))];
        let recent = self.audit_recent_count();
        if recent > 0 {
            items.push(activity_item(
                &format!("  {recent} recent"),
                Color::rgb(120, 210, 120),
            ));
        }
        // #1040 contract §8/§9: current filter selection is always shown
        // (not only while non-default) so the `t`/`Tab` affordance is
        // discoverable before the operator has touched either filter.
        items.push(activity_item(
            &format!("  Time: {}", self.audit_time_range.label()),
            Color::rgb(150, 180, 220),
        ));
        items.push(activity_item(
            &format!("  Category: {}", self.audit_category.label()),
            Color::rgb(150, 180, 220),
        ));
        // #2653: tier selection, cycled by `T` — shown unconditionally, same
        // discoverability reasoning as Time/Category just above (visible
        // before the operator has touched it, not only while non-default).
        items.push(activity_item(
            &format!("  Tier: {}", self.audit_tier.label()),
            Color::rgb(150, 180, 220),
        ));
        // #1040 deliverable 1 / contract §10 ("f=filter"): free-text filter
        // on `/audit`'s `type` param, reusing `SidebarFilter`'s state (same
        // struct Board/Pipeline embed) but rendered as a plain row here —
        // this sidebar is a bare `ListView`, not a `SidebarSystem`-backed
        // tree/form like Board/Pipeline's own embedded filter. A trailing
        // block cursor stands in for a real text-input caret while focused.
        let type_line = if self.audit_type_filter.focused {
            format!("  Type: {}\u{2588}", self.audit_type_filter.query)
        } else if !self.audit_type_filter.is_empty() {
            format!("  Type: {}", self.audit_type_filter.query)
        } else {
            "  Type: (f to filter)".to_string()
        };
        items.push(activity_item(&type_line, Color::rgb(150, 180, 220)));
        ListView {
            id: WidgetId::new("audit-sidebar"),
            title: Some(StyledText::plain(" AUDIT ")),
            items,
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Count of audit rows in the last 15 minutes (`/board`'s
    /// `audit_recent_count`, #1037) — backs both the sidebar's "N recent"
    /// line above and the always-visible status-bar attention badge (#1090,
    /// #1039 deliverable #7, `mod.rs`'s `status_bar()`). Mirrors
    /// `plans_needing_attention_count`'s role as the single source both
    /// readers pull from, so the two can never drift out of sync.
    pub(crate) fn audit_recent_count(&self) -> u64 {
        self.data.audit_recent_count
    }

    /// #1040/#2653: `true` when any Audit filter differs from its default
    /// ("All" time-range, "all" category, "business" tier, empty type text)
    /// — drives the sidebar's `" (filtered)"` count-line suffix (mock
    /// `audit-panel-filters.screen`).
    ///
    /// `audit_tier`'s own default is `Business`, not "no filter" like the
    /// other three — deliberately: #2653 makes `Business` the default
    /// *view*, not a filter the operator applied, so it must NOT pin
    /// `" (filtered)"` on permanently (that would make the suffix
    /// meaningless — it'd show on literally every fresh session). Comparing
    /// against `AuditTier::Business` (the default) rather than
    /// `AuditTier::All` (the "everything" value the other two compare
    /// against) is what keeps that distinction: the suffix now means "you
    /// changed something from its default", exactly as it does for
    /// time-range/category/type, with tier's default just being a
    /// non-`All` value instead of `All`.
    pub(crate) fn audit_filters_active(&self) -> bool {
        self.audit_time_range != AuditTimeRange::All
            || self.audit_category != AuditCategory::All
            || self.audit_tier != AuditTier::default()
            || !self.audit_type_filter.is_empty()
    }

    /// #1094 column set for the main-panel `DataTable` (issue body "Column
    /// widths (decided)"). Widths are `Fixed` for the closed-vocabulary
    /// columns (Category: `dispatch/test/review/merge/override/plan/error`,
    /// max 8 chars; Actor: `coordinator/daemon/worker/user`, max 11 chars)
    /// plus Time (relative-time string, no benefit from flexing) — `Content`
    /// is deliberately NOT used here even though it looks tempting, because
    /// every rasteriser's `Content{min,max}` measurer only sizes off the
    /// column *title*'s character count, never row data (verified across
    /// `tui/`, `gtk/`, `macos/data_table.rs`), so it would just collapse to
    /// `min` for every one of these titles. Repo#Issue and Summary are
    /// `Flex` (genuinely unbounded width, no small vocabulary) so Summary —
    /// the primary content — gets the lion's share of whatever's left after
    /// the three fixed columns.
    fn audit_columns() -> Vec<Column> {
        vec![
            Column {
                title: "Time".to_string(),
                width: ColumnWidth::Fixed(11.0),
                align: ColumnAlign::Left,
            },
            Column {
                title: "Category".to_string(),
                width: ColumnWidth::Fixed(9.0),
                align: ColumnAlign::Left,
            },
            Column {
                title: "Actor".to_string(),
                width: ColumnWidth::Fixed(12.0),
                align: ColumnAlign::Left,
            },
            Column {
                title: "Repo#Issue".to_string(),
                width: ColumnWidth::Flex(1.0),
                align: ColumnAlign::Left,
            },
            Column {
                title: "Summary".to_string(),
                width: ColumnWidth::Flex(3.0),
                align: ColumnAlign::Left,
            },
        ]
    }

    /// Minimum total table width (#1094 "Narrow-terminal capacity"): the
    /// three fixed columns alone are 11+9+12 = 32 cells, and the sidebar +
    /// activity-bar overhead (~38 cols) leaves as little as ~42 cols of
    /// main-panel width at a plain 80-col terminal. Below this floor,
    /// `DataTable`'s built-in horizontal scrollbar takes over instead of the
    /// columns (and their text) being squeezed below legible widths.
    const AUDIT_TABLE_MIN_WIDTH: f32 = 75.0;

    /// `repo#issue` cell text — unchanged from the pre-#1094 joined-string
    /// format (contract §4a); still a single combined cell, not split into
    /// two columns (matches what #1039/#1040 already shipped and the sealed
    /// contract's existing mocks).
    fn audit_repo_issue(entry: &AuditEntry) -> String {
        match (&entry.repo, entry.issue) {
            (Some(repo), Some(n)) => format!("{repo}#{n}"),
            (Some(repo), None) => repo.clone(),
            (None, Some(n)) => format!("#{n}"),
            (None, None) => String::new(),
        }
    }

    /// One styled cell — same foreground colour every row used under the
    /// old joined-string `ListItem` rendering.
    fn audit_cell(text: impl Into<String>) -> StyledText {
        StyledText {
            spans: vec![StyledSpan::with_fg(text.into(), Color::rgb(200, 200, 200))],
        }
    }

    /// Build the `DataTable` row list for the populated main panel. No
    /// manual truncation here (contract §4a's old `trunc(&entry.summary,
    /// 60)` is dropped, #1094 deliverable 5) — `draw_data_table` already
    /// clips each cell to its resolved column width.
    fn audit_data_rows(entries: &[AuditEntry]) -> Vec<DataRow> {
        entries
            .iter()
            .map(|entry| DataRow {
                cells: vec![
                    Self::audit_cell(format_unix_time(entry.ts)),
                    Self::audit_cell(entry.category.clone()),
                    Self::audit_cell(entry.actor.clone()),
                    Self::audit_cell(Self::audit_repo_issue(entry)),
                    Self::audit_cell(entry.summary.clone()),
                ],
                decoration: Decoration::Normal,
            })
            .collect()
    }

    /// Inline entry-detail pane content (contract §4c): a header line plus
    /// one key/value row per required field. `details` renders as its raw
    /// JSON (`"{}"` when absent/null).
    fn audit_detail_items(entry: &AuditEntry) -> Vec<ListItem> {
        let details_str = entry
            .details
            .as_ref()
            .map(|v| v.to_string())
            .unwrap_or_else(|| "{}".to_string());
        let mut items = vec![
            kv_item("", " Entry Detail", Some(Color::rgb(230, 230, 255))),
            kv_item("id:", &entry.id.to_string(), None),
            kv_item("ts:", &entry.ts.to_string(), None),
            kv_item("category:", &entry.category, None),
            kv_item("event_type:", &entry.event_type, None),
            kv_item("actor:", &entry.actor, None),
        ];
        // The remaining wire fields (contract §6) are optional in practice
        // (e.g. a `plan`/`error` category entry may have no repo/issue) —
        // shown only when present, rather than as an always-blank row.
        if let Some(tier) = &entry.tier {
            items.push(kv_item("tier:", tier, None));
        }
        if let Some(assignment_id) = &entry.assignment_id {
            items.push(kv_item("assignment:", assignment_id, None));
        }
        if let Some(machine) = &entry.machine {
            items.push(kv_item("machine:", machine, None));
        }
        items.push(kv_item("summary:", &entry.summary, None));
        items.push(kv_item("details:", &details_str, None));
        items
    }

    /// Render the Audit main panel (#1039): empty state, populated list, or
    /// list-plus-inline-detail split when `audit_detail_open`.
    pub(crate) fn render_audit_panel(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        let entries = self.audit_entries();
        if entries.is_empty() {
            // Contract §4b — treated identically whether the fetch hasn't
            // completed yet or genuinely returned zero rows: the message
            // always STARTS with the exact contract-required string. When
            // the most recent fetch actually failed, or resolved to "no
            // board service configured" (#1039 review fix — previously
            // indistinguishable from a genuinely empty page, which made a
            // live-daemon smoke-test failure impossible to diagnose from
            // the UI alone), a qualifier is appended.
            let message = if let Some(reason) = &self.audit_fetch_error {
                format!("  No audit events yet.  (last fetch failed: {reason})")
            } else if self.audit_no_service {
                "  No audit events yet.  (no board service configured)".to_string()
            } else {
                "  No audit events yet.".to_string()
            };
            backend.draw_list(rect, &plain_list("audit-empty", &message, 0));
            return;
        }

        let sel = self.audit_selected_idx();
        let (list_rect, detail_rect) = if self.audit_detail_open {
            // Reserve roughly the bottom 40% (at least 7 rows: header + 6
            // fields) for the detail pane, the rest for the list above it.
            let min_detail_h = (lh * 7.0).min(rect.height);
            let detail_h = (rect.height * 0.4).max(min_detail_h).min(rect.height);
            let list_h = (rect.height - detail_h).max(0.0);
            let list_rect = Rect::new(rect.x, rect.y, rect.width, list_h);
            let detail_rect = Rect::new(rect.x, rect.y + list_h, rect.width, rect.height - list_h);
            (list_rect, Some(detail_rect))
        } else {
            (rect, None)
        };

        // #1094: the main-panel row list is a `DataTable` (was a plain
        // `ListView`) — real columns with user-driven resize. The resolved
        // layout is cached in `audit_table_layout` so mouse hit-testing
        // (`audit_table_hit`, called from `events.rs` with no `Backend`
        // handle in scope) can reuse the exact geometry that was painted,
        // same render-then-hit-test pattern `kanban_layout` already uses.
        let table = DataTable {
            id: WidgetId::new("audit-list"),
            columns: Self::audit_columns(),
            rows: Self::audit_data_rows(entries),
            selected_idx: Some(sel),
            // #1094 fix: was hardcoded to `0`/`0.0` — the table could never
            // actually scroll (see the #1094 fix-iteration-1 durable
            // finding). `audit_scroll`/`audit_h_scroll` are kept in sync
            // with `audit_sel` and scrollbar drags by `fix_audit_scroll` /
            // `audit_apply_vscroll` / `audit_apply_hscroll` (events.rs).
            scroll_offset: self.audit_scroll,
            sort: None,
            has_focus: true,
            show_scrollbar: true,
            min_total_width: Some(Self::AUDIT_TABLE_MIN_WIDTH),
            h_scroll: self.audit_h_scroll,
            column_overrides: self.audit_column_overrides.clone(),
            // #1094: the audit table has no pinned summary row.
            footer: None,
        };
        let layout = backend.draw_data_table(list_rect, &table, None);
        *self.audit_table_layout.borrow_mut() = Some(layout);

        if let Some(detail_rect) = detail_rect {
            if let Some(entry) = self.audit_selected() {
                backend.draw_list(
                    detail_rect,
                    &ListView {
                        id: WidgetId::new("audit-detail"),
                        title: None,
                        items: Self::audit_detail_items(entry),
                        selected_idx: 0,
                        scroll_offset: 0,
                        has_focus: false,
                        bordered: true,
                        h_scroll: 0,
                        max_content_width: None,
                        show_v_scrollbar: false,
                    },
                );
            }
        }
    }

    /// #1094: hit-test a click position against the last-rendered
    /// `DataTable` layout (`audit_table_layout`, cached by
    /// `render_audit_panel` — same render-then-hit-test pattern
    /// `kanban_layout`/`BoardLayout::hit_test` already use elsewhere in this
    /// crate). `None` when the list is empty, or nothing was cached yet
    /// (no render since navigating to the panel — shouldn't happen in
    /// practice since a render always precedes a click, but a stale click
    /// should fail closed rather than hit-test against a missing layout).
    /// The caller only invokes this in list-only mode — the table isn't
    /// rendered at all while `audit_detail_open` (see `render_audit_panel`),
    /// so there is no layout to hit-test against in that state either way.
    pub(crate) fn audit_table_hit(&self, pos: Point, main_b: Rect) -> Option<DataTableHit> {
        let n = self.audit_entries().len();
        if n == 0 {
            return None;
        }
        let layout_ref = self.audit_table_layout.borrow();
        let layout = layout_ref.as_ref()?;
        let x = pos.x - main_b.x;
        let y = pos.y - main_b.y;
        // `audit_scroll` matches what `render_audit_panel` builds the table
        // with — must be passed here too (#1094 fix) so a click while
        // scrolled resolves to the right absolute row index rather than
        // always assuming `scroll_offset == 0`.
        Some(layout.hit_test(x, y, self.audit_scroll, n))
    }

    /// #1094 fix: pre-check a click/drag position against the Audit table's
    /// scrollbar strips, using the same geometry the TUI rasteriser paints
    /// them at (`quadraui::tui::data_table::draw_data_table`: the vertical
    /// track occupies the rightmost `scrollbar_width` columns below the
    /// header row; the horizontal track occupies the bottom
    /// `h_scrollbar_height` row(s), left of the vertical track).
    ///
    /// `DataTableLayout::hit_test` (quadraui) has no concept of these strips
    /// at all — a click there falls through to whatever row/header region
    /// happens to be under the cursor (the #1094 fix-iteration-1 durable
    /// finding: "hit-testing appears to route straight to
    /// `DataTableHit::Row`"). Callers must check this *before*
    /// `audit_table_hit` so a scrollbar click never reaches row selection.
    pub(crate) fn audit_scrollbar_hit(&self, pos: Point, main_b: Rect) -> Option<AuditScrollAxis> {
        let layout_ref = self.audit_table_layout.borrow();
        let layout = layout_ref.as_ref()?;
        let x = pos.x - main_b.x;
        let y = pos.y - main_b.y;
        if x < 0.0 || y < 0.0 || x >= layout.viewport_width || y >= layout.viewport_height {
            return None;
        }
        // Vertical scrollbar takes priority in the bottom-right corner,
        // matching `hit_test`'s own divider-before-header priority style.
        if layout.scrollbar_width > 0.0 {
            let sb_x0 = layout.viewport_width - layout.scrollbar_width;
            if x >= sb_x0 && y >= layout.header_height {
                return Some(AuditScrollAxis::Vertical);
            }
        }
        if layout.h_scrollbar_height > 0.0 {
            let hsb_y0 = layout.viewport_height - layout.h_scrollbar_height;
            if y >= hsb_y0 {
                return Some(AuditScrollAxis::Horizontal);
            }
        }
        None
    }

    /// #1094 fix: jump `audit_scroll` to the row implied by a click/drag
    /// position along the vertical scrollbar's track — standard
    /// click/drag-to-position scrollbar behaviour (not thumb-relative
    /// dragging). No-op when there's nothing to scroll (empty list, or the
    /// cached layout is stale/missing).
    pub(crate) fn audit_apply_vscroll(&mut self, pos: Point, main_b: Rect) -> bool {
        let n = self.audit_entries().len();
        if n == 0 {
            return false;
        }
        let (track_y0, track_h, visible_rows) = {
            let layout_ref = self.audit_table_layout.borrow();
            let Some(layout) = layout_ref.as_ref() else {
                return false;
            };
            let track_y0 = main_b.y + layout.header_height;
            let track_h = (layout.viewport_height
                - layout.header_height
                - layout.h_scrollbar_height)
                .max(1.0);
            (track_y0, track_h, layout.visible_rows.max(1))
        };
        let max_scroll = n.saturating_sub(visible_rows);
        self.audit_scroll = if max_scroll == 0 {
            0
        } else {
            let frac = ((pos.y - track_y0) / track_h).clamp(0.0, 1.0);
            (frac * max_scroll as f32).round() as usize
        };
        true
    }

    /// #1094 fix: same as `audit_apply_vscroll` but for the horizontal
    /// scrollbar — jumps `audit_h_scroll` to the column offset implied by
    /// the click/drag position along the horizontal track.
    pub(crate) fn audit_apply_hscroll(&mut self, pos: Point, main_b: Rect) -> bool {
        let (track_x0, track_w, content_w, visible_w) = {
            let layout_ref = self.audit_table_layout.borrow();
            let Some(layout) = layout_ref.as_ref() else {
                return false;
            };
            let visible_w = (layout.viewport_width - layout.scrollbar_width).max(1.0);
            (main_b.x, visible_w, layout.content_width, visible_w)
        };
        let max_scroll = (content_w - visible_w).max(0.0);
        self.audit_h_scroll = if max_scroll <= 0.0 {
            0.0
        } else {
            let frac = ((pos.x - track_x0) / track_w).clamp(0.0, 1.0);
            frac * max_scroll
        };
        true
    }

    /// #1094 fix: keep `audit_sel` inside the visible window, same
    /// structural pattern as `fix_machine_scroll` (`mod.rs`). Must be called
    /// after every keyboard nav that moves `audit_sel`
    /// (`j`/`k`/`Down`/`Up`/`Home`/`End` in events.rs) — the table has no
    /// concept of "scroll to keep selection visible" on its own; that was
    /// the root cause of the fix-iteration-1 "no way to reach rows beyond
    /// the first screenful" report.
    pub(crate) fn fix_audit_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        let sel = self.audit_selected_idx();
        if sel < self.audit_scroll {
            self.audit_scroll = sel;
        } else if sel >= self.audit_scroll + visible {
            self.audit_scroll = sel + 1 - visible;
        }
    }

    /// Minimum width (cells) a column may be dragged down to — keeps a
    /// resize drag from collapsing a column to zero/negative width.
    const AUDIT_MIN_COLUMN_WIDTH: f32 = 4.0;

    /// #1094 deliverable 4: continue an in-progress column-resize drag on
    /// the Audit `DataTable`, started by a `MouseDown` on a
    /// `DataTableHit::HeaderDivider` (`audit_resize_col` set by the caller —
    /// see `mouse_main_click` in `events.rs`). Computes the new width for
    /// the dragged column from the cursor's current x position relative to
    /// that column's left edge (per the last-cached `audit_table_layout`)
    /// and stores it in `audit_column_overrides` — session-only
    /// persistence, matching how the panel's filters and scroll position
    /// already work (no cross-restart UI-state store exists in this
    /// codebase yet). Returns `true` (redraw needed) only while a drag is
    /// actually in progress and the cached layout still has that column.
    pub(crate) fn audit_update_resize_drag(&mut self, pos: Point, main_b: Rect) -> bool {
        let Some(col) = self.audit_resize_col else {
            return false;
        };
        let col_x = {
            let layout_ref = self.audit_table_layout.borrow();
            match layout_ref.as_ref().and_then(|l| l.columns.get(col)) {
                Some(rc) => rc.x,
                None => return false,
            }
        };
        let x = pos.x - main_b.x;
        let new_w = (x - col_x).max(Self::AUDIT_MIN_COLUMN_WIDTH);
        if let Some(slot) = self.audit_column_overrides.get_mut(col) {
            *slot = Some(new_w);
        }
        true
    }

    /// Force the next `run_periodic_work` tick to re-fetch `/audit`
    /// immediately, dropping any in-flight request (`r` — contract §7; also
    /// the mechanism `on_audit_filters_changed` re-arms on a filter edit).
    pub(crate) fn refresh_audit(&mut self) {
        self.audit_fetch_rx = None;
        self.audit_last_fetched = None;
    }

    /// #1040 contract §11: apply a just-changed filter (time-range,
    /// category, or the free-text type field) — reset the row selection
    /// and close any open detail pane (the previously-selected row may not
    /// exist in the new, differently-filtered result set), then re-arm the
    /// fetch. "Reset cursor to None" (contract §11 step 1) is implicit:
    /// `spawn_audit_fetch` always requests the first page, so there is no
    /// persisted cursor on `CoordApp` to explicitly clear.
    pub(crate) fn on_audit_filters_changed(&mut self) {
        self.audit_sel = 0;
        // #1094 fix: a differently-filtered result set invalidates whatever
        // window `audit_scroll`/`audit_h_scroll` were pointed at — reset
        // both alongside `audit_sel` so the panel reopens scrolled to the
        // top-left instead of a stale offset into the old row set.
        self.audit_scroll = 0;
        self.audit_h_scroll = 0.0;
        self.audit_detail_open = false;
        self.refresh_audit();
    }
}
