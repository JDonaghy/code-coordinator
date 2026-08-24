//! App data-model types extracted from `app/mod.rs` (#743).
//!
//! DTO/enum structs and their pure impls — no I/O, no quadraui rendering.
use std::time::{SystemTime, UNIX_EPOCH};
use quadraui::{Color, WidgetId};
use super::format::fmt_dur;


/// #349: One step in a generated smoke-test plan.  Parsed from the JSON stored
/// in `assignments.test_plan` when the DB row is loaded.
#[derive(Clone, Debug, serde::Deserialize)]
pub(crate) struct TestPlanStep {
    /// Step type: "pull", "run", or "verify".
    pub(crate) kind: String,
    /// Shell command for "pull" and "run" steps; absent for "verify".
    pub(crate) cmd: Option<String>,
    /// Human-readable label (mainly for "pull" steps).
    pub(crate) label: Option<String>,
    /// Observable assertion for "verify" steps.
    pub(crate) check: Option<String>,
}

/// #349: In-flight execution of a single test-plan step.  Background thread
/// runs the shell command and signals completion via the mpsc channel.
pub(crate) struct TestStepJob {
    /// (work_id, step_index) key — stored here for future diagnostics /
    /// logging; not read in the current implementation.
    #[allow(dead_code)]
    pub(crate) work_id: String,
    #[allow(dead_code)]
    pub(crate) step_idx: usize,
    /// Channel for receiving `(exit_code, captured_output)` once the subprocess
    /// finishes.  `captured_output` is the combined stdout + stderr of the
    /// command (newline-separated, bounded to 64 KiB to avoid unbounded growth).
    pub(crate) rx: std::sync::mpsc::Receiver<(i32, String)>,
}

/// The tabs shown in the Pipeline view detail panel.
///
/// #818: Redesigned tab set — Stages and Refinement removed; Pipeline renamed
/// to Overview.  A universal read-only stage strip is pinned at the top of
/// every non-Overview tab when a pipeline entry exists.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub(crate) enum PipelineDetailTab {
    /// Overview: horizontal stage boxes (click a box to expand that stage's
    /// detail inline), repo/labels/gates meta, and focused-stage content.
    /// Actions (Go / dispatch) are only available here.
    #[default]
    Overview,
    /// Full issue body text (scrollable with j/k).
    Issue,
    /// Live worker log — same content as the watch overlay but inline.
    Log,
    /// #558: session-history summary for the selected issue.  Lists every
    /// work/review/fix session (oldest → newest) with type, machine,
    /// status/verdict, and summary text.  Sourced from the in-memory board.
    Summary,
    /// #440: per-issue interactive shell for the detail view.  Each active
    /// issue gets its own `TerminalSession`; only the selected issue's
    /// terminal is visible while others keep running in the background.
    Terminal,
    /// #2405: the completed-issues grid — a report-style table of everything
    /// that finished inside an adjustable time range, replacing the old
    /// fixed-window "Done" sidebar section.  Unlike every other tab this one
    /// is **not** about the selected issue: it has its own controls
    /// (time range / window end / repo) and its own row selection, and a row
    /// click opens that row's Overview content *without* the
    /// Work/Test/Review/Merge stage strip (see `CompletedGrid::detail`).
    Completed,
}

/// #2405: state behind the Pipeline panel's completed-issues grid.
///
/// Deliberately mirrors the Reports panel's `issue-activity` controls
/// (`coord/reports.py`'s `ISSUE_ACTIVITY`): the same `since` presets with
/// free-form durations accepted, the same "empty `until` means now", and the
/// same "empty repo means all repos".  It is *not* routed through `/report`,
/// though — see the `#2405` note on `CoordApp::completed_rows` for why the
/// catalogue route was investigated and rejected.
///
/// Session-only, like the Reports panel's own sort and column widths: nothing
/// is written to settings.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct CompletedGrid {
    /// How far back the window reaches from `until`.  A preset from
    /// [`CompletedGrid::SINCE_PRESETS`] or any free-form duration
    /// (`13h`, `90m`, `3d`, …).  Never empty.
    pub(crate) since: String,
    /// Window end — epoch seconds or ISO-8601.  Empty means now.
    pub(crate) until: String,
    /// Restrict to one repo (matched against the issue's coord-local repo
    /// name, falling back to its GitHub slug).  Empty means all repos.
    pub(crate) repo: String,
    /// Which control has keyboard focus: 0 = time range, 1 = window end,
    /// 2 = repo.
    pub(crate) field_sel: usize,
    /// `(column index, ascending)`.  `None` = the default newest-finished-
    /// first order.
    pub(crate) sort: Option<(usize, bool)>,
    /// First visible row.
    pub(crate) scroll: usize,
    /// When `Some((repo_slug, issue_number))`, the grid is replaced by that
    /// row's detail view.  Held by identity rather than by row index so a
    /// background refresh (which re-derives the whole row set) can't silently
    /// swap which issue is on screen.
    pub(crate) detail: Option<(String, u64)>,
}

impl Default for CompletedGrid {
    fn default() -> Self {
        Self {
            since: "24h".to_string(),
            until: String::new(),
            repo: String::new(),
            field_sel: 0,
            sort: None,
            scroll: 0,
            detail: None,
        }
    }
}

/// #2405: one row of the completed-issues grid, already resolved against
/// `pipeline_issues`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct CompletedRow {
    /// Index into `CoordApp::pipeline_issues`.
    pub(crate) idx: usize,
    /// `(repo_slug, issue_number)` — the stable identity used by
    /// [`CompletedGrid::detail`].
    pub(crate) id: (String, u64),
    /// Short issue ref, e.g. `C#2345` — [`CoordApp::repo_tag`] of the issue's
    /// repo plus `#<number>`.
    pub(crate) issue_ref: String,
    /// Issue title.
    pub(crate) title: String,
    /// Earliest `dispatched_at` across this issue's assignments, or `None`
    /// when nothing was ever dispatched for it.
    pub(crate) started_at: Option<f64>,
    /// When it left the pipeline — `CoordApp::issue_done_at`.
    pub(crate) finished_at: Option<f64>,
}

/// The tabs shown in the Board view detail panel.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub(crate) enum BoardDetailTab {
    /// Default: assignment summary, status, machine, etc.
    #[default]
    Board,
    /// Full issue body text + labels (scrollable with j/k or scrollwheel).
    /// Reuses `issue_body_list` so the rendering matches the Pipeline view.
    Issue,
    /// #316 Phase A: board-level chat (new-issue or refine-board).
    /// Shows an empty state with Refine / New Issue CTAs when no chat is
    /// open; shows the ChatController when a board chat is live.
    Chat,
    /// #675: per-issue interactive shell for the Board detail view.
    /// Mirrors `PipelineDetailTab::Terminal` — spawned by "Chat about issue"
    /// from the Board context menu.  The same `detail_terminal_sessions` map
    /// is used; the session key is `(repo_slug, issue_number)`.
    Terminal,
}

/// #782: which pane currently has keyboard focus.
///
/// `Sidebar` is the default (activity-bar + list panel).
/// `Main` is the primary content area (main panel / detail panel).
/// `Detail` is the secondary pane visible when a detail sub-pane or PTY
/// occupies the right/bottom portion of the content area.
///
/// Used by the Ctrl-W focus cycler; the visible indicator is rendered in the
/// status bar.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub(crate) enum FocusedRegion {
    #[default]
    Sidebar,
    Main,
    Detail,
}

impl FocusedRegion {
    /// Short label shown in the status-bar focus indicator.
    pub(crate) fn label(self) -> &'static str {
        match self {
            FocusedRegion::Sidebar => "Sidebar",
            FocusedRegion::Main => "Main",
            FocusedRegion::Detail => "Detail",
        }
    }
}

/// The selectable top-level views shown in the left sidebar.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub(crate) enum SidebarView {
    #[default]
    Board,
    Machines,
    /// Pipeline panel: tracked-issue list + horizontal stage view per issue.
    Pipeline,
    /// Settings panel: category nav on the left, form controls on the right.
    Settings,
    /// #424: embedded terminal pane hosting a live PTY-backed shell session
    /// via `quadraui::terminal_engine::TerminalSession`.  Focus routing is
    /// controlled by `CoordApp::terminal_focused`: when true, keystrokes
    /// pass through to the PTY; when false, normal TUI chrome handles
    /// them. F12 toggles focus.
    Terminal,
    /// #638: Kanban view — three-column (Backlog / In Flight / Completed)
    /// rendered via `quadraui::Board`.
    Kanban,
    /// #737: Merge Queue panel — global view of all in-flight PR merges,
    /// grouped by milestone.  Key `7` switches to this view.
    MergeQueue,
    /// #771 (Phase 3 of #767): milestone work-order DAG/lane view — renders
    /// a milestone's `## Work order` block (parsed client-side from the
    /// already-synced tracking-issue body, see `app/milestone_dag.rs`) as
    /// cohort rows with done/in-flight/blocked/ready state per node, plus a
    /// "Dispatch milestone" action on the milestone header. Key `8`.
    MilestoneDag,
    /// #975: Plans panel — the first-class ActivityBar view onto the plan
    /// roster.  Elevates/subsumes the older `MilestoneDag` "Milestones" view:
    /// one row per milestone/epic with ready / blocked / in-flight / done
    /// counts, sourced from `BoardData::plan_roster` (server-computed by
    /// `coord/serve_app.py` via `coord.plans.aggregate_repo_plans`).  Read-only
    /// in this slice — health chips (#976), fast capture (#977), and the
    /// GOAL.md header (#978) come later.  Selecting a row opens that plan's
    /// tracking epic in the browser via `gh issue view --web`.
    Plans,
    /// #1032: Sessions panel — fleet-wide, machine → repo grouped tree of
    /// live `coord-<aid>` claude work sessions (`self.live_tmux_sessions`).
    /// Built on the same `TreeView` + flat-pixel-row click pattern as the
    /// #953 Terminal-view tree (`fleet_sessions.rs`), extended one level
    /// deeper (machine → repo → session leaf). Read-only nav/select in this
    /// slice — attach/kill/stop are a follow-up.
    Sessions,
    /// #1039: Audit panel — a scrollable, newest-first list of the audit
    /// trail (`/audit`, #1037) with an inline entry-detail view, modeled on
    /// the Plans panel. Filters (#1040) are a follow-up.
    Audit,
    /// #1741: Reports panel — a `MultiSectionView` stack with one collapsible
    /// section per entry in `GET /report`'s catalogue (#1742), each section
    /// body being that report's parameter `Form` plus a `Run` button. The
    /// completed run's `ReportResult` renders as a `DataTable` + notes block
    /// beneath the stack. Nothing about any specific report is hardcoded
    /// here — see `app/reports.rs`.
    Reports,
    /// #1866 (Q-1): Queue panel — a live `DataTable` grid over the drive
    /// queue (`BoardData::drive_queue`), filtered to the entries that have
    /// not finished, sortable by header click and reorderable with `J`/`K`.
    ///
    /// **Carries no fetch of its own.** `/board` already ships `drive_queue`
    /// and the existing `start_data_load` poll refreshes it on
    /// `settings.refresh_cadence`, so — unlike Reports (#1741) and Audit
    /// (#1039) — there is deliberately no view-gated fetch block in
    /// `settings_ui.rs::run_periodic_work` and no `spawn_*` for this panel.
    /// Adding one would be a second source of truth for data already in
    /// memory. See `app/drive_queue.rs`'s "Queue panel" section.
    Queue,
    /// #2532 (PB-2, ms-67 contract §3): Approved work items panel — a plain
    /// aligned-column list of portal submissions ready for decomposition
    /// into coordinator work (`signoff.approved` today; the operator
    /// "start work" override, `coord-portal`#132, once it lands). Modeled
    /// on the Audit panel's own list (not a `DataTable` grid, no sortable
    /// numeric columns and no drag-reorder verb) — see `app/approved.rs`.
    /// **No fetch of its own**: like Queue, rows ride the existing `/board`
    /// poll (`BoardData::approved_submissions`), not a view-gated fetch.
    Approved,
}

impl SidebarView {
    pub(crate) fn label(self) -> &'static str {
        match self {
            SidebarView::Board => "Board",
            SidebarView::Machines => "Machines",
            SidebarView::Pipeline => "Pipeline",
            SidebarView::Settings => "Settings",
            SidebarView::Terminal => "Terminal",
            SidebarView::Kanban => "Kanban",
            SidebarView::MergeQueue => "Merge Queue",
            SidebarView::MilestoneDag => "Milestones",
            SidebarView::Plans => "Plans",
            SidebarView::Sessions => "Sessions",
            SidebarView::Audit => "Audit",
            SidebarView::Reports => "Reports",
            SidebarView::Queue => "Queue",
            SidebarView::Approved => "Approved work items",
        }
    }

    /// The ActivityBar `WidgetId` (see `CoordApp::shell_config`) this view
    /// corresponds to, or `None` when the view has no top-level ActivityBar
    /// entry of its own (`MilestoneDag` — reached only as a drill-down from
    /// Plans; see the `#975` note on the `Plans` variant above).
    ///
    /// This is the inverse of the `panel_id_str` match in
    /// `render.rs::on_shell_event`. `CoordApp::switch_active_view` (#1029
    /// bug A) uses it to keep quadraui's ActivityBar highlight + sidebar
    /// header in sync with a *programmatic* view switch — one not already
    /// driven by an ActivityBar click, which `on_shell_event` already
    /// handles correctly.
    pub(crate) fn panel_widget_id(self) -> Option<WidgetId> {
        match self {
            SidebarView::Board => Some(WidgetId::new("panel:board")),
            SidebarView::Machines => Some(WidgetId::new("panel:machines")),
            SidebarView::Pipeline => Some(WidgetId::new("panel:pipeline")),
            SidebarView::Settings => Some(WidgetId::new("panel:settings")),
            SidebarView::Terminal => Some(WidgetId::new("panel:terminal")),
            SidebarView::Kanban => Some(WidgetId::new("panel:kanban")),
            SidebarView::MergeQueue => Some(WidgetId::new("panel:mergequeue")),
            SidebarView::Plans => Some(WidgetId::new("panel:plans")),
            SidebarView::Sessions => Some(WidgetId::new("panel:sessions")),
            SidebarView::Audit => Some(WidgetId::new("panel:audit")),
            SidebarView::Reports => Some(WidgetId::new("panel:reports")),
            SidebarView::Queue => Some(WidgetId::new("panel:queue")),
            SidebarView::Approved => Some(WidgetId::new("panel:approved")),
            SidebarView::MilestoneDag => None,
        }
    }

    /// The [`quadraui::HelpRegistry`] key this view's `?` cheatsheet / `/`
    /// command-palette content is registered under (#1124), or `None` for
    /// views that haven't adopted the reusable quadraui help-layer pattern
    /// yet (`JDonaghy/quadraui#431`) — see the module docs at the top of
    /// `plans.rs` for the pattern other panels are meant to copy. Reuses
    /// the exact same `"panel:X"` string `panel_widget_id` returns as a
    /// `WidgetId`, kept as a bare `&str` here because `HelpRegistry` keys
    /// on plain strings, not `WidgetId`.
    ///
    /// `CoordApp`'s shared `help_overlay`/`command_palette` machinery
    /// (`plans.rs`, `events.rs`, `render.rs`) is entirely driven off this
    /// method: a future panel adopts `?`/`/` by registering a `ViewHelp`
    /// under the id returned here — no other wiring changes required.
    pub(crate) fn help_view_id(self) -> Option<&'static str> {
        match self {
            SidebarView::Plans => Some("panel:plans"),
            // #2287 (ms-65 §8b): the Board panel's `?` cheatsheet — see
            // `render.rs::board_view_help`. Note this deliberately does NOT
            // also make Board opt into the shared `/` command-palette
            // trigger (`events.rs`'s `?`/`/` block): Board's own `/` is the
            // pre-existing sidebar search-focus binding, and this repo's
            // events.rs excludes `SidebarView::Board` from that trigger for
            // exactly that reason — see the comment there.
            SidebarView::Board => Some("panel:board"),
            _ => None,
        }
    }
}

