//! Backend-neutral app logic for coord-tui.
//!
//! [`CoordApp`] implements [`quadraui::ShellApp`] using only the
//! backend-neutral trait surface (`draw_list`, `draw_split`, `draw_tree`,
//! `draw_status_bar`). No ratatui or crossterm symbols appear here —
//! those live exclusively in the TUI and GTK shim entry points.
//!
//! ## Layout
//!
//! ```text
//! ┌──┬──────────────────────────────┬──────────────────────────────────┐
//! │B │ Board tree / Machines list   │ DETAIL                           │
//! │M │ ▼ Running (1)                │ claude-coordinator #115          │
//! │P │   #115  claude-coord  RUN    │  ID     6b2670e…                 │
//! │  │ ▼ Failed (0)                 │  Machine dellserver              │
//! │  │ ▶ Done (3)                   │                                  │
//! │  │                              │                                  │
//! ├──┴──────────────────────────────┴──────────────────────────────────┤
//! │ coord-tui  Board  [Sidebar]  ↻ 3s  click activity bar to switch  q  │
//! └────────────────────────────────────────────────────────────────────┘
//!  ↑ activity bar    ↑ sidebar (35 cols)    ↑ main content
//! ```
//!
//! The leftmost column is the quadraui AppShell activity bar (B/M/P icons).
//! The shell handles activity bar clicks, sidebar toggle, and divider drag.
//! `render_content()` draws the list (tree/machines/pipeline) into
//! `sidebar_content_bounds`, the detail panel into `main_content_bounds`,
//! and the status bar into `status_bar_bounds`.
//!
//! **Data sources:**
//! - `~/.coord/coord.db` — SQLite database (WAL mode) written by the coordinator
//!
//! **Auto-refresh:** every 5 s, checked at the start of each [`ShellApp::handle`]
//! call and on every [`ShellApp::tick`] (quadraui drives `tick` ~60Hz so
//! refreshes proceed while the user is idle).

use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::Connection;

use quadraui::compose::app_shell::{AppShellEvent, AppShellLayout, PanelDefinition};
use quadraui::compose::form_controller::{FormController, FormControllerEvent};
use quadraui::compose::sidebar_system::{
    NavigationMode, SidebarEvent, SidebarSectionDef, SidebarSystem,
};
use quadraui::primitives::context_menu::{
    ContextMenu, ContextMenuHit, ContextMenuItem as QuiContextMenuItem, ContextMenuItemMeasure,
    ContextMenuLayout, ContextMenuPlacement,
};
use quadraui::primitives::form::{FieldKind, Form, FormEvent, FormField};
use quadraui::primitives::toast::{ToastCorner, ToastItem, ToastSeverity, ToastStack};

use crate::settings::{
    LogCacheTtl, ModelPref, RefreshCadence, Theme, TuiSettings, ACTION_PIPELINE_REFRESH,
};
use quadraui::accelerator::{parse_key_binding, ParsedBinding};
use quadraui::{
    Backend, Badge, Chart, ChartKind, ChatController, ChatControllerEvent, ChatRole, ChatTurn,
    Color, Decoration,
    Dialog, DialogButton, DialogHit, DialogInput, DialogLayout, DialogMeasure,
    DialogSeverity, DialogTextInput,
    Key, ListItem, ListItemMeasure, ListView, ListViewHit, Modifiers, MouseButton, NamedKey,
    PipelineHit, PipelineStage as QuiPipelineStage, PipelineView as QuiPipelineView,
    // #2017: resize-cursor hover affordance for the Queue splitter.
    PointerShape, ResizeEdge,
    Point, Reaction, Rect, ScrollDelta, ScrollMode, SectionSize, Series, ShellApp,
    ShellConfig, ShellContext, SidebarPanel, SidebarPanelHit, StageStatus, StatusBar,
    StatusBarSegment, Scrollbar, StyledSpan, StyledText, TabBar, TabItem, TextRegion, Toolbar,
    // #2283 (ms-65 §4): overflow-affordance layout — `board_doc_tab_strip`
    // (render.rs) calls `TabBar::layout` directly (rather than only via
    // `Backend::tab_bar_layout`) to get `scroll_left`/`scroll_right`, which
    // the TUI backend's own wrapper never exposes (it hardcodes
    // `scroll_arrow_width: 0.0`).
    SegmentMeasure, TabMeasure,
    ToolbarButton, ToolbarHoverTracker, ToolbarItemMeasure, TreeRow, UiEvent, WidgetId,
    BadgeStatus, BoardCard, BoardColumn, BoardHit, BoardLayout, BoardModel, CardBadge, MoveDir,
    // #1094: Audit panel row list — first `DataTable` use in coord-tui.
    Column, ColumnAlign, ColumnWidth, DataRow, DataTable, DataTableHit, DataTableLayout,
    // #1116: Usage panel grid/drill sort-arrow indicator.
    SortDirection,
    // #1741: Reports panel — coord-tui's first `MultiSectionView` (the
    // collapsible per-report section stack) plus the `Form` layout/hit types
    // its bodies route through. `ScrollMode`/`SectionSize` are already
    // imported above (they come from the same primitive).
    FormHit, FormLayout, HeaderHit, MultiSectionView, MultiSectionViewHit,
    MultiSectionViewLayout, MsvAxis, Section, SectionBody, SectionHeader,
    // #1765: the per-section Export action and the save dialog it opens.
    // `HeaderAction` + `SectionHeader.actions` and
    // `PlatformServices::show_file_save_dialog` both already existed
    // upstream — this feature adds nothing to quadraui.
    FileDialogOptions, HeaderAction, Icon,
    // #953: Terminal-view left-pane machine tree — the app's first direct
    // `backend.draw_tree` sidebar (bypassing `SidebarSystem`).
    SelectionMode, TreePath, TreeStyle, TreeView,
    // #1124: reusable quadraui help layer (#431) — `?` cheatsheet registry
    // + `/` command palette. See `plans.rs` module docs.
    DualModePaletteController, DualModePaletteEvent, HelpAction, HelpNote, HelpOverlayController,
    HelpOverlayEvent, HelpRegistry, ViewHelp, filter_help_actions, help_actions_to_palette_items,
    PALETTE_CHROME_ROWS,
    // #2288 (ms-65 §9): the side-by-side pane split. `SplitTreeLayout` is
    // the geometry both the paint (`paint_board_pane_dividers`) and the
    // click/drag hit-test (`board_split_pane_at`) consume — one layout, per
    // quadraui's own "avoiding the vimcode drift bug class" note.
    // `TextDisplay` is how the `║` divider column is painted (see that
    // function's doc comment for why not `draw_split_tree`).
    SplitTreeLayout, TextDisplay, TextDisplayLine,
};
use quadraui::terminal_engine::TerminalMouseKind;

/// #2271 / quadraui#584: **does the pinned `quadraui` rev paint every series
/// of a multi-series bar chart?**
///
/// Before #584, `tui/chart.rs`'s `paint_bar` rendered `series[0]` and
/// silently discarded the rest — a multi-series bar drew a plausible-looking
/// chart missing most of its data, with no error anywhere. That is worse than
/// no chart, so the Reports panel gates on this constant and degrades a
/// multi-series bar declaration to a table with a stated reason rather than
/// drawing a wrong one (`reports::chart_plan`).
///
/// **This is a fact about `tui/Cargo.toml`'s `rev`, so it moves with the pin
/// and only with the pin** (#1973 — bumping the pin is its own reviewable
/// commit). It is a `const` rather than a probe because there is nothing to
/// probe: `paint_bar` is private, its wrongness is silent, and a compile-time
/// statement about the pin is exactly as trustworthy as the pin itself.
/// Verify by reading `paint_bar` at the pinned rev — if it loops
/// `chart.series.iter()` (and `ChartKind` has a `BarGrouped` variant), this
/// is `true`.
///
/// Currently `true`: the pin was bumped to `d70da7d` in this same change,
/// which carries #584 (`paint_bar` loops every series and `ChartKind` gained
/// `BarGrouped`).
pub(crate) const QUADRAUI_MULTI_SERIES_BARS: bool = true;

pub(crate) mod types;
pub(crate) mod format;
pub(crate) mod data;
pub(crate) mod dialogs;
pub(crate) mod sidebar;
pub(crate) mod settings_ui;
pub(crate) mod render;
pub(crate) mod chat;
pub(crate) mod terminal;
pub(crate) mod sessions;
pub(crate) mod events;
pub(crate) mod pipeline;
pub(crate) mod milestone_dag;
pub(crate) mod plans;
pub(crate) mod audit;
// #1741: Reports panel + the reusable `MultiSectionView` routing it is the
// first consumer of (epic #571's panel consolidation is the next).
pub(crate) mod msv;
pub(crate) mod reports;
pub(crate) mod fleet_terminals;
pub(crate) mod fleet_sessions;
pub(crate) mod workspace;
pub(crate) mod drive;
pub(crate) mod escalation;
pub(crate) mod fleet_health;
pub(crate) mod drive_queue;
// #2532 (ms-67 contract §3): Approved work items panel — portal submissions
// ready for decomposition into coordinator work.
pub(crate) mod approved;
// #2282 (ms-65): per-panel document tabs with VS Code preview/pin semantics.
pub(crate) mod doc_tabs;
#[allow(unused_imports)]
use self::doc_tabs::{
    doc_tab_label, resolve_doc_tab_click, DetailSubState, DocKey, DocTabPickerRow, DocTabs,
    PanelScope, TabClickKind, DOC_TAB_LABEL_COLS, PANE_DIVIDER_CHAR, SCROLL_LEFT_MARKER,
    SCROLL_RIGHT_MARKER, SPLIT_DOC_TAB_LABEL_COLS,
};
#[allow(unused_imports)]
use self::types::*;
#[allow(unused_imports)]
use self::format::*;
#[allow(unused_imports)]
use self::data::*;
#[allow(unused_imports)]
use self::dialogs::*;
#[allow(unused_imports)]
use self::sidebar::*;
#[allow(unused_imports)]
use self::settings_ui::*;
#[allow(unused_imports)]
use self::render::*;
#[allow(unused_imports)]
use self::chat::*;
#[allow(unused_imports)]
use self::terminal::*;
#[allow(unused_imports)]
use self::sessions::*;
#[allow(unused_imports)]
use self::events::*;
#[allow(unused_imports)]
use self::pipeline::*;
use self::milestone_dag::*;
#[allow(unused_imports)]
use self::fleet_terminals::*;
#[allow(unused_imports)]
use self::fleet_sessions::*;
#[allow(unused_imports)]
use self::workspace::*;
#[allow(unused_imports)]
use self::drive::*;
#[allow(unused_imports)]
use self::msv::*;
#[allow(unused_imports)]
use self::drive_queue::*;

// ─── Auto-refresh interval ────────────────────────────────────────────────────

/// Auto-run `coord notify` every 30 seconds when assignments are running.
const NOTIFY_EVERY: Duration = Duration::from_secs(30);

/// How long a toast stays visible before auto-dismissing.
const TOAST_TTL: Duration = Duration::from_secs(4);

// ─── Shared sidebar filter ────────────────────────────────────────────────────

/// State + logic for a sidebar "FILTER" search box.
///
/// Both the Board and Pipeline panels embed a quadraui `FieldKind::TextInput`
/// form at section 0 that filters issues by a case-insensitive substring on
/// issue number/title. This struct owns the query string, the cursor byte
/// offset (always kept at a UTF-8 char boundary), and the focus flag, and
/// centralises every edit/match operation so the two panels behave
/// identically rather than duplicating the keyboard arms.
#[derive(Default, Clone)]
struct SidebarFilter {
    /// Current value in the filter input (always lowercased on match, not here).
    query: String,
    /// Cursor byte offset into `query` (kept on a char boundary).
    cursor: usize,
    /// Whether the input is accepting keyboard input.
    focused: bool,
}

impl SidebarFilter {
    /// True if an issue matches the current query (empty query → all match).
    /// Case-insensitive substring on the decimal issue number or the title —
    /// unless the query starts with `#` (issue-number scope, #1301): the `#`
    /// is stripped and only the number is checked (still substring, so
    /// partial typing like `#44` keeps working), dropping the title clause
    /// entirely. That's the whole point — a bare `4` used to also pull in
    /// any issue whose *title* happened to contain a "4" (e.g. #1292's
    /// "...pin 4 ($0.50 vs $0.5000)"), which `#4` now avoids by construction.
    fn matches(&self, num: u64, title: &str) -> bool {
        if self.query.is_empty() {
            return true;
        }
        let query = self.query.to_lowercase();
        let num_str = num.to_string();
        if let Some(number_only) = query.strip_prefix('#') {
            // Bare "#" (no digits yet) behaves like an empty query — the user
            // has opted into number scope but hasn't typed a digit, so don't
            // hide every row while they're mid-keystroke.
            return number_only.is_empty() || num_str.contains(number_only);
        }
        num_str.contains(&query) || title.to_lowercase().contains(&query)
    }

    /// Insert a printable char at the cursor and advance past it.
    fn insert_char(&mut self, c: char) {
        self.query.insert(self.cursor, c);
        self.cursor += c.len_utf8();
    }

    /// Walk backward from `from` (a byte offset into `query`, not necessarily
    /// on a char boundary) to the start of the previous UTF-8 char boundary.
    /// Shared by `backspace` and `cursor_left` so both land on a valid
    /// boundary regardless of prior (possibly stale) byte offsets.
    fn prev_char_boundary(&self, from: usize) -> usize {
        let mut prev = from.saturating_sub(1);
        while prev > 0 && !self.query.is_char_boundary(prev) {
            prev -= 1;
        }
        prev
    }

    /// Delete the char before the cursor (UTF-8 aware), if any.
    fn backspace(&mut self) {
        if self.cursor > 0 {
            let prev = self.prev_char_boundary(self.cursor);
            self.query.remove(prev);
            self.cursor = prev;
        }
    }

    /// Clear the query, reset the cursor, and drop focus.
    fn clear(&mut self) {
        self.query.clear();
        self.cursor = 0;
        self.focused = false;
    }

    /// Move the cursor one Unicode scalar left.
    fn cursor_left(&mut self) {
        if self.cursor > 0 {
            self.cursor = self.prev_char_boundary(self.cursor);
        }
    }

    /// Move the cursor one Unicode scalar right.
    fn cursor_right(&mut self) {
        let max = self.query.len();
        if self.cursor < max {
            // Advance past the next char boundary.
            let rest = &self.query[self.cursor..];
            if let Some(ch) = rest.chars().next() {
                self.cursor += ch.len_utf8();
            }
        }
    }

    /// Replace the value (e.g. from a FormEvent) and park the cursor at the end.
    fn set_value(&mut self, value: &str) {
        self.query = value.to_string();
        self.cursor = value.len();
    }

    /// Whether the query is empty (no active filter).
    fn is_empty(&self) -> bool {
        self.query.is_empty()
    }

    /// Build the quadraui `Form` rendered in the FILTER section.
    ///
    /// `id_prefix` namespaces the widget ids so two filters on screen at
    /// once (Board vs Pipeline sidebars) don't collide. `placeholder` is the
    /// greyed hint shown when the query is empty.
    fn form(&self, id_prefix: &str, placeholder: &str) -> Form {
        Form {
            id: WidgetId::new(format!("{id_prefix}-form")),
            fields: vec![FormField {
                id: WidgetId::new(format!("{id_prefix}-input")),
                label: StyledText::plain(""),
                kind: FieldKind::TextInput {
                    value: self.query.clone(),
                    placeholder: placeholder.to_string(),
                    cursor: Some(self.cursor),
                    selection_anchor: None,
                },
                hint: StyledText::plain(""),
                disabled: false,
                validation: None,
            }],
            focused_field: if self.focused {
                Some(WidgetId::new(format!("{id_prefix}-input")))
            } else {
                None
            },
            scroll_offset: 0,
            has_focus: self.focused,
        }
    }
}

// ─── #2642: Ctrl+E doc-tab open-tabs picker ────────────────────────────────────

/// State for the Ctrl+E "open tabs" quick-pick — composes quadraui's
/// `Palette` primitive (via `DualModePaletteController`, the same wrapper
/// `command_palette` already drives, see `plans.rs`) rather than a
/// hand-rolled overlay like [`IssueFinder`] above.
///
/// Supersedes the dead `‹`/`›` overflow markers baked into the Board/
/// Pipeline doc-tab strip as the way to reach a tab scrolled out of view.
struct DocTabPickerState {
    /// Which panel's tab set this picker is listing — Board or Pipeline.
    scope: PanelScope,
    /// Board only: the pane the picker was opened from
    /// (`board_render_pane()` at open time). Pinned rather than re-read
    /// live: nothing can move focus while this modal owns all input, so it
    /// never goes stale, and pinning it is what lets every row-activating
    /// callback act on the SAME pane the rows were built from. Meaningless
    /// for Pipeline (`PaneSet` never splits there) — always `0`.
    pane: usize,
    palette: DualModePaletteController,
}

// ─── Issue fuzzy finder ───────────────────────────────────────────────────────

/// #541: State for the global Telescope-style issue fuzzy-finder overlay.
///
/// Opened with Ctrl+P from any view.  The user types to fuzzy-search across
/// all issues in `data.open_issues`; j/k (or Up/Down) navigates; Enter jumps
/// to the selected issue (Pipeline if tracked, otherwise Board); Esc closes.
#[derive(Default)]
struct IssueFinder {
    /// Current search query (as-typed, not lowercased).
    query: String,
    /// Byte offset of the text cursor inside `query` (on a char boundary).
    cursor: usize,
    /// Index of the highlighted result row within the current match list.
    selected_idx: usize,
}

impl IssueFinder {
    /// Insert a printable character at the cursor position.
    fn insert_char(&mut self, c: char) {
        self.query.insert(self.cursor, c);
        self.cursor += c.len_utf8();
        self.selected_idx = 0; // reset selection on every query change
    }

    /// Delete the character before the cursor (UTF-8 safe).
    fn backspace(&mut self) {
        if self.cursor > 0 {
            let mut prev = self.cursor - 1;
            while prev > 0 && !self.query.is_char_boundary(prev) {
                prev -= 1;
            }
            self.query.remove(prev);
            self.cursor = prev;
            self.selected_idx = 0;
        }
    }

    /// Move the highlight down by one row (wraps at the end of `max`).
    fn move_down(&mut self, max: usize) {
        if max > 0 {
            self.selected_idx = (self.selected_idx + 1).min(max - 1);
        }
    }

    /// Move the highlight up by one row (clamps at 0).
    fn move_up(&mut self) {
        self.selected_idx = self.selected_idx.saturating_sub(1);
    }
}

// ─── Detail panel tabs ────────────────────────────────────────────────────────

/// Per-assignment context for the live watch overlay (Pipeline > Stages
/// > Enter).  The overlay takes over the main panel, auto-refreshes the
/// worker log every render, and accepts K/q/scroll keys.
#[derive(Clone)]
struct WatchState {
    assignment_id: String,
    machine: String,
    repo: String,
    issue_number: u64,
    /// Assignment type ("work", "plan", "review"). Drives which control
    /// keys are available — e.g. 'A' (approve plan → dispatch work) is
    /// only meaningful when watching a plan-type assignment.
    assignment_type: String,
    /// Scroll offset into the log lines.  `usize::MAX` is a sentinel
    /// meaning "stick to the bottom"; any explicit user scroll replaces
    /// this with a concrete row.
    scroll: usize,
}

/// One active SSE watch session.  Stored in `CoordApp.watch_pool` keyed by
/// `assignment_id`.  Background sessions (not currently focused) keep their
/// background thread alive and accumulate lines so switching back requires no
/// reconnect.
pub(crate) struct WatchContext {
    state: WatchState,
    sse: WatchSseState,
    /// Per-assignment inject transcript.  Persists while the context lives in
    /// the pool; cleared on eviction.
    inject_transcript: Vec<ChatTurn>,
    /// #264: parallel to `inject_transcript` — for each user turn, the
    /// `sse.lines.len()` at the moment of submit.  Used to interleave
    /// user + assistant turns chronologically in `chat_transcript_from_pool`
    /// (assistant turns at sse position N appear after any user turns whose
    /// offset ≤ N).  Without this, all user turns piled at the top and all
    /// assistant turns followed, breaking the conversational order the user
    /// expects.
    inject_sse_offsets: Vec<usize>,
    /// #315: frozen transcript turns carried over from prior workers in the
    /// same chat session.  Populated by `maybe_bind_pending_resume` when a
    /// `--resume` re-dispatch lands: it captures the prior context's full
    /// rendered chat (user + assistant turns) and stashes it here so the
    /// rebound overlay shows the prior conversation rather than starting
    /// from a blank slate.  Always rendered first by
    /// `chat_transcript_from_pool`; empty on first-dispatch contexts.
    history_turns: Vec<ChatTurn>,
    /// Wall-clock time this context was most recently focused.  Used for LRU
    /// eviction when the pool would exceed `WATCH_POOL_CAP`.
    last_focused_at: Instant,
}

/// #235 Phase 1: in-flight `coord test <work_id>` build job spawned from
/// the TUI's local machine. Re-uses the existing CLI which does git fetch +
/// checkout + `repo.build_command`, so no Rust-side git/build logic is
/// duplicated here. Job state lives in-memory only — restarting the TUI
/// drops it; the user can re-press `B` to retrigger.
/// #264: state for a refinement-chat dispatch that has been shelled but
/// whose assignment row hasn't appeared in the DB yet.  Polled each tick;
/// on bind we add the new assignment to `watch_pool`, focus it, and open
/// the inject_chat overlay.  On timeout we drop and toast.
#[derive(Clone)]
struct PendingRefinement {
    /// Coord-local repo name (matches `coordinator.yml`).
    repo: String,
    /// Issue number under refinement.
    issue_number: u64,
    /// Wall-clock dispatch instant — used to bound the wait at
    /// `REFINEMENT_BIND_TIMEOUT`.
    dispatched_at: Instant,
}

/// How long to wait for the refinement assignment row to appear before
/// giving up on the chat overlay.  `coord refine-chat` typically writes
/// the row within ~1 s; 30 s is a generous ceiling for slow agents.
const REFINEMENT_BIND_TIMEOUT: Duration = Duration::from_secs(30);

/// #314 Phase B: state carried while we wait for a `coord test-chat
/// <work_assignment_id>` dispatch to appear in the local DB so the inject-chat
/// overlay can be bound to it.  Mirrors `PendingRefinement`.
#[derive(Clone)]
struct PendingTestChat {
    /// Coord-local repo name — used in toast copy.
    repo: String,
    /// The work assignment id passed to `coord test-chat`.
    work_assignment_id: String,
    /// Issue number — used to filter `self.data.assignments`.
    issue_number: u64,
    /// Wall-clock dispatch instant — bounds the wait at
    /// `REFINEMENT_BIND_TIMEOUT`.
    dispatched_at: Instant,
}

/// #315: state carried while we wait for a re-dispatched `coord chat-continue`
/// assignment to appear in the local DB so the inject-chat overlay can rebind
/// to the new assignment (and keep the session alive across `end_turn` exits).
#[derive(Clone)]
struct PendingChatResume {
    /// The prior (completed) assignment id — used to avoid rebinding to the
    /// same row that just exited.
    old_assignment_id: String,
    /// Issue number, for filtering `self.data.assignments`.
    issue_number: u64,
    /// Wall-clock instant when `coord chat-continue` was launched.  Used
    /// for the timeout (`REFINEMENT_BIND_TIMEOUT`).
    dispatched_at: Instant,
    /// Unix-time floor for matching new assignments.  Bind only accepts
    /// rows whose `dispatched_at >= arm_unix_secs - clock-skew grace`.
    /// Without this, the bind picks ANY older refinement on the same
    /// issue that happens to pass the `id != old` filter — bug spotted
    /// in repro logs where the second submit immediately bound BACKWARDS
    /// to the original (initial) refinement assignment because the new
    /// resume row hadn't been written to the DB yet.
    arm_unix_secs: f64,
    /// The `assignment_type` of the OLD (completed) assignment — used to
    /// match the continuation to the same chat type (e.g. "refinement",
    /// "test-chat", "new-issue-chat").  Without this the bind was
    /// hardcoded to "refinement" and test-chat / new-issue-chat
    /// continuations never rebound (#361).
    old_type: Option<String>,
}

/// #264: after the refinement chat closes we issue `coord stop` first,
/// then `coord ready` once the stop returns.  CommandRunner is single-slot
/// so we can't chain them in one call without a shell wrapper; this state
/// queues the follow-up `ready` for the next tick after `stop` completes.
#[derive(Clone)]
struct PendingRefineReady {
    repo: String,
    issue_number: u64,
    /// The assignment id of the refinement worker — only used to recognise
    /// "this is the stop we were waiting on" when CommandResult lands.
    assignment_id: String,
    /// #410: when true, queue a `coord assign` dispatch immediately after
    /// `coord ready` succeeds (Send = Save + dispatch to pipeline).
    then_dispatch: bool,
}

/// #319 Phase A: synth prompt injected into the refinement chat when the
/// user presses Ctrl+N to draft refinement notes.  `{DATE}` is substituted
/// with today's date before sending so the chat's header is dated even if
/// the assistant misreads the placeholder.
const REFINEMENT_NOTES_SYNTH_PROMPT: &str = "The developer has marked this refinement complete.  Write a concise \"refinement notes\" comment summarising what we settled on:\n  - Scope decisions (what's in, what's out)\n  - File / module boundaries that came up\n  - Acceptance criteria the developer confirmed\n  - Open questions left for the implementing worker\n\nOutput the comment body in markdown.  Start with a `## Refinement notes ({DATE})` header.  Stop after the comment — the developer's TUI will post it.";

/// #319 Phase A: ceiling on how long to wait for the assistant's reply
/// before dropping back to the chat.  Generous — long refinements can
/// take a minute to settle, and the user can still cancel manually.
const REFINEMENT_NOTES_SYNTH_TIMEOUT: Duration = Duration::from_secs(180);

/// #319 Phase A: state held while we wait for the chat to reply to the
/// synth prompt.  Populated by [`CoordApp::trigger_refinement_notes_synth`]
/// and cleared once the review modal opens or the wait times out.
#[derive(Clone)]
struct PendingRefinementNotesSynth {
    /// Assignment id at trigger time.  May be superseded by a chat-continue
    /// resume — the poll re-fetches `watch_focused` to find the live id.
    aid_at_trigger: String,
    /// Issue number — used to address the `gh issue comment` shell-out.
    issue_number: u64,
    /// Coord-local repo name (only used in toasts; the slug is below).
    #[allow(dead_code)]
    repo_coord: String,
    /// GitHub `owner/name` slug for the `gh` command's `--repo` flag.
    repo_github: String,
    /// `sse.lines.len()` at trigger time — assistant lines past this index
    /// are the reply we want to capture.  Becomes meaningless if the chat
    /// rebinds (new ctx starts at 0); the poll handles that by floor=0.
    baseline_sse_lines: usize,
    /// Wall-clock arm time.  Bounds the wait at
    /// [`REFINEMENT_NOTES_SYNTH_TIMEOUT`].
    armed_at: Instant,
}

/// #319 Phase A: review-and-post modal for the proposed refinement-notes
/// comment.  When `Some` on [`CoordApp`], intercepts all keyboard input and
/// renders an overlay above the chat.  The body is editable inline:
/// Backspace pops, Enter inserts a newline, printable chars append.  Ctrl+Y
/// posts via `gh issue comment`; Esc cancels.
#[derive(Clone)]
struct RefinementNotesModal {
    /// Issue number — for the `gh` command and the modal title.
    issue_number: u64,
    /// GitHub `owner/name` slug for the `--repo` flag.
    repo_github: String,
    /// The proposed comment body.  Mutated by edits.
    body: String,
    /// True while a `gh issue comment` shell-out is in flight.  Modal
    /// stays visible to capture the result; new keypresses are ignored
    /// so a fast user can't double-post.
    posting: bool,
}

/// #319 Phase A: outcome of the background `gh issue comment` shell-out,
/// sent over the mpsc back to the main thread.
struct RefinementNotesPostResult {
    success: bool,
    /// First non-empty stderr line — surfaced in the failure toast so the
    /// user knows whether it was a missing label, auth issue, etc.
    stderr_first_line: String,
    /// Echoed so the result-handler doesn't need to read modal state.
    issue_number: u64,
}

/// #328: Y/N/Esc prompt that appears when the user presses Esc on a
/// non-empty refinement chat.  The chat overlay stays visible behind the
/// prompt; the prompt is rendered as a status-bar hint (mirrors
/// `pending_test_fail` / `pending_force_merge`).  Cleared when the user
/// picks Y (chain into notes finaliser), N (immediate finalise), or Esc
/// (cancel — chat stays open).
#[derive(Clone)]
struct PendingRefinementClosePrompt {
    /// Issue number — only used for the prompt copy.  finalise_refinement_chat
    /// and trigger_refinement_notes_synth both re-resolve via
    /// `focused_watch_state`, so we don't need to carry the aid.
    issue_number: u64,
}

struct TestBuildJob {
    /// Work assignment id this build is verifying. Also the HashMap key.
    #[allow(dead_code)]
    work_id: String,
    /// Issue number, for friendlier toast titles (work_ids are uuids).
    issue_number: u64,
    /// Branch passed to `coord test` (carried for the completion toast).
    branch: String,
    /// `~/.coord/test-build-<id>.log` — stdout+stderr from the subprocess.
    /// Surfaced in failure toasts so the user can `tail` it.
    log_path: PathBuf,
    /// Wall-clock start; reported in the success toast as "took Ns".
    started_at: Instant,
    /// Receiver for the completion message; `try_recv` polled each tick.
    rx: std::sync::mpsc::Receiver<TestBuildOutcome>,
}

/// Outcome of a Phase 1 build. Sent once from the worker thread.
struct TestBuildOutcome {
    exit_code: i32,
    /// First non-empty stderr line (truncated). Surfaced in the failure
    /// toast so the user doesn't have to `cat` the log to see the cause.
    /// Empty string on success, since we don't bother capturing.
    first_error: String,
}

/// #271 part 2: persisted record of the most-recent Phase 1 build for
/// a work assignment.  Survives long after the 4-second toast expires,
/// so the user who walked away during a 5-minute `cargo build` returns
/// to a visible "Last build: ✓ 2m 30s" or "✗ exit 101: <first error>"
/// line in the Pipeline detail panel.
///
/// Stored on `CoordApp.last_test_builds` keyed by `work_id`.  A new
/// `spawn_test_build` for the same work id replaces the prior entry.
#[derive(Clone)]
struct TestBuildResult {
    branch: String,
    issue_number: u64,
    /// Exit code from `coord test`.  0 = success.
    exit_code: i32,
    /// First non-empty stderr line — useful one-liner for the failure
    /// case.  Empty on success.
    first_error: String,
    log_path: PathBuf,
    /// Wall-clock duration of the build.
    duration_secs: u64,
    /// When the build finished, used by the renderer to format an
    /// "Xs ago" hint.
    finished_at: Instant,
}

/// #434: Durable record of a `coord pull-artifact` run.
///
/// Stored on `CoordApp.last_artifact_pulls` keyed by `work_id`.  A new
/// pull for the same work id replaces the prior entry.
#[derive(Clone)]
struct ArtifactPullResult {
    /// Exit code from `coord pull-artifact`.  0 = success.
    exit_code: i32,
    /// On success: the local destination path (`~/.coord/artifacts/<repo>/<sanitized>/`).
    /// On failure: the first meaningful stderr line, or a generic message.
    message: String,
    /// When the pull finished, used by the renderer to format an "Xs ago" hint.
    finished_at: Instant,
}

/// #532: State for the re-openable artifact-pull info dialog.
///
/// Shown after `coord pull-artifact` completes (success or failure), and
/// re-openable at any time by pressing `a` again on the same pipeline row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct ArtifactPullDialog {
    /// The local destination path on success, or `None` on failure / absence.
    /// Only `Some` when a copy-to-clipboard action is meaningful.
    path: Option<String>,
    /// Human-readable body text rendered in the dialog.
    body: String,
}

/// #532: Outcome of a key press while the artifact-pull dialog is visible.
///
/// Pulled out so the key-intercept block in `handle()` and the unit tests
/// share the *same* matching code — fixing a typo in the key match (e.g.
/// `Escape` → `Back`) would now break tests, instead of leaving them passing
/// against a stale copy of the routing logic.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ArtifactDialogKeyOutcome {
    /// Copy the cached path to clipboard, push a confirmation toast, then dismiss.
    /// Only returned when the dialog carries a path (success case).
    CopyAndDismiss,
    /// Explicit dismiss key (Esc / Enter) — close the dialog with no side-effects.
    Dismiss,
    /// Anything else — swallow the key but keep the dialog visible.
    Swallow,
}

/// #532: True when the completing `pull-artifact` command result will be
/// surfaced by the artifact-pull dialog, so the generic "Command failed"
/// toast should be suppressed to avoid double-notifying the user.
///
/// The match is intentionally narrow: it returns false if the user has
/// kicked off a SECOND pull on a different row while the first is still
/// in flight (`pending_artifact_pull` then carries the newer work_id).
/// In that case the dialog handler will skip the stale result and the
/// generic toast is the ONLY signal the user has — losing it would mean
/// a silent failure.  Was the root cause of the iteration-2 regression
/// the reviewer flagged.
fn should_suppress_command_failed_toast(
    completed_label: &str,
    pending: Option<&(String, String, String)>,
) -> bool {
    if let Some((wid, _, _)) = pending {
        completed_label.contains(&format!("pull-artifact {}", wid))
    } else {
        false
    }
}

/// #863: true when `label` is the `coord assign --interactive --fix-of
/// <work_aid> [--force] … --dry-run` cap-preflight command for `work_aid` —
/// used by `run_periodic_work` to match a completed `CommandResult` back to
/// the in-flight `PendingFixCapPreflight` it belongs to.
fn is_fix_cap_preflight_label(label: &str, work_aid: &str) -> bool {
    label.contains("--fix-of") && label.contains(work_aid) && label.contains("--dry-run")
}

/// #1059: true when `label` is a "Dispatch Gate A mock" command
/// (`coord acceptance mock <repo> <tracking_issue>`).  A completed
/// `CommandResult` with a non-zero exit and a label matching this is routed
/// into the full-text `gate_a_error_dialog` (rather than the truncated
/// command-failed toast) so the operator can read the COMPLETE reason —
/// crucially the "no acceptance driver configured — add it under
/// acceptance.drivers…" case that otherwise reads as a permanent wedge.
///
/// Matched on the raw label (`coord acceptance mock …`) — the same label
/// `spawn_queued` records — so it never fires on an unrelated `acceptance`
/// subcommand (e.g. `acceptance run` / `acceptance record`).
pub(crate) fn is_gate_a_mock_dispatch_label(label: &str) -> bool {
    label.contains("acceptance mock ") || label.trim_end().ends_with("acceptance mock")
}

/// #532: Classify a key press while the artifact-pull dialog is open.
///
/// Pure function so tests can exhaustively drive every key path without
/// needing a Backend / ShellContext mock to call into `handle()`.
fn classify_artifact_pull_dialog_key(key: &Key, has_path: bool) -> ArtifactDialogKeyOutcome {
    match key {
        Key::Char('c') | Key::Char('C') if has_path => ArtifactDialogKeyOutcome::CopyAndDismiss,
        Key::Named(NamedKey::Escape) | Key::Named(NamedKey::Enter) => {
            ArtifactDialogKeyOutcome::Dismiss
        }
        _ => ArtifactDialogKeyOutcome::Swallow,
    }
}

/// #532: What pressing `a` on a pipeline row should do for the artifact flow.
///
/// Computed in one read-only place (`compute_a_key_artifact_action`) so the
/// production handler and the tests both route through the same decision.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum AKeyArtifactAction {
    /// A prior `coord pull-artifact` result is cached — re-open its dialog
    /// rather than re-running the pull command.
    ReopenDialog(ArtifactPullDialog),
    /// No cached result yet but the badge is visible — start a fresh pull
    /// against this work_id.  The producer fields are needed to seed
    /// `pending_artifact_pull` so the completion handler can match the label.
    StartPull {
        work_id: String,
        repo: String,
        sanitized: String,
    },
    /// No cached result, no badge — but we have a recorded #433 absence
    /// reason.  Open a dialog explaining why.
    ShowAbsence(ArtifactPullDialog),
}

/// #316 Phase A+C: pending board-chat dispatch.  Armed by
/// `dispatch_board_chat_new_issue()` / `dispatch_board_chat_refine()`; cleared
/// on bind or timeout (same poll + bind lifecycle as `PendingRefinement`).
#[derive(Clone)]
struct PendingBoardChat {
    repo: String,
    /// "new-issue-chat" or "refinement"
    assignment_type: String,
    dispatched_at: Instant,
}

/// #1017: pending milestone-chat dispatch.  Armed by the Plans-panel
/// "New milestone via chat" / "Open milestone chat" / "Add sub-issue via
/// chat…" entries after they shell `coord milestone chat …`; cleared on bind
/// or timeout.  Same poll+bind lifecycle as `PendingBoardChat` — the backend
/// records a `type="milestone-chat"` stream-json session (the refine-chat /
/// new-issue-chat family) and prints its assignment id, so the TUI can attach
/// the live chat overlay to it rather than leaving it a fire-and-forget
/// headless worker the operator can't participate in.
#[derive(Clone)]
struct PendingMilestoneChat {
    /// Coord-local repo name (matches `coordinator.yml`).
    repo: String,
    /// Tracking-issue number the chat is about, or `0` for a brand-new
    /// milestone that has no tracking issue yet — the backend's
    /// `issue_number=0` sentinel (`dispatch_new_milestone_chat`).
    issue_number: u64,
    /// Human-readable label for the bind toast / chat status strip.
    label: String,
    /// Wall-clock dispatch instant — bounds the wait at `REFINEMENT_BIND_TIMEOUT`.
    dispatched_at: Instant,
}

/// #2533 (ms-67 contract §4c/§4d): pending decomposition-chat dispatch.
/// Armed by `dispatch_approved_pull_into_decomposition` right after `coord
/// portal decompose-chat <submission_id>` is shelled out; cleared on bind or
/// timeout. Same poll+bind lifecycle as `PendingBoardChat`/
/// `PendingMilestoneChat` — the backend records a `type="decomposition-chat"`
/// stream-json session and prints its assignment id, so the TUI can attach
/// the live chat overlay to it once the assignment appears.
///
/// Unlike its two siblings, the "chat ready — type to start" toast fires at
/// ARM time, not at bind time (contract §4d: "a toast fires immediately on
/// dispatch") — `dispatch_approved_pull_into_decomposition` pushes it
/// directly rather than leaving it to `maybe_bind_pending_decomposition_chat`.
#[derive(Clone)]
struct PendingDecompositionChat {
    /// The portal submission id being decomposed — the match key
    /// `maybe_bind_pending_decomposition_chat` looks for on the freshly
    /// dispatched assignment (contract §4c leaves the exact bind key
    /// unpinned; this module picks `assignment.repo` carrying the
    /// submission id as a sentinel-free, session-scoped value — see that
    /// function's own doc comment).
    submission_id: String,
    /// Wall-clock dispatch instant — bounds the wait at `REFINEMENT_BIND_TIMEOUT`.
    dispatched_at: Instant,
}

/// #353: pending repo picker for the [Add] button on the Board panel.
/// When multiple repos exist, this shows a numeric picker (1, 2, …) for
/// the user to select which repo to open a refine-board chat for.
/// Armed when the user clicks the [Add] button and there are multiple repos.
/// Cleared when the user selects a repo (numeric key), cancels (Esc), or
/// the selection timeout expires.
#[derive(Clone)]
#[allow(dead_code)]
struct PendingRepoPicker {
    /// List of available repos (from `board_repo_names`).
    repos: Vec<String>,
    /// Currently selected index (0-based). Used for visual feedback if desired.
    selected: Option<usize>,
    /// Time the picker was opened.
    opened_at: Instant,
}

/// #486 Leg 4: pending fleet-machine picker for an interactive launch.
///
/// Armed by `launch_interactive_session_for_selected_issue` (Review/Fix only)
/// when more than one fleet machine can run the selected issue's repo, so the
/// operator can dispatch a remote review/fix over ssh+tmux from a board card
/// instead of always launching locally.  A numeric key (1, 2, …) picks the
/// machine and immediately launches; Esc cancels.  Mirrors `PendingRepoPicker`.
#[derive(Clone)]
struct PendingMachinePicker {
    /// Which interactive flavour to launch once a machine is chosen.
    mode: InteractiveLaunchMode,
    /// Candidate machines, local first, then reachable, then by name.
    machines: Vec<MachinePickEntry>,
}

/// One selectable machine in `PendingMachinePicker`.
#[derive(Clone)]
pub(crate) struct MachinePickEntry {
    /// Coordinator machine NAME (the `coord assign` positional, not the host).
    name: String,
    /// Tailscale FQDN — shown so the operator knows where the ssh lands.
    host: String,
    /// Live agent reachability (TCP/health probe) at board-build time.
    reachable: bool,
    /// True when this is the coordinator's own machine (launches on the TTY).
    is_local: bool,
}

/// #954: pending optional-name prompt for a new terminal about to be
/// created on `machine`. Armed by `begin_new_terminal_name_prompt` once a
/// machine is chosen (picker selection, or the single-machine fast path).
/// `buf` accumulates the operator's typed name; Enter submits via
/// `create_and_attach_terminal` (an empty buffer means "auto-generate a
/// slug"); Esc cancels. Mirrors the `pending_plan_capture` single-buffer
/// text-input pattern.
#[derive(Clone)]
pub(crate) struct PendingNewTerminal {
    /// Coordinator machine NAME the terminal will be created on.
    machine: String,
    /// Operator-typed name buffer, empty until they type something.
    buf: String,
}

/// #2147: pending "Set quiet hours…" dialog for `machine`, armed by the
/// Machines-panel right-click menu (`machine-quiet-hours-set`). Mirrors
/// `PendingNewTerminal`'s single-buffer text-input shape exactly — ONE text
/// field carries the whole window (`HH:MM-HH:MM [tz]`, tz optional and
/// space-separated), pre-filled with the machine's current schedule (from
/// `quiet_hours_windows`) or `22:00-08:00` when none is set. Enter/Submit
/// hands the buffer to `submit_quiet_hours`, which re-arms this (buffer
/// intact) on a parse error rather than closing the dialog. Esc/Cancel
/// discards with no spawn.
#[derive(Clone)]
pub(crate) struct PendingQuietHours {
    /// Coordinator machine NAME the window applies to.
    machine: String,
    /// Operator-typed buffer: "HH:MM-HH:MM" or "HH:MM-HH:MM Zone/Name".
    buf: String,
}

/// #935 Part B: pending "Diagnose & fix stage…" results dialog.
///
/// Populated from the `DIAGNOSE_JSON:` output of a `coord diagnose --json
/// --dry-run` command.  `build_prompt_dialog()` renders it as an option dialog;
/// `fire_dialog_button()` dispatches the chosen action.
#[derive(Clone)]
struct PendingDiagnoseDialog {
    /// Coord-local repo name (e.g. "api").
    repo: String,
    /// GitHub issue number.
    issue_number: u64,
    /// Resolved stage (e.g. "work", "review").
    stage: String,
    /// Human-readable findings from the dry-run pass.
    findings: Vec<String>,
    /// Actions the full recovery run WOULD take (from the dry-run result).
    actions_taken: Vec<String>,
    /// True when the dry-run says a manual reset is still needed.
    needs_reset: bool,
    /// True when ≥1 `live_tmux_sessions` entry is a `"pending-"` entry for
    /// this (repo, issue) — i.e. a phantom live session may be present.
    has_phantom_session: bool,
    /// True when this dialog was built from a best-effort parse of the
    /// legacy human-readable `coord diagnose` output (findings `·` lines,
    /// actions `✓` lines, and the `DIAGNOSE_RESULT:` trailer) because the
    /// daemon's response had no `DIAGNOSE_JSON:` line — i.e. a version-skewed
    /// (pre-#935) daemon on the other end. The dialog still opens so the
    /// operator gets Recover/Reset/Clear-phantom options instead of a bare
    /// toast; the body notes the degraded source (#935 follow-up).
    legacy: bool,
}

/// #316 Phase B: state for the file-issue finaliser modal.
/// Shown when the user confirms filing a new issue drafted by a new-issue-chat.
///
/// **Current scope: preview-only.**  The modal renders the parsed title + body
/// for confirmation but does not yet accept edits — Ctrl+Y files via
/// `gh issue create`, Esc cancels.  Inline editing is tracked separately and
/// will reuse the existing notes-modal text-edit primitives when wired up.
#[derive(Clone)]
struct FileIssueModal {
    /// Parsed title from the `TITLE: …` line in the chat transcript.
    title: String,
    /// Issue body text (everything after the `---` separator).
    body: String,
    /// GitHub `owner/name` slug for `--repo`.
    repo_github: String,
    /// True while `gh issue create` is in flight.
    submitting: bool,
}

/// Result sent from the background `gh issue create` thread.
struct FileIssuePostResult {
    success: bool,
    issue_url: String,
    stderr_first_line: String,
}

// ─── Sidebar views ────────────────────────────────────────────────────────────

// ─── App data model ───────────────────────────────────────────────────────────

// ─── #207: Machine metrics (CPU + memory) ───────────────────────────────────

// ─── #336: Artifact manifest types ──────────────────────────────────────────

/// An issue grouped with all its assignments and a summary status.
#[derive(Clone)]
struct IssueGroup {
    issue_number: u64,
    issue_title: String,
    assignments: Vec<Assignment>,
    /// Derived summary: "running", "failed", "done", "merged", "pending"
    status_summary: String,
    /// #265: whether the underlying GitHub issue is closed.  Populated
    /// from `data.open_issues` (which carries both `open` and `closed`
    /// states).  Drives the In-flight vs Completed section split.
    /// Defaults to false when the issue isn't in the cache.
    is_closed: bool,
    /// #265 / #257 fix: whether the brain has the issue as currently
    /// `state="open"` in the local cache.  Distinguishes "open and
    /// known active" from "not in cache at all" (the latter typically
    /// means the issue was closed long enough ago that the brain has
    /// pruned its row — see `coord/state.py::upsert_open_issues`,
    /// which only retains closed rows for 7 days).
    ///
    /// Without this distinction, every historical merged/done
    /// assignment on a long-closed issue routes to In-flight, swamping
    /// the lifecycle ledger with ancient work.
    has_open_record: bool,
    /// #226: GitHub labels (e.g. `status:refining`, `status:ready`,
    /// `coord`).  Populated from `data.open_issues`.  Empty for issues
    /// that aren't in the cache.  Drives the Backlog / Refining /
    /// Refined split inside the Pending bucket.
    labels: Vec<String>,
    /// #406: GitHub milestone number.  `None` for unassigned issues.
    /// Used by the Board sidebar to group issues under milestone headers.
    milestone_number: Option<i64>,
    /// #406: GitHub milestone title (e.g. "v0.5").  `None` when no milestone
    /// is assigned.  Displayed in the milestone group header.
    milestone_title: Option<String>,
}

/// #337: returns `true` for assignment types that represent real work
/// dispatched to a worker — `work`, `review`, `smoke`, `conflict-fix`,
/// and any `fix-N` variant.  Chat/scoping types (`refinement`,
/// `test-chat`, `new-issue-chat`) return `false`.
///
/// #1059: also `mock-author` (Gate A's mock-author, #930) — it dispatches
/// through the same Work → Test → Review → Merge pipeline as any other
/// branch (`coord/mock_author.py`'s docstring: "Gate A produces a normal
/// reviewed commit, not a special-cased one"), mirroring Python's
/// `coord.models.WORK_LIKE_TYPES`. Without it, an epic row with an active
/// Gate-A dispatch stayed classified "new" instead of "in-progress",
/// hiding Watch/Stop.
///
/// Used by `IssueGroup::lifecycle_section` so that an issue that only
/// has chat-type assignments still lands in Backlog / Refining / Refined
/// rather than being dragged into In-flight.
fn is_workable_type(ty: &str) -> bool {
    // #1553: "test-author" belongs here for the same reason "mock-author"
    // already does — both are pipeline execution, not a scoping conversation,
    // and Python agrees (`coord.models.WORK_LIKE_TYPES` is
    // {"work", "mock-author", "test-author"}). Its omission was half of why a
    // child issue with three live acceptance-authoring rounds classified as
    // Pending: even after the row was attributed to the child (see
    // `Assignment::effective_issue_number`), the type itself didn't count as
    // work. `[fix-N]` bounces of a slice reuse `type="test-author"` too
    // (`coord.auto_loop._dispatch_fix`), so they ride along on this arm.
    matches!(
        ty,
        "work" | "mock-author" | "test-author" | "review" | "smoke" | "conflict-fix"
    ) || ty.starts_with("fix-")
}

impl IssueGroup {
    #[allow(dead_code)]
    fn status_color(&self) -> Color {
        match self.status_summary.as_str() {
            "running" => Color::rgb(80, 220, 80),
            "failed" => Color::rgb(220, 70, 70),
            "done" => Color::rgb(120, 180, 120),
            "merged" => Color::rgb(100, 180, 240),
            _ => Color::rgb(140, 140, 160),
        }
    }

    /// #256 / #226 lifecycle bucketing.  Returns the section key the
    /// row belongs in:
    /// - `"backlog"` — no assignments, no `status:*` label
    /// - `"refining"` — no assignments, `status:refining` label
    /// - `"refined"` — no assignments, `status:ready` label
    /// - `"in-flight"` — has at least one open assignment
    /// - `"completed"` — closed issue + assignments (or stale settled
    ///   assignment with no open cache record)
    ///
    /// Rules (in order):
    /// - No real-work assignments → split into Backlog/Refining/Refined by label.
    /// - Issue cache says closed → **Completed**.
    /// - Issue is `merged` (PR closed the issue via `fixes #N`) →
    ///   **Completed**, even when the brain hasn't synced yet.
    /// - Issue has no open-cache record AND assignment is settled
    ///   (`done`/`merged`) → **Completed**.
    /// - Otherwise (with real-work assignments) → **In-flight**.
    fn lifecycle_section(&self) -> &'static str {
        // #337 / #264: chat/scoping assignment types (`refinement`,
        // `test-chat`, `new-issue-chat`) are conversational — they must
        // not drag an issue out of the Backlog/Refining/Refined buckets
        // into In-flight.  Only workable types count (see
        // `is_workable_type`).  None defaults to "work" which is workable.
        let has_real_work = self.assignments.iter().any(|a| {
            a.assignment_type
                .as_deref()
                .map(is_workable_type)
                .unwrap_or(true) // None → "work" → workable
        });
        if !has_real_work {
            // #628: a pre-work issue is just Backlog. `status:ready` gates
            // nothing (no dispatch/plan/merge path reads it), so it no longer
            // splits a separate "Refined" bucket — and `status:refining` was
            // eliminated too. The Board is Backlog → In-flight → Completed; the
            // Pipeline (coord-tracked) is the work queue.
            return "backlog";
        }
        if self.is_closed {
            return "completed";
        }
        if self.status_summary == "merged" {
            return "completed";
        }
        if !self.has_open_record && matches!(self.status_summary.as_str(), "done" | "merged") {
            return "completed";
        }
        "in-flight"
    }
}

/// #259: state of an open right-click context menu.  `None` on
/// `CoordApp.pending_context_menu` means no menu is showing.
#[derive(Clone, Debug)]
pub(crate) struct ContextMenuState {
    items: Vec<ContextMenuItem>,
    /// Anchor point — where the right-click landed.
    anchor: Point,
    /// Keyboard-selected item index in the root menu.  Maintained skipping
    /// separators and disabled items.
    selected_idx: usize,
    /// What the menu is "about" — used by `dispatch_context_menu_action`
    /// to route the action with row-specific data.
    target: ContextMenuTarget,
    /// #607: depth-first path of open submenus.  `submenu_path[d]` = the
    /// `item_idx` in the menu at depth `d` whose child submenu is currently
    /// open.  Empty ⟹ only the root menu is showing.
    submenu_path: Vec<usize>,
    /// #607: selected item index at each submenu depth.
    /// `submenu_selected[d]` is the selection inside the submenu opened by
    /// `submenu_path[d]`.
    submenu_selected: Vec<usize>,
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/// Format a Unix timestamp as a human-readable "X ago" string relative to `now`.
///
/// Used in the Merge Queue panel (#777) to show `enqueued_at` and `last_attempt`,
/// and (#2133) in the Queue panel to age-stamp a drive-queue entry's
/// `last_reason` so a snapshot taken hours ago never renders as current state.
/// Returns `""` for `None` timestamps.  Precision caps at minutes for < 1h,
/// hours for < 24h, days otherwise — keeps panel rows terse.
pub(crate) fn format_age(ts: Option<f64>, now: f64) -> String {
    let ts = match ts {
        Some(t) if t > 0.0 => t,
        _ => return String::new(),
    };
    let secs = (now - ts).max(0.0) as u64;
    if secs < 60 {
        format!("{}s ago", secs)
    } else if secs < 3600 {
        format!("{}m ago", secs / 60)
    } else if secs < 86400 {
        format!("{}h ago", secs / 3600)
    } else {
        format!("{}d ago", secs / 86400)
    }
}

/// #541: Build styled spans for `text` with per-character fuzzy-match highlights.
///
/// Characters at positions returned by `fuzzy_score(query, text)` are coloured
/// with `match_fg`; all other characters use `normal_fg`.  Returns a single
/// span in `normal_fg` when the query is empty, the text is empty, or no
/// subsequence match exists (fall-back: caller already filtered to matches).
fn styled_match_spans(
    query: &str,
    text: &str,
    normal_fg: Color,
    match_fg: Color,
) -> Vec<StyledSpan> {
    if query.is_empty() || text.is_empty() {
        return vec![StyledSpan::with_fg(text, normal_fg)];
    }
    let Some((_score, positions)) = fuzzy_score(query, text) else {
        // No match in this sub-string (possible when query matched across
        // the number+title boundary but not in title alone).  Fall back to
        // uniform colouring.
        return vec![StyledSpan::with_fg(text, normal_fg)];
    };
    if positions.is_empty() {
        return vec![StyledSpan::with_fg(text, normal_fg)];
    }
    // Build a set of matched char positions for O(1) lookup.
    let pos_set: std::collections::HashSet<usize> = positions.into_iter().collect();
    let mut spans: Vec<StyledSpan> = Vec::new();
    let mut current = String::new();
    let mut current_is_match = false;
    for (i, c) in text.chars().enumerate() {
        let is_match = pos_set.contains(&i);
        if is_match != current_is_match && !current.is_empty() {
            spans.push(StyledSpan::with_fg(
                std::mem::take(&mut current),
                if current_is_match { match_fg } else { normal_fg },
            ));
        }
        current_is_match = is_match;
        current.push(c);
    }
    if !current.is_empty() {
        spans.push(StyledSpan::with_fg(
            current,
            if current_is_match { match_fg } else { normal_fg },
        ));
    }
    spans
}

/// Build a key-value `ListItem` for the detail panel.
fn kv_item(key: &str, val: &str, val_color: Option<Color>) -> ListItem {
    let key_color = Color::rgb(130, 170, 210);
    let text = if key.is_empty() {
        // Blank line or plain value row (e.g. title text)
        StyledText {
            spans: vec![StyledSpan::with_fg(val, Color::rgb(210, 210, 210))],
        }
    } else {
        StyledText {
            spans: vec![
                StyledSpan::with_fg(format!(" {:12} ", key), key_color),
                StyledSpan::with_fg(val, val_color.unwrap_or(Color::rgb(210, 210, 210))),
            ],
        }
    };
    ListItem {
        text,
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    }
}

/// #1084: label + colour for one oracle-loop pre-req stage
/// ([`crate::app::pipeline::PrereqStage`]) — mirrors the colour palette the
/// rest of the Pipeline detail panel uses for [`StageStatus`] (Done/Failed
/// green/red; Active a distinct blue since it's genuinely different from a
/// settled verdict; Pending/Skipped/Stale muted).
///
/// #1572: a `Pending` stage whose `since` is `None` has never been
/// reached — its predecessor hasn't settled, so nothing has been
/// dispatched and nothing is being waited on (see the doc comment on
/// [`crate::app::pipeline::PrereqStage::since`]). Labelling that "not
/// started" rather than "pending" keeps it visually distinct from a
/// `Pending` stage that IS reachable but stalled, which `since: Some(_)`
/// pairs with a mounting "waiting Xm Ys" clock in
/// `append_prereq_pipeline_rows` — a running clock should always mean
/// "this should be moving," never "nothing has happened yet."
fn prereq_stage_label(stage: &PrereqStage) -> (&'static str, Color) {
    match (&stage.status, stage.since) {
        (StageStatus::Done, _) => ("done", Color::rgb(120, 200, 120)),
        (StageStatus::Active, _) => ("running", Color::rgb(120, 170, 220)),
        (StageStatus::Failed, _) => ("failed", Color::rgb(220, 100, 100)),
        (StageStatus::Stale, _) => ("stale", Color::rgb(220, 150, 80)),
        (StageStatus::Skipped, _) => ("skipped", Color::rgb(160, 160, 180)),
        (StageStatus::Pending, None) => ("not started", Color::rgb(110, 110, 125)),
        (StageStatus::Pending, Some(_)) => ("pending", Color::rgb(160, 160, 180)),
    }
}

/// #1084: `mm:ss`-formatted wall-clock elapsed time since Unix timestamp
/// `since`, for the pre-req pipeline's "running Xm Ys" / "waiting Xm Ys"
/// annotations.
fn elapsed_since_now(since: f64) -> String {
    let now_secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(since);
    fmt_elapsed((now_secs - since).max(0.0) as u64)
}

// ─── Activity log parsing ─────────────────────────────────────────────────────

/// Build a plain `ListItem` for the Activity feed.
fn activity_item(text: &str, color: Color) -> ListItem {
    ListItem {
        text: StyledText {
            spans: vec![StyledSpan::with_fg(text, color)],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    }
}

/// #558: Build a minimal single-item `ListView` with a plain text message.
/// Used for loading / empty states in the Summary tab.
fn plain_list(id: &str, msg: &str, scroll_offset: usize) -> ListView {
    let item = ListItem {
        text: StyledText {
            spans: vec![StyledSpan::with_fg(msg, Color::rgb(160, 160, 160))],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    };
    ListView {
        id: WidgetId::new(id),
        title: None,
        items: vec![item],
        selected_idx: 0,
        scroll_offset,
        has_focus: false,
        bordered: true,
        h_scroll: 0,
        max_content_width: None,
        show_v_scrollbar: false,
    }
}

/// #558: Build the `ListView` for the Pipeline Summary tab from a list of
/// `SessionSummary` entries (already sorted newest-first).
///
/// Each session renders as:
///   ● <type>  <machine>  <status/verdict (coloured)>
///      "<summary text wrapped>"
#[cfg(test)]
fn build_summary_list_view(summaries: Vec<SessionSummary>, scroll_offset: usize) -> ListView {
    if summaries.is_empty() {
        return plain_list(
            "pipeline-summary",
            "  No session summaries yet for this issue.",
            scroll_offset,
        );
    }

    let mut items: Vec<ListItem> = Vec::new();
    let dim = Color::rgb(120, 120, 120);
    let muted = Color::rgb(170, 170, 170);
    let machine_color = Color::rgb(130, 170, 210);

    for s in &summaries {
        // Header row: ● <type>  <machine>  <status/verdict>
        let (badge_text, badge_color) = if let Some(v) = &s.verdict {
            match v.as_str() {
                "approve" => ("approve ✓", Color::rgb(120, 200, 120)),
                "request-changes" => ("request-changes ✗", Color::rgb(220, 100, 100)),
                other => (other, Color::rgb(200, 200, 70)),
            }
        } else {
            match s.status.as_str() {
                "done" => ("done", Color::rgb(120, 120, 120)),
                "failed" => ("failed", Color::rgb(220, 70, 70)),
                "advisory" => ("advisory", Color::rgb(200, 200, 70)),
                other => (other, Color::rgb(200, 200, 70)),
            }
        };

        let type_label = session_type_label(&s.session_type);

        let header = ListItem {
            text: StyledText {
                spans: vec![
                    StyledSpan::with_fg("● ", muted),
                    StyledSpan::with_fg(format!("{:<10}", type_label), muted),
                    StyledSpan::with_fg(format!("  {:<14}", s.machine), machine_color),
                    StyledSpan::with_fg(format!("  {}", badge_text), badge_color),
                ],
            },
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        };
        items.push(header);

        // Summary text (may be multi-line; each line indented).
        if !s.summary_text.is_empty() {
            for line in s.summary_text.lines() {
                let trimmed: String = line.chars().take(200).collect();
                if trimmed.is_empty() {
                    continue;
                }
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!("   {}", trimmed),
                            Color::rgb(200, 200, 200),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Normal,
                });
            }
        }

        // Blank separator between sessions.
        items.push(ListItem {
            text: StyledText { spans: vec![StyledSpan::with_fg(" ", dim)] },
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        });
    }

    ListView {
        id: WidgetId::new("pipeline-summary"),
        title: None,
        items,
        selected_idx: 0,
        scroll_offset,
        has_focus: false,
        bordered: true,
        h_scroll: 0,
        max_content_width: None,
        show_v_scrollbar: false,
    }
}

/// Human-readable label for a session type string.
fn session_type_label(t: &str) -> &str {
    match t {
        "work" => "Work",
        "review" => "Review",
        "fix" => "Fix",
        "plan" => "Plan",
        "re-review" | "rereview" => "Re-review",
        "refinement" => "Refinement",
        "conflict-fix" => "Conflict-fix",
        "smoke" => "Smoke",
        "audit" => "Audit",
        other => other,
    }
}

/// Minimal JSON string-field extractor.
///
/// Finds the first occurrence of `"field":"value"` in `json` and returns
/// the unescaped string value. Returns `None` if the field is absent or
/// its value is not a quoted string. Only handles compact (no-space) JSON,
/// which is what `claude -p --output-format stream-json` emits.
fn json_str(json: &str, field: &str) -> Option<String> {
    let key = format!("\"{}\":\"", field);
    let start = json.find(&key)? + key.len();
    let rest = &json[start..];
    let mut result = String::new();
    let mut chars = rest.chars();
    loop {
        match chars.next()? {
            '"' => break,
            '\\' => match chars.next()? {
                'n' => result.push(' '),
                't' => result.push(' '),
                'r' => {}
                c => result.push(c),
            },
            c => result.push(c),
        }
    }
    Some(result)
}

/// Minimal JSON numeric-field extractor (handles integers and floats).
fn json_num(json: &str, field: &str) -> Option<f64> {
    // Match `"field":NUMBER` — value is terminated by , } or whitespace.
    let key = format!("\"{}\":", field);
    let pos = json.find(&key)? + key.len();
    let rest = json[pos..].trim_start();
    // Skip null / boolean / quoted values.
    if rest.starts_with('"') || rest.starts_with("null") || rest.starts_with("true") {
        return None;
    }
    let end = rest
        .find(|c: char| c == ',' || c == '}' || c == ']' || c.is_whitespace())
        .unwrap_or(rest.len());
    rest[..end].parse::<f64>().ok()
}

/// Return all tool names found in `"type":"tool_use"` blocks within `json`.
///
/// Searches for each `"type":"tool_use"` marker and extracts the `"name"`
/// field from the same JSON object. Works for both top-level `tool_use`
/// events and tool-use blocks nested inside `assistant` message content.
fn extract_tool_names(json: &str) -> Vec<String> {
    let marker = "\"type\":\"tool_use\"";
    let mut names: Vec<String> = Vec::new();
    let mut pos = 0;
    while let Some(found) = json[pos..].find(marker) {
        let after = pos + found + marker.len();
        // "name" should appear within the next ~200 bytes of the same object.
        // Round window_end up to the next UTF-8 char boundary so we don't slice
        // through a multi-byte glyph like `─` (3 bytes) and panic. Crash repro
        // was a worker for #218 whose Edit tool input contained box-drawing
        // characters that landed exactly on the 200-byte boundary.
        let mut window_end = (after + 200).min(json.len());
        while window_end < json.len() && !json.is_char_boundary(window_end) {
            window_end += 1;
        }
        let window = &json[after..window_end];
        if let Some(name) = json_str(window, "name") {
            if !name.is_empty() && !names.contains(&name) {
                names.push(name);
            }
        }
        pos = after;
    }
    names
}

/// Return the display-detail string for a named tool given the JSON fragment
/// of the tool's object (or enclosing event). Maps tool name → relevant field.
/// Used by both the top-level `tool_use` event handler and `extract_tool_calls`
/// so the two code paths stay in sync.
fn tool_detail(name: &str, json: &str) -> String {
    match name {
        "Bash" => json_str(json, "command").unwrap_or_default(),
        "Edit" | "Write" | "Read" | "Glob" | "NotebookEdit" => {
            json_str(json, "file_path").unwrap_or_default()
        }
        "Grep" => json_str(json, "pattern").unwrap_or_default(),
        _ => String::new(),
    }
}

/// Extract (name, detail) pairs from every `tool_use` block found in `json`.
///
/// Unlike `extract_tool_names`, this returns one entry *per occurrence* (not
/// deduplicated) and includes the per-tool detail string produced by
/// `tool_detail`.  Used to render one `→ Name: detail` line per tool call in
/// assistant events that contain only tool-use content blocks.
fn extract_tool_calls(json: &str) -> Vec<(String, String)> {
    let marker = "\"type\":\"tool_use\"";
    let mut calls: Vec<(String, String)> = Vec::new();
    let mut pos = 0;
    while let Some(found) = json[pos..].find(marker) {
        let after = pos + found + marker.len();
        // Search in the next ~400 bytes for name + detail fields.
        // Round up to the next UTF-8 char boundary (same safety measure as
        // extract_tool_names — box-drawing chars are 3 bytes each and can land
        // exactly on a fixed-size boundary).
        let mut window_end = (after + 400).min(json.len());
        while window_end < json.len() && !json.is_char_boundary(window_end) {
            window_end += 1;
        }
        let window = &json[after..window_end];
        let name = json_str(window, "name").unwrap_or_else(|| "?".to_string());
        let detail = tool_detail(&name, window);
        calls.push((name, detail));
        pos = after;
    }
    calls
}

/// Parse the `REVIEW_VERDICT` / `REVIEW_BODY` block embedded in a result event's
/// `result` string and return it as renderable list items. Returns an empty
/// Vec when the result isn't a structured review (e.g. a normal work or plan
/// completion).
///
/// The expected format is the one declared in the REVIEWER_SYSTEM_PROMPT:
///
/// ```text
/// REVIEW_VERDICT: approve | request-changes
/// REVIEW_BODY:
/// <markdown body lines>
/// END_REVIEW
/// ```
///
/// The body comes back with `\n` escape sequences (JSON-encoded). We
/// un-escape `\\n` → `\n` and `\\"` → `"` before splitting into lines so the
/// rendered output matches the verbatim review.
fn extract_review_items(line: &str) -> Vec<ListItem> {
    let mut items: Vec<ListItem> = Vec::new();
    // Find the verdict marker. The result text is itself JSON-encoded inside
    // the result field, so `\n` appears as the two-char sequence `\\n`.
    let verdict_marker = "REVIEW_VERDICT:";
    let body_marker = "REVIEW_BODY:";
    let end_marker = "END_REVIEW";

    let v_pos = line.find(verdict_marker);
    let b_pos = line.find(body_marker);
    let (Some(v_pos), Some(b_pos)) = (v_pos, b_pos) else {
        return items;
    };

    // Verdict word: between the verdict marker and the next newline marker.
    let verdict_after = &line[v_pos + verdict_marker.len()..];
    let verdict_end = verdict_after
        .find("\\n")
        .or_else(|| verdict_after.find('\n'))
        .unwrap_or(verdict_after.len());
    let verdict = verdict_after[..verdict_end].trim().trim_matches(',');

    // Header line, coloured by outcome.
    let color = if verdict == "approve" {
        Color::rgb(100, 220, 130)
    } else if verdict.contains("request-changes") || verdict.contains("fail") {
        Color::rgb(230, 130, 80)
    } else {
        Color::rgb(180, 180, 100)
    };
    items.push(activity_item(&format!("[review] {}", verdict), color));

    // Body: between the body marker and END_REVIEW (if present).
    let body_after = &line[b_pos + body_marker.len()..];
    let body_end_rel = body_after.find(end_marker).unwrap_or(body_after.len());
    let raw_body = &body_after[..body_end_rel];

    // Un-escape the JSON-encoded body so newlines / quotes render normally.
    let mut unescaped = String::with_capacity(raw_body.len());
    let mut chars = raw_body.chars();
    while let Some(c) = chars.next() {
        if c == '\\' {
            match chars.next() {
                Some('n') => unescaped.push('\n'),
                Some('t') => unescaped.push('\t'),
                Some('"') => unescaped.push('"'),
                Some('\\') => unescaped.push('\\'),
                Some(other) => {
                    unescaped.push('\\');
                    unescaped.push(other);
                }
                None => unescaped.push('\\'),
            }
        } else {
            unescaped.push(c);
        }
    }
    // Skip the leading newline(s) right after `REVIEW_BODY:` so the first
    // rendered line is meaningful content rather than a blank row.
    let body = unescaped.trim_start_matches('\n');
    for body_line in body.lines() {
        items.push(activity_item(
            &format!("  {}", body_line),
            Color::rgb(200, 200, 210),
        ));
    }
    items
}

/// Extract the first non-empty text block from an assistant message.
///
/// Looks for `"type":"text"` content blocks and returns the `"text"` field.
/// Returns an empty string if no text block is found.
fn extract_text_block(json: &str) -> String {
    let marker = "\"type\":\"text\"";
    if let Some(pos) = json.find(marker) {
        let after = &json[pos + marker.len()..];
        if let Some(text) = json_str(after, "text") {
            return text;
        }
    }
    String::new()
}

/// Extract the first `thinking` block's text from an assistant message.
///
/// Reasoning-only turns (`"type":"thinking"` with no `text`/`tool_use` block)
/// otherwise render as a bare "Turn N" with no visible output (#302 "empty
/// turns"). Returns an empty string if no thinking block is found.
fn extract_thinking_block(json: &str) -> String {
    let marker = "\"type\":\"thinking\"";
    if let Some(pos) = json.find(marker) {
        let after = &json[pos + marker.len()..];
        if let Some(text) = json_str(after, "thinking") {
            return text;
        }
    }
    String::new()
}

fn parse_json_event(line: &str, turn_n: &mut usize) -> Option<ListItem> {
    parse_json_event_inner(line, turn_n, None)
}

fn parse_json_event_inner(
    line: &str,
    turn_n: &mut usize,
    elapsed: Option<std::time::Duration>,
) -> Option<ListItem> {
    let type_val = json_str(line, "type")?;
    match type_val.as_str() {
        "system" => {
            let subtype = json_str(line, "subtype").unwrap_or_default();
            if subtype == "init" {
                let model = json_str(line, "model").unwrap_or_else(|| "?".to_string());
                return Some(activity_item(
                    &format!("[init] {}", model),
                    Color::rgb(100, 100, 180),
                ));
            }
            // Skip task_started / task_completed / other system subtypes.
            None
        }

        "assistant" => {
            *turn_n += 1;
            let n = *turn_n;

            // Check for STATUS: / STUCK: inside the text block first.
            let text = extract_text_block(line);
            if let Some(idx) = text.find("STATUS:") {
                let rest = &text[idx..];
                let end = rest.find('\n').unwrap_or(rest.len());
                let trimmed = rest[..end].trim();
                return Some(activity_item(trimmed, Color::rgb(80, 210, 80)));
            }
            if let Some(idx) = text.find("STUCK:") {
                let rest = &text[idx..];
                let end = rest.find('\n').unwrap_or(rest.len());
                let trimmed = rest[..end].trim();
                return Some(activity_item(trimmed, Color::rgb(220, 120, 50)));
            }

            // Summarise tool calls in this turn.
            let tools = extract_tool_names(line);
            let elapsed_str = match elapsed {
                Some(d) if d.as_secs() >= 1 => format!("  +{}s", d.as_secs()),
                _ => String::new(),
            };
            // #302: don't clip the turn text at 60 chars or debug-quote it —
            // the Log tab is horizontally scrollable, so show the assistant's
            // text in full on one line (newlines collapsed to spaces so it
            // stays a single scrollable row). Show BOTH text and tools when a
            // turn has both, so a turn that thinks *and* calls a tool isn't
            // rendered as bare "Turn N" with no visible output.
            let text_line = collapse_ws(&text);
            let summary = match (!text_line.is_empty(), !tools.is_empty()) {
                (true, true) => format!(
                    "[assistant] Turn {}{}  {}  tool_use={}",
                    n,
                    elapsed_str,
                    text_line,
                    tools.join(",")
                ),
                (true, false) => {
                    format!("[assistant] Turn {}{}  {}", n, elapsed_str, text_line)
                }
                (false, true) => format!(
                    "[assistant] Turn {}{}  tool_use={}",
                    n,
                    elapsed_str,
                    tools.join(",")
                ),
                (false, false) => {
                    // No text block and no tool call — a reasoning-only turn.
                    // Surface the thinking text so the row isn't blank (#302).
                    let thinking = collapse_ws(&extract_thinking_block(line));
                    if thinking.is_empty() {
                        format!("[assistant] Turn {}{}", n, elapsed_str)
                    } else {
                        format!("[assistant] Turn {}{}  💭 {}", n, elapsed_str, thinking)
                    }
                }
            };
            Some(activity_item(&summary, Color::rgb(150, 180, 240)))
        }

        "tool_use" => {
            let name = json_str(line, "name").unwrap_or_else(|| "?".to_string());
            let detail = match name.as_str() {
                "Bash" => json_str(line, "command").unwrap_or_default(),
                "Edit" | "Write" | "Read" | "Glob" | "NotebookEdit" => {
                    json_str(line, "file_path").unwrap_or_default()
                }
                _ => String::new(),
            };
            let text = if detail.is_empty() {
                format!("[tool] {}", name)
            } else {
                format!("[tool] {}: {}", name, detail)
            };
            Some(activity_item(&text, Color::rgb(180, 150, 220)))
        }

        "result" => {
            let turns = json_num(line, "num_turns").unwrap_or(0.0) as u64;
            let cost = json_num(line, "total_cost_usd").unwrap_or(0.0);
            let stop = json_str(line, "stop_reason").unwrap_or_else(|| "?".to_string());
            let dur_ms = json_num(line, "duration_ms").unwrap_or(0.0) as u64;
            let dur = fmt_dur(dur_ms / 1000);
            let text = format!(
                "[result] {} turns  ${:.2}  {}  stop={}",
                turns, cost, dur, stop
            );
            // Review-verdict body extraction happens in parse_log_content so
            // it can emit multiple list items below the metrics line.
            Some(activity_item(&text, Color::rgb(200, 200, 100)))
        }

        "rate_limit_event" => Some(activity_item("[rate_limit]", Color::rgb(220, 150, 50))),

        _ => None,
    }
}

/// Parse log content (NDJSON stream-json or plain text) into displayable `ListItem`s.
///
/// Handles both stream-json logs (NDJSON from `claude -p --output-format
/// stream-json`) and plain-text logs (stdout of older workers).
fn parse_log_content(content: &str) -> Vec<ListItem> {
    // Detect format: stream-json if the first non-comment non-blank line
    // starts with `{`.
    let is_json = content
        .lines()
        .find(|l| !l.starts_with('#') && !l.trim().is_empty())
        .map(|l| l.trim_start().starts_with('{'))
        .unwrap_or(false);

    let mut items: Vec<ListItem> = Vec::new();
    let mut turn_n: usize = 0;

    for line in content.lines() {
        if line.starts_with('#') || line.trim().is_empty() {
            continue;
        }

        if is_json {
            if let Some(item) = parse_json_event(line, &mut turn_n) {
                items.push(item);
            }
            // After the metrics line, surface the structured review verdict
            // (REVIEW_VERDICT / REVIEW_BODY) embedded in the `result` field
            // of result events so reviewers don't have to leave the TUI to
            // see what the reviewer said.
            if line.contains("\"type\":\"result\"") {
                items.extend(extract_review_items(line));
            }
        } else {
            // Plain-text log: surface STATUS: / STUCK: lines.
            if line.contains("STATUS:") {
                if let Some(idx) = line.find("STATUS:") {
                    let rest = line[idx..].trim();
                    items.push(activity_item(rest, Color::rgb(80, 210, 80)));
                }
            } else if line.contains("STUCK:") {
                if let Some(idx) = line.find("STUCK:") {
                    let rest = line[idx..].trim();
                    items.push(activity_item(rest, Color::rgb(220, 120, 50)));
                }
            }
        }
    }

    if items.is_empty() {
        items.push(kv_item(
            "",
            "  No activity yet",
            Some(Color::rgb(100, 100, 100)),
        ));
    }

    items
}

/// Read `~/.coord/logs/<id>.log` and return displayable `ListItem`s.
///
/// If the log file doesn't exist locally, returns a single "remote assignment"
/// notice. Callers that know the machine name should use
/// [`CoordApp::get_activity_log`] instead, which fetches from the remote agent.
fn load_activity_log(id: &str) -> Vec<ListItem> {
    let path = coord_dir().join("logs").join(format!("{}.log", id));

    let content = match std::fs::read_to_string(&path) {
        Ok(c) => c,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return vec![kv_item(
                "",
                "  Log not available (remote assignment)",
                Some(Color::rgb(100, 100, 100)),
            )];
        }
        Err(e) => {
            return vec![kv_item(
                "",
                &format!("  Error reading log: {}", e),
                Some(Color::rgb(220, 70, 70)),
            )];
        }
    };

    parse_log_content(&content)
}

// ─── Pipeline stage rendering ─────────────────────────────────────────────────

/// Whether a pipeline stage can be dispatched by clicking `[Go]` in the
/// Pipeline panel.  Stages outside this list still render — they just
/// don't show a button (they're driven implicitly by the coordinator).
fn is_dispatchable_stage(name: &str) -> bool {
    // #2402: "merge" was excluded here per #738 ("merge is initiated solely
    // from the Merge Queue panel") — but that left a blocked-and-already-
    // dispatched Merge stage with literally no in-TUI recovery affordance
    // beyond that separate panel (#2397's checks_stale/smoke_required case,
    // with no automatic retry coming). Re-including it here attaches a
    // [Go]/[Retry] action to the per-issue Merge box exactly like the other
    // stages; `dispatch_pipeline_stage`'s "merge" arm routes it through the
    // same `coord merge --order <aid>` the Start-(automated)-Merge menu item
    // already runs, so this doesn't add a new command path — only a new way
    // to reach the existing one.
    matches!(name, "plan" | "work" | "review" | "merge")
}

/// Build a `ListItem` for one pipeline stage (plan/work/review/smoke).
///
/// `assignment` is the best-matching assignment for the stage, or `None`
/// when the stage hasn't started yet.
fn pipeline_stage_item(name: &str, assignment: Option<&Assignment>) -> ListItem {
    let (indicator, color, detail_str) = match assignment {
        None => ("  -", Color::rgb(100, 100, 100), "pending".to_string()),
        Some(a) => match a.status.as_str() {
            "running" => (
                "  ~",
                Color::rgb(80, 220, 80),
                format!("{}  {}", trunc(&a.id, 8), a.age_str()),
            ),
            "done" => (
                "  ✓",
                Color::rgb(120, 200, 120),
                format!("{}  {}", trunc(&a.id, 8), a.age_str()),
            ),
            "failed" => (
                "  ✗",
                Color::rgb(220, 70, 70),
                format!("{}  {}", trunc(&a.id, 8), a.age_str()),
            ),
            _ => (
                "  ?",
                Color::rgb(200, 200, 70),
                format!("{}  {}", trunc(&a.id, 8), a.age_str()),
            ),
        },
    };
    ListItem {
        text: StyledText {
            spans: vec![
                StyledSpan::with_fg(indicator, color),
                StyledSpan::with_fg(format!(" {:8}", name), Color::rgb(180, 180, 200)),
                StyledSpan::with_fg(format!("  {}", detail_str), color),
            ],
        },
        icon: None,
        detail: None,
        decoration: if assignment.map(|a| a.status.as_str()) == Some("failed") {
            Decoration::Error
        } else {
            Decoration::Normal
        },
    }
}

/// Build a `ListItem` for the PR/Merge stage, sourced from `merge_queue`.
fn pipeline_merge_item(entry: Option<&MergeQueueEntry>) -> ListItem {
    let (indicator, color, detail_str) = match entry {
        None => ("  -", Color::rgb(100, 100, 100), "pending".to_string()),
        Some(e) => {
            let pr_label = match e.pr_number {
                Some(n) => format!("PR #{}", n),
                None => e.state.clone(),
            };
            match e.state.as_str() {
                "merged" => ("  ✓", Color::rgb(120, 200, 120), pr_label),
                "open" | "queued" => ("  ~", Color::rgb(80, 220, 80), pr_label),
                "failed" => ("  ✗", Color::rgb(220, 70, 70), pr_label),
                "human_required" => (
                    "  !",
                    Color::rgb(220, 100, 100),
                    "needs manual rebase".to_string(),
                ),
                _ => ("  -", Color::rgb(100, 100, 100), pr_label),
            }
        }
    };
    ListItem {
        text: StyledText {
            spans: vec![
                StyledSpan::with_fg(indicator, color),
                StyledSpan::with_fg(" PR/Merge".to_string(), Color::rgb(180, 180, 200)),
                StyledSpan::with_fg(format!("  {}", detail_str), color),
            ],
        },
        icon: None,
        detail: None,
        decoration: match entry.map(|e| e.state.as_str()) {
            Some("failed") | Some("human_required") => Decoration::Error,
            _ => Decoration::Normal,
        },
    }
}


// ─── Log items cache ──────────────────────────────────────────────────────────

/// Resumable parser state for [`parse_sse_log_more`].
///
/// Stored in [`LogItemsCache`] so incremental re-parses resume from where the
/// last parse left off — only newly appended SSE lines need to be processed.
/// Defaults to the "before any lines" state via `Default`.
#[derive(Default, Clone)]
pub(crate) struct LogParseState {
    turn_n: usize,
    assistant_idx: usize,
    last_assistant_time: Option<std::time::Instant>,
    /// Last `user.timestamp` epoch (seconds) seen during the pre-pass.  Carried
    /// across calls so that `user_epochs` can be extended with only new lines.
    last_user_epoch: Option<f64>,
    /// `user_epoch_per_turn` result, extended incrementally.  One entry per
    /// `"type":"assistant"` event in order.
    user_epochs: Vec<Option<f64>>,
}

/// Cached result of parsing a log's SSE (or file) content into [`ListItem`]s.
///
/// Invalidated fully when the assignment ID or wrap width changes, or when
/// `line_count` *shrinks* (shouldn't happen, but defensive).  When `line_count`
/// grows and the assignment/width are unchanged, the cache is extended
/// incrementally by [`parse_sse_log_more`] — only the new lines are parsed.
struct LogItemsCache {
    assignment_id: String,
    /// Number of SSE lines (or file bytes for local logs) at the time of caching.
    line_count: usize,
    wrap_width: usize,
    items: Vec<ListItem>,
    /// Resumed parser state — allows O(new_lines) incremental append when the
    /// SSE stream grows.  Not meaningful for the file-based path (which does its
    /// own exact-match caching via byte length).
    parse_state: LogParseState,
}

/// Minimum wall-clock gap between successive tick-driven `Reaction::Redraw`s.
///
/// Caps streaming redraws at ≈15 fps.  User-input events (`dispatch_event`)
/// still bypass this and redraw immediately — the throttle lives only in `tick`.
const CONTENT_REDRAW_MIN: Duration = Duration::from_millis(66);

// ─── CoordApp ─────────────────────────────────────────────────────────────────

/// Backend-neutral coordinator dashboard.
///
/// Implements [`AppLogic`]: all rendering uses the [`Backend`] trait's
/// `draw_*` methods, and event handling maps [`UiEvent`] to state
/// mutations. No ratatui or GTK types appear here.
pub struct CoordApp {
    data: BoardData,
    /// #1326: which repos ("projects") are open + which is active — the
    /// Project/Workspace model (A-1 of the Project-Scoped Cockpit chassis,
    /// epic #1325; `docs/COCKPIT.md`). Kept in its own module
    /// (`app/workspace.rs`) instead of flat fields here, chipping at the
    /// god-struct (#751). Nothing reads this for rendering yet (that's
    /// A-2/A-3) — `sync_workspace_repos()` (called from
    /// `apply_pending_data`) is what keeps it reconciled against `data`.
    workspace: Workspace,
    /// Which top-level view is currently shown in the content area.
    active_view: SidebarView,
    /// SidebarSystem for the Board view — repo sections with issue rows.
    board_sidebar: SidebarSystem,
    /// Ordered list of repo names used as section IDs in the sidebar.
    /// Rebuilt on each data refresh to stay in sync with `board_sidebar`.
    board_repo_names: Vec<String>,
    /// Cached result of `issues_by_repo()`. Rebuilt on `rebuild_board_sidebar()`.
    board_issues_cache: Vec<(String, Vec<IssueGroup>)>,
    /// True when a PROPOSALS section is prepended to the sidebar.
    has_proposals_section: bool,
    /// Selected machine index in the Machines view.
    machine_sel: usize,
    /// Scroll offset for the machines list.
    machine_scroll: usize,
    refreshed_at: Instant,
    /// Scroll offset for the Board Summary detail panel (right side).
    detail_scroll: usize,
    /// Scroll offset for the Machine detail panel.
    machine_detail_scroll: usize,
    /// Background command runner for `coord` CLI subcommands.
    command_runner: crate::commands::CommandRunner,
    /// Last time `coord notify` was auto-triggered.
    last_notify: Instant,
    // ── Issue sync state ─────────────────────────────────────────────────
    /// Last time `coord sync --quiet` was spawned (to rate-limit kicks).
    issue_sync_last: Option<Instant>,
    // ── Board search / milestone state ──────────────────────────────────
    /// Filter state (query / cursor / focus) for the Board sidebar's FILTER box.
    board_search: SidebarFilter,
    /// #406/#410/#857: Expanded state for each (repo, milestone_key) pair.
    /// Default for an untouched key: expanded when the milestone has
    /// in-flight items, collapsed otherwise (#857 — milestones-first view;
    /// in-flight work stays visible without an extra click). Once a user
    /// toggles a key, that choice persists across rebuilds.
    /// milestone_key is the milestone number as a string, or `"no-milestone"`.
    board_milestone_expanded: std::collections::HashMap<(String, String), bool>,
    /// #1270: per-epic collapse state for the Board panel's nested epic
    /// rows — the Board-panel counterpart of `pipeline_epic_expanded`.
    /// Key: `(repo, epic_issue_number)`. Default for an untouched key
    /// mirrors `epic_expand_state_in`'s rule (collapsed once every child is
    /// Done, expanded otherwise).
    board_epic_expanded: std::collections::HashMap<(String, u64), bool>,
    /// #1270: maps a rendered Board epic row's `(section_idx, TreePath)` to
    /// its `board_epic_expanded` key. Rebuilt by every
    /// `rebuild_board_sidebar` pass — the Board-panel counterpart of
    /// `pipeline_epic_row_keys`.
    board_epic_row_keys: std::collections::HashMap<(usize, TreePath), (String, u64)>,
    /// #2282 (ms-65 §2): per-panel document tabs — the ordered set of open
    /// issues, which one is active, and which (at most one) is the preview.
    ///
    /// This, not the sidebar cursor, is what the detail pane follows once a
    /// tab is open (see `board_detail_issue_group`). With zero tabs open the
    /// panel behaves exactly as it did pre-ms-65: no strip row is rendered
    /// and selection-follows-tree is restored.
    doc_tabs: DocTabs,
    /// #2288 (ms-65 §9): which pane of the active scope's `PaneSet` the
    /// per-pane readers (`board_doc_tabs`, `board_pane_detail_tab`,
    /// `board_pane_detail_scroll`) currently resolve against.
    ///
    /// `None` — the value outside a paint, and the only value any event
    /// handler ever observes — means "the focused pane", which is what
    /// every mutation path acts on anyway. `render_content` sets it to
    /// `Some(i)` around each pane it paints so the ~150 existing `&self`
    /// readers that derive the detail pane from the active document
    /// (`board_detail_issue_group` → `detail_list` → …) follow the pane
    /// being painted instead of the focused one, and clears it again
    /// immediately afterwards.
    ///
    /// A `Cell` for the same reason `last_main_visible_rows` and
    /// `last_issue_panel_cols` are: `ShellApp::render_content` takes
    /// `&self`, so per-frame paint cursors cannot be plain fields.
    render_pane: std::cell::Cell<Option<usize>>,
    /// #2288 (ms-65 §9): an in-progress divider drag on the Board panel's
    /// split. Set by a `MouseDown` on the divider column, consumed by
    /// `MouseMoved`, cleared on `MouseUp` — the same shape as
    /// `queue_split_drag` (#2017).
    board_split_drag: bool,
    /// #2288 (ms-65 §9): the pane rects and divider geometry the last Board
    /// paint resolved from `PaneSet::split_tree`, in screen coordinates.
    ///
    /// Cached rather than recomputed in the event handler for the reason
    /// quadraui's `SplitTree` docs give (the vimcode #582/#452 paint/click
    /// drift bug class): hit-testing must consume *the same* layout the
    /// paint consumed, never a second derivation of it.
    board_split_layout: std::cell::RefCell<Option<quadraui::SplitTreeLayout>>,
    /// #2288 (ms-65 §9): armed by a `Ctrl-W` on the Board panel, consumed
    /// by the very next key press.
    ///
    /// `Some(undo)` means "a §9 pane chord may be completing right now".
    /// `undo` is the state §4's bare-`Ctrl-W` close already applied and
    /// which the chord must retract (`None` when the `Ctrl-W` closed
    /// nothing — no tab was open — and so has nothing to undo). See
    /// `events.rs::resolve_board_pane_chord` for why the close is
    /// speculative rather than deferred.
    board_pane_chord: Option<Option<BoardPaneChordUndo>>,
    /// #2282 (ms-65 §2f): row shape of the last `rebuild_board_sidebar` pass,
    /// indexed by sidebar section — for every emitted row, its `TreePath` and
    /// whether it is a `Decoration::Header` row (which quadraui's MSV measures
    /// at `lh * 1.2` rather than `lh * 1.4`).
    ///
    /// `SidebarSystem` exposes no `rows()` getter, so reveal-on-activate has
    /// no other way to work out how far down the panel a given row sits.
    /// Sections whose body is a `Form` (the FILTER box) hold an empty vec —
    /// their height comes from the form's field count instead.
    board_section_rows: Vec<Vec<(TreePath, bool)>>,
    /// #2282 (ms-65 §2f): per-repo tree windowing for reveal-on-activate.
    ///
    /// Contract §2f pins that the `⌕ Filter issues…` box "stays fixed at the
    /// top of the sidebar regardless of scroll state" — so reveal must NOT
    /// lean on `set_panel_scroll` (`ScrollMode::WholePanel` scrolls the form
    /// off first). Instead the revealed repo's own tree is windowed: the
    /// value here is how many leading emitted rows of that repo's section are
    /// collapsed into a single `⋮ N more above` marker row, computed by
    /// [`Self::board_reveal_hidden_count`] so the target row lands inside the
    /// viewport with the panel unscrolled. Absent key ⇒ no windowing.
    /// (quadraui#595's `SidebarSystem::reveal` will subsume this once landed.)
    board_tree_hidden_above: std::collections::HashMap<String, usize>,
    /// #2282 (ms-65 §2f): the Board sidebar tree's last-painted geometry —
    /// `(rect, line_height, msv_header_size)`.
    ///
    /// Reveal-on-activate is driven from the event path, which has no
    /// `Backend` handle and no layout; this is the same render-then-act stash
    /// the panel already uses for `last_main_visible_rows` and friends. The
    /// backend's own `msv_metrics().header_size` is captured rather than
    /// assumed so the arithmetic stays exact on GTK/macOS, not just TUI.
    last_sidebar_geom: std::cell::Cell<Option<(Rect, f32, f32)>>,
    // ── Pipeline panel state ────────────────────────────────────────────
    /// SidebarSystem listing tracked issues grouped by state → (optionally) repo.
    pipeline_sidebar: SidebarSystem,
    /// Ordered list of repo keys (coord_repo or repo_slug) present in the
    /// current pipeline.  Used by `pipeline_groups_for_repo` and helpers;
    /// no longer maps 1-to-1 to sidebar section indices (that is
    /// `pipeline_state_section_names`).  Rebuilt on each
    /// `rebuild_pipeline_sidebar()`.
    pipeline_repo_names: Vec<String>,
    /// Ordered list of lifecycle state keys for the current pipeline sidebar
    /// sections.  Section index N in the sidebar maps to
    /// `pipeline_state_section_names[N - 1]` (section 0 is always FILTER).
    /// Only non-empty state sections appear.  Values are one of:
    /// `"new"` / `"refining"` / `"pending"` / `"in-progress"` / `"done"`.
    /// Rebuilt on each `rebuild_pipeline_sidebar()`.
    pipeline_state_section_names: Vec<&'static str>,
    /// Filter state (query / cursor / focus) for the Pipeline sidebar's FILTER box.
    pipeline_search: SidebarFilter,
    /// Expanded state for each (lifecycle_key, group_key) sub-group in the
    /// Pipeline sidebar's New/Refining/Pending/In-progress sections (Done is
    /// a flat list post-#728 and never keys into this map).  Default: true
    /// (expanded).  Persists across rebuilds so collapse survives refresh.
    ///
    /// Key semantics: `(lifecycle_key, group_key)` — note the order is
    /// lifecycle-first now that lifecycle is the top-level grouping and the
    /// sub-group is the repo (#1487: In-progress groups by repo too — it used
    /// to key on the Live/Idle liveness bucket).
    pipeline_lifecycle_expanded: std::collections::HashMap<(String, String), bool>,
    /// #668/#857/#1069: Expanded state for milestone sub-headers in the
    /// Pipeline New and In-progress sections (Done is a flat list post-#728
    /// and Refining/Pending have no milestone tier, so neither keys into this
    /// map).  Key: `(lifecycle_key, group_key, milestone_key)` — `group_key`
    /// is the repo for both New and In-progress (#1487; In-progress used to
    /// key on its Live/Idle liveness bucket).
    /// Default for an untouched key: New → false (collapsed, #857
    /// milestones-first view); In-progress → true (expanded — that work is
    /// already in flight).  Persists across rebuilds once a user toggles a
    /// key.
    pipeline_milestone_expanded: std::collections::HashMap<(String, String, String), bool>,
    /// #1197: per-epic collapse state for the Pipeline's nested epic rows.
    /// Key: `(coord_repo, epic_issue_number)` — an epic is independently
    /// collapsible wherever it renders, so the key deliberately omits the
    /// section/group/milestone tier that `pipeline_milestone_expanded` uses
    /// (an epic keeps one collapse state as it moves through lifecycle
    /// states, rather than resetting on every stage transition).
    /// Default for an untouched key: `true` (expanded) — an epic's children
    /// are its whole point, so they're visible until the user hides them.
    pipeline_epic_expanded: std::collections::HashMap<(String, u64), bool>,
    /// #1197: maps a rendered epic row's `(section_idx, TreePath)` to its
    /// `pipeline_epic_expanded` key. Rebuilt by every
    /// `rebuild_pipeline_sidebar` pass, and read by the `RowToggleExpand`
    /// handler so it can recognise an epic row without re-deriving path
    /// shapes (epic rows sit at path len 2 *or* 3 depending on the section
    /// and whether it's milestone-grouped).
    pipeline_epic_row_keys: std::collections::HashMap<(usize, TreePath), (String, u64)>,
    /// #2452: row shape of the last `rebuild_pipeline_sidebar` pass, indexed
    /// by sidebar section — mirrors `board_section_rows` exactly (same
    /// `(TreePath, is_header)` shape, same reason: `SidebarSystem` exposes no
    /// `rows()` getter, so `reveal_pipeline_active_doc`'s scroll-into-view has
    /// no other way to work out how far down the panel a given row sits).
    pipeline_section_rows: Vec<Vec<(TreePath, bool)>>,
    /// #2452: the Pipeline sidebar tree's last-painted geometry —
    /// `(rect, line_height, msv_header_size)`. Mirrors `last_sidebar_geom`
    /// (Board's own copy); kept separate because the two panels paint on
    /// mutually exclusive frames (`active_view` is one or the other) and a
    /// shared cell would go stale the instant the user switches panels.
    last_pipeline_sidebar_geom: std::cell::Cell<Option<(Rect, f32, f32)>>,
    /// Tracked issues for the Pipeline panel (loaded asynchronously via gh).
    pipeline_issues: Vec<PipelineIssue>,
    /// Selected issue index into `pipeline_issues`, if any.
    pipeline_sel: Option<usize>,
    /// Status message shown when a dispatch is queued/skipped due to no
    /// available machine. Cleared after a short TTL.
    pipeline_status: Option<(String, Instant)>,
    /// Active toasts rendered as a bottom-right overlay. Each entry pairs
    /// a `ToastItem` with the time it was added so the host can auto-expire
    /// them after a few seconds without the user dismissing manually.
    toasts: Vec<(ToastItem, Instant, ToastSeverity)>,
    /// Monotonic counter for toast widget IDs (must be unique per toast).
    next_toast_id: u64,
    /// Pool of concurrent SSE watch sessions keyed by `assignment_id`.
    /// Sessions remain alive (accumulating lines) even when not focused, so
    /// switching between issues requires no reconnect.  Capped at
    /// `WATCH_POOL_CAP`; the least-recently-focused entry is evicted when
    /// the pool would exceed the cap.
    watch_pool: std::collections::HashMap<String, WatchContext>,
    /// The `assignment_id` of the currently-focused watch context, or `None`
    /// when the watch overlay is closed.
    watch_focused: Option<String>,
    /// #386: True when `inject_chat` was opened from the Pipeline Log tab via
    /// the `i` keybind (which temporarily sets `watch_focused`).  On Esc /
    /// Cancelled the Cancelled arm uses this flag to clear `watch_focused`
    /// again, restoring the invariant that `watch_focused.is_none()` on the
    /// Log tab so j/k scroll falls through to the Log-tab arms (#308).
    inject_opened_from_log_tab: bool,
    /// Chat overlay for mid-flight worker guidance. `None` when closed.
    inject_chat: Option<ChatController>,
    /// Animation frame counter for the inject chat spinner.
    inject_spinner_frame: usize,
    /// Which tab is active in the Pipeline detail pane.
    pipeline_detail_tab: PipelineDetailTab,
    /// Which tab is active in the Board detail pane.
    board_detail_tab: BoardDetailTab,
    /// Scroll offset for the issue body on the Issue tab.
    pipeline_detail_scroll: usize,
    /// Horizontal scroll offset (chars) for the Log tab list (#302). Left/Right
    /// (and h/l) adjust it; the quadraui ListView rasteriser honors `h_scroll`
    /// and paints a horizontal scrollbar when content overflows the viewport.
    pipeline_log_hscroll: usize,
    /// #2342: sticky per-issue pick for the Pipeline Log tab, keyed by
    /// `(coord_repo, issue_number)` — the same `(String, u64)` shape as
    /// `pipeline_epic_expanded` / `board_epic_expanded`, so two different
    /// repos that happen to share an issue number can't read/write each
    /// other's pin. An issue can have more than one candidate log-source
    /// assignment — most visibly a milestone tracking/epic issue, which
    /// carries both its own Gate A `mock-author` row and every member's
    /// `test-author` row (test-author always books `issue_number` to the
    /// tracking issue, never the member it's actually for). Pressing a
    /// digit key on the Log tab's source picker records that choice here;
    /// an absent entry means "auto-pick" (running, else most-recently-
    /// dispatched — the pre-#2342 behavior). Keyed by issue rather than
    /// reset on selection change, so no explicit invalidation hook is
    /// needed when the user switches issues. An issue with no `coord_repo`
    /// keys under `String::new()` — harmless since `log_candidates_for_issue`
    /// already matches across all repos in that case (no repo to filter on).
    pipeline_log_pinned_assignment: std::collections::HashMap<(String, u64), String>,
    /// Cache of remotely-fetched log items, keyed by assignment ID.
    ///
    /// Each entry stores `(fetched_at, items)`. Entries older than 30 s are
    /// re-fetched on the next render that needs them. `RefCell` is used so the
    /// cache can be updated from `&self` methods (render path).
    remote_log_cache:
        std::cell::RefCell<std::collections::HashMap<String, (Instant, Vec<ListItem>)>>,
    /// Pending background board-data load.  `Some` while a load is in flight;
    /// `None` when idle.  Polled non-blockingly on every [`handle`] call.
    pending_data: Option<std::sync::mpsc::Receiver<BoardData>>,
    /// Most-recent data-load error (message + timestamp), displayed in the
    /// status bar for a short time.  Cleared when the next load succeeds.
    fetch_error: Option<(String, Instant)>,
    /// In-flight remote log fetches keyed by assignment ID.
    ///
    /// Each `Receiver` yields `Ok(raw_content)` or `Err(error_message)`.
    /// `RefCell` allows mutation from `&self` render methods.
    pending_log_fetches: std::cell::RefCell<
        std::collections::HashMap<String, std::sync::mpsc::Receiver<Result<String, String>>>,
    >,
    /// In-flight `gh issue view` fetches for Board Issue tab bodies that
    /// weren't in the local issues table (e.g. closed >7d ago and pruned).
    /// Keyed by `(repo_name, issue_number)`. The receiver yields `Ok(issue)`
    /// or `Err(error_message)`.
    pending_issue_fetches: std::cell::RefCell<
        std::collections::HashMap<
            (String, u64),
            std::sync::mpsc::Receiver<Result<FetchedIssue, String>>,
        >,
    >,
    /// In-memory cache for successfully-fetched single issues. Survives until
    /// the TUI restarts; the background thread also upserts into the DB so
    /// the next `load_data()` finds it. No TTL — `coord sync` is the source
    /// of truth for invalidation.
    fetched_issues_cache:
        std::cell::RefCell<std::collections::HashMap<(String, u64), FetchedIssue>>,
    // #876: removed pending_comments_fetches and fetched_comments_cache —
    // the Summary tab now sources directly from in-memory board assignments.
    /// Pending purge confirmation state.  `Some((assignments, issues))` means
    /// we are waiting for the user to confirm; `None` means not pending.
    ///
    /// Triggered by pressing 'P' when the Board sidebar selection is in the
    /// Completed (done/merged) group.  Any key other than 'y'/'Y' cancels.
    pending_purge: Option<(usize, usize)>,
    /// #200: inline reason input for Test gate failure. `Some(buf)` means we
    /// are accumulating the reason; Enter submits, Esc cancels. The carried
    /// `usize` is the work-assignment index in `self.data.assignments` that
    /// the verdict will be applied to.
    pending_test_fail: Option<(usize, String)>,
    /// #296: inline reason input for "report & dispatch fix" (key `r` in
    /// Pipeline view when Test gate is actionable).  `Some(buf)` means we are
    /// accumulating the description; Enter records test_state=failed AND
    /// dispatches `coord fix <work_id> --guidance <buf>`.  Esc cancels.
    pending_report_fix: Option<String>,
    /// #977: inline title input for the Plans-panel "fast plan capture" key
    /// (`c` while `active_view == SidebarView::Plans`). `Some(buf)` means we
    /// are accumulating the plan title; Enter dispatches `coord milestone
    /// capture <repo> --title <buf>` via `capture_plan_stub`. Esc cancels.
    pending_plan_capture: Option<String>,
    /// #1003: pending single-field text input for a Plans-panel /
    /// MilestoneDag row action (Edit milestone title / Add issue to
    /// milestone / Remove issue from milestone). Mirrors the #977
    /// `pending_plan_capture` single-buffer pattern — Enter submits via
    /// `submit_milestone_row_input`, Esc cancels.
    pending_milestone_row_input: Option<PendingMilestoneRowInput>,
    /// #1003: pending "Close / archive plan" confirmation (`coord issue
    /// close`) — 'y' confirms, any other key cancels, mirroring
    /// `pending_restart`.
    pending_close_plan: Option<PendingClosePlan>,
    /// #1017: inline (optional) title input for the Plans-panel "New
    /// milestone via chat…" key (bare `C` while `active_view ==
    /// SidebarView::Plans`, sibling to #977's `c` capture). `Some(buf)`
    /// means we are accumulating an optional seed title; Enter dispatches
    /// `coord milestone chat <repo> --new [--title <buf>]` via
    /// `capture_plan_chat` — unlike `pending_plan_capture`, an empty buffer
    /// is a valid submission (the operator discusses the title in chat).
    /// Esc cancels.
    pending_new_milestone_chat: Option<String>,
    /// #264: refinement-chat dispatch is pending — we've shelled `coord
    /// refine-chat <repo> <issue>` and are polling the DB for the new
    /// `type="refinement"` assignment to appear so we can open the chat
    /// overlay bound to its id.  Cleared on bind or timeout.
    pending_refinement: Option<PendingRefinement>,
    /// #314 Phase B: test-chat dispatch is pending — we've shelled `coord
    /// test-chat <work_assignment_id>` and are polling the DB for the new
    /// `type="test-chat"` assignment to appear so we can open the chat
    /// overlay bound to its id.  Cleared on bind or timeout.
    pending_test_chat: Option<PendingTestChat>,
    /// #315: chat-continue dispatch is pending — the prior worker exited after
    /// `end_turn`, the user typed another message, and we've shelled
    /// `coord chat-continue <old_aid> <text>`.  We poll `self.data.assignments`
    /// each tick for the new running refinement row and rebind the open chat
    /// overlay to it when it appears.  Cleared on bind or timeout.
    pending_chat_resume: Option<PendingChatResume>,
    /// #315: channel for `spawn_inject_post` to signal worker-exit races
    /// (HTTP 409/410 from /inject/{id}).  The TX side is cloned and handed
    /// to each spawned POST thread; the RX side is drained on tick and the
    /// signals trigger transparent `coord chat-continue` fallbacks so the
    /// typed message isn't lost when the worker exited mid-submit.
    inject_fallback_tx: std::sync::mpsc::Sender<InjectFallback>,
    inject_fallback_rx: std::sync::mpsc::Receiver<InjectFallback>,
    /// #264: queued follow-up `coord ready` after `coord stop` completes
    /// at the end of a refinement chat.  Single-slot CommandRunner forces
    /// sequential dispatch; the poll handler fires this once stop's
    /// CommandResult arrives.
    pending_refine_ready: Option<PendingRefineReady>,
    /// #410: when true, the next `finalise_refinement_chat()` call should
    /// also queue a pipeline dispatch after `coord ready` completes.
    refine_then_dispatch: bool,
    /// #264: cache key for the last chat-transcript rebuild so tick()
    /// can skip the JSON-parsing pass when nothing changed.  Tuple is
    /// `(focused_assignment_id, sse.lines.len(), inject_transcript.len())`.
    /// Without this the rebuild ran at 60 fps over every accumulated
    /// stream-json line — quadratic in chat duration and the dominant CPU
    /// cost when the refinement overlay sat open for minutes.
    chat_transcript_cache_key: Option<(String, usize, usize)>,
    /// #264: timestamp of the most recent transcript change (user submit
    /// or assistant turn arrival).  Drives the chat's `set_busy()` —
    /// busy is true while activity is recent (≤2 s), false once the
    /// stream goes quiet.  Without this the spinner never animates so
    /// the user can't tell whether the worker is mid-reply or done.
    chat_last_activity: Option<Instant>,
    /// #264: spinner-redraw throttle.  When the chat is busy we need to
    /// force redraws to animate the spinner — but at 60 fps that burns
    /// CPU for no benefit (the spinner glyph only changes ~10 times a
    /// second).  Increment per tick, redraw every Nth, so spinner is
    /// visually smooth without a full 60 fps repaint while busy.
    chat_spinner_throttle: u8,
    /// #pause: set of machine names the user has paused via
    /// right-click → Pause routing.  Loaded from
    /// `~/.coord/paused_machines.json` when this TUI is talking to a local
    /// daemon/DB, refreshed each periodic data load so out-of-band changes
    /// (`coord pause foo` from another terminal) propagate within a few
    /// seconds.
    ///
    /// #1563: on a thin client (board service configured) this is instead
    /// populated from the daemon's `GET /pause` — mirrors
    /// `coord.machine_pause.paused_set()`'s daemon-aware routing on the
    /// Python side. A thin-client TUI's own copy of `paused_machines.json`
    /// is never written to by `coord pause` anymore (that now goes straight
    /// to the daemon), so reading it directly here would silently revert
    /// the optimistic update in the `p` keybind handler and let
    /// `best_machine_for()` keep selecting a machine the operator just
    /// paused — see `spawn_paused_machines_fetch`/`poll_paused_machines`.
    paused_machines: std::collections::HashSet<String>,
    /// #1862: subset of `paused_machines` that's paused specifically
    /// because a `quiet_hours` window covers the current moment, rather
    /// than an explicit `coord pause` — lets `machines_list()` badge a
    /// quiet-paused machine differently from a hand-paused one (the
    /// acceptance criterion that a 1AM operator can tell "asleep until
    /// 08:00" from "someone paused this"). Only ever populated via the
    /// daemon's `GET /pause` (`data::PausedFetch`'s `quiet` field) — see
    /// its doc comment for why the local-file fallback always leaves this
    /// empty. Never consulted for routing, display only.
    quiet_paused_machines: std::collections::HashSet<String>,
    /// #2101: the subset of `paused_machines` under a *release cordon* —
    /// draining so `coord release propagate` can roll them onto the released
    /// version. Badged distinctly for the same reason `quiet_paused_machines`
    /// is: a machine that stopped taking work because the fleet is upgrading
    /// itself must not read as one a human paused, because the remedy an
    /// operator would reach for (`coord unpause`) does not apply and would
    /// not work. Only ever populated via the daemon's `GET /pause`
    /// (`data::PausedFetch`'s `cordoned` field). Never consulted for
    /// routing, display only.
    cordoned_machines: std::collections::HashSet<String>,
    /// #2147: `{machine: QuietHoursWindow}` for every machine with an
    /// effective quiet-hours schedule (operator-set OR `coordinator.yml`),
    /// whether or not it covers *now* — unlike `quiet_paused_machines`
    /// above, which is only the currently-covered subset. Feeds the
    /// sidebar badge's schedule text (`[QUIET 22:00-08:00]`) and pre-fills
    /// the "Set quiet hours…" dialog. Only ever populated via the daemon's
    /// `GET /pause` (`data::PausedFetch`'s `quiet_hours` field); a
    /// thin-client-less local-file fallback always leaves this empty, same
    /// gap `quiet_paused_machines`/`cordoned_machines` document.
    ///
    /// Deliberately its own field rather than folded into
    /// `quiet_paused_machines`: setting a window for later tonight must
    /// update the schedule an operator sees on the next menu open without
    /// predicting that the machine is paused right now (see the
    /// `dispatch_context_menu_action`/`submit_quiet_hours` optimistic-update
    /// comments — the #2101 "leave the sets alone" precedent).
    quiet_hours_windows: std::collections::HashMap<String, data::QuietHoursWindow>,
    /// #1563: in-flight background fetch of the paused-machine set (local
    /// file read or daemon `GET /pause`, whichever `spawn_paused_machines_fetch`
    /// determined applies). Armed at startup and re-armed by `refresh()` on
    /// the same cadence as `pending_remote_sessions`/`pending_drive_sessions`;
    /// `poll_paused_machines()` drains it into `paused_machines`/
    /// `quiet_paused_machines`.
    pending_paused_machines: Option<std::sync::mpsc::Receiver<data::PausedFetch>>,
    /// #245: pending `coord merge --force-merge` confirmation.  `Some(repo)`
    /// means the user pressed `m` while the "Checks failed" hint was visible
    /// and we're waiting for one-key confirmation before bypassing the CI
    /// gate.  `repo` is the coord-local repo name to scope the force-merge
    /// to (empty string ⇒ no scope, force-merge the whole queue).  Any key
    /// other than `y`/`Y` cancels; the early-intercept block consumes the
    /// keypress so the normal `y`-as-prefix paths can't fire.
    pending_force_merge: Option<String>,
    /// #780: pending "Merge all ready" confirmation from the Merge Queue panel.
    /// `Some(aids)` means the user pressed `a` and we are waiting for one-key
    /// confirmation before running `coord merge` to drain every READY entry.
    /// `aids` is the list of READY assignment_ids shown in the confirm prompt.
    /// Any key other than `y`/`Y` cancels.
    pending_merge_all_ready: Option<Vec<String>>,
    /// #2402: pending `coord merge --revalidate --only <assignment_id>`
    /// confirmation, armed by the Pipeline row's "Re-verify (revalidate)"
    /// context-menu action (offered only on a Merge stage BLOCKED with a
    /// `format::merge_revalidate_eligible` reason — #2397's surfaced
    /// checks_stale/smoke_required case). Mirrors `pending_force_merge`'s
    /// confirm-then-spawn shape: any key other than `y`/`Y` cancels, and a
    /// click-outside/Escape dismiss leaves the entry exactly as blocked as
    /// it was.
    pending_merge_revalidate: Option<PendingMergeRevalidate>,
    /// #259: open right-click context menu, or `None` if no menu is showing.
    /// Opened by right-click on a Board / Pipeline sidebar row; dismissed by
    /// click-outside, Escape, or item activation.
    pending_context_menu: Option<ContextMenuState>,
    /// #1374: text staged by `dispatch_context_menu_action`'s
    /// `"copy-issue-number"` arm for the caller to write to the clipboard.
    /// That fn has no `&mut dyn Backend` (most of its callers never reach
    /// this action), so it stashes the payload here and the two direct UI
    /// entry points (`handle_context_menu_click` / mouse,
    /// `context_menu_activate_selected` / keyboard) flush it via
    /// `flush_pending_clipboard_copy` right after dispatch returns.
    pending_clipboard_copy: Option<String>,
    /// #259 / #607: cached layout stack from the last render — one entry per
    /// open menu level (root + any open submenus).  Each entry is a
    /// `(ContextMenu, ContextMenuLayout)` pair so hit-testing can walk the
    /// stack deepest-first without rebuilding the menus.  Borrowed from the
    /// `&self` render path; populated on every frame while a menu is open and
    /// cleared when it dismisses.
    context_menu_layout: std::cell::RefCell<Vec<(ContextMenu, ContextMenuLayout)>>,
    /// #1631 (H-4): the fleet-health detail overlay — opened via the status
    /// bar's "Fleet health…" right-click menu entry (`fleet_health.rs`).
    /// Owns ALL input while open (Esc closes it; see `events.rs`), same
    /// posture as `help_overlay` below. A plain `bool` rather than a
    /// quadraui `HelpOverlayController`: the content here (per-machine check
    /// rows grouped under a machine header, sourced from `self.data.
    /// fleet_health`) doesn't fit that controller's notes/actions shape.
    fleet_health_overlay_open: bool,
    /// #1755: the "Add to drive queue after…" prompt. `Some` while the
    /// operator is typing pre-req issue numbers; owns ALL keys until Enter
    /// (submit) or Esc (cancel), same posture as
    /// `pending_milestone_row_input`.
    pending_drive_queue_after: Option<PendingDriveQueueAfter>,
    /// #2500: the "What needs to change?" prompt raised before `coord
    /// gate-a --changes` fires — `Some` while the operator is typing the
    /// note that becomes `--note`; owns ALL keys until Enter (submit, empty
    /// buffer OK) or Esc (cancel — the whole dispatch is dropped, unlike
    /// the pre-#2500 behavior where the context-menu click fired
    /// unconditionally). Same posture as `pending_drive_queue_after`.
    pending_gate_a_changes_note: Option<PendingGateAChangesNote>,
    // ── #2532: Approved work items ActivityBar panel ─────────────────────
    /// Selected row index (0-based) into `approved_submissions()` (contract
    /// §3c's oldest-first order). Clamped to bounds on navigation, same
    /// pattern as `audit_sel`.
    approved_sel: usize,
    /// Vertical scroll offset (in *submission* rows, header excluded) into
    /// the main-panel list. Kept in step with `approved_sel` by
    /// `fix_approved_scroll` at every keyboard-nav site in `events.rs` —
    /// without it `j`/`End` would walk the selection past the first
    /// screenful and the row would simply never be painted, which is the
    /// exact defect `audit_scroll`/`fix_audit_scroll` were added for in
    /// #1094.
    approved_scroll: usize,
    /// `true` when the inline submission-detail pane (Enter on a selected
    /// row) is open. `Esc` closes it back to the list-only view (contract
    /// §3e), same pattern as `audit_detail_open`.
    approved_detail_open: bool,

    /// #1866 (Q-1): selected row index into `queue_rows()` — the Queue
    /// panel's own *filtered, possibly re-sorted* row set.
    queue_sel: usize,
    /// #1866: vertical scroll offset into the Queue grid, kept in step with
    /// `queue_sel` by `fix_queue_scroll` and driven directly by the wheel
    /// and the scrollbar track.
    queue_scroll: usize,
    /// #1866: which grid column is sorted and in which direction, or `None`
    /// for the queue's own run (`position`) order. Toggled by a header click
    /// (`queue_sort_by_column`). View state over a live row set, never
    /// persisted — the same posture as `reports_sort`.
    queue_sort: Option<(usize, SortDirection)>,
    /// #1866: the most recently painted Queue-grid geometry **and the rect
    /// it was painted into**, cached so `events.rs` can route a click
    /// without a `Backend` handle.
    ///
    /// The rect is part of the cache for the reason `reports_table_layout`
    /// documents and `audit_table_layout` gets away with ignoring: this
    /// table need not start at the main panel's origin, so a bare
    /// `pos - main_b` would mis-hit-test. Set to `None` at the top of
    /// `render_queue_panel`, so a click can never route against a table
    /// that isn't on screen.
    queue_table_layout: std::cell::RefCell<Option<(Rect, DataTableLayout)>>,
    /// #1867 (Q-2): vertical scroll offset into the selected row's issue
    /// body, rendered in the bottom ~40% of the Queue panel. Stepped by
    /// `scroll_focused_content` (keyboard, once focus has moved off the grid
    /// via Ctrl-W) and by the mouse wheel over the detail pane
    /// (`mouse_main_scroll`). Reset to 0 whenever the selected row changes
    /// (`CoordApp::queue_set_sel`) — otherwise a short issue would inherit a
    /// long one's offset and render blank, reading as "no body".
    queue_detail_scroll: usize,
    /// #1867: the content width the Queue detail pane was last painted at,
    /// stashed immediately before drawing so `queue_issue_body_list` can
    /// word-wrap to the live viewport. Same `last_issue_panel_cols` pattern
    /// used by the Board/Pipeline Issue tabs.
    last_queue_detail_cols: std::cell::Cell<usize>,
    /// #1867: visible-row count of the Queue detail pane's OWN painted
    /// rect, stashed alongside `last_queue_detail_cols` right before
    /// drawing. The detail pane is only the bottom ~40% of the Queue
    /// panel (`render_queue_panel`'s `detail_h`) — the generic `visible`
    /// computed in `mouse_main_scroll` from `ctx.main_bounds()` is the
    /// WHOLE panel (grid + detail) and overestimates this pane's rows by
    /// roughly 2-3x, which was the #1910-class bug: `max = items -
    /// (visible - 1)` saturates to 0 for any body shorter than the full
    /// panel, and wheel-down goes dead.
    last_queue_detail_visible_rows: std::cell::Cell<usize>,
    /// #1867: `queue_issue_body_list().items.len()` from the last paint,
    /// stashed at the same time as `last_queue_detail_visible_rows` so
    /// `mouse_main_scroll` doesn't have to re-run `queue_issue_body_list`
    /// (a markdown re-render that also drains the pending-fetch channel and
    /// touches the fetch cache) on every wheel notch just to read a length.
    last_queue_detail_item_count: std::cell::Cell<usize>,
    /// #2017: the Queue panel's grid/detail split, as the DETAIL pane's
    /// fraction of the panel height available to the two panes (i.e. minus
    /// the one-row draggable separator) — a fraction rather than an
    /// absolute row count so a terminal resize preserves the operator's
    /// intent instead of snapping back to the old 40% default or
    /// permanently squashing one side (#2017 acceptance test 4). Read and
    /// clamped by `CoordApp::queue_clamp_detail_h` on every paint
    /// (`render_queue_panel`) and rewritten live while `queue_split_drag`
    /// is active (`queue_update_split_drag`). Session-only persistence —
    /// same tier as `audit_column_overrides` / `reports_column_overrides`;
    /// no cross-restart UI-state store exists in this codebase yet.
    queue_split_frac: f32,
    /// #2017: `true` while the operator is dragging the separator between
    /// the Queue grid and its detail pane — set on a `MouseDown` inside
    /// `queue_separator_hit`'s region and cleared on `MouseUp`. Mirrors
    /// `audit_resize_col` / `reports_resize_col`'s drag-state shape.
    queue_split_drag: bool,
    /// #2017: the most recently painted separator's rect, cached by
    /// `render_queue_panel` so `events.rs` can hit-test a click/hover
    /// without a `Backend` handle. `None` when no grid+detail split is on
    /// screen (empty queue — see `render_queue_panel`'s early return).
    queue_separator_rect: std::cell::Cell<Option<Rect>>,
    /// #2017: `true` while the operator is dragging the Queue grid's own
    /// vertical-scrollbar thumb — set on a `MouseDown` inside
    /// `queue_scrollbar_hit`'s region and cleared on `MouseUp`. Mirrors
    /// `reports_vscroll_drag`; the click-to-position half
    /// (`queue_apply_vscroll`) already existed pre-#2017, this just adds
    /// the continuation so the drag doesn't die after one jump.
    queue_vscroll_drag: bool,
    /// #2043: horizontal scroll offset (cells) into the Queue grid, applied
    /// only once the grid has dropped below its `QUEUE_MIN_WIDTH_CHARS`
    /// floor and started scrolling instead of squeezing columns
    /// (`render_queue_panel`). Driven by the horizontal scrollbar's
    /// click-to-position (`queue_apply_hscroll`) and its wheel
    /// (`mouse_main_scroll`'s Queue arm) — mirrors `audit_h_scroll`, minus
    /// the quadraui#550 caveat: that fix is what makes driving this safe at
    /// all (a non-zero value used to shift painted headers out from under
    /// `DataTableLayout::hit_test`, mis-routing sort clicks — see
    /// `reports.rs:76-85`).
    ///
    /// `Cell`, not a bare `f32`, because `render_queue_panel` takes `&self`
    /// (the immediate-mode pattern every `render_*_panel` in this crate
    /// follows) but still has to self-correct this value every frame: a
    /// terminal resize back above `QUEUE_MIN_WIDTH_CHARS` shrinks
    /// `content_width` back to the viewport, and a stale offset from before
    /// the resize must not go on shifting the painted grid left with no
    /// scrollbar left to click to fix it. See `queue_clamp_h_scroll` and
    /// its call site in `render_queue_panel`.
    queue_h_scroll: std::cell::Cell<f32>,
    /// #2043: `true` while the operator is dragging the Queue grid's own
    /// horizontal-scrollbar thumb — set on a `MouseDown` inside
    /// `queue_scrollbar_hit`'s `Horizontal` region and cleared on
    /// `MouseUp`. Mirrors `queue_vscroll_drag`, the other axis of the same
    /// track pair.
    queue_hscroll_drag: bool,
    /// #2017: the Queue detail pane's most recently painted vertical
    /// scrollbar geometry (`Backend::list_vscrollbar`, called with the same
    /// `detail_rect` `render_queue_panel` draws into), cached so
    /// `events.rs` can hit-test and drag-scrub it without a `Backend`
    /// handle at click time. `None` whenever the pane's content fits (no
    /// track painted — `ListView::vscrollbar` already returns `None` for
    /// that case, and this cache mirrors it verbatim) or nothing is on
    /// screen.
    queue_detail_scrollbar: std::cell::RefCell<Option<Scrollbar>>,
    /// #2017: `true` while the operator is dragging the Queue detail pane's
    /// vertical-scrollbar thumb. Same shape as `queue_vscroll_drag`, for
    /// the other pane.
    queue_detail_vscroll_drag: bool,
    /// Cached `DialogLayout` from the last prompt-dialog render — used for
    /// click hit-testing on dialog buttons.  Populated while any
    /// `pending_*` prompt dialog is visible; cleared when it dismisses.
    dialog_layout: std::cell::RefCell<Option<DialogLayout>>,
    /// Machines panel: name of machine awaiting restart confirmation.
    /// While `Some`, 'y'/'Y' fires `coord agent restart --machine <name>`;
    /// any other key cancels.
    pending_restart: Option<String>,
    /// Per-machine last time a `/health` HTTP fetch returned successfully.
    /// Updated from `apply_pending_data()`.  Used to show "Xs ago" in the
    /// Machines panel even across multiple load cycles.
    machine_last_contact: std::collections::HashMap<String, Instant>,
    /// Version of the local `coord` binary, fetched once at startup.
    /// Used to flag remote agents that are running an older version.
    local_coord_version: Option<String>,
    /// Cached visible-row count for the main panel, updated on scroll events
    /// and tick. Used by `watch_log_list` to compute a stick-to-bottom scroll
    /// offset that actually keeps the latest lines on screen (the previous
    /// hard-coded `items.len() - 40` cut off latest lines when the terminal
    /// viewport was under 40 rows).
    last_main_visible_rows: std::cell::Cell<usize>,
    /// Panel width in "backend units" (character columns for TUI, pixels for
    /// GTK) of the Pipeline Log tab's content rect, updated just before
    /// `pipeline_log_list` is called on each render.  Defaults to 120.  Used
    /// by `parse_log_content_readable` / `parse_sse_log_readable` to word-wrap
    /// long assistant text blocks so the Log tab is readable without horizontal
    /// scrolling (#385).
    last_log_panel_cols: std::cell::Cell<usize>,
    /// Panel width in backend units of the Issue tab's content rect, updated
    /// just before `board_issue_body_list` / `pipeline_issue_body_list` is
    /// called on each render.  Defaults to 120.  Used by `issue_body_list` to
    /// word-wrap long lines to the viewport width (#669).
    last_issue_panel_cols: std::cell::Cell<usize>,
    /// #2288 (ms-65 §9): the Board panel's Issue sub-tab content width, in
    /// backend units, **per pane** — entry `i` is the width pane `i` last
    /// painted its Issue body at.
    ///
    /// A single shared `Cell` (`last_issue_panel_cols`) cannot express this
    /// once the panel can hold two panes at once: `render_content` paints
    /// both, so the second-painted pane's width would overwrite the first's
    /// and outlive the frame, and `mouse_main_scroll` — which reads
    /// `board_issue_body_list().items.len()` to clamp the wheel — would
    /// word-wrap at the *other* pane's width and clamp to a line count that
    /// never appeared on screen. Indexing by pane keeps each pane's wrap
    /// width its own, and leaves `last_issue_panel_cols` to the Pipeline
    /// panel, which is still single-pane.
    ///
    /// Empty (or a `0` entry) means "this pane has not painted its Issue
    /// body yet", which falls back to `last_issue_panel_cols` so a
    /// never-painted fixture keeps the 120-column default it always had.
    board_pane_issue_cols: std::cell::RefCell<Vec<usize>>,
    /// Minimum age in days for a done/failed assignment row to be eligible
    /// for the 'P' purge action.  Default 7.
    ///
    /// TODO: wire from coordinator.yml `purge_days` key (e.g. under a top-level
    /// `tui:` section) once the Python config layer supports it.
    purge_days: u32,
    /// Hover state for the sidebar action bar (above the Board / Pipeline
    /// tree).  Driven by `UiEvent::MouseMoved`; read at render time and
    /// passed to `backend.draw_sidebar_panel` so the rasteriser can tint
    /// the hovered button.  Cleared when the cursor leaves the bar.
    sidebar_action_bar_hover: ToolbarHoverTracker,
    /// Hover state for the view-level panel toolbar (above the main
    /// detail area).  Same shape and lifecycle as
    /// `sidebar_action_bar_hover`.
    panel_toolbar_hover: ToolbarHoverTracker,
    /// Hover state for the pipeline action bar — the `[ Go ⏎ ]` /
    /// `[ Retry ⏎ ]` strip drawn above the stage boxes when a
    /// dispatchable stage exists.  Separate from `panel_toolbar_hover`
    /// because the action bar is rendered directly with `draw_toolbar`
    /// (not through a `SidebarPanel`), so it needs its own tracker.
    pipeline_action_bar_hover: ToolbarHoverTracker,

    /// Index of the currently-selected stage in the Pipeline > Stages
    /// tab.  `None` defaults to "no stage selected" — first arrow / click
    /// snaps to a sensible default (usually the latest non-pending
    /// stage).  Driven by Left/Right arrows when the Stages tab is
    /// active, and by mouse clicks on stage boxes.  When focused,
    /// quadraui's PipelineView rasteriser draws the box with an accent
    /// border, and coord-tui renders the matching stage's content
    /// (logs / findings) in the scrollable panel below the strip.
    pipeline_focused_stage: Option<usize>,
    /// Scroll offset (line index) for the stage-content scrollable
    /// panel below the pipeline strip.  Reset when the focused stage
    /// changes.
    pipeline_stage_content_scroll: usize,

    // ── Settings panel ───────────────────────────────────────────────────
    /// Persisted user settings loaded from `~/.coord/settings.toml`.
    settings: TuiSettings,
    /// Pre-parsed keybindings from `settings.keybindings`. Rebuilt whenever
    /// settings are saved so lookups are O(n) equality checks, not string parses.
    parsed_keybindings: Vec<(String, ParsedBinding)>,
    /// FormController backing the settings form (right pane).
    ///
    /// `RefCell` is used so the form can be rebuilt and rendered from the
    /// `&self` render path (same pattern as `remote_log_cache`).
    settings_form: std::cell::RefCell<FormController>,
    /// Which category row is selected in the settings sidebar (left pane).
    // #237: removed `settings_category_sel` — settings render as one
    // unified scrollable form; there are no categories to select.
    /// Which interactive field is focused within the current category's form.
    /// Reset to 0 when the category changes.
    settings_field_sel: usize,
    /// Assignment IDs that were in `running` state at the most recent data
    /// refresh.  Used to detect running→done/failed transitions so we can
    /// ring the terminal bell when `audio_on_completion` is enabled.
    audio_prev_running: std::collections::HashSet<String>,
    /// #235 Phase 1: in-flight Test-stage build jobs keyed by work_id.
    /// Drained by `poll_test_build_jobs` each tick — completed entries are
    /// removed and surfaced as toasts. Empty in the steady state.
    test_build_jobs: std::collections::HashMap<String, TestBuildJob>,
    /// #271 part 2: persisted outcomes from completed Phase 1 builds.
    /// Keyed by `work_id`.  Drives the persistent "Last build: …" line
    /// in the Pipeline detail panel so the user who missed the 4 s
    /// toast can still see the result.
    last_test_builds: std::collections::HashMap<String, TestBuildResult>,
    /// #271 part 2 follow-up: in-flight `gh pr view` background fetches
    /// keyed by `(repo_slug, pr_number)`.  Drained by
    /// `poll_pending_pr_fetches` each tick; results land in
    /// `fetched_prs_cache`.
    pending_pr_fetches: std::cell::RefCell<
        std::collections::HashMap<
            (String, i64),
            std::sync::mpsc::Receiver<Result<FetchedPr, String>>,
        >,
    >,
    /// In-memory cache of the latest `gh pr view` snapshot per PR.
    /// Populated by completed `pending_pr_fetches` rounds; consumed by
    /// the Pipeline detail panel's Test guidance block.
    fetched_prs_cache: std::cell::RefCell<std::collections::HashMap<(String, i64), FetchedPr>>,
    /// #1344: CI check summaries for PRs in the merge queue, keyed by
    /// `(repo_github, pr_number)`. Synced from `/board`'s `merge_plan[].ci_summary`
    /// on every board refresh by `sync_ci_checks_from_board` — no client-side
    /// `gh pr checks` calls (superseded the old `pipeline_ci_loader` poll loop).
    pipeline_ci_checks: std::collections::HashMap<(String, i64), CiCheckSummary>,
    /// Pipeline issues dismissed from the Done section by the user pressing 'D'.
    ///
    /// Keyed by `(repo_slug, issue_number)`.  Dismissed issues are hidden from
    /// the sidebar for the lifetime of the current session; they reappear on
    /// the next startup (or can be re-fetched if the user clears the set).
    /// This is intentionally in-memory: for MVP, hiding accumulation is
    /// sufficient — no persistence needed.
    pipeline_dismissed: std::collections::HashSet<(String, u64)>,
    /// #290: issues for which a `coord merge` was just dispatched via the
    /// Pipeline view's Go button but the local DB hasn't yet confirmed the
    /// new merge_queue entry.  `merge_stage_status_for` returns Active for
    /// these issues so the Merge box turns blue and the Go button drops
    /// immediately — without waiting for the next DB refresh.  Cleared in
    /// `apply_pending_data` once the real merge_queue row lands.
    pipeline_inflight_merges: std::collections::HashSet<(String, u64)>,
    /// #319 Phase A: armed when the user triggers the refinement-notes
    /// finaliser (Ctrl+N in a refinement chat).  The tick poll watches the
    /// focused chat for the assistant's reply, captures it on `end_turn`,
    /// and opens [`Self::refinement_notes_modal`] for review-and-post.
    /// Cleared on modal open, timeout, or empty reply.
    pending_refinement_notes_synth: Option<PendingRefinementNotesSynth>,
    /// #319 Phase A: review-and-post modal showing the proposed
    /// refinement-notes comment.  When `Some`, intercepts all keyboard
    /// input — see [`Self::handle_refinement_notes_modal_key`].
    refinement_notes_modal: Option<RefinementNotesModal>,
    /// #319 Phase A: receiver for the background `gh issue comment` thread
    /// spawned by [`Self::post_refinement_notes`].  Drained on tick; the
    /// modal stays open on failure so the typed text isn't lost.
    refinement_notes_post_rx: Option<std::sync::mpsc::Receiver<RefinementNotesPostResult>>,
    /// #328: Y/N/Esc prompt shown when the user presses Esc on a refinement
    /// chat that has any user turns.  `None` ⇒ Esc finalises immediately,
    /// matching the empty-chat fast-exit.
    pending_refinement_close_prompt: Option<PendingRefinementClosePrompt>,
    /// #328: when true, a successful `gh issue comment` post triggers the
    /// finalise (stop + ready) chain — used to make the close-prompt Y
    /// path "draft, post, then mark ready" rather than leaving the chat
    /// open after a successful post.  Cleared on modal Esc or failure.
    finalise_after_notes_post: bool,
    /// #316 Phase A+C: pending board-chat dispatch — armed when the user
    /// clicks Refine or New Issue in the Board Chat tab and `coord
    /// refine-board` / `coord new-issue-chat` is running.  Polled each
    /// tick; on bind we open the chat overlay in the Board Chat tab.
    pending_board_chat: Option<PendingBoardChat>,
    /// #1017: pending milestone-chat dispatch — armed by the Plans-panel
    /// "New milestone via chat" / "Open milestone chat" / "Add sub-issue via
    /// chat…" entries.  Polled each tick; on bind we attach the live chat
    /// overlay to the new `type="milestone-chat"` session so the operator can
    /// converse in it (the #1017 review fix — previously fire-and-forget).
    pending_milestone_chat: Option<PendingMilestoneChat>,
    /// #2533: pending decomposition-chat dispatch — armed when the operator
    /// pulls an Approved-work-items row into a decomposition session
    /// (`dispatch_approved_pull_into_decomposition`). Polled each tick; on
    /// bind we attach the live chat overlay to the new
    /// `type="decomposition-chat"` session, same lifecycle as
    /// `pending_board_chat` / `pending_milestone_chat`.
    pending_decomposition_chat: Option<PendingDecompositionChat>,
    /// #353: pending repo picker for the [Add] button on the Board panel.
    /// Armed when the user clicks [Add] and multiple repos exist. The picker
    /// intercepts numeric keys (1, 2, …) to select a repo, or Esc to cancel.
    /// Cleared when a repo is selected or the picker times out.
    pending_repo_picker: Option<PendingRepoPicker>,
    /// #486 Leg 4: pending fleet-machine picker for a remote interactive
    /// Review/Fix launch.  Intercepts numeric keys (1, 2, …) to pick the
    /// target machine, or Esc to cancel.  Cleared when a machine is chosen.
    pending_machine_picker: Option<PendingMachinePicker>,
    /// #954: pending machine picker for "new terminal" (`n` in the Terminal
    /// view). Distinct from `pending_machine_picker` — a new terminal
    /// carries no repo/issue, so EVERY configured machine is a candidate
    /// (`fleet_machines_for_terminal`, not `fleet_machines_for_repo`), and
    /// picking one opens the name prompt rather than launching an
    /// interactive session. Intercepts numeric keys (1, 2, …); Esc cancels.
    pending_new_terminal_picker: Option<Vec<MachinePickEntry>>,
    /// #954: pending optional-name prompt for a terminal about to be
    /// created — see `PendingNewTerminal`.
    pending_new_terminal: Option<PendingNewTerminal>,
    /// #2147: pending "Set quiet hours…" dialog — see `PendingQuietHours`.
    pending_quiet_hours: Option<PendingQuietHours>,
    /// #935 Part B: parsed results from a `coord diagnose --json --dry-run`
    /// run, waiting for the user to choose Recover / Reset / Clear phantom /
    /// Dismiss in `build_prompt_dialog`.
    pending_diagnose_dialog: Option<PendingDiagnoseDialog>,
    /// #935: `(repo, issue)` awaiting the no-`--json` retry we dispatched
    /// because a version-skewed `coord` CLI/daemon rejected the `--json`
    /// diagnose flag (Click "No such option '--json'", exit 2, no stdout).
    /// The retry runs the SAME dry-run without `--json`; its completion is
    /// routed into the legacy options dialog (best-effort text parse) instead
    /// of a bare "Command failed" toast.
    pending_diagnose_legacy_retry: Option<(String, u64)>,
    /// Force-quit confirmation.  Set when Esc/q is pressed while an
    /// interactive session is live in the Terminal tab — instead of silently
    /// swallowing the keypress (a dead end), we show a dialog so the operator
    /// can confirm quitting (the session keeps running in tmux) or cancel.
    pending_quit_confirm: bool,
    /// Set by the force-quit dialog's mouse button (the click path can't
    /// return `Reaction::Exit` directly); checked after mouse handling.
    quit_requested: bool,
    /// #316 Phase B: file-issue edit modal.  Shown after the user chooses
    /// to file the issue drafted in a new-issue-chat.  Intercepts all
    /// keyboard input while open.
    file_issue_modal: Option<FileIssueModal>,
    /// #316 Phase B: receiver for the background `gh issue create` thread.
    file_issue_post_rx: Option<std::sync::mpsc::Receiver<FileIssuePostResult>>,
    /// #336: Cache of artifact manifests keyed by `(repo, sanitized_branch)`.
    /// Entries have a 30-second TTL; stale entries trigger a background re-fetch.
    artifact_cache: std::collections::HashMap<(String, String), ArtifactCacheEntry>,
    /// #336: In-flight artifact manifest fetch.  `None` when idle.
    artifact_fetch_rx: Option<(
        (String, String),
        std::sync::mpsc::Receiver<ArtifactFetchOutcome>,
    )>,
    /// #1337: Full `review_findings` bodies hydrated from the daemon's
    /// `GET /assignment/{id}` detail endpoint, keyed by `(assignment_id,
    /// stored_len)` — the /board collection wire carries only a bounded
    /// preview (`review_findings_truncated`).  A successful entry never
    /// expires (findings for one assignment are effectively immutable —
    /// #650: one review per id; a force-overwrite changes
    /// `review_findings_len` and therefore the key, re-fetching naturally).
    /// A failed entry (`full: None`) backs off for 30 s before re-arming.
    findings_detail_cache:
        std::collections::HashMap<(String, i64), FindingsDetailEntry>,
    /// #1337: In-flight findings detail fetch.  `None` when idle.
    findings_fetch_rx: Option<(
        (String, i64),
        std::sync::mpsc::Receiver<Option<String>>,
    )>,
    /// #2497: Full issue bodies hydrated from the daemon's
    /// `GET /issue/{repo}/{number}` detail endpoint, keyed by `(repo,
    /// issue_number)` — the /board collection wire drops a closed
    /// (non-epic) issue's body to 0 chars (`board_wire.bound_issue_row`,
    /// #1791). A successful entry never expires (a closed issue's body is
    /// immutable in practice). A failed entry (`full: None`) backs off for
    /// 30 s before re-arming. Mirrors `findings_detail_cache` (#1337).
    issue_detail_cache: std::collections::HashMap<(String, u64), IssueDetailEntry>,
    /// #2497: In-flight issue-body detail fetch.  `None` when idle.
    issue_fetch_rx: Option<(
        (String, u64),
        std::sync::mpsc::Receiver<Option<String>>,
    )>,
    /// #336: Tracks a pending `coord pull-artifact` dispatch.
    /// Contains `(work_id, repo, sanitized_branch)` so we can show the
    /// destination path in a completion dialog.
    pending_artifact_pull: Option<(String, String, String)>,
    /// #434: Durable record of the last `coord pull-artifact` per work-id.
    /// Keyed by `work_id`.  Drives the persistent "Last pull: …" line in the
    /// Pipeline detail panel so the user who missed the initial dialog can still
    /// see the result.
    last_artifact_pulls: std::collections::HashMap<String, ArtifactPullResult>,
    /// #532: Open artifact-pull info dialog.  `Some` while the dialog is
    /// visible.  Cleared on dismiss (Esc / outside click / 'c' copy action).
    artifact_pull_dialog: Option<ArtifactPullDialog>,
    /// #399: Cache for parsed/wrapped log content items.
    ///
    /// Avoids the full O(n) re-parse of every log line on every render tick
    /// (~60 fps).  Rebuilt only when the assignment, line count, or wrap
    /// width changes.  `RefCell` so `pipeline_log_list` can update from `&self`.
    /// Extended incrementally (#787) when the SSE stream grows.
    log_items_cache: std::cell::RefCell<Option<LogItemsCache>>,
    /// #787: true while a tick source requested a redraw but the 15-fps
    /// throttle gate has not yet elapsed.  Cleared when the redraw fires.
    redraw_pending: bool,
    /// #787: when the most recent tick-driven `Reaction::Redraw` was emitted.
    last_redraw_at: Instant,

    // ── #349: Test-plan state ─────────────────────────────────────────────────
    /// Work-assignment IDs for which we have already queued a
    /// `coord test-plan` spawn (or are waiting for it to complete).
    /// Prevents duplicate spawns — once an ID is in this set the
    /// "Preparing plan…" placeholder is shown and no further spawns are issued
    /// until the plan lands (after which `assignment.test_plan` becomes `Some`
    /// and the set entry is irrelevant).  Cleared when the test stage focus
    /// moves to a different assignment so a re-focus always re-checks.
    test_plan_pending: std::collections::HashSet<String>,
    /// Assignment ID for which we last ran the staleness check.  Avoids
    /// re-running the git HEAD read on every tick while the same stage is
    /// focused; reset when the pipeline selection or focused stage changes.
    test_plan_staleness_checked_for: Option<String>,
    /// In-flight plan-step jobs keyed by (work_id, step_index).  Spawned
    /// when the user presses 1–9 (or `a` for pull steps) in the test stage.
    /// Polled each tick.
    test_step_jobs: std::collections::HashMap<(String, usize), TestStepJob>,
    /// Completed step exit codes keyed by (work_id, step_index).  Persists
    /// for the lifetime of the session so the rendered panel always shows the
    /// last outcome even after the job is drained.
    test_step_results: std::collections::HashMap<(String, usize), i32>,
    /// Captured stdout+stderr for completed test-plan steps, keyed by
    /// (work_id, step_index).  Displayed inline below each step row so the
    /// user can see *why* a step passed or failed without leaving the panel.
    test_step_output: std::collections::HashMap<(String, usize), String>,
    // ── #424: embedded terminal pane state ──────────────────────────────
    /// Live PTY-backed shell session shown when `active_view ==
    /// SidebarView::Terminal`.  Lazily spawned by [`tick`] the first
    /// time the user opens the Terminal view; persisted across view
    /// switches (switching to Board then back to Terminal does NOT
    /// respawn the shell).  Becomes `None` only on initial construction
    /// or if the initial spawn failed (error captured in
    /// `terminal_spawn_error`).
    terminal_session: Option<quadraui::terminal_engine::TerminalSession>,
    /// FOCUS PROTOCOL (#424):
    ///   - `true`  → keystrokes on the Terminal view encode to xterm
    ///     escape sequences and write to the PTY (`write_input`).  TUI
    ///     chrome view-switching (1/2/3/4/5) is INACTIVE in this mode;
    ///     press F12 to release focus.
    ///   - `false` → keystrokes drive normal TUI chrome (view switching,
    ///     hotkeys); the PTY output is still visible but read-only.
    /// Default on entering the Terminal view: `true`.
    /// Toggle key: F12 (chosen because it is rarely produced by the
    /// PTY and is a single key — no chord needed).
    terminal_focused: bool,
    /// Pending PTY resize target written by [`render_content`] (which
    /// only has `&self`) and applied by [`tick`] (`&mut self`).  Stores
    /// `(cols, rows)` derived from the live viewport rect divided by
    /// the backend's cell metrics.  `Cell` because render is `&self`.
    terminal_pending_dims: std::cell::Cell<Option<(u16, u16)>>,
    /// Error captured if `TerminalSession::spawn` failed (missing
    /// `$SHELL`, fork failure, etc.).  Displayed in the Terminal pane
    /// as a one-line placeholder so the user knows why nothing rendered.
    terminal_spawn_error: Option<String>,
    /// #1029 bug A fix: queued by [`Self::switch_active_view`], polled once
    /// by `ShellApp::take_requested_panel` (`render.rs`). Lets an action
    /// handler (e.g. `launch_milestone_chat_session`) jump `active_view`
    /// straight to a different panel — no ActivityBar click — while still
    /// keeping quadraui's AppShell chrome (ActivityBar highlight + sidebar
    /// panel header) in sync, instead of the two drifting apart the way a
    /// raw `self.active_view = ...` write left them.
    pending_panel_switch: Option<WidgetId>,
    /// #1029 bug B (iter-2): marks the *next* `on_shell_event` as the
    /// programmatic replay of a queued [`Self::pending_panel_switch`] rather
    /// than a fresh operator mouse click. Set in
    /// [`ShellApp::take_requested_panel`] the moment quadraui pulls the
    /// queued panel (it always follows that pull with an
    /// `on_shell_event(PanelChanged)` — see quadraui `apply_requested_panel`),
    /// and consumed by `on_shell_event`. Needed because both a real
    /// ActivityBar click *and* our own programmatic switch funnel through the
    /// same `on_shell_event`, but only a real click should invalidate the
    /// `terminal_return_view` bookmark — the programmatic replay of a
    /// milestone-chat launch must leave the freshly-set bookmark intact.
    pending_switch_is_programmatic: bool,
    /// #1029 bug B fix: the view to restore on Esc-close of a standalone
    /// Terminal session that was launched *from* somewhere other than the
    /// Terminal panel itself (currently: milestone chat, set in
    /// `launch_milestone_chat_session`). `None` means there is nothing to
    /// return to — e.g. the Terminal panel was reached by an ordinary
    /// ActivityBar click, or the return has already been consumed.
    ///
    /// Invalidated aggressively so a stale bookmark can never replay a jump
    /// from an earlier, unrelated flow (iter-2 fix): (1) `switch_active_view`
    /// clears it on *every* view switch — including switches *into* Terminal
    /// for an unrelated reason (review/fix/merge/fleet/reattach); (2)
    /// `on_shell_event` clears it on every real (non-programmatic) ActivityBar
    /// click. The single site that legitimately wants a bookmark
    /// (`launch_milestone_chat_session`) re-sets it *after* switching, and its
    /// programmatic replay is skipped via `pending_switch_is_programmatic`.
    terminal_return_view: Option<SidebarView>,
    // ── #440: per-issue detail-view terminals ──────────────────────────
    /// Per-issue terminal sessions for the Pipeline detail Terminal tab.
    /// Keyed by `(repo_slug, issue_number)` (#455) so that same-numbered
    /// issues in different repos (e.g. quadraui #336 vs coord #336) each
    /// get their own session.  Lazily spawned the first time the user
    /// opens the Terminal tab for a given issue; kept running while the
    /// issue stays in `pipeline_issues`; dropped when the issue leaves
    /// the pipeline (on the next load that no longer contains it).
    detail_terminal_sessions: std::collections::HashMap<(String, u64), quadraui::terminal_engine::TerminalSession>,
    /// Spawn errors for per-issue terminals (shown as a one-line
    /// placeholder in the Terminal tab).  Keyed by `(repo_slug, issue_number)`.
    detail_terminal_spawn_errors: std::collections::HashMap<(String, u64), String>,
    /// Focus state for the Pipeline detail Terminal tab.  `true` when
    /// keypresses route to the selected issue's PTY.  F12 toggles.
    /// Independent of `terminal_focused` (the standalone pane's flag).
    detail_terminal_focused: bool,
    /// #605: `Ctrl-W` pane-leader latch — `true` after the user presses
    /// `Ctrl-W`, until the next key resolves the chord (`h`/`Left` → side
    /// panel, `l`/`Right` → content/PTY, `Ctrl-W` → literal to the PTY).
    /// A keyboard-only slice of the #578 focus model (no mouse focus).
    ctrl_w_pending: bool,
    /// #782: which of the three pane regions (Sidebar / Main / Detail) has
    /// keyboard focus.  Cycled by `Ctrl-W Left` / `Ctrl-W Right`.
    /// When a PTY-backed terminal is shown, this also controls the
    /// `terminal_focused` / `detail_terminal_focused` flags.
    focused_region: FocusedRegion,
    /// Pending `(cols, rows)` for per-issue terminal resize, stashed by
    /// `render_detail_terminal_tab` (`&self`) and applied by `tick`
    /// (`&mut self`).
    detail_terminal_pending_dims: std::cell::Cell<Option<(u16, u16)>>,
    /// #454: bitmask of mouse buttons whose `Press` was forwarded to an
    /// embedded PTY but whose matching `Release` has not yet been
    /// forwarded.  Used so that a release fires even when the cursor
    /// has been dragged outside the terminal content area — without it,
    /// terminal apps (vim visual mode, tmux mouse drag, less) stay
    /// stuck in "button held" state until the next click.
    /// Bits: `1` = Left, `2` = Middle, `4` = Right.
    pty_pressed_buttons: u8,
    /// #464: `true` while a host-side text-selection drag is in progress in
    /// the active terminal pane. Set when mouse-reporting is OFF (plain
    /// shell) or Shift is held (force-override). Cleared on the matching
    /// mouse-release. `pty_pressed_buttons` is NOT set for these drags.
    terminal_host_sel_dragging: bool,
    /// #790: `true` while the terminal pane is in keyboard-toggled "copy
    /// mode" (F9).  Shift+drag is intercepted by an outer tmux before
    /// coord-tui ever sees it, so a mouse modifier can't reliably trigger a
    /// host selection; copy mode is the tmux-proof alternative.  While it is
    /// on, mouse events are NOT forwarded to the embedded PTY — a plain
    /// left-drag begins a host-side selection (via `terminal_should_host_select`)
    /// that Ctrl+C copies.  Only meaningful in the copy-capable terminal
    /// contexts (`terminal_copy_mode_available`); auto-cleared when the view
    /// changes out from under it.
    terminal_copy_mode: bool,

    // ── #207: Machine metrics sparklines ─────────────────────────────────
    /// Rolling ring-buffer of CPU + memory samples per machine, keyed by
    /// machine name.  Capped at [`METRICS_HISTORY`] entries (60 × 5 s =
    /// 5 minutes).  Populated by background `/metrics` polls.
    machine_metrics: std::collections::HashMap<String, std::collections::VecDeque<MetricSample>>,
    /// In-flight `/metrics` fetches — one entry per machine per poll cycle.
    /// Drained each tick; completed entries update `machine_metrics`.
    pending_metrics: Vec<PendingMetrics>,
    /// When the last `/metrics` poll round was kicked.  The next round fires
    /// after [`METRICS_CADENCE`] has elapsed and the Machines panel is visible.
    metrics_last_polled: Instant,

    // ── #487: live tmux session discovery ──────────────────────────────────
    /// Running `coord-*` tmux sessions discovered at startup via
    /// `coord sessions --json`.  Each entry represents an interactive
    /// session that survived a previous TUI run (or is still active in
    /// the background).  Displayed as a startup toast and used by
    /// `launch_interactive_session_for_selected_issue` to offer reattach
    /// instead of starting a fresh session when one already exists.
    live_tmux_sessions: Vec<LiveTmuxSession>,
    /// #486 Leg 4: background fetch of local + REMOTE sessions (ssh-probes the
    /// fleet).  When it lands, it REPLACES `live_tmux_sessions` (a superset of
    /// the local-only startup snapshot) so reattach can target remote sessions.
    pending_remote_sessions:
        Option<std::sync::mpsc::Receiver<Vec<LiveTmuxSession>>>,

    // ── #953: fleet terminal discovery (Terminal-view left-pane tree) ───────
    /// Persistent `coord-term-*` terminals discovered via `coord terminal
    /// list --json[--remote]`, grouped by machine in the Terminal view's
    /// left-pane tree (`fleet_terminals` module). Separate from
    /// `live_tmux_sessions` (assignment-scoped `coord-<aid>` sessions) —
    /// different prefix, different data model, no board/pipeline involvement.
    fleet_terminals: Vec<FleetTerminal>,
    /// In-flight background sweep of `coord terminal list --json --remote`,
    /// armed at startup and re-armed by `refresh()`. Mirrors
    /// `pending_remote_sessions`; when it lands it merges into
    /// `fleet_terminals` (#954: preserving any not-yet-covered `pending`
    /// optimistic entry, see `poll_remote_terminals`).
    pending_remote_terminals: Option<std::sync::mpsc::Receiver<Vec<FleetTerminal>>>,
    /// Per-machine expand state for the Terminal-view tree, keyed by machine
    /// name. Absent entries default to expanded when the machine hosts ≥1
    /// terminal, collapsed (no chevron) otherwise.
    terminal_tree_expanded: std::collections::HashMap<String, bool>,
    /// Selected-node cursor for the Terminal-view tree: `[machine_idx]` for
    /// a machine row, `[machine_idx, terminal_idx]` for a terminal row.
    terminal_tree_selected: Option<TreePath>,
    /// Scroll offset (in flattened tree rows) for the Terminal-view tree.
    terminal_tree_scroll: usize,
    /// #956: pending "Kill terminal" confirmation — armed by `K` or the
    /// context-menu "Kill terminal" item on a selected terminal-tree node.
    /// `y`/`Y` (or the dialog's confirm button) fires the kill; any other
    /// key/outside click cancels.  Mirrors `pending_restart`.
    pending_kill_terminal: Option<PendingKillTerminal>,

    // ── #1398: unattended `coord drive --tmux` session discovery ────────────
    /// Live `coord-drive-<repo>-<issue>` tmux sessions, discovered via `coord
    /// drive-sessions --json` (`drive` module). Drives the Pipeline row
    /// "driving" badge, the context-menu Attach/Stop gate, and the per-issue
    /// Terminal tab's auto-attach. LOCAL ONLY — no `--remote` sweep, since a
    /// drive runs on the operator's own machine (see the `drive` module doc).
    drive_sessions: Vec<DriveSession>,
    /// In-flight background sweep of `coord drive-sessions --json`, armed at
    /// startup and re-armed by `refresh()`. Mirrors `pending_remote_terminals`;
    /// when it lands it merges into `drive_sessions` (preserving any
    /// not-yet-covered `pending` optimistic entry, see `poll_drive_sessions`).
    pending_drive_sessions: Option<std::sync::mpsc::Receiver<Vec<DriveSession>>>,
    /// Pending "Stop drive" confirmation — armed by the context-menu "Stop
    /// drive" item. The dialog's confirm button fires `confirm_kill_drive`;
    /// any other key/outside click cancels. Mirrors `pending_kill_terminal`.
    pending_kill_drive: Option<PendingKillDrive>,

    // ── #1032: Sessions panel (machine → repo → session tree) ───────────────
    /// Per-machine expand state for the Sessions-view tree, keyed by machine
    /// name. Absent entries default to expanded when the machine hosts ≥1
    /// live session, collapsed (no chevron) otherwise. Mirrors
    /// `terminal_tree_expanded`; repo groups beneath an expanded machine are
    /// always shown (no separate per-repo collapse in this read-only slice).
    sessions_tree_expanded: std::collections::HashMap<String, bool>,
    /// Selected-node cursor for the Sessions-view tree: `[machine_idx]` for a
    /// machine row, `[machine_idx, repo_idx]` for a repo-group row,
    /// `[machine_idx, repo_idx, session_idx]` for a session leaf.
    sessions_tree_selected: Option<TreePath>,
    /// Scroll offset (in flattened tree rows) for the Sessions-view tree.
    sessions_tree_scroll: usize,
    /// #1033: pending "Kill session" confirmation — armed by `K` on a
    /// selected Sessions-tree leaf. `y`/`Y` (or the dialog's confirm button)
    /// fires the kill; any other key/outside click cancels. Mirrors
    /// `pending_kill_terminal`.
    pending_kill_session: Option<PendingKillSession>,

    // ── #955: attached fleet-terminal sessions (Terminal-view main pane) ───
    /// Local PTYs running `coord terminal attach <machine:name>`, keyed by
    /// `(machine, name)` — one per fleet-terminal tree leaf the operator has
    /// selected this run. Mirrors `detail_terminal_sessions`'s per-key
    /// cache: kept warm across selection swaps so switching back to a
    /// previously-attached terminal is instant rather than re-spawning +
    /// re-attaching. Selecting a *different* leaf does not evict the prior
    /// entry — only the render path (`SidebarView::Terminal` in
    /// `render.rs`) decides which one is currently visible, driven by
    /// `terminal_tree_selected`. Distinct from `terminal_session` (the
    /// bare-`$SHELL` fallback used when no fleet-terminal leaf is
    /// selected — e.g. an empty tree, or a machine row selected).
    /// Values are [`self::terminal::FleetTerminalSession`] (#955), a thin
    /// wrapper that cleanly detaches (never kills) the tmux client on drop
    /// — see that type's doc comment for why a plain `TerminalSession`
    /// isn't safe here.
    fleet_terminal_sessions: std::collections::HashMap<(String, String), FleetTerminalSession>,
    /// Spawn/attach errors for `fleet_terminal_sessions`, keyed the same
    /// way — mirrors `detail_terminal_spawn_errors`. Prevents an endless
    /// respawn loop (e.g. target machine unreachable over ssh) and lets the
    /// Terminal pane show a readable diagnostic instead of a blank screen.
    fleet_terminal_spawn_errors: std::collections::HashMap<(String, String), String>,

    // ── #603: exact fix-briefing preview for the fail→fix / rework dialog ───
    /// The resolved fix briefing text (context block + findings/test story),
    /// shown in the confirm dialog so the operator sees what the fix worker
    /// gets BEFORE launching.  `Some("Resolving…")` while the async
    /// `coord fix-briefing` shell-out is in flight; cleared when the dialog is
    /// dismissed.
    fix_briefing_preview: Option<String>,
    /// In-flight `coord fix-briefing <aid>` fetch feeding `fix_briefing_preview`.
    fix_briefing_rx: Option<std::sync::mpsc::Receiver<String>>,

    // ── Leg 2 (#517): auto-advance Work → Review ───────────────────────────
    /// Interactive Work/Plan sessions launched this run, keyed by
    /// `(repo_slug, issue_number)`, armed to prompt for a review once the
    /// board shows the work finished.  Entry removed when the prompt fires
    /// or a Review is launched for the issue.  See [`ArmedAutoReview`].
    armed_for_auto_review: std::collections::HashMap<(String, u64), ArmedAutoReview>,
    /// When `Some`, an inline confirm prompt is up asking whether to launch
    /// the interactive review for a just-finished work session.  Enter
    /// confirms; Esc/n dismisses.  See [`PendingAutoReview`].
    pending_auto_review: Option<PendingAutoReview>,
    /// Post-review one-key stage offer (Fix / Test) — see [`PendingStageLaunch`].
    pending_stage_launch: Option<PendingStageLaunch>,
    /// #685: pending test-mode choice dialog.  Armed when the operator picks
    /// "Start (automated) > Work/Plan" from the Pipeline context menu or the
    /// "Set test mode" right-click item; cleared on confirm or Esc.
    pending_test_mode_choice: Option<PendingTestModeChoice>,
    /// #685: set of work assignment IDs for which the TUI already raised the
    /// interactive-smoke offer (via `detect_headless_smoke_work_done`).  Prevents
    /// the offer from re-firing on every tick after the user dismisses it.
    offered_smoke_for_headless_work: std::collections::HashSet<String>,

    // ── Leg 3 (#517): verdict-driven routing ───────────────────────────────
    /// Interactive reviews launched this run, keyed by `(repo_slug,
    /// issue_number)`, armed to route on their reported verdict.  Entry removed
    /// when the verdict is routed.  See [`ArmedVerdict`].
    armed_for_verdict: std::collections::HashMap<(String, u64), ArmedVerdict>,
    /// When `Some`, an inline confirm prompt is up asking whether to launch the
    /// interactive `--fix-of` session for a request-changes verdict.  Enter
    /// confirms; Esc dismisses.  See [`PendingRework`].
    pending_rework: Option<PendingRework>,
    /// #587: one-shot flag set by `confirm_rework` to suppress the secondary
    /// "no findings captured" gate inside `launch_interactive_session_for_selected_issue`
    /// while it routes through Fix mode to launch the session.  Cleared on
    /// the same tick, so it never survives a full user-initiated Fix request.
    rework_bypass: bool,

    // ── Leg 3c / A3 (#517, #581): test-verdict routing ─────────────────────
    /// Interactive testing sessions launched this run, keyed by `(repo_slug,
    /// issue_number)`, armed to route on the WORK row's recorded test verdict.
    /// Entry removed when the verdict is routed.  See [`ArmedTestVerdict`].
    armed_for_test_verdict: std::collections::HashMap<(String, u64), ArmedTestVerdict>,
    /// When `Some`, an inline confirm prompt is up asking whether to launch the
    /// interactive `--fix-of` fix for a FAILED test.  See [`PendingTestFix`].
    pending_test_fix: Option<PendingTestFix>,
    /// When `Some`, an inline confirm prompt is up asking whether to launch the
    /// interactive `--merge-of` merge agent after a PASSED test.  See
    /// [`PendingMerge`].
    pending_merge: Option<PendingMerge>,

    // ── #863: force-past-iteration-cap for the interactive Fix action ──────
    /// While `Some`, a headless `coord assign --fix-of --dry-run` preflight
    /// (spawned via `command_runner`) is in flight to detect a
    /// `pipeline.max_review_iterations` cap refusal BEFORE the human-attended
    /// terminal opens.  Consumed by the `run_periodic_work` completion
    /// handler, matched against the completed `CommandResult` by
    /// `work_aid`.  See [`PendingFixCapPreflight`].
    pending_fix_cap_preflight: Option<PendingFixCapPreflight>,
    /// When `Some`, an inline confirm prompt is up asking whether to
    /// re-dispatch the same Fix with `--force` (#862) after the preflight
    /// reported the iteration cap was hit.  Enter confirms; Esc/n dismisses.
    /// See [`PendingFixForceConfirm`].
    pending_fix_force_confirm: Option<PendingFixForceConfirm>,

    // ── #638: Kanban view ────────────────────────────────────────────────────
    kanban_model: BoardModel,
    kanban_layout: std::cell::RefCell<Option<BoardLayout>>,

    // ── #737: Merge Queue panel ──────────────────────────────────────────────
    /// Selected entry index in the Merge Queue panel list (0-based).  Clamped
    /// to `data.merge_queue.len().saturating_sub(1)` on navigation so it never
    /// goes out of bounds even after a data refresh.
    merge_queue_sel: usize,
    /// Scroll offset (first visible row index) for the Merge Queue panel list.
    /// Updated by `fix_merge_queue_scroll` after every j/k/Home/End navigation
    /// and passed as `scroll_offset` to `draw_list` so the selected item stays
    /// in the visible window.  See quadraui `primitives/list.rs`: "The app
    /// updates `selected_idx` and `scroll_offset` for the next frame."
    merge_queue_scroll: usize,

    // ── #771: Milestone DAG panel (Phase 3 of #767) ──────────────────────────
    /// Selected milestone index (0-based) into `milestone_dag_views()`'s
    /// return order.  Clamped to bounds on navigation, same pattern as
    /// `merge_queue_sel`.
    milestone_dag_sel: usize,

    // ── #1124: reusable quadraui help layer (`?` cheatsheet + `/` palette) ───
    /// Registered per-view help content (cheatsheet notes + palette
    /// actions), keyed by [`SidebarView::help_view_id`]. Populated once in
    /// [`CoordApp::new`] — currently only `"panel:plans"` is registered;
    /// see the module docs at the top of `plans.rs` for the pattern other
    /// panels copy as they adopt `?`/`/`.
    help_registry: HelpRegistry,
    /// `?`-triggered cheatsheet overlay state machine, shared across every
    /// view that registers help content — only one can be open at a time.
    /// Painted by `plans.rs::render_help_overlay` rather than
    /// [`HelpOverlayController::render`] — see that function's doc comment
    /// for why (contract §5b's exact title-string ordering).
    help_overlay: HelpOverlayController,
    /// `/`-triggered command palette, shared the same way as
    /// `help_overlay` above. `None` when closed.
    command_palette: Option<DualModePaletteController>,

    // ── #975: Plans ActivityBar panel ────────────────────────────────────────
    /// Selected plan-roster index (0-based) into `plans_visible_entries()`'s
    /// return order (#1001: the currently-*rendered* rows, not the full
    /// unfiltered roster — collapsed no-work-order milestones aren't
    /// selectable).  Clamped to bounds on navigation, same pattern as
    /// `milestone_dag_sel`.
    plans_sel: usize,
    /// Repos whose "without a work order" milestones are expanded in the
    /// Plans panel (#1001).  Absent = collapsed (the default) — only
    /// `has_work_order` milestones render under that repo's header, with a
    /// trailing "+N without a work order" summary line instead. Toggled with
    /// `u` on the repo of the currently-selected row.
    plans_expanded_repos: std::collections::HashSet<String>,
    /// `true` when the #1122 in-app plan detail pane is open, replacing the
    /// roster list in the Plans main panel — `Enter` on a row whose
    /// `tracking_issue` is `Some(_)` opens it (`open_selected_plan_detail`,
    /// `plans.rs`); `Esc` closes it back to the list (mirrors
    /// `audit_detail_open`'s list/detail toggle).
    plans_detail_open: bool,
    /// #1122 fix-iteration-1 (review finding): real scroll offset for the
    /// detail pane's `ListView`, shared by the paint path
    /// (`render_plan_detail_pane`) and the click hit-test path
    /// (`plan_detail_action_at`) — both previously hardcoded
    /// `scroll_offset: 0`, which meant any row past the first screenful
    /// (including the contract §3d actions row, on any plan whose work
    /// order was long enough to fill the viewport) could never be revealed
    /// or clicked. Kept following `plans_detail_sel` by
    /// `fix_plans_detail_scroll`, same structural pattern as
    /// `audit_scroll`/`fix_audit_scroll` (#1094). Reset to 0 whenever the
    /// pane (re)opens.
    plans_detail_scroll: usize,
    /// Selected row index (0-based) into the #1122 detail pane's flattened
    /// item list (`CoordApp::plan_detail_items`) — moved by `j`/`k` while
    /// the pane is open (events.rs) and kept inside the visible window by
    /// `fix_plans_detail_scroll`. Reset to 0 whenever the pane (re)opens.
    plans_detail_sel: usize,
    /// The `(repo, milestone_number)` identity of the plan the #1122 detail
    /// pane was opened for, captured once by `open_selected_plan_detail`.
    /// `plans_selected()` resolves through this — instead of `plans_sel`
    /// indexing into `plans_visible_entries()` — whenever `plans_detail_open`
    /// is true, so a board refresh that reorders/shrinks/grows the visible
    /// roster while the pane is open can never silently swap the pane onto a
    /// different plan than the one the operator opened (review finding on
    /// #1122's non-blocking concerns). `None` when the pane is closed.
    plans_detail_target: Option<(String, i64)>,

    // ── #1121: Plans sidebar repo→plan tree + repo scoping ──────────────────
    /// Per-repo expand state for the Plans sidebar `TreeView`, keyed by repo
    /// name. Absent entries default to collapsed (mirrors the main panel's
    /// own #1001 collapsed-by-default convention — see
    /// `plans_tree_repo_expanded` in `plans.rs`). Distinct from
    /// `plans_expanded_repos` above: that one controls whether *untracked*
    /// milestones show inline in the main panel; this one controls whether
    /// the *sidebar tree* shows a repo's milestone children at all.
    plans_tree_expanded: std::collections::HashMap<String, bool>,
    /// Selected-node cursor for the Plans sidebar tree: `None`/`[0]` = "All
    /// repos" (no scoping — the pre-#1121 unfiltered behaviour), `[i+1]` =
    /// the repo at index `i` of `plans_repo_list()`, `[i+1, j]` = the `j`th
    /// milestone of that repo's `plans_entries()` group. Drives
    /// `plans_scope_repo()` / `plans_scoped_entries()`, which every
    /// main-panel rendering/selection path now reads through.
    plans_tree_selected: Option<TreePath>,
    /// Scroll offset (in flattened tree rows) for the Plans sidebar tree.
    plans_tree_scroll: usize,

    // ── #1039: Audit ActivityBar panel ───────────────────────────────────────
    /// Cached first page of `/audit` (#1037), fetched by `spawn_audit_fetch`
    /// (`data.rs`) and armed only while `active_view == SidebarView::Audit`
    /// (mirrors `artifact_cache`/`artifact_fetch_rx`'s Pipeline-only
    /// gating). `None` before the first fetch completes — the panel renders
    /// the same "No audit events yet." empty state as a genuinely-empty
    /// page (contract §4b) so a slow first fetch never reads as broken.
    audit_page: Option<AuditPage>,
    /// In-flight `/audit` fetch, if any. Drained each tick in the same poll
    /// loop that drains `artifact_fetch_rx`/`pending_metrics`.
    audit_fetch_rx: Option<std::sync::mpsc::Receiver<AuditFetchOutcome>>,
    /// When the current `audit_page` was fetched — drives the re-fetch
    /// cadence while the panel is active (mirrors `ArtifactCacheEntry`'s
    /// TTL). `None` forces an immediate fetch on the next tick the panel is
    /// visible (startup, or after `r` — see below).
    audit_last_fetched: Option<Instant>,
    /// Selected row index (0-based) into `audit_page`'s entries. Clamped to
    /// bounds on navigation, same pattern as `plans_sel`.
    audit_sel: usize,
    /// `true` when the inline entry-detail pane (Enter on a selected row) is
    /// open. `Esc` closes it back to the list-only view (contract §4c/§7).
    audit_detail_open: bool,
    /// Reason the most recent `/audit` fetch failed (network error / bad
    /// status / parse error), or `None` when the last attempt succeeded (or
    /// no board service is configured — that's not an error, just nothing
    /// to fetch from). Appended to the empty-state message so a genuinely
    /// unreachable daemon reads differently from "nothing fetched yet".
    audit_fetch_error: Option<String>,
    /// `true` when the most recently *completed* `/audit` fetch attempt
    /// resolved `AuditFetchOutcome::NoBoardService` — i.e.
    /// `resolve_board_service()` found neither `COORD_SERVICE_URL` nor a
    /// usable `board_service` in `~/.coord/client.toml`. Distinct from
    /// `audit_fetch_error` (a genuine network/parse failure) and from the
    /// pre-first-fetch `None` state: surfaced as a targeted hint in the
    /// empty-state message (§4b) so "board service isn't configured on
    /// this box" is immediately visible instead of looking identical to a
    /// live daemon that simply has zero audit rows (#1039 review fix —
    /// a live-daemon smoke test that always showed the bare empty state
    /// with no diagnostic could not be told apart from this case without
    /// live instrumentation; this makes the distinction visible in the UI
    /// itself, no debug build required).
    audit_no_service: bool,
    /// #1040: current time-range filter (contract §8), cycled by `t`.
    /// Persists across panel navigation (contract §11); resets to `All` on
    /// TUI restart (in-memory only).
    audit_time_range: AuditTimeRange,
    /// #1040: current category filter (contract §9), cycled by `Tab`.
    /// Persists across panel navigation; resets to `All` on restart.
    audit_category: AuditCategory,
    /// #2653: current tier filter, cycled by `T`. Defaults to `Business` —
    /// unlike `audit_time_range`/`audit_category` above, whose defaults are
    /// "no filter" — because the un-filtered `/audit` stream is ~96%
    /// `forge_availability` operational telemetry (#1896 Phase 0) that
    /// drowns real board transitions; see `AuditTier`'s doc comment.
    /// Persists across panel navigation; resets to `Business` on restart.
    audit_tier: AuditTier,
    /// #1040: free-text filter on the `/audit` `type` (`event_type`) param
    /// — reuses the same `SidebarFilter` widget Board/Pipeline embed for
    /// their issue filters (deliverable 1). Toggled into edit mode with
    /// `f`; `Enter` commits the typed value and re-arms the fetch; `Esc`
    /// while focused clears it back to empty (mirrors `board_search`'s
    /// Escape-while-focused semantics — see `events.rs`).
    audit_type_filter: SidebarFilter,
    /// #1094: per-column width overrides from a user's header-divider drag on
    /// the Audit `DataTable`. Always length-5 (matching `audit_columns()`'s
    /// column count) or empty; a `Some(w)` entry replaces that column's
    /// `ColumnWidth` strategy with `Fixed(w)` for the rest of the session.
    /// Session-only persistence — matches how filters/scroll position
    /// already work (no cross-restart UI-state store exists in this
    /// codebase yet).
    audit_column_overrides: Vec<Option<f32>>,
    /// #1094: the most recently rendered Audit `DataTable`'s resolved
    /// layout, cached by `render_audit_panel` so mouse hit-testing
    /// (`audit_table_hit`/`audit_update_resize_drag`) can reuse the exact
    /// column geometry that was painted, without a `Backend` handle at
    /// click time. Mirrors the `kanban_layout` render-then-hit-test pattern
    /// (`RefCell` because `render_audit_panel` takes `&self`). `None` until
    /// the panel has rendered at least once with rows present.
    audit_table_layout: std::cell::RefCell<Option<DataTableLayout>>,
    /// #1094: index of the column being resized (the column to the LEFT of
    /// the divider being dragged), set by a `MouseDown` on a
    /// `DataTableHit::HeaderDivider` and cleared on `MouseUp`. `None` when no
    /// resize drag is in progress.
    audit_resize_col: Option<usize>,
    /// #1094 fix (fix-iteration-1): vertical scroll offset (index of the
    /// first visible row) for the Audit `DataTable`. Was hardcoded to `0` in
    /// the initial #1094 landing — keyboard nav (`j`/`k`/`Home`/`End`) could
    /// move `audit_sel` off-screen with no way to bring it back into view,
    /// since `DataTableLayout::hit_test`/`draw_data_table` only page from
    /// whatever `scroll_offset` the table is built with. Kept in sync with
    /// `audit_sel` by `fix_audit_scroll` (events.rs nav sites) and settable
    /// directly by a vertical-scrollbar track click/drag
    /// (`audit_apply_vscroll`). Session-only, same persistence tier as
    /// `audit_column_overrides`.
    audit_scroll: usize,
    /// #1094 fix: horizontal scroll offset (surface cells) for the same
    /// table — only does anything once `min_total_width` forces the
    /// h-scrollbar to appear (narrow terminal). Was also hardcoded to `0.0`
    /// originally, so the h-scrollbar rendered but could never actually move
    /// the viewport. Set by `audit_apply_hscroll`.
    audit_h_scroll: f32,
    /// #1094 fix: which scrollbar (if any) is being drag-scrolled, set by a
    /// `MouseDown` that lands in a scrollbar track (`audit_scrollbar_hit`)
    /// and cleared on `MouseUp`. Mutually exclusive with `audit_resize_col`
    /// — a single click starts at most one kind of drag.
    audit_scrollbar_drag: Option<AuditScrollAxis>,

    // ── #1741: Reports ActivityBar panel ─────────────────────────────────────
    //
    // Everything here is keyed by the *catalogue's* report ids and param ids
    // — no field names a specific report. See `app/reports.rs`.
    /// Report catalogue from `GET /report` (#1742). `None` until the first
    /// fetch completes, which the panel renders as "Loading reports…";
    /// `Some(vec![])` means the daemon answered with an empty catalogue,
    /// which is a different (and separately diagnosable) state.
    reports_catalogue: Option<Vec<ReportDef>>,
    /// In-flight catalogue fetch, drained by the same view-gated poll loop
    /// that drains `audit_fetch_rx` (`settings_ui.rs`).
    reports_catalogue_rx: Option<std::sync::mpsc::Receiver<ReportsCatalogueOutcome>>,
    /// Whether a catalogue fetch has already been attempted this session.
    /// The catalogue is static metadata, so unlike `/audit` it is fetched
    /// once rather than on a TTL; `r` with no prior run re-arms it.
    reports_catalogue_fetched: bool,
    /// Operator-edited parameter values: report id → param id → value.
    /// Anything absent falls back to the catalogue's `default`, so this map
    /// only ever holds deliberate edits.
    reports_params: std::collections::HashMap<String, std::collections::HashMap<String, String>>,
    /// Which sections are currently expanded (by report id).
    reports_expanded: std::collections::HashSet<String>,
    /// Report ids whose section the operator has explicitly collapsed or
    /// expanded. Sections default to expanded, so `reports_seed_expansion`
    /// needs to know which ones not to re-open behind the operator's back
    /// when a fresh catalogue lands.
    reports_touched: std::collections::HashSet<String>,
    /// Id of the report whose run is currently in flight, or `None`.
    reports_running: Option<String>,
    /// In-flight run, delivering a `ReportResult` (or a failure).
    reports_run_rx: Option<std::sync::mpsc::Receiver<ReportRunOutcome>>,
    /// The last completed run's result. Cleared when a new run starts, so a
    /// stale table can never sit under a fresh run or a fresh failure.
    reports_result: Option<ReportResult>,
    /// Why the last catalogue fetch or run failed, surfaced in the result
    /// area instead of leaving the old result on screen.
    reports_error: Option<String>,
    /// `true` when the last completed fetch resolved `NoBoardService` —
    /// same diagnosability rationale as `audit_no_service`.
    reports_no_service: bool,
    /// Selected section index (0-based) into the catalogue. Clamped on
    /// navigation, same pattern as `audit_sel`.
    reports_sel: usize,
    /// Focused field index within the selected section's form (params, then
    /// the trailing Run button). Clamped by `reports_clamp_field_sel`.
    reports_field_sel: usize,
    /// `true` while typing edits the focused free-text param rather than
    /// hitting the panel's own single-key bindings (`r`, `j`/`k`, …).
    /// Entered by clicking a text field or pressing Enter on it; left by
    /// Enter/Esc. Mirrors `audit_type_filter.focused`'s role.
    reports_text_editing: bool,
    /// Vertical scroll offset into the result `DataTable` (wheel-driven —
    /// the keyboard's `j`/`k` belong to section navigation here).
    reports_result_scroll: usize,
    /// The most recently painted section-stack geometry (plus each form
    /// body's own), cached by `render_reports_panel` so `events.rs` can
    /// hit-test without a `Backend` handle. See `app/msv.rs`.
    reports_layout: std::cell::RefCell<MsvLayoutCache>,
    /// #1762: which result column is sorted and in which direction, or
    /// `None` for the daemon's own row order. Toggled by a header click
    /// (`reports_sort_by_column`) and cleared by every run — view state
    /// over one result set, never persisted.
    reports_sort: Option<(usize, SortDirection)>,
    /// #1762: the most recently painted result-`DataTable` geometry **and
    /// the rect it was painted into**, cached so `events.rs` can route a
    /// header click without a `Backend` handle. The rect is part of the
    /// cache because — unlike `audit_table_layout` — this table does not
    /// start at the main panel's origin; the section stack sits above it.
    /// `None` whenever no table is on screen.
    reports_table_layout: std::cell::RefCell<Option<(Rect, DataTableLayout)>>,
    /// #1853: per-column width overrides from a user's header-divider drag
    /// on the result `DataTable`, **paired with the column set they were
    /// measured against** — `(report_id, column ids)`.
    ///
    /// The key is the whole point, and the one structural difference from
    /// Audit's `audit_column_overrides`. Audit has five columns forever, so
    /// a bare `Vec<Option<f32>>` can never mean the wrong thing there. A
    /// report's columns come from the daemon and differ per report, so an
    /// unkeyed override would silently re-apply column 2's dragged width to
    /// a *different* column 2 the moment another report's result landed —
    /// mis-sizing a column the user never touched. Keying by report id
    /// **and** column ids is what makes a stale override unrepresentable;
    /// a length check alone would not, since two reports can have the same
    /// column count. Read back through `reports_active_overrides`, which
    /// returns "no overrides" whenever the key doesn't match the result
    /// being rendered.
    ///
    /// Session-only, exactly like `audit_column_overrides` and like the
    /// panel's sort: nothing here is written to settings.
    reports_column_overrides: Option<(ReportsColumnKey, Vec<Option<f32>>)>,
    /// #1853: index of the result-table column being resized (the column to
    /// the LEFT of the dragged divider), set by a `MouseDown` on a
    /// `DataTableHit::HeaderDivider` and cleared on `MouseUp`. `None` when
    /// no resize drag is in progress. Mirrors `audit_resize_col`.
    reports_resize_col: Option<usize>,
    /// #1910: whether a `MouseDown` landed on the result table's vertical
    /// scrollbar track and a drag is in progress, so subsequent `MouseMoved`
    /// events should keep scrubbing `reports_result_scroll` instead of doing
    /// anything else. Cleared on `MouseUp`. There is no horizontal
    /// counterpart — the table's `h_scroll` is pinned at `0.0` (see
    /// `render_reports_result`), so no horizontal scrollbar can ever be on
    /// screen to drag. Mirrors `audit_scrollbar_drag`, minus the axis enum
    /// since only one axis exists here.
    reports_vscroll_drag: bool,
    /// #1765: the report id whose section-header Export action was just
    /// clicked, awaiting the save dialog.
    ///
    /// Mouse routing (`mouse_main_click`) has no `Backend` handle, and
    /// `PlatformServices::show_file_save_dialog` needs one — so the click
    /// parks the request here and `dispatch_handle` (which does have the
    /// backend) drains it in the same turn. Same shape as
    /// `pending_panel_switch`: a one-shot request, never a mode.
    reports_pending_export: Option<String>,
    /// #1765: the outcome of the last Export — the destination it was
    /// written to, that it was cancelled, or why it failed. Rendered in the
    /// notes area, because a silent write is indistinguishable from a
    /// silent failure.
    reports_export_status: Option<String>,
    /// #1765: in-flight CSV export (fetch `?format=csv` + write), drained by
    /// the same view-gated poll loop that drains `reports_run_rx`. A thread
    /// rather than an inline blocking fetch: a report run can take seconds
    /// server-side, and freezing the UI for it would be worse than the
    /// spinner-less wait.
    reports_export_rx: Option<std::sync::mpsc::Receiver<ReportExportOutcome>>,

    // ── #541: global Telescope-style issue fuzzy finder ──────────────────────
    /// Active state of the issue fuzzy-finder overlay.  `None` when the
    /// overlay is closed.  Opened with Ctrl+P from any non-PTY view; closed
    /// with Esc or Enter (Enter also navigates to the selected issue).
    issue_finder: Option<IssueFinder>,

    // ── #2642: Ctrl+E doc-tab open-tabs picker ────────────────────────────
    /// Active state of the doc-tab quick-pick. `None` when closed. Opened
    /// with Ctrl+E from the Board or Pipeline view; closed with Esc or Enter
    /// (Enter also activates the selected tab).
    doc_tab_picker: Option<DocTabPickerState>,

    // ── #217 Theming ─────────────────────────────────────────────────────────
    /// Resolved colour palette, derived from `settings.theme` (and an optional
    /// `~/.coord/theme.toml` override file) at startup and after each settings
    /// change.  Passed into theme-sensitive rendering helpers such as
    /// `Assignment::status_color_themed` and `stage_badge`.
    active_theme: quadraui::Theme,

    // ── #2405: completed-issues grid ─────────────────────────────────────────
    /// Controls + selection behind the Pipeline panel's `Completed` tab —
    /// the report-style grid that replaced #728's fixed-window Done section.
    /// In-memory only; resets to its 24h default on restart.
    pub(crate) completed_grid: CompletedGrid,
    /// The completed grid's last-painted control-row geometry, with the rect
    /// it was painted into.  Same render-then-hit-test contract as
    /// `msv::MsvLayoutCache` — and for the same reason: `FormController`'s
    /// backend-free `handle_cached` re-derives char width as `lh * 0.6`,
    /// which is wrong on a cell-granular TUI surface and mis-routes clicks
    /// inside a segmented control.  `FormLayout` bounds are **form-local**
    /// (origin `(0, 0)`), so hit-testing subtracts the stored rect.
    pub(crate) completed_form_layout: std::cell::RefCell<Option<(Rect, FormLayout)>>,
    /// The completed grid's last-painted `DataTable` geometry, with the rect
    /// it was painted into.  Same render-then-hit-test pattern (and same
    /// reason for carrying the rect) as `reports_table_layout`: the table does
    /// not start at the main panel's origin, so a bare `pos - main_b` would
    /// mis-hit-test by the control row's height.
    pub(crate) completed_table_layout: std::cell::RefCell<Option<(Rect, DataTableLayout)>>,

    // ── #816: PTY-panic modal ─────────────────────────────────────────────────
    /// When `Some`, a dismissible modal dialog is shown explaining that a vt100
    /// parser panic evicted an active terminal session.  The string is the raw
    /// panic message captured by `catch_unwind` and converted via
    /// [`vt100_panic_to_string`].  Set by [`report_terminal_panic`]; cleared
    /// when the operator dismisses the dialog (Esc / Enter / outside-click).
    pty_panic_dialog: Option<String>,

    // ── #1059: Gate A dispatch-failure modal ──────────────────────────────────
    /// When `Some`, a dismissible modal dialog shows the COMPLETE reason a
    /// "Dispatch Gate A mock" (`coord acceptance mock`) attempt failed.
    ///
    /// The generic command-failed toast is a single un-wrapped ~40-col line
    /// (quadraui#771), so a long Gate A reason — e.g. "repo 'x' has no
    /// acceptance driver configured — add it under acceptance.drivers in
    /// coordinator.yml before running Gate A (docs/ORACLE_LOOP.md)" — is cut
    /// off mid-sentence with nowhere to read the rest.  The operator read that
    /// truncation as a *permanent wedge* (#1041 "PERMANENTLY STUCK") when it
    /// was actually a clean pre-dispatch failure that claimed nothing.  This
    /// modal word-wraps the full message and states explicitly that nothing
    /// was left in-flight, so a config gap can never masquerade as a stuck
    /// epic again.  Set by the poll handler in `settings_ui.rs`; cleared on
    /// dismiss (Esc / Enter / outside-click).
    gate_a_error_dialog: Option<String>,
    /// #2863: full body of the "Decomposition session dispatch failed"
    /// modal, or `None` when it isn't showing.  Same problem and same
    /// solution as `gate_a_error_dialog` directly above: a failed `coord
    /// portal decompose-chat` is most often the multi-sentence thin-client
    /// refusal, which names its own fix and is unreadable clipped to the
    /// ~40-col toast box.  Kept as its OWN field rather than folded into
    /// `gate_a_error_dialog` so each modal keeps its own accurate title —
    /// they only ever share dismissal plumbing, never content.  Set by the
    /// poll handler in `settings_ui.rs` via
    /// [`CoordApp::fail_pending_decomposition_chat`]; cleared on dismiss
    /// (Esc / Enter / outside-click).
    decompose_chat_error_dialog: Option<String>,
}

/// #1094 fix: which axis of the Audit `DataTable`'s scrollbars a
/// `MouseDown`/drag is currently operating on. `quadraui`'s
/// `DataTableLayout::hit_test` has no concept of the scrollbar strips it
/// reserves space for (see the #1094 fix-iteration-1 durable finding), so
/// coord-tui hit-tests them itself (`audit_scrollbar_hit`) and tracks the
/// in-progress drag here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AuditScrollAxis {
    Vertical,
    Horizontal,
}

/// #2043: which axis of the Queue grid's scrollbars a `MouseDown`/drag is
/// currently operating on. Same shape and reason as `AuditScrollAxis` —
/// `DataTableLayout::hit_test` has no concept of either scrollbar strip, so
/// `queue_scrollbar_hit` hit-tests them itself and needs to report which one
/// so the caller (`mouse_main_click`) arms the matching drag flag
/// (`queue_vscroll_drag` / `queue_hscroll_drag`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum QueueScrollAxis {
    Vertical,
    Horizontal,
}

impl Default for CoordApp {
    fn default() -> Self {
        Self::new()
    }
}

/// Parse all keybinding strings from a `TuiSettings` into `ParsedBinding`s.
/// Unrecognised or unparseable entries are silently skipped.
fn parse_keybindings(settings: &TuiSettings) -> Vec<(String, ParsedBinding)> {
    settings
        .keybindings
        .iter()
        .filter_map(|(action, key_str)| {
            if key_str.is_empty() {
                return None;
            }
            let parsed = parse_key_binding(key_str)?;
            Some((action.clone(), parsed))
        })
        .collect()
}

/// Normalise a `Key` to the string format used by `ParsedBinding::key`:
/// single chars are lowercased; named keys use their display name.
fn key_to_binding_str(key: &Key) -> String {
    use quadraui::NamedKey;
    match key {
        Key::Char(c) => c.to_ascii_lowercase().to_string(),
        Key::Named(n) => match n {
            NamedKey::Escape => "Escape".to_string(),
            NamedKey::Enter => "Enter".to_string(),
            NamedKey::Tab => "Tab".to_string(),
            NamedKey::Backspace => "Backspace".to_string(),
            NamedKey::Delete => "Delete".to_string(),
            NamedKey::Home => "Home".to_string(),
            NamedKey::End => "End".to_string(),
            NamedKey::PageUp => "PageUp".to_string(),
            NamedKey::PageDown => "PageDown".to_string(),
            NamedKey::Up => "Up".to_string(),
            NamedKey::Down => "Down".to_string(),
            NamedKey::Left => "Left".to_string(),
            NamedKey::Right => "Right".to_string(),
            NamedKey::F(n) => format!("F{n}"),
            _ => String::new(),
        },
    }
}

/// #638: Derive per-stage badge statuses for a Kanban card from its IssueGroup.
///
/// Returns a compact badge row: Plan → Work → Test → Review → Merge.
/// Status is inferred from the group's status_summary and assignments.
///
/// quadraui#476 genericized the Board primitive: the old fixed `Stage` enum
/// (which rendered as the single chars `P W T R M`) is gone, and badge
/// vocabulary is now host-supplied via [`CardBadge`]. The single-char labels
/// below preserve the previous on-screen rendering exactly.
fn kanban_stage_badges(g: &IssueGroup) -> Vec<CardBadge> {
    // Determine the dominant assignment status.
    let has_running = g.assignments.iter().any(|a| a.status == "running");
    let has_failed = g.assignments.iter().any(|a| a.status == "failed");
    let status = g.status_summary.as_str();

    // Work stage status.
    let work_status = if has_running {
        BadgeStatus::Running
    } else if has_failed {
        BadgeStatus::Blocked
    } else if matches!(status, "done" | "merged") {
        BadgeStatus::Passed
    } else {
        BadgeStatus::Pending
    };

    // Review stage: check review_verdict on any assignment.
    let review_status = {
        let verdict = g.assignments.iter()
            .filter_map(|a| a.review_verdict.as_deref())
            .last();
        match verdict {
            Some("approve") => BadgeStatus::Passed,
            // quadraui#476 renamed `RequestChanges` -> the generic `Warning`
            // ("needs attention before it can proceed"), same ↩ glyph + tint.
            Some("request-changes") => BadgeStatus::Warning,
            Some(_) => BadgeStatus::Running,
            None => BadgeStatus::Pending,
        }
    };

    // Merge stage: passed when status is "merged".
    let merge_status = if status == "merged" {
        BadgeStatus::Passed
    } else {
        BadgeStatus::Pending
    };

    vec![
        CardBadge { label: "W".into(), status: work_status },
        CardBadge { label: "R".into(), status: review_status },
        CardBadge { label: "M".into(), status: merge_status },
    ]
}

impl CoordApp {
    ///
    /// Board data is fetched on a background thread so the UI renders
    /// immediately.  The status bar shows "↻ loading…" until the first
    /// load completes.
    pub fn new() -> Self {
        let mut sidebar = SidebarSystem::new(Vec::new());
        sidebar.set_navigation_mode(NavigationMode::Selection);
        sidebar.set_allow_collapse(true);
        let mut pipeline_sidebar = SidebarSystem::new(Vec::new());
        pipeline_sidebar.set_navigation_mode(NavigationMode::Selection);
        pipeline_sidebar.set_allow_collapse(true);
        let (inject_fallback_tx, inject_fallback_rx) = std::sync::mpsc::channel();
        let mut app = Self {
            data: BoardData::default(),
            // #1326: reload whatever was persisted last session; reconciled
            // against real board data by `sync_workspace_repos()` once the
            // first `apply_pending_data` tick lands (see there for why an
            // empty-data tick must not stomp this).
            workspace: Workspace::load(),
            // #2286 (ms-65 §6): same story as `workspace` above — reload
            // whatever was persisted, unpruned, until `sync_doc_tabs()`
            // (called alongside `sync_workspace_repos()` from
            // `apply_pending_data`) reconciles it against the first real
            // board data.
            active_view: SidebarView::default(),
            board_sidebar: sidebar,
            board_repo_names: Vec::new(),
            board_issues_cache: Vec::new(),
            has_proposals_section: false,
            machine_sel: 0,
            machine_scroll: 0,
            // Use a far-past instant so the "↻ Xs" counter starts at 0.
            refreshed_at: Instant::now(),
            detail_scroll: 0,
            machine_detail_scroll: 0,
            command_runner: crate::commands::CommandRunner::new(),
            last_notify: Instant::now(),
            issue_sync_last: None,
            board_search: SidebarFilter::default(),
            board_milestone_expanded: std::collections::HashMap::new(),
            board_epic_expanded: std::collections::HashMap::new(),
            board_epic_row_keys: std::collections::HashMap::new(),
            doc_tabs: DocTabs::load(),
            render_pane: std::cell::Cell::new(None),
            board_split_drag: false,
            board_split_layout: std::cell::RefCell::new(None),
            board_pane_chord: None,
            board_section_rows: Vec::new(),
            board_tree_hidden_above: std::collections::HashMap::new(),
            last_sidebar_geom: std::cell::Cell::new(None),
            pipeline_sidebar,
            pipeline_repo_names: Vec::new(),
            pipeline_state_section_names: Vec::new(),
            pipeline_search: SidebarFilter::default(),
            pipeline_lifecycle_expanded: std::collections::HashMap::new(),
            pipeline_milestone_expanded: std::collections::HashMap::new(),
            pipeline_epic_expanded: std::collections::HashMap::new(),
            pipeline_epic_row_keys: std::collections::HashMap::new(),
            pipeline_section_rows: Vec::new(),
            last_pipeline_sidebar_geom: std::cell::Cell::new(None),
            pipeline_issues: Vec::new(),
            pipeline_sel: None,
            pipeline_status: None,
            toasts: Vec::new(),
            next_toast_id: 0,
            watch_pool: std::collections::HashMap::new(),
            watch_focused: None,
            inject_opened_from_log_tab: false,
            inject_chat: None,
            inject_spinner_frame: 0,
            pipeline_detail_tab: PipelineDetailTab::default(),
            board_detail_tab: BoardDetailTab::default(),
            pipeline_detail_scroll: 0,
            pipeline_log_hscroll: 0,
            pipeline_log_pinned_assignment: std::collections::HashMap::new(),
            remote_log_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_data: Some(start_data_load()),
            fetch_error: None,
            pending_log_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
            pending_issue_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
            fetched_issues_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            // #876: pending_comments_fetches and fetched_comments_cache removed.
            pending_purge: None,
            pending_test_fail: None,
            pending_report_fix: None,
            pending_plan_capture: None,
            pending_milestone_row_input: None,
            pending_close_plan: None,
            pending_new_milestone_chat: None,
            pending_refinement: None,
            pending_test_chat: None,
            pending_chat_resume: None,
            inject_fallback_tx,
            inject_fallback_rx,
            pending_refine_ready: None,
            refine_then_dispatch: false,
            chat_transcript_cache_key: None,
            chat_last_activity: None,
            chat_spinner_throttle: 0,
            paused_machines: read_paused_machines(),
            quiet_paused_machines: std::collections::HashSet::new(),
            cordoned_machines: std::collections::HashSet::new(),
            quiet_hours_windows: std::collections::HashMap::new(),
            pending_paused_machines: Some(spawn_paused_machines_fetch()),
            pending_force_merge: None,
            pending_merge_all_ready: None,
            pending_merge_revalidate: None,
            pending_context_menu: None,
            pending_clipboard_copy: None,
            context_menu_layout: std::cell::RefCell::new(Vec::new()),
            fleet_health_overlay_open: false,
            pending_drive_queue_after: None,
            pending_gate_a_changes_note: None,
            approved_sel: 0,
            approved_scroll: 0,
            approved_detail_open: false,
            queue_sel: 0,
            queue_scroll: 0,
            queue_sort: None,
            queue_table_layout: std::cell::RefCell::new(None),
            queue_detail_scroll: 0,
            last_queue_detail_cols: std::cell::Cell::new(120),
            last_queue_detail_visible_rows: std::cell::Cell::new(10),
            last_queue_detail_item_count: std::cell::Cell::new(0),
            // #2017: same 40% default `render_queue_panel` hardcoded before
            // the splitter existed — the floor clamps still apply on top of
            // this on every paint, so a pathologically small starting value
            // here couldn't collapse either pane anyway.
            queue_split_frac: 0.4,
            queue_split_drag: false,
            queue_separator_rect: std::cell::Cell::new(None),
            queue_vscroll_drag: false,
            queue_h_scroll: std::cell::Cell::new(0.0),
            queue_hscroll_drag: false,
            queue_detail_scrollbar: std::cell::RefCell::new(None),
            queue_detail_vscroll_drag: false,
            dialog_layout: std::cell::RefCell::new(None),
            pending_restart: None,
            machine_last_contact: std::collections::HashMap::new(),
            local_coord_version: fetch_local_coord_version(),
            last_main_visible_rows: std::cell::Cell::new(40),
            last_log_panel_cols: std::cell::Cell::new(120),
            last_issue_panel_cols: std::cell::Cell::new(120),
            board_pane_issue_cols: std::cell::RefCell::new(Vec::new()),
            purge_days: 7,
            sidebar_action_bar_hover: ToolbarHoverTracker::new(),
            panel_toolbar_hover: ToolbarHoverTracker::new(),
            pipeline_action_bar_hover: ToolbarHoverTracker::new(),
            pipeline_focused_stage: None,
            pipeline_stage_content_scroll: 0,
            settings: TuiSettings::load(),
            parsed_keybindings: parse_keybindings(&TuiSettings::load()),
            settings_form: std::cell::RefCell::new(FormController::new("settings".to_string())),
            settings_field_sel: 0,
            audio_prev_running: std::collections::HashSet::new(),
            test_build_jobs: std::collections::HashMap::new(),
            last_test_builds: std::collections::HashMap::new(),
            pending_pr_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
            fetched_prs_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
            pipeline_ci_checks: std::collections::HashMap::new(),
            pipeline_dismissed: std::collections::HashSet::new(),
            pipeline_inflight_merges: std::collections::HashSet::new(),
            pending_refinement_notes_synth: None,
            refinement_notes_modal: None,
            refinement_notes_post_rx: None,
            pending_refinement_close_prompt: None,
            finalise_after_notes_post: false,
            pending_board_chat: None,
            pending_milestone_chat: None,
            pending_decomposition_chat: None,
            pending_repo_picker: None,
            pending_machine_picker: None,
            pending_new_terminal_picker: None,
            pending_new_terminal: None,
            pending_quiet_hours: None,
            pending_diagnose_dialog: None,
            pending_diagnose_legacy_retry: None,
            pending_quit_confirm: false,
            quit_requested: false,
            file_issue_modal: None,
            file_issue_post_rx: None,
            artifact_cache: std::collections::HashMap::new(),
            artifact_fetch_rx: None,
            findings_detail_cache: std::collections::HashMap::new(),
            findings_fetch_rx: None,
            issue_detail_cache: std::collections::HashMap::new(),
            issue_fetch_rx: None,
            pending_artifact_pull: None,
            last_artifact_pulls: std::collections::HashMap::new(),
            artifact_pull_dialog: None,
            log_items_cache: std::cell::RefCell::new(None),
            redraw_pending: false,
            last_redraw_at: Instant::now(),
            test_plan_pending: std::collections::HashSet::new(),
            test_plan_staleness_checked_for: None,
            test_step_jobs: std::collections::HashMap::new(),
            test_step_results: std::collections::HashMap::new(),
            test_step_output: std::collections::HashMap::new(),
            // #424: terminal pane is lazily spawned the first time the
            // user opens the Terminal view (see `tick`).  Start with no
            // session, focus enabled-by-default once the view is opened.
            terminal_session: None,
            terminal_focused: false,
            terminal_pending_dims: std::cell::Cell::new(None),
            terminal_spawn_error: None,
            // #1029: no queued programmatic panel switch / Terminal
            // return-view bookmark on startup.
            pending_panel_switch: None,
            pending_switch_is_programmatic: false,
            terminal_return_view: None,
            // #440: per-issue detail-view terminals.
            detail_terminal_sessions: std::collections::HashMap::new(),
            detail_terminal_spawn_errors: std::collections::HashMap::new(),
            detail_terminal_focused: false,
            ctrl_w_pending: false,
            focused_region: FocusedRegion::default(),
            detail_terminal_pending_dims: std::cell::Cell::new(None),
            // #454: tracks Press → Release for PTY mouse-reporting drags.
            pty_pressed_buttons: 0,
            // #464: host-side terminal selection drag state.
            terminal_host_sel_dragging: false,
            // #790: F9 keyboard-toggled terminal copy mode.
            terminal_copy_mode: false,
            // #207: machine metrics sparklines.
            machine_metrics: std::collections::HashMap::new(),
            pending_metrics: Vec::new(),
            metrics_last_polled: Instant::now(),
            // #487: live tmux session discovery.
            live_tmux_sessions: fetch_live_tmux_sessions(),
            pending_remote_sessions: Some(spawn_remote_tmux_sessions_fetch(
                crate::commands::find_config(),
            )),
            // #953: fleet terminal discovery — local snapshot now, remote
            // sweep in the background (mirrors #487's session discovery).
            fleet_terminals: fetch_fleet_terminals(),
            pending_remote_terminals: Some(spawn_remote_fleet_terminals_fetch(
                crate::commands::find_config(),
            )),
            terminal_tree_expanded: std::collections::HashMap::new(),
            terminal_tree_selected: None,
            terminal_tree_scroll: 0,
            // #955: attached fleet-terminal PTYs — empty until a tree leaf
            // is selected and `drive_terminal_pane` lazily attaches it.
            fleet_terminal_sessions: std::collections::HashMap::new(),
            fleet_terminal_spawn_errors: std::collections::HashMap::new(),
            pending_kill_terminal: None,
            // #1398: drive-session discovery — local snapshot now, background
            // sweep re-armed on the same cadence as sessions/terminals.
            drive_sessions: fetch_drive_sessions(),
            pending_drive_sessions: Some(spawn_drive_sessions_fetch()),
            pending_kill_drive: None,
            // #1032: Sessions panel tree — no persisted expand/selection
            // state, mirrors terminal_tree_*'s defaults.
            sessions_tree_expanded: std::collections::HashMap::new(),
            sessions_tree_selected: None,
            sessions_tree_scroll: 0,
            // #1033: pending "Kill session" confirmation — closed by default.
            pending_kill_session: None,
            // #603: fix-briefing preview (lazily populated when a dialog raises).
            fix_briefing_preview: None,
            fix_briefing_rx: None,
            // Leg 2 (#517): auto-advance Work → Review.
            armed_for_auto_review: std::collections::HashMap::new(),
            pending_auto_review: None,
            pending_stage_launch: None,
            // #685: per-issue test-mode policy choice dialog.
            pending_test_mode_choice: None,
            offered_smoke_for_headless_work: std::collections::HashSet::new(),
            // Leg 3 (#517): verdict-driven routing.
            armed_for_verdict: std::collections::HashMap::new(),
            pending_rework: None,
            rework_bypass: false,
            // #541: global issue fuzzy finder — closed by default.
            issue_finder: None,
            // #2642: doc-tab picker — closed by default.
            doc_tab_picker: None,
            // Leg 3c / A3 (#517, #581): test-verdict routing.
            armed_for_test_verdict: std::collections::HashMap::new(),
            pending_test_fix: None,
            pending_merge: None,
            // #863: force-past-iteration-cap preflight + confirm.
            pending_fix_cap_preflight: None,
            pending_fix_force_confirm: None,
            // #638: Kanban view — empty until rebuild_board_sidebar populates it.
            kanban_model: BoardModel {
                id: WidgetId::new("kanban:coord"),
                columns: Vec::new(),
                selected_card_id: None,
                col_scroll_offset: 0,
            },
            kanban_layout: std::cell::RefCell::new(None),
            // #737: Merge Queue panel — selection and scroll start at the top.
            merge_queue_sel: 0,
            merge_queue_scroll: 0,
            // #771: Milestone DAG panel — selection starts at the top.
            milestone_dag_sel: 0,
            // #1124: reusable help layer — registered once at startup;
            // see `plans.rs::plans_view_help`.
            help_registry: {
                let mut registry = HelpRegistry::new();
                registry.register("panel:plans", CoordApp::plans_view_help());
                // #2287 (ms-65 §8b): Board panel `?` cheatsheet.
                registry.register("panel:board", CoordApp::board_view_help());
                registry
            },
            help_overlay: HelpOverlayController::new(),
            command_palette: None,
            // #975: Plans panel — selection starts at the top.
            plans_sel: 0,
            // #1001: no repo starts expanded — untracked milestones default
            // to collapsed everywhere.
            plans_expanded_repos: std::collections::HashSet::new(),
            // #1122: starts on the roster list, not the detail pane.
            plans_detail_open: false,
            // #1122 fix-iteration-1: no scroll/selection/target until the
            // pane is actually opened.
            plans_detail_scroll: 0,
            plans_detail_sel: 0,
            plans_detail_target: None,
            // #1121: sidebar tree — nothing scoped yet ("All repos"), no
            // explicit expand overrides (falls back to collapsed).
            plans_tree_expanded: std::collections::HashMap::new(),
            plans_tree_selected: None,
            plans_tree_scroll: 0,
            // #1039: Audit panel — nothing fetched yet; the first tick with
            // active_view == Audit arms spawn_audit_fetch (settings_ui.rs).
            audit_page: None,
            audit_fetch_rx: None,
            audit_last_fetched: None,
            audit_sel: 0,
            audit_detail_open: false,
            audit_fetch_error: None,
            audit_no_service: false,
            // #1040: no filter applied on startup — matches contract §8/§9
            // defaults ("All" time-range, "all" category, empty type text).
            audit_time_range: AuditTimeRange::All,
            audit_category: AuditCategory::All,
            // #2653: tier defaults to `Business` (not "no filter") — see
            // `AuditTier`'s doc comment.
            audit_tier: AuditTier::default(),
            audit_type_filter: SidebarFilter::default(),
            // #1094: no column-width overrides / active resize drag / cached
            // layout on startup — all populated by the first render + user
            // drag, same lifecycle as the fields above.
            audit_column_overrides: vec![None; 5],
            audit_table_layout: std::cell::RefCell::new(None),
            audit_resize_col: None,
            audit_scroll: 0,
            audit_h_scroll: 0.0,
            audit_scrollbar_drag: None,
            // #1116: Usage panel — defaults to today/by-issue/cost-total-desc,
            // nothing expanded, no custom-range dialog open.
            // #1741: Reports panel — nothing fetched yet; the first tick with
            // active_view == Reports arms the catalogue fetch (settings_ui.rs).
            reports_catalogue: None,
            reports_catalogue_rx: None,
            reports_catalogue_fetched: false,
            reports_params: std::collections::HashMap::new(),
            reports_expanded: std::collections::HashSet::new(),
            reports_touched: std::collections::HashSet::new(),
            reports_running: None,
            reports_run_rx: None,
            reports_result: None,
            reports_error: None,
            reports_no_service: false,
            reports_sel: 0,
            reports_field_sel: 0,
            reports_text_editing: false,
            reports_result_scroll: 0,
            reports_layout: std::cell::RefCell::new(MsvLayoutCache::default()),
            reports_sort: None,
            reports_table_layout: std::cell::RefCell::new(None),
            reports_column_overrides: None,
            reports_resize_col: None,
            reports_vscroll_drag: false,
            reports_pending_export: None,
            reports_export_status: None,
            reports_export_rx: None,
            // #217: resolved theme palette — computed from settings + optional
            // ~/.coord/theme.toml override file.
            active_theme: {
                let s = TuiSettings::load();
                crate::settings::TuiSettings::load_custom_theme_file()
                    .unwrap_or_else(|| s.theme.to_quadraui_theme())
            },
            // #2405: completed-issues grid defaults (24h / all repos).
            completed_grid: CompletedGrid::default(),
            completed_form_layout: std::cell::RefCell::new(None),
            completed_table_layout: std::cell::RefCell::new(None),
            // #816: no pending PTY-panic dialog on startup.
            pty_panic_dialog: None,
            // #1059: no pending Gate A dispatch-failure dialog on startup.
            gate_a_error_dialog: None,
            // #2863: no pending decomposition-dispatch failure on startup.
            decompose_chat_error_dialog: None,
        };
        // #584: a thin client pulls config from the daemon (no local
        // coordinator.yml) so the status bar doesn't warn and subcommands have
        // a config path.  Best-effort: leave config_path None if the fetch fails.
        if is_remote_board_service() {
            if let Some(cfg) = fetch_remote_config_to_cache() {
                app.command_runner.config_path = Some(cfg);
            }
        }
        app.rebuild_board_sidebar();
        app.rebuild_pipeline_sidebar(None);
        // Sync issues from GitHub on startup so the board backlog is fresh.
        app.kick_issue_sync();
        // #487: notify the operator about any live sessions from a previous run.
        if !app.live_tmux_sessions.is_empty() {
            let n = app.live_tmux_sessions.len();
            let ids: Vec<String> = app
                .live_tmux_sessions
                .iter()
                .map(|s| {
                    if let (Some(repo), Some(num)) = (&s.repo_name, s.issue_number) {
                        format!("{} #{}", repo, num)
                    } else {
                        s.assignment_id.clone()
                    }
                })
                .collect();
            let summary = ids.join(", ");
            let title = format!(
                "{} running interactive session{}",
                n,
                if n == 1 { "" } else { "s" }
            );
            let body = format!(
                "{} — open Terminal tab to reattach, or: coord reattach <id>",
                summary
            );
            app.push_toast(&title, &body, ToastSeverity::Info);
        }
        app
    }

    /// Build the [`ShellConfig`] for the AppShell chrome.
    ///
    /// Three activity-bar panels correspond to the three top-level views.
    /// The status bar is enabled so `render_content()` can draw into
    /// `layout.status_bar_bounds`.
    pub fn shell_config() -> ShellConfig {
        // §2 (#782): Settings is pinned to the bottom of the activity bar via
        // `with_bottom_items` so it is visually separated from the primary views.
        let settings_panel = PanelDefinition {
            id: WidgetId::new("panel:settings"),
            // ⚙ gear icon for settings.
            icon: "⚙".into(),
            tooltip: "Settings".into(),
            title: "SETTINGS".into(),
        };
        let mut config = ShellConfig::new(
            "coord-tui",
            vec![
                PanelDefinition {
                    id: WidgetId::new("panel:board"),
                    icon: "B".into(),
                    tooltip: "Board".into(),
                    title: "BOARD".into(),
                },
                PanelDefinition {
                    id: WidgetId::new("panel:machines"),
                    icon: "M".into(),
                    tooltip: "Machines".into(),
                    title: "MACHINES".into(),
                },
                PanelDefinition {
                    id: WidgetId::new("panel:pipeline"),
                    // ▶ marks a horizontal play / run pipeline.
                    icon: "▶".into(),
                    tooltip: "Pipeline".into(),
                    title: "PIPELINE".into(),
                },
                // #424: embedded terminal pane (PTY-backed shell via
                // quadraui::terminal_engine::TerminalSession).
                PanelDefinition {
                    id: WidgetId::new("panel:terminal"),
                    // >_ for a shell prompt glyph.
                    icon: ">_".into(),
                    tooltip: "Terminal".into(),
                    title: "TERMINAL".into(),
                },
                // §1 (#782): Kanban view — three-column board display.
                PanelDefinition {
                    id: WidgetId::new("panel:kanban"),
                    // ▦ box-grid icon for a kanban board.
                    icon: "▦".into(),
                    tooltip: "Kanban".into(),
                    title: "KANBAN".into(),
                },
                // §1 (#782): Merge Queue panel — global PR merge pipeline.
                PanelDefinition {
                    id: WidgetId::new("panel:mergequeue"),
                    // ≣ horizontal-lines icon for a queue.
                    icon: "≣".into(),
                    tooltip: "Merge Queue".into(),
                    title: "MERGE QUEUE".into(),
                },
                // #975: Plans panel — elevates/subsumes the older #771
                // "Milestones" DAG view.  One row per milestone/epic with
                // ready / blocked / in-flight / done counts, sourced from
                // the server-computed `plan_roster` on `/board`.  The old
                // `panel:milestones` id is still recognised in
                // `on_shell_event` for backward-compat with users who had
                // it pinned, but the button surfaces as "Plans" now.
                PanelDefinition {
                    id: WidgetId::new("panel:plans"),
                    // ◆ diamond — conventional plan/milestone marker.
                    icon: "◆".into(),
                    tooltip: "Plans".into(),
                    title: "PLANS".into(),
                },
                // #1032: Sessions panel — fleet-wide machine → repo → session
                // tree of live claude work sessions. ◉ matches the icon used
                // by the pre-existing #628 fleet-wide live-sessions overlay.
                PanelDefinition {
                    id: WidgetId::new("panel:sessions"),
                    icon: "◉".into(),
                    tooltip: "Sessions".into(),
                    title: "SESSIONS".into(),
                },
                // #1039: Audit panel — newest-first list of the audit trail
                // (`/audit`, #1037) with an inline entry-detail view. `§`
                // (section-mark) is the contract-pinned icon
                // (tests/acceptance/ms-33/contract.md §1).
                PanelDefinition {
                    id: WidgetId::new("panel:audit"),
                    icon: "§".into(),
                    tooltip: "Audit".into(),
                    title: "AUDIT".into(),
                },
                // #1741: Reports panel — one collapsible section per entry in
                // the `GET /report` catalogue (#1742). `▤` collides with none
                // of the icons above (B M ▶ >_ ▦ ≣ ◆ ◉ § $) or the pinned ⚙.
                PanelDefinition {
                    id: WidgetId::new("panel:reports"),
                    icon: "▤".into(),
                    tooltip: "Reports".into(),
                    title: "REPORTS".into(),
                },
                // #1866 (Q-1): Queue panel — the live drive-queue grid.
                // `⇅` (up-down arrow) is the reorder affordance that is the
                // point of this panel, and collides with none of the icons
                // above (B M ▶ >_ ▦ ≣ ◆ ◉ § ▤) or the pinned ⚙. Deliberately
                // NOT `≣`, which is already Merge Queue — two queues with
                // one glyph is how an activity bar stops being scannable.
                PanelDefinition {
                    id: WidgetId::new("panel:queue"),
                    icon: "⇅".into(),
                    tooltip: "Queue".into(),
                    title: "QUEUE".into(),
                },
                // #2532 (ms-67 contract §3a): Approved work items panel —
                // portal submissions ready for decomposition. `✓` collides
                // with none of the icons above (B M ▶ >_ ▦ ≣ ◆ ◉ § ▤ ⇅) or
                // the pinned ⚙; it echoes the same glyph `icon_for_action`
                // already uses for `mark-refined`/`approve-gate-a`/`ready`
                // (a different namespace — panel icon vs. row-action icon —
                // so not a re-collision). Appended after Queue and before
                // the bottom-pinned Settings, per contract §3a.
                PanelDefinition {
                    id: WidgetId::new("panel:approved"),
                    icon: "✓".into(),
                    tooltip: "Approved work items".into(),
                    title: "APPROVED WORK ITEMS".into(),
                },
            ],
        )
        .with_status_bar()
        // §2 (#782): Settings pinned to the bottom of the activity bar.
        .with_bottom_items(vec![settings_panel]);
        config.default_sidebar_width = 35.0;
        config.min_sidebar_width = 20.0;
        config.max_sidebar_width = 55.0;
        config
    }

    /// Switch `active_view`, keeping quadraui's AppShell chrome (ActivityBar
    /// highlight + sidebar panel header) in sync (#1029 bug A).
    ///
    /// `on_shell_event` (`render.rs`) already keeps the two in sync for a
    /// switch the operator drove by clicking the ActivityBar — the shell
    /// tells us about those. It has no equivalent for the *other*
    /// direction: an action handler (e.g. `launch_milestone_chat_session`)
    /// that decides on its own to jump to a different panel. A raw
    /// `self.active_view = ...` write in that case changes what
    /// `render_content` draws in the main pane / sidebar tree, but leaves
    /// the ActivityBar highlight and panel header — state quadraui's
    /// `AppShell` owns internally and never exposes for direct mutation —
    /// still pointing at whatever panel was active before. Every
    /// programmatic (non-click) view switch should go through this method
    /// instead of writing `active_view` directly; it queues the panel id
    /// for `ShellApp::take_requested_panel` to hand back to quadraui, which
    /// applies it to the real `AppShell` state and re-fires
    /// `on_shell_event` exactly as a click would.
    ///
    /// `MilestoneDag` has no ActivityBar entry of its own (reached only as
    /// a Plans drill-down — see `SidebarView::panel_widget_id`), so
    /// switching to it queues nothing; that matches its pre-#1029 behavior,
    /// since there was never any chrome for it to desync from.
    pub(crate) fn switch_active_view(&mut self, view: SidebarView) {
        self.active_view = view;
        if let Some(id) = view.panel_widget_id() {
            self.pending_panel_switch = Some(id);
        }
        // The "return to origin on Esc" bookmark (#1029 bug B) belongs to
        // the single milestone-chat launch that set it. Clear it on EVERY
        // view switch — crucially including switches *into* Terminal for an
        // unrelated reason (review/fix/merge/fleet-terminal/reattach), which
        // the old `if view != Terminal` guard silently let inherit a stale
        // bookmark from an earlier milestone chat (iter-1 review Bug B gap).
        // The one site that legitimately wants a bookmark
        // (`launch_milestone_chat_session`) re-sets it *after* calling this.
        self.terminal_return_view = None;
    }

    /// Kick off a background data load if one is not already in flight.
    fn refresh(&mut self) {
        if self.pending_data.is_none() {
            self.pending_data = Some(start_data_load());
        }
        // #559: re-arm remote session discovery so per-row liveness stays
        // current for the entire TUI run.  The initial arm is one-shot
        // (startup only); without this, any session started after startup
        // never registers as live until the TUI is restarted.  (#1487 dropped
        // the Live/Idle *grouping*, but liveness still drives the per-row
        // `in-flight`/`ready` badge — the refresh is still required.)
        if self.pending_remote_sessions.is_none() {
            self.pending_remote_sessions = Some(spawn_remote_tmux_sessions_fetch(
                crate::commands::find_config(),
            ));
        }
        // #953: re-arm the fleet-terminal remote sweep for the same reason.
        if self.pending_remote_terminals.is_none() {
            self.pending_remote_terminals = Some(spawn_remote_fleet_terminals_fetch(
                crate::commands::find_config(),
            ));
        }
        // #1398: re-arm the drive-session sweep for the same reason — a
        // drive started (or stopped) from an EXTERNAL shell should still
        // show up without a full TUI restart.
        if self.pending_drive_sessions.is_none() {
            self.pending_drive_sessions = Some(spawn_drive_sessions_fetch());
        }
        // #1563: the paused-machine sweep is deliberately NOT re-armed here.
        // Unlike the sweeps above (which shell out to `coord` and are
        // stubbed/harmless under `CommandRunner::new_for_test()`),
        // `spawn_paused_machines_fetch` makes a REAL `ureq` HTTP call when a
        // board service is configured — re-arming it from every mutation
        // that calls `refresh()` (label changes, dispatches, …) would fire
        // an extra `GET /pause` against whatever board-service mock a test
        // happens to have wired up via `set_test_board_service`, breaking
        // exact-request-count assertions unrelated to pausing. It's instead
        // re-armed on the plain periodic cadence in `settings_ui.rs`'s
        // `should_refresh` block, exactly where the pre-#1563 local-file
        // rescan used to live — same cadence, same "picks up out-of-band
        // changes within a few seconds" guarantee, just not from every
        // mutation-triggered `refresh()` call too.
    }

    /// Push a new toast with the given title, body, and severity. Toasts
    /// auto-expire after [`TOAST_TTL`] in [`render`]/[`prune_toasts`].
    #[allow(dead_code)] // used by the watch overlay (#TBD)
    fn push_toast(&mut self, title: &str, body: &str, severity: ToastSeverity) {
        self.next_toast_id += 1;
        let id = WidgetId::new(format!("toast-{}", self.next_toast_id));
        let item = ToastItem {
            id,
            title: title.to_string(),
            body: body.to_string(),
            severity,
            action: None,
            accent: None,
        };
        self.toasts.push((item, Instant::now(), severity));
    }

    /// Drop toasts older than [`TOAST_TTL`] so the overlay stays uncluttered.
    fn prune_toasts(&mut self) {
        self.toasts.retain(|(_, t, _)| t.elapsed() < TOAST_TTL);
    }

    /// Build the visible `ToastStack` for the bottom-right overlay.
    ///
    /// Returns `None` when no toasts are active.  Auto-promotes the
    /// most recent `pipeline_status` message to a toast so every
    /// dispatch site already gets corner feedback without each helper
    /// having to call `push_toast` explicitly.
    fn toast_stack(&self) -> Option<ToastStack> {
        let mut items: Vec<ToastItem> = self.toasts.iter().map(|(it, _, _)| it.clone()).collect();
        if let Some((msg, when)) = &self.pipeline_status {
            if when.elapsed() < TOAST_TTL {
                items.push(ToastItem {
                    id: WidgetId::new(format!("pipeline-status-{}", when.elapsed().as_millis())),
                    title: "Pipeline".to_string(),
                    body: msg.clone(),
                    severity: if msg.contains("no reachable")
                        || msg.contains("no failed")
                        || msg.contains("not found")
                    {
                        ToastSeverity::Warning
                    } else {
                        ToastSeverity::Info
                    },
                    action: None,
                    accent: None,
                });
            }
        }
        if items.is_empty() {
            return None;
        }
        Some(ToastStack {
            id: WidgetId::new("coord-toasts"),
            corner: ToastCorner::BottomRight,
            toasts: items,
        })
    }

    /// Open the watch overlay for the running assignment of the currently
    /// selected Pipeline issue. Falls back to the most recent non-done
    /// assignment when nothing is actively running; pushes a toast and
    /// returns `false` when there's nothing to watch.
    ///
    /// If the assignment already has a live context in `watch_pool` (because
    /// the user switched away from it earlier), we simply re-focus it without
    /// disturbing the background stream.  Otherwise we open a new SSE
    /// connection and insert it into the pool (evicting the LRU entry if the
    /// pool has reached `WATCH_POOL_CAP`).
    fn open_watch_for_selected_issue(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let local_repo = issue.coord_repo.as_deref();

        let candidates: Vec<_> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .collect();

        // Prefer running → any non-done → most-recent done (read-only log review).
        let pick = candidates
            .iter()
            .copied()
            .find(|a| a.status == "running")
            .or_else(|| candidates.iter().copied().find(|a| a.status != "done"))
            .or_else(|| {
                candidates.iter().copied().max_by(|a, b| {
                    a.dispatched_at
                        .partial_cmp(&b.dispatched_at)
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
            });

        match pick {
            Some(a) => {
                let aid = a.id.clone();
                // Re-focus an existing stream without touching it.
                if self.watch_pool.contains_key(&aid) {
                    if let Some(ctx) = self.watch_pool.get_mut(&aid) {
                        ctx.last_focused_at = Instant::now();
                    }
                    self.watch_focused = Some(aid);
                    return true;
                }
                // New stream — build WatchState and open SSE.
                let state = WatchState {
                    assignment_id: a.id.clone(),
                    machine: a.machine.clone(),
                    repo: a.repo.clone(),
                    issue_number: a.issue_number,
                    assignment_type: a
                        .assignment_type
                        .clone()
                        .unwrap_or_else(|| "work".to_string()),
                    scroll: usize::MAX,
                };
                let sse = if let Some(m) = self.data.machines.iter().find(|m| m.name == a.machine) {
                    if !m.host.is_empty() {
                        let rx = spawn_sse_watch(&m.host, &a.id, 0);
                        WatchSseState {
                            rx,
                            lines: Vec::new(),
                            last_event_id: 0,
                            fail_count: 0,
                            first_fail_at: None,
                            done: false,
                            host: m.host.clone(),
                            pending_tail: String::new(),
                            line_times: Vec::new(),
                            current_turn: 0,
                        }
                    } else {
                        make_local_sse_state(&a.id)
                    }
                } else {
                    make_local_sse_state(&a.id)
                };
                // Evict the LRU entry if the pool is at capacity.
                if self.watch_pool.len() >= WATCH_POOL_CAP {
                    let lru_id = self
                        .watch_pool
                        .iter()
                        .min_by_key(|(_, ctx)| ctx.last_focused_at)
                        .map(|(id, _)| id.clone());
                    if let Some(id) = lru_id {
                        self.watch_pool.remove(&id);
                    }
                }
                self.watch_pool.insert(
                    aid.clone(),
                    WatchContext {
                        state,
                        sse,
                        inject_transcript: Vec::new(),
                        inject_sse_offsets: Vec::new(),
                        history_turns: Vec::new(),
                        last_focused_at: Instant::now(),
                    },
                );
                self.watch_focused = Some(aid);
                true
            }
            None => {
                self.pipeline_status = Some((
                    format!("no assignment to watch for #{}", issue.number),
                    Instant::now(),
                ));
                false
            }
        }
    }

    /// Close the watch overlay (clear focus).  The background SSE stream for
    /// the previously-focused assignment is kept alive in `watch_pool` so the
    /// user can switch back without a reconnect.
    fn close_watch(&mut self) {
        self.watch_focused = None;
        self.inject_chat = None;
        // #386: if the Log-tab steer overlay was open, clear its flag too.
        self.inject_opened_from_log_tab = false;
    }

    /// True when the Pipeline view's Log tab is the active scroller.  On this
    /// tab the visible content is `pipeline_log_list` (scrolled via
    /// `pipeline_detail_scroll`); used by the periodic `tick` to re-attach
    /// SSE when the running assignment under the cursor changes.
    fn on_pipeline_log_tab(&self) -> bool {
        self.active_view == SidebarView::Pipeline
            && self.pipeline_detail_tab == PipelineDetailTab::Log
    }

    /// Called whenever the Log tab becomes active (and on each tick while it's
    /// active).  Ensures `watch_pool` holds an SSE stream for the assignment
    /// `pipeline_log_list` would currently render — without setting
    /// `watch_focused`, so the watch-overlay j/k arms don't shadow the Log-tab
    /// scroll arms (#308).
    ///
    /// The pool check is keyed on the *picked* assignment id, not just the
    /// issue number.  Without that, auto_loop transitions
    /// (work→review→work→review on the same issue) leave a stale pool entry
    /// for the old assignment and the new running one would fall back to
    /// HTTP polling — reintroducing the "Loading log…" flicker.
    fn ensure_log_tab_sse(&mut self) {
        // #2342: pick the same assignment pipeline_log_list does — its
        // pinned choice if any, else running, else most-recent by
        // dispatched_at — via the shared `log_candidates_for_issue` /
        // `pick_log_source` so this pool entry tracks whatever's visible,
        // including a milestone tracking issue's Gate A `mock-author` row
        // or a member issue's own `test-author` row (both invisible to the
        // old raw-`issue_number`-only match this replaced).
        let pick_id = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i).cloned())
            .and_then(|issue| {
                let candidates = self.log_candidates_for_issue(&issue);
                let pinned = self
                    .pipeline_log_pinned_assignment
                    .get(&pipeline_log_pin_key(&issue));
                pick_log_source(&candidates, pinned).map(|a| a.id.clone())
            });
        let Some(target) = pick_id else {
            return;
        };
        // Pool already has the picked assignment? Leave it (live or done).
        if self.watch_pool.contains_key(&target) {
            return;
        }
        // No entry for the picked assignment — open one without focusing.
        self.open_sse_in_pool_for_selected_issue();
    }

    /// Open (or reuse) an SSE stream in `watch_pool` for the selected issue
    /// WITHOUT setting `watch_focused`.  Used by the Log tab and any other
    /// caller that wants background accumulation but not the watch overlay.
    fn open_sse_in_pool_for_selected_issue(&mut self) {
        let Some(idx) = self.pipeline_sel else {
            return;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return;
        };

        // #2342: route through the same candidate list / pin-aware pick as
        // `pipeline_log_list` (what the Log tab renders) and
        // `ensure_log_tab_sse` (which target it computes before falling
        // through to this function), instead of an independent
        // running-else-newest heuristic. Keeps all three from silently
        // disagreeing on "the" assignment for this issue's Log tab, and
        // makes this function pin-aware — previously it had no knowledge of
        // `pipeline_log_pinned_assignment` at all, so a pinned Gate A row
        // could render correctly (via the HTTP/file fallback) while this
        // function kept a different assignment's SSE stream warm.
        //
        // Cloned out of the borrow up front: `log_candidates_for_issue`
        // takes `&self`, so its returned refs (and anything derived from
        // them) must be gone before the `&mut self` calls below (watch_pool
        // insert, etc.).
        let picked: Option<Assignment> = {
            let candidates = self.log_candidates_for_issue(&issue);
            let pinned = self
                .pipeline_log_pinned_assignment
                .get(&pipeline_log_pin_key(&issue));
            pick_log_source(&candidates, pinned).cloned()
        };

        let Some(a) = picked else {
            return;
        };
        let aid = a.id.clone();

        // Already in the pool — nothing to do.
        if self.watch_pool.contains_key(&aid) {
            return;
        }
        let state = WatchState {
            assignment_id: a.id.clone(),
            machine: a.machine.clone(),
            repo: a.repo.clone(),
            issue_number: a.issue_number,
            assignment_type: a
                .assignment_type
                .clone()
                .unwrap_or_else(|| "work".to_string()),
            scroll: usize::MAX,
        };
        let sse = if let Some(m) = self.data.machines.iter().find(|m| m.name == a.machine) {
            if !m.host.is_empty() {
                let rx = spawn_sse_watch(&m.host, &a.id, 0);
                WatchSseState {
                    rx,
                    lines: Vec::new(),
                    last_event_id: 0,
                    fail_count: 0,
                    first_fail_at: None,
                    done: false,
                    host: m.host.clone(),
                    pending_tail: String::new(),
                    line_times: Vec::new(),
                    current_turn: 0,
                }
            } else {
                make_local_sse_state(&a.id)
            }
        } else {
            make_local_sse_state(&a.id)
        };
        if self.watch_pool.len() >= WATCH_POOL_CAP {
            let lru_id = self
                .watch_pool
                .iter()
                .min_by_key(|(_, ctx)| ctx.last_focused_at)
                .map(|(id, _)| id.clone());
            if let Some(id) = lru_id {
                self.watch_pool.remove(&id);
            }
        }
        self.watch_pool.insert(
            aid,
            WatchContext {
                state,
                sse,
                inject_transcript: Vec::new(),
                inject_sse_offsets: Vec::new(),
                history_turns: Vec::new(),
                last_focused_at: Instant::now(),
            },
        );
    }

    /// Force a fresh SSE connection for the focused watch session (R key).
    ///
    /// Replaces the existing `WatchSseState` with a fresh connection starting
    /// from byte offset 0, so the full log is streamed from the beginning.
    fn reset_sse_watch(&mut self) {
        let id = match &self.watch_focused {
            Some(id) => id.clone(),
            None => return,
        };
        let (host, machine) = match self.watch_pool.get(&id) {
            Some(ctx) => (ctx.sse.host.clone(), ctx.state.machine.clone()),
            None => return,
        };
        // If we don't have a host yet (local machine), try to derive it.
        let host = if host.is_empty() {
            match self.data.machines.iter().find(|m| m.name == machine) {
                Some(m) if !m.host.is_empty() => m.host.clone(),
                _ => return,
            }
        } else {
            host
        };
        let rx = spawn_sse_watch(&host, &id, 0);
        if let Some(ctx) = self.watch_pool.get_mut(&id) {
            ctx.sse = WatchSseState {
                rx,
                lines: Vec::new(),
                last_event_id: 0,
                fail_count: 0,
                first_fail_at: None,
                done: false,
                host,
                pending_tail: String::new(),
                line_times: Vec::new(),
                current_turn: 0,
            };
        }
    }

    // ── Focused-context accessors ─────────────────────────────────────────────

    /// Borrow the `WatchState` for the focused session, if any.
    fn focused_watch_state(&self) -> Option<&WatchState> {
        self.watch_focused
            .as_ref()
            .and_then(|id| self.watch_pool.get(id))
            .map(|ctx| &ctx.state)
    }

    /// Mutably borrow the `WatchState` for the focused session, if any.
    fn focused_watch_state_mut(&mut self) -> Option<&mut WatchState> {
        let id = self.watch_focused.clone()?;
        self.watch_pool.get_mut(&id).map(|ctx| &mut ctx.state)
    }

    /// Borrow the inject transcript for the focused session (empty slice if none).
    fn focused_transcript(&self) -> &[ChatTurn] {
        self.watch_focused
            .as_ref()
            .and_then(|id| self.watch_pool.get(id))
            .map(|ctx| ctx.inject_transcript.as_slice())
            .unwrap_or(&[])
    }

    /// Accept the plan being watched: dispatches `coord approve-plan <id>`.
    /// No-op when the watched assignment isn't a plan; toasts otherwise.
    fn approve_watched_plan(&mut self) -> bool {
        let (aid, issue_number, atype) = match self.focused_watch_state() {
            Some(w) => (
                w.assignment_id.clone(),
                w.issue_number,
                w.assignment_type.clone(),
            ),
            None => return false,
        };
        if atype != "plan" {
            self.pipeline_status = Some((
                "approve only works on a plan-type assignment".to_string(),
                Instant::now(),
            ));
            return false;
        }
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["approve-plan", &aid]);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("approving plan #{} → dispatching work", issue_number),
                    Instant::now(),
                ));
                // Close the watch so the user returns to Stages and can see the
                // new work assignment appear on the next refresh.
                self.close_watch();
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "approve-plan runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
    }

    /// Find a running or non-done assignment for the currently-selected
    /// Pipeline row and dispatch `coord stop <id>` for it.  Returns
    /// `true` when a stop was dispatched, `false` when no candidate
    /// assignment was found (the caller toasts the user).
    fn dispatch_stop_for_selected_pipeline_row(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let local_repo = issue.coord_repo.as_deref();
        // Prefer a running worker; fall back to any non-done assignment
        // (matches `open_watch_for_selected_issue`'s ordering so
        // Watch and Stop target the same worker).
        let pick = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .find(|a| a.status == "running")
            .or_else(|| {
                self.data
                    .assignments
                    .iter()
                    .filter(|a| a.issue_number == issue.number)
                    .filter(|a| match local_repo {
                        Some(r) => a.repo == r,
                        None => true,
                    })
                    .find(|a| a.status != "done")
            });
        let Some(a) = pick else {
            return false;
        };
        let aid = a.id.clone();
        let issue_n = a.issue_number;
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["stop", &aid]);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status =
                    Some((format!("stop dispatched for #{}", issue_n), Instant::now()));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "stop runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
    }

    /// Open the PR for the currently-selected Pipeline row in the user's
    /// default browser via `gh pr view --web`.  Returns `true` when the
    /// child was spawned; `false` when no PR has been opened yet (the
    /// merge_queue entry has no `pr_number`).
    fn dispatch_open_pr_for_selected_pipeline_row(&mut self) -> bool {
        let Some(idx) = self.pipeline_sel else {
            return false;
        };
        let Some(issue) = self.pipeline_issues.get(idx).cloned() else {
            return false;
        };
        let Some(pr_number) = self.pipeline_pr_number(&issue) else {
            return false;
        };
        // gh handles xdg-open / open / start cross-platform.  Fire and
        // forget — we don't care about exit code (the user sees the
        // browser open, not a coord-tui status).
        let _ = std::process::Command::new("gh")
            .args([
                "pr",
                "view",
                &pr_number.to_string(),
                "--repo",
                &issue.repo_slug,
                "--web",
            ])
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
        self.pipeline_status = Some((
            format!("opening PR #{} in browser…", pr_number),
            Instant::now(),
        ));
        true
    }

    /// Stop the assignment being watched: dispatches `coord stop <id>`.
    /// Pushes a toast on success or when another command is running.
    fn kill_watched(&mut self) -> bool {
        let (aid, issue_number) = match self.focused_watch_state() {
            Some(w) => (w.assignment_id.clone(), w.issue_number),
            None => return false,
        };
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["stop", &aid]);
        match outcome {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("stop dispatched for #{}", issue_number),
                    Instant::now(),
                ));
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "stop runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        matches!(
            outcome,
            SpawnQueuedOutcome::Started | SpawnQueuedOutcome::Queued
        )
    }

    /// Build the body `ListView` for the watch overlay — the raw log lines
    /// from the worker.  Title carries the repo/issue/machine context.
    /// Inject prompt (when open) is rendered as the last list row.
    ///
    /// Log content is driven by the SSE stream when the focused context has
    /// a live stream; falls back to the polling path (`get_activity_log`)
    /// when SSE is unavailable (e.g. local assignment, no host).
    fn watch_log_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        let ctx = self
            .watch_focused
            .as_ref()
            .and_then(|id| self.watch_pool.get(id));
        let title = match ctx {
            None => " WATCH ".to_string(),
            Some(ctx) => {
                let w = &ctx.state;
                let sse = &ctx.sse;
                // SSE stream: show accumulated lines or "Connecting…" placeholder.
                if !sse.done || !sse.lines.is_empty() {
                    if sse.lines.is_empty() && !sse.done {
                        items.push(kv_item(
                            "",
                            "  Connecting to log stream…",
                            Some(Color::rgb(140, 140, 140)),
                        ));
                    } else {
                        // Use the readable renderer (#385) so the live watch view
                        // word-wraps prose and shows arrow-prefixed tool calls —
                        // same format as the Log tab and `coord log` output.
                        let wrap_width = self.last_log_panel_cols.get().max(40);
                        items.extend(parse_sse_log_readable(
                            &sse.lines,
                            &sse.line_times,
                            wrap_width,
                        ));
                    }
                    if sse.done {
                        items.push(kv_item(
                            "",
                            "  ── stream ended ──",
                            Some(Color::rgb(90, 90, 90)),
                        ));
                    }
                } else {
                    // Fallback: polling path (local file or remote HTTP GET).
                    items.extend(self.get_activity_log(&w.assignment_id, &w.machine));
                }
                let extra_keys = if w.assignment_type == "plan" {
                    "  A=accept"
                } else {
                    ""
                };
                format!(
                    " WATCH — {} #{} → {} ({}) (b=chat{}  K=kill  R=refresh  q=close) ",
                    w.repo, w.issue_number, w.machine, w.assignment_type, extra_keys
                )
            }
        };
        // Stick-to-bottom default: position the viewport so the LAST item
        // is the last visible row. The viewport-row count is cached during
        // the most recent mouse_main_scroll / tick (see
        // `last_main_visible_rows`); on the very first frame it defaults to
        // 40 so this fits a typical terminal. The hard-coded 40 used to be
        // a literal `items.len() - 40`, which clipped the latest lines on
        // smaller terminals.
        let visible_rows = self.last_main_visible_rows.get().max(1);
        let scroll = self
            .watch_focused
            .as_ref()
            .and_then(|id| self.watch_pool.get(id))
            .map(|ctx| {
                if ctx.state.scroll == usize::MAX {
                    items.len().saturating_sub(visible_rows)
                } else {
                    ctx.state.scroll
                }
            })
            .unwrap_or(0);
        ListView {
            id: WidgetId::new("watch-log"),
            title: Some(StyledText::plain(&title)),
            items,
            selected_idx: 0,
            scroll_offset: scroll,
            has_focus: false,
            bordered: true,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Send `text` to the watched worker by POSTing to the agent's
    /// `/inject/{id}` endpoint directly.  Bypasses the single-slot
    /// `command_runner` so chat submits aren't blocked by the periodic
    /// `coord notify` (which fires every 30 s while any assignment is
    /// running and otherwise gives a guaranteed "another command is
    /// running" lockout every cycle for a long-lived refinement chat).
    /// Appends the text to the inject transcript and clears the chat
    /// input on success.
    ///
    /// Convenience wrapper that defaults to showing the "Steer sent"
    /// confirmation toast — appropriate for user-typed submits.  Internal
    /// callers like [`Self::trigger_refinement_notes_synth`] that want a
    /// silent send (they emit their own tailored toasts) should call
    /// [`Self::submit_inject_with_toast`] with `show_toast = false`
    /// instead.
    fn submit_inject(&mut self, text: String) -> bool {
        self.submit_inject_with_toast(text, true)
    }

    /// Implementation of [`Self::submit_inject`] with explicit control
    /// over the "Steer sent" confirmation toast.
    fn submit_inject_with_toast(&mut self, text: String, show_toast: bool) -> bool {
        let (aid, issue_number, machine_name, old_type) = match self.focused_watch_state() {
            Some(w) => (
                w.assignment_id.clone(),
                w.issue_number,
                w.machine.clone(),
                w.assignment_type.clone(),
            ),
            None => return false,
        };
        let text = text.trim().to_string();
        if text.is_empty() {
            return false;
        }
        // #264: claude -p exits after `stop_reason: end_turn` (it doesn't
        // stay alive waiting for more injects), so once the assignment is
        // in `done` state any new POST /inject/{id} will return 410
        // BrokenPipeError.
        //
        // #315: Instead of refusing, fall back to `coord chat-continue` which
        // re-dispatches the worker with `--resume <session_id>` so it loads
        // the full prior conversation before seeing this message as the next
        // user turn.  The TUI polls for the new assignment row and rebinds the
        // chat overlay to it via `maybe_bind_pending_resume`.
        //
        // We OR two signals: (1) `data.assignments` status — authoritative
        // but updated only on the coordinator's notify cycle (seconds of
        // lag), and (2) `sse.done` — flips the instant the SSE stream
        // closes, which happens immediately when the worker exits.  Without
        // the SSE check, a user submit within the few-second lag window
        // after `end_turn` would slip through the worker_done gate and hit
        // the agent's `/inject/{id}` endpoint, which returns HTTP 409
        // ("assignment is `done`, not running") and the message was lost.
        let worker_done = self.data.assignments.iter().any(|a| {
            a.id == aid && (a.status == "done" || a.status == "failed" || a.status == "cancelled")
        }) || self
            .watch_pool
            .get(&aid)
            .map(|ctx| ctx.sse.done)
            .unwrap_or(false);
        // Guard against double-dispatch: if a resume is already in flight,
        // refuse new submits with a hint rather than firing a second
        // chat-continue against the same session (which would race the
        // first and confuse claude's session storage).
        if worker_done && self.pending_chat_resume.is_some() {
            self.pipeline_status = Some((
                "⏳ Resume in flight — wait for assistant reply before sending again".to_string(),
                Instant::now(),
            ));
            return false;
        }
        if worker_done {
            // Record the user turn in the transcript before the shell-out so
            // the typed text is visible immediately and can't disappear even
            // if the dispatch takes a moment.
            let sse_offset_at_send = self
                .watch_pool
                .get(&aid)
                .map(|ctx| ctx.sse.lines.len())
                .unwrap_or(0);
            {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .ok()
                    .map(|d| d.as_secs_f64());
                let turn = ChatTurn {
                    role: ChatRole::User,
                    text: StyledText::plain(text.clone()),
                    timestamp_unix: now,
                    line_scales: Vec::new(),
                };
                if let Some(ctx) = self.watch_pool.get_mut(&aid) {
                    ctx.inject_transcript.push(turn);
                    ctx.inject_sse_offsets.push(sse_offset_at_send);
                }
            }
            // Clear the input field.
            if let Some(ref mut chat) = self.inject_chat {
                chat.clear_input();
            }
            // Dispatch the resume in a background thread.
            let config_path = self.command_runner.config_path.clone();
            let arm_unix_secs = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            spawn_chat_continue(config_path, aid.clone(), text.clone());
            // Arm the pending-resume poll so the overlay rebinds on the next
            // data refresh that surfaces the new assignment row.
            self.pending_chat_resume = Some(PendingChatResume {
                old_assignment_id: aid.clone(),
                issue_number,
                dispatched_at: Instant::now(),
                arm_unix_secs,
                old_type: Some(old_type),
            });
            self.pipeline_status =
                Some((format!("⏳ Resuming chat #{issue_number}…"), Instant::now()));
            return true;
        }
        let host = match self.data.machines.iter().find(|m| m.name == machine_name) {
            Some(m) if !m.host.is_empty() => m.host.clone(),
            _ => {
                self.pipeline_status = Some((
                    format!("no host for machine {}", machine_name),
                    Instant::now(),
                ));
                return false;
            }
        };
        spawn_inject_post(
            &host,
            &aid,
            &text,
            issue_number,
            self.inject_fallback_tx.clone(),
        );
        // Optimistic toast: fire-and-forget; 409/410 will fall back via
        // inject_fallback_rx, so we surface the confirmation immediately
        // rather than waiting for the HTTP round-trip.  Use "Steer sent"
        // (not "delivered") since network errors are silently dropped.
        //
        // Suppressed for internal callers (e.g. refinement-notes synth)
        // that emit their own tailored toasts — the user pressed Ctrl+N,
        // not the steer keybind, so "Steer sent" would be misleading.
        if show_toast {
            self.push_toast(
                "Steer sent",
                &format!("Message sent to worker #{}", issue_number),
                ToastSeverity::Info,
            );
        }
        // Stamp activity NOW so the busy spinner appears the instant the
        // user sends — without this we'd wait for the first SSE byte to
        // come back (which can be several seconds for the model's
        // first-token latency).
        self.chat_last_activity = Some(Instant::now());
        // Capture the SSE stream position at submit time so the transcript
        // can interleave this user turn between the right pair of
        // assistant turns when it rebuilds.  Without this the user turns
        // all pile at the top of the transcript regardless of when they
        // were sent (the bug the user reported: "my message goes above
        // the ai messages not inline in order").
        let sse_offset_at_send = self
            .watch_pool
            .get(&aid)
            .map(|ctx| ctx.sse.lines.len())
            .unwrap_or(0);
        {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .ok()
                .map(|d| d.as_secs_f64());
            let turn = ChatTurn {
                role: ChatRole::User,
                text: StyledText::plain(text.clone()),
                timestamp_unix: now,
                line_scales: Vec::new(),
            };
            if let Some(ctx) = self.watch_pool.get_mut(&aid) {
                ctx.inject_transcript.push(turn);
                ctx.inject_sse_offsets.push(sse_offset_at_send);
            }
            // #264: build the chat transcript by merging user turns from
            // inject_transcript with assistant turns parsed from the SSE
            // stream-json lines, so the chat shows both sides of the
            // conversation.  The previous behaviour (user turns only)
            // worked for worker-guidance chat — the user assumed the
            // assistant was "doing the work" via the Log tab — but for
            // refinement-chat the assistant's reply IS the deliverable.
            let transcript = self
                .focused_watch_state()
                .map(|w| w.assignment_id.clone())
                .and_then(|id| self.watch_pool.get(&id))
                .map(chat_transcript_from_pool)
                .unwrap_or_else(|| self.focused_transcript().to_vec());
            if let Some(ref mut chat) = self.inject_chat {
                chat.set_transcript(transcript);
                chat.clear_input();
            }
            self.pipeline_status = Some((
                format!("asked worker #{}: {}", issue_number, text),
                Instant::now(),
            ));
        }
        true
    }

    /// Spawn `coord sync --quiet` in the background if not already running
    /// and the last sync was more than 5 minutes ago (or never run).
    fn kick_issue_sync(&mut self) {
        const SYNC_INTERVAL: Duration = Duration::from_secs(300);
        // #584: a thin client must not auto-run `coord sync` (host-side; it
        // writes the issues cache to the local DB and needs a config).
        if is_remote_board_service() {
            return;
        }
        if let Some(last) = self.issue_sync_last {
            if last.elapsed() < SYNC_INTERVAL {
                return;
            }
        }
        if self.command_runner.is_running() {
            return;
        }
        if self.command_runner.spawn(&["sync", "--quiet"]) {
            self.issue_sync_last = Some(Instant::now());
        }
    }

    /// Force-run `coord sync` immediately, bypassing the 5-minute guard.
    /// Used by the Sync toolbar button and the `S` keybind in the Board panel.
    fn force_issue_sync(&mut self) {
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&["sync", "--quiet"]) {
            SpawnQueuedOutcome::Started => {
                self.issue_sync_last = Some(Instant::now());
                self.push_toast(
                    "Sync",
                    "Fetching open issues from GitHub…",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Sync",
                    "Sync queued — will run after current command.",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {
                // A sync is already running or already queued — nothing to do.
            }
        }
    }

    /// Drain any completed background data load, applying results to
    /// `self.data`.  Returns `true` if data was updated (caller should
    /// trigger a redraw).
    /// #486: build the Pipeline issue list from the local DB cache
    /// (`data.open_issues`, kept fresh by the background `coord sync`) instead
    /// of a live `gh search`.  Same source the Board uses — instant, no Search
    /// API rate limit (30/min), no eventually-consistent index lag.  An issue
    /// is in the Pipeline iff it carries one of the tracked labels (e.g.
    /// `coord`); classification into New/Active/Done is done downstream by
    /// `pipeline_lifecycle_section` against `data.assignments` / `merge_queue`.
    fn pipeline_issues_from_cache(&self) -> Vec<PipelineIssue> {
        let tracked: std::collections::HashSet<&str> = self
            .data
            .pipeline_tracked_labels
            .iter()
            .map(|s| s.as_str())
            .collect();
        if tracked.is_empty() {
            return Vec::new();
        }
        // coord-local repo name → github slug (for PipelineIssue.repo_slug).
        let slug_of: std::collections::HashMap<&str, &str> = self
            .data
            .pipeline_repos
            .iter()
            .map(|(local, slug)| (local.as_str(), slug.as_str()))
            .collect();
        let mut issues: Vec<PipelineIssue> = self
            .data
            .open_issues
            .iter()
            .filter_map(|oi| {
                let matched: Vec<String> = oi
                    .labels
                    .iter()
                    .filter(|l| tracked.contains(l.as_str()))
                    .cloned()
                    .collect();
                if matched.is_empty() {
                    return None; // not tracked → not in the Pipeline
                }
                let repo_slug = slug_of
                    .get(oi.repo_name.as_str())
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| oi.repo_name.clone());
                Some(PipelineIssue {
                    number: oi.number,
                    title: oi.title.clone(),
                    body: oi.body.clone(),
                    repo_slug,
                    coord_repo: Some(oi.repo_name.clone()),
                    matched_labels: matched,
                    all_labels: oi.labels.clone(),
                    is_closed: oi.state == "closed",
                    body_truncated: oi.body_truncated,
                    body_len: oi.body_len,
                })
            })
            .collect();

        // #1199 fix (blank pipeline lane for a regular issue nested under
        // an epic): an epic's `## Sub-issues` children are real,
        // dispatchable issues that #1197 nests beneath the epic's row in
        // the Pipeline tree — but a child that doesn't independently carry
        // a tracked label was previously absent from `pipeline_issues`
        // entirely. `resolve_nested_child_index` then had no index to
        // resolve to, so clicking such a child left `pipeline_sel` at
        // `None`: no Work/Test/Review/Merge lane rendered, and the Issue
        // tab fell back to "No issue selected". Backfill any such child
        // from `data.open_issues` (when still cached) so it's
        // independently selectable exactly like a directly-tracked
        // sibling. This can't create a duplicate flat top-level row: the
        // existing `nesting.nested` de-dup (#1197,
        // `compute_epic_nesting`) already suppresses a flat row for any
        // child issue number found under its parent epic within the same
        // lifecycle bucket, and that check is keyed on issue number alone
        // — it doesn't care whether the child got here via the tracked-
        // label filter above or via this backfill.
        let mut present: std::collections::HashSet<(String, u64)> = issues
            .iter()
            .map(|i| (i.coord_repo.clone().unwrap_or_default(), i.number))
            .collect();
        for ec in &self.data.epic_children {
            // Only backfill children of an epic that is itself tracked
            // (i.e. already made it into `issues` above) — an untracked
            // epic's children are out of scope for the Pipeline.
            if !present.contains(&(ec.repo_name.clone(), ec.tracking_issue)) {
                continue;
            }
            for child in &ec.children {
                let key = (ec.repo_name.clone(), child.number);
                if present.contains(&key) {
                    continue;
                }
                let Some(oi) = self
                    .data
                    .open_issues
                    .iter()
                    .find(|oi| oi.repo_name == ec.repo_name && oi.number == child.number)
                else {
                    // Aged out of the open_issues cache (#771) — stays
                    // nested-display-only, same as before this fix.
                    continue;
                };
                let matched: Vec<String> = oi
                    .labels
                    .iter()
                    .filter(|l| tracked.contains(l.as_str()))
                    .cloned()
                    .collect();
                let repo_slug = slug_of
                    .get(oi.repo_name.as_str())
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| oi.repo_name.clone());
                issues.push(PipelineIssue {
                    number: oi.number,
                    title: oi.title.clone(),
                    body: oi.body.clone(),
                    repo_slug,
                    coord_repo: Some(oi.repo_name.clone()),
                    matched_labels: matched,
                    all_labels: oi.labels.clone(),
                    is_closed: oi.state == "closed",
                    body_truncated: oi.body_truncated,
                    body_len: oi.body_len,
                });
                present.insert(key);
            }
        }

        // Stable order: by repo, then issue number (mirrors the old loader).
        issues.sort_by(|a, b| a.repo_slug.cmp(&b.repo_slug).then(a.number.cmp(&b.number)));
        issues
    }

    fn apply_pending_data(&mut self) -> bool {
        let rx = match &self.pending_data {
            Some(rx) => rx,
            None => return false,
        };
        match rx.try_recv() {
            Ok(data) => {
                // #620: a refresh tick that comes back completely empty is
                // almost always a transient fetch FAILURE, not a genuinely
                // empty board.  `load_data_remote` returns `BoardData::default()`
                // on any connect/timeout/parse error (thin clients poll the
                // dellserver daemon over Tailscale, so an 8s timeout or a blip
                // yields an empty payload), and the local SQLite path returns it
                // when the DB is briefly locked.  Wholesale-applying that empty
                // payload wiped the Pipeline tree, recomputed an empty
                // `live_keys`, and `retain`-ed every live embedded terminal out
                // of existence — bouncing the operator out of an attended
                // session every couple of minutes and losing selection +
                // tree-expansion state.  Guard: if we ALREADY have data and the
                // incoming tick has no machines, no issues, AND no assignments,
                // treat it as a degraded tick — keep the last good data, surface
                // a soft self-clearing warning, and skip the apply.  (A real
                // first load from an empty board has empty CURRENT data, so the
                // guard is inert and the empty state still applies; machines come
                // from coordinator.yml and never legitimately drop to zero on a
                // configured board, making the all-three-empty check a reliable
                // proxy for the default sentinel.)
                let incoming_empty = data.machines.is_empty()
                    && data.open_issues.is_empty()
                    && data.assignments.is_empty();
                let have_data = !self.data.machines.is_empty()
                    || !self.data.open_issues.is_empty()
                    || !self.data.assignments.is_empty();
                if incoming_empty && have_data {
                    // Consume the receiver but do NOT replace self.data; leave
                    // refreshed_at alone so the "Xs ago" indicator keeps aging
                    // (honest: this tick didn't land).  The next healthy tick
                    // clears the warning and updates normally.
                    self.pending_data = None;
                    self.fetch_error = Some((
                        "refresh failed — showing last good data".into(),
                        Instant::now(),
                    ));
                    return true;
                }

                // Snapshot which assignments were running before the update so
                // we can detect running→done/failed transitions for the audio bell.
                let prev_running: std::collections::HashSet<String> = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| a.status == "running")
                    .map(|a| a.id.clone())
                    .collect();

                self.data = data;
                self.pending_data = None;
                self.refreshed_at = Instant::now();
                self.fetch_error = None;
                // #1344: refresh CI-check badges from the board's own
                // `merge_plan[].ci_summary` — no client-side `gh pr checks`.
                self.sync_ci_checks_from_board();
                // #290: clear optimistic merge-in-flight flags for issues where
                // the DB has now confirmed a real merge_queue entry (any state).
                // Once the row exists, merge_stage_status_for reads the real state.
                self.pipeline_inflight_merges.retain(|(_, issue_number)| {
                    !self
                        .data
                        .merge_queue
                        .iter()
                        .any(|m| m.issue_number == Some(*issue_number))
                });
                let m = self.data.machines.len();
                if m > 0 {
                    self.machine_sel = self.machine_sel.min(m - 1);
                } else {
                    self.machine_sel = 0;
                }
                // Update last-contact timestamps for machines whose health
                // fetch succeeded this cycle (version is Some).
                for machine in &self.data.machines {
                    if machine.version.is_some() {
                        self.machine_last_contact
                            .insert(machine.name.clone(), Instant::now());
                    }
                }
                self.rebuild_board_sidebar();
                // #1326: reconcile the Project/Workspace model against the
                // repos this tick just confirmed exist (drops stale repos,
                // derives a first-run default) — see `sync_workspace_repos`
                // in `app/workspace.rs`.
                self.sync_workspace_repos();
                // #2286 (ms-65 §6): same reconciliation for the persisted
                // doc-tab sets — drop any tab whose issue this tick doesn't
                // know about. See `Self::sync_doc_tabs`.
                self.sync_doc_tabs();
                // #486: the Pipeline now sources its issue list from the same
                // DB cache the Board uses (data.open_issues), rebuilt on every
                // data tick — no live gh search.  Capture the selection first so
                // replacing pipeline_issues doesn't jump the cursor to row 0.
                let prev_pl_sel = self.capture_pipeline_selection_id();
                self.pipeline_issues = self.pipeline_issues_from_cache();
                // Prune terminal sessions / spawn errors for issues no longer
                // in the pipeline or on the board (mirrors the old loader's cleanup).
                let mut live_keys: std::collections::HashSet<(String, u64)> = self
                    .pipeline_issues
                    .iter()
                    .map(|i| (i.repo_slug.clone(), i.number))
                    .collect();
                // #675 / follow-up: Board Terminal sessions must survive even when
                // their issue is visible on the Board ONLY via assignment history
                // (no open_issues record — coord sync hasn't run yet for this
                // repo, or the assignment was created directly via `coord assign`
                // before the first sync).  The d3dcd39 fix only iterated
                // data.open_issues, which misses assignment-only Board entries.
                // Use board_issues_cache instead — it was just rebuilt above by
                // rebuild_board_sidebar() and includes BOTH open_issues-based
                // entries AND assignment-only entries, exactly matching the full
                // set of issues visible on the Board.  A session is pruned only
                // when its issue is gone from BOTH the pipeline AND the board.
                for (repo, groups) in &self.board_issues_cache {
                    let slug = self
                        .data
                        .pipeline_repos
                        .iter()
                        .find(|(name, _)| name.as_str() == repo.as_str())
                        .map(|(_, s)| s.clone())
                        .unwrap_or_else(|| repo.clone());
                    for group in groups {
                        live_keys.insert((slug.clone(), group.issue_number));
                    }
                }
                // #620: never prune EVERY terminal at once.  The top-level
                // degraded-tick guard above catches a fully-empty payload, but a
                // *partial* degradation (machines/issues present yet
                // `pipeline_tracked_labels` momentarily empty from a board_meta
                // hiccup) still yields an empty `pipeline_issues` → empty
                // `live_keys` → the retain would drop every live embedded
                // terminal and bounce the operator.  An empty live set while we
                // still hold terminals is itself a near-certain degradation, so
                // skip the prune this tick; stale terminals are harmless and get
                // pruned on the next healthy tick (when live_keys is non-empty).
                let prune_terminals =
                    !live_keys.is_empty() || self.detail_terminal_sessions.is_empty();
                if prune_terminals {
                    self.detail_terminal_sessions.retain(|k, _| live_keys.contains(k));
                }
                self.detail_terminal_spawn_errors.retain(|k, _| live_keys.contains(k));
                self.rebuild_pipeline_sidebar(prev_pl_sel);

                // Ring the terminal bell (BEL) when an assignment that was
                // running is now done or failed, if the user enabled audio.
                if self.settings.audio_on_completion && !prev_running.is_empty() {
                    let newly_finished = self.data.assignments.iter().any(|a| {
                        (a.status == "done" || a.status == "failed") && prev_running.contains(&a.id)
                    });
                    if newly_finished {
                        // BEL character — rings the terminal bell or triggers
                        // a system notification depending on terminal settings.
                        eprint!("\x07");
                    }
                }
                self.audio_prev_running = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| a.status == "running")
                    .map(|a| a.id.clone())
                    .collect();

                // #349: Reset staleness-check tracking so the next tick re-runs
                // the git HEAD comparison.  New data may reflect new commits so
                // we want to re-check even for the same work assignment.
                self.test_plan_staleness_checked_for = None;

                // #349: Clear pending-spawn entries for work assignments whose
                // plan has now landed (test_plan is Some after the refresh).
                // Without this, the pending set would grow forever and the
                // "Preparing plan…" label might linger after the plan is ready.
                let newly_planned: Vec<String> = self
                    .test_plan_pending
                    .iter()
                    .filter(|id| {
                        self.data
                            .assignments
                            .iter()
                            .any(|a| &a.id == *id && a.test_plan.is_some())
                    })
                    .cloned()
                    .collect();
                for id in newly_planned {
                    self.test_plan_pending.remove(&id);
                }

                true
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => false,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                // Worker thread panicked or dropped sender without sending.
                self.pending_data = None;
                self.fetch_error = Some(("data load failed".into(), Instant::now()));
                true
            }
        }
    }

    /// Group assignments by `(repo, issue_number)`, returning repos in a
    /// stable order (repos with running issues first, then by name).
    fn issues_by_repo(&self) -> Vec<(String, Vec<IssueGroup>)> {
        use std::collections::{BTreeMap, HashMap};

        // Collect unique repos from machines, assignments, and open issues.
        let mut all_repos: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
        for m in &self.data.machines {
            for r in &m.repos {
                all_repos.insert(r.clone());
            }
        }
        for a in &self.data.assignments {
            all_repos.insert(a.repo.clone());
        }
        for oi in &self.data.open_issues {
            all_repos.insert(oi.repo_name.clone());
        }

        // Group assignments by (repo, issue_number).
        let mut repo_issues: HashMap<String, BTreeMap<u64, IssueGroup>> = HashMap::new();
        for a in &self.data.assignments {
            let group = repo_issues
                .entry(a.repo.clone())
                .or_default()
                .entry(a.issue_number)
                .or_insert_with(|| IssueGroup {
                    issue_number: a.issue_number,
                    issue_title: a.issue_title.clone(),
                    assignments: Vec::new(),
                    status_summary: String::new(),
                    is_closed: false,
                    has_open_record: false,
                    labels: Vec::new(),
                    milestone_number: None,
                    milestone_title: None,
                });
            group.assignments.push(a.clone());
        }

        // Derive status_summary for each issue group.
        for groups in repo_issues.values_mut() {
            for group in groups.values_mut() {
                // Sort assignments by dispatched_at ascending.
                group.assignments.sort_by(|a, b| {
                    a.dispatched_at
                        .partial_cmp(&b.dispatched_at)
                        .unwrap_or(std::cmp::Ordering::Equal)
                });

                // Check merge queue for this issue.
                let merged = self
                    .data
                    .merge_queue
                    .iter()
                    .any(|e| e.issue_number == Some(group.issue_number) && e.state == "merged");

                if merged && group.assignments.iter().all(|a| a.status == "done") {
                    group.status_summary = "merged".to_string();
                } else if group.assignments.iter().any(|a| a.status == "running") {
                    group.status_summary = "running".to_string();
                } else if group
                    .assignments
                    .last()
                    .map(|a| a.status == "failed")
                    .unwrap_or(false)
                {
                    group.status_summary = "failed".to_string();
                } else if group.assignments.iter().all(|a| a.status == "done") {
                    group.status_summary = "done".to_string();
                } else {
                    group.status_summary = "pending".to_string();
                }
            }
        }

        // #265 / #257 fix: stamp both `is_closed` (cache says closed)
        // and `has_open_record` (cache says open) on every IssueGroup
        // that came from assignments.  Groups whose issue has no cache
        // row stay with both flags `false` — the bucketing treats that
        // as "brain has forgotten about this issue", which is the
        // typical state for historical merged/done assignments whose
        // rows got pruned (the brain only retains closed rows for 7d).
        //
        // #226: also copy the issue's labels onto the group so the
        // Backlog/Refining/Refined classification has data to read.
        for oi in self.data.open_issues.iter() {
            if let Some(groups) = repo_issues.get_mut(&oi.repo_name) {
                if let Some(group) = groups.get_mut(&oi.number) {
                    match oi.state.as_str() {
                        "closed" => group.is_closed = true,
                        "open" => group.has_open_record = true,
                        _ => {}
                    }
                    if group.labels.is_empty() {
                        group.labels = oi.labels.clone();
                    }
                    // #406: copy milestone from the issues cache onto the group.
                    // The open_issues row is always the authority — overwrite even
                    // if a previous assignment had already stamped a value.
                    group.milestone_number = oi.milestone_number;
                    group.milestone_title = oi.milestone_title.clone();
                }
            }
        }

        // Inject open issues with no assignment as Pending entries.
        // `data.open_issues` now also carries closed issues (for the Board
        // Issue tab's body lookup), so we must filter to state="open" here
        // or closed issues would appear as Pending rows.
        for oi in self.data.open_issues.iter().filter(|i| i.state == "open") {
            let entry = repo_issues
                .entry(oi.repo_name.clone())
                .or_default()
                .entry(oi.number);
            // Only insert if there's no existing assignment group for this issue.
            entry.or_insert_with(|| IssueGroup {
                issue_number: oi.number,
                issue_title: oi.title.clone(),
                assignments: Vec::new(),
                status_summary: "pending".to_string(),
                is_closed: false,
                has_open_record: true,
                labels: oi.labels.clone(),
                milestone_number: oi.milestone_number,
                milestone_title: oi.milestone_title.clone(),
            });
        }

        // Build result: each repo with its issues, sorted.
        let mut result: Vec<(String, Vec<IssueGroup>)> = Vec::new();
        for repo in &all_repos {
            let issues = repo_issues
                .remove(repo)
                .map(|m| {
                    let mut v: Vec<IssueGroup> = m.into_values().collect();
                    // Issues with running status first, then by issue number.
                    v.sort_by(|a, b| {
                        let rank = |s: &str| match s {
                            "running" => 0u8,
                            "failed" => 1,
                            "pending" => 2,
                            "done" => 3,
                            "merged" => 4,
                            _ => 5,
                        };
                        rank(&a.status_summary)
                            .cmp(&rank(&b.status_summary))
                            .then_with(|| a.issue_number.cmp(&b.issue_number))
                    });
                    v
                })
                .unwrap_or_default();
            result.push((repo.clone(), issues));
        }

        // Sort repos: those with running/failed issues first.
        result.sort_by(|a, b| {
            let has_active = |issues: &[IssueGroup]| -> u8 {
                if issues.iter().any(|i| i.status_summary == "running") {
                    0
                } else if issues.iter().any(|i| i.status_summary == "failed") {
                    1
                } else if issues.is_empty() {
                    3
                } else {
                    2
                }
            };
            has_active(&a.1)
                .cmp(&has_active(&b.1))
                .then_with(|| a.0.cmp(&b.0))
        });

        result
    }

    /// #406: Group a repo's issues by milestone, then by lifecycle status within
    /// each milestone group.
    ///
    /// Returns `Vec<(milestone_key, display_title, status_groups)>` where:
    /// - `milestone_key` — `"<n>"` for numbered milestones (e.g. `"5"`), or
    ///   `"no-milestone"` for unassigned issues.
    /// - `display_title` — the milestone title (e.g. `"v0.5"`) or
    ///   `"No milestone"`.
    /// - `status_groups` — `Vec<(display_name, key, issues)>` in display order
    ///   (Backlog → Refining → Refined → In-flight → Completed), with empty
    ///   groups omitted and the `board_search` filter applied.
    ///
    /// Sorted: named milestones by number ASC, `"No milestone"` last.
    ///
    /// Production rendering uses `board_milestones_for_repo` (flat, no status
    /// sub-groups).  This function is retained for tests that verify the
    /// status-bucketing logic.
    #[allow(dead_code)]
    fn board_milestones_with_status_for_repo<'a>(
        &'a self,
        issues: &'a [(String, Vec<IssueGroup>)],
        repo: &str,
    ) -> Vec<(
        String,
        String,
        Vec<(&'static str, &'static str, Vec<(usize, &'a IssueGroup)>)>,
    )> {
        let flat: &[IssueGroup] = match issues.iter().find(|(r, _)| r == repo) {
            Some((_, v)) => v,
            None => return Vec::new(),
        };

        // Bucket flat indices into milestones.
        let mut milestone_map: std::collections::BTreeMap<
            (i64, String), // (number, title) for sorting; number=i64::MAX for "no-milestone"
            (String, String, Vec<usize>), // (key, display_title, flat_indices)
        > = std::collections::BTreeMap::new();

        for (flat_idx, g) in flat.iter().enumerate() {
            if !self.board_search.matches(g.issue_number, &g.issue_title) {
                continue;
            }
            match g.milestone_number {
                Some(n) => {
                    let title = g.milestone_title.clone().unwrap_or_default();
                    let key = n.to_string();
                    let sort_key = (n, title.clone());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| (key, title, Vec::new()))
                        .2
                        .push(flat_idx);
                }
                None => {
                    let sort_key = (i64::MAX, String::new());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| {
                            (
                                "no-milestone".to_string(),
                                "No milestone".to_string(),
                                Vec::new(),
                            )
                        })
                        .2
                        .push(flat_idx);
                }
            }
        }

        // For each milestone, build the status sub-groups.
        let mut result = Vec::new();
        for (_, (key, display_title, flat_indices)) in milestone_map {
            let mut backlog: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut refining: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut refined: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut in_flight: Vec<(usize, &IssueGroup)> = Vec::new();
            let mut completed: Vec<(usize, &IssueGroup)> = Vec::new();
            for fi in &flat_indices {
                let g = &flat[*fi];
                match g.lifecycle_section() {
                    "backlog" => backlog.push((*fi, g)),
                    "refining" => refining.push((*fi, g)),
                    "refined" => refined.push((*fi, g)),
                    "in-flight" => in_flight.push((*fi, g)),
                    "completed" => completed.push((*fi, g)),
                    _ => backlog.push((*fi, g)),
                }
            }
            let status_groups: Vec<(&'static str, &'static str, Vec<(usize, &IssueGroup)>)> = [
                ("Backlog", "backlog", backlog),
                ("Refining", "refining", refining),
                ("Refined", "refined", refined),
                ("In-flight", "in-flight", in_flight),
                ("Completed", "completed", completed),
            ]
            .into_iter()
            .filter(|(_, _, v)| !v.is_empty())
            .collect();

            if !status_groups.is_empty() {
                result.push((key, display_title, status_groups));
            }
        }
        result
    }

    /// #857: Whether a board milestone group has any in-flight (dispatched,
    /// not-yet-done) work — the sole exception to "collapsed by default".
    /// Shared by the render path (`rebuild_board_sidebar`) and the click
    /// toggle handlers (`events.rs`) so a first click always inverts the
    /// same default the row was actually painted with.
    fn board_milestone_has_inflight(group_issues: &[(usize, &IssueGroup)]) -> bool {
        group_issues
            .iter()
            .any(|(_, g)| g.lifecycle_section() == "in-flight")
    }

    /// #410: Group a repo's issues by milestone only (no status sub-grouping).
    ///
    /// Returns `Vec<(milestone_key, display_title, issues)>` where:
    /// - `milestone_key` — `"<n>"` for numbered milestones (e.g. `"5"`), or
    ///   `"no-milestone"` for unassigned issues.
    /// - `display_title` — milestone title or `"No milestone"`.
    /// - `issues` — `Vec<(flat_idx, &IssueGroup)>` in board order, with the
    ///   `board_search` filter applied when `apply_search` is `true`.
    ///
    /// Sorted: named milestones by number ASC, `"No milestone"` last.
    ///
    /// #2452: `apply_search` exists for exactly one caller —
    /// `board_selected_issue_unfiltered` — see its doc comment for why a
    /// stored tree path can only be safely re-resolved against the SAME
    /// (unfiltered) coordinate space it was set against, never against
    /// whatever `board_search` happens to hold NOW. Every other call site
    /// passes `true`, preserving this function's exact prior behaviour.
    fn board_milestones_for_repo<'a>(
        &'a self,
        issues: &'a [(String, Vec<IssueGroup>)],
        repo: &str,
        apply_search: bool,
    ) -> Vec<(String, String, Vec<(usize, &'a IssueGroup)>)> {
        let flat: &[IssueGroup] = match issues.iter().find(|(r, _)| r == repo) {
            Some((_, v)) => v,
            None => return Vec::new(),
        };

        let mut milestone_map: std::collections::BTreeMap<
            (i64, String),
            (String, String, Vec<usize>),
        > = std::collections::BTreeMap::new();

        for (flat_idx, g) in flat.iter().enumerate() {
            if apply_search && !self.board_search.matches(g.issue_number, &g.issue_title) {
                continue;
            }
            match g.milestone_number {
                Some(n) => {
                    let title = g.milestone_title.clone().unwrap_or_default();
                    let key = n.to_string();
                    let sort_key = (n, title.clone());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| (key, title, Vec::new()))
                        .2
                        .push(flat_idx);
                }
                None => {
                    let sort_key = (i64::MAX, String::new());
                    milestone_map
                        .entry(sort_key)
                        .or_insert_with(|| {
                            (
                                "no-milestone".to_string(),
                                "No milestone".to_string(),
                                Vec::new(),
                            )
                        })
                        .2
                        .push(flat_idx);
                }
            }
        }

        milestone_map
            .into_values()
            .filter(|(_, _, idxs)| !idxs.is_empty())
            .map(|(key, display_title, idxs)| {
                let group_issues: Vec<(usize, &IssueGroup)> =
                    idxs.into_iter().map(|fi| (fi, &flat[fi])).collect();
                (key, display_title, group_issues)
            })
            .collect()
    }

    /// #1270: every child issue nested under some epic anywhere on the
    /// Board (mirrors `pipeline_globally_nested_children`) — used to
    /// suppress the flat top-level row for a child so it renders only once,
    /// nested under its parent epic's row, instead of a second time as an
    /// ordinary sibling in its own milestone bucket.
    ///
    /// Applies the `board_search` filter to the *parent* before
    /// contributing its children — same #1253-review rationale as the
    /// Pipeline version: an epic that's filtered out of view shouldn't
    /// still suppress a child that DOES match the search, which would
    /// otherwise vanish from the sidebar entirely (nested under a row that
    /// isn't rendered, and skipped as "already nested" everywhere else).
    fn board_globally_nested_children(&self) -> std::collections::HashSet<(String, u64)> {
        let mut nested = std::collections::HashSet::new();
        for (repo, issues) in &self.board_issues_cache {
            for g in issues {
                if !self.board_search.matches(g.issue_number, &g.issue_title) {
                    continue;
                }
                let Some(children) = self.epic_children_for_repo_issue(repo, g.issue_number)
                else {
                    continue;
                };
                for child in children {
                    nested.insert((repo.clone(), child.number));
                }
            }
        }
        nested
    }

    /// #1270: true when `g` (in `repo`) is nested under some epic elsewhere
    /// on the Board (see `board_globally_nested_children`) and should
    /// therefore be skipped when emitting a flat top-level row.
    fn board_is_globally_nested(
        repo: &str,
        g: &IssueGroup,
        globally_nested: &std::collections::HashSet<(String, u64)>,
    ) -> bool {
        globally_nested.contains(&(repo.to_string(), g.issue_number))
    }

    /// #1270: maps every child issue number (repo-scoped) to its parent
    /// epic's issue number, across the whole Board — the Kanban
    /// counterpart of `board_globally_nested_children`'s suppression set.
    /// Unlike that set, this is NOT `board_search`-filtered: Kanban builds
    /// straight from `board_issues_cache` with no search gate of its own
    /// (`build_kanban_model` doesn't filter today), so filtering here would
    /// just silently drop attribution with nothing else picking up the
    /// slack.
    fn board_epic_child_parents(&self) -> std::collections::HashMap<(String, u64), u64> {
        let mut parents = std::collections::HashMap::new();
        for (repo, issues) in &self.board_issues_cache {
            for g in issues {
                let Some(children) = self.epic_children_for_repo_issue(repo, g.issue_number)
                else {
                    continue;
                };
                for child in children {
                    parents.insert((repo.clone(), child.number), g.issue_number);
                }
            }
        }
        parents
    }

    /// Rebuild the SidebarSystem from current data.
    ///
    /// Layout:
    /// - Section 0: search form (always present)
    /// - Section 1: PROPOSALS (only when proposals exist)
    /// - Section 1/2+: one section per repo
    ///
    /// Within each repo section, issues are grouped by milestone then issue
    /// (Repo > Milestone > Issue).  Each issue row carries a trailing
    /// status letter (R/A/D) as a right-aligned badge.  Empty groups are
    /// omitted.  Rows are filtered by `board_search` (case-insensitive
    /// substring).
    fn rebuild_board_sidebar(&mut self) {
        self.board_issues_cache = self.issues_by_repo();
        let grouped = &self.board_issues_cache;

        // #2452: `board_selected_issue_unfiltered`, not `board_selected_
        // issue` — every FILTER keystroke handler mutates `board_search`
        // BEFORE calling this rebuild, so the OLD sidebar's stored path
        // must be resolved against the UNFILTERED grouping to name the
        // issue it actually pointed at; see that method's doc comment for
        // the silent-wrong-issue failure mode this avoids.
        let prev_selection = self.board_selected_issue_unfiltered();
        let prev_panel_scroll = self.board_sidebar.panel_scroll();

        // Save per-section collapse state keyed by section name.
        let search_offset = 1usize; // always one search section
        let prev_collapsed: std::collections::HashMap<String, bool> = {
            let offset = search_offset + if self.has_proposals_section { 1 } else { 0 };
            let mut map = std::collections::HashMap::new();
            if self.has_proposals_section {
                map.insert(
                    "__proposals__".to_string(),
                    self.board_sidebar.is_collapsed(search_offset),
                );
            }
            let old_names: Vec<String> = self.board_repo_names.clone();
            for (i, name) in old_names.into_iter().enumerate() {
                map.insert(name, self.board_sidebar.is_collapsed(i + offset));
            }
            map
        };

        // #192: the PROPOSALS section is retired.  Right-click → Send
        // to Pipeline (#261) is the canonical path from Refined →
        // Pipeline:New; `coord plan` proposals are a parallel system
        // we no longer surface.  Force the flag false so the section
        // never renders and all the offset arithmetic still adds 0.
        // The `coord plan` CLI keeps working for backwards-compat;
        // its output just doesn't appear in the TUI any more.
        self.has_proposals_section = false;
        let mut defs: Vec<SidebarSectionDef> = Vec::new();

        // Section 0: search/filter form.
        defs.push(SidebarSectionDef::form("board-search", "FILTER"));

        if self.has_proposals_section {
            let mut def =
                SidebarSectionDef::new("section:proposals".to_string(), "PROPOSALS".to_string());
            def.show_chevron = true;
            def.size = SectionSize::Content;
            defs.push(def);
        }

        for (repo, _) in grouped.iter() {
            let mut def = SidebarSectionDef::new(format!("repo:{}", repo), repo.clone());
            def.show_chevron = true;
            def.size = SectionSize::Content;
            defs.push(def);
        }

        self.board_repo_names = grouped.iter().map(|(r, _)| r.clone()).collect();

        // #2282: captured before `defs` is moved into the new SidebarSystem —
        // `board_section_rows` is indexed by section, so it has to cover every
        // section, form sections included.
        let defs_len = defs.len();
        self.board_sidebar = SidebarSystem::new(defs);
        self.board_sidebar
            .set_navigation_mode(NavigationMode::Selection);
        self.board_sidebar.set_allow_collapse(true);
        self.board_sidebar.set_scroll_mode(ScrollMode::WholePanel);

        // Populate search form (section 0).
        self.board_sidebar
            .set_form(
                0,
                self.board_search
                    .form("board-search", "Filter issues… (#4 = number only)"),
            );

        let offset = search_offset + if self.has_proposals_section { 1 } else { 0 };

        // Populate PROPOSALS section.
        if self.has_proposals_section {
            let proposal_color = Color::rgb(200, 180, 255);
            let rows: Vec<TreeRow> = self
                .data
                .proposals
                .iter()
                .enumerate()
                .map(|(i, p)| {
                    let text = StyledText {
                        spans: vec![
                            StyledSpan::with_fg(format!("[{}] ", p.id), Color::rgb(180, 180, 220)),
                            StyledSpan::with_fg(
                                format!("{} ", p.machine),
                                Color::rgb(140, 200, 140),
                            ),
                            StyledSpan::with_fg(
                                format!("#{} ", p.issue_number),
                                Color::rgb(150, 150, 240),
                            ),
                            StyledSpan::plain(trunc(&p.issue_title, 18)),
                        ],
                    };
                    TreeRow {
                        path: vec![i as u16],
                        indent: 0,
                        icon: None,
                        text,
                        badge: Some(Badge::colored(&p.proposal_type, proposal_color)),
                        is_expanded: None,
                        decoration: Decoration::Normal,
                        edit: None,
                    }
                })
                .collect();
            self.board_sidebar.set_rows(search_offset, rows);
            self.board_sidebar.set_section_badge(
                search_offset,
                Some(StyledText::plain(format!(
                    "({})",
                    self.data.proposals.len()
                ))),
            );
        }

        // #1270: every child issue nested under some epic, computed once up
        // front (owned data — no borrow held across the loop below) so the
        // row-emission loop can skip a child's flat sibling row wherever it
        // would otherwise render, mirroring `pipeline_globally_nested_children`.
        let globally_nested = self.board_globally_nested_children();
        // #1270: rendered epic rows' `(section_idx, path)` → `board_epic_expanded`
        // key, collected across every repo section then assigned to
        // `self.board_epic_row_keys` once the loop below releases its &self
        // borrow (mirrors Pipeline's `epic_row_keys` local-then-assign pattern).
        let mut epic_row_keys: std::collections::HashMap<(usize, TreePath), (String, u64)> =
            std::collections::HashMap::new();

        // #2282 (ms-65 §2f): the issue whose document tab is active. Its
        // sidebar row carries the `▸` marker so "which document am I looking
        // at" is legible in the tree, not only in the tab strip. `None` when
        // no tab is open, which is what keeps the zero-tab render byte-identical
        // to pre-ms-65 Board.
        let active_doc: Option<DocKey> = self.board_doc_tabs().active_key().cloned();
        // #2282 (ms-65 §2f): per-section row shape, for reveal-on-activate's
        // offset arithmetic. Grown to `defs.len()` up front so a Form section
        // (the FILTER box, section 0) still occupies its slot.
        let mut section_rows: Vec<Vec<(TreePath, bool)>> = vec![Vec::new(); defs_len];

        // #410: Build per-repo milestone > issue rows (status sub-group removed).
        //
        // Two-phase loop to satisfy the borrow checker:
        // Phase 1 — inside the block, compute `(total, rows)` using
        //   `board_milestones_for_repo` (borrows &self).
        //   `milestones` is dropped at the end of the block.
        // Phase 2 — outside the block, apply mutations to `self.board_sidebar`
        //   (requires &mut self, which is safe once milestones is dropped).
        for (cache_idx, (repo, _issues)) in grouped.iter().enumerate() {
            let section_idx = cache_idx + offset;

            let (total, mut rows): (usize, Vec<TreeRow>) = {
                // Compute milestone-grouped data for this repo.
                // milestones holds &IssueGroup refs; it must be dropped before
                // any &mut self borrow (sidebar mutations) below.
                let milestones = self.board_milestones_for_repo(grouped, repo, true);

                let total: usize = milestones.iter().map(|(_, _, v)| v.len()).sum();

                let mut rows: Vec<TreeRow> = Vec::new();

                for (milestone_idx, (m_key, m_display, group_issues)) in
                    milestones.iter().enumerate()
                {
                    let mi = milestone_idx as u16;

                    let m_has_inflight = Self::board_milestone_has_inflight(group_issues);

                    // #857: default to collapsed (milestones-first view) so the
                    // long tail doesn't bury the user in issue rows on open;
                    // in-flight milestones stay expanded by default so active
                    // work remains visible without an extra click.
                    let m_is_exp = *self
                        .board_milestone_expanded
                        .get(&(repo.clone(), m_key.clone()))
                        .unwrap_or(&m_has_inflight);

                    // Milestone header color.
                    let m_header_color = if m_has_inflight {
                        Color::rgb(200, 200, 100)
                    } else if m_key == "no-milestone" {
                        Color::rgb(100, 100, 120)
                    } else {
                        Color::rgb(160, 160, 200)
                    };

                    rows.push(TreeRow {
                        path: vec![mi],
                        indent: 1,
                        icon: None,
                        text: StyledText {
                            spans: vec![StyledSpan::with_fg(
                                format!("{} ({})", m_display, group_issues.len()),
                                m_header_color,
                            )],
                        },
                        badge: None,
                        is_expanded: Some(m_is_exp),
                        decoration: Decoration::Header,
                        edit: None,
                    });

                    if m_is_exp {
                        for (issue_idx, (_flat_idx, g)) in group_issues.iter().enumerate() {
                            // #1270: a child nested under some epic elsewhere
                            // on the Board renders once, under that epic's
                            // row — skip the flat sibling here.
                            if Self::board_is_globally_nested(repo, g, &globally_nested) {
                                continue;
                            }
                            // #2282 (ms-65 §2f): mark the row whose document
                            // tab is active. `▸` (U+25B8) is the milestone's
                            // pinned marker — distinct from `▶` (the Pipeline
                            // activity-bar icon) and from the doc-tab strip's
                            // `[...]` active-tab bracket.
                            let is_active_doc = active_doc
                                .as_ref()
                                .is_some_and(|(r, n)| r == repo && *n == g.issue_number);
                            let mut spans = Vec::new();
                            if is_active_doc {
                                spans.push(StyledSpan::with_fg(
                                    "▸ ".to_string(),
                                    Color::rgb(220, 220, 120),
                                ));
                            }
                            spans.push(StyledSpan::with_fg(
                                format!("#{:<5}", g.issue_number),
                                Color::rgb(150, 150, 240),
                            ));
                            // #1270: epic marker, label-driven — see
                            // `epic_badge_span_for_labels` (shared with the
                            // Pipeline panel's `epic_badge_span`). Spliced
                            // between `#N` and the title, same placement
                            // rationale as the Pipeline row.
                            if let Some(marker) = epic_badge_span_for_labels(&g.labels) {
                                spans.push(marker);
                            }
                            spans.push(StyledSpan::plain(trunc(&g.issue_title, 20)));

                            // #1270: an epic row is a branch — nest its
                            // `## Sub-issues` children beneath it instead of
                            // letting them render a second time as flat
                            // siblings elsewhere (suppressed above via
                            // `globally_nested`). Reuses the exact same
                            // DAG-state computation and row-building the
                            // Pipeline panel uses, per this issue's "Board
                            // and Pipeline don't drift" ask.
                            let children = self.epic_child_rows_for(repo, g.issue_number);
                            let epic = if children.is_empty() {
                                None
                            } else {
                                let key = (repo.clone(), g.issue_number);
                                let expanded = Self::epic_expand_state_in(
                                    &key,
                                    &children,
                                    &self.board_epic_expanded,
                                );
                                // "N/M done" child-progress summary, visible
                                // whether expanded or collapsed.
                                if let Some(span) = Self::epic_progress_span(&children) {
                                    spans.push(span);
                                }
                                Some((key, expanded, children))
                            };

                            // #410: per-row status letter badge (R/A/D).
                            let badge = board_row_status_badge(g.lifecycle_section());
                            let row_path = vec![mi, issue_idx as u16];
                            rows.push(TreeRow {
                                path: row_path.clone(),
                                indent: 2,
                                icon: None,
                                text: StyledText { spans },
                                badge,
                                is_expanded: epic.as_ref().map(|(_, e, _)| *e),
                                decoration: if g.status_summary == "failed" {
                                    Decoration::Error
                                } else {
                                    Decoration::Normal
                                },
                                edit: None,
                            });
                            if let Some((key, expanded, children)) = epic {
                                epic_row_keys.insert((section_idx, row_path.clone()), key);
                                if expanded {
                                    for (ci, child) in children.iter().enumerate() {
                                        rows.push(self.epic_child_tree_row(
                                            &row_path, 3, ci, child,
                                        ));
                                    }
                                }
                            }
                        }
                    }
                }
                // milestones dropped here — &self borrow released.
                (total, rows)
            };

            // #2282 (ms-65 §2f): reveal-on-activate tree windowing. When
            // `reveal_board_active_doc` decided this repo's tree must start
            // part-way down for the active document's row to fit the
            // viewport, the hidden leading rows are collapsed into a single
            // `⋮ N more above` marker row — the whole panel stays unscrolled
            // so the FILTER box keeps its pinned spot at the top (§2f).
            // Clamped so the marker never swallows the entire tree; the kept
            // rows retain their original `TreePath`s, so selection and click
            // routing are untouched. The marker's `u16::MAX` path can never
            // collide with a real milestone index, and every handler that
            // resolves paths through `board_milestones_for_repo` treats the
            // failed lookup as a no-op.
            let hidden = self
                .board_tree_hidden_above
                .get(repo)
                .copied()
                .unwrap_or(0)
                .min(rows.len().saturating_sub(1));
            if hidden > 0 {
                rows.drain(..hidden);
                rows.insert(
                    0,
                    TreeRow {
                        path: vec![u16::MAX],
                        indent: 1,
                        icon: None,
                        text: StyledText {
                            spans: vec![StyledSpan::with_fg(
                                format!("⋮ {hidden} more above"),
                                Color::rgb(120, 120, 140),
                            )],
                        },
                        badge: None,
                        is_expanded: None,
                        decoration: Decoration::Normal,
                        edit: None,
                    },
                );
            }

            // Phase 2: apply mutations now that milestones is dropped.
            if total > 0 {
                self.board_sidebar.set_section_badge(
                    section_idx,
                    Some(StyledText::plain(format!("({})", total))),
                );
            }
            if total == 0 {
                self.board_sidebar.set_collapsed(section_idx, true);
            }
            // #2282 (ms-65 §2f): remember this section's row shape before the
            // rows are handed to the sidebar (which offers no way to read them
            // back) so reveal-on-activate can work out a row's panel offset.
            if let Some(slot) = section_rows.get_mut(section_idx) {
                *slot = rows
                    .iter()
                    .map(|r| (r.path.clone(), matches!(r.decoration, Decoration::Header)))
                    .collect();
            }
            self.board_sidebar.set_rows(section_idx, rows);
        }
        self.board_epic_row_keys = epic_row_keys;
        self.board_section_rows = section_rows;

        // Activate first non-empty repo section.
        if self.board_sidebar.active_section().is_none() {
            if self.has_proposals_section {
                self.board_sidebar.set_active_section(Some(search_offset));
            } else {
                for (i, (_repo, issues)) in grouped.iter().enumerate() {
                    if !issues.is_empty() {
                        self.board_sidebar.set_active_section(Some(i + offset));
                        break;
                    }
                }
            }
        }

        // Restore previous selection.
        if let Some((prev_repo, prev_issue)) = prev_selection {
            self.select_issue(&prev_repo, prev_issue);
        }

        self.board_sidebar.set_panel_scroll(prev_panel_scroll);

        // Restore collapsed state by section name.
        {
            let new_offset = search_offset + if self.has_proposals_section { 1 } else { 0 };
            if self.has_proposals_section {
                if let Some(&was_collapsed) = prev_collapsed.get("__proposals__") {
                    self.board_sidebar
                        .set_collapsed(search_offset, was_collapsed);
                }
            }
            let new_names: Vec<String> = self.board_repo_names.clone();
            for (i, name) in new_names.into_iter().enumerate() {
                if let Some(&was_collapsed) = prev_collapsed.get(&name) {
                    self.board_sidebar
                        .set_collapsed(i + new_offset, was_collapsed);
                }
            }
        }
        // Re-apply filter focus after rebuild so the cursor survives auto-refresh.
        if self.board_search.focused {
            self.board_sidebar.focus_form(0, true);
        }

        // #638: Keep Kanban model in sync with new board data.
        let new_kanban = self.build_kanban_model();
        let old_sel = self.kanban_model.selected_card_id.clone();
        let old_col_scroll = self.kanban_model.col_scroll_offset;
        // Preserve per-column vertical scroll offsets so a background data
        // refresh does not snap the user's view back to the top (review issue 3).
        let old_vert_offsets: Vec<(WidgetId, usize)> = self.kanban_model.columns
            .iter()
            .map(|c| (c.id.clone(), c.scroll_offset))
            .collect();
        self.kanban_model = new_kanban;
        if let Some(sel) = &old_sel {
            let still_exists = self.kanban_model.columns.iter()
                .any(|c| c.cards.iter().any(|card| &card.id == sel));
            if still_exists {
                self.kanban_model.selected_card_id = old_sel;
            }
        }
        self.kanban_model.col_scroll_offset = old_col_scroll;
        // Restore per-column vertical scroll offsets.
        for col in &mut self.kanban_model.columns {
            if let Some((_, offset)) = old_vert_offsets.iter().find(|(id, _)| id == &col.id) {
                col.scroll_offset = *offset;
            }
        }
    }

    /// Repo section offset: 1 for the search form + 1 more if proposals exist.
    fn board_repo_offset(&self) -> usize {
        1 + if self.has_proposals_section { 1 } else { 0 }
    }

    /// #638: Build a fresh `BoardModel` from the current `board_issues_cache`.
    ///
    /// Three columns: **Backlog** (backlog + refining + refined),
    /// **In Flight** (in-flight), **Completed** (completed).
    /// Each issue group becomes one card; its repo name is prepended as a label.
    fn build_kanban_model(&self) -> BoardModel {
        let mut backlog_cards: Vec<BoardCard> = Vec::new();
        let mut inflight_cards: Vec<BoardCard> = Vec::new();
        let mut completed_cards: Vec<BoardCard> = Vec::new();

        // #1270: a card's containing column is still driven solely by its
        // OWN lifecycle status (Backlog / In Flight / Completed stay exactly
        // as before) — an epic and its children can genuinely land in
        // different columns. What changes is *labelling*: the epic card
        // gets the same "◆EPIC" marker the Pipeline/Board panel use, and
        // every child card gets a `hint` naming its parent epic, so
        // the two no longer read as unrelated loose cards scattered across
        // columns with zero visual link between them.
        let epic_parents = self.board_epic_child_parents();

        for (repo, groups) in &self.board_issues_cache {
            for g in groups {
                let card_id = WidgetId::new(&format!("card:{}:{}", repo, g.issue_number));
                let badges = kanban_stage_badges(g);
                let mut labels = vec![repo.clone()];
                if labels_carry_epic_label(&g.labels) {
                    labels.push("◆EPIC".to_string());
                }
                let hint = epic_parents
                    .get(&(repo.clone(), g.issue_number))
                    .map(|epic_number| format!("↳ part of epic #{epic_number}"));
                let card = BoardCard {
                    id: card_id,
                    title: format!("#{} {}", g.issue_number, g.issue_title),
                    labels,
                    badges,
                    hint,
                };
                match g.lifecycle_section() {
                    "in-flight" => inflight_cards.push(card),
                    "completed" => completed_cards.push(card),
                    _ => backlog_cards.push(card), // backlog / refining / refined
                }
            }
        }

        BoardModel {
            id: WidgetId::new("kanban:coord"),
            columns: vec![
                BoardColumn {
                    id: WidgetId::new("kanban-col:backlog"),
                    title: "Backlog".to_string(),
                    cards: backlog_cards,
                    scroll_offset: 0,
                },
                BoardColumn {
                    id: WidgetId::new("kanban-col:inflight"),
                    title: "In Flight".to_string(),
                    cards: inflight_cards,
                    scroll_offset: 0,
                },
                BoardColumn {
                    id: WidgetId::new("kanban-col:completed"),
                    title: "Completed".to_string(),
                    cards: completed_cards,
                    scroll_offset: 0,
                },
            ],
            selected_card_id: None,
            col_scroll_offset: 0,
        }
    }

    /// Clamp the focused column's scroll offset so the selected card is visible.
    fn kanban_clamp_col_scroll(&mut self) {
        // Compute the selected card's (col_index, card_index) before the
        // mutable borrow of kanban_model.columns below.  We replicate the
        // logic of quadraui's private `BoardModel::selected_position()`.
        let sel_pos: (Option<usize>, Option<usize>) = {
            let mut result = (None, None);
            if let Some(id) = self.kanban_model.selected_card_id.as_ref() {
                'outer: for (ci, col) in self.kanban_model.columns.iter().enumerate() {
                    for (ri, c) in col.cards.iter().enumerate() {
                        if &c.id == id {
                            result = (Some(ci), Some(ri));
                            break 'outer;
                        }
                    }
                }
            }
            result
        };
        let (sel_col, sel_card_idx) = sel_pos;

        if let Some(layout) = self.kanban_layout.borrow().as_ref() {
            for col_layout in &layout.columns {
                let ci = col_layout.col_index;
                let col = &mut self.kanban_model.columns[ci];
                let visible = col_layout.visible_cards.max(1);
                let total = col.cards.len();
                if total == 0 {
                    col.scroll_offset = 0;
                    continue;
                }
                // Scroll-follow: adjust the window so the selected card stays
                // visible.  Only applies to the column that owns the selection.
                if sel_col == Some(ci) {
                    if let Some(idx) = sel_card_idx {
                        if idx < col.scroll_offset {
                            // Selection scrolled above the top of the window.
                            col.scroll_offset = idx;
                        } else if idx >= col.scroll_offset + visible {
                            // Selection scrolled below the bottom of the window.
                            col.scroll_offset = (idx + 1).saturating_sub(visible);
                        }
                    }
                }
                // Upper-bound clamp so we never show empty rows at the bottom.
                col.scroll_offset = col.scroll_offset.min(total.saturating_sub(visible));
            }
        }
    }

    /// Open the Board view and select the issue represented by a Kanban card id.
    fn kanban_open_card(&mut self, card_id: &WidgetId) {
        // card id format: "card:<repo>:<issue_number>"
        let s = card_id.as_str();
        if let Some(colon) = s.rfind(':') {
            let right = &s[colon + 1..];
            if let Ok(num) = right.parse::<u64>() {
                let repo_part = &s[..colon];
                if let Some(repo_colon) = repo_part.rfind(':') {
                    let repo = &repo_part[repo_colon + 1..];
                    // #1029 bug A: keep the ActivityBar/header chrome in sync too.
                    self.switch_active_view(SidebarView::Board);
                    self.select_issue(repo, num);
                }
            }
        }
    }

    /// Empty placeholder list for the Kanban sidebar slot.
    fn kanban_sidebar_placeholder(&self) -> ListView {
        ListView {
            id: WidgetId::new("kanban-sidebar"),
            title: None,
            items: vec![],
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Return the repo name for the active sidebar section, if any.
    fn board_active_repo(&self) -> Option<&str> {
        let section = self.board_sidebar.active_section()?;
        let offset = self.board_repo_offset();
        if section < offset {
            return None;
        }
        self.board_repo_names
            .get(section - offset)
            .map(|s| s.as_str())
    }

    /// Reconstruct the lifecycle groups for a repo using the current
    /// search filter.  Returns `(key, [(flat_idx, &IssueGroup)…])` in
    /// display order: **Backlog → Refining → Refined → In-flight → Completed**.
    ///
    /// Section rules (per #256 / #226 lifecycle model):
    /// - **Backlog**   — open, no assignments, no `status:*` label
    /// - **Refining**  — open, no assignments, `status:refining` label
    /// - **Refined**   — open, no assignments, `status:ready` label
    /// - **In-flight** — open AND has at least one assignment
    /// - **Completed** — closed AND has at least one assignment
    ///
    /// Empty groups are omitted so the sidebar doesn't show stub headers.
    ///
    /// Used by tests.  Production rendering goes through
    /// `board_milestones_for_repo` instead.
    #[allow(dead_code)]
    fn board_grouped_for_repo<'a>(
        &'a self,
        issues: &'a [(String, Vec<IssueGroup>)],
        repo: &str,
    ) -> Vec<(&'static str, Vec<(usize, &'a IssueGroup)>)> {
        let (_, flat) = match issues.iter().find(|(r, _)| r == repo) {
            Some(v) => v,
            None => return Vec::new(),
        };
        let mut backlog = Vec::new();
        let mut refining = Vec::new();
        let mut refined = Vec::new();
        let mut in_flight = Vec::new();
        let mut completed = Vec::new();
        for (i, g) in flat.iter().enumerate() {
            if !self.board_search.matches(g.issue_number, &g.issue_title) {
                continue;
            }
            match g.lifecycle_section() {
                "backlog" => backlog.push((i, g)),
                "refining" => refining.push((i, g)),
                "refined" => refined.push((i, g)),
                "in-flight" => in_flight.push((i, g)),
                "completed" => completed.push((i, g)),
                _ => backlog.push((i, g)),
            }
        }
        [
            ("backlog", backlog),
            ("refining", refining),
            ("refined", refined),
            ("in-flight", in_flight),
            ("completed", completed),
        ]
        .into_iter()
        .filter(|(_, v)| !v.is_empty())
        .collect()
    }

    /// Return the IssueGroup currently selected in the board sidebar.
    ///
    /// Paths are two-level: `[milestone_idx, issue_idx]`.
    /// A one-level path (milestone header selected) returns `None`.
    ///
    /// #1270: a nested epic-child row is depth 3 —
    /// `[milestone_idx, issue_idx, child_idx]` — and resolves to the CHILD
    /// issue, not its parent epic (mirrors Pipeline's
    /// `resolve_nested_child_index`). Returns `None` when the child isn't
    /// independently present in `board_issues_cache` (no assignment and not
    /// itself synced into `open_issues`) — such a row exists for nested
    /// display only, same as the Pipeline equivalent.
    fn board_selected_issue_group(&self) -> Option<&IssueGroup> {
        let section = self.board_sidebar.active_section()?;
        let offset = self.board_repo_offset();
        if section < offset {
            return None;
        }
        let path = self.board_sidebar.selected_path(section)?;
        // #410: issue rows are at path depth 2 — [milestone_idx, issue_idx].
        if path.len() < 2 {
            return None;
        }
        let milestone_idx = path[0] as usize;
        let issue_idx = path[1] as usize;
        let repo = self.board_repo_names.get(section - offset)?;
        let milestones = self.board_milestones_for_repo(&self.board_issues_cache, repo, true);
        let (_, _, group_issues) = milestones.get(milestone_idx)?;
        let (flat_idx, _) = group_issues.get(issue_idx)?;
        let (_, all_issues) = self.board_issues_cache.iter().find(|(r, _)| r == repo)?;
        let epic_group = all_issues.get(*flat_idx)?;
        // A 3rd path segment only means "nested epic child" when `epic_group`
        // actually has a child at that index — some historical callers set a
        // stale 3rd segment on an ordinary (non-epic) issue row, and that
        // must keep resolving to the row itself rather than going `None`.
        if path.len() >= 3 {
            let child_idx = path[2] as usize;
            let children = self.epic_child_rows_for(repo, epic_group.issue_number);
            if let Some(child_number) = children.get(child_idx).map(|c| c.number) {
                if let Some(child_group) =
                    all_issues.iter().find(|g| g.issue_number == child_number)
                {
                    return Some(child_group);
                }
            }
        }
        Some(epic_group)
    }

    /// Return the (repo, issue_number) currently selected in the board sidebar.
    fn board_selected_issue(&self) -> Option<(String, u64)> {
        let group = self.board_selected_issue_group()?;
        let repo = self.board_active_repo()?;
        Some((repo.to_string(), group.issue_number))
    }

    /// #2452: like `board_selected_issue`, but resolves the sidebar's stored
    /// tree path against the UNFILTERED milestone grouping — "what issue
    /// does this path identify, independent of whatever `board_search`
    /// happens to hold right now".
    ///
    /// `board_selected_issue`'s filter-sensitive resolution is correct for
    /// its many other callers (context menu targeting, the `P` purge guard,
    /// notify/retry) — they only ever run against the tree as it is
    /// CURRENTLY painted, and the stored path was set under that SAME
    /// paint's filter, so re-resolving it through the current filter is
    /// self-consistent there.
    ///
    /// `rebuild_board_sidebar`'s previous-selection capture is the one
    /// exception: every Board FILTER keystroke handler (`events.rs`)
    /// mutates `board_search` and THEN calls `rebuild_board_sidebar` — so by
    /// the time that capture runs, the OLD sidebar's stored path was set
    /// under the filter as it was BEFORE this keystroke, but `board_search`
    /// already holds the NEW query. Resolving that stale path through the
    /// new (smaller) filtered grouping can silently name a DIFFERENT issue
    /// that happens to now occupy the same `[milestone_idx, issue_idx]`
    /// coordinates — e.g. the row that WAS selected drops out of the
    /// filtered view, and whatever slides up into its slot gets "restored"
    /// in its place instead. Resolving against the unfiltered grouping
    /// avoids that: the coordinate space doesn't shift out from under the
    /// path just because a keystroke changed the filter.
    fn board_selected_issue_unfiltered(&self) -> Option<(String, u64)> {
        let section = self.board_sidebar.active_section()?;
        let offset = self.board_repo_offset();
        if section < offset {
            return None;
        }
        let path = self.board_sidebar.selected_path(section)?;
        if path.len() < 2 {
            return None;
        }
        let milestone_idx = path[0] as usize;
        let issue_idx = path[1] as usize;
        let repo = self.board_repo_names.get(section - offset)?;
        let milestones =
            self.board_milestones_for_repo(&self.board_issues_cache, repo, false);
        let (_, _, group_issues) = milestones.get(milestone_idx)?;
        let (flat_idx, _) = group_issues.get(issue_idx)?;
        let (_, all_issues) = self.board_issues_cache.iter().find(|(r, _)| r == repo)?;
        let epic_group = all_issues.get(*flat_idx)?;
        // #1270 parity with `board_selected_issue_group`: a 3rd path segment
        // means "nested epic child" only when `epic_group` actually has a
        // child at that index.
        if path.len() >= 3 {
            let child_idx = path[2] as usize;
            let children = self.epic_child_rows_for(repo, epic_group.issue_number);
            if let Some(child_number) = children.get(child_idx).map(|c| c.number) {
                return Some((repo.clone(), child_number));
            }
        }
        Some((repo.clone(), epic_group.issue_number))
    }

    // ── #2285 (ms-65 §5): per-tab detail sub-state ───────────────────────
    //
    // The detail pane's sub-tab, scroll offset and expanded stage belong to
    // the DOCUMENT, not to the panel: with two tabs open, switching one to
    // `Issue` or scrolling its body must leave the other exactly where it
    // was. `CoordApp`'s `board_detail_tab` / `pipeline_detail_tab` /
    // `detail_scroll` / `pipeline_detail_scroll` / `pipeline_stage_content_scroll`
    // / `pipeline_focused_stage` fields stay put as the **live** values —
    // roughly 150 call sites across `events.rs`, `dialogs.rs`, `render.rs`,
    // `sessions.rs` and `terminal.rs` read and write them, most from inside
    // `match` guards — and are checkpointed into / restored out of the active
    // document's `DetailSubState` record on every tab switch. The record in
    // `doc_tabs.rs` is the source of truth for every *inactive* tab; the live
    // fields are the active tab's working copy.
    //
    // That is what keeps the slice behaviour-preserving at one open tab: with
    // a single tab there is nothing to switch to, so no checkpoint/restore
    // pair ever runs and the live fields behave exactly as they do today.
    //
    // The three hooks, in the order they must run:
    //   1. `checkpoint_detail_sub_state` — BEFORE the tab-set mutation, so it
    //      still sees the OUTGOING active key.
    //   2. the mutation itself (`open_preview` / `pin` / `activate_index` /
    //      `close`).
    //   3. `restore_detail_sub_state` — AFTER the reveal (§2f), because
    //      `reveal_pipeline_active_doc` resets `pipeline_focused_stage` and
    //      `pipeline_stage_content_scroll` on its way past and the restored
    //      values must win.

    /// Snapshot the live detail sub-state fields `scope`'s pane owns.
    ///
    /// Only that scope's fields are populated; the rest keep their `Default`
    /// value and are never read back (see [`DetailSubState`]'s scope-ownership
    /// note), so a Board checkpoint can never clobber Pipeline's live pane and
    /// vice versa.
    fn detail_sub_state_snapshot(&self, scope: PanelScope) -> DetailSubState {
        match scope {
            PanelScope::Board => DetailSubState {
                board_tab: self.board_detail_tab,
                scroll: self.detail_scroll,
                ..DetailSubState::default()
            },
            PanelScope::Pipeline => DetailSubState {
                pipeline_tab: self.pipeline_detail_tab,
                scroll: self.pipeline_detail_scroll,
                stage_scroll: self.pipeline_stage_content_scroll,
                focused_stage: self.pipeline_focused_stage,
                ..DetailSubState::default()
            },
        }
    }

    /// Load a stored record back into the live fields `scope`'s pane reads.
    fn apply_detail_sub_state(&mut self, scope: PanelScope, state: &DetailSubState) {
        match scope {
            PanelScope::Board => {
                self.board_detail_tab = state.board_tab;
                self.detail_scroll = state.scroll;
            }
            PanelScope::Pipeline => {
                self.pipeline_detail_tab = state.pipeline_tab;
                self.pipeline_detail_scroll = state.scroll;
                self.pipeline_stage_content_scroll = state.stage_scroll;
                self.pipeline_focused_stage = state.focused_stage;
            }
        }
    }

    /// Reset `scope`'s live sub-state to the pane defaults, for a document
    /// being shown for the first time (contract §5: "defaults on open match
    /// today's defaults — `Board` / `Overview`, scroll 0").
    ///
    /// Deliberately narrower than [`Self::apply_detail_sub_state`]: it leaves
    /// `pipeline_focused_stage` / `pipeline_stage_content_scroll` alone,
    /// because `reveal_pipeline_active_doc` has just seeded them from
    /// `default_focused_stage_for_selected_issue()` — the pre-ms-65 default
    /// for a freshly-revealed Pipeline issue, which a blunt `None` would
    /// regress.
    fn reset_detail_sub_state(&mut self, scope: PanelScope) {
        match scope {
            PanelScope::Board => {
                self.board_detail_tab = BoardDetailTab::default();
                self.detail_scroll = 0;
            }
            PanelScope::Pipeline => {
                self.pipeline_detail_tab = PipelineDetailTab::default();
                self.pipeline_detail_scroll = 0;
            }
        }
    }

    /// Save the live sub-state under `scope`'s currently-active document.
    ///
    /// A no-op when nothing is open — with zero tabs the pane is still the
    /// pure function of the sidebar selection it was before ms-65, and there
    /// is no document to attribute the state to.
    fn checkpoint_detail_sub_state(&mut self, scope: PanelScope) {
        let Some(key) = self.doc_tabs.group(scope).active_key().cloned() else {
            return;
        };
        let state = self.detail_sub_state_snapshot(scope);
        self.doc_tabs.group_mut(scope).set_sub_state(key, state);
    }

    /// Load `scope`'s newly-active document's sub-state into the live fields,
    /// or fall back to the pane defaults for a document with no record yet.
    ///
    /// A no-op when nothing is open (§4's empty state): closing the last tab
    /// returns to selection-follows-tree, and blanking the pane's sub-tab on
    /// the way out would be a change no clause asks for.
    fn restore_detail_sub_state(&mut self, scope: PanelScope) {
        let Some(key) = self.doc_tabs.group(scope).active_key().cloned() else {
            return;
        };
        match self.doc_tabs.group(scope).sub_state(&key).cloned() {
            Some(state) => self.apply_detail_sub_state(scope, &state),
            None => self.reset_detail_sub_state(scope),
        }
    }

    // ── #2286 (ms-65 §6): persist doc tabs to `~/.coord/tabs.json` ──────

    /// #1326-style reconciliation for the persisted doc-tab sets — called
    /// from `apply_pending_data` right after `sync_workspace_repos()`, once
    /// per real data tick. `CoordApp::new()` seeds `doc_tabs` from whatever
    /// was on disk, unpruned (the board hasn't loaded yet at that point);
    /// this is what drops any tab whose issue this tick doesn't know about,
    /// mirroring `sync_workspace_repos`'s own "reconcile against what the
    /// board just confirmed exists" shape.
    ///
    /// Persists only when something actually changed, exactly like
    /// `sync_workspace_repos` — most ticks reconcile to a no-op once the
    /// first one has run.
    fn sync_doc_tabs(&mut self) {
        let known = doc_tabs::known_doc_keys(&self.data);
        let before = self.doc_tabs.clone();
        self.doc_tabs.retain_known(&known);
        if self.doc_tabs != before {
            self.persist_doc_tabs();
        }
    }

    /// Persist the current doc-tab sets to `~/.coord/tabs.json` if that file
    /// already exists (contract §6) — see [`doc_tabs::DocTabs::save_if_exists`]
    /// for why this deliberately never CREATES the file. Called from every
    /// mutator that changes a tab set's shape (open/pin/activate/close/…,
    /// both scopes) and from `sync_doc_tabs`'s reconciliation. Errors are
    /// surfaced as a stderr line rather than a panic or toast, matching
    /// `Self::persist_workspace`: the TUI must keep working even when
    /// `~/.coord` is unwritable, it just won't remember tabs next launch.
    ///
    /// Called unconditionally by the mutators, including on gestures that
    /// turn out to change nothing (re-clicking the already-active tab):
    /// [`doc_tabs::DocTabs::save_to_path`] compares against the file's real
    /// content and skips the write itself, which is the only place that
    /// comparison is correct — see its doc comment (#2286 review,
    /// non-blocking 1).
    fn persist_doc_tabs(&self) {
        if let Err(e) = self.doc_tabs.save_if_exists() {
            eprintln!("coord-tui: failed to persist doc tabs: {e}");
        }
    }

    /// Snapshot BOTH scopes' live detail sub-state under their own active
    /// document.
    ///
    /// [`Self::checkpoint_detail_sub_state`] runs on a document-tab *switch*,
    /// where it banks the outgoing document's sub-state. That leaves the
    /// currently-active document's live sub-state unbanked for as long as the
    /// user stays on it: a sub-tab-only switch (Board → Issue → Chat, which
    /// writes `board_detail_tab` / `pipeline_detail_tab` directly from
    /// `events.rs`) never passes through a tab switch. This banks both scopes
    /// at once, for the moments where the whole record is about to be
    /// serialised.
    ///
    /// Each scope's call is independently a no-op when that scope has no open
    /// document, and the two can never clobber each other: a checkpoint only
    /// populates the fields its own scope owns (see
    /// [`Self::detail_sub_state_snapshot`]).
    fn checkpoint_all_detail_sub_state(&mut self) {
        self.checkpoint_detail_sub_state(PanelScope::Board);
        self.checkpoint_detail_sub_state(PanelScope::Pipeline);
    }

    /// Persist the current doc-tab sets to `~/.coord/tabs.json`
    /// unconditionally, creating the file if this is the very first time
    /// (contract §6). Called exactly once, on the way out — see
    /// `ShellApp::handle`'s `Reaction::Exit` hook in `render.rs`.
    ///
    /// #2286 review (blocking): checkpoints both scopes FIRST. Without it the
    /// most ordinary session there is — open one issue, click through to its
    /// `Chat` sub-tab, quit — persisted whatever was last banked (usually
    /// nothing) rather than the sub-tab actually on screen, and the restart
    /// dropped back to the scope default. Everything the exit hook is for
    /// (`board_tab` / `pipeline_tab`, the "cheap" half of `DetailSubState`)
    /// depends on the live fields being banked before `save()` reads them.
    fn persist_doc_tabs_on_exit(&mut self) {
        self.checkpoint_all_detail_sub_state();
        if let Err(e) = self.doc_tabs.save() {
            eprintln!("coord-tui: failed to persist doc tabs: {e}");
        }
    }

    // ── #2642 (ms-65 §…): Ctrl+E open-tabs picker ─────────────────────────

    /// Open-tab rows for `scope`'s picker, in strip order — `pane` selects
    /// which Board pane (ignored for Pipeline, which never splits). Same
    /// title lookups `board_doc_tab_bar_for_pane` / `pipeline_doc_tab_bar`
    /// (render.rs) use, so a row here always matches what the strip itself
    /// would paint.
    fn doc_tab_picker_rows(&self, scope: PanelScope, pane: usize) -> Vec<DocTabPickerRow> {
        match scope {
            PanelScope::Board => {
                let group = self.doc_tabs.panes(PanelScope::Board).pane(pane);
                group
                    .tabs()
                    .iter()
                    .enumerate()
                    .map(|(idx, (repo, number))| DocTabPickerRow {
                        strip_idx: idx,
                        repo: repo.clone(),
                        number: *number,
                        title: self
                            .board_issue_group_for(repo, *number)
                            .map(|g| g.issue_title.clone())
                            .unwrap_or_default(),
                        is_active: group.active_index() == Some(idx),
                        is_preview: group.is_preview(idx),
                    })
                    .collect()
            }
            PanelScope::Pipeline => {
                let group = self.pipeline_doc_tabs();
                group
                    .tabs()
                    .iter()
                    .enumerate()
                    .map(|(idx, (repo, number))| DocTabPickerRow {
                        strip_idx: idx,
                        repo: repo.clone(),
                        number: *number,
                        title: self
                            .pipeline_issue_for(repo, *number)
                            .map(|pi| pi.title.clone())
                            .unwrap_or_default(),
                        is_active: group.active_index() == Some(idx),
                        is_preview: group.is_preview(idx),
                    })
                    .collect()
            }
        }
    }

    /// `scope`/`pane`'s picker rows, filtered by `query` and rendered as
    /// `PaletteItem`s — the source both [`Self::open_doc_tab_picker`] (empty
    /// query) and the `QueryChanged` re-filter in `events.rs` call.
    fn doc_tab_picker_items_for(
        &self,
        scope: PanelScope,
        pane: usize,
        query: &str,
    ) -> Vec<quadraui::PaletteItem> {
        let rows = self.doc_tab_picker_rows(scope, pane);
        let show_repo = doc_tabs::doc_tab_picker_show_repo(&rows);
        let filtered = doc_tabs::filter_doc_tab_picker_rows(rows, query);
        doc_tabs::doc_tab_picker_items(&filtered, show_repo)
    }

    /// Popup geometry for the picker — the same centered-inset formula as
    /// `plans.rs`'s `help_overlay_rect` (no separate label row needed here,
    /// unlike `command_palette_popup_rect`: the picker's title lives in the
    /// `Palette` primitive's own `title` field). Reimplemented rather than
    /// shared because `help_overlay_rect` is private to the `plans` module
    /// and this picker isn't Plans-scoped.
    pub(crate) fn doc_tab_picker_popup_rect(main: Rect) -> Rect {
        let w = (main.width - 4.0).max(20.0).min(main.width);
        let h = (main.height - 2.0).max(10.0).min(main.height);
        let x = main.x + (main.width - w) * 0.5;
        let y = main.y + (main.height - h) * 0.5;
        Rect::new(x, y, w, h)
    }

    /// Paint the picker — a no-op when closed.
    pub(crate) fn render_doc_tab_picker(&self, backend: &mut dyn Backend, main: Rect) {
        let Some(picker) = &self.doc_tab_picker else {
            return;
        };
        let popup = Self::doc_tab_picker_popup_rect(main);
        picker.palette.render(popup, backend);
    }

    /// Open the picker for `scope` (contract: `Ctrl+E`). No-op — and no
    /// picker opens — when that panel currently has zero doc tabs open,
    /// mirroring `open_command_palette`'s "nothing registered" no-op.
    fn open_doc_tab_picker(&mut self, scope: PanelScope) {
        let pane = match scope {
            PanelScope::Board => self.board_render_pane(),
            PanelScope::Pipeline => 0,
        };
        let items = self.doc_tab_picker_items_for(scope, pane, "");
        if items.is_empty() {
            return;
        }
        self.doc_tab_picker = Some(DocTabPickerState {
            scope,
            pane,
            palette: DualModePaletteController::new("Open tabs", None, items)
                .with_id("doc-tab-picker"),
        });
    }

    /// Confirm the picker's `idx`-th CURRENTLY FILTERED row (i.e. the same
    /// `query` the palette was last filtered against — mirrors
    /// `activate_command_palette_action`'s own re-derive-from-query
    /// pattern) — activates the corresponding tab via the exact entry point
    /// ([`Self::activate_board_doc_tab`] / [`Self::activate_pipeline_doc_tab`])
    /// a click on the strip itself already uses, so preview/pin semantics
    /// and the #2288 pane routing can't drift. A stale/out-of-range `idx`
    /// (nothing else can move the query while the picker owns all input, so
    /// this should never actually happen) is a silent no-op.
    fn confirm_doc_tab_picker(
        &mut self,
        scope: PanelScope,
        pane: usize,
        idx: usize,
        query: &str,
    ) -> bool {
        let rows = doc_tabs::filter_doc_tab_picker_rows(
            self.doc_tab_picker_rows(scope, pane),
            query,
        );
        let Some(row) = rows.get(idx) else {
            return false;
        };
        let strip_idx = row.strip_idx;
        match scope {
            PanelScope::Board => self.activate_board_doc_tab(strip_idx),
            PanelScope::Pipeline => self.activate_pipeline_doc_tab(strip_idx),
        }
    }

    // ── #2282 (ms-65 §2): Board document tabs ────────────────────────────

    /// The Board panel's document-tab set.
    ///
    /// #2288 (§9): the *currently-addressed* pane's set — the pane being
    /// painted during a render pass, the focused pane everywhere else (see
    /// [`Self::board_render_pane`]). Unsplit, there is only one pane and
    /// this is exactly what it was before the split existed.
    fn board_doc_tabs(&self) -> &doc_tabs::DocTabGroup {
        self.doc_tabs
            .panes(PanelScope::Board)
            .pane(self.board_render_pane())
    }

    /// #2288 (ms-65 §9): the Board pane index the `&self` readers resolve
    /// against — `render_pane` while a pane is being painted, otherwise the
    /// focused pane.
    pub(crate) fn board_render_pane(&self) -> usize {
        self.render_pane
            .get()
            .unwrap_or_else(|| self.doc_tabs.panes(PanelScope::Board).focused_index())
    }

    /// #2288 (ms-65 §9): the Board panel's panes + split geometry.
    pub(crate) fn board_panes(&self) -> &doc_tabs::PaneSet {
        self.doc_tabs.panes(PanelScope::Board)
    }

    /// The sub-tab (`Board` / `Issue` / `Chat` / `Terminal`) the
    /// currently-addressed pane shows.
    ///
    /// #2285 keeps the *live* `board_detail_tab` field as the focused
    /// document's working copy and banks every other document's value in
    /// its `DetailSubState`. A background pane's active document is by
    /// definition not the live one, so its sub-tab has to be read back out
    /// of that record (defaulting exactly as #2285's "defaults on open
    /// match today's defaults" clause says).
    pub(crate) fn board_pane_detail_tab(&self) -> BoardDetailTab {
        match self.board_pane_sub_state() {
            Some(state) => state.board_tab,
            None => self.board_detail_tab,
        }
    }

    /// The body scroll offset of the currently-addressed pane — the
    /// [`Self::board_pane_detail_tab`] rule applied to `detail_scroll`.
    pub(crate) fn board_pane_detail_scroll(&self) -> usize {
        match self.board_pane_sub_state() {
            Some(state) => state.scroll,
            None => self.detail_scroll,
        }
    }

    /// Write `pane` scroll offset back, wherever that pane keeps it.
    ///
    /// The mirror of [`Self::board_pane_detail_scroll`]: the focused pane's
    /// offset is the live `detail_scroll` field, every other pane's is
    /// banked in its active document's [`DetailSubState`]. A pane holding no
    /// document has nowhere to put it — and nothing on screen to scroll — so
    /// that case is a deliberate no-op.
    pub(crate) fn set_board_pane_detail_scroll(&mut self, pane: usize, scroll: usize) {
        let panes = self.doc_tabs.panes(PanelScope::Board);
        if pane >= panes.len() || pane == panes.focused_index() {
            self.detail_scroll = scroll;
            return;
        }
        let group = panes.pane(pane);
        let Some(key) = group.active_key().cloned() else {
            return;
        };
        let mut state = group.sub_state(&key).cloned().unwrap_or_default();
        state.scroll = scroll;
        self.doc_tabs
            .panes_mut(PanelScope::Board)
            .pane_mut(pane)
            .set_sub_state(key, state);
    }

    /// #2288 (ms-65 §9): stash the Issue sub-tab content width for the pane
    /// currently being painted — see [`Self::board_pane_issue_cols`].
    pub(crate) fn stash_board_pane_issue_cols(&self, cols: usize) {
        let idx = self.board_render_pane();
        let mut widths = self.board_pane_issue_cols.borrow_mut();
        if widths.len() <= idx {
            widths.resize(idx + 1, 0);
        }
        widths[idx] = cols;
    }

    /// #2288 (ms-65 §9): the word-wrap width `board_issue_body_list` must
    /// use — the *addressed* pane's own last painted width, never whichever
    /// pane happened to paint last.
    pub(crate) fn board_issue_wrap_cols(&self) -> usize {
        let idx = self.board_render_pane();
        self.board_pane_issue_cols
            .borrow()
            .get(idx)
            .copied()
            .filter(|cols| *cols > 0)
            .unwrap_or_else(|| self.last_issue_panel_cols.get())
    }

    /// Drop stashed Issue widths for panes that no longer exist, so a pane
    /// index recycled by a later `Ctrl-W v` never inherits the width the
    /// pane that used to live there painted at.
    pub(crate) fn truncate_board_pane_issue_cols(&self, panes: usize) {
        self.board_pane_issue_cols.borrow_mut().truncate(panes);
    }

    /// The banked sub-state of the addressed pane's active document, or
    /// `None` when that pane IS the focused one (whose values live in the
    /// `CoordApp` fields) or holds no document at all.
    fn board_pane_sub_state(&self) -> Option<DetailSubState> {
        let panes = self.doc_tabs.panes(PanelScope::Board);
        let idx = self.board_render_pane();
        if idx == panes.focused_index() {
            return None;
        }
        let group = panes.pane(idx);
        let key = group.active_key()?;
        Some(group.sub_state(key).cloned().unwrap_or_default())
    }

    // ── #2288 (ms-65 §9): pane verbs ─────────────────────────────────────
    //
    // Every one of these funnels through `PaneSet`, checkpointing the live
    // detail sub-state first and restoring it afterwards for exactly the
    // reason #2285's tab switches do: the focused pane's active document
    // owns `board_detail_tab` / `detail_scroll` as its working copy, so
    // moving focus between panes is a document switch and has to bank the
    // outgoing document's values before the incoming one's are loaded.

    /// `Ctrl-W v` — split the focused Board pane right (contract §9).
    ///
    /// The new pane is empty and takes focus, so the next single click from
    /// the sidebar opens its preview *there* and leaves the original pane's
    /// preview untouched — §9's independence clause.
    fn split_board_pane_right(&mut self) -> bool {
        self.checkpoint_detail_sub_state(PanelScope::Board);
        if !self.doc_tabs.panes_mut(PanelScope::Board).split_focused() {
            return false;
        }
        self.restore_detail_sub_state(PanelScope::Board);
        true
    }

    /// `Ctrl-W w` — move focus to the next Board pane, wrapping. A no-op
    /// with one pane.
    fn focus_next_board_pane(&mut self) -> bool {
        self.checkpoint_detail_sub_state(PanelScope::Board);
        if !self.doc_tabs.panes_mut(PanelScope::Board).focus_next() {
            return false;
        }
        self.reveal_board_active_doc();
        self.restore_detail_sub_state(PanelScope::Board);
        true
    }

    /// `Ctrl-W x` — close the focused Board pane (contract §9). A **no-op
    /// at one pane**: "a scope always has ≥1 pane".
    fn close_focused_board_pane(&mut self) -> bool {
        // No checkpoint: the closed pane's documents (and their sub-state)
        // are discarded, exactly as closing their tabs one by one would
        // discard them — the same rule `close_board_doc_tab` follows.
        if !self
            .doc_tabs
            .panes_mut(PanelScope::Board)
            .close_focused_pane()
        {
            return false;
        }
        self.reveal_board_active_doc();
        self.restore_detail_sub_state(PanelScope::Board);
        self.persist_doc_tabs();
        true
    }

    /// Focus the Board pane at `idx` — a click landing inside it. Returns
    /// `true` when focus actually moved.
    ///
    /// Every main-panel click routes through here, so the "already focused"
    /// case (which is *every* click on an unsplit panel) is rejected before
    /// the checkpoint rather than after it: banking the live sub-state on a
    /// click that changes no document would be pure work.
    fn focus_board_pane(&mut self, idx: usize) -> bool {
        if self.board_panes().focused_index() == idx {
            return false;
        }
        self.checkpoint_detail_sub_state(PanelScope::Board);
        if !self.doc_tabs.panes_mut(PanelScope::Board).set_focused(idx) {
            return false;
        }
        self.restore_detail_sub_state(PanelScope::Board);
        true
    }

    /// Contract §9: "Closing the last tab in one pane collapses that pane
    /// back to a single-pane layout." Called from every Board close path,
    /// never from the split path (whose new pane is empty by design).
    fn collapse_empty_board_panes(&mut self) -> bool {
        if !self
            .doc_tabs
            .panes_mut(PanelScope::Board)
            .collapse_empty_panes()
        {
            return false;
        }
        self.reveal_board_active_doc();
        self.restore_detail_sub_state(PanelScope::Board);
        true
    }

    /// The active Board document's key, or `None` when no tab is open.
    fn board_doc_active_key(&self) -> Option<&DocKey> {
        self.board_doc_tabs().active_key()
    }

    /// Look one issue up in the Board cache by `(repo, number)`.
    ///
    /// Returns `None` for a document whose issue is no longer on the board
    /// (e.g. it was purged between the tab being opened and this frame) —
    /// callers fall back to the tree selection rather than rendering a stale
    /// document.
    fn board_issue_group_for(&self, repo: &str, number: u64) -> Option<&IssueGroup> {
        let (_, issues) = self.board_issues_cache.iter().find(|(r, _)| r == repo)?;
        issues.iter().find(|g| g.issue_number == number)
    }

    /// The issue the Board **detail pane** is showing.
    ///
    /// Contract §2: with a document tab open the pane follows the **active
    /// tab**, not the tree cursor — that is the whole point of the milestone,
    /// and the reason moving the sidebar selection no longer loses what you
    /// were reading. With zero tabs open (or an active tab whose issue has
    /// left the board) this is exactly `board_selected_issue_group`, i.e.
    /// pre-ms-65 selection-follows-tree.
    ///
    /// Deliberately *not* folded into `board_selected_issue_group` itself:
    /// that accessor also drives the sidebar context menu, the `P` purge
    /// guard and the notify/retry verbs, which must keep acting on the row
    /// the user has highlighted.
    fn board_detail_issue_group(&self) -> Option<&IssueGroup> {
        if let Some((repo, number)) = self.board_doc_active_key() {
            if let Some(group) = self.board_issue_group_for(repo, *number) {
                return Some(group);
            }
        }
        self.board_selected_issue_group()
    }

    /// The repo the Board detail pane's issue belongs to — the active tab's
    /// repo when one is open, else the sidebar's active repo. Paired with
    /// [`Self::board_detail_issue_group`]; the two must agree or the pane
    /// renders one issue's body under another repo's heading.
    fn board_detail_repo(&self) -> Option<&str> {
        if let Some((repo, number)) = self.board_doc_active_key() {
            if self.board_issue_group_for(repo, *number).is_some() {
                return Some(repo.as_str());
            }
        }
        self.board_active_repo()
    }

    /// Open `key` in the Board tab strip.
    ///
    /// `pin == false` is the single-click path (contract §2e rules 1/2/4);
    /// `pin == true` is the double-click path (rule 3: open-or-activate, then
    /// promote). Reveals the newly-active document in the sidebar (§2f) when
    /// activation actually moved.
    ///
    /// #2285 (§5): checkpoints the outgoing document's detail sub-state before
    /// the mutation and restores the incoming one's after the reveal, so the
    /// tab you were reading keeps its sub-tab and scroll and the tab you open
    /// starts from the defaults instead of inheriting them.
    fn open_board_doc_tab(&mut self, key: DocKey, pin: bool) {
        let before = self.board_doc_active_key().cloned();
        self.checkpoint_detail_sub_state(PanelScope::Board);
        {
            let group = self.doc_tabs.group_mut(PanelScope::Board);
            if pin {
                group.pin(key);
            } else {
                group.open_preview(key);
            }
        }
        if self.board_doc_active_key().cloned() != before {
            self.reveal_board_active_doc();
            self.restore_detail_sub_state(PanelScope::Board);
        }
        self.persist_doc_tabs();
    }

    /// Activate the Board tab at strip index `idx` (a click on the strip
    /// itself). Returns `true` when the click landed on a real tab — not
    /// merely when the active index moved, because the unconditional reveal
    /// below can change sidebar state (selection, tree window) even for a
    /// re-activation, and the caller uses this bool as its redraw signal.
    fn activate_board_doc_tab(&mut self, idx: usize) -> bool {
        if idx >= self.board_doc_tabs().tabs().len() {
            return false;
        }
        // #2285 (§5): banking the outgoing tab's sub-state is what lets the
        // incoming tab restore its own. Unconditional, like the reveal below —
        // when the click lands on the already-active tab, checkpoint and
        // restore are each other's inverse and the pair is a no-op.
        self.checkpoint_detail_sub_state(PanelScope::Board);
        self.doc_tabs.group_mut(PanelScope::Board).activate_index(idx);
        // Reveal unconditionally, not just when the active index moved:
        // contract §2f says activating a tab "by any path" reveals its row,
        // and re-activating the already-active tab is exactly how a user asks
        // to be taken back to a document they have navigated away from in the
        // tree. Gating on `changed` would make that click a silent no-op.
        self.reveal_board_active_doc();
        self.restore_detail_sub_state(PanelScope::Board);
        self.persist_doc_tabs();
        true
    }

    /// Close the Board tab at strip index `idx` (contract §4, #2283) —
    /// clicking a tab's `×`, middle-clicking a tab, and `Ctrl-W` on the
    /// active tab all funnel through here.
    ///
    /// The active-neighbour rule is `DocTabGroup::close`'s job, not this
    /// wrapper's: closing the ACTIVE tab activates the tab immediately to
    /// its left, or (when the closed tab was the leftmost) the new
    /// leftmost. Closing an INACTIVE tab leaves the active *document*
    /// unchanged.
    ///
    /// Reveals the new active document (§2f) only when the active KEY
    /// actually changed — comparing keys rather than indices, so closing
    /// an inactive tab (which only re-bases the active index to keep
    /// pointing at the same document) never touches sidebar selection or
    /// scroll. Closing the last tab clears the active key to `None`; the
    /// key comparison catches that too, and `reveal_board_active_doc`
    /// itself no-ops on `None` — nothing left to reveal, which is exactly
    /// §4's "return to selection-follows-tree" (the sidebar selection made
    /// when the closed tab was opened is simply what's left).
    /// #2285 (§5): deliberately does NOT checkpoint first — the closed tab's
    /// sub-state is discarded, not banked (`DocTabGroup::close` prunes its
    /// record), so re-opening the same issue number starts from the defaults.
    /// Closing an INACTIVE tab leaves the active key alone, so the live fields
    /// are left untouched too; only a close that moves the active document
    /// restores the neighbour's own sub-state.
    fn close_board_doc_tab(&mut self, idx: usize) -> bool {
        let before = self.board_doc_active_key().cloned();
        if !self.doc_tabs.group_mut(PanelScope::Board).close(idx) {
            return false;
        }
        if self.board_doc_active_key().cloned() != before {
            self.reveal_board_active_doc();
            self.restore_detail_sub_state(PanelScope::Board);
        }
        // #2288 (§9): "Closing the last tab in one pane collapses that pane
        // back to a single-pane layout." No-op unless the panel is split
        // AND this close emptied a pane.
        self.collapse_empty_board_panes();
        self.persist_doc_tabs();
        true
    }

    /// `Ctrl-W` (contract §4, #2283) — close the ACTIVE Board tab. Returns
    /// `false` when no tab is open, so the caller can fall through to
    /// `Ctrl-W`'s #605 pane-focus-leader meaning.
    fn close_active_board_doc_tab(&mut self) -> bool {
        match self.board_doc_tabs().active_index() {
            Some(idx) => self.close_board_doc_tab(idx),
            None => false,
        }
    }

    /// `Ctrl-Tab` / `Ctrl-Shift-Tab` (contract §4, #2283) — cycle the active
    /// Board tab, wrapping at both ends. `forward == true` is `Ctrl-Tab`
    /// ("moves to the next tab, wrapping from the last to the first");
    /// `false` is `Ctrl-Shift-Tab` ("moves to the previous, wrapping from
    /// the first to the last"). Returns `false` when no tab is open.
    ///
    /// Routes through `activate_board_doc_tab` so cycling gets the exact
    /// same reveal-on-activate (§2f) and promote-on-select
    /// (`DocTabGroup::activate_index`) behaviour a strip click gets —
    /// cycling onto a preview tab is the same "I want to keep looking at
    /// this one" gesture a click is.
    fn cycle_board_doc_tab(&mut self, forward: bool) -> bool {
        let len = self.board_doc_tabs().tabs().len();
        if len == 0 {
            return false;
        }
        let cur = self.board_doc_tabs().active_index().unwrap_or(0);
        let next = if forward {
            (cur + 1) % len
        } else {
            (cur + len - 1) % len
        };
        self.activate_board_doc_tab(next)
    }

    /// #2287 (ms-65 §8c): the tab context menu's "Close others" item —
    /// close every Board tab except the RIGHT-CLICKED one at strip index
    /// `idx` (not necessarily the active tab, see
    /// `ContextMenuTarget::BoardDocTab`'s doc comment). Mirrors
    /// `close_board_doc_tab`'s reveal-on-active-change behaviour.
    fn close_other_board_doc_tabs(&mut self, idx: usize) -> bool {
        let before = self.board_doc_active_key().cloned();
        if !self.doc_tabs.group_mut(PanelScope::Board).close_others(idx) {
            return false;
        }
        if self.board_doc_active_key().cloned() != before {
            self.reveal_board_active_doc();
            self.restore_detail_sub_state(PanelScope::Board);
        }
        self.persist_doc_tabs();
        true
    }

    /// #2287 (ms-65 §8c): the tab context menu's "Close all" item — the
    /// menu's route to the same "zero doc tabs" end state as closing every
    /// tab one at a time (contract §4 / §8c).
    fn close_all_board_doc_tabs(&mut self) -> bool {
        let before = self.board_doc_active_key().cloned();
        if !self.doc_tabs.group_mut(PanelScope::Board).close_all() {
            return false;
        }
        if self.board_doc_active_key().cloned() != before {
            self.reveal_board_active_doc();
            self.restore_detail_sub_state(PanelScope::Board);
        }
        // #2288 (§9): emptying a pane collapses it, whether the tabs went
        // one at a time or all at once.
        self.collapse_empty_board_panes();
        self.persist_doc_tabs();
        true
    }

    /// #2287 (ms-65 §8c): the tab context menu's "Pin tab" item — promote
    /// the RIGHT-CLICKED Board tab at strip index `idx` out of the preview
    /// slot. Never moves the active tab or reorders the strip (unlike
    /// open/close), so no reveal / sub-state restore is needed.
    fn promote_board_doc_tab(&mut self, idx: usize) -> bool {
        let changed = self.doc_tabs.group_mut(PanelScope::Board).promote(idx);
        if changed {
            self.persist_doc_tabs();
        }
        changed
    }

    /// Contract §2f — reveal-on-activate.
    ///
    /// Activating a tab selects the matching sidebar row **and scrolls it into
    /// view**, expanding its milestone group first when that group is
    /// collapsed (otherwise the row it is meant to select does not exist in
    /// the tree at all).
    fn reveal_board_active_doc(&mut self) {
        let Some((repo, number)) = self.board_doc_active_key().cloned() else {
            return;
        };
        self.expand_board_milestone_for(&repo, number);
        // Drop any previous windowing first: the hidden-row count below must
        // be recomputed against the FULL row list (a stale window would both
        // skew `board_section_rows` and double-count its own marker row).
        // The `▸` active-document marker is baked into the row text, so the
        // tree has to be rebuilt for the marker to move anyway.
        self.board_tree_hidden_above.remove(&repo);
        self.rebuild_board_sidebar();
        self.select_issue(&repo, number);
        // §2f scroll-into-view, without moving the whole panel: the FILTER
        // box is pinned to the top of the sidebar, so the reveal windows the
        // repo's own tree (leading rows collapse into a `⋮ N more above`
        // marker) rather than calling `set_panel_scroll`.
        self.board_sidebar.set_panel_scroll(0.0);
        let hidden = self.board_reveal_hidden_count();
        if hidden > 0 {
            self.board_tree_hidden_above.insert(repo.clone(), hidden);
            self.rebuild_board_sidebar();
            self.select_issue(&repo, number);
            self.board_sidebar.set_panel_scroll(0.0);
        }
        // Degenerate fallback (windowing alone cannot lift the row above the
        // fold when the sections ABOVE the target repo already overflow the
        // viewport): minimal whole-panel scroll. No-op whenever the windowed
        // row is already visible — i.e. in every §2f-shaped layout.
        self.scroll_board_selection_into_view();
    }

    /// §2f windowing arithmetic: how many leading rows of the active
    /// section's tree must collapse into the `⋮ N more above` marker for the
    /// currently-selected row's bottom edge to sit inside the viewport with
    /// the panel unscrolled. `0` ⇒ the row already fits un-windowed. Uses the
    /// same reconstructed row model as [`Self::scroll_board_selection_into_view`]
    /// (quadraui#595's `SidebarSystem::reveal` will replace both once landed).
    fn board_reveal_hidden_count(&self) -> usize {
        let Some((rect, lh, header_size)) = self.last_sidebar_geom.get() else {
            return 0;
        };
        if rect.height <= 0.0 || lh <= 0.0 {
            return 0;
        }
        let Some(section) = self.board_sidebar.active_section() else {
            return 0;
        };
        let Some(path) = self.board_sidebar.selected_path(section) else {
            return 0;
        };
        if !self.board_sidebar.is_section_visible(section) {
            return 0;
        }

        // Fixed content above the target section's body (panel scroll 0).
        let mut base = 0.0_f32;
        for s in 0..section {
            if !self.board_sidebar.is_section_visible(s) {
                continue;
            }
            base += header_size + self.board_section_content_height(s, lh);
        }
        base += header_size;

        let Some(rows) = self.board_section_rows.get(section) else {
            return 0;
        };
        let mut heights = Vec::with_capacity(rows.len());
        let mut target = None;
        for (i, (row_path, is_header)) in rows.iter().enumerate() {
            heights.push(Self::board_row_height(lh, *is_header));
            if row_path == path {
                target = Some(i);
            }
        }
        let Some(t) = target else {
            return 0;
        };
        // prefix[i] = summed height of rows[..i].
        let mut prefix = Vec::with_capacity(heights.len() + 1);
        let mut acc = 0.0_f32;
        prefix.push(0.0);
        for h in &heights {
            acc += h;
            prefix.push(acc);
        }
        let marker_h = Self::board_row_height(lh, false);
        // Bottom edge of the target row when the first `h` rows are hidden
        // behind the (one-row) marker.
        let bottom_of = |h: usize| -> f32 {
            let marker = if h > 0 { marker_h } else { 0.0 };
            base + marker + (prefix[t] - prefix[h]) + heights[t]
        };
        if bottom_of(0) <= rect.height {
            return 0;
        }
        for h in 1..=t {
            if bottom_of(h) <= rect.height {
                return h;
            }
        }
        // Even maximal windowing leaves the row below the fold; hide all we
        // can and let the caller's panel-scroll fallback cover the rest.
        t
    }

    /// Force the milestone group containing `(repo, number)` open, so that
    /// issue's row is actually emitted by the next `rebuild_board_sidebar`.
    fn expand_board_milestone_for(&mut self, repo: &str, number: u64) {
        let milestone_key = {
            let cache = &self.board_issues_cache;
            let milestones = self.board_milestones_for_repo(cache, repo, true);
            milestones
                .iter()
                .find(|(_, _, group_issues)| {
                    group_issues.iter().any(|(_, g)| g.issue_number == number)
                })
                .map(|(m_key, _, _)| m_key.clone())
        };
        if let Some(m_key) = milestone_key {
            self.board_milestone_expanded
                .insert((repo.to_string(), m_key), true);
        }
    }

    /// Height, in main-axis units, of one Board sidebar row.
    ///
    /// Mirrors quadraui's `sidebar_system::body_measure`: a
    /// `Decoration::Header` row measures `lh * 1.2`, everything else
    /// `lh * 1.4`, both rounded. Getting this wrong does not misplace the row
    /// visually — quadraui still owns the paint — it just makes
    /// reveal-on-activate scroll to the wrong offset.
    fn board_row_height(lh: f32, is_header: bool) -> f32 {
        if is_header {
            (lh * 1.2).round()
        } else {
            (lh * 1.4).round()
        }
    }

    /// Content height of Board sidebar section `s` (excluding its own header
    /// row), as quadraui's `ScrollMode::WholePanel` sizing computes it.
    fn board_section_content_height(&self, s: usize, lh: f32) -> f32 {
        if self.board_sidebar.is_collapsed(s) {
            return 0.0;
        }
        if let Some(form) = self.board_sidebar.form(s) {
            return form.fields.len() as f32 * Self::board_row_height(lh, false);
        }
        self.board_section_rows
            .get(s)
            .map(|rows| {
                rows.iter()
                    .map(|(_, is_header)| Self::board_row_height(lh, *is_header))
                    .sum()
            })
            .unwrap_or(0.0)
    }

    /// Contract §2f, the half a selection marker alone does not prove: scroll
    /// the Board sidebar so the currently-selected row is inside the viewport.
    ///
    /// The Board sidebar runs in `ScrollMode::WholePanel`, so the whole
    /// section stack scrolls as one and `SidebarSystem::set_panel_scroll` is
    /// the only lever — there is no per-tree scroll setter on `SidebarSystem`
    /// (that is quadraui#595's `SidebarSystem::reveal`, which is not landed at
    /// any pushed quadraui rev; when it is, this method becomes a delegation).
    /// The offset is therefore reconstructed here from the panel's own row
    /// model plus the backend metrics stashed at paint time.
    ///
    /// Scrolls the minimum distance that makes the row visible — down to the
    /// row's bottom edge when it is below the fold, up to its top edge when it
    /// is above — and is a no-op when the row is already on screen. quadraui
    /// clamps the stored value to `[0, total - viewport]` when it lays out, so
    /// an over-shot value can never scroll the panel past its own end.
    fn scroll_board_selection_into_view(&mut self) {
        let Some((rect, lh, header_size)) = self.last_sidebar_geom.get() else {
            return;
        };
        if rect.height <= 0.0 || lh <= 0.0 {
            return;
        }
        let Some(section) = self.board_sidebar.active_section() else {
            return;
        };
        let Some(path) = self.board_sidebar.selected_path(section).cloned() else {
            return;
        };

        // Walk the section stack down to the target section's body. Hidden
        // sections are dropped from the view entirely (`build_view` skips
        // them), so they cost nothing.
        let mut y = 0.0_f32;
        for s in 0..section {
            if !self.board_sidebar.is_section_visible(s) {
                continue;
            }
            y += header_size + self.board_section_content_height(s, lh);
        }
        if !self.board_sidebar.is_section_visible(section) {
            return;
        }
        y += header_size;

        // …then across the rows above the target inside that section.
        let Some(rows) = self.board_section_rows.get(section) else {
            return;
        };
        let mut row_h = None;
        for (row_path, is_header) in rows {
            let h = Self::board_row_height(lh, *is_header);
            if *row_path == path {
                row_h = Some(h);
                break;
            }
            y += h;
        }
        let Some(row_h) = row_h else {
            return;
        };

        let scroll = self.board_sidebar.panel_scroll();
        let new = if y < scroll {
            y
        } else if y + row_h > scroll + rect.height {
            y + row_h - rect.height
        } else {
            return;
        };
        self.board_sidebar.set_panel_scroll(new.max(0.0));
    }

    /// Return the Proposal currently selected in the sidebar's PROPOSALS section.
    fn board_selected_proposal(&self) -> Option<&Proposal> {
        if !self.has_proposals_section {
            return None;
        }
        let section = self.board_sidebar.active_section()?;
        // Proposals section is at index 1 (after the search form).
        if section != 1 {
            return None;
        }
        let path = self.board_sidebar.selected_path(1)?;
        if path.is_empty() {
            return None;
        }
        self.data.proposals.get(path[0] as usize)
    }

    /// Return the failed Assignment currently selected in the board sidebar.
    fn board_selected_failed_assignment(&self) -> Option<&Assignment> {
        let group = self.board_selected_issue_group()?;
        let failed = group.assignments.iter().find(|a| a.status == "failed")?;
        Some(failed)
    }

    /// Return `true` when the currently selected row in the Board sidebar is
    /// within the "Completed" (done/merged) status group.
    ///
    /// Used as the guard condition for the 'P' purge keybind: the action is
    /// only available when the user has navigated into the Done section,
    /// preventing accidental purges from other views.
    fn board_selection_in_completed_group(&self) -> bool {
        let section = match self.board_sidebar.active_section() {
            Some(s) => s,
            None => return false,
        };
        let offset = self.board_repo_offset();
        if section < offset {
            return false;
        }
        let path = match self.board_sidebar.selected_path(section) {
            Some(p) => p,
            None => return false,
        };
        // #410: issue rows are at path depth 2 — [milestone_idx, issue_idx].
        if path.len() < 2 {
            return false;
        }
        // Check lifecycle of the actually-selected issue.
        match self.board_selected_issue_group() {
            Some(g) => g.lifecycle_section() == "completed",
            None => false,
        }
    }

    /// Try to select a specific issue in the sidebar by repo and issue number.
    ///
    /// #2452: every exit that fails to resolve `(repo, issue_number)` to a
    /// row in the CURRENT (possibly `board_search`-filtered) tree explicitly
    /// clears whatever row is already highlighted in the *target repo's own*
    /// section, via `clear_board_tree_selection`, instead of silently
    /// leaving it stand — same "explicit no-selection" fix
    /// `rebuild_pipeline_sidebar` applies for the identical Pipeline-side
    /// bug. This matters most for `reveal_board_active_doc`, whose own
    /// preceding `rebuild_board_sidebar` pass may already have restored a
    /// *different*, still-matching issue's selection in some OTHER,
    /// currently-focused section before this call runs — Board's sidebar is
    /// a multi-section accordion where every repo section keeps its own
    /// selection independent of which section currently has keyboard focus,
    /// so the not-found path must operate on `section_idx` (the target
    /// repo's own section), never on whatever `active_section()` happens to
    /// be at call time, or it would both leave the target repo's section
    /// showing stale/no feedback AND wipe out an unrelated section's
    /// perfectly valid selection. Every other call site (`select_issue` is
    /// only ever used to jump to a specific issue: the issue finder's Board
    /// fallback, Queue's "View on Board", the Kanban card click, and
    /// `rebuild_board_sidebar`'s own previous-selection restore) already
    /// wants "not found" to mean "nothing is selected in that repo's
    /// section", so generalizing costs nothing.
    fn select_issue(&mut self, repo: &str, issue_number: u64) {
        let offset = self.board_repo_offset();
        // Find the repo's section index.
        let cache_idx = match self.board_repo_names.iter().position(|r| r == repo) {
            Some(i) => i,
            None => {
                // #2452: `repo` has no section in the sidebar at all —
                // `board_repo_names` is built from the UNFILTERED board
                // cache (`rebuild_board_sidebar`), so this only happens when
                // the repo genuinely has zero board issues, never as a
                // `board_search` side effect. There is no section to move
                // focus to or to clear, so leave whatever section is
                // currently focused/selected untouched rather than clearing
                // an unrelated repo's valid selection.
                return;
            }
        };
        let section_idx = cache_idx + offset;
        // Clone to avoid borrow conflicts.
        let cache = self.board_issues_cache.clone();
        // #410: milestone > issue bucketing — must stay in sync with
        // `board_milestones_for_repo` and `rebuild_board_sidebar`
        // so the path this function produces matches what the click handler resolves to.
        let milestones = self.board_milestones_for_repo(&cache, repo, true);
        for (milestone_idx, (_, _, group_issues)) in milestones.iter().enumerate() {
            for (issue_idx, (_, g)) in group_issues.iter().enumerate() {
                if g.issue_number == issue_number {
                    self.board_sidebar.set_active_section(Some(section_idx));
                    self.board_sidebar.set_selected_path(
                        section_idx,
                        Some(vec![milestone_idx as u16, issue_idx as u16]),
                    );
                    return;
                }
                // #1270: `issue_number` might be a child nested under this
                // epic row (suppressed as a flat sibling — see
                // `board_globally_nested_children`) rather than a flat row
                // of its own. Fall back to a depth-3 path pointing at the
                // nested child before giving up on this repo.
                let children = self.epic_child_rows_for(repo, g.issue_number);
                if let Some(child_idx) = children.iter().position(|c| c.number == issue_number) {
                    self.board_sidebar.set_active_section(Some(section_idx));
                    self.board_sidebar.set_selected_path(
                        section_idx,
                        Some(vec![milestone_idx as u16, issue_idx as u16, child_idx as u16]),
                    );
                    return;
                }
            }
        }
        // #2452: not found under `board_search` in the target repo's own
        // section (filtered out, or otherwise absent). Move keyboard focus
        // to that section — mirroring the "found" branches above, so the
        // visible/focused section always agrees with the active document —
        // and clear ITS selection explicitly by index, never by reading back
        // whatever `active_section()` was before this call.
        self.board_sidebar.set_active_section(Some(section_idx));
        self.clear_board_tree_selection(section_idx);
    }

    /// #2452: clear whatever row is highlighted in sidebar `section`,
    /// without touching `board_search`. Takes an explicit section index
    /// rather than deriving one from `active_section()` — Board's sidebar is
    /// a multi-section accordion where every repo section keeps its own
    /// selection independent of keyboard focus, so deriving the section from
    /// `active_section()` would silently clear the wrong repo's selection on
    /// a multi-repo board whenever the target repo isn't the one currently
    /// focused. The shared tail every `select_issue` not-found exit funnels
    /// through, always after `set_active_section(Some(section))` so focus
    /// and clearing agree on the same section.
    fn clear_board_tree_selection(&mut self, section: usize) {
        self.board_sidebar.set_selected_path(section, None);
    }

    /// Clamp `machine_scroll` so that `machine_sel` is inside the visible window.
    fn fix_machine_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        if self.machine_sel < self.machine_scroll {
            self.machine_scroll = self.machine_sel;
        } else if self.machine_sel >= self.machine_scroll + visible {
            self.machine_scroll = self.machine_sel + 1 - visible;
        }
    }

    /// Keep the selected merge-queue entry inside the visible window.
    ///
    /// Must be called after every j/k/Home/End navigation in the Merge Queue
    /// panel.  `visible` is the number of rows the list widget can display
    /// (pass `content_visible_rows(rect, lh)` from the render path, or a
    /// reasonable constant like 10 in tests).  The quadraui list widget's
    /// `layout()` skips rows above `scroll_offset`, so `merge_queue_scroll`
    /// must track the display-index of the selected entry (accounting for
    /// milestone-group header rows that precede it in the items array).
    /// Same structural pattern as `fix_machine_scroll`.
    fn fix_merge_queue_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        let sel = self.merge_queue_display_idx_for_sel();
        if sel < self.merge_queue_scroll {
            self.merge_queue_scroll = sel;
        } else if sel >= self.merge_queue_scroll + visible {
            self.merge_queue_scroll = sel + 1 - visible;
        }
    }

    /// Return the display-list index for the currently selected merge-queue
    /// entry, accounting for group header rows inserted before each group in
    /// the render functions.
    ///
    /// When `data.merge_plan` is non-empty (#776 path), groups are by
    /// `(repo_github, target_branch)` using **consecutive-run** counting —
    /// matching `render_merge_plan_panel` which emits a new header each time
    /// the current entry's group differs from the previous one (not just on
    /// the first occurrence of each group).  When only `data.merge_queue` is
    /// available (legacy path), groups are by `milestone_title` (first-seen).
    /// Adding the header count to `sel` gives the row index the selected
    /// entry occupies in the rendered items array.
    fn merge_queue_display_idx_for_sel(&self) -> usize {
        if !self.data.merge_plan.is_empty() {
            // #776 plan path: groups are (repo_github, target_branch).
            // Use consecutive-run counting to match render_merge_plan_panel,
            // which emits a header on every group transition — including when
            // the same repo/branch pair reappears after an intervening entry.
            let entries = &self.data.merge_plan;
            let sel = self.merge_queue_sel.min(entries.len() - 1);
            let mut headers = 0usize;
            let mut last_key: Option<(&str, &str)> = None;
            for entry in entries.iter().take(sel + 1) {
                let key = (entry.repo_github.as_str(), entry.target_branch.as_str());
                if last_key != Some(key) {
                    headers += 1;
                    last_key = Some(key);
                }
            }
            sel + headers
        } else {
            // Legacy path: groups are milestone_title (first-seen).
            let entries = &self.data.merge_queue;
            if entries.is_empty() {
                return 0;
            }
            let sel = self.merge_queue_sel.min(entries.len() - 1);
            let mut seen: Vec<Option<String>> = Vec::new();
            for entry in entries.iter().take(sel + 1) {
                if !seen.contains(&entry.milestone_title) {
                    seen.push(entry.milestone_title.clone());
                }
            }
            sel + seen.len()
        }
    }

    // ── Widget builders ──────────────────────────────────────────────────

    fn machines_list(&self, has_focus: bool) -> ListView {
        let items: Vec<ListItem> = self
            .data
            .machines
            .iter()
            .map(|m| {
                let (col, bullet) = if m.reachable {
                    (Color::rgb(70, 210, 70), "● ")
                } else {
                    (Color::rgb(90, 90, 90), "○ ")
                };
                let is_local = m.name == self.data.local_machine;
                let display_name = if is_local {
                    format!("{} (local)", trunc(&m.name, 12))
                } else {
                    trunc(&m.name, 18).to_string()
                };

                // Version badge: green if same as local, red if older/different, gray if unknown.
                let (ver_str, ver_col) = match (&m.version, &self.local_coord_version) {
                    (Some(v), Some(local)) if v != local => {
                        (format!(" v{}", v), Color::rgb(220, 80, 80))
                    }
                    (Some(v), _) => (format!(" v{}", v), Color::rgb(80, 160, 80)),
                    (None, _) => ("".to_string(), Color::rgb(90, 90, 90)),
                };

                // Last-contact age ("12s ago") when we have a recorded time.
                let contact_str = self.machine_last_contact.get(&m.name).map(|t| {
                    let secs = t.elapsed().as_secs();
                    format!(" {}ago", fmt_dur(secs))
                });

                let mut spans = vec![
                    StyledSpan::with_fg(bullet, col),
                    StyledSpan::plain(&display_name),
                ];
                if !ver_str.is_empty() {
                    spans.push(StyledSpan::with_fg(&ver_str, ver_col));
                }
                // #pause: badge a paused machine so the user sees at a
                // glance which rows are excluded from routing.  Amber so
                // it doesn't read as a failure (machine is healthy, just
                // deliberately offline for new work).
                //
                // #1862: a quiet-hours pause gets its own badge/color — an
                // operator debugging a stalled queue at 1AM needs to tell
                // "asleep until 08:00" (nothing to do) from "someone
                // paused this" (maybe worth investigating) at a glance.
                // Blue so it reads as scheduled/expected rather than the
                // amber "someone did this" signal.
                //
                // #2101: a release cordon gets the third badge, checked
                // FIRST because it is the most specific claim about why the
                // machine is excluded. "Work stopping with no stated reason
                // is the thing this fleet keeps doing to itself" — a machine
                // draining so the fleet can upgrade itself must not read as
                // one a human paused, because `coord unpause` (the remedy
                // that badge implies) neither applies nor works on it.
                // Violet: neither the amber "someone did this" nor the blue
                // "scheduled and will lift itself on the clock".
                if self.cordoned_machines.contains(&m.name) {
                    spans.push(StyledSpan::with_fg(" [DRAINING]", Color::rgb(170, 130, 240)));
                } else if self.quiet_paused_machines.contains(&m.name) {
                    // #2147: append the schedule (and lighten the color for
                    // a `coordinator.yml`-sourced window vs. an
                    // operator-set one) when known; `quiet_hours_windows`
                    // can lag `quiet_paused_machines` by up to one poll
                    // (different fields of the same `GET /pause` fetch, but
                    // an older daemon populates only the latter), so a bare
                    // `[QUIET]` is still the correct fallback rather than
                    // blocking the badge on it.
                    let (label, color) = match self.quiet_hours_windows.get(&m.name) {
                        Some(w) if w.source == "store" => (
                            format!(" [QUIET {}-{}]", w.start, w.end),
                            Color::rgb(120, 190, 255),
                        ),
                        Some(w) => (
                            format!(" [QUIET {}-{}]", w.start, w.end),
                            Color::rgb(90, 160, 230),
                        ),
                        None => (" [QUIET]".to_string(), Color::rgb(90, 160, 230)),
                    };
                    spans.push(StyledSpan::with_fg(label, color));
                } else if self.paused_machines.contains(&m.name) {
                    spans.push(StyledSpan::with_fg(" [PAUSED]", Color::rgb(230, 180, 60)));
                }

                let text = StyledText { spans };

                // #628: live interactive sessions (tmux) are not board `running`
                // rows, so active_count misses them.  Surface them here so a
                // machine hosting detached sessions no longer reads "idle".
                let live = self.live_session_count_for_machine(&m.name);
                let detail_str = if m.active_count > 0 || live > 0 {
                    let mut parts: Vec<String> = Vec::new();
                    if m.active_count > 0 {
                        parts.push(format!("{} active", m.active_count));
                    }
                    if live > 0 {
                        parts.push(format!("◉ {} live", live));
                    }
                    parts.join(" · ")
                } else if let Some(ref age) = contact_str {
                    age.clone()
                } else {
                    "idle".to_string()
                };
                let detail_col = if live > 0 {
                    Color::rgb(150, 210, 255) // matches the status-bar live badge
                } else if m.active_count > 0 {
                    Color::rgb(80, 210, 80)
                } else if contact_str.is_some() {
                    Color::rgb(90, 110, 90)
                } else {
                    Color::rgb(90, 90, 90)
                };
                let detail = Some(StyledText {
                    spans: vec![StyledSpan::with_fg(&detail_str, detail_col)],
                });
                ListItem {
                    text,
                    icon: None,
                    detail,
                    decoration: Decoration::Normal,
                }
            })
            .collect();

        let n = self.data.machines.len();
        ListView {
            id: WidgetId::new("machines"),
            title: Some(StyledText::plain(format!(" MACHINES ({}) ", n))),
            items,
            selected_idx: if n > 0 { self.machine_sel } else { 0 },
            scroll_offset: self.machine_scroll,
            has_focus,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    fn detail_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();

        // If a proposal is selected, show proposal detail instead.
        if let Some(p) = self.board_selected_proposal() {
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        format!(" Proposal #{} ", p.id),
                        Color::rgb(210, 220, 255),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });
            items.push(kv_item("  Machine", &format!("  {}", p.machine), None));
            items.push(kv_item("  Repo", &format!("  {}", p.repo), None));
            items.push(kv_item(
                "  Issue",
                &format!("  #{}: {}", p.issue_number, p.issue_title),
                None,
            ));
            items.push(kv_item("  Type", &format!("  {}", p.proposal_type), None));
            items.push(kv_item("", "", None));
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        " RATIONALE ",
                        Color::rgb(130, 130, 150),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });
            for line in p.rationale.lines() {
                items.push(kv_item("", &format!("  {}", line), None));
            }
            items.push(kv_item("", "", None));
            items.push(kv_item(
                "",
                "  a=approve  A=approve all",
                Some(Color::rgb(180, 180, 120)),
            ));
            return ListView {
                id: WidgetId::new("detail"),
                title: Some(StyledText::plain(&format!("Proposal #{}", p.id))),
                items,
                selected_idx: 0,
                scroll_offset: self.detail_scroll,
                has_focus: false,
                bordered: false,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
            };
        }

        // #2282 (ms-65 §2): the detail pane follows the ACTIVE DOCUMENT TAB,
        // not the tree cursor — moving the sidebar selection no longer loses
        // the issue you were reading. With zero tabs open this is exactly the
        // pre-ms-65 selection-follows-tree behaviour.
        match self.board_detail_issue_group() {
            None => {
                items.push(kv_item("", " No issue selected", None));
            }
            Some(group) => {
                let repo = self.board_detail_repo().unwrap_or("?");

                // Section header
                let header_text = format!(" {} #{} ", repo, group.issue_number);
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(&header_text, Color::rgb(210, 220, 255))],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                // Issue title
                items.push(kv_item(
                    "",
                    &format!("  {}", trunc(&group.issue_title, 52)),
                    None,
                ));
                items.push(kv_item("", "", None)); // blank separator

                // Pipeline stages sub-header
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            " PIPELINE STAGES ",
                            Color::rgb(130, 130, 150),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                // Show actual pipeline stages from assignments (ordered by dispatched_at).
                for a in &group.assignments {
                    let type_label = match a.assignment_type.as_deref() {
                        Some("review") => "Review",
                        Some("smoke") => "Smoke",
                        Some("plan") => "Plan",
                        _ => "Work",
                    };
                    items.push(pipeline_stage_item(type_label, Some(a)));
                }

                // PR/Merge stage from merge_queue (only if entry exists).
                if let Some(mq_entry) = self
                    .data
                    .merge_queue
                    .iter()
                    .find(|e| e.issue_number == Some(group.issue_number))
                {
                    items.push(pipeline_merge_item(Some(mq_entry)));
                }

                items.push(kv_item("", "", None)); // blank separator

                // Per-assignment detail section
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            " ASSIGNMENTS ",
                            Color::rgb(130, 130, 150),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                for a in &group.assignments {
                    let sc = a.status_color(&self.active_theme);
                    let type_label = a.assignment_type.as_deref().unwrap_or("work");
                    let text = StyledText {
                        spans: vec![
                            StyledSpan::with_fg(
                                format!("  {:<8}", type_label),
                                Color::rgb(180, 180, 200),
                            ),
                            StyledSpan::with_fg(a.status_label(), sc),
                            StyledSpan::with_fg(
                                format!("  {}", trunc(&a.machine, 15)),
                                Color::rgb(160, 160, 160),
                            ),
                            StyledSpan::with_fg(
                                format!("  {}", a.age_str()),
                                Color::rgb(100, 100, 100),
                            ),
                        ],
                    };
                    items.push(ListItem {
                        text,
                        icon: None,
                        detail: Some(StyledText {
                            spans: vec![StyledSpan::with_fg(
                                trunc(&a.id, 8),
                                Color::rgb(100, 100, 120),
                            )],
                        }),
                        decoration: if a.status == "failed" {
                            Decoration::Error
                        } else {
                            Decoration::Normal
                        },
                    });

                    // Show branch if present
                    if let Some(b) = &a.branch {
                        items.push(kv_item("    Branch", trunc(b, 40), None));
                    }
                    if let Some(m) = &a.model {
                        items.push(kv_item("    Model", trunc(m, 30), None));
                    }
                    if let Some(code) = a.exit_code {
                        let (s, c) = if code == 0 {
                            (format!("{} (ok)", code), Some(Color::rgb(80, 210, 80)))
                        } else {
                            (format!("{} (err)", code), Some(Color::rgb(210, 70, 70)))
                        };
                        items.push(kv_item("    Exit", &s, c));
                    }
                    if let (Some(start), Some(end)) = (a.dispatched_at, a.finished_at) {
                        let dur = (end - start).max(0.0) as u64;
                        items.push(kv_item("    Duration", &fmt_dur(dur), None));
                    }
                }
            }
        }

        let title = match self.board_detail_issue_group() {
            Some(group) => {
                let repo = self.board_detail_repo().unwrap_or("?");
                format!(" {} #{} ", repo, group.issue_number)
            }
            None => " DETAIL ".to_string(),
        };

        ListView {
            id: WidgetId::new("detail"),
            title: Some(StyledText::plain(&title)),
            items,
            selected_idx: 0,
            // #2288 (§9): a background pane scrolls independently of the
            // focused one — see `board_pane_detail_scroll`.
            scroll_offset: self.board_pane_detail_scroll(),
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Return log items for `id`, reading locally or fetching from the remote agent.
    ///
    /// This method **never blocks** the UI thread:
    ///
    /// 1. If a local log file exists, parse and return it immediately.
    /// 2. Drain any completed background fetch from `pending_log_fetches`; on
    ///    success update `remote_log_cache` and return the parsed items.
    /// 3. Return cached items if the 30-second TTL has not expired.
    /// 4. If a fetch is already in flight, return a "Loading log…" placeholder.
    /// 5. Otherwise spawn a background fetch (via [`spawn_log_fetch`]) and
    ///    return a "Loading log…" placeholder; the result will be picked up on
    ///    the next render pass.
    fn get_activity_log(&self, id: &str, machine_name: &str) -> Vec<ListItem> {
        // 1. Local file takes priority — fast path, no network involved.
        let path = coord_dir().join("logs").join(format!("{}.log", id));
        if path.exists() {
            return load_activity_log(id);
        }

        // 2. Drain any completed background fetch for this ID.
        //    We borrow, call try_recv(), then drop the borrow before touching
        //    `remote_log_cache` to avoid a double-borrow panic.
        let pending_result = {
            let pending = self.pending_log_fetches.borrow();
            pending.get(id).map(|rx| rx.try_recv())
        };
        if let Some(recv_result) = pending_result {
            match recv_result {
                Ok(fetch_result) => {
                    self.pending_log_fetches.borrow_mut().remove(id);
                    let items = match fetch_result {
                        Ok(content) => parse_log_content(&content),
                        Err(e) => vec![kv_item(
                            "",
                            &format!("  Log unavailable: {}", e),
                            Some(Color::rgb(100, 100, 100)),
                        )],
                    };
                    self.remote_log_cache
                        .borrow_mut()
                        .insert(id.to_string(), (Instant::now(), items.clone()));
                    return items;
                }
                Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                    // Thread died without sending; remove so we retry below.
                    self.pending_log_fetches.borrow_mut().remove(id);
                }
                Err(std::sync::mpsc::TryRecvError::Empty) => {} // still in flight
            }
        }

        // 3. Cache hit (within the configured TTL). Short TTL makes the Watch
        // overlay feel like tail -f rather than a periodic poll. TTL is
        // user-configurable in the Settings panel (default 2 s).
        {
            let cache = self.remote_log_cache.borrow();
            if let Some((fetched_at, items)) = cache.get(id) {
                if fetched_at.elapsed() < self.settings.log_cache_ttl.as_duration() {
                    return items.clone();
                }
            }
        }

        // 4. Fetch still in flight — return placeholder so the render doesn't block.
        if self.pending_log_fetches.borrow().contains_key(id) {
            return vec![kv_item(
                "",
                "  Loading log…",
                Some(Color::rgb(140, 140, 140)),
            )];
        }

        // 5. Cache cold/stale and no fetch in flight. Look up host and spawn.
        let host = match self.data.machines.iter().find(|m| m.name == machine_name) {
            Some(m) if !m.host.is_empty() => m.host.clone(),
            _ => {
                return vec![kv_item(
                    "",
                    "  Log unavailable: machine host unknown",
                    Some(Color::rgb(100, 100, 100)),
                )];
            }
        };
        let rx = spawn_log_fetch(&host, id);
        self.pending_log_fetches
            .borrow_mut()
            .insert(id.to_string(), rx);
        vec![kv_item(
            "",
            "  Loading log…",
            Some(Color::rgb(140, 140, 140)),
        )]
    }

    /// Detail panel for the selected machine: status, version, workers, disk, history.
    fn machine_detail_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();

        match self.data.machines.get(self.machine_sel) {
            None => {
                items.push(kv_item("", " No machine selected", None));
            }
            Some(m) => {
                // ── Header ──────────────────────────────────────────────
                let header_text = format!(" {} ", m.name);
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(&header_text, Color::rgb(210, 220, 255))],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                items.push(kv_item("", "", None)); // blank

                // ── Status + last-contact age ────────────────────────────
                let last_contact_str = self
                    .machine_last_contact
                    .get(&m.name)
                    .map(|t| format!("  ({} ago)", fmt_dur(t.elapsed().as_secs())));
                let (reach_str, reach_col) = if m.reachable {
                    ("reachable".to_string(), Color::rgb(80, 210, 80))
                } else {
                    ("unreachable".to_string(), Color::rgb(220, 70, 70))
                };
                let reach_val = if let Some(ref age) = last_contact_str {
                    format!("{}{}", reach_str, age)
                } else {
                    reach_str
                };
                items.push(kv_item("Status", &reach_val, Some(reach_col)));

                // ── Agent version (red if older than local) ──────────────
                let (ver_val, ver_col) = match (&m.version, &self.local_coord_version) {
                    (Some(v), Some(local)) if v != local => (
                        format!("{}  ← local is {}", v, local),
                        Color::rgb(220, 80, 80),
                    ),
                    (Some(v), Some(_)) => (v.clone(), Color::rgb(80, 160, 80)),
                    (Some(v), None) => (v.clone(), Color::rgb(150, 150, 150)),
                    (None, _) => ("unknown".to_string(), Color::rgb(90, 90, 90)),
                };
                items.push(kv_item("Version", &ver_val, Some(ver_col)));

                // ── Worktree disk usage ──────────────────────────────────
                let wt_mb = m.worktree_bytes as f64 / (1024.0 * 1024.0);
                let wt_str = if m.worktree_bytes == 0 && !m.reachable {
                    "—".to_string()
                } else {
                    format!("{:.1} MB", wt_mb)
                };
                items.push(kv_item("Worktrees", &wt_str, None));

                // ── Local indicator ──────────────────────────────────────
                let is_local = m.name == self.data.local_machine;
                if is_local {
                    items.push(kv_item(
                        "Location",
                        "local",
                        Some(Color::rgb(100, 180, 240)),
                    ));
                }

                items.push(kv_item("", "", None)); // blank

                // ── Active workers ───────────────────────────────────────
                let active_workers: Vec<&Assignment> = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| a.machine == m.name && a.status == "running")
                    .collect();

                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!(" ACTIVE WORKERS ({}) ", active_workers.len()),
                            Color::rgb(130, 130, 150),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                if active_workers.is_empty() {
                    items.push(kv_item("", "  idle", Some(Color::rgb(80, 80, 80))));
                } else {
                    for a in &active_workers {
                        let id8 = trunc(&a.id, 8);
                        let type_label = a.assignment_type.as_deref().unwrap_or("work");
                        let text = StyledText {
                            spans: vec![
                                StyledSpan::with_fg(
                                    format!("  {} ", id8),
                                    Color::rgb(100, 100, 180),
                                ),
                                StyledSpan::with_fg(
                                    format!("#{:<6}", a.issue_number),
                                    Color::rgb(150, 150, 240),
                                ),
                                StyledSpan::with_fg(
                                    format!("{:<8}", type_label),
                                    Color::rgb(150, 180, 150),
                                ),
                                StyledSpan::with_fg(
                                    format!("  {}", trunc(&a.repo, 18)),
                                    Color::rgb(110, 110, 110),
                                ),
                            ],
                        };
                        items.push(ListItem {
                            text,
                            icon: None,
                            detail: Some(StyledText {
                                spans: vec![StyledSpan::with_fg(
                                    a.age_str(),
                                    Color::rgb(90, 90, 90),
                                )],
                            }),
                            decoration: Decoration::Normal,
                        });
                    }
                }

                items.push(kv_item("", "", None)); // blank

                // ── Job history ──────────────────────────────────────────
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            " JOB HISTORY ",
                            Color::rgb(130, 130, 150),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });

                let machine_jobs: Vec<&Assignment> = self
                    .data
                    .assignments
                    .iter()
                    .filter(|a| a.machine == m.name)
                    .take(20)
                    .collect();

                if machine_jobs.is_empty() {
                    items.push(kv_item("", "  No jobs found", None));
                } else {
                    for a in machine_jobs {
                        let sc = a.status_color(&self.active_theme);
                        let issue = format!("  #{:<5}", a.issue_number);
                        let repo = format!("{:<15}", trunc(&a.repo, 15));
                        let st = a.status_label();
                        let text = StyledText {
                            spans: vec![
                                StyledSpan::with_fg(&issue, Color::rgb(150, 150, 240)),
                                StyledSpan::plain(&repo),
                                StyledSpan::with_fg(st, sc),
                            ],
                        };
                        let detail = Some(StyledText {
                            spans: vec![StyledSpan::with_fg(
                                a.age_str(),
                                Color::rgb(100, 100, 100),
                            )],
                        });
                        items.push(ListItem {
                            text,
                            icon: None,
                            detail,
                            decoration: if a.status == "failed" {
                                Decoration::Error
                            } else {
                                Decoration::Normal
                            },
                        });
                    }
                }

                // ── Action hints ─────────────────────────────────────────
                if m.reachable {
                    items.push(kv_item("", "", None));
                    items.push(kv_item(
                        "",
                        "  r=restart  u=update  c=clean worktrees",
                        Some(Color::rgb(140, 140, 100)),
                    ));
                }
            }
        }

        let title = match self.data.machines.get(self.machine_sel) {
            Some(m) => format!(" DETAIL — {} ", m.name),
            None => " DETAIL ".to_string(),
        };

        ListView {
            id: WidgetId::new("machine-detail"),
            title: Some(StyledText::plain(&title)),
            items,
            selected_idx: 0,
            scroll_offset: self.machine_detail_scroll,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    // ── #1032: Sessions panel main-pane detail ────────────────────────────

    /// Detail panel for the session leaf selected in the Sessions-view tree
    /// (`selected_fleet_session`, `fleet_sessions.rs`): assignment id,
    /// issue/repo, machine, type, and attached/dead status. Shows a
    /// placeholder when nothing is selected or the selection is a
    /// machine/repo row rather than a session leaf — this slice is
    /// read-only nav/select, no actions live here yet (#1032).
    fn sessions_detail_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        match self.selected_fleet_session() {
            None => {
                items.push(kv_item(
                    "",
                    " Select a session to see its details",
                    Some(Color::rgb(140, 140, 140)),
                ));
            }
            Some(s) => {
                let header_text = format!(" {} ", s.assignment_id);
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(&header_text, Color::rgb(210, 220, 255))],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                });
                items.push(kv_item("", "", None)); // blank

                let issue_val = s
                    .issue_number
                    .map(|n| format!("#{}", n))
                    .unwrap_or_else(|| "(unknown)".to_string());
                items.push(kv_item("Issue", &issue_val, None));
                items.push(kv_item(
                    "Repo",
                    s.repo_name.as_deref().unwrap_or("(unknown)"),
                    None,
                ));
                items.push(kv_item("Machine", &self.session_machine_for(s), None));
                items.push(kv_item("Type", &self.session_type_for(s), None));
                let (status_val, status_col) = if s.pane_dead {
                    ("dead pane".to_string(), Color::rgb(220, 80, 80))
                } else if s.attached {
                    ("attached".to_string(), Color::rgb(140, 200, 140))
                } else {
                    ("detached".to_string(), Color::rgb(190, 200, 215))
                };
                items.push(kv_item("Status", &status_val, Some(status_col)));
            }
        }
        ListView {
            id: WidgetId::new("sessions-detail"),
            title: None,
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

    // ── #207: Machine metrics sparklines ─────────────────────────────────

    /// Render CPU and memory sparklines for the currently-selected machine
    /// into `area`.  The area is split vertically: top half = CPU sparkline
    /// with a label row, bottom half = memory sparkline with a label row.
    ///
    /// When no samples have been collected yet (e.g. the machine is
    /// unreachable or the user just switched to the Machines panel), a
    /// subdued placeholder is drawn instead so the layout stays stable.
    fn render_machine_sparklines(&self, backend: &mut dyn Backend, area: Rect, lh: f32) {
        if area.height < lh * 2.0 {
            return; // Not enough vertical room to be useful.
        }

        let machine_name = match self.data.machines.get(self.machine_sel) {
            Some(m) => &m.name,
            None => return,
        };

        // #2088: iterate the deque in logical order (`iter()`/`back()`)
        // rather than `as_slices().0`, which is only the whole buffer
        // before the ring has wrapped. Once `pop_front` has run once,
        // the front slice is the *older* portion and shrinks over time,
        // making `.last()` on it a stale sample rather than the newest.
        let empty = std::collections::VecDeque::new();
        let samples: &std::collections::VecDeque<MetricSample> = self
            .machine_metrics
            .get(machine_name)
            .unwrap_or(&empty);

        // Split area into top (CPU) and bottom (Mem) halves.
        let half_h = area.height / 2.0;
        let cpu_area = Rect::new(area.x, area.y, area.width, half_h);
        let mem_area = Rect::new(area.x, area.y + half_h, area.width, half_h);

        // Render CPU row.
        let cpu_data: Vec<f64> = samples.iter().map(|s| s.cpu as f64).collect();
        let last_cpu = samples.back().map(|s| s.cpu).unwrap_or(0.0);
        self.render_metric_sparkline(
            backend, cpu_area, lh,
            "CPU",
            last_cpu,
            cpu_data,
            Color::rgb(80, 160, 240),
        );

        // Render memory row.
        let mem_data: Vec<f64> = samples.iter().map(|s| s.mem as f64).collect();
        let last_mem = samples.back().map(|s| s.mem).unwrap_or(0.0);
        self.render_metric_sparkline(
            backend, mem_area, lh,
            "Mem",
            last_mem,
            mem_data,
            Color::rgb(120, 200, 120),
        );
    }

    /// Draw a single labelled sparkline row.
    ///
    /// Layout: one `lh`-tall label+value row on top, the rest of the
    /// rect for the sparkline chart body.  When `data` is empty a
    /// placeholder "—" value is shown and no chart is drawn.
    fn render_metric_sparkline(
        &self,
        backend: &mut dyn Backend,
        area: Rect,
        lh: f32,
        label: &str,
        last_val: f32,
        data: Vec<f64>,
        color: Color,
    ) {
        if area.height < lh {
            return;
        }
        let label_h = lh;
        let chart_h = (area.height - label_h).max(0.0);
        let label_rect = Rect::new(area.x, area.y, area.width, label_h);
        let chart_rect = Rect::new(area.x, area.y + label_h, area.width, chart_h);

        // Label row — "CPU  42%" or "Mem  67%" with subdued colouring.
        let val_str = if data.is_empty() {
            "  —".to_string()
        } else {
            format!("  {:.0}%", last_val)
        };
        backend.draw_list(label_rect, &ListView {
            id: WidgetId::new(format!("metric-label-{}", label.to_lowercase())),
            title: None,
            items: vec![ListItem {
                text: StyledText {
                    spans: vec![
                        StyledSpan::with_fg(
                            format!(" {} ", label),
                            Color::rgb(120, 120, 140),
                        ),
                        StyledSpan::with_fg(val_str, color),
                    ],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            }],
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        });

        // Sparkline body — only when we have data and enough vertical room.
        if data.is_empty() || chart_h < 1.0 {
            return;
        }
        let chart = Chart {
            id: WidgetId::new(format!("metric-chart-{}", label.to_lowercase())),
            kind: ChartKind::Sparkline,
            series: vec![Series {
                label: label.to_string(),
                data,
                color: Some(color),
                fill: false,
            }],
            x_label: None,
            y_label: None,
            y_range: Some((0.0, 100.0)),
            x_range: None,
            show_legend: false,
            y_ticks: None,
            x_ticks: None,
            show_grid: false,
        };
        backend.draw_chart(chart_rect, &chart, None, None);
    }



    fn status_bar(&self) -> StatusBar {
        let view_label = self.active_view.label();
        // Show a loading indicator while a background fetch is in flight;
        // otherwise show seconds since the last completed refresh.
        let (refresh_text, refresh_fg, refresh_bold) = if self.pending_data.is_some() {
            (" ↻ loading… ".to_string(), Color::rgb(255, 220, 80), true)
        } else {
            (
                format!(" ↻ {}s ", self.refreshed_at.elapsed().as_secs()),
                Color::rgb(140, 140, 140),
                false,
            )
        };
        let mut left = vec![
            StatusBarSegment {
                text: " coord-tui ".to_string(),
                fg: Color::rgb(255, 255, 255),
                bg: Color::rgb(25, 70, 130),
                bold: true,
                action_id: None,
            },
            // §3 (#782): numeric key hints removed — views are now discovered
            // via the activity bar panel buttons.  §4: show the focused pane
            // region so Ctrl-W navigation is visible. The segment switches to
            // a bright, high-contrast highlight whenever focus has moved off
            // the Sidebar (the default) so the change is unmistakable at a
            // glance rather than a subtle text-only diff (#782 review finding B).
            {
                let (focus_fg, focus_bg, focus_bold) =
                    if self.focused_region == FocusedRegion::Sidebar {
                        (Color::rgb(200, 220, 255), Color::rgb(40, 60, 90), false)
                    } else {
                        (Color::rgb(20, 20, 20), Color::rgb(255, 200, 60), true)
                    };
                StatusBarSegment {
                    text: format!(" {}  [{}] ", view_label, self.focused_region.label()),
                    fg: focus_fg,
                    bg: focus_bg,
                    bold: focus_bold,
                    action_id: None,
                }
            },
            StatusBarSegment {
                text: refresh_text,
                fg: refresh_fg,
                bg: Color::rgb(30, 30, 40),
                bold: refresh_bold,
                action_id: None,
            },
        ];
        // #1631 (H-4): the always-visible fleet-health indicator — present
        // in every view's status bar regardless of severity ("OK states its
        // OK-ness rather than printing nothing"), colour-coded by
        // `FleetSeverity`. Right-click anywhere on the status bar to open
        // the detail overlay (`events.rs`); see `fleet_health.rs`'s module
        // doc comment for why this isn't itself clickable.
        // #1755 (DQ-3): the always-visible drive-queue indicator, same rule
        // as the fleet-health segment below — an empty queue reads "QUEUE:
        // empty", never nothing, because silence is indistinguishable from a
        // broken feature. Escalates to warn (STALLED: rows waiting, none
        // eligible) / crit (BLOCKED) so the epic's "if nothing can start,
        // alert the operator" requirement has a surface. Right-click the
        // status bar → "Drive queue…" switches to the Queue panel
        // (`drive_queue.rs`) — its modal overlay predecessor was retired by
        // #1868.
        //
        // **Ordered BEFORE fleet health deliberately.** The bar drops whole
        // trailing `left` segments once the view hints claim the rest of the
        // row, and "FLEET: OK  (coord health for detail)" is 38 columns —
        // appended after it, `QUEUE:` vanished entirely below ~150 columns,
        // which breaks the "always shows a QUEUE: segment" rule at every
        // realistic terminal width. Ahead of it, both indicators fit and the
        // only thing that can be squeezed out is fleet health's parenthetical
        // "(coord health for detail)" hint — the least load-bearing text on
        // the bar. Neither verdict is ever hidden.
        left.push(self.drive_queue_status_bar_segment());
        // #2608: a live roll-pending marker is its own segment, right next to
        // `drive_queue_status_bar_segment` (same priority tier — both are
        // queue-launch state) — never folded into it, so a queue held for a
        // roll never rides QUEUE's own escalation vocabulary
        // (STALLED/HELD/BLOCKED). `None` (nothing pushed) when no roll is
        // pending, same "absent, not empty" posture as the fleet-escalation
        // segment below.
        if let Some(seg) = self.roll_pending_status_bar_segment() {
            left.push(seg);
        }
        left.push(self.fleet_health_status_bar_segment());
        // #2374: fleet-level escalations (release-cordon / drive-queue
        // sentinel rows in `self.data.escalations`, keyed by a `"("`-prefixed
        // pseudo-repo — see `escalation.rs`'s `fleet_escalations` doc
        // comment) have no `PipelineIssue` row to badge, so before this they
        // were visible only via a Reports-panel `decisions` visit. Right
        // after fleet health / drive queue — same urgency tier, and `None`
        // (not pushed at all) when the fleet has none open, mirroring the
        // `plans_attn`/`audit_recent` badges below rather than fleet
        // health's always-on "OK states its OK-ness" treatment (there's no
        // useful "zero escalations" thing to say here).
        if let Some(seg) = self.fleet_escalation_status_bar_segment() {
            left.push(seg);
        }
        // Non-blocking warning if the last load failed.
        if let Some((err_msg, when)) = &self.fetch_error {
            if when.elapsed() < Duration::from_secs(10) {
                left.push(StatusBarSegment {
                    text: format!(" ⚠ {} ", err_msg),
                    fg: Color::rgb(255, 160, 60),
                    bg: Color::rgb(50, 30, 10),
                    bold: true,
                    action_id: None,
                });
            }
        }
        // #976: persistent "N plans need you" attention badge. quadraui's
        // `PanelDefinition` (the ◆ activity-bar icon behind the Plans view)
        // has no badge/count slot in this vendored version, so the
        // ActivityBar-level "you're reminded without opening it" signal
        // lives here instead — always visible regardless of `active_view`,
        // same as the live-session badge below. Uses the same
        // `plans_needing_attention_count` the Plans sidebar hint reads
        // (`plans.rs`) so the two numbers never drift apart.
        let plans_attn = self.plans_needing_attention_count();
        if plans_attn > 0 {
            let (noun, verb) = if plans_attn == 1 { ("plan", "needs") } else { ("plans", "need") };
            left.push(StatusBarSegment {
                text: format!(" ◆ {} {} {} you ", plans_attn, noun, verb),
                fg: Color::rgb(255, 210, 100),
                bg: Color::rgb(70, 45, 10),
                bold: true,
                action_id: None,
            });
        }
        // #1090 (#1039 deliverable #7): persistent "N audit events" attention
        // badge, mirroring the `plans_attn` segment immediately above —
        // always visible regardless of `active_view`, so recent audit
        // activity is noticeable without opening the Audit panel. Reads the
        // same `audit_recent_count()` the Audit sidebar's "N recent" line
        // reads (`audit.rs`), so the two can never drift apart.
        let audit_recent = self.audit_recent_count();
        if audit_recent > 0 {
            let noun = if audit_recent == 1 { "event" } else { "events" };
            left.push(StatusBarSegment {
                text: format!(" § {} audit {} ", audit_recent, noun),
                fg: Color::rgb(120, 210, 120),
                bg: Color::rgb(15, 50, 15),
                bold: true,
                action_id: None,
            });
        }
        // #628: persistent indicator for live interactive sessions. Detached
        // sessions create no board `running` row, so they were invisible —
        // they only showed in the one-time #487 startup toast and then piled
        // up unseen (burning Claude Max quota, holding worktrees). This durable
        // badge — refreshed by the background `coord sessions --remote`
        // discovery, and independent of any board row's status (a merged row
        // moves to Done but its session may still be live) — means you can
        // never silently accumulate them again.
        // #491: dead-pane sessions are shown with an amber warning badge;
        // alive sessions keep the normal blue badge.
        if !self.live_tmux_sessions.is_empty() {
            let n = self.live_tmux_sessions.len();
            let n_dead = self.live_tmux_sessions.iter().filter(|s| s.pane_dead).count();
            let badge_text = if n_dead > 0 {
                format!(
                    " ◉ {} session{} ({} dead) ",
                    n,
                    if n == 1 { "" } else { "s" },
                    n_dead
                )
            } else {
                format!(" ◉ {} live session{} ", n, if n == 1 { "" } else { "s" })
            };
            let (badge_fg, badge_bg) = if n_dead > 0 {
                // Amber: dead-pane sessions need attention.
                (Color::rgb(255, 210, 100), Color::rgb(70, 45, 10))
            } else {
                // Normal blue for all-alive sessions.
                (Color::rgb(150, 210, 255), Color::rgb(20, 45, 70))
            };
            left.push(StatusBarSegment {
                text: badge_text,
                fg: badge_fg,
                bg: badge_bg,
                bold: true,
                action_id: None,
            });
        }
        // #251: persistent warning when coordinator.yml was not located at
        // startup.  Previously surfaced at the top of the bottom COMMANDS
        // panel; the status bar carries it now that the panel is gone.
        if self.command_runner.config_path.is_none() {
            left.push(StatusBarSegment {
                text: " ⚠ coordinator.yml not found ".to_string(),
                fg: Color::rgb(255, 255, 255),
                bg: Color::rgb(140, 60, 30),
                bold: true,
                action_id: None,
            });
        }
        if let Some((label, elapsed)) = self.command_runner.running_info() {
            let depth = self.command_runner.queue_depth();
            let text = if depth > 0 {
                format!(
                    " {} ({:.0}s) · {} queued ",
                    label,
                    elapsed.as_secs_f64(),
                    depth
                )
            } else {
                format!(" {} ({:.0}s) ", label, elapsed.as_secs_f64())
            };
            left.push(StatusBarSegment {
                text,
                fg: Color::rgb(255, 220, 100),
                bg: Color::rgb(60, 50, 20),
                bold: true,
                action_id: None,
            });
        } else if let Some((msg, when)) = &self.command_runner.message {
            if when.elapsed() < Duration::from_secs(8) {
                left.push(StatusBarSegment {
                    text: format!(" {} ", msg),
                    fg: Color::rgb(120, 200, 120),
                    bg: Color::rgb(20, 50, 20),
                    bold: false,
                    action_id: None,
                });
            }
        }
        let proposals = self.data.proposals.len();
        // #369/#329: Prompt questions are now shown as quadraui Dialog
        // popups rather than status-bar hint strings — the dialog is the
        // affordance.  The hints chain below falls through to the normal
        // view-specific hints whenever a prompt dialog is showing, so the
        // status bar remains informative instead of going blank.
        let hints = if self.active_view == SidebarView::Terminal {
            // #424: Terminal pane — F12 toggles PTY focus / chrome focus.
            // #605: Ctrl-W is the keyboard pane leader (Ctrl-W h = side panel).
            if self.terminal_focused {
                " PTY focused — F12 / Ctrl-W h = release  (typed keys go to the shell) ".to_string()
            } else if self.terminal_return_view.is_some() {
                // #1029 bug B: only surface the Esc-return hint when there's
                // actually somewhere to return to (e.g. milestone chat) —
                // an ordinary ActivityBar-driven visit to Terminal has no
                // bookmark and Esc does nothing special here.
                " PTY released — F12 / Ctrl-W l = focus  ·  Esc = return  ·  click activity bar to switch view  ·  q=quit ".to_string()
            } else {
                " PTY released — F12 / Ctrl-W l = focus  ·  click activity bar to switch view  ·  q=quit ".to_string()
            }
        } else if self.active_view == SidebarView::Pipeline
            && self.pipeline_detail_tab == PipelineDetailTab::Terminal
        {
            // #440/#467: Pipeline detail Terminal tab — same F12 model as
            // the standalone pane but scoped to the selected issue's session.
            // `s` (released only) launches a local `coord assign --interactive`
            // claude session for the selected issue.
            if self.detail_terminal_focused {
                " PTY focused — F12 / Ctrl-W h = release  (typed keys go to the shell) ".to_string()
            } else {
                " PTY released — F12 / Ctrl-W l = focus  ·  s = interactive claude  ·  j/k=nav  h/l=tabs  Ctrl-W h=side panel  q=quit ".to_string()
            }
        } else if self.active_view == SidebarView::Board
            && self.board_detail_tab == BoardDetailTab::Terminal
        {
            // #675: Board detail Terminal tab — same F12 model as Pipeline.
            // The `s` shortcut is not available here (Board Terminal is for Chat).
            if self.detail_terminal_focused {
                " PTY focused — F12 to release  (typed keys go to the shell) ".to_string()
            } else {
                " PTY released — F12 to focus  ·  h/l=tabs  q=quit ".to_string()
            }
        } else if self.active_view == SidebarView::Machines {
            // Machines panel action hints.
            " r=restart  u=update  c=clean worktrees  p=pause/unpause  q=quit ".to_string()
        } else if self.active_view == SidebarView::Pipeline && self.test_gate_actionable() {
            // #200: surface the Test gate keybinds when actionable for the
            // currently-selected pipeline issue.
            // #235: include B (Phase 1 build) when no build is already in
            // flight for this work id.  #314: always include T=test-chat.
            if self.can_trigger_test_build() {
                " Test gate: B=build  T=chat  1–8=run step  P=pass  F=fail  r=report+fix  S=skip  q=quit ".to_string()
            } else {
                " Test gate: T=chat  1–8=run step  P=pass  F=fail  r=report+fix  S=skip  q=quit "
                    .to_string()
            }
        } else if self.can_bounce_work_after_test_fail() {
            // #236: Test just failed — the user's next step is to fix the
            // code and re-dispatch Work. Make the affordance obvious.
            " Test failed — R=re-dispatch Work  q=quit ".to_string()
        } else if self.can_dispatch_review_after_test_done() {
            // #236: Test passed — Review can be dispatched immediately
            // instead of waiting for the reconcile auto-dispatch tick.
            " Test passed — R=dispatch review  q=quit ".to_string()
        } else if self.active_view == SidebarView::Pipeline
            && self
                .ci_summary_for_selected_issue()
                .map(|s| s.has_failures())
                .unwrap_or(false)
        {
            // #240: when the selected pipeline issue's PR has failed CI
            // checks, swap the default hint so the user can't merge without
            // seeing it. coord merge will refuse by default; --force-merge
            // bypasses.
            let summary = self.ci_summary_for_selected_issue().unwrap();
            let names = if summary.failed_names.len() > 2 {
                format!(
                    "{} +{}",
                    summary.failed_names[..2].join(", "),
                    summary.failed_names.len() - 2
                )
            } else {
                summary.failed_names.join(", ")
            };
            format!(" Checks failed: {}  m=merge anyway  q=quit ", names)
        } else if self.merge_blocked_on_review_for_selected_issue() {
            // #253: the selected merge entry has no approved review on the
            // board.  Surface the block so the user doesn't press lowercase
            // `m` and silently merge unreviewed code.  Capital `M` runs
            // `coord merge --skip-review` for the deliberate override.
            " Merge blocked: review not yet approved  R=dispatch review  M=merge anyway  q=quit "
                .to_string()
        } else if self.active_view == SidebarView::Pipeline {
            // #194: Pipeline-specific hints: refresh, navigate, go, dismiss done.
            // #386: include i=steer on the Log tab so the feature is discoverable.
            // #628: include L=sessions when live sessions are present.
            let live_hint = if !self.live_tmux_sessions.is_empty() {
                "  L=sessions"
            } else {
                ""
            };
            if self.pipeline_detail_tab == PipelineDetailTab::Log {
                format!(
                    " j/k=nav  Enter=go  i=steer  R=refresh  D=dismiss-done  h/l=tabs  < >=hscroll{}  q=quit ",
                    live_hint
                )
            } else {
                format!(
                    " j/k=nav  Enter=go  R=refresh  D=dismiss-done  h/l=tabs{}  q=quit ",
                    live_hint
                )
            }
        } else if self.active_view == SidebarView::MergeQueue {
            // #737 / #780: Merge Queue panel hints.
            let attn = if self.merge_queue_needs_attention() {
                " ⚠ needs attention!"
            } else {
                ""
            };
            format!(
                " j/k=nav  a=merge-all-ready  m=merge-only-this  M=force-merge  d=drop  s=interactive{}  q=quit ",
                attn
            )
        } else if self.active_view.help_view_id().is_some()
            && (self.help_overlay.is_open() || self.command_palette.is_some())
        {
            // #1124 contract §5i: both the `?` cheatsheet and the `/`
            // command palette are modal overlays over whichever panel has
            // adopted the reusable help layer — Esc closes whichever is
            // open. Must precede the general per-view arms just below,
            // which this shadows while either surface is up. No `q=quit`
            // here — both surfaces own ALL input while open (`events.rs`),
            // so a typed `q` is swallowed (or filters the palette query),
            // not a quit.
            //
            // #2287 (ms-65 §8b): generalized from a `SidebarView::Plans`-only
            // check so the Board panel's own `?` cheatsheet (registered
            // under `"panel:board"`, see `render.rs::board_view_help`) gets
            // the same `" Esc=close "` chrome Plans' overlay already has
            // (contract §8b: "mirrors ms-38 §5i's identical convention").
            // Board never sets `command_palette` (events.rs deliberately
            // excludes it from the `/` trigger — Board's own `/` is the
            // pre-existing search-focus binding), so this fires for Board
            // only while `help_overlay` is open.
            " Esc=close ".to_string()
        } else if self.active_view == SidebarView::Plans && self.plans_detail_open {
            // #1122 (contract §3f): the detail pane replaces the roster
            // list, so its own hint set replaces the list-mode one just
            // below rather than layering onto it — `Esc=back` (to the list)
            // is the load-bearing substring §3f requires; `right-click=menu`
            // and `?=help` remain true (the cheatsheet/palette still open
            // over the pane) so they're kept for discoverability.
            " j/k=nav  Esc=back to list  right-click=menu  ?=help  q=quit ".to_string()
        } else if self.active_view == SidebarView::Plans {
            // #1123 (contract §4f): the CC-1 status bar advertised the
            // bare `c`/`u` letters directly (see the old comment this
            // replaces); that's now cryptic since #1123 makes every Plans
            // row right-clickable and moves those bindings into a labelled
            // context menu (`c`/`C`/`u`/`r` all still work as accelerators
            // *inside* the menu — contract §4e). The status bar instead
            // points at the two ways to discover them: `right-click=menu`
            // and `?=help` (the cheatsheet lists every binding, #1124).
            // `/=palette` is kept as an additional hint. `Enter=detail`
            // (#1122) replaces the old `Enter=open epic` — Enter now opens
            // the in-app detail pane, not the browser (that's demoted to
            // the pane's own "Open in browser" action) — the contract only
            // requires the `right-click=menu`/`?=help` substrings, so this
            // rewording doesn't affect §4f's assertions.
            " j/k=nav  Enter=detail  right-click=menu  ?=help  /=palette  q=quit ".to_string()
        } else if self.active_view == SidebarView::Sessions {
            // #1033: mirrors the retired #628 live-sessions overlay's
            // footer hint verbatim ([r]eattach / [K]ill / [f]stop), now
            // scoped to the Sessions-tree's selected leaf.
            " j/k=nav  r=reattach  K=kill  f=stop  q=quit ".to_string()
        } else if self.active_view == SidebarView::Audit {
            // #1039 contract §7 / #1040 contract §10: list-mode vs
            // detail-mode vs type-filter-editing hint sets.
            if self.audit_detail_open {
                " Esc=close detail  q=quit ".to_string()
            } else if self.audit_type_filter.focused {
                // No `q=quit` here — typed characters (including 'q') go
                // into the filter text while it's focused (events.rs), so
                // advertising it as a quit key would be wrong.
                " type to filter (event_type)  Enter=apply  Esc=clear ".to_string()
            } else {
                // #2653: `T=tier` is deliberately shown WITHOUT an inline
                // `(value)` — unlike `t=time-range`/`Tab=category` above,
                // which the ms-33 contract requires to show their current
                // selection inline. Adding a third `(value)` here (worst
                // case `" (operational)"`, 13 chars) pushes this line past
                // the sealed contract's 120-column fixture width alongside
                // the other two at their own longest labels ("Last hour" /
                // "override"), clipping the trailing `q=quit` the
                // `status_bar_hints_list_mode` acceptance test requires.
                // The sidebar's `"Tier: {label}"` row (`audit_sidebar`
                // above) is the value's real home; this hint only needs to
                // advertise that the `T` key exists.
                format!(
                    " j/k=nav  Enter=detail  r=refresh  t=time-range ({})  \
                     Tab=category ({})  T=tier  f=filter  Esc=clear  q=quit ",
                    self.audit_time_range.label(),
                    self.audit_category.label(),
                )
            }
        } else if self.active_view == SidebarView::Reports {
            // #1741: three hint sets — editing a free-text param (where
            // every typed character belongs to the field, so `q=quit` would
            // be a lie, same reasoning as the Audit type-filter above),
            // an empty/unfetched catalogue, and the normal stack.
            if self.reports_text_editing {
                " type to edit  Enter=done  Esc=cancel ".to_string()
            } else if self.reports_catalogue().is_empty() {
                " r=refresh catalogue  q=quit ".to_string()
            } else {
                " j/k=section  Space=collapse  Tab=field  ←/→=value  \
                 Enter=run  r=re-run  q=quit "
                    .to_string()
            }
        } else if self.active_view == SidebarView::Approved {
            // #2532 contract §3d/§3e: three hint sets — empty, list-mode,
            // detail-mode — same three-way shape Audit/Queue use above.
            if self.approved_submissions().is_empty() {
                " no approved submissions  q=quit ".to_string()
            } else if self.approved_detail_open {
                " j/k=nav  Esc=close detail  right-click=menu  q=quit ".to_string()
            } else {
                " j/k=nav  Enter=detail  right-click=menu  q=quit ".to_string()
            }
        } else if self.active_view == SidebarView::Queue {
            // #1866: the two hint sets are "there is a queue" and "there
            // isn't" — the reorder/remove/unblock verbs are all row-scoped,
            // so advertising them over an empty grid would be a lie.
            if self.queue_rows().is_empty() {
                " drive queue is empty  q=quit ".to_string()
            } else {
                " j/k=nav  J/K=reorder  x=remove  u=unblock  r=resume  \
                 click header=sort  right-click=menu  q=quit "
                    .to_string()
            }
        } else {
            // #192: `p` / `a` / `A` retired alongside the PROPOSALS
            // section.  Right-click → Send to Pipeline (#261) is the
            // canonical dispatch path now.
            // #628: include L=sessions when live sessions are present.
            let _ = proposals;
            let live_hint = if !self.live_tmux_sessions.is_empty() {
                "  L=sessions"
            } else {
                ""
            };
            // #2287 (ms-65 §8a): whenever the Board panel owns a non-empty
            // document-tab set, the pinned, verbatim tab-gesture hint
            // REPLACES the normal Board hint set (rather than layering onto
            // it) — "every interaction in this milestone is a mouse gesture
            // whose meaning is not visible on screen" (issue rationale),
            // and `mocks/board-preview-tab.screen` / `mocks/board-tab-
            // context-menu.screen` both show the status bar as exactly
            // `click=preview  dbl-click=pin  ctrl-w=close  ctrl-tab=next
            // q=quit` with tabs open — no `n=notify`/`m=merge`/`R=retry`/
            // `P=purge`. Concatenating the two sets (tried first) also
            // overflowed a 120-column bar enough to push the LEFT status
            // segments (e.g. the `[Sidebar]`/`[Main]` focus indicator, #605)
            // off the front — quadraui's `StatusBar::layout` never drops a
            // LEFT segment, so an overlong single RIGHT segment just
            // overdraws them instead (`app::tests::
            // ctrl_w_closes_the_active_doc_tab_and_walks_left`).
            //
            // Board falls through to this catch-all branch whenever it
            // isn't on the Terminal detail sub-tab (handled by its own
            // earlier arm above) and no other view-specific branch claimed
            // it, so this is the Board's own default hint set. Zero tabs
            // (or any other view reaching this branch, e.g. Kanban) leaves
            // the string exactly as it was before #2287 — the CONTROL
            // `tests/acceptance/ms-65/tabs_discoverability_2287.rs::
            // zero_tabs_status_bar_keeps_the_boards_own_hints_and_shows_no_tab_hints`
            // pins that this must stay true.
            if self.active_view == SidebarView::Board && !self.board_doc_tabs().is_empty() {
                " click=preview  dbl-click=pin  ctrl-w=close  ctrl-tab=next  q=quit ".to_string()
            } else {
                format!(
                    " n=notify  m=merge  R=retry  P=purge{}  q=quit ",
                    live_hint
                )
            }
        };
        StatusBar {
            id: WidgetId::new("statusbar"),
            left_segments: left,
            right_segments: vec![StatusBarSegment {
                text: hints,
                fg: Color::rgb(140, 140, 140),
                bg: Color::rgb(30, 30, 40),
                bold: false,
                action_id: None,
            }],
        }
    }
}


/// #319 Phase A: today's date as `YYYY-MM-DD`, computed from `SystemTime`
/// via Howard Hinnant's `days_from_civil` algorithm (the same one chrono,
/// time-rs, and the C++ standard library use).  Pulled in by hand to
/// avoid a chrono dependency for a single use site.
fn today_yyyy_mm_dd() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    // Days since 1970-01-01 (UTC).  Local time is "close enough" for a
    // header date stamp; off-by-a-day at most.
    let days = secs.div_euclid(86_400);
    // Shift so the epoch is 0000-03-01 (start of a Hinnant "era").
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = y + if m <= 2 { 1 } else { 0 };
    format!("{:04}-{:02}-{:02}", y, m, d)
}


/// #pause: read the set of paused machines from the LOCAL
/// `~/.coord/paused_machines.json`.  Shared with the Python coordinator
/// when this process and the daemon are the same host (`coord pause` /
/// `coord unpause` write the same file there) so the TUI's filter agrees
/// with `coord plan`, auto_loop, reconcile, review, and refine-chat
/// dispatches.  Returns empty on any I/O or parse failure — failure to
/// read should never block routing.
///
/// #1563: on a thin client (board service configured) this file is no
/// longer authoritative — `coord pause` on such a client now writes
/// straight to the daemon instead. `CoordApp::new` still calls this
/// directly for the *initial* synchronous `paused_machines` snapshot
/// (mirroring `live_tmux_sessions`/`fleet_terminals`/`drive_sessions`
/// further up this file, each seeded from a cheap local read before their
/// background remote sweep is armed). `app::fixtures::make_test_app`
/// deliberately does NOT call this (#2028) — a test fixture must never
/// read ambient machine state, so it seeds `paused_machines` empty instead
/// and lets individual tests assign it directly. Every *periodic* refresh
/// after startup goes through
/// [`data::fetch_paused_machines`] / [`data::spawn_paused_machines_fetch`]
/// instead, which route to the daemon's `GET /pause` when a board service
/// is configured and fall back to this function otherwise — so a
/// thin-client TUI's view converges to the daemon's copy within one
/// refresh cycle of startup rather than reading this stale local file
/// forever.
fn read_paused_machines() -> std::collections::HashSet<String> {
    let mut out = std::collections::HashSet::new();
    let home = match std::env::var_os("HOME") {
        Some(h) => std::path::PathBuf::from(h),
        None => return out,
    };
    let path = home.join(".coord").join("paused_machines.json");
    let content = match std::fs::read_to_string(&path) {
        Ok(c) => c,
        Err(_) => return out,
    };
    // Tiny hand-rolled extract — the file is `{"paused": ["foo", "bar"]}`
    // and pulling in serde just for this would inflate dependencies.
    // Walk the `paused` array, collect bare-string entries.
    let lower = content.to_string();
    let key_pos = match lower.find("\"paused\"") {
        Some(p) => p,
        None => return out,
    };
    let after_key = &content[key_pos..];
    let bracket_start = match after_key.find('[') {
        Some(p) => p,
        None => return out,
    };
    let bracket_end = match after_key[bracket_start..].find(']') {
        Some(p) => bracket_start + p,
        None => return out,
    };
    let inner = &after_key[bracket_start + 1..bracket_end];
    let mut chars = inner.chars().peekable();
    let mut current = String::new();
    let mut in_string = false;
    while let Some(c) = chars.next() {
        if in_string {
            if c == '"' {
                if !current.is_empty() {
                    out.insert(std::mem::take(&mut current));
                }
                in_string = false;
            } else if c == '\\' {
                if let Some(esc) = chars.next() {
                    current.push(esc);
                }
            } else {
                current.push(c);
            }
        } else if c == '"' {
            in_string = true;
            current.clear();
        }
    }
    out
}

/// Pick a single short line out of a child command's stderr suitable for a
/// toast body.  Skips blank lines and Python/Click tracebacks/prefixes (which
/// the coord CLI emits before the actual error message) so the user gets the
/// real reason — e.g. `gh failed: 'status:refining' not found` rather than
/// the wrapping `Error:`/`Traceback…` boilerplate.
///
/// `pub(crate)` (not private): `commands.rs`'s `CommandRunner::poll` also
/// uses this — quadraui's toast box is a single ~40-col line with no
/// wrapping (#771), so a long reason (e.g. Gate A's "repo 'x' has no
/// acceptance driver configured — add it under...") reads as cut off with no
/// way to see the rest. The status-bar segment `poll` sets on failure is a
/// full-width row, so surfacing the same reason there too (#1059 review)
/// gives the operator somewhere to actually read the complete message.
pub(crate) fn first_meaningful_stderr_line(stderr: &str) -> Option<String> {
    let trimmed = stderr.trim();
    if trimmed.is_empty() {
        return None;
    }
    // Prefer the LAST non-empty line — coord error paths end with the
    // operative message (`gh failed: …`, `failed to update N issues`), and
    // Click tracebacks tend to pile prelude lines before it.
    let last = trimmed
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .last()?;
    // Cap length for a toast.
    let max = 200usize;
    if last.len() > max {
        let mut out = last[..max].to_string();
        out.push('…');
        Some(out)
    } else {
        Some(last.to_string())
    }
}

/// #303: Strip every stage's `action` field so quadraui's `draw_pipeline_view`
/// renders the boxes as purely informational (status icon + label + elapsed),
/// no inline `[Go]`/`[Retry]` button.  The action info lives on the widget
/// returned by `build_pipeline_widget` (used by `dispatch_pipeline_active_go`
/// and `pipeline_action_button`); the visible buttons move to the bar above.
///
/// Pass the *stripped* view to both `draw_pipeline_view` AND
/// `Backend::pipeline_view_layout` so paint and hit-test agree on the
/// (now-shrunk) stage-box bounds — quadraui drops the action row when no
/// stage has one.




// #1042: app-construction fixtures (`make_test_app` + siblings), used by the
// in-crate `#[cfg(test)]` suite below and — when the `test-support` feature
// is enabled — re-exported at the crate root so an external integration-test
// crate (`tui/tests/acceptance.rs`) can build a `CoordApp` from in-memory
// `BoardData` without a live daemon. `pub` here is inert unless something
// downstream (lib.rs) actually re-exports it; the feature stays off for a
// normal `cargo build`/`cargo test`.
#[cfg(any(test, feature = "test-support"))]
pub mod fixtures;

#[cfg(test)]
mod tests;
