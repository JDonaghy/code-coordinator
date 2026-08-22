//! Sidebar panel and action bar extracted from `app/mod.rs` (#744).
//!
//! **Import pattern:** `use super::*` is intentional — these methods live on `CoordApp`
//! and need the full parent namespace. See `sessions.rs` for the full rationale.
#[allow(unused_imports)]
use super::*;

// ─── Selected-item action bar (#270) ─────────────────────────────────────────

impl CoordApp {
    /// #272: build the `SidebarPanel` for the Board / Pipeline sidebar.
    ///
    /// Always emits a header toolbar — even when no row-specific verbs
    /// apply — so the layout reserves the slot and the tree below
    /// doesn't shift between selections.  Returns the panel as a
    /// value rather than a `&SidebarPanel` because the toolbar buttons
    /// are derived from current state (selected row's lifecycle).
    /// Build the sidebar header panel.  The per-row contextual action bar
    /// (#270 — a no-mouse mirror of the right-click menu) was removed once the
    /// keyboard context menu (Menu / Shift+F10 / '.') made it redundant; the
    /// only surviving header button is the Board "Sync" affordance.  Views
    /// with no header button (Pipeline / Machines) reserve no slot, so their
    /// tree starts at the very top of the sidebar.
    pub(crate) fn build_sidebar_action_panel(&self, lh: f32) -> SidebarPanel {
        let mut buttons: Vec<ToolbarButton> = Vec::new();
        // Board panel keeps a Sync button so the user can pull fresh issues
        // from GitHub on demand without needing a terminal ('S' does the same).
        if self.active_view == SidebarView::Board {
            buttons.push(ToolbarButton::Action {
                id: WidgetId::new("sidebar-action:sync-issues"),
                label: "Sync".to_string(),
                icon: Some("↻".to_string()),
                key_hint: Some("S".to_string()),
                enabled: true,
                is_active: false,
                tooltip: "coord sync — fetch all open issues from GitHub".to_string(),
            });
        }
        // #954 Gap A: the Terminal view's machine/terminal tree (#953) had
        // only the `n` keybinding to create a terminal — no visible
        // affordance for an operator who doesn't know the shortcut. Mirror
        // the Board "Sync" header button above with a "+ New terminal" one,
        // wired to the same `open_new_terminal_picker` entry point as `n`.
        if self.active_view == SidebarView::Terminal {
            buttons.push(ToolbarButton::Action {
                id: WidgetId::new("sidebar-action:new-terminal"),
                label: "New terminal".to_string(),
                icon: Some("+".to_string()),
                key_hint: Some("n".to_string()),
                enabled: true,
                is_active: false,
                tooltip: "Create a new terminal on a fleet machine".to_string(),
            });
        }
        if buttons.is_empty() {
            // No header button → don't reserve the slot; the tree gets the
            // full sidebar height.
            return SidebarPanel {
                id: WidgetId::new("sidebar-panel"),
                toolbar: None,
                toolbar_height: None,
            };
        }
        SidebarPanel {
            id: WidgetId::new("sidebar-panel"),
            toolbar: Some(Toolbar {
                focused_index: None,
                id: WidgetId::new("sidebar-action-bar"),
                buttons,
                bg: None,
            }),
            toolbar_height: Some(self.sidebar_action_bar_height(lh)),
        }
    }

    /// Height of the per-row action bar rendered above the sidebar tree.
    ///
    /// Two cells tall in TUI / two line-heights in GTK.  The quadraui
    /// rasteriser now paints multi-row toolbars (vertically centring
    /// the text) and `SidebarPanel` reserves the slot at the
    /// configured height even when the bar has no buttons — so this
    /// extra height is safe (no off-by-one click bug) and gives a
    /// proper button-row visual.
    pub(crate) fn sidebar_action_bar_height(&self, lh: f32) -> f32 {
        lh * 2.0
    }

