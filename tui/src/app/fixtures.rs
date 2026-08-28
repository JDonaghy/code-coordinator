//! Test-support fixtures: build a [`CoordApp`] from in-memory `BoardData`,
//! no live daemon required.
//!
//! Always compiled for in-crate `#[cfg(test)]` unit/TuiDriver tests (the
//! in-crate tests in `app/tests.rs` pull these in via `use super::fixtures::*;`).
//! Also compiled — and made reachable outside the crate — when the
//! `test-support` feature is enabled, so an external integration-test crate
//! (`tui/tests/acceptance.rs`) can build a `CoordApp` exactly like the
//! in-crate tests do (#1042, oracle-loop Gate-A prerequisite: docs/ORACLE_LOOP.md).
//!
//! The feature stays off for a normal `cargo build`/`cargo test`, so none of
//! this is part of the crate's default public surface.

use super::*;

// Re-exported so `use coord_tui::fixtures::{make_test_app, BoardData};` is
// enough — callers don't also need `coord_tui::app::types::BoardData`
// (which isn't reachable outside the crate; `app` and `app::types` stay
// private/`pub(crate)`, only these specific names are re-exported).
pub use super::types::{Assignment, BoardData};

// #1087: re-exported so `coord_tui::fixtures::set_test_board_service` pairs
// with `MockBoardService` below for an external integration-test crate,
// exactly like the other names in this module. Goes through `super::data`
// (the defining module) rather than `super::set_test_board_service` — the
// latter only resolves via `app/mod.rs`'s private `use self::data::*;`,
// which itself isn't re-exportable (same reason `Assignment`/`BoardData`
// above are re-exported via `super::types::` and not bare `super::`).
pub use super::data::{set_test_board_service, TestBoardServiceGuard};

