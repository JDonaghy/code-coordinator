//! Approved work items ActivityBar panel (#2532, ms-67 contract §3).
//!
//! A plain aligned-column list of portal submissions ready for
//! decomposition into coordinator work — today `signoff.approved`
//! submissions (once `coord-portal`#132's operator "start work" override
//! lands, that widens too, but that is a wire-shape amendment, not a client
//! change). Modeled on the Audit panel's own list, not the bordered
//! `DataTable` grid Queue uses — no sortable numeric columns and no
//! drag-reorder verb here (contract §3c).
//!
//! **No fetch of its own.** Like Queue, rows ride the existing `/board`
//! poll (`BoardData::approved_submissions`, server-computed by
//! `coord/approved_work.py` and injected into the projection by
//! `coord/serve_app.py`'s board builder — `repos` is resolved there from
//! `portal.project_repos` in `coordinator.yml`, so this module never reads
//! config directly). See `SidebarView::Approved`'s doc comment for why the #2336
//! daemon-host guard that gates `coord portal outbox/events` does not apply
//! here: this panel only ever talks to `/board` over HTTP, same as every
//! other panel in this app.
//!
//! **Detail pane (contract §3e).** Enter opens an inline detail region
//! below the list, headed by a manually-authored `"── Submission Detail
//! ──"` divider line (NOT `ListView::bordered`'s rounded-box title overlay
//! — quadraui's `draw_list` renders that as `"╭─ Title ─────╮"`, a single
//! dash before the title rather than the two the contract's convention
//! pins verbatim, so reusing it would silently drift from the contract).
//! Long field values (outcome/audience/done/constraints) word-wrap with
//! continuation lines indented to the value column (13 chars, contract
//! §3e / §6.2) — the wrap width is derived from the actual painted detail
//! rect so a value's tail is never clipped by the renderer regardless of
//! terminal width.
#[allow(unused_imports)]
use super::*;
use super::doc_tabs::truncate_with_ellipsis;

/// Contract §3c's pinned column widths (display columns, not derived from
/// any existing constant — same posture ms-65 §2b/§9 took for its own
/// truncation widths).
const APPROVED_COL_SUBMISSION: usize = 12;
const APPROVED_COL_CLIENT: usize = 22;
const APPROVED_COL_REPOS: usize = 16;
const APPROVED_COL_OUTCOME: usize = 32;

/// #2863: label of the attended (`--interactive`, #2750) intake-session
/// context-menu item. Deliberately shares no substring with the sealed
/// ms-67 slice's `PULL_ITEM` needle ("Pull into decomposition session") so
/// that slice's `find`/`screen_contains` can never match this row by
/// accident.
pub(crate) const APPROVED_ATTENDED_INTAKE_ITEM: &str = "Open attended intake session…";

/// Contract §3c: the literal placeholder for a row whose `repos_for_project`
/// resolved empty — never a blank cell, so "no mapping" and "not yet
/// loaded" are never visually indistinguishable.
const APPROVED_NO_MAPPING: &str = "— no mapping —";

/// Contract §3e: the value column continuation lines indent to (13 chars —
/// the width of e.g. `"constraints: "`).
const APPROVED_DETAIL_LABEL_WIDTH: usize = 13;

/// Floor (in text rows) for the inline detail region when it is open — the
/// `"── Submission Detail ──"` divider plus the nine identity/briefing
/// fields, plus one row of headroom for a wrapped value. Without a floor a
/// list long enough to fill the panel leaves the detail rect zero-height and
/// `Enter` visibly does nothing; `render_audit_panel` reserves its own
/// `lh * 7.0` floor for exactly this reason.
const APPROVED_DETAIL_MIN_ROWS: f32 = 11.0;

impl CoordApp {
    /// The approved-submissions list, server-order (contract §3c:
    /// oldest-first — mirrors `list_submissions()`'s own `ORDER BY
    /// first_seen_at ASC`; harness note 5 in the sealed slice: the client
    /// preserves payload order rather than re-deriving it).
    pub(crate) fn approved_submissions(&self) -> &[ApprovedSubmission] {
        &self.data.approved_submissions
    }

    /// Selected row index, clamped against the current row count. `0` on an
    /// empty list (never read in that case — callers check emptiness
    /// first), same pattern as `audit_selected_idx`.
    pub(crate) fn approved_selected_idx(&self) -> usize {
        let n = self.approved_submissions().len();
        if n == 0 {
            0
        } else {
            self.approved_sel.min(n - 1)
        }
    }

    /// The currently-selected submission, or `None` on an empty list.
    pub(crate) fn approved_selected(&self) -> Option<&ApprovedSubmission> {
        self.approved_submissions().get(self.approved_selected_idx())
    }

    /// Count of rows whose `repos` resolved empty — drives both the
    /// sidebar's conditional "missing a repo mapping" line (contract §3b)
    /// and (in a follow-up, #2533) the disabled state of the "Pull into
    /// decomposition session" context-menu item.
    fn approved_missing_mapping_count(&self) -> usize {
        self.approved_submissions()
            .iter()
            .filter(|e| e.repos.is_empty())
            .count()
    }

