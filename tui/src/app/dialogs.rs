//! Context menus, modal dialogs, and overlay widgets extracted from `app/mod.rs` (#744).
//!
//! **Import pattern:** `use super::*` is intentional — these methods live on `CoordApp`
//! and need the full parent namespace (all quadraui types, app-field types, and bindings
//! from other extracted modules). Pure-function submodules (`format.rs`, `data.rs`) use
//! explicit imports because their dependency surface is small and stable.
#[allow(unused_imports)]
use super::*;
// #2863: the attended intake item's label, referenced both by the menu
// builder (`approved.rs`) and by the dispatch-failure modal below, which
// points the operator at it as the local-only alternative.
use super::approved::APPROVED_ATTENDED_INTAKE_ITEM;

// ─── Right-click context menu (#259) ─────────────────────────────────────────

impl CoordApp {
    /// #260: classify a Board row by repo + issue number into a
    /// lifecycle bucket.  Drives which menu items appear at right-click
    /// time (Refine on Backlog rows, etc.).
    ///
    /// Cross-references both `data.open_issues` (for labels + state)
    /// and `data.assignments` (for "has assignments → In-flight").
    pub(crate) fn board_row_lifecycle(&self, repo_name: &str, issue_number: u64) -> BoardRowLifecycle {
        let issue = self
            .data
            .open_issues
            .iter()
            .find(|oi| oi.repo_name == repo_name && oi.number == issue_number);
        // #264: refinement assignments are conversational scoping, not
        // real work — they shouldn't make `board_row_lifecycle` flip
        // to In-flight or hide the Refine menu.  Mirrors the same
        // filter `IssueGroup::lifecycle_section` applies for section
        // bucketing.  Without this the right-click menu and action
        // bar both treated refined-once-then-rolled-back issues as
        // In-flight (no Refine option) even though they visually sit
        // in the Backlog section.
        let has_assignment = self.data.assignments.iter().any(|a| {
            a.repo == repo_name
                && a.issue_number == issue_number
                && a.assignment_type.as_deref() != Some("refinement")
        });
        let Some(issue) = issue else {
            // No cache row — fall back on assignment presence; without
            // either signal we don't know the state.
            return if has_assignment {
                BoardRowLifecycle::InFlight
            } else {
                BoardRowLifecycle::Unknown
            };
        };
        if issue.state == "closed" && has_assignment {
            return BoardRowLifecycle::Completed;
        }
        if has_assignment {
            return BoardRowLifecycle::InFlight;
        }
        // Open issue with no assignments — classify by labels.
        let has_refining = issue.labels.iter().any(|l| l == "status:refining");
        let has_ready = issue.labels.iter().any(|l| l == "status:ready");
        if has_refining {
            BoardRowLifecycle::Refining
        } else if has_ready {
            BoardRowLifecycle::Refined
        } else {
            BoardRowLifecycle::Backlog
        }
    }

    /// Build the menu item list for a right-click on a Board sidebar row.
    ///
    /// Items vary by lifecycle bucket (#260 onward):
    /// - **Backlog** → Refine
    /// - other states → just Copy + Refresh for now (subsequent issues
    ///   wire #261 Send to Pipeline, #262 Start, etc.).
    pub(crate) fn context_menu_items_for_board_row(
        &self,
        issue_number: Option<u64>,
        lifecycle: &BoardRowLifecycle,
        repo_name: Option<&str>,
    ) -> Vec<ContextMenuItem> {
        let mut items: Vec<ContextMenuItem> = Vec::new();
        // #628: the Board is Backlog → In-flight → Completed (the status:ready
        // "Refined" split is gone — it gated nothing). "Send to Pipeline"
        // (coord track) is how a Backlog issue enters the Pipeline as a tracked
        // card; offered on Backlog rows.
        if matches!(lifecycle, BoardRowLifecycle::Backlog) {
            items.push(ContextMenuItem::action(
                "send-to-pipeline",
                "Send to Pipeline",
            ));
            // #2494: "Add to drive queue" — `coord drive-queue add <repo>
            // <issue>`, this repo's own recommended dispatch path
            // (docs/DRIVE_QUEUE.md) and a genuinely different mechanism from
            // "Send to Pipeline" above (`coord track`, the label-based `coord
            // plan`/`approve` flow, with no drive-queue equivalent). Reuses
            // the exact same menu-item/dispatch machinery the Pipeline row's
            // `PipelineRowLifecycle::New` block already offers below —
            // `pipeline_menu_repo_issue` (the shared dispatch-side resolver
            // behind the `"drive-queue-add"`/`"drive-queue-row-remove"`/
            // `"drive-queue-add-on:<machine>"` action ids) already treats
            // `BoardRow` and `PipelineRow` targets identically, so no new
            // action ids or dispatch plumbing are needed here — only this
            // menu-content wiring. `offer_add: true` (queue-or-update, safe
            // to invoke more than once) gated on the same Backlog-only
            // lifecycle as "Send to Pipeline": queuing something already
            // in-flight doesn't make sense. Swaps to "Remove from drive
            // queue" for a row somehow already queued (e.g. added via `coord
            // drive-queue add` directly before ever visiting the TUI).
            items.extend(self.drive_queue_menu_items_for_pipeline_row(
                issue_number,
                repo_name,
                true,
            ));
            items.push(ContextMenuItem::separator());
        }
        // #661: the Board has NO direct dispatch. Choosing HOW to run an issue
        // (interactive vs automated) is a Pipeline concern — the Board only
        // pushes an issue into the Pipeline via "Send to Pipeline" above; from
        // Pipeline:New the Pipeline menu's Start (interactive)/(automated)
        // submenus pick the execution mode. (Reverses the #410 board "Send".)
        // #628: "Chat about issue" on EVERY issue row (any lifecycle) — a
        // human-attended session seeded with the issue's data that can answer
        // questions, sketch the UX, diagnose a stall, edit the issue, and send
        // it to Pending. Replaced the refine entries (and Troubleshoot on the
        // Pipeline). Placed after the lifecycle actions so the primary action
        // stays the default-selected item.
        if issue_number.is_some() {
            items.push(ContextMenuItem::action("chat-about-issue", "Chat about issue"));
            // #815/#1598: "View in Pipeline" — navigate from the Board to
            // the matching Pipeline entry.  Disabled when
            // `pipeline_jump_target` can't resolve a real, currently-
            // rendered row for it — the SAME query `jump_board_to_pipeline`
            // uses, so this can never again say "enabled" for an issue the
            // jump then fails to land on (#1598's regression: raw
            // `pipeline_issues` membership is broader than what actually
            // renders).
            let jump_result = issue_number
                .zip(repo_name)
                .map(|(num, repo)| self.pipeline_jump_target(repo, num))
                .unwrap_or(Err(PipelineNotVisible::Untracked));
            let mut jump_item =
                ContextMenuItem::action("jump-to-pipeline", "View in Pipeline");
            if let Err(reason) = jump_result {
                jump_item = jump_item.disabled_because(reason.menu_hint());
            }
            items.push(jump_item);
            items.push(ContextMenuItem::separator());
        }
        if let Some(num) = issue_number {
            items.push(ContextMenuItem::action(
                "copy-issue-number",
                &format!("Copy issue #{}", num),
            ));
            items.push(ContextMenuItem::separator());
        }
        items.push(ContextMenuItem::action("refresh", "Refresh").with_shortcut("r"));
        items
    }

    /// #2287 (ms-65 §8c): build the menu item list for a right-click on a
    /// Board document tab. Exact labels, taken verbatim from the tracking
    /// issue: **Close**, **Close others**, **Close all**, **Pin tab**.
    ///
    /// "Pin tab" is always rendered (never hidden — contract §8c's own base
    /// case right-clicks an already-pinned tab and still expects to find
    /// it, `tests/acceptance/ms-65/tabs_discoverability_2287.rs::
    /// right_clicking_a_tab_opens_a_menu_with_close_close_others_close_all_and_pin_tab`)
    /// but `disabled_because` when `is_pinned` — this codebase's reading of
    /// the contract's permissive "hidden or inert": a disabled item hit-
    /// tests as `ContextMenuHit::Inert` (`quadraui::context_menu`), so a
    /// click on it dispatches no action and leaves the tab set untouched,
    /// exactly what "inert" means.
    pub(crate) fn context_menu_items_for_board_doc_tab(&self, is_pinned: bool) -> Vec<ContextMenuItem> {
        let mut items = vec![
            ContextMenuItem::action("close-doc-tab", "Close"),
            ContextMenuItem::action("close-other-doc-tabs", "Close others"),
            ContextMenuItem::action("close-all-doc-tabs", "Close all"),
        ];
        let pin_item = ContextMenuItem::action("pin-doc-tab", "Pin tab");
        items.push(if is_pinned {
            pin_item.disabled_because("already pinned")
        } else {
            pin_item
        });
        items
    }

    /// #262: classify a Pipeline row into the umbrella's three sections
    /// plus an "Other" catch-all.  Drives the right-click menu so Start
    /// only appears when the row is genuinely dispatch-ready.
    ///
    /// Maps the existing 5-state `pipeline_lifecycle_section` to the
    /// 4-state lifecycle:
    /// - `"pending"` (coord + status:ready, no assignments) → **New**
    /// - `"in-progress"` (has assignments)                   → **InProgress**
    /// - `"done"`     (closed + assignments)                 → **Done**
    /// - `"refining"` / `"new"`                              → **Other**
    /// #271 part 2 follow-up: extract a PR number from a Pipeline
    /// issue.  Looks up the `merge_queue` entry — it's populated by
    /// `coord merge` when the PR is opened, and `pr_number` is on the
    /// entry directly.  Returns `None` when no PR has been opened yet
    /// (Plan- or Work-stage only, before a merge queue entry exists).
    pub(crate) fn pipeline_pr_number(&self, issue: &PipelineIssue) -> Option<i64> {
        self.data
            .merge_queue
            .iter()
            .find(|m| m.issue_number == Some(issue.number) && m.repo_github == issue.repo_slug)
            .and_then(|m| m.pr_number)
    }

    /// #271 part 2 follow-up: cache-aware accessor for a PR's title /
    /// body / files-changed.  Returns `Some(cached)` on hit; on miss,
    /// kicks off a background `gh pr view` (only once per key — repeat
    /// calls coalesce on the in-flight `Receiver`) and returns None
    /// while the fetch is in flight.  Drained by
    /// `poll_pending_pr_fetches` each tick.
    pub(crate) fn pr_info_for_issue(&self, issue: &PipelineIssue) -> Option<FetchedPr> {
        let pr_number = self.pipeline_pr_number(issue)?;
        let key = (issue.repo_slug.clone(), pr_number);
        // Cache hit — done.
        if let Some(cached) = self.fetched_prs_cache.borrow().get(&key).cloned() {
            return Some(cached);
        }
        // Already fetching — let the poll loop pick it up.
        if self.pending_pr_fetches.borrow().contains_key(&key) {
            return None;
        }
        // Kick off a fresh background fetch.
        let rx = spawn_pr_fetch(issue.repo_slug.clone(), pr_number);
        self.pending_pr_fetches.borrow_mut().insert(key, rx);
        None
    }

    /// Drain completed `gh pr view` fetches into the in-memory cache.
    /// Returns `true` when at least one fetch resolved — caller should
    /// redraw so the Test guidance block picks up the new info.
    pub(crate) fn poll_pending_pr_fetches(&self) -> bool {
        use std::sync::mpsc::TryRecvError;
        let mut changed = false;
        // Receive each ready message exactly once — `try_recv` consumes it, so
        // capturing the payload here (rather than re-receiving in a second pass)
        // is required; the second receive would always see `Disconnected` and
        // silently drop the fetched PR info.
        let mut done: Vec<(String, i64)> = Vec::new();
        let mut resolved: Vec<((String, i64), FetchedPr)> = Vec::new();
        {
            let pending = self.pending_pr_fetches.borrow();
            for (key, rx) in pending.iter() {
                match rx.try_recv() {
                    Ok(Ok(fp)) => {
                        resolved.push((key.clone(), fp));
                        done.push(key.clone());
                    }
                    Ok(Err(_)) | Err(TryRecvError::Disconnected) => {
                        // Fetch failed — drop the receiver; next render retries.
                        done.push(key.clone());
                    }
                    Err(TryRecvError::Empty) => {}
                }
            }
        }
        for key in &done {
            self.pending_pr_fetches.borrow_mut().remove(key);
        }
        for (key, fp) in resolved {
            self.fetched_prs_cache.borrow_mut().insert(key, fp);
            changed = true;
        }
        changed
    }

