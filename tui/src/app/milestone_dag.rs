//! Milestone work-order DAG/lane view — Phase 3 of #767 (#771).
//!
//! **Design note (client-side, zero daemon changes):** the `## Work order`
//! block (#768's convention) lives in the milestone's *tracking issue* body
//! — the issue carrying the `"epic"` label, mirroring
//! `coord.milestone_order.TRACKING_ISSUE_LABEL`. That body is already synced
//! into `OpenIssue.body` (#406 plumbing), so the whole DAG + node states can
//! be computed **client-side** from data the TUI already has — no new daemon
//! endpoint, no extra `gh`/network round trip. The pure functions below are
//! a lenient Rust port of `coord/milestone_order.py`'s `parse_work_order` /
//! `ready_frontier` grammar and semantics (lenient: malformed lines are
//! skipped rather than raising — this is a read-only display, and
//! `coord milestone order`/`write-order` remain the validating authority).
//!
//! "Dispatch milestone" reuses the already-shipped `coord milestone dispatch
//! <repo> <tracking_issue>` CLI (#769, Phase 1) via the same
//! spawn-a-subprocess pattern every other TUI action uses
//! (`CommandRunner::spawn_queued`) — no bespoke plumbing needed there either.
#[allow(unused_imports)]
use super::*;

/// Mirrors `coord.milestone_order.TRACKING_ISSUE_LABEL`.
pub(crate) const TRACKING_ISSUE_LABEL: &str = "epic";

/// One `- [ ] #N {group: A, after: #1,#2}` line from a `## Work order` block.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct WorkOrderNode {
    pub(crate) issue_number: u64,
    pub(crate) group: Option<String>,
    pub(crate) after: Vec<u64>,
}

/// A work-order node's dispatch/pipeline state, computed against the
/// current board (open/closed issue state + active assignments).
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum NodeState {
    /// Issue closed — this node has reached a terminal state.
    Done,
    /// Any **workable** assignment (work, review, smoke, conflict-fix, fix-*)
    /// exists for this issue — regardless of whether it is currently running.
    /// An open issue whose work has completed but is still awaiting
    /// Test/Review/Merge (no live session, but with a settled work row) lands
    /// here rather than `Ready`.  `refinement`, `chat`, `new-issue-chat`, and
    /// `test-chat` assignments do NOT promote a node out of `Ready` — the same
    /// `is_workable_type` predicate used by the Pipeline lifecycle classifier.
    InFlight,
    /// Not done, not in flight, and at least one `after` dependency hasn't
    /// reached `Done` yet. Carries the still-unmet dependency issue numbers.
    Blocked(Vec<u64>),
    /// Not done, not in flight, all `after` dependencies are `Done` — part
    /// of the ready frontier `coord milestone dispatch` would pick up now.
    Ready,
}

/// A work-order node plus its computed display state and issue title.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct MilestoneDagNode {
    pub(crate) issue_number: u64,
    pub(crate) title: String,
    pub(crate) group: Option<String>,
    pub(crate) after: Vec<u64>,
    pub(crate) state: NodeState,
}

/// One milestone's parsed + state-annotated work order, ready to render.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct MilestoneDagView {
    pub(crate) repo_name: String,
    pub(crate) milestone_number: i64,
    pub(crate) milestone_title: String,
    /// Issue number of the "epic"-labelled tracking issue whose body carries
    /// the `## Work order` block — what `coord milestone dispatch` takes.
    pub(crate) tracking_issue: u64,
    pub(crate) nodes: Vec<MilestoneDagNode>,
}

// ─── #1003: Plans-panel / MilestoneDag row CRUD pending state ────────────────

/// Which single-field prompt [`PendingMilestoneRowInput`] is currently
/// collecting.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum MilestoneRowInputKind {
    /// "Edit milestone…" — pre-filled with the current title; submitting
    /// calls `coord milestone edit <repo> <number> --title <buf>`. Title-only
    /// for now (description/due are the fuller multi-field / chat-driven
    /// edit path #1003 defers to a follow-up).
    EditTitle,
    /// "Add issue to milestone…" — an issue number; submitting calls
    /// `coord milestone assign <repo> <buf> <milestone_number>`.
    AddIssue,
    /// "Remove issue from milestone…" — an issue number; submitting calls
    /// `coord milestone remove <repo> <buf>`.
    RemoveIssue,
    /// "Add sub-issue to epic…" (#1008) — an issue number, optionally
    /// followed by a `{group: G, after: #N,...}` annotation (same grammar
    /// as a `## Sub-issues` / `## Work order` checklist line, e.g. `1050`
    /// or `1050 {group: B, after: #1049}`); submitting calls `coord
    /// milestone add-child <repo> <tracking_issue> <issue> [--group G]
    /// [--after N,...]`. Parsed by `submit_add_sub_issue_input` rather than
    /// the shared bare-issue-number path the other two kinds use, since the
    /// buffer may carry more than just a number.
    AddSubIssue,
    /// "Add sub-issue via chat…" (#1017, #1029) — a bare candidate issue
    /// number (no `{group/after}` annotation here; that gets discussed live
    /// in the chat instead of parsed client-side). Submitting launches (or
    /// reattaches to) a genuine tmux-attached interactive milestone chat via
    /// `launch_milestone_chat_session`, seeding a `type="milestone-chat"`
    /// session with the epic body plus the candidate issue
    /// (`resolve_milestone_chat_briefing`'s `candidate_child_issue` seed
    /// mode). Parsed by `submit_add_sub_issue_chat_input`.
    AddSubIssueChat,
}

/// #1003: pending single-field text input for a Plans-panel / MilestoneDag
/// row action. Mirrors the #977 `pending_plan_capture` single-buffer
/// pattern (one `String` buf, Enter submits, Esc cancels) — quick and
/// keyboard-only rather than a full multi-field form.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PendingMilestoneRowInput {
    pub(crate) kind: MilestoneRowInputKind,
    pub(crate) repo_name: String,
    pub(crate) tracking_issue: u64,
    pub(crate) milestone_number: i64,
    pub(crate) milestone_title: String,
    pub(crate) buf: String,
}

/// #1003: pending "Close / archive plan" confirmation — closes the
/// milestone's tracking issue via `coord issue close`. Mirrors the
/// `pending_restart` yes/any-other-key-cancels pattern.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PendingClosePlan {
    pub(crate) repo_name: String,
    pub(crate) tracking_issue: u64,
    pub(crate) milestone_title: String,
}

// ─── Parsing (pure, mirrors coord/milestone_order.py's grammar) ──────────────

/// Parse the `## Work order` block out of a tracking issue's markdown body.
///
/// Grammar (mirrors `_HEADING_RE` / `_ITEM_RE` in `coord/milestone_order.py`):
/// a line matching `^#{1,6}\s*Work order\s*$` (case-insensitive) starts the
/// block; each following `- [ ] #N  {group: A, after: #1,#2}` line (checkbox
/// state ignored — cosmetic only, per the Python module) becomes a node;
/// a line starting with `#` ends the block (next markdown heading). Lines
/// that don't match the checklist-item shape are skipped rather than
/// raising — the TUI is a read-only consumer; `coord milestone order`/
/// `write-order` own validation (duplicate numbers, cycles, unknown keys).
pub(crate) fn parse_work_order(body: &str) -> Vec<WorkOrderNode> {
    let lines: Vec<&str> = body.lines().collect();
    let mut start: Option<usize> = None;
    for (i, line) in lines.iter().enumerate() {
        if is_work_order_heading(line.trim()) {
            start = Some(i + 1);
            break;
        }
    }
    let Some(start) = start else {
        return Vec::new();
    };

    let mut nodes = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for line in &lines[start..] {
        let stripped = line.trim();
        if stripped.is_empty() {
            continue;
        }
        if stripped.starts_with('#') {
            break; // next markdown heading — block ended
        }
        if let Some(node) = parse_item_line(stripped) {
            // First declaration wins on a duplicate; `coord milestone order`
            // is the authority that rejects duplicates outright.
            if seen.insert(node.issue_number) {
                nodes.push(node);
            }
        }
    }
    nodes
}