/// Build a bare [`CoordApp`] from the given [`BoardData`] — no real `coord`
/// subprocess is ever spawned (`CommandRunner::new_for_test()`), no live
/// daemon, no I/O. This is the seam every other fixture in this module (and
/// every in-crate `#[cfg(test)]` test) builds on.
pub fn make_test_app(data: BoardData) -> CoordApp {
    let mut sidebar = SidebarSystem::new(Vec::new());
    sidebar.set_navigation_mode(NavigationMode::Selection);
    sidebar.set_allow_collapse(true);
    let mut pipeline_sidebar = SidebarSystem::new(Vec::new());
    pipeline_sidebar.set_navigation_mode(NavigationMode::Selection);
    pipeline_sidebar.set_allow_collapse(true);
    let (inject_fallback_tx, inject_fallback_rx) = std::sync::mpsc::channel();
    // #1326: derive straight from the fixture's `data` — never touch the
    // real `~/.coord/workspace.json` from a test fixture (mirrors
    // `command_runner: CommandRunner::new_for_test()` below: no real I/O).
    let workspace = Workspace::derive_from_repos(&known_repos(&data));
    // #2286 (ms-65 §6): unlike `workspace` above, this DOES read
    // `~/.coord/tabs.json` — deliberately, and unlike every other field in
    // this constructor. `CoordApp::new()` is the only other doc-tabs load
    // site, and it builds the real, daemon-backed app against an empty
    // board (data arrives asynchronously), which structurally can never
    // witness a restore: every persisted document would be pruned before
    // the first real tick landed. This fixture path is therefore the ONLY
    // seam that can restore doc tabs against data that's actually known at
    // construction time (`tests/acceptance/ms-65/manifest.yml` finding 14
    // spells this out — flagged there as a coordinator-owned follow-up: an
    // injectable `~/.coord` seam would let this go back to being pure, like
    // `workspace` above). Pruned immediately against `data`, so a fixture
    // whose synthetic issues don't match the real file's (the overwhelming
    // common case) restores nothing.
    let mut doc_tabs = DocTabs::load();
    doc_tabs.retain_known(&doc_tabs::known_doc_keys(&data));
    CoordApp {
        data,
        workspace,
        active_view: SidebarView::default(),
        board_sidebar: sidebar,
        board_repo_names: Vec::new(),
        board_issues_cache: Vec::new(),
        has_proposals_section: false,
        machine_sel: 0,
        machine_scroll: 0,
        refreshed_at: Instant::now(),
        detail_scroll: 0,
        machine_detail_scroll: 0,
        // Use new_for_test() so no real `coord` subprocess is ever spawned
        // when tests exercise dispatch paths (merge-queue key-bindings,
        // etc.).  Calls are recorded in spawned_calls; a zero-exit success
        // is resolved on the channel immediately so poll() returns quickly.
        command_runner: crate::commands::CommandRunner::new_for_test(),
        last_notify: Instant::now(),
        issue_sync_last: None,
        board_search: SidebarFilter::default(),
        board_milestone_expanded: std::collections::HashMap::new(),
        board_epic_expanded: std::collections::HashMap::new(),
        board_epic_row_keys: std::collections::HashMap::new(),
        doc_tabs,
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
        pending_data: None,
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
        // #2028: never call the real `read_paused_machines()` from a test
        // fixture — it reads the LOCAL `~/.coord/paused_machines.json`
        // unconditionally (mirrors `workspace` above: no real I/O). Before
        // this, any host whose real pause file was non-empty (e.g. two
        // long-destroyed ephemeral workers left paused on dellserver since
        // 2026-08-04) failed `poll_paused_machines_replaces_snapshot_when_ready`
        // and `..._reverts_an_operator_pause_..` (`app/tests.rs`) purely from
        // ambient machine state — both tests assert `paused_machines.is_empty()`
        // immediately after construction. Empty here matches what every
        // caller of this fixture already expects; a test that wants a
        // pre-populated set assigns `app.paused_machines` directly (see the
        // tests further down this file).
        paused_machines: std::collections::HashSet::new(),
        quiet_paused_machines: std::collections::HashSet::new(),
        cordoned_machines: std::collections::HashSet::new(),
        quiet_hours_windows: std::collections::HashMap::new(),
        pending_paused_machines: None,
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
        local_coord_version: None,
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
        settings: TuiSettings::default(),
        parsed_keybindings: parse_keybindings(&TuiSettings::default()),
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
        // #424
        terminal_session: None,
        terminal_focused: false,
        terminal_pending_dims: std::cell::Cell::new(None),
        terminal_spawn_error: None,
        // #1029: no queued programmatic panel switch / Terminal
        // return-view bookmark on startup.
        pending_panel_switch: None,
        pending_switch_is_programmatic: false,
        terminal_return_view: None,
        // #440
        detail_terminal_sessions: std::collections::HashMap::new(),
        detail_terminal_spawn_errors: std::collections::HashMap::new(),
        detail_terminal_focused: false,
        ctrl_w_pending: false,
        focused_region: FocusedRegion::default(),
        detail_terminal_pending_dims: std::cell::Cell::new(None),
        // #454
        pty_pressed_buttons: 0,
        // #464
        terminal_host_sel_dragging: false,
        // #790
        terminal_copy_mode: false,
        // #207
        machine_metrics: std::collections::HashMap::new(),
        pending_metrics: Vec::new(),
        metrics_last_polled: Instant::now(),
        // #487
        live_tmux_sessions: Vec::new(),
        pending_remote_sessions: None,
        // #953
        fleet_terminals: Vec::new(),
        pending_remote_terminals: None,
        terminal_tree_expanded: std::collections::HashMap::new(),
        terminal_tree_selected: None,
        terminal_tree_scroll: 0,
        // #955
        fleet_terminal_sessions: std::collections::HashMap::new(),
        fleet_terminal_spawn_errors: std::collections::HashMap::new(),
        pending_kill_terminal: None,
        // #1398
        drive_sessions: Vec::new(),
        pending_drive_sessions: None,
        pending_kill_drive: None,
        // #1032
        sessions_tree_expanded: std::collections::HashMap::new(),
        sessions_tree_selected: None,
        sessions_tree_scroll: 0,
        // #1033
        pending_kill_session: None,
        fix_briefing_preview: None,
        fix_briefing_rx: None,
        // Leg 2 (#517)
        armed_for_auto_review: std::collections::HashMap::new(),
        pending_auto_review: None,
        pending_stage_launch: None,
        // #685: per-issue test-mode policy choice dialog.
        pending_test_mode_choice: None,
        offered_smoke_for_headless_work: std::collections::HashSet::new(),
        // Leg 3 (#517)
        armed_for_verdict: std::collections::HashMap::new(),
        pending_rework: None,
        rework_bypass: false,
        // #541
        issue_finder: None,
        // #2642
        doc_tab_picker: None,
        // Leg 3c / A3 (#517, #581)
        armed_for_test_verdict: std::collections::HashMap::new(),
        pending_test_fix: None,
        pending_merge: None,
        // #863
        pending_fix_cap_preflight: None,
        pending_fix_force_confirm: None,
        // #638
        kanban_model: BoardModel {
            id: WidgetId::new("kanban:coord"),
            columns: Vec::new(),
            selected_card_id: None,
            col_scroll_offset: 0,
        },
        kanban_layout: std::cell::RefCell::new(None),
        // #737
        merge_queue_sel: 0,
        merge_queue_scroll: 0,
        // #771
        milestone_dag_sel: 0,
        // #1124: same registration `CoordApp::new()` does — fixtures must
        // not skip it, or a test driving the Plans `?`/`/` surfaces would
        // find an empty registry regardless of what it exercises.
        help_registry: {
            let mut registry = HelpRegistry::new();
            registry.register("panel:plans", CoordApp::plans_view_help());
            // #2287 (ms-65 §8b): same registration `CoordApp::new()` does —
            // fixtures must not skip it, or a test driving the Board `?`
            // overlay would find an empty registry regardless of what it
            // exercises.
            registry.register("panel:board", CoordApp::board_view_help());
            registry
        },
        help_overlay: HelpOverlayController::new(),
        command_palette: None,
        // #975
        plans_sel: 0,
        // #1001
        plans_expanded_repos: std::collections::HashSet::new(),
        // #1122
        plans_detail_open: false,
        // #1122 fix-iteration-1
        plans_detail_scroll: 0,
        plans_detail_sel: 0,
        plans_detail_target: None,
        // #1121
        plans_tree_expanded: std::collections::HashMap::new(),
        plans_tree_selected: None,
        plans_tree_scroll: 0,
        // #1039: Audit panel — nothing seeded by default; use
        // `make_app_with_audit_json` to pre-populate `audit_page`.
        audit_page: None,
        audit_fetch_rx: None,
        audit_last_fetched: None,
        audit_sel: 0,
        audit_detail_open: false,
        audit_fetch_error: None,
        audit_no_service: false,
        // #1040: no time/category filter applied by default in test
        // helpers; individual tests override these fields directly to
        // exercise the filters.
        audit_time_range: AuditTimeRange::All,
        audit_category: AuditCategory::All,
        // #2653: tier defaults to `Business` here too, matching
        // `CoordApp::new` — tests that want the unfiltered/operational view
        // override this field directly, same pattern as the two above.
        audit_tier: AuditTier::default(),
        audit_type_filter: SidebarFilter::default(),
        // #1094: no column-width overrides / active resize drag / cached
        // layout in test helpers by default.
        audit_column_overrides: vec![None; 5],
        audit_table_layout: std::cell::RefCell::new(None),
        audit_resize_col: None,
        audit_scroll: 0,
        audit_h_scroll: 0.0,
        audit_scrollbar_drag: None,
        // #1741: Reports panel — nothing seeded by default; use
        // `make_app_with_reports` to pre-populate the catalogue (and
        // optionally a completed run) with no daemon and no fetch thread.
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
        // #1853: no dragged column widths and no active resize drag by
        // default. `None` rather than `vec![None; N]` (Audit's shape) —
        // there is no N until a result lands, which is the whole reason
        // these are keyed.
        reports_column_overrides: None,
        reports_resize_col: None,
        reports_vscroll_drag: false,
        reports_pending_export: None,
        reports_export_status: None,
        reports_export_rx: None,
        // #217: use the default dark palette for test helpers.
        active_theme: crate::settings::Theme::Dark.to_quadraui_theme(),
        // #2405: completed-issues grid defaults (24h / all repos).
        completed_grid: CompletedGrid::default(),
        completed_form_layout: std::cell::RefCell::new(None),
        completed_table_layout: std::cell::RefCell::new(None),
        // #816: no pending PTY-panic dialog in test helpers.
        pty_panic_dialog: None,
        // #1059: no pending Gate A dispatch-failure dialog in test helpers.
        gate_a_error_dialog: None,
        // #2863: no pending decomposition-dispatch failure in test helpers.
        decompose_chat_error_dialog: None,
    }
}