#[derive(Clone, serde::Deserialize)]
// #1042: `pub`, not `pub(crate)` — it's a parameter/return type of the
// `test-support`-feature-gated fixtures in `app::fixtures` (e.g.
// `make_app_with_assignments(Vec<Assignment>)`), which must be at least as
// visible as those `pub fn`s (E0446) to be reachable from an external
// integration-test crate. Fields stay `pub(crate)`: nothing outside the
// crate constructs one field-by-field, only via `app::fixtures` helpers or
// `serde::Deserialize`.
pub struct Assignment {
    #[serde(rename = "assignment_id")]
    pub(crate) id: String,
    #[serde(rename = "repo_name")]
    pub(crate) repo: String,
    pub(crate) issue_number: u64,
    pub(crate) issue_title: String,
    #[serde(rename = "machine_name")]
    pub(crate) machine: String,
    pub(crate) status: String,
    #[serde(default)]
    pub(crate) branch: Option<String>,
    #[serde(default)]
    pub(crate) model: Option<String>,
    #[serde(default)]
    pub(crate) dispatched_at: Option<f64>,
    #[serde(default)]
    pub(crate) finished_at: Option<f64>,
    #[serde(default)]
    pub(crate) exit_code: Option<i32>,
    #[serde(rename = "type", default)]
    pub(crate) assignment_type: Option<String>,
    /// #200: human-driven Test gate verdict for type="work" assignments.
    /// None | "passed" | "failed" | "skipped".
    #[serde(default)]
    pub(crate) test_state: Option<String>,
    /// #253: parsed adversarial-review verdict for type="review" assignments.
    /// None | "approve" | "request-changes".  Drives the merge-gate hint
    /// swap so the user sees the block before pressing m.
    #[serde(default)]
    pub(crate) review_verdict: Option<String>,
    /// #253: links a review assignment back to the work assignment it
    /// reviews — needed to pair review_verdict with the merge entry.
    #[serde(default)]
    pub(crate) review_of_assignment_id: Option<String>,
    /// #208: worker cost captured from the final stream-json result event.
    /// `None` for in-flight workers and for pre-#208 rows.
    #[serde(default)]
    pub(crate) cost_usd: Option<f64>,
    /// #252: worker-emitted smoke-test list, parsed from the SMOKE_TESTS
    /// block in the worker's log.
    ///
    /// * `None`     — no block found (graceful degradation: TUI shows
    ///                "inspect the diff" placeholder).
    /// * `Some([])` — explicit "(none — change is internal)" form.
    /// * `Some(vec)` — bullets to render under the Test stage.
    ///
    /// #584: on the /board wire this is a real JSON array (already decoded),
    /// so plain serde handles it.
    #[serde(default)]
    pub(crate) smoke_tests: Option<Vec<String>>,
    /// #bounce: cached review findings (verdict + body), JSON-encoded
    /// in the DB column.  `None` for non-review assignments and for
    /// reviews completed before the cache landed.
    ///
    /// #584: intentionally kept as a raw JSON STRING on the /board wire, so
    /// plain serde deserialization works.
    #[serde(default)]
    pub(crate) review_findings: Option<String>,
    /// #1337: true when the daemon bounded `review_findings` on the /board
    /// wire (the collection carries a preview; the full body lives on
    /// `GET /assignment/{id}`).  Absent (→ false) on pre-#1337 daemons and
    /// on the local-SQLite path, both of which carry the full text.
    #[serde(default)]
    pub(crate) review_findings_truncated: bool,
    /// #1337: full stored length of `review_findings` when truncated —
    /// combined with the assignment id as the detail-fetch cache key, so a
    /// force-overwritten review (different length) re-fetches.
    #[serde(default)]
    pub(crate) review_findings_len: Option<i64>,
    /// #349 Phase B: AI-generated smoke-test plan for type="work" assignments.
    /// Parsed from the JSON blob in `assignments.test_plan`.  `None` = not
    /// yet generated (TUI will spawn `coord test-plan` to fill it in).
    ///
    /// #584: on the /board wire this is a decoded OBJECT `{"steps":[...]}`,
    /// not an array — see [`deserialize_test_plan`].
    #[serde(default, deserialize_with = "deserialize_test_plan")]
    pub(crate) test_plan: Option<Vec<TestPlanStep>>,
    /// #349 Phase B: git branch HEAD SHA at the time the cached test_plan was
    /// generated.  `None` when no plan exists or when it was generated without
    /// branch tracking.  Used to detect staleness (branch advanced →
    /// auto-refresh via `coord test-plan --refresh`).
    #[serde(default)]
    pub(crate) test_plan_branch_head: Option<String>,
    /// #546: token counts for automated (claude -p) assignments, parsed from
    /// the final stream-json result event alongside `cost_usd`.
    /// 0 for interactive (Claude Max / OAuth) sessions — those have no
    /// per-token billing and the TUI shows "Max" instead of a $ figure.
    #[serde(default)]
    pub(crate) input_tokens: i64,
    #[serde(default)]
    pub(crate) output_tokens: i64,
    #[serde(default)]
    #[allow(dead_code)] // populated from DB / SSE; not yet read in TUI render (#818 removed the Stages tab that displayed them)
    pub(crate) cache_creation_tokens: i64,
    #[serde(default)]
    #[allow(dead_code)] // see above
    pub(crate) cache_read_tokens: i64,
    /// #546: true when the assignment ran as a human-attended interactive session
    /// (Max / Pro subscription).  Set by `finalize_interactive_exit`; prevents
    /// misidentifying old automated rows (which also have cost_usd=NULL and zero
    /// token counts) as Max sessions.
    //
    // #628 hotfix: the daemon serializes this SQLite boolean as an int (0/1), so
    // a strict `bool` here fails the ENTIRE /board parse on `is_interactive:0`,
    // returning BoardData::default() and blanking every panel. Accept int-or-bool.
    #[serde(default, deserialize_with = "de_bool_from_int_or_bool")]
    pub(crate) is_interactive: bool,
    /// #618: short human-readable reason written immediately when an interactive
    /// session fails to launch (e.g. "branch already checked out at <path>").
    /// `None` for assignments that launched successfully.  Shown in the
    /// assignment detail panel so the TUI explains the red box without a log file.
    #[serde(default)]
    pub(crate) failure_reason: Option<String>,
    /// #803: fix-round counter — 0 on the original work assignment, N on the
    /// N-th fix.  Used to compute the next iteration's escalated model via
    /// [`fix_model_for_iteration`]: `next_iteration = review_iteration + 1`.
    #[serde(default)]
    pub(crate) review_iteration: i64,
    /// #932/#944: the Acceptance-gate verdict (oracle loop,
    /// docs/ORACLE_LOOP.md) for type="work" assignments, stamped by `coord
    /// acceptance record --issue N --sha <sha>` — the coordinator's
    /// external re-run of the sealed suite against the pushed SHA.
    /// None | "passed" | "failed". Reported and gated SEPARATELY from
    /// `test_state` — its own box, its own verdict.
    #[serde(default)]
    pub(crate) acceptance_state: Option<String>,
    /// Short failing-test summary when `acceptance_state == "failed"`.
    #[serde(default)]
    pub(crate) acceptance_reason: Option<String>,
    /// SHA the last `acceptance record` verdict was recorded against.
    #[serde(default)]
    pub(crate) acceptance_sha: Option<String>,
    /// #932: per-test counts from the same verdict, so the Acceptance box
    /// can read as partial progress ("3/7 acceptance green") rather than a
    /// bare pass/fail — a growing suite is expected to be sub-100% until
    /// the feature completes. `None` for rows predating this column.
    #[serde(default)]
    pub(crate) acceptance_total: Option<i64>,
    #[serde(default)]
    pub(crate) acceptance_passed: Option<i64>,
    /// #876: test failure reason entered by the operator via `coord test --fail`
    /// (written by `record_test_verdict`).  `None` for assignments without a
    /// failed test or for pre-#876 rows.
    #[serde(default)]
    pub(crate) test_reason: Option<String>,
    /// Internal review-state machine value: "pending" | "done" etc.
    /// `None` for non-review assignments and for pre-#876 rows.
    /// Deserialized from the board for future display; not yet read in
    /// production rendering paths.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) review_state: Option<String>,
    /// URL to the pull request for this work assignment (populated when the
    /// worker pushes a branch and the coordinator records it).
    /// `None` when no PR URL is known.
    #[serde(default)]
    pub(crate) pr_url: Option<String>,
    /// #886 Phase 2: Milestone Outcome Audit structured verdict, set only on
    /// `type="audit"` assignments (see #885's `--audit-of`). Kept as a raw
    /// JSON STRING on the wire, same convention as `review_findings` above.
    /// The Plans panel renders the *aggregated* latest-run-per-milestone view
    /// via `PlanRosterEntry`'s `outcome_*` fields, not this raw column — these
    /// are present for a possible future per-assignment audit detail view.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) audit_goals_json: Option<String>,
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) audit_bottom_line: Option<String>,
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) audit_run_number: Option<i64>,
    /// #1084: for `type="test-author"` JIT-mode assignments, the specific
    /// work-order member issue this dispatch is extending the acceptance
    /// suite for (`coord.test_author.dispatch_test_author`'s `issue_number`
    /// argument) — NOT the same as `issue_number` above, which test-author
    /// always sets to the milestone's *tracking* issue (every JIT dispatch
    /// for a milestone shares one branch/PR). `None` for milestone-mode
    /// (Gate A) authoring, every other assignment type, and rows predating
    /// this column. Used by the per-issue Acceptance-Authoring mini-
    /// pipeline to attribute a shared-branch assignment row back to the
    /// right member issue's row.
    #[serde(default)]
    pub(crate) for_issue_number: Option<u64>,
    /// #1499: durable provenance — `Some("drive:<repo>#<issue>")` when this
    /// assignment was dispatched by `coord drive` (via `coord assign
    /// --driven-by`), `None` for a hand `coord assign` and for rows
    /// predating this column. This is what lets the Pipeline distinguish a
    /// drive-dispatched row from a hand dispatch (and, combined with
    /// `drive_sessions`, "drive exited unfinished" from "never driven")
    /// even after the driver's tmux session is long gone — see
    /// `CoordApp::issue_has_drive_provenance` in `drive.rs`.
    #[serde(default)]
    pub(crate) driven_by: Option<String>,
    /// #2417: the CALLING assignment's id when this row was dispatched by a
    /// `coord` subcommand run from INSIDE another worker's own turn (e.g. a
    /// `type="work"` session shelling out to `coord acceptance author repo
    /// ms --issue N` or `coord fix <other-id>`), as opposed to a human
    /// typing the same command in their own shell. `None` for a hand or
    /// coordinator/brain dispatch, and for rows predating this column.
    /// Mirrors `coord.models.Assignment.dispatched_by_assignment_id`. Read
    /// in reverse via [`crate::app::CoordApp::dispatched_children`]: given
    /// an ORIGIN row, find every assignment whose value here equals the
    /// origin's `id` — that reverse lookup is what lets the origin row show
    /// "→ dispatched test-author <id> — running/done" instead of the
    /// operator having to grep the raw worker transcript (#2417).
    #[serde(default)]
    pub(crate) dispatched_by_assignment_id: Option<String>,
}

/// Deserialize a boolean the daemon may send as a SQLite-style integer (0/1)
/// instead of a JSON bool. Accepts bool, int, or null (→ false). One mistyped
/// boolean would otherwise fail the whole `BoardPayload` parse and blank the
/// board (the #546 `is_interactive` regression).
pub(crate) fn de_bool_from_int_or_bool<'de, D>(d: D) -> Result<bool, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(serde::Deserialize)]
    #[serde(untagged)]
    enum BoolOrInt {
        Bool(bool),
        Int(i64),
    }
    Ok(match <Option<BoolOrInt> as serde::Deserialize>::deserialize(d)? {
        Some(BoolOrInt::Bool(b)) => b,
        Some(BoolOrInt::Int(n)) => n != 0,
        None => false,
    })
}

impl Assignment {
    /// #1553: the issue this assignment's work is *attributed* to.
    ///
    /// For ordinary work this is just `issue_number`. For oracle-loop
    /// acceptance-slice work (`coord acceptance author <repo> <tracking>
    /// --issue N`, and everything derived from it — its review, its
    /// `[fix-N]` bounces, its smoke, a retry) `issue_number` is the
    /// milestone's **tracking/epic** issue, because the whole milestone's
    /// JIT slices share one branch and one PR. The child issue the work is
    /// really *for* lives in `for_issue_number`; this prefers it.
    ///
    /// Use this for "is this issue being worked on right now?" and "what
    /// did this issue cost?" — the questions where booking a child's six
    /// live sessions against the epic left the child's Pipeline row looking
    /// idle (the exact operator report in #1553).
    ///
    /// Do NOT use it for *merge/completion* attribution: `issue_number`
    /// deliberately stays the tracking issue so the shared branch/PR
    /// bookkeeping keeps working, and re-attributing merge state by
    /// `for_issue_number` was tried in #1203 and reverted in #1652 (it just
    /// moved a false green from the epic onto the child).
    ///
    /// Mirrors `coord.models.effective_issue_number`.
    pub(crate) fn effective_issue_number(&self) -> u64 {
        self.for_issue_number.unwrap_or(self.issue_number)
    }

    /// Return the colour for this assignment's status badge, drawn from the
    /// active theme palette.
    ///
    /// Mapping (semantic → `quadraui::Theme` field):
    /// - `"running"`    → `theme.badge_running`  (active worker — green on dark)
    /// - `"finalizing"` → `theme.diagnostic_info` (#1566: review agent done,
    ///   verdict not yet captured/posted — distinct from both "done" and
    ///   "failed" so it never reads as a dropped verdict)
    /// - `"done"`       → `theme.muted_fg`        (completed, no longer active)
    /// - `"failed"`     → `theme.badge_blocked`   (hard failure — red)
    /// - other          → `theme.warning_fg`      (pending / unknown — yellow)
    pub(crate) fn status_color(&self, theme: &quadraui::Theme) -> Color {
        match self.status.as_str() {
            "running" => theme.badge_running,
            "finalizing" => theme.diagnostic_info,
            "done" => theme.muted_fg,
            "failed" => theme.badge_blocked,
            _ => theme.warning_fg,
        }
    }

    pub(crate) fn status_label(&self) -> &str {
        match self.status.as_str() {
            "running" => "RUN ",
            "finalizing" => "WRAP",
            "done" => "DONE",
            "failed" => "FAIL",
            _ => "PEND",
        }
    }

    pub(crate) fn age_str(&self) -> String {
        match self.dispatched_at {
            None => "-".to_string(),
            Some(ts) => {
                let now = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs_f64();
                fmt_dur((now - ts).max(0.0) as u64)
            }
        }
    }
}