fn is_work_order_heading(line: &str) -> bool {
    if !line.starts_with('#') {
        return false;
    }
    line.trim_start_matches('#').trim().eq_ignore_ascii_case("Work order")
}

/// Parse one checklist line: `- [ ] #762  {group: A, after: #1,#2}`.
/// Returns `None` for anything that doesn't match the `- [ ] #N` shape.
fn parse_item_line(line: &str) -> Option<WorkOrderNode> {
    let rest = line.strip_prefix('-')?.trim_start();
    let rest = rest.strip_prefix('[')?;
    let (_checkbox, rest) = rest.split_once(']')?;
    let rest = rest.trim_start().strip_prefix('#')?;
    let digits_end = rest
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(rest.len());
    if digits_end == 0 {
        return None;
    }
    let issue_number: u64 = rest[..digits_end].parse().ok()?;
    let tail = rest[digits_end..].trim_start();

    let mut group = None;
    let mut after = Vec::new();
    if let Some(brace_rest) = tail.strip_prefix('{') {
        if let Some((inside, _)) = brace_rest.split_once('}') {
            let (g, a) = parse_annotations(inside);
            group = g;
            after = a;
        }
    }
    Some(WorkOrderNode { issue_number, group, after })
}

/// Parse the `{group: A, after: #1,#2}` annotation body. Only two keys are
/// defined (mirrors `_parse_annotation` in the Python module); this scans
/// for each key's word-boundary position so either order (`group` before
/// `after` or vice versa) parses correctly without needing a full
/// comma-splitting grammar (the `after` value is itself comma-separated).
fn parse_annotations(inside: &str) -> (Option<String>, Vec<u64>) {
    let group_pos = find_key(inside, "group");
    let after_pos = find_key(inside, "after");

    let group = group_pos.map(|gp| {
        let value_start = gp + "group".len();
        let value_end = after_pos.filter(|&ap| ap > gp).unwrap_or(inside.len());
        inside[value_start..value_end]
            .trim_start_matches(':')
            .trim()
            .trim_end_matches(',')
            .trim()
            .to_string()
    });

    let after = after_pos
        .map(|ap| {
            let value_start = ap + "after".len();
            let value_end = group_pos.filter(|&gp| gp > ap).unwrap_or(inside.len());
            let raw = inside[value_start..value_end]
                .trim_start_matches(':')
                .trim()
                .trim_end_matches(',')
                .trim();
            raw.split(',')
                .filter_map(|chunk| chunk.trim().trim_start_matches('#').parse::<u64>().ok())
                .collect()
        })
        .unwrap_or_default();

    (group.filter(|g| !g.is_empty()), after)
}

/// Find the start index of `key` in `s` when followed (after optional
/// whitespace) by `:`, and not itself preceded by an alphanumeric char
/// (crude word-boundary check — good enough for the two-key annotation
/// grammar; a full lexer/regex crate is overkill here).
fn find_key(s: &str, key: &str) -> Option<usize> {
    let bytes = s.as_bytes();
    let mut idx = 0;
    while idx < s.len() {
        let Some(rel) = s[idx..].find(key) else { return None };
        let abs = idx + rel;
        let pre_ok = abs == 0 || !bytes[abs - 1].is_ascii_alphanumeric();
        let mut j = abs + key.len();
        while j < s.len() && bytes[j] == b' ' {
            j += 1;
        }
        let post_ok = j < s.len() && bytes[j] == b':';
        if pre_ok && post_ok {
            return Some(abs);
        }
        idx = abs + key.len();
    }
    None
}

// ─── Tracking-issue discovery + DAG/state computation ────────────────────────

/// Case-insensitive check for the `"epic"` tracking-issue label against an
/// arbitrary label list (`OpenIssue::labels`, `PipelineIssue::all_labels`, …).
///
/// #1198: the single source of truth for "is this issue an epic" — unifies
/// what used to be three independent checks: this module's own
/// `eq_ignore_ascii_case`, plus case-sensitive `== "epic"` exact matches in
/// `dialogs.rs`'s context-menu gate and `render.rs`'s Gate A guidance rows.
/// An `Epic`/`EPIC`-cased label previously behaved correctly here but lost
/// its context-menu actions and detail-pane rows — callers should go through
/// this helper instead of re-deriving the comparison.
pub(crate) fn labels_carry_epic_label(labels: &[String]) -> bool {
    labels
        .iter()
        .any(|l| l.eq_ignore_ascii_case(TRACKING_ISSUE_LABEL))
}

/// Mirrors `coord.commands.issues.track`'s `"coord"` label — the label that
/// marks an issue as Pipeline-tracked (`coord/dispatch.py`'s
/// `label_change_for_subcommand("track")` adds it, `board_row_lifecycle`
/// reads it back).
pub(crate) const COORD_LABEL: &str = "coord";

/// Case-insensitive check for [`COORD_LABEL`] against an arbitrary label
/// list, mirroring `labels_carry_epic_label`'s convention. #2494: an epic
/// tracking issue created via `coord milestone capture`/`chat` never carries
/// this label until something runs `coord track` against it — used to gate
/// the Plans-panel epic-row menu's "Send to Pipeline" item so it only
/// appears while the epic is still untracked.
pub(crate) fn labels_carry_coord_label(labels: &[String]) -> bool {
    labels.iter().any(|l| l.eq_ignore_ascii_case(COORD_LABEL))
}

/// The open issue in `repo_name`/`milestone_number` carrying the `"epic"`
/// label — the milestone's tracking issue (mirrors
/// `coord.milestone_order.TRACKING_ISSUE_LABEL` / the #645 convention).
pub(crate) fn milestone_tracking_issue<'a>(
    open_issues: &'a [OpenIssue],
    repo_name: &str,
    milestone_number: i64,
) -> Option<&'a OpenIssue> {
    open_issues.iter().find(|oi| {
        oi.repo_name == repo_name
            && oi.milestone_number == Some(milestone_number)
            && labels_carry_epic_label(&oi.labels)
    })
}

/// Compute each node's [`NodeState`] against the current board: issue
/// `state`/assignments for done/in-flight, `after`-edges vs. the terminal
/// (closed) set for blocked/ready. Terminal is scoped to `repo_name` so two
/// repos can't cross-pollinate issue numbers.
///
/// **A work-order issue absent from the local open-issues cache counts as
/// terminal (done), not unknown** (#771 review finding). `coord/state.py`'s
/// `_upsert_open_issues_local` only ever prunes a row for being *closed* and
/// stale (`synced_at` older than 7 days) — a still-open issue is refreshed on
/// every sync and never ages out. So "missing entirely" is strong evidence of
/// "closed a while back," not genuine ambiguity; treating it as `Unknown`
/// (the previous behavior) made the "done" badge effectively never render for
/// realistic milestones (issues closed more than ~a week ago, whose
/// `synced_at` froze at their last *open* sync, had already aged out) and
/// made dependent nodes show as incorrectly `Blocked` on a dependency that
/// was actually done.
pub(crate) fn build_dag_nodes(
    work_order: &[WorkOrderNode],
    repo_name: &str,
    open_issues: &[OpenIssue],
    assignments: &[Assignment],
) -> Vec<MilestoneDagNode> {
    let terminal: std::collections::HashSet<u64> = work_order
        .iter()
        .filter(|n| {
            match open_issues
                .iter()
                .find(|oi| oi.repo_name == repo_name && oi.number == n.issue_number)
            {
                Some(oi) => oi.state == "closed",
                None => true, // aged out of the sync cache ⇒ presumed closed/done.
            }
        })
        .map(|n| n.issue_number)
        .collect();

    work_order
        .iter()
        .map(|n| {
            let found = open_issues
                .iter()
                .find(|oi| oi.repo_name == repo_name && oi.number == n.issue_number);
            let state = if terminal.contains(&n.issue_number) {
                NodeState::Done
            } else if assignments.iter().any(|a| {
                // #1287: widen InFlight from "has a *running* assignment" to "has
                // ANY workable assignment" — a child whose work has completed but
                // whose issue is still open (Test/Review/Merge pending, no live
                // session) must not fall back to Ready.  Scoping conversations
                // (refinement, chat, new-issue-chat, test-chat) are NOT pipeline
                // execution and do NOT promote a node out of Ready — same
                // is_workable_type predicate as the Pipeline lifecycle classifier.
                // None assignment_type defaults to true (treated as "work").
                // #1553: `effective_issue_number` so a member node whose
                // acceptance slice is running reads InFlight. The slice row
                // carries the milestone's TRACKING issue in `issue_number`,
                // so the raw match would have promoted the epic (never a DAG
                // node) instead of the child it was authored for.
                a.repo == repo_name
                    && a.effective_issue_number() == n.issue_number
                    && a.assignment_type.as_deref().map(is_workable_type).unwrap_or(true)
            }) {
                NodeState::InFlight
            } else {
                let unmet: Vec<u64> = n
                    .after
                    .iter()
                    .copied()
                    .filter(|d| !terminal.contains(d))
                    .collect();
                if unmet.is_empty() {
                    NodeState::Ready
                } else {
                    NodeState::Blocked(unmet)
                }
            };
            MilestoneDagNode {
                issue_number: n.issue_number,
                title: found
                    .map(|oi| oi.title.clone())
                    .unwrap_or_else(|| format!("#{}", n.issue_number)),
                group: n.group.clone(),
                after: n.after.clone(),
                state,
            }
        })
        .collect()
}