    /// Sidebar content (contract §3b): the panel title (" APPROVED WORK
    /// ITEMS ", rendered by `ShellConfig` from `shell_config()`'s
    /// `PanelDefinition.title`, mirrored here on the `ListView` itself same
    /// as Audit/Queue) plus a "N ready to pull" aggregate line and an
    /// optional "⚠ M missing a repo mapping" line, only rendered when M > 0
    /// — mirrors `queue_sidebar`'s own `if s.blocked > 0`.
    pub(crate) fn approved_sidebar(&self) -> ListView {
        let n = self.approved_submissions().len();
        let mut items = vec![activity_item(
            &format!("  {n} ready to pull"),
            Color::rgb(160, 160, 160),
        )];
        let missing = self.approved_missing_mapping_count();
        if missing > 0 {
            items.push(activity_item(
                &format!("  ⚠ {missing} missing a repo mapping"),
                Color::rgb(255, 210, 100),
            ));
        }
        ListView {
            id: WidgetId::new("approved-sidebar"),
            title: Some(StyledText::plain(" APPROVED WORK ITEMS ")),
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

    /// The column-header row (contract §3c), painted into its own one-row
    /// rect above the scrolling row list so it stays pinned no matter how
    /// far `approved_scroll` has advanced. (It used to be item 0 of the row
    /// list itself, which both scrolled the header away and forced every
    /// selection index to carry a +1 offset.)
    fn approved_header_view() -> ListView {
        ListView {
            id: WidgetId::new("approved-header"),
            title: None,
            items: vec![approved_plain_item(
                format!(
                    "{}{}{}{}",
                    approved_pad("Submission", APPROVED_COL_SUBMISSION),
                    approved_pad("Client / Project", APPROVED_COL_CLIENT),
                    approved_pad("Repo(s)", APPROVED_COL_REPOS),
                    "Outcome",
                ),
                Color::rgb(150, 180, 220),
            )],
            // No selection highlight on a header: `has_focus: false` keeps
            // `draw_list` from painting the `▶` prefix / selected background.
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Contract §4a: right-click / keyboard-menu target for the current
    /// selection — mirrors `queue_context_target()`. `None` on an empty
    /// list (no row to act on).
    pub(crate) fn approved_context_target(&self) -> Option<ContextMenuTarget> {
        self.approved_selected().map(|e| ContextMenuTarget::ApprovedRow {
            submission_id: e.submission_id.clone(),
        })
    }

    /// Contract §4a: the context menu for an approved-work-items row.
    ///
    /// Item 1 — "Pull into decomposition session" (the HEADLESS dispatch),
    /// disabled (present, greyed, inert) when the row's `repos` resolved
    /// empty (the same "— no mapping —" condition §3c renders). It is
    /// pinned by the sealed ms-67 slice as the **top** item with that exact
    /// label and action id: never reorder, relabel, or re-flag it.
    ///
    /// Item 2 (#2863) — "Open attended intake session…", the `--interactive`
    /// counterpart #2750 added to the very same CLI verb. It is a SECOND
    /// item rather than a flag on the first precisely because §4c pins the
    /// first one's argv verbatim; both stay, and #2750 wants them
    /// interchangeable per iteration (the ledger, #2749, is the memory — not
    /// the session). Greyed with a reason when this machine can't host it —
    /// see [`Self::attended_intake_blocked_reason`].
    ///
    /// Re-resolves `submission_id` against the current
    /// `approved_submissions()` rather than trusting a cached flag on the
    /// target, so a `/board` poll that changes the mapping between
    /// right-click and click can't leave a stale decision baked in.
    pub(crate) fn context_menu_items_for_approved_row(
        &self,
        submission_id: &str,
    ) -> Vec<ContextMenuItem> {
        let repos: Vec<String> = self
            .approved_submissions()
            .iter()
            .find(|e| e.submission_id == submission_id)
            .map(|e| e.repos.clone())
            .unwrap_or_default();

        let pull = ContextMenuItem::action(
            "pull-into-decomposition-session",
            "Pull into decomposition session",
        );
        let pull = if repos.is_empty() {
            pull.disabled_because("no repo mapping")
        } else {
            pull
        };

        let attended = ContextMenuItem::action(
            "open-attended-intake-session",
            APPROVED_ATTENDED_INTAKE_ITEM,
        );
        let attended = match self.attended_intake_blocked_reason(&repos) {
            Some(reason) => attended.disabled_because(&reason),
            None => attended,
        };

        vec![pull, attended]
    }

    /// #2863: `Some(reason)` when "Open attended intake session…" must be
    /// greyed for a row whose mapped repos are `repos`, `None` when it can
    /// run here.
    ///
    /// Mirrors `_run_decompose_chat_interactive`'s own refusals
    /// (`coord/commands/portal.py`) exactly, so the menu never offers a verb
    /// the CLI is going to reject: no mapping at all, this host not being a
    /// configured machine, or the local machine not claiming **every** repo
    /// the submission maps to. `--interactive` is local-only for now (Track
    /// B / #486 is remote), which is why there is no machine picker here.
    ///
    /// Note this reads `BoardData::local_machine`, resolved in
    /// `data.rs` by a **case-insensitive** hostname compare — so it is not
    /// affected by #2860 (the Python-side `local_machine()`'s
    /// case-sensitive match). The CLI it launches still is; #2860 remains
    /// the prerequisite for the launch itself succeeding, not for this
    /// greying decision being right.
    pub(crate) fn attended_intake_blocked_reason(&self, repos: &[String]) -> Option<String> {
        if repos.is_empty() {
            return Some("no repo mapping".to_string());
        }
        let local = self.data.local_machine.clone();
        if local.is_empty() {
            return Some("this machine is not in coordinator.yml".to_string());
        }
        const NO_REPOS: &[String] = &[];
        let claimed = self
            .data
            .machines
            .iter()
            .find(|m| m.name == local)
            .map(|m| m.repos.as_slice())
            .unwrap_or(NO_REPOS);
        let missing: Vec<&str> = repos
            .iter()
            .filter(|r| !claimed.iter().any(|c| c == *r))
            .map(String::as_str)
            .collect();
        if missing.is_empty() {
            None
        } else {
            Some(format!("{local} does not claim {}", missing.join(", ")))
        }
    }

    /// Row index (into `approved_submissions()`) under `pos`, or `None` when
    /// `pos` misses the row list entirely — the pinned header row, empty
    /// space below a short list, or (when open) the detail region below the
    /// list. Reproduces `render_approved_panel`'s own rect math exactly
    /// (header consumes its own one-row rect; the list only gets what
    /// remains above any open detail pane) — same "hit-test mirrors render"
    /// posture `plans_row_at` documents for its own panel.
    pub(crate) fn approved_row_at(&self, pos: Point, main_b: Rect, lh: f32) -> Option<usize> {
        let entries = self.approved_submissions();
        if entries.is_empty() || lh <= 0.0 {
            return None;
        }
        let list_natural_h = ((entries.len() + 1) as f32 * lh).min(main_b.height);
        let list_rect = if self.approved_detail_open {
            let min_detail_h = (lh * APPROVED_DETAIL_MIN_ROWS).min(main_b.height * 0.5);
            let detail_h = (main_b.height - list_natural_h)
                .max(min_detail_h)
                .min(main_b.height);
            let list_h = (main_b.height - detail_h).max(0.0);
            Rect::new(main_b.x, main_b.y, main_b.width, list_h)
        } else {
            main_b
        };
        let header_h = lh.min(list_rect.height);
        let rows_y0 = list_rect.y + header_h;
        if pos.y < rows_y0
            || pos.x < list_rect.x
            || pos.x >= list_rect.x + list_rect.width
            || pos.y >= list_rect.y + list_rect.height
        {
            return None;
        }
        let row_in_window = ((pos.y - rows_y0) / lh).floor() as usize;
        let idx = self.approved_scroll + row_in_window;
        (idx < entries.len()).then_some(idx)
    }

    /// Contract §4a: anchor for a right-clicked row's context menu.
    /// Deliberately NOT the clicked position itself, for two reasons:
    ///
    /// 1. This panel's sidebar aggregate lines and its own row list are
    ///    both short (a handful of rows), so a menu anchored near an EARLY
    ///    row would have its own border — or the real content either side
    ///    of its bounded width — collide with another row's own text (e.g.
    ///    the disabled item's "no repo mapping" hint must never eclipse the
    ///    very row it describes, and this panel's menu is never wide enough
    ///    to blank a whole terminal row on its own).
    /// 2. quadraui's TUI `ContextMenuLayout` paints the popup's border one
    ///    cell above a *rounded* anchor Y but hit-tests against the
    ///    *unrounded* one (see `dialogs.rs`'s Board-doc-tab right-click arm
    ///    for the fuller explanation) — a fractional anchor (a raw click
    ///    position always is, since rows are hit-test in whole cells but
    ///    reported as cell centers) leaves the painted item and its actual
    ///    clickable region up to half a row apart. A whole-number anchor
    ///    makes the two agree.
    ///
    /// So this opens just below the LAST populated row of whichever of
    /// {sidebar aggregate lines, main row list} is taller, at a whole-number
    /// Y — never on top of anything else this panel paints, and never
    /// fractional.
    pub(crate) fn approved_context_menu_anchor(&self, pos: Point, main_b: Rect) -> Point {
        // Sidebar: the panel's title is painted twice — once as the shell's
        // own panel-title chrome, once again as `approved_sidebar()`'s own
        // `ListView.title` (the same `title: Some(...)` convention
        // `queue_sidebar()` uses) — so its aggregate content occupies those
        // 2 title rows plus the "N ready to pull" line plus (conditionally)
        // the "missing a repo mapping" line.
        let sidebar_rows = 2 + 1 + usize::from(self.approved_missing_mapping_count() > 0);
        // Main: the pinned header row (contract §3c) plus one row per
        // submission actually painted (never more than the panel shows).
        let main_rows = 1 + self
            .approved_submissions()
            .len()
            .min(main_b.height.max(0.0) as usize + 1);
        let below = sidebar_rows.max(main_rows) as f32;
        Point::new(pos.x, main_b.y + below + 1.0)
    }

    /// #1094-style scroll follow: keep `approved_sel` inside the visible
    /// window. Must be called after every keyboard nav that moves
    /// `approved_sel` (`j`/`k`/`Down`/`Up`/`Home`/`End` in `events.rs`) —
    /// `ListView` has no "scroll to keep the selection visible" behaviour of
    /// its own, so without this the selection walks off the bottom of the
    /// first screenful and the selected row is never painted. Mirrors
    /// `fix_audit_scroll` / `fix_queue_scroll`.
    ///
    /// `visible` is the number of *submission* rows the panel can paint —
    /// pass `content_visible_rows(main_bounds, lh)`, whose one-row title
    /// deduction matches the pinned header row this panel reserves.
    pub(crate) fn fix_approved_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        let sel = self.approved_selected_idx();
        if sel < self.approved_scroll {
            self.approved_scroll = sel;
        } else if sel >= self.approved_scroll + visible {
            self.approved_scroll = sel + 1 - visible;
        }
    }

    /// Build the main-panel row list (contract §3c) — one row per
    /// submission (the header is painted separately by
    /// `approved_header_view`). `list_view` (not a `DataTable`): no sortable
    /// numeric columns, no drag-reorder verb.
    fn approved_list_view(&self, sel: usize) -> ListView {
        let mut items = Vec::with_capacity(self.approved_submissions().len());
        for e in self.approved_submissions() {
            let client_project = format!("{} / {}", e.client, e.project_label);
            let repos_text = if e.repos.is_empty() {
                APPROVED_NO_MAPPING.to_string()
            } else {
                e.repos.join(", ")
            };
            items.push(approved_plain_item(
                format!(
                    "{}{}{}{}",
                    approved_pad(&e.submission_id, APPROVED_COL_SUBMISSION),
                    approved_pad(&client_project, APPROVED_COL_CLIENT),
                    approved_pad(&repos_text, APPROVED_COL_REPOS),
                    truncate_with_ellipsis(&e.outcome, APPROVED_COL_OUTCOME),
                ),
                Color::rgb(200, 200, 200),
            ));
        }
        ListView {
            id: WidgetId::new("approved-list"),
            title: None,
            items,
            selected_idx: sel,
            // Was hardcoded to `0`, which made every row past the first
            // screenful unreachable however far `j`/`End` moved the
            // selection. Kept in step with `approved_sel` by
            // `fix_approved_scroll` at the events.rs nav sites; `.min(sel)`
            // is the defensive half — a `/board` poll that shrinks the list
            // can strand `approved_scroll` below a re-clamped selection, and
            // the render path must never paint a window the selected row
            // sits above. (The overshoot half is clamped by quadraui's own
            // `clamp_scroll_offset`.)
            scroll_offset: self.approved_scroll.min(sel),
            has_focus: true,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Build the inline submission-detail pane (contract §3e): the
    /// `"── Submission Detail ──"` divider, identity/routing context
    /// (submission/client/project/repos/received), then the four briefing
    /// fields (#2533 consumes the same four) with long values word-wrapped
    /// to `wrap_width` and continuation lines indented to the value column.
    fn approved_detail_items(entry: &ApprovedSubmission, wrap_width: usize, rect_width: usize) -> Vec<ListItem> {
        let mut divider = "── Submission Detail ──".to_string();
        while divider.chars().count() < rect_width {
            divider.push('─');
        }
        let mut items = vec![approved_plain_item(divider, Color::rgb(230, 230, 255))];

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let received = crate::app::data::parse_iso8601_to_epoch(&entry.received_at)
            .map(|ts| format_age(Some(ts), now))
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "unknown".to_string());
        let repos_text = if entry.repos.is_empty() {
            APPROVED_NO_MAPPING.to_string()
        } else {
            entry.repos.join(", ")
        };

        let mut push_field = |label: &str, value: &str| {
            let lines = wrap_words(value, wrap_width);
            let mut it = lines.into_iter();
            let first = it.next().unwrap_or_default();
            items.push(approved_plain_item(
                format!("{:<width$}{}", label, first, width = APPROVED_DETAIL_LABEL_WIDTH),
                Color::rgb(210, 210, 210),
            ));
            for cont in it {
                items.push(approved_plain_item(
                    format!("{:width$}{}", "", cont, width = APPROVED_DETAIL_LABEL_WIDTH),
                    Color::rgb(210, 210, 210),
                ));
            }
        };

        push_field("submission:", &entry.submission_id);
        push_field("client:", &entry.client);
        push_field(
            "project:",
            &format!("{} ({})", entry.project_id, entry.project_label),
        );
        push_field("outcome:", &entry.outcome);
        push_field("audience:", &entry.audience);
        push_field("done:", &entry.done_definition);
        push_field("constraints:", &entry.constraints);
        push_field("repos:", &repos_text);
        push_field("received:", &received);
        items
    }

    /// Render the Approved work items main panel (contract §3d/§3e): empty
    /// state, populated list, or list-plus-inline-detail split when
    /// `approved_detail_open`.
    pub(crate) fn render_approved_panel(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        let entries = self.approved_submissions();
        if entries.is_empty() {
            // Contract §3d: the empty-state message deliberately never
            // contains the substring "Submission" (the header-only word) —
            // see the module doc comment on why that phrasing was picked.
            backend.draw_list(
                rect,
                &plain_list(
                    "approved-empty",
                    "  No approved work items yet — check back after a customer signs off.",
                    0,
                ),
            );
            return;
        }

        let sel = self.approved_selected_idx();
        // Header row + one row per submission, capped at the panel.
        let list_natural_h = ((entries.len() + 1) as f32 * lh).min(rect.height);
        let (list_rect, detail_rect) = if self.approved_detail_open {
            // The list only ever takes what it actually needs, so a short
            // list still hands the whole remainder to the detail region —
            // but the detail region never shrinks below
            // `APPROVED_DETAIL_MIN_ROWS`, so a list long enough to fill the
            // panel can no longer squeeze it to zero height (which made
            // `Enter` look like a no-op). The floor is itself capped at half
            // the panel so the list above never collapses on a short
            // terminal either.
            let min_detail_h = (lh * APPROVED_DETAIL_MIN_ROWS).min(rect.height * 0.5);
            let detail_h = (rect.height - list_natural_h)
                .max(min_detail_h)
                .min(rect.height);
            let list_h = (rect.height - detail_h).max(0.0);
            let list_rect = Rect::new(rect.x, rect.y, rect.width, list_h);
            let detail_rect = Rect::new(rect.x, rect.y + list_h, rect.width, detail_h);
            (list_rect, Some(detail_rect))
        } else {
            (rect, None)
        };

        // The header is painted into its own one-row rect so it stays pinned
        // while the rows below it scroll.
        let header_h = lh.min(list_rect.height);
        backend.draw_list(
            Rect::new(list_rect.x, list_rect.y, list_rect.width, header_h),
            &Self::approved_header_view(),
        );
        backend.draw_list(
            Rect::new(
                list_rect.x,
                list_rect.y + header_h,
                list_rect.width,
                (list_rect.height - header_h).max(0.0),
            ),
            &self.approved_list_view(sel),
        );

        if let Some(detail_rect) = detail_rect {
            if let Some(entry) = self.approved_selected() {
                let rect_width = detail_rect.width.round().max(0.0) as usize;
                let wrap_width = rect_width.saturating_sub(APPROVED_DETAIL_LABEL_WIDTH).max(20);
                backend.draw_list(
                    detail_rect,
                    &ListView {
                        id: WidgetId::new("approved-detail"),
                        title: None,
                        items: Self::approved_detail_items(entry, wrap_width, rect_width),
                        selected_idx: 0,
                        scroll_offset: 0,
                        has_focus: false,
                        bordered: false,
                        h_scroll: 0,
                        max_content_width: None,
                        show_v_scrollbar: false,
                    },
                );
            }
        }
    }
}

/// Left-align `text` within `width` display columns, truncating with an
/// ellipsis (via `doc_tabs::truncate_with_ellipsis`) when it overflows.
fn approved_pad(text: &str, width: usize) -> String {
    let n = text.chars().count();
    if n >= width {
        return truncate_with_ellipsis(text, width);
    }
    format!("{}{}", text, " ".repeat(width - n))
}

/// A plain, unstyled-key `ListItem` — this panel's rows/detail lines are
/// pre-formatted whole strings (column-padded or label-indented), not
/// `kv_item`'s `" {key:12} "` key/value split (which reserves a different,
/// wider prefix than this panel's contract-pinned 13-column value column).
fn approved_plain_item(text: String, color: Color) -> ListItem {
    ListItem {
        text: StyledText {
            spans: vec![StyledSpan::with_fg(text, color)],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    }
}

/// Small ASCII/whitespace word-wrapper (contract §3e / §6.2 — a new
/// convention no prior detail pane in this crate needed): breaks `text` on
/// whitespace into lines no longer than `width` characters. A single word
/// longer than `width` is kept whole on its own line rather than
/// hard-split, mirroring `trunc`'s existing "no more clever than it needs
/// to be" posture elsewhere in this crate.
fn wrap_words(text: &str, width: usize) -> Vec<String> {
    if width == 0 {
        return vec![text.to_string()];
    }
    let mut lines = Vec::new();
    let mut current = String::new();
    for word in text.split_whitespace() {
        if current.is_empty() {
            current.push_str(word);
        } else if current.chars().count() + 1 + word.chars().count() <= width {
            current.push(' ');
            current.push_str(word);
        } else {
            lines.push(std::mem::take(&mut current));
            current.push_str(word);
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::fixtures::make_test_app;
    use quadraui::tui::testing::driver_with_shell;

    /// `n` synthetic approved submissions, ids `sub_0000`..`sub_{n-1}` —
    /// enough of them to overflow any terminal the driver builds, which is
    /// the precondition both regressions below need.
    fn approved_rows(n: usize) -> Vec<ApprovedSubmission> {
        (0..n)
            .map(|i| ApprovedSubmission {
                submission_id: format!("sub_{i:04}"),
                client: format!("Client {i}"),
                project_id: format!("proj-{i}"),
                project_label: format!("Project {i}"),
                outcome: format!("Outcome number {i} for the acceptance fixture"),
                audience: "Internal ops".to_string(),
                done_definition: "Ships and is observable".to_string(),
                constraints: "No new dependencies".to_string(),
                repos: vec!["claude-coordinator".to_string()],
                received_at: "2026-08-01T00:00:00Z".to_string(),
            })
            .collect()
    }

    fn approved_driver(n: usize) -> quadraui::tui::testing::TuiDriver<impl quadraui::AppLogic> {
        let mut app = make_test_app(BoardData {
            approved_submissions: approved_rows(n),
            ..BoardData::default()
        });
        app.active_view = SidebarView::Approved;
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
        driver.render();
        driver
    }

    /// Regression: `approved_list_view` used to hardcode `scroll_offset: 0`,
    /// so `End` (and a long run of `j`) moved `approved_sel` past the first
    /// screenful while the list never scrolled — the selected row was simply
    /// never painted. Same defect class as #1094's `fix_audit_scroll`.
    #[test]
    fn end_scrolls_the_last_approved_row_into_view() {
        let mut driver = approved_driver(60);

        // Precondition: the tail row is genuinely off-screen to begin with,
        // otherwise this test would pass against the buggy code.
        assert!(
            !driver.screen_contains("sub_0059"),
            "fixture must overflow the panel for this regression to bite:\n{}",
            driver.screen(),
        );

        driver.press_named(quadraui::NamedKey::End);
        driver.render();

        assert!(
            driver.screen_contains("sub_0059"),
            "End must scroll the last approved row into view:\n{}",
            driver.screen(),
        );
    }

    /// Same regression from the other direction: repeated `j` past the
    /// bottom of the window must drag the viewport along with it, and `k`
    /// back to the head must rewind it.
    #[test]
    fn j_and_k_keep_the_selected_approved_row_on_screen() {
        let mut driver = approved_driver(60);

        for _ in 0..50 {
            driver.type_char('j');
        }
        driver.render();
        assert!(
            driver.screen_contains("sub_0050"),
            "j past the first screenful must scroll the selection into view:\n{}",
            driver.screen(),
        );

        for _ in 0..50 {
            driver.type_char('k');
        }
        driver.render();
        assert!(
            driver.screen_contains("sub_0000"),
            "k back to the head must rewind the scroll window:\n{}",
            driver.screen(),
        );
    }

    /// The column header is painted into its own pinned rect, so it survives
    /// scrolling instead of being item 0 of the scrolled list.
    #[test]
    fn column_header_stays_pinned_while_the_list_scrolls() {
        let mut driver = approved_driver(60);
        driver.press_named(quadraui::NamedKey::End);
        driver.render();

        assert!(
            driver.screen_contains("Submission"),
            "the column header must stay pinned once the list has scrolled:\n{}",
            driver.screen(),
        );
    }

    /// Regression: `render_approved_panel` handed the detail region whatever
    /// height the list did not use, with no floor — so on a list long enough
    /// to fill the panel `detail_rect` was zero-height and `Enter` looked
    /// like a no-op. `render_audit_panel` has reserved a floor for exactly
    /// this since it shipped.
    #[test]
    fn detail_pane_still_renders_when_the_list_fills_the_panel() {
        let mut driver = approved_driver(60);
        assert!(
            !driver.screen_contains("Submission Detail"),
            "detail must start closed:\n{}",
            driver.screen(),
        );

        driver.press_named(quadraui::NamedKey::Enter);
        driver.render();

        assert!(
            driver.screen_contains("Submission Detail"),
            "Enter must open a non-degenerate detail region even when the \
             row list alone would fill the panel:\n{}",
            driver.screen(),
        );
        for label in ["outcome:", "audience:", "done:", "constraints:"] {
            assert!(
                driver.screen_contains(label),
                "the detail floor must leave room for the `{label}` field:\n{}",
                driver.screen(),
            );
        }
    }

    /// The short-list case must be unchanged: with only a couple of rows the
    /// detail region still gets all the leftover height, not just the floor.
    #[test]
    fn short_list_detail_pane_keeps_the_whole_remainder() {
        let mut driver = approved_driver(2);
        driver.press_named(quadraui::NamedKey::Enter);
        driver.render();

        assert!(
            driver.screen_contains("Submission Detail"),
            "Enter must open the detail region on a short list too:\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains("received:"),
            "the last detail field must be visible on a short list:\n{}",
            driver.screen(),
        );
    }

    // ── #2533 (ms-67 contract §4) — coverage beyond the sealed slice ────────
    //
    // `tests/acceptance/ms-67/pull_decomposition_2533.rs` is sealed
    // (read-only/run-only) and drives everything reachable through mouse
    // right-click. It deliberately does not exercise: the menu-builder in
    // isolation (harness note 4 hands `icon_for_action` to an in-crate test
    // — see `app::tests::pull_into_decomposition_session_has_a_fresh_icon_
    // not_the_pty_glyph` — and the disabled/enabled decision has no other
    // in-crate coverage either), the keyboard-equivalent-of-right-click path
    // (`.` / Menu / Shift+F10, `context_menu_target_for_selection`), or the
    // actual dispatched command (`command_runner.spawned_calls`, which the
    // sealed slice's `no_spawn` fixture never inspects). These tests close
    // those gaps.

    fn one_approved_row(submission_id: &str, repos: Vec<String>) -> ApprovedSubmission {
        ApprovedSubmission {
            submission_id: submission_id.to_string(),
            client: "Acme".to_string(),
            project_id: "proj_1".to_string(),
            project_label: "Project One".to_string(),
            outcome: "Outcome".to_string(),
            audience: "Audience".to_string(),
            done_definition: "Done".to_string(),
            constraints: "None".to_string(),
            repos,
            received_at: "2026-08-01T00:00:00Z".to_string(),
        }
    }

    /// Contract §4a: the menu builder itself — enabled on a mapped row,
    /// `disabled_because` on an unmapped one. Unit-level, no driver/render
    /// needed; the sealed slice only ever observes the *consequence*
    /// (clicking is a no-op), never this struct-level fact directly.
    ///
    /// #2863 widened the menu to two items; the sealed slice pins the
    /// headless one as the TOP item with an exact label and action id, so
    /// this asserts index 0 specifically, not "the only item".
    #[test]
    fn context_menu_items_for_approved_row_gates_on_mapping() {
        let app = make_test_app(BoardData {
            approved_submissions: vec![
                one_approved_row("sub_mapped", vec!["claude-coordinator".to_string()]),
                one_approved_row("sub_unmapped", vec![]),
            ],
            ..BoardData::default()
        });

        let mapped_items = app.context_menu_items_for_approved_row("sub_mapped");
        assert!(!mapped_items[0].disabled, "mapped row's item must be enabled");
        assert_eq!(
            mapped_items[0].action_id.as_deref(),
            Some("pull-into-decomposition-session"),
        );

        let unmapped_items = app.context_menu_items_for_approved_row("sub_unmapped");
        assert!(
            unmapped_items[0].disabled,
            "unmapped row's item must be disabled, not omitted"
        );
    }

    // ── #2863: the attended (`--interactive`, #2750) sibling item ──────────

    /// Board data for the #2863 greying tests: one approved row mapped to
    /// `repos`, plus a local machine named `"local"` that claims
    /// `local_claims`.
    fn attended_board(repos: Vec<String>, local_claims: Vec<String>) -> BoardData {
        BoardData {
            approved_submissions: vec![one_approved_row("sub_0000", repos)],
            local_machine: "local".to_string(),
            machines: vec![Machine {
                name: "local".to_string(),
                host: "local.example.ts.net".to_string(),
                reachable: true,
                active_count: 0,
                repos: local_claims,
                version: None,
                worktree_bytes: 0,
            }],
            ..BoardData::default()
        }
    }

    /// #2863: the new item is a SECOND entry, below the sealed slice's
    /// pinned top item — never a change to it. The headless item's label,
    /// position and action id are exactly what `tests/acceptance/ms-67`
    /// asserts; regressing any of them breaks a sealed suite that only
    /// `coord acceptance mock --amend` may move.
    #[test]
    fn attended_intake_item_is_added_below_the_pinned_headless_item() {
        let app = make_test_app(attended_board(
            vec!["claude-coordinator".to_string()],
            vec!["claude-coordinator".to_string()],
        ));
        let items = app.context_menu_items_for_approved_row("sub_0000");

        assert_eq!(items.len(), 2, "exactly two items: headless, then attended");
        assert_eq!(
            items[0].action_id.as_deref(),
            Some("pull-into-decomposition-session"),
            "ms-67 §4a pins the HEADLESS item as the top menu item",
        );
        assert_eq!(
            items[0].label, "Pull into decomposition session",
            "ms-67 §4a pins this label verbatim — do not touch it",
        );
        assert_eq!(
            items[1].action_id.as_deref(),
            Some("open-attended-intake-session"),
        );
        assert_eq!(items[1].label, APPROVED_ATTENDED_INTAKE_ITEM);
        assert!(
            !items[1].label.contains("Pull into decomposition session"),
            "the attended label must not contain the sealed slice's PULL_ITEM \
             needle, or its `find`/`click` helpers could target the wrong row",
        );
    }

    /// #2863: `--interactive` is local-only (Track B / #486), and
    /// `_run_decompose_chat_interactive` refuses when this machine doesn't
    /// claim EVERY mapped repo. The menu greys the item with that reason
    /// rather than offering a verb the CLI will reject.
    #[test]
    fn attended_intake_item_is_enabled_only_when_local_machine_claims_every_repo() {
        // Claims all of them → enabled.
        let app = make_test_app(attended_board(
            vec!["grocery-list".to_string()],
            vec!["grocery-list".to_string(), "claude-coordinator".to_string()],
        ));
        let items = app.context_menu_items_for_approved_row("sub_0000");
        assert!(
            !items[1].disabled,
            "a machine claiming the mapped repo must be offered the attended \
             session, reason: {:?}",
            items[1].disabled_reason,
        );

        // Claims only SOME of them → greyed, and the reason names the gap.
        let app = make_test_app(attended_board(
            vec!["grocery-list".to_string(), "coord-portal".to_string()],
            vec!["grocery-list".to_string()],
        ));
        let items = app.context_menu_items_for_approved_row("sub_0000");
        assert!(items[1].disabled, "a partial claim must not be offered");
        let reason = items[1].disabled_reason.clone().unwrap_or_default();
        assert!(
            reason.contains("coord-portal"),
            "the greyed reason must name the repo this machine lacks, got: \
             {reason:?}",
        );
        assert!(
            !items[0].disabled,
            "the HEADLESS item stays enabled — it dispatches to whichever \
             machine claims the repo, so the local claim is irrelevant to it",
        );
    }

    /// #2863: with no resolvable local machine (this host isn't in
    /// `coordinator.yml` at all) the attended item is greyed with a reason
    /// saying so — never silently enabled into a launch that must fail.
    #[test]
    fn attended_intake_item_is_greyed_when_this_host_is_not_a_configured_machine() {
        let app = make_test_app(BoardData {
            approved_submissions: vec![one_approved_row(
                "sub_0000",
                vec!["grocery-list".to_string()],
            )],
            ..BoardData::default()
        });
        let items = app.context_menu_items_for_approved_row("sub_0000");
        assert!(items[1].disabled);
        assert!(
            items[1]
                .disabled_reason
                .as_deref()
                .unwrap_or_default()
                .contains("coordinator.yml"),
            "reason should point at the config gap, got: {:?}",
            items[1].disabled_reason,
        );
    }

    /// #2863: an unmapped row greys BOTH items — the attended one for the
    /// same "no repo mapping" reason §4a already gives the headless one.
    #[test]
    fn attended_intake_item_is_greyed_on_an_unmapped_row() {
        let app = make_test_app(attended_board(vec![], vec!["grocery-list".to_string()]));
        let items = app.context_menu_items_for_approved_row("sub_0000");
        assert!(items[0].disabled && items[1].disabled);
        assert_eq!(items[1].disabled_reason.as_deref(), Some("no repo mapping"));
    }

    /// #2863 acceptance: selecting the new item shells exactly
    /// `coord portal decompose-chat <id> --interactive` — the same fact
    /// `pull_action_spawns_the_decompose_chat_command` above asserts for the
    /// headless argv, checked here on the launch LINE because the attended
    /// path deliberately does not go through `command_runner` (it needs a
    /// TTY; see `launch_attended_intake_session`).
    #[test]
    fn attended_intake_launch_cmd_is_the_sealed_argv_plus_interactive() {
        let cmd = crate::app::dialogs::build_attended_intake_launch_cmd(None, "sub_0000");
        assert_eq!(
            cmd, "coord portal decompose-chat sub_0000 --interactive\r",
            "must be §4c's verbatim verb plus the one #2750 flag",
        );
        assert!(cmd.ends_with('\r'), "launcher must auto-run");

        let cmd = crate::app::dialogs::build_attended_intake_launch_cmd(
            Some("/home/john/.coord/coordinator.yml"),
            "sub_0000",
        );
        assert!(
            cmd.contains("--config /home/john/.coord/coordinator.yml"),
            "must inject --config like every other interactive launcher: {cmd}",
        );
        assert!(cmd.contains("--interactive"), "got: {cmd}");
    }

    /// #2863 acceptance (black-box, through the real
    /// `event → handle → open_context_menu → render` chain): right-clicking
    /// an approved row shows BOTH items.
    #[test]
    fn right_click_shows_both_the_headless_and_attended_items() {
        let mut app = make_test_app(attended_board(
            vec!["claude-coordinator".to_string()],
            vec!["claude-coordinator".to_string()],
        ));
        app.active_view = SidebarView::Approved;
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
        driver.render();
        driver.type_char('.');
        driver.render();
        let screen = driver.screen();
        assert!(
            screen.contains("Pull into decomposition session"),
            "the sealed slice's item must survive #2863 unchanged:\n{screen}",
        );
        assert!(
            screen.contains("Open attended intake session"),
            "#2863: the attended item must be offered on the same menu:\n\
             {screen}",
        );
    }

    /// #2863 (companion to the above): on a machine that can't host it, the
    /// attended item is still RENDERED — greyed with its reason beside it,
    /// the same "present, greyed, inert" convention §4a pins for the
    /// headless item on an unmapped row. A hidden item leaves the operator
    /// with no clue why the board won't start the conversation.
    #[test]
    fn attended_item_renders_with_its_reason_when_this_machine_cannot_host_it() {
        let mut app = make_test_app(attended_board(
            vec!["grocery-list".to_string()],
            vec!["claude-coordinator".to_string()],
        ));
        app.active_view = SidebarView::Approved;
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
        driver.render();
        driver.type_char('.');
        driver.render();
        let screen = driver.screen();
        assert!(
            screen.contains("Open attended intake"),
            "the greyed item must still be painted:\n{screen}",
        );
        assert!(
            screen.contains("does not claim grocery-list"),
            "the reason must be visible so the operator knows WHY:\n{screen}",
        );
    }

    /// Contract §4a via the KEYBOARD path (`.` / Menu / Shift+F10) rather
    /// than mouse right-click — `context_menu_target_for_selection`'s
    /// `SidebarView::Approved` arm, unreached by the sealed slice.
    #[test]
    fn keyboard_menu_key_opens_the_pull_item_for_the_selected_row() {
        let mut driver = approved_driver(1);
        // `approved_driver` leaves `approved_sel` at its default (0) — the
        // one row, mapped to "claude-coordinator" (see `approved_rows`).
        driver.type_char('.');
        driver.render();
        assert!(
            driver.screen_contains("Pull into decomposition session"),
            "the '.' keyboard trigger must open the same menu right-click \
             does, for whatever row is currently selected:\n{}",
            driver.screen(),
        );
    }

    /// Contract §4a/§4c: dispatching the (enabled) item actually shells
    /// `coord portal decompose-chat <submission_id>` — the one fact the
    /// sealed slice's `no_spawn` fixture never inspects (it only reads the
    /// resulting toast/screen, never `spawned_calls`). Calls the dispatch
    /// method directly on a plain `CoordApp` rather than through a
    /// `TuiDriver` — `driver_with_shell` wraps the app in an opaque
    /// `ShellAdapter` that doesn't expose `command_runner` (see the many
    /// `driver.app() isn't CoordApp` notes elsewhere in this crate's
    /// tests) — the full event→handle→dispatch chain is already proven
    /// live by the sealed slice's own `pull_action_fires_the_chat_ready_
    /// toast` (it only observes the toast, not the argv).
    #[test]
    fn pull_action_spawns_the_decompose_chat_command() {
        let mut app = make_test_app(BoardData {
            approved_submissions: vec![one_approved_row(
                "sub_0000",
                vec!["claude-coordinator".to_string()],
            )],
            ..BoardData::default()
        });

        app.dispatch_approved_pull_into_decomposition("sub_0000");

        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "portal".to_string(),
                "decompose-chat".to_string(),
                "sub_0000".to_string(),
            ]],
            "the pull action must shell exactly `coord portal decompose-chat \
             <submission_id>` (contract §4c)",
        );
    }

    // ── #2863: one coherent error surface on a failed headless dispatch ────

    /// #2863 acceptance: "A non-zero exit from either action leaves the
    /// operator with **one** coherent error surface — not a success toast
    /// followed by a failure toast followed by a timeout toast."
    ///
    /// Drives the REAL spawn → poll → result pipeline (`new_for_test`'s
    /// `no_spawn` branch resolves the canned result synchronously at spawn
    /// time), the same seam `gate_a_dispatch_failure_opens_full_text_error_
    /// dialog` uses.
    #[test]
    fn failed_decompose_chat_dispatch_leaves_exactly_one_error_surface() {
        let mut app = make_test_app(BoardData {
            approved_submissions: vec![one_approved_row(
                "sub_0000",
                vec!["claude-coordinator".to_string()],
            )],
            ..BoardData::default()
        });
        app.command_runner = crate::commands::CommandRunner::new_for_test();
        // The live 2026-08-27/28 failure, verbatim in shape: the thin-client
        // refusal, multi-sentence, with the actionable half at the END.
        let refusal = "Error: coord portal decompose-chat must run on the \
             daemon host. This machine is a thin client (board_service is set \
             in ~/.coord/client.toml). ssh to the daemon host and run it \
             there, or use the attended --interactive session locally.";
        app.command_runner.push_canned_result(1, refusal);

        app.dispatch_approved_pull_into_decomposition("sub_0000");
        // Precondition: the optimistic §4d toast really did fire first —
        // otherwise "retracted" would pass vacuously.
        assert!(
            app.toasts
                .iter()
                .any(|(t, _, _)| t.title == "Decomposition chat"),
            "contract §4d's optimistic toast must fire at dispatch time",
        );
        assert!(app.pending_decomposition_chat.is_some());

        app.run_periodic_work();

        // 1. The optimistic "chat ready" toast is retracted — it is now
        //    known to be false and must not sit beside the error.
        assert!(
            !app.toasts
                .iter()
                .any(|(t, _, _)| t.title == "Decomposition chat"),
            "the optimistic \"chat ready\" toast must be retracted once the \
             dispatch is known to have failed",
        );
        // 2. The generic 40-col "Command failed" toast is suppressed — the
        //    modal is the single notification.
        assert!(
            !app.toasts.iter().any(|(t, _, _)| t.title == "Command failed"),
            "the truncating command-failed toast must be suppressed when the \
             full-text modal is shown",
        );
        // 3. The 30 s bind-timeout toast can never fire: nothing is pending.
        assert!(
            app.pending_decomposition_chat.is_none(),
            "a failed dispatch must disarm the bind timeout — no assignment \
             is ever going to appear, so its \"timed out\" toast would only \
             restate the same failure as a second, different problem",
        );
        // 4. The FULL refusal is readable, not a ~40-col truncation.
        let body = app
            .decompose_chat_error_dialog
            .as_ref()
            .expect("a failed decomposition dispatch must raise the modal");
        assert!(
            body.contains("thin client") && body.contains("--interactive"),
            "the modal must carry the COMPLETE reason including its trailing \
             actionable sentence, got: {body}",
        );
        assert!(
            body.contains("sub_0000"),
            "the modal must name the submission that failed, got: {body}",
        );
    }

    /// #2863: the failure modal actually paints through the real
    /// ShellAdapter → render path, word-wrapped, and Esc dismisses it.
    #[test]
    fn decompose_chat_error_dialog_renders_full_reason_and_dismisses() {
        let mut app = make_test_app(BoardData::default());
        app.decompose_chat_error_dialog = Some(
            "Error: coord portal decompose-chat must run on the daemon host. \
             ssh to the daemon host and run it there."
                .to_string(),
        );
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
        driver.render();
        assert!(
            driver.screen_contains("Decomposition session dispatch failed"),
            "modal title must render on screen:\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains("ssh to the daemon host"),
            "the tail of the refusal — the actionable half a 40-col toast \
             eats — must be readable on screen:\n{}",
            driver.screen(),
        );

        driver.press_named(quadraui::NamedKey::Escape);
        driver.render();
        assert!(
            !driver.screen_contains("Decomposition session dispatch failed"),
            "Esc must dismiss the modal:\n{}",
            driver.screen(),
        );
    }

    /// #2863: the label matcher fires on the headless dispatch this issue
    /// fixes and on nothing else — a sibling `portal` verb keeps the normal
    /// command-failed toast.
    #[test]
    fn is_decompose_chat_dispatch_label_matches_only_that_dispatch() {
        use crate::app::dialogs::{
            decompose_chat_label_submission_id, is_decompose_chat_dispatch_label,
        };
        assert!(is_decompose_chat_dispatch_label(
            "coord portal decompose-chat SUB-1EA1D3"
        ));
        assert!(!is_decompose_chat_dispatch_label("coord portal ledger SUB-1EA1D3"));
        assert!(!is_decompose_chat_dispatch_label("coord portal link SUB-1EA1D3 42"));
        assert!(!is_decompose_chat_dispatch_label("coord milestone dispatch api 42"));

        assert_eq!(
            decompose_chat_label_submission_id("coord portal decompose-chat SUB-1EA1D3"),
            Some("SUB-1EA1D3".to_string()),
        );
        assert_eq!(
            decompose_chat_label_submission_id("coord portal decompose-chat"),
            None,
            "a malformed label must not panic",
        );
    }
}