/// Build a [`CoordApp`] seeded with `assignments` and the board sidebar
/// already rebuilt from them (so selection/navigation tests can drive it
/// immediately).
pub fn make_app_with_assignments(assignments: Vec<Assignment>) -> CoordApp {
    let mut app = make_test_app(BoardData {
        assignments,
        ..BoardData::default()
    });
    app.rebuild_board_sidebar();
    app
}

/// #2281 data-model seam: build a [`CoordApp`] whose entire board — open
/// issues, assignments, machines, pipeline_repos, pipeline_default_gates,
/// plan_roster, etc. — is seeded from a raw JSON object shaped exactly like
/// the daemon's `GET /board` response body (the same wire contract
/// [`super::types::BoardPayload`] deserializes on the real
/// `load_data_remote` path), rather than the empty `BoardData::default()`
/// every other fixture in this module starts from.
///
/// `BoardData`'s fields (and `OpenIssue`) are `pub(crate)` — see the note on
/// `BoardData` itself: "external callers only ever need
/// `BoardData::default()`". That left an external `--test acceptance` crate
/// able to build exactly one board: an empty one — no tree rows in the
/// Board sidebar, nothing feeding the Pipeline tracked-issue list. This is
/// the seam that removes that ceiling (#2281), and the prerequisite for
/// every ms-65 slice (clicking Board/Pipeline sidebar rows).
///
/// Deliberately takes JSON rather than a `BoardData`/`OpenIssue`/`Machine` —
/// same reasoning [`make_app_with_drive_queue`]'s doc comment already
/// records: those types are `pub(crate)`, so a `pub fn` accepting one
/// wouldn't compile (E0446), and going through the wire shape means a
/// fixture that drifts from the daemon's real payload fails to parse here
/// instead of rendering a plausible lie.
///
/// Skips the machine reachability/health TCP probes the real load path runs
/// in `assemble_board_data` (`tcp_probe` / `spawn_machine_health` dialing
/// `machines[*].host:7433`) — a fixture must stay pure/offline like every
/// other helper in this module, so seeded machines always come back
/// `reachable: false`, `version: None`, `worktree_bytes: 0`. `active_count`
/// is still derived from the seeded `assignments` (a pure local
/// computation, same as the real path).
///
/// Also rebuilds both sidebars and the Pipeline tracked-issue list
/// (`pipeline_issues_from_cache`) before returning, mirroring the exact
/// sequence the real data-apply tick runs (`rebuild_board_sidebar` →
/// `pipeline_issues_from_cache` → `rebuild_pipeline_sidebar`) — so a caller
/// gets a board that already renders, not one that needs to know which
/// private rebuild methods to invoke itself.
///
/// Malformed JSON is a silent no-op — `make_test_app(BoardData::default())`,
/// the empty-board render — matching every other JSON seam in this module
/// (`make_app_with_audit_json`, `make_app_with_drive_queue`,
/// `make_app_with_reports`). Callers assert on the resulting screen, not on
/// this function's return.
pub fn make_app_with_board_json(board_json: &str) -> CoordApp {
    let data = match serde_json::from_str::<BoardPayload>(board_json) {
        Ok(payload) => board_data_from_payload(payload),
        Err(_) => BoardData::default(),
    };
    let mut app = make_test_app(data);
    app.rebuild_board_sidebar();
    app.pipeline_issues = app.pipeline_issues_from_cache();
    app.rebuild_pipeline_sidebar(None);
    app
}