#[derive(Clone)]
pub(crate) struct Machine {
    pub(crate) name: String,
    /// Tailscale FQDN (the `host` column in the machines table).
    pub(crate) host: String,
    pub(crate) reachable: bool,
    pub(crate) active_count: usize,
    pub(crate) repos: Vec<String>,
    /// Agent version string from `/health.version`; `None` when unreachable.
    pub(crate) version: Option<String>,
    /// Total git-worktree disk usage in bytes, from `/health.worktree_bytes`.
    pub(crate) worktree_bytes: u64,
}

/// #584: a machine row as it arrives on the `coord serve` /board wire.
///
/// `Machine` itself carries probe-only fields (reachable / active_count /
/// version / worktree_bytes) that never appear in the payload, so we
/// deserialize into this minimal shape and let `assemble_board_data` run the
/// reachability + health probes exactly like the SQLite path does.
#[derive(serde::Deserialize)]
pub(crate) struct RawMachine {
    pub(crate) name: String,
    pub(crate) host: String,
    #[serde(default)]
    pub(crate) repos: Vec<String>,
}

/// #778: one entry in the "approved but not yet queued" staging section.
///
/// Mirrors `coord.merge_queue.StagingItem` on the Python side.  Populated
/// by `staging_items()` server-side; the TUI receives it via the `/board`
/// JSON payload and also computes it locally in `load_data`.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct StagingEntry {
    /// Wire field — present for future right-click / force-enqueue actions.
    #[allow(dead_code)]
    pub(crate) assignment_id: String,
    /// Wire field — present for future per-repo grouping.
    #[allow(dead_code)]
    pub(crate) repo_name: String,
    pub(crate) issue_number: i64,
    pub(crate) issue_title: String,
    /// Wire field — present for future branch-detail display.
    #[allow(dead_code)]
    pub(crate) branch: String,
    /// `"ready"` — all gates pass, will enqueue on next daemon tick.
    /// `"blocked"` — at least one non-review gate is failing.
    #[serde(default)]
    pub(crate) status: String,
    /// Human-readable gate failure when `status == "blocked"`, e.g.
    /// `"test verdict missing"`.  `None` when ready.
    #[serde(default)]
    pub(crate) reason: Option<String>,
}

/// #584: the top-level `coord serve` /board payload.
///
/// serde ignores unknown JSON keys by default, so the many extra columns the
/// daemon emits (schema_version, notifications, per-row provider/files fields,
/// etc.) are silently dropped.  JSON columns are decoded to native objects on
/// the wire EXCEPT `assignments.review_findings` (a raw JSON string) — handled
/// by the per-field serde attributes on `Assignment`.
#[derive(serde::Deserialize, Default)]
pub(crate) struct BoardPayload {
    /// Round counter from the daemon.  Not rendered today (the SQLite path
    /// never read it either) but kept on the wire shape for parity + tests.
    #[serde(default)]
    #[allow(dead_code)]
    pub(crate) round_number: i64,
    #[serde(default)]
    pub(crate) assignments: Vec<Assignment>,
    #[serde(default)]
    pub(crate) machines: Vec<RawMachine>,
    #[serde(default)]
    pub(crate) merge_queue: Vec<MergeQueueEntry>,
    /// Server-side merge plan (#776) — ranked, annotated list of planned merges.
    /// Computed by `coord.merge_queue.plan()` and injected by `serve_app.py`.
    /// Empty when the daemon is older than v0.4.53 (pre-#776).
    #[serde(default)]
    pub(crate) merge_plan: Vec<PlannedMergeEntry>,
    #[serde(default)]
    pub(crate) proposals: Vec<Proposal>,
    #[serde(default)]
    pub(crate) issues: Vec<OpenIssue>,
    /// assignment_id → decoded plan object (parsed via [`parse_plan_data`]).
    #[serde(default)]
    pub(crate) plans: std::collections::HashMap<String, serde_json::Value>,
    /// board_meta key → STRING value (JSON-encoded for the pipeline_* keys).
    #[serde(default)]
    pub(crate) board_meta: std::collections::HashMap<String, String>,
    /// #778: approved/done work not yet in the merge queue.  Computed
    /// server-side by `staging_items()`; empty when the board daemon is not
    /// running (the local SQLite path computes its own in `load_data`).
    #[serde(default)]
    pub(crate) merge_staging: Vec<StagingEntry>,
    /// #550: server-computed per-issue stage/gate projection.  Empty when
    /// the daemon predates #550 — the client-local `pipeline.rs` functions
    /// this mirrors remain the fallback in that case (and always for the
    /// local-SQLite-mode read path, which has no daemon to ask).
    #[serde(default)]
    pub(crate) issue_stage_projection: Vec<IssueStageProjection>,
    /// #795 Phase 3b: per-milestone work-order rank + ready frontier.  Empty
    /// when the daemon predates #795.  The TUI renders rank, next-up, and
    /// blocked-on badges on Pipeline milestone cards using this projection.
    #[serde(default)]
    pub(crate) milestone_work_orders: Vec<MilestoneWorkOrder>,
    /// #1195/#1197: per-epic child-issue lists, published under the wire key
    /// `children`. Empty on the local-SQLite-mode read path (no daemon to
    /// compute it) and on daemons older than #1195. The Pipeline view nests
    /// each epic's children beneath its row using this projection.
    #[serde(default, rename = "children")]
    pub(crate) epic_children: Vec<EpicChildren>,
    /// #975: milestone plan-roster — one entry per milestone/epic with
    /// ready / blocked / in-flight / done counts + attention signals,
    /// computed server-side by `coord.plans.aggregate_repo_plans`.  Empty on
    /// the local-SQLite-mode read path (no daemon to compute it) and on
    /// daemons older than #975 — the Plans panel renders "No plans yet" in
    /// that case.
    #[serde(default)]
    pub(crate) plan_roster: Vec<PlanRosterEntry>,
    /// #976: capability flag — true whenever the daemon computes
    /// `plan_roster` at all (see `serve_app.py`'s `board()` handler), even
    /// if the roster came back empty this tick due to a per-repo
    /// aggregation error. Absent (→ `false` via `#[serde(default)]`) on
    /// daemons older than #975, which never emit `plan_roster`/this flag at
    /// all. Lets the Plans panel tell "genuinely zero milestones" apart
    /// from "connected to a daemon too old to compute a roster" instead of
    /// rendering an identical, silent "0 plans" for both (the #976 review
    /// finding: a stale/pre-#975 daemon produced exactly this ambiguity).
    #[serde(default)]
    pub(crate) plan_roster_supported: bool,
    /// #978: GOAL.md pinned north-star header for the Plans panel — see
    /// `GoalHeader` doc comment. `#[serde(default)]` leaves `available:
    /// false` (the type's `Default`) on daemons that predate #978, which
    /// never emit this key at all.
    #[serde(default)]
    pub(crate) goal_header: GoalHeader,
    /// #1037/#1039: count of `audit_log` rows written in the last 15
    /// minutes — a single forward-compatible integer so the Audit
    /// ActivityBar panel can show an attention badge without fetching the
    /// full paginated `/audit` log.  `0` (the `Default`/`#[serde(default)]`
    /// value) on daemons older than #1037, which never emit this key.
    #[serde(default)]
    pub(crate) audit_recent_count: u64,
    /// #1505: board-visible driver-escalation records — see
    /// `coord.drive._escalate_merge` / `coord/db.py`'s `drive_escalations`
    /// table doc comment. Empty on the local-SQLite-mode read path (no
    /// daemon to compute it — it's a raw table dump, but still server-side
    /// only) and on daemons older than #1505.
    #[serde(default)]
    pub(crate) escalations: Vec<EscalationEntry>,
    /// #1631 (H-4): the fleet-health aggregate H-3 computes — see
    /// `coord.health.fleet_snapshot.FleetHealthSnapshot.to_dict`. Empty
    /// (`FleetHealthBlock::default()`) on the local-SQLite-mode read path
    /// (no daemon to compute it — H-3's snapshot lives in-process on
    /// whichever host runs `coord serve`) and on daemons older than #1630.
    #[serde(default)]
    pub(crate) fleet_health: FleetHealthBlock,
    /// #1753 (DQ-1) / #1755 (DQ-3): the operator-declared `coord drive` work
    /// queue, in run order. Empty on the local-SQLite-mode read path (the
    /// table lives in the daemon host's DB, same posture as `escalations`)
    /// and on daemons older than #1753, which never emit this key.
    #[serde(default)]
    pub(crate) drive_queue: Vec<BoardDriveQueueEntry>,
    /// #2608: the machine-local roll-pending marker — see `RollPending`'s
    /// doc comment. `#[serde(default)]` leaves this `None` both when the key
    /// is absent (daemon predates #2608) and when it is explicit JSON
    /// `null` (no roll pending).
    #[serde(default)]
    pub(crate) roll_pending: Option<RollPending>,
    /// #2532 (ms-67 contract §5): portal submissions ready for
    /// decomposition into coordinator work — `signoff.approved` today.
    /// Server-computed (`coord/approved_work.py`, injected by
    /// `coord/serve_app.py`'s board builder), including the `repos` field
    /// resolved from `portal.project_repos` in `coordinator.yml` — the TUI
    /// never reads config or the portal DB directly. Empty on daemons that
    /// predate #2532, and on any daemon whose portal bridge has no
    /// approved sign-off yet.
    #[serde(default)]
    pub(crate) approved_submissions: Vec<ApprovedSubmission>,
}

/// #2532 (ms-67 contract §5): one portal submission ready to pull into a
/// briefed decomposition session — the wire shape of `/board`'s
/// `approved_submissions` entries. Field names are the contract's own
/// proposal (§6.9), not read off coord-portal's real schema (a separate
/// repo) — `coord/approved_work.py` reads them out of the read-only
/// customer mirror under these spellings (plus a camelCase alias), so if
/// the portal's real spelling differs, that alias table is the one place to
/// correct it and this struct does not change.
#[derive(Clone, Debug, serde::Deserialize)]
pub(crate) struct ApprovedSubmission {
    pub(crate) submission_id: String,
    pub(crate) client: String,
    pub(crate) project_id: String,
    pub(crate) project_label: String,
    pub(crate) outcome: String,
    pub(crate) audience: String,
    pub(crate) done_definition: String,
    pub(crate) constraints: String,
    /// Server-resolved repo(s) this submission's project maps to
    /// (`PortalConfig.repos_for_project` in `coord/config.py`). Empty means
    /// "no mapping" — contract §3c's literal `"— no mapping —"`
    /// placeholder, never a blank cell.
    #[serde(default)]
    pub(crate) repos: Vec<String>,
    /// ISO-8601 timestamp (`YYYY-MM-DDTHH:MM:SSZ`), mirrors
    /// `coord.portal_store.SubmissionRecord.first_seen_at`. Parsed with
    /// `data::parse_iso8601_to_epoch` for the detail pane's "Xd ago"
    /// rendering; kept as the raw wire string so a parse failure never
    /// blanks the whole row.
    #[serde(default)]
    pub(crate) received_at: String,
}

/// #1631 (H-4): one already-rendered health-check row — the wire shape of
/// `coord.health.models.CheckResult.to_dict()`. `severity`/`headroom` are
/// already decided by the probe that produced this row; nothing on the TUI
/// side may re-derive either from raw numbers (see `fleet_health.rs`'s
/// module doc for why that invariant matters).
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct FleetHealthCheckResult {
    // Parsed for wire parity / a future per-row selection or drill-down
    // (mirrors `EscalationEntry::id`/`stage` above); the overlay renders
    // `label`/`headroom`/`threshold`/`detail` today, not these.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) key: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) check_id: String,
    #[serde(default)]
    pub(crate) title: String,
    #[serde(default)]
    pub(crate) label: String,
    // Wire parity only — `label` already carries the subject when the
    // check has one (see `CheckResult.label` in `coord/health/models.py`).
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) subject: Option<String>,
    #[serde(default)]
    pub(crate) severity: String,
    #[serde(default)]
    pub(crate) headroom: String,
    #[serde(default)]
    pub(crate) threshold: String,
    #[serde(default)]
    pub(crate) detail: String,
}

/// #1631 (H-4): one machine's rolled-up health — the wire shape of one
/// entry in `fleet_health.machine_health` (see
/// `coord.health.fleet_snapshot._machine_health_rows`). `severity` is
/// already stale-aware ("unknown" when the machine never reported or its
/// last report is too old — never silently carried forward as green); it is
/// NOT recomputed from `results` on this side.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct FleetMachineHealth {
    pub(crate) machine: String,
    #[serde(default)]
    pub(crate) state: String,
    #[serde(default)]
    pub(crate) severity: String,
    #[serde(default)]
    pub(crate) stale: bool,
    #[serde(default)]
    pub(crate) checked_at: Option<f64>,
    #[serde(default)]
    pub(crate) results: Vec<FleetHealthCheckResult>,
}

/// #1631 (H-4): the wire shape of `/board`'s `fleet_health` key —
/// `coord.health.fleet_snapshot.FleetHealthSnapshot.to_dict()`.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct FleetHealthBlock {
    #[serde(default)]
    pub(crate) machine_health: Vec<FleetMachineHealth>,
    /// Fleet-scope checks (board latency, phantom-running rows, deploy-lane
    /// skew, …) that have no single owning machine.
    #[serde(default)]
    pub(crate) fleet_checks: Vec<FleetHealthCheckResult>,
}

/// #1505: one board-visible "driver stuck" record — `coord drive`'s merge
/// stage hit a status no amount of retrying can fix (NEEDS_ATTENTION / an
/// unrecognised status) and escalated instead of burning the merge-attempt
/// budget on it. A raw dump of the `drive_escalations` table row, one
/// (repo_name, issue_number) at a time — see `coord/db.py`'s table comment
/// and `coord.drive._escalate_merge`.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct EscalationEntry {
    #[allow(dead_code)]
    pub(crate) id: i64,
    pub(crate) repo_name: String,
    pub(crate) issue_number: i64,
    // #2427: which stage box on the Overview strip gets the "blocked" mark
    // — see `CoordApp::build_pipeline_widget`'s `esc.stage == name` check.
    #[serde(default)]
    pub(crate) stage: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) assignment_id: Option<String>,
    pub(crate) reason: String,
    /// Human-readable "key=value | key=value" summary of the gate readings
    /// the driver observed — deliberately NOT JSON (see `coord/db.py`): this
    /// record exists to be read by a human, not machine-parsed.
    #[allow(dead_code)] // not yet surfaced in the TUI (unlike `stage`, #2427)
    #[serde(default)]
    pub(crate) gate_readings: String,
    pub(crate) proposed_command: String,
    #[serde(default)]
    #[allow(dead_code)]
    pub(crate) created_at: Option<f64>,
}