    /// #pause: right-click menu for a Machines panel row.  One toggle
    /// item ("Pause routing" / "Resume routing") plus a separator so the
    /// menu reads as a "single verb" affordance rather than a junk drawer.
    ///
    /// #2147: `quiet_hours` is the machine's CURRENT effective window
    /// (`self.quiet_hours_windows.get(name)` at the caller), so the label
    /// can read the schedule back rather than always saying "Set…" — an
    /// operator adjusting an existing window shouldn't have to remember
    /// what it was. A "Clear quiet hours" item only appears when one is
    /// actually set (mirrors the Pause/Resume toggle's "only offer the
    /// verb that applies" shape). Both new items sit above the existing
    /// separator, same slot the Pause/Resume toggle already occupies.
    pub(crate) fn context_menu_items_for_machine_row(
        &self,
        _name: &str,
        is_paused: bool,
        quiet_hours: Option<&data::QuietHoursWindow>,
    ) -> Vec<ContextMenuItem> {
        let mut items: Vec<ContextMenuItem> = Vec::new();
        if is_paused {
            items.push(ContextMenuItem::action("machine-resume", "Resume routing"));
        } else {
            items.push(ContextMenuItem::action("machine-pause", "Pause routing"));
        }
        let quiet_label = match quiet_hours {
            Some(w) => format!("Quiet hours: {}-{}…", w.start, w.end),
            None => "Set quiet hours…".to_string(),
        };
        items.push(ContextMenuItem::action("machine-quiet-hours-set", &quiet_label));
        if quiet_hours.is_some() {
            items.push(ContextMenuItem::action(
                "machine-quiet-hours-clear",
                "Clear quiet hours",
            ));
        }
        items.push(ContextMenuItem::separator());
        items.push(ContextMenuItem::action("refresh", "Refresh").with_shortcut("r"));
        items
    }

    /// #956: right-click menu for a Terminal-view tree TERMINAL row. One
    /// verb — "Kill terminal" — matching the fleet-wide `K` = kill
    /// convention (also used by the #1033 Sessions panel); the menu item
    /// and the `K` keybinding both arm the same confirm dialog rather than
    /// killing directly (terminals are persistent and may hold live work).
    pub(crate) fn context_menu_items_for_terminal_row(&self) -> Vec<ContextMenuItem> {
        vec![ContextMenuItem::action("kill-terminal", "Kill terminal").with_shortcut("K")]
    }

    /// Hit-test a left-click against the sidebar action bar at the top
    /// of `sidebar_b`.  Returns `(shrunken_sidebar_b, consumed)` — same
    /// shape as `hit_test_panel_toolbar`.
    pub(crate) fn hit_test_sidebar_action_bar(
        &mut self,
        pos: Point,
        sidebar_b: Rect,
        lh: f32,
    ) -> (Rect, bool) {
        // #272: layout/hit-test goes through the SidebarPanel primitive
        // so click and paint can't drift apart.  `Content { .. }`
        // means the click landed below the toolbar slot (tree
        // territory) and the caller should forward to the tree.
        let panel = self.build_sidebar_action_panel(lh);
        let layout = panel.layout(
            sidebar_b,
            quadraui::SidebarPanelMeasure::new(lh, 8.0),
            toolbar_tui_measure,
        );
        let content_rect = layout.content_bounds;
        match layout.hit_test(pos.x, pos.y) {
            SidebarPanelHit::ToolbarButton(id) => {
                // The Board "Sync" and Terminal "New terminal" buttons are
                // the only header actions left — the #270 contextual verbs
                // now live in the keyboard context menu.
                match id.as_str().strip_prefix("sidebar-action:") {
                    Some("sync-issues") => self.force_issue_sync(),
                    // #954 Gap A: same entry point as the `n` keybinding.
                    Some("new-terminal") => self.open_new_terminal_picker(),
                    _ => {}
                }
                (content_rect, true)
            }
            // Click inside the toolbar slot but not on a clickable
            // button (gap, separator, or disabled) — swallow so it
            // doesn't fall through to the tree.
            SidebarPanelHit::ToolbarEmpty => (content_rect, true),
            // Below the toolbar slot — let the tree handle it.
            SidebarPanelHit::Content { .. } | SidebarPanelHit::Empty => (content_rect, false),
        }
    }
}