/// Shared conversion behind [`make_app_with_board_json`]: turn a decoded
/// [`BoardPayload`] into [`BoardData`] without the real load path's network
/// probes (see that function's doc comment for why). Mirrors
/// `load_data_remote` (`app/data.rs`) field-for-field, minus
/// `assemble_board_data`'s probing tail — and minus the merge-queue
/// milestone-title join `assemble_board_data` also runs (`app/data.rs:1796-1820`),
/// which no acceptance criterion for this seam needs and which a caller can
/// still exercise directly on `app.data.open_issues` / `app.data.merge_queue`
/// if a future test needs it. Concretely: a test that seeds `merge_queue`
/// entries carrying an `issue_number` and then asserts on a rendered
/// milestone title will silently see an empty string through this seam,
/// unlike the real load path — seed `open_issues`/`plan_roster` directly
/// with the milestone title already attached instead.
fn board_data_from_payload(payload: BoardPayload) -> BoardData {
    let mut assignments = payload.assignments;
    // Same ordering `load_data_remote` applies: running, then failed, then
    // done (most recent first within each group) — so a fixture-built board
    // sorts identically to a real one instead of surprising a test that
    // asserts on row order.
    assignments.sort_by(|a, b| {
        let rank = |s: &str| match s {
            "running" => 0u8,
            "failed" => 1,
            "done" => 2,
            _ => 3,
        };
        rank(&a.status).cmp(&rank(&b.status)).then_with(|| {
            b.dispatched_at
                .partial_cmp(&a.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        })
    });

    let machines: Vec<Machine> = payload
        .machines
        .into_iter()
        .map(|m| {
            let active_count = assignments
                .iter()
                .filter(|a| a.machine == m.name && a.status == "running")
                .count();
            Machine {
                name: m.name,
                host: m.host,
                reachable: false,
                active_count,
                repos: m.repos,
                version: None,
                worktree_bytes: 0,
            }
        })
        .collect();

    let plans: std::collections::HashMap<String, PlanData> = payload
        .plans
        .iter()
        .map(|(aid, v)| (aid.clone(), parse_plan_data(v)))
        .collect();

    let (
        pipeline_default_gates,
        pipeline_tracked_labels,
        pipeline_repos,
        pipeline_require_plan,
        pipeline_repo_run_cmds,
        pipeline_repo_paths,
        pipeline_models,
        pipeline_acceptance_routes,
    ) = parse_pipeline_meta_from_map(&payload.board_meta);

    let merge_staging = if payload.merge_staging.is_empty() {
        compute_staging_local(&assignments, &payload.merge_queue, &pipeline_default_gates)
    } else {
        payload.merge_staging
    };

    BoardData {
        // No wire equivalent worth deriving here: the real path matches the
        // OS hostname against `machines[*].host`, which is meaningless for a
        // fixture that never "runs" on any of the seeded machines. Leave it
        // empty, same as `BoardData::default()`.
        local_machine: String::new(),
        assignments,
        open_issues: payload.issues,
        machines,
        merge_queue: payload.merge_queue,
        merge_plan: payload.merge_plan,
        proposals: payload.proposals,
        pipeline_default_gates,
        pipeline_tracked_labels,
        pipeline_repos,
        pipeline_require_plan,
        pipeline_repo_run_cmds,
        pipeline_repo_paths,
        pipeline_acceptance_routes,
        plans,
        merge_staging,
        pipeline_models,
        issue_stage_projection: payload.issue_stage_projection,
        milestone_work_orders: payload.milestone_work_orders,
        epic_children: payload.epic_children,
        plan_roster: payload.plan_roster,
        plan_roster_supported: payload.plan_roster_supported,
        goal_header: payload.goal_header,
        audit_recent_count: payload.audit_recent_count,
        escalations: payload.escalations,
        fleet_health: payload.fleet_health,
        drive_queue: payload.drive_queue,
        roll_pending: payload.roll_pending,
        approved_submissions: payload.approved_submissions,
        // #2895: a fixture built from a board payload is by definition a
        // successful load.
        load_error: None,
    }
}

/// #2281 (ms-38 follow-on) data-model seam: build a [`CoordApp`] with the
/// Plans panel's `plan_roster` pre-seeded from a raw JSON array shaped
/// exactly like `/board`'s own `plan_roster` field (`Vec<PlanRosterEntry>`)
/// — no live daemon, no `make_app_with_board_json`'s full board-payload
/// shape required when a test only cares about plan-roster rows.
///
/// Falls out of the same [`BoardPayload`]/`PlanRosterEntry` deserialization
/// [`make_app_with_board_json`] uses, mirroring the existing
/// [`make_app_with_audit_json`] shape exactly (`BoardData` + one JSON
/// blob) — this is the seam ms-38's `plans_detail_1122` /
/// `plans_rightclick_1123` slices were blocked on (`tests/acceptance/ms-38/
/// manifest.yml`'s HARNESS BLOCKER comment names this exact function).
///
/// Sets `plan_roster_supported = true` on success — matching what a real
/// daemon that emits `plan_roster` at all always sends — so the Plans panel
/// renders the seeded rows rather than its "not connected to a daemon that
/// computes this" placeholder. Malformed JSON is a silent no-op (both
/// fields stay whatever `data` implied) rather than a panic — callers
/// should assert on the resulting screen, not on this function's return.
pub fn make_app_with_plan_roster_json(data: BoardData, plan_roster_json: &str) -> CoordApp {
    let mut app = make_test_app(data);
    if let Ok(roster) = serde_json::from_str::<Vec<super::types::PlanRosterEntry>>(plan_roster_json)
    {
        app.data.plan_roster = roster;
        app.data.plan_roster_supported = true;
    }
    app
}

/// #1039 data-model seam: build a [`CoordApp`] with the Audit panel's cache
/// (`audit_page`) pre-seeded from a raw JSON string shaped exactly like the
/// `GET /audit` response body (contract §6, `tests/acceptance/ms-33/
/// contract.md`) — no live daemon, no background fetch thread.
///
/// This is the seam a later JIT extension of the sealed acceptance suite
/// needs for the populated-list / entry-detail / count+badge assertions
/// that `tests/acceptance/ms-33/audit_1039.rs` deliberately deferred (its
/// TODO block names this exact helper shape). Malformed JSON is a silent
/// no-op (`audit_page` stays whatever `data` implied, i.e. `None`) rather
/// than a panic — callers that care should assert on the resulting screen,
/// not on this function's return.
pub fn make_app_with_audit_json(data: BoardData, audit_json: &str) -> CoordApp {
    let mut app = make_test_app(data);
    if let Ok(page) = serde_json::from_str::<super::types::AuditPage>(audit_json) {
        app.audit_page = Some(page);
    }
    app
}

/// #1866 (Q-1) data-model seam: build a [`CoordApp`] whose `/board` payload
/// carries a drive queue, from a raw JSON array shaped exactly like
/// `/board`'s own `drive_queue` key (OpenAPI `BoardDriveQueueEntry`, i.e. a
/// raw `drive_queue` table dump — note `after_json`, not `after`).
///
/// Deliberately takes **JSON rather than a `Vec<BoardDriveQueueEntry>`**:
/// that type is `pub(crate)`, so a `pub fn` accepting it would not compile
/// (E0446), and going through the wire shape means a fixture that drifts
/// from the daemon's payload fails here instead of rendering a plausible
/// lie. Same posture as [`make_app_with_audit_json`].
///
/// The Queue panel carries no fetch of its own — `/board` already ships this
/// data and the existing poll refreshes it — so unlike
/// [`make_app_with_reports`] there is nothing here to mark as "already
/// fetched". That very poll is instead disarmed here (`refresh_cadence:
/// Off`): the usual fixture is `BoardData::default()` + only a drive queue,
/// which leaves `apply_pending_data`'s #620 degraded-tick guard inert (it
/// keys on machines/issues/assignments — all empty), so a periodic refresh
/// firing mid-test would wholesale-replace `self.data` with the test stub's
/// empty payload and wipe the seeded queue. On a loaded machine a driver
/// test's dispatch loop can exceed the default 5 s cadence in wall-clock
/// time, which made queue tests flaky under a full parallel suite run.
///
/// Malformed JSON is a silent no-op (the panel then renders its own empty
/// state) rather than a panic — assert on the resulting screen, not on this
/// function's return.
pub fn make_app_with_drive_queue(data: BoardData, drive_queue_json: &str) -> CoordApp {
    let mut app = make_test_app(data);
    app.settings.refresh_cadence = crate::settings::RefreshCadence::Off;
    if let Ok(entries) =
        serde_json::from_str::<Vec<super::types::BoardDriveQueueEntry>>(drive_queue_json)
    {
        app.data.drive_queue = entries;
    }
    app
}

/// #1741 data-model seam: build a [`CoordApp`] with the Reports panel's
/// catalogue (and optionally one completed run) pre-seeded from raw JSON
/// strings shaped exactly like `GET /report` / `GET /report/{id}` (#1742) —
/// no live daemon, no background fetch thread.
///
/// `catalogue_json` is the `{"reports": [...]}` envelope; `result_json` is a
/// `ReportResult` body, or `None` for "no run yet". Sections are seeded
/// expanded, matching what the real catalogue-fetch drain does
/// (`reports_seed_expansion` in `settings_ui.rs`), so a test renders the
/// same first frame the operator sees. `reports_catalogue_fetched` is set so
/// a driver test that ticks `run_periodic_work` never arms a real fetch on
/// top of the seeded data.
///
/// Malformed JSON is a silent no-op (the panel then renders its own
/// empty/loading state) rather than a panic — assert on the resulting
/// screen, not on this function's return.
pub fn make_app_with_reports(
    data: BoardData,
    catalogue_json: &str,
    result_json: Option<&str>,
) -> CoordApp {
    let mut app = make_test_app(data);
    if let Ok(cat) = serde_json::from_str::<super::types::ReportCatalogue>(catalogue_json) {
        app.reports_catalogue = Some(cat.reports);
        app.reports_catalogue_fetched = true;
        app.reports_seed_expansion();
    }
    if let Some(json) = result_json {
        if let Ok(result) = serde_json::from_str::<super::types::ReportResult>(json) {
            app.reports_result = Some(result);
        }
    }
    app
}

/// #1853 test seam: the sending half of a fake in-flight report run, handed
/// back by [`make_app_with_reports_awaiting_run`] so a test can decide *when*
/// the next result lands.
///
/// Exists because `TuiDriver::app_mut` yields the opaque shell adapter, not
/// the `CoordApp` inside it — a driver test therefore has no way to swap
/// `reports_result` mid-session by poking state. Delivering through the
/// app's own run channel is better than poking anyway: it is the exact path
/// a real completed run takes (`run_periodic_work` drains it on the next
/// event), so what the test exercises is the real report switch, not a
/// hand-assembled imitation of one.
pub struct PendingReportRun {
    tx: std::sync::mpsc::Sender<super::data::ReportRunOutcome>,
}

impl PendingReportRun {
    /// Deliver `result_json` as the run's outcome. It is picked up by the
    /// next `run_periodic_work` tick — i.e. by the next event the driver
    /// dispatches while the Reports panel is the active view.
    ///
    /// Returns `false` if `result_json` doesn't parse or the app has been
    /// dropped, so a typo in a test fixture fails at the `assert!` rather
    /// than as a mystifying "the table never changed".
    pub fn deliver(&self, result_json: &str) -> bool {
        match serde_json::from_str::<super::types::ReportResult>(result_json) {
            Ok(result) => self
                .tx
                .send(super::data::ReportRunOutcome::Result(Box::new(result)))
                .is_ok(),
            Err(_) => false,
        }
    }
}

/// #1853 data-model seam: [`make_app_with_reports`] plus an *open run
/// channel*, so a driver test can replace the report on screen part-way
/// through a session — the event a column-width override must not survive.
///
/// The app looks exactly as it does with a run in flight, minus the thread:
/// `reports_run_rx` is live and empty, and stays that way until the returned
/// [`PendingReportRun::deliver`] is called.
pub fn make_app_with_reports_awaiting_run(
    data: BoardData,
    catalogue_json: &str,
    result_json: &str,
) -> (CoordApp, PendingReportRun) {
    let mut app = make_app_with_reports(data, catalogue_json, Some(result_json));
    let (tx, rx) = std::sync::mpsc::channel();
    app.reports_run_rx = Some(rx);
    (app, PendingReportRun { tx })
}

/// #1087: a minimal, in-process, `std`-only mock HTTP server for exercising
/// `spawn_audit_fetch` / `resolve_board_service`'s real network path
/// end-to-end (paired with [`super::set_test_board_service`]).
///
/// Binds to an OS-assigned free port on `127.0.0.1` and, for the lifetime of
/// the returned value, answers *every* accepted connection with the same
/// canned response — `200 OK` / `application/json` / the `body` passed to
/// [`MockBoardService::start`] — regardless of method or path. It loops
/// rather than serving a single request so an incidental extra connection
/// (e.g. from an unrelated Audit-panel test on another thread that also
/// nudges `spawn_audit_fetch`, harmless because of the thread-local scoping
/// described on `set_test_board_service`, but still a live TCP client that
/// might dial in) can never "steal" the response meant for the caller's own
/// request.
///
/// No request parsing beyond draining the socket — `spawn_audit_fetch`'s
/// `GET /audit[?since=...&category=...&type=...]` carries no body, so a
/// full HTTP parser isn't needed to answer it correctly.
pub struct MockBoardService {
    addr: std::net::SocketAddr,
    shutdown: std::sync::Arc<std::sync::atomic::AtomicBool>,
    handle: Option<std::thread::JoinHandle<()>>,
    /// Request line (e.g. `"GET /audit?since=123 HTTP/1.1"`) of every
    /// request handled so far, oldest first — lets a test assert the real
    /// path/query `spawn_audit_fetch` sent, not just the parsed response.
    requests: std::sync::Arc<std::sync::Mutex<Vec<String>>>,
}

impl MockBoardService {
    /// Start the server. `body` is served verbatim as the JSON response
    /// body for every request received while this value is alive.
    pub fn start(body: impl Into<String>) -> Self {
        let listener = std::net::TcpListener::bind("127.0.0.1:0")
            .expect("MockBoardService: failed to bind 127.0.0.1:0");
        let addr = listener
            .local_addr()
            .expect("MockBoardService: failed to read local_addr");
        // Non-blocking + a short poll interval so the accept loop notices
        // `shutdown` promptly instead of blocking in `accept()` forever.
        listener
            .set_nonblocking(true)
            .expect("MockBoardService: failed to set_nonblocking");
        let shutdown = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let requests = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let body = body.into();
        let handle = {
            let shutdown = shutdown.clone();
            let requests = requests.clone();
            std::thread::spawn(move || {
                while !shutdown.load(std::sync::atomic::Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((stream, _)) => Self::respond(stream, &body, &requests),
                        Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                            std::thread::sleep(std::time::Duration::from_millis(5));
                        }
                        // Any other accept error (e.g. the listener got torn
                        // down) — stop looping rather than spin.
                        Err(_) => break,
                    }
                }
            })
        };
        Self {
            addr,
            shutdown,
            handle: Some(handle),
            requests,
        }
    }

    /// `http://127.0.0.1:<port>` — pass to [`super::set_test_board_service`]
    /// so `resolve_board_service()` (and thus `spawn_audit_fetch`) points
    /// here for the duration of the returned guard.
    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    /// Request lines received so far, oldest first (e.g.
    /// `"GET /audit?since=123 HTTP/1.1"`). Poisoned-lock recovery falls back
    /// to an empty list rather than propagating a second panic over a
    /// first — a test asserting on an empty `requests()` after some other
    /// assertion already panicked would only obscure the real failure.
    pub fn requests(&self) -> Vec<String> {
        self.requests
            .lock()
            .map(|g| g.clone())
            .unwrap_or_default()
    }

    fn respond(
        mut stream: std::net::TcpStream,
        body: &str,
        requests: &std::sync::Mutex<Vec<String>>,
    ) {
        use std::io::{BufRead, BufReader, Write};
        let _ = stream.set_read_timeout(Some(std::time::Duration::from_millis(500)));
        // Only the request line matters for the mock's purposes (method +
        // path + query); headers/body (if any) are left undrained on the
        // socket, which is fine since we respond and close immediately.
        let mut reader = BufReader::new(stream.try_clone().expect("clone stream for read"));
        let mut request_line = String::new();
        if reader.read_line(&mut request_line).is_ok() {
            let trimmed = request_line.trim_end().to_string();
            if !trimmed.is_empty() {
                if let Ok(mut log) = requests.lock() {
                    log.push(trimmed);
                }
            }
        }
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body,
        );
        let _ = stream.write_all(response.as_bytes());
        let _ = stream.flush();
    }
}