/// #1753 (DQ-1) / #1755 (DQ-3): one row of the operator-declared `coord
/// drive` work queue — the wire shape of `/board`'s `drive_queue` array
/// (OpenAPI component `BoardDriveQueueEntry`, a raw `drive_queue` table dump
/// via `coord.dao.SqliteStore.board_projection`; see `coord/db.py`'s table
/// comment for the storage contract).
///
/// **Every field is `#[serde(default)]` on purpose.** `/board` is one
/// payload: a single type mismatch on a single field fails the *entire*
/// `BoardPayload` parse and blanks every panel with no error message — the
/// #632/#546/#628 class this repo has been bitten by three times.
/// `tests.rs`'s `board_payload_deserializes_real_sample` round-trips the
/// golden fixture (which now carries a populated `drive_queue`) so schema
/// drift fails a test instead.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct BoardDriveQueueEntry {
    #[serde(default)]
    pub(crate) repo_name: String,
    #[serde(default)]
    pub(crate) issue_number: i64,
    /// Dense, 0-based run order (`coord/db.py`: no fractional positions —
    /// `move` renumbers the affected span in one transaction).
    #[serde(default)]
    pub(crate) position: i64,
    /// `None`/absent means "let `coord drive` route it" — NOT "unpinned is
    /// an error".
    #[serde(default)]
    pub(crate) machine: Option<String>,
    /// Pre-req keys (`"repo#N"`) that must land before this entry may start.
    ///
    /// Named `after` here but renamed from the wire's `after_json`: the
    /// column is a JSON *string* in SQLite and `coord.dao._JSON_COLUMNS`
    /// decodes it to a real array on the way out, keeping the column name.
    /// A plain `after` field would silently stay empty forever.
    #[serde(default, rename = "after_json")]
    pub(crate) after: Vec<String>,
    /// `waiting` | `running` | `blocked` | `done` | … — decided by DQ-2's
    /// tick, never re-derived here (same posture as `fleet_health.rs`'s
    /// "renderers consume `severity` verbatim").
    #[serde(default)]
    pub(crate) state: String,
    #[serde(default)]
    pub(crate) attempts: i64,
    /// How many ticks skipped this entry because it wasn't eligible yet.
    /// Surfaced in the overlay next to `last_reason` — a row that keeps
    /// being passed over is the thing an operator most needs to see.
    #[serde(default)]
    pub(crate) deferrals: i64,
    #[serde(default)]
    pub(crate) last_reason: String,
    /// #2133: capture time of `last_reason` — `last_reason` is a
    /// point-in-time observation, never re-validated, so the Queue panel
    /// age-stamps it (`queue_row`) rather than rendering it bare as if it
    /// were current state. `None` for a row predating the migration or one
    /// whose `last_reason` is still the `""` default.
    #[serde(default)]
    pub(crate) reason_at: Option<f64>,
    #[allow(dead_code)] // wire parity; the Queue panel shows `state`, not the clock
    #[serde(default)]
    pub(crate) launched_at: Option<f64>,
    /// #1757: this entry ends with a DEPLOY GATE — when it completes, the
    /// tick launches nothing until a human deploys and releases it.
    ///
    /// SQLite stores 0/1 in an `INTEGER` column, so this is `i64`, not
    /// `bool`: an `INTEGER` column deserialised into a Rust `bool` is the
    /// exact #632/#546/#628 type mismatch that blanks the whole board (see
    /// `tests/test_board_fixture.py::
    /// test_no_unguarded_integer_bool_columns_reach_the_wire`).
    #[serde(default)]
    pub(crate) hold_after: i64,
    /// What the operator must do while the gate is held — rendered verbatim.
    #[serde(default)]
    pub(crate) hold_reason: String,
    /// `""` | `armed` | `fired` | `released` — decided by the tick, consumed
    /// verbatim here (same posture as `state` above).
    #[serde(default)]
    pub(crate) hold_state: String,
    /// Consecutive failed `resume_when` runs since the gate fired. A rising
    /// count is how a gate that never clears becomes visible instead of
    /// silent, so it is carried on the wire rather than re-derived.
    #[serde(default)]
    pub(crate) hold_probes: i64,
    /// #2186: how FAR a fired gate reaches — `"entry"` (the default) holds
    /// only entries whose own `after` names this gate's key; `"fleet"` is
    /// the pre-#2186 whole-queue stop, now opt-in only.
    ///
    /// Fail-closed, mirroring `coord.drive_queue.QueueEntry.
    /// _normalize_hold_scope`: `""` (a row predating this column, or any
    /// other server this build has never heard of) reads as entry-scoped,
    /// the narrower/safer reading — NEVER as fleet-wide. See
    /// `drive_queue::stops_fleet`, the one place this is consulted.
    #[serde(default)]
    pub(crate) hold_scope: String,
}

impl BoardDriveQueueEntry {
    /// `"repo#N"` — the same fully-qualified key `coord.drive_queue`'s
    /// `entry_key` builds, so a TUI-rendered key and a CLI-rendered one are
    /// always byte-identical.
    pub(crate) fn key(&self) -> String {
        format!("{}#{}", self.repo_name, self.issue_number)
    }
}

/// #584: serde deserializer for `Assignment::test_plan` on the remote
/// (`coord serve` /board) read path.  The wire shape is the decoded JSON
/// OBJECT `{"steps":[{kind,cmd,label,check},...], ...}` — NOT an array — so
/// we read it as an `Option<serde_json::Value>` and reuse the same
/// `.get("steps").as_array()` extraction as [`parse_test_plan_steps`].
/// Returns `None` on any shape mismatch (mirrors the SQLite path's tolerant
/// degradation to the "Preparing plan…" placeholder).
pub(crate) fn deserialize_test_plan<'de, D>(deserializer: D) -> Result<Option<Vec<TestPlanStep>>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::Deserialize;
    let val = Option::<serde_json::Value>::deserialize(deserializer)?;
    let Some(val) = val else {
        return Ok(None);
    };
    let Some(steps) = val.get("steps").and_then(|s| s.as_array()) else {
        return Ok(None);
    };
    let result: Vec<TestPlanStep> = steps
        .iter()
        .filter_map(|s| {
            let kind = s.get("kind")?.as_str()?.to_string();
            Some(TestPlanStep {
                kind,
                cmd: s.get("cmd").and_then(|v| v.as_str()).map(|s| s.to_string()),
                label: s
                    .get("label")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
                check: s
                    .get("check")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
            })
        })
        .collect();
    Ok(Some(result))
}

/// #259: identifies what kind of sidebar row a context menu was opened
/// against.  Lets `dispatch_context_menu_action` route the action with
/// row-specific context (e.g. which issue number to copy, which repo
/// to scope a `gh issue edit` call against, etc.).
///
/// `repo_name` carries the **coord-local** name (matches `coordinator.yml`),
/// which is what `coord refine` / `coord ready` / etc. take as their
/// `<repo>` arg — the GH slug is looked up internally on the Python side.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum ContextMenuTarget {
    /// Right-click on a Board sidebar row.
    BoardRow {
        /// Issue under the cursor, or `None` when the click landed on
        /// a section header / empty space.
        issue_number: Option<u64>,
        /// Coord-local repo name for the row.
        repo_name: Option<String>,
        /// #260: lifecycle classification of the row at right-click
        /// time.  Drives which menu items appear (e.g. Refine only on
        /// Backlog rows).  See `Self::lifecycle_for_target` for how
        /// this is computed.
        lifecycle: BoardRowLifecycle,
    },
    /// Right-click on a Pipeline sidebar row.
    PipelineRow {
        issue_number: Option<u64>,
        /// Coord-local repo name for the row, drawn from
        /// `PipelineIssue::coord_repo`.  Used to gate live/zombie-session
        /// actions (Reattach vs Start) repo-precisely so that repo-a/#N and
        /// repo-b/#N don't cross-contaminate each other's menus (#983).
        /// `None` when no row is selected or the issue has no `coord_repo`.
        repo_name: Option<String>,
        /// #262: lifecycle classification of the row at right-click
        /// time.  Drives which menu items appear (e.g. Start with
        /// Plan / Skip Plan only on New rows).
        lifecycle: PipelineRowLifecycle,
    },
    /// Right-click on a Machines sidebar row.  Used for the routing-pause
    /// menu (`Pause` / `Resume`) — the user can steer agents away from a
    /// machine without editing coordinator.yml or shelling out.
    MachineRow { name: String, is_paused: bool },
    /// #771: right-click (or the keyboard shortcut) on the Milestone DAG
    /// view's milestone header. Carries what `coord milestone dispatch`
    /// needs — the coord-local repo name and the tracking-issue number —
    /// so the menu's "Dispatch milestone" item can spawn it directly.
    ///
    /// #1003: also reused for a right-click on a **Plans-panel** row (the
    /// Plans panel "elevates and subsumes" this view per its own doc
    /// comment) — `milestone_number` was added so the CRUD items (Edit
    /// milestone…, Add/Remove issue…) have what `coord milestone
    /// edit`/`assign`/`remove` need without a second GH round trip.
    MilestoneHeader {
        repo_name: String,
        tracking_issue: u64,
        milestone_title: String,
        milestone_number: i64,
    },
    /// #956: right-click on a Terminal-view tree TERMINAL row (not a
    /// machine row — those have no menu yet). Carries what "Kill terminal"
    /// needs to dispatch `coord terminal kill <machine>:<name>`.
    TerminalRow { machine: String, name: String },
    /// #1123: right-click on a Plans-panel row with no tracking epic yet
    /// (`PlanRosterEntry::tracking_issue == None`), or on the repo-header
    /// row / empty main-panel space. Kept distinct from `MilestoneHeader`
    /// (which requires a `tracking_issue: u64` to act on) because none of
    /// these targets have an epic yet for the eleven-item CRUD menu to
    /// operate on — the menu offered instead is either "create one" for a
    /// specific stub row, or "New plan" for no specific row (contract
    /// §4b/§4c, `tests/acceptance/ms-38/contract.md`).
    PlansStub {
        /// Coord-local repo name to scope a new plan into, when known:
        /// the repo of the stub milestone under the cursor, or (for a
        /// repo-header/empty-space click) whichever repo the Plans sidebar
        /// is currently scoped to. `None` when nothing can be inferred.
        repo_name: Option<String>,
        /// `Some((milestone_number, title))` when a specific epic-less
        /// milestone row was under the cursor (contract §4b). `None` for
        /// the repo-header/empty-space case (contract §4c).
        milestone: Option<(i64, String)>,
    },
    /// #1631 (H-4): right-click anywhere on the status bar. Carries no
    /// payload — the menu it opens lists "Fleet health…" (opens the
    /// fleet-health detail overlay) and, since #1755, "Drive queue…"
    /// (switches to the Queue panel, #1868), unconditionally, regardless of
    /// where in the bar the click landed.
    ///
    /// **Deliberately still one target for the whole bar**, not one per
    /// segment: `StatusBarSegment::action_id` exists on the wire but this
    /// codebase wires no per-segment click dispatch anywhere, and adding one
    /// was explicitly out of scope for #1755. See `fleet_health.rs`'s module
    /// doc comment.
    FleetHealth,
    /// #1755 (DQ-3): right-click on a row of the Queue panel (`drive_queue.rs`
    /// — the drive-queue detail overlay this target used to also serve was
    /// retired by #1868). Carries the row's identity + state so the menu can
    /// offer Remove / Move / Unblock without re-reading a selection that may
    /// have moved.
    DriveQueueRow {
        repo_name: String,
        issue_number: i64,
        /// `waiting` / `running` / `blocked` / … — gates "Unblock" (only a
        /// `blocked` row has anything to unblock).
        state: String,
        /// 0-based position, so "Move up"/"Move down" can be disabled at the
        /// ends rather than silently no-op'ing.
        position: i64,
        /// Total queue length — the other half of the end-of-queue gate.
        queue_len: usize,
        /// #1757: this row's DEPLOY GATE has fired and is holding the queue —
        /// gates "Resume". Captured here rather than re-read at click time
        /// for the same reason `state` is: the selection may have moved.
        held: bool,
    },
    /// #2454: right-click on a row of the **Reports** panel's result table.
    ///
    /// Carries only the row's *identity* — the coord-local repo name and the
    /// issue number, resolved generically through the catalogue's
    /// [`ReportRowIdentity`] declaration. Deliberately NOT the row's content:
    /// a "jump elsewhere" menu never needs it, and carrying it would be the
    /// first step back toward the per-report `match` inside `reports.rs` that
    /// #2405 declined to introduce (see that module's header).
    ///
    /// A report whose catalogue entry declares no `row_identity`, or a row
    /// whose declared cells don't parse, produces no target at all — so the
    /// menu simply never opens there rather than opening a menu of disabled
    /// items with nothing to act on.
    ReportRow {
        /// Coord-local repo name (matches `coordinator.yml`), from the
        /// column the catalogue named.
        repo_name: String,
        issue_number: u64,
    },
    /// #2287 (ms-65 §8c): right-click on a Board document tab (the strip
    /// above the detail pane, #2282). Carries the RIGHT-CLICKED tab's strip
    /// index — deliberately not necessarily the active tab, since "Close"
    /// and "Pin tab" both target the clicked tab, not whichever one happens
    /// to be active (see `dialogs.rs::context_menu_items_for_board_doc_tab`
    /// and finding 16 in `tests/acceptance/ms-65/manifest.yml`).
    BoardDocTab {
        /// Strip index of the clicked tab at right-click time.
        idx: usize,
        /// Whether the clicked tab is already pinned — gates "Pin tab"
        /// (contract §8c: "hidden or inert" on an already-pinned tab; this
        /// codebase's reading is inert, via `ContextMenuItem::
        /// disabled_because`).
        is_pinned: bool,
    },
    /// #2533 (ms-67 contract §4a): right-click on an Approved-work-items row.
    /// Carries only the submission id — the menu builder
    /// (`context_menu_items_for_approved_row`) re-resolves the row from
    /// `self.data.approved_submissions` at build/dispatch time rather than
    /// caching its mapped-repo state here, so a `/board` poll that changes
    /// the mapping between right-click and click can't leave a stale
    /// enabled/disabled decision baked into the target.
    ApprovedRow { submission_id: String },
}

/// #262: lifecycle bucket for a Pipeline sidebar row at right-click
/// time.  Mirrors the umbrella's three Pipeline sections (New /
/// In-progress / Done) plus a catch-all for the existing classifier's
/// "refining" / "new" rows (coord-labelled but not dispatch-ready),
/// where Start is not yet appropriate.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum PipelineRowLifecycle {
    /// `coord` + `status:ready`, no assignments — ready to dispatch.
    New,
    /// Has at least one assignment.
    InProgress,
    /// Closed issue with assignments.
    Done,
    /// Other state (e.g. coord-labelled but `status:refining`, or
    /// coord-labelled without a `status:*` label — still needs
    /// scoping before Start is meaningful).
    Other,
}

/// #260: lifecycle bucket for a Board sidebar row at right-click time.
/// Different from `IssueGroup::lifecycle_section()` because this also
/// distinguishes Pending into Backlog / Refining / Refined based on
/// labels — `#226` will eventually surface those as separate sections,
/// but right-click actions need the distinction now so they can offer
/// the right next-step verb.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum BoardRowLifecycle {
    /// Open issue, no `status:*` label, no assignments.
    Backlog,
    /// Open issue with `status:refining` label.
    Refining,
    /// Open issue with `status:ready` label, no `coord` label yet.
    Refined,
    /// Has at least one assignment, issue still open.
    InFlight,
    /// Closed issue with at least one assignment.
    Completed,
    /// Couldn't classify (no row focused / row not in any known state).
    Unknown,
}