/// Group nodes by `group` cohort, preserving first-seen order (ungrouped
/// nodes form their own bucket, wherever they first appear).
pub(crate) fn nodes_by_cohort(
    nodes: &[MilestoneDagNode],
) -> Vec<(Option<String>, Vec<&MilestoneDagNode>)> {
    let mut order: Vec<Option<String>> = Vec::new();
    let mut map: std::collections::HashMap<Option<String>, Vec<&MilestoneDagNode>> =
        std::collections::HashMap::new();
    for n in nodes {
        let key = n.group.clone();
        if !map.contains_key(&key) {
            order.push(key.clone());
        }
        map.entry(key).or_default().push(n);
    }
    order
        .into_iter()
        .map(|k| {
            let v = map.remove(&k).unwrap_or_default();
            (k, v)
        })
        .collect()
}

/// Every milestone (any repo) whose tracking issue carries a non-empty
/// `## Work order` block, sorted by `(repo_name, milestone_number)`.
pub(crate) fn milestones_with_work_orders(
    open_issues: &[OpenIssue],
    assignments: &[Assignment],
) -> Vec<MilestoneDagView> {
    let mut seen: std::collections::BTreeSet<(String, i64)> = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    for oi in open_issues {
        let Some(mn) = oi.milestone_number else { continue };
        let key = (oi.repo_name.clone(), mn);
        if !seen.insert(key) {
            continue;
        }
        let Some(tracking) = milestone_tracking_issue(open_issues, &oi.repo_name, mn) else {
            continue;
        };
        let work_order = parse_work_order(&tracking.body);
        if work_order.is_empty() {
            continue;
        }
        let nodes = build_dag_nodes(&work_order, &oi.repo_name, open_issues, assignments);
        out.push(MilestoneDagView {
            repo_name: oi.repo_name.clone(),
            milestone_number: mn,
            milestone_title: tracking.milestone_title.clone().unwrap_or_default(),
            tracking_issue: tracking.number,
            nodes,
        });
    }
    out.sort_by(|a, b| {
        (a.repo_name.as_str(), a.milestone_number).cmp(&(b.repo_name.as_str(), b.milestone_number))
    });
    out
}

fn milestone_node_badge(state: &NodeState, theme: &quadraui::Theme) -> (&'static str, Color) {
    match state {
        NodeState::Done => ("done", theme.badge_passed),
        NodeState::InFlight => ("in-flight", theme.link_fg),
        NodeState::Ready => ("ready", theme.accent_fg),
        NodeState::Blocked(_) => ("blocked", theme.badge_warning),
    }
}

// ─── impl CoordApp — sidebar/main-panel rendering + actions ──────────────────

impl CoordApp {
    /// Every milestone-with-work-order across all synced repos, in stable
    /// `(repo_name, milestone_number)` order. Recomputed on demand (cheap —
    /// linear scan + string parse over the already-in-memory open-issues
    /// cache) rather than cached, so it never goes stale after a refresh.
    pub(crate) fn milestone_dag_views(&self) -> Vec<MilestoneDagView> {
        milestones_with_work_orders(&self.data.open_issues, &self.data.assignments)
    }

    /// The currently-selected milestone (`milestone_dag_sel`, clamped), or
    /// `None` when no milestone has a parsed work order.
    pub(crate) fn milestone_dag_selected(&self) -> Option<MilestoneDagView> {
        let views = self.milestone_dag_views();
        if views.is_empty() {
            return None;
        }
        let idx = self.milestone_dag_sel.min(views.len() - 1);
        views.into_iter().nth(idx)
    }

    /// Sidebar placeholder for the Milestone DAG view — all content lives in
    /// the main panel (mirrors `merge_queue_sidebar`'s "count + hint" shape).
    pub(crate) fn milestone_dag_sidebar(&self) -> ListView {
        let n = self.milestone_dag_views().len();
        let hint = format!(
            "  {} milestone{} with a work order",
            n,
            if n == 1 { "" } else { "s" }
        );
        ListView {
            id: WidgetId::new("milestonedag-sidebar"),
            title: Some(StyledText::plain(" MILESTONES ")),
            items: vec![activity_item(&hint, Color::rgb(160, 160, 160))],
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Render the Milestone DAG main panel: every milestone-with-work-order
    /// stacked as a header row (repo — title (#tracking) + the "Dispatch
    /// milestone [d]" hint) followed by its nodes grouped into cohort rows
    /// (`group A` / `group B` / ungrouped), each node showing its state
    /// badge and — when blocked — which issue(s) it's waiting on. The
    /// currently-selected milestone's header is highlighted via
    /// `selected_idx` so `d`/right-click "Dispatch milestone" has a visible
    /// target.
    pub(crate) fn render_milestone_dag_panel(&self, backend: &mut dyn Backend, rect: Rect, _lh: f32) {
        let views = self.milestone_dag_views();
        if views.is_empty() {
            backend.draw_list(
                rect,
                &plain_list(
                    "milestonedag-empty",
                    "  No milestone tracking issue with a `## Work order` block found.",
                    0,
                ),
            );
            return;
        }
        let sel = self.milestone_dag_sel.min(views.len() - 1);
        let theme = &self.active_theme;
        let mut items: Vec<ListItem> = Vec::new();
        let mut selected_idx = 0usize;

        for (vi, view) in views.iter().enumerate() {
            if vi == sel {
                selected_idx = items.len();
            }
            let header_label = format!(
                " {} — {} (#{})  ·  Dispatch milestone [d] ",
                view.repo_name, view.milestone_title, view.tracking_issue
            );
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(header_label, Color::rgb(150, 190, 230))],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });

            for (group, nodes) in nodes_by_cohort(&view.nodes) {
                let group_label = match &group {
                    Some(g) => format!("  cohort: {}", g),
                    None => "  ungrouped".to_string(),
                };
                items.push(activity_item(&group_label, Color::rgb(140, 140, 140)));

                for node in nodes {
                    let (state_text, color) = milestone_node_badge(&node.state, theme);
                    let mut spans = vec![
                        StyledSpan::with_fg(
                            format!("    #{:<5} {}", node.issue_number, trunc(&node.title, 40)),
                            Color::rgb(200, 200, 200),
                        ),
                        StyledSpan::with_fg(format!("  [{}]", state_text), color),
                    ];
                    if let NodeState::Blocked(on) = &node.state {
                        let deps = on
                            .iter()
                            .map(|n| format!("#{}", n))
                            .collect::<Vec<_>>()
                            .join(", ");
                        spans.push(StyledSpan::with_fg(
                            format!("  blocked on {}", deps),
                            Color::rgb(200, 140, 60),
                        ));
                    }
                    items.push(ListItem {
                        text: StyledText { spans },
                        icon: None,
                        detail: None,
                        decoration: Decoration::Normal,
                    });
                }
            }
        }