// ─── Per-panel toolbar (#249 Principle 1) ────────────────────────────────────

impl CoordApp {
    /// Height of the toolbar row at the top of the main panel.
    ///
    /// Two cells tall — matches `sidebar_action_bar_height` for visual
    /// consistency across the activity bar / sidebar / main panel
    /// chrome.  Safe to grow now that the quadraui rasteriser paints
    /// multi-row toolbars.
    pub(crate) fn toolbar_height(&self, lh: f32) -> f32 {
        lh * 2.0
    }

    /// Build the toolbar (a row of clickable verb buttons) for the current
    /// view, or `None` for views where no panel-level verbs apply.
    ///
    /// Backed by the quadraui [`Toolbar`] primitive — each button carries
    /// Returns `true` when any modal dialog that owns ALL keyboard input is
    /// currently active.
    ///
    /// Used to prevent the Ctrl+P issue-finder (and any future global
    /// shortcut) from opening on top of a modal that already holds focus.
    /// The design contract: one modal at a time — each modal "owns ALL input
    /// while open" (see handle() for each individual guard).
    pub(crate) fn any_blocking_modal_active(&self) -> bool {
        self.watch_focused.is_some()
            || self.pending_purge.is_some()
            || self.pending_force_merge.is_some()
            || self.pending_merge_all_ready.is_some()
            || self.pending_merge_revalidate.is_some()
            || self.pending_test_fail.is_some()
            || self.pending_report_fix.is_some()
            || self.pending_plan_capture.is_some()
            || self.pending_new_milestone_chat.is_some()
            || self.pending_milestone_row_input.is_some()
            || self.pending_close_plan.is_some()
            || self.pending_refinement_close_prompt.is_some()
            || self.pending_auto_review.is_some()
            || self.pending_rework.is_some()
            || self.artifact_pull_dialog.is_some()
            || self.pty_panic_dialog.is_some()
            || self.gate_a_error_dialog.is_some()
            || self.pending_machine_picker.is_some()
            || self.pending_new_terminal_picker.is_some()
            || self.pending_new_terminal.is_some()
            || self.pending_quiet_hours.is_some()
            || self.pending_repo_picker.is_some()
            || self.refinement_notes_modal.is_some()
            || self.pending_refinement_notes_synth.is_some()
            || self.file_issue_modal.is_some()
            || self.pending_restart.is_some()
            || self.pending_kill_terminal.is_some()
            || self.pending_kill_drive.is_some()
            || self.pending_kill_session.is_some()
            // #1124: Plans `?` help overlay / `/` command palette — both
            // own ALL input while open (see `events.rs`'s dispatch block).
            || self.help_overlay.is_open()
            || self.command_palette.is_some()
            // #1631: the fleet-health detail overlay owns ALL input while open.
            || self.fleet_health_overlay_open
            // #1755: the "Add to drive queue after…" prompt owns ALL input
            // while open (the drive-queue overlay itself was retired by
            // #1868 — the Queue panel is the one queue surface now).
            || self.pending_drive_queue_after.is_some()
            // #2500: the "What needs to change?" Gate-A note prompt owns
            // ALL input while open, same posture as the drive-queue one
            // just above.
            || self.pending_gate_a_changes_note.is_some()
    }