/// Whether a Merge action against the currently-selected Pipeline row
/// will actually do something useful.  Drives both the dispatch path
/// (so silent no-ops become actionable toasts) and the toolbar button's
/// enabled state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum PipelineMergeState {
    /// View isn't the Pipeline panel, or no row is selected.
    NotApplicable,
    /// Selected issue has no merge_queue entry — the worker hasn't
    /// pushed a branch / opened a PR yet.
    NoQueue { issue: u64 },
    /// Selected issue's merge_queue entry is already merged.
    Merged { issue: u64 },
    /// Adversarial-review verdict isn't `approve` (`request-changes`,
    /// `pending`, or never run).  Server-side `coord merge` will
    /// refuse; surfacing it here gives the user actionable feedback.
    BlockedOnReview { issue: u64, verdict: String },
    /// CI checks are failed for the PR.  Falls through to the existing
    /// `pending_force_merge` confirm prompt so the user can opt in.
    BlockedOnCi { issue: u64, repo: String },
    /// Safe to dispatch `coord merge --repo <repo>`.
    /// `repo_slug` is the GitHub `owner/name` slug used to key
    /// `pipeline_inflight_merges`; `repo` is the coord-local name passed to
    /// `--repo`.
    Ready { issue: u64, repo: String, repo_slug: String },
}

/// #259: one item in an open context menu.  Lightweight engine-side
/// shape converted to `quadraui::ContextMenuItem` at render time.
#[derive(Clone, Debug)]
pub(crate) struct ContextMenuItem {
    /// Action identifier dispatched on click / Enter.
    /// `None` with `submenu.is_none()` ⇒ separator.
    /// `None` with `submenu.is_some()` ⇒ pull-right parent.
    pub(crate) action_id: Option<String>,
    pub(crate) label: String,
    /// Optional right-aligned shortcut hint (e.g. `"r"`).
    pub(crate) shortcut: Option<String>,
    pub(crate) disabled: bool,
    /// #1598: short reason this item is disabled, shown as its
    /// right-aligned hint (via [`super::dialogs::coord_item_to_qui`]) in
    /// place of `shortcut` — a disabled item with no explanation is a dead
    /// end for the operator. `None` for a disabled item just means no
    /// reason was given (falls back to `shortcut`, if any).
    pub(crate) disabled_reason: Option<String>,
    /// Pull-right submenu (#607).  When `Some`, activating the item opens
    /// the child menu instead of dispatching an action.
    pub(crate) submenu: Option<Vec<ContextMenuItem>>,
}

impl ContextMenuItem {
    pub(crate) fn action(id: &str, label: &str) -> Self {
        Self {
            action_id: Some(id.to_string()),
            label: label.to_string(),
            shortcut: None,
            disabled: false,
            disabled_reason: None,
            submenu: None,
        }
    }
    pub(crate) fn separator() -> Self {
        Self {
            action_id: None,
            label: String::new(),
            shortcut: None,
            disabled: false,
            disabled_reason: None,
            submenu: None,
        }
    }
    /// Create a pull-right parent item whose children appear in a submenu.
    pub(crate) fn parent(label: &str, children: Vec<ContextMenuItem>) -> Self {
        Self {
            action_id: None,
            label: label.to_string(),
            shortcut: None,
            disabled: false,
            disabled_reason: None,
            submenu: Some(children),
        }
    }
    pub(crate) fn with_shortcut(mut self, s: &str) -> Self {
        self.shortcut = Some(s.to_string());
        self
    }
    /// #1598: mark this item disabled and attach a short reason, shown as
    /// its right-aligned hint in place of `shortcut`.
    pub(crate) fn disabled_because(mut self, reason: &str) -> Self {
        self.disabled = true;
        self.disabled_reason = Some(reason.to_string());
        self
    }
    /// True iff this item is a visual separator (neither action nor parent).
    pub(crate) fn is_separator(&self) -> bool {
        self.action_id.is_none() && self.submenu.is_none()
    }
    /// True iff this item can be selected (action or submenu parent, not disabled).
    pub(crate) fn is_selectable(&self) -> bool {
        (self.action_id.is_some() || self.submenu.is_some()) && !self.disabled
    }
}

#[derive(Clone, serde::Deserialize)]
#[allow(dead_code)] // pr_url stored for future display
pub(crate) struct MergeQueueEntry {
    pub(crate) assignment_id: String,
    #[serde(default)]
    pub(crate) issue_number: Option<u64>,
    pub(crate) state: String,
    #[serde(default)]
    pub(crate) pr_number: Option<i64>,
    #[serde(default)]
    pub(crate) pr_url: Option<String>,
    /// Repo slug (owner/name) — keys the board-synced CI summary lookup in
    /// `pipeline_ci_checks` (see [`PlannedMergeEntry::ci_summary`]).
    /// Joined from the `merge_queue.repo_github` column.
    pub(crate) repo_github: String,
    /// Target branch the PR merges into (e.g. "main").  `None` for entries
    /// written before this column was read by the TUI.
    #[serde(default)]
    pub(crate) target_branch: Option<String>,
    /// Last gate-eval error string from `coord merge`, if any.  Non-empty
    /// is the single most useful clue when a merge is stalled.
    #[serde(default)]
    pub(crate) error: Option<String>,
    /// Worker branch name — joined from `assignments.branch`.  Used by
    /// `compute_staging_local` for branch-level dedup (#778): a fix worker
    /// that shares a branch with an already-queued original work assignment
    /// must be excluded from staging even though it has a different assignment_id.
    #[serde(default)]
    pub(crate) branch: Option<String>,
    /// Milestone title — client-side join from `open_issues` keyed on
    /// `(repo_name, issue_number)`.  `None` when the issue carries no
    /// milestone or the issue row is absent from `open_issues`.
    #[serde(default)]
    pub(crate) milestone_title: Option<String>,
    /// Unix timestamp of the last merge attempt (`coord/merge_queue.py` sets
    /// this immediately before calling `gh pr merge`, and leaves it untouched
    /// on success) — for a `state == "merged"` entry this IS the merge time.
    /// #913: the Pipeline Done section's recency window + sort use this (via
    /// `issue_done_at`) instead of the work assignment's `finished_at`, so a
    /// freshly-merged item lands in Done immediately rather than being keyed
    /// off however long ago the work itself finished.
    #[serde(default)]
    pub(crate) last_attempt: Option<f64>,
}

/// One entry in the server-side merge plan from #776.
///
/// Deserialized from the `merge_plan` key of the `/board` payload, which is
/// computed by `coord.merge_queue.plan()` and injected by `serve_app.py`.
/// Unlike the raw `MergeQueueEntry` (DB row), this carries computed fields
/// that are always fresh: `rank` (true merge order), `status` (READY /
/// BLOCKED / MERGING / MERGED / NEEDS_ATTENTION), `reason` (live gate
/// explanation), `size` (diff lines), `enqueued_at`, and `last_attempt`.
///
/// The panel reads this structure; it does **not** re-derive ordering or gate
/// status in Rust.
#[derive(Clone, serde::Deserialize, Default)]
pub(crate) struct PlannedMergeEntry {
    pub(crate) assignment_id: String,
    pub(crate) repo_name: String,
    pub(crate) repo_github: String,
    /// #2397: worker branch — the merge queue dedups by this (not
    /// `assignment_id`), so it's the precise key for the #1084 prereq
    /// track's Merge-stage lookup (`merge_plan_block_reason_for_branch`),
    /// which can't use `issue_number` alone (a shared tracking issue can
    /// carry both Gate A's and a member issue's JIT queue entry).
    pub(crate) branch: String,
    pub(crate) target_branch: String,
    pub(crate) issue_number: u64,
    pub(crate) issue_title: String,
    /// 1-based position in the true merge sequence across all repos.
    pub(crate) rank: u32,
    /// Diff size in lines (populated at enqueue; `None` = unknown).
    #[serde(default)]
    pub(crate) size: Option<i64>,
    /// Computed status: "READY" | "BLOCKED" | "MERGING" | "MERGED" | "NEEDS_ATTENTION".
    pub(crate) status: String,
    /// Human-readable explanation of why the entry is BLOCKED (None when not blocked).
    #[serde(default)]
    pub(crate) reason: Option<String>,
    /// Unix timestamp when the entry was enqueued.
    #[serde(default)]
    pub(crate) enqueued_at: Option<f64>,
    /// Unix timestamp of the last merge attempt.
    #[serde(default)]
    pub(crate) last_attempt: Option<f64>,
    /// Issue milestone title (None when no milestone).  Stored for future
    /// display (e.g. milestone grouping in a later phase); not shown in the
    /// #777 panel which groups by repo→target_branch instead.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) milestone: Option<String>,
    /// PR number for this entry, when one is open (mirrors `QueuedMerge.pr_number`
    /// on the Python side). `None` before a PR exists.
    #[serde(default)]
    pub(crate) pr_number: Option<i64>,
    /// #1344: structured CI rollup computed server-side by
    /// `coord.merge_queue.plan()` from the tick-refreshed gate snapshot
    /// (`coord/gate_snapshot.py`, #1336) — replaces the TUI's former
    /// per-PR `gh pr checks` client-side poll loop. `None` when no PR is
    /// open yet, or the CI store has no checks recorded for this PR.
    #[serde(default)]
    pub(crate) ci_summary: Option<PlannedMergeCiSummary>,
    /// #2397: mirrors `merge.auto_drain` (the config flag gating the
    /// daemon's background auto-retry tick) at the moment this plan was
    /// built — `false` (matching the config default) when an older daemon
    /// doesn't send it yet.
    ///
    /// Not currently read by the TUI: an earlier version of
    /// `format::merge_wait_affordance` branched on this to distinguish
    /// "nothing retries until a human runs `coord merge`" from "an
    /// automatic retry is already in flight," but that was factually wrong
    /// — `_auto_drain_tick` (coord/serve_app.py) only ever retries entries
    /// already marked `PLAN_READY`; a `PLAN_BLOCKED` entry (the only case
    /// `reason` is `Some`, which is the only case this field would matter
    /// for) is filtered out before any retry logic runs, so `auto_drain`
    /// has no bearing on it. Kept on the wire (the daemon still sends it)
    /// in case a genuinely `auto_drain`-sensitive display need shows up
    /// later; see #2397's review discussion for the full trace.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) auto_drain: bool,
}

/// Structured CI check rollup for one PR, deserialized from the `ci_summary`
/// key of a `merge_plan` entry. See [`PlannedMergeEntry::ci_summary`].
#[derive(Clone, Debug, serde::Deserialize, Default)]
pub(crate) struct PlannedMergeCiSummary {
    pub(crate) passed: usize,
    pub(crate) failed: usize,
    pub(crate) running: usize,
    #[serde(default)]
    pub(crate) failed_names: Vec<String>,
    #[serde(default)]
    pub(crate) first_failed_url: Option<String>,
}

/// One entry in the server-side per-issue stage/gate projection (#550).
///
/// Deserialized from the `issue_stage_projection` key of the `/board`
/// payload, computed by `coord.stage_projection.compute_board_stage_projection`
/// and injected by `serve_app.py` — generalizes the #776/#778 pattern to
/// coord-tui's `pipeline.rs` stage-status functions (`stage_status_for`,
/// `merge_stage_status_for`, `test_stage_status_for`,
/// `issue_has_any_approved_review`).
///
/// `stages` maps a stage name (e.g. "work", "review", "test", "merge") to a
/// lowercase status string (`"pending"|"active"|"done"|"failed"|"stale"|"skipped"`)
/// — kept as a plain string on the wire (not `quadraui::StageStatus` directly)
/// so this crate isn't coupled to that enum's serde representation; see
/// [`parse_stage_status`] in `pipeline.rs` for the mapping.
///
/// Deliberately excludes TUI-session-local overlays (an optimistic
/// "merge just dispatched" flag, a locally-spawned test-build subprocess,
/// the TUI's own CI-check poll cache) — those are applied client-side on top
/// of this projection; see `pipeline.rs` for where each is layered back in.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct IssueStageProjection {
    pub(crate) repo_name: String,
    pub(crate) issue_number: u64,
    #[allow(dead_code)]
    pub(crate) issue_title: String,
    pub(crate) stages: std::collections::HashMap<String, String>,
    pub(crate) has_approved_review: bool,
}

/// One node in a milestone work order from the `/board` payload (#795 Phase 3b).
///
/// Deserialized from the `milestone_work_orders[*].nodes[*]` key of the
/// `/board` payload, computed by `coord/serve_app.py` from the tracking
/// issue's `## Work order` block via `coord.milestone_order`. The TUI uses
/// this to render work-order rank, next-up, and blocked-on badges on Pipeline
/// milestone cards without re-implementing the DAG logic in Rust.
///
/// `rank` is 0-indexed position in the declared work order. `ready` is `true`
/// when all `after:` dependencies are terminal. `next_up` refines `ready`
/// further: the issue is ready AND not already claimed by an active assignment.
/// `blocked_on` carries the still-unmet dependency issue numbers when `ready`
/// is `false`.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct MilestoneWorkOrderNode {
    pub(crate) issue_number: u64,
    /// 0-indexed position in the `## Work order` list (display as rank+1).
    pub(crate) rank: u32,
    pub(crate) ready: bool,
    /// `true` when `ready` AND not claimed — the dispatcher's next candidates.
    pub(crate) next_up: bool,
    #[serde(default)]
    pub(crate) blocked_on: Vec<u64>,
}

/// One child of an epic tracking issue, from the `/board` payload's
/// top-level `children[*].children[*]` (#1195 EP-1 seam).
///
/// `state` reflects the tracking issue's `## Sub-issues` checklist checkbox
/// (`"closed"` when checked, `"open"` otherwise) — an approximation of the
/// real GitHub issue state, computed server-side by
/// `coord.parentage.MarkdownParentage` (see its docstring). Good enough for
/// nesting/display; #1197 additionally cross-references
/// `data.open_issues`/`data.assignments` via `milestone_dag::build_dag_nodes`
/// for a live Done/InFlight/Ready badge rather than trusting this checkbox
/// state directly.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct EpicChild {
    pub(crate) number: u64,
    #[allow(dead_code)]
    pub(crate) state: String,
}

/// One epic's child-issue list from the `/board` payload (#1195 EP-1 seam,
/// consumed for Pipeline tree nesting by #1197).
///
/// Deserialized from `children[*]` — computed server-side by
/// `coord/serve_app.py` via `coord.parentage.MarkdownParentage` over the
/// tracking issue's own already-cached body (the `## Sub-issues` checklist,
/// #1008), so no extra `gh`/network round trip. Only epics with at least one
/// child are published. Empty on daemons older than #1195.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct EpicChildren {
    pub(crate) repo_name: String,
    pub(crate) tracking_issue: u64,
    #[serde(default)]
    pub(crate) children: Vec<EpicChild>,
}

/// One milestone's work-order data from the `/board` payload (#795 Phase 3b).
///
/// Deserialized from `milestone_work_orders[*]` in the `/board` payload. The
/// TUI looks up `(repo_name, issue_number)` across all `MilestoneWorkOrder`
/// entries to augment Pipeline milestone card rows with rank and frontier state.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct MilestoneWorkOrder {
    pub(crate) repo_name: String,
    /// Tracking issue number (the "epic"-labelled issue carrying the
    /// `## Work order` block). Not rendered directly in the #795 Phase 3b
    /// UI, but read by `milestone_tracking_issue_for` (#1060) to build the
    /// `coord acceptance author <repo> <tracking_issue> --issue N` argument
    /// list for a member issue's per-row context-menu action.
    pub(crate) tracking_issue: u64,
    /// Milestone title from the tracking issue's GitHub milestone field.
    /// Kept for future display; not shown in the #795 Phase 3b card badges.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) milestone_title: String,
    #[serde(default)]
    pub(crate) nodes: Vec<MilestoneWorkOrderNode>,
}