    /// #876: Build the Pipeline Summary tab `ListView` for the selected issue
    /// directly from in-memory board assignments — no GitHub shellout.
    ///
    /// Sources: `test_state`, `test_reason`, `review_verdict`, `review_findings`,
    /// `failure_reason`, cost, tokens, timing, machine/model, `is_interactive`.
    /// Gracefully omits fields absent from older daemon rows.
    /// #2417: assignments dispatched BY `assignment_id`'s own turn — every
    /// row whose `dispatched_by_assignment_id` points back at it (e.g. a
    /// `type="work"` session shelling out to `coord acceptance author` or
    /// `coord fix <other-id>` from its own bash tool). Deliberately GLOBAL,
    /// not issue-scoped: a dispatched sibling typically lands on a
    /// DIFFERENT `issue_number` from the origin row (a test-author's
    /// `issue_number` is the milestone's tracking issue; a `coord fix`
    /// target is whatever unrelated assignment needed the fix), so the
    /// per-issue filter `build_board_summary_list_view` otherwise uses
    /// would never find it. Sorted oldest-first for a stable render order
    /// in the rare case of more than one dispatch from the same turn.
    pub(crate) fn dispatched_children<'a>(&'a self, assignment_id: &str) -> Vec<&'a Assignment> {
        let mut children: Vec<&Assignment> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.dispatched_by_assignment_id.as_deref() == Some(assignment_id))
            .collect();
        children.sort_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        children
    }

    pub(crate) fn pipeline_summary_list(&self) -> ListView {
        let issue = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i));
        let Some(issue) = issue else {
            return plain_list("pipeline-summary", "  (no issue selected)", 0);
        };
        self.build_board_summary_list_view(issue, self.pipeline_detail_scroll)
    }

    /// #876: Build the Pipeline Summary tab `ListView` directly from board-layer
    /// assignments.  Replaces the old `spawn_comments_fetch` / GitHub shellout
    /// path so `test_reason`, `review_findings`, and other verdict data from the
    /// board are always visible — even when they were never posted as issue
    /// comments.
    ///
    /// Layout:
    ///   1. Status strip:  Work ✓  Test ✗  Review —  Merge —
    ///   2. Per-assignment entries (oldest → newest, matching stage order):
    ///      ● <type>  <machine>  <status/verdict>  <duration>  <cost>  <model>
    ///         <reason text (test_reason / review_findings / failure_reason)>
    pub(crate) fn build_board_summary_list_view(
        &self,
        issue: &PipelineIssue,
        scroll_offset: usize,
    ) -> ListView {
        let dim = Color::rgb(80, 80, 80);
        let muted = Color::rgb(160, 160, 160);
        let machine_color = Color::rgb(130, 170, 210);
        let model_color = Color::rgb(100, 160, 100);
        let cost_color = Color::rgb(180, 160, 100);
        let duration_color = Color::rgb(110, 110, 130);
        let reason_color = Color::rgb(210, 210, 210);
        let pr_color = Color::rgb(100, 150, 200);
        // #2417: distinct from pr_color so a dispatched-sibling link never
        // reads as "this row's own PR" at a glance.
        let dispatched_color = Color::rgb(160, 130, 210);

        // --- collect assignments for this issue (oldest → newest) ---
        let local_repo = issue.coord_repo.as_deref();
        let mut assignments: Vec<&Assignment> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == issue.number)
            .filter(|a| local_repo.map_or(true, |r| a.repo == r))
            .collect();

        // Sort oldest-first so entries read Work → Fix → Review chronologically.
        assignments.sort_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        if assignments.is_empty() {
            return plain_list(
                "pipeline-summary",
                "  No board sessions for this issue yet.",
                scroll_offset,
            );
        }

        let mut items: Vec<ListItem> = Vec::new();

        // #818: The inline status strip is removed from the Summary tab body.
        // The universal pinned stage strip (rendered via `build_pipeline_widget`
        // + `pipeline_detail_pv_rect_strip`) now sits above every non-Overview
        // tab, so duplicating it here would show the strip twice.

        // #1022: flat-row rule — the Test-gate verdict/reason belongs on the
        // Test (smoke) row, never the Work/Fix row that actually recorded it
        // in the DB (`test_state`/`test_reason` live on the WORK assignment —
        // see `coord/state.py::record_test_verdict`).
        //
        // The reason must render once *per verdict source*, not once globally:
        // a single commit re-tested N times shares one source (reason shows
        // once), but N independent fix iterations each own a distinct
        // `test_reason` on their own work assignment and must each stay visible
        // under their own iteration's last Test row. So we key on
        // `smoke_test_source_id` (which assignment owns the reason) rather than
        // a single global last-smoke-row gate.
        let has_any_smoke_row = assignments
            .iter()
            .any(|a| a.assignment_type.as_deref().unwrap_or("work") == "smoke");

        // --- Per-assignment entries ---
        for (idx, a) in assignments.iter().enumerate() {
            // Skip scoping sessions (refinement, chat, new-issue-chat) that are
            // not pipeline execution.
            let atype = a.assignment_type.as_deref().unwrap_or("work");
            if matches!(atype, "refinement" | "new-issue-chat" | "chat" | "test-chat") {
                continue;
            }

            // #1022: relabel "smoke" → "Test" locally in the Summary view so
            // the stage name matches the pipeline gate name ("Test").  The shared
            // `session_type_label` in mod.rs is NOT changed (it is tested and
            // used by other views).
            let type_label = if atype == "smoke" {
                "Test"
            } else {
                session_type_label(atype)
            };
            // #1022: a Test (smoke) row's badge shows the Test-gate verdict
            // from the work/fix assignment it actually tested (linked via
            // `review_of_assignment_id`, or the latest verdict-bearing
            // work-like assignment as a defensive fallback) — falling back
            // to the smoke row's own status only when no verdict has been
            // recorded anywhere.
            let (badge_text, badge_color) = if atype == "smoke" {
                let (verdict, _) = resolve_smoke_test_verdict(a, &assignments);
                match verdict {
                    Some("passed") => ("passed ✓".to_string(), Color::rgb(120, 200, 120)),
                    Some("failed") => ("failed ✗".to_string(), Color::rgb(220, 70, 70)),
                    Some("skipped") => ("skipped ↷".to_string(), Color::rgb(150, 150, 150)),
                    _ => assignment_status_badge(a),
                }
            } else {
                assignment_status_badge(a)
            };

            // Duration: finished_at - dispatched_at.
            let dur_str = match (a.dispatched_at, a.finished_at) {
                (Some(s), Some(e)) if e > s => fmt_dur((e - s) as u64),
                _ => String::new(),
            };

            // Cost.
            let cost_str = if a.is_interactive {
                "Max".to_string()
            } else {
                a.cost_usd
                    .map(|c| format!("${:.2}", c))
                    .unwrap_or_default()
            };

            // #1022: relative completion timestamp ("Xm ago") so the operator
            // can see at a glance when each stage finished.
            let ago_str = a.finished_at.map(format_unix_time).unwrap_or_default();

            // Header row: ● <type>  <machine>  <badge>  [duration]  [cost]  [model]  [ago]
            let mut spans: Vec<StyledSpan> = vec![
                StyledSpan::with_fg("● ", muted),
                StyledSpan::with_fg(format!("{:<10}", type_label), muted),
                StyledSpan::with_fg(format!("  {:<14}", &a.machine), machine_color),
                StyledSpan::with_fg(format!("  {}", badge_text), badge_color),
            ];
            if !dur_str.is_empty() {
                spans.push(StyledSpan::with_fg(format!("  {}", dur_str), duration_color));
            }
            if !cost_str.is_empty() {
                spans.push(StyledSpan::with_fg(format!("  {}", cost_str), cost_color));
            }
            if let Some(m) = a.model.as_deref() {
                spans.push(StyledSpan::with_fg(format!("  {}", m), model_color));
            }
            if !ago_str.is_empty() {
                spans.push(StyledSpan::with_fg(format!("  {}", ago_str), duration_color));
            }
            items.push(ListItem {
                text: StyledText { spans },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            });

            // Reason inline: test_reason for failed tests; review_findings for
            // reviews; failure_reason for hard-failed assignments.
            //
            // #1022: flat-row rule — render each verdict source's test reason
            // exactly once, under the LAST Test row that displays that source.
            // A Test row shows its reason iff no *later* Test row resolves to
            // the same source (so a re-tested commit's shared reason renders
            // once, while each independent fix iteration's own reason stays
            // visible under its own Test row). Work/Fix rows only fall back to
            // their own `test_reason` when the issue has no Test row at all
            // (e.g. a bare `coord test <id>` verdict recorded with no smoke
            // session to carry it) — otherwise #865's "the reason must be
            // visible somewhere" guarantee would silently regress.
            let reason = if atype == "smoke" {
                let source_id = smoke_test_source_id(a, &assignments);
                let is_last_for_source =
                    !assignments.iter().skip(idx + 1).any(|other| {
                        other.assignment_type.as_deref().unwrap_or("work") == "smoke"
                            && smoke_test_source_id(other, &assignments) == source_id
                    });
                if is_last_for_source {
                    let (_, linked_reason) = resolve_smoke_test_verdict(a, &assignments);
                    linked_reason
                        .map(|s| s.to_string())
                        .filter(|s| !s.trim().is_empty())
                        .unwrap_or_else(|| board_assignment_reason(a))
                } else {
                    String::new()
                }
            } else if has_any_smoke_row {
                board_assignment_reason_sans_test(a)
            } else {
                board_assignment_reason(a)
            };
            if !reason.is_empty() {
                for line in reason.lines().take(8) {
                    let s: String = line.chars().take(200).collect();
                    if s.trim().is_empty() {
                        continue;
                    }
                    items.push(ListItem {
                        text: StyledText {
                            spans: vec![StyledSpan::with_fg(format!("   {}", s), reason_color)],
                        },
                        icon: None,
                        detail: None,
                        decoration: Decoration::Normal,
                    });
                }
            }

            // PR URL.
            if let Some(ref url) = a.pr_url {
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(format!("   {}", url), pr_color)],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Normal,
                });
            }

            // #2417: this row dispatched an independent sibling assignment
            // from its own turn (`coord acceptance author`, `coord fix`,
            // ...) — surface it as a visible link/status right here instead
            // of leaving the operator to grep the raw worker transcript for
            // the printed "Dispatched ... to ..." line and manually
            // cross-reference a second `coord log`.
            for child in self.dispatched_children(&a.id) {
                let (child_badge, child_color) = assignment_status_badge(child);
                let child_type = session_type_label(
                    child.assignment_type.as_deref().unwrap_or("work"),
                );
                let short_id: String = child.id.chars().take(12).collect();
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![
                            StyledSpan::with_fg(
                                format!(
                                    "   → dispatched {} {} on {}",
                                    child_type, short_id, child.machine
                                ),
                                dispatched_color,
                            ),
                            StyledSpan::with_fg(format!("  {}", child_badge), child_color),
                        ],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Normal,
                });
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

    pub(crate) fn pipeline_row_lifecycle(&self, issue: &PipelineIssue) -> PipelineRowLifecycle {
        match self.pipeline_lifecycle_section(issue) {
            // #628: a pre-work tracked issue is "new" now — status:ready and
            // status:refining no longer split separate Pending / Refining
            // buckets, so "new" (not "pending") is the New menu state.
            "new" | "pending" => PipelineRowLifecycle::New,
            "in-progress" => PipelineRowLifecycle::InProgress,
            "done" => PipelineRowLifecycle::Done,
            _ => PipelineRowLifecycle::Other,
        }
    }

    /// Build the menu item list for a right-click on a Pipeline sidebar row.
    ///
    /// New rows (coord + status:ready, no assignments) get the two
    /// Start variants from #262 — Start with Plan (dispatches a Plan
    /// worker first, gated by `coord approve-plan`) and Skip Plan
    /// (dispatches Work directly).  Other states omit them.
    /// True when the row identified by `issue_number` has at least one
    /// review-typed assignment with `verdict='request-changes'`.  Drives
    /// the "Address review findings" menu / action-bar item — only
    /// shown when there's actually a review for the user to address.
    pub(crate) fn selected_row_has_request_changes_for(&self, issue_number: Option<u64>) -> bool {
        let Some(num) = issue_number else {
            return false;
        };
        // Match by issue number across both review and work assignments;
        // the link is via review_of_assignment_id.  Filter the review
        // pool by issue_number directly — cheaper than walking the
        // merge_queue too.
        let work_ids: Vec<&str> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == num)
            .filter(|a| a.assignment_type.as_deref() == Some("work"))
            .filter_map(|a| Some(a.id.as_str()))
            .collect();
        self.data
            .assignments
            .iter()
            .filter(|a| a.assignment_type.as_deref() == Some("review"))
            .filter(|a| a.review_verdict.as_deref() == Some("request-changes"))
            .any(|a| {
                a.review_of_assignment_id
                    .as_deref()
                    .map(|id| work_ids.iter().any(|w| *w == id))
                    .unwrap_or(false)
            })
    }

    pub(crate) fn context_menu_items_for_pipeline_row(
        &self,
        issue_number: Option<u64>,
        lifecycle: &PipelineRowLifecycle,
        // Coord-local repo name for the selected Pipeline row.  When `Some`,
        // the live/zombie session gates are scoped to that repo so that
        // repo-a/#N and repo-b/#N cannot cross-contaminate the menu (#983).
        // Falls back to the repo-agnostic helpers only when `None`.
        repo_name: Option<&str>,
    ) -> Vec<ContextMenuItem> {
        let mut items: Vec<ContextMenuItem> = Vec::new();
        // #467 / #607: "Start" launchers grouped into pull-right submenus.
        // Interactive (Claude Max, human-attended) and automated (claude -p,
        // coordinator-dispatched) are separate parents so the execution model
        // is explicit at a glance.
        // #1223: resolved up front (was computed further down, just before
        // the "Audit outcomes" item) so the Start (interactive)/(automated)
        // submenu construction below can nest the per-issue JIT
        // acceptance-authoring actions alongside Work/Review/Fix/Testing/
        // Merge — discoverable in the same place as the rest of the
        // pipeline instead of a flat top-level item (#1223 gap 1).
        let epic_issue: Option<&PipelineIssue> = issue_number
            .and_then(|n| self.pipeline_issues.iter().find(|iss| iss.number == n));
        let member_tracking_issue =
            epic_issue.and_then(|iss| self.milestone_tracking_issue_for(iss));

        if issue_number.is_some() {
            // #486 Leg 4 UX: when a live interactive session already exists for
            // this issue, the only sensible interactive action is to reattach —
            // every mode would attach to the same session, and you can't start a
            // review/fix while the work session is still live.  Offer one clear
            // "Reattach" item (flat, no submenu) and hide the Start variants.
            //
            // #727: extend to zombie sessions — a tmux session that is still
            // alive (shown as In-progress:Live by `issue_session_is_live`) but
            // whose board assignment has already been finalised (`done`/`failed`)
            // or is absent.  The zombie case adds "Reattach" alongside the Start
            // actions rather than hard-locking the menu, so the operator can
            // still access Kill/Diagnose/Drop-to-backlog (#676 guard).
            // #983: gate on the repo-precise variants when the row's repo is
            // known, so a live/zombie session for repo-a/#N does not falsely
            // show "Reattach" on repo-b/#N's menu.  Falls back to the
            // repo-agnostic helpers only when repo_name is unknown.
            let has_running = issue_number
                .map(|n| match repo_name {
                    Some(r) => self.issue_has_live_session_for_repo(n, r),
                    None => self.issue_has_live_session(n),
                })
                .unwrap_or(false);
            let has_zombie = !has_running
                && issue_number
                    .map(|n| match repo_name {
                        Some(r) => self.issue_has_any_discovered_session_for_repo(n, r),
                        None => self.issue_has_any_discovered_session(n),
                    })
                    .unwrap_or(false);
            // #1398: a live `coord drive --tmux` run is a THIRD kind of
            // liveness, independent of the interactive-session axis above —
            // it dispatches Work/Test/Review/Merge itself, so every "Start …"
            // action would race it for the same gate (offering "Start
            // testing" mid-drive is the two-writers case the issue calls
            // out). Only checked when `repo_name` is known: a drive session
            // is always repo-scoped (unlike the interactive fallback above,
            // there is no repo-agnostic variant to fall back to).
            let has_live_drive = issue_number
                .zip(repo_name)
                .map(|(n, r)| self.issue_has_live_drive(n, r))
                .unwrap_or(false);
            if has_running {
                // Running session — collapse the entire launcher block to a
                // single Reattach item; everything else is unreachable anyway.
                items.push(ContextMenuItem::action(
                    "reattach-live-session",
                    "Reattach to live session",
                ));
            } else if has_live_drive {
                // Driving — collapse to Attach/Stop; every manual Start
                // action is unreachable (coord drive dispatches them itself).
                items.push(ContextMenuItem::action("attach-drive", "Attach to drive"));
                items.push(ContextMenuItem::action("stop-drive", "Stop drive"));
            } else {
                // Zombie session — offer Reattach alongside Start launchers so
                // the operator can reach both the still-alive tmux session AND
                // the normal pipeline actions.
                if has_zombie {
                    items.push(ContextMenuItem::action(
                        "reattach-live-session",
                        "Reattach to live session",
                    ));
                }

                // ── Start (interactive) submenu ───────────────────────────
                let mut interactive_children: Vec<ContextMenuItem> = Vec::new();
                interactive_children.push(ContextMenuItem::action(
                    "start-work-interactive",
                    "Work",
                ));
                interactive_children.push(ContextMenuItem::action(
                    "start-plan-interactive",
                    "Plan",
                ));
                // #1104: Continue shown only when the issue's LATEST work
                // attempt failed outright (worker-reported, no test/review
                // gate — that's Fix's territory) while leaving a real branch
                // behind. Additive, mirroring the Fix precedent: Work is left
                // unchanged (always forks fresh) rather than auto-swapping its
                // behavior based on hidden state.
                if self.selected_failed_work_aid_with_branch().is_some() {
                    interactive_children.push(ContextMenuItem::action(
                        "start-continue-interactive",
                        "Continue",
                    ));
                }
                // #539: Review gated on a completed work assignment.
                let mut review_item = ContextMenuItem::action("start-review-interactive", "Review");
                review_item.disabled = self.selected_completed_work_aid().is_none();
                interactive_children.push(review_item);
                // Leg 3 (#517 / #581): Fix shown only when a request-changes
                // review OR a test-failure exists for this issue.
                if self.selected_row_has_request_changes_for(issue_number)
                    || self.selected_test_failed_work_aid().is_some()
                {
                    interactive_children.push(ContextMenuItem::action(
                        "start-fix-interactive",
                        "Fix",
                    ));
                }
                // Leg 3c / A3 (#517 / #581 / #306): Testing and Merge gated on
                // a completed work assignment.
                let mut test_item = ContextMenuItem::action("start-testing-interactive", "Testing");
                test_item.disabled = self.selected_completed_work_aid().is_none();
                interactive_children.push(test_item);
                let mut merge_item = ContextMenuItem::action("start-merge-interactive", "Merge");
                merge_item.disabled = self.selected_completed_work_aid().is_none();
                interactive_children.push(merge_item);

                // #1223 (docs/ORACLE_LOOP.md, #1173): the JIT test-authoring
                // track's human-attended sibling nests here too — same
                // Start (interactive) submenu as Work/Review/Fix/Testing/
                // Merge, instead of a flat top-level item. NOT epic-gated:
                // applies to ordinary member issues of a milestone's `##
                // Work order`, resolved via `milestone_tracking_issue_for`.
                // Gated on the milestone's Gate A contract existing on disk
                // — the independent test-author reads it from its own
                // checkout (#931).
                if let Some(iss) = epic_issue {
                    if member_tracking_issue.is_some() {
                        let mut author_interactive_item = ContextMenuItem::action(
                            "author-acceptance-tests-interactive",
                            "Author acceptance tests (interactive)",
                        );
                        author_interactive_item.disabled = !self.gate_a_contract_exists_for(iss);
                        interactive_children.push(author_interactive_item);
                    }
                }

                items.push(ContextMenuItem::parent("Start (interactive)", interactive_children));

                // ── Start (automated) submenu ─────────────────────────────
                // #leg1: non-interactive (claude -p) dispatch.  Claim-check in
                // dispatch_pipeline_work/plan refuses a duplicate on an already-
                // active issue gracefully.
                // `start-skip-plan` = work directly; `start-with-plan` = plan-then-work.
                // #684: `start-merge-automated` = headless merge via the existing
                // merge queue (`coord merge --order <aid>`).  Gated on a completed
                // work assignment; disabled when an interactive --merge-of session
                // is already running for this issue (branch-race guard).
                let mut automated_children = vec![
                    ContextMenuItem::action("start-skip-plan", "Work"),
                    ContextMenuItem::action("start-with-plan", "Plan"),
                ];
                let mut auto_merge_item =
                    ContextMenuItem::action("start-merge-automated", "Merge");
                auto_merge_item.disabled = self.selected_completed_work_aid().is_none()
                    || issue_number
                        .map(|n| self.has_active_interactive_merge_for_issue(n))
                        .unwrap_or(false);
                automated_children.push(auto_merge_item);

                // #1223: the JIT test-authoring track's headless dispatch +
                // its "record acceptance" trust-gate re-run nest into Start
                // (automated) alongside Work/Plan/Merge — same gating as the
                // interactive sibling above.
                if let Some(iss) = epic_issue {
                    if member_tracking_issue.is_some() {
                        let mut author_item = ContextMenuItem::action(
                            "author-acceptance-tests",
                            "Author acceptance tests",
                        );
                        author_item.disabled = !self.gate_a_contract_exists_for(iss);
                        automated_children.push(author_item);

                        let mut record_item =
                            ContextMenuItem::action("record-acceptance", "Record acceptance");
                        record_item.disabled = self.selected_completed_work_aid().is_none();
                        automated_children.push(record_item);
                    }
                }

                items.push(ContextMenuItem::parent("Start (automated)", automated_children));

                // #1398: "Drive (automated)" — hands the WHOLE Work → Test →
                // Review → Merge sequence to the automated driver, unlike the
                // single-stage items above. Flat, not nested, for visibility
                // — this is the marquee one-click entry point the issue asks
                // for. Only offered when `has_running`/`has_live_drive` are
                // both false (this whole `else` branch) — like the "Start
                // (automated)" parent item right above it, `has_zombie` does
                // NOT gate this: it still appears alongside "Reattach to live
                // session" when a zombie session exists.
                //
                // #2634: this used to launch `coord drive --tmux` in a LOCAL
                // PTY — a driver that runs correctly but writes nothing to
                // the daemon's `drive_queue` table, so it could never appear
                // in the Queue panel and died with the operator's laptop.
                // The name promised "the automated driver" while the
                // mechanism was a local, ephemeral one; the durable,
                // board-backed queue (visible in the Queue panel, survives a
                // restart, subject to the queue's ordering/overlap/hold
                // machinery) is what "the automated driver" should mean, so
                // this item now dispatches through the SAME
                // `"drive-queue-add"` seam the Board/Pipeline "Add to drive
                // queue" items use (`dispatch_context_menu_action`).
                items.push(ContextMenuItem::action("start-drive", "Drive (automated)"));
                // #2634: the OLD behavior, demoted and distinctly named so
                // it can never again be mistaken for the durable queue path
                // above — a local `coord drive --tmux` run that dies with
                // this machine/tmux and is invisible to the Queue panel by
                // construction (see `drive.rs`'s module doc). Kept for an
                // operator deliberately choosing a local, unqueued run.
                items.push(ContextMenuItem::action(
                    "start-drive-local",
                    "Drive locally (tmux, this machine)",
                ));

                // #685: "Set test mode" — pick smoke vs auto policy for headless Work.
                if let Some(num) = issue_number {
                    // Find current mode from pipeline issue labels.
                    let current_mode = self
                        .pipeline_issues
                        .iter()
                        .find(|iss| iss.number == num)
                        .and_then(|iss| {
                            if iss.all_labels.iter().any(|l| l == "test-mode:auto") {
                                Some("auto")
                            } else if iss.all_labels.iter().any(|l| l == "test-mode:smoke") {
                                Some("smoke")
                            } else {
                                None
                            }
                        });
                    let mode_suffix = match current_mode {
                        Some("auto") => " (auto)",
                        Some("smoke") => " (smoke)",
                        _ => "",
                    };
                    items.push(ContextMenuItem::action(
                        "set-test-mode",
                        &format!("Set test mode…{}", mode_suffix),
                    ));
                }
            }
            // #628: "Chat about issue" on EVERY pipeline row, any lifecycle — a
            // human-attended session seeded with the issue's data: ask
            // questions, sketch the UX, diagnose a stall, edit the issue, send it
            // to Pending. Subsumes the old (InProgress-only) Troubleshoot.
            items.push(ContextMenuItem::action("chat-about-issue", "Chat about issue"));
            // Milestone Outcome Audit Phase 1 (#885): "Audit outcomes" —
            // human-attended READ-ONLY milestone-outcome analyst — only for
            // rows carrying the `epic` label (a milestone's tracking issue).
            // `epic_issue` itself is resolved up top now (#1223), shared
            // with the Start submenu nesting above.
            // #1198: case-insensitive — unified via `labels_carry_epic_label`
            // (was a case-sensitive `== "epic"` that silently dropped these
            // actions for an `Epic`/`EPIC`-cased label).
            let is_epic_row = epic_issue
                .map(|iss| labels_carry_epic_label(&iss.all_labels))
                .unwrap_or(false);
            if is_epic_row {
                items.push(ContextMenuItem::action("audit-outcomes", "Audit outcomes"));
                // #1059 (docs/ORACLE_LOOP.md, #930/#947): Gate A dispatch +
                // sign-off for the milestone this epic tracks. "Dispatch Gate
                // A mock" runs `coord acceptance mock <repo> <tracking_issue>`
                // headlessly (an independent mock-author agent renders the
                // viewable mock + writes tests/acceptance/ms-NN/contract.md —
                // the CLI's own claim-detection refuses a duplicate dispatch).
                // "View Gate A mock (PR)" opens that worker's PR in the
                // browser (same mechanism as the Done-lifecycle "Open PR"
                // below) so a human can read contract.md and review/merge
                // the branch — the Test → Review → Merge round trip that
                // constitutes sign-off — before `coord acceptance author`
                // (out of scope here) is run against the merged contract.
                // #2501: the PR route was NEVER a way to see the rendered
                // mock(s) themselves — GitHub's "Files changed" view renders
                // `.html` as a source diff, not a live page. "View Gate A
                // mock (local)" sits beside it for that: it fast-forward-
                // pulls the local checkout's default branch (never forcing,
                // never touching a dirty tree — see
                // `pull_default_branch_ff_only`) and then opens
                // tests/acceptance/ms-NN/mocks/index.html from disk via a
                // `file://` URL — a `.html` file IS MIME-associated with the
                // OS's default browser, so this reliably renders there.
                // #2512 amendment: until `mocks/index.html` exists (older
                // checkouts, or a mock-author run predating #2512), this
                // falls back to opening the raw `mocks/` DIRECTORY instead —
                // a directory has no such MIME association, so on most
                // desktops that `file://` URL opens in the OS's file
                // manager, not the browser; the operator is one extra click
                // (pick an `.html` file) from the rendered mock, not looking
                // at it directly. Interim and acceptable per that amendment,
                // but worth being precise about here rather than implying
                // both paths land in the browser equally.
                items.push(ContextMenuItem::action(
                    "dispatch-gate-a-mock",
                    "Dispatch Gate A mock",
                ));
                let mut view_gate_a_item =
                    ContextMenuItem::action("view-gate-a-mock", "View Gate A mock (PR)");
                view_gate_a_item.disabled =
                    epic_issue.and_then(|iss| self.pipeline_pr_number(iss)).is_none();
                items.push(view_gate_a_item);
                let mut view_gate_a_local_item = ContextMenuItem::action(
                    "view-gate-a-mock-local",
                    "View Gate A mock (local)",
                );
                view_gate_a_local_item.disabled = !epic_issue
                    .map(|iss| self.gate_a_mocks_dir_exists_for(iss))
                    .unwrap_or(false);
                items.push(view_gate_a_local_item);
                // #2063: the sign-off itself, right beside the 👁 that opens
                // the thing being signed off on — reviewing and recording are
                // one gesture. Until now "merging that PR is what satisfies
                // Gate A" was a convention nothing enforced; these record a
                // board verdict (`coord gate-a`) that
                // `coord.milestone_dispatch.issue_oracle_ready` refuses
                // dispatch without.
                items.push(ContextMenuItem::action(
                    "approve-gate-a",
                    "Approve Gate A",
                ));
                items.push(ContextMenuItem::action(
                    "request-gate-a-changes",
                    "Request Gate A changes",
                ));
                // #2513 (PDR-5): "Publish mocks to portal" — on-demand
                // counterpart to PDR-3's merge-triggered auto-push, for
                // iterating on a local `--amend` before it's merged, or
                // re-publishing after a manual edit, without a full PR
                // round trip. Same "can't dispatch, so don't offer it"
                // posture as the rest of this menu: disabled when there is
                // nothing on THIS machine's local checkout to upload yet
                // (`gate_a_mocks_dir_exists_for`, the mocks-dir sibling of
                // `gate_a_contract_exists_for` right above it). A missing
                // portal link (PDR-1, `coord portal link`) is not
                // client-visible here — `coord portal publish-mocks`
                // itself fails loud with that exact remedy rather than a
                // silent no-op, so the item stays enabled and the CLI
                // surfaces that failure as a toast via the usual path.
                let mut publish_mocks_item = ContextMenuItem::action(
                    "publish-mocks-to-portal",
                    "Publish mocks to portal",
                );
                publish_mocks_item.disabled = !epic_issue
                    .map(|iss| self.gate_a_mocks_dir_exists_for(iss))
                    .unwrap_or(false);
                items.push(publish_mocks_item);
            }
            // #1223 (was #1060, docs/ORACLE_LOOP.md, #931/#932): the
            // per-issue acceptance actions — JIT authoring of THIS issue's
            // slice of the milestone's sealed acceptance suite, and the
            // external trust-gate re-run against the pushed SHA — now nest
            // into the Start (interactive)/(automated) submenus above
            // instead of living here as flat top-level items (#1223 gap 1:
            // not discoverable in the same place as the rest of the
            // pipeline). See the `member_tracking_issue`-gated pushes into
            // `interactive_children`/`automated_children` near the top of
            // this function.
            items.push(ContextMenuItem::separator());

            // #1505: a driver escalation — `coord drive`'s merge stage
            // stopped rather than retry a status no retry can fix
            // (NEEDS_ATTENTION / unrecognised) and left a proposed fix.
            // Shown regardless of lifecycle classification: a stuck merge
            // still reads as "in progress" (the issue is still open), so
            // this can't be nested under one specific `match lifecycle` arm
            // below. "Open PR" is deliberately the SAME `"open-pr"` action
            // the Done lifecycle uses below — a stuck merge almost always
            // already has a PR open, so there is nothing escalation-
            // specific about opening it.
            if let (Some(n), Some(repo)) = (issue_number, repo_name) {
                if let Some(esc) = self.escalation_for(repo, n) {
                    // Informational, non-actionable header naming WHY the
                    // driver stopped — the mockup's "Driver stuck on #N —
                    // <reason>" line — so the operator isn't choosing
                    // between Run/Open PR/Dismiss blind.
                    let mut reason_item = ContextMenuItem::action(
                        "escalation-reason",
                        &format!("Driver stuck: {}", trunc(&esc.reason, 50)),
                    );
                    reason_item.disabled = true;
                    items.push(reason_item);
                    // #1505 review nit: 40 chars clipped the tail of the
                    // sanctioned `gh pr merge <n> --rebase && coord
                    // reconcile-merges` recipe for larger PR numbers (a
                    // 6-digit PR number already runs to 53 chars). This
                    // isn't gated behind a confirmation dialog, so the
                    // operator should be able to read the whole command
                    // here before clicking — widen the budget accordingly.
                    //
                    // #2370: an escalation-table card always folds to TWO
                    // `decisions`-report options (`coord/reports.py`'s
                    // `fold_decisions`: "Recommended" = this row's
                    // `proposed_command`, "Inspect" = `coord escalate list
                    // --repo <repo>`) — so per the design's point 4 this is
                    // always a pull-right submenu, never a flat action, and
                    // each child calls `coord decide <repo> <issue>
                    // <index>` (via `dispatch_decide_escalation`) instead of
                    // `coord escalate run` directly. That command re-reads
                    // the card fresh and runs `options[index]` itself —
                    // this menu never re-derives or caches what the options
                    // are, it only labels them for display.
                    //
                    // #2375: the construction itself now lives in
                    // `escalation.rs`'s `run_proposed_fix_menu_item`, shared
                    // with the drive-queue Queue-panel row menu, so the two
                    // panels can never render different children/ordering
                    // for the same (repo, issue).
                    items.push(Self::run_proposed_fix_menu_item(esc));
                    let has_pr = self
                        .pipeline_issues
                        .iter()
                        .find(|iss| iss.number == n)
                        .and_then(|iss| self.pipeline_pr_number(iss))
                        .is_some();
                    let mut open_pr_item = ContextMenuItem::action("open-pr", "Open PR");
                    open_pr_item.disabled = !has_pr;
                    items.push(open_pr_item);
                    items.push(ContextMenuItem::action(
                        "dismiss-escalation",
                        "Dismiss escalation",
                    ));
                    items.push(ContextMenuItem::separator());
                }
            }

            // #2402: "Re-verify (revalidate)" — a Pipeline row whose Merge
            // stage is BLOCKED with no automatic retry coming (#2397: the
            // daemon's auto-drain tick never touches a BLOCKED entry) had no
            // in-TUI recovery action at all; the only fix was `coord merge
            // --revalidate` typed by hand. Shown regardless of `lifecycle`
            // for the same reason the escalation block above is — a stuck
            // merge still reads as "in progress" — so this can't be nested
            // under one specific `match lifecycle` arm below.
            //
            // Eligibility mirrors `coord.merge_queue.revalidation_candidates`
            // / `ci_revalidation_candidates` EXACTLY
            // (`format::merge_revalidate_eligible`, gated on the entry's raw
            // `reason` text): a review/CI-failure/conflict/missing-verdict
            // block never offers this action, only a stale-but-passed local
            // verdict or a stale-but-green CI check does. `repo_name` here is
            // the coord-local name (`PipelineIssue::coord_repo`); the
            // merge-plan lookup needs the GitHub `owner/repo` slug instead,
            // so the matching `PipelineIssue` is re-resolved repo-precisely
            // (#983 discipline — same reasoning as `has_running`/`has_zombie`
            // above) rather than reusing `epic_issue`, which only matches on
            // issue number.
            if let (Some(n), Some(repo)) = (issue_number, repo_name) {
                let repo_slug = self.pipeline_issues.iter().find_map(|iss| {
                    (iss.number == n && iss.coord_repo.as_deref() == Some(repo))
                        .then_some(iss.repo_slug.as_str())
                });
                if let Some(repo_slug) = repo_slug {
                    if let Some(entry) = self.merge_plan_entry_for_issue(repo_slug, n) {
                        if entry.status == "BLOCKED" {
                            if let Some(reason) = entry.reason.as_deref() {
                                if merge_revalidate_eligible(reason) {
                                    items.push(ContextMenuItem::action(
                                        "merge-revalidate",
                                        "Re-verify (revalidate)",
                                    ));
                                    items.push(ContextMenuItem::separator());
                                }
                            }
                        }
                    }
                }
            }
        }
        match lifecycle {
            PipelineRowLifecycle::New => {
                // #1500: "Mark ready" stages the row into In-progress:`ready`
                // by setting `status:queued` — display + intent only, no
                // dispatch. An epic row (`epic_children_for` resolves) names
                // the non-Done child count in the label, matching exactly
                // what clicking it will fan the label change out to
                // (`mark_selected_ready`) — so the label never promises a
                // count the action doesn't deliver.
                let mut epic_ready_count: Option<usize> = None;
                if let Some(iss) = epic_issue {
                    if let Some(repo) = iss.coord_repo.as_deref() {
                        if self.epic_children_for(iss).is_some() {
                            let rows = self.epic_child_rows_for(repo, iss.number);
                            epic_ready_count = Some(
                                rows.iter()
                                    .filter(|r| Self::epic_child_kept_in_progress(r))
                                    .count(),
                            );
                        }
                    }
                }
                let mark_ready_label = match epic_ready_count {
                    Some(n) if n > 0 => format!("Mark ready (epic + {} children)", n),
                    _ => "Mark ready".to_string(),
                };
                items.push(ContextMenuItem::action("mark-ready", &mark_ready_label));
                // #1755 (DQ-3): "Add to drive queue" sits beside "Mark
                // ready" — both express the same kind of operator intent
                // ("this one next") about a row no worker has touched yet,
                // so they belong in the same block. Swaps to "Remove from
                // drive queue" for a row already queued; empty for a row
                // with no issue/repo mapping (see the helper's doc comment).
                items.extend(self.drive_queue_menu_items_for_pipeline_row(
                    issue_number,
                    repo_name,
                    true,
                ));
                // #266: Drop a not-yet-started pipeline item back to Backlog
                // (strips `status:ready`).  Always enabled for New — no work
                // has been dispatched, so there is nothing to interrupt.
                items.push(ContextMenuItem::action("drop-to-backlog", "Drop to backlog"));
                items.push(ContextMenuItem::separator());
            }
            PipelineRowLifecycle::InProgress => {
                // Watch is also reachable via Enter on the row; surfacing
                // it here gives the no-right-click / Android-over-SSH
                // path a clickable affordance.
                items.push(ContextMenuItem::action("watch", "Watch").with_shortcut("Enter"));
                items.push(ContextMenuItem::action("stop", "Stop"));
                // #bounce: when the latest review wants changes, offer to
                // dispatch a fix worker (auto-loop's path) right from
                // the row menu.  Shows up while the row is still
                // "in-progress" because the work assignment may have
                // completed but the issue's open state classifies it
                // as InProgress (assignments + issue open).
                if self.selected_row_has_request_changes_for(issue_number) {
                    items.push(
                        ContextMenuItem::action("bounce", "Address review findings")
                            .with_shortcut("f"),
                    );
                }
                // #935 Part B: unified per-stage doctor — dry-run-diagnoses
                // first, then shows a results dialog with option buttons
                // (Recover / Reset stage / Clear phantom live session / Dismiss).
                // Replaces the two separate "Diagnose & fix stage" and
                // "Reset stage" items that required knowing which to pick.
                items.push(ContextMenuItem::action(
                    "diagnose-fix-stage",
                    "Diagnose & fix stage…",
                ));
                // #1500: "Unmark ready" reverses "Mark ready" — strips
                // `status:queued`, returning the row to New. Only offered
                // when the row actually got here via that marker (an
                // ordinary in-progress row with real work has nothing to
                // "unmark"), and — same guard as "Drop to backlog" below —
                // never once real work exists, so a live/completed session
                // can't be silently un-staged out from under it.
                let has_real_progress = issue_number
                    .map(|n| self.issue_has_live_session(n))
                    .unwrap_or(false)
                    || self.selected_issue_has_work_progress();
                let is_staged = epic_issue
                    .map(|iss| self.pipeline_issue_is_queued(iss))
                    .unwrap_or(false);
                if is_staged && !has_real_progress {
                    items.push(ContextMenuItem::action("unmark-ready", "Unmark ready"));
                }
                // #1755 (DQ-3): a row queued while New and since started must
                // stay dequeueable from the row it is actually on — `offer_
                // add: false` so this contributes ONLY "Remove from drive
                // queue", never a fresh Add (see the helper's doc comment).
                items.extend(self.drive_queue_menu_items_for_pipeline_row(
                    issue_number,
                    repo_name,
                    false,
                ));
                // #266: Drop an In-progress *idle* item back to Backlog (strips
                // `status:ready`).  Disabled when (a) a live session is attached
                // — a row whose work is actively running must not be yanked out
                // from under it — or (b) the issue already has real pipeline
                // work to preserve (a workable assignment that is done / merged
                // / running, e.g. #618).  Rows whose only assignments are
                // scoping chats or *failed* attempts stay droppable.
                let mut drop_item =
                    ContextMenuItem::action("drop-to-backlog", "Drop to backlog");
                drop_item.disabled = has_real_progress;
                items.push(drop_item);
                items.push(ContextMenuItem::separator());
            }
            PipelineRowLifecycle::Done => {
                // Only meaningful when a PR exists (merge_queue entry
                // with a pr_number).  When no PR is open yet the
                // dispatcher toasts a "no PR" warning.
                items.push(ContextMenuItem::action("open-pr", "Open PR"));
                items.push(ContextMenuItem::separator());
            }
            PipelineRowLifecycle::Other => {}
        }
        if let Some(num) = issue_number {
            items.push(ContextMenuItem::action(
                "copy-issue-number",
                &format!("Copy issue #{}", num),
            ));
            items.push(ContextMenuItem::separator());
        }
        items.push(ContextMenuItem::action("refresh", "Refresh").with_shortcut("r"));
        items
    }

    /// Build the context-menu target for the row that is *currently selected*
    /// in the sidebar, independent of any mouse position.  Shared by the
    /// right-click handler (which synthesises a left-click to select the row
    /// under the cursor first) and the keyboard shortcut (Menu key / Shift+F10
    /// / '.'), which opens the menu for whatever row j/k has already selected.
    /// Returns `None` for views that have no context menu (Settings/Terminal).
    pub(crate) fn context_menu_target_for_selection(&self) -> Option<ContextMenuTarget> {
        match self.active_view {
            SidebarView::Board => {
                let selected = self.board_selected_issue();
                let (repo_name, issue_number) = match selected {
                    Some((r, n)) => (Some(r), Some(n)),
                    None => (self.board_active_repo().map(|s| s.to_string()), None),
                };
                // #260: classify the row so the menu items reflect the current
                // lifecycle state (Backlog → Refine, etc.).
                let lifecycle = match (&repo_name, issue_number) {
                    (Some(r), Some(n)) => self.board_row_lifecycle(r, n),
                    _ => BoardRowLifecycle::Unknown,
                };
                Some(ContextMenuTarget::BoardRow {
                    issue_number,
                    repo_name,
                    lifecycle,
                })
            }
            SidebarView::Pipeline => {
                let sel = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i));
                let issue_number = sel.map(|i| i.number);
                // #983: carry the coord-local repo name so the menu gate can
                // be scoped repo-precisely (no cross-repo #N collision).
                let repo_name = sel.and_then(|i| i.coord_repo.clone());
                // #262: classify the row so the menu offers Start only when New.
                let lifecycle = sel
                    .map(|i| self.pipeline_row_lifecycle(i))
                    .unwrap_or(PipelineRowLifecycle::Other);
                Some(ContextMenuTarget::PipelineRow {
                    issue_number,
                    repo_name,
                    lifecycle,
                })
            }
            SidebarView::Machines => self.data.machines.get(self.machine_sel).map(|m| {
                ContextMenuTarget::MachineRow {
                    name: m.name.clone(),
                    is_paused: self.paused_machines.contains(&m.name),
                }
            }),
            // #771: milestone header — carries what "Dispatch milestone" needs.
            SidebarView::MilestoneDag => self.milestone_dag_selected().map(|v| {
                ContextMenuTarget::MilestoneHeader {
                    repo_name: v.repo_name.clone(),
                    tracking_issue: v.tracking_issue,
                    milestone_title: v.milestone_title.clone(),
                    milestone_number: v.milestone_number,
                }
            }),
            // #1003: Plans-panel row — reuses `MilestoneHeader` rather than a
            // new parallel target (the Plans panel's own doc comment says it
            // "elevates and subsumes" the old MilestoneDag view).
            //
            // #1123 (contract §4a): the old gate that only built a target
            // for rows with a tracking epic (`tracking_issue: Some`) is
            // dropped — every Plans row is now right-clickable. A milestone
            // surfaced purely because member issues reference it (no epic
            // yet, `PlanRosterEntry::tracking_issue == None`) builds a
            // `PlansStub` target instead of `MilestoneHeader`, which offers
            // "Create work order / promote to epic…" (contract §4b) rather
            // than a silent no-op.
            SidebarView::Plans => self.plans_selected().map(|e| match e.tracking_issue {
                Some(tracking_issue) => ContextMenuTarget::MilestoneHeader {
                    repo_name: e.repo.clone(),
                    tracking_issue,
                    milestone_title: e.title.clone(),
                    milestone_number: e.milestone_number,
                },
                None => ContextMenuTarget::PlansStub {
                    repo_name: Some(e.repo.clone()),
                    milestone: Some((e.milestone_number, e.title.clone())),
                },
            }),
            // #956: Terminal-view tree — only terminal rows get a menu; a
            // machine row (or nothing selected) has no verb defined yet.
            SidebarView::Terminal => self
                .selected_fleet_terminal()
                .map(|(machine, name)| ContextMenuTarget::TerminalRow { machine, name }),
            // #1866 (Q-1): the Queue grid's selected row. Builds the very
            // same `DriveQueueRow` target the #1755 overlay does, so the
            // menu (move/remove/unblock/resume, with end-of-queue moves
            // `disabled_because` rather than silently no-op) and every
            // dispatcher behind it are shared verbatim between the two
            // surfaces. Reached by the keyboard menu key here; the mouse
            // path pre-selects the row under the cursor first (`events.rs`).
            SidebarView::Queue => self.queue_context_target(),
            // #2533 (ms-67 contract §4a): the Approved-panel selected row —
            // same shape as Queue above (persistent `approved_sel`, no
            // mouse position needed). Reached by the keyboard menu key here;
            // the mouse path pre-selects the row under the cursor first
            // (`events.rs`).
            SidebarView::Approved => self.approved_context_target(),
            _ => None,
        }
    }

    /// Open a right-click context menu anchored at `pos` for `target`.
    /// Pre-builds the item list and picks the first selectable item as the
    /// keyboard focus.  Returns `true` if a menu was opened (i.e. items
    /// are non-empty).
    pub(crate) fn open_context_menu(&mut self, pos: Point, target: ContextMenuTarget) -> bool {
        let items = match &target {
            ContextMenuTarget::BoardRow {
                issue_number,
                repo_name,
                lifecycle,
            } => self.context_menu_items_for_board_row(
                *issue_number,
                lifecycle,
                repo_name.as_deref(),
            ),
            ContextMenuTarget::PipelineRow {
                issue_number,
                repo_name,
                lifecycle,
            } => self.context_menu_items_for_pipeline_row(
                *issue_number,
                lifecycle,
                repo_name.as_deref(),
            ),
            ContextMenuTarget::MachineRow { name, is_paused } => self
                .context_menu_items_for_machine_row(
                    name,
                    *is_paused,
                    self.quiet_hours_windows.get(name),
                ),
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                milestone_title,
                milestone_number,
            } => {
                // #2494: look up the epic tracking issue's own labels from
                // the cached open-issues list so the menu can gate "Send to
                // Pipeline" on whether it's already `coord`-labeled. No
                // cache row (e.g. a stale/offline board) → empty labels,
                // which offers the item — worst case a harmless re-`track`.
                let labels: &[String] = self
                    .data
                    .open_issues
                    .iter()
                    .find(|oi| oi.repo_name == *repo_name && oi.number == *tracking_issue)
                    .map(|oi| oi.labels.as_slice())
                    .unwrap_or(&[]);
                self.context_menu_items_for_milestone_header(
                    repo_name,
                    *tracking_issue,
                    milestone_title,
                    *milestone_number,
                    labels,
                )
            }
            ContextMenuTarget::TerminalRow { .. } => self.context_menu_items_for_terminal_row(),
            ContextMenuTarget::PlansStub { repo_name, milestone } => {
                self.context_menu_items_for_plans_stub(repo_name.as_deref(), milestone.as_ref())
            }
            // #1631 (H-4) / #1755 (DQ-3): right-click anywhere on the status
            // bar. One target covers the WHOLE bar (there is no per-segment
            // click dispatch in this codebase — see `fleet_health.rs`'s
            // module doc comment), so its menu is the union of "Fleet
            // health…" (opens the fleet-health detail overlay) and "Drive
            // queue…" (switches to the Queue panel, #1868).
            ContextMenuTarget::FleetHealth => {
                let mut v = self.context_menu_items_for_fleet_health();
                v.extend(self.context_menu_items_for_drive_queue_segment());
                // #2374: fleet-level escalation cards (release-cordon /
                // drive-queue sentinel rows) — empty when the fleet has none
                // open, so this is a no-op append on the common case.
                v.extend(self.context_menu_items_for_fleet_escalations());
                v
            }
            // #1755 (DQ-3) / #1868 (Q-3): right-click on a Queue panel row.
            ContextMenuTarget::DriveQueueRow {
                repo_name,
                issue_number,
                state,
                position,
                queue_len,
                held,
            } => self.context_menu_items_for_drive_queue_row(
                repo_name,
                *issue_number,
                state,
                *position,
                *queue_len,
                *held,
            ),
            // #2454: right-click on a Reports result-table row. The target
            // only ever carries `(repo, issue)` — see `reports.rs`'s
            // `context_menu_items_for_report_row` for why that is the whole
            // surface this menu needs.
            ContextMenuTarget::ReportRow {
                repo_name,
                issue_number,
            } => self.context_menu_items_for_report_row(repo_name, *issue_number),
            // #2287 (ms-65 §8c): right-click on a Board document tab.
            ContextMenuTarget::BoardDocTab { is_pinned, .. } => {
                self.context_menu_items_for_board_doc_tab(*is_pinned)
            }
            // #2533 (ms-67 contract §4a): right-click on an Approved-work-items row.
            ContextMenuTarget::ApprovedRow { submission_id } => {
                self.context_menu_items_for_approved_row(submission_id)
            }
        };
        if items.is_empty() {
            return false;
        }
        // First selectable item (action or submenu parent, not disabled) is
        // the initial keyboard selection.
        let selected_idx = items
            .iter()
            .position(|it| it.is_selectable())
            .unwrap_or(0);
        self.pending_context_menu = Some(ContextMenuState {
            items,
            anchor: pos,
            selected_idx,
            target,
            submenu_path: Vec::new(),
            submenu_selected: Vec::new(),
        });
        true
    }

    /// Render the open context menu (if any) on top of the rest of the UI,
    /// including any currently-open pull-right submenus (#607).
    /// Caches the resolved layout stack so the click hit-test can match items
    /// without recomputing.  Pure render — no state mutation.
    pub(crate) fn render_context_menu(&self, backend: &mut dyn Backend, viewport: Rect) {
        let Some(state) = self.pending_context_menu.as_ref() else {
            return;
        };
        let lh = backend.line_height();
        let stack = build_context_menu_stack(state, lh, viewport);
        let mut cache: Vec<(ContextMenu, ContextMenuLayout)> = Vec::with_capacity(stack.len());
        for (menu, layout) in &stack {
            backend.draw_context_menu(menu, layout);
            cache.push((menu.clone(), layout.clone()));
        }
        *self.context_menu_layout.borrow_mut() = cache;
    }

    /// Hit-test a click against the open context menu and all open submenus.
    /// Walk levels deepest-first so clicks on a child popup don't fall through
    /// to the parent.
    ///
    /// Returns `Some(true)` when the click was handled (action dispatched or
    /// submenu toggled), `Some(false)` when the click landed on a non-actionable
    /// cell (separator / disabled — swallow, keep menu open), or `None` when no
    /// menu is open.  A click outside all open levels dismisses the menu.
    pub(crate) fn handle_context_menu_click(
        &mut self,
        pos: Point,
        backend: &mut dyn Backend,
    ) -> Option<bool> {
        if self.pending_context_menu.is_none() {
            return None;
        }
        let stack = self.context_menu_layout.borrow().clone();
        if stack.is_empty() {
            return None;
        }

        // Walk deepest-first so a child popup intercepts before the parent.
        for (depth_idx, (ref menu, ref layout)) in stack.iter().enumerate().rev() {
            match layout.hit_test(pos.x, pos.y) {
                ContextMenuHit::Item(ref id) => {
                    // Locate the item_idx this id belongs to.
                    let item_idx_opt = layout
                        .visible_items
                        .iter()
                        .find(|v| {
                            v.clickable && menu.items[v.item_idx].id.as_ref() == Some(id)
                        })
                        .map(|v| v.item_idx);

                    // Check whether the quadraui item has a submenu (⟹ parent).
                    let is_parent = item_idx_opt
                        .and_then(|idx| menu.items.get(idx))
                        .map(|item| item.submenu.is_some())
                        .unwrap_or(false);

                    if is_parent {
                        if let Some(item_idx) = item_idx_opt {
                            let first_sel = menu
                                .items
                                .get(item_idx)
                                .and_then(|it| it.submenu.as_ref())
                                .and_then(|sub| {
                                    sub.iter().position(|i| !i.is_separator() && !i.disabled)
                                })
                                .unwrap_or(0);
                            if let Some(state) = self.pending_context_menu.as_mut() {
                                // Trim deeper submenus and open this one.
                                state.submenu_path.truncate(depth_idx);
                                state.submenu_selected.truncate(depth_idx);
                                state.submenu_path.push(item_idx);
                                state.submenu_selected.push(first_sel);
                            }
                        }
                        *self.context_menu_layout.borrow_mut() = Vec::new();
                        return Some(true);
                    }

                    // Leaf action — guard against synthetic parent ids leaking through.
                    let action_id = id.as_str().to_string();
                    if action_id.starts_with("__parent__") {
                        return Some(false);
                    }
                    let state = self.pending_context_menu.take()?;
                    *self.context_menu_layout.borrow_mut() = Vec::new();
                    self.dispatch_context_menu_action(&action_id, &state.target);
                    self.flush_pending_clipboard_copy(backend);
                    return Some(true);
                }
                ContextMenuHit::Inert => return Some(false),
                ContextMenuHit::Empty => {
                    // Click outside this level — keep searching shallower levels
                    // only if the click is within the parent's bounds (otherwise
                    // fall through and dismiss below).
                    continue;
                }
            }
        }

        // Click outside all open menu levels → dismiss.
        self.pending_context_menu = None;
        *self.context_menu_layout.borrow_mut() = Vec::new();
        Some(true)
    }

    /// Move the keyboard selection within the currently-deepest open menu level,
    /// skipping separators and disabled items.  No-op when no menu is open.
    pub(crate) fn context_menu_move_selection(&mut self, delta: i32) {
        let Some(ref state) = self.pending_context_menu else {
            return;
        };
        let depth = state.submenu_path.len();
        let current_sel = if depth == 0 {
            state.selected_idx
        } else {
            *state.submenu_selected.last().unwrap_or(&0)
        };

        let level_items = items_at_depth(state, depth);
        // Build a transient quadraui menu to reuse its move_selection logic
        // (skips separators + disabled items, wraps around).
        let qui_items: Vec<QuiContextMenuItem> = level_items.iter().map(coord_item_to_qui).collect();
        let temp = ContextMenu {
            id: WidgetId::new("_move"),
            items: qui_items,
            selected_idx: current_sel,
            bg: None,
            placement: ContextMenuPlacement::AnchorPoint,
        };
        let new_sel = temp.move_selection(current_sel, delta);

        if let Some(ref mut s) = self.pending_context_menu {
            if depth == 0 {
                s.selected_idx = new_sel;
            } else if let Some(sel) = s.submenu_selected.last_mut() {
                *sel = new_sel;
            }
        }
    }

    /// Activate the keyboard-selected item (Enter) at the current menu depth.
    /// If the selected item is a submenu parent → open the submenu.
    /// If it's a leaf action → dispatch and dismiss the menu.
    /// No-op when no menu is open or the item is a separator.
    pub(crate) fn context_menu_activate_selected(&mut self, backend: &mut dyn Backend) -> bool {
        let Some(ref state) = self.pending_context_menu else {
            return false;
        };
        let depth = state.submenu_path.len();
        let current_sel = if depth == 0 {
            state.selected_idx
        } else {
            *state.submenu_selected.last().unwrap_or(&0)
        };

        let level_items = items_at_depth(state, depth);
        let Some(item) = level_items.get(current_sel) else {
            return false;
        };

        if item.submenu.is_some() {
            // Open the submenu: push path + first-selectable.
            let sub_items = item.submenu.as_ref().unwrap();
            let first_sel = sub_items.iter().position(|i| i.is_selectable()).unwrap_or(0);
            let path_idx = current_sel;
            if let Some(ref mut s) = self.pending_context_menu {
                s.submenu_path.push(path_idx);
                s.submenu_selected.push(first_sel);
            }
            *self.context_menu_layout.borrow_mut() = Vec::new();
            return true;
        }

        let Some(action_id) = item.action_id.clone() else {
            // Selected a separator — shouldn't happen since move_selection
            // skips them, but defend anyway.
            return false;
        };
        let state = self.pending_context_menu.take().unwrap();
        *self.context_menu_layout.borrow_mut() = Vec::new();
        self.dispatch_context_menu_action(&action_id, &state.target);
        self.flush_pending_clipboard_copy(backend);
        true
    }

    /// Write out a clipboard payload staged by `dispatch_context_menu_action`
    /// (e.g. `"copy-issue-number"`), if any. Split out so both direct UI
    /// entry points (`handle_context_menu_click` / mouse,
    /// `context_menu_activate_selected` / keyboard) share one flush site
    /// rather than duplicating the `backend.services().clipboard()` call.
    fn flush_pending_clipboard_copy(&mut self, backend: &mut dyn Backend) {
        if let Some(text) = self.pending_clipboard_copy.take() {
            backend.services().clipboard().write_text(&text);
        }
    }

    /// Close the deepest open submenu (#607).  If no submenus are open, dismisses
    /// the whole menu.
    pub(crate) fn context_menu_close_submenu_or_dismiss(&mut self) {
        if let Some(ref mut state) = self.pending_context_menu {
            if !state.submenu_path.is_empty() {
                state.submenu_path.pop();
                state.submenu_selected.pop();
                *self.context_menu_layout.borrow_mut() = Vec::new();
                return;
            }
        }
        self.dismiss_context_menu();
    }

    /// Dismiss the open context menu without firing an action.
    pub(crate) fn dismiss_context_menu(&mut self) {
        self.pending_context_menu = None;
        *self.context_menu_layout.borrow_mut() = Vec::new();
    }

    /// Returns `true` if the currently-selected item at the deepest open
    /// context-menu level has a submenu.  Used by the `Right`-key handler to
    /// distinguish "open submenu" (submenu parent) from "no-op" (leaf item).
    pub(crate) fn context_menu_selected_has_submenu(&self) -> bool {
        let Some(ref state) = self.pending_context_menu else {
            return false;
        };
        let depth = state.submenu_path.len();
        let sel = if depth == 0 {
            state.selected_idx
        } else {
            *state.submenu_selected.last().unwrap_or(&0)
        };
        items_at_depth(state, depth)
            .get(sel)
            .map(|i| i.submenu.is_some())
            .unwrap_or(false)
    }

    // ── #369 / #329: Prompt dialogs ─────────────────────────────────────────
    //
    // Replaces the seven status-bar hint prompts with quadraui `Dialog`
    // popups — centered modal overlays with labelled, clickable buttons.
    // Keyboard bindings are preserved unchanged; clicking a button fires
    // the same action as the corresponding key.

    /// Build the `quadraui::Dialog` for whichever prompt is currently
    /// active, following the same priority order as `status_bar()`.
    /// Returns `None` when no prompt is pending.
    pub(crate) fn build_prompt_dialog(&self) -> Option<Dialog> {
        // ── Force-quit confirmation (interactive session live) ───────────
        if self.pending_quit_confirm {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:quit-confirm"),
                title: StyledText::plain("Quit — interactive session live?"),
                body: vec![StyledText::plain(
                    "An interactive session is still running in the Terminal tab. \
                     It keeps running in tmux — you can reattach later. Quit anyway?",
                )],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Esc  Cancel — stay".into(),
                        is_default: true,
                        is_cancel: true,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("force-quit"),
                        label: "Q  Force quit (session stays in tmux)".into(),
                        is_default: false,
                        is_cancel: false,
                        tint: Some(Color::rgb(220, 120, 60)),
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: true,
                input: None,
            });
        }

        // ── #486 Leg 4: Machine picker (Work / Plan / Review / Fix) ──────
        if let Some(ref picker) = self.pending_machine_picker {
            let verb = interactive_mode_verb(picker.mode);
            let mut buttons: Vec<DialogButton> = picker
                .machines
                .iter()
                .enumerate()
                .map(|(i, m)| {
                    let where_tag = if m.is_local { "local" } else { "remote" };
                    let reach = if m.reachable { "●" } else { "○ offline?" };
                    DialogButton {
                        id: WidgetId::new(format!("machine:{i}")),
                        label: format!(
                            "{}  {} ({}) {}  {}",
                            i + 1,
                            m.name,
                            where_tag,
                            reach,
                            m.host
                        ),
                        is_default: i == 0,
                        is_cancel: false,
                        tint: None,
                    }
                })
                .collect();
            buttons.push(DialogButton {
                id: WidgetId::new("cancel"),
                label: "Esc  Cancel".into(),
                is_default: false,
                is_cancel: true,
                tint: None,
            });
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:machine-picker"),
                title: StyledText::plain(format!("Select machine for interactive {verb}")),
                body: vec![StyledText::plain(
                    "Local runs on this TTY; a remote machine dispatches over ssh+tmux.",
                )],
                buttons,
                severity: Some(DialogSeverity::Question),
                vertical_buttons: true,
                input: None,
            });
        }

        // ── #954: "New terminal" machine picker ───────────────────────────
        if let Some(ref machines) = self.pending_new_terminal_picker {
            let mut buttons: Vec<DialogButton> = machines
                .iter()
                .enumerate()
                .map(|(i, m)| {
                    let where_tag = if m.is_local { "local" } else { "remote" };
                    let reach = if m.reachable { "●" } else { "○ offline?" };
                    DialogButton {
                        id: WidgetId::new(format!("new-terminal-machine:{i}")),
                        label: format!(
                            "{}  {} ({}) {}  {}",
                            i + 1,
                            m.name,
                            where_tag,
                            reach,
                            m.host
                        ),
                        is_default: i == 0,
                        is_cancel: false,
                        tint: None,
                    }
                })
                .collect();
            buttons.push(DialogButton {
                id: WidgetId::new("cancel"),
                label: "Esc  Cancel".into(),
                is_default: false,
                is_cancel: true,
                tint: None,
            });
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:new-terminal-machine-picker"),
                title: StyledText::plain("Select machine for new terminal"),
                body: vec![StyledText::plain(
                    "Local runs on this TTY; a remote machine creates over ssh+tmux.",
                )],
                buttons,
                severity: Some(DialogSeverity::Question),
                vertical_buttons: true,
                input: None,
            });
        }

        // ── #954: New-terminal optional name input ─────────────────────────
        if let Some(ref input) = self.pending_new_terminal {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:new-terminal-name"),
                title: StyledText::plain(format!("New terminal on {}", input.machine)),
                body: vec![StyledText::plain("Optional name (Enter to auto-name):")],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("submit"),
                        label: "Submit".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: None,
                vertical_buttons: false,
                input: Some(DialogInput::TextInput(DialogTextInput {
                    value: input.buf.clone(),
                    placeholder: "terminal name…".into(),
                    cursor: Some(input.buf.len()),
                })),
            });
        }

        // ── #2147: "Set quiet hours…" window input ──────────────────────────
        // Armed by the Machines-panel right-click menu
        // (`machine-quiet-hours-set`), pre-filled with the machine's current
        // window (or `22:00-08:00`) by `open_quiet_hours_dialog`. One text
        // field carries the whole window — see `PendingQuietHours`'s doc
        // comment for why this isn't a 3-field form. Submit/Clear both
        // dispatch through `submit_quiet_hours`/`clear_quiet_hours_dialog`
        // (mouse: `fire_dialog_button` below; keyboard: `events.rs`).
        if let Some(ref input) = self.pending_quiet_hours {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:quiet-hours"),
                title: StyledText::plain(format!("Set quiet hours: {}", input.machine)),
                body: vec![StyledText::plain(
                    "No new dispatch during this window each day (HH:MM-HH:MM, \
                     optional trailing time zone):",
                )],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("submit"),
                        label: "Submit".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("clear"),
                        label: "Clear".into(),
                        is_default: false,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: None,
                vertical_buttons: false,
                input: Some(DialogInput::TextInput(DialogTextInput {
                    value: input.buf.clone(),
                    placeholder: "HH:MM-HH:MM [tz]".into(),
                    cursor: Some(input.buf.len()),
                })),
            });
        }

        // ── Repo picker ──────────────────────────────────────────────────
        if let Some(ref picker) = self.pending_repo_picker {
            let mut buttons: Vec<DialogButton> = picker
                .repos
                .iter()
                .enumerate()
                .map(|(i, repo)| DialogButton {
                    id: WidgetId::new(format!("repo:{i}")),
                    label: format!("{}  {}", i + 1, repo),
                    is_default: i == 0,
                    is_cancel: false,
                    tint: None,
                })
                .collect();
            buttons.push(DialogButton {
                id: WidgetId::new("cancel"),
                label: "Cancel".into(),
                is_default: false,
                is_cancel: true,
                tint: None,
            });
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:repo-picker"),
                title: StyledText::plain("Select Target Repo"),
                body: vec![StyledText::plain(
                    "Choose the repo to file the new issue in:",
                )],
                buttons,
                severity: Some(DialogSeverity::Question),
                vertical_buttons: true,
                input: None,
            });
        }

        // ── #935 Part B: Diagnose & fix stage results dialog ────────────
        if let Some(ref dlg) = self.pending_diagnose_dialog {
            let repo = dlg.repo.clone();
            let issue_number = dlg.issue_number;
            let stage = dlg.stage.clone();
            let needs_reset = dlg.needs_reset;
            let has_phantom = dlg.has_phantom_session;

            // Body: prefer actions (dry-run reports what would be done) over
            // findings; fall back to findings if no actions; empty → healthy.
            let body_text = if !dlg.actions_taken.is_empty() {
                dlg.actions_taken
                    .iter()
                    .take(6)
                    .map(|a| format!("✓ {a}"))
                    .collect::<Vec<_>>()
                    .join("\n")
            } else if !dlg.findings.is_empty() {
                dlg.findings
                    .iter()
                    .take(6)
                    .map(|f| format!("· {f}"))
                    .collect::<Vec<_>>()
                    .join("\n")
            } else {
                "No issues found — stage appears healthy.".to_string()
            };
            let body_text = if dlg.legacy {
                format!(
                    "{body_text}\n\n(daemon predates #935 JSON support — best-effort \
                     parsed from legacy text output)"
                )
            } else {
                body_text
            };

            let mut buttons: Vec<DialogButton> = Vec::new();
            // "Recover" — run the full (non-dry-run) diagnose.
            // Always offered; it's safe to retry if already healthy.
            buttons.push(DialogButton {
                id: WidgetId::new("diagnose-stage"),
                label: "R  Recover (diagnose & fix)".into(),
                is_default: !needs_reset,
                is_cancel: false,
                tint: None,
            });
            // "Reset stage" — diagnose --reset; always available as a
            // non-destructive escape hatch (keeps the branch).
            buttons.push(DialogButton {
                id: WidgetId::new("diagnose-reset"),
                label: "X  Reset stage (keeps branch + commits)".into(),
                is_default: needs_reset,
                is_cancel: false,
                tint: Some(Color::rgb(200, 130, 50)),
            });
            // "Clear phantom live session" — only meaningful when there is
            // actually a pending- entry for this issue.
            if has_phantom {
                buttons.push(DialogButton {
                    id: WidgetId::new("diagnose-clear-phantom"),
                    label: "C  Clear phantom live session".into(),
                    is_default: false,
                    is_cancel: false,
                    tint: Some(Color::rgb(150, 100, 200)),
                });
            }
            buttons.push(DialogButton {
                id: WidgetId::new("cancel"),
                label: "Esc  Dismiss".into(),
                is_default: false,
                is_cancel: true,
                tint: None,
            });

            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:diagnose-fix-stage"),
                title: StyledText::plain(format!(
                    "Diagnose: {repo} #{issue_number} — {stage}"
                )),
                body: vec![StyledText::plain(body_text)],
                buttons,
                severity: Some(DialogSeverity::Question),
                vertical_buttons: true,
                input: None,
            });
        }

        // ── Refinement-chat close (#410: Cancel / Save / Send) ──────────
        if let Some(ref p) = self.pending_refinement_close_prompt {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:refinement-close"),
                title: StyledText::plain("Close Refinement Chat"),
                body: vec![StyledText::plain(format!(
                    "What do you want to do with issue #{}?",
                    p.issue_number
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Esc  Cancel — discard, issue unchanged".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("save"),
                        label: "S  Save — draft notes + mark ready".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("send"),
                        label: "D  Send — save + dispatch to pipeline".into(),
                        is_default: false,
                        is_cancel: false,
                        tint: Some(Color::rgb(60, 180, 220)),
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: true,
                input: None,
            });
        }

        // ── Report-fix description input ─────────────────────────────────
        if let Some(ref buf) = self.pending_report_fix {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:report-fix"),
                title: StyledText::plain("Report & Dispatch Fix"),
                body: vec![StyledText::plain(
                    "Enter a description for the fix (optional):",
                )],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("submit"),
                        label: "Submit".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: None,
                vertical_buttons: false,
                input: Some(DialogInput::TextInput(DialogTextInput {
                    value: buf.clone(),
                    placeholder: "description…".into(),
                    cursor: Some(buf.len()),
                })),
            });
        }

        // ── #977 Plan-capture title input ─────────────────────────────────
        if let Some(ref buf) = self.pending_plan_capture {
            let repo = self
                .plans_selected()
                .map(|e| e.repo)
                .or_else(|| self.data.pipeline_repos.first().map(|(n, _)| n.clone()))
                .unwrap_or_else(|| "?".to_string());
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:plan-capture"),
                title: StyledText::plain("New plan (capture)"),
                body: vec![StyledText::plain(format!(
                    "Enter a short plan title — captured immediately as {repo}, \
                     no work order yet:"
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("submit"),
                        label: "Submit".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: None,
                vertical_buttons: false,
                input: Some(DialogInput::TextInput(DialogTextInput {
                    value: buf.clone(),
                    placeholder: "plan title…".into(),
                    cursor: Some(buf.len()),
                })),
            });
        }

        // ── #1017 New-milestone-via-chat title input ────────────────────────
        if let Some(ref buf) = self.pending_new_milestone_chat {
            let repo = self
                .plans_selected()
                .map(|e| e.repo)
                .or_else(|| self.data.pipeline_repos.first().map(|(n, _)| n.clone()))
                .unwrap_or_else(|| "?".to_string());
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:new-milestone-chat"),
                title: StyledText::plain("New milestone via chat"),
                body: vec![StyledText::plain(format!(
                    "Optional seed title for {repo} — a milestone-chat steward will \
                     discuss goal/scope with you before creating anything. Leave \
                     blank to start from scratch:"
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("submit"),
                        label: "Submit".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: None,
                vertical_buttons: false,
                input: Some(DialogInput::TextInput(DialogTextInput {
                    value: buf.clone(),
                    placeholder: "seed title (optional)…".into(),
                    cursor: Some(buf.len()),
                })),
            });
        }

        // ── #1003 Plans-row single-field input (Edit milestone / Add issue /
        // Remove issue) ─────────────────────────────────────────────────────
        if let Some(ref input) = self.pending_milestone_row_input {
            let (title, body, placeholder) = match input.kind {
                MilestoneRowInputKind::EditTitle => (
                    "Edit milestone",
                    format!(
                        "New title for milestone #{} ({}):",
                        input.milestone_number, input.repo_name
                    ),
                    "milestone title…",
                ),
                MilestoneRowInputKind::AddIssue => (
                    "Add issue to milestone",
                    format!(
                        "Issue number to add to \"{}\" ({}):",
                        input.milestone_title, input.repo_name
                    ),
                    "issue number…",
                ),
                MilestoneRowInputKind::RemoveIssue => (
                    "Remove issue from milestone",
                    format!(
                        "Issue number to remove from \"{}\" ({}):",
                        input.milestone_title, input.repo_name
                    ),
                    "issue number…",
                ),
                MilestoneRowInputKind::AddSubIssue => (
                    "Add sub-issue to epic",
                    format!(
                        "Issue number to add as a sub-issue of \"{}\" ({}) — \
                         optionally `{{group: G, after: #N,...}}`:",
                        input.milestone_title, input.repo_name
                    ),
                    "e.g. 1050 or 1050 {group: B}…",
                ),
                MilestoneRowInputKind::AddSubIssueChat => (
                    "Add sub-issue via chat",
                    format!(
                        "Candidate issue number to discuss adding as a sub-issue of \
                         \"{}\" ({}) — a milestone-chat steward will propose the \
                         `{{group/after}}` annotation with you. Bare number, `#`-prefix \
                         both OK (e.g. 1050 or #1050) — same-repo issue only, no \
                         owner/repo#N form:",
                        input.milestone_title, input.repo_name
                    ),
                    "e.g. 1050 or #1050…",
                ),
            };
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:milestone-row-input"),
                title: StyledText::plain(title),
                body: vec![StyledText::plain(body)],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("submit"),
                        label: "Submit".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: None,
                vertical_buttons: false,
                input: Some(DialogInput::TextInput(DialogTextInput {
                    value: input.buf.clone(),
                    placeholder: placeholder.into(),
                    cursor: Some(input.buf.len()),
                })),
            });
        }

        // ── #1755 (DQ-3) "Add to drive queue after…" ────────────────────────
        // Same single-field shape as the Plans-row input just above; Enter
        // submits, Esc cancels (`events.rs`).
        if let Some(ref input) = self.pending_drive_queue_after {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:drive-queue-after"),
                title: StyledText::plain("Add to drive queue after…"),
                body: vec![StyledText::plain(format!(
                    "Pre-req issue(s) that must land before {} #{} is driven. \
                     Comma- or space-separated; bare numbers resolve against \
                     {}, or use REPO#N for a cross-repo pre-req:",
                    input.repo_name, input.issue_number, input.repo_name
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("submit"),
                        label: "Submit".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: None,
                vertical_buttons: false,
                input: Some(DialogInput::TextInput(DialogTextInput {
                    value: input.buf.clone(),
                    placeholder: "e.g. 1753, 1754 or otherrepo#12…".into(),
                    cursor: Some(input.buf.len()),
                })),
            });
        }

        // ── #2500 "What needs to change?" — Gate-A changes-requested note ──
        // Same single-field shape as "Add to drive queue after…" just
        // above; Enter submits (blank OK, falls back to no `--note`), Esc
        // cancels the whole `coord gate-a --changes` dispatch (`events.rs`).
        if let Some(ref input) = self.pending_gate_a_changes_note {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:gate-a-changes-note"),
                title: StyledText::plain("Request Gate A changes"),
                body: vec![StyledText::plain(format!(
                    "What needs to change in {} #{}'s Gate-A contract? Recorded as \
                     the verdict's --note (optional — leave blank to record the \
                     rejection with no note):",
                    input.repo_name, input.issue_number
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("submit"),
                        label: "Submit".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: None,
                vertical_buttons: false,
                input: Some(DialogInput::TextInput(DialogTextInput {
                    value: input.buf.clone(),
                    placeholder: "e.g. status vocabulary is wrong…".into(),
                    cursor: Some(input.buf.len()),
                })),
            });
        }

        // ── #1003 Close / archive plan confirm ──────────────────────────────
        if let Some(ref plan) = self.pending_close_plan {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:close-plan"),
                title: StyledText::plain("Close / archive plan"),
                body: vec![StyledText::plain(format!(
                    "Close #{} — \"{}\" ({})? This closes the tracking issue on GitHub.",
                    plan.tracking_issue, plan.milestone_title, plan.repo_name
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("yes"),
                        label: "y  Confirm close".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── Test-failure reason input ────────────────────────────────────
        if let Some((_, ref buf)) = self.pending_test_fail {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:test-fail"),
                title: StyledText::plain("Record Test Failure"),
                body: vec![StyledText::plain("Enter a reason (optional):")],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("submit"),
                        label: "Submit".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: Some(DialogInput::TextInput(DialogTextInput {
                    value: buf.clone(),
                    placeholder: "reason…".into(),
                    cursor: Some(buf.len()),
                })),
            });
        }

        // ── Test → Review confirm (Test precedes Review) ─────────────────
        // Raised by detect_test_verdict once the smoke test passes/skips.
        if let Some(ref p) = self.pending_auto_review {
            // #722: a live remote session blocks the offer — direct the operator
            // to reattach and exit first; the detector will re-fire once clear.
            if let Some(d) = self.live_session_blocking_dialog(p.issue_num, &p.coord_repo) {
                return Some(d);
            }
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:auto-review"),
                title: StyledText::plain("Test passed — start review?"),
                body: vec![
                    StyledText::plain(format!(
                        "The smoke test for {} #{} passed.",
                        p.coord_repo, p.issue_num,
                    )),
                    StyledText::plain(
                        "Start the human-attended interactive review now?".to_string(),
                    ),
                ],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("review"),
                        label: "⏎  Start review".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Esc  Not now".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── One-key stage offer (Fix / Test) ─────────────────────────────
        // Fix: raised by detect_review_verdict (request-changes, findings in
        // the DB).  Test: raised by detect_completed_interactive_work once a
        // work/fix finishes (Test precedes Review).  No findings input — the
        // next stage needs no operator typing.
        if let Some(ref p) = self.pending_stage_launch {
            // #722: live-session gate (same as pending_auto_review above).
            if let Some(d) = self.live_session_blocking_dialog(p.issue_num, &p.coord_repo) {
                return Some(d);
            }
            let (title, intro, action, button) = match p.kind {
                StageLaunchKind::Fix => (
                    "Review requested changes — start a fix?",
                    format!(
                        "The review of {} #{} requested changes (findings already \
                         captured in the DB).",
                        p.coord_repo, p.issue_num,
                    ),
                    "Start an interactive fix on the same branch?",
                    "⏎  Start fix",
                ),
                StageLaunchKind::Test => (
                    "Work complete — start testing?",
                    format!(
                        "Interactive work for {} #{} finished and pushed a branch.",
                        p.coord_repo, p.issue_num,
                    ),
                    "Start the human-attended testing session (smoke tests) now?",
                    "⏎  Start testing",
                ),
            };
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:stage-launch"),
                title: StyledText::plain(title.to_string()),
                body: vec![
                    StyledText::plain(intro),
                    StyledText::plain(action.to_string()),
                ],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("stage-go"),
                        label: button.into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Esc  Not now".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── Leg 3 (#517): rework (request-changes) confirm ───────────────
        // #587: the dialog now includes a required findings text input.
        // The operator types what the reviewer flagged; `confirm_rework`
        // persists it via `coord set-review-findings` before launching the
        // fix, so the fix worker is briefed with concrete feedback instead
        // of the "(No structured findings were captured)" fallback.
        if let Some(ref p) = self.pending_rework {
            // #803: compute the escalated model before borrowing `p` for the
            // dialog body.  `fix_model_for_issue` resolves `pipeline_models`
            // from board_meta and the latest work assignment's review_iteration.
            let fix_model_hint = self.fix_model_for_issue(&p.coord_repo, p.issue_num);
            // #587 owns this dialog (operator types the reviewer's findings into
            // the input below); the per-issue context block (#603 Phase 2) still
            // reaches the fix worker via the briefing injection, so the #603
            // dialog preview is reserved for the test-fix dialog where the
            // findings are already captured (test_reason).
            let mut rework_body = vec![
                StyledText::plain(format!(
                    "The review of {} #{} requested changes.",
                    p.coord_repo, p.issue_num,
                )),
                StyledText::plain(
                    "Type the reviewer's findings in the text box below \
                     (required), then press Enter to save them and start \
                     an interactive fix on the same branch."
                        .to_string(),
                ),
            ];
            // #803: surface which model will be used for the fix so an opus
            // escalation is visible before the operator presses Enter.
            if let Some(ref model) = fix_model_hint {
                rework_body.push(StyledText::plain(format!(
                    "Model: {model} (auto-escalated per fix iteration)",
                )));
            }
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:rework"),
                title: StyledText::plain("Review requested changes — enter findings & start fix"),
                body: rework_body,
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("fix"),
                        label: "⏎  Save & start fix".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Esc  Not now".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: false,
                input: Some(DialogInput::TextInput(DialogTextInput {
                    value: p.findings.clone(),
                    placeholder: "What did the reviewer flag? (required)".into(),
                    cursor: Some(p.findings.len()),
                })),
            });
        }

        // ── Leg 3c / A3 (#517, #581): test FAILED → start fix confirm ────
        if let Some(ref p) = self.pending_test_fix {
            // #722: live-session gate (same as pending_auto_review above).
            if let Some(d) = self.live_session_blocking_dialog(p.issue_num, &p.coord_repo) {
                return Some(d);
            }
            // #803: compute escalated model before further borrows of `self`.
            let fix_model_hint = self.fix_model_for_issue(&p.coord_repo, p.issue_num);
            let mut body = vec![
                StyledText::plain(format!(
                    "The manual smoke test for {} #{} failed.",
                    p.coord_repo, p.issue_num,
                )),
                StyledText::plain(
                    "Start an interactive fix on the same branch, briefed with \
                     the failure (continues the PR; re-reviews after)?"
                        .to_string(),
                ),
            ];
            // #803: surface the escalated model tier before the operator confirms.
            if let Some(ref model) = fix_model_hint {
                body.push(StyledText::plain(format!(
                    "Model: {model} (auto-escalated per fix iteration)",
                )));
            }
            body.extend(self.fix_briefing_preview_lines());  // #603 preview
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:test-fix"),
                title: StyledText::plain("Test failed — start fix?"),
                body,
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("fix"),
                        label: "⏎  Start fix".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Esc  Not now".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── #863: Fix dispatch hit the iteration cap → force-past-cap confirm
        if let Some(ref p) = self.pending_fix_force_confirm {
            // #722: live-session gate, same discipline as the other Fix confirms.
            if let Some(d) = self.live_session_blocking_dialog(p.issue_num, &p.coord_repo) {
                return Some(d);
            }
            let cap_text = p
                .max_iterations
                .map(|n| n.to_string())
                .unwrap_or_else(|| "configured".to_string());
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:fix-force-cap"),
                title: StyledText::plain("Iteration cap reached"),
                body: vec![
                    StyledText::plain(format!(
                        "The iteration cap ({}) has been reached for {} #{} — \
                         `pipeline.max_review_iterations` is blocking another fix round.",
                        cap_text, p.coord_repo, p.issue_num,
                    )),
                    StyledText::plain(
                        "Force another fix round anyway? (--force, #862's override)".to_string(),
                    ),
                ],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("force-fix"),
                        label: "⏎  Force fix".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Esc  Not now".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── Leg 3c (#517, #306): review APPROVED → start merge agent confirm
        if let Some(ref p) = self.pending_merge {
            // #722: live-session gate (same as pending_auto_review above).
            if let Some(d) = self.live_session_blocking_dialog(p.issue_num, &p.coord_repo) {
                return Some(d);
            }
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:merge-agent"),
                title: StyledText::plain("Review approved — start merge agent?"),
                body: vec![
                    StyledText::plain(format!(
                        "The review of {} #{} approved the branch.",
                        p.coord_repo, p.issue_num,
                    )),
                    StyledText::plain(
                        "Start an interactive merge agent (rebases onto the default \
                         branch, resolves conflicts, pushes — then you merge)?"
                            .to_string(),
                    ),
                ],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("merge"),
                        label: "⏎  Start merge".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Esc  Not now".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── Force-merge confirm ──────────────────────────────────────────
        if let Some(ref repo) = self.pending_force_merge {
            let scope_line = if repo.is_empty() {
                "Scope: whole merge queue".to_string()
            } else {
                format!("Scope: --repo {}", repo)
            };
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:force-merge"),
                title: StyledText::plain("Force Merge"),
                body: vec![
                    StyledText::plain("Force-merge despite failed CI checks?"),
                    StyledText::plain(scope_line),
                ],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("yes"),
                        label: "y  Force merge".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: Some(Color::rgb(200, 80, 80)),
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── #780: Merge-all-ready confirm ───────────────────────────────
        if let Some(ref aids) = self.pending_merge_all_ready {
            let n = aids.len();
            let preview = if aids.len() <= 4 {
                aids.iter()
                    .map(|a| {
                        // Show PR-friendly short label: look up issue number from
                        // merge_plan; fall back to the raw assignment_id.
                        self.data.merge_plan.iter()
                            .find(|e| &e.assignment_id == a)
                            .map(|e| format!("#{}", e.issue_number))
                            .unwrap_or_else(|| a.clone())
                    })
                    .collect::<Vec<_>>()
                    .join(", ")
            } else {
                let shown = aids.iter().take(3)
                    .map(|a| {
                        self.data.merge_plan.iter()
                            .find(|e| &e.assignment_id == a)
                            .map(|e| format!("#{}", e.issue_number))
                            .unwrap_or_else(|| a.clone())
                    })
                    .collect::<Vec<_>>()
                    .join(", ");
                format!("{}, +{} more", shown, aids.len() - 3)
            };
            return Some(Dialog {
                id: WidgetId::new("dialog:merge-all-ready"),
                title: StyledText::plain("Merge All Ready"),
                body: vec![
                    StyledText::plain(format!(
                        "Merge {} READY entr{}?",
                        n,
                        if n == 1 { "y" } else { "ies" }
                    )),
                    StyledText::plain(preview),
                ],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("yes"),
                        label: format!("y  Merge {} entr{}", n, if n == 1 { "y" } else { "ies" }),
                        is_default: true,
                        is_cancel: false,
                        tint: Some(Color::rgb(60, 160, 80)),
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: false,
                table: None,
                input: None,
            });
        }

        // ── #2402: Re-verify (revalidate) confirm ───────────────────────
        // State plainly what it will do (#1214 precedent): `--revalidate`
        // runs a real build+test locally on the daemon host — cheap, no
        // tokens, but not instant, so it gets the same confirm-before-spawn
        // shape as `pending_force_merge` rather than a bare click-and-go.
        if let Some(ref p) = self.pending_merge_revalidate {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:merge-revalidate"),
                title: StyledText::plain("Re-verify (revalidate)"),
                body: vec![
                    StyledText::plain(format!(
                        "Re-test #{} against the current base?",
                        p.issue_num,
                    )),
                    StyledText::plain(
                        "Runs `coord merge --revalidate --only <assignment>` — a real \
                         build+test (or CI re-run) locally on the daemon host before \
                         merging. Only re-tests a stale-but-passed verdict; a genuine \
                         review/CI/conflict block is left untouched."
                            .to_string(),
                    ),
                ],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("yes"),
                        label: "y  Revalidate".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── #956: Kill terminal confirm ───────────────────────────────────
        if let Some(ref p) = self.pending_kill_terminal {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:kill-terminal"),
                title: StyledText::plain("Kill Terminal"),
                body: vec![StyledText::plain(format!(
                    "Kill terminal '{}' on {}? The tmux session ends immediately.",
                    p.name, p.machine,
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("yes"),
                        label: "y  Confirm kill".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: Some(Color::rgb(200, 80, 80)),
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── #1398: Stop drive confirm ───────────────────────────────────────
        if let Some(ref p) = self.pending_kill_drive {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:stop-drive"),
                title: StyledText::plain("Stop Drive"),
                body: vec![StyledText::plain(format!(
                    "Stop driving {} #{}? This kills the tmux session — the driver's \
                     lock releases immediately and the run ends wherever it currently \
                     is (in-flight Work/Test/Review/Merge is NOT rolled back).",
                    p.repo, p.issue,
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("yes"),
                        label: "y  Confirm stop".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: Some(Color::rgb(200, 80, 80)),
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── #1033: Kill session confirm ───────────────────────────────────
        if let Some(ref p) = self.pending_kill_session {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:kill-session"),
                title: StyledText::plain("Kill Session"),
                body: vec![StyledText::plain(format!(
                    "Kill session '{}'? The tmux session ends immediately.",
                    p.label,
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("yes"),
                        label: "y  Confirm kill".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: Some(Color::rgb(200, 80, 80)),
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── Restart confirm ──────────────────────────────────────────────
        if let Some(ref name) = self.pending_restart {
            let active = self
                .data
                .machines
                .iter()
                .find(|m| &m.name == name)
                .map(|m| m.active_count)
                .unwrap_or(0);
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:restart"),
                title: StyledText::plain("Restart Machine"),
                body: vec![StyledText::plain(format!(
                    "Restart {} ({} active worker{})?",
                    name,
                    active,
                    if active == 1 { "" } else { "s" },
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("yes"),
                        label: "y  Confirm restart".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── Purge confirm ────────────────────────────────────────────────
        if let Some((a, i)) = self.pending_purge {
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:purge"),
                title: StyledText::plain("Purge Old Data"),
                body: vec![StyledText::plain(format!(
                    "Purge {} assignment{} + {} closed issue{} older than {}d?",
                    a,
                    if a == 1 { "" } else { "s" },
                    i,
                    if i == 1 { "" } else { "s" },
                    self.purge_days,
                ))],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("yes"),
                        label: "y  Confirm purge".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: Some(Color::rgb(200, 80, 80)),
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── #685: Test-mode choice (headless Work/Plan start or right-click flip)
        if let Some(ref p) = self.pending_test_mode_choice {
            let is_smoke_default = p
                .current_mode
                .as_deref()
                .map(|m| m != "auto")
                .unwrap_or(true); // default is smoke
            let action_label = match p.action {
                TestModeChoiceAction::DispatchWork => "starting Work",
                TestModeChoiceAction::SetOnly => "this issue",
            };
            let smoke_label = if is_smoke_default {
                "1 ⏎  Pause for my smoke test (default)".to_string()
            } else {
                "1    Pause for my smoke test".to_string()
            };
            let auto_label = if !is_smoke_default {
                "2 ⏎  Fully automated (default)".to_string()
            } else {
                "2    Fully automated".to_string()
            };
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:test-mode-choice"),
                title: StyledText::plain(format!(
                    "Test mode for {} #{} ({})",
                    p.coord_repo, p.issue_num, action_label,
                )),
                body: vec![
                    StyledText::plain(format!(
                        "When headless Work completes, how should the Test gate proceed for #{}?",
                        p.issue_num,
                    )),
                ],
                buttons: vec![
                    DialogButton {
                        id: WidgetId::new("mode:smoke"),
                        label: smoke_label,
                        is_default: is_smoke_default,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("mode:auto"),
                        label: auto_label,
                        is_default: !is_smoke_default,
                        is_cancel: false,
                        tint: None,
                    },
                    DialogButton {
                        id: WidgetId::new("cancel"),
                        label: "Esc  Cancel".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ],
                severity: Some(DialogSeverity::Question),
                vertical_buttons: true,
                input: None,
            });
        }

        // ── #816: PTY-panic notification ────────────────────────────────────
        // Shown when a vt100 parser panic evicted an active terminal session.
        // Dismissed by Esc / Enter / outside-click.
        if let Some(ref panic_msg) = self.pty_panic_dialog {
            let body = format!(
                "A renderer fault (vt100 parser panic) evicted the active terminal \
                 session.  The session has ended; board-driven actions (Test, Review, \
                 Merge) continue normally.\n\nFault: {}",
                panic_msg
            );
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:pty-panic"),
                title: StyledText::plain("Renderer fault — session evicted"),
                body: vec![StyledText::plain(body)],
                buttons: vec![DialogButton {
                    id: WidgetId::new("close"),
                    label: "Esc / Enter  Dismiss".into(),
                    is_default: true,
                    is_cancel: true,
                    tint: None,
                }],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── #1059: Gate A dispatch-failure modal ────────────────────────────
        // Shown when a "Dispatch Gate A mock" (`coord acceptance mock`) attempt
        // fails.  The generic command-failed toast truncates a long reason to
        // ~40 cols (quadraui#771); this modal word-wraps the FULL message so a
        // config gap ("no acceptance driver configured — add it under
        // acceptance.drivers…") can't read as a permanent wedge.  Dismissed by
        // Esc / Enter / outside-click.
        if let Some(ref reason) = self.gate_a_error_dialog {
            let body = format!(
                "The Gate A mock dispatch could not start:\n\n{}\n\n\
                 Nothing was claimed and no worker is in flight — this is a \
                 clean pre-dispatch failure, not a stuck epic.  Fix the cause \
                 above and dispatch again.",
                reason
            );
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:gate-a-error"),
                title: StyledText::plain("Gate A dispatch failed"),
                body: vec![StyledText::plain(body)],
                buttons: vec![DialogButton {
                    id: WidgetId::new("close"),
                    label: "Esc / Enter  Dismiss".into(),
                    is_default: true,
                    is_cancel: true,
                    tint: None,
                }],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── #2863: decomposition-dispatch failure modal ─────────────────────
        // Same class of problem the Gate A modal above solves, same solution:
        // `coord portal decompose-chat`'s commonest failure is the
        // thin-client refusal ("must run on the daemon host…"), a
        // multi-sentence message that names its own fix and survives the
        // ~40-col toast box only as `Error: coord portal decompose-chat mus…`.
        // This is the ONE error surface for that dispatch — the generic
        // command-failed toast is suppressed, the optimistic "chat ready"
        // toast retracted and the 30 s bind-timeout toast disarmed, all by
        // `fail_pending_decomposition_chat` before this is raised.
        if let Some(ref reason) = self.decompose_chat_error_dialog {
            let body = format!(
                "{}\n\nNo session was started and nothing is in flight — this \
                 is a clean pre-dispatch failure, not a stuck session.  Fix \
                 the cause above and pull the row again, or use \"{}\" on the \
                 same row to hold the intake conversation on this machine \
                 instead.",
                reason, APPROVED_ATTENDED_INTAKE_ITEM,
            );
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:decompose-chat-error"),
                title: StyledText::plain("Decomposition session dispatch failed"),
                body: vec![StyledText::plain(body)],
                buttons: vec![DialogButton {
                    id: WidgetId::new("close"),
                    label: "Esc / Enter  Dismiss".into(),
                    is_default: true,
                    is_cancel: true,
                    tint: None,
                }],
                severity: Some(DialogSeverity::Warning),
                vertical_buttons: false,
                input: None,
            });
        }

        // ── Artifact-pull result (#532) ─────────────────────────────────────
        // Info dialog shown after `coord pull-artifact` completes, and
        // re-openable at any time by pressing `a` on the same pipeline row.
        if let Some(ref dlg) = self.artifact_pull_dialog {
            let buttons = if dlg.path.is_some() {
                vec![
                    DialogButton {
                        id: WidgetId::new("copy"),
                        label: "c  Copy path".into(),
                        is_default: true,
                        is_cancel: false,
                        tint: Some(Color::rgb(60, 200, 80)),
                    },
                    DialogButton {
                        id: WidgetId::new("close"),
                        label: "Esc  Close".into(),
                        is_default: false,
                        is_cancel: true,
                        tint: None,
                    },
                ]
            } else {
                vec![DialogButton {
                    id: WidgetId::new("close"),
                    label: "Esc  Close".into(),
                    is_default: true,
                    is_cancel: true,
                    tint: None,
                }]
            };
            let severity = if dlg.path.is_some() {
                Some(DialogSeverity::Info)
            } else {
                Some(DialogSeverity::Warning)
            };
            return Some(Dialog {
                table: None,
                id: WidgetId::new("dialog:artifact-pull"),
                title: StyledText::plain("Artifacts"),
                body: vec![StyledText::plain(dlg.body.clone())],
                buttons,
                severity,
                vertical_buttons: false,
                input: None,
            });
        }

        None
    }

    /// Render the active prompt dialog (if any) centered in `viewport`.
    /// Stores the computed layout in `self.dialog_layout` for click
    /// hit-testing on the next mouse event.
    pub(crate) fn render_prompt_dialog(&self, backend: &mut dyn Backend, viewport: Rect) {
        let Some(mut dialog) = self.build_prompt_dialog() else {
            *self.dialog_layout.borrow_mut() = None;
            return;
        };
        let lh = backend.line_height();
        let padding = lh;
        // Dialog width — clamp so it never exceeds a small viewport.
        let dialog_w = (viewport.width * 0.5)
            .clamp(30.0 * lh, 60.0 * lh)
            .min((viewport.width - 2.0 * lh).max(10.0 * lh));
        // Wrap the body to the inner width so a long question is never
        // truncated on a narrow screen — one StyledText per rendered row.
        let content_cells = ((dialog_w - 2.0 * padding) / lh).floor().max(1.0) as usize;
        dialog.body = dialog
            .body
            .iter()
            .flat_map(|line| {
                let flat: String = line.spans.iter().map(|s| s.text.as_str()).collect();
                let wrapped = word_wrap(&flat, content_cells);
                if wrapped.is_empty() {
                    vec![StyledText::plain("")]
                } else {
                    wrapped
                        .into_iter()
                        .map(StyledText::plain)
                        .collect::<Vec<_>>()
                }
            })
            .collect();
        let body_h = dialog.body.len() as f32 * lh;
        let input_h = if dialog.input.is_some() { lh } else { 0.0 };
        let btn_h = lh;
        // Per-button row height. quadraui's dialog layout stacks N vertical
        // buttons of this height itself, so we pass the height of ONE option
        // (label row + a blank spacer row) — not the whole block.
        let btn_row_h = if dialog.vertical_buttons {
            btn_h * 2.0
        } else {
            btn_h
        };
        let title_h = if dialog.title.spans.iter().any(|s| !s.text.is_empty()) {
            lh
        } else {
            0.0
        };
        let max_label_len = dialog
            .buttons
            .iter()
            .map(|b| b.label.chars().count())
            .max()
            .unwrap_or(6);
        let btn_w = (max_label_len as f32 * 0.7 * lh + padding * 2.0).max(8.0 * lh);
        let measure = DialogMeasure {
            table_height: 0.0,
            width: dialog_w,
            title_height: title_h,
            body_height: body_h,
            input_height: input_h,
            button_row_height: btn_row_h,
            button_width: btn_w,
            button_gap: lh,
            padding,
        };
        let layout = dialog.layout(viewport, measure, |_| {
            quadraui::ToolbarItemMeasure::new(0.0)
        });
        backend.draw_dialog(&dialog, &layout);
        *self.dialog_layout.borrow_mut() = Some(layout);
    }

    /// Hit-test a click against the active prompt dialog.
    ///
    /// Returns `Some(true)` when the click fired an action (or was
    /// swallowed inside the dialog body), `Some(false)` when no dialog
    /// layout is cached (no dialog showing), and the outside-click case
    /// dismisses the dialog and returns `Some(true)`.
    pub(crate) fn handle_dialog_click(&mut self, pos: Point, backend: &mut dyn Backend) -> Option<bool> {
        let layout = self.dialog_layout.borrow().clone()?;
        match layout.hit_test(pos.x, pos.y) {
            DialogHit::Outside => {
                // Click outside the dialog — dismiss (same as Esc).
                self.dismiss_prompt_dialog();
                Some(true)
            }
            DialogHit::Body | DialogHit::BodyToolbarButton(_) => {
                // Click inside dialog but not on a button — swallow.
                Some(true)
            }
            DialogHit::Button(id) => {
                let id_str = id.as_str().to_string();
                self.fire_dialog_button(&id_str, backend);
                Some(true)
            }
        }
    }

    /// Dismiss whatever prompt dialog is currently showing (Esc / outside click).
    pub(crate) fn dismiss_prompt_dialog(&mut self) {
        if self.pending_quit_confirm {
            self.pending_quit_confirm = false;
        } else if self.pending_test_mode_choice.is_some() {
            self.pending_test_mode_choice = None;
        } else if self.pending_auto_review.is_some() {
            // #722: when the blocking dialog is showing (live remote session),
            // an outside-click dismiss must NOT destroy the pending offer.  The
            // offer re-fires automatically once the session closes.
            let blocked = self
                .pending_auto_review
                .as_ref()
                .is_some_and(|p| self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo));
            if !blocked {
                self.pending_auto_review = None;
            }
        } else if self.pending_stage_launch.is_some() {
            // #722: same blocking-dialog guard for the stage-launch offer.
            let blocked = self
                .pending_stage_launch
                .as_ref()
                .is_some_and(|p| self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo));
            if !blocked {
                self.pending_stage_launch = None;
            }
        } else if self.pending_rework.is_some() {
            self.pending_rework = None;
        } else if self.pending_machine_picker.is_some() {
            self.pending_machine_picker = None;
        } else if self.pending_new_terminal_picker.is_some() {
            self.pending_new_terminal_picker = None;
        } else if self.pending_new_terminal.is_some() {
            self.pending_new_terminal = None;
        } else if self.pending_quiet_hours.is_some() {
            self.pending_quiet_hours = None;
        } else if self.pending_test_fix.is_some() {
            // #722: blocking-dialog guard for the test-fix offer.
            let blocked = self
                .pending_test_fix
                .as_ref()
                .is_some_and(|p| self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo));
            if !blocked {
                self.pending_test_fix = None;
            }
        } else if self.pending_merge.is_some() {
            // #722: blocking-dialog guard for the merge offer.
            let blocked = self
                .pending_merge
                .as_ref()
                .is_some_and(|p| self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo));
            if !blocked {
                self.pending_merge = None;
            }
        } else if self.pending_fix_force_confirm.is_some() {
            // #722: blocking-dialog guard, same discipline as the other Fix confirms.
            let blocked = self.pending_fix_force_confirm.as_ref().is_some_and(|p| {
                self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo)
            });
            if !blocked {
                self.pending_fix_force_confirm = None;
            }
        } else if self.pending_repo_picker.is_some() {
            self.pending_repo_picker = None;
        } else if self.pending_refinement_close_prompt.is_some() {
            self.pending_refinement_close_prompt = None;
        } else if self.pending_report_fix.is_some() {
            self.pending_report_fix = None;
        } else if self.pending_test_fail.is_some() {
            self.pending_test_fail = None;
        } else if self.pending_force_merge.is_some() {
            self.pending_force_merge = None;
            self.push_toast(
                "Force-merge cancelled",
                "CI gate stays in place",
                ToastSeverity::Info,
            );
        } else if self.pending_merge_revalidate.is_some() {
            self.pending_merge_revalidate = None;
            self.push_toast(
                "Revalidate cancelled",
                "entry stays blocked — nothing changed",
                ToastSeverity::Info,
            );
        } else if self.pending_restart.is_some() {
            self.pending_restart = None;
        } else if self.pending_kill_terminal.is_some() {
            self.pending_kill_terminal = None;
        } else if self.pending_kill_drive.is_some() {
            self.pending_kill_drive = None;
        } else if self.pending_kill_session.is_some() {
            self.pending_kill_session = None;
        } else if self.pending_purge.is_some() {
            self.pending_purge = None;
        } else if self.artifact_pull_dialog.is_some() {
            self.artifact_pull_dialog = None;
        } else if self.pty_panic_dialog.is_some() {
            // #816: dismiss the PTY-panic notification dialog.
            self.pty_panic_dialog = None;
        } else if self.gate_a_error_dialog.is_some() {
            // #1059: dismiss the Gate A dispatch-failure dialog.
            self.gate_a_error_dialog = None;
        } else if self.decompose_chat_error_dialog.is_some() {
            // #2863: dismiss the decomposition-dispatch failure dialog.
            self.decompose_chat_error_dialog = None;
        }
        *self.dialog_layout.borrow_mut() = None;
    }

    /// Fire the action associated with a dialog button id.
    /// Button ids mirror those set in `build_prompt_dialog`.
    pub(crate) fn fire_dialog_button(&mut self, id: &str, backend: &mut dyn Backend) {
        // ── Force-quit confirmation ──────────────────────────────────────
        // The click path can't return Reaction::Exit, so set a flag the event
        // handler checks right after mouse dispatch.
        if self.pending_quit_confirm {
            if id == "force-quit" {
                self.quit_requested = true;
            } else {
                self.pending_quit_confirm = false;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Test → Review confirm (Test precedes Review) ─────────────────
        if self.pending_auto_review.is_some() {
            if id == "review" {
                self.confirm_auto_review();
            } else if id == "close" {
                // "close" is the blocking dialog's only button (#722) — the
                // operator dismissed the "reattach first" notice; preserve the
                // pending offer so it re-fires once the session closes.
                if let Some(ref p) = self.pending_auto_review {
                    let n = p.issue_num;
                    self.push_toast(
                        "Reattach first",
                        &format!(
                            "Close the live session for #{n} first; \
                             the review offer will re-appear automatically.",
                        ),
                        ToastSeverity::Warning,
                    );
                }
                // Do NOT clear pending_auto_review.
            } else {
                // "cancel" or anything else → normal defer.
                self.pending_auto_review = None;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Post-review one-key stage offer (Fix / Test) ─────────────────
        if self.pending_stage_launch.is_some() {
            if id == "stage-go" {
                self.confirm_stage_launch();
            } else if id == "close" {
                // "close" = blocking dialog dismiss (#722) — preserve the offer.
                if let Some(ref p) = self.pending_stage_launch {
                    let n = p.issue_num;
                    self.push_toast(
                        "Reattach first",
                        &format!(
                            "Close the live session for #{n} first; \
                             the stage offer will re-appear automatically.",
                        ),
                        ToastSeverity::Warning,
                    );
                }
            } else {
                self.pending_stage_launch = None;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Leg 3 (#517): rework (request-changes) ───────────────────────
        if self.pending_rework.is_some() {
            if id == "fix" {
                self.confirm_rework();
            } else {
                self.pending_rework = None;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #685: test-mode choice ────────────────────────────────────────
        if self.pending_test_mode_choice.is_some() {
            let mode = match id {
                "mode:smoke" => Some("smoke"),
                "mode:auto" => Some("auto"),
                _ => None, // "cancel" or anything else → dismiss
            };
            if let Some(mode) = mode {
                if let Some(choice) = self.pending_test_mode_choice.take() {
                    self.confirm_test_mode_choice(choice, mode);
                }
            } else {
                self.pending_test_mode_choice = None;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #486 Leg 4: Machine picker (remote Review/Fix) ───────────────
        if self.pending_machine_picker.is_some() {
            if id == "cancel" {
                self.pending_machine_picker = None;
            } else if let Some(idx_str) = id.strip_prefix("machine:") {
                if let Ok(idx) = idx_str.parse::<usize>() {
                    if let Some(picker) = self.pending_machine_picker.take() {
                        if let Some(entry) = picker.machines.get(idx) {
                            let mode = picker.mode;
                            let machine = entry.name.clone();
                            self.launch_interactive_session_on_machine(mode, machine, None, false);
                        }
                    }
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #954: "New terminal" machine picker (mouse) ──────────────────
        if self.pending_new_terminal_picker.is_some() {
            if id == "cancel" {
                self.pending_new_terminal_picker = None;
            } else if let Some(idx_str) = id.strip_prefix("new-terminal-machine:") {
                if let Ok(idx) = idx_str.parse::<usize>() {
                    if let Some(machines) = self.pending_new_terminal_picker.take() {
                        if let Some(entry) = machines.get(idx) {
                            let machine = entry.name.clone();
                            self.begin_new_terminal_name_prompt(machine);
                        }
                    }
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #954 bug 2: New-terminal name prompt (mouse) ──────────────────
        // The name-entry dialog's "Submit" button previously had NO click
        // dispatch here, so only the Enter key (`events.rs`) created the
        // terminal — a click on Submit did nothing. Mirror that keyboard
        // path: "submit" creates + attaches (empty buffer ⇒ auto-name),
        // anything else (Cancel / outside click) discards.
        if self.pending_new_terminal.is_some() {
            match id {
                "submit" => {
                    if let Some(input) = self.pending_new_terminal.take() {
                        self.create_and_attach_terminal(input.machine, input.buf);
                    }
                }
                _ => {
                    self.pending_new_terminal = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #2147: Quiet-hours window prompt (mouse) ──────────────────────
        // "submit" validates + spawns (`submit_quiet_hours` re-arms the
        // dialog itself on a parse error, so no early return here for
        // that case — the dialog layout cache is cleared either way since
        // the dialog CONTENT changes, not because it necessarily closes).
        // "clear" spawns `--clear` directly; anything else discards.
        if self.pending_quiet_hours.is_some() {
            match id {
                "submit" => self.submit_quiet_hours(),
                "clear" => {
                    if let Some(input) = self.pending_quiet_hours.take() {
                        self.dispatch_quiet_hours_clear(&input.machine);
                    }
                }
                _ => {
                    self.pending_quiet_hours = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Leg 3c / A3 (#517, #581): test failed → start fix ────────────
        if self.pending_test_fix.is_some() {
            if id == "fix" {
                self.confirm_test_fix();
            } else if id == "close" {
                // "close" = blocking dialog dismiss (#722) — preserve the offer.
                if let Some(ref p) = self.pending_test_fix {
                    let n = p.issue_num;
                    self.push_toast(
                        "Reattach first",
                        &format!(
                            "Close the live session for #{n} first; \
                             the fix offer will re-appear automatically.",
                        ),
                        ToastSeverity::Warning,
                    );
                }
            } else {
                self.pending_test_fix = None;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Leg 3c (#517, #306): test passed → start merge agent ─────────
        if self.pending_merge.is_some() {
            if id == "merge" {
                self.confirm_merge();
            } else if id == "close" {
                // "close" = blocking dialog dismiss (#722) — preserve the offer.
                if let Some(ref p) = self.pending_merge {
                    let n = p.issue_num;
                    self.push_toast(
                        "Reattach first",
                        &format!(
                            "Close the live session for #{n} first; \
                             the merge offer will re-appear automatically.",
                        ),
                        ToastSeverity::Warning,
                    );
                }
            } else {
                self.pending_merge = None;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #863: iteration cap reached → force-past-cap confirm ─────────
        if self.pending_fix_force_confirm.is_some() {
            if id == "force-fix" {
                self.confirm_fix_force_past_cap();
            } else if id == "close" {
                // "close" = blocking dialog dismiss (#722) — preserve the offer.
                if let Some(ref p) = self.pending_fix_force_confirm {
                    let n = p.issue_num;
                    self.push_toast(
                        "Reattach first",
                        &format!(
                            "Close the live session for #{n} first; \
                             the force-fix offer will re-appear automatically.",
                        ),
                        ToastSeverity::Warning,
                    );
                }
            } else {
                self.pending_fix_force_confirm = None;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #935 Part B: Diagnose & fix stage dialog ─────────────────────
        if self.pending_diagnose_dialog.is_some() {
            // Route the button id to the appropriate action.  The action
            // dispatch also clears pending_diagnose_dialog where needed.
            match id {
                "diagnose-stage" | "diagnose-reset" | "diagnose-clear-phantom" => {
                    // These are real action IDs: dispatch to
                    // dispatch_context_menu_action which handles them (including
                    // the phantom-clear path which takes the dialog from self).
                    // We can't call dispatch_context_menu_action directly here
                    // (borrow conflict), so inline the relevant logic:
                    if id == "diagnose-stage" {
                        self.dispatch_diagnose_for_selected_pipeline_row(false, false, false);
                        self.pending_diagnose_dialog = None;
                    } else if id == "diagnose-reset" {
                        self.dispatch_diagnose_for_selected_pipeline_row(true, false, false);
                        self.pending_diagnose_dialog = None;
                    } else {
                        // diagnose-clear-phantom
                        if let Some(dlg) = self.pending_diagnose_dialog.take() {
                            let repo = dlg.repo.clone();
                            let issue_number = dlg.issue_number;
                            let before = self.live_tmux_sessions.len();
                            self.live_tmux_sessions.retain(|s| {
                                !(s.assignment_id.starts_with("pending-")
                                    && s.issue_number == Some(issue_number)
                                    && s.repo_name.as_deref() == Some(&repo))
                            });
                            let removed = before - self.live_tmux_sessions.len();
                            self.push_toast(
                                "Phantom session cleared",
                                &format!(
                                    "#{issue_number}: removed {removed} phantom live-session \
                                     entr{}. The card will show as 'ready' on the next refresh.",
                                    if removed == 1 { "y" } else { "ies" },
                                ),
                                ToastSeverity::Info,
                            );
                        }
                    }
                }
                _ => {
                    // "cancel" or anything else → dismiss without action.
                    self.pending_diagnose_dialog = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Repo picker ──────────────────────────────────────────────────
        if self.pending_repo_picker.is_some() {
            if id == "cancel" {
                self.pending_repo_picker = None;
            } else if let Some(idx_str) = id.strip_prefix("repo:") {
                if let Ok(idx) = idx_str.parse::<usize>() {
                    if let Some(picker) = self.pending_repo_picker.take() {
                        if let Some(repo) = picker.repos.get(idx) {
                            let repo = repo.clone();
                            self.dispatch_board_chat_new_issue(&repo);
                        }
                    }
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Refinement-close (#410: Cancel/Save/Send) ────────────────────
        if self.pending_refinement_close_prompt.is_some() {
            match id {
                "save" => {
                    // Save: draft notes + mark ready, stay on Board.
                    self.pending_refinement_close_prompt = None;
                    self.finalise_after_notes_post = true;
                    self.trigger_refinement_notes_synth();
                    if self.pending_refinement_notes_synth.is_none() {
                        self.finalise_after_notes_post = false;
                    }
                }
                "send" => {
                    // Send: draft notes + mark ready + dispatch to pipeline.
                    self.pending_refinement_close_prompt = None;
                    self.finalise_after_notes_post = true;
                    self.refine_then_dispatch = true;
                    self.trigger_refinement_notes_synth();
                    if self.pending_refinement_notes_synth.is_none() {
                        self.finalise_after_notes_post = false;
                        self.refine_then_dispatch = false;
                    }
                }
                _ => {
                    // Cancel (Esc / outside click): discard, issue unchanged.
                    self.pending_refinement_close_prompt = None;
                    self.cancel_refinement_chat();
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Report-fix ───────────────────────────────────────────────────
        if self.pending_report_fix.is_some() {
            match id {
                "submit" => {
                    let description = self.pending_report_fix.take().unwrap_or_default();
                    let description = description.trim().to_string();
                    let reason_opt = if description.is_empty() {
                        None
                    } else {
                        Some(description.as_str())
                    };
                    if self.record_test_verdict("failed", reason_opt) {
                        if let Some(work_id) = self.pipeline_selected_work_id() {
                            let args: Vec<String> = if description.is_empty() {
                                vec!["fix".to_string(), work_id.clone()]
                            } else {
                                vec![
                                    "fix".to_string(),
                                    work_id.clone(),
                                    "--guidance".to_string(),
                                    description.clone(),
                                ]
                            };
                            let args_ref: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
                            let issue_num = self
                                .pipeline_sel
                                .and_then(|i| self.pipeline_issues.get(i))
                                .map(|iss| iss.number)
                                .unwrap_or(0);
                            use crate::commands::SpawnQueuedOutcome;
                            match self.command_runner.spawn_queued(&args_ref) {
                                SpawnQueuedOutcome::Deduped => {}
                                SpawnQueuedOutcome::Queued => {
                                    self.push_toast(
                                        "Fix worker queued",
                                        &format!("Fix worker queued for #{} — will dispatch after current command.", issue_num),
                                        ToastSeverity::Info,
                                    );
                                }
                                SpawnQueuedOutcome::Started => {
                                    self.push_toast(
                                        "Fix worker dispatched",
                                        &format!("Fix worker dispatched for #{}", issue_num),
                                        ToastSeverity::Info,
                                    );
                                }
                            }
                        }
                    }
                    self.pending_report_fix = None;
                }
                _ => {
                    self.pending_report_fix = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #977 Plan-capture ────────────────────────────────────────────
        if self.pending_plan_capture.is_some() {
            match id {
                "submit" => {
                    let title = self.pending_plan_capture.take().unwrap_or_default();
                    self.capture_plan_stub(title);
                }
                _ => {
                    self.pending_plan_capture = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #1017 New-milestone-via-chat ─────────────────────────────────
        if self.pending_new_milestone_chat.is_some() {
            match id {
                "submit" => {
                    let title = self.pending_new_milestone_chat.take().unwrap_or_default();
                    self.capture_plan_chat(title);
                }
                _ => {
                    self.pending_new_milestone_chat = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #1003 Plans-row single-field input ─────────────────────────────
        if self.pending_milestone_row_input.is_some() {
            match id {
                "submit" => {
                    if let Some(input) = self.pending_milestone_row_input.take() {
                        self.submit_milestone_row_input(input);
                    }
                }
                _ => {
                    self.pending_milestone_row_input = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #1755 (DQ-3) "Add to drive queue after…" ────────────────────────
        if self.pending_drive_queue_after.is_some() {
            match id {
                "submit" => {
                    if let Some(input) = self.pending_drive_queue_after.take() {
                        self.submit_drive_queue_after_input(input);
                    }
                }
                _ => {
                    self.pending_drive_queue_after = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #2500 "What needs to change?" Gate-A note ───────────────────────
        // "submit" dispatches `coord gate-a --changes ... --note <buf>`
        // (blank buf OK); anything else (Cancel, or the dialog's own Esc
        // route via `events.rs`) drops the pending input with no dispatch.
        if self.pending_gate_a_changes_note.is_some() {
            match id {
                "submit" => {
                    if let Some(input) = self.pending_gate_a_changes_note.take() {
                        self.submit_gate_a_changes_note_input(input);
                    }
                }
                _ => {
                    self.pending_gate_a_changes_note = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #1003 Close / archive plan confirm ──────────────────────────────
        if self.pending_close_plan.is_some() {
            match id {
                "yes" => {
                    if let Some(plan) = self.pending_close_plan.take() {
                        self.confirm_close_plan(plan);
                    }
                }
                _ => {
                    self.pending_close_plan = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Test-fail ────────────────────────────────────────────────────
        if self.pending_test_fail.is_some() {
            match id {
                "submit" => {
                    let reason = self
                        .pending_test_fail
                        .as_ref()
                        .map(|(_, b)| b.trim().to_string())
                        .unwrap_or_default();
                    let reason_opt = if reason.is_empty() {
                        None
                    } else {
                        Some(reason.as_str())
                    };
                    self.record_test_verdict("failed", reason_opt);
                    self.pending_test_fail = None;
                }
                _ => {
                    self.pending_test_fail = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Force-merge ──────────────────────────────────────────────────
        if let Some(ref repo) = self.pending_force_merge.clone() {
            match id {
                "yes" => {
                    let scoped = !repo.is_empty();
                    let mut args: Vec<&str> = vec!["merge", "--force-merge"];
                    if scoped {
                        args.push("--repo");
                        args.push(repo);
                    }
                    use crate::commands::SpawnQueuedOutcome;
                    let scope_str = if scoped {
                        format!(" --repo {}", repo)
                    } else {
                        String::new()
                    };
                    match self.command_runner.spawn_queued(&args) {
                        SpawnQueuedOutcome::Started => {
                            self.push_toast(
                                "Force-merge dispatched",
                                &format!(
                                    "coord merge --force-merge{} — CI gate bypassed",
                                    scope_str
                                ),
                                ToastSeverity::Warning,
                            );
                        }
                        SpawnQueuedOutcome::Queued => {
                            self.push_toast(
                                "⏳ Queued",
                                "force-merge runs after current command",
                                ToastSeverity::Info,
                            );
                        }
                        SpawnQueuedOutcome::Deduped => {}
                    }
                    self.pending_force_merge = None;
                }
                _ => {
                    self.pending_force_merge = None;
                    self.push_toast(
                        "Force-merge cancelled",
                        "CI gate stays in place",
                        ToastSeverity::Info,
                    );
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #2402: Re-verify (revalidate) ──────────────────────────────────
        if let Some(p) = self.pending_merge_revalidate.clone() {
            match id {
                "yes" => {
                    self.pending_merge_revalidate = None;
                    self.confirm_merge_revalidate(p);
                }
                _ => {
                    self.pending_merge_revalidate = None;
                    self.push_toast(
                        "Revalidate cancelled",
                        "entry stays blocked — nothing changed",
                        ToastSeverity::Info,
                    );
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #956: Kill terminal ────────────────────────────────────────────
        if let Some(p) = self.pending_kill_terminal.clone() {
            if id == "yes" {
                self.confirm_kill_terminal(p);
            } else {
                self.pending_kill_terminal = None;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #1398: Stop drive ────────────────────────────────────────────────
        if let Some(p) = self.pending_kill_drive.clone() {
            if id == "yes" {
                self.confirm_kill_drive(p);
            } else {
                self.pending_kill_drive = None;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── #1033: Kill session ─────────────────────────────────────────────
        if let Some(p) = self.pending_kill_session.clone() {
            if id == "yes" {
                self.confirm_kill_session(p);
            } else {
                self.pending_kill_session = None;
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Restart ──────────────────────────────────────────────────────
        if let Some(name) = self.pending_restart.clone() {
            match id {
                "yes" => {
                    use crate::commands::SpawnQueuedOutcome;
                    match self.command_runner.spawn_queued(&[
                        "agent",
                        "restart",
                        "--machine",
                        &name,
                    ]) {
                        SpawnQueuedOutcome::Queued => {
                            self.push_toast(
                                "⏳ Queued",
                                "agent restart runs after current command",
                                ToastSeverity::Info,
                            );
                        }
                        SpawnQueuedOutcome::Deduped | SpawnQueuedOutcome::Started => {}
                    }
                    self.pending_restart = None;
                }
                _ => {
                    self.pending_restart = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
            return;
        }

        // ── Purge ────────────────────────────────────────────────────────
        if self.pending_purge.is_some() {
            match id {
                "yes" => {
                    let secs = self.purge_days as f64 * 86_400.0;
                    match purge_done_assignments_db(secs) {
                        Ok((a, i)) => self.push_toast(
                            "Purge complete",
                            &format!(
                                "Removed {} assignment{} + {} closed issue{}",
                                a,
                                if a == 1 { "" } else { "s" },
                                i,
                                if i == 1 { "" } else { "s" }
                            ),
                            ToastSeverity::Info,
                        ),
                        Err(e) => {
                            self.push_toast("Purge failed", &format!("{}", e), ToastSeverity::Error)
                        }
                    }
                    self.pending_purge = None;
                    self.refresh();
                }
                _ => {
                    self.pending_purge = None;
                }
            }
            *self.dialog_layout.borrow_mut() = None;
        }

        // ── Artifact-pull result (#532) ─────────────────────────────────────
        if self.artifact_pull_dialog.is_some() {
            if id == "copy" {
                if let Some(path) = self
                    .artifact_pull_dialog
                    .as_ref()
                    .and_then(|d| d.path.clone())
                {
                    backend.services().clipboard().write_text(&path);
                    self.push_toast("Copied", "Path copied to clipboard", ToastSeverity::Info);
                }
            }
            // Both "copy" and "close" dismiss the dialog.
            self.artifact_pull_dialog = None;
            *self.dialog_layout.borrow_mut() = None;
        }

        // ── #816: PTY-panic notification ────────────────────────────────────
        if self.pty_panic_dialog.is_some() && id == "close" {
            self.pty_panic_dialog = None;
            *self.dialog_layout.borrow_mut() = None;
        }

        // ── #1059: Gate A dispatch-failure notification ─────────────────────
        if self.gate_a_error_dialog.is_some() && id == "close" {
            self.gate_a_error_dialog = None;
            *self.dialog_layout.borrow_mut() = None;
        }

        // ── #2863: decomposition-dispatch failure notification ──────────────
        if self.decompose_chat_error_dialog.is_some() && id == "close" {
            self.decompose_chat_error_dialog = None;
            *self.dialog_layout.borrow_mut() = None;
        }
    }

    /// #266: shared helper for board-row context-menu actions that
    /// spawn a single `coord <subcommand> <repo> <issue>` command.
    /// `toast_title` and `body_template` (with `{}` for the issue
    /// number) drive the dispatch toast; on missing target data, the
    /// helper surfaces a guidance toast and returns `false` without
    /// spawning anything.
    /// #264: finalise an active refinement chat — close the worker and
    /// flip the GitHub label so the issue moves Refining → Refined.  Best
    /// effort: failures surface via the existing command-runner toast on
    /// non-zero exit (`bead06a`).
    ///
    /// Called when Esc closes the refinement chat overlay.  Reads the
    /// focused watch context for the worker id + repo + issue rather than
    /// re-parsing — that state is already in `watch_pool` from
    /// `maybe_bind_pending_refinement`.
    pub(crate) fn finalise_refinement_chat(&mut self) {
        let (aid, repo, issue_n) = match self.focused_watch_state() {
            Some(w) => (w.assignment_id.clone(), w.repo.clone(), w.issue_number),
            None => return,
        };
        // CommandRunner is single-slot.  We fire `coord stop` now and
        // queue the follow-up `coord ready` for the post-stop poll handler
        // — leaves a clean sequence (stop → ready), and a failed stop
        // keeps the issue in status:refining rather than racing the label
        // flip.
        let then_dispatch = std::mem::take(&mut self.refine_then_dispatch);
        // Use spawn_queued so the stop is enqueued if another command is already
        // running — the pending_refine_ready handler fires when the stop eventually
        // completes via poll(), so the stop → ready sequence is always preserved.
        self.command_runner.spawn_queued(&["stop", &aid]);
        self.pending_refine_ready = Some(PendingRefineReady {
            repo,
            issue_number: issue_n,
            assignment_id: aid,
            then_dispatch,
        });
        let action = if then_dispatch {
            "stopping worker, then marking ready + dispatching…"
        } else {
            "stopping worker, then marking ready…"
        };
        self.push_toast(
            "Refine with chat",
            &format!("#{}: {}", issue_n, action),
            ToastSeverity::Info,
        );
    }

    /// #410: Stop the refinement worker without flipping any status labels.
    /// Used when the user picks Cancel in the close dialog — the issue stays
    /// in its current state (Refining / Backlog / whatever).
    pub(crate) fn cancel_refinement_chat(&mut self) {
        self.refine_then_dispatch = false;
        if let Some(id) = self.watch_focused.clone() {
            self.command_runner.spawn_queued(&["stop", &id]);
            let issue_n = self
                .watch_pool
                .get(&id)
                .map(|c| c.state.issue_number)
                .unwrap_or(0);
            if issue_n != 0 {
                self.push_toast(
                    "Refine cancelled",
                    &format!("#{}: stopping worker, no label change.", issue_n),
                    ToastSeverity::Info,
                );
            }
        }
        self.inject_chat = None;
        self.watch_focused = None;
    }

    /// #319 Phase A: trigger the "Add refinement notes" finaliser.  Called
    /// when the user presses Ctrl+N in a refinement chat.  Composes the
    /// synth prompt with today's date, dispatches it as a user turn via
    /// [`Self::submit_inject`], and arms `pending_refinement_notes_synth`
    /// so the tick poll can capture the assistant's reply and open the
    /// review modal.
    pub(crate) fn trigger_refinement_notes_synth(&mut self) {
        if self.refinement_notes_modal.is_some() || self.pending_refinement_notes_synth.is_some() {
            self.push_toast(
                "Refinement notes",
                "Notes flow already in progress — finish it or Esc to cancel.",
                ToastSeverity::Info,
            );
            return;
        }
        let (aid, issue_number, repo_coord) = match self.focused_watch_state() {
            Some(w) => (w.assignment_id.clone(), w.issue_number, w.repo.clone()),
            None => {
                self.push_toast(
                    "Refinement notes",
                    "No focused chat — open a refinement chat first.",
                    ToastSeverity::Info,
                );
                return;
            }
        };
        // Resolve coord-local repo name → GitHub `owner/name` slug.  The
        // pipeline_repos map is the same one the merge-queue uses for
        // `gh pr` calls, so the slug is authoritative.
        let repo_github = match self
            .data
            .pipeline_repos
            .iter()
            .find(|(coord, _)| coord == &repo_coord)
            .map(|(_, slug)| slug.clone())
        {
            Some(slug) => slug,
            None => {
                self.push_toast(
                    "Refinement notes",
                    &format!(
                        "No GitHub slug found for repo '{}' — check coordinator.yml.",
                        repo_coord,
                    ),
                    ToastSeverity::Warning,
                );
                return;
            }
        };
        // Snapshot the SSE baseline BEFORE submit_inject runs.  Without
        // this, the assistant's reply lines couldn't be distinguished from
        // earlier turns.
        let baseline_sse_lines = self
            .watch_pool
            .get(&aid)
            .map(|c| c.sse.lines.len())
            .unwrap_or(0);
        let today = today_yyyy_mm_dd();
        let prompt = REFINEMENT_NOTES_SYNTH_PROMPT.replace("{DATE}", &today);
        // Suppress the generic "Steer sent" toast — the user hit Ctrl+N
        // for refinement-notes synth, not the steer keybind, so they
        // shouldn't see a "Steer sent" confirmation.  This path emits its
        // own tailored "asking the chat to draft notes…" toast below.
        if !self.submit_inject_with_toast(prompt, false) {
            self.push_toast(
                "Refinement notes",
                "Couldn't send the synth prompt — chat busy or unavailable.",
                ToastSeverity::Warning,
            );
            return;
        }
        self.pending_refinement_notes_synth = Some(PendingRefinementNotesSynth {
            aid_at_trigger: aid,
            issue_number,
            repo_coord,
            repo_github,
            baseline_sse_lines,
            armed_at: Instant::now(),
        });
        self.push_toast(
            "Refinement notes",
            &format!("#{}: asking the chat to draft notes…", issue_number),
            ToastSeverity::Info,
        );
    }

    /// #319 Phase A: called each tick while `pending_refinement_notes_synth`
    /// is armed.  Watches the focused chat for the assistant's reply to the
    /// synth prompt and, once `end_turn` lands, opens the review modal with
    /// the captured body.  Returns true when state changed (caller redraws).
    pub(crate) fn poll_refinement_notes_synth(&mut self) -> bool {
        let pending = match self.pending_refinement_notes_synth.clone() {
            Some(p) => p,
            None => return false,
        };
        if pending.armed_at.elapsed() > REFINEMENT_NOTES_SYNTH_TIMEOUT {
            self.pending_refinement_notes_synth = None;
            self.push_toast(
                "Refinement notes timed out",
                &format!(
                    "No reply in {}s — try again.",
                    REFINEMENT_NOTES_SYNTH_TIMEOUT.as_secs(),
                ),
                ToastSeverity::Warning,
            );
            return true;
        }
        // If a chat-resume is in flight, the old worker already exited
        // (sse.done=true on the still-focused old ctx) but the resume
        // worker hasn't replied yet.  Reading now would extract nothing
        // and toast "empty reply".  Wait for the rebind to finish.
        if self.pending_chat_resume.is_some() {
            return false;
        }
        // The focused chat may have rebound to a new aid after a
        // chat-continue resume — track the live focused id, not the
        // trigger-time one.  baseline_sse_lines only applies when the
        // ctx is the same one; otherwise use 0.
        let focused_id = match self.watch_focused.clone() {
            Some(id) => id,
            None => return false,
        };
        let ctx = match self.watch_pool.get(&focused_id) {
            Some(c) => c,
            None => return false,
        };
        if !ctx.sse.done {
            return false;
        }
        let floor = if focused_id == pending.aid_at_trigger {
            pending.baseline_sse_lines
        } else {
            0
        };
        let body = extract_assistant_text_after(ctx, floor);
        if body.trim().is_empty() {
            self.pending_refinement_notes_synth = None;
            self.push_toast(
                "Refinement notes",
                "Chat reply was empty — try again after a fuller refinement.",
                ToastSeverity::Warning,
            );
            return true;
        }
        self.refinement_notes_modal = Some(RefinementNotesModal {
            issue_number: pending.issue_number,
            repo_github: pending.repo_github.clone(),
            body,
            posting: false,
        });
        self.pending_refinement_notes_synth = None;
        true
    }

    /// #319 Phase A: handle a keypress while the review-and-post modal is
    /// open.  All keys land here exclusively — the inject_chat overlay
    /// and the rest of the app don't see them.  Returns true if anything
    /// changed (caller redraws).
    pub(crate) fn handle_refinement_notes_modal_key(
        &mut self,
        key: &Key,
        modifiers: &quadraui::Modifiers,
    ) -> bool {
        // `posting` lockout is read first so we don't accidentally
        // mutate the body while gh is in flight.
        let posting = self
            .refinement_notes_modal
            .as_ref()
            .map(|m| m.posting)
            .unwrap_or(false);
        if posting {
            return false;
        }
        match key {
            Key::Named(NamedKey::Escape) => {
                self.refinement_notes_modal = None;
                // #328: clear the close-prompt's finalise commitment too —
                // the user backed out, so don't quietly finalise on the
                // next post.  Their next Esc returns to the close prompt
                // afresh.
                self.finalise_after_notes_post = false;
                self.push_toast(
                    "Refinement notes",
                    "Cancelled — nothing posted.",
                    ToastSeverity::Info,
                );
                true
            }
            Key::Char('y') | Key::Char('Y') if modifiers.ctrl => {
                self.post_refinement_notes();
                true
            }
            Key::Named(NamedKey::Enter) => {
                if let Some(m) = self.refinement_notes_modal.as_mut() {
                    m.body.push('\n');
                }
                true
            }
            Key::Named(NamedKey::Backspace) => {
                if let Some(m) = self.refinement_notes_modal.as_mut() {
                    m.body.pop();
                }
                true
            }
            Key::Char(ch) if !modifiers.ctrl && !modifiers.alt && !modifiers.cmd => {
                if let Some(m) = self.refinement_notes_modal.as_mut() {
                    m.body.push(*ch);
                }
                true
            }
            _ => false,
        }
    }

    /// #319 Phase A: spawn `gh issue comment` in a background thread.
    /// Mirrors [`spawn_chat_continue`]'s fire-and-forget shape — the result
    /// lands on `refinement_notes_post_rx` and is drained on the next tick
    /// so the TUI never blocks on the network.
    pub(crate) fn post_refinement_notes(&mut self) {
        let (issue_number, repo_github, body) = match self.refinement_notes_modal.as_mut() {
            Some(m) => {
                m.posting = true;
                (m.issue_number, m.repo_github.clone(), m.body.clone())
            }
            None => return,
        };
        let (tx, rx) = std::sync::mpsc::channel();
        self.refinement_notes_post_rx = Some(rx);
        std::thread::spawn(move || {
            let mut cmd = std::process::Command::new("gh");
            cmd.arg("issue")
                .arg("comment")
                .arg(issue_number.to_string());
            cmd.arg("--repo").arg(&repo_github);
            cmd.arg("--body").arg(&body);
            let out = cmd
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::piped())
                .output();
            let result = match out {
                Ok(o) => RefinementNotesPostResult {
                    success: o.status.success(),
                    stderr_first_line: first_meaningful_stderr_line(&String::from_utf8_lossy(
                        &o.stderr,
                    ))
                    .unwrap_or_default(),
                    issue_number,
                },
                Err(e) => RefinementNotesPostResult {
                    success: false,
                    stderr_first_line: format!("spawn failed: {}", e),
                    issue_number,
                },
            };
            let _ = tx.send(result);
        });
        self.push_toast(
            "Refinement notes",
            &format!("#{}: posting comment…", issue_number),
            ToastSeverity::Info,
        );
    }

    /// #319 Phase A: drain the post-result receiver if a `gh` shell-out is
    /// in flight.  On success the modal closes; on failure it stays open
    /// (with `posting` cleared) so the user can retry without retyping.
    /// Returns true when state changed (caller redraws).
    pub(crate) fn poll_refinement_notes_post(&mut self) -> bool {
        let result = match self.refinement_notes_post_rx.as_ref() {
            Some(rx) => match rx.try_recv() {
                Ok(r) => r,
                Err(std::sync::mpsc::TryRecvError::Empty) => return false,
                Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                    self.refinement_notes_post_rx = None;
                    return false;
                }
            },
            None => return false,
        };
        self.refinement_notes_post_rx = None;
        if result.success {
            self.refinement_notes_modal = None;
            self.push_toast(
                "Refinement notes posted",
                &format!("#{}: comment added.", result.issue_number),
                ToastSeverity::Info,
            );
            // #328: when the close-prompt's Y path triggered this post,
            // chain the finalise (stop + ready) now that the comment is on
            // the issue.  Y committed to "draft, post, then mark ready" —
            // skipping finalise here would leave the chat open after
            // success, which contradicts the choice the user made.
            if self.finalise_after_notes_post {
                self.finalise_after_notes_post = false;
                self.finalise_refinement_chat();
                self.inject_chat = None;
            }
        } else {
            if let Some(ref mut m) = self.refinement_notes_modal {
                m.posting = false;
            }
            let reason = if result.stderr_first_line.is_empty() {
                "gh failed (no stderr captured)".to_string()
            } else {
                result.stderr_first_line.clone()
            };
            self.push_toast(
                "Post failed — modal kept open",
                &format!("#{}: {}", result.issue_number, reason),
                ToastSeverity::Error,
            );
        }
        true
    }

    /// #319 Phase A: render the review-and-post modal as a centered
    /// bordered list overlay above the chat.  Each line of `body` is one
    /// list item; a trailing hints row tells the user the keybinds.  The
    /// `_` caret on the last content line marks the edit cursor.
    pub(crate) fn render_refinement_notes_modal(&self, backend: &mut dyn Backend, viewport: Rect) {
        let modal = match self.refinement_notes_modal.as_ref() {
            Some(m) => m,
            None => return,
        };
        // Centre the box at 80% of the viewport, clamped so it stays
        // sensible on a wide panel and doesn't disappear on a narrow one.
        let width = (viewport.width * 0.8)
            .clamp(40.0, 120.0)
            .min(viewport.width.max(20.0));
        let height = (viewport.height * 0.8).clamp(10.0, viewport.height.max(10.0));
        let x = viewport.x + ((viewport.width - width) / 2.0).max(0.0);
        let y = viewport.y + ((viewport.height - height) / 2.0).max(0.0);
        let modal_rect = Rect::new(x, y, width, height);

        let mut items: Vec<ListItem> = modal
            .body
            .split('\n')
            .map(|line| ListItem {
                text: StyledText::plain(line.to_string()),
                icon: None,
                detail: None,
                decoration: Decoration::default(),
            })
            .collect();
        // Append a visible caret on the last line so the user can see
        // where their next char will land.  Hidden while posting.
        if !modal.posting {
            if let Some(last) = items.last_mut() {
                let plain: String = last.text.spans.iter().map(|s| s.text.as_str()).collect();
                last.text = StyledText::plain(format!("{}_", plain));
            }
        }
        // Blank separator + hints row.
        items.push(ListItem {
            text: StyledText::plain(String::new()),
            icon: None,
            detail: None,
            decoration: Decoration::default(),
        });
        let hints = if modal.posting {
            "  Posting to GitHub…  ".to_string()
        } else {
            "  Ctrl+Y = post   Enter = newline   Backspace = delete   Esc = cancel  ".to_string()
        };
        items.push(ListItem {
            text: StyledText::plain(hints),
            icon: None,
            detail: None,
            decoration: Decoration::default(),
        });

        let title = StyledText::plain(format!(" Refinement notes → #{} ", modal.issue_number,));
        let list = ListView {
            id: WidgetId::new("refinement-notes-modal"),
            title: Some(title),
            items,
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: true,
            bordered: true,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        };
        backend.draw_list(modal_rect, &list);
    }

    /// #264: shell `coord refine-chat <repo> <issue>` and arm
    /// `pending_refinement` so the next tick can bind the chat overlay to
    /// the new assignment row when it appears in the DB.
    pub(crate) fn dispatch_refine_chat(&mut self, target: &ContextMenuTarget) -> bool {
        let (repo, num) = match target {
            ContextMenuTarget::BoardRow {
                issue_number: Some(num),
                repo_name: Some(repo),
                ..
            } => (repo.clone(), *num),
            _ => {
                self.push_toast(
                    "Refine with chat unavailable",
                    "No issue + repo target — focus a row first.",
                    ToastSeverity::Info,
                );
                return false;
            }
        };
        let num_str = num.to_string();
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self
            .command_runner
            .spawn_queued(&["refine-chat", &repo, &num_str]);
        if outcome == SpawnQueuedOutcome::Deduped {
            return false;
        }
        // Arm the bind so the next tick that sees the new assignment row
        // can open the chat overlay.  Arm immediately regardless of whether
        // the command started or was queued — the 30 s window is generous
        // enough for any realistic queue depth.
        self.pending_refinement = Some(PendingRefinement {
            repo,
            issue_number: num,
            dispatched_at: Instant::now(),
        });
        let msg = if outcome == SpawnQueuedOutcome::Queued {
            format!(
                "#{}: refine-chat queued — will start after current command.",
                num
            )
        } else {
            format!("#{}: starting refinement chat…", num)
        };
        self.push_toast("Refine with chat", &msg, ToastSeverity::Info);
        true
    }

    /// #264: called each tick while `pending_refinement` is armed.  Looks
    /// for the freshly-dispatched `type="refinement"` row in
    /// `self.data.assignments` matching the pending issue; on hit, adds it
    /// to `watch_pool`, focuses it, and opens the inject_chat overlay.
    /// Returns true when the overlay was opened (caller redraws).
    pub(crate) fn maybe_bind_pending_refinement(&mut self) -> bool {
        let pending = match &self.pending_refinement {
            Some(p) => p.clone(),
            None => return false,
        };
        // Timeout — drop and toast.  Common cause: `coord refine-chat`
        // failed (the command's stderr-to-toast plumbing landed in
        // `bead06a`, so the user already saw the underlying error).
        if pending.dispatched_at.elapsed() > REFINEMENT_BIND_TIMEOUT {
            self.pending_refinement = None;
            self.push_toast(
                "Refine with chat timed out",
                &format!(
                    "No refinement assignment appeared for #{} within {}s.",
                    pending.issue_number,
                    REFINEMENT_BIND_TIMEOUT.as_secs()
                ),
                ToastSeverity::Warning,
            );
            return true;
        }
        // Find the matching assignment.  Prefer the newest dispatched
        // one in case there were prior aborted attempts on the same issue.
        let pick = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == pending.issue_number)
            .filter(|a| a.assignment_type.as_deref() == Some("refinement"))
            .filter(|a| a.status == "running")
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .cloned();
        let Some(asg) = pick else {
            return false;
        };

        // Add to watch_pool (if not already), focus it, open chat.  Reuses
        // the worker-guidance overlay shape — refinement is a chat too.
        let aid = asg.id.clone();
        if !self.watch_pool.contains_key(&aid) {
            let state = WatchState {
                assignment_id: aid.clone(),
                machine: asg.machine.clone(),
                repo: asg.repo.clone(),
                issue_number: asg.issue_number,
                assignment_type: asg
                    .assignment_type
                    .clone()
                    .unwrap_or_else(|| "refinement".to_string()),
                scroll: usize::MAX,
            };
            let sse = if let Some(m) = self.data.machines.iter().find(|m| m.name == asg.machine) {
                if !m.host.is_empty() {
                    let rx = spawn_sse_watch(&m.host, &aid, 0);
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
                    make_local_sse_state(&aid)
                }
            } else {
                make_local_sse_state(&aid)
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
        }
        self.watch_focused = Some(aid.clone());
        // Open the inject_chat overlay so the user can type immediately.
        let mut chat = ChatController::new("refinement-chat");
        chat.set_status(StyledText::plain(format!(
            "  Refinement chat → {} #{}  (Ctrl+S/Alt+Enter = send · Ctrl+N = post notes · Esc = finish)",
            pending.repo, pending.issue_number
        )));
        chat.set_transcript(Vec::new());
        self.inject_chat = Some(chat);
        // #264/#818: route the user to the Pipeline view.  The Refinement
        // tab was removed in #818, so we land on Overview instead.  The
        // refinement chat backend continues to run; its output appears in
        // the log (coord assign --interactive launched by the board CTA).
        // #1029 bug A: keep the ActivityBar/header chrome in sync too.
        self.switch_active_view(SidebarView::Pipeline);
        self.pipeline_detail_tab = PipelineDetailTab::Overview;
        if let Some(idx) = self
            .pipeline_issues
            .iter()
            .position(|i| i.number == pending.issue_number)
        {
            self.pipeline_sel = Some(idx);
        }
        self.pending_refinement = None;
        self.push_toast(
            "Refine with chat",
            &format!("#{}: chat ready — type to refine.", pending.issue_number),
            ToastSeverity::Info,
        );
        true
    }

    // ── #316 Board Chat dispatch + bind ──────────────────────────────────────

    /// #316 Phase C: shell `coord refine-board <repo>` and arm
    /// `pending_board_chat` so the next tick can bind the chat overlay.
    pub(crate) fn dispatch_board_chat_refine(&mut self, repo: &str) -> bool {
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["refine-board", repo]);
        if outcome == SpawnQueuedOutcome::Deduped {
            // Already running or queued — don't overwrite pending state or re-toast.
            return false;
        }
        self.pending_board_chat = Some(PendingBoardChat {
            repo: repo.to_string(),
            assignment_type: "refinement".to_string(),
            dispatched_at: Instant::now(),
        });
        let msg = if outcome == SpawnQueuedOutcome::Queued {
            format!("Board refinement chat queued for {}…", repo)
        } else {
            format!("Starting board-level refinement chat for {}…", repo)
        };
        self.push_toast("Board refinement chat", &msg, ToastSeverity::Info);
        true
    }

    /// #316 Phase A: shell `coord new-issue-chat <repo>` and arm
    /// `pending_board_chat` so the next tick can bind the chat overlay.
    pub(crate) fn dispatch_board_chat_new_issue(&mut self, repo: &str) -> bool {
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["new-issue-chat", repo]);
        if outcome == SpawnQueuedOutcome::Deduped {
            // Already running or queued — don't overwrite pending state or re-toast.
            return false;
        }
        self.pending_board_chat = Some(PendingBoardChat {
            repo: repo.to_string(),
            assignment_type: "new-issue-chat".to_string(),
            dispatched_at: Instant::now(),
        });
        let msg = if outcome == SpawnQueuedOutcome::Queued {
            format!("New-issue chat queued for {}…", repo)
        } else {
            format!("Starting new-issue chat for {}…", repo)
        };
        self.push_toast("New issue chat", &msg, ToastSeverity::Info);
        true
    }

    /// #316 Phase A: called each tick while `pending_board_chat` is armed.
    /// Looks for the freshly-dispatched board-chat assignment in
    /// `self.data.assignments` (matching by repo + assignment_type +
    /// `issue_number == 0`); on hit, adds it to `watch_pool`, focuses it,
    /// and opens the inject_chat overlay in the Board Chat tab.
    /// Returns true when the overlay was opened (caller redraws).
    pub(crate) fn maybe_bind_pending_board_chat(&mut self) -> bool {
        let pending = match &self.pending_board_chat {
            Some(p) => p.clone(),
            None => return false,
        };
        if pending.dispatched_at.elapsed() > REFINEMENT_BIND_TIMEOUT {
            self.pending_board_chat = None;
            self.push_toast(
                "Board chat timed out",
                &format!(
                    "No board-chat assignment appeared for {} within {}s.",
                    pending.repo,
                    REFINEMENT_BIND_TIMEOUT.as_secs(),
                ),
                ToastSeverity::Warning,
            );
            return true;
        }
        let pick = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == 0)
            .filter(|a| a.repo == pending.repo)
            .filter(|a| a.assignment_type.as_deref() == Some(&*pending.assignment_type))
            .filter(|a| a.status == "running")
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .cloned();
        let Some(asg) = pick else {
            return false;
        };

        let aid = asg.id.clone();
        if !self.watch_pool.contains_key(&aid) {
            let state = WatchState {
                assignment_id: aid.clone(),
                machine: asg.machine.clone(),
                repo: asg.repo.clone(),
                issue_number: 0,
                assignment_type: asg
                    .assignment_type
                    .clone()
                    .unwrap_or_else(|| pending.assignment_type.clone()),
                scroll: usize::MAX,
            };
            let sse = if let Some(m) = self.data.machines.iter().find(|m| m.name == asg.machine) {
                if !m.host.is_empty() {
                    let rx = spawn_sse_watch(&m.host, &aid, 0);
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
                    make_local_sse_state(&aid)
                }
            } else {
                make_local_sse_state(&aid)
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
        }
        self.watch_focused = Some(aid.clone());

        let is_new_issue = pending.assignment_type == "new-issue-chat";
        let status_hint = if is_new_issue {
            format!(
                "  New issue chat → {}  (Ctrl+S/Alt+Enter = send · Ctrl+F = file issue · Esc = close)",
                pending.repo
            )
        } else {
            format!(
                "  Board refinement → {}  (Ctrl+S/Alt+Enter = send · Esc = close)",
                pending.repo
            )
        };

        let mut chat = ChatController::new("board-chat");
        chat.set_status(StyledText::plain(status_hint));
        chat.set_transcript(Vec::new());
        self.inject_chat = Some(chat);

        // Route to Board Chat tab immediately.
        // #1029 bug A: keep the ActivityBar/header chrome in sync too.
        self.switch_active_view(SidebarView::Board);
        self.board_detail_tab = BoardDetailTab::Chat;

        self.pending_board_chat = None;
        let label = if is_new_issue {
            "New issue chat"
        } else {
            "Board refinement"
        };
        self.push_toast(
            label,
            &format!("{}: chat ready — type to start.", pending.repo),
            ToastSeverity::Info,
        );
        true
    }

    /// #1017: called each tick while `pending_milestone_chat` is armed.
    /// Looks for the freshly-dispatched `type="milestone-chat"` assignment in
    /// `self.data.assignments` (matching by repo + tracking-issue number,
    /// where `0` is the brand-new-milestone sentinel), adds it to
    /// `watch_pool`, focuses it, and opens the inject_chat overlay in the
    /// Board Chat tab.  Mirrors `maybe_bind_pending_board_chat` — a
    /// milestone-chat is the same stream-json `claude -p` session family as
    /// refine-chat / new-issue-chat, so the operator gets a live, attachable
    /// chat pane instead of a fire-and-forget headless worker (the review
    /// finding this fixes).  Returns true when the overlay was opened.
    pub(crate) fn maybe_bind_pending_milestone_chat(&mut self) -> bool {
        let pending = match &self.pending_milestone_chat {
            Some(p) => p.clone(),
            None => return false,
        };
        if pending.dispatched_at.elapsed() > REFINEMENT_BIND_TIMEOUT {
            self.pending_milestone_chat = None;
            self.push_toast(
                "Milestone chat timed out",
                &format!(
                    "No milestone-chat session appeared for {} within {}s.",
                    pending.label,
                    REFINEMENT_BIND_TIMEOUT.as_secs(),
                ),
                ToastSeverity::Warning,
            );
            return true;
        }
        let pick = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == pending.issue_number)
            .filter(|a| a.repo == pending.repo)
            .filter(|a| a.assignment_type.as_deref() == Some("milestone-chat"))
            .filter(|a| a.status == "running")
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .cloned();
        let Some(asg) = pick else {
            return false;
        };

        let aid = asg.id.clone();
        if !self.watch_pool.contains_key(&aid) {
            let state = WatchState {
                assignment_id: aid.clone(),
                machine: asg.machine.clone(),
                repo: asg.repo.clone(),
                issue_number: asg.issue_number,
                assignment_type: asg
                    .assignment_type
                    .clone()
                    .unwrap_or_else(|| "milestone-chat".to_string()),
                scroll: usize::MAX,
            };
            let sse = if let Some(m) = self.data.machines.iter().find(|m| m.name == asg.machine) {
                if !m.host.is_empty() {
                    let rx = spawn_sse_watch(&m.host, &aid, 0);
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
                    make_local_sse_state(&aid)
                }
            } else {
                make_local_sse_state(&aid)
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
        }
        self.watch_focused = Some(aid.clone());

        let mut chat = ChatController::new("milestone-chat");
        chat.set_status(StyledText::plain(format!(
            "  Milestone chat → {}  (Ctrl+S/Alt+Enter = send · Esc = close)",
            pending.label
        )));
        chat.set_transcript(Vec::new());
        self.inject_chat = Some(chat);

        // Route to the Board Chat tab — milestone chats are plan/board-level
        // conversations (`chat_is_board_chat()` returns true for them so the
        // overlay renders inline there rather than modally).
        // #1029 bug A: keep the ActivityBar/header chrome in sync too.
        self.switch_active_view(SidebarView::Board);
        self.board_detail_tab = BoardDetailTab::Chat;

        self.pending_milestone_chat = None;
        self.push_toast(
            "Milestone chat",
            &format!("{}: chat ready — type to start.", pending.label),
            ToastSeverity::Info,
        );
        true
    }

    // ── #2533 Decomposition-chat dispatch + bind (ms-67 contract §4c/§4d) ────

    /// Contract §4a/§4c: shell `coord portal decompose-chat <submission_id>`
    /// and arm `pending_decomposition_chat` so the next tick can bind the
    /// chat overlay once the `type="decomposition-chat"` assignment appears.
    ///
    /// Unlike `dispatch_board_chat_new_issue` / the milestone-chat dispatch
    /// entries, the "chat ready — type to start" toast fires HERE, at
    /// dispatch time, not from the bind function below — contract §4d pins
    /// it as firing "immediately on dispatch... within one tick", ahead of
    /// (and independent from) the later Board/[Chat] redirect that only
    /// happens once the polled assignment actually appears.
    pub(crate) fn dispatch_approved_pull_into_decomposition(&mut self, submission_id: &str) {
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self
            .command_runner
            .spawn_queued(&["portal", "decompose-chat", submission_id]);
        if outcome == SpawnQueuedOutcome::Deduped {
            // Already running or queued for this submission — don't
            // overwrite the pending state or re-toast (same guard the two
            // sibling dispatchers use).
            return;
        }
        self.pending_decomposition_chat = Some(PendingDecompositionChat {
            submission_id: submission_id.to_string(),
            dispatched_at: Instant::now(),
        });
        // #2533 (ms-67 contract §4d): the toast body the contract quotes
        // verbatim — "{submission_id}: chat ready — type to start." — is 38
        // characters wide once a real submission id is substituted in; the
        // TUI `ToastStack`'s box is a *fixed* 40 cols (quadraui's
        // `TUI_TOAST_WIDTH`, sealed — not this crate's to change), and its
        // dismiss button + 1-cell padding always reserve 4 of those,
        // leaving exactly 36 usable — 2 short. One toast physically cannot
        // show the id-prefixed sentence AND its own unclipped tail at once.
        // Two toasts split the content instead of clipping it: the first is
        // the contract-quoted sentence verbatim (its tail may clip for a
        // long id, same as any fixed-width toast); the second restates just
        // the "chat ready" clause in full, guaranteed to fit (27 of 36
        // cols) — so the operator can always read the complete phrase
        // somewhere, never a mid-word cutoff.
        self.push_toast(
            "Decomposition chat",
            &format!("{submission_id}: chat ready — type to start."),
            ToastSeverity::Info,
        );
        self.push_toast(
            "Decomposition chat",
            "chat ready — type to start.",
            ToastSeverity::Info,
        );
    }

    /// #2863: "Open attended intake session…" — the `--interactive` (#2750)
    /// counterpart of the headless dispatch above.
    ///
    /// `coord portal decompose-chat <id> --interactive` takes over a TTY (it
    /// tmux-attaches a real `claude` on this machine), so it can NOT go
    /// through `command_runner.spawn_queued` like its headless sibling — a
    /// non-TTY subprocess would strand the conversation. It is typed into
    /// the embedded terminal instead, exactly the way
    /// [`Self::launch_milestone_chat_session`] (#1029) launches the closest
    /// analogue in this crate: bookmark the origin view, switch to
    /// Terminal, feed the launch line as literal keystrokes, let the
    /// terminal itself be the truth.
    ///
    /// No TUI-side reattach branch: the CLI already checks
    /// `tmux_session_running` for the submission's session and attaches to
    /// the live one rather than starting a duplicate
    /// (`coord/commands/portal.py`). Duplicating that decision here could
    /// only ever disagree with it.
    pub(crate) fn launch_attended_intake_session(&mut self, submission_id: &str) {
        // #1029 bug B's bookmark rule: capture the origin *before* the view
        // switch (which clears `terminal_return_view`) and re-apply it
        // *after*, so Esc returns the operator where they came from.
        let bookmark_origin =
            (self.active_view != SidebarView::Terminal).then_some(self.active_view);

        let cfg_path = self
            .command_runner
            .config_path
            .as_ref()
            .map(|p| p.to_string_lossy().into_owned());
        let launch_line = build_attended_intake_launch_cmd(cfg_path.as_deref(), submission_id);

        // #955: a selected Terminal-tree leaf takes over the main pane —
        // clear the selection so the bare-shell pane this writes into is
        // actually visible.
        self.terminal_tree_selected = None;
        self.switch_active_view(SidebarView::Terminal);
        self.terminal_return_view = bookmark_origin;

        if let Some(ref mut sess) = self.terminal_session {
            sess.send_str(&launch_line);
        } else {
            let cwd = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("/"));
            let shell = quadraui::terminal_engine::default_shell();
            match quadraui::terminal_engine::TerminalSession::spawn(80, 24, &shell, &cwd, 10_000) {
                Ok(mut sess) => {
                    sess.send_str(&launch_line);
                    self.terminal_session = Some(sess);
                    self.terminal_spawn_error = None;
                }
                Err(e) => {
                    self.terminal_spawn_error = Some(e.to_string());
                    self.push_toast(
                        "Intake session",
                        &format!("Failed to spawn terminal: {}", e),
                        ToastSeverity::Error,
                    );
                    return;
                }
            }
        }
        self.terminal_focused = true;
        self.push_toast(
            "Intake session",
            &format!("Launching attended intake session for {submission_id} …"),
            ToastSeverity::Info,
        );
    }

    /// #2863: a `coord portal decompose-chat <id>` dispatch came back
    /// non-zero — collapse the three contradictory notifications the
    /// operator used to get (optimistic "chat ready", a 40-col-truncated
    /// "Command failed", then a 30 s "timed out") into ONE coherent error
    /// surface.
    ///
    /// Three things happen together, and they only make sense together:
    ///
    /// 1. **Retract the optimistic toasts.** Contract §4d pins the "chat
    ///    ready — type to start" toast as firing *immediately on dispatch*,
    ///    before the exit code exists — that stays (the sealed slice only
    ///    ever exercises the success path, since `CommandRunner`'s
    ///    `no_spawn` branch synthesizes exit 0). But once we KNOW it was
    ///    wrong, leaving it on screen contradicting an error toast beside it
    ///    is worse than either alone, so both "Decomposition chat" toasts
    ///    are dropped here.
    /// 2. **Disarm `pending_decomposition_chat`,** which suppresses the
    ///    `REFINEMENT_BIND_TIMEOUT` "Decomposition chat timed out" toast 30 s
    ///    later. No assignment is ever going to appear for a dispatch that
    ///    failed; that toast would only restate the same failure in a way
    ///    that reads like a *second*, different problem.
    /// 3. **Raise the full-text modal,** #1059's Gate-A precedent. The
    ///    thin-client refusal this fires on most often is multi-sentence and
    ///    names its own fix; at ~40 toast cols it truncates to something
    ///    like `Error: coord portal decompose-chat mus…`, which is
    ///    actionable to nobody.
    ///
    /// The generic "Command failed" toast is suppressed by the caller
    /// (`settings_ui.rs`) for the same reason it is for Gate A — this is the
    /// one and only notification.
    pub(crate) fn fail_pending_decomposition_chat(&mut self, submission_id: &str, reason: &str) {
        // Scoped to THIS submission's pair of §4d toasts: the id-prefixed
        // sentence, and the id-less restatement the dispatcher pushes
        // alongside it so the phrase always fits the 40-col box. Anything
        // else titled "Decomposition chat" belongs to a different
        // submission and is not ours to retract.
        self.toasts.retain(|(item, _, _)| {
            item.title != "Decomposition chat"
                || !(item.body.contains(submission_id)
                    || item.body == "chat ready — type to start.")
        });
        self.pending_decomposition_chat = None;
        self.decompose_chat_error_dialog = Some(format!(
            "Dispatching a decomposition session for {submission_id} failed:\n\n{reason}"
        ));
    }

    /// Called each tick while `pending_decomposition_chat` is armed. Looks
    /// for the freshly-dispatched `type="decomposition-chat"` assignment in
    /// `self.data.assignments`. Contract §4c leaves the exact match key
    /// unpinned (no `submission_id` column exists on `Assignment`), so this
    /// module's own choice — matched nowhere else, safe to revisit — is
    /// `issue_title == "decomposition: {submission_id}"`, the sentinel
    /// `dispatch_new_issue_chat`'s "(new issue draft)" convention already
    /// established for a chat type with no real issue number yet
    /// (`coord/decomposition_chat.py`'s dispatcher sets it). On hit, adds it
    /// to `watch_pool`, focuses it, and — mirroring
    /// `maybe_bind_pending_milestone_chat` — routes to the Board panel's
    /// Chat sub-tab with an empty-transcript `ChatController` overlay. No
    /// second toast: contract §4d's "chat ready" toast already fired at
    /// dispatch time above. Returns true when the overlay was opened.
    pub(crate) fn maybe_bind_pending_decomposition_chat(&mut self) -> bool {
        let pending = match &self.pending_decomposition_chat {
            Some(p) => p.clone(),
            None => return false,
        };
        if pending.dispatched_at.elapsed() > REFINEMENT_BIND_TIMEOUT {
            self.pending_decomposition_chat = None;
            self.push_toast(
                "Decomposition chat timed out",
                &format!(
                    "No decomposition-chat assignment appeared for {} within {}s.",
                    pending.submission_id,
                    REFINEMENT_BIND_TIMEOUT.as_secs(),
                ),
                ToastSeverity::Warning,
            );
            return true;
        }
        let want_title = format!("decomposition: {}", pending.submission_id);
        let pick = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_title == want_title)
            .filter(|a| a.assignment_type.as_deref() == Some("decomposition-chat"))
            .filter(|a| a.status == "running")
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .cloned();
        let Some(asg) = pick else {
            return false;
        };

        let aid = asg.id.clone();
        if !self.watch_pool.contains_key(&aid) {
            let state = WatchState {
                assignment_id: aid.clone(),
                machine: asg.machine.clone(),
                repo: asg.repo.clone(),
                issue_number: 0,
                assignment_type: asg
                    .assignment_type
                    .clone()
                    .unwrap_or_else(|| "decomposition-chat".to_string()),
                scroll: usize::MAX,
            };
            let sse = if let Some(m) = self.data.machines.iter().find(|m| m.name == asg.machine) {
                if !m.host.is_empty() {
                    let rx = spawn_sse_watch(&m.host, &aid, 0);
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
                    make_local_sse_state(&aid)
                }
            } else {
                make_local_sse_state(&aid)
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
        }
        self.watch_focused = Some(aid.clone());

        let mut chat = ChatController::new("decomposition-chat");
        chat.set_status(StyledText::plain(format!(
            "  Decomposition chat → {}  (Ctrl+S/Alt+Enter = send · Esc = close)",
            pending.submission_id
        )));
        chat.set_transcript(Vec::new());
        self.inject_chat = Some(chat);

        // Route to the Board Chat tab — same redirect every sibling chat
        // family performs regardless of which panel dispatched it, since
        // `inject_chat` is one shared overlay slot (contract §4d).
        self.switch_active_view(SidebarView::Board);
        self.board_detail_tab = BoardDetailTab::Chat;

        self.pending_decomposition_chat = None;
        true
    }

    /// #314 Phase B: shell `coord test-chat <work_assignment_id>` and arm
    /// `pending_test_chat` so the next tick can bind the chat overlay to the
    /// new assignment row when it appears in the DB.
    ///
    /// Returns true when the dispatch was successfully initiated.
    pub(crate) fn spawn_test_chat(&mut self) -> bool {
        let Some(work_id) = self.pipeline_selected_work_id() else {
            self.push_toast(
                "Test chat unavailable",
                "No work assignment found — dispatch Work first.",
                ToastSeverity::Info,
            );
            return false;
        };
        // Resolve the issue number and repo for the pending state.
        let (issue_number, repo) = self
            .data
            .assignments
            .iter()
            .find(|a| a.id == work_id)
            .map(|a| (a.issue_number, a.repo.clone()))
            .unwrap_or((0, String::new()));

        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["test-chat", &work_id]);
        if outcome == SpawnQueuedOutcome::Deduped {
            return false;
        }
        // Arm immediately — the 30 s bind window covers any realistic queue wait.
        self.pending_test_chat = Some(PendingTestChat {
            repo: repo.clone(),
            work_assignment_id: work_id.clone(),
            issue_number,
            dispatched_at: Instant::now(),
        });
        let msg = if outcome == SpawnQueuedOutcome::Queued {
            format!(
                "#{}: test-chat queued — will start after current command.",
                issue_number
            )
        } else {
            format!("#{}: starting test chat…", issue_number)
        };
        self.push_toast("Test chat", &msg, ToastSeverity::Info);
        true
    }

    /// #314 Phase B: called each tick while `pending_test_chat` is armed.
    /// Looks for the freshly-dispatched `type="test-chat"` row in
    /// `self.data.assignments` matching the pending issue; on hit, adds it
    /// to `watch_pool`, focuses it, and opens the inject_chat overlay.
    /// Returns true when the overlay was opened (caller redraws).
    pub(crate) fn maybe_bind_pending_test_chat(&mut self) -> bool {
        let pending = match &self.pending_test_chat {
            Some(p) => p.clone(),
            None => return false,
        };
        // Timeout — drop and toast.
        if pending.dispatched_at.elapsed() > REFINEMENT_BIND_TIMEOUT {
            self.pending_test_chat = None;
            self.push_toast(
                "Test chat timed out",
                &format!(
                    "No test-chat assignment appeared for #{} within {}s.",
                    pending.issue_number,
                    REFINEMENT_BIND_TIMEOUT.as_secs()
                ),
                ToastSeverity::Warning,
            );
            return true;
        }
        // Find the matching assignment — newest running test-chat for this issue.
        let pick = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == pending.issue_number)
            .filter(|a| a.assignment_type.as_deref() == Some("test-chat"))
            .filter(|a| a.status == "running")
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .cloned();
        let Some(asg) = pick else {
            return false;
        };

        let aid = asg.id.clone();
        if !self.watch_pool.contains_key(&aid) {
            let state = WatchState {
                assignment_id: aid.clone(),
                machine: asg.machine.clone(),
                repo: asg.repo.clone(),
                issue_number: asg.issue_number,
                assignment_type: asg
                    .assignment_type
                    .clone()
                    .unwrap_or_else(|| "test-chat".to_string()),
                scroll: usize::MAX,
            };
            let sse = if let Some(m) = self.data.machines.iter().find(|m| m.name == asg.machine) {
                if !m.host.is_empty() {
                    let rx = spawn_sse_watch(&m.host, &aid, 0);
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
                    make_local_sse_state(&aid)
                }
            } else {
                make_local_sse_state(&aid)
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
        }
        self.watch_focused = Some(aid.clone());
        // Open the inject_chat overlay.
        let mut chat = ChatController::new("test-chat");
        chat.set_status(StyledText::plain(format!(
            "  Test chat → {} #{}  (Ctrl+S/Alt+Enter = send · Esc = close)",
            pending.repo, pending.issue_number
        )));
        chat.set_transcript(Vec::new());
        self.inject_chat = Some(chat);
        // #818: Refinement tab removed; land on Overview instead.
        // #1029 bug A: keep the ActivityBar/header chrome in sync too.
        self.switch_active_view(SidebarView::Pipeline);
        self.pipeline_detail_tab = PipelineDetailTab::Overview;
        if let Some(idx) = self
            .pipeline_issues
            .iter()
            .position(|i| i.number == pending.issue_number)
        {
            self.pipeline_sel = Some(idx);
        }
        self.pending_test_chat = None;
        self.push_toast(
            "Test chat",
            &format!(
                "#{}: chat ready (work: {}) — type to ask about the change.",
                pending.issue_number,
                &pending.work_assignment_id[..pending.work_assignment_id.len().min(8)],
            ),
            ToastSeverity::Info,
        );
        true
    }

    // ── #316 Phase B: file-issue finaliser ───────────────────────────────────

    /// #316 Phase B: scan the focused chat transcript for a `TITLE: …` line
    /// followed by a `---` separator.  Returns `(title, body)` on success.
    pub(crate) fn detect_file_issue_proposal(&self) -> Option<(String, String)> {
        let id = self.watch_focused.as_ref()?;
        let ctx = self.watch_pool.get(id)?;
        // Only applies to new-issue-chat sessions.
        if ctx.state.assignment_type != "new-issue-chat" {
            return None;
        }
        // Scan lines for the TITLE: / --- / body format.
        // The worker emits: TITLE: <title>\n---\n<body>
        let text = ctx.sse.lines.join("\n");
        parse_issue_proposal(&text)
    }

    /// #316 Phase B: open the file-issue modal after the user requests to
    /// file the drafted issue.  Scans the transcript for TITLE: format;
    /// if found, opens the edit modal.  Shows a toast if no proposal found.
    pub(crate) fn open_file_issue_modal(&mut self) {
        let Some((title, body)) = self.detect_file_issue_proposal() else {
            self.push_toast(
                "No issue proposal found",
                "The chat hasn't produced a TITLE: / --- / body block yet.",
                ToastSeverity::Info,
            );
            return;
        };
        let repo_github = self
            .watch_focused
            .as_ref()
            .and_then(|id| self.watch_pool.get(id))
            .map(|ctx| {
                let repo = ctx.state.repo.clone();
                // Look up the GitHub slug from the pipeline_repos map.
                self.data
                    .pipeline_repos
                    .iter()
                    .find(|(name, _)| *name == repo)
                    .map(|(_, slug)| slug.clone())
                    .unwrap_or(repo)
            })
            .unwrap_or_default();
        self.file_issue_modal = Some(FileIssueModal {
            title,
            body,
            repo_github,
            submitting: false,
        });
    }

    /// #316 Phase B: shell `gh issue create` with the modal's title + body.
    pub(crate) fn submit_file_issue(&mut self) {
        let modal = match &mut self.file_issue_modal {
            Some(m) => m,
            None => return,
        };
        if modal.submitting {
            return;
        }
        modal.submitting = true;

        let title = modal.title.clone();
        let body = modal.body.clone();
        let repo_github = modal.repo_github.clone();
        let (tx, rx) = std::sync::mpsc::channel();
        self.file_issue_post_rx = Some(rx);

        std::thread::spawn(move || {
            let mut cmd = std::process::Command::new("gh");
            cmd.args([
                "issue",
                "create",
                "--repo",
                &repo_github,
                "--title",
                &title,
                "--body",
                &body,
            ]);
            match cmd.output() {
                Ok(out) => {
                    let success = out.status.success();
                    let url = String::from_utf8_lossy(&out.stdout).trim().to_string();
                    let stderr = String::from_utf8_lossy(&out.stderr);
                    let first_err = stderr
                        .lines()
                        .find(|l| !l.is_empty())
                        .unwrap_or("")
                        .to_string();
                    let _ = tx.send(FileIssuePostResult {
                        success,
                        issue_url: url,
                        stderr_first_line: first_err,
                    });
                }
                Err(e) => {
                    let _ = tx.send(FileIssuePostResult {
                        success: false,
                        issue_url: String::new(),
                        stderr_first_line: e.to_string(),
                    });
                }
            }
        });
    }

    /// #316 Phase B: drain the file-issue post receiver.  Called each tick.
    pub(crate) fn poll_file_issue_post(&mut self) -> bool {
        let Some(rx) = &self.file_issue_post_rx else {
            return false;
        };
        match rx.try_recv() {
            Ok(result) => {
                self.file_issue_post_rx = None;
                if let Some(m) = &mut self.file_issue_modal {
                    m.submitting = false;
                }
                if result.success {
                    let url = result.issue_url.clone();
                    self.file_issue_modal = None;
                    self.inject_chat = None;
                    self.watch_focused = None;
                    self.push_toast(
                        "Issue filed",
                        &if url.is_empty() {
                            "Issue created successfully.".to_string()
                        } else {
                            format!("Issue created: {}", url)
                        },
                        ToastSeverity::Info,
                    );
                } else {
                    self.push_toast(
                        "Failed to file issue",
                        &if result.stderr_first_line.is_empty() {
                            "gh issue create failed — check gh auth status.".to_string()
                        } else {
                            result.stderr_first_line.clone()
                        },
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => false,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                self.file_issue_post_rx = None;
                if let Some(m) = &mut self.file_issue_modal {
                    m.submitting = false;
                }
                false
            }
        }
    }

    /// #316 Phase B: render the file-issue edit modal as an overlay over the
    /// Board Chat tab content area.
    pub(crate) fn render_file_issue_modal(&self, backend: &mut dyn Backend, content_rect: Rect) {
        let Some(modal) = &self.file_issue_modal else {
            return;
        };
        let lh = backend.line_height();

        // Modal background (reuse the refinement-notes-modal pattern).
        let modal_rect = shrink_rect(content_rect, lh * 2.0);
        backend.draw_list(
            modal_rect,
            &ListView {
                id: WidgetId::new("file-issue-modal-bg"),
                title: None,
                items: Vec::new(),
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: true,
                bordered: true,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
            },
        );

        let inner = shrink_rect(modal_rect, lh * 0.5);
        let mut items: Vec<ListItem> = Vec::new();

        items.push(kv_item(
            "",
            "  File New GitHub Issue",
            Some(Color::rgb(100, 200, 255)),
        ));
        items.push(kv_item("", "", None));
        items.push(kv_item(
            "",
            &format!("  Repo: {}", modal.repo_github),
            Some(Color::rgb(140, 180, 140)),
        ));
        items.push(kv_item("", "", None));
        items.push(kv_item("", "  TITLE ▸", Some(Color::rgb(200, 200, 100))));
        items.push(kv_item("", &format!("  {}", modal.title), None));
        items.push(kv_item("", "", None));
        items.push(kv_item("", "  BODY ▸", Some(Color::rgb(200, 200, 100))));
        // Show first 6 lines of body.
        for line in modal.body.lines().take(6) {
            items.push(kv_item("", &format!("  {}", line), None));
        }
        if modal.body.lines().count() > 6 {
            items.push(kv_item("", "  …", Some(Color::rgb(140, 140, 160))));
        }
        items.push(kv_item("", "", None));
        if modal.submitting {
            items.push(kv_item("", "  Submitting…", Some(Color::rgb(200, 180, 60))));
        } else {
            items.push(kv_item(
                "",
                "  Ctrl+Y — file issue  ·  Esc — cancel",
                Some(Color::rgb(140, 140, 160)),
            ));
        }

        backend.draw_list(
            inner,
            &ListView {
                id: WidgetId::new("file-issue-modal"),
                title: None,
                items,
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: true,
                bordered: false,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
            },
        );
    }

    // ── #541: global issue fuzzy finder ──────────────────────────────────────

    /// Compute a score-sorted match list for the current finder query.
    ///
    /// Searches all `data.open_issues` (both `open` and `closed` state — the
    /// backlog contains historical issues too, and the finder is a navigation
    /// tool, not a work-queue filter).  Matches are scored by [`fuzzy_score`]
    /// against the combined string `"#N title"` so typing a bare number or a
    /// word fragment from the title both work naturally.
    ///
    /// Returns at most 50 results (enough to cover any reasonable viewport
    /// without unbounded allocation on large backlogs).
    pub(crate) fn finder_matches(&self, query: &str) -> Vec<(String, u64, String)> {
        const MAX_RESULTS: usize = 50;
        let mut scored: Vec<(u32, String, u64, String)> = self
            .data
            .open_issues
            .iter()
            .filter_map(|oi| {
                let haystack = format!("#{} {}", oi.number, oi.title);
                let (score, _) = fuzzy_score(query, &haystack)?;
                Some((score, oi.repo_name.clone(), oi.number, oi.title.clone()))
            })
            .collect();
        // Higher score first; tie-break by repo then number for stable ordering.
        scored.sort_by(|a, b| {
            b.0.cmp(&a.0)
                .then(a.1.cmp(&b.1))
                .then(a.2.cmp(&b.2))
        });
        scored
            .into_iter()
            .take(MAX_RESULTS)
            .map(|(_, repo, num, title)| (repo, num, title))
            .collect()
    }

    /// Called when the user presses Enter in the issue finder.
    ///
    /// Navigates to the selected issue:
    /// * If the issue appears in `pipeline_issues` (i.e. it carries a tracked
    ///   label), switch to the Pipeline view and set the selection there.
    /// * Otherwise switch to the Board view and call the existing
    ///   `select_issue()` helper.
    ///
    /// Clears `issue_finder` in all cases.
    pub(crate) fn confirm_issue_finder(&mut self) {
        let query = match &self.issue_finder {
            Some(f) => f.query.clone(),
            None => return,
        };
        let sel = self.issue_finder.as_ref().map(|f| f.selected_idx).unwrap_or(0);
        let matches = self.finder_matches(&query);
        let Some((repo, number, _title)) = matches.into_iter().nth(sel) else {
            self.issue_finder = None;
            return;
        };
        self.issue_finder = None;

        // Navigate to Pipeline if the issue appears there (tracked label).
        // Match by coord_repo (local name) since `finder_matches` uses oi.repo_name.
        let pipeline_entry = self.pipeline_issues.iter().find(|pi| {
            pi.number == number
                && pi.coord_repo.as_deref().unwrap_or(&pi.repo_slug) == repo
        });
        if let Some(pi) = pipeline_entry {
            let repo_slug = pi.repo_slug.clone();
            // #1029 bug A: keep the ActivityBar/header chrome in sync too.
            self.switch_active_view(SidebarView::Pipeline);
            // `rebuild_pipeline_sidebar` with prev_sel_override wires up the
            // sidebar selection highlight and syncs `pipeline_sel` for us.
            self.rebuild_pipeline_sidebar(Some((repo_slug, number)));
            self.pipeline_focused_stage =
                self.default_focused_stage_for_selected_issue();
            self.pipeline_stage_content_scroll = 0;
        } else {
            // Fall back to the Board view.
            self.switch_active_view(SidebarView::Board);
            self.select_issue(&repo, number);
            self.detail_scroll = 0;
        }
    }

    /// #815: Jump from the Board to the Pipeline for the currently-selected
    /// issue.  Thin wrapper around [`Self::jump_to_pipeline`] — see that
    /// method for the reveal-and-select behaviour shared with #2016's Queue
    /// panel "View in Pipeline" entry.
    pub(crate) fn jump_board_to_pipeline(&mut self) {
        let Some((repo_name, issue_number)) = self.board_selected_issue() else {
            return;
        };
        self.jump_to_pipeline(&repo_name, issue_number);
    }

    /// #2016: shared open-or-activate body for "View in Pipeline", called by
    /// both the Board row jump (`jump_board_to_pipeline`, #815) and the
    /// Queue row jump (`drive-queue-view-in-pipeline`) — #1069 already
    /// showed how this logic drifts when duplicated, so there is exactly
    /// one copy.
    ///
    /// If `repo`/`number` resolves to a real Pipeline issue (per
    /// [`Self::pipeline_jump_target`] — #1598: the same query the
    /// right-click menu's enablement check uses), the Pipeline view is
    /// activated and a pinned document tab for the issue is opened (or
    /// activated, if one is already open) — #2449. Otherwise a toast names
    /// the specific reason ([`PipelineNotVisible`]) and the caller's current
    /// view stays active.
    pub(crate) fn jump_to_pipeline(&mut self, repo_name: &str, issue_number: u64) {
        // Kick the pipeline loader so data is current — in particular, if the
        // user has never visited the Pipeline view this session, pipeline_issues
        // is empty and the lookup below would always miss (false negative).
        self.maybe_kick_pipeline_loader();
        // Match by coord_repo (the local repo name from coordinator.yml) just
        // like `confirm_issue_finder` does — `board_active_repo` returns the
        // local name, not the GitHub owner/name slug.
        match self.pipeline_jump_target(repo_name, issue_number) {
            Ok(idx) => {
                let repo_slug = self.pipeline_issues[idx].repo_slug.clone();
                // #1029 bug A: keep the ActivityBar/header chrome in sync too.
                self.switch_active_view(SidebarView::Pipeline);
                // #2449: open-or-activate a *pinned* document tab for the
                // target issue — `pin: true`, not `open_preview`, so a
                // right-click "View in Pipeline" always lands its own tab
                // rather than clobbering a different issue's preview tab in
                // place. `open_pipeline_doc_tab` already does the
                // milestone/repo-group-expand + `rebuild_pipeline_sidebar`
                // reveal internally (`reveal_pipeline_active_doc`), including
                // forcing `pipeline_sel` onto this issue even when it's
                // hidden by the current Pipeline search filter (so the
                // Overview/Issue/Log detail panes show the target's content,
                // not a stale or wrong-issue one) — so there is nothing left
                // to hand-roll here.
                self.open_pipeline_doc_tab((repo_slug, issue_number), true);
            }
            Err(reason) => {
                self.push_toast(
                    "Not in Pipeline",
                    &reason.toast_body(issue_number),
                    ToastSeverity::Info,
                );
            }
        }
    }

    /// #2454: shared open-or-activate body for "View on Board" — the exact
    /// mirror image of [`Self::jump_to_pipeline`], and for the same reason:
    /// there is now more than one caller (#2016's Queue row and this issue's
    /// Reports result row), and #1069 already showed how this logic drifts
    /// when duplicated.
    ///
    /// **Why a pinned document tab and not just `select_issue`.** #2016
    /// shipped this as a plain focus change because that was all the Board
    /// had; #2282/#2285 then gave the Board its own document tabs, and #2449
    /// made the symmetric "View in Pipeline" jump open-or-activate a *pinned*
    /// Pipeline tab. Leaving this one a bare selection change would mean the
    /// two navigation items in the very same menu behave differently — and,
    /// worse, that jumping here silently clobbers whatever preview tab the
    /// operator was reading. `pin: true` (not `open_preview`) is what makes
    /// the jump land its own tab.
    ///
    /// `open_board_doc_tab` subsumes the old body rather than adding to it:
    /// its `reveal_board_active_doc` already calls `select_issue`, and its
    /// `restore_detail_sub_state` already zeroes `detail_scroll` for a
    /// first-open document (#2285 §5) — which is what the hand-rolled
    /// `detail_scroll = 0` used to approximate.
    ///
    /// Falls back to that pre-#2282 body when `(repo, issue)` names nothing
    /// on the board: pinning a tab for a document that cannot render would
    /// leave a dead tab the operator has to close by hand, where
    /// `select_issue`'s existing no-op-if-not-loaded behaviour is silent and
    /// harmless (#2016's own stated call).
    pub(crate) fn jump_to_board(&mut self, repo_name: &str, issue_number: u64) {
        self.switch_active_view(SidebarView::Board);
        // The Board sidebar caches (`board_issues_cache` / `board_repo_names`)
        // are only built by a rebuild, and a session that has never *visited*
        // the Board has never had one — without this the lookup below (and
        // `select_issue` behind it) would always miss. A pure re-derivation
        // from `self.data`, so calling it here is free of side effects beyond
        // the ones a Board keystroke already triggers.
        self.rebuild_board_sidebar();
        if self.board_issue_group_for(repo_name, issue_number).is_some() {
            self.open_board_doc_tab((repo_name.to_string(), issue_number), true);
        } else {
            self.select_issue(repo_name, issue_number);
            self.detail_scroll = 0;
        }
    }

    /// Render the Telescope-style issue finder overlay.
    ///
    /// Draws a centered box (~70 % wide, ~60 % tall) over `viewport` that shows:
    /// * A search input line at the top (the typed query with a blinking cursor).
    /// * Up to 15 scrolled result rows, the highlighted one inverted.
    /// * A footer with the match count and key hints.
    pub(crate) fn render_issue_finder(&self, backend: &mut dyn Backend, viewport: Rect) {
        let Some(finder) = &self.issue_finder else {
            return;
        };
        let lh = backend.line_height();

        // ── Size + position ──────────────────────────────────────────────────
        let box_w = (viewport.width * 0.72).clamp(40.0 * lh, 90.0 * lh);
        let box_h = (viewport.height * 0.65).clamp(10.0 * lh, 32.0 * lh);
        let box_x = viewport.x + (viewport.width - box_w) * 0.5;
        let box_y = viewport.y + (viewport.height - box_h) * 0.3;
        let finder_rect = Rect::new(box_x, box_y, box_w, box_h);

        // Outer bordered box (background + border).
        backend.draw_list(
            finder_rect,
            &ListView {
                id: WidgetId::new("issue-finder-bg"),
                title: None,
                items: Vec::new(),
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: true,
                bordered: true,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
            },
        );

        let inner = shrink_rect(finder_rect, lh * 0.5);

        // ── Build items ──────────────────────────────────────────────────────
        let matches = self.finder_matches(&finder.query);
        let match_count = matches.len();

        // Header: query input line.
        // Show a block-cursor glyph (▌) at the byte offset stored in
        // `finder.cursor`.  The cursor is always on a valid UTF-8 char boundary
        // so the byte-slices are safe.
        let cursor_display = {
            let before = &finder.query[..finder.cursor.min(finder.query.len())];
            let after = &finder.query[finder.cursor.min(finder.query.len())..];
            format!("  ▶  {}▌{}", before, after)
        };
        let mut items: Vec<ListItem> = Vec::new();
        items.push(ListItem {
            text: StyledText {
                spans: vec![StyledSpan::with_fg(cursor_display, Color::rgb(120, 220, 120))],
            },
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        });

        // Separator — width tracks the rendered box so it fills the column
        // on both narrow and wide terminals.
        let sep_chars = (box_w / lh) as usize;
        items.push(ListItem {
            text: StyledText {
                spans: vec![StyledSpan::with_fg(
                    "─".repeat(sep_chars),
                    Color::rgb(70, 70, 90),
                )],
            },
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        });

        // ── Result rows ──────────────────────────────────────────────────────
        // Visible window: keep selected_idx in view.
        let visible_rows: usize = ((box_h / lh) as usize).saturating_sub(4).max(1);
        let scroll_offset = if finder.selected_idx >= visible_rows {
            finder.selected_idx + 1 - visible_rows
        } else {
            0
        };

        let visible_matches: Vec<_> = matches
            .iter()
            .enumerate()
            .skip(scroll_offset)
            .take(visible_rows)
            .collect();

        // Per-character match highlight colour (matches theme.rs `match_fg`).
        let match_fg_color = Color::rgb(255, 200, 80);
        if visible_matches.is_empty() {
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        if finder.query.is_empty() {
                            "  (type to search across all issues)".to_string()
                        } else {
                            "  No matching issues".to_string()
                        },
                        Color::rgb(100, 100, 120),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            });
        } else {
            for (abs_idx, (repo, number, title)) in &visible_matches {
                let is_selected = *abs_idx == finder.selected_idx;
                let prefix = if is_selected { "▶ " } else { "  " };
                let repo_short = trunc(repo, 18);
                let title_short = trunc(title, 45);
                let normal_fg = if is_selected {
                    Color::rgb(230, 240, 255)
                } else {
                    Color::rgb(200, 200, 210)
                };
                // Build the row as multiple spans: a fixed prefix (arrow,
                // number, repo) followed by per-character highlighted title.
                // The prefix columns are not in the search haystack (repo)
                // or use a fixed format (#number); only the title carries
                // enough free text for per-char highlights to be useful.
                // `Decoration::Header` is used for the selected row because
                // quadraui's Decoration enum has no `Selected` variant; the
                // Header variant applies the closest available background.
                let prefix_text =
                    format!("{}#{:<5}  {:18}  ", prefix, number, repo_short);
                let mut spans = vec![StyledSpan::with_fg(prefix_text, normal_fg)];
                spans.extend(styled_match_spans(
                    &finder.query,
                    title_short,
                    normal_fg,
                    match_fg_color,
                ));
                items.push(ListItem {
                    text: StyledText { spans },
                    icon: None,
                    detail: None,
                    decoration: if is_selected {
                        Decoration::Header
                    } else {
                        Decoration::Normal
                    },
                });
            }
        }

        // Footer separator (same dynamic width as the header separator).
        items.push(ListItem {
            text: StyledText {
                spans: vec![StyledSpan::with_fg(
                    "─".repeat(sep_chars),
                    Color::rgb(70, 70, 90),
                )],
            },
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        });
        let hint = format!(
            "  {} matches  ·  j/k ↑↓ navigate  ·  Enter jump  ·  Esc close",
            match_count
        );
        items.push(ListItem {
            text: StyledText {
                spans: vec![StyledSpan::with_fg(hint, Color::rgb(100, 100, 120))],
            },
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        });

        backend.draw_list(
            inner,
            &ListView {
                id: WidgetId::new("issue-finder"),
                title: None,
                items,
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: true,
                bordered: false,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
            },
        );
    }

    // ── Live-session display helpers ────────────────────────────────────────
    // Originally added for the #628 fleet-wide live-sessions overlay
    // (retired by #1033); still used by the #1032/#1033 Sessions panel's
    // tree rows and detail view.

    /// Return the coordinator assignment type label for a live session, e.g.
    /// `"work"`, `"review"`, `"fix"`, `"smoke"`, `"chat"`.  Falls back to
    /// `"work"` when the assignment is not in the local DB (remote machine that
    /// hasn't synced), or `"(unknown)"` when the assignment id can't be found.
    pub(crate) fn session_type_for(&self, session: &LiveTmuxSession) -> String {
        self.data
            .assignments
            .iter()
            .find(|a| a.id == session.assignment_id)
            .map(|a| {
                a.assignment_type
                    .as_deref()
                    .unwrap_or("work")
                    .to_string()
            })
            .unwrap_or_else(|| "(unknown)".to_string())
    }

    /// Return the machine name for a live session.  Prefers the `machine`
    /// field populated from `coord sessions --json`, falls back to joining
    /// the assignment record, then to `"(local)"`.
    pub(crate) fn session_machine_for(&self, session: &LiveTmuxSession) -> String {
        if let Some(m) = &session.machine {
            return m.clone();
        }
        self.data
            .assignments
            .iter()
            .find(|a| a.id == session.assignment_id)
            .map(|a| a.machine.clone())
            .filter(|m| !m.is_empty())
            .unwrap_or_else(|| "(local)".to_string())
    }

    /// #316 Phase A: render the Board Chat tab content.
    /// Shows the chat when a board chat is live, or an empty state with
    /// "Refine" and "New Issue" CTAs when no chat is active.
    pub(crate) fn render_board_chat_tab(&self, backend: &mut dyn Backend, content_rect: Rect) {
        let lh = backend.line_height();
        if self.chat_is_board_chat() {
            // Draw an opaque backing so the empty transcript area doesn't bleed through.
            backend.draw_list(
                content_rect,
                &ListView {
                    id: WidgetId::new("board-chat-tab-bg"),
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
            if let Some(ref chat) = self.inject_chat {
                chat.render(backend, content_rect);
            }
        } else {
            // Empty state: CTA buttons + explanatory text.
            let repo = self.board_active_repo();
            let bar_h = lh * 2.0;
            let bar_rect = Rect::new(content_rect.x, content_rect.y, content_rect.width, bar_h);
            let text_rect = Rect::new(
                content_rect.x,
                content_rect.y + bar_h,
                content_rect.width,
                (content_rect.height - bar_h).max(0.0),
            );

            // Action bar with Refine + New Issue buttons.
            let repo_known = repo.is_some();
            let toolbar = Toolbar {
                focused_index: None,
                id: WidgetId::new("board-chat-cta"),
                buttons: vec![
                    ToolbarButton::Action {
                        id: WidgetId::new("board-chat:refine"),
                        label: "Refine".to_string(),
                        icon: None,
                        key_hint: Some("r".to_string()),
                        enabled: repo_known,
                        is_active: false,
                        tooltip: "Start a board-level refinement chat for the selected repo"
                            .to_string(),
                    },
                    ToolbarButton::Action {
                        id: WidgetId::new("board-chat:new-issue"),
                        label: "New Issue".to_string(),
                        icon: None,
                        key_hint: Some("n".to_string()),
                        enabled: repo_known,
                        is_active: false,
                        tooltip: "Draft a new issue with AI assistance".to_string(),
                    },
                ],
                bg: None,
            };
            backend.draw_toolbar(bar_rect, &toolbar, None, None);

            // Descriptive text below the buttons.
            let mut items: Vec<ListItem> = Vec::new();
            items.push(kv_item("", "", None));
            if let Some(r) = repo {
                items.push(kv_item(
                    "",
                    &format!("  Repo: {}", r),
                    Some(Color::rgb(140, 180, 140)),
                ));
            } else {
                items.push(kv_item(
                    "",
                    "  Select a repo in the sidebar first.",
                    Some(Color::rgb(200, 140, 60)),
                ));
            }
            items.push(kv_item("", "", None));
            items.push(kv_item(
                "",
                "  Refine — explore ideas or discuss the codebase without",
                Some(Color::rgb(140, 140, 160)),
            ));
            items.push(kv_item(
                "",
                "  being tied to a specific issue.",
                Some(Color::rgb(140, 140, 160)),
            ));
            items.push(kv_item("", "", None));
            items.push(kv_item(
                "",
                "  New Issue — chat with an AI to draft a well-structured",
                Some(Color::rgb(140, 140, 160)),
            ));
            items.push(kv_item(
                "",
                "  issue body.  Press f when done to file it on GitHub.",
                Some(Color::rgb(140, 140, 160)),
            ));
            backend.draw_list(
                text_rect,
                &ListView {
                    id: WidgetId::new("board-chat-empty"),
                    title: None,
                    items,
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

    /// #315: called each tick while `pending_chat_resume` is armed.  Looks for
    /// a NEW running assignment of the same chat type for the same issue
    /// (dispatched AFTER the prior worker exited) and, when found, rebinds the
    /// open `inject_chat` overlay to it so the conversation continues seamlessly.
    ///
    /// Returns true when the overlay was rebound (caller should redraw).
    pub(crate) fn maybe_bind_pending_resume(&mut self) -> bool {
        let pending = match &self.pending_chat_resume {
            Some(p) => p.clone(),
            None => return false,
        };
        // Timeout — the resume dispatch likely failed; clear and warn the user.
        if pending.dispatched_at.elapsed() > REFINEMENT_BIND_TIMEOUT {
            self.pending_chat_resume = None;
            self.push_toast(
                "Chat resume timed out",
                &format!(
                    "No new assignment appeared for #{} within {}s. \
                     Try 'coord chat-continue' manually.",
                    pending.issue_number,
                    REFINEMENT_BIND_TIMEOUT.as_secs(),
                ),
                ToastSeverity::Warning,
            );
            return true;
        }
        // Find the newest assignment of the same chat type for this issue that
        // is NOT the prior assignment (which just exited).  Accept either
        // "running" (typical case — the resume worker is still mid-reply) or
        // "done" (fast workers may complete before our bind poll fires, leaving
        // the status already terminal — e.g. trivial replies in <2s).  We
        // still bind so the overlay can render the worker's reply from its SSE
        // log.
        // 5-second grace on the dispatch floor — accounts for clock skew
        // between this machine and whichever machine wrote the row, and for
        // the time between `coord chat-continue` starting and the DB write
        // landing.  Without this floor, the bind picks the OLDEST matching
        // assignment that passes the `id != old` filter, which on a 2nd submit
        // is the FIRST assignment (original `coord refine-chat`).
        // #361: match by `old_type` (the originating assignment's type) rather
        // than hardcoding "refinement" so that test-chat and new-issue-chat
        // continuations also rebind correctly.
        let dispatch_floor = pending.arm_unix_secs - 5.0;
        let matching: Vec<_> = self
            .data
            .assignments
            .iter()
            .filter(|a| a.issue_number == pending.issue_number)
            .filter(|a| a.assignment_type == pending.old_type)
            .filter(|a| a.id != pending.old_assignment_id)
            .filter(|a| {
                a.dispatched_at
                    .map(|d| d >= dispatch_floor)
                    .unwrap_or(false)
            })
            .collect();
        let pick = matching
            .iter()
            .copied()
            .filter(|a| a.status == "running" || a.status == "done")
            .max_by(|a, b| {
                a.dispatched_at
                    .partial_cmp(&b.dispatched_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .cloned();
        let Some(asg) = pick else {
            return false;
        };
        let new_aid = asg.id.clone();

        // Capture the *full* chat history (user + assistant turns) from the
        // old context via the existing transcript builder, drop the synthetic
        // System seed (we only want one of those per chat session — the
        // history itself serves as the seed for the rebound chat), and stash
        // on the new context as `history_turns`.  Without this, only the
        // user's turns survived rebind and the assistant's prior replies
        // visibly vanished — what the user reported as "blew away the
        // previous messages".
        let history_turns: Vec<ChatTurn> =
            if let Some(old_ctx) = self.watch_pool.get(&pending.old_assignment_id) {
                chat_transcript_from_pool(old_ctx)
                    .into_iter()
                    .filter(|t| !matches!(t.role, ChatRole::System))
                    .collect()
            } else {
                Vec::new()
            };

        // Build the new WatchContext for the new assignment (mirrors
        // maybe_bind_pending_refinement, but the overlay stays open).
        if !self.watch_pool.contains_key(&new_aid) {
            let state = WatchState {
                assignment_id: new_aid.clone(),
                machine: asg.machine.clone(),
                repo: asg.repo.clone(),
                issue_number: asg.issue_number,
                assignment_type: asg
                    .assignment_type
                    .clone()
                    .unwrap_or_else(|| "refinement".to_string()),
                scroll: usize::MAX,
            };
            let sse = if let Some(m) = self.data.machines.iter().find(|m| m.name == asg.machine) {
                if !m.host.is_empty() {
                    let rx = spawn_sse_watch(&m.host, &new_aid, 0);
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
                    make_local_sse_state(&new_aid)
                }
            } else {
                make_local_sse_state(&new_aid)
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
                new_aid.clone(),
                WatchContext {
                    state,
                    sse,
                    // inject_transcript / inject_sse_offsets are for NEW turns
                    // submitted in this worker's window; prior turns live in
                    // history_turns (already-rendered ChatTurns) so they survive
                    // every subsequent rebind without re-walking offsets.
                    inject_transcript: Vec::new(),
                    inject_sse_offsets: Vec::new(),
                    history_turns,
                    last_focused_at: Instant::now(),
                },
            );
        }
        // Rebind the overlay to the new assignment.
        self.watch_focused = Some(new_aid.clone());
        // Invalidate the transcript cache so the next tick rebuilds from the
        // new context (avoids displaying stale content for a frame).
        self.chat_transcript_cache_key = None;
        self.pending_chat_resume = None;
        self.pipeline_status = Some((
            format!("✓ Chat resumed — #{}", pending.issue_number),
            Instant::now(),
        ));
        true
    }

    pub(crate) fn dispatch_board_row_command(
        &mut self,
        target: &ContextMenuTarget,
        subcommand: &str,
        toast_title: &str,
        body_template: &str,
    ) -> bool {
        let (repo, num) = match target {
            ContextMenuTarget::BoardRow {
                issue_number: Some(num),
                repo_name: Some(repo),
                ..
            } => (repo.clone(), *num),
            // #2494: the Plans-panel epic-row menu's "Send to Pipeline"
            // reuses this same entry point (`coord track <repo>
            // <tracking_issue>` is identical to the Board row's `track`
            // call — the epic's own issue number just arrives via a
            // different target shape).
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                ..
            } => (repo_name.clone(), *tracking_issue),
            _ => {
                self.push_toast(
                    &format!("{} unavailable", toast_title),
                    "No issue + repo target — focus a row first.",
                    ToastSeverity::Info,
                );
                return false;
            }
        };

        // #1012: when a board service is configured *and* this subcommand
        // maps to a known label change, POST straight to the daemon's
        // `/issue-label` seam instead of spawning a `coord` subprocess — the
        // daemon endpoint is exactly what `coord <subcommand>` itself calls
        // under the hood (`_apply_label_change` →
        // `state.apply_issue_labels` → `POST /issue-label`), so this removes
        // a redundant Python cold-start + import hop on the common path.
        // Falls back to the subprocess spawn below, unchanged, when no
        // board service is configured or the subcommand has no label
        // mapping (`label_change_for_subcommand` returns `None`).
        if let Some((url, token)) = resolve_board_service() {
            if let Some((add, remove)) = label_change_for_subcommand(subcommand) {
                let repo_github = self
                    .data
                    .pipeline_repos
                    .iter()
                    .find(|(name, _)| name == &repo)
                    .map(|(_, gh)| gh.as_str());
                return match apply_issue_labels_remote(
                    &url,
                    token.as_deref(),
                    &repo,
                    num,
                    add,
                    remove,
                    repo_github,
                ) {
                    Ok((_labels, changed)) => {
                        if changed {
                            let body = body_template.replace("{}", &num.to_string());
                            self.push_toast(toast_title, &body, ToastSeverity::Success);
                        } else {
                            self.push_toast(
                                toast_title,
                                &format!("#{}: no change (labels already match).", num),
                                ToastSeverity::Info,
                            );
                        }
                        true
                    }
                    Err(e) => {
                        self.push_toast(
                            &format!("{} failed", toast_title),
                            &format!("#{}: {}", num, e),
                            ToastSeverity::Error,
                        );
                        true
                    }
                };
            }
        }

        let num_str = num.to_string();
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self
            .command_runner
            .spawn_queued(&[subcommand, &repo, &num_str]);
        match outcome {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    toast_title,
                    &format!("#{}: queued — will run after current command.", num),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                let body = body_template.replace("{}", &num.to_string());
                self.push_toast(toast_title, &body, ToastSeverity::Info);
            }
        }
        true
    }

    /// Per-stage doctor: spawn `coord diagnose <repo> <issue> [--reset]` for the
    /// currently-selected pipeline issue.  `coord diagnose` routes through the
    /// daemon (#diagnose), so it reconciles the canonical board even from a thin
    /// client.  The findings + `DIAGNOSE_RESULT` summary are surfaced on
    /// completion by the `poll` handler (which toasts the captured stdout).
    ///
    /// `--stage` is intentionally omitted: the command auto-detects the issue's
    /// most-recent stage server-side, which is the wedged one in practice.
    /// The stage box the operator currently has focused in the Stages strip,
    /// mapped to a `coord diagnose --stage` value — so Diagnose/Reset target the
    /// box they're looking at, not a newest-assignment guess.  `None` (no stage
    /// focused, or a gate coord diagnose doesn't model) → server auto-detect.
    pub(crate) fn selected_pipeline_stage_name(&self) -> Option<String> {
        let idx = self.pipeline_focused_stage?;
        let issue = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i))?;
        let name = self.pipeline_stage_names_for_issue(issue).get(idx)?.clone();
        match name.as_str() {
            "plan" | "work" | "review" | "test" | "merge" => Some(name),
            "smoke" => Some("test".to_string()), // gate alias → diagnose's "test"
            _ => None,
        }
    }

    pub(crate) fn dispatch_diagnose_for_selected_pipeline_row(
        &mut self,
        reset: bool,
        dry_run: bool,
        output_json: bool,
    ) -> bool {
        let (repo, key) = match self.selected_issue_repo_and_key() {
            Some(v) => v,
            None => {
                self.push_toast(
                    "Diagnose",
                    "No issue selected — focus a pipeline row first.",
                    ToastSeverity::Info,
                );
                return false;
            }
        };
        let issue_str = key.1.to_string();
        let mut argv: Vec<String> = vec!["diagnose".into(), repo, issue_str];
        // Target the focused stage box when there is one (else auto-detect).
        if let Some(stage) = self.selected_pipeline_stage_name() {
            argv.push("--stage".into());
            argv.push(stage);
        }
        if reset {
            argv.push("--reset".into());
        }
        if dry_run {
            argv.push("--dry-run".into());
        }
        if output_json {
            argv.push("--json".into());
        }
        let argv_refs: Vec<&str> = argv.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&argv_refs);
        let verb = if reset {
            "Reset stage"
        } else if dry_run {
            "Diagnose stage"
        } else {
            "Diagnose & fix stage"
        };
        match outcome {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => self.push_toast(
                verb,
                &format!("#{}: queued — will run after current command.", key.1),
                ToastSeverity::Info,
            ),
            SpawnQueuedOutcome::Started => self.push_toast(
                verb,
                &format!("#{}: running…", key.1),
                ToastSeverity::Info,
            ),
        }
        true
    }


    /// Route a context-menu `action_id` to the right behaviour.
    /// Stub actions for the MVP (#259); subsequent issues replace with
    /// row-state-specific dispatch.
    pub(crate) fn dispatch_context_menu_action(
        &mut self,
        action_id: &str,
        target: &ContextMenuTarget,
    ) -> bool {
        match action_id {
            "copy-issue-number" => {
                let num = match target {
                    ContextMenuTarget::BoardRow { issue_number, .. } => issue_number.unwrap_or(0),
                    ContextMenuTarget::PipelineRow { issue_number, .. } => {
                        issue_number.unwrap_or(0)
                    }
                    ContextMenuTarget::MachineRow { .. } => 0,
                    ContextMenuTarget::MilestoneHeader { tracking_issue, .. } => *tracking_issue,
                    ContextMenuTarget::TerminalRow { .. } => 0,
                    ContextMenuTarget::PlansStub { .. } => 0,
                    ContextMenuTarget::FleetHealth => 0,
                    // #1755: drive-queue rows carry an issue number, and
                    // it's an `i64` on the wire — clamp rather than silently
                    // reporting 0 for a legitimate row.
                    ContextMenuTarget::DriveQueueRow { issue_number, .. } => {
                        (*issue_number).max(0) as u64
                    }
                    // #2454: a Reports result row's issue number is already
                    // the resolved `u64` its `row_identity` lookup produced
                    // (a row that couldn't resolve one never becomes a
                    // target at all), so there is nothing to clamp.
                    ContextMenuTarget::ReportRow { issue_number, .. } => *issue_number,
                    // #2287: the tab's own issue number, looked up by strip
                    // index (Copy isn't one of this menu's four items today,
                    // but every `ContextMenuTarget` needs a match arm here
                    // regardless of which menu offers "Copy issue #…").
                    ContextMenuTarget::BoardDocTab { idx, .. } => self
                        .board_doc_tabs()
                        .tabs()
                        .get(*idx)
                        .map(|(_, number)| *number)
                        .unwrap_or(0),
                    // #2533: an approved-work-items row carries a portal
                    // submission id, not a GitHub issue number — "Copy issue
                    // #…" isn't one of this menu's items, same rationale as
                    // MachineRow/TerminalRow above.
                    ContextMenuTarget::ApprovedRow { .. } => 0,
                };
                // #1374: the actual clipboard write happens in the two
                // direct UI callers (`handle_context_menu_click` /
                // `context_menu_activate_selected`), which have a
                // `&mut dyn Backend` in scope — this fn doesn't, and
                // threading one through would cascade into every other
                // caller (command-palette / drive dispatch and their
                // tests) that never reaches this action. Stash the text
                // here; the caller flushes it right after this returns.
                self.pending_clipboard_copy = Some(num.to_string());
                self.push_toast(
                    "Copied",
                    &format!("#{} copied to clipboard", num),
                    ToastSeverity::Info,
                );
                true
            }
            "refresh" => {
                self.refresh();
                true
            }
            // #2287 (ms-65 §8c): the Board tab context menu's four items.
            // Each targets the RIGHT-CLICKED tab's strip index carried by
            // `ContextMenuTarget::BoardDocTab`, not necessarily the active
            // tab — see that variant's doc comment and finding 16 in
            // `tests/acceptance/ms-65/manifest.yml`.
            "close-doc-tab" => {
                if let ContextMenuTarget::BoardDocTab { idx, .. } = target {
                    self.close_board_doc_tab(*idx);
                }
                true
            }
            "close-other-doc-tabs" => {
                if let ContextMenuTarget::BoardDocTab { idx, .. } = target {
                    self.close_other_board_doc_tabs(*idx);
                }
                true
            }
            "close-all-doc-tabs" => {
                self.close_all_board_doc_tabs();
                true
            }
            // "Pin tab" is disabled (via `disabled_because`) when the
            // clicked tab is already pinned, so a click never reaches here
            // in that case (`ContextMenuHit::Inert` swallows it before
            // dispatch) — `promote_board_doc_tab` is ALSO its own no-op in
            // that case regardless, as a second line of defence (e.g. the
            // keyboard-activation path, which doesn't consult `disabled`
            // the same way — `context_menu_activate_selected` skips
            // disabled items when moving selection, but belt-and-braces
            // here costs nothing).
            "pin-doc-tab" => {
                if let ContextMenuTarget::BoardDocTab { idx, .. } = target {
                    self.promote_board_doc_tab(*idx);
                }
                true
            }
            // #1631 (H-4): the status bar's "Fleet health…" menu item —
            // opens the detail overlay (`fleet_health.rs::
            // render_fleet_health_overlay`); Esc closes it (`events.rs`).
            "open-fleet-health-detail" => {
                self.open_fleet_health_overlay();
                true
            }
            // #1755 (DQ-3) / #1868 (Q-3): the status bar's "Drive queue…"
            // menu item — the modal overlay it used to open was retired by
            // #1868 in favour of the Queue panel, the one queue surface.
            "open-drive-queue-detail" => {
                self.switch_active_view(SidebarView::Queue);
                true
            }
            // #1755 (DQ-3): drive-queue row actions (Queue panel, and the
            // Pipeline/Board "Add to drive queue" row menu). Each shells the
            // matching `coord drive-queue` verb through the spawn-and-toast
            // seam — no direct DB access, same posture as every other TUI
            // action.
            "drive-queue-remove" => {
                if let ContextMenuTarget::DriveQueueRow {
                    repo_name,
                    issue_number,
                    ..
                } = target
                {
                    self.dispatch_drive_queue_remove(repo_name, *issue_number);
                }
                true
            }
            "drive-queue-move-up" | "drive-queue-move-down" => {
                if let ContextMenuTarget::DriveQueueRow {
                    repo_name,
                    issue_number,
                    position,
                    ..
                } = target
                {
                    let delta = if action_id == "drive-queue-move-up" { -1 } else { 1 };
                    let (repo, issue, to) = (repo_name.clone(), *issue_number, *position + delta);
                    self.dispatch_drive_queue_move(&repo, issue, to);
                }
                true
            }
            "drive-queue-unblock" => {
                if let ContextMenuTarget::DriveQueueRow {
                    repo_name,
                    issue_number,
                    ..
                } = target
                {
                    let (repo, issue) = (repo_name.clone(), *issue_number);
                    self.dispatch_drive_queue_unblock(&repo, issue);
                }
                true
            }
            // #1757: release a fired deploy gate on the named row. Offered
            // only when that row's gate is actually holding the queue (see
            // `context_menu_items_for_drive_queue_row`).
            "drive-queue-resume" => {
                if let ContextMenuTarget::DriveQueueRow {
                    repo_name,
                    issue_number,
                    ..
                } = target
                {
                    let (repo, issue) = (repo_name.clone(), *issue_number);
                    self.dispatch_drive_queue_resume(&repo, issue);
                }
                true
            }
            // #2016: Queue row → "View in Pipeline" / "View on Board" — jump
            // to the row's issue elsewhere in the app without disturbing the
            // queue itself (no reorder, no optimistic mutation, no refetch).
            "drive-queue-view-in-pipeline" => {
                if let ContextMenuTarget::DriveQueueRow {
                    repo_name,
                    issue_number,
                    ..
                } = target
                {
                    let (repo, issue) = (repo_name.clone(), (*issue_number).max(0) as u64);
                    self.jump_to_pipeline(&repo, issue);
                }
                true
            }
            "drive-queue-view-on-board" => {
                if let ContextMenuTarget::DriveQueueRow {
                    repo_name,
                    issue_number,
                    ..
                } = target
                {
                    let (repo, issue) = (repo_name.clone(), (*issue_number).max(0) as u64);
                    // #2454: through the shared `jump_to_board` now, so this
                    // item and the Reports panel's own "View on Board" can't
                    // drift — and so both get the pinned Board document tab
                    // (#2282/#2285) that didn't exist when #2016 shipped.
                    self.jump_to_board(&repo, issue);
                }
                true
            }
            // #2454: Reports result row → "View on Board". Same verb, same
            // shared body as the Queue row's above; the only difference is
            // where the `(repo, issue)` came from — a `ReportRow` target
            // resolved generically through the catalogue's `row_identity`
            // declaration, never through a `match` on which report it is.
            "report-row-view-on-board" => {
                if let ContextMenuTarget::ReportRow {
                    repo_name,
                    issue_number,
                } = target
                {
                    let (repo, issue) = (repo_name.clone(), *issue_number);
                    self.jump_to_board(&repo, issue);
                }
                true
            }
            // #2533 (ms-67 contract §4a/§4c): "Pull into decomposition
            // session" on an Approved-work-items row. The menu already
            // gates this item on the row having ≥1 mapped repo
            // (`context_menu_items_for_approved_row`'s `disabled_because`) —
            // a disabled item never reaches `handle_context_menu_click`'s
            // leaf-action dispatch, so this arm can dispatch unconditionally
            // once it resolves a row.
            "pull-into-decomposition-session" => {
                if let ContextMenuTarget::ApprovedRow { submission_id } = target {
                    self.dispatch_approved_pull_into_decomposition(submission_id);
                }
                true
            }
            // #2863: "Open attended intake session…" — the `--interactive`
            // (#2750) sibling of the arm above. Same gating posture: the
            // menu already greys it (`attended_intake_blocked_reason`) when
            // this machine can't host it, and a disabled item never reaches
            // this leaf dispatch, so no re-check here.
            "open-attended-intake-session" => {
                if let ContextMenuTarget::ApprovedRow { submission_id } = target {
                    let sid = submission_id.clone();
                    self.launch_attended_intake_session(&sid);
                }
                true
            }
            // #1755 (DQ-3): Pipeline-row enqueue items. `drive-queue-add-on:
            // <machine>` is the submenu variant — the machine name rides in
            // the action id (the `start-*-on:` precedent) because
            // `ContextMenuItem` carries no per-item payload.
            "drive-queue-add" => {
                if let Some((repo, issue)) = self.pipeline_menu_repo_issue(target) {
                    self.dispatch_drive_queue_add(&repo, issue, None, &[]);
                }
                true
            }
            "drive-queue-add-after" => {
                if let Some((repo, issue)) = self.pipeline_menu_repo_issue(target) {
                    self.open_drive_queue_after_input(&repo, issue);
                }
                true
            }
            "drive-queue-row-remove" => {
                if let Some((repo, issue)) = self.pipeline_menu_repo_issue(target) {
                    self.dispatch_drive_queue_remove(&repo, issue as i64);
                }
                true
            }
            _ if action_id.starts_with("drive-queue-add-on:") => {
                let machine = action_id
                    .strip_prefix("drive-queue-add-on:")
                    .unwrap_or_default()
                    .to_string();
                if let Some((repo, issue)) = self.pipeline_menu_repo_issue(target) {
                    self.dispatch_drive_queue_add(&repo, issue, Some(&machine), &[]);
                }
                true
            }
            // #1123 (contract §4c): "New plan > Quick capture" — the
            // labelled menu equivalent of the bare `c` key. Opens the same
            // title-entry prompt `c` arms (`pending_plan_capture`); submit
            // handling (`capture_plan_stub`) is unchanged.
            "capture-plan-quick" => {
                self.pending_plan_capture = Some(String::new());
                true
            }
            // #1123 (contract §4c): "New plan > Guided chat…" — the
            // labelled menu equivalent of the bare `C` key. Opens the same
            // (optional) title-entry prompt `C` arms
            // (`pending_new_milestone_chat`); submit handling
            // (`capture_plan_chat`) is unchanged.
            "capture-plan-chat" => {
                self.pending_new_milestone_chat = Some(String::new());
                true
            }
            // #1123 (contract §4b / §8 note 2): "Create work order /
            // promote to epic…" on an epic-less stub row. The contract
            // pins only the menu label, not the action — this routes
            // directly to the same guided-chat flow "New plan > Guided
            // chat…" uses, pre-filled with the stub's own title (when
            // known) rather than inventing a bespoke "promote" CLI verb
            // that `coord milestone` doesn't have. `capture_plan_chat`
            // resolves its target repo from `plans_selected()`, which
            // already points at this row (mouse right-click pre-selects it
            // in `events.rs` before building the target).
            "promote-milestone-to-epic" => {
                let title = match target {
                    ContextMenuTarget::PlansStub {
                        milestone: Some((_, title)),
                        ..
                    } => title.clone(),
                    _ => String::new(),
                };
                self.capture_plan_chat(title);
                true
            }
            // #260: Refine — move a Backlog row into the Refining
            // section by spawning `coord refine <repo> <num>`.
            "refine" => self.dispatch_board_row_command(
                target,
                "refine",
                "Refine",
                "#{}: tagging status:refining…",
            ),
            // #264: Refine with chat — dispatch a refinement-chat worker
            // and open a ChatController bound to it.
            "refine-chat" => self.dispatch_refine_chat(target),
            // #pause: toggle routing-pause for a machine.
            "machine-pause" | "machine-resume" => {
                let name = match target {
                    ContextMenuTarget::MachineRow { name, .. } => name.clone(),
                    _ => return false,
                };
                let cmd = if action_id == "machine-pause" {
                    "pause"
                } else {
                    "unpause"
                };
                use crate::commands::SpawnQueuedOutcome;
                let outcome = self.command_runner.spawn_queued(&[cmd, &name]);
                match outcome {
                    SpawnQueuedOutcome::Deduped => return false,
                    SpawnQueuedOutcome::Queued => {
                        let verb = if action_id == "machine-pause" {
                            "pause"
                        } else {
                            "resume"
                        };
                        self.push_toast(
                            "Machine routing",
                            &format!(
                                "{}: {} queued — will run after current command.",
                                name, verb
                            ),
                            ToastSeverity::Info,
                        );
                    }
                    SpawnQueuedOutcome::Started => {
                        // Optimistic local update — the file write is fast and the
                        // next periodic refresh re-reads to catch any concurrent
                        // edits.  Without this the badge would lag by ~1 s.
                        if action_id == "machine-pause" {
                            self.paused_machines.insert(name.clone());
                        } else if self.cordoned_machines.contains(&name) {
                            // #2101 trap A, at the optimism layer: `coord
                            // unpause` does NOT lift a release cordon, so
                            // predicting that it did would show the operator
                            // a machine taking work again for one refresh
                            // cycle when nothing changed. Leave both sets
                            // alone; the next `GET /pause` confirms it.
                        } else {
                            self.paused_machines.remove(&name);
                            // #1862: `coord unpause` on a quiet-paused machine
                            // grants an override (dispatchable again this
                            // window) — clear the quiet badge too, or it would
                            // stay stuck on `[QUIET]` (paused_machines no
                            // longer contains it, but the badge check looks at
                            // `quiet_paused_machines` first) until the next
                            // periodic `GET /pause` refresh catches up.
                            self.quiet_paused_machines.remove(&name);
                        }
                        let verb = if action_id == "machine-pause" {
                            "paused"
                        } else {
                            "resumed"
                        };
                        self.push_toast(
                            "Machine routing",
                            &format!("{}: {}", name, verb),
                            ToastSeverity::Info,
                        );
                    }
                }
                true
            }
            // #2147: "Set quiet hours…" / "Quiet hours: HH:MM-HH:MM…" —
            // arms the text-input dialog (`pending_quiet_hours`) rather
            // than spawning directly; the window has to be typed first.
            "machine-quiet-hours-set" => {
                let name = match target {
                    ContextMenuTarget::MachineRow { name, .. } => name.clone(),
                    _ => return false,
                };
                self.open_quiet_hours_dialog(name);
                true
            }
            // #2147: "Clear quiet hours" — direct action, no dialog needed
            // (there's nothing to type). Mirrors the `machine-pause` arm
            // above: same three-arm `SpawnQueuedOutcome` match, same toast
            // shape, same "schedule map only, never the paused/quiet sets"
            // optimistic-update trap (see `dispatch_quiet_hours_clear`).
            "machine-quiet-hours-clear" => {
                let name = match target {
                    ContextMenuTarget::MachineRow { name, .. } => name.clone(),
                    _ => return false,
                };
                self.dispatch_quiet_hours_clear(&name)
            }
            // #956: Kill terminal — arms the confirm dialog rather than
            // killing directly (terminals are persistent and may hold live
            // work). Shares `pending_kill_terminal` with the `K` keybinding.
            "kill-terminal" => {
                let (machine, name) = match target {
                    ContextMenuTarget::TerminalRow { machine, name } => {
                        (machine.clone(), name.clone())
                    }
                    _ => return false,
                };
                self.pending_kill_terminal = Some(PendingKillTerminal { machine, name });
                true
            }
            // #815: View in Pipeline — jump from the Board to the matching
            // Pipeline entry (same logic as pressing `p`).
            "jump-to-pipeline" => {
                self.jump_board_to_pipeline();
                true
            }
            // #261: Send to Pipeline — add the `coord` label so the
            // issue moves Refined → Pipeline:New.  Wraps the new
            // `coord track <repo> <num>` Python command.
            "send-to-pipeline" => {
                let dispatched = self.dispatch_board_row_command(
                    target,
                    "track",
                    "Send to Pipeline",
                    "#{} → Pipeline (adding coord label…)",
                );
                if dispatched {
                    // coord track adds the coord label on GitHub; kick the
                    // pipeline loader so the issue appears in New without
                    // waiting for the 60 s auto-refresh.
                    self.maybe_kick_pipeline_loader();
                }
                dispatched
            }
            // #266: Refining → Refined — wraps existing `coord ready`.
            "mark-refined" => self.dispatch_board_row_command(
                target,
                "ready",
                "Mark Refined",
                "#{} → Refined (tagging status:ready…)",
            ),
            // #266: Refining → Backlog (strips status:refining).
            // Refined → Refining is handled by `coord refine` which
            // also removes status:ready, so `drop-to-refining` reuses
            // the same command.
            // #266: shared by the Board row menu (Refining/Refined → Backlog)
            // and the Pipeline row menu (New / In-progress → Backlog).
            // Board rows carry repo+num on the target; Pipeline rows resolve
            // them from the selected pipeline issue instead.
            "drop-to-backlog" => match target {
                ContextMenuTarget::PipelineRow { .. } => {
                    let ok = self.drop_selected_to_backlog();
                    if !ok {
                        self.push_toast(
                            "Drop to backlog",
                            "No issue selected or no repo mapping found.",
                            ToastSeverity::Warning,
                        );
                    }
                    ok
                }
                _ => self.dispatch_board_row_command(
                    target,
                    "backlog",
                    "Drop to Backlog",
                    "#{}: stripping status:* label…",
                ),
            },
            "drop-to-refining" => self.dispatch_board_row_command(
                target,
                "refine",
                "Drop to Refining",
                "#{}: tagging status:refining…",
            ),
            // #1500: "Mark ready" — stage the selected New row (or epic +
            // non-Done children) into In-progress:`ready` via `status:queued`.
            // No dispatch — pure display + intent.
            "mark-ready" => {
                let ok = self.mark_selected_ready();
                if !ok {
                    self.push_toast(
                        "Mark ready",
                        "No issue selected or no repo mapping found.",
                        ToastSeverity::Warning,
                    );
                }
                ok
            }
            // #1500: "Unmark ready" — the reverse: strips `status:queued`
            // (epic + any queued children too), returning the row to New.
            "unmark-ready" => {
                let ok = self.unmark_selected_ready();
                if !ok {
                    self.push_toast(
                        "Unmark ready",
                        "No issue selected or no repo mapping found.",
                        ToastSeverity::Warning,
                    );
                }
                ok
            }
            // #262: Start with Plan — dispatches a `type="plan"` worker
            // first.  Reuses the existing Pipeline-stage dispatcher so
            // the click + the [Go] button on the stage strip share one
            // code path.
            // #467: human-attended interactive launchers from the row menu.
            // Both switch to the detail Terminal tab so the spawned session
            // is visible.  Work implements directly; Plan plans-then-works in
            // the same session (work tools via --no-plan + plan-first briefing).
            "start-work-interactive" => {
                self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Work);
                true
            }
            // Dedicated "Reattach to live session" action — reattaches to the
            // live session for this issue REGARDLESS of its type (work / review
            // / test / merge).  The generic Start launchers stay type-gated
            // (#569), so this explicit reattach is the only type-agnostic path.
            "reattach-live-session" => {
                self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                self.reattach_to_selected_issue_live_session();
                true
            }
            "start-plan-interactive" => {
                self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Plan);
                true
            }
            // #539: human-attended interactive review launcher.
            "start-review-interactive" => {
                self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Review);
                true
            }
            "start-fix-interactive" => {
                self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Fix);
                true
            }
            // #1104: human-attended CONTINUE of a work assignment that
            // failed outright, on its existing branch (`--rework-of`).
            "start-continue-interactive" => {
                self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Continue);
                true
            }
            // #569: Troubleshoot — human-attended diagnostic session for a
            // stalled In-progress item.  Only shown for InProgress rows;
            // the session is seeded with a full board-state snapshot.
            // #628: "Chat about issue" — subsumes the old Troubleshoot. Both
            // action ids launch the Chat session (any lingering keyboard/menu
            // path to the old id still works).
            // #675: Route to the Board Terminal tab when invoked from the Board
            // panel, not the Pipeline Terminal tab.
            "chat-about-issue" | "troubleshoot-interactive" => {
                if self.active_view == SidebarView::Board {
                    self.board_detail_tab = BoardDetailTab::Terminal;
                } else {
                    self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                }
                self.launch_interactive_session_for_selected_issue(
                    InteractiveLaunchMode::Chat,
                );
                true
            }
            // Milestone Outcome Audit Phase 1 (#885): human-attended
            // read-only milestone-outcome analyst for an epic row.
            "audit-outcomes" => {
                if self.active_view == SidebarView::Board {
                    self.board_detail_tab = BoardDetailTab::Terminal;
                } else {
                    self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                }
                self.launch_interactive_session_for_selected_issue(
                    InteractiveLaunchMode::Audit,
                );
                true
            }
            // #1059: Gate A dispatch for an epic row — headless (mirrors
            // `start-skip-plan`/`start-merge-automated`), NOT an interactive
            // session: `coord acceptance mock` dispatches a normal
            // (non-interactive) `claude -p` mock-author worker
            // (`coord/mock_author.py`), same as `coord assign`.
            "dispatch-gate-a-mock" => {
                self.dispatch_gate_a_mock_for_selected_pipeline_row();
                true
            }
            // #1059: view the Gate-A mock-author's PR (contract.md + the
            // rendered mock(s)) for human sign-off — reuses the same "Open
            // PR" mechanism as the Done-lifecycle item below, just reachable
            // on an epic row before it reaches Done.
            "view-gate-a-mock" => {
                let opened = self.dispatch_open_pr_for_selected_pipeline_row();
                if !opened {
                    self.push_toast(
                        "View Gate A mock",
                        "No PR yet for this issue — dispatch Gate A mock first, \
                         or wait for the worker to push its branch.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            // #2501: local counterpart to "view-gate-a-mock" — pulls the
            // local checkout's default branch (fast-forward only) and opens
            // the rendered mock(s) from disk, since the PR route can never
            // actually show them (GitHub renders `.html` as source, not a
            // live page). All toasting on abort/failure happens inside
            // `dispatch_view_gate_a_mock_local_for_selected_pipeline_row`.
            "view-gate-a-mock-local" => {
                self.dispatch_view_gate_a_mock_local_for_selected_pipeline_row();
                true
            }
            // #2063: record the human Gate-A verdict. `--approved` is what
            // unblocks the milestone's issues; `--changes` records a
            // rejection so the refusal downstream can say what to fix
            // instead of "nobody has looked yet".
            "approve-gate-a" => {
                self.dispatch_gate_a_verdict_for_selected_pipeline_row(true, "");
                true
            }
            // #2500: no longer dispatches directly — opens the "What needs
            // to change?" prompt (`pending_gate_a_changes_note`) first, so
            // the rejection carries a note instead of firing silently.
            // `submit_gate_a_changes_note_input` does the actual dispatch
            // on Submit; Cancel/Esc drops it with no `coord gate-a` call.
            "request-gate-a-changes" => {
                self.open_gate_a_changes_note_input();
                true
            }
            // #2513 (PDR-5): on-demand "publish mocks to portal" —
            // `coord portal publish-mocks <repo> <tracking_issue>`.
            "publish-mocks-to-portal" => {
                self.dispatch_publish_mocks_to_portal_for_selected_pipeline_row();
                true
            }
            // #1060: JIT-author this issue's acceptance-suite slice —
            // `coord acceptance author <repo> <tracking_issue> --issue N`.
            "author-acceptance-tests" => {
                self.dispatch_acceptance_author_for_selected_pipeline_row();
                true
            }
            // #1174 (docs/ORACLE_LOOP.md, #1173): human-attended
            // test-authoring session — `coord acceptance author <repo>
            // <tracking_issue> --issue N --interactive`, launched into the
            // standalone Terminal pane like Test/Review/Merge's
            // "(interactive)" actions.
            "author-acceptance-tests-interactive" => {
                self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                self.launch_acceptance_author_interactive_for_selected_pipeline_row();
                true
            }
            // #1060: external trust-gate re-run against the pushed SHA —
            // `coord acceptance record --repo <repo> --issue N --sha <sha>`.
            "record-acceptance" => {
                self.dispatch_acceptance_record_for_selected_pipeline_row();
                true
            }
            // #935 Part B: unified per-stage doctor — dry-run-diagnoses first,
            // then opens a results dialog with option buttons.
            "diagnose-fix-stage" => {
                self.dispatch_diagnose_for_selected_pipeline_row(false, true, true);
                true
            }
            // Internal action IDs used by the diagnose dialog buttons:
            // Recover = full diagnose (no reset, no dry-run).
            "diagnose-stage" => {
                self.dispatch_diagnose_for_selected_pipeline_row(false, false, false);
                self.pending_diagnose_dialog = None;
                true
            }
            // Reset stage = diagnose --reset.
            "diagnose-reset" => {
                self.dispatch_diagnose_for_selected_pipeline_row(true, false, false);
                self.pending_diagnose_dialog = None;
                true
            }
            // Clear phantom live session — purges stale "pending-" live_tmux_sessions
            // entries for the selected issue without running any coord command.
            "diagnose-clear-phantom" => {
                if let Some(dlg) = self.pending_diagnose_dialog.take() {
                    let repo = dlg.repo.clone();
                    let issue_number = dlg.issue_number;
                    let before = self.live_tmux_sessions.len();
                    self.live_tmux_sessions.retain(|s| {
                        !(s.assignment_id.starts_with("pending-")
                            && s.issue_number == Some(issue_number)
                            && s.repo_name.as_deref() == Some(&repo))
                    });
                    let removed = before - self.live_tmux_sessions.len();
                    self.push_toast(
                        "Phantom session cleared",
                        &format!(
                            "#{}: removed {} phantom live-session entr{}. \
                             The card will show as 'ready' on the next refresh.",
                            issue_number,
                            removed,
                            if removed == 1 { "y" } else { "ies" },
                        ),
                        ToastSeverity::Info,
                    );
                }
                true
            }
            // Leg 3c / A3 (#517, #581): human-attended testing agent launcher.
            "start-testing-interactive" => {
                self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Test);
                true
            }
            // Leg 3c (#517, #306): human-attended merge agent launcher.
            // #684: guard against launching an interactive session when the
            // headless queue is already actively merging this branch.
            "start-merge-interactive" => {
                let issue_num = self
                    .selected_issue_repo_and_key()
                    .map(|(_, k)| k.1)
                    .unwrap_or(0);
                if self.has_active_headless_merge_for_issue(issue_num) {
                    self.push_toast(
                        "Start merge (interactive)",
                        "The headless merge queue is already merging this branch — \
                         wait for it to finish (or drop it from the Merge Queue panel \
                         first).",
                        ToastSeverity::Warning,
                    );
                } else {
                    self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                    self.launch_interactive_session_for_selected_issue(
                        InteractiveLaunchMode::Merge,
                    );
                }
                true
            }
            // #684: headless merge via the existing queue — `coord merge --order`.
            "start-merge-automated" => {
                self.dispatch_merge_automated_for_selected_pipeline_issue();
                true
            }
            // #2402: Pipeline row → "Re-verify (revalidate)" — arms the
            // confirm dialog (`pending_merge_revalidate`, resolved by
            // `confirm_merge_revalidate`) rather than spawning directly, the
            // same posture `pending_force_merge` takes for a gate-bypass
            // action (#1214 precedent): `--revalidate` runs a real
            // build+test locally on the daemon host, so it isn't a bare
            // click-and-go. `context_menu_items_for_pipeline_row` already
            // confirmed BLOCKED + a revalidate-eligible reason before
            // offering this item, so this arm only resolves the
            // `assignment_id` to pass — it does not re-check eligibility.
            "merge-revalidate" => {
                if let ContextMenuTarget::PipelineRow {
                    issue_number: Some(n),
                    repo_name: Some(r),
                    ..
                } = target
                {
                    let repo_slug = self.pipeline_issues.iter().find_map(|iss| {
                        (iss.number == *n && iss.coord_repo.as_deref() == Some(r.as_str()))
                            .then_some(iss.repo_slug.clone())
                    });
                    if let Some(entry) = repo_slug
                        .and_then(|slug| self.merge_plan_entry_for_issue(&slug, *n).cloned())
                    {
                        self.pending_merge_revalidate = Some(PendingMergeRevalidate {
                            assignment_id: entry.assignment_id,
                            issue_num: *n,
                        });
                    }
                }
                true
            }
            // #2634: "Drive (automated)" — the primary automated action now
            // queues the issue on the durable, board-backed drive queue
            // (`coord drive-queue add <repo> <issue>`) instead of launching
            // a local tmux session. Same seam the Board/Pipeline "Add to
            // drive queue" items use (`"drive-queue-add"` below) — the queue
            // is the supported path, and this is the menu's default lead-in
            // to it. Deliberately does NOT touch `pipeline_detail_tab`:
            // queueing starts nothing locally, so there is no live run for
            // the Terminal tab to show yet.
            //
            // #2645: an epic (tracking-issue) row is NOT "the issue" in the
            // sense `drive-queue add` expects — it carries the `epic`
            // label, so `coord.dispatch.enforce_epic_dispatch_guard`
            // refuses any `type="work"` dispatch against it (#1314), and
            // `coord/drive_queue.py` sends a pre-dispatch refusal straight
            // to `blocked` without incrementing `attempts` — one of the two
            // cases #2230's self-heal deliberately never re-evaluates, so
            // the row stays permanently blocked. What an epic row's "Drive
            // (automated)" click should mean is exactly what the Plans
            // panel's "Dispatch milestone" already does — expand the
            // epic's `## Work order` DAG and queue every still-open child
            // in dependency order — so this re-points at the same `coord
            // milestone dispatch <repo> <tracking_issue>` seam
            // `dispatch_milestone_action` uses, instead of queuing the
            // tracking issue itself.
            "start-drive" => {
                if let Some((repo, issue)) = self.pipeline_menu_repo_issue(target) {
                    let epic_target = self
                        .pipeline_issues
                        .iter()
                        .find(|iss| {
                            iss.number == issue && iss.coord_repo.as_deref() == Some(repo.as_str())
                        })
                        .filter(|iss| labels_carry_epic_label(&iss.all_labels))
                        .map(|iss| iss.title.clone());
                    if let Some(title) = epic_target {
                        self.spawn_milestone_dispatch_command(&repo, issue, &title);
                    } else {
                        self.dispatch_drive_queue_add(&repo, issue, None, &[]);
                    }
                }
                true
            }
            // #2634: "Drive locally (tmux, this machine)" — the demoted,
            // explicitly-local former "Drive (automated)" behavior: `coord
            // drive <repo> <issue> --tmux`, detached so it survives the TUI
            // restarting, but LOCAL and never written to `drive_queue` (see
            // `drive.rs`'s module doc for why that matters).
            "start-drive-local" => {
                self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                self.launch_drive_for_selected_issue();
                true
            }
            // #1398: reattach the per-issue Terminal tab to a live drive.
            "attach-drive" => {
                self.pipeline_detail_tab = PipelineDetailTab::Terminal;
                self.attach_drive_for_selected_issue();
                true
            }
            // #1398: arms the "Stop drive" confirm dialog rather than
            // killing directly — a drive can carry 60-90 minutes of
            // in-flight work. Resolved straight from `target` (mirrors
            // "kill-terminal") rather than the live selection.
            "stop-drive" => {
                let (repo, issue) = match target {
                    ContextMenuTarget::PipelineRow {
                        issue_number: Some(n),
                        repo_name: Some(r),
                        ..
                    } => (r.clone(), *n),
                    _ => return false,
                };
                self.pending_kill_drive = Some(PendingKillDrive { repo, issue });
                true
            }
            // #1505: "Run it" — the human's one-key response to a driver
            // escalation.  Deliberately NOT gated behind a confirm dialog:
            // the click itself, reached only by an operator reading the
            // proposed command off the menu label, IS the "one-key human
            // decision" the escalation record exists for (see
            // `dispatch_run_escalation`'s doc comment).
            "run-escalation" => {
                let (repo, issue) = match target {
                    ContextMenuTarget::PipelineRow {
                        issue_number: Some(n),
                        repo_name: Some(r),
                        ..
                    } => (r.clone(), *n),
                    _ => return false,
                };
                self.dispatch_run_escalation(&repo, issue);
                true
            }
            // #1505: "Dismiss escalation" — clears the record without
            // acting on it (e.g. it was resolved by hand already, or the
            // proposed command doesn't apply).
            "dismiss-escalation" => {
                let (repo, issue) = match target {
                    ContextMenuTarget::PipelineRow {
                        issue_number: Some(n),
                        repo_name: Some(r),
                        ..
                    } => (r.clone(), *n),
                    _ => return false,
                };
                self.dispatch_dismiss_escalation(&repo, issue);
                true
            }
            // #2370: "Run proposed fix"'s submenu children — one per
            // `decisions`-report option (`decide-escalation:0` =
            // Recommended, `decide-escalation:1` = Inspect; see the
            // `ContextMenuItem::parent` push above). The index rides in the
            // action id (the `drive-queue-add-on:<machine>` precedent)
            // since `ContextMenuItem` carries no per-item payload.
            _ if action_id.starts_with("decide-escalation:") => {
                let index: usize = action_id
                    .strip_prefix("decide-escalation:")
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0);
                if let Some((repo, issue)) = self.pipeline_menu_repo_issue(target) {
                    self.dispatch_decide_escalation(&repo, issue, index);
                }
                true
            }
            // #2374: the fleet-escalation status-bar strip's submenu
            // children — same shape as `decide-escalation:<index>` above,
            // except the `(repo, issue)` payload rides in the action id
            // itself (`decide-fleet-escalation:<repo>#<issue>:<index>`)
            // rather than `target`, since a right-click on the status bar is
            // a single whole-bar `ContextMenuTarget::FleetHealth` with no
            // per-row identity to recover it from (`escalation.rs`'s
            // `fleet_escalation_key` doc comment).
            _ if action_id.starts_with("decide-fleet-escalation:") => {
                if let Some(rest) = action_id.strip_prefix("decide-fleet-escalation:") {
                    if let Some((key, index_str)) = rest.rsplit_once(':') {
                        let index: usize = index_str.parse().unwrap_or(0);
                        self.dispatch_decide_fleet_escalation(key, index);
                    }
                }
                true
            }
            // #2374: the fleet-escalation strip's "Dismiss escalation".
            _ if action_id.starts_with("dismiss-fleet-escalation:") => {
                if let Some(key) = action_id.strip_prefix("dismiss-fleet-escalation:") {
                    self.dispatch_dismiss_fleet_escalation(key);
                }
                true
            }
            "start-with-plan" => {
                let dispatched = self.dispatch_pipeline_plan();
                if !dispatched {
                    self.push_toast(
                        "Start with Plan",
                        "Couldn't dispatch — see status bar for details.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            // Pipeline:InProgress — Watch opens the live log overlay.
            // Same behaviour as Enter on a Pipeline row; the menu /
            // action-bar entry is for users without right-click or who
            // prefer mousing to keyboard shortcuts.
            "watch" => {
                let opened = self.open_watch_for_selected_issue();
                if !opened {
                    self.push_toast(
                        "Watch",
                        "No active assignment to watch for this issue.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            // #bounce: address review findings — dispatches a fix
            // worker for the most recent review-typed assignment whose
            // verdict is `request-changes`.  Same code path as the auto-
            // loop's automatic bounce; this is the manual trigger for
            // when the auto-loop didn't fire (remote log unreachable,
            // notify happened too late, etc.) or for retrying.
            "bounce" => {
                self.dispatch_bounce_for_selected_pipeline_row();
                true
            }
            // Pipeline:InProgress — Stop cancels the running worker.
            // Mirrors `kill_watched` but works from outside the watch
            // overlay (which is the whole point — you shouldn't have
            // to open the overlay to kill a worker).
            "stop" => {
                let stopped = self.dispatch_stop_for_selected_pipeline_row();
                if !stopped {
                    self.push_toast(
                        "Stop",
                        "No running assignment to stop for this issue.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            // Pipeline:Done — Open PR launches the PR in a browser via
            // `gh pr view --web`, which handles cross-platform browser
            // opening (xdg-open / open / start) on our behalf.
            "open-pr" => {
                let opened = self.dispatch_open_pr_for_selected_pipeline_row();
                if !opened {
                    self.push_toast(
                        "Open PR",
                        "No PR open yet for this issue.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            // #262: Skip Plan, start Work — dispatches a `type="work"`
            // worker directly.  Same path the existing [Go] button on
            // the Work stage uses.
            "start-skip-plan" => {
                let dispatched = self.dispatch_pipeline_work();
                if !dispatched {
                    self.push_toast(
                        "Start Work",
                        "Couldn't dispatch — see status bar for details.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            // #771: promote a milestone's whole declared work order into the
            // pipeline — `coord milestone dispatch <repo> <tracking_issue>`.
            "dispatch-milestone" => self.dispatch_milestone_action(target),
            // #1003: Plans-panel / MilestoneDag row CRUD — see
            // milestone_dag.rs for each action's doc comment.
            "open-milestone-chat" => self.open_milestone_chat_action(target),
            "dispatch-milestone-next" => self.dispatch_milestone_next_action(target),
            "view-milestone-order" => self.view_milestone_order_action(target),
            "edit-milestone" => self.open_edit_milestone_input(target),
            "add-issue-to-milestone" => self.open_add_issue_to_milestone_input(target),
            "remove-issue-from-milestone" => {
                self.open_remove_issue_from_milestone_input(target)
            }
            // #1008: splices the epic's own `## Sub-issues` checklist
            // (`coord milestone add-child`) — see milestone_dag.rs.
            "add-sub-issue-to-epic" => self.open_add_sub_issue_to_epic_input(target),
            // #1017: chat-driven alternative — see milestone_dag.rs.
            "add-sub-issue-to-epic-chat" => self.open_add_sub_issue_to_epic_chat_input(target),
            "close-plan" => self.open_close_plan_confirm(target),
            // #685: open the test-mode choice dialog (SetOnly — no dispatch).
            "set-test-mode" => {
                let result = self.arm_set_test_mode_for_selected();
                if !result {
                    self.push_toast(
                        "Set test mode",
                        "No issue selected or no repo mapping found.",
                        ToastSeverity::Warning,
                    );
                }
                true
            }
            _ => {
                self.push_toast(
                    "Unknown context-menu action",
                    &format!("No handler for `{}` — likely a stale id.", action_id),
                    ToastSeverity::Warning,
                );
                false
            }
        }
    }

    /// #2147: arm `pending_quiet_hours` for `machine`, pre-filled with its
    /// current effective window (`quiet_hours_windows`, either an
    /// operator-set or `coordinator.yml` schedule) or the `22:00-08:00`
    /// default when none is set — so an operator adjusting an existing
    /// window sees it rather than a blank field. `tz` is appended
    /// space-separated only when known (`PendingQuietHours`'s single-buffer
    /// shape).
    pub(crate) fn open_quiet_hours_dialog(&mut self, machine: String) {
        let buf = self
            .quiet_hours_windows
            .get(&machine)
            .map(|w| {
                if w.tz.is_empty() {
                    format!("{}-{}", w.start, w.end)
                } else {
                    format!("{}-{} {}", w.start, w.end, w.tz)
                }
            })
            .unwrap_or_else(|| "22:00-08:00".to_string());
        self.pending_quiet_hours = Some(PendingQuietHours { machine, buf });
    }

    /// #2147: spawn `coord quiet-hours <name> --clear`. Mirrors the
    /// `machine-pause` context-menu arm's three-outcome `SpawnQueuedOutcome`
    /// match and toast shape exactly (same CLI/spawn seam, different argv)
    /// — `Deduped` returns `false` ("not handled", same as that arm),
    /// everything else returns `true`. Shared by the context menu's direct
    /// "Clear quiet hours" item and the dialog's "Clear" button.
    pub(crate) fn dispatch_quiet_hours_clear(&mut self, name: &str) -> bool {
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&["quiet-hours", name, "--clear"]);
        match outcome {
            SpawnQueuedOutcome::Deduped => return false,
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Quiet hours",
                    &format!("{name}: clear queued — will run after current command."),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                // #2147 trap: the SCHEDULE map only — never
                // `paused_machines`/`quiet_paused_machines`. A window
                // clearing does not un-pause a machine currently inside it;
                // predicting that would flip a still-quiet machine's badge
                // off for one refresh cycle (#2101 "leave the sets alone"
                // precedent — see `mod.rs`'s `quiet_hours_windows` doc
                // comment).
                self.quiet_hours_windows.remove(name);
                self.push_toast(
                    "Quiet hours",
                    &format!("{name}: quiet hours cleared"),
                    ToastSeverity::Info,
                );
            }
        }
        true
    }

    /// #2147: validate + spawn `coord quiet-hours` for the pending dialog's
    /// buffer (`pending_quiet_hours`, taken so a parse error can restore it
    /// verbatim rather than leaving a half-consumed state). Mirrors the
    /// `machine-pause` arm's three-outcome `SpawnQueuedOutcome` match and
    /// toast shape (`dispatch_quiet_hours_clear` above); the only new step
    /// is the client-side shape check before anything reaches
    /// `spawn_queued` at all — a malformed window must never spawn.
    pub(crate) fn submit_quiet_hours(&mut self) {
        let Some(pending) = self.pending_quiet_hours.take() else {
            return;
        };
        let (window, tz) = match parse_quiet_hours_buf(&pending.buf) {
            Ok(parsed) => parsed,
            Err(err) => {
                self.push_toast("Quiet hours", &err, ToastSeverity::Warning);
                // Keep the dialog open, buffer intact, so the operator can
                // fix the typo rather than retyping the whole window.
                self.pending_quiet_hours = Some(pending);
                return;
            }
        };
        let machine = pending.machine;
        let mut args: Vec<&str> = vec!["quiet-hours", &machine, &window];
        if let Some(ref tz) = tz {
            args.push("--tz");
            args.push(tz);
        }
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&args);
        match outcome {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Quiet hours",
                    &format!(
                        "{machine}: quiet hours queued — will run after current command."
                    ),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                // #2147 trap: the SCHEDULE map only — see
                // `dispatch_quiet_hours_clear` above for why
                // `paused_machines`/`quiet_paused_machines` stay untouched.
                // A window set for later tonight does not pause the
                // machine now.
                self.quiet_hours_windows.insert(
                    machine.clone(),
                    data::QuietHoursWindow {
                        start: window.split('-').next().unwrap_or_default().to_string(),
                        end: window.split('-').nth(1).unwrap_or_default().to_string(),
                        tz: tz.unwrap_or_default(),
                        source: "store".to_string(),
                    },
                );
                self.push_toast(
                    "Quiet hours",
                    &format!("{machine}: quiet hours set {window}"),
                    ToastSeverity::Info,
                );
            }
        }
    }
}

/// #2147: parse the "Set quiet hours…" dialog buffer into `(window,
/// Option<tz>)`, or a human-readable error. Accepts `"22:00-08:00"` or
/// `"22:00-08:00 America/Chicago"` — the first whitespace-separated token
/// is the window, everything after (trimmed) is an optional IANA zone.
///
/// Shape-only, mirroring `coord.commands.agent_ops._parse_window_arg`'s
/// documented contract: real HH:MM/IANA-zone validation still happens
/// server-side (`coord.config.parse_quiet_hours_window`, which the CLI
/// answers a 400 for) — this just catches the "operator fat-fingered it"
/// cases so a malformed submit never reaches `spawn_queued` at all (the
/// #2147 acceptance criterion that malformed input keeps the dialog open
/// and spawns nothing).
fn parse_quiet_hours_buf(buf: &str) -> Result<(String, Option<String>), String> {
    let trimmed = buf.trim();
    if trimmed.is_empty() {
        return Err("enter a window, e.g. 22:00-08:00".to_string());
    }
    let mut parts = trimmed.splitn(2, char::is_whitespace);
    let window = parts.next().unwrap_or("").trim();
    let tz = parts.next().map(str::trim).filter(|s| !s.is_empty());

    fn valid_hhmm(s: &str) -> bool {
        let bytes = s.as_bytes();
        bytes.len() == 5
            && bytes[2] == b':'
            && matches!(s[0..2].parse::<u32>(), Ok(h) if h < 24)
            && matches!(s[3..5].parse::<u32>(), Ok(m) if m < 60)
    }

    let halves: Vec<&str> = window.splitn(2, '-').collect();
    if halves.len() != 2 || !valid_hhmm(halves[0]) || !valid_hhmm(halves[1]) {
        return Err(format!("window must look like '22:00-08:00', got {window:?}"));
    }
    Ok((window.to_string(), tz.map(str::to_string)))
}

/// #876 / #1022: Derive a coloured status/verdict badge text from an assignment row.
///
/// #1022: Flat-row rule — each stage owns its own status; the Test verdict is
/// shown on the Test (smoke) row, **not** overlaid on the Work row.
pub(crate) fn assignment_status_badge(a: &Assignment) -> (String, Color) {
    let atype = a.assignment_type.as_deref().unwrap_or("work");
    // Review / re-review: verdict is the primary signal.
    if atype == "review" || atype == "re-review" {
        return match a.review_verdict.as_deref() {
            Some("approve") => ("approve ✓".to_string(), Color::rgb(120, 200, 120)),
            Some("request-changes") => {
                ("request-changes ✗".to_string(), Color::rgb(220, 100, 100))
            }
            _ if a.status == "running" => ("reviewing…".to_string(), Color::rgb(100, 180, 220)),
            _ => ("done".to_string(), Color::rgb(120, 120, 120)),
        };
    }
    // Smoke / test: the session IS the test, so test_state is the primary badge.
    if atype == "smoke" {
        return match a.test_state.as_deref() {
            Some("passed") => ("passed ✓".to_string(), Color::rgb(120, 200, 120)),
            Some("failed") => ("failed ✗".to_string(), Color::rgb(220, 70, 70)),
            Some("skipped") => ("skipped ↷".to_string(), Color::rgb(150, 150, 150)),
            _ if a.status == "running" => ("testing…".to_string(), Color::rgb(100, 180, 220)),
            _ => ("done".to_string(), Color::rgb(120, 120, 120)),
        };
    }
    // Work / fix / plan / conflict-fix: each stage owns its own completion status
    // only.  The test verdict belongs to the Test row and is NOT shown here.
    match a.status.as_str() {
        "running" => ("running…".to_string(), Color::rgb(100, 180, 220)),
        "done" => ("done".to_string(), Color::rgb(120, 120, 120)),
        "failed" => ("failed".to_string(), Color::rgb(220, 70, 70)),
        other => (other.to_string(), Color::rgb(200, 200, 70)),
    }
}

/// #876: `review_findings` is stored server-side as a JSON envelope
/// `{"verdict": ..., "body": ...}` (see `coord/state.py`
/// `_update_assignment_review_findings_local` / `_parse_review_findings_blob`).
/// Extract just the prose `body` for display; mirrors the Python helper so
/// the TUI never renders the raw JSON blob to the operator.
pub(crate) fn extract_review_findings_body(raw: &str) -> Option<String> {
    let value: serde_json::Value = serde_json::from_str(raw).ok()?;
    let body = value.get("body")?.as_str()?;
    if body.trim().is_empty() {
        None
    } else {
        Some(body.to_string())
    }
}

/// #876: Extract the most informative reason string from an assignment.
///
/// Priority:
///   1. `test_reason` — recorded by the operator when marking a test failed
///   2. `review_findings` — the review body for review assignments
///   3. `failure_reason` — short launch-failure explanation
pub(crate) fn board_assignment_reason(a: &Assignment) -> String {
    if let Some(r) = a.test_reason.as_deref() {
        if !r.trim().is_empty() {
            return r.to_string();
        }
    }
    board_assignment_reason_sans_test(a)
}

/// #1022: `board_assignment_reason` minus the `test_reason` branch.
///
/// Used for Work/Fix rows once a dedicated Test (smoke) row exists to carry
/// the test verdict/reason instead — the flat-row rule means each stage owns
/// only the text relevant to itself, and `test_reason` moves to the Test row.
pub(crate) fn board_assignment_reason_sans_test(a: &Assignment) -> String {
    if let Some(r) = a.review_findings.as_deref() {
        if !r.trim().is_empty() {
            // `review_findings` is a JSON envelope, not plain prose — parse
            // out the `body` field. Fall back to the raw string only if it
            // doesn't parse as the expected envelope (defensive; shouldn't
            // happen with server-written data).
            return extract_review_findings_body(r).unwrap_or_else(|| r.to_string());
        }
    }
    if let Some(r) = a.failure_reason.as_deref() {
        if !r.trim().is_empty() {
            return r.to_string();
        }
    }
    String::new()
}

/// #1022: Resolve the Test-gate verdict + reason to show on a Test (`smoke`)
/// row. `test_state`/`test_reason` are recorded on the WORK/FIX assignment
/// (`coord test <work_id> --passed|--fail --reason ...` —
/// `coord/state.py::record_test_verdict`), never on the smoke row itself, so
/// the smoke row's own fields are only a last-resort fallback.
///
/// Preference order:
///   1. The work/fix assignment this smoke row explicitly tested — linked via
///      `review_of_assignment_id`, set by both automated (`coord/smoke.py`)
///      and interactive (`--smoke-of`, `coord/commands/dispatch_workers.py`)
///      dispatch. **When the link is valid but the linked work has not yet
///      been verdicted (test_state is None — i.e. the Test is still running),
///      resolution short-circuits to tier 3 (this smoke row's own status).**
///      An in-progress Test must show `running…`/its own status, NOT borrow an
///      unrelated historical verdict via tier 2 (#1022 smoke-round-2 bug).
///   2. The most-recently-dispatched work-like assignment for the issue that
///      has recorded a verdict (defensive: **legacy rows only** — rows with no
///      `review_of_assignment_id`, or whose linked assignment no longer
///      exists). Never fires when the smoke row carries a valid link.
///   3. The smoke assignment's own `test_state`/`test_reason` — the fallback
///      for a linked-but-not-yet-verdicted (still-running) Test row, and a
///      last resort for legacy rows with no verdict recorded anywhere.
pub(crate) fn resolve_smoke_test_verdict<'a>(
    smoke: &'a Assignment,
    assignments: &[&'a Assignment],
) -> (Option<&'a str>, Option<&'a str>) {
    if let Some(work_id) = smoke.review_of_assignment_id.as_deref() {
        if let Some(w) = assignments.iter().find(|a| a.id == work_id) {
            // The link is valid — this smoke row tested a known assignment.
            // Return that assignment's verdict if one has been recorded;
            // otherwise fall straight through to tier 3 (the smoke row's own
            // status).  A linked-but-unverdicted Test is still in progress and
            // must NOT resurrect an older, unrelated verdict from tier 2.
            if w.test_state.is_some() {
                return (w.test_state.as_deref(), w.test_reason.as_deref());
            }
            return (smoke.test_state.as_deref(), smoke.test_reason.as_deref());
        }
        // The linked id is set but the assignment is no longer present — treat
        // as an orphaned/legacy link and fall through to the defensive scan.
    }
    let latest_work = assignments
        .iter()
        .filter(|a| {
            !matches!(
                a.assignment_type.as_deref().unwrap_or("work"),
                "smoke" | "review" | "re-review"
            )
        })
        .filter(|a| a.test_state.is_some())
        .max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
    if let Some(w) = latest_work {
        return (w.test_state.as_deref(), w.test_reason.as_deref());
    }
    (smoke.test_state.as_deref(), smoke.test_reason.as_deref())
}

/// #1022: Identify WHICH assignment owns the verdict/reason a Test (`smoke`)
/// row displays — the "reason source". This mirrors `resolve_smoke_test_verdict`'s
/// resolution tiers exactly but returns the *source assignment id* instead of
/// the text, so the Summary view can group Test rows that share a reason.
///
/// Why it exists: when an issue has multiple independent fix iterations, each
/// iteration's work/fix assignment records its own distinct `test_reason`. Each
/// of those reasons must stay visible under its own iteration's Test row — the
/// reason must render once *per source*, not once globally. Grouping by this id
/// lets the caller pick the last Test row for each source (so a single commit
/// re-tested N times still shows its shared reason exactly once, while N
/// independent iterations each show their own).
pub(crate) fn smoke_test_source_id<'a>(
    smoke: &'a Assignment,
    assignments: &[&'a Assignment],
) -> &'a str {
    if let Some(work_id) = smoke.review_of_assignment_id.as_deref() {
        if let Some(w) = assignments.iter().find(|a| a.id == work_id) {
            // Valid link with a recorded verdict → that work owns the reason.
            if w.test_state.is_some() {
                return w.id.as_str();
            }
            // Linked but unverdicted (Test still running) → this row owns its
            // own (probably empty) reason; keep it in its own group.
            return smoke.id.as_str();
        }
        // Orphaned link — fall through to the tier-2 defensive scan.
    }
    let latest_work = assignments
        .iter()
        .filter(|a| {
            !matches!(
                a.assignment_type.as_deref().unwrap_or("work"),
                "smoke" | "review" | "re-review"
            )
        })
        .filter(|a| a.test_state.is_some())
        .max_by(|a, b| {
            a.dispatched_at
                .partial_cmp(&b.dispatched_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
    if let Some(w) = latest_work {
        return w.id.as_str();
    }
    smoke.id.as_str()
}

/// #269: Hit-test a click against a TUI tab bar's labels.  Walks the
/// labels left-to-right, accumulating character widths to derive each
/// tab's `start_x..end_x` boundary.  Returns the (absolute) tab index
/// under the cursor or `None` if the click landed past the last tab.
///
/// `scroll_offset` (#605) is the index of the first *visible* tab: the
/// painter skips that many tabs and renders the rest from the left edge
/// (the TUI rasteriser disables scroll arrows, so there is no left-arrow
/// shift). The walk therefore starts accumulating at `origin_x` from tab
/// `scroll_offset`, and the returned index is absolute so it still maps
/// to the right tab. Pass `0` when the bar isn't scrolled.
///
/// Why not `Backend::tab_bar_layout`: that's the rasteriser's authoritative
/// hit-region map but requires a `&mut Backend` we don't want to plumb
/// through every test.  In the TUI rasteriser, labels are rendered 1:1
/// in character cells, so summing label char counts gives the exact
/// same boundaries the painter used.  GTK rendering uses pixel widths
/// and would need the layout call — track in a follow-up if that
/// backend ever has a regression here.
pub(crate) fn hit_tab_index_from_labels(
    labels: &[&str],
    origin_x: f32,
    click_x: f32,
    scroll_offset: usize,
) -> Option<usize> {
    let mut cursor = origin_x;
    for (i, label) in labels.iter().enumerate().skip(scroll_offset) {
        let width = label.chars().count() as f32;
        let end = cursor + width;
        if click_x >= cursor && click_x < end {
            return Some(i);
        }
        cursor = end;
    }
    None
}

/// #2863: build the single-line shell command that launches the ATTENDED
/// (`--interactive`, #2750) intake session for `submission_id`.
///
/// Deliberately the sealed contract's own argv (§4c: `coord portal
/// decompose-chat <submission_id>`) plus the one flag — not a
/// `coord assign --interactive …` flavour like the `InteractiveLaunchMode`
/// builders in `sessions.rs`, because #2750 put `--interactive` on the
/// portal verb itself ("`coord portal decompose-chat` … takes only
/// --machine, so there is no per-dispatch way to ask for anything else").
///
/// `--config` is injected the same way every other interactive launcher in
/// this crate does it (`portal decompose-chat` carries `_CONFIG_OPTION`).
/// The returned line is a single physical line; the trailing `\r` is the
/// submit key, not a line break.
pub(crate) fn build_attended_intake_launch_cmd(
    config_path: Option<&str>,
    submission_id: &str,
) -> String {
    let cfg = match config_path {
        Some(p) if !p.is_empty() => format!("--config {} ", shell_quote_arg(p)),
        _ => String::new(),
    };
    format!(
        "coord portal decompose-chat {}{} --interactive\r",
        cfg,
        shell_quote_arg(submission_id),
    )
}

/// #2863: true when `label` is a headless `coord portal decompose-chat
/// <submission_id>` dispatch — i.e. the command
/// [`CoordApp::dispatch_approved_pull_into_decomposition`] spawned, matched
/// back to its completed `CommandResult` in `run_periodic_work`.
///
/// Matched on the raw label (`spawn_queued` records `"coord " +
/// argv.join(" ")`), so it never fires on an unrelated `portal` subcommand.
/// The attended launcher above is NOT matched and cannot be: it never goes
/// through `command_runner` at all — it is typed into the embedded terminal,
/// where its own stderr is already on screen, unwrapped and in full.
pub(crate) fn is_decompose_chat_dispatch_label(label: &str) -> bool {
    label.contains("portal decompose-chat ")
}

/// #2863: recover the `<submission_id>` positional from such a label, for
/// the failure dialog's headline. `None` when the label has no trailing
/// argument (shouldn't happen — `is_decompose_chat_dispatch_label` requires
/// the trailing space — but this must not panic on a malformed label).
pub(crate) fn decompose_chat_label_submission_id(label: &str) -> Option<String> {
    label
        .split_whitespace()
        .skip_while(|t| *t != "decompose-chat")
        .nth(1)
        .map(str::to_string)
}

/// Union of an optional rect and a required rect.  Used to compute the
/// context-menu viewport so a menu anchored in the sidebar can extend
/// rightward into the main panel without being clipped.
pub(crate) fn union_rects(a: Option<Rect>, b: Rect) -> Rect {
    let Some(a) = a else {
        return b;
    };
    let x = a.x.min(b.x);
    let y = a.y.min(b.y);
    let right = (a.x + a.width).max(b.x + b.width);
    let bottom = (a.y + a.height).max(b.y + b.height);
    Rect::new(x, y, right - x, bottom - y)
}

/// #410: Per-issue status badge for the Board tree.
///
/// Returns the color-coded single-letter badge shown trailing each issue row:
/// - `"R"` (yellow)  — `status:refining` OR `status:ready` (refined-but-not-dispatched)
/// - `"A"` (cyan)    — in-flight / dispatched
/// - `"D"` (dim)     — completed
/// - `None`          — backlog (unrefined, no badge shown)
pub(crate) fn board_row_status_badge(lifecycle: &str) -> Option<Badge> {
    match lifecycle {
        "refining" | "refined" => Some(Badge::colored("R", Color::rgb(220, 180, 50))),
        "in-flight" => Some(Badge::colored("A", Color::rgb(60, 180, 220))),
        "completed" => Some(Badge::colored("D", Color::rgb(100, 100, 110))),
        _ => None, // backlog — blank
    }
}

/// Convert a single coord-tui `ContextMenuItem` into a `quadraui::ContextMenuItem`.
/// Separators (no action_id, no submenu) map to a default item with `id: None`.
/// Parent items (submenu.is_some()) get a synthetic id (`"__parent__<label>"`) so
/// quadraui treats them as selectable; their children are mapped recursively.
/// Leaf actions carry the original `action_id`.
pub(crate) fn coord_item_to_qui(it: &ContextMenuItem) -> QuiContextMenuItem {
    if it.is_separator() {
        QuiContextMenuItem::default()
    } else if let Some(ref children) = it.submenu {
        QuiContextMenuItem {
            id: Some(WidgetId::new(&format!("__parent__{}", it.label))),
            label: StyledText::plain(it.label.clone()),
            disabled: it.disabled,
            submenu: Some(children.iter().map(coord_item_to_qui).collect()),
            ..Default::default()
        }
    } else {
        QuiContextMenuItem {
            id: it.action_id.as_ref().map(WidgetId::new),
            label: StyledText::plain(it.label.clone()),
            // #1598: a disabled reason wins over the plain shortcut hint —
            // an item is never both disabled and mid-action, so there's no
            // case where losing the shortcut display would matter.
            detail: it
                .disabled_reason
                .as_ref()
                .or(it.shortcut.as_ref())
                .map(|s| StyledText::plain(s.clone())),
            disabled: it.disabled,
            ..Default::default()
        }
    }
}

/// Convert an engine-side `ContextMenuState` into the `quadraui::ContextMenu`
/// primitive the rasteriser draws (root level only; submenus are embedded
/// recursively via `coord_item_to_qui`).
pub(crate) fn build_quadraui_context_menu(state: &ContextMenuState) -> ContextMenu {
    let items: Vec<QuiContextMenuItem> = state.items.iter().map(coord_item_to_qui).collect();
    ContextMenu {
        id: WidgetId::new("coord-context-menu"),
        items,
        selected_idx: state.selected_idx,
        bg: None,
        placement: ContextMenuPlacement::AnchorPoint,
    }
}

/// Return the coord-tui items at the given `depth` by walking `state.submenu_path`.
/// Depth 0 = root items.  Returns an empty vec when a path entry is out of bounds
/// or its target has no submenu.
pub(crate) fn items_at_depth(state: &ContextMenuState, depth: usize) -> Vec<ContextMenuItem> {
    let mut items = state.items.clone();
    for &path_idx in state.submenu_path.iter().take(depth) {
        match items.get(path_idx).and_then(|it| it.submenu.clone()) {
            Some(sub) => items = sub,
            None => return Vec::new(),
        }
    }
    items
}

/// Compute a menu-popup width from a slice of coord-tui items:
/// longest label + longest right-aligned hint + 6 cells of padding, clamped
/// to [20, 60].
///
/// #1598: the right-aligned hint is `disabled_reason` when set, else
/// `shortcut` — mirrors [`coord_item_to_qui`]'s own precedence exactly, so a
/// long disabled-reason string (e.g. "outside Done window") can't overlap
/// the label the way a plain shortcut ("r") never grew long enough to.
pub(crate) fn compute_menu_width(items: &[ContextMenuItem]) -> f32 {
    let max_label = items
        .iter()
        .map(|it| it.label.chars().count())
        .max()
        .unwrap_or(4);
    let max_shortcut = items
        .iter()
        .filter_map(|it| it.disabled_reason.as_ref().or(it.shortcut.as_ref()))
        .map(|s| s.chars().count())
        .max()
        .unwrap_or(0);
    // Extra 2 chars for the ▶ affordance on parent items.
    let has_parent = items.iter().any(|it| it.submenu.is_some());
    let extra = if has_parent { 2 } else { 0 };
    (max_label + max_shortcut + extra + 6).clamp(20, 60) as f32
}

/// Build the full stack of `(ContextMenu, ContextMenuLayout)` for the open
/// context menu and all currently-open submenus.  Index 0 = root; index 1 = first
/// open submenu, etc.  Drives both rendering (one `draw_context_menu` call per
/// level) and hit-testing (walk deepest-first).
pub(crate) fn build_context_menu_stack(
    state: &ContextMenuState,
    lh: f32,
    viewport: Rect,
) -> Vec<(ContextMenu, ContextMenuLayout)> {
    let mut stack: Vec<(ContextMenu, ContextMenuLayout)> = Vec::new();

    // ── Root level ────────────────────────────────────────────────────────
    let root_menu = build_quadraui_context_menu(state);
    let root_width = compute_menu_width(&state.items);
    let root_layout = root_menu.layout(
        state.anchor.x,
        state.anchor.y,
        viewport,
        root_width,
        |_| ContextMenuItemMeasure::new(lh),
    );
    stack.push((root_menu, root_layout));

    // ── Open submenu levels ───────────────────────────────────────────────
    let mut current_coord_items: Vec<ContextMenuItem> = state.items.clone();

    for (depth, &path_idx) in state.submenu_path.iter().enumerate() {
        let sub_coord_items = match current_coord_items
            .get(path_idx)
            .and_then(|it| it.submenu.clone())
        {
            Some(items) => items,
            None => break,
        };
        let selected = state.submenu_selected.get(depth).copied().unwrap_or(0);

        let qui_items: Vec<QuiContextMenuItem> = sub_coord_items.iter().map(coord_item_to_qui).collect();
        let sub_menu = ContextMenu {
            id: WidgetId::new("coord-context-submenu"),
            items: qui_items,
            selected_idx: selected,
            bg: None,
            placement: ContextMenuPlacement::AnchorPoint,
        };

        // Pull-right anchor: right border of parent level + 1.
        let parent_bounds = stack[depth].1.bounds;
        let anchor_y = stack[depth]
            .1
            .visible_items
            .iter()
            .find(|v| v.item_idx == path_idx)
            .map(|v| v.bounds.y)
            .unwrap_or(parent_bounds.y);

        let sub_width = compute_menu_width(&sub_coord_items);
        let preferred_x = parent_bounds.x + parent_bounds.width + 1.0;
        let flipped_x = parent_bounds.x - sub_width - 1.0;
        let anchor_x = if preferred_x + sub_width <= viewport.x + viewport.width {
            preferred_x
        } else if flipped_x >= viewport.x {
            flipped_x
        } else {
            (viewport.x + viewport.width - sub_width).max(viewport.x)
        };

        let sub_layout =
            sub_menu.layout(anchor_x, anchor_y, viewport, sub_width, |_| {
                ContextMenuItemMeasure::new(lh)
            });
        stack.push((sub_menu, sub_layout));
        current_coord_items = sub_coord_items;
    }

    stack
}