    /// an `action_id` of the form `"toolbar:<verb>"` resolved by
    /// [`dispatch_toolbar_action`].  Disabled buttons set
    /// [`ToolbarButton::Action::enabled = false`] so the primitive dims
    /// them and the hit-test declines clicks; the affordance stays
    /// visible without misleading the user.
    pub(crate) fn panel_toolbar(&self) -> Option<Toolbar> {
        // Toolbar suppressed while the watch overlay or any inline confirm
        // prompt has the keyboard — these modes consume every keystroke
        // and a clickable toolbar above them would be misleading.
        if self.watch_focused.is_some()
            || self.pending_purge.is_some()
            || self.pending_force_merge.is_some()
            || self.pending_merge_all_ready.is_some()
            || self.pending_merge_revalidate.is_some()
            || self.pending_test_fail.is_some()
            || self.pending_report_fix.is_some()
            || self.pending_plan_capture.is_some()
            || self.pending_new_milestone_chat.is_some()
            || self.pending_milestone_row_input.is_some()
            || self.pending_close_plan.is_some()
            || self.pending_refinement_close_prompt.is_some()
            || self.pending_auto_review.is_some()
            || self.pending_rework.is_some()
            || self.pending_test_fix.is_some()
            || self.pending_merge.is_some()
            || self.pending_fix_force_confirm.is_some()
            || self.artifact_pull_dialog.is_some()
            || self.pty_panic_dialog.is_some()
            || self.gate_a_error_dialog.is_some()
            || self.pending_new_terminal.is_some()
            || self.pending_quiet_hours.is_some()
        {
            return None;
        }

        // #192 / #263: toolbar revised — Plan and Approve dropped now
        // that Proposals is retired; Merge moves Pipeline-only.  Most
        // row-level actions live on the action bar (#270) or right-
        // click menu (#259-#262, #266); the panel toolbar is reserved
        // for genuine panel-wide ops.
        //
        // #438: Pipeline toolbar removed — every verb it offered is
        // covered by an existing keybind (N=notify, r=ready, m/M=merge,
        // R=retry) and the per-stage Go/Retry action lives in the
        // pipeline-action-bar just below the tab row.  Reclaims `2*lh`
        // of vertical space for the pipeline list.
        let buttons: Vec<ToolbarButton> = match self.active_view {
            SidebarView::Board => {
                vec![
                    toolbar_button("add", "[A]dd", !self.board_repo_names.is_empty()),
                    toolbar_button("notify", "[N]otify", true),
                    toolbar_button(
                        "retry",
                        "[R]etry",
                        self.board_selected_failed_assignment().is_some(),
                    ),
                    toolbar_button(
                        "purge",
                        "[P]urge",
                        self.board_selection_in_completed_group(),
                    ),
                ]
            }
            // Pipeline, Machines, Settings, Terminal, Kanban, MergeQueue, MilestoneDag,
            // Sessions, Audit: no panel-level toolbar.
            // Pipeline verbs are fully covered by keybinds; Terminal is a
            // pure pass-through pane (#424); Kanban uses the Board widget natively;
            // MergeQueue actions are surfaced in the status-bar hints (#737);
            // MilestoneDag's "Dispatch milestone" is a keybind + context menu (#771);
            // Sessions is read-only nav/select in this slice (#1032); Audit's
            // verbs (nav/detail/refresh) are all keybinds, surfaced in the
            // status-bar hints (#1039).
            SidebarView::Pipeline
            | SidebarView::Machines
            | SidebarView::Settings
            | SidebarView::Terminal
            | SidebarView::Kanban
            | SidebarView::MergeQueue
            | SidebarView::MilestoneDag
            | SidebarView::Plans
            | SidebarView::Sessions
            | SidebarView::Audit
            // #1741: Reports' only verb is Run, and it lives inside each
            // section's own form (collapsing a section must hide it — the
            // stack itself moved into the sidebar in #1911, but that verb
            // stayed put) — a panel-level toolbar button would be a second,
            // always-visible trigger with no section to bind it to.
            | SidebarView::Reports
            // #1866: Queue's verbs are all row-scoped (J/K reorder, x/u/r,
            // right-click menu) — a panel-level toolbar button would have no
            // row to bind to, exactly as for Reports above.
            | SidebarView::Queue
            // #2532 (ms-67 contract §3a): Approved work items' one verb
            // (#2533's "Pull into decomposition session") is row-scoped
            // (right-click menu) — no panel-wide toolbar button to bind,
            // same reasoning as Queue immediately above.
            | SidebarView::Approved => return None,
        };

        Some(Toolbar {
            focused_index: None,
            id: WidgetId::new("panel-toolbar"),
            buttons,
            bg: None,
        })
    }

