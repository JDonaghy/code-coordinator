//! `ShellApp` trait implementation and rendering logic extracted from `app/mod.rs` (#744).
//!
//! **Import pattern:** `use super::*` is intentional — these methods live on `CoordApp`
//! and need the full parent namespace (all quadraui types, app-field types, and bindings
//! from other extracted modules). Pure-function submodules (`format.rs`, `data.rs`) use
//! explicit imports because their dependency surface is small and stable.
#[allow(unused_imports)]
use super::*;

// ─── ShellApp implementation ──────────────────────────────────────────────────

impl ShellApp for CoordApp {
    /// Draw content into the shell's content zones.
    ///
    /// The shell has already rendered the activity bar, sidebar header, and
    /// divider. We draw:
    /// - The status bar into `layout.status_bar_bounds`.
    /// - The list (tree/machines/pipeline) into `layout.sidebar_content_bounds`.
    /// - The detail panel into `layout.main_content_bounds`.
    /// Push `active_theme` to the backend on first startup so the shell
    /// chrome (activity bar, sidebar) uses the user's saved theme from
    /// frame 0 rather than quadraui's built-in dark defaults.
    fn setup(&mut self, backend: &mut dyn Backend) {
        backend.set_theme(self.active_theme.clone());
    }

    fn render_content(&self, backend: &mut dyn Backend, layout: &AppShellLayout) {
        // Push the active theme to the backend at the start of every frame.
        // This ensures (a) content is always painted with the user's chosen
        // palette, and (b) the shell chrome (drawn by ShellAdapter *before*
        // render_content) uses the correct theme on the *next* frame — so
        // theme switches are visible within one redraw cycle.
        backend.set_theme(self.active_theme.clone());

        let lh = backend.line_height();

        // ── Status bar ────────────────────────────────────────────────
        if let Some(sb_bounds) = layout.status_bar_bounds {
            backend.draw_status_bar(sb_bounds, &self.status_bar(), None, None);
        }

        // ── Sidebar: list content (sidebar system / machines) ────────
        if let Some(full_sidebar_rect) = layout.sidebar_content_bounds {
            // #272: SidebarPanel composes the optional header toolbar (now
            // just the Board "Sync" button — the #270 contextual action bar
            // was retired in favour of the keyboard context menu) + the tree
            // beneath into one primitive.  When there's no header button the
            // slot isn't reserved, so the tree gets the full height; click
            // dispatch routes through `SidebarPanelLayout::hit_test` so the
            // off-by-one math we had to maintain by hand is gone.
            let panel = self.build_sidebar_action_panel(lh);
            let panel_layout = backend.draw_sidebar_panel(
                full_sidebar_rect,
                &panel,
                self.sidebar_action_bar_hover.hovered_id(),
                None,
            );
            let sidebar_rect = panel_layout.content_bounds;
            match self.active_view {
                SidebarView::Board => {
                    // #2282 (ms-65 §2f): stash the tree's painted geometry and
                    // the backend's own MSV header metric so reveal-on-activate
                    // — which runs on the event path, with no `Backend` handle
                    // — can compute a row's panel offset. Same render-then-act
                    // discipline as `last_main_visible_rows` above.
                    self.last_sidebar_geom
                        .set(Some((sidebar_rect, lh, backend.msv_metrics().header_size)));
                    self.board_sidebar.render(backend, sidebar_rect);
                }
                SidebarView::Machines => {
                    backend.draw_list(sidebar_rect, &self.machines_list(true));
                }
                SidebarView::Pipeline => {
                    // #2452: stash the tree's painted geometry the same way
                    // the Board arm above does — `scroll_pipeline_selection_
                    // into_view` runs on the event path (no `Backend`
                    // handle), so this render-then-act stash is its only
                    // source for the metrics its scroll-offset math needs.
                    self.last_pipeline_sidebar_geom
                        .set(Some((sidebar_rect, lh, backend.msv_metrics().header_size)));
                    self.pipeline_sidebar.render(backend, sidebar_rect);
                }
                SidebarView::Settings => {
                    // #237: settings is one full-width form — no category
                    // nav.  Render an empty placeholder list so the sidebar
                    // slot keeps a header (consistent with other views)
                    // without offering a misleading affordance.
                    backend.draw_list(sidebar_rect, &self.settings_sidebar_placeholder());
                }
                SidebarView::Terminal => {
                    // #953: left-pane machine-grouped tree of open
                    // terminals. #954 added create+attach (the `n`
                    // keybinding and the header "+ New terminal" button
                    // above this tree, see `build_sidebar_action_panel`) —
                    // kill is still a follow-up. The main area remains a
                    // single PTY surface.
                    backend.draw_tree(sidebar_rect, &self.terminal_tree_view());
                }
                // #638: Kanban sidebar is a placeholder — all content is in the main panel.
                SidebarView::Kanban => {
                    backend.draw_list(sidebar_rect, &self.kanban_sidebar_placeholder());
                }
                // #737: Merge Queue sidebar — entry count + attention indicator.
                SidebarView::MergeQueue => {
                    backend.draw_list(sidebar_rect, &self.merge_queue_sidebar());
                }
                // #771: Milestone DAG sidebar — milestone-with-work-order list.
                SidebarView::MilestoneDag => {
                    backend.draw_list(sidebar_rect, &self.milestone_dag_sidebar());
                }
                // #1121: Plans sidebar — repo->plan tree (All repos + one
                // node per configured repo, expandable to milestones), same
                // raw-`TreeView` pattern as the #953 Terminal / #1032
                // Sessions trees. Was a static plan-count `ListView`
                // placeholder (#975) before this.
                SidebarView::Plans => {
                    backend.draw_tree(sidebar_rect, &self.plans_tree_view());
                }
                // #1032: Sessions sidebar — machine → repo → session tree,
                // same raw-`TreeView` pattern as the #953 Terminal tree.
                SidebarView::Sessions => {
                    backend.draw_tree(sidebar_rect, &self.sessions_tree_view());
                }
                // #1039: Audit sidebar — entry count + "N recent" badge.
                SidebarView::Audit => {
                    backend.draw_list(sidebar_rect, &self.audit_sidebar());
                }
                // #1911: Reports sidebar — one collapsible section per
                // catalogue entry (parameters + Run), moved here from the
                // main panel so the grid gets the main panel to itself.
                SidebarView::Reports => {
                    self.render_reports_sidebar(backend, sidebar_rect);
                }
                // #1866: Queue sidebar — the aggregate queue reading
                // (running / waiting / blocked / held). The grid itself
                // lives in the main panel.
                SidebarView::Queue => {
                    backend.draw_list(sidebar_rect, &self.queue_sidebar());
                }
                // #2532: Approved work items sidebar — the aggregate
                // reading ("N ready to pull" + the conditional "missing a
                // repo mapping" warning line, contract §3b).
                SidebarView::Approved => {
                    backend.draw_list(sidebar_rect, &self.approved_sidebar());
                }
            }
        }

        // ── Main: detail panel only (full main_content_bounds) ───────
        let full_m = layout.main_content_bounds;
        // #249 Principle 1: draw the per-panel toolbar at the top of
        // main_content_bounds so every panel verb has a visible
        // affordance (Plan / Notify / Merge / etc.).  Carve the toolbar
        // row off the top; everything else renders into the shrunken
        // rect below it.
        let m = if let Some(toolbar) = self.panel_toolbar() {
            // #272: same SidebarPanel composition for the main-panel
            // toolbar so the tab bar below doesn't have to coordinate
            // its own slot carving.
            let panel = SidebarPanel {
                id: WidgetId::new("panel-toolbar"),
                toolbar: Some(toolbar),
                toolbar_height: Some(self.toolbar_height(lh)),
            };
            let panel_layout = backend.draw_sidebar_panel(
                full_m,
                &panel,
                self.panel_toolbar_hover.hovered_id(),
                None,
            );
            panel_layout.content_bounds
        } else {
            full_m
        };
        // Keep watch_log_list's stick-to-bottom math in sync with the live
        // viewport on every frame (not just when the user scrolls).
        self.last_main_visible_rows
            .set(content_visible_rows(m, lh).max(1));
        match self.active_view {
            SidebarView::Board => {
                // #2288 (ms-65 §9): the panel toolbar above has already been
                // carved off `full_m` and spans the FULL panel width — it is
                // panel-scoped, not pane-scoped. Everything from here down
                // (doc-tab strip, sub-tab bar, content) is what the split
                // duplicates, so `m` is the rect the `SplitTree` divides.
                //
                // One pane ⇒ `split_tree` is a bare `Leaf`, its single leaf
                // rect is `m` verbatim and no divider is painted: §9's "with
                // a single pane, rendering is byte-identical to the
                // non-split case".
                let panes = self.board_panes();
                let tree = panes.split_tree(PanelScope::Board);
                let split_layout = backend.split_tree_layout(m, &tree);
                self.paint_board_pane_dividers(backend, &split_layout);
                let leaves: Vec<Rect> = split_layout
                    .leaves
                    .iter()
                    .map(|(_, rect)| *rect)
                    .collect();
                *self.board_split_layout.borrow_mut() = Some(split_layout);
                // #2288: a pane that no longer exists must not leave its
                // Issue wrap width behind for whatever pane later recycles
                // its index.
                self.truncate_board_pane_issue_cols(leaves.len());
                for (pane_idx, pane_rect) in leaves.into_iter().enumerate() {
                    self.render_pane.set(Some(pane_idx));
                    self.render_board_pane(backend, pane_rect, lh);
                }
                // Back to "the focused pane" the moment the paint is over —
                // every event handler must see the focused pane, never
                // whichever one happened to be painted last.
                self.render_pane.set(None);
            }
            SidebarView::Machines => {
                // #207: Reserve two sparkline rows (CPU + mem) at the bottom
                // of the main panel.  Each row is 2 × lh tall: one cell for
                // the label line and one for the chart body.  When there are
                // no metrics yet the area shows a subtle placeholder.
                let chart_h = lh * 4.0; // 2 rows × 2 lh each
                let detail_h = (m.height - chart_h).max(0.0);
                let detail_rect = Rect::new(m.x, m.y, m.width, detail_h);
                let chart_rect = Rect::new(m.x, m.y + detail_h, m.width, chart_h);
                backend.draw_list(detail_rect, &self.machine_detail_list());
                self.render_machine_sparklines(backend, chart_rect, lh);
            }
            SidebarView::Settings => {
                // Build form for the current category and render via
                // FormController (handles scrollbar + layout).
                let form = self.build_settings_form();
                let mut fc = self.settings_form.borrow_mut();
                fc.set_form(form);
                fc.render_and_cache(backend, m);
            }
            SidebarView::Pipeline => {
                // Watch overlay takes over the entire main panel when active —
                // tabs, pipeline view, meta line all hidden while watching.
                if self.pipeline_sel.is_none() && self.pipeline_issues.is_empty() {
                    backend.draw_list(m, &self.pipeline_placeholder_list());
                } else {
                    // #2284 (ms-65 §3a): Pipeline's `panel_toolbar()` returns
                    // `None` — `m` above is the untouched `full_m` — so the
                    // doc-tab strip is the FIRST row of the main panel here,
                    // with no toolbar row above it (Board's own strip is the
                    // SECOND row: `panel_toolbar()` precedes it there). Zero
                    // Pipeline tabs open ⇒ `pipeline_doc_tab_strip` is `None`
                    // and the row is not reserved — §3a's own "Pipeline shows
                    // its baseline" zero-tab control, mirroring Board's #2282
                    // zero-tab baseline.
                    let tab_h = detail_tab_bar_height(lh);
                    let m = match self.pipeline_doc_tab_strip(m.width) {
                        Some(strip) => {
                            let strip_rect = Rect::new(m.x, m.y, m.width, tab_h);
                            backend.draw_tab_bar(strip_rect, &strip, None);
                            Rect::new(m.x, m.y + tab_h, m.width, (m.height - tab_h).max(0.0))
                        }
                        None => m,
                    };
                    // Tab bar.  `#464`: route through `detail_tab_bar_height`
                    // so the painted top of the content rect lines up with
                    // the hit-test origin in the TUI backend.
                    let mut tab_bar = self.pipeline_detail_tab_bar();
                    let tab_rect = Rect::new(m.x, m.y, m.width, tab_h);
                    let content_rect =
                        Rect::new(m.x, m.y + tab_h, m.width, (m.height - tab_h).max(0.0));
                    // #605: scroll the tab bar so the ACTIVE tab is visible on a
                    // narrow width (7 tabs overflow a small screen). The TUI
                    // painter renders from `scroll_offset` verbatim, so resolve
                    // the offset that keeps the active tab on-screen before
                    // drawing. The click hit-test below derives the same offset.
                    tab_bar.scroll_offset =
                        backend.tab_bar_layout(tab_rect, &tab_bar).correct_scroll_offset;
                    backend.draw_tab_bar(tab_rect, &tab_bar, None);

                    // #818: helper — draw the compact read-only stage strip
                    // pinned at the top of every non-Overview tab.  Returns
                    // the rect below the strip for the tab's own content.
                    let content_below_strip = |app: &Self,
                                               backend: &mut dyn Backend,
                                               cr: Rect|
                     -> Rect {
                        let Some(pv) = app.build_pipeline_widget() else {
                            return cr;
                        };
                        let strip_rect = pipeline_detail_pv_rect_strip(cr, lh);
                        let render_view = pipeline_view_for_render(&pv);
                        backend.draw_pipeline_view(strip_rect, &render_view);
                        Rect::new(
                            cr.x,
                            cr.y + strip_rect.height,
                            cr.width,
                            (cr.height - strip_rect.height).max(0.0),
                        )
                    };

                    match self.pipeline_detail_tab {
                        PipelineDetailTab::Overview => {
                            // #303: button bar above the stage row when a
                            // stage is dispatchable.  Eats `bar_h` from the
                            // top of `content_rect`; the stage row and meta
                            // body shift down by the same amount.
                            // #438: use the shared helper so render and
                            // hover-tracking always agree on the toolbar
                            // shape; pass the live hover id so the button
                            // tints on mouse-over.
                            let action_toolbar = self.pipeline_action_bar_toolbar();
                            let bar_h = pipeline_action_bar_height(action_toolbar.is_some(), lh);
                            let bar_rect = Rect::new(
                                content_rect.x,
                                content_rect.y,
                                content_rect.width,
                                bar_h,
                            );
                            let pv_origin = Rect::new(
                                content_rect.x,
                                content_rect.y + bar_h,
                                content_rect.width,
                                (content_rect.height - bar_h).max(0.0),
                            );
                            let pv_rect = pipeline_detail_pv_rect(pv_origin, lh);
                            let meta_rect = Rect::new(
                                content_rect.x,
                                pv_rect.y + pv_rect.height,
                                content_rect.width,
                                (content_rect.height - bar_h - pv_rect.height).max(0.0),
                            );
                            if let Some(toolbar) = action_toolbar {
                                backend.draw_toolbar(
                                    bar_rect,
                                    &toolbar,
                                    self.pipeline_action_bar_hover.hovered_id(),
                                    None,
                                );
                            }
                            if let Some(view) = self.build_pipeline_widget() {
                                backend
                                    .draw_pipeline_view(pv_rect, &pipeline_view_for_render(&view));
                            } else {
                                backend.draw_list(pv_rect, &self.pipeline_placeholder_list());
                            }
                            backend.draw_list(meta_rect, &self.pipeline_tab_body_list());
                        }
                        PipelineDetailTab::Issue => {
                            // #818: pinned stage strip above the issue body.
                            let body_rect = content_below_strip(self, backend, content_rect);
                            // #669: stash content width so pipeline_issue_body_list can
                            // word-wrap long lines to the viewport.
                            self.last_issue_panel_cols.set(body_rect.width as usize);
                            backend.draw_list(body_rect, &self.pipeline_issue_body_list());
                        }
                        PipelineDetailTab::Log => {
                            // #818: pinned stage strip above the log.
                            let log_rect = content_below_strip(self, backend, content_rect);
                            // #399: reserve 1 column on the right for the
                            // vertical scrollbar.  This keeps the list content
                            // out from under the thumb glyph.  For GTK/macOS
                            // backends, `width` is in pixels and 1 px is below
                            // the thumb minimum so the scrollbar is a no-op —
                            // those backends use native scrolling.
                            let sb_col_w = if log_rect.width >= 2.0 {
                                1.0_f32
                            } else {
                                0.0_f32
                            };
                            let list_rect = Rect::new(
                                log_rect.x,
                                log_rect.y,
                                (log_rect.width - sb_col_w).max(1.0),
                                log_rect.height,
                            );
                            // #385: stash panel width so pipeline_log_list can
                            // word-wrap assistant prose to the viewport.
                            self.last_log_panel_cols.set(list_rect.width as usize);
                            let log_list = self.pipeline_log_list();

                            // #399: draw the vertical scrollbar at the right edge.
                            if sb_col_w > 0.0 {
                                let total = log_list.items.len();
                                let visible = (log_rect.height as usize).max(1);
                                if total > visible {
                                    let sb_track = Rect::new(
                                        log_rect.x + list_rect.width,
                                        log_rect.y,
                                        sb_col_w,
                                        log_rect.height,
                                    );
                                    let vsb = Scrollbar::vertical(
                                        "pipeline-log-vsb",
                                        sb_track,
                                        log_list.scroll_offset as f32,
                                        total as f32,
                                        visible as f32,
                                        1.0,
                                    );
                                    backend.draw_scrollbar(sb_track, &vsb);
                                }
                            }

                            // Collect the visible text for pixel-based backends
                            // (GTK/macOS).  The TUI backend ignores `lines` and
                            // reads selection directly from its ratatui cell
                            // buffer, so this is a no-cost no-op for TUI users.
                            let lines: Vec<String> = log_list
                                .items
                                .iter()
                                .map(|it| it.text.spans.iter().map(|s| s.text.as_str()).collect())
                                .collect();
                            backend.draw_list(list_rect, &log_list);
                            // #312: register as a selectable TextRegion so the
                            // quadraui runtime handles click-drag line selection
                            // and Ctrl-C copy (OSC52 + arboard) automatically.
                            backend.register_text_region(TextRegion {
                                id: WidgetId::new("pipeline-log"),
                                bounds: list_rect,
                                lines,
                            });
                        }
                        PipelineDetailTab::Summary => {
                            // #818: pinned stage strip above the summary.
                            let summary_body = content_below_strip(self, backend, content_rect);
                            // #558/#876: session-history summary sourced from
                            // the in-memory board.
                            let summary_list = self.pipeline_summary_list();
                            backend.draw_list(summary_body, &summary_list);
                        }
                        PipelineDetailTab::Terminal => {
                            // #818: pinned stage strip above the terminal.
                            // The PTY is resized to the reduced rect so the
                            // strip doesn't overdraw it.
                            let term_rect = content_below_strip(self, backend, content_rect);
                            // #440: per-issue interactive shell.  Stashes
                            // dims, reads the session snapshot, paints the
                            // PTY surface.  Spawn / resize / poll happen in
                            // `drive_detail_terminals` on every tick.
                            self.render_detail_terminal_tab(backend, term_rect);
                        }
                        PipelineDetailTab::Completed => {
                            // #2405: the completed-issues grid owns the whole
                            // content rect — no pinned stage strip. The strip
                            // describes *the selected issue's* pipeline, and
                            // this tab is not about the selected issue at all
                            // (it is a historical report over every completed
                            // one), so pinning it here would caption the grid
                            // with an unrelated issue's stages.
                            self.render_pipeline_completed(backend, content_rect, lh);
                        }
                    }
                }
            }
            SidebarView::Terminal => {
                // #424: draw the live PTY surface into the full main rect.
                // Render path is `&self`, so we cannot spawn / resize /
                // poll here.  Instead:
                //   - stash the desired (cols, rows) for `tick` to apply
                //     via `TerminalSession::resize`,
                //   - read the session and build a paint snapshot, or
                //   - render a one-line placeholder when no session yet
                //     exists (spawn happens on the next `tick`).
                let cell_w = backend.char_width().max(1.0);
                let cell_h = lh.max(1.0);
                // #790: reserve a 1-row hint strip at the bottom advertising
                // F9 copy mode (and its in-mode controls).  The terminal
                // content — and thus the PTY resize — uses the shrunken rect.
                let hint_h = cell_h.min(m.height);
                let term_rect =
                    Rect::new(m.x, m.y, m.width, (m.height - hint_h).max(0.0));
                let hint_rect = Rect::new(
                    m.x,
                    m.y + term_rect.height,
                    m.width,
                    m.height - term_rect.height,
                );
                let cols = (term_rect.width / cell_w).floor().max(1.0) as u16;
                let rows = (term_rect.height / cell_h).floor().max(1.0) as u16;
                self.terminal_pending_dims.set(Some((cols, rows)));

                // #955: a selected Terminal-tree leaf switches the main
                // pane to that fleet terminal's attached PTY; otherwise
                // it's the bare local-shell fallback — both are painted
                // identically via `standalone_pty_session` once live.
                let selected_fleet = self.selected_fleet_terminal_key();
                if let Some(sess) = self.standalone_pty_session() {
                    let total = sess.history_len() + sess.rows() as usize;
                    let sb = if total > sess.rows() as usize {
                        Some(sess.scrollbar_state(None))
                    } else {
                        None
                    };
                    let snapshot = sess.to_terminal(WidgetId::new("coord-terminal:0"), sb);
                    backend.draw_terminal(term_rect, &snapshot);
                } else {
                    // No session yet — show a one-line placeholder.  The
                    // first `tick` after this render will spawn/attach it.
                    let (msg, is_err) = if let Some((machine, name)) = selected_fleet.as_ref() {
                        match self
                            .fleet_terminal_spawn_errors
                            .get(&(machine.clone(), name.clone()))
                        {
                            Some(err) => (
                                format!(
                                    "  ⚠ Attach error ({}:{}): {}  (click the activity bar to switch views)",
                                    machine, name, err
                                ),
                                true,
                            ),
                            None => (format!("  Attaching to {}:{}…", machine, name), false),
                        }
                    } else {
                        match &self.terminal_spawn_error {
                            // §3 (#782): numeric keys no longer switch views —
                            // point at the activity bar instead.
                            Some(err) => (
                                format!(
                                    "  ⚠ Terminal session error: {}  (click the activity bar to switch views)",
                                    err
                                ),
                                true,
                            ),
                            None => ("  Starting shell session…".to_string(), false),
                        }
                    };
                    let item = activity_item(
                        &msg,
                        if is_err {
                            Color::rgb(220, 80, 80)
                        } else {
                            Color::rgb(180, 180, 180)
                        },
                    );
                    backend.draw_list(
                        term_rect,
                        &ListView {
                            id: WidgetId::new("terminal-placeholder"),
                            title: None,
                            items: vec![item],
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
                // #790: paint the hint strip last so it sits below the
                // terminal content.
                backend.draw_list(hint_rect, &self.terminal_copy_hint_list());
            }
            // #638: Kanban view — render the Board widget into the full main rect.
            SidebarView::Kanban => {
                let layout = backend.draw_board(m, &self.kanban_model);
                *self.kanban_layout.borrow_mut() = Some(layout);
            }
            // #737: Merge Queue panel — render the entry list in the main area.
            SidebarView::MergeQueue => {
                self.render_merge_queue_panel(backend, m, lh);
            }
            // #771: Milestone DAG panel — cohort rows + state badges for the
            // selected milestone's work order.
            SidebarView::MilestoneDag => {
                self.render_milestone_dag_panel(backend, m, lh);
            }
            // #975: Plans panel — one row per milestone/epic with counts.
            SidebarView::Plans => {
                self.render_plans_panel(backend, m, lh);
            }
            // #1032: Sessions panel — detail view of the selected session
            // leaf (assignment id, issue/repo, machine, type, status).
            SidebarView::Sessions => {
                backend.draw_list(m, &self.sessions_detail_list());
            }
            // #1039: Audit panel — newest-first entry list, with an inline
            // detail split when `audit_detail_open`.
            SidebarView::Audit => {
                self.render_audit_panel(backend, m, lh);
            }
            // #1911: Reports panel — the result grid + notes only; the
            // section stack (parameters + Run) lives in the sidebar now.
            SidebarView::Reports => {
                self.render_reports_panel(backend, m, lh);
            }
            // #1866: Queue panel — the live drive-queue grid.
            SidebarView::Queue => {
                self.render_queue_panel(backend, m, lh);
            }
            // #2532: Approved work items panel — the row list, with an
            // inline detail split when `approved_detail_open`.
            SidebarView::Approved => {
                self.render_approved_panel(backend, m, lh);
            }
        }

        // ── Inject chat overlay — renders over the main panel ───────────
        // #264: refinement chats render in the Pipeline Refinement tab above.
        // #316: board chats render in the Board Chat tab above.
        // Worker-guidance chats (`b` over a watched worker) stay modal —
        // those are short bursts where modal hijacking matches intent.
        let chat_is_refinement = self.chat_is_refinement();
        let chat_is_board = self.chat_is_board_chat();
        if let Some(ref chat) = self.inject_chat {
            if !chat_is_refinement && !chat_is_board {
                backend.draw_list(
                    m,
                    &ListView {
                        id: WidgetId::new("chat-backing"),
                        title: None,
                        items: Vec::new(),
                        selected_idx: 0,
                        scroll_offset: 0,
                        has_focus: false,
                        bordered: true,
                        h_scroll: 0,
                        max_content_width: None,
                        show_v_scrollbar: false,
                    },
                );
                chat.render(backend, m);
            }
        }

        // ── #319 Phase A: refinement-notes review modal ────────────────
        // Renders on top of the chat so the user can compare the proposed
        // body against what's in the chat transcript above.  Drawn before
        // toasts/context-menu so those still appear over it if relevant.
        if self.refinement_notes_modal.is_some() {
            self.render_refinement_notes_modal(backend, layout.main_content_bounds);
        }

        // ── Toast overlay (bottom-right of main content) ────────────────
        if let Some(stack) = self.toast_stack() {
            backend.draw_toast_stack(layout.main_content_bounds, &stack);
        }

        // ── #259: open context menu (above everything except dialogs) ───
        // Drawn before dialogs so a prompt dialog still sits on top.
        // The viewport unions the sidebar + main panel so a menu anchored
        // in the sidebar can flow rightward into the main area without
        // being clipped to the (narrow) sidebar width.
        if self.pending_context_menu.is_some() {
            let viewport =
                union_rects(layout.sidebar_content_bounds, layout.main_content_bounds);
            self.render_context_menu(backend, viewport);
        } else {
            // Keep the cached layout in sync — clear it once the menu
            // is no longer rendered so a stale layout can't satisfy a
            // hit-test on the next click.
            *self.context_menu_layout.borrow_mut() = Vec::new();
        }

        // ── #369/#329: Prompt dialogs (highest z-order) ──────────────────
        // Rendered after everything else so the modal sits on top of the
        // context menu, toasts, and panel content.  The viewport unions
        // sidebar + main so the dialog centers across the whole content
        // area (not just the narrow sidebar).
        let dialog_viewport =
            union_rects(layout.sidebar_content_bounds, layout.main_content_bounds);
        self.render_prompt_dialog(backend, dialog_viewport);

        // ── #541: issue fuzzy finder (topmost overlay) ─────────────────
        // Rendered last so it sits above all other content including
        // prompt dialogs.  When the finder is closed this is a no-op.
        if self.issue_finder.is_some() {
            self.render_issue_finder(backend, dialog_viewport);
        }

        // ── #1124: Plans `?` help overlay / `/` command palette ────────
        // Same topmost z-order reasoning as the issue finder just above —
        // modal overlays scoped to the main content area of whichever view
        // registered help content (today only Plans; see
        // `SidebarView::help_view_id`). Both are no-ops when closed.
        self.render_help_overlay(backend, layout.main_content_bounds, lh);
        self.render_command_palette(backend, layout.main_content_bounds, lh);

        // ── #1631 (H-4): fleet-health detail overlay (topmost) ──────────
        // Same z-order reasoning as the help overlay/command palette just
        // above — a global overlay, not scoped to any one view's main
        // content. No-op when closed.
        self.render_fleet_health_overlay(backend, layout.main_content_bounds);

        // ── #2642: Ctrl+E doc-tab picker (topmost) ───────────────────────
        // Same z-order reasoning as the issue finder / command palette
        // above. No-op when closed.
        self.render_doc_tab_picker(backend, layout.main_content_bounds);
    }

    fn handle(
        &mut self,
        event: UiEvent,
        backend: &mut dyn Backend,
        ctx: &ShellContext,
    ) -> Reaction {
        let reaction = self.dispatch_handle(event, backend, ctx);
        // #2286 (ms-65 §6): unconditionally flush doc tabs on the way out —
        // exactly once, regardless of which of the several `Reaction::Exit`
        // call sites in `events.rs` (plain `q`/Esc, the force-quit confirm
        // dialog's key or mouse-click path) fired. This is what CREATES
        // `~/.coord/tabs.json` the first time; every tab mutator's own
        // `persist_doc_tabs()` call only ever UPDATES it once it exists (see
        // `doc_tabs::DocTabs::save_if_exists`'s doc comment for why).
        if matches!(reaction, Reaction::Exit) {
            self.persist_doc_tabs_on_exit();
        }
        reaction
    }

    /// Sync `active_view` when the shell switches panels via activity bar click.
    ///
    /// §2 (#782): Settings lives in `with_bottom_items`, so clicking it
    /// fires `AppShellEvent::BottomItemClicked { id }` rather than
    /// `PanelChanged`.  Both variants are handled here.
    fn on_shell_event(&mut self, event: &AppShellEvent) {
        // §3 (#782): any activity-bar navigation resets keyboard focus back
        // to Sidebar so Ctrl-W h/l starts from a known state.
        let panel_id_str = match event {
            AppShellEvent::PanelChanged { panel_id } => panel_id.as_str(),
            AppShellEvent::BottomItemClicked { id } => id.as_str(),
            _ => return,
        };
        self.focused_region = FocusedRegion::Sidebar;
        // #1029 bug B (iter-2): a *real* ActivityBar click is always a fresh,
        // explicit operator choice, so it invalidates any pending
        // "return to origin on Esc" bookmark — even a click that lands on
        // Terminal (a plain visit with nothing to return to). The one
        // exception is the programmatic replay of our own queued panel switch
        // (`pending_panel_switch`): quadraui pulls it via
        // `take_requested_panel` and immediately re-fires this handler as a
        // `PanelChanged`, but that replay must NOT wipe the bookmark a
        // milestone-chat launch just set. `take_requested_panel` flags that
        // one replay; consume the flag here and skip the clear for it only.
        if self.pending_switch_is_programmatic {
            self.pending_switch_is_programmatic = false;
        } else {
            self.terminal_return_view = None;
        }
        self.active_view = match panel_id_str {
            "panel:board" => SidebarView::Board,
            "panel:machines" => SidebarView::Machines,
            "panel:pipeline" => {
                self.maybe_kick_pipeline_loader();
                SidebarView::Pipeline
            }
            "panel:settings" => SidebarView::Settings,
            "panel:terminal" => {
                // #424: entering the Terminal view defaults to
                // PTY-focused so the user can start typing
                // immediately.  F12 releases focus back to the TUI chrome.
                self.terminal_focused = true;
                SidebarView::Terminal
            }
            // §1 (#782): Kanban + Merge Queue activity-bar panels.
            "panel:kanban" => SidebarView::Kanban,
            "panel:mergequeue" => SidebarView::MergeQueue,
            // #975: Plans panel — the ActivityBar item now labelled "Plans"
            // (see shell_config()).  Legacy `panel:milestones` id still
            // routes here so users who had the old button pinned land on
            // the new Plans panel (which subsumes MilestoneDag's roster
            // view); the MilestoneDag view itself remains accessible as a
            // future drill-down but no longer has its own top-level entry.
            "panel:plans" | "panel:milestones" => SidebarView::Plans,
            // #1032: Sessions panel — fleet-wide machine → repo → session tree.
            "panel:sessions" => SidebarView::Sessions,
            // #1039: Audit panel — newest-first audit-trail list.
            "panel:audit" => SidebarView::Audit,
            // #1741: Reports panel — catalogue-driven collapsible sections.
            //
            // #1763 retired the #1116 Usage panel: its per-issue/repo
            // cost+token rollup is now the `usage` entry in the server-side
            // report catalogue, priced from the daemon's own `pricing:`
            // config instead of a compiled-in snapshot. The legacy
            // `panel:usage` id still routes here — exactly as
            // `panel:milestones` still routes to Plans after #975 — so an
            // operator who had that button pinned lands on the panel that
            // subsumed it rather than on a dead view.
            "panel:reports" | "panel:usage" => SidebarView::Reports,
            // #1866 (Q-1): Queue panel — the live drive-queue grid. Carries
            // no fetch of its own (`/board` already ships `drive_queue`), so
            // unlike `panel:pipeline` above there is nothing to kick here.
            "panel:queue" => SidebarView::Queue,
            // #2532: Approved work items panel — no fetch to kick, same as
            // Queue immediately above (rows ride the existing `/board` poll).
            "panel:approved" => SidebarView::Approved,
            _ => return,
        };
        // #1124: the `?` help overlay / `/` command palette are scoped to
        // whichever view opened them — a real ActivityBar click is always
        // an explicit "leave this view" choice (same reasoning as the
        // `terminal_return_view` clear above), so close them rather than
        // let them silently bleed into the newly-active view's screen.
        if self.help_overlay.is_open() {
            self.help_overlay.close();
        }
        self.command_palette = None;
    }

    /// #1029 bug A: hand quadraui the panel queued by
    /// `CoordApp::switch_active_view`, if any. `ShellAdapter` applies it to
    /// the real `AppShell` state (ActivityBar highlight + sidebar header)
    /// and re-fires `on_shell_event(PanelChanged)` — the same notification
    /// a mouse click produces, so `on_shell_event` above stays the single
    /// place `active_view` gets set from shell-driven switches.
    fn take_requested_panel(&mut self) -> Option<WidgetId> {
        let panel = self.pending_panel_switch.take();
        // #1029 bug B (iter-2): quadraui always follows a non-None pull here
        // with an `on_shell_event(PanelChanged)` replay (see quadraui
        // `apply_requested_panel`). Flag that replay so `on_shell_event`
        // treats it as programmatic — leaving any freshly-set
        // `terminal_return_view` bookmark intact — instead of as a fresh
        // operator click that would clear it.
        if panel.is_some() {
            self.pending_switch_is_programmatic = true;
        }
        panel
    }

    /// Periodic callback driven by the quadraui runner (~60Hz on TUI).
    /// Does the same time-based work as `handle()` so background refreshes,
    /// command-runner draining, and watch-log polling proceed even when the
    /// user isn't typing.
    fn tick(&mut self, _backend: &mut dyn Backend) -> Reaction {
        // Drain any completed background data load first.  Without this,
        // run_periodic_work() kicks off a new fetch and shows
        // "↻ loading…" but never picks up the completed receiver, so the
        // status bar stays yellow and assignment state (e.g. running→done)
        // doesn't reflect until the user moves the mouse or presses a key.
        let mut needs_redraw = self.apply_pending_data();
        needs_redraw |= self.run_periodic_work();
        // ── #424: drive the embedded terminal PTY ──────────────────────
        // (Lazy spawn, resize-on-demand, poll-for-output.)  Performed on
        // every tick regardless of which view is active so output keeps
        // arriving even when the user has switched away — the same
        // pattern the watch-pool already follows.  Spawn, however, only
        // fires the first time the Terminal view becomes active.
        needs_redraw |= self.drive_terminal_pane();
        // ── #440: drive per-issue detail-view terminals ─────────────────
        // Lazy spawn for the selected issue when the Terminal tab is open;
        // resize and poll all sessions on every tick so background issues'
        // shells keep accumulating output.
        needs_redraw |= self.drive_detail_terminals();
        // ── Leg 2 (#517): auto-advance Work → Review ────────────────────
        // After the board has been refreshed (apply_pending_data above),
        // check whether any interactive work we launched this run has
        // finished (board-driven — never scrapes the session TTY) and, if
        // so, raise the one-key confirm prompt to start its review.
        needs_redraw |= self.detect_completed_interactive_work();
        // ── #685: headless work with test-mode:smoke → interactive smoke ──
        // When headless Work completes on an issue carrying test-mode:smoke,
        // raise the interactive-smoke offer (same UX as interactive Work → Test).
        needs_redraw |= self.detect_headless_smoke_work_done();
        // ── Leg 3 (#517): verdict-driven routing ────────────────────────
        // Route a freshly-reported review verdict: request-changes → rework
        // prompt; approve → smoke/merge notice.  Board-driven, never scraped.
        needs_redraw |= self.detect_review_verdict();
        // ── Leg 3c / A3 (#517, #581): test-verdict routing ──────────────
        // Route a freshly-recorded `coord test` verdict on the work row:
        // failed → fail→fix prompt; passed/skipped → pass→merge prompt.
        // Board-driven (the verdict is written to the DB), never scraped.
        needs_redraw |= self.detect_test_verdict();
        // After data has been applied, the Log tab's preferred assignment may
        // have changed (e.g. auto_loop dispatched a new review/fix). Re-attach
        // SSE so we don't fall back to the polling 'Loading log…' flicker.
        if self.on_pipeline_log_tab() {
            self.ensure_log_tab_sse();
        }
        // #264: while a chat overlay is open, refresh its transcript from
        // the focused pool entry when there is new content.  Skip the
        // rebuild on ticks where neither sse.lines nor inject_transcript
        // grew — at 60 fps with a long-running chat this was scanning and
        // JSON-parsing thousands of accumulated lines every 16 ms and
        // dominated the TUI's CPU budget.
        if self.inject_chat.is_some() {
            if let Some(id) = self.watch_focused.clone() {
                if let Some(ctx) = self.watch_pool.get(&id) {
                    let key = (id.clone(), ctx.sse.lines.len(), ctx.inject_transcript.len());
                    if self.chat_transcript_cache_key.as_ref() != Some(&key) {
                        let transcript = chat_transcript_from_pool(ctx);
                        if let Some(ref mut chat) = self.inject_chat {
                            chat.set_transcript(transcript);
                        }
                        self.chat_transcript_cache_key = Some(key);
                        // Activity stamp drives the busy indicator below.
                        self.chat_last_activity = Some(Instant::now());
                    }
                }
                // #264: reflect worker state in the chat status strip so the
                // user can see when claude -p has exited (stop_reason:
                // end_turn).  Otherwise the strip stays on its initial
                // "Refinement chat → repo #N" label and the chat looks like
                // it's just slow when really the session is dead.
                let assignment_state = self
                    .data
                    .assignments
                    .iter()
                    .find(|a| a.id == id)
                    .map(|a| (a.status.clone(), a.assignment_type.clone()));
                if let Some((status, atype)) = assignment_state {
                    let issue_number = self
                        .watch_pool
                        .get(&id)
                        .map(|c| c.state.issue_number)
                        .unwrap_or(0);
                    let is_board_chat = issue_number == 0;
                    if let Some(ref mut chat) = self.inject_chat {
                        let label = match atype.as_deref() {
                            Some("refinement") if !is_board_chat => "Refinement chat",
                            Some("refinement") => "Board refinement",
                            Some("new-issue-chat") => "New issue chat",
                            _ => "Chat",
                        };
                        let suffix = if self.pending_chat_resume.is_some() {
                            // #315: a resume dispatch is in-flight — tell the
                            // user we're spinning up a new worker.
                            "  ⏳ Resuming session…"
                        } else {
                            match (atype.as_deref(), status.as_str(), is_board_chat) {
                                // #315: refinement chats are resumable from
                                // any terminal state — typing triggers
                                // chat-continue, claude reloads the session,
                                // the chat continues seamlessly.  Never show
                                // a "read-only" warning here; the prompt
                                // suffix stays send-enabled.
                                // #410: Esc now shows Cancel/Save/Send dialog.
                                (Some("refinement"), _, false) => {
                                    "  (Ctrl+S/Alt+Enter = send · Ctrl+N = notes · Esc = Cancel/Save/Send)"
                                }
                                // #316: board chats — no notes, but Ctrl+F = file issue for new-issue-chat.
                                (Some("new-issue-chat"), _, true) => {
                                    "  (Ctrl+S/Alt+Enter = send · Ctrl+F = file issue · Esc = close)"
                                }
                                (_, _, true) => {
                                    "  (Ctrl+S/Alt+Enter = send · Esc = close)"
                                }
                                (_, "done", _) | (_, "failed", _) | (_, "cancelled", _) => {
                                    "  ⚠ Worker exited — chat is read-only.  Esc to close."
                                }
                                _ => "  (Ctrl+S/Alt+Enter = send · Ctrl+N = post notes · Esc = finish)",
                            }
                        };
                        let target = if is_board_chat {
                            let repo = self
                                .watch_pool
                                .get(&id)
                                .map(|c| c.state.repo.clone())
                                .unwrap_or_default();
                            format!("  {} → {}{}", label, repo, suffix)
                        } else {
                            format!("  {} → #{}{}", label, issue_number, suffix)
                        };
                        chat.set_status(StyledText::plain(target));
                    }
                }
            }
            // Busy = activity in the last 2 s.  Animates the chat's
            // spinner so the user can tell the worker is mid-reply.
            // Throttle the spinner-driven redraw to ~10 fps so we don't
            // burn the 60 fps repaint budget on a glyph that only
            // changes ten times a second.
            let busy = self
                .chat_last_activity
                .map(|t| t.elapsed() < std::time::Duration::from_secs(2))
                .unwrap_or(false);
            if let Some(ref mut chat) = self.inject_chat {
                chat.set_busy(busy);
            }
            if busy {
                self.chat_spinner_throttle = self.chat_spinner_throttle.wrapping_add(1);
                // 6 × 16 ms ≈ 96 ms ⇒ ~10 fps spinner.
                if self.chat_spinner_throttle % 6 == 0 {
                    needs_redraw = true;
                }
            }
        } else if self.chat_transcript_cache_key.is_some() {
            // Chat closed — drop the cache key so the next open starts
            // fresh and doesn't false-hit against a stale state.
            self.chat_transcript_cache_key = None;
            self.chat_last_activity = None;
            self.chat_spinner_throttle = 0;
        }
        // #787: accumulate any tick-source redraw request and gate the
        // actual Reaction::Redraw to at most CONTENT_REDRAW_MIN apart
        // (≈15 fps).  `redraw_pending` stays true until a frame fires, so
        // the trailing update always paints within one interval.
        if needs_redraw {
            self.redraw_pending = true;
        }
        let (fire_now, new_last) = coalesce_redraw(
            self.redraw_pending,
            self.last_redraw_at,
            Instant::now(),
            CONTENT_REDRAW_MIN,
        );
        if fire_now {
            self.redraw_pending = false;
            self.last_redraw_at = new_last;
            Reaction::Redraw
        } else {
            Reaction::Continue
        }
    }
}


/// Estimate the number of visible rows in a `ListView` panel.
///
/// Deducts one row for the panel title strip.
pub(crate) fn content_visible_rows(panel: Rect, lh: f32) -> usize {
    if lh <= 0.0 {
        return 10;
    }
    let content_h = (panel.height - lh).max(0.0); // minus list title row
    (content_h / lh) as usize
}

/// #316 Phase B: scan raw SSE log text for the first `TITLE: …` / `---` /
/// body block emitted by a `new-issue-chat` worker.  The worker is
/// instructed to produce exactly this format so the TUI can file the issue.
///
/// Returns `(title, body)` when found, or `None` when no proposal exists yet.
pub(crate) fn parse_issue_proposal(text: &str) -> Option<(String, String)> {
    // Extract JSON "text" content from stream-json lines, then scan for TITLE:.
    let mut full_text = String::new();
    for line in text.lines() {
        if let Some(extracted) = extract_json_text_content(line) {
            full_text.push_str(&extracted);
            full_text.push('\n');
        }
    }
    // Find the LAST TITLE: line — the worker may have drafted multiple
    // proposals during the conversation; the most recent one is what
    // the developer wants to file.  We index by line position (not just
    // pattern match) so the body comes from the same proposal block as
    // the title — fixing a bug where a `skip_while` on the same prefix
    // would skip to the FIRST `TITLE:` and mismatch title vs body when
    // multiple proposals exist in the transcript.
    let title_prefix = "TITLE:";
    let lines: Vec<&str> = full_text.lines().collect();
    let title_idx = lines
        .iter()
        .rposition(|l| l.trim_start().starts_with(title_prefix))?;
    let title = lines[title_idx]
        .trim_start()
        .trim_start_matches(title_prefix)
        .trim()
        .to_string();
    if title.is_empty() {
        return None;
    }
    // Body is everything after the first `---` separator that follows the
    // *selected* TITLE: line (i.e. starts looking from title_idx + 1).
    let after_title = &lines[title_idx + 1..];
    let sep_idx = after_title.iter().position(|l| l.trim() == "---")?;
    let body_lines = &after_title[sep_idx + 1..];
    let body = body_lines.join("\n").trim().to_string();
    Some((title, body))
}

/// Extract the text content from a single stream-json line (type=text events).
pub(crate) fn extract_json_text_content(line: &str) -> Option<String> {
    // Lines look like: {"type":"content_block_delta","delta":{"type":"text_delta","text":"…"}}
    // or {"type":"text","value":"…"} depending on streaming mode.
    // Use a simple substring search rather than full JSON parsing.
    if !line.contains("\"text\"") {
        return None;
    }
    // Try to find `"text":"<value>"` pattern.
    let needle = "\"text\":\"";
    let start = line.find(needle)? + needle.len();
    let rest = &line[start..];
    // Collect until unescaped closing quote.
    let mut out = String::new();
    let mut chars = rest.chars().peekable();
    loop {
        match chars.next() {
            None | Some('"') => break,
            Some('\\') => match chars.next() {
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some(c) => out.push(c),
                None => break,
            },
            Some(c) => out.push(c),
        }
    }
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}

/// Shrink a `Rect` inward on all sides by `margin` (clamped to zero).
pub(crate) fn shrink_rect(r: Rect, margin: f32) -> Rect {
    let m = margin.min(r.width / 2.0).min(r.height / 2.0).max(0.0);
    Rect::new(
        r.x + m,
        r.y + m,
        (r.width - 2.0 * m).max(0.0),
        (r.height - 2.0 * m).max(0.0),
    )
}

/// #264: Build a chat transcript from a pool entry — merges the
/// developer-typed user turns (`inject_transcript`) with the assistant
/// turns parsed from the SSE stream-json lines, in chronological order.
///
/// Tool calls / system events are excluded — those belong in the Log tab,
/// not the chat.
///
/// For refinement chats the transcript opens with a System turn that
/// explains what the assistant was seeded with so the worker's first
/// reply (a clarifying question about the seeded issue) doesn't read as
/// a message-from-nowhere.
///
/// Ordering rule:
///   * Each user turn carries an `sse_offset_at_send` (its position in
///     `ctx.sse.lines` at submit time).
///   * Each assistant text event has an implicit offset = its index in
///     `ctx.sse.lines`.
///   * Walk both lists in parallel: emit user turns whose offset ≤ the
///     next assistant index, then advance through assistants.  Ties
///     prefer user (the user submitted *just before* the next assistant
///     turn arrived).
///
/// Assistant text events are split on `\n` so multi-line replies (numbered
/// lists, paragraph breaks) render as separate rows in the chat —
/// quadraui's `MessageList` doesn't honour embedded newlines inside a
/// single `StyledText`, so each line has to be its own `ChatTurn`.
/// #319 Phase A: extract the `text` field of a stream-json `assistant`
/// event with `\n` escapes preserved as real newlines.  Mirrors
/// [`extract_text_block`] but uses a newline-preserving unescaper —
/// the shared [`json_str`] helper converts `\n` to spaces so the Log
/// tab can render a multi-line block on one row (#302), which would
/// otherwise flatten markdown structure when we post the body to
/// GitHub.  Returns the empty string if no `text` block is found.
pub(crate) fn extract_text_block_keep_newlines(json: &str) -> String {
    let marker = "\"type\":\"text\"";
    let pos = match json.find(marker) {
        Some(p) => p,
        None => return String::new(),
    };
    let after = &json[pos + marker.len()..];
    let key = "\"text\":\"";
    let start = match after.find(key) {
        Some(p) => p + key.len(),
        None => return String::new(),
    };
    let rest = &after[start..];
    let mut out = String::new();
    let mut chars = rest.chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => break,
            '\\' => match chars.next() {
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some('r') => {}
                Some('"') => out.push('"'),
                Some('\\') => out.push('\\'),
                Some(other) => {
                    out.push('\\');
                    out.push(other);
                }
                None => out.push('\\'),
            },
            c => out.push(c),
        }
    }
    out
}

// ─── Readable log rendering (#385) ───────────────────────────────────────────

/// Parse one stream-json event line into zero or more displayable `ListItem`s
/// using a readable, human-friendly format (#385):
///
/// * `assistant` turns: compact `Turn N  +Xs` header on its own line, then
///   the assistant's prose wrapped to `wrap_width` columns (2-space indent),
///   STATUS:/STUCK: lines remain as single coloured rows.
/// * `tool_use` events: `  → Name: detail` (arrow prefix, no `[tool]` noise).
/// * `system(init)`, `result`, `rate_limit_event`: unchanged compact lines.
///
/// `turn_n` is incremented for each `assistant` event (same counter as the
/// old renderer — the two renderers are not mixed, so the counters stay in
/// sync independently).  `wrap_width == 0` disables wrapping (lines are
/// emitted as-is up to the text block length).
pub(crate) fn parse_json_events_readable(
    line: &str,
    turn_n: &mut usize,
    elapsed: Option<std::time::Duration>,
    wrap_width: usize,
) -> Vec<ListItem> {
    let type_val = match json_str(line, "type") {
        Some(t) => t,
        None => {
            // #1954: a line with no JSON "type" field isn't necessarily
            // noise — it's the `# agent=… repo=… argv=…` header every log
            // starts with, or an `[sse error] …` diagnostic the watch pool
            // injects when the stream itself fails (see
            // `settings_ui::drain_pool_entry`). Silently dropping it here
            // used to destroy the only evidence of *why* a stream died.
            // Render it verbatim instead — skip only genuinely blank lines.
            let trimmed = line.trim();
            if trimmed.is_empty() {
                return Vec::new();
            }
            let color = if trimmed.starts_with("[sse error]") {
                Color::rgb(220, 100, 100)
            } else {
                Color::rgb(140, 140, 140)
            };
            return vec![activity_item(trimmed, color)];
        }
    };

    match type_val.as_str() {
        "system" => {
            let subtype = json_str(line, "subtype").unwrap_or_default();
            if subtype == "init" {
                let model = json_str(line, "model").unwrap_or_else(|| "?".to_string());
                return vec![activity_item(
                    &format!("[init] {}", model),
                    Color::rgb(100, 100, 180),
                )];
            }
            Vec::new()
        }

        "assistant" => {
            *turn_n += 1;
            let n = *turn_n;

            // STATUS: / STUCK: — single special line, same colours as before.
            let text_flat = extract_text_block(line);
            if let Some(idx) = text_flat.find("STATUS:") {
                let rest = &text_flat[idx..];
                let end = rest.find('\n').unwrap_or(rest.len());
                let trimmed = rest[..end].trim();
                return vec![activity_item(trimmed, Color::rgb(80, 210, 80))];
            }
            if let Some(idx) = text_flat.find("STUCK:") {
                let rest = &text_flat[idx..];
                let end = rest.find('\n').unwrap_or(rest.len());
                let trimmed = rest[..end].trim();
                return vec![activity_item(trimmed, Color::rgb(220, 120, 50))];
            }

            let elapsed_str = match elapsed {
                Some(d) if d.as_secs() >= 1 => format!("  +{}s", d.as_secs()),
                _ => String::new(),
            };

            // Readable text — preserve newlines so paragraph structure survives.
            let text_nl = extract_text_block_keep_newlines(line);

            // No prose text block — could be thinking-only, tool-only, or both.
            if text_nl.trim().is_empty() {
                let calls = extract_tool_calls(line);
                if calls.is_empty() {
                    // Thinking-only (or completely empty) turn: header on its own
                    // line, then thinking text wrapped on separate line(s) so it
                    // isn't glued inline to "Turn N".
                    let thinking = collapse_ws(&extract_thinking_block(line));
                    let header = format!("  Turn {}{}", n, elapsed_str);
                    if thinking.is_empty() {
                        return vec![activity_item(&header, Color::rgb(80, 80, 100))];
                    }
                    let mut items = vec![activity_item(&header, Color::rgb(80, 80, 100))];
                    let think_inner_wrap = if wrap_width > 2 { wrap_width - 2 } else { 0 };
                    for wl in word_wrap(&format!("💭 {}", thinking), think_inner_wrap) {
                        items.push(activity_item(
                            &format!("  {}", wl),
                            Color::rgb(130, 110, 150),
                        ));
                    }
                    return items;
                }
                // Tool-only turn: one arrow line per tool call.
                // Drop the bare "Turn N" header — each tool line carries its own
                // content so the header is pure noise (#issue items 1-2).
                let mut items = Vec::new();
                for (call_name, call_detail) in &calls {
                    let prefix = if call_detail.is_empty() {
                        format!("  \u{2192} {}", call_name) // →
                    } else {
                        format!("  \u{2192} {}: {}", call_name, call_detail)
                    };
                    let display = if wrap_width > 0 && prefix.chars().count() > wrap_width {
                        let cut = prefix
                            .char_indices()
                            .nth(wrap_width.saturating_sub(1))
                            .map(|(i, _)| i)
                            .unwrap_or(prefix.len());
                        format!("{}…", &prefix[..cut])
                    } else {
                        prefix
                    };
                    items.push(activity_item(&display, Color::rgb(160, 130, 200)));
                }
                return items;
            }

            // Normal turn: dim header + wrapped prose + any tool calls.
            let indent = "  ";
            let prose_wrap = if wrap_width > indent.len() {
                wrap_width - indent.len()
            } else {
                0
            };
            let mut items = Vec::new();
            let header = format!("  Turn {}{}", n, elapsed_str);
            items.push(activity_item(&header, Color::rgb(80, 80, 100)));
            for wrapped_line in word_wrap(text_nl.trim_end(), prose_wrap) {
                let display = format!("{}{}", indent, wrapped_line);
                items.push(activity_item(&display, Color::rgb(200, 210, 230)));
            }
            // Mixed-content turn: also emit one arrow line per tool call in
            // the same turn (e.g. "I'll read that file" + Read in one turn).
            for (call_name, call_detail) in extract_tool_calls(line) {
                let prefix = if call_detail.is_empty() {
                    format!("  \u{2192} {}", call_name) // →
                } else {
                    format!("  \u{2192} {}: {}", call_name, call_detail)
                };
                let display = if wrap_width > 0 && prefix.chars().count() > wrap_width {
                    let cut = prefix
                        .char_indices()
                        .nth(wrap_width.saturating_sub(1))
                        .map(|(i, _)| i)
                        .unwrap_or(prefix.len());
                    format!("{}…", &prefix[..cut])
                } else {
                    prefix
                };
                items.push(activity_item(&display, Color::rgb(160, 130, 200)));
            }
            items
        }

        "tool_use" => {
            let name = json_str(line, "name").unwrap_or_else(|| "?".to_string());
            let detail = tool_detail(&name, line);
            // Compact single line: "  → Bash: <cmd>" or "  → Read: <path>"
            // Wrap the detail if it's very long, but keep the arrow prefix.
            let prefix = if detail.is_empty() {
                format!("  \u{2192} {}", name) // →
            } else {
                format!("  \u{2192} {}: {}", name, detail)
            };
            // For very long bash commands, truncate at wrap_width with ellipsis
            // so the arrow line stays on screen without horizontal scrolling.
            let display = if wrap_width > 0 && prefix.chars().count() > wrap_width {
                let cut = prefix
                    .char_indices()
                    .nth(wrap_width.saturating_sub(1))
                    .map(|(i, _)| i)
                    .unwrap_or(prefix.len());
                format!("{}…", &prefix[..cut])
            } else {
                prefix
            };
            vec![activity_item(&display, Color::rgb(160, 130, 200))]
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
            vec![activity_item(&text, Color::rgb(200, 200, 100))]
        }

        "rate_limit_event" => vec![activity_item("[rate_limit]", Color::rgb(220, 150, 50))],

        _ => Vec::new(),
    }
}

/// Pre-compute per-turn timing anchors from embedded `user.timestamp` fields.
///
/// Scans `lines` and returns one entry per `"type":"assistant"` event found:
/// the epoch-seconds of the **last** `"type":"user"` event seen *before* that
/// assistant turn.  Entries are `None` when no user event preceded the turn
/// (i.e. the very first turn, or a turn reached without any tool-result in
/// between).
///
/// `user` events in the `claude -p --output-format stream-json` protocol carry
/// an ISO-8601 `"timestamp"` field recording when the tool result was submitted.
/// Because these timestamps are **embedded in the log content** rather than
/// derived from reception time, they survive byte-0 SSE replay intact — all
/// historical lines arrive in one burst sharing a single `Instant::now()`, but
/// the JSON timestamps inside each line are real wall-clock values.
///
/// Using these timestamps to drive the `+Ns` inter-turn display (instead of
/// arrival `Instant`s) fixes the #309 regression where replayed logs showed
/// every delta as 0 and suppressed all timing labels.
pub(crate) fn user_epoch_per_turn<'a>(lines: impl Iterator<Item = &'a str>) -> Vec<Option<f64>> {
    let mut result: Vec<Option<f64>> = Vec::new();
    let mut last_user_epoch: Option<f64> = None;
    for line in lines {
        match json_str(line, "type").as_deref() {
            Some("user") => {
                if let Some(ts) = json_str(line, "timestamp") {
                    last_user_epoch = parse_iso8601_to_epoch(&ts);
                }
            }
            Some("assistant") => {
                result.push(last_user_epoch);
            }
            _ => {}
        }
    }
    result
}