        let total = items.len();
        backend.draw_list(
            rect,
            &ListView {
                id: WidgetId::new("milestonedag-list"),
                title: Some(StyledText::plain(" MILESTONE WORK ORDER ")),
                items,
                selected_idx,
                scroll_offset: 0,
                has_focus: true,
                bordered: true,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: total > 10,
            },
        );
    }

    /// Context-menu items for a milestone header row (right-click, or the
    /// keyboard shortcut / Menu key on the selected header).
    ///
    /// #1003: extended from the original "Dispatch milestone" + Refresh pair
    /// to the full row-level CRUD set the Plans panel now shares this target
    /// with — chat, next-pick, read-only order view, edit/add/remove, and
    /// close/archive. `_milestone_number` isn't read here (the item list
    /// doesn't depend on it — only the id matters); it's accepted for
    /// symmetry with the target's fields and because a future item may need
    /// it for a disabled-state check.
    ///
    /// #2494: `labels` is the epic tracking issue's current GitHub labels
    /// (looked up by the caller from `data.open_issues`) — used solely to
    /// gate "Send to Pipeline": an epic created via `coord milestone
    /// capture`/`chat` carries neither `coord` nor `status:ready` until
    /// something runs `coord track` against it, and until then it never
    /// appears in Pipeline at all, so every Gate A action stays unreachable.
    /// Shown only while the epic is still untracked (mirrors the Board row
    /// menu's own Backlog-only gate on the same action).
    pub(crate) fn context_menu_items_for_milestone_header(
        &self,
        _repo_name: &str,
        _tracking_issue: u64,
        _milestone_title: &str,
        _milestone_number: i64,
        labels: &[String],
    ) -> Vec<ContextMenuItem> {
        let mut items = vec![
            ContextMenuItem::action("open-milestone-chat", "Open milestone chat"),
            ContextMenuItem::action("dispatch-milestone", "Dispatch milestone").with_shortcut("d"),
            ContextMenuItem::action("dispatch-milestone-next", "Dispatch next…"),
            ContextMenuItem::action("view-milestone-order", "View order / DAG"),
        ];
        if !labels_carry_coord_label(labels) {
            items.push(ContextMenuItem::action(
                "send-to-pipeline",
                "Send to Pipeline",
            ));
        }
        items.push(ContextMenuItem::separator());
        items.extend([
            ContextMenuItem::action("edit-milestone", "Edit milestone…"),
            ContextMenuItem::action("add-issue-to-milestone", "Add issue to milestone…"),
            ContextMenuItem::action(
                "remove-issue-from-milestone",
                "Remove issue from milestone…",
            ),
            // #1008: distinct from "Add issue to milestone…" above — this
            // splices the epic tracking issue's own `## Sub-issues`
            // checklist (`coord milestone add-child`) rather than assigning
            // GitHub milestone membership (`coord milestone assign`).
            ContextMenuItem::action("add-sub-issue-to-epic", "Add sub-issue to epic…"),
            // #1017: chat-driven alternative to the dialog above — discusses
            // the candidate + its `{group/after}` annotation with a
            // milestone-chat steward instead of typing it into the quick
            // dialog buffer.
            ContextMenuItem::action("add-sub-issue-to-epic-chat", "Add sub-issue via chat…"),
            ContextMenuItem::separator(),
            ContextMenuItem::action("close-plan", "Close / archive plan"),
            ContextMenuItem::separator(),
            ContextMenuItem::action("refresh", "Refresh").with_shortcut("r"),
        ]);
        items
    }

    /// #1123 (contract §4b/§4c): context-menu items for a Plans-panel
    /// right-click that has no tracking epic to act on yet — either a
    /// specific epic-less milestone row (`milestone: Some`, §4b) or the
    /// repo-header row / empty main-panel space (`milestone: None`, §4c).
    /// Distinct from [`Self::context_menu_items_for_milestone_header`]'s
    /// eleven-item CRUD menu, which requires a `tracking_issue` to build
    /// `coord milestone dispatch`/`chat`/`edit`/... calls against.
    ///
    /// `repo_name` is accepted (not yet read) for the day
    /// `promote-milestone-to-epic` needs to pass `--repo` explicitly rather
    /// than relying on `capture_plan_chat`'s own selection-based lookup —
    /// see `dispatch_context_menu_action`.
    pub(crate) fn context_menu_items_for_plans_stub(
        &self,
        _repo_name: Option<&str>,
        milestone: Option<&(i64, String)>,
    ) -> Vec<ContextMenuItem> {
        if milestone.is_some() {
            // §4b: a specific epic-less milestone row under the cursor —
            // offer to bootstrap it into a real tracking epic. The action
            // itself is deliberately unpinned by the contract ("TBD — new
            // action"); see `dispatch_context_menu_action` for the chosen
            // implementation (routes to the guided-chat flow).
            vec![
                ContextMenuItem::action(
                    "promote-milestone-to-epic",
                    "Create work order / promote to epic…",
                ),
                ContextMenuItem::separator(),
                ContextMenuItem::action("refresh", "Refresh").with_shortcut("r"),
            ]
        } else {
            // §4c: repo-header row or empty main-panel space — the
            // discoverable replacement for the cryptic `c`/`C` bare-key
            // bindings, with the accelerators surfaced inside the menu
            // (contract §4e) instead.
            vec![
                ContextMenuItem::action("capture-plan-quick", "New plan > Quick capture")
                    .with_shortcut("c"),
                ContextMenuItem::action("capture-plan-chat", "New plan > Guided chat…")
                    .with_shortcut("C"),
                ContextMenuItem::separator(),
                ContextMenuItem::action("refresh", "Refresh").with_shortcut("r"),
            ]
        }
    }

    /// Promote the target milestone's whole declared work order into the
    /// pipeline: `coord milestone dispatch <repo> <tracking_issue>` (#769,
    /// Phase 1 — already shipped; this just spawns it the same way every
    /// other TUI action spawns a `coord` subcommand).
    pub(crate) fn dispatch_milestone_action(&mut self, target: &ContextMenuTarget) -> bool {
        let (repo, tracking_issue, title) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                milestone_title,
                ..
            } => (repo_name.clone(), *tracking_issue, milestone_title.clone()),
            _ => {
                self.push_toast(
                    "Dispatch milestone unavailable",
                    "No milestone target — focus a milestone header first.",
                    ToastSeverity::Info,
                );
                return false;
            }
        };
        self.spawn_milestone_dispatch_command(&repo, tracking_issue, &title)
    }

    /// #2645: the reusable core of [`Self::dispatch_milestone_action`] —
    /// spawns `coord milestone dispatch <repo> <tracking_issue>` and toasts
    /// the outcome. Split out so a Pipeline-row epic right-click (which
    /// carries a `ContextMenuTarget::PipelineRow`, not `MilestoneHeader`)
    /// can drive the exact same seam `dialogs.rs`'s "Drive (automated)" item
    /// re-points at for epic rows, instead of the mis-wired
    /// `drive-queue add <repo> <epic_number>` it used to fall through to.
    pub(crate) fn spawn_milestone_dispatch_command(
        &mut self,
        repo: &str,
        tracking_issue: u64,
        title: &str,
    ) -> bool {
        let issue_str = tracking_issue.to_string();
        use crate::commands::SpawnQueuedOutcome;
        let outcome =
            self.command_runner
                .spawn_queued(&["milestone", "dispatch", repo, &issue_str]);
        match outcome {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Dispatch milestone",
                    &format!("{}: queued — will run after current command.", title),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Dispatch milestone",
                    &format!("{}: dispatching ready frontier…", title),
                    ToastSeverity::Info,
                );
            }
        }
        true
    }

    /// "Open milestone chat" (#1003, #1017, #1029) — launch (or reattach to)
    /// a genuine tmux-attached interactive `claude` session via
    /// [`Self::launch_milestone_chat_session`]. #1029 replaced the headless
    /// `claude -p` / SSE-chat-overlay mechanism #1017 introduced — that
    /// overlay never actually cleared its "worker exited" banner, locked out
    /// board/tab navigation while open, and could spawn a duplicate session
    /// on reopen. The tmux path fixes all of that by construction: the
    /// terminal itself is the truth, reopening reattaches to the same
    /// session, and there's no bespoke modal held open over the whole screen.
    pub(crate) fn open_milestone_chat_action(&mut self, target: &ContextMenuTarget) -> bool {
        let (repo, tracking_issue) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                ..
            } => (repo_name.clone(), *tracking_issue),
            _ => {
                self.push_toast(
                    "Open milestone chat",
                    "No milestone target — focus a milestone header first.",
                    ToastSeverity::Info,
                );
                return false;
            }
        };
        self.launch_milestone_chat_session(repo, tracking_issue, None);
        true
    }

    /// "Dispatch next…" (#1003) — lighter-weight than "Dispatch milestone"
    /// (which drains the whole current ready frontier): computes the ready
    /// frontier **client-side** (the same `NodeState::Ready` computation the
    /// MilestoneDag panel already renders, mirroring
    /// `coord/milestone_order.py`'s grammar/semantics) and dispatches the
    /// first ready item non-interactively via `--next --pick
    /// <issue_number>` (a #1003 CLI addition — `coord milestone dispatch
    /// --next` on its own drops into an interactive `click.prompt`, which
    /// `spawn_queued`'s non-TTY subprocess can never answer). Machine
    /// availability/capability is still resolved server-side; a
    /// ready-but-unschedulable issue surfaces as an ordinary
    /// command-failure toast rather than being silently skipped.
    pub(crate) fn dispatch_milestone_next_action(&mut self, target: &ContextMenuTarget) -> bool {
        let (repo, tracking_issue, title) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                milestone_title,
                ..
            } => (repo_name.clone(), *tracking_issue, milestone_title.clone()),
            _ => {
                self.push_toast(
                    "Dispatch next",
                    "No milestone target — focus a milestone header first.",
                    ToastSeverity::Info,
                );
                return false;
            }
        };
        let next_issue = self
            .milestone_dag_views()
            .into_iter()
            .find(|v| v.repo_name == repo && v.tracking_issue == tracking_issue)
            .and_then(|v| {
                v.nodes
                    .into_iter()
                    .find(|n| n.state == NodeState::Ready)
                    .map(|n| n.issue_number)
            });
        let Some(issue_number) = next_issue else {
            self.push_toast(
                "Dispatch next",
                &format!("{}: no ready frontier item right now.", title),
                ToastSeverity::Info,
            );
            return false;
        };
        let issue_str = tracking_issue.to_string();
        let pick_str = issue_number.to_string();
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&[
            "milestone",
            "dispatch",
            &repo,
            &issue_str,
            "--next",
            "--pick",
            &pick_str,
        ]);
        match outcome {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Dispatch next",
                    &format!("#{} ({}): queued — will run after current command.", issue_number, title),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Dispatch next",
                    &format!("#{} ({}): dispatching…", issue_number, title),
                    ToastSeverity::Info,
                );
            }
        }
        true
    }

    /// "View order / DAG" (#1003) — switches to the already-shipped
    /// MilestoneDag view and selects this milestone, rather than shelling
    /// out to the read-only `coord milestone order` CLI: the DAG view
    /// already computes the identical ready/blocked/in-flight/done state
    /// client-side (`milestones_with_work_orders`) and renders it
    /// interactively, so reusing it is strictly more useful than a static
    /// text dump.
    pub(crate) fn view_milestone_order_action(&mut self, target: &ContextMenuTarget) -> bool {
        let (repo, tracking_issue) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                ..
            } => (repo_name.clone(), *tracking_issue),
            _ => return false,
        };
        let views = self.milestone_dag_views();
        let Some(idx) = views
            .iter()
            .position(|v| v.repo_name == repo && v.tracking_issue == tracking_issue)
        else {
            self.push_toast(
                "View order / DAG",
                "This milestone has no parsed `## Work order` block yet.",
                ToastSeverity::Info,
            );
            return false;
        };
        self.milestone_dag_sel = idx;
        // #1029: MilestoneDag has no ActivityBar entry of its own (reached
        // only as a Plans drill-down), so `switch_active_view` queues no
        // chrome update here — this is a plain `active_view` write with a
        // single call site, kept in the helper for consistency with every
        // other programmatic view switch.
        self.switch_active_view(SidebarView::MilestoneDag);
        true
    }

    /// Open the "Edit milestone…" quick dialog, pre-filled with the current
    /// title (#1003). Title-only — see [`MilestoneRowInputKind::EditTitle`].
    pub(crate) fn open_edit_milestone_input(&mut self, target: &ContextMenuTarget) -> bool {
        let (repo_name, tracking_issue, milestone_number, milestone_title) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                milestone_title,
                milestone_number,
            } => (
                repo_name.clone(),
                *tracking_issue,
                *milestone_number,
                milestone_title.clone(),
            ),
            _ => return false,
        };
        self.pending_milestone_row_input = Some(PendingMilestoneRowInput {
            kind: MilestoneRowInputKind::EditTitle,
            repo_name,
            tracking_issue,
            milestone_number,
            milestone_title: milestone_title.clone(),
            buf: milestone_title,
        });
        true
    }

    /// Open the "Add issue to milestone…" quick dialog (#1003).
    pub(crate) fn open_add_issue_to_milestone_input(&mut self, target: &ContextMenuTarget) -> bool {
        let (repo_name, tracking_issue, milestone_number, milestone_title) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                milestone_title,
                milestone_number,
            } => (
                repo_name.clone(),
                *tracking_issue,
                *milestone_number,
                milestone_title.clone(),
            ),
            _ => return false,
        };
        self.pending_milestone_row_input = Some(PendingMilestoneRowInput {
            kind: MilestoneRowInputKind::AddIssue,
            repo_name,
            tracking_issue,
            milestone_number,
            milestone_title,
            buf: String::new(),
        });
        true
    }

    /// Open the "Remove issue from milestone…" quick dialog (#1003).
    pub(crate) fn open_remove_issue_from_milestone_input(
        &mut self,
        target: &ContextMenuTarget,
    ) -> bool {
        let (repo_name, tracking_issue, milestone_number, milestone_title) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                milestone_title,
                milestone_number,
            } => (
                repo_name.clone(),
                *tracking_issue,
                *milestone_number,
                milestone_title.clone(),
            ),
            _ => return false,
        };
        self.pending_milestone_row_input = Some(PendingMilestoneRowInput {
            kind: MilestoneRowInputKind::RemoveIssue,
            repo_name,
            tracking_issue,
            milestone_number,
            milestone_title,
            buf: String::new(),
        });
        true
    }

    /// Open the "Add sub-issue to epic…" quick dialog (#1008).
    pub(crate) fn open_add_sub_issue_to_epic_input(&mut self, target: &ContextMenuTarget) -> bool {
        let (repo_name, tracking_issue, milestone_number, milestone_title) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                milestone_title,
                milestone_number,
            } => (
                repo_name.clone(),
                *tracking_issue,
                *milestone_number,
                milestone_title.clone(),
            ),
            _ => return false,
        };
        self.pending_milestone_row_input = Some(PendingMilestoneRowInput {
            kind: MilestoneRowInputKind::AddSubIssue,
            repo_name,
            tracking_issue,
            milestone_number,
            milestone_title,
            buf: String::new(),
        });
        true
    }

    /// Open the "Add sub-issue via chat…" quick dialog (#1017) — same
    /// target shape as [`Self::open_add_sub_issue_to_epic_input`], just a
    /// different `kind` so submission routes to the chat dispatch instead
    /// of the direct `add-child` CLI call.
    pub(crate) fn open_add_sub_issue_to_epic_chat_input(
        &mut self,
        target: &ContextMenuTarget,
    ) -> bool {
        let (repo_name, tracking_issue, milestone_number, milestone_title) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                milestone_title,
                milestone_number,
            } => (
                repo_name.clone(),
                *tracking_issue,
                *milestone_number,
                milestone_title.clone(),
            ),
            _ => return false,
        };
        self.pending_milestone_row_input = Some(PendingMilestoneRowInput {
            kind: MilestoneRowInputKind::AddSubIssueChat,
            repo_name,
            tracking_issue,
            milestone_number,
            milestone_title,
            buf: String::new(),
        });
        true
    }

    /// Submit handler for the #1003 Plans-row single-field input dialogs —
    /// mirrors `capture_plan_stub`'s validate-then-spawn-and-toast shape.
    pub(crate) fn submit_milestone_row_input(&mut self, input: PendingMilestoneRowInput) {
        let buf = input.buf.trim().to_string();
        if buf.is_empty() {
            self.push_toast("Milestone", "Nothing entered — cancelled.", ToastSeverity::Info);
            return;
        }
        // #1008: `AddSubIssue`'s buffer may carry more than a bare issue
        // number (an optional `{group/after}` annotation), so it gets its
        // own parse-and-dispatch path rather than the bare-number one below.
        if input.kind == MilestoneRowInputKind::AddSubIssue {
            self.submit_add_sub_issue_input(input, &buf);
            return;
        }
        // #1017: `AddSubIssueChat` is a bare issue number that dispatches a
        // milestone-chat session rather than the direct `add-child` CLI.
        if input.kind == MilestoneRowInputKind::AddSubIssueChat {
            self.submit_add_sub_issue_chat_input(input, &buf);
            return;
        }
        let needs_issue_number = matches!(
            input.kind,
            MilestoneRowInputKind::AddIssue | MilestoneRowInputKind::RemoveIssue
        );
        if needs_issue_number && buf.parse::<u64>().is_err() {
            self.push_toast(
                "Milestone",
                "Issue number must be numeric.",
                ToastSeverity::Warning,
            );
            return;
        }
        let ms_str = input.milestone_number.to_string();
        let (args, label): (Vec<String>, String) = match input.kind {
            MilestoneRowInputKind::EditTitle => (
                vec![
                    "milestone".into(),
                    "edit".into(),
                    input.repo_name.clone(),
                    ms_str,
                    "--title".into(),
                    buf.clone(),
                ],
                format!("Edit milestone: \"{}\"", buf),
            ),
            MilestoneRowInputKind::AddIssue => (
                vec![
                    "milestone".into(),
                    "assign".into(),
                    input.repo_name.clone(),
                    buf.clone(),
                    ms_str,
                ],
                format!("Add #{} to {}", buf, input.milestone_title),
            ),
            MilestoneRowInputKind::RemoveIssue => (
                vec![
                    "milestone".into(),
                    "remove".into(),
                    input.repo_name.clone(),
                    buf.clone(),
                ],
                format!("Remove #{} from {}", buf, input.milestone_title),
            ),
            // Handled by `submit_add_sub_issue_input` above (early return) —
            // this arm exists only so the match stays exhaustive.
            MilestoneRowInputKind::AddSubIssue => {
                unreachable!("AddSubIssue is dispatched via submit_add_sub_issue_input")
            }
            // Handled by `submit_add_sub_issue_chat_input` above (early
            // return) — this arm exists only so the match stays exhaustive.
            MilestoneRowInputKind::AddSubIssueChat => {
                unreachable!("AddSubIssueChat is dispatched via submit_add_sub_issue_chat_input")
            }
        };
        let arg_refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&arg_refs) {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    &label,
                    "queued — will run after current command.",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(&label, "dispatching…", ToastSeverity::Info);
            }
        }
    }

    /// Parse + dispatch the "Add sub-issue to epic…" input (#1008): an
    /// issue number optionally followed by a `{group: G, after: #N,...}`
    /// annotation — the same grammar a `## Sub-issues` / `## Work order`
    /// checklist line uses, so `1050`, `1050 {group: B}`, and
    /// `1050{after: #1049}` all parse. Reuses the module's own
    /// [`parse_annotations`] (the pure Rust port of
    /// `coord.milestone_order`'s annotation grammar already used to render
    /// the DAG) rather than re-deriving it. Fires `coord milestone
    /// add-child <repo> <tracking_issue> <issue> [--group G] [--after
    /// N,...]`.
    fn submit_add_sub_issue_input(&mut self, input: PendingMilestoneRowInput, buf: &str) {
        let split_at = buf.find(|c: char| c.is_whitespace() || c == '{');
        let (issue_part, rest) = match split_at {
            Some(idx) => (buf[..idx].trim(), buf[idx..].trim()),
            None => (buf, ""),
        };
        let Ok(issue_number) = issue_part.trim_start_matches('#').parse::<u64>() else {
            self.push_toast(
                "Add sub-issue",
                "Issue number must be numeric.",
                ToastSeverity::Warning,
            );
            return;
        };

        let mut args: Vec<String> = vec![
            "milestone".into(),
            "add-child".into(),
            input.repo_name.clone(),
            input.tracking_issue.to_string(),
            issue_number.to_string(),
        ];
        if !rest.is_empty() {
            let inside = rest.trim_start_matches('{').trim_end_matches('}');
            let (group, after) = parse_annotations(inside);
            if let Some(g) = group {
                args.push("--group".into());
                args.push(g);
            }
            if !after.is_empty() {
                args.push("--after".into());
                args.push(
                    after
                        .iter()
                        .map(|n| n.to_string())
                        .collect::<Vec<_>>()
                        .join(","),
                );
            }
        }

        let label = format!(
            "Add #{} to {}'s sub-issues",
            issue_number, input.milestone_title
        );
        let arg_refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&arg_refs) {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    &label,
                    "queued — will run after current command.",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(&label, "dispatching…", ToastSeverity::Info);
            }
        }
    }

    /// Parse + dispatch the "Add sub-issue via chat…" input (#1017, #1029):
    /// a bare candidate issue number — unlike
    /// [`Self::submit_add_sub_issue_input`], no `{group/after}` annotation
    /// grammar here; that gets proposed and confirmed live in the chat
    /// instead. Launches (or reattaches to) an interactive milestone-chat
    /// session via [`Self::launch_milestone_chat_session`] with the
    /// candidate child issue, which seeds `resolve_milestone_chat_briefing`'s
    /// "Add sub-issue" mode (`coord/milestone_chat.py`) with the epic body
    /// plus the candidate issue.
    fn submit_add_sub_issue_chat_input(&mut self, input: PendingMilestoneRowInput, buf: &str) {
        let issue_part = buf.trim().trim_start_matches('#');
        let Ok(issue_number) = issue_part.parse::<u64>() else {
            self.push_toast(
                "Add sub-issue via chat",
                "Issue number must be numeric.",
                ToastSeverity::Warning,
            );
            return;
        };

        self.launch_milestone_chat_session(
            input.repo_name.clone(),
            input.tracking_issue,
            Some(issue_number),
        );
    }

    /// Open the "Close / archive plan" confirm (#1003) — mirrors
    /// `pending_restart`'s y/any-other-key-cancels pattern.
    pub(crate) fn open_close_plan_confirm(&mut self, target: &ContextMenuTarget) -> bool {
        let (repo_name, tracking_issue, milestone_title) = match target {
            ContextMenuTarget::MilestoneHeader {
                repo_name,
                tracking_issue,
                milestone_title,
                ..
            } => (repo_name.clone(), *tracking_issue, milestone_title.clone()),
            _ => return false,
        };
        self.pending_close_plan = Some(PendingClosePlan {
            repo_name,
            tracking_issue,
            milestone_title,
        });
        true
    }

    /// Fire `coord issue close <repo> <tracking_issue>` for a confirmed
    /// "Close / archive plan" (#1003).
    pub(crate) fn confirm_close_plan(&mut self, plan: PendingClosePlan) {
        let issue_str = plan.tracking_issue.to_string();
        use crate::commands::SpawnQueuedOutcome;
        match self
            .command_runner
            .spawn_queued(&["issue", "close", &plan.repo_name, &issue_str])
        {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Close plan",
                    &format!("#{}: queued — will close after current command.", plan.tracking_issue),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Close plan",
                    &format!("#{} ({}): closing…", plan.tracking_issue, plan.milestone_title),
                    ToastSeverity::Info,
                );
            }
        }
    }
}