    /// Hit-test a left-click against the panel toolbar at the top of
    /// the main content rect.  Returns `(shrunken_main_b, consumed)`:
    /// `consumed=true` means the click landed on a toolbar segment and
    /// the action was dispatched; the caller should NOT continue routing
    /// the click to the panel body.  `shrunken_main_b` is `main_b` with
    /// the toolbar row carved off the top, ready for downstream tab-bar
    /// hit-tests (whose math expects `pos.y - main_b.y < tab_h`).
    pub(crate) fn hit_test_panel_toolbar(&mut self, pos: Point, main_b: Rect, lh: f32) -> (Rect, bool) {
        let Some(toolbar) = self.panel_toolbar() else {
            return (main_b, false);
        };
        // #272: route through SidebarPanelLayout::hit_test so click +
        // paint share one definition of "where the toolbar slot is".
        let panel = SidebarPanel {
            id: WidgetId::new("panel-toolbar"),
            toolbar: Some(toolbar),
            toolbar_height: Some(self.toolbar_height(lh)),
        };
        let layout = panel.layout(
            main_b,
            quadraui::SidebarPanelMeasure::new(lh, 8.0),
            toolbar_tui_measure,
        );
        let content_rect = layout.content_bounds;
        match layout.hit_test(pos.x, pos.y) {
            SidebarPanelHit::ToolbarButton(id) => {
                let action_id = id.as_str().to_string();
                self.dispatch_toolbar_action(&action_id);
                (content_rect, true)
            }
            SidebarPanelHit::ToolbarEmpty => (content_rect, true),
            // Click landed below the toolbar — caller routes to the
            // tab bar / content beneath.
            SidebarPanelHit::Content { .. } | SidebarPanelHit::Empty => (content_rect, false),
        }
    }