/// Compute the inter-turn `Duration` for assistant turn `idx` using the
/// pre-computed `user_epochs` table from [`user_epoch_per_turn`].
///
/// Returns `Some(d)` only when both this turn and the previous turn have a
/// recorded user-event epoch AND the difference is ≥ 1 s (same suppression
/// threshold used for arrival-`Instant` timing).  Returns `None` otherwise —
/// callers fall back to arrival-`Instant` deltas in that case.
pub(crate) fn user_epoch_elapsed(user_epochs: &[Option<f64>], idx: usize) -> Option<std::time::Duration> {
    if idx == 0 {
        return None;
    }
    let this_epoch = (*user_epochs.get(idx)?)?;
    let prev_epoch = (*user_epochs.get(idx - 1)?)?;
    if this_epoch > prev_epoch {
        let secs = (this_epoch - prev_epoch) as u64;
        if secs >= 1 {
            return Some(std::time::Duration::from_secs(secs));
        }
    }
    None
}

/// #787: decide whether to emit a tick-driven redraw this frame.
///
/// Returns `(fire_now, new_last_redraw_at)`:
/// - `(true, now)` when `pending` is set and at least `min` has elapsed since `last`.
/// - `(false, last)` otherwise (caller should leave `last_redraw_at` unchanged).
///
/// Kept pure (no `Instant::now()` call inside) so unit tests can inject
/// controlled `Instant` values without real-time delays.
pub(crate) fn coalesce_redraw(
    pending: bool,
    last: Instant,
    now: Instant,
    min: Duration,
) -> (bool, Instant) {
    if pending && now.duration_since(last) >= min {
        (true, now)
    } else {
        (false, last)
    }
}