/// One row in the milestone plan-roster from the `/board` payload (#975).
///
/// Deserialized from the top-level `plan_roster[*]` list, computed
/// server-side by `coord/serve_app.py` via
/// `coord.plans.aggregate_repo_plans` — one entry per milestone/epic across
/// all configured repos.  The coord-tui "Plans" panel renders one row per
/// entry with ready / blocked / in-flight / done counts and a `needs_you`
/// attention list.
///
/// **Serde note (#632).** Every field here must match the JSON type emitted
/// by `PlanEntry.to_dict()` in `coord/plans.py`.  Counts are integers,
/// `has_work_order` is a bool (Python bool → JSON true/false, so no
/// `de_bool_from_int_or_bool` needed), `tracking_issue` can be null.  A
/// mistyped field would fail the whole `BoardPayload` parse and blank the
/// board on load.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct PlanRosterEntry {
    /// Coord-local repo name (matches `coordinator.yml`).
    pub(crate) repo: String,
    /// Milestone title, e.g. `"Substrate"`.
    pub(crate) title: String,
    /// GitHub milestone number.
    pub(crate) milestone_number: i64,
    /// Tracking-epic issue number, or `None` when the milestone has no
    /// `"epic"`-labelled issue.  When present, selecting the row opens this
    /// issue in the browser via `gh issue view --web`.
    #[serde(default)]
    pub(crate) tracking_issue: Option<u64>,
    /// True iff the tracking epic body carries a parseable `## Work order`
    /// block with ≥1 node.  False for milestones surfaced in the roster
    /// purely because member issues reference them (no epic yet) — those
    /// get `needs_you: ["no_work_order"]` and zero counts.
    #[serde(default)]
    pub(crate) has_work_order: bool,
    /// Work-order nodes on the ready frontier (dependencies met, unclaimed,
    /// not terminal) — the dispatcher's next candidates.
    #[serde(default)]
    pub(crate) ready_frontier: u32,
    /// Work-order nodes blocked by unmet dependencies or a conflict.
    #[serde(default)]
    pub(crate) blocked: u32,
    /// Work-order nodes currently claimed by an active board assignment or
    /// a remote `issue-N-*` branch.
    #[serde(default)]
    pub(crate) in_flight: u32,
    /// Work-order nodes that have reached a terminal (closed) state.
    #[serde(default)]
    pub(crate) done: u32,
    /// Total work-order nodes declared under this milestone.
    #[serde(default)]
    pub(crate) total: u32,
    /// Ordered attention signals — currently one of:
    ///   * `"no_work_order"` — open milestone with no parseable work order
    ///   * `"ready_waiting"` — ≥1 ready-frontier entry exists to dispatch
    ///   * `"stalled"` — open with a work order but nothing ready or in flight
    /// #976 will paint these as health chips on each row.
    #[serde(default)]
    pub(crate) needs_you: Vec<String>,
    /// #886 Phase 2: the latest Milestone Outcome Audit (`--audit-of`) run
    /// number for this milestone's epic. `None` when no audit has ever run
    /// against it — the Outcome chip is omitted entirely in that case rather
    /// than showing a fabricated 0/0.
    #[serde(default)]
    pub(crate) outcome_run_number: Option<u32>,
    /// Goals verdicted `"met"` in the latest run.
    #[serde(default)]
    pub(crate) outcome_met: Option<u32>,
    /// Goals verdicted `"partial"` in the latest run.
    #[serde(default)]
    pub(crate) outcome_partial: Option<u32>,
    /// Goals verdicted `"gap"` in the latest run — the count the done-gate
    /// cares about: milestone completion is judged by goals met, not issues
    /// closed.
    #[serde(default)]
    pub(crate) outcome_gap: Option<u32>,
    /// The latest run's one-line bottom-line verdict (e.g. "5/6 goals met").
    #[serde(default)]
    pub(crate) outcome_bottom_line: Option<String>,
    /// Pre-rendered delta vs the previous run (e.g. "v1→v2: closed: tests.rs
    /// split; still open: #550"), computed server-side by
    /// `coord.plans._latest_audit_outcome`. `None` on the first run (nothing
    /// to diff against) or when neither run moved any goal.
    #[serde(default)]
    pub(crate) outcome_diff_summary: Option<String>,
}

/// GOAL.md pinned north-star header for the Plans panel (#978).
///
/// Deserialized from the top-level `goal_header` object, computed
/// server-side by `coord.goal.read_goal_header()` and injected into the
/// `/board` payload by `coord/serve_app.py`'s `board()` handler. GOAL.md is a
/// repo-root doc that isn't shipped in the `coord` PyPI package, so this is
/// only ever populated when the daemon happens to be running from an actual
/// git checkout — everywhere else (older daemons that predate #978, packaged
/// installs, local-SQLite-mode with no daemon at all) `available` is `false`
/// (the `Default` impl, also what `#[serde(default)]` on `BoardData`'s field
/// produces when the key is absent entirely) and the Plans panel renders
/// exactly as it did before this field existed — no header strip, full rect
/// to the roster.
/// #2608: the machine-local fleet-roll marker, surfaced on the `/board`
/// payload's optional `roll_pending` key — same shape as
/// `coord.drive_queue.RollPending.to_dict()`. Set by `coord release
/// propagate`/`nightly-window`, read every tick by
/// `coord.drive_queue.plan_tick` (via the shell's `read_roll_pending`),
/// which refuses to LAUNCH a new drive while this is live — reconciliation
/// (steps 1/1b) runs unaffected. `None` — via `#[serde(default)]`, covering
/// both an absent key (a daemon predating #2608) and an explicit JSON
/// `null` (no roll pending right now) — means the Queue panel renders
/// exactly as it did before this field existed: no banner, no layout shift.
/// Deliberately not folded into the alert/escalation channel: a queue held
/// for a roll is expected, self-clearing behaviour, never surfaced as
/// broken (mirrors `coord drive-queue status`'s own non-alarming rendering
/// of this same marker).
#[derive(Clone, Debug, serde::Deserialize)]
pub(crate) struct RollPending {
    /// The version this roll targets.
    pub(crate) target_version: String,
    /// `time.time()` epoch when the marker was set — the TTL's origin.
    pub(crate) set_at: f64,
    /// Free text naming who/why set it ("nightly-window" / "propagate").
    #[serde(default)]
    pub(crate) reason: String,
    #[serde(default)]
    pub(crate) ttl_seconds: f64,
    #[serde(default)]
    pub(crate) max_deferrals: i64,
    /// Consecutive ticks that observed the marker still pending and the
    /// fleet still busy — bumped by the shell each tick it defers the roll.
    #[serde(default)]
    pub(crate) deferrals: i64,
}

#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct GoalHeader {
    /// True iff the daemon located and parsed a GOAL.md this tick.
    #[serde(default)]
    pub(crate) available: bool,
    /// The north-star one-liner (bolded sentence under GOAL.md's "North
    /// star" heading, or its H1 title as a fallback), pre-truncated
    /// server-side.
    #[serde(default)]
    pub(crate) headline: String,
    /// ISO `YYYY-MM-DD` parsed from GOAL.md's `_Last updated: ..._` line, or
    /// `None` if that line is missing/malformed.
    #[serde(default)]
    pub(crate) last_updated: Option<String>,
    /// Server-computed `today - last_updated` in days (Python's `datetime`
    /// does the calendar math so the Rust side never needs a date crate).
    /// `None` whenever `last_updated` is `None`.
    #[serde(default)]
    pub(crate) days_since_update: Option<i64>,
}

/// #1039: one row from the `/audit` endpoint (#1037) — deserialized
/// verbatim from the `entries[*]` wire shape pinned by contract §6
/// (`tests/acceptance/ms-33/contract.md`).
///
/// `pub`, not `pub(crate)`: this is the parameter type of
/// `app::fixtures::make_app_with_audit_json` (test-support-feature-gated),
/// which must be at least as visible as that `pub fn` (E0446) to be
/// reachable from an external integration-test crate — same rationale as
/// `Assignment` above.
#[derive(Clone, Debug, serde::Deserialize)]
pub struct AuditEntry {
    pub id: i64,
    pub ts: f64,
    #[serde(default)]
    pub tier: Option<String>,
    pub category: String,
    pub event_type: String,
    pub actor: String,
    #[serde(default)]
    pub repo: Option<String>,
    #[serde(default)]
    pub issue: Option<u64>,
    #[serde(default)]
    pub assignment_id: Option<String>,
    #[serde(default)]
    pub machine: Option<String>,
    pub summary: String,
    /// JSON-decoded `details` object — `null` (→ `None`) when the source
    /// `details_json` column was `NULL`.
    #[serde(default)]
    pub details: Option<serde_json::Value>,
}

/// #1039: the `/audit` endpoint's paginated response envelope (contract
/// §6). Cached on `CoordApp` (see `audit_page`), populated by
/// `spawn_audit_fetch`/`data::spawn_audit_fetch` — deliberately kept off
/// `BoardData`/`BoardPayload` (the epic's instruction: "Do not add the log
/// to BoardPayload" — `/board` carries only the `audit_recent_count`
/// summary integer below).
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub struct AuditPage {
    #[serde(default)]
    pub entries: Vec<AuditEntry>,
    /// Keyset cursor for the next page (contract §6). Unread in this slice:
    /// `spawn_audit_fetch` only ever requests the first page — #1040 wires
    /// `since`/`category`/`type` filters onto that first-page request, but
    /// "load more" pagination is still a later issue; kept on the
    /// wire-shape type now so that doesn't need another deserializer change.
    #[serde(default)]
    #[allow(dead_code)]
    pub next_cursor: Option<String>,
    /// Whether more pages exist beyond `next_cursor`. Same rationale as
    /// `next_cursor` above.
    #[serde(default)]
    #[allow(dead_code)]
    pub has_more: bool,
}

// ── #1741: Reports panel wire shapes (`GET /report`, `GET /report/{id}`) ──
//
// These mirror `coord/reports.py`'s `ReportParam.to_dict` / `ReportDef.
// to_dict` / `ReportResult.to_dict` (#1742) field-for-field. They are the
// ONLY report-shaped types in `tui/**`: the panel builds its section list
// and every parameter control from the catalogue at runtime, so adding a
// second report to `coord/reports.py` must require zero Rust changes.
//
// `pub`, not `pub(crate)`, for the same E0446 reason `AuditEntry` above is:
// they are named by the `test-support`-gated `app::fixtures` helpers.

/// One parameter of a report, as described by `GET /report`'s catalogue.
///
/// `kind` is `"choice"` (render a picker over `choices`) or `"text"` (free
/// text). `free_form` marks a `choice` whose `choices` are presets rather
/// than a whitelist — the server-side validator is the authority either way,
/// so the panel never rejects a value locally.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub struct ReportParamDef {
    pub id: String,
    #[serde(default)]
    pub label: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub choices: Vec<String>,
    #[serde(default)]
    pub default: String,
    #[serde(default)]
    pub help: String,
    #[serde(default)]
    #[allow(dead_code)]
    pub free_form: bool,
}

/// #2454: which two `columns` of a report's rows name the `(repo, issue)`
/// that row is *about*, mirroring `coord/reports.py`'s `RowIdentity`.
///
/// This is the whole reason the Reports panel can offer a right-click "View
/// on Board" on a result row without a per-report `match`. A jump only needs
/// row **identity**, never row **content**, so the catalogue declaring where
/// that identity lives is enough — the panel reads an optional field and
/// stays exactly as generic as `reports.rs`'s module doc requires (adding
/// report #2 still requires zero `tui/**` changes; a report that wants the
/// menu just declares `row_identity` server-side).
///
/// `repo_column` names the column holding the **coord-local** repo name —
/// the one `select_issue` / the Board doc-tab `DocKey` are keyed by, not the
/// GitHub `owner/name` slug.
///
/// Absent for every report whose rows have no single issue (`usage` grouped
/// `by=repo`, `decisions`' cards, `queue-outcomes`' per-period aggregates),
/// and absent entirely from a daemon that predates #2454 — in both cases the
/// panel simply offers no row menu.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub struct ReportRowIdentity {
    #[serde(default)]
    pub repo_column: String,
    #[serde(default)]
    pub issue_column: String,
}

/// One catalogue entry from `GET /report` — everything needed to build a
/// section header and its parameter form, minus the server-side callable.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub struct ReportDef {
    pub id: String,
    #[serde(default)]
    pub title: String,
    // #1911: the sidebar's `MultiSectionView` section header is one line
    // (title + badge) and the body is the parameter form — neither has room
    // for a description, so nothing in `tui/**` reads this today. Kept
    // (rather than dropped) because it is part of `GET /report`'s wire
    // contract; deserialisation must not choke on a field the daemon still
    // sends. Same posture as `ReportParamDef::free_form` just below.
    #[serde(default)]
    #[allow(dead_code)]
    pub description: String,
    #[serde(default)]
    pub params: Vec<ReportParamDef>,
    /// #2454: optional per-row `(repo, issue)` identity — see
    /// [`ReportRowIdentity`]. `#[serde(default)]` is the whole
    /// forward/backward-compatibility story: a daemon that predates the
    /// field sends nothing and this stays `None`, so the panel renders (and
    /// right-clicks) exactly as it did before.
    #[serde(default)]
    pub row_identity: Option<ReportRowIdentity>,
}

/// The `GET /report` response envelope (`{"reports": [...]}`).
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub struct ReportCatalogue {
    #[serde(default)]
    pub reports: Vec<ReportDef>,
}

/// #1760: additive display metadata for one result column, mirroring
/// `coord/reports.py`'s `ColumnMeta.to_dict` field-for-field.
///
/// **Every field is optional and every field is a hint.** The wire's
/// `columns`/`rows` stay byte-identical whether or not `column_meta` is
/// present, so a daemon that predates #1760 simply sends none and the panel
/// falls back to its pre-#1762 rendering. `kind` in particular is an open
/// vocabulary — a newer daemon may declare a kind this binary has never
/// heard of, and the renderer must degrade to plain stringification rather
/// than panic (see `CoordApp::reports_cell_text`).
///
/// `id` matches the corresponding `columns[]` entry and the order matches
/// too, so a client can zip them — but this panel looks meta up **by `id`**,
/// for exactly the reason cells are looked up by column name: a daemon that
/// ships a partial or reordered `column_meta` must mis-render nothing.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub struct ReportColumnMeta {
    #[serde(default)]
    pub id: String,
    /// Human-facing column title. Empty → fall back to `id`.
    #[serde(default)]
    pub label: String,
    /// `"text" | "int" | "timestamp" | "list" | "enum" | "duration"`, or
    /// anything a future daemon invents.
    #[serde(default)]
    pub kind: String,
    /// `"left"` | `"right"` (anything else is treated as `"left"`).
    #[serde(default)]
    pub align: String,
    /// Relative column width hint. Absent/zero/NaN → treated as 1.0.
    #[serde(default)]
    pub weight: f32,
}