    /// Resolve a toolbar `action_id` (e.g. `"toolbar:plan"`) to the same
    /// behaviour as the matching keybind.  Returns `true` if a redraw is
    /// required.  Mirrors the action paths in the key-press handler so
    /// click + keyboard stay in sync.
    pub(crate) fn dispatch_toolbar_action(&mut self, action_id: &str) -> bool {
        match action_id {
            // #192 / #263: `toolbar:plan` and `toolbar:approve` retired
            // alongside the PROPOSALS section.  The `coord plan` CLI
            // still works for scripts; the TUI just doesn't surface
            // it any more.
            "toolbar:add" => {
                // #353: [Add] button opens a refine-board chat for the selected repo.
                // If one repo exists, dispatch directly; if multiple, show picker.
                if self.board_repo_names.is_empty() {
                    self.push_toast(
                        "No repos configured",
                        "No repos found in coordinator.yml.",
                        ToastSeverity::Info,
                    );
                    return true;
                }
                if self.board_repo_names.len() == 1 {
                    let repo = self.board_repo_names[0].clone();
                    self.dispatch_board_chat_new_issue(&repo)
                } else {
                    // Multiple repos — open the picker dialog.
                    self.pending_repo_picker = Some(PendingRepoPicker {
                        repos: self.board_repo_names.clone(),
                        selected: None,
                        opened_at: Instant::now(),
                    });
                    true
                }
            }
            "toolbar:notify" => {
                use crate::commands::SpawnQueuedOutcome;
                match self.command_runner.spawn_queued(&["notify"]) {
                    SpawnQueuedOutcome::Started => {
                        self.last_notify = Instant::now();
                    }
                    SpawnQueuedOutcome::Queued => {
                        self.push_toast(
                            "⏳ Queued",
                            "notify runs after current command",
                            ToastSeverity::Info,
                        );
                    }
                    SpawnQueuedOutcome::Deduped => {}
                }
                true
            }
            "toolbar:merge" => {
                // #272-followup: shared classifier with the `m` keybind.
                // Outside the Pipeline view this falls through to plain
                // `coord merge` (server-side gates still apply).
                self.dispatch_pipeline_merge_for_selected_issue()
            }
            "toolbar:retry" => {
                if self.active_view == SidebarView::Pipeline {
                    if self.can_bounce_work_after_test_fail() {
                        self.dispatch_pipeline_work();
                    } else {
                        let dispatched = self.dispatch_pipeline_active_go();
                        if !dispatched {
                            self.push_toast(
                                "Nothing to retry",
                                "No pending or failed stage on the selected pipeline issue.",
                                ToastSeverity::Info,
                            );
                        }
                    }
                } else if let Some(a) = self.board_selected_failed_assignment() {
                    let id = a.id.clone();
                    use crate::commands::SpawnQueuedOutcome;
                    if let SpawnQueuedOutcome::Queued =
                        self.command_runner.spawn_queued(&["retry", &id])
                    {
                        self.push_toast(
                            "⏳ Queued",
                            "retry runs after current command",
                            ToastSeverity::Info,
                        );
                    }
                } else {
                    self.push_toast(
                        "No failed assignment selected",
                        "Focus a row with status FAIL in the Board sidebar, then click Retry.",
                        ToastSeverity::Info,
                    );
                }
                true
            }
            "toolbar:purge" => {
                if self.active_view == SidebarView::Board
                    && self.board_selection_in_completed_group()
                {
                    let secs = self.purge_days as f64 * 86_400.0;
                    let counts = count_purgeable_db(secs).unwrap_or((0, 0));
                    self.pending_purge = Some(counts);
                } else {
                    self.push_toast(
                        "Purge only runs on the Completed group",
                        "Focus a done/merged row in the Board sidebar, then click Purge.",
                        ToastSeverity::Info,
                    );
                }
                true
            }
            "toolbar:ready" => {
                let selected = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i));
                match selected {
                    None => self.push_toast(
                        "Nothing to mark ready",
                        "Select an issue in the pipeline first.",
                        ToastSeverity::Info,
                    ),
                    Some(issue) if issue.coord_repo.is_none() => self.push_toast(
                        "No coord_repo mapping",
                        &format!(
                            "{} isn't mapped in coordinator.yml — add a `repos` entry first.",
                            issue.repo_slug,
                        ),
                        ToastSeverity::Warning,
                    ),
                    Some(issue) => {
                        let repo = issue.coord_repo.clone().unwrap();
                        let num_str = issue.number.to_string();
                        use crate::commands::SpawnQueuedOutcome;
                        match self
                            .command_runner
                            .spawn_queued(&["ready", &repo, &num_str])
                        {
                            SpawnQueuedOutcome::Started => {
                                self.pipeline_status = Some((
                                    format!("#{}: marking ready", issue.number),
                                    Instant::now(),
                                ));
                            }
                            SpawnQueuedOutcome::Queued => {
                                self.push_toast(
                                    "⏳ Queued",
                                    "ready runs after current command",
                                    ToastSeverity::Info,
                                );
                            }
                            SpawnQueuedOutcome::Deduped => {}
                        }
                    }
                }
                true
            }
            // #316 Phase A: Board Chat tab CTA buttons.
            "board-chat:refine" => {
                if let Some(repo) = self.board_active_repo().map(str::to_string) {
                    self.dispatch_board_chat_refine(&repo)
                } else {
                    self.push_toast(
                        "No repo selected",
                        "Select a repo in the sidebar before starting a chat.",
                        ToastSeverity::Info,
                    );
                    false
                }
            }
            "board-chat:new-issue" => {
                if let Some(repo) = self.board_active_repo().map(str::to_string) {
                    self.dispatch_board_chat_new_issue(&repo)
                } else {
                    self.push_toast(
                        "No repo selected",
                        "Select a repo in the sidebar before starting a chat.",
                        ToastSeverity::Info,
                    );
                    false
                }
            }
            _ => false,
        }
    }
}