pub(crate) fn pipeline_view_for_render(view: &QuiPipelineView) -> QuiPipelineView {
    QuiPipelineView {
        id: view.id.clone(),
        stages: view
            .stages
            .iter()
            .map(|s| QuiPipelineStage {
                label: s.label.clone(),
                status: s.status.clone(),
                action: None,
            })
            .collect(),
        focused_stage: view.focused_stage,
    }
}

/// #303: Height of the pipeline button bar strip above the stage row when
/// any stage has a dispatchable action.  Zero when the bar is empty (no
/// vertical space stolen from the stage row in that case).
pub(crate) fn pipeline_action_bar_height(has_button: bool, lh: f32) -> f32 {
    if has_button {
        lh * 1.5
    } else {
        0.0
    }
}

/// Carve out the rect used by the PipelineView primitive at the top of the
/// Pipeline detail pane.  Reserves 6 rows by default (icon row + label row
/// + action row + 1 row of padding/border), clamped to ≤ 55 % of the
/// available height so the issue summary below remains visible.
pub(crate) fn pipeline_detail_pv_rect(main: Rect, lh: f32) -> Rect {
    if lh <= 0.0 {
        return Rect::new(main.x, main.y, main.width, 0.0);
    }
    let want_rows = 6.0_f32;
    let max_h = (main.height * 0.55).max(lh);
    let h = (want_rows * lh).min(max_h);
    Rect::new(main.x, main.y, main.width, h)
}