/// The `GET /report/{id}` response — one completed run.
///
/// `columns` is the ordered list of row keys worth tabulating; `rows` may
/// carry extra keys beyond it (the engine's docstring calls this out), so
/// cells are looked up **by column name**, never by position. `notes` holds
/// derived anomalies and renders under the table. `column_meta` (#1760) is
/// per-column display metadata — see [`ReportColumnMeta`].
///
/// `totals` (#1763) is an optional grand-total row keyed by the same column
/// ids as `rows`, rendered as the table's pinned footer. Additive in both
/// directions: a daemon that predates it sends nothing (`None`, no footer,
/// today's rendering exactly), and a report that has no meaningful sum
/// (`issue-activity`, `drive-queue-status`) sends `null`.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub struct ReportResult {
    #[serde(default)]
    pub report_id: String,
    #[serde(default)]
    #[allow(dead_code)]
    pub generated_at: f64,
    /// `[start, end]` epoch seconds. Kept as a `Vec` rather than a tuple so a
    /// malformed/absent value degrades to "unknown window" instead of
    /// failing the whole deserialize.
    #[serde(default)]
    pub window: Vec<f64>,
    #[serde(default)]
    pub columns: Vec<String>,
    #[serde(default)]
    pub column_meta: Vec<ReportColumnMeta>,
    #[serde(default)]
    pub rows: Vec<serde_json::Value>,
    #[serde(default)]
    pub notes: Vec<String>,
    /// Grand-total row, or `None`. Kept as a raw `Value` for the same
    /// reason `rows` is: its keys are the report's own column ids, which
    /// this binary must not know.
    #[serde(default)]
    pub totals: Option<serde_json::Value>,
    /// #2271: optional declaration that this result also reads as a chart.
    /// `#[serde(default)]` is the whole forward-compatibility story in the
    /// other direction: a daemon that predates the field sends nothing and
    /// this stays `None`, so the panel renders the table exactly as before.
    #[serde(default)]
    pub chart: Option<ReportChart>,
}

/// #2271: a report's chart declaration, deserialised from the wire block
/// `coord/reports.py`'s `ChartSpec` emits.
///
/// It carries **no numbers** — every series names a `columns[]` id and the
/// renderer reads the same `rows` the table renders. That is what keeps the
/// table the fallback rendering and keeps CSV export (#1765) untouched.
///
/// Every field is `#[serde(default)]`: a daemon may add one, and a partial
/// block must degrade rather than fail the whole `ReportResult` deserialise
/// (which would take the table down with it — the exact "a report must never
/// become unreadable on an older coord-tui" failure this issue rules out).
#[derive(Debug, Clone, Default, serde::Deserialize)]
pub struct ReportChart {
    /// Open vocabulary — `bar` | `line` | `sparkline` today. A kind this
    /// binary predates degrades to the table; see `chart_support`.
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub series: Vec<ReportChartSeries>,
    /// Column id supplying each point's category/time label.
    #[serde(default)]
    pub x: Option<String>,
    /// Column id to pivot on: one output series per distinct value, x-axis
    /// deduped, `(group, x)` cells summed. `None` = one point per row.
    #[serde(default)]
    pub group_by: Option<String>,
    /// Bar only; ignored by every other kind.
    #[serde(default)]
    pub stacked: bool,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub y_label: String,
}

/// One declared series of a [`ReportChart`].
#[derive(Debug, Clone, Default, serde::Deserialize)]
pub struct ReportChartSeries {
    #[serde(default)]
    pub label: String,
    /// The `columns[]` id whose per-row value supplies the y-values.
    #[serde(default)]
    pub column: String,
    /// `"#rrggbb"` hint. Ignored under `group_by` — one declared colour
    /// cannot describe N groups, so the backend palette picks there.
    #[serde(default)]
    pub color: Option<String>,
}

/// #1853: identity of the column set a Reports result-table width override
/// was measured against — `(report_id, column ids)`.
///
/// Both halves are load-bearing. The report id alone would let one report's
/// override survive that report changing shape (a daemon adding a column);
/// the column ids alone would let two reports that happen to share column
/// names collide. Together they say exactly "these widths were dragged
/// against *this* table", which is the only condition under which replaying
/// them is honest. A column *count* is deliberately not part of the
/// comparison — it is implied by the id list, and on its own it would let a
/// five-column `usage` inherit a five-column `drive-queue-status`'s widths.
pub(crate) type ReportsColumnKey = (String, Vec<String>);

/// #1040: time-range filter for the Audit panel, cycled forward by the `t`
/// key (contract §8, `tests/acceptance/ms-33/contract.md`). Maps to the
/// `/audit` endpoint's `since` query param — there is deliberately no
/// `until` side, matching contract §8's param table.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub(crate) enum AuditTimeRange {
    /// Last 60 minutes.
    LastHour,
    /// Since the start of the current UTC day. (No timezone crate is
    /// available in this workspace — see `since()` below.)
    Today,
    /// Last 7 days.
    D7,
    /// No lower bound — the default (contract §8: "Default: All").
    #[default]
    All,
}

impl AuditTimeRange {
    /// Cycle forward: `LastHour -> Today -> D7 -> All -> LastHour`.
    pub(crate) fn next(self) -> Self {
        match self {
            AuditTimeRange::LastHour => AuditTimeRange::Today,
            AuditTimeRange::Today => AuditTimeRange::D7,
            AuditTimeRange::D7 => AuditTimeRange::All,
            AuditTimeRange::All => AuditTimeRange::LastHour,
        }
    }

    /// Exact display string pinned by contract §8 — shown in the sidebar
    /// and the `t=time-range (…)` status-bar hint.
    pub(crate) fn label(self) -> &'static str {
        match self {
            AuditTimeRange::LastHour => "Last hour",
            AuditTimeRange::Today => "Today",
            AuditTimeRange::D7 => "7d",
            AuditTimeRange::All => "All",
        }
    }

    /// The `/audit` `since=` lower bound (epoch seconds) for this range, or
    /// `None` for "All" (no param sent). `now` is epoch seconds, passed in
    /// by the caller (rather than read here via `SystemTime::now()`) so
    /// this stays a pure function callers can unit-test deterministically.
    ///
    /// "Today" uses the UTC calendar day boundary (`now` truncated to the
    /// nearest 86400s), not the operator's local timezone — this workspace
    /// has no timezone crate, and UTC-day is a reasonable, unambiguous
    /// approximation of contract §8's "start_of_today_epoch".
    pub(crate) fn since(self, now: f64) -> Option<f64> {
        match self {
            AuditTimeRange::LastHour => Some(now - 3_600.0),
            AuditTimeRange::Today => Some(now - now.rem_euclid(86_400.0)),
            AuditTimeRange::D7 => Some(now - 604_800.0),
            AuditTimeRange::All => None,
        }
    }
}

/// #1040: category filter for the Audit panel, cycled forward by `Tab`
/// (contract §9). Maps to the `/audit` endpoint's `category` query param.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub(crate) enum AuditCategory {
    /// No category filter applied — the default (contract §9).
    #[default]
    All,
    Dispatch,
    Test,
    Review,
    Merge,
    Override,
    Plan,
    Error,
}

impl AuditCategory {
    /// Cycle forward through the exact enum-value order pinned by contract
    /// §9: `all -> dispatch -> test -> review -> merge -> override -> plan
    /// -> error -> all`.
    pub(crate) fn next(self) -> Self {
        match self {
            AuditCategory::All => AuditCategory::Dispatch,
            AuditCategory::Dispatch => AuditCategory::Test,
            AuditCategory::Test => AuditCategory::Review,
            AuditCategory::Review => AuditCategory::Merge,
            AuditCategory::Merge => AuditCategory::Override,
            AuditCategory::Override => AuditCategory::Plan,
            AuditCategory::Plan => AuditCategory::Error,
            AuditCategory::Error => AuditCategory::All,
        }
    }

    /// Exact lowercase string (contract §9) — both the display label (shown
    /// in the sidebar / status-bar hint) and the `/audit` `category=` query
    /// value for every variant except `All`.
    pub(crate) fn label(self) -> &'static str {
        match self {
            AuditCategory::All => "all",
            AuditCategory::Dispatch => "dispatch",
            AuditCategory::Test => "test",
            AuditCategory::Review => "review",
            AuditCategory::Merge => "merge",
            AuditCategory::Override => "override",
            AuditCategory::Plan => "plan",
            AuditCategory::Error => "error",
        }
    }

    /// The `/audit` `category=` query value, or `None` for `All` (contract
    /// §9: "no category filter applied").
    pub(crate) fn query_value(self) -> Option<&'static str> {
        match self {
            AuditCategory::All => None,
            other => Some(other.label()),
        }
    }
}

/// CI check status for one PR.
///
/// #1344: previously fetched client-side via a perpetual `gh pr checks`
/// background poll loop (sustained high CPU across the fleet — one `gh`
/// subprocess loop per running `coord-tui`). Now synced straight from the
/// `/board` payload's `merge_plan[].ci_summary` (see
/// [`PlannedMergeCiSummary`]), which the board daemon computes once per tick
/// from the tick-refreshed gate snapshot (#1336, `coord/gate_snapshot.py`) —
/// zero client-side `gh` calls. Stored on `CoordApp` keyed by
/// `(repo_github, pr_number)`. Drives the "Checks: 2✓ 1✗" line under the
/// Merge stage in the Pipeline detail tab and the "Checks failed" status bar
/// hint when Merge is actionable.
#[derive(Clone, Debug)]
pub(crate) struct CiCheckSummary {
    pub(crate) passed: usize,
    pub(crate) failed: usize,
    pub(crate) running: usize,
    /// Names of failed checks (for the status-bar hint and detail row).
    pub(crate) failed_names: Vec<String>,
    /// URL of the first failed check — populated for future surfacing in the
    /// Merge Queue panel CI detail view (#738: previously shown in the retired
    /// per-issue Merge stage rows, now awaiting a Phase-3 panel detail row).
    #[allow(dead_code)]
    pub(crate) first_failed_url: Option<String>,
}

impl CiCheckSummary {
    pub(crate) fn has_failures(&self) -> bool {
        self.failed > 0
    }

    /// One-line summary like `2✓ 1✗ 1⋯`. Empty string when no checks at all
    /// (caller can suppress the row in that case).
    pub(crate) fn terse(&self) -> String {
        let mut parts: Vec<String> = Vec::new();
        if self.passed > 0 {
            parts.push(format!("{}✓", self.passed));
        }
        if self.failed > 0 {
            parts.push(format!("{}✗", self.failed));
        }
        if self.running > 0 {
            parts.push(format!("{}⋯", self.running));
        }
        parts.join(" ")
    }
}

#[derive(Clone, serde::Deserialize)]
pub(crate) struct Proposal {
    pub(crate) id: i64,
    #[serde(rename = "machine_name")]
    pub(crate) machine: String,
    #[serde(rename = "repo_name")]
    pub(crate) repo: String,
    pub(crate) issue_number: u64,
    pub(crate) issue_title: String,
    pub(crate) rationale: String,
    #[serde(rename = "type")]
    pub(crate) proposal_type: String,
}

/// One GitHub issue tracked by the pipeline panel.
///
/// Sourced from a background `gh search issues label:<L> --state all` poll
/// and matched back to a coord-local repo name via `pipeline_repos` in
/// `board_meta`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PipelineIssue {
    /// Issue number within the GitHub repo.
    pub(crate) number: u64,
    /// Issue title (as returned by gh).
    pub(crate) title: String,
    /// Issue body text (as returned by gh). Empty string when absent.
    pub(crate) body: String,
    /// `owner/name` slug of the GitHub repo the issue lives in.
    pub(crate) repo_slug: String,
    /// Coord-local repo name (matched via `pipeline_repos` map). `None` when
    /// the issue is in a repo not declared in coordinator.yml — such issues
    /// are still listed but cannot be dispatched.
    pub(crate) coord_repo: Option<String>,
    /// Tracked labels that flagged this issue (subset of `all_labels`).
    pub(crate) matched_labels: Vec<String>,
    /// All GitHub labels on this issue (not filtered by tracked labels).
    /// Used to compute lifecycle sections (status:refining, status:ready, …).
    pub(crate) all_labels: Vec<String>,
    /// True when the issue is closed on GitHub (`state == "closed"`).
    pub(crate) is_closed: bool,
    /// #2497: true when `body` is a wire preview, not the full text — mirrors
    /// `OpenIssue::body_truncated` (this struct is built from `OpenIssue` in
    /// `pipeline_issues_from_cache`). Closed (non-epic) issues get their body
    /// dropped to 0 chars by `board_wire.bound_issue_row`; the Issue tab uses
    /// this to arm a `GET /issue/{repo}/{number}` hydration fetch.
    pub(crate) body_truncated: bool,
    /// #2497: original body length before truncation, present only when
    /// `body_truncated` is true. Mirrors `OpenIssue::body_len`.
    pub(crate) body_len: Option<i64>,
}

#[derive(Default)]
/// A single issue freshly fetched via `gh issue view` for the Board Issue tab
/// when no row exists in the local `issues` table. Mirrors [`OpenIssue`] but
/// produced on-demand rather than from a sync.
#[derive(Clone, Debug)]
pub(crate) struct FetchedIssue {
    pub(crate) number: u64,
    pub(crate) title: String,
    pub(crate) body: String,
    pub(crate) labels: Vec<String>,
    /// "open" | "closed".  Carried so the DB upsert mirrors what `coord sync`
    /// would have written.
    pub(crate) state: String,
    /// #406: GitHub milestone number, if any.
    pub(crate) milestone_number: Option<i64>,
    /// #406: GitHub milestone title, if any.
    pub(crate) milestone_title: Option<String>,
}

/// #271 part 2 follow-up: cached `gh pr view` snapshot for a Pipeline
/// PR.  Surfaces the worker's PR description (which typically explains
/// what they did, including any new sample apps / demo binaries / entry
/// points) plus the list of files touched and the latest review's
/// verdict + body, so the user testing the branch has in-TUI context
/// instead of having to ask Claude or click out to GitHub.
#[derive(Clone, Debug)]
pub(crate) struct FetchedPr {
    pub(crate) title: String,
    /// Markdown body of the PR.  May be empty when the worker / merge
    /// command didn't write one.
    pub(crate) body: String,
    /// File paths touched by the PR.  Sorted by gh's default ordering.
    pub(crate) files: Vec<String>,
    /// Reviews posted on this PR, in the order gh returns them
    /// (chronological).  The latest one is what the user typically
    /// cares about (was it an approve or request-changes?).  When the
    /// adversarial reviewer posted via `coord notify`, the body is the
    /// full review text the reviewer wrote.
    pub(crate) reviews: Vec<FetchedReview>,
}

/// One row from `gh pr view --json reviews`.  `state` is gh's verbatim
/// status string: `"APPROVED"`, `"CHANGES_REQUESTED"`, `"COMMENTED"`,
/// or `"PENDING"`.
#[derive(Clone, Debug)]
pub(crate) struct FetchedReview {
    /// gh status string (uppercase).
    pub(crate) state: String,
    /// Markdown review body.  Empty when the reviewer left only a
    /// status change with no comment.
    pub(crate) body: String,
}

/// #248: parsed `<!-- coord:review ... -->` header.  The coordinator
/// prepends this to every review body so the TUI can surface the
/// verdict + counts without ingesting the prose.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct CoordReviewHeader {
    pub(crate) verdict: Option<String>,
    pub(crate) blocking: Option<u32>,
    pub(crate) nonblocking: Option<u32>,
    pub(crate) nits: Option<u32>,
    pub(crate) reviewer: Option<String>,
    pub(crate) assignment: Option<String>,
}