/// Cell-width measure used to lay out a [`Toolbar`] for hit-testing.
///
/// Mirrors `quadraui::tui::toolbar::tui_item_width` exactly — that
/// helper is `pub(crate)` so we can't import it.  Keep in sync with
/// upstream when the rasteriser's framing changes (currently
/// `"[ icon? label (hint)? ]"` for actions, 2 cells for separators,
/// raw char width for labels).
pub(crate) fn toolbar_tui_measure(btn: &ToolbarButton) -> ToolbarItemMeasure {
    let w = match btn {
        ToolbarButton::Action {
            label,
            icon,
            key_hint,
            ..
        } => {
            let icon_w = icon.as_ref().map(|s| s.chars().count() + 1).unwrap_or(0);
            // " (xxx)" — 3 cells of decoration ("()" + leading space)
            // plus the hint's own char width.
            let hint_w = key_hint
                .as_ref()
                .map(|s| s.chars().count() + 3)
                .unwrap_or(0);
            // "[ " + content + " ]"
            (4 + icon_w + label.chars().count() + hint_w) as f32
        }
        ToolbarButton::Separator => 2.0,
        ToolbarButton::Label { text, .. } => text.chars().count() as f32,
    };
    ToolbarItemMeasure::new(w)
}

/// Helper that builds one `ToolbarButton::Action` for the panel toolbar.
/// Action id is always `toolbar:<verb>` — disabled buttons keep the id
/// (so the layout still records them for hover tooltips) but the
/// primitive's `enabled` flag prevents click dispatch.
pub(crate) fn toolbar_button(verb: &str, label: &str, enabled: bool) -> ToolbarButton {
    ToolbarButton::Action {
        id: WidgetId::new(format!("toolbar:{}", verb)),
        // Strip the surrounding spaces — the primitive adds its own
        // padding via `[ ... ]` framing in the TUI rasteriser.
        label: label.trim().to_string(),
        icon: icon_for_action(verb).map(String::from),
        key_hint: None,
        enabled,
        is_active: false,
        tooltip: String::new(),
    }
}

/// Map an `action_id` (sidebar row action or panel-toolbar verb) to a
/// short unicode glyph used as the button icon.  Plain printable
/// unicode rather than Private-Use-Area nerdfont so the icons render
/// on every terminal; the user can swap to nerdfont later if desired.
pub(crate) fn icon_for_action(action_id: &str) -> Option<&'static str> {
    match action_id {
        // Row actions (sidebar action bar).
        "refine" => Some("✎"),
        "mark-refined" => Some("✓"),
        "send-to-pipeline" => Some("→"),
        "drop-to-backlog" => Some("↩"),
        "drop-to-refining" => Some("↶"),
        "start-work-interactive" => Some("⌨"),
        "start-plan-interactive" => Some("⌨"),
        "start-review-interactive" => Some("⌨"),
        "start-fix-interactive" => Some("⌨"),
        "reattach-live-session" => Some("⌨"),
        "chat-about-issue" => Some("✦"),
        "audit-outcomes" => Some("🔍"),
        "dispatch-gate-a-mock" => Some("🎭"),
        "view-gate-a-mock" => Some("👁"),
        // #2063: the sign-off verdict, beside the 👁 it follows from.
        "approve-gate-a" => Some("✓"),
        "request-gate-a-changes" => Some("✎"),
        "troubleshoot-interactive" => Some("⚕"),
        "diagnose-fix-stage" => Some("⚕"),
        "diagnose-stage" => Some("⚕"),
        "diagnose-reset" => Some("↺"),
        "start-with-plan" => Some("☰"),
        "start-skip-plan" => Some("▶"),
        "watch" => Some("◉"),
        "stop" => Some("■"),
        "open-pr" => Some("↗"),
        "bounce" => Some("↺"),
        // #2533 (ms-67 contract §4a): a fresh glyph, deliberately NOT `⌨`
        // (which this table already spends on the `InteractiveLaunchMode`
        // PTY family above) — this action dispatches a stream-json chat
        // session, not a PTY, and reusing `⌨` would misleadingly imply
        // otherwise (contract §1/§4c).
        "pull-into-decomposition-session" => Some("⇢"),
        // Panel-level verbs (`toolbar:<verb>` keys after the prefix).
        "notify" => Some("ⓘ"),
        "retry" => Some("↻"),
        "purge" => Some("✕"),
        "ready" => Some("✓"),
        "merge" => Some("⤵"),
        _ => None,
    }
}