/// #818: Compact variant of [`pipeline_detail_pv_rect`] used for the
/// universal pinned stage strip shown on every non-Overview tab.
///
/// The TUI `PipelineView` rasteriser (`quadraui::tui::pipeline_view`)
/// reserves 1 row above the boxes for the keyboard-focus caret, then draws
/// a bordered box needing at least 4 rows itself (top border / status icon
/// / label / bottom border) to show the checkmark-or-dot status icon and
/// stage label inside the border — anything shorter collapses the box to
/// just its two border rows stacked with no content, which reads as a
/// flat line instead of a box. 5 rows (1 caret + 4 box) is the minimum
/// that reproduces the same boxed look as the full-size widget on the
/// Overview tab, just without room for the two-line label or the action
/// row. Found via #818 fix-iteration-1 smoke: the previous 3-row strip
/// rendered as a flat bar because the box had no room for its interior.
pub(crate) fn pipeline_detail_pv_rect_strip(main: Rect, lh: f32) -> Rect {
    if lh <= 0.0 {
        return Rect::new(main.x, main.y, main.width, 0.0);
    }
    let want_rows = 5.0_f32;
    let max_h = (main.height * 0.40).max(lh);
    let h = (want_rows * lh).min(max_h);
    Rect::new(main.x, main.y, main.width, h)
}