impl Drop for MockBoardService {
    fn drop(&mut self) {
        self.shutdown.store(true, std::sync::atomic::Ordering::Relaxed);
        // Wake the accept loop immediately (rather than waiting up to the
        // 5ms poll interval) by dialing in once ourselves — keeps teardown
        // prompt and deterministic instead of racing the sleep.
        let _ = std::net::TcpStream::connect(self.addr);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

/// #265 helper: build an app where issue #10 is closed (on GitHub) and has a
/// done assignment, so it lands in the Completed group.
pub fn make_app_with_one_completed_issue() -> CoordApp {
    let mut app = make_app_with_assignments(vec![make_assignment_typed(
        "done",
        10,
        "repo-a",
        Some("work"),
    )]);
    app.data.open_issues.push(OpenIssue {
        repo_name: "repo-a".to_string(),
        number: 10,
        title: "closed one".to_string(),
        body: String::new(),
        state: "closed".to_string(),
        labels: Vec::new(),
        milestone_number: None,
        milestone_title: None,
        body_truncated: false,
        body_len: None,
        // #1941: fields added to the generated wire DTO; not exercised by this test.
        synced_at: None,
    });
    app.rebuild_board_sidebar();
    app
}

/// Build an [`Assignment`] with the handful of fields most tests care about
/// (`status`, `issue_number`, `repo`, `assignment_type`) and sensible
/// defaults for everything else.
pub fn make_assignment_typed(
    status: &str,
    issue: u64,
    repo: &str,
    atype: Option<&str>,
) -> Assignment {
    Assignment {
        id: format!("id-{}-{}", issue, status),
        repo: repo.to_string(),
        issue_number: issue,
        issue_title: format!("Issue {}", issue),
        machine: "testmachine".to_string(),
        status: status.to_string(),
        branch: None,
        model: None,
        dispatched_at: Some(1_000_000.0 + issue as f64),
        finished_at: None,
        exit_code: None,
        assignment_type: atype.map(|s| s.to_string()),
        test_state: None,
        review_verdict: None,
        review_of_assignment_id: None,
        cost_usd: None,
        smoke_tests: None,
        review_findings: None,
        review_findings_truncated: false,
        review_findings_len: None,
        test_plan: None,
        test_plan_branch_head: None,
        input_tokens: 0,
        output_tokens: 0,
        cache_creation_tokens: 0,
        cache_read_tokens: 0,
        is_interactive: false,
        failure_reason: None,
        review_iteration: 0,
        acceptance_state: None,
        acceptance_reason: None,
        acceptance_sha: None,
        acceptance_total: None,
        acceptance_passed: None,
        test_reason: None,
        review_state: None,
        pr_url: None,
        audit_goals_json: None,
        audit_bottom_line: None,
        audit_run_number: None,
        for_issue_number: None,
        driven_by: None,
        dispatched_by_assignment_id: None,
        // #1941: fields added to the generated wire DTO; not exercised by this test.
        repo_github: None,
        files_allowed: None,
        files_forbidden: None,
        smoke_test: None,
        smoke_test_reason: None,
        review_target: None,
        required_gates: None,
        plan: None,
        unreachable_count: None,
        review_posted_at: None,
        uat_state: None,
        uat_reason: None,
        claude_session_id: None,
        provider_name: None,
        review_head_sha: None,
        completion_summary: None,
        review_verdict_original: None,
        review_verdict_override_reason: None,
        review_patch_id: None,
        test_head_sha: None,
        test_patch_id: None,
        test_base_sha: None,
        review_scoped: None,
        review_scope_base_sha: None,
        test_toolchain: None,
        verdict_source: None,
        verdict_source_reason: None,
        stop_reason: None,
        num_turns: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// #2281 seam smoke test.
    ///
    /// Proves the gap the issue describes is actually closed: before this seam,
    /// `BoardData`'s fields (and `OpenIssue`) were `pub(crate)`, so this crate
    /// could build only an empty board (`BoardData::default()`) — no tree rows
    /// in the Board sidebar, nothing feeding the Pipeline tracked-issue list.
    /// `make_app_with_board_json` seeds three open issues across two repos from
    /// a raw JSON object shaped like the daemon's `/board` payload, and both
    /// panels must render real rows from it.
    ///
    /// Relocated from `tui/tests/acceptance.rs` (#2281 review): that file is
    /// this repo's `tui-tuidriver` acceptance-driver `entrypoint:` in
    /// `coordinator.yml`, which makes the *whole file* an exact-match sealed
    /// oracle path (`coord/config.py` `AcceptanceConfig.sealed_paths()`) — any
    /// `type="work"` diff touching it, even additively, is a mandatory
    /// request-changes. CLAUDE.md's TuiDriver section already asks for this
    /// placement: "Put the tests in-crate (`#[cfg(test)]`), not in
    /// `tui/tests/`".
    #[test]
    fn make_app_with_board_json_renders_seeded_issues_in_board_and_pipeline() {
        use quadraui::tui::testing::driver_with_shell;

        const BOARD_JSON: &str = r#"{
          "issues": [
            {"repo_name": "repo-a", "number": 2266, "title": "Alpha issue", "state": "open", "labels": ["coord"]},
            {"repo_name": "repo-a", "number": 2267, "title": "Beta issue", "state": "open", "labels": ["coord"]},
            {"repo_name": "repo-b", "number": 2268, "title": "Gamma issue", "state": "open", "labels": ["coord"]}
          ],
          "board_meta": {
            "pipeline_repos": "{\"repo-a\": \"acme/repo-a\", \"repo-b\": \"acme/repo-b\"}",
            "pipeline_default_gates": "[\"review\", \"merge\"]"
          }
        }"#;

        let app = make_app_with_board_json(BOARD_JSON);
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);

        // SidebarView::Board is the app's default active view, so the seeded
        // repo groups are already on screen — but each repo's "No milestone"
        // sub-group starts collapsed (no in-flight assignment to force it open,
        // #857's default), so the issue rows themselves need one click each to
        // reveal. Target by the group's issue count ("(2)" / "(1)") since both
        // repos otherwise render an identical "No milestone" header.
        let screen_before = driver.screen();
        let (x, y) = driver.find("No milestone (2)").unwrap_or_else(|| {
            panic!("repo-a's collapsed \"No milestone\" group header must be on screen:\n{screen_before}")
        });
        driver.click(x, y);
        driver.render();
        assert!(
            driver.screen_contains("#2266"),
            "Board sidebar must render a row for seeded issue #2266 — screen:\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains("#2267"),
            "Board sidebar must render a row for seeded issue #2267 — screen:\n{}",
            driver.screen(),
        );

        let screen_before = driver.screen();
        let (x, y) = driver.find("No milestone (1)").unwrap_or_else(|| {
            panic!("repo-b's collapsed \"No milestone\" group header must be on screen:\n{screen_before}")
        });
        driver.click(x, y);
        driver.render();
        assert!(
            driver.screen_contains("#2268"),
            "Board sidebar must render a row for seeded issue #2268 — screen:\n{}",
            driver.screen(),
        );

        // Switch to the Pipeline panel (activity-bar icon "▶") and confirm the
        // same seed feeds its tracked-issue list too.
        let screen_before = driver.screen();
        let (x, y) = driver
            .find("▶")
            .unwrap_or_else(|| panic!("activity bar must render the Pipeline icon:\n{screen_before}"));
        driver.click(x, y);
        driver.render();
        assert!(
            driver.screen_contains(" PIPELINE "),
            "clicking the Pipeline icon must activate SidebarView::Pipeline — screen:\n{}",
            driver.screen(),
        );
        // All three seeded issues carry the default-tracked "coord" label and no
        // assignment yet, so they land in a single "New (3)" lifecycle section —
        // proof `pipeline_issues_from_cache` picked up all three from the seed.
        assert!(
            driver.screen_contains("New (3)"),
            "Pipeline tracked-issue list must show all 3 seeded issues under New — screen:\n{}",
            driver.screen(),
        );
        // Drill in one more level (same collapsed-by-default "No milestone"
        // sub-group as the Board sidebar) to confirm an actual issue row, not
        // just the count, renders from the seed.
        let screen_before = driver.screen();
        let (x, y) = driver.find("No milestone (2)").unwrap_or_else(|| {
            panic!("repo-a's collapsed \"No milestone\" group header must be on screen:\n{screen_before}")
        });
        driver.click(x, y);
        driver.render();
        assert!(
            driver.screen_contains("#2266"),
            "Pipeline tracked-issue list must render seeded issue #2266 — screen:\n{}",
            driver.screen(),
        );
    }

    /// #2281: malformed JSON must not panic — it degrades to the same
    /// empty-board render every other JSON seam in `app::fixtures` falls back
    /// to (`make_app_with_audit_json`, `make_app_with_drive_queue`, ...).
    #[test]
    fn make_app_with_board_json_malformed_json_is_a_silent_no_op() {
        let app = make_app_with_board_json("not valid json");
        let _ = app;
    }

    /// #2281 review follow-up: `make_app_with_plan_roster_json` shipped as
    /// new public `test-support` surface with zero coverage — it flips
    /// `plan_roster_supported` on success, which is easy to get backwards, so
    /// this pins both the flag and an actual rendered row from the seed.
    /// Mirrors `make_app_with_board_json_renders_seeded_issues_in_board_and_pipeline`
    /// above and `plans_panel_lists_plans_from_board_data` (`app/tests.rs`)
    /// for the Plans-panel click path.
    #[test]
    fn make_app_with_plan_roster_json_renders_seeded_roster_entry() {
        use quadraui::tui::testing::driver_with_shell;

        const ROSTER_JSON: &str = r#"[
          {
            "repo": "repo-a",
            "title": "Substrate",
            "milestone_number": 7,
            "has_work_order": true,
            "ready_frontier": 1,
            "blocked": 0,
            "in_flight": 0,
            "done": 1,
            "total": 2
          }
        ]"#;

        let app = make_app_with_plan_roster_json(BoardData::default(), ROSTER_JSON);
        assert!(
            app.data.plan_roster_supported,
            "successful parse must flip plan_roster_supported so the Plans panel \
             renders the seeded rows instead of its \"not connected\" placeholder"
        );

        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
        let screen_before = driver.screen();
        let (x, y) = driver
            .find("◆")
            .unwrap_or_else(|| panic!("activity bar must render the Plans icon:\n{screen_before}"));
        driver.click(x, y);
        driver.render();
        assert!(
            driver.screen_contains("PLANS"),
            "clicking the Plans icon must activate SidebarView::Plans — screen:\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains("Substrate"),
            "Plans panel must render the seeded roster entry's milestone title — screen:\n{}",
            driver.screen(),
        );
    }

    /// #2281: malformed JSON must not panic and must leave `plan_roster`
    /// (and `plan_roster_supported`) exactly as `data` implied, same as
    /// every other JSON seam in this module.
    #[test]
    fn make_app_with_plan_roster_json_malformed_json_is_a_silent_no_op() {
        let app = make_app_with_plan_roster_json(BoardData::default(), "not valid json");
        assert!(
            !app.data.plan_roster_supported,
            "malformed JSON must not flip plan_roster_supported"
        );
        assert!(app.data.plan_roster.is_empty());
    }
}