/// #558: One session entry shown in the Pipeline Summary tab.
/// Represents a single work/review/fix/plan session from GitHub comments.
/// #876: Used only in unit tests (the live Summary tab now sources from the
/// board layer via `build_board_summary_list_view`); the struct and its
/// helpers are compiled exclusively in test mode.
#[cfg(test)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct SessionSummary {
    /// The coord assignment id (from the `assignment=` marker token), or empty.
    pub(crate) assignment_id: String,
    /// Session type: "work", "review", "fix", "plan", "re-review", etc.
    /// Derived from `assignment_type` in the local DB when available, otherwise
    /// inferred from the comment event (completion → "work", review → "review").
    pub(crate) session_type: String,
    /// Machine name from the comment marker (`machine=` or `reviewer=` token).
    pub(crate) machine: String,
    /// Terminal status: "done", "failed", "advisory".
    pub(crate) status: String,
    /// Verdict for review sessions: "approve" | "request-changes".  None for
    /// non-review sessions.
    pub(crate) verdict: Option<String>,
    /// One-line prose summary extracted from the `### Summary` block (completion
    /// comments) or the first non-empty line of the REVIEW_BODY block (review
    /// comments).  May be empty when the worker didn't emit a summary.
    pub(crate) summary_text: String,
    /// Unix timestamp of the comment creation, used for newest-first sorting.
    /// Zero when the comment carries no timestamp.
    pub(crate) created_at_ts: f64,
}

/// An open issue from the local `issues` table (synced from GitHub on coord plan).
#[derive(Clone, serde::Deserialize)]
pub(crate) struct OpenIssue {
    pub(crate) repo_name: String,
    pub(crate) number: u64,
    pub(crate) title: String,
    /// Issue body, synced from GitHub via `coord sync`.  Empty string when
    /// the issue has no description.
    #[serde(default)]
    pub(crate) body: String,
    /// GitHub labels on this issue. Used by the Board Issue tab to render the
    /// same context the Pipeline Issue tab shows.
    #[serde(default)]
    pub(crate) labels: Vec<String>,
    /// "open" | "closed".  We load both into `data.open_issues` so the Board
    /// Issue tab can display bodies for closed issues (e.g. in the Completed
    /// group), but only "open" entries get injected as Pending rows.
    pub(crate) state: String,
    /// #406: GitHub milestone number.  `None` for issues without a milestone.
    #[serde(default)]
    pub(crate) milestone_number: Option<i64>,
    /// #406: GitHub milestone title (e.g. "v0.5").  `None` when no milestone.
    #[serde(default)]
    pub(crate) milestone_title: Option<String>,
    /// #2497: true when the `/board` wire bounded `body` — set for a closed
    /// (non-epic) issue, whose body `board_wire.bound_issue_row` drops to 0
    /// chars (#1791). `#[serde(default)]` so an older daemon (pre-#2497,
    /// never stamps this) deserializes as `false` — the pre-existing
    /// behavior of trusting `body` verbatim. Absent for open/epic issues,
    /// whose bodies the wire never truncates to 0.
    #[serde(default)]
    pub(crate) body_truncated: bool,
    /// #2497: the issue's full body length before truncation. `Some` only
    /// when `body_truncated` is true; drives the Issue tab's hydration gate
    /// (`CoordApp::issue_body_fetch_target`).
    #[serde(default)]
    pub(crate) body_len: Option<i64>,
}

/// #803: models config snapshot read from `board_meta['pipeline_models']`.
/// Mirrors `coord.config.ModelsConfig` and `pipeline.escalate_fix_model`.
/// Used to compute the escalated model tier for the interactive `--fix-of`
/// path without requiring the TUI to parse `coordinator.yml` itself.
#[derive(Clone, Debug, serde::Deserialize)]
pub(crate) struct PipelineModels {
    /// Alias used when no model is specified (e.g. `"sonnet"`).
    #[serde(default = "pipeline_models_default_tier")]
    pub(crate) default: String,
    /// Ordered list of model aliases (low → high).  Mirrors
    /// `models.escalation` in coordinator.yml.
    #[serde(default = "pipeline_models_default_escalation")]
    pub(crate) escalation: Vec<String>,
    /// When `false`, no escalation happens and every fix iteration uses the
    /// default model.  Mirrors `pipeline.escalate_fix_model`.
    #[serde(default = "pipeline_models_default_escalate")]
    pub(crate) escalate_fix_model: bool,
}

pub(crate) fn pipeline_models_default_tier() -> String { "sonnet".to_string() }
pub(crate) fn pipeline_models_default_escalation() -> Vec<String> {
    vec!["haiku".to_string(), "sonnet".to_string(), "opus".to_string()]
}
pub(crate) fn pipeline_models_default_escalate() -> bool { true }

impl Default for PipelineModels {
    fn default() -> Self {
        Self {
            default: pipeline_models_default_tier(),
            escalation: pipeline_models_default_escalation(),
            escalate_fix_model: pipeline_models_default_escalate(),
        }
    }
}

/// #803: pure function — mirrors Python's `_fix_model_for_iteration`.
///
/// Returns the model alias to use for a fix on the given iteration number
/// (1-based: iteration 1 = first fix, iteration 2 = second fix, …).
///
/// - `None` when `escalate_fix_model` is false — escalation is disabled, so
///   no model hint is surfaced (the fix will use coord's normal default).
/// - Iteration 1 → `Some(models.default)` (first fix stays cheap/fast).
/// - Iteration 2+ → climb one rung up `models.escalation` per extra
///   iteration, capped at the ladder top.
pub(crate) fn fix_model_for_iteration(models: &PipelineModels, iteration: i64) -> Option<String> {
    if !models.escalate_fix_model {
        return None;
    }
    let mut model = models.default.clone();
    let extra = (iteration.max(1) - 1) as usize;
    for _ in 0..extra {
        let pos = models.escalation.iter().position(|m| m == &model);
        match pos {
            Some(idx) if idx + 1 < models.escalation.len() => {
                model = models.escalation[idx + 1].clone();
            }
            _ => break, // already at the top or not on the ladder → cap
        }
    }
    Some(model)
}

#[derive(Default)]
// #1042: `pub`, not `pub(crate)` — see the note on `Assignment` above; this
// is `make_test_app`'s parameter type. Fields stay `pub(crate)`; external
// callers only ever need `BoardData::default()` (derived, no field access
// required) to build a fixture.
pub struct BoardData {
    pub(crate) local_machine: String,
    pub(crate) assignments: Vec<Assignment>,
    /// Open issues from the local SQLite `issues` table — the full backlog.
    pub(crate) open_issues: Vec<OpenIssue>,
    pub(crate) machines: Vec<Machine>,
    pub(crate) merge_queue: Vec<MergeQueueEntry>,
    /// Server-side merge plan (#776) — ranked list from `/board`.  Non-empty
    /// when the daemon is v0.4.53+ and has planned merges.  The MergeQueue
    /// panel renders this in preference to `merge_queue` when non-empty.
    pub(crate) merge_plan: Vec<PlannedMergeEntry>,
    pub(crate) proposals: Vec<Proposal>,
    /// Pipeline gate names from `pipeline.default_gates` in coordinator.yml.
    /// Defaults to `["review", "merge"]` when the board_meta key is absent.
    pub(crate) pipeline_default_gates: Vec<String>,
    /// GitHub issue labels considered "in the pipeline". Defaults to
    /// `["coord"]` when the board_meta key is absent.
    pub(crate) pipeline_tracked_labels: Vec<String>,
    /// Coord-local repo name → GitHub `owner/repo` slug (and inverse).
    /// Empty when no config snapshot has been written yet.
    pub(crate) pipeline_repos: Vec<(String, String)>,
    /// #296: coord-local repo name → `run_cmd` shell command from
    /// `coordinator.yml`.  Only repos that define `run_cmd` are present.
    /// Surfaced in the Test stage detail panel as the "Run" row.
    pub(crate) pipeline_repo_run_cmds: std::collections::HashMap<String, String>,
    /// #349: coord-local repo name → absolute local checkout path on this
    /// machine.  Populated by `_save_config_snapshot` from the hostname-
    /// matched `repo_paths` in `coordinator.yml` and stored in
    /// `board_meta['pipeline_repo_paths']`.  Used by the TUI to read git
    /// branch HEAD SHAs for test-plan staleness detection.
    pub(crate) pipeline_repo_paths: std::collections::HashMap<String, String>,
    /// #1151: coord-local repo name → list of `acceptance.drivers.<repo>
    /// .routes[*].match` globs (#1125 in-repo path routing), for repos whose
    /// acceptance driver is *routed*. Absent (no key) for an unrouted repo,
    /// or when the daemon predates this field. Populated by
    /// `_save_config_snapshot` and stored in
    /// `board_meta['pipeline_acceptance_routes']`. Used by
    /// `dispatch_gate_a_mock_for_selected_pipeline_row` /
    /// `dispatch_acceptance_author_for_selected_pipeline_row` /
    /// `dispatch_acceptance_record_for_selected_pipeline_row` to decide
    /// whether a `--for-path` is needed before firing `coord acceptance
    /// mock/author/record` — those CLI commands 500 with "no route matched"
    /// when the repo is routed and `--for-path` is omitted, which the TUI
    /// used to do unconditionally, every time.
    pub(crate) pipeline_acceptance_routes: std::collections::HashMap<String, Vec<String>>,
    /// Mirror of `dispatch.require_plan` from coordinator.yml.  When true,
    /// the pipeline prepends a Plan stage before Work, and Work [Go]
    /// becomes "approve the plan and dispatch work" rather than fresh
    /// dispatch.  Defaults to `false`.
    pub(crate) pipeline_require_plan: bool,
    /// Cached structured plans keyed by plan-assignment-id.  Populated by
    /// `coord notify` parsing the worker log into the `plans` table; the
    /// TUI just loads the JSON blob and renders it in the Plan stage
    /// content panel.
    pub(crate) plans: std::collections::HashMap<String, PlanData>,
    /// #778: approved/done work not yet in the merge queue.  Computed
    /// locally in `load_data` or received from the `/board` JSON payload.
    pub(crate) merge_staging: Vec<StagingEntry>,
    /// #803: model config snapshot for the interactive `--fix-of` escalation.
    /// `None` when `board_meta['pipeline_models']` has not been written yet
    /// (pre-#803 coordinator versions).  Falls back to the struct's `Default`
    /// implementation (sonnet default, [haiku,sonnet,opus] ladder, escalation
    /// enabled) which matches the coordinator.yml defaults.
    pub(crate) pipeline_models: Option<PipelineModels>,
    /// #550: server-computed per-issue stage/gate projection from `/board`.
    /// Empty on the local-SQLite-mode read path (no daemon to compute it) and
    /// on daemons older than #550 — `pipeline.rs`'s stage functions fall back
    /// to local computation in both cases.
    pub(crate) issue_stage_projection: Vec<IssueStageProjection>,
    /// #795 Phase 3b: per-milestone work-order rank + ready frontier from
    /// `/board`.  Empty on the local-SQLite-mode read path and daemons older
    /// than #795.  The Pipeline view renders rank/next-up/blocked-on badges
    /// on milestone cards using this projection.
    pub(crate) milestone_work_orders: Vec<MilestoneWorkOrder>,
    /// #1195/#1197: per-epic child-issue lists from `/board`'s `children`
    /// field.  Empty on the local-SQLite-mode read path and daemons older
    /// than #1195.  The Pipeline view nests each epic's children beneath its
    /// row using this projection (`compute_epic_nesting`, `pipeline.rs`).
    pub(crate) epic_children: Vec<EpicChildren>,
    /// #975: milestone plan-roster — one entry per milestone/epic, sourced
    /// from `/board`'s `plan_roster` field.  Empty on the local-SQLite-mode
    /// read path and daemons older than #975.  The Plans panel renders one
    /// row per entry.
    pub(crate) plan_roster: Vec<PlanRosterEntry>,
    /// #976: mirrors `BoardPayload::plan_roster_supported` — `true` only
    /// when the connected daemon actually computes `plan_roster` (any
    /// #975+ daemon, whether or not it found milestones this tick). `false`
    /// on the local-SQLite-mode read path (no daemon at all) and on daemons
    /// older than #975 (no such field on the wire). The Plans panel uses
    /// this — not `plan_roster.is_empty()` — to decide whether an empty
    /// roster means "genuinely no plans" or "not receiving plan data."
    pub(crate) plan_roster_supported: bool,
    /// #978: mirrors `BoardPayload::goal_header` — the pinned GOAL.md
    /// north-star header for the Plans panel. `available: false` (the
    /// type's `Default`) on the local-SQLite-mode read path (no daemon to
    /// read GOAL.md from) and on daemons older than #978.
    pub(crate) goal_header: GoalHeader,
    /// #1037/#1039: mirrors `BoardPayload::audit_recent_count` — count of
    /// audit rows written in the last 15 minutes, driving the Audit panel's
    /// sidebar "N recent" badge. `0` on the local-SQLite-mode read path and
    /// on daemons older than #1037.
    pub(crate) audit_recent_count: u64,
    /// #1505: mirrors `BoardPayload::escalations` — board-visible driver-
    /// escalation records. Empty on the local-SQLite-mode read path and on
    /// daemons older than #1505.
    pub(crate) escalations: Vec<EscalationEntry>,
    /// #1631 (H-4): mirrors `BoardPayload::fleet_health` — see that field's
    /// doc comment. Consumed by `fleet_health.rs`'s status-bar segment and
    /// detail overlay; never re-derives severity from raw values.
    pub(crate) fleet_health: FleetHealthBlock,
    /// #1755 (DQ-3): mirrors `BoardPayload::drive_queue` — the drive queue in
    /// run order. Consumed by `drive_queue.rs`'s status-bar segment, detail
    /// overlay and Pipeline-row menu items. Empty on the local-SQLite-mode
    /// read path and on daemons older than #1753.
    pub(crate) drive_queue: Vec<BoardDriveQueueEntry>,
    /// #2608: mirrors `BoardPayload::roll_pending` — see `RollPending`'s doc
    /// comment. `None` on the local-SQLite-mode read path (no daemon to read
    /// the marker file from) and on daemons older than #2608.
    pub(crate) roll_pending: Option<RollPending>,
    /// #2532: mirrors `BoardPayload::approved_submissions` — portal
    /// submissions ready for decomposition. Empty on the local-SQLite-mode
    /// read path (no daemon to compute the server-resolved `repos`) and on
    /// daemons older than #2532.
    pub(crate) approved_submissions: Vec<ApprovedSubmission>,
}

/// Parsed plan data, mirroring `coord.plan_parser.WorkerPlan.to_dict()`.
/// Only the fields we render are pulled out; everything else stays in
/// the original JSON blob (we don't roundtrip it back to disk).
#[derive(Clone, Debug, Default)]
pub(crate) struct PlanData {
    pub(crate) plan: String,
    pub(crate) files_modify: Vec<String>,
    pub(crate) approach: String,
    pub(crate) risks: String,
    pub(crate) estimate: String,
    /// Tri-state: None = no SMOKE_TESTS block emitted (legacy / plan
    /// worker forgot); `Some(empty)` = "(none — change is internal)";
    /// `Some(non-empty)` = bullets.
    pub(crate) smoke_tests: Option<Vec<String>>,
}