// ─── Pure-function unit tests ─────────────────────────────────────────────────

#[cfg(test)]
mod pure_tests {
    use super::*;

    fn issue(repo: &str, number: u64, title: &str, state: &str, labels: &[&str]) -> OpenIssue {
        OpenIssue {
            repo_name: repo.to_string(),
            number,
            title: title.to_string(),
            body: String::new(),
            labels: labels.iter().map(|s| s.to_string()).collect(),
            state: state.to_string(),
            milestone_number: None,
            milestone_title: None,
            body_truncated: false,
            body_len: None,
            // #1941: fields added to the generated wire DTO; not exercised by this test.
            synced_at: None,
        }
    }

    fn make_assignment(repo: &str, issue_number: u64, status: &str) -> Assignment {
        make_assignment_typed(repo, issue_number, status, None)
    }

    fn make_assignment_typed(
        repo: &str,
        issue_number: u64,
        status: &str,
        atype: Option<&str>,
    ) -> Assignment {
        Assignment {
            id: "a1".to_string(),
            repo: repo.to_string(),
            issue_number,
            issue_title: "Test issue".to_string(),
            machine: "testmachine".to_string(),
            status: status.to_string(),
            branch: None,
            model: None,
            dispatched_at: None,
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

    #[test]
    fn parse_work_order_basic() {
        let body = "\
Some intro text.

## Work order
- [ ] #762  {group: A}        # cohort A
- [ ] #763  {group: A}
- [x] #765  {after: #762,#763}   # hard dependency edge
- [ ] #766  {after: #765}

## Notes
Not part of the block.
";
        let nodes = parse_work_order(body);
        assert_eq!(nodes.len(), 4);
        assert_eq!(nodes[0].issue_number, 762);
        assert_eq!(nodes[0].group.as_deref(), Some("A"));
        assert!(nodes[0].after.is_empty());
        assert_eq!(nodes[2].issue_number, 765);
        assert_eq!(nodes[2].after, vec![762, 763]);
        assert_eq!(nodes[3].after, vec![765]);
    }

    #[test]
    fn milestone_dag_complete_for_tracking_body_larger_than_wire_document_cap() {
        // #1337 review finding: the daemon bounds member-issue bodies on the
        // /board wire at 16 KB (coord.board_wire.DOCUMENT_CHARS) but must
        // EXEMPT tracking (epic) bodies, because this view parses
        // `## Work order` client-side.  Guard the client half: a tracking
        // body whose work-order items sit past the 16 KB mark must still
        // yield the complete DAG — every node, edges intact.
        let prose = "x".repeat(16_384 + 100);
        let body = format!(
            "## Work order\n{prose}\n- [ ] #4242 {{group: A}}\n- [ ] #4243 {{after: #4242}}\n"
        );
        let mut epic = issue("repo", 4200, "Epic", "open", &["epic"]);
        epic.milestone_number = Some(5);
        epic.body = body;
        let mut m1 = issue("repo", 4242, "member a", "open", &[]);
        m1.milestone_number = Some(5);
        let mut m2 = issue("repo", 4243, "member b", "open", &[]);
        m2.milestone_number = Some(5);
        let issues = vec![epic, m1, m2];

        let views = milestones_with_work_orders(&issues, &[]);
        assert_eq!(views.len(), 1, "tracking issue must produce a DAG view");
        let numbers: Vec<u64> = views[0].nodes.iter().map(|n| n.issue_number).collect();
        assert_eq!(
            numbers,
            vec![4242, 4243],
            "work-order items past the wire document cap must not be dropped",
        );
        // The dependency edge past the cap survives too.
        assert_eq!(views[0].nodes[1].after, vec![4242]);
    }

    #[test]
    fn parse_work_order_no_heading_returns_empty() {
        assert!(parse_work_order("just a normal issue body, no work order here").is_empty());
    }

    #[test]
    fn parse_work_order_group_after_either_order() {
        // `after` before `group` in the annotation body.
        let body = "## Work order\n- [ ] #1  {after: #2, group: B}\n- [ ] #2\n";
        let nodes = parse_work_order(body);
        assert_eq!(nodes[0].group.as_deref(), Some("B"));
        assert_eq!(nodes[0].after, vec![2]);
    }

    #[test]
    fn parse_work_order_skips_malformed_lines() {
        let body = "## Work order\n- not a valid item\n- [ ] #5\n";
        let nodes = parse_work_order(body);
        assert_eq!(nodes.len(), 1);
        assert_eq!(nodes[0].issue_number, 5);
    }

    /// #1198: `labels_carry_epic_label` — the single source of truth every
    /// call site now delegates to — must accept `epic` in any casing and
    /// reject everything else, including a merely-similar label.
    #[test]
    fn labels_carry_epic_label_is_case_insensitive() {
        assert!(labels_carry_epic_label(&["epic".to_string()]));
        assert!(labels_carry_epic_label(&["Epic".to_string()]));
        assert!(labels_carry_epic_label(&["EPIC".to_string()]));
        assert!(labels_carry_epic_label(&["coord".to_string(), "EpIc".to_string()]));
        assert!(!labels_carry_epic_label(&["coord".to_string()]));
        assert!(!labels_carry_epic_label(&["epics".to_string()]));
        assert!(!labels_carry_epic_label(&[]));
    }

    #[test]
    fn milestone_tracking_issue_requires_epic_label_and_milestone_match() {
        let mut a = issue("repo", 100, "Epic", "open", &["epic", "coord"]);
        a.milestone_number = Some(5);
        let b = issue("repo", 101, "Not an epic", "open", &["coord"]);
        let issues = vec![a, b];
        let found = milestone_tracking_issue(&issues, "repo", 5);
        assert_eq!(found.map(|i| i.number), Some(100));
        assert!(milestone_tracking_issue(&issues, "repo", 99).is_none());
    }

    #[test]
    fn build_dag_nodes_computes_done_inflight_blocked_ready() {
        let work_order = vec![
            WorkOrderNode { issue_number: 1, group: None, after: vec![] },
            WorkOrderNode { issue_number: 2, group: None, after: vec![1] },
            WorkOrderNode { issue_number: 3, group: None, after: vec![2] },
            WorkOrderNode { issue_number: 4, group: None, after: vec![] },
        ];
        let open_issues = vec![
            issue("repo", 1, "Done one", "closed", &[]),
            issue("repo", 2, "In flight", "open", &[]),
            issue("repo", 3, "Blocked", "open", &[]),
            issue("repo", 4, "Ready", "open", &[]),
        ];
        let assignments = vec![make_assignment("repo", 2, "running")];
        let nodes = build_dag_nodes(&work_order, "repo", &open_issues, &assignments);
        assert_eq!(nodes[0].state, NodeState::Done);
        assert_eq!(nodes[1].state, NodeState::InFlight);
        assert_eq!(nodes[2].state, NodeState::Blocked(vec![2]));
        assert_eq!(nodes[3].state, NodeState::Ready);
    }

    /// #771 review finding 1: an issue absent from the local open-issues
    /// cache entirely (the effective outcome once a closed issue ages out of
    /// `coord/state.py`'s 7-day prune) must be presumed `Done`, not shown as
    /// an unhelpful `Unknown`/`?` — the local cache only ever drops rows for
    /// being closed-and-stale, never for a still-open issue.
    #[test]
    fn build_dag_nodes_presumes_done_when_issue_not_synced() {
        let work_order = vec![WorkOrderNode { issue_number: 99, group: None, after: vec![] }];
        let nodes = build_dag_nodes(&work_order, "repo", &[], &[]);
        assert_eq!(nodes[0].state, NodeState::Done);
        // Title falls back to the bare issue number when nothing is cached.
        assert_eq!(nodes[0].title, "#99");
    }

    /// #771 review finding 2: a dependency that has aged out of the cache
    /// (missing entirely, same as above) must clear the `after` edge rather
    /// than leaving the dependent node incorrectly `Blocked` on something
    /// that's actually done.
    #[test]
    fn build_dag_nodes_ready_when_dependency_aged_out_of_cache() {
        let work_order = vec![
            WorkOrderNode { issue_number: 765, group: None, after: vec![] },
            WorkOrderNode { issue_number: 766, group: None, after: vec![765] },
        ];
        // #765 is NOT present in open_issues — simulating a real closed
        // issue that's aged out of the 7-day-stale-closed prune.
        let open_issues = vec![issue("repo", 766, "Independent, ready", "open", &[])];
        let nodes = build_dag_nodes(&work_order, "repo", &open_issues, &[]);
        assert_eq!(nodes[0].state, NodeState::Done);
        assert_eq!(nodes[1].state, NodeState::Ready);
    }

    // ── #1287: completed-assignment ⇒ InFlight, not Ready ────────────────────

    /// #1287: a child whose work assignment has FINISHED (status="done") but
    /// whose issue is still OPEN (Test/Review/Merge still pending) must be
    /// `InFlight`, not `Ready`.  Before this fix, the `InFlight` arm only
    /// checked `status == "running"`, so a settled work row fell through to
    /// `Ready`, dragging the parent epic into "New".
    #[test]
    fn build_dag_nodes_completed_work_assignment_is_inflight() {
        let work_order = vec![WorkOrderNode { issue_number: 10, group: None, after: vec![] }];
        let open_issues = vec![issue("repo", 10, "Worked child", "open", &[])];
        // status="done", type="work" — completed but issue still open
        let assignments = vec![make_assignment_typed("repo", 10, "done", Some("work"))];
        let nodes = build_dag_nodes(&work_order, "repo", &open_issues, &assignments);
        assert_eq!(
            nodes[0].state,
            NodeState::InFlight,
            "an open issue with a settled work assignment must be InFlight, not Ready",
        );
    }

    /// #1287: a child with ONLY a refinement assignment (not workable) must
    /// stay `Ready` — scoping conversations are NOT pipeline execution.
    #[test]
    fn build_dag_nodes_refinement_assignment_stays_ready() {
        let work_order = vec![WorkOrderNode { issue_number: 11, group: None, after: vec![] }];
        let open_issues = vec![issue("repo", 11, "Refining child", "open", &[])];
        let assignments = vec![make_assignment_typed("repo", 11, "done", Some("refinement"))];
        let nodes = build_dag_nodes(&work_order, "repo", &open_issues, &assignments);
        assert_eq!(
            nodes[0].state,
            NodeState::Ready,
            "a refinement assignment must NOT promote a node out of Ready",
        );
    }

    /// #1287: same for other non-workable assignment types: `chat`,
    /// `new-issue-chat`, and `test-chat` must all leave the node at `Ready`.
    #[test]
    fn build_dag_nodes_chat_assignments_stay_ready() {
        for ty in ["chat", "new-issue-chat", "test-chat"] {
            let work_order = vec![WorkOrderNode { issue_number: 12, group: None, after: vec![] }];
            let open_issues = vec![issue("repo", 12, "Chat child", "open", &[])];
            let assignments = vec![make_assignment_typed("repo", 12, "done", Some(ty))];
            let nodes = build_dag_nodes(&work_order, "repo", &open_issues, &assignments);
            assert_eq!(
                nodes[0].state,
                NodeState::Ready,
                "assignment_type={ty:?} must NOT promote a node out of Ready",
            );
        }
    }

    /// #1287: a running work assignment (the original `status == "running"` case)
    /// must still be `InFlight` — the fix must not regress the pre-existing path.
    #[test]
    fn build_dag_nodes_running_work_assignment_still_inflight() {
        let work_order = vec![WorkOrderNode { issue_number: 13, group: None, after: vec![] }];
        let open_issues = vec![issue("repo", 13, "Running child", "open", &[])];
        let assignments = vec![make_assignment_typed("repo", 13, "running", Some("work"))];
        let nodes = build_dag_nodes(&work_order, "repo", &open_issues, &assignments);
        assert_eq!(
            nodes[0].state,
            NodeState::InFlight,
            "a running work assignment must still be InFlight",
        );
    }

    /// #1287: "Dispatch next…" uses the first `NodeState::Ready` node.  A
    /// child with a completed work assignment must NOT be `Ready`, so it can
    /// no longer be incorrectly re-dispatched.
    #[test]
    fn build_dag_nodes_dispatch_next_skips_worked_child() {
        // Node 20 has a done work assignment — must be InFlight, not picked.
        // Node 21 has no assignment — must be Ready, picked first.
        let work_order = vec![
            WorkOrderNode { issue_number: 20, group: None, after: vec![] },
            WorkOrderNode { issue_number: 21, group: None, after: vec![] },
        ];
        let open_issues = vec![
            issue("repo", 20, "Worked", "open", &[]),
            issue("repo", 21, "Not yet started", "open", &[]),
        ];
        let assignments = vec![make_assignment_typed("repo", 20, "done", Some("work"))];
        let nodes = build_dag_nodes(&work_order, "repo", &open_issues, &assignments);
        assert_eq!(nodes[0].state, NodeState::InFlight);
        assert_eq!(nodes[1].state, NodeState::Ready);
        // Simulate "Dispatch next…" — first Ready wins.
        let next = nodes.iter().find(|n| n.state == NodeState::Ready).map(|n| n.issue_number);
        assert_eq!(
            next,
            Some(21),
            "Dispatch next must skip the worked child (#20 is InFlight) and pick #21",
        );
    }

    #[test]
    fn nodes_by_cohort_groups_and_preserves_order() {
        let nodes = vec![
            MilestoneDagNode {
                issue_number: 1,
                title: "a".into(),
                group: Some("A".into()),
                after: vec![],
                state: NodeState::Ready,
            },
            MilestoneDagNode {
                issue_number: 2,
                title: "b".into(),
                group: None,
                after: vec![],
                state: NodeState::Ready,
            },
            MilestoneDagNode {
                issue_number: 3,
                title: "c".into(),
                group: Some("A".into()),
                after: vec![],
                state: NodeState::Ready,
            },
        ];
        let cohorts = nodes_by_cohort(&nodes);
        assert_eq!(cohorts.len(), 2);
        assert_eq!(cohorts[0].0.as_deref(), Some("A"));
        assert_eq!(cohorts[0].1.len(), 2);
        assert_eq!(cohorts[1].0, None);
        assert_eq!(cohorts[1].1.len(), 1);
    }

    #[test]
    fn milestones_with_work_orders_finds_and_sorts() {
        let mut epic = issue("repo", 100, "Epic", "open", &["epic"]);
        epic.milestone_number = Some(5);
        epic.milestone_title = Some("v0.5".to_string());
        epic.body = "## Work order\n- [ ] #101\n".to_string();
        let mut child = issue("repo", 101, "Child", "open", &[]);
        child.milestone_number = Some(5);
        let issues = vec![epic, child];
        let views = milestones_with_work_orders(&issues, &[]);
        assert_eq!(views.len(), 1);
        assert_eq!(views[0].tracking_issue, 100);
        assert_eq!(views[0].milestone_title, "v0.5");
        assert_eq!(views[0].nodes.len(), 1);
    }

    #[test]
    fn milestones_without_work_order_block_are_skipped() {
        let mut epic = issue("repo", 100, "Epic", "open", &["epic"]);
        epic.milestone_number = Some(5);
        epic.body = "just a normal description, no work order".to_string();
        let views = milestones_with_work_orders(&[epic], &[]);
        assert!(views.is_empty());
    }
}