/// Render an issue's GitHub body as a ListView. Shared between the Pipeline
/// view's Issue tab and the Board view's Issue tab so the rendering and
/// scroll handling stay in lock-step.
///
/// `issue` is `Some((number, title, body, labels))` for the selected issue,
/// or `None` when no issue is selected (renders a placeholder).
///
/// `width` is the content-area width in backend units (character columns for
/// TUI, pixels for GTK).  Passed to `render_markdown_to_styled_wrapped` so
/// long body lines are word-wrapped to the panel width (#669); fenced code
/// blocks are never wrapped.  Pass 0 to disable wrapping.
///
/// `show_v_scrollbar` is per-call rather than a hardcoded `false` so #2017
/// can give the Queue panel's detail pane a real scrollbar without flipping
/// it on for Board and Pipeline too — this helper is shared across all
/// three, and `ListView::show_v_scrollbar` has no other way to differ per
/// caller. Board and Pipeline pass `false` (unchanged); only Queue passes
/// `true`.
///
/// `repo` names the row's repo in full in the header line, e.g.
/// `claude-coordinator #1835`. `None` keeps the header as plain `#1835`
/// (Board and Pipeline: both are already scoped to one repo via their own
/// selector, so restating it here would be noise). #2042: Queue's grid
/// shortens the `Issue`/`After` cells to a repo alias, so its detail pane —
/// the cheapest place available — is where the full name has to stay
/// discoverable; only Queue passes `Some`.
pub(crate) fn issue_body_list(
    issue: Option<(u64, &str, &str, &[String])>,
    scroll_offset: usize,
    widget_id: &'static str,
    width: usize,
    show_v_scrollbar: bool,
    repo: Option<&str>,
) -> ListView {
    let mut items: Vec<ListItem> = Vec::new();
    match issue {
        None => {
            items.push(kv_item(
                "",
                " No issue selected",
                Some(Color::rgb(100, 100, 100)),
            ));
        }
        Some((number, title, body, labels)) => {
            let number_span = match repo {
                Some(repo) => format!(" {repo} #{number}"),
                None => format!(" #{number}"),
            };
            items.push(ListItem {
                text: StyledText {
                    spans: vec![
                        StyledSpan::with_fg(number_span, Color::rgb(150, 150, 240)),
                        StyledSpan::with_fg(format!("  {}", title), Color::rgb(230, 230, 255)),
                    ],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });
            if !labels.is_empty() {
                items.push(kv_item(
                    "",
                    &format!(" labels: {}", labels.join(", ")),
                    Some(Color::rgb(160, 160, 180)),
                ));
            }
            items.push(kv_item("", "", None));
            if body.is_empty() {
                items.push(kv_item(
                    "",
                    " (no description)",
                    Some(Color::rgb(100, 100, 100)),
                ));
            } else {
                // #669: render through `render_markdown_to_styled_wrapped` so
                // long body lines are wrapped to the panel width.  Fenced code
                // blocks are never wrapped (quadraui guarantees that).  When
                // width == 0 the function falls back to the unwrapped path.
                // #372-pattern: headings, bold, italic, inline code, lists,
                // blockquotes, and fenced code blocks are styled.
                // TODO(#217): pass active_theme once issue_body_list accepts a theme
                // parameter; for now the quadraui dark default is a reasonable fallback.
                let md_theme = quadraui::Theme::default();
                let rendered =
                    quadraui::render_markdown_to_styled_wrapped(body, &md_theme, width);
                for md_line in rendered.lines {
                    items.push(ListItem {
                        text: md_line,
                        icon: None,
                        detail: None,
                        decoration: Decoration::Normal,
                    });
                }
            }
        }
    }
    ListView {
        id: WidgetId::new(widget_id),
        title: None,
        items,
        selected_idx: 0,
        scroll_offset,
        has_focus: false,
        bordered: false,
        h_scroll: 0,
        max_content_width: None,
        show_v_scrollbar,
    }
}

// ─── Pipeline display methods ─────────────────────────────────────────────────

impl CoordApp {

    /// #790: one-line copy-mode hint strip painted at the bottom of the
    /// terminal pane.  Advertises the F9 keyboard toggle — Shift+drag is
    /// intercepted by an outer tmux, so a key is the only tmux-proof way to
    /// select-and-copy pane text — and, while copy mode is active, the
    /// in-mode controls.
    pub(crate) fn terminal_copy_hint_list(&self) -> ListView {
        let (msg, color) = if self.terminal_copy_mode {
            (
                "  ● COPY MODE — drag to select · Ctrl+C copy · Esc/F9 exit".to_string(),
                Color::rgb(240, 200, 90),
            )
        } else {
            (
                "  F9 copy-mode: drag-select text out of the pane (tmux-safe)".to_string(),
                Color::rgb(130, 130, 130),
            )
        };
        ListView {
            id: WidgetId::new("terminal-copy-hint"),
            title: None,
            items: vec![activity_item(&msg, color)],
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Pipeline panel detail-side: list-style fallback when no PipelineView
    /// can be drawn yet (no issue selected / still loading).
    pub(crate) fn pipeline_placeholder_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        if self.pending_data.is_some() && self.pipeline_issues.is_empty() {
            items.push(kv_item(
                "",
                "  Loading tracked issues…",
                Some(Color::rgb(180, 180, 100)),
            ));
        } else if self.pipeline_issues.is_empty() {
            let labels = self.data.pipeline_tracked_labels.join(", ");
            items.push(kv_item(
                "",
                &format!(
                    "  No issues found with label(s): {}",
                    if labels.is_empty() {
                        "(none)".into()
                    } else {
                        labels
                    }
                ),
                Some(Color::rgb(140, 140, 140)),
            ));
            items.push(kv_item(
                "",
                "  Press 'r' to refresh, or label issues with 'coord' on GitHub.",
                Some(Color::rgb(100, 100, 100)),
            ));
        } else if let Some(issue) = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i)) {
            // An issue is selected but the widget was suppressed (closed-no-pipeline).
            if issue.is_closed && !self.issue_has_any_assignment(issue) {
                items.push(kv_item(
                    "",
                    "  Closed without coord pipeline — no stages tracked.",
                    Some(Color::rgb(120, 180, 120)),
                ));
            } else {
                items.push(kv_item(
                    "",
                    "  Select an issue on the left to see its pipeline.",
                    Some(Color::rgb(140, 140, 140)),
                ));
            }
        } else {
            items.push(kv_item(
                "",
                "  Select an issue on the left to see its pipeline.",
                Some(Color::rgb(140, 140, 140)),
            ));
        }
        ListView {
            id: WidgetId::new("pipeline-empty"),
            title: Some(StyledText::plain(" PIPELINE ")),
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

    // ─── #2288 (ms-65 §9): side-by-side split ────────────────────────────

    /// The `║` (U+2551) pane divider §9 pins, painted at exactly the cell
    /// `SplitTreeDivider::cell_position` resolves — the same conversion
    /// `board_split_divider_hit` (events.rs) hit-tests against, which is
    /// the structural fix quadraui's `SplitTree` docs prescribe for the
    /// paint/click drift bug class (vimcode #582/#452).
    ///
    /// quadraui's own `draw_split_tree` rasteriser is deliberately NOT used
    /// here: it paints `│` (U+2502), the same glyph this app already paints
    /// at column 2 for the sidebar/main boundary, and §9 pins a distinct
    /// `║` precisely so a pane divider is unambiguous on a symbols-only
    /// grid. Only the *painting* differs — the geometry still comes from
    /// `SplitTree::layout` via `Backend::split_tree_layout`, never from
    /// hand-rolled math.
    ///
    /// A no-op for an unsplit scope, whose tree is a bare `Leaf` and so has
    /// no dividers at all.
    fn paint_board_pane_dividers(&self, backend: &mut dyn Backend, layout: &SplitTreeLayout) {
        for div in &layout.dividers {
            let rows = div.cross_size.max(0.0) as usize;
            if rows == 0 {
                continue;
            }
            let display = TextDisplay {
                id: WidgetId::new("board-pane-divider"),
                lines: (0..rows)
                    .map(|_| TextDisplayLine {
                        spans: vec![StyledSpan::plain(PANE_DIVIDER_CHAR.to_string())],
                        decoration: Decoration::Muted,
                        timestamp: None,
                    })
                    .collect(),
                scroll_offset: 0,
                // The divider is exactly as tall as its rect, so there is
                // nothing to scroll — but `auto_scroll` defaults to `true`
                // and would let the rasteriser re-pin the view, so it is
                // switched off explicitly rather than left to luck.
                auto_scroll: false,
                max_lines: 0,
                has_focus: false,
                title: None,
                show_scrollbar: false,
            };
            backend.draw_text_display(
                Rect::new(
                    div.cell_position() as f32,
                    div.cross_start,
                    div.thickness,
                    div.cross_size,
                ),
                &display,
            );
        }
    }

    /// Paint ONE Board pane into `rect` — the doc-tab strip, the
    /// `Board / Issue / Chat / Terminal` sub-tab bar and the active
    /// sub-tab's content, in that order (contract §2a).
    ///
    /// #2288: this is verbatim the body the Board arm of `render_content`
    /// used to inline, with `m` replaced by the pane's own rect. Which
    /// pane's tabs it reads is carried by `render_pane`, which the caller
    /// sets around this call — see `CoordApp::board_render_pane`. With one
    /// pane the caller passes the full panel rect and nothing about the
    /// output changes.
    fn render_board_pane(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        // `#464`: route through `detail_tab_bar_height` so render and
        // hit-test agree on the cell boundary in the TUI backend.
        let tab_h = detail_tab_bar_height(lh);
        // #2282 (ms-65 §2a): the document tab strip is a NEW row
        // inserted between the panel toolbar and the existing
        // `Board / Issue / Chat / Terminal` sub-tab bar — two tab rows,
        // in that order, pinned by the contract. With zero documents
        // open `board_doc_tab_strip` is `None`: the strip renders
        // nothing and reserves no row, so the sub-tab bar sits exactly
        // where it did pre-ms-65 (§4/#2283: also the close-the-last-tab
        // empty state).
        //
        // #2283 (ms-65 §4): `board_doc_tab_strip` is the single place
        // that resolves `scroll_offset` AND bakes in the `‹`/`›`
        // overflow affordances — the click hit-test
        // (`events.rs::mouse_main_click`) builds the exact same
        // `TabBar` from the exact same call so a click column can
        // never disagree with what got painted here.
        let m = match self.board_doc_tab_strip(rect.width) {
            Some(strip) => {
                let strip_rect = Rect::new(rect.x, rect.y, rect.width, tab_h);
                backend.draw_tab_bar(strip_rect, &strip, None);
                Rect::new(
                    rect.x,
                    rect.y + tab_h,
                    rect.width,
                    (rect.height - tab_h).max(0.0),
                )
            }
            None => rect,
        };
        // Sub-tab bar (Board / Issue / Chat / Terminal), then the
        // active sub-tab's content.
        let tab_bar = self.board_detail_tab_bar();
        let tab_rect = Rect::new(m.x, m.y, m.width, tab_h);
        let content_rect = Rect::new(m.x, m.y + tab_h, m.width, (m.height - tab_h).max(0.0));
        backend.draw_tab_bar(tab_rect, &tab_bar, None);
        match self.board_pane_detail_tab() {
            BoardDetailTab::Board => {
                backend.draw_list(content_rect, &self.detail_list());
            }
            BoardDetailTab::Issue => {
                // #669: stash content width so board_issue_body_list can
                // word-wrap long lines to the viewport.
                //
                // #2288 (ms-65 §9): stashed PER PANE, not in the shared
                // `last_issue_panel_cols` cell. Two panes are painted in one
                // pass and each is roughly half the panel wide, so a shared
                // cell would leave the second-painted pane's width standing
                // for both — wrong wrap for the first pane's body here, and
                // (because the cell outlives the frame) a wrong scroll clamp
                // in `mouse_main_scroll`, which re-derives `items.len()`
                // from it after the paint.
                self.stash_board_pane_issue_cols(content_rect.width as usize);
                backend.draw_list(content_rect, &self.board_issue_body_list());
            }
            // #316: Chat tab — empty state CTA or live board chat.
            BoardDetailTab::Chat => {
                self.render_board_chat_tab(backend, content_rect);
            }
            // #675: Terminal tab — per-issue interactive shell, mirrors
            // PipelineDetailTab::Terminal rendering.
            BoardDetailTab::Terminal => {
                self.render_detail_terminal_tab(backend, content_rect);
            }
        }
        // #316 Phase B: file-issue modal renders on top of the Chat tab.
        //
        // #2288 review (blocking, carried from round 2): `render_board_pane`
        // now runs ONCE PER PANE, so an ungated overlay painted itself into
        // every pane — with a split panel and the modal open, the user saw
        // two copies, each squeezed into half the panel and the second one
        // hanging over content that has nothing to do with it. Only the
        // FOCUSED pane can have opened the modal (`open_file_issue_modal`
        // runs off that pane's Chat tab), so only that pane's pass paints it.
        // Unsplit, `board_render_pane()` is the focused pane by definition
        // and this is the pre-split behaviour verbatim.
        //
        // The tab test reads through `board_pane_detail_tab()` rather than
        // the raw live field for the same reason every other reader in this
        // function does — inside the focused pane's pass the two are the same
        // value, and going through the accessor keeps "which pane am I
        // painting" the single question this function asks.
        if self.board_render_pane() == self.board_panes().focused_index()
            && self.board_pane_detail_tab() == BoardDetailTab::Chat
            && self.file_issue_modal.is_some()
        {
            self.render_file_issue_modal(backend, m);
        }
    }

    /// #2282 (ms-65 §2): the Board panel's **document** tab strip — one tab per
    /// open issue, at most one of them a preview.
    ///
    /// `None` when zero documents are open, which is how §2a's "the strip
    /// renders nothing and reserves no row" is expressed: the caller skips the
    /// row entirely rather than painting an empty bar.
    ///
    /// Every tab's whole rendered form — brackets, close glyph and the trailing
    /// separator — lives in `TabItem::label`; see [`doc_tab_label`] for why the
    /// close glyph cannot come from `TabBar::show_tab_close`. `is_preview` is
    /// still set so quadraui paints the preview tab italic (contract §1); the
    /// `∘ ` marker inside the label is the symbols-only stand-in for that
    /// styling, not a replacement for it.
    ///
    /// #2288 (§9): reports the **currently-addressed pane** — the one the
    /// render loop is painting (`board_render_pane`), or the focused pane
    /// outside a paint. See [`Self::board_doc_tab_bar_for_pane`].
    #[cfg(test)]
    pub(crate) fn board_doc_tab_bar(&self) -> Option<TabBar> {
        self.board_doc_tab_bar_for_pane(self.board_render_pane())
    }

    /// #2288 (ms-65 §9): [`Self::board_doc_tab_bar`] for one explicit pane.
    ///
    /// Each pane owns its own strip, so the split state changes two things
    /// here and nothing else: which [`doc_tabs::DocTabGroup`] the tabs come
    /// from, and the label budget — §9 pins a narrower 14 (16 with the §1
    /// preview marker) for a split pane, against §2b's 20/22 for an
    /// undivided one.
    pub(crate) fn board_doc_tab_bar_for_pane(&self, pane: usize) -> Option<TabBar> {
        let panes = self.doc_tabs.panes(PanelScope::Board);
        let group = panes.pane(pane);
        let max_cols = if panes.is_split() {
            SPLIT_DOC_TAB_LABEL_COLS
        } else {
            DOC_TAB_LABEL_COLS
        };
        if group.is_empty() {
            return None;
        }
        // Labels stay bare `#<N> <title>` while every open document belongs to
        // the same repo; the moment the set spans two, every tab carries its
        // repo so the numbers can't be confused across repos.
        let show_repo = group
            .tabs()
            .first()
            .map(|(first, _)| group.tabs().iter().any(|(repo, _)| repo != first))
            .unwrap_or(false);
        let tabs: Vec<TabItem> = group
            .tabs()
            .iter()
            .enumerate()
            .map(|(idx, (repo, number))| {
                // An issue that has left the board (purged, closed and pruned)
                // still has a tab until #2283 lands close semantics — render
                // its number with an empty title rather than dropping the tab
                // and silently renumbering the strip under the user's cursor.
                let title = self
                    .board_issue_group_for(repo, *number)
                    .map(|g| g.issue_title.clone())
                    .unwrap_or_default();
                let is_preview = group.is_preview(idx);
                let is_active = group.active_index() == Some(idx);
                TabItem {
                    label: doc_tab_label(
                        repo, *number, &title, show_repo, is_preview, is_active, max_cols,
                    ),
                    is_active,
                    is_dirty: false,
                    is_preview,
                    is_closable: true,
                }
            })
            .collect();
        Some(TabBar {
            id: WidgetId::new("board-doc-tabs"),
            tabs,
            right_segments: Vec::new(),
            active_accent: None,
            scroll_offset: 0,
            // The close glyph is painted as part of the label (§2c needs the
            // active tab's `]` to land to the RIGHT of the `×`, which the
            // rasteriser's own close-button slot cannot express).
            show_tab_close: false,
            compact: false,
        })
    }

    /// The doc-tab strip's rendered labels, unmutated by the §4 (#2283)
    /// overflow-arrow bake-in — in strip order. Empty when no strip is
    /// rendered.
    ///
    /// Test-only: the click hit-test now builds from
    /// [`Self::board_doc_tab_strip`] instead (its `scroll_offset` and
    /// `‹`/`›` markers must agree with what's painted), but this plain
    /// accessor is still the simplest fixture for tests asserting on raw
    /// label content (e.g. the repo-prefix rule) without wading through
    /// arrow bake-in.
    #[cfg(test)]
    pub(crate) fn board_doc_tab_labels(&self) -> Vec<String> {
        self.board_doc_tab_bar()
            .map(|bar| bar.tabs.into_iter().map(|t| t.label).collect())
            .unwrap_or_default()
    }

    /// #2283 (ms-65 §4): the Board doc-tab strip **as it will actually
    /// paint** at `width` columns — `scroll_offset` resolved and the
    /// `‹`/`›` overflow affordances baked in.
    ///
    /// Both the paint path (`render_content` above) and the click hit-test
    /// (`events.rs::mouse_main_click`, `handle_mouse`'s middle-click arm)
    /// build the `TabBar` they measure against from THIS function — never
    /// independently — so a click column can never resolve to a different
    /// tab than the one actually painted there.
    ///
    /// # Why the arrows are baked into label text, not painted by quadraui
    ///
    /// `TabBar::layout` (the primitive) DOES compute `scroll_left`/
    /// `scroll_right` authoritatively given a nonzero `scroll_arrow_width` —
    /// that's the part reused below, not reimplemented. But nothing
    /// downstream paints the glyph for TUI: `TuiBackend::draw_tab_bar` /
    /// `tab_bar_layout` hardcode `scroll_arrow_width: 0.0` ("no scroll
    /// arrows in TUI") and simply honour whatever `scroll_offset` the
    /// caller supplies. So this bakes `‹`/`›` into the boundary visible
    /// tab's label, exactly the way [`doc_tab_label`] already bakes in the
    /// close glyph and the §2c active bracket (both for the same root
    /// reason: the close button can't land where §2c's bracket needs it to
    /// via `TabBar::show_tab_close`).
    ///
    /// The `scroll_arrow_width: 1.0` reservation (1 column each side,
    /// mirroring `TabBar::layout`'s own "reserve space for two, even if
    /// only one ends up shown" policy) guarantees the extra `‹`/`›`
    /// character never crowds its tab out of the width the real
    /// (unreserved) TUI paint has to work with — so the active tab's
    /// visibility guarantee from `TabBar::fit_active_scroll_offset`
    /// survives the label mutation below.
    ///
    /// `None` when zero documents are open (identical to
    /// [`Self::board_doc_tab_bar`]).
    pub(crate) fn board_doc_tab_strip(&self, width: f32) -> Option<TabBar> {
        self.board_doc_tab_strip_for_pane(self.board_render_pane(), width)
    }

    /// #2288 (ms-65 §9): [`Self::board_doc_tab_strip`] for one explicit
    /// pane, at that pane's own width.
    ///
    /// The split makes `width` genuinely per-pane — each half of the panel
    /// resolves its own `scroll_offset` and its own `‹`/`›` markers — so a
    /// click in the right pane is measured against the right pane's rect,
    /// never the whole panel's.
    pub(crate) fn board_doc_tab_strip_for_pane(&self, pane: usize, width: f32) -> Option<TabBar> {
        let mut bar = self.board_doc_tab_bar_for_pane(pane)?;
        Self::bake_doc_tab_overflow_markers(&mut bar, width);
        Some(bar)
    }

    /// #2284 (ms-65 §3): the Pipeline panel's **document** tab strip — the
    /// exact same model and label shape as [`Self::board_doc_tab_bar`]
    /// (contract §3's "same preview/pin semantics"), just sourced from the
    /// Pipeline scope's own [`doc_tabs::DocTabGroup`] and Pipeline's own
    /// issue cache (`pipeline_issues`) rather than the Board's.
    ///
    /// `None` when zero Pipeline documents are open — §3a's "Pipeline shows
    /// its baseline (no doc-tab strip row)" zero-tab control.
    pub(crate) fn pipeline_doc_tab_bar(&self) -> Option<TabBar> {
        let group = self.pipeline_doc_tabs();
        if group.is_empty() {
            return None;
        }
        let show_repo = group
            .tabs()
            .first()
            .map(|(first, _)| group.tabs().iter().any(|(repo, _)| repo != first))
            .unwrap_or(false);
        let tabs: Vec<TabItem> = group
            .tabs()
            .iter()
            .enumerate()
            .map(|(idx, (repo, number))| {
                // Mirrors board_doc_tab_bar: an issue that's left the tracked
                // set still gets a tab (no close semantics wired for
                // Pipeline in this slice — see the "#2284" note on
                // `pipeline_doc_tab_bar`'s callers), rendered with an empty
                // title rather than dropped.
                let title = self
                    .pipeline_issue_for(repo, *number)
                    .map(|pi| pi.title.clone())
                    .unwrap_or_default();
                let is_preview = group.is_preview(idx);
                let is_active = group.active_index() == Some(idx);
                TabItem {
                    label: doc_tab_label(
                        repo,
                        *number,
                        &title,
                        show_repo,
                        is_preview,
                        is_active,
                        // #2288 ships side-by-side splitting for the Board
                        // panel only (§9's mock is Board's), so Pipeline's
                        // strip always uses §2b's undivided budget.
                        DOC_TAB_LABEL_COLS,
                    ),
                    is_active,
                    is_dirty: false,
                    is_preview,
                    is_closable: true,
                }
            })
            .collect();
        Some(TabBar {
            id: WidgetId::new("pipeline-doc-tabs"),
            tabs,
            right_segments: Vec::new(),
            active_accent: None,
            scroll_offset: 0,
            show_tab_close: false,
            compact: false,
        })
    }

    /// The Pipeline doc-tab strip as it will actually paint at `width`
    /// columns — the Pipeline-scope counterpart of
    /// [`Self::board_doc_tab_strip`]; see that method's doc comment for why
    /// the overflow markers are baked into label text rather than painted by
    /// quadraui.
    pub(crate) fn pipeline_doc_tab_strip(&self, width: f32) -> Option<TabBar> {
        let mut bar = self.pipeline_doc_tab_bar()?;
        Self::bake_doc_tab_overflow_markers(&mut bar, width);
        Some(bar)
    }

    /// Shared by [`Self::board_doc_tab_strip`] and
    /// [`Self::pipeline_doc_tab_strip`]: resolve `bar`'s `scroll_offset` for
    /// `width` columns and bake the `‹`/`›` (§4, #2283) overflow markers into
    /// the boundary visible tab's label in place — see
    /// `Self::board_doc_tab_strip`'s doc comment for why this can't be a
    /// quadraui-painted affordance in the TUI backend. A no-op (beyond
    /// resolving `scroll_offset`, itself inert at `width <= 0.0`) when `bar`
    /// has no tabs.
    fn bake_doc_tab_overflow_markers(bar: &mut TabBar, width: f32) {
        if bar.tabs.is_empty() || width <= 0.0 {
            return;
        }
        let label_lens: Vec<f32> = bar
            .tabs
            .iter()
            .map(|t| t.label.chars().count() as f32)
            .collect();
        let layout = bar.layout(
            width,
            1.0,
            1.0,
            |i| TabMeasure::new(label_lens[i], 0.0),
            |_| SegmentMeasure::new(0.0),
        );
        bar.scroll_offset = layout.resolved_scroll_offset;
        if layout.scroll_left.is_some() {
            if let Some(vt) = layout.visible_tabs.first() {
                if let Some(t) = bar.tabs.get_mut(vt.tab_idx) {
                    t.label = format!("{SCROLL_LEFT_MARKER}{}", t.label);
                }
            }
        }
        if layout.scroll_right.is_some() {
            if let Some(vt) = layout.visible_tabs.last() {
                if let Some(t) = bar.tabs.get_mut(vt.tab_idx) {
                    t.label = format!("{}{SCROLL_RIGHT_MARKER}", t.label);
                }
            }
        }
    }

    // ─── #2287 (ms-65 §8b): `?` help overlay content ──────────────────────

    /// The reusable quadraui help-layer content for the Board panel
    /// (contract §8b), registered under `"panel:board"` in `CoordApp::new`
    /// / `app::fixtures` — see `plans.rs`'s module docs for the overall
    /// `HelpRegistry`/`ViewHelp` pattern this copies.
    ///
    /// `render_help_overlay` (`plans.rs`) paints the `notes` list's header
    /// as **"Document tabs"** rather than the generic **"Reference"** Plans
    /// gets, and the `actions` list's header as **"Split"** rather than
    /// **"Actions"** — both Board-only special cases there, mirroring how it
    /// already special-cases the Plans-only "Health chips" section.
    ///
    /// The six `notes` rows are contract §8b's own pinned phrases, verbatim,
    /// and match `mocks/board-tabs-help-overlay.screen`'s "Document tabs"
    /// section exactly.
    ///
    /// #2288 (§9): the four `actions` rows are §8b's second section — §9's
    /// pinned key table, verbatim from `mocks/board-tabs-help-overlay.screen`
    /// rows 12–15. They are carried as `actions` purely because `ViewHelp`
    /// has exactly two lists and the overlay needs two sections; Board still
    /// has no `/` command palette (`SidebarView::help_view_id`'s doc comment
    /// on the `Board` arm explains why `events.rs` deliberately never calls
    /// `open_command_palette` for it), and these rows carry no accelerator
    /// so they render in the same two-column shape the notes above do.
    ///
    /// `Ctrl-W s` is listed but inert: contract Note 2 reserves the key for
    /// a later 2×2 layout so a future milestone doesn't have to renegotiate
    /// the binding, and pins that no ms-65 behaviour depends on it doing
    /// anything.
    pub(crate) fn board_view_help() -> ViewHelp {
        ViewHelp::new("Board")
            .with_notes(vec![
                HelpNote::new("click row", "open preview tab"),
                HelpNote::new("double-click row", "pin tab"),
                HelpNote::new("Ctrl-W", "close active tab"),
                HelpNote::new("Ctrl-Tab / Ctrl-Shift-Tab", "cycle tabs"),
                HelpNote::new("middle-click tab", "close tab"),
                HelpNote::new("right-click tab", "tab menu"),
            ])
            .with_actions(vec![
                HelpAction::new("split-right", "Ctrl-W v", "split right"),
                HelpAction::new("split-down", "Ctrl-W s", "split down"),
                HelpAction::new("focus-next-pane", "Ctrl-W w", "next pane"),
                HelpAction::new("close-pane", "Ctrl-W x", "close pane"),
            ])
    }

    pub(crate) fn board_detail_tab_bar(&self) -> TabBar {
        // #316: show an active-dot on the Board Chat tab while a board chat is live.
        let board_chat_live = self.chat_is_board_chat();
        // #675: dot indicator on the Terminal tab when a session exists for the
        // selected board issue.
        let board_terminal_live = self.board_selected_issue().map_or(false, |(repo, num)| {
            // Resolve the repo_slug so we can look up the session key.
            let slug = self
                .data
                .pipeline_repos
                .iter()
                .find(|(name, _)| *name == repo)
                .map(|(_, s)| s.as_str())
                .unwrap_or(repo.as_str())
                .to_string();
            self.detail_terminal_sessions.contains_key(&(slug, num))
        });
        TabBar {
            id: WidgetId::new("board-detail-tabs"),
            tabs: vec![
                TabItem {
                    label: " Board ".to_string(),
                    is_active: self.board_detail_tab == BoardDetailTab::Board,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    label: " Issue ".to_string(),
                    is_active: self.board_detail_tab == BoardDetailTab::Issue,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    // #316: dot indicator when a board chat is live so the
                    // tab is discoverable without forcing the user back to it.
                    // #675: renamed "Chat" → "Board Chat" to distinguish it
                    // from the new per-issue Terminal tab below.
                    label: if board_chat_live {
                        " Board Chat ● ".to_string()
                    } else {
                        " Board Chat ".to_string()
                    },
                    is_active: self.board_detail_tab == BoardDetailTab::Chat,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    // #675: per-issue interactive terminal.  Dot when a session
                    // is live for the selected issue.
                    label: if board_terminal_live {
                        " Terminal ● ".to_string()
                    } else {
                        " Terminal ".to_string()
                    },
                    is_active: self.board_detail_tab == BoardDetailTab::Terminal,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
            ],
            scroll_offset: 0,
            right_segments: vec![],
            active_accent: None,
            show_tab_close: false,
            compact: true,
        }
    }

    /// Look up the selected board issue's body and render via the shared
    /// `issue_body_list` helper. Layered lookup (#168 motivated this):
    ///
    /// 1. Synced row in `data.open_issues` — fast path, no I/O.
    /// 2. In-memory `fetched_issues_cache` populated by a prior background
    ///    `gh issue view` for this session.
    /// 3. In-flight background fetch — show a "Fetching…" placeholder and
    ///    let the next render pick up the result.
    /// 4. No data yet — spawn `gh issue view` in the background (writes the
    ///    result through to the local `issues` table on success so future
    ///    sessions don't re-fetch) and show a placeholder.
    pub(crate) fn board_issue_body_list(&self) -> ListView {
        // #669: use the panel width stashed at draw time for word-wrapping.
        // #2288 (ms-65 §9): the ADDRESSED pane's own stashed width — see
        // `CoordApp::board_issue_wrap_cols`.
        let wrap_width = self.board_issue_wrap_cols().max(40);
        // #2282 (ms-65 §2): the Issue sub-tab's body follows the ACTIVE
        // DOCUMENT TAB, not the tree cursor (see `board_detail_issue_group`).
        let repo = self.board_detail_repo().map(str::to_string);
        let group = self.board_detail_issue_group().cloned();
        let (Some(repo), Some(g)) = (repo, group) else {
            return issue_body_list(
                None,
                self.board_pane_detail_scroll(),
                "board-issue-body",
                wrap_width,
                false,
                None,
            );
        };
        let key = (repo.clone(), g.issue_number);

        // 1. Synced row.
        if let Some(oi) = self
            .data
            .open_issues
            .iter()
            .find(|oi| oi.repo_name == repo && oi.number == g.issue_number)
        {
            // #2497: the /board wire drops a closed (non-epic) issue's body
            // to 0 chars (`body_truncated`); once the background detail
            // fetch (run_periodic_work → GET /issue/{repo}/{number}) has
            // hydrated the full body, render that instead of the wire's
            // truncation notice.
            let hydrated: Option<&str> = if oi.body_truncated {
                self.issue_detail_cache
                    .get(&key)
                    .and_then(|e| e.full.as_deref())
            } else {
                None
            };
            return issue_body_list(
                Some((
                    oi.number,
                    oi.title.as_str(),
                    hydrated.unwrap_or(oi.body.as_str()),
                    &oi.labels[..],
                )),
                self.board_pane_detail_scroll(),
                "board-issue-body",
                wrap_width,
                false,
                None,
            );
        }

        // 2. Drain any completed background fetch into the cache so step 3 picks it up.
        let pending_result = {
            let pending = self.pending_issue_fetches.borrow();
            pending.get(&key).map(|rx| rx.try_recv())
        };
        if let Some(recv) = pending_result {
            match recv {
                Ok(Ok(fetched)) => {
                    self.pending_issue_fetches.borrow_mut().remove(&key);
                    self.fetched_issues_cache
                        .borrow_mut()
                        .insert(key.clone(), fetched);
                }
                Ok(Err(_)) | Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                    // Fetch finished with an error or the thread died — drop
                    // the receiver so the cold-path below will re-spawn next
                    // render. Error surfaces below as the placeholder.
                    self.pending_issue_fetches.borrow_mut().remove(&key);
                }
                Err(std::sync::mpsc::TryRecvError::Empty) => {} // still in flight
            }
        }

        // 3. In-memory cache (populated by a completed fetch).
        if let Some(f) = self.fetched_issues_cache.borrow().get(&key).cloned() {
            return issue_body_list(
                Some((f.number, f.title.as_str(), f.body.as_str(), &f.labels[..])),
                self.board_pane_detail_scroll(),
                "board-issue-body",
                wrap_width,
                false,
                None,
            );
        }

        // 4. Spawn if no fetch is already running.
        if !self.pending_issue_fetches.borrow().contains_key(&key) {
            // Resolve the GitHub slug for this repo. If we can't, fall back to
            // the title-only placeholder instead of a broken gh call.
            let slug = self
                .data
                .pipeline_repos
                .iter()
                .find(|(local, _)| local == &repo)
                .map(|(_, slug)| slug.clone());
            if let Some(slug) = slug {
                let rx = spawn_issue_fetch(slug, repo.clone(), g.issue_number);
                self.pending_issue_fetches
                    .borrow_mut()
                    .insert(key.clone(), rx);
            } else {
                // No slug → can't fetch. Show the title we have with a hint.
                return issue_body_list(
                    Some((
                        g.issue_number,
                        g.issue_title.as_str(),
                        "(no GitHub slug for this repo — add it to coordinator.yml.repos[].github)",
                        &[][..],
                    )),
                    self.board_pane_detail_scroll(),
                    "board-issue-body",
                    wrap_width,
                    false,
                    None,
                );
            }
        }

        // Placeholder while fetch is in flight.
        issue_body_list(
            Some((
                g.issue_number,
                g.issue_title.as_str(),
                "(fetching body via `gh issue view`…)",
                &[][..],
            )),
            self.board_pane_detail_scroll(),
            "board-issue-body",
            wrap_width,
            false,
            None,
        )
    }

    pub(crate) fn pipeline_detail_tab_bar(&self) -> TabBar {
        // #818: redesigned tab set — Pipeline renamed to Overview; Stages and
        // Refinement removed.  Order: Overview / Issue / Log / Summary /
        // Terminal, plus #2405's Completed grid pinned last.
        TabBar {
            id: WidgetId::new("pipeline-detail-tabs"),
            tabs: vec![
                TabItem {
                    label: " Overview ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Overview,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    label: " Issue ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Issue,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                TabItem {
                    label: " Log ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Log,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                // #558: session-history summary tab.
                TabItem {
                    label: " Summary ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Summary,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                // #440: per-issue interactive shell tab.
                TabItem {
                    label: if self.detail_terminal_focused
                        && self.pipeline_detail_tab == PipelineDetailTab::Terminal
                    {
                        " Terminal ▶ ".to_string()
                    } else {
                        " Terminal ".to_string()
                    },
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Terminal,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
                // #2405: the completed-issues grid. Last, so the five
                // per-issue tabs keep the indices every existing click test
                // and Tab-cycle already assumes.
                TabItem {
                    label: " Completed ".to_string(),
                    is_active: self.pipeline_detail_tab == PipelineDetailTab::Completed,
                    is_dirty: false,
                    is_preview: false,
                    is_closable: false,
                },
            ],
            scroll_offset: 0,
            right_segments: vec![],
            active_accent: None,
            show_tab_close: false,
            compact: true,
        }
    }

    /// Issue tab: title header + scrollable full body (j/k to scroll).
    pub(crate) fn pipeline_issue_body_list(&self) -> ListView {
        // #669: use the panel width stashed at draw time for word-wrapping.
        let wrap_width = self.last_issue_panel_cols.get().max(40);
        let issue = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i));
        issue_body_list(
            issue.map(|i| {
                // #2497: the /board wire drops a closed (non-epic) issue's
                // body to 0 chars (`body_truncated`); once the background
                // detail fetch (run_periodic_work → GET
                // /issue/{repo}/{number}) has hydrated the full body,
                // render that instead of the wire's truncation notice.
                let hydrated: Option<&str> = if i.body_truncated {
                    i.coord_repo.as_deref().and_then(|repo| {
                        self.issue_detail_cache
                            .get(&(repo.to_string(), i.number))
                            .and_then(|e| e.full.as_deref())
                    })
                } else {
                    None
                };
                (
                    i.number,
                    i.title.as_str(),
                    hydrated.unwrap_or(i.body.as_str()),
                    &i.all_labels[..],
                )
            }),
            self.pipeline_detail_scroll,
            "pipeline-issue-body",
            wrap_width,
            false,
            None,
        )
    }

    /// Pipeline tab: meta strip (repo/labels/gates/status) plus
    /// #271 part 2 test guidance (branch / repo path / suggested
    /// commands / persisted Phase 1 build result) when Test is
    /// actionable or has been built.
    ///
    /// Still used by tests; the render path now uses
    /// `pipeline_tab_body_list` which inlines this content alongside the
    /// focused-stage output.
    #[allow(dead_code)]
    pub(crate) fn pipeline_issue_summary(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        if let Some(idx) = self.pipeline_sel {
            if let Some(issue) = self.pipeline_issues.get(idx) {
                items.push(kv_item(
                    "Repo",
                    &issue.repo_slug,
                    Some(Color::rgb(160, 160, 180)),
                ));
                if let Some(local) = &issue.coord_repo {
                    items.push(kv_item("Local", local, Some(Color::rgb(140, 200, 140))));
                } else {
                    items.push(kv_item(
                        "Local",
                        "(no coordinator.yml mapping)",
                        Some(Color::rgb(220, 150, 80)),
                    ));
                }
                if !issue.matched_labels.is_empty() {
                    items.push(kv_item(
                        "Labels",
                        &issue.matched_labels.join(", "),
                        Some(Color::rgb(160, 160, 180)),
                    ));
                }
                items.push(kv_item(
                    "Gates",
                    &self.pipeline_stage_names_for_issue(issue).join(" → "),
                    Some(Color::rgb(160, 160, 180)),
                ));
                if let Some((msg, when)) = &self.pipeline_status {
                    if when.elapsed() < Duration::from_secs(8) {
                        items.push(kv_item("", "", None));
                        items.push(kv_item(
                            "",
                            &format!("  {}", msg),
                            Some(Color::rgb(180, 180, 100)),
                        ));
                    }
                }

                // #271 part 2: surface test guidance + persisted build
                // result inline.  Both rely on having a Work assignment
                // to anchor against; without one there's nothing to
                // test or build.
                self.append_test_guidance_rows(&mut items, issue);
                // #932: the Acceptance box's own guidance, reported
                // separately from Test.
                self.append_acceptance_guidance_rows(&mut items, issue);
            }
        }
        ListView {
            id: WidgetId::new("pipeline-summary"),
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

    /// Body list for the **Pipeline** detail tab: issue meta summary
    /// (repo, labels, gates, test guidance) followed immediately by the
    /// focused stage's full content (plan log, worker log, test output,
    /// review verdict + body, merge details).
    ///
    /// Replacing the plain `pipeline_issue_summary` with this combined
    /// list means the user sees the most relevant stage output on the
    /// default tab without switching to Stages.  The scroll offset is
    /// driven by `pipeline_stage_content_scroll` so the wheel and j/k
    /// keys can move through the content as on the Stages tab.
    pub(crate) fn pipeline_tab_body_list(&self) -> ListView {
        self.pipeline_tab_body_list_for(self.pipeline_sel)
    }

    /// #2405: the Overview body for an *arbitrary* `pipeline_issues` index,
    /// rather than always for `pipeline_sel`.
    ///
    /// Extracted so the Completed grid's row detail can render byte-identical
    /// Overview content for an issue that is deliberately **not** the sidebar
    /// selection (completed issues have no sidebar row at all any more). The
    /// no-argument `pipeline_tab_body_list` is the unchanged Overview-tab
    /// caller; everything below is verbatim what it always did.
    pub(crate) fn pipeline_tab_body_list_for(&self, sel: Option<usize>) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        let issue = sel.and_then(|i| self.pipeline_issues.get(i)).cloned();

        // ── Meta summary (repo / labels / gates / status) ────────────
        if let Some(ref issue) = issue {
            items.push(kv_item(
                "Repo",
                &issue.repo_slug,
                Some(Color::rgb(160, 160, 180)),
            ));
            if let Some(local) = &issue.coord_repo {
                items.push(kv_item("Local", local, Some(Color::rgb(140, 200, 140))));
            } else {
                items.push(kv_item(
                    "Local",
                    "(no coordinator.yml mapping)",
                    Some(Color::rgb(220, 150, 80)),
                ));
            }
            if !issue.matched_labels.is_empty() {
                items.push(kv_item(
                    "Labels",
                    &issue.matched_labels.join(", "),
                    Some(Color::rgb(160, 160, 180)),
                ));
            }
            // #1199: an epic row is gated by the milestone gate lane, not
            // the per-issue Work→Test→Review→Merge stages — keep this
            // summary line consistent with the lane widget rendered above it
            // (`build_epic_gate_lane_widget`).
            let gates_line = if labels_carry_epic_label(&issue.all_labels) {
                "Gate A → Gate B → Gate C → Gate D".to_string()
            } else {
                self.pipeline_stage_names_for_issue(issue).join(" → ")
            };
            items.push(kv_item(
                "Gates",
                &gates_line,
                Some(Color::rgb(160, 160, 180)),
            ));
            // #546: per-issue cost rollup — sum of metered (claude -p) cost_usd
            // across all stage iterations (work + review + fix + smoke + plan).
            // Interactive (Max subscription) assignments show cost_usd=NULL and
            // are excluded; they appear as individual "Max" rows in the stages.
            if let Some(total_cost) = self.issue_total_cost(issue) {
                let tok = self.issue_total_tokens(issue);
                let cost_str = if tok > 0 {
                    format!("{}  ({} tokens)", format_cost_usd(total_cost), fmt_tokens(tok))
                } else {
                    format_cost_usd(total_cost)
                };
                items.push(kv_item(
                    "Cost (Σ)",
                    &cost_str,
                    Some(Color::rgb(160, 220, 160)),
                ));
            }
            if let Some((msg, when)) = &self.pipeline_status {
                if when.elapsed() < Duration::from_secs(8) {
                    items.push(kv_item("", "", None));
                    items.push(kv_item(
                        "",
                        &format!("  {}", msg),
                        Some(Color::rgb(180, 180, 100)),
                    ));
                }
            }
            // #1084: the oracle-loop pre-req pipeline (Gate A / per-issue
            // acceptance authoring) precedes — visually and functionally —
            // this issue's normal Work→Test→Review→Merge strip, so its rows
            // render first.
            self.append_gate_a_prereq_guidance_rows(&mut items, issue);
            self.append_acceptance_authoring_prereq_guidance_rows(&mut items, issue);
            self.append_test_guidance_rows(&mut items, issue);
            self.append_acceptance_guidance_rows(&mut items, issue);
        }

        // ── Focused-stage content ─────────────────────────────────────
        // #1199: an epic row has no per-issue Work/Test/Review/Merge stages
        // to show content for — its own Gate A content already renders above
        // via `append_gate_a_prereq_guidance_rows`, and Gates B/C/D have no
        // content source yet (#933/#934, unpersisted `coord milestone
        // gate-c`). Showing this section for an epic would otherwise render
        // a stale "Stage content: Work" heading contradicting the gate lane
        // above it.
        if let Some(ref issue) = issue.as_ref().filter(|i| !labels_carry_epic_label(&i.all_labels)) {
            let stage_names = self.pipeline_stage_names_for_issue(issue);
            if let Some(focused_idx) = self
                .pipeline_focused_stage
                .filter(|&i| i < stage_names.len())
            {
                let name = &stage_names[focused_idx];
                items.push(kv_item("", "", None));
                items.push(kv_item(
                    "",
                    &format!(" ── Stage content: {} ──", capitalize(name)),
                    Some(Color::rgb(220, 220, 230)),
                ));
                items.push(kv_item(
                    "",
                    "   ([/] previous · [/] next · click a stage box above to switch)",
                    Some(Color::rgb(140, 140, 160)),
                ));
                items.push(kv_item("", "", None));
                let content_rows = self.stage_content_for(issue, name);
                if content_rows.is_empty() {
                    items.push(kv_item(
                        "",
                        "   (no content available for this stage yet)",
                        Some(Color::rgb(140, 140, 160)),
                    ));
                } else {
                    items.extend(content_rows);
                }
            }
        }

        ListView {
            id: WidgetId::new("pipeline-tab-body"),
            title: None,
            items,
            selected_idx: 0,
            scroll_offset: self.pipeline_stage_content_scroll,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// #271 part 2: append a "Test guidance" block — branch, local
    /// path, last-build outcome (persisted), suggested next commands —
    /// when the user is looking at an issue whose Test stage is in
    /// play (actionable or recently built).
    pub(crate) fn append_test_guidance_rows(&self, items: &mut Vec<ListItem>, issue: &PipelineIssue) {
        // Find the latest Work assignment for this issue (the build
        // hangs off its branch).
        let work = self.assignments_for_stage(issue, "work");
        let latest = work.iter().max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        let Some(latest) = latest else {
            return;
        };
        // Show this block ONLY when the Test stage is in play (Active
        // or Pending with Work done).  Skip for issues that aren't at
        // the test step yet.
        let test_status = self.test_stage_status_for(issue);
        let actionable = self.test_gate_actionable();
        let has_build_record = self.last_test_builds.contains_key(&latest.id);
        let has_pull_record = self.last_artifact_pulls.contains_key(&latest.id);
        let in_flight = self.test_build_in_flight(&latest.id);
        if !actionable && !has_build_record && !has_pull_record && !in_flight {
            return;
        }

        items.push(kv_item("", "", None));
        items.push(kv_item(
            "Test",
            "ready for manual verification",
            Some(Color::rgb(160, 200, 220)),
        ));
        if let Some(branch) = &latest.branch {
            items.push(kv_item("  Branch", branch, Some(Color::rgb(160, 160, 180))));
        }
        // Local repo path — only when we have a coord-repo mapping.
        if let Some(local) = &issue.coord_repo {
            items.push(kv_item(
                "  Path",
                &format!("see coordinator.yml `repos`: {}", local),
                Some(Color::rgb(160, 160, 180)),
            ));
        }
        // #296: run_cmd — show the manual launch command when defined for
        // this repo.  Absent repos (no run_cmd set) silently skip.
        if let Some(local) = &issue.coord_repo {
            if let Some(cmd) = self.data.pipeline_repo_run_cmds.get(local.as_str()) {
                items.push(kv_item("  Run", cmd, Some(Color::rgb(200, 220, 160))));
            }
        }
        // Persistent build status.  Three states surfaced.
        if in_flight {
            // The job is still going; show elapsed.
            if let Some(job) = self.test_build_jobs.get(&latest.id) {
                let elapsed = job.started_at.elapsed().as_secs();
                items.push(kv_item(
                    "  Build",
                    &format!(
                        "running ({elapsed}s elapsed) — log {}",
                        job.log_path.display()
                    ),
                    Some(Color::rgb(220, 180, 100)),
                ));
            }
        } else if let Some(last) = self.last_test_builds.get(&latest.id) {
            let ago = last.finished_at.elapsed().as_secs();
            // Show the branch the build was actually run against —
            // useful when the user has fix-iterated since (the work
            // assignment's branch may have advanced).
            let branch_note = if Some(&last.branch) != latest.branch.as_ref() {
                format!(" on {}", last.branch)
            } else {
                String::new()
            };
            if last.exit_code == 0 {
                items.push(kv_item(
                    "  Build",
                    &format!(
                        "✓ succeeded in {}s ({}s ago{}) — log {}",
                        last.duration_secs,
                        ago,
                        branch_note,
                        last.log_path.display()
                    ),
                    Some(Color::rgb(120, 200, 120)),
                ));
            } else {
                let snippet = if last.first_error.is_empty() {
                    String::new()
                } else {
                    let trimmed: String = last.first_error.chars().take(80).collect();
                    if last.first_error.chars().count() > 80 {
                        format!("{}…", trimmed)
                    } else {
                        trimmed
                    }
                };
                items.push(kv_item(
                    "  Build",
                    &format!(
                        "✗ exit {} ({}s ago{}){} — log {}",
                        last.exit_code,
                        ago,
                        branch_note,
                        if snippet.is_empty() {
                            String::new()
                        } else {
                            format!(": {snippet}")
                        },
                        last.log_path.display(),
                    ),
                    Some(Color::rgb(220, 100, 100)),
                ));
            }
            // Issue-number breadcrumb, useful when scrolling back via
            // log files — the issue number is the human-friendly key.
            let _ = last.issue_number; // anchored in `last` for future use
        } else {
            items.push(kv_item(
                "  Build",
                "(not run yet — press B)",
                Some(Color::rgb(160, 160, 180)),
            ));
        }
        // #434: Persistent artifact-pull result — survives the 4 s toast.
        if let Some(pull) = self.last_artifact_pulls.get(&latest.id) {
            let ago = pull.finished_at.elapsed().as_secs();
            if pull.exit_code == 0 {
                items.push(kv_item(
                    "  Pull",
                    &format!("✓ → {} ({}s ago)", pull.message, ago),
                    Some(Color::rgb(120, 200, 120)),
                ));
            } else {
                let snippet: String = pull.message.chars().take(80).collect();
                let ellipsis = if pull.message.chars().count() > 80 {
                    "…"
                } else {
                    ""
                };
                items.push(kv_item(
                    "  Pull",
                    &format!(
                        "✗ exit {} ({}s ago): {}{}",
                        pull.exit_code, ago, snippet, ellipsis
                    ),
                    Some(Color::rgb(220, 100, 100)),
                ));
            }
        }
        // #271 part 2 follow-up: surface the PR description and files
        // changed inline when a PR exists.  The worker's PR body is
        // the canonical place they explain new sample apps, demo
        // binaries, manual test steps — without this the user had to
        // ask Claude separately.
        if let Some(pr_number) = self.pipeline_pr_number(issue) {
            items.push(kv_item(
                "  PR",
                &format!("#{}", pr_number),
                Some(Color::rgb(160, 200, 220)),
            ));
            match self.pr_info_for_issue(issue) {
                Some(pr) => {
                    if !pr.title.is_empty() {
                        items.push(kv_item(
                            "  PR title",
                            &pr.title,
                            Some(Color::rgb(220, 220, 220)),
                        ));
                    }
                    // Show up to 6 body lines; the user can open the PR
                    // for the rest.  Skip empty lines at the head so
                    // the preview is dense.
                    let body_lines: Vec<&str> = pr
                        .body
                        .lines()
                        .skip_while(|l| l.trim().is_empty())
                        .take(6)
                        .collect();
                    if !body_lines.is_empty() {
                        items.push(kv_item("  PR notes", "", Some(Color::rgb(160, 160, 180))));
                        for line in body_lines {
                            // Truncate any wildly long line so the
                            // single-row list doesn't blow out.
                            let trimmed: String = line.chars().take(140).collect();
                            items.push(kv_item(
                                "",
                                &format!("    {trimmed}"),
                                Some(Color::rgb(200, 200, 200)),
                            ));
                        }
                        if pr.body.lines().count() > 6 {
                            items.push(kv_item("", "    …", Some(Color::rgb(140, 140, 160))));
                        }
                    }
                    // Files-changed list — useful for "what should I
                    // test?" — capped at the first 10 entries.
                    if !pr.files.is_empty() {
                        items.push(kv_item(
                            "  Files",
                            &format!("({} changed)", pr.files.len()),
                            Some(Color::rgb(160, 160, 180)),
                        ));
                        for path in pr.files.iter().take(10) {
                            items.push(kv_item(
                                "",
                                &format!("    {path}"),
                                Some(Color::rgb(200, 200, 200)),
                            ));
                        }
                        if pr.files.len() > 10 {
                            items.push(kv_item(
                                "",
                                &format!("    … and {} more", pr.files.len() - 10),
                                Some(Color::rgb(140, 140, 160)),
                            ));
                        }
                    }
                    // The latest substantive review (state != PENDING,
                    // non-empty body when possible).  Filters out
                    // "COMMENTED" reviews with empty bodies that gh
                    // sometimes returns from sidecar bots.
                    let latest_review = pr.reviews.iter().rev().find(|r| {
                        r.state != "PENDING"
                            && (!r.body.is_empty()
                                || r.state == "APPROVED"
                                || r.state == "CHANGES_REQUESTED")
                    });
                    if let Some(rev) = latest_review {
                        let (state_label, state_color) = match rev.state.as_str() {
                            "APPROVED" => ("✓ Approved", Color::rgb(120, 200, 120)),
                            "CHANGES_REQUESTED" => {
                                ("✗ Changes Requested", Color::rgb(220, 100, 100))
                            }
                            "COMMENTED" => ("Commented", Color::rgb(160, 200, 220)),
                            other => (other, Color::rgb(200, 200, 200)),
                        };
                        items.push(kv_item("  Review", state_label, Some(state_color)));
                        // #248: surface the coord:review header counts as a
                        // single dense line when the coordinator embedded
                        // one.  Lets the user see "2 blocking, 5 polish"
                        // without scrolling the prose body.
                        if let Some(header) = parse_coord_review_header(&rev.body) {
                            let mut parts: Vec<String> = Vec::new();
                            if let Some(b) = header.blocking {
                                parts.push(format!("{b} blocking"));
                            }
                            if let Some(n) = header.nonblocking {
                                parts.push(format!("{n} non-blocking"));
                            }
                            if let Some(n) = header.nits {
                                parts.push(format!("{n} nits"));
                            }
                            if let Some(r) = header.reviewer.as_deref() {
                                parts.push(format!("reviewer: {r}"));
                            }
                            if !parts.is_empty() {
                                items.push(kv_item(
                                    "",
                                    &format!("    ({})", parts.join(", ")),
                                    Some(Color::rgb(160, 160, 180)),
                                ));
                            }
                        }
                        // Skip leading whitespace and the coord:review
                        // header HTML comment so the preview is dense
                        // and human-readable.
                        let body_lines: Vec<&str> = rev
                            .body
                            .lines()
                            .filter(|l| !l.trim_start().starts_with("<!-- coord:review"))
                            .skip_while(|l| l.trim().is_empty())
                            .take(10)
                            .collect();
                        for line in &body_lines {
                            let trimmed: String = line.chars().take(140).collect();
                            items.push(kv_item(
                                "",
                                &format!("    {trimmed}"),
                                Some(Color::rgb(200, 200, 200)),
                            ));
                        }
                        if rev.body.lines().count() > 10 {
                            items.push(kv_item("", "    …", Some(Color::rgb(140, 140, 160))));
                        }
                    }
                }
                None => {
                    items.push(kv_item(
                        "  PR notes",
                        "(loading via gh pr view…)",
                        Some(Color::rgb(160, 160, 180)),
                    ));
                }
            }
        }

        // #252: worker-emitted smoke tests.  Three states (see
        // Assignment.smoke_tests doc): None → graceful placeholder,
        // empty list → "change is internal", non-empty → bullets.
        items.push(kv_item("", "", None));
        match latest.smoke_tests.as_deref() {
            Some(tests) if !tests.is_empty() => {
                items.push(kv_item(
                    "Smoke tests",
                    "(from worker)",
                    Some(Color::rgb(160, 200, 220)),
                ));
                for t in tests {
                    items.push(kv_item(
                        "",
                        &format!("  • {t}"),
                        Some(Color::rgb(220, 220, 220)),
                    ));
                }
            }
            Some(_empty) => {
                items.push(kv_item(
                    "Smoke tests",
                    "(none — worker reported change is internal)",
                    Some(Color::rgb(160, 160, 180)),
                ));
            }
            None => {
                items.push(kv_item(
                    "Smoke tests",
                    "(worker did not provide a list — inspect the diff)",
                    Some(Color::rgb(160, 160, 180)),
                ));
            }
        }

        // #336/#433: Artifact badge — show when the manifest is cached and
        // non-empty for this branch.  When the fetch has completed but no
        // artifacts are available, surface the specific reason rather than
        // silently hiding the badge (intermittency was invisible before #433).
        if let Some(branch) = &latest.branch {
            let sanitized = sanitize_branch(branch);
            let key = (latest.repo.clone(), sanitized.clone());
            match self.artifact_cache.get(&key) {
                Some(entry) => {
                    if let Some(manifest) = &entry.manifest {
                        // ── Artifact stash found — show the download badge ────
                        let file_count = manifest.files.len();
                        let total_mb = manifest.total_bytes as f64 / 1_048_576.0;
                        // Warn when the stash was built by a different
                        // assignment — e.g. the branch was re-pushed and a
                        // newer worker ran.
                        let built_by_note = if manifest.built_by_assignment_id.as_deref()
                            != Some(latest.id.as_str())
                        {
                            if let Some(id) = &manifest.built_by_assignment_id {
                                let id_short: String = id.chars().take(8).collect();
                                format!(" [built by {}]", id_short)
                            } else {
                                String::new()
                            }
                        } else {
                            String::new()
                        };
                        items.push(kv_item("", "", None));
                        items.push(kv_item(
                            "  Artifacts",
                            &format!(
                                "📦 {} file{}, {:.1} MB on {}{} — press a to pull",
                                file_count,
                                if file_count == 1 { "" } else { "s" },
                                total_mb,
                                latest.machine,
                                built_by_note,
                            ),
                            Some(Color::rgb(200, 180, 100)),
                        ));
                    } else {
                        // ── Fetch completed but no artifacts available ────────
                        // Surface why, so intermittent absences are diagnosable.
                        let reason = match &entry.absence_reason {
                            Some(ArtifactAbsence::NotStashed)
                            | Some(ArtifactAbsence::ManifestEmpty) => {
                                if issue_produces_build_artifact(&latest.repo, &issue.title) {
                                    format!("no binary built on {} — a: how to test", latest.machine)
                                } else {
                                    "CLI change — no binary; a: how to test".to_string()
                                }
                            }
                            Some(ArtifactAbsence::AgentUnreachable(e)) => {
                                let msg: String = e.chars().take(80).collect();
                                let ellipsis = if e.chars().count() > 80 { "…" } else { "" };
                                format!("agent unreachable: {}{}", msg, ellipsis)
                            }
                            None => "(fetch result unknown)".to_string(),
                        };
                        items.push(kv_item("", "", None));
                        items.push(kv_item(
                            "  Artifacts",
                            &format!("[a] unavailable — {}", reason),
                            Some(Color::rgb(160, 140, 100)),
                        ));
                    }
                }
                None => {
                    // No cache entry yet — fetch is in-flight (triggered by
                    // the tick handler as soon as the Pipeline view is active).
                    items.push(kv_item("", "", None));
                    items.push(kv_item(
                        "  Artifacts",
                        "(checking agent…)",
                        Some(Color::rgb(140, 140, 160)),
                    ));
                }
            }
        }

        // Suggested next steps.
        let test_label = match test_status {
            StageStatus::Failed => {
                "previously failed — press R to re-dispatch Work, or P/F/S to re-record"
            }
            _ => "press P=pass, F=fail, r=report+fix, S=skip after manual verification",
        };
        items.push(kv_item(
            "  Next",
            test_label,
            Some(Color::rgb(160, 160, 180)),
        ));
    }

    /// #1084: append the Gate A mini-pipeline block (Author → Test → Review
    /// → Merge) for an epic/tracking-issue row — the milestone-level
    /// mock-author pre-req that gates every member issue's acceptance
    /// authoring (docs/ORACLE_LOOP.md). Silent (no rows) for non-epic rows
    /// and for epics where Gate A has never been dispatched, so the panel
    /// stays quiet until there's something to report.
    pub(crate) fn append_gate_a_prereq_guidance_rows(&self, items: &mut Vec<ListItem>, issue: &PipelineIssue) {
        // #1198: case-insensitive — unified via `labels_carry_epic_label`
        // (was a case-sensitive `== "epic"` that silently hid this block for
        // an `Epic`/`EPIC`-cased label).
        if !labels_carry_epic_label(&issue.all_labels) {
            return;
        }
        let status = self.gate_a_prereq_status(issue);
        if status.author_id.is_none() {
            return;
        }
        self.append_prereq_pipeline_rows(items, "Gate A", &status);
    }

    /// #1084: append the per-issue Acceptance-Authoring mini-pipeline block
    /// (Author → Test → Review → Merge) — the JIT `test-author` slice that
    /// extends the milestone's sealed acceptance suite for this specific
    /// work-order member issue, and gates that issue's real Work dispatch
    /// (docs/ORACLE_LOOP.md). Silent for issues outside a milestone work
    /// order and for members whose slice has never been dispatched.
    pub(crate) fn append_acceptance_authoring_prereq_guidance_rows(
        &self,
        items: &mut Vec<ListItem>,
        issue: &PipelineIssue,
    ) {
        let Some(tracking_issue) = self.milestone_tracking_issue_for(issue) else {
            return;
        };
        let status = self.acceptance_authoring_prereq_status(issue, tracking_issue);
        if status.author_id.is_none() {
            return;
        }
        self.append_prereq_pipeline_rows(items, "Acceptance Authoring", &status);
    }

    /// Shared renderer for the two oracle-loop pre-req blocks above: a
    /// title row naming the resolved Author-stage assignment, then one row
    /// per stage (Author/Test/Review/Merge) with its status and — for the
    /// stage currently "live" (Active) or stalled (Pending directly behind
    /// a settled predecessor) — an elapsed-time annotation. A long elapsed
    /// time on a Pending row *is* the "stuck, and on which gate" signal
    /// (claude-coordinator#947's 19-hour-invisible-stall scenario): no
    /// separate "stuck" status is needed when the number speaks for itself.
    fn append_prereq_pipeline_rows(&self, items: &mut Vec<ListItem>, title: &str, status: &PrereqPipelineStatus) {
        items.push(kv_item("", "", None));
        let id_short: String = status.author_id.as_deref().unwrap_or("").chars().take(8).collect();
        items.push(kv_item(
            title,
            &format!("(assignment {})", id_short),
            Some(Color::rgb(160, 160, 180)),
        ));
        const STAGE_NAMES: [&str; 4] = ["Author", "Test", "Review", "Merge"];
        for (name, stage) in STAGE_NAMES.iter().zip(status.stages.iter()) {
            let (label, color) = prereq_stage_label(stage);
            let suffix = match (&stage.status, stage.since) {
                (StageStatus::Active, Some(since)) => {
                    format!(" — running {}", elapsed_since_now(since))
                }
                (StageStatus::Pending, Some(since)) => {
                    // #2397: append the merge-queue's own computed block
                    // reason (Merge stage only — `block_reason` is `None`
                    // for every other stage) so "pending — waiting 2h22m"
                    // no longer sits with zero signal for *why*, alongside
                    // the disambiguated `elapsed_since_now` unit.
                    match &stage.block_reason {
                        Some(reason) => {
                            format!(" — waiting {} — {reason}", elapsed_since_now(since))
                        }
                        None => format!(" — waiting {}", elapsed_since_now(since)),
                    }
                }
                _ => String::new(),
            };
            items.push(kv_item(
                &format!("  {}", name),
                &format!("{label}{suffix}"),
                Some(color),
            ));
        }
    }

    /// #932: append an "Acceptance" guidance block — verdict, per-test
    /// progress ("3/7"), failing-test summary, and the SHA it was recorded
    /// against — when this issue's Acceptance box has ever been recorded.
    /// Reported and gated SEPARATELY from the Test block above: silent
    /// (no rows at all) for issues outside an oracle-loop milestone, since
    /// `acceptance_stage_status_for` reads Skipped as "no signal", not
    /// "in play".
    pub(crate) fn append_acceptance_guidance_rows(&self, items: &mut Vec<ListItem>, issue: &PipelineIssue) {
        let status = self.acceptance_stage_status_for(issue);
        if status == StageStatus::Skipped {
            return;
        }

        items.push(kv_item("", "", None));
        let (label, color) = match status {
            StageStatus::Done => ("passed", Color::rgb(120, 200, 120)),
            StageStatus::Failed => ("failed", Color::rgb(220, 100, 100)),
            _ => ("pending", Color::rgb(160, 160, 180)),
        };
        let progress = self
            .acceptance_progress_for(issue)
            .map(|(passed, total)| format!(" ({passed}/{total} green)"))
            .unwrap_or_default();
        items.push(kv_item(
            "Acceptance",
            &format!("{label}{progress}"),
            Some(color),
        ));

        let work = self.assignments_for_stage(issue, "work");
        let latest = work
            .iter()
            .filter(|a| {
                a.acceptance_state
                    .as_deref()
                    .map(|s| !s.is_empty())
                    .unwrap_or(false)
            })
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        if let Some(a) = latest {
            if let Some(sha) = &a.acceptance_sha {
                items.push(kv_item(
                    "  SHA",
                    &sha.chars().take(12).collect::<String>(),
                    Some(Color::rgb(160, 160, 180)),
                ));
            }
            if status == StageStatus::Failed {
                if let Some(reason) = &a.acceptance_reason {
                    for (i, line) in reason.lines().take(5).enumerate() {
                        let label = if i == 0 { "  Failing" } else { "" };
                        items.push(kv_item(label, line, Some(Color::rgb(220, 140, 140))));
                    }
                }
            }
        }
    }

    /// Detail list for the Stages tab. One section per stage in the
    /// pipeline; under each section, the latest matching assignment's
    /// id, machine, status, dispatched/finished times and exit code
    /// (or the merge_queue row's state and PR for the merge stage).
    /// #stage-content: return the content rows for the focused stage's
    /// detail panel.  Each stage type sources its content differently:
    ///
    /// - **Plan**   — the worker's plan log tail (planning agent output)
    /// - **Work**   — the worker's log tail (summary of work done)
    /// - **Test**   — the cached `TestBuildResult.log_path` first 200 lines
    /// - **Review** — the cached `review_findings` body from the DB
    ///                (populated by notify when the review completed)
    /// - **Merge**  — the merge_queue entry's state + error if any
    ///
    /// Returns an empty `Vec` when no content can be sourced — the
    /// caller renders a "no content available" placeholder.
    pub(crate) fn stage_content_for(&self, issue: &PipelineIssue, stage_name: &str) -> Vec<ListItem> {
        match stage_name {
            "review" => self.stage_content_review(issue),
            "test" => self.stage_content_test(issue),
            "merge" => self.stage_content_merge(issue),
            // Plan: prefer the structured plan cached in the plans table
            // (parsed by `coord notify`); fall back to log tail if no row.
            // Without this the panel dumped raw stream-json events,
            // unreadable to humans.
            "plan" => self.stage_content_plan(issue),
            // Work: read the latest assignment's log tail.
            "work" => self.stage_content_assignment_log(issue, stage_name),
            _ => Vec::new(),
        }
    }

    /// Render the structured plan for the selected pipeline issue.
    /// Pulls from `BoardData.plans` (populated by `coord notify` parsing
    /// the plan worker's log).  When no plan row exists (notify hasn't
    /// run yet, or the worker exited without a structured plan), falls
    /// back to the log-tail view so the user sees something.
    pub(crate) fn stage_content_plan(&self, issue: &PipelineIssue) -> Vec<ListItem> {
        let local_repo = issue.coord_repo.as_deref();
        let plan_assignment = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .filter(|a| a.assignment_type.as_deref() == Some("plan"))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        let Some(a) = plan_assignment else {
            return vec![kv_item(
                "",
                "   (plan assignment not found in board — press S to sync, or run `coord notify`)",
                Some(Color::rgb(160, 160, 180)),
            )];
        };
        let Some(plan) = self.data.plans.get(&a.id) else {
            // No structured plan cached — fall back to log tail with a hint.
            let mut rows = vec![kv_item(
                "",
                "   (plan not yet parsed — run `coord notify` to refresh)",
                Some(Color::rgb(160, 160, 180)),
            )];
            rows.push(kv_item("", "", None));
            rows.extend(self.stage_content_assignment_log(issue, "plan"));
            return rows;
        };

        let label_color = Color::rgb(180, 200, 240);
        let body_color = Color::rgb(220, 220, 220);
        let mut rows: Vec<ListItem> = Vec::new();

        if !plan.plan.is_empty() {
            rows.push(kv_item("", " Summary", Some(label_color)));
            for line in plan.plan.lines() {
                rows.push(kv_item("", &format!("   {}", line), Some(body_color)));
            }
            rows.push(kv_item("", "", None));
        }

        if !plan.files_modify.is_empty() {
            rows.push(kv_item("", " Files to modify", Some(label_color)));
            for f in &plan.files_modify {
                rows.push(kv_item("", &format!("   - {}", f), Some(body_color)));
            }
            rows.push(kv_item("", "", None));
        }

        if !plan.approach.is_empty() {
            rows.push(kv_item("", " Approach", Some(label_color)));
            for line in plan.approach.lines() {
                rows.push(kv_item("", &format!("   {}", line), Some(body_color)));
            }
            rows.push(kv_item("", "", None));
        }

        if !plan.risks.is_empty() {
            rows.push(kv_item("", " Risks", Some(label_color)));
            for line in plan.risks.lines() {
                rows.push(kv_item("", &format!("   {}", line), Some(body_color)));
            }
            rows.push(kv_item("", "", None));
        }

        if !plan.estimate.is_empty() {
            rows.push(kv_item("", " Estimate", Some(label_color)));
            rows.push(kv_item(
                "",
                &format!("   {}", plan.estimate),
                Some(body_color),
            ));
            rows.push(kv_item("", "", None));
        }

        match &plan.smoke_tests {
            Some(bullets) if bullets.is_empty() => {
                rows.push(kv_item("", " Smoke tests", Some(label_color)));
                rows.push(kv_item(
                    "",
                    "   (none — change is internal)",
                    Some(Color::rgb(160, 160, 180)),
                ));
                rows.push(kv_item("", "", None));
            }
            Some(bullets) => {
                rows.push(kv_item("", " Smoke tests", Some(label_color)));
                for b in bullets {
                    rows.push(kv_item("", &format!("   - {}", b), Some(body_color)));
                }
                rows.push(kv_item("", "", None));
            }
            None => {
                // Plan worker predates the SMOKE_TESTS-in-plan prompt
                // (or just forgot).  Show a muted line so the user knows
                // it's missing but doesn't have to dig.
                rows.push(kv_item("", " Smoke tests", Some(label_color)));
                rows.push(kv_item(
                    "",
                    "   (no SMOKE_TESTS block in plan — author manually)",
                    Some(Color::rgb(160, 160, 180)),
                ));
                rows.push(kv_item("", "", None));
            }
        }

        rows
    }

    /// Pull the cached review findings (verdict + body) for the
    /// selected pipeline issue.  Reads the JSON column populated by
    /// `coord/notify.py::_persist_review_findings`.
    pub(crate) fn stage_content_review(&self, issue: &PipelineIssue) -> Vec<ListItem> {
        // Find the latest review assignment for this issue.
        let local_repo = issue.coord_repo.as_deref();
        let review = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .filter(|a| a.assignment_type.as_deref() == Some("review"))
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        let Some(review) = review else {
            return Vec::new();
        };
        // Findings JSON was loaded with the board (no per-render DB
        // query).  When None: for an automated review, notify hasn't parsed
        // it yet — run `coord notify` or `coord bounce`.  For a human-
        // attended review with request-changes (#587), the findings were
        // never written; the rework dialog will capture them when the fix
        // is started.
        // #1337: the /board wire carries only a bounded preview of the
        // findings (review_findings_truncated).  When the background detail
        // fetch (run_periodic_work → GET /assignment/{id}) has hydrated the
        // full body, render that instead; until then the preview renders —
        // its body text ends with an explicit truncation notice.
        let hydrated: Option<&str> = if review.review_findings_truncated {
            review.review_findings_len.and_then(|len| {
                self.findings_detail_cache
                    .get(&(review.id.clone(), len))
                    .and_then(|e| e.full.as_deref())
            })
        } else {
            None
        };
        let Some(raw) = hydrated.or(review.review_findings.as_deref()) else {
            if review.review_verdict.as_deref() == Some("request-changes") {
                return vec![
                    kv_item(
                        "",
                        "   ⚠ No findings captured for this review.",
                        Some(Color::rgb(220, 180, 80)),
                    ),
                    kv_item(
                        "",
                        "   Use 'Start Fix' — the dialog will ask you to enter them.",
                        Some(Color::rgb(180, 180, 180)),
                    ),
                ];
            }
            return vec![kv_item(
                "",
                "   (review not yet parsed — run `coord notify` or `coord bounce` to refresh)",
                Some(Color::rgb(160, 160, 180)),
            )];
        };
        let parsed: serde_json::Value = match serde_json::from_str(raw) {
            Ok(v) => v,
            Err(_) => {
                return vec![kv_item(
                    "",
                    "   (review_findings JSON malformed — re-parse via `coord notify`)",
                    Some(Color::rgb(220, 180, 100)),
                )];
            }
        };
        let verdict = parsed
            .get("verdict")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown")
            .to_string();
        let body = parsed
            .get("body")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let mut rows: Vec<ListItem> = Vec::new();
        let (vtext, vcolor) = match verdict.as_str() {
            "approve" => ("✓ approved", Color::rgb(120, 200, 120)),
            "request-changes" => ("✗ changes requested", Color::rgb(220, 100, 100)),
            other => (other, Color::rgb(220, 180, 100)),
        };
        rows.push(kv_item("Verdict", vtext, Some(vcolor)));
        rows.push(kv_item("", "", None));
        // Render the body line-by-line as plain text (markdown
        // styling lands once quadraui#262 ships and we adopt it).
        // Filter out the coord:review header — that's machine-readable
        // metadata, not user-facing prose.
        for line in body
            .lines()
            .filter(|l| !l.trim_start().starts_with("<!-- coord:review"))
        {
            if line.is_empty() {
                rows.push(kv_item("", "", None));
            } else {
                let trimmed: String = line.chars().take(180).collect();
                rows.push(kv_item("", &format!("   {trimmed}"), None));
            }
        }
        rows
    }

    /// Test stage content — #349 plan (if available) + the cached build log.
    ///
    /// Shows the AI-generated smoke test plan as a "SMOKE TEST PLAN" system
    /// block at the top, with numbered steps that the user can run via keys
    /// 1–8.  Below that, the cached `coord test` build log is rendered as
    /// before.
    pub(crate) fn stage_content_test(&self, issue: &PipelineIssue) -> Vec<ListItem> {
        // Find the latest work assignment.
        let local_repo = issue.coord_repo.as_deref();
        let work_assignment = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .filter(|a| a.assignment_type.as_deref().unwrap_or("work") == "work")
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        let Some(work) = work_assignment else {
            return Vec::new();
        };
        let work_id = work.id.clone();
        let mut rows: Vec<ListItem> = Vec::new();

        // ── #349: Smoke test plan section ────────────────────────────────────
        let header_color = Color::rgb(200, 200, 240);
        let step_color = Color::rgb(220, 220, 220);
        let pending_color = Color::rgb(220, 180, 100);
        let ok_color = Color::rgb(120, 200, 120);
        let fail_color = Color::rgb(220, 100, 100);
        let dim_color = Color::rgb(140, 140, 160);

        match &work.test_plan {
            Some(steps) => {
                rows.push(kv_item("", "── SMOKE TEST PLAN ──", Some(header_color)));
                rows.push(kv_item(
                    "",
                    "   Press 1–9 to run a step.  [a] to pull artifacts.  \
                     Verify steps: press key to mark ✓.",
                    Some(dim_color),
                ));
                rows.push(kv_item("", "", None));

                // Assign number keys only to non-pull steps (1–9).
                // Pull steps use the [a] keybind and display [a] as their hint.
                let mut run_key: u8 = 0;
                for (i, step) in steps.iter().enumerate() {
                    // Determine the display key for this step.
                    let is_pull = step.kind == "pull";
                    let key_hint: String = if is_pull {
                        "[a]".to_string()
                    } else {
                        run_key += 1;
                        if run_key > 9 {
                            // More than 9 runnable steps — stop rendering to
                            // avoid implying a key binding that doesn't exist.
                            break;
                        }
                        format!("[{}]", run_key)
                    };

                    // Determine status indicator for this step.
                    let key = (work_id.clone(), i);
                    let status_str = if self.test_step_jobs.contains_key(&key) {
                        "⏳ running…".to_string()
                    } else if let Some(&exit) = self.test_step_results.get(&key) {
                        if exit == 0 {
                            "✓".to_string()
                        } else {
                            format!("✗ (exit {})", exit)
                        }
                    } else {
                        String::new()
                    };
                    let status_color = if self.test_step_jobs.contains_key(&key) {
                        pending_color
                    } else if let Some(&exit) = self.test_step_results.get(&key) {
                        if exit == 0 {
                            ok_color
                        } else {
                            fail_color
                        }
                    } else {
                        step_color
                    };

                    // Build the step description line.
                    let desc = match step.kind.as_str() {
                        "pull" => {
                            let label = step.label.as_deref().unwrap_or("");
                            let cmd = step.cmd.as_deref().unwrap_or("(no cmd)");
                            if label.is_empty() {
                                format!("{} pull: {}", key_hint, cmd)
                            } else {
                                format!("{} pull {}: {}", key_hint, label, cmd)
                            }
                        }
                        "verify" => {
                            let check = step.check.as_deref().unwrap_or("(no check)");
                            format!("{} (verify) {}", key_hint, check)
                        }
                        _ => {
                            // "run" and unknown kinds.
                            let cmd = step.cmd.as_deref().unwrap_or("(no cmd)");
                            format!("{} {}", key_hint, cmd)
                        }
                    };
                    let desc_capped: String = desc.chars().take(160).collect();
                    let display = if status_str.is_empty() {
                        desc_capped
                    } else {
                        format!("{desc_capped}  {status_str}")
                    };
                    rows.push(kv_item("", &format!("   {display}"), Some(status_color)));

                    // Display captured output lines below the step row.
                    if let Some(output) = self.test_step_output.get(&key) {
                        for line in output.lines().take(50) {
                            let trimmed: String = line.chars().take(160).collect();
                            rows.push(kv_item("", &format!("     {trimmed}"), Some(dim_color)));
                        }
                    }
                }
                rows.push(kv_item("", "", None));
            }
            None => {
                // Plan not yet generated — "Preparing plan…" placeholder.
                // `maybe_spawn_test_plan` (called each tick) will spawn
                // `coord test-plan` the next time it runs.
                rows.push(kv_item("", "── SMOKE TEST PLAN ──", Some(header_color)));
                if self.test_plan_pending.contains(&work_id) {
                    rows.push(kv_item(
                        "",
                        "   Preparing plan… (running `coord test-plan`)",
                        Some(pending_color),
                    ));
                } else {
                    rows.push(kv_item("", "   Preparing plan…", Some(pending_color)));
                }
                rows.push(kv_item("", "", None));
            }
        }

        // ── #434: Artifact-pull result ───────────────────────────────────────
        if let Some(pull) = self.last_artifact_pulls.get(&work_id) {
            let ago = pull.finished_at.elapsed().as_secs();
            if pull.exit_code == 0 {
                rows.push(kv_item(
                    "Last pull",
                    &format!("✓ → {} ({}s ago)", pull.message, ago),
                    Some(ok_color),
                ));
            } else {
                let snippet: String = pull.message.chars().take(80).collect();
                let ellipsis = if pull.message.chars().count() > 80 {
                    "…"
                } else {
                    ""
                };
                rows.push(kv_item(
                    "Last pull",
                    &format!(
                        "✗ exit {} ({}s ago): {}{}",
                        pull.exit_code, ago, snippet, ellipsis
                    ),
                    Some(fail_color),
                ));
            }
        }

        // ── Build log section (unchanged from prior implementation) ─────────
        let Some(build) = self.last_test_builds.get(&work_id) else {
            rows.push(kv_item(
                "",
                "   (no build recorded — press B to run `coord test`)",
                Some(dim_color),
            ));
            return rows;
        };
        let (status_label, status_color) = if build.exit_code == 0 {
            ("✓ succeeded", ok_color)
        } else {
            ("✗ failed", fail_color)
        };
        rows.push(kv_item("Build", status_label, Some(status_color)));
        rows.push(kv_item(
            "Exit code",
            &build.exit_code.to_string(),
            Some(Color::rgb(180, 180, 180)),
        ));
        rows.push(kv_item(
            "Log",
            &build.log_path.display().to_string(),
            Some(Color::rgb(160, 160, 180)),
        ));
        rows.push(kv_item("", "", None));
        // Read the first 200 lines of the log for inline display.
        let content = std::fs::read_to_string(&build.log_path).unwrap_or_default();
        if content.is_empty() {
            rows.push(kv_item(
                "",
                "   (log file empty or unreadable)",
                Some(Color::rgb(160, 160, 180)),
            ));
        } else {
            for line in content.lines().take(200) {
                let trimmed: String = line.chars().take(180).collect();
                rows.push(kv_item("", &format!("   {trimmed}"), None));
            }
        }
        rows
    }

    /// Merge stage content — pulled from the merge_queue entry.
    pub(crate) fn stage_content_merge(&self, issue: &PipelineIssue) -> Vec<ListItem> {
        let entry = self
            .data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug);
        let Some(entry) = entry else {
            return Vec::new();
        };
        let mut rows: Vec<ListItem> = Vec::new();
        rows.push(kv_item(
            "State",
            &entry.state,
            Some(Color::rgb(200, 200, 220)),
        ));
        if let Some(pr) = entry.pr_number {
            rows.push(kv_item(
                "PR",
                &format!("#{pr}"),
                Some(Color::rgb(160, 200, 220)),
            ));
        }
        rows
    }

    /// Plan / Work stage content — read the tail of the matching
    /// assignment's log file.  Returns an empty placeholder when no
    /// assignment exists or the log is unreadable.
    pub(crate) fn stage_content_assignment_log(&self, issue: &PipelineIssue, stage: &str) -> Vec<ListItem> {
        let local_repo = issue.coord_repo.as_deref();
        let assignment = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .filter(|a| {
                let t = a.assignment_type.as_deref().unwrap_or("work");
                if stage == "work" {
                    t == "work"
                } else {
                    t == stage
                }
            })
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        let Some(a) = assignment else {
            return Vec::new();
        };
        let log_path = std::path::PathBuf::from(std::env::var("HOME").unwrap_or_default())
            .join(".coord")
            .join("logs")
            .join(format!("{}.log", a.id));
        let content = std::fs::read_to_string(&log_path).unwrap_or_default();
        if content.is_empty() {
            return vec![kv_item(
                "",
                &format!(
                    "   (log not on this machine — assignment ran on {}; \
                     run `coord log {} -f` to follow)",
                    a.machine, a.id,
                ),
                Some(Color::rgb(160, 160, 180)),
            )];
        }
        // Tail of the log — last 200 lines.
        let lines: Vec<&str> = content.lines().collect();
        let tail_start = lines.len().saturating_sub(200);
        let mut rows: Vec<ListItem> = Vec::new();
        if tail_start > 0 {
            rows.push(kv_item(
                "",
                &format!(
                    "   (showing last 200 of {} lines from {}.log)",
                    lines.len(),
                    a.id,
                ),
                Some(Color::rgb(140, 140, 160)),
            ));
            rows.push(kv_item("", "", None));
        }
        for line in &lines[tail_start..] {
            // #302: don't hard-clip at 180 — the Log tab is horizontally
            // scrollable. Keep a generous cap so a pathological single-line
            // blob can't blow up the row, but let normal lines through whole.
            let trimmed: String = line.chars().take(4000).collect();
            rows.push(kv_item("", &format!("   {trimmed}"), None));
        }
        rows
    }

    /// #2342: candidate log-source assignments for `issue`'s Log tab,
    /// newest-dispatched first.
    ///
    /// An assignment qualifies if either its raw `issue_number` matches
    /// `issue.number` — the pre-#2342 match, unchanged for
    /// `work`/`review`/`smoke`/`conflict-fix`/`fix-*`, and how a milestone
    /// **tracking/epic** issue picks up its own Gate A `mock-author` row
    /// *and* every member's `test-author` row (test-author always books
    /// `issue_number` to the tracking issue, never the member issue it's
    /// actually for — `Assignment::for_issue_number`'s doc comment) — or
    /// its `effective_issue_number()` matches (#1084's `for_issue_number`
    /// correlation), which is how a milestone **member** issue finds its
    /// own `test-author` row even though that row's raw `issue_number`
    /// points at the tracking issue instead.
    ///
    /// Restricted to `is_workable_type` so scoping-chat assignment types
    /// (`refinement`, `test-chat`, `new-issue-chat`, …) don't clutter the
    /// picker.
    pub(crate) fn log_candidates_for_issue(&self, issue: &PipelineIssue) -> Vec<&Assignment> {
        let local_repo = issue.coord_repo.as_deref();
        let mut candidates: Vec<&Assignment> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number || a.effective_issue_number() == issue.number)
            .filter(|a| match local_repo {
                Some(r) => a.repo == r,
                None => true,
            })
            .filter(|a| is_workable_type(a.assignment_type.as_deref().unwrap_or("work")))
            .collect();
        candidates.sort_by(|a, b| {
            b.dispatched_at
                .partial_cmp(&a.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        candidates
    }

    /// Log tab: show the worker log for the selected pipeline issue.
    ///
    /// Prefers the live SSE stream when open for this issue's assignment
    /// (avoids the polling "Loading log…" flicker every cache-TTL seconds).
    /// Falls back to `get_activity_log` (local file or remote HTTP cache)
    /// when no SSE is active.
    ///
    /// Content items (the expensive parse) are cached in `log_items_cache`
    /// and rebuilt only when the assignment, line count, or wrap width
    /// changes (#399 scroll-perf).
    pub(crate) fn pipeline_log_list(&self) -> ListView {
        let mut items: Vec<ListItem> = Vec::new();
        let issue = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i));
        if let Some(issue) = issue {
            // #2342: candidates newest-dispatched first — see
            // `log_candidates_for_issue` for why a single issue can have
            // more than one (Gate A + N members' test-author rows sharing
            // a tracking issue's `issue_number`).
            let candidates = self.log_candidates_for_issue(issue);
            let pinned = self
                .pipeline_log_pinned_assignment
                .get(&pipeline_log_pin_key(issue));
            let assignment = pick_log_source(&candidates, pinned);

            // #2342: when more than one assignment could be "the" log for
            // this issue, render a picker header so the user can choose
            // instead of being stuck with whichever the auto-pick
            // heuristic above landed on. Silent (no rows) for the common
            // case of a single candidate, so ordinary work/review/smoke/
            // conflict-fix issues render exactly as before.
            if candidates.len() > 1 {
                items.push(kv_item(
                    "",
                    "── LOG SOURCE (press 1–9 to switch) ──",
                    Some(Color::rgb(160, 160, 180)),
                ));
                for (i, a) in candidates.iter().enumerate().take(9) {
                    let is_shown = assignment.is_some_and(|shown| shown.id == a.id);
                    let marker = if is_shown { "▶" } else { " " };
                    let color = if is_shown {
                        Color::rgb(140, 200, 140)
                    } else {
                        Color::rgb(140, 140, 160)
                    };
                    items.push(kv_item(
                        "",
                        &format!(
                            "  {marker} [{}] {} · {} · {}",
                            i + 1,
                            log_source_label(a),
                            a.machine,
                            a.status,
                        ),
                        Some(color),
                    ));
                }
                // #2342: the 1-9 keybind can only reach the first 9
                // candidates — flag the overflow instead of silently
                // capping so a milestone with many members mid-authoring
                // doesn't look like it only ever had 9 relevant assignments.
                if candidates.len() > 9 {
                    items.push(kv_item(
                        "",
                        &format!("  … +{} more (not reachable via 1-9)", candidates.len() - 9),
                        Some(Color::rgb(140, 140, 160)),
                    ));
                }
                items.push(kv_item("", "", None));
            }

            if let Some(a) = assignment {
                // Session elapsed header — always recomputed (time advances every
                // second even when no new log lines arrive).
                let now_secs = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs_f64())
                    .unwrap_or(0.0);
                let elapsed_header = match (a.dispatched_at, a.finished_at) {
                    (Some(start), Some(end)) => {
                        let secs = (end - start).max(0.0) as u64;
                        format!(
                            "  {} · {} · elapsed {}",
                            a.assignment_type.as_deref().unwrap_or("work"),
                            a.machine,
                            fmt_elapsed(secs)
                        )
                    }
                    (Some(start), None) => {
                        let secs = (now_secs - start).max(0.0) as u64;
                        format!(
                            "  {} · {} · running {}",
                            a.assignment_type.as_deref().unwrap_or("work"),
                            a.machine,
                            fmt_elapsed(secs)
                        )
                    }
                    _ => format!(
                        "  {} · {}",
                        a.assignment_type.as_deref().unwrap_or("work"),
                        a.machine
                    ),
                };
                items.push(kv_item(
                    "",
                    &elapsed_header,
                    Some(Color::rgb(120, 120, 140)),
                ));
                items.push(kv_item("", "", None));

                // #385: readable wrapped rendering.  Panel width is set by
                // the render path via `last_log_panel_cols` just before calling
                // this method — TUI backends store columns directly; GTK stores
                // pixels (large values mean wrapping won't fire, which is fine
                // since GTK handles layout internally).
                let wrap_width = self.last_log_panel_cols.get().max(40);

                // Use SSE from the watch pool if a stream exists for this
                // assignment (focused or background) — avoids the
                // HTTP-cache-TTL "Loading log…" flicker.
                if let Some(ctx) = self.watch_pool.get(&a.id) {
                    let sse = &ctx.sse;
                    if sse.lines.is_empty() && !sse.done {
                        items.push(kv_item(
                            "",
                            "  Connecting to log stream…",
                            Some(Color::rgb(140, 140, 140)),
                        ));
                    } else {
                        // #399/#787 scroll-perf: 3-way cache decision.
                        //   exact hit  → extend items from cache (zero parse).
                        //   can extend → parse only new lines, append (O(new)).
                        //   full build → parse all lines from scratch (O(total)).
                        let line_count = sse.lines.len();
                        // #899: the incremental cache-extend branch below slices
                        // `sse.line_times[old_count..]` with indices derived
                        // from `sse.lines.len()`. If a producer ever desyncs the
                        // two parallel vectors (lines ahead of line_times), that
                        // slice panics ("range start index N out of range").
                        // When they're not in lockstep, fall through to the
                        // bounds-safe FullBuild path (parse_sse_log_more reads
                        // times via `.get(i)`, so a length mismatch there is
                        // harmless). Producer-side this should never fire — the
                        // #899 fix keeps line_times in lockstep — but rendering
                        // must never panic on malformed state.
                        let times_synced = sse.line_times.len() == line_count;
                        // Determine the cache status in a scoped borrow so we
                        // can take `borrow_mut` below without a conflict.
                        enum CacheStatus { ExactHit, CanExtend(usize), FullBuild }
                        let status = {
                            let cache = self.log_items_cache.borrow();
                            match cache.as_ref() {
                                Some(c)
                                    if times_synced
                                        && c.assignment_id == a.id
                                        && c.wrap_width == wrap_width =>
                                {
                                    if c.line_count == line_count {
                                        CacheStatus::ExactHit
                                    } else if c.line_count < line_count {
                                        CacheStatus::CanExtend(c.line_count)
                                    } else {
                                        // line_count shrank — defensive full rebuild.
                                        CacheStatus::FullBuild
                                    }
                                }
                                _ => CacheStatus::FullBuild,
                            }
                        };
                        match status {
                            CacheStatus::ExactHit => {
                                let cache = self.log_items_cache.borrow();
                                items.extend(cache.as_ref().unwrap().items.iter().cloned());
                            }
                            CacheStatus::CanExtend(old_count) => {
                                // Parse only the new suffix, append to cached items.
                                let cached = {
                                    let mut cache = self.log_items_cache.borrow_mut();
                                    let c = cache.as_mut().unwrap();
                                    let new_items = parse_sse_log_more(
                                        &sse.lines[old_count..],
                                        &sse.line_times[old_count..],
                                        wrap_width,
                                        &mut c.parse_state,
                                    );
                                    c.items.extend(new_items);
                                    c.line_count = line_count;
                                    c.items.clone()
                                };
                                items.extend(cached);
                            }
                            CacheStatus::FullBuild => {
                                let mut state = LogParseState::default();
                                let content_items = parse_sse_log_more(
                                    &sse.lines,
                                    &sse.line_times,
                                    wrap_width,
                                    &mut state,
                                );
                                *self.log_items_cache.borrow_mut() = Some(LogItemsCache {
                                    assignment_id: a.id.clone(),
                                    line_count,
                                    wrap_width,
                                    items: content_items.clone(),
                                    parse_state: state,
                                });
                                items.extend(content_items);
                            }
                        }
                    }
                    if sse.done {
                        // #1954: a stream that ended without ever delivering
                        // a single recognizable log event (no line with a
                        // JSON "type" field — e.g. it died on connection
                        // errors before any turn data arrived, so `lines`
                        // holds only `[sse error] …` diagnostics or nothing
                        // at all) has no real content for the parse above to
                        // show. Defect-1 now renders those diagnostic lines
                        // verbatim instead of dropping them, but the actual
                        // log is still the thing worth showing — it's
                        // reachable over `GET /logs/{id}` (or the local
                        // file) for the life of the worker, so fall back to
                        // it rather than leaving the pane holding only a
                        // couple of error lines and a bare end-of-stream
                        // marker.
                        let has_real_content =
                            sse.lines.iter().any(|l| json_str(l, "type").is_some());
                        if !has_real_content {
                            items.extend(self.get_activity_log(&a.id, &a.machine));
                        }
                        items.push(kv_item(
                            "",
                            "  ── stream ended ──",
                            Some(Color::rgb(90, 90, 90)),
                        ));
                    }
                } else {
                    // For local logs, apply readable formatting directly.
                    // For remote/cached logs, fall back to get_activity_log.
                    let log_path = coord_dir().join("logs").join(format!("{}.log", a.id));
                    if let Ok(content) = std::fs::read_to_string(&log_path) {
                        // #399 scroll-perf: cache local-file parse by byte length.
                        let line_count = content.len();
                        let cache_valid = {
                            let cache = self.log_items_cache.borrow();
                            cache.as_ref().map_or(false, |c| {
                                c.assignment_id == a.id
                                    && c.line_count == line_count
                                    && c.wrap_width == wrap_width
                            })
                        };
                        if cache_valid {
                            let cache = self.log_items_cache.borrow();
                            items.extend(cache.as_ref().unwrap().items.iter().cloned());
                        } else {
                            let content_items = parse_log_content_readable(&content, wrap_width);
                            *self.log_items_cache.borrow_mut() = Some(LogItemsCache {
                                assignment_id: a.id.clone(),
                                line_count,
                                wrap_width,
                                items: content_items.clone(),
                                // File-based path uses exact-match caching; parse_state is
                                // unused here (file content is not parsed incrementally).
                                parse_state: LogParseState::default(),
                            });
                            items.extend(content_items);
                        }
                    } else {
                        items.extend(self.get_activity_log(&a.id, &a.machine));
                    }
                }
            } else {
                items.push(kv_item(
                    "",
                    "  (no assignment log available)",
                    Some(Color::rgb(100, 100, 100)),
                ));
            }
        } else {
            items.push(kv_item(
                "",
                "  (select an issue to view its log)",
                Some(Color::rgb(100, 100, 100)),
            ));
        }
        // Sticky-to-bottom: usize::MAX is the sentinel for "follow tail".
        // Compute the real offset here so draw_list gets a clamped value.
        let visible_rows = self.last_main_visible_rows.get().max(1);
        let scroll = if self.pipeline_detail_scroll == usize::MAX {
            items.len().saturating_sub(visible_rows)
        } else {
            self.pipeline_detail_scroll
        };
        // #302: measure the widest row so the rasteriser knows when content
        // overflows and should paint a horizontal scrollbar. Width = the 3-char
        // "   " indent the rows carry + the item's visible text width.
        let max_content_width = items.iter().map(|it| 3 + it.text.visible_width()).max();
        // Clamp the horizontal offset so it can't scroll past the content.
        let h_scroll = match max_content_width {
            Some(w) => self.pipeline_log_hscroll.min(w.saturating_sub(1)),
            None => 0,
        };
        ListView {
            id: WidgetId::new("pipeline-log"),
            title: None,
            items,
            selected_idx: 0,
            scroll_offset: scroll,
            has_focus: false,
            bordered: false,
            h_scroll,
            max_content_width,
            show_v_scrollbar: false,
        }
    }

    /// Count `[assistant]` turns in the local log for an assignment.
    /// Returns 0 when the log is not cached locally.
    pub(crate) fn turn_count_from_log(&self, assignment_id: &str) -> usize {
        let path = coord_dir()
            .join("logs")
            .join(format!("{}.log", assignment_id));
        let Ok(content) = std::fs::read_to_string(&path) else {
            return 0;
        };
        content
            .lines()
            .filter(|l| json_str(l, "type").as_deref() == Some("assistant"))
            .count()
    }
}

/// #2342: resolve which candidate a Pipeline issue's Log tab should show,
/// given its pinned choice (if any) — pinned, else running, else the
/// newest-dispatched (`candidates` is expected pre-sorted by
/// `log_candidates_for_issue`, newest first).
///
/// Shared by `pipeline_log_list` (what to render) and `ensure_log_tab_sse`
/// (which assignment's SSE stream to keep warm) so the two can never pick
/// different assignments — `ensure_log_tab_sse`'s doc comment already
/// states that invariant (its #308 "Loading log…" flicker note predates
/// #2342, but the requirement is the same: the pool entry must track
/// whatever's visible).
pub(crate) fn pick_log_source<'a>(
    candidates: &[&'a Assignment],
    pinned: Option<&String>,
) -> Option<&'a Assignment> {
    pinned
        .and_then(|id| candidates.iter().find(|a| &a.id == id).copied())
        .or_else(|| candidates.iter().find(|a| a.status == "running").copied())
        .or_else(|| candidates.first().copied())
}

/// #2342: the `pipeline_log_pinned_assignment` key for `issue` —
/// `(coord_repo, issue_number)`, matching the `(String, u64)` shape already
/// used by `pipeline_epic_expanded` / `board_epic_expanded` so two repos
/// that happen to share an issue number can't collide on the same pin. An
/// issue with no `coord_repo` keys under `String::new()`, which is harmless:
/// `log_candidates_for_issue` doesn't filter by repo either in that case.
pub(crate) fn pipeline_log_pin_key(issue: &PipelineIssue) -> (String, u64) {
    (issue.coord_repo.clone().unwrap_or_default(), issue.number)
}

/// #2342: friendly label for a Log-tab picker row. Reuses the "Gate A" /
/// "Acceptance Authoring" names already established by #1084's prereq
/// blocks (`append_gate_a_prereq_guidance_rows` /
/// `append_acceptance_authoring_prereq_guidance_rows` above) rather than
/// inventing new ones — an `Acceptance Authoring` row is further tagged
/// with the member issue it's for (`for_issue_number`) so multiple
/// members' rows on a milestone tracking issue's picker stay distinguishable.
/// Every other type (`work`, `review`, `smoke`, `conflict-fix`, `fix-N`)
/// is shown as-is — those are already self-explanatory and unchanged from
/// pre-#2342 Log tab behavior.
pub(crate) fn log_source_label(a: &Assignment) -> String {
    match a.assignment_type.as_deref().unwrap_or("work") {
        "mock-author" => "Gate A".to_string(),
        "test-author" => match a.for_issue_number {
            Some(n) => format!("Acceptance Authoring #{n}"),
            None => "Acceptance Authoring".to_string(),
        },
        other => other.to_string(),
    }
}
